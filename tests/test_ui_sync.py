import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ui_sync.sh"
HOST_ROOT = "/offline/quant-ui"
VALID_SHA = "0" * 64


STUB = r'''#!/usr/bin/env python3
import json
import os
import sys

source, destination = sys.argv[1:3]
with open(source, "rb") as stream:
    payload = stream.read()
with open(os.environ["XFER_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({
        "destination": destination,
        "payload_hex": payload.hex(),
    }, sort_keys=True) + "\n")

failure_match = os.environ.get("XFER_FAIL", "")
no_marker_match = os.environ.get("XFER_NO_MARKER", "")
if failure_match and failure_match in destination:
    print("XFER_FAILED")
    raise SystemExit(1)
if no_marker_match and no_marker_match in destination:
    raise SystemExit(0)
print("XFER_VERIFIED")
'''


class UiSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "repo"
        self.ui = self.root / "ui"
        self.ui.mkdir(parents=True)
        self.state = Path(self.temp_dir.name) / "state.json"
        self.log = Path(self.temp_dir.name) / "xfer.jsonl"
        self.stub = Path(self.temp_dir.name) / "xfer-stub.py"
        self.stub.write_text(STUB, encoding="utf-8")
        self.stub.chmod(0o700)

    def run_sync(self, *arguments, env_update=None):
        environment = os.environ.copy()
        environment.update({
            "REPO_ROOT": str(self.root),
            "HOST_UI_ROOT": HOST_ROOT,
            "UI_SYNC_STATE": str(self.state),
            "SYNC_INTERVAL": "1",
            "XFER_CMD": shlex.quote(str(self.stub)),
            "XFER_LOG": str(self.log),
        })
        environment.pop("XFER_FAIL", None)
        environment.pop("XFER_NO_MARKER", None)
        if env_update:
            environment.update(env_update)
        return subprocess.run(
            ["/bin/sh", str(SCRIPT), *arguments],
            cwd=self.root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in
                self.log.read_text(encoding="utf-8").splitlines()]

    def clear_calls(self):
        self.log.unlink(missing_ok=True)

    def state_files(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["files"]

    def write_state(self, files):
        self.state.write_text(
            json.dumps({"version": 1, "files": files}, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_new_unchanged_and_changed_files(self):
        app = self.ui / "app.js"
        ignore = self.ui / ".gitignore"
        app.write_bytes(b"first")
        ignore.write_bytes(b"live/\n")

        first = self.run_sync("--once")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(
            [call["destination"] for call in self.calls()],
            [HOST_ROOT + "/.gitignore", HOST_ROOT + "/app.js"],
        )
        self.assertEqual(
            self.state_files(),
            {
                "ui/.gitignore": hashlib.sha256(b"live/\n").hexdigest(),
                "ui/app.js": hashlib.sha256(b"first").hexdigest(),
            },
        )

        self.clear_calls()
        unchanged = self.run_sync("--once")
        self.assertEqual(unchanged.returncode, 0, unchanged.stderr)
        self.assertEqual(self.calls(), [])

        app.write_bytes(b"second")
        changed = self.run_sync("--once")
        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertEqual(
            [call["destination"] for call in self.calls()],
            [HOST_ROOT + "/app.js"],
        )
        self.assertEqual(
            self.state_files()["ui/app.js"],
            hashlib.sha256(b"second").hexdigest(),
        )

    def test_allowlist_refuses_traversal_symlink_outside_and_hidden_files(self):
        outside = Path(self.temp_dir.name) / "outside.txt"
        outside.write_bytes(b"outside")
        (self.ui / "linked.js").symlink_to(outside)
        (self.ui / ".env").write_bytes(b"secret-shaped test data")
        large = self.ui / "large.bin"
        with large.open("wb") as stream:
            stream.seek(5 * 1024 * 1024)
            stream.write(b"x")
        self.write_state({
            "outside.txt": VALID_SHA,
            "ui/../escape.js": VALID_SHA,
        })

        result = self.run_sync("--once")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertIn("REFUSE state outside.txt: path is outside ui/",
                      result.stderr)
        self.assertIn("REFUSE state ui/../escape.js: path contains '..'",
                      result.stderr)
        self.assertIn("REFUSE ui/linked.js: symlink", result.stderr)
        self.assertIn("REFUSE ui/.env: dot path", result.stderr)
        self.assertIn("REFUSE ui/large.bin: file exceeds 5MB", result.stderr)

    def test_state_advances_only_after_xfer_verification(self):
        (self.ui / "bad.js").write_bytes(b"bad")
        (self.ui / "good.js").write_bytes(b"good")

        failed = self.run_sync(
            "--once", env_update={"XFER_NO_MARKER": "/bad.js"})
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(set(self.state_files()), {"ui/good.js"})
        self.assertIn("sha_verified=no", failed.stderr)

        self.clear_calls()
        recovered = self.run_sync("--once")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(
            [call["destination"] for call in self.calls()],
            [HOST_ROOT + "/bad.js"],
        )
        self.assertEqual(set(self.state_files()),
                         {"ui/bad.js", "ui/good.js"})

    def test_deletion_manifest_is_transferred_before_state_removal(self):
        old = self.ui / "old.css"
        old.write_bytes(b"old")
        initial = self.run_sync("--once")
        self.assertEqual(initial.returncode, 0, initial.stderr)

        self.clear_calls()
        old.unlink()
        deleted = self.run_sync("--once")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(len(self.calls()), 1)
        call = self.calls()[0]
        self.assertEqual(call["destination"],
                         HOST_ROOT + "/ui-sync-deletions.txt")
        self.assertEqual(bytes.fromhex(call["payload_hex"]), b"ui/old.css\n")
        self.assertEqual(self.state_files(), {})

    def test_dry_run_is_inert_for_transfers_deletions_and_state(self):
        (self.ui / "new.html").write_bytes(b"new")
        self.write_state({"ui/gone.html": VALID_SHA})
        before = self.state.read_bytes()

        result = self.run_sync("--once", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertIn("DRY-RUN transfer ui/new.html", result.stdout)
        self.assertIn("DRY-RUN delete ui/gone.html", result.stdout)
        self.assertIn("DRY-RUN transfer ui-sync-deletions.txt", result.stdout)


if __name__ == "__main__":
    unittest.main()
