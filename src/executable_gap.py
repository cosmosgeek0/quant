"""Pure executable NORMAL-basis reads and bounded in-memory history.

The input seam follows ``market_stream``: raw book-ticker rows are reduced from
``bidPrice``/``askPrice`` (market_stream.py:753-761), then published as the
Decimal-text fields ``spot_bid``, ``spot_ask``, ``perp_bid``, and ``perp_ask``
(market_stream.py:1214-1238).  A source is an upstream producer or feed; an
observation is one atomic record emitted by that source.  Source labels do not
prove that two quotes came from different observations.  ``VenueQuote`` binds
each price pair to its venue, observation time, and a typed observation-record
identity.  Quotes extracted from one stream record share that identity even if
the caller supplies unequal venue times.  A caller with only one shared record
uses ``read_executable_gap_shared_observation``; its result records that
paired-venue skew was not checked.

This module implements BASIS-LOCKIN-SPEC rows 023-024, 050, 089, 092, and 094.
Entry is Futures bid minus Spot ask over Spot ask.  Exit is Futures ask minus
Spot bid over Spot bid.  Both use the canonical basis-kernel return functions;
mark and midpoint prices are not accepted by this API.

The default five-second age bound is the landed dislocation-event actionability
bound (basis_events.py:145-148).  The one-second paired-quote skew bound is the
stream coherence default (market_stream.py:919-950).  Either old quote, excess
skew, or a crossed or locked venue book returns ``GapRefusal``.

``WindowStats`` and ``parse_window`` mirror the recorder reader's public shape
and inclusive-window behavior (study/basis_history_reader.py:73-76,234-275).
They are kept local because a runtime ``src`` module should not depend on the
study CLI module.  The ring keeps 481 cuts per symbol: both endpoints of 24
hours at that reader's documented three-minute cadence.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Deque, Dict, Literal, Mapping, Optional, Tuple, Union

from basis_events import ACTIONABLE_EVENT_MAX_AGE_MS, SYMBOL_RE
from basis_kernel import entry_gap_pct as kernel_entry_gap_pct
from basis_kernel import executable_return_pct
from market_stream import DEFAULT_BASIS_MAX_SKEW_MS


__all__ = [
    "DEFAULT_MAX_QUOTE_AGE_MS", "DEFAULT_MAX_QUOTE_SKEW_MS", "NAMED_WINDOWS",
    "TimedQuote", "ObservationRecordId", "VenueQuote", "ExecutableGapReading",
    "GapRefusal", "ExecutableGapResult", "GapObservation", "WindowStats",
    "HistoryQueryError", "quotes_from_stream_record", "read_executable_gap",
    "read_executable_gap_shared_observation", "parse_window", "GapHistoryRing",
]


DEFAULT_MAX_QUOTE_AGE_MS = int(ACTIONABLE_EVENT_MAX_AGE_MS)
DEFAULT_MAX_QUOTE_SKEW_MS = DEFAULT_BASIS_MAX_SKEW_MS
HISTORY_RETENTION = timedelta(hours=24); DEFAULT_RING_CAPACITY = 481
NAMED_WINDOWS = ("1h", "5h", "15h", "24h"); EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

RefusalReason = Literal[
    "SYMBOL_INVALID", "NOW_INVALID", "AGE_BOUND_INVALID",
    "SKEW_BOUND_INVALID", "SPOT_QUOTE_INVALID", "PERP_QUOTE_INVALID",
    "SPOT_QUOTE_IN_FUTURE", "PERP_QUOTE_IN_FUTURE",
    "SPOT_QUOTE_STALE", "PERP_QUOTE_STALE", "QUOTE_SKEW_EXCEEDED",
    "SPOT_BOOK_CROSSED_OR_LOCKED", "PERP_BOOK_CROSSED_OR_LOCKED",
    "GAP_UNAVAILABLE",
]


@dataclass(frozen=True)
class TimedQuote:
    """Legacy best bid/ask shape without explicit venue provenance."""

    bid: Any
    ask: Any
    observed_ms: Any


@dataclass(frozen=True, eq=False)
class ObservationRecordId:
    """Opaque typed identity for one atomic upstream observation record."""


@dataclass(frozen=True, init=False)
class VenueQuote(TimedQuote):
    """One venue touch with descriptive source and observation identity."""

    venue: Literal["spot", "perp"]
    source: Any
    record_id: Optional[ObservationRecordId]

    def __init__(
        self,
        venue: Literal["spot", "perp"],
        bid: Any,
        ask: Any,
        observed_ms: Any,
        source: Any = None,
        *,
        record_id: Optional[ObservationRecordId] = None,
    ) -> None:
        object.__setattr__(self, "venue", venue)
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "observed_ms", observed_ms)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "record_id", record_id)


@dataclass(frozen=True)
class ExecutableGapReading:
    """Accepted paired touch, with percentage-point gaps and venue ages."""

    symbol: str
    now_ms: int
    entry_gap_pct: Decimal
    exit_gap_pct: Decimal
    spot_age_ms: int
    perp_age_ms: int
    verdict: Literal["READY"] = "READY"
    skew_checked: bool = True


@dataclass(frozen=True)
class GapRefusal:
    """Typed fail-closed result; no gap values exist on this type."""

    symbol: str
    reason: RefusalReason
    spot_age_ms: Optional[int] = None
    perp_age_ms: Optional[int] = None
    verdict: Literal["REFUSED"] = "REFUSED"
    skew_checked: bool = True


ExecutableGapResult = Union[ExecutableGapReading, GapRefusal]


@dataclass(frozen=True)
class GapObservation:
    symbol: str
    utc: datetime
    utc_text: str
    entry_gap_pct: Decimal
    exit_gap_pct: Decimal
    skew_checked: bool = True


@dataclass(frozen=True)
class WindowStats:
    symbol: str
    window: timedelta
    start: Optional[datetime]
    end: Optional[datetime]
    samples: int
    gap_min: Optional[Decimal]
    gap_max: Optional[Decimal]
    gap_mean: Optional[Decimal]
    gap_latest: Optional[Decimal]
    latest_utc: Optional[str]


class HistoryQueryError(ValueError):
    """The requested symbol, metric, window, or endpoint is invalid."""


def quotes_from_stream_record(
    record: Mapping[str, Any],
    *,
    spot_observed_ms: Any,
    perp_observed_ms: Any,
) -> Tuple[VenueQuote, VenueQuote]:
    """Adapt one stream observation into quotes with one shared record ID."""
    if not isinstance(record, Mapping):
        raise TypeError("stream record must be a mapping")
    record_id = ObservationRecordId()
    return (
        VenueQuote("spot", record.get("spot_bid"), record.get("spot_ask"),
                   spot_observed_ms, "market_stream", record_id=record_id),
        VenueQuote("perp", record.get("perp_bid"), record.get("perp_ask"),
                   perp_observed_ms, "market_stream", record_id=record_id),
    )


def _price(raw: Any) -> Optional[Decimal]:
    if isinstance(raw, Decimal):
        value = raw
    elif type(raw) is str and raw:
        try:
            value = Decimal(raw)
        except InvalidOperation:
            return None
    else:
        return None
    return value if value.is_finite() and value > 0 else None


def _valid_ms(raw: Any) -> bool:
    return not isinstance(raw, bool) and isinstance(raw, int) and raw >= 0


def _valid_quote_for_venue(quote: Any, venue: str) -> bool:
    if isinstance(quote, VenueQuote):
        return quote.venue == venue and _valid_ms(quote.observed_ms)
    return isinstance(quote, TimedQuote) and _valid_ms(quote.observed_ms)


def _pair_skew_checked(spot: Any, perp: Any) -> bool:
    if not isinstance(spot, VenueQuote) \
            or not isinstance(perp, VenueQuote):
        return False
    if not _valid_ms(spot.observed_ms) or not _valid_ms(perp.observed_ms) \
            or not isinstance(spot.record_id, ObservationRecordId) \
            or not isinstance(perp.record_id, ObservationRecordId):
        return False
    return spot.record_id is not perp.record_id


def _refused(
    symbol: Any,
    reason: RefusalReason,
    spot_age_ms: Optional[int] = None,
    perp_age_ms: Optional[int] = None,
    *,
    skew_checked: bool = True,
) -> GapRefusal:
    return GapRefusal(
        symbol if type(symbol) is str else "",
        reason,
        spot_age_ms,
        perp_age_ms,
        skew_checked=skew_checked,
    )


def _read_executable_gap(
    symbol: Any,
    spot: Any,
    perp: Any,
    now_ms: Any,
    *,
    max_age_ms: Any,
    max_skew_ms: Any,
    skew_checked: bool,
) -> ExecutableGapResult:
    def refused(
        reason: RefusalReason,
        spot_age_ms: Optional[int] = None,
        perp_age_ms: Optional[int] = None,
    ) -> GapRefusal:
        return _refused(
            symbol, reason, spot_age_ms, perp_age_ms,
            skew_checked=skew_checked,
        )

    if type(symbol) is not str or SYMBOL_RE.fullmatch(symbol) is None:
        return refused("SYMBOL_INVALID")
    if not _valid_ms(now_ms):
        return refused("NOW_INVALID")
    if not _valid_ms(max_age_ms) or max_age_ms == 0:
        return refused("AGE_BOUND_INVALID")
    if not _valid_ms(max_skew_ms):
        return refused("SKEW_BOUND_INVALID")
    if not _valid_quote_for_venue(spot, "spot"):
        return refused("SPOT_QUOTE_INVALID")
    if not _valid_quote_for_venue(perp, "perp"):
        return refused("PERP_QUOTE_INVALID")

    spot_age = now_ms - spot.observed_ms
    perp_age = now_ms - perp.observed_ms
    if spot_age < 0:
        return refused("SPOT_QUOTE_IN_FUTURE", spot_age, perp_age)
    if perp_age < 0:
        return refused("PERP_QUOTE_IN_FUTURE", spot_age, perp_age)
    if spot_age > max_age_ms:
        return refused("SPOT_QUOTE_STALE", spot_age, perp_age)
    if perp_age > max_age_ms:
        return refused("PERP_QUOTE_STALE", spot_age, perp_age)
    if skew_checked \
            and abs(spot.observed_ms - perp.observed_ms) > max_skew_ms:
        return refused("QUOTE_SKEW_EXCEEDED", spot_age, perp_age)

    spot_bid, spot_ask = _price(spot.bid), _price(spot.ask)
    perp_bid, perp_ask = _price(perp.bid), _price(perp.ask)
    if spot_bid is None or spot_ask is None:
        return refused("SPOT_QUOTE_INVALID", spot_age, perp_age)
    if perp_bid is None or perp_ask is None:
        return refused("PERP_QUOTE_INVALID", spot_age, perp_age)
    if spot_bid >= spot_ask:
        return refused("SPOT_BOOK_CROSSED_OR_LOCKED", spot_age, perp_age)
    if perp_bid >= perp_ask:
        return refused("PERP_BOOK_CROSSED_OR_LOCKED", spot_age, perp_age)

    entry = kernel_entry_gap_pct(spot_ask, perp_bid)
    exit_gap = executable_return_pct(perp_ask, spot_bid)
    if entry is None or exit_gap is None:
        return refused("GAP_UNAVAILABLE", spot_age, perp_age)
    return ExecutableGapReading(
        symbol, now_ms, entry, exit_gap, spot_age, perp_age,
        skew_checked=skew_checked,
    )


def read_executable_gap(
    symbol: Any,
    spot: Any,
    perp: Any,
    now_ms: Any,
    *,
    max_age_ms: Any = DEFAULT_MAX_QUOTE_AGE_MS,
    max_skew_ms: Any = DEFAULT_MAX_QUOTE_SKEW_MS,
) -> ExecutableGapResult:
    """Read a venue pair and enforce skew only when provenance proves it."""
    return _read_executable_gap(
        symbol, spot, perp, now_ms,
        max_age_ms=max_age_ms,
        max_skew_ms=max_skew_ms,
        skew_checked=_pair_skew_checked(spot, perp),
    )


def read_executable_gap_shared_observation(
    symbol: Any,
    record: Mapping[str, Any],
    shared_observed_ms: Any,
    now_ms: Any,
    *,
    max_age_ms: Any = DEFAULT_MAX_QUOTE_AGE_MS,
) -> ExecutableGapResult:
    """Read both books at one shared time without claiming a skew check."""
    spot, perp = quotes_from_stream_record(
        record,
        spot_observed_ms=shared_observed_ms,
        perp_observed_ms=shared_observed_ms,
    )
    return _read_executable_gap(
        symbol, spot, perp, now_ms,
        max_age_ms=max_age_ms,
        max_skew_ms=DEFAULT_MAX_QUOTE_SKEW_MS,
        skew_checked=False,
    )


def parse_window(text: str) -> timedelta:
    """Parse the recorder reader's positive integer ``m``, ``h``, or ``d`` API."""
    if type(text) is not str or len(text) < 2 or not text[:-1].isdigit() \
            or text[0] == "0" or text[-1] not in "mhd":
        raise HistoryQueryError("window must be positive integer m, h, or d text")
    seconds = {"m": 60, "h": 3_600, "d": 86_400}[text[-1]]
    try:
        return timedelta(seconds=int(text[:-1]) * seconds)
    except (OverflowError, ValueError):
        raise HistoryQueryError("window is too large") from None


