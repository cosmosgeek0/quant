#!/usr/bin/env python3
"""Offline deterministic tests for the futures_hand SHORT side.

Every test injects a fake transport in place of ``futures_hand.http`` and arms
hard guards that fail the test immediately if anything touches
``urllib.request.urlopen`` or opens a socket. No network is possible.
"""

import contextlib
import importlib.util
import io
import os
import socket
import sys
import tempfile
import unittest
import urllib.request
from decimal import Decimal
from unittest import mock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(BASE_DIR, "src", "futures_hand.py")
SPEC = importlib.util.spec_from_file_location("futures_hand_under_test",
                                              MODULE_PATH)
fh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fh)

SHA40 = "1aa1e018740ae9dc012c1ebc58c4cf81a518df62"
TEN = Decimal("10")
BPS10 = Decimal("10")
BOOK = {"bidPrice": "99.90", "askPrice": "100.00"}
MARK = {"markPrice": "99.95"}


def symbol_info(**overrides):
    info = {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "orderTypes": ["LIMIT", "MARKET"],
        "timeInForce": ["GTC", "IOC"],
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10",
             "minPrice": "0.10", "maxPrice": "1000000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001",
             "minQty": "0.001", "maxQty": "1000"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001",
             "minQty": "0.001", "maxQty": "500"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
            {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05",
             "multiplierDown": "0.95"},
        ],
    }
    info.update(overrides)
    return info


def set_filter(info, filter_type, **values):
    for item in info["filters"]:
        if item["filterType"] == filter_type:
            item.update(values)
    return info


FLAT_EXTRA = {
    "/fapi/v3/positionRisk": ("ok", [{"symbol": "BTCUSDT",
                                      "positionSide": "BOTH",
                                      "positionAmt": "-0.099"}]),
    "/fapi/v1/openOrders": ("ok", []),
    "/fapi/v1/symbolConfig": ("ok", [{"symbol": "BTCUSDT", "leverage": "1"}]),
}


class FakeTransport:
    """Stands in for futures_hand.http and records every call it receives."""

    def __init__(self, post_result=None, order_results=(), extra=None):
        self.calls = []
        self.post_result = post_result
        self.order_results = list(order_results)
        self.extra = dict(extra or {})

    def __call__(self, method, path, params=None, signed=False):
        self.calls.append((method, path, dict(params or {}), signed))
        if method == "POST":
            if self.post_result is None:
                raise AssertionError("unexpected offline POST to " + path)
            return self.post_result
        if path == "/fapi/v1/order" and self.order_results:
            if len(self.order_results) > 1:
                return self.order_results.pop(0)
            return self.order_results[0]
        if path in self.extra:
            return self.extra[path]
        raise AssertionError("unexpected offline request: %s %s" % (method,
                                                                    path))

    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]

    def order_queries(self):
        return [call for call in self.calls
                if call[0] == "GET" and call[1] == "/fapi/v1/order"]


