"""Offline tests for src/basis_watch.py — no network, injected reads only.

B1 STUB NOTE (dispatch-required disclosure): src/basis_kernel.py (the B1
kernel) was ABSENT in this worktree at build time.  Per the dispatch ("if an
import target is missing at your test time, stub it in the TEST file only
and say so"), a spec-faithful stand-in kernel lives in THIS file only:
StubKernel.evaluate implements the spec's frozen entry guard ladder
(tests/fixtures/old_engine_spec.json, scanner area guard_ladder_order plus
the math/funding-area formulas it consumes) far enough to reproduce every
scanner-area DECISION fixture.  Tests patch basis_watch.basis_kernel to the
stub explicitly, so this suite stays deterministic when the real B1 lands;
the differential harness then compares stub-driven and kernel-driven runs.

Fixture accounting for the scanner area (23 fixtures):
- 19 reproduced below (names in ScannerFixtureTests / PipelineFixtureTests /
  HysteresisTests, each test cites its fixture).
- 4 WAIVED: causal_quadrants, causal_growth_then_confirmed_decay,
  causal_flip_resets, causal_expiry_and_source_reversal.  Written reason in
  CausalFixturesWaived below.

Numeric tolerance: the spec's expected values are float64 results of the
stated formulas; Decimal reproduction lands within 6e-13 of every pinned
value (measured), so the 1e-9 tolerance used here is strict against the old
suite's 1e-6 default.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from glob import glob
from pathlib import Path
from typing import Any, Dict, List, Optional

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_scan  # noqa: E402
import basis_position  # noqa: E402
import basis_watch  # noqa: E402
from basis_watch import (  # noqa: E402
    SymbolState,
    Watch,
    WatchError,
    accept_timestamp,
    depth_coverage_score,
    entry_gap_pct,
    infer_funding_period_ms,
    leg_depth_score,
    observe_gap,
    pair_depth_score,
    parse_funding_history,
    parse_ladder,
    parse_mark,
    priority_momentum,
    read_commission_rates,
    return_pct,
    slope_pct_per_min,
    spread_bps,
    stability_cv,
    stability_stats,
    universe_eligible,
    watch_config,
)

SPEC_PATH = Path(__file__).resolve().parent / "fixtures" / "old_engine_spec.json"
with open(SPEC_PATH, encoding="utf-8") as handle:
    SPEC = json.load(handle, parse_float=Decimal)

TOL = Decimal("1e-9")
T0 = 1_000_000
D = Decimal


def scanner_fixture(name: str) -> Dict[str, Any]:
    for fixture in SPEC["scanner"]["fixtures"]:
        if fixture["name"] == name:
            return fixture
    raise AssertionError("scanner fixture missing from spec: %s" % name)


def as_dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(value)


# ---------------------------------------------------------------------------
# TEST-ONLY B1 stand-in (see module docstring).  Spec formulas only; Decimal
# only; None = fail-closed null; guard order is the spec's frozen ladder.


def _clamp01(value: Decimal) -> Decimal:
    return basis_watch.clamp(value, D(0), D(1))


def _expected_hold_ms(gap: Optional[Decimal], slope: Optional[Decimal]) -> int:
    default, low, high = 14_400_000, 600_000, 86_400_000
    if gap is not None and gap > 0 and slope is not None \
            and slope < D("-0.000001"):
        raw = int((gap / -slope) * 60_000)
        return max(low, min(high, raw))
    return max(low, min(high, default))


def _funding_carry_pct(rates: List[Decimal], hold_ms: int, period_ms: int,
                       ms_to_next: Optional[int]) -> Optional[Decimal]:
    if hold_ms is None or hold_ms < 0 or period_ms <= 0 \
            or ms_to_next is None or ms_to_next <= 0:
        return None
    if ms_to_next > hold_ms:
        return D(0)
    crossings = 1 + (hold_ms - ms_to_next) // period_ms
    if crossings > len(rates):
        return None
    total = D(0)
    for rate in rates[:crossings]:
        if not rate.is_finite() or abs(rate) > D("0.1"):
            return None
        total += rate * 100  # sign +1: short perp RECEIVES positive funding
    return total


def _flip_risk(history: List[Decimal], current: Optional[Decimal]
               ) -> Optional[Decimal]:
    finite = [rate for rate in history if rate.is_finite()]
    if len(finite) < 3:
        return None
    flips = sum(1 for a, b in zip(finite, finite[1:])
                if a != 0 and b != 0 and (a < 0) != (b < 0))
    share = D(flips) / D(len(finite) - 1)
    anchor = current if current is not None and current.is_finite() \
        else finite[-1]
    magnitude = _clamp01(1 - abs(anchor) / D("0.0005"))
    return _clamp01(D("0.6") * share + D("0.4") * magnitude)


def _walk_slippage_pct(levels, notional: Decimal) -> Optional[Decimal]:
    if not levels or notional <= 0 or levels[0][0] <= 0:
        return None
    touch = levels[0][0]
    remaining, cost, filled = notional, D(0), D(0)
    for price, qty in levels:
        take = min(remaining, price * qty)
        cost += take
        filled += take / price
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        return None  # ladder cannot fill -> caller applies the x4 penalty
    return abs(cost / filled - touch) / touch * 100


def _leg_slippage_pct(levels, touch_px, touch_qty, notional: Decimal,
                      estimate: Decimal = D("0.05")) -> Decimal:
    if levels:
        walked = _walk_slippage_pct(levels, notional)
        return walked if walked is not None else estimate * 4
    base = estimate / 2
    if touch_px is None or touch_qty is None or touch_px <= 0 \
            or touch_qty <= 0 or notional <= 0:
        return base * 4
    consumption = notional / (touch_px * touch_qty)
    return base * max(D(1), consumption / D("0.25"))


def stub_evaluate(obs: Dict[str, Any]) -> Dict[str, Any]:
    cfg = obs["config"]
    evidence: Dict[str, Any] = {}

    def reject(code):
        return {**evidence, "kind": "reject", "reject_code": code}

    spot, perp = obs["spot_book"], obs["perp_book"]
    if spot is None:                                             # guard 1
        return reject("spot_missing")
    if perp is None:                                             # guard 2
        return reject("perp_missing")
    now = obs["now_ms"]
    spot_stale = max(0, now - spot["ts_ms"])
    perp_stale = max(0, now - perp["ts_ms"])
    if spot_stale > cfg["stale_ms"] or perp_stale > cfg["stale_ms"]:
        return reject("data_stale")                              # guard 3
    leg_skew = abs(spot["ts_ms"] - perp["ts_ms"])
    if leg_skew > cfg["max_leg_skew_ms"]:
        return reject("data_stale")                              # guard 4
    mark = obs["mark"]
    spot_fresh = _clamp01(1 - D(spot_stale) / D(cfg["stale_ms"]))
    perp_fresh = _clamp01(1 - D(perp_stale) / D(cfg["stale_ms"]))
    if mark is not None:
        mark_stale = max(0, now - mark["ts_ms"])
        mark_fresh = _clamp01(1 - D(mark_stale) / D(cfg["funding_stale_ms"]))
    else:
        mark_fresh = D(0)
    skew_comp = _clamp01(1 - D(leg_skew) / D(cfg["max_leg_skew_ms"]))
    perp_mid = (perp["bid"] + perp["ask"]) / 2
    if mark is not None and mark["mark_price"] > 0:
        divergence = abs(perp_mid - mark["mark_price"]) \
            / mark["mark_price"] * 10000
        div_comp = _clamp01(1 - divergence / D(50))
    else:
        div_comp = D("0.75")
    ladder_comp = D(1) if obs["spot_ladder"] and obs["perp_ladder"] else D("0.7")
    dq = _clamp01(min(spot_fresh, perp_fresh, mark_fresh)
                  * (D("0.5") + D("0.5") * skew_comp)
                  * (D("0.6") + D("0.4") * div_comp) * ladder_comp)
    evidence["dq_score"] = dq
    if dq < cfg["min_data_quality"]:
        return reject("data_stale")                              # guard 5
    funding_fresh = mark is not None
    if funding_fresh:
        rate = mark["funding_rate"]
        mark_age = now - mark["ts_ms"]
        funding_fresh = rate.is_finite() and abs(rate) <= D("0.1") \
            and mark["mark_price"] > 0 \
            and -cfg["max_future_skew_ms"] <= mark_age <= cfg["funding_stale_ms"] \
            and mark["next_funding_ts_ms"] > now
    if not funding_fresh:
        return reject("data_stale")                              # guard 6
    gap = entry_gap_pct(spot["ask"], perp["bid"])
    hold_ms = _expected_hold_ms(gap, obs.get("slope_pct_per_min"))
    carry = _funding_carry_pct([mark["funding_rate"]], hold_ms,
                               obs["funding_period_ms"],
                               mark["next_funding_ts_ms"] - now)
    flip = _flip_risk(obs.get("funding_history") or [], mark["funding_rate"])
    if carry is not None and carry > 0 and flip is not None:
        carry = carry * (1 - flip)  # bonus discounted, drag never
    if carry is None:
        return reject("data_stale")                              # guard 6 (carry)
    evidence["carry_pct"] = carry
    if gap is None:
        return reject("book_missing")                            # guard 7
    evidence["gap_pct"] = gap
    if gap < cfg["watch_gap_pct"]:
        return {**evidence, "kind": "ignore", "reject_code": None}  # guard 8
    spot_spread = spread_bps(spot["bid"], spot["ask"])
    perp_spread = spread_bps(perp["bid"], perp["ask"])
    if spot_spread is None or perp_spread is None \
            or spot_spread > cfg["max_spread_bps"] \
            or perp_spread > cfg["max_spread_bps"]:
        return reject("spread_too_wide")                         # guard 9
    notional = obs["notional_usd"]
    spot_leg = leg_depth_score(
        obs["spot_ladder"]["asks"] if obs["spot_ladder"] else None,
        spot["ask"], spot["ask_qty"], notional)
    perp_leg = leg_depth_score(
        obs["perp_ladder"]["bids"] if obs["perp_ladder"] else None,
        perp["bid"], perp["bid_qty"], notional)
    depth = pair_depth_score(spot_leg, perp_leg)
    evidence["depth_score"] = depth
    if depth is None or depth < cfg["min_depth_score"]:
        return reject("depth_too_thin")                          # guard 10
    slippage = _leg_slippage_pct(
        obs["spot_ladder"]["asks"] if obs["spot_ladder"] else None,
        spot["ask"], spot["ask_qty"], notional) + _leg_slippage_pct(
        obs["perp_ladder"]["bids"] if obs["perp_ladder"] else None,
        perp["bid"], perp["bid_qty"], notional)
    fees = 2 * obs["fees"]["spot_taker_fee_pct"] \
        + 2 * obs["fees"]["perp_taker_fee_pct"]
    evidence["fees_pct"] = fees
    evidence["slippage_pct"] = slippage
    if gap <= fees + slippage:
        return reject("edge_below_fees")                         # guard 11
    drag = max(D(0), -carry)
    if drag > cfg["max_funding_drag_pct"]:
        return reject("funding_against_trade")                   # guard 12
    cv = obs.get("stability_cv")                                 # guard 14
    if cv is not None and cv > cfg["max_gap_cv"] \
            and obs["persist_ticks"] < cfg["persist_ticks"]:
        return reject("gap_not_persistent")
    net = gap - (fees + slippage + drag) + max(D(0), carry)
    evidence["net_pct"] = net
    if net < cfg["min_net_basis_pct"]:
        return reject("edge_below_threshold")                    # guard 15
    if obs["persist_ticks"] >= cfg["persist_ticks"]:             # guard 16
        kind = "proposal"
    else:
        kind = "focus" if gap >= cfg["focus_gap_pct"] else "watch"
    return {**evidence, "kind": kind, "reject_code": None}


class StubKernel:
    """TEST-ONLY stand-in for the absent B1 basis_kernel surface."""

    def __init__(self, log: Optional[List] = None) -> None:
        self.log = log if log is not None else []

    def evaluate(self, observation: Dict[str, Any]) -> Dict[str, Any]:
        decision = stub_evaluate(observation)
        self.log.append((observation, decision))
        return decision


# ---------------------------------------------------------------------------
# Offline transport fakes and scenario builders.


def universe_row(symbol: str, entry_bps: str = "78",
                 fits: bool = True) -> Dict[str, Any]:
    return {"symbol": symbol, "entry_gap_bps": D(entry_bps),
            "exit_gap_bps": D("0"), "funding_bps": D("1"),
            "min_valid_notional": D("5"), "fits_10_cap": fits}


def funding_history_rows(period_ms: int, count: int = 3,
                         rates: Optional[List[str]] = None
                         ) -> List[Dict[str, Any]]:
    values = rates if rates is not None else ["0.0001"] * count
    return [{"fundingTime": 1 + period_ms * index,
             "fundingRate": values[index]}
            for index in range(count)]


def scenario_bundle(now_ms: int, gap_pct: str = "0.78", qty: str = "500",
                    rate: str = "0.0001", next_funding_ms: Optional[int] = None,
                    book_ts: Optional[int] = None,
                    perp_ts: Optional[int] = None,
                    mark_ts: Optional[int] = None,
                    default_book: bool = False,
                    funding_history: Optional[List[Dict[str, Any]]] = None
                    ) -> Dict[str, Any]:
    """One finalist bundle built with the old suite's generator:
    perp_bid = spot_ask * (1 + g/100) makes the entry gap exactly g.
    default_book=True uses the spec's default book instead (spot 100/100.02,
    perp 100.8/100.82 -> gap 0.7798440311937624)."""
    spot_ask = D("100.02")
    perp_bid = D("100.8") if default_book \
        else spot_ask * (1 + D(gap_pct) / 100)
    perp_ask = perp_bid + D("0.02")
    book_ts = now_ms if book_ts is None else book_ts
    perp_ts = book_ts if perp_ts is None else perp_ts
    mark_ts = now_ms if mark_ts is None else mark_ts
    next_funding = next_funding_ms if next_funding_ms is not None \
        else now_ms + 14_400_000
    mid = (perp_bid + perp_ask) / 2
    if funding_history is None:
        # Existing scanner fixtures intentionally leave flip risk unknown;
        # three valid timestamps still supply the now-required 8h cadence.
        funding_history = funding_history_rows(
            28_800_000, rates=["0.0001", "0.0001", "nan"])
    return {
        "spot_depth": {"bids": [["100", qty]], "asks": [[str(spot_ask), qty]]},
        "spot_receipt_ms": book_ts,
        "perp_depth": {"T": perp_ts, "bids": [[str(perp_bid), qty]],
                       "asks": [[str(perp_ask), qty]]},
        "perp_receipt_ms": now_ms,
        "funding_history": funding_history,
        "premium": {"markPrice": str(mid), "lastFundingRate": rate,
                    "nextFundingTime": next_funding, "time": mark_ts},
    }


class FakeReads:
    def __init__(self, rows: List[Dict[str, Any]],
                 bundles: Dict[str, Dict[str, Any]]) -> None:
        self.rows = rows
        self.bundles = bundles
        self.universe_calls = 0

    def universe_rows(self) -> List[Dict[str, Any]]:
        self.universe_calls += 1
        return self.rows

    def finalist(self, symbol: str) -> Dict[str, Any]:
        return self.bundles[symbol]


class StoppingReads(FakeReads):
    def __init__(self, rows, bundles, stop_path: str, after: int) -> None:
        super().__init__(rows, bundles)
        self.stop_path = stop_path
        self.after = after

    def universe_rows(self) -> List[Dict[str, Any]]:
        rows = super().universe_rows()
        if self.universe_calls >= self.after:
            with open(self.stop_path, "w", encoding="utf-8") as handle:
                handle.write("stop\n")
        return rows


class PipelineCase(unittest.TestCase):
    """Base: temp state dir + the stub kernel patched in (and restored)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "basis_watch_state.json")
        self.kernel_log: List = []
        self._saved_kernel = basis_watch.basis_kernel
        basis_watch.basis_kernel = StubKernel(self.kernel_log)
        self.addCleanup(self._restore_kernel)

    def _restore_kernel(self) -> None:
        basis_watch.basis_kernel = self._saved_kernel

    def make_watch(self, **overrides: Any) -> Watch:
        return Watch(self.state_path, self.tmp.name, watch_config(overrides))

    def tick(self, watch: Watch, now_ms: int, symbol: str = "TESTUSDT",
             **bundle_kwargs: Any) -> Dict[str, Any]:
        bundle = scenario_bundle(now_ms, **bundle_kwargs)
        reads = FakeReads([universe_row(symbol)], {symbol: bundle})
        rows = watch.run_pass(reads, now_ms)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def assertClose(self, value: Optional[Decimal], expected: Any,
                    tol: Decimal = TOL) -> None:
        self.assertIsNotNone(value)
        expected_dec = as_dec(expected)
        self.assertLessEqual(abs(value - expected_dec), tol,
                             "value %s != expected %s" % (value, expected_dec))


