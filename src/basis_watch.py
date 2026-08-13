"""basis_watch.py — continuous NORMAL-basis candidate observer (scanner area).

One pass = one engine tick of the old scanner, ported per
tests/fixtures/old_engine_spec.json (scanner area, plus the math-area unit
conventions).  Per pass:

1. Cheap all-universe public pass reusing basis_scan's machinery (five
   unsigned GETs: two exchangeInfo, two all-symbol bookTickers, one
   all-symbol premiumIndex).
2. Finalists: cap-fitting rows with the best raw entry gaps (bounded by
   --finalists), plus every symbol already WATCHING/FOCUS/PROPOSED so
   demotion stays honest.  COOLDOWN symbols are wholly skipped (spec rule
   cooldown_and_candidate_ttl: no decisions at all while cooling).
3. Per finalist, four more public GETs: Spot depth /api/v3/depth and USD-M
   depth /fapi/v1/depth (limit 50), funding history /fapi/v1/fundingRate
   (flip-risk and per-symbol settlement-period input), and the per-symbol
   premiumIndex.  Ingestion applies
   the spec's book/ladder validity and future-timestamp-skew guards
   (ingest_book_validity, depth_ladder_ingestion, future_timestamp_guard):
   invalid or future-skewed data CLEARS the leg to None — it is never
   priced.  Evaluate-time staleness stays the kernel's data_stale guard,
   because a REST pass carries its source timestamps to the kernel intact.
4. The B1 kernel evaluates the observation.  basis_kernel's real surface is
   typed — evaluate(BasisSnapshot, KernelConfig) returning {"accept",
   "reason", "components"} — and _kernel_decision adapts each observation to
   it (per-symbol commission truth folded into the KernelConfig) and maps
   the result back to kind ignore|watch|focus|proposal|reject plus the
   frozen reject vocabulary and numeric evidence.  A missing or crashing
   kernel yields the fail-closed kind "unknown": nothing promotes, nothing
   demotes, nothing is invented (lifecycle-area unknown_blocks).  The
   observer passes kill_switch_on=False: it holds no credentials and places
   no orders, so the kill switch's see-but-cannot-arm gate stays with the
   executor seat while proposal-grade candidates are still recorded.
5. Decisions fold into per-symbol candidate states with the spec's
   promotion/demotion hysteresis (scanner state_machine.symbol_lifecycle):
   IDLE/WATCHING/FOCUS/PROPOSED/REJECTED/COOLDOWN; focus soft/hard capacity
   with score-based eviction (focus_capacity_hysteresis); a reject from an
   advanced state (FOCUS/PROPOSED) cools the symbol for cooldownMs while a
   shallow reject dwells 2 ticks; WATCHING expires after candidateTtlMs;
   FOCUS sessions expire after focusMaxAgeMs back to WATCHING.

UNITS (math-area rule unit_conventions, tested explicitly): gaps, fees,
slippage, carry and net edge are PERCENT (0.78 means 0.78%); spreads and
mark divergence are BPS (x10000); funding rates are per-period FRACTIONS
(0.0001 = 1 bp per settlement, converted to percent only inside carry).

STATE: ONE atomic JSON file (default RECEIPT_DIR/watch/basis_watch_state.json)
holding per-symbol candidate state — lifecycle state, persistence counter,
gap-sample window, session timestamps — plus a pass watermark.  Written
with temp file + fsync + os.replace after every pass and reloaded on
start, so a kill at any moment loses at most the in-flight pass.

PROPOSALS: a positive-net kernel proposal is published once under
RECEIPT_DIR/proposals/proposal-<id>.json.  Each file is a complete, atomic,
120-second-by-default market authority, chmod 0400 after publication, and is
never updated or removed by this observer.  A live proposal for the same
symbol suppresses duplicates.

COMMISSION: RECEIPT_DIR/commission-rates.json (written by the lifecycle
tool from the signed commission endpoints) is read when present; maker and
taker rates are accepted in the lifecycle writer's top-level FRACTION shape
({"symbol": ..., "spot": {...}, "usdm": {...}}), or as legacy percent-per-leg
and Binance-native shapes.  Fraction rates are converted x100 to percent.
When the file is absent or unusable, explicit ESTIMATED defaults are used.

NOT ENFORCED HERE: the universe 24h-volume floor (universe_filter,
minQuoteVolume24hUsd 3,000,000) — the dispatch's cheap pass reuses
basis_scan's five endpoints, none of which carries 24h volume; the
predicate is still provided (universe_eligible) for callers that have it.
The causality sub-machine is evidence-only and deliberately not ported
(spec scanner notes: every decision fixture reproduces without it).

Decimal-only money math; None is the fail-closed null everywhere.  Stdlib
only.  Live GETs happen only inside LiveReads; tests inject reads.

CLI: --once (one pass, factual rows) or --loop SECONDS (repeat, honoring a
STOP file: the loop exits cleanly when the stop path exists).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field, fields as dataclass_fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, localcontext
from math import gcd
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from basis_scan import (
    CAP_USDT,
    FEE_BUFFER,
    SPOT_BASE,
    USDM_BASE,
    ScanError,
    book_map,
    build_rows,
    dec,
    fetch_json,
    funding_map,
    spot_universe,
    usdm_universe,
)

try:
    import basis_kernel  # B1 surface: basis_kernel.evaluate(observation) -> decision
except ImportError:  # B1 not landed yet — observer stays honest, nothing promotes
    basis_kernel = None  # type: ignore[assignment]

RECEIPT_DIR = os.path.expanduser(os.environ.get("RECEIPT_DIR", "~/quant_receipts"))
WATCH_STATE_DIR_NAME = "watch"
STATE_FILE_NAME = "basis_watch_state.json"
COMMISSION_FILE_NAME = "commission-rates.json"
PROPOSAL_DIR_NAME = "proposals"
STATE_VERSION = 1
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,32}$")  # spec universe_filter symbol regex
MAX_MARKET_NUMBER = Decimal("1e15")   # spec ingest_book_validity max_market_number
MAX_DEPTH_LEVELS = 5000               # spec depth_ladder_ingestion
DEPTH_LIMIT = 50                      # dispatch: /api/v3/depth, /fapi/v1/depth limit 50
FUNDING_HISTORY_LIMIT = 32            # spec funding_ingestion_and_history keep bound
FUNDING_PERIOD_MIN_ROWS = 3           # two consecutive spacings are the minimum
FUNDING_PERIOD_JITTER_MS = 60_000     # exchange stamps may drift slightly
PREC = 50

STATES = ("IDLE", "WATCHING", "FOCUS", "PROPOSED", "REJECTED", "COOLDOWN")
ADVANCED_STATES = ("FOCUS", "PROPOSED")
DECISION_KINDS = ("ignore", "watch", "focus", "proposal", "reject")
PROPOSAL_FIELDS = frozenset({
    "proposal_id", "created_utc", "expires_utc", "quant_sha",
    "config_digest", "symbol", "direction", "spot_ask", "perp_bid",
    "entry_gap_pct", "spot_bid", "perp_ask", "exit_gap_pct",
    "spot_maker_commission_pct", "spot_taker_commission_pct",
    "perp_maker_commission_pct", "perp_taker_commission_pct",
    "commission_source", "entry_slippage_pct", "exit_slippage_pct",
    "funding_rate", "funding_period_ms", "carry_pct",
    "safety_buffer_pct", "net_edge_pct", "quantity",
})

# Scanner-area defaults, spelled exactly as the spec's defaults (KEEP/ADAPT
# rules).  Percent values are PERCENT, spreads BPS, funding FRACTIONS.
DEFAULTS: Dict[str, Any] = {
    "watch_gap_pct": Decimal("0.15"),
    "focus_gap_pct": Decimal("0.3"),
    "persist_ticks": 5,
    "tick_ms": 2000,
    "gap_window_size": 60,
    "max_gap_cv": Decimal("0.6"),
    "stale_ms": 5000,
    "funding_stale_ms": 10000,
    "max_leg_skew_ms": 2500,
    "max_future_skew_ms": 2000,
    "max_spread_bps": Decimal("25"),
    "min_depth_score": Decimal("0.3"),
    "min_net_basis_pct": Decimal("0.25"),
    "max_funding_drag_pct": Decimal("0.15"),
    "min_data_quality": Decimal("0.4"),
    "cooldown_ms": 600000,
    "candidate_ttl_ms": 300000,
    "focus_max_age_ms": 900000,
    "focus_soft_cap": 10,
    "focus_hard_cap": 25,
    "priority_momentum_weight": Decimal("0.25"),
    "min_quote_volume_24h_usd": Decimal("3000000"),
    # spec REJECTs the old toy suggestedNotionalUsd; the intended notional is
    # an input, defaulting to this program's real compiled 10 USDT per-leg cap.
    "notional_usd": CAP_USDT,
    "proposal_ttl_seconds": 120,
    "spot_maker_fee_pct": Decimal("0.1"),
    "spot_taker_fee_pct": Decimal("0.1"),
    "perp_maker_fee_pct": Decimal("0.02"),
    "perp_taker_fee_pct": Decimal("0.05"),
    "finalist_cap": 5,
}


class WatchError(Exception):
    pass


def watch_config(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Spec defaults merged with overrides, then validated (fail-closed)."""
    cfg = dict(DEFAULTS)
    cfg.update(overrides or {})
    validate_config(cfg)
    return cfg


