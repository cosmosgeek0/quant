"""Pure plan and failure ladder for one reverse-mechanics roundtrip.

The risk order cites ``basis_cycle.run_cycle``: reduce-only futures close
precedes cash-side restoration.  State names cite ``reverse_basis_lifecycle``:
BORROW maps to PREFLIGHTED/BORROWING/LIABILITY_CONFIRMED; MARGIN_SELL to
ENTRY_MARGIN_SELL_WORKING; FUTURES_LONG_OPEN to HEDGE_WORKING/OPEN; both exit
legs to EXITING; REPAY to REPAYING/CLOSED; known failures to
RECOVERY_REQUIRED/RECOVERING; and unknown truth to MANUAL_INTERVENTION.
There is no I/O or execution.
"""

from __future__ import annotations

import hashlib, json, re
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, ROUND_DOWN, localcontext
from math import gcd
from typing import Any, Literal, Mapping, Optional, Tuple, Union

import limits, margin_ops
from reverse_rung_intent import (ASSET_RE, BORROW_CLAMP_EXCEEDED, GATE_ORDER,
    QUOTE_NOTIONAL_CAP_EXCEEDED, STEP_OR_MIN_NOTIONAL_INFEASIBLE)


ZERO = Decimal("0"); MAX_QUOTE_CAP = Decimal("8")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,30}$")
BORROW = "BORROW"; MARGIN_SELL = "MARGIN_SELL"
FUTURES_LONG_OPEN = "FUTURES_LONG_OPEN"; FUTURES_CLOSE = "FUTURES_CLOSE"
MARGIN_BUY = "MARGIN_BUY"; REPAY = "REPAY"
LEG_ORDER = (BORROW, MARGIN_SELL, FUTURES_LONG_OPEN, FUTURES_CLOSE,
             MARGIN_BUY, REPAY)
DONE_STATUS = "DONE"; FAILED = "FAILED"; UNKNOWN = "UNKNOWN"
NOT_STARTED = "NOT_STARTED"
LEDGER_STATUSES = frozenset((DONE_STATUS, FAILED, UNKNOWN, NOT_STARTED))

LIFECYCLE_BY_LEG = {
    BORROW: ("PREFLIGHTED", "BORROWING", "LIABILITY_CONFIRMED"),
    MARGIN_SELL: ("ENTRY_MARGIN_SELL_WORKING",),
    FUTURES_LONG_OPEN: ("HEDGE_WORKING", "OPEN"),
    FUTURES_CLOSE: ("EXITING",), MARGIN_BUY: ("EXITING",),
    REPAY: ("REPAYING", "CLOSED")}


@dataclass(frozen=True)
class ReverseLeg:
    name: str; client_id_seed: str; base_qty: Decimal; reduce_only: bool
    lifecycle_states: Tuple[str, ...]


@dataclass(frozen=True)
class ReversePlan:
    symbol: str; intent_id: str; base_qty: Decimal; borrow_asset: str
    borrow_principal: Decimal; borrowable: Decimal
    interest_rate_hourly: Decimal; quote_cap: Decimal; quote_notional: Decimal
    spot_notional: Decimal; perp_notional: Decimal; common_step: Decimal
    legs: Tuple[ReverseLeg, ...]
    verdict: Literal["READY"] = "READY"

    @property
    def dust(self) -> Decimal: return self.common_step

    def __post_init__(self) -> None:
        if self.base_qty != self.borrow_principal:
            raise ValueError("borrow principal must equal the common base quantity")
        if tuple(leg.name for leg in self.legs) != LEG_ORDER:
            raise ValueError("reverse mechanics leg order is fixed")


@dataclass(frozen=True)
class ReversePlanRefusal:
    symbol: str; gate: str; reason: str; detail: str; gate_index: int
    verdict: Literal["REFUSED"] = "REFUSED"


ReversePlanResult = Union[ReversePlan, ReversePlanRefusal]


@dataclass(frozen=True)
class LegOutcome:
    status: str = NOT_STARTED; fills: Tuple[Decimal, ...] = ()


