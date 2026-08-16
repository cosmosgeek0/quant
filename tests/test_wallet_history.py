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
from execution import binance_http, binance_transfer  # noqa: E402


def history_read(pages):
    feed = list(pages)

    def transport(*_args):
        return binance_http.HttpResult("ok", feed.pop(0), None, None)

    return binance_transfer.read_transfer_history(
        transport, "https://spot.invalid", "MAIN_UMFUTURE", 0, 1
    )


def complete_row():
    return {"tranId": 1, "asset": "USDT", "amount": "2",
            "status": "CONFIRMED", "timestamp": 1700000000000,
            "type": "MAIN_UMFUTURE"}


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
        result = history_read([{"total": 0}])
        self.assertEqual((result.kind, result.value), ("ok", ()))

    def test_rows_present_still_parse(self):
        result = history_read([{"total": 1, "rows": [complete_row()]}])
        self.assertEqual(result.kind, "ok")
        self.assertEqual(result.value, (binance_transfer.TransferRow(
            "MAIN_UMFUTURE", "USDT", Decimal("2"), 1, "CONFIRMED",
            1700000000000),))

    def test_null_rows_treated_as_empty(self):
        result = history_read([{"total": 0, "rows": None}])
        self.assertEqual((result.kind, result.value), ("ok", ()))

    def test_non_list_rows_still_a_schema_error(self):
        result = history_read([{"total": 1, "rows": "invalid"}])
        self.assertEqual((result.kind, result.value), ("unclear", None))
        self.assertEqual(result.error.msg, "transfer_history_schema")

    def test_non_dict_payload_still_a_schema_error(self):
        result = history_read([["invalid"]])
        self.assertEqual((result.kind, result.value), ("unclear", None))
        self.assertEqual(result.error.msg, "transfer_history_schema")


if __name__ == "__main__":
    unittest.main()
