"""basis_position.py — persistent, resumable one-position basis lifecycle.

Ports the spec's lifecycle area (tests/fixtures/old_engine_spec.json) as one
restart-safe position machine on top of the PROVEN executor mechanics. This
module composes; it does not duplicate: transport, order settlement, signed
reads, market filters, and the receipts/events discipline all come from
basis_executor (settle_leg, signed_read, market_context, query_client_order,
spot_trades, save), and the one leverage mutation comes from
set_leverage_1x.run (set-and-verify exactly 1x).

Spec mapping (python_target_mapping collapse):
- candidate layer (signal machine IDLE..COOLDOWN) is NOT here — that stage
  belongs to the scanner machine; this module begins at ready->opening.
- plan layer: persisted states OPENING / OPEN (HEDGED_OPEN) /
  ONE_LEG_EXPOSED / CLOSING / CLOSED / UNKNOWN (RECOVERY_REQUIRED collapse),
  with the spec's guards: exposure derived from leg facts only, CLOSED only
  via the close path or a proven reconcile, EXPIRED never after exposure
  (apply_naked_duration_expired), restart = query-first by persisted client
  ids before ANY progression, unknowns freeze and never fabricate an exit.
- exit authority while OPEN: supervise() is the spec's 18-row directive
  ladder (first match wins; risk-reducing rows sit ABOVE the UNKNOWN
  freeze); hold_decision() is the CLI hold-tick subset the scope names —
  emergency file (owner order, risk-reducing), max-hold (THESIS_AGE ->
  MAXIMUM_HOLDING_HORIZON), convergence take-profit (recomputed ENTRY gap
  below watchGapPct -> THESIS_TARGET_REACHED, the old gap_closed exit), and
  edge inversion (REMAINING_EDGE_EXHAUSTED / BASIS_DIVERGENCE_INVALIDATION).
- settlement record: HandoffLane is the spec's BasisHandoffLifecycle —
  verbatim canonical quantities, strict refusal order, terminal immutable,
  default OFF (enabling is an owner decision).

Unit conventions (spec math area, tested explicitly): gaps/fees/edges in
PERCENT (0.78 = 0.78%); book gaps arrive in BPS from basis_read and are
converted (pct = bps / 100); funding rates are per-period FRACTIONS
(0.0001 = 1 bp); the supervision layer speaks canonical exact-decimal
strings in RATIO units ('0.04' = 4%) and is never mixed with the percent
layer without an explicit boundary conversion.

Owner-decision thresholds (spec: "flag, not invent"): the old code has NO
global max-hold, remaining-edge, or adverse-move defaults — they are
per-thesis fields. --open therefore REQUIRES MAX_HOLD_MS,
MIN_REMAINING_EDGE_PCT and MAX_ADVERSE_MOVE_PCT in the environment and
persists them into the position; only WATCH_GAP_PCT has a spec default
(0.15, scanner entry_thresholds KEEP).

State file: RECEIPT_DIR/basis-position.json, written atomically; the exact
order intent and client id are persisted BEFORE every mutation, so a crash
between persist and POST leaves an id to query, never a mystery. Commission
rates: RECEIPT_DIR/commission-rates.json (rates, not secrets). The shared
lane lock (basis-executor.lock) is held while a position stands, so the
one-shot executor and this machine can never trade the account at once.
Discipline is the executor's: truth only by client id, UNKNOWN stays UNKNOWN
(freeze, name exposure, exit nonzero), entry legs are never resent, and the
sealed risk-reducing Spot unwind may proceed only after exact absence; a
timeout never means not-filled.

Exit codes: 0 decided/OPEN/CLOSED/recorded | 2 refused before entry |
3 blocked / standing exposure | 4 UNKNOWN (frozen) | 5 preflight refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Callable, Dict, List, Optional, Tuple

import events
import set_leverage_1x
from basis_cycle import (
    ENTER_SHORT,
    ENTER_SPOT,
    EXIT_SHORT,
    EXIT_SPOT,
    FEE_BUFFER,
    MAX_CYCLE_QUOTE,
    UNWIND_SPOT,
    CyclePlan,
    leg_intent,
    quantize_down,
)
from basis_executor import (
    SPOT,
    SPOT_ACCOUNT_PATH,
    SPOT_BOOK_PATH,
    SPOT_OPEN_ORDERS_PATH,
    USDM,
    USDM_ACCOUNT_CONFIG_PATH,
    USDM_ACCOUNT_PATH,
    USDM_BOOK_PATH,
    USDM_OPEN_ORDERS_PATH,
    USDM_POSITION_PATH,
    USDM_POSITION_MODE_PATH,
    USDM_SYMBOL_CONFIG_PATH,
    VENUES,
    ExecutorConfig,
    ExecutorExit,
    PreflightRefusal,
    Refusal,
    RunState,
    attach_fees,
    base_event,
    base_fee_of,
    contract_note,
    fees_by_asset,
    first_fail,
    futures_position,
    gap_row,
    get_retry,
    live_request_factory,
    market_context,
    new_run_id,
    parse_trade_capability,
    query_client_order,
    query_client_order_strongest,
    save as executor_save,
    settle_leg,
    signed_read,
    sold_leg_result,
    spot_trades,
    symbol_leverage,
    utc,
    write_events,
)
from futures_hand import PreflightError as FuturesPreflightError
from futures_hand import clean, d, fmt
from limits import FIRST_TRADE_CAP_USDT

STATE_FILE = "basis-position.json"
STATE_WRITE_GUARD_FILE = "basis-position.write.lock"
# Position writes are short local critical sections. A two-minute heartbeat
# bound permits slow fsync while still making a stopped owner recoverable.
POSITION_WRITE_GUARD_STALE_MS = 120_000
RATES_FILE = "commission-rates.json"
EMERGENCY_FILE = "EMERGENCY"
STOP_FILE = "STOP"
STOP_JOBS_FILE = "STOP-JOBS"
PROPOSAL_DIR = "proposals"
LOCK_FILE = "basis-executor.lock"          # shared lane lock with the executor
SPOT_COMMISSION_PATH = "/api/v3/account/commission"
USDM_COMMISSION_PATH = "/fapi/v1/commissionRate"
USDM_INCOME_PATH = "/fapi/v1/income"
SYMBOL_RE = re.compile(r"[A-Z0-9]{5,20}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
MS_SAFE_MAX = 2 ** 53 - 1
PROPOSAL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
V2_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
V2_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
MARGIN_QUANTUM = Decimal("0.00000001")
PROPOSAL_REQUIRED_FIELDS = frozenset({
    "proposal_id", "created_utc", "expires_utc", "quant_sha",
    "config_digest", "symbol", "direction", "spot_ask", "perp_bid",
    "entry_gap_pct", "spot_bid", "perp_ask", "exit_gap_pct",
    "spot_maker_commission_pct", "spot_taker_commission_pct",
    "perp_maker_commission_pct", "perp_taker_commission_pct",
    "commission_source", "entry_slippage_pct", "exit_slippage_pct",
    "funding_rate", "funding_period_ms", "carry_pct",
    "safety_buffer_pct", "net_edge_pct", "quantity",
})

# Spec defaults that exist (everything without a spec default is an owner
# decision required in the environment — never invented here).
WATCH_GAP_PCT_DEFAULT = Decimal("0.15")    # scanner entry_thresholds KEEP
SPOT_TAKER_FEE_PCT_DEFAULT = Decimal("0.1")   # math four_leg_fees KEEP, per leg
USDM_TAKER_FEE_PCT_DEFAULT = Decimal("0.05")  # math four_leg_fees KEEP, per leg
ENTRY_REVALIDATION_FAILED = "ENTRY_REVALIDATION_FAILED"
ENTRY_REVALIDATION_PASSED = "ENTRY_REVALIDATION_PASSED"

# Persisted machine states (plan-layer collapse).
ST_OPENING = "OPENING"
ST_OPEN = "OPEN"
ST_ONE_LEG = "ONE_LEG_EXPOSED"
ST_CLOSING = "CLOSING"
ST_CLOSED = "CLOSED"
ST_UNKNOWN = "UNKNOWN"
ST_NONE = "NONE"
ST_ADDING = "ADDING"
ST_ADD_ABORTED = "ADD_ABORTED"
STANDING_STATES = (ST_OPENING, ST_OPEN, ST_ONE_LEG, ST_CLOSING, ST_UNKNOWN)

# Version 2 persists one append-only ledger.  The ConfigSnapshot event at
# index zero is the only configuration truth; every operational view is
# reconstructed by derive_v2_state().  The legacy version key remains an
# opaque version-1 field unless a v2-only ledger shape is present.
V2_SCHEMA_VERSION = 2
V2_DIRECTION = "long_spot_short_perp"
V2_FLAT_PROOF_MAX_AGE_MS = 30000
V2_GENESIS_DIGEST = "0" * 64
V2_MIN_EPOCH_MS = 946684800000
# Exchange observations may lead the local clock by at most two seconds,
# matching the program's existing public-market future-time allowance.
V2_EVENT_FUTURE_SKEW_MS = 2000
# A private reconcile read may be at most one minute old at append time;
# observed_at_ms is read-digest-bound and read_received_at is the append
# clock, so older account truth cannot be relabelled as current ledger truth.
V2_RECONCILE_READ_MAX_AGE_MS = 60_000
# Event zero uses a fixed in-range cut so its digest remains config-bound;
# every later event must advance beyond this sentinel.
V2_GENESIS_CUT_MS = V2_MIN_EPOCH_MS
V2_MAX_LEDGER_EVENTS = 10000
V2_MAX_DECIMAL = Decimal("9999999999999999999999999999")
V2_MAX_GAP_PCT = Decimal("1000000")
V2_MAX_RATE = Decimal("1")
# The v2 snapshot does not yet persist a venue quantity step.  Until it does,
# receipt quantities and derived remainders share this exact Decimal floor.
V2_MIN_QUANTITY_FLOOR = Decimal(
    "0.0000000000000000000000000001")
V2_CLOSED_RESIDUAL = "CLOSED_RESIDUAL"
V2_DUST_STANDING = "DUST_STANDING"
V2_EVENT_TYPES = frozenset((
    "ConfigSnapshot", "ReceiptObserved", "RungFilled", "RungReduced",
    "ReconcileObserved", "FlatProved", "AddIntent", "AddHedgeIntent",
    "AddUnwindIntent", "AddAborted",
))
V2_RECEIPT_KINDS = frozenset(("REDUCTION", "FLAT"))
V2_TERMINAL_ORDER_STATUSES = frozenset((
    "FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED",
    "EXPIRED_IN_MATCH",
))
V2_TOP_STATES = frozenset((
    "UNPROVEN", "READY", ST_OPEN, ST_ADDING, "FROZEN",
    ST_ADD_ABORTED, "AWAITING_FLAT_PROOF", ST_CLOSED,
))

# Cross-lane verdict word carried by the state file so the executor's
# prior-receipt gate always sees a verdict key with an honest value.
STATE_VERDICT = {
    ST_OPENING: "OPENING",
    ST_OPEN: "OPEN_EXPOSURE",
    ST_ONE_LEG: "OPEN_EXPOSURE",
    ST_CLOSING: "CLOSING",
    ST_CLOSED: "CLOSED",
    ST_UNKNOWN: "UNKNOWN",
    ST_NONE: "REJECTED_BEFORE_ENTRY",
}

EXIT_CODE = {
    "RATES_RECORDED": 0,
    "OPEN": 0,
    "DECIDED": 0,
    "CLOSED": 0,
    "NOTHING_STANDING": 0,
    "REJECTED_BEFORE_ENTRY": 2,
    "REFUSED": 2,
    "BLOCKED": 3,
    "OPEN_EXPOSURE": 3,
    "ONE_LEG_EXPOSED": 3,
    "UNKNOWN": 4,
    "PREFLIGHT_REFUSED": 5,
    ST_ADD_ABORTED: 2,
}

# Supervision vocabulary (spec supervision_directive_ladder / exit rules).
HOLD = "HOLD"
REQUEST_HEDGE = "REQUEST_HEDGE"
REQUEST_EXIT = "REQUEST_EXIT"
REQUEST_EMERGENCY_UNWIND = "REQUEST_EMERGENCY_UNWIND"
REDUCE = "REDUCE"
FREEZE_NEW_ACTIONS = "FREEZE_NEW_ACTIONS"
ADD_STAGE = "ADD_STAGE"

ROUTINE = "ROUTINE"
ELEVATED = "ELEVATED"
URGENT = "URGENT"
EMERGENCY = "EMERGENCY"

RISK_REDUCING = "RISK_REDUCING"
RISK_NONE = "NONE"
RISK_INCREASING = "INCREASING"


class PositionError(Exception):
    """A local refusal before any mutation; factual, never a market outcome."""


class _EntryBlocked(PositionError):
    """A standing local authority blocks a new entry before transport."""


class _EntrySealBeforeSpot(Exception):
    """Carries a shared entry-seal decision to the entry core."""

    def __init__(self, seals: List[str]):
        super().__init__(",".join(seals))
        self.seals = list(seals)


def _fsync_file_and_parent(path: str) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent = os.path.dirname(path) or "."
    descriptor = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save(state: RunState) -> None:
    """Use the executor's single durable command-receipt writer."""
    try:
        executor_save(state)
    except OSError:
        raise PositionError(
            "DURABLE_RECEIPT_WRITE_REFUSED: command receipt was not sealed") \
            from None


# --------------------------------------------------------------------------
# Decision shape shared by supervise() and hold_decision(). Deterministic:
# fixed keys, canonical string numbers, keys derived only from the inputs —
# same input + same now_ms = byte-equivalent decision.
# --------------------------------------------------------------------------

def _key(prefix: str, kind: str, why0: str, now_ms: int) -> str:
    payload = json.dumps([prefix, kind, why0, now_ms], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _decision(kind: str, why: List[str], urgency: str, risk_effect: str,
              target: Optional[Dict[str, str]], deadline: Optional[int],
              now_ms: int, completeness: str = "OK",
              blocking: Tuple[str, ...] = (),
              stage_auth: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    why0 = why[0] if why else ""
    return {
        "kind": kind,
        "why": list(why),
        "urgency": urgency,
        "risk_effect": risk_effect,
        "target": target,
        "hard_deadline_ms": deadline,
        "completeness": completeness,
        "blocking_facts": list(blocking),
        "stage_transition_authorization": stage_auth,
        "decision_key": _key("decision", kind, why0, now_ms),
        "idempotency_key": _key("intent", kind, why0, now_ms),
        "now_ms": now_ms,
    }


def _ratio(value: Any) -> Optional[Decimal]:
    """Canonical exact-decimal RATIO string -> Decimal; anything else None."""
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _safe_ms(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value <= MS_SAFE_MAX:
        return None
    return value


def _converging_run(path: List[Any]) -> int:
    """Trailing run (by observation order) where |basis| is non-increasing;
    count starts at 1 for a non-empty path (spec exit_convergence)."""
    values = []
    for item in path:
        parsed = _ratio(item)
        if parsed is None:
            return 0
        values.append(abs(parsed))
    if not values:
        return 0
    run = 1
    for index in range(len(values) - 1, 0, -1):
        if values[index] <= values[index - 1]:
            run += 1
        else:
            break
    return run


def supervise(position: Dict[str, Any], common_policy: Dict[str, Any],
              basis_policy: Dict[str, Any], facts: Dict[str, Any],
              now_ms: int) -> Dict[str, Any]:
    """The spec's supervision directive ladder — strict priority, first match
    wins; risk-reducing one-leg/reconciliation rows evaluate BEFORE the
    UNKNOWN freeze, so unknowns can never suppress risk reduction and never
    fabricate an exit. Pure and deterministic."""
    legs = position.get("legs", [])
    exposures = []
    for leg in legs:
        value = _ratio(leg.get("quote_exposure"))
        if value is None:
            value = Decimal(0)
        exposures.append(value if leg.get("side") == "LONG" else -value)
    net_exposure = sum(exposures, Decimal(0))
    gross_exposure = sum((abs(v) for v in exposures), Decimal(0))
    one_leg = abs(net_exposure)
    reconciliation = position.get("reconciliation", "EXACT")
    lifecycle = position.get("lifecycle", "HEDGED_OPEN")
    recon_uncertain = reconciliation not in ("EXACT", "PASS") \
        or lifecycle == "RECOVERY_REQUIRED"

    # Row 1 — a closed position needs nothing.
    if lifecycle == "CLOSED":
        return _decision(HOLD, ["POSITION_CLOSED"], ROUTINE, RISK_NONE,
                         None, None, now_ms)

    # Common policy parse (invalid -> row 3 while one-leg).
    max_one_leg = _ratio(common_policy.get("max_one_leg_exposure_usd"))
    max_one_leg_ms = _safe_ms(common_policy.get("maximum_one_leg_duration_ms"))
    emergency_ms = _safe_ms(common_policy.get("emergency_one_leg_duration_ms"))
    max_gross = _ratio(common_policy.get("max_gross_exposure_usd"))
    reduce_to = common_policy.get("reduction_target_exposure_usd")
    min_depth = _ratio(common_policy.get("min_market_depth_usd"))
    policy_valid = None not in (max_one_leg, max_one_leg_ms, emergency_ms,
                                max_gross, min_depth) \
        and isinstance(reduce_to, str) and _ratio(reduce_to) is not None

    if one_leg > 0:
        # Row 2 — uncertain truth under naked exposure is an emergency.
        if recon_uncertain:
            return _decision(
                REQUEST_EMERGENCY_UNWIND,
                ["ONE_LEG_RECONCILIATION_UNCERTAIN"], EMERGENCY,
                RISK_REDUCING, {"kind": "FLAT", "quote_notional": "0"},
                now_ms, now_ms)
        # Row 3 — invalid policy under naked exposure.
        if not policy_valid:
            return _decision(
                REQUEST_EMERGENCY_UNWIND,
                ["ONE_LEG_SUPERVISION_POLICY_INVALID"], EMERGENCY,
                RISK_REDUCING, {"kind": "FLAT", "quote_notional": "0"},
                now_ms, now_ms)
        # Row 4 — duration unknown or beyond the emergency clock.
        since = _safe_ms(position.get("one_leg_since_ms"))
        elapsed = None if since is None else now_ms - since
        if elapsed is None or elapsed > emergency_ms:
            return _decision(
                REQUEST_EMERGENCY_UNWIND, ["ONE_LEG_DURATION_EMERGENCY"],
                EMERGENCY, RISK_REDUCING,
                {"kind": "FLAT", "quote_notional": "0"}, now_ms, now_ms)
        # Row 5 — hedge the naked leg back to delta-neutral.
        breach = one_leg > max_one_leg or elapsed > max_one_leg_ms
        return _decision(
            REQUEST_HEDGE, ["ONE_LEG_EXPOSURE"],
            URGENT if breach else ELEVATED, RISK_REDUCING,
            {"kind": "DELTA", "quote_notional": fmt(-net_exposure)},
            since + max_one_leg_ms, now_ms)

    # Row 6 — reconciliation not exact freezes everything below.
    if recon_uncertain:
        return _decision(
            FREEZE_NEW_ACTIONS, ["RECONCILIATION_NOT_EXACT"],
            EMERGENCY if reconciliation == "BREACH" else URGENT,
            RISK_NONE, None, None, now_ms,
            blocking=("RECONCILIATION_NOT_EXACT",))
    if not policy_valid:
        # Fail-closed without exposure: no guesses over a broken policy.
        return _decision(FREEZE_NEW_ACTIONS, ["SUPERVISION_POLICY_INVALID"],
                         URGENT, RISK_NONE, None, None, now_ms,
                         completeness="UNKNOWN",
                         blocking=("SUPERVISION_POLICY_INVALID",))

    # Basis truth freshness gates every check BELOW this line. A missing or
    # malformed fact becomes an UNKNOWN check — never a guess — and any
    # UNKNOWN check ends in the row-16 freeze (no exit fabricated).
    fresh = "OK"
    if not facts.get("required_value_present", True):
        fresh = "UNKNOWN"
    else:
        source = _safe_ms(facts.get("source_time_ms"))
        max_age = _safe_ms(facts.get("max_age_ms"))
        if source is None or max_age is None or now_ms - source > max_age:
            fresh = "STALE"

    unknown_checks: List[str] = []
    thesis = facts.get("thesis", {})
    if fresh == "OK":
        # Row 7 — thesis exits: invalidation, max-hold, convergence target.
        if thesis.get("invalidated") is True:
            return _decision(REQUEST_EXIT, ["THESIS_INVALIDATED"], URGENT,
                             RISK_REDUCING,
                             {"kind": "FLAT", "quote_notional": "0"},
                             now_ms, now_ms)
        opened = _safe_ms(thesis.get("opened_at_ms"))
        horizon = _safe_ms(thesis.get("maximum_holding_horizon_ms"))
        if opened is None or horizon is None:
            unknown_checks.append("THESIS_AGE")
        elif now_ms - opened >= horizon:
            return _decision(REQUEST_EXIT, ["MAXIMUM_HOLDING_HORIZON"],
                             URGENT, RISK_REDUCING,
                             {"kind": "FLAT", "quote_notional": "0"},
                             now_ms, now_ms)
        if thesis.get("target_reached") is True:
            return _decision(REQUEST_EXIT, ["THESIS_TARGET_REACHED"],
                             ROUTINE, RISK_REDUCING,
                             {"kind": "FLAT", "quote_notional": "0"},
                             None, now_ms)
        # Row 8 — gross exposure.
        if gross_exposure > max_gross:
            return _decision(REDUCE, ["GROSS_EXPOSURE_LIMIT"], URGENT,
                             RISK_REDUCING,
                             {"kind": "EXPOSURE", "quote_notional": reduce_to},
                             now_ms, now_ms)
        # Row 9 — market depth / exit capacity.
        depth = _ratio(facts.get("available_depth_usd"))
        if depth is None:
            unknown_checks.append("MARKET_DEPTH")
        elif depth < min_depth:
            return _decision(REDUCE, ["MARKET_DEPTH"], ELEVATED,
                             RISK_REDUCING,
                             {"kind": "EXPOSURE", "quote_notional": reduce_to},
                             None, now_ms)
        # Row 10 — outstanding intents.
        if facts.get("outstanding_intents_breach") is True:
            return _decision(FREEZE_NEW_ACTIONS, ["OUTSTANDING_INTENTS"],
                             ELEVATED, RISK_NONE, None, None, now_ms,
                             blocking=("OUTSTANDING_INTENTS",))

        # Basis-specific rows.
        min_liq = _ratio(basis_policy.get("minimum_liquidation_distance"))
        min_margin = _ratio(basis_policy.get("minimum_margin_distance"))
        liq = _ratio(facts.get("liquidation_distance"))
        margin = _ratio(facts.get("margin_distance"))
        borrow = facts.get("borrow", {})
        # Row 11 — capital survival.
        if None in (min_liq, min_margin, liq, margin):
            unknown_checks.append("BASIS_CAPITAL_SURVIVAL")
        elif liq < min_liq or margin < min_margin \
                or borrow.get("availability") == "RECALLED":
            return _decision(
                REQUEST_EMERGENCY_UNWIND, ["BASIS_CAPITAL_SURVIVAL_LIMIT"],
                EMERGENCY, RISK_REDUCING,
                {"kind": "FLAT", "quote_notional": "0"}, now_ms, now_ms)
        if borrow.get("availability") == "RECALLED" \
                and "BASIS_CAPITAL_SURVIVAL" in unknown_checks:
            # A recall is a breach fact on its own even with distances unknown.
            return _decision(
                REQUEST_EMERGENCY_UNWIND, ["BASIS_CAPITAL_SURVIVAL_LIMIT"],
                EMERGENCY, RISK_REDUCING,
                {"kind": "FLAT", "quote_notional": "0"}, now_ms, now_ms)
        # Row 12 — borrow gone.
        if borrow.get("availability") == "UNAVAILABLE":
            return _decision(REQUEST_EXIT, ["BORROW_UNAVAILABLE"], URGENT,
                             RISK_REDUCING,
                             {"kind": "FLAT", "quote_notional": "0"},
                             now_ms, now_ms)
        # Row 13 — hedge drift.
        drift = _ratio(facts.get("hedge_drift"))
        max_drift = _ratio(basis_policy.get("maximum_hedge_drift"))
        if drift is None or max_drift is None:
            unknown_checks.append("BASIS_HEDGE_DRIFT")
        elif drift > max_drift:
            return _decision(REQUEST_HEDGE, ["BASIS_HEDGE_DRIFT"], URGENT,
                             RISK_REDUCING,
                             {"kind": "DELTA", "quote_notional": "0"},
                             now_ms, now_ms)
        # Row 14 — edge inversion exits.
        remaining = _ratio(facts.get("expected_remaining_edge_after_exit_costs"))
        min_edge = _ratio(basis_policy.get("minimum_remaining_edge"))
        if remaining is None or min_edge is None:
            unknown_checks.append("BASIS_REMAINING_EDGE")
        elif remaining <= min_edge:
            return _decision(REQUEST_EXIT, ["REMAINING_EDGE_EXHAUSTED"],
                             URGENT, RISK_REDUCING,
                             {"kind": "FLAT", "quote_notional": "0"},
                             now_ms, now_ms)
        entry = _ratio(facts.get("entry_executable_basis"))
        current = _ratio(facts.get("current_executable_basis"))
        max_move = _ratio(basis_policy.get("maximum_adverse_basis_move"))
        if None in (entry, current, max_move):
            unknown_checks.append("BASIS_DIVERGENCE")
        elif max(Decimal(0), abs(current) - abs(entry)) > max_move:
            return _decision(REQUEST_EXIT, ["BASIS_DIVERGENCE_INVALIDATION"],
                             URGENT, RISK_REDUCING,
                             {"kind": "FLAT", "quote_notional": "0"},
                             now_ms, now_ms)
        # Row 15 — carry / borrow-rate / liquidity pressure reduces.
        funding = facts.get("funding", {})
        rate = _ratio(funding.get("rate"))
        max_funding = _ratio(basis_policy.get("maximum_adverse_funding_rate"))
        lead = _safe_ms(basis_policy.get("funding_exit_lead_time_ms"))
        next_at = _safe_ms(funding.get("next_funding_at_ms"))
        direction = funding.get("direction")
        if direction not in ("PAID", "RECEIVED") or rate is None \
                or max_funding is None or lead is None \
                or (direction == "PAID" and next_at is None):
            unknown_checks.append("BASIS_FUNDING")
        elif direction == "PAID" and abs(rate) > max_funding \
                and next_at <= now_ms + lead:
            return _decision(REDUCE, ["BASIS_FUNDING"], ELEVATED,
                             RISK_REDUCING,
                             {"kind": "EXPOSURE", "quote_notional": reduce_to},
                             None, now_ms)
        borrow_rate = _ratio(borrow.get("rate"))
        max_borrow = _ratio(basis_policy.get("maximum_borrow_rate"))
        if borrow_rate is not None and max_borrow is not None \
                and borrow_rate > max_borrow:
            return _decision(REDUCE, ["BASIS_BORROW_RATE"], ELEVATED,
                             RISK_REDUCING,
                             {"kind": "EXPOSURE", "quote_notional": reduce_to},
                             None, now_ms)
        deterioration = _ratio(facts.get("liquidity_deterioration"))
        max_det = _ratio(basis_policy.get("maximum_liquidity_deterioration"))
        if deterioration is None or max_det is None:
            unknown_checks.append("BASIS_LIQUIDITY")
        elif deterioration > max_det:
            return _decision(REDUCE, ["BASIS_LIQUIDITY"], ELEVATED,
                             RISK_REDUCING,
                             {"kind": "EXPOSURE", "quote_notional": reduce_to},
                             None, now_ms)

    # Row 16 — any UNKNOWN check freezes increases; no exit fabricated.
    if fresh != "OK":
        return _decision(FREEZE_NEW_ACTIONS, ["BASIS_REQUIRED_TRUTH_UNKNOWN"],
                         URGENT, RISK_NONE, None, None, now_ms,
                         completeness=fresh,
                         blocking=("BASIS_FACT_FRESHNESS",))
    if unknown_checks:
        return _decision(FREEZE_NEW_ACTIONS, ["BASIS_REQUIRED_TRUTH_UNKNOWN"],
                         URGENT, RISK_NONE, None, None, now_ms,
                         completeness="UNKNOWN",
                         blocking=tuple(unknown_checks))

    # Row 17 — ADD only when EVERY check passed and truth allows increase.
    posture = position.get("posture", "NORMAL")
    authority = facts.get("stage_authority", {})
    required = basis_policy.get("required_converging_observations_for_add")
    run = _converging_run(facts.get("basis_path", []))
    remaining = _ratio(facts.get("expected_remaining_edge_after_exit_costs"))
    min_for_add = _ratio(basis_policy.get("minimum_remaining_edge_for_add"))
    consuming = authority.get("consuming_usd")
    if posture == "NORMAL" and authority.get("valid") is True \
            and isinstance(required, int) and run >= required \
            and remaining is not None and min_for_add is not None \
            and remaining >= min_for_add \
            and _ratio(consuming) is not None \
            and gross_exposure + _ratio(consuming) <= max_gross:
        return _decision(ADD_STAGE, ["BASIS_TRUTH_ALLOWS_INCREASE"], ROUTINE,
                         RISK_INCREASING, None, None, now_ms,
                         stage_auth={"consuming_usd": consuming})

    # Row 18 — nothing demanded; hold within policy.
    return _decision(HOLD, ["BASIS_THESIS_REMAINS_WITHIN_POLICY"], ROUTINE,
                     RISK_NONE, None, None, now_ms)


# --------------------------------------------------------------------------
# Plan-layer reducer check: naked-duration expiry (spec
# reducer_naked_duration_expiry / naked_duration_timer). EXPIRED is
# unreachable after exposure — the only outcome is RECOVERY_REQUIRED.
# --------------------------------------------------------------------------

def apply_naked_duration_expired(exposed_since_ms: Optional[int],
                                 max_naked_duration_ms: Optional[int],
                                 occurred_at_ms: int) -> Tuple[str, str]:
    """Returns ("INVALID_EVENT", why) or ("RECOVERY_REQUIRED", why)."""
    since = _safe_ms(exposed_since_ms)
    limit = _safe_ms(max_naked_duration_ms)
    if since is None or limit is None or limit <= 0:
        return ("INVALID_EVENT",
                "maxNakedDurationMs is a required positive per-plan field "
                "and exposedSinceMs must be latched")
    if occurred_at_ms < since + limit:
        return ("INVALID_EVENT",
                "occurredAtMs before exposedSince + maxNakedDuration")
    return ("RECOVERY_REQUIRED",
            "naked exposure outlived the per-plan clock; EXPIRED is "
            "unreachable after exposure")


# --------------------------------------------------------------------------
# Handoff settlement record (spec handoff_machine): single-use, default OFF,
# verbatim canonical quantities, strict refusal order, terminal immutable.
# --------------------------------------------------------------------------

HANDOFF_FIELDS = (
    "handoff_id", "proposal_id", "symbol", "direction",
    "accepted_notional_usd", "accepted_revision_ts", "accepted_at_ms",
    "correlation_id", "bound_ts", "state", "last_sequence",
    "last_observed_at_ms", "working_count", "ambiguous_count",
    "cancel_requested_count", "terminal_kind", "cumulative_filled_quantity",
    "terminal_ts",
)
CANONICAL_QTY_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?")
TERMINAL_KINDS = ("TERMINAL", "CANCEL_CONFIRMED")
OBSERVATION_KINDS = ("WORKING", "AMBIGUOUS", "CANCEL_REQUESTED")


def canonical_quantity(value: Any) -> bool:
    """True only for a string equal to its own canonical non-negative
    exact-decimal form: no sign, no exponent, no padding, no trailing or
    leading zeros (a lone 0 stands)."""
    return isinstance(value, str) and \
        CANONICAL_QTY_RE.fullmatch(value) is not None


class HandoffLane:
    """One BasisHandoffLifecycle lane. Disabled lanes refuse BEFORE any
    dependency is touched, so no acceptance ever burns without a handoff."""

    def __init__(self, store: Any = None, clock: Callable[[], int] = None,
                 enabled: bool = False):
        self.store = store
        self.clock = clock
        self.enabled = enabled
        self.record: Optional[Dict[str, Any]] = None

    def snapshot(self) -> Dict[str, Any]:
        return {"enabled": self.enabled,
                "handoff": dict(self.record) if self.record else None}

    def consume_accepted(self, proposal_id: str) -> Dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "refusal": "DISABLED"}
        if self.record is not None:
            return {"ok": False, "refusal": "ALREADY_BOUND"}
        accepted = self.store.claim(proposal_id)
        bound_ts = self.clock()
        self.record = {
            "handoff_id": "bh_" + str(accepted["proposal_id"]),
            "proposal_id": accepted["proposal_id"],
            "symbol": accepted["symbol"],
            "direction": accepted["direction"],
            "accepted_notional_usd": accepted["accepted_notional_usd"],
            "accepted_revision_ts": accepted["accepted_revision_ts"],
            "accepted_at_ms": accepted["accepted_at_ms"],
            "correlation_id": accepted["correlation_id"],
            "bound_ts": bound_ts,
            "state": "AWAITING_FEEDBACK",
            "last_sequence": 0,
            "last_observed_at_ms": bound_ts,
            "working_count": 0,
            "ambiguous_count": 0,
            "cancel_requested_count": 0,
            "terminal_kind": None,
            "cumulative_filled_quantity": None,
            "terminal_ts": None,
        }
        return {"ok": True, "handoff_id": self.record["handoff_id"]}

    def apply_feedback(self, message: Any) -> Dict[str, Any]:
        # Refusal order is the spec's, exactly.
        if not self.enabled:
            return {"ok": False, "refusal": "DISABLED"}
        if self.record is None:
            return {"ok": False, "refusal": "NO_ACTIVE_HANDOFF"}
        if not isinstance(message, dict) \
                or message.get("kind") not in TERMINAL_KINDS + OBSERVATION_KINDS:
            return {"ok": False, "refusal": "FEEDBACK_MALFORMED"}
        sequence = message.get("sequence")
        observed = message.get("observed_at_ms")
        revision = message.get("accepted_revision_ts")
        if _safe_ms(sequence) is None or sequence < 1 \
                or _safe_ms(observed) is None or _safe_ms(revision) is None:
            return {"ok": False, "refusal": "FEEDBACK_MALFORMED"}
        for field in ("handoff_id", "proposal_id", "symbol"):
            if not isinstance(message.get(field), str):
                return {"ok": False, "refusal": "FEEDBACK_MALFORMED"}
        record = self.record
        if message["handoff_id"] != record["handoff_id"] \
                or message["proposal_id"] != record["proposal_id"] \
                or message["symbol"] != record["symbol"]:
            return {"ok": False, "refusal": "WRONG_IDENTITY"}
        if revision != record["accepted_revision_ts"]:
            return {"ok": False, "refusal": "REVISION_MISMATCH"}
        if record["state"] == "TERMINAL":
            return {"ok": False, "refusal": "ALREADY_TERMINAL"}
        if sequence < record["last_sequence"]:
            return {"ok": False, "refusal": "STALE_SEQUENCE"}
        if sequence == record["last_sequence"]:
            return {"ok": False, "refusal": "DUPLICATE_SEQUENCE"}
        if observed < record["bound_ts"] \
                or observed < record["last_observed_at_ms"]:
            return {"ok": False, "refusal": "STALE_OBSERVATION"}
        kind = message["kind"]
        if kind in TERMINAL_KINDS:
            quantity = message.get("cumulative_filled_quantity")
            if not canonical_quantity(quantity):
                return {"ok": False, "refusal": "QUANTITY_NOT_EXACT"}
            # Terminal clock runs BEFORE any mutation: a throw or a
            # regression leaves the record wholly awaiting.
            terminal_ts = self.clock()
            if terminal_ts < record["bound_ts"]:
                raise ValueError("terminal clock must not regress")
            record["state"] = "TERMINAL"
            record["terminal_kind"] = "TERMINAL_REPORTED" \
                if kind == "TERMINAL" else "CANCEL_CONFIRMED"
            record["cumulative_filled_quantity"] = quantity
            record["last_sequence"] = sequence
            record["last_observed_at_ms"] = observed
            record["terminal_ts"] = terminal_ts
            return {"ok": True, "progressed": True, "state": "TERMINAL",
                    "terminal_kind": record["terminal_kind"],
                    "cumulative_filled_quantity": quantity,
                    "last_sequence": sequence, "terminal_ts": terminal_ts}
        # Observations advance ordering only; smuggled quantity is ignored.
        record["last_sequence"] = sequence
        record["last_observed_at_ms"] = observed
        counter = {"WORKING": "working_count", "AMBIGUOUS": "ambiguous_count",
                   "CANCEL_REQUESTED": "cancel_requested_count"}[kind]
        record[counter] += 1
        return {"ok": True, "progressed": False, "state": "AWAITING_FEEDBACK",
                "last_sequence": sequence}


# --------------------------------------------------------------------------
# Persistent state file.  Version 1 keeps its frozen behavior.  Version 2
# has one append-only ledger and no persisted projection of that ledger.
# --------------------------------------------------------------------------

_V2_POSITION_FIELDS = frozenset({"version", "schema_version", "ledger"})
_V2_EVENT_COMMON_FIELDS = frozenset({
    "event_type", "cut_ms", "previous_digest", "event_digest",
})
_V2_CONFIG_FIELDS = frozenset({
    "symbol", "direction", "schedule", "caps", "venue",
})
_V2_CONFIG_SCHEDULE_FIELDS = frozenset({
    "ordinal", "entry_gap_pct", "quote_cap",
})
_V2_CONFIG_CAP_FIELDS = frozenset({"stack_quote", "entry_quote"})
_V2_CONFIG_VENUE_FIELDS = frozenset({
    "spot", "futures", "base_asset", "quote_asset",
})
_V2_RECEIPT_FIELDS = frozenset({
    "receipt_id", "receipt_kind", "receipt_digest", "observed_at_ms",
    "source", "facts",
})
_V2_REDUCTION_FACT_FIELDS = frozenset({
    "ordinal", "spot_quantity", "short_quantity",
})
_V2_FLAT_FACT_FIELDS = frozenset({
    "futures_position_quantity", "spot_sellable_base_quantity",
    "standard_open_order_count", "algorithmic_open_order_count",
    "own_orders_terminal", "positions_digest", "open_orders_digest",
    "own_orders_digest",
})
_V2_RUNG_FILLED_FIELDS = frozenset({
    "ordinal", "legs", "dislocation",
})
_V2_LEGS_FIELDS = frozenset({"spot", "short"})
_V2_LEG_FIELDS = frozenset({
    "stage", "market", "client_order_id", "exchange_order_id",
    "terminal_status", "executed_quantity", "executed_quote",
    "fee_by_asset",
})
_V2_DISLOCATION_FIELDS = frozenset({
    "event_id", "source_time_ms", "entry_gap_pct", "exit_gap_pct",
    "funding_rate", "funding_period_ms", "digest",
})
_V2_DISLOCATION_INPUT_FIELDS = _V2_DISLOCATION_FIELDS - {"digest"}
_V2_RUNG_REDUCED_FIELDS = frozenset({
    "ordinal", "receipt_ids", "amounts",
})
_V2_REDUCTION_AMOUNT_FIELDS = frozenset({
    "spot_quantity", "short_quantity",
})
_V2_RECONCILE_FACT_FIELDS = frozenset({
    "reconcile_id", "observed_at_ms", "source", "baseline",
    "positions", "open_orders", "own_orders", "balances",
})
_V2_RECONCILE_READ_FIELDS = _V2_RECONCILE_FACT_FIELDS | frozenset({
    "read_digest",
})
_V2_RECONCILE_FIELDS = _V2_RECONCILE_READ_FIELDS | frozenset({
    "read_received_at",
})
_V2_RECONCILE_POSITION_FIELDS = frozenset({
    "spot_base_quantity", "futures_position_quantity",
})
_V2_RECONCILE_OPEN_ORDER_FIELDS = frozenset({
    "spot_standard_client_order_ids", "spot_algorithmic_client_order_ids",
    "futures_standard_client_order_ids",
    "futures_algorithmic_client_order_ids",
})
_V2_RECONCILE_OWN_ORDER_FIELDS = frozenset({
    "client_order_id", "market", "status", "executed_quantity",
})
_V2_RECONCILE_BALANCE_FIELDS = frozenset({
    "spot_base_free_quantity", "spot_base_locked_quantity",
    "spot_quote_free_quantity", "futures_wallet_balance",
})
_V2_FLAT_PROVED_FIELDS = frozenset({"receipt_id", "module_time_ms"})
_V2_FLAT_PROVED_INPUT_FIELDS = frozenset({"event_type", "receipt_id"})
_V2_ADD_INTENT_FIELDS = frozenset({
    "ordinal", "run_id", "proposal_id", "proposal_digest",
    "config_event_digest", "effective_cap_quote",
    "schedule_entry_gap_pct", "dislocation", "intent",
})
_V2_ADD_HEDGE_INTENT_FIELDS = frozenset({
    "ordinal", "run_id", "spot_leg", "intent",
})
_V2_ADD_UNWIND_INTENT_FIELDS = frozenset({
    "ordinal", "run_id", "spot_leg", "short_status",
    "net_spot_quantity", "sellable_quantity", "initial_dust_quantity",
    "intent",
})
_V2_ADD_ABORTED_FIELDS = frozenset({
    "ordinal", "run_id", "reason", "spot_status", "short_status",
    "spot_net_quantity", "unwind_requested_quantity",
    "unwind_filled_quantity", "unwind_base_fee_quantity",
    "dust_quantity", "spot_leg", "unwind_leg",
})
_V2_SPOT_INTENT_FIELDS = frozenset({
    "symbol", "newClientOrderId", "market", "side", "type",
    "quoteOrderQty",
})
_V2_SHORT_INTENT_FIELDS = frozenset({
    "symbol", "newClientOrderId", "market", "side", "type", "quantity",
})
_V2_UNWIND_INTENT_FIELDS = _V2_SHORT_INTENT_FIELDS
_V2_ADD_SHORT_TERMINAL_STATUSES = frozenset({
    "NOT_SEALED", "REJECTED", "NOT_FOUND", "ZERO_FILLED",
})
_V2_ADD_ABORT_SPOT_STATUSES = frozenset({
    "NOT_FOUND", "NOT_SUBMITTED", "FILLED",
})
_V2_EVENT_FIELDS = {
    "ConfigSnapshot": _V2_EVENT_COMMON_FIELDS | _V2_CONFIG_FIELDS,
    "ReceiptObserved": _V2_EVENT_COMMON_FIELDS | _V2_RECEIPT_FIELDS,
    "RungFilled": _V2_EVENT_COMMON_FIELDS | _V2_RUNG_FILLED_FIELDS,
    "RungReduced": _V2_EVENT_COMMON_FIELDS | _V2_RUNG_REDUCED_FIELDS,
    "ReconcileObserved": _V2_EVENT_COMMON_FIELDS | _V2_RECONCILE_FIELDS,
    "FlatProved": _V2_EVENT_COMMON_FIELDS | _V2_FLAT_PROVED_FIELDS,
    "AddIntent": _V2_EVENT_COMMON_FIELDS | _V2_ADD_INTENT_FIELDS,
    "AddHedgeIntent":
        _V2_EVENT_COMMON_FIELDS | _V2_ADD_HEDGE_INTENT_FIELDS,
    "AddUnwindIntent":
        _V2_EVENT_COMMON_FIELDS | _V2_ADD_UNWIND_INTENT_FIELDS,
    "AddAborted": _V2_EVENT_COMMON_FIELDS | _V2_ADD_ABORTED_FIELDS,
}
_V2_EVENT_PAYLOAD_FIELDS = {
    kind: fields - {"previous_digest", "event_digest", "cut_ms"}
    for kind, fields in _V2_EVENT_FIELDS.items()
}
_V2_EVENT_PAYLOAD_FIELDS["RungFilled"] = (
    _V2_EVENT_PAYLOAD_FIELDS["RungFilled"] | {"cut_ms"})
_V2_EVENT_PAYLOAD_FIELDS["ReconcileObserved"] = (
    _V2_EVENT_PAYLOAD_FIELDS["ReconcileObserved"]
    - {"read_received_at"})
_V2_SHAPE_SENTINELS = frozenset({
    "schema_version", "ledger", "event_type", "previous_digest",
    "event_digest", "receipt_digest",
})
_V2_MIN_POSITIVE_DECIMAL = V2_MIN_QUANTITY_FLOOR
_V2_DERIVED_CONTEXT_PRECISION = 128
_V2_HEAD_FIELDS = frozenset({"event_count", "last_event_digest"})


def _v2_exact_fields(value: Any, expected: frozenset, path: str) -> None:
    if not isinstance(value, dict):
        raise PositionError("version-2 %s must be an object" % path)
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise PositionError("version-2 %s is missing field(s): %s"
                            % (path, ", ".join(missing)))
    if unknown:
        raise PositionError("version-2 %s has unknown field(s): %s"
                            % (path, ", ".join(unknown)))


def _v2_int(value: Any, path: str, minimum: int = 0,
            maximum: int = MS_SAFE_MAX) -> int:
    if isinstance(value, bool) or not isinstance(value, int) \
            or value < minimum or value > maximum:
        raise PositionError("version-2 %s is not a bounded integer" % path)
    return value


def _v2_epoch_ms(value: Any, path: str) -> int:
    return _v2_int(value, path, V2_MIN_EPOCH_MS, MS_SAFE_MAX)


def _v2_id(value: Any, path: str) -> str:
    if not isinstance(value, str) or V2_ID_RE.fullmatch(value) is None:
        raise PositionError(
            "version-2 %s is not a non-empty bounded ASCII identifier"
            % path)
    return value


def _v2_digest(value: Any, path: str) -> str:
    if not isinstance(value, str) or V2_DIGEST_RE.fullmatch(value) is None:
        raise PositionError("version-2 %s is not a SHA-256 digest" % path)
    return value


def _v2_parse_decimal_input(value: Any, path: str) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str) and value and len(value) <= 128:
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError):
            raise PositionError(
                "version-2 %s is not Decimal text" % path) from None
    else:
        raise PositionError("version-2 %s is not Decimal text" % path)
    if not parsed.is_finite():
        raise PositionError(
            "version-2 %s is not finite Decimal text" % path)
    if len(parsed.as_tuple().digits) > 28:
        raise PositionError(
            "version-2 %s exceeds 28 significant digits" % path)
    if parsed != 0 and abs(parsed) < _V2_MIN_POSITIVE_DECIMAL:
        raise PositionError(
            "version-2 %s is below the minimum nonzero bound" % path)
    return parsed


