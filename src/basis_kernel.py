#!/usr/bin/env python3
"""basis_kernel.py — pure, stateless economics kernel for the NORMAL basis.

Port of the old engine's math + funding areas per the mined behavioral spec
(tests/fixtures/old_engine_spec.json).  Every formula here is a KEEP/ADAPT
rule from that spec; REJECT rules (old toy sizing default) are not ported —
intended notional is always an input.  The kernel is fully offline: no
network, no clock, no I/O, no state.  Callers (scanner/lifecycle areas) own
windows, persistence counters and ingestion; this module only prices what it
is handed.

UNIT CONVENTIONS (spec rule unit_conventions, KEEP — the single most likely
differential-harness failure, so it is explicit and tested):
- gaps, fees, slippage, carry, drag, net edge: PERCENT (0.78 means 0.78%);
- spreads and mark divergence: BASIS POINTS (x10000);
- funding rates: FRACTION per settlement period (0.0001 = 1 bp = 0.01%),
  converted to percent (x100) only inside carry;
- the 0.1 funding-rate sanity cap is 10% per period expressed as a fraction.

NULL LAW: money math is Decimal-only and fail-closed.  None is the null of
the old kernel: any missing, non-finite, boolean, float, or out-of-guard
input makes the affected result None, never a guessed number.  Floats are
refused (not coerced) because binary floats are how the old which-side and
x100-vs-x10000 defects hid; feed Decimal, int, or numeric str.

GUARD ORDER (spec state_machine.entry_ladder_ordered_guards, KEEP): the
evaluate() ladder applies every gate in the frozen order and the FIRST
failing guard names the reject — reject-code parity with the old engine
depends on the order as much as on the numbers.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Dict, Optional, Sequence, Tuple

__all__ = [
    "DIRECTION_NORMAL",
    "DIRECTION_REVERSE",
    "REJECT_CODES",
    "DECISION_KINDS",
    "KernelConfig",
    "DEFAULT_CONFIG",
    "Book",
    "FundingMark",
    "FundingContext",
    "LiquidityAssessment",
    "DataQuality",
    "EdgeBreakdown",
    "BasisSnapshot",
    "executable_return_pct",
    "entry_gap_pct",
    "exit_gap_pct",
    "spread_bps",
    "mark_divergence_bps",
    "fees_pct",
    "ladder_walk_slippage_pct",
    "leg_slippage_pct",
    "depth_score",
    "book_pressure",
    "assess_liquidity",
    "data_quality",
    "slope_pct_per_min",
    "gap_stats",
    "expected_hold_ms",
    "funding_carry_pct",
    "flip_risk",
    "discounted_carry_pct",
    "capture_factor",
    "funding_context",
    "net_edge",
    "evaluate",
]

PRECISION = 50
MAX_SAFE_INTEGER = 9_007_199_254_740_991  # JS Number.MAX_SAFE_INTEGER parity

DIRECTION_NORMAL = "long_spot_short_perp"    # buy spot, short perp
DIRECTION_REVERSE = "short_spot_long_perp"   # priced for exits only; not traded
_DIRECTION_SIGN = {DIRECTION_NORMAL: Decimal(1), DIRECTION_REVERSE: Decimal(-1)}

# Frozen reject vocabulary subset used by the NORMAL entry ladder (parity:
# codes are never invented per run).
REJECT_CODES = (
    "spot_missing",
    "perp_missing",
    "data_stale",
    "book_missing",
    "spread_too_wide",
    "depth_too_thin",
    "edge_below_fees",
    "funding_against_trade",
    "gap_not_persistent",
    "edge_below_threshold",
    "kill_switch_on",
)
DECISION_KINDS = ("ignore", "watch", "focus", "proposal")

# data_quality suspect reasons, in emission order.
_DQ_REASON_ORDER = (
    "leg_never_seen",
    "funding_never_seen",
    "funding_stale",
    "leg_timestamp_skew",
    "mark_book_divergence",
)


def _dec(value: Any) -> Optional[Decimal]:
    """One finite Decimal, or None (fail-closed null).

    Accepts Decimal, int, and numeric str.  bool and float are refused by
    design (floats are the old engine's unit-defect vector; the null law
    turns them into a refusal, never a coercion).  NaN/Infinity -> None.
    """
    if value is None or isinstance(value, (bool, float)):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    return None


def _safe_int(value: Any) -> Optional[int]:
    """A positive JS-safe integer, or None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > MAX_SAFE_INTEGER:
        return None
    return value


def _clamp01(value: Decimal) -> Decimal:
    if value < 0:
        return Decimal(0)
    if value > 1:
        return Decimal(1)
    return value


