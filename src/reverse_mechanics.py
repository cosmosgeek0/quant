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

import hashlib, re
from dataclasses import dataclass, field, replace
from decimal import Decimal, DecimalException, ROUND_DOWN, localcontext
from math import gcd
from typing import Any, Literal, Mapping, Optional, Tuple, Union

import limits, margin_ops
from reverse_rung_intent import (ASSET_RE, BORROW_CLAMP_EXCEEDED, GATE_ORDER,
    QUOTE_NOTIONAL_CAP_EXCEEDED, STEP_OR_MIN_NOTIONAL_INFEASIBLE)


ZERO = Decimal("0"); MAX_QUOTE_CAP = Decimal("30")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,30}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
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

    @property
    def quantity(self) -> Decimal:
        """Exact quantity carried by this plan or action leg."""
        return self.base_qty


@dataclass(frozen=True)
class ReversePlan:
    symbol: str; intent_id: str; client_tag: str; base_qty: Decimal
    borrow_asset: str
    borrow_principal: Decimal; borrowable: Decimal
    interest_rate_hourly: Decimal; quote_cap: Decimal; quote_notional: Decimal
    spot_notional: Decimal; perp_notional: Decimal; common_step: Decimal
    legs: Tuple[ReverseLeg, ...]
    verdict: Literal["READY"] = "READY"
    spot_step: Optional[Decimal] = None

    @property
    def dust(self) -> Decimal:
        return self.spot_step if self.spot_step is not None else self.common_step

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
    executed_qty: Optional[Decimal] = None
    net_base_after_fees: Optional[Decimal] = None


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
    interest_rate_hourly, intent_id, client_tag, caps=limits,
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
            or not _valid_decimal(interest_rate_hourly) \
            or type(intent_id) is not str or ID_RE.fullmatch(intent_id) is None \
            or type(client_tag) is not str or ID_RE.fullmatch(client_tag) is None:
        return _refusal(symbol, BORROW_CLAMP_EXCEEDED,
                        "borrow or claimed-intent facts are unavailable")

    spot_step, spot_min_qty, spot_min, spot_price = (
        _value(spot_meta, "step"), _value(spot_meta, "min_qty"),
        _value(spot_meta, "min_notional"), _value(spot_meta, "price"))
    perp_step, perp_min_qty, perp_min, perp_price = (
        _value(perp_meta, "step"), _value(perp_meta, "min_qty"),
        _value(perp_meta, "min_notional"), _value(perp_meta, "price"))
    if type(symbol) is not str or SYMBOL_RE.fullmatch(symbol) is None \
            or not all(_valid_decimal(value, positive=True) for value in (
                spot_step, spot_min_qty, spot_price,
                perp_step, perp_min_qty, perp_price)) \
            or not all(_valid_decimal(value) for value in (spot_min, perp_min)):
        return _refusal(symbol, STEP_OR_MIN_NOTIONAL_INFEASIBLE,
                        "symbol or venue sizing metadata is unavailable")

    try:
        with localcontext() as context:
            context.prec = 60
            common_step = _common_step(spot_step, perp_step)
            raw_qty = min(quote_cap / spot_price, quote_cap / perp_price,
                          margin_ops.PRINCIPAL_CEILING - common_step,
                          borrowable)
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
    if base_qty <= ZERO or base_qty < spot_min_qty \
            or base_qty < perp_min_qty \
            or spot_notional < spot_min or perp_notional < perp_min:
        return _refusal(symbol, STEP_OR_MIN_NOTIONAL_INFEASIBLE,
                        "no common floored quantity meets both venue minimums")

    quote_notional = max(spot_notional, perp_notional)
    legs = tuple(ReverseLeg(
        name,
        "rmo-" + hashlib.sha256(
            (intent_id + ":" + name).encode("utf-8")).hexdigest()[:24],
        base_qty, name == FUTURES_CLOSE, LIFECYCLE_BY_LEG[name],
    ) for name in LEG_ORDER)
    return ReversePlan(symbol, intent_id, client_tag, base_qty,
        borrow_asset, base_qty,
        borrowable, interest_rate_hourly, quote_cap, quote_notional,
        spot_notional, perp_notional, common_step, legs, spot_step=spot_step)


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
        executed, net_base = value.executed_qty, value.net_base_after_fees
    elif type(value) is str:
        status, raw_fills, executed, net_base = value, (), None, None
    elif isinstance(value, Mapping):
        status, raw_fills = value.get("status", NOT_STARTED), value.get("fills", ())
        executed = _value(value, "executed_qty", "executedQty")
        net_base = _value(value, "net_base_after_fees", "netBaseAfterFees")
    else: raise ValueError("leg outcome is malformed")
    if raw_fills is None:
        raw_fills = ()
    if status not in LEDGER_STATUSES or not isinstance(raw_fills, (list, tuple)):
        raise ValueError("leg status or fills are malformed")
    fills = tuple(_fill_amount(fill) for fill in raw_fills)
    if executed is None and fills:
        executed = sum(fills, ZERO)
    if executed is not None and not _valid_decimal(executed):
        raise ValueError("executed quantity is unavailable")
    if net_base is not None and not _valid_decimal(net_base):
        raise ValueError("net base after fees is unavailable")
    if executed is not None and sum(fills, ZERO) > executed:
        raise ValueError("fills exceed executed quantity")
    if executed is not None and net_base is not None and net_base > executed:
        raise ValueError("net base exceeds executed quantity")
    return LegOutcome(status, fills, executed, net_base)


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


