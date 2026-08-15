"""Deterministic offline tests for src/basis_position.py — injected transport
only; a module-level socket guard makes any real network attempt fail fast.

Differential-harness duty: this file reproduces the LIFECYCLE-area numeric
fixtures of tests/fixtures/old_engine_spec.json (supervision ladder, naked
duration reducer, handoff settlement record) and documents, one written
reason each, the five signal-layer fixtures this position machine cannot
reproduce because they live in the candidate/scanner layer outside B3.
Unit conventions (percent vs bps vs fraction vs ratio) are tested
explicitly per the spec's math-area unit hazard note.
"""

import dataclasses
import hashlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_position as bp  # noqa: E402
import events  # noqa: E402
from basis_position import PositionConfig  # noqa: E402

SPEC_PATH = Path(__file__).resolve().parent / "fixtures" / \
    "old_engine_spec.json"
with open(SPEC_PATH, encoding="utf-8") as _handle:
    SPEC = json.load(_handle)
LIFECYCLE_FIXTURES = {f["name"] for f in SPEC["lifecycle"]["fixtures"]}

SYMBOL = "BTCUSDT"
SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
RID = "cafe01"
PROPOSAL_ID = "abc123"
NOW = 1800000000000            # supervision fixtures' NOW
T_OPEN = 1755000000000
SPOT_URL = bp.SPOT.base_url
USDM_URL = bp.USDM.base_url

_real_getaddrinfo = socket.getaddrinfo


def setUpModule():
    def deny(*args, **kwargs):
        raise AssertionError("network access attempted in offline tests")
    socket.getaddrinfo = deny


def tearDownModule():
    socket.getaddrinfo = _real_getaddrinfo


# ==========================================================================
# Supervision ladder — spec lifecycle fixtures supervision_* (10 fixtures).
# Baseline inputs are the fixture's named values; the two completions the
# fixture leaves unnamed (a passing available depth, thesis horizons) are
# filled with passing values and marked here: depth 1000 >= min 150, age
# 1000ms << expected horizon 3600000ms.
# ==========================================================================

def common_policy():
    return {"max_one_leg_exposure_usd": "50",
            "maximum_one_leg_duration_ms": 1000,
            "emergency_one_leg_duration_ms": 2000,
            "max_gross_exposure_usd": "1000",
            "reduction_target_exposure_usd": "100",
            "min_market_depth_usd": "150"}


def basis_policy():
    return {"minimum_remaining_edge": "1",
            "minimum_remaining_edge_for_add": "2",
            "maximum_adverse_basis_move": "0.02",
            "maximum_adverse_funding_rate": "0.01",
            "funding_exit_lead_time_ms": 60000,
            "maximum_borrow_rate": "0.02",
            "maximum_hedge_drift": "0.05",
            "minimum_liquidation_distance": "2",
            "minimum_margin_distance": "2",
            "maximum_liquidity_deterioration": "0.5",
            "required_converging_observations_for_add": 3}


def baseline_position():
    return {"lifecycle": "HEDGED_OPEN", "reconciliation": "EXACT",
            "posture": "NORMAL",
            "legs": [
                {"side": "LONG", "venue": "spot", "quote_exposure": "100"},
                {"side": "SHORT", "venue": "perp", "quote_exposure": "100"},
            ]}


def baseline_facts():
    return {"required_value_present": True, "max_age_ms": 1000,
            "source_time_ms": NOW - 100,
            "entry_executable_basis": "0.04",
            "current_executable_basis": "0.01",
            "basis_path": ["0.03", "0.02", "0.01"],
            "expected_remaining_edge_after_exit_costs": "10",
            "funding": {"direction": "PAID", "rate": "0.0001",
                        "next_funding_at_ms": NOW + 30000},
            "borrow": {"availability": "AVAILABLE", "rate": "0.001"},
            "hedge_drift": "0", "liquidation_distance": "10",
            "margin_distance": "10", "liquidity_deterioration": "0.1",
            "available_depth_usd": "1000",
            "thesis": {"opened_at_ms": NOW - 1000,
                       "expected_horizon_ms": 3600000,
                       "maximum_holding_horizon_ms": 7200000,
                       "target_reached": False, "invalidated": False},
            "stage_authority": {"valid": True, "consuming_usd": "100"}}


def one_leg_position(since_ms):
    position = baseline_position()
    position["legs"] = [{"side": "LONG", "venue": "spot",
                         "quote_exposure": "100"}]
    position["lifecycle"] = "ONE_LEG_EXPOSED"
    position["one_leg_since_ms"] = since_ms
    return position


class SupervisionFixtureTests(unittest.TestCase):
    """spec.lifecycle fixtures supervision_baseline_add .. posture_hold."""

    def decide(self, position=None, facts=None):
        return bp.supervise(position or baseline_position(), common_policy(),
                            basis_policy(), facts or baseline_facts(), NOW)

    def test_supervision_baseline_add(self):
        decision = self.decide()
        self.assertEqual(decision["kind"], "ADD_STAGE")
        again = self.decide()
        self.assertEqual(json.dumps(decision, sort_keys=True),
                         json.dumps(again, sort_keys=True))

    def test_supervision_one_leg_hedge(self):
        decision = self.decide(position=one_leg_position(NOW - 500))
        self.assertEqual(decision["kind"], "REQUEST_HEDGE")
        self.assertEqual(decision["target"],
                         {"kind": "DELTA", "quote_notional": "-100"})
        self.assertEqual(decision["risk_effect"], "RISK_REDUCING")
        self.assertEqual(decision["hard_deadline_ms"], NOW - 500 + 1000)

    def test_supervision_one_leg_overdue_emergency(self):
        decision = self.decide(position=one_leg_position(NOW - 2500))
        self.assertEqual(decision["kind"], "REQUEST_EMERGENCY_UNWIND")
        self.assertEqual(decision["why"][0], "ONE_LEG_DURATION_EMERGENCY")
        self.assertEqual(decision["target"]["quote_notional"], "0")
        self.assertEqual(decision["hard_deadline_ms"], NOW)

    def test_supervision_one_leg_reconciliation_uncertain(self):
        position = one_leg_position(NOW - 500)
        position["reconciliation"] = "DIVERGED"
        decision = self.decide(position=position)
        self.assertEqual(decision["kind"], "REQUEST_EMERGENCY_UNWIND")
        self.assertIn("ONE_LEG_RECONCILIATION_UNCERTAIN", decision["why"])
        self.assertEqual(decision["risk_effect"], "RISK_REDUCING")

    def test_supervision_edge_and_divergence_exits(self):
        edge_facts = baseline_facts()
        edge_facts["expected_remaining_edge_after_exit_costs"] = "0"
        edge = self.decide(facts=edge_facts)
        divergence_facts = baseline_facts()
        divergence_facts["current_executable_basis"] = "0.1"
        divergence = self.decide(facts=divergence_facts)
        self.assertEqual(edge["kind"], "REQUEST_EXIT")
        self.assertEqual(divergence["kind"], "REQUEST_EXIT")
        self.assertEqual(edge["why"][0], "REMAINING_EDGE_EXHAUSTED")
        self.assertEqual(divergence["why"][0],
                         "BASIS_DIVERGENCE_INVALIDATION")
        self.assertNotEqual(edge["why"][0], divergence["why"][0])
        self.assertNotEqual(edge["idempotency_key"],
                            divergence["idempotency_key"])
        self.assertNotEqual(edge["decision_key"], divergence["decision_key"])

    def test_supervision_target_and_depth(self):
        target_facts = baseline_facts()
        target_facts["thesis"]["target_reached"] = True
        target = self.decide(facts=target_facts)
        self.assertEqual(target["kind"], "REQUEST_EXIT")
        self.assertEqual(target["why"][0], "THESIS_TARGET_REACHED")
        self.assertEqual(target["urgency"], "ROUTINE")
        depth_facts = baseline_facts()
        depth_facts["available_depth_usd"] = "10"
        depth = self.decide(facts=depth_facts)
        self.assertEqual(depth["kind"], "REDUCE")
        self.assertEqual(depth["target"]["quote_notional"], "100")

    def test_supervision_survival_emergencies(self):
        recall_facts = baseline_facts()
        recall_facts["borrow"] = {"availability": "RECALLED", "rate": "0.001"}
        recall = self.decide(facts=recall_facts)
        margin_facts = baseline_facts()
        margin_facts["margin_distance"] = "0.5"
        margin = self.decide(facts=margin_facts)
        for decision in (recall, margin):
            self.assertEqual(decision["kind"], "REQUEST_EMERGENCY_UNWIND")
            self.assertEqual(decision["why"][0],
                             "BASIS_CAPITAL_SURVIVAL_LIMIT")

    def test_supervision_carry_liquidity_reduce(self):
        liquidity_facts = baseline_facts()
        liquidity_facts["liquidity_deterioration"] = "0.8"
        liquidity = self.decide(facts=liquidity_facts)
        funding_facts = baseline_facts()
        funding_facts["funding"] = {"direction": "PAID", "rate": "0.02",
                                    "next_funding_at_ms": NOW + 30000}
        funding = self.decide(facts=funding_facts)
        for decision in (liquidity, funding):
            self.assertEqual(decision["kind"], "REDUCE")
            self.assertEqual(decision["urgency"], "ELEVATED")
        self.assertEqual(liquidity["target"]["quote_notional"], "100")

    def test_supervision_unknown_freeze(self):
        missing_facts = baseline_facts()
        missing_facts["required_value_present"] = False
        missing = self.decide(facts=missing_facts)
        stale_facts = baseline_facts()
        stale_facts["source_time_ms"] = NOW - 10000
        stale = self.decide(facts=stale_facts)
        for decision in (missing, stale):
            self.assertEqual(decision["kind"], "FREEZE_NEW_ACTIONS")
            self.assertNotIn(decision["why"][0],       # no exit fabricated
                             ("REQUEST_EXIT", "REQUEST_EMERGENCY_UNWIND"))
        self.assertEqual(missing["completeness"], "UNKNOWN")
        self.assertEqual(stale["completeness"], "STALE")
        self.assertIn("BASIS_FACT_FRESHNESS", stale["blocking_facts"])
        self.assertIn("BASIS_FACT_FRESHNESS", missing["blocking_facts"])

    def test_supervision_posture_hold(self):
        for posture in ("PAUSED_NEW_EXPOSURE", "KILL_NEW_EXPOSURE"):
            position = baseline_position()
            position["posture"] = posture
            decision = self.decide(position=position)
            self.assertEqual(decision["kind"], "HOLD")
            self.assertIsNone(decision["stage_transition_authorization"])
        # Risk reduction survives kill posture.
        position = baseline_position()
        position["posture"] = "KILL_NEW_EXPOSURE"
        facts = baseline_facts()
        facts["thesis"]["invalidated"] = True
        decision = self.decide(position=position, facts=facts)
        self.assertEqual(decision["kind"], "REQUEST_EXIT")
        self.assertEqual(decision["why"][0], "THESIS_INVALIDATED")

    def test_position_closed_holds(self):
        # Ladder row 1 (rule supervision_directive_ladder, no numeric
        # fixture): a CLOSED position asks for nothing.
        position = baseline_position()
        position["lifecycle"] = "CLOSED"
        decision = self.decide(position=position)
        self.assertEqual(decision["kind"], "HOLD")
        self.assertEqual(decision["why"][0], "POSITION_CLOSED")

    def test_max_hold_exit_row(self):
        # Ladder row 7 max-hold: age >= maximumHoldingHorizonMs -> URGENT
        # REQUEST_EXIT(MAXIMUM_HOLDING_HORIZON) with deadline now.
        facts = baseline_facts()
        facts["thesis"]["opened_at_ms"] = NOW - 7200000
        decision = self.decide(facts=facts)
        self.assertEqual(decision["kind"], "REQUEST_EXIT")
        self.assertEqual(decision["why"][0], "MAXIMUM_HOLDING_HORIZON")
        self.assertEqual(decision["urgency"], "URGENT")
        self.assertEqual(decision["hard_deadline_ms"], NOW)


