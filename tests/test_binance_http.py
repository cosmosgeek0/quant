"""Focused, network-free tests for the shared Binance HTTP transport."""
from __future__ import annotations

import hashlib, hmac, io, inspect, json, sys, unittest
import urllib.error, urllib.parse, urllib.request, urllib.response
from decimal import Decimal, Inexact
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from execution import binance_http as bh


class _Response:
    def __init__(self, body, status=200):
        self.body = body.encode() if isinstance(body, str) else body
        self.status = status

    def __enter__(self): return self

    def __exit__(self, *_args): return False

    def read(self): return self.body


def _http_error(status, body):
    return urllib.error.HTTPError(
        "https://hidden.invalid/private?signature=hidden", status, "hidden",
        None, io.BytesIO(body.encode()))


class BinanceHttpTests(unittest.TestCase):
    def test_exact_context_traps_inexact_arithmetic(self):
        with self.assertRaises(Inexact):
            with bh.exact_context() as context:
                self.assertEqual(context.prec, 60)
                Decimal(1) / Decimal(3)

    def test_exact_guard_maps_decimal_exception_to_refusal(self):
        with self.assertRaises(bh.AdapterRefusal):
            with bh.exact_guard():
                Decimal(1) / Decimal(3)

    def test_constructor_refuses_missing_credentials_and_invalid_bounds(self):
        invalid = (("", "secret", 15, 5000), ("key", " ", 15, 5000),
                   ("key", "secret", 0, 5000), ("key", "secret", True, 5000),
                   ("key", "secret", 15, 0), ("key", "secret", 15, 60001))
        for key, secret, timeout, window in invalid:
            with self.subTest(timeout=timeout, window=window):
                with self.assertRaises(bh.AdapterRefusal):
                    bh.BinanceHttp(key, secret, timeout_seconds=timeout,
                                   recv_window_ms=window)

    def test_call_has_one_wire_attempt_redirect_and_signed_unsynced_refuses(self):
        client = bh.BinanceHttp("key", "secret", now_ms=lambda: 1000)
        with mock.patch.object(client, "_open",
                               side_effect=TimeoutError("private URL")) as wire:
            result = client("POST", "https://spot.test", "/order")
            self.assertEqual(result.kind, "unclear")
            wire.assert_called_once()
        redirect = urllib.response.addinfourl(io.BytesIO(), {"location": "http://b.test/y"}, "http://a.test/x", 302)
        redirect.msg = "Found"
        with mock.patch.object(urllib.request.HTTPHandler, "http_open", return_value=redirect) as wire:
            result = client("GET", "http://a.test", "/x")
        self.assertEqual((result.kind, result.code), ("unclear", 302))
        wire.assert_called_once()
        with mock.patch.object(client, "_open") as wire:
            result = client("POST", "https://spot.test", "/order", signed=True)
            self.assertEqual(result.msg, "clock_not_synchronized")
            wire.assert_not_called()

    def test_sync_time_is_one_attempt_and_isolated_by_exact_base(self):
        client = bh.BinanceHttp("key", "secret", now_ms=lambda: 1000)
        responses = [_Response('{"serverTime":1500}'),
                     _Response('{"serverTime":false}'), _Response("{}"),
                     _Response("{}")]
        with mock.patch.object(client, "_open",
                               side_effect=responses) as wire:
            self.assertEqual(client.sync_time("https://a.test", "/time").kind,
                             "ok")
            self.assertEqual(client.sync_time("https://b.test", "/time").kind,
                             "unclear")
            self.assertEqual(client.sync_time("https://a.test", "/time").kind,
                             "unclear")
            signed = client("GET", "https://a.test", "/account", signed=True)
            refused = client("GET", "https://a.test/", "/account", signed=True)
        self.assertEqual(signed.kind, "ok")
        self.assertEqual(refused.msg, "clock_not_synchronized")
        self.assertEqual(wire.call_count, 4)
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(wire.call_args_list[3].args[0].full_url).query)
        self.assertEqual(query["timestamp"], ["1500"])

    def test_sync_time_invalid_payload_never_enables_signing(self):
        payloads = ("{}", '{"serverTime":1.5}', '{"serverTime":0}',
                    '{"serverTime":-1}', "[]")
        for payload in payloads:
            with self.subTest(payload=payload):
                client = bh.BinanceHttp("key", "secret", now_ms=lambda: 1000)
                with mock.patch.object(client, "_open",
                                       return_value=_Response(payload)) as wire:
                    self.assertEqual(client.sync_time("https://a.test", "/time").kind,
                                     "unclear")
                    self.assertEqual(client("GET", "https://a.test", "/x",
                                            signed=True).msg,
                                     "clock_not_synchronized")
                    wire.assert_called_once()

    def test_signed_query_header_and_hmac_are_exact(self):
        clock = mock.Mock(return_value=1000)
        client = bh.BinanceHttp("api-key", "secret", now_ms=clock)
        with mock.patch.object(client, "_open",
                               side_effect=[_Response('{"serverTime":1005}'),
                                            _Response("{}")]) as wire:
            client.sync_time("https://a.test", "/time")
            result = client("POST", "https://a.test", "/order",
                            (("symbol", "BTC USDT"),), True)
        self.assertEqual(result.kind, "ok")
        request = wire.call_args_list[1].args[0]
        query = urllib.parse.urlsplit(request.full_url).query
        unsigned, signature = query.rsplit("&signature=", 1)
        expected = hmac.new(b"secret", unsigned.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)
        self.assertEqual(unsigned,
                         "symbol=BTC+USDT&timestamp=1005&recvWindow=5000")
        self.assertEqual(request.get_header("X-mbx-apikey"), "api-key")
        self.assertEqual(wire.call_args_list[1].kwargs["timeout"], 15)
        self.assertEqual(clock.call_count, 2)

    def test_noncanonical_request_parts_are_refused_before_wire(self):
        client = bh.BinanceHttp("key", "secret")
        cases = ((("signature", "x"),), (("timestamp", "1"),),
                 (("recvWindow", "1"),), (("x", "1"), ("x", "2")),
                 ((1, "x"),), (("x", 1),))
        with mock.patch.object(client, "_open") as wire:
            for params in cases:
                with self.subTest(params=params), self.assertRaises(bh.AdapterRefusal):
                    client("GET", "https://a.test", "/x", params)
            for path in ("/x?pre=1", "/x#part"):
                with self.subTest(path=path), self.assertRaises(bh.AdapterRefusal):
                    client("GET", "https://a.test", path, signed=True)
        wire.assert_not_called()

    def test_read_retries_only_unclear_with_fixed_backoff(self):
        outcomes = iter((bh.HttpResult("unclear", None, None, "u1"),
                         bh.HttpResult("unclear", None, None, "u2"),
                         bh.HttpResult("ok", {"v": 1}, None, None)))
        calls, sleeps = [], []

        def transport(*args):
            calls.append(args)
            return next(outcomes)

        result = bh.read(transport, "https://a.test", "/x",
                         (("p", "1"),), signed=True, sleep=sleeps.append)
        self.assertEqual(result.kind, "ok")
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual([call[0] for call in calls], ["GET"] * 3)
        self.assertTrue(all(call[4] for call in calls))
        client = bh.BinanceHttp("key", "secret", timeout_seconds=7)
        with mock.patch.object(client, "_open",
                               side_effect=TimeoutError) as wire:
            bh.read(client, "https://a.test", "/x", sleep=lambda _delay: None)
        self.assertEqual([call.kwargs["timeout"] for call in wire.call_args_list],
                         [7, 7, 7])

    def test_read_stops_on_api_error_and_has_no_method_argument(self):
        answer = bh.HttpResult("api_error", None, -2010, "refused")
        transport = mock.Mock(return_value=answer)
        sleeps = mock.Mock()
        self.assertIs(bh.read(transport, "base", "/x", sleep=sleeps), answer)
        transport.assert_called_once_with("GET", "base", "/x", None, False)
        sleeps.assert_not_called()
        self.assertNotIn("method", inspect.signature(bh.read).parameters)
        with self.assertRaises(TypeError):
            bh.read(transport, "base", "/x", method="POST")

    def test_success_json_uses_decimal_int_and_rejects_nonfinite(self):
        client = bh.BinanceHttp("key", "secret")
        body = '{"price":1.234567890123456789,"count":7}'
        with mock.patch.object(client, "_open",
                               return_value=_Response(body)):
            result = client("GET", "https://a.test", "/x")
        self.assertEqual(result.payload["price"], bh.Decimal("1.234567890123456789"))
        self.assertIs(type(result.payload["price"]), bh.Decimal)
        self.assertIs(type(result.payload["count"]), int)
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant), mock.patch.object(
                    client, "_open",
                    return_value=_Response('{"price":%s}' % constant)):
                self.assertEqual(client("GET", "https://a.test", "/x").kind,
                                 "unclear")

    def test_api_error_has_clean_bounded_code_and_message(self):
        client = bh.BinanceHttp("visible-key", "visible-secret")
        unsafe = ("declined //hidden.invalid/private X-MBX-APIKEY visible-key "
                  "signature=visible-secret")
        with mock.patch.object(client, "_open",
                               side_effect=_http_error(
                                   400, json.dumps({"code": -2010, "msg": unsafe}))):
            result = client("POST", "https://a.test", "/order")
        self.assertEqual((result.kind, result.code), ("api_error", -2010))
        self.assertIsNone(result.payload)
        self.assertLessEqual(len(result.msg), 160)
        self.assertNotIn("//", result.msg)
        rendered = repr(result).lower()
        for private in ("hidden.invalid", "visible-key", "visible-secret",
                        "x-mbx-apikey", "signature"):
            self.assertNotIn(private, rendered)
        with mock.patch.object(client, "_open", side_effect=
                               _http_error(400, '{"code":-2010,"msg":"%s"}' %
                                           ("x" * 200))):
            bounded = client("POST", "https://a.test", "/order")
        self.assertEqual(len(bounded.msg), 160)

    def test_ambiguous_codes_and_uncertain_http_statuses_are_unclear(self):
        client = bh.BinanceHttp("key", "secret")
        cases = [(400, '{"code":-1006,"msg":"uncertain"}', -1006),
                 (400, '{"code":-1007,"msg":"uncertain"}', -1007),
                 (408, '{"code":-2010,"msg":"late"}', None),
                 (409, '{"code":-2010,"msg":"conflict"}', None),
                 (500, '{"code":-2010,"msg":"service"}', None)]
        for status, body, code in cases:
            with self.subTest(status=status, code=code), mock.patch.object(
                    client, "_open",
                    side_effect=_http_error(status, body)):
                result = client("POST", "https://a.test", "/x")
                self.assertEqual((result.kind, result.code), ("unclear", code))

    def test_invalid_json_missing_identity_and_exception_are_unclear_clean(self):
        client = bh.BinanceHttp("key", "secret")
        effects = [_Response("not-json"), _http_error(400, '{"msg":"no code"}'),
                   _http_error(400, '{"code":-1006,"code":-2010,"msg":"refused"}'),
                   RuntimeError("https://private signature=secret key")]
        for effect in effects:
            patcher = mock.patch.object(client, "_open", return_value=effect) if isinstance(effect, _Response) else mock.patch.object(client, "_open", side_effect=effect)
            with self.subTest(effect=type(effect).__name__), patcher:
                result = client("GET", "https://a.test", "/x")
                self.assertEqual(result.kind, "unclear")
                self.assertIsNone(result.payload)
                self.assertNotIn("private", repr(result).lower())
                self.assertNotIn("signature", repr(result).lower())


if __name__ == "__main__":
    unittest.main()
