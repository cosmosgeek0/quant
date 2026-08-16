from __future__ import annotations

"""Pure Network V1 health derivation and read-time state helpers."""

import copy
from datetime import datetime, timezone
import math


NODE_ROWS = (
    ("browser", "Browser", "client"), ("cloudflare", "Cloudflare edge", "edge"), ("cloudflared", "Tunnel connector", "connector"),
    ("quant_ui", "Quant UI origin", "service"), ("box", "Execution box", "host"), ("binance_spot", "Binance Spot", "venue"),
    ("binance_usdm", "Binance USD-M", "venue"), ("feed", "Market feed", "service"), ("machine", "Machine", "runtime"),
    ("news", "News sources", "source"), ("binance_account", "Binance signed reads", "dependency"),
    ("binance_exec", "Binance execution", "dependency"), ("aws_secrets", "AWS Secrets Manager", "dependency"), ("aws_ssm", "AWS SSM", "dependency"),
)
NODES = [dict(zip(("id", "label", "kind"), row)) for row in NODE_ROWS]

FULL_CONTROLS = ["pause_monitor", "monitor_every", "probe_now"]
READ_CONTROLS = ["pause_monitor", "monitor_every"]
CONTROL_SETS = {"full": FULL_CONTROLS, "read": READ_CONTROLS, "none": []}
EDGE_FIELDS = ("id", "source", "target", "label", "direction", "mode",
               "required", "group", "authority")
EDGE_ROWS = (
    ("browser_site", "browser", "quant_ui", "Browser site", "->", "Session", False, "session", "Public", "none"),
    ("cf_connector", "cloudflare", "cloudflared", "Cloudflare connector", "->", "Continuous", True, "cf", "Read-only", "full"),
    ("connector_origin", "cloudflared", "quant_ui", "Connector origin", "->", "Periodic", True, "cf", "Read-only", "full"),
    ("spot_rest", "box", "binance_spot", "Spot REST", "->", "Periodic", True, "rest", "Public", "full"),
    ("spot_wss", "box", "binance_spot", "Spot WSS", "<->", "Continuous", True, "wss", "Public", "read"),
    ("usdm_rest", "box", "binance_usdm", "USD-M REST", "->", "Periodic", True, "rest", "Public", "full"),
    ("usdm_wss", "box", "binance_usdm", "USD-M WSS", "<->", "Continuous", True, "wss", "Public", "read"),
    ("feed_machine", "feed", "machine", "Feed to Machine", "->", "Continuous", True, "wss", "Read-only", "read"),
    ("news_fetch", "news", "box", "News fetch", "->", "Periodic", True, "news", "Public", "read"),
    ("binance_account", "box", "binance_account", "Binance account reads", "->", "On-demand", False, "ondemand", "Signed-read", "none"),
    ("binance_exec", "box", "binance_exec", "Binance execution", "->", "On-demand", False, "ondemand", "Trade-capable", "none"),
    ("aws_secrets", "box", "aws_secrets", "AWS Secrets Manager", "->", "On-demand", False, "ondemand", "Owner-gated", "none"),
    ("aws_ssm", "box", "aws_ssm", "AWS SSM", "->", "On-demand", False, "ondemand", "Owner-gated", "none"),
)
EDGES = [dict(zip(EDGE_FIELDS, row[:9]), controls=CONTROL_SETS[row[9]][:])
         for row in EDGE_ROWS]