def _utc_from_ms(value: int) -> datetime:
    try:
        return EPOCH + timedelta(milliseconds=value)
    except OverflowError:
        raise HistoryQueryError("observation time is outside the supported range") \
            from None


def _utc_text(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class GapHistoryRing:
    """Bounded per-symbol accepted readings with recorder-shaped queries."""

    def __init__(self, capacity: int = DEFAULT_RING_CAPACITY) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) \
                or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self.capacity = capacity
        self._rows: Dict[str, Deque[GapObservation]] = {}

    def record(self, reading: ExecutableGapReading) -> GapObservation:
        if not isinstance(reading, ExecutableGapReading):
            raise TypeError("history accepts ExecutableGapReading only")
        if type(reading.skew_checked) is not bool:
            raise ValueError("history skew truth must be boolean")
        if any(not isinstance(value, Decimal) or not value.is_finite()
               for value in (reading.entry_gap_pct, reading.exit_gap_pct)):
            raise ValueError("history gaps must be finite Decimal values")
        utc = _utc_from_ms(reading.now_ms)
        observation = GapObservation(
            reading.symbol,
            utc,
            _utc_text(utc),
            reading.entry_gap_pct,
            reading.exit_gap_pct,
            reading.skew_checked,
        )
        rows = self._rows.setdefault(
            reading.symbol, deque(maxlen=self.capacity))
        if rows and utc < rows[-1].utc:
            raise ValueError("history observations must be time ordered")
        if rows and utc == rows[-1].utc:
            rows.pop()
        rows.append(observation)
        cutoff = utc - HISTORY_RETENTION
        while rows and rows[0].utc < cutoff:
            rows.popleft()
        return observation

    def series_for(self, symbol: str) -> Tuple[GapObservation, ...]:
        validated = self._symbol(symbol)
        return tuple(self._rows.get(validated, ()))

    @staticmethod
    def _symbol(symbol: Any) -> str:
        if type(symbol) is not str or SYMBOL_RE.fullmatch(symbol) is None:
            raise HistoryQueryError("symbol is invalid")
        return symbol

    def window_stats(
        self,
        symbol: str,
        window: Any,
        *,
        end: Optional[datetime] = None,
        metric: str = "entry",
    ) -> WindowStats:
        symbol = self._symbol(symbol)
        duration = parse_window(window) if type(window) is str else window
        if not isinstance(duration, timedelta) or duration <= timedelta(0):
            raise HistoryQueryError("window must be positive")
        if metric not in ("entry", "exit"):
            raise HistoryQueryError("metric must be entry or exit")
        if end is not None and (
                not isinstance(end, datetime) or end.tzinfo is None
                or end.utcoffset() is None):
            raise HistoryQueryError("window end must include a timezone")

        series = self.series_for(symbol)
        endpoint = (series[-1].utc if series else None) if end is None \
            else end.astimezone(timezone.utc)
        if endpoint is None:
            return WindowStats(symbol, duration, None, None, 0,
                               None, None, None, None, None)
        try:
            start = endpoint - duration
        except (OverflowError, ValueError):
            raise HistoryQueryError(
                "window start is outside the supported range") from None
        selected = [row for row in series if start <= row.utc <= endpoint]
        if not selected:
            return WindowStats(symbol, duration, start, endpoint, 0,
                               None, None, None, None, None)
        field = metric + "_gap_pct"
        gaps = [getattr(row, field) for row in selected]
        with localcontext() as context:
            context.prec = 50
            mean = sum(gaps, Decimal(0)) / Decimal(len(gaps))
        latest = selected[-1]
        return WindowStats(
            symbol, duration, start, endpoint, len(gaps), min(gaps), max(gaps),
            mean, gaps[-1], latest.utc_text)

    def named_windows(
        self,
        symbol: str,
        *,
        end: Optional[datetime] = None,
        metric: str = "entry",
    ) -> Mapping[str, WindowStats]:
        """Return the owner-named 1h, 5h, 15h, and 24h windows."""
        return {
            window: self.window_stats(symbol, window, end=end, metric=metric)
            for window in NAMED_WINDOWS
        }
