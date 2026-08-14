#!/usr/bin/env python3
"""Tests for the single money-cap authority."""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import job_runner as jr  # noqa: E402
import limits  # noqa: E402


NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def job(max_notional):
    return {
        "job_id": "cap",
        "op": "FUTURES_ROUNDTRIP",
        "symbol": "TSTUSDT",
        "max_notional": max_notional,
        "created_utc": "2026-08-14T11:59:00Z",
        "expires_utc": "2026-08-14T12:05:00Z",
        "requested_by": "limits-test",
    }


class LimitsTests(unittest.TestCase):
    def test_money_limits_are_positive_decimals(self):
        expected = {
            "PER_LEG_CAP_USDT": Decimal("32"),
            "FIRST_TRADE_CAP_USDT": Decimal("8"),
            "MAX_ADVERSE_PCT": Decimal("2"),
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                actual = getattr(limits, name)
                self.assertIs(type(actual), Decimal)
                self.assertEqual(actual, value)
                self.assertGreater(actual, Decimal("0"))

    def test_runner_clamps_first_trade_until_close_marker_exists(self):
        with tempfile.TemporaryDirectory() as receipt_dir:
            first = jr._validate_job(
                job(Decimal("32")), "cap", NOW, receipt_dir
            )
            self.assertEqual(first.effective_notional, Decimal("8"))

            Path(receipt_dir, "first_autonomous_close_passed").touch()
            proven = jr._validate_job(
                job(Decimal("32")), "cap", NOW, receipt_dir
            )
            self.assertEqual(proven.effective_notional, Decimal("32"))


if __name__ == "__main__":
    unittest.main()
