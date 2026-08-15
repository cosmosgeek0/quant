#!/usr/bin/env python3
"""Synthetic offline tests for basis-history reading and queries."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from study import basis_history_reader as reader


DAY = "2026-08-15"


def writer_row(
    symbol="BTCUSDT",
    utc=f"{DAY}T00:00:00.000Z",
    gap="0.100",
    funding="0.000100",
    **updates,
):
    row = {
        "utc": utc,
        "symbol": symbol,
        "status": "RECORDED",
        "funding_rate": funding,
        "next_funding_ms": "1786838400000",
        "basis_gap_pct": gap,
        "oi": "12345.6700",
        "mark": "100.2500",
        "unavailable": [],
    }
    row.update(updates)
    row["unavailable"] = ([] if row["utc"] is not None else ["utc"]) + [
        field for field in reader.METRIC_FIELDS if row[field] is None
    ]
    row["status"] = "RECORDED" if not row["unavailable"] else "SKIPPED"
    return row


class BasisHistoryReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "basis_history.jsonl"

    def write_rows(self, rows):
        self.path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_writer_shaped_schema_round_trips_with_decimal_values(self):
        rows = [
            writer_row(),
            writer_row(
                symbol="ETHUSDT",
                utc=f"{DAY}T00:03:00.000Z",
                gap="-0.2500",
                mark=None,
            ),
            writer_row(symbol="SOLUSDT", utc=None, gap=None),
        ]
        self.write_rows(rows)

        history = reader.read_history(self.path)
        restored = [
            item.as_writer_row()
            for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
            for item in history.rows_by_symbol[symbol]
        ]

        self.assertEqual(restored, rows)
        self.assertEqual(history.tally.accepted_rows, 3)
        self.assertEqual(history.tally.recorded_rows, 1)
        self.assertEqual(history.tally.writer_skipped_rows, 2)
        self.assertEqual(history.tally.untimed_rows, 1)
        point = history.series_for("ETHUSDT")[0]
        self.assertIsInstance(point.basis_gap_pct, Decimal)
        self.assertIsInstance(point.funding_rate, Decimal)
        self.assertEqual(point.basis_gap_pct, Decimal("-0.2500"))

    def test_window_math_uses_observed_values_in_the_inclusive_range(self):
        rows = [
            writer_row(utc=f"{DAY}T00:00:00.000Z", gap="9", funding="0.001"),
            writer_row(utc=f"{DAY}T00:30:00.000Z", gap="-2", funding="0.002"),
            writer_row(utc=f"{DAY}T01:00:00.000Z", gap="4", funding="0.003"),
            writer_row(utc=f"{DAY}T01:30:00.000Z", gap="1", funding="0.004"),
        ]
        self.write_rows(rows)

        history = reader.read_history(self.path)
        stats = reader.window_stats(history, "BTCUSDT", "1h")

        self.assertEqual(stats.samples, 3)
        self.assertEqual(stats.gap_min, Decimal("-2"))
        self.assertEqual(stats.gap_max, Decimal("4"))
        self.assertEqual(stats.gap_mean, Decimal("1"))
        self.assertEqual(stats.gap_latest, Decimal("1"))
        self.assertEqual(stats.latest_utc, f"{DAY}T01:30:00.000Z")
        self.assertEqual(
            [item.funding_rate for item in history.series_for("BTCUSDT")],
            [Decimal("0.001"), Decimal("0.002"), Decimal("0.003"), Decimal("0.004")],
        )

    def test_every_torn_or_malformed_physical_line_has_a_typed_tally(self):
        valid = (json.dumps(writer_row()) + "\n").encode("utf-8")
        invalid_schema = json.dumps({"utc": None}).encode("utf-8") + b"\n"
        invalid_time = (
            json.dumps(writer_row(utc="not-a-time")).encode("utf-8") + b"\n"
        )
        self.path.write_bytes(
            valid
            + b"[]\n"
            + invalid_schema
            + invalid_time
            + b"\n"
            + b"\xff\n"
            + b"{torn"
        )

        tally = reader.read_history(self.path).tally

        self.assertEqual(tally.total_lines, 7)
        self.assertEqual(tally.accepted_rows, 1)
        self.assertEqual(tally.non_object, 1)
        self.assertEqual(tally.invalid_schema, 1)
        self.assertEqual(tally.invalid_timestamp, 1)
        self.assertEqual(tally.blank_lines, 1)
        self.assertEqual(tally.invalid_utf8, 1)
        self.assertEqual(tally.invalid_json, 1)
        self.assertEqual(tally.rejected_rows, 6)

    def test_daily_summary_reports_observed_cadence_breaks_without_fill(self):
        rows = [
            writer_row(utc=f"{DAY}T00:00:00.000Z"),
            writer_row(utc=f"{DAY}T00:03:00.000Z"),
            writer_row(utc=f"{DAY}T00:09:00.000Z"),
            writer_row(symbol="ETHUSDT", utc=f"{DAY}T00:00:00.000Z"),
            writer_row(symbol="ETHUSDT", utc=f"{DAY}T00:03:00.000Z"),
        ]
        self.write_rows(rows)

        summary = reader.daily_summary(reader.read_history(self.path))

        self.assertEqual(summary.day.isoformat(), DAY)
        self.assertEqual(summary.symbols_seen, 2)
        self.assertEqual(summary.rows, 5)
        self.assertEqual(len(summary.coverage_gaps), 1)
        gap = summary.coverage_gaps[0]
        self.assertEqual(gap.symbol, "BTCUSDT")
        self.assertEqual(gap.previous_utc, f"{DAY}T00:03:00.000Z")
        self.assertEqual(gap.next_utc, f"{DAY}T00:09:00.000Z")
        self.assertEqual(gap.elapsed_seconds, Decimal("360"))

    def test_top_dislocations_order_by_absolute_gap_then_time(self):
        rows = [
            writer_row(symbol="ONEUSDT", utc=f"{DAY}T00:00:00.000Z", gap="0.4"),
            writer_row(symbol="TWOUSDT", utc=f"{DAY}T00:03:00.000Z", gap="-2.5"),
            writer_row(symbol="THREEUSDT", utc=f"{DAY}T00:06:00.000Z", gap="2.5"),
            writer_row(symbol="FOURUSDT", utc=f"{DAY}T00:09:00.000Z", gap="-3.0"),
        ]
        self.write_rows(rows)

        top = reader.top_dislocations(reader.read_history(self.path), 3, DAY)

        self.assertEqual(
            [(item.symbol, item.gap) for item in top],
            [
                ("FOURUSDT", Decimal("-3.0")),
                ("TWOUSDT", Decimal("-2.5")),
                ("THREEUSDT", Decimal("2.5")),
            ],
        )
        self.assertEqual(top[0].utc, f"{DAY}T00:09:00.000Z")


if __name__ == "__main__":
    unittest.main()
