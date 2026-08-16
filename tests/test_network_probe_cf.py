from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from ui.network_probe_cf import (
    cf_metrics_from_raw,
    discover_metrics_url,
    parse_prometheus_text,
    probe_origin,
    read_ready,
    scrape_metrics,
)


FIXTURE = Path(__file__).parent / "fixtures" / "network" / "cloudflared_metrics_sample.txt"


class PrometheusParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = FIXTURE.read_text(encoding="utf-8")
        cls.raw = parse_prometheus_text(cls.text)

    def test_fixture_metrics_are_parsed(self):
        self.assertEqual(4.0, self.raw["cloudflared_tunnel_ha_connections"])
        self.assertEqual(423875.0, self.raw["cloudflared_tunnel_total_requests"])
        self.assertEqual(11085.0, self.raw["cloudflared_tunnel_request_errors"])
        self.assertEqual(4, len(self.raw["quic_client_latest_rtt"]))
        self.assertEqual(335.0, sum(self.raw["quic_client_lost_packets"].values()))

    def test_r1_cf_label_test_uses_canonical_pairs(self):
        latest = self.raw["quic_client_latest_rtt"]
        lost = self.raw["quic_client_lost_packets"]
        self.assertEqual(1.0, latest['conn_index="0"'])
        self.assertEqual(5.0, lost['conn_index="0",reason="timeout"'])
        reordered = parse_prometheus_text(
            'metric{reason="time,\\"out\\"",conn_index="0"} 5\n'
        )
        self.assertEqual(
            {'conn_index="0",reason="time,\\"out\\""': 5.0},
            reordered["metric"],
        )

    def test_comments_blank_lines_and_invalid_samples_are_ignored(self):
        parsed = parse_prometheus_text("# note\n\nvalid 2\nnot a sample\nbad nope\n")
        self.assertEqual({"valid": 2.0}, parsed)

    def test_r5_probe_cf_157_non_finite_samples_are_absent(self):
        parsed = parse_prometheus_text(
            'quic_client_latest_rtt{conn_index="0"} NaN\n'
            "cloudflared_tunnel_ha_connections +Inf\n"
        )

        result = cf_metrics_from_raw(parsed, None)

        self.assertEqual({}, parsed)
        self.assertIsNone(result["cf_connector"]["latest_rtt_ms_max"])
        self.assertIsNone(result["cf_connector"]["ha_connections"])
        json.dumps(result, allow_nan=False)


class DiscoveryAndScrapeTests(unittest.TestCase):
    def test_discovery_uses_configured_then_ports_and_stops_at_first(self):
        configured = "http://127.0.0.1:20244/metrics"
        calls = []

        def opener(url, timeout):
            calls.append((url, timeout))
            if url.endswith(":20242/metrics"):
                return 200, {}, b"cloudflared_tunnel_ha_connections 4\n"
            return 200, {}, b"another_metric 1\n"

        found = discover_metrics_url(configured, opener=opener)

        self.assertEqual("http://127.0.0.1:20242/metrics", found)
        self.assertEqual(
            [configured, "http://127.0.0.1:20241/metrics", "http://127.0.0.1:20242/metrics"],
            [url for url, _timeout in calls],
        )
        self.assertEqual([3.0, 3.0, 3.0], [timeout for _url, timeout in calls])

    def test_discovery_never_uses_an_unlisted_port(self):
        calls = []

        def opener(url, timeout):
            calls.append(url)
            raise OSError("not available")

        self.assertIsNone(discover_metrics_url("http://127.0.0.1:20246/metrics", opener))
        self.assertEqual(
            ["http://127.0.0.1:{}/metrics".format(port) for port in range(20241, 20246)],
            calls,
        )

    def test_scrape_metrics_parses_a_successful_response(self):
        body = FIXTURE.read_bytes()
        result = scrape_metrics("http://127.0.0.1:20241/metrics", lambda _url, _timeout: (200, {}, body))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])
        self.assertEqual(4.0, result["raw_values"]["cloudflared_tunnel_ha_connections"])

    def test_scrape_metrics_returns_an_error_without_raising(self):
        def opener(_url, _timeout):
            raise OSError("metrics unavailable")

        result = scrape_metrics("http://127.0.0.1:20241/metrics", opener)
        self.assertEqual(
            {"ok": False, "raw_values": {}, "error": "metrics unavailable"},
            result,
        )

    def test_r1_cf_response_bodies_use_endpoint_caps(self):
        metrics_limit = 1024 * 1024
        ready_limit = 8 * 1024
        origin_limit = 2 * 1024 * 1024
        configured = "http://127.0.0.1:20244/metrics"

        def discovery_opener(url, _timeout):
            if url == configured:
                return 200, {}, b"cloudflared_metric 1\n" + b" " * metrics_limit
            if url.endswith(":20241/metrics"):
                return 200, {}, b"cloudflared_metric 1\n"
            return 404, {}, b""

        self.assertEqual(
            "http://127.0.0.1:20241/metrics",
            discover_metrics_url(configured, discovery_opener),
        )

        with mock.patch("ui.network_probe_cf.parse_prometheus_text") as parser:
            metrics = scrape_metrics(
                "http://127.0.0.1:20241/metrics",
                lambda _url, _timeout: (200, {}, b"m 1\n" + b" " * metrics_limit),
            )
        parser.assert_not_called()
        self.assertEqual(
            {"ok": False, "raw_values": {}, "error": "response too large"},
            metrics,
        )

        with mock.patch("ui.network_probe_cf.json.loads") as loads:
            ready = read_ready(
                "http://127.0.0.1:20241",
                lambda _url, _timeout: (200, {}, b"{" + b" " * ready_limit),
            )
        loads.assert_not_called()
        self.assertFalse(ready["ok"])
        self.assertEqual(ready["error"], "response too large")

        with mock.patch(
                "ui.network_probe_cf.time.monotonic",
                side_effect=[10.0, 10.01]):
            origin = probe_origin(
                opener=lambda _url, _timeout: (
                    200, {}, b" " * (origin_limit + 1)
                )
            )
        self.assertFalse(origin["ok"])
        self.assertEqual(origin["error"], "response too large")
        self.assertIsNone(origin["size_bytes"])


