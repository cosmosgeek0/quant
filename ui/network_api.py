from __future__ import annotations

"""Bounded Network V1 reads and narrow monitoring-control writes."""

import json
import math
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_CONFIG = {
    "rest_probe_s": 15, "wss_read_s": 15, "cf_monitor_s": 15,
    "news_monitor_s": 120,
    "paused": {"rest": False, "wss": False, "cf": False, "news": False},
    "updated_at": None, "receipt_id": None,
}
CONFIG_ALLOWED = {
    "rest_probe_s": ("rest", (15, 30, 60)),
    "wss_read_s": ("wss", (5, 10, 15)),
    "cf_monitor_s": ("cf", (15, 30, 60)),
    "news_monitor_s": ("news", (120, 300, 600)),
}
GROUPS = ("rest", "wss", "cf", "news")
PROBE_EDGES = ("spot_rest", "usdm_rest", "connector_origin", "cf_connector")
RANGES = {"1h": (3_600_000, 60_000), "6h": (21_600_000, 300_000),
          "24h": (86_400_000, 900_000)}
_EDGE_KEYS = frozenset((
    "id", "source", "target", "label", "direction", "mode", "required",
    "group", "health", "health_reason", "authority", "monitoring", "truth",
    "observed_at", "observed_ms", "fresh_until_ms", "cadence_ms",
    "sample_count", "metrics", "counters", "evidence", "controls",
))
_EVENT_EVIDENCE_KEYS = frozenset((
    "key", "group", "old", "new", "receipt_id", "reread", "http_status",
    "rtt_ms", "edge_id", "request_id", "delta", "from", "to", "note",
))
_COLLECTOR_KEYS = frozenset((
    "status", "started_at", "last_cycle_at", "cycle_ms", "tick_s",
    "recorder_release", "config", "probe_now",
))
_SUMMARY_KEYS = frozenset((
    "operational_required", "observed_required", "required_total",
    "unknown_count", "worst", "oldest_required_evidence_at",
    "last_transition",
))
_TRUTH_KEYS = frozenset(("configured", "transport", "application"))
_COUNTER_KEYS = frozenset((
    "count_429", "count_418", "consecutive_failures",
    "probes", "failures", "quorum",
))
_CONTROL_NAMES = frozenset(("pause_monitor", "monitor_every", "probe_now"))
_EVENT_RESPONSE_MAX_BYTES = 200_000
_DROP = object()

