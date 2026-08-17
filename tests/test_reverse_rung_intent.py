"""Offline lock-ins for the read-only REVERSE rung intent."""

import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
from unittest import mock


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_ladder  # noqa: E402
import depth_walk as depth  # noqa: E402
import executable_gap  # noqa: E402
import funding_ruler  # noqa: E402
import limits  # noqa: E402
import margin_ops  # noqa: E402
import reverse_rung_intent as reverse  # noqa: E402


D = Decimal
SYMBOL = "TESTUSDT"
NOW_MS = 1_800_000_000_000


def executable_reading(spot_bid="102", perp_ask="100"):
    spread = min(D("1"), D(spot_bid) / D("10"), D(perp_ask) / D("10"))
    spot_record = executable_gap.ObservationRecordId()
    perp_record = executable_gap.ObservationRecordId()
    spot = executable_gap.VenueQuote(
        "spot", D(spot_bid), D(spot_bid) + spread, NOW_MS - 100,
        "spot-fixture", record_id=spot_record,
    )
    perp = executable_gap.VenueQuote(
        "perp", D(perp_ask) - spread, D(perp_ask), NOW_MS - 200,
        "perp-fixture", record_id=perp_record,
    )
    return executable_gap.read_executable_gap(
        SYMBOL, spot, perp, NOW_MS)


def directional(spot_bid="102", perp_ask="100", direction="reverse"):
    return reverse.DirectionalGap(
        executable_reading(spot_bid, perp_ask), direction)


def ladder(size="2.5", threshold="0.1", *, state=None, max_rungs=1,
           max_total=None):
    amount = D(size)
    config = basis_ladder.LadderConfig(
        schedule=(basis_ladder.LadderLevel(D(threshold), amount),),
        max_rungs=max_rungs,
        max_total_notional=amount if max_total is None else D(max_total),
        min_notional=D("0.1"),
    )
    return reverse.LadderSnapshot(
        basis_ladder.LadderState.flat(basis_ladder.REVERSE)
        if state is None else state,
        config,
    )


def borrow(**changes):
    values = {
        "asset": "TEST",
        "borrowable_amount": D("0.03"),
        "hourly_interest_rate": D("0.000001"),
        "borrow_power": True,
    }
    values.update(changes)
    return reverse.BorrowFacts(**values)


def market(spot_bid="102", perp_ask="100", **changes):
    values = {
        "spot_bid": D(spot_bid),
        "spot_step": D("0.01"),
        "spot_min_notional": D("2"),
        "perp_ask": D(perp_ask),
        "perp_step": D("0.001"),
        "perp_min_notional": D("2"),
        "funding_rate": D("0.0001"),
        "next_funding_ms": NOW_MS + 3_600_000,
        "funding_period_ms": 28_800_000,
    }
    values.update(changes)
    return reverse.ReverseMarketMetadata(**values)


def depth_input(*, thin=False, insufficient=False):
    spot_bids = [["102", "0.001"]] if insufficient else [["102", "1"]]
    perp_asks = [["100", "1"]]
    if thin:
        spot_bids = [["102", "0.005"], ["100", "1"]]
        perp_asks = [["100", "0.005"], ["101", "1"]]
    return reverse.ReverseDepthInput(
        {"bids": spot_bids, "asks": [["103", "1"]]},
        {"bids": [["99", "1"]], "asks": perp_asks},
    )


def funding_input(cap="0.001", floor="-0.001", interval_hours=1,
                  hold_minutes="60"):
    return reverse.ReverseFundingInput(
        funding_ruler.FundingInfo(
            D(cap), D(floor), interval_hours),
        D(hold_minutes),
    )


def build(gap=None, ladder_input=None, borrow_input=None,
          market_input=None, caps=None, depth_value=None, funding_value=None,
          assumptions=frozenset()):
    return reverse.build_reverse_rung_intent(
        directional() if gap is None else gap,
        ladder() if ladder_input is None else ladder_input,
        borrow() if borrow_input is None else borrow_input,
        market() if market_input is None else market_input,
        reverse.ReverseRungCaps() if caps is None else caps,
        depth_value,
        funding_value,
        assumptions,
    )