def validate_config(cfg: Dict[str, Any]) -> None:
    """config_validation_invariants (KEEP): cheap checks, each blocks a footgun."""
    if not Decimal(0) < cfg["watch_gap_pct"] <= cfg["focus_gap_pct"]:
        raise WatchError("config: 0 < watch_gap_pct <= focus_gap_pct required")
    if cfg["gap_window_size"] < 3:
        raise WatchError("config: gap_window_size >= 3 required")
    if cfg["persist_ticks"] < 1:
        raise WatchError("config: persist_ticks >= 1 required")
    if not 0 < cfg["focus_soft_cap"] <= cfg["focus_hard_cap"]:
        raise WatchError("config: 0 < focus_soft_cap <= focus_hard_cap required")
    for key in ("min_net_basis_pct", "max_funding_drag_pct", "notional_usd"):
        if cfg[key] <= 0:
            raise WatchError("config: %s must be > 0" % key)
    ttl = dec(cfg.get("proposal_ttl_seconds"))
    if ttl is None or ttl <= 0:
        raise WatchError("config: proposal_ttl_seconds must be > 0")
    for key in ("spot_maker_fee_pct", "spot_taker_fee_pct",
                "perp_maker_fee_pct", "perp_taker_fee_pct"):
        rate = dec(cfg.get(key))
        if rate is None or rate < 0:
            raise WatchError("config: %s must be >= 0" % key)
    for key in ("stale_ms", "funding_stale_ms", "max_leg_skew_ms",
                "max_future_skew_ms", "cooldown_ms", "candidate_ttl_ms",
                "focus_max_age_ms", "tick_ms"):
        if cfg[key] <= 0:
            raise WatchError("config: %s must be > 0" % key)


# ---------------------------------------------------------------------------
# Pure scanner-area math (Decimal, None = fail-closed null).


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def return_pct(sell_proceeds_price: Optional[Decimal],
               long_entry_price: Optional[Decimal]) -> Optional[Decimal]:
    """executable_return_law: (sell - buy) / buy * 100; null unless both > 0.

    The denominator is ALWAYS the price of the leg bought at entry — one law
    owns both direction constructions (contract
    basis-executable-long-leg-notional-v1).  PERCENT out.
    """
    if sell_proceeds_price is None or long_entry_price is None:
        return None
    if sell_proceeds_price <= 0 or long_entry_price <= 0:
        return None
    with localcontext() as context:
        context.prec = PREC
        return ((sell_proceeds_price - long_entry_price)
                / long_entry_price * 100)


def entry_gap_pct(spot_ask: Optional[Decimal],
                  perp_bid: Optional[Decimal]) -> Optional[Decimal]:
    """executable_gap_law NORMAL: (perp_bid - spot_ask) / spot_ask * 100."""
    return return_pct(perp_bid, spot_ask)


def spread_bps(bid: Optional[Decimal], ask: Optional[Decimal]) -> Optional[Decimal]:
    """spread_bps rule: (ask - bid) / mid * 10000; null on bad or crossed book."""
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    with localcontext() as context:
        context.prec = PREC
        mid = (bid + ask) / 2
        return (ask - bid) / mid * Decimal(10000)


def slope_pct_per_min(samples: List[Tuple[int, Decimal]]) -> Optional[Decimal]:
    """slope_pct_per_min rule: least-squares fit, x in minutes since first ts.

    None with < 2 samples or a degenerate denominator (< 1e-12).
    Positive = widening, negative = closing.
    """
    if len(samples) < 2:
        return None
    with localcontext() as context:
        context.prec = PREC
        t0 = samples[0][0]
        xs = [Decimal(ts - t0) / Decimal(60000) for ts, _ in samples]
        ys = [value for _, value in samples]
        n = Decimal(len(samples))
        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sum_xx = sum(x * x for x in xs)
        denominator = n * sum_xx - sum_x * sum_x
        if abs(denominator) < Decimal("1e-12"):
            return None
        return (n * sum_xy - sum_x * sum_y) / denominator


def stability_stats(values: List[Decimal]
                    ) -> Tuple[Optional[Decimal], Optional[Decimal],
                               Optional[Decimal]]:
    """gap_stability_stats: (mean, sample stddev n-1, cv).

    stddev None under 2 samples; cv = stddev / max(|mean|, 1e-9).
    """
    if not values:
        return None, None, None
    with localcontext() as context:
        context.prec = PREC
        mean = sum(values) / Decimal(len(values))
        if len(values) < 2:
            return mean, None, None
        variance = sum((value - mean) ** 2 for value in values) \
            / Decimal(len(values) - 1)
        stddev = variance.sqrt()
        cv = stddev / max(abs(mean), Decimal("1e-9"))
        return mean, stddev, cv


def stability_cv(window: List[Tuple[int, Decimal]],
                 persist_required: int) -> Optional[Decimal]:
    """stability_cv rule: cv over ONLY the last max(2*persistTicks, 6) samples,
    None when fewer than 3 recent samples (all-window CV would punish a gap
    for having opened at all)."""
    recent = [value for _, value in window][-max(2 * persist_required, 6):]
    if len(recent) < 3:
        return None
    return stability_stats(recent)[2]


def depth_coverage_score(coverage: Optional[Decimal]) -> Optional[Decimal]:
    """depth_score curve: clamp(log10(1 + coverage) / log10(11), 0, 1).

    10x coverage saturates at exactly 1.0; 1x coverage is ~0.289 — below the
    0.3 default gate on purpose.
    """
    if coverage is None or coverage < 0:
        return None
    with localcontext() as context:
        context.prec = PREC
        score = (Decimal(1) + coverage).log10() / Decimal(11).log10()
        return clamp(score, Decimal(0), Decimal(1))


def leg_depth_score(levels: Optional[List[Tuple[Decimal, Decimal]]],
                    touch_px: Optional[Decimal], touch_qty: Optional[Decimal],
                    notional_usd: Decimal) -> Optional[Decimal]:
    """depth_score_at_size per leg: ladder visible USD, else touch product,
    else None; None when notional <= 0.  Unknown depth is not deep."""
    if notional_usd is None or notional_usd <= 0:
        return None
    with localcontext() as context:
        context.prec = PREC
        if levels:
            visible = sum(price * qty for price, qty in levels)
        elif touch_px is not None and touch_qty is not None \
                and touch_px > 0 and touch_qty > 0:
            visible = touch_px * touch_qty
        else:
            return None
        return depth_coverage_score(visible / notional_usd)


def pair_depth_score(spot_leg: Optional[Decimal],
                     perp_leg: Optional[Decimal]) -> Optional[Decimal]:
    """Two-leg depth = min(spot, perp); null if either leg is null."""
    if spot_leg is None or perp_leg is None:
        return None
    return min(spot_leg, perp_leg)


def dec_tanh(value: Decimal) -> Decimal:
    """tanh via exp for the momentum clamp (stdlib Decimal has no tanh)."""
    with localcontext() as context:
        context.prec = PREC
        if value > 40:
            return Decimal(1)
        if value < -40:
            return Decimal(-1)
        e2x = (2 * value).exp()
        return (e2x - 1) / (e2x + 1)


def priority_momentum(slope: Optional[Decimal], watch_gap_pct: Decimal,
                      weight: Decimal) -> Decimal:
    """priority_ranking momentum: clamp(1 + w*tanh(slope/watchGapPct), 0.5, 1.5).

    Unknown slope is neutral momentum 1.
    """
    if slope is None:
        return Decimal(1)
    with localcontext() as context:
        context.prec = PREC
        return clamp(Decimal(1) + weight * dec_tanh(slope / watch_gap_pct),
                     Decimal("0.5"), Decimal("1.5"))


def priority_score(net_pct: Optional[Decimal], persist_ticks: int,
                   persist_required: int, depth_score: Optional[Decimal],
                   carry_pct: Optional[Decimal],
                   capture_factor: Optional[Decimal],
                   dq_score: Optional[Decimal], slope: Optional[Decimal],
                   cfg: Dict[str, Any]) -> Optional[Decimal]:
    """priority_ranking (ADAPT, reproduced for parity): multiplicative score.

    None when net edge is unknown — an unrankable signal never outranks a
    ranked one (try_focus treats None as the weakest possible score).
    """
    if net_pct is None:
        return None
    with localcontext() as context:
        context.prec = PREC
        net_n = clamp(net_pct / (2 * cfg["min_net_basis_pct"]),
                      Decimal(0), Decimal("1.5"))
        persistence_n = max(
            clamp(Decimal(persist_ticks) / Decimal(max(persist_required, 1)),
                  Decimal(0), Decimal(1)), Decimal("0.1"))
        liquidity_n = max(clamp(depth_score if depth_score is not None
                                else Decimal(0), Decimal(0), Decimal(1)),
                          Decimal("0.05"))
        carry = carry_pct if carry_pct is not None else Decimal(0)
        capture = capture_factor if capture_factor is not None else Decimal(0)
        funding_usefulness = clamp(
            Decimal("0.5") + carry / (2 * cfg["max_funding_drag_pct"])
            + Decimal("0.25") * capture, Decimal(0), Decimal(1))
        dq = clamp(dq_score if dq_score is not None else Decimal(0),
                   Decimal(0), Decimal(1))
        momentum = priority_momentum(slope, cfg["watch_gap_pct"],
                                     cfg["priority_momentum_weight"])
        return (net_n * persistence_n * liquidity_n * funding_usefulness
                * dq * momentum)