_EDGE_ROWS = (
    ("browser_site", "browser", "quant_ui", "Browser site", "->", "Session", False, "session", "Public", ()),
    ("cf_connector", "cloudflare", "cloudflared", "Cloudflare connector", "->", "Continuous", True, "cf", "Read-only", ("pause_monitor", "monitor_every", "probe_now")),
    ("connector_origin", "cloudflared", "quant_ui", "Connector origin", "->", "Periodic", True, "cf", "Read-only", ("pause_monitor", "monitor_every", "probe_now")),
    ("spot_rest", "box", "binance_spot", "Spot REST", "->", "Periodic", True, "rest", "Public", ("pause_monitor", "monitor_every", "probe_now")),
    ("spot_wss", "box", "binance_spot", "Spot WSS", "<->", "Continuous", True, "wss", "Public", ("pause_monitor", "monitor_every")),
    ("usdm_rest", "box", "binance_usdm", "USD-M REST", "->", "Periodic", True, "rest", "Public", ("pause_monitor", "monitor_every", "probe_now")),
    ("usdm_wss", "box", "binance_usdm", "USD-M WSS", "<->", "Continuous", True, "wss", "Public", ("pause_monitor", "monitor_every")),
    ("feed_machine", "feed", "machine", "Feed to Machine", "->", "Continuous", True, "wss", "Read-only", ("pause_monitor", "monitor_every")),
    ("news_fetch", "news", "box", "News fetch", "->", "Periodic", True, "news", "Public", ("pause_monitor", "monitor_every")),
    ("binance_account", "box", "binance_account", "Binance account reads", "->", "On-demand", False, "ondemand", "Signed-read", ()),
    ("binance_exec", "box", "binance_exec", "Binance execution", "->", "On-demand", False, "ondemand", "Trade-capable", ()),
    ("aws_secrets", "box", "aws_secrets", "AWS Secrets Manager", "->", "On-demand", False, "ondemand", "Owner-gated", ()),
    ("aws_ssm", "box", "aws_ssm", "AWS SSM", "->", "On-demand", False, "ondemand", "Owner-gated", ()),
)
_NODE_ROWS = (
    ("browser", "Browser", "client"), ("cloudflare", "Cloudflare edge", "edge"), ("cloudflared", "Tunnel connector", "connector"),
    ("quant_ui", "Quant UI origin", "service"), ("box", "Execution box", "host"), ("binance_spot", "Binance Spot", "venue"),
    ("binance_usdm", "Binance USD-M", "venue"), ("feed", "Market feed", "service"), ("machine", "Machine", "runtime"), ("news", "News sources", "source"),
    ("binance_account", "Binance signed reads", "dependency"), ("binance_exec", "Binance execution", "dependency"), ("aws_secrets", "AWS Secrets Manager", "dependency"), ("aws_ssm", "AWS SSM", "dependency"),
)
_REST_METRICS = ("rtt_ms", "http_status", "clock_offset_ms", "used_weight_1m", "weight_limit_1m", "retry_after_s", "consecutive_failures")
_REST_COUNTERS = ("count_429", "count_418", "probes", "failures")
_WSS_METRICS = ("expected_connections", "connected_count", "successful_handshakes", "handshake_failures", "reconnects", "stream_count", "last_message_age_ms", "snapshot_age_ms", "dropped_frames_total", "dropped_frames_delta", "feed_status")
_METRICS = {
    "browser_site": ("fetch_ms",), "spot_rest": _REST_METRICS, "usdm_rest": _REST_METRICS,
    "spot_wss": _WSS_METRICS, "usdm_wss": _WSS_METRICS,
    "feed_machine": ("feed_snapshot_age_ms", "stream_row_age_ms", "symbols_total", "book_age_p50_ms", "book_age_p95_ms", "book_age_max_ms", "symbols_book_le_5s", "freshness_p50_ms", "source"),
    "cf_connector": ("ha_connections", "ready_connections", "ready_status", "total_requests_delta", "concurrent_requests", "latest_rtt_ms_max", "smoothed_rtt_ms_max", "min_rtt_ms_min", "lost_packets_delta", "connect_streams_errors_delta", "register_success_total", "metrics_url_port"),
    "connector_origin": ("rtt_ms", "http_status", "size_bytes", "request_errors_delta", "response_5xx_delta", "consecutive_failures"),
    "news_fetch": ("sources_available", "sources_live", "sources_pending", "sources_error", "as_of_age_s", "last_fetch_age_s"),
    "binance_exec": ("last_used_at", "last_op", "last_outcome"),
}
_METRIC_NAMES = frozenset(
    name for names in _METRICS.values() for name in names)

_CONFIG_COOLDOWNS = {}
_CONFIG_WRITE_LOCK = threading.Lock()
_PROBE_WRITE_LOCK = threading.Lock()
_LAST_RECEIPT_ID = 0


def _now_ms():
    return int(time.time() * 1000)


def _as_now_ms(value):
    if value is None: return _now_ms()
    value = value() if callable(value) else value
    if isinstance(value, datetime): return int(value.timestamp() * 1000)
    return int(value)


def _iso(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _read_dict(path):
    if path is None: return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _read_current(path):
    if path is None:
        return None, "missing"
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeError, TypeError):
        return None, "invalid"
    try:
        value = json.loads(text)
    except ValueError:
        return None, "invalid"
    return (value, "valid") if isinstance(value, dict) else (None, "invalid")