@dataclass(frozen=True)
class KernelConfig:
    """Spec defaults with their KEEP/ADAPT verdicts.

    Every default is the old engine's measured value; validation reproduces
    the spec's config_validation_invariants rule (KEEP) and fails closed
    with ValueError on construction.
    """

    # --- spreads / divergence (BPS) ---
    max_spread_bps: Decimal = Decimal("25")                # KEEP
    max_mark_divergence_bps: Decimal = Decimal("50")       # KEEP
    # --- fees (PERCENT per leg) ---
    spot_taker_fee_pct: Decimal = Decimal("0.1")           # KEEP (re-verify at deploy)
    perp_taker_fee_pct: Decimal = Decimal("0.05")          # KEEP (re-verify at deploy)
    # --- slippage model (PERCENT) ---
    slippage_pct_estimate: Decimal = Decimal("0.05")       # KEEP
    thin_ladder_penalty_multiple: Decimal = Decimal("4")   # KEEP (fail-expensive)
    tob_knee_consumption: Decimal = Decimal("0.25")        # KEEP (crude by design)
    # --- depth ---
    min_depth_score: Decimal = Decimal("0.3")              # KEEP (~1.05x coverage)
    # --- funding (rates are FRACTIONS per period) ---
    funding_period_ms: int = 28_800_000                    # ADAPT: per-symbol input;
    #   8h default retained only for differential parity — pass the symbol's
    #   real period, a wrong period silently miscounts settlements.
    rate_sanity_abs_max: Decimal = Decimal("0.1")          # KEEP (10%/period cap)
    max_funding_drag_pct: Decimal = Decimal("0.15")        # KEEP
    funding_stale_ms: int = 10_000                         # KEEP
    max_future_skew_ms: int = 2_000                        # KEEP
    funding_history_keep: int = 32                         # KEEP (validated >= 3)
    # --- flip risk (ADAPT: keep 0.6/0.4/5bp for parity, re-fit later) ---
    flip_weight: Decimal = Decimal("0.6")
    magnitude_weight: Decimal = Decimal("0.4")
    magnitude_anchor_fraction: Decimal = Decimal("0.0005")
    # --- expected hold (KEEP) ---
    default_hold_ms: int = 14_400_000
    min_hold_ms: int = 600_000
    max_hold_ms: int = 86_400_000
    closing_slope_epsilon: Decimal = Decimal("1e-6")       # KEEP (pct/min)
    # --- entry thresholds (KEEP: owner's documented posture) ---
    watch_gap_pct: Decimal = Decimal("0.15")
    focus_gap_pct: Decimal = Decimal("0.3")
    min_net_basis_pct: Decimal = Decimal("0.25")
    persist_ticks: int = 5
    max_gap_cv: Decimal = Decimal("0.6")
    tick_ms: int = 2_000
    gap_window_size: int = 60                              # KEEP (validated >= 3)
    # --- data quality (ADAPT: shape kept exactly for parity; the component
    #     constants are hand-tuned old defaults) ---
    min_data_quality: Decimal = Decimal("0.4")
    stale_ms: int = 5_000                                  # KEEP
    max_leg_skew_ms: int = 2_500                           # KEEP
    unknown_skew_component: Decimal = Decimal("0.5")
    unknown_divergence_component: Decimal = Decimal("0.75")
    no_ladder_component: Decimal = Decimal("0.7")

    def __post_init__(self) -> None:
        for spec_field in dataclasses.fields(self):
            value = getattr(self, spec_field.name)
            if isinstance(value, bool):
                raise ValueError(f"config {spec_field.name} must be numeric")
            if isinstance(value, Decimal):
                if not value.is_finite():
                    raise ValueError(f"config {spec_field.name} must be finite")
                # Config content identity rejects negative zero (KEEP).
                if value == 0 and value.is_signed():
                    raise ValueError(f"config {spec_field.name} is negative zero")
            elif not isinstance(value, int):
                raise ValueError(f"config {spec_field.name} must be Decimal or int")
        strictly_positive = (
            "max_spread_bps", "max_mark_divergence_bps", "slippage_pct_estimate",
            "thin_ladder_penalty_multiple", "tob_knee_consumption",
            "min_depth_score", "funding_period_ms", "rate_sanity_abs_max",
            "max_funding_drag_pct", "funding_stale_ms", "magnitude_anchor_fraction",
            "default_hold_ms", "min_hold_ms", "max_hold_ms",
            "closing_slope_epsilon", "watch_gap_pct", "focus_gap_pct",
            "min_net_basis_pct", "persist_ticks", "max_gap_cv", "tick_ms",
            "min_data_quality", "stale_ms", "max_leg_skew_ms",
        )
        for name in strictly_positive:
            if getattr(self, name) <= 0:
                raise ValueError(f"config {name} must be > 0")
        non_negative = (
            "spot_taker_fee_pct", "perp_taker_fee_pct", "max_future_skew_ms",
            "flip_weight", "magnitude_weight", "unknown_skew_component",
            "unknown_divergence_component", "no_ladder_component",
        )
        for name in non_negative:
            if getattr(self, name) < 0:
                raise ValueError(f"config {name} must be >= 0")
        if self.watch_gap_pct > self.focus_gap_pct:
            raise ValueError("config watch_gap_pct must be <= focus_gap_pct")
        if not self.min_hold_ms <= self.default_hold_ms <= self.max_hold_ms:
            raise ValueError(
                "config requires min_hold_ms <= default_hold_ms <= max_hold_ms"
            )
        if self.max_hold_ms > 1000 * self.funding_period_ms:
            raise ValueError("config max_hold_ms/funding_period_ms must be <= 1000")
        if self.gap_window_size < 3 or self.funding_history_keep < 3:
            raise ValueError("config rolling windows must be >= 3")


DEFAULT_CONFIG = KernelConfig()


@dataclass(frozen=True)
class Book:
    """One venue's touch: executable best bid/ask with visible quantities."""

    bid: Optional[Decimal]
    ask: Optional[Decimal]
    bid_qty: Optional[Decimal]
    ask_qty: Optional[Decimal]


@dataclass(frozen=True)
class FundingMark:
    """The perp mark observation funding is priced from."""

    mark_price: Optional[Decimal]
    funding_rate: Optional[Decimal]     # FRACTION per period
    next_funding_ts: Optional[int]      # epoch ms
    ts: Optional[int]                   # observation epoch ms


@dataclass(frozen=True)
class FundingContext:
    """Fresh-or-all-null funding view (spec funding_freshness_fail_closed)."""

    fresh: bool
    funding_rate: Optional[Decimal]         # fraction/period; None unless fresh
    ms_to_funding: Optional[Decimal]
    expected_hold_ms: Decimal               # computed even when not fresh
    carry_raw_pct: Optional[Decimal]        # pre-discount carry, PERCENT
    flip_risk: Optional[Decimal]
    carry_pct: Optional[Decimal]            # flip-discounted carry, PERCENT
    capture_factor: Decimal                 # 0 unless raw carry pays inside hold
    staleness_ms: Optional[Decimal]         # reported staleness, floored at 0


@dataclass(frozen=True)
class LiquidityAssessment:
    """NORMAL-direction liquidity at size (spot asks + perp bids consumed)."""

    depth_score: Optional[Decimal]
    slippage_pct: Optional[Decimal]
    ladder_backed: bool
    book_pressure: Optional[Decimal]


@dataclass(frozen=True)
class DataQuality:
    score: Decimal
    mark_divergence_bps: Optional[Decimal]
    suspect_reasons: Tuple[str, ...]


@dataclass(frozen=True)
class EdgeBreakdown:
    """Itemised cost assembly (spec cost_assembly + net_edge, KEEP)."""

    gross_pct: Optional[Decimal]
    fees_pct: Optional[Decimal]
    slippage_pct: Optional[Decimal]
    funding_drag_pct: Decimal           # max(0, -carry); 0 when carry unknown
    borrow_cost_pct: Decimal            # always 0 for NORMAL
    carry_pct: Optional[Decimal]
    total_costs_pct: Optional[Decimal]  # assembled for display even w/o carry
    net_pct: Optional[Decimal]          # None when gross or carry is unknown


