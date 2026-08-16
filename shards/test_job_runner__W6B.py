#!/usr/bin/env python3
"""Focused typed-Margin regressions for the job-runner W6B shard."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import job_runner as jr  # noqa: E402
from execution import binance_http, binance_margin  # noqa: E402


NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
SHA = "7ac26aa771937f802930dac5eb017d6583d968d8"


def utc(value):
    return value.isoformat().replace("+00:00", "Z")


def ok(payload):
    return binance_http.HttpResult("ok", payload, None, None)


class MarginWire:
    """Serve typed adapter fixtures and record the sole possible POST."""

    def __init__(self, direction, before, after, durable_check=None,
                 concurrent=False):
        self.direction = direction
        self.before = before
        self.after = after
        self.durable_check = durable_check
        self.concurrent = concurrent
        self.calls = []
        self.posted = False
        self.post_params = None

    @staticmethod
    def account(liability):
        return {"userAssets": [{
            "asset": liability.asset,
            "borrowed": format(liability.principal, "f"),
            "interest": format(liability.interest, "f"),
        }]}

    def target_row(self):
        return {
            "txId": 7301,
            "asset": "USDT",
            "amount": format(
                self.after.principal - self.before.principal
                if self.direction == "BORROW" else self.before.total,
                "f",
            ),
            "principal": format(
                self.after.principal - self.before.principal
                if self.direction == "BORROW" else self.before.principal,
                "f",
            ),
            "interest": "0" if self.direction == "BORROW" else format(
                self.before.interest, "f"
            ),
            "status": "CONFIRMED",
            "type": "MANUAL",
            "timestamp": 1786872000000,
        }

    @staticmethod
    def unrelated_row():
        return {
            "txId": 7302, "asset": "USDT", "amount": "1",
            "principal": "1", "interest": "0", "status": "CONFIRMED",
            "type": "MANUAL", "timestamp": 1786872000001,
        }

    def __call__(self, method, base_url, path, params=None, signed=False):
        params = dict(params or ())
        self.calls.append((method, base_url, path, params, signed))
        if base_url != binance_http.SPOT_BASE_URL or signed is not True:
            raise AssertionError("Margin wire was not a signed Spot call")
        if method == "POST":
            if self.durable_check is not None:
                self.durable_check()
            self.posted = True
            self.post_params = params
            return ok({"tranId": 7301})
        if path.endswith("/account"):
            return ok(self.account(self.after if self.posted else self.before))
        if method != "GET":
            raise AssertionError("Margin wire method was not expected")
        direction = params.get("type")
        rows = []
        if self.posted and direction == self.direction:
            rows.append(self.target_row())
        if self.posted and self.concurrent and direction != self.direction:
            rows.append(self.unrelated_row())
        tx_id = params.get("txId")
        if tx_id is not None:
            rows = [row for row in rows if str(row["txId"]) == tx_id]
        return ok({"total": len(rows), "rows": rows})

    @property
    def post_count(self):
        return sum(call[0] == "POST" for call in self.calls)


class JobRunnerMarginShardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.receipt_dir = temporary.name
        self.paths = jr._paths(self.receipt_dir)
        jr._make_dirs(self.paths)
        Path(self.paths["operator"]).write_text(
            json.dumps({"operator": "lead"}), encoding="utf-8"
        )
        Path(self.paths["accept_one"]).write_text(json.dumps({
            "operator": "lead",
            "created_utc": utc(NOW - timedelta(minutes=1)),
            "expires_utc": utc(NOW + timedelta(minutes=10)),
            "max_notional": "32",
        }), encoding="utf-8")

    def job(self, job_id, direction, amount):
        op = "MARGIN_" + direction
        raw = {
            "job_id": job_id,
            "op": op,
            "asset": "USDT",
            "principal": amount,
            "intent_id": "mrgintent-" + hashlib.sha256(
                job_id.encode("ascii")
            ).hexdigest()[:24],
            "created_utc": utc(NOW - timedelta(minutes=1)),
            "expires_utc": utc(NOW + timedelta(minutes=10)),
            "requested_by": "lead",
            "operator_token": "lead",
        }
        raw["client_tag"] = jr.margin_ops.deterministic_client_tag(
            op, raw["asset"], amount, raw["expires_utc"]
        )
        encoded = (json.dumps(raw, separators=(",", ":")) + "\n").encode()
        path = Path(self.paths["jobs"]) / ("job-%s.json" % job_id)
        path.write_bytes(encoded)
        return (
            jr._validate_job(raw, job_id, NOW, self.receipt_dir),
            hashlib.sha256(encoded).hexdigest(),
        )

    @staticmethod
    def services(wire, waits=None):
        waits = waits if waits is not None else []
        with mock.patch.dict(os.environ, {
                "BINANCE_API_KEY": "offline-key",
                "BINANCE_API_SECRET": "offline-secret",
        }, clear=False):
            table = jr._build_execution_services()
        return replace(table, transport=wire, sleep=waits.append)

    def run_margin(self, job, digest, services, governor_token=""):
        with mock.patch.object(jr, "_utc_now", return_value=NOW):
            return jr._run_margin_operation(
                job, self.paths, governor_token=governor_token,
                sha=SHA, validated_job_digest=digest,
                execution_services=services,
            )

    def source(self, outcome):
        return json.loads(Path(outcome.receipt_path).read_text(
            encoding="utf-8"
        ))

    def test_borrow_intent_is_durable_before_one_adapter_post(self):
        job, digest = self.job("typed-borrow", "BORROW", "2.5")
        zero = binance_margin.Liability(
            "USDT", Decimal("0"), Decimal("0"), Decimal("0")
        )
        after = binance_margin.Liability(
            "USDT", Decimal("2.5"), Decimal("0"), Decimal("2.5")
        )
        source_path = Path(self.paths["receipts"]) / "margin-typed-borrow.json"

        def durable_check():
            receipt = json.loads(source_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["mutation_attempts"], 1)
            self.assertEqual(receipt["sha"], SHA)
            self.assertEqual(receipt["ready"]["intent_id"], job.intent_id)

        wire = MarginWire("BORROW", zero, after, durable_check)
        outcome = self.run_margin(
            job, digest, self.services(wire), "governor-token-1"
        )
        self.assertEqual((outcome.verdict, outcome.exit_code), ("OPEN", 0))
        self.assertEqual(wire.post_count, 1)
        self.assertEqual(wire.post_params, {
            "asset": "USDT", "amount": "2.5", "type": "BORROW",
        })
        self.assertEqual(self.source(outcome)["snapshot_time_note"],
                         jr.MARGIN_SNAPSHOT_TIME_UNVERIFIED)

    def test_repay_uses_exact_typed_liability_split(self):
        job, digest = self.job("typed-repay", "REPAY", "2.5000387")
        before = binance_margin.Liability(
            "USDT", Decimal("2.5"), Decimal("0.0000387"),
            Decimal("2.5000387"),
        )
        zero = binance_margin.Liability(
            "USDT", Decimal("0"), Decimal("0"), Decimal("0")
        )
        wire = MarginWire("REPAY", before, zero)
        outcome = self.run_margin(job, digest, self.services(wire))
        self.assertEqual((outcome.verdict, outcome.exit_code), ("CLOSED", 0))
        self.assertEqual(wire.post_count, 1)
        self.assertEqual(wire.post_params["amount"], "2.5000387")
        self.assertEqual(self.source(outcome)["ledger_confirmation"], {
            "asset": "USDT", "amount": "2.5000387",
            "principal": "2.5", "interest": "0.0000387",
            "status": "REPAY_CONFIRMED",
        })

    def test_intent_writer_failure_sends_no_post(self):
        job, digest = self.job("writer-safe", "BORROW", "2.5")
        zero = binance_margin.Liability(
            "USDT", Decimal("0"), Decimal("0"), Decimal("0")
        )
        after = binance_margin.Liability(
            "USDT", Decimal("2.5"), Decimal("0"), Decimal("2.5")
        )
        wire = MarginWire("BORROW", zero, after)
        original = jr._persist_margin_source
        calls = []

        def fail_writer(path, payload, create=False):
            calls.append(path)
            if len(calls) == 3:
                raise OSError("offline persistence refusal")
            return original(path, payload, create=create)

        with mock.patch.object(jr, "_persist_margin_source",
                               side_effect=fail_writer):
            outcome = self.run_margin(
                job, digest, self.services(wire), "governor-token-1"
            )
        self.assertEqual((outcome.verdict, outcome.exit_code), ("UNKNOWN", 4))
        self.assertEqual(outcome.reason, "MARGIN_READY_PERSISTENCE_FAILED")
        self.assertEqual(wire.post_count, 0)

    def test_cross_direction_identity_fence_stays_unknown(self):
        job, digest = self.job("typed-fence", "BORROW", "2.5")
        zero = binance_margin.Liability(
            "USDT", Decimal("0"), Decimal("0"), Decimal("0")
        )
        after = binance_margin.Liability(
            "USDT", Decimal("2.5"), Decimal("0"), Decimal("2.5")
        )
        wire = MarginWire("BORROW", zero, after, concurrent=True)
        outcome = self.run_margin(
            job, digest, self.services(wire), "governor-token-1"
        )
        self.assertEqual(outcome.reason, "CONCURRENT_MARGIN_ACTIVITY")
        self.assertEqual((outcome.verdict, wire.post_count), ("UNKNOWN", 1))

    def test_legacy_receipt_truth_uses_typed_reads_only(self):
        before = binance_margin.Liability(
            "USDT", Decimal("0"), Decimal("0"), Decimal("0")
        )
        current = binance_margin.Liability(
            "USDT", Decimal("2.5"), Decimal("0"), Decimal("2.5")
        )
        wire = MarginWire("BORROW", before, current)
        wire.posted = True

        def no_submit(*_args, **_kwargs):
            raise AssertionError("legacy reconciliation tried to submit")

        services = replace(
            self.services(wire), margin_submit_once=no_submit
        )
        reason, history = jr._read_margin_history_identity(
            services, "USDT", "BORROW", "7301"
        )
        self.assertEqual(reason, "")
        reason, _histories, identifiers, proof = \
            jr._read_margin_legacy_exclusivity(
                services, "USDT", "7301",
                utc(NOW - timedelta(seconds=10)), NOW,
            )
        liability_reason, liability = jr._read_margin_liability(
            services, "USDT"
        )
        confirmation = jr._evaluate_legacy_margin_confirmation(
            history, identifiers, "7301", {
                "asset": "USDT", "principal": "2.5", "interest": "0",
                "status": "BORROW_CONFIRMED",
            }, "BORROW", liability, proof,
        )
        self.assertEqual(
            (reason, liability_reason),
            ("LEGACY_WINDOW_NOT_EXCLUSIVE", ""),
        )
        self.assertEqual(
            (confirmation.verdict, confirmation.reason),
            ("UNKNOWN", "LEGACY_WINDOW_NOT_EXCLUSIVE"),
        )
        self.assertTrue(all(call[0] == "GET" for call in wire.calls))


if __name__ == "__main__":
    unittest.main()
