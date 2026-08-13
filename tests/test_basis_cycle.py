"""Deterministic offline tests for src/basis_cycle.py — no network possible."""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_cycle  # noqa: E402
from basis_cycle import (  # noqa: E402
    BasisCycleError,
    CYCLE_PREFIX,
    ENTER_SHORT,
    ENTER_SPOT,
    EXIT_SHORT,
    EXIT_SPOT,
    UNWIND_SPOT,
    leg_intent,
    plan_cycle,
    run_cycle,
)
from basis_read import build_basis_read_row  # noqa: E402

ACQUIRED = Decimal("0.09970")  # exchange-truth net base: fee-deducted, floored


def fixture_row(spot_bid="99.9", spot_ask="100.0", usdm_bid="100.2",
                usdm_ask="100.3", funding="0.0001", symbol="BTCUSDT"):
    """Real basis_read math from synthetic payloads — the integration seam."""
    return build_basis_read_row(
        symbol,
        {"symbol": symbol, "bidPrice": spot_bid, "askPrice": spot_ask},
        {"symbol": symbol, "bidPrice": usdm_bid, "askPrice": usdm_ask,
         "time": 1755000000000},
        {"symbol": symbol, "lastFundingRate": funding,
         "nextFundingTime": 1755003600000},
        completed_utc="test",
    )


def full_plan(**overrides):
    row = fixture_row(**overrides)
    return plan_cycle(row, Decimal("1"), Decimal("0.00001"), Decimal("5"))


class PlanCycleTests(unittest.TestCase):
    def test_refuses_below_min_entry_gap(self):
        row = fixture_row(usdm_bid="100.001", usdm_ask="100.002")
        self.assertIsNone(plan_cycle(row, Decimal("5"), Decimal("0.00001"),
                                     Decimal("5")))

    def test_plan_holds_both_legs_under_ceiling(self):
        plan = full_plan()
        self.assertIsNotNone(plan)
        self.assertLessEqual(plan.spot_quote_cap, Decimal("10"))
        self.assertLessEqual(plan.short_notional, Decimal("10"))
        self.assertEqual(plan.spot_quantity, plan.short_quantity)
        self.assertEqual(plan.leverage, 1)

    def test_quantity_floored_to_step(self):
        plan = full_plan()
        step = Decimal("0.00001")
        self.assertEqual(plan.spot_quantity % step, Decimal("0"))
        self.assertGreater(plan.spot_quantity, 0)

    def test_refuses_when_below_min_notional(self):
        row = fixture_row()
        self.assertIsNone(plan_cycle(row, Decimal("1"), Decimal("0.00001"),
                                     Decimal("11")))

    def test_rejects_non_decimal_inputs(self):
        row = fixture_row()
        with self.assertRaises(BasisCycleError):
            plan_cycle(row, 1, Decimal("0.00001"), Decimal("5"))

    def test_all_money_fields_are_decimal(self):
        plan = full_plan()
        for value in (plan.spot_quantity, plan.short_quantity,
                      plan.spot_quote_cap, plan.short_notional,
                      plan.entry_gap_bps, plan.exit_gap_bps,
                      plan.funding_rate):
            self.assertIsInstance(value, Decimal)


class LegIntentTests(unittest.TestCase):
    def setUp(self):
        self.plan = full_plan()

    def test_spot_entry_is_quote_bounded_with_no_quantity(self):
        intent = leg_intent(ENTER_SPOT, self.plan, "r1")
        self.assertEqual(intent["quoteOrderQty"], str(self.plan.spot_quote_cap))
        self.assertNotIn("quantity", intent)

    def test_spot_entry_refuses_a_quantity(self):
        with self.assertRaises(BasisCycleError):
            leg_intent(ENTER_SPOT, self.plan, "r1", ACQUIRED)

    def test_dependent_stages_require_acquired_quantity(self):
        for stage in (ENTER_SHORT, EXIT_SHORT, EXIT_SPOT, UNWIND_SPOT):
            with self.assertRaises(BasisCycleError):
                leg_intent(stage, self.plan, "r1")
            with self.assertRaises(BasisCycleError):
                leg_intent(stage, self.plan, "r1", Decimal("0"))

    def test_dependent_stages_size_from_acquired_not_plan(self):
        for stage in (ENTER_SHORT, EXIT_SHORT, EXIT_SPOT, UNWIND_SPOT):
            intent = leg_intent(stage, self.plan, "r1", ACQUIRED)
            self.assertEqual(intent["quantity"], str(ACQUIRED))
            self.assertNotEqual(intent["quantity"], str(self.plan.spot_quantity))

    def test_short_close_is_reduce_only_buy(self):
        intent = leg_intent(EXIT_SHORT, self.plan, "r1", ACQUIRED)
        self.assertEqual(intent["side"], "BUY")
        self.assertEqual(intent["reduceOnly"], "true")
        self.assertEqual(intent["market"], "usdm")

    def test_short_open_is_sell_without_reduce_only(self):
        intent = leg_intent(ENTER_SHORT, self.plan, "r1", ACQUIRED)
        self.assertEqual(intent["side"], "SELL")
        self.assertNotIn("reduceOnly", intent)

    def test_client_ids_use_cycle_prefix_and_differ_per_stage(self):
        ids = [leg_intent(ENTER_SPOT, self.plan, "r1")["newClientOrderId"]]
        for stage in (ENTER_SHORT, EXIT_SHORT, EXIT_SPOT, UNWIND_SPOT):
            ids.append(leg_intent(stage, self.plan, "r1",
                                  ACQUIRED)["newClientOrderId"])
        self.assertEqual(len(set(ids)), 5)
        for value in ids:
            self.assertTrue(value.startswith(CYCLE_PREFIX))

    def test_unknown_stage_raises(self):
        with self.assertRaises(BasisCycleError):
            leg_intent("SIDEWAYS", self.plan, "r1", ACQUIRED)


