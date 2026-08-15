#!/usr/bin/env python3
"""Score pre-ignition absorption from offline spot klines and aggTrades.

The detector compares every value with the same symbol's preceding tape.  It
has no network, account, order, or execution path.  Market arithmetic is
Decimal-only and unavailable evidence produces a zero score rather than an
assumed value.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = STUDY_DIR / "data"
DEFAULT_OUTPUT_DIR = STUDY_DIR / "out"
DEFAULT_WINDOW_MINUTES = 60
DEFAULT_STEP_MINUTES = 60
DEFAULT_LOOKBACK_DAYS = 21
MAX_WINDOW_MINUTES = 1_440
MAX_LOOKBACK_DAYS = 90
FORWARD_HOURS = (1, 6, 24)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
KLINE_ARCHIVE = re.compile(
    r"^(?P<symbol>[A-Z0-9]{5,30})-1m-(?P<day>\d{4}-\d{2}-\d{2})\.zip$"
)
AGG_TRADES_ARCHIVE = re.compile(
    r"^(?P<symbol>[A-Z0-9]{5,30})-aggTrades-"
    r"(?P<day>\d{4}-\d{2}-\d{2})\.zip$"
)


class AbsorptionError(Exception):
    """The requested absorption result cannot be proved from the tape."""


@dataclass(frozen=True)
class KlineBar:
    symbol: str
    open_ms: int
    close_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    quote_volume: Decimal


@dataclass(frozen=True)
class AggTrade:
    symbol: str
    aggregate_id: int
    timestamp_ms: int
    price: Decimal
    quantity: Decimal
    buyer_is_maker: bool

    @property
    def quote_size(self) -> Decimal:
        with localcontext() as context:
            context.prec = 50
            return self.price * self.quantity


@dataclass(frozen=True)
class WindowMetrics:
    range_pct: Decimal
    move_pct: Decimal
    quote_volume: Decimal


@dataclass(frozen=True)
class TradeIndex:
    trades: Tuple[AggTrade, ...]
    timestamps: Tuple[int, ...]

    def between(self, start_ms: int, end_ms: int) -> Tuple[AggTrade, ...]:
        left = bisect.bisect_left(self.timestamps, start_ms)
        right = bisect.bisect_right(self.timestamps, end_ms)
        return self.trades[left:right]


def decimal_text(value: Decimal) -> str:
    """Render one finite Decimal without exponent notation."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AbsorptionError("cannot format a non-finite Decimal")
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
        raise AbsorptionError(f"{source} is not a Decimal") from None
    if not value.is_finite() or value < 0 or (positive and value <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise AbsorptionError(
            f"{source} is not a finite {qualifier} Decimal"
        )
    return value


def _integer(raw: Any, source: str, *, non_negative: bool) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise AbsorptionError(f"{source} is not an integer") from None
    if non_negative and value < 0:
        raise AbsorptionError(f"{source} is negative")
    return value


def _timestamp_ms(raw: Any, source: str) -> int:
    value = _integer(raw, source, non_negative=True)
    if value >= 100_000_000_000_000:
        value //= 1_000
    if value < 100_000_000_000 or value >= 100_000_000_000_000:
        raise AbsorptionError(f"{source} is outside the supported epoch range")
    return value


def _boolean(raw: Any, source: str) -> bool:
    normalized = str(raw).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise AbsorptionError(f"{source} is not true or false")


def utc_text(epoch_ms: int) -> str:
    value = EPOCH + timedelta(milliseconds=epoch_ms)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _archive_member(path: Path, expected: str) -> Tuple[zipfile.ZipFile, Any]:
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise AbsorptionError(f"cannot read {path.name}: {error}") from None
    members = [member for member in archive.infolist() if not member.is_dir()]
    if len(members) != 1 or members[0].filename != expected:
        archive.close()
        raise AbsorptionError(f"{path.name} does not contain only {expected}")
    if members[0].flag_bits & 0x1:
        archive.close()
        raise AbsorptionError(f"{path.name}:{expected} is encrypted")
    return archive, members[0]


def _kline_rows(path: Path, symbol: str) -> Iterable[KlineBar]:
    expected = path.stem + ".csv"
    archive, member = _archive_member(path, expected)
    found = False
    try:
        with archive:
            with archive.open(member, "r") as raw:
                with io.TextIOWrapper(
                    raw, encoding="utf-8-sig", newline=""
                ) as handle:
                    for line_number, row in enumerate(
                        csv.reader(handle), start=1
                    ):
                        if not row or not any(cell.strip() for cell in row):
                            continue
                        first = row[0].strip().lower().replace(" ", "_")
                        if line_number == 1 and first in {
                            "open_time",
                            "opentime",
                        }:
                            continue
                        source = f"{path.name}:{expected}:{line_number}"
                        if len(row) < 8:
                            raise AbsorptionError(
                                f"{source} has fewer than eight kline columns"
                            )
                        open_ms = _timestamp_ms(row[0], f"{source} open time")
                        close_ms = _timestamp_ms(
                            row[6], f"{source} close time"
                        )
                        if close_ms - open_ms not in {59_999, 60_000}:
                            raise AbsorptionError(
                                f"{source} is not a proved one-minute kline"
                            )
                        open_price = _decimal(
                            row[1], f"{source} open", positive=True
                        )
                        high = _decimal(
                            row[2], f"{source} high", positive=True
                        )
                        low = _decimal(
                            row[3], f"{source} low", positive=True
                        )
                        close = _decimal(
                            row[4], f"{source} close", positive=True
                        )
                        quote_volume = _decimal(
                            row[7],
                            f"{source} quote volume",
                            positive=False,
                        )
                        if high < max(open_price, low, close) or low > min(
                            open_price, high, close
                        ):
                            raise AbsorptionError(
                                f"{source} has inconsistent OHLC"
                            )
                        found = True
                        yield KlineBar(
                            symbol,
                            open_ms,
                            close_ms,
                            open_price,
                            high,
                            low,
                            close,
                            quote_volume,
                        )
    except (OSError, UnicodeError, csv.Error, zipfile.BadZipFile) as error:
        raise AbsorptionError(f"cannot parse {path.name}: {error}") from None
    if not found:
        raise AbsorptionError(f"{path.name}:{expected} has no kline rows")


def _agg_trade_rows(path: Path, symbol: str) -> Iterable[AggTrade]:
    expected = path.stem + ".csv"
    archive, member = _archive_member(path, expected)
    found = False
    try:
        with archive:
            with archive.open(member, "r") as raw:
                with io.TextIOWrapper(
                    raw, encoding="utf-8-sig", newline=""
                ) as handle:
                    for line_number, row in enumerate(
                        csv.reader(handle), start=1
                    ):
                        if not row or not any(cell.strip() for cell in row):
                            continue
                        first = row[0].strip().lower().replace(" ", "_")
                        if line_number == 1 and first in {
                            "agg_trade_id",
                            "aggregate_trade_id",
                            "aggtradeid",
                        }:
                            continue
                        source = f"{path.name}:{expected}:{line_number}"
                        if len(row) < 7:
                            raise AbsorptionError(
                                f"{source} has fewer than seven aggTrades columns"
                            )
                        aggregate_id = _integer(
                            row[0], f"{source} aggregate id", non_negative=True
                        )
                        price = _decimal(
                            row[1], f"{source} price", positive=True
                        )
                        quantity = _decimal(
                            row[2], f"{source} quantity", positive=True
                        )
                        first_trade_id = _integer(
                            row[3],
                            f"{source} first trade id",
                            non_negative=True,
                        )
                        last_trade_id = _integer(
                            row[4],
                            f"{source} last trade id",
                            non_negative=True,
                        )
                        if last_trade_id < first_trade_id:
                            raise AbsorptionError(
                                f"{source} has reversed trade ids"
                            )
                        timestamp_ms = _timestamp_ms(
                            row[5], f"{source} transact time"
                        )
                        buyer_is_maker = _boolean(
                            row[6], f"{source} buyer-is-maker"
                        )
                        found = True
                        yield AggTrade(
                            symbol,
                            aggregate_id,
                            timestamp_ms,
                            price,
                            quantity,
                            buyer_is_maker,
                        )
    except (OSError, UnicodeError, csv.Error, zipfile.BadZipFile) as error:
        raise AbsorptionError(f"cannot parse {path.name}: {error}") from None
    if not found:
        raise AbsorptionError(f"{path.name}:{expected} has no aggTrades rows")


def load_klines(data_dir: Path = DEFAULT_DATA_DIR) -> Dict[str, Tuple[KlineBar, ...]]:
    root = Path(data_dir)
    if not root.is_dir():
        raise AbsorptionError(f"data directory does not exist: {root}")
    archives = sorted(
        path
        for path in root.rglob("*.zip")
        if path.is_file() and KLINE_ARCHIVE.fullmatch(path.name) is not None
    )
    if not archives:
        raise AbsorptionError(f"data directory has no daily 1m kline zips: {root}")
    indexed: Dict[str, Dict[int, KlineBar]] = {}
    for archive in archives:
        match = KLINE_ARCHIVE.fullmatch(archive.name)
        if match is None:
            continue
        symbol = match.group("symbol")
        for bar in _kline_rows(archive, symbol):
            existing = indexed.setdefault(symbol, {}).get(bar.open_ms)
            if existing is not None and existing != bar:
                raise AbsorptionError(
                    f"conflicting duplicate {symbol} kline at "
                    f"{utc_text(bar.open_ms)}"
                )
            indexed[symbol][bar.open_ms] = bar
    return {
        symbol: tuple(by_open[key] for key in sorted(by_open))
        for symbol, by_open in sorted(indexed.items())
    }


def load_agg_trades(
    data_dir: Path = DEFAULT_DATA_DIR,
) -> Dict[str, Tuple[AggTrade, ...]]:
    root = Path(data_dir)
    if not root.is_dir():
        raise AbsorptionError(f"data directory does not exist: {root}")
    archives = sorted(
        path
        for path in root.rglob("*.zip")
        if path.is_file()
        and AGG_TRADES_ARCHIVE.fullmatch(path.name) is not None
    )
    if not archives:
        raise AbsorptionError(
            f"data directory has no daily aggTrades zips: {root}"
        )
    indexed: Dict[str, Dict[int, AggTrade]] = {}
    for archive in archives:
        match = AGG_TRADES_ARCHIVE.fullmatch(archive.name)
        if match is None:
            continue
        symbol = match.group("symbol")
        for trade in _agg_trade_rows(archive, symbol):
            existing = indexed.setdefault(symbol, {}).get(trade.aggregate_id)
            if existing is not None and existing != trade:
                raise AbsorptionError(
                    f"conflicting duplicate {symbol} aggTrade "
                    f"{trade.aggregate_id}"
                )
            indexed[symbol][trade.aggregate_id] = trade
    return {
        symbol: tuple(
            sorted(by_id.values(), key=lambda trade: (
                trade.timestamp_ms,
                trade.aggregate_id,
            ))
        )
        for symbol, by_id in sorted(indexed.items())
    }


def quantile(
    values: Sequence[Decimal], numerator: int, denominator: int
) -> Optional[Decimal]:
    """Return an exact linearly interpolated quantile."""
    if not values:
        return None
    if denominator <= 0 or numerator < 0 or numerator > denominator:
        raise AbsorptionError("invalid quantile")
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
            ordered[upper_index] - ordered[lower_index]
        ) * fraction


