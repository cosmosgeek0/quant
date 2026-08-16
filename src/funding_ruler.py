"""Pure per-symbol funding ruler for an intended basis hold.

Funding is discrete: a settlement exactly at the hold endpoint is crossed.
``MAX_SETTLEMENTS`` bounds both the forecast horizon and returned timestamp
tuple.  Rates are fractions per settlement; drag outputs are percent text.

Direction follows ``src/basis_kernel.py:971-989``
(``funding_carry_pct``): NORMAL is long Spot / short Perp and receives a
positive rate, while REVERSE is short Spot / long Perp and pays it.  Therefore
positive drag means paid funding and negative drag means received funding.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Literal, Optional, Tuple, Union


NORMAL = "long_spot_short_perp"
REVERSE = "short_spot_long_perp"
MAX_SETTLEMENTS = 64

ZERO = Decimal("0")
ONE_HOUR_MS = Decimal("3600000")
ONE_MINUTE_MS = Decimal("60000")
PERCENT = Decimal("100")
PRECISION = 50


@dataclass(frozen=True)
class FundingInfo:
    """Typed per-symbol limits from fundingInfo; None stays unknown."""

    cap: Optional[Decimal]
    floor: Optional[Decimal]
    interval_hours: Optional[int]


@dataclass(frozen=True)
class FundingRulerReading:
    settlements_crossed: int
    worst_case_drag_pct: str
    expected_drag_pct: str
    next_settlement_timestamps_ms: Tuple[str, ...]
    verdict: Literal["READY"] = "READY"


@dataclass(frozen=True)
class FundingRulerRefusal:
    reason: str
    verdict: Literal["REFUSED"] = "REFUSED"


FundingRulerResult = Union[FundingRulerReading, FundingRulerRefusal]


def _decimal_text_input(value: Any) -> Optional[Decimal]:
    if type(value) is not str or not value:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _finite_decimal(value: Any) -> Optional[Decimal]:
    if not isinstance(value, Decimal) or not value.is_finite():
        return None
    return value


def _timestamp_text_input(value: Any) -> Optional[Decimal]:
    parsed = _decimal_text_input(value)
    if parsed is None or parsed < ZERO or parsed != parsed.to_integral_value():
        return None
    return parsed


def _start_timestamp(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return Decimal(value)


def _text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if value == ZERO else rendered


def _refused(reason: str) -> FundingRulerRefusal:
    return FundingRulerRefusal(reason)


def rule_funding(
    funding_rate: Any,
    next_funding_ms: Any,
    info: FundingInfo,
    start_ms: Any,
    expected_hold_minutes: Any,
    direction: str,
) -> FundingRulerResult:
    """Measure crossed settlements and directional funding drag.

    ``funding_rate`` and ``next_funding_ms`` are the Decimal-text stream
    fields.  ``start_ms`` is an integer epoch millisecond and the hold is a
    finite nonnegative Decimal minute count.  No input is defaulted or capped.
    """

    if not isinstance(info, FundingInfo) or (
        info.cap is None or info.floor is None or info.interval_hours is None
    ):
        return _refused("UNKNOWN_FUNDING_INFO")

    cap = _finite_decimal(info.cap)
    floor = _finite_decimal(info.floor)
    interval_hours = info.interval_hours
    if (
        cap is None or floor is None or floor > cap
        or isinstance(interval_hours, bool)
        or not isinstance(interval_hours, int) or interval_hours <= 0
    ):
        return _refused("INVALID_FUNDING_INFO")

    if direction not in (NORMAL, REVERSE):
        return _refused("UNKNOWN_DIRECTION")

    rate = _decimal_text_input(funding_rate)
    next_timestamp = _timestamp_text_input(next_funding_ms)
    if funding_rate is None or next_funding_ms is None:
        return _refused("UNKNOWN_STREAM_FUNDING")
    if rate is None or next_timestamp is None or not floor <= rate <= cap:
        return _refused("INVALID_STREAM_FUNDING")

    start = _start_timestamp(start_ms)
    hold_minutes = _finite_decimal(expected_hold_minutes)
    if start is None or hold_minutes is None or hold_minutes < ZERO:
        return _refused("INVALID_HOLD_WINDOW")
    if next_timestamp <= start:
        return _refused("NEXT_SETTLEMENT_NOT_AFTER_START")

    with localcontext() as context:
        context.prec = PRECISION
        hold_end = start + hold_minutes * ONE_MINUTE_MS
        period_ms = Decimal(interval_hours) * ONE_HOUR_MS
        if next_timestamp > hold_end:
            count = 0
        else:
            count = int((hold_end - next_timestamp) // period_ms) + 1
        if count > MAX_SETTLEMENTS:
            return _refused("SETTLEMENT_LIMIT_EXCEEDED")

        timestamps = tuple(
            _text(next_timestamp + Decimal(index) * period_ms)
            for index in range(count)
        )
        drag_sign = Decimal("-1") if direction == NORMAL else Decimal("1")
        worst_rate_drag = max(floor * drag_sign, cap * drag_sign)
        multiplier = Decimal(count) * PERCENT
        return FundingRulerReading(
            settlements_crossed=count,
            worst_case_drag_pct=_text(worst_rate_drag * multiplier),
            expected_drag_pct=_text(rate * drag_sign * multiplier),
            next_settlement_timestamps_ms=timestamps,
        )
