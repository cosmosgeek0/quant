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
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_position as bp  # noqa: E402
import events  # noqa: E402
from basis_position import PositionConfig  # noqa: E402

SPEC_PATH = Path(__file__).resolve().parent / "fixtures" / \
    "old_engine_spec.json"
V1_CONTRACT_DIR = Path(__file__).resolve().parent / "fixtures" / \
    "position_v1_contract"
V1_CONTRACT_PATH = V1_CONTRACT_DIR / "basis-position.json"
V1_CONTRACT_UTC = "2025-08-12T00:00:00Z"
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


# ==========================================================================
# Version-1 persisted-position contract for the version-2 rung transition.
# The fixture is one happy open, check, resume, and close cycle emitted by
# the current writer with a fixed clock. Conditional recovery-only fields
# remain absent, and that absence is part of this canonical happy contract.
# ==========================================================================

class VersionOneContractTests(unittest.TestCase):
    def fixture_position(self):
        position = bp.load_position(str(V1_CONTRACT_DIR))
        self.assertIsNotNone(position)
        return position

    def assert_typed_fields(self, value, expected, path):
        self.assertIs(type(value), dict, path)
        self.assertEqual(set(value), set(expected), path)
        for field, expected_type in expected.items():
            with self.subTest(path="%s.%s" % (path, field)):
                self.assertIs(type(value[field]), expected_type)

    def save_and_reload(self, position):
        with tempfile.TemporaryDirectory() as receipt_dir:
            path = Path(receipt_dir) / bp.STATE_FILE
            path.write_text(json.dumps(position, indent=2), encoding="utf-8")
            loaded = bp.load_position(receipt_dir)
            with mock.patch.object(bp, "utc", return_value=V1_CONTRACT_UTC):
                bp.save_position(receipt_dir, loaded)
            return bp.load_position(receipt_dir)

    def test_v1_contract_happy_cycle_load_save_round_trip_is_byte_stable(self):
        fixture_bytes = V1_CONTRACT_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as receipt_dir:
            state_path = Path(receipt_dir) / bp.STATE_FILE
            state_path.write_bytes(fixture_bytes)
            position = bp.load_position(receipt_dir)
            with mock.patch.object(bp, "utc", return_value=V1_CONTRACT_UTC):
                bp.save_position(receipt_dir, position)
            saved_bytes = state_path.read_bytes()

        revision_before = b'  "revision": 17,'
        revision_after = b'  "revision": 18,'
        self.assertEqual(fixture_bytes.count(revision_before), 1)
        expected_bytes = fixture_bytes.rstrip(b"\n").replace(
            revision_before, revision_after, 1)
        self.assertEqual(saved_bytes, expected_bytes)
        self.assertEqual(position["revision"], 18)
        self.assertEqual(position["updated_utc"], V1_CONTRACT_UTC)

    def test_v1_contract_canonical_happy_cycle_field_types_and_presence(self):
        position = self.fixture_position()
        self.assert_typed_fields(position, {
            "version": int,
            "symbol": str,
            "sha": str,
            "state": str,
            "open_run_id": str,
            "proposal_id": str,
            "proposal_digest": str,
            "thesis": dict,
            "entry": dict,
            "legs": list,
            "client_ids": dict,
            "pending_intent": type(None),
            "revision": int,
            "verdict": str,
            "updated_utc": str,
            "last_check": dict,
            "reconcile": dict,
            "resume_proven": dict,
            "close_run_id": str,
            "close": dict,
        }, "position")
        self.assert_typed_fields(position["thesis"], {
            "opened_at_ms": int,
            "max_hold_ms": int,
            "watch_gap_pct": str,
            "min_remaining_edge_pct": str,
            "max_adverse_move_pct": str,
        }, "position.thesis")
        self.assert_typed_fields(position["entry"], {
            "opened_utc": str,
            "entry_gap_bps": str,
            "entry_gap_pct": str,
            "funding_rate": str,
            "proposal_net_edge_pct": str,
            "spot": dict,
            "shared_step": str,
            "spot_step": str,
            "base_asset": str,
            "hedge_qty": str,
            "short": dict,
        }, "position.entry")
        self.assert_typed_fields(position["entry"]["spot"], {
            "executed_qty": str,
            "quote_spent": str,
            "base_fee": str,
            "net_base": str,
        }, "position.entry.spot")
        self.assert_typed_fields(position["entry"]["short"], {
            "filled_qty": str,
            "cum_quote": str,
        }, "position.entry.short")

        common_leg_fields = {
            "stage": str,
            "client_id": str,
            "market": str,
            "outcome": str,
            "utc": str,
            "status": str,
            "executed_qty": str,
            "quote": str,
        }
        expected_stages = [bp.ENTER_SPOT, bp.ENTER_SHORT,
                           bp.EXIT_SHORT, bp.EXIT_SPOT]
        self.assertEqual([leg["stage"] for leg in position["legs"]],
                         expected_stages)
        for index, leg in enumerate(position["legs"]):
            expected = dict(common_leg_fields)
            if leg["stage"] in (bp.ENTER_SPOT, bp.EXIT_SPOT):
                expected["fee_by_asset"] = dict
            self.assert_typed_fields(
                leg, expected, "position.legs[%d]" % index)
        self.assert_typed_fields(position["legs"][0]["fee_by_asset"], {
            "BTC": str,
        }, "position.legs[0].fee_by_asset")
        self.assert_typed_fields(position["legs"][3]["fee_by_asset"], {
            "USDT": str,
        }, "position.legs[3].fee_by_asset")

        expected_client_ids = {
            "bc-cafe01-enter_spot",
            "bc-cafe01-enter_short",
            "bc-cafe04-exit_short",
            "bc-cafe04-exit_spot",
        }
        self.assertEqual(set(position["client_ids"]), expected_client_ids)
        for client_id, metadata in position["client_ids"].items():
            self.assert_typed_fields(metadata, {
                "stage": str,
                "market": str,
                "run_id": str,
            }, "position.client_ids.%s" % client_id)

        self.assert_typed_fields(position["last_check"], {
            "utc": str,
            "now_ms": int,
            "current_gap_pct": str,
            "exit_fees_pct": str,
            "fees_source": str,
            "decision": str,
            "why": str,
        }, "position.last_check")
        self.assert_typed_fields(position["reconcile"], {
            "utc": str,
            "run_id": str,
            "truths": dict,
            "futures_position": str,
            "base_free": str,
            "recommendations": list,
        }, "position.reconcile")
        self.assert_typed_fields(position["reconcile"]["truths"], {
            "bc-cafe01-enter_short": str,
            "bc-cafe01-enter_spot": str,
        }, "position.reconcile.truths")
        self.assertEqual(position["reconcile"]["recommendations"], [])
        self.assert_typed_fields(position["resume_proven"], {
            "run_id": str,
            "short_qty": str,
            "source": str,
        }, "position.resume_proven")
        self.assert_typed_fields(position["close"], {
            "closed_utc": str,
            "short_closed_qty": str,
            "exit_short_quote": str,
            "spot_sold_qty": str,
            "quote_received": str,
            "pnl": dict,
        }, "position.close")
        self.assert_typed_fields(position["close"]["pnl"], {
            "spot_quote_pnl_usdt": str,
            "short_quote_pnl_usdt": str,
            "spot_fees_usdt": str,
            "usdm_commission_usdt": str,
            "funding_usdt": str,
            "residual_base": str,
            "total_usdt": str,
            "spot_fees_by_asset": dict,
            "total_usdt_status": str,
            "excluded_spot_fees_by_asset": dict,
        }, "position.close.pnl")
        self.assert_typed_fields(
            position["close"]["pnl"]["spot_fees_by_asset"], {
                "BTC": str,
                "USDT": str,
            }, "position.close.pnl.spot_fees_by_asset")
        self.assert_typed_fields(
            position["close"]["pnl"]["excluded_spot_fees_by_asset"], {
                "BTC": str,
            }, "position.close.pnl.excluded_spot_fees_by_asset")

    def test_v1_contract_derive_resume_states_and_verdicts_are_pinned(self):
        position = self.fixture_position()
        closed_truths = {
            leg["client_id"]: (
                "terminal", {"executedQty": leg["executed_qty"]})
            for leg in position["legs"]
        }
        closed = bp.derive_resume(
            position, closed_truths, Decimal("0"), Decimal("0.000001"),
            Decimal(position["entry"]["spot_step"]))
        self.assertEqual(closed, {
            "state": bp.ST_CLOSED,
            "verdict": "CLOSED",
            "note": "every leg terminal, futures flat, no sellable base",
            "exposure": "none",
            "recommendations": [],
        })

        intact_truths = {
            client_id: ("terminal", payload)
            if client_id.startswith("bc-cafe01-") else ("not_found", None)
            for client_id, (_state, payload) in closed_truths.items()
        }
        intact = bp.derive_resume(
            position, intact_truths, Decimal("-0.098"),
            Decimal("0.098901"),
            Decimal(position["entry"]["spot_step"]))
        self.assertEqual(intact, {
            "state": bp.ST_OPEN,
            "verdict": "OPEN",
            "note": "short -0.098 stands on the exchange",
            "exposure": "short -0.098",
            "recommendations": [],
        })

    def test_v1_contract_resume_closed_trail_keeps_closed_state_and_verdict(self):
        fixture_bytes = V1_CONTRACT_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as receipt_dir:
            state_path = Path(receipt_dir) / bp.STATE_FILE
            state_path.write_bytes(fixture_bytes)
            cfg = PositionConfig(
                symbol=SYMBOL, sha=SHA, receipt_dir=receipt_dir,
                run_id="cafe05", max_hold_ms=86400000,
                min_remaining_edge_pct=Decimal("0.01"),
                max_adverse_move_pct=Decimal("0.5"))

            def no_transport(*args, **kwargs):
                self.fail("closed version-1 resume attempted transport")

            output = io.StringIO()
            with redirect_stdout(output):
                code = bp.cmd_resume(
                    cfg, no_transport, sleep=lambda seconds: None,
                    now_ms=lambda: T_OPEN + 120000)
            persisted = bp.load_position(receipt_dir)
            persisted_bytes = state_path.read_bytes()

        self.assertEqual(code, bp.EXIT_CODE["NOTHING_STANDING"])
        self.assertEqual((persisted["state"], persisted["verdict"]),
                         (bp.ST_CLOSED, "CLOSED"))
        self.assertEqual(persisted_bytes, fixture_bytes)
        self.assertIn("state CLOSED | nothing standing", output.getvalue())

    def test_v1_contract_current_behavior_accepts_unknown_future_field(self):
        position = self.fixture_position()
        future_value = {"rung_receipts": [{"rung_id": "future-01"}]}
        position["future_extension"] = future_value
        persisted = self.save_and_reload(position)
        self.assertEqual(persisted["future_extension"], future_value)
        self.assertEqual(persisted["version"], 1)

    def test_v1_contract_current_behavior_accepts_noncurrent_version_marker(self):
        position = self.fixture_position()
        position["version"] = 2
        persisted = self.save_and_reload(position)
        self.assertEqual(persisted["version"], 2)
        self.assertEqual(persisted["state"], bp.ST_CLOSED)


