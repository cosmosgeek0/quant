#!/usr/bin/env python3
"""Offline coverage for BASIS-LOCKIN-SPEC rows 089-099, 107, and 109."""

import sys
import unittest
from decimal import Decimal, localcontext
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import depth_walk as depth  # noqa: E402


def book(bids, asks):
    return {"bids": bids, "asks": asks}


def deep_book(bid, ask):
    return book([[str(bid), "10"]], [[str(ask), "10"]])


class DepthWalkTests(unittest.TestCase):
    def test_three_level_walk_uses_exact_decimal_arithmetic(self):
        levels = [
            ["100", "0.012"],
            ["125", "0.040"],
            ["150", "0.012"],
        ]

        result = depth.walk_depth(levels, "BUY", Decimal("8"))

        self.assertIsInstance(result, depth.DepthWalk)
        self.assertEqual(result.fillable_quantity, Decimal("0.064"))
        self.assertEqual(result.vwap, Decimal("125"))
        self.assertEqual(result.top_price, Decimal("100"))
        self.assertEqual(result.worst_price, Decimal("150"))
        self.assertEqual(result.slippage_pct, Decimal("25.00"))
        self.assertEqual(result.levels_touched, 3)

    def test_insufficient_depth_returns_typed_refusal(self):
        result = depth.walk_depth([["100", "0.01"]], "BUY", "8")

        self.assertIsInstance(result, depth.DepthWalkRefusal)
        self.assertEqual(result.reason, "DEPTH_INSUFFICIENT")
        self.assertFalse(hasattr(result, "vwap"))

    def test_unsorted_side_returns_typed_refusal(self):
        asks = [["101", "1"], ["100", "1"]]
        bids = [["100", "1"], ["101", "1"]]

        for levels, side in ((asks, "BUY"), (bids, "SELL")):
            with self.subTest(side=side):
                result = depth.walk_depth(levels, side, "8")
                self.assertIsInstance(result, depth.DepthWalkRefusal)
                self.assertEqual(result.reason, "LEVELS_UNSORTED")
                self.assertEqual(result.level_index, 1)

    def test_non_decimal_text_and_nonfinite_values_refuse(self):
        invalid_values = (Decimal("100"), 100, 100.0, "NaN", "Infinity", "0")
        for value in invalid_values:
            with self.subTest(value=value):
                result = depth.walk_depth([[value, "1"]], "BUY", "8")
                self.assertIsInstance(result, depth.DepthWalkRefusal)
                self.assertEqual(result.reason, "LEVEL_VALUE_INVALID")

    def test_crossed_or_locked_full_book_refuses(self):
        spot = book([["101", "1"]], [["100", "1"]])
        result = depth.walk_lockin(
            "NORMAL", "ENTRY", spot, deep_book("109", "111"), "8")

        self.assertIsInstance(result, depth.LockinDepthRefusal)
        self.assertEqual(result.reason, "BOOK_CROSSED_OR_LOCKED")
        self.assertEqual(result.leg, "SPOT")

    def test_entry_and_exit_select_both_directions(self):
        spot = deep_book("99", "100")
        perp = deep_book("110", "111")
        expected = {
            ("NORMAL", "ENTRY"): ("BUY", "SELL"),
            ("NORMAL", "EXIT"): ("SELL", "BUY"),
            ("REVERSE", "ENTRY"): ("SELL", "BUY"),
            ("REVERSE", "EXIT"): ("BUY", "SELL"),
        }

        for (direction, transition), sides in expected.items():
            with self.subTest(direction=direction, transition=transition):
                result = depth.walk_lockin(
                    direction, transition, spot, perp, Decimal("8"))
                self.assertIsInstance(result, depth.LockinDepthWalk)
                self.assertEqual(
                    (result.spot_walk.side, result.perp_walk.side), sides)
                with localcontext() as context:
                    context.prec = 50
                    if direction == "NORMAL":
                        expected_capture = (
                            result.perp_walk.vwap - result.spot_walk.vwap
                        ) / result.spot_walk.vwap * Decimal("100")
                    else:
                        expected_capture = (
                            result.spot_walk.vwap - result.perp_walk.vwap
                        ) / result.perp_walk.vwap * Decimal("100")
                self.assertEqual(result.executable_capture_pct,
                                 expected_capture)

    def test_deep_capture_matches_touch_and_thin_capture_diverges(self):
        deep = depth.walk_lockin(
            "NORMAL", "ENTRY", deep_book("99", "100"),
            deep_book("110", "111"), "8")
        thin_spot = book(
            [["99", "1"]], [["100", "0.02"], ["102", "1"]])
        thin_perp = book(
            [["110", "0.02"], ["108", "1"]], [["111", "1"]])
        thin = depth.walk_lockin(
            "NORMAL", "ENTRY", thin_spot, thin_perp, "8")

        self.assertIsInstance(deep, depth.LockinDepthWalk)
        self.assertIsInstance(thin, depth.LockinDepthWalk)
        self.assertEqual(deep.executable_capture_pct, Decimal("10.0"))
        self.assertNotEqual(thin.executable_capture_pct,
                            deep.executable_capture_pct)
        self.assertLess(thin.executable_capture_pct,
                        deep.executable_capture_pct)

    def test_target_above_first_trade_limit_refuses(self):
        result = depth.walk_depth([["100", "1"]], "BUY", "8.00000001")

        self.assertIsInstance(result, depth.DepthWalkRefusal)
        self.assertEqual(result.reason, "TARGET_NOTIONAL_LIMIT_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
