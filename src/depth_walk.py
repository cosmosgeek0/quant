"""Pure two-venue depth walks for executable basis capture.

BASIS-LOCKIN-SPEC rows 089-099 name fresh depth and sides; row 107 keeps
read-to-placement slippage unknown; row 109 calls for both walks.  Raw books
retain ``{"bids": [[price, qty], ...], "asks": ...}``; cells must be finite
positive Decimal text.  No float or top-of-book substitute is accepted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Dict, Literal, Optional, Tuple, Union

from limits import FIRST_TRADE_CAP_USDT

MAX_TARGET_QUOTE_NOTIONAL = FIRST_TRADE_CAP_USDT; PRECISION = 50
HUNDRED = Decimal("100")

TradeSide = Literal["BUY", "SELL"]; Direction = Literal["NORMAL", "REVERSE"]
Transition = Literal["ENTRY", "EXIT"]
RefusalReason = Literal[
    "SIDE_INVALID", "DIRECTION_INVALID", "TRANSITION_INVALID",
    "TARGET_NOTIONAL_INVALID", "TARGET_NOTIONAL_LIMIT_EXCEEDED",
    "BOOK_INVALID", "LEVELS_INVALID", "LEVEL_VALUE_INVALID", "LEVELS_UNSORTED",
    "BOOK_CROSSED_OR_LOCKED", "DEPTH_INSUFFICIENT", "ARITHMETIC_UNAVAILABLE",
]
ParsedLevels = Tuple[Tuple[Decimal, Decimal], ...]


@dataclass(frozen=True)
class DepthWalk:
    side: TradeSide
    target_quote_notional: Decimal
    fillable_quantity: Decimal
    vwap: Decimal
    top_price: Decimal
    worst_price: Decimal
    slippage_pct: Decimal
    levels_touched: int
    verdict: Literal["READY"] = "READY"


@dataclass(frozen=True)
class DepthWalkRefusal:
    side: str
    reason: RefusalReason
    level_index: Optional[int] = None
    verdict: Literal["REFUSED"] = "REFUSED"


DepthWalkResult = Union[DepthWalk, DepthWalkRefusal]


@dataclass(frozen=True)
class LockinDepthWalk:
    direction: Direction
    transition: Transition
    spot_walk: DepthWalk
    perp_walk: DepthWalk
    executable_capture_pct: Decimal
    verdict: Literal["READY"] = "READY"


@dataclass(frozen=True)
class LockinDepthRefusal:
    direction: str
    transition: str
    reason: RefusalReason
    leg: Optional[Literal["SPOT", "PERP"]] = None
    level_index: Optional[int] = None
    verdict: Literal["REFUSED"] = "REFUSED"


LockinDepthResult = Union[LockinDepthWalk, LockinDepthRefusal]


def _decimal_text(raw: Any) -> Optional[Decimal]:
    if type(raw) is not str or not raw:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() and value > 0 else None


def _target(raw: Any) -> Tuple[Optional[Decimal], Optional[RefusalReason]]:
    if isinstance(raw, Decimal):
        value = raw
    elif type(raw) is str and raw:
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError):
            return None, "TARGET_NOTIONAL_INVALID"
    else:
        return None, "TARGET_NOTIONAL_INVALID"
    if not value.is_finite() or value <= 0:
        return None, "TARGET_NOTIONAL_INVALID"
    if value > MAX_TARGET_QUOTE_NOTIONAL:
        return None, "TARGET_NOTIONAL_LIMIT_EXCEEDED"
    return value, None


def _levels(raw: Any, side: TradeSide):
    if not isinstance(raw, list):
        return None, "LEVELS_INVALID", None
    parsed = []
    previous = None
    for index, row in enumerate(raw):
        if not isinstance(row, list) or len(row) != 2:
            return None, "LEVELS_INVALID", index
        price, quantity = _decimal_text(row[0]), _decimal_text(row[1])
        if price is None or quantity is None:
            return None, "LEVEL_VALUE_INVALID", index
        if previous is not None and (
            (side == "BUY" and price < previous)
            or (side == "SELL" and price > previous)
        ):
            return None, "LEVELS_UNSORTED", index
        parsed.append((price, quantity))
        previous = price
    return tuple(parsed), None, None


def _walk_rows(
    rows: ParsedLevels, side: TradeSide, target: Decimal
) -> DepthWalkResult:
    if not rows:
        return DepthWalkRefusal(side, "DEPTH_INSUFFICIENT")
    try:
        with localcontext() as context:
            context.prec = PRECISION
            remaining = target
            filled_quote = Decimal(0)
            filled_quantity = Decimal(0)
            touched = 0
            for price, available_quantity in rows:
                if remaining <= 0:
                    break
                level_quote = price * available_quantity
                take_quote = min(remaining, level_quote)
                filled_quote += take_quote
                filled_quantity += take_quote / price
                remaining -= take_quote
                touched += 1
            if remaining > 0 or filled_quantity <= 0:
                return DepthWalkRefusal(side, "DEPTH_INSUFFICIENT")
            top = rows[0][0]
            vwap = filled_quote / filled_quantity
            slippage = abs(vwap - top) / top * HUNDRED
            return DepthWalk(
                side, target, filled_quantity, vwap, top,
                rows[touched - 1][0], slippage, touched,
            )
    except ArithmeticError:
        return DepthWalkRefusal(side, "ARITHMETIC_UNAVAILABLE")


def walk_depth(
    levels: Any, side: Any, target_quote_notional: Any
) -> DepthWalkResult:
    if side not in ("BUY", "SELL"):
        return DepthWalkRefusal(
            side if type(side) is str else "", "SIDE_INVALID")
    target, target_reason = _target(target_quote_notional)
    if target_reason is not None or target is None:
        return DepthWalkRefusal(
            side, target_reason or "TARGET_NOTIONAL_INVALID")
    rows, reason, index = _levels(levels, side)
    if reason is not None or rows is None:
        return DepthWalkRefusal(side, reason or "LEVELS_INVALID", index)
    return _walk_rows(rows, side, target)


def _book(raw: Any):
    if not isinstance(raw, Mapping):
        return None, "BOOK_INVALID", None
    parsed: Dict[str, ParsedLevels] = {}
    for name, side in (("bids", "SELL"), ("asks", "BUY")):
        rows, reason, index = _levels(raw.get(name), side)
        if reason is not None or rows is None:
            return None, reason or "LEVELS_INVALID", index
        if not rows:
            return None, "DEPTH_INSUFFICIENT", None
        parsed[name] = rows
    if parsed["bids"][0][0] >= parsed["asks"][0][0]:
        return None, "BOOK_CROSSED_OR_LOCKED", None
    return parsed, None, None


def walk_lockin(
    direction: Any,
    transition: Any,
    spot_depth: Any,
    perp_depth: Any,
    target_quote_notional: Any,
) -> LockinDepthResult:
    if direction not in ("NORMAL", "REVERSE"):
        return LockinDepthRefusal(
            direction if type(direction) is str else "",
            transition if type(transition) is str else "",
            "DIRECTION_INVALID",
        )
    if transition not in ("ENTRY", "EXIT"):
        return LockinDepthRefusal(
            direction, transition if type(transition) is str else "",
            "TRANSITION_INVALID")
    target, target_reason = _target(target_quote_notional)
    if target_reason is not None or target is None:
        return LockinDepthRefusal(
            direction, transition,
            target_reason or "TARGET_NOTIONAL_INVALID")
    spot_book, reason, index = _book(spot_depth)
    if reason is not None or spot_book is None:
        return LockinDepthRefusal(
            direction, transition, reason or "BOOK_INVALID", "SPOT", index)
    perp_book, reason, index = _book(perp_depth)
    if reason is not None or perp_book is None:
        return LockinDepthRefusal(
            direction, transition, reason or "BOOK_INVALID", "PERP", index)

    spot_side, perp_side = {
        ("NORMAL", "ENTRY"): ("BUY", "SELL"), ("NORMAL", "EXIT"): ("SELL", "BUY"),
        ("REVERSE", "ENTRY"): ("SELL", "BUY"), ("REVERSE", "EXIT"): ("BUY", "SELL"),
    }[(direction, transition)]
    spot_walk = _walk_rows(
        spot_book["asks" if spot_side == "BUY" else "bids"],
        spot_side, target,
    )
    if isinstance(spot_walk, DepthWalkRefusal):
        return LockinDepthRefusal(
            direction, transition, spot_walk.reason,
            "SPOT", spot_walk.level_index)
    perp_walk = _walk_rows(
        perp_book["asks" if perp_side == "BUY" else "bids"],
        perp_side, target,
    )
    if isinstance(perp_walk, DepthWalkRefusal):
        return LockinDepthRefusal(
            direction, transition, perp_walk.reason,
            "PERP", perp_walk.level_index)
    try:
        with localcontext() as context:
            context.prec = PRECISION
            if direction == "NORMAL":
                base, other = spot_walk.vwap, perp_walk.vwap
            else:
                base, other = perp_walk.vwap, spot_walk.vwap
            capture = (other - base) / base * HUNDRED
    except ArithmeticError:
        return LockinDepthRefusal(
            direction, transition, "ARITHMETIC_UNAVAILABLE")
    return LockinDepthWalk(direction, transition, spot_walk, perp_walk, capture)
