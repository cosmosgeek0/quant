"""Offline integration tests for wallet receipt and transfer policy."""

import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import wallet  # noqa: E402
from execution import binance_http as http  # noqa: E402
from execution import binance_transfer as transfer  # noqa: E402


SHA = "a" * 40
SPOT_BALANCES = {"balances": [
    {"asset": "USDT", "free": "8.2500"},
    {"asset": "BTC", "free": "0.1"},
]}
FUTURES_BALANCES = [
    {"asset": "USDT", "availableBalance": "7.500"},
    {"asset": "BTC", "availableBalance": "0.2"},
]


def http_result(kind, payload=None, code=None, msg=None):
    return http.HttpResult(kind, payload, code, msg)


def history_row(tran_id=101, status="CONFIRMED"):
    return {"tranId": tran_id, "asset": "USDT", "amount": "2",
            "status": status, "timestamp": 1700000000000,
            "type": "MAIN_UMFUTURE"}


def typed_row(tran_id=101, status="CONFIRMED"):
    return transfer.TransferRow(
        "MAIN_UMFUTURE", "USDT", Decimal("2"), tran_id, status,
        1700000000000,
    )


def balance_result():
    return http.ReadResult("ok", transfer.TransferBalances(
        "USDT", Decimal("8.2500"), Decimal("7.500")), None)


def transfer_receipt(request_id, submission_kind=None):
    intent = transfer.build_transfer(
        "spot_to_usdm", "USDT", Decimal("2"), request_id, SHA
    )
    receipt = {
        "kind": "internal_transfer", "request_id": request_id, "sha": SHA,
        "direction": "spot-to-usdm", "transfer_type": "MAIN_UMFUTURE",
        "asset": "USDT", "amount": "2", "history_start_ms": 0,
        "baseline_tran_ids": [], "state": "PREPARED",
        "mutation_attempts": 1, "intent": wallet._intent_record(intent),
    }
    if submission_kind is not None:
        receipt["submission_kind"] = submission_kind
    return receipt


def locked_receipt(root, marker="a", submission_kind=None):
    store = wallet.ReceiptStore(root)
    receipt = transfer_receipt("l3w" + marker * 20, submission_kind)
    if not store.acquire_lock(receipt["request_id"]):
        raise RuntimeError("temporary receipt lock was unavailable")
    path = store.create("transfer", receipt["request_id"], receipt)
    return store, path, receipt


class RecordingStore(wallet.ReceiptStore):
    def create(self, kind, request_id, payload):
        self.created = dict(payload)
        return super().create(kind, request_id, payload)