EDGE_BY_ID = {edge["id"]: edge for edge in EDGES}
DEFAULT_INTERVAL_S = {"rest": 15, "wss": 15, "cf": 15, "news": 120}
INTERVAL_KEY = {"rest": "rest_probe_s", "wss": "wss_read_s", "cf": "cf_monitor_s", "news": "news_monitor_s"}
STATUS_RANK = {"Outage": 6, "Stale": 5, "Degraded": 4, "Unknown": 3, "Off": 2, "Operational": 1}
SEVERITY = {"Outage": "high", "Stale": "high", "Degraded": "medium", "Unknown": "medium", "Off": "info", "Operational": "info"}
REST_METRICS = "rtt_ms http_status clock_offset_ms used_weight_1m weight_limit_1m retry_after_s consecutive_failures".split()
WSS_METRICS = "expected_connections connected_count successful_handshakes handshake_failures reconnects stream_count last_message_age_ms snapshot_age_ms dropped_frames_total dropped_frames_delta feed_status".split()
METRIC_KEYS = {
    "browser_site": ("fetch_ms",), "spot_rest": REST_METRICS, "usdm_rest": REST_METRICS,
    "spot_wss": WSS_METRICS, "usdm_wss": WSS_METRICS,
    "feed_machine": "feed_snapshot_age_ms stream_row_age_ms symbols_total book_age_p50_ms book_age_p95_ms book_age_max_ms symbols_book_le_5s freshness_p50_ms source".split(),
    "cf_connector": "ha_connections ready_connections ready_status total_requests_delta concurrent_requests latest_rtt_ms_max smoothed_rtt_ms_max min_rtt_ms_min lost_packets_delta connect_streams_errors_delta register_success_total metrics_url_port".split(),
    "connector_origin": "rtt_ms http_status size_bytes request_errors_delta response_5xx_delta consecutive_failures".split(),
    "news_fetch": "sources_available sources_live sources_pending sources_error as_of_age_s last_fetch_age_s".split(),
    "binance_exec": "last_used_at last_op last_outcome".split(),
}
SOURCES = {
    "browser_site": "browser session", "cf_connector": "cloudflared loopback metrics", "connector_origin": "Quant UI loopback origin",
    "spot_rest": "Binance Spot public time endpoint", "usdm_rest": "Binance USD-M public time endpoint",
    "spot_wss": "mirrored feed_state.json", "usdm_wss": "mirrored feed_state.json", "feed_machine": "mirrored feed_state.json and stream_state.json",
    "news_fetch": "mirrored news source state", "binance_exec": "mirrored job result",
    "binance_account": "no mirrored signed-read source", "aws_secrets": "no mirrored secrets-read source", "aws_ssm": "no mirrored SSM source",
}


def _is_number(value):
    return ((isinstance(value, int) and not isinstance(value, bool)) or
            (isinstance(value, float) and math.isfinite(value)))


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _iso(ms):
    if not _is_number(ms):
        return None
    stamp = datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp_ms(value):
    if _is_int(value):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return int(stamp.timestamp() * 1000)


def _cadence_ms(group, config):
    key = INTERVAL_KEY.get(group)
    if key is None:
        return None
    value = config.get(key, DEFAULT_INTERVAL_S[group]) if isinstance(config, dict) else DEFAULT_INTERVAL_S[group]
    if not _is_number(value) or value <= 0:
        value = DEFAULT_INTERVAL_S[group]
    return int(value * 1000)


def _fresh_until_ms(edge_id, evidence, observed_ms, cadence_ms):
    if observed_ms is None or cadence_ms is None:
        return None
    cadence_deadline = observed_ms + 3 * cadence_ms
    if not isinstance(evidence, dict):
        return cadence_deadline
    limits = {
        "spot_wss": ("snapshot_age_ms", 60000, 1),
        "usdm_wss": ("snapshot_age_ms", 60000, 1),
        "feed_machine": ("stream_row_age_ms", 600000, 1),
        "news_fetch": ("as_of_age_s", 3 * cadence_ms, 1000),
    }
    rule = limits.get(edge_id)
    if rule is None:
        return cadence_deadline
    age = evidence.get(rule[0])
    if not _is_number(age):
        return cadence_deadline
    remaining_ms = max(0, rule[1] - age * rule[2])
    evidence_deadline = int(observed_ms + remaining_ms)
    return min(cadence_deadline, evidence_deadline)


def _previous_count(prev, key):
    counters = prev.get("counters", {}) if isinstance(prev, dict) else {}
    value = counters.get(key) if isinstance(counters, dict) else None
    return int(value) if _is_number(value) and value >= 0 else 0


