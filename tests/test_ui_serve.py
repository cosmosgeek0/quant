"""Loopback HTTP evidence for ui/serve.py's read-only runtime truth view."""

import gzip
import hashlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
sys.path.insert(0, str(UI_DIR))

import serve  # noqa: E402


EVENT_LINES = [
    '{"ts":"2026-08-12T15:52:45Z","sha8":"d1b204bc","kind":"run_start",'
    '"run_id":"6a7c96cdb1754b","symbol":"BTCUSDT",'
    '"balances":{"BTC":"0.00000000","USDT":"95.62398534"}}',
    '{"ts":"2026-08-12T15:52:46Z","sha8":"d1b204bc","kind":"run_verdict",'
    '"run_id":"6a7c96cdb1754b","symbol":"BTCUSDT",'
    '"verdict":"REJECTED_BEFORE_BUY"}',
]


def stream_row(venues, surge, gap, spot=True):
    return {
        "venues": venues,
        "spot_bid": "100.00" if spot else None,
        "spot_ask": "100.01" if spot else None,
        "perp_bid": "100.40",
        "perp_ask": "100.41",
        "mark": "100.405",
        "vol_1m_quote": "125000.50",
        "vol_med_1m": "25000.10",
        "surge_ratio": surge,
        "oi": "450000.75",
        "oi_change_pct": "3.20",
        "funding_rate": "0.000125",
        "next_funding_ms": 1786608000000,
        "basis_gap_pct": gap,
        "freshness_ms": 184,
    }


def proposal(proposal_id="live01", expires="2099-08-13T00:02:00.000Z"):
    return {
        "proposal_id": proposal_id,
        "created_utc": "2026-08-13T00:00:00.000Z",
        "expires_utc": expires,
        "quant_sha": "a" * 40,
        "config_digest": "b" * 64,
        "symbol": "APRUSDT",
        "direction": "long_spot_short_perp",
        "spot_ask": "0.04510000",
        "perp_bid": "0.04533000",
        "entry_gap_pct": "0.50997783",
        "spot_bid": "0.04509000",
        "perp_ask": "0.04534000",
        "exit_gap_pct": "0.55444666",
        "spot_maker_commission_pct": "0.0800",
        "spot_taker_commission_pct": "0.0750",
        "perp_maker_commission_pct": "0.0200",
        "perp_taker_commission_pct": "0.0400",
        "commission_source": "measured",
        "entry_slippage_pct": "0.0100",
        "exit_slippage_pct": "0.0120",
        "funding_rate": "0.000125",
        "funding_period_ms": 28800000,
        "carry_pct": "0.0125",
        "safety_buffer_pct": "0.2500",
        "net_edge_pct": "0.02503117",
        "quantity": "220.00000000",
    }