class VersionTwoPersistenceTests(unittest.TestCase):
    CONFIG_SNAPSHOT = {
        "symbol": SYMBOL,
        "direction": bp.V2_DIRECTION,
        "schedule": [
            {"ordinal": 1, "entry_gap_pct": "0.42",
             "quote_cap": "10"},
        ],
        "caps": {"stack_quote": "20", "rung_quote": "10"},
        "venue": {
            "spot": "BINANCE_SPOT",
            "futures": "BINANCE_USDM",
            "base_asset": "BTC",
            "quote_asset": "USDT",
        },
    }
    CONFIG_EPOCH_DIGEST = bp.v2_config_epoch_digest(CONFIG_SNAPSHOT)
    CREATED_CUT_MS = 1755000010000
    EVENT_SOURCE_MS = 1755000004000
    FLAT_READ_PROVENANCE = {
        "positions": {"spot": True, "usdm": True},
        "open_orders": {
            "spot": True,
            "usdm_standard": True,
            "usdm_algorithmic": True,
        },
        "own_orders": [
            {"client_id": "bc-cafe01-enter_spot", "market": "spot",
             "status": "FILLED"},
            {"client_id": "bc-cafe01-enter_short", "market": "usdm",
             "status": "FILLED"},
            {"client_id": "bc-cafe04-exit_short", "market": "usdm",
             "status": "FILLED"},
            {"client_id": "bc-cafe04-exit_spot", "market": "spot",
             "status": "FILLED"},
        ],
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.receipt_dir = self.temp.name
        self.state_path = Path(self.receipt_dir) / bp.STATE_FILE
        self.state_path.write_bytes(V1_CONTRACT_PATH.read_bytes())

    def raw_position(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def write_raw_position(self, position):
        self.state_path.write_text(json.dumps(position, indent=2),
                                   encoding="utf-8")

    def flat_proof(self, source_revision=None, observed_at_ms=None,
                   config_epoch_digest=None, read_provenance=None):
        if source_revision is None:
            source_revision = self.raw_position()["revision"]
        if observed_at_ms is None:
            observed_at_ms = self.CREATED_CUT_MS
        if config_epoch_digest is None:
            config_epoch_digest = self.CONFIG_EPOCH_DIGEST
        if read_provenance is None:
            read_provenance = self.FLAT_READ_PROVENANCE
        return bp.new_v2_flat_proof(
            SYMBOL, config_epoch_digest, source_revision,
            observed_at_ms, Decimal("0"), Decimal("0"), 0, 0, True,
            None, read_provenance)

    def enable_raw(self, config_epoch_digest, flat_proof, revision,
                   config_snapshot, initialization_cut_ms):
        with mock.patch.object(
                bp, "default_now_ms",
                return_value=initialization_cut_ms):
            return bp.enable_v2_position(
                self.receipt_dir, SYMBOL, config_epoch_digest, flat_proof,
                revision, config_snapshot, initialization_cut_ms)

    def enable_v2(self, config_epoch_digest=None, config_snapshot=None,
                  initialization_cut_ms=None):
        revision = self.raw_position()["revision"]
        if config_epoch_digest is None:
            config_epoch_digest = self.CONFIG_EPOCH_DIGEST
        if config_snapshot is None:
            config_snapshot = self.CONFIG_SNAPSHOT
        if initialization_cut_ms is None:
            initialization_cut_ms = self.CREATED_CUT_MS
        return self.enable_raw(
            config_epoch_digest,
            self.flat_proof(revision,
                            config_epoch_digest=config_epoch_digest),
            revision, config_snapshot, initialization_cut_ms)

    def own_orders(self, terminal=False, spot_quote="9.80",
                   short_quote="9.79", suffix="01",
                   spot_quantity="0.098", short_quantity="0.098",
                   terminal_status=None, spot_fee_by_asset=None):
        spot_terminal_quantity = spot_quantity if terminal else None
        short_terminal_quantity = short_quantity if terminal else None
        if terminal_status is None:
            terminal_status = "FILLED" if terminal else "INTENT_SAVED"
        return [
            bp.new_v2_own_order_record(
                SYMBOL, self.CONFIG_EPOCH_DIGEST, bp.ENTER_SPOT, "spot",
                "bc-v2-%s-enter-spot" % suffix, terminal,
                terminal_status, spot_terminal_quantity,
                spot_quote if terminal else None, spot_fee_by_asset or {}),
            bp.new_v2_own_order_record(
                SYMBOL, self.CONFIG_EPOCH_DIGEST, bp.ENTER_SHORT, "usdm",
                "bc-v2-%s-enter-short" % suffix, terminal,
                terminal_status, short_terminal_quantity,
                short_quote if terminal else None, {}),
        ]

    def rung(self, ordinal=1, terminal=False, effective_cap="10",
             spot_quote="9.80", short_quote="9.79", suffix="01",
             spot_quantity="0.098", short_quantity="0.098",
             terminal_status=None, spot_fee_by_asset=None):
        spot_terminal_quantity = spot_quantity if terminal else None
        short_terminal_quantity = short_quantity if terminal else None
        remaining_spot = spot_terminal_quantity
        if terminal and spot_fee_by_asset:
            remaining_spot = str(
                Decimal(spot_quantity)
                - Decimal(spot_fee_by_asset.get("BTC", "0")))
        sizes = bp.new_v2_size_record(
            Decimal("0.098"), Decimal("9.80"), spot_terminal_quantity,
            short_terminal_quantity, remaining_spot,
            short_terminal_quantity)
        return bp.new_v2_rung_record(
            SYMBOL, self.CONFIG_EPOCH_DIGEST, "rung-%s" % suffix, ordinal,
            "ENTRY_PENDING", Decimal("0.42"),
            Decimal(effective_cap), sizes,
            self.own_orders(
                terminal, spot_quote, short_quote, suffix, spot_quantity,
                short_quantity, terminal_status, spot_fee_by_asset),
            None, self.EVENT_SOURCE_MS,
            self.CREATED_CUT_MS, Decimal("0.11"), Decimal("0.0001"),
            28800000)

    def transition(self, expected_revision, transition_id="transition-01",
                   from_state=bp.ST_NONE, to_state=bp.ST_OPENING,
                   evidence_digest=None, evidence_kind=None,
                   receipt_ids=None, amounts=None, created_cut_ms=None):
        if created_cut_ms is None:
            created_cut_ms = self.CREATED_CUT_MS
        if evidence_kind is None \
                and (from_state, to_state) == (bp.ST_NONE, bp.ST_OPENING):
            proof = self.raw_position()["flat_proof"]
            evidence_kind = bp.V2_EVIDENCE_FLAT_ENTRY
            receipt_ids = [proof["evidence_digest"], "proposal-01"]
            amounts = {
                "futures_position_qty": proof["futures_position_qty"],
                "spot_sellable_base_qty":
                    proof["spot_sellable_base_qty"],
                "observed_at_ms": str(proof["observed_at_ms"]),
            }
        elif evidence_kind is None \
                and (from_state, to_state) \
                in ((bp.ST_OPENING, bp.ST_OPEN), ("ADDING", bp.ST_OPEN)):
            rung = self.raw_position()["rungs"][-1]
            by_stage = {row["stage"]: row for row in rung["own_orders"]}
            spot = by_stage[bp.ENTER_SPOT]
            short = by_stage[bp.ENTER_SHORT]
            base_asset = self.CONFIG_SNAPSHOT["venue"]["base_asset"]
            net_spot = Decimal(spot["terminal_executed_qty"]) \
                - Decimal(spot["fee_by_asset"].get(base_asset, "0"))
            evidence_kind = bp.V2_EVIDENCE_TERMINAL_RUNG
            receipt_ids = [
                rung["rung_id"], spot["client_id"], short["client_id"],
            ]
            amounts = {
                "spot_net_base_qty": str(net_spot),
                "short_base_qty": short["terminal_executed_qty"],
            }
        elif evidence_kind is None:
            evidence_kind = bp.V2_EVIDENCE_AMBIGUOUS
            receipt_ids = ["evidence-01"]
            amounts = {}
        return bp.new_v2_transition_record(
            SYMBOL, self.CONFIG_EPOCH_DIGEST, transition_id, from_state,
            to_state, expected_revision, evidence_kind, evidence_digest,
            receipt_ids, created_cut_ms, amounts)

    def authorize_add(self, position, rung):
        from_state = position["state"]
        to_state = bp.ST_OPENING if from_state == bp.ST_NONE else "ADDING"
        revision = position["revision"]
        if from_state == bp.ST_NONE:
            transition = self.transition(
                revision, "add-transition-%d" % revision,
                from_state, to_state)
        else:
            transition = self.transition(
                revision, "add-transition-%d" % revision,
                from_state, to_state, evidence_kind=bp.V2_EVIDENCE_ADD,
                receipt_ids=[rung["rung_id"],
                             "add-plan-%d" % revision],
                amounts={
                    "requested_base_qty":
                        rung["sizes"]["requested_base_qty"],
                    "requested_quote": rung["sizes"]["requested_quote"],
                })
        return bp.write_v2_transition(
            self.receipt_dir, transition, revision)

    def add_rung(self, rung=None):
        record = rung or self.rung()
        position = bp.load_position(self.receipt_dir)
        if position["state"] in (bp.ST_NONE, bp.ST_OPEN):
            position = self.authorize_add(position, record)
        return bp.write_v2_rung(
            self.receipt_dir, record, position["revision"])

    def test_v2_receipts_bind_symbol_and_config_epoch_digest(self):
        self.enable_v2()
        self.add_rung()
        loaded = bp.load_position(self.receipt_dir)

        self.assertEqual(loaded["config_snapshot"], self.CONFIG_SNAPSHOT)
        self.assertEqual(
            bp.v2_config_epoch_digest(loaded["config_snapshot"]),
            self.CONFIG_EPOCH_DIGEST)
        receipts = [loaded["flat_proof"], loaded["rungs"][0],
                    loaded["rungs"][0]["event"],
                    *loaded["rungs"][0]["own_orders"],
                    loaded["transitions"][0]]
        for receipt in receipts:
            with self.subTest(record_type=receipt["record_type"]):
                self.assertEqual(receipt["version"], 2)
                self.assertEqual(receipt["symbol"], SYMBOL)
                self.assertEqual(receipt["config_epoch_digest"],
                                 self.CONFIG_EPOCH_DIGEST)

    def test_v2_own_order_identity_retains_terminal_quantities_and_quotes_for_every_leg(self):
        self.enable_v2()
        self.add_rung(self.rung(terminal=True))
        orders = bp.load_position(self.receipt_dir)["rungs"][0]["own_orders"]
        expected = {
            bp.ENTER_SPOT: ("bc-v2-01-enter-spot", "0.098", "9.8"),
            bp.ENTER_SHORT: ("bc-v2-01-enter-short", "0.098", "9.79"),
        }
        self.assertEqual(set(expected), {order["stage"] for order in orders})
        for order in orders:
            self.assertTrue(order["terminal"])
            self.assertEqual(order["fee_by_asset"], {})
            self.assertEqual(
                (order["client_id"], order["terminal_executed_qty"],
                 order["terminal_quote"]), expected[order["stage"]])

    def test_v2_rung_creation_seals_trigger_event_digest_and_economics(self):
        self.enable_v2()
        self.add_rung()
        rung = bp.load_position(self.receipt_dir)["rungs"][0]
        self.assertEqual(rung["entry_gap_pct"], "0.42")
        self.assertEqual(rung["effective_cap_quote"], "10")
        self.assertEqual(rung["created_cut_ms"], self.CREATED_CUT_MS)
        self.assertEqual(rung["event"], {
            "version": 2,
            "record_type": "TRIGGER_EVENT",
            "symbol": SYMBOL,
            "config_epoch_digest": self.CONFIG_EPOCH_DIGEST,
            "digest": rung["event"]["digest"],
            "source_time_ms": self.EVENT_SOURCE_MS,
            "age_at_creation_ms": 6000,
            "entry_gap_pct": "0.42",
            "exit_gap_pct": "0.11",
            "funding_rate": "0.0001",
            "funding_period_ms": 28800000,
        })
        self.assertEqual(bp.v2_event_digest(rung["event"]),
                         rung["event"]["digest"])

    def test_v2_effective_cap_reconcile_excess_freezes_with_reduce_only_path(self):
        self.enable_v2()
        rung = self.rung(terminal=True, effective_cap="10",
                         spot_quote="10.01", short_quote="9.99")
        position = self.add_rung(rung)
        position = bp.reconcile_v2_rung_cap(
            self.receipt_dir, "rung-01", position["revision"])
        cap = position["rungs"][0]["cap_reconcile"]
        self.assertEqual(cap, {
            "status": "EXCEEDED",
            "terminal_fill_quote": "10.01",
            "freeze_increases": True,
            "reduce_only_path": True,
        })
        self.assertEqual(position["rungs"][0]["status"], "REDUCE_ONLY")
        self.assertTrue(position["increases_frozen"])
        self.assertTrue(position["reduce_only_path"])

    def test_v2_effective_cap_reconcile_within_cap_clears_pending_freeze(self):
        self.enable_v2()
        position = self.add_rung(self.rung(terminal=True))
        self.assertTrue(position["increases_frozen"])
        position = bp.reconcile_v2_rung_cap(
            self.receipt_dir, "rung-01", position["revision"])
        self.assertEqual(position["rungs"][0]["cap_reconcile"]["status"],
                         "WITHIN_CAP")
        self.assertFalse(position["increases_frozen"])
        self.assertFalse(position["reduce_only_path"])

    def test_v2_transition_compare_and_swap_refuses_stale_revision(self):
        position = self.enable_v2()
        stale_revision = position["revision"]
        first = self.transition(stale_revision)
        position = bp.write_v2_transition(
            self.receipt_dir, first, stale_revision)
        replay = self.transition(stale_revision, "transition-replay")
        with self.assertRaisesRegex(bp.PositionError, "stale"):
            bp.write_v2_transition(
                self.receipt_dir, replay, stale_revision)
        persisted = bp.load_position(self.receipt_dir)
        self.assertEqual(persisted["revision"], position["revision"])
        self.assertEqual([row["transition_id"]
                          for row in persisted["transitions"]],
                         ["transition-01"])

    def test_v2_transition_receipt_seals_states_revision_and_evidence(self):
        position = self.enable_v2()
        expected_revision = position["revision"]
        transition = self.transition(expected_revision)
        position = bp.write_v2_transition(
            self.receipt_dir, transition, expected_revision)
        receipt = position["transitions"][0]
        self.assertEqual((receipt["from"], receipt["to"]),
                         (bp.ST_NONE, bp.ST_OPENING))
        self.assertEqual(receipt["expected_revision"], expected_revision)
        self.assertEqual(receipt["evidence"], transition["evidence"])
        self.assertEqual(receipt["evidence"]["kind"],
                         bp.V2_EVIDENCE_FLAT_ENTRY)
        self.assertEqual(
            receipt["evidence"]["digest"],
            bp.v2_evidence_digest(
                receipt["evidence"]["kind"],
                receipt["evidence"]["receipt_ids"],
                receipt["evidence"]["amounts"]))
        self.assertEqual(position["revision"], expected_revision + 1)

    def test_v2_event_age_uses_sealed_creation_cut_not_current_clock(self):
        self.enable_v2()
        self.add_rung()
        rung = bp.load_position(self.receipt_dir)["rungs"][0]
        with mock.patch.object(bp.time, "time", return_value=9999999999):
            self.assertEqual(bp.v2_event_age_at_creation_ms(rung), 6000)

    def test_v2_strict_loader_refuses_unknown_fields_recursively_while_v1_is_permissive(self):
        legacy = self.raw_position()
        legacy["future_extension"] = {"accepted": True}
        self.write_raw_position(legacy)
        self.assertEqual(bp.load_position(self.receipt_dir)["future_extension"],
                         {"accepted": True})

        revision = legacy["revision"]
        proof = self.flat_proof(revision)
        position = self.enable_raw(
            self.CONFIG_EPOCH_DIGEST, proof, revision,
            self.CONFIG_SNAPSHOT, self.CREATED_CUT_MS)
        position = self.add_rung()
        valid = self.raw_position()
        mutations = (
            lambda value: value.__setitem__("future_extension", {}),
            lambda value: value["rungs"][0].__setitem__("future", {}),
            lambda value: value["rungs"][0]["event"].__setitem__(
                "future", {}),
        )
        for mutation in mutations:
            invalid = json.loads(json.dumps(valid))
            mutation(invalid)
            self.write_raw_position(invalid)
            with self.assertRaisesRegex(bp.PositionError, "unknown field"):
                bp.load_position(self.receipt_dir)
        self.write_raw_position(valid)

    def test_v2_strict_loader_refuses_malformed_decimal_text(self):
        self.enable_v2()
        self.add_rung()
        invalid = self.raw_position()
        invalid["rungs"][0]["entry_gap_pct"] = "not-a-decimal"
        self.write_raw_position(invalid)
        with self.assertRaisesRegex(bp.PositionError, "Decimal text"):
            bp.load_position(self.receipt_dir)

    def test_v2_schema_version_marker_gates_strict_loading(self):
        legacy = self.raw_position()
        legacy["version"] = 99
        self.write_raw_position(legacy)
        self.assertEqual(bp.load_position(self.receipt_dir)["version"], 99)

        marked = self.raw_position()
        marked["schema_version"] = 3
        self.write_raw_position(marked)
        with self.assertRaisesRegex(bp.PositionError,
                                    "schema_version is not supported"):
            bp.load_position(self.receipt_dir)

    def test_v2_strict_loader_refuses_duplicate_rung_ordinals(self):
        self.enable_v2()
        position = self.add_rung(self.rung(terminal=True))
        position = bp.reconcile_v2_rung_cap(
            self.receipt_dir, "rung-01", position["revision"])
        invalid = self.raw_position()
        duplicate = json.loads(json.dumps(invalid["rungs"][0]))
        duplicate["rung_id"] = "rung-02"
        for order in duplicate["own_orders"]:
            order["client_id"] += "-02"
        invalid["rungs"].append(duplicate)
        self.write_raw_position(invalid)
        with self.assertRaisesRegex(bp.PositionError, "ordinal is duplicated"):
            bp.load_position(self.receipt_dir)

    def test_v2_strict_loader_refuses_config_epoch_drift(self):
        self.enable_v2()
        self.add_rung()
        invalid = self.raw_position()
        invalid["rungs"][0]["config_epoch_digest"] = "e" * 64
        self.write_raw_position(invalid)
        with self.assertRaisesRegex(bp.PositionError,
                                    "config-epoch binding"):
            bp.load_position(self.receipt_dir)

    def test_v2_deleted_schema_marker_refuses_legacy_fallback(self):
        self.enable_v2()
        invalid = self.raw_position()
        del invalid["schema_version"]
        self.write_raw_position(invalid)
        with self.assertRaisesRegex(bp.PositionError, "schema_version"):
            bp.load_position(self.receipt_dir)

    def test_v2_supplied_config_digest_must_match_canonical_snapshot(self):
        revision = self.raw_position()["revision"]
        supplied = "a" * 64
        self.assertNotEqual(supplied, self.CONFIG_EPOCH_DIGEST)
        with self.assertRaisesRegex(bp.PositionError,
                                    "config snapshot digest"):
            self.enable_raw(
                supplied,
                self.flat_proof(
                    revision, config_epoch_digest=supplied),
                revision, self.CONFIG_SNAPSHOT, self.CREATED_CUT_MS)

    def test_v2_stored_config_snapshot_edit_refuses_on_load(self):
        self.enable_v2()
        invalid = self.raw_position()
        invalid["config_snapshot"]["caps"]["stack_quote"] = "21"
        self.write_raw_position(invalid)
        with self.assertRaisesRegex(bp.PositionError,
                                    "config snapshot digest"):
            bp.load_position(self.receipt_dir)

    def test_v2_transition_table_refuses_illegal_edge(self):
        position = self.enable_v2()
        with self.assertRaisesRegex(bp.PositionError, "legal"):
            self.transition(position["revision"], from_state=bp.ST_NONE,
                            to_state=bp.ST_CLOSED)

    def test_v2_transition_evidence_digest_is_consumed_once(self):
        position = self.enable_v2()
        first = self.transition(position["revision"])
        position = bp.write_v2_transition(
            self.receipt_dir, first, position["revision"])
        with self.assertRaisesRegex(bp.PositionError,
                                    "supplied evidence digest"):
            self.transition(
                position["revision"], "transition-02", bp.ST_OPENING,
                bp.ST_UNKNOWN, first["evidence"]["digest"],
                bp.V2_EVIDENCE_AMBIGUOUS, ["ambiguous-01"], {})

    def test_v2_rung_append_requires_recorded_add_authorization(self):
        position = self.enable_v2()
        with self.assertRaisesRegex(bp.PositionError,
                                    "ADD transition"):
            bp.write_v2_rung(
                self.receipt_dir, self.rung(), position["revision"])

        invalid = self.raw_position()
        pre_reconciled = self.rung(terminal=True)
        pre_reconciled["status"] = "OPEN"
        invalid["rungs"].append(pre_reconciled)
        self.write_raw_position(invalid)
        with self.assertRaisesRegex(bp.PositionError,
                                    "ADD transition|cap reconciliation"):
            bp.load_position(self.receipt_dir)

    def test_v2_event_funding_edit_refuses_digest_recheck(self):
        self.enable_v2()
        self.add_rung()
        invalid = self.raw_position()
        invalid["rungs"][0]["event"]["funding_rate"] = "0.9"
        self.write_raw_position(invalid)
        with self.assertRaisesRegex(bp.PositionError,
                                    "event digest"):
            bp.load_position(self.receipt_dir)

    def test_v2_intent_saved_orders_keep_cap_safety_flags(self):
        self.enable_v2()
        position = self.add_rung(self.rung(
            terminal=True, terminal_status="INTENT_SAVED"))
        position = bp.reconcile_v2_rung_cap(
            self.receipt_dir, "rung-01", position["revision"])
        self.assertEqual(position["rungs"][0]["cap_reconcile"]["status"],
                         "UNKNOWN")
        self.assertTrue(position["increases_frozen"])
        self.assertTrue(position["reduce_only_path"])

    def test_v2_base_asset_fee_enters_terminal_exposure_agreement(self):
        self.enable_v2()
        rung = self.rung(
            terminal=True, spot_quantity="0.099", short_quantity="0.098",
            spot_fee_by_asset={"BTC": "0.001"})
        position = self.add_rung(rung)
        position = bp.reconcile_v2_rung_cap(
            self.receipt_dir, "rung-01", position["revision"])
        self.assertEqual(position["rungs"][0]["cap_reconcile"]["status"],
                         "WITHIN_CAP")
        self.assertFalse(position["increases_frozen"])
        self.assertFalse(position["reduce_only_path"])

    def test_v2_terminal_quantity_disagreement_keeps_safety_flags(self):
        self.enable_v2()
        rung = self.rung(
            terminal=True, spot_quantity="0.099", short_quantity="0.098")
        position = self.add_rung(rung)
        position = bp.reconcile_v2_rung_cap(
            self.receipt_dir, "rung-01", position["revision"])
        self.assertEqual(position["rungs"][0]["cap_reconcile"]["status"],
                         "UNKNOWN")
        self.assertTrue(position["increases_frozen"])
        self.assertTrue(position["reduce_only_path"])

    def test_v2_stale_flat_proof_refuses_initialization(self):
        revision = self.raw_position()["revision"]
        proof = self.flat_proof(
            revision, self.CREATED_CUT_MS - 30001)
        with self.assertRaisesRegex(bp.PositionError, "freshness"):
            self.enable_raw(
                self.CONFIG_EPOCH_DIGEST, proof, revision,
                self.CONFIG_SNAPSHOT, self.CREATED_CUT_MS)

    def test_v2_working_legacy_leg_refuses_initialization(self):
        legacy = self.raw_position()
        legacy["legs"][0]["status"] = "WORKING"
        legacy["legs"][0]["outcome"] = "working"
        self.write_raw_position(legacy)
        revision = legacy["revision"]
        with self.assertRaisesRegex(bp.PositionError,
                                    "legacy leg is not terminal"):
            self.enable_raw(
                self.CONFIG_EPOCH_DIGEST, self.flat_proof(revision),
                revision, self.CONFIG_SNAPSHOT, self.CREATED_CUT_MS)

    def test_v2_missing_flat_read_provenance_refuses_initialization(self):
        revision = self.raw_position()["revision"]
        proof = self.flat_proof(revision)
        del proof["read_provenance"]
        with self.assertRaisesRegex(bp.PositionError, "read_provenance"):
            self.enable_raw(
                self.CONFIG_EPOCH_DIGEST, proof, revision,
                self.CONFIG_SNAPSHOT, self.CREATED_CUT_MS)

    def test_v2_flat_proof_must_enumerate_every_legacy_own_order(self):
        revision = self.raw_position()["revision"]
        provenance = json.loads(json.dumps(self.FLAT_READ_PROVENANCE))
        provenance["own_orders"].pop()
        proof = self.flat_proof(revision, read_provenance=provenance)
        with self.assertRaisesRegex(bp.PositionError,
                                    "own-order enumeration"):
            self.enable_raw(
                self.CONFIG_EPOCH_DIGEST, proof, revision,
                self.CONFIG_SNAPSHOT, self.CREATED_CUT_MS)

    def test_v2_flat_proof_own_order_provenance_requires_terminal_status(self):
        provenance = json.loads(json.dumps(self.FLAT_READ_PROVENANCE))
        provenance["own_orders"] = [{
            "client_id": "bc-v2-prior-order",
            "market": "spot",
            "status": "INTENT_SAVED",
        }]
        with self.assertRaisesRegex(bp.PositionError,
                                    "terminal status"):
            self.flat_proof(read_provenance=provenance)

    def test_br2_finding_1_nested_v2_fields_select_strict_schema(self):
        mutations = (
            lambda value: value.__setitem__(
                "future_extension", {
                    "payload": {"config_epoch_digest": "a" * 64},
                }),
            lambda value: value.__setitem__(
                "future_extension", {"rung_id": "rung-hidden"}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.state_path.write_bytes(V1_CONTRACT_PATH.read_bytes())
                legacy = self.raw_position()
                mutation(legacy)
                self.write_raw_position(legacy)
                with self.assertRaisesRegex(bp.PositionError,
                                            "schema_version"):
                    bp.load_position(self.receipt_dir)

    def test_br2_finding_2_config_epoch_uses_typed_canonical_schema(self):
        equivalent = json.loads(json.dumps(self.CONFIG_SNAPSHOT))
        equivalent["caps"]["rung_quote"] = "10.0"
        self.assertEqual(
            bp.v2_config_epoch_digest(self.CONFIG_SNAPSHOT),
            bp.v2_config_epoch_digest(equivalent))
        self.enable_v2(config_snapshot=equivalent)
        self.assertEqual(
            self.raw_position()["config_snapshot"]["caps"]["rung_quote"],
            "10")

        invalid_snapshots = []
        hollow = json.loads(json.dumps(self.CONFIG_SNAPSHOT))
        hollow["schedule"] = [{}]
        invalid_snapshots.append(hollow)
        hollow = json.loads(json.dumps(self.CONFIG_SNAPSHOT))
        hollow["caps"] = {"optional": None}
        invalid_snapshots.append(hollow)
        hollow = json.loads(json.dumps(self.CONFIG_SNAPSHOT))
        hollow["venue"] = {"base_asset": "BTC"}
        invalid_snapshots.append(hollow)
        wrong_venue = json.loads(json.dumps(self.CONFIG_SNAPSHOT))
        wrong_venue["venue"]["spot"] = "CALLER_SPOT"
        invalid_snapshots.append(wrong_venue)
        for snapshot in invalid_snapshots:
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(bp.PositionError):
                    bp.v2_config_epoch_digest(snapshot)

    def test_br2_finding_3_state_graph_requires_transition_evidence(self):
        position = self.enable_v2()
        position = bp.write_v2_transition(
            self.receipt_dir, self.transition(position["revision"]),
            position["revision"])
        no_rung_open = self.transition(
            position["revision"], "zero-rung-open", bp.ST_OPENING,
            bp.ST_OPEN, evidence_kind=bp.V2_EVIDENCE_TERMINAL_RUNG,
            receipt_ids=["missing-rung", "missing-short", "missing-spot"],
            amounts={"spot_net_base_qty": "1", "short_base_qty": "1"})
        with self.assertRaisesRegex(bp.PositionError, "requires a rung"):
            bp.write_v2_transition(
                self.receipt_dir, no_rung_open, position["revision"])

        self.state_path.write_bytes(V1_CONTRACT_PATH.read_bytes())
        self.enable_v2()
        position = self.add_rung(self.rung(terminal=True))
        position = bp.reconcile_v2_rung_cap(
            self.receipt_dir, "rung-01", position["revision"])
        position = bp.write_v2_transition(
            self.receipt_dir,
            self.transition(
                position["revision"], "terminal-open", bp.ST_OPENING,
                bp.ST_OPEN),
            position["revision"])
        with self.assertRaisesRegex(bp.PositionError, "evidence kind"):
            self.transition(
                position["revision"], "missing-reduce", bp.ST_OPEN,
                "REDUCING")
        reducing = self.transition(
            position["revision"], "reduce-01", bp.ST_OPEN, "REDUCING",
            evidence_kind=bp.V2_EVIDENCE_REDUCE,
            receipt_ids=["reduce-receipt-01"],
            amounts={"reduced_base_qty": "0.01"})
        position = bp.write_v2_transition(
            self.receipt_dir, reducing, position["revision"])
        stale_flat = self.transition(
            position["revision"], "flat-close-stale", "REDUCING",
            bp.ST_CLOSED, evidence_kind=bp.V2_EVIDENCE_FLAT_CLOSE,
            receipt_ids=["flat-close-stale-receipt"], amounts={
                "futures_position_qty": "0",
                "spot_sellable_base_qty": "0",
                "observed_at_ms": str(self.CREATED_CUT_MS - 30001),
            })
        with mock.patch.object(
                bp, "default_now_ms", return_value=self.CREATED_CUT_MS):
            with self.assertRaisesRegex(bp.PositionError, "not fresh"):
                bp.write_v2_transition(
                    self.receipt_dir, stale_flat, position["revision"])
        fresh_flat = self.transition(
            position["revision"], "flat-close-fresh", "REDUCING",
            bp.ST_CLOSED, evidence_kind=bp.V2_EVIDENCE_FLAT_CLOSE,
            receipt_ids=["flat-close-fresh-receipt"], amounts={
                "futures_position_qty": "0",
                "spot_sellable_base_qty": "0",
                "observed_at_ms": str(self.CREATED_CUT_MS),
            })
        with mock.patch.object(
                bp, "default_now_ms", return_value=self.CREATED_CUT_MS):
            position = bp.write_v2_transition(
                self.receipt_dir, fresh_flat, position["revision"])
        self.assertEqual(position["state"], bp.ST_CLOSED)

    def test_br2_finding_4_evidence_receipts_cannot_change_kind(self):
        position = self.enable_v2()
        first = self.transition(position["revision"])
        position = bp.write_v2_transition(
            self.receipt_dir, first, position["revision"])
        changed_kind = self.transition(
            position["revision"], "same-receipts-new-kind",
            bp.ST_OPENING, bp.ST_UNKNOWN,
            evidence_kind=bp.V2_EVIDENCE_AMBIGUOUS,
            receipt_ids=list(first["evidence"]["receipt_ids"]), amounts={})
        self.assertNotEqual(first["evidence"]["digest"],
                            changed_kind["evidence"]["digest"])
        with self.assertRaisesRegex(bp.PositionError, "receipt is reused"):
            bp.write_v2_transition(
                self.receipt_dir, changed_kind, position["revision"])

    def test_br2_finding_5_event_decimal_equivalence_is_deliberate(self):
        event = self.rung()["event"]
        equivalent = json.loads(json.dumps(event))
        equivalent["funding_rate"] = "1E-4"
        self.assertEqual(bp.v2_event_digest(event),
                         bp.v2_event_digest(equivalent))

    def test_br2_finding_6_zero_or_fee_consumed_fills_do_not_open(self):
        cases = (
            {
                "terminal_status": "REJECTED",
                "spot_quantity": "0", "short_quantity": "0",
                "spot_quote": "0", "short_quote": "0",
            },
            {
                "terminal_status": "FILLED",
                "spot_quantity": "0.098", "short_quantity": "0",
                "spot_quote": "9.8", "short_quote": "0",
                "spot_fee_by_asset": {"BTC": "0.098"},
            },
        )
        for values in cases:
            with self.subTest(values=values):
                self.state_path.write_bytes(V1_CONTRACT_PATH.read_bytes())
                self.enable_v2()
                position = self.add_rung(self.rung(terminal=True, **values))
                position = bp.reconcile_v2_rung_cap(
                    self.receipt_dir, "rung-01", position["revision"])
                self.assertEqual(position["rungs"][0]["status"], "UNKNOWN")
                self.assertEqual(
                    position["rungs"][0]["cap_reconcile"]["status"],
                    "UNKNOWN")
                self.assertTrue(position["increases_frozen"])

    def test_br2_finding_7_flat_gate_uses_module_time_and_bound_digest(self):
        revision = self.raw_position()["revision"]
        historical = self.flat_proof(revision, observed_at_ms=1)
        with mock.patch.object(
                bp, "default_now_ms", return_value=self.CREATED_CUT_MS):
            with self.assertRaisesRegex(bp.PositionError,
                                        "module gate time"):
                bp.enable_v2_position(
                    self.receipt_dir, SYMBOL, self.CONFIG_EPOCH_DIGEST,
                    historical, revision, self.CONFIG_SNAPSHOT, 1)

        stale = self.flat_proof(
            revision, observed_at_ms=self.CREATED_CUT_MS - 30001)
        original_digest = stale["evidence_digest"]
        stale["observed_at_ms"] = self.CREATED_CUT_MS
        self.assertNotEqual(
            original_digest, bp.v2_flat_proof_evidence_digest(stale))
        with self.assertRaisesRegex(bp.PositionError, "evidence digest"):
            self.enable_raw(
                self.CONFIG_EPOCH_DIGEST, stale, revision,
                self.CONFIG_SNAPSHOT, self.CREATED_CUT_MS)

    def test_v2_requested_rung_size_is_immutable_after_creation(self):
        self.enable_v2()
        position = self.add_rung()
        expected_revision = position["revision"]
        position["rungs"][0]["sizes"]["requested_quote"] = "9.7"
        with self.assertRaisesRegex(bp.PositionError,
                                    "requested rung size is immutable"):
            bp.save_position(
                self.receipt_dir, position,
                expected_revision=expected_revision)

    def test_v2_enablement_requires_current_proven_flat_state(self):
        legacy = self.raw_position()
        legacy["state"] = bp.ST_OPEN
        legacy["verdict"] = "OPEN_EXPOSURE"
        self.write_raw_position(legacy)
        with self.assertRaisesRegex(bp.PositionError, "proven flat"):
            self.enable_raw(
                self.CONFIG_EPOCH_DIGEST,
                self.flat_proof(legacy["revision"]), legacy["revision"],
                self.CONFIG_SNAPSHOT, self.CREATED_CUT_MS)
        persisted = self.raw_position()
        self.assertNotIn("schema_version", persisted)
        self.assertEqual(persisted["state"], bp.ST_OPEN)

    def test_v2_writes_use_atomic_state_path_without_residue(self):
        self.enable_v2()
        self.add_rung()
        self.assertTrue(self.state_path.is_file())
        self.assertFalse(Path(str(self.state_path) + ".tmp").exists())
        self.assertFalse(
            (Path(self.receipt_dir) / bp.STATE_WRITE_GUARD_FILE).exists())


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
