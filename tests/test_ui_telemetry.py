#!/usr/bin/env python3
"""Offline tests for the local UI telemetry recorder."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ui import telemetry_recorder as telemetry


FIXED_NOW = datetime(2026, 8, 15, 6, 0, 0, tzinfo=timezone.utc)


class TelemetryRecorderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_build_row_maps_nested_values_and_news_age(self):
        row = telemetry.build_row(
            {
                "machine": "quant-local",
                "pull": {"age_seconds": "1.25"},
                "market_pass": {"age_seconds": 2},
                "supervisor": {
                    "age_seconds": "3.5",
                    "reported_status": "healthy",
                },
            },
            {
                "services": [
                    {"service": "other", "last_seen_age_seconds": 99},
                    {"service": "basis_watch",
                     "last_seen_age_seconds": "4.25"},
                    {"service": "basis_supervisor",
                     "last_seen_age_seconds": 5},
                ],
                "jobs_log": {"last_tick_age_seconds": "6"},
            },
            12.6,
            news_as_of="2026-08-15T05:59:50Z",
            now=FIXED_NOW,
        )

        self.assertEqual(row, {
            "ts": "2026-08-15T06:00:00Z",
            "rtt_ms": 13,
            "machine": "quant-local",
            "mirror_s": 1.25,
            "market_s": 2.0,
            "supervisor_s": 3.5,
            "supervisor_status": "healthy",
            "news_s": 10.0,
            "basis_watch_s": 4.25,
            "basis_sup_s": 5.0,
            "jobs_s": 6.0,
        })

    def test_build_row_rejects_junk_and_non_finite_numbers(self):
        row = telemetry.build_row(
            {
                "machine": ["not", "text"],
                "pull": {"age_seconds": "nan"},
                "market_pass": {"age_seconds": True},
                "supervisor": {
                    "age_seconds": "1e999",
                    "reported_status": {"bad": "shape"},
                },
            },
            {
                "services": [
                    {"service": "basis_watch",
                     "last_seen_age_seconds": object()},
                    {"service": "basis_supervisor",
                     "last_seen_age_seconds": "infinity"},
                ],
                "jobs_log": {"last_tick_age_seconds": False},
            },
            "not-a-number",
            news_as_of="not-a-time",
            now=FIXED_NOW,
        )

        self.assertEqual(row["ts"], "2026-08-15T06:00:00Z")
        self.assertTrue(all(
            row[field] is None for field in row if field != "ts"
        ))

    def test_collect_sample_uses_injected_fetcher_and_times_strip_only(self):
        calls = []
        clock_values = iter((100.0, 100.0146))

        def fake_fetch(url, timeout):
            calls.append((url, timeout))
            if url == telemetry.STRIP_URL:
                return {"machine": "fixture"}
            return {"jobs_log": {"last_tick_age_seconds": "7"}}

        row, double_failure = telemetry.collect_sample(
            fetcher=fake_fetch,
            news_path=self.root / "absent.json",
            now_fn=lambda: FIXED_NOW,
            clock=lambda: next(clock_values),
        )

        self.assertEqual(calls, [
            (telemetry.STRIP_URL, 4),
            (telemetry.HEALTH_URL, 4),
        ])
        self.assertEqual(row["rtt_ms"], 15)
        self.assertEqual(row["machine"], "fixture")
        self.assertEqual(row["jobs_s"], 7.0)
        self.assertFalse(double_failure)

    def test_collect_sample_turns_two_fetch_errors_into_an_honest_gap(self):
        clock_values = iter((50.0, 50.004))

        def failed_fetch(_url, _timeout):
            raise OSError("offline fixture")

        row, double_failure = telemetry.collect_sample(
            fetcher=failed_fetch,
            news_path=self.root / "absent.json",
            now_fn=lambda: FIXED_NOW,
            clock=lambda: next(clock_values),
        )

        self.assertTrue(double_failure)
        self.assertTrue(telemetry.is_double_failure_row(row))
        self.assertIsNone(row["rtt_ms"])
        self.assertNotIn(0, row.values())

    def test_news_file_is_optional_and_must_contain_parseable_as_of(self):
        news_path = self.root / "news_sources.json"
        news_path.write_text(
            json.dumps({"as_of": "2026-08-15T05:59:30Z"}),
            encoding="utf-8",
        )
        self.assertEqual(telemetry.read_news_as_of(news_path),
                         "2026-08-15T05:59:30Z")

        news_path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(telemetry.read_news_as_of(news_path))

    def test_trim_keeps_newest_rows_via_same_directory_replace(self):
        history = self.root / "telemetry.jsonl"
        rows = [{"sequence": number} for number in range(7)]
        history.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        real_replace = os.replace
        replacements = []

        def observed_replace(source, target):
            replacements.append((Path(source), Path(target)))
            return real_replace(source, target)

        with mock.patch.object(
            telemetry.os, "replace", side_effect=observed_replace
        ):
            trimmed = telemetry.trim_file(
                history, max_lines=5, keep_lines=3)

        self.assertTrue(trimmed)
        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0][0].parent, history.parent)
        self.assertEqual(replacements[0][1], history)
        parsed = [
            json.loads(line)
            for line in history.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(parsed, rows[-3:])
        self.assertEqual(list(self.root.glob(".telemetry.jsonl-*.tmp")), [])

    def test_append_writes_one_parseable_flushed_line(self):
        history = self.root / "telemetry.jsonl"
        row = telemetry.build_row({}, {}, None, now=FIXED_NOW)

        telemetry.append_row(row, history)

        lines = history.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), row)

    def test_only_consecutive_double_failures_are_suppressed(self):
        gap = telemetry.build_row({}, {}, None, now=FIXED_NOW)
        gap["news_s"] = 8.0

        self.assertTrue(telemetry.is_double_failure_row(gap))
        self.assertTrue(telemetry.should_write_sample(True, False))
        self.assertTrue(telemetry.should_write_sample(False, True))
        self.assertFalse(telemetry.should_write_sample(True, True))

        history = self.root / "telemetry.jsonl"
        history.write_text(json.dumps(gap) + "\n", encoding="utf-8")
        self.assertTrue(telemetry.last_row_was_double_failure(history))


if __name__ == "__main__":
    unittest.main()