def taker_quote_totals(
    trades: Sequence[AggTrade],
) -> Tuple[Decimal, Decimal, int, int]:
    """Return buy/sell taker quote and counts using Binance maker-side truth."""
    buy_quote = Decimal("0")
    sell_quote = Decimal("0")
    buy_count = 0
    sell_count = 0
    with localcontext() as context:
        context.prec = 50
        for trade in trades:
            if not isinstance(trade, AggTrade):
                raise AbsorptionError("taker pressure contains a non-trade")
            if trade.buyer_is_maker:
                sell_quote += trade.quote_size
                sell_count += 1
            else:
                buy_quote += trade.quote_size
                buy_count += 1
    return buy_quote, sell_quote, buy_count, sell_count


def taker_imbalance(trades: Sequence[AggTrade]) -> Optional[Decimal]:
    """Return (taker-buy quote - taker-sell quote) / total quote exactly."""
    buy_quote, sell_quote, _, _ = taker_quote_totals(trades)
    total = buy_quote + sell_quote
    if total <= 0:
        return None
    with localcontext() as context:
        context.prec = 50
        return (buy_quote - sell_quote) / total


def _ratio(
    numerator: Optional[Decimal], denominator: Optional[Decimal]
) -> Optional[Decimal]:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    with localcontext() as context:
        context.prec = 50
        return numerator / denominator


