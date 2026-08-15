"""Pure volume-event to basis-dislocation coupling tests."""

import sys
import unittest
from dataclasses import fields
from decimal import Decimal
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from basis_events import (  # noqa: E402
    DetectionThresholds,
    DislocationDetector,
    detect_dislocations,
)


D = Decimal
SYMBOL = "RUNUSDT"


def stream_record(
    minute,
    spot,
    perp,
    surge,
    *,
    funding="0.0001",
    freshness="100",
):
    """Exact market_stream record shape; irrelevant fields stay read-only."""
    spot = D(spot)
    perp = D(perp)
    entry_gap = (perp - spot) / spot * D(100)
    return {
        "utc": "2026-08-15T04:%02d:00.000Z" % minute,
        "venues": "both",
        "spot_bid": format(spot, "f"),
        "spot_ask": format(spot, "f"),
        "spot_step": "0.01",
        "spot_min_notional": "5",
        "perp_bid": format(perp, "f"),
        "perp_ask": format(perp, "f"),
        "perp_step": "0.01",
        "perp_min_notional": "5",
        "mark": format(perp, "f"),
        "vol_1m_quote": "8000",
        "vol_med_1m": "1000",
        "surge_ratio": str(surge),
        "oi": "10000",
        "oi_change_pct": "1",
        "funding_rate": str(funding),
        "next_funding_ms": "1786752000000",
        "basis_gap_pct": format(entry_gap, "f"),
        "freshness_ms": str(freshness),
    }


def gap_spike_series():
    return [
        stream_record(0, "100", "100.1", "1"),
        stream_record(1, "102", "104.04", "10"),
        stream_record(2, "103.02", "105.0804", "10"),
    ]


class BasisEventsTests(unittest.TestCase):
    def test_violent_move_emits_exactly_one_gap_spike_with_correct_fields(self):
        events = detect_dislocations(SYMBOL, gap_spike_series())

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.utc, "2026-08-15T04:01:00.000Z")
        self.assertEqual(event.symbol, SYMBOL)
        self.assertEqual(event.kind, "gap_spike")
        self.assertEqual(event.entry_gap_pct, D("2"))
        self.assertEqual(event.exit_gap_pct, D("2"))
        self.assertEqual(event.surge_ratio, D("10"))
        self.assertEqual(event.price_accel_pct, D("2"))
        self.assertEqual(event.gap_growth_pct, D("1.9"))
        self.assertEqual(event.freshness_ms, D("100"))
        self.assertEqual(event.observation_gap_ms, 60_000)
        self.assertEqual(event.spot_ask, D("102"))
        self.assertEqual(event.perp_bid, D("104.04"))
        self.assertEqual(
            event.as_dict()["evidence"]["funding_rate"], D("0.0001")
        )

    def test_quiet_wide_static_gap_is_not_an_event(self):
        quiet = [
            stream_record(0, "100", "105", "1"),
            stream_record(1, "100", "105", "1"),
            # The price/volume impulse turns violent, but the old 5% gap does
            # not grow; it remains a market-maker leftover, not a new event.
            stream_record(2, "101", "106.05", "10"),
        ]
        self.assertEqual(detect_dislocations(SYMBOL, quiet), ())

    def test_funding_dislocation_is_detected_during_violent_move(self):
        series = [
            stream_record(0, "100", "100.1", "1", funding="0.0001"),
            stream_record(1, "101", "101.404", "10", funding="0.005"),
        ]
        events = detect_dislocations(SYMBOL, series)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "funding_dislocation")
        self.assertEqual(events[0].entry_gap_pct, D("0.4"))
        self.assertEqual(events[0].funding_rate, D("0.005"))
        self.assertEqual(events[0].funding_rate_delta, D("0.0049"))

    def test_gradual_runner_gap_emits_runner_hedge_window(self):
        series = [
            stream_record(0, "100", "100.1", "1"),
            stream_record(1, "101", "101.606", "10"),
        ]
        events = detect_dislocations(SYMBOL, series)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "runner_hedge_window")
        self.assertEqual(events[0].entry_gap_pct, D("0.6"))
        self.assertEqual(events[0].gap_growth_pct, D("0.5"))

    def test_determinism_for_series_and_snapshot_key_order(self):
        first = detect_dislocations(SYMBOL, gap_spike_series())
        second = detect_dislocations(SYMBOL, gap_spike_series())
        self.assertEqual(first, second)
        self.assertEqual(first[0].as_dict(), second[0].as_dict())

        left = DislocationDetector()
        right = DislocationDetector()
        base_a = stream_record(0, "100", "100.1", "1")
        base_b = stream_record(0, "50", "50.05", "1")
        move_a = stream_record(1, "102", "104.04", "10")
        move_b = stream_record(1, "51", "52.02", "10")
        left.observe_snapshot({"ZZZUSDT": base_b, "AAAUSDT": base_a})
        right.observe_snapshot({"AAAUSDT": base_a, "ZZZUSDT": base_b})
        self.assertEqual(
            left.observe_snapshot({"ZZZUSDT": move_b, "AAAUSDT": move_a}),
            right.observe_snapshot({"AAAUSDT": move_a, "ZZZUSDT": move_b}),
        )

    def test_thresholds_are_one_documented_dataclass(self):
        configured = DetectionThresholds()
        self.assertTrue(all(item.metadata.get("why") for item in fields(configured)))


if __name__ == "__main__":
    unittest.main()
