#!/usr/bin/env python3
"""Offline pins for the adapter-backed futures_hand shard."""

import contextlib
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.request
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR / "src"))
MODULE_PATH = BASE_DIR / "shards" / "futures_hand__W2.py"
SPEC = importlib.util.spec_from_file_location("futures_hand_under_test", MODULE_PATH)
fh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fh)

from execution import binance_futures as bf
from execution import binance_http as bh

SHA40 = "1aa1e018740ae9dc012c1ebc58c4cf81a518df62"
CAP = Decimal("32")
BPS10 = Decimal("10")
SYMBOL = "BTCUSDT"


def ok(payload):
    return bh.HttpResult("ok", payload, None, None)


def unclear():
    return bh.HttpResult("unclear", None, None, "offline_unclear")


def rejected(code=-2013):
    return bh.HttpResult("api_error", None, code, "not found")


def symbol_info(**overrides):
    info = {
        "symbol": SYMBOL, "status": "TRADING", "contractType": "PERPETUAL",
        "quoteAsset": "USDT", "marginAsset": "USDT",
        "orderTypes": ["LIMIT", "MARKET"], "timeInForce": ["GTC", "IOC"],
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


def market(**overrides):
    value = bf.FuturesMarket(
        SYMBOL, "TRADING", "PERPETUAL", Decimal("0.001"), Decimal("0.001"),
        Decimal("5"), Decimal("1.05"), Decimal("99.90"), Decimal("100.00"),
        Decimal("99.95"), Decimal("0.10"), Decimal("0.10"),
        Decimal("1000000"), Decimal("500"), Decimal("0.95"), "USDT", "USDT",
        ("LIMIT", "MARKET"), ("GTC", "IOC"))
    return replace(value, **overrides)


def flat_routes():
    position = {"symbol": SYMBOL, "positionSide": "BOTH", "positionAmt": "0"}
    return {
        "/fapi/v1/accountConfig": ok({"canTrade": True}),
        "/fapi/v1/positionSide/dual": ok({"dualSidePosition": False}),
        "/fapi/v3/account": ok({"positions": [dict(position)]}),
        "/fapi/v1/openOrders": ok([]),
        "/fapi/v3/positionRisk": ok([dict(position)]),
        "/fapi/v3/balance": ok([{"asset": "USDT", "availableBalance": "100"}]),
        "/fapi/v1/symbolConfig": ok([{"symbol": SYMBOL, "leverage": "1"}]),
    }


def order_payload(client_id, side="SELL", reduce_only=False, quantity="0.319",
                  quote="31.88", order_id=4321, status="FILLED"):
    return {
        "symbol": SYMBOL, "status": status, "clientOrderId": client_id,
        "orderId": order_id, "side": side, "reduceOnly": reduce_only,
        "executedQty": quantity, "cumQuote": quote,
    }


class FakeTransport:
    def __init__(self, post_result=None, order_results=(), extra=None):
        self.calls = []
        self.post_result = post_result
        self.order_results = list(order_results)
        self.extra = dict(extra or {})

    def __call__(self, method, base_url, path, params, signed):
        self.calls.append((method, base_url, path, params, signed))
        if method == "POST":
            if self.post_result is None:
                raise AssertionError("unexpected offline POST to " + path)
            return self.post_result
        if path == bf.FUTURES_ORDER_PATH and self.order_results:
            if len(self.order_results) > 1:
                return self.order_results.pop(0)
            return self.order_results[0]
        if path in self.extra:
            return self.extra[path]
        raise AssertionError("unexpected offline request: %s %s" % (method, path))

    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]

    def order_queries(self):
        return [call for call in self.calls
                if call[0] == "GET" and call[2] == bf.FUTURES_ORDER_PATH]


class OfflineCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        values = {
            "SYMBOL": SYMBOL, "QUANT_SHA": SHA40, "MAX_NOTIONAL_RAW": "32",
            "MAX_SLIPPAGE_BPS_RAW": "10", "API_KEY": "offline-test-key",
            "API_SECRET": "offline-test-secret", "RECEIPT_DIR": self.root,
            "LOCK_PATH": os.path.join(self.root, "futures-hand.lock"),
            "receipt": {}, "receipt_path": "",
            "ctx": {"lock": False, "mutated": False}, "transport": None,
            "_sleep": lambda _seconds: None,
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
        return fh.plan_short_open(market(), CAP, BPS10)

