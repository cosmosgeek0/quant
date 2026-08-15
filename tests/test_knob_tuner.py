"""Offline contract tests for the hourly funnel knob tuner."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TUNER = ROOT / "scripts" / "knob_tuner.sh"
RUNTIME_TICK = ROOT / "scripts" / "runtime_tick.sh"
RUNTIME_PATHS = ROOT / "src" / "runtime_paths.py"
ORIGINAL_KNOBS = (
    "QUANT_IGNITION_SURGE=1.75\n"
    "QUANT_MAX_ENTRIES_DAY=11\n"
    "QUANT_DAILY_LOSS_CAP=2.25\n"
)


class KnobTunerTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.deploy = self.root / "deploy"
        (self.deploy / "scripts").mkdir(parents=True)
        (self.deploy / "src").mkdir()
        shutil.copy2(TUNER, self.deploy / "scripts" / TUNER.name)
        shutil.copy2(RUNTIME_PATHS, self.deploy / "src" / RUNTIME_PATHS.name)
        self.script = self.deploy / "scripts" / TUNER.name
        self.receipts = self.root / "receipts"
        self.funnel = self.receipts / "funnel.jsonl"
        self.health = self.receipts / "health.jsonl"
        self.knobs = self.deploy / "knobs.env"
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def funnel_row(self, stage, *, entered=0, age_minutes=1):
        counts = {
            "universe": 4,
            "fresh_ok": 4,
            "baseline_ok": 3,
            "ignited": 0 if stage in {"ignited", "ignition"} else 2,
            "ticket_eligible": 0 if stage == "ticket_eligible" else 1,
            "entered": entered,
        }
        if stage == "fresh_ok":
            counts["fresh_ok"] = 0
            counts["baseline_ok"] = 0
            counts["ignited"] = 0
            counts["ticket_eligible"] = 0
        return {
            "utc": (self.now - timedelta(minutes=age_minutes)).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z"),
            **counts,
            "starving_stage": stage,
        }

    def write_funnel(self, *rows):
        self.receipts.mkdir(parents=True, exist_ok=True)
        self.funnel.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    def run_tuner(self):
        environment = dict(
            os.environ,
            RECEIPT_DIR=str(self.receipts),
            PYTHONDONTWRITEBYTECODE="1",
        )
        completed = subprocess.run(
            ["bash", str(self.script)],
            cwd=self.deploy,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def health_rows(self):
        return [
            json.loads(line)
            for line in self.health.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_ignition_step_clamps_at_floor_and_is_idempotent(self):
        self.knobs.write_text(ORIGINAL_KNOBS, encoding="ascii")
        old_entry = self.funnel_row("none", entered=1, age_minutes=90)
        self.write_funnel(old_entry, self.funnel_row("ignited"))

        self.run_tuner()

        expected = ORIGINAL_KNOBS.replace(
            "QUANT_IGNITION_SURGE=1.75",
            "QUANT_IGNITION_SURGE=1.5",
        )
        self.assertEqual(self.knobs.read_text(encoding="ascii"), expected)
        changed = self.health_rows()[-1]
        self.assertEqual(changed["stage"], "knob_tuner")
        self.assertEqual(changed["action"], "changed")
        self.assertEqual(changed["change"], {
            "from": "1.75",
            "knob": "QUANT_IGNITION_SURGE",
            "step": "-0.5",
            "to": "1.5",
        })

        self.run_tuner()

        self.assertEqual(self.knobs.read_text(encoding="ascii"), expected)
        self.assertEqual(self.health_rows()[-1]["reason"], "already_processed")

    def test_advisory_only_stages_leave_every_knob_untouched(self):
        self.knobs.write_text(ORIGINAL_KNOBS, encoding="ascii")
        original = self.knobs.read_bytes()

        for index, stage in enumerate(
            ("fresh_ok", "ticket_eligible", "entered"), start=1
        ):
            with self.subTest(stage=stage):
                self.write_funnel(self.funnel_row(
                    stage, age_minutes=index
                ))
                self.run_tuner()
                self.assertEqual(self.knobs.read_bytes(), original)
                row = self.health_rows()[-1]
                self.assertEqual(row["action"], "advisory")
                self.assertIsNone(row["change"])
                self.assertEqual(row["starving_stage"], stage)
                expected_reason = (
                    "freshness_advisory"
                    if stage == "fresh_ok"
                    else "owner_ticket_advisory"
                )
                self.assertEqual(row["reason"], expected_reason)

    def test_absent_funnel_is_an_honest_safe_noop(self):
        self.run_tuner()

        self.assertFalse(self.knobs.exists())
        self.assertEqual(self.health_rows(), [{
            "action": "noop",
            "change": None,
            "reason": "funnel_absent",
            "rows": 0,
            "stage": "knob_tuner",
            "utc": self.health_rows()[0]["utc"],
        }])

    def test_runtime_tick_sources_exported_knobs_before_first_stage(self):
        source = RUNTIME_TICK.read_text(encoding="utf-8")

        self.assertIn('KNOBS_FILE="$DEPLOY_DIR/knobs.env"', source)
        self.assertLess(
            source.index("  set -a\n"),
            source.index('  . "$KNOBS_FILE"\n'),
        )
        self.assertLess(
            source.index('  . "$KNOBS_FILE"\n'),
            source.index("  set +a\n"),
        )
        self.assertLess(source.index("  set +a\n"), source.index("run_stage feed"))


if __name__ == "__main__":
    unittest.main()
