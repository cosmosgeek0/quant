#!/usr/bin/env python3
"""Differential tests for src/basis_kernel.py against the mined old-engine spec.

The spec file tests/fixtures/old_engine_spec.json is the requirements
document: every numeric fixture of its math and funding areas (the B1
economics-kernel scope) is loaded from the JSON and reproduced here — the
ZZFixtureLedger test fails if any in-scope fixture is left unconsumed, so a
spec drift can never silently drop coverage.  A fixture clause that cannot
be reproduced inside this scope carries one written reason at its assertion
site (never silent deletion).

Tolerances follow the spec's guidance: fixtures carry float64 results of
the measured formulas, this port computes in Decimal, so numeric fixtures
compare within 1e-9 (1e-12 for the return law, exact equality for fees).

Everything here is offline and deterministic: no sockets, no clock — "now"
is a pinned epoch.
"""

import json
import os
import sys
import unittest
from decimal import Decimal

SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
)
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import basis_kernel as bk

SPEC_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "old_engine_spec.json"
)
with open(SPEC_PATH, "r", encoding="utf-8") as handle:
    SPEC = json.load(handle, parse_float=Decimal)

MATH_FIXTURES = {fx["name"]: fx for fx in SPEC["math"]["fixtures"]}
FUNDING_FIXTURES = {fx["name"]: fx for fx in SPEC["funding"]["fixtures"]}
_COVERED = {"math": set(), "funding": set()}

EPS = Decimal("1e-9")
EPS_RETURN_LAW = Decimal("1e-12")
NOW = 1_700_000_000_000
D = Decimal
CFG = bk.DEFAULT_CONFIG


def fixture(area, name):
    """Fetch one spec fixture and mark it consumed for the ledger."""
    table = MATH_FIXTURES if area == "math" else FUNDING_FIXTURES
    _COVERED[area].add(name)
    return table[name]["inputs"], table[name]["expected"]


def now_expr(raw):
    """Resolve the spec's symbolic 'now'/'now+X'/'now-X' timestamps."""
    if isinstance(raw, str) and raw.startswith("now"):
        rest = raw[3:]
        return NOW + (int(rest) if rest else 0)
    return raw


def rows(levels):
    """Spec [price, qty] arrays -> tuples (JSON floats already Decimal)."""
    return tuple((level[0], level[1]) for level in levels)


def make_book(bid, ask, bid_qty=D(500), ask_qty=D(500)):
    return bk.Book(bid=D(str(bid)), ask=D(str(ask)),
                   bid_qty=bid_qty, ask_qty=ask_qty)


def make_snapshot(**overrides):
    """Proposal-grade NORMAL snapshot mirroring the old default test book.

    spot 100/100.02, perp 100.8/100.82 (entry gap 0.7798%), generous
    ladders, pristine freshness, funding fraction 0.0001 paying in 1h
    (one settlement inside the default 4h hold -> carry 0.01%), empty
    funding history (flip risk unknown -> bonus undiscounted), persistence
    met, kill switch OFF.  Overrides replace fields.
    """
    base = dict(
        now_ms=NOW,
        spot_book=make_book("100", "100.02"),
        perp_book=make_book("100.8", "100.82"),
        spot_staleness_ms=0,
        perp_staleness_ms=0,
        leg_skew_ms=0,
        spot_ask_ladder=((D("100.02"), D(500)),),
        perp_bid_ladder=((D("100.8"), D(500)),),
        mark=bk.FundingMark(
            mark_price=D("100.81"),
            funding_rate=D("0.0001"),
            next_funding_ts=NOW + 3_600_000,
            ts=NOW,
        ),
        mark_staleness_ms=0,
        funding_history=(),
        slope_pct_per_min=D(0),
        stability_cv=None,
        persist_ticks=5,
        notional_usd=D(10),
        kill_switch_on=False,
    )
    base.update(overrides)
    return bk.BasisSnapshot(**base)


def gap_snapshot(gap_pct, **overrides):
    """Snapshot built with the old suite's generator:
    perp_bid = spot_ask * (1 + g/100), so the entry gap is exactly g."""
    spot_ask = D("100.02")
    perp_bid = spot_ask * (D(1) + D(str(gap_pct)) / D(100))
    perp_ask = perp_bid + D("0.02")
    mark = (perp_bid + perp_ask) / D(2)  # zero mark divergence
    base = dict(
        perp_book=bk.Book(bid=perp_bid, ask=perp_ask,
                          bid_qty=D(500), ask_qty=D(500)),
        perp_bid_ladder=((perp_bid, D(500)),),
        mark=bk.FundingMark(
            mark_price=mark,
            funding_rate=D("0.0001"),
            next_funding_ts=NOW + 3_600_000,
            ts=NOW,
        ),
    )
    base.update(overrides)
    return make_snapshot(**base)


class KernelAssertions(unittest.TestCase):
    def assertClose(self, actual, expected, eps=EPS, msg=None):
        if expected is None:
            self.assertIsNone(actual, msg)
            return
        self.assertIsNotNone(actual, msg or f"expected {expected}, got None")
        self.assertLessEqual(
            abs(D(actual) - D(expected)), eps,
            msg or f"{actual} not within {eps} of {expected}",
        )


# ==========================================================================
# MATH area fixtures (33)
# ==========================================================================

class MathReturnLawFixtures(KernelAssertions):
    def test_return_law_reverse_denominator(self):
        inputs, expected = fixture("math", "return_law_reverse_denominator")
        result = bk.executable_return_pct(
            inputs["sellProceedsPrice"], inputs["longEntryPrice"]
        )
        self.assertClose(result, expected["returnPct"], EPS_RETURN_LAW)
        # The classic wrong-denominator bug must NOT reproduce.
        wrong = expected["mustNotEqual_wrongDenominator"]
        self.assertGreater(abs(result - D(wrong)), D("1e-6"))

    def test_return_law_equal_prices(self):
        inputs, expected = fixture("math", "return_law_equal_prices")
        self.assertClose(
            bk.executable_return_pct(
                inputs["sellProceedsPrice"], inputs["longEntryPrice"]
            ),
            expected["returnPct"], EPS_RETURN_LAW,
        )

    def test_return_law_profitable(self):
        inputs, expected = fixture("math", "return_law_profitable")
        self.assertClose(
            bk.executable_return_pct(
                inputs["sellProceedsPrice"], inputs["longEntryPrice"]
            ),
            expected["returnPct"], EPS_RETURN_LAW,
        )

    def test_return_law_nonpositive_price_null(self):
        inputs, expected = fixture("math", "return_law_nonpositive_price_null")
        self.assertIsNone(
            bk.executable_return_pct(
                inputs["sellProceedsPrice"], inputs["longEntryPrice"]
            )
        )
        self.assertIsNone(expected["returnPct"])


class MathGapAndSpreadFixtures(KernelAssertions):
    def test_entry_gap_default_book(self):
        inputs, expected = fixture("math", "entry_gap_default_book")
        self.assertClose(
            bk.entry_gap_pct(inputs["spotAsk"], inputs["perpBid"]),
            expected["gapInPct"],
        )

    def test_exit_gap_pair_default_book(self):
        inputs, expected = fixture("math", "exit_gap_pair_default_book")
        self.assertClose(
            bk.exit_gap_pct(inputs["spotBid"], inputs["perpAsk"]),
            expected["gapOutPct"],
        )

    def test_spread_bps_spot_default(self):
        inputs, expected = fixture("math", "spread_bps_spot_default")
        self.assertClose(
            bk.spread_bps(inputs["bid"], inputs["ask"]), expected["spreadBps"]
        )

    def test_spread_bps_perp_default(self):
        inputs, expected = fixture("math", "spread_bps_perp_default")
        self.assertClose(
            bk.spread_bps(inputs["bid"], inputs["ask"]), expected["spreadBps"]
        )

    def test_spread_bps_crossed_null(self):
        inputs, expected = fixture("math", "spread_bps_crossed_null")
        self.assertIsNone(bk.spread_bps(inputs["bid"], inputs["ask"]))
        self.assertIsNone(expected["spreadBps"])

    def test_four_leg_fees_default(self):
        inputs, expected = fixture("math", "four_leg_fees_default")
        # Spec: fees compare with EXACT equality, not approx.
        self.assertEqual(
            bk.fees_pct(inputs["spotTakerFeePct"], inputs["perpTakerFeePct"]),
            D(str(expected["feesPct"])),
        )

    def test_mark_divergence_default_book_vs_95(self):
        inputs, expected = fixture("math", "mark_divergence_default_book_vs_95")
        divergence = bk.mark_divergence_bps(inputs["perpMid"], inputs["markPrice"])
        self.assertClose(divergence, expected["divergenceBps"])
        # gateOutcome: exceeds maxMarkDivergenceBps 50.
        self.assertGreater(divergence, CFG.max_mark_divergence_bps)


