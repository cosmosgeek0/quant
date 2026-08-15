#!/usr/bin/env python3
"""Synthetic local-tape tests for Volume Dataset v2 Stage B."""

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path


from study import stage_b


BASE_MS = 1_767_225_600_000
DAY = "2026-01-01"
SYMBOL = "AAAUSDT"
FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "stage_a_line197191_event.json"
)


def trade(
    aggregate_id,
    minute,
    quote_size,
    *,
    buyer_is_maker,
    price="1",
    offset_ms=1_000,
):
    price_value = Decimal(price)
    return stage_b.AggTrade(
        SYMBOL,
        aggregate_id,
        BASE_MS + minute * 60_000 + offset_ms,
        price_value,
        Decimal(quote_size) / price_value,
        buyer_is_maker,
    )


def agg_row(item):
    return ",".join(
        (
            str(item.aggregate_id),
            str(item.price),
            str(item.quantity),
            str(item.aggregate_id * 10),
            str(item.aggregate_id * 10),
            str(item.timestamp_ms),
            str(item.buyer_is_maker).lower(),
            "true",
        )
    )


def write_agg_zip(root, symbol, rows):
    target = (
        root
        / "spot"
        / "daily"
        / "aggTrades"
        / symbol
        / f"{symbol}-aggTrades-{DAY}.zip"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{symbol}-aggTrades-{DAY}.csv",
            "\n".join(agg_row(item) for item in rows) + "\n",
        )
    return target


def write_kline_zip(root, symbol, ranges):
    target = (
        root
        / "spot"
        / "daily"
        / "klines"
        / symbol
        / "1m"
        / f"{symbol}-1m-{DAY}.zip"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for minute, (high, low) in enumerate(ranges):
        open_ms = BASE_MS + minute * 60_000
        rows.append(
            ",".join(
                (
                    str(open_ms),
                    str(low),
                    str(high),
                    str(low),
                    str(low),
                    "1",
                    str(open_ms + 59_999),
                    "1",
                )
            )
        )
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{symbol}-1m-{DAY}.csv", "\n".join(rows) + "\n"
        )
    return target


def no_float(test, value):
    if isinstance(value, dict):
        for item in value.values():
            no_float(test, item)
    elif isinstance(value, list):
        for item in value:
            no_float(test, item)
    else:
        test.assertNotIsInstance(value, float)