@dataclass(frozen=True)
class BasisSnapshot:
    """One symbol's per-tick inputs for the NORMAL entry decision.

    The kernel is stateless: window-derived inputs (slope, stability CV,
    persistence ticks) are produced by the scanner area's tracker and handed
    in here; ingestion guarantees (sorted uncrossed ladders, in-order books)
    are the ingest area's job.
    """

    now_ms: int
    spot_book: Optional[Book]
    perp_book: Optional[Book]
    spot_staleness_ms: Optional[int]
    perp_staleness_ms: Optional[int]
    leg_skew_ms: Optional[int]
    spot_ask_ladder: Optional[Sequence[Sequence[Any]]]   # NORMAL entry hits spot asks
    perp_bid_ladder: Optional[Sequence[Sequence[Any]]]   # ... and perp bids
    mark: Optional[FundingMark]
    mark_staleness_ms: Optional[int]
    funding_history: Sequence[Any] = ()
    slope_pct_per_min: Optional[Decimal] = None
    stability_cv: Optional[Decimal] = None
    persist_ticks: int = 0
    notional_usd: Optional[Decimal] = None   # ADAPT: sizing is an input, never
    #   the old toy 10 USD default (spec old_sizing_default, REJECT)
    kill_switch_on: bool = True              # ADAPT money_control_flags: starts
    #   halted; see-but-cannot-arm is preserved by guard order


# --------------------------------------------------------------------------
# Gap / return law
# --------------------------------------------------------------------------

def executable_return_pct(
    sell_proceeds_price: Any, long_entry_price: Any
) -> Optional[Decimal]:
    """Canonical versioned return law (KEEP).

    schema ExecutableBasisReturnContractV1, version
    basis-executable-long-leg-notional-v1, law RETURN_ON_LONG_LEG_ENTRY_NOTIONAL:
    return_pct = (sell_proceeds - long_entry) / long_entry * 100.  The
    denominator is ALWAYS the price of the leg BOUGHT at entry.  None unless
    both prices > 0.
    """
    sell = _dec(sell_proceeds_price)
    buy = _dec(long_entry_price)
    if sell is None or buy is None or sell <= 0 or buy <= 0:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        return (sell - buy) / buy * Decimal(100)


def entry_gap_pct(spot_ask: Any, perp_bid: Any) -> Optional[Decimal]:
    """NORMAL entry gap (KEEP): (perp_bid - spot_ask)/spot_ask*100, PERCENT.

    Real touch bid/ask only — never mids, never mark.  The long leg bought
    at entry is spot (at its ask), so spot_ask is the denominator.
    """
    return executable_return_pct(perp_bid, spot_ask)


def exit_gap_pct(spot_bid: Any, perp_ask: Any) -> Optional[Decimal]:
    """NORMAL unwind pair (ADAPT): (spot_bid - perp_ask)/perp_ask*100.

    Same return law; the leg bought at the unwind is the perp (at its ask).
    Exposed as exit math only — the ported exit TRIGGER stays the old
    engine's entry-gap-below-watch rule (lifecycle area), for parity.
    """
    return executable_return_pct(spot_bid, perp_ask)


# --------------------------------------------------------------------------
# Spreads, divergence, fees
# --------------------------------------------------------------------------

def spread_bps(bid: Any, ask: Any) -> Optional[Decimal]:
    """Mid-referenced spread in BPS (KEEP): (ask-bid)/mid*10000.

    None when bid <= 0, ask <= 0, or ask < bid (crossed book); ask == bid
    gives 0.
    """
    bid_px = _dec(bid)
    ask_px = _dec(ask)
    if bid_px is None or ask_px is None or bid_px <= 0 or ask_px <= 0:
        return None
    if ask_px < bid_px:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        mid = (bid_px + ask_px) / Decimal(2)
        return (ask_px - bid_px) / mid * Decimal(10_000)


def mark_divergence_bps(perp_mid: Any, mark: Any) -> Optional[Decimal]:
    """abs(perp_mid - mark)/mark*10000 in BPS (KEEP); None unless both > 0."""
    mid = _dec(perp_mid)
    mark_px = _dec(mark)
    if mid is None or mark_px is None or mid <= 0 or mark_px <= 0:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        return abs(mid - mark_px) / mark_px * Decimal(10_000)


def fees_pct(spot_taker_pct: Any, perp_taker_pct: Any) -> Optional[Decimal]:
    """Four-leg taker fees in PERCENT (KEEP): 2*spot + 2*perp.

    Open both legs + close both legs, all taker; inputs are percent PER LEG.
    Defaults 0.1/0.05 give the frozen 0.30% round trip.
    """
    spot = _dec(spot_taker_pct)
    perp = _dec(perp_taker_pct)
    if spot is None or perp is None or spot < 0 or perp < 0:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        return Decimal(2) * spot + Decimal(2) * perp


# --------------------------------------------------------------------------
# Slippage and depth
# --------------------------------------------------------------------------

def _ladder_rows(
    ladder: Optional[Sequence[Sequence[Any]]],
) -> Optional[Tuple[Tuple[Decimal, Decimal], ...]]:
    """Validate [price, qty] rows; None on any invalid row (fail closed)."""
    if ladder is None:
        return None
    rows = []
    for raw in ladder:
        if not isinstance(raw, (tuple, list)) or len(raw) != 2:
            return None
        price = _dec(raw[0])
        qty = _dec(raw[1])
        if price is None or qty is None or price <= 0 or qty <= 0:
            return None
        rows.append((price, qty))
    return tuple(rows)


def ladder_walk_slippage_pct(
    ladder: Optional[Sequence[Sequence[Any]]], notional_usd: Any
) -> Optional[Decimal]:
    """Average-fill-vs-touch ladder walk in PERCENT (KEEP).

    Walk depth rows [price, base_qty] best-first consuming notional_usd:
    per row take_usd = min(remaining, price*qty); avg_px = cost/filled;
    slippage_pct = abs(avg_px - touch)/touch*100 with touch = first row's
    price.  None when the ladder is empty/invalid, notional <= 0, touch <= 0,
    or the ladder cannot cover the size — the underfill None is load-bearing
    (it triggers the thin-ladder penalty in leg_slippage_pct).
    """
    rows = _ladder_rows(ladder)
    notional = _dec(notional_usd)
    if not rows or notional is None or notional <= 0:
        return None
    touch = rows[0][0]
    with localcontext() as context:
        context.prec = PRECISION
        remaining = notional
        cost_usd = Decimal(0)
        filled_base = Decimal(0)
        for price, qty in rows:
            if remaining <= 0:
                break
            level_usd = price * qty
            take_usd = level_usd if level_usd < remaining else remaining
            cost_usd += take_usd
            filled_base += take_usd / price
            remaining -= take_usd
        if remaining > 0 or filled_base <= 0:
            return None
        avg_px = cost_usd / filled_base
        return abs(avg_px - touch) / touch * Decimal(100)