class NakedDurationTests(unittest.TestCase):
    """spec.lifecycle fixture reducer_naked_duration_expiry."""

    def test_early_event_rejected(self):
        outcome, _why = bp.apply_naked_duration_expired(1000, 500, 1499)
        self.assertEqual(outcome, "INVALID_EVENT")

    def test_due_event_forces_recovery_never_expired(self):
        outcome, why = bp.apply_naked_duration_expired(1000, 500, 1500)
        self.assertEqual(outcome, "RECOVERY_REQUIRED")
        self.assertNotEqual(outcome, "EXPIRED")
        self.assertIn("EXPIRED is unreachable", why)

    def test_missing_plan_field_or_latch_rejected(self):
        self.assertEqual(bp.apply_naked_duration_expired(None, 500, 9999)[0],
                         "INVALID_EVENT")
        self.assertEqual(bp.apply_naked_duration_expired(1000, None, 9999)[0],
                         "INVALID_EVENT")
        self.assertEqual(bp.apply_naked_duration_expired(1000, 0, 9999)[0],
                         "INVALID_EVENT")


# ==========================================================================
# Handoff settlement record — spec.lifecycle fixtures handoff_* (6).
# ==========================================================================

BOUND = 1755000100000


class SeqClock:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        value = self.values.pop(0) if len(self.values) > 1 \
            else self.values[0]
        if isinstance(value, Exception):
            raise value
        return value


class ThrowingStore:
    def claim(self, proposal_id):
        raise AssertionError("store touched through a disabled lane")


class Store:
    def __init__(self):
        self.claims = []

    def claim(self, proposal_id):
        self.claims.append(proposal_id)
        return {"proposal_id": proposal_id, "symbol": "TESTUSDT",
                "direction": "long_spot_short_perp",
                "accepted_notional_usd": "10",
                "accepted_revision_ts": 1755000090000,
                "accepted_at_ms": 1755000090000,
                "correlation_id": "corr-1"}


def feedback(kind, sequence, observed, quantity=None, **overrides):
    message = {"kind": kind, "sequence": sequence,
               "observed_at_ms": observed,
               "accepted_revision_ts": 1755000090000,
               "handoff_id": "bh_bp_1", "proposal_id": "bp_1",
               "symbol": "TESTUSDT"}
    if quantity is not None:
        message["cumulative_filled_quantity"] = quantity
    message.update(overrides)
    return message


def bound_lane(clock_values=(BOUND,)):
    lane = bp.HandoffLane(store=Store(), clock=SeqClock(clock_values),
                          enabled=True)
    result = lane.consume_accepted("bp_1")
    assert result["ok"] is True
    return lane


class HandoffTests(unittest.TestCase):
    def snapshot(self, lane):
        return json.dumps(lane.snapshot(), sort_keys=True)

    def test_handoff_disabled_refuses_inert(self):
        # spec fixture handoff_disabled_refuses_inert: throwing deps prove
        # nothing is touched; the store never burns an acceptance.
        lane = bp.HandoffLane(store=ThrowingStore(),
                              clock=SeqClock([AssertionError("clock")]),
                              enabled=False)
        consume = lane.consume_accepted("bp_whatever")
        apply_result = lane.apply_feedback(
            feedback("TERMINAL", 1, BOUND + 1, "0.5"))
        self.assertEqual(consume, {"ok": False, "refusal": "DISABLED"})
        self.assertEqual(apply_result, {"ok": False, "refusal": "DISABLED"})
        self.assertEqual(lane.snapshot(), {"enabled": False, "handoff": None})

    def test_handoff_terminal_records_verbatim(self):
        # Clock throws at terminal: record unchanged, still awaiting.
        lane = bound_lane([BOUND, RuntimeError("clock failed"), BOUND])
        lane.apply_feedback(feedback("WORKING", 1, BOUND + 1000))
        before = self.snapshot(lane)
        with self.assertRaises(RuntimeError):
            lane.apply_feedback(feedback("TERMINAL", 2, BOUND + 2000,
                                         "0.0075"))
        self.assertEqual(self.snapshot(lane), before)
        # Clock regresses: raises 'must not regress', record unchanged.
        lane = bound_lane([BOUND, BOUND - 1, BOUND])
        lane.apply_feedback(feedback("WORKING", 1, BOUND + 1000))
        before = self.snapshot(lane)
        with self.assertRaises(ValueError) as caught:
            lane.apply_feedback(feedback("TERMINAL", 2, BOUND + 2000,
                                         "0.0075"))
        self.assertIn("must not regress", str(caught.exception))
        self.assertEqual(self.snapshot(lane), before)
        # Success: verbatim string quantity, terminalTs = boundTs clock.
        lane = bound_lane([BOUND])
        lane.apply_feedback(feedback("WORKING", 1, BOUND + 1000))
        result = lane.apply_feedback(feedback("TERMINAL", 2, BOUND + 2000,
                                              "0.0075"))
        self.assertEqual(result["progressed"], True)
        self.assertEqual(result["state"], "TERMINAL")
        self.assertEqual(result["terminal_kind"], "TERMINAL_REPORTED")
        self.assertIsInstance(result["cumulative_filled_quantity"], str)
        self.assertEqual(result["cumulative_filled_quantity"], "0.0075")
        self.assertEqual(result["last_sequence"], 2)
        self.assertEqual(result["terminal_ts"], BOUND)

    def test_handoff_cancel_confirmed_zero_and_partial(self):
        zero_lane = bound_lane()
        zero = zero_lane.apply_feedback(
            feedback("CANCEL_CONFIRMED", 4, BOUND + 100, "0"))
        partial_lane = bound_lane()          # fresh binding
        partial = partial_lane.apply_feedback(
            feedback("CANCEL_CONFIRMED", 9, BOUND + 100, "0.004"))
        for result, quantity in ((zero, "0"), (partial, "0.004")):
            self.assertTrue(result["progressed"])
            self.assertEqual(result["state"], "TERMINAL")
            self.assertEqual(result["terminal_kind"], "CANCEL_CONFIRMED")
            self.assertEqual(result["cumulative_filled_quantity"], quantity)
        record = zero_lane.snapshot()["handoff"]
        self.assertEqual(sorted(record), sorted(bp.HANDOFF_FIELDS))
        self.assertEqual(len(record), 18)
        for forbidden in ("flat", "residual", "unwind", "recovery"):
            self.assertFalse(any(forbidden in key for key in record),
                             "no flatness/residual/unwind/recovery fields")

    def test_handoff_quantity_canonicality(self):
        rejected_forms = ["0.0045000", "00.5", " 0.5", "0.5 ", "+0.5",
                          "1e-3", "-0.5", "", "abc", 0.5, None]
        for form in rejected_forms:
            lane = bound_lane()
            before = self.snapshot(lane)
            result = lane.apply_feedback(
                feedback("TERMINAL", 2, BOUND + 100, form))
            self.assertEqual(result, {"ok": False,
                                      "refusal": "QUANTITY_NOT_EXACT"},
                             "form %r must refuse" % (form,))
            self.assertEqual(self.snapshot(lane), before)
        # Missing quantity ("undefined") refuses the same way.
        lane = bound_lane()
        result = lane.apply_feedback(feedback("TERMINAL", 2, BOUND + 100))
        self.assertEqual(result["refusal"], "QUANTITY_NOT_EXACT")
        # '250' accepted while acceptedNotionalUsd=10: base quantity and USD
        # notional are different units, never compared.
        lane = bound_lane()
        result = lane.apply_feedback(feedback("TERMINAL", 2, BOUND + 100,
                                              "250"))
        self.assertTrue(result["progressed"])
        self.assertEqual(result["cumulative_filled_quantity"], "250")

    def test_handoff_ordering_refusals(self):
        cases = [
            (feedback("WORKING", 5, BOUND + 6000), "DUPLICATE_SEQUENCE"),
            (feedback("WORKING", 4, BOUND + 6000), "STALE_SEQUENCE"),
            (feedback("WORKING", 6, BOUND + 4999), "STALE_OBSERVATION"),
            (feedback("WORKING", 6, BOUND - 1), "STALE_OBSERVATION"),
            (feedback("WORKING", 1.5, BOUND + 6000), "FEEDBACK_MALFORMED"),
            (feedback("WORKING", -1, BOUND + 6000), "FEEDBACK_MALFORMED"),
            (feedback("WORKING", 2 ** 53, BOUND + 6000),
             "FEEDBACK_MALFORMED"),
            (feedback("WORKING", 6, 1.5), "FEEDBACK_MALFORMED"),
            (feedback("WORKING", 6, BOUND + 6000,
                      accepted_revision_ts=float("nan")),
             "FEEDBACK_MALFORMED"),
        ]
        for message, refusal in cases:
            lane = bound_lane()
            lane.apply_feedback(feedback("WORKING", 5, BOUND + 5000))
            before = self.snapshot(lane)
            result = lane.apply_feedback(message)
            self.assertEqual(result, {"ok": False, "refusal": refusal},
                             "case %r" % (message,))
            self.assertEqual(self.snapshot(lane), before,
                             "record must be byte-identical after refusal")

    def test_handoff_terminal_immutable(self):
        lane = bound_lane()
        result = lane.apply_feedback(feedback("TERMINAL", 2, BOUND + 100,
                                              "5"))
        self.assertTrue(result["progressed"])
        attempts = [feedback("TERMINAL", 3, BOUND + 200, "9"),
                    feedback("CANCEL_CONFIRMED", 3, BOUND + 200, "1"),
                    feedback("WORKING", 3, BOUND + 200),
                    feedback("AMBIGUOUS", 3, BOUND + 200),
                    feedback("CANCEL_REQUESTED", 3, BOUND + 200)]
        for attempt in attempts:
            outcome = lane.apply_feedback(attempt)
            self.assertEqual(outcome, {"ok": False,
                                       "refusal": "ALREADY_TERMINAL"})
        record = lane.snapshot()["handoff"]
        self.assertEqual(record["cumulative_filled_quantity"], "5")

    def test_wrong_identity_and_revision_refusals(self):
        # Rule handoff_machine refusal order rows 4-5 (identity before
        # revision), exercised beside the fixture table.
        lane = bound_lane()
        wrong = lane.apply_feedback(
            feedback("WORKING", 1, BOUND + 1, handoff_id="bh_other"))
        self.assertEqual(wrong["refusal"], "WRONG_IDENTITY")
        mismatch = lane.apply_feedback(
            feedback("WORKING", 1, BOUND + 1, accepted_revision_ts=1))
        self.assertEqual(mismatch["refusal"], "REVISION_MISMATCH")