class MathSlippageAndDepthFixtures(KernelAssertions):
    def test_ladder_slippage_touch_covers_size(self):
        inputs, expected = fixture("math", "ladder_slippage_touch_covers_size")
        self.assertClose(
            bk.ladder_walk_slippage_pct(rows(inputs["levels"]), inputs["notionalUsd"]),
            expected["slippagePct"],
        )

    def test_ladder_slippage_two_level_walk(self):
        inputs, expected = fixture("math", "ladder_slippage_two_level_walk")
        self.assertClose(
            bk.ladder_walk_slippage_pct(rows(inputs["levels"]), inputs["notionalUsd"]),
            expected["slippagePct"],
        )

    def test_ladder_slippage_cannot_fill(self):
        inputs, expected = fixture("math", "ladder_slippage_cannot_fill")
        ladder = rows(inputs["levels"])
        walked = bk.ladder_walk_slippage_pct(ladder, inputs["notionalUsd"])
        self.assertIsNone(walked)
        self.assertIsNone(expected["walkResult"])
        self.assertClose(
            bk.leg_slippage_pct(
                ladder, inputs["notionalUsd"], None, None,
                inputs["slippagePctEstimate"],
            ),
            expected["perLegPenaltySlippagePct"],
        )

    def test_top_of_book_slippage_2x_consumption(self):
        inputs, expected = fixture("math", "top_of_book_slippage_2x_consumption")
        estimate = inputs["baseSlippagePct"] * 2  # base = estimate/2
        self.assertClose(
            bk.leg_slippage_pct(
                None, inputs["notionalUsd"], inputs["touchPx"],
                inputs["touchQty"], estimate,
            ),
            expected["slippagePct"],
        )

    def test_top_of_book_slippage_invalid_inputs(self):
        inputs, expected = fixture("math", "top_of_book_slippage_invalid_inputs")
        estimate = inputs["baseSlippagePct"] * 2
        self.assertClose(
            bk.leg_slippage_pct(
                None, inputs["notionalUsd"], inputs["touchPx"],
                inputs["touchQty"], estimate,
            ),
            expected["slippagePct"],
        )

    def test_depth_score_10x_coverage_saturates(self):
        inputs, expected = fixture("math", "depth_score_10x_coverage_saturates")
        ladder = rows(inputs["levels"])
        self.assertClose(
            bk.depth_score(ladder, ladder, None, None, inputs["notionalUsd"]),
            expected["depthScore"],
        )

    def test_depth_score_1x_coverage(self):
        inputs, expected = fixture("math", "depth_score_1x_coverage")
        ladder = rows(inputs["levels"])
        score = bk.depth_score(ladder, ladder, None, None, inputs["notionalUsd"])
        self.assertClose(score, expected["depthScore"])
        # note: below the 0.3 default gate.
        self.assertLess(score, CFG.min_depth_score)

    def test_depth_score_thin_spot_leg(self):
        inputs, expected = fixture("math", "depth_score_thin_spot_leg")
        thin = rows(inputs["levels"])
        generous = ((D(100), D(500)),)
        score = bk.depth_score(thin, generous, None, None, inputs["notionalUsd"])
        self.assertClose(score, expected["depthScore"])
        # gateOutcome: depth_too_thin at minDepthScore 0.3.
        self.assertLess(score, CFG.min_depth_score)


class MathFundingFixtures(KernelAssertions):
    def _carry(self, inputs):
        return bk.funding_carry_pct(
            inputs["holdMs"], inputs["msToNextFunding"], inputs["periodMs"],
            inputs["knownRates"], inputs["direction"],
        )

    def test_funding_carry_no_settlement_crossed(self):
        inputs, expected = fixture("math", "funding_carry_no_settlement_crossed")
        self.assertClose(self._carry(inputs), expected["carryPct"])

    def test_funding_carry_one_settlement(self):
        inputs, expected = fixture("math", "funding_carry_one_settlement")
        self.assertClose(self._carry(inputs), expected["carryPct"])

    def test_funding_carry_unknown_second_settlement_null(self):
        inputs, expected = fixture(
            "math", "funding_carry_unknown_second_settlement_null"
        )
        self.assertIsNone(self._carry(inputs))
        self.assertIsNone(expected["carryPct"])
        # The spec documents WHY: 3 crossings vs 1 known rate.
        crossings = 1 + (
            (inputs["holdMs"] - inputs["msToNextFunding"]) // inputs["periodMs"]
        )
        self.assertEqual(crossings, expected["crossings"])
        self.assertEqual(len(inputs["knownRates"]), expected["knownRatesCount"])

    def test_funding_carry_two_known_settlements_sum(self):
        inputs, expected = fixture("math", "funding_carry_two_known_settlements_sum")
        self.assertClose(self._carry(inputs), expected["carryPct"])

    def test_funding_carry_insane_rate_null(self):
        inputs, expected = fixture("math", "funding_carry_insane_rate_null")
        self.assertIsNone(self._carry(inputs))
        self.assertIsNone(expected["carryPct"])
        # reason: abs(rate) > 0.1 sanity cap (fraction per period).
        self.assertGreater(abs(inputs["knownRates"][0]), CFG.rate_sanity_abs_max)

    def test_funding_carry_negative_drag_boundary_equal_msToNext(self):
        inputs, expected = fixture(
            "math", "funding_carry_negative_drag_boundary_equal_msToNext"
        )
        carry = self._carry(inputs)
        # msToNext == hold still crosses the settlement.
        self.assertClose(carry, expected["carryPct"])
        # note: drag 1.0 > maxFundingDragPct 0.15 rejects funding_against_trade.
        drag = max(D(0), -carry)
        self.assertGreater(drag, CFG.max_funding_drag_pct)

    def test_flip_risk_alternating_history(self):
        inputs, expected = fixture("math", "flip_risk_alternating_history")
        self.assertClose(
            bk.flip_risk(inputs["historyRates"], inputs["currentRate"]),
            expected["flipRisk"],
        )


class MathHoldAndStabilityFixtures(KernelAssertions):
    def test_expected_hold_closing_clamped_to_min(self):
        inputs, expected = fixture("math", "expected_hold_closing_clamped_to_min")
        cfg = bk.KernelConfig(
            min_hold_ms=inputs["minHoldMs"],
            default_hold_ms=inputs["defaultHoldMs"],
            max_hold_ms=inputs["maxHoldMs"],
        )
        self.assertClose(
            bk.expected_hold_ms(inputs["gapPct"], inputs["slopePctPerMin"], cfg),
            expected["holdMs"],
        )
        raw = inputs["gapPct"] / abs(inputs["slopePctPerMin"]) * 60_000
        self.assertClose(raw, expected["rawConvergenceMs"])

    def test_expected_hold_flat_uses_default(self):
        inputs, expected = fixture("math", "expected_hold_flat_uses_default")
        cfg = bk.KernelConfig(
            min_hold_ms=inputs["minHoldMs"],
            default_hold_ms=inputs["defaultHoldMs"],
            max_hold_ms=inputs["maxHoldMs"],
        )
        self.assertClose(
            bk.expected_hold_ms(inputs["gapPct"], inputs["slopePctPerMin"], cfg),
            expected["holdMs"],
        )

    def test_slope_per_minute_two_points(self):
        inputs, expected = fixture("math", "slope_per_minute_two_points")
        samples = [(sample["ts"], sample["v"]) for sample in inputs["samples"]]
        self.assertClose(bk.slope_pct_per_min(samples), expected["slopePctPerMin"])

    def test_stability_cv_flickering_gap(self):
        inputs, expected = fixture("math", "stability_cv_flickering_gap")
        mean, std_dev, cv = bk.gap_stats(inputs["windowValues"])
        self.assertClose(mean, expected["mean"])
        self.assertClose(std_dev, expected["stdDevSample"])
        self.assertClose(cv, expected["cv"])
        # gateOutcome: cv > maxGapCv 0.6 while persistence short rejects
        # gap_not_persistent.
        self.assertGreater(cv, CFG.max_gap_cv)
        verdict = bk.evaluate(make_snapshot(stability_cv=cv, persist_ticks=1))
        self.assertFalse(verdict["accept"])
        self.assertEqual(verdict["reason"], "gap_not_persistent")


