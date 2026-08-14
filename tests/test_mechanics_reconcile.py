"""Offline deterministic tests for the mechanics reconciliation reader."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import mechanics_reconcile  # noqa: E402


SYMBOL = "TSTUSDT"


def futures_receipt(open_fee="0.020", close_fee="0.019", asset="USDT"):
    return {
        "hand": "long",
        "run_id": "abc123",
        "symbol": SYMBOL,
        "started_utc": "2026-08-13T23:21:04Z",
        "open": {
            "params": {"side": "BUY"},
            "status": "FILLED",
            "order_id": "101",
            "executed_qty": "2",
            "cum_quote": "20",
            "fee_by_asset": {asset: open_fee},
        },
        "close": {
            "params": {"side": "SELL"},
            "status": "FILLED",
            "order_id": "102",
            "executed_qty": "2",
            "cum_quote": "19",
            "fee_by_asset": {asset: close_fee},
        },
        "verdict": "CLOSED",
        "finished_utc": "2026-08-13T23:21:07Z",
    }


def futures_trades(open_fee="0.020", close_fee="0.019", asset="USDT"):
    return [
        {
            "symbol": SYMBOL,
            "id": 501,
            "orderId": 101,
            "side": "BUY",
            "qty": "2",
            "price": "10",
            "quoteQty": "20",
            "commission": open_fee,
            "commissionAsset": asset,
            "realizedPnl": "0",
        },
        {
            "symbol": SYMBOL,
            "id": 502,
            "orderId": 102,
            "side": "SELL",
            "qty": "2",
            "price": "9.5",
            "quoteQty": "19",
            "commission": close_fee,
            "commissionAsset": asset,
            "realizedPnl": "-1",
        },
    ]


def income_rows(open_fee="0.020", close_fee="0.019", asset="USDT"):
    return [
        {
            "symbol": SYMBOL,
            "incomeType": "COMMISSION",
            "income": "-" + open_fee,
            "asset": asset,
            "tradeId": 501,
        },
        {
            "symbol": SYMBOL,
            "incomeType": "COMMISSION",
            "income": "-" + close_fee,
            "asset": asset,
            "tradeId": 502,
        },
        {
            "symbol": SYMBOL,
            "incomeType": "REALIZED_PNL",
            "income": "-1",
            "asset": "USDT",
            "tradeId": 502,
        },
    ]


class ScriptedTransport:
    def __init__(self, trades, income, spot_trades=None):
        self.trades = trades
        self.income = income
        self.spot_trades = spot_trades
        self.calls = []

    def __call__(self, method, base_url, path, params=None, signed=False):
        self.calls.append((method, base_url, path, dict(params or {}), signed))
        if path == mechanics_reconcile.SPOT_TRADES_PATH:
            if self.spot_trades is None:
                raise AssertionError("unexpected Spot endpoint")
            return "ok", self.spot_trades
        if path == mechanics_reconcile.USDM_TRADES_PATH:
            return "ok", self.trades
        if path == mechanics_reconcile.USDM_INCOME_PATH:
            return "ok", self.income
        raise AssertionError("unexpected endpoint " + path)


class MechanicsReconcileTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.receipt_dir = holder.name
        self.path = os.path.join(
            self.receipt_dir, "futures-hand-abc123.json"
        )

    def write_receipt(self, payload):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def reconcile(self, receipt, trades, income):
        self.write_receipt(receipt)
        transport = ScriptedTransport(trades, income)
        result = mechanics_reconcile.reconcile_directory(
            self.receipt_dir, signed=True, request=transport
        )[0]
        return result, transport, "\n".join(result.rows)

    def test_happy_full_match_has_exchange_cost(self):
        result, transport, output = self.reconcile(
            futures_receipt(), futures_trades(), income_rows()
        )
        self.assertEqual(result.verdict, "MATCHED")
        self.assertEqual(str(result.total_cost_usdt), "1.039")
        self.assertIn("measured_round_trip_cost_usdt=1.039", output)
        self.assertTrue(all(call[0] == "GET" for call in transport.calls))
        self.assertTrue(all(call[4] is True for call in transport.calls))
        self.assertEqual(
            [call[2] for call in transport.calls],
            [mechanics_reconcile.USDM_TRADES_PATH,
             mechanics_reconcile.USDM_INCOME_PATH],
        )

    def test_sanitized_last_four_order_ids_match_one_order_each(self):
        receipt = futures_receipt()
        receipt["open"].pop("order_id")
        receipt["open"]["order_id_last4"] = "0101"
        receipt["close"].pop("order_id")
        receipt["close"]["order_id_last4"] = "0102"
        trades = futures_trades()
        trades[0]["orderId"] = 990101
        trades[1]["orderId"] = 990102
        result, _transport, _output = self.reconcile(
            receipt, trades, income_rows()
        )
        self.assertEqual(result.verdict, "MATCHED")

    def test_basis_receipt_reads_both_venues_and_income(self):
        basis_path = os.path.join(
            self.receipt_dir, "basis-executor-basis123.json"
        )
        receipt = {
            "run_id": "basis123",
            "symbol": SYMBOL,
            "started_utc": "2026-08-13T22:20:04Z",
            "legs": [
                {"stage": "ENTER_SPOT", "market": "spot", "side": "BUY",
                 "order_id": "201", "executed_qty": "2", "quote": "20",
                 "fee_by_asset": {"USDT": "0.020"}},
                {"stage": "ENTER_SHORT", "market": "usdm", "side": "SELL",
                 "order_id": "301", "executed_qty": "2", "quote": "20",
                 "fee_by_asset": {"USDT": "0.008"}},
                {"stage": "EXIT_SHORT", "market": "usdm", "side": "BUY",
                 "order_id": "302", "executed_qty": "2", "quote": "21",
                 "fee_by_asset": {"USDT": "0.0084"}},
                {"stage": "EXIT_SPOT", "market": "spot", "side": "SELL",
                 "order_id": "202", "executed_qty": "2", "quote": "19",
                 "fee_by_asset": {"USDT": "0.019"}},
            ],
            "verdict": "CLOSED",
            "finished_utc": "2026-08-13T22:20:09Z",
        }
        with open(basis_path, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle)
        spot = [
            {"symbol": SYMBOL, "id": 601, "orderId": 201,
             "isBuyer": True, "qty": "2", "price": "10",
             "quoteQty": "20", "commission": "0.020",
             "commissionAsset": "USDT"},
            {"symbol": SYMBOL, "id": 602, "orderId": 202,
             "isBuyer": False, "qty": "2", "price": "9.5",
             "quoteQty": "19", "commission": "0.019",
             "commissionAsset": "USDT"},
        ]
        futures = [
            {"symbol": SYMBOL, "id": 701, "orderId": 301, "side": "SELL",
             "qty": "2", "price": "10", "quoteQty": "20",
             "commission": "0.008", "commissionAsset": "USDT",
             "realizedPnl": "0"},
            {"symbol": SYMBOL, "id": 702, "orderId": 302, "side": "BUY",
             "qty": "2", "price": "10.5", "quoteQty": "21",
             "commission": "0.0084", "commissionAsset": "USDT",
             "realizedPnl": "-1"},
        ]
        income = [
            {"symbol": SYMBOL, "incomeType": "COMMISSION",
             "income": "-0.008", "asset": "USDT", "tradeId": 701},
            {"symbol": SYMBOL, "incomeType": "COMMISSION",
             "income": "-0.0084", "asset": "USDT", "tradeId": 702},
            {"symbol": SYMBOL, "incomeType": "REALIZED_PNL",
             "income": "-1", "asset": "USDT", "tradeId": 702},
        ]
        transport = ScriptedTransport(futures, income, spot)
        result = mechanics_reconcile.reconcile_directory(
            self.receipt_dir, signed=True, request=transport
        )[0]
        self.assertEqual(result.verdict, "MATCHED")
        self.assertEqual(str(result.total_cost_usdt), "2.0554")
        self.assertEqual(
            [call[2] for call in transport.calls],
            [mechanics_reconcile.SPOT_TRADES_PATH,
             mechanics_reconcile.USDM_TRADES_PATH,
             mechanics_reconcile.USDM_INCOME_PATH],
        )

    def test_commission_mismatch_reports_exact_delta(self):
        result, _transport, output = self.reconcile(
            futures_receipt(open_fee="0.020", close_fee="0.020"),
            futures_trades(open_fee="0.020", close_fee="0.021"),
            income_rows(open_fee="0.020", close_fee="0.021"),
        )
        self.assertEqual(result.verdict, "MISMATCH")
        self.assertIn("delta=0.001", output)
        self.assertIn("verdict=MISMATCH", output)

    def test_missing_exchange_rows_are_unknown(self):
        result, _transport, output = self.reconcile(
            futures_receipt(), [], []
        )
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIsNone(result.total_cost_usdt)
        self.assertIn("quantity_exchange=UNKNOWN", output)
        self.assertIn("measured_round_trip_cost_usdt=UNKNOWN", output)

    def test_missing_income_ledger_is_unknown_not_zero_or_mismatch(self):
        result, _transport, output = self.reconcile(
            futures_receipt(), futures_trades(), []
        )
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIsNone(result.total_cost_usdt)
        self.assertIn("income=UNKNOWN", output)
        self.assertNotIn("reconciliation=MISMATCH", output)

    def test_non_usdt_commission_is_unknown_not_zero(self):
        result, _transport, output = self.reconcile(
            futures_receipt(open_fee="0.001", close_fee="0.001", asset="BNB"),
            futures_trades(open_fee="0.001", close_fee="0.001", asset="BNB"),
            income_rows(open_fee="0.001", close_fee="0.001", asset="BNB"),
        )
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIsNone(result.total_cost_usdt)
        self.assertIn("asset=BNB", output)
        self.assertIn("measured_round_trip_cost_usdt=UNKNOWN", output)
        self.assertNotIn("measured_round_trip_cost_usdt=0 ", output)

    def test_get_only_wrapper_refuses_post_before_delegate(self):
        calls = []

        def delegate(*args):
            calls.append(args)
            return "ok", {}

        wrapped = mechanics_reconcile.get_only_transport(delegate)
        with self.assertRaises(AssertionError):
            wrapped("POST", mechanics_reconcile.USDM_BASE_URL,
                    "/fapi/v1/order", {}, True)
        self.assertEqual(calls, [])

    def test_default_mode_is_local_only_and_prints_claims(self):
        self.write_receipt(futures_receipt())

        def forbidden_transport(*_args):
            raise AssertionError("offline mode attempted transport")

        output = io.StringIO()
        with redirect_stdout(output):
            code = mechanics_reconcile.run(
                self.receipt_dir, signed=False, request=forbidden_transport
            )
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("CLAIMED | FILL", text)
        self.assertIn("CLAIMED | COMMISSION", text)
        self.assertIn("reconciliation=UNKNOWN", text)


if __name__ == "__main__":
    unittest.main()