# ==========================================================================
# Signal-layer fixtures — WAIVED with reasons (never silently deleted).
# ==========================================================================

class SignalLayerFixtureWaivers(unittest.TestCase):
    """The five remaining lifecycle-area fixtures exercise the signal
    (candidate) machine — IDLE/WATCHING/FOCUS/PROPOSED/COOLDOWN, proposal
    sweep, universe pruning. B3's scope is the position machine (plan +
    supervision + handoff layers); the candidate machine is the scanner
    builder's file, so basis_position deliberately exposes no signal-layer
    API. Each waiver asserts the fixture still exists in the spec (not
    silently deleted) and that this module stays out of that layer."""

    def waive(self, name):
        self.assertIn(name, LIFECYCLE_FIXTURES)
        for signal_api in ("tick", "apply_decision", "set_universe"):
            self.assertFalse(hasattr(bp, signal_api))

    def test_waived_persistence_gate_then_proposal(self):
        # WAIVED: replay-harness persistence gate + proposal formation =
        # signal/scanner layer, outside the B3 position machine.
        self.waive("persistence_gate_then_proposal")

    def test_waived_stale_data_cooldown_cycle(self):
        # WAIVED: cooldown bookkeeping lives in the signal machine.
        self.waive("stale_data_cooldown_cycle")

    def test_waived_focus_caps_eviction(self):
        # WAIVED: focus capacity/eviction is signal-attention machinery.
        self.waive("focus_caps_eviction")

    def test_waived_historical_risk_blocked_focus_exit(self):
        # WAIVED: reject-vocabulary projection acts on focus sessions in
        # the signal machine.
        self.waive("historical_risk_blocked_focus_exit")

    def test_waived_universe_removal_prunes_state(self):
        # WAIVED: universe retention prunes signal-layer candidate state.
        self.waive("universe_removal_prunes_state")


# ==========================================================================
# Unit conventions — spec math area unit hazard, tested explicitly.
# ==========================================================================

class UnitConventionTests(unittest.TestCase):
    def test_percent_bps_fraction_ratio_regimes(self):
        # Gaps arrive in BPS from basis_read; the position stores PERCENT:
        # 20 bps -> 0.2 percent.
        self.assertEqual(Decimal("20") / Decimal(100), Decimal("0.2"))
        # watchGapPct spec default is PERCENT (0.15 = 0.15%), not a fraction.
        self.assertEqual(bp.WATCH_GAP_PCT_DEFAULT, Decimal("0.15"))
        # Funding rates are per-period FRACTIONS: 0.0001 = 1 bp = 0.01%.
        self.assertEqual(Decimal("0.0001") * Decimal(10000), Decimal("1"))
        # Supervision layer speaks RATIO strings: '0.04' entry to '0.1'
        # current is a 0.06 adverse move, breaching the '0.02' policy.
        facts = baseline_facts()
        facts["current_executable_basis"] = "0.1"
        decision = bp.supervise(baseline_position(), common_policy(),
                                basis_policy(), facts, NOW)
        self.assertEqual(decision["why"][0], "BASIS_DIVERGENCE_INVALIDATION")
        # Fee rates land as FRACTIONS per leg and are stored with the
        # derived round-trip PERCENT: 0.001 + 0.0004 per leg, both legs
        # twice = (0.001*2 + 0.0004*2) * 100 = 0.28%.
        self.assertEqual((Decimal("0.001") * 2 + Decimal("0.0004") * 2)
                         * 100, Decimal("0.28"))

    def test_spec_default_exit_fees_are_per_leg_percent(self):
        with tempfile.TemporaryDirectory() as empty:
            fees, source = bp.exit_fees_pct(empty)
        self.assertEqual(fees, Decimal("0.15"))    # 0.1 + 0.05, one leg each
        self.assertEqual(source, "spec_default")


# ==========================================================================
# Persistent machine — scripted transport, executor conventions.
# ==========================================================================

def term_spot(status, executed, quote, order_id):
    def responder(params):
        return ("ok", {"clientOrderId": params["origClientOrderId"],
                       "status": status, "executedQty": executed,
                       "cummulativeQuoteQty": quote, "orderId": order_id})
    return responder


def term_usdm(status, executed, quote, order_id):
    def responder(params):
        return ("ok", {"clientOrderId": params["origClientOrderId"],
                       "status": status, "executedQty": executed,
                       "cumQuote": quote, "orderId": order_id})
    return responder


NEW_ORDER = ("ok", {"status": "NEW"})
MISSING = ("api_error", {"code": -2013, "msg": "Order does not exist."})
FLAT_POSITION = [{"symbol": SYMBOL, "positionAmt": "0.000"}]
_AUTO_PROPOSAL = object()


def proposal_payload(proposal_id=PROPOSAL_ID):
    return {
        "proposal_id": proposal_id,
        "created_utc": "2025-08-12T00:00:00Z",
        "expires_utc": "2099-01-01T00:00:00Z",
        "quant_sha": SHA,
        "config_digest": "c" * 64,
        "symbol": SYMBOL,
        "direction": "long_spot_short_perp",
        "spot_ask": "100.0",
        "perp_bid": "100.2",
        "entry_gap_pct": "0.2",
        "spot_bid": "99.9",
        "perp_ask": "100.3",
        "exit_gap_pct": "0.4004004004",
        "spot_maker_commission_pct": "0.08",
        "spot_taker_commission_pct": "0.075",
        "perp_maker_commission_pct": "0.02",
        "perp_taker_commission_pct": "0.04",
        "commission_source": "measured",
        "entry_slippage_pct": "0.01",
        "exit_slippage_pct": "0.01",
        "funding_rate": "0.0001",
        "funding_period_ms": 28800000,
        "carry_pct": "0.01",
        "safety_buffer_pct": "0.01",
        "net_edge_pct": "0.05",
        "quantity": "0.099",
    }


def working_usdm(params):
    return ("ok", {"clientOrderId": params["origClientOrderId"],
                   "status": "NEW", "executedQty": "0", "cumQuote": "0"})


