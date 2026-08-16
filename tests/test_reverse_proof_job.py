"""Offline checks for the reverse-mechanics proof job composer."""

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SRC = ROOT / "src"
for module_path in (SCRIPTS, SRC):
    if str(module_path) not in os.sys.path:
        os.sys.path.insert(0, str(module_path))

import reverse_proof_job as composer  # noqa: E402
import job_runner as runner  # noqa: E402


NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


class ReverseProofJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.operator = self.root / "OPERATOR"
        self.operator.write_text(
            json.dumps({"operator": "lead"}), encoding="utf-8"
        )

    def build(self, **changes):
        values = {
            "symbol": "TSTUSDT", "quote_cap": "8",
            "borrow_asset": "TST", "expiry_minutes": 10,
            "operator_token_path": self.operator, "now": NOW,
        }
        values.update(changes)
        return composer.build_job(**values)

    def test_job_record_has_the_exact_runner_shape(self):
        job = self.build()

        self.assertEqual(set(job), composer.JOB_FIELDS)
        self.assertEqual(job["op"], composer.OP)
        self.assertEqual(job["requested_by"], "lead")
        self.assertEqual(job["operator_token"], "lead")
        self.assertEqual(composer.validate_job(job, NOW), job)
        accepted = runner._validate_job(job, job["job_id"], NOW)
        self.assertEqual(accepted.job_id, job["job_id"])

    def test_expiry_window_accepts_fifteen_minutes_and_refuses_more(self):
        bounded = self.build(expiry_minutes=15)
        self.assertEqual(
            datetime.fromisoformat(bounded["expires_utc"][:-1] + "+00:00")
            - datetime.fromisoformat(
                bounded["created_utc"][:-1] + "+00:00"),
            timedelta(minutes=15),
        )
        with self.assertRaisesRegex(
                composer.JobRefusal, "BAD_EXPIRY_MINUTES"):
            self.build(expiry_minutes=16)

    def test_extra_field_is_refused(self):
        job = self.build()
        job["extra"] = "value"

        with self.assertRaisesRegex(composer.JobRefusal, "UNKNOWN_FIELD"):
            composer.validate_job(job, NOW)

    def test_identity_values_are_deterministic_and_bound_to_facts(self):
        first = self.build()
        second = self.build()
        changed = self.build(quote_cap="7.5")

        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["intent_id"], second["intent_id"])
        self.assertEqual(first["client_tag"], second["client_tag"])
        self.assertNotEqual(first["intent_id"], changed["intent_id"])
        self.assertNotEqual(first["client_tag"], changed["client_tag"])

    def test_cli_prints_the_file_json_and_sets_mode_0600(self):
        output = self.root / "job.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = composer.main([
                "--symbol", "TSTUSDT", "--quote-cap", "8",
                "--borrow-asset", "TST", "--expiry-minutes", "10",
                "--operator-token-path", str(self.operator),
                "--output", str(output),
            ])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()),
                         json.loads(output.read_text(encoding="utf-8")))
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
