#!/usr/bin/env python3
"""Offline fixture tests for adaptive per-token tape baselines."""

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock


from study import baselines


BASE_MS = 1_767_225_600_000
SYMBOL = "AAAUSDT"
DAY = "2026-01-01"
FIXTURE_SYMBOL = json.loads(
    (Path(__file__).parent / "fixtures" / "stage_a_line197191_event.json")
    .read_text(encoding="utf-8")
)["symbol"]


def kline(index, volume):
    open_ms = BASE_MS + index * 60_000
    close_ms = open_ms + 59_999
    return [
        str(open_ms),
        "100",
        "100.1",
        "99.9",
        "100",
        "1",
        str(close_ms),
        str(volume),
        "1",
        "0.5",
        str(Decimal(str(volume)) / Decimal("2")),
        "0",
    ]


def write_tape(root):
    data_dir = root / "data"
    archive_dir = data_dir / "spot" / "daily" / "klines" / SYMBOL / "1m"
    archive_dir.mkdir(parents=True)
    rows = [
        ",".join(kline(index, "10" if index < 60 else "20"))
        for index in range(120)
    ]
    archive = archive_dir / f"{SYMBOL}-1m-2026-01-01.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(
            f"{SYMBOL}-1m-2026-01-01.csv", "\n".join(rows) + "\n"
        )
    return data_dir


