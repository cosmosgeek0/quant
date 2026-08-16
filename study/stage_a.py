#!/usr/bin/env python3
"""Locate Volume Dataset v2 events from offline one-minute kline ZIPs.

The pass is deliberately token-local.  Only one symbol's selected venue tape
is resident at a time, while percentile ranks are maintained over that
symbol's own trailing 30 days.  Labels and every threshold are supplied by
``study.definitions``; this module only derives market facts.

Percentiles are exact empirical midranks against earlier bars.  Each volume
acceleration is the recent window's per-minute mean divided by the disjoint
earlier 30-day per-minute mean.  Dead-regime volume/activity use the minute
immediately before the candidate; ignition facts include the candidate.
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
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import (
    Any,
    Deque,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)


try:
    from study import tape_pull
except ModuleNotFoundError as error:
    if error.name != "study":
        raise
    import tape_pull  # type: ignore


try:
    try:
        from study.definitions import DEFINITIONS as _DEFAULT_DEFINITIONS
        from study.definitions import is_already_pumped
        from study.definitions import is_dead_regime
        from study.definitions import is_false_ignition
        from study.definitions import is_genuine_runner
        from study.definitions import is_ignition
        from study.definitions import is_slow_ramp
    except ModuleNotFoundError as error:
        if error.name != "study":
            raise
        from definitions import DEFINITIONS as _DEFAULT_DEFINITIONS
        from definitions import is_already_pumped
        from definitions import is_dead_regime
        from definitions import is_false_ignition
        from definitions import is_genuine_runner
        from definitions import is_ignition
        from definitions import is_slow_ramp
except ModuleNotFoundError as error:
    if error.name not in {"study.definitions", "definitions"}:
        raise
    _DEFAULT_DEFINITIONS = None
    is_already_pumped = None
    is_dead_regime = None
    is_false_ignition = None
    is_genuine_runner = None
    is_ignition = None
    is_slow_ramp = None


STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = STUDY_DIR / "data"
DEFAULT_OUTPUT_PATH = STUDY_DIR / "out" / "v2" / "events.jsonl"
FEATURE_WINDOWS = (1, 5, 10, 15, 60)
FORWARD_WINDOWS = (5, 15, 60)
MINUTE_MS = 60_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ARCHIVE_NAME = re.compile(
    r"^(?P<symbol>.+)-1m-"
    r"(?P<period>\d{4}-\d{2}(?:-\d{2})?)\.zip$",
)
MEMBER_NAME = re.compile(
    r"^(?P<symbol>.+)-1m-.*\.csv$",
)
LABEL_NAMES: Tuple[str, ...] = (
    "DeadRegime",
    "Ignition",
    "GenuineRunner",
    "FalseIgnition",
    "SlowRamp",
    "AlreadyPumped",
)
_UNSET = object()


class StageAError(Exception):
    """The offline tape, definitions, or output cannot be proved valid."""


@dataclass(frozen=True)
class Kline:
    symbol: str
    venue: str
    open_ms: int
    close_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    quote_volume: Decimal
    trade_count: Optional[int]


@dataclass(frozen=True)
class MarketTape:
    symbol: str
    venue_class: str
    source_venue: str
    archives: Tuple[Path, ...]


@dataclass(frozen=True)
class AnalyzedMinute:
    bar: Kline
    state: Mapping[str, Any]
    label_evidence: Mapping[str, Any]
    labels: Mapping[str, bool]


def decimal_text(value: Decimal) -> str:
    """Render one finite Decimal without exponent notation."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise StageAError("cannot render a non-finite Decimal")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0"}:
        return "0"
    return rendered