    def open_plan(self, client_id):
        planned = self.short_plan()
        return bf.build_open_order(
            market(), "SELL", client_id, SHA40, planned["quantity"],
            profile="LIMIT_IOC", limit_price=planned["limit_price"])

    @staticmethod
    def close_plan(client_id):
        return bf.build_reduce_only_close(
            market(), "BUY", client_id, SHA40, Decimal("0.319"))

    def arm(self, transport, open_id=None, close_id=None):
        open_id = open_id or fh.SHORT_LANE_PREFIX + "offline-o"
        close_id = close_id or fh.SHORT_LANE_PREFIX + "offline-c"
        fh.receipt["hand"] = "short"
        fh.acquire_run("offline", open_id, close_id,
                       fh.SHORT_LANE_PREFIX + "offline-l", CAP)
        fh.ctx["market"] = market()
        fh.transport = transport


class TestHttpParsing(OfflineCase):
    def test_http_decodes_numeric_mark_price_as_decimal(self):
        raw = io.BytesIO(
            b'{"markPrice":12345.678901234567890123,"serverTime":1755000000000}')
        raw.getcode = lambda: 200
        client = bh.BinanceHttp("offline-key", "offline-secret")
        with mock.patch.object(client, "_open", return_value=raw):
            result = client("GET", bh.FUTURES_BASE_URL,
                            "/fapi/v1/premiumIndex", None, False)
        self.assertEqual(result.kind, "ok")
        self.assertEqual(result.payload["markPrice"],
                         Decimal("12345.678901234567890123"))
        self.assertIs(type(result.payload["markPrice"]), Decimal)
        self.assertNotIsInstance(result.payload["markPrice"], float)
        self.assertIs(type(result.payload["serverTime"]), int)


class TestShortPlan(OfflineCase):
    def test_short_plan_sizes_worst_case_notional_at_cap(self):
        plan = self.short_plan()
        self.assertEqual(plan["open_side"], "SELL")
        self.assertEqual(plan["quantity"], Decimal("0.319"))
        self.assertEqual(plan["limit_price"], Decimal("99.8"))
        self.assertEqual(plan["max_fill_notional"], Decimal("31.9319"))
        self.assertLessEqual(plan["max_fill_notional"], fh.LANE_HARD_CEILING)
        self.assertLessEqual(plan["limit_price"], plan["bid"])

    def test_short_plan_refuses_above_hard_ceiling(self):
        with self.assertRaisesRegex(fh.PreflightError, "hard ceiling"):
            fh.plan_short_open(market(), Decimal("32.5"), BPS10)

    def test_short_plan_refuses_below_min_notional(self):
        with self.assertRaisesRegex(fh.PreflightError, "MIN_NOTIONAL"):
            fh.plan_short_open(market(min_notional=Decimal("40")), CAP, BPS10)

    def test_short_plan_refuses_percent_price_floor(self):
        with self.assertRaisesRegex(fh.PreflightError, "PERCENT_PRICE"):
            fh.plan_short_open(market(multiplier_down=Decimal("0.999")), CAP, BPS10)

    def test_short_plan_refuses_non_trading_symbol(self):
        with self.assertRaisesRegex(fh.PreflightError, "TRADING"):
            fh.plan_short_open(market(status="BREAK"), CAP, BPS10)

    def test_short_plan_refuses_non_perpetual(self):
        with self.assertRaisesRegex(fh.PreflightError, "PERPETUAL"):
            fh.plan_short_open(market(contract_type="CURRENT_QUARTER"), CAP, BPS10)

    def test_short_plan_refuses_non_usdt_symbol(self):
        with self.assertRaisesRegex(fh.PreflightError, "USDT"):
            fh.plan_short_open(market(quote_asset="USDC"), CAP, BPS10)

    def test_short_plan_refuses_missing_ioc(self):
        with self.assertRaisesRegex(fh.PreflightError, "IOC"):
            fh.plan_short_open(market(time_in_force=("GTC",)), CAP, BPS10)

    def test_short_plan_refuses_non_positive_bid(self):
        with self.assertRaisesRegex(fh.PreflightError, "non-positive"):
            fh.plan_short_open(market(bid=Decimal("0")), CAP, BPS10)

    def test_short_plan_refuses_zero_floored_sell_limit(self):
        with self.assertRaisesRegex(fh.PreflightError, "not positive"):
            fh.plan_short_open(market(bid=Decimal("0.05")), CAP, BPS10)

    def test_plan_open_refuses_invalid_side(self):
        with self.assertRaisesRegex(fh.PreflightError, "BUY or SELL"):
            fh.plan_open(market(), CAP, BPS10, open_side="HOLD")

