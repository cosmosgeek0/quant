"""Offline regressions for wallet receipt reconciliation."""

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


def locked_receipt(root, receipt):
    store = wallet.ReceiptStore(root)
    if not store.acquire_lock(receipt["request_id"]):
        raise RuntimeError("temporary receipt lock was unavailable")
    path = store.create("transfer", receipt["request_id"], receipt)
    return store, path


def current_receipt(marker):
    request_id = "l3w" + marker * 20
    intent = transfer.build_transfer(
        "spot_to_usdm", "USDT", Decimal("2"), request_id, SHA)
    return {
        "kind": "internal_transfer", "request_id": request_id, "sha": SHA,
        "direction": "spot-to-usdm", "transfer_type": "MAIN_UMFUTURE",
        "asset": "USDT", "amount": "2", "history_start_ms": 0,
        "baseline_tran_ids": [], "state": "UNKNOWN",
        "mutation_attempts": 1, "intent": wallet._intent_record(intent),
    }


class WalletReconciliationTests(unittest.TestCase):
    def test_legacy_receipt_queries_terminal_truth_without_submission(self):
        started = 1700000000000
        request_id = "l3w" + "1" * 20
        receipt = {
            "kind": "internal_transfer", "request_id": request_id, "sha": SHA,
            "direction": "spot-to-usdm", "transfer_type": "MAIN_UMFUTURE",
            "asset": "USDT", "amount": "2", "started_ms": started,
            "submission_started_ms": started + 1, "state": "UNKNOWN",
            "mutation_attempts": 1, "history_start_ms": started - 60000,
            "baseline_tran_ids": ["41"], "tran_id": "42",
            "idempotency": {
                "local_request_id": request_id,
                "exchange_client_id_supported": False,
                "rule": "single POST; reconcile history before any new action",
            },
        }
        calls = []

        def transport(method, _base, path, _params, _signed):
            calls.append((method, path))
            if path == "/sapi/v1/asset/transfer":
                row = {"tranId": 42, "asset": "USDT", "amount": "2",
                       "status": "CONFIRMED", "timestamp": started + 2,
                       "type": "MAIN_UMFUTURE"}
                return http.HttpResult("ok", {"total": 1, "rows": [row]}, None, None)
            if path == "/api/v3/account":
                return http.HttpResult("ok", {"balances": [
                    {"asset": "USDT", "free": "8"}]}, None, None)
            if path == "/fapi/v3/balance":
                return http.HttpResult("ok", [
                    {"asset": "USDT", "availableBalance": "4"}], None, None)
            raise AssertionError("unexpected offline transport path")

        with tempfile.TemporaryDirectory() as root:
            store, path = locked_receipt(root, receipt)
            _, result = wallet.reconcile_transfer(
                store, path, receipt, transport, sleep=lambda _seconds: None)
            saved = json.loads(Path(path).read_text())
            self.assertFalse(Path(store.lock_path).exists())

        self.assertEqual(result["state"], "CONFIRMED")
        self.assertEqual(saved["history_query"], {
            "kind": "ok", "receipt_format": "legacy", "state": "found"})
        self.assertEqual(sum(method == "POST" for method, _path in calls), 0)
        self.assertEqual(sum(path == "/sapi/v1/asset/transfer"
                             for _method, path in calls), 1)

    def test_reconcile_preserves_query_taxonomy(self):
        api_error = http.HttpResult("api_error", None, -2015, "permission refused")
        unclear = http.HttpResult("unclear", None, None, "time unavailable")
        cases = (
            ("api_error", transfer.TransferQuery("error", None, api_error),
             "error", "api_error"),
            ("unclear", transfer.TransferQuery("pending", None, unclear),
             "pending", "unclear"),
            ("refusal", http.AdapterRefusal("query input was refused"),
             "not_started", "refusal"),
        )
        for index, (label, outcome, state, kind) in enumerate(cases):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                receipt = current_receipt(format(index + 10, "x")[-1])
                store, path = locked_receipt(root, receipt)
                behavior = ({"side_effect": outcome} if isinstance(outcome, Exception)
                            else {"return_value": outcome})
                with mock.patch.object(
                        wallet.binance_transfer, "query_transfer", **behavior) as query:
                    _, result = wallet.reconcile_transfer(
                        store, path, receipt, mock.Mock())
                saved = json.loads(Path(path).read_text())
                query.assert_called_once()
                self.assertEqual(result["state"], "UNKNOWN")
                self.assertEqual(result["history_query"]["state"], state)
                self.assertEqual(result["history_query"]["kind"], kind)
                self.assertEqual(result["first_failure"]["kind"], kind)
                self.assertEqual(saved, result)
                self.assertTrue(Path(store.lock_path).exists())

    def test_accepted_unresolved_history_persists_and_prints_state(self):
        for index, state in enumerate(("ambiguous", "not_found")):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as root:
                receipt = current_receipt(format(index + 13, "x")[-1])
                receipt["submission_kind"] = "accepted"
                store, path = locked_receipt(root, receipt)
                outcome = transfer.TransferQuery(state, None, None)
                with mock.patch.object(
                        wallet.binance_transfer, "query_transfer",
                        return_value=outcome):
                    _, result = wallet.reconcile_transfer(
                        store, path, receipt, mock.Mock())
                saved = json.loads(Path(path).read_text())
                emitted = []
                with mock.patch.object(wallet, "emit", side_effect=emitted.append):
                    wallet.print_transfer(saved)

                self.assertEqual(result["state"], "UNKNOWN")
                self.assertEqual(saved, result)
                self.assertEqual(saved["first_failure"]["kind"], "ok")
                self.assertEqual(saved["first_failure"]["state"], state)
                self.assertEqual(
                    emitted[-1], {"first_failure": saved["first_failure"]})
                self.assertTrue(Path(store.lock_path).exists())

    def test_prior_submit_failure_prints_latest_history_query(self):
        receipt = current_receipt("8")
        receipt["submission_kind"] = "unclear"
        wallet.first_failure(receipt, "transfer_submit", "unclear")
        prior_failure = dict(receipt["first_failure"])
        with tempfile.TemporaryDirectory() as root:
            store, path = locked_receipt(root, receipt)
            outcome = transfer.TransferQuery("not_found", None, None)
            with mock.patch.object(
                    wallet.binance_transfer, "query_transfer",
                    return_value=outcome):
                _, result = wallet.reconcile_transfer(
                    store, path, receipt, mock.Mock())
            extra = "x" * 500
            long_error = "e" * 500
            result["history_query"]["extra"] = extra
            result["history_query"]["error"] = long_error
            store.save(path, result)
            _, saved = store.load_transfer(receipt["request_id"])
            emitted = []
            with mock.patch.object(wallet, "emit", side_effect=emitted.append):
                wallet.print_transfer(saved)

            self.assertEqual(saved, result)
            self.assertEqual(saved["first_failure"], prior_failure)
            self.assertEqual(
                saved["history_query"], {
                    "error": long_error,
                    "extra": extra,
                    "kind": "ok",
                    "state": "not_found",
                })
            self.assertEqual(
                emitted[-2], {"history_query": {
                    "error": long_error[:80],
                    "kind": "ok",
                    "state": "not_found",
                }})
            self.assertEqual(
                emitted[-1], {"first_failure": saved["first_failure"]})
            self.assertTrue(Path(store.lock_path).exists())

    def test_boolean_attempt_marker_stays_unqueried(self):
        receipt = current_receipt("f")
        receipt["mutation_attempts"] = True
        with tempfile.TemporaryDirectory() as root:
            store, path = locked_receipt(root, receipt)
            row = transfer.TransferRow(
                "MAIN_UMFUTURE", "USDT", Decimal("2"), 42, "CONFIRMED", 1)
            outcome = transfer.TransferQuery("found", row, None)
            balances = http.ReadResult(
                "ok", transfer.TransferBalances(
                    "USDT", Decimal("8"), Decimal("4")), None)
            with mock.patch.object(
                    wallet.binance_transfer, "query_transfer",
                    return_value=outcome) as query, mock.patch.object(
                    wallet, "_transfer_balances",
                    return_value=balances) as balance_read:
                _, result = wallet.reconcile_transfer(
                    store, path, receipt, mock.Mock())
            saved = json.loads(Path(path).read_text())
            emitted = []
            with mock.patch.object(wallet, "emit", side_effect=emitted.append):
                wallet.print_transfer(saved)

            query.assert_not_called()
            balance_read.assert_not_called()
            self.assertEqual(result["state"], "UNKNOWN")
            self.assertEqual(saved, result)
            self.assertIs(saved["mutation_attempts"], True)
            self.assertEqual(
                saved["history_query"],
                {"kind": "refusal", "state": "not_started"},
            )
            self.assertEqual(saved["first_failure"]["kind"], "refusal")
            self.assertEqual(
                emitted[-2], {"history_query": saved["history_query"]})
            self.assertEqual(
                emitted[-1], {"first_failure": saved["first_failure"]})
            self.assertTrue(Path(store.lock_path).exists())

    def test_invalid_integer_attempt_markers_are_typed_refusals(self):
        for marker, attempts in (("a", -1), ("b", 2)):
            with self.subTest(attempts=attempts), tempfile.TemporaryDirectory() as root:
                receipt = current_receipt(marker)
                receipt["mutation_attempts"] = attempts
                store, path = locked_receipt(root, receipt)
                with mock.patch.object(
                        wallet.binance_transfer, "query_transfer") as query:
                    _, result = wallet.reconcile_transfer(
                        store, path, receipt, mock.Mock())
                saved = json.loads(Path(path).read_text())

                query.assert_not_called()
                self.assertEqual(saved, result)
                self.assertEqual(saved["mutation_attempts"], attempts)
                self.assertEqual(saved["state"], "UNKNOWN")
                self.assertEqual(
                    saved["history_query"],
                    {"kind": "refusal", "state": "not_started"},
                )
                self.assertEqual(saved["first_failure"]["kind"], "refusal")
                self.assertTrue(Path(store.lock_path).exists())

    def test_zero_attempt_marker_stays_untouched(self):
        receipt = current_receipt("9")
        receipt["mutation_attempts"] = 0
        with tempfile.TemporaryDirectory() as root:
            store, path = locked_receipt(root, receipt)
            with mock.patch.object(
                    wallet.binance_transfer, "query_transfer") as query:
                _, result = wallet.reconcile_transfer(
                    store, path, receipt, mock.Mock())
            saved = json.loads(Path(path).read_text())

            query.assert_not_called()
            self.assertEqual(saved, result)
            self.assertNotIn("history_query", saved)
            self.assertNotIn("first_failure", saved)
            self.assertTrue(Path(store.lock_path).exists())


if __name__ == "__main__":
    unittest.main()
