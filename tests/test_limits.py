#!/usr/bin/env python3
"""Tests for the single money-cap authority."""

import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

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
    def test_per_leg_cap_is_positive_decimal(self):
        self.assertIs(type(limits.PER_LEG_CAP_USDT), Decimal)
        self.assertEqual(limits.PER_LEG_CAP_USDT, Decimal("32"))
        self.assertGreater(limits.PER_LEG_CAP_USDT, Decimal("0"))

    def test_runner_clamp_uses_limits_authority(self):
        authority_cap = Decimal("31")
        with mock.patch.object(limits, "PER_LEG_CAP_USDT", authority_cap):
            parsed = jr._validate_job(job(Decimal("99")), "cap", NOW)
        self.assertEqual(parsed.effective_notional, authority_cap)


if __name__ == "__main__":
    unittest.main()
