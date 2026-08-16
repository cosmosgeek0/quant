from __future__ import annotations

"""Offline tests for the host's local mirror."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MIRROR_SCRIPT = ROOT / "ui" / "host" / "local_mirror.sh"
STAGE_SCRIPT = ROOT / "ui" / "host" / "stage_server.sh"
VERIFY_SCRIPT = ROOT / "ui" / "host" / "verify_network.sh"
MEASURE_WRITES_SCRIPT = ROOT / "ui" / "host" / "measure_writes.sh"
NETWORK_SERVICE = ROOT / "ui" / "host" / "quant-ui-network.service"
NETWORK_DATA_SCRIPT = ROOT / "ui" / "network_data.js"
FEED_FIXTURE = ROOT / "tests" / "fixtures" / "network" / "feed_state_sample.json"
BASH = shutil.which("bash")
NODE = str(Path.home() / ".local" / "node22" / "bin" / "node")
if not Path(NODE).is_file():
    NODE = shutil.which("node")
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

    def run_mirror(self, extra_environment=None):
        environment = os.environ.copy()
        environment.update({
            "QUANT_MIRROR_R": str(self.receipts),
            "QUANT_MIRROR_L": str(self.live),
            "QUANT_MIRROR_D": str(self.run_dir),
            "QUANT_MIRROR_ONCE": "1",
        })
        if extra_environment:
            environment.update(extra_environment)
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

    def test_feed_source_is_not_copied(self):
        self.write_feed()

        self.run_mirror()

        manifest = self.read_json("pull_manifest.json")
        self.assertFalse((self.live / "feed_state.json").exists())
        self.assertFalse((self.live / "feed_state.meta.json").exists())
        self.assertEqual(manifest["errors"], [])
        self.assertTrue(manifest["ok"])
        self.assertEqual(manifest["files"], 0)

    def test_existing_feed_artifacts_are_not_rewritten(self):
        feed_target = self.live / "feed_state.json"
        meta_target = self.live / "feed_state.meta.json"
        feed_target.write_bytes(b'{"previous":"copy"}')
        meta_target.write_bytes(b'{"previous":"meta"}')
        fixed_ns = 1_600_000_000_000_000_000
        os.utime(feed_target, ns=(fixed_ns, fixed_ns))
        os.utime(meta_target, ns=(fixed_ns, fixed_ns))
        self.write_feed()

        self.run_mirror()

        manifest = self.read_json("pull_manifest.json")
        self.assertEqual(feed_target.read_bytes(), b'{"previous":"copy"}')
        self.assertEqual(meta_target.read_bytes(), b'{"previous":"meta"}')
        self.assertEqual(feed_target.stat().st_mtime_ns, fixed_ns)
        self.assertEqual(meta_target.stat().st_mtime_ns, fixed_ns)
        self.assertEqual(manifest["errors"], [])
        self.assertTrue(manifest["ok"])
        self.assertEqual(manifest["files"], 0)

    def test_unchanged_source_is_not_rewritten(self):
        source = self.receipts / "health.jsonl"
        target = self.live / "health.jsonl"
        source.write_bytes(b"same-size\n")
        self.run_mirror()
        first_ns = target.stat().st_mtime_ns
        source_ns = first_ns - 200_000_000_000
        target_ns = first_ns - 100_000_000_000
        os.utime(source, ns=(source_ns, source_ns))
        os.utime(target, ns=(target_ns, target_ns))

        self.run_mirror()

        manifest = self.read_json("pull_manifest.json")
        self.assertEqual(target.stat().st_mtime_ns, target_ns)
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertEqual(manifest["files"], 0)

    def test_changed_source_is_copied(self):
        source = self.receipts / "decisions.jsonl"
        target = self.live / "decisions.jsonl"
        source.write_bytes(b"row\n")
        self.run_mirror()
        source.write_bytes(source.read_bytes() + b"x")

        self.run_mirror()

        manifest = self.read_json("pull_manifest.json")
        self.assertEqual(target.read_bytes(), b"row\nx")
        self.assertEqual(target.stat().st_mtime_ns, source.stat().st_mtime_ns)
        self.assertEqual(manifest["files"], 1)

    def test_flag_present_twice_keeps_target_mtime(self):
        source = self.receipts / "STOP"
        target = self.live / "STOP"
        source.touch()
        self.run_mirror()
        fixed_ns = 1_600_000_000_000_000_000
        os.utime(target, ns=(fixed_ns, fixed_ns))

        self.run_mirror()

        self.assertEqual(target.stat().st_mtime_ns, fixed_ns)

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


class TestNetworkHostScripts(unittest.TestCase):

    def test_mirror_has_no_feed_copy_block(self):
        script = MIRROR_SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("feed_state", script)

    def test_r4_mirror_uses_set_u_and_short_metadata_rows(self):
        rows = MIRROR_SCRIPT.read_text(encoding="utf-8").splitlines()

        self.assertEqual(rows[1], "set -u")
        self.assertEqual(
            [(index, len(row)) for index, row in enumerate(rows, 1)
             if len(row) >= 200],
            [],
        )

    def test_r4_stage_launches_candidate_server(self):
        script = STAGE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('SERVER=${1:-ui/serve.py.new}', script)
        self.assertIn('python3 "$SERVER" --port 8081', script)

    def test_network_service_reads_feed_at_source(self):
        service = NETWORK_SERVICE.read_text(encoding="utf-8")

        self.assertIn(
            "--feed-source "
            "/home/ssm-user/quant_receipts/watch/feed_state.json",
            service,
        )

    def test_measure_writes_uses_disk_and_cgroup_counters(self):
        script = MEASURE_WRITES_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("df --output=source /home/ssm-user", script)
        self.assertIn("/proc/diskstats", script)
        self.assertIn("quant-ui-mirror.service/io.stat", script)
        self.assertIn("quant-ui-network.service/io.stat", script)
        self.assertIn("wbytes=", script)
        self.assertIn("sectors_delta * 512", script)
        self.assertNotIn("<<", script)
        self.assertEqual(
            [(index, len(row)) for index, row in enumerate(
                script.splitlines(), 1) if len(row) >= 200],
            [],
        )

    def test_r8_stage_round_2_validates_json_and_probe_route(self):
        script = STAGE_SCRIPT.read_text(encoding="utf-8")
        checked = set(re.findall(r'^get_path "([^"]+)"$', script, re.MULTILINE))
        required = {
            "/",
            "/api/strip",
            "/api/health",
            "/api/decisions",
            "/api/failures",
            "/api/events",
            "/api/stream",
            "/api/candidates",
            "/api/position",
            "/api/momentum",
            "/api/fees",
            "/api/pnl",
            "/api/news",
            "/api/telemetry",
            "/api/poll_config",
            "/api/grace",
            "/api/network",
            "/api/network/history?edge_id=spot_rest&metric=status&range=1h",
            "/api/network/events?limit=5",
        }

        self.assertEqual(checked, required)
        self.assertIn(
            "python3 -c 'import json,sys; json.load(sys.stdin)'",
            script,
        )
        self.assertIn('post_json "/api/network/config"', script)
        self.assertIn('post_json "/api/network/probe"', script)
        self.assertIn('"cooldown" in error or "already requested" in error',
                      script)
        self.assertEqual(
            [(index, len(row)) for index, row in enumerate(
                script.splitlines(), 1) if len(row) >= 200],
            [],
        )

    def test_r8_verify_round_2_gates_feed_and_api_bodies(self):
        script = VERIFY_SCRIPT.read_text(encoding="utf-8")
        sha_block = script.split('if [ "$source_sha"', 1)[1].split(
            'echo "== feed meta"', 1)[0]

        self.assertNotIn("failed=1", sha_block)
        self.assertIn('meta_sha=$(', script)
        self.assertIn('if [ "$meta_sha" = "Unknown" ]', script)
        self.assertIn('elif [ "$meta_sha" = "$mirror_sha" ]', script)
        self.assertIn(
            'feed_lag_s=$((source_mtime_epoch - mirror_mtime_epoch))',
            script,
        )
        self.assertIn('[ "$feed_lag_s" -gt 30 ]', script)
        self.assertIn(
            'benchmark /api/network "$network_url" "$tmp_dir/network.json" 100000',
            script,
        )
        self.assertIn(
            'benchmark /api/network/history-24h "$history_url" '
            '"$tmp_dir/history.json" 1000000',
            script,
        )
        self.assertIn(
            'benchmark /api/network/events "$events_url" '
            '"$tmp_dir/events.json" 300000',
            script,
        )
        self.assertRegex(
            script,
            r'\[ "\$bytes" -gt "\$max_body_bytes" \][\s\S]{0,200}failed=1',
        )
        for word in ("connectorId", "receipt_path", "job_id", '"signature"'):
            self.assertIn(word, script)
        self.assertIn('scan_identifiers network "$tmp_dir/network.json"', script)
        self.assertIn('scan_identifiers history "$tmp_dir/history.json"', script)
        self.assertIn('scan_identifiers events "$tmp_dir/events.json"', script)
        self.assertIn('echo "== VERIFY FAILED"', script)
        self.assertIn("exit 20", script)


@unittest.skipUnless(NODE, "node is required")
class TestNetworkData(unittest.TestCase):

    def run_network_data(self, body):
        harness = r'''
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const sourcePath = process.argv[1];
const source = fs.readFileSync(sourcePath, "utf8");

function jsonResponse(data) {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    text: function () { return Promise.resolve(JSON.stringify(data)); }
  };
}

function loadNetworkData(fetchImpl, performanceNow, wallNow) {
  const root = {
    AbortController: AbortController,
    fetch: fetchImpl,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    setInterval: setInterval,
    clearInterval: clearInterval,
    performance: { now: performanceNow }
  };
  const context = {
    window: root,
    Date: { now: wallNow },
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    setInterval: setInterval,
    clearInterval: clearInterval
  };
  vm.createContext(context);
  vm.runInContext(source, context, { filename: sourcePath });
  return root.NetworkData;
}

(async function () {
''' + body + r'''
}()).catch(function (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exitCode = 1;
});
'''
        result = subprocess.run(
            [NODE, "-e", harness, str(NETWORK_DATA_SCRIPT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_r4_history_cache_uses_monotonic_time(self):
        self.run_network_data(r'''
  let fetchCount = 0;
  const performanceValues = [1000, 0, 1];
  const wallValues = [100000, 0, 1];
  const api = loadNetworkData(function () {
    fetchCount += 1;
    return Promise.resolve(jsonResponse({ bucket_s: 60, buckets: [] }));
  }, function () {
    return performanceValues.shift();
  }, function () {
    return wallValues.shift();
  });

  await api.fetchHistory("spot_rest", "status", "1h");
  await api.fetchHistory("spot_rest", "status", "1h");
  assert.strictEqual(fetchCount, 2);
''')

    def test_r4_current_resets_a_lower_event_cursor(self):
        self.run_network_data(r'''
  const paths = [];
  const replies = [
    { events: [], next_after: 10 },
    { events: [{ event_id: 1 }] },
    { events: [], next_after: 0 }
  ];
  const api = loadNetworkData(function (path) {
    paths.push(path);
    return Promise.resolve(jsonResponse(replies.shift()));
  }, function () { return 0; }, function () { return 0; });

  await api.fetchEvents();
  await api.fetchCurrent();
  await api.fetchEvents();
  assert.strictEqual(paths[2], "/api/network/events?after=0&limit=50");
''')

    def test_r4_post_methods_resolve_with_error_results(self):
        self.run_network_data(r'''
  const errors = [];
  const api = loadNetworkData(function () {
    return Promise.reject(new Error("offline"));
  }, function () { return 0; }, function () { return 0; });
  api.subscribe("error", function (value) { errors.push(value); });

  const config = await api.postConfig("rest_probe_s", 15);
  const probe = await api.postProbe("spot_rest");
  assert.strictEqual(config.ok, false);
  assert.strictEqual(config.error, "offline");
  assert.strictEqual(probe.ok, false);
  assert.strictEqual(probe.error, "offline");
  assert.strictEqual(errors.length, 2);
''')


if __name__ == "__main__":
    unittest.main()
