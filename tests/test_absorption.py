#!/usr/bin/env python3
"""Offline fixtures for the pre-ignition absorption study."""

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock


from study import absorption
from study import baselines
from study import tape_pull


BASE_MS = 1_767_225_600_000
DAY = "2026-01-01"
SYMBOL = "AAAUSDT"
FIXTURE_SYMBOL = json.loads(
    (Path(__file__).parent / "fixtures" / "stage_a_line197191_event.json")
    .read_text(encoding="utf-8")
)["symbol"]


def bar(index, close, *, spread="0.5", volume="20"):
    close = Decimal(str(close))
    spread = Decimal(str(spread))
    open_ms = BASE_MS + index * 60_000
    return absorption.KlineBar(
        SYMBOL,
        open_ms,
        open_ms + 59_999,
        close,
        close + spread,
        close - spread,
        close,
        Decimal(str(volume)),
    )


def trade(aggregate_id, minute, quote_size, *, buyer_is_maker):
    return absorption.AggTrade(
        SYMBOL,
        aggregate_id,
        BASE_MS + minute * 60_000 + aggregate_id % 1_000,
        Decimal("100"),
        Decimal(str(quote_size)) / Decimal("100"),
        buyer_is_maker,
    )


def baseline_fixture():
    closes = ("120", "118", "116", "114", "112", "110",
              "108", "106", "104", "102", "101", "100.5")
    bars = tuple(bar(index, close) for index, close in enumerate(closes))
    trades = []
    aggregate_id = 1
    for minute in range(len(bars)):
        trades.append(
            trade(aggregate_id, minute, "10", buyer_is_maker=False)
        )
        aggregate_id += 1
        trades.append(
            trade(aggregate_id, minute, "10", buyer_is_maker=True)
        )
        aggregate_id += 1
    return bars, tuple(trades), aggregate_id


def accumulation_fixture():
    baseline_bars, baseline_trades, aggregate_id = baseline_fixture()
    window_bars = (
        bar(12, "100", spread="0.05", volume="20"),
        bar(13, "100", spread="0.05", volume="20"),
        bar(14, "100", spread="0.05", volume="20"),
        bar(15, "100", spread="0.05", volume="10"),
    )
    window_trades = (
        trade(aggregate_id, 12, "30", buyer_is_maker=False),
        trade(aggregate_id + 1, 13, "30", buyer_is_maker=False),
        trade(aggregate_id + 2, 15, "10", buyer_is_maker=True),
    )
    return baseline_bars, window_bars, baseline_trades, window_trades


