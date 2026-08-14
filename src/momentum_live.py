#!/usr/bin/env python3
"""Route persisted momentum OPEN/HOLD/EXIT decisions to the job runner.

This adapter performs no exchange, credential, or network operation.  It uses
``momentum_lifecycle`` as the only signal/state machine.  It publishes one
``MOMENTUM_OPEN`` per ignition, adopts only runner-ledger fills while the
runner position stands, and publishes one ``MOMENTUM_EXIT`` when HOLD logic
fails closed.  ADD and REDUCE remain durable decisions only.

The decision ledger and lifecycle state are durable before job publication.
Job ids are derived only from symbol plus the ignition stream timestamp, and
publication is an atomic create-if-absent operation.  A restart in the
state-to-job crash window therefore recovers the same immutable job instead of
issuing another.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

import momentum_lifecycle as lifecycle

try:
    from limits import PER_LEG_CAP_USDT
except ImportError:
    PER_LEG_CAP_USDT = Decimal("32")


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DECISIONS_PATH = ROOT / "decisions.jsonl"
DECISIONS_ROTATE_BYTES = 4 * 1024 * 1024
HARD_MAX_NOTIONAL = PER_LEG_CAP_USDT
DEFAULT_INITIAL_NOTIONAL = Decimal("8")
DEFAULT_JOB_TTL = timedelta(minutes=10)
MAX_JOB_TTL = timedelta(minutes=15)
REQUESTED_BY = "momentum_live"
JOB_OPEN_OP = "MOMENTUM_OPEN"
JOB_EXIT_OP = "MOMENTUM_EXIT"
JOB_OPS = frozenset((JOB_OPEN_OP, JOB_EXIT_OP))
# Kept as the entry-operation alias for callers that used ``JOB_OP``.
JOB_OP = JOB_OPEN_OP
RUNNER_SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
RUNNER_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
RUNNER_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
JOB_FIELDS = frozenset(
    {
        "job_id",
        "op",
        "symbol",
        "max_notional",
        "created_utc",
        "expires_utc",
        "requested_by",
    }
)


class MomentumLiveError(Exception):
    """A local validation or durable-publication failure."""


@dataclass(frozen=True)
class TickResult:
    decisions: Tuple[Dict[str, Any], ...]
    replayed_receipts: int
    recovered_job_ids: Tuple[str, ...]
    written_job_ids: Tuple[str, ...]


@dataclass(frozen=True)
class RunnerPosition:
    state: str
    symbol: Optional[str]
    open_job_id: Optional[str]
    exit_job_id: Optional[str]
    quantity: Optional[Decimal]
    entry_price: Optional[Decimal]


@dataclass
class _DecisionIndex:
    signature: Tuple[Any, ...]
    rows_by_id: Dict[str, Dict[str, Any]]
    ignition_by_symbol: Dict[str, str]
    job_rows: List[Dict[str, Any]]


_DECISION_INDEXES: Dict[str, _DecisionIndex] = {}


def default_jobs_dir() -> Path:
    receipt_dir = Path(
        os.path.expanduser(os.environ.get("RECEIPT_DIR", "~/quant_receipts"))
    )
    return receipt_dir / "jobs"


def default_position_path(jobs_dir: Path) -> Path:
    return Path(jobs_dir).parent / "momentum-position.json"


def default_results_path(jobs_dir: Path) -> Path:
    return Path(jobs_dir) / "results.jsonl"


def default_lifecycle_config() -> lifecycle.LifecycleConfig:
    """Use the existing brain with an entry request that fits the runner."""
    return lifecycle.LifecycleConfig(initial_notional=DEFAULT_INITIAL_NOTIONAL)


def _decimal(raw: Any, name: str) -> Decimal:
    if isinstance(raw, bool):
        raise MomentumLiveError(f"{name} must be a finite Decimal")
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise MomentumLiveError(f"{name} must be a finite Decimal") from None
    if not value.is_finite():
        raise MomentumLiveError(f"{name} must be a finite Decimal")
    return value


def _parse_utc(raw: Any, name: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise MomentumLiveError(f"{name} must be a timezone-aware UTC timestamp")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise MomentumLiveError(
            f"{name} must be a timezone-aware UTC timestamp"
        ) from None
    if value.tzinfo is None:
        raise MomentumLiveError(f"{name} must be a timezone-aware UTC timestamp")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def deterministic_job_id(
    symbol: str, ignition_utc: str, op: str = JOB_OPEN_OP
) -> str:
    """Return the sole OPEN or EXIT runner identity for one ignition."""
    if not isinstance(symbol, str) or RUNNER_SYMBOL_RE.fullmatch(symbol) is None:
        raise MomentumLiveError("symbol does not fit the runner schema")
    _parse_utc(ignition_utc, "ignition_utc")
    if op not in JOB_OPS:
        raise MomentumLiveError("momentum job op is invalid")
    digest = hashlib.sha256(
        "\0".join((symbol, ignition_utc, op)).encode("utf-8")
    ).hexdigest()[:18]
    kind = "open" if op == JOB_OPEN_OP else "exit"
    job_id = f"mom-{kind}-{symbol.lower()}-{digest}"
    if RUNNER_JOB_ID_RE.fullmatch(job_id) is None:
        raise MomentumLiveError("derived job id does not fit the runner schema")
    return job_id


def _json_with_decimal(payload: Mapping[str, Any]) -> bytes:
    """Encode the job's Decimal as a JSON number without a float conversion."""
    pieces: List[str] = []
    for name in sorted(payload):
        if name == "max_notional":
            rendered = lifecycle.fmt(_decimal(payload[name], name))
        else:
            rendered = json.dumps(payload[name], ensure_ascii=True)
        pieces.append(json.dumps(name) + ":" + rendered)
    return ("{" + ",".join(pieces) + "}\n").encode("utf-8")