def _v2_decimal_in_bounds(parsed: Decimal, path: str,
                          minimum: Decimal,
                          maximum: Decimal) -> Decimal:
    if parsed < minimum or parsed > maximum:
        raise PositionError(
            "version-2 %s is outside its explicit bounds" % path)
    return parsed


def _v2_canonical_decimal(parsed: Decimal) -> str:
    if parsed == 0:
        return "0"
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _v2_decimal_text(value: Any, path: str, minimum: Decimal,
                     maximum: Decimal) -> Decimal:
    if not isinstance(value, str):
        raise PositionError("version-2 %s is not Decimal text" % path)
    parsed = _v2_decimal_in_bounds(
        _v2_parse_decimal_input(value, path), path, minimum, maximum)
    if value != _v2_canonical_decimal(parsed):
        raise PositionError(
            "version-2 %s is not canonical Decimal text" % path)
    return parsed


def _v2_decimal_record_value(value: Any, path: str, minimum: Decimal,
                             maximum: Decimal) -> str:
    parsed = _v2_decimal_in_bounds(
        _v2_parse_decimal_input(value, path), path, minimum, maximum)
    return _v2_canonical_decimal(parsed)


def _v2_optional_decimal_value(
        value: Any, path: str, minimum: Decimal, maximum: Decimal,
        require_canonical: bool) -> Optional[str]:
    if value is None:
        return None
    if require_canonical:
        parsed = _v2_decimal_text(value, path, minimum, maximum)
        return _v2_canonical_decimal(parsed)
    return _v2_decimal_record_value(value, path, minimum, maximum)