# ---------------------------------------------------------------------------
# Scanner-area numeric fixtures (pure functions).


class ScannerFixtureTests(PipelineCase):
    def test_gap_forward_basic(self):
        fixture = scanner_fixture("gap_forward_basic")
        gap = entry_gap_pct(as_dec(fixture["inputs"]["spot_ask"]),
                            as_dec(fixture["inputs"]["perp_bid"]))
        self.assertClose(gap, fixture["expected"]["gap_pct"], D("1e-12"))

    def test_gap_reverse_divides_by_long_leg(self):
        fixture = scanner_fixture("gap_reverse_divides_by_long_leg")
        # Reverse pair under the ONE return law: sell = spot_bid, buy = perp_ask.
        gap = return_pct(as_dec(fixture["inputs"]["spot_bid"]),
                         as_dec(fixture["inputs"]["perp_ask"]))
        self.assertClose(gap, fixture["expected"]["gap_pct"], D("1e-12"))
        wrong = (as_dec(fixture["inputs"]["spot_bid"])
                 - as_dec(fixture["inputs"]["perp_ask"])) / D(100) * 100
        self.assertGreater(abs(gap - wrong), D("1e-4"))  # wrong denominator

    def test_gap_degenerate(self):
        fixture = scanner_fixture("gap_degenerate")
        for case, expected in zip(fixture["inputs"]["cases"],
                                  fixture["expected"]["returns"]):
            value = return_pct(as_dec(case["sell"]), as_dec(case["buy"]))
            if expected is None:
                self.assertIsNone(value)
            else:
                self.assertClose(value, expected, D("1e-12"))

    def test_spread_bps_cases(self):
        fixture = scanner_fixture("spread_bps")
        for case, expected in zip(fixture["inputs"]["cases"],
                                  fixture["expected"]["spread_bps"]):
            value = spread_bps(as_dec(case["bid"]), as_dec(case["ask"]))
            if expected is None:
                self.assertIsNone(value)  # crossed book fails closed
            else:
                self.assertClose(value, expected)
        self.assertEqual(spread_bps(D("100"), D("100")), D(0))  # ask == bid

    def test_slope_two_points(self):
        fixture = scanner_fixture("slope_two_points")
        samples = [(int(row["ts"]), as_dec(row["v"]))
                   for row in fixture["inputs"]["samples"]]
        self.assertClose(slope_pct_per_min(samples),
                         fixture["expected"]["slope_pct_per_min"])

    def test_slope_four_points_2s_apart(self):
        fixture = scanner_fixture("slope_four_points_2s_apart")
        samples = [(int(row["ts"]), as_dec(row["v"]))
                   for row in fixture["inputs"]["samples"]]
        self.assertClose(slope_pct_per_min(samples),
                         fixture["expected"]["slope_pct_per_min"])

    def test_slope_degenerate(self):
        fixture = scanner_fixture("slope_degenerate")
        for case in fixture["inputs"]["cases"]:
            samples = [(int(row["ts"]), as_dec(row["v"])) for row in case]
            self.assertIsNone(slope_pct_per_min(samples))

    def test_cv_flicker_series(self):
        fixture = scanner_fixture("cv_flicker_series")
        values = [as_dec(value) for value in fixture["inputs"]["recent_values"]]
        mean, stddev, cv = stability_stats(values)
        self.assertClose(mean, fixture["expected"]["mean"])
        self.assertClose(stddev, fixture["expected"]["sample_stddev"])
        self.assertClose(cv, fixture["expected"]["cv"])
        self.assertTrue(cv > watch_config()["max_gap_cv"])

    def test_cv_insufficient_samples(self):
        fixture = scanner_fixture("cv_insufficient_samples")
        window = [(T0 + 2000 * index, as_dec(value)) for index, value
                  in enumerate(fixture["inputs"]["recent_values"])]
        self.assertIsNone(stability_cv(window, 5))

    def test_depth_score_curve(self):
        fixture = scanner_fixture("depth_score_curve")
        for coverage, expected in zip(fixture["inputs"]["coverage_cases"],
                                      fixture["expected"]["scores"]):
            self.assertClose(depth_coverage_score(as_dec(coverage)), expected)
        # coverage 0.2 sits below the 0.3 gate (fixture note)
        gate = watch_config()["min_depth_score"]
        self.assertLess(depth_coverage_score(D("0.2")), gate)

    def test_depth_score_thin_leg_from_math_area(self):
        # math-area fixture depth_score_thin_spot_leg via the ladder path
        score = leg_depth_score([(D("100.02"), D("0.02"))], D("100.02"),
                                D("0.02"), D(10))
        self.assertClose(score, D("0.07604789568600906"))
        self.assertLess(score, watch_config()["min_depth_score"])

    def test_pair_depth_is_min_and_fails_closed(self):
        self.assertEqual(pair_depth_score(D("0.5"), D("0.9")), D("0.5"))
        self.assertIsNone(pair_depth_score(None, D("0.9")))
        self.assertIsNone(leg_depth_score(None, None, None, D(10)))

    def test_priority_momentum(self):
        fixture = scanner_fixture("priority_momentum")
        cfg = watch_config()
        for slope, expected in zip(fixture["inputs"]["slope_cases"],
                                   fixture["expected"]["momentum"]):
            value = priority_momentum(as_dec(slope), cfg["watch_gap_pct"],
                                      cfg["priority_momentum_weight"])
            self.assertClose(value, expected)
        self.assertEqual(priority_momentum(None, cfg["watch_gap_pct"],
                                           cfg["priority_momentum_weight"]),
                         D(1))

    def test_persistence_and_age(self):
        fixture = scanner_fixture("persistence_and_age")
        cfg = watch_config()
        state = SymbolState()
        ticks, ages = [], []
        for index, gap in enumerate(fixture["inputs"]["gap_sequence_pct"]):
            now = T0 + 2000 * index
            observe_gap(state, now, as_dec(gap), cfg)
            ticks.append(state.persist_ticks)
            ages.append(state.gap_age_ms(now))
        self.assertEqual(ticks, fixture["expected"]["persist_ticks"])
        self.assertEqual(ages, fixture["expected"]["gap_age_ms"])

    def test_unit_conventions_explicit(self):
        """math-area unit_conventions: percent vs bps vs fraction, one book."""
        gap = entry_gap_pct(D("100.02"), D("100.8"))     # PERCENT
        spread = spread_bps(D("100"), D("100.02"))       # BPS
        self.assertClose(gap / 100, D("0.0077984403119376"), D("1e-12"))
        self.assertClose(spread / 10000, D("0.00019998000199980002"), D("1e-12"))
        cfg = watch_config()
        fees_pct = 2 * cfg["spot_taker_fee_pct"] + 2 * cfg["perp_taker_fee_pct"]
        self.assertEqual(fees_pct, D("0.3"))             # PERCENT round trip
        # funding FRACTION -> carry PERCENT (x100) through the decision path
        watch = self.make_watch()
        row = self.tick(watch, T0 + 2000, rate="0.0001")
        self.assertEqual(row["funding_rate"], D("0.0001"))   # fraction
        _, decision = self.kernel_log[-1]
        self.assertClose(decision["carry_pct"], D("0.01"), D("1e-12"))