def _validate_job_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != JOB_FIELDS:
        raise MomentumLiveError("job payload does not match the runner schema")
    strings = (
        "job_id",
        "op",
        "symbol",
        "created_utc",
        "expires_utc",
        "requested_by",
    )
    if any(type(payload.get(name)) is not str for name in strings):
        raise MomentumLiveError("job payload has a non-string identity field")
    job_id = payload["job_id"]
    symbol = payload["symbol"]
    if RUNNER_JOB_ID_RE.fullmatch(job_id) is None:
        raise MomentumLiveError("job_id does not fit the runner schema")
    if RUNNER_SYMBOL_RE.fullmatch(symbol) is None:
        raise MomentumLiveError("symbol does not fit the runner schema")
    if payload["op"] not in JOB_OPS:
        raise MomentumLiveError("job op is not a momentum OPEN or EXIT")
    requester = payload["requested_by"]
    if not requester.strip() or len(requester) > 128:
        raise MomentumLiveError("requested_by does not fit the runner schema")
    if RUNNER_UTC_RE.fullmatch(payload["created_utc"]) is None \
            or RUNNER_UTC_RE.fullmatch(payload["expires_utc"]) is None:
        raise MomentumLiveError("job timestamp does not fit the runner schema")
    notional = _decimal(payload["max_notional"], "max_notional")
    if notional <= 0 or notional > HARD_MAX_NOTIONAL:
        raise MomentumLiveError("job notional is outside the compiled runner cap")
    created = _parse_utc(payload["created_utc"], "created_utc")
    expires = _parse_utc(payload["expires_utc"], "expires_utc")
    if expires <= created or expires - created > MAX_JOB_TTL:
        raise MomentumLiveError("job expiry window does not fit the runner schema")
    return {**dict(payload), "max_notional": notional}


def _read_existing_job(path: Path) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle, parse_float=Decimal, parse_int=Decimal)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise MomentumLiveError(f"existing job is unreadable: {path}") from None
    if not isinstance(raw, dict):
        raise MomentumLiveError(f"existing job is not an object: {path}")
    return _validate_job_payload(raw)


def _same_job(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left[name] == right[name] for name in JOB_FIELDS)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_job_record(jobs_dir: Path, payload: Mapping[str, Any]) -> bool:
    """Atomically publish one immutable runner job; return True iff created."""
    validated = _validate_job_payload(payload)
    jobs_dir = Path(jobs_dir)
    jobs_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    final_path = jobs_dir / f"job-{validated['job_id']}.json"
    if final_path.exists():
        existing = _read_existing_job(final_path)
        if not _same_job(existing, validated):
            raise MomentumLiveError(
                f"deterministic job id conflicts with existing record: {validated['job_id']}"
            )
        return False

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".job-{validated['job_id']}.", suffix=".tmp", dir=jobs_dir
    )
    temporary = Path(temporary_name)
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        encoded = _json_with_decimal(validated)
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("job write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, final_path)
            published = True
        except FileExistsError:
            existing = _read_existing_job(final_path)
            if not _same_job(existing, validated):
                raise MomentumLiveError(
                    "deterministic job id was concurrently published with different data"
                ) from None
        _fsync_directory(jobs_dir)
        return published
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _decision_backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".1")


def _decision_file_signature(path: Path) -> Optional[Tuple[int, int, int, int]]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        raise MomentumLiveError(f"decision ledger is unreadable: {path}") from None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _decision_signature(path: Path) -> Tuple[Any, ...]:
    return (
        _decision_file_signature(_decision_backup_path(path)),
        _decision_file_signature(path),
    )