def _read_release(path):
    if path is None: return None
    try:
        line = Path(path).read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, UnicodeError, IndexError, TypeError):
        return None
    return line or None


def _fallback_edges():
    keys = ("id", "source", "target", "label", "direction", "mode", "required", "group", "authority", "controls")
    return [dict(zip(keys, row[:-1] + (list(row[-1]),))) for row in _EDGE_ROWS]


def _fallback_nodes():
    return [{"id": row[0], "label": row[1], "kind": row[2]} for row in _NODE_ROWS]


def _static_graph():
    try:
        from ui.network_health import EDGES, NODES
    except ModuleNotFoundError as error:
        if error.name != "ui.network_health": raise
        return _fallback_edges(), _fallback_nodes()
    return [dict(edge) for edge in EDGES], [dict(node) for node in NODES]


def _unknown_current(release, note=None):
    static_edges, nodes = _static_graph()
    edges = []
    for spec in static_edges:
        edge_id = spec["id"]
        edge = dict(spec)
        edge.update({
            "health": "Unknown", "health_reason": "Recorder has not written yet",
            "monitoring": "Running",
            "truth": {"configured": "Unknown", "transport": "Unknown", "application": "Unknown"},
            "observed_at": None, "observed_ms": None, "fresh_until_ms": None,
            "cadence_ms": None, "sample_count": 0,
            "metrics": {name: None for name in _METRICS.get(edge_id, ())},
            "counters": {name: None for name in _REST_COUNTERS}
            if edge_id in ("spot_rest", "usdm_rest") else {},
            "evidence": {"source": None, "source_age_s": None},
        })
        edges.append(edge)
    current = {
        "schema_version": 1, "generated_at": None, "generated_ms": None,
        "collector": {"status": "unknown", "started_at": None, "last_cycle_at": None,
                      "cycle_ms": None, "tick_s": 5, "recorder_release": None,
                      "config": json.loads(json.dumps(DEFAULT_CONFIG)), "probe_now": {}},
        "deployed_ui_release": release,
        "summary": {"operational_required": 0, "observed_required": 0,
                    "required_total": 8, "unknown_count": len(edges),
                    "worst": {"edge_id": "browser_site", "health": "Unknown"},
                    "oldest_required_evidence_at": None, "last_transition": None},
        "nodes": nodes, "edges": edges, "events": [],
    }
    if note is not None:
        current["note"] = note
    return current


def _bounded_scalar(value, text_limit=120):
    if isinstance(value, str):
        return value[:text_limit]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return _DROP


def _project_scalar_map(value, allowed, text_limit=120):
    if not isinstance(value, dict):
        return {}
    projected = {}
    for key, item in value.items():
        if key not in allowed:
            continue
        scalar = _bounded_scalar(item, text_limit)
        if scalar is not _DROP:
            projected[key] = scalar
    return projected


def _project_config(value):
    if not isinstance(value, dict):
        return {}
    projected = {}
    for key, item in value.items():
        if key not in DEFAULT_CONFIG:
            continue
        if key == "paused":
            projected[key] = _project_scalar_map(item, frozenset(GROUPS))
            continue
        scalar = _bounded_scalar(item)
        if scalar is not _DROP:
            projected[key] = scalar
    return projected


def _project_collector(value):
    if not isinstance(value, dict):
        return {}
    projected = {}
    for key, item in value.items():
        if key not in _COLLECTOR_KEYS:
            continue
        if key == "config":
            projected[key] = _project_config(item)
        elif key == "probe_now":
            projected[key] = _project_scalar_map(
                item, frozenset(PROBE_EDGES))
        else:
            scalar = _bounded_scalar(item)
            if scalar is not _DROP:
                projected[key] = scalar
    return projected