@dataclass(frozen=True)
class ExchangeTruth:
    liability: Optional[Decimal]; base_balance_delta: Optional[Decimal]
    futures_position: Optional[Decimal]


@dataclass(frozen=True)
class NextLeg:
    leg: ReverseLeg
    kind: Literal["NEXT_LEG"] = field(default="NEXT_LEG", init=False)


@dataclass(frozen=True)
class Unwind:
    leg_sequence: Tuple[ReverseLeg, ...]
    kind: Literal["UNWIND"] = field(default="UNWIND", init=False)

    @property
    def sequence(self) -> Tuple[str, ...]:
        return tuple(leg.name for leg in self.leg_sequence)


@dataclass(frozen=True)
class Freeze:
    reason: str
    kind: Literal["FREEZE"] = field(default="FREEZE", init=False)


@dataclass(frozen=True)
class DoneAction:
    kind: Literal["DONE"] = field(default="DONE", init=False)

NEXT_LEG = NextLeg; UNWIND = Unwind; FREEZE = Freeze; DONE = DoneAction()
ReverseAction = Union[NextLeg, Unwind, Freeze, DoneAction]


def _valid_decimal(value: Any, *, positive: bool = False) -> bool:
    return isinstance(value, Decimal) and value.is_finite() \
        and (value > ZERO if positive else value >= ZERO)


def _text(value: Decimal) -> str: return "0" if value == ZERO else format(value.normalize(), "f")


def _value(source: Any, *names: str) -> Any:
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return None


def _refusal(symbol: Any, gate: str, detail: str) -> ReversePlanRefusal:
    name = symbol if type(symbol) is str else ""
    return ReversePlanRefusal(name, gate, gate, detail,
                              GATE_ORDER.index(gate) + 1)


def _common_step(first: Decimal, second: Decimal) -> Decimal:
    left, right = first.as_tuple(), second.as_tuple()
    exponent = min(left.exponent, right.exponent)
    left_units = int("".join(map(str, left.digits))) * 10 ** (left.exponent - exponent)
    right_units = int("".join(map(str, right.digits))) * 10 ** (right.exponent - exponent)
    units = left_units // gcd(left_units, right_units) * right_units
    return Decimal(units).scaleb(exponent)