def write_complete_tape(root):
    data_dir = root / "complete-data"
    archive_dir = data_dir / "spot" / "daily" / "klines" / SYMBOL / "1m"
    archive_dir.mkdir(parents=True)
    rows = [
        ",".join(kline(index, str((index // 60) + 1)))
        for index in range(1_440)
    ]
    archive = archive_dir / f"{SYMBOL}-1m-{DAY}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(
            f"{SYMBOL}-1m-{DAY}.csv", "\n".join(rows) + "\n"
        )
    return data_dir


def agg_trade_row(aggregate_id, minute, quote_size, buyer_is_maker):
    quote_size = Decimal(str(quote_size))
    timestamp_ms = BASE_MS + minute * 60_000 + aggregate_id
    return ",".join(
        (
            str(aggregate_id),
            "100",
            baselines.decimal_text(quote_size / Decimal("100")),
            str(aggregate_id),
            str(aggregate_id),
            str(timestamp_ms),
            "true" if buyer_is_maker else "false",
            "true",
        )
    )


def write_complete_agg_trades(data_dir):
    archive_dir = (
        data_dir / "spot" / "daily" / "aggTrades" / SYMBOL
    )
    archive_dir.mkdir(parents=True)
    rows = []
    aggregate_id = 1
    for hour in range(24):
        rows.append(agg_trade_row(aggregate_id, hour * 60, "3", False))
        aggregate_id += 1
        rows.append(agg_trade_row(aggregate_id, hour * 60 + 1, "1", True))
        aggregate_id += 1
    archive = archive_dir / f"{SYMBOL}-aggTrades-{DAY}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(
            f"{SYMBOL}-aggTrades-{DAY}.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker,is_best_match\n"
            + "\n".join(rows)
            + "\n",
        )
    return archive


def assert_no_float(test, value):
    test.assertNotIsInstance(value, float)
    if isinstance(value, dict):
        for nested in value.values():
            assert_no_float(test, nested)
    elif isinstance(value, list):
        for nested in value:
            assert_no_float(test, nested)


def synthetic_archive(declared_size):
    class SyntheticArchive:
        def __init__(self, archive_path, *args, **kwargs):
            expected = Path(archive_path).stem + ".csv"
            self.member = zipfile.ZipInfo(expected)
            self.member.file_size = declared_size

        def __enter__(self):
            return self

        def __exit__(self, error_type, error, traceback):
            return False

        def infolist(self):
            return [self.member]

    return SyntheticArchive


class AdaptiveBaselineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data_dir = write_tape(self.root)
        self.output_dir = self.root / "out"

    def test_filename_symbols_use_shared_validation(self):
        self.assertEqual(
            baselines._symbol_from_name(
                f"{FIXTURE_SYMBOL}-1m-{DAY}.zip"
            ),
            FIXTURE_SYMBOL,
        )
        self.assertEqual(
            baselines._symbol_from_name(f"{SYMBOL}-1m-{DAY}.zip"),
            SYMBOL,
        )
        self.assertEqual(
            baselines._symbol_from_name(f"aaausdt-1M-{DAY}.zip"),
            SYMBOL,
        )
        self.assertIsNone(
            baselines._symbol_from_name(f"../{SYMBOL}-1m-{DAY}.zip")
        )

    def test_members_above_retired_private_bound_use_shared_stream(self):
        declared_size = 512_000_001
        kline_path = self.root / f"{SYMBOL}-1m-{DAY}.zip"
        agg_path = self.root / f"{SYMBOL}-aggTrades-{DAY}.zip"
        kline_bytes = (",".join(kline(0, "10")) + "\n").encode()
        agg_bytes = (agg_trade_row(1, 0, "3", False) + "\n").encode()

        with mock.patch.object(
            baselines.zipfile,
            "ZipFile",
            synthetic_archive(declared_size),
        ), mock.patch.object(
            baselines.tape_pull,
            "stream_verified_member",
            side_effect=(iter([kline_bytes]), iter([agg_bytes])),
        ) as shared_reader:
            bars = tuple(baselines._read_archive(kline_path))
            trades = baselines._read_agg_trade_archive(
                agg_path, SYMBOL, DAY
            )

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].quote_volume, Decimal("10"))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].quote_size, Decimal("3"))
        self.assertEqual(shared_reader.call_count, 2)
        for call in shared_reader.call_args_list:
            archive, member, bound = call.args
            self.assertEqual(member.file_size, declared_size)
            self.assertIs(member, archive.member)
            self.assertEqual(bound, baselines.tape_pull.MAX_MEMBER_BYTES)

    def test_shared_ceiling_refusals_include_member_identity(self):
        ceiling = baselines.tape_pull.MAX_MEMBER_BYTES
        kline_path = self.root / f"{SYMBOL}-1m-{DAY}.zip"
        agg_path = self.root / f"{SYMBOL}-aggTrades-{DAY}.zip"
        cases = (
            (
                kline_path,
                lambda: tuple(baselines._read_archive(kline_path)),
            ),
            (
                agg_path,
                lambda: baselines._read_agg_trade_archive(
                    agg_path, SYMBOL, DAY
                ),
            ),
        )

        for path, read_archive in cases:
            expected = path.stem + ".csv"
            with self.subTest(archive=path.name), mock.patch.object(
                baselines.zipfile,
                "ZipFile",
                synthetic_archive(ceiling + 1),
            ):
                with self.assertRaises(baselines.BaselineError) as raised:
                    read_archive()

            message = str(raised.exception)
            self.assertIn(f"{path.name}:{expected}", message)
            self.assertIn(
                "archive member size is outside bounds", message
            )
            self.assertIn(f"measured={ceiling + 1}", message)
            self.assertIn(f"bound={ceiling}", message)

    def test_synthetic_tape_derives_token_regimes_and_seasonality(self):
        output = self.output_dir / "baselines.json"
        payload = baselines.run_baselines(self.data_dir, output)
        disk = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload, disk)
        self.assertEqual(payload["schema"], "adaptive-volume-baselines-v1")
        self.assertEqual(payload["windows_minutes"], [1, 10, 15, 60])
        token = payload["symbols"][SYMBOL]
        one_minute = token["rolling_volume_regimes"]["1m"]
        self.assertEqual(one_minute["quiet_floor"], "10")
        self.assertEqual(one_minute["typical"], "15")
        self.assertEqual(one_minute["hot"], "20")
        self.assertEqual(
            token["ignition_threshold"]["quiet_floor_multiple"], "2"
        )
        self.assertEqual(
            token["time_of_day_utc"]["00"]["ignition_quote_volume"],
            "20",
        )
        self.assertEqual(
            token["time_of_day_utc"]["01"]["ignition_quote_volume"],
            "40",
        )
        self.assertEqual(
            token["rolling_volume_regimes"]["60m"]["sample_count"], 61
        )
        assert_no_float(self, disk)

    def test_derivation_and_serialization_are_deterministic(self):
        first_path = self.output_dir / "first.json"
        second_path = self.output_dir / "second.json"
        first = baselines.run_baselines(self.data_dir, first_path)
        second = baselines.run_baselines(self.data_dir, second_path)

        self.assertEqual(first, second)
        self.assertEqual(
            first_path.read_bytes(), second_path.read_bytes()
        )
        self.assertEqual(first["source"]["data_dir"], "../data")

    def test_absorption_build_uses_exact_complete_kline_windows(self):
        data_dir = write_complete_tape(self.root)
        output = self.output_dir / "absorption-baseline.json"
        payload = baselines.run_absorption_baselines(
            data_dir,
            output,
            symbols=[SYMBOL],
            window_minutes=60,
            minimum_days=1,
        )
        disk = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload, disk)
        self.assertEqual(
            payload["schema"], "absorption-kline-baselines-v1"
        )
        profile = payload["symbols"][SYMBOL]
        self.assertEqual(profile["day_count"], 1)
        self.assertEqual(profile["bar_count"], 1_440)
        self.assertEqual(profile["window_sample_count"], 24)
        self.assertEqual(profile["window_quote_volume_samples"][0], "60")
        self.assertEqual(profile["window_quote_volume_samples"][-1], "1440")
        self.assertEqual(profile["historical_low"], "99.9")
        self.assertEqual(profile["historical_high"], "100.1")
        self.assertEqual(profile["close_q20"], "100")
        self.assertEqual(
            profile["components"]["tape"],
            {
                "status": "NOT-READY",
                "days_found": 0,
                "required_days": 1,
                "reason": "no_agg_trades_archives",
            },
        )
        self.assertEqual(
            payload["tape_not_ready_reasons"],
            {"no_agg_trades_archives": 1},
        )
        assert_no_float(self, disk)

    def test_absorption_build_uses_exact_agg_trade_math(self):
        data_dir = write_complete_tape(self.root)
        write_complete_agg_trades(data_dir)
        output = self.output_dir / "absorption-tape-baseline.json"

        payload = baselines.run_absorption_baselines(
            data_dir,
            output,
            symbols=[SYMBOL],
            window_minutes=60,
            minimum_days=1,
        )

        tape = payload["symbols"][SYMBOL]["components"]["tape"]
        self.assertEqual(tape["status"], "READY")
        self.assertEqual(tape["day_count"], 1)
        self.assertEqual(tape["trade_count"], 48)
        self.assertEqual(tape["window_expected_count"], 24)
        self.assertEqual(tape["window_sample_count"], 24)
        self.assertEqual(tape["baseline_taker_imbalance"], "0.5")
        self.assertEqual(
            set(tape["window_taker_imbalance_samples"]), {"0.5"}
        )
        self.assertEqual(
            tape["baseline_trade_size_median_quote"], "2"
        )
        self.assertEqual(tape["baseline_trade_size_p90_quote"], "3")
        self.assertEqual(
            set(tape["window_trade_size_median_quote_samples"]), {"2"}
        )
        self.assertEqual(
            set(tape["window_trade_size_p90_quote_samples"]), {"2.8"}
        )
        self.assertEqual(payload["tape_symbol_count"], 1)
        self.assertEqual(payload["tape_not_ready_count"], 0)
        assert_no_float(self, payload)

    def test_absorption_cli_reports_ready_and_not_ready_symbols(self):
        data_dir = write_complete_tape(self.root)
        output = self.output_dir / "coverage.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = baselines.main(
                [
                    "--absorption",
                    "--data-dir",
                    str(data_dir),
                    "--output",
                    str(output),
                    "--symbols",
                    SYMBOL,
                    "MISSINGUSDT",
                    "--min-days",
                    "1",
                ]
            )

        self.assertEqual(result, 0)
        self.assertIn(
            f"symbol={SYMBOL} | kline=READY | kline_days=1 | "
            "tape=NOT-READY | tape_days=0 | "
            "tape_reason=no_agg_trades_archives",
            stdout.getvalue(),
        )
        self.assertIn(
            "symbol=MISSINGUSDT | status=NOT-READY | days_found=0 | "
            "reason=no_kline_archives",
            stdout.getvalue(),
        )

    def test_absorption_build_does_not_fill_a_partial_day(self):
        output = self.output_dir / "not-ready.json"
        payload = baselines.run_absorption_baselines(
            self.data_dir,
            output,
            symbols=[SYMBOL],
            window_minutes=60,
            minimum_days=1,
        )

        self.assertEqual(payload["symbol_count"], 0)
        self.assertEqual(payload["not_ready_count"], 1)
        self.assertEqual(
            payload["not_ready"][SYMBOL],
            {
                "status": "NOT-READY",
                "days_found": 0,
                "reason": "insufficient_complete_days",
                "required_days": 1,
            },
        )

    def test_tape_readiness_does_not_change_kline_readiness(self):
        data_dir = write_complete_tape(self.root)
        write_complete_agg_trades(data_dir)
        output = self.output_dir / "component-readiness.json"

        payload = baselines.run_absorption_baselines(
            data_dir,
            output,
            symbols=[SYMBOL],
            window_minutes=60,
            minimum_days=1,
            minimum_tape_days=2,
        )

        profile = payload["symbols"][SYMBOL]
        self.assertEqual(
            profile["components"]["kline"],
            {"status": "READY", "day_count": 1, "required_days": 1},
        )
        self.assertEqual(
            profile["components"]["tape"],
            {
                "status": "NOT-READY",
                "days_found": 1,
                "archive_days_found": 1,
                "required_days": 2,
                "reason": "insufficient_aligned_complete_days",
            },
        )
        self.assertEqual(payload["symbol_count"], 1)
        self.assertEqual(payload["tape_symbol_count"], 0)
        self.assertEqual(
            payload["tape_not_ready_reasons"],
            {"insufficient_aligned_complete_days": 1},
        )


if __name__ == "__main__":
    unittest.main()
