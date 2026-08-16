"""Deterministic offline tests for the pure reverse-mechanics ladder."""

import hashlib
import sys
import unittest
from decimal import Decimal
from itertools import product
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


def outcome(status, quantity=None, net_base=None):
    executed = None if quantity is None else D(quantity)
    fills = () if executed is None or executed == D("0") else (executed,)
    net = None if net_base is None else D(net_base)
    return mechanics.LegOutcome(status, fills, executed, net)


def leg_outcome(name, status=mechanics.DONE_STATUS, quantity="0.072",
                net_base=None):
    if name in (mechanics.MARGIN_SELL, mechanics.MARGIN_BUY) \
            and net_base is None and D(quantity) > D("0"):
        net_base = quantity
    return outcome(status, quantity, net_base)


def prefix(count):
    return {
        name: leg_outcome(name)
        for name in mechanics.LEG_ORDER[:count]
    }


def truth(liability="0.072", base_delta="0", futures="0"):
    return mechanics.ExchangeTruth(D(liability), D(base_delta), D(futures))


def prefix_truth(count):
    values = (
        ("0", "0", "0"),
        ("0.072", "0.072", "0"),
        ("0.072", "0", "0"),
        ("0.072", "0", "0.072"),
        ("0.072", "0", "0"),
        ("0.072", "0.072", "0"),
        ("0", "0", "0"),
    )
    return truth(*values[count])