def universe_eligible(has_spot: bool, has_perp: bool,
                      quote_volume_24h_usd: Optional[Decimal],
                      min_quote_volume_24h_usd: Decimal) -> bool:
    """universe_filter predicate (KEEP).  The cheap-pass endpoints carry no
    24h volume, so run_pass cannot apply the volume floor — callers with a
    volume source use this predicate; unknown volume fails closed."""
    return bool(has_spot and has_perp and quote_volume_24h_usd is not None
                and quote_volume_24h_usd.is_finite()
                and quote_volume_24h_usd >= min_quote_volume_24h_usd)


# ---------------------------------------------------------------------------
# Ingestion guards (spec: ingest_book_validity, depth_ladder_ingestion,
# future_timestamp_guard).  Invalid data CLEARS the observation to None.


def usable_market_number(value: Optional[Decimal]) -> bool:
    return value is not None and value.is_finite() \
        and 0 < value <= MAX_MARKET_NUMBER


def pos_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def accept_timestamp(now_ms: int, ts_ms: Optional[int],
                     max_future_skew_ms: int) -> bool:
    """future_timestamp_guard: a source timestamp more than maxFutureSkewMs in
    the future invalidates the observation.  Evaluate-time staleness is the
    kernel's data_stale guard, not an ingest clear (a REST pass hands the
    kernel its source timestamps intact)."""
    return ts_ms is not None and now_ms - ts_ms >= -max_future_skew_ms