def leg_slippage_pct(
    ladder_rows: Optional[Sequence[Sequence[Any]]],
    notional_usd: Any,
    touch_px: Any,
    touch_qty: Any,
    estimate_pct: Any = None,
    config: Optional[KernelConfig] = None,
) -> Optional[Decimal]:
    """One leg's slippage in PERCENT: ladder walk, penalty, or touch fallback.

    - Ladder provided (even a thin one): walk it; a walk that cannot price
      the size (underfill/degenerate) costs the thin-ladder penalty
      estimate_pct * 4 (KEEP: deliberately fail-expensive placeholder).
    - No ladder (None): top-of-book model (KEEP: crude by design) —
      base = estimate_pct/2; invalid touch/notional (<= 0) costs base * 4;
      else base * max(1, consumption/0.25) with
      consumption = notional/(touch_px*touch_qty).
    Returns None only when the estimate itself is unusable (fail closed).
    """
    cfg = config if config is not None else DEFAULT_CONFIG
    estimate = _dec(estimate_pct) if estimate_pct is not None else cfg.slippage_pct_estimate
    if estimate is None or estimate < 0:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        if ladder_rows is not None:
            walked = ladder_walk_slippage_pct(ladder_rows, notional_usd)
            if walked is not None:
                return walked
            return estimate * cfg.thin_ladder_penalty_multiple
        base = estimate / Decimal(2)
        notional = _dec(notional_usd)
        px = _dec(touch_px)
        qty = _dec(touch_qty)
        if (
            notional is None or px is None or qty is None
            or notional <= 0 or px <= 0 or qty <= 0
        ):
            return base * cfg.thin_ladder_penalty_multiple
        consumption = notional / (px * qty)
        scale = consumption / cfg.tob_knee_consumption
        if scale < 1:
            scale = Decimal(1)
        return base * scale


def _leg_depth_score(
    ladder: Optional[Sequence[Sequence[Any]]],
    touch: Optional[Tuple[Any, Any]],
    notional: Optional[Decimal],
) -> Optional[Decimal]:
    """clamp01(log10(1 + coverage)/log10(11)) over visible USD (KEEP).

    Ladder present (even empty) -> visible = sum(price*qty) over its rows;
    ladder None -> touch px*qty fallback; neither -> None.  10x coverage
    saturates at exactly 1.0; 1x is ~0.289 (below the 0.3 gate).
    """
    if notional is None or notional <= 0:
        return None
    visible: Optional[Decimal] = None
    if ladder is not None:
        rows = _ladder_rows(ladder)
        if rows is None:
            return None
        visible = Decimal(0)
        for price, qty in rows:
            visible += price * qty
    elif touch is not None:
        px = _dec(touch[0])
        qty = _dec(touch[1])
        if px is not None and qty is not None and px > 0 and qty > 0:
            visible = px * qty
    if visible is None:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        coverage = visible / notional
        score = (Decimal(1) + coverage).log10() / Decimal(11).log10()
        return _clamp01(score)


def depth_score(
    spot_ladder: Optional[Sequence[Sequence[Any]]],
    perp_ladder: Optional[Sequence[Sequence[Any]]],
    spot_touch: Optional[Tuple[Any, Any]],
    perp_touch: Optional[Tuple[Any, Any]],
    notional_usd: Any,
) -> Optional[Decimal]:
    """Two-leg depth score (KEEP): min(spot leg, perp leg); None if either
    leg is None.  A two-leg trade is only as deep as its thinner leg."""
    notional = _dec(notional_usd)
    spot_leg = _leg_depth_score(spot_ladder, spot_touch, notional)
    perp_leg = _leg_depth_score(perp_ladder, perp_touch, notional)
    if spot_leg is None or perp_leg is None:
        return None
    return spot_leg if spot_leg < perp_leg else perp_leg


def book_pressure(
    spot_book: Optional[Book], perp_book: Optional[Book]
) -> Optional[Decimal]:
    """NORMAL-direction touch pressure (KEEP; informational, never a gate).

    (spot_ask_usd + perp_bid_usd on the HIT sides) / (spot_bid_usd +
    perp_ask_usd on the opposite sides); None unless both books are present
    and the opposite-side USD > 0.
    """
    if spot_book is None or perp_book is None:
        return None
    spot_ask = _dec(spot_book.ask)
    spot_ask_qty = _dec(spot_book.ask_qty)
    perp_bid = _dec(perp_book.bid)
    perp_bid_qty = _dec(perp_book.bid_qty)
    spot_bid = _dec(spot_book.bid)
    spot_bid_qty = _dec(spot_book.bid_qty)
    perp_ask = _dec(perp_book.ask)
    perp_ask_qty = _dec(perp_book.ask_qty)
    values = (
        spot_ask, spot_ask_qty, perp_bid, perp_bid_qty,
        spot_bid, spot_bid_qty, perp_ask, perp_ask_qty,
    )
    if any(value is None or value <= 0 for value in values):
        return None
    with localcontext() as context:
        context.prec = PRECISION
        hit_usd = spot_ask * spot_ask_qty + perp_bid * perp_bid_qty
        opposite_usd = spot_bid * spot_bid_qty + perp_ask * perp_ask_qty
        if opposite_usd <= 0:
            return None
        return hit_usd / opposite_usd


def assess_liquidity(
    spot_book: Optional[Book],
    perp_book: Optional[Book],
    spot_ask_ladder: Optional[Sequence[Sequence[Any]]],
    perp_bid_ladder: Optional[Sequence[Sequence[Any]]],
    notional_usd: Any,
    config: Optional[KernelConfig] = None,
) -> LiquidityAssessment:
    """NORMAL-direction liquidity at size (spec liquidity_sides, KEEP).

    Entry consumes spot ASKS and perp BIDS; each leg independently picks
    ladder walk vs top-of-book fallback; ladder_backed is True only when
    BOTH legs supplied ladders.
    """
    cfg = config if config is not None else DEFAULT_CONFIG
    spot_touch = None
    if spot_book is not None:
        spot_touch = (spot_book.ask, spot_book.ask_qty)
    perp_touch = None
    if perp_book is not None:
        perp_touch = (perp_book.bid, perp_book.bid_qty)
    score = depth_score(
        spot_ask_ladder, perp_bid_ladder, spot_touch, perp_touch, notional_usd
    )
    spot_slippage = leg_slippage_pct(
        spot_ask_ladder,
        notional_usd,
        spot_touch[0] if spot_touch is not None else None,
        spot_touch[1] if spot_touch is not None else None,
        cfg.slippage_pct_estimate,
        cfg,
    )
    perp_slippage = leg_slippage_pct(
        perp_bid_ladder,
        notional_usd,
        perp_touch[0] if perp_touch is not None else None,
        perp_touch[1] if perp_touch is not None else None,
        cfg.slippage_pct_estimate,
        cfg,
    )
    total_slippage: Optional[Decimal] = None
    if spot_slippage is not None and perp_slippage is not None:
        with localcontext() as context:
            context.prec = PRECISION
            total_slippage = spot_slippage + perp_slippage
    return LiquidityAssessment(
        depth_score=score,
        slippage_pct=total_slippage,
        ladder_backed=spot_ask_ladder is not None and perp_bid_ladder is not None,
        book_pressure=book_pressure(spot_book, perp_book),
    )


