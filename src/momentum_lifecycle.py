#!/usr/bin/env python3
"""Restart-safe momentum signal and HOLD lifecycle over ``stream_state.json``.

The input is the frozen shared-stream mapping documented by the Wave 4
contract.  This module performs no network I/O and has no live order path.
It turns each token observation into one explicit lifecycle decision:

* WATCH an ignition edge and require consecutive elevated/price-holding passes.
* Request one small runner OPEN after persistent ignition.
* Adopt quantity and entry price only from a completed runner fill.
* HOLD a standing position against every fresh stream observation.
* Record ADD/REDUCE recommendations as decisions only; they never alter the
  runner-owned position.
* Request EXIT on a trailing stop, invalidated ignition, missing token, or
  stale data, while retaining exposure until runner truth proves it flat.

State is one atomically replaced JSON document.  Strategy actions are appended
and fsynced to a decision receipt ledger before state is saved.  Each receipt
contains the complete per-token post-action state, so ``reconcile_receipts``
can close the append-before-save crash window without inventing an action.
This module has no exchange or network path.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple


STATE_VERSION = 2
DECISION_NOTE = "strategy decision only; runner owns every position mutation"
DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "momentum_state.json"
DEFAULT_RECEIPT_PATH = (
    Path(__file__).resolve().parent.parent / "momentum_decision_receipts.jsonl"
)
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{5,30}")
QTY_QUANTUM = Decimal("0.00000001")

PHASE_WATCH = "WATCH"
PHASE_ENTER_SMALL = "OPEN_PENDING"
PHASE_HOLD = "HOLD"
PHASE_ADD = PHASE_HOLD
PHASE_REDUCE_TRAIL = PHASE_HOLD
PHASE_EXIT_PENDING = "EXIT_PENDING"

ACTION_WATCH = "WATCH"
ACTION_ENTER_SMALL = "ENTER_SMALL"
ACTION_ADD = "ADD"
ACTION_REDUCE_TRAIL = "REDUCE_TRAIL"
ACTION_TRAIL = "TRAIL"
ACTION_HOLD = "HOLD"
ACTION_EXIT = "EXIT"
ACTION_DUPLICATE = "DUPLICATE"

RECEIPT_KINDS = {
    ACTION_ENTER_SMALL: "momentum_open_decision",
    ACTION_ADD: "momentum_add_decision",
    ACTION_REDUCE_TRAIL: "momentum_reduce_decision",
    ACTION_EXIT: "momentum_exit_decision",
}

NUMERIC_FIELDS = (
    "spot_bid",
    "spot_ask",
    "perp_bid",
    "perp_ask",
    "mark",
    "vol_1m_quote",
    "vol_med_1m",
    "surge_ratio",
    "oi",
    "oi_change_pct",
    "funding_rate",
    "next_funding_ms",
    "basis_gap_pct",
    "freshness_ms",
)
REQUIRED_STREAM_FIELDS = frozenset(("utc", "venues") + NUMERIC_FIELDS)


class MomentumLifecycleError(Exception):
    """Expected input/state failure suitable for a concise CLI error."""


class StreamDataError(MomentumLifecycleError):
    """The frozen stream schema or persisted data is malformed."""


class ConfigurationError(MomentumLifecycleError):
    """A lifecycle parameter is unsafe or internally inconsistent."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def fmt(value: Decimal) -> str:
    """Render a finite Decimal without exponent notation."""
    return format(value, "f")