class ReadyAndOriginTests(unittest.TestCase):
    def test_ready_keeps_only_the_contract_fields(self):
        payload = json.dumps(
            {"status": 200, "readyConnections": 4, "connectorId": "private-id"}
        ).encode("utf-8")
        calls = []

        def opener(url, timeout):
            calls.append((url, timeout))
            return 200, {}, payload

        result = read_ready("http://127.0.0.1:20241/", opener)

        self.assertEqual({"ok": True, "status": 200, "ready_connections": 4}, result)
        self.assertNotIn("connectorId", result)
        self.assertEqual([("http://127.0.0.1:20241/ready", 3.0)], calls)

    def test_ready_failure_is_fail_closed(self):
        result = read_ready(
            "http://127.0.0.1:20241",
            lambda _url, _timeout: (_ for _ in ()).throw(OSError("not ready")),
        )
        self.assertEqual({"ok": False, "status": None, "ready_connections": None}, result)

    def test_r1_cf_negative_ready_connections_is_unknown(self):
        payload = json.dumps(
            {"status": 200, "readyConnections": -1}
        ).encode("utf-8")

        result = read_ready(
            "http://127.0.0.1:20241",
            lambda _url, _timeout: (200, {}, payload),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["ready_connections"])

    def test_origin_probe_records_a_success(self):
        opener = lambda _url, _timeout: (200, {}, b"hello")
        with mock.patch("ui.network_probe_cf.time.monotonic", side_effect=[10.0, 10.025]):
            result = probe_origin(opener=opener)
        self.assertEqual(
            {"ok": True, "http_status": 200, "rtt_ms": 25, "size_bytes": 5, "error": None},
            result,
        )

    def test_origin_probe_records_http_failure(self):
        opener = lambda _url, _timeout: (503, {}, b"later")
        with mock.patch("ui.network_probe_cf.time.monotonic", side_effect=[2.0, 2.004]):
            result = probe_origin(opener=opener)
        self.assertEqual(
            {
                "ok": False,
                "http_status": 503,
                "rtt_ms": 4,
                "size_bytes": 5,
                "error": "HTTP status 503",
            },
            result,
        )

    def test_origin_probe_catches_transport_failure(self):
        def opener(_url, _timeout):
            raise OSError("origin unavailable")

        with mock.patch("ui.network_probe_cf.time.monotonic", return_value=1.0):
            result = probe_origin(opener=opener)
        self.assertEqual(
            {
                "ok": False,
                "http_status": None,
                "rtt_ms": None,
                "size_bytes": None,
                "error": "origin unavailable",
            },
            result,
        )


class MetricNormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = parse_prometheus_text(FIXTURE.read_text(encoding="utf-8"))

    def previous(self):
        previous = dict(self.raw)
        previous["cloudflared_tunnel_total_requests"] = 423800.0
        previous["cloudflared_tunnel_request_errors"] = 11080.0
        previous["cloudflared_proxy_connect_streams_errors"] = 1.0
        previous["quic_client_lost_packets"] = {
            'conn_index="0",reason="reordering"': 1.0,
            'conn_index="0",reason="timeout"': 4.0,
            'conn_index="1",reason="reordering"': 285.0,
            'conn_index="1",reason="timeout"': 40.0,
        }
        previous["cloudflared_tunnel_response_by_code"] = {
            'status_code="200"': 399950.0,
            'status_code="304"': 11990.0,
            'status_code="502"': 11080.0,
        }
        return previous

    def test_metrics_and_counter_deltas_follow_contract_names(self):
        raw = dict(self.raw)
        raw.update(
            {"ready_connections": 4, "ready_status": 200, "metrics_url_port": 20241}
        )
        result = cf_metrics_from_raw(raw, self.previous())
        connector = result["cf_connector"]
        origin = result["connector_origin_delta"]

        self.assertEqual(
            {
                "ha_connections",
                "ready_connections",
                "ready_status",
                "total_requests_delta",
                "concurrent_requests",
                "latest_rtt_ms_max",
                "smoothed_rtt_ms_max",
                "min_rtt_ms_min",
                "lost_packets_delta",
                "connect_streams_errors_delta",
                "register_success_total",
                "metrics_url_port",
            },
            set(connector),
        )
        self.assertEqual(75.0, connector["total_requests_delta"])
        self.assertEqual(5.0, connector["lost_packets_delta"])
        self.assertEqual(2.0, connector["connect_streams_errors_delta"])
        self.assertEqual(1.0, connector["latest_rtt_ms_max"])
        self.assertEqual(2.0, connector["smoothed_rtt_ms_max"])
        self.assertEqual(1.0, connector["min_rtt_ms_min"])
        self.assertEqual(12.0, connector["register_success_total"])
        self.assertEqual({"request_errors_delta": 5.0, "response_5xx_delta": 5.0}, origin)

    def test_first_sample_deltas_are_unknown(self):
        result = cf_metrics_from_raw(self.raw, None)
        connector = result["cf_connector"]
        origin = result["connector_origin_delta"]
        self.assertIsNone(connector["total_requests_delta"])
        self.assertIsNone(connector["lost_packets_delta"])
        self.assertIsNone(connector["connect_streams_errors_delta"])
        self.assertEqual({"request_errors_delta": None, "response_5xx_delta": None}, origin)

    def test_r1_cf_label_order_preserves_counter_delta(self):
        previous = parse_prometheus_text(
            'quic_client_lost_packets{conn_index="0",reason="timeout"} 4\n'
        )
        current = parse_prometheus_text(
            'quic_client_lost_packets{reason="timeout",conn_index="0"} 5\n'
        )

        result = cf_metrics_from_raw(current, previous)

        self.assertEqual(
            1.0, result["cf_connector"]["lost_packets_delta"]
        )

    def test_backwards_counters_return_unknown_not_negative(self):
        previous = self.previous()
        previous["cloudflared_tunnel_total_requests"] = 500000.0
        previous["cloudflared_tunnel_request_errors"] = 12000.0
        previous["cloudflared_proxy_connect_streams_errors"] = 4.0
        previous["quic_client_lost_packets"] = {
            label: value + 1000.0
            for label, value in self.raw["quic_client_lost_packets"].items()
        }
        previous["cloudflared_tunnel_response_by_code"] = {
            'status_code="502"': 12000.0
        }

        result = cf_metrics_from_raw(self.raw, previous)
        connector = result["cf_connector"]
        origin = result["connector_origin_delta"]
        self.assertIsNone(connector["total_requests_delta"])
        self.assertIsNone(connector["lost_packets_delta"])
        self.assertIsNone(connector["connect_streams_errors_delta"])
        self.assertEqual({"request_errors_delta": None, "response_5xx_delta": None}, origin)


if __name__ == "__main__":
    unittest.main()
