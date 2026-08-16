from __future__ import annotations

import calendar
import json
from datetime import datetime, timezone
from pathlib import Path


_COUNTER_FIELDS = ("successful_handshakes", "handshake_failures", "reconnects")


def _read_object(path: str):
    try:
        with Path(path).open("r", encoding="utf-8") as source:
            payload = json.load(source)
    except (OSError, UnicodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _integer(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value):
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _iso_ms(value):
    text = _text(value)
    if text is None:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    utc = parsed.astimezone(timezone.utc)
    return calendar.timegm(utc.utctimetuple()) * 1000 + utc.microsecond // 1000


def _age_ms(now_ms, observed_ms):
    return None if observed_ms is None else now_ms - observed_ms


def _age_s(now_ms, observed_ms):
    age = _age_ms(now_ms, observed_ms)
    return None if age is None else age / 1000.0


def _rows(value):
    return (
        [row for row in value if isinstance(row, dict)]
        if isinstance(value, list) else []
    )


def _connection_rows(venue):
    direct = _rows(venue.get("shards"))
    ticker = venue.get("ticker")
    if not isinstance(ticker, dict):
        return direct
    ticker_shards = _rows(ticker.get("shards"))
    return direct + (ticker_shards if ticker_shards else [ticker])


def _sum_rows(rows, field):
    values = [
        value for row in rows
        if (value := _integer(row.get(field))) is not None
    ]
    return sum(values) if values else None


def _counter(venue, rows, field):
    aggregate = _integer(venue.get(field))
    return aggregate if aggregate is not None else _sum_rows(rows, field)


def _latest_message_ms(venue):
    values = []
    pending = [venue]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            value = _integer(item.get("last_message_ms"))
            if value is not None:
                values.append(value)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return max(values) if values else None


def _feed_status(value):
    status = _text(value)
    lowered = status.lower() if status is not None else None
    if lowered is None:
        return None
    return lowered if lowered in {"healthy", "degraded", "broken"} else "other"


def _venue_metrics(venue, now_ms):
    rows = _connection_rows(venue)
    stream_count = _integer(venue.get("stream_count"))
    if stream_count is None:
        stream_count = _sum_rows(rows, "stream_count")
    latest_message_ms = _latest_message_ms(venue)
    connected_values = [row.get("connected") for row in rows]
    connected_count = (
        sum(value is True for value in connected_values)
        if connected_values and all(isinstance(value, bool) for value in connected_values)
        else None
    )
    result = {
        "expected_connections": _integer(venue.get("expected_connections")),
        "connected_count": connected_count,
        "stream_count": stream_count,
        "last_message_age_ms": _age_ms(now_ms, latest_message_ms),
        "dropped_frames_total": _integer(venue.get("dropped_frames")),
        "feed_status": _feed_status(venue.get("status")),
    }
    for field in _COUNTER_FIELDS:
        result[field] = _counter(venue, rows, field)
    return result


def read_feed_connections(path: str, now_ms: int) -> dict | None:
    payload = _read_object(path)
    if payload is None or not isinstance(payload.get("connections"), dict):
        return None
    written_ms = _integer(payload.get("written_ms"))
    result = {
        "written_ms": written_ms,
        "snapshot_age_ms": _age_ms(now_ms, written_ms),
    }
    for venue_name in ("spot", "perp"):
        venue = payload["connections"].get(venue_name)
        if isinstance(venue, dict):
            result[venue_name] = _venue_metrics(venue, now_ms)
    return result


def _nearest_rank(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    rank = (percentile * len(ordered) + 99) // 100
    return ordered[max(1, rank) - 1]


def read_stream_state(path: str, now_ms: int) -> dict | None:
    payload = _read_object(path)
    if payload is None:
        return None
    book_ages = []
    freshness = []
    row_times = []
    symbols_total = 0
    for row in payload.values():
        if not isinstance(row, dict):
            continue
        symbols_total += 1
        book_age = _integer(row.get("book_age_ms"))
        fresh_age = _integer(row.get("freshness_ms"))
        if book_age is not None:
            book_ages.append(book_age)
        if fresh_age is not None:
            freshness.append(fresh_age)
        row_time = _iso_ms(row.get("utc"))
        if row_time is not None:
            row_times.append(row_time)
    return {
        "symbols_total": symbols_total,
        "book_age_p50_ms": _nearest_rank(book_ages, 50),
        "book_age_p95_ms": _nearest_rank(book_ages, 95),
        "book_age_max_ms": max(book_ages) if book_ages else None,
        "symbols_book_le_5s": sum(value <= 5000 for value in book_ages),
        "freshness_p50_ms": _nearest_rank(freshness, 50),
        "stream_row_age_ms": _age_ms(now_ms, max(row_times) if row_times else None),
        "source": None,
    }


def read_news(
    sources_path: str, jsonl_path: str, now_ms: int
) -> dict | None:
    payload = _read_object(sources_path)
    if payload is None:
        return None
    raw_sources = payload.get("sources")
    has_source_list = isinstance(raw_sources, list)
    source_rows = _rows(raw_sources)
    statuses = [_text(row.get("status")) for row in source_rows]
    statuses = [status.lower() for status in statuses if status is not None]
    counts = {name: statuses.count(name) for name in ("live", "pending", "error")}
    try:
        modified_ms = Path(jsonl_path).stat().st_mtime_ns // 1_000_000
    except OSError:
        modified_ms = None
    def reported(key, fallback):
        value = _integer(payload.get(key))
        return value if value is not None else fallback if has_source_list else None
    return {
        "sources_available": reported("available", len(source_rows)),
        "sources_live": reported("live", counts["live"]),
        "sources_pending": reported("pending", counts["pending"]),
        "sources_error": counts["error"] if has_source_list else None,
        "as_of_age_s": _age_s(now_ms, _iso_ms(payload.get("as_of"))),
        "last_fetch_age_s": _age_s(now_ms, modified_ms),
    }


def read_last_job(path: str) -> dict | None:
    last = None
    try:
        with Path(path).open("r", encoding="utf-8") as source:
            for line in source:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(row, dict):
                    last = row
    except (OSError, UnicodeError):
        return None
    if last is None:
        return None
    return {
        "last_used_at": _text(last.get("utc")),
        "last_op": _text(last.get("op")),
        "last_outcome": _text(last.get("verdict")),
    }


def read_manifest(path: str) -> dict | None:
    payload = _read_object(path)
    if payload is None:
        return None
    result = {
        key: payload.get(key)
        for key in ("utc", "ok", "interval_ms", "cycle_ms", "files")
    }
    plural = payload.get("errors")
    candidates = plural if isinstance(plural, list) else [plural]
    candidates.append(payload.get("error"))
    errors = []
    for value in candidates:
        if isinstance(value, str) and value:
            bounded = value[:200]
            if bounded not in errors:
                errors.append(bounded)
        if len(errors) == 20:
            break
    result["errors"] = errors
    return result
