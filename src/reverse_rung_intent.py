"""Pure read-only REVERSE rung sizing and economics.

Shadow implementation of BASIS-LOCKIN-SPEC rows 004, 089-095, 098, 108, and
119-122.  It sizes the Spot-bid short and Perp-ask long and returns data only;
there is no clock, network, signing, persistence, job, order, or position API.

BF2 ``exit_gap_pct`` seals this pair with the NORMAL unwind denominator.
Reverse capture uses the long Perp entry denominator, while ``DirectionalGap``
keeps the caller's intended direction explicit.  Optional depth and funding
inputs replace the stamped top-of-book and kernel-default economics sources.

The fixed gate order is borrow, fresh executable gap, reverse direction,
ladder headroom, quote cap, borrow clamp, step/minimum feasibility, depth, then
net edge.  Depth therefore cannot hide an earlier sizing refusal.
"""

from __future__ import annotations

import hashlib, json, re
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, localcontext
from typing import Any, Literal, Mapping, Optional, Union

import basis_kernel, basis_ladder, depth_walk, executable_gap, funding_ruler
import limits, margin_ops


(BORROW_POWER_REQUIRED, GAP_NOT_EXECUTABLE_OR_STALE,
 GAP_DIRECTION_NOT_REVERSE, LADDER_HEADROOM_UNAVAILABLE,
 QUOTE_NOTIONAL_CAP_EXCEEDED, BORROW_CLAMP_EXCEEDED,
 STEP_OR_MIN_NOTIONAL_INFEASIBLE, DEPTH_INSUFFICIENT_OR_INVALID,
 NET_EDGE_NOT_POSITIVE) = GATE_ORDER = (
    "BORROW_POWER_REQUIRED",
    "GAP_NOT_EXECUTABLE_OR_STALE",
    "GAP_DIRECTION_NOT_REVERSE",
    "LADDER_HEADROOM_UNAVAILABLE",
    "QUOTE_NOTIONAL_CAP_EXCEEDED",
    "BORROW_CLAMP_EXCEEDED",
    "STEP_OR_MIN_NOTIONAL_INFEASIBLE",
    "DEPTH_INSUFFICIENT_OR_INVALID",
    "NET_EDGE_NOT_POSITIVE",
)

ZERO = Decimal("0"); MINUTE_MS = Decimal("60000")
HOUR_MS = Decimal("3600000"); PERCENT = Decimal("100")
ASSET_RE = re.compile(r"^[A-Z0-9]{1,20}$")
TOP_OF_BOOK = "top_of_book"; DEPTH_WALK = "depth_walk"
KERNEL_DEFAULT_HOLD = "kernel_default_hold"
FUNDING_RULER_WORST_CASE = "funding_ruler_worst_case"


@dataclass(frozen=True)
class DirectionalGap:
    """One BF2 executable result plus the caller-selected basis direction."""
    result: executable_gap.ExecutableGapResult; direction: str


@dataclass(frozen=True)
class LadderSnapshot:
    """The landed ladder state together with its owner-configured schedule."""
    state: basis_ladder.LadderState; config: basis_ladder.LadderConfig


@dataclass(frozen=True)
class BorrowFacts:
    """Fresh Margin capability facts; rates are fractions per hour."""
    asset: Optional[str]; borrowable_amount: Optional[Decimal]
    hourly_interest_rate: Optional[Decimal]
    borrow_power: bool = False


@dataclass(frozen=True)
class ReverseMarketMetadata:
    """Fresh BF3 stream fields needed for the reverse entry pair."""
    spot_bid: Optional[Decimal]; spot_step: Optional[Decimal]
    spot_min_notional: Optional[Decimal]; perp_ask: Optional[Decimal]
    perp_step: Optional[Decimal]; perp_min_notional: Optional[Decimal]
    funding_rate: Optional[Decimal]; next_funding_ms: Optional[int]
    funding_period_ms: Optional[int]


@dataclass(frozen=True)
class ReverseRungCaps:
    """Explicit copies of the three relevant ``limits`` authorities."""
    per_leg_cap_usdt: Optional[Decimal] = limits.PER_LEG_CAP_USDT
    first_trade_cap_usdt: Optional[Decimal] = limits.FIRST_TRADE_CAP_USDT
    roundtrip_fee_pct: Optional[Decimal] = limits.MEASURED_ROUNDTRIP_FEE_PCT


@dataclass(frozen=True)
class ReverseDepthInput:
    """Both raw venue books in the shape accepted by ``walk_lockin``."""
    spot_depth: Mapping[str, Any]; perp_depth: Mapping[str, Any]


