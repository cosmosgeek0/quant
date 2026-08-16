#!/usr/bin/env python3
"""Read-only localhost runtime for the quant UI. Stdlib only.

Binds 127.0.0.1 exclusively and serves static ui/ files plus factual GETs over
the files written beneath RECEIPT_DIR:

  /api/stream      stream_state.json, ranked by surge plus every basis gap
  /api/candidates  complete, structurally valid, unexpired proposal JSON files
  /api/position    the current basis-position.json and its supervision fields
  /api/momentum    runner-backed momentum state, receipts, and result ledgers
  /api/fees        commission-rates.json verbatim
  /api/pnl         explicit P&L blocks in CLOSED/reconcile basis receipts
  /api/events      the recorded spot-run fixture, history only
  /api/health      service ticks, job-log tail, stop flags, and tick ages
  /api/decisions   recent momentum decisions with recorded signal evidence
  /api/failures    recent REFUSED/UNKNOWN job results and recorded reasons
  /api/telemetry   recent timestamped UI telemetry, range-filtered and sampled

Missing sources are honest empty responses. Malformed input is counted, never
repaired. GET and HEAD are the only accepted methods. The server never writes a
file and never calls the network.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import stat as stat_module
import sys
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

UI_DIR = Path(__file__).resolve().parent
ROOT_DIR = UI_DIR.parent
LIVE_DIR = UI_DIR / "live"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
try:
    from src.runtime_paths import resolve_runtime_paths
except ModuleNotFoundError as error:
    if error.name not in {"src", "src.runtime_paths"}:
        raise

    # Temporary deployment fallback.  These constants mirror
    # src/runtime_paths.py so a UI-only host copy still reads canonical files.
    DEFAULT_RECEIPT_DIR = "~/quant_receipts"
    JOB_RESULTS_NAME = "results.jsonl"
    PROPOSALS_DIR_NAME = "proposals"
    WATCH_DIR_NAME = "watch"
    STREAM_STATE_NAME = "stream_state.json"
    STREAM_LOG_NAME = "stream_log.jsonl"
    STREAM_WINDOWS_NAME = "stream_windows.json"
    BASIS_WATCH_STATE_NAME = "basis_watch_state.json"
    DECISIONS_NAME = "decisions.jsonl"
    HEALTH_NAME = "health.jsonl"
    MOMENTUM_STATE_NAME = "momentum-paper-state.json"
    MOMENTUM_POSITION_NAME = "momentum-position.json"
    MOMENTUM_RECEIPTS_NAME = "momentum-paper-receipts.jsonl"
    STOP_NAME = "STOP"
    STOP_JOBS_NAME = "STOP-JOBS"

    def resolve_runtime_paths(environ=None):
        source = os.environ if environ is None else environ
        receipt_dir = Path(os.path.expanduser(
            source.get("RECEIPT_DIR", DEFAULT_RECEIPT_DIR)))
        jobs_dir = receipt_dir / "jobs"
        watch_dir = receipt_dir / WATCH_DIR_NAME
        return SimpleNamespace(
            receipt_dir=receipt_dir,
            jobs_dir=jobs_dir,
            job_results=jobs_dir / JOB_RESULTS_NAME,
            proposals_dir=receipt_dir / PROPOSALS_DIR_NAME,
            watch_dir=watch_dir,
            stream_state=receipt_dir / STREAM_STATE_NAME,
            stream_log=receipt_dir / STREAM_LOG_NAME,
            stream_windows=receipt_dir / STREAM_WINDOWS_NAME,
            basis_watch_state=watch_dir / BASIS_WATCH_STATE_NAME,
            decisions=receipt_dir / DECISIONS_NAME,
            health=receipt_dir / HEALTH_NAME,
            momentum_state=receipt_dir / MOMENTUM_STATE_NAME,
            momentum_position=receipt_dir / MOMENTUM_POSITION_NAME,
            momentum_receipts=receipt_dir / MOMENTUM_RECEIPTS_NAME,
            stop=receipt_dir / STOP_NAME,
            stop_jobs=receipt_dir / STOP_JOBS_NAME,
        )

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8899
STREAM_TOP_N = 20
RECENT_MOMENTUM_RECEIPTS = 50
RECENT_MOMENTUM_RESULTS = 50
MOMENTUM_RESULT_SCAN_RECORDS = 250
MOMENTUM_RESULT_OPS = frozenset({
    "MOMENTUM_ROUNDTRIP", "MOMENTUM_OPEN", "MOMENTUM_EXIT",
})
RECENT_HEALTH_RECORDS = 200
RECENT_JOB_LOG_LINES = 40
RECENT_DECISIONS = 50
RECENT_NEWS = 2000
RECENT_FAILURES = 50
FAILURE_SCAN_RECORDS = 250
TELEMETRY_MAX_POINTS = 7200
TELEMETRY_RANGE_HOURS = {"1h": 1, "6h": 6, "24h": 24}
TAIL_BYTES = 1024 * 1024
SOURCE_PARSE_CACHE_ENTRIES = 512
GZIP_MIN_BYTES = 512
REQUEST_TIMEOUT_SECONDS = 10
HEALTH_AMBER_AFTER_SECONDS = 90
HEALTH_RED_AFTER_SECONDS = 300
JOB_AMBER_AFTER_SECONDS = 90
JOB_RED_AFTER_SECONDS = 180
# The Lead's pull cycle is ~30s over SSM; older than these means it is dying.
STRIP_PULL_AMBER_AFTER_SECONDS = 45
STRIP_PULL_RED_AFTER_SECONDS = 120
# Display mirrors of the account-governor defaults; the ledger is the truth.
GOVERNOR_ENTRIES_CAP = 5
GOVERNOR_LOSS_FEES_CAP = Decimal("1.00")
GOVERNOR_LEDGER_PREFIX = "account-governor-"

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
UTC_IN_TEXT_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)

PROPOSAL_REQUIRED_FIELDS = frozenset({
    "proposal_id", "created_utc", "expires_utc", "quant_sha",
    "config_digest", "symbol", "direction", "spot_ask", "perp_bid",
    "entry_gap_pct", "spot_bid", "perp_ask", "exit_gap_pct",
    "spot_maker_commission_pct", "spot_taker_commission_pct",
    "perp_maker_commission_pct", "perp_taker_commission_pct",
    "commission_source", "entry_slippage_pct", "exit_slippage_pct",
    "funding_rate", "funding_period_ms", "carry_pct",
    "safety_buffer_pct", "net_edge_pct", "quantity",
})
BASIS_RECEIPT_PREFIXES = (
    "basis-executor-", "basis-position-", "basis-reconcile-",
)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}

SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy",
     "default-src 'none'; script-src 'self'; "
     "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
     "img-src 'self'; font-src 'self'; base-uri 'none'; "
     "form-action 'none'; frame-ancestors 'none'"),
)

_SOURCE_PARSE_CACHE = OrderedDict()
_SOURCE_PARSE_CACHE_LOCK = threading.RLock()


def default_sources():
    """Resolve source paths inside the pulled host mirror at ui/live/.

    The UI seat never reads the host or the writer's runtime paths directly;
    the seated Lead's pull script mirrors the host receipt dir into ui/live/
    (~30s cycle, atomic per-file, manifest written last). Absent files render
    as honest empties until the first pull lands.
    """
    return {
        "events": UI_DIR / "fixtures" / "events.jsonl",
        "stream_state": LIVE_DIR / "stream_state.json",
        "stream_log": LIVE_DIR / "stream_log.jsonl",
        "proposals_dir": LIVE_DIR / "proposals",
        "basis_position": LIVE_DIR / "basis-position.json",
        "basis_receipts_dir": LIVE_DIR,
        "fees": LIVE_DIR / "commission-rates.json",
        "momentum_state": LIVE_DIR / "momentum-position.json",
        "momentum_receipts": LIVE_DIR / "momentum-paper-receipts.jsonl",
        "health": LIVE_DIR / "health.jsonl",
        "poll_config": LIVE_DIR / "poll_config.json",
        "jobs_log": Path(os.path.expanduser(os.environ.get(
            "JOBS_LOG", str(LIVE_DIR / "jobs.log")))),
        "stop": LIVE_DIR / "STOP",
        "stop_jobs": LIVE_DIR / "STOP-JOBS",
        "momentum_decisions": LIVE_DIR / "decisions.jsonl",
        "job_results": LIVE_DIR / "jobs" / "results.jsonl",
        "job_receipts_dir": LIVE_DIR / "jobs" / "receipts",
        "governor_dir": LIVE_DIR,
        "governor_lock": LIVE_DIR / "account-governor.lock",
        "manifest": LIVE_DIR / "pull_manifest.json",
        "news": LIVE_DIR / "news.jsonl",
        "news_sources": LIVE_DIR / "news_sources.json",
        "telemetry": LIVE_DIR / "telemetry.jsonl",
        "grace": LIVE_DIR / "grace.json",
    }


def _stat_identity(facts):
    return facts.st_mtime_ns, facts.st_size, facts.st_ino


def _source_cache_key(path):
    return os.path.abspath(os.fspath(path))


def _cache_file_value(path, variant, loader, absent):
    """Return one parsed file value, invalidated by atomic-swap stat facts."""
    key = _source_cache_key(path)
    with _SOURCE_PARSE_CACHE_LOCK:
        try:
            current_identity = _stat_identity(os.stat(path))
        except (OSError, ValueError):
            _SOURCE_PARSE_CACHE.pop(key, None)
            return absent, None

        entry = _SOURCE_PARSE_CACHE.get(key)
        if entry is not None and entry["identity"] == current_identity:
            cached = entry["values"].get(variant)
            if cached is not None:
                _SOURCE_PARSE_CACHE.move_to_end(key)
                return cached, current_identity

        try:
            value, opened_identity = loader(path)
        except (OSError, UnicodeError, ValueError):
            _SOURCE_PARSE_CACHE.pop(key, None)
            return absent, None

        try:
            after_identity = _stat_identity(os.stat(path))
        except (OSError, ValueError):
            _SOURCE_PARSE_CACHE.pop(key, None)
        else:
            if after_identity == opened_identity:
                entry = _SOURCE_PARSE_CACHE.get(key)
                if entry is None or entry["identity"] != opened_identity:
                    entry = {"identity": opened_identity, "values": {}}
                    _SOURCE_PARSE_CACHE[key] = entry
                entry["values"][variant] = value
                _SOURCE_PARSE_CACHE.move_to_end(key)
                while len(_SOURCE_PARSE_CACHE) > SOURCE_PARSE_CACHE_ENTRIES:
                    _SOURCE_PARSE_CACHE.popitem(last=False)
        return value, opened_identity


def _load_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        identity = _stat_identity(os.fstat(handle.fileno()))
        text = handle.read()
    return (True, text), identity


def _read_text(path):
    """Return (present, text); absent/unreadable sources are never created."""
    value, _ = _cache_file_value(
        path, ("text",), _load_text, (False, None))
    return value


def _read_json(path):
    """Return (present, value, parse_errors)."""
    def load_json(source):
        with open(source, "r", encoding="utf-8") as handle:
            identity = _stat_identity(os.fstat(handle.fileno()))
            text = handle.read()
        try:
            value = (True, json.loads(text), 0)
        except (json.JSONDecodeError, TypeError):
            value = (True, None, 1)
        return value, identity

    value, _ = _cache_file_value(
        path, ("json",), load_json, (False, None, 0))
    return value


def _read_jsonl(path):
    """Return (present, records, parse_errors), preserving valid line order."""
    def load_jsonl(source):
        with open(source, "r", encoding="utf-8") as handle:
            identity = _stat_identity(os.fstat(handle.fileno()))
            text = handle.read()
        records, parse_errors = [], 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                parse_errors += 1
        return (True, records, parse_errors), identity

    value, _ = _cache_file_value(
        path, ("jsonl",), load_jsonl, (False, [], 0))
    return value


def _load_tail_lines(path, limit):
    with open(path, "rb") as handle:
        identity = _stat_identity(os.fstat(handle.fileno()))
        end = handle.seek(0, os.SEEK_END)
        start = max(0, end - TAIL_BYTES)
        handle.seek(start)
        raw = handle.read()
    truncated = start > 0
    if truncated:
        boundary = raw.find(b"\n")
        raw = raw[boundary + 1:] if boundary >= 0 else b""
    encoding_errors = 0
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        encoding_errors = 1
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (True, lines[-limit:], encoding_errors, truncated), identity


def _read_tail_lines(path, limit):
    """Return a bounded tail without treating a partial first line as data."""
    value, _ = _cache_file_value(
        path, ("tail_lines", limit, TAIL_BYTES),
        lambda source: _load_tail_lines(source, limit),
        (False, [], 0, False))
    return value


def _read_jsonl_tail(path, limit, scan_limit=None):
    """Return valid objects from a bounded JSONL tail in file order."""
    line_limit = scan_limit or max(limit * 4, limit)

    def load_jsonl_tail(source):
        tail, identity = _load_tail_lines(source, line_limit)
        _, lines, parse_errors, truncated = tail
        records = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            records.append(value)
        return (True, records[-limit:], parse_errors, truncated), identity

    value, _ = _cache_file_value(
        path, ("jsonl_tail", limit, line_limit, TAIL_BYTES),
        load_jsonl_tail, (False, [], 0, False))
    return value


def _read_static(path):
    """Return a cached static-file snapshot and its strong identity facts."""
    def load_static(source):
        with open(source, "rb") as handle:
            facts = os.fstat(handle.fileno())
            body = handle.read()
        identity = _stat_identity(facts)
        return (body, facts.st_mtime), identity

    value, identity = _cache_file_value(
        path, ("static",), load_static, None)
    if value is None:
        return False, None, None, None
    body, modified = value
    return True, body, identity, modified


def _path_as_of(path):
    """UTC mtime for a read source, used only when the record has no clock."""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _record_as_of(record, fallback=None):
    if isinstance(record, dict):
        for key in ("as_of", "last_tick_utc", "tick_utc", "decision_utc",
                    "updated_utc", "updated_at", "utc", "ts", "timestamp",
                    "finished_utc", "closed_utc", "created_utc"):
            if record.get(key) not in (None, ""):
                return record[key]
    return fallback


def _latest_as_of(values, fallback=None):
    usable = [str(value) for value in values if value not in (None, "")]
    return max(usable) if usable else fallback


def _finite_decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _parse_utc(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value, now):
    parsed = _parse_utc(value)
    if parsed is None:
        return None
    age = (now - parsed).total_seconds()
    if age < 0:
        return None
    return round(age, 3)


def _positive_seconds(value):
    number = _finite_decimal(value)
    if number is None or number <= 0:
        return None
    return float(number)


def _stream_symbols(payload):
    """Accept the planned symbol->record shape and harmless wrapper variants."""
    if not isinstance(payload, dict):
        return {}
    for key in ("symbols", "tokens"):
        wrapped = payload.get(key)
        if isinstance(wrapped, dict):
            return wrapped
    return {symbol: record for symbol, record in payload.items()
            if isinstance(record, dict)}


def _both_venues(record):
    venues = record.get("venues")
    names = set()
    if isinstance(venues, dict):
        names = {str(name).lower() for name, available in venues.items()
                 if available}
    elif isinstance(venues, (list, tuple, set)):
        names = {str(name).lower() for name in venues}
    elif isinstance(venues, str):
        names = {part.strip().lower() for part in venues.split(",")}
    named_both = "spot" in names and bool(
        names.intersection({"perp", "usdm", "futures"}))
    quoted_both = all(record.get(key) is not None for key in (
        "spot_bid", "spot_ask", "perp_bid", "perp_ask"))
    return named_both or quoted_both


def api_events(sources):
    """Historical spot runs only; this is the one deliberately kept fixture."""
    path = sources["events"]
    present, records, parse_errors = _read_jsonl(path)
    return {
        "source": str(path),
        "present": present,
        "as_of": _path_as_of(path) if present else None,
        "historical": True,
        "count": len(records),
        "events": records,
        "parse_errors": parse_errors,
    }


def api_stream(sources):
    state_path = sources["stream_state"]
    log_path = sources["stream_log"]
    present, state, state_errors = _read_json(state_path)
    log_present, log_records, log_errors = _read_jsonl(log_path)
    symbol_map = _stream_symbols(state)

    source_as_of = _record_as_of(state)
    if source_as_of is None:
        for record in reversed(log_records):
            source_as_of = _record_as_of(record)
            if source_as_of is not None:
                break
    if source_as_of is None and present:
        source_as_of = _path_as_of(state_path)

    ranked = []
    basis_symbols = set()
    for symbol, record in symbol_map.items():
        if not isinstance(record, dict):
            state_errors += 1
            continue
        ratio = _finite_decimal(record.get("surge_ratio"))
        if ratio is not None:
            ranked.append((ratio, str(symbol), record))
        gap = _finite_decimal(record.get("basis_gap_pct"))
        if gap is not None and _both_venues(record):
            basis_symbols.add(str(symbol))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    top_symbols = [symbol for _, symbol, _ in ranked[:STREAM_TOP_N]]
    ordered_symbols = top_symbols + sorted(basis_symbols.difference(top_symbols))

    tokens = []
    for symbol in ordered_symbols:
        record = symbol_map[symbol]
        selection = []
        if symbol in top_symbols:
            selection.append("top_surge")
        if symbol in basis_symbols:
            selection.append("basis_gap")
        tokens.append({
            "symbol": symbol,
            "selection": selection,
            "as_of": _record_as_of(record, source_as_of),
            "data": record,
        })
    return {
        "source": str(state_path),
        "log_source": str(log_path),
        "present": present,
        "log_present": log_present,
        "as_of": source_as_of,
        "top_n": STREAM_TOP_N,
        "count": len(tokens),
        "top_surge_count": len(top_symbols),
        "basis_gap_count": len(basis_symbols),
        "tokens": tokens,
        "log_count": len(log_records),
        "parse_errors": state_errors + log_errors,
        "state_parse_errors": state_errors,
        "log_parse_errors": log_errors,
    }


def _proposal_status(payload, now):
    if not isinstance(payload, dict) \
            or not PROPOSAL_REQUIRED_FIELDS.issubset(payload) \
            or any(payload.get(key) is None for key in PROPOSAL_REQUIRED_FIELDS):
        return "invalid"
    expires = _parse_utc(payload.get("expires_utc"))
    if expires is None:
        return "invalid"
    if expires <= now:
        return "expired"
    net = _finite_decimal(payload.get("net_edge_pct"))
    quantity = _finite_decimal(payload.get("quantity"))
    if net is None or net <= 0 or quantity is None or quantity <= 0:
        return "invalid"
    if not isinstance(payload.get("proposal_id"), str) \
            or not isinstance(payload.get("symbol"), str):
        return "invalid"
    return "valid"


def api_candidates(sources):
    directory = sources["proposals_dir"]
    payload = {
        "source": str(directory), "present": False, "as_of": None,
        "count": 0, "scanned_count": 0, "expired_count": 0,
        "invalid_count": 0, "candidates": [], "parse_errors": 0,
    }
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.endswith(".json"))
    except OSError:
        return payload
    payload["present"] = True
    payload["scanned_count"] = len(names)
    as_of_values = []
    now = datetime.now(timezone.utc)
    for name in names:
        path = directory / name
        present, proposal, errors = _read_json(path)
        file_as_of = _path_as_of(path) if present else None
        as_of_values.append(file_as_of)
        payload["parse_errors"] += errors
        if not present or errors:
            payload["invalid_count"] += 1
            continue
        status = _proposal_status(proposal, now)
        if status == "expired":
            payload["expired_count"] += 1
            continue
        if status != "valid":
            payload["invalid_count"] += 1
            continue
        payload["candidates"].append({
            "file": name,
            "as_of": _record_as_of(proposal, file_as_of),
            "proposal": proposal,
        })
    payload["candidates"].sort(
        key=lambda entry: (-_finite_decimal(
            entry["proposal"]["net_edge_pct"]),
                           entry["proposal"]["symbol"]))
    payload["count"] = len(payload["candidates"])
    payload["as_of"] = _latest_as_of(
        as_of_values, _path_as_of(directory))
    return payload


def _position_supervision(position):
    if not isinstance(position, dict):
        return None
    if "supervision" in position:
        return position["supervision"]
    fields = {}
    for key in ("last_check", "reconcile", "recommendations"):
        if key in position:
            fields[key] = position[key]
    return fields or None


def api_position(sources):
    path = sources["basis_position"]
    present, position, parse_errors = _read_json(path)
    if present and position is not None and not isinstance(position, dict):
        position = None
        parse_errors += 1
    return {
        "source": str(path),
        "present": present,
        "as_of": _record_as_of(position, _path_as_of(path))
        if present else None,
        "position": position,
        "supervision": _position_supervision(position),
        "parse_errors": parse_errors,
    }


def _momentum_positions(state):
    """Return only positions proven open by the persisted runner state."""
    if not isinstance(state, dict):
        return {}, 0
    runner_state = state.get("state")
    if runner_state in {"OPENING", "OPEN", "CLOSING", "UNKNOWN"}:
        symbol = state.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            return {"position": state}, 1
        return {symbol: state}, 0
    positions = state.get("positions")
    if isinstance(positions, (dict, list)):
        return positions, 0
    position = state.get("position")
    if isinstance(position, dict):
        symbol = position.get("symbol")
        return {str(symbol or "position"): position}, 0

    tokens = state.get("tokens")
    if not isinstance(tokens, dict):
        return {}, 0
    opened = {}
    parse_errors = 0
    for symbol, token in tokens.items():
        if not isinstance(token, dict):
            parse_errors += 1
            continue
        quantity = _finite_decimal(token.get("qty"))
        if quantity is None:
            parse_errors += 1
            continue
        if quantity != 0:
            opened[str(symbol)] = token
    return opened, parse_errors


def api_momentum(sources):
    state_path = sources["momentum_state"]
    receipts_path = sources["momentum_receipts"]
    results_path = sources["job_results"]
    state_present, state, state_errors = _read_json(state_path)
    if state_present and state is not None and not isinstance(state, dict):
        state = None
        state_errors += 1
    positions, position_errors = _momentum_positions(state)
    state_errors += position_errors
    receipts_present, raw_records, receipt_errors = _read_jsonl(receipts_path)
    records = []
    for record in raw_records:
        if isinstance(record, dict):
            records.append(record)
        else:
            receipt_errors += 1

    state_as_of = _record_as_of(state, _path_as_of(state_path)) \
        if state_present else None
    receipt_as_of = None
    for record in reversed(records):
        receipt_as_of = _record_as_of(record)
        if receipt_as_of is not None:
            break
    if receipt_as_of is None and receipts_present:
        receipt_as_of = _path_as_of(receipts_path)
    recent = records[-RECENT_MOMENTUM_RECEIPTS:]

    results_present, raw_results, result_errors, results_truncated = \
        _read_jsonl_tail(
            results_path, MOMENTUM_RESULT_SCAN_RECORDS,
            scan_limit=MOMENTUM_RESULT_SCAN_RECORDS * 2)
    realized = []
    for record in raw_results:
        if not isinstance(record, dict):
            result_errors += 1
            continue
        if record.get("op") not in MOMENTUM_RESULT_OPS:
            continue
        if not isinstance(record.get("fills"), list) \
                or not isinstance(record.get("fees"), list) \
                or not isinstance(record.get("realized"), dict):
            result_errors += 1
            continue
        realized.append(record)
    realized = realized[-RECENT_MOMENTUM_RESULTS:]
    result_as_of = None
    for record in reversed(realized):
        result_as_of = _record_as_of(record)
        if result_as_of is not None:
            break
    if result_as_of is None and results_present:
        result_as_of = _path_as_of(results_path)
    return {
        "present": state_present or receipts_present or results_present,
        "as_of": _latest_as_of(
            (state_as_of, receipt_as_of, result_as_of)),
        "state": {
            "source": str(state_path), "present": state_present,
            "as_of": state_as_of, "state": state, "positions": positions,
            "parse_errors": state_errors,
        },
        "receipts": {
            "source": str(receipts_path), "present": receipts_present,
            "as_of": receipt_as_of, "count": len(records),
            "recent_count": len(recent), "records": recent,
            "parse_errors": receipt_errors,
        },
        "realized": {
            "source": str(results_path), "present": results_present,
            "as_of": result_as_of, "count": len(realized),
            "records": realized, "parse_errors": result_errors,
            "tail_truncated": results_truncated,
        },
        "parse_errors": state_errors + receipt_errors + result_errors,
    }


def api_fees(sources):
    path = sources["fees"]
    present, rates, parse_errors = _read_json(path)
    if present and rates is not None and not isinstance(rates, dict):
        rates = None
        parse_errors += 1
    return {
        "source": str(path),
        "present": present,
        "as_of": _record_as_of(rates, _path_as_of(path))
        if present else None,
        "rates": rates,
        "parse_errors": parse_errors,
    }


def _pnl_blocks(receipt):
    if not isinstance(receipt, dict):
        return []
    blocks = []
    for key in ("pnl", "realized_pnl"):
        if isinstance(receipt.get(key), dict):
            blocks.append(receipt[key])
    listed = receipt.get("realized_pnl_blocks")
    if isinstance(listed, list):
        blocks.extend(block for block in listed if isinstance(block, dict))
    for parent_key in ("close", "result", "reconcile"):
        parent = receipt.get(parent_key)
        if not isinstance(parent, dict):
            continue
        for key in ("pnl", "realized_pnl"):
            if isinstance(parent.get(key), dict):
                blocks.append(parent[key])
    unique, seen = [], set()
    for block in blocks:
        marker = json.dumps(block, sort_keys=True, default=str)
        if marker not in seen:
            seen.add(marker)
            unique.append(block)
    return unique


def _partial_fee_facts(block):
    keys = (
        "total_usdt_status", "fee_status", "partial_fees", "partial_fee",
        "fees_complete", "excluded_spot_fees_by_asset",
    )
    facts = {key: block[key] for key in keys if key in block}
    text = " ".join(str(value).upper() for value in facts.values())
    partial = bool(facts.get("excluded_spot_fees_by_asset")) \
        or facts.get("partial_fees") is True \
        or facts.get("partial_fee") is True \
        or facts.get("fees_complete") is False \
        or any(word in text for word in ("PARTIAL", "EXCLUDED", "UNKNOWN"))
    return facts, partial


def _is_closed_or_reconcile(receipt):
    verdict = str(receipt.get("verdict", "")).upper()
    state = str(receipt.get("state", "")).upper()
    kind = str(receipt.get("kind", "")).lower()
    return verdict == "CLOSED" or state == "CLOSED" or "reconcile" in kind


def _append_pnl_records(rows, name, receipt, file_as_of):
    if not isinstance(receipt, dict) or not _is_closed_or_reconcile(receipt):
        return
    as_of = _record_as_of(receipt, file_as_of)
    for block in _pnl_blocks(receipt):
        flags, partial = _partial_fee_facts(block)
        rows.append({
            "file": name,
            "run_id": receipt.get("run_id"),
            "symbol": receipt.get("symbol"),
            "kind": receipt.get("kind"),
            "verdict": receipt.get("verdict") or receipt.get("state"),
            "as_of": as_of,
            "pnl": block,
            "partial_fee_flags": flags,
            "partial_fees": partial,
        })


def api_pnl(sources):
    directory = sources["basis_receipts_dir"]
    position_path = sources["basis_position"]
    payload = {
        "source": str(directory), "position_source": str(position_path),
        "present": False, "as_of": None, "count": 0,
        "scanned_receipts": 0, "records": [], "parse_errors": 0,
    }
    as_of_values = []
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.endswith(".json")
                       and name.startswith(BASIS_RECEIPT_PREFIXES))
    except OSError:
        names = []
    else:
        payload["present"] = True
    payload["scanned_receipts"] = len(names)
    for name in names:
        path = directory / name
        present, receipt, errors = _read_json(path)
        file_as_of = _path_as_of(path) if present else None
        as_of_values.append(file_as_of)
        payload["parse_errors"] += errors
        if present and not errors:
            if not isinstance(receipt, dict):
                payload["parse_errors"] += 1
            else:
                _append_pnl_records(
                    payload["records"], name, receipt, file_as_of)

    state_present, position, position_errors = _read_json(position_path)
    if state_present:
        payload["present"] = True
        state_as_of = _path_as_of(position_path)
        as_of_values.append(state_as_of)
        payload["parse_errors"] += position_errors
        if not position_errors:
            if isinstance(position, dict):
                _append_pnl_records(
                    payload["records"], position_path.name, position,
                    state_as_of)
            else:
                payload["parse_errors"] += 1

    payload["records"].sort(
        key=lambda row: (str(row.get("as_of") or ""), row["file"]),
        reverse=True)
    payload["count"] = len(payload["records"])
    payload["as_of"] = _latest_as_of(
        [row.get("as_of") for row in payload["records"]] + as_of_values,
        _path_as_of(directory) if payload["present"] else None)
    return payload


def _reported_health_level(record):
    if not isinstance(record, dict):
        return None, None
    value = None
    for key in ("level", "health", "status", "state", "verdict"):
        if record.get(key) not in (None, ""):
            value = str(record[key])
            break
    if value is None:
        return None, None
    normalized = value.strip().upper()
    if normalized in {"RED", "ERROR", "FAILED", "FAIL", "DOWN", "HALTED",
                      "UNKNOWN", "REFUSED"} \
            or "ERROR" in normalized or "FAIL" in normalized \
            or "UNKNOWN" in normalized or "REFUSED" in normalized:
        return value, "red"
    if normalized in {"AMBER", "WARN", "WARNING", "DEGRADED", "STALE",
                      "BLOCKED", "BUSY", "STOPPED"} \
            or "WARN" in normalized or "DEGRADED" in normalized \
            or "STALE" in normalized or "BLOCKED" in normalized:
        return value, "amber"
    if normalized in {"GREEN", "OK", "HEALTHY", "RUNNING", "PASS", "IDLE",
                      "TICK"}:
        return value, "green"
    return value, None


def _health_thresholds(record, amber_default, red_default):
    cadence = None
    if isinstance(record, dict):
        for key in ("cadence_seconds", "interval_seconds", "tick_seconds"):
            cadence = _positive_seconds(record.get(key))
            if cadence is not None:
                break
    amber = None
    red = None
    if isinstance(record, dict):
        for key in ("amber_after_seconds", "stale_after_seconds",
                    "max_age_seconds"):
            amber = _positive_seconds(record.get(key))
            if amber is not None:
                break
        for key in ("red_after_seconds", "dead_after_seconds"):
            red = _positive_seconds(record.get(key))
            if red is not None:
                break
    if amber is None:
        amber = cadence * 2 if cadence is not None else amber_default
    if red is None:
        red = cadence * 5 if cadence is not None else red_default
    return amber, max(red, amber)


def _health_level(record, age, amber_default=HEALTH_AMBER_AFTER_SECONDS,
                  red_default=HEALTH_RED_AFTER_SECONDS):
    reported, explicit = _reported_health_level(record)
    amber_after, red_after = _health_thresholds(
        record, amber_default, red_default)
    if age is None:
        return explicit or "red", reported, "reported_status" \
            if explicit else "last_seen_unknown"
    if age > red_after:
        return "red", reported, "last_seen_age"
    if age > amber_after and explicit != "red":
        return "amber", reported, "last_seen_age"
    if explicit is not None:
        return explicit, reported, "reported_status"
    return "green", reported, "last_seen_age"


def _service_name(record):
    if not isinstance(record, dict):
        return None
    for key in ("service", "component", "lane", "name", "worker"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _jobs_log_summary(path, now, pull_age=None):
    present, lines, encoding_errors, truncated = _read_tail_lines(
        path, RECENT_JOB_LOG_LINES)
    if not present:
        return {
            "source": str(path), "present": False, "as_of": None,
            "last_tick_utc": None, "last_tick_age_seconds": None,
            "capture_age_seconds": None,
            "last_tick_basis": None, "level": None, "level_basis": None,
            "tail": [], "line_count": 0, "parse_errors": 0,
            "tail_truncated": False,
        }
    tick_utc = None
    tick_index = None
    for index in range(len(lines) - 1, -1, -1):
        match = UTC_IN_TEXT_RE.search(lines[index])
        if match and _parse_utc(match.group(0)) is not None:
            tick_utc = match.group(0)
            tick_index = index
            break
    file_as_of = _path_as_of(path)
    last_seen = tick_utc or file_as_of
    age = _age_seconds(last_seen, now)
    tick_lines = lines[tick_index:] if tick_index is not None else lines[-5:]
    text = " ".join(tick_lines).upper()
    reported = "OK"
    if any(word in text for word in (
            "UNKNOWN", "REFUSED", "HALTED", "TRACEBACK", " ERROR", "FAILED")):
        reported = "ERROR"
    elif any(word in text for word in ("WARNING", "WARN", "BUSY", "STOPPED")):
        reported = "WARNING"
    capture_age = _capture_age_seconds(age, pull_age)
    # Attribution: grade the runner on its own lag when the transport
    # term is known — the pull row carries transport staleness itself.
    level, _, basis = _health_level(
        {"status": reported},
        capture_age if capture_age is not None else age,
        JOB_AMBER_AFTER_SECONDS, JOB_RED_AFTER_SECONDS)
    return {
        "source": str(path), "present": True, "as_of": file_as_of,
        "last_tick_utc": last_seen, "last_tick_age_seconds": age,
        "capture_age_seconds": capture_age,
        "last_tick_basis": "log_timestamp" if tick_utc else "file_mtime",
        "level": level, "level_basis": basis, "tail": lines,
        "line_count": len(lines), "parse_errors": encoding_errors,
        "tail_truncated": truncated,
    }


def api_health(sources, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    path = sources["health"]
    present, raw_records, parse_errors, truncated = _read_jsonl_tail(
        path, RECENT_HEALTH_RECORDS)
    file_as_of = _path_as_of(path) if present else None
    latest = {}
    for record in raw_records:
        name = _service_name(record)
        if name is None:
            parse_errors += 1
            continue
        latest[name] = record

    # Transport term for attribution: these files arrive via the mirror
    # pull, so each service's observed age includes the pull's own age.
    pull_age = _strip_pull_facts(sources, now).get("age_seconds")

    services = []
    for name in sorted(latest):
        record = latest[name]
        record_as_of = _record_as_of(record)
        last_seen = record_as_of or file_as_of
        age = _age_seconds(last_seen, now)
        capture_age = _capture_age_seconds(age, pull_age)
        # Grade on the loop's OWN lag when attribution is possible — a
        # stale mirror pull must not paint every host service amber; the
        # pull row carries its own staleness.
        level, reported, basis = _health_level(
            record, capture_age if capture_age is not None else age)
        services.append({
            "service": name,
            "level": level,
            "level_basis": basis,
            "reported_status": reported,
            "last_seen_utc": last_seen,
            "last_seen_age_seconds": age,
            "capture_age_seconds": capture_age,
            "last_seen_basis": "record" if record_as_of else "file_mtime",
            "record": record,
        })

    jobs = _jobs_log_summary(sources["jobs_log"], now, pull_age)
    flags = {}
    for label, source_name in (("STOP", "stop"), ("STOP-JOBS", "stop_jobs")):
        flag_path = sources[source_name]
        flag_present = flag_path.is_file()
        flags[label] = {
            "source": str(flag_path),
            "present": flag_present,
            "as_of": _path_as_of(flag_path) if flag_present else None,
        }

    levels = [service["level"] for service in services]
    if jobs["level"] is not None:
        levels.append(jobs["level"])
    if any(flag["present"] for flag in flags.values()):
        levels.append("red")
    system_level = None
    if levels:
        system_level = "red" if "red" in levels else (
            "amber" if "amber" in levels else "green")
    as_of = _latest_as_of(
        [service["last_seen_utc"] for service in services]
        + [jobs["last_tick_utc"]]
        + [flag["as_of"] for flag in flags.values()],
        file_as_of)
    last_tick_ages = {
        service["service"]: service["last_seen_age_seconds"]
        for service in services
    }
    if jobs["present"]:
        last_tick_ages["jobs"] = jobs["last_tick_age_seconds"]
    return {
        "ok": True,
        "read_only": True,
        "bind": BIND_HOST,
        "methods": ["GET", "HEAD"],
        "source": str(path),
        "present": present or jobs["present"]
        or any(flag["present"] for flag in flags.values()),
        "as_of": as_of,
        "system_level": system_level,
        "service_count": len(services),
        "services": services,
        "last_tick_ages": last_tick_ages,
        "jobs_log": jobs,
        "flags": flags,
        "stop": flags["STOP"]["present"],
        "stop_jobs": flags["STOP-JOBS"]["present"],
        "parse_errors": parse_errors + jobs["parse_errors"],
        "tail_truncated": truncated,
        # Telemetry is presentation history served by its own endpoint, not
        # part of health's established operational-source inventory.
        "sources": {
            name: str(source) for name, source in sources.items()
            if name != "telemetry"
        },
    }


def api_decisions(sources):
    path = sources["momentum_decisions"]
    present, raw_records, parse_errors, truncated = _read_jsonl_tail(
        path, RECENT_DECISIONS)
    decisions = []
    for record in raw_records:
        if isinstance(record, dict):
            decisions.append(record)
        else:
            parse_errors += 1
    as_of = None
    for record in reversed(decisions):
        as_of = _record_as_of(record)
        if as_of is not None:
            break
    if as_of is None and present:
        as_of = _path_as_of(path)
    return {
        "source": str(path),
        "present": present,
        "as_of": as_of,
        "count": len(decisions),
        "decisions": decisions,
        "parse_errors": parse_errors,
        "tail_truncated": truncated,
    }


def api_news(sources):
    path = sources["news"]
    present, raw_records, parse_errors, truncated = _read_jsonl_tail(
        path, RECENT_NEWS)
    items = []
    for record in raw_records:
        if isinstance(record, dict) and isinstance(record.get("title"), str):
            items.append(record)
        else:
            parse_errors += 1
    items.reverse()  # file is ascending; serve newest first
    as_of = _path_as_of(path) if present else None
    board_path = sources["news_sources"]
    board = {"present": False, "available": 0, "live": 0, "sources": []}
    try:
        raw_board = json.loads(board_path.read_text(encoding="utf-8"))
        if isinstance(raw_board, dict):
            rows = raw_board.get("sources")
            board = {
                "present": True,
                "as_of": raw_board.get("as_of"),
                "available": raw_board.get("available", 0),
                "live": raw_board.get("live", 0),
                "sources": rows if isinstance(rows, list) else [],
            }
    except (OSError, ValueError):
        pass
    return {
        "source": str(path),
        "present": present,
        "as_of": as_of,
        "count": len(items),
        "items": items,
        "sources_board": board,
        "parse_errors": parse_errors,
        "tail_truncated": truncated,
    }


TELEMETRY_TAIL_BYTES = 16 * 1024 * 1024


# Attribution: a host loop's observed age on this Mac = transport age
# (mirror pull) + the loop's REAL lag at the moment the mirror snapshotted
# the host. The raw *_s fields conflate the two, which paints the pull
# cycle's sawtooth onto every host loop. The derived *_true_s fields carry
# the loop's own truth; mirror_s alone carries the transport.
TRUE_LAG_FIELDS = {
    "market_s": "market_true_s",
    "supervisor_s": "supervisor_true_s",
    "basis_watch_s": "basis_watch_true_s",
    "basis_sup_s": "basis_sup_true_s",
    "jobs_s": "jobs_true_s",
}
TRUE_LAG_MIRROR_CARRY_SECONDS = 240

# Beat representation: every loop's graphable truth is the interval
# between its real works (write timestamps recovered as sample_ts - age,
# which stay constant between pulls). Healthy = a flat line at the loop's
# own rhythm; a late loop's line RISES with its real silence; a stalled
# transport freezes the view (None) instead of faking lateness.
BEAT_FIELDS = {
    "mirror_s": ("mirror_beat_s", False),
    "news_s": ("news_beat_s", False),
    "market_s": ("market_beat_s", True),
    "supervisor_s": ("supervisor_beat_s", True),
    "basis_watch_s": ("basis_watch_beat_s", True),
    "basis_sup_s": ("basis_sup_beat_s", True),
    "jobs_s": ("jobs_beat_s", True),
}
BEAT_NEW_WRITE_SECONDS = 2.5


def _capture_age_seconds(age, pull_age):
    """Loop lag at the mirror's snapshot instant: observed minus transport,
    floored at zero (host-vs-Mac clock skew appears as a constant; the
    floor keeps the number honest rather than negative)."""
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        return None
    if isinstance(pull_age, bool) or not isinstance(pull_age, (int, float)):
        return None
    return round(max(0.0, float(age) - float(pull_age)), 3)


def _derive_true_lags(records):
    """Add *_true_s to chronological telemetry rows. mirror_s is carried
    forward briefly (growing by wall clock, as a manifest age does) so
    sparse loop samples still receive their transport term; past the carry
    window the derived field is honestly None."""
    last_mirror = None
    last_mirror_ts = None
    beat_state = {}
    for record in records:
        ts = _parse_utc(record.get("ts"))
        mirror = record.get("mirror_s")
        if (ts is not None and not isinstance(mirror, bool)
                and isinstance(mirror, (int, float))):
            last_mirror = float(mirror)
            last_mirror_ts = ts
        carried = None
        if ts is not None and last_mirror_ts is not None:
            gap = (ts - last_mirror_ts).total_seconds()
            if 0 <= gap <= TRUE_LAG_MIRROR_CARRY_SECONDS:
                carried = last_mirror + gap
        for raw_key, true_key in TRUE_LAG_FIELDS.items():
            if raw_key not in record:
                continue
            value = record.get(raw_key)
            if (carried is not None and not isinstance(value, bool)
                    and isinstance(value, (int, float))):
                record[true_key] = _capture_age_seconds(value, carried)
            else:
                record[true_key] = None
        _derive_beats(record, ts, beat_state)


def _derive_beats(record, ts, state):
    """Add *_beat_s: max(last completed beat, current open interval).
    Write timestamps derive from sample_ts - age and are pull-phase
    stable; a new write farther than BEAT_NEW_WRITE_SECONDS advances the
    beat. Warm-up (no beat yet) and frozen-transport rows stay None."""
    for raw_key, (out_key, hosted) in BEAT_FIELDS.items():
        if raw_key not in record:
            continue
        value = record.get(raw_key)
        if (ts is None or isinstance(value, bool)
                or not isinstance(value, (int, float))):
            record[out_key] = None
            continue
        write_ts = ts - timedelta(seconds=float(value))
        st = state.setdefault(raw_key, {"write": None, "beat": None})
        if st["write"] is None:
            st["write"] = write_ts
        else:
            delta = (write_ts - st["write"]).total_seconds()
            if delta > BEAT_NEW_WRITE_SECONDS:
                st["beat"] = delta
                st["write"] = write_ts
            elif delta < -BEAT_NEW_WRITE_SECONDS:
                # rotation/out-of-order artifact: re-anchor, keep the beat
                st["write"] = write_ts
        if hosted:
            open_s = record.get(TRUE_LAG_FIELDS[raw_key])
        else:
            open_s = float(value)
        if st["beat"] is None or open_s is None:
            record[out_key] = None
        else:
            record[out_key] = round(max(st["beat"], open_s), 3)


def api_telemetry(sources, range_name="1h", now=None):
    path = sources["telemetry"]
    if range_name not in TELEMETRY_RANGE_HOURS:
        range_name = "1h"
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_start = now - timedelta(hours=TELEMETRY_RANGE_HOURS[range_name])

    # _read_jsonl_tail bounds the byte read with TAIL_BYTES first. The same
    # value as its line cap retains every non-empty line that can fit in that
    # bounded tail without ever slurping an indefinitely growing source.
    present, raw_records, parse_errors, tail_truncated = _read_jsonl_tail(
        path, TELEMETRY_TAIL_BYTES, scan_limit=TELEMETRY_TAIL_BYTES)
    kept = []
    for record in raw_records:
        if not isinstance(record, dict):
            parse_errors += 1
            continue
        parsed = _parse_utc(record.get("ts"))
        if parsed is None:
            parse_errors += 1
            continue
        if window_start <= parsed <= now:
            kept.append((parsed, record))
    kept.sort(key=lambda item: item[0])

    # dict copies: the jsonl tail is cached; derived fields must never
    # leak into the shared cache rows.
    records = [dict(record) for _, record in kept]
    _derive_true_lags(records)
    kept_count = len(records)
    truncated = tail_truncated
    stride = None
    if kept_count > TELEMETRY_MAX_POINTS:
        # Audit fix: whole-row striding aliased sparse per-metric rows
        # (a 15s field could vanish entirely for a phase). Merge each
        # bucket instead: the last non-null value per field survives,
        # so every metric keeps its presence at display resolution.
        stride = (kept_count + TELEMETRY_MAX_POINTS - 1) \
            // TELEMETRY_MAX_POINTS
        merged = []
        for start in range(0, kept_count, stride):
            chunk = records[start:start + stride]
            row = {"ts": chunk[-1]["ts"]}
            for record in chunk:
                for key, value in record.items():
                    if key == "ts":
                        continue
                    if value is not None:
                        row[key] = value
                    elif key not in row:
                        row[key] = None
            merged.append(row)
        records = merged
        truncated = True

    payload = {
        "ok": True,
        "read_only": True,
        "source": str(path),
        "present": present,
        "range": range_name,
        "as_of": records[-1]["ts"] if records else None,
        "count": kept_count,
        "parse_errors": parse_errors,
        "truncated": truncated,
        "points": records,
    }
    if stride is not None:
        payload["stride"] = stride
    return payload


def _failure_verdict(record):
    if not isinstance(record, dict):
        return None
    value = record.get("verdict")
    if not isinstance(value, str):
        return None
    verdict = value.strip().upper()
    if verdict == "UNKNOWN" or "REFUSED" in verdict:
        return verdict
    return None


def _recorded_reason(record):
    if not isinstance(record, dict):
        return None
    for key in ("reason", "verdict_note", "error", "detail", "note",
                "exposure"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("first_failure", "result", "outcome"):
        nested = record.get(key)
        if isinstance(nested, dict):
            reason = _recorded_reason(nested)
            if reason is not None:
                return reason
    return None


def _runner_receipt_reason(directory, job_id):
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return None, 0
    present, receipt, errors = _read_json(directory / ("job-%s.json" % job_id))
    if not present:
        return None, 0
    if errors or not isinstance(receipt, dict) \
            or receipt.get("job_id") != job_id:
        return None, errors + (0 if errors else 1)
    return _recorded_reason(receipt), 0


def _contained_receipt_reason(directory, raw_path):
    """Read an explicitly named receipt only when it stays under its source."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, 0
    try:
        root = directory.resolve()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = directory / candidate
        candidate = candidate.resolve()
    except OSError:
        return None, 0
    if candidate.suffix != ".json" \
            or (candidate != root and root not in candidate.parents):
        return None, 0
    present, receipt, errors = _read_json(candidate)
    if not present:
        return None, 0
    if errors or not isinstance(receipt, dict):
        return None, errors + (0 if errors else 1)
    return _recorded_reason(receipt), 0