def _read_decision_file(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise MomentumLiveError(f"decision ledger is unreadable: {path}") from None
    rows: List[Dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _read_decisions(path: Path) -> List[Dict[str, Any]]:
    """Read the two bounded decision segments, oldest first."""
    path = Path(path)
    return (
        _read_decision_file(_decision_backup_path(path))
        + _read_decision_file(path)
    )


def _index_decision(index: _DecisionIndex, row: Mapping[str, Any]) -> None:
    copied = dict(row)
    decision_id = copied.get("decision_id")
    if isinstance(decision_id, str) and decision_id:
        existing = index.rows_by_id.get(decision_id)
        if existing is not None:
            if existing != copied:
                raise MomentumLiveError(f"decision id conflict: {decision_id}")
            return
        index.rows_by_id[decision_id] = copied
    symbol = copied.get("symbol")
    if isinstance(symbol, str):
        ignition_utc = copied.get("ignition_utc")
        if isinstance(ignition_utc, str):
            index.ignition_by_symbol[symbol] = ignition_utc
        snapshot = copied.get("lifecycle_state")
        if isinstance(snapshot, dict) \
                and snapshot.get("position_status") == "FLAT" \
                and snapshot.get("phase") == lifecycle.PHASE_WATCH \
                and snapshot.get("ignition_utc") is None:
            index.ignition_by_symbol.pop(symbol, None)
    if copied.get("action") in {
        lifecycle.ACTION_ENTER_SMALL, lifecycle.ACTION_EXIT
    } and copied.get("job_eligible") is True:
        index.job_rows.append(copied)


def _decision_cache_key(path: Path) -> str:
    return os.path.abspath(str(path))


def _decision_index(path: Path) -> _DecisionIndex:
    """Return a cached O(1)-lookup index, rebuilding only after file changes."""
    path = Path(path)
    key = _decision_cache_key(path)
    signature = _decision_signature(path)
    cached = _DECISION_INDEXES.get(key)
    if cached is not None and cached.signature == signature:
        return cached
    index = _DecisionIndex(signature, {}, {}, [])
    for row in _read_decisions(path):
        _index_decision(index, row)
    index.signature = _decision_signature(path)
    _DECISION_INDEXES[key] = index
    return index


def _rotate_decisions(path: Path, incoming_bytes: int) -> bool:
    if DECISIONS_ROTATE_BYTES <= 0:
        raise MomentumLiveError("decision rotation threshold must be positive")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    except OSError:
        raise MomentumLiveError(f"decision ledger is unreadable: {path}") from None
    if size == 0 or size + incoming_bytes <= DECISIONS_ROTATE_BYTES:
        return False
    try:
        os.replace(path, _decision_backup_path(path))
        _fsync_directory(path.parent)
    except OSError:
        raise MomentumLiveError(f"decision ledger rotation failed: {path}") from None
    _DECISION_INDEXES.pop(_decision_cache_key(path), None)
    return True


def _data_decimal(raw: Any, name: str) -> Optional[Decimal]:
    """Parse a runner-owned Decimal string; null stays unknown."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise MomentumLiveError(f"{name} must be a Decimal string or null")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise MomentumLiveError(f"{name} must be a finite Decimal string") from None
    if not value.is_finite():
        raise MomentumLiveError(f"{name} must be a finite Decimal string")
    return value


def _first(raw: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
    return None


def read_runner_position(path: Path) -> Optional[RunnerPosition]:
    """Read the runner's single momentum-position state without inference."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise MomentumLiveError(f"momentum position state is unreadable: {path}") from None
    if not isinstance(raw, dict):
        raise MomentumLiveError("momentum position state is not an object")
    nested = raw.get("position")
    if isinstance(nested, dict):
        raw = nested
    state = _first(raw, ("state", "status", "position_state"))
    if not isinstance(state, str):
        raise MomentumLiveError("momentum position state has no status")
    runner_state = state.upper()
    aliases = {
        "NONE": "FLAT", "CLOSED": "FLAT", "OPENED": "OPEN",
        "CLOSING": "EXIT_PENDING",
    }
    state = aliases.get(runner_state, runner_state)
    if state not in {"FLAT", "OPEN", "EXIT_PENDING", "OPEN_PENDING", "UNKNOWN"}:
        raise MomentumLiveError("momentum position state has invalid status")
    symbol = raw.get("symbol")
    if symbol is not None and (
        not isinstance(symbol, str) or RUNNER_SYMBOL_RE.fullmatch(symbol) is None
    ):
        raise MomentumLiveError("momentum position state has invalid symbol")
    job_id = raw.get("job_id")
    open_job_id = _first(raw, ("open_job_id", "entry_job_id"))
    exit_job_id = _first(raw, ("exit_job_id", "close_job_id"))
    if open_job_id is None and runner_state in {"OPENING", "OPEN"}:
        open_job_id = job_id
    if exit_job_id is None and runner_state in {"CLOSING", "CLOSED"}:
        exit_job_id = job_id
    for name, value in (("open_job_id", open_job_id), ("exit_job_id", exit_job_id)):
        if value is not None and (
            not isinstance(value, str) or RUNNER_JOB_ID_RE.fullmatch(value) is None
        ):
            raise MomentumLiveError(f"momentum position state has invalid {name}")
    entry = raw.get("entry") if isinstance(raw.get("entry"), dict) else {}
    quantity_raw = _first(
        raw, ("measured_qty", "qty", "quantity", "position_qty", "executed_qty")
    )
    if quantity_raw is None and state in {"OPEN", "EXIT_PENDING"}:
        quantity_raw = _first(entry, ("qty", "quantity", "executed_qty"))
    quantity = _data_decimal(
        quantity_raw,
        "momentum position quantity",
    )
    entry_price_raw = _first(
        raw, ("entry_price", "avg_entry_price", "fill_price")
    )
    if entry_price_raw is None:
        entry_price_raw = _first(entry, ("price", "entry_price", "avg_fill_price"))
    entry_price = _data_decimal(
        entry_price_raw,
        "momentum position entry_price",
    )
    if state in {"OPEN", "EXIT_PENDING"}:
        identity = open_job_id if state == "OPEN" else exit_job_id
        if symbol is None or identity is None or quantity is None or quantity <= 0:
            raise MomentumLiveError("standing momentum position lacks proven identity or quantity")
    elif quantity is not None and quantity != 0:
        raise MomentumLiveError("non-open momentum position has non-zero quantity")
    return RunnerPosition(
        state, symbol, open_job_id, exit_job_id, quantity, entry_price
    )


def read_results_ledger(path: Path) -> List[Dict[str, Any]]:
    """Read complete result rows, ignoring only a torn final append."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise MomentumLiveError(f"runner results ledger is unreadable: {path}") from None
    rows: List[Dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line, parse_float=Decimal, parse_int=Decimal)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                continue
            raise MomentumLiveError("runner results ledger has a torn interior row") from None
        if not isinstance(row, dict):
            raise MomentumLiveError("runner results ledger row is not an object")
        rows.append(row)
    return rows


def _result_for_job(
    rows: Iterable[Mapping[str, Any]], job_id: str, op: str, symbol: str
) -> Optional[Mapping[str, Any]]:
    matches = [row for row in rows if row.get("job_id") == job_id]
    if not matches:
        return None
    canonical = {
        json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=str)
        for row in matches
    }
    if len(canonical) != 1:
        raise MomentumLiveError(f"runner results conflict for job {job_id}")
    row = matches[-1]
    if row.get("op") != op or row.get("symbol") != symbol:
        raise MomentumLiveError(f"runner result identity conflicts for job {job_id}")
    return row


def _observed_price_quantum(value: Decimal) -> Decimal:
    """Infer the finest visible exchange price unit without trailing zeroes."""
    exponent = min(value.normalize().as_tuple().exponent, 0)
    return Decimal(1).scaleb(exponent)


def _within_fill_rounding(
    reported: Decimal, computed: Decimal, fill_prices: Iterable[Decimal]
) -> bool:
    """Allow at most half the finest price unit visible in exchange facts."""
    quantum = _observed_price_quantum(reported)
    for fill_price in fill_prices:
        quantum = min(quantum, _observed_price_quantum(fill_price))
    return abs(reported - computed) <= quantum / Decimal("2")


def _fill_values(
    raw: Mapping[str, Any], source: str
) -> Tuple[Optional[Decimal], Optional[Decimal], Tuple[Decimal, ...]]:
    containers: List[Mapping[str, Any]] = [raw]
    for name in ("fill", "open", "position"):
        nested = raw.get(name)
        if isinstance(nested, dict):
            containers.append(nested)
    quantity: Optional[Decimal] = None
    price: Optional[Decimal] = None
    quote: Optional[Decimal] = None
    fill_prices: List[Decimal] = []
    fills = raw.get("fills")
    if fills is not None:
        if not isinstance(fills, list) or not fills:
            raise MomentumLiveError(f"{source} fills do not prove an execution")
        fill_qty = Decimal("0")
        fill_quote = Decimal("0")
        for index, fill in enumerate(fills):
            if not isinstance(fill, dict):
                raise MomentumLiveError(f"{source} fill {index} is not an object")
            part_qty = _data_decimal(fill.get("qty"), f"{source} fill {index} qty")
            part_price = _data_decimal(
                fill.get("price"), f"{source} fill {index} price"
            )
            if part_qty is None or part_price is None \
                    or part_qty <= 0 or part_price <= 0:
                raise MomentumLiveError(f"{source} fill {index} is not positive")
            fill_prices.append(part_price)
            with localcontext() as context:
                context.prec = 50
                fill_qty += part_qty
                fill_quote += part_qty * part_price
        quantity = fill_qty
        with localcontext() as context:
            context.prec = 50
            price = fill_quote / fill_qty
    for container in containers:
        candidate_qty = _first(
            container, ("filled_qty", "executed_qty", "executedQty", "qty", "quantity")
        )
        candidate_price = _first(
            container, ("entry_price", "avg_fill_price", "fill_price", "avgPrice")
        )
        candidate_quote = _first(container, ("cum_quote", "cumQuote", "quote_qty"))
        if candidate_qty is not None:
            parsed_qty = _data_decimal(candidate_qty, f"{source} fill quantity")
            if quantity is not None and parsed_qty != quantity:
                raise MomentumLiveError(f"{source} fill quantities conflict")
            quantity = parsed_qty
        if candidate_price is not None:
            parsed_price = _data_decimal(candidate_price, f"{source} fill price")
            if price is not None and parsed_price is not None \
                    and not _within_fill_rounding(
                        parsed_price, price, fill_prices
                    ):
                raise MomentumLiveError(f"{source} fill prices conflict")
            price = parsed_price
        if quote is None and candidate_quote is not None:
            quote = _data_decimal(candidate_quote, f"{source} cumulative quote")
    if price is None and quantity is not None and quote is not None and quantity > 0:
        with localcontext() as context:
            context.prec = 50
            price = quote / quantity
    return quantity, price, tuple(fill_prices)


def _open_fill_from_result(
    row: Mapping[str, Any]
) -> Tuple[Decimal, Decimal, Tuple[Decimal, ...]]:
    if row.get("verdict") not in {"OPEN", "OPEN_EXPOSURE"}:
        raise MomentumLiveError("momentum OPEN result does not prove standing exposure")
    quantity, price, fill_prices = _fill_values(row, "runner result")
    if quantity is None or price is None or quantity <= 0 or price <= 0:
        raise MomentumLiveError("momentum OPEN result lacks a positive actual fill")
    return quantity, price, fill_prices


def reconcile_runner_truth(
    state: lifecycle.LifecycleState,
    position: Optional[RunnerPosition],
    results: Iterable[Mapping[str, Any]],
    config: lifecycle.LifecycleConfig,
) -> int:
    """Reconcile OPEN fills and confirmed EXITs into lifecycle state."""
    changes = 0
    result_rows = list(results)
    if position is not None and position.state in {"OPEN", "EXIT_PENDING"}:
        assert position.symbol is not None
        token = state.tokens.get(position.symbol)
        if token is None:
            raise MomentumLiveError("runner position stands without lifecycle state")
        expected_open = token.get("open_job_id")
        if not isinstance(expected_open, str):
            raise MomentumLiveError("lifecycle position has no deterministic OPEN identity")
        if position.open_job_id is not None and expected_open != position.open_job_id:
            raise MomentumLiveError("runner position OPEN identity does not match ignition")
        result = _result_for_job(
            result_rows, expected_open, JOB_OPEN_OP, position.symbol
        )
        if result is None:
            raise MomentumLiveError("runner position stands without an OPEN ledger result")
        quantity, price, fill_prices = _open_fill_from_result(result)
        if position.quantity != quantity:
            raise MomentumLiveError("runner position and OPEN ledger quantity conflict")
        if position.entry_price is not None:
            if not _within_fill_rounding(
                position.entry_price, price, fill_prices
            ):
                raise MomentumLiveError(
                    "runner position and OPEN ledger entry price conflict"
                )
            price = position.entry_price
        if lifecycle.apply_open_fill(
            state,
            position.symbol,
            expected_open,
            expected_open,
            quantity,
            price,
            config,
        ):
            changes += 1

    for symbol, token in sorted(state.tokens.items()):
        qty = _decimal(token.get("qty"), f"state {symbol} qty")
        if qty <= 0:
            continue
        if position is None:
            raise MomentumLiveError("standing lifecycle position has no runner position state")
        if position.state in {"OPEN", "EXIT_PENDING"}:
            if position.symbol != symbol:
                raise MomentumLiveError("runner position symbol conflicts with lifecycle exposure")
            if position.state == "EXIT_PENDING" and position.exit_job_id \
                    != token.get("exit_job_id"):
                raise MomentumLiveError("runner CLOSING identity conflicts with lifecycle EXIT")
            continue
        if position.state in {"UNKNOWN", "OPEN_PENDING"}:
            raise MomentumLiveError("runner position state is not terminally known")
        if token.get("phase") != lifecycle.PHASE_EXIT_PENDING:
            raise MomentumLiveError("runner reports flat before lifecycle requested EXIT")
        exit_job_id = token.get("exit_job_id")
        if not isinstance(exit_job_id, str):
            raise MomentumLiveError("pending lifecycle EXIT has no deterministic job id")
        if position.exit_job_id is not None and position.exit_job_id != exit_job_id:
            raise MomentumLiveError("runner CLOSED identity conflicts with lifecycle EXIT")
        result = _result_for_job(result_rows, exit_job_id, JOB_EXIT_OP, symbol)
        if result is None or result.get("verdict") not in {"CLOSED", "FLAT"}:
            raise MomentumLiveError("runner flat state lacks a terminal EXIT ledger result")
        if lifecycle.confirm_position_flat(
            state, symbol, exit_job_id, exit_job_id
        ):
            changes += 1
    return changes


def append_decision(path: Path, row: Mapping[str, Any]) -> bool:
    """Durably append a decision once by its deterministic decision id."""
    decision_id = row.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id:
        raise MomentumLiveError("decision has no deterministic id")
    path = Path(path)
    index = _decision_index(path)
    existing = index.rows_by_id.get(decision_id)
    if existing is not None:
        if existing != dict(row):
            raise MomentumLiveError(f"decision id conflict: {decision_id}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(dict(row), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    needs_separator = False
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as check:
            check.seek(-1, os.SEEK_END)
            needs_separator = check.read(1) != b"\n"
    if _rotate_decisions(path, len(encoded) + int(needs_separator)):
        index = _decision_index(path)
        needs_separator = False
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    descriptor = os.open(str(path), flags, 0o600)
    try:
        if needs_separator:
            os.write(descriptor, b"\n")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("decision append made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _index_decision(index, json.loads(encoded))
    index.signature = _decision_signature(path)
    return True


def _ignitions(rows: Iterable[Mapping[str, Any]]) -> Dict[str, str]:
    active: Dict[str, str] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not isinstance(symbol, str):
            continue
        snapshot = row.get("lifecycle_state")
        ignition_utc = row.get("ignition_utc")
        if isinstance(ignition_utc, str):
            active[symbol] = ignition_utc
        if isinstance(snapshot, dict) and snapshot.get("position_status") == "FLAT" \
                and snapshot.get("phase") == lifecycle.PHASE_WATCH \
                and snapshot.get("ignition_utc") is None:
            active.pop(symbol, None)
    return active


def _job_from_ledger_row(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    action = row.get("action")
    if action not in {lifecycle.ACTION_ENTER_SMALL, lifecycle.ACTION_EXIT} \
            or row.get("job_eligible") is not True:
        return None
    raw = row.get("job")
    if not isinstance(raw, dict):
        raise MomentumLiveError("eligible momentum decision has no job intent")
    payload = _validate_job_payload(raw)
    symbol = row.get("symbol")
    ignition_utc = row.get("ignition_utc")
    expected_op = (
        JOB_OPEN_OP if action == lifecycle.ACTION_ENTER_SMALL else JOB_EXIT_OP
    )
    if payload["symbol"] != symbol or payload["job_id"] != deterministic_job_id(
        symbol, ignition_utc, expected_op
    ) or payload["op"] != expected_op:
        raise MomentumLiveError("decision job identity is inconsistent")
    return payload


def _recover_jobs(
    rows: Iterable[Mapping[str, Any]],
    jobs_dir: Path,
    job_writer: Callable[[Path, Mapping[str, Any]], bool],
) -> Tuple[str, ...]:
    recovered: List[str] = []
    seen = set()
    for row in rows:
        payload = _job_from_ledger_row(row)
        if payload is None or payload["job_id"] in seen:
            continue
        seen.add(payload["job_id"])
        if job_writer(Path(jobs_dir), payload):
            recovered.append(payload["job_id"])
    return tuple(recovered)


def _bind_ledger_jobs(
    state: lifecycle.LifecycleState, rows: Iterable[Mapping[str, Any]]
) -> None:
    for row in rows:
        payload = _job_from_ledger_row(row)
        if payload is None:
            continue
        lifecycle.bind_job_id(
            state, payload["symbol"], str(row.get("action")), payload["job_id"]
        )


def _action_receipts(receipt_path: Path) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for receipt in lifecycle._read_receipt_lines(Path(receipt_path)):
        if receipt.get("action") not in {
            lifecycle.ACTION_ENTER_SMALL, lifecycle.ACTION_EXIT
        }:
            continue
        receipt_id = receipt.get("receipt_id")
        timestamp = receipt.get("ts")
        if isinstance(receipt_id, str) and isinstance(timestamp, str):
            _parse_utc(timestamp, "action receipt ts")
            result[receipt_id] = receipt
    return result


def _recover_unlogged_entries(
    state: lifecycle.LifecycleState,
    records: Mapping[str, lifecycle.StreamRecord],
    receipt_path: Path,
    decisions_path: Path,
    issued_job_ids: Iterable[str],
    ignition_by_symbol: Dict[str, str],
    maximum: Decimal,
    requested_by: str,
    job_ttl: timedelta,
    now_fn: Callable[[], str],
) -> Tuple[Dict[str, Any], ...]:
    """Recover an OPEN/EXIT receipt created before its decision-ledger row."""
    issued = set(issued_job_ids)
    action_receipts = _action_receipts(receipt_path)
    pending_jobs: List[Dict[str, Any]] = []
    for symbol in sorted(state.tokens):
        token = state.tokens[symbol]
        record = records.get(symbol)
        receipt_id = token.get("last_receipt_id")
        if not isinstance(receipt_id, str):
            continue
        receipt = action_receipts.get(receipt_id)
        if receipt is None:
            continue
        action = receipt.get("action")
        expected_phase = (
            lifecycle.PHASE_ENTER_SMALL
            if action == lifecycle.ACTION_ENTER_SMALL
            else lifecycle.PHASE_EXIT_PENDING
        )
        if action not in {lifecycle.ACTION_ENTER_SMALL, lifecycle.ACTION_EXIT} \
                or token.get("phase") != expected_phase:
            continue
        ignition_utc = token.get("ignition_utc") \
            or ignition_by_symbol.get(symbol)
        if ignition_utc is None:
            continue
        ignition_by_symbol[symbol] = ignition_utc
        op = JOB_OPEN_OP if action == lifecycle.ACTION_ENTER_SMALL else JOB_EXIT_OP
        job_id = deterministic_job_id(symbol, ignition_utc, op)
        if job_id in issued:
            continue
        decision = {
            "action": action,
            "phase": token["phase"],
            "reason": (
                "configured persistence satisfied"
                if action == lifecycle.ACTION_ENTER_SMALL
                else str(receipt.get("exit_reason") or "strategy exit")
            ),
            "receipt_id": receipt_id,
            "add_checks": None,
            "deterioration_checks": None,
        }
        decision_utc = receipt.get("ts")
        if not isinstance(decision_utc, str):
            decision_utc = now_fn()
        row = _decision_row(
            decision,
            token,
            record,
            decision_utc,
            ignition_utc,
            maximum,
            requested_by,
            job_ttl,
        )
        append_decision(decisions_path, row)
        payload = row["job"]
        if payload is not None:
            lifecycle.bind_job_id(state, symbol, action, payload["job_id"])
            issued.add(payload["job_id"])
            pending_jobs.append(payload)
    return tuple(pending_jobs)


def _state_snapshot(token: Mapping[str, Any]) -> Dict[str, Any]:
    names = (
        "phase",
        "position_status",
        "ignition_utc",
        "candidate",
        "persistence_count",
        "qty",
        "paper_notional_requested",
        "adds",
        "reductions",
        "adding_open",
        "peak_price",
        "trail_stop_price",
        "avg_entry_price",
        "entry_price",
        "open_job_id",
        "exit_job_id",
        "fill_result_id",
        "last_stream_utc",
    )
    return {name: token.get(name) for name in names}


def _signal_evidence(
    record: Optional[lifecycle.StreamRecord],
) -> Dict[str, Any]:
    if record is None:
        return {
            "stream_utc": None,
            "observation_id": None,
            "venues": None,
            "surge_ratio": None,
            "price": None,
            "price_venue": None,
            "perp_price": None,
            "vol_1m_quote": None,
            "vol_med_1m": None,
            "oi": None,
            "oi_change_pct": None,
            "basis_gap_pct": None,
            "freshness_ms": None,
        }
    _, _, price, venue = record.quote()
    perp_price: Optional[Decimal] = None
    if record.perp_bid is not None and record.perp_ask is not None:
        with localcontext() as context:
            context.prec = 50
            perp_price = (record.perp_bid + record.perp_ask) / Decimal("2")
    return {
        "stream_utc": record.utc,
        "observation_id": record.observation_id,
        "venues": record.venues,
        "surge_ratio": None
        if record.surge_ratio is None
        else lifecycle.fmt(record.surge_ratio),
        "price": None if price is None else lifecycle.fmt(price),
        "price_venue": venue,
        "perp_price": None if perp_price is None else lifecycle.fmt(perp_price),
        "vol_1m_quote": None
        if record.vol_1m_quote is None
        else lifecycle.fmt(record.vol_1m_quote),
        "vol_med_1m": None
        if record.vol_med_1m is None
        else lifecycle.fmt(record.vol_med_1m),
        "oi": None if record.oi is None else lifecycle.fmt(record.oi),
        "oi_change_pct": None
        if record.oi_change_pct is None
        else lifecycle.fmt(record.oi_change_pct),
        "basis_gap_pct": None
        if record.basis_gap_pct is None
        else lifecycle.fmt(record.basis_gap_pct),
        "freshness_ms": None
        if record.freshness_ms is None
        else lifecycle.fmt(record.freshness_ms),
    }


def _job_eligibility(
    decision: Mapping[str, Any],
    token: Mapping[str, Any],
    record: Optional[lifecycle.StreamRecord],
    max_job_notional: Decimal,
) -> Tuple[bool, str, Optional[Decimal]]:
    action = decision.get("action")
    if action == lifecycle.ACTION_EXIT:
        if RUNNER_SYMBOL_RE.fullmatch(str(token.get("symbol", ""))) is None:
            return False, "symbol does not fit the runner schema", None
        qty = _decimal(token.get("qty"), "position qty")
        if qty <= 0 or token.get("phase") != lifecycle.PHASE_EXIT_PENDING:
            return False, "no standing position is pending EXIT", None
        requested = _decimal(
            token.get("paper_notional_requested"), "paper_notional_requested"
        )
        if requested <= 0:
            requested = max_job_notional
        return True, "standing position requires runner EXIT", min(
            requested, max_job_notional, HARD_MAX_NOTIONAL
        )
    if action != lifecycle.ACTION_ENTER_SMALL:
        return False, "lifecycle action has no runner job", None
    if record is None or record.venues not in {"perp", "both"}:
        return False, "token has no perpetual venue", None
    if record.perp_bid is None or record.perp_ask is None:
        return False, "token has no executable perpetual quote", None
    if RUNNER_SYMBOL_RE.fullmatch(record.symbol) is None:
        return False, "symbol does not fit the runner schema", None
    requested = _decimal(
        token.get("paper_notional_requested"), "paper_notional_requested"
    )
    if requested <= 0:
        return False, "entry notional is not positive", None
    if requested > max_job_notional or requested > HARD_MAX_NOTIONAL:
        return False, "entry notional exceeds the runner cap", requested
    return True, "perpetual entry fits the runner cap", requested


def _decision_id(
    symbol: str,
    action: str,
    observation_id: Optional[str],
    receipt_id: Optional[str],
    decision_utc: str,
) -> str:
    evidence_id = receipt_id or "\0".join((observation_id or "missing", decision_utc))
    digest = hashlib.sha256(
        "\0".join((symbol, action, evidence_id)).encode("utf-8")
    ).hexdigest()[:24]
    return "dec-" + digest


def _decision_row(
    decision: Mapping[str, Any],
    token: Mapping[str, Any],
    record: Optional[lifecycle.StreamRecord],
    decision_utc: str,
    ignition_utc: Optional[str],
    max_job_notional: Decimal,
    requested_by: str,
    job_ttl: timedelta,
) -> Dict[str, Any]:
    evidence = _signal_evidence(record)
    eligible, job_reason, requested = _job_eligibility(
        decision, token, record, max_job_notional
    )
    job: Optional[Dict[str, Any]] = None
    if eligible:
        if ignition_utc is None:
            raise MomentumLiveError("job decision has no proven ignition timestamp")
        op = (
            JOB_OPEN_OP
            if decision.get("action") == lifecycle.ACTION_ENTER_SMALL
            else JOB_EXIT_OP
        )
        created = _parse_utc(decision_utc, "decision_utc")
        job = {
            "job_id": deterministic_job_id(token["symbol"], ignition_utc, op),
            "op": op,
            "symbol": token["symbol"],
            "max_notional": lifecycle.fmt(requested),
            "created_utc": _utc_text(created),
            "expires_utc": _utc_text(created + job_ttl),
            "requested_by": requested_by,
        }
        _validate_job_payload(job)
    row = {
        "decision_id": _decision_id(
            token["symbol"],
            str(decision.get("action", "")),
            evidence["observation_id"],
            decision.get("receipt_id"),
            decision_utc,
        ),
        "decision_utc": decision_utc,
        "stream_utc": evidence["stream_utc"],
        "ignition_utc": ignition_utc,
        "symbol": token["symbol"],
        "action": decision.get("action"),
        "phase": decision.get("phase"),
        "reason": decision.get("reason"),
        "receipt_id": decision.get("receipt_id"),
        "surge_ratio": evidence["surge_ratio"],
        "price": evidence["price"],
        "price_venue": evidence["price_venue"],
        "perp_price": evidence["perp_price"],
        "vol_1m_quote": evidence["vol_1m_quote"],
        "vol_med_1m": evidence["vol_med_1m"],
        "oi": evidence["oi"],
        "oi_change_pct": evidence["oi_change_pct"],
        "basis_gap_pct": evidence["basis_gap_pct"],
        "venues": evidence["venues"],
        "freshness_ms": evidence["freshness_ms"],
        "observation_id": evidence["observation_id"],
        "lifecycle_state": _state_snapshot(token),
        "add_checks": decision.get("add_checks"),
        "deterioration_checks": decision.get("deterioration_checks"),
        "job_eligible": eligible,
        "job_reason": job_reason,
        "job": job,
    }
    return row


def _validate_runtime_config(
    max_job_notional: Decimal, requested_by: str, job_ttl: timedelta
) -> Decimal:
    if not isinstance(max_job_notional, Decimal):
        raise MomentumLiveError("max_job_notional must be a Decimal")
    maximum = _decimal(max_job_notional, "max_job_notional")
    if maximum <= 0 or maximum > HARD_MAX_NOTIONAL:
        raise MomentumLiveError(
            f"max_job_notional must be in (0, {HARD_MAX_NOTIONAL}]"
        )
    if not isinstance(requested_by, str) or not requested_by.strip() \
            or len(requested_by) > 128:
        raise MomentumLiveError("requested_by must be 1-128 visible characters")
    if not isinstance(job_ttl, timedelta) \
            or job_ttl <= timedelta(0) or job_ttl > MAX_JOB_TTL:
        raise MomentumLiveError("job_ttl must be in (0, 15 minutes]")
    return maximum


def run_tick(
    stream_state_path: Path,
    state_path: Path = lifecycle.DEFAULT_STATE_PATH,
    receipt_path: Path = lifecycle.DEFAULT_RECEIPT_PATH,
    decisions_path: Path = DEFAULT_DECISIONS_PATH,
    jobs_dir: Optional[Path] = None,
    position_path: Optional[Path] = None,
    results_path: Optional[Path] = None,
    config: Optional[lifecycle.LifecycleConfig] = None,
    now_fn: Callable[[], str] = lifecycle.utc_now,
    max_job_notional: Decimal = HARD_MAX_NOTIONAL,
    requested_by: str = REQUESTED_BY,
    job_ttl: timedelta = DEFAULT_JOB_TTL,
    job_writer: Optional[
        Callable[[Path, Mapping[str, Any]], bool]
    ] = None,
) -> TickResult:
    """Process one snapshot, reconcile runner truth, and publish job intents."""
    maximum = _validate_runtime_config(
        max_job_notional, requested_by, job_ttl
    )
    resolved_config = config or default_lifecycle_config()
    records = lifecycle.read_stream_state(Path(stream_state_path))
    state = lifecycle.load_state(Path(state_path))
    replayed = lifecycle.reconcile_receipts(state, Path(receipt_path))
    decision_index = _decision_index(Path(decisions_path))
    prior_job_rows = tuple(decision_index.job_rows)
    resolved_jobs_dir = Path(jobs_dir) if jobs_dir is not None else default_jobs_dir()
    resolved_position_path = (
        Path(position_path)
        if position_path is not None
        else default_position_path(resolved_jobs_dir)
    )
    resolved_results_path = (
        Path(results_path)
        if results_path is not None
        else default_results_path(resolved_jobs_dir)
    )
    resolved_job_writer = job_writer or write_job_record

    ignition_by_symbol = dict(decision_index.ignition_by_symbol)
    issued_job_ids = {
        payload["job_id"]
        for payload in (
            _job_from_ledger_row(row) for row in prior_job_rows
        )
        if payload is not None
    }
    unlogged_jobs = _recover_unlogged_entries(
        state,
        records,
        Path(receipt_path),
        Path(decisions_path),
        issued_job_ids,
        ignition_by_symbol,
        maximum,
        requested_by,
        job_ttl,
        now_fn,
    )
    _bind_ledger_jobs(state, prior_job_rows)
    reconcile_runner_truth(
        state,
        read_runner_position(resolved_position_path),
        read_results_ledger(resolved_results_path),
        resolved_config,
    )
    rows: List[Dict[str, Any]] = []
    pending_jobs: List[Dict[str, Any]] = []

    for symbol in sorted(set(state.tokens).union(records)):
        record = records.get(symbol)
        if record is None:
            decision = lifecycle.process_missing_token(
                state, symbol, resolved_config, Path(receipt_path), now_fn
            )
        else:
            decision = lifecycle.process_record(
                state, record, resolved_config, Path(receipt_path), now_fn
            )
        token = state.tokens[symbol]
        ignition_utc = token.get("ignition_utc") or ignition_by_symbol.get(symbol)
        if isinstance(ignition_utc, str):
            ignition_by_symbol[symbol] = ignition_utc

        decision_utc = now_fn()
        row = _decision_row(
            decision,
            token,
            record,
            decision_utc,
            ignition_utc,
            maximum,
            requested_by,
            job_ttl,
        )
        job = row["job"]
        if job is not None:
            lifecycle.bind_job_id(
                state, symbol, str(decision.get("action")), job["job_id"]
            )
            row["lifecycle_state"] = _state_snapshot(token)
        append_decision(Path(decisions_path), row)
        rows.append(row)
        if job is not None:
            pending_jobs.append(job)

        snapshot = row["lifecycle_state"]
        if snapshot.get("position_status") == "FLAT" \
                and snapshot.get("phase") == lifecycle.PHASE_WATCH \
                and snapshot.get("ignition_utc") is None:
            ignition_by_symbol.pop(symbol, None)

    lifecycle.save_state(Path(state_path), state, now_fn)
    recovered = list(
        _recover_jobs(prior_job_rows, resolved_jobs_dir, resolved_job_writer)
    )
    for job in unlogged_jobs:
        if resolved_job_writer(resolved_jobs_dir, job):
            recovered.append(job["job_id"])
    written: List[str] = []
    for job in pending_jobs:
        if resolved_job_writer(resolved_jobs_dir, job):
            written.append(job["job_id"])
    return TickResult(
        decisions=tuple(rows),
        replayed_receipts=replayed,
        recovered_job_ids=tuple(recovered),
        written_job_ids=tuple(written),
    )


def _positive_seconds(raw: str) -> timedelta:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number of seconds") from None
    result = timedelta(seconds=value)
    if result <= timedelta(0) or result > MAX_JOB_TTL:
        raise argparse.ArgumentTypeError("must be between 1 and 900 seconds")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = lifecycle.build_parser()
    parser.description = "route momentum OPEN/HOLD/EXIT decisions to runner jobs"
    parser.set_defaults(initial_notional=DEFAULT_INITIAL_NOTIONAL)
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS_PATH)
    parser.add_argument("--jobs-dir", type=Path, default=None)
    parser.add_argument("--position", type=Path, default=None)
    parser.add_argument("--results", type=Path, default=None)
    parser.add_argument(
        "--max-job-notional",
        type=lifecycle._decimal_argument,
        default=HARD_MAX_NOTIONAL,
    )
    parser.add_argument("--job-ttl-seconds", type=_positive_seconds,
                        default=DEFAULT_JOB_TTL)
    parser.add_argument("--requested-by", default=REQUESTED_BY)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_tick(
            args.tick,
            state_path=args.state,
            receipt_path=args.receipts,
            decisions_path=args.decisions,
            jobs_dir=args.jobs_dir,
            position_path=args.position,
            results_path=args.results,
            config=lifecycle._config_from_args(args),
            max_job_notional=args.max_job_notional,
            requested_by=args.requested_by,
            job_ttl=args.job_ttl_seconds,
        )
    except (MomentumLiveError, lifecycle.MomentumLifecycleError, OSError) as exc:
        print(f"momentum_live_error={exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "decisions": len(result.decisions),
                "jobs_recovered": len(result.recovered_job_ids),
                "jobs_written": len(result.written_job_ids),
                "replayed_receipts": result.replayed_receipts,
            },
            sort_keys=True,
        )
    )
    for decision in result.decisions:
        print(json.dumps(decision, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
