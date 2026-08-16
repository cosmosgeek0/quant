from __future__ import annotations

"""Deterministic tests for the Network SQLite history store."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ui import network_store
from ui.network_store import NetworkStore


NOW_MS = 1_800_000_000
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


class NetworkStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "network_history.sqlite3"
        self.store = NetworkStore(str(self.path))
        self.store.open()
        self.addCleanup(self.store.close)

    def _rows(self, table: str) -> list:
        connection = sqlite3.connect(str(self.path))
        self.addCleanup(connection.close)
        return connection.execute("SELECT * FROM %s" % table).fetchall()

    def test_open_creates_schema_indexes_pragmas_and_meta(self) -> None:
        connection = sqlite3.connect(str(self.path))
        self.addCleanup(connection.close)
        objects = dict(connection.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE name IN ('samples', 'events', 'meta', "
            "'samples_edge_ts', 'events_ts')"
        ).fetchall())

        self.assertEqual(objects, {
            "samples": "table",
            "samples_edge_ts": "index",
            "events": "table",
            "events_ts": "index",
            "meta": "table",
        })
        self.assertEqual(connection.execute(
            "PRAGMA journal_mode"
        ).fetchone()[0].lower(), "wal")
        self.assertEqual(connection.execute(
            "PRAGMA synchronous"
        ).fetchone()[0], 1)
        self.assertEqual(connection.execute(
            "PRAGMA auto_vacuum"
        ).fetchone()[0], 2)
        self.assertEqual(dict(connection.execute(
            "SELECT key, value FROM meta"
        ).fetchall()), {"schema_version": "1", "last_prune_ms": "0"})

    def test_sample_commit_can_be_deferred_to_the_cycle(self) -> None:
        self.store.insert_sample(
            NOW_MS, "spot_rest", "Operational", {"rtt_ms": 12},
            commit=False,
        )
        observer = sqlite3.connect(str(self.path))
        self.addCleanup(observer.close)
        self.assertEqual(observer.execute(
            "SELECT COUNT(*) FROM samples"
        ).fetchone()[0], 0)

        self.store.commit()

        self.assertEqual(observer.execute(
            "SELECT COUNT(*) FROM samples"
        ).fetchone()[0], 1)
        raw_metrics = observer.execute(
            "SELECT metrics FROM samples"
        ).fetchone()[0]
        self.assertEqual(json.loads(raw_metrics), {"rtt_ms": 12})

    def test_history_uses_each_frozen_range_and_bucket_width(self) -> None:
        for range_key, (range_ms, bucket_ms) in network_store.RANGES.items():
            edge_id = "edge_%s" % range_key
            bucket_count = range_ms // bucket_ms
            start_ms = NOW_MS - (bucket_count - 1) * bucket_ms
            self.store.insert_sample(
                start_ms + 10, edge_id, "Operational", {"value": 10},
                commit=False,
            )
            self.store.insert_sample(
                start_ms + bucket_ms - 1,
                edge_id,
                "Degraded",
                {"value": 20},
                commit=False,
            )
            self.store.insert_sample(
                start_ms + bucket_ms,
                edge_id,
                "Operational",
                {"value": 30},
                commit=False,
            )
            self.store.insert_sample(
                start_ms - 1, edge_id, "Outage", {"value": 100},
                commit=False,
            )
            self.store.insert_sample(
                NOW_MS + 1, edge_id, "Outage", {"value": 100},
                commit=False,
            )
        self.store.commit()

        for range_key, (range_ms, bucket_ms) in network_store.RANGES.items():
            with self.subTest(range_key=range_key):
                result = self.store.history(
                    "edge_%s" % range_key, "value", range_key, NOW_MS
                )
                bucket_count = range_ms // bucket_ms
                start_ms = NOW_MS - (bucket_count - 1) * bucket_ms
                self.assertEqual(result["bucket_s"], bucket_ms // 1000)
                self.assertEqual(result["from_ms"], start_ms)
                self.assertEqual(result["to_ms"], NOW_MS)
                self.assertEqual(result["buckets"], [
                    {
                        "t": start_ms,
                        "n": 2,
                        "status": "Degraded",
                        "avg": 15.0,
                        "max": 20,
                    },
                    {
                        "t": start_ms + bucket_ms,
                        "n": 1,
                        "status": "Operational",
                        "avg": 30.0,
                        "max": 30,
                    },
                ])

    def test_r1_store_bucket_alignment_uses_fixed_grid_starts(self) -> None:
        now_ms = 1_800_000_123
        bucket_ms = network_store.RANGES["1h"][1]
        last_bucket_start = (now_ms // bucket_ms) * bucket_ms
        first_bucket_start = last_bucket_start - 59 * bucket_ms
        self.store.insert_sample(
            first_bucket_start + 1,
            "spot_rest",
            "Operational",
            {"rtt_ms": 10},
            commit=False,
        )
        self.store.insert_sample(
            last_bucket_start + 1,
            "spot_rest",
            "Operational",
            {"rtt_ms": 20},
            commit=False,
        )
        self.store.commit()

        result = self.store.history("spot_rest", "rtt_ms", "1h", now_ms)

        self.assertEqual(result["from_ms"], first_bucket_start)
        self.assertEqual(result["to_ms"], now_ms)
        self.assertEqual(
            [bucket["t"] for bucket in result["buckets"]],
            [first_bucket_start, last_bucket_start],
        )
        self.assertTrue(all(
            bucket["t"] % bucket_ms == 0 for bucket in result["buckets"]
        ))

    def test_history_uses_the_frozen_worst_status_order(self) -> None:
        statuses = [
            "Operational", "Off", "Unknown", "Degraded", "Stale", "Outage"
        ]
        start_ms = NOW_MS - HOUR_MS + 60_000
        for index, status in enumerate(statuses):
            self.store.insert_sample(
                start_ms + index * 60_000 + 1,
                "spot_rest",
                status,
                {},
                commit=False,
            )
            if index:
                self.store.insert_sample(
                    start_ms + index * 60_000 + 2,
                    "spot_rest",
                    statuses[index - 1],
                    {},
                    commit=False,
                )
        self.store.commit()

        result = self.store.history(
            "spot_rest", "status", "1h", NOW_MS
        )

        self.assertEqual(
            [bucket["status"] for bucket in result["buckets"]], statuses
        )
        self.assertTrue(all(
            "avg" not in bucket and "max" not in bucket
            for bucket in result["buckets"]
        ))

    def test_metric_history_averages_only_numeric_json_values(self) -> None:
        values = [10, 20.5, None, "30", True]
        for offset, value in enumerate(values):
            metrics = {} if value is None else {"rtt_ms": value}
            self.store.insert_sample(
                NOW_MS - 30_000 + offset,
                "spot_rest",
                "Operational",
                metrics,
                commit=False,
            )
        self.store.commit()

        bucket = self.store.history(
            "spot_rest", "rtt_ms", "1h", NOW_MS
        )["buckets"][0]

        self.assertEqual(bucket["n"], 5)
        self.assertEqual(bucket["avg"], 15.25)
        self.assertEqual(bucket["max"], 20.5)

    def test_r1_store_malformed_persisted_json_is_fail_closed(self) -> None:
        connection = sqlite3.connect(str(self.path))
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO samples(ts_ms, edge_id, status, metrics) "
            "VALUES(?, ?, ?, ?)",
            (NOW_MS - 1, "spot_rest", "Operational", "{"),
        )
        connection.commit()

        status = self.store.history(
            "spot_rest", "status", "1h", NOW_MS
        )
        numeric = self.store.history(
            "spot_rest", "rtt_ms", "1h", NOW_MS
        )

        self.assertEqual(status["buckets"][0]["status"], "Unknown")
        self.assertEqual(status["buckets"][0]["n"], 1)
        self.assertEqual(numeric["buckets"], [])

    def test_empty_history_and_unknown_range_are_explicit(self) -> None:
        result = self.store.history(
            "edge_without_samples", "status", "24h", NOW_MS
        )
        self.assertEqual(result["buckets"], [])
        with self.assertRaisesRegex(ValueError, "unknown history range"):
            self.store.history("spot_rest", "status", "7d", NOW_MS)

    def test_history_never_returns_more_than_720_recent_buckets(self) -> None:
        custom_range = (721_000, 1_000)
        start_ms = NOW_MS - 720_000
        with mock.patch.dict(
            network_store.RANGES, {"test_cap": custom_range}
        ):
            for index in range(721):
                self.store.insert_sample(
                    start_ms + index * 1_000,
                    "spot_rest",
                    "Operational",
                    {},
                    commit=False,
                )
            self.store.commit()
            buckets = self.store.history(
                "spot_rest", "status", "test_cap", NOW_MS
            )["buckets"]

        self.assertEqual(len(buckets), 720)
        self.assertEqual(buckets[0]["t"], start_ms + 1_000)
        self.assertEqual(buckets[-1]["t"], start_ms + 720_000)

    def test_events_cursor_is_oldest_first_and_latest_is_newest_first(self) -> None:
        first_id = self.store.insert_event(
            NOW_MS - 1, "spot_rest", "degraded", "warning",
            "one probe was delayed", {"rtt_ms": 40},
        )
        second_id = self.store.insert_event(
            NOW_MS - 3, None, "collector", "info",
            "collector started", {},
        )
        third_id = self.store.insert_event(
            NOW_MS - 2, "spot_rest", "recovered", "info",
            "two probes succeeded", {"count": 2},
        )

        first_page = self.store.events_after(0, 2)
        second_page = self.store.events_after(first_page["next_after"], 50)

        self.assertEqual(
            [row["event_id"] for row in first_page["events"]],
            [first_id, second_id],
        )
        self.assertEqual(first_page["next_after"], second_id)
        self.assertEqual(second_page["events"][0]["event_id"], third_id)
        self.assertEqual(second_page["events"][0]["evidence"], {"count": 2})
        self.assertEqual(
            [row["event_id"] for row in self.store.latest_events()],
            [first_id, third_id, second_id],
        )
        self.assertEqual(
            self.store.events_after(third_id, 50),
            {"events": [], "next_after": third_id},
        )

    def test_prune_applies_retention_and_runs_at_most_hourly(self) -> None:
        sample_cutoff = NOW_MS - 48 * HOUR_MS
        event_cutoff = NOW_MS - 30 * DAY_MS
        for ts_ms in (sample_cutoff - 1, sample_cutoff, NOW_MS):
            self.store.insert_sample(
                ts_ms, "spot_rest", "Operational", {}, commit=False
            )
        self.store.commit()
        for ts_ms in (event_cutoff - 1, event_cutoff, NOW_MS):
            self.store.insert_event(
                ts_ms, "spot_rest", "sample", "info", "retention", {}
            )

        result = self.store.prune(NOW_MS)
        self.store.insert_sample(
            sample_cutoff - 2, "spot_rest", "Operational", {}
        )
        skipped = self.store.prune(NOW_MS + HOUR_MS - 1)

        self.assertEqual(result["samples_deleted"], 1)
        self.assertEqual(result["events_deleted"], 1)
        self.assertEqual(len(self._rows("samples")), 3)
        self.assertEqual(len(self._rows("events")), 2)
        self.assertEqual(skipped["samples_deleted"], 0)
        self.assertEqual(skipped["events_deleted"], 0)
        self.assertEqual(result["bytes"], self.store.size_bytes())

    def test_r1_size_cap_test_asserts_configured_cap(self) -> None:
        for index in range(5_000):
            self.store.insert_sample(
                NOW_MS - HOUR_MS + index,
                "spot_rest",
                "Operational",
                {"padding": "x" * 256},
                commit=False,
            )
        self.store.commit()
        measured_size = self.store.size_bytes()
        self.store.size_cap_bytes = measured_size // 2

        result = self.store.prune(NOW_MS)

        self.assertTrue(result["cap_met"])
        self.assertLessEqual(result["bytes"], self.store.size_cap_bytes)
        self.assertEqual(result["bytes"], self.store.size_bytes())

    def test_r1_store_size_cap_removes_oldest_recent_samples(self) -> None:
        oldest_ms = NOW_MS - HOUR_MS
        for index in range(2_500):
            self.store.insert_sample(
                oldest_ms + index,
                "spot_rest",
                "Operational",
                {"padding": "x" * 512},
                commit=False,
            )
        self.store.commit()
        measured_size = self.store.size_bytes()
        self.store.size_cap_bytes = measured_size * 3 // 4

        result = self.store.prune(NOW_MS)
        remaining = self._rows("samples")

        self.assertTrue(result["cap_met"])
        self.assertLessEqual(result["bytes"], self.store.size_cap_bytes)
        self.assertGreater(result["samples_deleted"], 0)
        self.assertGreater(len(remaining), 0)
        self.assertGreaterEqual(min(row[0] for row in remaining), oldest_ms + 1_000)

    def test_corrupt_database_is_moved_aside_and_reset_is_noted(self) -> None:
        self.store.close()
        corrupt_path = self.root / "corrupt.sqlite3"
        original = b"offline fixture that is not a database"
        corrupt_path.write_bytes(original)
        self.path = corrupt_path
        self.store = NetworkStore(str(corrupt_path))
        self.addCleanup(self.store.close)

        with mock.patch.object(network_store.time, "time", return_value=1234.567):
            self.store.open()

        moved_path = Path(str(corrupt_path) + ".corrupt-1234567")
        self.assertEqual(moved_path.read_bytes(), original)
        self.assertIn("storage reset", self.store.reset_note)
        self.store.insert_sample(
            NOW_MS, "spot_rest", "Unknown", {"rtt_ms": None}
        )
        self.assertEqual(len(self._rows("samples")), 1)


if __name__ == "__main__":
    unittest.main()
