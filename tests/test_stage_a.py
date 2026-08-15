#!/usr/bin/env python3
"""Offline synthetic tests for Volume Dataset v2 Stage A."""

import importlib
import json
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path


from study import stage_a

try:
    definitions = importlib.import_module("study.definitions")
except ModuleNotFoundError as error:
    if error.name != "study.definitions":
        raise
    definitions = None


BASE_MS = 1_767_225_600_000
SYMBOL = "AAAUSDT"


def _kline(index, close, volume, trades):
    close = Decimal(str(close))
    open_ms = BASE_MS + index * 60_000
    high = max(Decimal("100"), close)
    low = min(Decimal("100"), close)
    return [
        str(open_ms),
        "100",
        str(high),
        str(low),
        str(close),
        "1",
        str(open_ms + 59_999),
        str(volume),
        str(trades),
        "0.5",
        "1",
        "0",
    ]


def _write_tape(root, rows, *, venue="spot"):
    data_dir = root / "data"
    archive_dir = (
        data_dir
        / venue
        / "daily"
        / "klines"
        / SYMBOL
        / "1m"
    )
    archive_dir.mkdir(parents=True)
    archive = archive_dir / f"{SYMBOL}-1m-2026-01-01.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(
            f"{SYMBOL}-1m-2026-01-01.csv",
            "\n".join(",".join(row) for row in rows) + "\n",
        )
    return data_dir


def _fixture_rows(*, runner):
    rows = [_kline(index, "100", "10", 1) for index in range(1_441)]
    rows.append(_kline(1_441, "101", "610", 61))
    for offset in range(1, 61):
        close = "110" if runner and offset >= 5 else "101"
        rows.append(_kline(1_441 + offset, close, "10", 1))
    return rows


def _dead_ignition_rows():
    rows = [_kline(index, "100", "100", 10) for index in range(3_000)]
    rows.extend(
        _kline(index, "100", "10", 1)
        for index in range(3_000, 4_442)
    )
    rows.append(_kline(4_442, "101", "100000", 1_000))
    for offset in range(1, 61):
        close = "110" if offset >= 5 else "101"
        rows.append(_kline(4_442 + offset, close, "10", 1))
    return rows


@unittest.skipUnless(
    definitions is not None,
    "study.definitions is being built in a sibling lane",
)
class StageAFeatureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_discovery_classifies_both_and_futures_only_without_duplication(self):
        rows = [_kline(0, "100", "10", 1)]
        both_data = _write_tape(self.root / "both", rows, venue="spot")
        _write_tape(self.root / "both", rows, venue="futures")
        both = stage_a.discover_market_tapes(both_data)

        futures_data = _write_tape(
            self.root / "futures_only", rows, venue="futures"
        )
        futures_only = stage_a.discover_market_tapes(futures_data)

        self.assertEqual(len(both), 1)
        self.assertEqual(both[0].venue_class, "both")
        self.assertEqual(both[0].source_venue, "spot")
        self.assertEqual(len(both[0].archives), 1)
        self.assertEqual(futures_only[0].venue_class, "futures_only")
        self.assertEqual(futures_only[0].source_venue, "futures")

    def test_synthetic_klines_produce_exact_pre_move_features(self):
        data_dir = _write_tape(self.root, _fixture_rows(runner=True))
        market = stage_a.discover_market_tapes(data_dir)[0]
        bars = stage_a.read_market_bars(market)
        analyses = stage_a.analyze_bars(bars)
        target = next(
            item for item in analyses if item.bar.open_ms == BASE_MS + 1_441 * 60_000
        )
        state = target.state

        self.assertEqual(
            state["flat_candle_pct"],
            {
                "1h": Decimal("100"),
                "6h": Decimal("100"),
                "24h": Decimal("100"),
            },
        )
        self.assertEqual(state["minutes_since_meaningful_move"], 1_441)
        self.assertTrue(state["minutes_since_meaningful_move_censored"])
        self.assertEqual(state["volume_percentile"], Decimal("100"))
        self.assertEqual(state["activity_percentile"], Decimal("100"))
        self.assertEqual(
            state["pre_ignition_volume_percentile_30d"], Decimal("50")
        )
        self.assertEqual(
            state["pre_ignition_activity_percentile_30d"], Decimal("50")
        )
        self.assertEqual(state["volume_history_count"], 1_441)
        self.assertEqual(
            state["volume_acceleration"],
            {
                "1m": Decimal("61"),
                "5m": Decimal("13"),
                "10m": Decimal("7"),
                "15m": Decimal("5"),
                "60m": Decimal("2"),
            },
        )
        self.assertEqual(state["price_acceleration_5m_pct"], Decimal("1"))
        self.assertEqual(state["price_change_pct"]["1m"], Decimal("1"))
        self.assertFalse(state["dead_regime"])

    def test_crafted_ignition_emits_runner_with_full_state(self):
        data_dir = _write_tape(self.root, _dead_ignition_rows())
        output = self.root / "out" / "events.jsonl"
        summary = stage_a.run_stage_a(data_dir, output)
        rows = [
            json.loads(line)
            for line in output.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        target = next(
            row for row in rows if row["open_ms"] == BASE_MS + 4_442 * 60_000
        )

        self.assertEqual(summary["symbol_count"], 1)
        self.assertEqual(target["label"], "GenuineRunner")
        self.assertIs(target["is_event"], True)
        self.assertEqual(
            target["labels"], ["DeadRegime", "Ignition", "GenuineRunner"]
        )
        self.assertTrue(target["dead_regime"])
        self.assertEqual(target["pre_state"]["volume_percentile"], "100")
        self.assertLessEqual(
            Decimal(
                target["pre_state"]["pre_ignition_volume_percentile_30d"]
            ),
            Decimal("20"),
        )
        self.assertGreaterEqual(
            Decimal(
                target["label_evidence"][
                    "max_favorable_excursion_60m_pct"
                ]
            ),
            Decimal("5"),
        )
        self.assertNotIn(".tmp", {path.suffix for path in output.parent.iterdir()})

    def test_dead_regime_is_false_without_full_pre_move_window(self):
        rows = [_kline(index, "100", "10", 1) for index in range(1_000)]
        data_dir = _write_tape(self.root, rows)
        market = stage_a.discover_market_tapes(data_dir)[0]
        analyses = list(
            stage_a.analyze_bars(stage_a.read_market_bars(market))
        )

        self.assertFalse(analyses[-1].labels["DeadRegime"])
        self.assertIsNone(analyses[-1].state["flat_candle_pct"]["24h"])


if __name__ == "__main__":
    unittest.main()