class StageBSymbolValidationTests(unittest.TestCase):
    @staticmethod
    def window(symbol):
        return stage_b._window_spec(
            {
                "window_id": "symbol-check",
                "symbol": symbol,
                "venue_class": "spot_only",
                "venue": "spot",
                "window_start_ms": BASE_MS,
                "event_ms": BASE_MS + 60_000,
                "window_end_ms": BASE_MS + 120_000,
            },
            0,
        )

    def test_fixture_and_plain_ascii_symbols_pass_value_validation(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(self.window(fixture["symbol"]).symbol, fixture["symbol"])
        self.assertEqual(self.window(SYMBOL).symbol, SYMBOL)
        self.assertEqual(self.window(SYMBOL.lower()).symbol, SYMBOL)

    def test_traversal_symbol_is_refused_by_value_and_filename_validation(self):
        with self.assertRaisesRegex(
            stage_b.StageBError, "^window 0 has an invalid symbol$"
        ):
            self.window("../AAAUSDT")

        filename_cases = (
            (stage_b.AGG_ARCHIVE, f"AAA..USDT-aggTrades-{DAY}.zip"),
            (stage_b.KLINE_ARCHIVE, f"AAA..USDT-1m-{DAY}.zip"),
        )
        for pattern, filename in filename_cases:
            with self.subTest(filename=filename):
                self.assertIsNone(
                    stage_b._archive_name_match(Path(filename), pattern)
                )
                self.assertIsNone(
                    stage_b._archive_name_match(Path("unrelated.zip"), pattern)
                )

    def test_fixture_symbol_is_extracted_from_both_archive_filenames(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        symbol = fixture["symbol"]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        agg_path = write_agg_zip(
            root,
            symbol,
            (trade(1, 0, "1", buyer_is_maker=False),),
        )
        kline_path = write_kline_zip(root, symbol, (("101", "100"),))

        agg_match = stage_b._archive_name_match(
            agg_path, stage_b.AGG_ARCHIVE
        )
        kline_match = stage_b._archive_name_match(
            kline_path, stage_b.KLINE_ARCHIVE
        )
        self.assertIsNotNone(agg_match)
        self.assertIsNotNone(kline_match)
        self.assertEqual(agg_match[1], symbol)
        self.assertEqual(kline_match[1], symbol)

        trades = stage_b.load_agg_trades(
            (agg_path,), symbol, BASE_MS, BASE_MS + 60_000
        )
        bars = stage_b.load_klines(
            (kline_path,), symbol, BASE_MS, BASE_MS + 60_000
        )
        self.assertEqual(trades[0].symbol, symbol)
        self.assertEqual(bars[0].symbol, symbol)


class StageBFeatureTests(unittest.TestCase):
    def test_taker_imbalance_is_exact_quote_weighted_maker_side_truth(self):
        trades = (
            trade(1, 0, "30", buyer_is_maker=False),
            trade(2, 0, "10", buyer_is_maker=False),
            trade(3, 0, "10", buyer_is_maker=True),
        )

        buy, sell, buy_count, sell_count = stage_b.taker_quote_totals(trades)

        self.assertEqual((buy, sell), (Decimal("40"), Decimal("10")))
        self.assertEqual((buy_count, sell_count), (2, 1))
        self.assertEqual(stage_b.taker_imbalance(trades), Decimal("0.6"))

    def test_trade_size_quantile_shift_is_exact_at_every_quantile(self):
        baseline = tuple(
            trade(index, 0, str(size), buyer_is_maker=False)
            for index, size in enumerate((1, 2, 3, 4, 5), start=1)
        )
        scoring = tuple(
            trade(index + 10, 1, str(size * 2), buyer_is_maker=False)
            for index, size in enumerate((1, 2, 3, 4, 5), start=1)
        )

        shifted = stage_b.size_quantile_shift(baseline, scoring)

        self.assertEqual(shifted["before"]["q50"], Decimal("3"))
        self.assertEqual(shifted["before"]["q90"], Decimal("4.6"))
        self.assertEqual(shifted["after"]["q90"], Decimal("9.2"))
        for name, _, _ in stage_b.QUANTILES:
            self.assertEqual(shifted["ratio"][name], Decimal("2"))
            self.assertEqual(
                shifted["delta"][name], shifted["before"][name]
            )

    def test_absorption_score_is_high_when_buying_does_not_move_price(self):
        absorbed = tuple(
            trade(index, index, "10", buyer_is_maker=False, price="100")
            for index in range(1, 6)
        ) + (trade(8, 6, "5", buyer_is_maker=True, price="100"),)
        moved = (
            trade(20, 1, "10", buyer_is_maker=False, price="100"),
            trade(21, 2, "10", buyer_is_maker=False, price="110"),
        )

        self.assertGreaterEqual(
            stage_b.absorption_score(absorbed), Decimal("90")
        )
        self.assertLess(stage_b.absorption_score(moved), Decimal("10"))

    def test_persistence_is_measured_after_first_print_not_assumed(self):
        scoring = (
            trade(1, 0, "1", buyer_is_maker=False),
            trade(2, 1, "1", buyer_is_maker=False),
            trade(3, 3, "1", buyer_is_maker=True),
        )

        result = stage_b.persistence_metrics(
            scoring, BASE_MS, BASE_MS + 5 * 60_000
        )

        self.assertEqual(result["measured_minutes"], 4)
        self.assertEqual(result["active_minutes"], 2)
        self.assertEqual(result["buy_dominant_minutes"], 1)
        self.assertEqual(result["active_ratio"], Decimal("0.5"))
        self.assertEqual(result["buy_dominant_ratio"], Decimal("0.25"))
        self.assertEqual(result["score"], Decimal("50"))

    def test_local_manifest_run_writes_features_and_honest_missing_skip(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        data = root / "data"
        data.mkdir()
        available = (
            trade(1, 0, "2", buyer_is_maker=True, price="100"),
            trade(2, 1, "4", buyer_is_maker=False, price="100"),
            trade(3, 2, "8", buyer_is_maker=False, price="100"),
            trade(4, 3, "8", buyer_is_maker=False, price="100"),
            trade(5, 5, "8", buyer_is_maker=False, price="100"),
        )
        write_agg_zip(data, SYMBOL, available)
        manifest = root / "windows_manifest.json"
        output = root / "features.jsonl"
        manifest.write_text(
            json.dumps(
                {
                    "windows": [
                        {
                            "window_id": "runner-a",
                            "symbol": SYMBOL,
                            "venue_class": "spot_only",
                            "venue": "spot",
                            "label": "GenuineRunner",
                            "window_start_ms": BASE_MS,
                            "event_ms": BASE_MS + 2 * 60_000,
                            "window_end_ms": BASE_MS + 6 * 60_000,
                        },
                        {
                            "window_id": "control-missing",
                            "symbol": "BBBUSDT",
                            "venue_class": "spot_only",
                            "venue": "spot",
                            "label": "control",
                            "window_start_ms": BASE_MS,
                            "event_ms": BASE_MS + 2 * 60_000,
                            "window_end_ms": BASE_MS + 6 * 60_000,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        returned = stage_b.run_stage_b(manifest, data, output)
        disk = tuple(
            json.loads(line)
            for line in output.read_text(encoding="utf-8").splitlines()
        )

        self.assertEqual(returned, disk)
        self.assertEqual(disk[0]["status"], "ok")
        self.assertEqual(disk[0]["features"]["taker_imbalance"], "1")
        self.assertEqual(
            disk[0]["features"]["trade_size_quantiles_quote"]["ratio"]["q50"],
            "2.6666666666666666666666666666666666666666666666667",
        )
        self.assertEqual(disk[0]["features"]["persistence_score"], "66.666666666666666666666666666666666666666666666667")
        self.assertEqual(disk[0]["features"]["absorption_score"], "100")
        self.assertIn("spread_proxy_missing", disk[0]["unavailable"])
        self.assertEqual(disk[1]["status"], "skipped")
        self.assertEqual(disk[1]["skip_reason"], "missing_agg_trades_zip")
        self.assertEqual(len(disk[1]["missing_files"]), 1)
        no_float(self, disk)

    def test_available_kline_funding_and_oi_context_are_emitted(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        data = root / "data"
        data.mkdir()
        write_agg_zip(
            data,
            SYMBOL,
            (
                trade(1, 0, "1", buyer_is_maker=True, price="100"),
                trade(2, 2, "2", buyer_is_maker=False, price="100"),
            ),
        )
        write_kline_zip(
            data,
            SYMBOL,
            (
                ("101", "100"),
                ("101", "100"),
                ("100.5", "100"),
                ("100.5", "100"),
            ),
        )
        funding = data / f"{SYMBOL}-funding.jsonl"
        funding.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "symbol": SYMBOL,
                            "fundingTime": BASE_MS,
                            "fundingRate": "0.001",
                        }
                    ),
                    json.dumps(
                        {
                            "symbol": SYMBOL,
                            "fundingTime": BASE_MS + 3 * 60_000,
                            "fundingRate": "0.002",
                        }
                    ),
                )
            ),
            encoding="utf-8",
        )
        oi = data / f"{SYMBOL}-open-interest.csv"
        oi.write_text(
            "timestamp,symbol,sumOpenInterest\n"
            f"{BASE_MS},{SYMBOL},100\n"
            f"{BASE_MS + 3 * 60_000},{SYMBOL},125\n",
            encoding="utf-8",
        )
        manifest = root / "windows_manifest.json"
        output = root / "features.jsonl"
        manifest.write_text(
            json.dumps(
                {
                    "windows": [
                        {
                            "window_id": "with-context",
                            "symbol": SYMBOL,
                            "venue_class": "both",
                            "venue": "spot",
                            "window_start_ms": BASE_MS,
                            "event_ms": BASE_MS + 2 * 60_000,
                            "window_end_ms": BASE_MS + 4 * 60_000,
                            "funding_file": funding.name,
                            "open_interest_file": oi.name,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        row = stage_b.run_stage_b(manifest, data, output)[0]
        features = row["features"]

        self.assertEqual(row["status"], "ok")
        self.assertEqual(features["spread_proxy_before_pct"], "1")
        self.assertEqual(features["spread_proxy_pct"], "0.5")
        self.assertEqual(features["funding_rate_before"], "0.001")
        self.assertEqual(features["funding_rate_after"], "0.002")
        self.assertEqual(features["funding_rate_change"], "0.001")
        self.assertEqual(features["open_interest_before"], "100")
        self.assertEqual(features["open_interest_after"], "125")
        self.assertEqual(features["open_interest_change_pct"], "25")
        self.assertEqual(row["unavailable"], [])
        no_float(self, row)

    def test_help_is_available_without_touching_data(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                stage_b.argument_parser().parse_args(["--help"])
        self.assertEqual(raised.exception.code, 0)

    def test_absent_data_directory_still_emits_honest_skip_row(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        manifest = root / "windows_manifest.json"
        output = root / "features.jsonl"
        manifest.write_text(
            json.dumps(
                [
                    {
                        "window_id": "absent-data",
                        "symbol": SYMBOL,
                        "venue_class": "spot_only",
                        "event_ms": BASE_MS + 60_000,
                    }
                ]
            ),
            encoding="utf-8",
        )

        row = stage_b.run_stage_b(manifest, root / "missing", output)[0]

        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["skip_reason"], "missing_agg_trades_zip")
        self.assertTrue(row["missing_files"])


if __name__ == "__main__":
    unittest.main()