# --------------------------------------------------------------------------
# Data quality
# --------------------------------------------------------------------------

def _freshness_component(
    staleness_ms: Optional[Any], limit_ms: int
) -> Optional[Decimal]:
    """clamp01(1 - staleness/limit); None when staleness is unknown."""
    staleness = _dec(staleness_ms)
    if staleness is None:
        return None
    if staleness < 0:
        staleness = Decimal(0)
    with localcontext() as context:
        context.prec = PRECISION
        return _clamp01(Decimal(1) - staleness / Decimal(limit_ms))


def data_quality(
    spot_staleness_ms: Optional[Any],
    perp_staleness_ms: Optional[Any],
    mark_staleness_ms: Optional[Any],
    leg_skew_ms: Optional[Any],
    perp_book: Optional[Book],
    mark_price: Any,
    ladder_backed: bool,
    config: Optional[KernelConfig] = None,
) -> DataQuality:
    """Freshness-backbone data-quality composite (ADAPT: reproduce exactly).

    score = min(spot_fresh, perp_fresh, mark_fresh) * (0.5 + 0.5*skew_comp)
            * (0.6 + 0.4*div_comp) * ladder_comp, clamped 0..1.
    Never-seen legs/marks contribute 0 to the backbone; unknown skew is 0.5;
    unknown divergence is 0.75 (mild distrust); no-ladder is 0.7.
    """
    cfg = config if config is not None else DEFAULT_CONFIG
    reasons = set()

    spot_fresh = _freshness_component(spot_staleness_ms, cfg.stale_ms)
    if spot_fresh is None:
        spot_fresh = Decimal(0)
        reasons.add("leg_never_seen")
    perp_fresh = _freshness_component(perp_staleness_ms, cfg.stale_ms)
    if perp_fresh is None:
        perp_fresh = Decimal(0)
        reasons.add("leg_never_seen")

    mark_fresh = _freshness_component(mark_staleness_ms, cfg.funding_stale_ms)
    if mark_fresh is None:
        mark_fresh = Decimal(0)
        reasons.add("funding_never_seen")
    else:
        staleness = _dec(mark_staleness_ms)
        if staleness is not None and staleness > cfg.funding_stale_ms:
            reasons.add("funding_stale")

    skew = _dec(leg_skew_ms)
    if skew is None:
        skew_comp = cfg.unknown_skew_component
    else:
        skew_comp = _freshness_component(abs(skew), cfg.max_leg_skew_ms)
        if abs(skew) > cfg.max_leg_skew_ms:
            reasons.add("leg_timestamp_skew")

    perp_mid: Optional[Decimal] = None
    if perp_book is not None:
        bid = _dec(perp_book.bid)
        ask = _dec(perp_book.ask)
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            with localcontext() as context:
                context.prec = PRECISION
                perp_mid = (bid + ask) / Decimal(2)
    divergence = mark_divergence_bps(perp_mid, mark_price)
    if divergence is None:
        div_comp = cfg.unknown_divergence_component
    else:
        with localcontext() as context:
            context.prec = PRECISION
            div_comp = _clamp01(
                Decimal(1) - divergence / cfg.max_mark_divergence_bps
            )
        if divergence > cfg.max_mark_divergence_bps:
            reasons.add("mark_book_divergence")

    ladder_comp = Decimal(1) if ladder_backed else cfg.no_ladder_component

    with localcontext() as context:
        context.prec = PRECISION
        backbone = min(spot_fresh, perp_fresh, mark_fresh)
        half = Decimal("0.5")
        score = _clamp01(
            backbone
            * (half + half * skew_comp)
            * (Decimal("0.6") + Decimal("0.4") * div_comp)
            * ladder_comp
        )
    ordered = tuple(name for name in _DQ_REASON_ORDER if name in reasons)
    return DataQuality(
        score=score, mark_divergence_bps=divergence, suspect_reasons=ordered
    )


# --------------------------------------------------------------------------
# Gap window statistics (formulas only — the window itself is scanner state)
# --------------------------------------------------------------------------

def slope_pct_per_min(
    samples: Sequence[Tuple[Any, Any]],
) -> Optional[Decimal]:
    """Least-squares gap slope in PERCENT per MINUTE (KEEP).

    x = (ts - ts_first)/60000 minutes, y = gap pct;
    slope = (n*Sxy - Sx*Sy)/(n*Sxx - Sx^2).  None with < 2 samples, any
    invalid sample, or denominator magnitude < 1e-12 (indistinct
    timestamps).  Positive = widening.
    """
    if samples is None or len(samples) < 2:
        return None
    points = []
    for raw in samples:
        if not isinstance(raw, (tuple, list)) or len(raw) != 2:
            return None
        ts = _dec(raw[0])
        value = _dec(raw[1])
        if ts is None or value is None:
            return None
        points.append((ts, value))
    with localcontext() as context:
        context.prec = PRECISION
        t0 = points[0][0]
        n = Decimal(len(points))
        sum_x = Decimal(0)
        sum_y = Decimal(0)
        sum_xx = Decimal(0)
        sum_xy = Decimal(0)
        for ts, value in points:
            x = (ts - t0) / Decimal(60_000)
            sum_x += x
            sum_y += value
            sum_xx += x * x
            sum_xy += x * value
        denominator = n * sum_xx - sum_x * sum_x
        if abs(denominator) < Decimal("1e-12"):
            return None
        return (n * sum_xy - sum_x * sum_y) / denominator