# ---------------------------------------------------------------------------
# Scanner-area decision fixtures through the full pipeline (stub kernel).


class PipelineFixtureTests(PipelineCase):
    def test_decision_sequence_persist3(self):
        fixture = scanner_fixture("decision_sequence_persist3")
        watch = self.make_watch(persist_ticks=3)
        kinds = []
        for index in range(3):
            row = self.tick(watch, T0 + 2000 * (index + 1), default_book=True)
            kinds.append(row["kind"])
        self.assertEqual(kinds, fixture["expected"]["kinds"])
        self.assertEqual(watch.symbols["TESTUSDT"].state, "PROPOSED")
        observation, decision = self.kernel_log[-1]
        self.assertEqual(observation["direction"],
                         fixture["expected"]["proposal_direction"])
        self.assertClose(decision["gap_pct"],
                         fixture["expected"]["gross_edge_pct"])
        self.assertEqual(decision["fees_pct"], as_dec(fixture["expected"]["fees_pct"]))
        low, high = fixture["expected"]["net_edge_pct_range"]
        self.assertTrue(as_dec(low) < decision["net_pct"] < as_dec(high))

    def test_edge_below_fees(self):
        fixture = scanner_fixture("edge_below_fees")
        watch = self.make_watch()
        row = self.tick(watch, T0 + 2000, gap_pct="0.25")
        self.assertEqual(row["kind"], fixture["expected"]["kind"])
        self.assertEqual(row["reject_code"], fixture["expected"]["reject_code"])
        self.assertEqual(watch.pass_count, fixture["expected"]["fires_on_tick"])
        # shallow reject: never advanced, so REJECTED not COOLDOWN
        self.assertEqual(watch.symbols["TESTUSDT"].state, "REJECTED")

    def test_edge_below_threshold(self):
        fixture = scanner_fixture("edge_below_threshold")
        watch = self.make_watch()
        row = self.tick(watch, T0 + 2000, gap_pct="0.45")
        self.assertEqual(row["kind"], "reject")
        self.assertEqual(row["reject_code"], fixture["expected"]["reject_code"])
        self.assertEqual(watch.pass_count, fixture["expected"]["fires_on_tick"])
        _, decision = self.kernel_log[-1]
        self.assertClose(decision["net_pct"], fixture["expected"]["net_edge_pct"])

    def test_flicker_rejects_not_persistent(self):
        fixture = scanner_fixture("flicker_rejects_not_persistent")
        watch = self.make_watch(persist_ticks=5, min_net_basis_pct=D("0.01"))
        rows = []
        for index, gap in enumerate(fixture["inputs"]["gap_sequence_pct"]):
            rows.append(self.tick(watch, T0 + 2000 * (index + 1),
                                  gap_pct=str(gap)))
        self.assertEqual(rows[-1]["kind"], fixture["expected"]["final_kind"])
        self.assertEqual(rows[-1]["reject_code"],
                         fixture["expected"]["reject_code"])
        self.assertClose(rows[-1]["stability_cv"],
                         fixture["expected"]["evidence_stability_cv"])
        self.assertEqual(rows[-1]["persist_ticks"],
                         fixture["expected"]["persist_ticks_at_reject"])
        # the advanced signal died with a named reason -> cooldown
        self.assertEqual(watch.symbols["TESTUSDT"].state, "COOLDOWN")

    def test_stale_books_then_cooldown(self):
        fixture = scanner_fixture("stale_books_then_cooldown")
        watch = self.make_watch()
        self.tick(watch, T0 + 2000)
        self.tick(watch, T0 + 4000)
        self.assertEqual(watch.symbols["TESTUSDT"].state, "FOCUS")
        stale_row = self.tick(watch, T0 + 12000, book_ts=T0 + 2000)
        self.assertEqual(stale_row["kind"], "reject")
        self.assertEqual(stale_row["reject_code"],
                         fixture["expected"]["reject_code"])
        observation, _ = self.kernel_log[-1]
        staleness = observation["now_ms"] - observation["spot_book"]["ts_ms"]
        self.assertEqual(staleness, fixture["expected"]["staleness_ms"])
        self.assertEqual(watch.symbols["TESTUSDT"].state, "COOLDOWN")
        self.assertEqual(watch.active_cooldown_count(T0 + 14000), 1)
        # next tick: the symbol is skipped entirely (no decisions at all)
        bundle = scenario_bundle(T0 + 14000)
        reads = FakeReads([universe_row("TESTUSDT")], {"TESTUSDT": bundle})
        self.assertEqual(watch.run_pass(reads, T0 + 14000), [])
        # cooldown no longer active after cooldownMs
        expiry = T0 + 12000 + watch.cfg["cooldown_ms"]
        watch.maintain(expiry)
        self.assertEqual(watch.active_cooldown_count(expiry), 0)

    def test_cross_leg_skew_reject(self):
        fixture = scanner_fixture("cross_leg_skew_reject")
        watch = self.make_watch()
        now = T0 + 2000
        row = self.tick(watch, now, perp_ts=now - 4000)
        self.assertEqual(row["kind"], "reject")
        self.assertEqual(row["reject_code"], fixture["expected"]["reject_code"])
        observation, _ = self.kernel_log[-1]
        skew = abs(observation["spot_book"]["ts_ms"]
                   - observation["perp_book"]["ts_ms"])
        self.assertEqual(skew, fixture["expected"]["leg_skew_ms"])