class MathNetEdgeFixtures(KernelAssertions):
    def test_net_edge_full_pipeline_default_proposal(self):
        inputs, expected = fixture("math", "net_edge_full_pipeline_default_proposal")
        gross = bk.entry_gap_pct(inputs["spotAsk"], inputs["perpBid"])
        hold = bk.expected_hold_ms(gross, D(0))  # flat gap -> default 4h hold
        raw_carry = bk.funding_carry_pct(
            hold, 60_000, CFG.funding_period_ms,
            [inputs["fundingRateFractionPerPeriod"]], bk.DIRECTION_NORMAL,
        )
        carry = bk.discounted_carry_pct(raw_carry, inputs["flipRisk"])
        fees = bk.fees_pct(inputs["spotTakerFeePct"], inputs["perpTakerFeePct"])
        edge = bk.net_edge(
            gross, fees, inputs["slippagePct"], carry, inputs["borrowCostPct"]
        )
        self.assertClose(edge.gross_pct, expected["grossPct"])
        self.assertClose(edge.carry_pct, expected["carryPct"])
        self.assertClose(edge.total_costs_pct, expected["totalCostsPct"])
        self.assertClose(edge.net_pct, expected["netPct"])
        # oldTestAssertion: net in (0.45, 0.52), feesPct == 0.3 exactly.
        self.assertTrue(D("0.45") < edge.net_pct < D("0.52"))
        self.assertEqual(edge.fees_pct, D("0.3"))
        # settlementsCrossed 1, as constructed.
        self.assertEqual(inputs["settlementsCrossed"], 1)

    def test_net_edge_unknown_carry_stays_null(self):
        inputs, expected = fixture("math", "net_edge_unknown_carry_stays_null")
        edge = bk.net_edge(
            inputs["grossPct"], inputs["feesPct"], inputs["slippagePct"],
            inputs["carryPct"], inputs["borrowCostPct"],
        )
        # note: costs assembled with carry treated as 0, net still null.
        self.assertIsNone(edge.net_pct)
        self.assertIsNone(expected["netPct"])
        self.assertClose(edge.total_costs_pct, expected["totalCostsPct"])
        self.assertEqual(edge.funding_drag_pct, D(0))

    def test_entry_and_exit_thresholds_by_construction(self):
        inputs, expected = fixture(
            "math", "entry_and_exit_thresholds_by_construction"
        )
        self.assertEqual(inputs["spotAsk"], D("100.02"))
        gaps = inputs["grossGapExamplesPct"]
        self.assertEqual(gaps, [D("0.25"), D("0.45"), D("0.6"), D("0.12")])
        # g=0.25 -> reject edge_below_fees (gross <= fees 0.3 + slippage 0).
        verdict = bk.evaluate(gap_snapshot("0.25"))
        self.assertEqual(verdict["reason"], "edge_below_fees")
        self.assertClose(verdict["components"]["gap_pct"], D("0.25"))
        # g=0.45 -> net 0.16 < 0.25 rejects edge_below_threshold.
        verdict = bk.evaluate(gap_snapshot("0.45"))
        self.assertEqual(verdict["reason"], "edge_below_threshold")
        self.assertClose(verdict["components"]["net_pct"], D("0.16"))
        # g=0.60 -> proposal.  The fixture's "survives refresh
        # (proposal_updated)" clause is proposal-object behavior owned by
        # the lifecycle area, not this stateless kernel; reproduced here as
        # the accepting entry decision only.
        verdict = bk.evaluate(gap_snapshot("0.6"))
        self.assertTrue(verdict["accept"])
        self.assertEqual(verdict["reason"], "proposal")
        # g=0.12 -> below watchGapPct 0.15.  The fixture's "kills proposal
        # with gap_closed" clause is the exit sweep (lifecycle area); the
        # entry ladder's decision for the same book is ignore, asserted here.
        verdict = bk.evaluate(gap_snapshot("0.12"))
        self.assertEqual(verdict["reason"], "ignore")


# ==========================================================================
# FUNDING area fixtures (47)
# ==========================================================================

class FundingHoldFixtures(KernelAssertions):
    def _hold(self, inputs):
        return bk.expected_hold_ms(inputs["gapPct"], inputs["slopePctPerMin"])

    def test_hold_closing_fast_clamped_to_min(self):
        inputs, expected = fixture("funding", "hold_closing_fast_clamped_to_min")
        self.assertClose(self._hold(inputs), expected["expectedHoldMs"])

    def test_hold_closing_slow(self):
        inputs, expected = fixture("funding", "hold_closing_slow")
        self.assertClose(self._hold(inputs), expected["expectedHoldMs"])

    def test_hold_widening_uses_default(self):
        inputs, expected = fixture("funding", "hold_widening_uses_default")
        self.assertClose(self._hold(inputs), expected["expectedHoldMs"])

    def test_hold_slope_within_epsilon_uses_default(self):
        inputs, expected = fixture(
            "funding", "hold_slope_within_epsilon_uses_default"
        )
        self.assertClose(self._hold(inputs), expected["expectedHoldMs"])


class FundingFlipFixtures(KernelAssertions):
    def _flip(self, inputs):
        return bk.flip_risk(inputs["historyRates"], inputs["current"])

    def test_flip_stable_small_magnitude(self):
        inputs, expected = fixture("funding", "flip_stable_small_magnitude")
        self.assertClose(self._flip(inputs), expected["flipRisk"])

    def test_flip_alternating(self):
        inputs, expected = fixture("funding", "flip_alternating")
        self.assertClose(self._flip(inputs), expected["flipRisk"])

    def test_flip_needs_three_points(self):
        inputs, expected = fixture("funding", "flip_needs_three_points")
        self.assertIsNone(self._flip(inputs))
        self.assertIsNone(expected["flipRisk"])

    def test_flip_strong_stable_rate_is_zero_risk(self):
        inputs, expected = fixture("funding", "flip_strong_stable_rate_is_zero_risk")
        self.assertClose(self._flip(inputs), expected["flipRisk"])

    def test_flip_zero_transitions_not_counted_current_null_uses_last(self):
        inputs, expected = fixture(
            "funding", "flip_zero_transitions_not_counted_current_null_uses_last"
        )
        self.assertClose(self._flip(inputs), expected["flipRisk"])


