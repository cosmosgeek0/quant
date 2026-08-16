from __future__ import annotations

"""Network V1 evidence scheduler and current-state recorder."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import importlib
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
from urllib.parse import urlparse


UI_DIR = Path(__file__).resolve().parent
REPO_ROOT = UI_DIR.parent
DEFAULT_LIVE_DIR = UI_DIR / "live"
DEFAULT_DATA_DIR = REPO_ROOT / "data"
GROUP_EDGES = {
    "rest": ("spot_rest", "usdm_rest"), "wss": ("spot_wss", "usdm_wss", "feed_machine"),
    "cf": ("cf_connector", "connector_origin"), "news": ("news_fetch",),
}
INTERVAL_KEYS = {"rest": "rest_probe_s", "wss": "wss_read_s",
                 "cf": "cf_monitor_s", "news": "news_monitor_s"}
ALLOWED_INTERVALS = {"rest_probe_s": (15, 30, 60), "wss_read_s": (5, 10, 15),
                     "cf_monitor_s": (15, 30, 60), "news_monitor_s": (120, 300, 600)}
DEFAULT_INTERVALS = {"rest_probe_s": 15, "wss_read_s": 15,
                     "cf_monitor_s": 15, "news_monitor_s": 120}
PROBE_EDGES = frozenset(("spot_rest", "usdm_rest", "cf_connector", "connector_origin"))
DEPENDENCIES = {
    **{name: ("network_probe_cf", name) for name in (
        "discover_metrics_url", "scrape_metrics", "read_ready",
        "probe_origin", "cf_metrics_from_raw")},
    **{name: ("network_evidence", name) for name in (
        "read_feed_connections", "read_stream_state", "read_news", "read_last_job")},
    **{name: ("network_health", name) for name in (
        "derive_edge", "transitions", "summarize")},
    "NetworkStore": ("network_store", "NetworkStore"),
    "probe_time": ("network_probe_binance", "probe_time"),
}
PROBE_COOLDOWN_MS = 30_000
PRUNE_INTERVAL_MS = 3_600_000
HEALTH_STATES = frozenset((
    "Operational", "Degraded", "Outage", "Stale", "Unknown", "Off",
))
SEED_METRIC_KEYS = frozenset((
    "dropped_frames_total", "total_requests", "request_errors",
    "lost_packets", "connect_streams_errors",
))
CF_SEED_METRICS = {
    "total_requests": "cloudflared_tunnel_total_requests",
    "request_errors": "cloudflared_tunnel_request_errors",
    "lost_packets": "quic_client_lost_packets",
    "connect_streams_errors": "cloudflared_proxy_connect_streams_errors",
}


def _iso(timestamp_ms):
    value = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_ms(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return int(parsed.timestamp() * 1000) if parsed.tzinfo else None


def default_config():
    return {
        **DEFAULT_INTERVALS,
        "paused": {group: False for group in GROUP_EDGES},
        "updated_at": None, "receipt_id": None,
    }


def _tick(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("tick must be a positive number")
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("tick must be a positive number")
    return number


def _plain_nonnegative_int(value):
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= 0)


def _allowed_origin_url(value):
    if not isinstance(value, str) or value != value.strip():
        return False
    try:
        parsed = urlparse(value)
        parsed.port
    except ValueError:
        return False
    return (parsed.scheme.lower() == "http"
            and parsed.hostname in ("127.0.0.1", "localhost")
            and parsed.username is None and parsed.password is None)


def _safe_release(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    return value if all(char.isalnum() or char in "._-" for char in value) else None


def build_parser():
    parser = argparse.ArgumentParser(description="record Network V1 evidence")
    parser.add_argument("--live-dir", default=str(DEFAULT_LIVE_DIR))
    parser.add_argument("--data-dir", default=os.environ.get("QUANT_UI_DATA_DIR", str(DEFAULT_DATA_DIR)))
    parser.add_argument("--tick", type=_tick, default=5.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--metrics-url")
    parser.add_argument("--origin-url", default="http://127.0.0.1:8080/")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser


def _sibling(name):
    prefix = "." if __package__ else ""
    return importlib.import_module(prefix + name, __package__)


def read_config(path):
    config = default_config()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config, []
    except (OSError, UnicodeError, ValueError):
        return config, ["file"]
    if not isinstance(payload, dict):
        return config, ["file"]
    invalid = []
    for key, allowed in ALLOWED_INTERVALS.items():
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, int) and not isinstance(value, bool) and value in allowed:
            config[key] = value
        else:
            invalid.append(key)
    paused = payload.get("paused")
    if "paused" in payload and not isinstance(paused, dict):
        invalid.append("paused")
    elif isinstance(paused, dict):
        for group in GROUP_EDGES:
            if group not in paused:
                continue
            if isinstance(paused[group], bool):
                config["paused"][group] = paused[group]
            else:
                invalid.append("paused.%s" % group)
    for key, expected in (("updated_at", str), ("receipt_id", int)):
        value = payload.get(key)
        valid = value is None or (isinstance(value, expected) and not isinstance(value, bool))
        if key == "updated_at" and isinstance(value, str):
            valid = _iso_ms(value) is not None
        if valid:
            config[key] = value
        elif key in payload:
            invalid.append(key)
    return config, sorted(set(invalid))


class Recorder:
    """One process-local scheduler with injectable collection callables."""

    def __init__(self, live_dir, data_dir, tick_s=5.0, metrics_url=None,
                 origin_url="http://127.0.0.1:8080/", quiet=False, deps=None):
        self.live_dir, self.data_dir = Path(live_dir), Path(data_dir)
        self.tick_s, self.metrics_url = float(tick_s), metrics_url
        self.origin_url, self.quiet = origin_url, quiet
        self.overrides, self.resolved = dict(deps or {}), {}
        self.current_path, self.config_path, self.probe_path = (
            self.live_dir / "network_current.json", self.live_dir / "network_config.json",
            self.live_dir / "network_probe_request.json")
        self.store = None
        self.started = self.closed = self.stop_requested = False
        self.status, self.started_at, self.last_cycle_at = "unknown", None, None
        self.last_cycle_ms, self.release, self.previous_release = 0, "dev", None
        self.config, self.config_mtime = default_config(), object()
        self.edge_defs, self.nodes, self.edges = [], [], {}
        self.seed_previous = {}
        self.next_due = {group: 0 for group in GROUP_EDGES}
        self.next_allowed, self.probe_now, self.last_probe = {}, {}, {}
        self.last_prune, self.metrics_found, self.previous_cf_raw = None, None, None

    def dep(self, name):
        if name in self.resolved:
            return self.resolved[name]
        if name in self.overrides:
            value = self.overrides[name]
        elif name in ("now_ms", "monotonic_ms", "sleep"):
            value = {"now_ms": lambda: int(time.time() * 1000),
                     "monotonic_ms": lambda: time.monotonic() * 1000.0,
                     "sleep": time.sleep}[name]
        else:
            module, attribute = DEPENDENCIES[name]
            value = getattr(_sibling(module), attribute)
        self.resolved[name] = value
        return value

    def _defs(self, name):
        getter = self.overrides.get("get_%s" % name.lower())
        value = getter() if getter else self.overrides.get(name)
        if value is None:
            value = getattr(_sibling("network_health"), name)
        return deepcopy(value() if callable(value) else value)

    def now_ms(self):
        return int(self.dep("now_ms")())

    def start(self, now_ms=None):
        if self.started:
            return
        now_ms = self.now_ms() if now_ms is None else int(now_ms)
        self.live_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = self.dep("NetworkStore")(str(self.data_dir / "network_history.sqlite3"))
        self.store.open()
        self.edge_defs, self.nodes = self._defs("EDGES"), self._defs("NODES")
        self.seed()
        self.release = self.read_release()
        self.status, self.started_at, self.started = "running", _iso(now_ms), True
        reset_note = getattr(self.store, "reset_note", None)
        if isinstance(reset_note, str) and reset_note:
            self.event(now_ms, None, "storage reset", "medium", reset_note, {})
        self.event(now_ms, None, "collector started", "info", "collector started", {})
        self.reload_config(now_ms, initial=True)
        for row in self.edge_defs:
            edge_id = row["id"]
            previous = self.seed_previous.get(edge_id)
            current = self.dep("derive_edge")(
                edge_id, None, previous, self.config, now_ms)
            self._keep_uncollected_continuity(current, previous)
            self.edges[edge_id] = current
        if self.previous_release is not None and self.previous_release != self.release:
            evidence = {"old": self.previous_release, "new": self.release}
            self.event(now_ms, None, "deployed release changed", "info",
                       "deployed release changed", evidence)

    def seed(self):
        try:
            payload = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        edge_rows = payload.get("edges")
        if not isinstance(edge_rows, list):
            return
        allowed = {row["id"] for row in self.edge_defs}
        previous_cf_raw = {}
        for edge in edge_rows:
            if not isinstance(edge, dict):
                continue
            edge_id = edge.get("id")
            if not isinstance(edge_id, str) or edge_id not in allowed:
                continue
            previous = {"id": edge_id}
            counters = edge.get("counters")
            if isinstance(counters, dict):
                safe_counters = {
                    key: value for key, value in counters.items()
                    if isinstance(key, str) and _plain_nonnegative_int(value)
                }
                if safe_counters:
                    previous["counters"] = safe_counters
            metrics = edge.get("metrics")
            if isinstance(metrics, dict):
                safe_metrics = {
                    key: value for key, value in metrics.items()
                    if key in SEED_METRIC_KEYS and _plain_nonnegative_int(value)
                }
                if safe_metrics:
                    previous["metrics"] = safe_metrics
                    for key, raw_name in CF_SEED_METRICS.items():
                        if key in safe_metrics:
                            previous_cf_raw[raw_name] = safe_metrics[key]
            health = edge.get("health")
            if isinstance(health, str) and health in HEALTH_STATES:
                previous["health"] = health
            observed_ms = edge.get("observed_ms")
            if _plain_nonnegative_int(observed_ms):
                previous["observed_ms"] = observed_ms
            self.seed_previous[edge_id] = previous
        if previous_cf_raw:
            self.previous_cf_raw = previous_cf_raw
        collector = payload.get("collector", {})
        probes = collector.get("probe_now") if isinstance(collector, dict) else None
        for edge_id, value in probes.items() if isinstance(probes, dict) else ():
            parsed = _iso_ms(value)
            if edge_id in PROBE_EDGES and parsed is not None:
                self.probe_now[edge_id], self.last_probe[edge_id] = value, parsed
        previous_release = _safe_release(payload.get("deployed_ui_release"))
        if previous_release is not None:
            self.previous_release = previous_release

    def read_release(self):
        try:
            value = (UI_DIR / "RELEASE").read_text(encoding="utf-8").splitlines()[0].strip()
        except (IndexError, OSError, UnicodeError):
            return "dev"
        return _safe_release(value) or "dev"

    def event(self, now_ms, edge_id, transition, severity, reason, evidence):
        return self.store.insert_event(
            int(now_ms), edge_id, transition, severity, reason, evidence or {})

    def reload_config(self, now_ms, initial=False):
        try:
            mtime = self.config_path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if not initial and mtime == self.config_mtime:
            return False
        old, (new, invalid) = deepcopy(self.config), read_config(self.config_path)
        self.config, self.config_mtime = new, mtime
        if invalid:
            evidence = {"keys": invalid, "reread": True}
            self.event(now_ms, None, "config", "medium", "invalid config values used defaults", evidence)
        if not initial:
            for group, key in INTERVAL_KEYS.items():
                if (old[key] != new[key]
                        or old["paused"][group] != new["paused"][group]):
                    self.next_due[group] = min(self.next_due[group], now_ms)
        return True

    def derive(self, edge_id, evidence, now_ms, collected=False):
        previous = self.seed_previous.pop(edge_id, self.edges.get(edge_id))
        current = self.dep("derive_edge")(edge_id, evidence, previous, self.config, now_ms)
        if not collected and evidence is None:
            self._keep_uncollected_continuity(current, previous)
        if isinstance(previous, dict) and isinstance(previous.get("last_transition"), dict):
            current["last_transition"] = deepcopy(previous["last_transition"])
        for row in self.dep("transitions")(previous, current) or ():
            if isinstance(row, dict):
                self.event(now_ms, edge_id, row.get("transition") or "health",
                           row.get("severity") or "info", row.get("reason") or "state changed",
                           row.get("evidence") or {})
                current["last_transition"] = {
                    "ts": int(now_ms), "edge_id": edge_id,
                    "transition": row.get("transition") or "health",
                }
        self.edges[edge_id] = current
        if collected:
            metrics = current.get("metrics")
            status = current.get("health") or "Unknown"
            self.store.insert_sample(int(now_ms), edge_id, status, metrics if isinstance(metrics, dict) else {})

    def _keep_uncollected_continuity(self, current, previous):
        previous_count = previous.get("sample_count") if isinstance(previous, dict) else None
        current["sample_count"] = (
            previous_count if _plain_nonnegative_int(previous_count) else 0)
        observed_ms = previous.get("observed_ms") if isinstance(previous, dict) else None
        if _plain_nonnegative_int(observed_ms):
            current["observed_ms"] = observed_ms
            current["observed_at"] = _iso(observed_ms)
            cadence_ms = current.get("cadence_ms")
            current["fresh_until_ms"] = (
                observed_ms + 3 * cadence_ms
                if _plain_nonnegative_int(cadence_ms) else None)

    def consume_probe(self, now_ms):
        claimed_path = Path(str(self.probe_path) + ".claimed-%d" % os.getpid())
        try:
            os.replace(str(self.probe_path), str(claimed_path))
        except FileNotFoundError:
            return set()
        except OSError:
            return set()
        try:
            payload = json.loads(claimed_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            payload = None
        try:
            claimed_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        edge_id = payload.get("edge_id") if isinstance(payload, dict) else None
        if not isinstance(edge_id, str) or edge_id not in PROBE_EDGES:
            self.event(now_ms, None, "probe", "medium", "probe refused (not allowed)", {})
            return set()
        group = "rest" if edge_id.endswith("_rest") else "cf"
        last = self.last_probe.get(edge_id)
        reason = None
        if self.config["paused"][group]:
            reason = "probe refused (monitor paused)"
        elif last is not None and now_ms - last < PROBE_COOLDOWN_MS:
            reason = "probe refused (cooldown)"
        elif edge_id.endswith("_rest") and now_ms < self.next_allowed.get(edge_id, 0):
            reason = "probe refused (retry-after)"
        if reason:
            self.event(now_ms, edge_id, "probe", "info", reason, {})
            return set()
        self.probe_now[edge_id], self.last_probe[edge_id] = _iso(now_ms), now_ms
        return {edge_id}

    def collect_rest(self, edge_ids, now_ms, forced):
        for edge_id in edge_ids:
            if now_ms < self.next_allowed.get(edge_id, 0):
                continue
            evidence = self.dep("probe_time")(edge_id, now_ms=now_ms)
            evidence = dict(evidence) if isinstance(evidence, dict) else None
            retry = evidence.get("retry_after_s") if evidence else None
            if (isinstance(retry, (int, float)) and not isinstance(retry, bool)
                    and retry > 0):
                self.next_allowed[edge_id] = now_ms + int(retry * 1000)
            if edge_id in forced:
                evidence = evidence or {}
                evidence["probe_now"] = True
            self.derive(edge_id, evidence, now_ms, collected=True)

    def collect_wss(self, now_ms):
        feed_path = str(self.live_dir / "feed_state.json")
        stream_path = str(self.live_dir / "stream_state.json")
        feed = self.dep("read_feed_connections")(feed_path, now_ms)
        stream = self.dep("read_stream_state")(stream_path, now_ms)
        for edge_id, venue in (("spot_wss", "spot"), ("usdm_wss", "perp")):
            row = feed.get(venue) if isinstance(feed, dict) else None
            evidence = dict(row) if isinstance(row, dict) else None
            if evidence is not None:
                evidence.update(written_ms=feed.get("written_ms"),
                                snapshot_age_ms=feed.get("snapshot_age_ms"))
            self.derive(edge_id, evidence, now_ms, collected=True)
        machine = dict(stream) if isinstance(stream, dict) else None
        if machine is not None:
            value = feed.get("snapshot_age_ms") if isinstance(feed, dict) else None
            machine["feed_snapshot_age_ms"] = value
        self.derive("feed_machine", machine, now_ms, collected=True)

    def collect_cf(self, edge_ids, now_ms, forced):
        edge_ids, shared, connector = set(edge_ids), None, None
        if "cf_connector" in edge_ids:
            url = self.metrics_found
            if url is None:
                url = self.dep("discover_metrics_url")(self.metrics_url)
                self.metrics_found = url
            scraped = self.dep("scrape_metrics")(url) if url else None
            if isinstance(scraped, dict) and scraped.get("ok"):
                raw = scraped.get("raw_values")
                raw_values = raw if isinstance(raw, dict) else {}
                converted = self.dep("cf_metrics_from_raw")(raw_values, self.previous_cf_raw)
                converted = converted if isinstance(converted, dict) else {}
                shared = converted.get("connector_origin_delta", converted)
                if isinstance(raw, dict):
                    self.previous_cf_raw = deepcopy(raw)
                connector_values = converted.get("cf_connector", converted)
                connector = dict(connector_values) if isinstance(connector_values, dict) else {}
                connector["metrics_reachable"] = True
                parsed = urlparse(url)
                base_url = "%s://%s" % (parsed.scheme, parsed.netloc)
                ready = self.dep("read_ready")(base_url)
                if isinstance(ready, dict):
                    connector.update(ready_status=ready.get("status"),
                                     ready_connections=ready.get("ready_connections"))
                connector["metrics_url_port"] = parsed.port
            elif url:
                connector, self.metrics_found = {"metrics_reachable": False}, None
            if "cf_connector" in forced:
                connector = connector or {}
                connector["probe_now"] = True
            self.derive("cf_connector", connector, now_ms, collected=True)
        if "connector_origin" in edge_ids:
            origin = self.dep("probe_origin")(self.origin_url)
            origin = dict(origin) if isinstance(origin, dict) else None
            if origin is not None and isinstance(shared, dict):
                for key in ("request_errors_delta", "response_5xx_delta"):
                    origin[key] = shared.get(key)
            if "connector_origin" in forced:
                origin = origin or {}
                origin["probe_now"] = True
            self.derive("connector_origin", origin, now_ms, collected=True)

    def collect_news(self, now_ms):
        sources = str(self.live_dir / "news_sources.json")
        items = str(self.live_dir / "news.jsonl")
        evidence = self.dep("read_news")(sources, items, now_ms)
        self.derive("news_fetch", evidence, now_ms, collected=True)

    def refresh_other_edges(self, now_ms):
        job_path = str(self.live_dir / "jobs" / "results.jsonl")
        job = self.dep("read_last_job")(job_path)
        self.derive("binance_exec", job, now_ms)

    def cycle(self, now_ms=None):
        if not self.started:
            self.start(now_ms)
        now_ms = self.now_ms() if now_ms is None else int(now_ms)
        began = float(self.dep("monotonic_ms")())
        self.reload_config(now_ms)
        forced = self.consume_probe(now_ms)
        for group, edge_ids in GROUP_EDGES.items():
            if self.config["paused"][group]:
                for edge_id in edge_ids:
                    if self.edges[edge_id].get("monitoring") != "Paused":
                        self.derive(edge_id, None, now_ms)
                continue
            due = now_ms >= self.next_due[group]
            selected = set(edge_ids) if due else set(edge_ids).intersection(forced)
            if not selected:
                continue
            if group == "rest":
                chosen = [edge for edge in edge_ids if edge in selected]
                self.collect_rest(chosen, now_ms, forced)
            elif group == "wss":
                self.collect_wss(now_ms)
            elif group == "cf":
                self.collect_cf(selected, now_ms, forced)
            else:
                self.collect_news(now_ms)
            if due:
                self.next_due[group] = now_ms + self.config[INTERVAL_KEYS[group]] * 1000
        self.refresh_other_edges(now_ms)
        self.seed_previous.clear()
        if self.last_prune is None or now_ms - self.last_prune >= PRUNE_INTERVAL_MS:
            try:
                self.store.prune(now_ms)
            except Exception:
                pass
            self.last_prune = now_ms
        elapsed = float(self.dep("monotonic_ms")()) - began
        self.last_cycle_ms = max(0, int(round(elapsed)))
        self.last_cycle_at = _iso(now_ms)
        self.write_current(now_ms)
        if not self.quiet:
            print("network recorder cycle %s" % self.last_cycle_at)

    def payload(self, now_ms):
        edges = [deepcopy(self.edges[row["id"]]) for row in self.edge_defs]
        tick_s = int(self.tick_s) if self.tick_s.is_integer() else self.tick_s
        return {
            "schema_version": 1, "generated_at": _iso(now_ms), "generated_ms": int(now_ms),
            "collector": {
                "status": self.status, "started_at": self.started_at,
                "last_cycle_at": self.last_cycle_at,
                "cycle_ms": self.last_cycle_ms, "tick_s": tick_s,
                "recorder_release": self.release,
                "config": deepcopy(self.config),
                "probe_now": deepcopy(self.probe_now),
            },
            "deployed_ui_release": self.release,
            "summary": self.dep("summarize")(edges, now_ms),
            "nodes": deepcopy(self.nodes), "edges": edges,
            "events": self.store.latest_events(20),
        }

    def write_current(self, now_ms):
        temporary = Path(str(self.current_path) + ".tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as output:
                json.dump(self.payload(now_ms), output, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False)
                output.write("\n")
                output.flush()
            os.replace(str(temporary), str(self.current_path))
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def wait_seconds(self, now_ms):
        waits = [self.tick_s]
        waits.extend((due - now_ms) / 1000.0
                     for group, due in self.next_due.items()
                     if not self.config["paused"][group] and due > now_ms)
        return max(0.0, min(waits))

    def finish_shutdown(self, now_ms=None):
        if self.closed:
            return
        now_ms = self.now_ms() if now_ms is None else int(now_ms)
        try:
            if self.started and self.store is not None:
                self.status, self.last_cycle_at, self.last_cycle_ms = (
                    "stopped", _iso(now_ms), 0)
                self.event(now_ms, None, "collector stopped", "info", "collector stopped", {})
                self.write_current(now_ms)
        finally:
            self.close()

    def close(self):
        if not self.closed and self.store is not None:
            self.store.close()
        self.closed = True


def shutdown(recorder, now_ms=None):
    """Write the stopped receipt and snapshot, then close the store."""
    recorder.stop_requested = True
    recorder.finish_shutdown(now_ms)


def _wait(recorder, seconds):
    deadline = recorder.dep("monotonic_ms")() + seconds * 1000.0
    while not recorder.stop_requested:
        remaining = (deadline - recorder.dep("monotonic_ms")()) / 1000.0
        if remaining <= 0:
            return
        recorder.dep("sleep")(min(1.0, remaining))


def main(argv=None, deps=None):
    args = build_parser().parse_args(argv)
    if not _allowed_origin_url(args.origin_url):
        print("origin URL must use http://127.0.0.1 or http://localhost",
              file=sys.stderr)
        return 2
    recorder = Recorder(args.live_dir, args.data_dir, args.tick,
                        args.metrics_url, args.origin_url, args.quiet, deps)
    previous = {}

    def request_stop(_signum, _frame):
        recorder.stop_requested = True

    try:
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, request_stop)
        except ValueError:
            previous = {}
        recorder.start()
        while not recorder.stop_requested:
            now_ms = recorder.now_ms()
            recorder.cycle(now_ms)
            if args.once:
                break
            _wait(recorder, recorder.wait_seconds(now_ms))
        shutdown(recorder) if recorder.stop_requested else recorder.close()
        return 0
    except KeyboardInterrupt:
        shutdown(recorder)
        return 0
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        recorder.close()


if __name__ == "__main__":
    raise SystemExit(main())
