"""Offline framing and mirror tests for scripts/ui_pull.sh."""

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PULL_SCRIPT = ROOT / "scripts" / "ui_pull.sh"
UTC = "2026-08-14T00:00:00Z"


def file_block(relpath, payload, sha=None, close=True):
    digest = sha or hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode("ascii")
    lines = ["FILE %s %s %d" % (relpath, digest, len(payload))]
    lines.extend(encoded[index:index + 1800]
                 for index in range(0, len(encoded), 1800))
    if close:
        lines.append("ENDFILE %s" % relpath)
    return lines


def transcript(rows, count):
    return "\n".join(
        ["session noise", "LIVEPULL BEGIN %s" % UTC]
        + rows + ["LIVEPULL END %d" % count, "__SSMRUN_DONE__", ""])


class TestUiPull(unittest.TestCase):

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.live = self.root / "live"
        self.transcript_path = self.root / "transcript.txt"

    def run_pull(self, content):
        self.transcript_path.write_text(content, encoding="utf-8")
        environment = os.environ.copy()
        environment["UI_LIVE_DIR"] = str(self.live)
        result = subprocess.run(
            ["bash", str(PULL_SCRIPT), "--transcript",
             str(self.transcript_path)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        manifest_path = self.live / "pull_manifest.json"
        self.assertTrue(manifest_path.is_file(), result.stderr)
        return result, json.loads(manifest_path.read_text(encoding="utf-8"))

    def test_happy_mirror_verifies_folded_files(self):
        health = b'{"ok":true}\n'
        results = (b"result-row\n" * 250)
        rows = (file_block("health.jsonl", health)
                + file_block("jobs/results.jsonl", results)
                + ["FLAG STOP ABSENT", "FLAG STOP-JOBS ABSENT",
                   "FLAG account-governor.lock ABSENT"])

        result, manifest = self.run_pull(transcript(rows, 2))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.live / "health.jsonl").read_bytes(), health)
        self.assertEqual(
            (self.live / "jobs" / "results.jsonl").read_bytes(), results)
        manifest.pop("interval_ms", None)
        self.assertEqual(manifest, {
            "utc": UTC,
            "ok": True,
            "cycle_ms": manifest["cycle_ms"],
            "files": 2,
            "errors": [],
        })
        self.assertIsInstance(manifest["cycle_ms"], int)
        self.assertGreaterEqual(manifest["cycle_ms"], 0)

    def test_sha_mismatch_keeps_old_copy_and_records_error(self):
        self.live.mkdir()
        target = self.live / "health.jsonl"
        target.write_bytes(b"old\n")
        rows = file_block("health.jsonl", b"new\n", sha="0" * 64)

        result, manifest = self.run_pull(transcript(rows, 1))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), b"old\n")
        self.assertFalse(manifest["ok"])
        self.assertEqual(manifest["files"], 0)
        self.assertTrue(any("sha256 mismatch" in row
                            for row in manifest["errors"]))

    def test_absent_removes_previous_local_copy(self):
        target = self.live / "proposals" / "old.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"stale")

        result, manifest = self.run_pull(
            transcript(["ABSENT proposals/old.json"], 0))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(target.exists())
        self.assertTrue(manifest["ok"])
        self.assertEqual(manifest["files"], 0)

    def test_truncated_block_keeps_old_copy(self):
        self.live.mkdir()
        target = self.live / "stream_state.json"
        target.write_bytes(b"old-state")
        rows = file_block("stream_state.json", b"new-state", close=False)

        result, manifest = self.run_pull(transcript(rows, 1))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), b"old-state")
        self.assertFalse(manifest["ok"])
        self.assertTrue(any("truncated FILE block" in row
                            for row in manifest["errors"]))

    def test_flags_are_added_and_removed(self):
        flag_names = ("STOP", "STOP-JOBS", "account-governor.lock")
        present_rows = ["FLAG %s PRESENT" % name for name in flag_names]
        result, manifest = self.run_pull(transcript(present_rows, 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(manifest["ok"])
        for name in flag_names:
            self.assertTrue((self.live / name).is_file())

        absent_rows = ["FLAG %s ABSENT" % name for name in flag_names]
        result, manifest = self.run_pull(transcript(absent_rows, 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(manifest["ok"])
        for name in flag_names:
            self.assertFalse((self.live / name).exists())

    def test_manifest_is_last_and_is_rewritten_on_failure(self):
        payload = b"current"
        result, manifest = self.run_pull(
            transcript(file_block("decisions.jsonl", payload), 1))
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.live / "decisions.jsonl"
        manifest_path = self.live / "pull_manifest.json"
        self.assertGreaterEqual(
            manifest_path.stat().st_mtime_ns, target.stat().st_mtime_ns)
        previous_manifest_mtime = manifest_path.stat().st_mtime_ns

        time.sleep(0.01)
        result, manifest = self.run_pull("incomplete transcript\n")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(manifest["ok"])
        self.assertTrue(manifest["errors"])
        self.assertEqual(target.read_bytes(), payload)
        self.assertGreater(manifest_path.stat().st_mtime_ns,
                           previous_manifest_mtime)


if __name__ == "__main__":
    unittest.main()