def api_failures(sources):
    path = sources["job_results"]
    receipts_dir = sources["job_receipts_dir"]
    present, raw_records, parse_errors, truncated = _read_jsonl_tail(
        path, FAILURE_SCAN_RECORDS, scan_limit=FAILURE_SCAN_RECORDS * 2)
    failures = []
    for record in raw_records:
        verdict = _failure_verdict(record)
        if verdict is None:
            if not isinstance(record, dict):
                parse_errors += 1
            continue
        failure = dict(record)
        reason = _recorded_reason(record)
        reason_source = "result" if reason is not None else None
        if reason is None:
            reason, errors = _runner_receipt_reason(
                receipts_dir, record.get("job_id"))
            parse_errors += errors
            if reason is not None:
                reason_source = "runner_receipt"
        if reason is None:
            reason, errors = _contained_receipt_reason(
                sources["basis_receipts_dir"], record.get("receipt_path"))
            parse_errors += errors
            if reason is not None:
                reason_source = "receipt_path"
        failure["verdict"] = verdict
        failure["reason"] = reason
        failure["reason_source"] = reason_source
        failure["as_of"] = _record_as_of(record)
        failures.append(failure)
    failures = failures[-RECENT_FAILURES:]
    source_as_of = _path_as_of(path) if present else None
    as_of = None
    for failure in reversed(failures):
        as_of = failure.get("as_of")
        if as_of is not None:
            break
    return {
        "source": str(path),
        "reason_source": str(receipts_dir),
        "present": present,
        "reason_source_present": receipts_dir.is_dir(),
        "as_of": as_of or source_as_of,
        "count": len(failures),
        "failures": failures,
        "parse_errors": parse_errors,
        "tail_truncated": truncated,
    }


