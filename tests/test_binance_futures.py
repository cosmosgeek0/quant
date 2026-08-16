"""Offline contract pins for the shared USD-M Futures adapter."""
from __future__ import annotations
import copy
import sys
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from execution import binance_futures as futures
from execution import binance_http as http

BASE = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"
SHA = "1aa1e018740ae9dc012c1ebc58c4cf81a518df62"
D = Decimal

def ok(payload): return http.HttpResult("ok", payload, None, None)
def unclear(): return http.HttpResult("unclear", None, None, "offline_unclear")
def rejected(code=-2013): return http.HttpResult("api_error", None, code, "not found")

class Spy:
    def __init__(self, routes, events=None):
        self.routes = dict(routes); self.calls = []; self.events = events
    def __call__(self, method, base_url, path, params, signed):
        self.calls.append((method, base_url, path, params, signed))
        if self.events is not None: self.events.append(method + " " + path)
        value = self.routes.get((method, path), self.routes.get(path))
        if isinstance(value, list):
            if not value: raise AssertionError("offline route exhausted")
            return value.pop(0) if len(value) > 1 else value[0]
        if value is None: raise AssertionError("unexpected offline call " + method + " " + path)
        return value
    def posts(self): return [call for call in self.calls if call[0] == "POST"]

def symbol_row(symbol=SYMBOL):
    return {"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL",
            "quoteAsset": "USDT", "marginAsset": "USDT", "orderTypes": ["LIMIT", "MARKET"], "timeInForce": ["GTC", "IOC"],
            "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.1", "minPrice": "0.1", "maxPrice": "1000000"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "500"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        {"filterType": "PERCENT_PRICE", "multiplierUp": "1.20", "multiplierDown": "0.80"}]}

def market_spy(symbols):
    return Spy({"/fapi/v1/exchangeInfo": ok({"symbols": symbols}),
                "/fapi/v1/ticker/bookTicker": ok({"symbol": SYMBOL, "bidPrice": "99", "askPrice": "100"}),
                "/fapi/v1/premiumIndex": ok({"symbol": SYMBOL, "markPrice": "99.5"})})

def market():
    return futures.FuturesMarket(SYMBOL, "TRADING", "PERPETUAL", D("0.001"),
                                 D("0.001"), D("5"), D("1.20"), D("99"), D("100"), D("99.5"),
                                 D("0.1"), D("0.1"), D("1000000"), D("500"), D("0.80"),
                                 "USDT", "USDT", ("LIMIT", "MARKET"), ("GTC", "IOC"))

def preflight_routes():
    position = {"symbol": SYMBOL, "positionSide": "BOTH", "positionAmt": "0"}
    return {"/fapi/v1/accountConfig": ok({"canTrade": True}),
            "/fapi/v1/positionSide/dual": ok({"dualSidePosition": False}),
            "/fapi/v3/account": ok({"positions": [copy.deepcopy(position)]}),
            "/fapi/v1/openOrders": ok([]),
            "/fapi/v3/positionRisk": ok([copy.deepcopy(position)]),
            "/fapi/v3/balance": ok([{"asset": "USDT", "availableBalance": "120"}]),
            "/fapi/v1/symbolConfig": ok([{"symbol": SYMBOL, "leverage": "1"}])}

def order_payload(client_id, side="BUY", reduce=False):
    return {"symbol": SYMBOL, "clientOrderId": client_id, "orderId": 77,
            "status": "FILLED", "side": side, "reduceOnly": reduce,
            "executedQty": "1.234", "cumQuote": "123.4"}