class ServerHarness(unittest.TestCase):
    """Start the real server on 127.0.0.1:0 with isolated source paths."""

    def start(self, sources):
        server = serve.make_server(sources, port=0)
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05),
            daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server
        self.port = server.server_address[1]
        return "http://127.0.0.1:%d" % self.port

    def tempdir(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def get(self, base, path, headers=None):
        request = urllib.request.Request(
            base + path, headers=headers or {}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def get_json(self, base, path, expect_status=200):
        status, _, body = self.get(base, path)
        self.assertEqual(status, expect_status,
                         "unexpected status for %s: %s" % (path, status))
        return json.loads(body)

    def request_full(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def request(self, method, path, body=None):
        status, _, response_body = self.request_full(method, path, body=body)
        return status, response_body

    def empty_sources(self, missing_root):
        return {
            "events": missing_root / "events.jsonl",
            "stream_state": missing_root / "stream_state.json",
            "stream_log": missing_root / "stream_log.jsonl",
            "proposals_dir": missing_root / "proposals",
            "basis_position": missing_root / "basis-position.json",
            "basis_receipts_dir": missing_root,
            "fees": missing_root / "commission-rates.json",
            "momentum_state": missing_root / "momentum-paper-state.json",
            "momentum_receipts": (
                missing_root / "momentum-paper-receipts.jsonl"),
            "health": missing_root / "health.jsonl",
            "jobs_log": missing_root / "jobs.log",
            "stop": missing_root / "STOP",
            "stop_jobs": missing_root / "STOP-JOBS",
            "momentum_decisions": missing_root / "momentum-decisions.jsonl",
            "job_results": missing_root / "jobs" / "results.jsonl",
            "job_receipts_dir": missing_root / "jobs" / "receipts",
            "governor_dir": missing_root,
            "governor_lock": missing_root / "account-governor.lock",
            "manifest": missing_root / "pull_manifest.json",
            "news": missing_root / "news.jsonl",
            "news_sources": missing_root / "news_sources.json",
        }

    @staticmethod
    def utc_text(moment):
        return moment.astimezone(timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")


class TestStaticServing(ServerHarness):

    def test_runtime_page_and_assets_serve_over_http(self):
        # Front-end is a blank page by owner order (2026-08-15); the
        # data layer keeps serving. Markers return when a UI returns.
        base = self.start({})
        for path, kind, markers in (
                ("/", "text/html", (b"<!doctype html", b"<title>Quant",)),
                ("/index.html", "text/html",
                 (b"<!doctype html", b"<title>Quant",)),
                ("/fixtures/events.jsonl", "text/plain", (b'"run_id"',))):
            status, headers, body = self.get(base, path)
            self.assertEqual(status, 200, path)
            self.assertIn(kind, headers.get("Content-Type", ""), path)
            for marker in markers:
                self.assertIn(marker, body, path)

    def test_page_has_no_browser_exchange_fetch_or_legacy_live_sections(self):
        base = self.start({})
        _, _, html = self.get(base, "/index.html")
        self.assertNotIn(b"api.binance.com", html)
        self.assertNotIn(b"PRICE_URL", html)
        for legacy in ("/api/basis", "/api/futures", "/api/volume"):
            payload = self.get_json(base, legacy, expect_status=404)
            self.assertEqual(payload["error"], "not found")

    def test_unknown_path_and_traversal_are_refused(self):
        self.start({})
        for path in ("/api/nope", "/../TASK.md", "/%2e%2e/TASK.md",
                     "/fixtures/../../TASK.md"):
            status, body = self.request("GET", path)
            self.assertEqual(status, 404, path)
            self.assertIn(b"not found", body, path)

    def test_binds_loopback_only(self):
        self.start({})
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


class TestBackendHardening(ServerHarness):

    SECURITY_HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self'; font-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"),
    }

    def assert_security_headers(self, headers):
        for name, expected in self.SECURITY_HEADERS.items():
            self.assertEqual(headers.get(name), expected, name)

    def test_api_and_static_etag_304_round_trips(self):
        root = self.tempdir()
        fees = root / "fees.json"
        fees.write_text('{"maker":"0.01"}\n', encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources["fees"] = fees
        base = self.start(sources)

        api_status, api_headers, api_body = self.get(base, "/api/fees")
        self.assertEqual(api_status, 200)
        self.assertTrue(api_body)
        self.assertRegex(api_headers["ETag"], r'^"[0-9a-f]{64}"$')
        self.assertEqual(
            api_headers["ETag"],
            '"%s"' % hashlib.sha256(api_body).hexdigest())
        self.assertEqual(api_headers["Cache-Control"], "no-cache")
        status, headers, body = self.get(
            base, "/api/fees",
            headers={"If-None-Match": api_headers["ETag"]})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(headers["ETag"], api_headers["ETag"])
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertEqual(headers["Content-Length"], "0")
        self.assertNotIn("Content-Encoding", headers)

        static_status, static_headers, static_body = self.get(
            base, "/index.html")
        self.assertEqual(static_status, 200)
        self.assertTrue(static_body)
        self.assertRegex(static_headers["ETag"], r'^"[0-9a-f]{64}"$')
        static_facts = (UI_DIR / "index.html").stat()
        static_prefix = ("static:%d:%d:%d:" % (
            static_facts.st_mtime_ns, static_facts.st_size,
            static_facts.st_ino)).encode("ascii")
        self.assertEqual(
            static_headers["ETag"],
            '"%s"' % hashlib.sha256(
                static_prefix + static_body).hexdigest())
        self.assertEqual(static_headers["Cache-Control"], "no-cache")
        self.assertIn("Last-Modified", static_headers)
        for conditional in (
                {"If-None-Match": static_headers["ETag"]},
                {"If-Modified-Since": static_headers["Last-Modified"]}):
            status, headers, body = self.get(
                base, "/index.html", headers=conditional)
            self.assertEqual(status, 304)
            self.assertEqual(body, b"")
            self.assertEqual(headers["ETag"], static_headers["ETag"])
            self.assertEqual(headers["Cache-Control"], "no-cache")
            self.assertEqual(
                headers["Last-Modified"], static_headers["Last-Modified"])
            self.assertNotIn("Content-Encoding", headers)
            self.assert_security_headers(headers)

    def test_source_cache_reuses_then_invalidates_change_and_delete(self):
        root = self.tempdir()
        fees = root / "fees.json"
        fees.write_text('{"maker":"0.01"}\n', encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources["fees"] = fees
        base = self.start(sources)

        with mock.patch("builtins.open", wraps=open) as opened:
            status, first_headers, first_body = self.get(base, "/api/fees")
            self.assertEqual(status, 200)
            status, second_headers, second_body = self.get(base, "/api/fees")
            self.assertEqual(status, 200)
            self.assertEqual(second_body, first_body)
            self.assertEqual(second_headers["ETag"], first_headers["ETag"])
            source_opens = [
                call for call in opened.call_args_list
                if call.args and os.fspath(call.args[0]) == os.fspath(fees)]
            self.assertEqual(len(source_opens), 1)

            replacement = root / "fees.next"
            replacement.write_text(
                '{"maker":"0.0125","taker":"0.02"}\n', encoding="utf-8")
            os.replace(replacement, fees)
            status, changed_headers, changed_body = self.get(
                base, "/api/fees")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(changed_body)["rates"]["maker"],
                             "0.0125")
            self.assertNotEqual(changed_headers["ETag"],
                                first_headers["ETag"])
            source_opens = [
                call for call in opened.call_args_list
                if call.args and os.fspath(call.args[0]) == os.fspath(fees)]
            self.assertEqual(len(source_opens), 2)

            fees.unlink()
            status, deleted_headers, deleted_body = self.get(
                base, "/api/fees")
            self.assertEqual(status, 200)
            deleted = json.loads(deleted_body)
            self.assertFalse(deleted["present"])
            self.assertIsNone(deleted["rates"])
            self.assertNotEqual(deleted_headers["ETag"],
                                changed_headers["ETag"])
            self.assertNotIn(os.path.abspath(os.fspath(fees)),
                             serve._SOURCE_PARSE_CACHE)

    def test_gzip_negotiation_vary_and_small_identity_response(self):
        root = self.tempdir()
        events = root / "events.jsonl"
        events.write_text(
            "".join(json.dumps({
                "ts": "2026-08-14T10:00:%02dZ" % (index % 60),
                "kind": "measurement", "value": "x" * 40,
            }) + "\n" for index in range(40)), encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources["events"] = events
        base = self.start(sources)

        status, identity_headers, identity_body = self.get(
            base, "/api/events")
        self.assertEqual(status, 200)
        self.assertNotIn("Content-Encoding", identity_headers)
        self.assertEqual(identity_headers["Vary"], "Accept-Encoding")
        self.assertEqual(int(identity_headers["Content-Length"]),
                         len(identity_body))

        status, gzip_headers, gzip_body = self.get(
            base, "/api/events", headers={"Accept-Encoding": "br, gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(gzip_headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip_headers["Vary"], "Accept-Encoding")
        self.assertEqual(int(gzip_headers["Content-Length"]), len(gzip_body))
        self.assertEqual(gzip.decompress(gzip_body), identity_body)
        self.assertNotEqual(gzip_headers["ETag"], identity_headers["ETag"])
        self.assertEqual(
            gzip_headers["ETag"],
            '"%s"' % hashlib.sha256(gzip_body).hexdigest())

        status, disabled_headers, disabled_body = self.get(
            base, "/api/events", headers={"Accept-Encoding": "gzip;q=0"})
        self.assertEqual(status, 200)
        self.assertNotIn("Content-Encoding", disabled_headers)
        self.assertEqual(disabled_body, identity_body)

        status, small_headers, small_body = self.get(
            base, "/api/fees", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertLessEqual(len(small_body), serve.GZIP_MIN_BYTES)
        self.assertNotIn("Content-Encoding", small_headers)
        self.assertEqual(small_headers["Vary"], "Accept-Encoding")

    def test_security_headers_cover_static_and_api(self):
        root = self.tempdir()
        base = self.start(self.empty_sources(root / "missing"))
        for path in ("/index.html", "/api/fees"):
            status, headers, _ = self.get(base, path)
            self.assertEqual(status, 200)
            self.assert_security_headers(headers)
            self.assertNotIn("Python", headers["Server"])

    def test_405_allow_and_all_unknown_methods_are_refused(self):
        self.start({})
        for method in ("POST", "PROPFIND"):
            status, headers, body = self.request_full(method, "/api/fees")
            self.assertEqual(status, 405)
            self.assertEqual(headers["Allow"], "GET, HEAD")
            self.assertEqual(int(headers["Content-Length"]), len(body))
            self.assertIn(b"read-only", body)
            self.assert_security_headers(headers)

    def test_head_headers_match_get_including_gzip_and_304(self):
        root = self.tempdir()
        fees = root / "fees.json"
        fees.write_text('{"maker":"0.01"}\n', encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources["fees"] = fees
        base = self.start(sources)
        request_headers = {"Accept-Encoding": "gzip"}

        for path in ("/index.html", "/api/fees"):
            get_status, get_headers, get_body = self.get(
                base, path, headers=request_headers)
            head_status, head_headers, head_body = self.request_full(
                "HEAD", path, headers=request_headers)
            self.assertEqual((get_status, head_status), (200, 200))
            self.assertEqual(head_body, b"")
            self.assertEqual(int(get_headers["Content-Length"]), len(get_body))
            for name in ("Content-Type", "Content-Length", "Cache-Control",
                         "ETag", "Vary"):
                self.assertEqual(head_headers.get(name), get_headers.get(name),
                                 "%s %s" % (path, name))
            self.assertEqual(head_headers.get("Content-Encoding"),
                             get_headers.get("Content-Encoding"))

            conditional = dict(request_headers)
            conditional["If-None-Match"] = get_headers["ETag"]
            status, headers, body = self.request_full(
                "HEAD", path, headers=conditional)
            self.assertEqual(status, 304)
            self.assertEqual(body, b"")
            self.assertEqual(headers["Content-Length"], "0")
            self.assertEqual(headers["ETag"], get_headers["ETag"])
            self.assertEqual(headers["Cache-Control"],
                             get_headers["Cache-Control"])
            self.assertNotIn("Content-Encoding", headers)

    def test_error_types_lengths_timeout_and_nul_traversal(self):
        self.start({})
        for path in ("/api/no-such-route", "/api%2Fno-such-route"):
            status, headers, body = self.request_full("GET", path)
            self.assertEqual(status, 404)
            self.assertIn("application/json", headers["Content-Type"])
            self.assertEqual(int(headers["Content-Length"]), len(body))
            self.assertEqual(json.loads(body)["error"], "not found")

        for path in ("/no-such-file", "/%00", "//["):
            status, headers, body = self.request_full("GET", path)
            self.assertEqual(status, 404)
            self.assertIn("text/plain", headers["Content-Type"])
            self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertEqual(
            self.server.RequestHandlerClass.timeout,
            serve.REQUEST_TIMEOUT_SECONDS)
        self.assertTrue(self.server.RequestHandlerClass.quiet)

    def test_cli_quiet_default_and_verbose_opt_in(self):
        for arguments, expected_quiet in ((["--port", "0"], True),
                                          (["--port", "0", "--verbose"],
                                           False)):
            fake_server = mock.Mock()
            fake_server.server_address = ("127.0.0.1", 43210)
            with mock.patch.object(
                    serve, "make_server", return_value=fake_server) as build, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(serve.main(arguments), 0)
            self.assertEqual(build.call_args.kwargs["quiet"], expected_quiet)
            fake_server.serve_forever.assert_called_once_with()
            fake_server.server_close.assert_called_once_with()


class TestEventsHistoryEndpoint(ServerHarness):

    def test_present_is_verbatim_and_explicitly_historical(self):
        root = self.tempdir()
        events = root / "events.jsonl"
        events.write_text("\n".join(EVENT_LINES) + "\n", encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources["events"] = events
        payload = self.get_json(self.start(sources), "/api/events")
        self.assertTrue(payload["present"])
        self.assertIs(payload["historical"], True)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["events"][0]["balances"]["USDT"],
                         "95.62398534")
        self.assertEqual(payload["events"][1]["verdict"],
                         "REJECTED_BEFORE_BUY")
        self.assertIsNotNone(payload["as_of"])

    def test_absent_is_honest_empty(self):
        root = self.tempdir()
        payload = self.get_json(
            self.start(self.empty_sources(root / "missing")), "/api/events")
        self.assertFalse(payload["present"])
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["count"], 0)
        self.assertIsNone(payload["as_of"])


class TestStreamEndpoint(ServerHarness):

    def test_top_surge_union_all_both_venue_gaps_and_as_of(self):
        root = self.tempdir()
        state_path = root / "stream_state.json"
        state_path.write_text(json.dumps({
            "BTCUSDT": stream_row(["spot", "perp"], "9.25", "0.40"),
            "ETHUSDT": stream_row(["perp"], "7.50", None, spot=False),
            "APRUSDT": stream_row(["spot", "perp"], "1.10", "0.62"),
        }), encoding="utf-8")
        log_path = root / "stream_log.jsonl"
        log_path.write_text(
            '{"ts":"2026-08-13T10:00:01.250Z","kind":"stream_tick",'
            '"symbol":"APRUSDT"}\n', encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources.update({"stream_state": state_path, "stream_log": log_path})
        base = self.start(sources)
        with mock.patch.object(serve, "STREAM_TOP_N", 1):
            payload = self.get_json(base, "/api/stream")

        self.assertTrue(payload["present"])
        self.assertTrue(payload["log_present"])
        self.assertEqual(payload["as_of"], "2026-08-13T10:00:01.250Z")
        self.assertEqual(payload["top_surge_count"], 1)
        self.assertEqual(payload["basis_gap_count"], 2)
        self.assertEqual([row["symbol"] for row in payload["tokens"]],
                         ["BTCUSDT", "APRUSDT"])
        btc, apr = payload["tokens"]
        self.assertEqual(btc["selection"], ["top_surge", "basis_gap"])
        self.assertEqual(apr["selection"], ["basis_gap"])
        self.assertEqual(btc["data"]["surge_ratio"], "9.25")
        self.assertEqual(apr["data"]["basis_gap_pct"], "0.62")
        self.assertEqual(apr["data"]["next_funding_ms"], 1786608000000)
        self.assertEqual(payload["parse_errors"], 0)

    def test_absent_state_and_log_are_honest_empty_and_not_created(self):
        root = self.tempdir()
        missing = root / "missing"
        payload = self.get_json(
            self.start(self.empty_sources(missing)), "/api/stream")
        self.assertFalse(payload["present"])
        self.assertFalse(payload["log_present"])
        self.assertEqual(payload["tokens"], [])
        self.assertEqual(payload["count"], 0)
        self.assertIsNone(payload["as_of"])
        self.assertFalse(missing.exists())

    def test_malformed_state_is_present_but_never_invented(self):
        root = self.tempdir()
        state_path = root / "stream_state.json"
        state_path.write_text("{torn", encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources["stream_state"] = state_path
        payload = self.get_json(self.start(sources), "/api/stream")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["tokens"], [])
        self.assertEqual(payload["state_parse_errors"], 1)


class TestCandidatesEndpoint(ServerHarness):

    def test_only_complete_positive_unexpired_proposals_are_served(self):
        root = self.tempdir()
        directory = root / "proposals"
        directory.mkdir()
        live = proposal()
        (directory / "proposal-live01.json").write_text(
            json.dumps(live), encoding="utf-8")
        (directory / "proposal-expired.json").write_text(
            json.dumps(proposal("expired", "2000-01-01T00:00:00Z")),
            encoding="utf-8")
        invalid = proposal("invalid")
        del invalid["funding_rate"]
        (directory / "proposal-invalid.json").write_text(
            json.dumps(invalid), encoding="utf-8")
        (directory / "proposal-torn.json").write_text(
            "{torn", encoding="utf-8")
        (directory / "proposal-live01.claim").write_text(
            "not a proposal", encoding="utf-8")

        sources = self.empty_sources(root / "missing")
        sources["proposals_dir"] = directory
        payload = self.get_json(self.start(sources), "/api/candidates")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["scanned_count"], 4)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["expired_count"], 1)
        self.assertEqual(payload["invalid_count"], 2)
        self.assertEqual(payload["parse_errors"], 1)
        served = payload["candidates"][0]
        self.assertEqual(served["file"], "proposal-live01.json")
        self.assertEqual(served["proposal"], live)
        self.assertEqual(served["proposal"]["net_edge_pct"], "0.02503117")
        self.assertEqual(set(served["proposal"]),
                         set(serve.PROPOSAL_REQUIRED_FIELDS))

    def test_absent_directory_is_honest_empty(self):
        root = self.tempdir()
        missing = root / "missing"
        payload = self.get_json(
            self.start(self.empty_sources(missing)), "/api/candidates")
        self.assertFalse(payload["present"])
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["count"], 0)
        self.assertFalse(missing.exists())


class TestPositionEndpoint(ServerHarness):

    def test_current_state_and_supervision_are_served_verbatim(self):
        root = self.tempdir()
        path = root / "basis-position.json"
        position = {
            "version": 1,
            "updated_utc": "2026-08-13T10:01:00.000Z",
            "state": "OPEN",
            "verdict": "OPEN",
            "symbol": "APRUSDT",
            "entry": {"quantity": "220.00000000", "gap_pct": "0.51"},
            "last_check": {
                "checked_utc": "2026-08-13T10:00:59.900Z",
                "directive": "HOLD",
                "current_gap_pct": "0.43",
                "freshness_ms": 210,
            },
            "reconcile": {"status": "PASS", "recommendations": []},
        }
        path.write_text(json.dumps(position), encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources["basis_position"] = path
        payload = self.get_json(self.start(sources), "/api/position")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["position"], position)
        self.assertEqual(payload["as_of"], position["updated_utc"])
        self.assertEqual(payload["supervision"]["last_check"]["directive"],
                         "HOLD")
        self.assertEqual(payload["supervision"]["reconcile"]["status"],
                         "PASS")

    def test_absent_position_is_honest_empty(self):
        root = self.tempdir()
        payload = self.get_json(
            self.start(self.empty_sources(root / "missing")), "/api/position")
        self.assertFalse(payload["present"])
        self.assertIsNone(payload["position"])
        self.assertIsNone(payload["supervision"])
        self.assertIsNone(payload["as_of"])


class TestMomentumEndpoint(ServerHarness):

    def test_open_position_receipts_and_runner_ledgers_are_file_rows(self):
        root = self.tempdir()
        state_path = root / "momentum-position.json"
        state = {
            "version": 1, "revision": 3,
            "updated_utc": "2026-08-13T10:02:00.000Z",
            "state": "OPEN", "symbol": "APRUSDT", "job_id": "open1",
            "pending_intent": None, "measured_qty": "110.00000000",
            "entry": {
                "qty": "110.00000000", "price": "0.04510",
                "quote": "4.961", "fees": [],
            },
        }
        state_path.write_text(json.dumps(state), encoding="utf-8")
        receipts_path = root / "momentum_paper_receipts.jsonl"
        receipts = [
            {"ts": "2026-08-13T10:01:10.000Z",
             "kind": "momentum_open_decision", "runner_truth": True,
             "decision_only": True, "symbol": "APRUSDT",
             "qty": "55.00000000",
             "price": "0.04510"},
            {"ts": "2026-08-13T10:01:40.000Z",
             "kind": "momentum_add_decision", "runner_truth": True,
             "decision_only": True, "symbol": "APRUSDT",
             "qty": "55.00000000",
             "price": "0.04550", "surge_ratio": "6.20"},
        ]
        receipts_path.write_text(
            "\n".join(json.dumps(row) for row in receipts) + "\nnot json\n",
            encoding="utf-8")
        results_path = root / "results.jsonl"
        opened = {
            "job_id": "momentum-open1", "op": "MOMENTUM_OPEN",
            "symbol": "APRUSDT", "verdict": "OPEN", "exit_code": 0,
            "receipt_path": "/receipts/job-momentum-open1.json",
            "utc": "2026-08-13T10:02:05.000Z",
            "fills": [{"side": "BUY", "qty": "110.00000000"}],
            "fees": [],
            "realized": {"gross_usdt": "UNKNOWN", "net_usdt": "UNKNOWN"},
        }
        closed = {
            "job_id": "momentum-exit1", "op": "MOMENTUM_EXIT",
            "symbol": "APRUSDT", "verdict": "CLOSED", "exit_code": 0,
            "receipt_path": "/receipts/job-momentum-exit1.json",
            "utc": "2026-08-13T10:02:10.000Z",
            "fills": [{"side": "SELL", "qty": "110.00000000"}],
            "fees": [{"asset": "USDT", "amount": "0.001"}],
            "realized": {"gross_usdt": "0.120", "net_usdt": "0.119"},
        }
        results_path.write_text(
            json.dumps({
                "job_id": "basis1", "op": "BASIS_ROUNDTRIP",
                "symbol": "BTCUSDT", "verdict": "CLOSED",
                "utc": "2026-08-13T10:02:01.000Z",
            }) + "\n" + json.dumps(opened) + "\n" + json.dumps(closed) +
            "\n" + json.dumps({
                "job_id": "momentum-bad", "op": "MOMENTUM_OPEN",
                "symbol": "ETHUSDT", "verdict": "UNKNOWN",
                "utc": "2026-08-13T10:02:15.000Z",
            }) + "\n[]\n{torn\n", encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources.update({"momentum_state": state_path,
                        "momentum_receipts": receipts_path,
                        "job_results": results_path})
        payload = self.get_json(self.start(sources), "/api/momentum")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["state"]["state"], state)
        self.assertEqual(
            payload["state"]["positions"]["APRUSDT"]["state"], "OPEN")
        self.assertEqual(payload["receipts"]["count"], 2)
        self.assertEqual(payload["receipts"]["records"], receipts)
        self.assertEqual(payload["receipts"]["parse_errors"], 1)
        self.assertEqual(payload["realized"]["records"], [opened, closed])
        self.assertEqual(payload["realized"]["count"], 2)
        self.assertEqual(payload["realized"]["parse_errors"], 3)
        self.assertEqual(payload["as_of"], closed["utc"])

    def test_absent_momentum_sources_are_honest_empty(self):
        root = self.tempdir()
        payload = self.get_json(
            self.start(self.empty_sources(root / "missing")), "/api/momentum")
        self.assertFalse(payload["present"])
        self.assertFalse(payload["state"]["present"])
        self.assertEqual(payload["state"]["positions"], {})
        self.assertFalse(payload["receipts"]["present"])
        self.assertEqual(payload["receipts"]["records"], [])
        self.assertFalse(payload["realized"]["present"])
        self.assertEqual(payload["realized"]["records"], [])
        self.assertIsNone(payload["as_of"])


class TestFeesEndpoint(ServerHarness):

    def test_commission_rates_are_served_verbatim(self):
        root = self.tempdir()
        path = root / "commission-rates.json"
        rates = {
            "symbol": "APRUSDT",
            "updated_utc": "2026-08-13T10:03:00.000Z",
            "spot": {"maker": "0.0008", "taker": "0.00075"},
            "usdm": {"maker": "0.0002", "taker": "0.0004"},
        }
        path.write_text(json.dumps(rates), encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources["fees"] = path
        payload = self.get_json(self.start(sources), "/api/fees")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["rates"], rates)
        self.assertEqual(payload["as_of"], rates["updated_utc"])
        self.assertEqual(payload["rates"]["spot"]["taker"], "0.00075")

    def test_absent_fees_are_honest_empty(self):
        root = self.tempdir()
        payload = self.get_json(
            self.start(self.empty_sources(root / "missing")), "/api/fees")
        self.assertFalse(payload["present"])
        self.assertIsNone(payload["rates"])
        self.assertIsNone(payload["as_of"])


class TestPnlEndpoint(ServerHarness):

    def test_closed_and_reconcile_pnl_blocks_include_partial_fee_truth(self):
        root = self.tempdir()
        receipts_dir = root / "receipts"
        receipts_dir.mkdir()
        partial_pnl = {
            "spot_quote_pnl_usdt": "0.12000000",
            "short_quote_pnl_usdt": "-0.03000000",
            "spot_fees_usdt": "0.01000000",
            "usdm_commission_usdt": "-0.00400000",
            "funding_usdt": "0.00200000",
            "total_usdt": "0.07800000",
            "total_usdt_status": "PARTIAL_FEES_EXCLUDED",
            "excluded_spot_fees_by_asset": {"BNB": "0.00000120"},
        }
        complete_pnl = {
            "total_usdt": "-0.01400000", "fees_complete": True,
        }
        receipts = {
            "basis-executor-closed.json": {
                "kind": "basis_cycle", "verdict": "CLOSED",
                "run_id": "closed01", "symbol": "APRUSDT",
                "finished_utc": "2026-08-13T10:04:00.000Z",
                "pnl": partial_pnl,
            },
            "basis-executor-reconcile.json": {
                "kind": "reconcile", "verdict": "RECONCILED",
                "run_id": "recon01", "symbol": "BTCUSDT",
                "finished_utc": "2026-08-13T10:05:00.000Z",
                "result": {"pnl": complete_pnl},
            },
            "basis-executor-open.json": {
                "kind": "basis_cycle", "verdict": "OPEN_EXPOSURE",
                "run_id": "open01", "pnl": {"total_usdt": "999"},
            },
            "basis-position-no-pnl.json": {
                "kind": "position-close", "verdict": "CLOSED",
                "run_id": "nopnl01",
            },
        }
        for name, value in receipts.items():
            (receipts_dir / name).write_text(
                json.dumps(value), encoding="utf-8")
        (receipts_dir / "basis-executor-torn.json").write_text(
            "{torn", encoding="utf-8")
        (receipts_dir / "unrelated.json").write_text(
            json.dumps({"pnl": {"total_usdt": "500"}}), encoding="utf-8")

        sources = self.empty_sources(root / "missing")
        sources["basis_receipts_dir"] = receipts_dir
        payload = self.get_json(self.start(sources), "/api/pnl")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["scanned_receipts"], 5)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["parse_errors"], 1)
        by_run = {row["run_id"]: row for row in payload["records"]}
        self.assertEqual(by_run["closed01"]["pnl"], partial_pnl)
        self.assertTrue(by_run["closed01"]["partial_fees"])
        self.assertEqual(
            by_run["closed01"]["partial_fee_flags"]
            ["excluded_spot_fees_by_asset"]["BNB"], "0.00000120")
        self.assertEqual(by_run["recon01"]["pnl"], complete_pnl)
        self.assertFalse(by_run["recon01"]["partial_fees"])
        self.assertNotIn("open01", by_run)

    def test_closed_position_state_explicit_pnl_is_also_visible(self):
        root = self.tempdir()
        position_path = root / "basis-position.json"
        pnl = {
            "total_usdt": "-0.02757000",
            "total_usdt_status": "PARTIAL_FEES_EXCLUDED",
            "excluded_spot_fees_by_asset": {"BNB": "0.00042000"},
        }
        position_path.write_text(json.dumps({
            "state": "CLOSED", "verdict": "CLOSED", "run_id": "pos01",
            "symbol": "BTCUSDT", "updated_utc": "2026-08-13T10:06:00Z",
            "close": {"closed_utc": "2026-08-13T10:06:00Z", "pnl": pnl},
        }), encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources["basis_position"] = position_path
        payload = self.get_json(self.start(sources), "/api/pnl")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["records"][0]["pnl"], pnl)
        self.assertTrue(payload["records"][0]["partial_fees"])

    def test_absent_pnl_sources_are_honest_empty(self):
        root = self.tempdir()
        missing = root / "missing"
        payload = self.get_json(
            self.start(self.empty_sources(missing)), "/api/pnl")
        self.assertFalse(payload["present"])
        self.assertEqual(payload["records"], [])
        self.assertEqual(payload["count"], 0)
        self.assertIsNone(payload["as_of"])
        self.assertFalse(missing.exists())


class TestHealthEndpoint(ServerHarness):

    def test_present_sources_aggregate_latest_ticks_log_flags_and_ages(self):
        root = self.tempdir()
        now = datetime.now(timezone.utc)
        old = (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        fresh = (now - timedelta(seconds=4)).isoformat().replace("+00:00", "Z")
        health_path = root / "health.jsonl"
        rows = [
            {"service": "market-stream", "status": "ERROR", "utc": old},
            {"service": "market-stream", "status": "OK", "utc": fresh,
             "cadence_seconds": 10},
            {"service": "momentum", "status": "DEGRADED", "utc": fresh},
        ]
        health_path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n{torn\n",
            encoding="utf-8")
        jobs_log = root / "jobs.log"
        jobs_log.write_text(
            "=== %s\nJOB-RUNNER | IDLE | no due unclaimed job\n" % fresh,
            encoding="utf-8")
        stop = root / "STOP"
        stop.touch()

        sources = self.empty_sources(root / "missing")
        sources.update({
            "health": health_path,
            "jobs_log": jobs_log,
            "stop": stop,
            "stop_jobs": root / "STOP-JOBS",
        })
        payload = self.get_json(self.start(sources), "/api/health")

        self.assertTrue(payload["present"])
        self.assertEqual(payload["service_count"], 2)
        services = {row["service"]: row for row in payload["services"]}
        self.assertEqual(services["market-stream"]["level"], "green")
        self.assertEqual(services["momentum"]["level"], "amber")
        self.assertGreaterEqual(
            services["market-stream"]["last_seen_age_seconds"], 0)
        self.assertLess(
            services["market-stream"]["last_seen_age_seconds"], 20)
        self.assertEqual(payload["jobs_log"]["level"], "green")
        self.assertIn("JOB-RUNNER | IDLE", payload["jobs_log"]["tail"][-1])
        self.assertTrue(payload["flags"]["STOP"]["present"])
        self.assertFalse(payload["flags"]["STOP-JOBS"]["present"])
        self.assertTrue(payload["stop"])
        self.assertFalse(payload["stop_jobs"])
        self.assertEqual(payload["system_level"], "red")
        self.assertEqual(payload["parse_errors"], 1)

    def test_absent_health_sources_are_honest_empty_and_not_created(self):
        root = self.tempdir()
        missing = root / "missing"
        payload = self.get_json(
            self.start(self.empty_sources(missing)), "/api/health")
        self.assertFalse(payload["present"])
        self.assertIsNone(payload["system_level"])
        self.assertEqual(payload["services"], [])
        self.assertEqual(payload["last_tick_ages"], {})
        self.assertFalse(payload["jobs_log"]["present"])
        self.assertFalse(payload["flags"]["STOP"]["present"])
        self.assertFalse(payload["flags"]["STOP-JOBS"]["present"])
        self.assertFalse(missing.exists())


class TestDecisionsEndpoint(ServerHarness):

    def test_present_tail_preserves_decisions_and_signal_evidence(self):
        root = self.tempdir()
        path = root / "momentum-decisions.jsonl"
        decisions = [
            {
                "decision_utc": "2026-08-14T10:00:00Z", "symbol": "APRUSDT",
                "action": "WATCH", "reason": "ignition baseline",
                "signal_evidence": {
                    "surge_ratio": "9.40", "oi_change_pct": "1.20",
                    "freshness_ms": "180",
                },
            },
            {
                "decision_utc": "2026-08-14T10:00:02Z", "symbol": "APRUSDT",
                "action": "ENTER_SMALL", "reason": "persistence confirmed",
                "surge_ratio": "10.20", "vol_1m_quote": "95000.00",
            },
        ]
        path.write_text(
            json.dumps(decisions[0]) + "\n[]\n{torn\n" +
            json.dumps(decisions[1]) + "\n", encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources["momentum_decisions"] = path

        payload = self.get_json(self.start(sources), "/api/decisions")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["decisions"], decisions)
        self.assertEqual(
            payload["decisions"][0]["signal_evidence"]["surge_ratio"],
            "9.40")
        self.assertEqual(payload["as_of"], decisions[-1]["decision_utc"])
        self.assertEqual(payload["parse_errors"], 2)

    def test_absent_decisions_are_honest_empty_and_not_created(self):
        root = self.tempdir()
        missing = root / "missing"
        payload = self.get_json(
            self.start(self.empty_sources(missing)), "/api/decisions")
        self.assertFalse(payload["present"])
        self.assertEqual(payload["decisions"], [])
        self.assertEqual(payload["count"], 0)
        self.assertIsNone(payload["as_of"])
        self.assertFalse(missing.exists())


class TestFailuresEndpoint(ServerHarness):

    def test_present_results_filter_failures_and_surface_recorded_reasons(self):
        root = self.tempdir()
        jobs = root / "jobs"
        receipts = jobs / "receipts"
        receipts.mkdir(parents=True)
        results = jobs / "results.jsonl"
        rows = [
            {"job_id": "done1", "symbol": "BTCUSDT", "verdict": "CLOSED",
             "utc": "2026-08-14T10:00:00Z"},
            {"job_id": "refused1", "symbol": "APRUSDT",
             "verdict": "REFUSED", "reason": "SEQUENCE_LOCKED",
             "utc": "2026-08-14T10:01:00Z"},
            {"job_id": "preflight1", "symbol": "SOLUSDT",
             "verdict": "PREFLIGHT_REFUSED",
             "receipt_path": str(root / "basis-executor-preflight1.json"),
             "utc": "2026-08-14T10:01:30Z"},
            {"job_id": "unknown1", "symbol": "ETHUSDT",
             "verdict": "UNKNOWN", "utc": "2026-08-14T10:02:00Z"},
        ]
        results.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n{torn\n",
            encoding="utf-8")
        (receipts / "job-unknown1.json").write_text(json.dumps({
            "job_id": "unknown1", "verdict": "UNKNOWN",
            "reason": "LEVERAGE_UNPROVEN", "utc": rows[-1]["utc"],
        }), encoding="utf-8")
        (root / "basis-executor-preflight1.json").write_text(json.dumps({
            "verdict": "PREFLIGHT_REFUSED",
            "verdict_note": "actual leverage was not proven at 1x",
        }), encoding="utf-8")
        sources = self.empty_sources(root / "missing")
        sources.update({"job_results": results,
                        "job_receipts_dir": receipts,
                        "basis_receipts_dir": root})

        payload = self.get_json(self.start(sources), "/api/failures")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["count"], 3)
        by_job = {row["job_id"]: row for row in payload["failures"]}
        self.assertEqual(by_job["refused1"]["reason"], "SEQUENCE_LOCKED")
        self.assertEqual(by_job["refused1"]["reason_source"], "result")
        self.assertEqual(by_job["unknown1"]["reason"],
                         "LEVERAGE_UNPROVEN")
        self.assertEqual(by_job["unknown1"]["reason_source"],
                         "runner_receipt")
        self.assertEqual(by_job["preflight1"]["reason"],
                         "actual leverage was not proven at 1x")
        self.assertEqual(by_job["preflight1"]["reason_source"],
                         "receipt_path")
        self.assertEqual(payload["as_of"], rows[-1]["utc"])
        self.assertEqual(payload["parse_errors"], 1)

    def test_absent_results_are_honest_empty_and_not_created(self):
        root = self.tempdir()
        missing = root / "missing"
        payload = self.get_json(
            self.start(self.empty_sources(missing)), "/api/failures")
        self.assertFalse(payload["present"])
        self.assertEqual(payload["failures"], [])
        self.assertEqual(payload["count"], 0)
        self.assertIsNone(payload["as_of"])
        self.assertFalse(missing.exists())


class TestStripEndpoint(ServerHarness):

    def test_no_pull_claims_nothing(self):
        root = self.tempdir()
        base = self.start(self.empty_sources(root / "missing"))
        payload = self.get_json(base, "/api/strip")
        self.assertEqual(payload["machine"], "NO-DATA")
        self.assertEqual(payload["market_pass"]["state"], "no-data")
        self.assertFalse(payload["pull"]["present"])
        self.assertEqual(payload["pull"]["level"], "absent")
        self.assertFalse(payload["governor"]["present"])
        self.assertIsNone(payload["supervisor"])

    def test_paused_with_fresh_pull_shows_sealed_never_blank(self):
        root = self.tempdir()
        sources = self.empty_sources(root / "missing")
        live = root / "live"
        live.mkdir()
        sources["manifest"] = live / "pull_manifest.json"
        sources["stop"] = live / "STOP"
        sources["stop_jobs"] = live / "STOP-JOBS"
        now = datetime.now(timezone.utc)
        sources["manifest"].write_text(json.dumps(
            {"utc": self.utc_text(now), "ok": True, "files": 3}))
        sources["stop"].touch()
        base = self.start(sources)
        payload = self.get_json(base, "/api/strip")
        self.assertEqual(payload["machine"], "PAUSED")
        self.assertTrue(payload["stop_present"])
        self.assertIn("sealed", payload["paused_note"])
        self.assertEqual(payload["market_pass"]["state"], "sealed")
        self.assertEqual(payload["pull"]["level"], "green")

    def test_armed_counts_governor_ledger(self):
        root = self.tempdir()
        sources = self.empty_sources(root / "missing")
        live = root / "live"
        live.mkdir()
        now = datetime.now(timezone.utc)
        sources["manifest"] = live / "pull_manifest.json"
        sources["manifest"].write_text(json.dumps(
            {"utc": self.utc_text(now), "ok": True}))
        sources["stop"] = live / "STOP"
        sources["stop_jobs"] = live / "STOP-JOBS"
        sources["governor_dir"] = live
        sources["governor_lock"] = live / "account-governor.lock"
        ledger = live / ("account-governor-%s.jsonl"
                         % now.date().isoformat())
        rows = [
            {"kind": "entry", "utc_day": now.date().isoformat()},
            {"kind": "entry", "utc_day": now.date().isoformat()},
            {"kind": "realized", "net_usdt": "-0.10", "fees_usdt": "0.02"},
            {"kind": "realized", "net_usdt": "0.30", "fees_usdt": "0.01"},
        ]
        ledger.write_text(
            "".join(json.dumps(row) + "\n" for row in rows))
        base = self.start(sources)
        payload = self.get_json(base, "/api/strip")
        self.assertEqual(payload["machine"], "ARMED")
        governor = payload["governor"]
        self.assertTrue(governor["present"])
        self.assertEqual(governor["entries_used"], 2)
        self.assertEqual(governor["entries_cap"], 5)
        self.assertEqual(governor["loss_fees_used_usdt"], "0.13")
        self.assertEqual(governor["loss_fees_remaining_usdt"], "0.87")
        self.assertFalse(governor["entry_lock_present"])

    def test_slow_cycle_manifest_widens_thresholds(self):
        # A ~45s declared cycle makes a 75s age healthy green; without the
        # declaration the same age would be amber under the 45s default.
        root = self.tempdir()
        sources = self.empty_sources(root / "missing")
        live = root / "live"
        live.mkdir()
        aged = datetime.now(timezone.utc) - timedelta(seconds=75)
        sources["manifest"] = live / "pull_manifest.json"
        sources["manifest"].write_text(json.dumps(
            {"utc": self.utc_text(aged), "ok": True, "interval_ms": 45000}))
        base = self.start(sources)
        payload = self.get_json(base, "/api/strip")
        self.assertEqual(payload["pull"]["level"], "green")
        # cycle_ms >= 5s also counts as a cadence declaration.
        sources["manifest"].write_text(json.dumps(
            {"utc": self.utc_text(aged), "ok": True, "cycle_ms": 45000}))
        payload = self.get_json(base, "/api/strip")
        self.assertEqual(payload["pull"]["level"], "green")
        # A tiny cycle_ms is a copy DURATION, not a cadence — ignored.
        sources["manifest"].write_text(json.dumps(
            {"utc": self.utc_text(aged), "ok": True, "cycle_ms": 42}))
        payload = self.get_json(base, "/api/strip")
        self.assertEqual(payload["pull"]["level"], "amber")
        sources["manifest"].write_text(json.dumps(
            {"utc": self.utc_text(aged), "ok": True}))
        payload = self.get_json(base, "/api/strip")
        self.assertEqual(payload["pull"]["level"], "amber")

    def test_stale_or_failed_pull_goes_red(self):
        root = self.tempdir()
        sources = self.empty_sources(root / "missing")
        live = root / "live"
        live.mkdir()
        stale = datetime.now(timezone.utc) - timedelta(minutes=10)
        sources["manifest"] = live / "pull_manifest.json"
        sources["manifest"].write_text(json.dumps(
            {"utc": self.utc_text(stale), "ok": True}))
        base = self.start(sources)
        payload = self.get_json(base, "/api/strip")
        self.assertEqual(payload["pull"]["level"], "red")
        sources["manifest"].write_text(json.dumps(
            {"utc": self.utc_text(datetime.now(timezone.utc)),
             "ok": False, "error": "ssm session refused"}))
        payload = self.get_json(base, "/api/strip")
        self.assertEqual(payload["pull"]["level"], "red")
        self.assertEqual(payload["pull"]["error"], "ssm session refused")


class TestReadOnlyGuarantees(ServerHarness):

    def test_mutating_methods_all_answer_405_on_runtime_routes(self):
        self.start({})
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            for path in ("/api/health", "/api/decisions", "/api/failures",
                         "/api/stream", "/api/candidates", "/api/position",
                         "/api/momentum", "/api/fees", "/api/pnl"):
                status, body = self.request(method, path, body=b"{}")
                self.assertEqual(status, 405, "%s %s" % (method, path))
                self.assertIn(b"read-only", body)

    def test_health_reports_exact_read_only_sources(self):
        base = self.start({})
        payload = self.get_json(base, "/api/health")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["bind"], "127.0.0.1")
        self.assertEqual(payload["methods"], ["GET", "HEAD"])
        self.assertEqual(sorted(payload["sources"]), [
            "basis_position", "basis_receipts_dir", "events", "fees",
            "governor_dir", "governor_lock", "health", "job_receipts_dir",
            "job_results", "jobs_log", "manifest", "momentum_decisions",
            "momentum_receipts", "momentum_state", "news", "news_sources",
            "poll_config", "proposals_dir", "stop", "stop_jobs",
            "stream_log", "stream_state",
        ])

    def test_head_matches_status_and_has_empty_body(self):
        root = self.tempdir()
        base = self.start(self.empty_sources(root / "missing"))
        get_status, _, get_body = self.get(base, "/api/stream")
        self.assertEqual(get_status, 200)
        self.assertTrue(get_body)
        status, head_body = self.request("HEAD", "/api/stream")
        self.assertEqual(status, 200)
        self.assertEqual(head_body, b"")

    def test_get_endpoints_write_nothing(self):
        root = self.tempdir()
        missing = root / "nothing-here"
        base = self.start(self.empty_sources(missing))
        for path in ("/api/events", "/api/stream", "/api/candidates",
                     "/api/position", "/api/momentum", "/api/fees",
                     "/api/pnl", "/api/health", "/api/decisions",
                     "/api/failures"):
            self.get_json(base, path)
        self.assertEqual(list(root.iterdir()), [],
                         "GETs must never create a runtime source")


class TestHostCheck(unittest.TestCase):

    def test_rows_report_file_directory_absence_and_measured_age(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            present_file = root / "state.json"
            present_file.write_text("{}\n", encoding="utf-8")
            stamp = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
            os.utime(present_file, (stamp.timestamp(), stamp.timestamp()))
            directory = root / "jobs"
            directory.mkdir()
            rows = serve.host_check_rows({
                "present_file": present_file,
                "directory": directory,
                "missing": root / "missing.json",
            }, now=stamp + timedelta(seconds=12.345))
        by_source = {row["source"]: row for row in rows}
        self.assertEqual(by_source["present_file"]["status"], "present")
        self.assertEqual(by_source["present_file"]["kind"], "file")
        self.assertEqual(by_source["present_file"]["age_seconds"], 12.345)
        self.assertEqual(by_source["present_file"]["modified_utc"],
                         "2026-08-14T10:00:00.000Z")
        self.assertEqual(by_source["present_file"]["checked_utc"],
                         "2026-08-14T10:00:12.345Z")
        self.assertTrue(by_source["directory"]["exists"])
        self.assertEqual(by_source["directory"]["kind"], "directory")
        self.assertFalse(by_source["missing"]["exists"])
        self.assertIsNone(by_source["missing"]["age_seconds"])

    def test_host_check_flag_prints_jsonl_and_does_not_bind(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stream_state.json"
            path.write_text("{}\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(
                    serve, "default_sources",
                    return_value={"stream_state": Path(temporary) / "missing"}), \
                    mock.patch.object(serve, "make_server") as make_server, \
                    redirect_stdout(output):
                code = serve.main([
                    "--host-check", "--stream-state", str(path)])
        self.assertEqual(code, 0)
        make_server.assert_not_called()
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        row = next(item for item in rows
                   if item["source"] == "stream_state")
        self.assertEqual(row["source"], "stream_state")
        self.assertEqual(row["path"], str(path))
        self.assertTrue(row["exists"])
        self.assertEqual(row["kind"], "file")
        self.assertIsInstance(row["age_seconds"], float)
        self.assertEqual({
            item["source"] for item in rows
            if item["source"] in {
                "stream_windows", "basis_watch_state",
                "momentum_lifecycle_state",
            }}, {
                "stream_windows", "basis_watch_state",
                "momentum_lifecycle_state",
            })


class TestDefaultSources(unittest.TestCase):

    def test_defaults_read_only_the_pulled_live_mirror(self):
        # Owner-endorsed correction 2026-08-14: the UI never reads the
        # writer's runtime paths or the host; it reads only the ui/live/
        # mirror that the seated Lead's pull script maintains.
        old = os.environ.get("RECEIPT_DIR")
        old_jobs_log = os.environ.get("JOBS_LOG")
        try:
            os.environ["RECEIPT_DIR"] = "/tmp-example/quant-receipts"
            os.environ.pop("JOBS_LOG", None)
            sources = serve.default_sources()
            live = UI_DIR / "live"
            self.assertEqual(sources["stream_state"],
                             live / "stream_state.json")
            self.assertEqual(sources["stream_log"],
                             live / "stream_log.jsonl")
            self.assertEqual(sources["proposals_dir"], live / "proposals")
            self.assertEqual(sources["basis_position"],
                             live / "basis-position.json")
            self.assertEqual(sources["basis_receipts_dir"], live)
            self.assertEqual(sources["fees"],
                             live / "commission-rates.json")
            self.assertEqual(sources["momentum_state"],
                             live / "momentum-position.json")
            self.assertEqual(sources["momentum_receipts"],
                             live / "momentum-paper-receipts.jsonl")
            self.assertEqual(sources["health"], live / "health.jsonl")
            self.assertEqual(sources["jobs_log"], live / "jobs.log")
            self.assertEqual(sources["stop"], live / "STOP")
            self.assertEqual(sources["stop_jobs"], live / "STOP-JOBS")
            self.assertEqual(sources["momentum_decisions"],
                             live / "decisions.jsonl")
            self.assertEqual(sources["job_results"],
                             live / "jobs" / "results.jsonl")
            self.assertEqual(sources["job_receipts_dir"],
                             live / "jobs" / "receipts")
            self.assertEqual(sources["governor_dir"], live)
            self.assertEqual(sources["governor_lock"],
                             live / "account-governor.lock")
            self.assertEqual(sources["manifest"],
                             live / "pull_manifest.json")
            self.assertEqual(sources["events"],
                             UI_DIR / "fixtures" / "events.jsonl")
            for name, path in sources.items():
                if name == "events":
                    continue
                self.assertTrue(
                    str(path).startswith(str(live)),
                    "%s escapes ui/live/: %s" % (name, path))
        finally:
            if old is None:
                os.environ.pop("RECEIPT_DIR", None)
            else:
                os.environ["RECEIPT_DIR"] = old
            if old_jobs_log is None:
                os.environ.pop("JOBS_LOG", None)
            else:
                os.environ["JOBS_LOG"] = old_jobs_log

    def test_events_is_the_only_fixture_source(self):
        sources = serve.default_sources()
        fixture_sources = [name for name, path in sources.items()
                           if "fixtures" in path.parts]
        self.assertEqual(fixture_sources, ["events"])


class TestNewsEndpoint(ServerHarness):

    def test_news_absent_then_present(self):
        root = self.tempdir()
        sources = self.empty_sources(root / "missing")
        news_path = root / "news.jsonl"
        sources["news"] = news_path
        base = self.start(sources)
        payload = self.get_json(base, "/api/news")
        self.assertFalse(payload["present"])
        self.assertEqual(payload["count"], 0)
        self.assertFalse(payload["sources_board"]["present"])
        # file convention: ascending by time; the server returns
        # newest-first, so the FOMC line (latest) must come back first
        news_path.write_text(
            '{"id":"2","ts":"2026-08-15T05:01:00Z","source":"Wu",'
            '"title":"Listing update","url":"","impact":2,'
            '"category":"Exchange"}\n'
            'not json\n'
            '{"id":"1","ts":"2026-08-15T05:02:00Z","source":"Fed",'
            '"title":"FOMC rate decision","url":"https://example.org",'
            '"impact":3,"category":"Macro"}\n',
            encoding="utf-8")
        payload = self.get_json(base, "/api/news")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["parse_errors"], 1)
        self.assertEqual(payload["items"][0]["impact"], 3)
        self.assertIsNotNone(payload["as_of"])


class TestTelemetryEndpoint(ServerHarness):

    def telemetry_sources(self, root):
        sources = self.empty_sources(root / "missing")
        sources["telemetry"] = root / "telemetry.jsonl"
        return sources

    def test_range_filters_and_sorts_points_ascending(self):
        root = self.tempdir()
        now = datetime.now(timezone.utc)
        rows = [
            {"ts": self.utc_text(now - timedelta(minutes=15)), "value": 3},
            {"ts": self.utc_text(now - timedelta(hours=7)), "value": 1},
            {"ts": self.utc_text(now - timedelta(hours=5)), "value": 2},
            {"ts": self.utc_text(now + timedelta(minutes=1)), "value": 4},
        ]
        sources = self.telemetry_sources(root)
        sources["telemetry"].write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8")

        payload = self.get_json(
            self.start(sources), "/api/telemetry?range=6h")

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["read_only"])
        self.assertTrue(payload["present"])
        self.assertEqual(payload["range"], "6h")
        self.assertEqual([row["value"] for row in payload["points"]], [2, 3])
        self.assertEqual(payload["as_of"], rows[0]["ts"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["parse_errors"], 0)
        self.assertFalse(payload["truncated"])

    def test_downsamples_by_stride_and_always_keeps_newest(self):
        root = self.tempdir()
        now = datetime.now(timezone.utc)
        rows = [
            {"ts": self.utc_text(now - timedelta(seconds=1600 - index)),
             "value": index}
            for index in range(1601)
        ]
        sources = self.telemetry_sources(root)
        sources["telemetry"].write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8")

        payload = self.get_json(self.start(sources), "/api/telemetry")

        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["stride"], 2)
        self.assertEqual(payload["count"], 1601)
        self.assertEqual(len(payload["points"]), 801)
        self.assertEqual(payload["points"][0]["value"], 0)
        self.assertEqual(payload["points"][-1]["value"], 1600)

    def test_malformed_lines_and_unparseable_timestamps_are_counted(self):
        root = self.tempdir()
        now = datetime.now(timezone.utc)
        valid = {"ts": self.utc_text(now - timedelta(minutes=1)), "value": 7}
        sources = self.telemetry_sources(root)
        sources["telemetry"].write_text(
            "not json\n[]\n{\"value\":1}\n{\"ts\":\"junk\"}\n" +
            json.dumps(valid) + "\n", encoding="utf-8")

        payload = self.get_json(self.start(sources), "/api/telemetry")

        self.assertEqual(payload["points"], [valid])
        self.assertEqual(payload["parse_errors"], 4)

    def test_absent_file_is_honest_empty(self):
        root = self.tempdir()
        sources = self.telemetry_sources(root)

        payload = self.get_json(self.start(sources), "/api/telemetry")

        self.assertFalse(payload["present"])
        self.assertEqual(payload["source"], str(sources["telemetry"]))
        self.assertEqual(payload["points"], [])
        self.assertEqual(payload["count"], 0)
        self.assertIsNone(payload["as_of"])
        self.assertEqual(payload["parse_errors"], 0)
        self.assertFalse(payload["truncated"])

    def test_junk_range_defaults_to_one_hour(self):
        root = self.tempdir()
        now = datetime.now(timezone.utc)
        rows = [
            {"ts": self.utc_text(now - timedelta(hours=2)), "value": 1},
            {"ts": self.utc_text(now - timedelta(minutes=30)), "value": 2},
        ]
        sources = self.telemetry_sources(root)
        sources["telemetry"].write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8")

        payload = self.get_json(
            self.start(sources), "/api/telemetry?range=junk")

        self.assertEqual(payload["range"], "1h")
        self.assertEqual([row["value"] for row in payload["points"]], [2])


class TestPollConfigEndpoint(ServerHarness):
    """The server's one write: the recorder's per-metric poll config."""

    def poll_sources(self, root):
        return {"poll_config": root / "poll_config.json",
                "telemetry": root / "telemetry.jsonl"}

    def test_get_defaults_when_absent(self):
        root = self.tempdir()
        payload = self.get_json(
            self.start(self.poll_sources(root)), "/api/poll_config")
        self.assertFalse(payload["present"])
        self.assertEqual(payload["default_s"], 15)
        self.assertEqual(payload["metrics"]["server"], 15)
        self.assertIn(1, payload["allowed"])
        self.assertIn(2, payload["allowed"])
        self.assertIn(5, payload["allowed"])

    def test_post_roundtrip(self):
        root = self.tempdir()
        base = self.start(self.poll_sources(root))
        status, body = self.request(
            "POST", "/api/poll_config",
            body=json.dumps({"metrics": {"server": 1, "mirror": 60}}))
        self.assertEqual(status, 200)
        saved = json.loads(body)["saved"]
        self.assertEqual(saved["metrics"]["server"], 1)
        self.assertEqual(saved["metrics"]["mirror"], 60)
        payload = self.get_json(base, "/api/poll_config")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["metrics"]["server"], 1)
        self.assertEqual(payload["metrics"]["mirror"], 60)
        self.assertEqual(payload["metrics"]["market"], 15)

    def test_post_rejects_bad_values(self):
        root = self.tempdir()
        self.start(self.poll_sources(root))
        status, body = self.request(
            "POST", "/api/poll_config",
            body=json.dumps({"metrics": {"server": 7}}))
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])
        status, body = self.request(
            "POST", "/api/poll_config",
            body=json.dumps({"metrics": {"nope": 5}}))
        self.assertEqual(status, 400)

    def test_post_other_paths_still_refused(self):
        root = self.tempdir()
        self.start(self.poll_sources(root))
        status, _ = self.request("POST", "/api/strip", body="{}")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