def _strip_pull_facts(sources, now):
    """Distill pull freshness from the mirror manifest, fail-closed."""
    path = Path(sources.get("manifest", LIVE_DIR / "pull_manifest.json"))
    present, manifest, parse_errors = _read_json(path)
    utc = ok = error = cycle_ms = interval_ms = None
    if present and isinstance(manifest, dict):
        if isinstance(manifest.get("utc"), str):
            utc = manifest["utc"]
        if isinstance(manifest.get("ok"), bool):
            ok = manifest["ok"]
        if isinstance(manifest.get("error"), str):
            error = manifest["error"]
        if isinstance(manifest.get("cycle_ms"), (int, float)):
            cycle_ms = manifest["cycle_ms"]
        if isinstance(manifest.get("interval_ms"), (int, float)):
            interval_ms = manifest["interval_ms"]
    elif present:
        parse_errors += 1
    age = _age_seconds(utc, now)
    # The mirror declares its loop cadence via interval_ms (host ~5s, Mac
    # pull ~30-60s); thresholds adapt so a healthy slow cycle never reads
    # amber. cycle_ms is accepted only when it plausibly IS an interval —
    # some writers record the copy duration there (tens of ms).
    amber_after = STRIP_PULL_AMBER_AFTER_SECONDS
    red_after = STRIP_PULL_RED_AFTER_SECONDS
    cadence_ms = interval_ms
    if cadence_ms is None and isinstance(cycle_ms, (int, float)) \
            and cycle_ms >= 5000:
        cadence_ms = cycle_ms
    if isinstance(cadence_ms, (int, float)) and cadence_ms > 0:
        cadence_seconds = cadence_ms / 1000.0
        amber_after = max(amber_after, cadence_seconds * 2)
        red_after = max(red_after, cadence_seconds * 4)
    if not present:
        level = "absent"
    elif ok is False or (present and manifest is None):
        level = "red"
    elif age is None:
        level = "amber"
    elif age > red_after:
        level = "red"
    elif age > amber_after:
        level = "amber"
    else:
        level = "green"
    return {
        "source": str(path),
        "present": present,
        "ok": ok,
        "utc": utc,
        "age_seconds": age,
        "cycle_ms": cycle_ms,
        "interval_ms": interval_ms,
        "error": error,
        "level": level,
        "parse_errors": parse_errors,
    }


