"""Shell-level tests for the host atomic-deploy gate."""

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = ROOT / "scripts" / "host_deploy_atomic.sh"
JOB_TICK = ROOT / "scripts" / "job_tick.sh"
RELEASE_SHA = "a" * 40
OLD_RELEASE_SHA = "b" * 40


class TestHostDeployAtomic(unittest.TestCase):

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.stage = self.root / "stage"
        self.deploy = self.root / "deploy"
        self.manifest = self.root / "manifest.sha256"
        self.stage.mkdir()
        self.deploy.mkdir()

    def stage_files(self, files, digests=None):
        rows = []
        for relpath, payload in files.items():
            path = self.stage / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            if digests and relpath in digests:
                digest = digests[relpath]
            rows.append("%s  %s\n" % (digest, relpath))
        self.manifest.write_text("".join(rows), encoding="utf-8")

    def run_deploy(self, release_sha=RELEASE_SHA):
        return subprocess.run(
            ["bash", str(DEPLOY_SCRIPT), str(self.stage),
             str(self.deploy), str(self.manifest), release_sha],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def test_lands_all_files_then_publishes_release(self):
        files = {
            "src/job_runner.py": b"import margin_ops\n",
            "src/margin_ops.py": b"VALUE = 1\n",
        }
        self.stage_files(files)
        old_runner = self.deploy / "src" / "job_runner.py"
        old_runner.parent.mkdir()
        old_runner.write_bytes(b"old runner\n")
        (self.deploy / "RELEASE").write_text(
            OLD_RELEASE_SHA + "\n", encoding="ascii")

        result = self.run_deploy()

        self.assertEqual(result.returncode, 0, result.stderr)
        for relpath, payload in files.items():
            self.assertEqual((self.deploy / relpath).read_bytes(), payload)
            self.assertFalse((self.stage / relpath).exists())
        self.assertEqual(
            (self.deploy / "RELEASE").read_text(encoding="ascii"),
            RELEASE_SHA + "\n",
        )
        self.assertFalse((self.deploy / "DEPLOYING").exists())
        self.assertIn("HOST-DEPLOY | OK | files=2", result.stdout)

    def test_landed_digest_mismatch_keeps_gate_and_old_release(self):
        relpath = "src/job_runner.py"
        self.stage_files(
            {relpath: b"new runner\n"},
            {relpath: hashlib.sha256(b"expected runner\n").hexdigest()},
        )
        (self.deploy / "RELEASE").write_text(
            OLD_RELEASE_SHA + "\n", encoding="ascii")

        result = self.run_deploy()

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "HOST-DEPLOY | REFUSED | LANDED_SHA256_MISMATCH",
            result.stderr,
        )
        self.assertTrue((self.deploy / "DEPLOYING").is_file())
        self.assertEqual(
            (self.deploy / "RELEASE").read_text(encoding="ascii"),
            OLD_RELEASE_SHA + "\n",
        )

    def test_manifest_must_cover_every_staged_file(self):
        self.stage_files({"src/job_runner.py": b"runner\n"})
        extra = self.stage / "src" / "margin_ops.py"
        extra.write_bytes(b"margin\n")

        result = self.run_deploy()

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "HOST-DEPLOY | REFUSED | MANIFEST_STAGE_MISMATCH",
            result.stderr,
        )
        self.assertTrue((self.deploy / "DEPLOYING").is_file())
        self.assertTrue(extra.is_file())
        self.assertFalse((self.deploy / "RELEASE").exists())

    def test_job_tick_refuses_while_marker_is_present(self):
        scripts = self.deploy / "scripts"
        source = self.deploy / "src"
        scripts.mkdir()
        source.mkdir()
        shutil.copy2(JOB_TICK, scripts / "job_tick.sh")
        (source / "runtime_paths.py").write_text("raise AssertionError\n")
        (self.deploy / "DEPLOYING").write_text(RELEASE_SHA + "\n")

        result = subprocess.run(
            ["bash", str(scripts / "job_tick.sh")],
            cwd=self.deploy,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "JOB-TICK | REFUSED | DEPLOYING marker present",
            (self.deploy / "jobs.log").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
