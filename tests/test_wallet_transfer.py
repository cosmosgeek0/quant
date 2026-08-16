"""Offline characterization tests for wallet transfer and read behavior."""

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


SPOT_BALANCES = {
    "balances": [
        {"asset": "USDT", "free": "8.2500"},
        {"asset": "BTC", "free": "0.1"},
    ]
}
FUTURES_BALANCES = [
    {"asset": "USDT", "availableBalance": "7.500"},
    {"asset": "BTC", "availableBalance": "0.2"},
]


def history_row(tran_id="101", status="CONFIRMED"):
    return {
        "tranId": tran_id,
        "asset": "USDT",
        "amount": "2",
        "status": status,
        "timestamp": 1700000000000,
        "type": "MAIN_UMFUTURE",
    }


def transfer_receipt(request_id, submission_kind=None):
    receipt = {
        "kind": "internal_transfer",
        "request_id": request_id,
        "direction": "spot-to-usdm",
        "transfer_type": "MAIN_UMFUTURE",
        "asset": "USDT",
        "amount": "2",
        "history_start_ms": 0,
        "baseline_tran_ids": [],
        "state": "PREPARED",
        "mutation_attempts": 1,
    }
    if submission_kind is not None:
        receipt["submission_kind"] = submission_kind
    return receipt