def _v2_json_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if value < -MS_SAFE_MAX or value > MS_SAFE_MAX:
            raise PositionError(
                "version-2 %s integer exceeds the JSON bound" % path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _v2_json_value(item, "%s[%d]" % (path, index))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise PositionError(
                    "version-2 %s has an invalid JSON key" % path)
            _v2_json_value(item, path + "." + key)
        return
    raise PositionError(
        "version-2 %s is not canonical JSON data" % path)


def _canonical_json_sha256(value: Any, path: str) -> str:
    _v2_json_value(value, path)
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise PositionError(
            "version-2 %s is not canonical JSON data" % path) from None
    return hashlib.sha256(encoded).hexdigest()


def _copy_v2_record(value: Any, path: str) -> Any:
    _v2_json_value(value, path)
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        raise PositionError(
            "version-2 %s is not JSON data" % path) from None


def _normalize_v2_config_snapshot(
        snapshot: Any, require_canonical: bool) -> Dict[str, Any]:
    path = "config_snapshot"
    _v2_exact_fields(snapshot, _V2_CONFIG_FIELDS, path)
    symbol = snapshot["symbol"]
    if not isinstance(symbol, str) or SYMBOL_RE.fullmatch(symbol) is None:
        raise PositionError("version-2 config snapshot symbol is invalid")
    if snapshot["direction"] != V2_DIRECTION:
        raise PositionError(
            "version-2 config snapshot direction is not enabled")
    schedule = snapshot["schedule"]
    if not isinstance(schedule, list) or not schedule \
            or len(schedule) > 1000:
        raise PositionError(
            "version-2 config snapshot schedule is invalid")
    normalized_schedule = []
    prior_gap: Optional[Decimal] = None
    for index, row in enumerate(schedule):
        row_path = "%s.schedule[%d]" % (path, index)
        _v2_exact_fields(row, _V2_CONFIG_SCHEDULE_FIELDS, row_path)
        ordinal = _v2_int(
            row["ordinal"], row_path + ".ordinal", 1, 1000)
        if ordinal != index + 1:
            raise PositionError(
                "version-2 config schedule ordinals are not contiguous")
        if require_canonical:
            gap = _v2_decimal_text(
                row["entry_gap_pct"], row_path + ".entry_gap_pct",
                _V2_MIN_POSITIVE_DECIMAL, V2_MAX_GAP_PCT)
            quote_cap = _v2_decimal_text(
                row["quote_cap"], row_path + ".quote_cap",
                _V2_MIN_POSITIVE_DECIMAL, V2_MAX_DECIMAL)
        else:
            gap_text = _v2_decimal_record_value(
                row["entry_gap_pct"], row_path + ".entry_gap_pct",
                _V2_MIN_POSITIVE_DECIMAL, V2_MAX_GAP_PCT)
            cap_text = _v2_decimal_record_value(
                row["quote_cap"], row_path + ".quote_cap",
                _V2_MIN_POSITIVE_DECIMAL, V2_MAX_DECIMAL)
            gap = Decimal(gap_text)
            quote_cap = Decimal(cap_text)
        if prior_gap is not None and gap <= prior_gap:
            raise PositionError(
                "version-2 config schedule gaps are not increasing")
        normalized_schedule.append({
            "ordinal": ordinal,
            "entry_gap_pct": _v2_canonical_decimal(gap),
            "quote_cap": _v2_canonical_decimal(quote_cap),
        })
        prior_gap = gap

    caps = snapshot["caps"]
    _v2_exact_fields(caps, _V2_CONFIG_CAP_FIELDS, path + ".caps")
    normalized_caps = {}
    for field in sorted(_V2_CONFIG_CAP_FIELDS):
        field_path = path + ".caps." + field
        if require_canonical:
            parsed = _v2_decimal_text(
                caps[field], field_path, _V2_MIN_POSITIVE_DECIMAL,
                V2_MAX_DECIMAL)
        else:
            parsed = Decimal(_v2_decimal_record_value(
                caps[field], field_path, _V2_MIN_POSITIVE_DECIMAL,
                V2_MAX_DECIMAL))
        normalized_caps[field] = _v2_canonical_decimal(parsed)

    venue = snapshot["venue"]
    _v2_exact_fields(venue, _V2_CONFIG_VENUE_FIELDS, path + ".venue")
    if venue["spot"] != "BINANCE_SPOT" \
            or venue["futures"] != "BINANCE_USDM":
        raise PositionError("version-2 config snapshot venue is invalid")
    base_asset = venue["base_asset"]
    quote_asset = venue["quote_asset"]
    if not isinstance(base_asset, str) \
            or re.fullmatch(r"[A-Z0-9]{2,20}", base_asset) is None \
            or not isinstance(quote_asset, str) \
            or re.fullmatch(r"[A-Z0-9]{2,20}", quote_asset) is None \
            or base_asset == quote_asset \
            or symbol != base_asset + quote_asset:
        raise PositionError(
            "version-2 config snapshot asset binding is invalid")
    return {
        "symbol": symbol,
        "direction": V2_DIRECTION,
        "schedule": normalized_schedule,
        "caps": normalized_caps,
        "venue": {
            "spot": venue["spot"],
            "futures": venue["futures"],
            "base_asset": base_asset,
            "quote_asset": quote_asset,
        },
    }


def new_v2_config_snapshot_event(
        config_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Return canonical event-zero content; append computes both hashes."""
    normalized = _normalize_v2_config_snapshot(
        config_snapshot, require_canonical=False)
    return {"event_type": "ConfigSnapshot", **normalized}


def v2_config_epoch_digest(config_snapshot: Dict[str, Any]) -> str:
    """Return the digest used by the ConfigSnapshot event at ledger zero."""
    payload = new_v2_config_snapshot_event(config_snapshot)
    sealed = {
        **payload,
        "cut_ms": V2_GENESIS_CUT_MS,
        "previous_digest": V2_GENESIS_DIGEST,
    }
    return _canonical_json_sha256(sealed, "ConfigSnapshot")


def _normalize_v2_reduction_facts(
        facts: Any, path: str, require_canonical: bool) -> Dict[str, Any]:
    _v2_exact_fields(facts, _V2_REDUCTION_FACT_FIELDS, path)
    ordinal = _v2_int(facts["ordinal"], path + ".ordinal", 1, 1000)
    values = {}
    for field in ("spot_quantity", "short_quantity"):
        field_path = path + "." + field
        if require_canonical:
            parsed = _v2_decimal_text(
                facts[field], field_path, Decimal(0), V2_MAX_DECIMAL)
        else:
            parsed = Decimal(_v2_decimal_record_value(
                facts[field], field_path, Decimal(0), V2_MAX_DECIMAL))
        values[field] = _v2_canonical_decimal(parsed)
    if Decimal(values["spot_quantity"]) == 0 \
            and Decimal(values["short_quantity"]) == 0:
        raise PositionError(
            "version-2 reduction receipt amounts are both zero")
    return {"ordinal": ordinal, **values}


def _normalize_v2_flat_facts(
        facts: Any, path: str, require_canonical: bool) -> Dict[str, Any]:
    _v2_exact_fields(facts, _V2_FLAT_FACT_FIELDS, path)
    values = {}
    for field in (
            "futures_position_quantity", "spot_sellable_base_quantity"):
        field_path = path + "." + field
        if require_canonical:
            parsed = _v2_decimal_text(
                facts[field], field_path, -V2_MAX_DECIMAL, V2_MAX_DECIMAL)
        else:
            parsed = Decimal(_v2_decimal_record_value(
                facts[field], field_path, -V2_MAX_DECIMAL,
                V2_MAX_DECIMAL))
        values[field] = _v2_canonical_decimal(parsed)
    counts = {}
    for field in (
            "standard_open_order_count", "algorithmic_open_order_count"):
        counts[field] = _v2_int(
            facts[field], path + "." + field, 0, 1000000)
    if not isinstance(facts["own_orders_terminal"], bool):
        raise PositionError(
            "version-2 flat receipt own_orders_terminal is not boolean")
    digests = {}
    for field in (
            "positions_digest", "open_orders_digest", "own_orders_digest"):
        digests[field] = _v2_digest(facts[field], path + "." + field)
    return {
        **values,
        **counts,
        "own_orders_terminal": facts["own_orders_terminal"],
        **digests,
    }


def _normalize_v2_receipt_facts(
        receipt_kind: str, facts: Any, path: str,
        require_canonical: bool) -> Dict[str, Any]:
    if receipt_kind == "REDUCTION":
        return _normalize_v2_reduction_facts(
            facts, path, require_canonical)
    if receipt_kind == "FLAT":
        return _normalize_v2_flat_facts(facts, path, require_canonical)
    raise PositionError("version-2 receipt kind is invalid")


def _v2_receipt_digest_payload(receipt: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "receipt_id": receipt["receipt_id"],
        "receipt_kind": receipt["receipt_kind"],
        "observed_at_ms": receipt["observed_at_ms"],
        "source": receipt["source"],
        "facts": receipt["facts"],
    }


def v2_receipt_digest(receipt: Dict[str, Any]) -> str:
    """Return the module digest for one typed observed receipt."""
    if not isinstance(receipt, dict):
        raise PositionError("version-2 receipt must be an object")
    required = _V2_RECEIPT_FIELDS - {"receipt_digest"}
    missing = sorted(required - set(receipt))
    allowed = required | {"receipt_digest"}
    unknown = sorted(set(receipt) - allowed)
    if missing:
        raise PositionError(
            "version-2 receipt digest input is missing field(s): %s"
            % ", ".join(missing))
    if unknown:
        raise PositionError(
            "version-2 receipt digest input has unknown field(s): %s"
            % ", ".join(unknown))
    receipt_id = _v2_id(receipt["receipt_id"], "receipt.receipt_id")
    kind = receipt["receipt_kind"]
    if kind not in V2_RECEIPT_KINDS:
        raise PositionError("version-2 receipt kind is invalid")
    observed = _v2_epoch_ms(
        receipt["observed_at_ms"], "receipt.observed_at_ms")
    source = _v2_id(receipt["source"], "receipt.source")
    facts = _normalize_v2_receipt_facts(
        kind, receipt["facts"], "receipt.facts", require_canonical=True)
    payload = {
        "receipt_id": receipt_id,
        "receipt_kind": kind,
        "observed_at_ms": observed,
        "source": source,
        "facts": facts,
    }
    return _canonical_json_sha256(payload, "receipt")


def new_v2_receipt_observed_event(
        receipt_id: str, receipt_kind: str, observed_at_ms: int,
        source: str, facts: Dict[str, Any]) -> Dict[str, Any]:
    """Return a typed registry event; its receipt digest is module-owned."""
    receipt_id = _v2_id(receipt_id, "receipt.receipt_id")
    if receipt_kind not in V2_RECEIPT_KINDS:
        raise PositionError("version-2 receipt kind is invalid")
    observed = _v2_epoch_ms(observed_at_ms, "receipt.observed_at_ms")
    source = _v2_id(source, "receipt.source")
    normalized_facts = _normalize_v2_receipt_facts(
        receipt_kind, facts, "receipt.facts", require_canonical=False)
    receipt = {
        "receipt_id": receipt_id,
        "receipt_kind": receipt_kind,
        "observed_at_ms": observed,
        "source": source,
        "facts": normalized_facts,
    }
    receipt["receipt_digest"] = _canonical_json_sha256(
        _v2_receipt_digest_payload(receipt), "receipt")
    return {"event_type": "ReceiptObserved", **receipt}


def _normalize_v2_reconcile_positions(
        value: Any, path: str, require_canonical: bool,
        require_known: bool) -> Dict[str, Optional[str]]:
    _v2_exact_fields(value, _V2_RECONCILE_POSITION_FIELDS, path)
    normalized: Dict[str, Optional[str]] = {}
    for field in sorted(_V2_RECONCILE_POSITION_FIELDS):
        field_path = path + "." + field
        normalized[field] = _v2_optional_decimal_value(
            value[field], field_path, -V2_MAX_DECIMAL, V2_MAX_DECIMAL,
            require_canonical)
        if require_known and normalized[field] is None:
            raise PositionError(
                "version-2 reconcile baseline quantity is unknown")
    return normalized


def _normalize_v2_reconcile_open_orders(
        value: Any, path: str,
        require_canonical: bool) -> Dict[str, Optional[List[str]]]:
    _v2_exact_fields(value, _V2_RECONCILE_OPEN_ORDER_FIELDS, path)
    normalized: Dict[str, Optional[List[str]]] = {}
    for field in sorted(_V2_RECONCILE_OPEN_ORDER_FIELDS):
        rows = value[field]
        if rows is None:
            normalized[field] = None
            continue
        if not isinstance(rows, list) or len(rows) > 10000:
            raise PositionError(
                "version-2 %s must be a bounded list or null" %
                (path + "." + field))
        ids = [
            _v2_id(client_id, "%s.%s[%d]" % (path, field, index))
            for index, client_id in enumerate(rows)
        ]
        canonical = sorted(ids)
        if len(set(canonical)) != len(canonical):
            raise PositionError(
                "version-2 reconcile open-order id is duplicated")
        if require_canonical and ids != canonical:
            raise PositionError(
                "version-2 reconcile open-order ids are not canonical")
        normalized[field] = canonical
    return normalized


def _normalize_v2_reconcile_own_orders(
        value: Any, path: str,
        require_canonical: bool) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 10000:
        raise PositionError(
            "version-2 reconcile own_orders must be a bounded list")
    normalized = []
    for index, row in enumerate(value):
        row_path = "%s[%d]" % (path, index)
        _v2_exact_fields(row, _V2_RECONCILE_OWN_ORDER_FIELDS, row_path)
        client_id = _v2_id(
            row["client_order_id"], row_path + ".client_order_id")
        market = _v2_id(row["market"], row_path + ".market")
        if market not in ("BINANCE_SPOT", "BINANCE_USDM"):
            raise PositionError(
                "version-2 reconcile own-order market is invalid")
        status = _v2_id(row["status"], row_path + ".status")
        quantity = _v2_optional_decimal_value(
            row["executed_quantity"], row_path + ".executed_quantity",
            Decimal(0), V2_MAX_DECIMAL, require_canonical)
        if status in V2_TERMINAL_ORDER_STATUSES and quantity is None:
            raise PositionError(
                "version-2 terminal own order lacks executed quantity")
        if status == "NOT_FOUND" and quantity is not None:
            raise PositionError(
                "version-2 missing own order has a quantity")
        if status == "UNKNOWN" and quantity is not None:
            raise PositionError(
                "version-2 unknown own order has a quantity")
        normalized.append({
            "client_order_id": client_id,
            "market": market,
            "status": status,
            "executed_quantity": quantity,
        })
    canonical = sorted(
        normalized, key=lambda row: (row["client_order_id"], row["market"]))
    client_ids = [row["client_order_id"] for row in canonical]
    if len(set(client_ids)) != len(client_ids):
        raise PositionError(
            "version-2 reconcile own-order id is duplicated")
    if require_canonical and normalized != canonical:
        raise PositionError(
            "version-2 reconcile own_orders are not canonical")
    return canonical


def _normalize_v2_reconcile_balances(
        value: Any, path: str,
        require_canonical: bool) -> Dict[str, Optional[str]]:
    _v2_exact_fields(value, _V2_RECONCILE_BALANCE_FIELDS, path)
    return {
        field: _v2_optional_decimal_value(
            value[field], path + "." + field,
            Decimal(0), V2_MAX_DECIMAL, require_canonical)
        for field in sorted(_V2_RECONCILE_BALANCE_FIELDS)
    }


def _normalize_v2_reconcile_observation(
        value: Any, require_canonical: bool) -> Dict[str, Any]:
    path = "ReconcileObserved"
    _v2_exact_fields(value, _V2_RECONCILE_FACT_FIELDS, path)
    return {
        "reconcile_id": _v2_id(
            value["reconcile_id"], path + ".reconcile_id"),
        "observed_at_ms": _v2_epoch_ms(
            value["observed_at_ms"], path + ".observed_at_ms"),
        "source": _v2_id(value["source"], path + ".source"),
        "baseline": _normalize_v2_reconcile_positions(
            value["baseline"], path + ".baseline",
            require_canonical, require_known=True),
        "positions": _normalize_v2_reconcile_positions(
            value["positions"], path + ".positions",
            require_canonical, require_known=False),
        "open_orders": _normalize_v2_reconcile_open_orders(
            value["open_orders"], path + ".open_orders",
            require_canonical),
        "own_orders": _normalize_v2_reconcile_own_orders(
            value["own_orders"], path + ".own_orders",
            require_canonical),
        "balances": _normalize_v2_reconcile_balances(
            value["balances"], path + ".balances",
            require_canonical),
    }


def _v2_reconcile_read_digest(value: Dict[str, Any]) -> str:
    return _canonical_json_sha256(
        {field: value[field]
         for field in sorted(_V2_RECONCILE_FACT_FIELDS)},
        "reconcile read")


def _validate_v2_reconcile_read_digest(value: Dict[str, Any],
                                       path: str) -> None:
    supplied = _v2_digest(
        value["read_digest"], path + ".read_digest")
    if supplied != _v2_reconcile_read_digest(value):
        raise PositionError(
            "version-2 reconcile read digest does not match its facts")


def new_v2_reconcile_observed_event(
        reconcile_id: str, observed_at_ms: int, source: str,
        baseline: Dict[str, Any], positions: Dict[str, Any],
        open_orders: Dict[str, Any], own_orders: List[Dict[str, Any]],
        balances: Dict[str, Any]) -> Dict[str, Any]:
    """Return a typed private read with its facts and source time bound."""
    normalized = _normalize_v2_reconcile_observation({
        "reconcile_id": reconcile_id,
        "observed_at_ms": observed_at_ms,
        "source": source,
        "baseline": baseline,
        "positions": positions,
        "open_orders": open_orders,
        "own_orders": own_orders,
        "balances": balances,
    }, require_canonical=False)
    normalized["read_digest"] = _v2_reconcile_read_digest(normalized)
    return {"event_type": "ReconcileObserved", **normalized}


def _validate_v2_fee_map(value: Any, path: str) -> Dict[str, Decimal]:
    if not isinstance(value, dict) or len(value) > 100:
        raise PositionError(
            "version-2 %s must be a bounded object" % path)
    parsed = {}
    for asset, amount in value.items():
        if not isinstance(asset, str) \
                or re.fullmatch(r"[A-Z0-9]{2,20}", asset) is None:
            raise PositionError("version-2 %s asset is invalid" % path)
        parsed[asset] = _v2_decimal_text(
            amount, path + "." + asset, Decimal(0), V2_MAX_DECIMAL)
    return parsed


def _normalize_v2_fee_map(value: Any, path: str) -> Dict[str, str]:
    if not isinstance(value, dict) or len(value) > 100:
        raise PositionError(
            "version-2 %s must be a bounded object" % path)
    normalized = {}
    for asset, amount in value.items():
        if not isinstance(asset, str) \
                or re.fullmatch(r"[A-Z0-9]{2,20}", asset) is None:
            raise PositionError("version-2 %s asset is invalid" % path)
        normalized[asset] = _v2_decimal_record_value(
            amount, path + "." + asset, Decimal(0), V2_MAX_DECIMAL)
    return normalized


def _normalize_v2_order_leg(
        fill: Any, expected_stage: str, expected_market: str, path: str,
        require_canonical: bool) -> Dict[str, Any]:
    _v2_exact_fields(fill, _V2_LEG_FIELDS, path)
    if fill["stage"] != expected_stage:
        raise PositionError(
            "version-2 %s stage does not match its leg" % path)
    market = _v2_id(fill["market"], path + ".market")
    if market != expected_market:
        raise PositionError(
            "version-2 %s market does not match its leg" % path)
    client_id = _v2_id(
        fill["client_order_id"], path + ".client_order_id")
    exchange_id = _v2_id(
        fill["exchange_order_id"], path + ".exchange_order_id")
    status = fill["terminal_status"]
    if status not in V2_TERMINAL_ORDER_STATUSES:
        raise PositionError(
            "version-2 %s terminal status is invalid" % path)
    if require_canonical:
        quantity = _v2_decimal_text(
            fill["executed_quantity"], path + ".executed_quantity",
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_DECIMAL)
        quote = _v2_decimal_text(
            fill["executed_quote"], path + ".executed_quote",
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_DECIMAL)
        fees = {
            key: _v2_canonical_decimal(amount)
            for key, amount in _validate_v2_fee_map(
                fill["fee_by_asset"], path + ".fee_by_asset").items()
        }
    else:
        quantity = Decimal(_v2_decimal_record_value(
            fill["executed_quantity"], path + ".executed_quantity",
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_DECIMAL))
        quote = Decimal(_v2_decimal_record_value(
            fill["executed_quote"], path + ".executed_quote",
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_DECIMAL))
        fees = _normalize_v2_fee_map(
            fill["fee_by_asset"], path + ".fee_by_asset")
    return {
        "stage": expected_stage,
        "market": market,
        "client_order_id": client_id,
        "exchange_order_id": exchange_id,
        "terminal_status": status,
        "executed_quantity": _v2_canonical_decimal(quantity),
        "executed_quote": _v2_canonical_decimal(quote),
        "fee_by_asset": fees,
    }


def _normalize_v2_leg(fill: Any, role: str, path: str,
                      require_canonical: bool) -> Dict[str, Any]:
    expected_stage = ENTER_SPOT if role == "spot" else ENTER_SHORT
    expected_market = "BINANCE_SPOT" if role == "spot" else "BINANCE_USDM"
    return _normalize_v2_order_leg(
        fill, expected_stage, expected_market, path, require_canonical)


def _normalize_v2_unwind_leg(
        fill: Any, path: str, require_canonical: bool) -> Dict[str, Any]:
    return _normalize_v2_order_leg(
        fill, UNWIND_SPOT, "BINANCE_SPOT", path, require_canonical)


def _normalize_v2_dislocation(
        dislocation: Any, path: str,
        require_canonical: bool) -> Dict[str, Any]:
    _v2_exact_fields(dislocation, _V2_DISLOCATION_FIELDS, path)
    event_id = _v2_id(dislocation["event_id"], path + ".event_id")
    source_time = _v2_epoch_ms(
        dislocation["source_time_ms"], path + ".source_time_ms")
    if require_canonical:
        entry_gap = _v2_decimal_text(
            dislocation["entry_gap_pct"], path + ".entry_gap_pct",
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_GAP_PCT)
        exit_gap = _v2_decimal_text(
            dislocation["exit_gap_pct"], path + ".exit_gap_pct",
            -V2_MAX_GAP_PCT, V2_MAX_GAP_PCT)
        funding = _v2_decimal_text(
            dislocation["funding_rate"], path + ".funding_rate",
            -V2_MAX_RATE, V2_MAX_RATE)
    else:
        entry_gap = Decimal(_v2_decimal_record_value(
            dislocation["entry_gap_pct"], path + ".entry_gap_pct",
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_GAP_PCT))
        exit_gap = Decimal(_v2_decimal_record_value(
            dislocation["exit_gap_pct"], path + ".exit_gap_pct",
            -V2_MAX_GAP_PCT, V2_MAX_GAP_PCT))
        funding = Decimal(_v2_decimal_record_value(
            dislocation["funding_rate"], path + ".funding_rate",
            -V2_MAX_RATE, V2_MAX_RATE))
    funding_period = _v2_int(
        dislocation["funding_period_ms"], path + ".funding_period_ms",
        1, MS_SAFE_MAX)
    normalized = {
        "event_id": event_id,
        "source_time_ms": source_time,
        "entry_gap_pct": _v2_canonical_decimal(entry_gap),
        "exit_gap_pct": _v2_canonical_decimal(exit_gap),
        "funding_rate": _v2_canonical_decimal(funding),
        "funding_period_ms": funding_period,
    }
    expected_digest = _canonical_json_sha256(
        normalized, "dislocation")
    if require_canonical:
        supplied = _v2_digest(
            dislocation["digest"], path + ".digest")
        if supplied != expected_digest:
            raise PositionError(
                "version-2 dislocation digest does not match its facts")
    normalized["digest"] = expected_digest
    return normalized


def new_v2_dislocation_event(
        event_id: str, source_time_ms: int, entry_gap_pct: Any,
        exit_gap_pct: Any, funding_rate: Any,
        funding_period_ms: int) -> Dict[str, Any]:
    raw = {
        "event_id": event_id,
        "source_time_ms": source_time_ms,
        "entry_gap_pct": _v2_decimal_record_value(
            entry_gap_pct, "dislocation.entry_gap_pct",
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_GAP_PCT),
        "exit_gap_pct": _v2_decimal_record_value(
            exit_gap_pct, "dislocation.exit_gap_pct",
            -V2_MAX_GAP_PCT, V2_MAX_GAP_PCT),
        "funding_rate": _v2_decimal_record_value(
            funding_rate, "dislocation.funding_rate",
            -V2_MAX_RATE, V2_MAX_RATE),
        "funding_period_ms": funding_period_ms,
    }
    _v2_exact_fields(raw, _V2_DISLOCATION_INPUT_FIELDS, "dislocation")
    raw["event_id"] = _v2_id(raw["event_id"], "dislocation.event_id")
    raw["source_time_ms"] = _v2_epoch_ms(
        raw["source_time_ms"], "dislocation.source_time_ms")
    raw["funding_period_ms"] = _v2_int(
        raw["funding_period_ms"], "dislocation.funding_period_ms",
        1, MS_SAFE_MAX)
    raw["digest"] = _canonical_json_sha256(raw, "dislocation")
    return raw


def _normalize_v2_entry_intent(
        intent: Any, role: str, path: str,
        require_canonical: bool) -> Dict[str, str]:
    fields = _V2_SPOT_INTENT_FIELDS if role == "spot" \
        else _V2_SHORT_INTENT_FIELDS
    _v2_exact_fields(intent, fields, path)
    symbol = intent["symbol"]
    if not isinstance(symbol, str) or SYMBOL_RE.fullmatch(symbol) is None:
        raise PositionError("version-2 %s symbol is invalid" % path)
    client_id = _v2_id(
        intent["newClientOrderId"], path + ".newClientOrderId")
    expected = {
        "spot": ("spot", "BUY", "quoteOrderQty"),
        "short": ("usdm", "SELL", "quantity"),
    }[role]
    market, side, amount_field = expected
    if intent["market"] != market or intent["side"] != side \
            or intent["type"] != "MARKET":
        raise PositionError(
            "version-2 %s is not the canonical %s entry intent"
            % (path, role))
    if require_canonical:
        amount = _v2_decimal_text(
            intent[amount_field], path + "." + amount_field,
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_DECIMAL)
    else:
        amount = Decimal(_v2_decimal_record_value(
            intent[amount_field], path + "." + amount_field,
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_DECIMAL))
    return {
        "symbol": symbol,
        "newClientOrderId": client_id,
        "market": market,
        "side": side,
        "type": "MARKET",
        amount_field: _v2_canonical_decimal(amount),
    }


def _normalize_v2_unwind_intent(
        intent: Any, path: str,
        require_canonical: bool) -> Dict[str, str]:
    _v2_exact_fields(intent, _V2_UNWIND_INTENT_FIELDS, path)
    symbol = intent["symbol"]
    if not isinstance(symbol, str) or SYMBOL_RE.fullmatch(symbol) is None:
        raise PositionError("version-2 %s symbol is invalid" % path)
    client_id = _v2_id(
        intent["newClientOrderId"], path + ".newClientOrderId")
    if intent["market"] != "spot" or intent["side"] != "SELL" \
            or intent["type"] != "MARKET":
        raise PositionError(
            "version-2 %s is not the canonical Spot unwind intent" % path)
    if require_canonical:
        amount = _v2_decimal_text(
            intent["quantity"], path + ".quantity",
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_DECIMAL)
    else:
        amount = Decimal(_v2_decimal_record_value(
            intent["quantity"], path + ".quantity",
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_DECIMAL))
    return {
        "symbol": symbol,
        "newClientOrderId": client_id,
        "market": "spot",
        "side": "SELL",
        "type": "MARKET",
        "quantity": _v2_canonical_decimal(amount),
    }


def new_v2_add_intent_event(
        ordinal: int, run_id: str, proposal_id: str,
        proposal_digest: str, config_event_digest: str,
        effective_cap_quote: Any, schedule_entry_gap_pct: Any,
        dislocation: Dict[str, Any], intent: Dict[str, Any]
        ) -> Dict[str, Any]:
    return {
        "event_type": "AddIntent",
        "ordinal": _v2_int(ordinal, "AddIntent.ordinal", 1, 1000),
        "run_id": _v2_id(run_id, "AddIntent.run_id"),
        "proposal_id": _v2_id(
            proposal_id, "AddIntent.proposal_id"),
        "proposal_digest": _v2_digest(
            proposal_digest, "AddIntent.proposal_digest"),
        "config_event_digest": _v2_digest(
            config_event_digest, "AddIntent.config_event_digest"),
        "effective_cap_quote": _v2_decimal_record_value(
            effective_cap_quote, "AddIntent.effective_cap_quote",
            _V2_MIN_POSITIVE_DECIMAL, FIRST_TRADE_CAP_USDT),
        "schedule_entry_gap_pct": _v2_decimal_record_value(
            schedule_entry_gap_pct, "AddIntent.schedule_entry_gap_pct",
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_GAP_PCT),
        "dislocation": _normalize_v2_dislocation(
            dislocation, "AddIntent.dislocation",
            require_canonical=True),
        "intent": _normalize_v2_entry_intent(
            intent, "spot", "AddIntent.intent",
            require_canonical=False),
    }


def new_v2_add_hedge_intent_event(
        ordinal: int, run_id: str, spot_leg: Dict[str, Any],
        intent: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event_type": "AddHedgeIntent",
        "ordinal": _v2_int(
            ordinal, "AddHedgeIntent.ordinal", 1, 1000),
        "run_id": _v2_id(run_id, "AddHedgeIntent.run_id"),
        "spot_leg": _normalize_v2_leg(
            spot_leg, "spot", "AddHedgeIntent.spot_leg",
            require_canonical=False),
        "intent": _normalize_v2_entry_intent(
            intent, "short", "AddHedgeIntent.intent",
            require_canonical=False),
    }


def new_v2_add_unwind_intent_event(
        ordinal: int, run_id: str, spot_leg: Dict[str, Any],
        short_status: str, net_spot_quantity: Any,
        sellable_quantity: Any, initial_dust_quantity: Any,
        intent: Dict[str, Any]) -> Dict[str, Any]:
    if short_status not in _V2_ADD_SHORT_TERMINAL_STATUSES:
        raise PositionError(
            "version-2 AddUnwindIntent short status is invalid")
    net = Decimal(_v2_decimal_record_value(
        net_spot_quantity, "AddUnwindIntent.net_spot_quantity",
        _V2_MIN_POSITIVE_DECIMAL, V2_MAX_DECIMAL))
    sellable = Decimal(_v2_decimal_record_value(
        sellable_quantity, "AddUnwindIntent.sellable_quantity",
        _V2_MIN_POSITIVE_DECIMAL, V2_MAX_DECIMAL))
    dust = Decimal(_v2_decimal_record_value(
        initial_dust_quantity, "AddUnwindIntent.initial_dust_quantity",
        Decimal(0), V2_MAX_DECIMAL))
    if sellable > net or _v2_decimal_subtract(net, sellable) != dust:
        raise PositionError(
            "version-2 AddUnwindIntent quantities do not reconcile")
    normalized_intent = _normalize_v2_unwind_intent(
        intent, "AddUnwindIntent.intent", require_canonical=False)
    if Decimal(normalized_intent["quantity"]) != sellable:
        raise PositionError(
            "version-2 AddUnwindIntent wire quantity is not sellable quantity")
    return {
        "event_type": "AddUnwindIntent",
        "ordinal": _v2_int(
            ordinal, "AddUnwindIntent.ordinal", 1, 1000),
        "run_id": _v2_id(run_id, "AddUnwindIntent.run_id"),
        "spot_leg": _normalize_v2_leg(
            spot_leg, "spot", "AddUnwindIntent.spot_leg",
            require_canonical=False),
        "short_status": short_status,
        "net_spot_quantity": _v2_canonical_decimal(net),
        "sellable_quantity": _v2_canonical_decimal(sellable),
        "initial_dust_quantity": _v2_canonical_decimal(dust),
        "intent": normalized_intent,
    }


def new_v2_add_aborted_event(
        ordinal: int, run_id: str, reason: str,
        spot_status: str, short_status: str,
        spot_net_quantity: Any, unwind_requested_quantity: Any,
        unwind_filled_quantity: Any, dust_quantity: Any,
        spot_leg: Optional[Dict[str, Any]],
        unwind_leg: Optional[Dict[str, Any]],
        unwind_base_fee_quantity: Any = Decimal(0)) -> Dict[str, Any]:
    reason = _v2_id(reason, "AddAborted.reason")
    if spot_status not in _V2_ADD_ABORT_SPOT_STATUSES:
        raise PositionError("version-2 AddAborted Spot status is invalid")
    if short_status not in _V2_ADD_SHORT_TERMINAL_STATUSES:
        raise PositionError("version-2 AddAborted short status is invalid")
    quantities = {}
    for field, value in (
            ("spot_net_quantity", spot_net_quantity),
            ("unwind_requested_quantity", unwind_requested_quantity),
            ("unwind_filled_quantity", unwind_filled_quantity),
            ("unwind_base_fee_quantity", unwind_base_fee_quantity),
            ("dust_quantity", dust_quantity)):
        quantities[field] = Decimal(_v2_decimal_record_value(
            value, "AddAborted." + field, Decimal(0), V2_MAX_DECIMAL))
    net = quantities["spot_net_quantity"]
    requested = quantities["unwind_requested_quantity"]
    filled = quantities["unwind_filled_quantity"]
    base_fee = quantities["unwind_base_fee_quantity"]
    dust = quantities["dust_quantity"]
    normalized_unwind = None
    normalized_spot = None
    if spot_leg is not None:
        normalized_spot = _normalize_v2_leg(
            spot_leg, "spot", "AddAborted.spot_leg",
            require_canonical=False)
    if unwind_leg is not None:
        normalized_unwind = _normalize_v2_unwind_leg(
            unwind_leg, "AddAborted.unwind_leg", require_canonical=False)
        if Decimal(normalized_unwind["executed_quantity"]) != filled:
            raise PositionError(
                "version-2 AddAborted unwind fill does not match its leg")
    if spot_status in ("NOT_FOUND", "NOT_SUBMITTED"):
        if short_status != "NOT_SEALED" or any(
                value != 0 for value in quantities.values()) \
                or normalized_spot is not None or normalized_unwind is not None:
            raise PositionError(
                "version-2 missing Spot ADD abort has exposure facts")
    else:
        if normalized_spot is None:
            raise PositionError(
                "version-2 filled Spot ADD abort lacks its measured leg")
        if net <= 0 or requested > net or filled > requested \
                or filled + base_fee > net \
                or _v2_decimal_subtract(
                    _v2_decimal_subtract(net, filled), base_fee) != dust:
            raise PositionError(
                "version-2 AddAborted measured quantities do not reconcile")
        if normalized_unwind is None and (requested != 0 or filled != 0):
            raise PositionError(
                "version-2 AddAborted unwind quantities lack a receipt")
        if normalized_unwind is not None and requested <= 0:
            raise PositionError(
                "version-2 AddAborted unwind receipt lacks an intent amount")
    return {
        "event_type": "AddAborted",
        "ordinal": _v2_int(ordinal, "AddAborted.ordinal", 1, 1000),
        "run_id": _v2_id(run_id, "AddAborted.run_id"),
        "reason": reason,
        "spot_status": spot_status,
        "short_status": short_status,
        **{field: _v2_canonical_decimal(value)
           for field, value in quantities.items()},
        "spot_leg": normalized_spot,
        "unwind_leg": normalized_unwind,
    }


def new_v2_rung_filled_event(
        ordinal: int, legs: Dict[str, Any],
        dislocation: Dict[str, Any], cut_ms: int) -> Dict[str, Any]:
    ordinal = _v2_int(ordinal, "RungFilled.ordinal", 1, 1000)
    _v2_exact_fields(legs, _V2_LEGS_FIELDS, "RungFilled.legs")
    normalized_legs = {
        role: _normalize_v2_leg(
            legs[role], role, "RungFilled.legs." + role,
            require_canonical=False)
        for role in ("spot", "short")
    }
    normalized_dislocation = _normalize_v2_dislocation(
        dislocation, "RungFilled.dislocation", require_canonical=True)
    cut = _v2_epoch_ms(cut_ms, "RungFilled.cut_ms")
    if normalized_dislocation["source_time_ms"] > cut:
        raise PositionError(
            "version-2 dislocation source time is later than its cut")
    return {
        "event_type": "RungFilled",
        "ordinal": ordinal,
        "legs": normalized_legs,
        "dislocation": normalized_dislocation,
        "cut_ms": cut,
    }


def _normalize_v2_reduction_amounts(
        amounts: Any, path: str,
        require_canonical: bool) -> Dict[str, str]:
    _v2_exact_fields(amounts, _V2_REDUCTION_AMOUNT_FIELDS, path)
    normalized = {}
    for field in ("spot_quantity", "short_quantity"):
        field_path = path + "." + field
        if require_canonical:
            parsed = _v2_decimal_text(
                amounts[field], field_path, Decimal(0), V2_MAX_DECIMAL)
        else:
            parsed = Decimal(_v2_decimal_record_value(
                amounts[field], field_path, Decimal(0), V2_MAX_DECIMAL))
        normalized[field] = _v2_canonical_decimal(parsed)
    if Decimal(normalized["spot_quantity"]) == 0 \
            and Decimal(normalized["short_quantity"]) == 0:
        raise PositionError(
            "version-2 RungReduced amounts are both zero")
    return normalized


def new_v2_rung_reduced_event(
        ordinal: int, receipt_ids: List[str],
        amounts: Dict[str, Any]) -> Dict[str, Any]:
    ordinal = _v2_int(ordinal, "RungReduced.ordinal", 1, 1000)
    if not isinstance(receipt_ids, list) or not receipt_ids \
            or len(receipt_ids) > 1000:
        raise PositionError(
            "version-2 RungReduced receipt_ids must be a bounded non-empty list")
    normalized_ids = sorted(
        _v2_id(value, "RungReduced.receipt_ids[%d]" % index)
        for index, value in enumerate(receipt_ids))
    if len(set(normalized_ids)) != len(normalized_ids):
        raise PositionError(
            "version-2 RungReduced receipt id is duplicated")
    return {
        "event_type": "RungReduced",
        "ordinal": ordinal,
        "receipt_ids": normalized_ids,
        "amounts": _normalize_v2_reduction_amounts(
            amounts, "RungReduced.amounts", require_canonical=False),
    }


def new_v2_flat_proved_event(receipt_id: str) -> Dict[str, Any]:
    """Return a request; append_v2_event supplies the module clock."""
    return {
        "event_type": "FlatProved",
        "receipt_id": _v2_id(
            receipt_id, "FlatProved.receipt_id"),
    }


def v2_event_digest(event: Dict[str, Any]) -> str:
    """Compute an event digest from all fields except event_digest."""
    if not isinstance(event, dict):
        raise PositionError("version-2 event must be an object")
    if "event_digest" not in event:
        raise PositionError(
            "version-2 event digest input lacks event_digest")
    payload = dict(event)
    del payload["event_digest"]
    return _canonical_json_sha256(payload, "ledger event")


def _validate_v2_event_shape(event: Any, index: int) -> None:
    path = "ledger[%d]" % index
    if not isinstance(event, dict):
        raise PositionError("version-2 %s must be an object" % path)
    event_type = event.get("event_type")
    if event_type not in V2_EVENT_TYPES:
        raise PositionError("version-2 %s event type is invalid" % path)
    _v2_exact_fields(event, _V2_EVENT_FIELDS[event_type], path)
    cut = _v2_epoch_ms(event["cut_ms"], path + ".cut_ms")
    _v2_digest(event["previous_digest"], path + ".previous_digest")
    stored_digest = _v2_digest(
        event["event_digest"], path + ".event_digest")

    if event_type == "ConfigSnapshot":
        if cut != V2_GENESIS_CUT_MS:
            raise PositionError(
                "version-2 ConfigSnapshot cut is not the genesis cut")
        normalized = _normalize_v2_config_snapshot(
            {key: event[key] for key in _V2_CONFIG_FIELDS},
            require_canonical=True)
        if normalized != {
                key: event[key] for key in _V2_CONFIG_FIELDS}:
            raise PositionError(
                "version-2 ConfigSnapshot is not canonical")
    elif event_type == "ReceiptObserved":
        _v2_id(event["receipt_id"], path + ".receipt_id")
        if event["receipt_kind"] not in V2_RECEIPT_KINDS:
            raise PositionError(
                "version-2 ReceiptObserved kind is invalid")
        _v2_epoch_ms(event["observed_at_ms"], path + ".observed_at_ms")
        if cut != event["observed_at_ms"]:
            raise PositionError(
                "version-2 ReceiptObserved cut does not match observation")
        _v2_id(event["source"], path + ".source")
        normalized_facts = _normalize_v2_receipt_facts(
            event["receipt_kind"], event["facts"], path + ".facts",
            require_canonical=True)
        if normalized_facts != event["facts"]:
            raise PositionError(
                "version-2 ReceiptObserved facts are not canonical")
        supplied = _v2_digest(
            event["receipt_digest"], path + ".receipt_digest")
        receipt_value = {
            key: event[key] for key in _V2_RECEIPT_FIELDS
        }
        if supplied != v2_receipt_digest(receipt_value):
            raise PositionError(
                "version-2 receipt digest does not match its facts")
    elif event_type == "AddIntent":
        _v2_int(event["ordinal"], path + ".ordinal", 1, 1000)
        _v2_id(event["run_id"], path + ".run_id")
        _v2_id(event["proposal_id"], path + ".proposal_id")
        _v2_digest(event["proposal_digest"], path + ".proposal_digest")
        _v2_digest(
            event["config_event_digest"],
            path + ".config_event_digest")
        _v2_decimal_text(
            event["effective_cap_quote"],
            path + ".effective_cap_quote",
            _V2_MIN_POSITIVE_DECIMAL, FIRST_TRADE_CAP_USDT)
        _v2_decimal_text(
            event["schedule_entry_gap_pct"],
            path + ".schedule_entry_gap_pct",
            _V2_MIN_POSITIVE_DECIMAL, V2_MAX_GAP_PCT)
        normalized_dislocation = _normalize_v2_dislocation(
            event["dislocation"], path + ".dislocation",
            require_canonical=True)
        if normalized_dislocation != event["dislocation"]:
            raise PositionError(
                "version-2 AddIntent dislocation is not canonical")
        normalized_intent = _normalize_v2_entry_intent(
            event["intent"], "spot", path + ".intent",
            require_canonical=True)
        if normalized_intent != event["intent"]:
            raise PositionError(
                "version-2 AddIntent wire intent is not canonical")
        if event["dislocation"]["source_time_ms"] > cut:
            raise PositionError(
                "version-2 AddIntent source time is later than its cut")
    elif event_type == "AddHedgeIntent":
        _v2_int(event["ordinal"], path + ".ordinal", 1, 1000)
        _v2_id(event["run_id"], path + ".run_id")
        normalized_spot = _normalize_v2_leg(
            event["spot_leg"], "spot", path + ".spot_leg",
            require_canonical=True)
        if normalized_spot != event["spot_leg"]:
            raise PositionError(
                "version-2 AddHedgeIntent Spot leg is not canonical")
        normalized_intent = _normalize_v2_entry_intent(
            event["intent"], "short", path + ".intent",
            require_canonical=True)
        if normalized_intent != event["intent"]:
            raise PositionError(
                "version-2 AddHedgeIntent wire intent is not canonical")
    elif event_type == "AddUnwindIntent":
        normalized = new_v2_add_unwind_intent_event(
            event["ordinal"], event["run_id"], event["spot_leg"],
            event["short_status"], event["net_spot_quantity"],
            event["sellable_quantity"], event["initial_dust_quantity"],
            event["intent"])
        normalized.pop("event_type")
        if normalized != {
                key: event[key] for key in _V2_ADD_UNWIND_INTENT_FIELDS}:
            raise PositionError(
                "version-2 AddUnwindIntent facts are not canonical")
    elif event_type == "AddAborted":
        normalized = new_v2_add_aborted_event(
            event["ordinal"], event["run_id"], event["reason"],
            event["spot_status"], event["short_status"],
            event["spot_net_quantity"],
            event["unwind_requested_quantity"],
            event["unwind_filled_quantity"], event["dust_quantity"],
            event["spot_leg"],
            event["unwind_leg"], event["unwind_base_fee_quantity"])
        normalized.pop("event_type")
        if normalized != {
                key: event[key] for key in _V2_ADD_ABORTED_FIELDS}:
            raise PositionError(
                "version-2 AddAborted facts are not canonical")
    elif event_type == "RungFilled":
        _v2_int(event["ordinal"], path + ".ordinal", 1, 1000)
        _v2_exact_fields(event["legs"], _V2_LEGS_FIELDS, path + ".legs")
        for role in ("spot", "short"):
            normalized = _normalize_v2_leg(
                event["legs"][role], role, path + ".legs." + role,
                require_canonical=True)
            if normalized != event["legs"][role]:
                raise PositionError(
                    "version-2 RungFilled leg is not canonical")
        normalized_dislocation = _normalize_v2_dislocation(
            event["dislocation"], path + ".dislocation",
            require_canonical=True)
        if normalized_dislocation != event["dislocation"]:
            raise PositionError(
                "version-2 RungFilled dislocation is not canonical")
        if event["dislocation"]["source_time_ms"] > cut:
            raise PositionError(
                "version-2 dislocation source time is later than its cut")
    elif event_type == "RungReduced":
        _v2_int(event["ordinal"], path + ".ordinal", 1, 1000)
        receipt_ids = event["receipt_ids"]
        if not isinstance(receipt_ids, list) or not receipt_ids \
                or len(receipt_ids) > 1000:
            raise PositionError(
                "version-2 RungReduced receipt_ids are invalid")
        normalized_ids = [
            _v2_id(value, "%s.receipt_ids[%d]" % (path, offset))
            for offset, value in enumerate(receipt_ids)
        ]
        if normalized_ids != sorted(normalized_ids) \
                or len(set(normalized_ids)) != len(normalized_ids):
            raise PositionError(
                "version-2 RungReduced receipt_ids are not sorted unique")
        normalized_amounts = _normalize_v2_reduction_amounts(
            event["amounts"], path + ".amounts",
            require_canonical=True)
        if normalized_amounts != event["amounts"]:
            raise PositionError(
                "version-2 RungReduced amounts are not canonical")
    elif event_type == "ReconcileObserved":
        normalized_observation = _normalize_v2_reconcile_observation(
            {key: event[key] for key in _V2_RECONCILE_FACT_FIELDS},
            require_canonical=True)
        if normalized_observation != {
                key: event[key] for key in _V2_RECONCILE_FACT_FIELDS}:
            raise PositionError(
                "version-2 ReconcileObserved facts are not canonical")
        _validate_v2_reconcile_read_digest(event, path)
        if cut != event["observed_at_ms"]:
            raise PositionError(
                "version-2 ReconcileObserved cut does not match observation")
        read_received_at = _v2_epoch_ms(
            event["read_received_at"], path + ".read_received_at")
        if event["observed_at_ms"] > read_received_at:
            raise PositionError(
                "FUTURE_RECONCILE_READ: version-2 reconcile observation "
                "is later than its append clock")
        if read_received_at - event["observed_at_ms"] \
                > V2_RECONCILE_READ_MAX_AGE_MS:
            raise PositionError(
                "STALE_RECONCILE_READ: version-2 reconcile observation "
                "exceeds the documented maximum age")
    else:
        _v2_id(event["receipt_id"], path + ".receipt_id")
        module_time = _v2_epoch_ms(
            event["module_time_ms"], path + ".module_time_ms")
        if cut != module_time:
            raise PositionError(
                "version-2 FlatProved cut does not match module time")

    if stored_digest != v2_event_digest(event):
        raise PositionError(
            "version-2 ledger event digest does not match its content")


def _validate_v2_ledger_chain(ledger: Any) -> None:
    if not isinstance(ledger, list) or not ledger \
            or len(ledger) > V2_MAX_LEDGER_EVENTS:
        raise PositionError(
            "version-2 ledger must be a non-empty bounded list")
    previous = V2_GENESIS_DIGEST
    for index, event in enumerate(ledger):
        _validate_v2_event_shape(event, index)
        if event["previous_digest"] != previous:
            raise PositionError(
                "version-2 ledger previous digest chain is broken")
        if index == 0 and event["event_type"] != "ConfigSnapshot":
            raise PositionError(
                "version-2 ledger event zero must be ConfigSnapshot")
        if index > 0 and event["event_type"] == "ConfigSnapshot":
            raise PositionError(
                "version-2 ConfigSnapshot is allowed only at event zero")
        previous = event["event_digest"]


def _v2_non_monotonic_cut_indices(
        ledger: List[Dict[str, Any]]) -> List[int]:
    """Return persisted event indexes whose sealed cut did not advance."""
    invalid = []
    previous_cut: Optional[int] = None
    for index, event in enumerate(ledger):
        cut = event["cut_ms"]
        if previous_cut is not None and cut <= previous_cut:
            invalid.append(index)
        previous_cut = cut
    return invalid


def _v2_record_ledger(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return value
    _v2_exact_fields(value, _V2_POSITION_FIELDS, "position")
    for field in ("version", "schema_version"):
        if isinstance(value[field], bool) \
                or not isinstance(value[field], int) \
                or value[field] != V2_SCHEMA_VERSION:
            raise PositionError(
                "version-2 position marker is invalid")
    return value["ledger"]


def _v2_decimal_sum(values: List[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = _V2_DERIVED_CONTEXT_PRECISION
        total = Decimal(0)
        for value in values:
            total += value
        return total


def _v2_decimal_subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _V2_DERIVED_CONTEXT_PRECISION
        return left - right


def _v2_active(rung: Dict[str, Any]) -> bool:
    return rung["remaining_spot"] >= V2_MIN_QUANTITY_FLOOR \
        or rung["remaining_short"] >= V2_MIN_QUANTITY_FLOOR


def _v2_closed_residual(rung: Dict[str, Any]) -> bool:
    remaining = (rung["remaining_spot"], rung["remaining_short"])
    return any(value > 0 for value in remaining) \
        and all(value < V2_MIN_QUANTITY_FLOOR for value in remaining)


def _v2_current_freeze_reasons(rung: Dict[str, Any]) -> List[str]:
    if not _v2_active(rung):
        return []
    reasons = list(rung["permanent_freeze_reasons"])
    if rung["remaining_spot"] != rung["remaining_short"] \
            and "QUANTITY_MISMATCH" not in reasons:
        reasons.append("QUANTITY_MISMATCH")
    return reasons


def _v2_used_quote(rungs: List[Dict[str, Any]]) -> Decimal:
    # Reduction receipts carry exact quantities, not an invented proportional
    # entry-notional allocation.  A partly open rung therefore conservatively
    # consumes its full terminal entry quote until both quantities reach zero.
    return _v2_decimal_sum([
        rung["terminal_fill_quote"] for rung in rungs
        if _v2_active(rung)
    ])


def _v2_headroom(stack_cap: Decimal,
                 rungs: List[Dict[str, Any]]) -> Decimal:
    headroom = _v2_decimal_subtract(stack_cap, _v2_used_quote(rungs))
    return max(Decimal(0), headroom)


def _v2_flat_receipt_proves_flat(receipt: Dict[str, Any]) -> bool:
    facts = receipt["facts"]
    return receipt["receipt_kind"] == "FLAT" \
        and Decimal(facts["futures_position_quantity"]) == 0 \
        and Decimal(facts["spot_sellable_base_quantity"]) == 0 \
        and facts["standard_open_order_count"] == 0 \
        and facts["algorithmic_open_order_count"] == 0 \
        and facts["own_orders_terminal"] is True


def _v2_quantities_agree(left: Decimal, right: Decimal) -> bool:
    return abs(_v2_decimal_subtract(left, right)) \
        < V2_MIN_QUANTITY_FLOOR


def _v2_add_reconcile_reason(reasons: List[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _v2_reconcile_observed_state(
        event: Dict[str, Any], rungs: List[Dict[str, Any]],
        prior_baseline: Dict[str, str]) -> Dict[str, Any]:
    rows = []
    reasons: List[str] = []
    event_baseline = event["baseline"]
    baseline = prior_baseline
    for field in sorted(_V2_RECONCILE_POSITION_FIELDS):
        expected = Decimal(prior_baseline[field])
        observed = Decimal(event_baseline[field])
        if not _v2_quantities_agree(expected, observed):
            rows.append({
                "row_type": "BASELINE_MISMATCH",
                "field": field,
                "derived_total": _v2_canonical_decimal(expected),
                "exchange_total": _v2_canonical_decimal(observed),
                "quantity_floor": _v2_canonical_decimal(
                    V2_MIN_QUANTITY_FLOOR),
            })
            _v2_add_reconcile_reason(reasons, "BASELINE_MISMATCH")

    observed_orders = {
        row["client_order_id"]: row for row in event["own_orders"]
    }
    expected_client_ids = set()
    rung_statuses = {}
    for rung in rungs:
        leg_rows = {}
        leg_statuses = []
        for role in ("spot", "short"):
            expected = rung["orders"][role]
            client_id = expected["client_order_id"]
            expected_client_ids.add(client_id)
            observed = observed_orders.get(client_id)
            exchange_status = None if observed is None \
                else observed["status"]
            exchange_quantity = None if observed is None \
                else observed["executed_quantity"]
            if observed is None:
                status = "UNKNOWN"
            elif exchange_status == "NOT_FOUND":
                status = "MISSING"
            elif observed["market"] != expected["market"]:
                status = "UNKNOWN"
            elif exchange_status not in V2_TERMINAL_ORDER_STATUSES \
                    or exchange_quantity is None:
                status = "UNKNOWN"
            elif exchange_status != expected["terminal_status"]:
                status = "DRIFTED"
            elif not _v2_quantities_agree(
                    Decimal(expected["executed_quantity"]),
                    Decimal(exchange_quantity)):
                status = "DRIFTED"
            else:
                status = "CONFIRMED"
            leg_statuses.append(status)
            leg_rows[role] = {
                "status": status,
                "client_order_id": client_id,
                "market": expected["market"],
                "derived_terminal_status": expected["terminal_status"],
                "exchange_terminal_status": exchange_status,
                "derived_terminal_quantity":
                    expected["executed_quantity"],
                "exchange_terminal_quantity": exchange_quantity,
            }
        if "UNKNOWN" in leg_statuses:
            rung_status = "UNKNOWN"
        elif "MISSING" in leg_statuses:
            rung_status = "MISSING"
        elif "DRIFTED" in leg_statuses:
            rung_status = "DRIFTED"
        else:
            rung_status = "CONFIRMED"
        rung_statuses[rung["ordinal"]] = rung_status
        rows.append({
            "row_type": "RUNG_RECONCILE",
            "ordinal": rung["ordinal"],
            "status": rung_status,
            "legs": leg_rows,
        })
        if rung_status != "CONFIRMED":
            _v2_add_reconcile_reason(
                reasons, "RECONCILE_" + rung_status)

    for client_id in sorted(set(observed_orders) - expected_client_ids):
        observed = observed_orders[client_id]
        rows.append({
            "row_type": "UNEXPECTED_OWN_ORDER",
            "client_order_id": client_id,
            "market": observed["market"],
            "status": observed["status"],
            "exchange_terminal_quantity": observed["executed_quantity"],
        })
        _v2_add_reconcile_reason(reasons, "UNEXPECTED_OWN_ORDER")

    for field in sorted(_V2_RECONCILE_OPEN_ORDER_FIELDS):
        order_ids = event["open_orders"][field]
        if order_ids is None:
            rows.append({
                "row_type": "READ_UNKNOWN",
                "scope": "open_orders",
                "field": field,
            })
            _v2_add_reconcile_reason(reasons, "RECONCILE_UNKNOWN")
            continue
        for client_id in order_ids:
            rows.append({
                "row_type": "OPEN_ORDER_STANDING",
                "field": field,
                "client_order_id": client_id,
            })
            _v2_add_reconcile_reason(reasons, "OPEN_ORDER_STANDING")

    positions = event["positions"]
    for field in sorted(_V2_RECONCILE_POSITION_FIELDS):
        if positions[field] is None:
            rows.append({
                "row_type": "READ_UNKNOWN",
                "scope": "positions",
                "field": field,
            })
            _v2_add_reconcile_reason(reasons, "RECONCILE_UNKNOWN")

    derived_totals = {
        "spot": _v2_decimal_sum([
            rung["remaining_spot"] for rung in rungs]),
        "short": _v2_decimal_sum([
            rung["remaining_short"] for rung in rungs]),
    }
    exchange_totals: Dict[str, Optional[Decimal]] = {
        "spot": None,
        "short": None,
    }
    if positions["spot_base_quantity"] is not None:
        exchange_totals["spot"] = _v2_decimal_subtract(
            Decimal(positions["spot_base_quantity"]),
            Decimal(baseline["spot_base_quantity"]))
    if positions["futures_position_quantity"] is not None:
        exchange_totals["short"] = _v2_decimal_subtract(
            Decimal(baseline["futures_position_quantity"]),
            Decimal(positions["futures_position_quantity"]))
    for leg in ("spot", "short"):
        exchange_total = exchange_totals[leg]
        if exchange_total is not None and not _v2_quantities_agree(
                derived_totals[leg], exchange_total):
            rows.append({
                "row_type": "AGGREGATE_MISMATCH",
                "leg": leg,
                "derived_total": _v2_canonical_decimal(
                    derived_totals[leg]),
                "exchange_total": _v2_canonical_decimal(exchange_total),
                "quantity_floor": _v2_canonical_decimal(
                    V2_MIN_QUANTITY_FLOOR),
            })
            _v2_add_reconcile_reason(reasons, "AGGREGATE_MISMATCH")

    balances = event["balances"]
    for field in sorted(_V2_RECONCILE_BALANCE_FIELDS):
        if balances[field] is None:
            rows.append({
                "row_type": "READ_UNKNOWN",
                "scope": "balances",
                "field": field,
            })
            _v2_add_reconcile_reason(reasons, "RECONCILE_UNKNOWN")
    if positions["spot_base_quantity"] is not None \
            and balances["spot_base_free_quantity"] is not None \
            and balances["spot_base_locked_quantity"] is not None:
        balance_total = _v2_decimal_sum([
            Decimal(balances["spot_base_free_quantity"]),
            Decimal(balances["spot_base_locked_quantity"]),
        ])
        position_total = Decimal(positions["spot_base_quantity"])
        if not _v2_quantities_agree(balance_total, position_total):
            rows.append({
                "row_type": "BALANCE_MISMATCH",
                "leg": "spot",
                "position_total": _v2_canonical_decimal(position_total),
                "balance_total": _v2_canonical_decimal(balance_total),
                "quantity_floor": _v2_canonical_decimal(
                    V2_MIN_QUANTITY_FLOOR),
            })
            _v2_add_reconcile_reason(reasons, "BALANCE_MISMATCH")

    return {
        "reconcile_id": event["reconcile_id"],
        "observed_at_ms": event["observed_at_ms"],
        "status": "FROZEN" if reasons else "CONFIRMED",
        "blocking": bool(reasons),
        "freeze_reasons": reasons,
        "rung_statuses": rung_statuses,
        "rows": rows,
        "baseline": baseline,
    }


def _v2_mark_reconcile_required(
        reconciliation: Optional[Dict[str, Any]],
        rungs: List[Dict[str, Any]], event_type: str,
        ordinal: int) -> None:
    if reconciliation is None:
        return
    reconciliation["status"] = "FROZEN"
    reconciliation["blocking"] = True
    _v2_add_reconcile_reason(
        reconciliation["freeze_reasons"], "RECONCILIATION_REQUIRED")
    reconciliation["rung_statuses"] = {
        rung["ordinal"]: "UNOBSERVED" for rung in rungs
    }
    reconciliation["rows"].append({
        "row_type": "RECONCILIATION_REQUIRED",
        "after_event_type": event_type,
        "ordinal": ordinal,
    })


def derive_v2_state(value: Any) -> Dict[str, Any]:
    """Purely derive all v2 operational state from one validated ledger."""
    ledger = _v2_record_ledger(value)
    _validate_v2_ledger_chain(ledger)
    non_monotonic_cuts = _v2_non_monotonic_cut_indices(ledger)
    ledger_freeze_reasons = ["NON_MONOTONIC_EVENT_TIME"] \
        if non_monotonic_cuts else []
    config = ledger[0]
    schedule = config["schedule"]
    stack_cap = Decimal(config["caps"]["stack_quote"])
    entry_cap = Decimal(config["caps"]["entry_quote"])
    base_asset = config["venue"]["base_asset"]
    registry: Dict[str, Dict[str, Any]] = {}
    registry_indices: Dict[str, int] = {}
    receipt_digests = set()
    consumed_receipts = set()
    client_ids = set()
    exchange_ids = set()
    dislocation_ids = set()
    reconcile_ids = set()
    add_run_ids = set()
    add_proposal_digests = set()
    rungs: List[Dict[str, Any]] = []
    active_add: Optional[Dict[str, Any]] = None
    # Version 2 can only begin after its exact flat proof, so the position
    # baseline is the two-leg zero fixed by that ledger transition.  Each
    # observation repeats it; a caller cannot move the baseline to hide drift.
    reconcile_baseline: Dict[str, str] = {
        "spot_base_quantity": "0",
        "futures_position_quantity": "0",
    }
    reconciliation: Optional[Dict[str, Any]] = None
    initial_flat = False
    final_flat = False
    last_transition_index = 0
    last_reduction_index = -1
    last_reduction_observed_ms = -1
    last_add_terminal: Optional[Dict[str, Any]] = None

    for index, event in enumerate(ledger[1:], start=1):
        event_type = event["event_type"]
        if active_add is not None:
            allowed = {
                "SPOT_INTENT_PERSISTED": frozenset((
                    "AddHedgeIntent", "AddUnwindIntent", "AddAborted")),
                "SHORT_INTENT_PERSISTED": frozenset((
                    "RungFilled", "AddUnwindIntent", "AddAborted")),
                "UNWIND_INTENT_PERSISTED": frozenset(("AddAborted",)),
            }.get(active_add["stage"], frozenset())
            if event_type not in allowed:
                raise PositionError(
                    "version-2 active ADD permits only its next persisted "
                    "entry or measured-unwind transition")
        if last_add_terminal is not None:
            raise PositionError(
                "ADD_TERMINAL_SINK: version-2 AddAborted refuses every "
                "later transition, including ReconcileObserved")
        if event_type == "ReceiptObserved":
            if final_flat:
                raise PositionError(
                    "version-2 CLOSED ledger cannot observe more receipts")
            receipt_id = event["receipt_id"]
            if receipt_id in registry:
                raise PositionError(
                    "version-2 receipt id is registered more than once")
            if event["receipt_digest"] in receipt_digests:
                raise PositionError(
                    "version-2 receipt digest is registered more than once")
            registry[receipt_id] = event
            registry_indices[receipt_id] = index
            receipt_digests.add(event["receipt_digest"])
            continue

        if event_type == "ReconcileObserved":
            if final_flat:
                raise PositionError(
                    "version-2 CLOSED ledger cannot reconcile again")
            reconcile_id = event["reconcile_id"]
            if reconcile_id in reconcile_ids:
                raise PositionError(
                    "version-2 reconcile id is registered more than once")
            reconcile_ids.add(reconcile_id)
            reconciliation = _v2_reconcile_observed_state(
                event, rungs, reconcile_baseline)
            continue

        if event_type == "FlatProved":
            if reconciliation is not None \
                    and reconciliation["blocking"]:
                raise PositionError(
                    "version-2 reconciliation blocks FlatProved")
            receipt_id = event["receipt_id"]
            if receipt_id not in registry:
                raise PositionError(
                    "version-2 FlatProved receipt was not observed earlier")
            if receipt_id in consumed_receipts:
                raise PositionError(
                    "version-2 receipt id is consumed more than once")
            current_receipt_ids = {
                current_id for current_id in registry
                if current_id not in consumed_receipts
                and registry_indices[current_id] > last_transition_index
            }
            if current_receipt_ids != {receipt_id}:
                raise PositionError(
                    "version-2 FlatProved must consume the complete current "
                    "receipt set")
            receipt = registry[receipt_id]
            if registry_indices[receipt_id] <= last_transition_index:
                raise PositionError(
                    "version-2 FlatProved receipt predates the current state")
            if not _v2_flat_receipt_proves_flat(receipt):
                raise PositionError(
                    "version-2 FlatProved receipt does not prove flat")
            module_time = event["module_time_ms"]
            observed = receipt["observed_at_ms"]
            if observed > module_time \
                    or module_time - observed > V2_FLAT_PROOF_MAX_AGE_MS:
                raise PositionError(
                    "version-2 FlatProved receipt is not fresh at module time")
            if not initial_flat:
                if rungs:
                    raise PositionError(
                        "version-2 initial flat proof follows a rung")
                initial_flat = True
            elif not final_flat:
                if not rungs or any(_v2_active(rung) for rung in rungs):
                    raise PositionError(
                        "version-2 final flat proof precedes full reduction")
                if registry_indices[receipt_id] <= last_reduction_index:
                    raise PositionError(
                        "version-2 final flat receipt predates reduction")
                if receipt["observed_at_ms"] < last_reduction_observed_ms:
                    raise PositionError(
                        "version-2 final flat observation predates reduction")
                final_flat = True
            else:
                raise PositionError(
                    "version-2 ledger has more than two flat proofs")
            consumed_receipts.add(receipt_id)
            last_transition_index = index
            continue

        if event_type == "AddIntent":
            if not initial_flat or final_flat or active_add is not None:
                raise PositionError(
                    "version-2 AddIntent is not a legal next transition")
            if reconciliation is None or reconciliation["blocking"]:
                raise PositionError(
                    "version-2 AddIntent requires confirmed reconciliation")
            if any(_v2_closed_residual(rung) for rung in rungs):
                raise PositionError(
                    "version-2 AddIntent is blocked by DUST_STANDING")
            if rungs and not any(_v2_active(rung) for rung in rungs):
                raise PositionError(
                    "version-2 AddIntent follows complete reduction")
            if any(_v2_current_freeze_reasons(rung)
                   for rung in rungs if _v2_active(rung)):
                raise PositionError(
                    "version-2 AddIntent is blocked by derived FROZEN state")
            if any(
                    receipt_id not in consumed_receipts
                    and registry_indices[receipt_id] > last_transition_index
                    for receipt_id in registry):
                raise PositionError(
                    "version-2 AddIntent cannot pass current receipt evidence")
            ordinal = event["ordinal"]
            if ordinal != len(rungs) + 1 or ordinal > len(schedule):
                raise PositionError(
                    "version-2 AddIntent ordinal is not the next schedule row")
            row = schedule[ordinal - 1]
            if Decimal(row["quote_cap"]) > FIRST_TRADE_CAP_USDT \
                    or entry_cap > FIRST_TRADE_CAP_USDT:
                raise PositionError(
                    "ADD_HARD_CAP_CONFIG: version-2 AddIntent exceeds the "
                    "shared first-trade authority")
            effective_cap = min(
                Decimal(row["quote_cap"]),
                _v2_headroom(stack_cap, rungs), entry_cap,
                FIRST_TRADE_CAP_USDT)
            if Decimal(event["effective_cap_quote"]) != effective_cap:
                raise PositionError(
                    "version-2 AddIntent effective cap is not schedule-derived")
            if Decimal(event["schedule_entry_gap_pct"]) != Decimal(
                    row["entry_gap_pct"]):
                raise PositionError(
                    "version-2 AddIntent gap is not the frozen schedule row")
            if event["config_event_digest"] != config["event_digest"]:
                raise PositionError(
                    "version-2 AddIntent config epoch does not match")
            if event["run_id"] in add_run_ids:
                raise PositionError(
                    "version-2 ADD run id is reused")
            if event["proposal_digest"] in add_proposal_digests:
                raise PositionError(
                    "version-2 ADD proposal digest is reused")
            expected_event_id = "add:" + event["proposal_digest"][:32]
            if event["dislocation"]["event_id"] != expected_event_id \
                    or expected_event_id in dislocation_ids:
                raise PositionError(
                    "version-2 AddIntent dislocation identity is reused "
                    "or unbound")
            if Decimal(event["dislocation"]["entry_gap_pct"]) < Decimal(
                    row["entry_gap_pct"]):
                raise PositionError(
                    "version-2 AddIntent does not meet its schedule gap")
            intent = event["intent"]
            if intent["symbol"] != config["symbol"] \
                    or Decimal(intent["quoteOrderQty"]) \
                    > effective_cap:
                raise PositionError(
                    "version-2 AddIntent wire intent exceeds frozen authority")
            if intent["newClientOrderId"] in client_ids:
                raise PositionError(
                    "version-2 AddIntent own-order identity is reused")
            add_run_ids.add(event["run_id"])
            add_proposal_digests.add(event["proposal_digest"])
            active_add = {
                "stage": "SPOT_INTENT_PERSISTED",
                "ordinal": ordinal,
                "rung_id": event["event_digest"],
                "run_id": event["run_id"],
                "proposal_id": event["proposal_id"],
                "proposal_digest": event["proposal_digest"],
                "config_event_digest": event["config_event_digest"],
                "reconcile_id": reconciliation["reconcile_id"],
                "effective_cap_quote": event["effective_cap_quote"],
                "schedule_entry_gap_pct":
                    event["schedule_entry_gap_pct"],
                "dislocation": event["dislocation"],
                "spot_intent": intent,
                "spot_leg": None,
                "short_intent": None,
                "intent_index": index,
            }
            continue

        if event_type == "AddHedgeIntent":
            if active_add is None \
                    or active_add["stage"] != "SPOT_INTENT_PERSISTED" \
                    or event["ordinal"] != active_add["ordinal"] \
                    or event["run_id"] != active_add["run_id"]:
                raise PositionError(
                    "version-2 AddHedgeIntent does not bind the active ADD")
            spot_leg = event["spot_leg"]
            short_intent = event["intent"]
            if spot_leg["market"] != config["venue"]["spot"] \
                    or spot_leg["client_order_id"] != active_add[
                        "spot_intent"]["newClientOrderId"]:
                raise PositionError(
                    "version-2 AddHedgeIntent Spot identity does not bind")
            if short_intent["symbol"] != config["symbol"] \
                    or short_intent["newClientOrderId"] \
                    == spot_leg["client_order_id"] \
                    or short_intent["newClientOrderId"] in client_ids:
                raise PositionError(
                    "version-2 AddHedgeIntent short identity is invalid")
            active_add["stage"] = "SHORT_INTENT_PERSISTED"
            active_add["spot_leg"] = spot_leg
            active_add["short_intent"] = short_intent
            active_add["hedge_intent_index"] = index
            continue

        if event_type == "AddUnwindIntent":
            if active_add is None or active_add["stage"] not in (
                    "SPOT_INTENT_PERSISTED", "SHORT_INTENT_PERSISTED") \
                    or event["ordinal"] != active_add["ordinal"] \
                    or event["run_id"] != active_add["run_id"]:
                raise PositionError(
                    "version-2 AddUnwindIntent does not bind the active ADD")
            spot_leg = event["spot_leg"]
            if spot_leg["client_order_id"] != active_add[
                    "spot_intent"]["newClientOrderId"]:
                raise PositionError(
                    "version-2 AddUnwindIntent Spot identity does not bind")
            if active_add["spot_leg"] is not None \
                    and spot_leg != active_add["spot_leg"]:
                raise PositionError(
                    "version-2 AddUnwindIntent Spot truth drifted")
            if active_add["stage"] == "SPOT_INTENT_PERSISTED" \
                    and event["short_status"] != "NOT_SEALED":
                raise PositionError(
                    "version-2 AddUnwindIntent invents a short intent")
            if active_add["stage"] == "SHORT_INTENT_PERSISTED" \
                    and event["short_status"] == "NOT_SEALED":
                raise PositionError(
                    "version-2 AddUnwindIntent drops a sealed short intent")
            spot_gross = Decimal(spot_leg["executed_quantity"])
            base_fee = Decimal(
                spot_leg["fee_by_asset"].get(base_asset, "0"))
            if base_fee > spot_gross:
                raise PositionError(
                    "version-2 AddUnwindIntent Spot fee exceeds its fill")
            net_spot = _v2_decimal_subtract(spot_gross, base_fee)
            if net_spot != Decimal(event["net_spot_quantity"]):
                raise PositionError(
                    "version-2 AddUnwindIntent net Spot fact disagrees")
            unwind_intent = event["intent"]
            if unwind_intent["symbol"] != config["symbol"] \
                    or Decimal(unwind_intent["quantity"]) != Decimal(
                        event["sellable_quantity"]):
                raise PositionError(
                    "version-2 AddUnwindIntent wire authority disagrees")
            used_add_clients = {
                active_add["spot_intent"]["newClientOrderId"],
            }
            if active_add["short_intent"] is not None:
                used_add_clients.add(
                    active_add["short_intent"]["newClientOrderId"])
            if unwind_intent["newClientOrderId"] in client_ids \
                    or unwind_intent["newClientOrderId"] in used_add_clients:
                raise PositionError(
                    "version-2 AddUnwindIntent own-order identity is reused")
            active_add["stage"] = "UNWIND_INTENT_PERSISTED"
            active_add["spot_leg"] = spot_leg
            active_add["short_status"] = event["short_status"]
            active_add["net_spot_quantity"] = event["net_spot_quantity"]
            active_add["sellable_quantity"] = event["sellable_quantity"]
            active_add["initial_dust_quantity"] = event[
                "initial_dust_quantity"]
            active_add["unwind_intent"] = unwind_intent
            active_add["unwind_intent_index"] = index
            continue

        if event_type == "AddAborted":
            if active_add is None \
                    or event["ordinal"] != active_add["ordinal"] \
                    or event["run_id"] != active_add["run_id"]:
                raise PositionError(
                    "version-2 AddAborted does not bind the active ADD")
            if event["spot_status"] in ("NOT_FOUND", "NOT_SUBMITTED"):
                if active_add["stage"] != "SPOT_INTENT_PERSISTED":
                    raise PositionError(
                        "version-2 AddAborted missing Spot fact is too late")
            else:
                spot_leg = event["spot_leg"]
                if spot_leg["client_order_id"] != active_add[
                        "spot_intent"]["newClientOrderId"]:
                    raise PositionError(
                        "version-2 AddAborted Spot identity does not bind")
                if active_add["spot_leg"] is not None \
                        and spot_leg != active_add["spot_leg"]:
                    raise PositionError(
                        "version-2 AddAborted Spot truth drifted")
                spot_gross = Decimal(spot_leg["executed_quantity"])
                base_fee = Decimal(
                    spot_leg["fee_by_asset"].get(base_asset, "0"))
                if _v2_decimal_subtract(spot_gross, base_fee) != Decimal(
                        event["spot_net_quantity"]):
                    raise PositionError(
                        "version-2 AddAborted net Spot fact disagrees")
                if active_add["stage"] == "UNWIND_INTENT_PERSISTED":
                    if event["short_status"] != active_add["short_status"] \
                            or Decimal(event["unwind_requested_quantity"]) \
                            != Decimal(active_add["sellable_quantity"]):
                        raise PositionError(
                            "version-2 AddAborted unwind authority drifted")
                    unwind_leg = event["unwind_leg"]
                    if unwind_leg is None \
                            or unwind_leg["client_order_id"] != active_add[
                                "unwind_intent"]["newClientOrderId"]:
                        raise PositionError(
                            "version-2 AddAborted unwind identity does not bind")
                elif event["unwind_leg"] is not None \
                        or Decimal(event["unwind_requested_quantity"]) != 0:
                    raise PositionError(
                        "version-2 AddAborted unwind lacks its sealed intent")
            last_add_terminal = {
                "event_index": index,
                "ordinal": event["ordinal"],
                "rung_id": active_add["rung_id"],
                "run_id": event["run_id"],
                "proposal_id": active_add["proposal_id"],
                "proposal_digest": active_add["proposal_digest"],
                "config_event_digest": active_add[
                    "config_event_digest"],
                "reconcile_id": active_add["reconcile_id"],
                "schedule_entry_gap_pct": active_add[
                    "schedule_entry_gap_pct"],
                "effective_cap_quote": active_add[
                    "effective_cap_quote"],
                "dislocation": active_add["dislocation"],
                "spot_intent": active_add["spot_intent"],
                "short_intent": active_add["short_intent"],
                "unwind_intent": active_add.get("unwind_intent"),
                "reason": event["reason"],
                "spot_status": event["spot_status"],
                "short_status": event["short_status"],
                "spot_net_quantity": event["spot_net_quantity"],
                "unwind_requested_quantity":
                    event["unwind_requested_quantity"],
                "unwind_filled_quantity": event["unwind_filled_quantity"],
                "unwind_base_fee_quantity":
                    event["unwind_base_fee_quantity"],
                "dust_quantity": event["dust_quantity"],
            }
            _v2_mark_reconcile_required(
                reconciliation, rungs, "AddAborted", event["ordinal"])
            active_add = None
            last_transition_index = index
            continue

        if event_type == "RungFilled":
            completing_add = active_add is not None
            if completing_add and active_add["stage"] \
                    != "SHORT_INTENT_PERSISTED":
                raise PositionError(
                    "version-2 RungFilled precedes the persisted short intent")
            if not initial_flat or final_flat:
                raise PositionError(
                    "version-2 RungFilled is not a legal next transition")
            if reconciliation is not None and reconciliation["blocking"]:
                raise PositionError(
                    "version-2 RungFilled is blocked by reconciliation")
            if any(_v2_closed_residual(rung) for rung in rungs):
                raise PositionError(
                    "version-2 RungFilled is blocked by DUST_STANDING")
            if rungs and not any(_v2_active(rung) for rung in rungs):
                raise PositionError(
                    "version-2 RungFilled follows complete reduction")
            if any(_v2_current_freeze_reasons(rung)
                   for rung in rungs if _v2_active(rung)):
                raise PositionError(
                    "version-2 RungFilled is blocked by derived FROZEN state")
            if any(
                    receipt_id not in consumed_receipts
                    and registry_indices[receipt_id] > last_transition_index
                    for receipt_id in registry):
                raise PositionError(
                    "version-2 RungFilled cannot pass current receipt evidence")
            ordinal = event["ordinal"]
            if ordinal != len(rungs) + 1 or ordinal > len(schedule):
                raise PositionError(
                    "version-2 RungFilled ordinal is not the next schedule row")
            if completing_add:
                if ordinal != active_add["ordinal"] \
                        or event["legs"]["spot"] != active_add["spot_leg"] \
                        or event["legs"]["short"]["client_order_id"] \
                        != active_add["short_intent"][
                            "newClientOrderId"] \
                        or event["dislocation"] != active_add["dislocation"]:
                    raise PositionError(
                        "version-2 RungFilled does not bind the active ADD")
                if Decimal(event["legs"]["spot"]["executed_quote"]) \
                        > Decimal(active_add["spot_intent"][
                            "quoteOrderQty"]):
                    raise PositionError(
                        "version-2 ADD Spot fill exceeds its persisted intent")
                if Decimal(event["legs"]["short"]["executed_quantity"]) \
                        > Decimal(active_add["short_intent"]["quantity"]):
                    raise PositionError(
                        "version-2 ADD short fill exceeds its persisted intent")
            row = schedule[ordinal - 1]
            headroom_before = _v2_headroom(stack_cap, rungs)
            if completing_add and (
                    Decimal(row["quote_cap"]) > FIRST_TRADE_CAP_USDT
                    or entry_cap > FIRST_TRADE_CAP_USDT):
                raise PositionError(
                    "ADD_HARD_CAP_CONFIG: version-2 RungFilled exceeds the "
                    "shared first-trade authority")
            effective_cap = min(
                Decimal(row["quote_cap"]), headroom_before, entry_cap,
                FIRST_TRADE_CAP_USDT if completing_add else V2_MAX_DECIMAL)
            spot = event["legs"]["spot"]
            short = event["legs"]["short"]
            if spot["market"] != config["venue"]["spot"] \
                    or short["market"] != config["venue"]["futures"]:
                raise PositionError(
                    "version-2 RungFilled market does not match config")
            for leg in (spot, short):
                client_id = leg["client_order_id"]
                exchange_key = (leg["market"], leg["exchange_order_id"])
                if client_id in client_ids or exchange_key in exchange_ids:
                    raise PositionError(
                        "version-2 own-order identity is reused")
                client_ids.add(client_id)
                exchange_ids.add(exchange_key)
            dislocation_id = event["dislocation"]["event_id"]
            if dislocation_id in dislocation_ids:
                raise PositionError(
                    "version-2 dislocation event id is reused")
            dislocation_ids.add(dislocation_id)
            spot_gross = Decimal(spot["executed_quantity"])
            short_quantity = Decimal(short["executed_quantity"])
            base_fee = Decimal(
                spot["fee_by_asset"].get(base_asset, "0"))
            if base_fee > spot_gross:
                raise PositionError(
                    "version-2 base-asset fee exceeds the Spot fill")
            spot_net = _v2_decimal_subtract(spot_gross, base_fee)
            terminal_fill_quote = max(
                Decimal(spot["executed_quote"]),
                Decimal(short["executed_quote"]))
            permanent_reasons = []
            if terminal_fill_quote > effective_cap:
                permanent_reasons.append("CAP_EXCEEDED")
            if spot_net <= 0:
                permanent_reasons.append("NONPOSITIVE_SPOT_NET")
            if Decimal(event["dislocation"]["entry_gap_pct"]) \
                    < Decimal(row["entry_gap_pct"]):
                permanent_reasons.append("SCHEDULE_GAP_NOT_MET")
            if completing_add:
                rung_identity = {
                    key: active_add[key] for key in (
                        "rung_id", "run_id", "proposal_id",
                        "proposal_digest", "config_event_digest",
                        "reconcile_id", "dislocation")
                }
            else:
                rung_identity = {
                    "rung_id": event["event_digest"],
                    "run_id": None,
                    "proposal_id": None,
                    "proposal_digest": None,
                    "config_event_digest": config["event_digest"],
                    "reconcile_id": None if reconciliation is None else
                        reconciliation["reconcile_id"],
                    "dislocation": event["dislocation"],
                }
            rungs.append({
                "ordinal": ordinal,
                **rung_identity,
                "fill_index": index,
                "cut_ms": event["cut_ms"],
                "schedule_entry_gap": Decimal(row["entry_gap_pct"]),
                "effective_cap": effective_cap,
                "terminal_fill_quote": terminal_fill_quote,
                "spot_filled": spot_net,
                "short_filled": short_quantity,
                "remaining_spot": spot_net,
                "remaining_short": short_quantity,
                "permanent_freeze_reasons": permanent_reasons,
                "orders": {
                    role: {
                        "client_order_id": event["legs"][role][
                            "client_order_id"],
                        "market": event["legs"][role]["market"],
                        "terminal_status": event["legs"][role][
                            "terminal_status"],
                        "executed_quantity": event["legs"][role][
                            "executed_quantity"],
                    }
                    for role in ("spot", "short")
                },
            })
            _v2_mark_reconcile_required(
                reconciliation, rungs, "RungFilled", ordinal)
            active_add = None
            last_transition_index = index
            continue

        if event_type == "RungReduced":
            if not initial_flat or final_flat or not rungs:
                raise PositionError(
                    "version-2 RungReduced is not a legal next transition")
            active_ordinals = [
                rung["ordinal"] for rung in rungs if _v2_active(rung)]
            if not active_ordinals \
                    or event["ordinal"] != max(active_ordinals):
                raise PositionError(
                    "version-2 RungReduced must target the top active rung")
            rung = rungs[event["ordinal"] - 1]
            receipts = []
            for receipt_id in event["receipt_ids"]:
                if receipt_id not in registry:
                    raise PositionError(
                        "version-2 RungReduced receipt was not observed earlier")
                if receipt_id in consumed_receipts:
                    raise PositionError(
                        "version-2 receipt id is consumed more than once")
                receipt = registry[receipt_id]
                if receipt["receipt_kind"] != "REDUCTION" \
                        or receipt["facts"]["ordinal"] != event["ordinal"]:
                    raise PositionError(
                        "version-2 reduction receipt does not bind the rung")
                if registry_indices[receipt_id] <= last_transition_index \
                        or receipt["observed_at_ms"] < rung["cut_ms"]:
                    raise PositionError(
                        "version-2 reduction receipt predates the current "
                        "rung state")
                receipts.append(receipt)
            current_receipt_ids = {
                current_id for current_id in registry
                if current_id not in consumed_receipts
                and registry_indices[current_id] > last_transition_index
            }
            if current_receipt_ids != set(event["receipt_ids"]):
                raise PositionError(
                    "version-2 RungReduced must consume the complete current "
                    "receipt set")
            spot_sum = _v2_decimal_sum([
                Decimal(receipt["facts"]["spot_quantity"])
                for receipt in receipts
            ])
            short_sum = _v2_decimal_sum([
                Decimal(receipt["facts"]["short_quantity"])
                for receipt in receipts
            ])
            if spot_sum != Decimal(event["amounts"]["spot_quantity"]) \
                    or short_sum != Decimal(
                        event["amounts"]["short_quantity"]):
                raise PositionError(
                    "version-2 RungReduced amounts do not match receipts")
            if spot_sum > rung["remaining_spot"] \
                    or short_sum > rung["remaining_short"]:
                raise PositionError(
                    "version-2 RungReduced exceeds remaining quantity")
            rung["remaining_spot"] = _v2_decimal_subtract(
                rung["remaining_spot"], spot_sum)
            rung["remaining_short"] = _v2_decimal_subtract(
                rung["remaining_short"], short_sum)
            _v2_mark_reconcile_required(
                reconciliation, rungs, "RungReduced", event["ordinal"])
            consumed_receipts.update(event["receipt_ids"])
            last_transition_index = index
            last_reduction_index = index
            last_reduction_observed_ms = max(
                last_reduction_observed_ms,
                *(receipt["observed_at_ms"] for receipt in receipts))
            continue

        raise PositionError(
            "version-2 event is not a legal ledger transition at index %d"
            % index)

    active_rungs = [rung for rung in rungs if _v2_active(rung)]
    top_active_ordinal = max(
        (rung["ordinal"] for rung in active_rungs), default=None)
    pending_receipt_ids = [
        receipt_id for receipt_id in registry
        if receipt_id not in consumed_receipts
        and registry_indices[receipt_id] > last_transition_index
    ]
    pending_receipts = [
        registry[receipt_id] for receipt_id in pending_receipt_ids
    ]
    pending_active_evidence = bool(active_rungs and pending_receipts)
    dust_standing = not final_flat and any(
        _v2_closed_residual(rung) for rung in rungs)
    reconcile_blocking = reconciliation is not None \
        and reconciliation["blocking"]
    add_terminal_current = last_add_terminal is not None

    derived_rungs = []
    dust_rows = []
    for rung in rungs:
        reasons = _v2_current_freeze_reasons(rung)
        reconcile_status = None if reconciliation is None else \
            reconciliation["rung_statuses"].get(rung["ordinal"])
        if reconcile_status not in (None, "CONFIRMED") \
                and _v2_active(rung):
            reasons.append("RECONCILE_" + reconcile_status)
        if pending_active_evidence \
                and rung["ordinal"] == top_active_ordinal:
            reasons.append("RECEIPT_EVIDENCE_PENDING")
        active = _v2_active(rung)
        closed_residual = _v2_closed_residual(rung)
        recorded_not_lost = closed_residual and final_flat
        status = V2_CLOSED_RESIDUAL if closed_residual and not final_flat \
            else "CLOSED" if not active \
            else "FROZEN" if reasons else ST_OPEN
        if closed_residual:
            dust_rows.append({
                "row_type": "DUST",
                "status": V2_CLOSED_RESIDUAL,
                "ordinal": rung["ordinal"],
                "reason": "BELOW_MINIMUM_QUANTITY_FLOOR",
                "quantity_floor": _v2_canonical_decimal(
                    V2_MIN_QUANTITY_FLOOR),
                "spot_quantity": _v2_canonical_decimal(
                    rung["remaining_spot"]),
                "short_quantity": _v2_canonical_decimal(
                    rung["remaining_short"]),
                "recorded_not_lost": recorded_not_lost,
            })
        derived_rungs.append({
            "ordinal": rung["ordinal"],
            "rung_id": rung["rung_id"],
            "run_id": rung["run_id"],
            "proposal_id": rung["proposal_id"],
            "proposal_digest": rung["proposal_digest"],
            "config_event_digest": rung["config_event_digest"],
            "reconcile_id": rung["reconcile_id"],
            "dislocation": _copy_v2_record(
                rung["dislocation"], "rung dislocation projection"),
            "status": status,
            "schedule_entry_gap_pct":
                _v2_canonical_decimal(rung["schedule_entry_gap"]),
            "effective_cap_quote":
                _v2_canonical_decimal(rung["effective_cap"]),
            "terminal_fill_quote":
                _v2_canonical_decimal(rung["terminal_fill_quote"]),
            "spot_net_filled_quantity":
                _v2_canonical_decimal(rung["spot_filled"]),
            "short_filled_quantity":
                _v2_canonical_decimal(rung["short_filled"]),
            "remaining_spot_quantity":
                _v2_canonical_decimal(rung["remaining_spot"]),
            "remaining_short_quantity":
                _v2_canonical_decimal(rung["remaining_short"]),
            "cap_exceeded":
                "CAP_EXCEEDED" in rung["permanent_freeze_reasons"],
            "freeze_reasons": reasons,
            "reconcile_status": reconcile_status,
        })

    any_frozen = any(
        _v2_current_freeze_reasons(rung) for rung in active_rungs)
    any_frozen = any_frozen or pending_active_evidence
    any_frozen = any_frozen or bool(active_rungs and dust_standing)
    any_frozen = any_frozen or reconcile_blocking
    flat_transition_ready = len(pending_receipts) == 1 \
        and _v2_flat_receipt_proves_flat(pending_receipts[0])
    top_active_rung = None if top_active_ordinal is None \
        else rungs[top_active_ordinal - 1]
    reduction_transition_ready = bool(pending_receipts) \
        and top_active_rung is not None \
        and all(
            receipt["receipt_kind"] == "REDUCTION"
            and receipt["facts"]["ordinal"] == top_active_ordinal
            and receipt["observed_at_ms"] >= top_active_rung["cut_ms"]
            for receipt in pending_receipts) \
        and _v2_decimal_sum([
            Decimal(receipt["facts"]["spot_quantity"])
            for receipt in pending_receipts]) \
            <= top_active_rung["remaining_spot"] \
        and _v2_decimal_sum([
            Decimal(receipt["facts"]["short_quantity"])
            for receipt in pending_receipts]) \
            <= top_active_rung["remaining_short"]
    if ledger_freeze_reasons:
        top_state = "FROZEN"
    elif final_flat:
        top_state = ST_CLOSED
    elif active_add is not None:
        top_state = ST_ADDING
    elif add_terminal_current:
        top_state = ST_ADD_ABORTED
    elif reconcile_blocking:
        top_state = "FROZEN"
    elif not initial_flat:
        top_state = "UNPROVEN" if flat_transition_ready \
            or not pending_receipts else "FROZEN"
    elif not rungs:
        top_state = "READY" if not pending_receipts else "FROZEN"
    elif not active_rungs:
        top_state = "AWAITING_FLAT_PROOF" \
            if flat_transition_ready or not pending_receipts else "FROZEN"
    elif any_frozen:
        top_state = "FROZEN"
    else:
        top_state = ST_OPEN

    headroom = _v2_headroom(stack_cap, rungs)
    next_ordinal: Optional[int] = None
    if top_state in ("READY", ST_OPEN) and not dust_standing \
            and len(rungs) < len(schedule):
        next_ordinal = len(rungs) + 1
    if active_add is not None and not ledger_freeze_reasons:
        next_types = {
            "SPOT_INTENT_PERSISTED": (
                "AddHedgeIntent", "AddUnwindIntent", "AddAborted"),
            "SHORT_INTENT_PERSISTED": (
                "RungFilled", "AddUnwindIntent", "AddAborted"),
            "UNWIND_INTENT_PERSISTED": ("AddAborted",),
        }[active_add["stage"]]
        legal_next = [
            {"event_type": event_type, "ordinal": active_add["ordinal"]}
            for event_type in next_types
        ]
    else:
        legal_next = [] if top_state in (ST_CLOSED, ST_ADD_ABORTED) \
            or ledger_freeze_reasons \
            else [{"event_type": "ReceiptObserved"}]
        if legal_next:
            legal_next.append({"event_type": "ReconcileObserved"})
        if top_state in ("UNPROVEN", "AWAITING_FLAT_PROOF") \
                and flat_transition_ready and not reconcile_blocking:
            legal_next.append({"event_type": "FlatProved"})
        if next_ordinal is not None:
            legal_next.append({
                "event_type": "AddIntent", "ordinal": next_ordinal,
            })
            legal_next.append({
                "event_type": "RungFilled", "ordinal": next_ordinal,
            })
        if reduction_transition_ready and not ledger_freeze_reasons:
            legal_next.append({
                "event_type": "RungReduced",
                "ordinal": top_active_ordinal,
            })

    top_state_flags = list(ledger_freeze_reasons)
    if dust_standing:
        top_state_flags.append(V2_DUST_STANDING)
    if active_add is not None:
        top_state_flags.append("ADD_TRANSITION_ACTIVE")
    if add_terminal_current:
        top_state_flags.append(ST_ADD_ABORTED)
    if reconciliation is not None:
        for reason in reconciliation["freeze_reasons"]:
            if reason not in top_state_flags:
                top_state_flags.append(reason)

    return {
        "symbol": config["symbol"],
        "direction": config["direction"],
        "config_event_digest": config["event_digest"],
        "top_state": top_state,
        "freeze_reasons": top_state_flags,
        "reconciliation_status": "UNOBSERVED" if reconciliation is None
            else reconciliation["status"],
        "last_reconcile_id": None if reconciliation is None
            else reconciliation["reconcile_id"],
        "active_transition": None if active_add is None else
            _copy_v2_record(active_add, "active ADD projection"),
        "add_terminal": None if last_add_terminal is None else
            _copy_v2_record(last_add_terminal, "ADD terminal projection"),
        "reconcile_rows": [] if reconciliation is None
            else reconciliation["rows"],
        "reconcile_baseline": reconcile_baseline,
        "flat_proved_blocked": reconcile_blocking,
        "ledger_event_count": len(ledger),
        "rungs": derived_rungs,
        "dust_rows": dust_rows,
        "remaining_spot_quantity": _v2_canonical_decimal(
            _v2_decimal_sum([
                rung["remaining_spot"] for rung in rungs])),
        "remaining_short_quantity": _v2_canonical_decimal(
            _v2_decimal_sum([
                rung["remaining_short"] for rung in rungs])),
        "used_quote": _v2_canonical_decimal(_v2_used_quote(rungs)),
        "cap_headroom_quote": _v2_canonical_decimal(headroom),
        "next_ordinal": next_ordinal,
        "increases_frozen":
            top_state in (
                "UNPROVEN", ST_ADDING, ST_ADD_ABORTED, "FROZEN",
                "AWAITING_FLAT_PROOF", ST_CLOSED),
        "reduce_only": (
            top_state == "FROZEN" and bool(active_rungs)
            and not ledger_freeze_reasons) or (
                top_state == ST_ADDING
                and active_add is not None
                and active_add["spot_leg"] is not None),
        "legal_next_transitions": legal_next,
    }


def _validate_v2_position(position: Dict[str, Any]) -> None:
    derive_v2_state(position)


def _has_v2_record_shape(value: Any, path: Tuple[str, ...] = ()) -> bool:
    if isinstance(value, list):
        return any(_has_v2_record_shape(item, path) for item in value)
    if not isinstance(value, dict):
        return False
    fields = set(value)
    strong_sentinels = _V2_SHAPE_SENTINELS - {"event_type"}
    if fields & strong_sentinels \
            or value.get("event_type") in V2_EVENT_TYPES:
        return True
    return any(
        _has_v2_record_shape(nested, path + (key,))
        for key, nested in value.items())


def _is_v2_position(position: Any) -> bool:
    return _has_v2_record_shape(position)


def _require_v2_schema_marker(position: Dict[str, Any]) -> None:
    marker = position.get("schema_version")
    if isinstance(marker, bool) or not isinstance(marker, int) \
            or marker != V2_SCHEMA_VERSION:
        raise PositionError(
            "basis-position.json schema_version is not supported or is missing")


class _DuplicateV2Field(ValueError):
    pass


def _v2_no_duplicate_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateV2Field(key)
        result[key] = value
    return result


def position_path(receipt_dir: str) -> str:
    return os.path.join(receipt_dir, STATE_FILE)


def position_head_path(receipt_dir: str) -> str:
    return position_path(receipt_dir) + ".head"


def _load_v2_committed_head(receipt_dir: str) -> Dict[str, Any]:
    try:
        with open(position_head_path(receipt_dir),
                  encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        raise PositionError(
            "version-2 committed head is missing; ledger refused") from None
    except OSError:
        raise PositionError(
            "version-2 committed head is unreadable; ledger refused") \
            from None
    try:
        head = json.loads(raw, object_pairs_hook=_v2_no_duplicate_fields)
    except (_DuplicateV2Field, json.JSONDecodeError):
        raise PositionError(
            "version-2 committed head is duplicated or unreadable; "
            "ledger refused") from None
    _v2_exact_fields(head, _V2_HEAD_FIELDS, "committed head")
    _v2_int(
        head["event_count"], "committed head.event_count",
        1, V2_MAX_LEDGER_EVENTS)
    _v2_digest(
        head["last_event_digest"],
        "committed head.last_event_digest")
    return head


def _match_v2_committed_head(
        position: Dict[str, Any], head: Dict[str, Any]) -> Dict[str, Any]:
    """Return the committed view, ignoring one validated prepared tail.

    The ledger replace is PREPARE and the head replace is COMMIT.  A reader
    may therefore see exactly one complete event beyond the committed head.
    Every other disagreement remains a refusal.
    """
    ledger = position["ledger"]
    count = head["event_count"]
    if count == len(ledger) \
            and head["last_event_digest"] == ledger[-1]["event_digest"]:
        return position
    if count == len(ledger) - 1 and count > 0 \
            and head["last_event_digest"] \
            == ledger[count - 1]["event_digest"]:
        committed = _copy_v2_record(position, "committed ledger view")
        committed["ledger"] = committed["ledger"][:count]
        _validate_v2_position(committed)
        return committed
    raise PositionError(
        "version-2 ledger disagrees with its committed head")


def load_position(receipt_dir: str) -> Optional[Dict[str, Any]]:
    try:
        with open(position_path(receipt_dir), encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        if os.path.exists(position_head_path(receipt_dir)):
            raise PositionError(
                "version-2 committed head exists without its ledger") \
                from None
        return None
    except OSError:
        raise PositionError(
            "basis-position.json exists but is unreadable; "
            "human check required before any trade")
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        raise PositionError(
            "basis-position.json exists but is unreadable; "
            "human check required before any trade") from None
    if not isinstance(loaded, dict):
        raise PositionError("basis-position.json is not an object")
    if _is_v2_position(loaded):
        try:
            loaded = json.loads(
                raw, object_pairs_hook=_v2_no_duplicate_fields)
        except (_DuplicateV2Field, json.JSONDecodeError):
            raise PositionError(
                "basis-position.json version-2 fields are duplicated or "
                "unreadable") from None
        _require_v2_schema_marker(loaded)
        head = _load_v2_committed_head(receipt_dir)
        _validate_v2_position(loaded)
        loaded = _match_v2_committed_head(loaded, head)
    elif os.path.exists(position_head_path(receipt_dir)):
        raise PositionError(
            "version-2 committed head cannot bind a non-version-2 record")
    return loaded


def _position_revision(position: Optional[Dict[str, Any]]) -> int:
    if position is None:
        return 0
    revision = position.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) \
            or revision < 0:
        raise PositionError("basis-position.json revision is invalid")
    return revision


_NO_REVISION_CHECK = object()


_POSITION_GUARD_LOCAL_START = "local:%s:%s" % (
    os.getpid(), time.monotonic_ns())
_POSITION_GUARD_FIELDS = frozenset({
    "owner_pid", "owner_start_marker", "created_epoch_ms",
    "heartbeat_epoch_ms", "heartbeat_monotonic_ns", "token",
})


@dataclass(frozen=True)
class _PositionWriteGuardAttempt:
    token: Optional[str]
    recovery_row: Optional[Dict[str, str]] = None
    refusal: Optional[str] = None

    def __bool__(self) -> bool:
        return self.token is not None


def _position_guard_process_start(pid: int) -> Optional[str]:
    """Return an OS start marker when available, with a local fallback."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        with open("/proc/%s/stat" % pid, "rb") as handle:
            text = handle.read().decode("ascii")
        closing = text.rfind(")")
        fields = text[closing + 2:].split() if closing >= 0 else []
        start_ticks = fields[19]
        if start_ticks.isdigit():
            return "linux:" + start_ticks
    except (IndexError, OSError, UnicodeError):
        pass
    if pid == os.getpid():
        return _POSITION_GUARD_LOCAL_START
    return None


def _position_guard_owner_alive(pid: int) -> Optional[bool]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _valid_position_guard(owner: Any) -> bool:
    return isinstance(owner, dict) \
        and set(owner) == _POSITION_GUARD_FIELDS \
        and type(owner.get("owner_pid")) is int \
        and owner["owner_pid"] > 0 \
        and isinstance(owner.get("owner_start_marker"), str) \
        and bool(owner["owner_start_marker"]) \
        and len(owner["owner_start_marker"]) <= 128 \
        and type(owner.get("created_epoch_ms")) is int \
        and owner["created_epoch_ms"] >= 0 \
        and type(owner.get("heartbeat_epoch_ms")) is int \
        and owner["heartbeat_epoch_ms"] >= owner["created_epoch_ms"] \
        and type(owner.get("heartbeat_monotonic_ns")) is int \
        and owner["heartbeat_monotonic_ns"] >= 0 \
        and isinstance(owner.get("token"), str) \
        and bool(owner["token"])


def _encode_position_guard(owner: Dict[str, Any]) -> bytes:
    return (json.dumps(owner, sort_keys=True, separators=(",", ":"))
            + "\n").encode("ascii")


def _create_position_write_guard(path: str) -> str:
    now_ms = time.time_ns() // 1_000_000
    marker = _position_guard_process_start(os.getpid())
    if marker is None:
        raise OSError("position write owner start marker is unavailable")
    token = secrets.token_hex(16)
    encoded = _encode_position_guard({
        "owner_pid": os.getpid(),
        "owner_start_marker": marker,
        "created_epoch_ms": now_ms,
        "heartbeat_epoch_ms": now_ms,
        "heartbeat_monotonic_ns": time.monotonic_ns(),
        "token": token,
    })
    descriptor = None
    try:
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        if os.write(descriptor, encoded) != len(encoded):
            raise OSError("position write guard write was incomplete")
        os.fsync(descriptor)
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    else:
        os.close(descriptor)
    return token


def _take_position_write_guard(
        receipt_dir: str) -> _PositionWriteGuardAttempt:
    """Acquire once, or reclaim one provably stale owner with a typed row."""
    try:
        os.makedirs(receipt_dir, mode=0o700, exist_ok=True)
    except OSError:
        return _PositionWriteGuardAttempt(
            None, refusal="POSITION_WRITE_GUARD_ACQUIRE_REFUSED")
    guard_path = os.path.join(receipt_dir, STATE_WRITE_GUARD_FILE)
    recovered = None
    for attempt in range(2):
        try:
            token = _create_position_write_guard(guard_path)
            return _PositionWriteGuardAttempt(token, recovered)
        except FileExistsError:
            if attempt:
                return _PositionWriteGuardAttempt(
                    None, refusal="POSITION_WRITE_GUARD_ACTIVE")
        except OSError:
            return _PositionWriteGuardAttempt(
                None, refusal="POSITION_WRITE_GUARD_ACQUIRE_REFUSED")

        try:
            with open(guard_path, "rb") as handle:
                encoded = handle.read()
            stat_result = os.stat(guard_path)
        except FileNotFoundError:
            continue
        except OSError:
            return _PositionWriteGuardAttempt(
                None, refusal="POSITION_WRITE_GUARD_INSPECTION_REFUSED")
        now_ms = time.time_ns() // 1_000_000
        owner = None
        try:
            decoded = json.loads(encoded.decode("ascii"))
            if _valid_position_guard(decoded):
                owner = decoded
        except (UnicodeError, ValueError, json.JSONDecodeError):
            owner = None
        stale_reason = None
        prior_pid = "UNKNOWN"
        if owner is None:
            modified_ms = stat_result.st_mtime_ns // 1_000_000
            if now_ms >= modified_ms \
                    and now_ms - modified_ms > POSITION_WRITE_GUARD_STALE_MS:
                stale_reason = "LEGACY_HEARTBEAT_STALE"
        else:
            prior_pid = str(owner["owner_pid"])
            heartbeat_ms = owner["heartbeat_epoch_ms"]
            if now_ms < heartbeat_ms:
                return _PositionWriteGuardAttempt(
                    None, refusal="POSITION_WRITE_GUARD_CLOCK_REFUSED")
            alive = _position_guard_owner_alive(owner["owner_pid"])
            if alive is False:
                stale_reason = "OWNER_NOT_RUNNING"
            elif alive is True:
                current_marker = _position_guard_process_start(
                    owner["owner_pid"])
                if current_marker is not None \
                        and current_marker != owner["owner_start_marker"]:
                    stale_reason = "OWNER_START_CHANGED"
                else:
                    # A living matching process remains the owner regardless
                    # of heartbeat age.  Slow local fsync cannot transfer its
                    # token to another writer.
                    return _PositionWriteGuardAttempt(
                        None, refusal="POSITION_WRITE_GUARD_ACTIVE")
            elif now_ms - heartbeat_ms > POSITION_WRITE_GUARD_STALE_MS:
                stale_reason = "HEARTBEAT_STALE"
            else:
                return _PositionWriteGuardAttempt(
                    None, refusal="POSITION_WRITE_GUARD_OWNER_REFUSED")
        if stale_reason is None:
            return _PositionWriteGuardAttempt(
                None, refusal="POSITION_WRITE_GUARD_ACTIVE")
        try:
            with open(guard_path, "rb") as handle:
                if handle.read() != encoded:
                    return _PositionWriteGuardAttempt(
                        None, refusal="POSITION_WRITE_GUARD_CHANGED")
            os.unlink(guard_path)
        except FileNotFoundError:
            continue
        except OSError:
            return _PositionWriteGuardAttempt(
                None, refusal="POSITION_WRITE_GUARD_RECLAIM_REFUSED")
        recovered = {
            "row_type": "POSITION_WRITE_GUARD_RECLAIMED",
            "reason": stale_reason,
            "prior_owner_pid": prior_pid,
        }
    return _PositionWriteGuardAttempt(
        None, refusal="POSITION_WRITE_GUARD_ACTIVE")


def _heartbeat_position_write_guard(
        receipt_dir: str, token: str) -> bool:
    path = os.path.join(receipt_dir, STATE_WRITE_GUARD_FILE)
    try:
        with open(path, encoding="ascii") as handle:
            owner = json.load(handle)
        marker = _position_guard_process_start(os.getpid())
        if not _valid_position_guard(owner) \
                or owner["token"] != token \
                or owner["owner_pid"] != os.getpid() \
                or marker is None \
                or owner["owner_start_marker"] != marker:
            return False
        owner["heartbeat_epoch_ms"] = time.time_ns() // 1_000_000
        owner["heartbeat_monotonic_ns"] = time.monotonic_ns()
        encoded = _encode_position_guard(owner)
        descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC)
        try:
            if os.write(descriptor, encoded) != len(encoded):
                return False
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False
    return True


def _release_position_write_guard(receipt_dir: str, token: str) -> None:
    path = os.path.join(receipt_dir, STATE_WRITE_GUARD_FILE)
    try:
        with open(path, encoding="ascii") as handle:
            owner = json.load(handle)
        if _valid_position_guard(owner) and owner["token"] == token:
            os.unlink(path)
    except (FileNotFoundError, OSError, UnicodeError, ValueError,
            json.JSONDecodeError):
        return


def _atomic_replace_position(path: str, value: Dict[str, Any],
                             allow_decimal_default: bool) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            if allow_decimal_default:
                json.dump(value, handle, indent=2, default=str)
            else:
                json.dump(value, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _fsync_file_and_parent(path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def save_position(receipt_dir: str, position: Dict[str, Any],
                  expected_revision: Any = _NO_REVISION_CHECK) -> None:
    """Preserve the version-1 save contract; v2 changes use ledger append."""
    if _is_v2_position(position):
        raise PositionError(
            "version-2 state changes require append_v2_event")
    os.makedirs(receipt_dir, mode=0o700, exist_ok=True)
    guard = _take_position_write_guard(receipt_dir)
    if not guard:
        raise PositionError(
            "%s: basis-position state update refused"
            % guard.refusal)
    try:
        current = load_position(receipt_dir)
        if _is_v2_position(current):
            raise PositionError(
                "version-2 state cannot be replaced by a version-1 record")
        current_revision = _position_revision(current)
        if expected_revision is not _NO_REVISION_CHECK \
                and current_revision != expected_revision:
            raise PositionError(
                "basis-position.json changed while --check was reading; "
                "stale check not saved")
        candidate_revision = _position_revision(position)
        position["revision"] = max(
            candidate_revision, current_revision) + 1
        position["verdict"] = STATE_VERDICT[position["state"]]
        position["updated_utc"] = utc()
        if not _heartbeat_position_write_guard(receipt_dir, guard.token):
            raise PositionError(
                "POSITION_WRITE_GUARD_OWNERSHIP_REFUSED: state update stopped")
        _atomic_replace_position(
            position_path(receipt_dir), position,
            allow_decimal_default=True)
    finally:
        _release_position_write_guard(receipt_dir, guard.token)


def _seal_v2_event_payload(
        payload: Dict[str, Any], previous_digest: str,
        previous_cut: Optional[int]) -> Dict[str, Any]:
    copied = _copy_v2_record(payload, "event payload")
    event_type = copied.get("event_type")
    if event_type not in V2_EVENT_TYPES:
        raise PositionError("version-2 event payload type is invalid")
    module_now = _v2_epoch_ms(
        default_now_ms(), "module append time")
    if event_type == "FlatProved":
        _v2_exact_fields(
            copied, _V2_FLAT_PROVED_INPUT_FIELDS,
            "FlatProved payload")
        cut = module_now
        copied["module_time_ms"] = cut
    else:
        _v2_exact_fields(
            copied, _V2_EVENT_PAYLOAD_FIELDS[event_type],
            event_type + " payload")
        if event_type == "ReconcileObserved":
            reconcile_facts = {
                key: copied[key] for key in _V2_RECONCILE_FACT_FIELDS
            }
            normalized_reconcile = _normalize_v2_reconcile_observation(
                reconcile_facts, require_canonical=True)
            if normalized_reconcile != reconcile_facts:
                raise PositionError(
                    "version-2 ReconcileObserved facts are not canonical")
            _validate_v2_reconcile_read_digest(
                copied, "ReconcileObserved")
        if event_type == "ConfigSnapshot":
            cut = V2_GENESIS_CUT_MS
        elif event_type in ("ReceiptObserved", "ReconcileObserved"):
            cut = _v2_epoch_ms(
                copied["observed_at_ms"], event_type + ".cut_ms")
        elif event_type == "RungFilled":
            cut = _v2_epoch_ms(copied["cut_ms"], "RungFilled.cut_ms")
        else:
            if previous_cut is None:
                raise PositionError(
                    "version-2 logical transition lacks a prior event cut")
            # Intent and reduction events have no external timestamp.  Their
            # sealed logical cut advances one millisecond and still passes the
            # append-clock gate.
            cut = _v2_epoch_ms(
                previous_cut + 1, "RungReduced.cut_ms")
    if event_type == "ReconcileObserved" and cut > module_now:
        raise PositionError(
            "FUTURE_RECONCILE_READ: version-2 reconcile observation "
            "is later than the append clock")
    if cut > module_now + V2_EVENT_FUTURE_SKEW_MS:
        raise PositionError(
            "version-2 event cut exceeds the documented future skew bound")
    if previous_cut is not None and cut <= previous_cut:
        raise PositionError(
            "version-2 event cut is not strictly greater than the prior cut")
    if event_type == "ReconcileObserved" \
            and module_now - cut > V2_RECONCILE_READ_MAX_AGE_MS:
        raise PositionError(
            "STALE_RECONCILE_READ: version-2 reconcile observation "
            "exceeds the documented maximum age")
    if event_type == "ReconcileObserved":
        copied["read_received_at"] = module_now
    copied["cut_ms"] = cut
    copied["previous_digest"] = previous_digest
    copied["event_digest"] = V2_GENESIS_DIGEST
    copied["event_digest"] = v2_event_digest(copied)
    return copied


def append_v2_event(receipt_dir: str, event_payload: Dict[str, Any],
                    expected_length: int,
                    expected_last_digest: Optional[str] = None
                    ) -> Dict[str, Any]:
    """CAS-append; replace the ledger, then its committed head sidecar."""
    expected_length = _v2_int(
        expected_length, "expected_length", 0, V2_MAX_LEDGER_EVENTS - 1)
    if expected_last_digest is not None:
        _v2_digest(expected_last_digest, "expected_last_digest")
    os.makedirs(receipt_dir, mode=0o700, exist_ok=True)
    guard = _take_position_write_guard(receipt_dir)
    if not guard:
        raise PositionError(
            "%s: version-2 ledger append refused"
            % guard.refusal)
    try:
        current = load_position(receipt_dir)
        if current is None:
            ledger = []
        elif not _is_v2_position(current):
            raise PositionError(
                "version-2 ledger cannot replace a version-1 record")
        else:
            ledger = current["ledger"]
        if len(ledger) != expected_length:
            raise PositionError(
                "version-2 expected_length is stale; ledger unchanged")
        current_digest = V2_GENESIS_DIGEST if not ledger \
            else ledger[-1]["event_digest"]
        if expected_last_digest is not None \
                and current_digest != expected_last_digest:
            raise PositionError(
                "version-2 expected_last_digest is stale; ledger unchanged")
        if len(ledger) >= V2_MAX_LEDGER_EVENTS:
            raise PositionError(
                "version-2 ledger has reached its event bound")
        if ledger and _v2_non_monotonic_cut_indices(ledger):
            raise PositionError(
                "version-2 current ledger has non-monotonic event time")
        previous = V2_GENESIS_DIGEST if not ledger \
            else ledger[-1]["event_digest"]
        previous_cut = None if not ledger else ledger[-1]["cut_ms"]
        event = _seal_v2_event_payload(
            event_payload, previous, previous_cut)
        if not ledger and event["event_type"] != "ConfigSnapshot":
            raise PositionError(
                "version-2 first append must be ConfigSnapshot")
        if ledger and event["event_type"] == "ConfigSnapshot":
            raise PositionError(
                "version-2 ConfigSnapshot is frozen at event zero")
        candidate = {
            "version": V2_SCHEMA_VERSION,
            "schema_version": V2_SCHEMA_VERSION,
            "ledger": _copy_v2_record(
                ledger, "current ledger") + [event],
        }
        _validate_v2_position(candidate)
        if not _heartbeat_position_write_guard(receipt_dir, guard.token):
            raise PositionError(
                "POSITION_WRITE_GUARD_OWNERSHIP_REFUSED: ledger append stopped")
        _atomic_replace_position(
            position_path(receipt_dir), candidate,
            allow_decimal_default=False)
        if not _heartbeat_position_write_guard(receipt_dir, guard.token):
            raise PositionError(
                "POSITION_WRITE_GUARD_OWNERSHIP_REFUSED: prepared ledger "
                "tail was not committed")
        _atomic_replace_position(
            position_head_path(receipt_dir), {
                "event_count": len(candidate["ledger"]),
                "last_event_digest":
                    candidate["ledger"][-1]["event_digest"],
            }, allow_decimal_default=False)
        return candidate
    finally:
        _release_position_write_guard(receipt_dir, guard.token)


def initialize_v2_position(
        receipt_dir: str, config_snapshot: Dict[str, Any],
        expected_length: int = 0) -> Dict[str, Any]:
    """Create a v2 record by appending its ConfigSnapshot event zero."""
    return append_v2_event(
        receipt_dir, new_v2_config_snapshot_event(config_snapshot),
        expected_length)


def v2_event_age_at_creation_ms(event: Dict[str, Any]) -> int:
    """Return a RungFilled event age using only its sealed source and cut."""
    if not isinstance(event, dict) \
            or event.get("event_type") != "RungFilled":
        raise PositionError("version-2 event is not RungFilled")
    age = _v2_epoch_ms(
        event["cut_ms"], "RungFilled.cut_ms") - _v2_epoch_ms(
            event["dislocation"]["source_time_ms"],
            "RungFilled.dislocation.source_time_ms")
    if age < 0:
        raise PositionError(
            "version-2 dislocation source time is later than its cut")
    return age


def _prior_receipt_blocker(receipt_dir: str) -> Optional[str]:
    """Mirror basis_executor.acquire's cross-lane prior-receipt rule."""
    try:
        names = sorted(os.listdir(receipt_dir))
    except FileNotFoundError:
        return None
    except OSError:
        return "the shared receipt directory is unreadable"
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(receipt_dir, name),
                      encoding="utf-8") as handle:
                prior = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeError):
            return ("a prior receipt in the receipt directory is unreadable; "
                    "reconcile it before any new trade")
        if not isinstance(prior, dict) or "verdict" not in prior:
            return ("a prior run in the receipt directory has no verdict; "
                    "any lane blocks until its client ids are reconciled")
    return None


def persist_intent(receipt_dir: str, position: Dict[str, Any], stage: str,
                   plan: CyclePlan, run_id: str,
                   quantity: Optional[Decimal] = None) -> str:
    """Persist the EXACT intent and client id BEFORE the mutation. The wire
    intent is rebuilt deterministically by settle_leg from the same inputs,
    so what is persisted is what is sent."""
    intent = leg_intent(stage, plan, run_id, quantity)
    client = intent["newClientOrderId"]
    position["pending_intent"] = {
        "stage": stage, "run_id": run_id, "client_id": client,
        "intent": dict(intent), "persisted_utc": utc(),
    }
    position.setdefault("client_ids", {})[client] = {
        "stage": stage, "market": intent["market"], "run_id": run_id,
    }
    save_position(receipt_dir, position)
    return client


def record_leg(receipt_dir: str, position: Dict[str, Any], stage: str,
               client: str, market: str, outcome: str, order: Any,
               fees: Optional[Dict[str, str]] = None) -> None:
    entry: Dict[str, Any] = {"stage": stage, "client_id": client,
                             "market": market, "outcome": outcome,
                             "utc": utc()}
    if isinstance(order, dict):
        if "status" in order:
            entry["status"] = str(order.get("status"))
        if "executedQty" in order:
            entry["executed_qty"] = str(order.get("executedQty"))
        quote_field = VENUES[market].quote_field
        if quote_field in order:
            entry["quote"] = str(order.get(quote_field))
    if fees:
        entry["fee_by_asset"] = dict(fees)
    position.setdefault("legs", []).append(entry)
    if outcome != "unknown":
        position["pending_intent"] = None    # unresolved intents stay pending
    save_position(receipt_dir, position)


def attach_position_fees(receipt_dir: str, position: Dict[str, Any],
                         state: RunState) -> None:
    """Copy the exchange-truth fee map of the newest receipt leg onto the
    newest position leg, so P&L survives restarts."""
    receipt_fees = state.receipt["legs"][-1].get("fee_by_asset")
    if receipt_fees and position.get("legs"):
        position["legs"][-1]["fee_by_asset"] = dict(receipt_fees)
        save_position(receipt_dir, position)


# --------------------------------------------------------------------------
# Configuration and per-command runtime state.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PositionConfig:
    symbol: str
    sha: str
    receipt_dir: str
    min_entry_bps: Decimal = Decimal("1")
    watch_gap_pct: Decimal = WATCH_GAP_PCT_DEFAULT
    max_hold_ms: Optional[int] = None              # owner decision, required
    min_remaining_edge_pct: Optional[Decimal] = None   # owner decision
    max_adverse_move_pct: Optional[Decimal] = None     # owner decision
    run_id: str = ""


def _env_decimal(env, name: str, default: Optional[str]) -> Optional[Decimal]:
    raw = env.get(name, default)
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise PositionError("%s is not a valid decimal" % name) from None
    if not value.is_finite():
        raise PositionError("%s must be finite" % name)
    return value


def config_from_env(env, symbol: str) -> PositionConfig:
    symbol = (symbol or env.get("SYMBOL", "")).upper()
    sha = env.get("QUANT_SHA", "").lower()
    if not SYMBOL_RE.fullmatch(symbol):
        raise PositionError("SYMBOL must be 5-20 uppercase letters/digits")
    if not SHA_RE.fullmatch(sha):
        raise PositionError("QUANT_SHA must be a full 40-character git SHA")
    min_entry_bps = _env_decimal(env, "MIN_ENTRY_BPS", "1")
    if min_entry_bps < 0:
        raise PositionError("MIN_ENTRY_BPS must be non-negative")
    watch = _env_decimal(env, "WATCH_GAP_PCT", str(WATCH_GAP_PCT_DEFAULT))
    if watch <= 0:
        raise PositionError("WATCH_GAP_PCT must be positive")
    max_hold_raw = env.get("MAX_HOLD_MS", "")
    max_hold = None
    if max_hold_raw:
        try:
            max_hold = int(max_hold_raw)
        except ValueError:
            raise PositionError("MAX_HOLD_MS must be an integer of "
                                "milliseconds") from None
        if max_hold <= 0:
            raise PositionError("MAX_HOLD_MS must be positive")
    return PositionConfig(
        symbol=symbol, sha=sha,
        receipt_dir=os.path.expanduser(env.get("RECEIPT_DIR",
                                               "~/quant_receipts")),
        min_entry_bps=min_entry_bps,
        watch_gap_pct=watch,
        max_hold_ms=max_hold,
        min_remaining_edge_pct=_env_decimal(env, "MIN_REMAINING_EDGE_PCT",
                                            None),
        max_adverse_move_pct=_env_decimal(env, "MAX_ADVERSE_MOVE_PCT", None),
    )


def make_state(cfg: PositionConfig, request, sleep, command: str,
               receipted: bool = True,
               run_id_override: Optional[str] = None) -> RunState:
    run_id = run_id_override or cfg.run_id or new_run_id()
    receipt_path = os.path.join(cfg.receipt_dir,
                                "basis-position-%s.json" % run_id)
    state = RunState(
        cfg=ExecutorConfig(symbol=cfg.symbol, sha=cfg.sha,
                           receipt_dir=cfg.receipt_dir,
                           min_entry_bps=cfg.min_entry_bps, run_id=run_id),
        request=request, sleep=sleep, run_id=run_id,
        receipt_path=receipt_path if receipted else "",
        lock_path=os.path.join(cfg.receipt_dir, LOCK_FILE),
        events_path=os.path.join(cfg.receipt_dir, "events.jsonl"),
    )
    if receipted:
        os.makedirs(cfg.receipt_dir, mode=0o700, exist_ok=True)
        state.receipt.update({
            "kind": "position-" + command,
            "run_id": run_id, "sha": cfg.sha, "symbol": cfg.symbol,
            "started_utc": utc(), "legs": [],
        })
        save(state)
        start = base_event(state, "run_start")
        start["note"] = contract_note("basis-position %s" % command, ())
        state.events.append(start)
    return state


def conclude(state: RunState, verdict: str, exposure: str, note: str) -> int:
    """Receipt + events + factual rows; returns the process exit code."""
    if state.receipt_path:
        state.receipt.update({
            "verdict": verdict, "exposure": clean(exposure, 300),
            "verdict_note": clean(note, 300), "finished_utc": utc(),
        })
        save(state)
        event = base_event(state, "run_verdict")
        event["verdict"] = verdict
        text = exposure if exposure and exposure != "none" else note
        if text:
            event["note"] = contract_note(text, state.client_ids)
        state.events.append(event)
        write_events(state)
    print("lane: BASIS-POSITION")
    print("run_id:", state.run_id)
    print("symbol:", state.cfg.symbol)
    print("VERDICT:", verdict)
    print("EXPOSURE:", clean(exposure, 300))
    print("NOTE:", clean(note, 300))
    return EXIT_CODE[verdict]


def take_lock(state: RunState) -> bool:
    try:
        descriptor = os.open(state.lock_path,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, state.run_id.encode())
        os.close(descriptor)
        return True
    except FileExistsError:
        return False


def release_lock(state: RunState) -> None:
    try:
        os.unlink(state.lock_path)
    except FileNotFoundError:
        pass


def default_now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# --rates: signed commission-rate reads, written locally (rates, no secrets).
# --------------------------------------------------------------------------

def cmd_rates(cfg: PositionConfig, request, sleep=time.sleep,
              now_ms: Callable[[], int] = default_now_ms) -> int:
    state = make_state(cfg, request, sleep, "rates", receipted=False)
    try:
        spot = signed_read(state, SPOT.base_url, SPOT_COMMISSION_PATH,
                           {"symbol": cfg.symbol}, "Spot commission read")
        usdm = signed_read(state, USDM.base_url, USDM_COMMISSION_PATH,
                           {"symbol": cfg.symbol}, "USD-M commission read")
    except PreflightRefusal as refusal:
        print("RATES | %s | REFUSED | %s" % (cfg.symbol, clean(refusal, 300)))
        return EXIT_CODE["REFUSED"]
    if not isinstance(spot, dict) or not isinstance(usdm, dict) \
            or spot.get("symbol") != cfg.symbol \
            or usdm.get("symbol") != cfg.symbol:
        print("RATES | %s | REFUSED | commission responses malformed or for "
              "another symbol" % cfg.symbol)
        return EXIT_CODE["REFUSED"]
    try:
        standard = spot.get("standardCommission", {})
        spot_maker = d(standard.get("maker"))
        spot_taker = d(standard.get("taker"))
        usdm_maker = d(usdm.get("makerCommissionRate"))
        usdm_taker = d(usdm.get("takerCommissionRate"))
    except (ArithmeticError, TypeError):
        print("RATES | %s | REFUSED | commission responses unreadable"
              % cfg.symbol)
        return EXIT_CODE["REFUSED"]
    rates = [spot_maker, spot_taker, usdm_maker, usdm_taker]
    if any(not rate.is_finite() or rate < 0 for rate in rates):
        print("RATES | %s | REFUSED | commission rates out of range"
              % cfg.symbol)
        return EXIT_CODE["REFUSED"]
    # Rates are FRACTIONS per leg (0.001 = 10 bps); percent is derived.
    round_trip_pct = (spot_taker * 2 + usdm_taker * 2) * Decimal(100)
    payload = {
        "symbol": cfg.symbol,
        "spot": {"maker": fmt(spot_maker), "taker": fmt(spot_taker)},
        "usdm": {"maker": fmt(usdm_maker), "taker": fmt(usdm_taker)},
        "round_trip_taker_fees_pct": fmt(round_trip_pct),
        "source": {"spot": SPOT_COMMISSION_PATH,
                   "usdm": USDM_COMMISSION_PATH},
        "fetched_utc": utc(),
        "verdict": "RATES_RECORDED",   # cross-lane gate: always a verdict key
    }
    os.makedirs(cfg.receipt_dir, mode=0o700, exist_ok=True)
    path = os.path.join(cfg.receipt_dir, RATES_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    print("RATES | %s | spot taker %s | usdm taker %s | round trip %s%% | %s"
          % (cfg.symbol, fmt(spot_taker), fmt(usdm_taker),
             fmt(round_trip_pct), path))
    return EXIT_CODE["RATES_RECORDED"]


def exit_fees_pct(receipt_dir: str) -> Tuple[Decimal, str]:
    """Percent cost of the two REMAINING legs, from measured rates when
    recorded, else the spec's per-leg defaults (labeled)."""
    try:
        with open(os.path.join(receipt_dir, RATES_FILE),
                  encoding="utf-8") as handle:
            rates = json.load(handle)
        spot_taker = d(rates["spot"]["taker"]) * Decimal(100)
        usdm_taker = d(rates["usdm"]["taker"]) * Decimal(100)
        if spot_taker >= 0 and usdm_taker >= 0:
            return spot_taker + usdm_taker, "measured"
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        pass
    return (SPOT_TAKER_FEE_PCT_DEFAULT + USDM_TAKER_FEE_PCT_DEFAULT,
            "spec_default")


# --------------------------------------------------------------------------
# --open: preflight, set-and-verify 1x, spot entry + short, persist OPEN.
# --------------------------------------------------------------------------

def _spot_free(account: Any, asset: str) -> Optional[Decimal]:
    if not isinstance(account, dict):
        return None
    for row in account.get("balances", []):
        if isinstance(row, dict) and row.get("asset") == asset:
            try:
                return d(row.get("free", "0"))
            except ArithmeticError:
                return None
    return Decimal(0)


@dataclass(frozen=True)
class AcceptedProposal:
    proposal_id: str
    digest: str
    payload: Dict[str, Any]
    quantity: Decimal
    net_edge_pct: Decimal


def _proposal_value(payload: Dict[str, Any],
                    paths: Tuple[Tuple[str, ...], ...]) -> Any:
    for path in paths:
        value: Any = payload
        for key in path:
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            return value
    return None


def _proposal_decimal(payload: Dict[str, Any], label: str,
                      paths: Tuple[Tuple[str, ...], ...],
                      required: bool = True) -> Optional[Decimal]:
    raw = _proposal_value(payload, paths)
    if raw is None:
        if required:
            raise PositionError("proposal required field %s is missing" % label)
        return None
    if isinstance(raw, bool):
        raise PositionError("proposal field %s is not a finite decimal" % label)
    try:
        value = d(raw)
    except (ArithmeticError, TypeError, ValueError):
        raise PositionError(
            "proposal field %s is not a finite decimal" % label) from None
    if not value.is_finite():
        raise PositionError("proposal field %s is not a finite decimal" % label)
    return value


def _proposal_utc_ms(value: Any, label: str) -> int:
    if not isinstance(value, str) or not value:
        raise PositionError("proposal required field %s is missing" % label)
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return int(parsed.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        raise PositionError(
            "proposal %s is not a timezone-aware timestamp" % label) from None


def load_entry_proposal(receipt_dir: str, proposal_id: Optional[str],
                        symbol: str, quant_sha: str,
                        now_ms: int) -> AcceptedProposal:
    """Load and validate the immutable entry authority before any transport.

    The digest is over the exact bytes read, so the position and run receipt
    identify precisely which proposal authorized the entry.
    """
    if not isinstance(proposal_id, str) or not PROPOSAL_ID_RE.fullmatch(
            proposal_id):
        raise PositionError("--open requires --proposal <id>")
    path = os.path.join(receipt_dir, PROPOSAL_DIR,
                        "proposal-%s.json" % proposal_id)
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        payload = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except FileNotFoundError:
        raise PositionError("proposal %s is missing" % proposal_id) from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PositionError("proposal %s is unreadable" % proposal_id) from None
    if not isinstance(payload, dict):
        raise PositionError("proposal %s is unreadable: JSON object required"
                            % proposal_id)

    missing_fields = sorted(PROPOSAL_REQUIRED_FIELDS - set(payload))
    if missing_fields:
        raise PositionError("proposal required field(s) missing: %s"
                            % ", ".join(missing_fields))

    stored_id = payload.get("proposal_id")
    if not isinstance(stored_id, str):
        raise PositionError("proposal required field id is missing")
    if stored_id != proposal_id:
        raise PositionError("proposal id %s does not match requested id %s"
                            % (stored_id, proposal_id))
    if "id" in payload and payload["id"] != payload["proposal_id"]:
        raise PositionError("proposal id fields disagree")
    proposal_symbol = payload.get("symbol")
    if not isinstance(proposal_symbol, str):
        raise PositionError("proposal required field symbol is missing")
    if proposal_symbol != symbol:
        raise PositionError("proposal symbol %s does not match SYMBOL %s"
                            % (proposal_symbol, symbol))
    proposal_sha = payload.get("quant_sha")
    if not isinstance(proposal_sha, str):
        raise PositionError("proposal required field quant_sha is missing")
    if proposal_sha != quant_sha:
        raise PositionError("proposal quant_sha %s does not match QUANT_SHA %s"
                            % (proposal_sha, quant_sha))
    created_ms = _proposal_utc_ms(payload.get("created_utc"), "created_utc")
    expires_ms = _proposal_utc_ms(payload.get("expires_utc"), "expires_utc")
    if expires_ms <= created_ms:
        raise PositionError("proposal expires_utc must be after created_utc")
    if now_ms >= expires_ms:
        raise PositionError("proposal %s expired at %s"
                            % (proposal_id, payload.get("expires_utc")))

    digest = payload.get("config_digest")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) \
            is None:
        raise PositionError("proposal config_digest is missing or invalid")
    if payload.get("direction") != "long_spot_short_perp":
        raise PositionError("proposal direction is not long_spot_short_perp")
    source = payload.get("commission_source")
    if not isinstance(source, str) or not source:
        raise PositionError("proposal commission_source is missing")
    funding_period = payload.get("funding_period_ms")
    if isinstance(funding_period, bool) or not isinstance(funding_period, int) \
            or funding_period <= 0:
        raise PositionError("proposal funding_period_ms must be positive")

    positive_fields = ("spot_ask", "perp_bid", "spot_bid", "perp_ask",
                       "entry_gap_pct", "safety_buffer_pct")
    nonnegative_fields = (
        "spot_maker_commission_pct", "spot_taker_commission_pct",
        "perp_maker_commission_pct", "perp_taker_commission_pct",
        "entry_slippage_pct", "exit_slippage_pct")
    for field in positive_fields:
        value = _proposal_decimal(payload, field, ((field,),))
        if value is None or value <= 0:
            raise PositionError("proposal %s must be positive" % field)
    for field in nonnegative_fields:
        value = _proposal_decimal(payload, field, ((field,),))
        if value is None or value < 0:
            raise PositionError("proposal %s must be non-negative" % field)
    for field in ("exit_gap_pct", "funding_rate", "carry_pct"):
        _proposal_decimal(payload, field, ((field,),))

    net_edge = _proposal_decimal(
        payload, "net_edge_pct",
        (("net_edge_pct",), ("final_net_edge_pct",),
         ("economics", "net_edge_pct"),
         ("economics", "final_net_edge_pct")))
    if net_edge is None or net_edge <= 0:
        raise PositionError("proposal net_edge_pct must be positive")
    quantity = _proposal_decimal(
        payload, "quantity",
        (("quantity",), ("base_quantity",), ("quantity_base",),
         ("execution", "quantity")))
    if quantity is None or quantity <= 0:
        raise PositionError("proposal quantity must be positive")
    return AcceptedProposal(
        proposal_id=proposal_id,
        digest=hashlib.sha256(raw).hexdigest(),
        payload=payload,
        quantity=quantity,
        net_edge_pct=net_edge,
    )


def _proposal_plan(accepted: AcceptedProposal,
                   market: Dict[str, Any],
                   effective_cap: Optional[Decimal] = None,
                   *, enforce_first_trade_cap: bool = False) -> CyclePlan:
    """Build the entry plan only from proposal facts plus venue filters."""
    payload = accepted.payload
    quantity = accepted.quantity
    if quantize_down(quantity, market["shared_step"]) != quantity:
        raise PositionError("proposal quantity is not on the shared venue step")
    if quantity < market["min_qty"]:
        raise PositionError("proposal quantity is below a venue minimum")

    entry_bps = _proposal_decimal(
        payload, "entry_gap_bps",
        (("entry_gap_bps",), ("economics", "entry_gap_bps")),
        required=False)
    entry_pct = _proposal_decimal(
        payload, "entry_gap_pct",
        (("entry_gap_pct",), ("gross_edge_pct",),
         ("entry", "gap_pct"), ("executable_entry", "gap_pct"),
         ("economics", "entry_gap_pct"),
         ("economics", "entry", "gap_pct")),
        required=False)
    spot_ask = _proposal_decimal(
        payload, "entry spot ask",
        (("entry_spot_ask",), ("spot_ask",),
         ("entry", "spot_ask"), ("executable_entry", "spot_ask"),
         ("economics", "entry", "spot_ask")),
        required=False)
    usdm_bid = _proposal_decimal(
        payload, "entry perp bid",
        (("entry_usdm_bid",), ("entry_perp_bid",), ("usdm_bid",),
         ("perp_bid",), ("entry", "usdm_bid"),
         ("entry", "perp_bid"), ("executable_entry", "perp_bid"),
         ("economics", "entry", "perp_bid")),
        required=False)
    if entry_bps is None:
        if entry_pct is not None:
            entry_bps = entry_pct * Decimal(100)
        elif spot_ask is not None and usdm_bid is not None and spot_ask > 0:
            entry_bps = (usdm_bid - spot_ask) / spot_ask * Decimal(10000)
        else:
            raise PositionError(
                "proposal executable entry economics are missing")
    if entry_bps <= 0:
        raise PositionError("proposal executable entry gap must be positive")

    exit_bps = _proposal_decimal(
        payload, "exit_gap_bps",
        (("exit_gap_bps",), ("economics", "exit_gap_bps")),
        required=False)
    exit_pct = _proposal_decimal(
        payload, "exit_gap_pct",
        (("exit_gap_pct",), ("intended_exit_gap_pct",),
         ("exit", "gap_pct"), ("intended_exit", "gap_pct"),
         ("economics", "exit_gap_pct"),
         ("economics", "exit", "gap_pct")),
        required=False)
    exit_spot_bid = _proposal_decimal(
        payload, "exit spot bid",
        (("exit_spot_bid",), ("exit", "spot_bid"),
         ("intended_exit", "spot_bid"),
         ("economics", "exit", "spot_bid")),
        required=False)
    exit_usdm_ask = _proposal_decimal(
        payload, "exit perp ask",
        (("exit_usdm_ask",), ("exit_perp_ask",),
         ("exit", "usdm_ask"), ("exit", "perp_ask"),
         ("intended_exit", "perp_ask"),
         ("economics", "exit", "perp_ask")),
        required=False)
    if exit_bps is None:
        if exit_pct is not None:
            exit_bps = exit_pct * Decimal(100)
        elif exit_spot_bid is not None and exit_usdm_ask is not None \
                and exit_spot_bid > 0:
            exit_bps = ((exit_usdm_ask - exit_spot_bid) / exit_spot_bid
                        * Decimal(10000))
        else:
            raise PositionError(
                "proposal intended-exit economics are missing")

    funding_rate = _proposal_decimal(
        payload, "funding_rate",
        (("funding_rate",), ("funding", "rate"),
         ("economics", "funding_rate"),
         ("economics", "funding", "rate")))
    spot_quote_cap = _proposal_decimal(
        payload, "spot_quote_cap",
        (("spot_quote_cap",), ("notional_usd",),
         ("suggested_notional_usd",),
         ("execution", "spot_quote_cap"),
         ("economics", "notional_usd")),
        required=False)
    if spot_quote_cap is None and spot_ask is not None:
        spot_quote_cap = (quantity * spot_ask * (Decimal(1) + FEE_BUFFER)
                          ).quantize(MARGIN_QUANTUM)
    if spot_quote_cap is None:
        raise PositionError("proposal execution notional is missing")
    short_notional = _proposal_decimal(
        payload, "short_notional",
        (("short_notional",), ("notional_usd",),
         ("suggested_notional_usd",),
         ("execution", "short_notional"),
         ("economics", "notional_usd")),
        required=False)
    if short_notional is None and usdm_bid is not None:
        short_notional = (quantity * usdm_bid).quantize(MARGIN_QUANTUM)
    if short_notional is None:
        raise PositionError("proposal short notional is missing")
    if spot_quote_cap <= 0 or short_notional <= 0:
        raise PositionError("proposal execution notionals must be positive")
    if effective_cap is not None:
        if not isinstance(effective_cap, Decimal) \
                or not effective_cap.is_finite() or effective_cap <= 0:
            raise PositionError("effective entry cap is invalid")
        if enforce_first_trade_cap and (
                spot_quote_cap > FIRST_TRADE_CAP_USDT
                or short_notional > FIRST_TRADE_CAP_USDT):
            raise PositionError(
                "ADD_HARD_CAP_PROPOSAL: proposal execution notional exceeds "
                "the shared first-trade authority %s USDT"
                % fmt(FIRST_TRADE_CAP_USDT))
        # The immutable proposal remains the priced authority.  The frozen
        # ledger authorities may narrow it, never raise the shared hard cap.
        spot_quote_cap = min(spot_quote_cap, effective_cap)
        short_notional = min(short_notional, effective_cap)
    if spot_quote_cap > MAX_CYCLE_QUOTE or short_notional > MAX_CYCLE_QUOTE:
        raise PositionError("proposal execution notional exceeds the compiled "
                            "%s USDT cap" % fmt(MAX_CYCLE_QUOTE))
    if spot_quote_cap < market["min_notional"] \
            or short_notional < market["min_notional"]:
        raise PositionError("proposal execution notional is below a venue minimum")
    return CyclePlan(
        symbol=payload["symbol"],
        entry_gap_bps=entry_bps,
        exit_gap_bps=exit_bps,
        funding_rate=funding_rate,
        spot_quantity=quantity,
        short_quantity=quantity,
        spot_quote_cap=spot_quote_cap,
        short_notional=short_notional,
        leverage=1,
        reason="accepted proposal %s digest %s" % (
            accepted.proposal_id, accepted.digest),
    )


def _read_only_entry_preflight(state: RunState, plan: CyclePlan,
                               market: Dict[str, Any]) -> int:
    """Complete every account proof while leverage is still unchanged."""
    symbol = state.cfg.symbol
    capability = signed_read(state, USDM.base_url,
                             USDM_ACCOUNT_CONFIG_PATH, None,
                             "Futures account-configuration read")
    try:
        parse_trade_capability(capability)
    except FuturesPreflightError as exc:
        raise PreflightRefusal(str(exc)) from None

    spot_account = signed_read(state, SPOT.base_url, SPOT_ACCOUNT_PATH, None,
                               "Spot account and balance read")
    can_trade = spot_account.get("canTrade") \
        if isinstance(spot_account, dict) else None
    if not (can_trade is True or str(can_trade).strip().lower() == "true"):
        raise PreflightRefusal("Spot account canTrade is not true")
    free_quote = _spot_free(spot_account, "USDT")
    if free_quote is None or free_quote < plan.spot_quote_cap:
        raise PreflightRefusal(
            "spot USDT free balance %s does not cover the quote cap %s"
            % ("UNKNOWN" if free_quote is None else fmt(free_quote),
               fmt(plan.spot_quote_cap)))

    mode = signed_read(state, USDM.base_url, USDM_POSITION_MODE_PATH, None,
                       "Futures position-mode read")
    dual = mode.get("dualSidePosition") if isinstance(mode, dict) else None
    if not (dual is False or str(dual).strip().lower() == "false"):
        raise PreflightRefusal("Futures position mode is not proven ONE_WAY; "
                               "cycle intents send no positionSide")

    config_rows = signed_read(state, USDM.base_url,
                              USDM_SYMBOL_CONFIG_PATH, {"symbol": symbol},
                              "Futures symbol-configuration read")
    leverage_before = symbol_leverage(config_rows, symbol)
    if leverage_before < 1:
        raise PreflightRefusal("actual symbol leverage is not positive")

    for venue, orders_path in ((SPOT, SPOT_OPEN_ORDERS_PATH),
                               (USDM, USDM_OPEN_ORDERS_PATH)):
        orders = signed_read(state, venue.base_url, orders_path,
                             {"symbol": symbol},
                             "%s open-orders read" % venue.name)
        if not isinstance(orders, list):
            raise PreflightRefusal(
                "%s open-orders response is not a list" % venue.name)
        if orders:
            raise PreflightRefusal(
                "%s open order(s) already standing on %s for the plan "
                "symbol; attribution unsafe" % (len(orders), venue.name))

    rows = signed_read(state, USDM.base_url, USDM_POSITION_PATH,
                       {"symbol": symbol}, "Futures position read")
    amount = futures_position(rows, symbol)
    if amount is None:
        raise PreflightRefusal("Futures position read cannot prove flatness "
                               "on the plan symbol")
    if amount != 0:
        raise PreflightRefusal("existing Futures position %s on the plan "
                               "symbol; attribution unsafe" % fmt(amount))

    account = signed_read(state, USDM.base_url, USDM_ACCOUNT_PATH, None,
                          "Futures balance read")
    try:
        available = d(account.get("availableBalance", "0")) \
            if isinstance(account, dict) else Decimal(0)
    except ArithmeticError:
        raise PreflightRefusal(
            "Futures available balance is unreadable") from None
    required = (plan.short_notional * market["band_up"]).quantize(
        MARGIN_QUANTUM)
    if available < required:
        raise PreflightRefusal(
            "available Futures balance %s is below the banded short margin "
            "%s USDT (notional %s x band %s at 1x)" % (
                fmt(available), fmt(required), fmt(plan.short_notional),
                fmt(market["band_up"])))
    state.receipt["preflight"] = {
        "futures_can_trade": True,
        "spot_can_trade": True,
        "position_mode": "ONE_WAY",
        "symbol_leverage_before": leverage_before,
        "open_orders": 0,
        "position_amount": "0",
        "spot_available_balance": fmt(free_quote),
        "futures_available_balance": fmt(available),
        "required_short_margin_1x": fmt(required),
        "margin_band_up": fmt(market["band_up"]),
        "verified_utc": utc(),
    }
    save(state)
    return leverage_before


def _spot_balance_quantities(
        account: Any, asset: str) -> Optional[Tuple[Decimal, Decimal]]:
    if not isinstance(account, dict) or not isinstance(
            account.get("balances"), list):
        return None
    for row in account["balances"]:
        if not isinstance(row, dict) or row.get("asset") != asset:
            continue
        try:
            free = d(row["free"])
            locked = d(row["locked"])
        except (ArithmeticError, KeyError, TypeError, ValueError):
            return None
        if not free.is_finite() or not locked.is_finite() \
                or free < 0 or locked < 0:
            return None
        return free, locked
    return None


def _read_only_add_preflight(
        state: RunState, plan: CyclePlan, market: Dict[str, Any],
        derived: Dict[str, Any], reconcile_event: Dict[str, Any]) -> None:
    """Recheck account capability and the reconciled standing pair.

    The persisted ReconcileObserved event is the complete four-order-space
    authority.  These later standard reads close over capability, balances,
    one-way exposure, and leverage without replacing that digest-bound read.
    """
    symbol = state.cfg.symbol
    capability = signed_read(
        state, USDM.base_url, USDM_ACCOUNT_CONFIG_PATH, None,
        "Futures account-configuration read")
    try:
        parse_trade_capability(capability)
    except FuturesPreflightError as exc:
        raise PreflightRefusal(str(exc)) from None

    spot_account = signed_read(
        state, SPOT.base_url, SPOT_ACCOUNT_PATH, None,
        "Spot account and balance read")
    can_trade = spot_account.get("canTrade") \
        if isinstance(spot_account, dict) else None
    if not (can_trade is True or str(can_trade).strip().lower() == "true"):
        raise PreflightRefusal("Spot account canTrade is not true")
    quote_balance = _spot_balance_quantities(spot_account, "USDT")
    base_balance = _spot_balance_quantities(spot_account, market["base"])
    if quote_balance is None:
        raise PreflightRefusal("spot USDT balance is unreadable")
    free_quote = quote_balance[0]
    if free_quote < plan.spot_quote_cap:
        raise PreflightRefusal(
            "spot USDT free balance %s does not cover the quote cap %s"
            % (fmt(free_quote), fmt(plan.spot_quote_cap)))
    if base_balance is None:
        raise PreflightRefusal("Spot base balance is unreadable")
    observed_spot = _v2_decimal_sum(list(base_balance))
    expected_spot = Decimal(derived["remaining_spot_quantity"])
    if not _v2_quantities_agree(observed_spot, expected_spot):
        raise PreflightRefusal(
            "Spot base quantity %s does not agree with reconciled ledger %s"
            % (fmt(observed_spot), fmt(expected_spot)))

    mode = signed_read(
        state, USDM.base_url, USDM_POSITION_MODE_PATH, None,
        "Futures position-mode read")
    dual = mode.get("dualSidePosition") if isinstance(mode, dict) else None
    if not (dual is False or str(dual).strip().lower() == "false"):
        raise PreflightRefusal(
            "Futures position mode is not proven ONE_WAY; "
            "cycle intents send no positionSide")

    config_rows = signed_read(
        state, USDM.base_url, USDM_SYMBOL_CONFIG_PATH,
        {"symbol": symbol}, "Futures symbol-configuration read")
    leverage = symbol_leverage(config_rows, symbol)
    if leverage != 1:
        raise PreflightRefusal(
            "actual symbol leverage %s is not the frozen 1x entry setting"
            % fmt(leverage))

    for venue, orders_path in ((SPOT, SPOT_OPEN_ORDERS_PATH),
                               (USDM, USDM_OPEN_ORDERS_PATH)):
        orders = signed_read(
            state, venue.base_url, orders_path, {"symbol": symbol},
            "%s open-orders read" % venue.name)
        if not isinstance(orders, list):
            raise PreflightRefusal(
                "%s open-orders response is not a list" % venue.name)
        if orders:
            raise PreflightRefusal(
                "%s open order(s) now stand on %s; the prior reconcile "
                "no longer licenses an ADD" % (len(orders), venue.name))

    rows = signed_read(
        state, USDM.base_url, USDM_POSITION_PATH,
        {"symbol": symbol}, "Futures position read")
    observed_short = futures_position(rows, symbol)
    expected_short = -Decimal(derived["remaining_short_quantity"])
    if observed_short is None or not _v2_quantities_agree(
            observed_short, expected_short):
        raise PreflightRefusal(
            "Futures position %s does not agree with reconciled ledger %s"
            % ("UNKNOWN" if observed_short is None else fmt(observed_short),
               fmt(expected_short)))

    account = signed_read(
        state, USDM.base_url, USDM_ACCOUNT_PATH, None,
        "Futures balance read")
    try:
        available = d(account["availableBalance"])
    except (ArithmeticError, KeyError, TypeError, ValueError):
        raise PreflightRefusal(
            "Futures available balance is unreadable") from None
    if not available.is_finite() or available < 0:
        raise PreflightRefusal(
            "Futures available balance is unreadable")
    required = (plan.short_notional * market["band_up"]).quantize(
        MARGIN_QUANTUM)
    if available < required:
        raise PreflightRefusal(
            "available Futures balance %s is below the banded short margin "
            "%s USDT (notional %s x band %s at 1x)" % (
                fmt(available), fmt(required), fmt(plan.short_notional),
                fmt(market["band_up"])))
    state.receipt["preflight"] = {
        "futures_can_trade": True,
        "spot_can_trade": True,
        "position_mode": "ONE_WAY",
        "symbol_leverage": leverage,
        "standard_open_orders": 0,
        "reconcile_id": reconcile_event["reconcile_id"],
        "reconcile_read_digest": reconcile_event["read_digest"],
        "spot_position_quantity": fmt(observed_spot),
        "futures_position_quantity": fmt(observed_short),
        "spot_available_balance": fmt(free_quote),
        "futures_available_balance": fmt(available),
        "required_short_margin_1x": fmt(required),
        "margin_band_up": fmt(market["band_up"]),
        "verified_utc": utc(),
    }
    save(state)


def _book_snapshot(kind: Any, payload: Any) -> Dict[str, Any]:
    """Receipt-safe public-book fields, including malformed-read evidence."""
    snapshot = {"read_status": clean(kind, 40),
                "symbol": None, "bid": None, "ask": None}
    if not isinstance(payload, dict):
        return snapshot
    for receipt_key, payload_key in (("symbol", "symbol"),
                                     ("bid", "bidPrice"),
                                     ("ask", "askPrice")):
        raw = payload.get(payload_key)
        if isinstance(raw, (str, int, Decimal)) and not isinstance(raw, bool):
            snapshot[receipt_key] = clean(raw, 80)
    return snapshot


def _validated_book(payload: Any, symbol: str,
                    label: str) -> Tuple[Decimal, Decimal]:
    if not isinstance(payload, dict):
        raise PositionError("%s book returned a non-object response" % label)
    if payload.get("symbol") != symbol:
        raise PositionError("%s book symbol does not match %s"
                            % (label, symbol))
    prices = []
    for field in ("bidPrice", "askPrice"):
        raw = payload.get(field)
        if raw is None or isinstance(raw, (bool, float)):
            raise PositionError("%s book has unreadable %s" % (label, field))
        try:
            value = d(raw)
        except (ArithmeticError, TypeError, ValueError):
            raise PositionError("%s book has unreadable %s"
                                % (label, field)) from None
        if not value.is_finite() or value <= 0:
            raise PositionError("%s book has unreadable %s"
                                % (label, field))
        prices.append(value)
    bid, ask = prices
    if bid > ask:
        raise PositionError("%s book is crossed" % label)
    return bid, ask


def _revalidate_entry(state: RunState,
                      accepted: AcceptedProposal,
                      schedule_gap_pct: Optional[Decimal] = None
                      ) -> Optional[str]:
    """Read exactly two public books and persist the entry-time decision.

    Returns a plain refusal detail, or None when the fresh executable gap is
    still within the proposal's configured safety buffer.
    """
    symbol = state.cfg.symbol
    spot_kind, spot_payload = state.request(
        "GET", SPOT.base_url, SPOT_BOOK_PATH, {"symbol": symbol}, False)
    usdm_kind, usdm_payload = state.request(
        "GET", USDM.base_url, USDM_BOOK_PATH, {"symbol": symbol}, False)

    proposal_gap_pct = _proposal_decimal(
        accepted.payload, "entry_gap_pct", (("entry_gap_pct",),))
    tolerance_pct = _proposal_decimal(
        accepted.payload, "safety_buffer_pct", (("safety_buffer_pct",),))
    proposal_minimum_gap_pct = proposal_gap_pct - tolerance_pct
    if schedule_gap_pct is not None:
        if not isinstance(schedule_gap_pct, Decimal) \
                or not schedule_gap_pct.is_finite() \
                or schedule_gap_pct <= 0:
            raise PositionError("frozen schedule entry gap is invalid")
        minimum_gap_pct = max(
            proposal_minimum_gap_pct, schedule_gap_pct)
    else:
        minimum_gap_pct = proposal_minimum_gap_pct
    record = {
        "proposal_entry_gap_pct": fmt(proposal_gap_pct),
        "tolerance_pct": fmt(tolerance_pct),
        "minimum_acceptable_gap_pct": fmt(minimum_gap_pct),
        "fresh_entry_gap_pct": None,
        "fresh_books": {
            "spot": _book_snapshot(spot_kind, spot_payload),
            "perp": _book_snapshot(usdm_kind, usdm_payload),
        },
        "decision": "REFUSE",
        "reason": ENTRY_REVALIDATION_FAILED,
        "checked_utc": utc(),
    }
    if schedule_gap_pct is not None:
        record["schedule_entry_gap_pct"] = fmt(schedule_gap_pct)

    detail = None
    if spot_kind != "ok":
        detail = "fresh Spot book read failed"
    elif usdm_kind != "ok":
        detail = "fresh USD-M book read failed"
    else:
        try:
            _spot_bid, spot_ask = _validated_book(
                spot_payload, symbol, "Spot")
            usdm_bid, _usdm_ask = _validated_book(
                usdm_payload, symbol, "USD-M")
            fresh_gap_pct = ((usdm_bid - spot_ask) / spot_ask
                             * Decimal(100))
            record["fresh_entry_gap_pct"] = fmt(fresh_gap_pct)
            if fresh_gap_pct <= 0:
                detail = "fresh executable entry gap is not positive"
            elif fresh_gap_pct < minimum_gap_pct:
                detail = ("fresh executable entry gap %s%% is below the "
                          "minimum %s%% (proposal %s%% minus tolerance %s%%)"
                          % (fmt(fresh_gap_pct), fmt(minimum_gap_pct),
                             fmt(proposal_gap_pct), fmt(tolerance_pct)))
        except PositionError as error:
            detail = str(error)

    if detail is None:
        record["decision"] = "PROCEED"
        record["reason"] = ENTRY_REVALIDATION_PASSED
    else:
        record["detail"] = clean(detail, 300)
    state.receipt["entry_revalidation"] = record
    save(state)
    return detail


@dataclass(frozen=True)
class EntryAuthority:
    command: str
    ordinal: int
    rung_id: Optional[str]
    schedule_entry_gap_pct: Optional[Decimal]
    schedule_quote_cap: Optional[Decimal]
    effective_cap_quote: Optional[Decimal]
    config_event_digest: Optional[str]
    reconcile_id: Optional[str]
    dislocation: Dict[str, Any]


def _entry_seals(receipt_dir: str) -> List[str]:
    return [name for name in (STOP_FILE, STOP_JOBS_FILE)
            if os.path.exists(os.path.join(receipt_dir, name))]


def _entry_runner_cap(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, (bool, float)):
        raise PositionError("runner entry cap must be an exact decimal")
    try:
        cap = d(value)
    except (ArithmeticError, TypeError, ValueError):
        raise PositionError("runner entry cap must be an exact decimal") \
            from None
    if not cap.is_finite() or cap <= 0:
        raise PositionError("runner entry cap must be positive")
    return cap


def _entry_dislocation(
        accepted: AcceptedProposal, command: str) -> Dict[str, Any]:
    return new_v2_dislocation_event(
        "%s:%s" % (command.lower(), accepted.digest[:32]),
        _proposal_utc_ms(accepted.payload["created_utc"], "created_utc"),
        _proposal_decimal(
            accepted.payload, "entry_gap_pct", (("entry_gap_pct",),)),
        _proposal_decimal(
            accepted.payload, "exit_gap_pct", (("exit_gap_pct",),)),
        _proposal_decimal(
            accepted.payload, "funding_rate", (("funding_rate",),)),
        accepted.payload["funding_period_ms"],
    )


def _authority_row(
        authority: EntryAuthority, rung_id: Optional[str] = None
        ) -> Dict[str, Any]:
    return {
        "command": authority.command,
        "ordinal": authority.ordinal,
        "rung_id": authority.rung_id if rung_id is None else rung_id,
        "schedule_entry_gap_pct": None
            if authority.schedule_entry_gap_pct is None else
            fmt(authority.schedule_entry_gap_pct),
        "schedule_quote_cap": None
            if authority.schedule_quote_cap is None else
            fmt(authority.schedule_quote_cap),
        "effective_cap_quote": None
            if authority.effective_cap_quote is None else
            fmt(authority.effective_cap_quote),
        "config_event_digest": authority.config_event_digest,
        "reconcile_id": authority.reconcile_id,
        "dislocation": _copy_v2_record(
            authority.dislocation, "entry authority dislocation"),
    }


def _latest_v2_reconcile_event(
        position: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for event in reversed(position["ledger"]):
        if event["event_type"] == "ReconcileObserved":
            return event
    return None


def _require_fresh_v2_add_reconcile(
        reconcile: Dict[str, Any], now: int) -> None:
    current = _v2_epoch_ms(now, "ADD command time")
    observed = reconcile["observed_at_ms"]
    received = reconcile["read_received_at"]
    if current < observed or current < received:
        raise PositionError(
            "FUTURE_RECONCILE_READ: version-2 ADD reconcile is later than "
            "the command clock")
    if current - observed > V2_RECONCILE_READ_MAX_AGE_MS \
            or current - received > V2_RECONCILE_READ_MAX_AGE_MS:
        raise PositionError(
            "STALE_RECONCILE_READ: version-2 ADD reconcile exceeds the "
            "documented maximum age")


class _EntryPersistence:
    """Small persistence boundary used by the one shared entry core."""

    def __init__(self, cfg: PositionConfig, accepted: AcceptedProposal,
                 authority: EntryAuthority):
        self.cfg = cfg
        self.accepted = accepted
        self.authority = authority
        self.plan: Optional[CyclePlan] = None
        self.market: Optional[Dict[str, Any]] = None
        self.opened_at_ms: Optional[int] = None

    def preflight(self, state: RunState, plan: CyclePlan,
                  market: Dict[str, Any]) -> None:
        raise NotImplementedError

    def prepare(self, state: RunState, plan: CyclePlan,
                market: Dict[str, Any], now_ms: int) -> None:
        self.plan = plan
        self.market = market
        self.opened_at_ms = now_ms

    def persist_spot_intent(
            self, state: RunState, plan: CyclePlan) -> str:
        raise NotImplementedError

    def persist_short_intent(
            self, state: RunState, plan: CyclePlan,
            spot_leg: Dict[str, Any], quantity: Decimal) -> str:
        raise NotImplementedError

    def persist_unwind_intent(
            self, state: RunState, plan: CyclePlan,
            spot_leg: Dict[str, Any], short_status: str,
            quantity: Decimal) -> str:
        raise NotImplementedError

    def commit_filled(
            self, state: RunState, spot_leg: Dict[str, Any],
            short_leg: Dict[str, Any], cut_ms: int) -> Any:
        raise NotImplementedError

    def commit_aborted(
            self, state: RunState, reason: str,
            measured_facts: Dict[str, Any]) -> Any:
        raise NotImplementedError

    def record_leg(self, state: RunState, stage: str, client: str,
                   outcome: str, order: Any) -> None:
        return

    def attach_spot_fees(self, state: RunState) -> None:
        return

    def stage_spot_result(
            self, state: RunState, spot_leg: Dict[str, Any],
            hedge: Optional[Decimal]) -> None:
        return

    def freeze(self, state: RunState, detail: str,
               *, one_leg: bool = False) -> None:
        state.receipt.setdefault("entry_transition", {})["status"] = \
            "FROZEN"
        state.receipt["entry_transition"]["detail"] = clean(detail, 220)
        save(state)

    def observation_sink(self, state: RunState):
        def observe(role: str, order: Dict[str, Any]) -> None:
            observations = state.receipt.setdefault(
                "entry_observations", {})
            observations[str(role)] = dict(order)
            save(state)
        return observe

    def market_name(self, role: str) -> str:
        return "spot" if role in ("spot", "unwind") else "usdm"


class _LegacyOpenPersistence(_EntryPersistence):
    def __init__(self, cfg: PositionConfig, accepted: AcceptedProposal,
                 authority: EntryAuthority,
                 prior_position: Optional[Dict[str, Any]]):
        super().__init__(cfg, accepted, authority)
        self.position = prior_position

    def preflight(self, state: RunState, plan: CyclePlan,
                  market: Dict[str, Any]) -> None:
        _read_only_entry_preflight(state, plan, market)

    def prepare(self, state: RunState, plan: CyclePlan,
                market: Dict[str, Any], now_ms: int) -> None:
        super().prepare(state, plan, market, now_ms)
        self.position = {
            "version": 1,
            "symbol": self.cfg.symbol,
            "sha": self.cfg.sha,
            "state": ST_OPENING,
            "open_run_id": state.run_id,
            "proposal_id": self.accepted.proposal_id,
            "proposal_digest": self.accepted.digest,
            "thesis": {
                "opened_at_ms": None,
                "max_hold_ms": self.cfg.max_hold_ms,
                "watch_gap_pct": fmt(self.cfg.watch_gap_pct),
                "min_remaining_edge_pct": fmt(
                    self.cfg.min_remaining_edge_pct),
                "max_adverse_move_pct": fmt(
                    self.cfg.max_adverse_move_pct),
            },
            "entry": None,
            "legs": [],
            "client_ids": {},
            "pending_intent": None,
        }
        save_position(self.cfg.receipt_dir, self.position)
        if set_leverage_1x.run(self.cfg.symbol, state.request) != 0:
            self.position["state"] = ST_NONE
            save_position(self.cfg.receipt_dir, self.position)
            raise PreflightRefusal(
                "leverage could not be proven exactly 1x; nothing submitted")
        state.receipt["preflight"]["symbol_leverage_after"] = 1
        state.receipt["preflight"]["leverage_verified_utc"] = utc()
        save(state)

    def persist_spot_intent(
            self, state: RunState, plan: CyclePlan) -> str:
        return persist_intent(
            self.cfg.receipt_dir, self.position, ENTER_SPOT,
            plan, state.run_id)

    def persist_short_intent(
            self, state: RunState, plan: CyclePlan,
            spot_leg: Dict[str, Any], quantity: Decimal) -> str:
        return persist_intent(
            self.cfg.receipt_dir, self.position, ENTER_SHORT,
            plan, state.run_id, quantity)

    def persist_unwind_intent(
            self, state: RunState, plan: CyclePlan,
            spot_leg: Dict[str, Any], short_status: str,
            quantity: Decimal) -> str:
        return persist_intent(
            self.cfg.receipt_dir, self.position, UNWIND_SPOT,
            plan, state.run_id, quantity)

    def record_leg(self, state: RunState, stage: str, client: str,
                   outcome: str, order: Any) -> None:
        market = "spot" if stage in (ENTER_SPOT, UNWIND_SPOT) else "usdm"
        record_leg(
            self.cfg.receipt_dir, self.position, stage, client,
            market, outcome, order)

    def attach_spot_fees(self, state: RunState) -> None:
        attach_position_fees(self.cfg.receipt_dir, self.position, state)

    def stage_spot_result(
            self, state: RunState, spot_leg: Dict[str, Any],
            hedge: Optional[Decimal]) -> None:
        market = self.market
        plan = self.plan
        executed = Decimal(spot_leg["executed_quantity"])
        quote = Decimal(spot_leg["executed_quote"])
        base_fee = Decimal(
            spot_leg["fee_by_asset"].get(market["base"], "0"))
        net_base = _v2_decimal_subtract(executed, base_fee)
        self.position["entry"] = {
            "opened_utc": utc(),
            "entry_gap_bps": fmt(plan.entry_gap_bps),
            "entry_gap_pct": fmt(plan.entry_gap_bps / Decimal(100)),
            "funding_rate": fmt(plan.funding_rate),
            "proposal_net_edge_pct": fmt(self.accepted.net_edge_pct),
            "spot": {
                "executed_qty": fmt(executed),
                "quote_spent": fmt(quote),
                "base_fee": fmt(base_fee),
                "net_base": fmt(net_base),
            },
            "shared_step": fmt(market["shared_step"]),
            "spot_step": fmt(market["spot_step"]),
            "base_asset": market["base"],
            "hedge_qty": "0" if hedge is None else fmt(hedge),
        }
        save_position(self.cfg.receipt_dir, self.position)

    def commit_filled(
            self, state: RunState, spot_leg: Dict[str, Any],
            short_leg: Dict[str, Any], cut_ms: int) -> Dict[str, Any]:
        market = self.market
        plan = self.plan
        short_quantity = Decimal(short_leg["executed_quantity"])
        hedge = Decimal(state.receipt["entry_result"]["hedge_quantity"])
        entry = self.position.get("entry")
        if not isinstance(entry, dict):
            raise PositionError("legacy Spot entry snapshot is missing")
        entry["short"] = {
            "filled_qty": fmt(short_quantity),
            "cum_quote": short_leg["executed_quote"],
        }
        if short_quantity != hedge:
            entry["hedge_note"] = (
                "short filled %s of %s; imperfect hedge recorded"
                % (fmt(short_quantity), fmt(hedge)))
            first_fail(
                state, "enter_short", "short filled %s of %s"
                % (fmt(short_quantity), fmt(hedge)))
        self.position["entry"] = entry
        self.position["thesis"]["opened_at_ms"] = self.opened_at_ms
        self.position["state"] = ST_OPEN
        save_position(self.cfg.receipt_dir, self.position)
        return self.position

    def commit_aborted(
            self, state: RunState, reason: str,
            measured_facts: Dict[str, Any]) -> Dict[str, Any]:
        residual = measured_facts.get("dust_quantity", Decimal(0))
        completed = measured_facts.get("unwind_completed", False)
        no_spot = measured_facts.get("spot_status") in (
            "NOT_FOUND", "NOT_SUBMITTED")
        if completed or no_spot:
            self.position["state"] = ST_NONE
            if completed:
                self.position["unwound"] = {
                    "sold_qty": fmt(measured_facts[
                        "unwind_filled_quantity"]),
                    "residual_base": fmt(residual),
                }
            save_position(self.cfg.receipt_dir, self.position)
            release_lock(state)
        else:
            self.position["state"] = ST_ONE_LEG
            self.position["one_leg_since_ms"] = default_now_ms()
            save_position(self.cfg.receipt_dir, self.position)
        return self.position

    def freeze(self, state: RunState, detail: str,
               *, one_leg: bool = False) -> None:
        self.position["state"] = ST_ONE_LEG if one_leg else ST_UNKNOWN
        if one_leg:
            self.position["one_leg_since_ms"] = default_now_ms()
        save_position(self.cfg.receipt_dir, self.position)
        super().freeze(state, detail, one_leg=one_leg)


class _V2RungPersistence(_EntryPersistence):
    def __init__(self, cfg: PositionConfig, accepted: AcceptedProposal,
                 authority: EntryAuthority, position: Dict[str, Any],
                 reconcile: Dict[str, Any]):
        super().__init__(cfg, accepted, authority)
        self.position = position
        self.reconcile = reconcile
        self.expected_length = len(position["ledger"])
        self.expected_digest = position["ledger"][-1]["event_digest"]
        self.rung_id: Optional[str] = authority.rung_id
        self.audit_path: Optional[str] = None

    def preflight(self, state: RunState, plan: CyclePlan,
                  market: Dict[str, Any]) -> None:
        _read_only_add_preflight(
            state, plan, market, derive_v2_state(self.position),
            self.reconcile)

    def prepare(self, state: RunState, plan: CyclePlan,
                market: Dict[str, Any], now_ms: int) -> None:
        super().prepare(state, plan, market, now_ms)
        if market["base"] != self.position["ledger"][0]["venue"][
                "base_asset"]:
            raise PositionError(
                "venue base asset no longer matches the frozen config")
        _require_fresh_v2_add_reconcile(self.reconcile, now_ms)

    def _activate_receipt(
            self, state: RunState, intent: Dict[str, Any],
            persisted: Dict[str, Any]) -> None:
        active = derive_v2_state(persisted)["active_transition"]
        if active is None or active["run_id"] != state.run_id:
            raise PositionError(
                "committed AddIntent is not the active ledger transition")
        self.rung_id = active["rung_id"]
        self.position = persisted
        self.audit_path = os.path.join(
            self.cfg.receipt_dir,
            "basis-position-%s.json" % state.run_id)
        state.receipt_path = self.audit_path
        state.receipt.update({
            "kind": "position-add",
            "run_id": state.run_id,
            "sha": self.cfg.sha,
            "symbol": self.cfg.symbol,
            "proposal_id": self.accepted.proposal_id,
            "proposal_digest": self.accepted.digest,
            "ordinal": self.authority.ordinal,
            "rung_id": self.rung_id,
            "entry_authority": _authority_row(
                self.authority, self.rung_id),
            "entry_intents": {"spot": dict(active["spot_intent"])},
            "entry_transition": {
                "status": "ACTIVE",
                "stage": "SPOT_INTENT_PERSISTED",
                "rung_id": self.rung_id,
                "updated_utc": utc(),
            },
            "client_ids": {
                active["spot_intent"]["newClientOrderId"]: {
                    "stage": ENTER_SPOT, "market": "spot",
                    "run_id": state.run_id,
                },
            },
        })
        save(state)

    def persist_spot_intent(
            self, state: RunState, plan: CyclePlan) -> str:
        if os.path.exists(os.path.join(
                self.cfg.receipt_dir,
                "basis-position-%s.json" % state.run_id)):
            raise PositionError(
                "ADD run id already has a command receipt")
        current = load_position(self.cfg.receipt_dir)
        if current is None or not _is_v2_position(current):
            raise PositionError("version-2 ADD ledger disappeared")
        if len(current["ledger"]) != self.expected_length \
                or current["ledger"][-1]["event_digest"] \
                != self.expected_digest:
            raise PositionError(
                "ADD_AUTHORITY_CHANGED: ledger changed before Spot intent")
        intent = leg_intent(ENTER_SPOT, plan, state.run_id)
        payload = new_v2_add_intent_event(
            self.authority.ordinal, state.run_id,
            self.accepted.proposal_id, self.accepted.digest,
            self.authority.config_event_digest,
            self.authority.effective_cap_quote,
            self.authority.schedule_entry_gap_pct,
            self.authority.dislocation, intent)
        persisted = append_v2_event(
            self.cfg.receipt_dir, payload, self.expected_length,
            expected_last_digest=self.expected_digest)
        self._activate_receipt(state, intent, persisted)
        return intent["newClientOrderId"]

    def persist_short_intent(
            self, state: RunState, plan: CyclePlan,
            spot_leg: Dict[str, Any], quantity: Decimal) -> str:
        intent = leg_intent(ENTER_SHORT, plan, state.run_id, quantity)
        current = load_position(self.cfg.receipt_dir)
        active = derive_v2_state(current)["active_transition"]
        if active is None or active["rung_id"] != self.rung_id \
                or active["stage"] != "SPOT_INTENT_PERSISTED":
            raise PositionError(
                "active rung no longer licenses a short intent")
        persisted = append_v2_event(
            self.cfg.receipt_dir,
            new_v2_add_hedge_intent_event(
                self.authority.ordinal, state.run_id, spot_leg, intent),
            len(current["ledger"]),
            expected_last_digest=current["ledger"][-1]["event_digest"])
        self.position = persisted
        active = derive_v2_state(persisted)["active_transition"]
        state.receipt["entry_intents"]["short"] = dict(
            active["short_intent"])
        state.receipt["entry_transition"].update({
            "stage": "SHORT_INTENT_PERSISTED", "updated_utc": utc(),
        })
        state.receipt["client_ids"][intent["newClientOrderId"]] = {
            "stage": ENTER_SHORT, "market": "usdm",
            "run_id": state.run_id,
        }
        save(state)
        return intent["newClientOrderId"]

    def persist_unwind_intent(
            self, state: RunState, plan: CyclePlan,
            spot_leg: Dict[str, Any], short_status: str,
            quantity: Decimal) -> str:
        intent = leg_intent(UNWIND_SPOT, plan, state.run_id, quantity)
        current = load_position(self.cfg.receipt_dir)
        active = derive_v2_state(current)["active_transition"]
        if active is None or active["rung_id"] != self.rung_id:
            raise PositionError(
                "active rung no longer licenses a Spot unwind")
        net = Decimal(spot_leg["executed_quantity"]) - Decimal(
            spot_leg["fee_by_asset"].get(
                current["ledger"][0]["venue"]["base_asset"], "0"))
        dust = _v2_decimal_subtract(net, quantity)
        persisted = append_v2_event(
            self.cfg.receipt_dir,
            new_v2_add_unwind_intent_event(
                self.authority.ordinal, state.run_id, spot_leg,
                short_status, net, quantity, dust, intent),
            len(current["ledger"]),
            expected_last_digest=current["ledger"][-1]["event_digest"])
        self.position = persisted
        active = derive_v2_state(persisted)["active_transition"]
        state.receipt["entry_intents"]["unwind"] = dict(
            active["unwind_intent"])
        state.receipt["entry_transition"].update({
            "stage": "UNWIND_INTENT_PERSISTED", "updated_utc": utc(),
        })
        state.receipt["client_ids"][intent["newClientOrderId"]] = {
            "stage": UNWIND_SPOT, "market": "spot",
            "run_id": state.run_id,
        }
        save(state)
        return intent["newClientOrderId"]

    def commit_filled(
            self, state: RunState, spot_leg: Dict[str, Any],
            short_leg: Dict[str, Any], cut_ms: int) -> Dict[str, Any]:
        current = load_position(self.cfg.receipt_dir)
        active = derive_v2_state(current)["active_transition"]
        if active is None or active["rung_id"] != self.rung_id \
                or active["stage"] != "SHORT_INTENT_PERSISTED":
            raise PositionError(
                "active rung no longer licenses RungFilled")
        event_cut = max(
            _v2_epoch_ms(cut_ms, "entry fill cut"),
            current["ledger"][-1]["cut_ms"] + 1)
        persisted = append_v2_event(
            self.cfg.receipt_dir,
            new_v2_rung_filled_event(
                self.authority.ordinal,
                {"spot": spot_leg, "short": short_leg},
                self.authority.dislocation, event_cut),
            len(current["ledger"]),
            expected_last_digest=current["ledger"][-1]["event_digest"])
        state.receipt["entry_transition"].update({
            "status": "COMMITTED",
            "stage": "RUNG_FILLED_COMMITTED",
            "ledger_event_count": len(persisted["ledger"]),
            "updated_utc": utc(),
        })
        save(state)
        self.position = persisted
        return persisted

    def commit_aborted(
            self, state: RunState, reason: str,
            measured_facts: Dict[str, Any]) -> Dict[str, Any]:
        current = load_position(self.cfg.receipt_dir)
        active = derive_v2_state(current)["active_transition"]
        if active is None or active["rung_id"] != self.rung_id:
            raise PositionError(
                "active rung no longer licenses AddAborted")
        payload = new_v2_add_aborted_event(
            self.authority.ordinal, state.run_id, reason,
            measured_facts["spot_status"],
            measured_facts.get("short_status", "NOT_SEALED"),
            measured_facts.get("spot_net_quantity", Decimal(0)),
            measured_facts.get("unwind_requested_quantity", Decimal(0)),
            measured_facts.get("unwind_filled_quantity", Decimal(0)),
            measured_facts.get("dust_quantity", Decimal(0)),
            measured_facts.get("spot_leg"),
            measured_facts.get("unwind_leg"),
            measured_facts.get("unwind_base_fee_quantity", Decimal(0)))
        persisted = append_v2_event(
            self.cfg.receipt_dir, payload, len(current["ledger"]),
            expected_last_digest=current["ledger"][-1]["event_digest"])
        state.receipt["entry_transition"].update({
            "status": "ABORTED",
            "stage": ST_ADD_ABORTED,
            "reason": reason,
            "ledger_event_count": len(persisted["ledger"]),
            "updated_utc": utc(),
        })
        save(state)
        self.position = persisted
        return persisted

    def market_name(self, role: str) -> str:
        venue = self.position["ledger"][0]["venue"]
        return venue["spot"] if role in ("spot", "unwind") \
            else venue["futures"]


def _prepare_open_entry(
        cfg: PositionConfig, accepted: AcceptedProposal,
        runner_cap: Any = None
        ) -> Tuple[EntryAuthority, _LegacyOpenPersistence]:
    missing = [name for name, value in (
        ("MAX_HOLD_MS", cfg.max_hold_ms),
        ("MIN_REMAINING_EDGE_PCT", cfg.min_remaining_edge_pct),
        ("MAX_ADVERSE_MOVE_PCT", cfg.max_adverse_move_pct))
        if value is None]
    if missing:
        raise PositionError(
            "owner decision required: %s have no spec default and must be set"
            % ", ".join(missing))
    blocker = _prior_receipt_blocker(cfg.receipt_dir)
    if blocker:
        raise _EntryBlocked(blocker)
    try:
        position = load_position(cfg.receipt_dir)
    except PositionError as error:
        raise _EntryBlocked(str(error)) from None
    if position and _is_v2_position(position):
        raise _EntryBlocked(
            "version-2 position stands; OPEN cannot replace it")
    if position and position.get("state") in STANDING_STATES:
        raise _EntryBlocked(
            "position state %s stands; one-position machine"
            % position.get("state"))
    authority = EntryAuthority(
        command="OPEN", ordinal=1, rung_id=None,
        schedule_entry_gap_pct=None, schedule_quote_cap=None,
        effective_cap_quote=_entry_runner_cap(runner_cap),
        config_event_digest=accepted.payload["config_digest"],
        reconcile_id=None,
        dislocation=_entry_dislocation(accepted, "OPEN"))
    return authority, _LegacyOpenPersistence(
        cfg, accepted, authority, position)


def _prepare_add_entry(
        cfg: PositionConfig, accepted: AcceptedProposal, now: int,
        runner_cap: Any = None
        ) -> Tuple[EntryAuthority, _V2RungPersistence]:
    position = load_position(cfg.receipt_dir)
    if position is None or not _is_v2_position(position):
        raise PositionError(
            "cmd_add requires a version-2 ledger; version-1 is unchanged")
    for event in position["ledger"]:
        if event["event_type"] == "AddIntent" \
                and event["proposal_digest"] == accepted.digest:
            raise PositionError(
                "proposal digest already owns a version-2 entry rung")
        if cfg.run_id and event["event_type"] == "AddIntent" \
                and event["run_id"] == cfg.run_id:
            raise PositionError(
                "ADD run id already owns a version-2 entry rung")
    if cfg.run_id and os.path.exists(os.path.join(
            cfg.receipt_dir,
            "basis-position-%s.json" % cfg.run_id)):
        raise PositionError("ADD run id already has a command receipt")
    derived = derive_v2_state(position)
    if V2_DUST_STANDING in derived["freeze_reasons"] \
            or derived["dust_rows"]:
        raise PositionError("version-2 ADD is blocked by DUST_STANDING")
    if derived["top_state"] != ST_OPEN:
        raise PositionError(
            "version-2 ADD requires derived OPEN; current state is %s"
            % derived["top_state"])
    if derived["increases_frozen"] or derived["freeze_reasons"]:
        raise PositionError(
            "version-2 ADD is blocked by derived freeze: %s"
            % (",".join(derived["freeze_reasons"]) or "UNKNOWN"))
    if derived["reconciliation_status"] != "CONFIRMED":
        raise PositionError(
            "version-2 ADD requires a confirmed ReconcileObserved read")
    reconcile = _latest_v2_reconcile_event(position)
    if reconcile is None \
            or reconcile["reconcile_id"] != derived["last_reconcile_id"]:
        raise PositionError(
            "version-2 ADD cannot bind the confirmed reconcile event")
    _require_fresh_v2_add_reconcile(reconcile, now)
    ordinal = derived["next_ordinal"]
    if ordinal is None:
        raise PositionError(
            "version-2 ADD has no next frozen schedule row")
    config = position["ledger"][0]
    row = config["schedule"][ordinal - 1]
    schedule_gap = Decimal(row["entry_gap_pct"])
    schedule_cap = Decimal(row["quote_cap"])
    entry_cap = Decimal(config["caps"]["entry_quote"])
    if schedule_cap > FIRST_TRADE_CAP_USDT:
        raise PositionError(
            "ADD_HARD_CAP_SCHEDULE: frozen schedule cap exceeds 8 USDT")
    if entry_cap > FIRST_TRADE_CAP_USDT:
        raise PositionError(
            "ADD_HARD_CAP_ENTRY: frozen entry cap exceeds 8 USDT")
    effective = min(
        schedule_cap, entry_cap,
        Decimal(derived["cap_headroom_quote"]), FIRST_TRADE_CAP_USDT)
    cap = _entry_runner_cap(runner_cap)
    if cap is not None:
        effective = min(effective, cap)
    if effective <= 0:
        raise PositionError("version-2 ADD has no positive cap headroom")
    proposal_gap = _proposal_decimal(
        accepted.payload, "entry_gap_pct", (("entry_gap_pct",),))
    if proposal_gap < schedule_gap:
        raise PositionError(
            "proposal entry gap is below the frozen schedule row")
    authority = EntryAuthority(
        command="ADD", ordinal=ordinal, rung_id=None,
        schedule_entry_gap_pct=schedule_gap,
        schedule_quote_cap=schedule_cap,
        effective_cap_quote=effective,
        config_event_digest=config["event_digest"],
        reconcile_id=reconcile["reconcile_id"],
        dislocation=_entry_dislocation(accepted, "ADD"))
    return authority, _V2RungPersistence(
        cfg, accepted, authority, position, reconcile)


def _entry_state(
        cfg: PositionConfig, request, sleep,
        accepted: AcceptedProposal, authority: EntryAuthority) -> RunState:
    receipted = authority.command == "OPEN"
    state = make_state(
        cfg, request, sleep, authority.command.lower(),
        receipted=receipted)
    if not receipted:
        state.receipt.update({
            "kind": "position-add",
            "run_id": state.run_id,
            "sha": cfg.sha,
            "symbol": cfg.symbol,
            "started_utc": utc(),
            "legs": [],
        })
        start = base_event(state, "run_start")
        start["note"] = contract_note("basis-position add", ())
        state.events.append(start)
    state.receipt.update({
        "proposal_id": accepted.proposal_id,
        "proposal_digest": accepted.digest,
        "entry_authority": _authority_row(authority),
    })
    save(state)
    return state


def _entry_order_leg(
        state: RunState, persistence: _EntryPersistence,
        role: str, client: str, order: Any) -> Dict[str, Any]:
    if not isinstance(order, dict) \
            or order.get("symbol") != state.cfg.symbol \
            or order.get("clientOrderId") != client:
        raise PositionError(
            "ORDER_RESPONSE_IDENTITY_MISMATCH: terminal order does not bind")
    order_id = order.get("orderId")
    if order_id is None or not str(order_id):
        raise PositionError(
            "ORDER_RESPONSE_IDENTITY_MISMATCH: exchange order id is missing")
    venue = SPOT if role in ("spot", "unwind") else USDM
    try:
        executed = d(order["executedQty"])
        quote = d(order[venue.quote_field])
    except (ArithmeticError, KeyError, TypeError, ValueError):
        raise PositionError("terminal entry quantities are unreadable") \
            from None
    if not executed.is_finite() or executed < 0 \
            or not quote.is_finite() or quote < 0:
        raise PositionError("terminal entry quantities are unreadable")
    stage = {
        "spot": ENTER_SPOT,
        "short": ENTER_SHORT,
        "unwind": UNWIND_SPOT,
    }[role]
    receipt_rows = [
        row for row in state.receipt.get("legs", [])
        if isinstance(row, dict) and row.get("stage") == stage
    ]
    if len(receipt_rows) != 1:
        raise PositionError("entry leg receipt is not singular")
    fee_map = receipt_rows[0].get("fee_by_asset", {})
    if not isinstance(fee_map, dict):
        raise PositionError("entry fee map is unreadable")
    return {
        "stage": stage,
        "market": persistence.market_name(role),
        "client_order_id": client,
        "exchange_order_id": str(order_id),
        "terminal_status": str(order.get("status", "")),
        "executed_quantity": fmt(executed),
        "executed_quote": fmt(quote),
        "fee_by_asset": dict(fee_map),
    }


def _settle_entry_leg(
        state: RunState, persistence: _EntryPersistence,
        stage: str, plan: CyclePlan,
        quantity: Optional[Decimal] = None):
    original_request = state.request

    def sealed_request(method, base_url, path, params=None, signed=False):
        if method == "POST" and stage == ENTER_SPOT \
                and path.endswith("/order"):
            seals = _entry_seals(state.cfg.receipt_dir)
            state.receipt.setdefault("entry_seal_rows", []).append({
                "row_type": "ENTRY_PRE_SPOT_POST_SEAL",
                "stage": stage,
                "present": list(seals),
                "action": "REFUSE" if seals else "ALLOW",
                "checked_utc": utc(),
            })
            save(state)
            if seals:
                raise _EntrySealBeforeSpot(seals)
        return original_request(method, base_url, path, params, signed)

    state.request = sealed_request
    try:
        return settle_leg(
            state, stage, plan, quantity,
            query_order=query_client_order_strongest,
            observation_sink=persistence.observation_sink(state))
    finally:
        state.request = original_request


def _entry_hedge_quantity(
        accepted: AcceptedProposal, authority: EntryAuthority,
        plan: CyclePlan, market: Dict[str, Any],
        net_base: Decimal) -> Tuple[Optional[Decimal], str]:
    ceiling = market.get("futures_execution_price_ceiling")
    step = market.get("futures_step")
    minimum = market.get("futures_min_qty")
    minimum_notional = market.get("futures_min_notional")
    values = (ceiling, step, minimum, minimum_notional)
    if any(not isinstance(value, Decimal) or not value.is_finite()
           for value in values) \
            or ceiling <= 0 or step <= 0 \
            or minimum < 0 or minimum_notional < 0:
        return None, "FUTURES_EXECUTION_CEILING_UNKNOWN"
    effective = plan.short_notional if authority.effective_cap_quote is None \
        else min(plan.short_notional, authority.effective_cap_quote)
    bounded = min(
        net_base,
        accepted.quantity,
        plan.short_notional / ceiling,
        effective / ceiling)
    hedge = quantize_down(bounded, step)
    if hedge <= 0 or hedge < minimum:
        return None, "FUTURES_QUANTITY_BELOW_MINIMUM"
    if hedge * ceiling < minimum_notional:
        return None, "FUTURES_NOTIONAL_BELOW_MINIMUM"
    return hedge, ""


def _fresh_spot_sellable(
        state: RunState, market: Dict[str, Any], quantity: Decimal
        ) -> Tuple[str, Optional[Decimal], Optional[Decimal]]:
    kind, payload = state.request(
        "GET", SPOT.base_url, SPOT_BOOK_PATH,
        {"symbol": state.cfg.symbol}, False)
    state.receipt["unwind_fresh_spot_book"] = _book_snapshot(kind, payload)
    save(state)
    if kind != "ok":
        return "UNKNOWN", None, None
    try:
        bid, _ask = _validated_book(payload, state.cfg.symbol, "Spot")
        step = market["spot_step"]
        minimum = market["spot_min_qty"]
        minimum_notional = market["spot_min_notional"]
    except (KeyError, PositionError):
        return "UNKNOWN", None, None
    sellable = quantize_down(quantity, step)
    if sellable <= 0 or sellable < minimum \
            or sellable * bid < minimum_notional:
        return "DUST", None, bid
    return "SELLABLE", sellable, bid


def _entry_freeze(
        state: RunState, persistence: _EntryPersistence,
        exposure: str, note: str, *, one_leg: bool = False) -> int:
    persistence.freeze(state, note, one_leg=one_leg)
    return conclude(
        state, "UNKNOWN", exposure, note)


def _execute_entry_unwind(
        cfg: PositionConfig, state: RunState,
        persistence: _EntryPersistence, plan: CyclePlan,
        market: Dict[str, Any], spot_leg: Dict[str, Any],
        net_base: Decimal, short_status: str, reason: str) -> int:
    sellability, sellable, bid = _fresh_spot_sellable(
        state, market, net_base)
    if sellability == "UNKNOWN":
        return _entry_freeze(
            state, persistence,
            "Spot %s %s remains" % (fmt(net_base), market["base"]),
            "fresh Spot sellability is UNKNOWN; no unwind was submitted",
            one_leg=True)
    if sellable is None:
        measured = {
            "spot_status": "FILLED",
            "short_status": short_status,
            "spot_net_quantity": net_base,
            "unwind_requested_quantity": Decimal(0),
            "unwind_filled_quantity": Decimal(0),
            "unwind_base_fee_quantity": Decimal(0),
            "dust_quantity": net_base,
            "spot_leg": spot_leg,
            "unwind_leg": None,
            "unwind_completed": False,
        }
        persistence.commit_aborted(
            state, "SPOT_DUST_BELOW_SELLABLE_MINIMUM", measured)
        if persistence.authority.command == "ADD":
            return conclude(
                state, ST_ADD_ABORTED,
                "Spot dust %s %s" % (fmt(net_base), market["base"]),
                "AddAborted records fresh Spot filters and measured dust")
        return conclude(
            state, "ONE_LEG_EXPOSED",
            "Spot dust %s %s" % (fmt(net_base), market["base"]),
            "fresh Spot bid %s proves the amount is below sellability"
            % fmt(bid))
    try:
        client = persistence.persist_unwind_intent(
            state, plan, spot_leg, short_status, sellable)
    except PositionError as error:
        return _entry_freeze(
            state, persistence,
            "Spot %s %s remains" % (fmt(net_base), market["base"]),
            "durable unwind intent was refused: %s" % clean(error, 180),
            one_leg=True)
    res, order, fills = _settle_entry_leg(
        state, persistence, UNWIND_SPOT, plan, sellable)
    persistence.record_leg(
        state, UNWIND_SPOT, client, res, order)
    if res == "unknown":
        return _entry_freeze(
            state, persistence,
            "Spot unwind outcome is unconfirmed",
            "persisted unwind identity must be queried before any action",
            one_leg=True)
    sold = d(order.get("executedQty", "0")) \
        if res == "terminal" and isinstance(order, dict) else Decimal(0)
    if res == "rejected" or sold <= 0:
        return _entry_freeze(
            state, persistence,
            "Spot %s %s remains" % (fmt(net_base), market["base"]),
            "measured Spot unwind had no positive terminal fill",
            one_leg=True)
    try:
        residual = sold_leg_result(
            state, UNWIND_SPOT, order, fills,
            net_base, sellable, market)
    except ExecutorExit as exit_signal:
        persistence.freeze(
            state, "shared sold-leg terminal gate refused the unwind",
            one_leg=True)
        return exit_signal.code
    unwind_leg = _entry_order_leg(
        state, persistence, "unwind", client, order)
    base_fee = Decimal(
        unwind_leg["fee_by_asset"].get(market["base"], "0"))
    measured = {
        "spot_status": "FILLED",
        "short_status": short_status,
        "spot_net_quantity": net_base,
        "unwind_requested_quantity": sellable,
        "unwind_filled_quantity": sold,
        "unwind_base_fee_quantity": base_fee,
        "dust_quantity": residual,
        "spot_leg": spot_leg,
        "unwind_leg": unwind_leg,
        "unwind_completed": True,
    }
    persistence.commit_aborted(state, reason, measured)
    verdict = ST_ADD_ABORTED \
        if persistence.authority.command == "ADD" \
        else "REJECTED_BEFORE_ENTRY"
    return conclude(
        state, verdict, "none",
        "measured Spot unwind is terminal; residual %s %s"
        % (fmt(residual), market["base"]))


def _execute_entry(
        cfg, request, sleep, now_ms, *, accepted, authority,
        persistence) -> int:
    """Execute ordinal N through the sole persistent two-leg entry core."""
    state = _entry_state(cfg, request, sleep, accepted, authority)
    acquired_lock = False
    if authority.command == "OPEN":
        if not take_lock(state):
            return conclude(
                state, "BLOCKED", "none",
                "lane lock exists; another run owns the lane")
        acquired_lock = True
    elif not os.path.exists(state.lock_path):
        if not take_lock(state):
            return conclude(
                state, "BLOCKED", "none",
                "lane lock appeared while ADD was starting")
        acquired_lock = True
    try:
        market = market_context(state)
        plan = _proposal_plan(
            accepted, market,
            effective_cap=authority.effective_cap_quote,
            enforce_first_trade_cap=authority.command == "ADD")
    except (PositionError, Refusal) as error:
        if acquired_lock:
            release_lock(state)
        return conclude(
            state, "REJECTED_BEFORE_ENTRY", "none", str(error))
    try:
        persistence.preflight(state, plan, market)
    except PreflightRefusal as error:
        if acquired_lock:
            release_lock(state)
        return conclude(state, "PREFLIGHT_REFUSED", "none", str(error))
    failure = _revalidate_entry(
        state, accepted,
        schedule_gap_pct=authority.schedule_entry_gap_pct)
    if failure is not None:
        if acquired_lock:
            release_lock(state)
        return conclude(
            state, "REJECTED_BEFORE_ENTRY", "none",
            "%s: %s" % (ENTRY_REVALIDATION_FAILED, failure))
    try:
        persistence.prepare(state, plan, market, now_ms())
    except (PositionError, PreflightRefusal) as error:
        if acquired_lock:
            release_lock(state)
        return conclude(state, "PREFLIGHT_REFUSED", "none", str(error))
    try:
        spot_client = persistence.persist_spot_intent(state, plan)
    except PositionError as error:
        if acquired_lock:
            release_lock(state)
        return conclude(
            state, "BLOCKED", "none",
            "entry intent was not committed: %s" % clean(error, 220))
    try:
        res, order, fills = _settle_entry_leg(
            state, persistence, ENTER_SPOT, plan)
    except _EntrySealBeforeSpot as seal:
        persistence.commit_aborted(state, "ENTRY_SEAL_BEFORE_SPOT_POST", {
            "spot_status": "NOT_SUBMITTED",
            "short_status": "NOT_SEALED",
            "spot_net_quantity": Decimal(0),
            "unwind_requested_quantity": Decimal(0),
            "unwind_filled_quantity": Decimal(0),
            "dust_quantity": Decimal(0),
            "spot_leg": None,
            "unwind_leg": None,
        })
        if acquired_lock:
            release_lock(state)
        verdict = ST_ADD_ABORTED if authority.command == "ADD" \
            else "REJECTED_BEFORE_ENTRY"
        return conclude(
            state, verdict, "none",
            "%s appeared before the Spot POST; no entry order was submitted"
            % (",".join(seal.seals) or "entry seal"))
    persistence.record_leg(
        state, ENTER_SPOT, spot_client, res, order)
    if res == "rejected":
        persistence.commit_aborted(state, "SPOT_INTENT_EXACTLY_NOT_FOUND", {
            "spot_status": "NOT_FOUND",
            "short_status": "NOT_SEALED",
            "spot_net_quantity": Decimal(0),
            "unwind_requested_quantity": Decimal(0),
            "unwind_filled_quantity": Decimal(0),
            "dust_quantity": Decimal(0),
            "spot_leg": None,
            "unwind_leg": None,
        })
        if acquired_lock:
            release_lock(state)
        verdict = ST_ADD_ABORTED if authority.command == "ADD" \
            else "REJECTED_BEFORE_ENTRY"
        return conclude(
            state, verdict, "none",
            "Spot entry was exactly absent after a refused submit")
    if res == "unknown":
        return _entry_freeze(
            state, persistence,
            "Spot BUY outcome is unconfirmed for %s" % spot_client,
            "persisted Spot identity must be queried; nothing is resent")
    executed = d(order.get("executedQty", "0"))
    if executed <= 0:
        return _entry_freeze(
            state, persistence,
            "Spot terminal quantity is zero",
            "aggregate agreement is required before closing the intent")
    trades, covered = spot_trades(
        state, state.receipt["legs"][-1].get("order_id", ""),
        executed, fills)
    attach_fees(state, len(state.receipt["legs"]) - 1, trades)
    persistence.attach_spot_fees(state)
    if covered < executed:
        return _entry_freeze(
            state, persistence,
            "holding up to %s %s from Spot"
            % (fmt(executed), market["base"]),
            "Spot fee truth is incomplete; no hedge was sized",
            one_leg=True)
    spot_leg = _entry_order_leg(
        state, persistence, "spot", spot_client, order)
    base_fee = Decimal(
        spot_leg["fee_by_asset"].get(market["base"], "0"))
    if base_fee > executed:
        return _entry_freeze(
            state, persistence, "Spot fee exceeds its fill",
            "entry fee truth is inconsistent", one_leg=True)
    net_base = _v2_decimal_subtract(executed, base_fee)
    spot_quote = Decimal(spot_leg["executed_quote"])
    spot_gate = None
    if executed < market["spot_min_qty"]:
        spot_gate = "SPOT_ACTUAL_QUANTITY_BELOW_MINIMUM"
    elif spot_quote < market["spot_min_notional"]:
        spot_gate = "SPOT_ACTUAL_NOTIONAL_BELOW_MINIMUM"
    elif spot_quote > plan.spot_quote_cap:
        spot_gate = "SPOT_FILL_EXCEEDS_PERSISTED_INTENT"
    hedge, hedge_refusal = _entry_hedge_quantity(
        accepted, authority, plan, market, net_base)
    state.receipt["entry_result"] = {
        "spot_executed_quantity": fmt(executed),
        "spot_executed_quote": fmt(spot_quote),
        "spot_base_fee": fmt(base_fee),
        "spot_net_quantity": fmt(net_base),
        "hedge_quantity": "0" if hedge is None else fmt(hedge),
        "hedge_refusal": hedge_refusal or spot_gate,
    }
    save(state)
    persistence.stage_spot_result(state, spot_leg, hedge)
    if spot_gate is not None or hedge is None:
        return _execute_entry_unwind(
            cfg, state, persistence, plan, market, spot_leg, net_base,
            "NOT_SEALED", spot_gate or hedge_refusal)
    try:
        short_client = persistence.persist_short_intent(
            state, plan, spot_leg, hedge)
    except PositionError as error:
        return _execute_entry_unwind(
            cfg, state, persistence, plan, market, spot_leg, net_base,
            "NOT_SEALED", "SHORT_INTENT_NOT_COMMITTED")
    res, order, _ = _settle_entry_leg(
        state, persistence, ENTER_SHORT, plan, hedge)
    persistence.record_leg(
        state, ENTER_SHORT, short_client, res, order)
    if res == "unknown":
        return _entry_freeze(
            state, persistence,
            "Spot %s %s stands; state is either unhedged spot or the full pair"
            % (fmt(net_base), market["base"]),
            "persisted short identity must be queried; nothing is resent")
    short_filled = d(order.get("executedQty", "0")) \
        if res == "terminal" and isinstance(order, dict) else Decimal(0)
    if res == "rejected" or short_filled <= 0:
        status = "REJECTED" if res == "rejected" else "ZERO_FILLED"
        return _execute_entry_unwind(
            cfg, state, persistence, plan, market, spot_leg, net_base,
            status, "HEDGE_NOT_FILLED_SPOT_UNWOUND")
    short_leg = _entry_order_leg(
        state, persistence, "short", short_client, order)
    short_quote = Decimal(short_leg["executed_quote"])
    short_cap = plan.short_notional \
        if authority.effective_cap_quote is None else min(
            plan.short_notional, authority.effective_cap_quote)
    terminal_refusal = None
    if short_filled < market["futures_min_qty"]:
        terminal_refusal = "SHORT_ACTUAL_QUANTITY_BELOW_MINIMUM"
    elif short_quote < market["futures_min_notional"]:
        terminal_refusal = "SHORT_ACTUAL_NOTIONAL_BELOW_MINIMUM"
    elif short_filled > hedge:
        terminal_refusal = "SHORT_FILL_EXCEEDS_PERSISTED_INTENT"
    elif short_quote > short_cap:
        terminal_refusal = "SHORT_FILL_EXCEEDS_QUOTE_CAP"
    if terminal_refusal is not None:
        return _entry_freeze(
            state, persistence,
            "positive short fill remains outside terminal entry gates",
            terminal_refusal, one_leg=True)
    try:
        persistence.commit_filled(
            state, spot_leg, short_leg, now_ms())
    except PositionError as error:
        return _entry_freeze(
            state, persistence,
            "both own orders are terminal but entry commit is unproved",
            clean(error, 220), one_leg=True)
    return conclude(
        state, "OPEN",
        "entry ordinal %s records Spot net %s %s and short %s"
        % (authority.ordinal, fmt(net_base), market["base"],
           fmt(short_filled)),
        "terminal own-order fills were committed before the audit verdict")


def cmd_open(cfg: PositionConfig, request, sleep=time.sleep,
             now_ms: Callable[[], int] = default_now_ms,
             proposal_id: Optional[str] = None, *,
             runner_cap: Optional[Decimal] = None) -> int:
    seals = _entry_seals(cfg.receipt_dir)
    if seals:
        print("OPEN | %s | BLOCKED | %s file present; entry is disabled"
              % (cfg.symbol, seals[0]))
        return EXIT_CODE["BLOCKED"]
    try:
        accepted = load_entry_proposal(
            cfg.receipt_dir, proposal_id, cfg.symbol, cfg.sha, now_ms())
        authority, persistence = _prepare_open_entry(
            cfg, accepted, runner_cap)
    except _EntryBlocked as error:
        print("OPEN | %s | BLOCKED | %s" % (
            cfg.symbol, clean(error, 300)))
        return EXIT_CODE["BLOCKED"]
    except PositionError as error:
        print("OPEN | %s | REFUSED | %s" % (
            cfg.symbol, clean(error, 300)))
        return EXIT_CODE["REFUSED"]
    return _execute_entry(
        cfg, request, sleep, now_ms, accepted=accepted,
        authority=authority, persistence=persistence)


def cmd_add(cfg: PositionConfig, request, sleep=time.sleep,
            now_ms: Callable[[], int] = default_now_ms,
            proposal_id: Optional[str] = None, *,
            runner_cap: Optional[Decimal] = None) -> int:
    seals = _entry_seals(cfg.receipt_dir)
    if seals:
        print("ADD | %s | BLOCKED | %s file present; entry is disabled"
              % (cfg.symbol, seals[0]))
        return EXIT_CODE["BLOCKED"]
    command_now = now_ms()
    try:
        accepted = load_entry_proposal(
            cfg.receipt_dir, proposal_id, cfg.symbol, cfg.sha, command_now)
        authority, persistence = _prepare_add_entry(
            cfg, accepted, command_now, runner_cap)
    except PositionError as error:
        print("ADD | %s | BLOCKED | %s" % (
            cfg.symbol, clean(error, 300)))
        return EXIT_CODE["BLOCKED"]
    return _execute_entry(
        cfg, request, sleep, now_ms, accepted=accepted,
        authority=authority, persistence=persistence)
def hold_decision(position: Dict[str, Any], current_gap_pct: Decimal,
                  fees_pct: Decimal, emergency_present: bool,
                  now_ms: int) -> Dict[str, Any]:
    """The scope's four exit triggers in supervision-ladder priority:
    emergency file (owner order, risk-reducing, above thesis rows), then
    row 7 (max-hold URGENT, convergence target ROUTINE), then row 14 (edge
    inversion). All values PERCENT."""
    if emergency_present:
        return _decision(REQUEST_EMERGENCY_UNWIND, ["EMERGENCY_FILE_PRESENT"],
                         EMERGENCY, RISK_REDUCING,
                         {"kind": "FLAT", "quote_notional": "0"},
                         now_ms, now_ms)
    thesis = position["thesis"]
    opened = _safe_ms(thesis.get("opened_at_ms"))
    max_hold = _safe_ms(thesis.get("max_hold_ms"))
    if opened is not None and max_hold is not None \
            and now_ms - opened >= max_hold:
        return _decision(REQUEST_EXIT, ["MAXIMUM_HOLDING_HORIZON",
                                        "held %d ms >= max hold %d ms"
                                        % (now_ms - opened, max_hold)],
                         URGENT, RISK_REDUCING,
                         {"kind": "FLAT", "quote_notional": "0"},
                         now_ms, now_ms)
    if current_gap_pct is None or not position.get("entry"):
        # Fail-closed guard (unreachable through this machine's own flow —
        # the entry snapshot is persisted before the short leg): gap
        # triggers cannot be judged, so freeze; never fabricate an exit.
        return _decision(FREEZE_NEW_ACTIONS, ["BASIS_REQUIRED_TRUTH_UNKNOWN"],
                         URGENT, RISK_NONE, None, None, now_ms,
                         completeness="UNKNOWN",
                         blocking=("ENTRY_SNAPSHOT_OR_GAP_UNKNOWN",))
    watch = d(thesis["watch_gap_pct"])
    entry_gap = d(position["entry"]["entry_gap_pct"])
    if current_gap_pct < watch:
        return _decision(REQUEST_EXIT, ["THESIS_TARGET_REACHED",
                                        "gap_closed: entry gap %s%% below "
                                        "watch %s%%"
                                        % (fmt(current_gap_pct), fmt(watch))],
                         ROUTINE, RISK_REDUCING,
                         {"kind": "FLAT", "quote_notional": "0"},
                         None, now_ms)
    remaining = current_gap_pct - fees_pct
    min_edge = d(thesis["min_remaining_edge_pct"])
    if remaining <= min_edge:
        return _decision(REQUEST_EXIT, ["REMAINING_EDGE_EXHAUSTED",
                                        "remaining edge %s%% <= minimum %s%%"
                                        % (fmt(remaining), fmt(min_edge))],
                         URGENT, RISK_REDUCING,
                         {"kind": "FLAT", "quote_notional": "0"},
                         now_ms, now_ms)
    adverse = max(Decimal(0), abs(current_gap_pct) - abs(entry_gap))
    max_move = d(thesis["max_adverse_move_pct"])
    if adverse > max_move:
        return _decision(REQUEST_EXIT, ["BASIS_DIVERGENCE_INVALIDATION",
                                        "adverse move %s%% > maximum %s%%"
                                        % (fmt(adverse), fmt(max_move))],
                         URGENT, RISK_REDUCING,
                         {"kind": "FLAT", "quote_notional": "0"},
                         now_ms, now_ms)
    return _decision(HOLD, ["BASIS_THESIS_REMAINS_WITHIN_POLICY"], ROUTINE,
                     RISK_NONE, None, None, now_ms)


def cmd_check(cfg: PositionConfig, request, sleep=time.sleep,
              now_ms: Callable[[], int] = default_now_ms) -> int:
    try:
        position = load_position(cfg.receipt_dir)
        loaded_revision = _position_revision(position)
    except PositionError as error:
        print("CHECK | %s | BLOCKED | %s" % (cfg.symbol, clean(error, 300)))
        return EXIT_CODE["BLOCKED"]
    if position is None or position.get("state") in (ST_NONE, ST_CLOSED):
        print("CHECK | %s | no open position" % cfg.symbol)
        return EXIT_CODE["REFUSED"]
    if position.get("symbol") != cfg.symbol:
        print("CHECK | position holds %s, command names %s | refusing "
              "mismatch" % (position.get("symbol"), cfg.symbol))
        return EXIT_CODE["REFUSED"]
    if position["state"] == ST_UNKNOWN:
        print("CHECK | %s | UNKNOWN | lane frozen; run --resume to "
              "reconcile by client id before anything" % cfg.symbol)
        return EXIT_CODE["UNKNOWN"]
    if position["state"] in (ST_OPENING, ST_CLOSING, ST_ONE_LEG):
        print("CHECK | %s | %s | standing exposure; run --resume"
              % (cfg.symbol, position["state"]))
        return EXIT_CODE["ONE_LEG_EXPOSED"]

    state = make_state(cfg, request, sleep, "check", receipted=False)
    tick = now_ms()
    emergency = os.path.exists(os.path.join(cfg.receipt_dir, EMERGENCY_FILE))
    if emergency:
        decision = hold_decision(position, Decimal(0), Decimal(0), True, tick)
        return _print_decision(cfg, position, decision, None, "emergency "
                               "file present; reads skipped")
    try:
        row = gap_row(state)
    except Refusal as refusal:
        # Unreadable truth freezes; it never fabricates an exit.
        decision = _decision(FREEZE_NEW_ACTIONS,
                             ["BASIS_REQUIRED_TRUTH_UNKNOWN",
                              clean(refusal, 120)],
                             URGENT, RISK_NONE, None, None, tick,
                             completeness="UNKNOWN",
                             blocking=("BASIS_FACT_FRESHNESS",))
        return _print_decision(cfg, position, decision, None,
                               "public reads unusable; no exit fabricated")
    current_gap_pct = row.entry_gap_bps_vs_spot_ask / Decimal(100)
    fees, fees_source = exit_fees_pct(cfg.receipt_dir)
    decision = hold_decision(position, current_gap_pct, fees, False, tick)
    position["last_check"] = {
        "utc": utc(), "now_ms": tick,
        "current_gap_pct": fmt(current_gap_pct),
        "exit_fees_pct": fmt(fees), "fees_source": fees_source,
        "decision": decision["kind"], "why": decision["why"][0],
    }
    try:
        save_position(cfg.receipt_dir, position,
                      expected_revision=loaded_revision)
    except PositionError as error:
        print("CHECK | %s | BLOCKED | %s"
              % (cfg.symbol, clean(error, 300)))
        return EXIT_CODE["BLOCKED"]
    return _print_decision(cfg, position, decision, current_gap_pct,
                           "exit fees %s%% (%s)" % (fmt(fees), fees_source))


def _print_decision(cfg: PositionConfig, position: Dict[str, Any],
                    decision: Dict[str, Any], current_gap_pct,
                    note: str) -> int:
    print("lane: BASIS-POSITION")
    print("symbol:", cfg.symbol)
    if position.get("entry"):
        print("entry_gap_pct:", position["entry"]["entry_gap_pct"])
    if current_gap_pct is not None:
        print("current_gap_pct:", fmt(current_gap_pct))
    print("DECISION:", decision["kind"])
    print("WHY:", "; ".join(decision["why"]))
    print("URGENCY:", decision["urgency"])
    print("NOTE:", clean(note, 300))
    if decision["kind"] == FREEZE_NEW_ACTIONS:
        return EXIT_CODE["UNKNOWN"]
    return EXIT_CODE["DECIDED"]


# --------------------------------------------------------------------------
# --close: reduce-only short close, spot sale of the actual remaining,
# reconcile flatness, P&L from fills + commissions + funding.
# --------------------------------------------------------------------------

def _close_plan(symbol: str) -> CyclePlan:
    return CyclePlan(symbol=symbol, entry_gap_bps=Decimal(0),
                     exit_gap_bps=Decimal(0), funding_rate=Decimal(0),
                     spot_quantity=Decimal(0), short_quantity=Decimal(0),
                     spot_quote_cap=Decimal(0), short_notional=Decimal(0),
                     leverage=1,
                     reason="close: persistent position ordered exit")


def _spot_usdt_fees(position: Dict[str, Any]) -> Decimal:
    """Quote-asset commissions across the position's whole life, from the
    persisted per-leg fee maps (base-asset fees already live in the
    quantities and need no second deduction)."""
    total = Decimal(0)
    for leg in position.get("legs", []):
        for asset, amount in (leg.get("fee_by_asset") or {}).items():
            if asset == "USDT":
                try:
                    total += d(amount)
                except ArithmeticError:
                    pass
    return total


def _spot_fees_by_asset(position: Dict[str, Any]) -> Dict[str, Decimal]:
    """Aggregate the persisted spot commission map without conversion."""
    totals: Dict[str, Decimal] = {}
    for leg in position.get("legs", []):
        for asset, amount in (leg.get("fee_by_asset") or {}).items():
            try:
                totals[asset] = totals.get(asset, Decimal(0)) + d(amount)
            except ArithmeticError:
                pass
    return totals


def _close_short_quantity(position: Dict[str, Any]) -> Optional[Decimal]:
    """Newest proven short size, with the entry fill as the initial proof."""
    resume_proven = position.get("resume_proven")
    if resume_proven is not None:
        raw = resume_proven.get("short_qty") \
            if isinstance(resume_proven, dict) else None
    else:
        entry = position.get("entry")
        short = entry.get("short") if isinstance(entry, dict) else None
        raw = short.get("filled_qty") if isinstance(short, dict) else None
    try:
        quantity = d(raw)
    except (ArithmeticError, TypeError):
        return None
    if not quantity.is_finite() or quantity <= 0:
        return None
    return quantity


def cmd_close(cfg: PositionConfig, request, sleep=time.sleep,
              now_ms: Callable[[], int] = default_now_ms) -> int:
    try:
        position = load_position(cfg.receipt_dir)
    except PositionError as error:
        print("CLOSE | %s | BLOCKED | %s" % (cfg.symbol, clean(error, 300)))
        return EXIT_CODE["BLOCKED"]
    if position is None or position.get("state") != ST_OPEN:
        print("CLOSE | %s | no OPEN position (state %s); --resume owns "
              "recovery" % (cfg.symbol,
                            position.get("state") if position else "NONE"))
        return EXIT_CODE["REFUSED"]
    if position.get("symbol") != cfg.symbol:
        print("CLOSE | position holds %s, command names %s | refusing "
              "mismatch" % (position.get("symbol"), cfg.symbol))
        return EXIT_CODE["REFUSED"]
    short_qty = _close_short_quantity(position)
    if short_qty is None:
        print("CLOSE | %s | REFUSED | no proven short quantity in the "
              "persisted entry or latest resume measurement" % cfg.symbol)
        return EXIT_CODE["REFUSED"]
    entry = position["entry"]
    net_base = d(entry["spot"]["net_base"])
    spot_step = d(entry["spot_step"])
    base_asset = entry["base_asset"]

    state = make_state(cfg, request, sleep, "close")
    plan = _close_plan(cfg.symbol)
    position["state"] = ST_CLOSING
    position["close_run_id"] = state.run_id
    save_position(cfg.receipt_dir, position)

    # Leg 3 — reduce-only BUY close of the EXACT recorded short.
    client = persist_intent(cfg.receipt_dir, position, EXIT_SHORT, plan,
                            state.run_id, short_qty)
    res, order, _ = settle_leg(state, EXIT_SHORT, plan, short_qty)
    record_leg(cfg.receipt_dir, position, EXIT_SHORT, client, "usdm", res,
               order)
    if res == "unknown":
        position["state"] = ST_UNKNOWN
        save_position(cfg.receipt_dir, position)
        return conclude(state, "UNKNOWN",
                        "short close unconfirmed: state is either the "
                        "intact pair or unhedged spot; query %s" % client,
                        "lane frozen; nothing resent")
    closed = d(order.get("executedQty", "0")) if res == "terminal" \
        else Decimal(0)
    if closed <= 0:
        position["state"] = ST_OPEN     # pair intact, factually
        save_position(cfg.receipt_dir, position)
        return conclude(state, "OPEN_EXPOSURE",
                        "reduce-only close rejected; hedged pair intact "
                        "(spot %s %s + short %s)"
                        % (fmt(net_base), base_asset, fmt(short_qty)),
                        "position remains OPEN; no repair trade")
    if closed < short_qty:
        position["state"] = ST_CLOSING
        save_position(cfg.receipt_dir, position)
        first_fail(state, "exit_short", "closed %s of short %s"
                   % (fmt(closed), fmt(short_qty)))
        return conclude(state, "OPEN_EXPOSURE",
                        "reduce-only close executed %s of short %s; short "
                        "remains partly open with spot %s %s held"
                        % (fmt(closed), fmt(short_qty), fmt(net_base),
                           base_asset),
                        "partial close; --resume reconciles; no repair trade")
    exit_short_quote = d(order.get(USDM.quote_field, "0"))

    # Leg 4 — spot SELL of the actual remaining base.
    sell_qty = quantize_down(net_base, spot_step)
    client = persist_intent(cfg.receipt_dir, position, EXIT_SPOT, plan,
                            state.run_id, sell_qty)
    res, order, fills = settle_leg(state, EXIT_SPOT, plan, sell_qty)
    record_leg(cfg.receipt_dir, position, EXIT_SPOT, client, "spot", res,
               order)
    if res == "unknown":
        position["state"] = ST_UNKNOWN
        save_position(cfg.receipt_dir, position)
        return conclude(state, "UNKNOWN",
                        "spot SELL unconfirmed: either flat or holding spot "
                        "%s %s; query %s"
                        % (fmt(net_base), base_asset, client),
                        "lane frozen; nothing resent")
    sold = d(order.get("executedQty", "0")) if res == "terminal" \
        else Decimal(0)
    if sold <= 0:
        position["state"] = ST_ONE_LEG
        position["one_leg_since_ms"] = now_ms()
        save_position(cfg.receipt_dir, position)
        return conclude(state, "ONE_LEG_EXPOSED",
                        "short closed but the spot sale was rejected; "
                        "holding unlevered spot %s %s"
                        % (fmt(net_base), base_asset),
                        "no repair trade; --resume reconciles")
    quote_received = d(order.get(SPOT.quote_field, "0"))
    trades, covered = spot_trades(
        state, state.receipt["legs"][-1].get("order_id", ""), sold, fills)
    attach_fees(state, len(state.receipt["legs"]) - 1, trades)
    attach_position_fees(cfg.receipt_dir, position, state)
    if covered < sold:
        position["state"] = ST_UNKNOWN
        save_position(cfg.receipt_dir, position)
        return conclude(state, "UNKNOWN",
                        "spot sale executed but its trade and fee data "
                        "never loaded; residual unknowable",
                        "reconcile the exit client ids before trusting "
                        "balances")
    residual = net_base - sold - base_fee_of(trades, base_asset)
    if residual >= spot_step:
        position["state"] = ST_ONE_LEG
        position["one_leg_since_ms"] = now_ms()
        save_position(cfg.receipt_dir, position)
        return conclude(state, "ONE_LEG_EXPOSED",
                        "spot sale left a sellable residual %s %s"
                        % (fmt(residual), base_asset),
                        "no repair trade; --resume reconciles")

    # Reconcile: CLOSED only on observed exchange flatness.
    kind, rows = get_retry(state, USDM.base_url, USDM_POSITION_PATH,
                           {"symbol": cfg.symbol}, signed=True)
    amount = futures_position(rows, cfg.symbol) if kind == "ok" else None
    if amount is None:
        position["state"] = ST_UNKNOWN
        save_position(cfg.receipt_dir, position)
        return conclude(state, "UNKNOWN",
                        "all legs terminal but the final futures position "
                        "is unreadable",
                        "nothing resent; reconcile from the receipt ids")
    if amount != 0:
        position["state"] = ST_ONE_LEG
        position["one_leg_since_ms"] = now_ms()
        save_position(cfg.receipt_dir, position)
        return conclude(state, "ONE_LEG_EXPOSED",
                        "futures position %s remains after the reduce-only "
                        "close" % fmt(amount),
                        "no repair trade; --resume reconciles")

    # P&L from fills + commissions + funding (income is exchange truth;
    # an unreadable income read leaves funding UNKNOWN, never assumed zero).
    entry_quote_spent = d(entry["spot"]["quote_spent"])
    try:
        entry_short_quote = d(entry.get("short", {}).get("cum_quote"))
    except (ArithmeticError, TypeError):
        entry_short_quote = None
    spot_pnl = quote_received - entry_quote_spent
    short_pnl = entry_short_quote - exit_short_quote \
        if entry_short_quote is not None else None
    spot_fees_usdt = _spot_usdt_fees(position)
    spot_fees_by_asset = _spot_fees_by_asset(position)
    opened_at = position["thesis"].get("opened_at_ms") or 0
    kind, income = get_retry(state, USDM.base_url, USDM_INCOME_PATH,
                             {"symbol": cfg.symbol,
                              "startTime": str(opened_at), "limit": "1000"},
                             signed=True)
    funding_total: Any = "UNKNOWN"
    usdm_commission: Any = "UNKNOWN"
    if kind == "ok" and isinstance(income, list):
        funding_sum, commission_sum = Decimal(0), Decimal(0)
        readable = True
        for row in income:
            if not isinstance(row, dict) or row.get("symbol") != cfg.symbol:
                continue
            try:
                value = d(row.get("income", "0"))
            except ArithmeticError:
                readable = False
                break
            if row.get("incomeType") == "FUNDING_FEE":
                funding_sum += value
            elif row.get("incomeType") == "COMMISSION":
                commission_sum += value
        if readable:
            funding_total, usdm_commission = funding_sum, commission_sum
    pnl: Dict[str, Any] = {
        "spot_quote_pnl_usdt": fmt(spot_pnl),
        "short_quote_pnl_usdt": fmt(short_pnl)
        if isinstance(short_pnl, Decimal) else "UNKNOWN",
        "spot_fees_usdt": fmt(spot_fees_usdt),
        "usdm_commission_usdt": fmt(usdm_commission)
        if isinstance(usdm_commission, Decimal) else "UNKNOWN",
        "funding_usdt": fmt(funding_total)
        if isinstance(funding_total, Decimal) else "UNKNOWN",
        "residual_base": fmt(residual),
    }
    if isinstance(short_pnl, Decimal) \
            and isinstance(funding_total, Decimal) \
            and isinstance(usdm_commission, Decimal):
        pnl["total_usdt"] = fmt(spot_pnl + short_pnl - spot_fees_usdt
                                + funding_total + usdm_commission)
    else:
        pnl["total_usdt"] = "UNKNOWN"    # funding never assumed zero
    excluded_spot_fees = {
        asset: fmt(amount) for asset, amount in spot_fees_by_asset.items()
        if asset != "USDT"
    }
    if excluded_spot_fees:
        pnl["spot_fees_by_asset"] = {
            asset: fmt(amount) for asset, amount
            in spot_fees_by_asset.items()
        }
        pnl["total_usdt_status"] = "PARTIAL_FEES_EXCLUDED"
        pnl["excluded_spot_fees_by_asset"] = excluded_spot_fees
    position["close"] = {
        "closed_utc": utc(), "short_closed_qty": fmt(closed),
        "exit_short_quote": fmt(exit_short_quote),
        "spot_sold_qty": fmt(sold), "quote_received": fmt(quote_received),
        "pnl": pnl,
    }
    position["state"] = ST_CLOSED
    save_position(cfg.receipt_dir, position)
    release_lock(state)
    return conclude(state, "CLOSED", "none",
                    "pair closed reduce-only first; residual %s below the "
                    "spot step; pnl total %s USDT"
                    % (fmt(residual), pnl["total_usdt"]))


# --------------------------------------------------------------------------
# --resume: restart recovery — exchange truth by persisted client ids BEFORE
# any decision; anything unprovable = UNKNOWN; recommendations are
# data-only, automatic:false always; nothing is ever resent.
# --------------------------------------------------------------------------


def _v2_resume_observation_payload(
        position: Dict[str, Any], observation: Any) -> Dict[str, Any]:
    if not isinstance(observation, dict):
        raise PositionError(
            "version-2 resume observation must be an object")
    copied = _copy_v2_record(observation, "resume observation")
    event_type = copied.pop("event_type", "ReconcileObserved")
    if event_type != "ReconcileObserved":
        raise PositionError(
            "version-2 resume requires a ReconcileObserved event")
    _v2_exact_fields(
        copied, _V2_RECONCILE_READ_FIELDS,
        "ReconcileObserved read")
    reconcile_facts = {
        key: copied[key] for key in _V2_RECONCILE_FACT_FIELDS
    }
    normalized = _normalize_v2_reconcile_observation(
        reconcile_facts, require_canonical=False)
    _validate_v2_reconcile_read_digest(
        {**normalized, "read_digest": copied["read_digest"]},
        "ReconcileObserved")
    expected = {}
    for event in position["ledger"]:
        if event["event_type"] != "RungFilled":
            continue
        for role in ("spot", "short"):
            leg = event["legs"][role]
            expected[leg["client_order_id"]] = leg["market"]
    observed_ids = {
        row["client_order_id"] for row in normalized["own_orders"]
    }
    completed = list(normalized["own_orders"])
    for client_id in sorted(set(expected) - observed_ids):
        completed.append({
            "client_order_id": client_id,
            "market": expected[client_id],
            # Omitted caller evidence is unknown.  NOT_FOUND is accepted only
            # when the caller explicitly supplies that private-read result.
            "status": "UNKNOWN",
            "executed_quantity": None,
        })
    normalized["own_orders"] = _normalize_v2_reconcile_own_orders(
        completed, "ReconcileObserved.own_orders",
        require_canonical=False)
    normalized["read_digest"] = _v2_reconcile_read_digest(normalized)
    return {"event_type": "ReconcileObserved", **normalized}


def derive_resume(position: Dict[str, Any],
                  truths: Dict[str, Any],
                  short_amount: Optional[Decimal] = None,
                  base_free: Optional[Decimal] = None,
                  spot_step: Optional[Decimal] = None,
                  *, receipt_dir: Optional[str] = None) -> Dict[str, Any]:
    """Derive resume truth; v2 first CAS-appends its typed observation.

    The version-1 return remains the pure legacy judgment with no automatic
    action.  Version 2 returns the ledger-derived reconciliation view.
    """
    if _is_v2_position(position):
        if receipt_dir is None:
            raise PositionError(
                "version-2 resume requires receipt_dir for its CAS append")
        _validate_v2_position(position)
        payload = _v2_resume_observation_payload(position, truths)
        ledger = position["ledger"]
        persisted = append_v2_event(
            receipt_dir, payload, len(ledger),
            expected_last_digest=ledger[-1]["event_digest"])
        return derive_v2_state(persisted)
    if spot_step is None:
        raise PositionError("version-1 resume requires spot_step")
    unresolved = []
    working = []
    fills: Dict[str, Decimal] = {}
    for client, meta in sorted(position.get("client_ids", {}).items()):
        query_state, order = truths.get(client, ("missing", None))
        if query_state == "terminal":
            executed = d(order.get("executedQty", "0")) \
                if isinstance(order, dict) else Decimal(0)
            fills[meta["stage"]] = fills.get(meta["stage"],
                                             Decimal(0)) + executed
        elif query_state == "not_found":
            fills.setdefault(meta["stage"], Decimal(0))
        elif query_state == "pending":
            working.append(client)
        else:
            unresolved.append(client)
    if short_amount is None:
        unresolved.append("positionRisk")
    if base_free is None:
        unresolved.append("spot-balance")
    if unresolved or working:
        recommendations = [{"action": "VERIFY_AMBIGUOUS_ORDER",
                            "client_id": client, "automatic": False}
                           for client in unresolved
                           if client not in ("positionRisk", "spot-balance")]
        recommendations += [{"action": "CANCEL_REMAINDER",
                             "client_id": client, "automatic": False}
                            for client in working]
        return {
            "state": ST_UNKNOWN, "verdict": "UNKNOWN",
            "note": "unprovable facts freeze the lane; blocks new entries",
            "exposure": "unproven: %s; working: %s"
                        % (", ".join(unresolved) or "none",
                           ", ".join(working) or "none"),
            "recommendations": recommendations,
        }
    entered_spot = fills.get(ENTER_SPOT, Decimal(0)) > 0
    sold_spot = fills.get(EXIT_SPOT, Decimal(0)) \
        + fills.get(UNWIND_SPOT, Decimal(0)) > 0
    if short_amount != 0:
        state = ST_OPEN if entered_spot else ST_ONE_LEG
        return {
            "state": state, "verdict": "OPEN" if state == ST_OPEN
            else "ONE_LEG_EXPOSED",
            "note": "short %s stands on the exchange" % fmt(short_amount),
            "exposure": "short %s%s" % (
                fmt(short_amount),
                "" if entered_spot else "; NO spot leg proven — naked short"),
            "recommendations": [] if entered_spot else
            [{"action": "CLOSE_EXPOSED_LEG", "automatic": False}],
        }
    if entered_spot and base_free >= spot_step:
        # A partial exit fill never proves flatness: the account's own base
        # balance is the truth, sold or not (panel HIGH 2026-08-13 — the old
        # sold_spot short-circuit declared CLOSED over a sellable residual).
        return {
            "state": ST_ONE_LEG, "verdict": "ONE_LEG_EXPOSED",
            "note": "sellable spot base still standing"
                    + ("" if not sold_spot else " after a partial exit fill"),
            "exposure": "spot base free %s at or above the spot step %s"
                        % (fmt(base_free), fmt(spot_step)),
            "recommendations": [{"action": "CLOSE_EXPOSED_LEG",
                                 "automatic": False}],
        }
    if entered_spot and base_free < spot_step:
        return {
            "state": ST_CLOSED, "verdict": "CLOSED",
            "note": "every leg terminal, futures flat, no sellable base",
            "exposure": "none", "recommendations": [],
        }
    return {
        "state": ST_NONE, "verdict": "NOTHING_STANDING",
        "note": "no fill ever happened; nothing held",
        "exposure": "none", "recommendations": [],
    }


def _entry_audit_receipts(
        receipt_dir: str) -> List[Tuple[str, Dict[str, Any]]]:
    try:
        names = sorted(os.listdir(receipt_dir))
    except FileNotFoundError:
        return []
    except OSError:
        raise PositionError("entry audit directory is unreadable") from None
    receipts = []
    for name in names:
        if not name.startswith("basis-position-") \
                or not name.endswith(".json"):
            continue
        path = os.path.join(receipt_dir, name)
        try:
            with open(path, encoding="utf-8") as handle:
                receipt = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise PositionError(
                "an entry audit receipt is unreadable") from None
        if isinstance(receipt, dict) \
                and receipt.get("kind") == "position-add":
            receipts.append((path, receipt))
    return receipts


def _entry_receipt_rung_id(receipt: Dict[str, Any]) -> Optional[str]:
    direct = receipt.get("rung_id")
    authority = receipt.get("entry_authority")
    nested = authority.get("rung_id") \
        if isinstance(authority, dict) else None
    if direct is not None and nested is not None and direct != nested:
        raise PositionError("entry audit rung identity disagrees")
    value = direct if direct is not None else nested
    return value if isinstance(value, str) and value else None


def _entry_receipt_for_rung(
        receipts: List[Tuple[str, Dict[str, Any]]], rung_id: str
        ) -> Optional[Tuple[str, Dict[str, Any]]]:
    matches = [row for row in receipts
               if _entry_receipt_rung_id(row[1]) == rung_id]
    if len(matches) > 1:
        raise PositionError(
            "more than one audit receipt binds the same rung")
    return matches[0] if matches else None


def _projection_authority(
        position: Dict[str, Any], projection: Dict[str, Any]
        ) -> EntryAuthority:
    ordinal = projection["ordinal"]
    config = position["ledger"][0]
    row = config["schedule"][ordinal - 1]
    return EntryAuthority(
        command="ADD",
        ordinal=ordinal,
        rung_id=projection["rung_id"],
        schedule_entry_gap_pct=Decimal(
            projection.get("schedule_entry_gap_pct",
                           row["entry_gap_pct"])),
        schedule_quote_cap=Decimal(row["quote_cap"]),
        effective_cap_quote=Decimal(
            projection["effective_cap_quote"]),
        config_event_digest=projection["config_event_digest"],
        reconcile_id=projection.get("reconcile_id"),
        dislocation=_copy_v2_record(
            projection["dislocation"], "recovery dislocation"),
    )


def _audit_matches_projection(
        receipt: Dict[str, Any], authority: EntryAuthority,
        projection: Dict[str, Any]) -> bool:
    row = receipt.get("entry_authority")
    if not isinstance(row, dict):
        return False
    expected = _authority_row(authority, projection["rung_id"])
    if row != expected \
            or receipt.get("run_id") != projection.get("run_id") \
            or receipt.get("proposal_id") \
            != projection.get("proposal_id") \
            or receipt.get("proposal_digest") \
            != projection.get("proposal_digest"):
        return False
    intents = receipt.get("entry_intents", {})
    for role in ("spot", "short", "unwind"):
        projected = projection.get(role + "_intent")
        if projected is not None and intents.get(role) != projected:
            return False
    return True


def _reconstruct_entry_audit(
        cfg: PositionConfig, position: Dict[str, Any],
        projection: Dict[str, Any], authority: EntryAuthority
        ) -> Tuple[str, Dict[str, Any]]:
    run_id = projection.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise PositionError(
            "legacy directly seeded rung has no resumable ADD audit")
    path = os.path.join(
        cfg.receipt_dir, "basis-position-%s.json" % run_id)
    if os.path.exists(path):
        raise PositionError(
            "rung audit path exists but does not bind its ledger identity")
    intents = {
        role: projection[role + "_intent"]
        for role in ("spot", "short", "unwind")
        if projection.get(role + "_intent") is not None
    }
    receipt = {
        "kind": "position-add",
        "run_id": run_id,
        "sha": cfg.sha,
        "symbol": cfg.symbol,
        "started_utc": utc(),
        "legs": [],
        "proposal_id": projection.get("proposal_id"),
        "proposal_digest": projection.get("proposal_digest"),
        "ordinal": projection["ordinal"],
        "rung_id": projection["rung_id"],
        "entry_authority": _authority_row(
            authority, projection["rung_id"]),
        "entry_intents": _copy_v2_record(
            intents, "reconstructed entry intents"),
        "entry_transition": {
            "status": "RECOVERING",
            "stage": projection.get("stage", "LEDGER_TERMINAL"),
            "rung_id": projection["rung_id"],
            "updated_utc": utc(),
        },
        "client_ids": {
            intent["newClientOrderId"]: {
                "stage": {"spot": ENTER_SPOT,
                          "short": ENTER_SHORT,
                          "unwind": UNWIND_SPOT}[role],
                "market": intent["market"],
                "run_id": run_id,
            }
            for role, intent in intents.items()
        },
    }
    state = make_state(
        cfg, lambda *args, **kwargs: ("unclear", {}),
        lambda seconds: None, "resume", receipted=False,
        run_id_override=run_id)
    state.receipt_path = path
    state.receipt = receipt
    save(state)
    return path, receipt


def _recovery_state(
        cfg: PositionConfig, request, sleep,
        path: str, receipt: Dict[str, Any], run_id: str) -> RunState:
    state = make_state(
        cfg, request, sleep, "resume", receipted=False,
        run_id_override=run_id)
    state.receipt_path = path
    state.receipt = receipt
    state.events.append(base_event(state, "run_start"))
    return state


def _prior_entry_observation(
        receipt: Dict[str, Any], role: str) -> Optional[Dict[str, Any]]:
    observations = receipt.get("entry_observations", {})
    if isinstance(observations, dict):
        for key in (role, {
                "spot": ENTER_SPOT,
                "short": ENTER_SHORT,
                "unwind": UNWIND_SPOT,
                }[role]):
            row = observations.get(key)
            if isinstance(row, dict):
                return dict(row)
    return None


def _merge_entry_query_truth(
        state: RunState, role: str, intent: Dict[str, Any],
        query_state: str, order: Any) -> None:
    if query_state == "identity_mismatch":
        raise PositionError(
            "ORDER_RESPONSE_IDENTITY_MISMATCH: recovery identity refused")
    stage = {"spot": ENTER_SPOT,
             "short": ENTER_SHORT,
             "unwind": UNWIND_SPOT}[role]
    rows = [row for row in state.receipt.get("legs", [])
            if isinstance(row, dict) and row.get("stage") == stage]
    if len(rows) > 1:
        raise PositionError("recovery found duplicate entry leg receipts")
    if query_state != "terminal":
        return
    if not isinstance(order, dict) \
            or order.get("symbol") != state.cfg.symbol \
            or order.get("clientOrderId") \
            != intent["newClientOrderId"]:
        raise PositionError(
            "ORDER_RESPONSE_IDENTITY_MISMATCH: recovery order does not bind")
    venue = VENUES[intent["market"]]
    try:
        executed = d(order["executedQty"])
        quote = d(order[venue.quote_field])
    except (ArithmeticError, KeyError, TypeError, ValueError):
        raise PositionError(
            "recovery terminal quantities are unreadable") from None
    order_id = order.get("orderId")
    if not executed.is_finite() or executed < 0 \
            or not quote.is_finite() or quote < 0 \
            or order_id is None or not str(order_id):
        raise PositionError("recovery terminal order truth is incomplete")
    if rows:
        row = rows[0]
        prior_id = row.get("order_id")
        if prior_id is not None and str(prior_id) != str(order_id):
            raise PositionError(
                "ORDER_RESPONSE_IDENTITY_MISMATCH: order id changed")
    else:
        row = {"stage": stage, "market": venue.name,
               "client_id": intent["newClientOrderId"]}
        state.receipt.setdefault("legs", []).append(row)
    row.update({
        "status": str(order.get("status", "")),
        "order_id": str(order_id),
        "executed_qty": fmt(executed),
        "quote": fmt(quote),
        "terminal_utc": utc(),
    })
    save(state)


def _query_recovery_role(
        state: RunState, persistence: _V2RungPersistence,
        role: str, intent: Dict[str, Any]) -> Tuple[str, Any]:
    prior = _prior_entry_observation(state.receipt, role)
    expected_order_id = None
    if prior is not None and prior.get("orderId") is not None:
        expected_order_id = str(prior["orderId"])

    def observation_sink(_observed_role: str,
                         order: Dict[str, Any]) -> None:
        state.receipt.setdefault("entry_observations", {})[role] = \
            dict(order)
        save(state)

    query_state, order = query_client_order_strongest(
        state, VENUES[intent["market"]], intent["newClientOrderId"],
        expected_order_id=expected_order_id,
        prior_order=prior,
        observation_sink=observation_sink)
    _merge_entry_query_truth(
        state, role, intent, query_state, order)
    return query_state, order


def _recovery_plan(
        active: Dict[str, Any], quantity: Decimal) -> CyclePlan:
    spot_cap = Decimal(active["spot_intent"]["quoteOrderQty"])
    effective = Decimal(active["effective_cap_quote"])
    return CyclePlan(
        symbol=active["spot_intent"]["symbol"],
        entry_gap_bps=Decimal(
            active["dislocation"]["entry_gap_pct"]) * Decimal(100),
        exit_gap_bps=Decimal(
            active["dislocation"]["exit_gap_pct"]) * Decimal(100),
        funding_rate=Decimal(active["dislocation"]["funding_rate"]),
        spot_quantity=quantity,
        short_quantity=quantity,
        spot_quote_cap=spot_cap,
        short_notional=effective,
        leverage=1,
        reason="ledger-first entry recovery",
    )


def _recovery_persistence(
        cfg: PositionConfig, position: Dict[str, Any],
        active: Dict[str, Any], authority: EntryAuthority
        ) -> _V2RungPersistence:
    accepted = AcceptedProposal(
        proposal_id=active["proposal_id"],
        digest=active["proposal_digest"],
        payload={}, quantity=Decimal("1"), net_edge_pct=Decimal("0"))
    reconcile = _latest_v2_reconcile_event(position)
    persistence = _V2RungPersistence(
        cfg, accepted, authority, position, reconcile)
    persistence.rung_id = active["rung_id"]
    return persistence


def _finish_recovered_unwind(
        state: RunState, persistence: _V2RungPersistence,
        active: Dict[str, Any], plan: CyclePlan,
        market: Dict[str, Any], spot_leg: Dict[str, Any],
        net_base: Decimal, order: Dict[str, Any]) -> int:
    intent = active["unwind_intent"]
    sellable = Decimal(intent["quantity"])
    sold = d(order.get("executedQty", "0"))
    if sold <= 0:
        return _entry_freeze(
            state, persistence, "Spot exposure remains",
            "persisted unwind has no positive terminal fill", one_leg=True)
    try:
        residual = sold_leg_result(
            state, UNWIND_SPOT, order, [], net_base, sellable, market)
    except ExecutorExit as exit_signal:
        persistence.freeze(
            state, "shared sold-leg terminal gate refused recovery",
            one_leg=True)
        return exit_signal.code
    unwind_leg = _entry_order_leg(
        state, persistence, "unwind",
        intent["newClientOrderId"], order)
    base_fee = Decimal(
        unwind_leg["fee_by_asset"].get(market["base"], "0"))
    persistence.commit_aborted(state, "HEDGE_NOT_FILLED_SPOT_UNWOUND", {
        "spot_status": "FILLED",
        "short_status": active["short_status"],
        "spot_net_quantity": net_base,
        "unwind_requested_quantity": sellable,
        "unwind_filled_quantity": sold,
        "unwind_base_fee_quantity": base_fee,
        "dust_quantity": residual,
        "spot_leg": spot_leg,
        "unwind_leg": unwind_leg,
        "unwind_completed": True,
    })
    return conclude(
        state, ST_ADD_ABORTED, "none",
        "ledger-first recovery committed measured Spot unwind")


def _finalize_orphan_entry_receipt(
        cfg: PositionConfig, request, sleep,
        receipts: List[Tuple[str, Dict[str, Any]]],
        known_rung_ids: set) -> Optional[int]:
    orphans = []
    for path, receipt in receipts:
        rung_id = _entry_receipt_rung_id(receipt)
        if rung_id not in known_rung_ids and "verdict" not in receipt:
            orphans.append((path, receipt))
    if len(orphans) > 1:
        raise PositionError("more than one receipt lacks ledger authority")
    if not orphans:
        return None
    path, receipt = orphans[0]
    receipt.update({
        "verdict": "NOT_STARTED",
        "exposure": "none",
        "verdict_note": "no initiating AddIntent exists; zero transport",
        "finished_utc": utc(),
    })
    state = _recovery_state(
        cfg, request, sleep, path, receipt,
        str(receipt.get("run_id") or cfg.run_id or "not-started"))
    save(state)
    print("RESUME | %s | NOT_STARTED | no AddIntent; zero transport"
          % cfg.symbol)
    return EXIT_CODE["REFUSED"]


def _resume_v2_entry(
        cfg: PositionConfig, request, sleep,
        now_ms: Callable[[], int], position: Dict[str, Any]) -> int:
    derived = derive_v2_state(position)
    if derived["symbol"] != cfg.symbol:
        raise PositionError(
            "version-2 ledger symbol does not match resume command")
    receipts = _entry_audit_receipts(cfg.receipt_dir)
    known_rung_ids = {
        rung["rung_id"] for rung in derived["rungs"]
    }
    if derived["active_transition"] is not None:
        known_rung_ids.add(derived["active_transition"]["rung_id"])
    if derived["add_terminal"] is not None:
        known_rung_ids.add(derived["add_terminal"]["rung_id"])

    active = derived["active_transition"]
    if active is None:
        terminal = derived["add_terminal"]
        if terminal is not None:
            authority = _projection_authority(position, terminal)
            found = _entry_receipt_for_rung(
                receipts, terminal["rung_id"])
            if found is None:
                found = _reconstruct_entry_audit(
                    cfg, position, terminal, authority)
            path, receipt = found
            if not _audit_matches_projection(
                    receipt, authority, terminal):
                raise PositionError(
                    "terminal audit identity does not bind its rung")
            if receipt.get("verdict") == ST_ADD_ABORTED:
                print("RESUME | %s | state %s | nothing to progress"
                      % (cfg.symbol, ST_ADD_ABORTED))
                return EXIT_CODE[ST_ADD_ABORTED]
            state = _recovery_state(
                cfg, request, sleep, path, receipt, terminal["run_id"])
            receipt["entry_transition"].update({
                "status": "ABORTED", "stage": ST_ADD_ABORTED,
                "reason": terminal["reason"], "updated_utc": utc(),
            })
            save(state)
            return conclude(
                state, ST_ADD_ABORTED,
                "none" if Decimal(terminal["dust_quantity"]) == 0 else
                "Spot residual %s" % terminal["dust_quantity"],
                "terminal ledger repaired its missing audit; zero transport")
        for rung in reversed(derived["rungs"]):
            if rung["run_id"] is None:
                continue
            found = _entry_receipt_for_rung(receipts, rung["rung_id"])
            if found is None:
                authority = _projection_authority(position, rung)
                path, receipt = _reconstruct_entry_audit(
                    cfg, position, rung, authority)
                state = _recovery_state(
                    cfg, request, sleep, path, receipt, rung["run_id"])
                receipt["entry_transition"].update({
                    "status": "COMMITTED",
                    "stage": "RUNG_FILLED_COMMITTED",
                    "updated_utc": utc(),
                })
                save(state)
                return conclude(
                    state, "OPEN", "rung already recorded",
                    "terminal ledger repaired its missing audit; "
                    "zero transport")
        orphan = _finalize_orphan_entry_receipt(
            cfg, request, sleep, receipts, known_rung_ids)
        if orphan is not None:
            return orphan
        print("RESUME | %s | BLOCKED | no active v2 entry; state %s"
              % (cfg.symbol, derived["top_state"]))
        return EXIT_CODE["BLOCKED"]

    authority = _projection_authority(position, active)
    found = _entry_receipt_for_rung(receipts, active["rung_id"])
    if found is None:
        found = _reconstruct_entry_audit(
            cfg, position, active, authority)
    path, receipt = found
    if not _audit_matches_projection(receipt, authority, active):
        raise PositionError(
            "active audit identity does not bind rung, proposal, and "
            "dislocation")
    state = _recovery_state(
        cfg, request, sleep, path, receipt, active["run_id"])
    persistence = _recovery_persistence(
        cfg, position, active, authority)
    state.client_ids = tuple(
        intent["newClientOrderId"] for intent in (
            active.get("spot_intent"), active.get("short_intent"),
            active.get("unwind_intent")) if intent is not None)
    try:
        spot_state, spot_order = _query_recovery_role(
            state, persistence, "spot", active["spot_intent"])
        if active.get("short_intent") is not None:
            short_state, short_order = _query_recovery_role(
                state, persistence, "short", active["short_intent"])
        else:
            short_state, short_order = None, None
        if active.get("unwind_intent") is not None:
            unwind_state, unwind_order = _query_recovery_role(
                state, persistence, "unwind", active["unwind_intent"])
        else:
            unwind_state, unwind_order = None, None
    except PositionError as error:
        return _entry_freeze(
            state, persistence, "unfinished ADD truth disagrees",
            clean(error, 220), one_leg=True)
    if spot_state == "not_found" \
            and active["stage"] == "SPOT_INTENT_PERSISTED":
        persistence.commit_aborted(
            state, "SPOT_INTENT_EXACTLY_NOT_FOUND", {
                "spot_status": "NOT_FOUND",
                "short_status": "NOT_SEALED",
                "spot_net_quantity": Decimal(0),
                "unwind_requested_quantity": Decimal(0),
                "unwind_filled_quantity": Decimal(0),
                "dust_quantity": Decimal(0),
                "spot_leg": None, "unwind_leg": None,
            })
        return conclude(
            state, ST_ADD_ABORTED, "none",
            "ledger-first recovery proved exact Spot intent absence")
    if spot_state != "terminal":
        return _entry_freeze(
            state, persistence, "ADD Spot order remains unconfirmed",
            "ledger-first recovery queried the persisted identity only")
    spot_executed = d(spot_order.get("executedQty", "0"))
    if spot_executed <= 0:
        return _entry_freeze(
            state, persistence, "ADD Spot terminal quantity is zero",
            "aggregate agreement is required", one_leg=True)
    spot_rows = [row for row in state.receipt.get("legs", [])
                 if row.get("stage") == ENTER_SPOT]
    if not isinstance(spot_rows[0].get("fee_by_asset"), dict):
        trades, covered = spot_trades(
            state, spot_rows[0]["order_id"], spot_executed, [])
        if covered < spot_executed:
            return _entry_freeze(
                state, persistence, "ADD Spot fee truth is incomplete",
                "no hedge or unwind was inferred", one_leg=True)
        attach_fees(
            state, state.receipt["legs"].index(spot_rows[0]), trades)
    spot_leg = _entry_order_leg(
        state, persistence, "spot",
        active["spot_intent"]["newClientOrderId"], spot_order)
    base_asset = position["ledger"][0]["venue"]["base_asset"]
    base_fee = Decimal(spot_leg["fee_by_asset"].get(base_asset, "0"))
    if base_fee > spot_executed:
        return _entry_freeze(
            state, persistence, "ADD Spot fee exceeds its fill",
            "fee truth is inconsistent", one_leg=True)
    net_base = _v2_decimal_subtract(spot_executed, base_fee)
    try:
        market = market_context(state)
    except Refusal as error:
        return _entry_freeze(
            state, persistence, "ADD Spot exposure remains",
            str(error), one_leg=True)
    plan = _recovery_plan(active, net_base)
    persistence.plan = plan
    persistence.market = market
    if active["stage"] == "UNWIND_INTENT_PERSISTED":
        if unwind_state != "terminal":
            return _entry_freeze(
                state, persistence, "ADD Spot unwind remains unconfirmed",
                "later absence cannot authorize a resend", one_leg=True)
        return _finish_recovered_unwind(
            state, persistence, active, plan, market,
            spot_leg, net_base, unwind_order)
    if active.get("short_intent") is None:
        return _execute_entry_unwind(
            cfg, state, persistence, plan, market, spot_leg, net_base,
            "NOT_SEALED", "CUT_BEFORE_HEDGE_INTENT")
    if short_state == "not_found":
        return _execute_entry_unwind(
            cfg, state, persistence, plan, market, spot_leg, net_base,
            "NOT_FOUND", "HEDGE_NOT_FILLED_SPOT_UNWOUND")
    if short_state != "terminal":
        return _entry_freeze(
            state, persistence,
            "ADD Spot stands; short order remains unconfirmed",
            "ledger-first recovery submitted nothing", one_leg=True)
    short_filled = d(short_order.get("executedQty", "0"))
    if short_filled <= 0:
        return _execute_entry_unwind(
            cfg, state, persistence, plan, market, spot_leg, net_base,
            "ZERO_FILLED", "HEDGE_NOT_FILLED_SPOT_UNWOUND")
    short_leg = _entry_order_leg(
        state, persistence, "short",
        active["short_intent"]["newClientOrderId"], short_order)
    short_quote = Decimal(short_leg["executed_quote"])
    intended = Decimal(active["short_intent"]["quantity"])
    if short_filled < market["futures_min_qty"] \
            or short_quote < market["futures_min_notional"] \
            or short_filled > intended \
            or short_quote > Decimal(active["effective_cap_quote"]):
        return _entry_freeze(
            state, persistence,
            "positive short fill remains outside terminal entry gates",
            "RungFilled was not committed", one_leg=True)
    try:
        persistence.commit_filled(
            state, spot_leg, short_leg, now_ms())
    except PositionError as error:
        return _entry_freeze(
            state, persistence,
            "terminal orders could not commit RungFilled",
            clean(error, 220), one_leg=True)
    return conclude(
        state, "OPEN", "ADD rung reconstructed from terminal own orders",
        "ledger-first recovery submitted no duplicate entry order")


def cmd_resume(cfg: PositionConfig, request, sleep=time.sleep,
               now_ms: Callable[[], int] = default_now_ms) -> int:
    try:
        position = load_position(cfg.receipt_dir)
    except PositionError as error:
        print("RESUME | %s | BLOCKED | %s" % (cfg.symbol, clean(error, 300)))
        return EXIT_CODE["BLOCKED"]
    lock_path = os.path.join(cfg.receipt_dir, LOCK_FILE)
    if position is None:
        if os.path.exists(lock_path):
            print("RESUME | %s | lock held but no position state | human "
                  "check required" % cfg.symbol)
            return EXIT_CODE["BLOCKED"]
        print("RESUME | %s | nothing to resume" % cfg.symbol)
        return EXIT_CODE["NOTHING_STANDING"]
    if _is_v2_position(position):
        try:
            return _resume_v2_entry(
                cfg, request, sleep, now_ms, position)
        except PositionError as error:
            print("RESUME | %s | BLOCKED | %s" % (
                cfg.symbol, clean(error, 300)))
            return EXIT_CODE["BLOCKED"]
    if position.get("symbol") != cfg.symbol:
        print("RESUME | position holds %s, command names %s | refusing "
              "mismatch" % (position.get("symbol"), cfg.symbol))
        return EXIT_CODE["REFUSED"]
    if position.get("state") in (ST_NONE, ST_CLOSED):
        print("RESUME | %s | state %s | nothing standing%s"
              % (cfg.symbol, position["state"],
                 "; lane lock present — verify ownership by hand"
                 if os.path.exists(lock_path) else ""))
        return EXIT_CODE["NOTHING_STANDING"]

    state = make_state(cfg, request, sleep, "resume")
    # Exchange truth BEFORE any decision: every persisted client id.
    truths: Dict[str, Tuple[str, Any]] = {}
    for client, meta in sorted(position.get("client_ids", {}).items()):
        venue = VENUES[meta["market"]]
        truths[client] = query_client_order(state, venue, client)
    kind, rows = get_retry(state, USDM.base_url, USDM_POSITION_PATH,
                           {"symbol": cfg.symbol}, signed=True)
    short_amount = futures_position(rows, cfg.symbol) if kind == "ok" else None
    base_free: Optional[Decimal] = None
    spot_step = Decimal("0")
    entry = position.get("entry") or {}
    if entry.get("base_asset") and entry.get("spot_step"):
        base_asset, spot_step = entry["base_asset"], d(entry["spot_step"])
    else:
        try:
            market = market_context(state)
            base_asset, spot_step = market["base"], market["spot_step"]
        except Refusal:
            base_asset = None
    if base_asset:
        kind, account = get_retry(state, SPOT.base_url, SPOT_ACCOUNT_PATH,
                                  None, signed=True)
        if kind == "ok":
            base_free = _spot_free(account, base_asset)

    judgment = derive_resume(position, truths, short_amount, base_free,
                             spot_step)
    position["state"] = judgment["state"]
    position["reconcile"] = {
        "utc": utc(),
        "run_id": state.run_id,
        "truths": {client: query_state
                   for client, (query_state, _) in truths.items()},
        "futures_position": fmt(short_amount)
        if short_amount is not None else "UNKNOWN",
        "base_free": fmt(base_free) if base_free is not None else "UNKNOWN",
        "recommendations": judgment["recommendations"],
    }
    if short_amount is not None:
        position["resume_proven"] = {
            "run_id": state.run_id,
            "short_qty": fmt(abs(short_amount)),
            "source": "positionRisk",
        }
    if judgment["state"] == ST_OPEN \
            and position["thesis"].get("opened_at_ms") is None:
        position["thesis"]["opened_at_ms"] = now_ms()   # proven late; latch
    save_position(cfg.receipt_dir, position)
    if judgment["state"] in (ST_CLOSED, ST_NONE):
        release_lock(state)
    for recommendation in judgment["recommendations"]:
        print("RECOMMEND | %s | automatic=false"
              % json.dumps(recommendation, sort_keys=True))
    return conclude(state, judgment["verdict"], judgment["exposure"],
                    judgment["note"])


# --------------------------------------------------------------------------
# CLI wiring.
# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="persistent resumable one-position basis lifecycle")
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag, text in (
            ("--rates", "signed commission-rate reads, written locally"),
            ("--open", "preflight + set-and-verify 1x + entry, persist OPEN"),
            ("--add", "add one reconciled version-2 schedule rung"),
            ("--check", "one hold tick: public reads + exit triggers"),
            ("--close", "reduce-only close + spot sale + P&L"),
            ("--resume", "restart recovery: query-first by client id")):
        mode.add_argument(flag, metavar="SYMBOL", nargs="?", const="",
                          default=None, help=text)
    parser.add_argument("--proposal", metavar="ID",
                        help="immutable proposal id required by --open/--add")
    args = parser.parse_args(argv)
    command, symbol = next(
        (name, getattr(args, name))
        for name in ("rates", "open", "add", "check", "close", "resume")
        if getattr(args, name) is not None)
    if args.proposal is not None and command not in ("open", "add"):
        parser.error("--proposal is valid only with --open/--add")
    if command in ("open", "add"):
        receipt_dir = os.path.expanduser(
            os.environ.get("RECEIPT_DIR", "~/quant_receipts"))
        if os.path.exists(os.path.join(receipt_dir, STOP_FILE)):
            display_symbol = (symbol or os.environ.get("SYMBOL", "")).upper()
            print("%s | %s | BLOCKED | STOP file present; entry is disabled"
                  % (command.upper(), display_symbol))
            return EXIT_CODE["BLOCKED"]
    try:
        cfg = config_from_env(os.environ, symbol)
    except PositionError as error:
        print("lane: BASIS-POSITION")
        print("VERDICT: BLOCKED")
        print("NOTE:", clean(error, 300))
        return 2
    if command in ("open", "add") and args.proposal is None:
        print("%s | %s | REFUSED | --%s requires --proposal <id>"
              % (command.upper(), cfg.symbol, command))
        return EXIT_CODE["REFUSED"]
    if command == "check":
        return cmd_check(cfg, live_request_factory("", ""))
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        print("lane: BASIS-POSITION")
        print("VERDICT: BLOCKED")
        print("NOTE: missing BINANCE_API_KEY / BINANCE_API_SECRET in "
              "environment")
        return 2
    factory = live_request_factory(api_key, api_secret)
    handler = {"rates": cmd_rates, "open": cmd_open, "add": cmd_add,
               "close": cmd_close, "resume": cmd_resume}[command]
    if command in ("open", "add"):
        return handler(cfg, factory, proposal_id=args.proposal)
    return handler(cfg, factory)


if __name__ == "__main__":
    raise SystemExit(main())
