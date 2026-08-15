"""Pure violent-move to executable-basis event coupling.

The detector consumes the per-symbol records already written by
``market_stream``.  It performs no I/O and has no order, account, or sizing
surface.  Prices and all derived percentages are ``Decimal`` values.

Units:

* gap and price fields are percentage points (``1`` means one percent);
* ``surge_ratio`` is a multiple of the symbol's rolling volume median;
* funding fields are per-settlement fractions (``0.001`` means 0.1%); and
* freshness and observation distance are milliseconds.

``price_accel_pct`` is the signed spot-midpoint move between consecutive
stream observations.  This is the short-horizon acceleration proxy available
from the frozen stream schema; it is derived rather than added to that schema.

A static gap is never sufficient.  Every emitted event is coupled to a fresh
volume/price impulse, and the gap kinds require the executable gap magnitude
to grow during that impulse.  This deliberately rejects quiet-market gaps as
market-maker leftovers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from types import MappingProxyType
from typing import Any, Dict, Iterable, Literal, Mapping, Optional, Tuple


DislocationKind = Literal[
    "gap_spike", "funding_dislocation", "runner_hedge_window"
]

DISLOCATION_KINDS = frozenset(
    ("gap_spike", "funding_dislocation", "runner_hedge_window")
)
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,40}$")
ZERO = Decimal("0")
HUNDRED = Decimal("100")


def _threshold(default: Any, why: str) -> Any:
    """Attach a tuning rationale to one detection threshold."""
    return field(default=default, metadata={"why": why})


@dataclass(frozen=True)
class DetectionThresholds:
    """All numeric policy for classifying one dislocation onset.

    Defaults are explicit initial operating boundaries, not claims of a
    universal market constant.  Callers can replace the whole immutable value
    as measured runner data supports retuning.
    """

    max_freshness_ms: Decimal = _threshold(
        Decimal("5000"),
        "Reject a stream record older than the scanner's five-second gate.",
    )
    max_observation_gap_ms: int = _threshold(
        120_000,
        "Do not call a long data outage a short-horizon price impulse.",
    )
    min_surge_ratio: Decimal = _threshold(
        Decimal("8"),
        "Require an eightfold own-symbol volume burst before coupling basis.",
    )
    min_abs_price_accel_pct: Decimal = _threshold(
        Decimal("0.50"),
        "Require a half-percent executable spot move, above ordinary churn.",
    )
    min_gap_spike_pct: Decimal = _threshold(
        Decimal("1.00"),
        "Reserve gap_spike for a material one-percent executable dislocation.",
    )
    min_gap_spike_growth_pct: Decimal = _threshold(
        Decimal("0.50"),
        "Require the gap magnitude to open by half a percentage point.",
    )
    min_abs_funding_rate: Decimal = _threshold(
        Decimal("0.001"),
        "Treat a ten-basis-point settlement rate as materially displaced.",
    )
    min_abs_funding_rate_change: Decimal = _threshold(
        Decimal("0.0005"),
        "Require funding to move five basis points during the violent move.",
    )
    min_funding_gap_pct: Decimal = _threshold(
        Decimal("0.25"),
        "Require executable basis context before emitting a funding event.",
    )
    min_runner_gap_pct: Decimal = _threshold(
        Decimal("0.50"),
        "Require a half-percent hedgeable gap around a volume runner.",
    )
    min_runner_gap_growth_pct: Decimal = _threshold(
        Decimal("0.10"),
        "Reject an unchanged pre-existing gap while allowing gradual opening.",
    )
    max_abs_funding_rate: Decimal = _threshold(
        Decimal("0.10"),
        "Match the basis kernel's fail-closed funding sanity ceiling.",
    )

    def __post_init__(self) -> None:
        decimal_names = (
            "max_freshness_ms",
            "min_surge_ratio",
            "min_abs_price_accel_pct",
            "min_gap_spike_pct",
            "min_gap_spike_growth_pct",
            "min_abs_funding_rate",
            "min_abs_funding_rate_change",
            "min_funding_gap_pct",
            "min_runner_gap_pct",
            "min_runner_gap_growth_pct",
            "max_abs_funding_rate",
        )
        for name in decimal_names:
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() \
                    or value < ZERO:
                raise ValueError("%s must be a finite non-negative Decimal" % name)
        if isinstance(self.max_observation_gap_ms, bool) \
                or not isinstance(self.max_observation_gap_ms, int) \
                or self.max_observation_gap_ms <= 0:
            raise ValueError("max_observation_gap_ms must be a positive integer")
        if self.min_surge_ratio <= ZERO or self.min_abs_price_accel_pct <= ZERO:
            raise ValueError("violent-move thresholds must be positive")
        if self.min_gap_spike_pct < self.min_runner_gap_pct \
                or self.min_gap_spike_growth_pct < self.min_runner_gap_growth_pct:
            raise ValueError("gap-spike thresholds must not be below runner thresholds")
        if self.max_abs_funding_rate < self.min_abs_funding_rate:
            raise ValueError("funding sanity ceiling is below detection threshold")
        if any(not item.metadata.get("why") for item in fields(self)):
            raise ValueError("every detection threshold needs a rationale")


DEFAULT_THRESHOLDS = DetectionThresholds()

# A ladder-facing event must still describe the same fresh observation that
# the detector accepted.  Reuse the detector's five-second stream bound for
# wall-clock event age so delayed or replayed event objects are not actionable.
ACTIONABLE_EVENT_MAX_AGE_MS = DEFAULT_THRESHOLDS.max_freshness_ms


@dataclass(frozen=True)
class DislocationEvent:
    """Typed, execution-free fact for the upstream basis ladder."""

    utc: str
    symbol: str
    kind: DislocationKind
    entry_gap_pct: Decimal
    exit_gap_pct: Decimal
    surge_ratio: Decimal
    price_accel_pct: Decimal
    gap_growth_pct: Decimal
    funding_rate: Decimal
    funding_rate_delta: Decimal
    freshness_ms: Decimal
    observation_gap_ms: int
    spot_bid: Decimal
    spot_ask: Decimal
    perp_bid: Decimal
    perp_ask: Decimal

    @property
    def evidence(self) -> Mapping[str, Any]:
        """Immutable evidence view, separate from ladder-facing core fields."""
        return MappingProxyType({
            "surge_ratio": self.surge_ratio,
            "price_accel_pct": self.price_accel_pct,
            "gap_growth_pct": self.gap_growth_pct,
            "funding_rate": self.funding_rate,
            "funding_rate_delta": self.funding_rate_delta,
            "freshness_ms": self.freshness_ms,
            "observation_gap_ms": self.observation_gap_ms,
            "spot_bid": self.spot_bid,
            "spot_ask": self.spot_ask,
            "perp_bid": self.perp_bid,
            "perp_ask": self.perp_ask,
        })

    def as_dict(self) -> Dict[str, Any]:
        """Return the explicit event envelope expected by a ledger adapter."""
        return {
            "utc": self.utc,
            "symbol": self.symbol,
            "kind": self.kind,
            "entry_gap_pct": self.entry_gap_pct,
            "exit_gap_pct": self.exit_gap_pct,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class _Sample:
    utc: str
    utc_value: datetime
    spot_bid: Decimal
    spot_ask: Decimal
    perp_bid: Decimal
    perp_ask: Decimal
    spot_mid: Decimal
    entry_gap_pct: Decimal
    exit_gap_pct: Decimal
    surge_ratio: Decimal
    funding_rate: Decimal
    freshness_ms: Decimal


def _decimal(raw: Any) -> Optional[Decimal]:
    if isinstance(raw, bool) or raw is None or isinstance(raw, float):
        return None
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def _utc(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        value = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        return None
    return value


def _return_pct(later: Decimal, earlier: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return (later - earlier) / earlier * HUNDRED


def _executable_gaps(
    spot_bid: Decimal,
    spot_ask: Decimal,
    perp_bid: Decimal,
    perp_ask: Decimal,
) -> Tuple[Decimal, Decimal]:
    """Derive the two executable gaps used by emission and validation."""
    return (
        _return_pct(perp_bid, spot_ask),
        # Same executable unwind-cost convention used by StreamStateReads.
        _return_pct(perp_ask, spot_bid),
    )


def _gap_growth_pct(
    current_entry_gap_pct: Decimal,
    previous_entry_gap_pct: Decimal,
) -> Decimal:
    """Derive detector growth from consecutive absolute entry gaps."""
    return abs(current_entry_gap_pct) - abs(previous_entry_gap_pct)


def _observation_is_continuous(
    observation_gap_ms: Any, thresholds: DetectionThresholds
) -> bool:
    return (
        not isinstance(observation_gap_ms, bool)
        and isinstance(observation_gap_ms, int)
        and 0 < observation_gap_ms <= thresholds.max_observation_gap_ms
    )


def _move_is_qualifying(
    surge_ratio: Decimal,
    price_accel_pct: Decimal,
    thresholds: DetectionThresholds,
) -> bool:
    return (
        surge_ratio >= thresholds.min_surge_ratio
        and abs(price_accel_pct) >= thresholds.min_abs_price_accel_pct
    )


def _magnitude_checks(
    entry_gap_pct: Decimal,
    gap_growth_pct: Decimal,
    funding_rate: Decimal,
    funding_rate_delta: Decimal,
    thresholds: DetectionThresholds,
) -> Dict[str, bool]:
    """Compute every classification comparison once for both public paths."""
    gap_magnitude = abs(entry_gap_pct)
    return {
        "funding_rate": abs(funding_rate)
        >= thresholds.min_abs_funding_rate,
        "funding_change": abs(funding_rate_delta)
        >= thresholds.min_abs_funding_rate_change,
        "funding_gap": gap_magnitude >= thresholds.min_funding_gap_pct,
        "spike_gap": gap_magnitude >= thresholds.min_gap_spike_pct,
        "spike_growth": gap_growth_pct
        >= thresholds.min_gap_spike_growth_pct,
        "runner_gap": gap_magnitude >= thresholds.min_runner_gap_pct,
        "runner_growth": gap_growth_pct
        >= thresholds.min_runner_gap_growth_pct,
    }


def _emitted_kind(
    entry_gap_pct: Decimal,
    gap_growth_pct: Decimal,
    funding_rate: Decimal,
    funding_rate_delta: Decimal,
    thresholds: DetectionThresholds,
) -> Optional[DislocationKind]:
    """Apply the detector's one classification policy in priority order."""
    checks = _magnitude_checks(
        entry_gap_pct,
        gap_growth_pct,
        funding_rate,
        funding_rate_delta,
        thresholds,
    )
    if checks["funding_rate"] and checks["funding_change"] \
            and checks["funding_gap"]:
        return "funding_dislocation"
    if checks["spike_gap"] and checks["spike_growth"]:
        return "gap_spike"
    if checks["runner_gap"] and checks["runner_growth"]:
        return "runner_hedge_window"
    return None


