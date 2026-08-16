#!/usr/bin/env python3
"""Deterministic, offline policy simulation over labelled price tapes.

Policy percentages (``stop_pct`` and partial ``gain_pct``) are percentage
points: ``5`` means five percent.  Fee, spread, and slippage values are
per-side fractional rates: ``0.001`` means ten basis points.  Entry-stage
triggers are whole minutes after ``entry_delay_min``; ``final_exit`` is whole
minutes after the labelled event.

The simulator has no network or execution path.  Market and money arithmetic
is Decimal-only.  Every completed trade row exposes entry and exit fees,
spread cost, conservative slippage cost, and their total.  An unproved entry
or exit produces an ``UNKNOWN`` row with those economic results set to None.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


try:
    from study.windows import validate_symbol
    from study.windows import WindowError as SymbolValidationError
except ModuleNotFoundError as error:
    if error.name != "study":
        raise
    from windows import validate_symbol
    from windows import WindowError as SymbolValidationError


STUDY_DIR = Path(__file__).resolve().parent
DEFAULT_EVENTS_PATH = STUDY_DIR / "out" / "v2" / "events.jsonl"
DEFAULT_TAPE_PATH = STUDY_DIR / "out" / "v2" / "tape_prices.jsonl"
DEFAULT_OUTPUT_PATH = STUDY_DIR / "out" / "v2" / "trades.jsonl"
VENUE_CLASSES = ("spot_only", "futures_only", "both")
MINUTE_MS = 60_000
PERCENT_QUANTUM = Decimal("0.000000000000000001")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class PolicySimError(Exception):
    """The policy, event, tape, or result cannot be proved valid."""


def _decimal_value(
    value: Any,
    source: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PolicySimError(f"{source} must be a finite Decimal")
    if positive and value <= 0:
        raise PolicySimError(f"{source} must be positive")
    if non_negative and value < 0:
        raise PolicySimError(f"{source} must be non-negative")
    return value


def parse_decimal(
    raw: Any,
    source: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    """Parse Decimal text while refusing binary floating-point inputs."""
    if isinstance(raw, Decimal):
        value = raw
    elif type(raw) is str and raw.strip():
        try:
            value = Decimal(raw.strip())
        except InvalidOperation:
            raise PolicySimError(f"{source} is not Decimal text") from None
    else:
        raise PolicySimError(f"{source} is not Decimal text")
    return _decimal_value(
        value,
        source,
        positive=positive,
        non_negative=non_negative,
    )


def decimal_text(value: Decimal) -> str:
    """Render a finite Decimal without exponent notation."""
    _decimal_value(value, "output Decimal")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _reported_percent(value: Decimal) -> Decimal:
    """Round visible percentages so downstream Decimal sums stay exact."""
    _decimal_value(value, "reported percentage")
    with localcontext() as context:
        context.prec = 50
        return value.quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_EVEN)


def _whole_minute(value: Any, source: str, *, non_negative: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicySimError(f"{source} must be a whole minute integer")
    if non_negative and value < 0:
        raise PolicySimError(f"{source} must be non-negative")
    return value


def _timestamp_ms(value: Any, source: str) -> int:
    if isinstance(value, bool):
        raise PolicySimError(f"{source} must be an integer timestamp")
    if isinstance(value, int):
        parsed = value
    elif type(value) is str and value.strip():
        try:
            parsed = int(value.strip())
        except ValueError:
            raise PolicySimError(f"{source} must be an integer timestamp") from None
    else:
        raise PolicySimError(f"{source} must be an integer timestamp")
    if parsed >= 100_000_000_000_000:
        parsed //= 1_000
    if parsed < 100_000_000_000 or parsed >= 100_000_000_000_000:
        raise PolicySimError(f"{source} is outside the supported epoch range")
    return parsed


def _utc_ms(value: Any, source: str) -> int:
    if type(value) is not str or not value.endswith("Z"):
        raise PolicySimError(f"{source} is not millisecond UTC text")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise PolicySimError(f"{source} is not millisecond UTC text") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PolicySimError(f"{source} is not millisecond UTC text")
    delta = parsed - EPOCH
    if delta.days < 0 or delta.microseconds % 1_000:
        raise PolicySimError(f"{source} is not millisecond UTC text")
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def utc_text(timestamp_ms: int) -> str:
    value = EPOCH + timedelta(milliseconds=timestamp_ms)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize_venue(value: Any, source: str) -> str:
    aliases = {
        "spot": "spot",
        "perp": "perp",
        "futures": "perp",
        "futures_perp": "perp",
        "perpetual": "perp",
    }
    if type(value) is not str or value not in aliases:
        raise PolicySimError(f"{source} must be spot or perp")
    return aliases[value]


@dataclass(frozen=True)
class Policy:
    """Complete economic policy for one long-only research simulation.

    Each stage is ``(fraction_of_max_allocation, trigger_minutes)``.  The first
    trigger must be zero and later triggers strictly increase.  Each partial
    is ``(fraction_of_acquired_quantity, gain_pct)`` and fires at most once.
    """

    entry_delay_min: int
    stages: Tuple[Tuple[Decimal, int], ...]
    max_alloc_usdt: Decimal
    stop_pct: Decimal
    partials: Tuple[Tuple[Decimal, Decimal], ...]
    final_exit: int
    spread_cost: Decimal
    slippage_conservative: Decimal
    spot_taker_fee: Decimal = Decimal("0.001")
    perp_taker_fee: Decimal = Decimal("0.0005")

    def __post_init__(self) -> None:
        _whole_minute(
            self.entry_delay_min, "entry_delay_min", non_negative=True
        )
        _whole_minute(self.final_exit, "final_exit", non_negative=False)
        if self.final_exit <= 0:
            raise PolicySimError("final_exit must be positive")
        _decimal_value(self.max_alloc_usdt, "max_alloc_usdt", positive=True)
        _decimal_value(self.stop_pct, "stop_pct", positive=True)
        if self.stop_pct >= Decimal("100"):
            raise PolicySimError("stop_pct must be below 100")

        stages = self._validate_stages(self.stages)
        partials = self._validate_partials(self.partials)
        object.__setattr__(self, "stages", stages)
        object.__setattr__(self, "partials", partials)

        for name, value in (
            ("spread_cost", self.spread_cost),
            ("slippage_conservative", self.slippage_conservative),
            ("spot_taker_fee", self.spot_taker_fee),
            ("perp_taker_fee", self.perp_taker_fee),
        ):
            _decimal_value(value, name, non_negative=True)
            if value >= Decimal("1"):
                raise PolicySimError(f"{name} must be below 1")
        if self.spread_cost + self.slippage_conservative >= Decimal("1"):
            raise PolicySimError(
                "spread_cost plus slippage_conservative must be below 1"
            )
        last_entry_minute = self.entry_delay_min + stages[-1][1]
        if self.final_exit <= last_entry_minute:
            raise PolicySimError(
                "final_exit must follow every scheduled entry stage"
            )

    @staticmethod
    def _validate_stages(
        raw: Any,
    ) -> Tuple[Tuple[Decimal, int], ...]:
        if not isinstance(raw, (list, tuple)) or not raw:
            raise PolicySimError("stages must contain at least one pair")
        stages: List[Tuple[Decimal, int]] = []
        total = Decimal("0")
        previous_trigger: Optional[int] = None
        for index, item in enumerate(raw):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise PolicySimError(f"stages[{index}] must be a pair")
            fraction = _decimal_value(
                item[0], f"stages[{index}] fraction", positive=True
            )
            if fraction > Decimal("1"):
                raise PolicySimError(
                    f"stages[{index}] fraction must not exceed 1"
                )
            trigger = _whole_minute(
                item[1], f"stages[{index}] trigger", non_negative=True
            )
            if previous_trigger is not None and trigger <= previous_trigger:
                raise PolicySimError("stage triggers must strictly increase")
            previous_trigger = trigger
            total += fraction
            stages.append((fraction, trigger))
        if stages[0][1] != 0:
            raise PolicySimError("the first stage trigger must be zero")
        if total > Decimal("1"):
            raise PolicySimError("stage fractions exceed the allocation cap")
        return tuple(stages)

    @staticmethod
    def _validate_partials(
        raw: Any,
    ) -> Tuple[Tuple[Decimal, Decimal], ...]:
        if not isinstance(raw, (list, tuple)):
            raise PolicySimError("partials must be a sequence of pairs")
        partials: List[Tuple[Decimal, Decimal]] = []
        total = Decimal("0")
        previous_gain: Optional[Decimal] = None
        for index, item in enumerate(raw):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise PolicySimError(f"partials[{index}] must be a pair")
            fraction = _decimal_value(
                item[0], f"partials[{index}] fraction", positive=True
            )
            gain = _decimal_value(
                item[1], f"partials[{index}] gain_pct", positive=True
            )
            if fraction > Decimal("1"):
                raise PolicySimError(
                    f"partials[{index}] fraction must not exceed 1"
                )
            if previous_gain is not None and gain <= previous_gain:
                raise PolicySimError("partial gains must strictly increase")
            previous_gain = gain
            total += fraction
            partials.append((fraction, gain))
        if total > Decimal("1"):
            raise PolicySimError("partial fractions exceed acquired quantity")
        return tuple(partials)

    def fee_rate_for(self, venue: str) -> Decimal:
        normalized = _normalize_venue(venue, "venue")
        return (
            self.spot_taker_fee
            if normalized == "spot"
            else self.perp_taker_fee
        )

    @property
    def taker_fee(self) -> Mapping[str, Decimal]:
        """Return the explicit per-side venue fee schedule."""
        return {
            "spot": self.spot_taker_fee,
            "perp": self.perp_taker_fee,
        }

    @property
    def taker_fee_spot(self) -> Decimal:
        return self.spot_taker_fee

    @property
    def taker_fee_perp(self) -> Decimal:
        return self.perp_taker_fee


@dataclass(frozen=True)
class LabelledEvent:
    event_id: str
    symbol: str
    label: str
    venue_class: str
    event_time_ms: int
    venue: Optional[str] = None
    runner_score: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if type(self.event_id) is not str or not self.event_id:
            raise PolicySimError("event_id must be non-empty text")
        try:
            symbol = validate_symbol(self.symbol, "event symbol")
        except SymbolValidationError:
            raise PolicySimError("event symbol is invalid") from None
        object.__setattr__(self, "symbol", symbol)
        if type(self.label) is not str or not self.label:
            raise PolicySimError("event label must be non-empty text")
        if self.venue_class not in VENUE_CLASSES:
            raise PolicySimError("event venue_class is invalid")
        event_time_ms = _timestamp_ms(self.event_time_ms, "event_time_ms")
        object.__setattr__(self, "event_time_ms", event_time_ms)

        if self.venue is None:
            if self.venue_class == "both":
                raise PolicySimError("both-venue events require an execution venue")
            venue = "spot" if self.venue_class == "spot_only" else "perp"
        else:
            venue = _normalize_venue(self.venue, "event venue")
        if self.venue_class == "spot_only" and venue != "spot":
            raise PolicySimError("spot_only event cannot execute on perp")
        if self.venue_class == "futures_only" and venue != "perp":
            raise PolicySimError("futures_only event cannot execute on spot")
        object.__setattr__(self, "venue", venue)
        if self.runner_score is not None:
            _decimal_value(self.runner_score, "event runner_score")


@dataclass(frozen=True)
class TapePrice:
    symbol: str
    venue: str
    timestamp_ms: int
    price: Decimal

    def __post_init__(self) -> None:
        try:
            symbol = validate_symbol(self.symbol, "tape symbol")
        except SymbolValidationError:
            raise PolicySimError("tape symbol is invalid") from None
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "venue", _normalize_venue(self.venue, "tape venue"))
        timestamp_ms = _timestamp_ms(self.timestamp_ms, "tape timestamp_ms")
        object.__setattr__(self, "timestamp_ms", timestamp_ms)
        _decimal_value(self.price, "tape price", positive=True)


def _policy_row(policy: Policy) -> Dict[str, Any]:
    return {
        "entry_delay_min": policy.entry_delay_min,
        "stages": [
            {
                "fraction": decimal_text(fraction),
                "trigger_min_after_delay": trigger,
            }
            for fraction, trigger in policy.stages
        ],
        "max_alloc_usdt": decimal_text(policy.max_alloc_usdt),
        "stop_pct": decimal_text(policy.stop_pct),
        "partials": [
            {
                "fraction": decimal_text(fraction),
                "gain_pct": decimal_text(gain),
            }
            for fraction, gain in policy.partials
        ],
        "final_exit_min_after_event": policy.final_exit,
        "spot_taker_fee_rate_per_side": decimal_text(
            policy.spot_taker_fee
        ),
        "perp_taker_fee_rate_per_side": decimal_text(
            policy.perp_taker_fee
        ),
        "spread_cost_rate_per_side": decimal_text(policy.spread_cost),
        "slippage_conservative_rate_per_side": decimal_text(
            policy.slippage_conservative
        ),
    }


def _base_row(event: LabelledEvent, policy: Policy) -> Dict[str, Any]:
    fee_rate = policy.fee_rate_for(event.venue or "")
    return {
        "schema": "volume-policy-trade-v2",
        "kind": "trade",
        "event_id": event.event_id,
        "symbol": event.symbol,
        "label": event.label,
        "venue_class": event.venue_class,
        "venue": event.venue,
        "event_time_ms": event.event_time_ms,
        "event_utc": utc_text(event.event_time_ms),
        "runner_score": (
            None
            if event.runner_score is None
            else decimal_text(event.runner_score)
        ),
        "policy": _policy_row(policy),
        "taker_fee_rate_per_side": decimal_text(fee_rate),
        "spread_cost_rate_per_side": decimal_text(policy.spread_cost),
        "slippage_conservative_rate_per_side": decimal_text(
            policy.slippage_conservative
        ),
    }


def _unknown_row(
    event: LabelledEvent,
    policy: Policy,
    reason: str,
    entry_fills: Sequence[Mapping[str, Any]] = (),
    exit_fills: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    row = _base_row(event, policy)
    row.update(
        {
            "status": "UNKNOWN",
            "unknown_reason": reason,
            "exit_reason": None,
            "stop_triggered": None,
            "entry_fills": [dict(fill) for fill in entry_fills],
            "exit_fills": [dict(fill) for fill in exit_fills],
            "entry_latency_min": None,
            "mfe_pct": None,
            "mae_pct": None,
            "time_to_peak_min": None,
            "allocation_cap_usdt": decimal_text(policy.max_alloc_usdt),
            "allocation_used_usdt": None,
            "allocation_cap_respected": None,
            "entry_quantity": None,
            "entry_vwap_tape_price": None,
            "exit_vwap_tape_price": None,
            "gross_entry_notional_usdt": None,
            "gross_exit_notional_usdt": None,
            "executed_entry_notional_usdt": None,
            "executed_exit_notional_usdt": None,
            "entry_fee_usdt": None,
            "exit_fee_usdt": None,
            "fee_cost_usdt": None,
            "fee_cost_pct": None,
            "entry_spread_cost_usdt": None,
            "exit_spread_cost_usdt": None,
            "spread_cost_usdt": None,
            "spread_cost_pct": None,
            "entry_slippage_cost_usdt": None,
            "exit_slippage_cost_usdt": None,
            "slippage_cost_usdt": None,
            "slippage_cost_pct": None,
            "total_cost_usdt": None,
            "all_costs_pct": None,
            "gross_pnl_usdt": None,
            "net_pnl_usdt": None,
            "gross_return_pct": None,
            "net_return_pct": None,
        }
    )
    return row


def _entry_fill(
    event: LabelledEvent,
    policy: Policy,
    point: TapePrice,
    stage_index: int,
    fraction: Decimal,
    trigger: int,
) -> Tuple[Dict[str, Any], Dict[str, Decimal]]:
    fee_rate = policy.fee_rate_for(event.venue or "")
    budget = policy.max_alloc_usdt * fraction
    total_rate = policy.spread_cost + policy.slippage_conservative
    gross_notional = budget / (Decimal("1") + total_rate)
    spread = gross_notional * policy.spread_cost
    slippage = budget - gross_notional - spread
    quantity = gross_notional / point.price
    execution_price = budget / quantity
    fee = budget * fee_rate
    due_ms = event.event_time_ms + (
        policy.entry_delay_min + trigger
    ) * MINUTE_MS
    fill = {
        "side": "buy",
        "reason": "entry_stage",
        "stage_index": stage_index,
        "stage_fraction": decimal_text(fraction),
        "trigger_min_after_delay": trigger,
        "scheduled_time_ms": due_ms,
        "scheduled_utc": utc_text(due_ms),
        "timestamp_ms": point.timestamp_ms,
        "utc": utc_text(point.timestamp_ms),
        "tape_price": decimal_text(point.price),
        "execution_price": decimal_text(execution_price),
        "quantity": decimal_text(quantity),
        "gross_notional_usdt": decimal_text(gross_notional),
        "executed_notional_usdt": decimal_text(budget),
        "spread_cost_usdt": decimal_text(spread),
        "slippage_cost_usdt": decimal_text(slippage),
        "fee_usdt": decimal_text(fee),
    }
    values = {
        "quantity": quantity,
        "gross": gross_notional,
        "executed": budget,
        "spread": spread,
        "slippage": slippage,
        "fee": fee,
    }
    return fill, values


def _exit_fill(
    event: LabelledEvent,
    policy: Policy,
    point: TapePrice,
    quantity: Decimal,
    reason: str,
    *,
    partial_index: Optional[int] = None,
    partial_fraction: Optional[Decimal] = None,
    trigger_pct: Optional[Decimal] = None,
    trigger_price: Optional[Decimal] = None,
) -> Tuple[Dict[str, Any], Dict[str, Decimal]]:
    fee_rate = policy.fee_rate_for(event.venue or "")
    gross_notional = quantity * point.price
    spread = gross_notional * policy.spread_cost
    slippage = gross_notional * policy.slippage_conservative
    executed = gross_notional - spread - slippage
    execution_price = executed / quantity
    fee = executed * fee_rate
    fill: Dict[str, Any] = {
        "side": "sell",
        "reason": reason,
        "timestamp_ms": point.timestamp_ms,
        "utc": utc_text(point.timestamp_ms),
        "tape_price": decimal_text(point.price),
        "execution_price": decimal_text(execution_price),
        "quantity": decimal_text(quantity),
        "gross_notional_usdt": decimal_text(gross_notional),
        "executed_notional_usdt": decimal_text(executed),
        "spread_cost_usdt": decimal_text(spread),
        "slippage_cost_usdt": decimal_text(slippage),
        "fee_usdt": decimal_text(fee),
        "partial_index": partial_index,
        "partial_fraction": (
            None
            if partial_fraction is None
            else decimal_text(partial_fraction)
        ),
        "trigger_pct": (
            None if trigger_pct is None else decimal_text(trigger_pct)
        ),
        "trigger_price": (
            None if trigger_price is None else decimal_text(trigger_price)
        ),
    }
    values = {
        "quantity": quantity,
        "gross": gross_notional,
        "executed": executed,
        "spread": spread,
        "slippage": slippage,
        "fee": fee,
    }
    return fill, values


def _sum_values(values: Sequence[Mapping[str, Decimal]], name: str) -> Decimal:
    return sum((value[name] for value in values), Decimal("0"))


def _cost_reconciliation_detail(
    event: LabelledEvent,
    entry_fills: Sequence[Mapping[str, Any]],
    *,
    gross_entry: Decimal,
    gross_exit: Decimal,
    executed_entry: Decimal,
    executed_exit: Decimal,
    entry_fee: Decimal,
    exit_fee: Decimal,
    entry_spread: Decimal,
    exit_spread: Decimal,
    entry_slippage: Decimal,
    exit_slippage: Decimal,
    fee_cost: Decimal,
    spread_cost: Decimal,
    slippage_cost: Decimal,
    total_cost: Decimal,
    gross_pnl: Decimal,
    net_pnl: Decimal,
    cash_net: Decimal,
) -> str:
    """Name every Decimal operand used by the reconciliation guard."""
    stage_index = entry_fills[-1]["stage_index"]
    fields = (
        ("fee_in", entry_fee),
        ("fee_out", exit_fee),
        ("fee", fee_cost),
        ("spread_in", entry_spread),
        ("spread_out", exit_spread),
        ("spread", spread_cost),
        ("slippage_in", entry_slippage),
        ("slippage_out", exit_slippage),
        ("slippage", slippage_cost),
        ("total_cost", total_cost),
        ("gross_in", gross_entry),
        ("gross_out", gross_exit),
        ("gross", gross_pnl),
        ("executed_in", executed_entry),
        ("executed_out", executed_exit),
        ("net", net_pnl),
        ("cash_net", cash_net),
        ("compared_total_costs", net_pnl),
        ("compared_total_cash", cash_net),
        ("difference", cash_net - net_pnl),
    )
    detail = " | ".join(
        f"{name}={decimal_text(value)}" for name, value in fields
    )
    return (
        "cost decomposition does not reconcile | "
        f"event_id={event.event_id} | stage_index={stage_index} | {detail}"
    )


def simulate_event(
    event: LabelledEvent,
    tape_prices: Sequence[TapePrice],
    policy: Policy,
) -> Dict[str, Any]:
    """Simulate one event and return its complete trades.jsonl row."""
    if not isinstance(event, LabelledEvent):
        raise PolicySimError("event must be a LabelledEvent")
    if not isinstance(policy, Policy):
        raise PolicySimError("policy must be a Policy")
    points: List[TapePrice] = []
    for point in tape_prices:
        if not isinstance(point, TapePrice):
            raise PolicySimError("tape prices must contain TapePrice values")
        if point.symbol == event.symbol and point.venue == event.venue:
            points.append(point)
    points.sort(key=lambda point: point.timestamp_ms)
    if not points:
        return _unknown_row(event, policy, "no_matching_tape_prices")

    final_due_ms = event.event_time_ms + policy.final_exit * MINUTE_MS
    entry_fills: List[Dict[str, Any]] = []
    exit_fills: List[Dict[str, Any]] = []
    entry_values: List[Dict[str, Decimal]] = []
    exit_values: List[Dict[str, Decimal]] = []
    next_stage = 0
    triggered_partials = set()
    position_quantity = Decimal("0")
    acquired_quantity = Decimal("0")
    position_basis = Decimal("0")
    exit_reason: Optional[str] = None

    with localcontext() as context:
        context.prec = 50
        for point in points:
            if point.timestamp_ms < event.event_time_ms:
                continue
            if point.timestamp_ms >= final_due_ms:
                if position_quantity > 0:
                    fill, values = _exit_fill(
                        event,
                        policy,
                        point,
                        position_quantity,
                        "final_exit",
                    )
                    exit_fills.append(fill)
                    exit_values.append(values)
                    position_quantity = Decimal("0")
                    position_basis = Decimal("0")
                    exit_reason = "final_exit"
                break

            if position_quantity > 0:
                average_entry = position_basis / position_quantity
                stop_price = average_entry * (
                    Decimal("1") - policy.stop_pct / Decimal("100")
                )
                if point.price <= stop_price:
                    sold_quantity = position_quantity
                    fill, values = _exit_fill(
                        event,
                        policy,
                        point,
                        sold_quantity,
                        "stop",
                        trigger_pct=policy.stop_pct,
                        trigger_price=stop_price,
                    )
                    exit_fills.append(fill)
                    exit_values.append(values)
                    position_quantity = Decimal("0")
                    position_basis = Decimal("0")
                    exit_reason = "stop"
                    break

            entered_here = False
            while next_stage < len(policy.stages):
                fraction, trigger = policy.stages[next_stage]
                due_ms = event.event_time_ms + (
                    policy.entry_delay_min + trigger
                ) * MINUTE_MS
                if point.timestamp_ms < due_ms:
                    break
                fill, values = _entry_fill(
                    event,
                    policy,
                    point,
                    next_stage,
                    fraction,
                    trigger,
                )
                entry_fills.append(fill)
                entry_values.append(values)
                position_quantity += values["quantity"]
                acquired_quantity += values["quantity"]
                position_basis += values["gross"]
                next_stage += 1
                entered_here = True
            if entered_here:
                continue
            if position_quantity <= 0:
                if entry_fills:
                    exit_reason = "partials_complete"
                    break
                continue

            average_entry = position_basis / position_quantity
            for partial_index, (fraction, gain) in enumerate(policy.partials):
                if partial_index in triggered_partials:
                    continue
                target = average_entry * (
                    Decimal("1") + gain / Decimal("100")
                )
                if point.price < target:
                    continue
                sold_quantity = min(
                    position_quantity, acquired_quantity * fraction
                )
                if sold_quantity <= 0:
                    raise PolicySimError("partial exit quantity is not positive")
                fill, values = _exit_fill(
                    event,
                    policy,
                    point,
                    sold_quantity,
                    "partial",
                    partial_index=partial_index,
                    partial_fraction=fraction,
                    trigger_pct=gain,
                    trigger_price=target,
                )
                exit_fills.append(fill)
                exit_values.append(values)
                basis_reduction = average_entry * sold_quantity
                position_quantity -= sold_quantity
                position_basis -= basis_reduction
                triggered_partials.add(partial_index)
                if position_quantity == 0:
                    position_basis = Decimal("0")
                    exit_reason = "partials_complete"
                    break
            if exit_reason == "partials_complete":
                break

        if not entry_fills:
            return _unknown_row(
                event, policy, "no_entry_price_before_final_exit"
            )
        if position_quantity > 0 or exit_reason is None:
            return _unknown_row(
                event,
                policy,
                "no_exit_price_at_or_after_final_exit",
                entry_fills,
                exit_fills,
            )

        gross_entry = _sum_values(entry_values, "gross")
        gross_exit = _sum_values(exit_values, "gross")
        executed_entry = _sum_values(entry_values, "executed")
        executed_exit = _sum_values(exit_values, "executed")
        entry_fee = _sum_values(entry_values, "fee")
        exit_fee = _sum_values(exit_values, "fee")
        entry_spread = _sum_values(entry_values, "spread")
        exit_spread = _sum_values(exit_values, "spread")
        entry_slippage = _sum_values(entry_values, "slippage")
        exit_slippage = _sum_values(exit_values, "slippage")
        fee_cost = entry_fee + exit_fee
        spread_cost = entry_spread + exit_spread
        slippage_cost = entry_slippage + exit_slippage
        total_cost = fee_cost + spread_cost + slippage_cost
        gross_pnl = gross_exit - gross_entry
        net_pnl = gross_pnl - total_cost
        cash_net = executed_exit - executed_entry - fee_cost
        if cash_net != net_pnl:
            raise PolicySimError(
                _cost_reconciliation_detail(
                    event,
                    entry_fills,
                    gross_entry=gross_entry,
                    gross_exit=gross_exit,
                    executed_entry=executed_entry,
                    executed_exit=executed_exit,
                    entry_fee=entry_fee,
                    exit_fee=exit_fee,
                    entry_spread=entry_spread,
                    exit_spread=exit_spread,
                    entry_slippage=entry_slippage,
                    exit_slippage=exit_slippage,
                    fee_cost=fee_cost,
                    spread_cost=spread_cost,
                    slippage_cost=slippage_cost,
                    total_cost=total_cost,
                    gross_pnl=gross_pnl,
                    net_pnl=net_pnl,
                    cash_net=cash_net,
                )
            )
        if executed_entry > policy.max_alloc_usdt:
            raise PolicySimError("entry allocation exceeded policy cap")
        gross_return = gross_pnl / executed_entry * Decimal("100")
        net_return = net_pnl / executed_entry * Decimal("100")
        fee_cost_pct = fee_cost / executed_entry * Decimal("100")
        spread_cost_pct = spread_cost / executed_entry * Decimal("100")
        slippage_cost_pct = (
            slippage_cost / executed_entry * Decimal("100")
        )
        all_costs_pct = total_cost / executed_entry * Decimal("100")
        gross_return_reported = _reported_percent(gross_return)
        fee_cost_pct_reported = _reported_percent(fee_cost_pct)
        spread_cost_pct_reported = _reported_percent(spread_cost_pct)
        slippage_cost_pct_reported = _reported_percent(slippage_cost_pct)
        all_costs_pct_reported = (
            fee_cost_pct_reported
            + spread_cost_pct_reported
            + slippage_cost_pct_reported
        )
        net_return_reported = (
            gross_return_reported - all_costs_pct_reported
        )
        entry_quantity = _sum_values(entry_values, "quantity")
        exit_quantity = _sum_values(exit_values, "quantity")
        if entry_quantity != exit_quantity:
            raise PolicySimError("closed trade quantities do not reconcile")
        entry_vwap = gross_entry / entry_quantity
        exit_vwap = gross_exit / exit_quantity
        first_entry_ms = int(entry_fills[0]["timestamp_ms"])
        last_exit_ms = int(exit_fills[-1]["timestamp_ms"])
        observed = [
            point
            for point in points
            if first_entry_ms <= point.timestamp_ms <= last_exit_ms
        ]
        if not observed:
            raise PolicySimError("closed trade has no observed price interval")
        highest = max(point.price for point in observed)
        lowest = min(point.price for point in observed)
        peak_point = next(point for point in observed if point.price == highest)
        mfe = (highest / entry_vwap - Decimal("1")) * Decimal("100")
        mae = (lowest / entry_vwap - Decimal("1")) * Decimal("100")
        time_to_peak = Decimal(
            peak_point.timestamp_ms - first_entry_ms
        ) / Decimal(MINUTE_MS)
        latency = Decimal(
            entry_fills[0]["timestamp_ms"] - event.event_time_ms
        ) / Decimal(MINUTE_MS)

        row = _base_row(event, policy)
        row.update(
            {
                "status": "CLOSED",
                "unknown_reason": None,
                "exit_reason": exit_reason,
                "stop_triggered": exit_reason == "stop",
                "entry_fills": entry_fills,
                "exit_fills": exit_fills,
                "entry_latency_min": decimal_text(latency),
                "mfe_pct": decimal_text(mfe),
                "mae_pct": decimal_text(mae),
                "time_to_peak_min": decimal_text(time_to_peak),
                "allocation_cap_usdt": decimal_text(
                    policy.max_alloc_usdt
                ),
                "allocation_used_usdt": decimal_text(executed_entry),
                "allocation_cap_respected": True,
                "entry_quantity": decimal_text(entry_quantity),
                "entry_vwap_tape_price": decimal_text(entry_vwap),
                "exit_vwap_tape_price": decimal_text(exit_vwap),
                "gross_entry_notional_usdt": decimal_text(gross_entry),
                "gross_exit_notional_usdt": decimal_text(gross_exit),
                "executed_entry_notional_usdt": decimal_text(
                    executed_entry
                ),
                "executed_exit_notional_usdt": decimal_text(executed_exit),
                "entry_fee_usdt": decimal_text(entry_fee),
                "exit_fee_usdt": decimal_text(exit_fee),
                "fee_cost_usdt": decimal_text(fee_cost),
                "fee_cost_pct": decimal_text(fee_cost_pct_reported),
                "entry_spread_cost_usdt": decimal_text(entry_spread),
                "exit_spread_cost_usdt": decimal_text(exit_spread),
                "spread_cost_usdt": decimal_text(spread_cost),
                "spread_cost_pct": decimal_text(spread_cost_pct_reported),
                "entry_slippage_cost_usdt": decimal_text(entry_slippage),
                "exit_slippage_cost_usdt": decimal_text(exit_slippage),
                "slippage_cost_usdt": decimal_text(slippage_cost),
                "slippage_cost_pct": decimal_text(
                    slippage_cost_pct_reported
                ),
                "total_cost_usdt": decimal_text(total_cost),
                "all_costs_pct": decimal_text(all_costs_pct_reported),
                "gross_pnl_usdt": decimal_text(gross_pnl),
                "net_pnl_usdt": decimal_text(net_pnl),
                "gross_return_pct": decimal_text(gross_return_reported),
                "net_return_pct": decimal_text(net_return_reported),
            }
        )
        return row


def simulate_policy(
    events: Sequence[LabelledEvent],
    tape_prices: Sequence[TapePrice],
    policy: Policy,
) -> Tuple[Dict[str, Any], ...]:
    """Simulate every labelled event in input order with one fixed policy."""
    seen = set()
    rows: List[Dict[str, Any]] = []
    for event in events:
        if not isinstance(event, LabelledEvent):
            raise PolicySimError("events must contain LabelledEvent values")
        if event.event_id in seen:
            raise PolicySimError(f"duplicate event_id: {event.event_id}")
        seen.add(event.event_id)
        rows.append(simulate_event(event, tape_prices, policy))
    return tuple(rows)


def _read_jsonl(path: Path, source_name: str) -> Iterable[Tuple[int, Dict[str, Any]]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    raise PolicySimError(
                        f"{source_name}:{line_number} is not JSON"
                    ) from None
                if not isinstance(row, dict):
                    raise PolicySimError(
                        f"{source_name}:{line_number} is not an object"
                    )
                yield line_number, row
    except (OSError, UnicodeDecodeError) as error:
        raise PolicySimError(f"cannot read {source_name}: {error}") from None


def _mapping_timestamp(row: Mapping[str, Any], source: str) -> int:
    candidates: List[int] = []
    for name in (
        "event_time_ms",
        "timestamp_ms",
        "time_ms",
        "open_ms",
        "transact_time",
    ):
        if name in row:
            candidates.append(_timestamp_ms(row[name], f"{source} {name}"))
    for name in ("event_utc", "utc"):
        if name in row:
            candidates.append(_utc_ms(row[name], f"{source} {name}"))
    if not candidates:
        raise PolicySimError(f"{source} has no event timestamp")
    if len(set(candidates)) != 1:
        raise PolicySimError(f"{source} has conflicting timestamps")
    return candidates[0]


def event_from_mapping(row: Mapping[str, Any], source: str) -> LabelledEvent:
    symbol = row.get("symbol")
    label = row.get("label", row.get("event_label"))
    venue_class = row.get("venue_class")
    event_time_ms = _mapping_timestamp(row, source)
    venue = row.get("venue", row.get("source_venue"))
    runner_score_raw = None
    for score_name in (
        "runner_score",
        "signal_score",
        "detector_score",
        "score",
        "absorption_score",
    ):
        if score_name in row:
            runner_score_raw = row[score_name]
            break
    runner_score = (
        None
        if runner_score_raw is None
        else parse_decimal(runner_score_raw, f"{source} runner_score")
    )
    event_id = row.get("event_id")
    if event_id is None and type(symbol) is str and type(label) is str:
        event_id = f"{symbol}:{event_time_ms}:{label}"
    return LabelledEvent(
        event_id=event_id,
        symbol=symbol,
        label=label,
        venue_class=venue_class,
        event_time_ms=event_time_ms,
        venue=venue,
        runner_score=runner_score,
    )


def tape_price_from_mapping(row: Mapping[str, Any], source: str) -> TapePrice:
    return TapePrice(
        symbol=row.get("symbol"),
        venue=row.get("venue"),
        timestamp_ms=_mapping_timestamp(row, source),
        price=parse_decimal(row.get("price"), f"{source} price", positive=True),
    )


def load_events(
    path: Path, *, limit: Optional[int] = None
) -> Tuple[LabelledEvent, ...]:
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise PolicySimError("limit must be a positive integer")
    events: List[LabelledEvent] = []
    for line_number, row in _read_jsonl(Path(path), Path(path).name):
        if row.get("kind") == "summary":
            continue
        events.append(
            event_from_mapping(row, f"{Path(path).name}:{line_number}")
        )
        if limit is not None and len(events) >= limit:
            break
    if not events:
        raise PolicySimError("events JSONL has no labelled events")
    return tuple(events)


def load_tape_prices(path: Path) -> Tuple[TapePrice, ...]:
    prices = tuple(
        tape_price_from_mapping(row, f"{Path(path).name}:{line_number}")
        for line_number, row in _read_jsonl(Path(path), Path(path).name)
    )
    if not prices:
        raise PolicySimError("tape JSONL has no prices")
    return prices


def write_trades(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Atomically emit one deterministic JSON object per event."""
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
        raise PolicySimError(f"cannot write trades output: {error}") from None