def _source_age(edge_id, evidence, observed_ms, now_ms):
    if not isinstance(evidence, dict):
        return None
    direct = evidence.get("source_age_s")
    if _is_number(direct):
        return direct
    if edge_id == "news_fetch" and _is_number(evidence.get("as_of_age_s")):
        return evidence["as_of_age_s"]
    age_keys = ("snapshot_age_ms",) if edge_id in ("spot_wss", "usdm_wss") else (
        ("feed_snapshot_age_ms", "stream_row_age_ms") if edge_id == "feed_machine" else ())
    ages = [evidence.get(key) for key in age_keys if _is_number(evidence.get(key))]
    if ages:
        return max(ages) / 1000.0
    return max(0, now_ms - observed_ms) / 1000.0 if _is_number(observed_ms) else None


def _base(edge_id, evidence, prev, config, now_ms):
    if edge_id not in EDGE_BY_ID:
        raise ValueError("Unknown network edge: %s" % edge_id)
    static = EDGE_BY_ID[edge_id]
    cadence_ms = _cadence_ms(static["group"], config)
    observed_ms = evidence.get("observed_ms") if isinstance(evidence, dict) else None
    observed_ms = int(observed_ms) if _is_number(observed_ms) else (
        now_ms if isinstance(evidence, dict) else None)
    prior_samples = prev.get("sample_count") if isinstance(prev, dict) else 0
    sample_count = (int(prior_samples) if _is_number(prior_samples) and prior_samples >= 0 else 0) + 1
    paused = bool(isinstance(config, dict) and isinstance(config.get("paused"), dict)
                  and config["paused"].get(static["group"]) is True)
    safe_evidence = {"source": SOURCES.get(edge_id, "no stored evidence"), "source_age_s": _source_age(edge_id, evidence, observed_ms, now_ms)}
    if isinstance(evidence, dict) and evidence.get("probe_now") is True:
        safe_evidence["probe_now"] = True
    edge = dict(static)
    edge.update({
        "health": "Unknown", "health_reason": "Evidence is not available", "monitoring": "Paused" if paused else "Running",
        "truth": {"configured": "Unknown", "transport": "Unknown", "application": "Unknown"},
        "observed_at": _iso(observed_ms), "observed_ms": observed_ms,
        "fresh_until_ms": _fresh_until_ms(edge_id, evidence, observed_ms, cadence_ms),
        "cadence_ms": cadence_ms, "sample_count": sample_count,
        "metrics": {key: None for key in METRIC_KEYS.get(edge_id, ())},
        "counters": copy.deepcopy(prev.get("counters", {})) if isinstance(prev, dict) else {},
        "evidence": safe_evidence})
    return edge


def _copy_metrics(edge, evidence):
    if not isinstance(evidence, dict):
        return
    for key in edge["metrics"]:
        if key in evidence:
            value = evidence[key]
            edge["metrics"][key] = None if isinstance(value, float) and not math.isfinite(value) else value


def _truth(edge, configured, transport, application):
    edge["truth"].update(configured=configured, transport=transport, application=application)


def _apply_rules(edge, rules):
    for applies, state, reason in rules:
        if applies:
            edge["health"], edge["health_reason"] = state, reason
            return


def _paused(edge, config):
    if edge["monitoring"] != "Paused":
        return False
    receipt = config.get("receipt_id") if isinstance(config, dict) else None
    edge["health"] = "Off"
    edge["health_reason"] = "Monitor paused (receipt %s)" % (receipt if receipt is not None else "Unknown")
    return True


def _stale_sample(edge, now_ms):
    return (edge["cadence_ms"] is not None and edge["observed_ms"] is not None and now_ms - edge["observed_ms"] > 3 * edge["cadence_ms"])


