#!/usr/bin/env python3
"""Append read-only executable-gap observations from ``stream_state.json``.

The stream schema publishes ``utc``, ``venues``, four touch-price fields, and
one shared quote age.  ``book_age_ms`` is preferred when known; older records
fall back to conservative composite ``freshness_ms``.  No per-venue times are
present, so accepted rows state that paired-venue skew was not checked.

Startup replay reads at most the final 64 MiB of ``executable_gaps.jsonl``.
It restores only OK rows from the last 24 hours into the ring and retains one
last refusal UTC per symbol for dedupe.  BF2 retains at most 481 accepted cuts
per symbol.  This covers spec rows 089, 092, 094, and BUILD-01 row 109.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Literal, Mapping, Optional, Sequence, Tuple

from executable_gap import (ExecutableGapReading, GapHistoryRing,
                            HistoryQueryError,
                            read_executable_gap_shared_observation)
from runtime_paths import PATHS, RuntimePaths


OUTPUT_FILENAME = "executable_gaps.jsonl"; REPLAY_MAX_BYTES = 64 * 1024 * 1024
REPLAY_WINDOW_MS = 24 * 60 * 60 * 1_000
# Inclusive boundary: +2000 ms is permitted and +2001 ms is future.
RECORD_UTC_FUTURE_TOLERANCE_MS = 2_000
VENUES = frozenset(("spot", "perp", "both"))
STALE_REASONS = frozenset(("SPOT_QUOTE_STALE", "PERP_QUOTE_STALE"))
CROSSED_REASONS = frozenset(("SPOT_BOOK_CROSSED_OR_LOCKED",
                             "PERP_BOOK_CROSSED_OR_LOCKED"))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ExecutableGapWatchError(Exception):
    """The local stream input or receipt ledger could not be used safely."""


@dataclass(frozen=True)
class WatchSummary:
    symbols_seen: int
    computed: int
    refused_stale: int
    refused_crossed: int
    top_5: Tuple[Tuple[str, Decimal], ...]


@dataclass(frozen=True)
class WatchPass:
    rows: Tuple[Mapping[str, Any], ...]
    summary: WatchSummary
    history: GapHistoryRing


@dataclass(frozen=True)
class RecordTiming:
    utc_ms: Optional[int]
    observed_ms: Optional[int]
    age_source: Literal["book_age_ms", "freshness_ms"]
    utc_ahead_ms: Optional[int] = None
    refusal_reason: Optional[Literal["RECORD_UTC_IN_FUTURE"]] = None


@dataclass(frozen=True)
class RecordRefusal:
    symbol: str
    reason: Literal["RECORD_UTC_IN_FUTURE"]
    spot_age_ms: None = None
    perp_age_ms: None = None
    skew_checked: Literal[False] = False


def _utc_ms(raw: Any) -> Optional[int]:
    if type(raw) is not str or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        return None
    delta = value.astimezone(timezone.utc) - EPOCH
    if delta < timedelta(0):
        return None
    return ((delta.days * 86_400 + delta.seconds) * 1_000
            + delta.microseconds // 1_000)


def _utc_text(now_ms: int) -> str:
    try:
        value = EPOCH + timedelta(milliseconds=now_ms)
    except OverflowError:
        raise ExecutableGapWatchError(
            "current time is outside the supported range") from None
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _nonnegative_ms(raw: Any) -> Optional[int]:
    if type(raw) is int and raw >= 0:
        return raw
    if type(raw) is str and raw.isdigit():
        return int(raw)
    return None


def _record_utc_is_permitted(record_ms: int, now_ms: int) -> bool:
    return record_ms <= now_ms + RECORD_UTC_FUTURE_TOLERANCE_MS


def _record_timing(record: Mapping[str, Any], now_ms: int) -> RecordTiming:
    written_ms = _utc_ms(record.get("utc"))
    uses_book_age = "book_age_ms" in record \
        and record.get("book_age_ms") is not None
    age_source = "book_age_ms" if uses_book_age else "freshness_ms"
    age_ms = _nonnegative_ms(record.get(age_source))
    utc_ahead_ms = max(0, written_ms - now_ms) \
        if written_ms is not None else None
    if written_ms is not None \
            and not _record_utc_is_permitted(written_ms, now_ms):
        return RecordTiming(
            written_ms, None, age_source, utc_ahead_ms,
            "RECORD_UTC_IN_FUTURE")
    raw_observed_ms = written_ms - age_ms \
        if written_ms is not None and age_ms is not None and age_ms <= written_ms \
        else None
    observed_ms = min(raw_observed_ms, now_ms) \
        if raw_observed_ms is not None else None
    return RecordTiming(
        written_ms, observed_ms, age_source, utc_ahead_ms)


def _book_text(record: Mapping[str, Any]) -> Mapping[str, Optional[str]]:
    fields = ("spot_bid", "spot_ask", "perp_bid", "perp_ask")
    return {field: record.get(field) if type(record.get(field)) is str else None
            for field in fields}


def load_stream_state(path: Path) -> Mapping[str, Mapping[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ExecutableGapWatchError(f"stream state is unreadable: {path}") \
            from None
    if not isinstance(payload, dict):
        raise ExecutableGapWatchError("stream state must be a symbol mapping")
    for symbol, record in payload.items():
        if type(symbol) is not str or not isinstance(record, dict):
            raise ExecutableGapWatchError("stream state has an invalid record")
        if record.get("venues") not in VENUES:
            raise ExecutableGapWatchError(
                f"stream state venue is invalid for {symbol}")
    return payload


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _row(
    utc: str,
    result: Any,
    age_source: str,
    utc_ahead_ms: Optional[int] = None,
) -> Dict[str, Any]:
    accepted = isinstance(result, ExecutableGapReading)
    return {
        "utc": utc, "symbol": result.symbol,
        "entry_gap_pct": _decimal_text(result.entry_gap_pct) if accepted else None,
        "exit_gap_pct": _decimal_text(result.exit_gap_pct) if accepted else None,
        "spot_age_ms": result.spot_age_ms,
        "perp_age_ms": result.perp_age_ms,
        "age_source": age_source,
        "utc_ahead_ms": utc_ahead_ms,
        "skew_checked": result.skew_checked,
        "result": "OK" if accepted else result.reason,
    }


def build_rows(state: Mapping[str, Mapping[str, Any]], now_ms: int) -> Tuple[
        Tuple[Dict[str, Any], ...], Tuple[ExecutableGapReading, ...], WatchSummary]:
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ExecutableGapWatchError("current time must be nonnegative epoch-ms")
    fallback_utc = _utc_text(now_ms)
    rows = []
    accepted = []
    refusals = []
    for symbol in sorted(state):
        record = state[symbol]
        if record.get("venues") != "both":
            continue
        timing = _record_timing(record, now_ms)
        row_utc = _utc_text(timing.utc_ms) \
            if timing.utc_ms is not None else fallback_utc
        if timing.refusal_reason is not None:
            result = RecordRefusal(symbol, timing.refusal_reason)
        else:
            result = read_executable_gap_shared_observation(
                symbol, _book_text(record), timing.observed_ms, now_ms)
        rows.append(_row(
            row_utc, result, timing.age_source, timing.utc_ahead_ms))
        if isinstance(result, ExecutableGapReading):
            accepted.append(result)
        else:
            refusals.append(result)
    ranked = sorted(((row.symbol, row.entry_gap_pct) for row in accepted),
                    key=lambda item: (-abs(item[1]), item[0]))
    summary = WatchSummary(
        symbols_seen=len(rows),
        computed=len(accepted),
        refused_stale=sum(row.reason in STALE_REASONS for row in refusals),
        refused_crossed=sum(row.reason in CROSSED_REASONS for row in refusals),
        top_5=tuple(ranked[:5]),
    )
    return tuple(rows), tuple(accepted), summary


def _tail_lines(path: Path):
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            start = max(0, size - REPLAY_MAX_BYTES)
            if start:
                handle.seek(start - 1)
                if handle.read(1) != b"\n":
                    handle.readline()
            for raw in handle:
                if raw.strip():
                    yield raw
    except FileNotFoundError:
        return
    except OSError:
        raise ExecutableGapWatchError(f"gap receipt ledger is unreadable: {path}") \
            from None


def _decode_replay_row(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ExecutableGapWatchError("gap receipt ledger has an invalid row") from None


def _reading_from_replay_row(row: Any, cutoff_ms: int, now_ms: int):
    if not isinstance(row, dict) or row.get("result") != "OK":
        return None
    row_ms = _utc_ms(row.get("utc"))
    if row_ms is None or row_ms < cutoff_ms \
            or not _record_utc_is_permitted(row_ms, now_ms):
        return None
    gap_text = (row.get("entry_gap_pct"), row.get("exit_gap_pct"))
    if any(type(value) is not str for value in gap_text):
        raise ExecutableGapWatchError("gap receipt OK row has invalid gap text")
    try:
        entry, exit_gap = (Decimal(value) for value in gap_text)
    except (KeyError, TypeError, InvalidOperation):
        raise ExecutableGapWatchError("gap receipt OK row has invalid gap text") \
            from None
    ages = (row.get("spot_age_ms"), row.get("perp_age_ms"))
    skew_checked = row.get("skew_checked", False)
    if any(isinstance(age, bool) or not isinstance(age, int) or age < 0
           for age in ages) or type(skew_checked) is not bool \
            or not entry.is_finite() or not exit_gap.is_finite():
        raise ExecutableGapWatchError("gap receipt OK row has invalid values")
    return ExecutableGapReading(
        row.get("symbol"), row_ms, entry, exit_gap, ages[0], ages[1],
        skew_checked=skew_checked,
    )


def _replay_reading(raw: bytes, cutoff_ms: int, now_ms: int):
    return _reading_from_replay_row(
        _decode_replay_row(raw), cutoff_ms, now_ms)


def _replay_refusal_key(row: Any) -> Optional[Tuple[str, int]]:
    if not isinstance(row, dict) or type(row.get("result")) is not str \
            or row.get("result") == "OK":
        return None
    symbol = row.get("symbol")
    row_ms = _utc_ms(row.get("utc"))
    if type(symbol) is not str or row_ms is None:
        return None
    return symbol, row_ms


def replay_history(
    path: Path,
    now_ms: int,
    *,
    last_refusal_utc: Optional[Dict[str, int]] = None,
) -> GapHistoryRing:
    """Restore accepted history and optionally collect last refusal UTCs."""
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ExecutableGapWatchError("current time must be nonnegative epoch-ms")
    history = GapHistoryRing()
    cutoff_ms = max(0, now_ms - REPLAY_WINDOW_MS)
    for raw in _tail_lines(Path(path)):
        row = _decode_replay_row(raw)
        reading = _reading_from_replay_row(row, cutoff_ms, now_ms)
        if reading is None:
            refusal_key = _replay_refusal_key(row)
            if refusal_key is not None and last_refusal_utc is not None:
                symbol, row_ms = refusal_key
                last_refusal_utc[symbol] = row_ms
            continue
        try:
            history.series_for(reading.symbol)
            history.record(reading)
        except (TypeError, ValueError):
            raise ExecutableGapWatchError(
                "gap receipt OK rows are not valid ordered history") from None
    return history


def _append_rows(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    path = Path(path)
    try:
        encoded_rows = tuple(
            (json.dumps(dict(row), separators=(",", ":")) + "\n").encode("utf-8")
            for row in rows
        )
        if not path.parent.is_dir():
            raise ExecutableGapWatchError(
                f"gap receipt directory is unavailable: {path.parent}")
        if path.is_symlink():
            raise ExecutableGapWatchError("gap receipt ledger must not be a symlink")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT \
            | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            for encoded in encoded_rows:
                if os.write(descriptor, encoded) != len(encoded):
                    raise OSError("gap receipt append was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except ExecutableGapWatchError:
        raise
    except (OSError, TypeError, ValueError):
        raise ExecutableGapWatchError(
            f"gap receipt append failed: {path}") from None


def format_summary(summary: WatchSummary) -> str:
    top = ",".join(
        f"{symbol}:{_decimal_text(value)}" for symbol, value in summary.top_5
    ) or "-"
    return (
        "EXECUTABLE-GAP | symbols_seen=%d computed=%d refused_stale=%d "
        "refused_crossed=%d top_5_abs_entry=%s"
        % (summary.symbols_seen, summary.computed, summary.refused_stale,
           summary.refused_crossed, top)
    )


def run_once(paths: RuntimePaths = PATHS, *, now_ms: Optional[int] = None) -> WatchPass:
    current_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    output_path = paths.receipt_dir / OUTPUT_FILENAME
    last_refusal_utc: Dict[str, int] = {}
    history = replay_history(
        output_path, current_ms, last_refusal_utc=last_refusal_utc)
    state = load_stream_state(paths.stream_state)
    rows, accepted, summary = build_rows(state, current_ms)
    accepted_by_symbol = {reading.symbol: reading for reading in accepted}
    append_rows = []
    append_readings = []
    for row in rows:
        cut_ms = _utc_ms(row["utc"])
        if cut_ms is None:
            raise ExecutableGapWatchError("stream cut time is invalid")
        try:
            latest = history.series_for(row["symbol"])
        except HistoryQueryError:
            latest = ()
        if latest and latest[-1].utc_text == row["utc"] \
                or last_refusal_utc.get(row["symbol"]) == cut_ms:
            continue
        reading = accepted_by_symbol.get(row["symbol"])
        if reading is not None:
            append_readings.append((row, reading))
        else:
            last_refusal_utc[row["symbol"]] = cut_ms
        append_rows.append(row)
    try:
        for row, reading in append_readings:
            cut_ms = _utc_ms(row["utc"])
            if cut_ms is None:
                raise ValueError("accepted cut time is invalid")
            history.record(ExecutableGapReading(
                reading.symbol, cut_ms, reading.entry_gap_pct,
                reading.exit_gap_pct, reading.spot_age_ms,
                reading.perp_age_ms, skew_checked=reading.skew_checked,
            ))
    except (TypeError, ValueError):
        raise ExecutableGapWatchError(
            "stream cuts are not valid ordered history") from None
    _append_rows(output_path, append_rows)
    return WatchPass(tuple(append_rows), summary, history)


def main(argv: Optional[Sequence[str]] = None, *, paths: RuntimePaths = PATHS,
         now_ms: Optional[int] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("--once is required")
    try:
        completed = run_once(paths, now_ms=now_ms)
    except ExecutableGapWatchError as exc:
        print(f"EXECUTABLE-GAP | ERROR | {exc}", file=sys.stderr)
        return 2
    print(format_summary(completed.summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
