#!/usr/bin/env python3
"""Offline checks for the census fine-slice study."""

import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from study import fine_slice_study as fine


BASE_MS = 1_767_268_800_000
DAY = datetime.fromtimestamp(BASE_MS / 1000, timezone.utc).date()


def census(path, label="HELD"):
    payload = {"episodes": [{"episode_id": "P00001", "symbol": "TESTUSDT", "venue": "spot",
                              "base_utc": "2026-01-01T12:00:00.000Z", "class": label}],
               "header": {"schema": "pump-census-6mo-v1"}, "summary": {"episodes": 1}}
    path.write_text(json.dumps(payload), encoding="utf-8")


def archive(root):
    path = fine.tape_pull.agg_trades_path(root, "TESTUSDT", DAY, "spot")
    path.parent.mkdir(parents=True, exist_ok=True)
    start_ms = BASE_MS - 12 * 60 * 60_000
    rows = [(1, start_ms + 60_000, "10", "false"), (2, start_ms + 120_000, "20", "true"),
            (3, start_ms + 31 * 60_000, "1000", "false")]
    text = "".join(f"{trade},{price},1,{trade},{trade},{stamp},{maker},true\n" for trade, stamp, price, maker in rows)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as handle:
        handle.writestr(path.stem + ".csv", text)
    return path


class FineSliceStudyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.census, self.tape, self.output = self.root / "census.json", self.root / "tape", self.root / "out.jsonl"

    def rows(self):
        return [json.loads(line) for line in self.output.read_text(encoding="utf-8").splitlines()]

    def test_streamed_archive_emits_labelled_decimal_feature_slices(self):
        census(self.census); archive(self.tape)
        shared = fine.tape_pull.stream_verified_member
        with mock.patch.object(fine.tape_pull, "stream_verified_member", wraps=shared) as reader:
            summary = fine.run_fine_slice(self.census, self.tape, self.output)
        rows = self.rows()
        self.assertEqual(summary, {"episodes": 1, "fully_banked": 1, "tape_absent": 0, "tape_invalid": 0,
                                   "slices": 24, "rows": 24, "measured_slices": 2, "no_trade_slices": 22})
        self.assertEqual(reader.call_count, 2)
        self.assertEqual({row["class"] for row in rows}, {"HELD"})
        self.assertEqual(rows[0]["features"]["trade_size_p50_quote"], "15")
        self.assertEqual(rows[1]["features"]["large_trade_net_aggression"], "1")
        self.assertEqual(rows[1]["large_print_p95_source"], "OWN_MONTH_P95")
        self.assertFalse(rows[1]["large_print_p95_fallback"])

    def test_absent_episode_keeps_all_slices_and_collapsed_label(self):
        census(self.census, "COLLAPSED")
        summary = fine.run_fine_slice(self.census, self.tape, self.output)
        rows = self.rows()
        self.assertEqual((summary["fully_banked"], summary["tape_absent"], summary["rows"]), (0, 1, 24))
        self.assertEqual({row["status"] for row in rows}, {"TAPE_ABSENT"})
        self.assertEqual({row["class"] for row in rows}, {"COLLAPSED"})
        self.assertTrue(all(value is None for value in rows[0]["features"].values()))

    def test_window_threshold_fallback_is_explicit(self):
        self.assertEqual(fine._threshold(None, Decimal("12.5")), (Decimal("12.5"), "OWN_WINDOW_SET_P95", True))
        self.assertEqual(fine._threshold(None, None), (None, None, True))


if __name__ == "__main__":
    unittest.main()
