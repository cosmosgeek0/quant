#!/usr/bin/env python3
"""Local telemetry sampler for the Quant dashboard.

Samples the dashboard's local strip and health endpoints, then appends one
honest JSON row under ui/live/. The history is bounded with an atomic rewrite
that retains the newest rows.

Run once:  python3 ui/telemetry_recorder.py [-q]
Live loop: python3 ui/telemetry_recorder.py --loop 15 -q
"""

import argparse
from collections import deque
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.request


BASE = Path(__file__).resolve().parent
OUT_PATH = BASE / "live" / "telemetry.jsonl"
POLL_CONFIG_PATH = BASE / "live" / "poll_config.json"
ALLOWED_INTERVALS = (1, 2, 5, 10, 15, 30, 60, 120)
DEFAULT_INTERVAL = 15
METRIC_FIELDS = {
    "server": ("rtt_ms", "machine"),
    "mirror": ("mirror_s",),
    "market": ("market_s",),
    "supervisor": ("supervisor_s", "supervisor_status"),
    "newsloop": ("news_s",),
    "basiswatch": ("basis_watch_s",),
    "basissup": ("basis_sup_s",),
    "jobrunner": ("jobs_s",),
}
STRIP_METRICS = ("server", "mirror", "market", "supervisor")
HEALTH_METRICS = ("basiswatch", "basissup", "jobrunner")
NEWS_SOURCES_PATH = BASE / "live" / "news_sources.json"
STRIP_URL = "http://127.0.0.1:8899/api/strip"
HEALTH_URL = "http://127.0.0.1:8899/api/health"
FETCH_TIMEOUT = 4
TRIM_AT_LINES = 220000
KEEP_LINES = 200000

DOUBLE_FAILURE_FIELDS = (
    "rtt_ms",
    "machine",
    "mirror_s",
    "market_s",
    "supervisor_s",
    "supervisor_status",
    "basis_watch_s",
    "basis_sup_s",
    "jobs_s",
)


def finite_float(value):
    """Return a finite float, or None for absent and untrusted values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _text(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _service_age(health, service_name):
    services = health.get("services")
    if not isinstance(services, list):
        return None
    for service in services:
        if (isinstance(service, dict)
                and service.get("service") == service_name):
            return finite_float(service.get("last_seen_age_seconds"))
    return None


def _news_age(as_of, now):
    if not isinstance(as_of, str):
        return None
    try:
        parsed = datetime.fromisoformat(as_of.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    age = (now.astimezone(timezone.utc)
           - parsed.astimezone(timezone.utc)).total_seconds()
    return finite_float(age)


def build_row(strip, health, rtt_ms, news_as_of=None, now=None):
    """Build one telemetry row from already-fetched payloads."""
    strip = _mapping(strip)
    health = _mapping(health)
    pull = _mapping(strip.get("pull"))
    market = _mapping(strip.get("market_pass"))
    supervisor = _mapping(strip.get("supervisor"))
    jobs_log = _mapping(health.get("jobs_log"))
    now = now or datetime.now(timezone.utc)

    measured_rtt = finite_float(rtt_ms)
    if measured_rtt is not None and measured_rtt >= 0:
        measured_rtt = round(measured_rtt)
    else:
        measured_rtt = None

    return {
        "ts": now.astimezone(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z"),
        "rtt_ms": measured_rtt,
        "machine": _text(strip.get("machine")),
        "mirror_s": finite_float(pull.get("age_seconds")),
        "market_s": finite_float(market.get("age_seconds")),
        "supervisor_s": finite_float(supervisor.get("age_seconds")),
        "supervisor_status": _text(supervisor.get("reported_status")),
        "news_s": _news_age(news_as_of, now),
        "basis_watch_s": _service_age(health, "basis_watch"),
        "basis_sup_s": _service_age(health, "basis_supervisor"),
        "jobs_s": finite_float(jobs_log.get("last_tick_age_seconds")),
    }


def fetch_json(url, timeout=FETCH_TIMEOUT):
    """GET one JSON object; callers handle all transport/data failures."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


def _safe_fetch(fetcher, url):
    try:
        payload = fetcher(url, FETCH_TIMEOUT)
    except Exception:
        return {}, False
    if not isinstance(payload, dict):
        return {}, False
    return payload, True


def read_news_as_of(path=NEWS_SOURCES_PATH):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload.get("as_of")


def load_poll_config(path=POLL_CONFIG_PATH):
    """Per-metric sample intervals, owner-set through /api/poll_config."""
    intervals = {}
    default = DEFAULT_INTERVAL
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        payload = {}
    if isinstance(payload, dict):
        candidate = payload.get("default_s")
        if candidate in ALLOWED_INTERVALS:
            default = candidate
        metrics = payload.get("metrics")
        if isinstance(metrics, dict):
            for key in METRIC_FIELDS:
                value = metrics.get(key)
                if value in ALLOWED_INTERVALS:
                    intervals[key] = value
    for key in METRIC_FIELDS:
        intervals.setdefault(key, default)
    return intervals


def prune_row(row, due_metrics):
    """Keep ts plus only the fields whose metrics were due this cycle."""
    kept = {"ts": row["ts"]}
    for metric in due_metrics:
        for field in METRIC_FIELDS[metric]:
            kept[field] = row.get(field)
    return kept


