from __future__ import annotations

"""Offline tests for the host's local mirror."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MIRROR_SCRIPT = ROOT / "ui" / "host" / "local_mirror.sh"
FEED_FIXTURE = ROOT / "tests" / "fixtures" / "network" / "feed_state_sample.json"
BASH = shutil.which("bash")
MIRRORED_FILES = (
    "health.jsonl",
    "decisions.jsonl",
    "stream_state.json",
    "basis-position.json",
    "momentum-position.json",
    "momentum-paper-receipts.jsonl",
    "commission-rates.json",
)


@unittest.skipUnless(BASH, "bash is required")
class TestNetworkMirror(unittest.TestCase):

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.receipts = self.root / "receipts"
        self.live = self.root / "live"
        self.run_dir = self.root / "run"
        self.receipts.mkdir()
        self.live.mkdir()
        self.run_dir.mkdir()
        self.feed_bytes = FEED_FIXTURE.read_bytes()

    def write_feed(self, payload=None):
        watch = self.receipts / "watch"
        watch.mkdir(exist_ok=True)
        (watch / "feed_state.json").write_bytes(
            self.feed_bytes if payload is None else payload)

    def run_mirror(self):
        environment = os.environ.copy()
        environment.update({
            "QUANT_MIRROR_R": str(self.receipts),
            "QUANT_MIRROR_L": str(self.live),
            "QUANT_MIRROR_D": str(self.run_dir),
            "QUANT_MIRROR_ONCE": "1",
        })
        result = subprocess.run(
            [BASH, str(MIRROR_SCRIPT)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def read_json(self, name):
        return json.loads((self.live / name).read_text(encoding="utf-8"))

    def test_complete_feed_is_copied_with_meta_and_manifest(self):
        self.write_feed()

        self.run_mirror()

        mirrored = (self.live / "feed_state.json").read_bytes()
        digest = hashlib.sha256(mirrored).hexdigest()
        meta = self.read_json("feed_state.meta.json")
        manifest = self.read_json("pull_manifest.json")
        self.assertEqual(mirrored, self.feed_bytes)
        self.assertEqual(meta["source"], "watch/feed_state.json")
        self.assertEqual(meta["source_bytes"], len(mirrored))
        self.assertEqual(meta["sha256"], digest)
        self.assertIsNone(meta["error"])
        self.assertEqual(manifest["errors"], [])
        self.assertTrue(manifest["ok"])
        self.assertEqual(manifest["files"], 1)

    def test_missing_feed_keeps_previous_copy_and_records_error(self):
        previous = b'{"previous":"good"}'
        (self.live / "feed_state.json").write_bytes(previous)

        self.run_mirror()

        meta = self.read_json("feed_state.meta.json")
        manifest = self.read_json("pull_manifest.json")
        self.assertEqual((self.live / "feed_state.json").read_bytes(), previous)
        self.assertIn("missing", meta["error"])
        self.assertEqual(manifest["errors"], [meta["error"]])
        self.assertFalse(manifest["ok"])
        self.assertEqual(manifest["files"], 0)

    def test_incomplete_feed_keeps_previous_copy_and_records_error(self):
        previous = b'{"previous":"good"}'
        truncated = self.feed_bytes[:len(self.feed_bytes) // 2]
        self.assertNotEqual(truncated[-1:], b"}")
        (self.live / "feed_state.json").write_bytes(previous)
        self.write_feed(truncated)

        self.run_mirror()

        meta = self.read_json("feed_state.meta.json")
        manifest = self.read_json("pull_manifest.json")
        self.assertEqual((self.live / "feed_state.json").read_bytes(), previous)
        self.assertIn("incomplete", meta["error"])
        self.assertEqual(manifest["errors"], [meta["error"]])
        self.assertFalse(manifest["ok"])

    def test_existing_files_and_stop_flags_keep_their_behavior(self):
        expected = {}
        for index, name in enumerate(MIRRORED_FILES):
            payload = ("row-%s\n" % index).encode("ascii")
            expected[name] = payload
            (self.receipts / name).write_bytes(payload)
        jobs = self.receipts / "jobs"
        jobs.mkdir()
        (jobs / "results.jsonl").write_bytes(b"result\n")
        (self.run_dir / "jobs.log").write_bytes(b"job\n")
        (self.receipts / "account-governor-a.jsonl").write_bytes(b"governor\n")
        (self.receipts / "STOP").touch()
        (self.live / "STOP-JOBS").touch()
        (self.live / "account-governor.lock").touch()
        self.write_feed()

        self.run_mirror()

        for name, payload in expected.items():
            self.assertEqual((self.live / name).read_bytes(), payload)
        self.assertEqual((self.live / "jobs" / "results.jsonl").read_bytes(), b"result\n")
        self.assertEqual((self.live / "jobs.log").read_bytes(), b"job\n")
        self.assertEqual((self.live / "account-governor-a.jsonl").read_bytes(), b"governor\n")
        self.assertTrue((self.live / "STOP").is_file())
        self.assertFalse((self.live / "STOP-JOBS").exists())
        self.assertFalse((self.live / "account-governor.lock").exists())
        self.assertEqual(self.read_json("pull_manifest.json")["files"], 10)


if __name__ == "__main__":
    unittest.main()