@dataclass(frozen=True)
class ReverseFundingInput:
    """Per-symbol funding bounds and the intended nonnegative hold window."""
    info: funding_ruler.FundingInfo
    expected_hold_minutes: Decimal


@dataclass(frozen=True)
class ReverseRungIntent:
    symbol: str; ordinal: int; borrow_asset: str
    borrow_qty: Decimal; spot_short_qty: Decimal; perp_long_qty: Decimal
    quote_notional: Decimal; expected_capture_pct: Decimal
    funding_and_interest_drag_pct: Decimal
    intent_id: str
    sealed_inputs_digest: str
    capture_source: str = TOP_OF_BOOK
    drag_source: str = KERNEL_DEFAULT_HOLD
    settlements_crossed: Optional[int] = None


@dataclass(frozen=True)
class ReverseRungRefusal:
    symbol: str; gate: str; reason: str; detail: str; gate_index: int
    verdict: Literal["REFUSED"] = "REFUSED"
    capture_source: str = TOP_OF_BOOK
    drag_source: str = KERNEL_DEFAULT_HOLD
    settlements_crossed: Optional[int] = None


ReverseRungResult = Union[ReverseRungIntent, ReverseRungRefusal]


def _finite_decimal(
    value: Any, *, positive: bool = False, signed: bool = False
) -> bool:
    if not isinstance(value, Decimal) or not value.is_finite(): return False
    if signed: return True
    return value > ZERO if positive else value >= ZERO


def _text(value: Decimal) -> str:
    if value == ZERO: return "0"
    return format(value.normalize(), "f")

def _optional_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else _text(value)


def _refusal(
    symbol: Any, gate: str, reason: str, capture_source: str = TOP_OF_BOOK,
    drag_source: str = KERNEL_DEFAULT_HOLD,
    settlements_crossed: Optional[int] = None,
) -> ReverseRungRefusal:
    name = symbol if type(symbol) is str else ""
    return ReverseRungRefusal(
        name, gate, gate, reason, GATE_ORDER.index(gate) + 1,
        capture_source=capture_source, drag_source=drag_source,
        settlements_crossed=settlements_crossed)