class PlanTests(unittest.TestCase):
    def test_common_quantity_is_valid_and_bounded(self):
        plan = build()

        self.assertIsInstance(plan, mechanics.ReversePlan)
        self.assertEqual(plan.base_qty, D("0.072"))
        self.assertEqual(plan.borrow_principal, plan.base_qty)
        self.assertEqual(plan.base_qty % SPOT["step"], D("0"))
        self.assertEqual(plan.base_qty % PERP["step"], D("0"))
        self.assertEqual((plan.spot_step, plan.dust),
                         (SPOT["step"], SPOT["step"]))
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

    def test_happy_path_closes_first_and_requires_flat_truth(self):
        for count, expected in enumerate(mechanics.LEG_ORDER):
            action = mechanics.next_action(
                self.plan, prefix(count), prefix_truth(count),
            )
            if expected == mechanics.FUTURES_CLOSE:
                self.assertIsInstance(action, mechanics.UNWIND)
                self.assertEqual(action.sequence, (
                    mechanics.FUTURES_CLOSE, mechanics.MARGIN_BUY,
                    mechanics.REPAY,
                ))
                self.assertEqual(action.leg_sequence[0].quantity, D("0.072"))
            else:
                self.assertIsInstance(action, mechanics.NEXT_LEG)
                self.assertEqual(action.leg.name, expected)
                self.assertEqual(action.leg.quantity, D("0.072"))

        final = mechanics.next_action(
            self.plan, prefix(6), truth("0", "0.002", "0"))
        self.assertIs(final, mechanics.DONE)
        not_flat = mechanics.next_action(
            self.plan, prefix(6), truth("0", "0.003", "0"))
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
                ledger[failed_leg] = leg_outcome(
                    failed_leg, mechanics.FAILED, "0", "0"
                    if failed_leg in (mechanics.MARGIN_SELL,
                                      mechanics.MARGIN_BUY) else None,
                )
                current = {
                    mechanics.BORROW: truth("0", "0", "0"),
                    mechanics.MARGIN_SELL: truth("0.072", "0.072", "0"),
                    mechanics.FUTURES_LONG_OPEN: truth("0.072", "0", "0"),
                    mechanics.FUTURES_CLOSE: truth(
                        "0.072", "0", "0.072",
                    ),
                    mechanics.MARGIN_BUY: truth("0.072", "0", "0"),
                    mechanics.REPAY: truth("0.072", "0.072", "0"),
                }[failed_leg]
                action = mechanics.next_action(self.plan, ledger, current)
                if terminal is mechanics.DONE:
                    self.assertIs(action, mechanics.DONE)
                else:
                    self.assertIsInstance(action, mechanics.UNWIND)
                    self.assertEqual(action.sequence, expected)
                    self.assertTrue(all(
                        leg.quantity > D("0") for leg in action.leg_sequence
                    ))

    def test_partial_sell_and_partial_long_use_the_filled_ladder(self):
        sell_failed = prefix(1)
        sell_failed[mechanics.MARGIN_SELL] = leg_outcome(
            mechanics.MARGIN_SELL, mechanics.FAILED, "0.012", "0.011",
        )
        action = mechanics.next_action(
            self.plan, sell_failed, truth("0.072", "0.060", "0"),
        )
        self.assertEqual(action.sequence,
                         (mechanics.MARGIN_BUY, mechanics.REPAY))
        self.assertEqual(
            tuple(leg.quantity for leg in action.leg_sequence),
            (D("0.009"), D("0.072")),
        )

        long_failed = prefix(2)
        long_failed[mechanics.FUTURES_LONG_OPEN] = outcome(
            mechanics.FAILED, "0.012")
        action = mechanics.next_action(
            self.plan, long_failed, truth(futures="0.012"))
        self.assertEqual(action.sequence, (
            mechanics.FUTURES_CLOSE, mechanics.MARGIN_BUY,
            mechanics.REPAY,
        ))
        self.assertTrue(action.leg_sequence[0].reduce_only)
        self.assertEqual(
            tuple(leg.quantity for leg in action.leg_sequence),
            (D("0.012"), D("0.072"), D("0.072")),
        )

    def test_truth_mismatch_blocks_borrow_and_reopens_close_first(self):
        two_plan = build(
            spot_meta={
                "step": D("0.1"), "min_notional": D("5"), "price": D("3"),
            },
            perp_meta={
                "step": D("1"), "min_notional": D("5"), "price": D("3.5"),
            },
            borrowable=D("3"),
        )
        self.assertEqual(two_plan.base_qty, D("2"))
        action = mechanics.next_action(
            two_plan, {}, truth("2", "0", "0"),
        )
        self.assertIsInstance(action, mechanics.FREEZE)
        self.assertEqual(action.reason, "LEDGER_TRUTH_MISMATCH")

        ledger = {
            name: leg_outcome(name, quantity="2")
            for name in mechanics.LEG_ORDER[:4]
        }
        action = mechanics.next_action(
            two_plan, ledger, truth("2", "0", "2"),
        )
        self.assertIsInstance(action, mechanics.UNWIND)
        self.assertEqual(action.leg_sequence[0].name,
                         mechanics.FUTURES_CLOSE)
        self.assertEqual(action.leg_sequence[0].quantity, D("2"))

    def test_spot_step_dust_is_sale_evidence_and_larger_delta_freezes(self):
        ledger = prefix(2)
        ledger[mechanics.FUTURES_LONG_OPEN] = outcome(
            mechanics.FAILED, "0",
        )
        exact_step = mechanics.next_action(
            self.plan, ledger, truth("0.072", "-0.003", "0"),
        )
        self.assertIsInstance(exact_step, mechanics.UNWIND)
        self.assertEqual(exact_step.sequence,
                         (mechanics.MARGIN_BUY, mechanics.REPAY))

        outside_step = mechanics.next_action(
            self.plan, ledger, truth("0.072", "-0.0031", "0"),
        )
        self.assertIsInstance(outside_step, mechanics.FREEZE)
        self.assertEqual(outside_step.reason, "LEDGER_TRUTH_MISMATCH")

    def test_unknown_at_every_leg_freezes_before_any_action(self):
        for index, leg in enumerate(mechanics.LEG_ORDER):
            with self.subTest(leg=leg):
                ledger = prefix(index)
                ledger[leg] = outcome(mechanics.UNKNOWN)
                action = mechanics.next_action(
                    self.plan, ledger, prefix_truth(index),
                )
                self.assertIsInstance(action, mechanics.FREEZE)
                self.assertEqual(action.reason, leg + "_UNKNOWN")

    def test_unknown_exchange_truth_freezes(self):
        action = mechanics.next_action(
            self.plan, {}, mechanics.ExchangeTruth(None, D("0"), D("0")))
        self.assertIsInstance(action, mechanics.FREEZE)
        self.assertEqual(action.reason, "EXCHANGE_TRUTH_UNKNOWN")

    def test_full_status_truth_matrix_preserves_exposure_rules(self):
        statuses = (
            mechanics.NOT_STARTED, mechanics.DONE_STATUS,
            mechanics.FAILED, mechanics.UNKNOWN,
        )
        checked = 0
        for status_rows in product(statuses, repeat=len(mechanics.LEG_ORDER)):
            ledger = {}
            for name, status in zip(mechanics.LEG_ORDER, status_rows):
                if status in (mechanics.NOT_STARTED, mechanics.UNKNOWN):
                    ledger[name] = outcome(status)
                elif status == mechanics.DONE_STATUS:
                    ledger[name] = leg_outcome(name)
                else:
                    ledger[name] = leg_outcome(
                        name, mechanics.FAILED, "0", "0"
                        if name in (mechanics.MARGIN_SELL,
                                    mechanics.MARGIN_BUY) else None,
                    )
            for liability, base_delta, futures in product(
                    (D("0"), D("0.072")),
                    (D("0"), D("-0.003"), D("0.072")),
                    (D("0"), D("0.072"))):
                current = mechanics.ExchangeTruth(
                    liability, base_delta, futures,
                )
                action = mechanics.next_action(self.plan, ledger, current)
                self.assertIsInstance(action, (
                    mechanics.NextLeg, mechanics.Unwind,
                    mechanics.Freeze, mechanics.DoneAction,
                ), msg=(status_rows, current, action))
                if futures > D("0") and not isinstance(
                        action, mechanics.Freeze):
                    self.assertIsInstance(
                        action, mechanics.Unwind,
                        msg=(status_rows, current, action),
                    )
                    self.assertEqual(
                        action.leg_sequence[0].name,
                        mechanics.FUTURES_CLOSE,
                        msg=(status_rows, current, action),
                    )
                if liability > D("0") and isinstance(
                        action, mechanics.NextLeg):
                    self.assertNotEqual(
                        action.leg.name, mechanics.BORROW,
                        msg=(status_rows, current, action),
                    )
                checked += 1
        self.assertEqual(checked, 49152)


if __name__ == "__main__":
    unittest.main()