def collect_sample(fetcher=None, news_path=NEWS_SOURCES_PATH,
                   now_fn=None, clock=None, due_metrics=None):
    """Fetch what the due metrics need and return (row, all_fetches_failed)."""
    fetcher = fetcher or fetch_json
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    clock = clock or time.monotonic
    if due_metrics is None:
        due_metrics = tuple(METRIC_FIELDS)

    need_strip = any(m in due_metrics for m in STRIP_METRICS)
    need_health = any(m in due_metrics for m in HEALTH_METRICS)
    need_news = "newsloop" in due_metrics

    strip, strip_ok = ({}, True)
    rtt_ms = None
    if need_strip:
        started = clock()
        strip, strip_ok = _safe_fetch(fetcher, STRIP_URL)
        elapsed_ms = (clock() - started) * 1000.0
        rtt_ms = round(elapsed_ms) if strip_ok and finite_float(
            elapsed_ms) is not None and elapsed_ms >= 0 else None

    health, health_ok = ({}, True)
    if need_health:
        health, health_ok = _safe_fetch(fetcher, HEALTH_URL)

    now = now_fn()
    row = build_row(
        strip,
        health,
        rtt_ms,
        news_as_of=read_news_as_of(news_path) if need_news else None,
        now=now,
    )
    row = prune_row(row, due_metrics)
    attempted_failures = []
    if need_strip:
        attempted_failures.append(strip_ok)
    if need_health:
        attempted_failures.append(health_ok)
    all_failed = (bool(attempted_failures)
                  and not any(attempted_failures))
    return row, all_failed


def trim_file(path, max_lines=TRIM_AT_LINES, keep_lines=KEEP_LINES):
    """Atomically retain the newest keep_lines when max_lines is exceeded."""
    if keep_lines <= 0 or max_lines < keep_lines:
        raise ValueError("line bounds must satisfy 0 < keep_lines <= max_lines")
    path = Path(path)
    newest = deque(maxlen=keep_lines)
    count = 0
    try:
        with open(path, "r", encoding="utf-8") as source:
            for line in source:
                count += 1
                newest.append(line)
    except FileNotFoundError:
        return False
    if count <= max_lines:
        return False

    handle, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".%s-" % path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temporary:
            for line in newest:
                temporary.write(line.rstrip("\r\n") + "\n")
            temporary.flush()
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return True


_APPENDS_SINCE_TRIM = {"n": 0}


def append_row(row, path=OUT_PATH):
    """Append one flushed JSON line; bound the history every 200 rows
    (a full-file line scan per append cannot survive 1s sampling)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as output:
        output.write(encoded + "\n")
        output.flush()
    _APPENDS_SINCE_TRIM["n"] += 1
    if _APPENDS_SINCE_TRIM["n"] >= 200:
        _APPENDS_SINCE_TRIM["n"] = 0
        trim_file(path)


def is_double_failure_row(row):
    """A row carrying no observation at all (every field beyond ts None)."""
    if not isinstance(row, dict):
        return False
    fields = [k for k in row if k != "ts"]
    return bool(fields) and all(row[k] is None for k in fields)


def last_row_was_double_failure(path=OUT_PATH):
    last = None
    try:
        with open(path, "r", encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    last = line
    except (OSError, UnicodeError):
        return False
    if last is None:
        return False
    try:
        return is_double_failure_row(json.loads(last))
    except ValueError:
        return False


def should_write_sample(both_fetches_failed, previous_double_failure):
    return not (both_fetches_failed and previous_double_failure)


def run_cycle(previous_double_failure, quiet=False, due_metrics=None):
    row, both_fetches_failed = collect_sample(due_metrics=due_metrics)
    if should_write_sample(both_fetches_failed, previous_double_failure):
        append_row(row)
        previous_double_failure = both_fetches_failed
    if not quiet:
        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    return previous_double_failure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, default=0,
                        help="sample every N seconds (0 = run once)")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args()

    previous_double_failure = last_row_was_double_failure()
    if args.loop <= 0:
        try:
            run_cycle(previous_double_failure, args.quiet)
        except Exception as error:
            print("telemetry_recorder: sample failed: %s" % error,
                  file=sys.stderr)
            return 1
        return 0

    # Owner intervals rule; --loop is only the sleep granularity floor.
    # Fixed-rate scheduling: deadlines advance from the PREVIOUS
    # deadline, so fetch latency does not stretch every interval, and
    # a lowered interval takes effect within one wake.
    next_due = {}
    prev_intervals = {}
    try:
        while True:
            intervals = load_poll_config()
            now = time.monotonic()
            for m in METRIC_FIELDS:
                if prev_intervals.get(m) != intervals[m]:
                    limit = now + intervals[m]
                    if m not in next_due or next_due[m] > limit:
                        next_due[m] = min(next_due.get(m, now), limit)
                    prev_intervals[m] = intervals[m]
            due = [m for m in METRIC_FIELDS
                   if next_due.get(m, 0) <= now]
            if due:
                try:
                    previous_double_failure = run_cycle(
                        previous_double_failure, args.quiet,
                        due_metrics=tuple(due))
                except Exception as error:  # keep the loop alive
                    print("telemetry_recorder: sample failed: %s" % error,
                          file=sys.stderr)
                after = time.monotonic()
                for m in due:
                    base = next_due.get(m, after)
                    target = base + intervals[m]
                    if target <= after:
                        target = after + intervals[m]
                    next_due[m] = target
            wake = min(next_due.values()) if next_due else (
                time.monotonic() + 1)
            remaining = wake - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, 120))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
