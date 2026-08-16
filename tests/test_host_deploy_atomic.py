"""Shell-level tests for the host atomic-deploy gate."""

import hashlib
import os
import shutil
import stat
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

    def stage_files(self, files, digests=None, modes=None):
        rows = []
        for relpath, payload in files.items():
            path = self.stage / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            if digests and relpath in digests:
                digest = digests[relpath]
            row = "%s  %s" % (digest, relpath)
            if modes and relpath in modes:
                row += " %s" % modes[relpath]
            rows.append(row + "\n")
        self.manifest.write_text("".join(rows), encoding="utf-8")

    def run_deploy(self, release_sha=RELEASE_SHA, env=None):
        return subprocess.run(
            ["bash", str(DEPLOY_SCRIPT), str(self.stage),
             str(self.deploy), str(self.manifest), release_sha],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
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
        for relpath, payload in files.items():
            self.assertIn(
                "HOST-DEPLOY | LANDED | path=%s | sha256=%s | mode=" % (
                    relpath, hashlib.sha256(payload).hexdigest()),
                result.stdout,
            )

    def test_preserves_executable_mode_when_replacing_file(self):
        relpath = "scripts/job_tick.sh"
        payload = b"#!/bin/bash\nprintf 'tick\\n'\n"
        self.stage_files({relpath: payload})
        staged = self.stage / relpath
        staged.chmod(0o600)
        target = self.deploy / relpath
        target.parent.mkdir()
        target.write_bytes(b"#!/bin/bash\nprintf 'old tick\\n'\n")
        target.chmod(0o751)

        result = self.run_deploy()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o751)
        self.assertIn(
            "HOST-DEPLOY | LANDED | path=scripts/job_tick.sh | "
            "sha256=%s | mode=751" % hashlib.sha256(payload).hexdigest(),
            result.stdout,
        )

    def test_applies_and_verifies_explicit_manifest_mode(self):
        relpath = "scripts/job_tick.sh"
        payload = b"#!/bin/bash\nprintf 'tick\\n'\n"
        self.stage_files({relpath: payload}, modes={relpath: "700"})
        target = self.deploy / relpath
        target.parent.mkdir()
        target.write_bytes(b"old tick\n")
        target.chmod(0o600)

        result = self.run_deploy()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertIn("path=scripts/job_tick.sh", result.stdout)
        self.assertIn("mode=700", result.stdout)

    def test_execute_mode_regression_keeps_gate_and_old_release(self):
        relpath = "scripts/job_tick.sh"
        self.stage_files(
            {relpath: b"#!/bin/bash\nprintf 'tick\\n'\n"},
            modes={relpath: "600"},
        )
        target = self.deploy / relpath
        target.parent.mkdir()
        target.write_bytes(b"old tick\n")
        target.chmod(0o700)
        (self.deploy / "RELEASE").write_text(
            OLD_RELEASE_SHA + "\n", encoding="ascii")

        result = self.run_deploy()

        self.assertEqual(result.returncode, 2)
        self.assertIn("HOST-DEPLOY | REFUSED | MODE_REGRESSION", result.stderr)
        self.assertTrue((self.deploy / "DEPLOYING").is_file())
        self.assertEqual(
            (self.deploy / "RELEASE").read_text(encoding="ascii"),
            OLD_RELEASE_SHA + "\n",
        )

    def test_hash_mismatch_after_rename_keeps_gate_and_old_release(self):
        relpath = "src/job_runner.py"
        self.stage_files({relpath: b"new runner\n"})
        (self.deploy / "RELEASE").write_text(
            OLD_RELEASE_SHA + "\n", encoding="ascii")
        shim_dir = self.root / "bin"
        shim_dir.mkdir()
        mv_shim = shim_dir / "mv"
        mv_shim.write_text(
            "#!/bin/bash\n"
            "command -p mv \"$@\"\n"
            "status=$?\n"
            "[ \"$status\" -eq 0 ] || exit \"$status\"\n"
            "target=\n"
            "for value in \"$@\"; do target=$value; done\n"
            "if [ \"$target\" = \"$HASH_MISMATCH_TARGET\" ]; then\n"
            "  printf 'changed during rename\\n' >> \"$target\"\n"
            "fi\n",
            encoding="utf-8",
        )
        mv_shim.chmod(0o700)
        env = os.environ.copy()
        env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
        env["HASH_MISMATCH_TARGET"] = str((self.deploy / relpath).resolve())

        result = self.run_deploy(env=env)

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
        self.assertIn(
            "UNEXPECTED_STAGED_PATH | path=src/margin_ops.py",
            result.stderr,
        )
        self.assertFalse((self.deploy / "DEPLOYING").exists())
        self.assertTrue(extra.is_file())
        self.assertFalse((self.deploy / "RELEASE").exists())

    def test_manifest_missing_staged_file_names_path_without_gate(self):
        files = {
            "src/job_runner.py": b"runner\n",
            "src/margin_ops.py": b"margin\n",
        }
        self.stage_files(files)
        missing = self.stage / "src" / "margin_ops.py"
        missing.unlink()

        result = self.run_deploy()

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "MISSING_MANIFEST_PATH | path=src/margin_ops.py",
            result.stderr,
        )
        self.assertFalse((self.deploy / "DEPLOYING").exists())
        self.assertTrue((self.stage / "src" / "job_runner.py").is_file())

    def test_unsafe_staged_path_names_path_without_gate(self):
        self.stage_files({"src/job_runner.py": b"runner\n"})
        unsafe = self.stage / "src" / "margin ops.py"
        unsafe.write_bytes(b"margin\n")

        result = self.run_deploy()

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "HOST-DEPLOY | REFUSED | STAGING_PATH_UNSAFE | "
            "path=src/margin\\ ops.py",
            result.stderr,
        )
        self.assertFalse((self.deploy / "DEPLOYING").exists())
        self.assertTrue(unsafe.is_file())

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
