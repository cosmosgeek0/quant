"""Deterministic offline tests for the pure reverse-mechanics ladder."""

import hashlib
import sys
import unittest
from decimal import Decimal
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import reverse_mechanics as mechanics  # noqa: E402


D = Decimal
SYMBOL = "TESTUSDT"
SPOT = {"step": D("0.003"), "min_notional": D("2"), "price": D("100")}
PERP = {"step": D("0.004"), "min_notional": D("2"), "price": D("102")}


def build(**changes):
    values = {
        "symbol": SYMBOL,
        "quote_cap": D("8"),
        "spot_meta": SPOT,
        "perp_meta": PERP,
        "borrow_asset": "TEST",
        "borrowable": D("0.1"),
        "interest_rate_hourly": D("0.000001"),
    }
    values.update(changes)
    return mechanics.plan_reverse_mechanics(**values)


def outcome(status, quantity=None):
    fills = () if quantity is None else (D(quantity),)
    return mechanics.LegOutcome(status, fills)


def prefix(count):
    return {
        name: outcome(mechanics.DONE_STATUS, "0.01")
        for name in mechanics.LEG_ORDER[:count]
    }


def truth(liability="0.08", base_delta="0", futures="0"):
    return mechanics.ExchangeTruth(D(liability), D(base_delta), D(futures))


class PlanTests(unittest.TestCase):
    def test_common_quantity_is_valid_and_bounded(self):
        plan = build()

        self.assertIsInstance(plan, mechanics.ReversePlan)
        self.assertEqual(plan.base_qty, D("0.072"))
        self.assertEqual(plan.borrow_principal, plan.base_qty)
        self.assertEqual(plan.base_qty % SPOT["step"], D("0"))
        self.assertEqual(plan.base_qty % PERP["step"], D("0"))
        self.assertGreaterEqual(plan.spot_notional, SPOT["min_notional"])
        self.assertGreaterEqual(plan.perp_notional, PERP["min_notional"])
        self.assertLessEqual(plan.quote_notional, D("8"))
        self.assertLessEqual(plan.borrow_principal,
                             mechanics.margin_ops.PRINCIPAL_CEILING)
        self.assertEqual(tuple(leg.name for leg in plan.legs),
                         mechanics.LEG_ORDER)
        self.assertTrue(plan.legs[3].reduce_only)
        self.assertTrue(all(not leg.reduce_only for leg in
                            plan.legs[:3] + plan.legs[4:]))

    def test_plan_refuses_at_each_applicable_ordered_gate(self):
        cases = (
            (build(quote_cap=D("8.01")),
             mechanics.QUOTE_NOTIONAL_CAP_EXCEEDED),
            (build(borrowable=D("0.01")),
             mechanics.BORROW_CLAMP_EXCEEDED),
            (build(spot_meta={**SPOT, "step": None}),
             mechanics.STEP_OR_MIN_NOTIONAL_INFEASIBLE),
        )
        for result, gate in cases:
            with self.subTest(gate=gate):
                self.assertIsInstance(result, mechanics.ReversePlanRefusal)
                self.assertEqual((result.gate, result.reason), (gate, gate))
                self.assertEqual(result.gate_index,
                                 mechanics.GATE_ORDER.index(gate) + 1)

    def test_first_plan_gate_wins_when_inputs_fail_together(self):
        result = build(
            quote_cap=D("9"), borrowable=None,
            spot_meta={**SPOT, "step": None},
        )
        self.assertEqual(result.gate,
                         mechanics.QUOTE_NOTIONAL_CAP_EXCEEDED)
        result = build(
            borrowable=None, spot_meta={**SPOT, "step": None},
        )
        self.assertEqual(result.gate, mechanics.BORROW_CLAMP_EXCEEDED)

    def test_intent_and_leg_seeds_are_deterministic_and_bound_to_names(self):
        first, second = build(), build()
        changed = build(interest_rate_hourly=D("0.000002"))

        self.assertEqual(first, second)
        self.assertNotEqual(first.intent_id, changed.intent_id)
        self.assertEqual(len({leg.client_id_seed for leg in first.legs}), 6)
        for leg in first.legs:
            expected = "rmo-" + hashlib.sha256(
                (first.intent_id + ":" + leg.name).encode("ascii")
            ).hexdigest()[:24]
            self.assertEqual(leg.client_id_seed, expected)


