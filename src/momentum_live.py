#!/usr/bin/env python3
"""Route persisted momentum ENTER decisions to the deterministic job runner.

This adapter performs no exchange, credential, or network operation.  It uses
``momentum_lifecycle`` as the only signal/state machine and publishes an exact
``MOMENTUM_ROUNDTRIP`` job record when an ENTER_SMALL decision is both
perpetual-capable and within the compiled runner cap.

The decision ledger is durable before job publication.  Job ids are derived
only from symbol plus the ignition stream timestamp, and publication is an
atomic create-if-absent operation.  A restart in the decision-to-job crash
window therefore recovers the same immutable job instead of issuing another.
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


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DECISIONS_PATH = ROOT / "decisions.jsonl"
HARD_MAX_NOTIONAL = Decimal("8")
DEFAULT_JOB_TTL = timedelta(minutes=10)
MAX_JOB_TTL = timedelta(minutes=15)
REQUESTED_BY = "momentum_live"
JOB_OP = "MOMENTUM_ROUNDTRIP"
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


def default_jobs_dir() -> Path:
    receipt_dir = Path(
        os.path.expanduser(os.environ.get("RECEIPT_DIR", "~/quant_receipts"))
    )
    return receipt_dir / "jobs"


def default_lifecycle_config() -> lifecycle.LifecycleConfig:
    """Use the existing brain with an entry request that fits the runner."""
    return lifecycle.LifecycleConfig(initial_notional=HARD_MAX_NOTIONAL)


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


def deterministic_job_id(symbol: str, ignition_utc: str) -> str:
    """Return the sole runner identity for one token ignition."""
    if not isinstance(symbol, str) or RUNNER_SYMBOL_RE.fullmatch(symbol) is None:
        raise MomentumLiveError("symbol does not fit the runner schema")
    _parse_utc(ignition_utc, "ignition_utc")
    digest = hashlib.sha256(
        (symbol + "\0" + ignition_utc).encode("utf-8")
    ).hexdigest()[:20]
    job_id = f"mom-{symbol.lower()}-{digest}"
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
    if payload["op"] != JOB_OP:
        raise MomentumLiveError("job op is not MOMENTUM_ROUNDTRIP")
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


def _read_decisions(path: Path) -> List[Dict[str, Any]]:
    path = Path(path)
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


def append_decision(path: Path, row: Mapping[str, Any]) -> bool:
    """Durably append a decision once by its deterministic decision id."""
    decision_id = row.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id:
        raise MomentumLiveError("decision has no deterministic id")
    for existing in _read_decisions(Path(path)):
        if existing.get("decision_id") != decision_id:
            continue
        if existing != dict(row):
            raise MomentumLiveError(f"decision id conflict: {decision_id}")
        return False
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_separator = False
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as check:
            check.seek(-1, os.SEEK_END)
            needs_separator = check.read(1) != b"\n"
    encoded = (
        json.dumps(dict(row), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
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
    return True


def _ignitions(rows: Iterable[Mapping[str, Any]]) -> Dict[str, str]:
    active: Dict[str, str] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not isinstance(symbol, str):
            continue
        snapshot = row.get("lifecycle_state")
        if row.get("action") == lifecycle.ACTION_EXIT:
            active.pop(symbol, None)
            continue
        ignition_utc = row.get("ignition_utc")
        if isinstance(ignition_utc, str):
            active[symbol] = ignition_utc
        if isinstance(snapshot, dict) and snapshot.get("qty") == "0" \
                and snapshot.get("candidate") is False:
            active.pop(symbol, None)
    return active


def _job_from_ledger_row(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if row.get("action") != lifecycle.ACTION_ENTER_SMALL \
            or row.get("job_eligible") is not True:
        return None
    raw = row.get("job")
    if not isinstance(raw, dict):
        raise MomentumLiveError("eligible ENTER decision has no job intent")
    payload = _validate_job_payload(raw)
    symbol = row.get("symbol")
    ignition_utc = row.get("ignition_utc")
    if payload["symbol"] != symbol or payload["job_id"] != deterministic_job_id(
        symbol, ignition_utc
    ):
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


def _entry_receipt_times(receipt_path: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for receipt in lifecycle._read_receipt_lines(Path(receipt_path)):
        if receipt.get("action") != lifecycle.ACTION_ENTER_SMALL:
            continue
        receipt_id = receipt.get("receipt_id")
        timestamp = receipt.get("ts")
        if isinstance(receipt_id, str) and isinstance(timestamp, str):
            _parse_utc(timestamp, "entry receipt ts")
            result[receipt_id] = timestamp
    return result


def _recover_unlogged_entries(
    state: lifecycle.LifecycleState,
    records: Mapping[str, lifecycle.StreamRecord],
    receipt_path: Path,
    decisions_path: Path,
    prior_rows: List[Dict[str, Any]],
    ignition_by_symbol: Dict[str, str],
    jobs_dir: Path,
    maximum: Decimal,
    requested_by: str,
    job_ttl: timedelta,
    now_fn: Callable[[], str],
    job_writer: Callable[[Path, Mapping[str, Any]], bool],
) -> Tuple[str, ...]:
    """Recover an ENTER receipt created just before its decision-ledger row."""
    issued = {
        payload["job_id"]
        for payload in (_job_from_ledger_row(row) for row in prior_rows)
        if payload is not None
    }
    receipt_times = _entry_receipt_times(receipt_path)
    recovered: List[str] = []
    for symbol in sorted(state.tokens):
        token = state.tokens[symbol]
        record = records.get(symbol)
        entry = token.get("entry_structure")
        receipt_id = token.get("last_receipt_id")
        is_unlogged_entry_window = (
            token.get("phase") == lifecycle.PHASE_ENTER_SMALL
            and token.get("adds") == 0
            and token.get("reductions") == 0
            and token.get("qty") != "0"
            and isinstance(entry, dict)
            and isinstance(receipt_id, str)
            and record is not None
            and token.get("last_observation_id") == record.observation_id
        )
        if not is_unlogged_entry_window:
            continue
        ignition_utc = ignition_by_symbol.get(symbol)
        if ignition_utc is None and isinstance(entry.get("utc"), str):
            ignition_utc = entry["utc"]
            ignition_by_symbol[symbol] = ignition_utc
        if ignition_utc is None:
            continue
        job_id = deterministic_job_id(symbol, ignition_utc)
        if job_id in issued:
            continue
        decision = {
            "action": lifecycle.ACTION_ENTER_SMALL,
            "phase": token["phase"],
            "reason": "configured persistence satisfied",
            "receipt_id": receipt_id,
            "add_checks": None,
            "deterioration_checks": None,
        }
        decision_utc = receipt_times.get(receipt_id)
        if decision_utc is None:
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
        prior_rows.append(row)
        payload = row["job"]
        if payload is not None:
            issued.add(payload["job_id"])
            if job_writer(jobs_dir, payload):
                recovered.append(payload["job_id"])
    return tuple(recovered)


def _state_snapshot(token: Mapping[str, Any]) -> Dict[str, Any]:
    names = (
        "phase",
        "candidate",
        "persistence_count",
        "qty",
        "paper_notional_requested",
        "adds",
        "reductions",
        "adding_open",
        "peak_price",
        "trail_stop_price",
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


def _entry_eligibility(
    decision: Mapping[str, Any],
    token: Mapping[str, Any],
    record: Optional[lifecycle.StreamRecord],
    max_job_notional: Decimal,
) -> Tuple[bool, str, Optional[Decimal]]:
    if decision.get("action") != lifecycle.ACTION_ENTER_SMALL:
        return False, "lifecycle action is not ENTER_SMALL", None
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
    eligible, job_reason, requested = _entry_eligibility(
        decision, token, record, max_job_notional
    )
    job: Optional[Dict[str, Any]] = None
    if eligible:
        if ignition_utc is None:
            raise MomentumLiveError("ENTER decision has no proven ignition timestamp")
        created = _parse_utc(decision_utc, "decision_utc")
        job = {
            "job_id": deterministic_job_id(token["symbol"], ignition_utc),
            "op": JOB_OP,
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
        raise MomentumLiveError("max_job_notional must be in (0, 8]")
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
    config: Optional[lifecycle.LifecycleConfig] = None,
    now_fn: Callable[[], str] = lifecycle.utc_now,
    max_job_notional: Decimal = HARD_MAX_NOTIONAL,
    requested_by: str = REQUESTED_BY,
    job_ttl: timedelta = DEFAULT_JOB_TTL,
    job_writer: Optional[
        Callable[[Path, Mapping[str, Any]], bool]
    ] = None,
) -> TickResult:
    """Process one frozen stream snapshot and publish eligible ENTER jobs."""
    maximum = _validate_runtime_config(
        max_job_notional, requested_by, job_ttl
    )
    resolved_config = config or default_lifecycle_config()
    records = lifecycle.read_stream_state(Path(stream_state_path))
    state = lifecycle.load_state(Path(state_path))
    replayed = lifecycle.reconcile_receipts(state, Path(receipt_path))
    prior_rows = _read_decisions(Path(decisions_path))
    resolved_jobs_dir = Path(jobs_dir) if jobs_dir is not None else default_jobs_dir()
    resolved_job_writer = job_writer or write_job_record

    ignition_by_symbol = _ignitions(prior_rows)
    recovered = list(
        _recover_jobs(prior_rows, resolved_jobs_dir, resolved_job_writer)
    )
    recovered.extend(
        _recover_unlogged_entries(
            state,
            records,
            Path(receipt_path),
            Path(decisions_path),
            prior_rows,
            ignition_by_symbol,
            resolved_jobs_dir,
            maximum,
            requested_by,
            job_ttl,
            now_fn,
            resolved_job_writer,
        )
    )
    written: List[str] = []
    rows: List[Dict[str, Any]] = []

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
        if decision.get("reason") == "ignition edge observed; persistence started" \
                and record is not None:
            ignition_by_symbol[symbol] = record.utc
        ignition_utc = ignition_by_symbol.get(symbol)
        if decision.get("action") == lifecycle.ACTION_ENTER_SMALL \
                and ignition_utc is None:
            entry = token.get("entry_structure")
            if isinstance(entry, dict) and isinstance(entry.get("utc"), str):
                ignition_utc = entry["utc"]
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
        append_decision(Path(decisions_path), row)
        rows.append(row)
        job = row["job"]
        if job is not None and resolved_job_writer(resolved_jobs_dir, job):
            written.append(job["job_id"])

        snapshot = row["lifecycle_state"]
        if decision.get("action") == lifecycle.ACTION_EXIT \
                or (snapshot.get("qty") == "0" and snapshot.get("candidate") is False):
            ignition_by_symbol.pop(symbol, None)

    lifecycle.save_state(Path(state_path), state, now_fn)
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
    parser.description = "route momentum lifecycle ENTER decisions to runner jobs"
    parser.set_defaults(initial_notional=HARD_MAX_NOTIONAL)
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS_PATH)
    parser.add_argument("--jobs-dir", type=Path, default=None)
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
