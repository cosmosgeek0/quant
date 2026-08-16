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


def spot_info(symbol, status="TRADING", step=None, min_notional=None):
    row = {"symbol": symbol, "status": status}
    filters = []
    if step is not None:
        filters.append({"filterType": "LOT_SIZE", "stepSize": str(step)})
    if min_notional is not None:
        filters.append({
            "filterType": "NOTIONAL", "minNotional": str(min_notional)
        })
    if filters:
        row["filters"] = filters
    return row


def perp_info(symbol, status="TRADING", contract_type="PERPETUAL",
              step=None, min_notional=None):
    row = {
        "symbol": symbol,
        "status": status,
        "contractType": contract_type,
    }
    filters = []
    if step is not None:
        filters.append({"filterType": "LOT_SIZE", "stepSize": str(step)})
    if min_notional is not None:
        filters.append({
            "filterType": "MIN_NOTIONAL", "notional": str(min_notional)
        })
    if filters:
        row["filters"] = filters
    return row


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


def feed_row(venues, bid, ask, closed=()):
    row = {
        "venues": venues,
        "spot_bid": None,
        "spot_ask": None,
        "perp_bid": None,
        "perp_ask": None,
        "freshness_ms": "4",
        "book_age_ms": "4",
        "closed_1m": [[str(close_ms), str(volume)] for close_ms, volume in closed],
    }
    prefix = "perp" if venues == "perp" else "spot"
    row[f"{prefix}_bid"] = str(bid)
    row[f"{prefix}_ask"] = str(ask)
    return row


class FixtureReader:
    """Injected endpoint reader; it has no network implementation."""

    def __init__(
        self,
        *,
        spot=None,
        perp=None,
        spot_symbols=None,
        perp_symbols=None,
        premiums=None,
        kline_batches=None,
        oi=None,
    ):
        self.spot = list(spot or [])
        self.perp = list(perp or [])
        self.spot_symbols = None if spot_symbols is None else list(spot_symbols)
        self.perp_symbols = None if perp_symbols is None else list(perp_symbols)
        self.premiums = list(premiums or [])
        self.kline_batches = {
            symbol: list(batches) for symbol, batches in (kline_batches or {}).items()
        }
        self.oi = dict(oi or {})
        self.calls = []

    def __call__(self, base_url, path, params):
        self.calls.append((base_url, path, dict(params)))
        if path == ms.SPOT_EXCHANGE_INFO_PATH:
            symbols = self.spot_symbols
            if symbols is None:
                symbols = [spot_info(row["symbol"]) for row in self.spot]
            return {"symbols": symbols}
        if path == ms.FAPI_EXCHANGE_INFO_PATH:
            symbols = self.perp_symbols
            if symbols is None:
                symbols = [perp_info(row["symbol"]) for row in self.perp]
            return {"symbols": symbols}
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
        self.feed_path = self.root / "market_feed.json"

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

    def write_feed(self, symbols, written_ms=FIXED_MS):
        self.feed_path.write_text(
            json.dumps(
                {
                    "version": ms.FEED_STATE_VERSION,
                    "written_ms": str(written_ms),
                    "window": 3,
                    "symbols": symbols,
                }
            ),
            encoding="utf-8",
        )


