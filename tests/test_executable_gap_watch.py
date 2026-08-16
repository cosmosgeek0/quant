#!/usr/bin/env python3
"""Deterministic offline tests for the executable-gap stream watcher."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import executable_gap_watch as watch  # noqa: E402
from runtime_paths import resolve_runtime_paths  # noqa: E402


NOW_MS = 1_800_000_000_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc_text(at_ms=NOW_MS):
    return (EPOCH + timedelta(milliseconds=at_ms)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def stream_record(venues="both", *, at_ms=NOW_MS, **updates):
    row = {
        "utc": utc_text(at_ms),
        "venues": venues,
        "spot_bid": "100" if venues in {"spot", "both"} else None,
        "spot_ask": "101" if venues in {"spot", "both"} else None,
        "perp_bid": "111.1" if venues in {"perp", "both"} else None,
        "perp_ask": "112" if venues in {"perp", "both"} else None,
        "freshness_ms": "100",
    }
    row.update(updates)
    return row


class ExecutableGapWatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = resolve_runtime_paths({"RECEIPT_DIR": str(self.root)})
        self.output = self.root / watch.OUTPUT_FILENAME

    def write_state(self, state):
        self.paths.stream_state.write_text(
            json.dumps(state), encoding="utf-8")

    def read_rows(self):
        return [
            json.loads(line)
            for line in self.output.read_text(encoding="utf-8").splitlines()
        ]

    def test_once_records_exact_shared_results_and_summary(self):
        self.write_state({
            "OKAYUSDT": stream_record(),
            "SPOTUSDT": stream_record("spot"),
            "PERPUSDT": stream_record("perp"),
            "STALEUSDT": stream_record(freshness_ms="5001"),
            "CROSSEDUSDT": stream_record(spot_bid="101", spot_ask="101"),
        })

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = watch.main(
                ["--once"], paths=self.paths, now_ms=NOW_MS)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.getvalue(),
            "EXECUTABLE-GAP | symbols_seen=3 computed=1 refused_stale=1 "
            "refused_crossed=1 top_5_abs_entry=OKAYUSDT:10.0\n",
        )
        rows = self.read_rows()
        self.assertEqual(
            [row["symbol"] for row in rows],
            ["CROSSEDUSDT", "OKAYUSDT", "STALEUSDT"],
        )
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual(by_symbol["OKAYUSDT"], {
            "utc": utc_text(), "symbol": "OKAYUSDT",
            "entry_gap_pct": "10.0", "exit_gap_pct": "12.00",
            "spot_age_ms": 100, "perp_age_ms": 100, "result": "OK",
        })
        self.assertEqual(
            by_symbol["STALEUSDT"]["result"], "SPOT_QUOTE_STALE")
        self.assertEqual(
            by_symbol["CROSSEDUSDT"]["result"],
            "SPOT_BOOK_CROSSED_OR_LOCKED",
        )
        for symbol in ("STALEUSDT", "CROSSEDUSDT"):
            self.assertIsNone(by_symbol[symbol]["entry_gap_pct"])
            self.assertIsNone(by_symbol[symbol]["exit_gap_pct"])

    def test_restart_replay_restores_ring_before_current_reading(self):
        self.write_state({"OKAYUSDT": stream_record(freshness_ms="0")})
        first = watch.run_once(self.paths, now_ms=NOW_MS)
        self.assertEqual(len(first.history.series_for("OKAYUSDT")), 1)

        later_ms = NOW_MS + 30 * 60 * 1_000
        replayed = watch.replay_history(self.output, later_ms)
        self.assertEqual(
            [row.entry_gap_pct for row in replayed.series_for("OKAYUSDT")],
            [Decimal("10")],
        )
        self.write_state({
            "OKAYUSDT": stream_record(
                at_ms=later_ms, freshness_ms="0",
                perp_bid="121.2", perp_ask="122")
        })
        second = watch.run_once(self.paths, now_ms=later_ms)

        self.assertEqual(
            [row.entry_gap_pct for row in second.history.series_for("OKAYUSDT")],
            [Decimal("10"), Decimal("20")],
        )
        self.assertEqual(
            second.history.window_stats("OKAYUSDT", "1h").samples, 2)

    def test_gap_values_are_decimal_text_and_numeric_prices_refuse(self):
        self.write_state({
            "TEXTUSDT": stream_record(),
            "NUMBERUSDT": stream_record(spot_bid=100.0),
        })

        completed = watch.run_once(self.paths, now_ms=NOW_MS)

        self.assertEqual(completed.summary.computed, 1)
        rows = {row["symbol"]: row for row in self.read_rows()}
        self.assertIsInstance(rows["TEXTUSDT"]["entry_gap_pct"], str)
        self.assertIsInstance(rows["TEXTUSDT"]["exit_gap_pct"], str)
        self.assertEqual(rows["NUMBERUSDT"]["result"], "SPOT_QUOTE_INVALID")
        raw = self.output.read_text(encoding="utf-8")
        self.assertIn('"entry_gap_pct":"10.0"', raw)
        self.assertNotIn('"entry_gap_pct":10', raw)

    def test_replay_discards_rows_older_than_24_hours(self):
        self.write_state({"OKAYUSDT": stream_record(freshness_ms="0")})
        watch.run_once(self.paths, now_ms=NOW_MS)
        later_ms = NOW_MS + watch.REPLAY_WINDOW_MS + 1

        replayed = watch.replay_history(self.output, later_ms)

        self.assertEqual(replayed.series_for("OKAYUSDT"), ())


if __name__ == "__main__":
    unittest.main()
