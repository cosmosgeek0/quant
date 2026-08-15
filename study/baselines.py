#!/usr/bin/env python3
"""Derive deterministic, per-symbol volume baselines from offline 1m tapes.

The input is the verified Binance Vision kline archive layout used by the
study bench.  No network, account, order, or execution code is imported.
Every market value written to JSON is represented as Decimal text.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import zipfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from study.windows import WindowError as _SymbolValidationError
    from study.windows import validate_symbol
except ImportError:
    from windows import WindowError as _SymbolValidationError
    from windows import validate_symbol


STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = STUDY_DIR / "data"
DEFAULT_OUTPUT_PATH = STUDY_DIR / "out" / "baselines.json"
WINDOWS_MINUTES = (1, 10, 15, 60)
ABSORPTION_BASELINE_SCHEMA = "absorption-kline-baselines-v1"
DEFAULT_ABSORPTION_WINDOW_MINUTES = 60
DEFAULT_ABSORPTION_MIN_DAYS = 21
MAX_ABSORPTION_WINDOW_MINUTES = 1_440
MAX_ABSORPTION_DAYS = 90
MINUTES_PER_DAY = 1_440
MILLISECONDS_PER_MINUTE = 60_000
MILLISECONDS_PER_DAY = 86_400_000
SYMBOL_FROM_NAME = re.compile(
    r"^(?P<symbol>.+?)-1m(?:-|\.)", re.IGNORECASE
)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class BaselineError(Exception):
    """The tape or requested baseline output cannot be proved valid."""


@dataclass(frozen=True)
class TapeBar:
    symbol: str
    open_ms: int
    close_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    quote_volume: Decimal


def decimal_text(value: Decimal) -> str:
    """Return finite Decimal text without exponent notation."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BaselineError("cannot format a non-finite Decimal")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0"}:
        return "0"
    return rendered


