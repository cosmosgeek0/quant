#!/usr/bin/env python3
"""Tests for the single money-cap authority."""

import importlib.util
import os
import sys
import tempfile
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
LIMITS_PATH = SRC / "limits.py"
ACCOUNT_GOVERNOR_PATH = SRC / "account_governor.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
            "FIRST_TRADE_CAP_USDT": Decimal("30"),
            "MAX_ADVERSE_PCT": Decimal("2"),
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                actual = getattr(limits, name)
                self.assertIs(type(actual), Decimal)
                self.assertEqual(actual, value)
                self.assertGreater(actual, Decimal("0"))

    def test_knob_environment_overrides_are_honored(self):
        environment = {
            "QUANT_IGNITION_SURGE": "4.25",
            "QUANT_MAX_ENTRIES_DAY": "17",
            "QUANT_DAILY_LOSS_CAP": "6.50",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            configured = load_module("limits_overrides_test", LIMITS_PATH)

        self.assertEqual(
            configured.MOMENTUM_IGNITION_SURGE_RATIO, Decimal("4.25")
        )
        self.assertEqual(configured.MAX_ENTRIES_PER_UTC_DAY, 17)
        self.assertEqual(
            configured.DAILY_LOSS_PLUS_FEES_USDT, Decimal("6.50")
        )

    def test_invalid_knob_environment_falls_back_to_defaults(self):
        environment = {
            "QUANT_IGNITION_SURGE": "NaN",
            "QUANT_MAX_ENTRIES_DAY": "0",
            "QUANT_DAILY_LOSS_CAP": "garbage",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            configured = load_module("limits_garbage_test", LIMITS_PATH)

        self.assertEqual(
            configured.MOMENTUM_IGNITION_SURGE_RATIO, Decimal("3")
        )
        self.assertEqual(configured.MAX_ENTRIES_PER_UTC_DAY, 24)
        self.assertEqual(
            configured.DAILY_LOSS_PLUS_FEES_USDT, Decimal("10.00")
        )

    def test_governor_defaults_follow_limits_values(self):
        environment = {
            "QUANT_MAX_ENTRIES_DAY": "19",
            "QUANT_DAILY_LOSS_CAP": "7.25",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            configured_limits = load_module(
                "limits_governor_test", LIMITS_PATH
            )
            with mock.patch.dict(sys.modules, {"limits": configured_limits}):
                configured_governor = load_module(
                    "account_governor_limits_test", ACCOUNT_GOVERNOR_PATH
                )

        self.assertEqual(
            configured_governor.DEFAULT_MAX_ENTRIES_PER_UTC_DAY,
            configured_limits.MAX_ENTRIES_PER_UTC_DAY,
        )
        self.assertEqual(
            configured_governor.DEFAULT_DAILY_LOSS_PLUS_FEES_USDT,
            configured_limits.DAILY_LOSS_PLUS_FEES_USDT,
        )

    def test_runner_clamps_first_trade_until_close_marker_exists(self):
        with tempfile.TemporaryDirectory() as receipt_dir:
            first = jr._validate_job(
                job(Decimal("32")), "cap", NOW, receipt_dir
            )
            self.assertEqual(first.effective_notional, Decimal("30"))

            Path(receipt_dir, "first_autonomous_close_passed").touch()
            proven = jr._validate_job(
                job(Decimal("32")), "cap", NOW, receipt_dir
            )
            self.assertEqual(proven.effective_notional, Decimal("32"))


if __name__ == "__main__":
    unittest.main()


class KnobCeilingTests(unittest.TestCase):
    def test_env_knobs_clamp_to_ceilings(self):
        import json, os, subprocess, sys
        env = dict(os.environ, QUANT_IGNITION_SURGE="5000",
                   QUANT_MAX_ENTRIES_DAY="500", QUANT_DAILY_LOSS_CAP="9999")
        src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
        out = subprocess.run(
            [sys.executable, "-c",
             "import limits, json; print(json.dumps(["
             "str(limits.MOMENTUM_IGNITION_SURGE_RATIO),"
             "limits.MAX_ENTRIES_PER_UTC_DAY,"
             "str(limits.DAILY_LOSS_PLUS_FEES_USDT)]))"],
            cwd=src, env=env, capture_output=True, text=True, check=True,
        )
        surge, entries, loss = json.loads(out.stdout)
        self.assertEqual(surge, "100")
        self.assertEqual(entries, 24)
        self.assertEqual(loss, "10.00")
