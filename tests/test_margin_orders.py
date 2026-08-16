"""Deterministic offline tests for cross-Margin order shaping and parsing."""

import sys
import unittest
from decimal import Decimal
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import margin_orders  # noqa: E402


SYMBOL = "BTCUSDT"
CLIENT_ID = "rmo-reverse-sell-1"


def build(side="SELL", quantity=Decimal("0.0204"), **changes):
    values = {
        "symbol": SYMBOL,
        "side": side,
        "quantity": quantity,
        "client_id": CLIENT_ID,
        "step": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "ref_price": Decimal("300"),
    }
    values.update(changes)
    return margin_orders.build_margin_order(**values)


def fill_payload(**changes):
    payload = {
        "symbol": SYMBOL,
        "clientOrderId": CLIENT_ID,
        "status": "FILLED",
        "executedQty": "0.020",
        "cummulativeQuoteQty": "6.000",
        "orderId": 481516,
        "fills": [{
            "price": "300",
            "qty": "0.020",
            "commission": "0.000020",
            "commissionAsset": "BTC",
        }],
    }
    payload.update(changes)
    return payload


class MarginOrderBuildTests(unittest.TestCase):
    def test_sell_shape_floors_quantity_and_uses_no_quote_quantity(self):
        ready = build(quantity=Decimal("0.0209"))
        self.assertIsInstance(ready, margin_orders.MarginOrderReady)
        self.assertEqual(ready.quantity, Decimal("0.020"))
        self.assertEqual(ready.params, {
            "symbol": SYMBOL,
            "side": "SELL",
            "type": "MARKET",
            "quantity": "0.020",
            "newClientOrderId": CLIENT_ID,
            "newOrderRespType": "FULL",
            "sideEffectType": "NO_SIDE_EFFECT",
            "isIsolated": "FALSE",
        })
        self.assertNotIn("quoteOrderQty", ready.params)

    def test_buy_uses_the_caller_quantity_after_step_flooring(self):
        ready = build(
            side="BUY", quantity=Decimal("0.0219"),
            client_id="rmo-reverse-buy-1",
        )
        self.assertIsInstance(ready, margin_orders.MarginOrderReady)
        self.assertEqual(ready.quantity, Decimal("0.021"))
        self.assertEqual(ready.params["quantity"], "0.021")
        self.assertNotIn("quoteOrderQty", ready.params)

    def test_min_notional_uses_floored_quantity_and_reference_price(self):
        refused = build(
            quantity=Decimal("0.0209"), ref_price=Decimal("249.99"),
        )
        self.assertIsInstance(refused, margin_orders.MarginOrderRefused)
        self.assertEqual(refused.reason, "MIN_NOTIONAL_NOT_MET")
        ready = build(
            quantity=Decimal("0.0209"), ref_price=Decimal("250"),
        )
        self.assertIsInstance(ready, margin_orders.MarginOrderReady)

    def test_bad_symbol_side_and_quantity_refuse(self):
        cases = (
            ({"symbol": "btcusdt"}, "BAD_SYMBOL"),
            ({"symbol": None}, "BAD_SYMBOL"),
            ({"side": "HOLD"}, "BAD_SIDE"),
            ({"side": None}, "BAD_SIDE"),
            ({"quantity": None}, "BAD_QUANTITY"),
            ({"quantity": Decimal("0")}, "BAD_QUANTITY"),
            ({"quantity": Decimal("NaN")}, "BAD_QUANTITY"),
            ({"quantity": "0.02"}, "BAD_QUANTITY"),
        )
        for changes, reason in cases:
            with self.subTest(changes=changes):
                refused = build(**changes)
                self.assertIsInstance(
                    refused, margin_orders.MarginOrderRefused,
                )
                self.assertEqual(refused.reason, reason)

    def test_unknown_filter_or_identity_input_refuses(self):
        cases = (
            ({"step": None}, "BAD_STEP"),
            ({"min_notional": None}, "BAD_MIN_NOTIONAL"),
            ({"ref_price": None}, "BAD_REF_PRICE"),
            ({"client_id": "other-1"}, "BAD_CLIENT_ID"),
            ({"quantity": Decimal("0.0009")}, "QUANTITY_BELOW_STEP"),
        )
        for changes, reason in cases:
            with self.subTest(changes=changes):
                refused = build(**changes)
                self.assertIsInstance(
                    refused, margin_orders.MarginOrderRefused,
                )
                self.assertEqual(refused.reason, reason)


