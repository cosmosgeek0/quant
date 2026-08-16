#!/usr/bin/env python3
"""Offline characterization tests for the current Spot round-trip hand."""

import hashlib
import hmac
import importlib.util
import io
import os
import socket
import unittest
import urllib.parse
import urllib.request
from decimal import Decimal
from unittest import mock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(BASE_DIR, "src", "spot_roundtrip.py")
SPEC = importlib.util.spec_from_file_location("spot_roundtrip_under_test",
                                              MODULE_PATH)
spot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spot)


class FakeTransport:
    """Stand in for spot.http and retain each attempted request."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, method, path, params=None, signed=False):
        self.calls.append((method, path, dict(params or {}), signed))
        if not self.results:
            raise AssertionError("unexpected offline request: %s %s" %
                                 (method, path))
        return self.results.pop(0)

    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]


class SpotRoundtripCase(unittest.TestCase):
    """Reset module state and prevent unintended network access."""

    def setUp(self):
        values = {
            "BASE_URL": "https://spot.test",
            "API_KEY": "offline-key",
            "API_SECRET": "offline-secret",
            "SYMBOL": "BTCUSDT",
            "RECV_WINDOW": "5000",
            "receipt": {},
            "receipt_path": "",
            "POLL_SECONDS": 100,
            "POLL_SLEEP": 0,
        }
        for name, value in values.items():
            patcher = mock.patch.object(spot, name, value)
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

    def test_get_retry_retries_unclear_then_stops_on_ok(self):
        transport = FakeTransport([
            ("unclear", {"error": "TimeoutError"}),
            ("ok", {"serverTime": 1}),
            ("api_error", {"code": -1}),
        ])
        with mock.patch.object(spot, "http", transport), \
                mock.patch.object(spot.time, "sleep") as sleep:
            result = spot.get_retry("/api/v3/time", tries=3)
        self.assertEqual(result, ("ok", {"serverTime": 1}))
        self.assertEqual(len(transport.calls), 2)
        sleep.assert_called_once_with(1)

    def test_get_retry_stops_on_api_error_without_retry(self):
        transport = FakeTransport([
            ("api_error", {"code": -1002}),
            ("ok", {"unused": True}),
        ])
        with mock.patch.object(spot, "http", transport), \
                mock.patch.object(spot.time, "sleep") as sleep:
            result = spot.get_retry("/api/v3/account", signed=True)
        self.assertEqual(result, ("api_error", {"code": -1002}))
        self.assertEqual(len(transport.calls), 1)
        sleep.assert_not_called()

    def test_query_order_returns_terminal_and_resets_missing_count(self):
        missing = ("api_error", {"code": -2013})
        transport = FakeTransport(
            [missing] * 9 + [("ok", {"status": "NEW"}), missing,
                             ("ok", {"status": "FILLED", "orderId": 7})]
        )
        with mock.patch.object(spot, "http", transport), \
                mock.patch.object(spot.time, "sleep"):
            state, order = spot.query_order("exact-id")
        self.assertEqual(state, "terminal")
        self.assertEqual(order["orderId"], 7)
        self.assertEqual(len(transport.calls), 12)

    def test_query_order_returns_not_found_after_ten_consecutive_missing(self):
        missing = ("api_error", {"code": -2013})
        transport = FakeTransport([missing] * 10)
        with mock.patch.object(spot, "http", transport), \
                mock.patch.object(spot.time, "sleep"):
            result = spot.query_order("exact-id")
        self.assertEqual(result, ("not_found", None))
        self.assertEqual(len(transport.calls), 10)

    def test_query_order_returns_error_for_other_api_error(self):
        payload = {"code": -1003, "msg": "request limit"}
        transport = FakeTransport([("api_error", payload)])
        with mock.patch.object(spot, "http", transport):
            result = spot.query_order("exact-id")
        self.assertEqual(result, ("error", payload))
        self.assertEqual(spot.receipt["first_failure"]["where"],
                         "query_order")

    def test_query_order_returns_pending_with_last_observed_order(self):
        order = {"status": "NEW", "orderId": 8}
        transport = FakeTransport([("ok", order)])
        with mock.patch.object(spot, "http", transport), \
                mock.patch.object(spot, "POLL_SECONDS", 0.5), \
                mock.patch.object(spot.time, "monotonic",
                                  side_effect=[10, 10, 11]), \
                mock.patch.object(spot.time, "sleep"):
            result = spot.query_order("exact-id")
        self.assertEqual(result, ("pending", order))
        self.assertEqual(len(transport.calls), 1)

    def test_place_confirmed_posts_once_then_queries_exact_id(self):
        requested = "requested-id"
        returned = {"status": "FILLED", "clientOrderId": "different-id",
                    "orderId": 9}
        transport = FakeTransport([
            ("unclear", {"error": "TimeoutError"}),
            ("ok", returned),
        ])
        params = {"symbol": "BTCUSDT", "newClientOrderId": requested}
        with mock.patch.object(spot, "http", transport):
            order, fills = spot.place_confirmed("BUY", params, "10 USDT")
        self.assertEqual(len(transport.posts()), 1)
        self.assertEqual(transport.calls[1][2]["origClientOrderId"], requested)
        self.assertTrue(transport.calls[1][3])
        # Known gap the shared adapter closes: the current hand does not verify
        # the clientOrderId returned by the exact-id query.
        self.assertEqual(order["clientOrderId"], "different-id")
        self.assertEqual(fills, [])

    def test_order_trades_uses_full_fills_when_they_cover_execution(self):
        fills = [{"qty": "0.4"}, {"qty": "0.6"}]
        transport = FakeTransport([])
        with mock.patch.object(spot, "http", transport):
            trades, covered = spot.order_trades(11, Decimal("1.0"), fills)
        self.assertIs(trades, fills)
        self.assertEqual(covered, Decimal("1.0"))
        self.assertEqual(transport.calls, [])

    def test_order_trades_reads_my_trades_until_execution_is_covered(self):
        rounds = [
            [{"qty": "0.2"}, {"qty": "0.2"}],
            [{"qty": "0.2"}, {"qty": "0.3"}],
        ]
        transport = FakeTransport([("ok", rounds[0]), ("ok", rounds[1])])
        with mock.patch.object(spot, "http", transport), \
                mock.patch.object(spot.time, "sleep"):
            trades, covered = spot.order_trades(
                12, Decimal("0.5"), [{"qty": "0.1"}]
            )
        self.assertEqual(trades, rounds[1])
        self.assertEqual(covered, Decimal("0.5"))
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(all(call[1] == "/api/v3/myTrades" and call[3]
                            for call in transport.calls))

    def test_fees_by_asset_aggregates_decimal_text_per_asset(self):
        fees = spot.fees_by_asset(
            [{"commissionAsset": "BTC", "commission": "0.0001"},
             {"commissionAsset": "BNB", "commission": "0.01"}],
            [{"commissionAsset": "BTC", "commission": "0.0002"}],
        )
        self.assertEqual(fees, {"BTC": "0.0003", "BNB": "0.01"})
        self.assertTrue(all(isinstance(value, str) for value in fees.values()))

    def test_signed_http_carries_clock_window_signature_and_api_key(self):
        captured = {}

        def respond(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return io.BytesIO(b'{"status":"ok"}')

        with mock.patch.object(spot, "now_ms", return_value=1700000000123), \
                mock.patch.object(urllib.request, "urlopen",
                                  side_effect=respond):
            result = spot.http("GET", "/api/v3/order",
                               {"symbol": "BTCUSDT"}, signed=True)
        self.assertEqual(result, ("ok", {"status": "ok"}))
        raw_query = urllib.parse.urlsplit(captured["request"].full_url).query
        unsigned, signature = raw_query.rsplit("&signature=", 1)
        fields = urllib.parse.parse_qs(unsigned)
        expected = hmac.new(b"offline-secret", unsigned.encode(),
                            hashlib.sha256).hexdigest()
        headers = {key.lower(): value for key, value
                   in captured["request"].header_items()}
        self.assertEqual(fields["timestamp"], ["1700000000123"])
        self.assertEqual(fields["recvWindow"], ["5000"])
        self.assertEqual(signature, expected)
        self.assertEqual(headers["x-mbx-apikey"], "offline-key")
        self.assertEqual(captured["timeout"], spot.HTTP_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
