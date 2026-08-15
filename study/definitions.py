#!/usr/bin/env python3
"""Reproducible label definitions for Volume Dataset V2.

All percentages and percentiles are Decimal percentage points: ``1`` means
one percent and percentile ranks span ``0`` through ``100``.  Volume
accelerations are Decimal multiples of the same symbol's trailing 30-day
baseline.  Counts and durations are exact integers.  Predicates accept only
those representations; missing, non-finite, or wrongly typed evidence returns
``False`` rather than being coerced.

Each frozen dataclass owns every calculation and decision threshold for one
label.  Threshold fields carry a ``why`` metadata entry so later tuning cannot
detach a number from its rationale.  Consumers should import the immutable
``DEFINITIONS`` registry and may call a definition's ``matches`` method, or
use the corresponding ``is_*`` function below.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping, Optional, Union, cast


Row = Mapping[str, object]


def _threshold(default: Union[Decimal, int], why: str) -> Any:
    """Build one documented immutable threshold field."""
    if not why.strip():
        raise ValueError("threshold rationale is empty")
    return field(default=default, metadata={"why": why})


def _validate_thresholds(definition: object) -> None:
    """Reject undocumented, non-Decimal/int, negative thresholds."""
    for item in fields(definition):
        value = getattr(definition, item.name)
        if not item.metadata.get("why"):
            raise ValueError(f"{item.name} has no threshold rationale")
        if type(value) is int:
            if value < 0:
                raise ValueError(f"{item.name} must be non-negative")
            continue
        if isinstance(value, Decimal):
            if not value.is_finite() or value < 0:
                raise ValueError(
                    f"{item.name} must be a finite non-negative Decimal"
                )
            continue
        raise TypeError(f"{item.name} must be Decimal or int")


def _decimal(
    row: Row,
    key: str,
    *,
    minimum: Optional[Decimal] = None,
    maximum: Optional[Decimal] = None,
) -> Optional[Decimal]:
    value = row.get(key)
    if not isinstance(value, Decimal) or not value.is_finite():
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _integer(
    row: Row,
    key: str,
    *,
    minimum: int = 0,
    maximum: Optional[int] = None,
) -> Optional[int]:
    value = row.get(key)
    if type(value) is not int or value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _flag(row: Row, key: str) -> Optional[bool]:
    value = row.get(key)
    return value if type(value) is bool else None


@dataclass(frozen=True)
class DeadRegime:
    """Thresholds for a quiet, stale pre-move market.

    ``flat_candle_pct_{1h,6h,24h}``, ``volume_percentile_30d``,
    ``activity_percentile_30d``, and ``minutes_since_meaningful_move`` are the
    required predicate row fields.  Stage A uses the remaining fields when it
    derives those measurements.
    """

    flat_candle_max_range_pct: Decimal = _threshold(
        Decimal("0.20"),
        "WHY: a 0.20% high-low range treats genuinely stale one-minute bars "
        "as nearly flat without requiring identical prints.",
    )
    flat_1h_minutes: int = _threshold(
        60,
        "WHY: one hour detects immediate quiet without letting one candle "
        "decide the regime.",
    )
    flat_6h_minutes: int = _threshold(
        360,
        "WHY: six hours proves that the quiet state spans more than a short "
        "intraday lull.",
    )
    flat_24h_minutes: int = _threshold(
        1_440,
        "WHY: one day catches structurally stale tokens across a full UTC "
        "activity cycle.",
    )
    min_flat_candle_pct_1h: Decimal = _threshold(
        Decimal("80"),
        "WHY: four of every five recent bars must be nearly flat before the "
        "market is called immediately dead.",
    )
    min_flat_candle_pct_6h: Decimal = _threshold(
        Decimal("70"),
        "WHY: the medium window allows brief noise but still requires a "
        "large quiet majority.",
    )
    min_flat_candle_pct_24h: Decimal = _threshold(
        Decimal("60"),
        "WHY: the daily window tolerates session changes while requiring "
        "quiet bars to remain the majority.",
    )
    meaningful_move_pct: Decimal = _threshold(
        Decimal("1"),
        "WHY: a one-minute 1% absolute move is large enough to reset staleness "
        "and is well above ordinary micro-noise.",
    )
    min_minutes_since_meaningful_move: int = _threshold(
        60,
        "WHY: at least one complete hour without a meaningful move prevents "
        "a recently active token from being called dead.",
    )
    percentile_history_days: int = _threshold(
        30,
        "WHY: the frozen interface requires a token-own trailing 30-day "
        "history, covering several weekly cycles.",
    )
    max_volume_percentile_30d: Decimal = _threshold(
        Decimal("20"),
        "WHY: the bottom volume quintile is quiet enough to distinguish "
        "staleness from normal trading.",
    )
    max_activity_percentile_30d: Decimal = _threshold(
        Decimal("20"),
        "WHY: the bottom trade-count quintile independently confirms that "
        "low volume is not merely a size-distribution effect.",
    )

    def __post_init__(self) -> None:
        _validate_thresholds(self)
        percentages = (
            self.flat_candle_max_range_pct,
            self.min_flat_candle_pct_1h,
            self.min_flat_candle_pct_6h,
            self.min_flat_candle_pct_24h,
            self.meaningful_move_pct,
            self.max_volume_percentile_30d,
            self.max_activity_percentile_30d,
        )
        if any(value > Decimal("100") for value in percentages):
            raise ValueError("dead-regime percentage exceeds 100")
        if not (
            self.min_flat_candle_pct_1h
            >= self.min_flat_candle_pct_6h
            >= self.min_flat_candle_pct_24h
        ):
            raise ValueError("flat-candle thresholds must relax with time")
        if not (
            self.flat_1h_minutes
            < self.flat_6h_minutes
            < self.flat_24h_minutes
        ):
            raise ValueError("flat-candle windows must increase")

    def matches(self, row: Row) -> bool:
        return is_dead_regime(row, self)

    def predicate(self, row: Row) -> bool:
        return self.matches(row)

    def __call__(self, row: Row) -> bool:
        return self.matches(row)


@dataclass(frozen=True)
class Ignition:
    """Thresholds for a sudden multi-window activity and price expansion.

    The predicate requires Decimal row fields ``volume_percentile_30d``,
    ``activity_percentile_30d``, ``volume_acceleration_{1m,5m,10m,15m,60m}``,
    and ``price_acceleration_5m_pct``.
    """

    percentile_history_days: int = _threshold(
        30,
        "WHY: a full 30-day own-token baseline prevents cross-token size from "
        "setting the ignition bar.",
    )
    min_volume_percentile_30d: Decimal = _threshold(
        Decimal("95"),
        "WHY: the top five percent of the token's own volume history makes "
        "ignition exceptional rather than routine.",
    )
    min_activity_percentile_30d: Decimal = _threshold(
        Decimal("90"),
        "WHY: top-decile trade activity confirms participation instead of a "
        "single large print.",
    )
    min_volume_acceleration_1m: Decimal = _threshold(
        Decimal("5"),
        "WHY: a fivefold one-minute burst marks an abrupt initial print.",
    )
    min_volume_acceleration_5m: Decimal = _threshold(
        Decimal("3"),
        "WHY: threefold five-minute volume requires the burst to persist past "
        "one isolated candle.",
    )
    min_volume_acceleration_10m: Decimal = _threshold(
        Decimal("2"),
        "WHY: doubling over ten minutes supplies early multi-window "
        "confirmation without demanding a mature move.",
    )
    min_volume_acceleration_15m: Decimal = _threshold(
        Decimal("1.75"),
        "WHY: a 1.75x quarter-hour rate distinguishes broadening activity from "
        "a rapidly fading print.",
    )
    min_volume_acceleration_60m: Decimal = _threshold(
        Decimal("1.25"),
        "WHY: a modest hourly lift permits early detection while proving the "
        "burst is visible beyond short windows.",
    )
    min_acceleration_windows: int = _threshold(
        3,
        "WHY: three of five horizons give multi-timeframe confirmation while "
        "the mandatory 1m and 5m checks retain suddenness.",
    )
    min_price_acceleration_5m_pct: Decimal = _threshold(
        Decimal("0.50"),
        "WHY: a half-percent five-minute move is above the measured 0.30% "
        "round-trip cost and rejects volume-only churn.",
    )

    def __post_init__(self) -> None:
        _validate_thresholds(self)
        if self.min_acceleration_windows < 2 or self.min_acceleration_windows > 5:
            raise ValueError("ignition acceleration count must be 2 through 5")
        if not (
            self.min_volume_acceleration_1m
            >= self.min_volume_acceleration_5m
            >= self.min_volume_acceleration_10m
            >= self.min_volume_acceleration_15m
            >= self.min_volume_acceleration_60m
        ):
            raise ValueError("ignition acceleration bars must relax with time")
        if (
            self.min_volume_percentile_30d > Decimal("100")
            or self.min_activity_percentile_30d > Decimal("100")
            or self.min_price_acceleration_5m_pct > Decimal("100")
        ):
            raise ValueError("ignition percentage exceeds 100")

    def matches(self, row: Row) -> bool:
        return is_ignition(row, self)

    def predicate(self, row: Row) -> bool:
        return self.matches(row)

    def __call__(self, row: Row) -> bool:
        return self.matches(row)


@dataclass(frozen=True)
class GenuineRunner:
    """Thresholds for material, persistent post-ignition follow-through.

    Required row fields are ``ignition``; Decimal
    ``forward_return_{15m,60m}_pct``, ``max_favorable_excursion_60m_pct``, and
    signed ``max_adverse_excursion_60m_pct``; plus integer
    ``positive_candles_next_15m``.
    """

    evaluation_minutes: int = _threshold(
        60,
        "WHY: one hour captures meaningful follow-through without using the "
        "later two-hour Stage B context as label evidence.",
    )
    min_forward_return_15m_pct: Decimal = _threshold(
        Decimal("2"),
        "WHY: a 2% quarter-hour gain is materially above measured costs and "
        "requires prompt continuation.",
    )
    min_forward_return_60m_pct: Decimal = _threshold(
        Decimal("3"),
        "WHY: retaining at least 3% after an hour rejects transient wicks.",
    )
    min_favorable_excursion_60m_pct: Decimal = _threshold(
        Decimal("5"),
        "WHY: a 5% peak establishes an economically meaningful runner rather "
        "than marginal drift.",
    )
    max_adverse_excursion_60m_pct: Decimal = _threshold(
        Decimal("2"),
        "WHY: limiting drawdown magnitude to 2% rejects paths whose apparent "
        "upside required severe adverse travel.",
    )
    min_positive_candles_next_15m: int = _threshold(
        9,
        "WHY: nine positive candles form a clear majority of the first 15 "
        "minutes and measure persistence, not only endpoints.",
    )

    def __post_init__(self) -> None:
        _validate_thresholds(self)
        if self.evaluation_minutes < 15:
            raise ValueError("runner evaluation must include 15 minutes")
        if self.min_positive_candles_next_15m > 15:
            raise ValueError("runner positive-candle count exceeds its window")
        percentages = (
            self.min_forward_return_15m_pct,
            self.min_forward_return_60m_pct,
            self.min_favorable_excursion_60m_pct,
            self.max_adverse_excursion_60m_pct,
        )
        if any(value > Decimal("100") for value in percentages):
            raise ValueError("runner percentage exceeds 100")

    def matches(self, row: Row) -> bool:
        return is_genuine_runner(row, self)

    def predicate(self, row: Row) -> bool:
        return self.matches(row)

    def __call__(self, row: Row) -> bool:
        return self.matches(row)


@dataclass(frozen=True)
class FalseIgnition:
    """Thresholds for an ignition that neither travels nor persists.

    Required row fields are ``ignition``; Decimal ``forward_return_15m_pct``
    and ``max_favorable_excursion_15m_pct``; and integer
    ``active_minutes_next_5m``.
    """

    evaluation_minutes: int = _threshold(
        15,
        "WHY: 15 minutes gives a true ignition time to develop while keeping "
        "the label about immediate failure.",
    )
    max_favorable_excursion_15m_pct: Decimal = _threshold(
        Decimal("1"),
        "WHY: no more than 1% upside means the print never became a material "
        "move after costs.",
    )
    max_abs_forward_return_15m_pct: Decimal = _threshold(
        Decimal("0.50"),
        "WHY: finishing within half a percent of ignition identifies a faded "
        "or directionless event.",
    )
    max_active_minutes_next_5m: int = _threshold(
        2,
        "WHY: activity in at most two of five following minutes distinguishes "
        "one-print noise from sustained participation.",
    )

    def __post_init__(self) -> None:
        _validate_thresholds(self)
        if self.evaluation_minutes < 5:
            raise ValueError("false-ignition evaluation must include 5 minutes")
        if self.max_active_minutes_next_5m > 5:
            raise ValueError("false-ignition activity count exceeds its window")
        if (
            self.max_favorable_excursion_15m_pct > Decimal("100")
            or self.max_abs_forward_return_15m_pct > Decimal("100")
        ):
            raise ValueError("false-ignition percentage exceeds 100")

    def matches(self, row: Row) -> bool:
        return is_false_ignition(row, self)

    def predicate(self, row: Row) -> bool:
        return self.matches(row)

    def __call__(self, row: Row) -> bool:
        return self.matches(row)


@dataclass(frozen=True)
class SlowRamp:
    """Thresholds for broad upward drift without an ignition burst.

    Required row fields are ``ignition``; Decimal ``price_change_60m_pct``,
    ``max_1m_price_change_60m_pct``, ``volume_acceleration_1m``, and
    ``volume_acceleration_60m``; plus integer ``positive_candles_60m``.
    """

    lookback_minutes: int = _threshold(
        60,
        "WHY: an hour is long enough to distinguish gradual accumulation from "
        "the five-minute ignition horizon.",
    )
    min_price_change_60m_pct: Decimal = _threshold(
        Decimal("3"),
        "WHY: a 3% hourly rise is material after costs even though no single "
        "bar ignited.",
    )
    max_1m_price_change_60m_pct: Decimal = _threshold(
        Decimal("0.75"),
        "WHY: capping each one-minute gain at 0.75% preserves the gradual-ramp "
        "shape.",
    )
    min_positive_candles_60m: int = _threshold(
        36,
        "WHY: 36 positive bars require upward breadth across three fifths of "
        "the hour.",
    )
    min_volume_acceleration_60m: Decimal = _threshold(
        Decimal("1.25"),
        "WHY: a 25% hourly activity lift confirms accumulation rather than "
        "price drift on vanishing liquidity.",
    )
    max_volume_acceleration_1m: Decimal = _threshold(
        Decimal("4"),
        "WHY: keeping the final one-minute burst at or below 4x separates a "
        "ramp from the default 5x ignition threshold.",
    )

    def __post_init__(self) -> None:
        _validate_thresholds(self)
        if self.lookback_minutes != 60:
            raise ValueError("slow-ramp candle thresholds require a 60m window")
        if self.min_positive_candles_60m > self.lookback_minutes:
            raise ValueError("slow-ramp positive-candle count exceeds its window")
        if (
            self.min_price_change_60m_pct > Decimal("100")
            or self.max_1m_price_change_60m_pct > Decimal("100")
        ):
            raise ValueError("slow-ramp percentage exceeds 100")

    def matches(self, row: Row) -> bool:
        return is_slow_ramp(row, self)

    def predicate(self, row: Row) -> bool:
        return self.matches(row)

    def __call__(self, row: Row) -> bool:
        return self.matches(row)


@dataclass(frozen=True)
class AlreadyPumped:
    """Thresholds for a fresh ignition arriving after an established pump.

    Required row fields are ``ignition``; Decimal
    ``price_change_prior_60m_pct``, ``volume_percentile_30d``, and
    ``volume_acceleration_60m``; plus integer
    ``minutes_since_meaningful_move``.
    """

    lookback_minutes: int = _threshold(
        60,
        "WHY: the prior hour is long enough to expose a mature move but close "
        "enough to the event to affect entry quality.",
    )
    min_price_change_prior_60m_pct: Decimal = _threshold(
        Decimal("3"),
        "WHY: a token already up 3% has made a cost-significant move before "
        "the new event.",
    )
    max_minutes_since_meaningful_move: int = _threshold(
        15,
        "WHY: a meaningful move within 15 minutes proves the market was "
        "recently active rather than dead.",
    )
    min_volume_percentile_30d: Decimal = _threshold(
        Decimal("80"),
        "WHY: top-quintile prior volume confirms that the earlier pump had "
        "broad activity.",
    )
    min_volume_acceleration_60m: Decimal = _threshold(
        Decimal("1.50"),
        "WHY: 1.5x hourly volume requires the prior move to remain elevated "
        "when the new ignition arrives.",
    )

    def __post_init__(self) -> None:
        _validate_thresholds(self)
        if self.lookback_minutes <= self.max_minutes_since_meaningful_move:
            raise ValueError("already-pumped lookback must exceed recency")
        if (
            self.min_price_change_prior_60m_pct > Decimal("100")
            or self.min_volume_percentile_30d > Decimal("100")
        ):
            raise ValueError("already-pumped percentage exceeds 100")

    def matches(self, row: Row) -> bool:
        return is_already_pumped(row, self)

    def predicate(self, row: Row) -> bool:
        return self.matches(row)

    def __call__(self, row: Row) -> bool:
        return self.matches(row)


Definition = Union[
    DeadRegime,
    Ignition,
    GenuineRunner,
    FalseIgnition,
    SlowRamp,
    AlreadyPumped,
]


DEFINITIONS: Mapping[str, Definition] = MappingProxyType(
    {
        "DeadRegime": DeadRegime(),
        "Ignition": Ignition(),
        "GenuineRunner": GenuineRunner(),
        "FalseIgnition": FalseIgnition(),
        "SlowRamp": SlowRamp(),
        "AlreadyPumped": AlreadyPumped(),
    }
)


def is_dead_regime(
    row: Row, definition: Optional[DeadRegime] = None
) -> bool:
    """Return whether complete pre-state evidence proves a dead regime."""
    selected = definition or cast(DeadRegime, DEFINITIONS["DeadRegime"])
    flat_1h = _decimal(
        row, "flat_candle_pct_1h", minimum=Decimal("0"), maximum=Decimal("100")
    )
    flat_6h = _decimal(
        row, "flat_candle_pct_6h", minimum=Decimal("0"), maximum=Decimal("100")
    )
    flat_24h = _decimal(
        row, "flat_candle_pct_24h", minimum=Decimal("0"), maximum=Decimal("100")
    )
    minutes = _integer(row, "minutes_since_meaningful_move")
    volume = _decimal(
        row, "volume_percentile_30d", minimum=Decimal("0"), maximum=Decimal("100")
    )
    activity = _decimal(
        row,
        "activity_percentile_30d",
        minimum=Decimal("0"),
        maximum=Decimal("100"),
    )
    if None in (flat_1h, flat_6h, flat_24h, minutes, volume, activity):
        return False
    return bool(
        flat_1h >= selected.min_flat_candle_pct_1h
        and flat_6h >= selected.min_flat_candle_pct_6h
        and flat_24h >= selected.min_flat_candle_pct_24h
        and minutes >= selected.min_minutes_since_meaningful_move
        and volume <= selected.max_volume_percentile_30d
        and activity <= selected.max_activity_percentile_30d
    )


def is_ignition(row: Row, definition: Optional[Ignition] = None) -> bool:
    """Return whether a row proves a sudden, confirmed ignition."""
    selected = definition or cast(Ignition, DEFINITIONS["Ignition"])
    volume_percentile = _decimal(
        row, "volume_percentile_30d", minimum=Decimal("0"), maximum=Decimal("100")
    )
    activity_percentile = _decimal(
        row,
        "activity_percentile_30d",
        minimum=Decimal("0"),
        maximum=Decimal("100"),
    )
    price_acceleration = _decimal(row, "price_acceleration_5m_pct")
    accelerations = (
        _decimal(row, "volume_acceleration_1m", minimum=Decimal("0")),
        _decimal(row, "volume_acceleration_5m", minimum=Decimal("0")),
        _decimal(row, "volume_acceleration_10m", minimum=Decimal("0")),
        _decimal(row, "volume_acceleration_15m", minimum=Decimal("0")),
        _decimal(row, "volume_acceleration_60m", minimum=Decimal("0")),
    )
    if (
        volume_percentile is None
        or activity_percentile is None
        or price_acceleration is None
        or any(value is None for value in accelerations)
    ):
        return False
    thresholds = (
        selected.min_volume_acceleration_1m,
        selected.min_volume_acceleration_5m,
        selected.min_volume_acceleration_10m,
        selected.min_volume_acceleration_15m,
        selected.min_volume_acceleration_60m,
    )
    passed = sum(
        1
        for value, threshold in zip(accelerations, thresholds)
        if value is not None and value >= threshold
    )
    return bool(
        volume_percentile >= selected.min_volume_percentile_30d
        and activity_percentile >= selected.min_activity_percentile_30d
        and accelerations[0] >= selected.min_volume_acceleration_1m
        and accelerations[1] >= selected.min_volume_acceleration_5m
        and passed >= selected.min_acceleration_windows
        and price_acceleration >= selected.min_price_acceleration_5m_pct
    )


def is_genuine_runner(
    row: Row, definition: Optional[GenuineRunner] = None
) -> bool:
    """Return whether an ignition proves material persistent follow-through."""
    selected = definition or cast(
        GenuineRunner, DEFINITIONS["GenuineRunner"]
    )
    ignition = _flag(row, "ignition")
    return_15m = _decimal(row, "forward_return_15m_pct")
    return_60m = _decimal(row, "forward_return_60m_pct")
    favorable = _decimal(
        row, "max_favorable_excursion_60m_pct", minimum=Decimal("0")
    )
    adverse = _decimal(
        row,
        "max_adverse_excursion_60m_pct",
        minimum=Decimal("-100"),
        maximum=Decimal("0"),
    )
    positive = _integer(row, "positive_candles_next_15m", maximum=15)
    if None in (
        ignition,
        return_15m,
        return_60m,
        favorable,
        adverse,
        positive,
    ):
        return False
    return bool(
        ignition
        and return_15m >= selected.min_forward_return_15m_pct
        and return_60m >= selected.min_forward_return_60m_pct
        and favorable >= selected.min_favorable_excursion_60m_pct
        and -adverse <= selected.max_adverse_excursion_60m_pct
        and positive >= selected.min_positive_candles_next_15m
    )


def is_false_ignition(
    row: Row, definition: Optional[FalseIgnition] = None
) -> bool:
    """Return whether an ignition proves immaterial, non-persistent follow-up."""
    selected = definition or cast(
        FalseIgnition, DEFINITIONS["FalseIgnition"]
    )
    ignition = _flag(row, "ignition")
    forward = _decimal(row, "forward_return_15m_pct")
    favorable = _decimal(
        row, "max_favorable_excursion_15m_pct", minimum=Decimal("0")
    )
    active = _integer(row, "active_minutes_next_5m", maximum=5)
    if None in (ignition, forward, favorable, active):
        return False
    return bool(
        ignition
        and favorable <= selected.max_favorable_excursion_15m_pct
        and abs(forward) <= selected.max_abs_forward_return_15m_pct
        and active <= selected.max_active_minutes_next_5m
    )


def is_slow_ramp(row: Row, definition: Optional[SlowRamp] = None) -> bool:
    """Return whether an hour proves broad gradual appreciation without ignition."""
    selected = definition or cast(SlowRamp, DEFINITIONS["SlowRamp"])
    ignition = _flag(row, "ignition")
    price_change = _decimal(row, "price_change_60m_pct")
    max_candle = _decimal(
        row, "max_1m_price_change_60m_pct", minimum=Decimal("0")
    )
    positive = _integer(row, "positive_candles_60m", maximum=60)
    volume_1m = _decimal(
        row, "volume_acceleration_1m", minimum=Decimal("0")
    )
    volume_60m = _decimal(
        row, "volume_acceleration_60m", minimum=Decimal("0")
    )
    if None in (
        ignition,
        price_change,
        max_candle,
        positive,
        volume_1m,
        volume_60m,
    ):
        return False
    return bool(
        not ignition
        and price_change >= selected.min_price_change_60m_pct
        and max_candle <= selected.max_1m_price_change_60m_pct
        and positive >= selected.min_positive_candles_60m
        and volume_60m >= selected.min_volume_acceleration_60m
        and volume_1m <= selected.max_volume_acceleration_1m
    )


def is_already_pumped(
    row: Row, definition: Optional[AlreadyPumped] = None
) -> bool:
    """Return whether a new ignition arrives after a proved recent pump."""
    selected = definition or cast(
        AlreadyPumped, DEFINITIONS["AlreadyPumped"]
    )
    ignition = _flag(row, "ignition")
    prior_return = _decimal(row, "price_change_prior_60m_pct")
    minutes = _integer(row, "minutes_since_meaningful_move")
    volume_percentile = _decimal(
        row, "volume_percentile_30d", minimum=Decimal("0"), maximum=Decimal("100")
    )
    volume_60m = _decimal(
        row, "volume_acceleration_60m", minimum=Decimal("0")
    )
    if None in (
        ignition,
        prior_return,
        minutes,
        volume_percentile,
        volume_60m,
    ):
        return False
    return bool(
        ignition
        and prior_return >= selected.min_price_change_prior_60m_pct
        and minutes <= selected.max_minutes_since_meaningful_move
        and volume_percentile >= selected.min_volume_percentile_30d
        and volume_60m >= selected.min_volume_acceleration_60m
    )


__all__ = (
    "AlreadyPumped",
    "DEFINITIONS",
    "DeadRegime",
    "FalseIgnition",
    "GenuineRunner",
    "Ignition",
    "SlowRamp",
    "is_already_pumped",
    "is_dead_regime",
    "is_false_ignition",
    "is_genuine_runner",
    "is_ignition",
    "is_slow_ramp",
)