def _quotes_are_consistent(quotes: Tuple[Decimal, ...]) -> bool:
    spot_bid, spot_ask, perp_bid, perp_ask = quotes
    return (
        min(quotes) > ZERO
        and spot_bid <= spot_ask
        and perp_bid <= perp_ask
    )


def _funding_is_sane(
    funding_rate: Decimal, thresholds: DetectionThresholds
) -> bool:
    return abs(funding_rate) <= thresholds.max_abs_funding_rate


def _sample(
    record: Any, thresholds: DetectionThresholds
) -> Optional[_Sample]:
    if not isinstance(record, Mapping) or record.get("venues") != "both":
        return None
    raw_utc = record.get("utc")
    utc_value = _utc(raw_utc)
    if utc_value is None:
        return None
    values = {
        name: _decimal(record.get(name))
        for name in (
            "spot_bid",
            "spot_ask",
            "perp_bid",
            "perp_ask",
            "surge_ratio",
            "funding_rate",
            "freshness_ms",
        )
    }
    if any(value is None for value in values.values()):
        return None
    spot_bid = values["spot_bid"]
    spot_ask = values["spot_ask"]
    perp_bid = values["perp_bid"]
    perp_ask = values["perp_ask"]
    surge_ratio = values["surge_ratio"]
    funding_rate = values["funding_rate"]
    freshness_ms = values["freshness_ms"]
    assert spot_bid is not None and spot_ask is not None
    assert perp_bid is not None and perp_ask is not None
    assert surge_ratio is not None and funding_rate is not None
    assert freshness_ms is not None
    if not _quotes_are_consistent(
            (spot_bid, spot_ask, perp_bid, perp_ask)):
        return None
    if surge_ratio < ZERO or freshness_ms < ZERO \
            or freshness_ms > thresholds.max_freshness_ms:
        return None
    if not _funding_is_sane(funding_rate, thresholds):
        return None
    with localcontext() as context:
        context.prec = 50
        spot_mid = (spot_bid + spot_ask) / Decimal("2")
    entry_gap_pct, exit_gap_pct = _executable_gaps(
        spot_bid, spot_ask, perp_bid, perp_ask)
    return _Sample(
        utc=raw_utc,
        utc_value=utc_value,
        spot_bid=spot_bid,
        spot_ask=spot_ask,
        perp_bid=perp_bid,
        perp_ask=perp_ask,
        spot_mid=spot_mid,
        entry_gap_pct=entry_gap_pct,
        exit_gap_pct=exit_gap_pct,
        surge_ratio=surge_ratio,
        funding_rate=funding_rate,
        freshness_ms=freshness_ms,
    )