def run_policy(
    events_path: Path,
    tape_path: Path,
    output_path: Path,
    policy: Policy,
    *,
    limit: Optional[int] = None,
) -> Tuple[Dict[str, Any], ...]:
    events = load_events(events_path, limit=limit)
    prices = load_tape_prices(tape_path)
    rows = simulate_policy(events, prices, policy)
    write_trades(output_path, rows)
    return rows


def _pair(raw: str, source: str, *, trigger_is_int: bool) -> Tuple[Any, Any]:
    parts = raw.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"{source} must be FRACTION:TRIGGER")
    try:
        fraction = parse_decimal(parts[0], f"{source} fraction", positive=True)
        if trigger_is_int:
            trigger = int(parts[1])
            if str(trigger) != parts[1].strip() or trigger < 0:
                raise ValueError
        else:
            trigger = parse_decimal(
                parts[1], f"{source} gain_pct", positive=True
            )
    except (PolicySimError, ValueError) as error:
        raise argparse.ArgumentTypeError(str(error)) from None
    return fraction, trigger


def _stage_argument(raw: str) -> Tuple[Decimal, int]:
    return _pair(raw, "stage", trigger_is_int=True)


def _partial_argument(raw: str) -> Tuple[Decimal, Decimal]:
    return _pair(raw, "partial", trigger_is_int=False)