class CausalFixturesWaived(unittest.TestCase):
    """WAIVED — 4 scanner fixtures not reproduced: causal_quadrants,
    causal_growth_then_confirmed_decay, causal_flip_resets,
    causal_expiry_and_source_reversal.

    Written reason (dispatch requires one, never silent deletion): the
    causality sub-machine is evidence-only.  The spec's scanner verdict and
    notes state that no scanner guard reads it and that every DECISION
    fixture reproduces without it, and rule causal_lifecycle_attribution is
    ADAPT with 'defer from the minimal kernel'.  B2 ships without the causal
    observer, so these four fixtures have no implementation to run against;
    they stay in the spec for a later attribution port.
    """

    def test_waived_names_still_exist_in_spec(self):
        names = {fixture["name"] for fixture in SPEC["scanner"]["fixtures"]}
        for name in ("causal_quadrants", "causal_growth_then_confirmed_decay",
                     "causal_flip_resets", "causal_expiry_and_source_reversal"):
            self.assertIn(name, names)


# ---------------------------------------------------------------------------
# Promotion/demotion hysteresis and lifecycle maintenance.


class HysteresisTests(PipelineCase):
    def seed(self, watch: Watch, *symbols: str) -> None:
        for symbol in symbols:
            watch.symbols.setdefault(symbol, SymbolState())

    def test_focus_caps_eviction(self):
        fixture = scanner_fixture("focus_caps_eviction")
        watch = self.make_watch(
            focus_soft_cap=fixture["inputs"]["focusSoftCap"],
            focus_hard_cap=fixture["inputs"]["focusHardCap"])
        order = fixture["inputs"]["same_tick_arrival_order"]
        scores = [as_dec(gap) for gap in fixture["inputs"]["gaps_pct"]]
        self.seed(watch, *order)
        for symbol, score in zip(order, scores):
            watch.apply_decision(symbol, "focus", None, score, T0)
        holding = sorted(symbol for symbol in order
                         if watch.symbols[symbol].state == "FOCUS")
        self.assertEqual(len(holding), fixture["expected"]["active_focus_count"])
        self.assertEqual(holding, sorted(fixture["expected"]["holding_focus"]))
        for evicted in fixture["expected"]["evicted"]:
            self.assertEqual(watch.symbols[evicted].state, "WATCHING")
            self.assertEqual(watch.symbols[evicted].last_exit_reason,
                             fixture["expected"]["evicted_exit_reason"])

    def test_between_caps_needs_stronger_score_and_never_evicts(self):
        watch = self.make_watch(focus_soft_cap=1, focus_hard_cap=3)
        self.seed(watch, "AAAUSDT", "BBBUSDT", "CCCUSDT")
        watch.apply_decision("AAAUSDT", "focus", None, D("0.9"), T0)
        watch.apply_decision("BBBUSDT", "focus", None, D("0.5"), T0)
        self.assertEqual(watch.symbols["BBBUSDT"].state, "WATCHING")  # denied
        self.assertEqual(watch.symbols["AAAUSDT"].state, "FOCUS")    # kept
        watch.apply_decision("CCCUSDT", "focus", None, D("1.5"), T0)
        self.assertEqual(watch.symbols["CCCUSDT"].state, "FOCUS")    # grew pool

    def test_watch_decision_demotes_focus(self):
        watch = self.make_watch()
        self.seed(watch, "AAAUSDT")
        watch.apply_decision("AAAUSDT", "focus", None, D("1"), T0)
        self.assertEqual(watch.symbols["AAAUSDT"].state, "FOCUS")
        watch.apply_decision("AAAUSDT", "watch", None, None, T0 + 2000)
        state = watch.symbols["AAAUSDT"]
        self.assertEqual(state.state, "WATCHING")
        self.assertEqual(state.last_exit_reason, "signal_expired")
        self.assertIsNone(state.focus_since_ms)

    def test_ignore_clears_to_idle(self):
        watch = self.make_watch()
        self.seed(watch, "AAAUSDT")
        watch.apply_decision("AAAUSDT", "proposal", None, D("1"), T0)
        self.assertEqual(watch.symbols["AAAUSDT"].state, "PROPOSED")
        watch.apply_decision("AAAUSDT", "ignore", None, None, T0 + 2000)
        self.assertEqual(watch.symbols["AAAUSDT"].state, "IDLE")
        self.assertIsNone(watch.symbols["AAAUSDT"].candidate_since_ms)

    def test_advanced_reject_cools_shallow_reject_dwells(self):
        watch = self.make_watch()
        self.seed(watch, "AAAUSDT", "BBBUSDT")
        watch.apply_decision("AAAUSDT", "focus", None, D("1"), T0)
        watch.apply_decision("AAAUSDT", "reject", "data_stale", None, T0 + 2000)
        advanced = watch.symbols["AAAUSDT"]
        self.assertEqual(advanced.state, "COOLDOWN")
        self.assertEqual(advanced.cooldown_until_ms,
                         T0 + 2000 + watch.cfg["cooldown_ms"])
        watch.apply_decision("BBBUSDT", "reject", "edge_below_fees", None, T0)
        shallow = watch.symbols["BBBUSDT"]
        self.assertEqual(shallow.state, "REJECTED")
        # shallow reject returns to IDLE after > 2 ticks; cooldown holds
        watch.maintain(T0 + 2 * watch.cfg["tick_ms"] + 1)
        self.assertEqual(watch.symbols["BBBUSDT"].state, "IDLE")
        self.assertEqual(watch.symbols["AAAUSDT"].state, "COOLDOWN")
        watch.maintain(advanced.cooldown_until_ms)
        self.assertEqual(watch.symbols["AAAUSDT"].state, "IDLE")

    def test_candidate_ttl_expires_watching(self):
        watch = self.make_watch()
        self.seed(watch, "AAAUSDT")
        watch.apply_decision("AAAUSDT", "watch", None, None, T0)
        watch.maintain(T0 + watch.cfg["candidate_ttl_ms"] + 1)
        self.assertEqual(watch.symbols["AAAUSDT"].state, "IDLE")
        self.assertEqual(watch.symbols["AAAUSDT"].last_exit_reason,
                         "candidate_ttl")

    def test_focus_max_age_demotes_to_watching(self):
        watch = self.make_watch()
        self.seed(watch, "AAAUSDT")
        watch.apply_decision("AAAUSDT", "focus", None, D("1"), T0)
        watch.maintain(T0 + watch.cfg["focus_max_age_ms"] + 1)
        state = watch.symbols["AAAUSDT"]
        self.assertEqual(state.state, "WATCHING")
        self.assertEqual(state.last_exit_reason, "max_focus_time")

    def test_unknown_kind_freezes_state(self):
        watch = self.make_watch()
        self.seed(watch, "AAAUSDT")
        watch.apply_decision("AAAUSDT", "focus", None, D("1"), T0)
        watch.apply_decision("AAAUSDT", "unknown", None, None, T0 + 2000)
        self.assertEqual(watch.symbols["AAAUSDT"].state, "FOCUS")

    def test_universe_departure_prunes_state(self):
        watch = self.make_watch()
        self.tick(watch, T0 + 2000)  # TESTUSDT becomes tracked
        self.assertIn("TESTUSDT", watch.symbols)
        bundle = scenario_bundle(T0 + 4000)
        reads = FakeReads([universe_row("OTHERUSDT")], {"OTHERUSDT": bundle})
        watch.run_pass(reads, T0 + 4000)
        self.assertNotIn("TESTUSDT", watch.symbols)


