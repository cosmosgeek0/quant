from __future__ import annotations

"""Public Binance time probes for Network V1."""

import json
import socket
import time
import urllib.error
import urllib.request


VENUES = {
    "spot_rest": ("https://api.binance.com/api/v3/time", 6000),
    "usdm_rest": ("https://fapi.binance.com/fapi/v1/time", 2400),
}
USER_AGENT = "quant-ui-network/1"


def _default_opener(url, timeout_s):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return (
                int(response.getcode()),
                dict(response.headers.items()),
                response.read(),
            )
    except urllib.error.HTTPError as error:
        headers = dict(error.headers.items()) if error.headers else {}
        return int(error.code), headers, error.read()


def _wall_ms(now_ms):
    value = now_ms() if callable(now_ms) else now_ms
    if value is None:
        value = time.time() * 1000.0
    return int(value)


def _header(headers, name):
    wanted = name.casefold()
    for key, value in (headers or {}).items():
        if str(key).casefold() == wanted:
            return value
    return None


def _nonnegative_int(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _transport_error(error):
    reason = getattr(error, "reason", error)
    text = str(reason).strip()
    return text or error.__class__.__name__


def probe_time(edge_id: str, opener=None, timeout_s: float = 5.0,
               now_ms=None) -> dict:
    """Make one public time request and return bounded probe evidence."""
    if edge_id not in VENUES:
        raise ValueError("Unknown Binance REST edge")

    url, _weight_limit = VENUES[edge_id]
    open_once = opener or _default_opener
    sent_ms = _wall_ms(now_ms)
    started = time.monotonic()
    result = {
        "ok": False,
        "http_status": None,
        "rtt_ms": None,
        "clock_offset_ms": None,
        "used_weight_1m": None,
        "retry_after_s": None,
        "error": None,
        "server_time_ms": None,
    }

    try:
        status, headers, body = open_once(url, timeout_s)
    except urllib.error.HTTPError as error:
        headers = dict(error.headers.items()) if error.headers else {}
        status, body = int(error.code), error.read()
    except (urllib.error.URLError, TimeoutError, socket.timeout,
            OSError) as error:
        result["error"] = _transport_error(error)
        return result

    elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
    result["rtt_ms"] = int(round(elapsed_ms))
    result["http_status"] = _nonnegative_int(status)
    result["used_weight_1m"] = _nonnegative_int(
        _header(headers, "X-MBX-USED-WEIGHT-1M"))
    result["retry_after_s"] = _nonnegative_int(
        _header(headers, "Retry-After"))

    if result["http_status"] != 200:
        if result["http_status"] is None:
            result["error"] = "Invalid HTTP status"
        else:
            result["error"] = "HTTP %d" % result["http_status"]
        return result

    try:
        payload = json.loads(body.decode("utf-8"))
    except (AttributeError, UnicodeError, ValueError):
        result["error"] = "Malformed JSON response"
        return result

    if not isinstance(payload, dict):
        result["error"] = "Malformed JSON response"
        return result
    server_time_ms = _nonnegative_int(payload.get("serverTime"))
    if server_time_ms is None:
        result["error"] = "Invalid serverTime response"
        return result

    midpoint_ms = sent_ms + elapsed_ms / 2.0
    result["ok"] = True
    result["server_time_ms"] = server_time_ms
    result["clock_offset_ms"] = int(round(server_time_ms - midpoint_ms))
    return result


def next_allowed_ms(prev_result: dict, now_ms: int) -> int:
    """Return the first allowed probe time from a Retry-After value."""
    current_ms = _wall_ms(now_ms)
    retry_after_s = _nonnegative_int(
        (prev_result or {}).get("retry_after_s"))
    if retry_after_s is None:
        return current_ms
    return current_ms + retry_after_s * 1000