def _strip_governor_facts(sources, now):
    """Read today's governor day ledger; the ledger rows are the only truth."""
    day = now.date().isoformat()
    ledger_dir = Path(sources.get("governor_dir", LIVE_DIR))
    path = ledger_dir / (GOVERNOR_LEDGER_PREFIX + day + ".jsonl")
    present, rows, parse_errors = _read_jsonl(path)
    entries_used = 0
    loss_fees_used = Decimal("0")
    for row in rows:
        if not isinstance(row, dict):
            parse_errors += 1
            continue
        kind = row.get("kind")
        if kind == "entry":
            entries_used += 1
        elif kind == "realized":
            try:
                net = Decimal(str(row.get("net_usdt", "0")))
                fees = Decimal(str(row.get("fees_usdt", "0")))
            except (InvalidOperation, ValueError):
                parse_errors += 1
                continue
            loss_fees_used += fees
            if net < 0:
                loss_fees_used += -net
    lock_path = Path(sources.get(
        "governor_lock", LIVE_DIR / "account-governor.lock"))
    lock_present = lock_path.is_file()
    remaining = GOVERNOR_LOSS_FEES_CAP - loss_fees_used
    return {
        "source": str(path),
        "present": present,
        "utc_day": day,
        "entries_used": entries_used,
        "entries_cap": GOVERNOR_ENTRIES_CAP,
        "loss_fees_used_usdt": format(loss_fees_used, "f"),
        "loss_fees_cap_usdt": format(GOVERNOR_LOSS_FEES_CAP, "f"),
        "loss_fees_remaining_usdt": format(remaining, "f"),
        "caps_basis": "display mirror of governor defaults",
        "entry_lock_present": lock_present,
        "parse_errors": parse_errors,
    }


