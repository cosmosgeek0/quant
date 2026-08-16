from __future__ import annotations

"""Deterministic tests for the frozen Network V1 health rules."""

import copy
import json
import unittest

from ui import network_health as health


NOW = 1_800_000_000_000
CONFIG = {
    "rest_probe_s": 15,
    "wss_read_s": 15,
    "cf_monitor_s": 15,
    "news_monitor_s": 120,
    "paused": {"rest": False, "wss": False, "cf": False, "news": False},
    "receipt_id": 17,
}


def rest(status=200, server_time=NOW, **changes):
    row = {
        "ok": status == 200,
        "http_status": status,
        "rtt_ms": 24,
        "clock_offset_ms": 3,
        "used_weight_1m": 10,
        "retry_after_s": None,
        "error": None,
        "server_time_ms": server_time,
    }
    row.update(changes)
    return row


def wss(**changes):
    row = {
        "expected_connections": 4,
        "connected_count": 4,
        "successful_handshakes": 4,
        "handshake_failures": 0,
        "reconnects": 0,
        "stream_count": 600,
        "last_message_age_ms": 40,
        "snapshot_age_ms": 20,
        "dropped_frames_total": 100,
        "feed_status": "healthy",
    }
    row.update(changes)
    return row


def machine(**changes):
    row = {
        "feed_snapshot_age_ms": 500,
        "stream_row_age_ms": 1000,
        "symbols_total": 656,
        "book_age_p50_ms": 3000,
        "book_age_p95_ms": 4500,
        "book_age_max_ms": 6000,
        "symbols_book_le_5s": 620,
        "freshness_p50_ms": 2500,
        "source": "untrusted fixture value",
    }
    row.update(changes)
    return row


def cloudflare(**changes):
    row = {
        "ok": True,
        "ha_connections": 4,
        "ready_connections": 4,
        "ready_status": 200,
        "total_requests_delta": 3,
        "concurrent_requests": 1,
        "latest_rtt_ms_max": 24,
        "smoothed_rtt_ms_max": 20,
        "min_rtt_ms_min": 12,
        "lost_packets_delta": 0,
        "connect_streams_errors_delta": 0,
        "register_success_total": 4,
        "metrics_url_port": 20241,
    }
    row.update(changes)
    return row


def origin(status=200, **changes):
    row = {
        "ok": status == 200,
        "http_status": status,
        "rtt_ms": 3,
        "size_bytes": 1200,
        "request_errors_delta": 0,
        "response_5xx_delta": 0,
        "error": None,
    }
    row.update(changes)
    return row


def news(**changes):
    row = {
        "sources_available": 104,
        "sources_live": 74,
        "sources_pending": 15,
        "sources_error": 0,
        "as_of_age_s": 30,
        "last_fetch_age_s": 20,
    }
    row.update(changes)
    return row