class FundingCarryFixtures(KernelAssertions):
    def _carry(self, inputs):
        return bk.funding_carry_pct(
            inputs["holdMs"], inputs["msToNextFunding"], inputs["periodMs"],
            inputs["rates"], inputs["direction"],
        )

    def test_carry_hold_ends_before_settlement(self):
        inputs, expected = fixture("funding", "carry_hold_ends_before_settlement")
        self.assertClose(self._carry(inputs), expected["carryPct"])

    def test_carry_one_settlement_direction_A(self):
        inputs, expected = fixture("funding", "carry_one_settlement_direction_A")
        self.assertClose(self._carry(inputs), expected["carryPct"])

    def test_carry_one_settlement_direction_B_sign_flips(self):
        inputs, expected = fixture(
            "funding", "carry_one_settlement_direction_B_sign_flips"
        )
        self.assertClose(self._carry(inputs), expected["carryPct"])

    def test_carry_unknown_second_settlement_is_null(self):
        inputs, expected = fixture(
            "funding", "carry_unknown_second_settlement_is_null"
        )
        self.assertIsNone(self._carry(inputs))
        self.assertIsNone(expected["carryPct"])

    def test_carry_two_known_settlements_sum(self):
        inputs, expected = fixture("funding", "carry_two_known_settlements_sum")
        self.assertClose(self._carry(inputs), expected["carryPct"])

    def test_carry_nonpositive_msToNext_is_null(self):
        inputs, expected = fixture("funding", "carry_nonpositive_msToNext_is_null")
        self.assertIsNone(self._carry(inputs))
        self.assertIsNone(expected["carryPct"])

    def test_carry_rate_beyond_10pct_band_is_null(self):
        inputs, expected = fixture("funding", "carry_rate_beyond_10pct_band_is_null")
        self.assertIsNone(self._carry(inputs))
        self.assertIsNone(expected["carryPct"])


class FundingContextFixtures(KernelAssertions):
    def _context(self, inputs):
        mark_raw = inputs["mark"]
        mark = bk.FundingMark(
            mark_price=mark_raw["markPrice"],
            funding_rate=mark_raw["fundingRate"],
            next_funding_ts=now_expr(mark_raw["nextFundingTs"]),
            ts=now_expr(mark_raw["ts"]),
        )
        gap = inputs.get("gap") or {}
        return bk.funding_context(
            mark,
            inputs["markStalenessMs"],
            inputs.get("historyRates", ()),
            inputs.get("direction", bk.DIRECTION_NORMAL),
            gap.get("gapPct"),
            gap.get("slopePctPerMin"),
            NOW,
        )

    def test_ctx_fresh_paying_capture(self):
        inputs, expected = fixture("funding", "ctx_fresh_paying_capture")
        ctx = self._context(inputs)
        self.assertTrue(ctx.fresh)
        self.assertClose(ctx.ms_to_funding, expected["msToFunding"])
        self.assertClose(ctx.carry_pct, expected["carryPctOverHold"])
        self.assertClose(ctx.expected_hold_ms, expected["expectedHoldMs"])
        self.assertClose(ctx.flip_risk, expected["flipRisk"])
        self.assertClose(ctx.capture_factor, expected["captureFactor"])

    def test_ctx_flip_risky_bonus_discounted(self):
        inputs, expected = fixture("funding", "ctx_flip_risky_bonus_discounted")
        ctx = self._context(inputs)
        self.assertTrue(ctx.fresh)
        self.assertClose(ctx.flip_risk, expected["flipRisk"])
        self.assertClose(ctx.carry_pct, expected["carryPctOverHold"])
        self.assertClose(ctx.capture_factor, expected["captureFactor"])

    def test_ctx_negative_carry_never_discounted(self):
        inputs, expected = fixture("funding", "ctx_negative_carry_never_discounted")
        ctx = self._context(inputs)
        self.assertTrue(ctx.fresh)
        self.assertClose(ctx.flip_risk, expected["flipRisk"])
        self.assertClose(ctx.carry_pct, expected["carryPctOverHold"])
        self.assertClose(ctx.capture_factor, expected["captureFactor"])

    def test_ctx_stale_mark_all_null(self):
        inputs, expected = fixture("funding", "ctx_stale_mark_all_null")
        self.assertEqual(inputs["cfg"]["fundingStaleMs"], CFG.funding_stale_ms)
        ctx = self._context(inputs)
        self.assertFalse(ctx.fresh)
        self.assertIsNone(ctx.funding_rate)
        self.assertIsNone(expected["fundingRate"])
        self.assertIsNone(ctx.carry_pct)
        self.assertIsNone(ctx.flip_risk)
        self.assertClose(ctx.capture_factor, expected["captureFactor"])
        self.assertClose(ctx.staleness_ms, expected["stalenessMs"])
        self.assertClose(ctx.expected_hold_ms, expected["expectedHoldMs"])

    def test_ctx_past_nextFundingTs_not_fresh(self):
        inputs, expected = fixture("funding", "ctx_past_nextFundingTs_not_fresh")
        ctx = self._context(inputs)
        self.assertFalse(ctx.fresh)
        self.assertIsNone(ctx.carry_pct)
        self.assertIsNone(expected["carryPctOverHold"])

    def test_ctx_rate_out_of_band_not_fresh(self):
        inputs, expected = fixture("funding", "ctx_rate_out_of_band_not_fresh")
        ctx = self._context(inputs)
        self.assertFalse(ctx.fresh)
        self.assertIsNone(ctx.carry_pct)
        self.assertIsNone(expected["carryPctOverHold"])

    def test_ctx_settlement_beyond_hold_zero_carry_zero_capture(self):
        inputs, expected = fixture(
            "funding", "ctx_settlement_beyond_hold_zero_carry_zero_capture"
        )
        ctx = self._context(inputs)
        self.assertTrue(ctx.fresh)
        self.assertClose(ctx.carry_pct, expected["carryPctOverHold"])
        self.assertClose(ctx.capture_factor, expected["captureFactor"])


class FundingWalkAndDepthFixtures(KernelAssertions):
    def test_walk_two_levels_average_fill(self):
        inputs, expected = fixture("funding", "walk_two_levels_average_fill")
        self.assertClose(
            bk.ladder_walk_slippage_pct(rows(inputs["ladder"]), inputs["notionalUsd"]),
            expected["slippagePct"],
        )

    def test_walk_ladder_too_thin_null(self):
        inputs, expected = fixture("funding", "walk_ladder_too_thin_null")
        self.assertIsNone(
            bk.ladder_walk_slippage_pct(rows(inputs["ladder"]), inputs["notionalUsd"])
        )
        self.assertIsNone(expected["slippagePct"])

    def test_walk_single_level_covers_zero_slippage(self):
        inputs, expected = fixture("funding", "walk_single_level_covers_zero_slippage")
        self.assertClose(
            bk.ladder_walk_slippage_pct(rows(inputs["ladder"]), inputs["notionalUsd"]),
            expected["slippagePct"],
        )

    def test_tob_fallback_half_visible_consumed(self):
        inputs, expected = fixture("funding", "tob_fallback_half_visible_consumed")
        estimate = inputs["baseSlippagePct"] * 2
        self.assertClose(
            bk.leg_slippage_pct(
                None, inputs["notionalUsd"], inputs["touchPx"],
                inputs["touchQty"], estimate,
            ),
            expected["slippagePct"],
        )

    def test_tob_fallback_invalid_touch_4x_base(self):
        inputs, expected = fixture("funding", "tob_fallback_invalid_touch_4x_base")
        estimate = inputs["baseSlippagePct"] * 2
        self.assertClose(
            bk.leg_slippage_pct(
                None, inputs["notionalUsd"], inputs["touchPx"],
                inputs["touchQty"], estimate,
            ),
            expected["slippagePct"],
        )

    def test_depthscore_10x_coverage_full(self):
        inputs, expected = fixture("funding", "depthscore_10x_coverage_full")
        ladder = rows(inputs["ladder"])
        self.assertClose(
            bk.depth_score(ladder, ladder, None, None, inputs["notionalUsd"]),
            expected["depthScore"],
        )

    def test_depthscore_1x_coverage(self):
        inputs, expected = fixture("funding", "depthscore_1x_coverage")
        ladder = rows(inputs["ladder"])
        self.assertClose(
            bk.depth_score(ladder, ladder, None, None, inputs["notionalUsd"]),
            expected["depthScore"],
        )

    def test_depthscore_fifth_coverage(self):
        inputs, expected = fixture("funding", "depthscore_fifth_coverage")
        ladder = rows(inputs["ladder"])
        self.assertClose(
            bk.depth_score(ladder, ladder, None, None, inputs["notionalUsd"]),
            expected["depthScore"],
        )