def scripted_client(*responses):
    client = wallet.BinanceClient("offline-key", "offline-secret")
    client.offsets.update({wallet.SPOT_BASE: 0, wallet.FUTURES_BASE: 0})
    transport = mock.Mock(side_effect=list(responses))
    client._once = transport
    return client, transport


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
    def test_signed_once_before_time_sync_sends_nothing(self):
        client = wallet.BinanceClient("offline-key", "offline-secret")
        with mock.patch.object(wallet.urllib.request, "urlopen") as urlopen:
            result = client._once(
                "GET", wallet.SPOT_BASE, "/private", signed=True
            )
        self.assertEqual(
            result, ("unclear", {"error": "TimeNotSynchronized"})
        )
        urlopen.assert_not_called()

    def test_read_retries_only_unclear_with_one_and_two_second_waits(self):
        client, transport = scripted_client(
            ("unclear", {"error": "TimeoutError"}),
            ("unclear", {"http": 503}),
            ("ok", {"value": "ready"}),
        )
        with mock.patch.object(wallet.time, "sleep") as sleep:
            result = client.read(wallet.SPOT_BASE, "/read", signed=True)
        self.assertEqual(result, ("ok", {"value": "ready"}))
        self.assertEqual(transport.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])

    def test_read_stops_on_ok_or_api_error(self):
        for kind, payload in (
            ("ok", {"value": "ready"}),
            ("api_error", {"code": -2015}),
        ):
            with self.subTest(kind=kind):
                client, transport = scripted_client((kind, payload))
                with mock.patch.object(wallet.time, "sleep") as sleep:
                    result = client.read(wallet.SPOT_BASE, "/read", tries=3)
                self.assertEqual(result, (kind, payload))
                self.assertEqual(transport.call_count, 1)
                sleep.assert_not_called()

    def test_submit_transfer_uses_one_signed_post(self):
        client, transport = scripted_client(
            ("unclear", {"error": "TimeoutError"})
        )
        result = client.submit_transfer("MAIN_UMFUTURE", "USDT", "2")
        self.assertEqual(result[0], "unclear")
        transport.assert_called_once_with(
            "POST",
            wallet.SPOT_BASE,
            "/sapi/v1/asset/transfer",
            {"type": "MAIN_UMFUTURE", "asset": "USDT", "amount": "2"},
            signed=True,
        )

    def test_execute_transfer_persists_attempt_before_post_and_tran_id(self):
        with tempfile.TemporaryDirectory() as root:
            store = RecordingStore(root)
            client = wallet.BinanceClient("offline-key", "offline-secret")
            client.offsets.update(
                {wallet.SPOT_BASE: 0, wallet.FUTURES_BASE: 0}
            )
            calls, post_receipt = [], {}
            history_reads = 0

            def transport(method, base, path, params=None, signed=False):
                nonlocal history_reads
                calls.append((method, base, path, params, signed))
                if method == "POST":
                    receipt_path = next(Path(root).glob("transfer-*.json"))
                    post_receipt.update(json.loads(receipt_path.read_text()))
                    return "ok", {"tranId": 321}
                if path == "/api/v3/account":
                    return "ok", SPOT_BALANCES
                if path == "/fapi/v3/balance":
                    return "ok", FUTURES_BALANCES
                if path == "/sapi/v1/asset/transfer":
                    history_reads += 1
                    if history_reads == 1:
                        return "ok", {"total": 0}
                    return "ok", {"total": 1, "rows": [history_row("321")]}
                raise AssertionError("unexpected offline transport call")

            client._once = transport
            path, receipt = wallet.execute_transfer(
                store, client, "spot-to-usdm", "USDT", "2", "abcdef1",
                query_attempts=1,
            )
            saved = json.loads(Path(path).read_text())

        self.assertEqual(store.created["state"], "PREPARED")
        self.assertEqual(store.created["mutation_attempts"], 0)
        self.assertEqual(post_receipt["state"], "PREPARED")
        self.assertEqual(post_receipt["mutation_attempts"], 1)
        self.assertEqual(sum(call[0] == "POST" for call in calls), 1)
        self.assertEqual(receipt["tran_id"], "321")
        self.assertEqual(saved["tran_id"], "321")

    def test_query_receipt_classifies_match_ambiguity_error_and_exhaustion(self):
        one = {"total": 1, "rows": [history_row("201")]}
        many = {
            "total": 2,
            "rows": [history_row("201"), history_row("202")],
        }
        empty = ("ok", {"total": 0})
        cases = (
            ("one", [("ok", one)], 1, "found", 1, []),
            ("many", [("ok", many)], 1, "ambiguous", 1, []),
            ("api", [("api_error", {"code": -2015})], 3,
             "api_error", 1, []),
            ("empty", [empty, empty], 2, "not_found", 2, [mock.call(2.0)]),
        )
        for label, responses, attempts, expected, count, waits in cases:
            with self.subTest(case=label):
                client, transport = scripted_client(*responses)
                with mock.patch.object(wallet.time, "sleep") as sleep:
                    state, payload = wallet.query_receipt(
                        client, transfer_receipt("l3w" + "b" * 20), attempts
                    )
                self.assertEqual(state, expected)
                self.assertEqual(transport.call_count, count)
                self.assertEqual(sleep.call_args_list, waits)
                if state == "found":
                    self.assertEqual(payload["tranId"], "201")
                if state == "ambiguous":
                    self.assertEqual(payload, {"matches": 2})

    def test_reconcile_terminal_rows_release_lock(self):
        for marker, status in (("c", "CONFIRMED"), ("d", "FAILED")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                store, path, receipt = locked_receipt(root, marker)
                client, _ = scripted_client(
                    ("ok", {"total": 1,
                            "rows": [history_row("301", status)]}),
                    ("ok", SPOT_BALANCES),
                    ("ok", FUTURES_BALANCES),
                )
                _, result = wallet.reconcile_transfer(
                    store, path, receipt, client, attempts=1
                )
                self.assertEqual(result["state"], status)
                self.assertEqual(result["exchange"]["status"], status)
                self.assertFalse(Path(store.lock_path).exists())

    def test_reconcile_rejected_post_without_row_releases_lock(self):
        with tempfile.TemporaryDirectory() as root:
            store, path, receipt = locked_receipt(root, "e", "api_error")
            client, _ = scripted_client(("ok", {"total": 0}))
            _, result = wallet.reconcile_transfer(
                store, path, receipt, client, attempts=1
            )
            self.assertEqual(result["state"], "REJECTED")
            self.assertFalse(Path(store.lock_path).exists())

    def test_reconcile_unresolved_history_keeps_lock(self):
        responses = (
            ("ambiguous", ("ok", {
                "total": 2,
                "rows": [history_row("401"), history_row("402")],
            })),
            ("unreadable", ("ok", [])),
        )
        for index, (label, response) in enumerate(responses):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as root:
                store, path, receipt = locked_receipt(root, "f" if not index else "a")
                client, _ = scripted_client(response)
                _, result = wallet.reconcile_transfer(
                    store, path, receipt, client, attempts=1
                )
                self.assertEqual(result["state"], "UNKNOWN")
                self.assertTrue(Path(store.lock_path).exists())

    def test_validated_transfer_amount_refuses_out_of_policy_values(self):
        cases = (
            ("BTC", "1"),
            ("USDT", "0"),
            ("USDT", "-1"),
            ("USDT", wallet.TRANSFER_HARD_CEILING + Decimal("0.01")),
        )
        for asset, amount in cases:
            with self.subTest(asset=asset, amount=str(amount)):
                with self.assertRaises(ValueError):
                    wallet.validated_transfer_amount(asset, amount)

    def test_balance_for_asset_parses_spot_and_futures(self):
        client, _ = scripted_client(("ok", SPOT_BALANCES))
        self.assertEqual(
            wallet.balance_for_asset(client, "spot", "USDT"),
            {"status": "OK", "account": "spot", "asset": "USDT",
             "free": "8.2500"},
        )
        client, _ = scripted_client(("ok", FUTURES_BALANCES))
        self.assertEqual(
            wallet.balance_for_asset(client, "usd_m", "USDT"),
            {"status": "OK", "account": "usd_m", "asset": "USDT",
             "free": "7.500"},
        )

    def test_balance_for_asset_maps_blocked_and_unknown(self):
        client, _ = scripted_client(("api_error", {"code": -2015}))
        blocked = wallet.balance_for_asset(client, "spot", "USDT")
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["code"], -2015)

        client, _ = scripted_client(("ok", {"balances": "unreadable"}))
        unknown = wallet.balance_for_asset(client, "spot", "USDT")
        self.assertEqual(unknown["status"], "UNKNOWN")
        self.assertEqual(unknown["kind"], "schema_error")


if __name__ == "__main__":
    unittest.main()
