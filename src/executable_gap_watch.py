#!/usr/bin/env python3
"""Append read-only executable-gap observations from ``stream_state.json``.

The frozen stream schema publishes ``utc``, ``venues``, four touch-price
fields, and conservative ``freshness_ms`` (market_stream.py:97-118,
1214-1238,1404-1420).  It has no separate venue times, so both touches use the
lower bound ``utc - freshness_ms``; missing timing remains unknown and refuses.

Startup replay reads at most the final 64 MiB of ``executable_gaps.jsonl``.
It accepts only OK rows from the last 24 hours, after which BF2 retains at most
481 per symbol.  This covers spec rows 089, 092, 094, and BUILD-01 row 109.
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
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from executable_gap import (ExecutableGapReading, GapHistoryRing,
                            quotes_from_stream_record, read_executable_gap)
from runtime_paths import PATHS, RuntimePaths


OUTPUT_FILENAME = "executable_gaps.jsonl"; REPLAY_MAX_BYTES = 64 * 1024 * 1024
REPLAY_WINDOW_MS = 24 * 60 * 60 * 1_000
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


def _observed_ms(record: Mapping[str, Any]) -> Optional[int]:
    written_ms = _utc_ms(record.get("utc"))
    freshness = record.get("freshness_ms")
    if written_ms is None or type(freshness) is not str or not freshness.isdigit():
        return None
    age_ms = int(freshness)
    return written_ms - age_ms if age_ms <= written_ms else None


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


def _row(utc: str, result: Any) -> Dict[str, Any]:
    accepted = isinstance(result, ExecutableGapReading)
    return {
        "utc": utc, "symbol": result.symbol,
        "entry_gap_pct": _decimal_text(result.entry_gap_pct) if accepted else None,
        "exit_gap_pct": _decimal_text(result.exit_gap_pct) if accepted else None,
        "spot_age_ms": result.spot_age_ms,
        "perp_age_ms": result.perp_age_ms,
        "result": "OK" if accepted else result.reason,
    }


def build_rows(state: Mapping[str, Mapping[str, Any]], now_ms: int) -> Tuple[
        Tuple[Dict[str, Any], ...], Tuple[ExecutableGapReading, ...], WatchSummary]:
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ExecutableGapWatchError("current time must be nonnegative epoch-ms")
    utc = _utc_text(now_ms)
    rows = []
    accepted = []
    refusals = []
    for symbol in sorted(state):
        record = state[symbol]
        if record.get("venues") != "both":
            continue
        observed_ms = _observed_ms(record)
        spot, perp = quotes_from_stream_record(
            _book_text(record), spot_observed_ms=observed_ms,
            perp_observed_ms=observed_ms)
        result = read_executable_gap(symbol, spot, perp, now_ms)
        rows.append(_row(utc, result))
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


def _replay_reading(raw: bytes, cutoff_ms: int, now_ms: int):
    try:
        row = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ExecutableGapWatchError("gap receipt ledger has an invalid row") from None
    if not isinstance(row, dict) or row.get("result") != "OK":
        return None
    row_ms = _utc_ms(row.get("utc"))
    if row_ms is None or not cutoff_ms <= row_ms <= now_ms:
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
    if any(isinstance(age, bool) or not isinstance(age, int) or age < 0
           for age in ages) or not entry.is_finite() or not exit_gap.is_finite():
        raise ExecutableGapWatchError("gap receipt OK row has invalid values")
    return ExecutableGapReading(
        row.get("symbol"), row_ms, entry, exit_gap, ages[0], ages[1])


def replay_history(path: Path, now_ms: int) -> GapHistoryRing:
    """Restore accepted last-24h rows within the documented tail byte bound."""
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ExecutableGapWatchError("current time must be nonnegative epoch-ms")
    history = GapHistoryRing()
    cutoff_ms = max(0, now_ms - REPLAY_WINDOW_MS)
    for raw in _tail_lines(Path(path)):
        reading = _replay_reading(raw, cutoff_ms, now_ms)
        if reading is None:
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
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.is_symlink():
            raise ExecutableGapWatchError("gap receipt ledger must not be a symlink")
        with open(path, "a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(dict(row), separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
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
    history = replay_history(output_path, current_ms)
    state = load_stream_state(paths.stream_state)
    rows, accepted, summary = build_rows(state, current_ms)
    _append_rows(output_path, rows)
    for reading in accepted:
        history.record(reading)
    return WatchPass(rows, summary, history)


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
