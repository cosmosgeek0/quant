#!/usr/bin/env python3
"""Deterministic offline tests for the v2 features of src/volume_observer.py.

No network anywhere: a guard patches the module's urlopen so any real network
attempt fails the test.  Covers (1) closed-candle volume-surge detection over
synthetic klines, (2) restart-safe paper positions (write, kill, reload —
without duplication or loss), (3) SPOT-USDT universe filtering from a fixture
exchangeInfo payload, and (4) the offline scan CLI flow end to end.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import volume_observer as vo


FIXED_TS = "2026-08-13T00:00:00.000Z"
MINUTE_MS = 60_000
BASE_OPEN_MS = 1_600_000_000_000  # fixed past epoch; every synthetic candle closed
FUTURE_CLOSE_MS = 4_102_444_800_000  # year 2100: a candle still forming


def fixed_now():
    return FIXED_TS


def kline_row(index, close_price="100", quote_volume="10", open_ms=None):
    """One raw kline list as /api/v3/klines returns it."""
    open_time = BASE_OPEN_MS + index * MINUTE_MS if open_ms is None else open_ms
    return [
        open_time,
        "100",
        "120",
        "90",
        close_price,
        "1",
        open_time + MINUTE_MS - 1,
        quote_volume,
        5,
        "0.5",
        "50",
        "0",
    ]


def surge_kline_fixture():
    """Baseline quote volumes 10,20,30 (median 20) then a 60 candle, then forming."""
    rows = [
        kline_row(0, quote_volume="10"),
        kline_row(1, quote_volume="20"),
        kline_row(2, quote_volume="30"),
        kline_row(3, quote_volume="60", close_price="100"),
    ]
    forming = kline_row(4, quote_volume="999")
    forming[6] = FUTURE_CLOSE_MS
    rows.append(forming)
    return rows


def quiet_kline_fixture():
    rows = [
        kline_row(0, quote_volume="10"),
        kline_row(1, quote_volume="20"),
        kline_row(2, quote_volume="30"),
        kline_row(3, quote_volume="20", close_price="100"),
    ]
    return rows


def exchange_info_fixture():
    return {
        "timezone": "UTC",
        "symbols": [
            {
                "symbol": "BBBUSDT",
                "status": "TRADING",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
            },
            {"symbol": "AAAUSDT", "status": "TRADING", "quoteAsset": "USDT"},
            {
                "symbol": "CCCUSDT",
                "status": "BREAK",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
            },
            {
                "symbol": "DDDBTC",
                "status": "TRADING",
                "quoteAsset": "BTC",
                "isSpotTradingAllowed": True,
            },
            {
                "symbol": "EEEUSDT",
                "status": "TRADING",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": False,
            },
            {
                # live-measured shape 2026-08-13: real TRADING USDT listing
                # whose symbol is outside the scannable A-Z0-9 charset
                "symbol": "币安人生USDT",
                "status": "TRADING",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
            },
            {"symbol": "WTRY", "status": "TRADING", "quoteAsset": "TRY"},
        ],
    }


def parsed_candles(rows, symbol="AAAUSDT"):
    return vo.parse_kline_rows(rows, symbol)


def closed_fixture_candles(rows, symbol="AAAUSDT"):
    """All synthetic candles are in the past except the marked forming one."""
    return vo.closed_candles_only(
        parsed_candles(rows, symbol), BASE_OPEN_MS + 100 * MINUTE_MS
    )


class NoNetworkTestCase(unittest.TestCase):
    """Every test runs with the module's urlopen booby-trapped."""

    def setUp(self):
        patcher = patch.object(
            vo, "urlopen", side_effect=AssertionError("network call attempted")
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.receipts = self.tmp_path / "paper_receipts.jsonl"
        self.state_path = self.tmp_path / "paper_state.json"

    def read_receipts(self):
        if not self.receipts.exists():
            return []
        with open(self.receipts, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle.read().splitlines()]


class KlineParsingTests(NoNetworkTestCase):
    def test_valid_rows_parse_in_order(self):
        candles = parsed_candles(surge_kline_fixture())
        self.assertEqual(len(candles), 5)
        self.assertEqual(candles[0].open_time_ms, BASE_OPEN_MS)
        self.assertEqual(candles[0].close_time_ms, BASE_OPEN_MS + MINUTE_MS - 1)
        self.assertEqual(candles[3].quote_volume, Decimal("60"))
        self.assertEqual(candles[3].close_price, Decimal("100"))
        self.assertEqual(candles[0].trade_count, 5)

    def test_non_list_payload_rejected(self):
        with self.assertRaises(vo.DataReadError):
            vo.parse_kline_rows({"not": "a list"}, "AAAUSDT")

    def test_short_row_rejected(self):
        with self.assertRaises(vo.DataReadError):
            vo.parse_kline_rows([[BASE_OPEN_MS, "1", "1"]], "AAAUSDT")

    def test_non_positive_price_rejected(self):
        row = kline_row(0, close_price="0")
        with self.assertRaises(vo.DataReadError):
            vo.parse_kline_rows([row], "AAAUSDT")

    def test_negative_volume_rejected(self):
        row = kline_row(0, quote_volume="-1")
        with self.assertRaises(vo.DataReadError):
            vo.parse_kline_rows([row], "AAAUSDT")

    def test_non_finite_volume_rejected(self):
        row = kline_row(0, quote_volume="NaN")
        with self.assertRaises(vo.DataReadError):
            vo.parse_kline_rows([row], "AAAUSDT")

    def test_close_before_open_rejected(self):
        row = kline_row(0)
        row[6] = row[0] - 1
        with self.assertRaises(vo.DataReadError):
            vo.parse_kline_rows([row], "AAAUSDT")

    def test_out_of_order_rows_rejected(self):
        with self.assertRaises(vo.DataReadError):
            vo.parse_kline_rows([kline_row(1), kline_row(0)], "AAAUSDT")

    def test_boolean_time_rejected(self):
        row = kline_row(0)
        row[0] = True
        with self.assertRaises(vo.DataReadError):
            vo.parse_kline_rows([row], "AAAUSDT")


class ClosedCandleTests(NoNetworkTestCase):
    def test_forming_candle_excluded(self):
        candles = parsed_candles(surge_kline_fixture())
        closed = vo.closed_candles_only(candles, BASE_OPEN_MS + 100 * MINUTE_MS)
        self.assertEqual(len(closed), 4)
        self.assertTrue(
            all(c.close_time_ms < BASE_OPEN_MS + 100 * MINUTE_MS for c in closed)
        )

    def test_candle_closing_exactly_now_not_closed(self):
        candles = parsed_candles([kline_row(0)])
        boundary = candles[0].close_time_ms
        self.assertEqual(vo.closed_candles_only(candles, boundary), [])
        self.assertEqual(len(vo.closed_candles_only(candles, boundary + 1)), 1)

    def test_all_future_candles_yield_empty(self):
        candles = parsed_candles(surge_kline_fixture())
        self.assertEqual(vo.closed_candles_only(candles, BASE_OPEN_MS), [])


class DecimalMedianTests(NoNetworkTestCase):
    def test_odd_count_middle_value(self):
        values = [Decimal("30"), Decimal("10"), Decimal("20")]
        self.assertEqual(vo.decimal_median(values), Decimal("20"))

    def test_even_count_mean_of_middles(self):
        values = [Decimal("10"), Decimal("40"), Decimal("20"), Decimal("30")]
        self.assertEqual(vo.decimal_median(values), Decimal("25"))

    def test_even_count_half_step_exact(self):
        self.assertEqual(
            vo.decimal_median([Decimal("10"), Decimal("21")]), Decimal("15.5")
        )

    def test_empty_rejected(self):
        with self.assertRaises(vo.PaperInputError):
            vo.decimal_median([])


class SurgeEvaluationTests(NoNetworkTestCase):
    def evaluate(self, rows, baseline=3, multiple=Decimal("3")):
        closed = closed_fixture_candles(rows)
        return vo.evaluate_surge(
            "AAAUSDT", "1m", closed, baseline, multiple, FIXED_TS
        )

    def test_surge_at_threshold_is_true(self):
        check = self.evaluate(surge_kline_fixture())
        self.assertEqual(check.status, "evaluated")
        self.assertEqual(check.closed_candle_count, 4)
        self.assertEqual(check.candle_quote_volume, Decimal("60"))
        self.assertEqual(check.baseline_median_quote_volume, Decimal("20"))
        self.assertEqual(check.threshold_quote_volume, Decimal("60"))
        self.assertTrue(check.surged)

    def test_below_threshold_is_false(self):
        check = self.evaluate(quiet_kline_fixture())
        self.assertEqual(check.status, "evaluated")
        self.assertEqual(check.candle_quote_volume, Decimal("20"))
        self.assertEqual(check.threshold_quote_volume, Decimal("60"))
        self.assertFalse(check.surged)

    def test_forming_candle_never_evaluated(self):
        # The forming candle carries quote volume 999; if it were evaluated the
        # check would surge on it.  The evaluated candle must be the last CLOSED.
        check = self.evaluate(surge_kline_fixture())
        self.assertEqual(
            check.candle_close_utc,
            vo.utc_from_epoch_ms(BASE_OPEN_MS + 4 * MINUTE_MS - 1, "test"),
        )

    def test_insufficient_closed_candles(self):
        rows = surge_kline_fixture()[:3]
        check = self.evaluate(rows)
        self.assertEqual(check.status, "insufficient_closed_candles")
        self.assertFalse(check.surged)
        self.assertIsNone(check.candle_quote_volume)
        self.assertIsNone(check.threshold_quote_volume)

    def test_baseline_zero_reported_not_surged(self):
        rows = [
            kline_row(0, quote_volume="0"),
            kline_row(1, quote_volume="0"),
            kline_row(2, quote_volume="0"),
            kline_row(3, quote_volume="50"),
        ]
        check = self.evaluate(rows)
        self.assertEqual(check.status, "baseline_zero")
        self.assertFalse(check.surged)
        self.assertEqual(check.baseline_median_quote_volume, Decimal("0"))
        self.assertIsNone(check.threshold_quote_volume)

    def test_baseline_bounds_rejected(self):
        with self.assertRaises(vo.PaperInputError):
            self.evaluate(surge_kline_fixture(), baseline=2)
        with self.assertRaises(vo.PaperInputError):
            self.evaluate(surge_kline_fixture(), baseline=501)

    def test_multiple_bounds_rejected(self):
        with self.assertRaises(vo.PaperInputError):
            self.evaluate(surge_kline_fixture(), multiple=Decimal("1"))
        with self.assertRaises(vo.PaperInputError):
            self.evaluate(surge_kline_fixture(), multiple=Decimal("101"))

    def test_bad_symbol_rejected(self):
        with self.assertRaises(vo.PaperInputError):
            vo.evaluate_surge("bad", "1m", [], 3, Decimal("3"), FIXED_TS)

    def test_evaluation_is_deterministic(self):
        first = self.evaluate(surge_kline_fixture())
        second = self.evaluate(surge_kline_fixture())
        self.assertEqual(first, second)


class UniverseFilteringTests(NoNetworkTestCase):
    def test_fixture_filtered_and_sorted(self):
        universe = vo.parse_spot_usdt_universe(exchange_info_fixture())
        self.assertEqual(universe, ["AAAUSDT", "BBBUSDT"])

    def test_non_object_payload_rejected(self):
        with self.assertRaises(vo.DataReadError):
            vo.parse_spot_usdt_universe([1, 2, 3])

    def test_missing_symbols_list_rejected(self):
        with self.assertRaises(vo.DataReadError):
            vo.parse_spot_usdt_universe({"timezone": "UTC"})

    def test_non_object_entry_rejected(self):
        with self.assertRaises(vo.DataReadError):
            vo.parse_spot_usdt_universe({"symbols": ["AAAUSDT"]})

    def test_non_string_symbol_rejected(self):
        payload = {
            "symbols": [{"symbol": 123, "status": "TRADING", "quoteAsset": "USDT"}]
        }
        with self.assertRaises(vo.DataReadError):
            vo.parse_spot_usdt_universe(payload)

    def test_unscannable_symbol_excluded_not_fatal(self):
        payload = {
            "symbols": [
                {"symbol": "bad", "status": "TRADING", "quoteAsset": "USDT"},
                {"symbol": "GOODUSDT", "status": "TRADING", "quoteAsset": "USDT"},
            ]
        }
        self.assertEqual(vo.parse_spot_usdt_universe(payload), ["GOODUSDT"])

    def test_missing_status_rejected(self):
        payload = {"symbols": [{"symbol": "AAAUSDT", "quoteAsset": "USDT"}]}
        with self.assertRaises(vo.DataReadError):
            vo.parse_spot_usdt_universe(payload)

    def test_duplicate_symbols_deduped(self):
        entry = {"symbol": "AAAUSDT", "status": "TRADING", "quoteAsset": "USDT"}
        universe = vo.parse_spot_usdt_universe({"symbols": [entry, dict(entry)]})
        self.assertEqual(universe, ["AAAUSDT"])

    def test_empty_universe_from_fetch_rejected(self):
        with patch.object(
            vo, "fetch_public_payload", return_value={"symbols": []}
        ):
            with self.assertRaises(vo.DataReadError):
                vo.fetch_spot_usdt_universe(5.0)

    def test_fetch_requests_reduced_payload_with_raised_bound(self):
        """One GET, reduced-payload params, universe-sized byte bound."""
        calls = []

        def capture(path, params, timeout, source, max_bytes=vo.MAX_RESPONSE_BYTES):
            calls.append((path, params, max_bytes))
            return exchange_info_fixture()

        with patch.object(vo, "fetch_public_payload", capture):
            universe = vo.fetch_spot_usdt_universe(5.0)
        self.assertEqual(universe, ["AAAUSDT", "BBBUSDT"])
        self.assertEqual(len(calls), 1)
        path, params, max_bytes = calls[0]
        self.assertEqual(path, vo.EXCHANGE_INFO_PATH)
        self.assertEqual(
            params, {"showPermissionSets": "false", "symbolStatus": "TRADING"}
        )
        self.assertEqual(max_bytes, vo.MAX_UNIVERSE_RESPONSE_BYTES)


class PaperStatePersistenceTests(NoNetworkTestCase):
    def entry_state(self):
        """Open one paper position from the surge fixture and persist it."""
        closed = closed_fixture_candles(surge_kline_fixture())
        check = vo.evaluate_surge(
            "AAAUSDT", "1m", closed, 3, Decimal("3"), FIXED_TS
        )
        state = vo.PaperState()
        action = vo.apply_paper_candle(
            state,
            check,
            closed[-1],
            Decimal("10"),
            Decimal("0.05"),
            self.receipts,
            "statetest01",
            fixed_now,
        )
        self.assertEqual(action, "paper_entry")
        vo.save_paper_state(self.state_path, state, fixed_now)
        return state, closed, check

    def test_missing_file_is_empty_state(self):
        state = vo.load_paper_state(self.state_path)
        self.assertEqual(state.positions, {})
        self.assertEqual(state.candle_marks, {})

    def test_entry_persists_and_reloads_identically(self):
        state, _, _ = self.entry_state()
        reloaded = vo.load_paper_state(self.state_path)
        self.assertEqual(reloaded.positions, state.positions)
        self.assertEqual(reloaded.candle_marks, state.candle_marks)
        position = reloaded.positions["AAAUSDT"]
        self.assertIs(position["paper"], True)
        self.assertEqual(position["entry_price"], "100")
        self.assertEqual(position["qty"], "0.10000000")
        self.assertEqual(position["peak_price"], "100")
        self.assertEqual(position["trail_pct"], "0.05")

    def test_state_file_top_level_marked_paper(self):
        self.entry_state()
        with open(self.state_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertIs(payload["paper"], True)
        self.assertEqual(payload["version"], 1)
        for record in payload["positions"].values():
            self.assertIs(record["paper"], True)

    def test_reload_then_same_candle_is_skipped_no_duplicate(self):
        _, closed, check = self.entry_state()
        reloaded = vo.load_paper_state(self.state_path)
        action = vo.apply_paper_candle(
            reloaded,
            check,
            closed[-1],
            Decimal("10"),
            Decimal("0.05"),
            self.receipts,
            "statetest02",
            fixed_now,
        )
        self.assertEqual(action, "skipped_candle_already_processed")
        self.assertEqual(len(self.read_receipts()), 1)
        self.assertEqual(len(reloaded.positions), 1)

    def test_open_position_blocks_second_entry_on_new_surge(self):
        _, _, _ = self.entry_state()
        reloaded = vo.load_paper_state(self.state_path)
        # A NEW surged candle arrives; symbol already has an open position, so
        # the position path (update) runs — never a second entry.
        next_rows = surge_kline_fixture()[:4] + [
            kline_row(4, quote_volume="500", close_price="110")
        ]
        closed = closed_fixture_candles(next_rows)
        check = vo.evaluate_surge(
            "AAAUSDT", "1m", closed, 3, Decimal("3"), FIXED_TS
        )
        self.assertTrue(check.surged)
        action = vo.apply_paper_candle(
            reloaded,
            check,
            closed[-1],
            Decimal("10"),
            Decimal("0.05"),
            self.receipts,
            "statetest03",
            fixed_now,
        )
        self.assertEqual(action, "position_updated")
        self.assertEqual(len(reloaded.positions), 1)
        self.assertEqual(reloaded.positions["AAAUSDT"]["peak_price"], "110")
        self.assertEqual(len(self.read_receipts()), 1)

    def test_trail_exit_closes_position_and_persists(self):
        self.entry_state()
        reloaded = vo.load_paper_state(self.state_path)
        # Peak first rises to 110, then a close at 104 <= 110 * 0.95 exits.
        rows = surge_kline_fixture()[:4] + [
            kline_row(4, quote_volume="10", close_price="110"),
            kline_row(5, quote_volume="10", close_price="104"),
        ]
        closed = closed_fixture_candles(rows)
        up_check = vo.evaluate_surge(
            "AAAUSDT", "1m", closed[:5], 3, Decimal("3"), FIXED_TS
        )
        action = vo.apply_paper_candle(
            reloaded,
            up_check,
            closed[4],
            Decimal("10"),
            Decimal("0.05"),
            self.receipts,
            "statetest04",
            fixed_now,
        )
        self.assertEqual(action, "position_updated")
        down_check = vo.evaluate_surge(
            "AAAUSDT", "1m", closed, 3, Decimal("3"), FIXED_TS
        )
        action = vo.apply_paper_candle(
            reloaded,
            down_check,
            closed[5],
            Decimal("10"),
            Decimal("0.05"),
            self.receipts,
            "statetest04",
            fixed_now,
        )
        self.assertEqual(action, "paper_exit")
        self.assertEqual(reloaded.positions, {})
        vo.save_paper_state(self.state_path, reloaded, fixed_now)
        after_restart = vo.load_paper_state(self.state_path)
        self.assertEqual(after_restart.positions, {})
        receipts = self.read_receipts()
        self.assertEqual(
            [record["kind"] for record in receipts], ["paper_entry", "paper_exit"]
        )
        exit_record = receipts[-1]
        self.assertIs(exit_record["paper"], True)
        self.assertEqual(exit_record["exit_reason"], "trail_stop_closed_candle")
        self.assertEqual(exit_record["price"], "104")
        self.assertEqual(exit_record["peak_price"], "110")
        self.assertEqual(exit_record["trail_stop_price"], "104.50")
        self.assertEqual(exit_record["paper_proceeds"], "10.40000000")
        self.assertEqual(exit_record["paper_pnl_quote"], "0.40000000")
        self.assertEqual(exit_record["entry_run_id"], "statetest01")

    def test_every_receipt_marked_paper(self):
        self.test_trail_exit_closes_position_and_persists()
        for record in self.read_receipts():
            self.assertIs(record["paper"], True)
            self.assertEqual(record["note"], "paper only; no real order path")

    def test_unrelated_position_survives_other_symbol_scan(self):
        state, _, _ = self.entry_state()
        closed = closed_fixture_candles(quiet_kline_fixture(), "BBBUSDT")
        check = vo.evaluate_surge(
            "BBBUSDT", "1m", closed, 3, Decimal("3"), FIXED_TS
        )
        action = vo.apply_paper_candle(
            state,
            check,
            closed[-1],
            Decimal("10"),
            Decimal("0.05"),
            self.receipts,
            "statetest05",
            fixed_now,
        )
        self.assertEqual(action, "no_action")
        vo.save_paper_state(self.state_path, state, fixed_now)
        reloaded = vo.load_paper_state(self.state_path)
        self.assertIn("AAAUSDT", reloaded.positions)
        self.assertEqual(reloaded.candle_marks["BBBUSDT"], closed[-1].close_time_ms)

    def test_interrupted_save_keeps_previous_state_intact(self):
        state, _, _ = self.entry_state()
        before = self.state_path.read_bytes()
        state.positions["AAAUSDT"]["peak_price"] = "999"
        with patch("os.replace", side_effect=OSError("killed mid-save")):
            with self.assertRaises(OSError):
                vo.save_paper_state(self.state_path, state, fixed_now)
        self.assertEqual(self.state_path.read_bytes(), before)
        reloaded = vo.load_paper_state(self.state_path)
        self.assertEqual(reloaded.positions["AAAUSDT"]["peak_price"], "100")

    def test_corrupt_state_file_raises_never_clobbers(self):
        self.state_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(vo.DataReadError):
            vo.load_paper_state(self.state_path)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "{not json")

    def test_wrong_version_rejected(self):
        payload = {"version": 99, "paper": True, "positions": {}, "candle_marks": {}}
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(vo.DataReadError):
            vo.load_paper_state(self.state_path)

    def test_unmarked_paper_state_rejected(self):
        payload = {"version": 1, "paper": False, "positions": {}, "candle_marks": {}}
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(vo.DataReadError):
            vo.load_paper_state(self.state_path)

    def test_position_missing_paper_flag_rejected(self):
        self.entry_state()
        with open(self.state_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        del payload["positions"]["AAAUSDT"]["paper"]
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(vo.DataReadError):
            vo.load_paper_state(self.state_path)

    def test_position_bad_numbers_rejected(self):
        self.entry_state()
        with open(self.state_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["positions"]["AAAUSDT"]["qty"] = "-1"
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(vo.DataReadError):
            vo.load_paper_state(self.state_path)

    def test_no_temp_residue_after_clean_save(self):
        self.entry_state()
        self.assertFalse(
            self.state_path.with_name(self.state_path.name + ".tmp").exists()
        )


class ScanFlowTests(NoNetworkTestCase):
    """run_scan and the scan CLI, entirely offline via patched fetchers."""

    def fake_fetch_payload(self, klines_by_symbol):
        def fake(path, params, timeout, source, max_bytes=vo.MAX_RESPONSE_BYTES):
            if path == vo.EXCHANGE_INFO_PATH:
                return exchange_info_fixture()
            if path == vo.KLINES_PATH:
                return klines_by_symbol[params["symbol"]]
            raise AssertionError(f"unexpected path {path}")

        return fake

    def run_scan_universe(self, klines_by_symbol, sleeps, **overrides):
        kwargs = dict(
            symbols=None,
            use_universe=True,
            interval="1m",
            baseline_candles=3,
            surge_multiple=Decimal("3"),
            max_symbols=2,
            timeout=5.0,
            paper_enabled=True,
            notional=Decimal("10"),
            trail_pct=Decimal("0.05"),
            receipt_path=self.receipts,
            state_path=self.state_path,
            run_id="scantest01",
            now_fn=fixed_now,
            now_ms_fn=lambda: BASE_OPEN_MS + 100 * MINUTE_MS,
            sleep_fn=sleeps.append,
        )
        kwargs.update(overrides)
        out = io.StringIO()
        with patch.object(
            vo, "fetch_public_payload", self.fake_fetch_payload(klines_by_symbol)
        ):
            with redirect_stdout(out):
                results = vo.run_scan(**kwargs)
        return results, out.getvalue()

    def default_klines(self):
        return {
            "AAAUSDT": surge_kline_fixture(),
            "BBBUSDT": quiet_kline_fixture(),
        }

    def test_universe_scan_rows_and_paper_entry(self):
        sleeps = []
        results, printed = self.run_scan_universe(self.default_klines(), sleeps)
        self.assertEqual(len(results), 2)
        (check_a, action_a), (check_b, action_b) = results
        self.assertEqual(check_a.symbol, "AAAUSDT")
        self.assertTrue(check_a.surged)
        self.assertEqual(action_a, "paper_entry")
        self.assertEqual(check_b.symbol, "BBBUSDT")
        self.assertFalse(check_b.surged)
        self.assertEqual(action_b, "no_action")
        self.assertIn("universe=" + vo.PUBLIC_BASE_URL + vo.EXCHANGE_INFO_PATH, printed)
        self.assertIn("universe_size=2", printed)
        self.assertIn(
            "request_budget=1_exchangeinfo_get+2_klines_gets; max_symbols_cap=2;"
            " spacing_seconds=0.25",
            printed,
        )
        self.assertIn("rule=" + vo.SURGE_RULE_TEXT, printed)
        self.assertIn("paper_actions=enabled", printed)
        self.assertIn("open_paper_positions=1", printed)
        state = vo.load_paper_state(self.state_path)
        self.assertIn("AAAUSDT", state.positions)

    def test_requests_spaced_never_hammered(self):
        sleeps = []
        self.run_scan_universe(self.default_klines(), sleeps)
        self.assertEqual(sleeps, [vo.REQUEST_SPACING_SECONDS])

    def test_second_scan_same_candles_skips_everything(self):
        sleeps = []
        self.run_scan_universe(self.default_klines(), sleeps)
        receipts_before = self.read_receipts()
        results, printed = self.run_scan_universe(
            self.default_klines(), sleeps, run_id="scantest02"
        )
        actions = [action for _, action in results]
        self.assertEqual(
            actions,
            ["skipped_candle_already_processed", "skipped_candle_already_processed"],
        )
        self.assertEqual(self.read_receipts(), receipts_before)
        state = vo.load_paper_state(self.state_path)
        self.assertEqual(len(state.positions), 1)

    def test_paper_disabled_scan_touches_no_state(self):
        sleeps = []
        results, printed = self.run_scan_universe(
            self.default_klines(), sleeps, paper_enabled=False
        )
        self.assertEqual(
            [action for _, action in results],
            ["paper_disabled", "paper_disabled"],
        )
        self.assertIn("paper_actions=disabled(observe_only)", printed)
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.receipts.exists())

    def test_explicit_symbols_deduped_uppercased_capped(self):
        sleeps = []
        klines = {
            "AAAUSDT": surge_kline_fixture(),
            "BBBUSDT": quiet_kline_fixture(),
        }
        results, printed = self.run_scan_universe(
            klines,
            sleeps,
            symbols=["aaausdt", "AAAUSDT", "bbbusdt"],
            use_universe=False,
        )
        self.assertEqual([check.symbol for check, _ in results], ["AAAUSDT", "BBBUSDT"])
        self.assertIn("universe=explicit_symbol_list", printed)
        self.assertIn("request_budget=2_klines_gets", printed)

    def test_insufficient_history_row_no_action(self):
        sleeps = []
        klines = {
            "AAAUSDT": surge_kline_fixture()[:2],
            "BBBUSDT": quiet_kline_fixture(),
        }
        results, _ = self.run_scan_universe(klines, sleeps)
        check, action = results[0]
        self.assertEqual(check.status, "insufficient_closed_candles")
        self.assertEqual(action, "no_action")

    def test_bad_max_symbols_rejected(self):
        with self.assertRaises(vo.PaperInputError):
            self.run_scan_universe(self.default_klines(), [], max_symbols=0)
        with self.assertRaises(vo.PaperInputError):
            self.run_scan_universe(
                self.default_klines(), [], max_symbols=vo.MAX_SCAN_SYMBOLS + 1
            )

    def test_cli_scan_kill_restart_no_duplicates(self):
        """CLI-level write-kill-reload: a rerun of the same scan duplicates nothing."""
        argv = [
            "scan",
            "--universe",
            "--interval",
            "1m",
            "--baseline",
            "3",
            "--multiple",
            "3",
            "--max-symbols",
            "2",
            "--paper",
            "--trail-pct",
            "0.05",
            "--receipts",
            str(self.receipts),
            "--state",
            str(self.state_path),
            "--run-id",
            "clirun01",
        ]
        fake = self.fake_fetch_payload(self.default_klines())
        with patch.object(vo, "fetch_public_payload", fake):
            with patch.object(vo.time, "sleep"):
                out = io.StringIO()
                with redirect_stdout(out):
                    first_code = vo.main(argv)
                # Simulated kill: the process is gone; only the files remain.
                out2 = io.StringIO()
                with redirect_stdout(out2):
                    second_code = vo.main(argv[:-1] + ["clirun02"])
        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        first_printed = out.getvalue()
        self.assertIn("paper_entry", first_printed)
        self.assertIn("universe_size=2", first_printed)
        self.assertIn("skipped_candle_already_processed", out2.getvalue())
        receipts = self.read_receipts()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["kind"], "paper_entry")
        self.assertIs(receipts[0]["paper"], True)
        state = vo.load_paper_state(self.state_path)
        self.assertEqual(list(state.positions), ["AAAUSDT"])

    def test_cli_state_command_prints_positions(self):
        sleeps = []
        self.run_scan_universe(self.default_klines(), sleeps)
        out = io.StringIO()
        with redirect_stdout(out):
            code = vo.main(["state", "--state", str(self.state_path)])
        self.assertEqual(code, 0)
        printed = out.getvalue()
        self.assertIn("open_paper_positions=1", printed)
        self.assertIn("AAAUSDT", printed)
        self.assertIn("paper=true", printed)

    def test_cli_scan_requires_symbol_source(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                vo.main(["scan"])


if __name__ == "__main__":
    unittest.main()
