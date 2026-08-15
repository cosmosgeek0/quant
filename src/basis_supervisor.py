#!/usr/bin/env python3
"""Stateless tick driver for the persistent basis-position machine.

The supervisor owns ordering, not trading mechanics.  Every lifecycle action
is delegated to ``basis_position`` and every tick reloads its durable state.
Ladder mode adds one atomic pending-rung receipt; held exposure is always a
fresh projection of the durable basis-position receipt and its own order ids.

Exit codes: 0 acted, 1 held, 2 frozen, 3 idle.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import hmac
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Callable, Dict, List, Mapping, Optional

import basis_ladder
import basis_position
from basis_cycle import client_id as position_client_id
from basis_events import (
    DislocationEvent,
    event_digest,
    restore_event,
    serialize_event,
    validate_actionable,
)
from basis_position import (
    EMERGENCY_FILE,
    EXIT_CODE as POSITION_EXIT_CODE,
    FEE_BUFFER,
    FREEZE_NEW_ACTIONS,
    LOCK_FILE,
    MARGIN_QUANTUM,
    PROPOSAL_DIR,
    PROPOSAL_ID_RE,
    REQUEST_EMERGENCY_UNWIND,
    REQUEST_EXIT,
    STATE_FILE,
    STOP_FILE,
    ST_CLOSED,
    ST_CLOSING,
    ST_NONE,
    ST_ONE_LEG,
    ST_OPEN,
    ST_OPENING,
    ST_UNKNOWN,
    SYMBOL_RE,
    PositionConfig,
    PositionError,
    cmd_check,
    cmd_close,
    cmd_open,
    cmd_resume,
    config_from_env,
    default_now_ms,
    live_request_factory,
    load_entry_proposal,
    load_position,
    supervise,
)
from job_runner import (
    FIRST_AUTONOMOUS_CLOSE_MARKER,
    JOB_ID_RE,
    Job,
    Outcome as RunnerOutcome,
    _acquire_governor,
    _consume_accept_one,
    _entry_outcome_succeeded,
    _entry_outcome_terminal,
    _entry_ticket_refusal,
    _load_operator,
    _paths,
    _release_governor,
    _release_recovered_governor,
    _touch_stop,
)

try:
    from limits import FIRST_TRADE_CAP_USDT
except ImportError:
    # TODO: remove this fallback after the sibling limits.py lane merges.
    FIRST_TRADE_CAP_USDT = Decimal("8")


EXIT_ACTED = 0
EXIT_HELD = 1
EXIT_FROZEN = 2
EXIT_IDLE = 3

FREEZE_STATES = frozenset((ST_UNKNOWN, ST_ONE_LEG, ST_OPENING, ST_CLOSING))
CLOSE_DECISIONS = frozenset((REQUEST_EXIT, REQUEST_EMERGENCY_UNWIND))
PROPOSAL_NAME_RE = re.compile(r"proposal-(.+)\.json")
HEALTH_FILE_NAME = "health.jsonl"
WATCH_STATE_PATH = os.path.join("watch", "basis_watch_state.json")
ACCEPT_ONE_FILE = "ACCEPT-ONE"
RUNNER_STOP_FILE = "STOP-JOBS"
POSITION_STATES = (ST_NONE, ST_OPENING, ST_ONE_LEG, ST_OPEN, ST_CLOSING,
                   ST_CLOSED, ST_UNKNOWN)
_ENTRY_OPEN_LOCK = threading.Lock()

LADDER_REVERSE_DISABLED = "REVERSE_DIRECTION_DISABLED"
LADDER_REVERSE_UNWIRED = "REVERSE_DIRECTION_EXECUTION_UNWIRED"
LADDER_PROPOSAL_UNAVAILABLE = "EVENT_PROPOSAL_UNAVAILABLE"
LADDER_FILL_INVALID = "RECONCILED_RUNG_FILL_INVALID"
LADDER_SOURCE_INVALID = "LADDER_SOURCE_INVALID"
PENDING_RUNG_INVALID = "PENDING_RUNG_INVALID"
PENDING_RUNG_CONFIG_MISMATCH = "PENDING_RUNG_CONFIG_MISMATCH"
PENDING_RUNG_SIZE_OUT_OF_BOUNDS = "PENDING_RUNG_SIZE_OUT_OF_BOUNDS"
PENDING_RUNG_RECORD_MISSING = "PENDING_RUNG_RECORD_MISSING"
PENDING_RUNG_RECORD_INVALID = "PENDING_RUNG_RECORD_INVALID"
PENDING_RUNG_PROVENANCE_INVALID = "PENDING_RUNG_PROVENANCE_INVALID"
PENDING_RUNG_FILE = "basis-ladder-pending.json"
PENDING_RUNG_VERSION = 1
PENDING_RUNG_VERDICT = "PENDING_RUNG"
RUNG_GATE_PASS = "PASS"
RUNG_GATE_REFUSED = "REFUSED"
RUNG_GATE_FROZEN = "FROZEN"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Public identity for callers which need the complete pure ladder.  Runtime
# market checks stay in cmd_check: the supervisor must not reproduce its
# executable-price/funding reads or its decision rules.
supervision_ladder = supervise


@dataclass(frozen=True)
class ProposalCandidate:
    proposal_id: str
    symbol: str
    created_ms: int
    expires_ms: int
    net_edge_pct: Decimal
    requested_notional: Decimal
    digest: str


@dataclass(frozen=True)
class LadderModeConfig:
    """Immutable schedule, bounds, identity, and direction for ladder mode."""

    schedule: tuple[basis_ladder.LadderLevel, ...]
    max_rungs: int
    max_total_notional: Decimal
    min_notional: Decimal
    symbol: str
    direction: str = basis_ladder.NORMAL
    reverse_enabled: bool = False
    convergence_gap: Decimal = Decimal("0")
    max_unwind_notional_per_tick: Optional[Decimal] = None

    def __post_init__(self) -> None:
        engine = basis_ladder.LadderConfig(
            schedule=self.schedule,
            max_rungs=self.max_rungs,
            max_total_notional=self.max_total_notional,
            min_notional=self.min_notional,
            convergence_gap=self.convergence_gap,
            max_unwind_notional_per_tick=self.max_unwind_notional_per_tick,
        )
        object.__setattr__(self, "schedule", engine.schedule)
        if not isinstance(self.symbol, str) \
                or SYMBOL_RE.fullmatch(self.symbol) is None:
            raise ValueError("ladder symbol is invalid")
        if self.direction not in (basis_ladder.NORMAL, basis_ladder.REVERSE):
            raise ValueError("ladder direction is invalid")
        if not isinstance(self.reverse_enabled, bool):
            raise ValueError("ladder reverse_enabled must be bool")

    @property
    def engine(self) -> basis_ladder.LadderConfig:
        return basis_ladder.LadderConfig(
            schedule=self.schedule,
            max_rungs=self.max_rungs,
            max_total_notional=self.max_total_notional,
            min_notional=self.min_notional,
            convergence_gap=self.convergence_gap,
            max_unwind_notional_per_tick=self.max_unwind_notional_per_tick,
        )


@dataclass(frozen=True)
class PendingRungRecord:
    """Self-digested transition receipt; it is never held-exposure truth."""

    version: int
    verdict: str
    proposal_id: str
    proposal_digest: str
    intent_size: Decimal
    effective_cap: Decimal
    event_digest: str
    event_utc: str
    event: DislocationEvent
    direction: str
    symbol: str
    created_ms: int
    config_digest: str
    schedule_index: int
    threshold: Decimal
    scheduled_size: Decimal
    gap_at_entry: Decimal
    record_digest: str


@dataclass(frozen=True)
class RungGateRow:
    """Typed result emitted by the one ladder gate on every ladder path."""

    outcome: str
    reason: str
    state: Optional[basis_ladder.LadderState] = None
    held_notional: Optional[Decimal] = None


def _text(value: Any, limit: int = 300) -> str:
    """One safe factual row, without multiline transport/error text."""
    return " ".join(str(value).split())[:limit]


def _row(action: str, outcome: str, note: Any) -> None:
    print("SUPERVISOR | %s | %s | %s"
          % (_text(action, 40), _text(outcome, 40), _text(note)))


def _utc_from_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _count_reject(histogram: Dict[str, int], reason: str) -> None:
    histogram[reason] = histogram.get(reason, 0) + 1


def _ladder_reject_key(reason: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", reason.lower()).strip("_")
    return "ladder_%s" % (normalized or "refused")


def _ladder_config_payload(config: LadderModeConfig) -> Dict[str, Any]:
    return {
        "schedule": [
            [format(level.gap_threshold, "f"), format(level.rung_size, "f")]
            for level in config.schedule
        ],
        "max_rungs": config.max_rungs,
        "max_total_notional": format(config.max_total_notional, "f"),
        "min_notional": format(config.min_notional, "f"),
        "convergence_gap": format(config.convergence_gap, "f"),
        "max_unwind_notional_per_tick": None
        if config.max_unwind_notional_per_tick is None
        else format(config.max_unwind_notional_per_tick, "f"),
        "symbol": config.symbol,
        "direction": config.direction,
        "reverse_enabled": config.reverse_enabled,
    }


def _ladder_config_digest(config: LadderModeConfig) -> str:
    encoded = json.dumps(
        _ladder_config_payload(config), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_PENDING_FIELDS = frozenset((
    "version", "verdict", "proposal_id", "proposal_digest", "intent_size",
    "effective_cap", "event_digest", "event_utc", "event", "direction",
    "symbol", "created_ms", "config_digest", "schedule_index", "threshold",
    "scheduled_size", "gap_at_entry", "record_digest",
))
_PENDING_DECIMAL_FIELDS = (
    "intent_size", "effective_cap", "threshold", "scheduled_size",
    "gap_at_entry",
)


def _pending_payload(record: PendingRungRecord) -> Dict[str, Any]:
    return {
        "version": record.version,
        "verdict": record.verdict,
        "proposal_id": record.proposal_id,
        "proposal_digest": record.proposal_digest,
        "intent_size": format(record.intent_size, "f"),
        "effective_cap": format(record.effective_cap, "f"),
        "event_digest": record.event_digest,
        "event_utc": record.event_utc,
        "event": serialize_event(record.event),
        "direction": record.direction,
        "symbol": record.symbol,
        "created_ms": record.created_ms,
        "config_digest": record.config_digest,
        "schedule_index": record.schedule_index,
        "threshold": format(record.threshold, "f"),
        "scheduled_size": format(record.scheduled_size, "f"),
        "gap_at_entry": format(record.gap_at_entry, "f"),
        "record_digest": record.record_digest,
    }


def _pending_digest(payload: Mapping[str, Any]) -> str:
    body = dict(payload)
    body.pop("record_digest", None)
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _pending_shape_reason(record: Any) -> Optional[str]:
    if not isinstance(record, PendingRungRecord):
        return PENDING_RUNG_RECORD_INVALID
    if isinstance(record.version, bool) \
            or not isinstance(record.version, int) \
            or record.version != PENDING_RUNG_VERSION \
            or record.verdict != PENDING_RUNG_VERDICT:
        return PENDING_RUNG_RECORD_INVALID
    if not isinstance(record.proposal_id, str) \
            or PROPOSAL_ID_RE.fullmatch(record.proposal_id) is None \
            or not isinstance(record.proposal_digest, str) \
            or _SHA256_RE.fullmatch(record.proposal_digest) is None \
            or not isinstance(record.event_digest, str) \
            or _SHA256_RE.fullmatch(record.event_digest) is None \
            or not isinstance(record.config_digest, str) \
            or _SHA256_RE.fullmatch(record.config_digest) is None \
            or not isinstance(record.record_digest, str) \
            or _SHA256_RE.fullmatch(record.record_digest) is None:
        return PENDING_RUNG_RECORD_INVALID
    decimals = tuple(getattr(record, name) for name in _PENDING_DECIMAL_FIELDS)
    if any(not isinstance(value, Decimal) or not value.is_finite()
           for value in decimals):
        return PENDING_RUNG_RECORD_INVALID
    if record.intent_size <= 0 or record.effective_cap <= 0 \
            or record.effective_cap > record.intent_size \
            or record.scheduled_size <= 0:
        return PENDING_RUNG_RECORD_INVALID
    if isinstance(record.created_ms, bool) \
            or not isinstance(record.created_ms, int) \
            or record.created_ms < 0 \
            or isinstance(record.schedule_index, bool) \
            or not isinstance(record.schedule_index, int) \
            or record.schedule_index < 0:
        return PENDING_RUNG_RECORD_INVALID
    if record.direction not in (basis_ladder.NORMAL, basis_ladder.REVERSE) \
            or not isinstance(record.symbol, str) \
            or SYMBOL_RE.fullmatch(record.symbol) is None \
            or not isinstance(record.event_utc, str) \
            or not isinstance(record.event, DislocationEvent) \
            or record.event_utc != record.event.utc:
        return PENDING_RUNG_RECORD_INVALID
    try:
        actual_event_digest = event_digest(record.event)
        payload = _pending_payload(record)
        actual_record_digest = _pending_digest(payload)
    except (TypeError, ValueError):
        return PENDING_RUNG_RECORD_INVALID
    if not hmac.compare_digest(record.event_digest, actual_event_digest) \
            or not hmac.compare_digest(record.record_digest,
                                       actual_record_digest):
        return PENDING_RUNG_RECORD_INVALID
    return None


def _pending_from_payload(payload: Any) -> PendingRungRecord:
    if not isinstance(payload, dict) or set(payload) != _PENDING_FIELDS:
        raise PositionError("pending rung record shape is invalid")
    parsed: Dict[str, Decimal] = {}
    for name in _PENDING_DECIMAL_FIELDS:
        raw = payload.get(name)
        if not isinstance(raw, str) or not raw:
            raise PositionError("pending rung record %s is invalid" % name)
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError):
            raise PositionError(
                "pending rung record %s is invalid" % name) from None
        if not value.is_finite():
            raise PositionError("pending rung record %s is invalid" % name)
        parsed[name] = value
    try:
        restored_event = restore_event(payload.get("event"))
        record = PendingRungRecord(
            version=payload.get("version"),
            verdict=payload.get("verdict"),
            proposal_id=payload.get("proposal_id"),
            proposal_digest=payload.get("proposal_digest"),
            event_digest=payload.get("event_digest"),
            event_utc=payload.get("event_utc"),
            event=restored_event,
            direction=payload.get("direction"),
            symbol=payload.get("symbol"),
            created_ms=payload.get("created_ms"),
            config_digest=payload.get("config_digest"),
            schedule_index=payload.get("schedule_index"),
            record_digest=payload.get("record_digest"),
            **parsed,
        )
    except (TypeError, ValueError):
        raise PositionError("pending rung record shape is invalid") from None
    if _pending_shape_reason(record) is not None:
        raise PositionError("pending rung record digest or shape is invalid")
    return record


def _pending_path(receipt_dir: str) -> str:
    return os.path.join(receipt_dir, PENDING_RUNG_FILE)


def _proposal_claim_exists(receipt_dir: str) -> bool:
    try:
        names = os.listdir(os.path.join(receipt_dir, PROPOSAL_DIR))
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    for name in names:
        if not name.startswith("proposal-") or not name.endswith(".claim"):
            continue
        proposal_id = name[len("proposal-"):-len(".claim")]
        if PROPOSAL_ID_RE.fullmatch(proposal_id) is not None:
            return True
    return False


def _load_pending_record(receipt_dir: str) -> Optional[PendingRungRecord]:
    try:
        with open(_pending_path(receipt_dir), encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError):
        raise PositionError("pending rung record is unreadable") from None
    return _pending_from_payload(payload)


def _write_pending_record(receipt_dir: str,
                          record: PendingRungRecord) -> None:
    """Validate a complete record, then make one atomic state replacement."""
    if _pending_shape_reason(record) is not None:
        raise PositionError("pending rung record is invalid")
    payload = _pending_payload(record)
    # Validate the exact serialized next record before it can replace anything.
    _pending_from_payload(payload)
    encoded = (json.dumps(
        payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    path = _pending_path(receipt_dir)
    temporary = "%s.tmp-%s-%s" % (
        path, os.getpid(), threading.get_ident())
    descriptor: Optional[int] = None
    directory: Optional[int] = None
    committed = False
    try:
        os.makedirs(receipt_dir, mode=0o700, exist_ok=True)
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("pending rung write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        directory = os.open(receipt_dir, os.O_RDONLY)
        os.replace(temporary, path)
        committed = True
        try:
            os.fsync(directory)
        except OSError:
            # The atomic rename is the acceptance boundary.  A later directory
            # sync concern cannot be reported as an uncommitted state change.
            pass
    except OSError as error:
        if committed:
            return
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise PositionError(
            "pending rung record atomic write failed: %s"
            % type(error).__name__) from None
    finally:
        if directory is not None:
            try:
                os.close(directory)
            except OSError:
                pass


def _ladder_refusal(reason: str,
                    rejects: Optional[Dict[str, int]] = None, *,
                    outcome: str = RUNG_GATE_REFUSED) -> None:
    if rejects is not None:
        _count_reject(rejects, _ladder_reject_key(reason))
    _row("LADDER", outcome, reason)


def _append_health_jsonl(receipt_dir: str,
                         payload: Dict[str, Any]) -> bool:
    """Append one compact UI telemetry record without gating supervision."""
    path = os.path.join(receipt_dir, HEALTH_FILE_NAME)
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":"))
               + "\n").encode("utf-8")
    descriptor: Optional[int] = None
    try:
        os.makedirs(receipt_dir, mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags, 0o600)
        if os.write(descriptor, encoded) != len(encoded):
            raise OSError("short health append")
        os.fsync(descriptor)
        return True
    except OSError:
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _watch_last_pass_ms(receipt_dir: str) -> Optional[int]:
    path = os.path.join(receipt_dir, WATCH_STATE_PATH)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        value = payload["watermark"]["last_pass_ms"]
    except (OSError, KeyError, TypeError, ValueError, UnicodeError):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _append_tick_health(cfg: PositionConfig, now_ms: int,
                        code: Optional[int], error: Optional[str],
                        rejects: Dict[str, int]) -> bool:
    try:
        position = load_position(cfg.receipt_dir)
    except PositionError:
        position = {"state": ST_UNKNOWN}
    state = position.get("state") if isinstance(position, dict) else ST_NONE
    if state not in POSITION_STATES:
        state = ST_UNKNOWN
    state_counts = {name: 0 for name in POSITION_STATES}
    state_counts[state] = 1
    last_pass_ms = _watch_last_pass_ms(cfg.receipt_dir)
    age = None
    if last_pass_ms is not None and now_ms >= last_pass_ms:
        age = now_ms - last_pass_ms
    if error is not None or code is None or code == EXIT_FROZEN:
        status = "ERROR"
    elif rejects:
        status = "DEGRADED"
    elif code == EXIT_IDLE:
        status = "IDLE"
    else:
        status = "OK"
    payload: Dict[str, Any] = {
        "component": "basis_supervisor",
        "cycle": "tick",
        "exit_code": code,
        "last_pass_age_ms": age,
        "last_pass_ms": last_pass_ms,
        "reject_histogram": dict(sorted(rejects.items())),
        "state_counts": state_counts,
        "status": status,
        "ts": _utc_from_ms(now_ms),
    }
    if error is not None:
        payload["error"] = error
    return _append_health_jsonl(cfg.receipt_dir, payload)


def _proposal_ms(payload: dict, field: str) -> int:
    text = payload[field]
    parsed = datetime.fromisoformat(
        text[:-1] + "+00:00" if text.endswith("Z") else text)
    return int(parsed.timestamp() * 1000)


def _positive_decimal(value: Any, label: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise PositionError("proposal %s is not a positive decimal" % label)
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PositionError(
            "proposal %s is not a positive decimal" % label) from None
    if not parsed.is_finite() or parsed <= 0:
        raise PositionError("proposal %s is not a positive decimal" % label)
    return parsed


def _accepted_notionals(accepted: Any) -> tuple[Decimal, Decimal]:
    """Return the exact proposal-plan notionals without guessing missing data."""
    payload = accepted.payload
    quantity = _positive_decimal(accepted.quantity, "quantity")
    spot_raw = payload.get("spot_quote_cap")
    if spot_raw is None:
        spot_ask = _positive_decimal(payload.get("spot_ask"), "spot_ask")
        spot_notional = (
            quantity * spot_ask * (Decimal(1) + FEE_BUFFER)
        ).quantize(MARGIN_QUANTUM)
    else:
        spot_notional = _positive_decimal(spot_raw, "spot_quote_cap")
    short_raw = payload.get("short_notional")
    if short_raw is None:
        perp_bid = _positive_decimal(payload.get("perp_bid"), "perp_bid")
        short_notional = (quantity * perp_bid).quantize(MARGIN_QUANTUM)
    else:
        short_notional = _positive_decimal(short_raw, "short_notional")
    if spot_notional <= 0 or short_notional <= 0:
        raise PositionError("proposal execution notionals must be positive")
    return spot_notional, short_notional


def _clamp_accepted_proposal(accepted: Any, cap: Decimal) -> Any:
    """Derive a capped execution view while preserving source bytes/digest."""
    if not isinstance(cap, Decimal) or not cap.is_finite() or cap <= 0:
        raise PositionError("first-trade notional cap is invalid")
    spot_notional, short_notional = _accepted_notionals(accepted)
    largest = max(spot_notional, short_notional)
    if largest <= cap:
        return accepted
    scale = cap / largest
    capped_spot = (spot_notional * scale).quantize(
        MARGIN_QUANTUM, rounding=ROUND_DOWN)
    capped_short = (short_notional * scale).quantize(
        MARGIN_QUANTUM, rounding=ROUND_DOWN)
    if capped_spot <= 0 or capped_short <= 0:
        raise PositionError("first-trade notional cap is below executable size")
    payload = dict(accepted.payload)
    payload["spot_quote_cap"] = format(capped_spot, "f")
    payload["short_notional"] = format(capped_short, "f")
    return dataclasses.replace(accepted, payload=payload)


def _entry_notional_cap(receipt_dir: str) -> Optional[Decimal]:
    marker = os.path.join(receipt_dir, FIRST_AUTONOMOUS_CLOSE_MARKER)
    return None if os.path.isfile(marker) else FIRST_TRADE_CAP_USDT


def _call_capped_open(cfg: PositionConfig, request, sleep, now_ms,
                      proposal_id: str,
                      cap: Optional[Decimal]) -> int:
    """Call cmd_open with a per-call accepted-proposal view capped in memory."""
    if cap is None:
        return cmd_open(
            cfg, request, sleep=sleep, now_ms=now_ms,
            proposal_id=proposal_id)
    # cmd_open has no notional argument.  Serialize the narrow dependency swap
    # so its loader sees a capped copy; the immutable proposal and digest stay
    # unchanged, and the original loader is restored on every outcome.
    with _ENTRY_OPEN_LOCK:
        original_loader = basis_position.load_entry_proposal

        def capped_loader(*args, **kwargs):
            accepted = original_loader(*args, **kwargs)
            return _clamp_accepted_proposal(accepted, cap)

        basis_position.load_entry_proposal = capped_loader
        try:
            return cmd_open(
                cfg, request, sleep=sleep, now_ms=now_ms,
                proposal_id=proposal_id)
        finally:
            basis_position.load_entry_proposal = original_loader


def _basis_entry_job(candidate: ProposalCandidate, requested_by: str,
                     cap: Optional[Decimal]) -> Job:
    requested = candidate.requested_notional
    effective = min(requested, cap) if cap is not None else requested
    job_id = _basis_entry_job_id(
        candidate.proposal_id, candidate.digest)
    return Job(
        job_id=job_id,
        op="BASIS_ROUNDTRIP",
        symbol=candidate.symbol,
        requested_max_notional=requested,
        effective_notional=effective,
        created_utc=datetime.fromtimestamp(
            candidate.created_ms / 1000, timezone.utc),
        expires_utc=datetime.fromtimestamp(
            candidate.expires_ms / 1000, timezone.utc),
        requested_by=requested_by,
    )


def _basis_entry_job_id(proposal_id: str, digest: str) -> str:
    if JOB_ID_RE.fullmatch(proposal_id) is not None:
        return proposal_id
    return "basis-" + digest[:58]


def _runner_entry_outcome(open_code: int, opened: Optional[dict],
                          cfg: PositionConfig) -> Optional[RunnerOutcome]:
    """Translate position truth to the runner's shared terminal vocabulary."""
    if opened is not None:
        state = opened.get("state")
        if state == ST_OPEN:
            verdict = "OPEN"
        elif state == ST_ONE_LEG:
            verdict = "OPEN_EXPOSURE"
        elif state in (ST_OPENING, ST_CLOSING, ST_UNKNOWN):
            verdict = "UNKNOWN"
        elif state in (ST_NONE, ST_CLOSED):
            verdict = "REFUSED"
        else:
            return None
        return RunnerOutcome(verdict, open_code, "")
    if _position_file_exists(cfg):
        return None
    if open_code in {
            POSITION_EXIT_CODE["REFUSED"],
            POSITION_EXIT_CODE["BLOCKED"],
            POSITION_EXIT_CODE["PREFLIGHT_REFUSED"]}:
        return RunnerOutcome("REFUSED", open_code, "")
    return None


