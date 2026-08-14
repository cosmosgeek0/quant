"""basis_cycle.py — the smallest normal-basis cycle: Spot long + USD-M short.

Composes the proven pieces without touching the network itself:
- consumes a basis_read.BasisReadRow (executable gaps: entry = USD-M bid minus
  Spot ask, exit = USD-M ask minus Spot bid, funding attached);
- plans both legs under one quote ceiling at 1x with reduce-only close;
- runs a deterministic lifecycle over injected leg outcomes.

Sizing discipline (the part money depends on):
- The spot entry intent carries quoteOrderQty = the plan's quote cap, so the
  exchange itself bounds the spend; the planned base quantity is an estimate.
- Every later leg is sized from ACQUIRED EXCHANGE TRUTH: the executor confirms
  the spot fill, deducts base-asset fees, floors to the shared step, and hands
  that quantity in. This module refuses to emit a dependent intent without it —
  selling the full planned quantity would overdraw the wallet by the fee.
- The exit closes the leveraged short FIRST (reduce-only), then the spot, so
  the worst frozen residual is unlevered spot inventory, never a naked short.

This module never submits, signs, or fetches. A later, owner-ordered executor
feeds it live payloads and carries out its intents through the one order
module. Verdicts: CLOSED | OPEN_EXPOSURE | REJECTED_BEFORE_ENTRY | UNKNOWN.
UNKNOWN stays UNKNOWN: an unconfirmed leg freezes the cycle with both possible
states named; there are no blind retries and no repair trades here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional

from basis_read import BasisReadRow, build_basis_read_row

try:
    from limits import PER_LEG_CAP_USDT as MAX_CYCLE_QUOTE
except ImportError:
    # TODO: remove this fallback after the sibling src/limits.py lane merges.
    MAX_CYCLE_QUOTE = Decimal("32")

CYCLE_PREFIX = "bc-"          # disjoint from spot (…b/…s) and l2fs- lanes
FEE_BUFFER = Decimal("0.002")     # spot base-fee headroom; net must cover qty
TARGET_LEVERAGE = 1               # owner law caps at 2x; the cycle runs 1x
QTY_QUANTUM = Decimal("0.00000001")

ENTER_SPOT = "ENTER_SPOT"
ENTER_SHORT = "ENTER_SHORT"
EXIT_SHORT = "EXIT_SHORT"
EXIT_SPOT = "EXIT_SPOT"
UNWIND_SPOT = "UNWIND_SPOT"
DEPENDENT_STAGES = (ENTER_SHORT, EXIT_SHORT, EXIT_SPOT, UNWIND_SPOT)

FILLED = "FILLED"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"
LEG_OUTCOMES = (FILLED, REJECTED, UNKNOWN)


class BasisCycleError(Exception):
    """Raised on malformed inputs; never used for market outcomes."""


@dataclass(frozen=True)
class CyclePlan:
    symbol: str
    entry_gap_bps: Decimal
    exit_gap_bps: Decimal
    funding_rate: Decimal
    spot_quantity: Decimal        # plan-time estimate; never an order size
    short_quantity: Decimal       # plan-time estimate; never an order size
    spot_quote_cap: Decimal       # the actual quoteOrderQty bound
    short_notional: Decimal       # plan-time estimate
    leverage: int
    reason: str


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise BasisCycleError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def plan_cycle(
    row: BasisReadRow,
    min_entry_bps: Decimal,
    quantity_step: Decimal,
    min_notional: Decimal,
) -> Optional[CyclePlan]:
    """Return a two-leg plan, or None when the gap or size refuses it.

    Quantities here are feasibility estimates from the read-time book. The
    binding numbers are spot_quote_cap (exchange-enforced via quoteOrderQty)
    and, later, the executor-confirmed acquired base quantity.
    """
    for name, value in (("min_entry_bps", min_entry_bps),
                        ("quantity_step", quantity_step),
                        ("min_notional", min_notional)):
        if not isinstance(value, Decimal):
            raise BasisCycleError(f"{name} must be Decimal")
    if row.entry_gap_bps_vs_spot_ask < min_entry_bps:
        return None
    if row.spot_ask <= 0 or row.usdm_bid <= 0:
        raise BasisCycleError("prices must be positive to plan")
    # Fee-aware: the quote cap must buy the planned quantity PLUS the spot
    # base-asset fee, or the net acquired floors below one futures lot (this
    # exact failure stranded spot BNB live on 2026-08-13). A symbol whose one
    # lot plus fee headroom exceeds the cap is refused outright.
    usable_spot_quote = MAX_CYCLE_QUOTE / (Decimal(1) + FEE_BUFFER)
    spot_cap_qty = usable_spot_quote / row.spot_ask
    short_cap_qty = MAX_CYCLE_QUOTE / row.usdm_bid
    quantity = quantize_down(min(spot_cap_qty, short_cap_qty), quantity_step)
    if quantity <= 0:
        return None
    spot_quote = (quantity * row.spot_ask
                  * (Decimal(1) + FEE_BUFFER)).quantize(QTY_QUANTUM)
    short_notional = (quantity * row.usdm_bid).quantize(QTY_QUANTUM)
    if spot_quote < min_notional or short_notional < min_notional:
        return None
    return CyclePlan(
        symbol=row.symbol,
        entry_gap_bps=row.entry_gap_bps_vs_spot_ask,
        exit_gap_bps=row.exit_gap_bps_vs_spot_bid,
        funding_rate=row.funding_rate,
        spot_quantity=quantity,
        short_quantity=quantity,
        spot_quote_cap=spot_quote,
        short_notional=short_notional,
        leverage=TARGET_LEVERAGE,
        reason="entry gap {} bps >= min {} bps".format(
            row.entry_gap_bps_vs_spot_ask, min_entry_bps),
    )


def client_id(stage: str, run_id: str) -> str:
    return f"{CYCLE_PREFIX}{run_id}-{stage.lower()}"


def leg_intent(stage: str, plan: CyclePlan, run_id: str,
               quantity: Optional[Decimal] = None) -> Dict[str, str]:
    """One factual order intent; the executor owns transport and retries.

    ENTER_SPOT is quote-bounded and takes no quantity. Every other stage
    REQUIRES the executor-confirmed acquired base quantity (net of base fees,
    floored to the shared step) — the plan estimate is never an order size.
    """
    base = {"symbol": plan.symbol, "newClientOrderId": client_id(stage, run_id)}
    if stage == ENTER_SPOT:
        if quantity is not None:
            raise BasisCycleError("ENTER_SPOT is quote-bounded; no quantity")
        return {**base, "market": "spot", "side": "BUY", "type": "MARKET",
                "quoteOrderQty": str(plan.spot_quote_cap)}
    if stage not in DEPENDENT_STAGES:
        raise BasisCycleError(f"unknown stage {stage}")
    if not isinstance(quantity, Decimal) or quantity <= 0:
        raise BasisCycleError(
            f"{stage} requires the acquired exchange-truth quantity")
    if stage == ENTER_SHORT:
        return {**base, "market": "usdm", "side": "SELL", "type": "MARKET",
                "quantity": str(quantity)}
    if stage == EXIT_SHORT:
        return {**base, "market": "usdm", "side": "BUY", "type": "MARKET",
                "reduceOnly": "true", "quantity": str(quantity)}
    # UNWIND_SPOT and EXIT_SPOT: sell exactly what was acquired, never more.
    return {**base, "market": "spot", "side": "SELL", "type": "MARKET",
            "quantity": str(quantity)}


@dataclass
class CycleResult:
    verdict: str
    stages: List[Dict[str, str]]
    exposure: str
    note: str


def run_cycle(plan: CyclePlan, run_id: str, outcomes: Dict[str, str],
              acquired_base: Optional[Decimal] = None) -> CycleResult:
    """Drive the lifecycle from injected terminal leg outcomes.

    outcomes maps stage name -> FILLED | REJECTED | UNKNOWN, exactly as the
    executor confirmed them (after its own query-before-retry). acquired_base
    is the executor-confirmed net base from the spot fill; it is REQUIRED the
    moment ENTER_SPOT is FILLED and sizes every later leg. Order is fixed:
    spot long, short, then reduce-only short close, then spot sale. A failed
    short entry unwinds the spot leg through the ordered path. Any UNKNOWN
    freezes the cycle with both possible states named.
    """
    stages: List[Dict[str, str]] = []

    def outcome(stage: str, quantity: Optional[Decimal] = None) -> str:
        result = outcomes.get(stage)
        if result not in LEG_OUTCOMES:
            raise BasisCycleError(f"stage {stage} outcome missing or invalid")
        stages.append({"stage": stage, "outcome": result,
                       **leg_intent(stage, plan, run_id, quantity)})
        return result

    entry_spot = outcome(ENTER_SPOT)
    if entry_spot == REJECTED:
        return CycleResult("REJECTED_BEFORE_ENTRY", stages, "none",
                           "spot entry rejected; nothing held")
    if entry_spot == UNKNOWN:
        return CycleResult(
            "UNKNOWN", stages,
            f"spot BUY unconfirmed: spend exchange-bounded at "
            f"{plan.spot_quote_cap} USDT via quoteOrderQty; acquired base "
            f"unknown until {client_id(ENTER_SPOT, run_id)} is queried",
            "query the spot entry client id before anything else")
    if not isinstance(acquired_base, Decimal) or acquired_base <= 0:
        raise BasisCycleError(
            "acquired_base (exchange-truth net base, fee-deducted, "
            "step-floored) is required once the spot entry filled")

    held = f"{acquired_base} {plan.symbol}"
    entry_short = outcome(ENTER_SHORT, acquired_base)
    if entry_short == UNKNOWN:
        return CycleResult(
            "UNKNOWN", stages,
            f"holding spot {held}; short SELL unconfirmed — state is either "
            f"unhedged spot or the full pair",
            f"query {client_id(ENTER_SHORT, run_id)}; no unwind until confirmed")
    if entry_short == REJECTED:
        unwind = outcome(UNWIND_SPOT, acquired_base)
        if unwind == FILLED:
            return CycleResult(
                "REJECTED_BEFORE_ENTRY", stages, "none",
                f"short rejected; spot {held} unwound through the ordered path")
        if unwind == UNKNOWN:
            return CycleResult(
                "UNKNOWN", stages,
                f"spot unwind unconfirmed: holding either spot {held} or nothing",
                f"query {client_id(UNWIND_SPOT, run_id)}")
        return CycleResult(
            "OPEN_EXPOSURE", stages,
            f"holding spot {held}, no hedge",
            "unwind rejected; exposure stands, executor reports and stops")

    exit_short = outcome(EXIT_SHORT, acquired_base)
    if exit_short == REJECTED:
        return CycleResult(
            "OPEN_EXPOSURE", stages,
            f"hedged pair standing: spot {held} + short {held}",
            "reduce-only close rejected; pair intact, executor reports and stops")
    if exit_short == UNKNOWN:
        return CycleResult(
            "UNKNOWN", stages,
            f"short close unconfirmed: state is either the intact pair or "
            f"unhedged spot ({held} each leg)",
            f"query {client_id(EXIT_SHORT, run_id)}")

    exit_spot = outcome(EXIT_SPOT, acquired_base)
    if exit_spot == FILLED:
        return CycleResult("CLOSED", stages, "none",
                           "both legs entered and exited; short closed reduce-only first")
    if exit_spot == UNKNOWN:
        return CycleResult(
            "UNKNOWN", stages,
            f"spot SELL unconfirmed: either flat or holding spot {held}",
            f"query {client_id(EXIT_SPOT, run_id)}")
    return CycleResult(
        "OPEN_EXPOSURE", stages,
        f"holding unlevered spot {held}; short already closed",
        "spot sale rejected; executor reports and stops")


def main(argv: Optional[List[str]] = None) -> int:
    """Offline fixture planner: three payload files in, one factual plan out."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("symbol")
    parser.add_argument("spot_book_json")
    parser.add_argument("usdm_book_json")
    parser.add_argument("usdm_premium_json")
    parser.add_argument("--min-entry-bps", default="1")
    parser.add_argument("--quantity-step", default="0.00001")
    parser.add_argument("--min-notional", default="5")
    args = parser.parse_args(argv)

    payloads = []
    for path in (args.spot_book_json, args.usdm_book_json, args.usdm_premium_json):
        with open(path, "r", encoding="utf-8") as handle:
            payloads.append(json.load(handle))
    row = build_basis_read_row(args.symbol, payloads[0], payloads[1], payloads[2],
                               completed_utc="offline-fixture")
    plan = plan_cycle(row, Decimal(args.min_entry_bps),
                      Decimal(args.quantity_step), Decimal(args.min_notional))
    if plan is None:
        print(f"REFUSED | {args.symbol} | entry gap {row.entry_gap_bps_vs_spot_ask} bps "
              f"below min {args.min_entry_bps} bps or size below minimum")
        return 1
    print(f"PLAN | {plan.symbol} | est qty {plan.spot_quantity} | spot spend cap "
          f"{plan.spot_quote_cap} USDT (quoteOrderQty) | est short notional "
          f"{plan.short_notional} USDT | leverage {plan.leverage}x | {plan.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