def happy_spec():
    """Open fixture: buy 0.099 (fee 0.000099 BTC) -> net 0.098901, hedge
    floored to 0.098 at the 0.001 shared step; short 0.098 at 9.8196; close
    legs and income rows for the close test."""
    return {
        "spot_info": {"symbols": [{
            "symbol": SYMBOL, "status": "TRADING", "isSpotTradingAllowed": True,
            "orderTypes": ["LIMIT", "MARKET"],
            "quoteOrderQtyMarketAllowed": True,
            "baseAsset": "BTC", "quoteAsset": "USDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.00010000",
                 "minQty": "0.00010000"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00010000",
                 "minQty": "0.00010000"},
                {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
            ]}]},
        "usdm_info": {"symbols": [{
            "symbol": SYMBOL, "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT", "marginAsset": "USDT",
            "orderTypes": ["LIMIT", "MARKET"],
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001",
                 "minQty": "0.001"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001",
                 "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]}]},
        "spot_book": {"symbol": SYMBOL, "bidPrice": "99.9",
                      "askPrice": "100.0"},
        "usdm_book": {"symbol": SYMBOL, "bidPrice": "100.2",
                      "askPrice": "100.3", "time": 1755000000000},
        "premium": {"symbol": SYMBOL, "lastFundingRate": "0.0001",
                    "nextFundingTime": 1755003600000},
        "my_trades": {},
        "spot_account": {"canTrade": True, "balances": [
            {"asset": "USDT", "free": "1000", "locked": "0"},
            {"asset": "BTC", "free": "0.098901", "locked": "0"}]},
        "income": [
            {"symbol": SYMBOL, "incomeType": "FUNDING_FEE",
             "income": "0.0011"},
            {"symbol": SYMBOL, "incomeType": "COMMISSION",
             "income": "-0.0079"}],
        "legs": {
            "enter_spot": {
                "submit": ("ok", {"fills": [
                    {"qty": "0.09900000", "commission": "0.00009900",
                     "commissionAsset": "BTC"}]}),
                "query": [term_spot("FILLED", "0.09900000", "9.90000000",
                                    111222)],
            },
            "enter_short": {
                "submit": ("ok", {}),
                "query": [term_usdm("FILLED", "0.098", "9.81960000",
                                    444555)],
            },
            "exit_short": {
                "submit": ("ok", {}),
                "query": [term_usdm("FILLED", "0.098", "9.81060000",
                                    444556)],
            },
            "exit_spot": {
                "submit": ("ok", {"fills": [
                    {"qty": "0.09890000", "commission": "0.00988000",
                     "commissionAsset": "USDT"}]}),
                "query": [term_spot("FILLED", "0.09890000", "9.88011000",
                                    111223)],
            },
            "unwind_spot": {
                "submit": ("ok", {"fills": [
                    {"qty": "0.09890000", "commission": "0.00988000",
                     "commissionAsset": "USDT"}]}),
                "query": [term_spot("FILLED", "0.09890000", "9.88011000",
                                    111224)],
            },
        },
    }


def one_unit_spec():
    """A valid 10 USDT fixture whose measured net entry is exactly 1.0."""
    spec = happy_spec()
    spec["spot_book"] = {"symbol": SYMBOL, "bidPrice": "9.89",
                         "askPrice": "9.9"}
    spec["usdm_book"] = {"symbol": SYMBOL, "bidPrice": "10.0",
                         "askPrice": "10.01", "time": 1755000000000}
    spec["spot_account"]["balances"][1]["free"] = "1.0"
    spec["legs"]["enter_spot"] = {
        "submit": ("ok", {"fills": [
            {"qty": "1.001", "commission": "0.001",
             "commissionAsset": "BTC"}]}),
        "query": [term_spot("FILLED", "1.001", "9.9099", 211222)],
    }
    spec["legs"]["enter_short"] = {
        "submit": ("ok", {}),
        "query": [term_usdm("FILLED", "1.0", "10.0", 544555)],
    }
    spec["legs"]["exit_short"] = {
        "submit": ("ok", {}),
        "query": [term_usdm("FILLED", "1.0", "9.99", 544556)],
    }
    spec["legs"]["exit_spot"] = {
        "submit": ("ok", {"fills": [
            {"qty": "1.0", "commission": "0.00989",
             "commissionAsset": "USDT"}]}),
        "query": [term_spot("FILLED", "1.0", "9.89", 211223)],
    }
    return spec


class ScriptedTransport:
    """(kind, payload) contract of the live transport. Differs from the
    executor harness in one place: ALL positionRisk reads serve the single
    "position" key (this machine reads it at most once per command)."""

    def __init__(self, spec, on_post=None):
        self.spec = spec
        self.on_post = on_post
        self.calls = []
        self.posts = []
        self.query_counts = {}
        self.leverage_posts = []

    def scripted(self, key, default):
        entry = self.spec.get(key, default)
        return entry if isinstance(entry, tuple) else ("ok", entry)

    def __call__(self, method, base_url, path, params=None, signed=False):
        params = dict(params or {})
        self.calls.append((method, base_url, path, params, signed))
        if method == "POST":
            if path == "/fapi/v1/leverage":
                self.leverage_posts.append(params)
                return self.scripted("leverage_post",
                                     {"symbol": SYMBOL, "leverage": 1})
            if self.on_post:
                self.on_post(params)
            self.posts.append((base_url, params))
            stage = params["newClientOrderId"].split("-", 2)[2]
            entry = self.spec["legs"][stage].get("submit", ("ok", {}))
            return entry(params) if callable(entry) else entry
        if path.endswith("exchangeInfo"):
            key = "spot_info" if base_url == SPOT_URL else "usdm_info"
            return ("ok", self.spec[key])
        if path.endswith("ticker/bookTicker"):
            key = "spot_book" if base_url == SPOT_URL else "usdm_book"
            return self.scripted(key, self.spec.get(key))
        if path.endswith("premiumIndex"):
            return self.scripted("premium", self.spec.get("premium"))
        if path.endswith("myTrades"):
            trades = self.spec.get("my_trades", {}).get(
                str(params.get("orderId")), [])
            return ("ok", trades)
        if path == "/api/v3/account/commission":
            return self.scripted("spot_commission", {
                "symbol": SYMBOL,
                "standardCommission": {"maker": "0.00100000",
                                       "taker": "0.00100000"}})
        if path == "/fapi/v1/commissionRate":
            return self.scripted("usdm_commission", {
                "symbol": SYMBOL, "makerCommissionRate": "0.000200",
                "takerCommissionRate": "0.000400"})
        if path == "/fapi/v1/income":
            return self.scripted("income", [])
        if path == "/fapi/v1/accountConfig":
            return self.scripted("usdm_account_config", {"canTrade": True})
        if path == "/api/v3/account":
            return self.scripted("spot_account", {"canTrade": True})
        if path == "/fapi/v3/account":
            return self.scripted("usdm_account", {"availableBalance": "1000"})
        if path == "/fapi/v1/positionSide/dual":
            return self.scripted("position_mode", {"dualSidePosition": False})
        if path == "/fapi/v1/symbolConfig":
            sequence = self.spec.get("symbol_config_seq")
            if sequence:
                entry = sequence.pop(0) if len(sequence) > 1 else sequence[0]
                return entry if isinstance(entry, tuple) else ("ok", entry)
            return self.scripted("symbol_config",
                                 [{"symbol": SYMBOL, "leverage": "1"}])
        if path.endswith("openOrders"):
            key = "spot_open_orders" if base_url == SPOT_URL \
                else "usdm_open_orders"
            return self.scripted(key, [])
        if path.endswith("positionRisk"):
            return self.scripted("position", FLAT_POSITION)
        if path.endswith("/order"):
            cid = params["origClientOrderId"]
            stage = cid.split("-", 2)[2]
            sequence = self.spec["legs"][stage]["query"]
            index = self.query_counts.get(cid, 0)
            self.query_counts[cid] = index + 1
            entry = sequence[index] if index < len(sequence) else sequence[-1]
            return entry(params) if callable(entry) else entry
        raise AssertionError("unexpected GET %s" % path)


def post_stages(transport):
    return [params["newClientOrderId"].split("-", 2)[2]
            for _, params in transport.posts]


class MachineCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = holder.name
        self.cfg = PositionConfig(
            symbol=SYMBOL, sha=SHA, receipt_dir=self.dir, run_id=RID,
            max_hold_ms=86400000,
            min_remaining_edge_pct=Decimal("0.01"),
            max_adverse_move_pct=Decimal("0.5"))
        self.write_proposal()

    def write_proposal(self, payload=None, proposal_id=PROPOSAL_ID):
        proposal_dir = os.path.join(self.dir, bp.PROPOSAL_DIR)
        os.makedirs(proposal_dir, exist_ok=True)
        path = os.path.join(proposal_dir,
                            "proposal-%s.json" % proposal_id)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload or proposal_payload(proposal_id), handle,
                      sort_keys=True)
        return path

    def run_cmd(self, handler, spec_or_transport, run_id=RID, now=T_OPEN,
                proposal_id=_AUTO_PROPOSAL):
        transport = spec_or_transport \
            if callable(spec_or_transport) \
            else ScriptedTransport(spec_or_transport)
        cfg = dataclasses.replace(self.cfg, run_id=run_id)
        buffer = io.StringIO()
        kwargs = {"sleep": lambda seconds: None,
                  "now_ms": lambda: now}
        if handler is bp.cmd_open:
            kwargs["proposal_id"] = PROPOSAL_ID \
                if proposal_id is _AUTO_PROPOSAL else proposal_id
        with redirect_stdout(buffer):
            code = handler(cfg, transport, **kwargs)
        return code, transport, buffer.getvalue()

    def position(self):
        with open(os.path.join(self.dir, bp.STATE_FILE),
                  encoding="utf-8") as handle:
            return json.load(handle)

    def receipt(self, run_id=RID):
        path = os.path.join(self.dir, "basis-position-%s.json" % run_id)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def lock_exists(self):
        return os.path.exists(os.path.join(self.dir, bp.LOCK_FILE))

    def events_list(self):
        path = os.path.join(self.dir, "events.jsonl")
        with open(path, encoding="utf-8") as handle:
            parsed = [json.loads(line)
                      for line in handle.read().splitlines() if line]
        for event in parsed:
            events.validate_event(event)
        return parsed

    def open_position(self, spec=None):
        code, transport, out = self.run_cmd(bp.cmd_open,
                                            spec or happy_spec())
        self.assertEqual(code, 0, out)
        return transport


class RatesTests(MachineCase):
    def test_rates_written_atomically_with_rate_values(self):
        code, transport, out = self.run_cmd(bp.cmd_rates, happy_spec())
        self.assertEqual(code, 0, out)
        path = os.path.join(self.dir, bp.RATES_FILE)
        with open(path, encoding="utf-8") as handle:
            rates = json.load(handle)
        self.assertEqual(rates["spot"]["taker"], "0.00100000")
        self.assertEqual(rates["usdm"]["taker"], "0.000400")
        self.assertEqual(Decimal(rates["round_trip_taker_fees_pct"]),
                         Decimal("0.28"))
        self.assertIn("verdict", rates)      # cross-lane verdict gate
        self.assertFalse(os.path.exists(path + ".tmp"))
        self.assertEqual(transport.posts, [])

    def test_rates_read_failure_writes_nothing(self):
        spec = happy_spec()
        spec["usdm_commission"] = ("api_error", {"code": -1, "msg": "down"})
        code, _transport, out = self.run_cmd(bp.cmd_rates, spec)
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", out)
        self.assertFalse(os.path.exists(os.path.join(self.dir,
                                                     bp.RATES_FILE)))

    def test_measured_rates_feed_exit_fees(self):
        self.run_cmd(bp.cmd_rates, happy_spec())
        fees, source = bp.exit_fees_pct(self.dir)
        self.assertEqual(fees, Decimal("0.14"))   # (0.001 + 0.0004) * 100
        self.assertEqual(source, "measured")


class OpenTests(MachineCase):
    def test_open_without_proposal_refuses_before_any_transport(self):
        transport = ScriptedTransport(happy_spec())
        code, transport, out = self.run_cmd(
            bp.cmd_open, transport, proposal_id=None)
        self.assertEqual(code, 2)
        self.assertIn("requires --proposal", out)
        self.assertEqual(transport.calls, [])

    def test_open_expired_proposal_refuses_before_any_transport(self):
        proposal = proposal_payload()
        proposal["expires_utc"] = "2025-08-12T01:00:00Z"
        self.write_proposal(proposal)
        transport = ScriptedTransport(happy_spec())
        code, transport, out = self.run_cmd(bp.cmd_open, transport)
        self.assertEqual(code, 2)
        self.assertIn("expired", out)
        self.assertEqual(transport.calls, [])

    def test_open_sha_mismatch_refuses_before_any_transport(self):
        proposal = proposal_payload()
        proposal["quant_sha"] = "feedfacefeedfacefeedfacefeedfacefeedface"
        self.write_proposal(proposal)
        transport = ScriptedTransport(happy_spec())
        code, transport, out = self.run_cmd(bp.cmd_open, transport)
        self.assertEqual(code, 2)
        self.assertIn("does not match QUANT_SHA", out)
        self.assertEqual(transport.calls, [])

    def test_open_negative_net_proposal_refuses_before_any_transport(self):
        proposal = proposal_payload()
        proposal["net_edge_pct"] = "-0.01"
        self.write_proposal(proposal)
        transport = ScriptedTransport(happy_spec())
        code, transport, out = self.run_cmd(bp.cmd_open, transport)
        self.assertEqual(code, 2)
        self.assertIn("net_edge_pct must be positive", out)
        self.assertEqual(transport.calls, [])

    def test_stop_and_stop_jobs_block_open_before_proposal_or_transport(self):
        for seal_file in (bp.STOP_FILE, bp.STOP_JOBS_FILE):
            with self.subTest(seal_file=seal_file):
                seal_path = os.path.join(self.dir, seal_file)
                with open(seal_path, "w", encoding="utf-8") as handle:
                    handle.write("owner stop\n")
                blocked = ScriptedTransport(happy_spec())
                code, blocked, out = self.run_cmd(
                    bp.cmd_open, blocked, proposal_id=None)
                self.assertEqual(code, 3)
                self.assertIn("%s file present" % seal_file, out)
                self.assertNotIn("requires --proposal", out)
                self.assertEqual(blocked.calls, [])
                os.unlink(seal_path)

    def test_resume_and_close_still_run_with_both_entry_seals(self):
        self.open_position()
        for seal_file in (bp.STOP_FILE, bp.STOP_JOBS_FILE):
            with open(os.path.join(self.dir, seal_file), "w",
                      encoding="utf-8") as handle:
                handle.write("owner stop\n")
        resume_spec = happy_spec()
        resume_spec["position"] = [
            {"symbol": SYMBOL, "positionAmt": "-0.098"}]
        code, resumed, out = self.run_cmd(
            bp.cmd_resume, resume_spec, run_id="cafe02")
        self.assertEqual(code, 0, out)
        self.assertTrue(resumed.calls)
        code, closed, out = self.run_cmd(
            bp.cmd_close, happy_spec(), run_id="cafe03")
        self.assertEqual(code, 0, out)
        self.assertEqual(post_stages(closed), ["exit_short", "exit_spot"])

    def test_open_applies_cross_lane_prior_receipt_gate_before_transport(self):
        prior_path = os.path.join(self.dir, "roundtrip-cafe00.json")
        with open(prior_path, "w", encoding="utf-8") as handle:
            json.dump({"run_id": "cafe00", "legs": []}, handle)

        blocked = ScriptedTransport(happy_spec())
        code, blocked, out = self.run_cmd(bp.cmd_open, blocked)
        self.assertEqual(code, 3)
        self.assertIn("no verdict", out)
        self.assertEqual(blocked.calls, [])
        self.assertEqual(blocked.posts, [])

        with open(prior_path, "w", encoding="utf-8") as handle:
            json.dump({"run_id": "cafe00", "verdict": "CLOSED"}, handle)
        code, allowed, out = self.run_cmd(bp.cmd_open, happy_spec())
        self.assertEqual(code, 0, out)
        self.assertEqual(post_stages(allowed), ["enter_spot", "enter_short"])

    def test_open_blocks_unreadable_prior_json_before_transport(self):
        with open(os.path.join(self.dir, "roundtrip-damaged.json"),
                  "w", encoding="utf-8") as handle:
            handle.write("{")
        transport = ScriptedTransport(happy_spec())
        code, transport, out = self.run_cmd(bp.cmd_open, transport)
        self.assertEqual(code, 3)
        self.assertIn("unreadable", out)
        self.assertEqual(transport.calls, [])

    def test_open_persists_open_with_entry_snapshot(self):
        transport = self.open_position()
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short"])
        position = self.position()
        self.assertEqual(position["state"], "OPEN")
        self.assertEqual(position["verdict"], "OPEN_EXPOSURE")
        entry = position["entry"]
        self.assertEqual(Decimal(entry["entry_gap_bps"]), Decimal("20"))
        self.assertEqual(Decimal(entry["entry_gap_pct"]), Decimal("0.2"))
        self.assertEqual(entry["funding_rate"], "0.0001")   # fraction
        self.assertEqual(Decimal(entry["spot"]["net_base"]),
                         Decimal("0.098901"))
        self.assertEqual(Decimal(entry["hedge_qty"]), Decimal("0.098"))
        self.assertEqual(Decimal(entry["short"]["filled_qty"]),
                         Decimal("0.098"))
        self.assertEqual(position["thesis"]["opened_at_ms"], T_OPEN)
        self.assertEqual(position["thesis"]["max_hold_ms"], 86400000)
        proposal_path = os.path.join(
            self.dir, bp.PROPOSAL_DIR,
            "proposal-%s.json" % PROPOSAL_ID)
        with open(proposal_path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(position["proposal_id"], PROPOSAL_ID)
        self.assertEqual(position["proposal_digest"], digest)
        receipt_path = os.path.join(
            self.dir, "basis-position-%s.json" % RID)
        with open(receipt_path, encoding="utf-8") as handle:
            receipt = json.load(handle)
        self.assertEqual(receipt["proposal_id"], PROPOSAL_ID)
        self.assertEqual(receipt["proposal_digest"], digest)
        self.assertIsNone(position["pending_intent"])
        self.assertEqual(len(position["client_ids"]), 2)
        self.assertTrue(self.lock_exists())    # lane held while standing
        kinds = [event["kind"] for event in self.events_list()]
        self.assertEqual(kinds, ["run_start", "order_submitted",
                                 "order_terminal", "order_submitted",
                                 "order_terminal", "run_verdict"])

    def test_intent_persisted_before_every_post(self):
        seen = []

        def on_post(params):
            with open(os.path.join(self.dir, bp.STATE_FILE),
                      encoding="utf-8") as handle:
                position = json.load(handle)
            pending = position["pending_intent"]
            seen.append((params["newClientOrderId"],
                         pending and pending["client_id"],
                         pending and pending["intent"].get("symbol")))
        transport = ScriptedTransport(happy_spec(), on_post=on_post)
        code, _t, out = self.run_cmd(bp.cmd_open, transport)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(seen), 2)
        for posted, persisted, symbol in seen:
            self.assertEqual(posted, persisted,
                             "the exact client id must be persisted before "
                             "the POST")
            self.assertEqual(symbol, SYMBOL)

    def test_open_sets_leverage_via_one_post_when_not_1x(self):
        spec = happy_spec()
        spec["symbol_config_seq"] = [
            [{"symbol": SYMBOL, "leverage": "10"}],
            [{"symbol": SYMBOL, "leverage": "10"}],
            [{"symbol": SYMBOL, "leverage": "1"}],
        ]
        code, transport, out = self.run_cmd(bp.cmd_open, spec)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(transport.leverage_posts), 1)
        self.assertEqual(transport.leverage_posts[0]["leverage"], "1")
        # Every read-only proof completed before the leverage mutation.
        leverage_index = next(index for index, call
                              in enumerate(transport.calls)
                              if call[2] == "/fapi/v1/leverage")
        required_reads = {
            (USDM_URL, "/fapi/v1/accountConfig"),
            (SPOT_URL, "/api/v3/account"),
            (USDM_URL, "/fapi/v1/positionSide/dual"),
            (SPOT_URL, "/api/v3/openOrders"),
            (USDM_URL, "/fapi/v1/openOrders"),
            (USDM_URL, "/fapi/v3/positionRisk"),
            (USDM_URL, "/fapi/v3/account"),
        }
        before_leverage = {(call[1], call[2])
                           for call in transport.calls[:leverage_index]
                           if call[0] == "GET"}
        self.assertTrue(required_reads <= before_leverage)
        revalidation_reads = [
            (index, call[2]) for index, call in enumerate(transport.calls)
            if call[2].endswith("ticker/bookTicker")]
        self.assertEqual([path for _index, path in revalidation_reads],
                         ["/api/v3/ticker/bookTicker",
                          "/fapi/v1/ticker/bookTicker"])
        self.assertTrue(all(index < leverage_index
                            for index, _path in revalidation_reads),
                        "entry revalidation must precede the leverage "
                        "mutation")
        first_order = next(index for index, call
                           in enumerate(transport.calls)
                           if call[0] == "POST" and call[2].endswith("/order"))
        self.assertLess(leverage_index, first_order)
        between = transport.calls[leverage_index + 1:first_order]
        self.assertEqual([(call[0], call[2]) for call in between],
                         [("GET", "/fapi/v1/symbolConfig")])

    def test_open_refuses_unproven_leverage_before_any_order(self):
        spec = happy_spec()
        spec["symbol_config_seq"] = [
            [{"symbol": SYMBOL, "leverage": "10"}],
            [{"symbol": SYMBOL, "leverage": "10"}],
            [{"symbol": SYMBOL, "leverage": "10"}],   # re-read disproves
        ]
        code, transport, _out = self.run_cmd(bp.cmd_open, spec)
        self.assertEqual(code, 5)
        self.assertEqual(transport.posts, [])
        self.assertFalse(self.lock_exists())

    def test_open_requires_owner_thresholds(self):
        for field in ("max_hold_ms", "min_remaining_edge_pct",
                      "max_adverse_move_pct"):
            cfg = dataclasses.replace(self.cfg, **{field: None})
            transport = ScriptedTransport(happy_spec())
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = bp.cmd_open(cfg, transport,
                                   sleep=lambda seconds: None,
                                   now_ms=lambda: T_OPEN,
                                   proposal_id=PROPOSAL_ID)
            self.assertEqual(code, 2)
            self.assertIn("owner decision required", buffer.getvalue())
            self.assertEqual(transport.posts, [])

    def test_open_entry_revalidation_equal_gap_proceeds_and_records(self):
        code, transport, out = self.run_cmd(bp.cmd_open, happy_spec())
        self.assertEqual(code, 0, out)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short"])
        check = self.receipt()["entry_revalidation"]
        self.assertEqual(Decimal(check["proposal_entry_gap_pct"]),
                         Decimal("0.2"))
        self.assertEqual(Decimal(check["fresh_entry_gap_pct"]),
                         Decimal("0.2"))
        self.assertEqual(check["decision"], "PROCEED")

    def test_open_entry_revalidation_better_gap_proceeds(self):
        spec = happy_spec()
        spec["usdm_book"]["bidPrice"] = "100.25"
        code, transport, out = self.run_cmd(bp.cmd_open, spec)
        self.assertEqual(code, 0, out)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short"])
        self.assertGreater(
            Decimal(self.receipt()["entry_revalidation"]
                    ["fresh_entry_gap_pct"]), Decimal("0.2"))

    def test_open_entry_revalidation_decay_refuses_without_posts(self):
        spec = happy_spec()
        spec["usdm_book"] = {"symbol": SYMBOL, "bidPrice": "100.18",
                             "askPrice": "100.28", "time": 1755000000000}
        code, transport, out = self.run_cmd(bp.cmd_open, spec)
        self.assertEqual(code, 2, out)
        self.assertIn("ENTRY_REVALIDATION_FAILED", out)
        self.assertFalse([call for call in transport.calls
                          if call[0] == "POST"])
        check = self.receipt()["entry_revalidation"]
        self.assertEqual(Decimal(check["proposal_entry_gap_pct"]),
                         Decimal("0.2"))
        self.assertEqual(Decimal(check["fresh_entry_gap_pct"]),
                         Decimal("0.18"))
        self.assertEqual(check["decision"], "REFUSE")
        self.assertEqual(check["reason"], "ENTRY_REVALIDATION_FAILED")

    def test_open_entry_revalidation_unreadable_book_refuses(self):
        spec = happy_spec()
        spec["spot_book"] = ("unclear", {"http": 503})
        code, transport, out = self.run_cmd(bp.cmd_open, spec)
        self.assertEqual(code, 2, out)
        self.assertIn("ENTRY_REVALIDATION_FAILED", out)
        self.assertFalse([call for call in transport.calls
                          if call[0] == "POST"])
        check = self.receipt()["entry_revalidation"]
        self.assertEqual(check["decision"], "REFUSE")
        self.assertEqual(check["fresh_books"]["spot"]["read_status"],
                         "unclear")
        book_paths = [call[2] for call in transport.calls
                      if call[2].endswith("ticker/bookTicker")]
        self.assertEqual(book_paths, ["/api/v3/ticker/bookTicker",
                                      "/fapi/v1/ticker/bookTicker"])

    def test_open_entry_revalidation_crossed_book_refuses(self):
        spec = happy_spec()
        spec["spot_book"] = {"symbol": SYMBOL, "bidPrice": "100.1",
                             "askPrice": "100.0"}
        code, transport, out = self.run_cmd(bp.cmd_open, spec)
        self.assertEqual(code, 2, out)
        self.assertIn("ENTRY_REVALIDATION_FAILED", out)
        self.assertIn("book is crossed", out)
        self.assertFalse([call for call in transport.calls
                          if call[0] == "POST"])

    def test_open_entry_revalidation_nonpositive_gap_refuses(self):
        spec = happy_spec()
        spec["usdm_book"]["bidPrice"] = "100.0"
        code, transport, out = self.run_cmd(bp.cmd_open, spec)
        self.assertEqual(code, 2, out)
        self.assertIn("ENTRY_REVALIDATION_FAILED", out)
        self.assertIn("not positive", out)
        self.assertFalse([call for call in transport.calls
                          if call[0] == "POST"])

    def test_open_wallet_shortfall_refuses(self):
        spec = happy_spec()
        spec["spot_account"] = {"canTrade": True, "balances": [
            {"asset": "USDT", "free": "1.00", "locked": "0"}]}
        code, transport, out = self.run_cmd(bp.cmd_open, spec)
        self.assertEqual(code, 5)
        self.assertEqual(transport.posts, [])
        self.assertIn("PREFLIGHT_REFUSED", out)

    def test_open_short_rejected_unwinds_and_frees_lane(self):
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("api_error", {"code": -2019, "msg": "margin"}),
            "query": [MISSING],
        }
        code, transport, out = self.run_cmd(bp.cmd_open, spec)
        self.assertEqual(code, 2)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short", "unwind_spot"])
        position = self.position()
        self.assertEqual(position["state"], "NONE")
        self.assertFalse(self.lock_exists())
        self.assertIn("REJECTED_BEFORE_ENTRY", out)

    def test_open_short_unknown_freezes_lane(self):
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("unclear", {"http": 504}),
            "query": [lambda params: ("unclear", {})],
        }
        code, transport, out = self.run_cmd(bp.cmd_open, spec)
        self.assertEqual(code, 4)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short"])   # never resent
        position = self.position()
        self.assertEqual(position["state"], "UNKNOWN")
        self.assertIsNotNone(position["pending_intent"])
        self.assertIn("bc-%s-enter_short" % RID,
                      position["pending_intent"]["client_id"])
        self.assertTrue(self.lock_exists())
        self.assertIn("either unhedged spot or the full pair", out)

    def test_open_refuses_while_position_stands(self):
        self.open_position()
        transport = ScriptedTransport(happy_spec())
        code, transport, out = self.run_cmd(bp.cmd_open, transport,
                                            run_id="cafe09")
        self.assertEqual(code, 3)
        self.assertEqual(transport.posts, [])
        self.assertIn("one-position machine", out)