def _entry_chain_refusal_reason(cfg: PositionConfig, proposal_id: str,
                                fallback: str) -> str:
    """Read back the proposal-bound position command's own verdict note."""
    rows = []
    try:
        names = os.listdir(cfg.receipt_dir)
    except OSError:
        return fallback
    for name in names:
        if not (name.startswith("basis-position-")
                and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(cfg.receipt_dir, name),
                      encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, TypeError, ValueError, UnicodeError):
            continue
        if not isinstance(payload, dict) \
                or payload.get("proposal_id") != proposal_id:
            continue
        note = payload.get("verdict_note")
        finished = payload.get("finished_utc")
        if isinstance(note, str) and note:
            rows.append((finished if isinstance(finished, str) else "", note))
    return max(rows)[1] if rows else fallback


def _load_candidate(cfg: PositionConfig, proposal_id: str,
                    tick_ms: int) -> ProposalCandidate:
    """Validate one proposal using the symbol declared by that proposal."""
    path = os.path.join(cfg.receipt_dir, PROPOSAL_DIR,
                        "proposal-%s.json" % proposal_id)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, ValueError):
        raise PositionError("proposal %s is unreadable" % proposal_id) \
            from None
    if not isinstance(payload, dict):
        raise PositionError(
            "proposal %s is unreadable: JSON object required" % proposal_id)
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise PositionError("proposal required field symbol is missing")
    if SYMBOL_RE.fullmatch(symbol) is None:
        raise PositionError("proposal symbol is invalid")
    accepted = load_entry_proposal(
        cfg.receipt_dir, proposal_id, symbol, cfg.sha, tick_ms)
    spot_notional, short_notional = _accepted_notionals(accepted)
    return ProposalCandidate(
        proposal_id=proposal_id,
        symbol=accepted.payload["symbol"],
        created_ms=_proposal_ms(accepted.payload, "created_utc"),
        expires_ms=_proposal_ms(accepted.payload, "expires_utc"),
        net_edge_pct=accepted.net_edge_pct,
        requested_notional=max(spot_notional, short_notional),
        digest=accepted.digest)


