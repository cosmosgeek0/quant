#!/usr/bin/env python3
"""Offline contract tests for the shared Spot + perpetual market stream."""

import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import market_stream as ms


FIXED_UTC = "2026-08-13T12:00:00.000Z"
FIXED_MS = 2_000_000_000


def book(symbol, bid, ask):
    return {
        "symbol": symbol,
        "bidPrice": str(bid),
        "bidQty": "1",
        "askPrice": str(ask),
        "askQty": "1",
    }


def premium(symbol, mark, funding="0.0001", next_ms="2000001000"):
    return {
        "symbol": symbol,
        "markPrice": str(mark),
        "indexPrice": str(mark),
        "estimatedSettlePrice": str(mark),
        "lastFundingRate": str(funding),
        "nextFundingTime": str(next_ms),
        "interestRate": "0.0001",
        "time": "2000000000",
    }


def klines(volumes, first_close=1_000_000):
    rows = []
    for index, volume in enumerate(volumes):
        close_ms = first_close + index * 60_000
        rows.append(
            [
                close_ms - 59_999,
                "1",
                "2",
                "1",
                "2",
                "10",
                close_ms,
                str(volume),
                4,
                "5",
                "5",
                "0",
            ]
        )
    return rows


class FixtureReader:
    """Injected endpoint reader; it has no network implementation."""

    def __init__(
        self,
        *,
        spot=None,
        perp=None,
        premiums=None,
        kline_batches=None,
        oi=None,
    ):
        self.spot = list(spot or [])
        self.perp = list(perp or [])
        self.premiums = list(premiums or [])
        self.kline_batches = {
            symbol: list(batches) for symbol, batches in (kline_batches or {}).items()
        }
        self.oi = dict(oi or {})
        self.calls = []

    def __call__(self, base_url, path, params):
        self.calls.append((base_url, path, dict(params)))
        if path == ms.SPOT_BOOK_PATH:
            return self.spot
        if path == ms.FAPI_BOOK_PATH:
            return self.perp
        if path == ms.FAPI_PREMIUM_PATH:
            return self.premiums
        if path in {ms.SPOT_KLINES_PATH, ms.FAPI_KLINES_PATH}:
            batches = self.kline_batches[params["symbol"]]
            if len(batches) > 1:
                return batches.pop(0)
            return batches[0]
        if path == ms.FAPI_OI_PATH:
            symbol = params["symbol"]
            value = self.oi[symbol]
            if isinstance(value, list):
                if len(value) > 1:
                    value = value.pop(0)
                else:
                    value = value[0]
            return {"symbol": symbol, "openInterest": str(value), "time": FIXED_MS}
        raise AssertionError(f"unexpected injected read: {path} {params}")


