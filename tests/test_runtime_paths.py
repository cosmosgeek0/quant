"""Offline contract tests for the canonical deployed runtime paths."""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import runtime_paths  # noqa: E402


class RuntimePathsTests(unittest.TestCase):
    def test_every_artifact_is_derived_from_receipt_dir(self):
        root = Path("/srv/quant/receipts")
        paths = runtime_paths.resolve_runtime_paths({"RECEIPT_DIR": str(root)})

        self.assertEqual(paths.receipt_dir, root)
        self.assertEqual(paths.jobs_dir, root / "jobs")
        self.assertEqual(paths.job_results, root / "jobs" / "results.jsonl")
        self.assertEqual(paths.proposals_dir, root / "proposals")
        self.assertEqual(paths.stream_state, root / "stream_state.json")
        self.assertEqual(paths.decisions, root / "decisions.jsonl")
        self.assertEqual(paths.health, root / "health.jsonl")
        self.assertEqual(paths.momentum_position,
                         root / "momentum-position.json")

    def test_stream_sidecars_and_watch_state_have_one_canonical_location(self):
        root = Path("/var/lib/quant")
        paths = runtime_paths.resolve_runtime_paths({"RECEIPT_DIR": str(root)})

        self.assertEqual(paths.stream_log, root / "stream_log.jsonl")
        self.assertEqual(paths.stream_windows, root / "stream_windows.json")
        self.assertEqual(paths.watch_dir, root / "watch")
        self.assertEqual(paths.basis_watch_state,
                         root / "watch" / "basis_watch_state.json")
        self.assertEqual(paths.momentum_state,
                         root / "momentum-paper-state.json")
        self.assertEqual(paths.momentum_receipts,
                         root / "momentum-paper-receipts.jsonl")

    def test_stop_files_are_exactly_the_two_receipt_root_interlocks(self):
        root = Path("/opt/quant-receipts")
        paths = runtime_paths.resolve_runtime_paths({"RECEIPT_DIR": str(root)})

        self.assertEqual(paths.stop, root / "STOP")
        self.assertEqual(paths.stop_jobs, root / "STOP-JOBS")
        self.assertNotEqual(paths.stop, paths.stop_jobs)

    def test_tilde_uses_the_supplied_home(self):
        paths = runtime_paths.resolve_runtime_paths({
            "HOME": "/home/runtime",
            "RECEIPT_DIR": "~/receipts",
        })
        self.assertEqual(paths.receipt_dir, Path("/home/runtime/receipts"))

    def test_shell_and_json_views_preserve_the_same_paths(self):
        paths = runtime_paths.resolve_runtime_paths({
            "RECEIPT_DIR": "/tmp/runtime receipts",
        })
        shell = runtime_paths.render_shell(paths)
        payload = {name: str(value) for name, value in asdict(paths).items()}

        self.assertIn("RECEIPT_DIR='/tmp/runtime receipts'", shell)
        self.assertEqual(json.loads(json.dumps(payload))["stop"],
                         "/tmp/runtime receipts/STOP")
        self.assertEqual(set(paths.as_env()), {
            "RECEIPT_DIR", "JOBS_DIR", "JOB_RESULTS", "PROPOSALS_DIR",
            "WATCH_DIR", "STREAM_STATE", "STREAM_LOG", "STREAM_WINDOWS",
            "BASIS_WATCH_STATE", "DECISIONS", "HEALTH",
            "MOMENTUM_STATE", "MOMENTUM_POSITION", "MOMENTUM_RECEIPTS",
            "STOP_FILE", "STOP_JOBS_FILE",
        })

    def test_imported_constants_are_the_resolved_path_object(self):
        self.assertIsInstance(runtime_paths.PATHS, runtime_paths.RuntimePaths)
        self.assertEqual(runtime_paths.RECEIPT_DIR,
                         runtime_paths.PATHS.receipt_dir)
        self.assertEqual(runtime_paths.STOP_FILE,
                         runtime_paths.RECEIPT_DIR / "STOP")
        self.assertEqual(runtime_paths.STOP_JOBS_FILE,
                         runtime_paths.RECEIPT_DIR / "STOP-JOBS")

    def test_invalid_receipt_dir_is_rejected(self):
        for raw in ("", "bad\npath", "bad\rpath", "bad\x00path"):
            with self.subTest(raw=repr(raw)):
                with self.assertRaises(ValueError):
                    runtime_paths.resolve_runtime_paths({"RECEIPT_DIR": raw})


if __name__ == "__main__":
    unittest.main()