class CheckTests(MachineCase):
    def check(self, spec=None, now=T_OPEN + 60000, transport=None):
        return self.run_cmd(bp.cmd_check, transport or spec or happy_spec(),
                            run_id="cafe02", now=now)

    def test_check_holds_within_policy(self):
        self.open_position()
        code, transport, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertEqual(transport.posts, [])     # a check never orders
        self.assertIn("DECISION: HOLD", out)
        position = self.position()
        self.assertEqual(position["last_check"]["decision"], "HOLD")
        self.assertEqual(Decimal(position["last_check"]["current_gap_pct"]),
                         Decimal("0.2"))

    def test_check_refuses_to_clobber_concurrent_state_mutation(self):
        self.open_position()
        transport = ScriptedTransport(happy_spec())
        mutated = False

        def mutate_during_public_reads(method, base_url, path, params=None,
                                       signed=False):
            nonlocal mutated
            if not mutated and path.endswith("ticker/bookTicker"):
                concurrent = bp.load_position(self.dir)
                concurrent["state"] = bp.ST_CLOSING
                concurrent["race_marker"] = "concurrent-close"
                bp.save_position(self.dir, concurrent)
                mutated = True
            return transport(method, base_url, path, params, signed)

        code, _request, out = self.check(
            transport=mutate_during_public_reads)
        self.assertTrue(mutated)
        self.assertEqual(code, 3)
        self.assertIn("changed while --check was reading", out)
        persisted = self.position()
        self.assertEqual(persisted["state"], bp.ST_CLOSING)
        self.assertEqual(persisted["race_marker"], "concurrent-close")
        self.assertNotIn("last_check", persisted)
        self.assertEqual(transport.posts, [])

    def test_check_gap_below_watch_is_target_reached(self):
        self.open_position()
        spec = happy_spec()
        spec["usdm_book"] = {"symbol": SYMBOL, "bidPrice": "100.05",
                             "askPrice": "100.10", "time": 1755000600000}
        code, _t, out = self.check(spec)
        self.assertEqual(code, 0)
        self.assertIn("DECISION: REQUEST_EXIT", out)
        self.assertIn("THESIS_TARGET_REACHED", out)
        self.assertIn("gap_closed", out)

    def test_check_max_hold_exit(self):
        self.open_position()
        code, _t, out = self.check(now=T_OPEN + 86400000)
        self.assertEqual(code, 0)
        self.assertIn("DECISION: REQUEST_EXIT", out)
        self.assertIn("MAXIMUM_HOLDING_HORIZON", out)

    def test_check_edge_inversion_remaining_edge(self):
        self.open_position()
        spec = happy_spec()
        # Gap 16 bps = 0.16%: above watch 0.15 but remaining edge after the
        # spec-default 0.15% exit fees is 0.01 <= minimum 0.01.
        spec["usdm_book"] = {"symbol": SYMBOL, "bidPrice": "100.16",
                             "askPrice": "100.26", "time": 1755000600000}
        code, _t, out = self.check(spec)
        self.assertEqual(code, 0)
        self.assertIn("REMAINING_EDGE_EXHAUSTED", out)

    def test_check_edge_inversion_divergence(self):
        self.open_position()
        spec = happy_spec()
        # Gap widened to 0.8%: adverse move 0.6% > owner maximum 0.5%.
        spec["usdm_book"] = {"symbol": SYMBOL, "bidPrice": "100.8",
                             "askPrice": "100.9", "time": 1755000600000}
        code, _t, out = self.check(spec)
        self.assertEqual(code, 0)
        self.assertIn("BASIS_DIVERGENCE_INVALIDATION", out)

    def test_check_emergency_file_wins_without_reads(self):
        self.open_position()
        with open(os.path.join(self.dir, bp.EMERGENCY_FILE), "w") as handle:
            handle.write("owner emergency\n")

        def exploding(method, base_url, path, params=None, signed=False):
            raise AssertionError("no reads expected under emergency")
        code, _t, out = self.check(transport=exploding,
                                   now=T_OPEN + 86400000)   # even past hold
        self.assertEqual(code, 0)
        self.assertIn("REQUEST_EMERGENCY_UNWIND", out)
        self.assertIn("EMERGENCY_FILE_PRESENT", out)

    def test_check_unreadable_books_freeze_no_exit_fabricated(self):
        self.open_position()
        spec = happy_spec()
        spec["spot_book"] = ("unclear", {"http": 503})
        code, _t, out = self.check(spec)
        self.assertEqual(code, 4)
        self.assertIn("FREEZE_NEW_ACTIONS", out)
        self.assertIn("BASIS_REQUIRED_TRUTH_UNKNOWN", out)
        self.assertNotIn("REQUEST_EXIT", out)

    def test_check_without_position_refuses(self):
        code, _t, out = self.check()
        self.assertEqual(code, 2)
        self.assertIn("no open position", out)

    def test_check_uses_measured_rates_when_recorded(self):
        self.open_position()
        self.run_cmd(bp.cmd_rates, happy_spec(), run_id="cafe03")
        code, _t, out = self.check()
        self.assertEqual(code, 0)
        position = self.position()
        self.assertEqual(position["last_check"]["fees_source"], "measured")
        self.assertEqual(Decimal(position["last_check"]["exit_fees_pct"]),
                         Decimal("0.14"))