def _derive_rest(edge, evidence, prev, now_ms):
    label = edge["label"]
    metrics, counters = edge["metrics"], edge["counters"]
    edge["truth"]["configured"] = "Pass"
    metrics["weight_limit_1m"] = 6000 if edge["id"] == "spot_rest" else 2400
    counter_names = ("count_429", "count_418", "consecutive_failures", "probes", "failures", "quorum")
    counters.update({key: _previous_count(prev, key) for key in counter_names})
    valid = isinstance(evidence, dict) and "ok" in evidence and "http_status" in evidence
    if not valid:
        counters["quorum"] = 0
        kind = "missing" if not isinstance(evidence, dict) else "malformed"
        edge["health_reason"] = "%s probe evidence is %s" % (label, kind)
        metrics["consecutive_failures"] = counters["consecutive_failures"]
        return
    _copy_metrics(edge, evidence)
    metrics["weight_limit_1m"] = 6000 if edge["id"] == "spot_rest" else 2400
    status = metrics["http_status"]
    server_time_ms = evidence.get("server_time_ms")
    valid_time = _is_int(server_time_ms) and server_time_ms > 0
    success = evidence.get("ok") is True and status == 200 and valid_time
    _truth(edge, "Pass", "Pass" if status is not None else "Fail", "Pass" if success else "Fail")
    counters["probes"] += 1
    counters["failures"] += int(not success)
    counters["consecutive_failures"] = 0 if success else counters["consecutive_failures"] + 1
    counters["quorum"] = min(2, counters["quorum"] + 1) if success else 0
    if status == 429:
        counters["count_429"] += 1
        counters["last_429_ms"] = edge["observed_ms"]
    if status == 418:
        counters["count_418"] += 1
        retry = metrics["retry_after_s"]
        if _is_number(retry) and retry > 0:
            counters["retry_until_ms"] = edge["observed_ms"] + int(retry * 1000)
    metrics["consecutive_failures"] = counters["consecutive_failures"]
    retry_until, failures = counters.get("retry_until_ms"), counters["consecutive_failures"]
    last_429 = counters.get("last_429_ms")
    recent_429 = success and _is_number(last_429) and now_ms - last_429 <= 300000
    age_429 = max(0, now_ms - last_429) if recent_429 else 0
    age_429_text = "less than 1 min" if age_429 < 60000 else "%d min" % (age_429 // 60000)
    used, limit = metrics["used_weight_1m"], metrics["weight_limit_1m"]
    high_weight = success and _is_number(used) and _is_number(limit) and limit > 0 and used * 100 >= limit * 80
    failed_fact = "did not include server time" if status == 200 else "probe failed once"
    _apply_rules(edge, (
        (_stale_sample(edge, now_ms), "Stale", "%s sample is older than its monitor interval" % label),
        (status == 418 or (_is_number(retry_until) and now_ms < retry_until), "Outage", "%s 418 is in force" % label),
        (failures >= 2, "Outage", "%s failed %d consecutive probes" % (label, failures)),
        (status == 429, "Degraded", "%s 429 seen now" % label),
        (not success, "Degraded", "%s %s" % (label, failed_fact)),
        (recent_429, "Degraded", "%s 429 seen %s ago" % (label, age_429_text)),
        (high_weight, "Degraded", "%s weight is %s of %s" % (label, used, limit)),
        (counters["quorum"] < 2, "Degraded", "%s is recovering %d/2" % (label, counters["quorum"])),
        (True, "Operational", "%s probe returned HTTP 200" % label),
    ))


def _derive_wss(edge, evidence, prev, _now_ms):
    label = edge["label"]
    if not isinstance(evidence, dict):
        edge["health_reason"] = "Feed evidence is not mirrored"
        return
    _copy_metrics(edge, evidence)
    total = edge["metrics"]["dropped_frames_total"]
    previous_total = prev.get("metrics", {}).get("dropped_frames_total") if isinstance(prev, dict) else None
    delta = total - previous_total if _is_number(total) and _is_number(previous_total) else None
    edge["metrics"]["dropped_frames_delta"] = delta if _is_number(delta) and delta >= 0 else None
    connected = edge["metrics"]["connected_count"]
    expected = edge["metrics"]["expected_connections"]
    last_age = edge["metrics"]["last_message_age_ms"]
    snapshot_age = edge["metrics"]["snapshot_age_ms"]
    status = edge["metrics"]["feed_status"]
    _truth(edge, "Pass", "Pass" if _is_number(connected) and connected > 0 else ("Fail" if connected == 0 else "Unknown"),
           "Pass" if _is_number(last_age) and last_age <= 60000 else ("Fail" if _is_number(last_age) else "Unknown"))
    counts_known = _is_number(connected) and _is_number(expected)
    _apply_rules(edge, (
        (not _is_number(snapshot_age), "Unknown", "%s snapshot age is not exposed" % label),
        (_is_number(snapshot_age) and snapshot_age > 60000, "Stale", "%s evidence is older than 60 seconds" % label),
        (status == "broken", "Outage", "%s has no usable connection" % label),
        (not counts_known, "Unknown", "%s connection count is not exposed" % label),
        (counts_known and connected == 0, "Outage", "%s has no usable connection" % label),
        (status not in ("healthy", "degraded"), "Unknown", "%s feed status is not exposed" % label),
        (status == "degraded", "Degraded", "%s reports degraded feed status" % label),
        (not _is_number(last_age), "Unknown", "%s last-message age is not exposed" % label),
        (_is_number(last_age) and last_age > 60000, "Degraded", "%s has been silent for more than 60 seconds" % label),
        (counts_known and connected < expected, "Degraded", "%s has %s of %s expected connections" % (label, connected, expected)),
        (True, "Operational", "%s feed is connected and current" % label),
    ))


def _derive_feed_machine(edge, evidence, _prev, _now_ms):
    if not isinstance(evidence, dict):
        edge["health_reason"] = "Feed or machine evidence is not mirrored"
        return
    _copy_metrics(edge, evidence)
    edge["metrics"]["source"] = None
    feed_age = edge["metrics"]["feed_snapshot_age_ms"]
    row_age = edge["metrics"]["stream_row_age_ms"]
    symbols_total = edge["metrics"]["symbols_total"]
    book_age = edge["metrics"]["book_age_p50_ms"]
    _truth(edge, "Pass", "Not-applicable", "Pass" if _is_number(book_age) and book_age <= 5000 else ("Fail" if _is_number(book_age) else "Unknown"))
    stream_known = _is_number(row_age) and _is_number(book_age)
    feed_fresh = _is_number(feed_age) and feed_age <= 5000
    _apply_rules(edge, (
        (_is_number(symbols_total) and symbols_total == 0, "Unknown", "Machine stream has no rows"),
        (not _is_number(feed_age), "Unknown", "Feed evidence is not mirrored"),
        (not stream_known, "Unknown", "Machine stream evidence is not mirrored"),
        (stream_known and row_age > 600000, "Stale", "Machine stream row is older than 600 seconds"),
        (stream_known and book_age > 60000 and feed_fresh, "Outage", "Machine median book age is above 60 seconds while the feed is fresh"),
        (stream_known and book_age > 5000 and feed_fresh, "Degraded", "Machine median book age is above 5 seconds while the feed is fresh"),
        (stream_known and book_age <= 5000, "Operational", "Machine median book age is at most 5 seconds"),
        (True, "Unknown", "Feed snapshot is too old to assess machine lag"),
    ))


def _derive_cf(edge, evidence, prev, now_ms):
    edge["counters"]["consecutive_failures"] = _previous_count(prev, "consecutive_failures")
    not_found = (not isinstance(evidence, dict) or set(evidence).issubset({"probe_now"})
                 or evidence.get("metrics_found") is False
                 or "not found" in str(evidence.get("error") or "").lower())
    if not_found:
        edge["health_reason"] = "cloudflared metrics endpoint not found on 127.0.0.1:20241–20245"
        return
    _copy_metrics(edge, evidence)
    ready = evidence.get("ready")
    if isinstance(ready, dict):
        edge["metrics"]["ready_status"] = ready.get("status")
        edge["metrics"]["ready_connections"] = ready.get("ready_connections")
    elif "status" in evidence and "ready_status" not in evidence:
        edge["metrics"]["ready_status"] = evidence.get("status")
    ha = edge["metrics"]["ha_connections"]
    reachable = evidence.get("metrics_reachable")
    if reachable is None:
        metric_names = set(METRIC_KEYS["cf_connector"]) - {"ready_connections", "ready_status", "metrics_url_port"}
        metric_seen = any(evidence.get(key) is not None for key in metric_names)
        reachable = True if metric_seen else (False if evidence.get("ok") is False else None)
    if reachable is None:
        edge["health_reason"] = "cloudflared metrics evidence is malformed"
        return
    if not reachable:
        edge["truth"]["configured"] = "Fail"
        edge["counters"]["consecutive_failures"] += 1
        count = edge["counters"]["consecutive_failures"]
        edge["health"] = "Outage" if count >= 2 else "Degraded"
        edge["health_reason"] = "cloudflared metrics were unreachable for %d consecutive sample%s" % (count, "" if count == 1 else "s")
        return
    edge["counters"]["consecutive_failures"] = 0
    ready_status = edge["metrics"]["ready_status"]
    stream_errors = edge["metrics"]["connect_streams_errors_delta"]
    _truth(edge, "Pass", "Pass" if _is_number(ha) and ha >= 1 else ("Fail" if ha == 0 else "Unknown"),
           "Pass" if ready_status == 200 else ("Fail" if ready_status is not None else "Unknown"))
    _apply_rules(edge, (
        (_stale_sample(edge, now_ms), "Stale", "cloudflared sample is older than its monitor interval"),
        (not _is_number(ha), "Unknown", "cloudflared HA connection metric is not exposed"),
        (ready_status is None, "Unknown", "cloudflared readiness status is not exposed"),
        (not _is_number(stream_errors), "Unknown", "cloudflared stream-error delta is not available yet"),
        (ha == 0, "Outage", "cloudflared has no HA connection"),
        (_is_number(ha) and ha < 4, "Degraded", "cloudflared has %s of 4 HA connections" % ha),
        (_is_number(stream_errors) and stream_errors > 0, "Degraded", "cloudflared stream errors increased by %s" % stream_errors),
        (ready_status != 200, "Degraded", "cloudflared readiness returned %s" % ready_status),
        (True, "Operational", "cloudflared connector is ready"),
    ))


def _derive_origin(edge, evidence, prev, now_ms):
    edge["truth"]["configured"] = "Pass"
    edge["counters"]["consecutive_failures"] = _previous_count(prev, "consecutive_failures")
    valid = isinstance(evidence, dict) and "ok" in evidence and "http_status" in evidence
    if not valid:
        kind = "missing" if not isinstance(evidence, dict) else "malformed"
        edge["health_reason"] = "Origin probe evidence is %s" % kind
        edge["metrics"]["consecutive_failures"] = edge["counters"]["consecutive_failures"]
        return
    _copy_metrics(edge, evidence)
    status = edge["metrics"]["http_status"]
    success = status == 200
    edge["counters"]["consecutive_failures"] = 0 if success else edge["counters"]["consecutive_failures"] + 1
    edge["metrics"]["consecutive_failures"] = edge["counters"]["consecutive_failures"]
    _truth(edge, "Pass", "Pass" if status is not None else "Fail", "Pass" if success else "Fail")
    count = edge["counters"]["consecutive_failures"]
    proxy_errors = edge["metrics"]["request_errors_delta"]
    failed_fact = "connection failed once" if status is None else "returned HTTP %s once" % status
    _apply_rules(edge, (
        (_stale_sample(edge, now_ms), "Stale", "Origin sample is older than its monitor interval"),
        (count >= 2, "Outage", "Origin failed %d consecutive probes" % count),
        (not success, "Degraded", "Origin %s" % failed_fact),
        (_is_number(proxy_errors) and proxy_errors > 0, "Degraded", "Origin proxy errors increased by %s" % proxy_errors),
        (True, "Operational", "Origin returned HTTP 200"),
    ))


def _derive_news(edge, evidence, _prev, _now_ms):
    if not isinstance(evidence, dict):
        edge["health_reason"] = "News source evidence is not mirrored"
        return
    _copy_metrics(edge, evidence)
    available = edge["metrics"]["sources_available"]
    live = edge["metrics"]["sources_live"]
    errors = edge["metrics"]["sources_error"]
    age = edge["metrics"]["as_of_age_s"]
    fresh_limit_s = edge["cadence_ms"] * 3 / 1000.0
    _truth(edge, "Pass", "Not-applicable", "Pass" if _is_number(age) and age <= fresh_limit_s else ("Fail" if _is_number(age) else "Unknown"))
    known = all(_is_number(value) for value in (available, live, errors, age))
    _apply_rules(edge, (
        (not known, "Unknown", "News source metrics are not exposed"),
        (known and age > fresh_limit_s, "Stale", "News evidence is older than %d seconds" % fresh_limit_s),
        (known and errors > 0, "Degraded", "News reports %s source errors" % errors),
        (known and live * 2 < available, "Degraded", "Fewer than half of available news sources are live"),
        (True, "Operational", "News source evidence is current"),
    ))


def _derive_passive(edge, evidence, _prev, _now_ms):
    _copy_metrics(edge, evidence)
    if edge["id"] == "browser_site":
        edge["health_reason"] = "Browser timing is session evidence only" if isinstance(evidence, dict) else "Browser session evidence is not stored"
    elif edge["id"] == "binance_exec" and isinstance(evidence, dict) and all(evidence.get(key) for key in ("last_used_at", "last_op", "last_outcome")):
        edge["health_reason"] = "On-demand path; last used %s, %s %s" % (
            evidence["last_used_at"], evidence["last_op"], evidence["last_outcome"])
    else:
        reasons = {"binance_account": "No signed-read evidence is mirrored", "aws_secrets": "No secrets-read evidence is mirrored", "aws_ssm": "No SSM evidence is mirrored"}
        edge["health_reason"] = reasons.get(edge["id"], "On-demand path; no execution evidence is mirrored")


DERIVERS = dict.fromkeys(("spot_rest", "usdm_rest"), _derive_rest)
DERIVERS.update(dict.fromkeys(("spot_wss", "usdm_wss"), _derive_wss))
DERIVERS.update({"feed_machine": _derive_feed_machine, "cf_connector": _derive_cf,
                 "connector_origin": _derive_origin, "news_fetch": _derive_news})


def derive_edge(edge_id, evidence, prev, config, now_ms):
    """Return one complete edge state from one deterministic observation."""
    edge = _base(edge_id, evidence, prev, config or {}, now_ms)
    if _paused(edge, config or {}):
        return edge
    DERIVERS.get(edge_id, _derive_passive)(edge, evidence, prev, now_ms)
    return edge


def _event(transition, severity, reason, evidence):
    return {"transition": transition, "severity": severity, "reason": reason, "evidence": evidence}


def transitions(prev_edge, new_edge):
    """Return only health changes and the marker events named by V1."""
    events, label = [], new_edge.get("label", new_edge.get("id", "Connection"))
    append = events.append
    if isinstance(prev_edge, dict) and prev_edge.get("health") != new_edge.get("health"):
        old, new = prev_edge.get("health", "Unknown"), new_edge.get("health", "Unknown")
        append(_event("%s → %s" % (old, new), SEVERITY.get(new, "medium"), new_edge.get("health_reason", "Health changed"), {"old_health": old, "new_health": new}))
    previous_metrics, metrics = (prev_edge.get("metrics", {}) if isinstance(prev_edge, dict) else {}), new_edge.get("metrics", {})
    old_reconnects, new_reconnects = previous_metrics.get("reconnects"), metrics.get("reconnects")
    if _is_number(old_reconnects) and _is_number(new_reconnects) and new_reconnects > old_reconnects:
        delta = new_reconnects - old_reconnects
        append(_event("reconnect", "medium", "%s reconnect count increased by %s" % (label, delta), {"delta": delta}))
    old_drop, new_drop = previous_metrics.get("dropped_frames_delta"), metrics.get("dropped_frames_delta")
    if old_drop == 0 and _is_number(new_drop) and new_drop > 0:
        append(_event("frames dropping", "medium", "%s dropped frames increased by %s" % (label, new_drop), {"delta": new_drop}))
    elif _is_number(old_drop) and old_drop > 0 and new_drop == 0:
        append(_event("frames stopped dropping", "info", "%s dropped-frame delta returned to zero" % label, {"delta": 0}))
    old_counters, counters = (prev_edge.get("counters", {}) if isinstance(prev_edge, dict) else {}), new_edge.get("counters", {})
    for code in (429, 418):
        key = "count_%d" % code
        if _previous_count({"counters": counters}, key) > _previous_count({"counters": old_counters}, key):
            severity = "high" if code == 418 else "medium"
            append(_event("%d seen" % code, severity, new_edge["health_reason"], {"http_status": code}))
    proxy_delta = metrics.get("request_errors_delta")
    if new_edge.get("id") == "connector_origin" and _is_number(proxy_delta) and proxy_delta > 0:
        append(_event("origin proxy errors", SEVERITY.get(new_edge.get("health"), "medium"), "Origin proxy errors increased by %s" % proxy_delta, {"request_errors_delta": proxy_delta}))
    if new_edge.get("evidence", {}).get("probe_now") is True:
        status, rtt = metrics.get("http_status", metrics.get("ready_status")), metrics.get("rtt_ms")
        result = "no HTTP response" if status is None else str(status)
        if status is not None and _is_number(rtt):
            result += " in %s ms" % rtt
        reason = "probe now %s: %s" % (new_edge["id"], result)
        append(_event("probe", SEVERITY.get(new_edge.get("health"), "medium"), reason, {"http_status": status, "rtt_ms": rtt}))
    return events


def _read_stale_edge(edge, now_ms):
    item = copy.deepcopy(edge)
    fresh_until = item.get("fresh_until_ms")
    if item.get("health") != "Off" and _is_number(fresh_until) and now_ms > fresh_until:
        item["health"] = "Stale"
        observed = item.get("observed_at") or "Unknown"
        item["health_reason"] = "No fresh sample since %s" % observed
    return item


def summarize(edges, now_ms):
    """Build the frozen summary from effective read-time edge states."""
    effective = [_read_stale_edge(edge, now_ms) for edge in edges]
    required = [edge for edge in effective if edge.get("required") is True]
    worst_edge = max(effective, key=lambda edge: STATUS_RANK.get(edge.get("health"), 3)) if effective else None
    observed_ms = [edge.get("observed_ms") for edge in required if _is_number(edge.get("observed_ms"))]
    latest = None
    latest_ms = None
    for edge in effective:
        candidate = edge.get("last_transition")
        candidate_ms = _timestamp_ms(candidate.get("ts")) if isinstance(candidate, dict) else None
        if (isinstance(candidate, dict) and candidate.get("transition") and
                candidate_ms is not None and
                (latest_ms is None or candidate_ms > latest_ms)):
            latest = dict(candidate)
            latest.setdefault("edge_id", edge.get("id"))
            latest_ms = candidate_ms
    return {
        "operational_required": sum(edge.get("health") == "Operational" for edge in required),
        "observed_required": sum(_is_number(edge.get("sample_count")) and edge.get("sample_count") > 0 for edge in required),
        "required_total": len(required), "unknown_count": sum(edge.get("health") == "Unknown" for edge in effective),
        "worst": {"edge_id": worst_edge.get("id"), "health": worst_edge.get("health")} if worst_edge else {"edge_id": None, "health": "Unknown"},
        "oldest_required_evidence_at": _iso(min(observed_ms)) if observed_ms else None, "last_transition": latest}


def apply_read_time_staleness(current, now_ms):
    """Return a copy with expired edges and collector state updated."""
    result = copy.deepcopy(current)
    result["edges"] = [_read_stale_edge(edge, now_ms) for edge in result.get("edges", [])]
    previous_last = result.get("summary", {}).get("last_transition")
    result["summary"] = summarize(result["edges"], now_ms)
    result["summary"]["last_transition"] = previous_last
    collector = result.get("collector")
    if not isinstance(collector, dict):
        collector = {"status": "unknown"}
        result["collector"] = collector
    generated_ms, tick_s = result.get("generated_ms"), collector.get("tick_s")
    valid_timing = _is_int(generated_ms) and _is_int(tick_s) and tick_s > 0
    if not valid_timing:
        collector["status"] = "unknown"
    elif now_ms - generated_ms > 3 * tick_s * 1000:
        collector["status"] = "stopped"
    return result