class StaticContractTests(unittest.TestCase):
    def test_nodes_and_edges_have_the_frozen_ids_and_fields(self):
        self.assertEqual(len(health.NODES), 14)
        self.assertEqual(len({node["id"] for node in health.NODES}), 14)
        self.assertTrue(all(set(node) == {"id", "label", "kind"}
                            for node in health.NODES))
        expected_ids = [
            "browser_site", "cf_connector", "connector_origin", "spot_rest",
            "spot_wss", "usdm_rest", "usdm_wss", "feed_machine", "news_fetch",
            "binance_account", "binance_exec", "aws_secrets", "aws_ssm",
        ]
        self.assertEqual([edge["id"] for edge in health.EDGES], expected_ids)
        fields = {"id", "source", "target", "label", "direction", "mode",
                  "required", "group", "authority", "controls"}
        self.assertTrue(all(set(edge) == fields for edge in health.EDGES))
        self.assertEqual(sum(edge["required"] for edge in health.EDGES), 8)

    def test_controls_are_present_only_for_implemented_groups(self):
        by_id = {edge["id"]: edge for edge in health.EDGES}
        full = ["pause_monitor", "monitor_every", "probe_now"]
        read = ["pause_monitor", "monitor_every"]
        for edge_id in ("spot_rest", "usdm_rest", "cf_connector", "connector_origin"):
            self.assertEqual(by_id[edge_id]["controls"], full)
        for edge_id in ("spot_wss", "usdm_wss", "feed_machine", "news_fetch"):
            self.assertEqual(by_id[edge_id]["controls"], read)
        for edge_id in ("browser_site", "binance_account", "binance_exec",
                        "aws_secrets", "aws_ssm"):
            self.assertEqual(by_id[edge_id]["controls"], [])

    def test_unknown_edge_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown network edge"):
            health.derive_edge("not_present", None, None, CONFIG, NOW)

    def test_derived_edge_has_the_complete_public_shape(self):
        edge = health.derive_edge("spot_rest", rest(), None, CONFIG, NOW)
        expected = {
            "id", "source", "target", "label", "direction", "mode", "required",
            "group", "authority", "controls", "health", "health_reason",
            "monitoring", "truth", "observed_at", "observed_ms", "fresh_until_ms",
            "cadence_ms", "sample_count", "metrics", "counters", "evidence",
        }
        self.assertEqual(set(edge), expected)
        json.dumps(edge, allow_nan=False)

    def test_r2_fresh_until_uses_each_evidence_deadline(self):
        cases = (
            ("spot_wss", wss(snapshot_age_ms=59000), NOW + 1000),
            ("feed_machine", machine(stream_row_age_ms=599000), NOW + 1000),
            ("news_fetch", news(as_of_age_s=359), NOW + 1000),
            ("spot_rest", rest(), NOW + 45000),
            ("usdm_wss", wss(snapshot_age_ms=60001), NOW),
        )
        for edge_id, evidence, expected in cases:
            with self.subTest(edge_id=edge_id):
                edge = health.derive_edge(
                    edge_id, evidence, None, CONFIG, NOW
                )
                self.assertEqual(edge["fresh_until_ms"], expected)