class FundingLiquidityFixtures(KernelAssertions):
    """The liq_A_* composites: NORMAL consumes spot ASKS + perp BIDS."""

    def _generous(self):
        return dict(
            spot_book=make_book("100", "100.02"),
            perp_book=make_book("100.8", "100.82"),
            spot_ask_ladder=((D("100.02"), D(500)),),
            perp_bid_ladder=((D("100.8"), D(500)),),
            notional=D(10),
        )

    def _assess(self, parts):
        return bk.assess_liquidity(
            parts["spot_book"], parts["perp_book"],
            parts["spot_ask_ladder"], parts["perp_bid_ladder"], parts["notional"],
        )

    def _check(self, liquidity, expected):
        self.assertClose(liquidity.depth_score, expected["depthScore"])
        self.assertClose(liquidity.slippage_pct, expected["slippagePct"])
        self.assertEqual(liquidity.ladder_backed, expected["ladderBacked"])
        self.assertClose(liquidity.book_pressure, expected["bookPressure"])

    def test_liq_A_generous_ladders(self):
        inputs, expected = fixture("funding", "liq_A_generous_ladders")
        spot = inputs["spotBook"]
        perp = inputs["perpBook"]
        parts = dict(
            spot_book=bk.Book(spot["bid"], spot["ask"], spot["bidQty"], spot["askQty"]),
            perp_book=bk.Book(perp["bid"], perp["ask"], perp["bidQty"], perp["askQty"]),
            spot_ask_ladder=rows(inputs["spotDepth"]["asks"]),
            perp_bid_ladder=rows(inputs["perpDepth"]["bids"]),
            notional=inputs["notionalUsd"],
        )
        self._check(self._assess(parts), expected)

    def test_liq_A_thin_ladders_penalty_slippage(self):
        _, expected = fixture("funding", "liq_A_thin_ladders_penalty_slippage")
        # sameAs liq_A_generous_ladders except all ladder qtys 0.02.
        parts = self._generous()
        parts["spot_ask_ladder"] = ((D("100.02"), D("0.02")),)
        parts["perp_bid_ladder"] = ((D("100.8"), D("0.02")),)
        self._check(self._assess(parts), expected)

    def test_liq_A_no_ladders_tob_model(self):
        inputs, expected = fixture("funding", "liq_A_no_ladders_tob_model")
        spot = inputs["spotBook"]
        perp = inputs["perpBook"]
        parts = dict(
            spot_book=bk.Book(spot["bid"], spot["ask"], spot["bidQty"], spot["askQty"]),
            perp_book=bk.Book(perp["bid"], perp["ask"], perp["bidQty"], perp["askQty"]),
            spot_ask_ladder=None,
            perp_bid_ladder=None,
            notional=inputs["notionalUsd"],
        )
        self._check(self._assess(parts), expected)

    def test_liq_A_walked_slippage_and_min_leg(self):
        inputs, expected = fixture("funding", "liq_A_walked_slippage_and_min_leg")
        spot = inputs["spotBook"]
        perp = inputs["perpBook"]
        parts = dict(
            spot_book=bk.Book(spot["bid"], spot["ask"], spot["bidQty"], spot["askQty"]),
            perp_book=bk.Book(perp["bid"], perp["ask"], perp["bidQty"], perp["askQty"]),
            spot_ask_ladder=rows(inputs["spotDepth"]["asks"]),
            perp_bid_ladder=rows(inputs["perpDepth"]["bids"]),
            notional=inputs["notionalUsd"],
        )
        self._check(self._assess(parts), expected)

    def test_liq_A_book_pressure_imbalance(self):
        inputs, expected = fixture("funding", "liq_A_book_pressure_imbalance")
        spot = inputs["spotBook"]
        perp = inputs["perpBook"]
        parts = dict(
            spot_book=bk.Book(spot["bid"], spot["ask"], spot["bidQty"], spot["askQty"]),
            perp_book=bk.Book(perp["bid"], perp["ask"], perp["bidQty"], perp["askQty"]),
            spot_ask_ladder=None,
            perp_bid_ladder=None,
            notional=inputs["notionalUsd"],
        )
        self._check(self._assess(parts), expected)

    def test_liq_zero_notional_null_score(self):
        _, expected = fixture("funding", "liq_zero_notional_null_score")
        # sameAs liq_A_generous_ladders except notionalUsd 0: unknown depth,
        # thin-ladder penalty slippage on both legs (walks cannot price 0).
        parts = self._generous()
        parts["notional"] = D(0)
        self._check(self._assess(parts), expected)


class FundingDataQualityFixtures(KernelAssertions):
    def _pristine(self):
        return dict(
            spot_staleness_ms=0,
            perp_staleness_ms=0,
            mark_staleness_ms=0,
            leg_skew_ms=0,
            perp_book=make_book("100.8", "100.82"),
            mark_price=D("100.81"),
            ladder_backed=True,
        )

    def _quality(self, parts):
        return bk.data_quality(
            parts["spot_staleness_ms"], parts["perp_staleness_ms"],
            parts["mark_staleness_ms"], parts["leg_skew_ms"],
            parts["perp_book"], parts["mark_price"], parts["ladder_backed"],
        )

    def test_dq_pristine(self):
        inputs, expected = fixture("funding", "dq_pristine")
        self.assertEqual(inputs["cfg"], "defaults")
        quality = self._quality(self._pristine())
        self.assertClose(quality.score, expected["score"])
        self.assertClose(quality.mark_divergence_bps, expected["markDivergenceBps"])
        self.assertEqual(list(quality.suspect_reasons), expected["suspectReasons"])

    def test_dq_mark_divergence_zeroes_component(self):
        _, expected = fixture("funding", "dq_mark_divergence_zeroes_component")
        parts = self._pristine()
        parts["mark_price"] = D(95)
        quality = self._quality(parts)
        self.assertClose(quality.score, expected["score"])
        self.assertClose(quality.mark_divergence_bps, expected["markDivergenceBps"])
        self.assertEqual(list(quality.suspect_reasons), expected["suspectReasons"])

    def test_dq_mark_never_seen_zero_score(self):
        _, expected = fixture("funding", "dq_mark_never_seen_zero_score")
        parts = self._pristine()
        parts["mark_staleness_ms"] = None
        parts["mark_price"] = None
        quality = self._quality(parts)
        self.assertClose(quality.score, expected["score"])
        self.assertEqual(list(quality.suspect_reasons), expected["suspectReasons"])

    def test_dq_unknown_skew_half_component(self):
        _, expected = fixture("funding", "dq_unknown_skew_half_component")
        parts = self._pristine()
        parts["leg_skew_ms"] = None
        quality = self._quality(parts)
        self.assertClose(quality.score, expected["score"])
        self.assertEqual(list(quality.suspect_reasons), expected["suspectReasons"])

    def test_dq_no_ladder_no_mark(self):
        _, expected = fixture("funding", "dq_no_ladder_no_mark")
        parts = self._pristine()
        parts["ladder_backed"] = False
        parts["perp_book"] = None
        parts["mark_price"] = None
        quality = self._quality(parts)
        self.assertClose(quality.score, expected["score"])
        self.assertEqual(list(quality.suspect_reasons), expected["suspectReasons"])

    def test_dq_half_stale_book_backbone_min(self):
        _, expected = fixture("funding", "dq_half_stale_book_backbone_min")
        parts = self._pristine()
        parts["spot_staleness_ms"] = 2500
        parts["perp_staleness_ms"] = 1000
        self.assertClose(self._quality(parts).score, expected["score"])

    def test_dq_skew_over_limit(self):
        _, expected = fixture("funding", "dq_skew_over_limit")
        parts = self._pristine()
        parts["leg_skew_ms"] = 4000
        quality = self._quality(parts)
        self.assertClose(quality.score, expected["score"])
        self.assertEqual(list(quality.suspect_reasons), expected["suspectReasons"])