def _decimal_config(value: Any, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ConfigurationError(f"{name} must be a finite Decimal")
    return value


@dataclass(frozen=True)
class LifecycleConfig:
    """All transition and runner-intent sizing thresholds.

    Ratio-growth, trail, fade, spread, and reduce values are fractions (``.05``
    means five percent).  ``oi_change_pct`` and ``basis_gap_pct`` thresholds
    use the percent units supplied verbatim by the shared stream.
    """

    ignition_surge_ratio: Decimal = Decimal("10")
    persistence_surge_ratio: Decimal = Decimal("8")
    persistence_passes: int = 2
    persistence_price_hold_pct: Decimal = Decimal("0")
    max_freshness_ms: Decimal = Decimal("5000")

    initial_notional: Decimal = Decimal("10")
    add_notional: Decimal = Decimal("5")
    max_total_notional: Decimal = Decimal("30")
    max_adds: int = 4
    qty_quantum: Decimal = QTY_QUANTUM

    min_volume_growth_pct: Decimal = Decimal("0.05")
    min_surge_growth_pct: Decimal = Decimal("0.02")
    min_price_growth_pct: Decimal = Decimal("0.002")
    max_spread_pct: Decimal = Decimal("0.01")
    min_depth_improvement_pct: Decimal = Decimal("0")
    min_oi_growth_pct: Decimal = Decimal("0.001")
    min_oi_change_pct: Decimal = Decimal("0")

    trail_pct: Decimal = Decimal("0.15")
    soft_price_fade_pct: Decimal = Decimal("0.08")
    volume_fade_pct: Decimal = Decimal("0.30")
    oi_fade_pct: Decimal = Decimal("0.03")
    oi_change_fade_pct: Decimal = Decimal("-2")
    depth_worsening_pct: Decimal = Decimal("0.25")
    reduce_fraction: Decimal = Decimal("0.25")
    ignition_invalidation_surge_ratio: Decimal = Decimal("1")

    basis_material_pct: Decimal = Decimal("0.50")

    def __post_init__(self) -> None:
        decimal_names = [
            item.name for item in fields(self) if item.name not in {"persistence_passes", "max_adds"}
        ]
        values = {name: _decimal_config(getattr(self, name), name) for name in decimal_names}
        if (
            isinstance(self.persistence_passes, bool)
            or not isinstance(self.persistence_passes, int)
            or self.persistence_passes < 1
        ):
            raise ConfigurationError("persistence_passes must be a positive integer")
        if (
            isinstance(self.max_adds, bool)
            or not isinstance(self.max_adds, int)
            or self.max_adds < 0
        ):
            raise ConfigurationError("max_adds must be a non-negative integer")
        positive = (
            "ignition_surge_ratio",
            "persistence_surge_ratio",
            "max_freshness_ms",
            "initial_notional",
            "add_notional",
            "max_total_notional",
            "qty_quantum",
            "max_spread_pct",
            "trail_pct",
            "volume_fade_pct",
            "oi_fade_pct",
            "reduce_fraction",
            "basis_material_pct",
        )
        if any(values[name] <= 0 for name in positive):
            raise ConfigurationError("positive lifecycle thresholds must be above zero")
        fractional = (
            "persistence_price_hold_pct",
            "min_volume_growth_pct",
            "min_surge_growth_pct",
            "min_price_growth_pct",
            "max_spread_pct",
            "min_depth_improvement_pct",
            "min_oi_growth_pct",
            "trail_pct",
            "soft_price_fade_pct",
            "volume_fade_pct",
            "oi_fade_pct",
            "depth_worsening_pct",
            "reduce_fraction",
        )
        if any(values[name] < 0 or values[name] >= 1 for name in fractional):
            raise ConfigurationError("fractional lifecycle thresholds must be in [0, 1)")
        if values["max_total_notional"] < values["initial_notional"]:
            raise ConfigurationError("max_total_notional cannot be below initial_notional")
        if values["ignition_invalidation_surge_ratio"] < 0:
            raise ConfigurationError("ignition invalidation ratio cannot be negative")


def _parse_utc(raw: Any, source: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise StreamDataError(f"{source} has invalid utc")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise StreamDataError(f"{source} has invalid utc") from None
    if value.tzinfo is None:
        raise StreamDataError(f"{source} utc must include a timezone")
    return value.astimezone(timezone.utc)


def _stream_decimal(raw: Any, field_name: str, symbol: str) -> Optional[Decimal]:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise StreamDataError(f"stream {symbol} {field_name} must be a Decimal string or null")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise StreamDataError(f"stream {symbol} has invalid {field_name}") from None
    if not value.is_finite():
        raise StreamDataError(f"stream {symbol} has non-finite {field_name}")
    return value


@dataclass(frozen=True)
class StreamRecord:
    symbol: str
    utc: str
    utc_value: datetime
    venues: str
    spot_bid: Optional[Decimal]
    spot_ask: Optional[Decimal]
    perp_bid: Optional[Decimal]
    perp_ask: Optional[Decimal]
    mark: Optional[Decimal]
    vol_1m_quote: Optional[Decimal]
    vol_med_1m: Optional[Decimal]
    surge_ratio: Optional[Decimal]
    oi: Optional[Decimal]
    oi_change_pct: Optional[Decimal]
    funding_rate: Optional[Decimal]
    next_funding_ms: Optional[Decimal]
    basis_gap_pct: Optional[Decimal]
    freshness_ms: Optional[Decimal]
    observation_id: str

    @classmethod
    def from_payload(cls, symbol: str, payload: Any) -> "StreamRecord":
        if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
            raise StreamDataError(f"invalid stream symbol {symbol!r}")
        if not isinstance(payload, dict):
            raise StreamDataError(f"stream {symbol} record is not an object")
        missing = sorted(REQUIRED_STREAM_FIELDS.difference(payload))
        if missing:
            raise StreamDataError(f"stream {symbol} missing fields: {', '.join(missing)}")
        venues = payload.get("venues")
        if venues not in {"spot", "perp", "both"}:
            raise StreamDataError(f"stream {symbol} has invalid venues")
        utc_text = payload.get("utc")
        utc_value = _parse_utc(utc_text, f"stream {symbol}")
        numbers = {
            name: _stream_decimal(payload.get(name), name, symbol)
            for name in NUMERIC_FIELDS
        }
        positive_fields = ("spot_bid", "spot_ask", "perp_bid", "perp_ask", "mark")
        for name in positive_fields:
            value = numbers[name]
            if value is not None and value <= 0:
                raise StreamDataError(f"stream {symbol} has non-positive {name}")
        nonnegative_fields = (
            "vol_1m_quote",
            "vol_med_1m",
            "surge_ratio",
            "oi",
            "next_funding_ms",
            "freshness_ms",
        )
        for name in nonnegative_fields:
            value = numbers[name]
            if value is not None and value < 0:
                raise StreamDataError(f"stream {symbol} has negative {name}")
        for bid_name, ask_name in (("spot_bid", "spot_ask"), ("perp_bid", "perp_ask")):
            bid, ask = numbers[bid_name], numbers[ask_name]
            if bid is not None and ask is not None and bid > ask:
                raise StreamDataError(f"stream {symbol} has {bid_name} above {ask_name}")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        observation_id = hashlib.sha256((symbol + "\0" + canonical).encode("utf-8")).hexdigest()
        return cls(
            symbol=symbol,
            utc=utc_text,
            utc_value=utc_value,
            venues=venues,
            observation_id=observation_id,
            **numbers,
        )

    def quote(self) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal], str]:
        """Return bid, ask, midpoint, and the deterministic momentum venue."""
        pairs: List[Tuple[Optional[Decimal], Optional[Decimal], str]] = []
        if self.venues in {"spot", "both"}:
            pairs.append((self.spot_bid, self.spot_ask, "spot"))
        if self.venues in {"perp", "both"}:
            pairs.append((self.perp_bid, self.perp_ask, "perp"))
        for bid, ask, venue in pairs:
            if bid is not None and ask is not None:
                with localcontext() as context:
                    context.prec = 50
                    return bid, ask, (bid + ask) / Decimal("2"), venue
        if self.mark is not None:
            # Mark is useful structure evidence, but it is not an executable
            # bid/ask and must never manufacture executable depth or a fill.
            return None, None, self.mark, "mark"
        return None, None, None, "unknown"

    def spread_pct(self) -> Optional[Decimal]:
        bid, ask, midpoint, _ = self.quote()
        if bid is None or ask is None or midpoint is None:
            return None
        with localcontext() as context:
            context.prec = 50
            return (ask - bid) / midpoint

    def is_fresh(self, config: LifecycleConfig) -> bool:
        return self.freshness_ms is not None and self.freshness_ms <= config.max_freshness_ms


@dataclass
class LifecycleState:
    tokens: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def _zero_token(symbol: str) -> Dict[str, Any]:
    return {
        "runner_truth": True,
        "symbol": symbol,
        "phase": PHASE_WATCH,
        "candidate": False,
        "persistence_count": 0,
        "pre_ignition_price": None,
        "ignition_utc": None,
        "entry_structure": None,
        "qty": "0",
        "paper_cost": "0",
        "paper_notional_requested": "0",
        "avg_entry_price": None,
        "entry_price": None,
        "peak_price": None,
        "trail_stop_price": None,
        "adds": 0,
        "reductions": 0,
        "adding_open": False,
        "realized_pnl_quote": "0",
        "last_price": None,
        "last_bid": None,
        "last_ask": None,
        "last_vol_1m_quote": None,
        "last_surge_ratio": None,
        "last_oi": None,
        "last_oi_change_pct": None,
        "last_spread_pct": None,
        "last_stream_utc": None,
        "last_observation_id": None,
        "basis_available": False,
        "basis_gap_pct": None,
        "basis_note": "unavailable: no stream pass observed",
        "action_seq": 0,
        "last_receipt_id": None,
        "open_job_id": None,
        "exit_job_id": None,
        "fill_result_id": None,
        "close_result_id": None,
        "position_status": "FLAT",
        "rearm_required": False,
    }


def _state_decimal(token: Mapping[str, Any], name: str, symbol: str, *, positive: bool = False) -> Decimal:
    raw = token.get(name)
    if not isinstance(raw, str):
        raise StreamDataError(f"state {symbol} has invalid {name}")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise StreamDataError(f"state {symbol} has invalid {name}") from None
    if not value.is_finite() or value < 0 or (positive and value <= 0):
        raise StreamDataError(f"state {symbol} has invalid {name}")
    return value