class RestHealthTests(unittest.TestCase):
    def test_pause_is_off_with_receipt_and_does_not_claim_truth(self):
        config = copy.deepcopy(CONFIG)
        config["paused"]["rest"] = True
        edge = health.derive_edge("spot_rest", rest(), None, config, NOW)
        self.assertEqual(edge["health"], "Off")
        self.assertEqual(edge["monitoring"], "Paused")
        self.assertEqual(edge["health_reason"], "Monitor paused (receipt 17)")
        self.assertEqual(edge["truth"]["application"], "Unknown")

    def test_missing_probe_is_unknown_and_metrics_stay_null(self):
        edge = health.derive_edge("spot_rest", None, None, CONFIG, NOW)
        self.assertEqual(edge["health"], "Unknown")
        self.assertIn("missing", edge["health_reason"])
        self.assertIsNone(edge["metrics"]["http_status"])
        self.assertEqual(edge["truth"]["configured"], "Pass")

    def test_probe_marker_without_result_is_unknown_malformed_evidence(self):
        edge = health.derive_edge(
            "spot_rest", {"probe_now": True}, None, CONFIG, NOW)
        self.assertEqual(edge["health"], "Unknown")
        self.assertIn("malformed", edge["health_reason"])
        self.assertIn("probe", [row["transition"] for row in health.transitions(None, edge)])

    def test_two_successes_complete_recovery_quorum(self):
        first = health.derive_edge("spot_rest", rest(), None, CONFIG, NOW)
        second = health.derive_edge("spot_rest", rest(), first, CONFIG, NOW + 1000)
        self.assertEqual(first["health"], "Degraded")
        self.assertIn("recovering 1/2", first["health_reason"])
        self.assertEqual(second["health"], "Operational")
        self.assertEqual(second["counters"]["quorum"], 2)
        self.assertEqual(second["sample_count"], 2)
        self.assertEqual(second["truth"], {
            "configured": "Pass", "transport": "Pass", "application": "Pass",
        })
        self.assertEqual(second["fresh_until_ms"], NOW + 1000 + 45000)

    def test_one_failure_is_degraded_and_two_are_outage(self):
        failed = rest(status=None, server_time=None, ok=False, error="fixture connect error")
        first = health.derive_edge("spot_rest", failed, None, CONFIG, NOW)
        second = health.derive_edge("spot_rest", failed, first, CONFIG, NOW + 1000)
        self.assertEqual(first["health"], "Degraded")
        self.assertEqual(second["health"], "Outage")
        self.assertEqual(second["counters"]["consecutive_failures"], 2)
        self.assertEqual(second["counters"]["failures"], 2)
        self.assertEqual(second["truth"]["transport"], "Fail")

    def test_418_is_an_outage_and_is_counted(self):
        edge = health.derive_edge(
            "spot_rest", rest(418, None, ok=False, retry_after_s=60),
            None, CONFIG, NOW,
        )
        self.assertEqual(edge["health"], "Outage")
        self.assertEqual(edge["counters"]["count_418"], 1)
        self.assertIn("418 is in force", edge["health_reason"])

    def test_recent_429_stays_degraded_for_five_minutes(self):
        limited = health.derive_edge(
            "spot_rest", rest(429, None, ok=False), None, CONFIG, NOW)
        first_good = health.derive_edge(
            "spot_rest", rest(), limited, CONFIG, NOW + 120000)
        second_good = health.derive_edge(
            "spot_rest", rest(), first_good, CONFIG, NOW + 300001)
        self.assertEqual(first_good["health"], "Degraded")
        self.assertEqual(first_good["health_reason"], "Spot REST 429 seen 2 min ago")
        self.assertEqual(second_good["health"], "Operational")
        self.assertEqual(second_good["counters"]["count_429"], 1)

    def test_weight_at_eighty_percent_is_degraded(self):
        first = health.derive_edge("spot_rest", rest(), None, CONFIG, NOW)
        ready = health.derive_edge("spot_rest", rest(), first, CONFIG, NOW + 1000)
        edge = health.derive_edge(
            "spot_rest", rest(used_weight_1m=4800), ready, CONFIG, NOW + 2000)
        self.assertEqual(edge["metrics"]["weight_limit_1m"], 6000)
        self.assertEqual(edge["health"], "Degraded")
        self.assertIn("4800 of 6000", edge["health_reason"])

    def test_old_probe_is_stale(self):
        evidence = rest(observed_ms=NOW - 45001)
        edge = health.derive_edge("spot_rest", evidence, None, CONFIG, NOW)
        self.assertEqual(edge["health"], "Stale")

    def test_http_200_without_server_time_is_one_failure(self):
        edge = health.derive_edge(
            "usdm_rest", rest(200, None, ok=False), None, CONFIG, NOW)
        self.assertEqual(edge["health"], "Degraded")
        self.assertIn("did not include server time", edge["health_reason"])
        self.assertEqual(edge["truth"]["application"], "Fail")

    def test_r2_rest_success_requires_strict_proof(self):
        prior = health.derive_edge("spot_rest", rest(), None, CONFIG, NOW)
        invalid_changes = (
            {"ok": 1},
            {"server_time_ms": "1800000000000"},
            {"server_time_ms": True},
            {"server_time_ms": 0},
            {"server_time_ms": -1},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes):
                edge = health.derive_edge(
                    "spot_rest", rest(**changes), prior, CONFIG, NOW + 1000
                )
                self.assertEqual(edge["truth"]["application"], "Fail")
                self.assertEqual(edge["counters"]["quorum"], 0)
                self.assertNotEqual(edge["health"], "Operational")

    def test_r2_unproven_rest_sample_resets_recovery_quorum(self):
        interruptions = (
            None,
            {"probe_now": True},
            rest(503, None, ok=False),
        )
        for evidence in interruptions:
            with self.subTest(evidence=evidence):
                first = health.derive_edge(
                    "spot_rest", rest(), None, CONFIG, NOW
                )
                interrupted = health.derive_edge(
                    "spot_rest", evidence, first, CONFIG, NOW + 1000
                )
                recovered = health.derive_edge(
                    "spot_rest", rest(), interrupted, CONFIG, NOW + 2000
                )
                self.assertEqual(interrupted["counters"]["quorum"], 0)
                self.assertEqual(recovered["counters"]["quorum"], 1)
                self.assertEqual(recovered["health"], "Degraded")


