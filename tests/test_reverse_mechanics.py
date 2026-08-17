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
SPOT = {"step": D("0.003"), "min_qty": D("0.003"),
        "min_notional": D("2"), "price": D("100")}
PERP = {"step": D("0.004"), "min_qty": D("0.004"),
        "min_notional": D("2"), "price": D("102")}
ZRO_SPOT = {"step": D("0.1"), "min_qty": D("0.1"),
            "min_notional": D("5"), "price": D("2.5")}
ZRO_PERP = {"step": D("0.1"), "min_qty": D("0.1"),
            "min_notional": D("5"), "price": D("2.4")}


def build(**changes):
    values = {
        "symbol": SYMBOL,
        "quote_cap": D("8"),
        "spot_meta": SPOT,
        "perp_meta": PERP,
        "borrow_asset": "TEST",
        "borrowable": D("0.1"),
        "interest_rate_hourly": D("0.000001"),
        "intent_id": "reverse-intent-a",
        "client_tag": "reverse-client-a",
    }
    values.update(changes)
    return mechanics.plan_reverse_mechanics(**values)


def build_zro(**changes):
    values = {
        "symbol": "ZROUSDT",
        "spot_meta": ZRO_SPOT,
        "perp_meta": ZRO_PERP,
        "borrow_asset": "ZRO",
        "borrowable": D("10.08"),
    }
    values.update(changes)
    return build(**values)


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

    def test_principal_ceiling_keeps_one_common_step_of_repay_headroom(self):
        plan = build_zro()

        self.assertIsInstance(plan, mechanics.ReversePlan)
        quote_only_qty = min(
            plan.quote_cap / ZRO_SPOT["price"],
            plan.quote_cap / ZRO_PERP["price"],
        )
        self.assertEqual(quote_only_qty, D("3.2"))
        self.assertEqual(mechanics.margin_ops.PRINCIPAL_CEILING, D("3"))
        self.assertEqual(plan.common_step, D("0.1"))
        self.assertEqual((plan.base_qty, plan.borrow_principal),
                         (D("2.9"), D("2.9")))
        self.assertEqual((plan.spot_notional, plan.perp_notional,
                          plan.quote_notional),
                         (D("7.25"), D("6.96"), D("7.25")))
        self.assertLessEqual(plan.quote_notional, plan.quote_cap)
        self.assertGreaterEqual(plan.spot_notional,
                                ZRO_SPOT["min_notional"])
        self.assertGreaterEqual(plan.perp_notional,
                                ZRO_PERP["min_notional"])
        self.assertTrue(all(leg.base_qty == D("2.9") for leg in plan.legs))

    def test_borrowable_sizes_the_plan_when_it_is_the_smaller_clamp(self):
        plan = build_zro(borrowable=D("2.7"))

        self.assertIsInstance(plan, mechanics.ReversePlan)
        self.assertEqual(plan.base_qty, D("2.7"))
        self.assertEqual(plan.borrow_principal, D("2.7"))
        self.assertLess(plan.borrowable,
                        mechanics.margin_ops.PRINCIPAL_CEILING)
        self.assertGreaterEqual(plan.spot_notional,
                                ZRO_SPOT["min_notional"])
        self.assertGreaterEqual(plan.perp_notional,
                                ZRO_PERP["min_notional"])

    def test_binding_window_below_a_venue_floor_refuses_without_a_plan(self):
        cases = (
            ("ceiling", {
                "spot_meta": {**ZRO_SPOT, "min_notional": D("7.6")},
                "perp_meta": {**ZRO_PERP, "min_notional": D("7.6")},
            }),
            ("borrowable", {"borrowable": D("1.9")}),
        )
        for name, changes in cases:
            with self.subTest(clamp=name):
                result = build_zro(**changes)
                self.assertIsInstance(result, mechanics.ReversePlanRefusal)
                self.assertEqual(
                    (result.gate, result.reason),
                    (mechanics.STEP_OR_MIN_NOTIONAL_INFEASIBLE,
                     mechanics.STEP_OR_MIN_NOTIONAL_INFEASIBLE),
                )
                self.assertFalse(hasattr(result, "legs"))

    def test_quantity_below_either_venue_minimum_refuses(self):
        cases = (
            ("spot", {"spot_meta": {**SPOT, "min_qty": D("0.073")}}),
            ("futures", {"perp_meta": {**PERP, "min_qty": D("0.073")}}),
        )
        for venue, changes in cases:
            with self.subTest(venue=venue):
                result = build(**changes)
                self.assertIsInstance(result, mechanics.ReversePlanRefusal)
                self.assertEqual(
                    (result.gate, result.reason),
                    (mechanics.STEP_OR_MIN_NOTIONAL_INFEASIBLE,
                     mechanics.STEP_OR_MIN_NOTIONAL_INFEASIBLE),
                )

    def test_missing_or_invalid_minimum_quantity_fails_closed(self):
        spot_without_minimum = {
            name: value for name, value in SPOT.items()
            if name != "min_qty"
        }
        perp_without_minimum = {
            name: value for name, value in PERP.items()
            if name != "min_qty"
        }
        cases = (
            ("spot-missing", {"spot_meta": spot_without_minimum}),
            ("futures-missing", {"perp_meta": perp_without_minimum}),
            ("spot-invalid", {
                "spot_meta": {**SPOT, "min_qty": D("NaN")},
            }),
            ("futures-zero", {
                "perp_meta": {**PERP, "min_qty": D("0")},
            }),
        )
        for name, changes in cases:
            with self.subTest(name=name):
                result = build(**changes)
                self.assertIsInstance(result, mechanics.ReversePlanRefusal)
                self.assertEqual(
                    result.reason,
                    mechanics.STEP_OR_MIN_NOTIONAL_INFEASIBLE,
                )

    def test_plan_refuses_at_each_applicable_ordered_gate(self):
        cases = (
            (build(quote_cap=D("8.01")),
             mechanics.QUOTE_NOTIONAL_CAP_EXCEEDED),
            (build(borrowable=None),
             mechanics.BORROW_CLAMP_EXCEEDED),
            (build(borrowable=D("0.01")),
             mechanics.STEP_OR_MIN_NOTIONAL_INFEASIBLE),
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
        market_changed = build(interest_rate_hourly=D("0.000002"))
        changed_intent = build(intent_id="reverse-intent-b")

        self.assertEqual(first, second)
        self.assertEqual((first.intent_id, first.client_tag),
                         ("reverse-intent-a", "reverse-client-a"))
        self.assertEqual(
            [leg.client_id_seed for leg in first.legs],
            [leg.client_id_seed for leg in market_changed.legs],
        )
        self.assertNotEqual(
            [leg.client_id_seed for leg in first.legs],
            [leg.client_id_seed for leg in changed_intent.legs],
        )
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
                "step": D("0.1"), "min_qty": D("0.1"),
                "min_notional": D("5"), "price": D("3"),
            },
            perp_meta={
                "step": D("1"), "min_qty": D("1"),
                "min_notional": D("5"), "price": D("3.5"),
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