def _decimal(raw: Any, source: str, *, positive: bool) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise BaselineError(f"{source} is not a Decimal") from None
    if not value.is_finite() or value < 0 or (positive and value <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise BaselineError(
            f"{source} is not a finite {qualifier} Decimal"
        )
    return value


def _timestamp_ms(raw: Any, source: str) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise BaselineError(f"{source} is not an integer timestamp") from None
    if value >= 100_000_000_000_000:
        value //= 1_000
    if value < 100_000_000_000 or value >= 100_000_000_000_000:
        raise BaselineError(f"{source} is outside the supported epoch range")
    return value


def utc_text(epoch_ms: int) -> str:
    value = EPOCH + timedelta(milliseconds=epoch_ms)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _symbol_from_name(name: str) -> Optional[str]:
    match = SYMBOL_FROM_NAME.match(name)
    if match is None:
        return None
    try:
        return validate_symbol(
            match.group("symbol").upper(), f"{name} symbol"
        )
    except _SymbolValidationError:
        return None


def _rows_from_member(
    archive: Path,
    handle: Iterable[str],
    member_name: str,
    symbol: str,
) -> Iterable[TapeBar]:
    source = f"{archive.name}:{member_name}"
    try:
        rows = csv.reader(handle)
        found = False
        for line_number, row in enumerate(rows, start=1):
            if not row or not any(cell.strip() for cell in row):
                continue
            first = row[0].strip().lower().replace(" ", "_")
            if line_number == 1 and first in {"open_time", "opentime"}:
                continue
            if len(row) < 8:
                raise BaselineError(
                    f"{source}:{line_number} has fewer than eight kline "
                    "columns"
                )
            line_source = f"{source}:{line_number}"
            open_ms = _timestamp_ms(row[0], f"{line_source} open time")
            close_ms = _timestamp_ms(row[6], f"{line_source} close time")
            if close_ms - open_ms not in {59_999, 60_000}:
                raise BaselineError(
                    f"{line_source} is not a proved one-minute kline"
                )
            open_price = _decimal(
                row[1], f"{line_source} open", positive=True
            )
            high = _decimal(row[2], f"{line_source} high", positive=True)
            low = _decimal(row[3], f"{line_source} low", positive=True)
            close = _decimal(row[4], f"{line_source} close", positive=True)
            quote_volume = _decimal(
                row[7], f"{line_source} quote volume", positive=False
            )
            if high < max(open_price, low, close) or low > min(
                open_price, high, close
            ):
                raise BaselineError(f"{line_source} has inconsistent OHLC")
            found = True
            yield TapeBar(
                symbol=symbol,
                open_ms=open_ms,
                close_ms=close_ms,
                open=open_price,
                high=high,
                low=low,
                close=close,
                quote_volume=quote_volume,
            )
        if not found:
            raise BaselineError(f"{source} has no kline rows")
    except csv.Error as error:
        raise BaselineError(f"{source} has invalid CSV: {error}") from None


def _read_archive(path: Path) -> Iterable[TapeBar]:
    archive_symbol = _symbol_from_name(path.name)
    try:
        with zipfile.ZipFile(path) as zipped:
            members = sorted(
                (
                    member
                    for member in zipped.infolist()
                    if not member.is_dir()
                    and member.filename.lower().endswith(".csv")
                ),
                key=lambda member: member.filename,
            )
            if not members:
                raise BaselineError(f"{path.name} has no CSV member")
            for member in members:
                if member.flag_bits & 0x1:
                    raise BaselineError(
                        f"{path.name}:{member.filename} is encrypted"
                    )
                member_symbol = _symbol_from_name(member.filename)
                symbol = member_symbol or archive_symbol
                if symbol is None:
                    raise BaselineError(
                        f"cannot prove symbol from "
                        f"{path.name}:{member.filename}"
                    )
                if (
                    archive_symbol is not None
                    and member_symbol is not None
                    and archive_symbol != member_symbol
                ):
                    raise BaselineError(
                        f"symbol mismatch in {path.name}:{member.filename}"
                    )
                with zipped.open(member, "r") as raw:
                    text = (line.decode("utf-8-sig") for line in raw)
                    yield from _rows_from_member(
                        path, text, member.filename, symbol
                    )
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as error:
        raise BaselineError(f"cannot read {path.name}: {error}") from None


def _normalized_symbols(
    symbols: Optional[Sequence[str]],
) -> Optional[Tuple[str, ...]]:
    if symbols is None:
        return None
    found: List[str] = []
    for raw in symbols:
        for candidate in str(raw).split(","):
            candidate = candidate.strip()
            if not candidate:
                raise BaselineError("symbol list contains an empty value")
            try:
                symbol = validate_symbol(candidate, "requested symbol")
            except _SymbolValidationError as error:
                raise BaselineError(str(error)) from None
            if symbol not in found:
                found.append(symbol)
    if not found:
        raise BaselineError("symbol list is empty")
    return tuple(found)


def _load_tape_archives(
    archives: Sequence[Path],
) -> Dict[str, Tuple[TapeBar, ...]]:
    indexed: Dict[str, Dict[int, TapeBar]] = {}
    for archive in archives:
        for bar in _read_archive(archive):
            existing = indexed.setdefault(bar.symbol, {}).get(bar.close_ms)
            if existing is not None and existing != bar:
                raise BaselineError(
                    f"conflicting duplicate {bar.symbol} kline at "
                    f"{utc_text(bar.close_ms)}"
                )
            indexed[bar.symbol][bar.close_ms] = bar
    if not indexed:
        raise BaselineError("tape has no valid symbols")
    return {
        symbol: tuple(by_close[key] for key in sorted(by_close))
        for symbol, by_close in sorted(indexed.items())
    }


def load_tape(
    data_dir: Path = DEFAULT_DATA_DIR,
    symbols: Optional[Sequence[str]] = None,
) -> Dict[str, Tuple[TapeBar, ...]]:
    """Load, validate, de-duplicate, and order Binance 1m kline zips."""
    root = Path(data_dir)
    if not root.is_dir():
        raise BaselineError(f"data directory does not exist: {root}")
    requested = _normalized_symbols(symbols)
    selected = None if requested is None else set(requested)
    archives: List[Path] = []
    for path in root.rglob("*.zip"):
        if not path.is_file():
            continue
        symbol = _symbol_from_name(path.name)
        if symbol is not None and (
            selected is None or symbol in selected
        ):
            archives.append(path)
    archives.sort()
    if not archives:
        if requested is not None:
            return {}
        raise BaselineError(f"data directory has no 1m kline zips: {root}")
    return _load_tape_archives(archives)


def quantile(
    values: Sequence[Decimal], numerator: int, denominator: int
) -> Optional[Decimal]:
    """Return a linearly interpolated quantile using exact Decimal math."""
    if not values:
        return None
    if denominator <= 0 or numerator < 0 or numerator > denominator:
        raise BaselineError("invalid quantile")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    with localcontext() as context:
        context.prec = 50
        position = (
            Decimal(len(ordered) - 1)
            * Decimal(numerator)
            / Decimal(denominator)
        )
        lower_index = int(position)
        upper_index = min(lower_index + 1, len(ordered) - 1)
        fraction = position - Decimal(lower_index)
        return ordered[lower_index] + (
            (ordered[upper_index] - ordered[lower_index]) * fraction
        )


def _regime(values: Sequence[Decimal]) -> Dict[str, Any]:
    positive = [value for value in values if value > 0]
    quiet = quantile(positive, 1, 5)
    typical = quantile(positive, 1, 2)
    hot = quantile(positive, 9, 10)
    return {
        "sample_count": len(values),
        "positive_sample_count": len(positive),
        "zero_sample_count": len(values) - len(positive),
        "quiet_floor": None if quiet is None else decimal_text(quiet),
        "typical": None if typical is None else decimal_text(typical),
        "hot": None if hot is None else decimal_text(hot),
    }


def _rolling_sums(
    bars: Sequence[TapeBar], window: int
) -> Tuple[Decimal, ...]:
    if window == 1:
        return tuple(bar.quote_volume for bar in bars)
    rolling: Deque[TapeBar] = deque()
    total = Decimal("0")
    found: List[Decimal] = []
    previous_close: Optional[int] = None
    for bar in bars:
        if (
            previous_close is not None
            and bar.close_ms - previous_close != 60_000
        ):
            rolling.clear()
            total = Decimal("0")
        previous_close = bar.close_ms
        rolling.append(bar)
        total += bar.quote_volume
        if len(rolling) > window:
            total -= rolling.popleft().quote_volume
        if len(rolling) == window:
            found.append(total)
    return tuple(found)


def bar_range_pct(bar: TapeBar) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return ((bar.high - bar.low) / bar.close) * Decimal("100")


def _divide(
    numerator: Optional[Decimal], denominator: Optional[Decimal]
) -> Optional[Decimal]:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    with localcontext() as context:
        context.prec = 50
        return numerator / denominator


def _value(regime: Mapping[str, Any], key: str) -> Optional[Decimal]:
    raw = regime.get(key)
    if raw is None:
        return None
    return _decimal(raw, f"regime {key}", positive=False)


def _hour_of(bar: TapeBar) -> str:
    value = EPOCH + timedelta(milliseconds=bar.close_ms)
    return f"{value.hour:02d}"


def _symbol_baseline(bars: Sequence[TapeBar]) -> Dict[str, Any]:
    if not bars:
        raise BaselineError("cannot derive a baseline from no bars")
    rolling = {
        f"{window}m": _regime(_rolling_sums(bars, window))
        for window in WINDOWS_MINUTES
    }
    one_minute = rolling["1m"]
    quiet = _value(one_minute, "quiet_floor")
    hot = _value(one_minute, "hot")
    ignition_multiple = _divide(hot, quiet)

    hourly_values: Dict[str, List[Decimal]] = {}
    hourly_ranges: Dict[str, List[Decimal]] = {}
    for bar in bars:
        hour = _hour_of(bar)
        hourly_values.setdefault(hour, []).append(bar.quote_volume)
        hourly_ranges.setdefault(hour, []).append(bar_range_pct(bar))

    global_typical = _value(one_minute, "typical")
    hourly: Dict[str, Dict[str, Any]] = {}
    for hour in sorted(hourly_values):
        volume_regime = _regime(hourly_values[hour])
        range_regime = _regime(hourly_ranges[hour])
        hour_quiet = _value(volume_regime, "quiet_floor")
        hour_typical = _value(volume_regime, "typical")
        seasonality = _divide(hour_typical, global_typical)
        threshold = None
        if hour_quiet is not None and ignition_multiple is not None:
            threshold = hour_quiet * ignition_multiple
        hourly[hour] = {
            "volume_regime": volume_regime,
            "range_regime_pct": range_regime,
            "seasonality_factor": (
                None if seasonality is None else decimal_text(seasonality)
            ),
            "ignition_quote_volume": (
                None if threshold is None else decimal_text(threshold)
            ),
        }

    range_regime = _regime([bar_range_pct(bar) for bar in bars])
    return {
        "bar_count": len(bars),
        "first_utc": utc_text(bars[0].close_ms),
        "last_utc": utc_text(bars[-1].close_ms),
        "rolling_volume_regimes": rolling,
        "range_regime_pct": range_regime,
        "ignition_threshold": {
            "basis": "token_hot_1m_over_token_quiet_floor_1m",
            "quiet_floor_multiple": (
                None
                if ignition_multiple is None
                else decimal_text(ignition_multiple)
            ),
        },
        "time_of_day_utc": hourly,
    }


def derive_baselines(
    tape: Mapping[str, Sequence[TapeBar]],
    *,
    source_data_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the deterministic v1 baseline document from an in-memory tape."""
    if not tape:
        raise BaselineError("tape has no symbols")
    days = {
        (EPOCH + timedelta(milliseconds=bar.close_ms)).date().isoformat()
        for bars in tape.values()
        for bar in bars
    }
    if not days:
        raise BaselineError("tape has no dated bars")
    symbols: Dict[str, Any] = {}
    for symbol in sorted(tape):
        bars = tuple(tape[symbol])
        if not bars:
            raise BaselineError(f"{symbol} has no bars")
        if any(bar.symbol != symbol for bar in bars):
            raise BaselineError(f"{symbol} tape contains another symbol")
        if any(
            current.close_ms <= previous.close_ms
            for previous, current in zip(bars, bars[1:])
        ):
            raise BaselineError(f"{symbol} bars are not strictly ordered")
        symbols[symbol] = _symbol_baseline(bars)
    return {
        "schema": "adaptive-volume-baselines-v1",
        "source": {"data_dir": source_data_dir},
        "windows_minutes": list(WINDOWS_MINUTES),
        "tape_days": len(days),
        "symbol_count": len(symbols),
        "symbols": symbols,
    }


def _complete_utc_days(
    bars: Sequence[TapeBar],
) -> Tuple[Tuple[str, Tuple[TapeBar, ...]], ...]:
    by_day: Dict[int, Dict[int, TapeBar]] = {}
    for bar in bars:
        day_start = bar.open_ms - (bar.open_ms % MILLISECONDS_PER_DAY)
        existing = by_day.setdefault(day_start, {}).get(bar.open_ms)
        if existing is not None and existing != bar:
            raise BaselineError(
                f"{bar.symbol} has a conflicting kline within one UTC day"
            )
        by_day[day_start][bar.open_ms] = bar

    complete: List[Tuple[str, Tuple[TapeBar, ...]]] = []
    for day_start in sorted(by_day):
        by_open = by_day[day_start]
        expected = tuple(
            day_start + index * MILLISECONDS_PER_MINUTE
            for index in range(MINUTES_PER_DAY)
        )
        if tuple(sorted(by_open)) != expected:
            continue
        day_bars = tuple(by_open[open_ms] for open_ms in expected)
        complete.append((utc_text(day_start)[:10], day_bars))
    return tuple(complete)


def _absorption_window_metrics(
    bars: Sequence[TapeBar],
) -> Tuple[Decimal, Decimal]:
    if not bars:
        raise BaselineError("cannot measure an empty absorption window")
    low = min(bar.low for bar in bars)
    high = max(bar.high for bar in bars)
    volume = sum((bar.quote_volume for bar in bars), Decimal("0"))
    with localcontext() as context:
        context.prec = 50
        range_pct = ((high - low) / low) * Decimal("100")
    return range_pct, volume


def _absorption_symbol_baseline(
    complete_days: Sequence[Tuple[str, Sequence[TapeBar]]],
    window_minutes: int,
) -> Dict[str, Any]:
    days = tuple((day, tuple(day_bars)) for day, day_bars in complete_days)
    bars = tuple(bar for _day, day_bars in days for bar in day_bars)
    if not bars:
        raise BaselineError("cannot derive an absorption baseline from no bars")
    ranges: List[Decimal] = []
    volumes: List[Decimal] = []
    for _day, day_bars in days:
        for start in range(
            0,
            MINUTES_PER_DAY - window_minutes + 1,
            window_minutes,
        ):
            range_pct, volume = _absorption_window_metrics(
                day_bars[start : start + window_minutes]
            )
            ranges.append(range_pct)
            volumes.append(volume)
    if not ranges:
        raise BaselineError("complete days have no comparable windows")
    close_q20 = quantile([bar.close for bar in bars], 1, 5)
    if close_q20 is None:
        raise BaselineError("complete days have no closing-price baseline")
    return {
        "status": "READY",
        "day_count": len(days),
        "days_used": [day for day, _day_bars in days],
        "bar_count": len(bars),
        "first_open_ms": bars[0].open_ms,
        "last_close_ms": bars[-1].close_ms,
        "historical_low": decimal_text(min(bar.low for bar in bars)),
        "historical_high": decimal_text(max(bar.high for bar in bars)),
        "close_q20": decimal_text(close_q20),
        "window_sample_count": len(ranges),
        "window_range_samples_pct": [
            decimal_text(value) for value in sorted(ranges)
        ],
        "window_quote_volume_samples": [
            decimal_text(value) for value in sorted(volumes)
        ],
    }


def derive_absorption_baselines(
    tape: Mapping[str, Sequence[TapeBar]],
    requested_symbols: Sequence[str],
    *,
    window_minutes: int = DEFAULT_ABSORPTION_WINDOW_MINUTES,
    minimum_days: int = DEFAULT_ABSORPTION_MIN_DAYS,
    source_data_dir: Optional[str] = None,
    load_errors: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Build compact kline baselines and explicit readiness coverage."""
    if (
        isinstance(window_minutes, bool)
        or not isinstance(window_minutes, int)
        or not 1 <= window_minutes <= MAX_ABSORPTION_WINDOW_MINUTES
    ):
        raise BaselineError(
            f"absorption window must be in [1, {MAX_ABSORPTION_WINDOW_MINUTES}]"
        )
    if (
        isinstance(minimum_days, bool)
        or not isinstance(minimum_days, int)
        or not 1 <= minimum_days <= MAX_ABSORPTION_DAYS
    ):
        raise BaselineError(
            f"minimum days must be in [1, {MAX_ABSORPTION_DAYS}]"
        )
    normalized = _normalized_symbols(requested_symbols)
    if normalized is None:
        raise BaselineError("absorption baseline requires a symbol list")

    ready: Dict[str, Any] = {}
    not_ready: Dict[str, Any] = {}
    used_days = set()
    for symbol in sorted(normalized):
        load_reason = None if load_errors is None else load_errors.get(symbol)
        if load_reason is not None:
            not_ready[symbol] = {
                "status": "NOT-READY",
                "days_found": 0,
                "reason": load_reason,
            }
            continue
        bars = tuple(tape.get(symbol, ()))
        if not bars:
            not_ready[symbol] = {
                "status": "NOT-READY",
                "days_found": 0,
                "reason": "no_kline_archives",
            }
            continue
        if any(bar.symbol != symbol for bar in bars):
            raise BaselineError(f"{symbol} tape contains another symbol")
        if any(
            current.open_ms <= previous.open_ms
            for previous, current in zip(bars, bars[1:])
        ):
            raise BaselineError(f"{symbol} bars are not strictly ordered")
        complete_days = _complete_utc_days(bars)
        if len(complete_days) < minimum_days:
            not_ready[symbol] = {
                "status": "NOT-READY",
                "days_found": len(complete_days),
                "reason": "insufficient_complete_days",
                "required_days": minimum_days,
            }
            continue
        profile = _absorption_symbol_baseline(
            complete_days, window_minutes
        )
        ready[symbol] = profile
        used_days.update(profile["days_used"])
    return {
        "schema": ABSORPTION_BASELINE_SCHEMA,
        "source": {"data_dir": source_data_dir, "tape": "spot_1m_klines"},
        "window_minutes": window_minutes,
        "minimum_days": minimum_days,
        "requested_symbol_count": len(normalized),
        "symbol_count": len(ready),
        "not_ready_count": len(not_ready),
        "tape_days": len(used_days),
        "symbols": ready,
        "not_ready": not_ready,
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(
                dict(payload),
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise BaselineError(
            f"cannot write baseline output {target}: {error}"
        ) from None


def run_baselines(
    data_dir: Path = DEFAULT_DATA_DIR,
    output_path: Path = DEFAULT_OUTPUT_PATH,
) -> Dict[str, Any]:
    source = Path(data_dir).resolve()
    target = Path(output_path)
    try:
        relative_source = os.path.relpath(source, target.parent.resolve())
    except ValueError as error:
        raise BaselineError(f"cannot relate tape and output paths: {error}") \
            from None
    tape = load_tape(source)
    payload = derive_baselines(tape, source_data_dir=relative_source)
    _write_json(target, payload)
    return payload


def _spot_tape_root(root: Path) -> Path:
    candidate = root / "spot"
    if (candidate / "daily" / "klines").is_dir():
        return candidate
    return root


def _load_absorption_tape(
    root: Path, requested: Sequence[str]
) -> Tuple[Dict[str, Tuple[TapeBar, ...]], Dict[str, str]]:
    selected = set(requested)
    by_symbol: Dict[str, List[Path]] = {symbol: [] for symbol in requested}
    for path in root.rglob("*.zip"):
        if not path.is_file():
            continue
        symbol = _symbol_from_name(path.name)
        if symbol in selected:
            by_symbol[symbol].append(path)

    tape: Dict[str, Tuple[TapeBar, ...]] = {}
    load_errors: Dict[str, str] = {}
    for symbol in sorted(requested):
        archives = sorted(by_symbol[symbol])
        if not archives:
            continue
        try:
            loaded = _load_tape_archives(archives)
            if tuple(loaded) != (symbol,):
                raise BaselineError(
                    f"{symbol} archives contain another symbol"
                )
            tape[symbol] = loaded[symbol]
        except BaselineError:
            load_errors[symbol] = "invalid_kline_archives"
    return tape, load_errors


def run_absorption_baselines(
    data_dir: Path,
    output_path: Path,
    *,
    symbols: Optional[Sequence[str]] = None,
    window_minutes: int = DEFAULT_ABSORPTION_WINDOW_MINUTES,
    minimum_days: int = DEFAULT_ABSORPTION_MIN_DAYS,
) -> Dict[str, Any]:
    """Build one absorption baseline file from measured spot kline days."""
    source = Path(data_dir).resolve()
    if not source.is_dir():
        raise BaselineError(f"data directory does not exist: {source}")
    tape_root = _spot_tape_root(source)
    requested = _normalized_symbols(symbols)
    if requested is None:
        requested = tuple(
            sorted(
                {
                    symbol
                    for path in tape_root.rglob("*.zip")
                    if path.is_file()
                    for symbol in (_symbol_from_name(path.name),)
                    if symbol is not None
                }
            )
        )
        if not requested:
            raise BaselineError(
                f"data directory has no 1m kline zips: {tape_root}"
            )
    tape, load_errors = _load_absorption_tape(tape_root, requested)
    target = Path(output_path)
    try:
        relative_source = os.path.relpath(source, target.parent.resolve())
    except ValueError as error:
        raise BaselineError(f"cannot relate tape and output paths: {error}") \
            from None
    payload = derive_absorption_baselines(
        tape,
        requested,
        window_minutes=window_minutes,
        minimum_days=minimum_days,
        source_data_dir=relative_source,
        load_errors=load_errors,
    )
    _write_json(target, payload)
    return payload


def _bounded_positive(raw: str, name: str, maximum: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{name} must be an integer") from None
    if not 1 <= value <= maximum:
        raise argparse.ArgumentTypeError(
            f"{name} must be between 1 and {maximum}"
        )
    return value


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Derive adaptive per-symbol baselines from offline tape."
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument(
        "--absorption",
        action="store_true",
        help="build the compact historical baseline used by absorption",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        help="space- or comma-separated symbols; omit for all spot symbols",
    )
    parser.add_argument(
        "--window",
        type=lambda raw: _bounded_positive(
            raw, "window", MAX_ABSORPTION_WINDOW_MINUTES
        ),
        default=DEFAULT_ABSORPTION_WINDOW_MINUTES,
        help="absorption scoring-window minutes (default: 60)",
    )
    parser.add_argument(
        "--min-days",
        type=lambda raw: _bounded_positive(
            raw, "min-days", MAX_ABSORPTION_DAYS
        ),
        default=DEFAULT_ABSORPTION_MIN_DAYS,
        help="minimum complete UTC kline days per symbol (default: 21)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = argument_parser().parse_args(argv)
    try:
        if args.absorption:
            payload = run_absorption_baselines(
                args.data_dir,
                args.output,
                symbols=args.symbols,
                window_minutes=args.window,
                minimum_days=args.min_days,
            )
        else:
            if args.symbols is not None:
                raise BaselineError("--symbols requires --absorption")
            payload = run_baselines(args.data_dir, args.output)
    except BaselineError as error:
        print(f"baselines: {error}", file=sys.stderr)
        return 2
    if args.absorption:
        for symbol, profile in sorted(payload["symbols"].items()):
            print(
                "BASELINES | "
                f"symbol={symbol} | status=READY | "
                f"days_used={profile['day_count']}"
            )
        for symbol, coverage in sorted(payload["not_ready"].items()):
            print(
                "BASELINES | "
                f"symbol={symbol} | status=NOT-READY | "
                f"days_found={coverage['days_found']} | "
                f"reason={coverage['reason']}"
            )
        print(
            "BASELINES | "
            f"built={payload['symbol_count']} | "
            f"skipped={payload['not_ready_count']} | output={args.output}"
        )
        return 0
    print(
        "BASELINES | "
        f"symbols={payload['symbol_count']} | "
        f"days={payload['tape_days']} | output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