# ---------------------------------------------------------------------------
# Ingestion guards: validity, future skew, cleared legs.


class IngestionTests(PipelineCase):
    def test_future_skewed_perp_ts_clears_leg(self):
        watch = self.make_watch()
        now = T0 + 2000
        row = self.tick(watch, now, perp_ts=now + 5000)  # > maxFutureSkewMs
        self.assertEqual(row["kind"], "reject")
        self.assertEqual(row["reject_code"], "perp_missing")
        observation, _ = self.kernel_log[-1]
        self.assertIsNone(observation["perp_book"])

    def test_accept_timestamp_window(self):
        self.assertTrue(accept_timestamp(10_000, 9_000, 2000))
        self.assertTrue(accept_timestamp(10_000, 12_000, 2000))   # +2s ok
        self.assertFalse(accept_timestamp(10_000, 12_001, 2000))  # too future
        self.assertFalse(accept_timestamp(10_000, None, 2000))

    def test_parse_ladder_rejects_bad_shapes(self):
        good = {"bids": [["100", "1"], ["99", "1"]],
                "asks": [["101", "1"], ["102", "1"]]}
        self.assertIsNotNone(parse_ladder(good))
        crossed = {"bids": [["102", "1"]], "asks": [["101", "1"]]}
        self.assertIsNone(parse_ladder(crossed))
        unsorted_bids = {"bids": [["99", "1"], ["100", "1"]],
                         "asks": [["101", "1"]]}
        self.assertIsNone(parse_ladder(unsorted_bids))
        negative = {"bids": [["100", "-1"]], "asks": [["101", "1"]]}
        self.assertIsNone(parse_ladder(negative))
        empty_side = {"bids": [], "asks": [["101", "1"]]}
        self.assertIsNone(parse_ladder(empty_side))
        self.assertIsNone(parse_ladder(None))

    def test_parse_mark_requires_structural_validity(self):
        good = {"markPrice": "100.8", "lastFundingRate": "0.0001",
                "nextFundingTime": 123456, "time": 123000}
        self.assertIsNotNone(parse_mark(good))
        for key in ("markPrice", "lastFundingRate", "nextFundingTime", "time"):
            broken = dict(good)
            del broken[key]
            self.assertIsNone(parse_mark(broken))
        zero_mark = dict(good, markPrice="0")
        self.assertIsNone(parse_mark(zero_mark))

    def test_parse_funding_history_keeps_order_and_bounds(self):
        payload = [{"fundingTime": 1, "fundingRate": "0.0001"},
                   {"fundingTime": 2, "fundingRate": "-0.0002"},
                   {"fundingTime": 3, "fundingRate": "nan"},
                   {"fundingRate": "0.0003"}]
        self.assertEqual(parse_funding_history(payload),
                         [{"funding_time_ms": 1,
                           "funding_rate": D("0.0001")},
                          {"funding_time_ms": 2,
                           "funding_rate": D("-0.0002")},
                          {"funding_time_ms": 3,
                           "funding_rate": None}])
        self.assertEqual(parse_funding_history(None), [])

    def test_funding_period_inference_one_and_eight_hours(self):
        one_hour = parse_funding_history(funding_history_rows(3_600_000))
        eight_hours = parse_funding_history(funding_history_rows(28_800_000))
        self.assertEqual(infer_funding_period_ms(one_hour), 3_600_000)
        self.assertEqual(infer_funding_period_ms(eight_hours), 28_800_000)

    def test_funding_period_inference_refuses_irregular_or_two_rows(self):
        irregular = parse_funding_history([
            {"fundingTime": 1, "fundingRate": "0.0001"},
            {"fundingTime": 3_600_001, "fundingRate": "0.0001"},
            {"fundingTime": 10_800_001, "fundingRate": "0.0001"},
        ])
        two_rows = parse_funding_history(funding_history_rows(3_600_000, 2))
        self.assertIsNone(infer_funding_period_ms(irregular))
        self.assertIsNone(infer_funding_period_ms(two_rows))

    def test_universe_eligible_predicate(self):
        floor = watch_config()["min_quote_volume_24h_usd"]
        self.assertTrue(universe_eligible(True, True, D("3000000"), floor))
        self.assertFalse(universe_eligible(True, True, D("2999999"), floor))
        self.assertFalse(universe_eligible(True, False, D("9000000"), floor))
        self.assertFalse(universe_eligible(True, True, None, floor))  # unknown