class FundingEndToEndFixtures(KernelAssertions):
    """The three e2e_* fixtures: reject codes are INFERRED in the spec
    (mined from engine-basis.mjs assertions); reproduced here through this
    kernel's evaluate() ladder."""

    def test_e2e_min_liquidity_refusal(self):
        inputs, expected = fixture("funding", "e2e_min_liquidity_refusal")
        verdict = bk.evaluate(make_snapshot(
            spot_ask_ladder=((D("100.02"), D("0.02")),),
            perp_bid_ladder=((D("100.8"), D("0.02")),),
        ))
        self.assertFalse(verdict["accept"])
        self.assertEqual(expected["decision"], "reject")
        self.assertEqual(verdict["reason"], expected["rejectCode"])
        self.assertClose(
            verdict["components"]["depth_score"], inputs["depthScoreComputed"]
        )
        self.assertLess(
            verdict["components"]["depth_score"], CFG.min_depth_score
        )

    def test_e2e_funding_drag_refusal(self):
        _, expected = fixture("funding", "e2e_funding_drag_refusal")
        # gap 0.78% direction A, fundingRate -0.01, nextFundingTs now+4h,
        # hold 4h (default): the settlement at exactly hold-end still
        # crosses, drag 1.0% > maxFundingDragPct 0.15%.
        verdict = bk.evaluate(make_snapshot(
            mark=bk.FundingMark(
                mark_price=D("100.81"),
                funding_rate=D("-0.01"),
                next_funding_ts=NOW + 14_400_000,
                ts=NOW,
            ),
        ))
        self.assertFalse(verdict["accept"])
        self.assertEqual(expected["decision"], "reject")
        self.assertEqual(verdict["reason"], expected["rejectCode"])
        self.assertClose(
            verdict["components"]["carry_pct"], expected["carryPctOverHold"]
        )

    def test_e2e_unknown_carry_keeps_net_edge_null(self):
        _, expected = fixture("funding", "e2e_unknown_carry_keeps_net_edge_null")
        # Fresh funding context, but the 12h hold crosses 2 settlements and
        # only the current rate is known -> carry null -> net null ->
        # data_stale ("funding unavailable").
        cfg = bk.KernelConfig(default_hold_ms=43_200_000)
        verdict = bk.evaluate(make_snapshot(
            mark=bk.FundingMark(
                mark_price=D("100.81"),
                funding_rate=D("0.0001"),
                next_funding_ts=NOW + 60_000,
                ts=NOW,
            ),
        ), cfg)
        self.assertTrue(verdict["components"]["funding_fresh"])
        self.assertIsNone(verdict["components"]["carry_pct"])
        self.assertIsNone(verdict["components"]["net_pct"])
        self.assertIsNone(expected["netEdgePct"])
        self.assertFalse(verdict["accept"])
        self.assertEqual(verdict["reason"], "data_stale")
        self.assertIn("data_stale", expected["scannerOutcome"])


# ==========================================================================
# Unit conventions (spec rule unit_conventions, KEEP: percent vs bps vs
# fraction is the named highest-risk porting defect — tested explicitly)
# ==========================================================================

class UnitConventionTests(KernelAssertions):
    def test_gaps_are_percent_not_bps_not_fraction(self):
        gap = bk.entry_gap_pct(D(100), D("100.25"))
        self.assertEqual(gap, D("0.25"))       # percent
        self.assertNotEqual(gap, D("25"))      # not bps
        self.assertNotEqual(gap, D("0.0025"))  # not a fraction

    def test_spreads_are_bps_not_percent(self):
        # Same 0.02-wide book: ~2 bps, which is ~0.02% — the x100 gulf.
        spread = bk.spread_bps(D(100), D("100.02"))
        self.assertClose(spread, D("1.9998000199976025"))
        pct_of_mid = (D("100.02") - D(100)) / D("100.01") * D(100)
        self.assertClose(spread, pct_of_mid * 100, D("1e-6"))

    def test_mark_divergence_is_bps(self):
        # 0.5 on a 100 mark = 0.5% = 50 bps.
        self.assertClose(bk.mark_divergence_bps(D("100.5"), D(100)), D(50))

    def test_funding_rate_fraction_becomes_percent_carry(self):
        # 0.0001 fraction/period = 1 bp = 0.01% -> carry x100 inside.
        carry = bk.funding_carry_pct(
            14_400_000, 60_000, 28_800_000, [D("0.0001")], bk.DIRECTION_NORMAL
        )
        self.assertEqual(carry, D("0.01"))     # percent
        self.assertNotEqual(carry, D("0.0001"))
        self.assertNotEqual(carry, D(1))

    def test_rate_sanity_cap_is_a_fraction_ten_percent_per_period(self):
        at_cap = bk.funding_carry_pct(
            14_400_000, 60_000, 28_800_000, [D("0.1")], bk.DIRECTION_NORMAL
        )
        self.assertEqual(at_cap, D(10))        # 0.1 fraction -> 10%
        beyond = bk.funding_carry_pct(
            14_400_000, 60_000, 28_800_000, [D("0.100001")], bk.DIRECTION_NORMAL
        )
        self.assertIsNone(beyond)

    def test_flip_magnitude_anchor_is_a_fraction(self):
        # |rate| = 0.0005 fraction (5 bps/period) zeroes the magnitude term.
        risk = bk.flip_risk([D("0.0005")] * 3, D("0.0005"))
        self.assertEqual(risk, D(0))

    def test_fees_are_percent_per_leg_summed_to_percent(self):
        self.assertEqual(bk.fees_pct(D("0.1"), D("0.05")), D("0.3"))


# ==========================================================================
# Null guards and fail-closed input law
# ==========================================================================