class ReverseIntentExactnessTests(unittest.TestCase):
    def assertMatchedIntent(self, result):
        self.assertIsInstance(result, reverse.ReverseRungIntent)
        self.assertEqual(result.borrow_qty, result.spot_short_qty)
        self.assertEqual(result.borrow_qty, result.perp_long_qty)

    def test_synthetic_reverse_dislocation_has_exact_intent(self):
        result = build()

        self.assertMatchedIntent(result)
        self.assertEqual(result.symbol, SYMBOL)
        self.assertEqual(result.ordinal, 1)
        self.assertEqual(result.borrow_asset, "TEST")
        self.assertEqual(result.borrow_qty, D("0.02"))
        self.assertEqual(result.spot_short_qty, D("0.02"))
        self.assertEqual(result.perp_long_qty, D("0.02"))
        self.assertEqual(result.quote_notional, D("2.5"))
        self.assertEqual(result.expected_capture_pct, D("2"))
        self.assertEqual(
            result.funding_and_interest_drag_pct, D("0.0104"))
        self.assertEqual(result.capture_source, "top_of_book")
        self.assertEqual(result.drag_source, "kernel_default_hold")
        self.assertEqual(result.settlements_crossed, 1)
        self.assertRegex(result.intent_id, r"^rri-[0-9a-f]{24}$")
        self.assertRegex(result.sealed_inputs_digest, r"^[0-9a-f]{64}$")
        self.assertEqual(result.assumptions, ())
        with self.assertRaises(FrozenInstanceError):
            result.ordinal = 2

    def test_each_leg_is_floored_to_its_own_step(self):
        result = build(market_input=market(
            spot_step=D("0.003"), perp_step=D("0.004")))

        self.assertMatchedIntent(result)
        self.assertEqual(result.spot_short_qty, D("0.024"))
        self.assertEqual(result.perp_long_qty, D("0.024"))
        self.assertEqual(result.borrow_qty, D("0.024"))

    def test_mismatched_intent_quantities_are_not_constructible(self):
        result = build()

        self.assertMatchedIntent(result)
        with self.assertRaisesRegex(ValueError, "base quantities must match"):
            replace(result, perp_long_qty=result.perp_long_qty + D("0.001"))

    def test_common_grid_without_a_positive_quantity_refuses(self):
        result = build(
            ladder_input=ladder(size="30"),
            borrow_input=borrow(borrowable_amount=D("0.1")),
            market_input=market(
                spot_step=D("0.1125"), perp_step=D("0.15")),
        )

        self.assertIsInstance(result, reverse.ReverseRungRefusal)
        self.assertEqual(result.gate,
                         reverse.STEP_OR_MIN_NOTIONAL_INFEASIBLE)

    def test_quote_cap_accepts_a_30_rung_and_refuses_just_over_30(self):
        accepted = build(
            ladder_input=ladder(size="30"),
            borrow_input=borrow(borrowable_amount=D("1")),
        )

        self.assertEqual(
            min(limits.PER_LEG_CAP_USDT, limits.FIRST_TRADE_CAP_USDT),
            D("30"),
        )
        self.assertMatchedIntent(accepted)
        self.assertEqual(accepted.quote_notional, D("30"))
        self.assertEqual(accepted.borrow_qty, D("0.29"))

        # The same plan refuses under the retired 8 bound, so the accepted
        # case above cannot pass against the smaller cap.
        under_old_bound = build(
            ladder_input=ladder(size="30"),
            borrow_input=borrow(borrowable_amount=D("1")),
            caps=reverse.ReverseRungCaps(
                per_leg_cap_usdt=D("8"), first_trade_cap_usdt=D("8")),
        )

        self.assertIsInstance(under_old_bound, reverse.ReverseRungRefusal)
        self.assertEqual(under_old_bound.gate,
                         reverse.QUOTE_NOTIONAL_CAP_EXCEEDED)

        just_over = build(
            ladder_input=ladder(size="30.01"),
            borrow_input=borrow(borrowable_amount=D("1")),
        )

        self.assertIsInstance(just_over, reverse.ReverseRungRefusal)
        self.assertEqual(just_over.gate,
                         reverse.QUOTE_NOTIONAL_CAP_EXCEEDED)

    def test_margin_principal_ceiling_boundary_with_cheap_asset_is_accepted(self):
        result = build(
            gap=directional("0.1", "0.099"),
            ladder_input=ladder(size="30"),
            borrow_input=borrow(
                borrowable_amount=margin_ops.PRINCIPAL_CEILING),
            market_input=market("0.1", "0.099"),
        )

        self.assertMatchedIntent(result)
        self.assertEqual(result.borrow_qty, margin_ops.PRINCIPAL_CEILING)
        self.assertEqual(result.quote_notional, D("30"))
        self.assertLessEqual(
            result.borrow_qty, margin_ops.PRINCIPAL_CEILING)
        self.assertLessEqual(result.quote_notional, D("30"))

    def test_identity_is_stable_and_verified_reading_field_changes_id(self):
        first = build()
        second = build()
        changed_reading = replace(
            executable_reading(), spot_age_ms=101, skew_checked=True)
        changed = build(gap=reverse.DirectionalGap(
            changed_reading, basis_ladder.REVERSE))

        self.assertMatchedIntent(first)
        self.assertMatchedIntent(second)
        self.assertMatchedIntent(changed)
        self.assertIs(changed_reading.skew_checked, True)
        self.assertEqual(first, second)
        self.assertEqual(first.intent_id, second.intent_id)
        self.assertEqual(
            first.sealed_inputs_digest, second.sealed_inputs_digest)
        self.assertNotEqual(first.intent_id, changed.intent_id)
        self.assertNotEqual(
            first.sealed_inputs_digest, changed.sealed_inputs_digest)

    def test_named_skew_assumption_is_carried_and_changes_verified_identity(self):
        assumptions = frozenset({reverse.SKEW_VERIFIED_ASSUMED})
        verified = build()
        assumed_verified = build(assumptions=assumptions)
        unverified = replace(executable_reading(), skew_checked=False)
        assumed_unverified = build(
            gap=reverse.DirectionalGap(unverified, basis_ladder.REVERSE),
            assumptions=assumptions,
        )

        self.assertMatchedIntent(verified)
        self.assertMatchedIntent(assumed_verified)
        self.assertMatchedIntent(assumed_unverified)
        self.assertEqual(
            assumed_unverified.assumptions,
            (reverse.SKEW_VERIFIED_ASSUMED,),
        )
        self.assertNotEqual(verified.intent_id, assumed_verified.intent_id)
        self.assertNotEqual(
            verified.sealed_inputs_digest,
            assumed_verified.sealed_inputs_digest,
        )

    def test_identity_material_seals_every_enumerated_field(self):
        gap_input = directional()
        reading = gap_input.result
        state = basis_ladder.LadderState(
            basis_ladder.REVERSE,
            (basis_ladder.Rung(D("1"), D("1")),),
        )
        config = basis_ladder.LadderConfig(
            schedule=(basis_ladder.LadderLevel(D("0.1"), D("2.5")),),
            max_rungs=2,
            max_total_notional=D("4"),
            min_notional=D("0.1"),
            convergence_gap=D("0"),
            max_unwind_notional_per_tick=D("1"),
        )
        ladder_input = reverse.LadderSnapshot(state, config)
        borrow_input = borrow()
        market_input = market()
        caps_input = reverse.ReverseRungCaps()
        depth_value = depth_input()
        funding_value = funding_input()
        base = {
            "gap": gap_input,
            "ladder": ladder_input,
            "borrow": borrow_input,
            "market": market_input,
            "caps": caps_input,
            "hold_ms": D("3600000"),
            "expected_capture_pct": D("2"),
            "capture_source": reverse.DEPTH_WALK,
            "drag_source": reverse.FUNDING_RULER_WORST_CASE,
            "settlements_crossed": 1,
            "depth": depth_value,
            "funding": funding_value,
            "assumptions": frozenset(),
        }

        def sealed(**changes):
            values = dict(base)
            values.update(changes)
            return reverse._sealed_material(**values)

        def changed_reading(**changes):
            return replace(gap_input, result=replace(reading, **changes))

        def changed_state(**changes):
            return replace(ladder_input, state=replace(state, **changes))

        def changed_config(**changes):
            return replace(ladder_input, config=replace(config, **changes))

        cases = {
            "gap.direction": {"gap": replace(gap_input, direction="normal")},
            "gap.symbol": {"gap": changed_reading(symbol="ALTUSDT")},
            "gap.now_ms": {"gap": changed_reading(now_ms=NOW_MS + 1)},
            "gap.verdict": {"gap": changed_reading(verdict="OTHER")},
            "gap.entry_gap_pct": {
                "gap": changed_reading(
                    entry_gap_pct=reading.entry_gap_pct + D("0.01"))},
            "gap.exit_gap_pct": {
                "gap": changed_reading(
                    exit_gap_pct=reading.exit_gap_pct + D("0.01"))},
            "gap.spot_age_ms": {"gap": changed_reading(spot_age_ms=101)},
            "gap.perp_age_ms": {"gap": changed_reading(perp_age_ms=201)},
            "gap.skew_checked": {"gap": changed_reading(skew_checked=False)},
            "ladder.direction": {
                "ladder": changed_state(direction=basis_ladder.NORMAL)},
            "ladder.rungs.gap_at_entry": {
                "ladder": changed_state(rungs=(
                    basis_ladder.Rung(D("1.1"), D("1")),))},
            "ladder.rungs.size": {
                "ladder": changed_state(rungs=(
                    basis_ladder.Rung(D("1"), D("1.1")),))},
            "ladder.schedule.gap_threshold": {
                "ladder": changed_config(schedule=(
                    basis_ladder.LadderLevel(D("0.2"), D("2.5")),))},
            "ladder.schedule.rung_size": {
                "ladder": changed_config(schedule=(
                    basis_ladder.LadderLevel(D("0.1"), D("2.6")),))},
            "ladder.max_rungs": {"ladder": changed_config(max_rungs=3)},
            "ladder.max_total_notional": {
                "ladder": changed_config(max_total_notional=D("5"))},
            "ladder.min_notional": {
                "ladder": changed_config(min_notional=D("0.2"))},
            "ladder.convergence_gap": {
                "ladder": changed_config(convergence_gap=D("0.05"))},
            "ladder.max_unwind_notional_per_tick": {
                "ladder": changed_config(
                    max_unwind_notional_per_tick=D("2"))},
            "borrow.asset": {"borrow": replace(borrow_input, asset="ALT")},
            "borrow.borrow_power": {
                "borrow": replace(borrow_input, borrow_power=False)},
            "borrow.borrowable_amount": {
                "borrow": replace(borrow_input, borrowable_amount=D("0.04"))},
            "borrow.hourly_interest_rate": {
                "borrow": replace(
                    borrow_input, hourly_interest_rate=D("0.000002"))},
            "market.spot_bid": {
                "market": replace(market_input, spot_bid=D("103"))},
            "market.spot_step": {
                "market": replace(market_input, spot_step=D("0.02"))},
            "market.spot_min_notional": {
                "market": replace(market_input, spot_min_notional=D("3"))},
            "market.perp_ask": {
                "market": replace(market_input, perp_ask=D("101"))},
            "market.perp_step": {
                "market": replace(market_input, perp_step=D("0.002"))},
            "market.perp_min_notional": {
                "market": replace(market_input, perp_min_notional=D("3"))},
            "market.funding_rate": {
                "market": replace(market_input, funding_rate=D("0.0002"))},
            "market.next_funding_ms": {
                "market": replace(
                    market_input, next_funding_ms=NOW_MS + 3_600_001)},
            "market.funding_period_ms": {
                "market": replace(market_input, funding_period_ms=3_600_000)},
            "caps.per_leg_cap_usdt": {
                "caps": replace(caps_input, per_leg_cap_usdt=D("31"))},
            "caps.first_trade_cap_usdt": {
                "caps": replace(caps_input, first_trade_cap_usdt=D("29"))},
            "caps.roundtrip_fee_pct": {
                "caps": replace(caps_input, roundtrip_fee_pct=D("0.31"))},
            "caps.expected_hold_ms": {"hold_ms": D("3600001")},
            "economics.expected_capture_pct": {
                "expected_capture_pct": D("2.1")},
            "economics.capture_source": {
                "capture_source": reverse.TOP_OF_BOOK},
            "economics.drag_source": {
                "drag_source": reverse.KERNEL_DEFAULT_HOLD},
            "economics.settlements_crossed": {"settlements_crossed": 2},
            "economics.depth.spot_depth": {
                "depth": replace(
                    depth_value, spot_depth={"bids": [["102", "2"]]})},
            "economics.depth.perp_depth": {
                "depth": replace(
                    depth_value, perp_depth={"asks": [["100", "2"]]})},
            "economics.funding.cap": {
                "funding": replace(
                    funding_value,
                    info=replace(funding_value.info, cap=D("0.002")))},
            "economics.funding.floor": {
                "funding": replace(
                    funding_value,
                    info=replace(funding_value.info, floor=D("-0.002")))},
            "economics.funding.interval_hours": {
                "funding": replace(
                    funding_value,
                    info=replace(funding_value.info, interval_hours=2))},
            "economics.funding.expected_hold_minutes": {
                "funding": replace(
                    funding_value, expected_hold_minutes=D("120"))},
            "assumptions": {
                "assumptions": frozenset({
                    reverse.SKEW_VERIFIED_ASSUMED})},
        }
        base_digest = sealed()
        changed_digests = []
        for field, changes in cases.items():
            with self.subTest(field=field):
                changed_digest = sealed(**changes)
                self.assertNotEqual(base_digest, changed_digest)
                changed_digests.append(changed_digest)

        authority_fields = (
            ("identity.schema", reverse,
             "_IDENTITY_SCHEMA", "reverse-rung-intent-v5"),
            ("caps.margin_principal_ceiling", reverse.margin_ops,
             "PRINCIPAL_CEILING", D("301")),
            ("caps.hard_per_leg_cap", reverse.limits,
             "PER_LEG_CAP_USDT", D("31")),
            ("caps.hard_first_trade_cap", reverse.limits,
             "FIRST_TRADE_CAP_USDT", D("29")),
            ("caps.hard_roundtrip_fee_pct", reverse.limits,
             "MEASURED_ROUNDTRIP_FEE_PCT", D("0.31")),
        )
        for field, module, name, value in authority_fields:
            with self.subTest(field=field), mock.patch.object(
                    module, name, value):
                changed_digest = sealed()
                self.assertNotEqual(base_digest, changed_digest)
                changed_digests.append(changed_digest)

        self.assertEqual(len(cases) + len(authority_fields), 52)
        self.assertEqual(len(set(changed_digests)), len(changed_digests))

    def test_depth_source_is_sealed_when_capture_matches_touch(self):
        top = build()
        walked = build(depth_value=depth_input())

        self.assertMatchedIntent(top)
        self.assertMatchedIntent(walked)
        self.assertEqual(top.expected_capture_pct,
                         walked.expected_capture_pct)
        self.assertEqual(walked.capture_source, "depth_walk")
        self.assertNotEqual(top.intent_id, walked.intent_id)
        self.assertNotEqual(top.sealed_inputs_digest,
                            walked.sealed_inputs_digest)

    def test_funding_ruler_records_worst_case_and_settlements(self):
        result = build(
            market_input=market(funding_period_ms=None),
            funding_value=funding_input())

        self.assertMatchedIntent(result)
        self.assertEqual(result.drag_source, "funding_ruler_worst_case")
        self.assertEqual(result.settlements_crossed, 1)
        self.assertEqual(
            result.funding_and_interest_drag_pct, D("0.1001"))