def api_strip(sources, now=None):
    """One-glance machine state distilled from the pulled mirror only."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    pull = _strip_pull_facts(sources, now)
    stop_present = Path(sources["stop"]).is_file()
    stop_jobs_present = Path(sources["stop_jobs"]).is_file()
    # Mirrored flag absence is meaningless before the first completed pull:
    # claiming ARMED off an empty mirror would be a fabricated state.
    if not pull["present"]:
        machine = "NO-DATA"
    elif stop_present:
        machine = "PAUSED"
    else:
        machine = "ENTRY-SEALED" if stop_jobs_present else "ARMED"

    state_path = Path(sources["stream_state"])
    state_present, state, _ = _read_json(state_path)
    market_as_of = _record_as_of(state) if state_present else None
    if market_as_of is None and state_present:
        market_as_of = _path_as_of(state_path)
    market_age = _age_seconds(market_as_of, now)
    # While the machine is paused the feed is sealed by design: that is a
    # state to display, never a blank. Before the first pull, no claim.
    if state_present:
        market_state = "live"
    elif machine == "PAUSED":
        market_state = "sealed"
    elif machine == "NO-DATA":
        market_state = "no-data"
    else:
        market_state = "absent"

    health = api_health(sources, now=now)
    supervisor = None
    for service in health.get("services", []):
        if service.get("service") == "basis_supervisor":
            supervisor = {
                "level": service.get("level"),
                "reported_status": service.get("reported_status"),
                "last_seen_utc": service.get("last_seen_utc"),
                "age_seconds": service.get("last_seen_age_seconds"),
                "capture_age_seconds": service.get("capture_age_seconds"),
            }
            break

    return {
        "ok": True,
        "read_only": True,
        "as_of": now.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"),
        "machine": machine,
        "stop_present": stop_present,
        "stop_jobs_present": stop_jobs_present,
        "paused_note": (
            "machine paused — positions and P&L stay empty; market passes "
            "may be sealed too" if machine == "PAUSED" else None),
        "market_pass": {
            "state": market_state,
            "as_of": market_as_of,
            "age_seconds": market_age,
            "capture_age_seconds": _capture_age_seconds(
                market_age, pull.get("age_seconds")),
        },
        "supervisor": supervisor,
        "governor": _strip_governor_facts(sources, now),
        "pull": pull,
    }


POLL_WRITE_LOCK = threading.Lock()
POLL_METRICS = ("server", "mirror", "market", "supervisor", "newsloop",
                "basiswatch", "basissup", "jobrunner")
POLL_ALLOWED = (1, 2, 5, 10, 15, 30, 60, 120)
POLL_DEFAULT = 15


def _read_poll_config(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}, False
    return (payload if isinstance(payload, dict) else {}), True


def _resolve_poll_config(payload):
    default = payload.get("default_s")
    if isinstance(default, bool) or default not in POLL_ALLOWED:
        default = POLL_DEFAULT
    metrics_in = payload.get("metrics")
    metrics_in = metrics_in if isinstance(metrics_in, dict) else {}
    metrics = {}
    for key in POLL_METRICS:
        value = metrics_in.get(key)
        if isinstance(value, bool):
            value = None
        metrics[key] = value if value in POLL_ALLOWED else default
    return {"default_s": default, "metrics": metrics}


def api_poll_config(sources, now=None):
    payload, present = _read_poll_config(sources["poll_config"])
    resolved = _resolve_poll_config(payload)
    return {
        "ok": True,
        "present": present,
        "default_s": resolved["default_s"],
        "metrics": resolved["metrics"],
        "allowed": list(POLL_ALLOWED),
    }


def write_poll_config(sources, body):
    """Validate and atomically write the recorder's poll config.

    The ONLY mutation this server performs: a UI-local preferences file
    (ui/live/poll_config.json) that the telemetry recorder reads. It can
    not reach the host, the machine, or any money path.
    """
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    with POLL_WRITE_LOCK:
        return _write_poll_config_locked(sources, body)


def _write_poll_config_locked(sources, body):
    current, _ = _read_poll_config(sources["poll_config"])
    resolved = _resolve_poll_config(current)
    default = body.get("default_s", resolved["default_s"])
    if isinstance(default, bool) or default not in POLL_ALLOWED:
        return None, "default_s must be one of %s" % (list(POLL_ALLOWED),)
    metrics_in = body.get("metrics", {})
    if not isinstance(metrics_in, dict):
        return None, "metrics must be an object"
    merged = dict(resolved["metrics"])
    for key, value in metrics_in.items():
        if key not in POLL_METRICS:
            return None, "unknown metric %r" % (key,)
        if isinstance(value, bool) or value not in POLL_ALLOWED:
            return None, "interval for %r must be one of %s" % (
                key, list(POLL_ALLOWED))
        merged[key] = value
    output = {"default_s": default, "metrics": merged}
    target = Path(sources["poll_config"])
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".%s-" % target.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            json.dump(output, tmp, ensure_ascii=False, separators=(",", ":"))
            tmp.flush()
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return output, None


# ---- per-loop grace (V5): UI grading preference, its own file --------
# Kept OUT of poll_config.json so the telemetry recorder (which reads
# that file) never sees an unknown key. Same read/merge/atomic-write
# discipline as the poll config.

GRACE_WRITE_LOCK = threading.Lock()
GRACE_ALLOWED = (15, 30, 60, 120, 300, 600)


def _read_grace(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}, False
    return (payload if isinstance(payload, dict) else {}), True


def _resolve_grace(payload):
    raw = payload.get("grace_s")
    raw = raw if isinstance(raw, dict) else {}
    resolved = {}
    for key in POLL_METRICS:
        value = raw.get(key)
        if isinstance(value, bool):
            value = None
        resolved[key] = value if value in GRACE_ALLOWED else "auto"
    return resolved


def api_grace(sources, now=None):
    payload, present = _read_grace(sources["grace"])
    return {
        "ok": True,
        "present": present,
        "grace_s": _resolve_grace(payload),
        "allowed": list(GRACE_ALLOWED),
    }


def write_grace(sources, body):
    """Validate and atomically write the per-loop grace preferences."""
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    with GRACE_WRITE_LOCK:
        return _write_grace_locked(sources, body)


def _write_grace_locked(sources, body):
    current, _ = _read_grace(sources["grace"])
    merged = _resolve_grace(current)
    grace_in = body.get("grace_s", {})
    if not isinstance(grace_in, dict):
        return None, "grace_s must be an object"
    for key, value in grace_in.items():
        if key not in POLL_METRICS:
            return None, "unknown metric %r" % (key,)
        if value == "auto":
            merged[key] = "auto"
            continue
        if isinstance(value, bool) or value not in GRACE_ALLOWED:
            return None, "grace for %r must be 'auto' or one of %s" % (
                key, list(GRACE_ALLOWED))
        merged[key] = value
    output = {"grace_s": merged}
    target = Path(sources["grace"])
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".%s-" % target.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            json.dump(output, tmp, ensure_ascii=False, separators=(",", ":"))
            tmp.flush()
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return output, None


API_ROUTES = {
    "/api/strip": api_strip,
    "/api/health": api_health,
    "/api/decisions": api_decisions,
    "/api/failures": api_failures,
    "/api/events": api_events,
    "/api/stream": api_stream,
    "/api/candidates": api_candidates,
    "/api/position": api_position,
    "/api/momentum": api_momentum,
    "/api/fees": api_fees,
    "/api/pnl": api_pnl,
    "/api/news": api_news,
    "/api/telemetry": api_telemetry,
    "/api/poll_config": api_poll_config,
    "/api/grace": api_grace,
}


def resolve_static(url_path):
    """Map a URL path to a file inside ui/, or None. Traversal is refused."""
    try:
        clean = unquote(urlsplit(url_path).path)
    except ValueError:
        return None
    if clean in ("", "/"):
        clean = "/index.html"
    parts = [part for part in clean.split("/") if part]
    if any(part in ("..", ".") for part in parts):
        return None
    candidate = UI_DIR.joinpath(*parts)
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None
    if UI_DIR not in resolved.parents and resolved != UI_DIR:
        return None
    if not resolved.is_file():
        return None
    return resolved


class ReadOnlyHandler(BaseHTTPRequestHandler):
    server_version = "QuantReadOnlyUI/2"
    sys_version = ""
    sources = None
    quiet = True
    timeout = REQUEST_TIMEOUT_SECONDS

    @staticmethod
    def _compressible(content_type):
        media_type = content_type.split(";", 1)[0].strip().lower()
        return media_type in {
            "application/json", "text/html", "text/css",
            "text/javascript", "application/javascript",
        }

    def _accepts_gzip(self):
        header = self.headers.get("Accept-Encoding", "")
        qualities = {}
        for item in header.split(","):
            fields = [field.strip() for field in item.split(";")]
            coding = fields[0].lower()
            if not coding:
                continue
            quality = 1.0
            for parameter in fields[1:]:
                name, separator, raw_value = parameter.partition("=")
                if separator and name.strip().lower() == "q":
                    try:
                        quality = float(raw_value.strip())
                    except ValueError:
                        quality = 0.0
            qualities[coding] = quality
        if "gzip" in qualities:
            return qualities["gzip"] > 0
        return qualities.get("*", 0) > 0

    @staticmethod
    def _strong_etag(body, prefix=b""):
        digest = hashlib.sha256()
        digest.update(prefix)
        digest.update(body)
        return '"%s"' % digest.hexdigest()

    def _etag_matches(self, etag):
        supplied = self.headers.get("If-None-Match")
        if supplied is None:
            return None
        for candidate in supplied.split(","):
            candidate = candidate.strip()
            if candidate == "*":
                return True
            if candidate.startswith("W/"):
                candidate = candidate[2:].strip()
            if candidate == etag:
                return True
        return False

    def _static_not_modified(self, modified):
        supplied = self.headers.get("If-Modified-Since")
        if supplied is None:
            return False
        try:
            parsed = parsedate_to_datetime(supplied)
        except (TypeError, ValueError, OverflowError):
            return False
        if parsed is None:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            return int(modified) <= int(parsed.timestamp())
        except (OSError, OverflowError, ValueError):
            return False

    def end_headers(self):
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def version_string(self):
        return self.server_version

    def _send(self, status, content_type, body, head_only=False,
              cache_control="no-cache", etag_prefix=None,
              last_modified=None, extra_headers=None):
        compressible = self._compressible(content_type)
        encoded = body
        content_encoding = None
        if compressible and len(body) > GZIP_MIN_BYTES \
                and self._accepts_gzip():
            encoded = gzip.compress(body, compresslevel=6, mtime=0)
            content_encoding = "gzip"

        etag = None
        if status == 200:
            etag = self._strong_etag(encoded, etag_prefix or b"")
            etag_match = self._etag_matches(etag)
            not_modified = etag_match is True
            if etag_match is None and last_modified is not None:
                not_modified = self._static_not_modified(last_modified[1])
            if not_modified:
                status = 304
                encoded = b""
                content_encoding = None

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", cache_control)
        if etag is not None:
            self.send_header("ETag", etag)
        if last_modified is not None:
            self.send_header("Last-Modified", last_modified[0])
        if compressible:
            self.send_header("Vary", "Accept-Encoding")
        if content_encoding is not None:
            self.send_header("Content-Encoding", content_encoding)
        for name, value in (extra_headers or ()):
            self.send_header(name, value)
        self.end_headers()
        if not head_only and status != 304:
            self.wfile.write(encoded)

    def _send_json(self, status, payload, head_only=False,
                   cache_control="no-cache", extra_headers=None):
        body = json.dumps(payload, indent=1).encode("utf-8")
        self._send(
            status, "application/json; charset=utf-8", body, head_only,
            cache_control=cache_control, etag_prefix=b"",
            extra_headers=extra_headers)

    def _handle(self, head_only):
        try:
            parsed_url = urlsplit(self.path)
            path = unquote(parsed_url.path)
        except ValueError:
            self._send(
                404, "text/plain; charset=utf-8", b"not found\n",
                head_only, cache_control="no-cache")
            return
        route = API_ROUTES.get(path)
        if route is not None:
            if route is api_telemetry:
                query = parse_qs(parsed_url.query, keep_blank_values=True)
                range_name = query.get("range", ["1h"])[0]
                payload = route(type(self).sources, range_name)
            else:
                payload = route(type(self).sources)
            self._send_json(
                200, payload, head_only,
                cache_control="no-cache")
            return
        if path.startswith("/api/"):
            self._send_json(
                404, {"error": "not found", "path": path}, head_only,
                cache_control="no-cache")
            return
        static = resolve_static(self.path)
        if static is None:
            self._send(
                404, "text/plain; charset=utf-8", b"not found\n",
                head_only, cache_control="no-cache")
            return
        content_type = CONTENT_TYPES.get(
            static.suffix, "application/octet-stream")
        present, body, identity, modified = _read_static(static)
        if not present:
            self._send(
                404, "text/plain; charset=utf-8", b"not found\n",
                head_only, cache_control="no-cache")
            return
        identity_bytes = ("static:%d:%d:%d:" % identity).encode("ascii")
        # no-cache (not no-store): the browser must revalidate every
        # request, and the strong ETag turns unchanged files into cheap
        # 304s. A max-age here made every design iteration serve stale
        # HTML/JS for up to 60s, which repeatedly misled live review.
        self._send(
            200, content_type, body, head_only,
            cache_control="no-cache", etag_prefix=identity_bytes,
            last_modified=(formatdate(modified, usegmt=True), modified))

    def do_GET(self):
        self._handle(head_only=False)

    def do_HEAD(self):
        self._handle(head_only=True)

    def _refuse(self):
        self._send_json(405, {
            "error": "read-only server: GET and HEAD only; "
                     "no mutating endpoint exists",
            "method": self.command,
        }, head_only=self.command == "HEAD",
                        extra_headers=(("Allow", "GET, HEAD"),))

    def do_POST(self):
        """Two narrow writes: recorder poll config, V5 grace prefs."""
        clean = urlsplit(self.path).path
        writers = {
            "/api/poll_config": write_poll_config,
            "/api/grace": write_grace,
        }
        writer = writers.get(clean)
        if writer is None:
            self._refuse()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 1 or length > 4096:
            self._send_json(400, {"ok": False,
                                  "error": "body must be 1-4096 bytes"})
            return
        raw_body = self.rfile.read(length)
        if len(raw_body) != length:
            self._send_json(400, {"ok": False, "error": "short body"})
            return
        try:
            body = json.loads(raw_body)
        except (ValueError, UnicodeError):
            self._send_json(400, {"ok": False,
                                  "error": "body must be valid JSON"})
            return
        try:
            saved, error = writer(type(self).sources, body)
        except OSError as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if error:
            self._send_json(400, {"ok": False, "error": error})
            return
        self._send_json(200, {"ok": True, "saved": saved})

    do_PUT = _refuse
    do_DELETE = _refuse
    do_PATCH = _refuse
    do_OPTIONS = _refuse
    do_CONNECT = _refuse
    do_TRACE = _refuse

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._refuse
        raise AttributeError(name)

    def log_message(self, format, *args):
        if not type(self).quiet:
            super().log_message(format, *args)


def make_server(sources=None, port=DEFAULT_PORT, quiet=True):
    """Build the loopback-only server; the caller owns its lifecycle."""
    resolved = dict(default_sources())
    if sources:
        resolved.update(sources)
    handler = type("BoundHandler", (ReadOnlyHandler,), {
        "sources": resolved, "quiet": quiet,
    })
    server = ThreadingHTTPServer((BIND_HOST, port), handler)
    server.daemon_threads = True
    return server


def host_check_sources(endpoint_sources):
    """Add canonical runtime sidecars that are not HTTP response sources."""
    runtime = resolve_runtime_paths()
    sources = dict(endpoint_sources)
    sources.update({
        "stream_windows": runtime.stream_windows,
        "basis_watch_state": runtime.basis_watch_state,
        "momentum_lifecycle_state": runtime.momentum_state,
    })
    return sources


def host_check_rows(sources, now=None):
    """Describe source presence and mtime age without opening or creating it."""
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    checked_at = checked_at.astimezone(timezone.utc)
    checked_utc = checked_at.isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    rows = []
    for name in sorted(sources):
        path = Path(sources[name])
        try:
            facts = path.stat()
        except FileNotFoundError:
            exists, kind, age, modified = \
                False, "missing", None, None
            status = "absent"
        except OSError:
            exists, kind, age, modified = \
                None, "unknown", None, None
            status = "unreadable"
        else:
            exists, status = True, "present"
            if stat_module.S_ISREG(facts.st_mode):
                kind = "file"
            elif stat_module.S_ISDIR(facts.st_mode):
                kind = "directory"
            else:
                kind = "other"
            elapsed = checked_at.timestamp() - facts.st_mtime
            age = round(elapsed, 3) if elapsed >= 0 else None
            modified = datetime.fromtimestamp(
                facts.st_mtime, timezone.utc).isoformat(
                    timespec="milliseconds").replace("+00:00", "Z")
        rows.append({
            "source": name,
            "path": str(path),
            "checked_utc": checked_utc,
            "status": status,
            "exists": exists,
            "kind": kind,
            "modified_utc": modified,
            "age_seconds": age,
        })
    return rows


def print_host_check(sources, output=None, now=None):
    """Print stable JSONL suitable for host deployment evidence."""
    output = output or sys.stdout
    for row in host_check_rows(sources, now=now):
        print(json.dumps(row, sort_keys=True), file=output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="port on 127.0.0.1 (0 = OS-assigned, printed)")
    parser.add_argument("--events-file", type=Path, default=None,
                        help="historical spot event JSONL")
    parser.add_argument("--stream-state", type=Path, default=None)
    parser.add_argument("--stream-log", type=Path, default=None)
    parser.add_argument("--proposals-dir", type=Path, default=None)
    parser.add_argument("--basis-position", type=Path, default=None)
    parser.add_argument("--basis-receipts-dir", type=Path, default=None)
    parser.add_argument("--fees-file", type=Path, default=None)
    parser.add_argument("--momentum-state", type=Path, default=None)
    parser.add_argument("--momentum-receipts", type=Path, default=None)
    parser.add_argument("--health-file", type=Path, default=None)
    parser.add_argument("--jobs-log", type=Path, default=None)
    parser.add_argument("--stop-file", type=Path, default=None)
    parser.add_argument("--stop-jobs-file", type=Path, default=None)
    parser.add_argument("--momentum-decisions", type=Path, default=None)
    parser.add_argument("--job-results", type=Path, default=None)
    parser.add_argument("--job-receipts-dir", type=Path, default=None)
    parser.add_argument(
        "--host-check", action="store_true",
        help="print runtime source existence and mtime ages, then exit")
    logging_group = parser.add_mutually_exclusive_group()
    logging_group.add_argument(
        "--quiet", dest="quiet", action="store_true", default=True,
        help="suppress per-request access logging (default)")
    logging_group.add_argument(
        "--verbose", dest="quiet", action="store_false",
        help="write per-request access logging to stderr")
    args = parser.parse_args(argv)

    overrides = {}
    for argument, source in (
            (args.events_file, "events"),
            (args.stream_state, "stream_state"),
            (args.stream_log, "stream_log"),
            (args.proposals_dir, "proposals_dir"),
            (args.basis_position, "basis_position"),
            (args.basis_receipts_dir, "basis_receipts_dir"),
            (args.fees_file, "fees"),
            (args.momentum_state, "momentum_state"),
            (args.momentum_receipts, "momentum_receipts"),
            (args.health_file, "health"),
            (args.jobs_log, "jobs_log"),
            (args.stop_file, "stop"),
            (args.stop_jobs_file, "stop_jobs"),
            (args.momentum_decisions, "momentum_decisions"),
            (args.job_results, "job_results"),
            (args.job_receipts_dir, "job_receipts_dir")):
        if argument is not None:
            overrides[source] = argument

    resolved_sources = dict(default_sources())
    resolved_sources.update(overrides)
    if args.host_check:
        print_host_check(host_check_sources(resolved_sources))
        return 0

    server = make_server(resolved_sources, args.port, quiet=args.quiet)
    host, port = server.server_address[0], server.server_address[1]
    print("serving quant ui (read-only, GET/HEAD only) on http://%s:%d/"
          % (host, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