class RunCycleTests(unittest.TestCase):
    """All eleven terminal paths, plus the input-contract guards."""

    def setUp(self):
        self.plan = full_plan()

    def drive(self, acquired=ACQUIRED, **outcomes):
        return run_cycle(self.plan, "r1", outcomes, acquired_base=acquired)

    def test_happy_path_closes_short_leg_first(self):
        result = self.drive(ENTER_SPOT="FILLED", ENTER_SHORT="FILLED",
                            EXIT_SHORT="FILLED", EXIT_SPOT="FILLED")
        self.assertEqual(result.verdict, "CLOSED")
        self.assertEqual([s["stage"] for s in result.stages],
                         [ENTER_SPOT, ENTER_SHORT, EXIT_SHORT, EXIT_SPOT])
        self.assertEqual(result.exposure, "none")

    def test_spot_entry_rejection_stops_everything(self):
        result = self.drive(ENTER_SPOT="REJECTED")
        self.assertEqual(result.verdict, "REJECTED_BEFORE_ENTRY")
        self.assertEqual(len(result.stages), 1)

    def test_spot_entry_unknown_freezes_naming_bound_and_id(self):
        result = self.drive(ENTER_SPOT="UNKNOWN")
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn(str(self.plan.spot_quote_cap), result.exposure)
        self.assertIn("bc-r1-enter_spot", result.exposure)

    def test_acquired_base_required_after_spot_fill(self):
        with self.assertRaises(BasisCycleError):
            self.drive(acquired=None, ENTER_SPOT="FILLED", ENTER_SHORT="FILLED")

    def test_acquired_base_must_be_positive_decimal(self):
        with self.assertRaises(BasisCycleError):
            self.drive(acquired=0.0997, ENTER_SPOT="FILLED",
                       ENTER_SHORT="FILLED")

    def test_short_entry_unknown_freezes_without_unwind(self):
        result = self.drive(ENTER_SPOT="FILLED", ENTER_SHORT="UNKNOWN")
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertNotIn(UNWIND_SPOT, [s["stage"] for s in result.stages])
        self.assertIn(str(ACQUIRED), result.exposure)
        self.assertIn("either", result.exposure)

    def test_short_rejection_unwinds_exactly_acquired(self):
        result = self.drive(ENTER_SPOT="FILLED", ENTER_SHORT="REJECTED",
                            UNWIND_SPOT="FILLED")
        self.assertEqual(result.verdict, "REJECTED_BEFORE_ENTRY")
        self.assertEqual(result.stages[-1]["stage"], UNWIND_SPOT)
        self.assertEqual(result.stages[-1]["quantity"], str(ACQUIRED))
        self.assertEqual(result.exposure, "none")

    def test_unwind_unknown_freezes_with_both_states(self):
        result = self.drive(ENTER_SPOT="FILLED", ENTER_SHORT="REJECTED",
                            UNWIND_SPOT="UNKNOWN")
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("either", result.exposure)
        self.assertIn(str(ACQUIRED), result.exposure)

    def test_failed_unwind_reports_open_exposure(self):
        result = self.drive(ENTER_SPOT="FILLED", ENTER_SHORT="REJECTED",
                            UNWIND_SPOT="REJECTED")
        self.assertEqual(result.verdict, "OPEN_EXPOSURE")
        self.assertIn("no hedge", result.exposure)

    def test_short_close_rejection_keeps_pair_intact(self):
        result = self.drive(ENTER_SPOT="FILLED", ENTER_SHORT="FILLED",
                            EXIT_SHORT="REJECTED")
        self.assertEqual(result.verdict, "OPEN_EXPOSURE")
        self.assertIn("hedged pair standing", result.exposure)
        self.assertNotIn(EXIT_SPOT, [s["stage"] for s in result.stages])

    def test_short_close_unknown_names_both_states(self):
        result = self.drive(ENTER_SPOT="FILLED", ENTER_SHORT="FILLED",
                            EXIT_SHORT="UNKNOWN")
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("either", result.exposure)
        self.assertIn(str(ACQUIRED), result.exposure)

    def test_spot_exit_rejection_is_unlevered_exposure(self):
        result = self.drive(ENTER_SPOT="FILLED", ENTER_SHORT="FILLED",
                            EXIT_SHORT="FILLED", EXIT_SPOT="REJECTED")
        self.assertEqual(result.verdict, "OPEN_EXPOSURE")
        self.assertIn("unlevered spot", result.exposure)
        self.assertIn("short already closed", result.exposure)

    def test_spot_exit_unknown_freezes_with_both_states(self):
        result = self.drive(ENTER_SPOT="FILLED", ENTER_SHORT="FILLED",
                            EXIT_SHORT="FILLED", EXIT_SPOT="UNKNOWN")
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("either flat or holding", result.exposure)

    def test_missing_outcome_raises(self):
        with self.assertRaises(BasisCycleError):
            self.drive(ENTER_SPOT="FILLED")

    def test_invalid_outcome_raises(self):
        with self.assertRaises(BasisCycleError):
            self.drive(ENTER_SPOT="MAYBE")


class NoNetworkGuardTests(unittest.TestCase):
    def test_module_imports_no_transport(self):
        self.assertFalse(hasattr(basis_cycle, "urlopen"))
        source = Path(basis_cycle.__file__).read_text(encoding="utf-8")
        for token in ("urllib", "socket", "http.client", "requests"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