def _clamp01(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value))


def _empirical_rank(value: Decimal, baseline: Sequence[Decimal]) -> Optional[Decimal]:
    if not baseline:
        return None
    lower = sum(sample < value for sample in baseline)
    equal = sum(sample == value for sample in baseline)
    with localcontext() as context:
        context.prec = 50
        return (
            Decimal(lower) + (Decimal(equal) / Decimal("2"))
        ) / Decimal(len(baseline))


def _compression_rank(
    value: Decimal, baseline: Sequence[Decimal]
) -> Optional[Decimal]:
    if not baseline:
        return None
    greater = sum(sample > value for sample in baseline)
    equal = sum(sample == value for sample in baseline)
    with localcontext() as context:
        context.prec = 50
        return (
            Decimal(greater) + (Decimal(equal) / Decimal("2"))
        ) / Decimal(len(baseline))


def _window_metrics(bars: Sequence[KlineBar]) -> WindowMetrics:
    if not bars:
        raise AbsorptionError("cannot measure an empty kline window")
    low = min(bar.low for bar in bars)
    high = max(bar.high for bar in bars)
    volume = sum((bar.quote_volume for bar in bars), Decimal("0"))
    with localcontext() as context:
        context.prec = 50
        range_pct = ((high - low) / low) * Decimal("100")
        move_pct = ((bars[-1].close / bars[0].open) - Decimal("1")) * Decimal(
            "100"
        )
    return WindowMetrics(range_pct, move_pct, volume)


