#!/usr/bin/env python3
"""Deterministic offline tests for the executable-gap stream watcher."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock


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
        "book_age_ms": "100",
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
            "STALEUSDT": stream_record(book_age_ms="5001"),
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
            "spot_age_ms": 100, "perp_age_ms": 100,
            "age_source": "book_age_ms", "utc_ahead_ms": 0,
            "skew_checked": False,
            "result": "OK",
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
        self.write_state({
            "OKAYUSDT": stream_record(freshness_ms="0", book_age_ms="0")})
        first = watch.run_once(self.paths, now_ms=NOW_MS)
        self.assertEqual(len(first.history.series_for("OKAYUSDT")), 1)

        later_ms = NOW_MS + 30 * 60 * 1_000
        replayed = watch.replay_history(self.output, later_ms)
        self.assertEqual(
            [row.entry_gap_pct for row in replayed.series_for("OKAYUSDT")],
            [Decimal("10")],
        )
        self.assertFalse(
            replayed.series_for("OKAYUSDT")[0].skew_checked)
        self.write_state({
            "OKAYUSDT": stream_record(
                at_ms=later_ms, freshness_ms="0", book_age_ms="0",
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
        self.write_state({
            "OKAYUSDT": stream_record(freshness_ms="0", book_age_ms="0")})
        watch.run_once(self.paths, now_ms=NOW_MS)
        later_ms = NOW_MS + watch.REPLAY_WINDOW_MS + 1

        replayed = watch.replay_history(self.output, later_ms)

        self.assertEqual(replayed.series_for("OKAYUSDT"), ())

    def test_shared_stream_time_never_claims_a_venue_skew_check(self):
        self.write_state({
            "HONESTUSDT": stream_record(basis_gap_pct=None),
        })

        completed = watch.run_once(self.paths, now_ms=NOW_MS)

        self.assertEqual(completed.summary.computed, 1)
        row = self.read_rows()[0]
        self.assertEqual(row["result"], "OK")
        self.assertFalse(row["skew_checked"])
        cut = completed.history.series_for("HONESTUSDT")[0]
        self.assertEqual(cut.skew_checked, row["skew_checked"])

    def test_quote_age_prefers_book_then_falls_back_for_null_and_old_rows(self):
        old = stream_record(freshness_ms="300")
        old.pop("book_age_ms")
        self.write_state({
            "BOOKUSDT": stream_record(
                at_ms=NOW_MS - 1_000,
                freshness_ms="5001", book_age_ms="40"),
            "NULLUSDT": stream_record(
                freshness_ms="200", book_age_ms=None),
            "OLDUSDT": old,
        })

        completed = watch.run_once(self.paths, now_ms=NOW_MS)

        self.assertEqual(completed.summary.computed, 3)
        rows = {row["symbol"]: row for row in self.read_rows()}
        self.assertEqual(
            (rows["BOOKUSDT"]["age_source"],
             rows["BOOKUSDT"]["spot_age_ms"]),
            ("book_age_ms", 1_040),
        )
        self.assertEqual(
            (rows["NULLUSDT"]["age_source"],
             rows["NULLUSDT"]["spot_age_ms"]),
            ("freshness_ms", 200),
        )
        self.assertEqual(
            (rows["OLDUSDT"]["age_source"],
             rows["OLDUSDT"]["spot_age_ms"]),
            ("freshness_ms", 300),
        )

    def test_negative_book_age_refuses_without_freshness_fallback(self):
        self.write_state({
            "NEGATIVEUSDT": stream_record(
                freshness_ms="0", book_age_ms=-1),
        })

        completed = watch.run_once(self.paths, now_ms=NOW_MS)

        self.assertEqual(completed.summary.computed, 0)
        row = self.read_rows()[0]
        self.assertEqual(row["result"], "SPOT_QUOTE_INVALID")
        self.assertEqual(row["age_source"], "book_age_ms")
        self.assertIsNone(row["spot_age_ms"])

    def test_record_utc_beyond_tolerance_has_typed_future_refusal(self):
        self.write_state({
            "FUTUREUSDT": stream_record(
                at_ms=NOW_MS + 6_000,
                freshness_ms="7000",
                book_age_ms=None,
            ),
        })

        completed = watch.run_once(self.paths, now_ms=NOW_MS)

        self.assertEqual(completed.summary.computed, 0)
        row = self.read_rows()[0]
        self.assertEqual(row["result"], "RECORD_UTC_IN_FUTURE")
        self.assertEqual(row["age_source"], "freshness_ms")
        self.assertEqual(row["utc_ahead_ms"], 6_000)
        self.assertFalse(row["skew_checked"])

    def test_future_tolerance_clamps_age_and_is_not_symmetric(self):
        self.write_state({
            "EDGEUSDT": stream_record(
                at_ms=NOW_MS + watch.RECORD_UTC_FUTURE_TOLERANCE_MS,
                book_age_ms="0"),
            "OVERUSDT": stream_record(
                at_ms=NOW_MS + watch.RECORD_UTC_FUTURE_TOLERANCE_MS + 1,
                book_age_ms="0"),
            "PASTUSDT": stream_record(
                at_ms=NOW_MS - watch.RECORD_UTC_FUTURE_TOLERANCE_MS - 1,
                book_age_ms="0"),
        })

        completed = watch.run_once(self.paths, now_ms=NOW_MS)

        self.assertEqual(completed.summary.computed, 2)
        rows = {row["symbol"]: row for row in self.read_rows()}
        self.assertEqual(rows["EDGEUSDT"]["result"], "OK")
        self.assertEqual(rows["EDGEUSDT"]["spot_age_ms"], 0)
        self.assertEqual(
            rows["EDGEUSDT"]["utc_ahead_ms"],
            watch.RECORD_UTC_FUTURE_TOLERANCE_MS,
        )
        self.assertEqual(rows["OVERUSDT"]["result"], "RECORD_UTC_IN_FUTURE")
        self.assertEqual(
            rows["OVERUSDT"]["utc_ahead_ms"],
            watch.RECORD_UTC_FUTURE_TOLERANCE_MS + 1,
        )
        self.assertEqual(rows["PASTUSDT"]["result"], "OK")
        self.assertEqual(
            rows["PASTUSDT"]["spot_age_ms"],
            watch.RECORD_UTC_FUTURE_TOLERANCE_MS + 1,
        )
        self.assertEqual(rows["PASTUSDT"]["utc_ahead_ms"], 0)

    def test_same_record_cut_is_appended_and_recorded_once(self):
        self.write_state({"ONCEUSDT": stream_record()})

        first = watch.run_once(self.paths, now_ms=NOW_MS)
        second = watch.run_once(self.paths, now_ms=NOW_MS + 6_000)

        self.assertEqual(len(first.history.series_for("ONCEUSDT")), 1)
        self.assertEqual(len(second.history.series_for("ONCEUSDT")), 1)
        self.assertEqual(len(self.read_rows()), 1)
        self.assertEqual(second.rows, ())

    def test_newer_cut_with_equal_quotes_is_appended(self):
        self.write_state({"FRESHUSDT": stream_record(book_age_ms="0")})
        watch.run_once(self.paths, now_ms=NOW_MS)
        later_ms = NOW_MS + 1
        self.write_state({
            "FRESHUSDT": stream_record(at_ms=later_ms, book_age_ms="0"),
        })

        completed = watch.run_once(self.paths, now_ms=later_ms)

        self.assertEqual(len(self.read_rows()), 2)
        self.assertEqual(len(completed.history.series_for("FRESHUSDT")), 2)

    def test_same_utc_future_refusal_is_appended_once_and_not_ring_data(self):
        self.write_state({
            "REFUSEUSDT": stream_record(
                at_ms=NOW_MS + watch.RECORD_UTC_FUTURE_TOLERANCE_MS + 1,
                book_age_ms="0"),
        })

        first = watch.run_once(self.paths, now_ms=NOW_MS)
        second = watch.run_once(self.paths, now_ms=NOW_MS)

        self.assertEqual(len(first.rows), 1)
        self.assertEqual(second.rows, ())
        self.assertEqual(len(self.read_rows()), 1)
        self.assertEqual(second.history.series_for("REFUSEUSDT"), ())

    def test_append_uses_one_newline_terminated_write_per_row(self):
        row = {"utc": utc_text(), "symbol": "WRITEUSDT", "result": "OK"}
        real_open = os.open
        real_write = os.write
        with mock.patch.object(
                watch.os, "open", side_effect=real_open) as opened, \
                mock.patch.object(
                    watch.os, "write", side_effect=real_write) as written:
            watch._append_rows(self.output, (row,))

        opened.assert_called_once()
        written.assert_called_once()
        flags = opened.call_args.args[1]
        self.assertTrue(flags & os.O_APPEND)
        self.assertTrue(flags & os.O_CREAT)
        self.assertEqual(opened.call_args.args[2], 0o600)
        payload = written.call_args.args[1]
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(payload.count(b"\n"), 1)

    def test_append_encodes_all_rows_before_opening(self):
        with mock.patch.object(
                watch.json, "dumps", side_effect=TypeError) as encoded, \
                mock.patch.object(watch.os, "open") as opened:
            with self.assertRaisesRegex(
                    watch.ExecutableGapWatchError, "append failed"):
                watch._append_rows(self.output, ({"result": "OK"},))

        encoded.assert_called_once()
        opened.assert_not_called()

    def test_append_refuses_a_short_write(self):
        with mock.patch.object(watch.os, "write", return_value=0) as written, \
                mock.patch.object(watch.os, "fsync") as synced:
            with self.assertRaisesRegex(
                    watch.ExecutableGapWatchError, "append failed"):
                watch._append_rows(self.output, ({"result": "OK"},))

        written.assert_called_once()
        synced.assert_not_called()

    def test_append_refuses_an_absent_receipt_directory(self):
        output = self.root / "runtime-owned" / watch.OUTPUT_FILENAME

        with mock.patch.object(watch.os, "open") as opened:
            with self.assertRaisesRegex(
                    watch.ExecutableGapWatchError, "directory is unavailable"):
                watch._append_rows(output, ({"result": "OK"},))

        opened.assert_not_called()
        self.assertFalse(output.parent.exists())

    def test_replay_skips_a_row_split_at_the_tail_boundary(self):
        valid = {
            "utc": utc_text(), "symbol": "TAILUSDT", "result": "OK",
            "entry_gap_pct": "10", "exit_gap_pct": "12",
            "spot_age_ms": 0, "perp_age_ms": 0,
        }
        encoded = (json.dumps(valid) + "\n").encode("utf-8")
        partial = b'{"discarded":"' + b"x" * 40 + b'"}\n'
        self.output.write_bytes(partial + encoded)

        with mock.patch.object(
                watch, "REPLAY_MAX_BYTES", len(encoded) + 8):
            replayed = watch.replay_history(self.output, NOW_MS)

        self.assertEqual(
            [row.entry_gap_pct for row in replayed.series_for("TAILUSDT")],
            [Decimal("10")],
        )

    def test_replay_ignores_refusals_and_rejects_malformed_rows(self):
        refusal = {
            "utc": utc_text(), "symbol": "REFUSEDUSDT",
            "result": "SPOT_QUOTE_STALE",
        }
        self.output.write_text(json.dumps(refusal) + "\n", encoding="utf-8")

        replayed = watch.replay_history(self.output, NOW_MS)
        self.assertEqual(replayed.series_for("REFUSEDUSDT"), ())

        with open(self.output, "ab") as handle:
            handle.write(b"{malformed\n")
        with self.assertRaisesRegex(
                watch.ExecutableGapWatchError, "invalid row"):
            watch.replay_history(self.output, NOW_MS)


if __name__ == "__main__":
    unittest.main()