def parse_ladder(payload: Any) -> Optional[Dict[str, List[Tuple[Decimal, Decimal]]]]:
    """One depth payload -> {'bids': [(px, qty)...], 'asks': [...]} or None.

    Rows must be finite positive pairs; bids non-increasing, asks
    non-decreasing, best bid <= best ask, both sides non-empty, bounded
    levels.  Anything else clears the ladder (never priced).
    """
    if not isinstance(payload, dict):
        return None
    parsed: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
    for side in ("bids", "asks"):
        rows = payload.get(side)
        if not isinstance(rows, list) or not rows \
                or len(rows) > MAX_DEPTH_LEVELS:
            return None
        levels: List[Tuple[Decimal, Decimal]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                return None
            price, qty = dec(row[0]), dec(row[1])
            if not usable_market_number(price) or not usable_market_number(qty):
                return None
            levels.append((price, qty))
        parsed[side] = levels
    bids, asks = parsed["bids"], parsed["asks"]
    if any(bids[i][0] < bids[i + 1][0] for i in range(len(bids) - 1)):
        return None
    if any(asks[i][0] > asks[i + 1][0] for i in range(len(asks) - 1)):
        return None
    if bids[0][0] > asks[0][0]:
        return None
    return parsed


def book_from_ladder(ladder: Optional[Dict[str, List[Tuple[Decimal, Decimal]]]],
                     ts_ms: Optional[int]) -> Optional[Dict[str, Any]]:
    """Executable touch book from a valid ladder; None when either is absent."""
    if ladder is None or ts_ms is None:
        return None
    return {
        "bid": ladder["bids"][0][0], "bid_qty": ladder["bids"][0][1],
        "ask": ladder["asks"][0][0], "ask_qty": ladder["asks"][0][1],
        "ts_ms": ts_ms,
    }


def parse_mark(payload: Any) -> Optional[Dict[str, Any]]:
    """premiumIndex payload -> mark observation or None (cleared).

    Sanity of |rate| and staleness are the kernel's funding-freshness gate;
    ingest requires structural validity only.
    """
    if not isinstance(payload, dict):
        return None
    mark_price = dec(payload.get("markPrice"))
    funding_rate = dec(payload.get("lastFundingRate"))
    next_funding_ts = pos_int(payload.get("nextFundingTime"))
    ts_ms = pos_int(payload.get("time"))
    if not usable_market_number(mark_price) or funding_rate is None \
            or next_funding_ts is None or ts_ms is None:
        return None
    return {"mark_price": mark_price, "funding_rate": funding_rate,
            "next_funding_ts_ms": next_funding_ts, "ts_ms": ts_ms}


def parse_funding_history(payload: Any) -> List[Dict[str, Any]]:
    """fundingRate history -> timestamped per-period FRACTION observations.

    Timestamped rows stay in exchange order and are bounded to the newest
    FUNDING_HISTORY_LIMIT.  A malformed rate is retained as None because its
    valid fundingTime still carries schedule evidence; it is omitted from the
    rate sequence passed to the kernel.
    """
    points: List[Dict[str, Any]] = []
    for row in payload if isinstance(payload, list) else []:
        if isinstance(row, dict):
            funding_time_ms = pos_int(row.get("fundingTime"))
            if funding_time_ms is not None:
                points.append({"funding_time_ms": funding_time_ms,
                               "funding_rate": dec(row.get("fundingRate"))})
    return points[-FUNDING_HISTORY_LIMIT:]


def infer_funding_period_ms(history: List[Dict[str, Any]]) -> Optional[int]:
    """Median consecutive fundingTime spacing, or None when not trustworthy."""
    if len(history) < FUNDING_PERIOD_MIN_ROWS:
        return None
    spacings = [current["funding_time_ms"] - previous["funding_time_ms"]
                for previous, current in zip(history, history[1:])]
    if any(spacing <= 0 for spacing in spacings):
        return None
    ordered = sorted(spacings)
    middle = len(ordered) // 2
    period_ms = ordered[middle] if len(ordered) % 2 else \
        (ordered[middle - 1] + ordered[middle]) // 2
    if period_ms <= FUNDING_PERIOD_JITTER_MS:
        return None
    if any(abs(spacing - period_ms) > FUNDING_PERIOD_JITTER_MS
           for spacing in spacings):
        return None
    return period_ms


# ---------------------------------------------------------------------------
# Commission truth (receipt file) with ESTIMATED spec-default fallback.


def _pct_field(record: Dict[str, Any], *keys: str) -> Optional[Decimal]:
    for key in keys:
        if key in record:
            value = dec(record[key])
            if value is not None and value >= 0:
                return value
    return None


def _fraction_fee_pct(record: Any, side: str) -> Optional[Decimal]:
    if not isinstance(record, dict):
        return None
    fraction = dec(record.get(side + "CommissionRate"))
    if fraction is None and isinstance(record.get("standardCommission"), dict):
        fraction = dec(record["standardCommission"].get(side))
    if fraction is None:
        fraction = dec(record.get(side))
    if fraction is None or fraction < 0:
        return None
    return fraction * 100


def _commission_from(record: Any) -> Optional[Dict[str, Optional[Decimal]]]:
    if not isinstance(record, dict):
        return None
    spot_taker = _pct_field(record, "spot_taker_fee_pct", "spotTakerFeePct")
    perp_taker = _pct_field(record, "perp_taker_fee_pct", "perpTakerFeePct")
    spot_maker = _pct_field(record, "spot_maker_fee_pct", "spotMakerFeePct")
    perp_maker = _pct_field(record, "perp_maker_fee_pct", "perpMakerFeePct")
    if spot_taker is None:
        spot_taker = _fraction_fee_pct(record.get("spot"), "taker")
    if perp_taker is None:
        perp_taker = _fraction_fee_pct(record.get("usdm"), "taker")
    if spot_maker is None:
        spot_maker = _fraction_fee_pct(record.get("spot"), "maker")
    if perp_maker is None:
        perp_maker = _fraction_fee_pct(record.get("usdm"), "maker")
    if spot_taker is None or perp_taker is None:
        return None
    return {
        "spot_maker_fee_pct": spot_maker,
        "spot_taker_fee_pct": spot_taker,
        "perp_maker_fee_pct": perp_maker,
        "perp_taker_fee_pct": perp_taker,
    }


def read_commission_rates(receipt_dir: str, symbol: str,
                          cfg: Dict[str, Any]) -> Dict[str, Any]:
    """RECEIPT_DIR/commission-rates.json when present, else spec defaults
    labeled ESTIMATED.  Any unusable file or record falls back to ESTIMATED
    — measured-looking numbers never come from a broken receipt."""
    estimated = {"spot_maker_fee_pct": cfg["spot_maker_fee_pct"],
                 "spot_taker_fee_pct": cfg["spot_taker_fee_pct"],
                 "perp_maker_fee_pct": cfg["perp_maker_fee_pct"],
                 "perp_taker_fee_pct": cfg["perp_taker_fee_pct"],
                 "source": "ESTIMATED",
                 "proposal_source": "ESTIMATED"}
    path = os.path.join(receipt_dir, COMMISSION_FILE_NAME)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return estimated
    if not isinstance(payload, dict):
        return estimated
    symbols = payload.get("symbols")
    candidates = [symbols.get(symbol) if isinstance(symbols, dict) else None,
                  payload.get(symbol),
                  payload if payload.get("symbol") in (None, symbol) else None,
                  payload.get("default")]
    for candidate in candidates:
        rates = _commission_from(candidate)
        if rates is not None:
            all_measured = rates["spot_maker_fee_pct"] is not None \
                and rates["perp_maker_fee_pct"] is not None
            return {
                "spot_maker_fee_pct": rates["spot_maker_fee_pct"]
                if rates["spot_maker_fee_pct"] is not None
                else cfg["spot_maker_fee_pct"],
                "spot_taker_fee_pct": rates["spot_taker_fee_pct"],
                "perp_maker_fee_pct": rates["perp_maker_fee_pct"]
                if rates["perp_maker_fee_pct"] is not None
                else cfg["perp_maker_fee_pct"],
                "perp_taker_fee_pct": rates["perp_taker_fee_pct"],
                "source": "receipt",
                "proposal_source": "measured" if all_measured
                else "ESTIMATED",
            }
    return estimated


# ---------------------------------------------------------------------------
# B1 kernel boundary.  The real basis_kernel surface is typed:
# evaluate(BasisSnapshot, KernelConfig) -> {"accept", "reason", "components"}.
# _kernel_decision adapts one observation to that surface and maps the result
# back to {"kind", "reject_code", ...evidence}.  A module without the typed
# surface (the offline test stub) is called with the observation dict as-is.


_DECISION_REASONS = ("ignore", "watch", "focus", "proposal")


def _canonical_json_value(value: Any) -> Any:
    """JSON-safe canonical form for a stable config SHA-256."""
    if isinstance(value, Decimal):
        if value == 0:
            return "0"
        return format(value.normalize(), "f")
    if isinstance(value, dict):
        return {str(key): _canonical_json_value(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise WatchError("kernel config contains an unsupported value")


def _stable_config_digest(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(_canonical_json_value(payload), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _typed_kernel_config(module: Any, observation: Dict[str, Any]) -> Any:
    """Build the exact per-symbol KernelConfig passed to the typed kernel."""
    cfg = observation["config"]
    fees = observation["fees"]
    config_kwargs: Dict[str, Any] = {}
    for spec_field in dataclass_fields(module.KernelConfig):
        if spec_field.name in ("spot_taker_fee_pct", "perp_taker_fee_pct",
                              "funding_period_ms"):
            continue
        if spec_field.name in cfg:
            config_kwargs[spec_field.name] = cfg[spec_field.name]
    funding_period_ms = observation.get("funding_period_ms")
    if funding_period_ms is not None:
        config_kwargs["funding_period_ms"] = funding_period_ms
    return module.KernelConfig(
        spot_taker_fee_pct=fees["spot_taker_fee_pct"],
        perp_taker_fee_pct=fees["perp_taker_fee_pct"], **config_kwargs)


def _kernel_decision(module: Any, observation: Dict[str, Any]) -> Any:
    config = _typed_kernel_config(module, observation)
    now_ms = observation["now_ms"]

    def to_book(book: Optional[Dict[str, Any]]) -> Any:
        if book is None:
            return None
        return module.Book(bid=book["bid"], ask=book["ask"],
                           bid_qty=book["bid_qty"], ask_qty=book["ask_qty"])

    spot, perp = observation["spot_book"], observation["perp_book"]
    spot_staleness = max(0, now_ms - spot["ts_ms"]) if spot else None
    perp_staleness = max(0, now_ms - perp["ts_ms"]) if perp else None
    leg_skew = abs(spot["ts_ms"] - perp["ts_ms"]) if spot and perp else None
    mark_obs = observation["mark"]
    mark = None
    mark_staleness = None
    if mark_obs is not None:
        mark = module.FundingMark(
            mark_price=mark_obs["mark_price"],
            funding_rate=mark_obs["funding_rate"],
            next_funding_ts=mark_obs["next_funding_ts_ms"],
            ts=mark_obs["ts_ms"])
        mark_staleness = now_ms - mark_obs["ts_ms"]
    snapshot = module.BasisSnapshot(
        now_ms=now_ms,
        spot_book=to_book(spot),
        perp_book=to_book(perp),
        spot_staleness_ms=spot_staleness,
        perp_staleness_ms=perp_staleness,
        leg_skew_ms=leg_skew,
        spot_ask_ladder=observation["spot_ladder"]["asks"]
        if observation["spot_ladder"] else None,
        perp_bid_ladder=observation["perp_ladder"]["bids"]
        if observation["perp_ladder"] else None,
        mark=mark,
        mark_staleness_ms=mark_staleness,
        funding_history=tuple(observation.get("funding_history") or ()),
        slope_pct_per_min=observation.get("slope_pct_per_min"),
        stability_cv=observation.get("stability_cv"),
        persist_ticks=observation.get("persist_ticks", 0),
        notional_usd=observation["notional_usd"],
        # The observer never arms anything: it holds no credentials, places no
        # orders, and only writes candidate facts to the state file.  The kill
        # switch's see-but-cannot-arm gate lives with the executor seat, so
        # proposal-grade candidates are recorded here instead of being
        # converted to kill_switch_on rejects (which would cool them down).
        kill_switch_on=False,
    )
    result = module.evaluate(snapshot, config)
    if not isinstance(result, dict):
        return result
    reason = result.get("reason")
    components = result.get("components")
    decision: Dict[str, Any] = dict(components) \
        if isinstance(components, dict) else {}
    if reason in _DECISION_REASONS:
        decision["kind"] = reason
        decision["reject_code"] = None
    elif isinstance(reason, str) and reason:
        decision["kind"] = "reject"
        decision["reject_code"] = reason
    decision["config_digest"] = _stable_config_digest({
        spec_field.name: getattr(config, spec_field.name)
        for spec_field in dataclass_fields(config)
    })
    return decision


def kernel_evaluate(observation: Dict[str, Any]) -> Dict[str, Any]:
    """One decision from the B1 kernel; fail-closed 'unknown' when the kernel
    is absent, crashes, or returns an unrecognizable decision.  'unknown'
    freezes the symbol: no promotion, no demotion, nothing invented."""
    module = basis_kernel
    if module is None or not hasattr(module, "evaluate"):
        return {"kind": "unknown", "reject_code": None,
                "note": "kernel_unavailable"}
    try:
        if hasattr(module, "BasisSnapshot") and hasattr(module, "KernelConfig"):
            decision = _kernel_decision(module, observation)
        else:
            decision = module.evaluate(observation)
            if isinstance(decision, dict):
                decision = dict(decision)
                effective_cfg = dict(observation.get("config") or {})
                fees = observation.get("fees") or {}
                effective_cfg["spot_taker_fee_pct"] = fees.get(
                    "spot_taker_fee_pct")
                effective_cfg["perp_taker_fee_pct"] = fees.get(
                    "perp_taker_fee_pct")
                effective_cfg["funding_period_ms"] = observation.get(
                    "funding_period_ms")
                decision["config_digest"] = _stable_config_digest(
                    effective_cfg)
    except Exception as exc:  # foreign-code boundary: never kills the observer
        return {"kind": "unknown", "reject_code": None,
                "note": "kernel_error:" + type(exc).__name__}
    if not isinstance(decision, dict) or decision.get("kind") not in DECISION_KINDS:
        return {"kind": "unknown", "reject_code": None,
                "note": "kernel_decision_invalid"}
    return decision


def decision_decimal(decision: Dict[str, Any], key: str) -> Optional[Decimal]:
    return dec(decision.get(key)) if decision.get(key) is not None else None


# ---------------------------------------------------------------------------
# Immutable proposal economics and sizing.


def ladder_walk_slippage_pct(
        levels: Optional[List[Tuple[Decimal, Decimal]]],
        notional_usd: Decimal) -> Optional[Decimal]:
    """Average-fill-vs-touch PERCENT using the kernel's ladder-walk law."""
    if not levels or notional_usd <= 0 or levels[0][0] <= 0:
        return None
    with localcontext() as context:
        context.prec = PREC
        touch = levels[0][0]
        remaining = notional_usd
        cost_usd = Decimal(0)
        filled_base = Decimal(0)
        for price, quantity in levels:
            if remaining <= 0:
                break
            take_usd = min(remaining, price * quantity)
            cost_usd += take_usd
            filled_base += take_usd / price
            remaining -= take_usd
        if remaining > 0 or filled_base <= 0:
            return None
        average_price = cost_usd / filled_base
        return abs(average_price - touch) / touch * 100


def _proposal_leg_slippage_pct(
        levels: Optional[List[Tuple[Decimal, Decimal]]],
        notional_usd: Decimal, cfg: Dict[str, Any]) -> Optional[Decimal]:
    """Ladder walk with the same fail-expensive thin-ladder penalty."""
    estimate = dec(cfg.get("slippage_pct_estimate", Decimal("0.05")))
    multiple = dec(cfg.get("thin_ladder_penalty_multiple", Decimal(4)))
    if estimate is None or estimate < 0 or multiple is None or multiple < 0:
        return None
    walked = ladder_walk_slippage_pct(levels, notional_usd)
    return walked if walked is not None else estimate * multiple


def exit_slippage_pct(observation: Dict[str, Any]) -> Optional[Decimal]:
    """NORMAL exit hits Spot bids and USD-M asks, never the entry sides."""
    spot_ladder = observation.get("spot_ladder")
    perp_ladder = observation.get("perp_ladder")
    notional = dec(observation.get("notional_usd"))
    if not isinstance(spot_ladder, dict) or not isinstance(perp_ladder, dict) \
            or notional is None or notional <= 0:
        return None
    spot = _proposal_leg_slippage_pct(
        spot_ladder.get("bids"), notional, observation["config"])
    perp = _proposal_leg_slippage_pct(
        perp_ladder.get("asks"), notional, observation["config"])
    if spot is None or perp is None:
        return None
    return spot + perp


def common_quantity_step(spot_step: Any, perp_step: Any) -> Optional[Decimal]:
    """Smallest positive decimal step aligned to both venue LOT_SIZE steps."""
    spot, perp = dec(spot_step), dec(perp_step)
    if spot is None or perp is None or spot <= 0 or perp <= 0:
        return None
    exponent = min(spot.as_tuple().exponent, perp.as_tuple().exponent)
    quantum = Decimal(1).scaleb(exponent)
    spot_units = int(spot / quantum)
    perp_units = int(perp / quantum)
    if spot_units <= 0 or perp_units <= 0:
        return None
    shared_units = spot_units // gcd(spot_units, perp_units) * perp_units
    return Decimal(shared_units) * quantum


def proposal_quantity(notional_usd: Any, spot_ask: Any, perp_bid: Any,
                      spot_step: Any, perp_step: Any) -> Optional[Decimal]:
    """Fee-aware equal-leg quantity, floored to both venues' lot rules."""
    notional = dec(notional_usd)
    spot_price, perp_price = dec(spot_ask), dec(perp_bid)
    step = common_quantity_step(spot_step, perp_step)
    if notional is None or spot_price is None or perp_price is None \
            or step is None or notional <= 0 or spot_price <= 0 \
            or perp_price <= 0:
        return None
    with localcontext() as context:
        context.prec = PREC
        spot_capacity = notional / (1 + FEE_BUFFER) / spot_price
        perp_capacity = notional / perp_price
        quantity = (min(spot_capacity, perp_capacity) / step).to_integral_value(
            rounding=ROUND_DOWN) * step
    return quantity if quantity > 0 else None


def _proposal_utc(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _proposal_decimal(value: Optional[Decimal]) -> Optional[str]:
    return format(value, "f") if value is not None and value.is_finite() else None


# ---------------------------------------------------------------------------
# Per-symbol candidate state (tracker + lifecycle).


@dataclass
class SymbolState:
    state: str = "IDLE"
    persist_ticks: int = 0
    opened_ms: Optional[int] = None
    window: List[Tuple[int, Decimal]] = field(default_factory=list)
    candidate_since_ms: Optional[int] = None
    focus_since_ms: Optional[int] = None
    cooldown_until_ms: Optional[int] = None
    rejected_at_ms: Optional[int] = None
    focus_score: Optional[Decimal] = None
    last_kind: Optional[str] = None
    last_reject: Optional[str] = None
    last_exit_reason: Optional[str] = None
    last_seen_ms: Optional[int] = None

    def gap_age_ms(self, now_ms: int) -> Optional[int]:
        return None if self.opened_ms is None else now_ms - self.opened_ms

    def in_cooldown(self, now_ms: int) -> bool:
        return self.state == "COOLDOWN" and self.cooldown_until_ms is not None \
            and now_ms < self.cooldown_until_ms


def observe_gap(state: SymbolState, now_ms: int, gap: Optional[Decimal],
                cfg: Dict[str, Any]) -> None:
    """persistence_counting + gap_sample_window: one observe per tick, BEFORE
    the kernel evaluates.  A missing gap adds no sample; missing or
    below-watch gaps reset persistence to 0 and age to null."""
    state.last_seen_ms = now_ms
    if gap is not None:
        state.window.append((now_ms, gap))
        del state.window[:-cfg["gap_window_size"]]
    if gap is None or gap < cfg["watch_gap_pct"]:
        state.persist_ticks = 0
        state.opened_ms = None
    else:
        state.persist_ticks += 1
        if state.opened_ms is None:
            state.opened_ms = now_ms


def _state_payload(state: SymbolState) -> Dict[str, Any]:
    return {
        "state": state.state,
        "persist_ticks": state.persist_ticks,
        "opened_ms": state.opened_ms,
        "window": [[ts, format(gap, "f")] for ts, gap in state.window],
        "candidate_since_ms": state.candidate_since_ms,
        "focus_since_ms": state.focus_since_ms,
        "cooldown_until_ms": state.cooldown_until_ms,
        "rejected_at_ms": state.rejected_at_ms,
        "focus_score": None if state.focus_score is None
        else format(state.focus_score, "f"),
        "last_kind": state.last_kind,
        "last_reject": state.last_reject,
        "last_exit_reason": state.last_exit_reason,
        "last_seen_ms": state.last_seen_ms,
    }


def _optional_int(record: Dict[str, Any], key: str, symbol: str) -> Optional[int]:
    value = record.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WatchError("state file has invalid %s for %s" % (key, symbol))
    return value


def _state_from_payload(symbol: str, record: Any) -> SymbolState:
    if not isinstance(record, dict) or record.get("state") not in STATES:
        raise WatchError("state file has invalid record for %s" % symbol)
    persist = record.get("persist_ticks")
    if isinstance(persist, bool) or not isinstance(persist, int) or persist < 0:
        raise WatchError("state file has invalid persist_ticks for %s" % symbol)
    window: List[Tuple[int, Decimal]] = []
    for row in record.get("window") or []:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise WatchError("state file has invalid window row for %s" % symbol)
        ts = row[0]
        gap = dec(row[1])
        if isinstance(ts, bool) or not isinstance(ts, int) or gap is None:
            raise WatchError("state file has invalid window row for %s" % symbol)
        window.append((ts, gap))
    score_raw = record.get("focus_score")
    score = dec(score_raw) if score_raw is not None else None
    if score_raw is not None and score is None:
        raise WatchError("state file has invalid focus_score for %s" % symbol)
    return SymbolState(
        state=record["state"],
        persist_ticks=persist,
        opened_ms=_optional_int(record, "opened_ms", symbol),
        window=window,
        candidate_since_ms=_optional_int(record, "candidate_since_ms", symbol),
        focus_since_ms=_optional_int(record, "focus_since_ms", symbol),
        cooldown_until_ms=_optional_int(record, "cooldown_until_ms", symbol),
        rejected_at_ms=_optional_int(record, "rejected_at_ms", symbol),
        focus_score=score,
        last_kind=record.get("last_kind"),
        last_reject=record.get("last_reject"),
        last_exit_reason=record.get("last_exit_reason"),
        last_seen_ms=_optional_int(record, "last_seen_ms", symbol),
    )


# ---------------------------------------------------------------------------
# Live transport (the only network site).  Tests inject a fake with the same
# two methods.


class LiveReads:
    """Unsigned public GETs only, exactly the dispatch's endpoints."""

    def __init__(self) -> None:
        self.now_ms = lambda: int(time.time() * 1000)

    def universe_rows(self) -> List[Dict[str, Any]]:
        spot_meta = spot_universe(fetch_json(
            SPOT_BASE + "/api/v3/exchangeInfo?symbolStatus=TRADING"
                        "&showPermissionSets=false"))
        fut_meta = usdm_universe(fetch_json(USDM_BASE + "/fapi/v1/exchangeInfo"))
        spot_books = book_map(fetch_json(SPOT_BASE + "/api/v3/ticker/bookTicker"))
        fut_books = book_map(fetch_json(USDM_BASE + "/fapi/v1/ticker/bookTicker"))
        funding = funding_map(fetch_json(USDM_BASE + "/fapi/v1/premiumIndex"))
        rows = build_rows(spot_meta, fut_meta, spot_books, fut_books, funding)
        # build_rows deliberately emits display economics only.  Proposals
        # additionally need both venue LOT_SIZE rules so one shared quantity
        # can be floored to a size accepted by each venue.
        for row in rows:
            symbol = row["symbol"]
            row["spot_step"] = spot_meta[symbol]["step"]
            row["perp_step"] = fut_meta[symbol]["step"]
        return rows

    def _get(self, url: str) -> Any:
        try:
            return fetch_json(url)
        except (ScanError, OSError, ValueError):
            return None  # a failed leg is a cleared leg, never a priced one

    def finalist(self, symbol: str) -> Dict[str, Any]:
        quoted = quote(symbol, safe="")
        spot_receipt = self.now_ms()
        spot_depth = self._get("%s/api/v3/depth?symbol=%s&limit=%d"
                               % (SPOT_BASE, quoted, DEPTH_LIMIT))
        perp_receipt = self.now_ms()
        perp_depth = self._get("%s/fapi/v1/depth?symbol=%s&limit=%d"
                               % (USDM_BASE, quoted, DEPTH_LIMIT))
        history = self._get("%s/fapi/v1/fundingRate?symbol=%s&limit=%d"
                            % (USDM_BASE, quoted, 8))
        premium_receipt = self.now_ms()
        premium = self._get("%s/fapi/v1/premiumIndex?symbol=%s"
                            % (USDM_BASE, quoted))
        return {"spot_depth": spot_depth, "spot_receipt_ms": spot_receipt,
                "perp_depth": perp_depth, "perp_receipt_ms": perp_receipt,
                "premium_receipt_ms": premium_receipt,
                "funding_history": history, "premium": premium}


# ---------------------------------------------------------------------------
# The observer engine.


class Watch:
    def __init__(self, state_path: str, receipt_dir: str = RECEIPT_DIR,
                 cfg: Optional[Dict[str, Any]] = None) -> None:
        self.cfg = cfg if cfg is not None else watch_config()
        validate_config(self.cfg)
        self.state_path = state_path
        self.receipt_dir = receipt_dir
        self.symbols: Dict[str, SymbolState] = {}
        self.pass_count = 0
        self.last_pass_ms: Optional[int] = None
        self.load_state()

    # -- state file ---------------------------------------------------------

    def load_state(self) -> None:
        """Reload the atomic state file; a missing file is a fresh start, a
        malformed file raises (fail closed — never silently discard truth)."""
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeDecodeError):
            raise WatchError("state file unreadable: %s" % self.state_path) \
                from None
        if not isinstance(payload, dict) \
                or payload.get("version") != STATE_VERSION \
                or not isinstance(payload.get("symbols"), dict) \
                or not isinstance(payload.get("watermark"), dict):
            raise WatchError("state file has invalid shape: %s" % self.state_path)
        watermark = payload["watermark"]
        pass_count = watermark.get("pass_count")
        if isinstance(pass_count, bool) or not isinstance(pass_count, int) \
                or pass_count < 0:
            raise WatchError("state file has invalid watermark")
        last_pass = watermark.get("last_pass_ms")
        if last_pass is not None and (isinstance(last_pass, bool)
                                      or not isinstance(last_pass, int)
                                      or last_pass < 0):
            raise WatchError("state file has invalid watermark")
        symbols = {}
        for symbol, record in payload["symbols"].items():
            symbols[str(symbol)] = _state_from_payload(str(symbol), record)
        self.symbols = symbols
        self.pass_count = pass_count
        self.last_pass_ms = last_pass

    def save_state(self) -> None:
        """Atomic write: temp file + fsync + os.replace.  A kill at any
        moment leaves the previous or the new complete file, never a tear."""
        payload = {
            "version": STATE_VERSION,
            "updated_utc": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"),
            "watermark": {"pass_count": self.pass_count,
                          "last_pass_ms": self.last_pass_ms},
            "symbols": {symbol: _state_payload(state)
                        for symbol, state in sorted(self.symbols.items())},
        }
        directory = os.path.dirname(self.state_path) or "."
        os.makedirs(directory, exist_ok=True)
        temp_path = self.state_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self.state_path)

    # -- immutable proposal files -----------------------------------------

    @property
    def proposal_dir(self) -> str:
        return os.path.join(self.receipt_dir, PROPOSAL_DIR_NAME)

    def _unexpired_proposal_id(self, symbol: str,
                               now_ms: int) -> Optional[str]:
        """Return an existing live proposal id without modifying any file."""
        try:
            names = sorted(os.listdir(self.proposal_dir))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WatchError("proposal directory unreadable: %s"
                             % self.proposal_dir) from exc
        now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
        for name in names:
            if not name.startswith("proposal-") or not name.endswith(".json"):
                continue
            path = os.path.join(self.proposal_dir, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    payload = json.load(handle)
                expires_text = payload.get("expires_utc") \
                    if isinstance(payload, dict) else None
                expires = datetime.fromisoformat(
                    expires_text.replace("Z", "+00:00")) \
                    if isinstance(expires_text, str) else None
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if payload.get("symbol") == symbol and expires is not None \
                    and expires > now:
                proposal_id = payload.get("proposal_id")
                return proposal_id if isinstance(proposal_id, str) else name[9:-5]
        return None

    def _proposal_payload(self, proposal_id: str, symbol: str, now_ms: int,
                          market_row: Dict[str, Any],
                          observation: Dict[str, Any],
                          decision: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        spot = observation.get("spot_book")
        perp = observation.get("perp_book")
        mark = observation.get("mark")
        if not isinstance(spot, dict) or not isinstance(perp, dict) \
                or not isinstance(mark, dict):
            return None
        entry_gap = entry_gap_pct(spot.get("ask"), perp.get("bid"))
        exit_gap = return_pct(perp.get("ask"), spot.get("bid"))
        entry_slippage = decision_decimal(decision, "slippage_pct")
        exit_slippage = exit_slippage_pct(observation)
        carry = decision_decimal(decision, "carry_pct")
        net_edge = decision_decimal(decision, "net_pct")
        quantity = proposal_quantity(
            observation.get("notional_usd"), spot.get("ask"), perp.get("bid"),
            market_row.get("spot_step"), market_row.get("perp_step"))
        digest = decision.get("config_digest")
        fees = observation.get("fees") or {}
        required_decimals = (
            spot.get("ask"), perp.get("bid"), entry_gap,
            spot.get("bid"), perp.get("ask"), exit_gap,
            fees.get("spot_maker_fee_pct"), fees.get("spot_taker_fee_pct"),
            fees.get("perp_maker_fee_pct"), fees.get("perp_taker_fee_pct"),
            entry_slippage, exit_slippage, mark.get("funding_rate"), carry,
            dec(self.cfg.get("min_net_basis_pct")), net_edge, quantity,
        )
        parsed = [dec(value) for value in required_decimals]
        if any(value is None for value in parsed) \
                or not isinstance(observation.get("funding_period_ms"), int) \
                or not isinstance(digest, str) \
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return None
        ttl = dec(self.cfg["proposal_ttl_seconds"])
        if ttl is None:
            return None
        created = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
        expires = created + timedelta(milliseconds=int(ttl * 1000))
        values = iter(parsed)
        payload = {
            "proposal_id": proposal_id,
            "created_utc": _proposal_utc(now_ms),
            "expires_utc": expires.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"),
            "quant_sha": os.environ.get("QUANT_SHA") or "UNCOMMITTED",
            "config_digest": digest,
            "symbol": symbol,
            "direction": observation.get("direction"),
            "spot_ask": _proposal_decimal(next(values)),
            "perp_bid": _proposal_decimal(next(values)),
            "entry_gap_pct": _proposal_decimal(next(values)),
            "spot_bid": _proposal_decimal(next(values)),
            "perp_ask": _proposal_decimal(next(values)),
            "exit_gap_pct": _proposal_decimal(next(values)),
            "spot_maker_commission_pct": _proposal_decimal(next(values)),
            "spot_taker_commission_pct": _proposal_decimal(next(values)),
            "perp_maker_commission_pct": _proposal_decimal(next(values)),
            "perp_taker_commission_pct": _proposal_decimal(next(values)),
            "commission_source": fees.get("proposal_source"),
            "entry_slippage_pct": _proposal_decimal(next(values)),
            "exit_slippage_pct": _proposal_decimal(next(values)),
            "funding_rate": _proposal_decimal(next(values)),
            "funding_period_ms": observation["funding_period_ms"],
            "carry_pct": _proposal_decimal(next(values)),
            # min_net_basis_pct is the kernel's configured positive safety
            # threshold: net edge had to clear this buffer to become proposal.
            "safety_buffer_pct": _proposal_decimal(next(values)),
            "net_edge_pct": _proposal_decimal(next(values)),
            "quantity": _proposal_decimal(next(values)),
        }
        if payload["direction"] != "long_spot_short_perp" \
                or payload["commission_source"] not in ("measured", "ESTIMATED") \
                or set(payload) != PROPOSAL_FIELDS:
            return None
        return payload

    def _write_proposal(self, payload: Dict[str, Any]) -> str:
        """Atomically publish one new file and make it owner-read-only."""
        net_edge = dec(payload.get("net_edge_pct"))
        if set(payload) != PROPOSAL_FIELDS or net_edge is None or net_edge <= 0:
            raise WatchError("proposal refuses incomplete or non-positive net")
        os.makedirs(self.proposal_dir, mode=0o700, exist_ok=True)
        proposal_id = payload["proposal_id"]
        path = os.path.join(self.proposal_dir,
                            "proposal-%s.json" % proposal_id)
        if os.path.exists(path):
            raise WatchError("proposal id collision: %s" % proposal_id)
        temp_path = "%s.tmp-%s-%s" % (
            path, os.getpid(), secrets.token_hex(2))
        try:
            descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, indent=1)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            # The mode travels with the inode through rename; the second chmod
            # makes the post-publish invariant explicit and independently true.
            os.chmod(temp_path, 0o400)
            os.replace(temp_path, path)
            os.chmod(path, 0o400)
            directory_fd = os.open(self.proposal_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise WatchError("proposal write failed: %s" % type(exc).__name__) \
                from exc
        return path

    def maybe_write_proposal(self, symbol: str, now_ms: int,
                             market_row: Dict[str, Any],
                             observation: Dict[str, Any],
                             decision: Dict[str, Any]
                             ) -> Tuple[Optional[str], str]:
        if decision.get("kind") != "proposal":
            return None, "not_applicable"
        net_edge = decision_decimal(decision, "net_pct")
        if net_edge is None or net_edge <= 0:
            return None, "refused_non_positive_net_edge"
        if self._unexpired_proposal_id(symbol, now_ms) is not None:
            return None, "duplicate_unexpired"
        for _attempt in range(8):
            proposal_id = format(now_ms // 1000, "x") + secrets.token_hex(3)
            path = os.path.join(self.proposal_dir,
                                "proposal-%s.json" % proposal_id)
            if not os.path.exists(path):
                break
        else:
            raise WatchError("could not allocate a unique proposal id")
        payload = self._proposal_payload(
            proposal_id, symbol, now_ms, market_row, observation, decision)
        if payload is None:
            return None, "refused_incomplete_proposal"
        self._write_proposal(payload)
        return proposal_id, "written"

    # -- focus capacity hysteresis (spec focus_capacity_hysteresis) ---------

    def _focus_pool(self, exclude: str) -> List[str]:
        return [symbol for symbol, state in self.symbols.items()
                if state.state in ADVANCED_STATES and symbol != exclude]

    def try_focus(self, symbol: str, score: Optional[Decimal]) -> bool:
        """Admission under soft/hard caps with score-based eviction; the hard
        cap is never exceeded.  Returns True when the symbol may hold a
        focus slot (already-advanced symbols just rescore and stay)."""
        state = self.symbols[symbol]
        state.focus_score = score
        if state.state in ADVANCED_STATES:
            return True
        pool = self._focus_pool(symbol)
        if len(pool) < self.cfg["focus_soft_cap"]:
            return True
        score_value = score if score is not None else Decimal(0)
        weakest = min(pool, key=lambda name: (
            self.symbols[name].focus_score
            if self.symbols[name].focus_score is not None else Decimal(0)))
        weakest_score = self.symbols[weakest].focus_score
        weakest_value = weakest_score if weakest_score is not None else Decimal(0)
        if len(pool) < self.cfg["focus_hard_cap"]:
            return score_value > weakest_value  # pool may grow, NO eviction
        if score_value > weakest_value:
            evicted = self.symbols[weakest]
            evicted.state = "WATCHING"
            evicted.focus_since_ms = None
            evicted.last_exit_reason = "capacity_evicted"
            return True
        return False

    # -- decision fold (spec signal_state_from_decision) --------------------

    def apply_decision(self, symbol: str, kind: str,
                       reject_code: Optional[str], score: Optional[Decimal],
                       now_ms: int) -> None:
        state = self.symbols[symbol]
        state.last_kind = kind
        state.last_reject = reject_code if kind == "reject" else None
        if kind == "unknown":
            return  # unknown_blocks: freeze — no promotion, no demotion
        was_advanced = state.state in ADVANCED_STATES
        if kind == "ignore":
            if was_advanced:
                state.last_exit_reason = "signal_expired"
            state.state = "IDLE"
            state.candidate_since_ms = None
            state.focus_since_ms = None
            state.focus_score = None
        elif kind == "watch":
            if was_advanced:
                state.last_exit_reason = "signal_expired"
                state.focus_since_ms = None
            if state.candidate_since_ms is None:
                state.candidate_since_ms = now_ms
            state.state = "WATCHING"
        elif kind == "focus":
            if state.candidate_since_ms is None:
                state.candidate_since_ms = now_ms
            if self.try_focus(symbol, score):
                if state.focus_since_ms is None:
                    state.focus_since_ms = now_ms
                state.state = "FOCUS"
            else:
                state.state = "WATCHING"  # capacity denied stays a candidate
        elif kind == "proposal":
            if state.candidate_since_ms is None:
                state.candidate_since_ms = now_ms
            if self.try_focus(symbol, score) and state.focus_since_ms is None:
                state.focus_since_ms = now_ms
            state.state = "PROPOSED"
        elif kind == "reject":
            state.candidate_since_ms = None
            state.focus_since_ms = None
            state.focus_score = None
            if was_advanced:
                # advanced signal died with a named reason -> cooldown
                state.last_exit_reason = "cooldown"
                state.state = "COOLDOWN"
                state.cooldown_until_ms = now_ms + self.cfg["cooldown_ms"]
            else:
                state.state = "REJECTED"
                state.rejected_at_ms = now_ms

    def maintain(self, now_ms: int) -> None:
        """cooldown_and_candidate_ttl + focus_max_age, after the tick's
        decisions (spec tick_determinism: sweep then maintain)."""
        cfg = self.cfg
        for symbol in list(self.symbols):
            state = self.symbols[symbol]
            if state.state == "COOLDOWN" and state.cooldown_until_ms is not None \
                    and now_ms >= state.cooldown_until_ms:
                state.state = "IDLE"
                state.cooldown_until_ms = None
                state.last_exit_reason = "cooldown_expired"
            elif state.state == "REJECTED" and state.rejected_at_ms is not None \
                    and now_ms - state.rejected_at_ms > 2 * cfg["tick_ms"]:
                state.state = "IDLE"
                state.rejected_at_ms = None
            elif state.state == "WATCHING" \
                    and state.candidate_since_ms is not None \
                    and now_ms - state.candidate_since_ms > cfg["candidate_ttl_ms"]:
                state.state = "IDLE"
                state.candidate_since_ms = None
                state.last_exit_reason = "candidate_ttl"
            elif state.state == "FOCUS" and state.focus_since_ms is not None \
                    and now_ms - state.focus_since_ms > cfg["focus_max_age_ms"]:
                state.state = "WATCHING"
                state.focus_since_ms = None
                state.last_exit_reason = "max_focus_time"
            state = self.symbols[symbol]
            if state.state == "IDLE" and state.persist_ticks == 0 \
                    and state.last_seen_ms is not None \
                    and now_ms - state.last_seen_ms > cfg["candidate_ttl_ms"]:
                del self.symbols[symbol]  # tracker_memory_bounds (bounded state)

    def active_cooldown_count(self, now_ms: int) -> int:
        return sum(1 for state in self.symbols.values()
                   if state.in_cooldown(now_ms))

    # -- observation & pass -------------------------------------------------

    def _observation(self, symbol: str, now_ms: int,
                     bundle: Dict[str, Any],
                     fees: Dict[str, Any]) -> Dict[str, Any]:
        """future_timestamp_guard is receipt-anchored (spec: a source event
        time must be <= receipt_ts + maxFutureSkewMs): each payload's stamp
        is judged against ITS OWN receipt clock, so slow earlier fetches in
        the same pass cannot make honest data look future-skewed.  Staleness
        against the evaluation clock stays the kernel's data_stale guard."""
        cfg = self.cfg
        skew = cfg["max_future_skew_ms"]
        spot_ladder = parse_ladder(bundle.get("spot_depth"))
        spot_ts = pos_int(bundle.get("spot_receipt_ms"))
        if spot_ts is None:
            spot_ts = now_ms  # Spot depth carries no source stamp: receipt only
        perp_receipt = pos_int(bundle.get("perp_receipt_ms"))
        if perp_receipt is None:
            perp_receipt = now_ms
        perp_payload = bundle.get("perp_depth")
        perp_ladder = parse_ladder(perp_payload)
        perp_ts = None
        if isinstance(perp_payload, dict):
            perp_ts = pos_int(perp_payload.get("T")) \
                or pos_int(perp_payload.get("E"))
        if perp_ts is None:
            perp_ts = perp_receipt
        if not accept_timestamp(perp_receipt, perp_ts, skew):
            perp_ladder, perp_ts = None, None
        premium_receipt = pos_int(bundle.get("premium_receipt_ms"))
        if premium_receipt is None:
            premium_receipt = perp_receipt
        mark = parse_mark(bundle.get("premium"))
        if mark is not None \
                and not accept_timestamp(premium_receipt, mark["ts_ms"], skew):
            mark = None
        funding_points = parse_funding_history(bundle.get("funding_history"))
        funding_period_ms = infer_funding_period_ms(funding_points)
        if funding_period_ms is None:
            # KernelConfig requires a positive period, so express an unknown
            # cadence by clearing the funding mark.  The kernel then applies
            # its normal data_stale refusal; no global 8h value prices it.
            mark = None
        return {
            "symbol": symbol,
            "direction": "long_spot_short_perp",
            "now_ms": now_ms,
            "notional_usd": cfg["notional_usd"],
            "spot_book": book_from_ladder(spot_ladder, spot_ts),
            "perp_book": book_from_ladder(perp_ladder, perp_ts),
            "spot_ladder": spot_ladder,
            "perp_ladder": perp_ladder,
            "mark": mark,
            "funding_history": [point["funding_rate"]
                                for point in funding_points
                                if point["funding_rate"] is not None],
            "funding_history_points": funding_points,
            "funding_period_ms": funding_period_ms,
            "fees": fees,
            "config": cfg,
        }

    def _pick_finalists(self, rows: List[Dict[str, Any]],
                        now_ms: int) -> List[str]:
        by_symbol = {row["symbol"]: row for row in rows}
        for symbol in list(self.symbols):
            if symbol not in by_symbol:
                del self.symbols[symbol]  # universe_retention: prune departed
        finalists: List[str] = []
        for row in rows:
            if len(finalists) >= self.cfg["finalist_cap"]:
                break
            # universe_filter (KEEP): symbol regex is engine meta validation;
            # a non-conforming symbol is ineligible, never a finalist.
            if row["fits_10_cap"] and SYMBOL_RE.fullmatch(row["symbol"]):
                finalists.append(row["symbol"])
        tracked = sorted(symbol for symbol, state in self.symbols.items()
                         if state.state in ("WATCHING", "FOCUS", "PROPOSED")
                         and symbol not in finalists)
        finalists.extend(tracked)
        return [symbol for symbol in finalists
                if not self.symbols.get(symbol, SymbolState())
                .in_cooldown(now_ms)]

    def run_pass(self, reads: Any, now_ms: Optional[int] = None
                 ) -> List[Dict[str, Any]]:
        """One tick: universe -> finalists -> enrich -> kernel -> fold ->
        maintain -> watermark -> atomic state write.  Returns factual rows.

        With no injected now_ms, every finalist is evaluated on its own
        post-fetch clock — live GETs are serial and slow, so one pass-start
        stamp would misdate every later observation.
        """
        injected = now_ms is not None
        clock = (lambda: now_ms) if injected \
            else (lambda: int(time.time() * 1000))
        pass_now = clock()
        rows = reads.universe_rows()
        market_rows = {row["symbol"]: row for row in rows}
        finalists = self._pick_finalists(rows, pass_now)
        out: List[Dict[str, Any]] = []
        for symbol in finalists:
            state = self.symbols.setdefault(symbol, SymbolState())
            bundle = reads.finalist(symbol)
            sym_now = clock()  # the symbol's evaluation clock, after its fetch
            fees = read_commission_rates(self.receipt_dir, symbol, self.cfg)
            observation = self._observation(symbol, sym_now, bundle, fees)
            spot_book = observation["spot_book"]
            perp_book = observation["perp_book"]
            gap = entry_gap_pct(spot_book["ask"] if spot_book else None,
                                perp_book["bid"] if perp_book else None)
            observe_gap(state, sym_now, gap, self.cfg)
            observation["persist_ticks"] = state.persist_ticks
            observation["gap_age_ms"] = state.gap_age_ms(sym_now)
            observation["slope_pct_per_min"] = slope_pct_per_min(state.window)
            observation["stability_cv"] = stability_cv(
                state.window, self.cfg["persist_ticks"])
            decision = kernel_evaluate(observation)
            score = priority_score(
                decision_decimal(decision, "net_pct"), state.persist_ticks,
                self.cfg["persist_ticks"],
                decision_decimal(decision, "depth_score"),
                decision_decimal(decision, "carry_pct"),
                decision_decimal(decision, "capture_factor"),
                decision_decimal(decision, "dq_score"),
                observation["slope_pct_per_min"], self.cfg)
            proposal_id, proposal_status = self.maybe_write_proposal(
                symbol, sym_now, market_rows[symbol], observation, decision)
            lifecycle_kind = decision["kind"]
            if lifecycle_kind == "proposal" and proposal_status.startswith(
                    "refused_"):
                lifecycle_kind = "unknown"  # no PROPOSED state without artifact
            self.apply_decision(symbol, lifecycle_kind,
                                decision.get("reject_code"), score, sym_now)
            spot_leg = leg_depth_score(
                observation["spot_ladder"]["asks"] if observation["spot_ladder"]
                else None,
                spot_book["ask"] if spot_book else None,
                spot_book["ask_qty"] if spot_book else None,
                self.cfg["notional_usd"])
            perp_leg = leg_depth_score(
                observation["perp_ladder"]["bids"] if observation["perp_ladder"]
                else None,
                perp_book["bid"] if perp_book else None,
                perp_book["bid_qty"] if perp_book else None,
                self.cfg["notional_usd"])
            out.append({
                "symbol": symbol,
                "state": self.symbols[symbol].state,
                "kind": decision["kind"],
                "reject_code": decision.get("reject_code"),
                "note": decision.get("note"),
                "gap_pct": gap,
                "spot_spread_bps": spread_bps(
                    spot_book["bid"] if spot_book else None,
                    spot_book["ask"] if spot_book else None),
                "perp_spread_bps": spread_bps(
                    perp_book["bid"] if perp_book else None,
                    perp_book["ask"] if perp_book else None),
                "depth_score": pair_depth_score(spot_leg, perp_leg),
                "funding_rate": observation["mark"]["funding_rate"]
                if observation["mark"] else None,
                "carry_pct": decision_decimal(decision, "carry_pct"),
                "net_pct": decision_decimal(decision, "net_pct"),
                "persist_ticks": self.symbols[symbol].persist_ticks,
                "stability_cv": observation["stability_cv"],
                "score": score,
                "fees_source": fees["source"],
                "proposal_id": proposal_id,
                "proposal_status": proposal_status,
            })
        end_now = clock()
        self.maintain(end_now)
        self.pass_count += 1
        self.last_pass_ms = end_now
        self.save_state()
        return out


# ---------------------------------------------------------------------------
# CLI.


def format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


ROW_FIELDS = ("symbol", "state", "kind", "reject_code", "gap_pct",
              "spot_spread_bps", "perp_spread_bps", "depth_score",
              "funding_rate", "carry_pct", "net_pct", "persist_ticks",
              "stability_cv", "fees_source", "proposal_id",
              "proposal_status")


def print_pass(watch: Watch, rows: List[Dict[str, Any]]) -> None:
    print("WATCH | pass %d | finalists %d | kernel %s | state %s"
          % (watch.pass_count, len(rows),
             "present" if basis_kernel is not None else "absent",
             watch.state_path))
    print(" | ".join(ROW_FIELDS))
    for row in rows:
        print(" | ".join(format_value(row[key]) for key in ROW_FIELDS))


def main(argv: Optional[List[str]] = None, reads: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true",
                      help="one pass, factual rows out")
    mode.add_argument("--loop", type=float, metavar="SECONDS",
                      help="repeat passes every SECONDS, honoring --stop-file")
    parser.add_argument("--receipt-dir", default=RECEIPT_DIR,
                        help="commission receipts + default state/stop dir")
    parser.add_argument("--state-file", default=None,
                        help="state path (default RECEIPT_DIR/%s/%s)"
                             % (WATCH_STATE_DIR_NAME, STATE_FILE_NAME))
    parser.add_argument("--stop-file", default=None,
                        help="loop exits cleanly when this path exists "
                             "(default RECEIPT_DIR/STOP)")
    parser.add_argument("--finalists", type=int, default=DEFAULTS["finalist_cap"],
                        help="cap-fitting best-gap finalist bound")
    parser.add_argument("--notional-usd", default=None,
                        help="intended notional for depth math (default %s)"
                             % CAP_USDT)
    args = parser.parse_args(argv)
    if args.loop is not None and args.loop <= 0:
        parser.error("--loop SECONDS must be > 0")
    if args.finalists < 1:
        parser.error("--finalists must be >= 1")

    receipt_dir = os.path.expanduser(args.receipt_dir)
    state_path = args.state_file or os.path.join(
        receipt_dir, WATCH_STATE_DIR_NAME, STATE_FILE_NAME)
    stop_path = args.stop_file or os.path.join(receipt_dir, "STOP")
    overrides: Dict[str, Any] = {"finalist_cap": args.finalists}
    if args.notional_usd is not None:
        notional = dec(args.notional_usd)
        if notional is None or notional <= 0:
            parser.error("--notional-usd must be a positive decimal")
        overrides["notional_usd"] = notional
    try:
        watch = Watch(state_path, receipt_dir, watch_config(overrides))
    except WatchError as exc:
        print("ERROR: %s" % exc)
        return 4
    if reads is None:
        reads = LiveReads()

    if args.once:
        try:
            rows = watch.run_pass(reads)
        except (ScanError, OSError, ValueError) as exc:
            print("WATCH | pass error | %s" % type(exc).__name__)
            return 3
        print_pass(watch, rows)
        return 0

    interval = float(args.loop)
    while True:
        if os.path.exists(stop_path):
            print("WATCH | stop file present | %s | exiting" % stop_path)
            return 0
        try:
            rows = watch.run_pass(reads)
            print_pass(watch, rows)
        except (ScanError, OSError, ValueError) as exc:
            print("WATCH | pass error | %s" % type(exc).__name__)
        deadline = time.monotonic() + interval
        while True:
            if os.path.exists(stop_path):
                print("WATCH | stop file present | %s | exiting" % stop_path)
                return 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))


if __name__ == "__main__":
    raise SystemExit(main())