class CloseTests(MachineCase):
    def replace_entry_spot_fees(self, fees):
        position = self.position()
        entry_spot = next(leg for leg in position["legs"]
                          if leg["stage"] == bp.ENTER_SPOT)
        entry_spot["fee_by_asset"] = dict(fees)
        bp.save_position(self.dir, position)

    def test_close_happy_reconciles_and_reports_pnl(self):
        self.open_position()
        code, transport, out = self.run_cmd(bp.cmd_close, happy_spec(),
                                            run_id="cafe02")
        self.assertEqual(code, 0, out)
        self.assertEqual(post_stages(transport), ["exit_short", "exit_spot"])
        exit_short_params = transport.posts[0][1]
        self.assertEqual(exit_short_params["reduceOnly"], "true")
        self.assertEqual(exit_short_params["quantity"], "0.098")
        exit_spot_params = transport.posts[1][1]
        self.assertEqual(Decimal(exit_spot_params["quantity"]),
                         Decimal("0.0989"))
        position = self.position()
        self.assertEqual(position["state"], "CLOSED")
        self.assertEqual(position["verdict"], "CLOSED")
        pnl = position["close"]["pnl"]
        self.assertEqual(Decimal(pnl["spot_quote_pnl_usdt"]),
                         Decimal("-0.01989"))
        self.assertEqual(Decimal(pnl["short_quote_pnl_usdt"]),
                         Decimal("0.009"))
        self.assertEqual(Decimal(pnl["spot_fees_usdt"]),
                         Decimal("0.00988"))
        self.assertEqual(Decimal(pnl["funding_usdt"]), Decimal("0.0011"))
        self.assertEqual(Decimal(pnl["usdm_commission_usdt"]),
                         Decimal("-0.0079"))
        self.assertEqual(Decimal(pnl["total_usdt"]), Decimal("-0.02757"))
        self.assertFalse(self.lock_exists())      # lane freed on CLOSED
        self.assertIn("VERDICT: CLOSED", out)

    def test_close_all_usdt_fees_keeps_complete_pnl(self):
        self.open_position()
        self.replace_entry_spot_fees({"USDT": "0.001"})
        code, _transport, out = self.run_cmd(
            bp.cmd_close, happy_spec(), run_id="cafe02")
        self.assertEqual(code, 0, out)
        pnl = self.position()["close"]["pnl"]
        self.assertEqual(Decimal(pnl["spot_fees_usdt"]),
                         Decimal("0.01088"))
        self.assertEqual(Decimal(pnl["total_usdt"]), Decimal("-0.02857"))
        self.assertNotIn("total_usdt_status", pnl)
        self.assertNotIn("excluded_spot_fees_by_asset", pnl)

    def test_close_mixed_usdt_bnb_fees_marks_partial_pnl(self):
        self.open_position()
        self.replace_entry_spot_fees({"USDT": "0.001",
                                      "BNB": "0.00042"})
        code, _transport, out = self.run_cmd(
            bp.cmd_close, happy_spec(), run_id="cafe02")
        self.assertEqual(code, 0, out)
        pnl = self.position()["close"]["pnl"]
        self.assertEqual(pnl["total_usdt_status"],
                         "PARTIAL_FEES_EXCLUDED")
        self.assertEqual(set(pnl["spot_fees_by_asset"]), {"USDT", "BNB"})
        self.assertEqual(Decimal(pnl["spot_fees_by_asset"]["USDT"]),
                         Decimal("0.01088"))
        self.assertEqual(Decimal(pnl["spot_fees_by_asset"]["BNB"]),
                         Decimal("0.00042"))
        self.assertEqual(pnl["excluded_spot_fees_by_asset"],
                         {"BNB": "0.00042"})
        self.assertEqual(Decimal(pnl["spot_fees_usdt"]),
                         Decimal("0.01088"))
        self.assertEqual(Decimal(pnl["total_usdt"]), Decimal("-0.02857"))

    def test_close_persists_intent_before_each_post(self):
        self.open_position()
        seen = []

        def on_post(params):
            with open(os.path.join(self.dir, bp.STATE_FILE),
                      encoding="utf-8") as handle:
                position = json.load(handle)
            pending = position["pending_intent"]
            seen.append((params["newClientOrderId"],
                         pending and pending["client_id"],
                         position["state"]))
        transport = ScriptedTransport(happy_spec(), on_post=on_post)
        code, _t, out = self.run_cmd(bp.cmd_close, transport,
                                     run_id="cafe02")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(seen), 2)
        for posted, persisted, state in seen:
            self.assertEqual(posted, persisted)
            self.assertEqual(state, "CLOSING")

    def test_close_short_rejected_keeps_pair_open(self):
        self.open_position()
        spec = happy_spec()
        spec["legs"]["exit_short"] = {
            "submit": ("api_error", {"code": -2022, "msg": "reduce only"}),
            "query": [MISSING],
        }
        code, transport, out = self.run_cmd(bp.cmd_close, spec,
                                            run_id="cafe02")
        self.assertEqual(code, 3)
        self.assertEqual(post_stages(transport), ["exit_short"])
        position = self.position()
        self.assertEqual(position["state"], "OPEN")     # pair intact
        self.assertTrue(self.lock_exists())
        self.assertIn("pair intact", out)

    def test_close_spot_unknown_freezes(self):
        self.open_position()
        spec = happy_spec()
        spec["legs"]["exit_spot"] = {
            "submit": ("unclear", {"http": 504}),
            "query": [lambda params: ("unclear", {})],
        }
        code, transport, out = self.run_cmd(bp.cmd_close, spec,
                                            run_id="cafe02")
        self.assertEqual(code, 4)
        position = self.position()
        self.assertEqual(position["state"], "UNKNOWN")
        self.assertTrue(self.lock_exists())
        self.assertIn("bc-cafe02-exit_spot", out.replace("_spo\n", "_spo"))
        self.assertEqual(post_stages(transport), ["exit_short", "exit_spot"])

    def test_close_unreadable_final_position_is_unknown(self):
        self.open_position()
        spec = happy_spec()
        spec["position"] = ("unclear", {})
        code, _t, out = self.run_cmd(bp.cmd_close, spec, run_id="cafe02")
        self.assertEqual(code, 4)
        self.assertEqual(self.position()["state"], "UNKNOWN")
        self.assertIn("unreadable", out)

    def test_close_unreadable_income_leaves_funding_unknown(self):
        self.open_position()
        spec = happy_spec()
        spec["income"] = ("unclear", {})
        code, _t, out = self.run_cmd(bp.cmd_close, spec, run_id="cafe02")
        self.assertEqual(code, 0, out)
        pnl = self.position()["close"]["pnl"]
        self.assertEqual(pnl["funding_usdt"], "UNKNOWN")
        self.assertEqual(pnl["total_usdt"], "UNKNOWN")  # never assumed zero

    def test_close_refuses_without_open_position(self):
        code, transport, out = self.run_cmd(bp.cmd_close, happy_spec())
        self.assertEqual(code, 2)
        self.assertEqual(transport.posts, [])
        self.assertIn("no OPEN position", out)

    def test_close_refuses_without_any_proven_short_quantity(self):
        self.open_position()
        position = bp.load_position(self.dir)
        del position["entry"]["short"]
        bp.save_position(self.dir, position)
        code, transport, out = self.run_cmd(bp.cmd_close, happy_spec(),
                                            run_id="cafe02")
        self.assertEqual(code, 2)
        self.assertEqual(transport.posts, [])
        self.assertEqual(self.position()["state"], "OPEN")
        self.assertIn("no proven short quantity", out)

    def test_close_after_partial_resume_uses_remaining_short(self):
        self.open_position(one_unit_spec())
        self.assertEqual(
            Decimal(self.position()["entry"]["short"]["filled_qty"]),
            Decimal("1.0"))

        partial_spec = one_unit_spec()
        partial_spec["legs"]["exit_short"]["query"] = [
            term_usdm("FILLED", "0.4", "4.0", 544557)]
        code, transport, out = self.run_cmd(bp.cmd_close, partial_spec,
                                            run_id="cafe02")
        self.assertEqual(code, 3, out)
        self.assertEqual(transport.posts[0][1]["quantity"], "1.0")
        self.assertEqual(self.position()["state"], "CLOSING")

        resume_spec = one_unit_spec()
        resume_spec["legs"]["exit_short"]["query"] = [
            term_usdm("FILLED", "0.4", "4.0", 544557)]
        resume_spec["position"] = [
            {"symbol": SYMBOL, "positionAmt": "-0.6"}]
        code, _transport, out = self.run_cmd(bp.cmd_resume, resume_spec,
                                             run_id="cafe03")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.position()["state"], "OPEN")

        second_spec = one_unit_spec()
        second_spec["legs"]["exit_short"]["query"] = [
            term_usdm("FILLED", "0.6", "5.99", 544558)]
        code, transport, out = self.run_cmd(bp.cmd_close, second_spec,
                                            run_id="cafe04")
        self.assertEqual(code, 0, out)
        self.assertEqual(transport.posts[0][1]["quantity"], "0.6")