def _scan_newest_valid(cfg: PositionConfig, tick_ms: int,
                       rejects: Optional[Dict[str, int]] = None
                       ) -> Optional[ProposalCandidate]:
    """Validate all proposals; select best edge, then newest, in whitelist."""
    directory = os.path.join(cfg.receipt_dir, PROPOSAL_DIR)
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return None
    except OSError as error:
        _row("SCAN", "FROZEN", "proposal directory unreadable: %s"
             % type(error).__name__)
        raise

    valid: List[ProposalCandidate] = []
    for name in names:
        match = PROPOSAL_NAME_RE.fullmatch(name)
        if match is None:
            continue
        proposal_id = match.group(1)
        if PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
            if rejects is not None:
                _count_reject(rejects, "proposal_id_invalid")
            _row("PROPOSAL", "SKIPPED", "%s has an invalid id" % name)
            continue
        try:
            candidate = _load_candidate(cfg, proposal_id, tick_ms)
            if cfg.symbol and candidate.symbol != cfg.symbol:
                if rejects is not None:
                    _count_reject(rejects, "proposal_symbol_filtered")
                _row("PROPOSAL", "FILTERED", "%s symbol %s"
                     % (proposal_id, candidate.symbol))
                continue
            valid.append(candidate)
        except (KeyError, TypeError, ValueError, PositionError) as error:
            if rejects is not None:
                reason = "proposal_expired" if "expired" in str(error) \
                    else "proposal_invalid"
                _count_reject(rejects, reason)
            _row("PROPOSAL", "SKIPPED", "%s: %s"
                 % (proposal_id, _text(error, 220)))
    if not valid:
        return None
    return max(valid, key=lambda item: (
        item.net_edge_pct, item.created_ms, item.proposal_id))