def gap_stats(
    values: Sequence[Any],
) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    """(mean, sample std-dev, cv) over window values (KEEP).

    Std-dev uses the n-1 denominator and needs >= 2 samples (else None);
    cv = std_dev / max(abs(mean), 1e-9).  Any invalid value -> all None.
    """
    if values is None or len(values) == 0:
        return (None, None, None)
    decoded = []
    for raw in values:
        value = _dec(raw)
        if value is None:
            return (None, None, None)
        decoded.append(value)
    with localcontext() as context:
        context.prec = PRECISION
        n = Decimal(len(decoded))
        mean = sum(decoded, Decimal(0)) / n
        if len(decoded) < 2:
            return (mean, None, None)
        squares = sum(((value - mean) ** 2 for value in decoded), Decimal(0))
        std_dev = (squares / (n - 1)).sqrt()
        floor = Decimal("1e-9")
        magnitude = abs(mean)
        cv = std_dev / (magnitude if magnitude > floor else floor)
        return (mean, std_dev, cv)


# --------------------------------------------------------------------------
# Expected hold and funding
# --------------------------------------------------------------------------

def expected_hold_ms(
    gap_pct: Any,
    slope: Any,
    config: Optional[KernelConfig] = None,
) -> Decimal:
    """Convergence-time hold estimate in ms (KEEP).

    Closing gap (gap > 0 and slope < -1e-6 pct/min):
    clamp((gap/abs(slope)) minutes, min_hold_ms, max_hold_ms).  Widening,
    flat, near-zero slope, or null gap: clamp(default_hold_ms, min, max).
    Feeds the funding-carry settlement count.
    """
    cfg = config if config is not None else DEFAULT_CONFIG
    gap = _dec(gap_pct)
    slope_value = _dec(slope)
    minimum = Decimal(cfg.min_hold_ms)
    maximum = Decimal(cfg.max_hold_ms)

    def _clamp(value: Decimal) -> Decimal:
        if value < minimum:
            return minimum
        if value > maximum:
            return maximum
        return value

    if (
        gap is not None and gap > 0
        and slope_value is not None
        and slope_value < -cfg.closing_slope_epsilon
    ):
        with localcontext() as context:
            context.prec = PRECISION
            raw = gap / abs(slope_value) * Decimal(60_000)
            return _clamp(raw)
    return _clamp(Decimal(cfg.default_hold_ms))


def funding_carry_pct(
    hold_ms: Any,
    ms_to_next_ms: Any,
    period_ms: Any,
    known_rates: Sequence[Any],
    direction: str,
    rate_sanity_abs_max: Any = DEFAULT_CONFIG.rate_sanity_abs_max,
) -> Optional[Decimal]:
    """Funding carry over the hold in PERCENT via DISCRETE settlements (KEEP).

    Carry accrues ONLY at funding timestamps crossed by the hold — never
    time-prorated.  ms_to_next > hold: 0 settlements, carry 0.  Else
    crossings = 1 + floor((hold - ms_to_next)/period); a crossing whose
    settlement rate is not in known_rates makes the whole carry None (an
    unknown settlement stays unknown).  Used rates must be finite fractions
    with abs <= rate_sanity_abs_max (0.1 by default).  sign = +1 for
    long_spot_short_perp (the short perp leg RECEIVES positive funding), -1
    for short_spot_long_perp.
    carry_pct = sum(rate_i) * 100 * sign.
    """
    sign = _DIRECTION_SIGN.get(direction)
    hold = _dec(hold_ms)
    ms_to_next = _dec(ms_to_next_ms)
    period = _dec(period_ms)
    sanity_cap = _dec(rate_sanity_abs_max)
    if (
        sign is None or hold is None or ms_to_next is None or period is None
        or sanity_cap is None
    ):
        return None
    if hold < 0 or ms_to_next <= 0 or period <= 0 or sanity_cap <= 0:
        return None
    if ms_to_next > hold:
        return Decimal(0)
    with localcontext() as context:
        context.prec = PRECISION
        try:
            crossings_dec = Decimal(1) + (hold - ms_to_next) // period
        except InvalidOperation:
            return None
        if crossings_dec > MAX_SAFE_INTEGER:
            return None
        crossings = int(crossings_dec)
        if known_rates is None or crossings > len(known_rates):
            return None
        total = Decimal(0)
        for raw in list(known_rates)[:crossings]:
            rate = _dec(raw)
            if rate is None or abs(rate) > sanity_cap:
                return None
            total += rate
        return total * Decimal(100) * sign


def flip_risk(
    rate_history: Sequence[Any],
    current_rate: Any = None,
    config: Optional[KernelConfig] = None,
) -> Optional[Decimal]:
    """Funding sign-flip risk in [0, 1] (ADAPT: weights kept for parity).

    Needs >= 3 finite history rates (fractions/period), else None.
    flip_share = consecutive nonzero sign changes / (n-1) — zero-touching
    transitions are not flips.  mag_risk = clamp01(1 - abs(rate)/0.0005)
    using current_rate when finite, else the last history rate.
    flip_risk = clamp01(0.6*flip_share + 0.4*mag_risk).
    """
    cfg = config if config is not None else DEFAULT_CONFIG
    if rate_history is None:
        return None
    finite = []
    for raw in rate_history:
        rate = _dec(raw)
        if rate is not None:
            finite.append(rate)
    if len(finite) < 3:
        return None
    with localcontext() as context:
        context.prec = PRECISION
        flips = 0
        for previous, current in zip(finite, finite[1:]):
            if previous != 0 and current != 0 and (previous < 0) != (current < 0):
                flips += 1
        flip_share = Decimal(flips) / Decimal(len(finite) - 1)
        anchor_rate = _dec(current_rate)
        if anchor_rate is None:
            anchor_rate = finite[-1]
        mag_risk = _clamp01(
            Decimal(1) - abs(anchor_rate) / cfg.magnitude_anchor_fraction
        )
        return _clamp01(cfg.flip_weight * flip_share + cfg.magnitude_weight * mag_risk)


def discounted_carry_pct(
    raw_carry_pct: Any, flip_risk_value: Any
) -> Optional[Decimal]:
    """Asymmetric flip discount (ADAPT): haircut the bonus, never the drag.

    POSITIVE carry with known flip risk: carry * (1 - risk).  Negative
    carry is never discounted; unknown flip risk leaves positive carry
    undiscounted.
    """
    carry = _dec(raw_carry_pct)
    if carry is None:
        return None
    risk = _dec(flip_risk_value)
    if carry > 0 and risk is not None:
        with localcontext() as context:
            context.prec = PRECISION
            return carry * (Decimal(1) - risk)
    return carry


