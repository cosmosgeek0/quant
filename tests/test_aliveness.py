import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "aliveness.sh"


class AlivenessFeedEyesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.receipts = Path(self.temp.name)
        self.watch = self.receipts / "watch"
        self.watch.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def state(self, books=True, age_ms=0, status="healthy"):
        book = "1" if books else None
        venue = {"status": status, "ticker": {"status": status}}
        return {
            "written_ms": str(int(time.time() * 1000) - age_ms),
            "connections": {"spot": venue, "perp": venue},
            "symbols": {"AAAUSDT": {
                "spot_bid": book, "spot_ask": book,
                "perp_bid": book, "perp_ask": book,
            }},
        }

    def run_state(self, payload):
        (self.watch / "feed_state.json").write_text(json.dumps(payload))
        env = dict(os.environ, RECEIPT_DIR=str(self.receipts))
        subprocess.run(["sh", str(SCRIPT)], env=env, check=True)
        rows = [json.loads(line) for line in
                (self.receipts / "health.jsonl").read_text().splitlines()]
        eyes = [row for row in rows if row.get("stage") == "feed_eyes"]
        self.assertEqual(len(eyes), 1)
        return eyes[0]

    def test_blind_fresh_state_raises_alarm(self):
        row = self.run_state(self.state(books=False))
        self.assertEqual((row["spot_with_ticker"], row["perp_with_ticker"]), (0, 0))
        self.assertEqual(row["alarm"], "feed_blind")
        self.assertTrue((self.receipts / "ALARM-feed-blind").is_file())

    def test_healthy_state_reports_connection_facts(self):
        row = self.run_state(self.state())
        self.assertTrue(row["ok"])
        self.assertEqual((row["spot_status"], row["spot_ticker_status"]),
                         ("healthy", "healthy"))
        self.assertFalse((self.receipts / "ALARM-feed-blind").exists())

    def test_stale_state_raises_alarm(self):
        row = self.run_state(self.state(age_ms=181000))
        self.assertIn("file_stale", row["reasons"])

    def test_long_broken_status_raises_alarm(self):
        marker = self.watch / ".feed_eyes-spot-bad"
        marker.touch()
        old = time.time() - 901
        os.utime(marker, (old, old))
        row = self.run_state(self.state(status="broken"))
        self.assertIn("spot_status_long", row["reasons"])


if __name__ == "__main__":
    unittest.main()