def _decimal_argument(raw: str) -> Decimal:
    try:
        return parse_decimal(raw, "policy value")
    except PolicySimError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _positive_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "limit must be a positive integer"
        ) from None
    if value <= 0 or str(value) != raw.strip():
        raise argparse.ArgumentTypeError("limit must be a positive integer")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="simulate an explicit policy over offline labelled events"
    )
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--tape", type=Path, default=DEFAULT_TAPE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument(
        "--limit",
        type=_positive_limit,
        help=(
            "process only the first N events; limited results are "
            "diagnostic only"
        ),
    )
    parser.add_argument("--entry-delay-min", type=int, required=True)
    parser.add_argument(
        "--stage",
        type=_stage_argument,
        action="append",
        required=True,
        metavar="FRACTION:MIN_AFTER_DELAY",
    )
    parser.add_argument("--max-alloc-usdt", type=_decimal_argument, required=True)
    parser.add_argument("--stop-pct", type=_decimal_argument, required=True)
    parser.add_argument(
        "--partial",
        type=_partial_argument,
        action="append",
        default=[],
        metavar="FRACTION:GAIN_PCT",
    )
    parser.add_argument("--final-exit", type=int, required=True)
    parser.add_argument("--spot-taker-fee", type=_decimal_argument, default=Decimal("0.001"))
    parser.add_argument("--perp-taker-fee", type=_decimal_argument, default=Decimal("0.0005"))
    parser.add_argument("--spread-cost", type=_decimal_argument, required=True)
    parser.add_argument(
        "--slippage-conservative", type=_decimal_argument, required=True
    )
    return parser


def _policy_from_args(args: argparse.Namespace) -> Policy:
    return Policy(
        entry_delay_min=args.entry_delay_min,
        stages=tuple(args.stage),
        max_alloc_usdt=args.max_alloc_usdt,
        stop_pct=args.stop_pct,
        partials=tuple(args.partial),
        final_exit=args.final_exit,
        spread_cost=args.spread_cost,
        slippage_conservative=args.slippage_conservative,
        spot_taker_fee=args.spot_taker_fee,
        perp_taker_fee=args.perp_taker_fee,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        policy = _policy_from_args(args)
        rows = run_policy(
            args.events,
            args.tape,
            args.output,
            policy,
            limit=args.limit,
        )
    except PolicySimError as error:
        print(f"POLICY-SIM | REFUSED | {error}", file=sys.stderr)
        return 2
    closed = sum(row["status"] == "CLOSED" for row in rows)
    diagnostic = " | diagnostic_only=true" if args.limit is not None else ""
    print(
        f"POLICY-SIM | events={len(rows)} | closed={closed} | "
        f"unknown={len(rows) - closed} | output={args.output}{diagnostic}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