def capture_factor(
    raw_carry_pct: Any, ms_to_funding: Any, hold_ms: Any, period_ms: Any
) -> Decimal:
    """Priority-only settlement-imminence bump (KEEP; never a gate).

    clamp01(1 - ms_to_funding/period) only when RAW (pre-discount) carry > 0
    AND the settlement lands inside the hold; else 0.  Uses raw carry so the
    flip discount cannot zero the bump.
    """
    carry = _dec(raw_carry_pct)
    ms_to = _dec(ms_to_funding)
    hold = _dec(hold_ms)
    period = _dec(period_ms)
    if (
        carry is None or ms_to is None or hold is None or period is None
        or period <= 0 or carry <= 0 or ms_to > hold
    ):
        return Decimal(0)
    with localcontext() as context:
        context.prec = PRECISION
        return _clamp01(Decimal(1) - ms_to / period)


def funding_context(
    mark: Optional[FundingMark],
    mark_staleness_ms: Optional[Any],
    history_rates: Sequence[Any],
    direction: str,
    gap_pct: Any,
    slope: Any,
    now_ms: int,
    config: Optional[KernelConfig] = None,
) -> FundingContext:
    """Fresh-or-all-null funding view (KEEP: fail closed, never prorated).

    Fresh only if ALL hold: mark exists; rate finite with abs within the
    configured sanity cap; mark_price finite > 0; mark.ts a positive safe
    integer; staleness known and within
    [-max_future_skew_ms, funding_stale_ms]; next_funding_ts a safe integer
    STRICTLY > now (a past schedule cannot manufacture a settlement).  Not
    fresh -> rate/carry/flip all None, capture 0, expected hold still
    computed, reported staleness floored at 0.  The engine knows only the
    CURRENT rate, so any hold crossing >= 2 settlements yields carry None
    by construction.
    """
    cfg = config if config is not None else DEFAULT_CONFIG
    hold = expected_hold_ms(gap_pct, slope, cfg)
    staleness = _dec(mark_staleness_ms)
    reported = None
    if staleness is not None:
        reported = staleness if staleness > 0 else Decimal(0)

    def _not_fresh() -> FundingContext:
        return FundingContext(
            fresh=False,
            funding_rate=None,
            ms_to_funding=None,
            expected_hold_ms=hold,
            carry_raw_pct=None,
            flip_risk=None,
            carry_pct=None,
            capture_factor=Decimal(0),
            staleness_ms=reported,
        )

    if mark is None:
        return _not_fresh()
    rate = _dec(mark.funding_rate)
    mark_px = _dec(mark.mark_price)
    if rate is None or abs(rate) > cfg.rate_sanity_abs_max:
        return _not_fresh()
    if mark_px is None or mark_px <= 0:
        return _not_fresh()
    if _safe_int(mark.ts) is None:
        return _not_fresh()
    if staleness is None:
        return _not_fresh()
    if staleness > cfg.funding_stale_ms or staleness < -cfg.max_future_skew_ms:
        return _not_fresh()
    next_ts = _safe_int(mark.next_funding_ts)
    if next_ts is None or next_ts <= now_ms:
        return _not_fresh()

    with localcontext() as context:
        context.prec = PRECISION
        ms_to_funding = Decimal(next_ts - now_ms)
    raw_carry = funding_carry_pct(
        hold, ms_to_funding, cfg.funding_period_ms, [rate], direction,
        rate_sanity_abs_max=cfg.rate_sanity_abs_max,
    )
    risk = flip_risk(history_rates, rate, cfg)
    carry = discounted_carry_pct(raw_carry, risk)
    capture = capture_factor(raw_carry, ms_to_funding, hold, cfg.funding_period_ms)
    return FundingContext(
        fresh=True,
        funding_rate=rate,
        ms_to_funding=ms_to_funding,
        expected_hold_ms=hold,
        carry_raw_pct=raw_carry,
        flip_risk=risk,
        carry_pct=carry,
        capture_factor=capture,
        staleness_ms=reported,
    )


# --------------------------------------------------------------------------
# Edge assembly
# --------------------------------------------------------------------------

def net_edge(
    gross_pct: Any,
    fees: Any,
    slippage_pct: Any,
    carry_pct: Any,
    borrow_cost_pct: Any = Decimal(0),
) -> EdgeBreakdown:
    """Cost assembly + net edge (KEEP).

    funding_drag = max(0, -carry) (drag only); total_costs = fees +
    slippage + drag + borrow (borrow = 0 for NORMAL).  Positive carry is
    NOT a cost — it enters only as the net-edge bonus:
    net = gross - total_costs + max(0, carry).  net is None when gross is
    None OR carry is unknown, even though the cost breakdown is still
    assembled treating unknown carry as 0 for display.  Carry here is the
    flip-risk-discounted value.
    """
    gross = _dec(gross_pct)
    fee_total = _dec(fees)
    slippage = _dec(slippage_pct)
    carry = _dec(carry_pct)
    borrow = _dec(borrow_cost_pct)
    with localcontext() as context:
        context.prec = PRECISION
        drag = Decimal(0)
        if carry is not None and carry < 0:
            drag = -carry
        total_costs: Optional[Decimal] = None
        if fee_total is not None and slippage is not None and borrow is not None:
            total_costs = fee_total + slippage + drag + borrow
        net: Optional[Decimal] = None
        if gross is not None and carry is not None and total_costs is not None:
            bonus = carry if carry > 0 else Decimal(0)
            net = gross - total_costs + bonus
    return EdgeBreakdown(
        gross_pct=gross,
        fees_pct=fee_total,
        slippage_pct=slippage,
        funding_drag_pct=drag,
        borrow_cost_pct=borrow if borrow is not None else Decimal(0),
        carry_pct=carry,
        total_costs_pct=total_costs,
        net_pct=net,
    )


# --------------------------------------------------------------------------
# The entry decision ladder
# --------------------------------------------------------------------------

