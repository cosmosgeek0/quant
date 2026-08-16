"""Offline contract tests for the shared Binance Spot adapter."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sys
import unittest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from execution import binance_http as http  # noqa: E402
from execution import binance_spot as spot  # noqa: E402

BASE, SYMBOL, CLIENT = "https://example.invalid", "BTCUSDT", "spot-order-1"
SHA = "a" * 40


def ok(payload): return http.HttpResult("ok", payload, None, None)
def api(code=-2013): return http.HttpResult("api_error", {"code": code}, code, "exchange reply")
def unclear(): return http.HttpResult("unclear", None, None, "offline uncertainty")


class Feed:
    def __init__(self, *results): self.results, self.calls = list(results), []

    def __call__(self, method, base_url, path, params, signed):
        self.calls.append((method, base_url, path, params, signed))
        if not self.results: raise AssertionError("unexpected offline request")
        return self.results.pop(0)


def market(**changes):
    values = dict(symbol=SYMBOL, base_asset="BTC", quote_asset="USDT",
                  status="TRADING", step_size=Decimal("0.01"),
                  min_quantity=Decimal("0.01"), min_notional=Decimal("10"),
                  bid=Decimal("1000"), ask=Decimal("1001"))
    values.update(changes); return spot.SpotMarket(**values)


def order_payload(status="FILLED", executed="1", client=CLIENT, order_id=7):
    return {"symbol": SYMBOL, "clientOrderId": client, "orderId": order_id,
            "status": status, "side": "BUY", "executedQty": executed,
            "cummulativeQuoteQty": str(Decimal(executed) * Decimal("10"))}


def fill(trade_id=1, quantity="1", asset="BTC", fee="0.01", side="BUY", is_maker=False, when=100):
    price = Decimal("10"); quantity = Decimal(quantity)
    return http.Fill(trade_id, 7, price, quantity, price * quantity,
                     http.Fee(asset, Decimal(fee)), side, is_maker, when, None)


def trade(trade_id=1, quantity="1", asset="BTC", fee="0.01", when=100, is_buyer=True, is_maker=False):
    return {"symbol": SYMBOL, "id": trade_id, "orderId": 7, "price": "10",
            "qty": quantity, "quoteQty": str(Decimal(quantity) * Decimal("10")),
            "commissionAsset": asset, "commission": fee, "time": when,
            "isBuyer": is_buyer, "isMaker": is_maker}


def terminal(executed="1", response_fills=()):
    return http.OrderResult("terminal", http.OrderIdentity(SYMBOL, CLIENT, 7),
                            CLIENT, "FILLED", True, "BUY", False,
                            Decimal(executed), Decimal(executed) * Decimal("10"),
                            tuple(response_fills), None)


def plan(): return spot.build_market_order(market(), "BUY", CLIENT, SHA, quantity=Decimal("0.02"))


class SpotAdapterTests(unittest.TestCase):
    def test_public_surface_and_state_reads_are_exact(self):
        filters = [{"filterType": "LOT_SIZE", "stepSize": "0.002", "minQty": "0.002"},
                   {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.003", "minQty": "0.006"},
                   {"filterType": "MIN_NOTIONAL", "minNotional": "10"}]
        info = {"symbols": [{"symbol": SYMBOL, "status": "TRADING",
                              "baseAsset": "BTC", "quoteAsset": "USDT",
                              "filters": filters}]}
        feed = Feed(ok(info), ok({"symbol": SYMBOL, "bidPrice": "100", "askPrice": "101"}))
        result = spot.read_market(feed, BASE, SYMBOL, sleep=lambda _delay: None)
        self.assertEqual((result.kind, result.value.step_size, result.value.min_quantity),
                         ("ok", Decimal("0.006"), Decimal("0.006")))
        self.assertEqual([call[2] for call in feed.calls],
                         ["/api/v3/exchangeInfo", "/api/v3/ticker/bookTicker"])
        account = {"canTrade": False, "balances": [
            {"asset": "BTC", "free": "1.25", "locked": "0.1"}]}
        feed = Feed(ok(account), ok([{"symbol": SYMBOL, "orderId": 8}]))
        state = spot.read_account_state(feed, BASE, SYMBOL).value
        self.assertEqual((state.can_trade, state.open_order_ids), (False, (8,)))
        self.assertEqual(state.balances[0].free, Decimal("1.25"))
        self.assertEqual(set(spot.__all__), {"SpotMarket", "SpotAccountState",
                         "read_market", "read_account_state", "build_market_order",
                         "query_order", "submit_order_once", "read_my_trades",
                         "read_fills", "fees_by_asset", "net_base_quantity", "SPOT_MY_TRADES_PATH", "SPOT_ORDER_PATH"})
        self.assertEqual((spot.SPOT_MY_TRADES_PATH, spot.SPOT_ORDER_PATH), ("/api/v3/myTrades", "/api/v3/order"))

    def test_builder_floors_step_and_uses_the_executable_side(self):
        built = spot.build_market_order(market(), "SELL", CLIENT, SHA,
                                        quantity=Decimal("0.016"))
        self.assertEqual((built.quantity, built.residual),
                         (Decimal("0.01"), Decimal("0.006")))
        self.assertEqual(dict(built.intent.params)["quantity"], "0.01")
        self.assertEqual((built.intent.venue, built.intent.operation),
                         ("BINANCE_SPOT", "ORDER"))
        with self.assertRaises(http.AdapterRefusal):
            spot.build_market_order(market(bid=Decimal("999.99")), "SELL",
                                    CLIENT, SHA, quantity=Decimal("0.01"))
        buy = spot.build_market_order(market(bid=Decimal("999.99")), "BUY",
                                      CLIENT, SHA, quantity=Decimal("0.01"))
        self.assertEqual(buy.quantity, Decimal("0.01"))

    def test_quote_limit_is_buy_only_and_exclusive(self):
        built = spot.build_market_order(market(), "BUY", CLIENT, SHA,
                                        quote_limit=Decimal("11"))
        self.assertEqual(dict(built.intent.params)["quoteOrderQty"], "11")
        for side, quantity, quote in (("SELL", None, Decimal("11")),
                                      ("BUY", Decimal("1"), Decimal("11")),
                                      ("BUY", None, None), ("BUY", 1.0, None)):
            with self.subTest(side=side, quantity=quantity, quote=quote), \
                    self.assertRaises(http.AdapterRefusal):
                spot.build_market_order(market(), side, CLIENT, SHA,
                                        quantity=quantity, quote_limit=quote)

    def test_query_terminal_preserves_partial_facts(self):
        feed = Feed(ok(order_payload("CANCELED", "0.4")))
        result = spot.query_order(feed, BASE, SYMBOL, CLIENT)
        self.assertEqual((result.tag, result.status, result.terminal),
                         ("terminal", "CANCELED", True))
        self.assertEqual((result.identity.order_id, result.executed_quantity),
                         (7, Decimal("0.4")))
        self.assertEqual(feed.calls[0][3],
                         (("symbol", SYMBOL), ("origClientOrderId", CLIENT)))

    def test_query_absence_needs_eight_consecutive_replies(self):
        feed = Feed(*([api()] * 7 + [ok(order_payload("NEW", "0"))] + [api()] * 8))
        waits = []; result = spot.query_order(feed, BASE, SYMBOL, CLIENT, sleep=waits.append)
        self.assertEqual((result.tag, len(feed.calls), len(waits)),
                         ("not_found", 16, 15))
        self.assertIsNone(result.error)

    def test_query_error_and_budget_exhaustion_keep_their_tags(self):
        rejected = spot.query_order(Feed(api(-1003)), BASE, SYMBOL, CLIENT)
        self.assertEqual((rejected.tag, rejected.error.code), ("error", -1003))
        waits = []; feed = Feed(*[unclear() for _ in range(http.ORDER_QUERY_ATTEMPTS)])
        pending = spot.query_order(feed, BASE, SYMBOL, CLIENT, sleep=waits.append)
        self.assertEqual((pending.tag, len(feed.calls), waits),
                         ("pending", 45, [2] * 44))

    def test_submit_writes_first_posts_once_and_keeps_full_fills(self):
        posted = {"clientOrderId": CLIENT, "orderId": 7, "fills": [
            {"tradeId": 1, "price": "10", "qty": "0.02",
             "commissionAsset": "BTC", "commission": "0.0001",
             "isMaker": False, "time": 100}]}
        replies, events = [ok(posted), ok(order_payload(executed="0.02"))], []

        def transport(method, base_url, path, params, signed):
            events.append(("wire", method, path)); return replies.pop(0)

        result = spot.submit_order_once(
            transport, BASE, plan(), lambda intent: events.append(("intent", intent)))
        self.assertEqual((sum(event[0] == "intent" for event in events),
                          sum(event[:2] == ("wire", "POST") for event in events),
                          events[0][0], events[1][:2], result.tag, len(result.response_fills)),
                         (1, 1, "intent", ("wire", "POST"), "terminal", 1))
        self.assertEqual((result.response_fills[0].side, result.response_fills[0].is_maker, result.response_fills[0].time_ms, result.response_fills[0].realized_pnl), ("BUY", False, 100, None))
        feed = Feed(ok(posted))
        with self.assertRaises(http.IntentNotDurable):
            spot.submit_order_once(feed, BASE, plan(),
                                   lambda _intent: (_ for _ in ()).throw(RuntimeError("write unavailable")))
        self.assertEqual(feed.calls, [])

    def test_crafted_invalid_plan_is_refused_before_receipt_or_post(self):
        built = plan()
        params = tuple((key, "NaN") if key == "quantity" else (key, value)
                       for key, value in built.intent.params)
        crafted = replace(built, quantity=1.0,
                          intent=replace(built.intent, params=params))
        feed, writes = Feed(), []
        with self.assertRaises(http.AdapterRefusal):
            spot.submit_order_once(feed, BASE, crafted, writes.append)
        self.assertEqual((writes, feed.calls), ([], []))

    def test_returned_id_mismatch_stays_pending_without_resend(self):
        feed = Feed(ok({"clientOrderId": "other-id"}), ok(order_payload()))
        result = spot.submit_order_once(feed, BASE, plan(), lambda _intent: None)
        self.assertEqual((result.tag, result.returned_client_id),
                         ("pending", "other-id"))
        self.assertEqual([call[0] for call in feed.calls].count("POST"), 1)

    def test_unclear_post_and_absent_order_stay_pending(self):
        feed = Feed(unclear(), *[api() for _ in range(8)])
        result = spot.submit_order_once(
            feed, BASE, plan(), lambda _intent: None, sleep=lambda _delay: None)
        self.assertEqual((result.tag, len(feed.calls)), ("pending", 9))
        self.assertEqual([call[0] for call in feed.calls].count("POST"), 1)

    def test_explicit_rejection_needs_exact_absence(self):
        absent = Feed(api(-1013), *[api() for _ in range(8)])
        result = spot.submit_order_once(
            absent, BASE, plan(), lambda _intent: None, sleep=lambda _delay: None)
        self.assertEqual((result.tag, result.error.code), ("error", -1013))
        present = Feed(api(-1013), ok(order_payload()))
        result = spot.submit_order_once(present, BASE, plan(), lambda _intent: None)
        self.assertEqual(result.tag, "terminal")
        self.assertEqual([call[0] for call in present.calls].count("POST"), 1)

    def test_trade_reads_require_one_bounded_selector(self):
        feed = Feed(ok([trade()]))
        result = spot.read_my_trades(feed, BASE, SYMBOL, order_id=7)
        self.assertEqual((result.kind, result.value[0].quantity),
                         ("ok", Decimal("1")))
        self.assertEqual(dict(feed.calls[0][3])["limit"], "1000")
        window = spot.read_my_trades(
            Feed(ok([trade(when=150)])), BASE, SYMBOL, start_ms=100, end_ms=200)
        self.assertEqual(window.kind, "ok")
        with self.assertRaises(http.AdapterRefusal):
            spot.read_my_trades(Feed(), BASE, SYMBOL, order_id=7,
                                start_ms=100, end_ms=200)

    def test_my_trades_preserves_measured_fill_facts(self):
        feed = Feed(ok([trade(when=321, is_buyer=False, is_maker=True)]))
        result = spot.read_my_trades(feed, BASE, SYMBOL, order_id=7)
        self.assertEqual((result.value[0].side, result.value[0].is_maker,
                          result.value[0].time_ms, result.value[0].realized_pnl),
                         ("SELL", True, 321, None))
        self.assertEqual(feed.calls[0][2], spot.SPOT_MY_TRADES_PATH)

    def test_my_trades_without_buyer_fact_is_unclear(self):
        row = trade(); del row["isBuyer"]
        result = spot.read_my_trades(Feed(ok([row])), BASE, SYMBOL, order_id=7)
        self.assertEqual(result.kind, "unclear")

    def test_full_fills_precede_trade_reads_and_ids_deduplicate(self):
        full = fill(quantity="1")
        feed = Feed(); covered = spot.read_fills(feed, BASE, terminal("1", (full,)))
        self.assertTrue(covered.value.complete); self.assertEqual(feed.calls, [])
        partial = fill(quantity="0.4")
        feed = Feed(ok([trade(1, "0.4"), trade(2, "0.6", "USDT", "0.02")]))
        result = spot.read_fills(feed, BASE, terminal("1"), (partial,),
                                 sleep=lambda _delay: None)
        self.assertEqual((result.kind, result.value.complete,
                          result.value.covered_quantity),
                         ("ok", True, Decimal("1.0")))
        self.assertEqual([row.trade_id for row in result.value.fills], [1, 2])
        self.assertEqual(len(feed.calls), 1)

    def test_incomplete_fill_coverage_stays_unsized_after_five_reads(self):
        feed = Feed(*[ok([]) for _ in range(5)]); waits = []
        result = spot.read_fills(feed, BASE, terminal("1"),
                                 (fill(quantity="0.2"),), sleep=waits.append)
        self.assertEqual((result.kind, result.value.complete,
                          result.value.covered_quantity),
                         ("ok", False, Decimal("0.2")))
        self.assertEqual((len(feed.calls), waits), (5, [1] * 5))

    def test_fee_totals_are_sorted_decimals_and_missing_fees_refuse(self):
        fills = (fill(1, "0.4", "BTC", "0.001"),
                 fill(2, "0.6", "USDT", "0.02"),
                 fill(3, "0.1", "BTC", "0.002"))
        fees = spot.fees_by_asset(fills)
        self.assertEqual(fees, (http.Fee("BTC", Decimal("0.003")),
                                http.Fee("USDT", Decimal("0.02"))))
        with self.assertRaises(http.AdapterRefusal):
            spot.fees_by_asset((replace(fill(), fee=http.Fee("BTC", None)),))

    def test_net_base_excludes_quote_and_third_asset_fees(self):
        fees = (http.Fee("BTC", Decimal("0.1")),
                http.Fee("USDT", Decimal("2")), http.Fee("BNB", Decimal("0.01")))
        self.assertEqual(spot.net_base_quantity(Decimal("1"), "BTC", fees),
                         Decimal("0.9"))
        with self.assertRaises(http.AdapterRefusal):
            spot.net_base_quantity(Decimal("-1"), "BTC", fees)

    def test_small_fee_increment_and_net_quantity_stay_exact(self):
        fees = spot.fees_by_asset((fill(1, fee="1"), fill(2, fee="1E-29")))
        self.assertEqual(fees[0].amount, Decimal("1.00000000000000000000000000001"))
        net = spot.net_base_quantity(Decimal("1"), "BTC", (http.Fee("BTC", Decimal("1E-29")),))
        self.assertEqual(net, Decimal("0.99999999999999999999999999999"))

    def test_near_complete_fill_coverage_stays_incomplete(self):
        quantity = "0.99999999999999999999999999999"
        result = spot.read_fills(Feed(*[ok([]) for _ in range(5)]), BASE, terminal("1"), (fill(quantity=quantity),), sleep=lambda _delay: None)
        self.assertEqual((result.value.complete, result.value.covered_quantity), (False, Decimal(quantity)))

    def test_large_quantity_uses_adapter_refusal(self):
        with self.assertRaises(http.AdapterRefusal): spot.build_market_order(market(), "BUY", CLIENT, SHA, quantity=Decimal("1E+100"))

    def test_filter_text_beyond_exact_domain_is_unclear(self):
        filters = [{"filterType": "LOT_SIZE", "stepSize": "0." + "1" * 61, "minQty": "0.01"},
                   {"filterType": "MIN_NOTIONAL", "minNotional": "10"}]
        info = {"symbols": [{"symbol": SYMBOL, "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT", "filters": filters}]}
        feed = Feed(ok(info), ok({"symbol": SYMBOL, "bidPrice": "100", "askPrice": "101"}))
        result = spot.read_market(feed, BASE, SYMBOL, sleep=lambda _delay: None)
        self.assertEqual((result.kind, len(feed.calls)), ("unclear", 1))


if __name__ == "__main__":
    unittest.main()
