"""basis_position.py — persistent, resumable one-position basis lifecycle.

Ports the spec's lifecycle area (tests/fixtures/old_engine_spec.json) as one
restart-safe position machine on top of the PROVEN executor mechanics. This
module composes; it does not duplicate: transport, order settlement, signed
reads, market filters, and the receipts/events discipline all come from
basis_executor (settle_leg, signed_read, market_context, query_client_order,
signed_preflight, spot_trades, save), and the one leverage mutation comes
from set_leverage_1x.run (set-and-verify exactly 1x).

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
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Tuple

import events
import set_leverage_1x
from basis_cycle import (
    ENTER_SHORT,
    ENTER_SPOT,
    EXIT_SHORT,
    EXIT_SPOT,
    UNWIND_SPOT,
    CyclePlan,
    leg_intent,
    plan_cycle,
    quantize_down,
)
from basis_executor import (
    SPOT,
    SPOT_ACCOUNT_PATH,
    USDM,
    USDM_POSITION_PATH,
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
    query_client_order,
    save,
    settle_leg,
    signed_preflight,
    signed_read,
    spot_trades,
    utc,
    write_events,
)
from futures_hand import clean, d, fmt

STATE_FILE = "basis-position.json"
RATES_FILE = "commission-rates.json"
EMERGENCY_FILE = "EMERGENCY"
LOCK_FILE = "basis-executor.lock"          # shared lane lock with the executor
SPOT_COMMISSION_PATH = "/api/v3/account/commission"
USDM_COMMISSION_PATH = "/fapi/v1/commissionRate"
USDM_INCOME_PATH = "/fapi/v1/income"
SYMBOL_RE = re.compile(r"[A-Z0-9]{5,20}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
MS_SAFE_MAX = 2 ** 53 - 1

# Spec defaults that exist (everything without a spec default is an owner
# decision required in the environment — never invented here).
WATCH_GAP_PCT_DEFAULT = Decimal("0.15")    # scanner entry_thresholds KEEP
SPOT_TAKER_FEE_PCT_DEFAULT = Decimal("0.1")   # math four_leg_fees KEEP, per leg
USDM_TAKER_FEE_PCT_DEFAULT = Decimal("0.05")  # math four_leg_fees KEEP, per leg

# Persisted machine states (plan-layer collapse).
ST_OPENING = "OPENING"
ST_OPEN = "OPEN"
ST_ONE_LEG = "ONE_LEG_EXPOSED"
ST_CLOSING = "CLOSING"
ST_CLOSED = "CLOSED"
ST_UNKNOWN = "UNKNOWN"
ST_NONE = "NONE"
STANDING_STATES = (ST_OPENING, ST_OPEN, ST_ONE_LEG, ST_CLOSING, ST_UNKNOWN)

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
# Persistent state file — atomic, verdict-carrying, intent-before-mutation.
# --------------------------------------------------------------------------

def position_path(receipt_dir: str) -> str:
    return os.path.join(receipt_dir, STATE_FILE)


def load_position(receipt_dir: str) -> Optional[Dict[str, Any]]:
    try:
        with open(position_path(receipt_dir), encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        raise PositionError("basis-position.json exists but is unreadable; "
                            "human check required before any trade")
    if not isinstance(loaded, dict):
        raise PositionError("basis-position.json is not an object")
    return loaded


def save_position(receipt_dir: str, position: Dict[str, Any]) -> None:
    position["verdict"] = STATE_VERDICT[position["state"]]
    position["updated_utc"] = utc()
    path = position_path(receipt_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(position, handle, indent=2, default=str)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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


def cmd_open(cfg: PositionConfig, request, sleep=time.sleep,
             now_ms: Callable[[], int] = default_now_ms) -> int:
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
    if not take_lock(state):
        return conclude(state, "BLOCKED", "none",
                        "lane lock exists; another or unreconciled run owns "
                        "the lane")
    try:
        market = market_context(state)
        row = gap_row(state)
    except Refusal as refusal:
        release_lock(state)
        return conclude(state, "REJECTED_BEFORE_ENTRY", "none", str(refusal))
    from basis_cycle import plan_cycle
    plan = plan_cycle(row, cfg.min_entry_bps, market["shared_step"],
                      market["min_notional"])
    if plan is None or plan.spot_quantity < market["min_qty"]:
        release_lock(state)
        return conclude(state, "REJECTED_BEFORE_ENTRY", "none",
                        "plan refused: entry gap %s bps below min %s bps or "
                        "size below venue minimums"
                        % (fmt(row.entry_gap_bps_vs_spot_ask),
                           fmt(cfg.min_entry_bps)))

    # Leverage SET-AND-VERIFY exactly 1x (the one owner-approved account
    # mutation, delegated whole to set_leverage_1x.run — no duplication).
    if set_leverage_1x.run(cfg.symbol, request) != 0:
        release_lock(state)
        return conclude(state, "PREFLIGHT_REFUSED", "none",
                        "leverage could not be proven exactly 1x; nothing "
                        "submitted")
    try:
        signed_preflight(state, plan, market)
        spot_account = signed_read(state, SPOT.base_url, SPOT_ACCOUNT_PATH,
                                   None, "Spot balance read")
        free_quote = _spot_free(spot_account, "USDT")
        if free_quote is None or free_quote < plan.spot_quote_cap:
            raise PreflightRefusal(
                "spot USDT free balance %s does not cover the quote cap %s"
                % ("UNKNOWN" if free_quote is None else fmt(free_quote),
                   fmt(plan.spot_quote_cap)))
    except PreflightRefusal as refusal:
        release_lock(state)
        return conclude(state, "PREFLIGHT_REFUSED", "none", str(refusal))

    # The position record exists BEFORE the first mutation.
    opened_at = now_ms()
    position = {
        "version": 1,
        "symbol": cfg.symbol,
        "sha": cfg.sha,
        "state": ST_OPENING,
        "open_run_id": state.run_id,
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
        "entry_gap_bps": fmt(row.entry_gap_bps_vs_spot_ask),
        "entry_gap_pct": fmt(row.entry_gap_bps_vs_spot_ask / Decimal(100)),
        "funding_rate": fmt(row.funding_rate),   # per-period FRACTION
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
                    % (fmt(row.entry_gap_bps_vs_spot_ask),
                       fmt(row.funding_rate)))


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
    save_position(cfg.receipt_dir, position)
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
    entry = position["entry"]
    short_qty = d(entry["short"]["filled_qty"])
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
    entry_short_quote = d(entry["short"]["cum_quote"])
    spot_pnl = quote_received - entry_quote_spent
    short_pnl = entry_short_quote - exit_short_quote
    spot_fees_usdt = _spot_usdt_fees(position)
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
        "short_quote_pnl_usdt": fmt(short_pnl),
        "spot_fees_usdt": fmt(spot_fees_usdt),
        "usdm_commission_usdt": fmt(usdm_commission)
        if isinstance(usdm_commission, Decimal) else "UNKNOWN",
        "funding_usdt": fmt(funding_total)
        if isinstance(funding_total, Decimal) else "UNKNOWN",
        "residual_base": fmt(residual),
    }
    if isinstance(funding_total, Decimal) \
            and isinstance(usdm_commission, Decimal):
        pnl["total_usdt"] = fmt(spot_pnl + short_pnl - spot_fees_usdt
                                + funding_total + usdm_commission)
    else:
        pnl["total_usdt"] = "UNKNOWN"    # funding never assumed zero
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

def derive_resume(position: Dict[str, Any],
                  truths: Dict[str, Tuple[str, Any]],
                  short_amount: Optional[Decimal],
                  base_free: Optional[Decimal],
                  spot_step: Decimal) -> Dict[str, Any]:
    """Pure judgment over reconciled truth. Returns {state, verdict, note,
    exposure, recommendations}. Never proposes an automatic action."""
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
    if entered_spot and not sold_spot and base_free >= spot_step:
        return {
            "state": ST_ONE_LEG, "verdict": "ONE_LEG_EXPOSED",
            "note": "spot leg held with no hedge standing",
            "exposure": "spot base free %s at or above the spot step %s"
                        % (fmt(base_free), fmt(spot_step)),
            "recommendations": [{"action": "CLOSE_EXPOSED_LEG",
                                 "automatic": False}],
        }
    if entered_spot and (sold_spot or base_free < spot_step):
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
    args = parser.parse_args(argv)
    command, symbol = next(
        (name, value) for name, value in vars(args).items()
        if value is not None)
    try:
        cfg = config_from_env(os.environ, symbol)
    except PositionError as error:
        print("lane: BASIS-POSITION")
        print("VERDICT: BLOCKED")
        print("NOTE:", clean(error, 300))
        return 2
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
    return handler(cfg, factory)


if __name__ == "__main__":
    raise SystemExit(main())