class MarginOrderParseTests(unittest.TestCase):
    def setUp(self):
        self.expected = build()
        self.assertIsInstance(self.expected, margin_orders.MarginOrderReady)

    def test_full_fill_parses_exact_decimal_and_identity_fields(self):
        result = margin_orders.parse_margin_order_response(
            fill_payload(), self.expected,
        )
        self.assertEqual(result, margin_orders.MarginFill(
            "FILLED",
            Decimal("0.020"),
            Decimal("6.000"),
            [(Decimal("300"), Decimal("0.020"),
              Decimal("0.000020"), "BTC")],
            481516,
            CLIENT_ID,
        ))

    def test_working_states_are_typed_non_terminal_results(self):
        for status in ("NEW", "PARTIALLY_FILLED"):
            with self.subTest(status=status):
                result = margin_orders.parse_margin_order_response(
                    fill_payload(status=status), self.expected,
                )
                self.assertIsInstance(
                    result, margin_orders.MarginOrderNonTerminal,
                )
                self.assertEqual((result.status, result.reason),
                                 (status, "NON_TERMINAL"))

    def test_each_identity_mismatch_is_typed(self):
        cases = (
            ({"symbol": "ETHUSDT"}, ("symbol",)),
            ({"clientOrderId": "rmo-other-order"}, ("clientOrderId",)),
            ({"symbol": "ETHUSDT", "clientOrderId": "rmo-other-order"},
             ("symbol", "clientOrderId")),
        )
        for changes, fields in cases:
            with self.subTest(fields=fields):
                result = margin_orders.parse_margin_order_response(
                    fill_payload(**changes), self.expected,
                )
                self.assertIsInstance(
                    result, margin_orders.MarginOrderIdentityMismatch,
                )
                self.assertEqual(result.fields, fields)

    def test_unknown_fill_data_is_a_typed_parse_result(self):
        payloads = (
            fill_payload(executedQty=None),
            fill_payload(fills=None),
            fill_payload(fills=[]),
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                result = margin_orders.parse_margin_order_response(
                    payload, self.expected,
                )
                self.assertIsInstance(
                    result, margin_orders.MarginOrderParseError,
                )
                self.assertEqual(result.status, "MALFORMED")


class MarginOrderQueryTests(unittest.TestCase):
    def test_terminal_query_without_fills_keeps_measured_row_truth(self):
        payload = fill_payload()
        payload.pop("fills")

        def transport(_method, _base_url, _path, _params=None, _signed=False):
            return "ok", payload

        result = margin_orders.query_margin_order(
            transport, SYMBOL, CLIENT_ID,
        )
        self.assertIsInstance(result, margin_orders.MarginFill)
        self.assertEqual(
            (result.status, result.executed_qty, result.cum_quote,
             result.fills),
            ("FILLED", Decimal("0.020"), Decimal("6.000"), None),
        )

    def test_not_found_is_typed_and_query_is_exactly_one_signed_get(self):
        calls = []

        def transport(method, base_url, path, params=None, signed=False):
            calls.append((method, base_url, path, dict(params or {}), signed))
            return "api_error", {"code": -2013, "msg": "Order absent"}

        result = margin_orders.query_margin_order(
            transport, SYMBOL, CLIENT_ID,
        )
        self.assertIsInstance(result, margin_orders.MarginOrderNotFound)
        self.assertEqual(result.status, "NOT_FOUND")
        self.assertEqual(calls, [(
            "GET",
            margin_orders.MARGIN_BASE_URL,
            margin_orders.MARGIN_ORDER_QUERY_PATH,
            {"symbol": SYMBOL, "origClientOrderId": CLIENT_ID,
             "isIsolated": "FALSE"},
            True,
        )])

    def test_query_response_still_requires_both_identities(self):
        def transport(_method, _base_url, _path, _params=None, _signed=False):
            return "ok", fill_payload(symbol="ETHUSDT")

        result = margin_orders.query_margin_order(
            transport, SYMBOL, CLIENT_ID,
        )
        self.assertIsInstance(
            result, margin_orders.MarginOrderIdentityMismatch,
        )
        self.assertEqual(result.fields, ("symbol",))


class MarginOrderFeeTests(unittest.TestCase):
    def test_only_base_asset_fees_reduce_net_base(self):
        for fee_asset, expected in (
            ("BTC", Decimal("0.019")),
            ("USDT", Decimal("0.020")),
            ("BNB", Decimal("0.020")),
        ):
            with self.subTest(fee_asset=fee_asset):
                fill = margin_orders.MarginFill(
                    "FILLED", Decimal("0.020"), Decimal("6"),
                    [(Decimal("300"), Decimal("0.020"), Decimal("0.001"),
                      fee_asset)],
                    481516, CLIENT_ID,
                )
                self.assertEqual(
                    margin_orders.net_base_after_fees(fill, "BTC"), expected,
                )

    def test_missing_query_fills_returns_typed_fee_unknown(self):
        fill = margin_orders.MarginFill(
            "FILLED", Decimal("0.020"), Decimal("6"), None,
            481516, CLIENT_ID,
        )
        result = margin_orders.net_base_after_fees(fill, "BTC")
        self.assertIsInstance(result, margin_orders.MarginFeeUnknown)
        self.assertEqual(
            (result.status, result.reason, result.fields),
            ("UNKNOWN", "FILL_DATA_UNKNOWN", ("fills",)),
        )


if __name__ == "__main__":
    unittest.main()