class FailureLadderTests(unittest.TestCase):
    def setUp(self):
        self.plan = build()
        self.assertIsInstance(self.plan, mechanics.ReversePlan)

    def test_happy_path_walks_each_leg_then_requires_flat_truth(self):
        for count, expected in enumerate(mechanics.LEG_ORDER):
            action = mechanics.next_action(
                self.plan, prefix(count), truth(futures="0.01")
                if count in (3, 4) else truth(),
            )
            self.assertIsInstance(action, mechanics.NEXT_LEG)
            self.assertEqual(action.leg.name, expected)

        final = mechanics.next_action(
            self.plan, prefix(6), truth("0", "0.001", "0"))
        self.assertIs(final, mechanics.DONE)
        not_flat = mechanics.next_action(
            self.plan, prefix(6), truth("0", "0.012", "0"))
        self.assertIsInstance(not_flat, mechanics.FREEZE)
        self.assertEqual(not_flat.reason, "TERMINAL_TRUTH_NOT_FLAT")

    def test_each_known_failure_has_the_exact_cleanup_sequence(self):
        cases = (
            (mechanics.BORROW, (), mechanics.DONE),
            (mechanics.MARGIN_SELL, (mechanics.REPAY,), None),
            (mechanics.FUTURES_LONG_OPEN,
             (mechanics.MARGIN_BUY, mechanics.REPAY), None),
            (mechanics.FUTURES_CLOSE,
             (mechanics.FUTURES_CLOSE, mechanics.MARGIN_BUY,
              mechanics.REPAY), None),
            (mechanics.MARGIN_BUY,
             (mechanics.MARGIN_BUY, mechanics.REPAY), None),
            (mechanics.REPAY, (mechanics.REPAY,), None),
        )
        for failed_leg, expected, terminal in cases:
            with self.subTest(leg=failed_leg):
                index = mechanics.LEG_ORDER.index(failed_leg)
                ledger = prefix(index)
                ledger[failed_leg] = outcome(mechanics.FAILED)
                current = truth("0", "0", "0") if index == 0 else truth(
                    futures="0.01" if failed_leg == mechanics.FUTURES_CLOSE
                    else "0")
                action = mechanics.next_action(self.plan, ledger, current)
                if terminal is mechanics.DONE:
                    self.assertIs(action, mechanics.DONE)
                else:
                    self.assertIsInstance(action, mechanics.UNWIND)
                    self.assertEqual(action.sequence, expected)

    def test_partial_sell_and_partial_long_use_the_filled_ladder(self):
        sell_failed = prefix(1)
        sell_failed[mechanics.MARGIN_SELL] = outcome(
            mechanics.FAILED, "0.01")
        action = mechanics.next_action(self.plan, sell_failed, truth())
        self.assertEqual(action.sequence,
                         (mechanics.MARGIN_BUY, mechanics.REPAY))

        long_failed = prefix(2)
        long_failed[mechanics.FUTURES_LONG_OPEN] = outcome(
            mechanics.FAILED, "0.01")
        action = mechanics.next_action(
            self.plan, long_failed, truth(futures="0.01"))
        self.assertEqual(action.sequence, (
            mechanics.FUTURES_CLOSE, mechanics.MARGIN_BUY,
            mechanics.REPAY,
        ))
        self.assertTrue(action.leg_sequence[0].reduce_only)

    def test_unknown_at_every_leg_freezes_before_any_action(self):
        for index, leg in enumerate(mechanics.LEG_ORDER):
            with self.subTest(leg=leg):
                ledger = prefix(index)
                ledger[leg] = outcome(mechanics.UNKNOWN)
                action = mechanics.next_action(self.plan, ledger, truth())
                self.assertIsInstance(action, mechanics.FREEZE)
                self.assertEqual(action.reason, leg + "_UNKNOWN")

    def test_unknown_exchange_truth_freezes(self):
        action = mechanics.next_action(
            self.plan, {}, mechanics.ExchangeTruth(None, D("0"), D("0")))
        self.assertIsInstance(action, mechanics.FREEZE)
        self.assertEqual(action.reason, "EXCHANGE_TRUTH_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