def _project_summary(value):
    if not isinstance(value, dict):
        return {}
    projected = {}
    for key, item in value.items():
        if key not in _SUMMARY_KEYS:
            continue
        if key == "worst":
            projected[key] = _project_scalar_map(
                item, frozenset(("edge_id", "health")))
        elif key == "last_transition":
            projected[key] = (
                None if item is None else _project_scalar_map(
                    item, frozenset(("ts", "edge_id", "transition"))))
        else:
            scalar = _bounded_scalar(item)
            if scalar is not _DROP:
                projected[key] = scalar
    return projected


def _project_edge_evidence(value):
    if not isinstance(value, dict):
        return {}
    projected = {}
    source = value.get("source")
    if isinstance(source, str):
        projected["source"] = source[:120]
    age = value.get("source_age_s")
    if age is None:
        projected["source_age_s"] = None
    elif (isinstance(age, (int, float)) and not isinstance(age, bool)
          and (not isinstance(age, float) or math.isfinite(age))):
        projected["source_age_s"] = age
    return projected


def _project_edge(edge):
    projected = {}
    for key, item in edge.items():
        if key not in _EDGE_KEYS:
            continue
        if key == "metrics":
            projected[key] = _project_scalar_map(item, _METRIC_NAMES)
        elif key == "counters":
            projected[key] = _project_scalar_map(item, _COUNTER_KEYS)
        elif key == "truth":
            projected[key] = _project_scalar_map(item, _TRUTH_KEYS)
        elif key == "evidence":
            projected[key] = _project_edge_evidence(item)
        elif key == "controls":
            projected[key] = [
                value for value in item
                if isinstance(value, str) and value in _CONTROL_NAMES
            ] if isinstance(item, list) else []
        else:
            scalar = _bounded_scalar(item, 500 if key == "health_reason" else 120)
            if scalar is not _DROP:
                projected[key] = scalar
    return projected


def _project_nodes(nodes):
    projected = []
    for node in nodes:
        if not isinstance(node, dict):
            return None
        projected.append(_project_scalar_map(
            node, frozenset(("id", "label", "kind"))))
    return projected


def _sanitize_current(current):
    if not isinstance(current, dict) or not isinstance(current.get("edges"), list):
        return None
    known_ids = {edge["id"] for edge in _static_graph()[0]}
    edges = []
    for edge in current["edges"]:
        if not isinstance(edge, dict):
            return None
        edge_id = edge.get("id")
        if not isinstance(edge_id, str) or edge_id not in known_ids:
            return None
        edges.append(_project_edge(edge))
    for key in ("collector", "summary"):
        if key in current and not isinstance(current[key], dict):
            return None
    for key in ("nodes", "events"):
        if key in current and not isinstance(current[key], list):
            return None
    output = {}
    for key in ("schema_version", "generated_at", "generated_ms",
                "deployed_ui_release"):
        if key in current:
            scalar = _bounded_scalar(current[key])
            if scalar is not _DROP:
                output[key] = scalar
    if "collector" in current:
        output["collector"] = _project_collector(current["collector"])
    if "summary" in current:
        output["summary"] = _project_summary(current["summary"])
    if "nodes" in current:
        nodes = _project_nodes(current["nodes"])
        if nodes is None:
            return None
        output["nodes"] = nodes
    if "events" in current:
        output["events"] = current["events"]
    output["edges"] = edges
    return output


def _project_evidence(value):
    if not isinstance(value, dict):
        return {}
    evidence = {}
    for key, item in value.items():
        if key not in _EVENT_EVIDENCE_KEYS:
            continue
        scalar = _bounded_scalar(item, 200)
        if scalar is not _DROP:
            evidence[key] = scalar
    return evidence


def _project_event(event):
    if not isinstance(event, dict):
        return None
    event_id = event.get("event_id")
    event_id = event_id if isinstance(event_id, int) and not isinstance(
        event_id, bool) else None
    ts = _bounded_scalar(event.get("ts"), 200)
    edge_id = event.get("edge_id")
    transition = event.get("transition")
    severity = event.get("severity")
    reason = event.get("reason")
    return {
        "event_id": event_id,
        "ts": None if ts is _DROP else ts,
        "edge_id": edge_id[:200] if isinstance(edge_id, str) else None,
        "transition": transition[:200] if isinstance(transition, str) else "",
        "severity": severity[:200] if isinstance(severity, str) else "",
        "reason": reason[:500] if isinstance(reason, str) else "",
        "evidence": _project_evidence(event.get("evidence")),
    }