class OrderedRefusalTests(unittest.TestCase):
    def assertGate(self, result, expected):
        self.assertIsInstance(result, reverse.ReverseRungRefusal)
        self.assertEqual(result.gate, expected)
        self.assertEqual(result.reason, expected)
        self.assertTrue(result.detail)
        self.assertEqual(result.gate_index,
                         reverse.GATE_ORDER.index(expected) + 1)
        self.assertIn(result.capture_source,
                      ("top_of_book", "depth_walk"))
        self.assertIn(result.drag_source,
                      ("kernel_default_hold", "funding_ruler_worst_case"))

    def test_ten_gate_order_is_exact(self):
        self.assertEqual(reverse.GATE_ORDER, (
            reverse.BORROW_POWER_REQUIRED,
            reverse.GAP_NOT_EXECUTABLE_OR_STALE,
            reverse.SKEW_NOT_VERIFIED,
            reverse.GAP_DIRECTION_NOT_REVERSE,
            reverse.LADDER_HEADROOM_UNAVAILABLE,
            reverse.QUOTE_NOTIONAL_CAP_EXCEEDED,
            reverse.BORROW_CLAMP_EXCEEDED,
            reverse.STEP_OR_MIN_NOTIONAL_INFEASIBLE,
            reverse.DEPTH_INSUFFICIENT_OR_INVALID,
            reverse.NET_EDGE_NOT_POSITIVE,
        ))

    def test_borrow_power_defaults_closed(self):
        closed = reverse.BorrowFacts(
            "TEST", D("0.03"), D("0.000001"))
        self.assertGate(build(borrow_input=closed),
                        reverse.BORROW_POWER_REQUIRED)
        self.assertGate(build(
            borrow_input=closed,
            assumptions=frozenset({reverse.SKEW_VERIFIED_ASSUMED}),
        ), reverse.BORROW_POWER_REQUIRED)

    def test_non_executable_or_stale_gap_refuses(self):
        stale = executable_gap.GapRefusal(
            SYMBOL, "SPOT_QUOTE_STALE", 5_001, 100)
        self.assertGate(
            build(gap=reverse.DirectionalGap(stale, "reverse")),
            reverse.GAP_NOT_EXECUTABLE_OR_STALE,
        )

    def test_unverified_skew_refuses_after_fresh_gap(self):
        unverified = replace(executable_reading(), skew_checked=False)
        self.assertGate(
            build(gap=reverse.DirectionalGap(
                unverified, basis_ladder.REVERSE)),
            reverse.SKEW_NOT_VERIFIED,
        )

    def test_skew_assumptions_require_the_named_frozen_set(self):
        for assumptions in (
                {reverse.SKEW_VERIFIED_ASSUMED},
                frozenset({"borrow_power_assumed"})):
            with self.subTest(assumptions=assumptions):
                self.assertGate(
                    build(assumptions=assumptions),
                    reverse.SKEW_NOT_VERIFIED,
                )

    def test_normal_direction_gap_refuses(self):
        self.assertGate(
            build(gap=directional(direction="normal")),
            reverse.GAP_DIRECTION_NOT_REVERSE,
        )

    def test_ladder_without_headroom_refuses(self):
        full = basis_ladder.LadderState(
            basis_ladder.REVERSE,
            (basis_ladder.Rung(D("2"), D("2.5")),),
        )
        self.assertGate(
            build(ladder_input=ladder(state=full)),
            reverse.LADDER_HEADROOM_UNAVAILABLE,
        )

    def test_quote_above_thirty_refuses_before_borrow_clamp(self):
        self.assertGate(
            build(ladder_input=ladder(size="31")),
            reverse.QUOTE_NOTIONAL_CAP_EXCEEDED,
        )

    def test_margin_principal_above_ceiling_refuses(self):
        self.assertGate(
            build(
                gap=directional("0.005", "0.0049"),
                borrow_input=borrow(borrowable_amount=D("500")),
                market_input=market("0.005", "0.0049"),
            ),
            reverse.BORROW_CLAMP_EXCEEDED,
        )

    def test_margin_principal_clamp_uses_margin_ops_authority(self):
        with mock.patch.object(
                margin_ops, "PRINCIPAL_CEILING", D("0.01")):
            self.assertGate(build(), reverse.BORROW_CLAMP_EXCEEDED)

    def test_step_or_min_notional_infeasibility_refuses(self):
        self.assertGate(
            build(market_input=market(spot_step=None)),
            reverse.STEP_OR_MIN_NOTIONAL_INFEASIBLE,
        )

    def test_insufficient_depth_refuses_after_sizing(self):
        self.assertGate(
            build(depth_value=depth_input(insufficient=True)),
            reverse.DEPTH_INSUFFICIENT_OR_INVALID,
        )

    def test_thin_depth_lowers_capture_and_can_refuse_net_edge(self):
        thin = depth_input(thin=True)
        walked = depth.walk_lockin(
            "REVERSE", "ENTRY", thin.spot_depth, thin.perp_depth, D("2.5"))
        result = build(depth_value=thin)

        self.assertIsInstance(walked, depth.LockinDepthWalk)
        self.assertLess(walked.executable_capture_pct, D("2"))
        self.assertGate(result, reverse.NET_EDGE_NOT_POSITIVE)
        self.assertEqual(result.capture_source, "depth_walk")

    def test_funding_worst_case_above_capture_refuses(self):
        result = build(funding_value=funding_input(
            cap="0.03", floor="-0.03"))

        self.assertGate(result, reverse.NET_EDGE_NOT_POSITIVE)
        self.assertEqual(result.drag_source, "funding_ruler_worst_case")
        self.assertEqual(result.settlements_crossed, 1)

    def test_nonpositive_all_cost_edge_refuses(self):
        self.assertGate(
            build(
                gap=directional("100.1", "100"),
                ladder_input=ladder(threshold="0.01"),
                market_input=market("100.1", "100"),
            ),
            reverse.NET_EDGE_NOT_POSITIVE,
        )

    def test_unknown_money_facts_refuse_at_their_ordered_gate(self):
        cases = (
            (build(gap=reverse.DirectionalGap(None, "reverse")),
             reverse.GAP_NOT_EXECUTABLE_OR_STALE),
            (build(market_input=replace(market(), spot_bid=None)),
             reverse.GAP_NOT_EXECUTABLE_OR_STALE),
            (build(caps=reverse.ReverseRungCaps(per_leg_cap_usdt=None)),
             reverse.QUOTE_NOTIONAL_CAP_EXCEEDED),
            (build(borrow_input=replace(borrow(), borrowable_amount=None)),
             reverse.BORROW_CLAMP_EXCEEDED),
            (build(market_input=replace(
                market(), perp_min_notional=None)),
             reverse.STEP_OR_MIN_NOTIONAL_INFEASIBLE),
            (build(borrow_input=replace(
                borrow(), hourly_interest_rate=None)),
             reverse.NET_EDGE_NOT_POSITIVE),
            (build(market_input=replace(market(), funding_rate=None)),
             reverse.NET_EDGE_NOT_POSITIVE),
        )
        for result, expected in cases:
            with self.subTest(gate=expected):
                self.assertGate(result, expected)

    def test_fixed_order_names_the_first_of_multiple_failures(self):
        stale = reverse.DirectionalGap(
            executable_gap.GapRefusal(
                SYMBOL, "SPOT_QUOTE_STALE", 5_001, 100),
            "normal",
        )
        cases = (
            (build(gap=stale, borrow_input=borrow(borrow_power=False)), 0),
            (build(gap=stale), 1),
            (build(gap=reverse.DirectionalGap(
                replace(executable_reading(), skew_checked=False),
                "normal")), 2),
            (build(gap=directional(direction="normal"),
                   ladder_input=ladder(size="31")), 3),
            (build(ladder_input=ladder(
                state=basis_ladder.LadderState(
                    basis_ladder.REVERSE,
                    (basis_ladder.Rung(D("2"), D("2.5")),)))), 4),
            (build(ladder_input=ladder(size="31"),
                   market_input=market(spot_step=None)), 5),
            (build(
                gap=directional("0.005", "0.0049"),
                borrow_input=borrow(
                    borrowable_amount=D("500"), hourly_interest_rate=None),
                market_input=market("0.005", "0.0049")), 6),
            (build(market_input=market(
                spot_step=None, funding_rate=None)), 7),
            (build(depth_value=reverse.ReverseDepthInput({}, {}),
                   market_input=market(funding_rate=None)), 8),
            (build(gap=directional("100.1", "100"),
                   ladder_input=ladder(threshold="0.01"),
                   market_input=market("100.1", "100")), 9),
        )
        for result, gate_index in cases:
            with self.subTest(gate=reverse.GATE_ORDER[gate_index]):
                self.assertGate(result, reverse.GATE_ORDER[gate_index])


if __name__ == "__main__":
    unittest.main()
