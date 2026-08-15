#!/usr/bin/env python3
"""Synthetic offline tests for Volume Dataset v2 validation."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock


from study import validate


WINDOW_START = date(2026, 1, 1)


def cohort_fixture():
    prefixes = {
        "spot_only": "S",
        "futures_only": "F",
        "both": "B",
    }
    return tuple(
        validate.CohortMember(f"{prefix}{index:02d}USDT", venue_class)
        for venue_class, prefix in prefixes.items()
        for index in range(10)
    )


def record(
    event_id,
    symbol,
    venue_class,
    day_offset,
    *,
    label="GenuineRunner",
    score="0.8",
    latency="2",
    mfe="10",
    mae="-2",
    peak="5",
    gross="4",
    costs="1",
    minute_of_day=0,
):
    gross_value = Decimal(gross)
    cost_value = Decimal(costs)
    return validate.ValidationRecord(
        event_id=event_id,
        symbol=symbol,
        venue_class=venue_class,
        event_utc=(
            datetime.combine(
                WINDOW_START + timedelta(days=day_offset),
                datetime.min.time(),
                timezone.utc,
            )
            + timedelta(minutes=minute_of_day)
        ),
        label=label,
        runner_score=Decimal(score),
        entry_latency_min=Decimal(latency),
        mfe_pct=Decimal(mfe),
        mae_pct=Decimal(mae),
        time_to_peak_min=Decimal(peak),
        gross_return_pct=gross_value,
        all_costs_pct=cost_value,
        net_return_pct=gross_value - cost_value,
    )


def policy_json_row(item):
    gross = item.gross_return_pct
    net = item.net_return_pct
    return {
        "kind": "trade",
        "status": "CLOSED",
        "event_id": item.event_id,
        "symbol": item.symbol,
        "venue_class": item.venue_class,
        "event_utc": item.event_utc.isoformat().replace("+00:00", "Z"),
        "label": item.label,
        "policy": {
            "entry_delay_min": 1,
            "stages": [
                {"fraction": "0.5", "trigger_min_after_delay": 0},
                {"fraction": "0.5", "trigger_min_after_delay": 2},
            ],
            "max_alloc_usdt": "100",
            "stop_pct": "5",
            "partials": [{"fraction": "0.5", "gain_pct": "10"}],
            "final_exit_min_after_event": 60,
            "spot_taker_fee_rate_per_side": "0.001",
            "perp_taker_fee_rate_per_side": "0.0005",
            "spread_cost_rate_per_side": "0.002",
            "slippage_conservative_rate_per_side": "0.003",
        },
        "entry_latency_min": validate.decimal_text(item.entry_latency_min),
        "allocation_used_usdt": "100",
        "fee_cost_usdt": "0.5",
        "spread_cost_usdt": "0.25",
        "slippage_cost_usdt": "0.25",
        "total_cost_usdt": "1",
        "gross_pnl_usdt": validate.decimal_text(gross),
        "net_pnl_usdt": validate.decimal_text(net),
        "gross_return_pct": validate.decimal_text(gross),
        "net_return_pct": validate.decimal_text(net),
    }


def event_json_row(item):
    return {
        "kind": "stage_a_event",
        "event_id": item.event_id,
        "symbol": item.symbol,
        "venue_class": item.venue_class,
        "utc": item.event_utc.isoformat().replace("+00:00", "Z"),
        "label": item.label,
        "pre_state": {
            "volume_acceleration": {
                "1m": validate.decimal_text(item.runner_score),
            },
            "mfe_pct": {"60m": validate.decimal_text(item.mfe_pct)},
            "mae_pct": {"60m": validate.decimal_text(item.mae_pct)},
            "time_to_peak_min": {"60m": int(item.time_to_peak_min)},
        },
    }


def assert_no_json_numbers(test, value):
    if isinstance(value, dict):
        for nested in value.values():
            assert_no_json_numbers(test, nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            assert_no_json_numbers(test, nested)
    elif isinstance(value, bool) or value is None:
        return
    else:
        test.assertNotIsInstance(value, (int, float))


class LeakageConstructionTests(unittest.TestCase):
    def test_only_double_intersections_can_reach_fit_or_score(self):
        cohort = cohort_fixture()
        token_split = validate.split_cohort_tokens(cohort, seed="fixture")
        time_split = validate.make_time_split(WINDOW_START)
        rows = []
        expected_fit = set()
        expected_score = set()

        for venue_class in validate.VENUE_CLASSES:
            fit_symbol = token_split.fit_by_venue[venue_class][0]
            score_symbol = token_split.score_by_venue[venue_class][0]
            fit_id = f"{venue_class}-double-fit"
            score_id = f"{venue_class}-double-score"
            rows.extend(
                (
                    record(fit_id, fit_symbol, venue_class, 0, score="0.4"),
                    record(
                        f"{venue_class}-score-token-fit-day",
                        score_symbol,
                        venue_class,
                        0,
                        score="999",
                    ),
                    record(
                        f"{venue_class}-fit-token-score-day",
                        fit_symbol,
                        venue_class,
                        20,
                        score="999",
                    ),
                    record(
                        score_id,
                        score_symbol,
                        venue_class,
                        20,
                        score="888",
                    ),
                    record(
                        f"{venue_class}-fit-label-crosses-boundary",
                        fit_symbol,
                        venue_class,
                        19,
                        score="999",
                        minute_of_day=23 * 60,
                    ),
                    record(
                        f"{venue_class}-score-label-crosses-boundary",
                        score_symbol,
                        venue_class,
                        29,
                        score="999",
                        minute_of_day=23 * 60,
                    ),
                )
            )
            expected_fit.add(fit_id)
            expected_score.add(score_id)

        partitions = validate.partition_records(
            rows, cohort, token_split, time_split
        )
        self.assertFalse(token_split.fit_tokens & token_split.score_tokens)
        self.assertFalse(set(time_split.fit_days) & set(time_split.score_days))
        self.assertEqual(
            {item.event_id for item in partitions.fit_records}, expected_fit
        )
        self.assertEqual(
            {item.event_id for item in partitions.score_records},
            expected_score,
        )
        self.assertEqual(len(partitions.excluded_records), 12)
        self.assertTrue(
            all(
                item.symbol in token_split.fit_tokens
                and item.event_day in time_split.fit_days
                for item in partitions.fit_records
            )
        )
        self.assertTrue(
            all(
                item.symbol in token_split.score_tokens
                and item.event_day in time_split.score_days
                for item in partitions.score_records
            )
        )

        with mock.patch.object(
            validate,
            "select_runner_score_threshold",
            wraps=validate.select_runner_score_threshold,
        ) as selector:
            thresholds = validate.fit_thresholds(partitions)
        self.assertEqual(set(thresholds), set(validate.VENUE_CLASSES))
        self.assertEqual(selector.call_count, 3)
        fitted = tuple(
            item
            for call in selector.call_args_list
            for item in call.args[0]
        )
        self.assertEqual({item.event_id for item in fitted}, expected_fit)
        self.assertTrue(all(item.runner_score == Decimal("0.4") for item in fitted))


class MetricArithmeticTests(unittest.TestCase):
    def test_tiny_fixture_metrics_are_exact(self):
        rows = (
            record(
                "true-positive",
                "S00USDT",
                "spot_only",
                20,
                score="0.9",
                latency="2",
                mfe="10",
                mae="-2",
                peak="5",
                gross="4",
                costs="1",
            ),
            record(
                "false-positive",
                "S01USDT",
                "spot_only",
                21,
                label="FalseIgnition",
                score="0.8",
                latency="4",
                mfe="6",
                mae="-4",
                peak="7",
                gross="-1",
                costs="1",
            ),
            record(
                "false-negative",
                "S02USDT",
                "spot_only",
                22,
                score="0.1",
                gross="100",
                costs="0",
            ),
        )
        metrics = validate.calculate_metrics(rows, Decimal("0.5"))

        self.assertEqual(metrics["events_detected_per_day"], Decimal("0.2"))
        self.assertEqual(metrics["runner_precision_pct"], Decimal("50"))
        self.assertEqual(metrics["runner_recall_pct"], Decimal("50"))
        self.assertEqual(metrics["false_positives"], 1)
        self.assertEqual(metrics["entry_latency_min"]["median"], Decimal("3"))
        self.assertEqual(
            metrics["maximum_favourable_excursion_pct"]["mean"],
            Decimal("8"),
        )
        self.assertEqual(
            metrics["maximum_adverse_excursion_pct"]["median"],
            Decimal("-3"),
        )
        self.assertEqual(metrics["time_to_peak_min"]["mean"], Decimal("6"))
        self.assertEqual(metrics["median_gross_return_pct"], Decimal("1.5"))
        self.assertEqual(metrics["mean_gross_return_pct"], Decimal("1.5"))
        self.assertEqual(metrics["tail_winners"]["event_ids"], ["true-positive"])
        self.assertEqual(metrics["tail_losses"]["event_ids"], ["false-positive"])
        net = metrics["net_return_after_all_costs_pct"]
        self.assertEqual(net["median"], Decimal("0.5"))
        self.assertEqual(net["mean"], Decimal("0.5"))
        self.assertEqual(net["sum"], Decimal("1"))
        self.assertEqual(net["compounded"], Decimal("0.9400"))
        self.assertEqual(metrics["maximum_drawdown_pct"], Decimal("2"))

    def test_fit_threshold_maximizes_f1_without_return_fields(self):
        rows = (
            record("runner-high", "S00USDT", "spot_only", 0, score="0.9"),
            record("runner-mid", "S01USDT", "spot_only", 0, score="0.8"),
            record(
                "false-mid",
                "S02USDT",
                "spot_only",
                0,
                label="FalseIgnition",
                score="0.7",
            ),
            record(
                "false-low",
                "S03USDT",
                "spot_only",
                0,
                label="FalseIgnition",
                score="0.1",
            ),
        )
        self.assertEqual(
            validate.select_runner_score_threshold(rows), Decimal("0.8")
        )


class CliAndBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_cli_writes_complete_decimal_text_result_and_prints_repro(self):
        cohort = cohort_fixture()
        token_split = validate.split_cohort_tokens(cohort)
        rows = []
        for venue_class in validate.VENUE_CLASSES:
            fit_symbol = token_split.fit_by_venue[venue_class][0]
            score_symbol = token_split.score_by_venue[venue_class][0]
            rows.append(
                record(
                    f"{venue_class}-fit",
                    fit_symbol,
                    venue_class,
                    0,
                    score="0.5",
                )
            )
            rows.append(
                record(
                    f"{venue_class}-score",
                    score_symbol,
                    venue_class,
                    20,
                    score="0.6",
                )
            )

        cohort_path = self.root / "cohort_manifest.json"
        events_path = self.root / "events.jsonl"
        trades_path = self.root / "trades.jsonl"
        output_path = self.root / "v2_result.json"
        cohort_path.write_text(
            json.dumps(
                [
                    {
                        "symbol": item.symbol,
                        "venue_class": item.venue_class,
                    }
                    for item in cohort
                ]
            ),
            encoding="utf-8",
        )
        trades_path.write_text(
            "".join(
                json.dumps(policy_json_row(item)) + "\n" for item in rows
            ),
            encoding="utf-8",
        )
        events_path.write_text(
            "".join(
                json.dumps(event_json_row(item)) + "\n" for item in rows
            ),
            encoding="utf-8",
        )
        argv = [
            "--cohort-manifest",
            str(cohort_path),
            "--trades",
            str(trades_path),
            "--events",
            str(events_path),
            "--window-start",
            WINDOW_START.isoformat(),
            "--output",
            str(output_path),
        ]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = validate.main(argv)

        self.assertEqual(exit_code, 0)
        result = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(stdout.getvalue(), result["repro_command"] + "\n")
        self.assertEqual(output_path.name, "v2_result.json")
        self.assertTrue(result["validation"]["zero_leakage"])
        self.assertEqual(result["validation"]["fit_record_count"], "3")
        self.assertEqual(result["validation"]["score_record_count"], "3")
        self.assertEqual(
            result["threshold_selection"]["runner_score_source"],
            "pre_state.volume_acceleration.1m",
        )
        self.assertEqual(result["policy"]["max_alloc_usdt"], "100")
        self.assertEqual(result["policy"]["final_exit_min_after_event"], "60")
        for venue_class in validate.VENUE_CLASSES:
            metrics = result["results_by_venue_class"][venue_class]
            required = {
                "events_detected_per_day",
                "runner_precision_pct",
                "runner_recall_pct",
                "false_positives",
                "entry_latency_min",
                "maximum_favourable_excursion_pct",
                "maximum_adverse_excursion_pct",
                "time_to_peak_min",
                "median_gross_return_pct",
                "mean_gross_return_pct",
                "tail_winners",
                "tail_losses",
                "net_return_after_all_costs_pct",
                "maximum_drawdown_pct",
            }
            self.assertTrue(required <= set(metrics))
            self.assertEqual(metrics["events_detected_per_day"], "0.1")
        assert_no_json_numbers(self, result)

    def test_loader_refuses_json_numbers_and_unproved_costs(self):
        base = {
            "event_id": "bad",
            "symbol": "S00USDT",
            "venue_class": "spot_only",
            "event_utc": "2026-01-01T00:00:00Z",
            "label": "GenuineRunner",
            "runner_score": 0.8,
            "entry_latency_min": "1",
            "mfe_pct": "2",
            "mae_pct": "-1",
            "time_to_peak_min": "3",
            "gross_return_pct": "1",
            "fee_cost_pct": "0.1",
            "spread_cost_pct": "0.1",
            "slippage_cost_pct": "0.1",
        }
        path = self.root / "bad.jsonl"
        path.write_text(json.dumps(base) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(validate.ValidationError, "Decimal text"):
            validate.load_trades(path)

        base["runner_score"] = "0.8"
        del base["slippage_cost_pct"]
        path.write_text(json.dumps(base) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(validate.ValidationError, "prove fee"):
            validate.load_trades(path)

    def test_stage_a_score_path_cannot_select_forward_outcomes(self):
        path = self.root / "events.jsonl"
        sample = record("event", "S00USDT", "spot_only", 0)
        path.write_text(
            json.dumps(event_json_row(sample)) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(validate.ValidationError, "outcome"):
            validate.load_event_evidence(
                path,
                runner_score_path="pre_state.mfe_pct.60m",
            )

    def test_full_cohort_accepts_unicode_exchange_symbols(self):
        member = validate.CohortMember("币安人生USDT", "spot_only")
        self.assertEqual(member.symbol, "币安人生USDT")


if __name__ == "__main__":
    unittest.main()