def _contiguous(bars: Sequence[KlineBar]) -> bool:
    return all(
        current.open_ms - previous.open_ms == 60_000
        for previous, current in zip(bars, bars[1:])
    )


def _bar_windows(
    bars: Sequence[KlineBar], width: int
) -> Tuple[Tuple[KlineBar, ...], ...]:
    if width <= 0 or len(bars) < width:
        return ()
    offset = len(bars) % width
    return tuple(
        tuple(bars[index : index + width])
        for index in range(offset, len(bars) - width + 1, width)
    )


def _optional_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else decimal_text(value)


def score_absorption_window(
    symbol: str,
    baseline_bars: Sequence[KlineBar],
    window_bars: Sequence[KlineBar],
    baseline_trades: Sequence[AggTrade],
    window_trades: Sequence[AggTrade],
    *,
    forward_outcomes: Optional[Mapping[str, Mapping[str, Optional[str]]]] = None,
) -> Dict[str, Any]:
    """Score one proved symbol/window against only its preceding baseline."""
    baseline_bars = tuple(baseline_bars)
    window_bars = tuple(window_bars)
    baseline_trades = tuple(baseline_trades)
    window_trades = tuple(window_trades)
    if not baseline_bars or not window_bars:
        raise AbsorptionError("baseline and scoring windows must contain bars")
    if len(baseline_bars) < len(window_bars):
        raise AbsorptionError("baseline is shorter than the scoring window")
    all_bars = baseline_bars + window_bars
    if any(bar.symbol != symbol for bar in all_bars):
        raise AbsorptionError("kline window contains another symbol")
    if any(trade.symbol != symbol for trade in baseline_trades + window_trades):
        raise AbsorptionError("aggTrades window contains another symbol")
    if not _contiguous(all_bars):
        raise AbsorptionError("baseline and scoring bars are not contiguous")

    historical_windows = _bar_windows(baseline_bars, len(window_bars))
    if not historical_windows:
        raise AbsorptionError("baseline has no comparable kline window")
    historical_metrics = tuple(_window_metrics(bars) for bars in historical_windows)
    current = _window_metrics(window_bars)
    historical_ranges = [metrics.range_pct for metrics in historical_metrics]
    historical_volumes = [metrics.quote_volume for metrics in historical_metrics]
    positive_volumes = [value for value in historical_volumes if value > 0]
    range_q20 = quantile(historical_ranges, 1, 5)
    range_median = quantile(historical_ranges, 1, 2)
    range_q90 = quantile(historical_ranges, 9, 10)
    volume_floor = quantile(positive_volumes, 1, 5)
    volume_q90 = quantile(positive_volumes, 9, 10)
    close_q20 = quantile([bar.close for bar in baseline_bars], 1, 5)

    historical_low = min(bar.low for bar in baseline_bars)
    historical_high = max(bar.high for bar in baseline_bars)
    if historical_high == historical_low:
        low_position = (
            Decimal("0")
            if window_bars[-1].close <= historical_low
            else Decimal("1")
        )
    else:
        with localcontext() as context:
            context.prec = 50
            low_position = (
                window_bars[-1].close - historical_low
            ) / (historical_high - historical_low)
    low_factor = Decimal("1") - _clamp01(low_position)
    compression_factor = _compression_rank(current.range_pct, historical_ranges)
    compression_ratio = _ratio(current.range_pct, range_median)
    volume_floor_ratio = _ratio(current.quote_volume, volume_floor)
    volume_factor = (
        None
        if volume_floor_ratio is None
        else _clamp01(volume_floor_ratio)
    )

    baseline_trade_index = TradeIndex(
        tuple(baseline_trades),
        tuple(trade.timestamp_ms for trade in baseline_trades),
    )
    baseline_window_imbalances: List[Decimal] = []
    baseline_window_size_medians: List[Decimal] = []
    baseline_window_size_p90s: List[Decimal] = []
    for bars in historical_windows:
        trades = baseline_trade_index.between(
            bars[0].open_ms, bars[-1].close_ms
        )
        imbalance = taker_imbalance(trades)
        sizes = [trade.quote_size for trade in trades]
        median_size = quantile(sizes, 1, 2)
        p90_size = quantile(sizes, 9, 10)
        if imbalance is not None:
            baseline_window_imbalances.append(imbalance)
        if median_size is not None:
            baseline_window_size_medians.append(median_size)
        if p90_size is not None:
            baseline_window_size_p90s.append(p90_size)

    buy_quote, sell_quote, buy_count, sell_count = taker_quote_totals(
        window_trades
    )
    current_imbalance = taker_imbalance(window_trades)
    baseline_imbalance = taker_imbalance(baseline_trades)
    baseline_imbalance_median = quantile(
        baseline_window_imbalances, 1, 2
    )
    imbalance_shift = (
        None
        if current_imbalance is None or baseline_imbalance is None
        else current_imbalance - baseline_imbalance
    )
    imbalance_factor = (
        None
        if current_imbalance is None
        else _empirical_rank(current_imbalance, baseline_window_imbalances)
    )
    if (
        imbalance_factor is not None
        and baseline_imbalance_median is not None
        and (
            current_imbalance <= Decimal("0")
            or current_imbalance <= baseline_imbalance_median
        )
    ):
        imbalance_factor = Decimal("0")

    baseline_sizes = [trade.quote_size for trade in baseline_trades]
    current_sizes = [trade.quote_size for trade in window_trades]
    baseline_size_median = quantile(baseline_sizes, 1, 2)
    baseline_size_p90 = quantile(baseline_sizes, 9, 10)
    current_size_median = quantile(current_sizes, 1, 2)
    current_size_p90 = quantile(current_sizes, 9, 10)
    median_size_ratio = _ratio(current_size_median, baseline_size_median)
    p90_size_ratio = _ratio(current_size_p90, baseline_size_p90)
    median_size_rank = (
        None
        if current_size_median is None
        else _empirical_rank(
            current_size_median, baseline_window_size_medians
        )
    )
    p90_size_rank = (
        None
        if current_size_p90 is None
        else _empirical_rank(current_size_p90, baseline_window_size_p90s)
    )
    size_factor: Optional[Decimal] = None
    if median_size_rank is not None and p90_size_rank is not None:
        size_factor = (median_size_rank + p90_size_rank) / Decimal("2")
        if (
            median_size_ratio is None
            or p90_size_ratio is None
            or (
                median_size_ratio <= Decimal("1")
                and p90_size_ratio <= Decimal("1")
            )
        ):
            size_factor = Decimal("0")

    at_multiweek_lows = (
        close_q20 is not None and window_bars[-1].close <= close_q20
    )
    price_flat = range_q20 is not None and current.range_pct <= range_q20
    volume_surging = (
        volume_q90 is not None and current.quote_volume > volume_q90
    )
    price_breakout = window_bars[-1].close > historical_high
    range_expanding = range_q90 is not None and current.range_pct > range_q90
    pumping = current.move_pct > 0 and (price_breakout or range_expanding)

    unknown: List[str] = []
    components = {
        "at_lows": low_factor,
        "range_compression": compression_factor,
        "volume_floor": volume_factor,
        "taker_imbalance_shift": imbalance_factor,
        "trade_size_shift": size_factor,
    }
    for name, value in components.items():
        if value is None:
            unknown.append(name)

    score = Decimal("0")
    if (
        not unknown
        and at_multiweek_lows
        and price_flat
        and not volume_surging
        and not pumping
    ):
        with localcontext() as context:
            context.prec = 50
            score = Decimal("100")
            for value in components.values():
                if value is None:
                    raise AbsorptionError("unknown score component escaped gate")
                score *= _clamp01(value)

    features: Dict[str, Any] = {
        "at_multiweek_lows": at_multiweek_lows,
        "low_reference_close_q20": _optional_text(close_q20),
        "low_position_in_history": decimal_text(low_position),
        "historical_low": decimal_text(historical_low),
        "historical_high": decimal_text(historical_high),
        "price_flat": price_flat,
        "window_range_pct": decimal_text(current.range_pct),
        "baseline_window_range_q20_pct": _optional_text(range_q20),
        "baseline_window_range_median_pct": _optional_text(range_median),
        "range_compression_ratio": _optional_text(compression_ratio),
        "window_price_move_pct": decimal_text(current.move_pct),
        "window_quote_volume": decimal_text(current.quote_volume),
        "baseline_window_volume_floor": _optional_text(volume_floor),
        "baseline_window_volume_q90": _optional_text(volume_q90),
        "volume_floor_ratio": _optional_text(volume_floor_ratio),
        "volume_surging": volume_surging,
        "taker_buy_quote": decimal_text(buy_quote),
        "taker_sell_quote": decimal_text(sell_quote),
        "taker_buy_count": buy_count,
        "taker_sell_count": sell_count,
        "taker_imbalance": _optional_text(current_imbalance),
        "baseline_taker_imbalance": _optional_text(baseline_imbalance),
        "taker_imbalance_shift": _optional_text(imbalance_shift),
        "trade_size_median_quote": _optional_text(current_size_median),
        "baseline_trade_size_median_quote": _optional_text(
            baseline_size_median
        ),
        "trade_size_median_shift_ratio": _optional_text(median_size_ratio),
        "trade_size_p90_quote": _optional_text(current_size_p90),
        "baseline_trade_size_p90_quote": _optional_text(baseline_size_p90),
        "trade_size_p90_shift_ratio": _optional_text(p90_size_ratio),
        "trade_count": len(window_trades),
        "pumping": pumping,
    }
    return {
        "schema": "absorption-v1",
        "kind": "absorption",
        "symbol": symbol,
        "window_minutes": len(window_bars),
        "window_start_utc": utc_text(window_bars[0].open_ms),
        "window_end_utc": utc_text(window_bars[-1].close_ms),
        "baseline_minutes": len(baseline_bars),
        "absorption_score": decimal_text(score),
        "score_components": {
            name: _optional_text(value) for name, value in components.items()
        },
        "unknown_features": unknown,
        "features": features,
        "forward_outcomes": dict(forward_outcomes or {}),
    }