class NullGuardTests(KernelAssertions):
    def test_gap_null_guards(self):
        self.assertIsNone(bk.entry_gap_pct(D(0), D(100)))
        self.assertIsNone(bk.entry_gap_pct(D(100), D(0)))
        self.assertIsNone(bk.entry_gap_pct(None, D(100)))
        self.assertIsNone(bk.exit_gap_pct(D(-1), D(100)))
        self.assertIsNone(bk.executable_return_pct(D("NaN"), D(100)))

    def test_floats_and_bools_are_refused_not_coerced(self):
        # Decimal-only money law: binary floats fail closed.
        self.assertIsNone(bk.entry_gap_pct(100.02, 100.8))
        self.assertIsNone(bk.spread_bps(True, D(100)))
        self.assertIsNone(bk.funding_carry_pct(
            14_400_000, 60_000, 28_800_000, [0.001], bk.DIRECTION_NORMAL
        ))

    def test_spread_null_guards(self):
        self.assertIsNone(bk.spread_bps(D(0), D(100)))
        self.assertIsNone(bk.spread_bps(D(100), D(0)))
        self.assertEqual(bk.spread_bps(D(100), D(100)), D(0))  # ask == bid -> 0

    def test_mark_divergence_null_guards(self):
        self.assertIsNone(bk.mark_divergence_bps(None, D(100)))
        self.assertIsNone(bk.mark_divergence_bps(D(100), D(0)))

    def test_fees_null_guards(self):
        self.assertIsNone(bk.fees_pct(D("-0.1"), D("0.05")))
        self.assertIsNone(bk.fees_pct(None, D("0.05")))

    def test_walk_null_guards(self):
        self.assertIsNone(bk.ladder_walk_slippage_pct(None, D(10)))
        self.assertIsNone(bk.ladder_walk_slippage_pct((), D(10)))
        self.assertIsNone(bk.ladder_walk_slippage_pct(((D(100), D(1)),), D(0)))
        self.assertIsNone(bk.ladder_walk_slippage_pct(((D(100), D(1)),), None))
        # A malformed row fails the whole walk closed.
        self.assertIsNone(
            bk.ladder_walk_slippage_pct(((D(100), D(1)), (D(0), D(1))), D(10))
        )

    def test_empty_ladder_is_thin_not_absent_for_slippage(self):
        # Old-engine parity: a present-but-empty ladder walks to null and
        # costs the thin-ladder penalty; only a truly absent ladder (None)
        # uses the top-of-book model.
        penalty = bk.leg_slippage_pct((), D(10), D(100), D(1), D("0.05"))
        self.assertEqual(penalty, D("0.2"))

    def test_depth_null_guards(self):
        generous = ((D(100), D(500)),)
        self.assertIsNone(bk.depth_score(generous, generous, None, None, D(0)))
        self.assertIsNone(bk.depth_score(generous, generous, None, None, None))
        # No ladder and no touch on one leg -> pair unknown.
        self.assertIsNone(bk.depth_score(None, generous, None, None, D(10)))
        # Present-but-empty ladder sums to zero visible USD (score 0), it
        # does NOT silently fall back to the touch.
        self.assertEqual(
            bk.depth_score((), generous, (D(100), D(500)), None, D(10)), D(0)
        )

    def test_book_pressure_null_guards(self):
        book = make_book("100", "100.02")
        self.assertIsNone(bk.book_pressure(None, book))
        self.assertIsNone(bk.book_pressure(book, None))
        broken = bk.Book(bid=D(100), ask=D("100.02"), bid_qty=D(0), ask_qty=D(1))
        self.assertIsNone(bk.book_pressure(broken, book))

    def test_carry_null_guards(self):
        rates = [D("0.001")]
        self.assertIsNone(bk.funding_carry_pct(-1, 60_000, 28_800_000, rates,
                                               bk.DIRECTION_NORMAL))
        self.assertIsNone(bk.funding_carry_pct(14_400_000, 60_000, 0, rates,
                                               bk.DIRECTION_NORMAL))
        self.assertIsNone(bk.funding_carry_pct(14_400_000, 60_000, 28_800_000,
                                               rates, "sideways"))
        self.assertIsNone(bk.funding_carry_pct(14_400_000, 60_000, 28_800_000,
                                               None, bk.DIRECTION_NORMAL))
        self.assertIsNone(bk.funding_carry_pct(D("NaN"), 60_000, 28_800_000,
                                               rates, bk.DIRECTION_NORMAL))

    def test_flip_risk_null_guards_and_finite_filter(self):
        self.assertIsNone(bk.flip_risk(None))
        self.assertIsNone(bk.flip_risk([D("0.0001")] * 2))
        # Non-finite entries are dropped, not counted.
        risk = bk.flip_risk(
            [D("0.0001"), None, D("NaN"), D("0.0002"), D("0.0001")], D("0.0001")
        )
        self.assertClose(risk, D("0.32"))

    def test_net_edge_null_propagation(self):
        edge = bk.net_edge(None, D("0.3"), D(0), D("0.01"))
        self.assertIsNone(edge.net_pct)
        edge = bk.net_edge(D(1), D("0.3"), None, D("0.01"))
        self.assertIsNone(edge.net_pct)
        self.assertIsNone(edge.total_costs_pct)

    def test_funding_context_fail_closed_variants(self):
        def ctx(**overrides):
            fields = dict(mark_price=D(100), funding_rate=D("0.001"),
                          next_funding_ts=NOW + 3_600_000, ts=NOW)
            fields.update(overrides)
            return bk.funding_context(
                bk.FundingMark(**fields), 0, (), bk.DIRECTION_NORMAL,
                D(1), D(0), NOW,
            )

        self.assertFalse(bk.funding_context(
            None, 0, (), bk.DIRECTION_NORMAL, D(1), D(0), NOW
        ).fresh)
        self.assertFalse(ctx(mark_price=D(0)).fresh)
        self.assertFalse(ctx(funding_rate=None).fresh)
        self.assertFalse(ctx(ts=0).fresh)
        self.assertFalse(ctx(next_funding_ts=NOW).fresh)  # strictly > now
        # A not-fresh context still computes the hold, and never a carry.
        stale = ctx(next_funding_ts=NOW)
        self.assertEqual(stale.expected_hold_ms, D(14_400_000))
        self.assertIsNone(stale.carry_pct)

    def test_funding_context_future_skew_band(self):
        mark = bk.FundingMark(D(100), D("0.001"), NOW + 3_600_000, NOW)
        within = bk.funding_context(
            mark, -1_000, (), bk.DIRECTION_NORMAL, D(1), D(0), NOW
        )
        self.assertTrue(within.fresh)
        self.assertEqual(within.staleness_ms, D(0))  # reported floor at 0
        beyond = bk.funding_context(
            mark, -3_000, (), bk.DIRECTION_NORMAL, D(1), D(0), NOW
        )
        self.assertFalse(beyond.fresh)


# ==========================================================================
# evaluate(): guard order and reject-code parity
# ==========================================================================

