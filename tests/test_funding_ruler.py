#!/usr/bin/env python3
"""Offline tests for the pure per-symbol funding ruler."""

import os
import sys
import unittest
from decimal import Decimal


SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
)
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import funding_ruler as ruler
import basis_kernel as kernel


D = Decimal
START = 1_700_000_000_000


def funding_info(cap="0.003", floor="-0.002", interval_hours=1):
    return ruler.FundingInfo(D(cap), D(floor), interval_hours)


def rule(*, rate="0.0001", next_minutes=30, hold_minutes="60",
         direction=ruler.NORMAL, info=None):
    return ruler.rule_funding(
        rate,
        str(START + next_minutes * 60_000),
        info if info is not None else funding_info(),
        START,
        D(hold_minutes),
        direction,
    )


class FundingWindowTests(unittest.TestCase):
    def test_zero_crossings_returns_zero_decimal_text(self):
        result = rule(next_minutes=61)
        self.assertIsInstance(result, ruler.FundingRulerReading)
        self.assertEqual(result.settlements_crossed, 0)
        self.assertEqual(result.worst_case_drag_pct, "0")
        self.assertEqual(result.expected_drag_pct, "0")
        self.assertEqual(result.next_settlement_timestamps_ms, ())

    def test_settlement_at_boundary_minute_is_included(self):
        result = rule(next_minutes=60, hold_minutes="60")
        self.assertEqual(result.settlements_crossed, 1)
        self.assertEqual(
            result.next_settlement_timestamps_ms,
            (str(START + 60 * 60_000),),
        )
        self.assertEqual(result.expected_drag_pct, "-0.01")

    def test_multiple_settlements_have_exact_timestamps(self):
        result = rule(next_minutes=30, hold_minutes="150")
        self.assertEqual(result.settlements_crossed, 3)
        self.assertEqual(
            result.next_settlement_timestamps_ms,
            tuple(str(START + minute * 60_000) for minute in (30, 90, 150)),
        )

    def test_reverse_worst_case_uses_symbol_cap(self):
        info = funding_info(cap="0.0075", floor="-0.004", interval_hours=1)
        result = rule(next_minutes=30, hold_minutes="90",
                      direction=ruler.REVERSE, info=info)
        self.assertEqual(result.settlements_crossed, 2)
        self.assertEqual(result.worst_case_drag_pct, "1.5")

    def test_direction_sign_matches_basis_kernel_convention(self):
        cases = (
            ("0.0001", ruler.NORMAL, "-0.01"),
            ("0.0001", ruler.REVERSE, "0.01"),
            ("-0.0002", ruler.NORMAL, "0.02"),
            ("-0.0002", ruler.REVERSE, "-0.02"),
        )
        for rate, direction, expected in cases:
            with self.subTest(rate=rate, direction=direction):
                result = rule(rate=rate, direction=direction)
                self.assertEqual(result.expected_drag_pct, expected)
                carry = kernel.funding_carry_pct(
                    60 * 60_000,
                    30 * 60_000,
                    60 * 60_000,
                    (D(rate),),
                    direction,
                )
                self.assertEqual(D(result.expected_drag_pct), -carry)

    def test_each_unknown_funding_info_field_refuses(self):
        cases = (
            ruler.FundingInfo(None, D("-0.002"), 1),
            ruler.FundingInfo(D("0.003"), None, 1),
            ruler.FundingInfo(D("0.003"), D("-0.002"), None),
        )
        for info in cases:
            with self.subTest(info=info):
                result = rule(info=info)
                self.assertIsInstance(result, ruler.FundingRulerRefusal)
                self.assertEqual(result.reason, "UNKNOWN_FUNDING_INFO")

    def test_hold_beyond_documented_settlement_limit_refuses(self):
        result = rule(
            next_minutes=1,
            hold_minutes=str(ruler.MAX_SETTLEMENTS * 60 + 1),
        )
        self.assertIsInstance(result, ruler.FundingRulerRefusal)
        self.assertEqual(result.reason, "SETTLEMENT_LIMIT_EXCEEDED")

    def test_current_rate_outside_symbol_bounds_refuses(self):
        result = rule(rate="0.0031")
        self.assertIsInstance(result, ruler.FundingRulerRefusal)
        self.assertEqual(result.reason, "INVALID_STREAM_FUNDING")


if __name__ == "__main__":
    unittest.main()
