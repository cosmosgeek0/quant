#!/usr/bin/env python3
"""Deterministic offline tests for src/volume_observer.py — no network.

The paper lifecycle is driven with synthetic price series through entry,
trailing stop, and exit, and the paper receipts' contents are asserted
exactly.  The observer's row building is tested against synthetic payloads.
A guard patches the module's urlopen so any network attempt fails the test.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import volume_observer as vo


FIXED_TS = "2026-08-13T00:00:00.000Z"


def fixed_now():
    return FIXED_TS


def prices(*raw):
    return [Decimal(x) for x in raw]


def valid_payload():
    return {
        "symbol": "BTCUSDT",
        "lastPrice": "60000.10",
        "priceChange": "-100.10",
        "priceChangePercent": "-0.166",
        "highPrice": "61000",
        "lowPrice": "59000",
        "volume": "12345.678",
        "quoteVolume": "740000000.5",
        "openTime": 0,
        "closeTime": 86_400_000,
        "count": 123456,
    }


class NoNetworkTestCase(unittest.TestCase):
    """Every test runs with the module's urlopen booby-trapped."""

    def setUp(self):
        patcher = patch.object(
            vo, "urlopen", side_effect=AssertionError("network call attempted")
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.receipts = Path(tmp.name) / "paper_receipts.jsonl"

    def lifecycle(self, series, **kwargs):
        kwargs.setdefault("notional", Decimal("10"))
        kwargs.setdefault("trail_pct", Decimal("0.05"))
        kwargs.setdefault("receipt_path", self.receipts)
        kwargs.setdefault("run_id", "papertest01")
        kwargs.setdefault("now_fn", fixed_now)
        return vo.run_paper_lifecycle("BTCUSDT", series, **kwargs)

    def read_lines(self, path=None):
        with open(path or self.receipts, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle.read().splitlines()]


class PaperLifecycleTests(NoNetworkTestCase):
    def test_entry_trail_exit_receipt_contents(self):
        entry, exit_ = self.lifecycle(prices("100", "105", "110", "108", "104"))
        self.assertEqual(
            entry,
            {
                "paper": True,
                "kind": "paper_entry",
                "ts": FIXED_TS,
                "run_id": "papertest01",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "seq": 0,
                "price": "100",
                "qty": "0.10000000",
                "paper_notional_requested": "10",
                "paper_cost": "10.00000000",
                "trail_pct": "0.05",
                "note": "paper only; no real order path",
            },
        )
        self.assertEqual(
            exit_,
            {
                "paper": True,
                "kind": "paper_exit",
                "ts": FIXED_TS,
                "run_id": "papertest01",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "seq": 4,
                "price": "104",
                "qty": "0.10000000",
                "exit_reason": "trail_stop",
                "peak_price": "110",
                "trail_stop_price": "104.50",
                "paper_proceeds": "10.40000000",
                "paper_pnl_quote": "0.40000000",
                "note": "paper only; no real order path",
            },
        )

    def test_receipt_file_matches_returned_records(self):
        entry, exit_ = self.lifecycle(prices("100", "105", "110", "108", "104"))
        lines = self.read_lines()
        self.assertEqual(lines, [entry, exit_])

    def test_every_record_marked_paper(self):
        self.lifecycle(prices("100", "90"))
        for record in self.read_lines():
            self.assertIs(record["paper"], True)

    def test_exit_at_first_trail_hit_not_later_lows(self):
        _, exit_ = self.lifecycle(prices("100", "110", "104", "90"))
        self.assertEqual(exit_["seq"], 2)
        self.assertEqual(exit_["price"], "104")
        self.assertEqual(exit_["exit_reason"], "trail_stop")
        self.assertEqual(exit_["peak_price"], "110")

    def test_immediate_drop_exits_on_prior_peak_stop(self):
        _, exit_ = self.lifecycle(prices("100", "90"))
        self.assertEqual(exit_["seq"], 1)
        self.assertEqual(exit_["price"], "90")
        self.assertEqual(exit_["exit_reason"], "trail_stop")
        self.assertEqual(exit_["trail_stop_price"], "95.00")
        self.assertEqual(exit_["paper_pnl_quote"], "-1.00000000")

    def test_rising_series_ends_without_trail_hit(self):
        _, exit_ = self.lifecycle(prices("100", "101", "102"))
        self.assertEqual(exit_["seq"], 2)
        self.assertEqual(exit_["price"], "102")
        self.assertEqual(exit_["exit_reason"], "series_end")
        self.assertEqual(exit_["peak_price"], "102")
        self.assertEqual(exit_["trail_stop_price"], "96.90")
        self.assertEqual(exit_["paper_proceeds"], "10.20000000")
        self.assertEqual(exit_["paper_pnl_quote"], "0.20000000")

    def test_single_price_series_flat_exit(self):
        entry, exit_ = self.lifecycle(prices("100"))
        self.assertEqual(entry["seq"], 0)
        self.assertEqual(exit_["seq"], 0)
        self.assertEqual(exit_["price"], "100")
        self.assertEqual(exit_["exit_reason"], "series_end")
        self.assertEqual(exit_["paper_pnl_quote"], "0.00000000")

    def test_receipts_append_across_runs(self):
        self.lifecycle(prices("100", "90"), run_id="paperrun01")
        self.lifecycle(prices("100", "101", "102"), run_id="paperrun02")
        lines = self.read_lines()
        self.assertEqual(
            [record["kind"] for record in lines],
            ["paper_entry", "paper_exit", "paper_entry", "paper_exit"],
        )
        self.assertEqual(
            [record["run_id"] for record in lines],
            ["paperrun01", "paperrun01", "paperrun02", "paperrun02"],
        )

    def test_lifecycle_is_deterministic(self):
        series = ("100", "105", "110", "108", "104")
        first = self.lifecycle(prices(*series))
        other_path = self.receipts.with_name("other.jsonl")
        second = self.lifecycle(prices(*series), receipt_path=other_path)
        self.assertEqual(first, second)
        self.assertEqual(self.read_lines(), self.read_lines(other_path))


class PaperInputValidationTests(NoNetworkTestCase):
    def assert_rejected(self, series, **kwargs):
        with self.assertRaises(vo.PaperInputError):
            self.lifecycle(series, **kwargs)
        self.assertFalse(self.receipts.exists(), "no receipt on rejected input")

    def test_empty_series_rejected(self):
        self.assert_rejected([])

    def test_non_positive_price_rejected(self):
        self.assert_rejected(prices("100", "0"))
        self.assert_rejected(prices("-1"))

    def test_non_decimal_price_rejected(self):
        self.assert_rejected([Decimal("100"), 101.5])

    def test_non_finite_price_rejected(self):
        self.assert_rejected([Decimal("100"), Decimal("NaN")])

    def test_trail_pct_bounds_rejected(self):
        self.assert_rejected(prices("100"), trail_pct=Decimal("0"))
        self.assert_rejected(prices("100"), trail_pct=Decimal("1"))

    def test_notional_bounds_rejected(self):
        self.assert_rejected(prices("100"), notional=Decimal("0"))
        self.assert_rejected(prices("100"), notional=Decimal("200"))

    def test_notional_below_one_quantum_rejected(self):
        self.assert_rejected(
            prices("1000000"), notional=Decimal("0.000001")
        )

    def test_bad_symbol_rejected(self):
        with self.assertRaises(vo.PaperInputError):
            vo.run_paper_lifecycle(
                "btc",
                prices("100"),
                receipt_path=self.receipts,
                run_id="papertest01",
                now_fn=fixed_now,
            )

    def test_bad_run_id_rejected(self):
        self.assert_rejected(prices("100"), run_id="bad id!")

    def test_series_length_cap_rejected(self):
        self.assert_rejected([Decimal("100")] * (vo.MAX_PRICES + 1))


class ObservationRowTests(NoNetworkTestCase):
    def test_valid_payload_builds_factual_row(self):
        row = vo.build_observation_row("BTCUSDT", valid_payload(), FIXED_TS)
        self.assertEqual(row.completed_utc, FIXED_TS)
        self.assertEqual(row.window_open_utc, "1970-01-01T00:00:00.000Z")
        self.assertEqual(row.window_close_utc, "1970-01-02T00:00:00.000Z")
        self.assertEqual(row.symbol, "BTCUSDT")
        self.assertEqual(row.last_price, Decimal("60000.10"))
        self.assertEqual(row.price_change, Decimal("-100.10"))
        self.assertEqual(row.price_change_percent, Decimal("-0.166"))
        self.assertEqual(row.high_price, Decimal("61000"))
        self.assertEqual(row.low_price, Decimal("59000"))
        self.assertEqual(row.base_volume, Decimal("12345.678"))
        self.assertEqual(row.quote_volume, Decimal("740000000.5"))
        self.assertEqual(row.trade_count, 123456)

    def assert_payload_rejected(self, **overrides):
        payload = valid_payload()
        payload.update(overrides)
        with self.assertRaises(vo.DataReadError):
            vo.build_observation_row("BTCUSDT", payload, FIXED_TS)

    def test_symbol_mismatch_rejected(self):
        self.assert_payload_rejected(symbol="ETHUSDT")

    def test_missing_price_rejected(self):
        payload = valid_payload()
        del payload["lastPrice"]
        with self.assertRaises(vo.DataReadError):
            vo.build_observation_row("BTCUSDT", payload, FIXED_TS)

    def test_non_positive_price_rejected(self):
        self.assert_payload_rejected(lastPrice="0")

    def test_non_finite_price_rejected(self):
        self.assert_payload_rejected(lastPrice="NaN")

    def test_high_below_low_rejected(self):
        self.assert_payload_rejected(highPrice="58000")

    def test_negative_volume_rejected(self):
        self.assert_payload_rejected(volume="-1")

    def test_window_close_before_open_rejected(self):
        self.assert_payload_rejected(openTime=86_400_000, closeTime=0)

    def test_boolean_count_rejected(self):
        self.assert_payload_rejected(count=True)

    def test_negative_count_rejected(self):
        self.assert_payload_rejected(count=-5)


class CliPaperTests(NoNetworkTestCase):
    def test_cli_paper_writes_receipts_and_prints_records(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = vo.main(
                [
                    "paper",
                    "--symbol",
                    "btcusdt",
                    "--prices",
                    "100",
                    "105",
                    "110",
                    "104",
                    "--trail-pct",
                    "0.05",
                    "--receipts",
                    str(self.receipts),
                    "--run-id",
                    "cli01",
                ]
            )
        self.assertEqual(code, 0)
        lines = self.read_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["symbol"], "BTCUSDT")
        self.assertEqual(lines[0]["run_id"], "cli01")
        self.assertEqual(lines[1]["exit_reason"], "trail_stop")
        printed = out.getvalue()
        self.assertIn("paper_receipts_path=" + str(self.receipts), printed)
        self.assertIn('"paper": true', printed)


if __name__ == "__main__":
    unittest.main()