    def test_long_plan_math_is_unchanged(self):
        plan = fh.plan_open(market(), CAP, BPS10)
        self.assertEqual(plan["open_side"], "BUY")
        self.assertEqual(plan["quantity"], Decimal("0.319"))
        self.assertEqual(plan["limit_price"], Decimal("100.1"))
        self.assertEqual(plan["max_fill_notional"], Decimal("31.9319"))


class TestIntents(OfflineCase):
    def test_sell_open_intent_shape(self):
        client_id = fh.SHORT_LANE_PREFIX + "run1-o"
        params = dict(self.open_plan(client_id).intent.params)
        self.assertEqual(params["side"], "SELL")
        self.assertEqual(params["type"], "LIMIT")
        self.assertEqual(params["timeInForce"], "IOC")
        self.assertEqual(params["reduceOnly"], "false")
        self.assertEqual(params["quantity"], "0.319")
        self.assertEqual(params["price"], "99.80")
        self.assertEqual(params["newClientOrderId"], client_id)
        self.assertTrue(params["newClientOrderId"].startswith("l2fs-"))

    def test_reduce_only_buy_close_intent_shape(self):
        client_id = fh.SHORT_LANE_PREFIX + "run1-c"
        params = dict(self.close_plan(client_id).intent.params)
        self.assertEqual(params["side"], "BUY")
        self.assertEqual(params["type"], "MARKET")
        self.assertEqual(params["reduceOnly"], "true")
        self.assertEqual(params["quantity"], "0.319")
        self.assertEqual(params["newClientOrderId"], client_id)
        self.assertTrue(params["newClientOrderId"].startswith("l2fs-"))

    def test_close_intent_is_reduce_only_for_both_hands(self):
        for side in ("BUY", "SELL"):
            plan = bf.build_reduce_only_close(
                market(), side, "x-c", SHA40, Decimal("0.319"))
            self.assertEqual(dict(plan.intent.params)["reduceOnly"], "true")

    def test_intents_refuse_invalid_side_or_quantity(self):
        planned = self.short_plan()
        with self.assertRaisesRegex(bh.AdapterRefusal, "order shape"):
            bf.build_open_order(market(), "HOLD", "x-o", SHA40,
                                planned["quantity"], profile="LIMIT_IOC",
                                limit_price=planned["limit_price"])
        with self.assertRaisesRegex(bh.AdapterRefusal, "order shape"):
            bf.build_reduce_only_close(
                market(), "HOLD", "x-c", SHA40, Decimal("0.319"))
        with self.assertRaisesRegex(bh.AdapterRefusal, "outside its factual range"):
            bf.build_reduce_only_close(market(), "BUY", "x-c", SHA40, Decimal("0"))

    def test_short_hand_wiring_and_distinct_prefix(self):
        self.assertEqual(fh.HANDS["short"]["prefix"], "l2fs-")
        self.assertEqual(fh.HANDS["short"]["open_side"], "SELL")
        self.assertEqual(fh.HANDS["short"]["close_side"], "BUY")
        self.assertNotEqual(fh.HANDS["short"]["prefix"], fh.HANDS["long"]["prefix"])


class TestSubmitQueryBeforeRetry(OfflineCase):
    def test_unclear_submit_queries_exact_id_and_never_resends(self):
        client_id = fh.SHORT_LANE_PREFIX + "run1-o"
        transport = FakeTransport(
            post_result=unclear(),
            order_results=[ok(order_payload(client_id, status="NEW")),
                           ok(order_payload(client_id))])
        self.arm(transport, open_id=client_id)
        order = fh._submit_order(
            "OPEN", self.open_plan(client_id), "up to 31.9319 USDT")
        self.assertEqual(order.status, "FILLED")
        self.assertEqual(len(transport.posts()), 1)
        queries = transport.order_queries()
        self.assertGreaterEqual(len(queries), 2)
        for _method, _base, _path, params, signed in queries:
            self.assertTrue(signed)
            self.assertEqual(dict(params).get("origClientOrderId"), client_id)

    def test_pending_query_stays_unknown_without_resend(self):
        client_id = fh.SHORT_LANE_PREFIX + "run2-o"
        transport = FakeTransport(
            post_result=unclear(),
            order_results=[ok(order_payload(client_id, status="NEW"))],
            extra=flat_routes())
        self.arm(transport, open_id=client_id)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                fh._submit_order(
                    "OPEN", self.open_plan(client_id), "up to 31.9319 USDT")
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(fh.receipt["verdict"], "UNKNOWN")
        self.assertEqual(len(transport.posts()), 1)
        self.assertIn("VERDICT: UNKNOWN", out.getvalue())

