from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request


_METRICS_URLS = tuple(
    "http://127.0.0.1:{}/metrics".format(port)
    for port in range(20241, 20246)
)
_SAMPLE_RE = re.compile(
    r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{([^}]*)\})?\s+([^\s]+)(?:\s+[^\s]+)?$"
)
_STATUS_CODE_RE = re.compile(r'(?:^|,)\s*status_code="([0-9]{3})"(?:\s*,|$)')


def _default_open(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": "quant-network/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        try:
            headers = dict(error.headers.items()) if error.headers else {}
            return error.code, headers, error.read()
        finally:
            error.close()


def _body_bytes(body):
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    if isinstance(body, bytearray):
        return bytes(body)
    raise TypeError("response body is not bytes or text")


def discover_metrics_url(configured, opener=None):
    candidates = []
    if isinstance(configured, str) and configured in _METRICS_URLS:
        candidates.append(configured)
    candidates.extend(url for url in _METRICS_URLS if url not in candidates)
    for url in candidates:
        try:
            status, _headers, body = (opener or _default_open)(url, 3.0)
            if int(status) == 200 and b"cloudflared_" in _body_bytes(body):
                return url
        except Exception:
            continue
    return None


def parse_prometheus_text(text):
    values = {}
    for source_line in text.splitlines():
        line = source_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if match is None:
            continue
        name, labels, raw_value = match.groups()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if labels is None:
            values[name] = value
        else:
            current = values.get(name)
            if not isinstance(current, dict):
                current = {}
                values[name] = current
            current[labels] = value
    return values


def scrape_metrics(url, opener=None, timeout_s=3.0):
    try:
        status, _headers, body = (opener or _default_open)(url, timeout_s)
        status = int(status)
        if status != 200:
            return {"ok": False, "raw_values": {}, "error": "HTTP status {}".format(status)}
        text = _body_bytes(body).decode("utf-8")
        return {"ok": True, "raw_values": parse_prometheus_text(text), "error": None}
    except Exception as error:
        return {
            "ok": False,
            "raw_values": {},
            "error": str(error).strip() or error.__class__.__name__,
        }


def _plain_int(value):
    if isinstance(value, int):
        return None if isinstance(value, bool) else value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def read_ready(base_url, opener=None):
    result = {"ok": False, "status": None, "ready_connections": None}
    try:
        ready_url = base_url.rstrip("/") + "/ready"
        http_status, _headers, body = (opener or _default_open)(ready_url, 3.0)
        http_status = int(http_status)
        if http_status != 200:
            result["status"] = http_status
            return result
        payload = json.loads(_body_bytes(body).decode("utf-8"))
        if not isinstance(payload, dict):
            return result
        result["status"] = _plain_int(payload.get("status"))
        result["ready_connections"] = _plain_int(payload.get("readyConnections"))
        result["ok"] = result["status"] == 200 and result["ready_connections"] is not None
    except Exception:
        pass
    return result


def probe_origin(url="http://127.0.0.1:8080/", opener=None, timeout_s=3.0):
    started = time.monotonic()
    try:
        status, _headers, body = (opener or _default_open)(url, timeout_s)
        elapsed_ms = max(0, int(round((time.monotonic() - started) * 1000.0)))
        status = int(status)
        size = len(_body_bytes(body))
        error = None if status == 200 else "HTTP status {}".format(status)
        return {"ok": status == 200, "http_status": status, "rtt_ms": elapsed_ms,
                "size_bytes": size, "error": error}
    except Exception as error:
        error_text = str(error).strip() or error.__class__.__name__
        return {"ok": False, "http_status": None, "rtt_ms": None,
                "size_bytes": None, "error": error_text}


def _number(value):
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) else float(value)


def _metric_values(value):
    if isinstance(value, dict):
        return [number for number in (_number(item) for item in value.values()) if number is not None]
    number = _number(value)
    return [] if number is None else [number]


def _metric_sum(raw, name):
    if name not in raw:
        return None
    values = _metric_values(raw.get(name))
    return sum(values) if values else None


def _metric_extreme(raw, name, choose):
    values = _metric_values(raw.get(name))
    return choose(values) if values else None


def _counter_rows(raw, name, keep=None):
    if name not in raw:
        return None
    value = raw.get(name)
    if isinstance(value, dict):
        rows = ((label, _number(item)) for label, item in value.items())
        return {label: number for label, number in rows
                if number is not None and (keep is None or keep(label))}
    number = _number(value)
    return None if number is None else {"": number}


def _counter_delta(raw, prev_raw, name, keep=None):
    if prev_raw is None:
        return None
    current = _counter_rows(raw, name, keep)
    previous = _counter_rows(prev_raw, name, keep)
    if current is None or previous is None:
        return None
    if any(current.get(label, 0.0) < value for label, value in previous.items()):
        return None
    delta = sum(current.values()) - sum(previous.values())
    return delta if delta >= 0 else None


def _is_5xx(label):
    match = _STATUS_CODE_RE.search(label)
    return match is not None and 500 <= int(match.group(1)) <= 599


def cf_metrics_from_raw(raw, prev_raw):
    connector = {
        "ha_connections": _metric_sum(raw, "cloudflared_tunnel_ha_connections"),
        "ready_connections": _plain_int(raw.get("ready_connections")),
        "ready_status": _plain_int(raw.get("ready_status")),
        "total_requests_delta": _counter_delta(raw, prev_raw, "cloudflared_tunnel_total_requests"),
        "concurrent_requests": _metric_sum(raw, "cloudflared_tunnel_concurrent_requests_per_tunnel"),
        "latest_rtt_ms_max": _metric_extreme(raw, "quic_client_latest_rtt", max),
        "smoothed_rtt_ms_max": _metric_extreme(raw, "quic_client_smoothed_rtt", max),
        "min_rtt_ms_min": _metric_extreme(raw, "quic_client_min_rtt", min),
        "lost_packets_delta": _counter_delta(raw, prev_raw, "quic_client_lost_packets"),
        "connect_streams_errors_delta": _counter_delta(raw, prev_raw, "cloudflared_proxy_connect_streams_errors"),
        "register_success_total": _metric_sum(raw, "cloudflared_tunnel_tunnel_register_success"),
        "metrics_url_port": _plain_int(raw.get("metrics_url_port")),
    }
    origin_delta = {
        "request_errors_delta": _counter_delta(raw, prev_raw, "cloudflared_tunnel_request_errors"),
        "response_5xx_delta": _counter_delta(raw, prev_raw, "cloudflared_tunnel_response_by_code", _is_5xx),
    }
    return {"cf_connector": connector, "connector_origin_delta": origin_delta}
