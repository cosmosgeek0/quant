from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ui import network_api, network_health
from ui.network_store import NetworkStore


NOW_MS = 1_700_000_000_000


class NetworkApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sources = {
            "network_current": self.root / "network_current.json",
            "network_config": self.root / "network_config.json",
            "network_probe_request": self.root / "network_probe_request.json",
            "network_db": self.root / "network_history.sqlite3",
            "ui_release": self.root / "RELEASE",
        }
        network_api._CONFIG_COOLDOWNS.clear()
        network_api._LAST_RECEIPT_ID = 0

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, key, value):
        self.sources[key].write_text(json.dumps(value), encoding="utf-8")

    def open_store(self):
        store = NetworkStore(str(self.sources["network_db"]))
        store.open()
        return store

    def test_current_file_applies_bound_and_release(self):
        current = {
            "schema_version": 1, "generated_ms": NOW_MS,
            "collector": {"status": "running"},
            "edges": [], "events": [{"event_id": value} for value in range(25)],
        }
        self.write_json("network_current", current)
        self.sources["ui_release"].write_text("release-7\n", encoding="utf-8")
        result = network_api.api_network(self.sources, now=NOW_MS)
        self.assertEqual(result["deployed_ui_release"], "release-7")
        self.assertEqual(len(result["events"]), 20)
        self.assertEqual(result["collector"]["status"], "running")

    def test_missing_current_is_static_unknown(self):
        result = network_api.api_network(self.sources, now=NOW_MS)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["collector"]["status"], "unknown")
        self.assertEqual(len(result["edges"]), 13)
        self.assertEqual(result["summary"]["unknown_count"], 13)
        self.assertTrue(all(edge["health"] == "Unknown" for edge in result["edges"]))
        self.assertTrue(all(edge["health_reason"] == "Recorder has not written yet"
                            for edge in result["edges"]))

    def test_current_applies_real_read_time_staleness(self):
        edge = dict(network_health.EDGES[3])
        edge.update({
            "health": "Operational", "health_reason": "fresh",
            "observed_at": network_api._iso(NOW_MS - 20_000),
            "observed_ms": NOW_MS - 20_000,
            "fresh_until_ms": NOW_MS - 1, "metrics": {}, "counters": {},
            "sample_count": 2,
        })
        self.write_json("network_current", {
            "generated_ms": NOW_MS - 20_000,
            "collector": {"status": "running", "tick_s": 5},
            "summary": {"last_transition": None}, "events": [], "edges": [edge],
        })
        result = network_api.api_network(self.sources, now=NOW_MS)
        self.assertEqual(result["collector"]["status"], "stopped")
        self.assertEqual(result["edges"][0]["health"], "Stale")
        self.assertIn("No fresh sample since", result["edges"][0]["health_reason"])

    def test_current_survives_missing_health_module(self):
        current = {"schema_version": 1, "collector": {"status": "running"},
                   "edges": [], "events": []}
        self.write_json("network_current", current)
        real_import = __import__

        def import_without_health(name, *args, **kwargs):
            if name == "ui.network_health":
                raise ModuleNotFoundError("health module absent",
                                          name="ui.network_health")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=import_without_health):
            result = network_api.api_network(self.sources, now=NOW_MS)
        self.assertEqual(result["collector"]["status"], "running")
        self.assertIsNone(result["deployed_ui_release"])

    def test_history_queries_real_store_with_bounded_shape(self):
        store = self.open_store()
        store.insert_sample(NOW_MS - 1_000, "spot_rest", "Operational",
                            {"rtt_ms": 12})
        store.close()
        with mock.patch.object(network_api, "_now_ms", return_value=NOW_MS):
            status, result = network_api.api_network_history(self.sources, {
                "edge_id": "spot_rest", "metric": "rtt_ms", "range": "1h"})
        self.assertEqual(status, 200)
        self.assertLessEqual(len(result["buckets"]), 720)
        self.assertEqual(result["buckets"][0]["n"], 1)
        self.assertEqual(result["buckets"][0]["status"], "Operational")
        self.assertEqual(result["buckets"][0]["avg"], 12.0)
        with mock.patch.object(network_api, "_now_ms", return_value=NOW_MS):
            status, status_result = network_api.api_network_history(self.sources, {
                "edge_id": "spot_rest", "metric": "status", "range": "1h"})
            counter_status, counter_result = network_api.api_network_history(
                self.sources, {"edge_id": "spot_rest", "metric": "count_429",
                               "range": "1h"})
        self.assertEqual(status, 200)
        self.assertNotIn("avg", status_result["buckets"][0])
        self.assertEqual(counter_status, 200)
        self.assertIsNone(counter_result["buckets"][0]["avg"])

    def test_history_rejects_unknown_values(self):
        queries = (
            {"edge_id": "not_an_edge", "metric": "status", "range": "1h"},
            {"edge_id": "spot_rest", "metric": "book_age_p50_ms", "range": "1h"},
            {"edge_id": "spot_rest", "metric": "status", "range": "2h"},
        )
        for query in queries:
            with self.subTest(query=query):
                status, _result = network_api.api_network_history(self.sources, query)
                self.assertEqual(status, 400)

    def test_missing_history_has_empty_shape_and_note(self):
        with mock.patch.object(network_api, "_now_ms", return_value=NOW_MS):
            status, result = network_api.api_network_history(self.sources, {
                "edge_id": "feed_machine", "metric": "status", "range": "6h"})
        self.assertEqual(status, 200)
        self.assertEqual(result["buckets"], [])
        self.assertEqual(result["bucket_s"], 300)
        self.assertEqual(result["from_ms"], NOW_MS - 21_600_000)
        self.assertEqual(result["note"], "history not yet recorded")

    def test_events_use_cursor_limit_and_oldest_order(self):
        store = self.open_store()
        for value in range(1, 7):
            store.insert_event(NOW_MS + value, "spot_rest", "change", "info",
                               "measured change", {"value": value})
        store.close()
        status, result = network_api.api_network_events(
            self.sources, {"after": "2", "limit": "3"})
        self.assertEqual(status, 200)
        self.assertEqual([row["event_id"] for row in result["events"]], [3, 4, 5])
        self.assertEqual(result["next_after"], 5)

    def test_events_validate_and_missing_store_is_empty(self):
        for query in ({"after": "-1"}, {"limit": "0"}, {"limit": "201"},
                      {"after": "one"}):
            with self.subTest(query=query):
                status, _result = network_api.api_network_events(self.sources, query)
                self.assertEqual(status, 400)
        status, result = network_api.api_network_events(self.sources, {})
        self.assertEqual(status, 200)
        self.assertEqual(result, {"events": [], "next_after": 0})

    def test_config_write_records_receipt_reread_and_cooldown(self):
        clock = {"now": NOW_MS}
        with mock.patch.object(network_api, "_now_ms",
                               side_effect=lambda: clock["now"]):
            saved, error = network_api.write_network_config(
                self.sources, {"key": "rest_probe_s", "value": 30})
            self.assertIsNone(error)
            self.assertEqual(saved["old"], 15)
            self.assertEqual(saved["new"], 30)
            self.assertTrue(saved["reread"])
            on_disk = json.loads(self.sources["network_config"].read_text(
                encoding="utf-8"))
            self.assertEqual(on_disk["rest_probe_s"], 30)
            self.assertEqual(on_disk["receipt_id"], saved["receipt_id"])
            store = self.open_store()
            event = store.events_after(0, 1)["events"][0]
            store.close()
            self.assertEqual(event["transition"], "config")
            self.assertEqual(event["evidence"], saved)
            repeated, repeated_error = network_api.write_network_config(
                self.sources, {"key": "rest_probe_s", "value": 60})
        self.assertIsNone(repeated)
        self.assertEqual(repeated_error, "cooldown 2 s")

    def test_config_paused_allowlist_and_origin_checks(self):
        with mock.patch.object(network_api, "_now_ms", return_value=NOW_MS):
            saved, error = network_api.write_network_config(
                self.sources, {"key": "paused", "group": "news", "value": True},
                {"Origin": "https://dash.local", "Host": "dash.local",
                 "Sec-Fetch-Site": "same-origin"})
        self.assertIsNone(error)
        self.assertEqual(saved["group"], "news")
        self.assertFalse(saved["old"])
        self.assertTrue(saved["new"])
        rejected = (
            {"key": "rest_probe_s", "value": 16},
            {"key": "rest_probe_s", "value": 30.0},
            {"key": "paused", "group": "other", "value": True},
            {"key": "paused", "group": "news", "value": 1},
            {"key": "other", "value": 30},
        )
        for body in rejected:
            with self.subTest(body=body):
                self.assertIsNotNone(
                    network_api.write_network_config(self.sources, body)[1])
        saved, error = network_api.write_network_config(
            self.sources, {"key": "wss_read_s", "value": 10},
            {"Origin": "https://elsewhere.test", "Host": "dash.local"})
        self.assertIsNone(saved)
        self.assertEqual(error, ("403", "cross-origin request refused"))

    def test_probe_accepts_allowlist_and_reports_current_cooldown(self):
        old_probe = network_api._iso(NOW_MS - 31_000)
        self.write_json("network_current", {
            "collector": {"probe_now": {"spot_rest": old_probe}}})
        with mock.patch.object(network_api, "_now_ms", return_value=NOW_MS), \
                mock.patch.object(network_api.secrets, "token_hex",
                                  return_value="abc123def456"):
            saved, error = network_api.write_network_probe(
                self.sources, {"edge_id": "spot_rest"})
        self.assertIsNone(error)
        self.assertEqual(saved, {"edge_id": "spot_rest",
                                 "request_id": "abc123def456"})
        request = json.loads(self.sources["network_probe_request"].read_text(
            encoding="utf-8"))
        self.assertEqual(request["requested_at"], network_api._iso(NOW_MS))
        self.write_json("network_current", {"collector": {"probe_now": {
            "spot_rest": network_api._iso(NOW_MS - 3_000)}}})
        with mock.patch.object(network_api, "_now_ms", return_value=NOW_MS):
            saved, error = network_api.write_network_probe(
                self.sources, {"edge_id": "spot_rest"})
        self.assertIsNone(saved)
        self.assertEqual(error, "cooldown 27 s")

    def test_probe_rejects_unknown_edge_and_cross_origin(self):
        self.assertIsNotNone(network_api.write_network_probe(
            self.sources, {"edge_id": "spot_wss"})[1])
        saved, error = network_api.write_network_probe(
            self.sources, {"edge_id": "cf_connector"},
            {"Host": "dash.local", "Sec-Fetch-Site": "cross-site"})
        self.assertIsNone(saved)
        self.assertEqual(error, ("403", "cross-origin request refused"))


if __name__ == "__main__":
    unittest.main()