class OfflineCase(unittest.TestCase):
    """Common per-test module state plus hard no-network guards."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        values = {
            "SYMBOL": "BTCUSDT",
            "QUANT_SHA": SHA40,
            "MAX_NOTIONAL_RAW": "10",
            "MAX_SLIPPAGE_BPS_RAW": "10",
            "API_KEY": "",
            "API_SECRET": "",
            "RECEIPT_DIR": tmp.name,
            "LOCK_PATH": os.path.join(tmp.name, "futures-hand.lock"),
            "receipt": {},
            "receipt_path": "",
            "ctx": {"lock": False, "mutated": False},
            "_time_offset_ms": 0,
            "POLL_SECONDS": 0.5,
            "POLL_SLEEP": 0.002,
        }
        for name, value in values.items():
            patcher = mock.patch.object(fh, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        def deny(*_args, **_kwargs):
            raise AssertionError("offline test attempted a network call")

        for target, attribute in ((urllib.request, "urlopen"),
                                  (socket, "socket"),
                                  (socket, "create_connection")):
            patcher = mock.patch.object(target, attribute, deny)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def short_plan():
        return fh.plan_short_open(symbol_info(), BOOK, MARK, TEN, BPS10)


class TestShortPlan(OfflineCase):
    def test_short_plan_sizes_worst_case_notional_at_cap(self):
        plan = self.short_plan()
        self.assertEqual(plan["open_side"], "SELL")
        self.assertEqual(plan["quantity"], Decimal("0.099"))
        self.assertEqual(plan["limit_price"], Decimal("99.8"))
        self.assertEqual(plan["max_fill_notional"], Decimal("9.9099"))
        self.assertLessEqual(plan["max_fill_notional"], fh.LANE_HARD_CEILING)
        self.assertLessEqual(plan["limit_price"], plan["bid"])

    def test_short_plan_refuses_above_hard_ceiling(self):
        with self.assertRaisesRegex(fh.PreflightError, "hard ceiling"):
            fh.plan_short_open(symbol_info(), BOOK, MARK, Decimal("10.5"),
                               BPS10)

    def test_short_plan_refuses_below_min_notional(self):
        info = set_filter(symbol_info(), "MIN_NOTIONAL", notional="20")
        with self.assertRaisesRegex(fh.PreflightError, "MIN_NOTIONAL"):
            fh.plan_short_open(info, BOOK, MARK, TEN, BPS10)

    def test_short_plan_refuses_percent_price_floor(self):
        info = set_filter(symbol_info(), "PERCENT_PRICE",
                          multiplierDown="0.999")
        with self.assertRaisesRegex(fh.PreflightError, "PERCENT_PRICE"):
            fh.plan_short_open(info, BOOK, MARK, TEN, BPS10)

    def test_short_plan_refuses_non_trading_symbol(self):
        with self.assertRaisesRegex(fh.PreflightError, "TRADING"):
            fh.plan_short_open(symbol_info(status="BREAK"), BOOK, MARK, TEN,
                               BPS10)

    def test_short_plan_refuses_non_perpetual(self):
        info = symbol_info(contractType="CURRENT_QUARTER")
        with self.assertRaisesRegex(fh.PreflightError, "PERPETUAL"):
            fh.plan_short_open(info, BOOK, MARK, TEN, BPS10)

    def test_short_plan_refuses_non_usdt_symbol(self):
        with self.assertRaisesRegex(fh.PreflightError, "USDT"):
            fh.plan_short_open(symbol_info(quoteAsset="USDC"), BOOK, MARK,
                               TEN, BPS10)

    def test_short_plan_refuses_missing_ioc(self):
        info = symbol_info(timeInForce=["GTC"])
        with self.assertRaisesRegex(fh.PreflightError, "IOC"):
            fh.plan_short_open(info, BOOK, MARK, TEN, BPS10)

    def test_short_plan_refuses_non_positive_bid(self):
        book = {"bidPrice": "0", "askPrice": "100.00"}
        with self.assertRaisesRegex(fh.PreflightError, "non-positive"):
            fh.plan_short_open(symbol_info(), book, MARK, TEN, BPS10)

    def test_short_plan_refuses_zero_floored_sell_limit(self):
        book = {"bidPrice": "0.05", "askPrice": "100.00"}
        with self.assertRaisesRegex(fh.PreflightError, "not positive"):
            fh.plan_short_open(symbol_info(), book, MARK, TEN, BPS10)

    def test_plan_open_refuses_invalid_side(self):
        with self.assertRaisesRegex(fh.PreflightError, "BUY or SELL"):
            fh.plan_open(symbol_info(), BOOK, MARK, TEN, BPS10,
                         open_side="HOLD")

    def test_long_plan_math_is_unchanged(self):
        plan = fh.plan_open(symbol_info(), BOOK, MARK, TEN, BPS10)
        self.assertEqual(plan["open_side"], "BUY")
        self.assertEqual(plan["quantity"], Decimal("0.099"))
        self.assertEqual(plan["limit_price"], Decimal("100.1"))
        self.assertEqual(plan["max_fill_notional"], Decimal("9.9099"))


class TestIntents(OfflineCase):
    def test_sell_open_intent_shape(self):
        client_id = fh.SHORT_LANE_PREFIX + "run1-o"
        params = fh.short_open_intent(self.short_plan(), client_id)
        self.assertEqual(params["side"], "SELL")
        self.assertEqual(params["type"], "LIMIT")
        self.assertEqual(params["timeInForce"], "IOC")
        self.assertEqual(params["reduceOnly"], "false")
        self.assertEqual(params["quantity"], "0.099")
        self.assertEqual(params["price"], "99.80")
        self.assertEqual(params["newClientOrderId"], client_id)
        self.assertTrue(params["newClientOrderId"].startswith("l2fs-"))

    def test_reduce_only_buy_close_intent_shape(self):
        client_id = fh.SHORT_LANE_PREFIX + "run1-c"
        params = fh.short_close_intent(Decimal("0.099"), client_id)
        self.assertEqual(params["side"], "BUY")
        self.assertEqual(params["type"], "MARKET")
        self.assertEqual(params["reduceOnly"], "true")
        self.assertEqual(params["quantity"], "0.099")
        self.assertEqual(params["newClientOrderId"], client_id)
        self.assertTrue(params["newClientOrderId"].startswith("l2fs-"))

    def test_close_intent_is_reduce_only_for_both_hands(self):
        for side in ("BUY", "SELL"):
            params = fh.close_intent(side, Decimal("0.099"), "x-c")
            self.assertEqual(params["reduceOnly"], "true")

    def test_intents_refuse_invalid_side_or_quantity(self):
        with self.assertRaisesRegex(fh.PreflightError, "BUY or SELL"):
            fh.open_intent("HOLD", self.short_plan(), "x-o")
        with self.assertRaisesRegex(fh.PreflightError, "BUY or SELL"):
            fh.close_intent("HOLD", Decimal("0.099"), "x-c")
        with self.assertRaisesRegex(fh.PreflightError, "positive"):
            fh.close_intent("BUY", Decimal("0"), "x-c")

    def test_short_hand_wiring_and_distinct_prefix(self):
        self.assertEqual(fh.HANDS["short"]["prefix"], "l2fs-")
        self.assertEqual(fh.HANDS["short"]["open_side"], "SELL")
        self.assertEqual(fh.HANDS["short"]["close_side"], "BUY")
        self.assertNotEqual(fh.HANDS["short"]["prefix"],
                            fh.HANDS["long"]["prefix"])


class TestSubmitQueryBeforeRetry(OfflineCase):
    def test_unclear_submit_queries_exact_id_and_never_resends(self):
        client_id = fh.SHORT_LANE_PREFIX + "run1-o"
        transport = FakeTransport(
            post_result=("unclear", {"error": "TimeoutError"}),
            order_results=[
                ("ok", {"status": "NEW", "clientOrderId": client_id}),
                ("ok", {"status": "FILLED", "clientOrderId": client_id,
                        "orderId": 4321, "executedQty": "0.099",
                        "cumQuote": "9.88"}),
            ],
        )
        with mock.patch.object(fh, "http", transport):
            order = fh.submit_once(
                "OPEN", fh.short_open_intent(self.short_plan(), client_id),
                "up to 9.9099 USDT",
            )
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(len(transport.posts()), 1)
        queries = transport.order_queries()
        self.assertGreaterEqual(len(queries), 2)
        for _method, _path, params, signed in queries:
            self.assertTrue(signed)
            self.assertEqual(params.get("origClientOrderId"), client_id)

    def test_pending_query_stays_unknown_without_resend(self):
        client_id = fh.SHORT_LANE_PREFIX + "run2-o"
        transport = FakeTransport(
            post_result=("unclear", {"error": "TimeoutError"}),
            order_results=[("ok", {"status": "NEW",
                                   "clientOrderId": client_id})],
            extra=FLAT_EXTRA,
        )
        out = io.StringIO()
        with mock.patch.object(fh, "http", transport), \
                mock.patch.object(fh, "POLL_SECONDS", 0.05), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                fh.submit_once(
                    "OPEN",
                    fh.short_open_intent(self.short_plan(), client_id),
                    "up to 9.9099 USDT",
                )
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(fh.receipt["verdict"], "UNKNOWN")
        self.assertEqual(len(transport.posts()), 1)
        self.assertIn("VERDICT: UNKNOWN", out.getvalue())

    def test_unfound_id_after_unclear_submit_stays_unknown(self):
        client_id = fh.SHORT_LANE_PREFIX + "run3-o"
        transport = FakeTransport(
            post_result=("unclear", {"error": "TimeoutError"}),
            order_results=[("api_error", {"code": -2013,
                                          "msg": "Order does not exist."})],
            extra=FLAT_EXTRA,
        )
        out = io.StringIO()
        with mock.patch.object(fh, "http", transport), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                fh.submit_once(
                    "OPEN",
                    fh.short_open_intent(self.short_plan(), client_id),
                    "up to 9.9099 USDT",
                )
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(fh.receipt["verdict"], "UNKNOWN")
        self.assertEqual(len(transport.posts()), 1)
        self.assertGreaterEqual(len(transport.order_queries()), 8)

    def test_rejected_short_open_refuses_after_id_query(self):
        client_id = fh.SHORT_LANE_PREFIX + "run4-o"
        transport = FakeTransport(
            post_result=("api_error", {"code": -2019,
                                       "msg": "Margin is insufficient."}),
            order_results=[("api_error", {"code": -2013,
                                          "msg": "Order does not exist."})],
            extra=FLAT_EXTRA,
        )
        out = io.StringIO()
        with mock.patch.object(fh, "http", transport), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                fh.submit_once(
                    "OPEN",
                    fh.short_open_intent(self.short_plan(), client_id),
                    "up to 9.9099 USDT",
                )
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(fh.receipt["verdict"], "REJECTED_BEFORE_OPEN")
        self.assertIn("rejected by Binance", fh.receipt["verdict_note"])
        self.assertEqual(len(transport.posts()), 1)
        self.assertGreaterEqual(len(transport.order_queries()), 8)

    def test_rejected_short_close_reports_exposure_without_retry(self):
        client_id = fh.SHORT_LANE_PREFIX + "run5-c"
        transport = FakeTransport(
            post_result=("api_error", {"code": -2022,
                                       "msg": "ReduceOnly Order is rejected."}),
            order_results=[("api_error", {"code": -2013,
                                          "msg": "Order does not exist."})],
            extra=FLAT_EXTRA,
        )
        out = io.StringIO()
        with mock.patch.object(fh, "http", transport), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                fh.submit_once(
                    "CLOSE",
                    fh.short_close_intent(Decimal("0.099"), client_id),
                    "0.099 BTCUSDT",
                )
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(fh.receipt["verdict"], "OPEN_EXPOSURE")
        self.assertEqual(len(transport.posts()), 1)

    def test_terminal_identity_mismatch_stays_unknown(self):
        client_id = fh.SHORT_LANE_PREFIX + "run6-o"
        transport = FakeTransport(
            post_result=("unclear", {"error": "TimeoutError"}),
            order_results=[("ok", {"status": "FILLED",
                                   "clientOrderId": "someone-else"})],
            extra=FLAT_EXTRA,
        )
        out = io.StringIO()
        with mock.patch.object(fh, "http", transport), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                fh.submit_once(
                    "OPEN",
                    fh.short_open_intent(self.short_plan(), client_id),
                    "up to 9.9099 USDT",
                )
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(fh.receipt["verdict"], "UNKNOWN")
        self.assertEqual(fh.receipt["first_failure"]["where"], "query_order")
        self.assertEqual(len(transport.posts()), 1)


class ShortExecuteTransport:
    """Serves one full offline ``--short --execute`` run deterministically.

    ``/fapi/v1/order`` GETs echo back the queried client order id so the
    run ids generated inside ``main`` always match; the open and close fills
    are injected per test. Leverage is already 1x so the only POSTs a run
    may make are the open and close orders.
    """

    def __init__(self, open_fill, close_fill):
        self.calls = []
        self.open_fill = dict(open_fill)
        self.close_fill = dict(close_fill)
        self.reads = {
            "/fapi/v1/time": ("ok", {"serverTime": 1755000000000}),
            "/fapi/v1/exchangeInfo": ("ok", {"symbols": [symbol_info()]}),
            "/fapi/v1/ticker/bookTicker": ("ok", dict(BOOK)),
            "/fapi/v1/premiumIndex": ("ok", dict(MARK)),
            "/fapi/v3/account": ("ok", {"canTrade": True,
                                        "availableBalance": "100",
                                        "positions": []}),
            "/fapi/v1/positionSide/dual": ("ok", {"dualSidePosition": False}),
            "/fapi/v1/openOrders": ("ok", []),
            "/fapi/v3/positionRisk": ("ok", []),
            "/fapi/v1/symbolConfig": ("ok", [{"symbol": "BTCUSDT",
                                              "leverage": "1"}]),
        }

    def __call__(self, method, path, params=None, signed=False):
        params = dict(params or {})
        self.calls.append((method, path, params, signed))
        if method == "POST":
            if path != "/fapi/v1/order":
                raise AssertionError("unexpected offline POST to " + path)
            return "ok", {}
        if path == "/fapi/v1/order":
            client_id = params.get("origClientOrderId", "")
            fill = self.open_fill if client_id.endswith("-o") \
                else self.close_fill
            return "ok", dict(fill, clientOrderId=client_id)
        if path in self.reads:
            return self.reads[path]
        raise AssertionError("unexpected offline request: %s %s" % (method,
                                                                    path))

    def order_posts(self):
        return [call for call in self.calls if call[0] == "POST"]


OPEN_FILL_BREACH = {"status": "FILLED", "orderId": 111,
                    "executedQty": "0.099", "cumQuote": "10.098"}
OPEN_FILL_UNDER_CAP = {"status": "FILLED", "orderId": 111,
                       "executedQty": "0.099", "cumQuote": "9.88"}
CLOSE_FILL = {"status": "FILLED", "orderId": 222, "executedQty": "0.099",
              "cumQuote": "10.10", "reduceOnly": True}


class TestShortNotionalBreachContainment(OfflineCase):
    """A sell fill above the cap must be flattened and named, never UNKNOWN."""

    def run_short_execute(self, open_fill, close_fill=CLOSE_FILL):
        transport = ShortExecuteTransport(open_fill, close_fill)
        out = io.StringIO()
        argv = ["futures_hand.py", "--short", "--execute"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(fh, "API_KEY", "offline-test-key"), \
                mock.patch.object(fh, "API_SECRET", "offline-test-secret"), \
                mock.patch.object(fh, "http", transport), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                fh.main()
        return transport, out.getvalue(), caught.exception.code

    def test_breach_is_detected_flattened_and_honestly_named(self):
        transport, text, code = self.run_short_execute(OPEN_FILL_BREACH)
        self.assertEqual(code, 3)
        self.assertEqual(fh.receipt["verdict"], "CEILING_BREACH_CONTAINED")
        self.assertEqual(fh.receipt["first_failure"]["where"], "hard_ceiling")
        self.assertIn("flattened", fh.receipt["verdict_note"])
        self.assertIn("10.098", fh.receipt["verdict_note"])
        posts = transport.order_posts()
        self.assertEqual(len(posts), 2)
        open_post, close_post = posts[0][2], posts[1][2]
        self.assertEqual(open_post["side"], "SELL")
        self.assertEqual(open_post["reduceOnly"], "false")
        self.assertEqual(close_post["side"], "BUY")
        self.assertEqual(close_post["type"], "MARKET")
        self.assertEqual(close_post["reduceOnly"], "true")
        self.assertEqual(close_post["quantity"], "0.099")
        self.assertTrue(close_post["newClientOrderId"].startswith("l2fs-"))
        self.assertIn("VERDICT: CEILING_BREACH_CONTAINED", text)
        self.assertEqual(fh.receipt["final_state"]["position_amount"],
                         Decimal("0"))
        self.assertTrue(os.path.exists(fh.LOCK_PATH))

    def test_no_breach_short_run_still_ends_closed(self):
        transport, text, code = self.run_short_execute(OPEN_FILL_UNDER_CAP)
        self.assertEqual(code, 0)
        self.assertEqual(fh.receipt["verdict"], "CLOSED")
        self.assertNotIn("first_failure", fh.receipt)
        self.assertEqual(len(transport.order_posts()), 2)
        self.assertIn("VERDICT: CLOSED", text)
        self.assertFalse(os.path.exists(fh.LOCK_PATH))

    def test_breach_with_partial_close_reports_exposure_not_containment(self):
        partial_close = dict(CLOSE_FILL, executedQty="0.050")
        transport, text, code = self.run_short_execute(OPEN_FILL_BREACH,
                                                       partial_close)
        self.assertEqual(code, 3)
        self.assertEqual(fh.receipt["verdict"], "OPEN_EXPOSURE")
        self.assertNotIn("CEILING_BREACH_CONTAINED", text)
        self.assertEqual(len(transport.order_posts()), 2)


class TestConfigRefusals(OfflineCase):
    def test_config_accepts_capped_notional(self):
        self.assertEqual(fh.config(), (Decimal("10"), Decimal("10")))

    def test_config_refuses_notional_above_ceiling(self):
        with mock.patch.object(fh, "MAX_NOTIONAL_RAW", "10.01"):
            with self.assertRaisesRegex(fh.PreflightError, "10 USDT"):
                fh.config()

    def test_config_refuses_invalid_symbol(self):
        with mock.patch.object(fh, "SYMBOL", "btc!"):
            with self.assertRaisesRegex(fh.PreflightError, "SYMBOL"):
                fh.config()

    def test_config_refuses_bad_slippage(self):
        with mock.patch.object(fh, "MAX_SLIPPAGE_BPS_RAW", "101"):
            with self.assertRaisesRegex(fh.PreflightError, "SLIPPAGE"):
                fh.config()


class TestMainShortRouting(OfflineCase):
    def test_main_short_validate_blocks_without_credentials(self):
        transport = FakeTransport(extra={
            "/fapi/v1/time": ("ok", {"serverTime": 1755000000000}),
            "/fapi/v1/exchangeInfo": ("ok", {"symbols": [symbol_info()]}),
            "/fapi/v1/ticker/bookTicker": ("ok", dict(BOOK)),
            "/fapi/v1/premiumIndex": ("ok", dict(MARK)),
        })
        out = io.StringIO()
        argv = ["futures_hand.py", "--short", "--validate-only"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(fh, "http", transport), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                fh.main()
        self.assertEqual(caught.exception.code, 3)
        text = out.getvalue()
        self.assertIn("hand: short", text)
        self.assertIn("VERDICT: BLOCKED", text)
        self.assertEqual(len(transport.posts()), 0)
        self.assertEqual(fh.receipt.get("hand"), "short")

    def test_main_refuses_unknown_argument_order(self):
        out = io.StringIO()
        argv = ["futures_hand.py", "--execute", "--short"]
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                fh.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("usage:", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
