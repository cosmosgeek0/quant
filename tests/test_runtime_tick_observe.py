#!/usr/bin/env python3
"""Offline integration checks for runtime-tick observation stages."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_TICK = ROOT / "scripts" / "runtime_tick.sh"
OBSERVE_PROGRAMS = {
    "executable_gap_watch.py",
    "reverse_lockin_shadow.py",
}

RUNTIME_PATHS_STUB = r'''#!/usr/bin/env python3
import os
import shlex
from pathlib import Path

receipt = Path(os.environ["RECEIPT_DIR"])
watch = receipt / "watch"
values = {
    "RECEIPT_DIR": receipt,
    "JOBS_DIR": receipt / "jobs",
    "JOB_RESULTS": receipt / "jobs" / "results.jsonl",
    "PROPOSALS_DIR": receipt / "proposals",
    "WATCH_DIR": watch,
    "STREAM_STATE": receipt / "stream_state.json",
    "STREAM_LOG": receipt / "stream_log.jsonl",
    "STREAM_WINDOWS": receipt / "stream_windows.json",
    "BASIS_WATCH_STATE": watch / "basis_watch_state.json",
    "DECISIONS": receipt / "decisions.jsonl",
    "HEALTH": receipt / "health.jsonl",
    "MOMENTUM_STATE": receipt / "momentum-paper-state.json",
    "MOMENTUM_POSITION": receipt / "momentum-position.json",
    "MOMENTUM_RECEIPTS": receipt / "momentum-paper-receipts.jsonl",
    "STOP_FILE": receipt / "STOP",
    "STOP_JOBS_FILE": receipt / "STOP-JOBS",
}
print("\n".join(
    f"{name}={shlex.quote(str(path))}" for name, path in values.items()
))
'''

STAGE_STUB = r'''#!/usr/bin/env python3
import json
import os
import signal
import sys
import time
from pathlib import Path

program = Path(sys.argv[0]).name
with open(os.environ["TRACE_FILE"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"program": program, "argv": sys.argv[1:]}) + "\n")
if program == "market_feed.py" and "--serve" in sys.argv[1:]:
    signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
    while True:
        time.sleep(0.1)
if os.environ.get("FAIL_STAGE") == program:
    raise SystemExit(int(os.environ.get("FAIL_CODE", "7")))
'''


class RuntimeTickObserveTests(unittest.TestCase):
    def run_tick(self, *, seal=None, fail_stage=None):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve()
            deploy = temporary_root / "deploy"
            scripts = deploy / "scripts"
            source = deploy / "src"
            receipts = temporary_root / "receipts"
            bin_dir = temporary_root / "bin"
            scripts.mkdir(parents=True)
            source.mkdir()
            bin_dir.mkdir()
            (receipts / "watch").mkdir(parents=True)
            tick = scripts / "runtime_tick.sh"
            shutil.copy2(RUNTIME_TICK, tick)
            (deploy / "RELEASE").write_text("0" * 40 + "\n", encoding="ascii")
            (source / "runtime_paths.py").write_text(
                RUNTIME_PATHS_STUB, encoding="utf-8"
            )
            stage_names = (
                "market_feed.py",
                "market_stream.py",
                "basis_recorder.py",
                "executable_gap_watch.py",
                "reverse_lockin_shadow.py",
                "basis_watch.py",
                "momentum_live.py",
                "basis_supervisor.py",
            )
            for name in stage_names:
                (source / name).write_text(STAGE_STUB, encoding="utf-8")
            ps_stub = bin_dir / "ps"
            ps_stub.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$FAKE_FEED_COMMAND"\n',
                encoding="ascii",
            )
            ps_stub.chmod(0o700)

            trace_path = temporary_root / "trace.jsonl"
            environment = os.environ.copy()
            environment.update({
                "RECEIPT_DIR": str(receipts),
                "TRACE_FILE": str(trace_path),
                "BINANCE_API_KEY": "offline-key",
                "BINANCE_API_SECRET": "offline-secret",
                "FAKE_FEED_COMMAND": (
                    f"{source / 'market_feed.py'} --serve"
                ),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            if fail_stage is not None:
                environment["FAIL_STAGE"] = fail_stage
                environment["FAIL_CODE"] = "7"
            if seal is not None:
                (receipts / seal).touch()

            feed = subprocess.Popen(
                [sys.executable, str(source / "market_feed.py"), "--serve"],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _attempt in range(100):
                if trace_path.is_file() and trace_path.stat().st_size:
                    break
                time.sleep(0.01)
            (receipts / "watch" / "feed.pid").write_text(
                f"{feed.pid}\n", encoding="ascii"
            )
            try:
                completed = subprocess.run(
                    ["bash", str(tick)],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                traces = [
                    json.loads(line)
                    for line in trace_path.read_text(encoding="utf-8").splitlines()
                ]
                health_path = receipts / "health.jsonl"
                health = [
                    json.loads(line)
                    for line in health_path.read_text(encoding="utf-8").splitlines()
                ]
                return completed, traces, health
            finally:
                feed.terminate()
                try:
                    feed.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    feed.kill()
                    feed.wait(timeout=3)

    @staticmethod
    def observed(traces):
        return [row for row in traces if row["program"] in OBSERVE_PROGRAMS]

    def test_observe_commands_run_under_each_seal(self):
        for seal in ("STOP", "STOP-JOBS"):
            with self.subTest(seal=seal):
                completed, traces, health = self.run_tick(seal=seal)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(
                    [row["program"] for row in self.observed(traces)],
                    ["executable_gap_watch.py", "reverse_lockin_shadow.py"],
                )
                rows = {
                    row["stage"]: row
                    for row in health
                    if row["stage"] in {
                        "executable_gap_watch",
                        "reverse_lockin_shadow",
                    }
                }
                self.assertEqual(set(rows), {
                    "executable_gap_watch",
                    "reverse_lockin_shadow",
                })
                for row in rows.values():
                    self.assertEqual(row["exit"], 0)
                    self.assertIsInstance(row["duration_ms"], int)
                    self.assertGreaterEqual(row["duration_ms"], 0)

    def test_observe_failure_is_recorded_and_later_stages_continue(self):
        cases = (
            ("executable_gap_watch.py", "executable_gap_watch",
             "reverse_lockin_shadow.py"),
            ("reverse_lockin_shadow.py", "reverse_lockin_shadow",
             "basis_watch.py"),
        )
        for failed_program, failed_stage, later_program in cases:
            with self.subTest(stage=failed_stage):
                completed, traces, health = self.run_tick(
                    fail_stage=failed_program
                )
                self.assertEqual(completed.returncode, 1)
                programs = [row["program"] for row in traces]
                self.assertGreater(
                    programs.index(later_program), programs.index(failed_program)
                )
                self.assertGreater(
                    programs.index("basis_supervisor.py"),
                    programs.index(failed_program),
                )
                failed_rows = [
                    row for row in health
                    if row.get("stage") == failed_stage and row.get("exit") == 7
                ]
                self.assertEqual(len(failed_rows), 1)
                self.assertGreaterEqual(failed_rows[0]["duration_ms"], 0)

    def test_observe_command_lines_are_exact(self):
        completed, traces, _health = self.run_tick()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.observed(traces), [
            {"program": "executable_gap_watch.py", "argv": ["--once"]},
            {
                "program": "reverse_lockin_shadow.py",
                "argv": ["--once", "--assume-borrow-power"],
            },
        ])


if __name__ == "__main__":
    unittest.main()