class OfflineCase(unittest.TestCase):
    def setUp(self):
        network_guard = patch.object(
            ms, "urlopen", side_effect=AssertionError("network call attempted")
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state_path = self.root / "stream_state.json"
        self.log_path = self.root / "stream_log.jsonl"
        self.windows_path = self.root / "stream_windows.json"

    def stream(self, reader, **kwargs):
        defaults = {
            "state_path": self.state_path,
            "log_path": self.log_path,
            "windows_path": self.windows_path,
            "window": 3,
            "focus_cap": 3,
            "reader": reader,
            "utc_fn": lambda: FIXED_UTC,
            "ms_fn": lambda: FIXED_MS,
            "request_spacing": 0,
        }
        defaults.update(kwargs)
        return ms.MarketStream(**defaults)

    def read_state(self):
        with open(self.state_path, encoding="utf-8") as handle:
            return json.load(handle)

    def read_log(self):
        if not self.log_path.exists():
            return []
        with open(self.log_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


class SchemaCompletenessTests(OfflineCase):
    def test_every_venue_class_has_exact_frozen_schema(self):
        reader = FixtureReader(
            spot=[book("AAAUSDT", "1", "1.1"), book("BBBUSDT", "10", "11")],
            perp=[book("BBBUSDT", "12", "13"), book("CCCUSDT", "20", "21")],
            premiums=[premium("BBBUSDT", "12.5"), premium("CCCUSDT", "20.5")],
            kline_batches={
                "AAAUSDT": [klines(("1", "2", "3"))],
                "BBBUSDT": [klines(("2", "2", "4"))],
                "CCCUSDT": [klines(("4", "5", "6"))],
            },
            oi={"BBBUSDT": "100", "CCCUSDT": "200"},
        )
        stats = self.stream(reader).run_once()
        state = self.read_state()

        self.assertEqual(set(state), {"AAAUSDT", "BBBUSDT", "CCCUSDT"})
        self.assertEqual(state["AAAUSDT"]["venues"], "spot")
        self.assertEqual(state["BBBUSDT"]["venues"], "both")
        self.assertEqual(state["CCCUSDT"]["venues"], "perp")
        for record in state.values():
            self.assertEqual(tuple(record), tuple(sorted(ms.STATE_FIELDS)))
            self.assertEqual(set(record), set(ms.STATE_FIELDS))
            for field in ms.NUMERIC_FIELDS:
                self.assertTrue(
                    record[field] is None or isinstance(record[field], str),
                    (field, record[field]),
                )

        spot_only = state["AAAUSDT"]
        self.assertIsNone(spot_only["perp_bid"])
        self.assertIsNone(spot_only["mark"])
        self.assertIsNone(spot_only["oi"])
        self.assertIsNone(spot_only["funding_rate"])
        self.assertIsNone(spot_only["basis_gap_pct"])

        perp_only = state["CCCUSDT"]
        self.assertIsNone(perp_only["spot_bid"])
        self.assertEqual(perp_only["oi"], "200")
        self.assertIsNone(perp_only["basis_gap_pct"])

        shared = state["BBBUSDT"]
        self.assertEqual(shared["basis_gap_pct"], "9.0909090909090909090909090909090909090909090909091")
        self.assertEqual(stats.total, 3)
        self.assertEqual((stats.spot_only, stats.perp_only, stats.both), (1, 1, 1))
        self.assertEqual(stats.request_count, 8)
        self.assertEqual(stats.request_budget, 9)
        self.assertEqual(stats.bulk_requests, 3)
        self.assertEqual(stats.kline_requests, 3)
        self.assertEqual(stats.oi_requests, 2)

    def test_nonpositive_or_crossed_books_fail_closed_to_null(self):
        reader = FixtureReader(
            spot=[book("AAAUSDT", "2", "1")],
            kline_batches={"AAAUSDT": [klines(("1", "1", "1"))]},
        )
        self.stream(reader, focus_cap=1).run_once()
        record = self.read_state()["AAAUSDT"]
        self.assertIsNone(record["spot_bid"])
        self.assertIsNone(record["spot_ask"])


class SurgeMathTests(OfflineCase):
    def test_short_window_is_null_then_median_and_ratio_are_decimal_text(self):
        self.assertEqual(
            ms.volume_metrics([["1", "10"], ["2", "20"]], 3),
            ("20", None, None),
        )
        self.assertEqual(
            ms.volume_metrics([["1", "10"], ["2", "20"], ["3", "90"]], 3),
            ("90", "20", "4.5"),
        )

    def test_zero_median_keeps_ratio_unknown(self):
        self.assertEqual(
            ms.volume_metrics([["1", "0"], ["2", "0"], ["3", "4"]], 3),
            ("4", "0", None),
        )


class RotationCoverageTests(OfflineCase):
    def test_rotating_slots_cover_every_symbol_within_stated_bound(self):
        symbols = [f"T{index:02d}USDT" for index in range(11)]
        scores = {symbol: Decimal(100 - index) for index, symbol in enumerate(symbols)}
        cursor = 0
        seen = set()
        first = ms.select_focus(symbols, scores, cap=4, cursor=cursor)
        self.assertEqual(first.rotation_slots, 2)
        self.assertEqual(first.mover_slots, 2)
        self.assertEqual(first.coverage_passes, 6)
        for _ in range(first.coverage_passes):
            selection = ms.select_focus(symbols, scores, cap=4, cursor=cursor)
            self.assertLessEqual(len(selection.symbols), 4)
            seen.update(selection.symbols)
            cursor = selection.next_cursor
        self.assertEqual(seen, set(symbols))


class RestartContinuityTests(OfflineCase):
    def test_window_and_rotation_cursor_survive_new_process_instance(self):
        first_reader = FixtureReader(
            spot=[book("AAAUSDT", "1", "2")],
            kline_batches={"AAAUSDT": [klines(("10", "20"))]},
        )
        first = self.stream(first_reader, focus_cap=1)
        first.run_once()
        self.assertIsNone(self.read_state()["AAAUSDT"]["surge_ratio"])

        second_reader = FixtureReader(
            spot=[book("AAAUSDT", "1.1", "2.1")],
            kline_batches={
                "AAAUSDT": [klines(("30",), first_close=1_120_000)]
            },
        )
        restarted = self.stream(second_reader, focus_cap=1)
        restarted.run_once()
        state = self.read_state()["AAAUSDT"]
        self.assertEqual(state["vol_1m_quote"], "30")
        self.assertEqual(state["vol_med_1m"], "20")
        self.assertEqual(state["surge_ratio"], "1.5")
        with open(self.windows_path, encoding="utf-8") as handle:
            sidecar = json.load(handle)
        self.assertEqual(sidecar["pass_number"], 2)
        self.assertEqual(
            sidecar["symbols"]["AAAUSDT"]["closed_1m"],
            [["1000000", "10"], ["1060000", "20"], ["1120000", "30"]],
        )


class TransitionLogTests(OfflineCase):
    def test_new_listing_threshold_crossings_both_directions_and_venue_change(self):
        reader = FixtureReader(
            spot=[book("AAAUSDT", "1", "2")],
            kline_batches={
                "AAAUSDT": [
                    klines(("10", "10", "10")),
                    klines(("40",), first_close=1_180_000),
                    klines(("10",), first_close=1_240_000),
                    klines(("10",), first_close=1_300_000),
                ]
            },
            oi={"AAAUSDT": "5"},
        )
        stream = self.stream(
            reader,
            focus_cap=1,
            thresholds=(Decimal("2"), Decimal("3")),
        )
        stream.run_once()
        stream.run_once()
        stream.run_once()
        reader.perp = [book("AAAUSDT", "1.5", "2.5")]
        reader.premiums = [premium("AAAUSDT", "2")]
        stream.run_once()

        events = self.read_log()
        self.assertEqual(events[0]["transition"], "new_listing_seen")
        crossings = [
            event for event in events if event["transition"] == "surge_threshold_crossed"
        ]
        self.assertEqual(
            [(event["direction"], event["threshold"]) for event in crossings],
            [("up", "2"), ("up", "3"), ("down", "2"), ("down", "3")],
        )
        venue_events = [event for event in events if event["transition"] == "venue_change"]
        self.assertEqual(len(venue_events), 1)
        self.assertEqual(venue_events[0]["from_venues"], "spot")
        self.assertEqual(venue_events[0]["to_venues"], "both")

    def test_no_crossing_event_from_unknown_ratio(self):
        self.assertEqual(
            ms._threshold_events(
                FIXED_UTC, "AAAUSDT", None, "9", (Decimal("2"),)
            ),
            [],
        )


class AtomicityTests(OfflineCase):
    def test_replace_failure_leaves_previous_state_complete(self):
        self.state_path.write_text('{"old":true}\n', encoding="utf-8")
        with patch.object(ms.os, "replace", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                ms.atomic_write_json(self.state_path, {"new": True})
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), '{"old":true}\n')
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_successful_write_is_complete_json_and_uses_replace(self):
        real_replace = ms.os.replace
        with patch.object(ms.os, "replace", wraps=real_replace) as replace:
            ms.atomic_write_json(self.state_path, {"AAAUSDT": {"ok": True}})
        replace.assert_called_once()
        self.assertEqual(self.read_state(), {"AAAUSDT": {"ok": True}})


class BulkFailureTests(OfflineCase):
    def test_bulk_failure_does_not_replace_existing_state(self):
        self.state_path.write_text('{"sentinel":"complete"}\n', encoding="utf-8")

        class FailingReader:
            def __call__(self, base_url, path, params):
                raise ms.HttpReadError("injected bulk failure")

        # The sentinel is deliberately not schema-valid, so use a fresh runner
        # and place it after initialization to isolate replacement behavior.
        self.state_path.unlink()
        stream = self.stream(FailingReader(), focus_cap=1)
        self.state_path.write_text('{"sentinel":"complete"}\n', encoding="utf-8")
        with self.assertRaises(ms.HttpReadError):
            stream.run_once()
        self.assertEqual(
            self.state_path.read_text(encoding="utf-8"),
            '{"sentinel":"complete"}\n',
        )


if __name__ == "__main__":
    unittest.main()
