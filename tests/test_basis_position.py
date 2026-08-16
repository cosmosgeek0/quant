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

    def test_br5_v1_round_trip_has_no_v2_head_sidecar(self):
        fixture_bytes = V1_CONTRACT_PATH.read_bytes()
        fixture_head = Path(str(V1_CONTRACT_PATH) + ".head")
        self.assertFalse(fixture_head.exists())
        with tempfile.TemporaryDirectory() as receipt_dir:
            state_path = Path(receipt_dir) / bp.STATE_FILE
            head_path = Path(str(state_path) + ".head")
            state_path.write_bytes(fixture_bytes)
            position = bp.load_position(receipt_dir)
            with mock.patch.object(bp, "utc", return_value=V1_CONTRACT_UTC):
                bp.save_position(receipt_dir, position)
            self.assertFalse(head_path.exists())
            self.assertEqual(bp.load_position(receipt_dir)["version"], 1)
        self.assertFalse(fixture_head.exists())

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
    MODULE_TIME_MS = 1755000010000
    SOURCE_TIME_MS = 1755000004000
    CONFIG_SNAPSHOT = {
        "symbol": SYMBOL,
        "direction": bp.V2_DIRECTION,
        "schedule": [
            {"ordinal": 1, "entry_gap_pct": "0.42",
             "quote_cap": "10"},
            {"ordinal": 2, "entry_gap_pct": "0.55",
             "quote_cap": "8"},
        ],
        "caps": {"stack_quote": "20", "entry_quote": "10"},
        "venue": {
            "spot": "BINANCE_SPOT",
            "futures": "BINANCE_USDM",
            "base_asset": "BTC",
            "quote_asset": "USDT",
        },
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.receipt_dir = self.temp.name
        self.state_path = Path(self.receipt_dir) / bp.STATE_FILE
        self.head_path = Path(str(self.state_path) + ".head")

    def current(self):
        return bp.load_position(self.receipt_dir)

    def length(self):
        position = self.current()
        return 0 if position is None else len(position["ledger"])

    def append(self, payload, expected_length=None):
        if expected_length is None:
            expected_length = self.length()
        return bp.append_v2_event(
            self.receipt_dir, payload, expected_length)

    def initialize(self, config=None):
        return bp.initialize_v2_position(
            self.receipt_dir, config or self.CONFIG_SNAPSHOT)

    def flat_facts(self):
        return {
            "futures_position_quantity": "0",
            "spot_sellable_base_quantity": "0",
            "standard_open_order_count": 0,
            "algorithmic_open_order_count": 0,
            "own_orders_terminal": True,
            "positions_digest": "a" * 64,
            "open_orders_digest": "b" * 64,
            "own_orders_digest": "c" * 64,
        }

    def observe_flat(self, receipt_id, observed_at_ms=None):
        if observed_at_ms is None:
            observed_at_ms = self.MODULE_TIME_MS - 100
        return self.append(bp.new_v2_receipt_observed_event(
            receipt_id, "FLAT", observed_at_ms,
            "private-read-bundle", self.flat_facts()))

    def prove_flat(self, receipt_id, module_time_ms=None):
        if module_time_ms is None:
            module_time_ms = self.MODULE_TIME_MS
        with mock.patch.object(
                bp, "default_now_ms", return_value=module_time_ms):
            return self.append(bp.new_v2_flat_proved_event(receipt_id))

    def ready_at(self, receipt_dir, config=None, receipt_id="flat-initial"):
        bp.initialize_v2_position(
            receipt_dir, config or self.CONFIG_SNAPSHOT)
        position = bp.append_v2_event(
            receipt_dir, bp.new_v2_receipt_observed_event(
                receipt_id, "FLAT", self.MODULE_TIME_MS - 100,
                "private-read-bundle", self.flat_facts()), 1)
        with mock.patch.object(
                bp, "default_now_ms", return_value=self.MODULE_TIME_MS):
            return bp.append_v2_event(
                receipt_dir, bp.new_v2_flat_proved_event(receipt_id),
                len(position["ledger"]))

    def ready(self, config=None):
        return self.ready_at(self.receipt_dir, config)

    def leg(self, role, ordinal, quantity, quote, fees=None,
            status="FILLED"):
        if role == "spot":
            stage = bp.ENTER_SPOT
            market = "BINANCE_SPOT"
        else:
            stage = bp.ENTER_SHORT
            market = "BINANCE_USDM"
        return {
            "stage": stage,
            "market": market,
            "client_order_id": "own-%d-%s" % (ordinal, role),
            "exchange_order_id": "exchange-%d-%s" % (ordinal, role),
            "terminal_status": status,
            "executed_quantity": quantity,
            "executed_quote": quote,
            "fee_by_asset": fees or {},
        }

    def filled(self, ordinal, spot_quantity="0.1",
               short_quantity="0.1", spot_quote=None,
               short_quote=None, spot_fees=None, entry_gap=None,
               source_time_ms=None, cut_ms=None):
        if spot_quote is None:
            spot_quote = "9" if ordinal == 1 else "7.5"
        if short_quote is None:
            short_quote = "8.9" if ordinal == 1 else "7.4"
        if entry_gap is None:
            entry_gap = "0.5" if ordinal == 1 else "0.6"
        if source_time_ms is None:
            source_time_ms = self.SOURCE_TIME_MS + ordinal
        if cut_ms is None:
            cut_ms = self.MODULE_TIME_MS + ordinal
        dislocation = bp.new_v2_dislocation_event(
            "dislocation-%d" % ordinal, source_time_ms,
            entry_gap, "0.1", "0.0001", 28800000)
        return bp.new_v2_rung_filled_event(
            ordinal, {
                "spot": self.leg(
                    "spot", ordinal, spot_quantity, spot_quote,
                    spot_fees),
                "short": self.leg(
                    "short", ordinal, short_quantity, short_quote),
            }, dislocation, cut_ms)

    def observe_reduction(self, receipt_id, ordinal,
                          spot_quantity, short_quantity):
        facts = {
            "ordinal": ordinal,
            "spot_quantity": spot_quantity,
            "short_quantity": short_quantity,
        }
        observed_at_ms = self.current()["ledger"][-1]["cut_ms"] + 1
        return self.append(bp.new_v2_receipt_observed_event(
            receipt_id, "REDUCTION", observed_at_ms,
            "own-reduce-receipt", facts))

    def reduce(self, receipt_id, ordinal,
               spot_quantity, short_quantity):
        self.observe_reduction(
            receipt_id, ordinal, spot_quantity, short_quantity)
        return self.append(bp.new_v2_rung_reduced_event(
            ordinal, [receipt_id], {
                "spot_quantity": spot_quantity,
                "short_quantity": short_quantity,
            }))

    def close_proof(self):
        observed = self.MODULE_TIME_MS + 900
        self.observe_flat("flat-final", observed)
        return self.prove_flat(
            "flat-final", self.MODULE_TIME_MS + 1000)

    def raw_position(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def write_raw_position(self, position):
        self.state_path.write_text(
            json.dumps(position, indent=2), encoding="utf-8")

    def write_raw_head(self, position):
        self.head_path.write_text(json.dumps({
            "event_count": len(position["ledger"]),
            "last_event_digest": position["ledger"][-1]["event_digest"],
        }, indent=2), encoding="utf-8")

    def test_br3_finding_1_second_rung_is_structurally_next_event(self):
        self.ready()
        self.append(self.filled(1))
        position = self.append(self.filled(2))
        derived = bp.derive_v2_state(position)
        self.assertEqual(
            [rung["ordinal"] for rung in derived["rungs"]], [1, 2])
        self.assertEqual(derived["top_state"], bp.ST_OPEN)
        self.assertEqual(
            [event["event_type"] for event in position["ledger"][-2:]],
            ["RungFilled", "RungFilled"])

    def test_br3_finding_2_cap_is_derived_from_row_headroom_and_entry_cap(self):
        config = {
            "symbol": SYMBOL,
            "direction": bp.V2_DIRECTION,
            "schedule": [
                {"ordinal": 1, "entry_gap_pct": "0.4",
                 "quote_cap": "7"},
                {"ordinal": 2, "entry_gap_pct": "0.5",
                 "quote_cap": "7"},
            ],
            "caps": {"stack_quote": "8", "entry_quote": "6"},
            "venue": dict(self.CONFIG_SNAPSHOT["venue"]),
        }
        self.ready(config)
        self.append(self.filled(
            1, spot_quote="4", short_quote="3.9", entry_gap="0.5"))
        position = self.append(self.filled(
            2, spot_quote="5", short_quote="4.9", entry_gap="0.6"))
        event = position["ledger"][-1]
        self.assertNotIn("cap", event)
        self.assertNotIn("effective_cap", event)
        derived = bp.derive_v2_state(position)
        self.assertEqual(
            derived["rungs"][1]["effective_cap_quote"], "4")
        self.assertTrue(derived["rungs"][1]["cap_exceeded"])
        self.assertEqual(derived["top_state"], "FROZEN")
        self.assertTrue(derived["reduce_only"])

    def test_br3_finding_3_decimal_quote_and_precision_edges_refuse(self):
        self.ready()
        zero_quote = {
            "spot": self.leg("spot", 1, "0.1", "0"),
            "short": self.leg("short", 1, "0.1", "9"),
        }
        dislocation = bp.new_v2_dislocation_event(
            "value-edge", self.SOURCE_TIME_MS,
            "0.5", "0.1", "0.0001", 28800000)
        with self.assertRaisesRegex(bp.PositionError, "bounds"):
            bp.new_v2_rung_filled_event(
                1, zero_quote, dislocation, self.MODULE_TIME_MS)

        too_precise = "1.00000000000000000000000000004"
        with self.assertRaisesRegex(
                bp.PositionError, "28 significant digits"):
            self.filled(
                1, spot_quantity=too_precise, short_quantity="1")

        boundary = "1.000000000000000000000000001"
        position = self.append(self.filled(
            1, spot_quantity=boundary, short_quantity="1",
            spot_fees={"BTC": "0.000000000000000000000000001"}))
        derived = bp.derive_v2_state(position)
        self.assertEqual(
            derived["rungs"][0]["spot_net_filled_quantity"], "1")
        self.assertEqual(derived["top_state"], bp.ST_OPEN)

        invalid = self.raw_position()
        config_event = invalid["ledger"][0]
        config_event["caps"]["entry_quote"] = "10.0"
        config_event["event_digest"] = bp.v2_event_digest(config_event)
        self.write_raw_position(invalid)
        with self.assertRaisesRegex(bp.PositionError, "canonical"):
            bp.load_position(self.receipt_dir)

    def test_br3_finding_4_top_and_rungs_are_not_persisted(self):
        self.ready()
        position = self.append(self.filled(1))
        self.assertEqual(
            set(position), {"version", "schema_version", "ledger"})
        self.assertEqual(
            position["ledger"][0]["event_digest"],
            bp.v2_config_epoch_digest(self.CONFIG_SNAPSHOT))
        for forbidden in (
                "state", "rungs", "remaining_spot_quantity",
                "remaining_short_quantity", "cap_headroom_quote",
                "reduce_only", "revision", "verdict"):
            self.assertNotIn(forbidden, position)
        self.assertEqual(
            bp.derive_v2_state(position)["rungs"][0]["status"],
            bp.ST_OPEN)

        invalid = json.loads(json.dumps(position))
        invalid["state"] = bp.ST_CLOSED
        self.write_raw_position(invalid)
        with self.assertRaisesRegex(bp.PositionError, "unknown field"):
            bp.load_position(self.receipt_dir)

    def test_br3_finding_5_reduction_amounts_must_match_observed_receipts(self):
        self.ready()
        self.append(self.filled(1))
        self.observe_reduction(
            "reduce-bound", 1, "0.05", "0.05")
        before = self.state_path.read_bytes()
        mismatched = bp.new_v2_rung_reduced_event(
            1, ["reduce-bound"], {
                "spot_quantity": "0.04",
                "short_quantity": "0.04",
            })
        with self.assertRaisesRegex(
                bp.PositionError, "do not match receipts"):
            self.append(mismatched)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_br3_finding_6_ids_timestamps_and_decimal_bounds_refuse(self):
        with self.assertRaisesRegex(bp.PositionError, "bounded"):
            bp.new_v2_dislocation_event(
                "early-time", 0, "0.5", "0.1",
                "0.0001", 28800000)
        with self.assertRaisesRegex(bp.PositionError, "identifier"):
            bp.new_v2_receipt_observed_event(
                "", "FLAT", self.MODULE_TIME_MS,
                "source", self.flat_facts())
        oversized = json.loads(json.dumps(self.CONFIG_SNAPSHOT))
        oversized["caps"]["stack_quote"] = "1" * 100001
        with self.assertRaises(bp.PositionError):
            bp.new_v2_config_snapshot_event(oversized)
        dislocation = bp.new_v2_dislocation_event(
            "valid-time", self.SOURCE_TIME_MS,
            "0.5", "0.1", "0.0001", 28800000)
        with self.assertRaisesRegex(bp.PositionError, "bounded"):
            bp.new_v2_rung_filled_event(
                1, {
                    "spot": self.leg("spot", 1, "0.1", "9"),
                    "short": self.leg("short", 1, "0.1", "9"),
                }, dislocation, 0)

    def test_v2_full_two_rung_add_reduce_close_ledger_walk(self):
        self.ready()
        self.append(self.filled(1, spot_quantity="0.1",
                                short_quantity="0.1"))
        position = self.append(self.filled(
            2, spot_quantity="0.2", short_quantity="0.2"))
        self.assertEqual(
            bp.derive_v2_state(position)["top_state"], bp.ST_OPEN)

        position = self.reduce("reduce-rung-2", 2, "0.2", "0.2")
        middle = bp.derive_v2_state(position)
        self.assertEqual(
            [row["status"] for row in middle["rungs"]],
            [bp.ST_OPEN, bp.ST_CLOSED])
        position = self.reduce("reduce-rung-1", 1, "0.1", "0.1")
        self.assertEqual(
            bp.derive_v2_state(position)["top_state"],
            "AWAITING_FLAT_PROOF")
        position = self.close_proof()
        derived = bp.derive_v2_state(position)
        self.assertEqual(derived["top_state"], bp.ST_CLOSED)
        self.assertEqual(derived["remaining_spot_quantity"], "0")
        self.assertEqual(derived["remaining_short_quantity"], "0")
        self.assertEqual(
            [row["status"] for row in derived["rungs"]],
            [bp.ST_CLOSED, bp.ST_CLOSED])
        self.assertEqual(len(position["ledger"]), 11)

    def test_v2_hash_chain_break_refuses_load(self):
        self.ready()
        invalid = self.raw_position()
        flat_event = invalid["ledger"][2]
        flat_event["previous_digest"] = bp.V2_GENESIS_DIGEST
        flat_event["event_digest"] = bp.v2_event_digest(flat_event)
        self.write_raw_position(invalid)
        with self.assertRaisesRegex(bp.PositionError, "chain is broken"):
            bp.load_position(self.receipt_dir)

    def test_v2_append_cas_refusal_changes_nothing(self):
        position = self.ready()
        before = self.state_path.read_bytes()
        with self.assertRaisesRegex(bp.PositionError, "append_v2_event"):
            bp.save_position(self.receipt_dir, position)
        payload = bp.new_v2_receipt_observed_event(
            "cas-receipt", "REDUCTION", self.MODULE_TIME_MS,
            "cas-source", {
                "ordinal": 1,
                "spot_quantity": "0.01",
                "short_quantity": "0.01",
            })
        with self.assertRaisesRegex(bp.PositionError, "expected_length"):
            self.append(payload, expected_length=self.length() - 1)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertFalse(Path(str(self.state_path) + ".tmp").exists())

    def test_v2_receipt_registry_refuses_missing_or_reused_receipts(self):
        self.ready()
        self.append(self.filled(1))
        before = self.state_path.read_bytes()
        missing_reduction = bp.new_v2_rung_reduced_event(
            1, ["not-observed"], {
                "spot_quantity": "0.01",
                "short_quantity": "0.01",
            })
        with self.assertRaisesRegex(bp.PositionError, "not observed"):
            self.append(missing_reduction)
        with self.assertRaisesRegex(bp.PositionError, "not observed"):
            self.append(bp.new_v2_flat_proved_event("flat-not-observed"))
        self.assertEqual(self.state_path.read_bytes(), before)

        self.reduce("reduce-once", 1, "0.05", "0.05")
        before_reuse = self.state_path.read_bytes()
        reuse = bp.new_v2_rung_reduced_event(
            1, ["reduce-once"], {
                "spot_quantity": "0.05",
                "short_quantity": "0.05",
            })
        with self.assertRaisesRegex(bp.PositionError, "consumed"):
            self.append(reuse)
        self.assertEqual(self.state_path.read_bytes(), before_reuse)

    def test_v2_derived_state_is_equal_after_reload(self):
        self.ready()
        position = self.append(self.filled(1))
        before_record = json.dumps(position, sort_keys=True)
        before = bp.derive_v2_state(position)
        reloaded = bp.load_position(self.receipt_dir)
        after = bp.derive_v2_state(reloaded)
        self.assertEqual(after, before)
        self.assertEqual(json.dumps(position, sort_keys=True), before_record)

    def test_v2_legal_next_transitions_follow_ledger_evidence(self):
        position = self.ready()
        self.assertIn(
            {"event_type": "RungFilled", "ordinal": 1},
            bp.derive_v2_state(position)["legal_next_transitions"])
        position = self.append(self.filled(1))
        transitions = bp.derive_v2_state(position)[
            "legal_next_transitions"]
        self.assertNotIn(
            {"event_type": "RungReduced", "ordinal": 1}, transitions)
        position = self.observe_reduction(
            "legal-reduce", 1, "0.1", "0.1")
        derived = bp.derive_v2_state(position)
        self.assertEqual(derived["top_state"], "FROZEN")
        self.assertTrue(derived["reduce_only"])
        self.assertNotIn(
            {"event_type": "RungFilled", "ordinal": 2},
            derived["legal_next_transitions"])
        self.assertIn(
            {"event_type": "RungReduced", "ordinal": 1},
            derived["legal_next_transitions"])
        with self.assertRaisesRegex(bp.PositionError, "receipt evidence"):
            self.append(self.filled(
                2, cut_ms=position["ledger"][-1]["cut_ms"] + 1))

    def test_v2_loader_refuses_unknown_fields_at_nested_levels(self):
        self.ready()
        valid = self.append(self.filled(1))
        mutations = (
            lambda value: value.__setitem__("extra", True),
            lambda value: value["ledger"][0]["schedule"][0].__setitem__(
                "extra", True),
            lambda value: value["ledger"][-1]["legs"]["spot"].__setitem__(
                "extra", True),
            lambda value: value["ledger"][-1]["dislocation"].__setitem__(
                "extra", True),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                invalid = json.loads(json.dumps(valid))
                mutation(invalid)
                self.write_raw_position(invalid)
                with self.assertRaisesRegex(
                        bp.PositionError, "unknown field"):
                    bp.load_position(self.receipt_dir)
        self.write_raw_position(valid)

    def test_v2_atomic_replace_failure_preserves_prior_ledger(self):
        self.ready()
        before = self.state_path.read_bytes()
        before_head = self.head_path.read_bytes()
        payload = bp.new_v2_receipt_observed_event(
            "atomic-receipt", "REDUCTION", self.MODULE_TIME_MS + 1,
            "atomic-source", {
                "ordinal": 1,
                "spot_quantity": "0.01",
                "short_quantity": "0.01",
            })
        with mock.patch.object(
                bp.os, "replace", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                self.append(payload)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(self.head_path.read_bytes(), before_head)
        self.assertFalse(Path(str(self.state_path) + ".tmp").exists())

    def test_v2_head_write_interruption_leaves_detectable_state(self):
        self.ready()
        before_head = self.head_path.read_bytes()
        payload = bp.new_v2_receipt_observed_event(
            "head-interruption", "REDUCTION", self.MODULE_TIME_MS + 1,
            "head-source", {
                "ordinal": 1,
                "spot_quantity": "0.01",
                "short_quantity": "0.01",
            })
        real_replace = os.replace

        def interrupt_head(source, target):
            if target == str(self.head_path):
                raise OSError("simulated head interruption")
            return real_replace(source, target)

        with mock.patch.object(bp.os, "replace", side_effect=interrupt_head):
            with self.assertRaises(OSError):
                self.append(payload)
        self.assertEqual(self.head_path.read_bytes(), before_head)
        self.assertFalse(Path(str(self.head_path) + ".tmp").exists())
        with self.assertRaisesRegex(bp.PositionError, "committed head"):
            bp.load_position(self.receipt_dir)

    def test_br4_event_time_regression_and_future_cut_refuse(self):
        self.ready()
        before_position = self.state_path.read_bytes()
        before_head = self.head_path.read_bytes()
        regressed = self.filled(
            1, source_time_ms=self.MODULE_TIME_MS - 6000,
            cut_ms=self.MODULE_TIME_MS - 5000)
        with mock.patch.object(
                bp, "default_now_ms", return_value=self.MODULE_TIME_MS):
            with self.assertRaisesRegex(
                    bp.PositionError, "strictly greater"):
                self.append(regressed)
        future = self.filled(
            1, cut_ms=self.MODULE_TIME_MS
            + bp.V2_EVENT_FUTURE_SKEW_MS + 1)
        with mock.patch.object(
                bp, "default_now_ms", return_value=self.MODULE_TIME_MS):
            with self.assertRaisesRegex(bp.PositionError, "future skew"):
                self.append(future)
        self.assertEqual(self.state_path.read_bytes(), before_position)
        self.assertEqual(self.head_path.read_bytes(), before_head)

        persisted = self.raw_position()
        event = regressed
        event["previous_digest"] = persisted["ledger"][-1]["event_digest"]
        event["event_digest"] = bp.V2_GENESIS_DIGEST
        event["event_digest"] = bp.v2_event_digest(event)
        persisted["ledger"].append(event)
        self.write_raw_position(persisted)
        self.write_raw_head(persisted)
        derived = bp.derive_v2_state(bp.load_position(self.receipt_dir))
        self.assertEqual(derived["top_state"], "FROZEN")
        self.assertIn(
            "NON_MONOTONIC_EVENT_TIME", derived["freeze_reasons"])

    def test_br4_derived_residual_uses_shared_quantity_floor(self):
        self.ready()
        quantity = "1.000000000000000000000000001e-28"
        self.append(self.filled(
            1, spot_quantity=quantity, short_quantity=quantity))
        position = self.reduce(
            "reduce-to-dust", 1, "1e-28", "1e-28")
        derived = bp.derive_v2_state(position)
        residual = "0.0000000000000000000000000000000000000000000000000000001"
        self.assertEqual(
            derived["rungs"][0]["status"], bp.V2_CLOSED_RESIDUAL)
        self.assertEqual(derived["remaining_spot_quantity"], residual)
        self.assertEqual(derived["remaining_short_quantity"], residual)
        self.assertEqual(derived["next_ordinal"], None)
        self.assertNotIn(
            {"event_type": "RungFilled", "ordinal": 2},
            derived["legal_next_transitions"])
        self.assertEqual(derived["dust_rows"], [{
            "row_type": "DUST",
            "status": bp.V2_CLOSED_RESIDUAL,
            "ordinal": 1,
            "reason": "BELOW_MINIMUM_QUANTITY_FLOOR",
            "quantity_floor": "0.0000000000000000000000000001",
            "spot_quantity": residual,
            "short_quantity": residual,
            "recorded_not_lost": False,
        }])
        self.assertEqual(
            bp.V2_MIN_QUANTITY_FLOOR,
            Decimal("0.0000000000000000000000000001"))
        with self.assertRaisesRegex(
                bp.PositionError, "minimum nonzero bound"):
            bp.new_v2_receipt_observed_event(
                "below-floor", "REDUCTION", self.MODULE_TIME_MS + 100,
                "floor-source", {
                    "ordinal": 1,
                    "spot_quantity": residual,
                    "short_quantity": "0",
                })

    def test_br4_committed_head_refuses_missing_and_prefix_truncation(self):
        prefix = self.ready()
        prefix_bytes = self.state_path.read_bytes()
        self.append(self.filled(1))
        self.reduce("reduce-all", 1, "0.1", "0.1")
        self.close_proof()

        self.write_raw_position(prefix)
        with self.assertRaisesRegex(bp.PositionError, "committed head"):
            bp.load_position(self.receipt_dir)

        self.state_path.write_bytes(prefix_bytes)
        self.head_path.unlink()
        with self.assertRaisesRegex(bp.PositionError, "committed head"):
            bp.load_position(self.receipt_dir)

    def test_br4_committed_head_refuses_old_suffix_replay(self):
        self.ready()
        self.append(self.filled(1))
        prefix = self.raw_position()
        old = self.reduce("reduce-old", 1, "0.02", "0.02")

        self.write_raw_position(prefix)
        self.write_raw_head(prefix)
        self.reduce("reduce-current", 1, "0.09", "0.09")
        current_head = self.head_path.read_bytes()
        self.write_raw_position(old)
        self.assertEqual(self.head_path.read_bytes(), current_head)
        with self.assertRaisesRegex(bp.PositionError, "committed head"):
            bp.load_position(self.receipt_dir)

    def test_br4_second_persistence_cycle_reloads_committed_head(self):
        self.ready()
        first = self.append(self.filled(1))
        self.assertEqual(self.current(), first)
        second = self.reduce("reduce-second-cycle", 1, "0.04", "0.04")
        reloaded = self.current()
        self.assertEqual(reloaded, second)
        self.assertEqual(json.loads(self.head_path.read_text(
            encoding="utf-8")), {
                "event_count": len(second["ledger"]),
                "last_event_digest": second["ledger"][-1]["event_digest"],
            })

    def test_br5_event_time_equal_boundary_and_later_clock_edges(self):
        self.ready()
        prior_cut = self.current()["ledger"][-1]["cut_ms"]
        before_position = self.state_path.read_bytes()
        before_head = self.head_path.read_bytes()
        equal = self.filled(1, cut_ms=prior_cut)
        with mock.patch.object(
                bp, "default_now_ms", return_value=prior_cut):
            with self.assertRaisesRegex(
                    bp.PositionError, "strictly greater"):
                self.append(equal)
        self.assertEqual(self.state_path.read_bytes(), before_position)
        self.assertEqual(self.head_path.read_bytes(), before_head)

        boundary = self.filled(
            1, cut_ms=prior_cut + bp.V2_EVENT_FUTURE_SKEW_MS)
        with mock.patch.object(
                bp, "default_now_ms", return_value=prior_cut):
            position = self.append(boundary)
        boundary_cut = position["ledger"][-1]["cut_ms"]
        self.assertEqual(
            boundary_cut, prior_cut + bp.V2_EVENT_FUTURE_SKEW_MS)

        later = self.filled(
            2, cut_ms=boundary_cut + bp.V2_EVENT_FUTURE_SKEW_MS + 1)
        before_position = self.state_path.read_bytes()
        before_head = self.head_path.read_bytes()
        with mock.patch.object(
                bp, "default_now_ms", return_value=boundary_cut):
            with self.assertRaisesRegex(bp.PositionError, "future skew"):
                self.append(later)
        self.assertEqual(self.state_path.read_bytes(), before_position)
        self.assertEqual(self.head_path.read_bytes(), before_head)
        with mock.patch.object(
                bp, "default_now_ms", return_value=boundary_cut + 1):
            accepted = self.append(later)
        self.assertEqual(accepted["ledger"][-1]["ordinal"], 2)

    def test_br5_frozen_time_ledger_refuses_later_reappend(self):
        self.ready()
        persisted = self.raw_position()
        high_cut = persisted["ledger"][-1]["cut_ms"]
        regressed = self.filled(
            1, source_time_ms=high_cut - 100, cut_ms=high_cut - 1)
        regressed["previous_digest"] = persisted["ledger"][-1][
            "event_digest"]
        regressed["event_digest"] = bp.V2_GENESIS_DIGEST
        regressed["event_digest"] = bp.v2_event_digest(regressed)
        persisted["ledger"].append(regressed)
        self.write_raw_position(persisted)
        self.write_raw_head(persisted)
        derived = bp.derive_v2_state(bp.load_position(self.receipt_dir))
        self.assertEqual(derived["top_state"], "FROZEN")
        self.assertEqual(derived["legal_next_transitions"], [])

        before_position = self.state_path.read_bytes()
        before_head = self.head_path.read_bytes()
        later = bp.new_v2_receipt_observed_event(
            "later-receipt", "REDUCTION", high_cut + 100,
            "later-source", {
                "ordinal": 1,
                "spot_quantity": "0.01",
                "short_quantity": "0.01",
            })
        with mock.patch.object(
                bp, "default_now_ms", return_value=high_cut + 100) as clock:
            with self.assertRaisesRegex(bp.PositionError, "non-monotonic"):
                bp.append_v2_event(
                    self.receipt_dir, later, len(persisted["ledger"]))
        self.assertEqual(clock.call_count, 0)
        self.assertEqual(self.state_path.read_bytes(), before_position)
        self.assertEqual(self.head_path.read_bytes(), before_head)

    def test_br5_one_leg_dust_is_recorded_only_by_flat_proof(self):
        self.ready()
        quantity = (
            "0.0000000000000000000000000001999999999999999999999999999")
        floor = "0.0000000000000000000000000001"
        self.append(self.filled(
            1, spot_quantity=quantity, short_quantity=floor))
        position = self.reduce("one-leg-dust", 1, floor, floor)
        derived = bp.derive_v2_state(position)
        self.assertEqual(
            derived["rungs"][0]["status"], bp.V2_CLOSED_RESIDUAL)
        self.assertEqual(derived["rungs"][0][
            "remaining_short_quantity"], "0")
        self.assertEqual(derived["top_state"], "AWAITING_FLAT_PROOF")
        self.assertIn(bp.V2_DUST_STANDING, derived["freeze_reasons"])
        self.assertFalse(derived["dust_rows"][0]["recorded_not_lost"])
        residual = derived["rungs"][0]["remaining_spot_quantity"]
        with self.assertRaisesRegex(
                bp.PositionError, "minimum nonzero bound"):
            bp.new_v2_receipt_observed_event(
                "one-leg-dust-reduction", "REDUCTION",
                position["ledger"][-1]["cut_ms"] + 1,
                "dust-source", {
                    "ordinal": 1,
                    "spot_quantity": residual,
                    "short_quantity": "0",
                })

        observed_at = position["ledger"][-1]["cut_ms"] + 1
        position = self.observe_flat("flat-one-leg-dust", observed_at)
        observed = bp.derive_v2_state(position)
        self.assertIn(bp.V2_DUST_STANDING, observed["freeze_reasons"])
        self.assertFalse(observed["dust_rows"][0]["recorded_not_lost"])
        self.assertIn(
            {"event_type": "FlatProved"},
            observed["legal_next_transitions"])

        position = self.prove_flat("flat-one-leg-dust", observed_at + 1)
        closed = bp.derive_v2_state(position)
        self.assertEqual(closed["top_state"], bp.ST_CLOSED)
        self.assertNotIn(bp.V2_DUST_STANDING, closed["freeze_reasons"])
        self.assertEqual(closed["rungs"][0]["status"], bp.ST_CLOSED)
        self.assertTrue(closed["dust_rows"][0]["recorded_not_lost"])

    def test_br5_multi_rung_dust_refuses_add_then_reduces_and_proves_flat(self):
        config = json.loads(json.dumps(self.CONFIG_SNAPSHOT))
        config["schedule"].append({
            "ordinal": 3,
            "entry_gap_pct": "0.65",
            "quote_cap": "6",
        })
        config["caps"] = {"stack_quote": "30", "entry_quote": "10"}
        self.ready(config)
        self.append(self.filled(
            1, spot_quantity="0.1", short_quantity="0.1"))
        quantity = (
            "0.0000000000000000000000000001999999999999999999999999999")
        floor = "0.0000000000000000000000000001"
        self.append(self.filled(
            2, spot_quantity=quantity, short_quantity=quantity))
        position = self.reduce("top-to-dust", 2, floor, floor)
        derived = bp.derive_v2_state(position)
        self.assertEqual(
            [row["status"] for row in derived["rungs"]],
            [bp.ST_OPEN, bp.V2_CLOSED_RESIDUAL])
        self.assertEqual(derived["top_state"], "FROZEN")
        self.assertEqual(derived["freeze_reasons"], [bp.V2_DUST_STANDING])
        self.assertEqual(derived["next_ordinal"], None)
        self.assertTrue(derived["reduce_only"])
        add_three = {"event_type": "RungFilled", "ordinal": 3}
        self.assertNotIn(add_three, derived["legal_next_transitions"])

        before_position = self.state_path.read_bytes()
        before_head = self.head_path.read_bytes()
        next_cut = position["ledger"][-1]["cut_ms"] + 1
        with mock.patch.object(
                bp, "default_now_ms", return_value=next_cut):
            with self.assertRaisesRegex(bp.PositionError, "DUST_STANDING"):
                self.append(self.filled(
                    3, spot_quote="1", short_quote="1",
                    entry_gap="0.7", cut_ms=next_cut))
        self.assertEqual(self.state_path.read_bytes(), before_position)
        self.assertEqual(self.head_path.read_bytes(), before_head)

        position = self.observe_reduction(
            "reduce-open-rung", 1, "0.1", "0.1")
        pending_reduction = bp.derive_v2_state(position)
        self.assertIn(
            {"event_type": "RungReduced", "ordinal": 1},
            pending_reduction["legal_next_transitions"])
        position = self.append(bp.new_v2_rung_reduced_event(
            1, ["reduce-open-rung"], {
                "spot_quantity": "0.1",
                "short_quantity": "0.1",
            }))
        reduced = bp.derive_v2_state(position)
        self.assertEqual(reduced["top_state"], "AWAITING_FLAT_PROOF")
        self.assertIn(bp.V2_DUST_STANDING, reduced["freeze_reasons"])

        observed_at = position["ledger"][-1]["cut_ms"] + 1
        position = self.observe_flat("flat-multi-dust", observed_at)
        pending_flat = bp.derive_v2_state(position)
        self.assertIn(
            {"event_type": "FlatProved"},
            pending_flat["legal_next_transitions"])
        position = self.prove_flat("flat-multi-dust", observed_at + 1)
        closed = bp.derive_v2_state(position)
        self.assertEqual(closed["top_state"], bp.ST_CLOSED)
        self.assertEqual(closed["freeze_reasons"], [])
        self.assertEqual(
            [row["status"] for row in closed["rungs"]],
            [bp.ST_CLOSED, bp.ST_CLOSED])
        self.assertTrue(closed["dust_rows"][0]["recorded_not_lost"])

    def test_br5_wrong_path_head_refuses_same_length_ledger(self):
        self.ready()
        with tempfile.TemporaryDirectory() as other_dir:
            config = json.loads(json.dumps(self.CONFIG_SNAPSHOT))
            config["caps"]["entry_quote"] = "9"
            self.ready_at(other_dir, config, "flat-other-path")
            other_state = Path(other_dir) / bp.STATE_FILE
            other_head = Path(str(other_state) + ".head")
            self.assertEqual(
                self.length(),
                len(bp.load_position(other_dir)["ledger"]))
            self.head_path.write_bytes(other_head.read_bytes())
        with self.assertRaisesRegex(bp.PositionError, "disagrees"):
            bp.load_position(self.receipt_dir)

    def test_br5_matching_count_with_older_tail_head_refuses(self):
        self.ready()
        older_tail = self.current()["ledger"][-1]["event_digest"]
        position = self.append(self.filled(1))
        self.head_path.write_text(json.dumps({
            "event_count": len(position["ledger"]),
            "last_event_digest": older_tail,
        }), encoding="utf-8")
        with self.assertRaisesRegex(bp.PositionError, "disagrees"):
            bp.load_position(self.receipt_dir)

    def test_br5_reset_heads_refuse_prior_and_orphan_state(self):
        self.ready()
        prior_head = self.head_path.read_bytes()
        with tempfile.TemporaryDirectory() as fresh_dir:
            config = json.loads(json.dumps(self.CONFIG_SNAPSHOT))
            config["schedule"][0]["quote_cap"] = "9"
            self.ready_at(fresh_dir, config, "flat-fresh-ledger")
            fresh_state = Path(fresh_dir) / bp.STATE_FILE
            self.state_path.write_bytes(fresh_state.read_bytes())
        with self.assertRaisesRegex(bp.PositionError, "disagrees"):
            bp.load_position(self.receipt_dir)

        with tempfile.TemporaryDirectory() as orphan_dir:
            orphan_state = Path(orphan_dir) / bp.STATE_FILE
            orphan_head = Path(str(orphan_state) + ".head")
            orphan_head.write_bytes(prior_head)
            with self.assertRaisesRegex(
                    bp.PositionError, "exists without its ledger"):
                bp.initialize_v2_position(
                    orphan_dir, self.CONFIG_SNAPSHOT)


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