class SchemaCompletenessTests(OfflineCase):
    def test_book_age_is_the_only_additive_tail_field(self):
        prior_fields = (
            "utc",
            "venues",
            "spot_bid",
            "spot_ask",
            "spot_step",
            "spot_min_notional",
            "perp_bid",
            "perp_ask",
            "perp_step",
            "perp_min_notional",
            "mark",
            "vol_1m_quote",
            "vol_med_1m",
            "surge_ratio",
            "oi",
            "oi_change_pct",
            "funding_rate",
            "next_funding_ms",
            "basis_gap_pct",
            "freshness_ms",
        )

        self.assertEqual(ms.STATE_PREFIX_FIELDS, prior_fields)
        self.assertEqual(ms.STATE_FIELDS[:-1], prior_fields)
        self.assertEqual(ms.STATE_FIELDS[-1], "book_age_ms")

    def test_state_without_book_age_loads_with_unknown_tail(self):
        old_record = {field: None for field in ms.STATE_FIELDS[:-1]}
        old_record["utc"] = FIXED_UTC
        old_record["venues"] = "both"
        self.state_path.write_text(
            json.dumps({"AAAUSDT": old_record}), encoding="utf-8"
        )

        loaded = ms.load_state(self.state_path)["AAAUSDT"]

        self.assertEqual(tuple(loaded), ms.STATE_FIELDS)
        self.assertIsNone(loaded["book_age_ms"])

    def test_every_venue_class_has_exact_frozen_schema(self):
        reader = FixtureReader(
            spot=[
                book("AAAUSDT", "1", "1.1"),
                book("BBBUSDT", "10", "11"),
                book("DEADUSDT", "0", "0"),
            ],
            perp=[
                book("BBBUSDT", "12", "13"),
                book("CCCUSDT", "20", "21"),
                book("HALTUSDT", "30", "31"),
                book("QUARTERUSDT", "40", "41"),
            ],
            spot_symbols=[
                spot_info("AAAUSDT"),
                spot_info("BBBUSDT"),
                spot_info("DEADUSDT", "BREAK"),
            ],
            perp_symbols=[
                perp_info("BBBUSDT"),
                perp_info("CCCUSDT"),
                perp_info("HALTUSDT", "SETTLING"),
                perp_info("QUARTERUSDT", contract_type="CURRENT_QUARTER"),
            ],
            premiums=[
                premium("BBBUSDT", "12.5"),
                premium("CCCUSDT", "20.5"),
                premium("HALTUSDT", "30.5"),
                premium("QUARTERUSDT", "40.5"),
            ],
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
        self.assertIsNone(spot_only["book_age_ms"])

        perp_only = state["CCCUSDT"]
        self.assertIsNone(perp_only["spot_bid"])
        self.assertEqual(perp_only["oi"], "200")
        self.assertIsNone(perp_only["basis_gap_pct"])
        self.assertIsNone(perp_only["book_age_ms"])

        shared = state["BBBUSDT"]
        self.assertEqual(shared["basis_gap_pct"], "9.0909090909090909090909090909090909090909090909091")
        self.assertEqual(shared["book_age_ms"], "0")
        self.assertEqual(stats.total, 3)
        self.assertEqual((stats.spot_only, stats.perp_only, stats.both), (1, 1, 1))
        self.assertEqual(stats.request_count, 10)
        self.assertEqual(stats.request_budget, 11)
        self.assertEqual(stats.bulk_requests, 5)
        self.assertEqual(stats.kline_requests, 3)
        self.assertEqual(stats.oi_requests, 2)
        self.assertEqual(stats.coverage_passes, 2)

    def test_nonpositive_or_crossed_books_fail_closed_to_null(self):
        reader = FixtureReader(
            spot=[book("AAAUSDT", "2", "1")],
            kline_batches={"AAAUSDT": [klines(("1", "1", "1"))]},
        )
        self.stream(reader, focus_cap=1).run_once()
        record = self.read_state()["AAAUSDT"]
        self.assertIsNone(record["spot_bid"])
        self.assertIsNone(record["spot_ask"])

    def test_old_oi_age_does_not_expand_book_age(self):
        three_hours_ms = 3 * 60 * 60 * 1_000
        clock = iter(
            (
                FIXED_MS,
                FIXED_MS,
                FIXED_MS,
                FIXED_MS,
                FIXED_MS,
                FIXED_MS + 5_000,
            )
        )
        reader = FixtureReader(
            spot=[book("AAAUSDT", "99", "100"), book("BBBUSDT", "199", "200")],
            perp=[book("AAAUSDT", "101", "102"), book("BBBUSDT", "201", "202")],
            premiums=[premium("AAAUSDT", "101"), premium("BBBUSDT", "201")],
            kline_batches={"AAAUSDT": [klines(("1", "1", "1"))]},
            oi={"AAAUSDT": "10"},
        )
        stream = self.stream(
            reader, focus_cap=1, ms_fn=lambda: next(clock)
        )
        stream.state["BBBUSDT"] = {"venues": "both", "oi": "20"}
        stream.sidecar["symbols"]["BBBUSDT"] = {
            "closed_1m": [],
            "last_oi_ms": str(FIXED_MS - three_hours_ms),
        }

        stream.run_once()
        record = self.read_state()["BBBUSDT"]

        self.assertEqual(record["freshness_ms"], str(three_hours_ms + 5_000))
        self.assertEqual(record["book_age_ms"], "5000")

    def test_both_venue_records_are_stamped_with_executable_metadata(self):
        reader = FixtureReader(
            spot=[book("AAAUSDT", "1", "2"), book("BBBUSDT", "10", "11")],
            perp=[book("BBBUSDT", "12", "13")],
            spot_symbols=[
                spot_info("AAAUSDT", step="0.01", min_notional="5"),
                spot_info("BBBUSDT", step="0.003", min_notional="6"),
            ],
            perp_symbols=[
                perp_info("BBBUSDT", step="0.002", min_notional="7")
            ],
            premiums=[premium("BBBUSDT", "12.5")],
            kline_batches={
                "AAAUSDT": [klines(("1", "1", "1"))],
                "BBBUSDT": [klines(("1", "1", "1"))],
            },
            oi={"BBBUSDT": "100"},
        )

        self.stream(reader, focus_cap=2).run_once()
        state = self.read_state()

        self.assertEqual(
            {key: state["BBBUSDT"][key] for key in ms.SIZING_FIELDS},
            {
                "spot_step": "0.003",
                "perp_step": "0.002",
                "spot_min_notional": "6",
                "perp_min_notional": "7",
            },
        )
        self.assertTrue(
            all(state["AAAUSDT"][key] is None for key in ms.SIZING_FIELDS)
        )


class MetadataCacheTests(OfflineCase):
    def test_exchange_info_refreshes_at_the_thirty_minute_boundary(self):
        clock = {"now": FIXED_MS}
        reader = FixtureReader(
            spot=[book("AAAUSDT", "100", "101")],
            perp=[book("AAAUSDT", "102", "103")],
            spot_symbols=[
                spot_info("AAAUSDT", step="0.01", min_notional="5")
            ],
            perp_symbols=[
                perp_info("AAAUSDT", step="0.01", min_notional="5")
            ],
            premiums=[premium("AAAUSDT", "102")],
            kline_batches={"AAAUSDT": [klines(("1", "1", "1"))]},
            oi={"AAAUSDT": "10"},
        )
        stream = self.stream(
            reader, focus_cap=1, ms_fn=lambda: clock["now"]
        )

        first = stream.run_once()
        self.assertEqual(first.bulk_requests, 5)
        metadata_path = self.state_path.with_name(ms.DEFAULT_METADATA_NAME)
        self.assertTrue(metadata_path.exists())

        reader.spot_symbols[0] = spot_info(
            "AAAUSDT", step="0.001", min_notional="6"
        )
        clock["now"] = FIXED_MS + ms.METADATA_REFRESH_MS - 1
        second = stream.run_once()
        self.assertEqual(second.bulk_requests, 3)
        self.assertEqual(self.read_state()["AAAUSDT"]["spot_step"], "0.01")

        clock["now"] = FIXED_MS + ms.METADATA_REFRESH_MS
        third = stream.run_once()
        self.assertEqual(third.bulk_requests, 5)
        self.assertEqual(self.read_state()["AAAUSDT"]["spot_step"], "0.001")
        self.assertEqual(
            sum(path == ms.SPOT_EXCHANGE_INFO_PATH
                for _, path, _ in reader.calls),
            2,
        )


class SurgeMathTests(OfflineCase):
    def test_short_window_is_null_then_median_and_ratio_are_decimal_text(self):
        self.assertEqual(
            ms.volume_metrics([["1000000", "10"], ["1060000", "20"]], 3),
            ("20", None, None),
        )
        self.assertEqual(
            ms.volume_metrics(
                [["1000000", "10"], ["1060000", "20"], ["1120000", "90"]],
                3,
            ),
            ("90", "20", "4.5"),
        )

    def test_zero_median_keeps_ratio_unknown(self):
        self.assertEqual(
            ms.volume_metrics(
                [["1000000", "0"], ["1060000", "0"], ["1120000", "4"]],
                3,
            ),
            ("4", "0", None),
        )

    def test_configured_span_coverage_and_per_candle_gap_both_fail_closed(self):
        compressed = [
            ["1000000", "1"],
            ["1055000", "1"],
            ["1110000", "1"],
            ["1165000", "100"],
        ]
        self.assertEqual(
            ms.volume_metrics(compressed, 4, Decimal("90")),
            ("100", "1", "100"),
        )
        self.assertEqual(
            ms.volume_metrics(compressed, 4, Decimal("95")),
            ("100", None, None),
        )
        sparse = [
            [str(FIXED_MS - (12 * 60 * 60 * 1000) - 3_000), "1"],
            [str(FIXED_MS - 123_000), "1"],
            [str(FIXED_MS - 63_000), "1"],
            [str(FIXED_MS - 3_000), "100"],
        ]
        self.assertEqual(
            ms.volume_metrics(sparse, 4),
            ("100", None, None),
        )


class LiveFeedMergeTests(OfflineCase):
    def test_fresh_feed_tickers_merge_for_the_complete_rest_universe(self):
        parsed = ms.argument_parser().parse_args(
            [
                "--once",
                "--feed-state",
                str(self.feed_path),
                "--window-min-coverage-pct",
                "95",
                "--basis-max-skew-ms",
                "25",
            ]
        )
        self.assertEqual(parsed.feed_state, self.feed_path)
        self.assertEqual(parsed.window_min_coverage_pct, Decimal("95"))
        self.assertEqual(parsed.basis_max_skew_ms, 25)
        self.write_feed(
            {
                "AAAUSDT": feed_row("spot", "10", "11"),
                "BBBUSDT": feed_row("spot", "20", "21"),
                "CCCUSDT": feed_row("spot", "30", "31"),
            }
        )
        reader = FixtureReader(
            spot=[
                book("AAAUSDT", "1", "2"),
                book("BBBUSDT", "2", "3"),
                book("CCCUSDT", "3", "4"),
            ],
            kline_batches={"AAAUSDT": [klines(("1", "2", "3"))]},
        )
        stats = self.stream(
            reader, focus_cap=1, feed_state_path=self.feed_path
        ).run_once()
        state = self.read_state()
        self.assertEqual(
            [state[symbol]["spot_bid"] for symbol in sorted(state)],
            ["10", "20", "30"],
        )
        kline_symbols = [
            params["symbol"]
            for _, path, params in reader.calls
            if path == ms.SPOT_KLINES_PATH
        ]
        self.assertEqual(kline_symbols, ["AAAUSDT"])
        self.assertTrue(stats.feed_used)
        self.assertEqual(stats.feed_reason, "accepted")

    def test_feed_windows_compute_surges_outside_the_focus_set(self):
        closes = (1_000_000, 1_060_000, 1_120_000)
        self.write_feed(
            {
                "AAAUSDT": feed_row("spot", "10", "11"),
                "BBBUSDT": feed_row(
                    "spot", "20", "21", zip(closes, ("10", "20", "90"))
                ),
                "CCCUSDT": feed_row(
                    "spot", "30", "31", zip(closes, ("2", "2", "10"))
                ),
            }
        )
        reader = FixtureReader(
            spot=[
                book("AAAUSDT", "1", "2"),
                book("BBBUSDT", "2", "3"),
                book("CCCUSDT", "3", "4"),
            ],
            kline_batches={"AAAUSDT": [klines(("1", "1", "1"))]},
        )
        self.stream(
            reader, focus_cap=1, feed_state_path=self.feed_path
        ).run_once()
        state = self.read_state()
        self.assertEqual(state["BBBUSDT"]["surge_ratio"], "4.5")
        self.assertEqual(state["CCCUSDT"]["surge_ratio"], "5")
        with open(self.windows_path, encoding="utf-8") as handle:
            sidecar = json.load(handle)
        self.assertEqual(set(sidecar["symbols"]), {"AAAUSDT", "BBBUSDT", "CCCUSDT"})

    def test_stale_feed_falls_back_without_merging_tickers_or_windows(self):
        self.write_feed(
            {
                "AAAUSDT": feed_row(
                    "spot",
                    "99",
                    "100",
                    ((1_000_000, "10"), (1_060_000, "20"), (1_120_000, "90")),
                ),
                "BBBUSDT": feed_row(
                    "spot",
                    "88",
                    "89",
                    ((1_000_000, "10"), (1_060_000, "20"), (1_120_000, "90")),
                ),
            },
            written_ms=FIXED_MS - ms.DEFAULT_FEED_MAX_AGE_MS - 1,
        )
        reader = FixtureReader(
            spot=[book("AAAUSDT", "1", "2"), book("BBBUSDT", "3", "4")],
            kline_batches={"AAAUSDT": [klines(("1", "2", "3"))]},
        )
        stats = self.stream(
            reader, focus_cap=1, feed_state_path=self.feed_path
        ).run_once()
        state = self.read_state()
        self.assertEqual(state["AAAUSDT"]["spot_bid"], "1")
        self.assertEqual(state["BBBUSDT"]["spot_bid"], "3")
        self.assertIsNone(state["BBBUSDT"]["surge_ratio"])
        self.assertFalse(stats.feed_used)
        self.assertEqual(stats.feed_reason, "snapshot_stale")
        with patch("builtins.print") as printer:
            ms.print_stats(stats)
        printer.assert_any_call("feed_used=no reason=snapshot_stale")

    def test_stale_feed_clears_persisted_surges_for_the_whole_universe(self):
        closes = (1_000_000, 1_060_000, 1_120_000)
        symbols = {
            "AAAUSDT": feed_row(
                "spot", "10", "11", zip(closes, ("1", "1", "4"))
            ),
            "BBBUSDT": feed_row(
                "spot", "20", "21", zip(closes, ("10", "20", "90"))
            ),
        }
        self.write_feed(symbols)
        reader = FixtureReader(
            spot=[book("AAAUSDT", "1", "2"), book("BBBUSDT", "3", "4")],
            kline_batches={
                "AAAUSDT": [klines(("1", "1", "4"))],
                "BBBUSDT": [klines(("10", "20", "90"))],
            },
        )
        stream = self.stream(
            reader, focus_cap=1, feed_state_path=self.feed_path
        )
        stream.run_once()
        self.assertEqual(self.read_state()["BBBUSDT"]["surge_ratio"], "4.5")

        self.write_feed(
            symbols,
            written_ms=FIXED_MS - ms.DEFAULT_FEED_MAX_AGE_MS - 1,
        )
        stats = stream.run_once()
        state = self.read_state()

        self.assertFalse(stats.feed_used)
        self.assertEqual(stats.feed_reason, "snapshot_stale")
        self.assertTrue(
            all(record["surge_ratio"] is None for record in state.values())
        )
        self.assertFalse(any(
            event["transition"] == "surge_threshold_crossed"
            for event in self.read_log()
        ))

    def test_sparse_four_candle_feed_repro_keeps_surge_unknown(self):
        latest_close = FIXED_MS - 3_000
        sparse_closes = (
            latest_close - (12 * 60 * 60 * 1000),
            latest_close - 120_000,
            latest_close - 60_000,
            latest_close,
        )
        self.write_feed(
            {
                "AAAUSDT": feed_row("spot", "10", "11"),
                "BBBUSDT": feed_row(
                    "spot",
                    "20",
                    "21",
                    zip(sparse_closes, ("1", "1", "1", "100")),
                ),
            }
        )
        reader = FixtureReader(
            spot=[book("AAAUSDT", "1", "2"), book("BBBUSDT", "3", "4")],
            kline_batches={"AAAUSDT": [klines(("1", "1", "1", "1"))]},
        )
        self.stream(
            reader,
            window=4,
            focus_cap=1,
            feed_state_path=self.feed_path,
        ).run_once()
        record = self.read_state()["BBBUSDT"]
        self.assertEqual(record["vol_1m_quote"], "100")
        self.assertIsNone(record["vol_med_1m"])
        self.assertIsNone(record["surge_ratio"])
        self.assertEqual(record["freshness_ms"], "3000")

    def test_feed_spot_and_rest_perp_repro_never_splice_basis(self):
        self.write_feed({"AAAUSDT": feed_row("spot", "89", "90")})
        reader = FixtureReader(
            spot=[book("AAAUSDT", "99", "100")],
            perp=[book("AAAUSDT", "100", "101")],
            premiums=[premium("AAAUSDT", "100")],
            kline_batches={"AAAUSDT": [klines(("1", "1", "1"))]},
            oi={"AAAUSDT": "10"},
        )
        self.stream(
            reader, focus_cap=1, feed_state_path=self.feed_path
        ).run_once()
        record = self.read_state()["AAAUSDT"]
        self.assertEqual(record["spot_ask"], "90")
        self.assertEqual(record["perp_bid"], "100")
        self.assertEqual(
            ms._basis_gap(record["spot_ask"], record["perp_bid"]),
            "11.111111111111111111111111111111111111111111111111",
        )
        self.assertIsNone(record["basis_gap_pct"])

    def test_two_fresh_feed_legs_from_one_snapshot_can_compute_basis(self):
        both = feed_row("spot", "89", "90")
        both.update(
            {
                "venues": "both",
                "perp_bid": "90",
                "perp_ask": "91",
            }
        )
        self.write_feed({"AAAUSDT": both})
        reader = FixtureReader(
            spot=[book("AAAUSDT", "99", "100")],
            perp=[book("AAAUSDT", "100", "101")],
            premiums=[premium("AAAUSDT", "100")],
            kline_batches={"AAAUSDT": [klines(("1", "1", "1"))]},
            oi={"AAAUSDT": "10"},
        )
        self.stream(
            reader, focus_cap=1, feed_state_path=self.feed_path
        ).run_once()
        record = self.read_state()["AAAUSDT"]
        self.assertEqual((record["spot_ask"], record["perp_bid"]), ("90", "90"))
        self.assertEqual(record["basis_gap_pct"], "0")
        self.assertEqual(record["book_age_ms"], "4")

    def test_future_feed_clock_does_not_understate_book_age(self):
        both = feed_row("spot", "89", "90")
        both.update(
            {
                "venues": "both",
                "perp_bid": "90",
                "perp_ask": "91",
                "freshness_ms": "6000",
                "book_age_ms": "6000",
            }
        )
        self.write_feed(
            {"AAAUSDT": both},
            written_ms=FIXED_MS + ms.FUTURE_SKEW_MS,
        )
        reader = FixtureReader(
            spot=[book("AAAUSDT", "99", "100")],
            perp=[book("AAAUSDT", "100", "101")],
            premiums=[premium("AAAUSDT", "100")],
            kline_batches={"AAAUSDT": [klines(("1", "1", "1"))]},
            oi={"AAAUSDT": "10"},
        )

        self.stream(
            reader,
            focus_cap=1,
            feed_state_path=self.feed_path,
            feed_max_age_ms=7_000,
        ).run_once()

        self.assertEqual(self.read_state()["AAAUSDT"]["book_age_ms"], "6000")

    def test_feed_leg_age_uncertainty_over_skew_bound_keeps_basis_unknown(self):
        both = feed_row("spot", "89", "90")
        both.update(
            {
                "venues": "both",
                "perp_bid": "90",
                "perp_ask": "91",
                "freshness_ms": str(ms.DEFAULT_BASIS_MAX_SKEW_MS + 1),
            }
        )
        self.write_feed({"AAAUSDT": both})
        reader = FixtureReader(
            spot=[book("AAAUSDT", "99", "100")],
            perp=[book("AAAUSDT", "100", "101")],
            premiums=[premium("AAAUSDT", "100")],
            kline_batches={"AAAUSDT": [klines(("1", "1", "1"))]},
            oi={"AAAUSDT": "10"},
        )
        self.stream(
            reader, focus_cap=1, feed_state_path=self.feed_path
        ).run_once()
        self.assertIsNone(self.read_state()["AAAUSDT"]["basis_gap_pct"])

    def test_same_rest_source_over_skew_bound_keeps_basis_unknown(self):
        clock = iter(
            (
                FIXED_MS,
                FIXED_MS,
                FIXED_MS + ms.DEFAULT_BASIS_MAX_SKEW_MS + 1,
                FIXED_MS + ms.DEFAULT_BASIS_MAX_SKEW_MS + 1,
                FIXED_MS + ms.DEFAULT_BASIS_MAX_SKEW_MS + 1,
                FIXED_MS + ms.DEFAULT_BASIS_MAX_SKEW_MS + 1,
            )
        )
        reader = FixtureReader(
            spot=[book("AAAUSDT", "99", "100")],
            perp=[book("AAAUSDT", "100", "101")],
            premiums=[premium("AAAUSDT", "100")],
            kline_batches={"AAAUSDT": [klines(("1", "1", "1"))]},
            oi={"AAAUSDT": "10"},
        )
        self.stream(reader, focus_cap=1, ms_fn=lambda: next(clock)).run_once()
        self.assertIsNone(self.read_state()["AAAUSDT"]["basis_gap_pct"])


class FeedDispositionTests(OfflineCase):
    def test_feed_written_within_future_skew_is_accepted(self):
        self.write_feed(
            {"AAAUSDT": feed_row("spot", "1", "2")},
            written_ms=FIXED_MS + ms.FUTURE_SKEW_MS,
        )

        result = ms.load_fresh_feed_state(self.feed_path, FIXED_MS)

        self.assertIsNotNone(result.snapshot)
        self.assertEqual(result.reason, "accepted")
        self.assertEqual(
            result.snapshot.symbols["AAAUSDT"]["book_age_ms"], "4"
        )

    def test_feed_written_beyond_future_skew_is_rejected(self):
        self.write_feed(
            {"AAAUSDT": feed_row("spot", "1", "2")},
            written_ms=FIXED_MS + ms.FUTURE_SKEW_MS + 1,
        )

        result = ms.load_fresh_feed_state(self.feed_path, FIXED_MS)

        self.assertIsNone(result.snapshot)
        self.assertEqual(result.reason, "written_ms_future")

    def test_future_feed_clock_keeps_the_source_age_bound(self):
        self.assertEqual(
            ms._clamped_age_ms(
                10_000,
                12_000,
                source_age_ms=6_000,
            ),
            6_000,
        )

    def test_every_feed_rejection_path_has_a_stable_reason(self):
        self.assertEqual(
            ms.load_fresh_feed_state(None, FIXED_MS).reason,
            "not_configured",
        )
        self.assertEqual(
            ms.load_fresh_feed_state(self.feed_path, FIXED_MS).reason,
            "path_missing",
        )
        self.feed_path.write_text("{", encoding="utf-8")
        self.assertEqual(
            ms.load_fresh_feed_state(self.feed_path, FIXED_MS).reason,
            "unreadable",
        )

        valid = {
            "version": ms.FEED_STATE_VERSION,
            "written_ms": str(FIXED_MS),
            "symbols": {
                "AAAUSDT": feed_row(
                    "spot", "1", "2", ((1_000_000, "1"),)
                )
            },
        }

        def changed(*path_and_value):
            payload = json.loads(json.dumps(valid))
            *path, value = path_and_value
            target = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return payload

        cases = (
            ("payload_not_mapping", []),
            ("unsupported_version", changed("version", 2)),
            ("written_ms_boolean", changed("written_ms", True)),
            ("written_ms_invalid", changed("written_ms", "bad")),
            ("written_ms_negative", changed("written_ms", "-1")),
            (
                "written_ms_future",
                changed(
                    "written_ms",
                    str(FIXED_MS + ms.FUTURE_SKEW_MS + 1),
                ),
            ),
            (
                "snapshot_stale",
                changed(
                    "written_ms",
                    str(FIXED_MS - ms.DEFAULT_FEED_MAX_AGE_MS - 1),
                ),
            ),
            ("symbols_not_mapping", changed("symbols", [])),
            (
                "symbol_invalid",
                changed("symbols", {"bad": valid["symbols"]["AAAUSDT"]}),
            ),
            ("symbol_record_not_mapping", changed("symbols", {"AAAUSDT": []})),
            (
                "closed_1m_not_list",
                changed("symbols", "AAAUSDT", "closed_1m", {}),
            ),
            (
                "candle_shape_invalid",
                changed("symbols", "AAAUSDT", "closed_1m", [["1"]]),
            ),
            (
                "candle_time_invalid",
                changed("symbols", "AAAUSDT", "closed_1m", [[True, "1"]]),
            ),
            (
                "candle_time_negative",
                changed("symbols", "AAAUSDT", "closed_1m", [["-1", "1"]]),
            ),
            (
                "candle_time_not_increasing",
                changed(
                    "symbols",
                    "AAAUSDT",
                    "closed_1m",
                    [["1", "1"], ["1", "2"]],
                ),
            ),
            (
                "candle_volume_invalid",
                changed("symbols", "AAAUSDT", "closed_1m", [["1", "bad"]]),
            ),
            (
                "candle_volume_negative",
                changed("symbols", "AAAUSDT", "closed_1m", [["1", "-1"]]),
            ),
            ("venues_invalid", changed("symbols", "AAAUSDT", "venues", "bad")),
            (
                "spot_bid_invalid",
                changed("symbols", "AAAUSDT", "spot_bid", "NaN"),
            ),
            (
                "freshness_not_string",
                changed("symbols", "AAAUSDT", "freshness_ms", 1),
            ),
            (
                "freshness_invalid",
                changed("symbols", "AAAUSDT", "freshness_ms", "bad"),
            ),
            (
                "freshness_negative",
                changed("symbols", "AAAUSDT", "freshness_ms", "-1"),
            ),
        )
        for reason, payload in cases:
            with self.subTest(reason=reason):
                self.feed_path.write_text(json.dumps(payload), encoding="utf-8")
                result = ms.load_fresh_feed_state(self.feed_path, FIXED_MS)
                self.assertIsNone(result.snapshot)
                self.assertEqual(result.reason, reason)

        self.feed_path.write_text(json.dumps(valid), encoding="utf-8")
        accepted = ms.load_fresh_feed_state(self.feed_path, FIXED_MS)
        self.assertIsNotNone(accepted.snapshot)
        self.assertEqual(accepted.reason, "accepted")


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
        clock = {"now": FIXED_MS}
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
            ms_fn=lambda: clock["now"],
        )
        stream.run_once()
        stream.run_once()
        stream.run_once()
        reader.perp = [book("AAAUSDT", "1.5", "2.5")]
        reader.premiums = [premium("AAAUSDT", "2")]
        clock["now"] += ms.METADATA_REFRESH_MS
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