def plan_reverse_mechanics(
    symbol, quote_cap, spot_meta, perp_meta, borrow_asset, borrowable,
    interest_rate_hourly, caps=limits,
) -> ReversePlanResult:
    """Return one immutable six-leg plan or the first ordered refusal."""
    per_leg = _value(caps, "per_leg_cap_usdt", "PER_LEG_CAP_USDT")
    first_trade = _value(caps, "first_trade_cap_usdt", "FIRST_TRADE_CAP_USDT")
    if not all(_valid_decimal(value, positive=True) for value in (
            quote_cap, per_leg, first_trade)) \
            or quote_cap > min(per_leg, first_trade, MAX_QUOTE_CAP,
                               limits.PER_LEG_CAP_USDT,
                               limits.FIRST_TRADE_CAP_USDT):
        return _refusal(symbol, QUOTE_NOTIONAL_CAP_EXCEEDED,
                        "quote_cap exceeds or cannot prove the effective cap")

    if type(borrow_asset) is not str or ASSET_RE.fullmatch(borrow_asset) is None \
            or not _valid_decimal(borrowable, positive=True) \
            or not _valid_decimal(interest_rate_hourly):
        return _refusal(symbol, BORROW_CLAMP_EXCEEDED,
                        "borrow asset, availability, or hourly rate is unavailable")

    spot_step, spot_min, spot_price = (_value(spot_meta, "step"),
        _value(spot_meta, "min_notional"), _value(spot_meta, "price"))
    perp_step, perp_min, perp_price = (_value(perp_meta, "step"),
        _value(perp_meta, "min_notional"), _value(perp_meta, "price"))
    if type(symbol) is not str or SYMBOL_RE.fullmatch(symbol) is None \
            or not all(_valid_decimal(value, positive=True) for value in (
                spot_step, spot_price, perp_step, perp_price)) \
            or not all(_valid_decimal(value) for value in (spot_min, perp_min)):
        return _refusal(symbol, STEP_OR_MIN_NOTIONAL_INFEASIBLE,
                        "symbol or venue sizing metadata is unavailable")

    try:
        with localcontext() as context:
            context.prec = 60
            common_step = _common_step(spot_step, perp_step)
            raw_qty = min(quote_cap / spot_price, quote_cap / perp_price)
            base_qty = (raw_qty / common_step).to_integral_value(
                rounding=ROUND_DOWN) * common_step
            spot_notional = base_qty * spot_price
            perp_notional = base_qty * perp_price
    except (DecimalException, ZeroDivisionError):
        return _refusal(symbol, STEP_OR_MIN_NOTIONAL_INFEASIBLE,
                        "one common quantity could not be calculated")

    if base_qty > margin_ops.PRINCIPAL_CEILING or base_qty > borrowable:
        return _refusal(symbol, BORROW_CLAMP_EXCEEDED,
                        "the common base quantity exceeds the borrow clamp")
    if base_qty <= ZERO or spot_notional < spot_min or perp_notional < perp_min:
        return _refusal(symbol, STEP_OR_MIN_NOTIONAL_INFEASIBLE,
                        "no common floored quantity meets both venue minimums")

    quote_notional = max(spot_notional, perp_notional)
    material = {
        "schema": "reverse-mechanics-plan-v1", "symbol": symbol,
        "quote_cap": _text(quote_cap), "spot_step": _text(spot_step),
        "spot_min": _text(spot_min), "spot_price": _text(spot_price),
        "perp_step": _text(perp_step), "perp_min": _text(perp_min),
        "perp_price": _text(perp_price), "borrow_asset": borrow_asset,
        "borrowable": _text(borrowable),
        "interest_rate_hourly": _text(interest_rate_hourly),
    }
    digest = hashlib.sha256(json.dumps(
        material, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    intent_id = "rmp-" + digest[:24]
    legs = tuple(ReverseLeg(
        name,
        "rmo-" + hashlib.sha256(
            (intent_id + ":" + name).encode("ascii")).hexdigest()[:24],
        base_qty, name == FUTURES_CLOSE, LIFECYCLE_BY_LEG[name],
    ) for name in LEG_ORDER)
    return ReversePlan(symbol, intent_id, base_qty, borrow_asset, base_qty,
        borrowable, interest_rate_hourly, quote_cap, quote_notional,
        spot_notional, perp_notional, common_step, legs)


def _fill_amount(value: Any) -> Decimal:
    if isinstance(value, Decimal): amount = value
    elif isinstance(value, Mapping):
        amount = _value(value, "executed_qty", "executedQty", "qty",
                        "quantity", "principal", "amount")
    elif isinstance(value, (list, tuple)) and len(value) >= 2 \
            and isinstance(value[1], Decimal):
        amount = value[1]
    else: raise ValueError("fill evidence is malformed")
    if not _valid_decimal(amount):
        raise ValueError("fill quantity is unavailable")
    return amount


def _outcome(value: Any) -> LegOutcome:
    if value is None: return LegOutcome()
    if isinstance(value, LegOutcome):
        status, raw_fills = value.status, value.fills
    elif type(value) is str:
        status, raw_fills = value, ()
    elif isinstance(value, Mapping):
        status, raw_fills = value.get("status", NOT_STARTED), value.get("fills", ())
    else: raise ValueError("leg outcome is malformed")
    if status not in LEDGER_STATUSES or not isinstance(raw_fills, (list, tuple)):
        raise ValueError("leg status or fills are malformed")
    return LegOutcome(status, tuple(_fill_amount(fill) for fill in raw_fills))


def _ledger(value: Any) -> Mapping[str, LegOutcome]:
    if not isinstance(value, Mapping): raise ValueError("completed-leg ledger is not a mapping")
    if set(value) - set(LEG_ORDER):
        raise ValueError("completed-leg ledger names an unsupported leg")
    return {name: _outcome(value.get(name)) for name in LEG_ORDER}


def _truth(value: Any) -> ExchangeTruth:
    truth = value if isinstance(value, ExchangeTruth) else ExchangeTruth(
        _value(value, "liability", "liability_total"),
        _value(value, "base_balance_delta", "base_delta"),
        _value(value, "futures_position", "futures_position_qty"),
    )
    if any(item is None for item in (
            truth.liability, truth.base_balance_delta, truth.futures_position)):
        raise LookupError("EXCHANGE_TRUTH_UNKNOWN")
    if not _valid_decimal(truth.liability) \
            or not isinstance(truth.base_balance_delta, Decimal) \
            or not truth.base_balance_delta.is_finite() \
            or not _valid_decimal(truth.futures_position):
        raise ValueError("EXCHANGE_TRUTH_INVALID")
    return truth


def _is_flat(plan: ReversePlan, truth: ExchangeTruth) -> bool:
    return truth.liability == truth.futures_position == ZERO and abs(truth.base_balance_delta) < plan.dust


def next_action(plan, ledger_of_completed_legs, exchange_truth) -> ReverseAction:
    """Return the sole next pure action under the frozen failure ladder."""
    if not isinstance(plan, ReversePlan):
        return FREEZE("PLAN_NOT_READY")
    try:
        ledger = _ledger(ledger_of_completed_legs)
        truth = _truth(exchange_truth)
    except (LookupError, ValueError) as error:
        return FREEZE(str(error))

    for name in LEG_ORDER:
        row = ledger[name]
        if row.status == UNKNOWN:
            return FREEZE(name + "_UNKNOWN")
        if row.status == DONE_STATUS and sum(row.fills, ZERO) <= ZERO:
            return FREEZE(name + "_DONE_WITHOUT_FILLS")

    failures = [name for name in LEG_ORDER if ledger[name].status == FAILED]
    if not failures:
        first_open = len(LEG_ORDER)
        for index, name in enumerate(LEG_ORDER):
            if ledger[name].status == NOT_STARTED:
                first_open = index
                break
            if ledger[name].status != DONE_STATUS:
                return FREEZE("LEDGER_OUT_OF_ORDER")
        if any(ledger[name].status != NOT_STARTED
               for name in LEG_ORDER[first_open:]):
            return FREEZE("LEDGER_OUT_OF_ORDER")
        if first_open < len(LEG_ORDER):
            return NEXT_LEG(plan.legs[first_open])
        return DONE if _is_flat(plan, truth) \
            else FREEZE("TERMINAL_TRUTH_NOT_FLAT")

    first_failure = LEG_ORDER.index(failures[0])
    if any(ledger[name].status != DONE_STATUS
           for name in LEG_ORDER[:first_failure]):
        return FREEZE("LEDGER_OUT_OF_ORDER")

    filled = lambda name: sum(ledger[name].fills, ZERO) > ZERO
    borrow_stands = first_failure > 0 or filled(BORROW) \
        or truth.liability > ZERO
    sell_filled = ledger[MARGIN_SELL].status == DONE_STATUS \
        or filled(MARGIN_SELL) or truth.base_balance_delta < -plan.dust
    long_filled = ledger[FUTURES_LONG_OPEN].status == DONE_STATUS \
        or filled(FUTURES_LONG_OPEN) or truth.futures_position > ZERO

    if long_filled and ledger[FUTURES_CLOSE].status != DONE_STATUS \
            and ledger[MARGIN_BUY].status == DONE_STATUS:
        return FREEZE("REDUCE_ONLY_CLOSE_MUST_PRECEDE_MARGIN_BUY")
    if borrow_stands and ledger[REPAY].status == DONE_STATUS \
            and truth.liability != ZERO:
        return FREEZE("REPAY_TRUTH_MISMATCH")

    names = [name for name, needed in (
        (FUTURES_CLOSE, long_filled), (MARGIN_BUY, sell_filled),
        (REPAY, borrow_stands)) if needed and ledger[name].status != DONE_STATUS]
    if names:
        by_name = {leg.name: leg for leg in plan.legs}
        return UNWIND(tuple(by_name[name] for name in names))
    return DONE if _is_flat(plan, truth) \
        else FREEZE("RECOVERY_TRUTH_NOT_FLAT")