class ResumeTests(MachineCase):
    def test_resume_open_position_intact(self):
        self.open_position()
        spec = happy_spec()
        spec["position"] = [{"symbol": SYMBOL, "positionAmt": "-0.098"}]
        code, transport, out = self.run_cmd(bp.cmd_resume, spec,
                                            run_id="cafe02")
        self.assertEqual(code, 0, out)
        self.assertEqual(transport.posts, [])          # nothing resent
        position = self.position()
        self.assertEqual(position["state"], "OPEN")
        self.assertEqual(position["thesis"]["opened_at_ms"], T_OPEN)
        self.assertEqual(position["reconcile"]["futures_position"], "-0.098")
        self.assertEqual(
            position["reconcile"]["truths"]["bc-%s-enter_spot" % RID],
            "terminal")
        self.assertTrue(self.lock_exists())

    def test_resume_proves_unknown_short_filled(self):
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("unclear", {"http": 504}),
            "query": [lambda params: ("unclear", {})],
        }
        code, _t, _out = self.run_cmd(bp.cmd_open, spec)
        self.assertEqual(code, 4)
        self.assertEqual(self.position()["state"], "UNKNOWN")
        resume_spec = happy_spec()
        resume_spec["position"] = [{"symbol": SYMBOL,
                                    "positionAmt": "-0.098"}]
        code, transport, out = self.run_cmd(bp.cmd_resume, resume_spec,
                                            run_id="cafe02",
                                            now=T_OPEN + 5000)
        self.assertEqual(code, 0, out)
        self.assertEqual(transport.posts, [])          # proven, not resent
        position = self.position()
        self.assertEqual(position["state"], "OPEN")
        self.assertNotIn("short", position["entry"])
        self.assertEqual(position["resume_proven"], {
            "run_id": "cafe02",
            "short_qty": "0.098",
            "source": "positionRisk",
        })
        # The short sub-snapshot was never persisted; the hold clock latches
        # at the proven-resume moment.
        self.assertEqual(position["thesis"]["opened_at_ms"], T_OPEN + 5000)

        code, close_transport, out = self.run_cmd(
            bp.cmd_close, happy_spec(), run_id="cafe03")
        self.assertEqual(code, 0, out)
        self.assertEqual(close_transport.posts[0][1]["quantity"], "0.098")
        position = self.position()
        self.assertEqual(position["state"], "CLOSED")
        self.assertEqual(position["close"]["pnl"]
                         ["short_quote_pnl_usdt"], "UNKNOWN")

    def test_resume_short_never_existed_names_one_leg(self):
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("unclear", {"http": 504}),
            "query": [MISSING],
        }
        code, _t, _out = self.run_cmd(bp.cmd_open, spec)
        self.assertEqual(code, 4)
        resume_spec = happy_spec()
        resume_spec["legs"]["enter_short"] = {"query": [MISSING]}
        code, transport, out = self.run_cmd(bp.cmd_resume, resume_spec,
                                            run_id="cafe02")
        self.assertEqual(code, 3, out)
        self.assertEqual(transport.posts, [])
        position = self.position()
        self.assertEqual(position["state"], "ONE_LEG_EXPOSED")
        recommendations = position["reconcile"]["recommendations"]
        self.assertEqual(recommendations,
                         [{"action": "CLOSE_EXPOSED_LEG",
                           "automatic": False}])
        self.assertIn("spot base free", out)
        self.assertTrue(self.lock_exists())

    def test_resume_never_infers_not_filled_from_timeout(self):
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("unclear", {"http": 504}),
            "query": [lambda params: ("unclear", {})],
        }
        self.run_cmd(bp.cmd_open, spec)
        resume_spec = happy_spec()
        resume_spec["legs"]["enter_short"] = {
            "query": [lambda params: ("unclear", {})]}   # still no truth
        code, transport, out = self.run_cmd(bp.cmd_resume, resume_spec,
                                            run_id="cafe02")
        self.assertEqual(code, 4, out)
        self.assertEqual(transport.posts, [])            # NEVER resent
        position = self.position()
        self.assertEqual(position["state"], "UNKNOWN")   # frozen, not closed
        self.assertTrue(self.lock_exists())
        self.assertIn("bc-%s-enter_short" % RID, out)

    def test_resume_working_order_freezes_with_cancel_recommendation(self):
        self.open_position()
        spec = happy_spec()
        spec["legs"]["enter_short"] = {"query": [working_usdm]}
        code, transport, out = self.run_cmd(bp.cmd_resume, spec,
                                            run_id="cafe02")
        self.assertEqual(code, 4, out)
        self.assertEqual(transport.posts, [])
        position = self.position()
        self.assertEqual(position["state"], "UNKNOWN")
        self.assertEqual(position["reconcile"]["recommendations"],
                         [{"action": "CANCEL_REMAINDER",
                           "client_id": "bc-%s-enter_short" % RID,
                           "automatic": False}])

    def test_resume_query_error_freezes_with_verify_recommendation(self):
        self.open_position()
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "query": [lambda params: ("api_error",
                                      {"code": -1000, "msg": "boom"})]}
        code, _t, out = self.run_cmd(bp.cmd_resume, spec, run_id="cafe02")
        self.assertEqual(code, 4, out)
        position = self.position()
        self.assertEqual(position["state"], "UNKNOWN")
        actions = [r["action"]
                   for r in position["reconcile"]["recommendations"]]
        self.assertIn("VERIFY_AMBIGUOUS_ORDER", actions)

    def test_resume_mid_close_proves_closed_and_frees_lane(self):
        self.open_position()
        position = bp.load_position(self.dir)
        position["state"] = bp.ST_CLOSING
        for stage in ("exit_short", "exit_spot"):
            client = "bc-cafe02-%s" % stage
            position["client_ids"][client] = {
                "stage": stage.upper(), "market":
                "usdm" if stage == "exit_short" else "spot",
                "run_id": "cafe02"}
        bp.save_position(self.dir, position)
        spec = happy_spec()
        spec["spot_account"] = {"canTrade": True, "balances": [
            {"asset": "USDT", "free": "1000", "locked": "0"},
            {"asset": "BTC", "free": "0.00000100", "locked": "0"}]}
        code, transport, out = self.run_cmd(bp.cmd_resume, spec,
                                            run_id="cafe03")
        self.assertEqual(code, 0, out)
        self.assertEqual(transport.posts, [])
        self.assertEqual(self.position()["state"], "CLOSED")
        self.assertFalse(self.lock_exists())
        self.assertIn("VERDICT: CLOSED", out)

    def test_resume_nothing_to_resume(self):
        code, _t, out = self.run_cmd(bp.cmd_resume, happy_spec())
        self.assertEqual(code, 0)
        self.assertIn("nothing to resume", out)

    def test_resume_lock_without_state_needs_human(self):
        with open(os.path.join(self.dir, bp.LOCK_FILE), "w") as handle:
            handle.write("stray")
        code, _t, out = self.run_cmd(bp.cmd_resume, happy_spec())
        self.assertEqual(code, 3)
        self.assertIn("human check required", out)