def _event_cursor(event, fallback):
    event_id = event.get("event_id") if isinstance(event, dict) else None
    return event_id if isinstance(event_id, int) and not isinstance(
        event_id, bool) else fallback


def _project_events(events, limit, after=0):
    projected = []
    next_after = after
    for event in events if isinstance(events, list) else ():
        row = _project_event(event)
        if row is None:
            continue
        candidate = projected + [row]
        candidate_after = _event_cursor(row, next_after)
        encoded = json.dumps(
            {"events": candidate, "next_after": candidate_after},
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > _EVENT_RESPONSE_MAX_BYTES:
            break
        projected.append(row)
        next_after = candidate_after
        if len(projected) >= limit:
            break
    return projected


def api_network(sources, now=None):
    """Return current recorder state, with read-time freshness applied."""
    release = _read_release(sources.get("ui_release"))
    current, state = _read_current(sources.get("network_current"))
    if state == "missing":
        return _unknown_current(release)
    current = _sanitize_current(current)
    if current is None:
        return _unknown_current(release, "current file invalid")
    try:
        from ui.network_health import apply_read_time_staleness
    except ModuleNotFoundError as error:
        if error.name != "ui.network_health": raise
    else:
        try:
            applied = apply_read_time_staleness(current, _as_now_ms(now))
        except (AttributeError, OverflowError, TypeError, ValueError):
            return _unknown_current(release, "current file invalid")
        current = applied if isinstance(applied, dict) else current
    current["deployed_ui_release"] = release
    current["events"] = _project_events(current.get("events"), 20)
    return current


def _query_value(query, key, default=None):
    value = query.get(key, default)
    if isinstance(value, (list, tuple)):
        if len(value) != 1: raise ValueError(key)
        value = value[0]
    return value


def _make_store(path):
    from ui.network_store import NetworkStore
    return NetworkStore(str(path))


def _store_read(path, method, *args):
    store = _make_store(path)
    store.open()
    try:
        return getattr(store, method)(*args)
    finally:
        store.close()


def api_network_history(sources, query):
    try:
        edge_id = _query_value(query, "edge_id")
        metric = _query_value(query, "metric")
        range_key = _query_value(query, "range")
    except (AttributeError, ValueError):
        return 400, {"error": "query values must be singular"}
    edge_ids = {edge["id"] for edge in _static_graph()[0]}
    if edge_id not in edge_ids: return 400, {"error": "unknown edge_id"}
    allowed_metrics = _METRICS.get(edge_id, ())
    if edge_id in ("spot_rest", "usdm_rest"):
        allowed_metrics += _REST_COUNTERS
    if metric != "status" and metric not in allowed_metrics: return 400, {"error": "unknown metric"}
    if range_key not in RANGES: return 400, {"error": "unknown range"}
    now_ms = _now_ms()
    duration_ms, bucket_ms = RANGES[range_key]
    path = Path(sources["network_db"])
    if not path.is_file():
        return 200, {"edge_id": edge_id, "metric": metric, "range": range_key,
                     "bucket_s": bucket_ms // 1000, "from_ms": now_ms - duration_ms,
                     "to_ms": now_ms, "buckets": [], "note": "history not yet recorded"}
    payload = _store_read(path, "history", edge_id, metric, range_key, now_ms)
    buckets = payload.get("buckets") if isinstance(payload, dict) else None
    if not isinstance(buckets, list):
        buckets = []
    payload = dict(payload) if isinstance(payload, dict) else {}
    payload.update({"edge_id": edge_id, "metric": metric, "range": range_key})
    payload["buckets"] = buckets[:720]
    return 200, payload


def _parse_int(value, default):
    if value is None: return default
    if isinstance(value, bool) or isinstance(value, float): raise ValueError
    text = str(value).strip()
    if not text or not text.lstrip("-").isdigit(): raise ValueError
    return int(text)


def api_network_events(sources, query):
    try:
        after = _parse_int(_query_value(query, "after", None), 0)
        limit = _parse_int(_query_value(query, "limit", None), 50)
    except (AttributeError, TypeError, ValueError):
        return 400, {"error": "after and limit must be integers"}
    if after < 0: return 400, {"error": "after must be at least 0"}
    if not 1 <= limit <= 200: return 400, {"error": "limit must be from 1 to 200"}
    path = Path(sources["network_db"])
    if not path.is_file():
        return 200, {"events": [], "next_after": after}
    payload = _store_read(path, "events_after", after, limit)
    events = payload.get("events") if isinstance(payload, dict) else None
    events = _project_events(events, limit, after)
    next_after = _event_cursor(events[-1], after) if events else after
    return 200, {"events": events, "next_after": next_after}


def _header(headers, name):
    if headers is None: return None
    value = headers.get(name)
    if value is not None: return value
    return next((item for key, item in headers.items()
                 if str(key).lower() == name.lower()), None)


def _origin_error(headers):
    site = _header(headers, "Sec-Fetch-Site")
    if site is not None and str(site).strip().lower() == "cross-site": return "cross-origin request refused"
    origin = _header(headers, "Origin")
    if origin is None: return None
    host = _header(headers, "Host")
    try:
        origin_host = urlsplit(str(origin)).netloc.lower()
    except ValueError:
        origin_host = ""
    if not origin_host or host is None or origin_host != str(host).strip().lower(): return "cross-origin request refused"
    return None


def _resolved_config(raw):
    output = json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(raw, dict): return output
    for key, (_group, allowed) in CONFIG_ALLOWED.items():
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value in allowed:
            output[key] = value
    paused = raw.get("paused")
    if isinstance(paused, dict):
        for group in GROUPS:
            if isinstance(paused.get(group), bool):
                output["paused"][group] = paused[group]
    updated_at, receipt_id = raw.get("updated_at"), raw.get("receipt_id")
    if isinstance(updated_at, str):
        output["updated_at"] = updated_at
    if isinstance(receipt_id, int) and not isinstance(receipt_id, bool):
        output["receipt_id"] = receipt_id
    return output


def _atomic_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".%s-" % target.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_bytes(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".%s-" % target.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _restore_previous(path, payload, existed):
    if existed:
        _atomic_bytes(path, payload)
        return
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def _receipt_id(now_ms):
    global _LAST_RECEIPT_ID
    _LAST_RECEIPT_ID = max(_LAST_RECEIPT_ID + 1, now_ms)
    return _LAST_RECEIPT_ID


def _config_request(body):
    if not isinstance(body, dict): return None, "body must be a JSON object"
    key = body.get("key")
    if key in CONFIG_ALLOWED and set(body) == {"key", "value"}:
        group, allowed = CONFIG_ALLOWED[key]
        value = body["value"]
        if not isinstance(value, int) or isinstance(value, bool) or value not in allowed: return None, "%s must be one of %s" % (key, list(allowed))
        return (key, group, value), None
    if key == "paused" and set(body) == {"key", "group", "value"}:
        group, value = body["group"], body["value"]
        if group not in GROUPS: return None, "unknown monitor group"
        if not isinstance(value, bool): return None, "paused value must be true or false"
        return (key, group, value), None
    return None, "unknown network config request"


def write_network_config(sources, body, headers=None):
    refused = _origin_error(headers)
    if refused: return None, ("403", refused)
    request, error = _config_request(body)
    if error: return None, error
    key, group, new = request
    with _CONFIG_WRITE_LOCK:
        now_ms = _now_ms()
        config_path = Path(sources["network_config"])
        try:
            previous_bytes, previous_exists = config_path.read_bytes(), True
        except FileNotFoundError:
            previous_bytes, previous_exists = b"", False
        current = _resolved_config(_read_dict(config_path))
        old = current["paused"][group] if key == "paused" else current[key]
        if old == new:
            return {
                "key": key, "group": group, "old": old, "new": new,
                "receipt_id": None, "reread": True, "unchanged": True,
            }, None
        last_ms = _CONFIG_COOLDOWNS.get((key, group))
        if last_ms is not None and now_ms - last_ms < 2_000:
            remaining = max(1, (2_000 - (now_ms - last_ms) + 999) // 1000)
            return None, "cooldown %d s" % remaining
        receipt_id = _receipt_id(now_ms)
        if key == "paused":
            current["paused"][group] = new
        else:
            current[key] = new
        current["updated_at"] = _iso(now_ms)
        current["receipt_id"] = receipt_id
        store = _make_store(Path(sources["network_db"]))
        store.open()
        try:
            _atomic_json(sources["network_config"], current)
            reread_config = _read_dict(sources["network_config"])
            reread = (reread_config.get("paused", {}).get(group) == new
                      if key == "paused" and isinstance(reread_config, dict)
                      else isinstance(reread_config, dict) and reread_config.get(key) == new)
            if not reread:
                _restore_previous(config_path, previous_bytes, previous_exists)
                return None, "saved value could not be confirmed"
            evidence = {"key": key, "group": group, "old": old, "new": new,
                        "receipt_id": receipt_id, "reread": True}
            label = "%s.%s" % (key, group) if key == "paused" else key
            reason = "%s %s → %s" % (label, json.dumps(old), json.dumps(new))
            try:
                store.insert_event(
                    now_ms, None, "config", "info", reason, evidence)
            except Exception:
                _restore_previous(config_path, previous_bytes, previous_exists)
                return None, "receipt could not be recorded; change reverted"
        finally:
            store.close()
        _CONFIG_COOLDOWNS[(key, group)] = now_ms
        return evidence, None


def _parse_iso_ms(value):
    if not isinstance(value, str): return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (OverflowError, ValueError):
        return None


def write_network_probe(sources, body, headers=None):
    refused = _origin_error(headers)
    if refused: return None, ("403", refused)
    if not isinstance(body, dict) or set(body) != {"edge_id"} or body.get("edge_id") not in PROBE_EDGES: return None, "unknown probe edge"
    edge_id = body["edge_id"]
    with _PROBE_WRITE_LOCK:
        now_ms = _now_ms()
        request_path = Path(sources["network_probe_request"])
        pending = _read_dict(request_path)
        pending_ms = _parse_iso_ms(
            pending.get("requested_at")) if isinstance(pending, dict) else None
        if pending_ms is None:
            try:
                pending_ms = int(request_path.stat().st_mtime * 1000)
            except OSError:
                pending_ms = None
        if pending_ms is not None and now_ms - pending_ms < 30_000:
            return None, "probe already requested"
        # The recorder owns per-edge cooldown enforcement; this API check is a courtesy.
        current = _read_dict(sources.get("network_current")) or {}
        collector = current.get("collector") if isinstance(current.get("collector"), dict) else {}
        probe_now = collector.get("probe_now") if isinstance(collector.get("probe_now"), dict) else {}
        previous_ms = _parse_iso_ms(probe_now.get(edge_id))
        if previous_ms is not None and now_ms - previous_ms < 30_000:
            remaining = max(1, (30_000 - (now_ms - previous_ms) + 999) // 1000)
            return None, "cooldown %d s" % remaining
        request_id = secrets.token_hex(6)
        _atomic_json(request_path, {
            "edge_id": edge_id, "request_id": request_id,
            "requested_at": _iso(now_ms),
        })
        return {"edge_id": edge_id, "request_id": request_id}, None
