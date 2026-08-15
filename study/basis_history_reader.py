#!/usr/bin/env python3
"""Stream and query the local basis-history JSONL ledger."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


CADENCE = timedelta(minutes=3)
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,40}$")
WINDOW_RE = re.compile(r"^([1-9][0-9]*)([mhd])$")
METRIC_FIELDS = ("funding_rate", "next_funding_ms", "basis_gap_pct", "oi", "mark")
ROW_FIELDS = frozenset(("utc", "symbol", "status", *METRIC_FIELDS, "unavailable"))


class HistoryReadError(Exception): pass
class HistoryQueryError(Exception): pass
class _SchemaError(Exception): pass
class _TimestampError(Exception): pass


@dataclass
class ReadTally:
    total_lines: int = 0; accepted_rows: int = 0; recorded_rows: int = 0
    writer_skipped_rows: int = 0; untimed_rows: int = 0; blank_lines: int = 0
    invalid_utf8: int = 0; invalid_json: int = 0; non_object: int = 0
    invalid_schema: int = 0
    invalid_timestamp: int = 0

    @property
    def rejected_rows(self) -> int: return self.total_lines - self.accepted_rows


@dataclass(frozen=True)
class BasisObservation:
    utc: Optional[datetime]; utc_text: Optional[str]; symbol: str
    status: str; funding_rate: Optional[Decimal]; next_funding_ms: Optional[int]
    basis_gap_pct: Optional[Decimal]; oi: Optional[Decimal]; mark: Optional[Decimal]
    unavailable: Tuple[str, ...]; line_number: int

    def as_writer_row(self) -> Dict[str, Any]:
        return {
            "utc": self.utc_text,
            "symbol": self.symbol,
            "status": self.status,
            "funding_rate": _decimal_text(self.funding_rate),
            "next_funding_ms": None if self.next_funding_ms is None else str(self.next_funding_ms),
            "basis_gap_pct": _decimal_text(self.basis_gap_pct),
            "oi": _decimal_text(self.oi),
            "mark": _decimal_text(self.mark),
            "unavailable": list(self.unavailable),
        }


@dataclass(frozen=True)
class BasisHistory:
    rows_by_symbol: Mapping[str, Tuple[BasisObservation, ...]]; tally: ReadTally

    def series_for(self, symbol: str) -> Tuple[BasisObservation, ...]:
        return tuple(row for row in self.rows_by_symbol.get(_validate_symbol(symbol), ())
                     if row.utc is not None)


@dataclass(frozen=True)
class WindowStats:
    symbol: str; window: timedelta; start: Optional[datetime]
    end: Optional[datetime]; samples: int; gap_min: Optional[Decimal]
    gap_max: Optional[Decimal]; gap_mean: Optional[Decimal]
    gap_latest: Optional[Decimal]; latest_utc: Optional[str]


@dataclass(frozen=True)
class CoverageGap:
    symbol: str; previous_utc: str
    next_utc: str; elapsed_microseconds: int

    @property
    def elapsed_seconds(self) -> Decimal:
        return Decimal(self.elapsed_microseconds) / Decimal(1_000_000)


@dataclass(frozen=True)
class DailySummary:
    day: date; symbols: Tuple[str, ...]
    rows: int; coverage_gaps: Tuple[CoverageGap, ...]

    @property
    def symbols_seen(self) -> int: return len(self.symbols)


@dataclass(frozen=True)
class Dislocation:
    symbol: str; utc: str; gap: Decimal

    @property
    def absolute_gap(self) -> Decimal: return abs(self.gap)


def _decimal_text(value: Optional[Decimal]) -> Optional[str]: return None if value is None else format(value, "f")


def _decimal(
    row: Mapping[str, Any], field: str, positive: bool = False,
    nonnegative: bool = False,
) -> Optional[Decimal]:
    raw = row[field]
    if raw is None:
        return None
    if type(raw) is not str or not raw:
        raise _SchemaError
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise _SchemaError from None
    if not value.is_finite() or format(value, "f") != raw:
        raise _SchemaError
    if (positive and value <= 0) or (nonnegative and value < 0):
        raise _SchemaError
    return value


def _utc(raw: str) -> datetime:
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise _TimestampError from None
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise _TimestampError
    return value.astimezone(timezone.utc)


def _parse_row(row: Mapping[str, Any], line_number: int) -> BasisObservation:
    if frozenset(row) != ROW_FIELDS:
        raise _SchemaError
    symbol, raw_utc = row["symbol"], row["utc"]
    if type(symbol) is not str or SYMBOL_RE.fullmatch(symbol) is None:
        raise _SchemaError
    if raw_utc is not None and (type(raw_utc) is not str or not raw_utc):
        raise _SchemaError
    funding = _decimal(row, "funding_rate")
    gap = _decimal(row, "basis_gap_pct")
    oi, mark = _decimal(row, "oi", nonnegative=True), _decimal(row, "mark", True)
    raw_next = row["next_funding_ms"]
    try:
        next_funding = None if raw_next is None else int(raw_next)
    except (TypeError, ValueError):
        raise _SchemaError from None
    if raw_next is not None and (type(raw_next) is not str or next_funding <= 0
                                 or str(next_funding) != raw_next):
        raise _SchemaError
    unavailable = row["unavailable"]
    expected = ([] if raw_utc is not None else ["utc"]) + [
        field for field in METRIC_FIELDS if row[field] is None
    ]
    if type(unavailable) is not list or unavailable != expected:
        raise _SchemaError
    status = "RECORDED" if not unavailable else "SKIPPED"
    if row["status"] != status:
        raise _SchemaError
    return BasisObservation(
        None if raw_utc is None else _utc(raw_utc), raw_utc, symbol, status,
        funding, next_funding, gap, oi, mark, tuple(unavailable), line_number,
    )


def read_history(path: Path) -> BasisHistory:
    """Read physical lines incrementally and retain validated observations."""
    selected, tally = Path(path), ReadTally()
    rows: Dict[str, list] = {}
    try:
        with open(selected, "rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                tally.total_lines += 1
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    tally.invalid_utf8 += 1
                    continue
                if not line.strip():
                    tally.blank_lines += 1
                    continue
                try:
                    payload = json.loads(line)
                except (ValueError, RecursionError):
                    tally.invalid_json += 1
                    continue
                if type(payload) is not dict:
                    tally.non_object += 1
                    continue
                try:
                    observation = _parse_row(payload, line_number)
                except _TimestampError:
                    tally.invalid_timestamp += 1
                    continue
                except _SchemaError:
                    tally.invalid_schema += 1
                    continue
                tally.accepted_rows += 1
                if observation.status == "RECORDED":
                    tally.recorded_rows += 1
                else:
                    tally.writer_skipped_rows += 1
                if observation.utc is None:
                    tally.untimed_rows += 1
                rows.setdefault(observation.symbol, []).append(observation)
    except OSError as error:
        raise HistoryReadError(
            f"basis history is unreadable: {selected}: {error}"
        ) from None
    maximum = datetime.max.replace(tzinfo=timezone.utc)
    result = {}
    for symbol, values in rows.items():
        values.sort(key=lambda item: (item.utc is None, item.utc or maximum,
                                      item.line_number))
        result[symbol] = tuple(values)
    return BasisHistory(result, tally)


def _validate_symbol(symbol: str) -> str:
    if type(symbol) is not str or SYMBOL_RE.fullmatch(symbol) is None:
        raise HistoryQueryError("symbol is invalid")
    return symbol


def parse_window(text: str) -> timedelta:
    match = WINDOW_RE.fullmatch(text) if type(text) is str else None
    if match is None:
        raise HistoryQueryError("window must be positive integer m, h, or d text")
    seconds = {"m": 60, "h": 3_600, "d": 86_400}[match.group(2)]
    try:
        return timedelta(seconds=int(match.group(1)) * seconds)
    except (OverflowError, ValueError):
        raise HistoryQueryError("window is too large") from None


def window_stats(
    history: BasisHistory, symbol: str, window: Any,
    *, end: Optional[datetime] = None,
) -> WindowStats:
    symbol = _validate_symbol(symbol)
    duration = parse_window(window) if type(window) is str else window
    if not isinstance(duration, timedelta) or duration <= timedelta(0):
        raise HistoryQueryError("window must be positive")
    series = history.series_for(symbol)
    if end is not None and (not isinstance(end, datetime) or end.tzinfo is None
                            or end.utcoffset() is None):
        raise HistoryQueryError("window end must include a timezone")
    endpoint = (series[-1].utc if series else None) if end is None else end.astimezone(timezone.utc)
    if endpoint is None:
        return WindowStats(symbol, duration, None, None, 0,
                           None, None, None, None, None)
    try:
        start = endpoint - duration
    except (OverflowError, ValueError):
        raise HistoryQueryError("window start is outside the supported range") from None
    selected = [row for row in series if start <= row.utc <= endpoint
                and row.basis_gap_pct is not None]
    if not selected:
        return WindowStats(symbol, duration, start, endpoint, 0,
                           None, None, None, None, None)
    gaps = [row.basis_gap_pct for row in selected]
    with localcontext() as context:
        context.prec = 50
        mean = sum(gaps, Decimal("0")) / Decimal(len(gaps))
    latest = selected[-1]
    return WindowStats(symbol, duration, start, endpoint, len(gaps), min(gaps),
                       max(gaps), mean, latest.basis_gap_pct, latest.utc_text)


def _timed(history: BasisHistory) -> Iterable[BasisObservation]:
    return (row for rows in history.rows_by_symbol.values() for row in rows
            if row.utc is not None)


def _day(history: BasisHistory, requested: Any) -> date:
    if type(requested) is date:
        return requested
    if type(requested) is str:
        try:
            return date.fromisoformat(requested)
        except ValueError:
            raise HistoryQueryError("day must be YYYY-MM-DD") from None
    if requested is not None:
        raise HistoryQueryError("day must be YYYY-MM-DD")
    latest = max((row.utc.date() for row in _timed(history)), default=None)
    if latest is None:
        raise HistoryQueryError("history has no timestamped rows")
    return latest


def _microseconds(delta: timedelta) -> int: return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def daily_summary(history: BasisHistory, day: Any = None) -> DailySummary:
    selected_day, by_symbol = _day(history, day), {}
    for symbol in history.rows_by_symbol:
        values = tuple(row for row in history.series_for(symbol)
                       if row.utc.date() == selected_day)
        if values:
            by_symbol[symbol] = values
    gaps = []
    for symbol in sorted(by_symbol):
        for previous, current in zip(by_symbol[symbol], by_symbol[symbol][1:]):
            elapsed = current.utc - previous.utc
            if elapsed > CADENCE:
                gaps.append(CoverageGap(symbol, previous.utc_text, current.utc_text,
                                        _microseconds(elapsed)))
    return DailySummary(selected_day, tuple(sorted(by_symbol)),
                        sum(map(len, by_symbol.values())), tuple(gaps))


def top_dislocations(
    history: BasisHistory, count: int, day: Any = None,
) -> Tuple[Dislocation, ...]:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise HistoryQueryError("top count must be a non-negative integer")
    selected_day = _day(history, day)
    values = [row for row in _timed(history)
              if row.utc.date() == selected_day and row.basis_gap_pct is not None]
    values.sort(key=lambda row: (-abs(row.basis_gap_pct), row.utc,
                                 row.symbol, row.line_number))
    return tuple(Dislocation(row.symbol, row.utc_text, row.basis_gap_pct)
                 for row in values[:count])


def _value(value: Optional[Decimal]) -> str: return "unknown" if value is None else format(value, "f")


def _time(value: Optional[datetime]) -> str:
    return "unknown" if value is None else value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _positive(text: str) -> int:
    if not text.isdigit() or int(text) <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--symbol")
    parser.add_argument("--window", action="append", default=[])
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--top", type=_positive)
    return parser


def _read_row(tally: ReadTally) -> str:
    return (
        f"READ | lines={tally.total_lines} | accepted={tally.accepted_rows} | "
        f"recorded={tally.recorded_rows} | writer_skipped={tally.writer_skipped_rows} | "
        f"rejected={tally.rejected_rows} | blank={tally.blank_lines} | "
        f"invalid_utf8={tally.invalid_utf8} | invalid_json={tally.invalid_json} | "
        f"non_object={tally.non_object} | invalid_schema={tally.invalid_schema} | "
        f"invalid_timestamp={tally.invalid_timestamp} | untimed={tally.untimed_rows}"
    )


def _window_row(stats: WindowStats, label: str) -> str:
    return (
        f"WINDOW | symbol={stats.symbol} | window={label} | samples={stats.samples} | "
        f"start={_time(stats.start)} | end={_time(stats.end)} | "
        f"gap_min={_value(stats.gap_min)} | gap_max={_value(stats.gap_max)} | "
        f"gap_mean={_value(stats.gap_mean)} | gap_latest={_value(stats.gap_latest)} | "
        f"latest_utc={stats.latest_utc or 'unknown'}"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.window and args.symbol is None:
            raise HistoryQueryError("--window requires --symbol")
        if not args.window and not args.summary and args.top is None:
            raise HistoryQueryError("select --window, --summary, or --top")
        history = read_history(args.file)
        if args.symbol is not None:
            _validate_symbol(args.symbol)
        windows = [(text, parse_window(text)) for text in args.window]
    except (HistoryReadError, HistoryQueryError) as error:
        print(f"BASIS-HISTORY | ERROR | {error}", file=sys.stderr)
        return 2
    print(_read_row(history.tally))
    try:
        summary = daily_summary(history) if args.summary or args.top is not None else None
    except HistoryQueryError as error:
        print(f"BASIS-HISTORY | ERROR | {error}", file=sys.stderr)
        return 2
    for label, duration in windows:
        print(_window_row(window_stats(history, args.symbol, duration), label))
    if args.summary:
        print(f"SUMMARY | day={summary.day} | symbols={summary.symbols_seen} | "
              f"rows={summary.rows} | cadence_seconds=180 | "
              f"coverage_gaps={len(summary.coverage_gaps)}")
        for gap in summary.coverage_gaps:
            print(f"COVERAGE-GAP | day={summary.day} | symbol={gap.symbol} | "
                  f"previous_utc={gap.previous_utc} | next_utc={gap.next_utc} | "
                  f"elapsed_seconds={format(gap.elapsed_seconds, 'f')}")
    if args.top is not None:
        for rank, item in enumerate(top_dislocations(history, args.top, summary.day), 1):
            print(f"TOP | day={summary.day} | rank={rank} | symbol={item.symbol} | "
                  f"utc={item.utc} | gap_pct={format(item.gap, 'f')} | "
                  f"abs_gap_pct={format(item.absolute_gap, 'f')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