class StateFileTests(MachineCase):
    def test_state_file_always_carries_verdict_for_cross_lane_gate(self):
        self.open_position()
        position = self.position()
        self.assertIn("verdict", position)
        self.assertEqual(position["verdict"], "OPEN_EXPOSURE")
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, bp.STATE_FILE + ".tmp")))

    def test_position_receipts_use_own_prefix(self):
        self.open_position()
        names = os.listdir(self.dir)
        self.assertIn("basis-position-%s.json" % RID, names)
        self.assertFalse([name for name in names
                          if name.startswith("basis-executor-")
                          and name.endswith(".json")])

    def test_entry_snapshot_survives_crash_and_check_resumes(self):
        # The entry snapshot is persisted BEFORE the short leg, so even an
        # open frozen at short-UNKNOWN keeps its entry truth; after a
        # proven resume the hold loop continues normally.
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("unclear", {"http": 504}),
            "query": [lambda params: ("unclear", {})],
        }
        self.run_cmd(bp.cmd_open, spec)
        self.assertIsNotNone(self.position()["entry"])
        resume_spec = happy_spec()
        resume_spec["position"] = [{"symbol": SYMBOL,
                                    "positionAmt": "-0.098"}]
        self.run_cmd(bp.cmd_resume, resume_spec, run_id="cafe02")
        code, _t, out = self.run_cmd(bp.cmd_check, happy_spec(),
                                     run_id="cafe03", now=T_OPEN + 10000)
        self.assertEqual(code, 0)
        self.assertIn("DECISION: HOLD", out)

    def test_hold_decision_freezes_without_entry_snapshot(self):
        # Direct fail-closed guard: with no entry snapshot the gap triggers
        # cannot be judged — freeze, never fabricate an exit. (Unreachable
        # through the machine's own flow; guarded anyway.)
        position = {"entry": None,
                    "thesis": {"opened_at_ms": T_OPEN,
                               "max_hold_ms": 86400000,
                               "watch_gap_pct": "0.15",
                               "min_remaining_edge_pct": "0.01",
                               "max_adverse_move_pct": "0.5"}}
        decision = bp.hold_decision(position, None, Decimal("0.15"), False,
                                    T_OPEN + 1000)
        self.assertEqual(decision["kind"], "FREEZE_NEW_ACTIONS")
        # The risk-reducing max-hold exit still fires above the freeze.
        overdue = bp.hold_decision(position, None, Decimal("0.15"), False,
                                   T_OPEN + 86400000)
        self.assertEqual(overdue["why"][0], "MAXIMUM_HOLDING_HORIZON")


class MainWiringTests(unittest.TestCase):
    def test_mode_flag_required(self):
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                bp.main([])

    def test_bad_env_blocks_before_any_transport(self):
        old = dict(os.environ)
        try:
            os.environ.pop("QUANT_SHA", None)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = bp.main(["--check", SYMBOL])
            self.assertEqual(code, 2)
            self.assertIn("BLOCKED", buffer.getvalue())
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
