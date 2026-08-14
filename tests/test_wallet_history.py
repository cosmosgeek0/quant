"""Offline tests for wallet.py transfer-history shapes — no network.

Regression for the live 2026-08-13 BLOCK: an empty history window comes back
from the exchange as {"total": 0} with NO rows key, which is a valid empty
baseline, not a schema error.
"""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import limits  # noqa: E402
import wallet  # noqa: E402


def client_with(pages):
    """A BinanceClient whose reads are scripted; time already synced."""
    client = wallet.BinanceClient("k", "s")
    client.offsets[wallet.SPOT_BASE] = 0
    feed = list(pages)
    client.read = lambda *args, **kwargs: feed.pop(0)
    return client


class TransferHistoryShapeTests(unittest.TestCase):
    def test_transfer_ceiling_uses_per_leg_cap(self):
        self.assertIs(wallet.TRANSFER_HARD_CEILING, limits.PER_LEG_CAP_USDT)
        self.assertEqual(wallet.TRANSFER_HARD_CEILING, Decimal("32"))
        self.assertEqual(
            wallet.validated_transfer_amount("USDT", "32"), Decimal("32")
        )
        with self.assertRaisesRegex(ValueError, "32 USDT transfer ceiling"):
            wallet.validated_transfer_amount("USDT", "32.00000001")

    def test_total_only_empty_window_is_a_valid_empty_baseline(self):
        client = client_with([("ok", {"total": 0})])
        kind, rows = client.transfer_history("MAIN_UMFUTURE", 0, 1)
        self.assertEqual(kind, "ok")
        self.assertEqual(rows, [])

    def test_rows_present_still_parse(self):
        page = {"total": 1, "rows": [{"tranId": 1, "asset": "USDT"}]}
        client = client_with([("ok", page)])
        kind, rows = client.transfer_history("MAIN_UMFUTURE", 0, 1)
        self.assertEqual(kind, "ok")
        self.assertEqual(len(rows), 1)

    def test_null_rows_treated_as_empty(self):
        client = client_with([("ok", {"total": 0, "rows": None})])
        kind, rows = client.transfer_history("MAIN_UMFUTURE", 0, 1)
        self.assertEqual(kind, "ok")
        self.assertEqual(rows, [])

    def test_non_list_rows_still_a_schema_error(self):
        client = client_with([("ok", {"total": 1, "rows": "junk"})])
        kind, payload = client.transfer_history("MAIN_UMFUTURE", 0, 1)
        self.assertEqual(kind, "schema_error")
        self.assertEqual(payload["error"], "InvalidTransferHistory")

    def test_non_dict_payload_still_a_schema_error(self):
        client = client_with([("ok", ["junk"])])
        kind, _ = client.transfer_history("MAIN_UMFUTURE", 0, 1)
        self.assertEqual(kind, "schema_error")


if __name__ == "__main__":
    unittest.main()
