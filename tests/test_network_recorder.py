from __future__ import annotations

"""Offline deterministic tests for the Network V1 recorder."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

from ui import network_recorder as recorder


EDGE_ROWS = (
    ("browser_site", "browser", "quant_ui", "Session", False, "session"),
    ("cf_connector", "cloudflare", "cloudflared", "Continuous", True, "cf"),
    ("connector_origin", "cloudflared", "quant_ui", "Periodic", True, "cf"),
    ("spot_rest", "box", "binance_spot", "Periodic", True, "rest"),
    ("spot_wss", "box", "binance_spot", "Continuous", True, "wss"),
    ("usdm_rest", "box", "binance_usdm", "Periodic", True, "rest"),
    ("usdm_wss", "box", "binance_usdm", "Continuous", True, "wss"),
    ("feed_machine", "feed", "machine", "Continuous", True, "wss"),
    ("news_fetch", "news", "box", "Periodic", True, "news"),
    ("binance_account", "box", "binance_account", "On-demand", False, "ondemand"),
    ("binance_exec", "box", "binance_exec", "On-demand", False, "ondemand"),
    ("aws_secrets", "box", "aws_secrets", "On-demand", False, "ondemand"),
    ("aws_ssm", "box", "aws_ssm", "On-demand", False, "ondemand"),
)
EDGES = [
    {
        "id": edge_id,
        "source": source,
        "target": target,
        "label": edge_id.replace("_", " ").title(),
        "direction": "<->" if edge_id in ("spot_wss", "usdm_wss") else "->",
        "mode": mode,
        "required": required,
        "group": group,
        "authority": "Read-only" if group == "wss" else "Public",
        "controls": [],
    }
    for edge_id, source, target, mode, required, group in EDGE_ROWS
]
NODE_IDS = {row[1] for row in EDGE_ROWS}.union(row[2] for row in EDGE_ROWS)
NODES = [
    {"id": node_id, "label": node_id.replace("_", " ").title(), "kind": "fixture"}
    for node_id in sorted(NODE_IDS)
]
EDGE_BY_ID = {row["id"]: row for row in EDGES}
REQUIRED_IDS = {row["id"] for row in EDGES if row["required"]}
BASE_NOW_MS = 1_787_000_000_000


class MemoryStore:
    databases = {}

    def __init__(self, path):
        self.path = path
        self.data = self.databases.setdefault(path, {
            "samples": [], "events": [], "prunes": [], "close_count": 0,
        })

    def open(self):
        self.data["opened"] = True

    def close(self):
        self.data["close_count"] += 1

    def insert_sample(self, ts_ms, edge_id, status, metrics):
        self.data["samples"].append({
            "ts_ms": ts_ms,
            "edge_id": edge_id,
            "status": status,
            "metrics": deepcopy(metrics),
        })

    def insert_event(self, ts_ms, edge_id, transition, severity, reason, evidence):
        event = {
            "event_id": len(self.data["events"]) + 1,
            "ts": recorder._iso(ts_ms),
            "edge_id": edge_id,
            "transition": transition,
            "severity": severity,
            "reason": reason,
            "evidence": deepcopy(evidence),
        }
        self.data["events"].append(event)
        return event["event_id"]

    def latest_events(self, limit=20):
        return deepcopy(self.data["events"][-limit:])

    def prune(self, now_ms):
        self.data["prunes"].append(now_ms)
        return {"samples_deleted": 0, "events_deleted": 0, "bytes": 1}


class FakeWorld:
    def __init__(self):
        self.now = BASE_NOW_MS
        self.monotonic = 50_000.0
        self.calls = {
            "rest": [], "discover": [], "scrape": [], "ready": [],
            "origin": [], "feed": [], "stream": [], "news": [], "job": [],
        }
        self.rest_results = {}
        self.previous_seen = []
        self.probe_derived = set()

    def deps(self):
        return {
            "NetworkStore": MemoryStore,
            "probe_time": self.probe_time,
            "discover_metrics_url": self.discover_metrics_url,
            "scrape_metrics": self.scrape_metrics,
            "read_ready": self.read_ready,
            "probe_origin": self.probe_origin,
            "cf_metrics_from_raw": self.cf_metrics_from_raw,
            "read_feed_connections": self.read_feed_connections,
            "read_stream_state": self.read_stream_state,
            "read_news": self.read_news,
            "read_last_job": self.read_last_job,
            "derive_edge": self.derive_edge,
            "transitions": self.transitions,
            "summarize": self.summarize,
            "get_edges": lambda: EDGES,
            "get_nodes": lambda: NODES,
            "now_ms": lambda: self.now,
            "monotonic_ms": lambda: self.monotonic,
            "sleep": self.unexpected_sleep,
        }

    def unexpected_sleep(self, _seconds):
        raise AssertionError("once mode must not sleep")

    def probe_time(self, edge_id, opener=None, timeout_s=5.0, now_ms=None):
        self.calls["rest"].append((edge_id, now_ms))
        default = {
            "ok": True,
            "http_status": 200,
            "rtt_ms": 12,
            "clock_offset_ms": 1,
            "used_weight_1m": 3,
            "retry_after_s": None,
        }
        return deepcopy(self.rest_results.get(edge_id, default))

    def discover_metrics_url(self, configured, opener=None):
        self.calls["discover"].append(configured)
        return configured or "http://127.0.0.1:20241/metrics"

    def scrape_metrics(self, url, opener=None, timeout_s=3.0):
        self.calls["scrape"].append(url)
        return {"ok": True, "raw_values": {"requests": 10.0}}

    def read_ready(self, base_url, opener=None):
        self.calls["ready"].append(base_url)
        return {"ok": True, "status": 200, "ready_connections": 4}

    def probe_origin(self, url="http://127.0.0.1:8080/", opener=None,
                     timeout_s=3.0):
        self.calls["origin"].append(url)
        return {"ok": True, "http_status": 200, "rtt_ms": 3, "size_bytes": 800}

    def cf_metrics_from_raw(self, raw, previous):
        connector = {
            "ha_connections": 4,
            "total_requests_delta": None if previous is None else 0,
            "connect_streams_errors_delta": 0,
        }
        origin = {"request_errors_delta": 0, "response_5xx_delta": 0}
        return {"cf_connector": connector, "connector_origin_delta": origin}

    def read_feed_connections(self, path, now_ms):
        self.calls["feed"].append((path, now_ms))
        venue = {
            "feed_status": "healthy",
            "expected_connections": 2,
            "connected_count": 2,
            "last_message_age_ms": 10,
        }
        return {
            "spot": deepcopy(venue),
            "perp": deepcopy(venue),
            "written_ms": now_ms - 10,
            "snapshot_age_ms": 10,
        }

    def read_stream_state(self, path, now_ms):
        self.calls["stream"].append((path, now_ms))
        return {"book_age_p50_ms": 100, "symbols_total": 4}

    def read_news(self, sources_path, jsonl_path, now_ms):
        self.calls["news"].append((sources_path, jsonl_path, now_ms))
        return {
            "sources_available": 10,
            "sources_live": 10,
            "sources_error": 0,
            "as_of_age_s": 2,
        }

    def read_last_job(self, path):
        self.calls["job"].append(path)
        return None

    def derive_edge(self, edge_id, evidence, previous, config, now_ms):
        definition = EDGE_BY_ID[edge_id]
        self.previous_seen.append((edge_id, deepcopy(previous)))
        paused = definition["group"] in config["paused"] and (
            config["paused"][definition["group"]]
        )
        if paused:
            health, reason, monitoring = "Off", "Monitor paused", "Paused"
        elif evidence is None:
            health, reason, monitoring = "Unknown", "Evidence is unavailable", "Running"
        else:
            health = evidence.get("health", "Operational")
            reason, monitoring = "Fixture observation", "Running"
        old_count = previous.get("sample_count", 0) if isinstance(previous, dict) else 0
        observed = evidence is not None
        metrics = {
            key: value for key, value in (evidence or {}).items()
            if key not in ("probe_now", "health")
        }
        current = {
            **definition,
            "health": health,
            "health_reason": reason,
            "monitoring": monitoring,
            "truth": {
                "configured": "Pass" if evidence is not None else "Unknown",
                "transport": "Pass" if evidence is not None else "Unknown",
                "application": "Pass" if health == "Operational" else "Unknown",
            },
            "observed_at": recorder._iso(now_ms) if observed else (
                previous.get("observed_at") if isinstance(previous, dict) else None
            ),
            "observed_ms": now_ms if observed else (
                previous.get("observed_ms") if isinstance(previous, dict) else None
            ),
            "fresh_until_ms": now_ms + 45_000 if observed else None,
            "cadence_ms": None,
            "sample_count": old_count + (1 if observed else 0),
            "metrics": metrics,
            "counters": {},
            "evidence": {"source": "fixture", "source_age_s": 0 if observed else None},
        }
        if isinstance(evidence, dict) and evidence.get("probe_now"):
            self.probe_derived.add(id(current))
        return current

    def transitions(self, previous, current):
        rows = []
        if isinstance(previous, dict) and previous.get("health") != current["health"]:
            rows.append({
                "transition": "health",
                "severity": current["health"],
                "reason": "%s to %s" % (previous.get("health"), current["health"]),
                "evidence": {},
            })
        if id(current) in self.probe_derived:
            rows.append({
                "transition": "probe",
                "severity": "Info",
                "reason": "probe now %s" % current["id"],
                "evidence": {},
            })
        return rows

    def summarize(self, edges, now_ms):
        required = [edge for edge in edges if edge["required"]]
        observed = [edge for edge in required if edge["sample_count"] > 0]
        unhealthy = [edge for edge in edges if edge["health"] != "Operational"]
        worst = unhealthy[0] if unhealthy else edges[0]
        observed_times = [edge["observed_at"] for edge in required if edge["observed_at"]]
        transitions = [edge.get("last_transition") for edge in edges
                       if isinstance(edge.get("last_transition"), dict)]
        latest = max(transitions, key=lambda row: row["ts"]) if transitions else None
        return {
            "operational_required": sum(edge["health"] == "Operational" for edge in required),
            "observed_required": len(observed),
            "required_total": 8,
            "unknown_count": sum(edge["health"] == "Unknown" for edge in edges),
            "worst": {"edge_id": worst["id"], "health": worst["health"]},
            "oldest_required_evidence_at": min(observed_times) if observed_times else None,
            "last_transition": latest,
        }


class NetworkRecorderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.live = self.root / "live"
        self.data = self.root / "data"
        self.live.mkdir()
        MemoryStore.databases.clear()
        self.world = FakeWorld()

    def args(self):
        return [
            "--live-dir", str(self.live),
            "--data-dir", str(self.data),
            "--once", "-q",
        ]

    def run_once(self):
        self.assertEqual(recorder.main(self.args(), deps=self.world.deps()), 0)
        return json.loads((self.live / "network_current.json").read_text(encoding="utf-8"))

    def database(self):
        path = str(self.data / "network_history.sqlite3")
        return MemoryStore.databases[path]

    def write_config(self, **changes):
        config = recorder.default_config()
        for key, value in changes.items():
            if key == "paused":
                config["paused"].update(value)
            else:
                config[key] = value
        path = self.live / "network_config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def write_probe(self, edge_id):
        path = self.live / "network_probe_request.json"
        path.write_text(json.dumps({
            "edge_id": edge_id,
            "request_id": "abcdef",
            "requested_at": recorder._iso(self.world.now),
        }), encoding="utf-8")
        return path

    def test_once_writes_current_shape_and_samples_only_collected_edges(self):
        current = self.run_once()

        self.assertEqual(current["schema_version"], 1)
        self.assertEqual(len(current["edges"]), 13)
        self.assertIsInstance(current["summary"], dict)
        self.assertIsInstance(current["summary"]["last_transition"], dict)
        self.assertIsInstance(current["events"], list)
        self.assertEqual(current["collector"]["status"], "running")
        self.assertEqual(current["collector"]["tick_s"], 5)
        self.assertEqual(current["collector"]["recorder_release"],
                         current["deployed_ui_release"])
        self.assertFalse((self.live / "network_current.json.tmp").exists())
        self.assertLess((self.live / "network_current.json").stat().st_size, 100 * 1024)
        by_id = {edge["id"]: edge for edge in current["edges"]}
        self.assertEqual(by_id["cf_connector"]["metrics"]["ha_connections"], 4)
        self.assertEqual(by_id["connector_origin"]["metrics"]["request_errors_delta"], 0)
        samples = self.database()["samples"]
        self.assertEqual(len(samples), 8)
        self.assertEqual({row["edge_id"] for row in samples}, REQUIRED_IDS)

    def test_paused_group_is_off_without_collection_or_samples(self):
        self.write_config(paused={"wss": True})

        current = self.run_once()

        by_id = {edge["id"]: edge for edge in current["edges"]}
        for edge_id in recorder.GROUP_EDGES["wss"]:
            self.assertEqual(by_id[edge_id]["health"], "Off")
            self.assertEqual(by_id[edge_id]["monitoring"], "Paused")
        self.assertEqual(self.world.calls["feed"], [])
        self.assertEqual(self.world.calls["stream"], [])
        sample_ids = {row["edge_id"] for row in self.database()["samples"]}
        self.assertTrue(sample_ids.isdisjoint(recorder.GROUP_EDGES["wss"]))
        self.assertEqual(len(sample_ids), 5)

    def test_same_evidence_on_second_run_adds_no_health_events(self):
        self.run_once()
        first = [
            row for row in self.database()["events"]
            if row["transition"] == "health"
        ]
        self.world.now += 15_000

        self.run_once()
        second = [
            row for row in self.database()["events"]
            if row["transition"] == "health"
        ]

        self.assertEqual(len(first), 8)
        self.assertEqual(len(second), len(first))

    def test_probe_request_is_consumed_and_second_request_hits_cooldown(self):
        request = self.write_probe("spot_rest")
        first = self.run_once()

        self.assertFalse(request.exists())
        first_probe_at = first["collector"]["probe_now"]["spot_rest"]
        self.assertTrue(any(
            row["transition"] == "probe" and "spot_rest" in row["reason"]
            for row in self.database()["events"]
        ))
        self.world.now += 10_000
        second_request = self.write_probe("spot_rest")
        second = self.run_once()

        self.assertFalse(second_request.exists())
        self.assertEqual(second["collector"]["probe_now"]["spot_rest"], first_probe_at)
        self.assertTrue(any(
            row["reason"] == "probe refused (cooldown)"
            for row in self.database()["events"]
        ))

    def test_config_reloads_only_after_mtime_change(self):
        config_path = self.write_config(wss_read_s=15)
        runner = recorder.Recorder(
            self.live, self.data, quiet=True, deps=self.world.deps()
        )
        runner.start(self.world.now)
        runner.cycle(self.world.now)
        initial_feed_reads = len(self.world.calls["feed"])
        initial_mtime = config_path.stat().st_mtime_ns
        config = recorder.default_config()
        config["wss_read_s"] = 5
        config["paused"]["rest"] = True
        config_path.write_text(json.dumps(config), encoding="utf-8")
        os.utime(config_path, ns=(initial_mtime + 1_000_000,
                                  initial_mtime + 1_000_000))
        self.world.now += 1_000

        runner.cycle(self.world.now)

        current = json.loads(runner.current_path.read_text(encoding="utf-8"))
        by_id = {edge["id"]: edge for edge in current["edges"]}
        self.assertEqual(current["collector"]["config"]["wss_read_s"], 5)
        self.assertEqual(by_id["spot_rest"]["health"], "Off")
        self.assertEqual(len(self.world.calls["feed"]), initial_feed_reads + 1)
        runner.close()

    def test_previous_current_edge_seeds_next_derivation(self):
        previous = self.world.derive_edge(
            "spot_rest", {"health": "Degraded"}, None,
            recorder.default_config(), self.world.now - 1_000,
        )
        previous["sample_count"] = 7
        (self.live / "network_current.json").write_text(json.dumps({
            "edges": [previous],
            "collector": {"probe_now": {}},
            "deployed_ui_release": "dev",
        }), encoding="utf-8")
        self.world.previous_seen.clear()

        current = self.run_once()

        seen = [old for edge_id, old in self.world.previous_seen
                if edge_id == "spot_rest" and isinstance(old, dict)]
        self.assertEqual(seen[0]["health"], "Degraded")
        spot = next(edge for edge in current["edges"] if edge["id"] == "spot_rest")
        self.assertEqual(spot["sample_count"], 8)

    def test_shutdown_function_writes_stopped_event_and_closes_store(self):
        runner = recorder.Recorder(
            self.live, self.data, quiet=True, deps=self.world.deps()
        )
        runner.start(self.world.now)

        recorder.shutdown(runner, self.world.now + 5_000)

        current = json.loads(runner.current_path.read_text(encoding="utf-8"))
        self.assertEqual(current["collector"]["status"], "stopped")
        self.assertEqual(current["events"][-1]["reason"], "collector stopped")
        self.assertEqual(current["events"][-1]["transition"], "collector stopped")
        self.assertTrue(runner.closed)
        self.assertEqual(self.database()["close_count"], 1)

    def test_invalid_config_uses_defaults_with_one_warning_event(self):
        (self.live / "network_config.json").write_text(json.dumps({
            "rest_probe_s": 99,
            "paused": {"news": "yes"},
            "receipt_id": True,
            "updated_at": "not-a-time",
        }), encoding="utf-8")

        current = self.run_once()

        self.assertEqual(current["collector"]["config"]["rest_probe_s"], 15)
        self.assertFalse(current["collector"]["config"]["paused"]["news"])
        self.assertIsNone(current["collector"]["config"]["updated_at"])
        warnings = [
            row for row in self.database()["events"]
            if row["reason"] == "invalid config values used defaults"
        ]
        self.assertEqual(len(warnings), 1)

    def test_retry_after_skips_rest_venue_until_allowed_time(self):
        self.world.rest_results["spot_rest"] = {
            "ok": False,
            "http_status": 429,
            "retry_after_s": 30,
        }
        runner = recorder.Recorder(
            self.live, self.data, quiet=True, deps=self.world.deps()
        )
        runner.start(self.world.now)
        runner.cycle(self.world.now)
        self.world.now += 15_000
        runner.cycle(self.world.now)
        spot_calls_during_wait = [
            row for row in self.world.calls["rest"] if row[0] == "spot_rest"
        ]
        self.world.now += 15_000
        runner.cycle(self.world.now)
        spot_calls_after_wait = [
            row for row in self.world.calls["rest"] if row[0] == "spot_rest"
        ]

        self.assertEqual(len(spot_calls_during_wait), 1)
        self.assertEqual(len(spot_calls_after_wait), 2)
        runner.close()


if __name__ == "__main__":
    unittest.main()