def _make_pending_record(config: LadderModeConfig,
                         candidate: ProposalCandidate,
                         intent: basis_ladder.Intent,
                         effective_cap: Decimal,
                         event: DislocationEvent,
                         created_ms: int,
                         schedule_index: int) -> PendingRungRecord:
    if intent.kind != basis_ladder.ADD_RUNG \
            or not isinstance(intent.size, Decimal) \
            or not isinstance(intent.threshold, Decimal) \
            or not isinstance(intent.scheduled_size, Decimal) \
            or not isinstance(intent.gap_at_entry, Decimal):
        raise PositionError("pending rung intent is invalid")
    provisional = PendingRungRecord(
        version=PENDING_RUNG_VERSION,
        verdict=PENDING_RUNG_VERDICT,
        proposal_id=candidate.proposal_id,
        proposal_digest=candidate.digest,
        intent_size=intent.size,
        effective_cap=effective_cap,
        event_digest=event_digest(event),
        event_utc=event.utc,
        event=event,
        direction=config.direction,
        symbol=config.symbol,
        created_ms=created_ms,
        config_digest=_ladder_config_digest(config),
        schedule_index=schedule_index,
        threshold=intent.threshold,
        scheduled_size=intent.scheduled_size,
        gap_at_entry=intent.gap_at_entry,
        record_digest="",
    )
    digest = _pending_digest(_pending_payload(provisional))
    record = dataclasses.replace(provisional, record_digest=digest)
    if _pending_shape_reason(record) is not None:
        raise PositionError("pending rung record construction failed")
    return record


def _ladder_candidate(
        config: LadderModeConfig,
        event: Optional[DislocationEvent],
        pending: Optional[PendingRungRecord],
        position: Optional[dict],
        cfg: PositionConfig,
        tick_ms: int,
        rejects: Dict[str, int],
) -> tuple[Optional[ProposalCandidate],
           Optional[PendingRungRecord], bool]:
    """Return a gated candidate, complete pending record, and fresh flag."""
    gate = _apply_ladder_projection(
        config, pending, event, position, rejects, now_ms=tick_ms)
    if gate.outcome != RUNG_GATE_PASS:
        return None, pending, False
    if cfg.symbol and cfg.symbol != config.symbol:
        _ladder_refusal("DISLOCATION_EVENT_SYMBOL_FILTERED", rejects)
        return None, pending, False

    if pending is not None:
        try:
            candidate = _load_candidate(cfg, pending.proposal_id, tick_ms)
        except (KeyError, TypeError, ValueError, PositionError) as error:
            _ladder_refusal(
                "PENDING_PROPOSAL_INVALID: %s" % _text(error, 180),
                rejects, outcome=RUNG_GATE_FROZEN)
            return None, pending, False
        if candidate.symbol != config.symbol \
                or not hmac.compare_digest(
                    candidate.digest, pending.proposal_digest):
            _ladder_refusal(
                "PENDING_PROPOSAL_DIGEST_MISMATCH", rejects,
                outcome=RUNG_GATE_FROZEN)
            return None, pending, False
        return candidate, pending, False

    assert isinstance(event, DislocationEvent)
    assert gate.state is not None
    try:
        funding = basis_ladder.FundingContext(
            previous_rate=event.funding_rate - event.funding_rate_delta,
            current_rate=event.funding_rate,
        )
        result = basis_ladder.advance(
            gate.state,
            event.entry_gap_pct,
            event.exit_gap_pct,
            funding,
            config.engine,
            borrow_power=False,
        )
    except (ArithmeticError, TypeError, ValueError, basis_ladder.LadderError):
        _ladder_refusal("DISLOCATION_EVENT_INVALID", rejects)
        return None, None, False
    intent = result.intent
    if intent.kind != basis_ladder.ADD_RUNG or intent.size is None:
        _ladder_refusal(intent.reason, rejects)
        return None, None, False

    event_cfg = dataclasses.replace(cfg, symbol=config.symbol)
    candidate = _scan_newest_valid(event_cfg, tick_ms, rejects)
    if candidate is None:
        _ladder_refusal(LADDER_PROPOSAL_UNAVAILABLE, rejects)
        return None, None, False
    first_cap = _entry_notional_cap(cfg.receipt_dir)
    effective_cap = intent.size if first_cap is None \
        else min(intent.size, first_cap)
    try:
        record = _make_pending_record(
            config, candidate, intent, effective_cap, event, tick_ms,
            len(gate.state.rungs))
    except (ArithmeticError, TypeError, ValueError, PositionError) as error:
        _ladder_refusal(
            "%s: %s" % (PENDING_RUNG_INVALID, _text(error, 160)),
            rejects, outcome=RUNG_GATE_FROZEN)
        return None, None, False
    pending_gate = _apply_ladder_projection(
        config, record, event, position, rejects, now_ms=tick_ms)
    if pending_gate.outcome != RUNG_GATE_PASS:
        return None, None, False
    _row(
        "LADDER", "PROPOSED",
        "ADD_RUNG proposal %s symbol %s size %s reason %s"
        % (candidate.proposal_id, candidate.symbol, intent.size,
           intent.reason),
    )
    return candidate, record, True