# ---------------------------------------------------------------------------
# Commission truth.


class CommissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = watch_config()

    def write(self, payload: Any) -> None:
        path = os.path.join(self.tmp.name, "commission-rates.json")
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(payload, str):
                handle.write(payload)
            else:
                json.dump(payload, handle)

    def test_missing_file_is_estimated_defaults(self):
        rates = read_commission_rates(self.tmp.name, "TESTUSDT", self.cfg)
        self.assertEqual(rates["source"], "ESTIMATED")
        self.assertEqual(rates["spot_taker_fee_pct"], D("0.1"))
        self.assertEqual(rates["perp_taker_fee_pct"], D("0.05"))

    def test_per_symbol_percent_shape(self):
        self.write({"symbols": {"TESTUSDT": {"spot_taker_fee_pct": "0.075",
                                             "perp_taker_fee_pct": "0.04"}}})
        rates = read_commission_rates(self.tmp.name, "TESTUSDT", self.cfg)
        self.assertEqual(rates["source"], "receipt")
        self.assertEqual(rates["spot_taker_fee_pct"], D("0.075"))
        self.assertEqual(rates["perp_taker_fee_pct"], D("0.04"))

    def test_binance_native_fraction_shape_converts_x100(self):
        self.write({"TESTUSDT": {
            "spot": {"standardCommission": {"taker": "0.001"}},
            "usdm": {"takerCommissionRate": "0.0004"}}})
        rates = read_commission_rates(self.tmp.name, "TESTUSDT", self.cfg)
        self.assertEqual(rates["source"], "receipt")
        self.assertEqual(rates["spot_taker_fee_pct"], D("0.1"))
        self.assertEqual(rates["perp_taker_fee_pct"], D("0.04"))

    def test_actual_cmd_rates_writer_shape_converts_to_kernel_percent(self):
        def request(method, _base_url, path, params, signed):
            self.assertEqual((method, params, signed),
                             ("GET", {"symbol": "TESTUSDT"}, True))
            if path == basis_position.SPOT_COMMISSION_PATH:
                return "ok", {
                    "symbol": "TESTUSDT",
                    "standardCommission": {
                        "maker": "0.0008", "taker": "0.00075"},
                }
            if path == basis_position.USDM_COMMISSION_PATH:
                return "ok", {
                    "symbol": "TESTUSDT",
                    "makerCommissionRate": "0.0002",
                    "takerCommissionRate": "0.0004",
                }
            self.fail("unexpected commission path")

        cfg = basis_position.PositionConfig(
            symbol="TESTUSDT", sha="0" * 40, receipt_dir=self.tmp.name)
        with contextlib.redirect_stdout(io.StringIO()):
            code = basis_position.cmd_rates(
                cfg, request, sleep=lambda _seconds: None, now_ms=lambda: T0)
        self.assertEqual(code, basis_position.EXIT_CODE["RATES_RECORDED"])

        path = os.path.join(self.tmp.name, "commission-rates.json")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["symbol"], "TESTUSDT")
        self.assertEqual(payload["spot"],
                         {"maker": "0.0008", "taker": "0.00075"})
        self.assertEqual(payload["usdm"],
                         {"maker": "0.0002", "taker": "0.0004"})
        self.assertNotIn("symbols", payload)

        rates = read_commission_rates(self.tmp.name, "TESTUSDT", self.cfg)
        self.assertEqual(rates["source"], "receipt")
        self.assertEqual(rates["spot_taker_fee_pct"], D("0.075"))
        self.assertEqual(rates["perp_taker_fee_pct"], D("0.04"))

    def test_default_block_used_when_symbol_absent(self):
        self.write({"default": {"spotTakerFeePct": "0.09",
                                "perpTakerFeePct": "0.045"}})
        rates = read_commission_rates(self.tmp.name, "OTHERUSDT", self.cfg)
        self.assertEqual(rates["source"], "receipt")
        self.assertEqual(rates["spot_taker_fee_pct"], D("0.09"))

    def test_malformed_file_falls_back_to_estimated(self):
        self.write("{not json")
        rates = read_commission_rates(self.tmp.name, "TESTUSDT", self.cfg)
        self.assertEqual(rates["source"], "ESTIMATED")

    def test_negative_rate_falls_back_to_estimated(self):
        self.write({"TESTUSDT": {"spot_taker_fee_pct": "-0.1",
                                 "perp_taker_fee_pct": "0.05"}})
        rates = read_commission_rates(self.tmp.name, "TESTUSDT", self.cfg)
        self.assertEqual(rates["source"], "ESTIMATED")


# ---------------------------------------------------------------------------
# Kernel boundary fail-closed behavior.


class KernelBoundaryTests(PipelineCase):
    def test_absent_kernel_yields_unknown(self):
        basis_watch.basis_kernel = None
        decision = basis_watch.kernel_evaluate({"config": watch_config()})
        self.assertEqual(decision["kind"], "unknown")
        self.assertEqual(decision["note"], "kernel_unavailable")

    def test_crashing_kernel_yields_unknown(self):
        class Boom:
            @staticmethod
            def evaluate(observation):
                raise ValueError("boom")
        basis_watch.basis_kernel = Boom()
        decision = basis_watch.kernel_evaluate({})
        self.assertEqual(decision["kind"], "unknown")
        self.assertEqual(decision["note"], "kernel_error:ValueError")

    def test_invalid_decision_shape_yields_unknown(self):
        class Weird:
            @staticmethod
            def evaluate(observation):
                return ["not", "a", "decision"]
        basis_watch.basis_kernel = Weird()
        decision = basis_watch.kernel_evaluate({})
        self.assertEqual(decision["kind"], "unknown")
        self.assertEqual(decision["note"], "kernel_decision_invalid")

    def test_unknown_pass_promotes_nothing(self):
        basis_watch.basis_kernel = None
        watch = self.make_watch()
        row = self.tick(watch, T0 + 2000)
        self.assertEqual(row["kind"], "unknown")
        self.assertEqual(row["note"], "kernel_unavailable")
        self.assertEqual(watch.symbols["TESTUSDT"].state, "IDLE")
        # observation still tracked honestly: persistence counted
        self.assertEqual(watch.symbols["TESTUSDT"].persist_ticks, 1)


# ---------------------------------------------------------------------------
# Atomic state file: round trip, restart continuity, corruption.


