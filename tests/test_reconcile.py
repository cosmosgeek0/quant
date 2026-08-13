"""Offline tests for basis_executor --reconcile — injected transport only."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_executor  # noqa: E402
from basis_executor import ExecutorConfig  # noqa: E402
from tests.test_basis_executor import (  # noqa: E402
    RID,
    SHA,
    SYMBOL,
    ScriptedTransport,
    happy_spec,
)

SOURCE_RID = "feed01"


class ReconcileCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = holder.name
        self.cfg = ExecutorConfig(symbol=SYMBOL, sha=SHA,
                                  receipt_dir=self.dir, run_id=RID)

    def seed_exposure(self, net_base="0.09970000", symbol=SYMBOL,
                      with_lock=True):
        source = {
            "verdict": "OPEN_EXPOSURE",
            "acquired": {"executed_qty": "0.01000000", "base_fee": "0.00001",
                         "net_base": net_base, "hedge_quantity": "0"},
        }
        self.source_path = os.path.join(
            self.dir, "basis-executor-%s.json" % SOURCE_RID)
        with open(self.source_path, "w", encoding="utf-8") as handle:
            json.dump(source, handle)
        with open(os.path.join(self.dir, "events.jsonl"), "a",
                  encoding="utf-8") as handle:
            handle.write(json.dumps({"run_id": SOURCE_RID, "symbol": symbol,
                                     "kind": "gap_read"}) + "\n")
        if with_lock:
            with open(os.path.join(self.dir, "basis-executor.lock"), "w",
                      encoding="utf-8") as handle:
                handle.write(SOURCE_RID)

    def reconcile(self, spec):
        transport = ScriptedTransport(spec)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = basis_executor.reconcile_once(self.cfg, transport,
                                                 sleep=lambda seconds: None)
        return code, transport, buffer.getvalue()

    def lock_exists(self):
        return os.path.exists(os.path.join(self.dir, "basis-executor.lock"))

    def source(self):
        with open(self.source_path, encoding="utf-8") as handle:
            return json.load(handle)


def sell_spec(executed="0.09970000"):
    spec = happy_spec()
    spec["legs"] = {"exit_spot": {
        "submit": ("ok", {}),
        "query": [("ok", {"clientOrderId": "bc-%s-exit_spot" % RID,
                          "status": "FILLED", "orderId": 7777,
                          "executedQty": executed,
                          "cummulativeQuoteQty": "9.0"})],
    }}
    return spec


class ReconcileTests(ReconcileCase):
    def test_nothing_pending_no_lock_is_clean(self):
        code, transport, out = self.reconcile(happy_spec())
        self.assertEqual(code, 0)
        self.assertEqual(transport.posts, [])
        self.assertIn("nothing pending", out)

    def test_lock_without_receipt_demands_a_human(self):
        with open(os.path.join(self.dir, "basis-executor.lock"), "w",
                  encoding="utf-8") as handle:
            handle.write("stale")
        code, transport, _ = self.reconcile(happy_spec())
        self.assertEqual(code, 3)
        self.assertEqual(transport.posts, [])
        self.assertTrue(self.lock_exists())

    def test_full_sale_frees_the_lane(self):
        self.seed_exposure()
        code, transport, out = self.reconcile(sell_spec())
        self.assertEqual(code, 0)
        self.assertEqual(len(transport.posts), 1)
        self.assertIn("lane freed", out)
        self.assertFalse(self.lock_exists())
        self.assertEqual(self.source()["reconciled_by"], RID)

    def test_dust_residual_accepted_without_selling(self):
        self.seed_exposure(net_base="0.00000900")
        code, transport, out = self.reconcile(happy_spec())
        self.assertEqual(code, 0)
        self.assertEqual(transport.posts, [])
        self.assertIn("dust accepted", out)
        self.assertFalse(self.lock_exists())
        self.assertEqual(self.source()["reconciled_by"], RID)

    def test_symbol_mismatch_refuses_before_any_order(self):
        self.seed_exposure(symbol="OTHERUSDT")
        code, transport, _ = self.reconcile(happy_spec())
        self.assertEqual(code, 3)
        self.assertEqual(transport.posts, [])
        self.assertTrue(self.lock_exists())

    def test_unknown_sale_keeps_the_lock(self):
        self.seed_exposure()
        spec = happy_spec()
        spec["legs"] = {"exit_spot": {
            "submit": ("unclear", {"error": "TimeoutError"}),
            "query": [("unclear", {"error": "TimeoutError"})] * 9,
        }}
        code, transport, out = self.reconcile(spec)
        self.assertEqual(code, 4)
        self.assertIn("UNKNOWN", out)
        self.assertTrue(self.lock_exists())
        self.assertNotIn("reconciled_by", self.source())


if __name__ == "__main__":
    unittest.main()