def _claim(candidate: ProposalCandidate, cfg: PositionConfig, claimant: str,
           claimed_ms: int) -> bool:
    """Create the proposal claim exactly once; an existing inode owns it."""
    path = os.path.join(cfg.receipt_dir, PROPOSAL_DIR,
                        "proposal-%s.claim" % candidate.proposal_id)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    payload = (json.dumps({
        "claimant": _text(claimant, 160),
        "utc": _utc_from_ms(claimed_ms),
    }, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        # The claim is durable before cmd_open starts.  A process can die in
        # that exact gap, leaving no position, lane lock, or position-command
        # receipt to resume.  The same supervisor identity may adopt only
        # that provably unstarted claim; every attempted/foreign claim remains
        # immutable contention and is never replayed.
        try:
            with open(path, encoding="utf-8") as handle:
                prior_claim = json.load(handle)
        except (OSError, ValueError, UnicodeError):
            prior_claim = None
        attempted = False
        try:
            names = os.listdir(cfg.receipt_dir)
        except OSError:
            names = []
            attempted = True
        for name in names:
            if not (name.startswith("basis-position-")
                    and name.endswith(".json")):
                continue
            try:
                with open(os.path.join(cfg.receipt_dir, name),
                          encoding="utf-8") as handle:
                    receipt = json.load(handle)
            except (OSError, ValueError, UnicodeError):
                attempted = True
                break
            if isinstance(receipt, dict) and receipt.get("proposal_id") \
                    == candidate.proposal_id:
                attempted = True
                break
        try:
            position = load_position(cfg.receipt_dir)
        except PositionError:
            position = None
            attempted = True
        if isinstance(position, dict) and position.get("proposal_id") \
                == candidate.proposal_id:
            attempted = True
        same_unstarted_owner = isinstance(prior_claim, dict) \
            and prior_claim.get("claimant") == _text(claimant, 160) \
            and isinstance(prior_claim.get("utc"), str) \
            and not attempted \
            and not os.path.exists(os.path.join(cfg.receipt_dir, LOCK_FILE))
        if same_unstarted_owner:
            _row("CLAIM", "RECOVERED", "proposal %s claimant %s"
                 % (candidate.proposal_id, _text(claimant, 80)))
            return True
        _row("CLAIM", "SKIPPED", "proposal %s already owned"
             % candidate.proposal_id)
        return False
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("claim write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    _row("CLAIM", "CREATED", "proposal %s claimant %s"
         % (candidate.proposal_id, _text(claimant, 80)))
    return True


def _claim_governor_token(cfg: PositionConfig,
                          proposal_id: str) -> str:
    if not isinstance(proposal_id, str) \
            or PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
        return ""
    path = os.path.join(cfg.receipt_dir, PROPOSAL_DIR,
                        "proposal-%s.claim" % proposal_id)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, TypeError, ValueError, UnicodeError):
        return ""
    token = payload.get("governor_token") \
        if isinstance(payload, dict) else None
    return token if isinstance(token, str) else ""


def _write_claim_governor_token(cfg: PositionConfig, proposal_id: str,
                                claimant: Optional[str], token: str) -> bool:
    """Persist or clear the recovery token inside the owned proposal claim."""
    if not isinstance(proposal_id, str) \
            or PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
        return False
    path = os.path.join(cfg.receipt_dir, PROPOSAL_DIR,
                        "proposal-%s.claim" % proposal_id)
    temporary = ""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or (
                claimant is not None
                and payload.get("claimant") != _text(claimant, 160)):
            return False
        if token:
            payload["governor_token"] = token
        else:
            payload.pop("governor_token", None)
        encoded = (json.dumps(
            payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        temporary = "%s.tmp-governor-%s-%s" % (
            path, os.getpid(), threading.get_ident())
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("claim write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    except (OSError, TypeError, ValueError, UnicodeError):
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        return False


def _load_after(action: str, cfg: PositionConfig) -> Optional[dict]:
    try:
        return load_position(cfg.receipt_dir)
    except PositionError as error:
        _row(action, "FROZEN", error)
        return None


def _reconciled_position_decimal(value: Any, label: str) -> Decimal:
    if not isinstance(value, str) or not value:
        raise PositionError("reconciled %s is missing" % label)
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise PositionError("reconciled %s is invalid" % label) from None
    if not parsed.is_finite() or parsed <= 0:
        raise PositionError("reconciled %s is not positive" % label)
    return parsed


def _reconciled_own_entry_quote(position: dict, entry: dict,
                                pending: PendingRungRecord, *,
                                stage: str, market: str, side: str,
                                quantity_field: str,
                                quote_field: str) -> Decimal:
    """Bind one entry fill to its deterministic terminal position identity."""
    client_ids = position.get("client_ids")
    legs = position.get("legs")
    reconcile = position.get("reconcile")
    truths = reconcile.get("truths") if isinstance(reconcile, dict) else None
    if not isinstance(client_ids, dict) or not isinstance(legs, list) \
            or not isinstance(truths, dict):
        raise PositionError("reconciled own-order evidence is missing")

    run_id = position.get("open_run_id")
    if not isinstance(run_id, str) or not run_id:
        raise PositionError("reconciled open run identity is invalid")
    expected_client = position_client_id(stage, run_id)
    meta = client_ids.get(expected_client)
    if not isinstance(meta, dict) \
            or meta.get("stage") != stage \
            or meta.get("market") != market \
            or meta.get("run_id") != run_id:
        raise PositionError("reconciled own %s order identity is invalid" % side)
    stage_clients = [
        client for client, item in client_ids.items()
        if isinstance(item, dict)
        and item.get("stage") == stage
        and item.get("market") == market
    ]
    if stage_clients != [expected_client] \
            or truths.get(expected_client) != "terminal":
        raise PositionError(
            "reconciled own %s fill is not terminal" % side)

    own_legs = [
        leg for leg in legs
        if isinstance(leg, dict)
        and leg.get("client_id") == expected_client
        and leg.get("stage") == stage
        and leg.get("market") == market
    ]
    if len(own_legs) != 1 \
            or own_legs[0].get("outcome") not in ("terminal", "unknown"):
        raise PositionError("reconciled own %s fill record is invalid" % side)
    side_entry = entry.get(side)
    leg = own_legs[0]
    if isinstance(side_entry, dict):
        raw_quantity = side_entry.get(quantity_field)
        raw_quote = side_entry.get(quote_field)
    else:
        # A pre-resume UNKNOWN leg may retain exact observed quantities in its
        # position receipt.  Resume terminality for that same deterministic id
        # promotes those facts without requiring the old outcome label to move.
        raw_quantity = leg.get("executed_qty")
        raw_quote = leg.get("quote")

    entry_quantity = _reconciled_position_decimal(
        raw_quantity, "%s entry quantity" % side)
    entry_quote = _reconciled_position_decimal(
        raw_quote, "%s entry quote" % side)
    leg_quantity = _reconciled_position_decimal(
        leg.get("executed_qty"), "%s own-order quantity" % side)
    leg_quote = _reconciled_position_decimal(
        leg.get("quote"), "%s own-order quote" % side)
    if entry_quantity != leg_quantity or entry_quote != leg_quote:
        raise PositionError(
            "reconciled %s entry fill does not match its own order" % side)
    return entry_quote


def _position_rung_values(position: dict) -> tuple[Decimal, Decimal]:
    """Read conservative fill notional and entry gap without granting credit."""
    entry = position.get("entry")
    legs = position.get("legs")
    if not isinstance(entry, dict) or not isinstance(legs, list):
        raise PositionError("reconciled entry fill truth is missing")
    spot = entry.get("spot")
    short = entry.get("short")
    spot_quote = _reconciled_position_decimal(
        spot.get("quote_spent") if isinstance(spot, dict) else None,
        "spot entry quote")
    if isinstance(short, dict):
        short_raw = short.get("cum_quote")
    else:
        short_rows = [
            leg for leg in legs
            if isinstance(leg, dict)
            and leg.get("stage") == basis_position.ENTER_SHORT
            and leg.get("market") == "usdm"
        ]
        short_raw = short_rows[0].get("quote") \
            if len(short_rows) == 1 else None
    short_quote = _reconciled_position_decimal(
        short_raw, "short entry quote")
    gap = _reconciled_position_decimal(
        entry.get("entry_gap_pct"), "entry gap")
    return max(spot_quote, short_quote), gap


def _reconciled_rung_notional(position: dict,
                               pending: PendingRungRecord) -> Decimal:
    """Project proven own-leg fill truth into one held rung notional."""
    if position.get("proposal_id") != pending.proposal_id \
            or position.get("proposal_digest") != pending.proposal_digest:
        raise PositionError(
            "reconciled position is not bound to the pending proposal")
    entry = position.get("entry")
    if not isinstance(entry, dict):
        raise PositionError("reconciled entry fill truth is missing")
    spot_quote = _reconciled_own_entry_quote(
        position, entry, pending,
        stage=basis_position.ENTER_SPOT, market="spot",
        side="spot", quantity_field="executed_qty",
        quote_field="quote_spent")
    short_quote = _reconciled_own_entry_quote(
        position, entry, pending,
        stage=basis_position.ENTER_SHORT, market="usdm",
        side="short", quantity_field="filled_qty", quote_field="cum_quote")
    return max(spot_quote, short_quote)


def _event_ms(event: DislocationEvent) -> int:
    value = datetime.fromisoformat(
        event.utc[:-1] + "+00:00" if event.utc.endswith("Z") else event.utc)
    return int(value.timestamp() * 1000)


def _gate_row(outcome: str, reason: str, *,
              state: Optional[basis_ladder.LadderState] = None,
              held_notional: Optional[Decimal] = None) -> RungGateRow:
    return RungGateRow(outcome, reason, state, held_notional)


def validate_rung_gate(
        config: Any,
        pending_record_or_none: Any,
        event_or_none: Any,
        position_or_none: Any,
        *, now_ms: Optional[int] = None) -> RungGateRow:
    """Validate config, direction, event, cap, then position provenance."""
    # 1. Immutable config and input shape.
    if not isinstance(config, LadderModeConfig):
        return _gate_row(RUNG_GATE_FROZEN, LADDER_SOURCE_INVALID)
    try:
        engine = config.engine
        config_digest = _ladder_config_digest(config)
    except (ArithmeticError, TypeError, ValueError, basis_ladder.LadderError):
        return _gate_row(RUNG_GATE_FROZEN, LADDER_SOURCE_INVALID)
    pending = pending_record_or_none
    if pending is not None:
        if _pending_shape_reason(pending) is not None:
            return _gate_row(RUNG_GATE_FROZEN, PENDING_RUNG_RECORD_INVALID)
        if pending.config_digest != config_digest \
                or pending.symbol != config.symbol \
                or pending.direction != config.direction:
            return _gate_row(
                RUNG_GATE_FROZEN, PENDING_RUNG_CONFIG_MISMATCH)
    if position_or_none is not None and not isinstance(position_or_none, dict):
        return _gate_row(RUNG_GATE_FROZEN, LADDER_FILL_INVALID)
    position = position_or_none
    if isinstance(position, dict) \
            and position.get("state") not in (None, ST_NONE, ST_CLOSED) \
            and pending is None:
        return _gate_row(RUNG_GATE_FROZEN, PENDING_RUNG_RECORD_MISSING)

    # 2. Direction. Reverse stays refused until its own execution is wired.
    if config.direction == basis_ladder.REVERSE:
        reason = LADDER_REVERSE_DISABLED if not config.reverse_enabled \
            else LADDER_REVERSE_UNWIRED
        return _gate_row(RUNG_GATE_REFUSED, reason)

    # 3. Event validity. A revisit revalidates the event sealed in the record
    # at its creation cut, so later wall-clock age cannot rewrite provenance.
    event = pending.event if pending is not None else event_or_none
    if pending is not None and event_or_none is not None:
        try:
            supplied_digest = event_digest(event_or_none)
        except (TypeError, ValueError):
            supplied_digest = ""
        if not hmac.compare_digest(pending.event_digest, supplied_digest):
            return _gate_row(RUNG_GATE_FROZEN, PENDING_RUNG_RECORD_INVALID)
    if pending is not None:
        event_now = pending.created_ms
    elif now_ms is not None:
        event_now = now_ms
    else:
        try:
            event_now = _event_ms(event)
        except (AttributeError, TypeError, ValueError):
            event_now = -1
    event_refusal = validate_actionable(event, event_now)
    if event_refusal is not None:
        outcome = RUNG_GATE_FROZEN if pending is not None \
            else RUNG_GATE_REFUSED
        return _gate_row(outcome, event_refusal)
    assert isinstance(event, DislocationEvent)
    if event.symbol != config.symbol:
        outcome = RUNG_GATE_FROZEN if pending is not None \
            else RUNG_GATE_REFUSED
        return _gate_row(outcome, "DISLOCATION_EVENT_SYMBOL_FILTERED")
    if pending is not None and pending.gap_at_entry != event.entry_gap_pct:
        return _gate_row(RUNG_GATE_FROZEN, PENDING_RUNG_RECORD_INVALID)

    # 4. Exact cap headroom from the current durable position projection.
    state = basis_ladder.LadderState.flat(config.direction)
    held_notional: Optional[Decimal] = None
    position_state = position.get("state") if position is not None else None
    if position_state == ST_OPEN:
        if pending is None:
            return _gate_row(RUNG_GATE_FROZEN, PENDING_RUNG_RECORD_MISSING)
        try:
            held_notional, held_gap = _position_rung_values(position)
            state = basis_ladder.LadderState(
                config.direction,
                (basis_ladder.Rung(held_gap, held_notional),),
            )
        except (ArithmeticError, TypeError, ValueError,
                basis_ladder.LadderError, PositionError):
            return _gate_row(RUNG_GATE_FROZEN, LADDER_FILL_INVALID)
        if held_notional > pending.effective_cap \
                or held_notional > pending.intent_size \
                or state.total_notional > config.max_total_notional \
                or len(state.rungs) > config.max_rungs:
            return _gate_row(
                RUNG_GATE_FROZEN, PENDING_RUNG_SIZE_OUT_OF_BOUNDS)
    elif position_state not in (None, ST_NONE, ST_CLOSED):
        return _gate_row(RUNG_GATE_FROZEN, LADDER_FILL_INVALID)

    if pending is not None:
        index = pending.schedule_index
        if index >= len(engine.schedule) or index >= engine.max_rungs:
            return _gate_row(
                RUNG_GATE_FROZEN, PENDING_RUNG_CONFIG_MISMATCH)
        level = engine.schedule[index]
        if pending.threshold != level.gap_threshold \
                or pending.scheduled_size != level.rung_size:
            return _gate_row(
                RUNG_GATE_FROZEN, PENDING_RUNG_CONFIG_MISMATCH)
        if position_state != ST_OPEN:
            remaining = engine.max_total_notional - state.total_notional
            expected = min(level.rung_size, remaining)
            if index != len(state.rungs) \
                    or remaining <= 0 or pending.intent_size != expected \
                    or pending.intent_size > remaining:
                return _gate_row(
                    RUNG_GATE_FROZEN, PENDING_RUNG_SIZE_OUT_OF_BOUNDS)

    # 5. Provenance. Credit requires the position's deterministic own ids and
    # proposal digest, with terminal truth from its latest resume receipt.
    if position_state == ST_OPEN:
        assert pending is not None and held_notional is not None
        try:
            proven_notional = _reconciled_rung_notional(position, pending)
        except Exception:
            return _gate_row(
                RUNG_GATE_FROZEN, PENDING_RUNG_PROVENANCE_INVALID)
        if proven_notional != held_notional:
            return _gate_row(
                RUNG_GATE_FROZEN, PENDING_RUNG_PROVENANCE_INVALID)

    return _gate_row(
        RUNG_GATE_PASS,
        "RUNG_GATE_PASS",
        state=state,
        held_notional=held_notional,
    )


def _report_rung_gate(row: RungGateRow,
                      rejects: Dict[str, int]) -> None:
    _row("RUNG-GATE", row.outcome, row.reason)
    if row.outcome != RUNG_GATE_PASS:
        _ladder_refusal(row.reason, rejects, outcome=row.outcome)


def _apply_ladder_projection(config: LadderModeConfig,
                             pending: Optional[PendingRungRecord],
                             event: Optional[DislocationEvent],
                             position: Optional[dict],
                             rejects: Dict[str, int], *,
                             now_ms: Optional[int] = None) -> RungGateRow:
    """Build held state fresh and report it; no supervisor state is mutated."""
    row = validate_rung_gate(
        config, pending, event, position, now_ms=now_ms)
    _report_rung_gate(row, rejects)
    if row.outcome == RUNG_GATE_PASS and pending is not None \
            and isinstance(position, dict) \
            and position.get("state") == ST_OPEN:
        _row(
            "LADDER", "RECONCILED",
            "RUNG_FILL_CONFIRMED proposal %s held_notional %s"
            % (pending.proposal_id, row.held_notional),
        )
    elif row.outcome == RUNG_GATE_PASS and pending is not None \
            and isinstance(position, dict) \
            and position.get("state") in (ST_NONE, ST_CLOSED) \
            and position.get("proposal_id") == pending.proposal_id:
        _row(
            "LADDER", "RECONCILED",
            "RUNG_EXIT_FILL_CONFIRMED proposal %s" % pending.proposal_id,
        )
    return row


def _position_file_exists(cfg: PositionConfig) -> bool:
    return os.path.exists(os.path.join(cfg.receipt_dir, STATE_FILE))


def _cfg_for_position(cfg: PositionConfig,
                      position: Optional[dict]) -> PositionConfig:
    """Bind commands to durable position identity, never the entry filter."""
    if position is None:
        return dataclasses.replace(cfg, symbol="")
    symbol = position.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise PositionError("basis-position.json symbol is missing or invalid")
    return dataclasses.replace(cfg, symbol=symbol)


def _reconcile(cfg: PositionConfig, request, sleep, now_ms,
               label: str = "RECONCILE") -> tuple[int, Optional[dict]]:
    code = cmd_resume(cfg, request, sleep=sleep, now_ms=now_ms)
    position = _load_after(label, cfg)
    state = position.get("state") if position else ST_NONE
    _row(label, "DONE" if code == 0 else "HELD",
         "position command exit %s state %s" % (code, state))
    return code, position


def _fresh_check_decision(before: dict, after: dict,
                          check_code: int) -> Optional[str]:
    if check_code != POSITION_EXIT_CODE["DECIDED"]:
        return None
    old_revision = before.get("revision")
    new_revision = after.get("revision")
    changed = isinstance(old_revision, int) and isinstance(new_revision, int) \
        and new_revision > old_revision
    if not changed and after.get("last_check") == before.get("last_check"):
        return None
    last_check = after.get("last_check")
    if not isinstance(last_check, dict):
        return None
    decision = last_check.get("decision")
    return decision if isinstance(decision, str) else None


def _default_claimant() -> str:
    return "pid-%s" % os.getpid()


def tick(cfg: PositionConfig, request, *, sleep: Callable[[float], None] = time.sleep,
         now_ms: Callable[[], int] = default_now_ms,
         claimant: Optional[str] = None,
         ladder_config: Optional[LadderModeConfig] = None,
         ladder_event: Optional[DislocationEvent] = None) -> int:
    """Run one cycle and append one non-authoritative health snapshot."""
    observed_times: List[int] = []
    rejects: Dict[str, int] = {}
    code: Optional[int] = None
    error_name: Optional[str] = None

    def tracked_now_ms() -> int:
        value = now_ms()
        observed_times.append(value)
        return value

    try:
        code = _tick(cfg, request, sleep=sleep, now_ms=tracked_now_ms,
                     claimant=claimant, rejects=rejects,
                     ladder_config=ladder_config,
                     ladder_event=ladder_event)
        return code
    except Exception as error:
        error_name = type(error).__name__
        _count_reject(rejects, "tick_error")
        raise
    finally:
        if code == EXIT_FROZEN and not rejects:
            _count_reject(rejects, "tick_frozen")
        if observed_times:
            health_now = observed_times[-1]
        else:
            try:
                health_now = now_ms()
            except Exception:
                health_now = default_now_ms()
        try:
            _append_tick_health(cfg, health_now, code, error_name, rejects)
        except Exception:
            pass


def _tick(cfg: PositionConfig, request, *,
          sleep: Callable[[float], None] = time.sleep,
          now_ms: Callable[[], int] = default_now_ms,
          claimant: Optional[str] = None,
          rejects: Optional[Dict[str, int]] = None,
          ladder_config: Optional[LadderModeConfig] = None,
          ladder_event: Optional[DislocationEvent] = None) -> int:
    """Run exactly one restart-safe supervisor cycle."""
    rejects = rejects if rejects is not None else {}
    if ladder_config is not None \
            and not isinstance(ladder_config, LadderModeConfig):
        _count_reject(rejects, "ladder_source_invalid")
        _row("LADDER", "FROZEN", LADDER_SOURCE_INVALID)
        return EXIT_FROZEN
    if ladder_config is None and ladder_event is not None:
        _count_reject(rejects, "ladder_source_invalid")
        _row("LADDER", "FROZEN", LADDER_SOURCE_INVALID)
        return EXIT_FROZEN
    operator = _load_operator(_paths(cfg.receipt_dir)["operator"])
    claimant = claimant or _default_claimant()
    ladder_pending: Optional[PendingRungRecord] = None
    pending_load_error: Optional[str] = None
    if ladder_config is not None:
        try:
            ladder_pending = _load_pending_record(cfg.receipt_dir)
        except PositionError as error:
            pending_load_error = "%s: %s" % (
                PENDING_RUNG_RECORD_INVALID, _text(error, 160))

    # Reconcile before every decision whenever position state (or its shared
    # ownership lock) exists.  A corrupt state also goes through cmd_resume so
    # the position machinery, not this driver, owns the refusal.
    position: Optional[dict]
    try:
        position = load_position(cfg.receipt_dir)
    except PositionError as error:
        _row("TICK", "FROZEN", error)
        return EXIT_FROZEN
    has_recovery_artifact = _position_file_exists(cfg) or os.path.exists(
        os.path.join(cfg.receipt_dir, LOCK_FILE))
    ladder_gate_blocked = False
    if has_recovery_artifact:
        try:
            position_cfg = _cfg_for_position(cfg, position)
        except PositionError as error:
            _row("TICK", "FROZEN", error)
            return EXIT_FROZEN
        resume_code, position = _reconcile(
            position_cfg, request, sleep, now_ms)
        state = position.get("state") if position else None
        lock_stands = os.path.exists(os.path.join(cfg.receipt_dir, LOCK_FILE))
        ownership_inconsistent = (state == ST_OPEN and not lock_stands) \
            or (state in (ST_NONE, ST_CLOSED) and lock_stands)
        proposal_id = position.get("proposal_id") if position else None
        recovered_token = _claim_governor_token(cfg, proposal_id) \
            if isinstance(proposal_id, str) else ""
        acceptance_claimed = True
        if recovered_token:
            entry = position.get("entry") if position else None
            consumes_ticket = state == ST_ONE_LEG or (
                state in (ST_OPEN, ST_CLOSED) and isinstance(entry, dict))
            if consumes_ticket:
                digest = position.get("proposal_digest")
                if not isinstance(digest, str):
                    acceptance_claimed = False
                else:
                    job_id = _basis_entry_job_id(proposal_id, digest)
                    accept_path = os.path.join(
                        cfg.receipt_dir, ACCEPT_ONE_FILE)
                    claimed_path = accept_path + ".claimed-" + job_id
                    already_claimed = os.path.isfile(claimed_path) \
                        and not os.path.islink(claimed_path)
                    acceptance_claimed = already_claimed \
                        or _consume_accept_one(accept_path, job_id)
                if not acceptance_claimed:
                    _touch_stop(os.path.join(
                        cfg.receipt_dir, RUNNER_STOP_FILE))
                    _count_reject(rejects, "accept_one_claim_failed")
                    _row("ENTRY", "FROZEN", "ACCEPT_ONE_CLAIM_FAILED")
        if ladder_config is not None:
            if pending_load_error is not None:
                gate = _gate_row(RUNG_GATE_FROZEN, pending_load_error)
                _report_rung_gate(gate, rejects)
            else:
                gate = _apply_ladder_projection(
                    ladder_config, ladder_pending, ladder_event, position,
                    rejects)
            ladder_gate_blocked = gate.outcome != RUNG_GATE_PASS
        if resume_code != 0 or state in FREEZE_STATES or position is None:
            _row("TICK", "FROZEN",
                 "reconciliation is the only action; state %s"
                 % (state or "UNREADABLE"))
            return EXIT_FROZEN
        if ownership_inconsistent:
            _row("TICK", "FROZEN",
                 "reconciliation is the only action; state/lock ownership "
                 "is inconsistent")
            return EXIT_FROZEN
        if recovered_token:
            if acceptance_claimed and _release_recovered_governor(
                    recovered_token, cfg.receipt_dir):
                if not _write_claim_governor_token(
                        cfg, proposal_id, None, ""):
                    _touch_stop(os.path.join(
                        cfg.receipt_dir, RUNNER_STOP_FILE))
                    _count_reject(rejects, "governor_token_clear_failed")
                    _row("GOVERNOR", "HELD",
                         "recovered release cleared account lock but not "
                         "claim token; STOP-JOBS written")
            elif acceptance_claimed:
                _touch_stop(os.path.join(cfg.receipt_dir, RUNNER_STOP_FILE))
                _count_reject(rejects, "governor_release_failed")
                _row("GOVERNOR", "HELD",
                     "recovered release failed; STOP-JOBS written")
        if ladder_gate_blocked and state != ST_OPEN:
            return EXIT_FROZEN

    if position is not None and position.get("state") == ST_OPEN:
        try:
            position_cfg = _cfg_for_position(cfg, position)
        except PositionError as error:
            _row("TICK", "FROZEN", error)
            return EXIT_FROZEN
        before = position
        prior_check = before.get("last_check")
        prior_decision = prior_check.get("decision") \
            if isinstance(prior_check, dict) else None
        emergency_before = os.path.exists(os.path.join(
            cfg.receipt_dir, EMERGENCY_FILE))
        check_code = cmd_check(
            position_cfg, request, sleep=sleep, now_ms=now_ms)
        after = _load_after("CHECK", position_cfg)
        if after is None:
            return EXIT_FROZEN
        decision = _fresh_check_decision(before, after, check_code)
        emergency = emergency_before or os.path.exists(os.path.join(
            cfg.receipt_dir, EMERGENCY_FILE))
        _row("CHECK", "DONE" if check_code == 0 else "FROZEN",
             "position command exit %s decision %s"
             % (check_code, decision or
                (REQUEST_EMERGENCY_UNWIND if emergency else "UNPROVEN")))
        close_reason = REQUEST_EMERGENCY_UNWIND if emergency else (
            decision if decision in CLOSE_DECISIONS else
            prior_decision if prior_decision in CLOSE_DECISIONS else None)
        if check_code != POSITION_EXIT_CODE["DECIDED"] \
                and close_reason is None:
            return EXIT_FROZEN
        if after.get("state") != ST_OPEN:
            return EXIT_FROZEN
        if close_reason is not None:
            close_code = cmd_close(
                position_cfg, request, sleep=sleep, now_ms=now_ms)
            _row("CLOSE", "AUTOMATIC",
                 "%s; position command exit %s"
                 % (close_reason, close_code))
            resume_code, final_position = _reconcile(
                position_cfg, request, sleep, now_ms,
                label="CLOSE-RECONCILE")
            final_state = final_position.get("state") \
                if final_position else None
            if resume_code != 0 or final_state in FREEZE_STATES \
                    or final_position is None:
                return EXIT_FROZEN
            if ladder_config is not None:
                try:
                    final_pending = _load_pending_record(cfg.receipt_dir)
                except PositionError as error:
                    final_gate = _gate_row(
                        RUNG_GATE_FROZEN,
                        "%s: %s" % (
                            PENDING_RUNG_RECORD_INVALID, _text(error, 160)))
                    _report_rung_gate(final_gate, rejects)
                else:
                    _apply_ladder_projection(
                        ladder_config, final_pending, ladder_event,
                        final_position, rejects)
            return EXIT_ACTED
        if decision == FREEZE_NEW_ACTIONS:
            return EXIT_FROZEN
        if decision is None:
            _row("TICK", "FROZEN", "check decision was not persisted")
            return EXIT_FROZEN
        _row("TICK", "HELD", "OPEN position remains within policy")
        return EXIT_FROZEN if ladder_gate_blocked else EXIT_HELD

    # CLOSED/NONE state does not stand.  STOP gates only entry; reconciliation,
    # checks, and exits above never consult it.
    for seal_name in (STOP_FILE, RUNNER_STOP_FILE):
        if os.path.exists(os.path.join(cfg.receipt_dir, seal_name)):
            _row("ENTRY", "SKIPPED", "%s file present" % seal_name)
            if ladder_config is not None:
                _ladder_refusal("%s file present" % seal_name, rejects)
            return EXIT_IDLE

    if ladder_config is not None and pending_load_error is None \
            and ladder_pending is None \
            and _proposal_claim_exists(cfg.receipt_dir):
        gate = _gate_row(RUNG_GATE_FROZEN, PENDING_RUNG_RECORD_MISSING)
        _report_rung_gate(gate, rejects)
        return EXIT_FROZEN

    tick_ms = now_ms()
    candidate_pending: Optional[PendingRungRecord] = None
    fresh_pending = False
    if pending_load_error is not None:
        gate = _gate_row(RUNG_GATE_FROZEN, pending_load_error)
        _report_rung_gate(gate, rejects)
        return EXIT_FROZEN
    active_pending = ladder_pending
    try:
        if ladder_config is None:
            candidate = _scan_newest_valid(cfg, tick_ms, rejects)
        else:
            if ladder_event is not None \
                    and isinstance(position, dict) \
                    and position.get("state") in (ST_NONE, ST_CLOSED) \
                    and ladder_pending is not None \
                    and position.get("proposal_id") \
                    == ladder_pending.proposal_id:
                # The position receipt proves the prior rung is no longer held.
                # A fresh event may now build a new atomic pending transition.
                active_pending = None
            candidate, candidate_pending, fresh_pending = _ladder_candidate(
                ladder_config, ladder_event, active_pending, position,
                cfg, tick_ms, rejects)
    except OSError:
        _count_reject(rejects, "proposal_scan_error")
        return EXIT_FROZEN
    if candidate is None:
        _row("ENTRY", "IDLE", "no valid ladder proposal"
             if ladder_config is not None else "no valid proposal")
        return EXIT_FROZEN if active_pending is not None else EXIT_IDLE
    if operator is None or not operator.strip() or len(operator) > 128:
        _count_reject(rejects, "operator_mismatch")
        _row("ENTRY", "HELD", "OPERATOR_MISMATCH")
        if ladder_config is not None:
            _ladder_refusal("OPERATOR_MISMATCH", rejects)
        return EXIT_HELD
    claim_path = os.path.join(
        cfg.receipt_dir, PROPOSAL_DIR,
        "proposal-%s.claim" % candidate.proposal_id)
    claim_preexists = os.path.exists(claim_path)
    if ladder_config is not None:
        if candidate_pending is None:
            _ladder_refusal(
                PENDING_RUNG_RECORD_MISSING, rejects,
                outcome=RUNG_GATE_FROZEN)
            return EXIT_FROZEN
        if fresh_pending and claim_preexists:
            _ladder_refusal(
                PENDING_RUNG_RECORD_MISSING, rejects,
                outcome=RUNG_GATE_FROZEN)
            return EXIT_FROZEN
        if not fresh_pending and not claim_preexists:
            _ladder_refusal(
                PENDING_RUNG_PROVENANCE_INVALID, rejects,
                outcome=RUNG_GATE_FROZEN)
            return EXIT_FROZEN
    try:
        claimed = _claim(candidate, cfg, claimant, tick_ms)
    except OSError as error:
        _count_reject(rejects, "claim_error")
        _row("CLAIM", "FROZEN", "%s: %s"
             % (candidate.proposal_id, type(error).__name__))
        if ladder_config is not None:
            _ladder_refusal(
                "CLAIM_ERROR: %s" % type(error).__name__, rejects,
                outcome=RUNG_GATE_FROZEN)
        return EXIT_FROZEN
    if not claimed:
        _count_reject(rejects, "claim_owned")
        if ladder_config is not None:
            _ladder_refusal("PROPOSAL_CLAIM_OWNED", rejects)
        return EXIT_IDLE

    # The claim owns the proposal id.  Re-read the immutable proposal after
    # that atomic boundary and bind the position machine to its symbol.
    try:
        claimed_candidate = _load_candidate(
            cfg, candidate.proposal_id, tick_ms)
    except (KeyError, TypeError, ValueError, PositionError) as error:
        _count_reject(rejects, "claimed_proposal_invalid")
        _row("CLAIM", "FROZEN", "%s: %s"
             % (candidate.proposal_id, _text(error, 220)))
        if ladder_config is not None:
            _ladder_refusal(
                "CLAIMED_PROPOSAL_INVALID: %s" % _text(error, 180),
                rejects, outcome=RUNG_GATE_FROZEN)
        return EXIT_FROZEN
    if claimed_candidate.digest != candidate.digest:
        _count_reject(rejects, "claimed_proposal_changed")
        _row("CLAIM", "FROZEN", "proposal %s changed during claim"
             % candidate.proposal_id)
        if ladder_config is not None:
            _ladder_refusal(
                "CLAIMED_PROPOSAL_CHANGED", rejects,
                outcome=RUNG_GATE_FROZEN)
        return EXIT_FROZEN
    if cfg.symbol and claimed_candidate.symbol != cfg.symbol:
        _count_reject(rejects, "proposal_symbol_filtered")
        _row("CLAIM", "FROZEN", "proposal %s escaped symbol whitelist"
             % candidate.proposal_id)
        if ladder_config is not None:
            _ladder_refusal(
                "CLAIMED_PROPOSAL_SYMBOL_FILTERED", rejects,
                outcome=RUNG_GATE_FROZEN)
        return EXIT_FROZEN

    if ladder_config is not None:
        assert candidate_pending is not None
        if not hmac.compare_digest(
                claimed_candidate.digest,
                candidate_pending.proposal_digest):
            _ladder_refusal(
                "CLAIMED_PROPOSAL_CHANGED", rejects,
                outcome=RUNG_GATE_FROZEN)
            return EXIT_FROZEN
        try:
            if fresh_pending:
                _write_pending_record(cfg.receipt_dir, candidate_pending)
            persisted_pending = _load_pending_record(cfg.receipt_dir)
        except PositionError as error:
            _ladder_refusal(
                "%s: %s" % (
                    PENDING_RUNG_RECORD_INVALID, _text(error, 160)),
                rejects, outcome=RUNG_GATE_FROZEN)
            return EXIT_FROZEN
        if persisted_pending is None \
                or not hmac.compare_digest(
                    persisted_pending.record_digest,
                    candidate_pending.record_digest):
            _ladder_refusal(
                PENDING_RUNG_RECORD_INVALID, rejects,
                outcome=RUNG_GATE_FROZEN)
            return EXIT_FROZEN
        candidate_pending = persisted_pending
        persisted_gate = _apply_ladder_projection(
            ladder_config, candidate_pending, ladder_event, position,
            rejects, now_ms=tick_ms)
        if persisted_gate.outcome != RUNG_GATE_PASS:
            return EXIT_FROZEN

    recovered_token = _claim_governor_token(cfg, candidate.proposal_id)
    if recovered_token:
        released = _release_recovered_governor(
            recovered_token, cfg.receipt_dir)
        cleared = released and _write_claim_governor_token(
            cfg, candidate.proposal_id, claimant, "")
        if not released or not cleared:
            _touch_stop(os.path.join(cfg.receipt_dir, RUNNER_STOP_FILE))
            reason = "governor_release_failed" if not released \
                else "governor_token_clear_failed"
            _count_reject(rejects, reason)
            _row("GOVERNOR", "HELD",
                 "unstarted claim recovery failed; STOP-JOBS written")
            if ladder_config is not None:
                _ladder_refusal(
                    reason.upper(), rejects, outcome=RUNG_GATE_FROZEN)
            return EXIT_FROZEN

    notional_cap = candidate_pending.effective_cap \
        if candidate_pending is not None \
        else _entry_notional_cap(cfg.receipt_dir)
    entry_job = _basis_entry_job(claimed_candidate, operator, notional_cap)
    ticket_refusal = _entry_ticket_refusal(
        entry_job, _paths(cfg.receipt_dir),
        datetime.fromtimestamp(tick_ms / 1000, timezone.utc))
    if ticket_refusal:
        reason_key = ticket_refusal.lower()
        _count_reject(rejects, reason_key)
        _row("ENTRY", "HELD", ticket_refusal)
        if ladder_config is not None:
            _ladder_refusal(ticket_refusal, rejects)
        return EXIT_HELD

    governor_token, release_governor, governor_refusal = \
        _acquire_governor(entry_job, cfg.receipt_dir)
    if governor_refusal:
        _count_reject(rejects, "governor_refused")
        _row("ENTRY", "HELD", governor_refusal)
        if ladder_config is not None:
            _ladder_refusal(governor_refusal, rejects)
        return EXIT_HELD
    if not _write_claim_governor_token(
            cfg, candidate.proposal_id, claimant, governor_token):
        released = _release_governor(
            release_governor, governor_token, cfg.receipt_dir)
        if not released:
            _touch_stop(os.path.join(cfg.receipt_dir, RUNNER_STOP_FILE))
        _count_reject(rejects, "governor_token_persist_failed")
        _row("ENTRY", "FROZEN", "GOVERNOR_TOKEN_PERSIST_FAILED")
        if ladder_config is not None:
            _ladder_refusal(
                "GOVERNOR_TOKEN_PERSIST_FAILED", rejects,
                outcome=RUNG_GATE_FROZEN)
        return EXIT_FROZEN

    if ladder_config is not None:
        try:
            final_pending = _load_pending_record(cfg.receipt_dir)
            latest_position = load_position(cfg.receipt_dir)
            latest_candidate = _load_candidate(
                cfg, candidate.proposal_id, tick_ms)
        except (KeyError, TypeError, ValueError, PositionError) as error:
            final_gate = _gate_row(
                RUNG_GATE_FROZEN,
                "%s: %s" % (
                    PENDING_RUNG_RECORD_INVALID, _text(error, 160)))
            _report_rung_gate(final_gate, rejects)
        else:
            if final_pending is None or candidate_pending is None \
                    or not hmac.compare_digest(
                        latest_candidate.digest,
                        candidate_pending.proposal_digest) \
                    or not hmac.compare_digest(
                        final_pending.record_digest,
                        candidate_pending.record_digest):
                final_gate = _gate_row(
                    RUNG_GATE_FROZEN, PENDING_RUNG_RECORD_INVALID)
                _report_rung_gate(final_gate, rejects)
            else:
                final_gate = _apply_ladder_projection(
                    ladder_config, final_pending, ladder_event,
                    latest_position, rejects, now_ms=tick_ms)
        if final_gate.outcome != RUNG_GATE_PASS:
            released = not governor_token or _release_governor(
                release_governor, governor_token, cfg.receipt_dir)
            cleared = released and _write_claim_governor_token(
                cfg, candidate.proposal_id, claimant, "")
            if not released or not cleared:
                _touch_stop(os.path.join(
                    cfg.receipt_dir, RUNNER_STOP_FILE))
            return EXIT_FROZEN

    proposal_cfg = dataclasses.replace(
        cfg, symbol=claimed_candidate.symbol)
    open_code = _call_capped_open(
        proposal_cfg, request, sleep, now_ms, candidate.proposal_id,
        notional_cap)
    opened = _load_after("OPEN", proposal_cfg)
    opened_state = opened.get("state") if opened else ST_NONE
    runner_outcome = _runner_entry_outcome(
        open_code, opened, proposal_cfg)
    accept_claim_failed = False
    if runner_outcome is not None and _entry_outcome_succeeded(runner_outcome):
        accept_path = os.path.join(cfg.receipt_dir, ACCEPT_ONE_FILE)
        accept_claim_failed = not _consume_accept_one(
            accept_path, entry_job.job_id)
        if accept_claim_failed:
            _touch_stop(os.path.join(cfg.receipt_dir, RUNNER_STOP_FILE))
            _count_reject(rejects, "accept_one_claim_failed")
            _row("ENTRY", "FROZEN", "ACCEPT_ONE_CLAIM_FAILED")
    _row("OPEN", "DONE" if open_code == 0 else "HELD",
         "proposal %s position command exit %s state %s"
         % (candidate.proposal_id, open_code, opened_state))
    if open_code != 0:
        _count_reject(rejects, "open_refused")
    if runner_outcome is not None \
            and _entry_outcome_terminal(runner_outcome) \
            and not _entry_outcome_succeeded(runner_outcome):
        reason = _entry_chain_refusal_reason(
            proposal_cfg, candidate.proposal_id,
            "POSITION_ENTRY_%s" % runner_outcome.verdict)
        if ladder_config is not None:
            _ladder_refusal(reason, rejects)
    if not accept_claim_failed and governor_token \
            and runner_outcome is not None \
            and _entry_outcome_terminal(runner_outcome):
        if not _release_governor(
                release_governor, governor_token, cfg.receipt_dir):
            _touch_stop(os.path.join(cfg.receipt_dir, RUNNER_STOP_FILE))
            _count_reject(rejects, "governor_release_failed")
            _row("GOVERNOR", "HELD",
                 "release failed; STOP-JOBS entry seal written")
        elif not _write_claim_governor_token(
                cfg, candidate.proposal_id, claimant, ""):
            _touch_stop(os.path.join(cfg.receipt_dir, RUNNER_STOP_FILE))
            _count_reject(rejects, "governor_token_clear_failed")
            _row("GOVERNOR", "HELD",
                 "released but claim token clear failed; STOP-JOBS written")
    if accept_claim_failed:
        return EXIT_FROZEN
    if opened is not None and opened_state in FREEZE_STATES:
        return EXIT_FROZEN
    return EXIT_ACTED


def run(cfg: PositionConfig, request, seconds: float, halt_file: str, *,
        sleep: Callable[[float], None] = time.sleep,
        now_ms: Callable[[], int] = default_now_ms,
        claimant: Optional[str] = None,
        ladder_config: Optional[LadderModeConfig] = None,
        ladder_event: Optional[DislocationEvent] = None) -> int:
    """Run ticks until the independent halt file appears."""
    if seconds <= 0:
        raise ValueError("SECONDS must be positive")
    last_code = EXIT_IDLE
    while not os.path.exists(halt_file):
        last_code = tick(cfg, request, sleep=sleep, now_ms=now_ms,
                         claimant=claimant,
                         ladder_config=ladder_config,
                         ladder_event=ladder_event)
        if os.path.exists(halt_file):
            break
        sleep(seconds)
    _row("LOOP", "HALTED", halt_file)
    return last_code


def _positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("SECONDS must be numeric") from None
    if not parsed > 0:
        raise argparse.ArgumentTypeError("SECONDS must be positive")
    return parsed


def _config_from_env(env, symbol_arg: str) -> PositionConfig:
    """Load shared policy while keeping SYMBOL an optional whitelist."""
    requested = symbol_arg or env.get("SYMBOL", "")
    cfg = config_from_env(env, requested or "ALLUSDT")
    return dataclasses.replace(cfg, symbol=cfg.symbol if requested else "")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="restart-safe one-position basis supervisor")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tick", action="store_true",
                      help="run one supervisor cycle")
    mode.add_argument("--run", metavar="SECONDS", type=_positive_seconds,
                      help="run ticks at this interval until the halt file")
    parser.add_argument("--symbol", default="",
                        help="optional proposal-symbol whitelist (default: "
                             "SYMBOL environment; unset means all)")
    parser.add_argument("--halt-file",
                        help="loop halt path (default: RECEIPT_DIR/HALT)")
    args = parser.parse_args(argv)

    try:
        cfg = _config_from_env(os.environ, args.symbol)
    except PositionError as error:
        _row("CONFIG", "FROZEN", error)
        return EXIT_FROZEN
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        _row("CONFIG", "FROZEN",
             "missing BINANCE_API_KEY / BINANCE_API_SECRET")
        return EXIT_FROZEN
    request = live_request_factory(api_key, api_secret)
    if args.tick:
        return tick(cfg, request)
    halt_file = os.path.expanduser(
        args.halt_file or os.path.join(cfg.receipt_dir, "HALT"))
    try:
        return run(cfg, request, args.run, halt_file)
    except KeyboardInterrupt:
        _row("LOOP", "HALTED", "keyboard interrupt")
        return EXIT_IDLE


if __name__ == "__main__":
    raise SystemExit(main())
