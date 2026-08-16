#!/usr/bin/env python3
"""Offline checks for the reverse lock-in shadow adapter."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import executable_gap  # noqa: E402
import reverse_lockin_shadow as shadow  # noqa: E402
import reverse_rung_intent as reverse  # noqa: E402
from runtime_paths import resolve_runtime_paths  # noqa: E402


NOW_MS = 1_800_000_000_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc_text(at_ms=NOW_MS):
    return (EPOCH + timedelta(milliseconds=at_ms)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def ok_row(symbol, spot_bid="102", spot_ask="103",
           perp_bid="99", perp_ask="100", at_ms=NOW_MS):
    reading = executable_gap.read_executable_gap_shared_observation(
        symbol,
        {"spot_bid": spot_bid, "spot_ask": spot_ask,
         "perp_bid": perp_bid, "perp_ask": perp_ask},
        at_ms - 100,
        at_ms,
    )
    if not isinstance(reading, executable_gap.ExecutableGapReading):
        raise AssertionError("fixture must make an executable gap")
    return {
        "utc": utc_text(at_ms), "symbol": symbol,
        "entry_gap_pct": format(reading.entry_gap_pct, "f"),
        "exit_gap_pct": format(reading.exit_gap_pct, "f"),
        "spot_age_ms": reading.spot_age_ms,
        "perp_age_ms": reading.perp_age_ms,
        "result": "OK", "skew_checked": False,
        "age_source": "book_age_ms",
    }


def refused_row(symbol, result):
    return {
        "utc": utc_text(), "symbol": symbol,
        "entry_gap_pct": None, "exit_gap_pct": None,
        "spot_age_ms": None, "perp_age_ms": None,
        "result": result, "skew_checked": False,
        "age_source": "book_age_ms",
    }


def stream_record(spot_bid="102", perp_ask="100"):
    return {
        "spot_bid": spot_bid, "perp_ask": perp_ask,
        "funding_rate": "0.0001",
        "next_funding_ms": str(NOW_MS + 3_600_000),
        "funding_period_ms": "28800000",
    }


class ReverseLockinShadowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = resolve_runtime_paths({"RECEIPT_DIR": str(self.root)})
        self.ledger = self.root / shadow.GAP_FILENAME
        self.output = self.root / shadow.OUTPUT_FILENAME

    def write_rows(self, rows):
        self.ledger.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    def write_stream(self, records):
        self.paths.stream_state.write_text(
            json.dumps(records), encoding="utf-8")
        metadata = {
            "version": 1, "refreshed_ms": NOW_MS,
            "spot": {}, "perp": {}, "margin": {},
        }
        for symbol in records:
            metadata["spot"][symbol] = {
                "step": "0.01", "min_notional": "2"}
            metadata["perp"][symbol] = {
                "step": "0.001", "min_notional": "2"}
            metadata["margin"][symbol] = {
                "hourly_interest_rate": "0.000001"}
        self.paths.stream_state.with_name(
            shadow.METADATA_FILENAME).write_text(
                json.dumps(metadata), encoding="utf-8")

    def read_rows(self):
        return [json.loads(line) for line in self.output.read_text(
            encoding="utf-8").splitlines()]

    def test_mixed_ledger_has_exact_default_counts_and_first_gate(self):
        self.write_rows([
            ok_row("NORMALUSDT", "100", "101", "111.1", "112"),
            ok_row("REVERSEUSDT"),
            refused_row("STALEUSDT", "SPOT_QUOTE_STALE"),
            refused_row("CROSSEDUSDT", "PERP_BOOK_CROSSED_OR_LOCKED"),
        ])
        self.write_stream({
            "NORMALUSDT": stream_record("100", "112"),
            "REVERSEUSDT": stream_record(),
        })

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = shadow.main(["--once"], paths=self.paths)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.getvalue(),
            "REVERSE-LOCKIN-SHADOW | symbols_evaluated=2 "
            "reverse_direction=1 would_intent=0 "
            "top_5_expected_capture_pct=- "
            "gate_histogram=BORROW_POWER_REQUIRED:1\n",
        )
        rows = self.read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "REVERSEUSDT")
        self.assertFalse(rows[0]["would_intent"])
        self.assertEqual(
            rows[0]["first_failing_gate"], reverse.BORROW_POWER_REQUIRED)
        self.assertIsNone(rows[0]["shadow_assumption"])
        self.assertFalse(self.paths.jobs_dir.exists())

    def test_assumption_stamps_every_row_and_preserves_direct_intent_id(self):
        self.write_rows([
            ok_row("LOWUSDT", "101", "102", "99", "100"),
            ok_row("TESTUSDT"),
        ])
        self.write_stream({
            "LOWUSDT": stream_record("101", "100"),
            "TESTUSDT": stream_record(),
        })

        completed = shadow.run_once(
            self.paths, assume_borrow_power=True)

        self.assertEqual(completed.summary.symbols_evaluated, 2)
        self.assertEqual(completed.summary.reverse_direction, 2)
        self.assertEqual(completed.summary.would_intent, 1)
        self.assertEqual(
            completed.summary.gate_histogram,
            ((reverse.LADDER_HEADROOM_UNAVAILABLE, 1),),
        )
        rows = self.read_rows()
        self.assertTrue(all(
            row["shadow_assumption"] == "borrow_power_assumed"
            for row in rows))
        intent_row = next(row for row in rows if row["would_intent"])

        candidates = shadow.latest_ok_candidates(self.ledger)
        candidate = next(
            row for row in candidates if row.reading.symbol == "TESTUSDT")
        state, metadata = shadow.load_stream_inputs(self.paths)
        record = shadow._stream_record("TESTUSDT", state, metadata)
        direct = reverse.build_reverse_rung_intent(
            *shadow._intent_inputs(
                candidate, record, True, shadow.DEFAULT_LADDER_CONFIG))
        self.assertIsInstance(direct, reverse.ReverseRungIntent)
        self.assertEqual(intent_row["intent_id"], direct.intent_id)

    def test_intent_decimal_values_are_json_text_and_identity_is_stable(self):
        self.write_rows([ok_row("TESTUSDT")])
        self.write_stream({"TESTUSDT": stream_record()})

        first = shadow.run_once(self.paths, assume_borrow_power=True)
        second = shadow.run_once(self.paths, assume_borrow_power=True)

        first_row, second_row = first.rows[0], second.rows[0]
        self.assertEqual(first_row["intent_id"], second_row["intent_id"])
        for field in (
                "entry_gap_pct", "borrow_qty", "spot_short_qty",
                "perp_long_qty", "quote_notional", "expected_capture_pct",
                "drag_pct"):
            self.assertIs(type(first_row[field]), str)
        raw = self.output.read_text(encoding="utf-8")
        self.assertIn('"quote_notional":"3"', raw)
        self.assertNotIn('"quote_notional":3', raw)

    def test_latest_ok_row_wins_and_tail_remains_bounded(self):
        old = ok_row("TESTUSDT", "104", "105", "99", "100",
                     at_ms=NOW_MS - 1_000)
        latest = ok_row("TESTUSDT")
        prefix = refused_row("OLDUSDT", "SPOT_QUOTE_STALE")
        self.write_rows([prefix, old, latest])
        encoded_latest = (json.dumps(latest) + "\n").encode("utf-8")

        with mock.patch.object(
                shadow, "TAIL_MAX_BYTES", len(encoded_latest) + 8):
            candidates = shadow.latest_ok_candidates(self.ledger)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0].reading.exit_gap_pct,
            executable_gap.read_executable_gap_shared_observation(
                "TESTUSDT",
                {"spot_bid": "102", "spot_ask": "103",
                 "perp_bid": "99", "perp_ask": "100"},
                NOW_MS - 100, NOW_MS).exit_gap_pct,
        )

    def test_append_uses_one_write_per_row_and_fsync(self):
        rows = ({"symbol": "ONEUSDT"}, {"symbol": "TWOUSDT"})
        real_write = os.write
        with mock.patch.object(
                shadow.os, "write", side_effect=real_write) as written, \
                mock.patch.object(
                    shadow.os, "fsync", wraps=os.fsync) as synced:
            shadow._append_rows(self.output, rows)

        self.assertEqual(written.call_count, 2)
        synced.assert_called_once()
        self.assertTrue(all(
            call.args[1].endswith(b"\n") and call.args[1].count(b"\n") == 1
            for call in written.call_args_list))

    def test_absent_receipt_directory_refuses_without_creating_it(self):
        absent = self.root / "runtime-owned"
        paths = resolve_runtime_paths({"RECEIPT_DIR": str(absent)})

        with self.assertRaisesRegex(
                shadow.ReverseShadowError, "directory is unavailable"):
            shadow.run_once(paths)

        self.assertFalse(absent.exists())

    def test_source_is_bounded_and_has_no_execution_imports(self):
        source = (SRC / "reverse_lockin_shadow.py").read_text(encoding="utf-8")
        self.assertLessEqual(sum(bool(line.strip())
                                 for line in source.splitlines()), 250)
        self.assertNotIn("import job_runner", source)
        self.assertNotIn("import basis_position", source)


if __name__ == "__main__":
    unittest.main()
