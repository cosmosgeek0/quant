"""Deterministic offline tests for reverse-basis Margin reconciliation."""

import sys
import unittest
from decimal import Decimal
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import margin_reconcile  # noqa: E402


ASSET = "BTC"
SYMBOL = "BTCUSDT"


def account(borrowed="0", interest="0", free="0", locked="0"):
    net = (Decimal(free) + Decimal(locked)
           - Decimal(borrowed) - Decimal(interest))
    return [{
        "asset": ASSET, "free": free, "locked": locked,
        "borrowed": borrowed, "interest": interest,
        "netAsset": format(net, "f"),
    }]


def ledger(open_principal="2", open_interest="0", repay_principal=None,
           repay_interest="0"):
    rows = [{
        "asset": ASSET, "principal": open_principal,
        "interest": open_interest, "status": "BORROWED",
    }]
    if repay_principal is not None:
        rows.append({
            "asset": ASSET, "principal": repay_principal,
            "interest": repay_interest, "status": "REPAID",
        })
    return rows


def fill(order_id, side, quantity, status="FILLED", fee="0",
         fee_asset="USDT"):
    return {
        "id": order_id, "side": side, "symbol": SYMBOL,
        "executedQty": quantity, "cummulativeQuoteQty": "100",
        "status": status, "commission": fee,
        "commissionAsset": fee_asset,
    }


def verdict(rows, name):
    return next(row for row in rows if row["verdict"] == name)


class MarginReconcileTests(unittest.TestCase):
    def test_reconciled_flat_names_all_fill_evidence(self):
        rows = margin_reconcile.reconcile_margin(
            account(), [fill("sell-1", "SELL", "2"),
                        fill("buy-1", "BUY", "2")],
            ledger(repay_principal="2", repay_interest="0.01"),
        )
        row = verdict(rows, "RECONCILED_FLAT")
        self.assertEqual(row["asset"], ASSET)
        self.assertEqual(row["evidence_order_ids"], ["sell-1", "buy-1"])
        self.assertIn("account_snapshot[0].interest", row["evidence"])
        self.assertIn("order_records[1].executedQty", row["evidence"])

    def test_liability_open_uses_principal_plus_interest_and_dust(self):
        rows = margin_reconcile.reconcile_margin(
            account(borrowed="0.0004", interest="0.0001"), [],
            ledger(open_principal="0.0004", open_interest="0.00005"),
            dust_threshold=Decimal("0.001"),
        )
        row = verdict(rows, "LIABILITY_OPEN")
        self.assertEqual(row["borrowed"], "0.0004")
        self.assertEqual(row["interest"], "0.0001")
        self.assertEqual(row["total_liability"], "0.0005")
        self.assertTrue(row["dust"])

    def test_partial_unwind_uses_only_reported_fill_quantities(self):
        rows = margin_reconcile.reconcile_margin(
            account(borrowed="2"),
            [fill("sell-1", "SELL", "2"),
             fill("buy-1", "BUY", "0.75", status="PARTIALLY_FILLED")],
            ledger(),
        )
        row = verdict(rows, "PARTIAL_UNWIND")
        self.assertEqual(row["remaining_qty"], "1.25")
        self.assertEqual(row["evidence_order_ids"], ["sell-1", "buy-1"])

    def test_unknown_freeze_for_malformed_snapshot(self):
        torn = account(borrowed="2")
        torn[0].pop("interest")
        row = margin_reconcile.reconcile_margin(torn, [], ledger())[0]
        self.assertEqual(row["verdict"], "UNKNOWN_FREEZE")
        self.assertEqual(row["missing_evidence"],
                         ["account_snapshot[0].interest"])

    def test_torn_ledger_row_freezes(self):
        torn = ledger()
        torn[0].pop("principal")
        row = margin_reconcile.reconcile_margin(
            account(borrowed="2"), [], torn)[0]
        self.assertEqual(row["verdict"], "UNKNOWN_FREEZE")
        self.assertIn("ledger_records[0].principal", row["missing_evidence"])

    def test_missing_ledger_row_never_turns_liability_into_zero(self):
        row = margin_reconcile.reconcile_margin(
            account(borrowed="2"), [], [])[0]
        self.assertEqual(row["verdict"], "UNKNOWN_FREEZE")
        self.assertEqual(row["reason"], "account liability has no ledger row")

    def test_partial_fill_then_completing_fill_reconciles_flat(self):
        opening = fill("sell-1", "SELL", "2")
        partial = fill("buy-1", "BUY", "1", status="PARTIALLY_FILLED")
        first = margin_reconcile.reconcile_margin(
            account(borrowed="2"), [opening, partial], ledger())
        self.assertEqual(verdict(first, "PARTIAL_UNWIND")["remaining_qty"], "1")

        complete = fill("buy-1", "BUY", "2")
        second = margin_reconcile.reconcile_margin(
            account(), [opening, complete],
            ledger(repay_principal="2", repay_interest="0.002"),
        )
        self.assertEqual([row["verdict"] for row in second],
                         ["RECONCILED_FLAT"])

    def test_interest_only_residual_is_open_liability(self):
        rows = margin_reconcile.reconcile_margin(
            account(interest="0.003"),
            [fill("sell-1", "SELL", "2"), fill("buy-1", "BUY", "2")],
            ledger(open_interest="0.001", repay_principal="2",
                   repay_interest="0.001"),
        )
        row = verdict(rows, "LIABILITY_OPEN")
        self.assertEqual(row["borrowed"], "0")
        self.assertEqual(row["interest"], "0.003")
        self.assertEqual(row["total_liability"], "0.003")

    def test_fee_in_base_asset_leaves_exact_residual(self):
        rows = margin_reconcile.reconcile_margin(
            account(borrowed="2"),
            [fill("sell-1", "SELL", "2"),
             fill("buy-1", "BUY", "2", fee="0.001", fee_asset=ASSET)],
            ledger(),
        )
        self.assertEqual(verdict(rows, "PARTIAL_UNWIND")["remaining_qty"],
                         "0.001")

    def test_new_intent_never_reduces_filled_exposure(self):
        intent = fill("buy-intent", "BUY", "0", status="NEW")
        rows = margin_reconcile.reconcile_margin(
            account(borrowed="2"),
            [fill("sell-1", "SELL", "2"), intent], ledger())
        row = verdict(rows, "PARTIAL_UNWIND")
        self.assertEqual(row["remaining_qty"], "2")
        self.assertEqual(row["evidence_order_ids"], ["sell-1"])

    def test_interest_reconciliation_uses_a_lower_bound(self):
        rows = margin_reconcile.reconcile_margin(
            account(borrowed="2", interest="0.006"), [],
            ledger(open_interest="0.004"),
        )
        self.assertEqual(verdict(rows, "LIABILITY_OPEN")["interest"], "0.006")


if __name__ == "__main__":
    unittest.main()