def evaluate(
    snapshot: BasisSnapshot, config: Optional[KernelConfig] = None
) -> Dict[str, Any]:
    """NORMAL entry decision with the frozen guard ORDER (KEEP).

    Returns {"accept": bool, "reason": str, "components": dict}.  accept is
    True only for a proposal; reason is the FIRST failing guard's reject
    code, or the decision kind (ignore/watch/focus/proposal).  Components
    carry the full audited breakdown regardless of outcome.  Order (spec
    entry_ladder_ordered_guards; borrow step 13 is reverse-only, out of
    port scope): spot_missing, perp_missing, data_stale (staleness), data_stale
    (leg skew), data_stale (dq composite), data_stale (funding), book_missing,
    ignore (< watch), spread_too_wide, depth_too_thin, edge_below_fees,
    funding_against_trade, gap_not_persistent, edge_below_threshold,
    watch/focus (persistence short), kill_switch_on, proposal.
    """
    cfg = config if config is not None else DEFAULT_CONFIG

    spot_book = snapshot.spot_book
    perp_book = snapshot.perp_book
    gap = None
    spot_spread = None
    perp_spread = None
    if spot_book is not None and perp_book is not None:
        gap = entry_gap_pct(spot_book.ask, perp_book.bid)
        spot_spread = spread_bps(spot_book.bid, spot_book.ask)
        perp_spread = spread_bps(perp_book.bid, perp_book.ask)

    liquidity = assess_liquidity(
        spot_book,
        perp_book,
        snapshot.spot_ask_ladder,
        snapshot.perp_bid_ladder,
        snapshot.notional_usd,
        cfg,
    )
    quality = data_quality(
        snapshot.spot_staleness_ms,
        snapshot.perp_staleness_ms,
        snapshot.mark_staleness_ms,
        snapshot.leg_skew_ms,
        perp_book,
        snapshot.mark.mark_price if snapshot.mark is not None else None,
        liquidity.ladder_backed,
        cfg,
    )
    funding = funding_context(
        snapshot.mark,
        snapshot.mark_staleness_ms,
        snapshot.funding_history,
        DIRECTION_NORMAL,
        gap,
        snapshot.slope_pct_per_min,
        snapshot.now_ms,
        cfg,
    )
    fee_total = fees_pct(cfg.spot_taker_fee_pct, cfg.perp_taker_fee_pct)
    edge = net_edge(
        gap, fee_total, liquidity.slippage_pct, funding.carry_pct, Decimal(0)
    )
    stability_cv = _dec(snapshot.stability_cv)

    components: Dict[str, Any] = {
        "gap_pct": gap,
        "spot_spread_bps": spot_spread,
        "perp_spread_bps": perp_spread,
        "dq_score": quality.score,
        "dq_suspect_reasons": quality.suspect_reasons,
        "mark_divergence_bps": quality.mark_divergence_bps,
        "depth_score": liquidity.depth_score,
        "slippage_pct": liquidity.slippage_pct,
        "ladder_backed": liquidity.ladder_backed,
        "book_pressure": liquidity.book_pressure,
        "fees_pct": edge.fees_pct,
        "funding_fresh": funding.fresh,
        "funding_rate": funding.funding_rate,
        "ms_to_funding": funding.ms_to_funding,
        "expected_hold_ms": funding.expected_hold_ms,
        "carry_raw_pct": funding.carry_raw_pct,
        "flip_risk": funding.flip_risk,
        "carry_pct": funding.carry_pct,
        "capture_factor": funding.capture_factor,
        "funding_drag_pct": edge.funding_drag_pct,
        "borrow_cost_pct": edge.borrow_cost_pct,
        "gross_pct": edge.gross_pct,
        "total_costs_pct": edge.total_costs_pct,
        "net_pct": edge.net_pct,
        "stability_cv": stability_cv,
        "persist_ticks": snapshot.persist_ticks,
        "notional_usd": _dec(snapshot.notional_usd),
    }

    def _decision(accept: bool, reason: str) -> Dict[str, Any]:
        return {"accept": accept, "reason": reason, "components": components}

    # (1)-(2) structure
    if spot_book is None:
        return _decision(False, "spot_missing")
    if perp_book is None:
        return _decision(False, "perp_missing")
    # (3) both book stalenesses known and within stale_ms
    spot_staleness = _dec(snapshot.spot_staleness_ms)
    perp_staleness = _dec(snapshot.perp_staleness_ms)
    if (
        spot_staleness is None or spot_staleness > cfg.stale_ms
        or perp_staleness is None or perp_staleness > cfg.stale_ms
    ):
        return _decision(False, "data_stale")
    # (4) leg skew (direct check only when the skew is known)
    skew = _dec(snapshot.leg_skew_ms)
    if skew is not None and abs(skew) > cfg.max_leg_skew_ms:
        return _decision(False, "data_stale")
    # (5) dq composite
    if quality.score < cfg.min_data_quality:
        return _decision(False, "data_stale")
    # (6) funding fresh AND rate known AND carry known
    if not funding.fresh or funding.funding_rate is None or funding.carry_pct is None:
        return _decision(False, "data_stale")
    # (7) entry gap computable
    if gap is None:
        return _decision(False, "book_missing")
    # (8) below watch: ignore, not a reject
    if gap < cfg.watch_gap_pct:
        return _decision(False, "ignore")
    # (9) spreads (fail closed on an uncomputable spread)
    if (
        spot_spread is None or spot_spread > cfg.max_spread_bps
        or perp_spread is None or perp_spread > cfg.max_spread_bps
    ):
        return _decision(False, "spread_too_wide")
    # (10) depth at intended size (unknown depth is not deep)
    if liquidity.depth_score is None or liquidity.depth_score < cfg.min_depth_score:
        return _decision(False, "depth_too_thin")
    # (11) gross vs fees + slippage ONLY (drag and borrow are gated later)
    if (
        edge.fees_pct is None or edge.slippage_pct is None
        or gap <= edge.fees_pct + edge.slippage_pct
    ):
        return _decision(False, "edge_below_fees")
    # (12) funding drag
    if edge.funding_drag_pct > cfg.max_funding_drag_pct:
        return _decision(False, "funding_against_trade")
    # (13) borrow guards are reverse-only — out of port scope.
    # (14) stability: noisy AND not yet persisted
    if (
        stability_cv is not None
        and stability_cv > cfg.max_gap_cv
        and snapshot.persist_ticks < cfg.persist_ticks
    ):
        return _decision(False, "gap_not_persistent")
    # (15) net edge threshold
    if edge.net_pct is None or edge.net_pct < cfg.min_net_basis_pct:
        return _decision(False, "edge_below_threshold")
    # (16) persistence gate: short -> watch/focus (keeps accruing)
    if snapshot.persist_ticks < cfg.persist_ticks:
        kind = "focus" if gap >= cfg.focus_gap_pct else "watch"
        return _decision(False, kind)
    # (17) kill switch: the engine keeps seeing, it cannot arm
    if snapshot.kill_switch_on:
        return _decision(False, "kill_switch_on")
    return _decision(True, "proposal")