def no_float(test, value):
    test.assertNotIsInstance(value, float)
    if isinstance(value, dict):
        for nested in value.values():
            no_float(test, nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            no_float(test, nested)


def kline_csv_row(item):
    return ",".join(
        (
            str(item.open_ms),
            absorption.decimal_text(item.open),
            absorption.decimal_text(item.high),
            absorption.decimal_text(item.low),
            absorption.decimal_text(item.close),
            "1",
            str(item.close_ms),
            absorption.decimal_text(item.quote_volume),
            "1",
            "0.5",
            "1",
            "0",
        )
    )


def agg_trade_csv_row(item):
    return ",".join(
        (
            str(item.aggregate_id),
            absorption.decimal_text(item.price),
            absorption.decimal_text(item.quantity),
            str(item.aggregate_id),
            str(item.aggregate_id),
            str(item.timestamp_ms),
            "true" if item.buyer_is_maker else "false",
            "true",
        )
    )


def write_tape(root, bars, trades):
    kline_path = (
        root
        / "spot"
        / "daily"
        / "klines"
        / SYMBOL
        / "1m"
        / f"{SYMBOL}-1m-{DAY}.zip"
    )
    kline_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(kline_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{SYMBOL}-1m-{DAY}.csv",
            "\n".join(kline_csv_row(item) for item in bars) + "\n",
        )
    trade_path = (
        root
        / "spot"
        / "daily"
        / "aggTrades"
        / SYMBOL
        / f"{SYMBOL}-aggTrades-{DAY}.zip"
    )
    trade_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(trade_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{SYMBOL}-aggTrades-{DAY}.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker,is_best_match\n"
            + "\n".join(agg_trade_csv_row(item) for item in trades)
            + "\n",
        )
    return kline_path, trade_path


def write_complete_absorption_tape(root):
    bars = tuple(
        bar(
            index,
            "100",
            spread="0.1",
            volume=str((index // 60) + 1),
        )
        for index in range(1_440)
    )
    trades = (
        trade(1, 0, "10", buyer_is_maker=False),
        trade(2, 59, "5", buyer_is_maker=True),
    )
    write_tape(root, bars, trades)
    return bars, trades


class AbsorptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_archive_symbols_validate_and_invalid_skips_are_counted(self):
        self.assertEqual(
            absorption._archive_name_parts(
                f"{FIXTURE_SYMBOL}-1m-{DAY}.zip",
                absorption.KLINE_ARCHIVE,
            ),
            (FIXTURE_SYMBOL, DAY),
        )
        self.assertEqual(
            absorption._archive_name_parts(
                f"{FIXTURE_SYMBOL}-aggTrades-{DAY}.zip",
                absorption.AGG_TRADES_ARCHIVE,
            ),
            (FIXTURE_SYMBOL, DAY),
        )
        self.assertEqual(
            absorption._archive_name_parts(
                f"{SYMBOL}-1m-{DAY}.zip", absorption.KLINE_ARCHIVE
            ),
            (SYMBOL, DAY),
        )
        self.assertIsNone(
            absorption._archive_name_parts(
                f"{SYMBOL.lower()}-1m-{DAY}.zip",
                absorption.KLINE_ARCHIVE,
            )
        )
        self.assertIsNone(
            absorption._archive_name_parts(
                f"..-1m-{DAY}.zip", absorption.KLINE_ARCHIVE
            )
        )

        bars, trades, _aggregate_id = baseline_fixture()
        write_tape(self.root, bars, trades)
        (self.root / f"..-1m-{DAY}.zip").touch()
        (self.root / f"..-aggTrades-{DAY}.zip").touch()

        kline_output = io.StringIO()
        with redirect_stdout(kline_output):
            klines = absorption.load_klines(self.root)
        self.assertEqual(tuple(klines), (SYMBOL,))
        self.assertEqual(
            kline_output.getvalue().strip(),
            "ABSORPTION | tape=klines | skipped=1 | "
            "reason=invalid_symbol_filename",
        )

        trade_output = io.StringIO()
        with redirect_stdout(trade_output):
            loaded_trades = absorption.load_agg_trades(self.root)
        self.assertEqual(tuple(loaded_trades), (SYMBOL,))
        self.assertEqual(
            trade_output.getvalue().strip(),
            "ABSORPTION | tape=aggTrades | skipped=1 | "
            "reason=invalid_symbol_filename",
        )

    def test_taker_imbalance_math_is_exact(self):
        trades = (
            trade(1, 0, "3", buyer_is_maker=False),
            trade(2, 0, "1", buyer_is_maker=True),
        )
        buy, sell, buy_count, sell_count = absorption.taker_quote_totals(
            trades
        )
        self.assertEqual((buy, sell), (Decimal("3"), Decimal("1")))
        self.assertEqual((buy_count, sell_count), (1, 1))
        self.assertEqual(absorption.taker_imbalance(trades), Decimal("0.5"))

    def test_flat_at_lows_with_quiet_buy_size_shift_scores_high(self):
        baseline_bars, window_bars, baseline_trades, window_trades = (
            accumulation_fixture()
        )
        row = absorption.score_absorption_window(
            SYMBOL,
            baseline_bars,
            window_bars,
            baseline_trades,
            window_trades,
        )

        self.assertGreaterEqual(
            Decimal(row["absorption_score"]), Decimal("80")
        )
        self.assertTrue(row["features"]["at_multiweek_lows"])
        self.assertTrue(row["features"]["price_flat"])
        self.assertFalse(row["features"]["pumping"])
        self.assertEqual(row["features"]["taker_buy_quote"], "60")
        self.assertEqual(row["features"]["taker_sell_quote"], "10")
        self.assertEqual(
            Decimal(row["features"]["taker_imbalance"]),
            absorption.taker_imbalance(window_trades),
        )
        self.assertEqual(
            row["features"]["trade_size_median_shift_ratio"], "3"
        )
        no_float(self, row)

    def test_already_pumping_window_scores_zero(self):
        baseline_bars, baseline_trades, aggregate_id = baseline_fixture()
        window_bars = (
            bar(12, "102", spread="1", volume="50"),
            bar(13, "108", spread="1", volume="50"),
            bar(14, "115", spread="1", volume="50"),
            bar(15, "120", spread="1", volume="50"),
        )
        window_trades = tuple(
            trade(
                aggregate_id + offset,
                12 + offset,
                "50",
                buyer_is_maker=False,
            )
            for offset in range(4)
        )
        row = absorption.score_absorption_window(
            SYMBOL,
            baseline_bars,
            window_bars,
            baseline_trades,
            window_trades,
        )

        self.assertEqual(row["absorption_score"], "0")
        self.assertTrue(row["features"]["pumping"])
        self.assertFalse(row["features"]["at_multiweek_lows"])

    def test_zip_tape_emits_jsonl_with_all_forward_horizons(self):
        baseline_bars, window_bars, baseline_trades, window_trades = (
            accumulation_fixture()
        )
        bars = list(baseline_bars + window_bars)
        for index in range(16, 16 + 1_440):
            close = Decimal("100")
            if index == 75:
                close = Decimal("101")
            elif index == 375:
                close = Decimal("106")
            elif index == 1_455:
                close = Decimal("124")
            bars.append(bar(index, close, spread="0.01", volume="1"))
        trades = baseline_trades + window_trades
        write_tape(self.root, bars, trades)
        output = self.root / "out" / "absorption-2026-01-02.jsonl"

        returned = absorption.run_absorption(
            self.root,
            output,
            window_minutes=4,
            lookback_minutes=12,
            step_minutes=1_440,
        )
        disk = tuple(
            json.loads(line)
            for line in output.read_text(encoding="utf-8").splitlines()
        )

        self.assertEqual(returned, disk)
        first = disk[0]
        self.assertEqual(
            first["forward_outcomes"]["1h"]["close_return_pct"], "1"
        )
        self.assertEqual(
            first["forward_outcomes"]["6h"]["close_return_pct"], "6"
        )
        self.assertEqual(
            first["forward_outcomes"]["24h"]["close_return_pct"], "24"
        )
        no_float(self, disk)

    def test_built_baseline_allows_a_short_tape_to_be_evaluated(self):
        write_complete_absorption_tape(self.root)
        baseline_path = self.root / "out" / "historical-baseline.json"
        baselines.run_absorption_baselines(
            self.root,
            baseline_path,
            symbols=[SYMBOL],
            window_minutes=60,
            minimum_days=1,
        )
        output = self.root / "out" / "absorption-built.jsonl"

        rows = absorption.run_absorption(
            self.root,
            output,
            window_minutes=60,
            step_minutes=1_440,
            baseline_path=baseline_path,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], SYMBOL)
        self.assertEqual(rows[0]["baseline_source"], "historical_file")
        self.assertEqual(rows[0]["baseline_days"], 1)
        self.assertEqual(rows[0]["baseline_minutes"], 1_440)
        self.assertIsNotNone(
            rows[0]["features"]["baseline_window_volume_floor"]
        )
        self.assertIn("trade_size_shift", rows[0]["unknown_features"])
        self.assertTrue(output.is_file())
        no_float(self, rows)

    def test_short_tape_still_refuses_without_a_built_baseline(self):
        write_complete_absorption_tape(self.root)
        output = self.root / "out" / "absorption-refusal.jsonl"

        with self.assertRaisesRegex(
            absorption.AbsorptionError,
            "tape has no proved absorption scoring windows",
        ):
            absorption.run_absorption(
                self.root,
                output,
                window_minutes=60,
                step_minutes=1_440,
            )
        self.assertFalse(output.exists())

    def test_agg_trades_pull_path_and_existing_zip_are_idempotent(self):
        day = "2026-01-01"
        expected = self.root / (
            "spot/daily/aggTrades/AAAUSDT/"
            "AAAUSDT-aggTrades-2026-01-01.zip"
        )
        self.assertEqual(
            tape_pull.agg_trades_url(SYMBOL, day),
            "https://data.binance.vision/data/spot/daily/aggTrades/"
            "AAAUSDT/AAAUSDT-aggTrades-2026-01-01.zip",
        )
        self.assertEqual(
            tape_pull.agg_trades_path(self.root, SYMBOL, day), expected
        )
        expected.parent.mkdir(parents=True)
        with zipfile.ZipFile(expected, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "AAAUSDT-aggTrades-2026-01-01.csv",
                "1,100,0.1,1,1,1767225600000,false,true\n",
            )
        with mock.patch.object(
            tape_pull, "_open_get", side_effect=AssertionError("network")
        ):
            result = tape_pull.download_agg_trades(
                SYMBOL, day, self.root
            )
        self.assertEqual(result.status, "SKIPPED")
        self.assertEqual(result.path, expected)
        tape_pull.verify_agg_trades_zip(expected, SYMBOL, day)
        self.assertTrue(
            tape_pull.build_parser().parse_args(["--agg-trades"]).agg_trades
        )
        with mock.patch.object(tape_pull, "pull_tapes") as pull:
            self.assertEqual(
                tape_pull.main(
                    [
                        "--cohort",
                        "1",
                        "--days",
                        "1",
                        "--dest",
                        str(self.root),
                        "--agg-trades",
                    ]
                ),
                0,
            )
        pull.assert_called_once_with(1, 1, self.root, agg_trades=True)


if __name__ == "__main__":
    unittest.main()