class WssHealthTests(unittest.TestCase):
    def test_healthy_feed_is_operational_and_first_delta_is_unknown(self):
        edge = health.derive_edge("spot_wss", wss(), None, CONFIG, NOW)
        self.assertEqual(edge["health"], "Operational")
        self.assertIsNone(edge["metrics"]["dropped_frames_delta"])
        self.assertEqual(edge["truth"], {
            "configured": "Pass", "transport": "Pass", "application": "Pass",
        })

    def test_cumulative_counter_reset_has_unknown_delta(self):
        previous = health.derive_edge("spot_wss", wss(dropped_frames_total=100), None, CONFIG, NOW)
        edge = health.derive_edge(
            "spot_wss", wss(dropped_frames_total=5), previous, CONFIG, NOW + 1000)
        self.assertIsNone(edge["metrics"]["dropped_frames_delta"])

    def test_missing_and_old_snapshots_are_unknown_and_stale(self):
        missing = health.derive_edge("spot_wss", None, None, CONFIG, NOW)
        stale = health.derive_edge(
            "spot_wss", wss(snapshot_age_ms=60001), None, CONFIG, NOW)
        self.assertEqual(missing["health_reason"], "Feed evidence is not mirrored")
        self.assertEqual(missing["health"], "Unknown")
        self.assertEqual(stale["health"], "Stale")

    def test_broken_or_empty_connection_is_outage(self):
        for changes in ({"feed_status": "broken"}, {"connected_count": 0}):
            with self.subTest(changes=changes):
                edge = health.derive_edge("spot_wss", wss(**changes), None, CONFIG, NOW)
                self.assertEqual(edge["health"], "Outage")

    def test_degraded_silent_and_partial_feeds_are_degraded(self):
        cases = (
            {"feed_status": "degraded"},
            {"last_message_age_ms": 60001},
            {"connected_count": 3},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                edge = health.derive_edge("usdm_wss", wss(**changes), None, CONFIG, NOW)
                self.assertEqual(edge["health"], "Degraded")

    def test_unexposed_feed_status_or_message_age_is_unknown(self):
        for changes in ({"feed_status": "other"}, {"last_message_age_ms": None}):
            with self.subTest(changes=changes):
                edge = health.derive_edge("spot_wss", wss(**changes), None, CONFIG, NOW)
                self.assertEqual(edge["health"], "Unknown")

    def test_non_finite_snapshot_age_is_unknown(self):
        edge = health.derive_edge(
            "spot_wss", wss(snapshot_age_ms=float("inf")), None, CONFIG, NOW)
        self.assertEqual(edge["health"], "Unknown")
        self.assertIsNone(edge["metrics"]["snapshot_age_ms"])

    def test_deltas_create_reconnect_and_frame_markers(self):
        initial = health.derive_edge("spot_wss", wss(), None, CONFIG, NOW)
        steady = health.derive_edge("spot_wss", wss(), initial, CONFIG, NOW + 1000)
        rising = health.derive_edge(
            "spot_wss", wss(dropped_frames_total=105, reconnects=1),
            steady, CONFIG, NOW + 2000,
        )
        stopped = health.derive_edge(
            "spot_wss", wss(dropped_frames_total=105, reconnects=1),
            rising, CONFIG, NOW + 3000,
        )
        self.assertEqual(rising["metrics"]["dropped_frames_delta"], 5)
        self.assertEqual(
            [event["transition"] for event in health.transitions(steady, rising)],
            ["reconnect", "frames dropping"],
        )
        self.assertIn(
            "frames stopped dropping",
            [event["transition"] for event in health.transitions(rising, stopped)],
        )


class FeedMachineHealthTests(unittest.TestCase):
    def test_book_age_rules_cover_operational_degraded_and_outage(self):
        cases = ((3000, "Operational"), (5001, "Degraded"), (60001, "Outage"))
        for book_age, expected in cases:
            with self.subTest(book_age=book_age):
                edge = health.derive_edge(
                    "feed_machine", machine(book_age_p50_ms=book_age),
                    None, CONFIG, NOW,
                )
                self.assertEqual(edge["health"], expected)
                self.assertIsNone(edge["metrics"]["source"])

    def test_p9_three_mirror_intervals_keep_feed_machine_assessment_stable(self):
        cases = (
            (5399, 8209, "Degraded"),
            (20000, 8209, "Unknown"),
            (4000, 3000, "Operational"),
        )
        for snapshot_age, book_age, expected in cases:
            with self.subTest(snapshot_age=snapshot_age, book_age=book_age):
                edge = health.derive_edge(
                    "feed_machine",
                    machine(feed_snapshot_age_ms=snapshot_age,
                            book_age_p50_ms=book_age),
                    None, CONFIG, NOW,
                )
                self.assertEqual(edge["health"], expected)

    def test_old_machine_row_is_stale(self):
        edge = health.derive_edge(
            "feed_machine", machine(stream_row_age_ms=600001),
            None, CONFIG, NOW,
        )
        self.assertEqual(edge["health"], "Stale")

    def test_missing_files_and_old_feed_with_old_books_are_unknown(self):
        cases = (
            (None, "Feed or machine evidence is not mirrored"),
            (machine(feed_snapshot_age_ms=None), "Feed evidence is not mirrored"),
            (machine(book_age_p50_ms=None), "Machine stream evidence is not mirrored"),
            (machine(feed_snapshot_age_ms=15001, book_age_p50_ms=6000),
             "Feed snapshot is too old to assess machine lag"),
        )
        for evidence, reason in cases:
            with self.subTest(reason=reason):
                edge = health.derive_edge("feed_machine", evidence, None, CONFIG, NOW)
                self.assertEqual(edge["health"], "Unknown")
                self.assertEqual(edge["health_reason"], reason)

    def test_r2_empty_machine_stream_is_configured_but_unknown(self):
        evidence = machine(
            feed_snapshot_age_ms=None,
            symbols_total=0,
            stream_row_age_ms=None,
            book_age_p50_ms=None,
            book_age_p95_ms=None,
            book_age_max_ms=None,
            freshness_p50_ms=None,
        )
        edge = health.derive_edge(
            "feed_machine", evidence, None, CONFIG, NOW
        )
        self.assertEqual(edge["truth"]["configured"], "Pass")
        self.assertEqual(edge["truth"]["application"], "Unknown")
        self.assertEqual(edge["health"], "Unknown")
        self.assertEqual(edge["health_reason"], "Machine stream has no rows")


class CloudflareHealthTests(unittest.TestCase):
    def test_ready_connector_is_operational(self):
        edge = health.derive_edge("cf_connector", cloudflare(), None, CONFIG, NOW)
        self.assertEqual(edge["health"], "Operational")
        self.assertEqual(edge["truth"], {
            "configured": "Pass", "transport": "Pass", "application": "Pass",
        })

    def test_flat_ready_shape_and_separate_ready_failure_preserve_metrics(self):
        flat = cloudflare()
        flat.pop("ready_status")
        flat["status"] = 200
        edge = health.derive_edge("cf_connector", flat, None, CONFIG, NOW)
        ready_failed = health.derive_edge(
            "cf_connector", cloudflare(ok=False, ready_status=503),
            edge, CONFIG, NOW + 1000,
        )
        self.assertEqual(edge["health"], "Operational")
        self.assertEqual(ready_failed["health"], "Degraded")
        self.assertEqual(ready_failed["truth"]["configured"], "Pass")

    def test_partial_ha_errors_and_readiness_are_degraded(self):
        cases = (
            {"ha_connections": 3},
            {"connect_streams_errors_delta": 1},
            {"ready_status": 503},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                edge = health.derive_edge(
                    "cf_connector", cloudflare(**changes), None, CONFIG, NOW)
                self.assertEqual(edge["health"], "Degraded")

    def test_zero_ha_connections_is_outage(self):
        edge = health.derive_edge(
            "cf_connector", cloudflare(ha_connections=0), None, CONFIG, NOW)
        self.assertEqual(edge["health"], "Outage")
        self.assertEqual(edge["truth"]["transport"], "Fail")

    def test_non_finite_ha_metric_is_unknown(self):
        edge = health.derive_edge(
            "cf_connector", cloudflare(ha_connections=float("nan")),
            None, CONFIG, NOW,
        )
        self.assertEqual(edge["health"], "Unknown")
        self.assertIsNone(edge["metrics"]["ha_connections"])

    def test_two_unreachable_samples_are_outage(self):
        evidence = {"ok": False, "metrics_url_port": 20241, "error": "fixture unavailable"}
        first = health.derive_edge("cf_connector", evidence, None, CONFIG, NOW)
        second = health.derive_edge("cf_connector", evidence, first, CONFIG, NOW + 1000)
        self.assertEqual(first["health"], "Degraded")
        self.assertEqual(second["health"], "Outage")
        self.assertEqual(second["counters"]["consecutive_failures"], 2)

    def test_no_endpoint_and_missing_metric_are_unknown(self):
        absent = health.derive_edge("cf_connector", None, None, CONFIG, NOW)
        incomplete = health.derive_edge(
            "cf_connector", cloudflare(connect_streams_errors_delta=None),
            None, CONFIG, NOW,
        )
        self.assertEqual(
            absent["health_reason"],
            "cloudflared metrics endpoint not found on 127.0.0.1:20241–20245",
        )
        self.assertEqual(incomplete["health"], "Unknown")

    def test_probe_marker_without_connector_result_keeps_endpoint_reason(self):
        edge = health.derive_edge(
            "cf_connector", {"probe_now": True}, None, CONFIG, NOW)
        self.assertEqual(edge["health"], "Unknown")
        self.assertIn("endpoint not found", edge["health_reason"])
        self.assertIn("probe", [row["transition"] for row in health.transitions(None, edge)])

    def test_old_connector_sample_is_stale(self):
        edge = health.derive_edge(
            "cf_connector", cloudflare(observed_ms=NOW - 45001),
            None, CONFIG, NOW,
        )
        self.assertEqual(edge["health"], "Stale")


class OriginAndNewsHealthTests(unittest.TestCase):
    def test_origin_200_is_operational_and_proxy_errors_degrade(self):
        good = health.derive_edge("connector_origin", origin(), None, CONFIG, NOW)
        proxy = health.derive_edge(
            "connector_origin", origin(request_errors_delta=2),
            good, CONFIG, NOW + 1000,
        )
        self.assertEqual(good["health"], "Operational")
        self.assertEqual(proxy["health"], "Degraded")
        names = [event["transition"] for event in health.transitions(good, proxy)]
        self.assertIn("origin proxy errors", names)

    def test_origin_one_failure_then_outage_and_missing_is_unknown(self):
        failed = origin(status=None, ok=False, rtt_ms=None, size_bytes=None)
        first = health.derive_edge("connector_origin", failed, None, CONFIG, NOW)
        second = health.derive_edge(
            "connector_origin", failed, first, CONFIG, NOW + 1000)
        missing = health.derive_edge("connector_origin", None, None, CONFIG, NOW)
        self.assertEqual(first["health"], "Degraded")
        self.assertEqual(second["health"], "Outage")
        self.assertEqual(missing["health"], "Unknown")

    def test_origin_probe_marker_without_result_is_unknown(self):
        edge = health.derive_edge(
            "connector_origin", {"probe_now": True}, None, CONFIG, NOW)
        self.assertEqual(edge["health"], "Unknown")
        self.assertIn("malformed", edge["health_reason"])

    def test_old_origin_sample_is_stale(self):
        edge = health.derive_edge(
            "connector_origin", origin(observed_ms=NOW - 45001),
            None, CONFIG, NOW,
        )
        self.assertEqual(edge["health"], "Stale")

    def test_news_freshness_and_source_rules(self):
        cases = (
            (news(), "Operational"),
            (news(sources_error=1), "Degraded"),
            (news(sources_live=20), "Degraded"),
            (news(as_of_age_s=361), "Stale"),
            (news(as_of_age_s=None), "Unknown"),
            (None, "Unknown"),
        )
        for evidence, expected in cases:
            with self.subTest(expected=expected, evidence=evidence):
                edge = health.derive_edge("news_fetch", evidence, None, CONFIG, NOW)
                self.assertEqual(edge["health"], expected)


class PassiveAndTransitionTests(unittest.TestCase):
    def test_session_and_on_demand_paths_stay_unknown(self):
        browser = health.derive_edge(
            "browser_site", {"fetch_ms": 42}, None, CONFIG, NOW)
        execution = health.derive_edge(
            "binance_exec",
            {"last_used_at": "2027-01-15T08:00:00Z", "last_op": "validate",
             "last_outcome": "accepted"},
            None, CONFIG, NOW,
        )
        self.assertEqual(browser["health"], "Unknown")
        self.assertEqual(execution["health"], "Unknown")
        self.assertEqual(
            execution["health_reason"],
            "On-demand path; last used 2027-01-15T08:00:00Z, validate accepted",
        )
        reasons = {
            edge_id: health.derive_edge(edge_id, None, None, CONFIG, NOW)["health_reason"]
            for edge_id in ("binance_account", "aws_secrets", "aws_ssm")
        }
        self.assertIn("signed-read", reasons["binance_account"])
        self.assertIn("secrets-read", reasons["aws_secrets"])
        self.assertIn("SSM", reasons["aws_ssm"])

    def test_health_change_severity_follows_new_health(self):
        expected = {
            "Outage": "high", "Stale": "high", "Degraded": "medium",
            "Unknown": "medium", "Off": "info", "Operational": "info",
        }
        for new_health, severity in expected.items():
            with self.subTest(new_health=new_health):
                old_health = "Unknown" if new_health == "Operational" else "Operational"
                previous = {"health": old_health, "metrics": {}, "counters": {}}
                new = {
                    "id": "spot_rest", "label": "Spot REST", "health": new_health,
                    "health_reason": "Fixture health changed", "metrics": {},
                    "counters": {}, "evidence": {},
                }
                event = health.transitions(previous, new)[0]
                self.assertEqual(event["severity"], severity)
                self.assertEqual(event["transition"], "%s → %s" % (old_health, new_health))

    def test_http_markers_and_probe_result_are_emitted(self):
        previous = health.derive_edge("spot_rest", rest(), None, CONFIG, NOW)
        limited = health.derive_edge(
            "spot_rest", rest(429, None, ok=False), previous, CONFIG, NOW + 1000)
        held = health.derive_edge(
            "spot_rest", rest(418, None, ok=False), limited, CONFIG, NOW + 2000)
        probe = health.derive_edge(
            "spot_rest", rest(probe_now=True), previous, CONFIG, NOW + 3000)
        self.assertIn("429 seen", [row["transition"] for row in health.transitions(previous, limited)])
        self.assertIn("418 seen", [row["transition"] for row in health.transitions(limited, held)])
        probe_rows = [row for row in health.transitions(previous, probe)
                      if row["transition"] == "probe"]
        self.assertEqual(len(probe_rows), 1)
        self.assertEqual(probe_rows[0]["reason"], "probe now spot_rest: 200 in 24 ms")

    def test_unchanged_healthy_sample_has_no_event(self):
        first = health.derive_edge("spot_wss", wss(), None, CONFIG, NOW)
        second = health.derive_edge("spot_wss", wss(), first, CONFIG, NOW + 1000)
        self.assertEqual(health.transitions(first, second), [])

    def test_p9_reason_only_unknown_change_has_no_health_event(self):
        previous = {
            "id": "binance_exec",
            "health_reason": "On-demand path; no execution evidence is mirrored",
            "metrics": {},
            "counters": {},
        }
        current = health.derive_edge(
            "binance_exec",
            {"last_used_at": "2027-01-15T08:00:00Z", "last_op": "validate",
             "last_outcome": "accepted"},
            previous, CONFIG, NOW,
        )

        self.assertEqual(health.transitions(previous, current), [])


class SummaryAndReadTimeTests(unittest.TestCase):
    def make_edges(self):
        edges = []
        for index, static in enumerate(health.EDGES):
            edge = dict(static)
            edge.update({
                "health": "Operational",
                "health_reason": "Fixture is current",
                "sample_count": 1,
                "observed_ms": NOW - index * 1000,
                "observed_at": "2027-01-15T08:00:%02d.000Z" % (20 - index),
                "fresh_until_ms": NOW + 60000,
            })
            edges.append(edge)
        return edges

    def test_summary_counts_required_edges_and_finds_worst_oldest_and_latest(self):
        edges = self.make_edges()
        edges[3]["health"] = "Outage"
        edges[0]["health"] = "Unknown"
        edges[2]["last_transition"] = {
            "ts": "2027-01-15T08:00:01Z", "transition": "first"}
        edges[3]["last_transition"] = {
            "ts": "2027-01-15T08:00:02Z", "transition": "second"}
        summary = health.summarize(edges, NOW)
        self.assertEqual(summary["operational_required"], 7)
        self.assertEqual(summary["observed_required"], 8)
        self.assertEqual(summary["required_total"], 8)
        self.assertEqual(summary["unknown_count"], 1)
        self.assertEqual(summary["worst"], {"edge_id": "spot_rest", "health": "Outage"})
        self.assertEqual(summary["oldest_required_evidence_at"], health._iso(NOW - 8000))
        self.assertEqual(summary["last_transition"]["edge_id"], "spot_rest")

    def test_r2_summary_compares_integer_transition_timestamps_numerically(self):
        edges = self.make_edges()
        edges[2]["last_transition"] = {"ts": 999, "transition": "older"}
        edges[3]["last_transition"] = {"ts": 1000, "transition": "newer"}

        summary = health.summarize(edges, NOW)

        self.assertEqual(summary["last_transition"]["ts"], 1000)
        self.assertEqual(summary["last_transition"]["edge_id"], "spot_rest")

    def test_summary_applies_read_time_staleness_without_mutation(self):
        edges = self.make_edges()
        edges[1]["fresh_until_ms"] = NOW - 1
        original = copy.deepcopy(edges)
        summary = health.summarize(edges, NOW)
        self.assertEqual(summary["worst"], {"edge_id": "cf_connector", "health": "Stale"})
        self.assertEqual(edges, original)

    def test_read_time_marks_expired_edge_and_stopped_collector_but_not_off_edge(self):
        edges = self.make_edges()[:2]
        edges[0]["fresh_until_ms"] = NOW - 1
        edges[1]["fresh_until_ms"] = NOW - 1
        edges[1]["health"] = "Off"
        current = {
            "generated_ms": NOW - 15001,
            "collector": {"status": "running", "tick_s": 5},
            "summary": {"last_transition": {"ts": "kept", "edge_id": "spot_rest",
                                             "transition": "kept"}},
            "edges": edges,
        }
        original = copy.deepcopy(current)
        result = health.apply_read_time_staleness(current, NOW)
        self.assertEqual(result["edges"][0]["health"], "Stale")
        self.assertEqual(
            result["edges"][0]["health_reason"],
            "No fresh sample since %s" % edges[0]["observed_at"],
        )
        self.assertEqual(result["edges"][1]["health"], "Off")
        self.assertEqual(result["collector"]["status"], "stopped")
        self.assertEqual(result["summary"]["last_transition"]["ts"], "kept")
        self.assertEqual(current, original)

    def test_collector_remains_running_at_exact_three_tick_boundary(self):
        current = {
            "generated_ms": NOW - 15000,
            "collector": {"status": "running", "tick_s": 5},
            "summary": {},
            "edges": [],
        }
        result = health.apply_read_time_staleness(current, NOW)
        self.assertEqual(result["collector"]["status"], "running")

    def test_r2_collector_timing_requires_valid_fields(self):
        cases = ((None, 5), (NOW, "5"), (NOW, 0), (float(NOW), 5))
        for generated_ms, tick_s in cases:
            with self.subTest(generated_ms=generated_ms, tick_s=tick_s):
                edge = self.make_edges()[0]
                edge["fresh_until_ms"] = NOW - 1
                current = {
                    "generated_ms": generated_ms,
                    "collector": {"status": "running", "tick_s": tick_s},
                    "summary": {},
                    "edges": [edge],
                }

                result = health.apply_read_time_staleness(current, NOW)

                self.assertEqual(result["collector"]["status"], "unknown")
                self.assertEqual(result["edges"][0]["health"], "Stale")

    def test_r6_list_collector_becomes_unknown_before_timing(self):
        current = {
            "generated_ms": NOW - 15001,
            "collector": [],
            "summary": {},
            "edges": [],
        }

        result = health.apply_read_time_staleness(current, NOW)

        self.assertEqual(result["collector"], {"status": "unknown"})
        self.assertEqual(current["collector"], [])


if __name__ == "__main__":
    unittest.main()
