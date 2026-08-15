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


def load_tape(
    data_dir: Path = DEFAULT_DATA_DIR,
) -> Dict[str, Tuple[TapeBar, ...]]:
    """Load, validate, de-duplicate, and order Binance 1m kline zips."""
    root = Path(data_dir)
    if not root.is_dir():
        raise BaselineError(f"data directory does not exist: {root}")
    archives = sorted(path for path in root.rglob("*.zip") if path.is_file())
    if not archives:
        raise BaselineError(f"data directory has no 1m kline zips: {root}")

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


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Derive adaptive per-symbol baselines from offline tape."
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = argument_parser().parse_args(argv)
    try:
        payload = run_baselines(args.data_dir, args.output)
    except BaselineError as error:
        print(f"baselines: {error}", file=sys.stderr)
        return 2
    print(
        "BASELINES | "
        f"symbols={payload['symbol_count']} | "
        f"days={payload['tape_days']} | output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
