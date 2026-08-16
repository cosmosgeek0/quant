"""Offline lock-ins for the read-only REVERSE rung intent."""

import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_ladder  # noqa: E402
import depth_walk as depth  # noqa: E402
import executable_gap  # noqa: E402
import funding_ruler  # noqa: E402
import reverse_rung_intent as reverse  # noqa: E402


D = Decimal
SYMBOL = "TESTUSDT"
NOW_MS = 1_800_000_000_000


def executable_reading(spot_bid="102", perp_ask="100"):
    spot = executable_gap.TimedQuote(
        D(spot_bid), D(spot_bid) + D("1"), NOW_MS - 100)
    perp = executable_gap.TimedQuote(
        D(perp_ask) - D("1"), D(perp_ask), NOW_MS - 200)
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
          market_input=None, caps=None, depth_value=None, funding_value=None):
    return reverse.build_reverse_rung_intent(
        directional() if gap is None else gap,
        ladder() if ladder_input is None else ladder_input,
        borrow() if borrow_input is None else borrow_input,
        market() if market_input is None else market_input,
        reverse.ReverseRungCaps() if caps is None else caps,
        depth_value,
        funding_value,
    )


class ReverseIntentExactnessTests(unittest.TestCase):
    def test_synthetic_reverse_dislocation_has_exact_intent(self):
        result = build()

        self.assertIsInstance(result, reverse.ReverseRungIntent)
        self.assertEqual(result.symbol, SYMBOL)
        self.assertEqual(result.ordinal, 1)
        self.assertEqual(result.borrow_asset, "TEST")
        self.assertEqual(result.borrow_qty, D("0.02"))
        self.assertEqual(result.spot_short_qty, D("0.02"))
        self.assertEqual(result.perp_long_qty, D("0.025"))
        self.assertEqual(result.quote_notional, D("2.5"))
        self.assertEqual(result.expected_capture_pct, D("2"))
        self.assertEqual(
            result.funding_and_interest_drag_pct, D("0.0104"))
        self.assertEqual(result.capture_source, "top_of_book")
        self.assertEqual(result.drag_source, "kernel_default_hold")
        self.assertEqual(result.settlements_crossed, 1)
        self.assertLessEqual(result.quote_notional, D("8"))
        self.assertRegex(result.intent_id, r"^rri-[0-9a-f]{24}$")
        self.assertRegex(result.sealed_inputs_digest, r"^[0-9a-f]{64}$")
        with self.assertRaises(FrozenInstanceError):
            result.ordinal = 2

    def test_each_leg_is_floored_to_its_own_step(self):
        result = build(market_input=market(
            spot_step=D("0.003"), perp_step=D("0.004")))

        self.assertIsInstance(result, reverse.ReverseRungIntent)
        self.assertEqual(result.spot_short_qty, D("0.024"))
        self.assertEqual(result.perp_long_qty, D("0.024"))
        self.assertEqual(result.borrow_qty, D("0.024"))

    def test_three_usdt_borrow_clamp_boundary_is_accepted(self):
        result = build(ladder_input=ladder(size="3"))

        self.assertIsInstance(result, reverse.ReverseRungIntent)
        self.assertEqual(result.quote_notional, D("3"))

    def test_identity_is_stable_and_seals_every_changed_input(self):
        first = build()
        second = build()
        changed = build(borrow_input=replace(
            borrow(), borrowable_amount=D("0.04")))

        self.assertEqual(first, second)
        self.assertEqual(first.intent_id, second.intent_id)
        self.assertEqual(
            first.sealed_inputs_digest, second.sealed_inputs_digest)
        self.assertNotEqual(first.intent_id, changed.intent_id)
        self.assertNotEqual(
            first.sealed_inputs_digest, changed.sealed_inputs_digest)

    def test_depth_source_is_sealed_when_capture_matches_touch(self):
        top = build()
        walked = build(depth_value=depth_input())

        self.assertIsInstance(top, reverse.ReverseRungIntent)
        self.assertIsInstance(walked, reverse.ReverseRungIntent)
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

        self.assertIsInstance(result, reverse.ReverseRungIntent)
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

    def test_borrow_power_defaults_closed(self):
        self.assertGate(
            build(borrow_input=reverse.BorrowFacts(
                "TEST", D("0.03"), D("0.000001"))),
            reverse.BORROW_POWER_REQUIRED,
        )

    def test_non_executable_or_stale_gap_refuses(self):
        stale = executable_gap.GapRefusal(
            SYMBOL, "SPOT_QUOTE_STALE", 5_001, 100)
        self.assertGate(
            build(gap=reverse.DirectionalGap(stale, "reverse")),
            reverse.GAP_NOT_EXECUTABLE_OR_STALE,
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

    def test_quote_above_eight_refuses_before_borrow_clamp(self):
        self.assertGate(
            build(ladder_input=ladder(size="9")),
            reverse.QUOTE_NOTIONAL_CAP_EXCEEDED,
        )

    def test_margin_three_usdt_clamp_refuses(self):
        self.assertGate(
            build(ladder_input=ladder(size="4")),
            reverse.BORROW_CLAMP_EXCEEDED,
        )

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
            (build(gap=directional(direction="normal"),
                   ladder_input=ladder(size="9")), 2),
            (build(ladder_input=ladder(
                state=basis_ladder.LadderState(
                    basis_ladder.REVERSE,
                    (basis_ladder.Rung(D("2"), D("2.5")),)))), 3),
            (build(ladder_input=ladder(size="9"),
                   market_input=market(spot_step=None)), 4),
            (build(ladder_input=ladder(size="4"),
                   market_input=market(spot_step=None)), 5),
            (build(market_input=market(
                spot_step=None, funding_rate=None)), 6),
            (build(depth_value=reverse.ReverseDepthInput({}, {}),
                   market_input=market(funding_rate=None)), 7),
            (build(gap=directional("100.1", "100"),
                   ladder_input=ladder(threshold="0.01"),
                   market_input=market("100.1", "100")), 8),
        )
        for result, gate_index in cases:
            with self.subTest(gate=reverse.GATE_ORDER[gate_index]):
                self.assertGate(result, reverse.GATE_ORDER[gate_index])


if __name__ == "__main__":
    unittest.main()