def utc_text(epoch_ms: int) -> str:
    value = EPOCH + timedelta(milliseconds=epoch_ms)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _decimal(raw: Any, source: str, *, positive: bool) -> Decimal:
    if isinstance(raw, bool) or isinstance(raw, float):
        raise StageAError(f"{source} is not Decimal text")
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise StageAError(f"{source} is not Decimal text") from None
    if not value.is_finite() or value < 0 or (positive and value <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise StageAError(f"{source} is not a finite {qualifier} Decimal")
    return value


def _timestamp_ms(raw: Any, source: str) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise StageAError(f"{source} is not an integer timestamp") from None
    if value >= 100_000_000_000_000:
        value //= 1_000
    if value < 100_000_000_000 or value >= 100_000_000_000_000:
        raise StageAError(f"{source} is outside the supported epoch range")
    return value


def _trade_count(raw: Any, source: str) -> Optional[int]:
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        raise StageAError(f"{source} is not an integer trade count") from None
    if value < 0:
        raise StageAError(f"{source} is a negative trade count")
    return value


def _venue_from_path(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    if "spot" in parts:
        return "spot"
    if "futures" in parts:
        return "futures"
    return "spot"


def _symbol(raw: Any, source: str) -> str:
    if (
        not isinstance(raw, str)
        or not 5 <= len(raw) <= 100
        or not raw.endswith("USDT")
        or not raw.isalnum()
        or raw.upper() != raw
    ):
        raise StageAError(f"{source} is not a safe uppercase USDT symbol")
    return raw


def discover_market_tapes(data_dir: Path = DEFAULT_DATA_DIR) -> Tuple[MarketTape, ...]:
    """Discover canonical token tapes without retaining any kline rows."""
    root = Path(data_dir)
    if not root.is_dir():
        raise StageAError(f"data directory does not exist: {root}")
    found: Dict[str, Dict[str, List[Path]]] = {}
    for path in root.rglob("*.zip"):
        if not path.is_file():
            continue
        match = ARCHIVE_NAME.fullmatch(path.name)
        if match is None:
            continue
        symbol = _symbol(match.group("symbol"), path.name)
        venue = _venue_from_path(path)
        found.setdefault(symbol, {}).setdefault(venue, []).append(path)
    if not found:
        raise StageAError(f"data directory has no 1m kline zips: {root}")

    markets: List[MarketTape] = []
    for symbol in sorted(found):
        by_venue = found[symbol]
        has_spot = bool(by_venue.get("spot"))
        has_futures = bool(by_venue.get("futures"))
        if has_spot and has_futures:
            venue_class = "both"
            source_venue = "spot"
        elif has_spot:
            venue_class = "spot_only"
            source_venue = "spot"
        elif has_futures:
            venue_class = "futures_only"
            source_venue = "futures"
        else:
            raise StageAError(f"{symbol} has no supported venue tape")
        markets.append(
            MarketTape(
                symbol=symbol,
                venue_class=venue_class,
                source_venue=source_venue,
                archives=tuple(sorted(by_venue[source_venue])),
            )
        )
    return tuple(markets)


def _rows_from_member(
    archive: Path,
    member_name: str,
    handle: Iterable[str],
    symbol: str,
    venue: str,
) -> Iterator[Kline]:
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
                raise StageAError(
                    f"{source}:{line_number} has fewer than eight kline columns"
                )
            line_source = f"{source}:{line_number}"
            open_ms = _timestamp_ms(row[0], f"{line_source} open time")
            close_ms = _timestamp_ms(row[6], f"{line_source} close time")
            if close_ms - open_ms not in {59_999, 60_000}:
                raise StageAError(
                    f"{line_source} is not a proved one-minute kline"
                )
            open_price = _decimal(row[1], f"{line_source} open", positive=True)
            high = _decimal(row[2], f"{line_source} high", positive=True)
            low = _decimal(row[3], f"{line_source} low", positive=True)
            close = _decimal(row[4], f"{line_source} close", positive=True)
            quote_volume = _decimal(
                row[7], f"{line_source} quote volume", positive=False
            )
            activity = (
                _trade_count(row[8], f"{line_source} trade count")
                if len(row) >= 9
                else None
            )
            if high < max(open_price, low, close) or low > min(
                open_price, high, close
            ):
                raise StageAError(f"{line_source} has inconsistent OHLC")
            found = True
            yield Kline(
                symbol=symbol,
                venue=venue,
                open_ms=open_ms,
                close_ms=close_ms,
                open=open_price,
                high=high,
                low=low,
                close=close,
                quote_volume=quote_volume,
                trade_count=activity,
            )
        if not found:
            raise StageAError(f"{source} has no kline rows")
    except csv.Error as error:
        raise StageAError(f"{source} has invalid CSV: {error}") from None


class _ChunkStream(io.RawIOBase):
    """Expose shared verified chunks as a buffered-reader source."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        super().__init__()
        self._chunks = iter(chunks)
        self._pending = memoryview(b"")

    def readable(self) -> bool:
        return True

    def readinto(self, target: Any) -> int:
        view = memoryview(target)
        while len(self._pending) == 0:
            try:
                self._pending = memoryview(next(self._chunks))
            except StopIteration:
                return 0
        size = min(len(view), len(self._pending))
        view[:size] = self._pending[:size]
        self._pending = self._pending[size:]
        return size

    def close(self) -> None:
        close_chunks = getattr(self._chunks, "close", None)
        if callable(close_chunks):
            close_chunks()
        super().close()


def _read_archive(path: Path, symbol: str, venue: str) -> Iterator[Kline]:
    try:
        with zipfile.ZipFile(path, "r") as zipped:
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
                raise StageAError(f"{path.name} has no CSV member")
            for member in members:
                if member.flag_bits & 0x1:
                    raise StageAError(f"{path.name}:{member.filename} is encrypted")
                match = MEMBER_NAME.fullmatch(Path(member.filename).name)
                if (
                    match is None
                    or _symbol(match.group("symbol"), member.filename) != symbol
                ):
                    raise StageAError(
                        f"{path.name}:{member.filename} does not match {symbol}"
                    )
                identity = f"{path.name}:{member.filename}"
                try:
                    chunks = tape_pull.stream_verified_member(
                        zipped, member, tape_pull.MAX_MEMBER_BYTES
                    )
                    raw = io.BufferedReader(_ChunkStream(chunks))
                    with io.TextIOWrapper(
                        raw, encoding="utf-8-sig", newline=""
                    ) as handle:
                        yield from _rows_from_member(
                            path, member.filename, handle, symbol, venue
                        )
                except tape_pull.ArchiveError as error:
                    details = ""
                    if error.measured_value is not None:
                        details += f" | measured={error.measured_value}"
                    if error.bound is not None:
                        details += f" | bound={error.bound}"
                    raise StageAError(
                        f"{identity} {error}{details}"
                    ) from None
    except StageAError:
        raise
    except (
        OSError,
        UnicodeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise StageAError(f"cannot read {path.name}: {error}") from None


def read_market_bars(market: MarketTape) -> Tuple[Kline, ...]:
    """Read, validate, de-duplicate, and order one canonical token tape."""
    indexed: Dict[int, Kline] = {}
    for archive in market.archives:
        for bar in _read_archive(archive, market.symbol, market.source_venue):
            existing = indexed.get(bar.open_ms)
            if existing is not None and existing != bar:
                raise StageAError(
                    f"conflicting duplicate {market.symbol} kline at "
                    f"{utc_text(bar.open_ms)}"
                )
            indexed[bar.open_ms] = bar
    if not indexed:
        raise StageAError(f"{market.symbol} tape has no kline rows")
    return tuple(indexed[key] for key in sorted(indexed))


def _pct_change(start: Decimal, end: Decimal) -> Decimal:
    if start <= 0 or end <= 0:
        raise StageAError("price change requires positive prices")
    with localcontext() as context:
        context.prec = 50
        return ((end / start) - Decimal("1")) * Decimal("100")


def _bar_range_pct(bar: Kline) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return ((bar.high - bar.low) / bar.close) * Decimal("100")


class _Fenwick:
    def __init__(self, size: int) -> None:
        self._tree = [0] * (size + 1)

    def add(self, index: int, delta: int) -> None:
        cursor = index + 1
        while cursor < len(self._tree):
            self._tree[cursor] += delta
            cursor += cursor & -cursor

    def prefix(self, end: int) -> int:
        total = 0
        cursor = end
        while cursor > 0:
            total += self._tree[cursor]
            cursor -= cursor & -cursor
        return total


class _TrailingRanks:
    """Exact midrank percentiles over one token's trailing time window."""

    def __init__(self, values: Iterable[Any], history_ms: int) -> None:
        if history_ms <= 0:
            raise StageAError("percentile history must be positive")
        self._coordinates = sorted(set(values))
        self._tree = _Fenwick(len(self._coordinates))
        self._entries: Deque[Tuple[int, Any]] = deque()
        self._history_ms = history_ms
        self.count = 0

    def _index(self, value: Any) -> int:
        index = bisect.bisect_left(self._coordinates, value)
        if index >= len(self._coordinates) or self._coordinates[index] != value:
            raise StageAError("percentile coordinate is absent")
        return index

    def expire(self, open_ms: int) -> None:
        cutoff = open_ms - self._history_ms
        while self._entries and self._entries[0][0] < cutoff:
            _, value = self._entries.popleft()
            self._tree.add(self._index(value), -1)
            self.count -= 1

    def percentile(self, value: Any) -> Optional[Decimal]:
        if self.count == 0:
            return None
        left = bisect.bisect_left(self._coordinates, value)
        right = bisect.bisect_right(self._coordinates, value)
        less = self._tree.prefix(left)
        equal = self._tree.prefix(right) - less
        with localcontext() as context:
            context.prec = 50
            return (
                (Decimal(less) + Decimal(equal) / Decimal("2"))
                * Decimal("100")
                / Decimal(self.count)
            )

    def add(self, open_ms: int, value: Any) -> None:
        self._tree.add(self._index(value), 1)
        self._entries.append((open_ms, value))
        self.count += 1


def _definition_registry(value: Any) -> Mapping[str, Any]:
    selected = _DEFAULT_DEFINITIONS if value is _UNSET else value
    if selected is None:
        raise StageAError("study.definitions is required for Stage A labels")
    if not isinstance(selected, Mapping):
        raise StageAError("definitions registry is not a mapping")
    missing = [name for name in LABEL_NAMES if name not in selected]
    if missing:
        raise StageAError(
            "definitions registry is missing " + ",".join(missing)
        )
    return selected


def _forward_extrema(
    bars: Sequence[Kline], horizon: int
) -> Tuple[Optional[Tuple[Decimal, Decimal, int]], ...]:
    """Return future high, low, and earliest peak offset in linear time."""
    result: List[Optional[Tuple[Decimal, Decimal, int]]] = [None] * len(bars)
    segment_start = 0
    while segment_start < len(bars):
        segment_end = segment_start
        while (
            segment_end + 1 < len(bars)
            and bars[segment_end + 1].open_ms - bars[segment_end].open_ms
            == MINUTE_MS
        ):
            segment_end += 1
        maxima: Deque[int] = deque()
        minima: Deque[int] = deque()
        added = segment_start
        for index in range(segment_start, segment_end + 1):
            right = index + horizon
            while added <= right and added <= segment_end:
                if added > index:
                    while maxima and bars[maxima[-1]].high < bars[added].high:
                        maxima.pop()
                    maxima.append(added)
                    while minima and bars[minima[-1]].low > bars[added].low:
                        minima.pop()
                    minima.append(added)
                added += 1
            while maxima and maxima[0] <= index:
                maxima.popleft()
            while minima and minima[0] <= index:
                minima.popleft()
            if right <= segment_end and maxima and minima:
                result[index] = (
                    bars[maxima[0]].high,
                    bars[minima[0]].low,
                    maxima[0] - index,
                )
        segment_start = segment_end + 1
    return tuple(result)


def _prefix_sums(bars: Sequence[Kline]) -> Tuple[Decimal, ...]:
    values = [Decimal("0")]
    total = Decimal("0")
    for bar in bars:
        total += bar.quote_volume
        values.append(total)
    return tuple(values)


def _rolling_max_price_change(
    bars: Sequence[Kline], window: int
) -> Tuple[Optional[Decimal], ...]:
    """Return the greatest non-negative close change in each trailing window."""
    result: List[Optional[Decimal]] = [None] * len(bars)
    candidates: Deque[Tuple[int, Decimal]] = deque()
    run_start = 0
    for index, bar in enumerate(bars):
        if (
            index == 0
            or bar.open_ms - bars[index - 1].open_ms != MINUTE_MS
        ):
            run_start = index
            candidates.clear()
            continue
        change = _pct_change(bars[index - 1].close, bar.close)
        while candidates and candidates[-1][1] < change:
            candidates.pop()
        candidates.append((index, change))
        first = index - window + 1
        while candidates and candidates[0][0] < first:
            candidates.popleft()
        if index - run_start >= window and candidates:
            result[index] = max(Decimal("0"), candidates[0][1])
    return tuple(result)


def _volume_acceleration(
    prefix: Sequence[Decimal],
    open_times: Sequence[int],
    index: int,
    window: int,
    run_start: int,
    history_ms: int,
) -> Optional[Decimal]:
    """Compare the recent per-minute rate with a disjoint trailing baseline."""
    if index - run_start + 1 < window:
        return None
    recent_start = index + 1 - window
    baseline_start = bisect.bisect_left(
        open_times, open_times[index] - history_ms, 0, recent_start
    )
    baseline_count = recent_start - baseline_start
    if baseline_count <= 0:
        return None
    recent = prefix[index + 1] - prefix[recent_start]
    baseline = prefix[recent_start] - prefix[baseline_start]
    if baseline <= 0:
        return None
    with localcontext() as context:
        context.prec = 50
        return (
            recent
            * Decimal(baseline_count)
            / (Decimal(window) * baseline)
        )


def _serialize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, Mapping):
        return {str(key): _serialize(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(nested) for nested in value]
    if value is None or type(value) in {bool, int, str}:
        return value
    raise StageAError(f"cannot serialize Stage A value of type {type(value).__name__}")


def analyze_bars(
    bars: Sequence[Kline],
    *,
    definitions_registry: Any = _UNSET,
) -> Iterator[AnalyzedMinute]:
    """Yield exact facts and imported-definition labels for one token tape."""
    registry = _definition_registry(definitions_registry)
    dead_definition = registry["DeadRegime"]
    ignition_definition = registry["Ignition"]
    runner_definition = registry["GenuineRunner"]
    false_definition = registry["FalseIgnition"]
    slow_definition = registry["SlowRamp"]
    pumped_definition = registry["AlreadyPumped"]
    try:
        flat_threshold = _decimal(
            dead_definition.flat_candle_max_range_pct,
            "DeadRegime.flat_candle_max_range_pct",
            positive=False,
        )
        meaningful_threshold = _decimal(
            dead_definition.meaningful_move_pct,
            "DeadRegime.meaningful_move_pct",
            positive=False,
        )
        history_days = dead_definition.percentile_history_days
        if (
            type(history_days) is not int
            or history_days <= 0
            or ignition_definition.percentile_history_days != history_days
        ):
            raise StageAError(
                "DeadRegime and Ignition require one positive history period"
            )
        flat_windows = (
            ("1h", dead_definition.flat_1h_minutes),
            ("6h", dead_definition.flat_6h_minutes),
            ("24h", dead_definition.flat_24h_minutes),
        )
        if any(type(window) is not int or window <= 0 for _, window in flat_windows):
            raise StageAError("DeadRegime flat windows must be positive integers")
        history_ms = history_days * 24 * 60 * MINUTE_MS
    except AttributeError as error:
        raise StageAError(f"definitions registry is incompatible: {error}") from None

    ordered = tuple(bars)
    if not ordered:
        return
    symbol = ordered[0].symbol
    venue = ordered[0].venue
    for previous, current in zip(ordered, ordered[1:]):
        if current.open_ms <= previous.open_ms:
            raise StageAError(f"{symbol} bars are not strictly ordered")
    if any(bar.symbol != symbol or bar.venue != venue for bar in ordered):
        raise StageAError("one analysis pass contains multiple token tapes")

    prefix_volume = _prefix_sums(ordered)
    open_times = tuple(bar.open_ms for bar in ordered)
    flat_prefix = [0]
    positive_prefix = [0]
    active_prefix = [0]
    missing_activity_prefix = [0]
    for bar in ordered:
        range_pct = _bar_range_pct(bar)
        flag = int(range_pct <= flat_threshold)
        flat_prefix.append(flat_prefix[-1] + flag)
        positive_prefix.append(
            positive_prefix[-1] + int(bar.close > bar.open)
        )
        active_prefix.append(
            active_prefix[-1]
            + int(bar.trade_count is not None and bar.trade_count > 0)
        )
        missing_activity_prefix.append(
            missing_activity_prefix[-1] + int(bar.trade_count is None)
        )

    volumes = _TrailingRanks(
        (bar.quote_volume for bar in ordered), history_ms
    )
    activities = _TrailingRanks(
        (bar.trade_count for bar in ordered if bar.trade_count is not None),
        history_ms,
    )
    extrema = {
        horizon: _forward_extrema(ordered, horizon)
        for horizon in FORWARD_WINDOWS
    }
    max_price_change_60m = _rolling_max_price_change(ordered, 60)

    run_start = 0
    last_meaningful: Optional[int] = None
    previous_volume_percentile: Optional[Decimal] = None
    previous_activity_percentile: Optional[Decimal] = None
    for index, bar in enumerate(ordered):
        if (
            index > 0
            and bar.open_ms - ordered[index - 1].open_ms != MINUTE_MS
        ):
            run_start = index
            last_meaningful = None
            previous_volume_percentile = None
            previous_activity_percentile = None
        contiguous_before = index - run_start

        flat_pct: Dict[str, Optional[Decimal]] = {}
        for name, window in flat_windows:
            if contiguous_before < window:
                flat_pct[name] = None
            else:
                count = flat_prefix[index] - flat_prefix[index - window]
                with localcontext() as context:
                    context.prec = 50
                    flat_pct[name] = (
                        Decimal(count) * Decimal("100") / Decimal(window)
                    )

        if last_meaningful is None:
            minutes_since_move: Optional[int] = contiguous_before
            censored = True
        else:
            minutes_since_move = index - last_meaningful
            censored = False

        volume_acceleration: Dict[str, Optional[Decimal]] = {}
        for window in FEATURE_WINDOWS:
            volume_acceleration[f"{window}m"] = _volume_acceleration(
                prefix_volume,
                open_times,
                index,
                window,
                run_start,
                history_ms,
            )

        current_change: Optional[Decimal] = None
        previous_change: Optional[Decimal] = None
        if contiguous_before >= 1:
            current_change = _pct_change(ordered[index - 1].close, bar.close)
        if contiguous_before >= 2:
            previous_change = _pct_change(
                ordered[index - 2].close, ordered[index - 1].close
            )
        price_acceleration = (
            None
            if current_change is None or previous_change is None
            else current_change - previous_change
        )

        price_change: Dict[str, Optional[Decimal]] = {}
        prior_price_change: Dict[str, Optional[Decimal]] = {}
        for window in FEATURE_WINDOWS:
            price_change[f"{window}m"] = (
                _pct_change(ordered[index - window].close, bar.close)
                if contiguous_before >= window
                else None
            )
            prior_price_change[f"{window}m"] = (
                _pct_change(
                    ordered[index - 1 - window].close,
                    ordered[index - 1].close,
                )
                if contiguous_before >= window + 1
                else None
            )

        volumes.expire(bar.open_ms)
        volume_history_count = volumes.count
        volume_percentile = volumes.percentile(bar.quote_volume)
        activities.expire(bar.open_ms)
        activity_history_count = activities.count
        activity_percentile = (
            None
            if bar.trade_count is None
            else activities.percentile(bar.trade_count)
        )

        forward_returns: Dict[str, Optional[Decimal]] = {}
        mfe: Dict[str, Optional[Decimal]] = {}
        mae: Dict[str, Optional[Decimal]] = {}
        time_to_peak: Dict[str, Optional[int]] = {}
        for horizon in FORWARD_WINDOWS:
            outcome = extrema[horizon][index]
            if outcome is None:
                forward_returns[f"{horizon}m"] = None
                mfe[f"{horizon}m"] = None
                mae[f"{horizon}m"] = None
                time_to_peak[f"{horizon}m"] = None
                continue
            future_high, future_low, peak_offset = outcome
            forward_returns[f"{horizon}m"] = _pct_change(
                bar.close, ordered[index + horizon].close
            )
            mfe[f"{horizon}m"] = max(
                Decimal("0"), _pct_change(bar.close, future_high)
            )
            mae[f"{horizon}m"] = min(
                Decimal("0"), _pct_change(bar.close, future_low)
            )
            time_to_peak[f"{horizon}m"] = peak_offset

        positive_next_15m: Optional[int] = None
        if extrema[15][index] is not None:
            positive_next_15m = (
                positive_prefix[index + 16] - positive_prefix[index + 1]
            )
        active_next_5m: Optional[int] = None
        if (
            extrema[5][index] is not None
            and missing_activity_prefix[index + 6]
            - missing_activity_prefix[index + 1]
            == 0
        ):
            active_next_5m = (
                active_prefix[index + 6] - active_prefix[index + 1]
            )
        positive_candles_60m: Optional[int] = None
        if contiguous_before >= 60:
            positive_candles_60m = (
                positive_prefix[index + 1] - positive_prefix[index - 59]
            )

        state: Dict[str, Any] = {
            "flat_candle_pct": flat_pct,
            "minutes_since_meaningful_move": minutes_since_move,
            "minutes_since_meaningful_move_censored": censored,
            "volume_percentile": volume_percentile,
            "volume_percentile_30d": volume_percentile,
            "volume_history_count": volume_history_count,
            "activity_percentile": activity_percentile,
            "activity_percentile_30d": activity_percentile,
            "activity_history_count": activity_history_count,
            "pre_ignition_volume_percentile_30d": (
                previous_volume_percentile
            ),
            "pre_ignition_activity_percentile_30d": (
                previous_activity_percentile
            ),
            "volume_acceleration": volume_acceleration,
            "price_acceleration_5m_pct": price_change["5m"],
            "price_acceleration_1m_delta_pct": price_acceleration,
            "price_change_pct": price_change,
            "prior_price_change_pct": prior_price_change,
            "max_1m_price_change_60m_pct": max_price_change_60m[index],
            "positive_candles_60m": positive_candles_60m,
        }
        facts: Dict[str, Any] = dict(state)
        for name, value in flat_pct.items():
            facts[f"flat_candle_pct_{name}"] = value
            facts[f"flat_pct_{name}"] = value
        facts["minutes_since_last_meaningful_move"] = minutes_since_move
        facts["volume_percentile_30d"] = volume_percentile
        facts["activity_percentile_30d"] = activity_percentile
        for name, value in volume_acceleration.items():
            facts[f"volume_acceleration_{name}"] = value
        facts["price_acceleration_5m_pct"] = price_change["5m"]
        for name, value in price_change.items():
            facts[f"price_change_{name}_pct"] = value
            facts[f"return_{name}_pct"] = value
        for name, value in prior_price_change.items():
            facts[f"prior_price_change_{name}_pct"] = value
            facts[f"pre_move_{name}_pct"] = value
        facts["price_change_prior_60m_pct"] = prior_price_change["60m"]
        for name, value in forward_returns.items():
            facts[f"forward_return_{name}_pct"] = value
        for name, value in mfe.items():
            facts[f"mfe_{name}_pct"] = value
            facts[f"max_forward_return_{name}_pct"] = value
        for name, value in mae.items():
            facts[f"mae_{name}_pct"] = value
            facts[f"min_forward_return_{name}_pct"] = value
        for name, value in time_to_peak.items():
            facts[f"time_to_peak_{name}_min"] = value
        facts["current_price_move_pct"] = current_change
        facts["price_move_pct"] = current_change
        facts["max_forward_return_pct"] = mfe["60m"]
        facts["min_forward_return_pct"] = mae["60m"]
        facts["max_favorable_excursion_15m_pct"] = mfe["15m"]
        facts["max_favorable_excursion_60m_pct"] = mfe["60m"]
        facts["max_adverse_excursion_60m_pct"] = mae["60m"]
        facts["positive_candles_next_15m"] = positive_next_15m
        facts["active_minutes_next_5m"] = active_next_5m
        facts["max_1m_price_change_60m_pct"] = max_price_change_60m[index]
        facts["positive_candles_60m"] = positive_candles_60m

        pre_state_keys = (
            "flat_candle_pct_1h",
            "flat_candle_pct_6h",
            "flat_candle_pct_24h",
            "minutes_since_meaningful_move",
            "volume_percentile_30d",
            "activity_percentile_30d",
            "volume_acceleration_1m",
            "volume_acceleration_5m",
            "volume_acceleration_10m",
            "volume_acceleration_15m",
            "volume_acceleration_60m",
            "price_acceleration_5m_pct",
            "price_change_1m_pct",
            "price_change_5m_pct",
            "price_change_10m_pct",
            "price_change_15m_pct",
            "price_change_60m_pct",
            "price_change_prior_60m_pct",
        )
        state.update({key: facts[key] for key in pre_state_keys})
        label_evidence: Dict[str, Any] = {
            "forward_return_pct": forward_returns,
            "mfe_pct": mfe,
            "mae_pct": mae,
            "time_to_peak_min": time_to_peak,
            "forward_return_15m_pct": forward_returns["15m"],
            "forward_return_60m_pct": forward_returns["60m"],
            "max_favorable_excursion_15m_pct": mfe["15m"],
            "max_favorable_excursion_60m_pct": mfe["60m"],
            "max_adverse_excursion_60m_pct": mae["60m"],
            "positive_candles_next_15m": positive_next_15m,
            "active_minutes_next_5m": active_next_5m,
        }

        labels: Dict[str, bool] = {}
        dead_facts = dict(facts)
        dead_facts["volume_percentile_30d"] = previous_volume_percentile
        dead_facts["activity_percentile_30d"] = previous_activity_percentile
        labels["DeadRegime"] = is_dead_regime(
            dead_facts, dead_definition
        )
        facts["dead_regime"] = labels["DeadRegime"]
        labels["Ignition"] = is_ignition(
            facts, ignition_definition
        )
        facts["ignition"] = labels["Ignition"]
        labels["GenuineRunner"] = is_genuine_runner(
            facts, runner_definition
        )
        facts["genuine_runner"] = labels["GenuineRunner"]
        labels["FalseIgnition"] = is_false_ignition(
            facts, false_definition
        )
        facts["false_ignition"] = labels["FalseIgnition"]
        labels["SlowRamp"] = is_slow_ramp(
            facts, slow_definition
        )
        facts["slow_ramp"] = labels["SlowRamp"]
        labels["AlreadyPumped"] = is_already_pumped(
            facts, pumped_definition
        )
        facts["already_pumped"] = labels["AlreadyPumped"]
        state["dead_regime"] = labels["DeadRegime"]

        volumes.add(bar.open_ms, bar.quote_volume)
        if bar.trade_count is not None:
            activities.add(bar.open_ms, bar.trade_count)
        previous_volume_percentile = volume_percentile
        previous_activity_percentile = activity_percentile
        if current_change is not None and abs(current_change) > meaningful_threshold:
            last_meaningful = index

        yield AnalyzedMinute(
            bar=bar,
            state=state,
            label_evidence=label_evidence,
            labels=labels,
        )


def _primary_label(labels: Mapping[str, bool]) -> Optional[str]:
    for label in (
        "GenuineRunner",
        "FalseIgnition",
        "SlowRamp",
        "AlreadyPumped",
        "Ignition",
    ):
        if labels.get(label) is True:
            return label
    return None


def event_row(analysis: AnalyzedMinute, market: MarketTape) -> Optional[Dict[str, Any]]:
    """Build one stable JSON-ready row, or None for an unlabelled minute."""
    primary = _primary_label(analysis.labels)
    if primary is None:
        return None
    labels = [label for label in LABEL_NAMES if analysis.labels[label]]
    return {
        "schema": "volume-dataset-v2-stage-a-event",
        "kind": "stage_a_event",
        "is_event": True,
        "event_id": f"{market.symbol}:{analysis.bar.open_ms}",
        "symbol": market.symbol,
        "venue_class": market.venue_class,
        "source_venue": market.source_venue,
        "open_ms": analysis.bar.open_ms,
        "utc": utc_text(analysis.bar.open_ms),
        "label": primary,
        "labels": labels,
        "label_flags": dict(analysis.labels),
        "dead_regime": analysis.labels["DeadRegime"],
        "volume_percentile_30d": _serialize(
            analysis.state["volume_percentile_30d"]
        ),
        "activity_percentile_30d": _serialize(
            analysis.state["activity_percentile_30d"]
        ),
        "pre_state": _serialize(analysis.state),
        "label_evidence": _serialize(analysis.label_evidence),
    }


def run_stage_a(
    data_dir: Path = DEFAULT_DATA_DIR,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    *,
    definitions_registry: Any = _UNSET,
) -> Dict[str, Any]:
    """Stream deterministic labelled events to an atomic JSONL output."""
    registry = _definition_registry(definitions_registry)
    markets = discover_market_tapes(Path(data_dir))
    target = Path(output_path)
    temporary = target.with_name(target.name + ".tmp")
    event_count = 0
    minute_count = 0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            for market in markets:
                bars = read_market_bars(market)
                for analysis in analyze_bars(
                    bars,
                    definitions_registry=registry,
                ):
                    minute_count += 1
                    row = event_row(analysis, market)
                    if row is None:
                        continue
                    json.dump(row, handle, sort_keys=True, separators=(",", ":"))
                    handle.write("\n")
                    event_count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except (OSError, TypeError, ValueError) as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise StageAError(f"cannot write Stage A output {target}: {error}") from None
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return {
        "schema": "volume-dataset-v2-stage-a-summary",
        "symbol_count": len(markets),
        "minute_count": minute_count,
        "event_count": event_count,
        "output": str(target),
    }


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Locate Volume Dataset v2 events from offline 1m kline ZIPs."
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = argument_parser().parse_args(argv)
    try:
        summary = run_stage_a(args.data_dir, args.output)
    except StageAError as error:
        print(f"stage-a: {error}", file=sys.stderr)
        return 2
    print(
        "STAGE-A | "
        f"symbols={summary['symbol_count']} | "
        f"minutes={summary['minute_count']} | "
        f"events={summary['event_count']} | output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