def _floor(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _sealed_material(
    gap: DirectionalGap,
    ladder: LadderSnapshot,
    borrow: BorrowFacts,
    market: ReverseMarketMetadata,
    caps: ReverseRungCaps,
    hold_ms: Decimal,
    expected_capture_pct: Decimal,
    capture_source: str,
    drag_source: str,
    settlements_crossed: int,
    depth: Optional[ReverseDepthInput],
    funding: Optional[ReverseFundingInput],
) -> str:
    reading = gap.result
    state, config = ladder.state, ladder.config
    material = {
        "schema": "reverse-rung-intent-v2",
        "gap": {
            "direction": gap.direction, "symbol": reading.symbol,
            "now_ms": reading.now_ms, "verdict": reading.verdict,
            "entry_gap_pct": _text(reading.entry_gap_pct), "exit_gap_pct": _text(reading.exit_gap_pct),
            "spot_age_ms": reading.spot_age_ms, "perp_age_ms": reading.perp_age_ms,
        },
        "ladder": {
            "direction": state.direction,
            "rungs": [[_text(row.gap_at_entry), _text(row.size)]
                      for row in state.rungs],
            "schedule": [[_text(row.gap_threshold), _text(row.rung_size)]
                         for row in config.schedule],
            "max_rungs": config.max_rungs, "max_total_notional": _text(config.max_total_notional),
            "min_notional": _text(config.min_notional), "convergence_gap": _text(config.convergence_gap),
            "max_unwind_notional_per_tick": _optional_text(config.max_unwind_notional_per_tick),
        },
        "borrow": {
            "asset": borrow.asset, "borrow_power": borrow.borrow_power,
            "borrowable_amount": _text(borrow.borrowable_amount), "hourly_interest_rate": _text(borrow.hourly_interest_rate),
        },
        "market": {
            "spot_bid": _text(market.spot_bid), "spot_step": _text(market.spot_step),
            "spot_min_notional": _text(market.spot_min_notional),
            "perp_ask": _text(market.perp_ask), "perp_step": _text(market.perp_step),
            "perp_min_notional": _text(market.perp_min_notional),
            "funding_rate": _text(market.funding_rate),
            "next_funding_ms": market.next_funding_ms, "funding_period_ms": market.funding_period_ms,
        },
        "caps": {
            "per_leg_cap_usdt": _text(caps.per_leg_cap_usdt),
            "first_trade_cap_usdt": _text(caps.first_trade_cap_usdt),
            "roundtrip_fee_pct": _text(caps.roundtrip_fee_pct),
            "margin_principal_ceiling": _text(margin_ops.PRINCIPAL_CEILING),
            "expected_hold_ms": _text(hold_ms),
            "hard_per_leg_cap": _text(limits.PER_LEG_CAP_USDT),
            "hard_first_trade_cap": _text(limits.FIRST_TRADE_CAP_USDT),
            "hard_roundtrip_fee_pct": _text(limits.MEASURED_ROUNDTRIP_FEE_PCT),
        },
        "economics": {
            "expected_capture_pct": _text(expected_capture_pct),
            "capture_source": capture_source, "drag_source": drag_source,
            "settlements_crossed": settlements_crossed,
            "depth": None if depth is None else {
                "spot_depth": depth.spot_depth,
                "perp_depth": depth.perp_depth,
            },
            "funding": None if funding is None else {
                "cap": _optional_text(funding.info.cap),
                "floor": _optional_text(funding.info.floor),
                "interval_hours": funding.info.interval_hours,
                "expected_hold_minutes": _text(
                    funding.expected_hold_minutes),
            },
        },
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def build_reverse_rung_intent(
    gap: DirectionalGap,
    ladder: LadderSnapshot,
    borrow: BorrowFacts,
    market: ReverseMarketMetadata,
    caps: ReverseRungCaps,
    depth: Optional[ReverseDepthInput] = None,
    funding: Optional[ReverseFundingInput] = None,
) -> ReverseRungResult:
    """Return one immutable shadow intent or the first ordered refusal.

    ``borrow_power`` defaults CLOSED.  Only the owner's explicit word may set
    that capability in configuration; market data or this code never opens it.
    All quantities, prices, rates, percentages, and caps must be finite
    ``Decimal`` values.  ``None`` is unknown and reaches its ordered refusal.
    Supplying ``depth`` or ``funding`` selects the corresponding stamped source;
    absence preserves the original top-of-book and kernel-default paths.
    """
    symbol = getattr(getattr(gap, "result", None), "symbol", "")
    capture_source = DEPTH_WALK if depth is not None else TOP_OF_BOOK
    drag_source = (FUNDING_RULER_WORST_CASE if funding is not None
                   else KERNEL_DEFAULT_HOLD)
    settlements_crossed: Optional[int] = None

    def refuse(gate: str, detail: str) -> ReverseRungRefusal:
        return _refusal(symbol, gate, detail, capture_source, drag_source,
                        settlements_crossed)

    if not isinstance(borrow, BorrowFacts) or borrow.borrow_power is not True:
        return refuse(BORROW_POWER_REQUIRED,
                      "borrow_power is not explicitly true")

    reading = getattr(gap, "result", None)
    if not isinstance(gap, DirectionalGap) \
            or not isinstance(reading, executable_gap.ExecutableGapReading) \
            or getattr(reading, "verdict", None) != "READY" \
            or not _finite_decimal(reading.entry_gap_pct, signed=True) \
            or not _finite_decimal(reading.exit_gap_pct, signed=True) \
            or isinstance(reading.now_ms, bool) \
            or not isinstance(reading.now_ms, int) \
            or any(isinstance(age, bool) or not isinstance(age, int)
                   or age < 0 or age > executable_gap.DEFAULT_MAX_QUOTE_AGE_MS
                   for age in (reading.spot_age_ms, reading.perp_age_ms)):
        return refuse(GAP_NOT_EXECUTABLE_OR_STALE,
                      "BF2 did not provide one fresh executable reading")

    if not isinstance(market, ReverseMarketMetadata) \
            or not _finite_decimal(market.spot_bid, positive=True) \
            or not _finite_decimal(market.perp_ask, positive=True) \
            or basis_kernel.executable_return_pct(
                market.perp_ask, market.spot_bid) != reading.exit_gap_pct:
        return refuse(GAP_NOT_EXECUTABLE_OR_STALE,
                      "fresh reverse prices do not seal to the BF2 gap")
    top_capture = basis_kernel.executable_return_pct(
        market.spot_bid, market.perp_ask)
    if top_capture is None:
        return refuse(GAP_NOT_EXECUTABLE_OR_STALE,
                      "reverse capture is unavailable from the sealed pair")

    if gap.direction != basis_ladder.REVERSE:
        return refuse(GAP_DIRECTION_NOT_REVERSE,
                      "the selected executable gap is not reverse")

    if not isinstance(ladder, LadderSnapshot) \
            or not isinstance(ladder.state, basis_ladder.LadderState) \
            or not isinstance(ladder.config, basis_ladder.LadderConfig) \
            or ladder.state.direction != basis_ladder.REVERSE:
        return refuse(LADDER_HEADROOM_UNAVAILABLE,
                      "reverse ladder state or configuration is unavailable")
    state, config = ladder.state, ladder.config
    index = len(state.rungs)
    if index >= config.max_rungs or index >= len(config.schedule) \
            or state.total_notional > config.max_total_notional:
        return refuse(LADDER_HEADROOM_UNAVAILABLE,
                      "the ladder has no next rung")
    level = config.schedule[index]
    remaining = config.max_total_notional - state.total_notional
    quote_notional = min(level.rung_size, remaining)
    if top_capture < level.gap_threshold or remaining <= ZERO \
            or quote_notional < config.min_notional:
        return refuse(LADDER_HEADROOM_UNAVAILABLE,
                      "the next rung is not eligible within headroom")
    ordinal = index + 1

    if not isinstance(caps, ReverseRungCaps) \
            or not _finite_decimal(caps.per_leg_cap_usdt, positive=True) \
            or not _finite_decimal(caps.first_trade_cap_usdt, positive=True) \
            or quote_notional > min(
                caps.per_leg_cap_usdt,
                caps.first_trade_cap_usdt,
                limits.PER_LEG_CAP_USDT,
                limits.FIRST_TRADE_CAP_USDT,
            ):
        return refuse(QUOTE_NOTIONAL_CAP_EXCEEDED,
                      "the rung exceeds the effective limits cap")

    if quote_notional > margin_ops.PRINCIPAL_CEILING \
            or type(borrow.asset) is not str \
            or ASSET_RE.fullmatch(borrow.asset) is None \
            or not _finite_decimal(borrow.borrowable_amount, positive=True):
        return refuse(BORROW_CLAMP_EXCEEDED,
                      "the borrow facts do not fit the 3 USDT clamp")

    if not all((
        _finite_decimal(market.spot_step, positive=True),
        _finite_decimal(market.perp_step, positive=True),
        _finite_decimal(market.spot_min_notional),
        _finite_decimal(market.perp_min_notional),
    )):
        return refuse(STEP_OR_MIN_NOTIONAL_INFEASIBLE,
                      "one venue sizing field is unknown or invalid")
    with localcontext() as context:
        context.prec = basis_kernel.PRECISION
        spot_qty = _floor(quote_notional / market.spot_bid, market.spot_step)
        perp_qty = _floor(quote_notional / market.perp_ask, market.perp_step)
        spot_notional = spot_qty * market.spot_bid
        perp_notional = perp_qty * market.perp_ask
    if borrow.borrowable_amount < spot_qty:
        return refuse(BORROW_CLAMP_EXCEEDED,
                      "borrowable amount is below the floored Spot short")
    if spot_qty <= ZERO or perp_qty <= ZERO \
            or spot_notional < market.spot_min_notional \
            or perp_notional < market.perp_min_notional:
        return refuse(STEP_OR_MIN_NOTIONAL_INFEASIBLE,
                      "floored quantity misses a venue minimum")

    expected_capture = top_capture
    if depth is not None:
        if not isinstance(depth, ReverseDepthInput):
            return refuse(DEPTH_INSUFFICIENT_OR_INVALID,
                          "the typed reverse depth input is unavailable")
        walked = depth_walk.walk_lockin(
            "REVERSE", "ENTRY", depth.spot_depth, depth.perp_depth,
            quote_notional)
        if isinstance(walked, depth_walk.LockinDepthRefusal):
            place = walked.leg or "PAIR"
            return refuse(DEPTH_INSUFFICIENT_OR_INVALID,
                          f"{place} depth walk refused: {walked.reason}")
        expected_capture = walked.executable_capture_pct
        if not _finite_decimal(expected_capture, signed=True):
            return refuse(DEPTH_INSUFFICIENT_OR_INVALID,
                          "depth capture is unavailable")

    if not _finite_decimal(borrow.hourly_interest_rate) \
            or not _finite_decimal(caps.roundtrip_fee_pct):
        return refuse(NET_EDGE_NOT_POSITIVE,
                      "an economics input is unknown or invalid")
    if funding is None:
        if not _finite_decimal(market.funding_rate) \
                or isinstance(market.next_funding_ms, bool) \
                or not isinstance(market.next_funding_ms, int) \
                or isinstance(market.funding_period_ms, bool) \
                or not isinstance(market.funding_period_ms, int) \
                or market.funding_period_ms <= 0:
            return refuse(NET_EDGE_NOT_POSITIVE,
                          "an economics input is unknown or invalid")
        hold_ms = Decimal(basis_kernel.DEFAULT_CONFIG.default_hold_ms)
        ms_to_next = Decimal(market.next_funding_ms - reading.now_ms)
        if ms_to_next > hold_ms:
            settlements_crossed = 0
        elif ms_to_next > ZERO:
            settlements_crossed = int(
                Decimal(1) + (hold_ms - ms_to_next)
                // Decimal(market.funding_period_ms))
        carry_pct = basis_kernel.funding_carry_pct(
            hold_ms, ms_to_next, Decimal(market.funding_period_ms),
            (market.funding_rate,), basis_kernel.DIRECTION_REVERSE)
        if carry_pct is None:
            return refuse(NET_EDGE_NOT_POSITIVE,
                          "kernel funding carry is unavailable")
        funding_drag_pct = max(ZERO, -carry_pct)
    else:
        if not isinstance(funding, ReverseFundingInput):
            return refuse(NET_EDGE_NOT_POSITIVE,
                          "the typed funding input is unavailable")
        rate_text = (_text(market.funding_rate)
                     if _finite_decimal(market.funding_rate, signed=True)
                     else None)
        next_text = (str(market.next_funding_ms)
                     if type(market.next_funding_ms) is int else None)
        ruled = funding_ruler.rule_funding(
            rate_text, next_text, funding.info, reading.now_ms,
            funding.expected_hold_minutes, funding_ruler.REVERSE)
        if isinstance(ruled, funding_ruler.FundingRulerRefusal):
            return refuse(NET_EDGE_NOT_POSITIVE,
                          f"funding ruler refused: {ruled.reason}")
        hold_ms = funding.expected_hold_minutes * MINUTE_MS
        settlements_crossed = ruled.settlements_crossed
        funding_drag_pct = Decimal(ruled.worst_case_drag_pct)
        carry_pct = -funding_drag_pct
    with localcontext() as context:
        context.prec = basis_kernel.PRECISION
        interest_pct = (borrow.hourly_interest_rate * hold_ms
                        / HOUR_MS * PERCENT)
    edge = basis_kernel.net_edge(
        expected_capture,
        max(caps.roundtrip_fee_pct, limits.MEASURED_ROUNDTRIP_FEE_PCT),
        ZERO,
        carry_pct,
        interest_pct,
        intended_exit_gap_pct=ZERO,
        exit_slippage_pct=ZERO,
        safety_buffer_pct=ZERO,
    )
    if edge.net_pct is None or edge.net_pct <= ZERO:
        return refuse(NET_EDGE_NOT_POSITIVE,
                      "fees, interest, and funding leave no positive edge")

    if settlements_crossed is None:
        return refuse(NET_EDGE_NOT_POSITIVE,
                      "funding settlement count is unavailable")
    digest = _sealed_material(
        gap, ladder, borrow, market, caps, hold_ms, expected_capture,
        capture_source, drag_source, settlements_crossed, depth, funding)
    drag = funding_drag_pct + interest_pct
    return ReverseRungIntent(
        symbol=reading.symbol,
        ordinal=ordinal,
        borrow_asset=borrow.asset,
        borrow_qty=spot_qty,
        spot_short_qty=spot_qty,
        perp_long_qty=perp_qty,
        quote_notional=quote_notional,
        expected_capture_pct=expected_capture,
        funding_and_interest_drag_pct=drag,
        intent_id="rri-" + digest[:24],
        sealed_inputs_digest=digest,
        capture_source=capture_source,
        drag_source=drag_source,
        settlements_crossed=settlements_crossed,
    )


__all__ = [
    "DirectionalGap", "LadderSnapshot", "BorrowFacts",
    "ReverseDepthInput", "ReverseFundingInput",
    "ReverseMarketMetadata", "ReverseRungCaps", "ReverseRungIntent",
    "ReverseRungRefusal", "ReverseRungResult", "GATE_ORDER",
    "DEPTH_INSUFFICIENT_OR_INVALID", "TOP_OF_BOOK", "DEPTH_WALK",
    "KERNEL_DEFAULT_HOLD", "FUNDING_RULER_WORST_CASE",
    "build_reverse_rung_intent",
]
