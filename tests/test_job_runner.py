#!/usr/bin/env python3
"""Offline deterministic tests for the restricted live-test job runner."""

import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import job_runner as jr  # noqa: E402


NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
SHA = "7ac26aa771937f802930dac5eb017d6583d968d8"


def utc(value):
    return value.isoformat().replace("+00:00", "Z")


class LeverageTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, base_url, path, params=None, signed=False):
        self.calls.append((method, path, dict(params or {}), signed))
        if method != "GET" or path != "/fapi/v1/symbolConfig":
            raise AssertionError("unexpected leverage transport call")
        symbol = (params or {}).get("symbol", "")
        return "ok", [{"symbol": symbol, "leverage": "1"}]


class JobRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.receipt_dir = temporary.name
        self.jobs_dir = os.path.join(self.receipt_dir, "jobs")
        os.makedirs(self.jobs_dir)
        self.hand_calls = []
        self.basis_calls = []
        self.hand_verdict = "CLOSED"
        self.hand_exit = 0
        self.leverage = LeverageTransport()

        env = mock.patch.dict(os.environ, {
            "RECEIPT_DIR": self.receipt_dir,
            "QUANT_SHA": SHA,
            "BINANCE_API_KEY": "offline-key",
            "BINANCE_API_SECRET": "offline-secret",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)

        factory = mock.patch.object(
            jr.set_leverage_1x, "live_request_factory",
            return_value=self.leverage,
        )
        factory.start()
        self.addCleanup(factory.stop)

        futures = mock.patch.object(jr.futures_hand, "main", self.fake_futures)
        futures.start()
        self.addCleanup(futures.stop)
        basis = mock.patch.object(jr.basis_executor, "main", self.fake_basis)
        basis.start()
        self.addCleanup(basis.stop)

        def deny(*_args, **_kwargs):
            raise AssertionError("offline job-runner test attempted network")

        for target, name in ((urllib.request, "urlopen"),
                             (socket, "socket"),
                             (socket, "create_connection")):
            patcher = mock.patch.object(target, name, deny)
            patcher.start()
            self.addCleanup(patcher.stop)

    def job(self, job_id, **overrides):
        value = {
            "job_id": job_id,
            "op": "FUTURES_ROUNDTRIP",
            "symbol": "TSTUSDT",
            "max_notional": 6,
            "created_utc": utc(NOW - timedelta(minutes=1)),
            "expires_utc": utc(NOW + timedelta(minutes=10)),
            "requested_by": "lead",
        }
        value.update(overrides)
        path = os.path.join(self.jobs_dir, "job-%s.json" % job_id)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return path

    def fake_futures(self):
        index = len(self.hand_calls) + 1
        self.hand_calls.append({
            "argv": list(sys.argv),
            "symbol": os.environ.get("SYMBOL"),
            "max_notional": os.environ.get("MAX_NOTIONAL"),
            "sha": os.environ.get("QUANT_SHA"),
            "module_symbol": jr.futures_hand.SYMBOL,
            "module_max_notional": jr.futures_hand.MAX_NOTIONAL_RAW,
        })
        path = os.path.join(
            self.receipt_dir, "futures-hand-offline%s.json" % index
        )
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"verdict": self.hand_verdict}, handle)
        jr.futures_hand.receipt_path = path
        raise SystemExit(self.hand_exit)

    def fake_basis(self, argv):
        index = len(self.basis_calls) + 1
        self.basis_calls.append({
            "argv": list(argv),
            "symbol": os.environ.get("SYMBOL"),
            "max_notional": os.environ.get("MAX_NOTIONAL"),
            "executor_cap": str(jr.basis_executor.MAX_CYCLE_QUOTE),
            "cycle_cap": str(jr.basis_cycle.MAX_CYCLE_QUOTE),
        })
        path = os.path.join(
            self.receipt_dir, "basis-executor-offline%s.json" % index
        )
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"verdict": "CLOSED"}, handle)
        return 0

    def tick(self):
        return jr.tick(receipt_dir=self.receipt_dir, now=NOW)

    def results(self):
        path = os.path.join(self.jobs_dir, "results.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def reason(self, row):
        with open(row["receipt_path"], encoding="utf-8") as handle:
            return json.load(handle)["reason"]

    def test_unknown_field_is_recorded_and_never_executed(self):
        self.job("unknown", extra="not allowed")
        self.assertEqual(self.tick(), 2)
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.results()[0]["verdict"], "REFUSED")
        self.assertEqual(self.reason(self.results()[0]), "UNKNOWN_FIELD")

    def test_bad_type_is_recorded_and_never_executed(self):
        self.job("badtype", max_notional="6")
        self.assertEqual(self.tick(), 2)
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.reason(self.results()[0]), "BAD_TYPE")

    def test_bad_op_is_recorded_and_never_executed(self):
        self.job("badop", op="TRANSFER")
        self.assertEqual(self.tick(), 2)
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.reason(self.results()[0]), "BAD_OP")

    def test_oversize_notional_is_clamped_to_eight(self):
        self.job("clamped", max_notional=800)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual(self.hand_calls[0]["argv"], [
            "futures_hand.py", "--short", "--execute"
        ])
        self.assertEqual(self.hand_calls[0]["max_notional"], "8")
        self.assertEqual(self.hand_calls[0]["module_max_notional"], "8")
        self.assertEqual(self.hand_calls[0]["sha"], SHA)
        self.assertEqual(len(self.leverage.calls), 2)

    def test_missing_expiry_is_recorded_and_never_executed(self):
        path = self.job("noexpiry")
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        del value["expires_utc"]
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        self.assertEqual(self.tick(), 2)
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.reason(self.results()[0]), "MISSING_FIELD")

    def test_expired_job_is_recorded_and_never_executed(self):
        self.job(
            "expired",
            created_utc=utc(NOW - timedelta(minutes=14)),
            expires_utc=utc(NOW - timedelta(minutes=1)),
        )
        self.assertEqual(self.tick(), 2)
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.reason(self.results()[0]), "EXPIRED")

    def test_claim_once_same_file_never_executes_twice(self):
        self.job("once")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual(len(self.results()), 1)
        claim = os.path.join(self.jobs_dir, "claims", "job-once.claim")
        self.assertTrue(os.path.isfile(claim))

    def test_tick_consumes_at_most_one_due_job(self):
        self.job("a")
        self.job("b")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual([row["job_id"] for row in self.results()], ["a"])
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.hand_calls), 2)
        self.assertEqual([row["job_id"] for row in self.results()], ["a", "b"])

    def test_stop_jobs_halts_without_claiming(self):
        self.job("stopped")
        stop = os.path.join(self.receipt_dir, "STOP-JOBS")
        Path(stop).touch()
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.results(), [])
        self.assertFalse(os.path.exists(os.path.join(
            self.jobs_dir, "claims", "job-stopped.claim"
        )))

    def test_unknown_touches_stop_and_blocks_next_job(self):
        self.job("a")
        self.job("b")
        self.hand_verdict = "UNKNOWN"
        self.hand_exit = 4
        self.assertEqual(self.tick(), 4)
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))
        self.hand_verdict = "CLOSED"
        self.hand_exit = 0
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual([row["job_id"] for row in self.results()], ["a"])

    def test_clearing_stop_cannot_bypass_unreconciled_unknown_receipt(self):
        self.job("a")
        self.job("b")
        self.hand_verdict = "UNKNOWN"
        self.hand_exit = 4
        self.assertEqual(self.tick(), 4)
        os.unlink(os.path.join(self.receipt_dir, "STOP-JOBS"))
        self.hand_verdict = "CLOSED"
        self.hand_exit = 0
        self.assertEqual(self.tick(), 3)
        self.assertEqual(len(self.hand_calls), 1)
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_unreconciled_lock_at_start_freezes_queue(self):
        self.job("locked")
        Path(os.path.join(self.receipt_dir, "futures-hand.lock")).touch()
        self.assertEqual(self.tick(), 3)
        self.assertEqual(self.hand_calls, [])
        self.assertTrue(os.path.exists(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_basis_is_sequence_locked_then_accepted_after_marker(self):
        self.job("basis1", op="BASIS_ROUNDTRIP", max_notional=99)
        self.assertEqual(self.tick(), 2)
        first = self.results()[0]
        self.assertEqual(first["verdict"], "REFUSED")
        self.assertEqual(self.reason(first), "SEQUENCE_LOCKED")
        self.assertEqual(self.basis_calls, [])

        Path(os.path.join(self.jobs_dir, "futures_proof_passed")).touch()
        self.job("basis2", op="BASIS_ROUNDTRIP", max_notional=99)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.basis_calls), 1)
        self.assertEqual(self.basis_calls[0]["argv"], ["--execute"])
        self.assertEqual(self.basis_calls[0]["executor_cap"], "8")
        self.assertEqual(self.basis_calls[0]["cycle_cap"], "8")

    def test_closed_futures_writes_proof_marker(self):
        self.job("proof")
        self.assertEqual(self.tick(), 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.jobs_dir, "futures_proof_passed"
        )))

    def test_results_line_has_exact_required_shape(self):
        self.job("shape")
        self.assertEqual(self.tick(), 0)
        row = self.results()[0]
        self.assertEqual(set(row), {
            "job_id", "op", "symbol", "verdict", "exit_code",
            "receipt_path", "utc",
        })
        self.assertEqual(row["job_id"], "shape")
        self.assertEqual(row["op"], "FUTURES_ROUNDTRIP")
        self.assertEqual(row["symbol"], "TSTUSDT")
        self.assertEqual(row["verdict"], "CLOSED")
        self.assertEqual(row["exit_code"], 0)
        self.assertTrue(os.path.isfile(row["receipt_path"]))


if __name__ == "__main__":
    unittest.main()