    def test_unfound_id_after_unclear_submit_stays_unknown(self):
        client_id = fh.SHORT_LANE_PREFIX + "run3-o"
        transport = FakeTransport(
            post_result=unclear(), order_results=[rejected()], extra=flat_routes())
        self.arm(transport, open_id=client_id)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                fh._submit_order(
                    "OPEN", self.open_plan(client_id), "up to 31.9319 USDT")
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(fh.receipt["verdict"], "UNKNOWN")
        self.assertEqual(len(transport.posts()), 1)
        self.assertGreaterEqual(len(transport.order_queries()), 8)

    def test_rejected_short_open_refuses_after_id_query(self):
        client_id = fh.SHORT_LANE_PREFIX + "run4-o"
        transport = FakeTransport(
            post_result=rejected(-2019), order_results=[rejected()], extra=flat_routes())
        self.arm(transport, open_id=client_id)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                fh._submit_order(
                    "OPEN", self.open_plan(client_id), "up to 31.9319 USDT")
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(fh.receipt["verdict"], "REJECTED_BEFORE_OPEN")
        self.assertIn("rejected by Binance", fh.receipt["verdict_note"])
        self.assertEqual(len(transport.posts()), 1)
        self.assertGreaterEqual(len(transport.order_queries()), 8)

    def test_rejected_short_close_reports_exposure_without_retry(self):
        client_id = fh.SHORT_LANE_PREFIX + "run5-c"
        transport = FakeTransport(
            post_result=rejected(-2022), order_results=[rejected()], extra=flat_routes())
        self.arm(transport, close_id=client_id)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                fh._submit_order("CLOSE", self.close_plan(client_id), "0.319 BTCUSDT")
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(fh.receipt["verdict"], "OPEN_EXPOSURE")
        self.assertEqual(len(transport.posts()), 1)

    def test_terminal_identity_mismatch_stays_unknown(self):
        client_id = fh.SHORT_LANE_PREFIX + "run6-o"
        wrong = order_payload("someone-else")
        transport = FakeTransport(
            post_result=unclear(), order_results=[ok(wrong)], extra=flat_routes())
        self.arm(transport, open_id=client_id)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                fh._submit_order(
                    "OPEN", self.open_plan(client_id), "up to 31.9319 USDT")
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(fh.receipt["verdict"], "UNKNOWN")
        self.assertEqual(fh.receipt["first_failure"]["where"], "query_order")
        self.assertEqual(len(transport.posts()), 1)


OPEN_FILL_BREACH = {"status": "FILLED", "orderId": 111,
                    "executedQty": "0.319", "cumQuote": "32.538"}
OPEN_FILL_UNDER_CAP = {"status": "FILLED", "orderId": 111,
                       "executedQty": "0.319", "cumQuote": "31.88"}
CLOSE_FILL = {"status": "FILLED", "orderId": 222,
              "executedQty": "0.319", "cumQuote": "32.10", "reduceOnly": True}


class ShortExecuteTransport:
    def __init__(self, open_fill, close_fill):
        self.calls = []
        self.open_fill = dict(open_fill)
        self.close_fill = dict(close_fill)
        self.routes = flat_routes()
        self.routes.update({
            "/fapi/v1/exchangeInfo": ok({"symbols": [symbol_info()]}),
            "/fapi/v1/ticker/bookTicker": ok(
                {"symbol": SYMBOL, "bidPrice": "99.90", "askPrice": "100.00"}),
            "/fapi/v1/premiumIndex": ok(
                {"symbol": SYMBOL, "markPrice": "99.95"}),
        })

    def sync_time(self, base_url, path):
        self.calls.append(("SYNC", base_url, path, None, False))
        return ok({"serverTime": 1755000000000})