class WalletTransferTests(unittest.TestCase):
    def test_intent_writer_syncs_file_and_directory_and_keeps_order(self):
        with tempfile.TemporaryDirectory() as root:
            store = wallet.ReceiptStore(root)
            request_id = "l3w" + "1" * 20
            receipt = transfer_receipt(request_id)
            receipt.pop("intent")
            receipt["mutation_attempts"] = 0
            store.acquire_lock(request_id)
            path = store.create("transfer", request_id, receipt)
            intent = transfer.build_transfer(
                "spot_to_usdm", "USDT", Decimal("2"), request_id, SHA
            )
            real_fsync = wallet.os.fsync
            with mock.patch.object(wallet.os, "fsync", wraps=real_fsync) as sync:
                store.intent_writer(path, receipt)(intent)
            saved = json.loads(Path(path).read_text())
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(saved["mutation_attempts"], 1)
        self.assertEqual(saved["intent"]["params"],
                         [list(pair) for pair in intent.params])

    def test_execute_transfer_persists_attempt_before_post_and_tran_id(self):
        with tempfile.TemporaryDirectory() as root:
            store = RecordingStore(root)
            calls, post_receipt = [], {}
            history_reads = 0

            def transport(method, base, path, params, signed):
                nonlocal history_reads
                calls.append((method, base, path, params, signed))
                if method == "POST":
                    receipt_path = next(Path(root).glob("transfer-*.json"))
                    post_receipt.update(json.loads(receipt_path.read_text()))
                    return http_result("ok", {"tranId": 321})
                if path == "/api/v3/account":
                    return http_result("ok", SPOT_BALANCES)
                if path == "/fapi/v3/balance":
                    return http_result("ok", FUTURES_BALANCES)
                if path == "/sapi/v1/asset/transfer":
                    history_reads += 1
                    payload = {"total": 0} if history_reads == 1 else {
                        "total": 1, "rows": [history_row(321)]}
                    return http_result("ok", payload)
                raise AssertionError("unexpected offline transport call")

            path, receipt = wallet.execute_transfer(
                store, transport, "spot-to-usdm", "USDT", "2", SHA,
                sleep=lambda _seconds: None,
            )
            saved = json.loads(Path(path).read_text())

        self.assertEqual(store.created["state"], "PREPARED")
        self.assertEqual(store.created["mutation_attempts"], 0)
        self.assertEqual(post_receipt["mutation_attempts"], 1)
        self.assertEqual(post_receipt["intent"]["git_sha"], SHA)
        self.assertEqual(sum(call[0] == "POST" for call in calls), 1)
        self.assertEqual(receipt["tran_id"], "321")
        self.assertEqual(saved["tran_id"], "321")

    def test_reconcile_terminal_rows_release_lock(self):
        for marker, status in (("c", "CONFIRMED"), ("d", "FAILED")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                store, path, receipt = locked_receipt(root, marker)
                query = transfer.TransferQuery(
                    "found", typed_row(301, status), None)
                with mock.patch.object(
                        wallet.binance_transfer, "query_transfer",
                        return_value=query), mock.patch.object(
                        wallet, "_transfer_balances", return_value=balance_result()):
                    _, result = wallet.reconcile_transfer(
                        store, path, receipt, mock.Mock())
                self.assertEqual(result["state"], status)
                self.assertEqual(result["exchange"]["status"], status)
                self.assertEqual(result["after"]["source"]["free"], "8.2500")
                self.assertEqual(result["after"]["destination"]["free"], "7.500")
                self.assertFalse(Path(store.lock_path).exists())

    def test_terminal_history_keeps_truth_when_after_balances_are_unproved(self):
        with tempfile.TemporaryDirectory() as root:
            store, path, receipt = locked_receipt(root, "8")
            query = transfer.TransferQuery("found", typed_row(302), None)
            unclear = http_result("unclear", msg="transfer_balance_schema")
            unreadable = http.ReadResult("unclear", None, unclear)
            with mock.patch.object(
                    wallet.binance_transfer, "query_transfer",
                    return_value=query), mock.patch.object(
                    wallet, "_transfer_balances", return_value=unreadable):
                _, result = wallet.reconcile_transfer(
                    store, path, receipt, mock.Mock())
            self.assertEqual(result["state"], "CONFIRMED")
            self.assertEqual(result["after"]["source"]["status"], "UNKNOWN")
            self.assertIn("first_failure", result)
            self.assertFalse(Path(store.lock_path).exists())

    def test_reconcile_rejected_post_without_row_releases_lock(self):
        with tempfile.TemporaryDirectory() as root:
            store, path, receipt = locked_receipt(root, "e", "api_error")
            with mock.patch.object(
                    wallet.binance_transfer, "query_transfer",
                    return_value=transfer.TransferQuery(
                        "not_found", None, None)):
                _, result = wallet.reconcile_transfer(
                    store, path, receipt, mock.Mock())
            self.assertEqual(result["state"], "REJECTED")
            self.assertFalse(Path(store.lock_path).exists())

    def test_reconcile_unresolved_history_keeps_lock(self):
        error = http_result("api_error", code=-2015, msg="permission refused")
        cases = (
            transfer.TransferQuery("ambiguous", None, None),
            transfer.TransferQuery("pending", None, None),
            transfer.TransferQuery("error", None, error),
            transfer.TransferQuery("found", typed_row(401, "PENDING"), None),
        )
        for index, query in enumerate(cases):
            with self.subTest(kind=query.kind), tempfile.TemporaryDirectory() as root:
                marker = format(index + 10, "x")[-1]
                store, path, receipt = locked_receipt(root, marker)
                with mock.patch.object(
                        wallet.binance_transfer, "query_transfer",
                        return_value=query):
                    _, result = wallet.reconcile_transfer(
                        store, path, receipt, mock.Mock())
                self.assertEqual(result["state"], "UNKNOWN")
                self.assertTrue(Path(store.lock_path).exists())

    def test_validated_transfer_amount_refuses_out_of_policy_values(self):
        cases = (("BTC", "1"), ("USDT", "0"), ("USDT", "-1"),
                 ("USDT", wallet.TRANSFER_HARD_CEILING + Decimal("0.01")))
        for asset, amount in cases:
            with self.subTest(asset=asset, amount=str(amount)):
                with self.assertRaises(ValueError):
                    wallet.validated_transfer_amount(asset, amount)

    def test_transfer_balance_failures_map_blocked_and_unknown_without_post(self):
        cases = (
            (http_result("api_error", code=-2015, msg="permission refused"),
             "BLOCKED"),
            (http_result("ok", {"balances": "invalid"}), "UNKNOWN"),
        )
        for index, (reply, status) in enumerate(cases):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                calls = []

                def transport(method, base, path, params, signed):
                    calls.append((method, path))
                    return reply

                _, receipt = wallet.execute_transfer(
                    wallet.ReceiptStore(root), transport, "spot-to-usdm",
                    "USDT", "2", SHA, sleep=lambda _seconds: None)
                self.assertEqual(receipt["state"], "BLOCKED")
                self.assertEqual(receipt["before"]["source"]["status"], status)
                self.assertNotIn("POST", [call[0] for call in calls])

    def test_time_sync_failure_gates_signed_transport(self):
        wire = mock.Mock(return_value=http_result("ok", {}))
        failure = http_result("unclear", msg="time unavailable")
        transport = wallet._gated_transport(
            wire, {"spot": failure, "usd_m": http_result("ok", {})})
        self.assertIs(transport(
            "GET", wallet.SPOT_BASE, "/private", None, True), failure)
        wire.assert_not_called()
        transport("GET", wallet.SPOT_BASE, "/public", None, False)
        wire.assert_called_once()

    def test_incomplete_legacy_receipt_stays_unknown_without_query(self):
        with tempfile.TemporaryDirectory() as root:
            store, path, receipt = locked_receipt(root, "9")
            receipt.pop("intent")
            store.save(path, receipt)
            with mock.patch.object(
                    wallet.binance_transfer, "query_transfer") as query:
                _, result = wallet.reconcile_transfer(
                    store, path, receipt, mock.Mock())
            self.assertEqual(result["state"], "UNKNOWN")
            self.assertTrue(Path(store.lock_path).exists())
            query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