class EvaluateLadderTests(KernelAssertions):
    def test_default_snapshot_is_the_measured_proposal(self):
        verdict = bk.evaluate(make_snapshot())
        self.assertTrue(verdict["accept"])
        self.assertEqual(verdict["reason"], "proposal")
        self.assertClose(
            verdict["components"]["net_pct"], D("0.48984403119376246")
        )
        self.assertEqual(verdict["components"]["fees_pct"], D("0.3"))

    def test_structure_guards_fire_first(self):
        self.assertEqual(
            bk.evaluate(make_snapshot(spot_book=None))["reason"], "spot_missing"
        )
        self.assertEqual(
            bk.evaluate(make_snapshot(perp_book=None))["reason"], "perp_missing"
        )
        # Spot beats perp when both are missing.
        self.assertEqual(
            bk.evaluate(make_snapshot(spot_book=None, perp_book=None))["reason"],
            "spot_missing",
        )

    def test_staleness_rejects_data_stale(self):
        self.assertEqual(
            bk.evaluate(make_snapshot(spot_staleness_ms=None))["reason"],
            "data_stale",
        )
        self.assertEqual(
            bk.evaluate(make_snapshot(perp_staleness_ms=6_000))["reason"],
            "data_stale",
        )

    def test_leg_skew_rejects_data_stale(self):
        self.assertEqual(
            bk.evaluate(make_snapshot(leg_skew_ms=3_000))["reason"], "data_stale"
        )

    def test_dq_composite_rejects_data_stale(self):
        # Mark 9s stale is still funding-fresh (<= 10s) but drags the dq
        # backbone to 0.1 < 0.4 — the composite gate fires, not funding.
        verdict = bk.evaluate(make_snapshot(mark_staleness_ms=9_000))
        self.assertEqual(verdict["reason"], "data_stale")
        self.assertTrue(verdict["components"]["funding_fresh"])
        self.assertClose(verdict["components"]["dq_score"], D("0.1"))

    def test_stale_funding_rejects_data_stale(self):
        verdict = bk.evaluate(make_snapshot(mark_staleness_ms=20_000))
        self.assertEqual(verdict["reason"], "data_stale")
        self.assertFalse(verdict["components"]["funding_fresh"])

    def test_below_watch_is_ignore_not_reject(self):
        verdict = bk.evaluate(gap_snapshot("0.12"))
        self.assertEqual(verdict["reason"], "ignore")

    def test_wide_spread_rejects(self):
        # Perp ask pushed out to ~39 bps spread; divergence stays inside 50.
        verdict = bk.evaluate(make_snapshot(
            perp_book=make_book("100.8", "101.2"),
            mark=bk.FundingMark(D("101.0"), D("0.0001"), NOW + 3_600_000, NOW),
        ))
        self.assertEqual(verdict["reason"], "spread_too_wide")

    def test_thin_depth_rejects(self):
        verdict = bk.evaluate(make_snapshot(
            spot_ask_ladder=((D("100.02"), D("0.02")),),
            perp_bid_ladder=((D("100.8"), D("0.02")),),
        ))
        self.assertEqual(verdict["reason"], "depth_too_thin")

    def test_unknown_notional_means_unknown_depth_rejects(self):
        # ADAPT old_sizing_default (REJECT): intended notional is an input;
        # without it depth is unknown, and unknown depth is not deep.
        verdict = bk.evaluate(make_snapshot(notional_usd=None))
        self.assertEqual(verdict["reason"], "depth_too_thin")
        self.assertIsNone(verdict["components"]["depth_score"])

    def test_kill_switch_blocks_arming_after_everything_else(self):
        # ADAPT money_control_flags: snapshots fail closed (halted) by
        # default, and the engine sees the full breakdown but cannot arm.
        self.assertTrue(bk.BasisSnapshot.__dataclass_fields__[
            "kill_switch_on"].default)
        verdict = bk.evaluate(make_snapshot(kill_switch_on=True))
        self.assertFalse(verdict["accept"])
        self.assertEqual(verdict["reason"], "kill_switch_on")
        self.assertClose(
            verdict["components"]["net_pct"], D("0.48984403119376246")
        )

    def test_persistence_short_is_focus_at_default_thresholds(self):
        verdict = bk.evaluate(make_snapshot(persist_ticks=4))
        self.assertEqual(verdict["reason"], "focus")  # gap 0.78 >= focus 0.3

    def test_persistence_short_watch_below_focus_gap(self):
        # With default fees (0.3%) a sub-focus gap can never clear
        # edge_below_fees, so watch needs a cheaper fee tier — the spec's
        # "economics rejects fire BEFORE the persistence gate" behavior.
        cfg = bk.KernelConfig(
            spot_taker_fee_pct=D("0.02"),
            perp_taker_fee_pct=D("0.02"),
            min_net_basis_pct=D("0.1"),
        )
        verdict = bk.evaluate(gap_snapshot("0.2", persist_ticks=1), cfg)
        self.assertEqual(verdict["reason"], "watch")

    def test_guard_order_stale_beats_spread(self):
        verdict = bk.evaluate(make_snapshot(
            spot_staleness_ms=6_000,
            perp_book=make_book("100.8", "101.2"),
            mark=bk.FundingMark(D("101.0"), D("0.0001"), NOW + 3_600_000, NOW),
        ))
        self.assertEqual(verdict["reason"], "data_stale")

    def test_guard_order_spread_beats_depth(self):
        verdict = bk.evaluate(make_snapshot(
            perp_book=make_book("100.8", "101.2"),
            mark=bk.FundingMark(D("101.0"), D("0.0001"), NOW + 3_600_000, NOW),
            spot_ask_ladder=((D("100.02"), D("0.02")),),
            perp_bid_ladder=((D("100.8"), D("0.02")),),
        ))
        self.assertEqual(verdict["reason"], "spread_too_wide")

    def test_guard_order_depth_beats_edge_below_fees(self):
        verdict = bk.evaluate(gap_snapshot(
            "0.25",
            spot_ask_ladder=((D("100.02"), D("0.02")),),
        ))
        self.assertEqual(verdict["reason"], "depth_too_thin")

    def test_guard_order_fees_beats_funding_drag(self):
        # gross 0.25 <= fees while drag 1.0 also over its cap: fees name it.
        verdict = bk.evaluate(gap_snapshot(
            "0.25",
            mark=bk.FundingMark(
                D("100.42"), D("-0.01"), NOW + 14_400_000, NOW
            ),
        ))
        self.assertEqual(verdict["reason"], "edge_below_fees")

    def test_guard_order_drag_beats_stability(self):
        verdict = bk.evaluate(make_snapshot(
            mark=bk.FundingMark(
                D("100.81"), D("-0.01"), NOW + 14_400_000, NOW
            ),
            stability_cv=D("0.9"),
            persist_ticks=1,
        ))
        self.assertEqual(verdict["reason"], "funding_against_trade")

    def test_guard_order_stability_beats_net_threshold(self):
        verdict = bk.evaluate(gap_snapshot(
            "0.45", stability_cv=D("0.9"), persist_ticks=1
        ))
        self.assertEqual(verdict["reason"], "gap_not_persistent")

    def test_survived_persistence_is_immune_to_cv(self):
        # The AND is the hysteresis: persisted N ticks overrides noisiness.
        verdict = bk.evaluate(make_snapshot(stability_cv=D("0.9"),
                                            persist_ticks=5))
        self.assertTrue(verdict["accept"])

    def test_guard_order_persistence_beats_kill_switch(self):
        verdict = bk.evaluate(make_snapshot(persist_ticks=2,
                                            kill_switch_on=True))
        self.assertEqual(verdict["reason"], "focus")

    def test_reject_vocabulary_is_frozen(self):
        # Reject codes are the frozen old-engine vocabulary subset used by
        # the NORMAL entry ladder — never invented per run.
        self.assertEqual(bk.REJECT_CODES, (
            "spot_missing", "perp_missing", "data_stale", "book_missing",
            "spread_too_wide", "depth_too_thin", "edge_below_fees",
            "funding_against_trade", "gap_not_persistent",
            "edge_below_threshold", "kill_switch_on",
        ))
        self.assertEqual(bk.DECISION_KINDS,
                         ("ignore", "watch", "focus", "proposal"))


# ==========================================================================
# Config validation invariants (KEEP)
# ==========================================================================

class ConfigValidationTests(KernelAssertions):
    def test_defaults_are_valid_and_frozen_values(self):
        cfg = bk.KernelConfig()
        self.assertEqual(cfg.max_spread_bps, D(25))
        self.assertEqual(cfg.min_net_basis_pct, D("0.25"))
        self.assertEqual(cfg.persist_ticks, 5)
        self.assertEqual(cfg.funding_period_ms, 28_800_000)

    def test_watch_must_not_exceed_focus(self):
        with self.assertRaises(ValueError):
            bk.KernelConfig(watch_gap_pct=D("0.4"), focus_gap_pct=D("0.3"))

    def test_hold_ordering_enforced(self):
        with self.assertRaises(ValueError):
            bk.KernelConfig(min_hold_ms=2, default_hold_ms=1)
        with self.assertRaises(ValueError):
            bk.KernelConfig(default_hold_ms=90_000_000)  # > max_hold_ms

    def test_hold_to_period_ratio_capped_at_1000(self):
        with self.assertRaises(ValueError):
            bk.KernelConfig(funding_period_ms=60_000)  # 86400000/60000 > 1000

    def test_zero_funding_period_throws(self):
        # Old suite: updateConfig({fundingPeriodMs: 0}) throws.
        with self.assertRaises(ValueError):
            bk.KernelConfig(funding_period_ms=0)

    def test_rolling_windows_at_least_three(self):
        with self.assertRaises(ValueError):
            bk.KernelConfig(gap_window_size=2)
        with self.assertRaises(ValueError):
            bk.KernelConfig(funding_history_keep=2)

    def test_money_thresholds_strictly_positive(self):
        with self.assertRaises(ValueError):
            bk.KernelConfig(min_net_basis_pct=D(0))
        with self.assertRaises(ValueError):
            bk.KernelConfig(watch_gap_pct=D(0), focus_gap_pct=D("0.3"))
        with self.assertRaises(ValueError):
            bk.KernelConfig(max_funding_drag_pct=D("-0.1"))

    def test_negative_zero_rejected(self):
        with self.assertRaises(ValueError):
            bk.KernelConfig(spot_taker_fee_pct=D("-0"))

    def test_non_finite_and_non_numeric_rejected(self):
        with self.assertRaises(ValueError):
            bk.KernelConfig(max_spread_bps=D("NaN"))
        with self.assertRaises(ValueError):
            bk.KernelConfig(persist_ticks=True)

    def test_funding_period_is_an_input_not_a_constant(self):
        # ADAPT funding_period_fixed_8h: a 4h-settling symbol passes its own
        # period; two settlements inside an 8h hold need two known rates.
        carry = bk.funding_carry_pct(
            28_800_000, 60_000, 14_400_000,
            [D("0.001"), D("0.001")], bk.DIRECTION_NORMAL,
        )
        self.assertEqual(carry, D("0.2"))


# ==========================================================================
# Fixture ledger — every in-scope spec fixture consumed, none silently lost
# ==========================================================================

class ZZFixtureLedger(unittest.TestCase):
    def test_all_math_fixtures_reproduced(self):
        self.assertEqual(len(MATH_FIXTURES), 33)
        missing = set(MATH_FIXTURES) - _COVERED["math"]
        self.assertEqual(missing, set(),
                         f"math fixtures not reproduced: {sorted(missing)}")

    def test_all_funding_fixtures_reproduced(self):
        self.assertEqual(len(FUNDING_FIXTURES), 47)
        missing = set(FUNDING_FIXTURES) - _COVERED["funding"]
        self.assertEqual(missing, set(),
                         f"funding fixtures not reproduced: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main()