    def __call__(self, method, base_url, path, params, signed):
        self.calls.append((method, base_url, path, params, signed))
        wire = dict(params or ())
        if method == "POST":
            if path != bf.FUTURES_ORDER_PATH:
                raise AssertionError("unexpected offline POST to " + path)
            stage = "open" if wire["newClientOrderId"].endswith("-o") else "close"
            with open(fh.receipt_path, encoding="utf-8") as handle:
                durable = json.load(handle)
            if stage not in durable.get("intents", {}):
                raise AssertionError("durable intent must exist before POST")
            return ok({"clientOrderId": wire["newClientOrderId"],
                       "reduceOnly": wire["reduceOnly"] == "true"})
        if path == bf.FUTURES_ORDER_PATH:
            client_id = wire.get("origClientOrderId", "")
            fill = self.open_fill if client_id.endswith("-o") else self.close_fill
            return ok(dict(fill, symbol=SYMBOL, clientOrderId=client_id,
                           side="SELL" if client_id.endswith("-o") else "BUY",
                           reduceOnly=not client_id.endswith("-o")))
        if path == bf.FUTURES_USER_TRADES_PATH:
            order_id = int(wire["orderId"])
            fill = self.open_fill if order_id == self.open_fill["orderId"] else self.close_fill
            side = "SELL" if order_id == self.open_fill["orderId"] else "BUY"
            return ok([{
                "symbol": SYMBOL, "id": order_id + 1000, "orderId": order_id,
                "price": "100", "qty": fill["executedQty"],
                "quoteQty": fill["cumQuote"], "commission": "0",
                "commissionAsset": "USDT", "side": side, "maker": False,
                "time": 1755000001000 + order_id, "realizedPnl": "0",
            }])
        if path in self.routes:
            return self.routes[path]
        raise AssertionError("unexpected offline request: %s %s" % (method, path))

    def order_posts(self):
        return [call for call in self.calls if call[0] == "POST"]


class TestShortNotionalBreachContainment(OfflineCase):
    def run_short_execute(self, open_fill, close_fill=CLOSE_FILL):
        transport = ShortExecuteTransport(open_fill, close_fill)
        out = io.StringIO()
        argv = ["futures_hand.py", "--short", "--execute"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(fh, "_new_transport", return_value=transport), \
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
        self.assertIn("32.538", fh.receipt["verdict_note"])
        posts = transport.order_posts()
        self.assertEqual(len(posts), 2)
        open_post, close_post = dict(posts[0][3]), dict(posts[1][3])
        self.assertEqual(open_post["side"], "SELL")
        self.assertEqual(open_post["reduceOnly"], "false")
        self.assertEqual(close_post["side"], "BUY")
        self.assertEqual(close_post["type"], "MARKET")
        self.assertEqual(close_post["reduceOnly"], "true")
        self.assertEqual(close_post["quantity"], "0.319")
        self.assertTrue(close_post["newClientOrderId"].startswith("l2fs-"))
        self.assertIn("VERDICT: CEILING_BREACH_CONTAINED", text)
        self.assertEqual(fh.receipt["final_state"]["position_amount"], Decimal("0"))
        self.assertTrue(os.path.exists(fh.LOCK_PATH))
        self.assertTrue(fh.receipt["open"]["fills_complete"])
        self.assertTrue(fh.receipt["close"]["fills_complete"])

    def test_no_breach_short_run_still_ends_closed(self):
        transport, text, code = self.run_short_execute(OPEN_FILL_UNDER_CAP)
        self.assertEqual(code, 0)
        self.assertEqual(fh.receipt["verdict"], "CLOSED")
        self.assertNotIn("first_failure", fh.receipt)
        self.assertEqual(len(transport.order_posts()), 2)
        self.assertIn("VERDICT: CLOSED", text)
        self.assertFalse(os.path.exists(fh.LOCK_PATH))

    def test_breach_with_partial_close_reports_exposure_not_containment(self):
        partial_close = dict(CLOSE_FILL, executedQty="0.160")
        transport, text, code = self.run_short_execute(OPEN_FILL_BREACH, partial_close)
        self.assertEqual(code, 3)
        self.assertEqual(fh.receipt["verdict"], "OPEN_EXPOSURE")
        self.assertNotIn("CEILING_BREACH_CONTAINED", text)
        self.assertEqual(len(transport.order_posts()), 2)


class TestConfigRefusals(OfflineCase):
    def test_config_accepts_capped_notional(self):
        self.assertEqual(fh.config(), (Decimal("32"), Decimal("10")))

    def test_config_refuses_notional_above_ceiling(self):
        with mock.patch.object(fh, "MAX_NOTIONAL_RAW", "32.01"):
            with self.assertRaisesRegex(fh.PreflightError, "32 USDT"):
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
        transport = FakeTransport()
        out = io.StringIO()
        argv = ["futures_hand.py", "--short", "--validate-only"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(fh, "API_KEY", ""), \
                mock.patch.object(fh, "API_SECRET", ""), \
                mock.patch.object(fh, "_new_transport", return_value=transport), \
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
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                fh.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("usage:", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