class FuturesAdapterTest(unittest.TestCase):
    def test_market_scans_full_response_for_one_exact_symbol(self):
        transport = market_spy([symbol_row("ETHUSDT"), symbol_row()])
        result = futures.read_market(transport, BASE, SYMBOL, sleep=lambda _: None)
        self.assertEqual(result.kind, "ok"); self.assertEqual(result.value.symbol, SYMBOL)
        self.assertEqual((result.value.tick_size, result.value.min_price, result.value.max_price, result.value.max_quantity, result.value.multiplier_down),
                         (D("0.1"), D("0.1"), D("1000000"), D("500"), D("0.80")))
        self.assertEqual((result.value.quote_asset, result.value.margin_asset, result.value.order_types, result.value.time_in_force),
                         ("USDT", "USDT", ("LIMIT", "MARKET"), ("GTC", "IOC")))
        self.assertIsNone(transport.calls[0][3])
        for rows in ([symbol_row("ETHUSDT")], [symbol_row(), symbol_row()]):
            with self.subTest(count=len(rows)):
                self.assertEqual(futures.read_market(market_spy(rows), BASE, SYMBOL,
                                                     sleep=lambda _: None).kind, "unclear")
        for key in ("quoteAsset", "marginAsset", "orderTypes", "timeInForce"):
            row = symbol_row(); row.pop(key)
            self.assertEqual(futures.read_market(market_spy([row]), BASE, SYMBOL, sleep=lambda _: None).kind, "unclear")

    def test_preflight_proves_all_reads_and_banded_margin(self):
        transport = Spy(preflight_routes())
        result = futures.read_preflight(transport, BASE, market(), D("100"),
                                        require_flat=True, sleep=lambda _: None)
        self.assertEqual(result.kind, "ok"); self.assertEqual(result.value.required_margin, D("120.00"))
        self.assertEqual(result.value.available_balance, D("120"))
        self.assertEqual(len(transport.calls), 7); self.assertTrue(all(call[4] for call in transport.calls))
        routes = preflight_routes(); routes["/fapi/v3/balance"] = ok([{"asset": "USDT", "availableBalance": "119.99"}])
        self.assertEqual(futures.read_preflight(Spy(routes), BASE, market(), D("100"),
                                               require_flat=True, sleep=lambda _: None).kind, "unclear")
        routes = preflight_routes(); routes["/fapi/v3/balance"] = ok([{"asset": "T", "availableBalance": "120"}])
        self.assertEqual(futures.read_preflight(Spy(routes), BASE, market(), D("100"), require_flat=True, sleep=lambda _: None).kind, "unclear")
        for proposed in ("100", 100.0):
            guarded = Spy({})
            with self.assertRaises(http.AdapterRefusal): futures.read_preflight(guarded, BASE, market(), proposed, require_flat=True, sleep=lambda _: None)
            self.assertEqual(guarded.calls, [])
        guarded = Spy({})
        with self.assertRaises(http.AdapterRefusal):
            futures.read_preflight(guarded, BASE, market(), D("9" * 60), require_flat=True, sleep=lambda _: None)
        self.assertEqual(guarded.calls, [])

    def test_preflight_refuses_permission_mode_orders_and_nonflat_state(self):
        cases = []
        routes = preflight_routes(); routes["/fapi/v1/accountConfig"] = ok({"canTrade": False}); cases.append(routes)
        routes = preflight_routes(); routes["/fapi/v1/positionSide/dual"] = ok({"dualSidePosition": True}); cases.append(routes)
        routes = preflight_routes(); routes["/fapi/v1/openOrders"] = ok([{"symbol": SYMBOL, "orderId": 9}]); cases.append(routes)
        routes = preflight_routes()
        row = {"symbol": SYMBOL, "positionSide": "BOTH", "positionAmt": "0.1"}
        routes["/fapi/v3/account"] = ok({"positions": [row]}); routes["/fapi/v3/positionRisk"] = ok([row]); cases.append(routes)
        for index, one in enumerate(cases):
            with self.subTest(case=index):
                result = futures.read_preflight(Spy(one), BASE, market(), None,
                                                require_flat=True, sleep=lambda _: None)
                self.assertEqual(result.kind, "unclear")

    def test_leverage_is_read_only_from_one_exact_symbol_config_row(self):
        for payload, expected in (([{"symbol": SYMBOL, "leverage": "2"}], ("ok", 2)),
                                  ([], ("unclear", None)),
                                  ([{"symbol": "ETHUSDT", "leverage": "1"}], ("unclear", None)),
                                  ([{"symbol": SYMBOL, "leverage": "1"}, {"symbol": SYMBOL, "leverage": "2"}], ("unclear", None))):
            with self.subTest(payload=payload):
                result = futures.read_leverage(Spy({"/fapi/v1/symbolConfig": ok(payload)}),
                                                BASE, SYMBOL, sleep=lambda _: None)
                self.assertEqual((result.kind, result.value), expected)

    def test_set_leverage_writes_then_posts_once_and_proves_after(self):
        events = []; transport = Spy({"/fapi/v1/symbolConfig": [ok([{"symbol": SYMBOL, "leverage": "2"}]),
                                                                  ok([{"symbol": SYMBOL, "leverage": "1"}])],
                                      ("POST", "/fapi/v1/leverage"): ok({"leverage": 1})}, events)
        intents = []
        def writer(intent): events.append("WRITE"); intents.append(intent)
        result = futures.set_leverage_1x_once(transport, BASE, SYMBOL, "lev-run-1", SHA,
                                              writer, sleep=lambda _: None)
        self.assertEqual((result.kind, result.before, result.after, result.mutated), ("proven", 2, 1, True))
        self.assertEqual(events[1:3], ["WRITE", "POST /fapi/v1/leverage"])
        self.assertEqual(len(transport.posts()), 1); self.assertEqual(intents[0].params[-1], ("leverage", "1"))
        already = Spy({"/fapi/v1/symbolConfig": ok([{"symbol": SYMBOL, "leverage": "1"}])})
        self.assertFalse(futures.set_leverage_1x_once(already, BASE, SYMBOL, "lev-run-2", SHA,
                                                     lambda _: self.fail("writer called"), sleep=lambda _: None).mutated)
        failed = Spy({"/fapi/v1/symbolConfig": ok([{"symbol": SYMBOL, "leverage": "2"}])})
        with self.assertRaises(http.IntentNotDurable):
            futures.set_leverage_1x_once(failed, BASE, SYMBOL, "lev-run-3", SHA,
                                         lambda _: (_ for _ in ()).throw(ValueError("offline")), sleep=lambda _: None)
        self.assertEqual(failed.posts(), [])

    def test_open_floors_and_close_is_exact_reduce_only(self):
        opened = futures.build_open_order(market(), "BUY", "fut-open-1", SHA, D("1.2345"), profile="MARKET")
        self.assertEqual((opened.quantity, opened.residual), (D("1.234"), D("0.0005")))
        buy = futures.build_open_order(market(), "BUY", "fut-limit-b", SHA, D("1.234"), profile="LIMIT_IOC", limit_price=D("99.51"))
        sell = futures.build_open_order(market(), "SELL", "fut-limit-s", SHA, D("1.234"), profile="LIMIT_IOC", limit_price=D("99.59"))
        self.assertEqual((dict(buy.intent.params)["price"], dict(sell.intent.params)["price"]), ("99.6", "99.5"))
        closed = futures.build_reduce_only_close(market(), "SELL", "fut-close-1", SHA, D("1.234"))
        wire = dict(closed.intent.params)
        self.assertEqual((wire["type"], wire["positionSide"], wire["reduceOnly"], wire["quantity"]),
                         ("MARKET", "BOTH", "true", "1.234"))
        with self.assertRaises(http.AdapterRefusal):
            futures.build_reduce_only_close(market(), "SELL", "fut-close-2", SHA, D("1.2345"))
        with self.assertRaises(http.AdapterRefusal):
            futures.build_open_order(market(), "BUY", "fut-open-2", SHA, "1.234", profile="MARKET")
        for unavailable in (replace(market(), order_types=("MARKET",)), replace(market(), time_in_force=("GTC",)), replace(market(), symbol="bad!")):
            with self.assertRaises(http.AdapterRefusal): futures.build_open_order(unavailable, "BUY", "fut-open-4", SHA, D("1.234"), profile="LIMIT_IOC", limit_price=D("99.5"))

    def test_submit_posts_once_queries_exact_id_and_never_resends(self):
        plan = futures.build_open_order(market(), "BUY", "fut-open-3", SHA, D("1.234"), profile="MARKET")
        transport = Spy({("POST", "/fapi/v1/order"): unclear(),
                         ("GET", "/fapi/v1/order"): ok(order_payload("fut-open-3"))})
        result = futures.submit_order_once(transport, BASE, plan, lambda _: None, sleep=lambda _: None)
        self.assertEqual(result.tag, "terminal"); self.assertEqual(len(transport.posts()), 1)
        queries = [call for call in transport.calls if call[0] == "GET"]
        self.assertEqual(dict(queries[0][3])["origClientOrderId"], "fut-open-3")
        absent = Spy({("POST", "/fapi/v1/order"): rejected(-2019),
                      ("GET", "/fapi/v1/order"): rejected()})
        result = futures.submit_order_once(absent, BASE, plan, lambda _: None, sleep=lambda _: None)
        self.assertEqual(result.tag, "error"); self.assertEqual(len(absent.posts()), 1)
        delays = []; pending = Spy({("POST", "/fapi/v1/order"): unclear(), ("GET", "/fapi/v1/order"): unclear()})
        result = futures.submit_order_once(pending, BASE, plan, lambda _: None, sleep=delays.append)
        queries = [call for call in pending.calls if call[0] == "GET"]
        self.assertEqual((result.tag, len(pending.posts()), len(queries)), ("pending", 1, http.ORDER_QUERY_ATTEMPTS))
        self.assertEqual(delays, [http.ORDER_QUERY_DELAY_SECONDS] * (http.ORDER_QUERY_ATTEMPTS - 1))

    def test_close_submit_requires_query_reduce_only_truth(self):
        plan = futures.build_reduce_only_close(market(), "SELL", "fut-close-3", SHA, D("1.234"))
        payload = order_payload("fut-close-3", "SELL", False)
        transport = Spy({("POST", "/fapi/v1/order"): ok({"clientOrderId": "fut-close-3", "reduceOnly": True}),
                         ("GET", "/fapi/v1/order"): ok(payload)})
        result = futures.submit_order_once(transport, BASE, plan, lambda _: None, sleep=lambda _: None)
        self.assertEqual((result.tag, result.returned_client_id), ("pending", "fut-close-3"))
        self.assertEqual(len(transport.posts()), 1)

    def test_plan_faults_refuse_before_intent_or_wire(self):
        built = futures.build_open_order(market(), "BUY", "fut-open-5", SHA, D("1.234"), profile="MARKET")
        params = built.intent.params; nan = tuple((key, "NaN") if key == "quantity" else pair for pair in params for key in (pair[0],))
        cases = (replace(built, intent=replace(built.intent, git_sha="short")), replace(built, intent=replace(built.intent, git_sha="z" * 40)),
                 replace(built, quantity=1.234), replace(built, intent=replace(built.intent, params=((['symbol'], SYMBOL),) + params[1:])),
                 replace(built, intent=replace(built.intent, params=nan)))
        for index, crafted in enumerate(cases):
            with self.subTest(case=index):
                transport, writes = Spy({}), []
                with self.assertRaises(http.AdapterRefusal): futures.submit_order_once(transport, BASE, crafted, writes.append)
                self.assertEqual((writes, transport.calls), ([], []))

    def test_fill_fee_and_income_ledgers_preserve_decimal_truth(self):
        trades = [{"symbol": SYMBOL, "id": 1, "orderId": 77, "price": "100", "qty": "1",
                   "quoteQty": "100", "commission": "0.04", "commissionAsset": "USDT", "side": "BUY", "maker": False, "time": 1001, "realizedPnl": "1.25"},
                  {"symbol": SYMBOL, "id": 2, "orderId": 77, "price": "100", "qty": "0.234",
                   "quoteQty": "23.4", "commission": "0.002", "commissionAsset": "BNB", "buyer": True, "maker": True, "time": 1002, "realizedPnl": "-0.10"}]
        direct_spy = Spy({futures.FUTURES_USER_TRADES_PATH: ok(trades)}); direct = futures.read_user_trades(direct_spy, BASE, SYMBOL, order_id=77, sleep=lambda _: None)
        self.assertEqual((direct.kind, direct_spy.calls[0][2]), ("ok", futures.FUTURES_USER_TRADES_PATH))
        self.assertEqual((direct.value[0].side, direct.value[0].is_maker, direct.value[0].time_ms, direct.value[0].realized_pnl), ("BUY", False, 1001, D("1.25")))
        self.assertEqual((direct.value[1].side, direct.value[1].is_maker, direct.value[1].time_ms, direct.value[1].realized_pnl), ("BUY", True, 1002, D("-0.10")))
        identity = http.OrderIdentity(SYMBOL, "fut-open-4", 77)
        order = http.OrderResult("terminal", identity, "fut-open-4", "FILLED", True, "BUY", False,
                                 D("1.234"), D("123.4"), (), None)
        coverage = futures.read_fills(Spy({futures.FUTURES_USER_TRADES_PATH: ok(trades)}), BASE, order,
                                      sleep=lambda _: None).value
        self.assertTrue(coverage.complete); self.assertEqual(coverage.covered_quantity, D("1.234"))
        self.assertEqual(coverage.fees, (http.Fee("BNB", D("0.002")), http.Fee("USDT", D("0.04"))))
        wrong = copy.deepcopy(trades); wrong[0]["orderId"] = 78
        self.assertEqual(futures.read_fills(Spy({"/fapi/v1/userTrades": ok(wrong)}), BASE,
                                             order, sleep=lambda _: None).kind, "unclear")
        valid = http.Fill(3, 77, D("100"), D("1.234"), D("123.4"), http.Fee("USDT", D("0")), "BUY", False, 1003, D("0"))
        invalid = (replace(valid, order_id=78), replace(valid, price=D("0")), replace(valid, quantity=D("0")),
                   replace(valid, quantity="1.234"), replace(valid, fee=http.Fee("USDT", D("-0.1"))),
                   replace(valid, fee=http.Fee("", D("0"))), replace(valid, side="HOLD"), replace(valid, is_maker=0),
                   replace(valid, time_ms="1003"), replace(valid, realized_pnl="0"))
        for supplied in invalid:
            with self.subTest(fill=supplied):
                feed = Spy({}); result = futures.read_fills(feed, BASE, order, (supplied,), sleep=lambda _: None)
                self.assertEqual((result.kind, feed.calls), ("unclear", []))
        feed = Spy({}); result = futures.read_fills(feed, BASE, order, [valid], sleep=lambda _: None)
        self.assertEqual((result.kind, feed.calls), ("unclear", []))
        self.assertEqual((futures.FUTURES_INCOME_PATH, futures.FUTURES_ORDER_PATH), ("/fapi/v1/income", "/fapi/v1/order"))
        income = [{"tranId": index, "symbol": SYMBOL, "incomeType": kind, "income": amount,
                   "asset": "USDT", "time": 1000 + index, "tradeId": ""}
                  for index, kind, amount in ((1, "COMMISSION", "-0.04"),
                                               (2, "REALIZED_PNL", "1.25"),
                                               (3, "FUNDING_FEE", "-0.01"))]
        rows = futures.read_income(Spy({futures.FUTURES_INCOME_PATH: ok(income)}), BASE, SYMBOL,
                                   0, 2000, sleep=lambda _: None).value
        self.assertEqual(tuple(row.income_type for row in rows), ("COMMISSION", "REALIZED_PNL", "FUNDING_FEE"))
        self.assertTrue(all(type(row.amount) is Decimal for row in rows))

if __name__ == "__main__":
    unittest.main()