def _floor_quantity(quantity: Decimal, step: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 60
        return (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step


def _action_leg(plan: ReversePlan, name: str, quantity: Decimal) -> ReverseLeg:
    if not _valid_decimal(quantity, positive=True):
        raise ValueError(name + "_QUANTITY_UNKNOWN")
    planned = next(leg for leg in plan.legs if leg.name == name)
    return replace(planned, base_qty=quantity)


def _measurement_problem(ledger: Mapping[str, LegOutcome]) -> Optional[str]:
    for name in LEG_ORDER:
        row = ledger[name]
        if row.status == UNKNOWN:
            return name + "_UNKNOWN"
        if row.status == NOT_STARTED:
            if row.executed_qty not in (None, ZERO) \
                    or row.net_base_after_fees not in (None, ZERO):
                return "LEDGER_TRUTH_MISMATCH"
            continue
        if row.executed_qty is None:
            return name + "_EXECUTED_QTY_UNKNOWN"
        if row.status == DONE_STATUS and row.executed_qty <= ZERO:
            return name + "_DONE_WITHOUT_FILLS"
        if name in (MARGIN_SELL, MARGIN_BUY) \
                and row.executed_qty > ZERO \
                and row.net_base_after_fees is None:
            return name + "_NET_BASE_UNKNOWN"
    return None


def _quantity(ledger: Mapping[str, LegOutcome], name: str) -> Decimal:
    row = ledger[name]
    return ZERO if row.status == NOT_STARTED else row.executed_qty  # type: ignore[return-value]


def _net_base(ledger: Mapping[str, LegOutcome], name: str) -> Decimal:
    row = ledger[name]
    if row.status == NOT_STARTED or row.executed_qty == ZERO:
        return ZERO
    return row.net_base_after_fees  # type: ignore[return-value]


def _ledger_truth_problem(
    plan: ReversePlan, ledger: Mapping[str, LegOutcome], truth: ExchangeTruth,
) -> Optional[str]:
    borrowed = _quantity(ledger, BORROW)
    sold = _quantity(ledger, MARGIN_SELL)
    sold_net = _net_base(ledger, MARGIN_SELL)
    opened = _quantity(ledger, FUTURES_LONG_OPEN)
    closed = _quantity(ledger, FUTURES_CLOSE)
    bought_net = _net_base(ledger, MARGIN_BUY)
    repaid = _quantity(ledger, REPAY)

    if truth.liability > ZERO and (
            ledger[BORROW].status == NOT_STARTED or borrowed <= ZERO):
        return "LEDGER_TRUTH_MISMATCH"
    if borrowed > ZERO and truth.liability == ZERO and repaid == ZERO:
        return "LEDGER_TRUTH_MISMATCH"
    if sold > ZERO and borrowed <= ZERO:
        return "LEDGER_TRUTH_MISMATCH"
    if opened > ZERO and sold_net <= ZERO:
        return "LEDGER_TRUTH_MISMATCH"
    if closed > opened or bought_net > sold_net + plan.dust:
        return "LEDGER_TRUTH_MISMATCH"
    if repaid > ZERO and borrowed <= ZERO:
        return "LEDGER_TRUTH_MISMATCH"
    if truth.futures_position > ZERO and (
            opened <= ZERO or truth.futures_position != opened):
        return "LEDGER_TRUTH_MISMATCH"
    if truth.futures_position == ZERO and opened > closed:
        return "LEDGER_TRUTH_MISMATCH"

    expected_delta = borrowed - sold + bought_net - repaid
    # Base reconciliation uses the Spot venue step. A negative difference of
    # one full step is sale evidence, not flatness; one step is the
    # inclusive reconciliation allowance, while DONE remains strictly inside.
    if abs(truth.base_balance_delta - expected_delta) > plan.dust:
        return "LEDGER_TRUTH_MISMATCH"
    if borrowed == sold == bought_net == repaid == ZERO \
            and truth.base_balance_delta != ZERO:
        return "LEDGER_TRUTH_MISMATCH"
    return None


def _cleanup_action(
    plan: ReversePlan, ledger: Mapping[str, LegOutcome], truth: ExchangeTruth,
) -> ReverseAction:
    names_and_quantities = []
    if truth.futures_position > ZERO:
        names_and_quantities.append((
            FUTURES_CLOSE, _quantity(ledger, FUTURES_LONG_OPEN),
        ))
    buy_remaining = max(
        _net_base(ledger, MARGIN_SELL) - _net_base(ledger, MARGIN_BUY), ZERO,
    )
    buy_quantity = _floor_quantity(buy_remaining, plan.dust)
    if buy_quantity > ZERO:
        names_and_quantities.append((MARGIN_BUY, buy_quantity))
    if truth.liability > ZERO:
        names_and_quantities.append((REPAY, truth.liability))
    if not names_and_quantities:
        return DONE if _is_flat(plan, truth) \
            else FREEZE("RECOVERY_TRUTH_NOT_FLAT")
    try:
        return UNWIND(tuple(
            _action_leg(plan, name, quantity)
            for name, quantity in names_and_quantities
        ))
    except ValueError as error:
        return FREEZE(str(error))


def next_action(plan, ledger_of_completed_legs, exchange_truth) -> ReverseAction:
    """Return the sole next pure action under the frozen failure ladder."""
    if not isinstance(plan, ReversePlan) \
            or not _valid_decimal(plan.dust, positive=True) \
            or not _valid_decimal(plan.common_step, positive=True):
        return FREEZE("PLAN_NOT_READY")
    try:
        ledger = _ledger(ledger_of_completed_legs)
        truth = _truth(exchange_truth)
    except (LookupError, ValueError) as error:
        return FREEZE(str(error))

    problem = _measurement_problem(ledger)
    if problem is not None:
        return FREEZE(problem)

    failures = [name for name in LEG_ORDER if ledger[name].status == FAILED]
    first_open = len(LEG_ORDER)
    if not failures:
        for index, name in enumerate(LEG_ORDER):
            if ledger[name].status == NOT_STARTED:
                first_open = index
                break
            if ledger[name].status != DONE_STATUS:
                return FREEZE("LEDGER_OUT_OF_ORDER")
        if any(ledger[name].status != NOT_STARTED
               for name in LEG_ORDER[first_open:]):
            return FREEZE("LEDGER_OUT_OF_ORDER")
    else:
        first_failure = LEG_ORDER.index(failures[0])
        if any(ledger[name].status != DONE_STATUS
               for name in LEG_ORDER[:first_failure]):
            return FREEZE("LEDGER_OUT_OF_ORDER")

    problem = _ledger_truth_problem(plan, ledger, truth)
    if problem is not None:
        return FREEZE(problem)

    # Exchange position truth outranks a completed close row. While any long
    # stands, cleanup starts with its exact measured standing quantity.
    if truth.futures_position > ZERO:
        return _cleanup_action(plan, ledger, truth)
    if failures:
        return _cleanup_action(plan, ledger, truth)
    if first_open == len(LEG_ORDER):
        return DONE if _is_flat(plan, truth) \
            else FREEZE("TERMINAL_TRUTH_NOT_FLAT")

    name = LEG_ORDER[first_open]
    if name == BORROW:
        quantity = plan.borrow_principal
    elif name == MARGIN_SELL:
        quantity = _floor_quantity(_quantity(ledger, BORROW), plan.dust)
    elif name == FUTURES_LONG_OPEN:
        quantity = _floor_quantity(
            _net_base(ledger, MARGIN_SELL), plan.common_step,
        )
    elif name == FUTURES_CLOSE:
        return FREEZE("LEDGER_TRUTH_MISMATCH")
    elif name == MARGIN_BUY:
        quantity = _floor_quantity(
            max(_net_base(ledger, MARGIN_SELL)
                - _net_base(ledger, MARGIN_BUY), ZERO), plan.dust,
        )
    else:
        quantity = truth.liability
    try:
        return NEXT_LEG(_action_leg(plan, name, quantity))
    except ValueError as error:
        return FREEZE(str(error))
