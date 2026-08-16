from __future__ import annotations

"""Deterministic tests for public Binance time probes."""

import io
import socket
import unittest
from unittest import mock
import urllib.error

from ui import network_probe_binance as probe


class BinanceProbeTests(unittest.TestCase):
    def measured_probe(self, opener, now_ms=1_000_000,
                       elapsed_s=0.2, edge_id="spot_rest"):
        with mock.patch.object(
                probe.time, "monotonic",
                side_effect=(10.0, 10.0 + elapsed_s)):
            return probe.probe_time(
                edge_id, opener=opener, timeout_s=2.5, now_ms=now_ms)

    def test_venues_match_the_frozen_contract(self):
        self.assertEqual(probe.VENUES, {
            "spot_rest": ("https://api.binance.com/api/v3/time", 6000),
            "usdm_rest": ("https://fapi.binance.com/fapi/v1/time", 2400),
        })

    def test_200_reads_weight_and_makes_one_request(self):
        calls = []

        def opener(url, timeout_s):
            calls.append((url, timeout_s))
            return 200, {"X-MBX-USED-WEIGHT-1M": "27"}, (
                b'{"serverTime":1000100}')

        result = self.measured_probe(opener)

        self.assertEqual(calls, [
            ("https://api.binance.com/api/v3/time", 2.5),
        ])
        self.assertTrue(result["ok"])
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["rtt_ms"], 200)
        self.assertEqual(result["used_weight_1m"], 27)
        self.assertEqual(result["server_time_ms"], 1_000_100)

    def test_429_reads_retry_after_without_retrying(self):
        calls = []

        def opener(url, timeout_s):
            calls.append((url, timeout_s))
            return 429, {"Retry-After": "7"}, b'{"code":-1003}'

        result = self.measured_probe(opener)

        self.assertEqual(len(calls), 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["retry_after_s"], 7)
        self.assertEqual(probe.next_allowed_ms(result, 50_000), 57_000)

    def test_418_is_visible_without_retrying(self):
        calls = []

        def opener(url, timeout_s):
            calls.append((url, timeout_s))
            return 418, {}, b""

        result = self.measured_probe(opener, edge_id="usdm_rest")

        self.assertEqual(len(calls), 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 418)
        self.assertEqual(result["error"], "HTTP 418")

    def test_connection_error_returns_bounded_evidence(self):
        def opener(url, timeout_s):
            raise socket.timeout("timed out")

        result = probe.probe_time(
            "spot_rest", opener=opener, now_ms=1_000_000)

        self.assertFalse(result["ok"])
        self.assertIsNone(result["http_status"])
        self.assertIsNone(result["rtt_ms"])
        self.assertEqual(result["error"], "timed out")

    def test_malformed_json_is_not_accepted(self):
        result = self.measured_probe(
            lambda url, timeout_s: (200, {}, b"not-json"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["error"], "Malformed JSON response")
        self.assertIsNone(result["server_time_ms"])

    def test_clock_offset_uses_response_midpoint(self):
        result = self.measured_probe(
            lambda url, timeout_s: (
                200, {}, b'{"serverTime":2000200}'),
            now_ms=lambda: 2_000_000,
            elapsed_s=0.1,
        )

        self.assertEqual(result["rtt_ms"], 100)
        self.assertEqual(result["clock_offset_ms"], 150)

    def test_headers_are_case_insensitive(self):
        result = self.measured_probe(
            lambda url, timeout_s: (
                429,
                {"x-mbx-used-weight-1m": "81", "rEtRy-AfTeR": "3"},
                b"",
            ))

        self.assertEqual(result["used_weight_1m"], 81)
        self.assertEqual(result["retry_after_s"], 3)

    def test_default_opener_sets_user_agent_and_preserves_http_error(self):
        error = urllib.error.HTTPError(
            probe.VENUES["spot_rest"][0],
            429,
            "rate limited",
            {"Retry-After": "11"},
            io.BytesIO(b"{}"),
        )
        with mock.patch.object(
                probe.urllib.request, "urlopen", side_effect=error) as opened:
            result = self.measured_probe(None)

        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), probe.USER_AGENT)
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["retry_after_s"], 11)
        self.assertFalse(result["ok"])

    def test_r1_binance_response_body_is_bounded_and_http_error_closes(self):
        limit = 65_536
        oversized = self.measured_probe(
            lambda _url, _timeout: (200, {}, b" " * (limit + 1))
        )
        self.assertFalse(oversized["ok"])
        self.assertEqual(oversized["error"], "response too large")

        class RecordingBody(io.BytesIO):
            def __init__(self, value):
                super().__init__(value)
                self.read_sizes = []
                self.was_closed = False

            def read(self, size=-1):
                self.read_sizes.append(size)
                return super().read(size)

            def close(self):
                self.was_closed = True
                super().close()

        stream = RecordingBody(b" " * (limit + 2))
        error = urllib.error.HTTPError(
            probe.VENUES["spot_rest"][0],
            429,
            "rate limited",
            {"Retry-After": "11"},
            stream,
        )
        with mock.patch.object(
                probe.urllib.request, "urlopen", side_effect=error):
            bounded_error = self.measured_probe(None)

        self.assertEqual(bounded_error["error"], "response too large")
        self.assertEqual(stream.read_sizes, [limit + 1])
        self.assertTrue(stream.was_closed)

    def test_no_retry_after_allows_the_current_time(self):
        self.assertEqual(
            probe.next_allowed_ms({"retry_after_s": None}, 9_000),
            9_000,
        )


if __name__ == "__main__":
    unittest.main()