class StateFileTests(PipelineCase):
    def test_atomic_write_leaves_no_temp_file(self):
        watch = self.make_watch()
        self.tick(watch, T0 + 2000)
        self.assertTrue(os.path.exists(self.state_path))
        self.assertFalse(os.path.exists(self.state_path + ".tmp"))

    def test_restart_loses_nothing_and_continues_counting(self):
        watch = self.make_watch(persist_ticks=3)
        self.tick(watch, T0 + 2000)
        self.tick(watch, T0 + 4000)
        before = watch.symbols["TESTUSDT"]
        self.assertEqual(before.persist_ticks, 2)
        self.assertEqual(before.state, "FOCUS")
        # kill/restart: a NEW instance rebuilt purely from the state file
        restarted = Watch(self.state_path, self.tmp.name,
                          watch_config({"persist_ticks": 3}))
        after = restarted.symbols["TESTUSDT"]
        self.assertEqual(after.state, before.state)
        self.assertEqual(after.persist_ticks, before.persist_ticks)
        self.assertEqual(after.window, before.window)
        self.assertEqual(after.focus_since_ms, before.focus_since_ms)
        self.assertEqual(restarted.pass_count, 2)
        row = self.tick(restarted, T0 + 6000)
        self.assertEqual(row["kind"], "proposal")  # 3rd persisting tick
        self.assertEqual(row["persist_ticks"], 3)

    def test_cooldown_survives_restart(self):
        watch = self.make_watch()
        self.tick(watch, T0 + 2000)
        self.tick(watch, T0 + 4000)
        self.tick(watch, T0 + 12000, book_ts=T0 + 2000)  # stale -> COOLDOWN
        until = watch.symbols["TESTUSDT"].cooldown_until_ms
        restarted = Watch(self.state_path, self.tmp.name, watch_config())
        state = restarted.symbols["TESTUSDT"]
        self.assertEqual(state.state, "COOLDOWN")
        self.assertEqual(state.cooldown_until_ms, until)
        self.assertEqual(restarted.active_cooldown_count(T0 + 14000), 1)

    def test_corrupt_state_file_raises(self):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            handle.write("{torn")
        with self.assertRaises(WatchError):
            Watch(self.state_path, self.tmp.name, watch_config())

    def test_invalid_state_shape_raises(self):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump({"version": 99}, handle)
        with self.assertRaises(WatchError):
            Watch(self.state_path, self.tmp.name, watch_config())

    def test_missing_state_file_is_fresh_start(self):
        watch = self.make_watch()
        self.assertEqual(watch.symbols, {})
        self.assertEqual(watch.pass_count, 0)


# ---------------------------------------------------------------------------
# Config invariants.


class ConfigTests(unittest.TestCase):
    def test_watch_above_focus_rejected(self):
        with self.assertRaises(WatchError):
            watch_config({"watch_gap_pct": D("0.5")})  # > focus 0.3

    def test_small_window_rejected(self):
        with self.assertRaises(WatchError):
            watch_config({"gap_window_size": 2})

    def test_caps_order_rejected(self):
        with self.assertRaises(WatchError):
            watch_config({"focus_soft_cap": 30})  # > hard 25

    def test_defaults_are_spec_defaults(self):
        cfg = watch_config()
        guard = SPEC["scanner"]["rules"]
        by_name = {rule["name"]: rule for rule in guard}
        thresholds = by_name["guard_ladder_order"]["defaults"]
        self.assertEqual(cfg["max_spread_bps"], as_dec(thresholds["maxSpreadBps"]))
        self.assertEqual(cfg["min_depth_score"], as_dec(thresholds["minDepthScore"]))
        self.assertEqual(cfg["min_net_basis_pct"],
                         as_dec(thresholds["minNetBasisPct"]))
        self.assertEqual(cfg["max_funding_drag_pct"],
                         as_dec(thresholds["maxFundingDragPct"]))
        watch_rule = by_name["watch_and_focus_thresholds"]["defaults"]
        self.assertEqual(cfg["watch_gap_pct"], as_dec(watch_rule["watchGapPct"]))
        self.assertEqual(cfg["focus_gap_pct"], as_dec(watch_rule["focusGapPct"]))
        persistence = by_name["persistence_counting"]["defaults"]
        self.assertEqual(cfg["persist_ticks"], persistence["persistTicks"])
        self.assertEqual(cfg["tick_ms"], persistence["tickMs"])
        cooldown = by_name["cooldown_and_candidate_ttl"]["defaults"]
        self.assertEqual(cfg["cooldown_ms"], cooldown["cooldownMs"])
        self.assertEqual(cfg["candidate_ttl_ms"], cooldown["candidateTtlMs"])


# ---------------------------------------------------------------------------
# CLI: --once and --loop with STOP file.


class CliTests(PipelineCase):
    def run_main(self, argv: List[str], reads: Any) -> tuple:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = basis_watch.main(argv, reads=reads)
        return code, stdout.getvalue()

    def base_args(self) -> List[str]:
        return ["--receipt-dir", self.tmp.name, "--state-file", self.state_path]

    def test_once_prints_factual_rows_and_persists_state(self):
        reads = FakeReads([universe_row("TESTUSDT")],
                          {"TESTUSDT": scenario_bundle(T0 + 2000)})
        code, output = self.run_main(["--once"] + self.base_args(), reads)
        self.assertEqual(code, 0)
        self.assertIn("WATCH | pass 1", output)
        self.assertIn("TESTUSDT", output)
        self.assertIn("ESTIMATED", output)  # no commission receipt present
        self.assertTrue(os.path.exists(self.state_path))

    def test_default_state_is_nested_outside_shared_receipt_glob(self):
        reads = FakeReads([universe_row("TESTUSDT")],
                          {"TESTUSDT": scenario_bundle(T0 + 2000)})
        code, _output = self.run_main(
            ["--once", "--receipt-dir", self.tmp.name], reads)
        state_path = os.path.join(
            self.tmp.name, basis_watch.WATCH_STATE_DIR_NAME,
            basis_watch.STATE_FILE_NAME)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(state_path))
        self.assertNotIn(state_path, glob(os.path.join(self.tmp.name, "*.json")))

    def test_loop_honors_preexisting_stop_file(self):
        stop = os.path.join(self.tmp.name, "STOP")
        with open(stop, "w", encoding="utf-8") as handle:
            handle.write("stop\n")
        reads = FakeReads([universe_row("TESTUSDT")],
                          {"TESTUSDT": scenario_bundle(T0 + 2000)})
        code, output = self.run_main(
            ["--loop", "0.01", "--stop-file", stop] + self.base_args(), reads)
        self.assertEqual(code, 0)
        self.assertEqual(reads.universe_calls, 0)  # no pass ran
        self.assertIn("stop file present", output)

    def test_loop_stops_when_stop_file_appears(self):
        stop = os.path.join(self.tmp.name, "STOP")
        reads = StoppingReads([universe_row("TESTUSDT")],
                              {"TESTUSDT": scenario_bundle(T0 + 2000)},
                              stop, after=2)
        code, output = self.run_main(
            ["--loop", "0.01", "--stop-file", stop] + self.base_args(), reads)
        self.assertEqual(code, 0)
        self.assertEqual(reads.universe_calls, 2)
        self.assertIn("stop file present", output)

    def test_once_with_corrupt_state_fails_closed(self):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            handle.write("{torn")
        reads = FakeReads([universe_row("TESTUSDT")],
                          {"TESTUSDT": scenario_bundle(T0 + 2000)})
        code, output = self.run_main(["--once"] + self.base_args(), reads)
        self.assertEqual(code, 4)
        self.assertIn("ERROR", output)


# ---------------------------------------------------------------------------
# Differential integration: the SAME decision fixtures through the REAL B1
# kernel (basis_kernel landed in this worktree mid-wave with a typed surface;
# basis_watch._kernel_decision adapts to it).  Skipped cleanly if B1 is
# absent — the stub-driven fixture tests above still pin every behavior.

_REAL_KERNEL = basis_watch.basis_kernel
HAVE_TYPED_KERNEL = _REAL_KERNEL is not None \
    and hasattr(_REAL_KERNEL, "BasisSnapshot") \
    and hasattr(_REAL_KERNEL, "KernelConfig")


