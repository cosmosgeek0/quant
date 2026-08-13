"""Deterministic offline tests for ui/serve.py — the read-only UI runtime.

Every test binds 127.0.0.1 on an OS-assigned port, drives the real server
over loopback HTTP, and never touches the network beyond loopback. Present
and absent source cases are both proven per endpoint, plus the read-only
guarantees: GET/HEAD only, no path traversal, and the server never creates
a missing source file or directory.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
sys.path.insert(0, str(UI_DIR))

import serve  # noqa: E402

# Real-shaped sample rows (field shapes match the recorded live data).
EVENT_LINES = [
    '{"ts":"2026-08-12T15:52:45Z","sha8":"d1b204bc","kind":"run_start",'
    '"run_id":"6a7c96cdb1754b","symbol":"BTCUSDT",'
    '"balances":{"BTC":"0.00000000","USDT":"95.62398534"},"note":"ceiling 5 USDT"}',
    '{"ts":"2026-08-12T15:52:46Z","sha8":"d1b204bc","kind":"run_verdict",'
    '"run_id":"6a7c96cdb1754b","symbol":"BTCUSDT","verdict":"REJECTED_BEFORE_BUY",'
    '"note":"approved 5 USDT too small"}',
]

BASIS_TEXT = (
    "entry_gap_quote_formula=usdm_bid-spot_ask\n"
    "exit_gap_quote_formula=usdm_ask-spot_bid\n"
    "entry_gap_bps_formula=entry_gap_quote/spot_ask*10000;bps_base=spot_ask\n"
    "exit_gap_bps_formula=exit_gap_quote/spot_bid*10000;bps_base=spot_bid\n"
    "completed_utc\tusdm_book_time_utc\tsymbol\tspot_bid\tspot_ask\tusdm_bid\t"
    "usdm_ask\tentry_gap_quote\tentry_gap_bps_vs_spot_ask\texit_gap_quote\t"
    "exit_gap_bps_vs_spot_bid\tfunding_rate\tnext_funding_utc\n"
    "2026-08-12T23:44:39.448Z\t2026-08-12T23:44:38.379Z\tBTCUSDT\t63389.99000000\t"
    "63390.00000000\t63379.90\t63380.00\t-10.10000000\t-1.59331125\t-9.99000000\t"
    "-1.57595860\t0.00006421\t2026-08-13T00:00:00.000Z\n"
    "2026-08-12T23:44:40.473Z\t2026-08-12T23:44:39.840Z\tETHUSDT\t1878.20000000\t"
    "1878.21000000\t1877.39\t1877.40\t-0.82000000\t-4.36585898\t-0.80000000\t"
    "-4.25939730\t0.00005433\t2026-08-13T00:00:00.000Z\n"
)

FUTURES_RECEIPT = {
    "run_id": "20260813-abcd",
    "open_client_id": "l2fs-20260813-abcd-o",
    "close_client_id": "l2fs-20260813-abcd-c",
    "sha": "0cffc896",
    "symbol": "BTCUSDT",
    "requested_max_notional": "10",
    "hard_ceiling_usdt": "10",
    "started_utc": "2026-08-13T00:00:00Z",
    "verdict": "REJECTED",
}

PAPER_LINES = [
    '{"kind": "paper_entry", "note": "paper only; no real order path", '
    '"paper": true, "paper_cost": "10.00000000", "price": "100", '
    '"qty": "0.10000000", "run_id": "paperdemo20260813", "seq": 0, '
    '"side": "BUY", "symbol": "BTCUSDT", "trail_pct": "0.05", '
    '"ts": "2026-08-12T23:46:04.453Z"}',
    '{"exit_reason": "trail_stop", "kind": "paper_exit", '
    '"note": "paper only; no real order path", "paper": true, '
    '"paper_pnl_quote": "0.40000000", "paper_proceeds": "10.40000000", '
    '"peak_price": "110", "price": "104", "qty": "0.10000000", '
    '"run_id": "paperdemo20260813", "seq": 4, "side": "SELL", '
    '"symbol": "BTCUSDT", "trail_stop_price": "104.50", '
    '"ts": "2026-08-12T23:46:04.453Z"}',
]


class ServerHarness(unittest.TestCase):
    """Starts the real server on 127.0.0.1:0 with per-test sources."""

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

    def get(self, base, path):
        try:
            with urllib.request.urlopen(base + path, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def get_json(self, base, path, expect_status=200):
        status, _, body = self.get(base, path)
        self.assertEqual(status, expect_status,
                         "unexpected status for %s: %s" % (path, status))
        return json.loads(body)

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def empty_sources(self, missing_root):
        """Sources that all point below one nonexistent directory."""
        return {
            "events": missing_root / "events.jsonl",
            "basis": missing_root / "basis.txt",
            "futures_dir": missing_root / "receipts",
            "volume_receipts": missing_root / "paper_receipts.jsonl",
            "volume_state": missing_root / "volume_state.json",
        }


class TestStaticServing(ServerHarness):

    def test_root_and_assets_serve(self):
        base = self.start({})
        for path, kind, marker in (
                ("/", "text/html", b"quant"),
                ("/index.html", "text/html", b'<button id="refresh"'),
                ("/app.js", "text/javascript", b"/api/events"),
                ("/style.css", "text/css", b"read-only"),
                ("/fixtures/events.jsonl", "text/plain", b'"run_id"')):
            status, headers, body = self.get(base, path)
            self.assertEqual(status, 200, path)
            self.assertIn(kind, headers.get("Content-Type", ""), path)
            self.assertIn(marker, body, path)

    def test_unknown_path_is_json_404(self):
        base = self.start({})
        status, _, body = self.get(base, "/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "not found")

    def test_traversal_refused_raw_and_encoded(self):
        self.start({})
        for path in ("/../TASK.md", "/%2e%2e/TASK.md", "/fixtures/../../TASK.md"):
            status, body = self.request("GET", path)
            self.assertEqual(status, 404, path)
            self.assertIn(b"not found", body, path)

    def test_binds_loopback_only(self):
        self.start({})
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


class TestEventsEndpoint(ServerHarness):

    def test_present_serves_real_rows_verbatim(self):
        root = self.tempdir()
        events = root / "events.jsonl"
        events.write_text("\n".join(EVENT_LINES) + "\n", encoding="utf-8")
        base = self.start({"events": events})
        payload = self.get_json(base, "/api/events")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["parse_errors"], 0)
        self.assertEqual(payload["source"], str(events))
        self.assertEqual(payload["events"][0]["kind"], "run_start")
        self.assertEqual(payload["events"][0]["balances"]["USDT"], "95.62398534")
        self.assertEqual(payload["events"][1]["verdict"], "REJECTED_BEFORE_BUY")

    def test_garbage_line_counted_never_invented(self):
        root = self.tempdir()
        events = root / "events.jsonl"
        events.write_text(EVENT_LINES[0] + "\nnot json at all\n"
                          + EVENT_LINES[1] + "\n", encoding="utf-8")
        base = self.start({"events": events})
        payload = self.get_json(base, "/api/events")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["parse_errors"], 1)
        kinds = [e["kind"] for e in payload["events"]]
        self.assertEqual(kinds, ["run_start", "run_verdict"])

    def test_absent_file_answers_honest_empty(self):
        root = self.tempdir()
        base = self.start(self.empty_sources(root / "missing"))
        payload = self.get_json(base, "/api/events")
        self.assertFalse(payload["present"])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["parse_errors"], 0)


class TestBasisEndpoint(ServerHarness):

    def test_present_parses_recorded_output(self):
        root = self.tempdir()
        basis = root / "basis.txt"
        basis.write_text(BASIS_TEXT, encoding="utf-8")
        base = self.start({"basis": basis})
        payload = self.get_json(base, "/api/basis")
        self.assertTrue(payload["present"])
        self.assertEqual(len(payload["formulas"]), 4)
        self.assertEqual(len(payload["columns"]), 13)
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["parse_errors"], 0)
        self.assertEqual(payload["rows"][0]["symbol"], "BTCUSDT")
        self.assertEqual(payload["rows"][1]["symbol"], "ETHUSDT")
        self.assertEqual(payload["rows"][0]["entry_gap_quote"], "-10.10000000")
        self.assertTrue(payload["text"].startswith("entry_gap_quote_formula"))

    def test_absent_file_answers_honest_empty(self):
        root = self.tempdir()
        base = self.start(self.empty_sources(root / "missing"))
        payload = self.get_json(base, "/api/basis")
        self.assertFalse(payload["present"])
        self.assertIsNone(payload["text"])
        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["columns"], [])


class TestFuturesEndpoint(ServerHarness):

    def test_present_receipts_and_bad_file_counted(self):
        root = self.tempdir()
        receipts = root / "receipts"
        receipts.mkdir()
        (receipts / "futures-hand-20260813-abcd.json").write_text(
            json.dumps(FUTURES_RECEIPT), encoding="utf-8")
        (receipts / "futures-hand-bad.json").write_text(
            "{broken", encoding="utf-8")
        (receipts / "futures-hand.lock").write_text("x", encoding="utf-8")
        (receipts / "unrelated.json").write_text("{}", encoding="utf-8")
        base = self.start({"futures_dir": receipts})
        payload = self.get_json(base, "/api/futures")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["count"], 2)  # lock + unrelated ignored
        self.assertEqual(payload["parse_errors"], 1)
        by_file = {r["file"]: r for r in payload["receipts"]}
        good = by_file["futures-hand-20260813-abcd.json"]
        self.assertEqual(good["receipt"]["verdict"], "REJECTED")
        self.assertEqual(good["receipt"]["hard_ceiling_usdt"], "10")
        bad = by_file["futures-hand-bad.json"]
        self.assertIsNone(bad["receipt"])
        self.assertEqual(bad["error"], "JSONDecodeError")

    def test_absent_dir_honest_and_never_created(self):
        root = self.tempdir()
        missing = root / "receipts-not-there"
        base = self.start({"futures_dir": missing})
        payload = self.get_json(base, "/api/futures")
        self.assertFalse(payload["present"])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["receipts"], [])
        self.assertFalse(missing.exists(),
                         "read-only server must never create the receipt dir")

    def test_empty_dir_is_present_with_zero(self):
        root = self.tempdir()
        receipts = root / "receipts"
        receipts.mkdir()
        base = self.start({"futures_dir": receipts})
        payload = self.get_json(base, "/api/futures")
        self.assertTrue(payload["present"])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["receipts"], [])


class TestVolumeEndpoint(ServerHarness):

    def test_receipts_present_state_absent(self):
        root = self.tempdir()
        paper = root / "paper_receipts.jsonl"
        paper.write_text("\n".join(PAPER_LINES) + "\n", encoding="utf-8")
        base = self.start({"volume_receipts": paper,
                           "volume_state": root / "volume_state.json"})
        payload = self.get_json(base, "/api/volume")
        receipts = payload["receipts"]
        self.assertTrue(receipts["present"])
        self.assertEqual(receipts["count"], 2)
        self.assertTrue(all(r["paper"] is True for r in receipts["records"]))
        self.assertEqual(receipts["records"][1]["exit_reason"], "trail_stop")
        state = payload["state"]
        self.assertFalse(state["present"])
        self.assertIsNone(state["state"])

    def test_state_present_served_verbatim(self):
        root = self.tempdir()
        paper = root / "paper_receipts.jsonl"
        paper.write_text(PAPER_LINES[0] + "\n", encoding="utf-8")
        state_file = root / "volume_state.json"
        state_file.write_text(
            '{"symbol": "BTCUSDT", "phase": "holding", "qty": "0.10000000"}',
            encoding="utf-8")
        base = self.start({"volume_receipts": paper,
                           "volume_state": state_file})
        payload = self.get_json(base, "/api/volume")
        self.assertTrue(payload["state"]["present"])
        self.assertEqual(payload["state"]["state"]["phase"], "holding")
        self.assertEqual(payload["state"]["state"]["qty"], "0.10000000")

    def test_absent_receipts_answer_honest_empty(self):
        root = self.tempdir()
        base = self.start(self.empty_sources(root / "missing"))
        payload = self.get_json(base, "/api/volume")
        self.assertFalse(payload["receipts"]["present"])
        self.assertEqual(payload["receipts"]["count"], 0)
        self.assertEqual(payload["receipts"]["records"], [])
        self.assertFalse(payload["state"]["present"])


class TestReadOnlyGuarantees(ServerHarness):

    def test_mutating_methods_all_answer_405(self):
        self.start({})
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            for path in ("/api/events", "/api/futures", "/index.html"):
                status, body = self.request(method, path, body=b"{}")
                self.assertEqual(status, 405, "%s %s" % (method, path))
                self.assertIn(b"read-only", body)

    def test_health_reports_read_only_and_sources(self):
        base = self.start({})
        payload = self.get_json(base, "/api/health")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["bind"], "127.0.0.1")
        self.assertEqual(payload["methods"], ["GET", "HEAD"])
        self.assertEqual(
            sorted(payload["sources"]),
            ["basis", "events", "futures_dir", "volume_receipts",
             "volume_state"])

    def test_head_matches_get_with_empty_body(self):
        base = self.start({})
        get_status, _, get_body = self.get(base, "/api/events")
        self.assertEqual(get_status, 200)
        status, head_body = self.request("HEAD", "/api/events")
        self.assertEqual(status, 200)
        self.assertEqual(head_body, b"")

    def test_get_endpoints_write_nothing_anywhere(self):
        root = self.tempdir()
        missing = root / "nothing-here"
        base = self.start(self.empty_sources(missing))
        for path in ("/api/events", "/api/basis", "/api/futures",
                     "/api/volume", "/api/health"):
            self.get_json(base, path)
        self.assertEqual(list(root.iterdir()), [],
                         "no source may be created by reads")


class TestDefaultSources(unittest.TestCase):

    def test_defaults_point_where_the_quant_writes(self):
        sources = serve.default_sources()
        repo = Path(__file__).resolve().parent.parent
        self.assertEqual(sources["events"],
                         repo / "ui" / "fixtures" / "events.jsonl")
        self.assertEqual(sources["basis"],
                         repo / "evidence" / "basis-read-live-run.txt")
        self.assertEqual(sources["volume_receipts"],
                         repo / "paper_receipts.jsonl")
        self.assertEqual(sources["volume_state"], repo / "volume_state.json")

    def test_futures_dir_honors_receipt_dir_env(self):
        old = os.environ.get("RECEIPT_DIR")
        try:
            os.environ["RECEIPT_DIR"] = "/tmp-example/receipts"
            self.assertEqual(serve.default_sources()["futures_dir"],
                             Path("/tmp-example/receipts"))
            del os.environ["RECEIPT_DIR"]
            self.assertEqual(serve.default_sources()["futures_dir"],
                             Path(os.path.expanduser("~/quant_receipts")))
        finally:
            if old is not None:
                os.environ["RECEIPT_DIR"] = old
            else:
                os.environ.pop("RECEIPT_DIR", None)


if __name__ == "__main__":
    unittest.main()
