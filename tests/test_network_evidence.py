from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ui import network_health as health
from ui.network_evidence import (
    read_feed_connections,
    read_feed_source,
    read_last_job,
    read_manifest,
    read_news,
    read_stream_state,
)


FIXTURES = Path(__file__).parent / "fixtures" / "network"


def epoch_ms(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


class FeedConnectionTests(unittest.TestCase):
    def large_feed(self):
        sample = json.loads(
            (FIXTURES / "feed_state_sample.json").read_text(encoding="utf-8")
        )
        return {
            "connections": sample["connections"],
            "symbols": {"SYNTHETIC": "x" * (1024 * 1024)},
            "version": 1,
            "window": 60,
            "window_newest_ms": "1786897849490",
            "window_oldest_ms": "1786897789490",
            "written_ms": sample["written_ms"],
        }

    def test_sample_reports_each_connection_once(self):
        now_ms = 1786897850490

        result = read_feed_connections(
            FIXTURES / "feed_state_sample.json", now_ms
        )

        self.assertEqual(result["written_ms"], 1786897849490)
        self.assertEqual(result["snapshot_age_ms"], 1000)
        self.assertEqual(
            result["spot"],
            {
                "expected_connections": 4,
                "connected_count": 4,
                "stream_count": 621,
                "last_message_age_ms": 1007,
                "dropped_frames_total": 199515,
                "feed_status": "healthy",
                "successful_handshakes": 4,
                "handshake_failures": 0,
                "reconnects": 0,
            },
        )
        self.assertEqual(result["perp"]["connected_count"], 4)
        self.assertEqual(result["perp"]["stream_count"], 525)

    def test_degraded_ticker_connections_are_not_counted(self):
        result = read_feed_connections(
            FIXTURES / "feed_state_degraded.json", 1786897865490
        )

        self.assertEqual(result["perp"]["connected_count"], 3)
        self.assertEqual(result["perp"]["reconnects"], 2)
        self.assertEqual(result["perp"]["feed_status"], "degraded")
        self.assertEqual(result["snapshot_age_ms"], 1000)

    def test_venue_stream_count_takes_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "feed.json"
            path.write_text(
                json.dumps(
                    {
                        "written_ms": "1000",
                        "connections": {
                            "spot": {
                                "stream_count": "7",
                                "shards": [
                                    {"connected": True, "stream_count": "bad"}
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = read_feed_connections(path, 1200)

        self.assertEqual(result["spot"]["stream_count"], 7)
        self.assertEqual(result["spot"]["connected_count"], 1)

    def test_missing_and_malformed_files_return_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = root / "feed.json"
            malformed.write_text("{", encoding="utf-8")

            self.assertIsNone(read_feed_connections(root / "missing", 1))
            self.assertIsNone(read_feed_connections(malformed, 1))

    def test_source_fast_path_matches_full_parse_on_one_megabyte_file(self):
        now_ms = 1786897850490
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "feed.json"
            path.write_text(
                json.dumps(self.large_feed(), sort_keys=True), encoding="utf-8"
            )

            full = read_feed_connections(path, now_ms)
            result = read_feed_source(path, now_ms)
            stat = path.stat()

        self.assertGreater(stat.st_size, 1024 * 1024)
        self.assertEqual(
            {key: result[key] for key in full},
            full,
        )
        self.assertFalse(result["full_parse"])
        self.assertEqual(result["source_bytes"], stat.st_size)
        self.assertEqual(
            result["source_mtime_ms"], stat.st_mtime_ns // 1_000_000
        )

    def test_source_changed_key_order_uses_one_full_parse(self):
        now_ms = 1786897850490
        payload = self.large_feed()
        changed_order = {
            "symbols": payload["symbols"],
            "connections": payload["connections"],
            "version": payload["version"],
            "window": payload["window"],
            "window_newest_ms": payload["window_newest_ms"],
            "window_oldest_ms": payload["window_oldest_ms"],
            "written_ms": payload["written_ms"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "feed.json"
            path.write_text(json.dumps(changed_order), encoding="utf-8")

            full = read_feed_connections(path, now_ms)
            result = read_feed_source(path, now_ms)

        self.assertEqual({key: result[key] for key in full}, full)
        self.assertTrue(result["full_parse"])

    def test_source_truncated_write_and_missing_file_return_none(self):
        payload = json.dumps(self.large_feed(), sort_keys=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "feed.json"
            path.write_text(payload[:-20], encoding="utf-8")

            self.assertIsNone(read_feed_source(path, 1))
            self.assertIsNone(read_feed_source(root / "missing.json", 1))

    def test_r2_connected_missing_remains_unknown_unless_feed_is_broken(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "feed.json"
            path.write_text(
                json.dumps(
                    {
                        "written_ms": "1000",
                        "connections": {
                            "spot": {
                                "expected_connections": 1,
                                "status": "healthy",
                                "ticker": {"last_message_ms": "900"},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = read_feed_connections(path, 1000)

        self.assertIsNone(result["spot"]["connected_count"])
        evidence = dict(
            result["spot"], snapshot_age_ms=result["snapshot_age_ms"]
        )
        config = {"wss_read_s": 15, "paused": {"wss": False}}
        unknown = health.derive_edge("spot_wss", evidence, None, config, 1000)
        self.assertEqual(unknown["health"], "Unknown")
        evidence["feed_status"] = "broken"
        broken = health.derive_edge("spot_wss", evidence, None, config, 1000)
        self.assertEqual(broken["health"], "Outage")


class StreamStateTests(unittest.TestCase):
    def test_sample_uses_nearest_rank_percentiles(self):
        now_ms = epoch_ms("2026-08-16T16:34:20.417Z")

        result = read_stream_state(
            FIXTURES / "stream_state_sample.json", now_ms
        )

        self.assertEqual(
            result,
            {
                "symbols_total": 3,
                "book_age_p50_ms": 10744,
                "book_age_p95_ms": 15700,
                "book_age_max_ms": 15700,
                "symbols_book_le_5s": 0,
                "freshness_p50_ms": 366728,
                "stream_row_age_ms": 4000,
                "source": None,
            },
        )

    def test_r2_partial_stream_rows_aggregate_fields_independently(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stream.json"
            path.write_text(
                json.dumps(
                    {
                        "BOOK": {
                            "book_age_ms": "100",
                            "utc": "2026-08-16T16:34:00.250Z",
                        },
                        "FRESH": {
                            "book_age_ms": "unknown",
                            "freshness_ms": "200",
                        },
                        "PARTIAL": {
                            "utc": "not-a-time",
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = read_stream_state(
                path, epoch_ms("2026-08-16T16:34:01.250Z")
            )

        self.assertEqual(result["symbols_total"], 3)
        self.assertEqual(result["book_age_p95_ms"], 100)
        self.assertEqual(result["freshness_p50_ms"], 200)
        self.assertEqual(result["stream_row_age_ms"], 1000)

    def test_empty_object_is_valid_but_has_unknown_percentiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stream.json"
            path.write_text("{}", encoding="utf-8")

            result = read_stream_state(path, 1)

        self.assertEqual(result["symbols_total"], 0)
        self.assertIsNone(result["book_age_p50_ms"])
        self.assertIsNone(result["stream_row_age_ms"])

    def test_r6_utc_only_stream_row_has_unknown_book_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stream.json"
            path.write_text(
                json.dumps({"UTC_ONLY": {"utc": "2026-08-16T16:34:00Z"}}),
                encoding="utf-8",
            )

            result = read_stream_state(
                path, epoch_ms("2026-08-16T16:34:01Z")
            )

        self.assertIsNone(result["symbols_book_le_5s"])


class NewsTests(unittest.TestCase):
    def test_sample_uses_aggregate_counts_and_status_errors(self):
        now_ms = epoch_ms("2026-08-16T16:34:07Z")
        with tempfile.TemporaryDirectory() as temporary:
            news_path = Path(temporary) / "news.jsonl"
            news_path.write_text("{}\n", encoding="utf-8")
            modified_ms = now_ms - 5000
            os.utime(
                news_path,
                ns=(modified_ms * 1_000_000, modified_ms * 1_000_000),
            )

            result = read_news(
                FIXTURES / "news_sources_sample.json", news_path, now_ms
            )

        self.assertEqual(
            result,
            {
                "sources_available": 104,
                "sources_live": 74,
                "sources_pending": 15,
                "sources_error": 1,
                "as_of_age_s": 60.0,
                "last_fetch_age_s": 5.0,
            },
        )

    def test_missing_news_jsonl_keeps_fetch_age_unknown(self):
        result = read_news(
            FIXTURES / "news_sources_sample.json", "/missing/news.jsonl", 0
        )

        self.assertIsNone(result["last_fetch_age_s"])

    def test_malformed_sources_file_returns_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sources.json"
            path.write_text("[]", encoding="utf-8")

            self.assertIsNone(read_news(path, path, 0))


class JobAndManifestTests(unittest.TestCase):
    def test_last_valid_job_exposes_only_allowed_fields(self):
        result = read_last_job(
            FIXTURES / "jobs_results_sample.jsonl"
        )

        self.assertEqual(
            result,
            {
                "last_used_at": "2026-08-16T09:39:13.497746Z",
                "last_op": "MARGIN_REPAY",
                "last_outcome": "CLOSED",
            },
        )
        self.assertNotIn("receipt_path", result)
        self.assertNotIn("job_id", result)

    def test_last_job_skips_malformed_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "results.jsonl"
            path.write_text(
                '{"utc":"first","op":"A","verdict":"OPEN"}\n'
                'not json\n'
                '{"utc":"last","op":"B","verdict":"CLOSED"}\n'
                '{\n',
                encoding="utf-8",
            )

            result = read_last_job(path)

        self.assertEqual(result["last_used_at"], "last")
        self.assertEqual(result["last_op"], "B")

    def test_manifest_shapes_are_normalized(self):
        box = read_manifest(FIXTURES / "pull_manifest_box.json")
        singular = read_manifest(
            FIXTURES / "pull_manifest_error_string.json"
        )
        plural = read_manifest(
            FIXTURES / "pull_manifest_errors_list.json"
        )

        self.assertEqual(box["errors"], [])
        self.assertEqual(singular["errors"], ["ssm session failed"])
        self.assertNotIn("error", singular)
        self.assertEqual(
            plural["errors"],
            ["feed_state.json: source missing, previous copy kept"],
        )

    def test_r2_manifest_output_is_allowlisted_and_errors_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "utc": "2026-08-16T16:34:00Z",
                        "ok": False,
                        "interval_ms": 5000,
                        "cycle_ms": 40,
                        "files": ["feed_state.json"],
                        "connectorId": "hidden",
                        "receipt_path": "/hidden/path",
                        "errors": (
                            [{"job_id": "hidden"}, "x" * 250]
                            + ["error-%02d" % index for index in range(25)]
                        ),
                        "error": "legacy error",
                    }
                ),
                encoding="utf-8",
            )

            result = read_manifest(path)

        self.assertEqual(
            set(result),
            {"utc", "ok", "interval_ms", "cycle_ms", "files", "errors"},
        )
        self.assertEqual(len(result["errors"]), 20)
        self.assertTrue(all(isinstance(item, str) for item in result["errors"]))
        self.assertTrue(all(len(item) <= 200 for item in result["errors"]))
        self.assertEqual(result["errors"][0], "x" * 200)

    def test_r6_nested_manifest_values_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "utc": {"value": "2026-08-16T16:34:00Z"},
                        "ok": [True],
                        "interval_ms": {"value": 5000},
                        "cycle_ms": [40],
                        "files": {"value": 8},
                        "errors": [{"message": "not public"}],
                        "error": ["not public"],
                    }
                ),
                encoding="utf-8",
            )

            result = read_manifest(path)

        self.assertEqual(
            result,
            {
                "utc": None,
                "ok": None,
                "interval_ms": None,
                "cycle_ms": None,
                "files": None,
                "errors": [],
            },
        )

    def test_missing_job_and_manifest_return_none(self):
        self.assertIsNone(read_last_job("/missing/results.jsonl"))
        self.assertIsNone(read_manifest("/missing/manifest.json"))


if __name__ == "__main__":
    unittest.main()