def _validate_token(symbol: str, raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise StreamDataError(f"state token {symbol} is not an object")
    if raw.get("runner_truth") is not True or raw.get("symbol") != symbol:
        raise StreamDataError(f"state token {symbol} is not runner-backed or mismatches symbol")
    if raw.get("phase") not in {
        PHASE_WATCH, PHASE_ENTER_SMALL, PHASE_HOLD, PHASE_EXIT_PENDING
    }:
        raise StreamDataError(f"state token {symbol} has invalid phase")
    if not isinstance(raw.get("candidate"), bool) or not isinstance(raw.get("adding_open"), bool):
        raise StreamDataError(f"state token {symbol} has invalid lifecycle flags")
    if not isinstance(raw.get("rearm_required"), bool):
        raise StreamDataError(f"state token {symbol} has invalid rearm flag")
    if raw.get("position_status") not in {"FLAT", "OPEN", "EXIT_PENDING"}:
        raise StreamDataError(f"state token {symbol} has invalid position status")
    for name in (
        "ignition_utc", "open_job_id", "exit_job_id", "fill_result_id",
        "close_result_id",
    ):
        value = raw.get(name)
        if value is not None and (not isinstance(value, str) or not value):
            raise StreamDataError(f"state token {symbol} has invalid {name}")
    for name in ("persistence_count", "adds", "reductions", "action_seq"):
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StreamDataError(f"state token {symbol} has invalid {name}")
    qty = _state_decimal(raw, "qty", symbol)
    cost = _state_decimal(raw, "paper_cost", symbol)
    _state_decimal(raw, "paper_notional_requested", symbol)
    realized_raw = raw.get("realized_pnl_quote")
    if not isinstance(realized_raw, str):
        raise StreamDataError(f"state token {symbol} has invalid realized_pnl_quote")
    try:
        realized = Decimal(realized_raw)
    except InvalidOperation:
        raise StreamDataError(f"state token {symbol} has invalid realized_pnl_quote") from None
    if not realized.is_finite():
        raise StreamDataError(f"state token {symbol} has invalid realized_pnl_quote")
    if (qty == 0) != (cost == 0):
        raise StreamDataError(f"state token {symbol} has inconsistent qty and paper_cost")
    if qty > 0:
        if raw.get("phase") not in {PHASE_HOLD, PHASE_EXIT_PENDING} \
                or raw.get("position_status") not in {"OPEN", "EXIT_PENDING"} \
                or not isinstance(raw.get("entry_structure"), dict):
            raise StreamDataError(f"state token {symbol} has open sizing without entry structure")
        for name in ("avg_entry_price", "entry_price", "peak_price"):
            value = raw.get(name)
            if not isinstance(value, str):
                raise StreamDataError(f"state token {symbol} has invalid {name}")
            try:
                parsed = Decimal(value)
            except InvalidOperation:
                raise StreamDataError(f"state token {symbol} has invalid {name}") from None
            if not parsed.is_finite() or parsed <= 0:
                raise StreamDataError(f"state token {symbol} has invalid {name}")
        if raw.get("entry_price") != raw.get("avg_entry_price"):
            raise StreamDataError(f"state token {symbol} has conflicting entry price")
    elif raw.get("position_status") != "FLAT" or raw.get("phase") == PHASE_HOLD:
        raise StreamDataError(f"state token {symbol} has position status without quantity")
    return dict(raw)


def load_state(path: Path) -> LifecycleState:
    path = Path(path)
    if not path.exists():
        return LifecycleState()
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise StreamDataError(f"momentum state unreadable: {path}") from None
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise StreamDataError("momentum state has unsupported version")
    if payload.get("runner_truth") is not True or not isinstance(payload.get("tokens"), dict):
        raise StreamDataError("momentum state is not a runner-backed token mapping")
    tokens: Dict[str, Dict[str, Any]] = {}
    for symbol, token in payload["tokens"].items():
        if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
            raise StreamDataError("momentum state has invalid symbol")
        tokens[symbol] = _validate_token(symbol, token)
    return LifecycleState(tokens=tokens)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def save_state(path: Path, state: LifecycleState, now_fn: Callable[[], str] = utc_now) -> None:
    """Atomically persist the complete per-token runner-backed state."""
    path = Path(path)
    for symbol, token in state.tokens.items():
        _validate_token(symbol, token)
    payload = {
        "version": STATE_VERSION,
        "runner_truth": True,
        "updated_utc": now_fn(),
        "tokens": state.tokens,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=1)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _read_receipt_lines(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise StreamDataError(f"momentum receipts unreadable: {path}") from None
    records: List[Dict[str, Any]] = []
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a killed append may leave a torn line
        if isinstance(record, dict):
            records.append(record)
    return records


def append_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    """Durably append one complete strategy decision before any state save."""
    if receipt.get("decision_only") is not True:
        raise StreamDataError("refusing to append a non-decision momentum receipt")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(dict(receipt), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    needs_separator = False
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as check:
            check.seek(-1, os.SEEK_END)
            needs_separator = check.read(1) != b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    descriptor = os.open(str(path), flags, 0o600)
    try:
        # Start a valid record on a new line even if a previous killed append
        # left a torn tail.  The torn line is ignored during reconciliation.
        if needs_separator:
            os.write(descriptor, b"\n")
        written = 0
        while written < len(encoded):
            count = os.write(descriptor, encoded[written:])
            if count <= 0:
                raise OSError("short receipt append")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def reconcile_receipts(state: LifecycleState, receipt_path: Path) -> int:
    """Replay complete sizing receipts beyond each token's persisted sequence."""
    replayed = 0
    for receipt in _read_receipt_lines(Path(receipt_path)):
        kind = receipt.get("kind")
        if kind not in set(RECEIPT_KINDS.values()):
            continue
        if receipt.get("decision_only") is not True:
            raise StreamDataError("momentum receipt is not marked decision-only")
        symbol = receipt.get("symbol")
        sequence = receipt.get("action_seq")
        receipt_id = receipt.get("receipt_id")
        snapshot = receipt.get("state_after")
        if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
            raise StreamDataError("momentum receipt has invalid symbol")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise StreamDataError(f"momentum receipt for {symbol} has invalid sequence")
        expected_id = f"{symbol}:{sequence}"
        if receipt_id != expected_id:
            raise StreamDataError(f"momentum receipt for {symbol} has invalid id")
        validated = _validate_token(symbol, snapshot)
        if validated.get("action_seq") != sequence or validated.get("last_receipt_id") != receipt_id:
            raise StreamDataError(f"momentum receipt for {symbol} has inconsistent snapshot")
        current = state.tokens.get(symbol)
        current_sequence = 0 if current is None else current.get("action_seq")
        if not isinstance(current_sequence, int):
            raise StreamDataError(f"momentum state for {symbol} has invalid sequence")
        if sequence <= current_sequence:
            if sequence == current_sequence and current is not None and current.get("last_receipt_id") != receipt_id:
                raise StreamDataError(f"momentum receipt sequence conflicts for {symbol}")
            continue
        if sequence != current_sequence + 1:
            raise StreamDataError(f"momentum receipt sequence gap for {symbol}")
        state.tokens[symbol] = validated
        replayed += 1
    return replayed


def quantize_down(value: Decimal, quantum: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    with localcontext() as context:
        context.prec = 50
        return (value / quantum).to_integral_value(rounding=ROUND_DOWN) * quantum


def paper_quantity(notional: Decimal, price: Decimal, quantum: Decimal = QTY_QUANTUM) -> Decimal:
    if notional <= 0 or price <= 0 or quantum <= 0:
        return Decimal("0")
    with localcontext() as context:
        context.prec = 50
        return quantize_down(notional / price, quantum)


def trail_stop_price(peak_price: Decimal, trail_pct: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return peak_price * (Decimal("1") - trail_pct)


def basis_availability(record: StreamRecord, config: LifecycleConfig) -> Tuple[bool, str]:
    """Factual hedged-basis availability note; never changes momentum routing."""
    if record.venues != "both":
        return False, f"unavailable: venues={record.venues} is not both"
    if not record.is_fresh(config):
        freshness = "unknown" if record.freshness_ms is None else fmt(record.freshness_ms)
        return False, f"unavailable: freshness_ms={freshness} exceeds max={fmt(config.max_freshness_ms)}"
    if record.basis_gap_pct is None:
        return False, "unavailable: basis_gap_pct unknown"
    magnitude = abs(record.basis_gap_pct)
    if magnitude < config.basis_material_pct:
        return False, (
            f"unavailable: abs_basis_gap_pct={fmt(magnitude)} below "
            f"material_pct={fmt(config.basis_material_pct)}"
        )
    return True, (
        f"available: abs_basis_gap_pct={fmt(magnitude)} meets "
        f"material_pct={fmt(config.basis_material_pct)}"
    )


def add_strength_checks(token: Mapping[str, Any], record: StreamRecord, config: LifecycleConfig) -> Dict[str, bool]:
    """Four independent, fail-closed ADD gates over consecutive passes."""
    _, _, price, _ = record.quote()
    spread = record.spread_pct()
    previous_price = _optional_decimal(token.get("last_price"))
    previous_volume = _optional_decimal(token.get("last_vol_1m_quote"))
    previous_surge = _optional_decimal(token.get("last_surge_ratio"))
    previous_spread = _optional_decimal(token.get("last_spread_pct"))
    previous_oi = _optional_decimal(token.get("last_oi"))
    peak = _optional_decimal(token.get("peak_price"))

    volume = all(value is not None for value in (record.vol_1m_quote, record.surge_ratio, previous_volume, previous_surge))
    if volume:
        volume = bool(
            record.vol_1m_quote >= previous_volume * (Decimal("1") + config.min_volume_growth_pct)
            and record.surge_ratio >= previous_surge * (Decimal("1") + config.min_surge_growth_pct)
        )
    price_structure = all(value is not None for value in (price, previous_price, peak))
    if price_structure:
        price_structure = bool(
            price >= previous_price * (Decimal("1") + config.min_price_growth_pct)
            and price >= peak
        )
    depth = spread is not None and previous_spread is not None
    if depth:
        depth = bool(
            spread <= config.max_spread_pct
            and spread <= previous_spread * (Decimal("1") - config.min_depth_improvement_pct)
        )
    participation = all(value is not None for value in (record.oi, record.oi_change_pct, previous_oi))
    if participation:
        participation = bool(
            record.oi >= previous_oi * (Decimal("1") + config.min_oi_growth_pct)
            and record.oi_change_pct >= config.min_oi_change_pct
        )
    within_ladder = bool(
        token.get("adding_open") is True
        and token.get("adds", 0) < config.max_adds
        and _state_decimal(token, "paper_notional_requested", record.symbol) < config.max_total_notional
    )
    return {
        "volume": bool(volume),
        "price_structure": bool(price_structure),
        "depth": bool(depth),
        "participation": bool(participation),
        "within_ladder": within_ladder,
    }


def deterioration_checks(token: Mapping[str, Any], record: StreamRecord, config: LifecycleConfig) -> Dict[str, bool]:
    """Soft-deterioration gates for decision-only reduction and trailing."""
    _, _, price, _ = record.quote()
    spread = record.spread_pct()
    previous_volume = _optional_decimal(token.get("last_vol_1m_quote"))
    previous_surge = _optional_decimal(token.get("last_surge_ratio"))
    previous_oi = _optional_decimal(token.get("last_oi"))
    previous_spread = _optional_decimal(token.get("last_spread_pct"))
    peak = _optional_decimal(token.get("peak_price"))

    volume_fade = False
    if record.vol_1m_quote is not None and previous_volume is not None:
        volume_fade = record.vol_1m_quote <= previous_volume * (Decimal("1") - config.volume_fade_pct)
    if record.surge_ratio is not None and previous_surge is not None:
        volume_fade = volume_fade or record.surge_ratio <= previous_surge * (Decimal("1") - config.volume_fade_pct)
    oi_fade = False
    if record.oi is not None and previous_oi is not None:
        oi_fade = record.oi <= previous_oi * (Decimal("1") - config.oi_fade_pct)
    if record.oi_change_pct is not None:
        oi_fade = oi_fade or record.oi_change_pct <= config.oi_change_fade_pct
    price_fade = bool(price is not None and peak is not None and price <= peak * (Decimal("1") - config.soft_price_fade_pct))
    depth_fade = bool(
        spread is not None
        and (
            spread > config.max_spread_pct
            or (
                previous_spread is not None
                and spread >= previous_spread * (Decimal("1") + config.depth_worsening_pct)
            )
        )
    )
    return {
        "volume_fade": volume_fade,
        "oi_fade": oi_fade,
        "price_fade": price_fade,
        "depth_fade": depth_fade,
    }


def _optional_decimal(raw: Any) -> Optional[Decimal]:
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def _commit_observation(token: Dict[str, Any], record: StreamRecord, basis: Tuple[bool, str]) -> None:
    bid, ask, price, _ = record.quote()
    spread = record.spread_pct()
    token.update(
        {
            "last_price": None if price is None else fmt(price),
            "last_bid": None if bid is None else fmt(bid),
            "last_ask": None if ask is None else fmt(ask),
            "last_vol_1m_quote": None if record.vol_1m_quote is None else fmt(record.vol_1m_quote),
            "last_surge_ratio": None if record.surge_ratio is None else fmt(record.surge_ratio),
            "last_oi": None if record.oi is None else fmt(record.oi),
            "last_oi_change_pct": None if record.oi_change_pct is None else fmt(record.oi_change_pct),
            "last_spread_pct": None if spread is None else fmt(spread),
            "last_stream_utc": record.utc,
            "last_observation_id": record.observation_id,
            "basis_available": basis[0],
            "basis_gap_pct": None if record.basis_gap_pct is None else fmt(record.basis_gap_pct),
            "basis_note": basis[1],
        }
    )


def _decision(token: Mapping[str, Any], action: str, reason: str, *, add_checks: Optional[Dict[str, bool]] = None, fade_checks: Optional[Dict[str, bool]] = None, receipt_id: Optional[str] = None) -> Dict[str, Any]:
    return {
        "runner_truth": True,
        "decision_only": action in {ACTION_ADD, ACTION_REDUCE_TRAIL},
        "symbol": token["symbol"],
        "action": action,
        "phase": token["phase"],
        "reason": reason,
        "qty": token["qty"],
        "paper_cost": token["paper_cost"],
        "position_cost": token["paper_cost"],
        "entry_price": token["entry_price"],
        "peak_price": token["peak_price"],
        "trail_stop_price": token["trail_stop_price"],
        "basis_available": token["basis_available"],
        "basis_gap_pct": token["basis_gap_pct"],
        "basis_note": token["basis_note"],
        "trade_router_note": "factual alternative only; no automatic switch",
        "add_checks": add_checks,
        "deterioration_checks": fade_checks,
        "receipt_id": receipt_id,
    }


def _record_action(token: Dict[str, Any], action: str, receipt_path: Path, now_fn: Callable[[], str], details: Mapping[str, Any]) -> str:
    token["action_seq"] += 1
    receipt_id = f"{token['symbol']}:{token['action_seq']}"
    token["last_receipt_id"] = receipt_id
    receipt = {
        "runner_truth": True,
        "decision_only": True,
        "kind": RECEIPT_KINDS[action],
        "action": action,
        "action_seq": token["action_seq"],
        "receipt_id": receipt_id,
        "ts": now_fn(),
        "symbol": token["symbol"],
        "note": DECISION_NOTE,
        **dict(details),
        "state_after": copy.deepcopy(token),
    }
    append_receipt(Path(receipt_path), receipt)
    return receipt_id


def _reset_watch(token: Dict[str, Any], *, rearm_required: bool) -> None:
    token.update(
        {
            "phase": PHASE_WATCH,
            "candidate": False,
            "persistence_count": 0,
            "pre_ignition_price": None,
            "ignition_utc": None,
            "entry_structure": None,
            "qty": "0",
            "paper_cost": "0",
            "paper_notional_requested": "0",
            "avg_entry_price": None,
            "entry_price": None,
            "peak_price": None,
            "trail_stop_price": None,
            "adds": 0,
            "reductions": 0,
            "adding_open": False,
            "realized_pnl_quote": "0",
            "open_job_id": None,
            "exit_job_id": None,
            "fill_result_id": None,
            "close_result_id": None,
            "position_status": "FLAT",
            "rearm_required": rearm_required,
        }
    )


def _enter_small(token: Dict[str, Any], record: StreamRecord, config: LifecycleConfig, receipt_path: Path, now_fn: Callable[[], str]) -> Tuple[bool, Optional[str]]:
    bid, ask, price, venue = record.quote()
    if ask is None or bid is None or price is None:
        return False, None
    suggested_qty = paper_quantity(config.initial_notional, ask, config.qty_quantum)
    if suggested_qty <= 0:
        return False, None
    token.update(
        {
            "phase": PHASE_ENTER_SMALL,
            "candidate": False,
            "persistence_count": 0,
            "entry_structure": {
                "utc": record.utc,
                "venue": venue,
                "price": None,
                "signal_price": fmt(ask),
                "structure_price": fmt(price),
                "surge_ratio": None if record.surge_ratio is None else fmt(record.surge_ratio),
                "oi": None if record.oi is None else fmt(record.oi),
                "vol_1m_quote": None if record.vol_1m_quote is None else fmt(record.vol_1m_quote),
                "spread_pct": None if record.spread_pct() is None else fmt(record.spread_pct()),
            },
            # The signal cannot manufacture a position.  These stay flat until
            # ``apply_open_fill`` consumes runner result-ledger truth.
            "qty": "0",
            "paper_cost": "0",
            "paper_notional_requested": fmt(config.initial_notional),
            "avg_entry_price": None,
            "entry_price": None,
            "peak_price": None,
            "trail_stop_price": None,
            "adds": 0,
            "reductions": 0,
            "adding_open": True,
            "realized_pnl_quote": "0",
            "open_job_id": None,
            "exit_job_id": None,
            "fill_result_id": None,
            "close_result_id": None,
            "position_status": "FLAT",
            "rearm_required": False,
        }
    )
    _commit_observation(token, record, basis_availability(record, config))
    receipt_id = _record_action(
        token,
        ACTION_ENTER_SMALL,
        receipt_path,
        now_fn,
        {
            "stream_utc": record.utc,
            "side": "BUY",
            "signal_price": fmt(ask),
            "suggested_qty": fmt(suggested_qty),
            "paper_notional_requested": fmt(config.initial_notional),
            "entry_structure": copy.deepcopy(token["entry_structure"]),
        },
    )
    return True, receipt_id


def _add(token: Dict[str, Any], record: StreamRecord, config: LifecycleConfig, receipt_path: Path, now_fn: Callable[[], str]) -> Optional[str]:
    bid, ask, price, _ = record.quote()
    if bid is None or ask is None or price is None:
        return None
    committed = _state_decimal(token, "paper_notional_requested", record.symbol)
    available = max(Decimal("0"), config.max_total_notional - committed)
    requested = min(config.add_notional, available)
    suggested_qty = paper_quantity(requested, ask, config.qty_quantum)
    if requested <= 0 or suggested_qty <= 0:
        token["adding_open"] = False
        return None
    token.update(
        {
            "phase": PHASE_HOLD,
            "paper_notional_requested": fmt(committed + requested),
            "adds": token["adds"] + 1,
        }
    )
    _commit_observation(token, record, basis_availability(record, config))
    return _record_action(
        token,
        ACTION_ADD,
        receipt_path,
        now_fn,
        {
            "stream_utc": record.utc,
            "execution": "DECISION_ONLY",
            "side": "BUY_RECOMMENDATION",
            "reference_price": fmt(ask),
            "suggested_qty": fmt(suggested_qty),
            "position_qty_unchanged": token["qty"],
            "paper_notional_requested": fmt(requested),
        },
    )


def _reduce(token: Dict[str, Any], record: StreamRecord, config: LifecycleConfig, receipt_path: Path, now_fn: Callable[[], str], reason: str) -> Optional[str]:
    bid, _, _, _ = record.quote()
    if bid is None:
        bid = _optional_decimal(token.get("last_bid")) or _optional_decimal(token.get("last_price"))
    if bid is None:
        return None
    old_qty = _state_decimal(token, "qty", record.symbol, positive=True)
    suggested_qty = quantize_down(old_qty * config.reduce_fraction, config.qty_quantum)
    if suggested_qty <= 0 or suggested_qty >= old_qty:
        return None
    token.update(
        {
            "phase": PHASE_HOLD,
            "reductions": token["reductions"] + 1,
            "adding_open": False,
        }
    )
    _commit_observation(token, record, basis_availability(record, config))
    return _record_action(
        token,
        ACTION_REDUCE_TRAIL,
        receipt_path,
        now_fn,
        {
            "stream_utc": record.utc,
            "execution": "DECISION_ONLY",
            "side": "SELL_RECOMMENDATION",
            "reason": reason,
            "reference_price": fmt(bid),
            "suggested_qty": fmt(suggested_qty),
            "position_qty_unchanged": token["qty"],
        },
    )


def _exit(token: Dict[str, Any], record: Optional[StreamRecord], config: LifecycleConfig, receipt_path: Path, now_fn: Callable[[], str], reason: str, basis: Tuple[bool, str]) -> str:
    symbol = token["symbol"]
    qty = _state_decimal(token, "qty", symbol, positive=True)
    bid: Optional[Decimal] = None
    stream_utc: Optional[str] = None
    if record is not None:
        bid, _, _, _ = record.quote()
        stream_utc = record.utc
    if bid is None:
        bid = _optional_decimal(token.get("last_bid")) or _optional_decimal(token.get("last_price"))
    peak = token.get("peak_price")
    stop = token.get("trail_stop_price")
    entry = copy.deepcopy(token.get("entry_structure"))
    token["phase"] = PHASE_EXIT_PENDING
    token["position_status"] = "EXIT_PENDING"
    token["adding_open"] = False
    if record is not None:
        _commit_observation(token, record, basis)
    else:
        token.update(
            {
                "basis_available": False,
                "basis_gap_pct": None,
                "basis_note": basis[1],
                "last_observation_id": None,
            }
        )
    return _record_action(
        token,
        ACTION_EXIT,
        receipt_path,
        now_fn,
        {
            "stream_utc": stream_utc,
            "side": "SELL_EXIT_INTENT",
            "exit_reason": reason,
            "reference_price": None if bid is None else fmt(bid),
            "qty": fmt(qty),
            "peak_price": peak,
            "trail_stop_price": stop,
            "entry_structure": entry,
            "adds": token["adds"],
            "reductions": token["reductions"],
        },
    )


def bind_job_id(
    state: LifecycleState, symbol: str, action: str, job_id: str
) -> None:
    """Bind a deterministic runner identity without changing exposure."""
    token = state.tokens.get(symbol)
    if token is None:
        raise StreamDataError(f"runner job for unknown momentum token {symbol}")
    name = "open_job_id" if action == ACTION_ENTER_SMALL else "exit_job_id"
    if action not in {ACTION_ENTER_SMALL, ACTION_EXIT}:
        raise StreamDataError(f"action {action} has no runner job")
    existing = token.get(name)
    if existing is not None and existing != job_id:
        raise StreamDataError(f"state {symbol} has conflicting {name}")
    token[name] = job_id


def apply_open_fill(
    state: LifecycleState,
    symbol: str,
    job_id: str,
    result_id: str,
    quantity: Decimal,
    entry_price: Decimal,
    config: LifecycleConfig,
) -> bool:
    """Adopt one runner-proven OPEN fill; return True iff state changed."""
    if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
        raise StreamDataError(f"runner fill for {symbol} has invalid quantity")
    if not isinstance(entry_price, Decimal) or not entry_price.is_finite() \
            or entry_price <= 0:
        raise StreamDataError(f"runner fill for {symbol} has invalid entry price")
    token = state.tokens.get(symbol)
    if token is None or token.get("phase") not in {
        PHASE_ENTER_SMALL, PHASE_HOLD, PHASE_EXIT_PENDING
    }:
        raise StreamDataError(f"runner OPEN fill has no pending signal for {symbol}")
    existing_job = token.get("open_job_id")
    if existing_job is not None and existing_job != job_id:
        raise StreamDataError(f"runner OPEN fill job conflicts for {symbol}")
    if token.get("fill_result_id") is not None:
        same = (
            token.get("fill_result_id") == result_id
            and token.get("qty") == fmt(quantity)
            and token.get("avg_entry_price") == fmt(entry_price)
        )
        if not same:
            raise StreamDataError(f"runner OPEN fill conflicts for {symbol}")
        return False
    if token.get("phase") != PHASE_ENTER_SMALL:
        raise StreamDataError(f"runner OPEN fill arrived after lifecycle advanced for {symbol}")
    entry = token.get("entry_structure")
    if not isinstance(entry, dict):
        raise StreamDataError(f"runner OPEN fill lacks signal structure for {symbol}")
    with localcontext() as context:
        context.prec = 50
        cost = quantity * entry_price
        stop = trail_stop_price(entry_price, config.trail_pct)
    entry["price"] = fmt(entry_price)
    entry["fill_price"] = fmt(entry_price)
    entry["fill_qty"] = fmt(quantity)
    entry["fill_result_id"] = result_id
    token.update(
        {
            "phase": PHASE_HOLD,
            "qty": fmt(quantity),
            "paper_cost": fmt(cost),
            "avg_entry_price": fmt(entry_price),
            "entry_price": fmt(entry_price),
            "peak_price": fmt(entry_price),
            "trail_stop_price": fmt(stop),
            "open_job_id": job_id,
            "fill_result_id": result_id,
            "position_status": "OPEN",
        }
    )
    return True


def confirm_position_flat(
    state: LifecycleState, symbol: str, job_id: str, result_id: str
) -> bool:
    """Clear exposure only after matching EXIT result and runner-flat truth."""
    token = state.tokens.get(symbol)
    if token is None:
        raise StreamDataError(f"runner EXIT result for unknown token {symbol}")
    if token.get("close_result_id") is not None:
        if token.get("close_result_id") != result_id:
            raise StreamDataError(f"runner EXIT result conflicts for {symbol}")
        return False
    if token.get("phase") != PHASE_EXIT_PENDING:
        raise StreamDataError(f"runner flat truth has no pending EXIT for {symbol}")
    existing_job = token.get("exit_job_id")
    if existing_job is not None and existing_job != job_id:
        raise StreamDataError(f"runner EXIT job conflicts for {symbol}")
    _reset_watch(token, rearm_required=True)
    token["close_result_id"] = result_id
    return True


def _ignition_invalidated(token: Mapping[str, Any], record: StreamRecord, price: Decimal, config: LifecycleConfig) -> bool:
    if record.surge_ratio is None:
        return True
    pre_ignition = _optional_decimal(token.get("pre_ignition_price"))
    if pre_ignition is None:
        entry = token.get("entry_structure")
        if isinstance(entry, dict):
            pre_ignition = _optional_decimal(entry.get("structure_price"))
    return bool(
        record.surge_ratio <= config.ignition_invalidation_surge_ratio
        and pre_ignition is not None
        and price <= pre_ignition
    )


def process_record(state: LifecycleState, record: StreamRecord, config: LifecycleConfig, receipt_path: Path, now_fn: Callable[[], str] = utc_now) -> Dict[str, Any]:
    """Apply one token observation and return its factual strategy decision."""
    token = state.tokens.setdefault(record.symbol, _zero_token(record.symbol))
    basis = basis_availability(record, config)
    if token.get("last_observation_id") == record.observation_id:
        token["basis_available"], token["basis_note"] = basis
        token["basis_gap_pct"] = None if record.basis_gap_pct is None else fmt(record.basis_gap_pct)
        return _decision(token, ACTION_DUPLICATE, "observation already processed")

    # A queued EXIT is single-shot.  Runner truth, not another signal pass,
    # decides when the position can be cleared.
    if token.get("phase") == PHASE_EXIT_PENDING:
        _commit_observation(token, record, basis)
        return _decision(token, ACTION_HOLD, "runner EXIT pending; position still stands")

    # Likewise, a signal is not a fill.  Do not re-enter or start HOLD until a
    # matching result-ledger fill and OPEN position have been reconciled.
    if token.get("phase") == PHASE_ENTER_SMALL:
        _commit_observation(token, record, basis)
        return _decision(token, ACTION_HOLD, "runner OPEN pending; no fill adopted")

    previous_utc = token.get("last_stream_utc")
    out_of_order = previous_utc is not None and record.utc_value < _parse_utc(previous_utc, f"state {record.symbol}")
    qty = _state_decimal(token, "qty", record.symbol)
    if out_of_order:
        if qty > 0:
            receipt_id = _exit(token, record, config, receipt_path, now_fn, "out_of_order_stream", basis)
            return _decision(token, ACTION_EXIT, "out-of-order stream fails closed", receipt_id=receipt_id)
        token["candidate"] = False
        token["persistence_count"] = 0
        token["basis_available"], token["basis_note"] = basis
        return _decision(token, ACTION_WATCH, "out-of-order stream ignored")

    bid, ask, price, _ = record.quote()
    fresh = record.is_fresh(config) and price is not None and bid is not None and ask is not None
    if not fresh:
        if qty > 0:
            receipt_id = _exit(token, record, config, receipt_path, now_fn, "freshness_lost", basis)
            return _decision(token, ACTION_EXIT, "freshness or executable quote lost; fail-closed exit", receipt_id=receipt_id)
        token["candidate"] = False
        token["persistence_count"] = 0
        token["pre_ignition_price"] = None
        _commit_observation(token, record, basis)
        return _decision(token, ACTION_WATCH, "freshness or executable quote unavailable")

    assert price is not None and bid is not None and ask is not None
    previous_price = _optional_decimal(token.get("last_price"))
    previous_surge = _optional_decimal(token.get("last_surge_ratio"))

    if qty <= 0:
        if token.get("rearm_required"):
            if record.surge_ratio is not None and record.surge_ratio < config.ignition_surge_ratio:
                token["rearm_required"] = False
            _commit_observation(token, record, basis)
            return _decision(token, ACTION_WATCH, "post-exit rearm requires surge below ignition")

        just_ignited = False
        if not token.get("candidate"):
            crossed = bool(
                record.surge_ratio is not None
                and record.surge_ratio >= config.ignition_surge_ratio
                and previous_surge is not None
                and previous_surge < config.ignition_surge_ratio
            )
            if crossed:
                token["candidate"] = True
                token["ignition_utc"] = record.utc
                token["pre_ignition_price"] = fmt(previous_price if previous_price is not None else price)
                token["persistence_count"] = 0
                just_ignited = True

        if token.get("candidate"):
            floor = _optional_decimal(token.get("pre_ignition_price"))
            persists = bool(
                record.surge_ratio is not None
                and record.surge_ratio >= config.persistence_surge_ratio
                and floor is not None
                and price > floor * (Decimal("1") + config.persistence_price_hold_pct)
            )
            if persists:
                token["persistence_count"] += 1
            else:
                token["candidate"] = False
                token["persistence_count"] = 0
                token["pre_ignition_price"] = None
            if token.get("candidate") and token["persistence_count"] >= config.persistence_passes:
                entered, receipt_id = _enter_small(token, record, config, receipt_path, now_fn)
                if entered:
                    return _decision(token, ACTION_ENTER_SMALL, "configured persistence satisfied", receipt_id=receipt_id)
                token["candidate"] = False
                token["persistence_count"] = 0
                _commit_observation(token, record, basis)
                return _decision(token, ACTION_WATCH, "initial runner sizing rounded to zero")
            _commit_observation(token, record, basis)
            reason = "ignition edge observed; persistence started" if just_ignited else "candidate persistence pending"
            return _decision(token, ACTION_WATCH, reason)

        _commit_observation(token, record, basis)
        return _decision(token, ACTION_WATCH, "no fresh ignition edge")

    peak = _state_decimal(token, "peak_price", record.symbol, positive=True)
    if price > peak:
        peak = price
        token["peak_price"] = fmt(peak)
    stop = trail_stop_price(peak, config.trail_pct)
    token["trail_stop_price"] = fmt(stop)
    if price <= stop:
        _commit_observation(token, record, basis)
        receipt_id = _exit(token, record, config, receipt_path, now_fn, "trail_hit", basis)
        return _decision(token, ACTION_EXIT, "price reached trailing stop", receipt_id=receipt_id)
    if _ignition_invalidated(token, record, price, config):
        _commit_observation(token, record, basis)
        receipt_id = _exit(token, record, config, receipt_path, now_fn, "ignition_invalidated", basis)
        return _decision(token, ACTION_EXIT, "ignition and price structure invalidated", receipt_id=receipt_id)

    add_checks = add_strength_checks(token, record, config)
    fade_checks = deterioration_checks(token, record, config)
    if all(add_checks.values()):
        receipt_id = _add(token, record, config, receipt_path, now_fn)
        if receipt_id is not None:
            return _decision(token, ACTION_ADD, "all strengthening gates passed", add_checks=add_checks, fade_checks=fade_checks, receipt_id=receipt_id)

    # The first failed strengthening pass permanently closes this ignition's
    # add window.  A later bounce may trail, but cannot be chased with an add.
    token["adding_open"] = False
    if any(fade_checks.values()):
        reasons = ",".join(name for name, failed in fade_checks.items() if failed)
        receipt_id = _reduce(token, record, config, receipt_path, now_fn, reasons)
        if receipt_id is not None:
            return _decision(token, ACTION_REDUCE_TRAIL, "structure deteriorated; reduced and trailing", add_checks=add_checks, fade_checks=fade_checks, receipt_id=receipt_id)
    token["phase"] = PHASE_HOLD
    _commit_observation(token, record, basis)
    return _decision(token, ACTION_HOLD, "position held; add window closed", add_checks=add_checks, fade_checks=fade_checks)


def process_missing_token(state: LifecycleState, symbol: str, config: LifecycleConfig, receipt_path: Path, now_fn: Callable[[], str] = utc_now) -> Dict[str, Any]:
    token = state.tokens[symbol]
    qty = _state_decimal(token, "qty", symbol)
    basis = (False, "unavailable: token absent from stream pass")
    if token.get("phase") == PHASE_EXIT_PENDING:
        return _decision(token, ACTION_HOLD, "runner EXIT pending; position still stands")
    if token.get("phase") == PHASE_ENTER_SMALL:
        return _decision(token, ACTION_HOLD, "runner OPEN pending; token missing")
    if qty > 0:
        receipt_id = _exit(token, None, config, receipt_path, now_fn, "freshness_lost_missing_token", basis)
        return _decision(token, ACTION_EXIT, "token missing from stream; fail-closed exit", receipt_id=receipt_id)
    token["candidate"] = False
    token["persistence_count"] = 0
    token["pre_ignition_price"] = None
    token["basis_available"] = False
    token["basis_gap_pct"] = None
    token["basis_note"] = basis[1]
    # A gap in observation continuity cannot prove a later threshold cross.
    # Keep the timestamp watermark, but require a new baseline pass.
    token["last_price"] = None
    token["last_bid"] = None
    token["last_ask"] = None
    token["last_vol_1m_quote"] = None
    token["last_surge_ratio"] = None
    token["last_oi"] = None
    token["last_oi_change_pct"] = None
    token["last_spread_pct"] = None
    token["last_observation_id"] = None
    return _decision(token, ACTION_WATCH, "token missing from stream")


def parse_stream_state(payload: Any) -> Dict[str, StreamRecord]:
    if not isinstance(payload, dict):
        raise StreamDataError("stream_state root must map symbols to records")
    return {symbol: StreamRecord.from_payload(symbol, raw) for symbol, raw in payload.items()}


def read_stream_state(path: Path) -> Dict[str, StreamRecord]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise StreamDataError(f"stream_state unreadable: {path}") from None
    return parse_stream_state(payload)


def process_stream(state: LifecycleState, records: Mapping[str, StreamRecord], config: LifecycleConfig, receipt_path: Path, now_fn: Callable[[], str] = utc_now) -> List[Dict[str, Any]]:
    decisions: List[Dict[str, Any]] = []
    for symbol in sorted(set(state.tokens).union(records)):
        if symbol in records:
            decisions.append(process_record(state, records[symbol], config, receipt_path, now_fn))
        else:
            decisions.append(process_missing_token(state, symbol, config, receipt_path, now_fn))
    return decisions


def run_tick(stream_state_path: Path, state_path: Path = DEFAULT_STATE_PATH, receipt_path: Path = DEFAULT_RECEIPT_PATH, config: Optional[LifecycleConfig] = None, now_fn: Callable[[], str] = utc_now) -> Tuple[List[Dict[str, Any]], int]:
    """Load, reconcile, process one shared-stream snapshot, and atomically save."""
    resolved_config = config or LifecycleConfig()
    records = read_stream_state(Path(stream_state_path))
    state = load_state(Path(state_path))
    replayed = reconcile_receipts(state, Path(receipt_path))
    decisions = process_stream(state, records, resolved_config, Path(receipt_path), now_fn)
    save_state(Path(state_path), state, now_fn)
    return decisions, replayed


def _decimal_argument(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("must be a decimal") from None
    if not value.is_finite():
        raise argparse.ArgumentTypeError("must be finite")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="runner-backed dynamic momentum lifecycle")
    parser.add_argument("--tick", required=True, type=Path, metavar="STREAM_STATE", help="process one frozen-schema stream_state JSON file")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--receipts", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument("--ignition-threshold", type=_decimal_argument, default=Decimal("10"))
    parser.add_argument("--persistence-threshold", type=_decimal_argument, default=Decimal("8"))
    parser.add_argument("--persistence-passes", type=int, default=2)
    parser.add_argument("--persistence-price-hold-pct", type=_decimal_argument, default=Decimal("0"))
    parser.add_argument("--max-freshness-ms", type=_decimal_argument, default=Decimal("5000"))
    parser.add_argument("--initial-notional", type=_decimal_argument, default=Decimal("10"))
    parser.add_argument("--add-notional", type=_decimal_argument, default=Decimal("5"))
    parser.add_argument("--max-total-notional", type=_decimal_argument, default=Decimal("30"))
    parser.add_argument("--max-adds", type=int, default=4)
    parser.add_argument("--trail-pct", type=_decimal_argument, default=Decimal("0.15"))
    parser.add_argument("--basis-material-pct", type=_decimal_argument, default=Decimal("0.50"))
    parser.add_argument("--min-volume-growth-pct", type=_decimal_argument, default=Decimal("0.05"))
    parser.add_argument("--min-surge-growth-pct", type=_decimal_argument, default=Decimal("0.02"))
    parser.add_argument("--min-price-growth-pct", type=_decimal_argument, default=Decimal("0.002"))
    parser.add_argument("--max-spread-pct", type=_decimal_argument, default=Decimal("0.01"))
    parser.add_argument("--min-depth-improvement-pct", type=_decimal_argument, default=Decimal("0"))
    parser.add_argument("--min-oi-growth-pct", type=_decimal_argument, default=Decimal("0.001"))
    parser.add_argument("--min-oi-change-pct", type=_decimal_argument, default=Decimal("0"))
    parser.add_argument("--volume-fade-pct", type=_decimal_argument, default=Decimal("0.30"))
    parser.add_argument("--oi-fade-pct", type=_decimal_argument, default=Decimal("0.03"))
    parser.add_argument("--oi-change-fade-pct", type=_decimal_argument, default=Decimal("-2"))
    parser.add_argument("--soft-price-fade-pct", type=_decimal_argument, default=Decimal("0.08"))
    parser.add_argument("--depth-worsening-pct", type=_decimal_argument, default=Decimal("0.25"))
    parser.add_argument("--reduce-fraction", type=_decimal_argument, default=Decimal("0.25"))
    parser.add_argument("--invalidation-surge-ratio", type=_decimal_argument, default=Decimal("1"))
    return parser


def _config_from_args(args: argparse.Namespace) -> LifecycleConfig:
    return LifecycleConfig(
        ignition_surge_ratio=args.ignition_threshold,
        persistence_surge_ratio=args.persistence_threshold,
        persistence_passes=args.persistence_passes,
        persistence_price_hold_pct=args.persistence_price_hold_pct,
        max_freshness_ms=args.max_freshness_ms,
        initial_notional=args.initial_notional,
        add_notional=args.add_notional,
        max_total_notional=args.max_total_notional,
        max_adds=args.max_adds,
        trail_pct=args.trail_pct,
        basis_material_pct=args.basis_material_pct,
        min_volume_growth_pct=args.min_volume_growth_pct,
        min_surge_growth_pct=args.min_surge_growth_pct,
        min_price_growth_pct=args.min_price_growth_pct,
        max_spread_pct=args.max_spread_pct,
        min_depth_improvement_pct=args.min_depth_improvement_pct,
        min_oi_growth_pct=args.min_oi_growth_pct,
        min_oi_change_pct=args.min_oi_change_pct,
        volume_fade_pct=args.volume_fade_pct,
        oi_fade_pct=args.oi_fade_pct,
        oi_change_fade_pct=args.oi_change_fade_pct,
        soft_price_fade_pct=args.soft_price_fade_pct,
        depth_worsening_pct=args.depth_worsening_pct,
        reduce_fraction=args.reduce_fraction,
        ignition_invalidation_surge_ratio=args.invalidation_surge_ratio,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        decisions, replayed = run_tick(
            args.tick,
            state_path=args.state,
            receipt_path=args.receipts,
            config=_config_from_args(args),
        )
    except (MomentumLifecycleError, OSError) as exc:
        print(f"momentum_lifecycle_error={exc}", file=sys.stderr)
        return 2
    print(json.dumps({"runner_truth": True, "replayed_receipts": replayed}, sort_keys=True))
    for decision in decisions:
        print(json.dumps(decision, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