def _forward_outcomes(
    bars: Sequence[KlineBar], end_index: int
) -> Dict[str, Dict[str, Optional[str]]]:
    signal = bars[end_index - 1]
    result: Dict[str, Dict[str, Optional[str]]] = {}
    for hours in FORWARD_HOURS:
        minutes = hours * 60
        future_index = end_index - 1 + minutes
        key = f"{hours}h"
        if future_index >= len(bars):
            result[key] = {
                "close_return_pct": None,
                "max_return_pct": None,
                "min_return_pct": None,
            }
            continue
        future = bars[end_index : future_index + 1]
        if (
            not future
            or not _contiguous((signal,) + tuple(future))
            or bars[future_index].close_ms - signal.close_ms
            != minutes * 60_000
        ):
            result[key] = {
                "close_return_pct": None,
                "max_return_pct": None,
                "min_return_pct": None,
            }
            continue
        with localcontext() as context:
            context.prec = 50
            close_return = (
                (bars[future_index].close / signal.close) - Decimal("1")
            ) * Decimal("100")
            max_return = (
                (max(bar.high for bar in future) / signal.close)
                - Decimal("1")
            ) * Decimal("100")
            min_return = (
                (min(bar.low for bar in future) / signal.close)
                - Decimal("1")
            ) * Decimal("100")
        result[key] = {
            "close_return_pct": decimal_text(close_return),
            "max_return_pct": decimal_text(max_return),
            "min_return_pct": decimal_text(min_return),
        }
    return result