@unittest.skipUnless(HAVE_TYPED_KERNEL,
                     "B1 basis_kernel (typed surface) not present")
class KernelIntegrationTests(PipelineCase):
    def setUp(self) -> None:
        super().setUp()
        basis_watch.basis_kernel = _REAL_KERNEL  # undo the stub: real B1

    def tick_recording_kernel(self, watch: Watch, now_ms: int,
                              **bundle_kwargs: Any) -> tuple:
        calls = []

        class RecordingKernel:
            BasisSnapshot = _REAL_KERNEL.BasisSnapshot
            KernelConfig = _REAL_KERNEL.KernelConfig
            Book = _REAL_KERNEL.Book
            FundingMark = _REAL_KERNEL.FundingMark

            @staticmethod
            def evaluate(snapshot, config):
                result = _REAL_KERNEL.evaluate(snapshot, config)
                calls.append((snapshot, config, result))
                return result

        basis_watch.basis_kernel = RecordingKernel()
        try:
            row = self.tick(watch, now_ms, **bundle_kwargs)
        finally:
            basis_watch.basis_kernel = _REAL_KERNEL
        self.assertEqual(len(calls), 1)
        return (row,) + calls[0]

    def test_one_hour_period_reaches_config_and_counts_all_crossings(self):
        now = T0 + 2000
        history = funding_history_rows(3_600_000)
        row, _, config, result = self.tick_recording_kernel(
            self.make_watch(), now, funding_history=history,
            next_funding_ms=now + 60_000)
        self.assertEqual(config.funding_period_ms, 3_600_000)
        hold = result["components"]["expected_hold_ms"]
        crossings = 1 + (hold - 60_000) // config.funding_period_ms
        self.assertEqual(crossings, 4)
        self.assertIsNone(result["components"]["carry_raw_pct"])
        self.assertEqual((row["kind"], row["reject_code"]),
                         ("reject", "data_stale"))

    def test_eight_hour_period_reaches_config(self):
        row, _, config, result = self.tick_recording_kernel(
            self.make_watch(), T0 + 2000,
            funding_history=funding_history_rows(28_800_000))
        self.assertEqual(config.funding_period_ms, 28_800_000)
        self.assertIsNotNone(result["components"]["carry_raw_pct"])
        self.assertNotEqual((row["kind"], row["reject_code"]),
                            ("reject", "data_stale"))

    def test_unknown_period_clears_funding_and_refuses(self):
        histories = {
            "irregular": [
                {"fundingTime": 1, "fundingRate": "0.0001"},
                {"fundingTime": 3_600_001, "fundingRate": "0.0001"},
                {"fundingTime": 10_800_001, "fundingRate": "0.0001"},
            ],
            "two_rows": funding_history_rows(3_600_000, 2),
        }
        for index, (name, history) in enumerate(histories.items()):
            with self.subTest(name=name):
                watch = Watch(self.state_path + ".period%d" % index,
                              self.tmp.name, watch_config())
                row, snapshot, _, result = self.tick_recording_kernel(
                    watch, T0 + 2000, funding_history=history)
                self.assertIsNone(snapshot.mark)
                self.assertFalse(result["components"]["funding_fresh"])
                self.assertIsNone(result["components"]["carry_pct"])
                self.assertFalse(result["accept"])
                self.assertEqual((row["kind"], row["reject_code"]),
                                 ("reject", "data_stale"))

    def test_decision_sequence_persist3_through_real_kernel(self):
        fixture = scanner_fixture("decision_sequence_persist3")
        watch = self.make_watch(persist_ticks=3)
        kinds = []
        rows = []
        for index in range(3):
            row = self.tick(watch, T0 + 2000 * (index + 1), default_book=True)
            kinds.append(row["kind"])
            rows.append(row)
        self.assertEqual(kinds, fixture["expected"]["kinds"])
        self.assertEqual(watch.symbols["TESTUSDT"].state, "PROPOSED")
        self.assertClose(rows[-1]["gap_pct"],
                         fixture["expected"]["gross_edge_pct"])
        low, high = fixture["expected"]["net_edge_pct_range"]
        self.assertTrue(as_dec(low) < rows[-1]["net_pct"] < as_dec(high))
        self.assertClose(rows[-1]["carry_pct"], D("0.01"), D("1e-12"))

    def test_edge_rejects_through_real_kernel(self):
        watch = self.make_watch()
        row = self.tick(watch, T0 + 2000, gap_pct="0.25")
        self.assertEqual((row["kind"], row["reject_code"]),
                         ("reject", "edge_below_fees"))
        watch2 = Watch(self.state_path + ".2", self.tmp.name, watch_config())
        row2 = self.tick(watch2, T0 + 2000, gap_pct="0.45")
        self.assertEqual((row2["kind"], row2["reject_code"]),
                         ("reject", "edge_below_threshold"))
        self.assertClose(row2["net_pct"], D("0.16"), D("1e-12"))

    def test_stale_and_skew_reject_through_real_kernel(self):
        watch = self.make_watch()
        row = self.tick(watch, T0 + 12000, book_ts=T0 + 2000)
        self.assertEqual((row["kind"], row["reject_code"]),
                         ("reject", "data_stale"))
        watch2 = Watch(self.state_path + ".2", self.tmp.name, watch_config())
        now = T0 + 2000
        row2 = self.tick(watch2, now, perp_ts=now - 4000)
        self.assertEqual((row2["kind"], row2["reject_code"]),
                         ("reject", "data_stale"))

    def test_commission_receipt_flows_into_real_kernel_fees(self):
        # Cheaper measured commissions move the fee gate: gap 0.25 passes
        # fees 0.2 (receipt) but fails the ESTIMATED default 0.3.
        with open(os.path.join(self.tmp.name, "commission-rates.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"default": {"spot_taker_fee_pct": "0.06",
                                   "perp_taker_fee_pct": "0.04"}}, handle)
        watch = self.make_watch()
        row = self.tick(watch, T0 + 2000, gap_pct="0.25")
        self.assertEqual(row["fees_source"], "receipt")
        # gross 0.25 > fees 0.2: the fee gate passes, the net gate rejects
        self.assertEqual((row["kind"], row["reject_code"]),
                         ("reject", "edge_below_threshold"))


class FinalistSelectionTests(PipelineCase):
    def test_non_conforming_symbol_never_becomes_finalist(self):
        watch = self.make_watch()
        rows = [universe_row("币安人生USDT", entry_bps="99"),
                universe_row("TESTUSDT", entry_bps="78")]
        reads = FakeReads(rows, {"TESTUSDT": scenario_bundle(T0 + 2000)})
        out = watch.run_pass(reads, T0 + 2000)
        self.assertEqual([row["symbol"] for row in out], ["TESTUSDT"])

    def test_cap_unfitting_rows_are_skipped(self):
        watch = self.make_watch()
        rows = [universe_row("AAAUSDT", entry_bps="99", fits=False),
                universe_row("TESTUSDT", entry_bps="78")]
        reads = FakeReads(rows, {"TESTUSDT": scenario_bundle(T0 + 2000)})
        out = watch.run_pass(reads, T0 + 2000)
        self.assertEqual([row["symbol"] for row in out], ["TESTUSDT"])


# ---------------------------------------------------------------------------
# Reuse contract with basis_scan (universe machinery by import).


class ReuseTests(unittest.TestCase):
    def test_universe_machinery_is_imported_not_copied(self):
        self.assertIs(basis_watch.fetch_json, basis_scan.fetch_json)
        self.assertIs(basis_watch.build_rows, basis_scan.build_rows)
        self.assertIs(basis_watch.spot_universe, basis_scan.spot_universe)
        self.assertIs(basis_watch.usdm_universe, basis_scan.usdm_universe)
        self.assertIs(basis_watch.book_map, basis_scan.book_map)
        self.assertIs(basis_watch.funding_map, basis_scan.funding_map)

    def test_notional_defaults_to_the_compiled_cap(self):
        self.assertEqual(watch_config()["notional_usd"], basis_scan.CAP_USDT)


if __name__ == "__main__":
    unittest.main()
