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
NEWS_SOURCES_PATH = BASE / "live" / "news_sources.json"
STRIP_URL = "http://127.0.0.1:8899/api/strip"
HEALTH_URL = "http://127.0.0.1:8899/api/health"
FETCH_TIMEOUT = 4
TRIM_AT_LINES = 46000
KEEP_LINES = 40000

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


def collect_sample(fetcher=None, news_path=NEWS_SOURCES_PATH,
                   now_fn=None, clock=None):
    """Fetch both endpoints and return (row, both_fetches_failed)."""
    fetcher = fetcher or fetch_json
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    clock = clock or time.monotonic

    started = clock()
    strip, strip_ok = _safe_fetch(fetcher, STRIP_URL)
    elapsed_ms = (clock() - started) * 1000.0
    rtt_ms = round(elapsed_ms) if strip_ok and finite_float(
        elapsed_ms) is not None and elapsed_ms >= 0 else None

    health, health_ok = _safe_fetch(fetcher, HEALTH_URL)
    now = now_fn()
    row = build_row(
        strip,
        health,
        rtt_ms,
        news_as_of=read_news_as_of(news_path),
        now=now,
    )
    return row, not strip_ok and not health_ok


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


def append_row(row, path=OUT_PATH):
    """Append exactly one flushed JSON line, then enforce the history bound."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as output:
        output.write(encoded + "\n")
        output.flush()
    trim_file(path)


def is_double_failure_row(row):
    """Identify a persisted row with no strip or health observations."""
    return (isinstance(row, dict)
            and all(field in row and row[field] is None
                    for field in DOUBLE_FAILURE_FIELDS))


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


def run_cycle(previous_double_failure, quiet=False):
    row, both_fetches_failed = collect_sample()
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

    interval = args.loop
    try:
        while True:
            started = time.monotonic()
            try:
                previous_double_failure = run_cycle(
                    previous_double_failure, args.quiet)
            except Exception as error:  # keep the loop alive
                print("telemetry_recorder: sample failed: %s" % error,
                      file=sys.stderr)
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