def _delta_ms(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def _event_decimal(event: DislocationEvent, name: str) -> Optional[Decimal]:
    value = getattr(event, name, None)
    if not isinstance(value, Decimal) or not value.is_finite():
        return None
    return value


def validate_actionable(event: Any, now_ms: Any) -> Optional[str]:
    """Return ``None`` only for a fresh event matching detector policy.

    The UTC age bound is ``ACTIONABLE_EVENT_MAX_AGE_MS`` and is intentionally
    the same as ``DetectionThresholds.max_freshness_ms``.  A non-empty reason
    is a stable refusal row for callers; this function never grants authority
    from the dataclass type alone.
    """
    if not isinstance(event, DislocationEvent):
        return "DISLOCATION_EVENT_TYPE_INVALID"
    if not isinstance(event.kind, str) or event.kind not in DISLOCATION_KINDS:
        return "DISLOCATION_EVENT_KIND_NOT_ACTIONABLE"
    if not isinstance(event.symbol, str) \
            or SYMBOL_RE.fullmatch(event.symbol) is None:
        return "DISLOCATION_EVENT_SYMBOL_INVALID"
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        return "DISLOCATION_EVENT_NOW_INVALID"

    utc_value = _utc(event.utc)
    if utc_value is None:
        return "DISLOCATION_EVENT_UTC_INVALID"
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    event_ms = _delta_ms(utc_value, epoch)
    age_ms = now_ms - event_ms
    if age_ms < 0:
        return "DISLOCATION_EVENT_UTC_IN_FUTURE"
    if Decimal(age_ms) > ACTIONABLE_EVENT_MAX_AGE_MS:
        return "DISLOCATION_EVENT_UTC_STALE"

    freshness = _event_decimal(event, "freshness_ms")
    if freshness is None or freshness < ZERO:
        return "DISLOCATION_EVENT_FRESHNESS_INVALID"
    if freshness > DEFAULT_THRESHOLDS.max_freshness_ms:
        return "DISLOCATION_EVENT_FRESHNESS_EXCEEDED"

    observation_gap_ms = event.observation_gap_ms
    if isinstance(observation_gap_ms, bool) \
            or not isinstance(observation_gap_ms, int) \
            or observation_gap_ms <= 0:
        return "DISLOCATION_EVENT_CONTINUITY_INVALID"
    if not _observation_is_continuous(
            observation_gap_ms, DEFAULT_THRESHOLDS):
        return "DISLOCATION_EVENT_CONTINUITY_EXCEEDED"

    surge = _event_decimal(event, "surge_ratio")
    acceleration = _event_decimal(event, "price_accel_pct")
    if surge is None:
        return "DISLOCATION_EVENT_SURGE_BELOW_THRESHOLD"
    if acceleration is None:
        return "DISLOCATION_EVENT_ACCELERATION_BELOW_THRESHOLD"
    if not _move_is_qualifying(surge, acceleration, DEFAULT_THRESHOLDS) \
            and surge < DEFAULT_THRESHOLDS.min_surge_ratio:
        return "DISLOCATION_EVENT_SURGE_BELOW_THRESHOLD"
    if not _move_is_qualifying(surge, acceleration, DEFAULT_THRESHOLDS):
        return "DISLOCATION_EVENT_ACCELERATION_BELOW_THRESHOLD"

    entry_gap = _event_decimal(event, "entry_gap_pct")
    exit_gap = _event_decimal(event, "exit_gap_pct")
    growth = _event_decimal(event, "gap_growth_pct")
    funding = _event_decimal(event, "funding_rate")
    funding_delta = _event_decimal(event, "funding_rate_delta")
    if entry_gap is None or exit_gap is None or growth is None:
        return "DISLOCATION_EVENT_MAGNITUDE_INVALID"
    if funding is None or funding_delta is None \
            or not _funding_is_sane(funding, DEFAULT_THRESHOLDS):
        return "DISLOCATION_EVENT_FUNDING_INVALID"

    checks = _magnitude_checks(
        entry_gap, growth, funding, funding_delta, DEFAULT_THRESHOLDS)
    if event.kind == "gap_spike":
        if not checks["spike_gap"]:
            return "DISLOCATION_EVENT_GAP_BELOW_THRESHOLD"
        if not checks["spike_growth"]:
            return "DISLOCATION_EVENT_GROWTH_BELOW_THRESHOLD"
    elif event.kind == "runner_hedge_window":
        if not checks["runner_gap"]:
            return "DISLOCATION_EVENT_GAP_BELOW_THRESHOLD"
        if not checks["runner_growth"]:
            return "DISLOCATION_EVENT_GROWTH_BELOW_THRESHOLD"
    else:
        if not checks["funding_rate"] or not checks["funding_change"]:
            return "DISLOCATION_EVENT_FUNDING_BELOW_THRESHOLD"
        if not checks["funding_gap"]:
            return "DISLOCATION_EVENT_GAP_BELOW_THRESHOLD"

    detected_kind = _emitted_kind(
        entry_gap, growth, funding, funding_delta, DEFAULT_THRESHOLDS)
    if detected_kind != event.kind:
        return "DISLOCATION_EVENT_KIND_MISMATCH"
    if growth > _gap_growth_pct(entry_gap, ZERO):
        return "DISLOCATION_EVENT_GROWTH_INCONSISTENT"

    quotes = tuple(_event_decimal(event, name) for name in (
        "spot_bid", "spot_ask", "perp_bid", "perp_ask"))
    if any(value is None for value in quotes):
        return "DISLOCATION_EVENT_QUOTES_INVALID"
    spot_bid, spot_ask, perp_bid, perp_ask = quotes
    assert spot_bid is not None and spot_ask is not None
    assert perp_bid is not None and perp_ask is not None
    if not _quotes_are_consistent(quotes):
        return "DISLOCATION_EVENT_QUOTES_INVALID"
    derived_entry, derived_exit = _executable_gaps(
        spot_bid, spot_ask, perp_bid, perp_ask)
    if entry_gap != derived_entry or exit_gap != derived_exit:
        return "DISLOCATION_EVENT_GAP_NOT_DERIVABLE"
    return None


class DislocationDetector:
    """Stateful two-observation adapter over otherwise pure detection logic.

    One event is emitted at the onset of each continuous violent opportunity.
    The episode rearms only after the volume/price impulse falls below its
    thresholds.  Duplicate or out-of-order observations are ignored.
    """

    def __init__(
        self, thresholds: DetectionThresholds = DEFAULT_THRESHOLDS
    ) -> None:
        if not isinstance(thresholds, DetectionThresholds):
            raise TypeError("thresholds must be DetectionThresholds")
        self.thresholds = thresholds
        self._previous: Dict[str, _Sample] = {}
        self._episode_active: Dict[str, bool] = {}

    def _break_continuity(self, symbol: str) -> None:
        self._previous.pop(symbol, None)
        self._episode_active.pop(symbol, None)

    def observe(
        self, symbol: str, record: Mapping[str, Any]
    ) -> Optional[DislocationEvent]:
        """Consume one unchanged stream record and maybe emit one event."""
        if not isinstance(symbol, str) or SYMBOL_RE.fullmatch(symbol) is None:
            return None
        current = _sample(record, self.thresholds)
        if current is None:
            self._break_continuity(symbol)
            return None
        previous = self._previous.get(symbol)
        if previous is None:
            self._previous[symbol] = current
            self._episode_active[symbol] = False
            return None
        observation_gap_ms = _delta_ms(current.utc_value, previous.utc_value)
        if observation_gap_ms <= 0:
            return None
        self._previous[symbol] = current
        if not _observation_is_continuous(
                observation_gap_ms, self.thresholds):
            self._episode_active[symbol] = False
            return None

        price_accel_pct = _return_pct(current.spot_mid, previous.spot_mid)
        gap_growth_pct = _gap_growth_pct(
            current.entry_gap_pct, previous.entry_gap_pct
        )
        funding_rate_delta = current.funding_rate - previous.funding_rate
        qualifying_move = _move_is_qualifying(
            current.surge_ratio, price_accel_pct, self.thresholds)
        if not qualifying_move:
            self._episode_active[symbol] = False
            return None
        if self._episode_active.get(symbol, False):
            return None

        kind = _emitted_kind(
            current.entry_gap_pct,
            gap_growth_pct,
            current.funding_rate,
            funding_rate_delta,
            self.thresholds,
        )
        if kind is None:
            return None

        self._episode_active[symbol] = True
        return DislocationEvent(
            utc=current.utc,
            symbol=symbol,
            kind=kind,
            entry_gap_pct=current.entry_gap_pct,
            exit_gap_pct=current.exit_gap_pct,
            surge_ratio=current.surge_ratio,
            price_accel_pct=price_accel_pct,
            gap_growth_pct=gap_growth_pct,
            funding_rate=current.funding_rate,
            funding_rate_delta=funding_rate_delta,
            freshness_ms=current.freshness_ms,
            observation_gap_ms=observation_gap_ms,
            spot_bid=current.spot_bid,
            spot_ask=current.spot_ask,
            perp_bid=current.perp_bid,
            perp_ask=current.perp_ask,
        )

    def observe_snapshot(
        self, stream_state: Mapping[str, Mapping[str, Any]]
    ) -> Tuple[DislocationEvent, ...]:
        """Consume one runtime snapshot in stable symbol order."""
        if not isinstance(stream_state, Mapping):
            return ()
        emitted = []
        for symbol in sorted(
            key for key in stream_state if isinstance(key, str)
        ):
            event = self.observe(symbol, stream_state[symbol])
            if event is not None:
                emitted.append(event)
        return tuple(emitted)


def detect_dislocations(
    symbol: str,
    records: Iterable[Mapping[str, Any]],
    thresholds: DetectionThresholds = DEFAULT_THRESHOLDS,
) -> Tuple[DislocationEvent, ...]:
    """Deterministically detect all episode onsets in one symbol series."""
    detector = DislocationDetector(thresholds)
    events = []
    for record in records:
        event = detector.observe(symbol, record)
        if event is not None:
            events.append(event)
    return tuple(events)