def absorption_rows(
    klines: Mapping[str, Sequence[KlineBar]],
    agg_trades: Mapping[str, Sequence[AggTrade]],
    *,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    lookback_minutes: int = DEFAULT_LOOKBACK_DAYS * 1_440,
    step_minutes: int = DEFAULT_STEP_MINUTES,
) -> Tuple[Dict[str, Any], ...]:
    """Build deterministic rows for every proved symbol/window pair."""
    for value, name, maximum in (
        (window_minutes, "window", MAX_WINDOW_MINUTES),
        (step_minutes, "step", MAX_WINDOW_MINUTES),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise AbsorptionError(f"{name} must be in [1, {maximum}]")
    if (
        isinstance(lookback_minutes, bool)
        or not isinstance(lookback_minutes, int)
        or lookback_minutes < window_minutes
        or lookback_minutes > MAX_LOOKBACK_DAYS * 1_440
    ):
        raise AbsorptionError(
            f"lookback must be between window and {MAX_LOOKBACK_DAYS} days"
        )
    symbols = sorted(set(klines) & set(agg_trades))
    if not symbols:
        raise AbsorptionError("kline and aggTrades tapes share no symbol")

    rows: List[Dict[str, Any]] = []
    for symbol in symbols:
        bars = tuple(klines[symbol])
        trades = tuple(agg_trades[symbol])
        if any(bar.symbol != symbol for bar in bars):
            raise AbsorptionError(f"{symbol} kline map contains another symbol")
        if any(trade.symbol != symbol for trade in trades):
            raise AbsorptionError(f"{symbol} trade map contains another symbol")
        if any(
            current.open_ms <= previous.open_ms
            for previous, current in zip(bars, bars[1:])
        ):
            raise AbsorptionError(f"{symbol} klines are not strictly ordered")
        if any(
            (current.timestamp_ms, current.aggregate_id)
            < (previous.timestamp_ms, previous.aggregate_id)
            for previous, current in zip(trades, trades[1:])
        ):
            raise AbsorptionError(f"{symbol} aggTrades are not ordered")
        trade_index = TradeIndex(
            trades,
            tuple(trade.timestamp_ms for trade in trades),
        )
        first_start = lookback_minutes
        last_start = len(bars) - window_minutes
        for start in range(first_start, last_start + 1, step_minutes):
            baseline = bars[start - lookback_minutes : start]
            window = bars[start : start + window_minutes]
            if not _contiguous(baseline + window):
                continue
            baseline_window_trades = trade_index.between(
                baseline[0].open_ms, baseline[-1].close_ms
            )
            current_window_trades = trade_index.between(
                window[0].open_ms, window[-1].close_ms
            )
            end_index = start + window_minutes
            row = score_absorption_window(
                symbol,
                baseline,
                window,
                baseline_window_trades,
                current_window_trades,
                forward_outcomes=_forward_outcomes(bars, end_index),
            )
            rows.append(row)
    if not rows:
        raise AbsorptionError("tape has no proved absorption scoring windows")
    rows.sort(
        key=lambda row: (
            str(row["window_end_utc"]),
            str(row["symbol"]),
            int(row["window_minutes"]),
        )
    )
    return tuple(rows)


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(
                    json.dumps(
                        dict(row), sort_keys=True, separators=(",", ":")
                    )
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
        raise AbsorptionError(
            f"cannot write absorption output {target}: {error}"
        ) from None


def default_output_path(run_date: Optional[date] = None) -> Path:
    resolved = run_date or datetime.now(timezone.utc).date()
    return DEFAULT_OUTPUT_DIR / f"absorption-{resolved.isoformat()}.jsonl"


def run_absorption(
    data_dir: Path = DEFAULT_DATA_DIR,
    output_path: Optional[Path] = None,
    *,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    lookback_minutes: int = DEFAULT_LOOKBACK_DAYS * 1_440,
    step_minutes: int = DEFAULT_STEP_MINUTES,
) -> Tuple[Dict[str, Any], ...]:
    klines = load_klines(Path(data_dir))
    trades = load_agg_trades(Path(data_dir))
    rows = absorption_rows(
        klines,
        trades,
        window_minutes=window_minutes,
        lookback_minutes=lookback_minutes,
        step_minutes=step_minutes,
    )
    _write_jsonl(output_path or default_output_path(), rows)
    return rows


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


def _run_date(raw: str) -> date:
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from None
    if parsed.isoformat() != raw:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD")
    return parsed


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score pre-ignition absorption from kline and aggTrades zips."
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--date", type=_run_date)
    parser.add_argument(
        "--window",
        type=lambda raw: _bounded_positive(
            raw, "window", MAX_WINDOW_MINUTES
        ),
        default=DEFAULT_WINDOW_MINUTES,
        help="scoring-window minutes (default: 60)",
    )
    parser.add_argument(
        "--step",
        type=lambda raw: _bounded_positive(raw, "step", MAX_WINDOW_MINUTES),
        default=DEFAULT_STEP_MINUTES,
        help="minutes between windows (default: 60)",
    )
    parser.add_argument(
        "--lookback-days",
        type=lambda raw: _bounded_positive(
            raw, "lookback-days", MAX_LOOKBACK_DAYS
        ),
        default=DEFAULT_LOOKBACK_DAYS,
        help="preceding per-token baseline days (default: 21)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = argument_parser().parse_args(argv)
    run_date = args.date or datetime.now(timezone.utc).date()
    output_path = args.output_dir / f"absorption-{run_date.isoformat()}.jsonl"
    try:
        rows = run_absorption(
            args.data_dir,
            output_path,
            window_minutes=args.window,
            lookback_minutes=args.lookback_days * 1_440,
            step_minutes=args.step,
        )
    except AbsorptionError as error:
        print(f"absorption: {error}", file=sys.stderr)
        return 2
    ranked = sorted(
        rows,
        key=lambda row: (
            -Decimal(str(row["absorption_score"])),
            str(row["symbol"]),
            str(row["window_end_utc"]),
        ),
    )
    top = ranked[0]
    print(
        "ABSORPTION | "
        f"windows={len(rows)} | symbols={len({row['symbol'] for row in rows})} | "
        f"top={top['symbol']}:{top['absorption_score']} | output={output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
