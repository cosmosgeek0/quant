#!/usr/bin/env python3
"""Synthetic offline tests for long-horizon kept-cohort re-scoring."""

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from decimal import Decimal, localcontext
from pathlib import Path


from study import baselines
from study import rescore_horizons as rescore


BASE_MS = 1_767_225_600_000
SYMBOL = "SLOWUSDT"
DAY = "2026-01-01"
FIXTURE_SYMBOL = json.loads(
    (Path(__file__).parent / "fixtures" / "stage_a_line197191_event.json")
    .read_text(encoding="utf-8")
)["symbol"]


def decimal_text(value):
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def close_price(index):
    if index <= 10:
        return Decimal("100")
    if index <= 15:
        return Decimal("99.5")
    if index <= 130:
        with localcontext() as context:
            context.prec = 50
            return Decimal("99.5") + (
                Decimal(index - 15) * Decimal("1.5") / Decimal("115")
            )
    if index <= 250:
        with localcontext() as context:
            context.prec = 50
            return Decimal("101") + (
                Decimal(index - 130) * Decimal("2") / Decimal("120")
            )
    with localcontext() as context:
        context.prec = 50
        return Decimal("103") + (
            Decimal(index - 250) * Decimal("2") / Decimal("120")
        )


def kline(index):
    close = close_price(index)
    open_ms = BASE_MS + index * 60_000
    return [
        str(open_ms),
        decimal_text(close),
        decimal_text(close + Decimal("0.05")),
        decimal_text(close - Decimal("0.05")),
        decimal_text(close),
        "1",
        str(open_ms + 59_999),
        "100",
        "1",
        "0.5",
        "50",
        "0",
    ]


def write_tape(root):
    data_dir = root / "data"
    archive_dir = data_dir / "spot/daily/klines" / SYMBOL / "1m"
    archive_dir.mkdir(parents=True)
    archive = archive_dir / f"{SYMBOL}-1m-2026-01-01.zip"
    payload = "\n".join(",".join(kline(index)) for index in range(371))
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(f"{SYMBOL}-1m-2026-01-01.csv", payload + "\n")
    return data_dir


def write_filtered(root):
    path = root / "filtered-2026-01-01.jsonl"
    utc = baselines.utc_text(BASE_MS + 10 * 60_000 + 59_999)
    rows = (
        {
            "kind": "filtered_ignition",
            "symbol": SYMBOL,
            "utc": utc,
            "decision": "keep",
        },
        {
            "kind": "summary",
            "filter": "adaptive-noise-filter-v1",
            "kept": 1,
        },
    )
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def ladder_parameters():
    return rescore.LadderParameters(
        first_target_pct=Decimal("1"),
        second_target_pct=Decimal("3"),
        trail_pct=Decimal("2"),
        first_weight=Decimal("1"),
        second_weight=Decimal("1"),
        trail_weight=Decimal("1"),
    )


class RescoreHorizonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data_dir = write_tape(self.root)
        self.filtered = write_filtered(self.root)
        self.fees = (
            rescore.FeeModel("taker_round_trip", Decimal("0.30")),
            rescore.FeeModel("maker_exit", Decimal("0.20")),
        )

    def test_archive_and_value_symbols_use_shared_validation(self):
        self.assertEqual(
            rescore._archive_name_parts(
                f"{FIXTURE_SYMBOL}-1m-{DAY}.zip"
            ),
            (FIXTURE_SYMBOL, DAY),
        )
        self.assertEqual(
            rescore._archive_name_parts(f"{SYMBOL}-1m-{DAY}.zip"),
            (SYMBOL, DAY),
        )
        self.assertIsNone(
            rescore._archive_name_parts(
                f"{SYMBOL.lower()}-1m-{DAY}.zip"
            )
        )
        self.assertIsNone(
            rescore._archive_name_parts(f"..-1m-{DAY}.zip")
        )
        self.assertEqual(
            rescore._validated_symbol(
                FIXTURE_SYMBOL, "cohort has invalid symbol"
            ),
            FIXTURE_SYMBOL,
        )
        self.assertEqual(
            rescore._validated_symbol(SYMBOL, "cohort has invalid symbol"),
            SYMBOL,
        )
        with self.assertRaisesRegex(
            rescore.RescoreError, "cohort has invalid symbol"
        ):
            rescore._validated_symbol(
                f"../{SYMBOL}", "cohort has invalid symbol"
            )

        (self.data_dir / f"..-1m-{DAY}.zip").touch()
        trades = rescore.load_kept_trades((self.filtered,))
        output = io.StringIO()
        with redirect_stdout(output):
            tape = rescore.load_relevant_tape(self.data_dir, trades, 5)
        self.assertEqual(tuple(tape), (SYMBOL,))
        self.assertEqual(
            output.getvalue().strip(),
            "RESCORE | tape=klines | skipped=1 | "
            "reason=invalid_symbol_filename",
        )

    def test_slow_runner_is_negative_at_5m_but_positive_at_6h_ladder(self):
        result = rescore.run_rescore(
            (self.filtered,),
            self.data_dir,
            (5, 360),
            ladder_parameters(),
            self.fees,
        )
        cells = {
            (
                cell["horizon_minutes"],
                cell["exit_model"],
                cell["fee_model"],
            ): cell
            for cell in result["grid"]
        }

        five = cells[(5, "ladder", "taker_round_trip")]
        six_hour = cells[(360, "ladder", "taker_round_trip")]
        self.assertEqual(five["n"], 1)
        self.assertEqual(five["median_pct"], "-0.5")
        self.assertEqual(five["win_pct"], "0")
        self.assertEqual(six_hour["n"], 1)
        self.assertEqual(six_hour["median_pct"], "3")
        self.assertEqual(six_hour["win_pct"], "100")
        self.assertEqual(result["skipped_by_horizon"], {5: 0, 360: 0})

    def test_round_trip_fee_models_are_subtracted_from_expectancy(self):
        gross = {
            (60, "simple"): (Decimal("1"), Decimal("-1")),
            (60, "ladder"): (Decimal("3"),),
        }
        grid = rescore.build_summary_grid(gross, (60,), self.fees)
        cells = {
            (cell["exit_model"], cell["fee_model"]): cell
            for cell in grid
        }

        taker_simple = cells[("simple", "taker_round_trip")]
        maker_simple = cells[("simple", "maker_exit")]
        self.assertEqual(taker_simple["n"], 2)
        self.assertEqual(taker_simple["median_pct"], "0")
        self.assertEqual(taker_simple["win_pct"], "50")
        self.assertEqual(taker_simple["net_expectancy_pct"], "-0.3")
        self.assertEqual(maker_simple["net_expectancy_pct"], "-0.2")
        self.assertEqual(
            cells[("ladder", "taker_round_trip")]["net_expectancy_pct"],
            "2.7",
        )
        self.assertEqual(
            cells[("ladder", "maker_exit")]["net_expectancy_pct"],
            "2.8",
        )

    def test_cli_exposes_ladder_weights_targets_trail_and_fees(self):
        args = rescore.build_parser().parse_args(
            [
                "--first-target-pct",
                "1.5",
                "--second-target-pct",
                "4",
                "--trail-pct",
                "2.5",
                "--first-weight",
                "2",
                "--second-weight",
                "1",
                "--trail-weight",
                "3",
                "--taker-round-trip-fee-pct",
                "0.31",
                "--maker-exit-round-trip-fee-pct",
                "0.19",
            ]
        )
        parameters = rescore._parameters_from_args(args)
        fees = rescore._fees_from_args(args)

        self.assertEqual(parameters.first_target_pct, Decimal("1.5"))
        self.assertEqual(parameters.second_target_pct, Decimal("4"))
        self.assertEqual(parameters.trail_pct, Decimal("2.5"))
        self.assertEqual(parameters.first_weight, Decimal("2"))
        self.assertEqual(parameters.second_weight, Decimal("1"))
        self.assertEqual(parameters.trail_weight, Decimal("3"))
        self.assertEqual(fees[0].round_trip_pct, Decimal("0.31"))
        self.assertEqual(fees[1].round_trip_pct, Decimal("0.19"))


if __name__ == "__main__":
    unittest.main()
