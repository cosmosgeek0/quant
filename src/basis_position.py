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
Discipline is the executor's: one submission per leg, truth only by client
id, UNKNOWN stays UNKNOWN (freeze, name exposure, exit nonzero), no repair
trades, nothing ever resent, and never infer not-filled from a timeout.

Exit codes: 0 decided/OPEN/CLOSED/recorded | 2 refused before entry |
3 blocked / standing exposure | 4 UNKNOWN (frozen) | 5 preflight refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
    PreflightRefusal,
    Refusal,
    RunState,
    attach_fees,
    base_event,
    base_fee_of,
    contract_note,
    first_fail,
    futures_position,
    gap_row,
    get_retry,
    live_request_factory,
    market_context,
    new_run_id,
    parse_trade_capability,
    query_client_order,
    save,
    settle_leg,
    signed_read,
    spot_trades,
    symbol_leverage,
    utc,
    write_events,
)
from futures_hand import PreflightError as FuturesPreflightError
from futures_hand import clean, d, fmt

STATE_FILE = "basis-position.json"
STATE_WRITE_GUARD_FILE = "basis-position.write.lock"
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
    "ReconcileObserved", "FlatProved",
))
V2_RECEIPT_KINDS = frozenset(("REDUCTION", "FLAT"))
V2_TERMINAL_ORDER_STATUSES = frozenset((
    "FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED",
    "EXPIRED_IN_MATCH",
))
V2_TOP_STATES = frozenset((
    "UNPROVEN", "READY", ST_OPEN, "FROZEN",
    "AWAITING_FLAT_PROOF", ST_CLOSED,
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
_V2_EVENT_FIELDS = {
    "ConfigSnapshot": _V2_EVENT_COMMON_FIELDS | _V2_CONFIG_FIELDS,
    "ReceiptObserved": _V2_EVENT_COMMON_FIELDS | _V2_RECEIPT_FIELDS,
    "RungFilled": _V2_EVENT_COMMON_FIELDS | _V2_RUNG_FILLED_FIELDS,
    "RungReduced": _V2_EVENT_COMMON_FIELDS | _V2_RUNG_REDUCED_FIELDS,
    "ReconcileObserved": _V2_EVENT_COMMON_FIELDS | _V2_RECONCILE_FIELDS,
    "FlatProved": _V2_EVENT_COMMON_FIELDS | _V2_FLAT_PROVED_FIELDS,
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


def _normalize_v2_leg(fill: Any, role: str, path: str,
                      require_canonical: bool) -> Dict[str, Any]:
    _v2_exact_fields(fill, _V2_LEG_FIELDS, path)
    expected_stage = ENTER_SPOT if role == "spot" else ENTER_SHORT
    if fill["stage"] != expected_stage:
        raise PositionError(
            "version-2 %s stage does not match its leg" % path)
    market = _v2_id(fill["market"], path + ".market")
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
    rungs: List[Dict[str, Any]] = []
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

    for index, event in enumerate(ledger[1:], start=1):
        event_type = event["event_type"]
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

        if event_type == "RungFilled":
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
            row = schedule[ordinal - 1]
            headroom_before = _v2_headroom(stack_cap, rungs)
            effective_cap = min(
                Decimal(row["quote_cap"]), headroom_before, entry_cap)
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
            rungs.append({
                "ordinal": ordinal,
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
    legal_next = [] if top_state == ST_CLOSED \
        or ledger_freeze_reasons \
        else [{"event_type": "ReceiptObserved"}]
    if legal_next:
        legal_next.append({"event_type": "ReconcileObserved"})
    if top_state in ("UNPROVEN", "AWAITING_FLAT_PROOF") \
            and flat_transition_ready and not reconcile_blocking:
        legal_next.append({"event_type": "FlatProved"})
    if next_ordinal is not None:
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
                "UNPROVEN", "FROZEN", "AWAITING_FLAT_PROOF", ST_CLOSED),
        "reduce_only": top_state == "FROZEN" and bool(active_rungs)
            and not ledger_freeze_reasons,
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
        position: Dict[str, Any], head: Dict[str, Any]) -> None:
    ledger = position["ledger"]
    if head["event_count"] != len(ledger) \
            or head["last_event_digest"] != ledger[-1]["event_digest"]:
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
        _match_v2_committed_head(loaded, head)
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
    guard_path = os.path.join(receipt_dir, STATE_WRITE_GUARD_FILE)
    try:
        descriptor = os.open(
            guard_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError:
        raise PositionError(
            "basis-position write already in progress; "
            "refusing concurrent state update") from None
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
        _atomic_replace_position(
            position_path(receipt_dir), position,
            allow_decimal_default=True)
    finally:
        try:
            os.unlink(guard_path)
        except FileNotFoundError:
            pass


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
                    "version-2 RungReduced lacks a prior event cut")
            # A reduction has no external timestamp.  Its sealed logical cut
            # advances one millisecond and still passes the append-clock gate.
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
    guard_path = os.path.join(receipt_dir, STATE_WRITE_GUARD_FILE)
    try:
        descriptor = os.open(
            guard_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError:
        raise PositionError(
            "basis-position write already in progress; "
            "refusing concurrent ledger append") from None
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
        _atomic_replace_position(
            position_path(receipt_dir), candidate,
            allow_decimal_default=False)
        _atomic_replace_position(
            position_head_path(receipt_dir), {
                "event_count": len(candidate["ledger"]),
                "last_event_digest":
                    candidate["ledger"][-1]["event_digest"],
            }, allow_decimal_default=False)
        return candidate
    finally:
        try:
            os.unlink(guard_path)
        except FileNotFoundError:
            pass


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
               receipted: bool = True) -> RunState:
    run_id = cfg.run_id or new_run_id()
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
                   market: Dict[str, Any]) -> CyclePlan:
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
                      accepted: AcceptedProposal) -> Optional[str]:
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
    minimum_gap_pct = proposal_gap_pct - tolerance_pct
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


def cmd_open(cfg: PositionConfig, request, sleep=time.sleep,
             now_ms: Callable[[], int] = default_now_ms,
             proposal_id: Optional[str] = None) -> int:
    # Both seals are global ENTRY interlocks.  This must remain the first
    # command check: do not inspect a proposal, receipt, state file, or
    # transport first.
    for seal_file in (STOP_FILE, STOP_JOBS_FILE):
        if os.path.exists(os.path.join(cfg.receipt_dir, seal_file)):
            print("OPEN | %s | BLOCKED | %s file present; entry is disabled"
                  % (cfg.symbol, seal_file))
            return EXIT_CODE["BLOCKED"]
    proposal_now_ms = now_ms()
    try:
        accepted = load_entry_proposal(
            cfg.receipt_dir, proposal_id, cfg.symbol, cfg.sha,
            proposal_now_ms)
    except PositionError as error:
        print("OPEN | %s | REFUSED | %s" % (cfg.symbol, clean(error, 300)))
        return EXIT_CODE["REFUSED"]

    # Owner-decision thesis fields are required BEFORE anything happens:
    # the spec forbids inventing max-hold / edge / divergence defaults.
    missing = [name for name, value in (
        ("MAX_HOLD_MS", cfg.max_hold_ms),
        ("MIN_REMAINING_EDGE_PCT", cfg.min_remaining_edge_pct),
        ("MAX_ADVERSE_MOVE_PCT", cfg.max_adverse_move_pct)) if value is None]
    if missing:
        print("OPEN | %s | REFUSED | owner decision required: %s have no "
              "spec default and must be set in the environment"
              % (cfg.symbol, ", ".join(missing)))
        return EXIT_CODE["REFUSED"]
    receipt_blocker = _prior_receipt_blocker(cfg.receipt_dir)
    if receipt_blocker:
        print("OPEN | %s | BLOCKED | %s" % (cfg.symbol, receipt_blocker))
        return EXIT_CODE["BLOCKED"]
    try:
        position = load_position(cfg.receipt_dir)
    except PositionError as error:
        print("OPEN | %s | BLOCKED | %s" % (cfg.symbol, clean(error, 300)))
        return EXIT_CODE["BLOCKED"]
    if position and position.get("state") in STANDING_STATES:
        print("OPEN | %s | BLOCKED | position state %s stands; one-position "
              "machine — run --check/--close/--resume first"
              % (cfg.symbol, position.get("state")))
        return EXIT_CODE["BLOCKED"]

    state = make_state(cfg, request, sleep, "open")
    state.receipt.update({
        "proposal_id": accepted.proposal_id,
        "proposal_digest": accepted.digest,
    })
    save(state)
    if not take_lock(state):
        return conclude(state, "BLOCKED", "none",
                        "lane lock exists; another or unreconciled run owns "
                        "the lane")
    try:
        market = market_context(state)
    except Refusal as refusal:
        release_lock(state)
        return conclude(state, "REJECTED_BEFORE_ENTRY", "none", str(refusal))
    try:
        plan = _proposal_plan(accepted, market)
    except PositionError as error:
        release_lock(state)
        return conclude(state, "REJECTED_BEFORE_ENTRY", "none",
                        str(error))

    # Every account/order/position/balance/margin proof completes while the
    # account is unchanged.  The current leverage only has to be readable
    # here; set-and-verify to exactly 1x is deliberately the final pre-order
    # account action below.
    try:
        _read_only_entry_preflight(state, plan, market)
    except PreflightRefusal as refusal:
        release_lock(state)
        return conclude(state, "PREFLIGHT_REFUSED", "none",
                        str(refusal))

    # Last read-only gate before leverage or an order can mutate the account:
    # the immutable proposal cannot authorize entry on stale economics.
    revalidation_failure = _revalidate_entry(state, accepted)
    if revalidation_failure is not None:
        release_lock(state)
        return conclude(
            state, "REJECTED_BEFORE_ENTRY", "none",
            "%s: %s" % (ENTRY_REVALIDATION_FAILED, revalidation_failure))

    # Persist the accepted authority and opening state before the sole
    # pre-entry account mutation.
    opened_at = now_ms()
    position = {
        "version": 1,
        "symbol": cfg.symbol,
        "sha": cfg.sha,
        "state": ST_OPENING,
        "open_run_id": state.run_id,
        "proposal_id": accepted.proposal_id,
        "proposal_digest": accepted.digest,
        "thesis": {
            "opened_at_ms": None,
            "max_hold_ms": cfg.max_hold_ms,
            "watch_gap_pct": fmt(cfg.watch_gap_pct),
            "min_remaining_edge_pct": fmt(cfg.min_remaining_edge_pct),
            "max_adverse_move_pct": fmt(cfg.max_adverse_move_pct),
        },
        "entry": None,
        "legs": [],
        "client_ids": {},
        "pending_intent": None,
    }
    save_position(cfg.receipt_dir, position)

    # Leverage SET-AND-VERIFY exactly 1x (the one owner-approved account
    # mutation), last after every read-only proof and directly before entry.
    if set_leverage_1x.run(cfg.symbol, request) != 0:
        position["state"] = ST_NONE
        save_position(cfg.receipt_dir, position)
        release_lock(state)
        return conclude(state, "PREFLIGHT_REFUSED", "none",
                        "leverage could not be proven exactly 1x; nothing "
                        "submitted")
    state.receipt["preflight"]["symbol_leverage_after"] = 1
    state.receipt["preflight"]["leverage_verified_utc"] = utc()
    save(state)

    # Leg 1 — spot BUY, quote-bounded; intent persisted before the POST.
    client = persist_intent(cfg.receipt_dir, position, ENTER_SPOT, plan,
                            state.run_id)
    res, order, fills = settle_leg(state, ENTER_SPOT, plan)
    record_leg(cfg.receipt_dir, position, ENTER_SPOT, client, "spot", res,
               order)
    if res == "rejected" or (res == "terminal"
                             and d(order.get("executedQty", "0")) <= 0):
        position["state"] = ST_NONE
        save_position(cfg.receipt_dir, position)
        release_lock(state)
        return conclude(state, "REJECTED_BEFORE_ENTRY", "none",
                        "spot entry rejected or zero-filled; nothing held")
    if res == "unknown":
        position["state"] = ST_UNKNOWN
        save_position(cfg.receipt_dir, position)
        return conclude(state, "UNKNOWN",
                        "spot BUY unconfirmed: spend exchange-bounded at %s "
                        "USDT via quoteOrderQty; query %s before anything"
                        % (fmt(plan.spot_quote_cap), client),
                        "lane frozen; nothing resent")
    executed = d(order.get("executedQty", "0"))
    quote_spent = d(order.get(SPOT.quote_field, "0"))
    trades, covered = spot_trades(
        state, state.receipt["legs"][-1].get("order_id", ""), executed, fills)
    attach_fees(state, len(state.receipt["legs"]) - 1, trades)
    attach_position_fees(cfg.receipt_dir, position, state)
    if covered < executed:
        position["state"] = ST_ONE_LEG
        position["one_leg_since_ms"] = now_ms()
        save_position(cfg.receipt_dir, position)
        return conclude(state, "ONE_LEG_EXPOSED",
                        "holding up to %s %s from the spot BUY; fee truth "
                        "never loaded" % (fmt(executed), market["base"]),
                        "sizing a hedge on an assumed fee is forbidden; "
                        "no repair trade")
    buy_base_fee = base_fee_of(trades, market["base"])
    net_base = executed - buy_base_fee
    hedge = quantize_down(net_base, market["shared_step"])
    entry = {
        "opened_utc": utc(),
        "entry_gap_bps": fmt(plan.entry_gap_bps),
        "entry_gap_pct": fmt(plan.entry_gap_bps / Decimal(100)),
        "funding_rate": fmt(plan.funding_rate),   # per-period FRACTION
        "proposal_net_edge_pct": fmt(accepted.net_edge_pct),
        "spot": {"executed_qty": fmt(executed),
                 "quote_spent": fmt(quote_spent),
                 "base_fee": fmt(buy_base_fee),
                 "net_base": fmt(net_base)},
        "shared_step": fmt(market["shared_step"]),
        "spot_step": fmt(market["spot_step"]),
        "base_asset": market["base"],
        "hedge_qty": fmt(hedge),
    }
    position["entry"] = entry
    save_position(cfg.receipt_dir, position)
    if hedge <= 0 or hedge < market["min_qty"]:
        # Unwind through the ordered path; never hold un-hedgeable spot.
        return _unwind_spot(cfg, state, position, plan, market, net_base,
                            now_ms)

    # Leg 2 — futures SELL short exactly the acquired quantity.
    client = persist_intent(cfg.receipt_dir, position, ENTER_SHORT, plan,
                            state.run_id, hedge)
    res, order, _ = settle_leg(state, ENTER_SHORT, plan, hedge)
    record_leg(cfg.receipt_dir, position, ENTER_SHORT, client, "usdm", res,
               order)
    if res == "unknown":
        position["state"] = ST_UNKNOWN
        save_position(cfg.receipt_dir, position)
        return conclude(state, "UNKNOWN",
                        "holding spot %s %s; short SELL unconfirmed — state "
                        "is either unhedged spot or the full pair; query %s"
                        % (fmt(net_base), market["base"], client),
                        "lane frozen; nothing resent")
    short_filled = d(order.get("executedQty", "0")) if res == "terminal" \
        else Decimal(0)
    if short_filled <= 0:
        return _unwind_spot(cfg, state, position, plan, market, net_base,
                            now_ms)
    short_quote = d(order.get(USDM.quote_field, "0"))
    entry["short"] = {"filled_qty": fmt(short_filled),
                      "cum_quote": fmt(short_quote)}
    position["thesis"]["opened_at_ms"] = opened_at
    position["state"] = ST_OPEN
    if short_filled != hedge:
        entry["hedge_note"] = ("short filled %s of %s; imperfect hedge "
                               "recorded" % (fmt(short_filled), fmt(hedge)))
        first_fail(state, "enter_short", "short filled %s of %s"
                   % (fmt(short_filled), fmt(hedge)))
    save_position(cfg.receipt_dir, position)
    return conclude(state, "OPEN",
                    "hedged pair standing: spot %s %s + short %s"
                    % (fmt(net_base), market["base"], fmt(short_filled)),
                    "entry gap %s bps; funding %s per period; lane lock held "
                    "while the position stands"
                    % (fmt(plan.entry_gap_bps), fmt(plan.funding_rate)))


def _unwind_spot(cfg: PositionConfig, state: RunState,
                 position: Dict[str, Any], plan: CyclePlan,
                 market: Dict[str, Any], net_base: Decimal,
                 now_ms) -> int:
    """Ordered unwind of a spot leg that never got its hedge: sell the
    SPOT-step floor of the acquired net base (the maximal honest sale),
    exactly the reconcile discipline."""
    sellable = quantize_down(net_base, market["spot_step"])
    if sellable <= 0:
        position["state"] = ST_ONE_LEG
        position["one_leg_since_ms"] = now_ms()
        save_position(cfg.receipt_dir, position)
        return conclude(state, "ONE_LEG_EXPOSED",
                        "holding %s %s dust below the spot step; cannot "
                        "hedge or sell it back"
                        % (fmt(net_base), market["base"]),
                        "short leg refused; no repair trade")
    client = persist_intent(cfg.receipt_dir, position, UNWIND_SPOT, plan,
                            state.run_id, sellable)
    res, order, fills = settle_leg(state, UNWIND_SPOT, plan, sellable)
    record_leg(cfg.receipt_dir, position, UNWIND_SPOT, client, "spot", res,
               order)
    if res == "unknown":
        position["state"] = ST_UNKNOWN
        save_position(cfg.receipt_dir, position)
        return conclude(state, "UNKNOWN",
                        "spot unwind unconfirmed: holding either spot %s %s "
                        "or nothing; query %s"
                        % (fmt(net_base), market["base"], client),
                        "lane frozen; nothing resent")
    sold = d(order.get("executedQty", "0")) if res == "terminal" \
        else Decimal(0)
    if sold <= 0:
        position["state"] = ST_ONE_LEG
        position["one_leg_since_ms"] = now_ms()
        save_position(cfg.receipt_dir, position)
        return conclude(state, "ONE_LEG_EXPOSED",
                        "holding spot %s %s, no hedge; unwind rejected"
                        % (fmt(net_base), market["base"]),
                        "exposure stands; report to owner; no repair trade")
    trades, _covered = spot_trades(
        state, state.receipt["legs"][-1].get("order_id", ""), sold, fills)
    attach_fees(state, len(state.receipt["legs"]) - 1, trades)
    attach_position_fees(cfg.receipt_dir, position, state)
    residual = net_base - sold - base_fee_of(trades, market["base"])
    if residual >= market["spot_step"]:
        position["state"] = ST_ONE_LEG
        position["one_leg_since_ms"] = now_ms()
        save_position(cfg.receipt_dir, position)
        return conclude(state, "ONE_LEG_EXPOSED",
                        "unwind sold %s; residual %s %s at or above the "
                        "spot step is sellable"
                        % (fmt(sold), fmt(residual), market["base"]),
                        "no repair trade; reconcile before any new entry")
    position["state"] = ST_NONE
    position["unwound"] = {"sold_qty": fmt(sold),
                           "residual_base": fmt(residual)}
    save_position(cfg.receipt_dir, position)
    release_lock(state)
    return conclude(state, "REJECTED_BEFORE_ENTRY", "none",
                    "short refused; spot unwound through the ordered path; "
                    "residual %s below the spot step" % fmt(residual))


# --------------------------------------------------------------------------
# --check: one hold-loop tick — public reads, recomputed entry gap, the
# spec's exit triggers, a factual decision. Never places an order.
# --------------------------------------------------------------------------

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
            ("--check", "one hold tick: public reads + exit triggers"),
            ("--close", "reduce-only close + spot sale + P&L"),
            ("--resume", "restart recovery: query-first by client id")):
        mode.add_argument(flag, metavar="SYMBOL", nargs="?", const="",
                          default=None, help=text)
    parser.add_argument("--proposal", metavar="ID",
                        help="immutable proposal id required by --open")
    args = parser.parse_args(argv)
    command, symbol = next(
        (name, getattr(args, name))
        for name in ("rates", "open", "check", "close", "resume")
        if getattr(args, name) is not None)
    if args.proposal is not None and command != "open":
        parser.error("--proposal is valid only with --open")
    if command == "open":
        receipt_dir = os.path.expanduser(
            os.environ.get("RECEIPT_DIR", "~/quant_receipts"))
        if os.path.exists(os.path.join(receipt_dir, STOP_FILE)):
            display_symbol = (symbol or os.environ.get("SYMBOL", "")).upper()
            print("OPEN | %s | BLOCKED | STOP file present; entry is disabled"
                  % display_symbol)
            return EXIT_CODE["BLOCKED"]
    try:
        cfg = config_from_env(os.environ, symbol)
    except PositionError as error:
        print("lane: BASIS-POSITION")
        print("VERDICT: BLOCKED")
        print("NOTE:", clean(error, 300))
        return 2
    if command == "open" and args.proposal is None:
        print("OPEN | %s | REFUSED | --open requires --proposal <id>"
              % cfg.symbol)
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
    handler = {"rates": cmd_rates, "open": cmd_open, "close": cmd_close,
               "resume": cmd_resume}[command]
    if command == "open":
        return handler(cfg, factory, proposal_id=args.proposal)
    return handler(cfg, factory)


if __name__ == "__main__":
    raise SystemExit(main())
