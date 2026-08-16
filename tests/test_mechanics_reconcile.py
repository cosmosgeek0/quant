"""Offline deterministic tests for the mechanics reconciliation reader."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import mechanics_reconcile  # noqa: E402
from execution import binance_futures, binance_http, binance_spot  # noqa: E402


SYMBOL = "TSTUSDT"
SHA = "a" * 40


def futures_receipt(open_fee="0.020", close_fee="0.019", asset="USDT"):
    return {
        "hand": "long",
        "run_id": "abc123",
        "sha": SHA,
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


def spot_roundtrip_receipt():
    receipt = futures_receipt()
    receipt.pop("hand")
    receipt["max_quote"] = "20"
    receipt["buy"] = receipt.pop("open")
    receipt["buy"]["quote_spent"] = receipt["buy"].pop("cum_quote")
    receipt["sell"] = receipt.pop("close")
    receipt["sell"]["quote_received"] = receipt["sell"].pop("cum_quote")
    receipt["sell"]["quantity"] = "2"
    client_ids = {"buy": "fmlabc123b", "sell": "fmlabc123s"}
    for side, client_id in client_ids.items():
        receipt[side + "_client_id"] = client_id
        bound = ["quoteOrderQty", "20"] \
            if side == "buy" else ["quantity", "2"]
        receipt[side]["intent"] = {
            "venue": "BINANCE_SPOT",
            "operation": "ORDER",
            "identity": client_id,
            "git_sha": SHA,
            "params": [
                ["symbol", SYMBOL],
                ["side", side.upper()],
                ["type", "MARKET"],
                bound,
                ["newClientOrderId", client_id],
                ["newOrderRespType", "FULL"],
            ],
        }
    return receipt


def futures_trades(open_fee="0.020", close_fee="0.019", asset="USDT"):
    return [
        {
            "symbol": SYMBOL,
            "id": 501,
            "orderId": 101,
            "side": "BUY",
            "buyer": True,
            "maker": False,
            "time": 1786663265000,
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
            "buyer": False,
            "maker": False,
            "time": 1786663266000,
            "qty": "2",
            "price": "9.5",
            "quoteQty": "19",
            "commission": close_fee,
            "commissionAsset": asset,
            "realizedPnl": "-1",
        },
    ]


def spot_trades():
    return [
        {
            "symbol": row["symbol"],
            "id": row["id"],
            "orderId": row["orderId"],
            "isBuyer": row["side"] == "BUY",
            "isMaker": row["maker"],
            "time": row["time"],
            "qty": row["qty"],
            "price": row["price"],
            "quoteQty": row["quoteQty"],
            "commission": row["commission"],
            "commissionAsset": row["commissionAsset"],
        }
        for row in futures_trades()
    ]


def spot_orders():
    client_ids = ("fmlabc123b", "fmlabc123s")
    orders = {}
    for client_id, fill in zip(client_ids, futures_trades()):
        orders[client_id] = {
            "symbol": SYMBOL,
            "clientOrderId": client_id,
            "orderId": fill["orderId"],
            "status": "FILLED",
            "side": fill["side"],
            "executedQty": fill["qty"],
            "cummulativeQuoteQty": fill["quoteQty"],
        }
    return orders


def income_rows(open_fee="0.020", close_fee="0.019", asset="USDT"):
    return [
        {
            "symbol": SYMBOL,
            "incomeType": "COMMISSION",
            "income": "-" + open_fee,
            "asset": asset,
            "tradeId": 501,
            "tranId": 801,
            "time": 1786663265000,
        },
        {
            "symbol": SYMBOL,
            "incomeType": "COMMISSION",
            "income": "-" + close_fee,
            "asset": asset,
            "tradeId": 502,
            "tranId": 802,
            "time": 1786663266000,
        },
        {
            "symbol": SYMBOL,
            "incomeType": "REALIZED_PNL",
            "income": "-1",
            "asset": "USDT",
            "tradeId": 502,
            "tranId": 803,
            "time": 1786663266000,
        },
    ]


class ScriptedTransport:
    def __init__(self, trades, income, spot_trades=None, spot_orders=None):
        self.trades = trades
        self.income = income
        self.spot_trades = spot_trades
        self.spot_orders = spot_orders
        self.calls = []

    def __call__(self, method, base_url, path, params=None, signed=False):
        parameters = dict(params or {})
        self.calls.append((method, base_url, path, parameters, signed))
        if path == binance_spot.SPOT_ORDER_PATH:
            client_id = parameters.get("origClientOrderId")
            if self.spot_orders is None or client_id not in self.spot_orders:
                raise AssertionError("unexpected Spot order query")
            return binance_http.HttpResult(
                "ok", dict(self.spot_orders[client_id]), None, None
            )
        if path == binance_spot.SPOT_MY_TRADES_PATH:
            if self.spot_trades is None:
                raise AssertionError("unexpected Spot endpoint")
            order_id = parameters.get("orderId")
            rows = self.spot_trades
            if order_id is not None:
                rows = [
                    row for row in rows if str(row["orderId"]) == order_id
                ]
            return binance_http.HttpResult(
                "ok", rows, None, None
            )
        if path == binance_futures.FUTURES_USER_TRADES_PATH:
            return binance_http.HttpResult("ok", self.trades, None, None)
        if path == binance_futures.FUTURES_INCOME_PATH:
            return binance_http.HttpResult("ok", self.income, None, None)
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
        self.assertIn("ledger=usdm_trades kind=ok", output)
        self.assertIn("ledger=income kind=ok", output)
        self.assertTrue(all(call[0] == "GET" for call in transport.calls))
        self.assertTrue(all(call[4] is True for call in transport.calls))
        self.assertEqual(
            [call[2] for call in transport.calls],
            [binance_futures.FUTURES_USER_TRADES_PATH,
             binance_futures.FUTURES_INCOME_PATH],
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
            "sha": SHA,
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
             "isBuyer": True, "isMaker": False,
             "time": 1786659605000, "qty": "2", "price": "10",
             "quoteQty": "20", "commission": "0.020",
             "commissionAsset": "USDT"},
            {"symbol": SYMBOL, "id": 602, "orderId": 202,
             "isBuyer": False, "isMaker": False,
             "time": 1786659608000, "qty": "2", "price": "9.5",
             "quoteQty": "19", "commission": "0.019",
             "commissionAsset": "USDT"},
        ]
        futures = [
            {"symbol": SYMBOL, "id": 701, "orderId": 301, "side": "SELL",
             "buyer": False, "maker": False, "time": 1786659606000,
             "qty": "2", "price": "10", "quoteQty": "20",
             "commission": "0.008", "commissionAsset": "USDT",
             "realizedPnl": "0"},
            {"symbol": SYMBOL, "id": 702, "orderId": 302, "side": "BUY",
             "buyer": True, "maker": False, "time": 1786659607000,
             "qty": "2", "price": "10.5", "quoteQty": "21",
             "commission": "0.0084", "commissionAsset": "USDT",
             "realizedPnl": "-1"},
        ]
        income = [
            {"symbol": SYMBOL, "incomeType": "COMMISSION",
             "income": "-0.008", "asset": "USDT", "tradeId": 701,
             "tranId": 901, "time": 1786659606000},
            {"symbol": SYMBOL, "incomeType": "COMMISSION",
             "income": "-0.0084", "asset": "USDT", "tradeId": 702,
             "tranId": 902, "time": 1786659607000},
            {"symbol": SYMBOL, "incomeType": "REALIZED_PNL",
             "income": "-1", "asset": "USDT", "tradeId": 702,
             "tranId": 903, "time": 1786659607000},
        ]
        transport = ScriptedTransport(futures, income, spot)
        result = mechanics_reconcile.reconcile_directory(
            self.receipt_dir, signed=True, request=transport
        )[0]
        self.assertEqual(result.verdict, "MATCHED")
        self.assertEqual(str(result.total_cost_usdt), "2.0554")
        self.assertEqual(
            [call[2] for call in transport.calls],
            [binance_spot.SPOT_MY_TRADES_PATH,
             binance_futures.FUTURES_USER_TRADES_PATH,
             binance_futures.FUTURES_INCOME_PATH],
        )

    def test_spot_roundtrip_receipt_is_discovered_and_measured(self):
        self.path = os.path.join(
            self.receipt_dir, "roundtrip-abc123.json"
        )
        self.write_receipt(spot_roundtrip_receipt())
        claim = mechanics_reconcile.read_receipt(self.path)
        self.assertEqual(claim.issues, ())
        self.assertEqual(
            [leg.client_id for leg in claim.legs],
            ["fmlabc123b", "fmlabc123s"],
        )
        transport = ScriptedTransport([], [], spot_trades(), spot_orders())
        results = mechanics_reconcile.reconcile_directory(
            self.receipt_dir, signed=True, request=transport
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].verdict, "MATCHED")
        self.assertEqual(str(results[0].total_cost_usdt), "1.039")
        self.assertEqual(
            [call[2] for call in transport.calls],
            [binance_spot.SPOT_ORDER_PATH,
             binance_spot.SPOT_ORDER_PATH,
             binance_spot.SPOT_MY_TRADES_PATH,
             binance_spot.SPOT_MY_TRADES_PATH],
        )
        self.assertEqual(
            [call[3].get("origClientOrderId") for call in transport.calls[:2]],
            ["fmlabc123b", "fmlabc123s"],
        )
        self.assertEqual(
            [call[3].get("orderId") for call in transport.calls[2:]],
            ["101", "102"],
        )
        output = "\n".join(results[0].rows)
        self.assertIn("ledger=spot_order_buy kind=ok", output)
        self.assertIn("ledger=spot_trades_sell kind=ok", output)

    def test_spot_roundtrip_accepts_canonical_quote_bound(self):
        self.path = os.path.join(
            self.receipt_dir, "roundtrip-abc123.json"
        )
        receipt = spot_roundtrip_receipt()
        receipt["max_quote"] = "20.00"
        self.write_receipt(receipt)
        claim = mechanics_reconcile.read_receipt(self.path)
        self.assertEqual(claim.issues, ())
        transport = ScriptedTransport([], [], spot_trades(), spot_orders())
        result = mechanics_reconcile.signed_reconciliation(claim, transport)
        self.assertEqual(result.verdict, "MATCHED")
        self.assertEqual(str(result.total_cost_usdt), "1.039")

    def test_spot_roundtrip_bounds_require_positive_wire_text(self):
        self.path = os.path.join(
            self.receipt_dir, "roundtrip-abc123.json"
        )
        cases = (
            ("buy_zero", "buy", "0", "0"),
            ("buy_negative", "buy", "-20", "-20"),
            ("buy_intent_integer", "buy", 20, "20"),
            ("buy_receipt_integer", "buy", "20", 20),
            ("sell_zero", "sell", "0", "0"),
            ("sell_negative", "sell", "-2", "-2"),
            ("sell_intent_integer", "sell", 2, "2"),
            ("sell_receipt_integer", "sell", "2", 2),
        )
        for name, side, intent_bound, receipt_bound in cases:
            with self.subTest(case=name):
                receipt = spot_roundtrip_receipt()
                if side == "buy":
                    receipt["max_quote"] = receipt_bound
                else:
                    receipt["sell"]["quantity"] = receipt_bound
                receipt[side]["intent"]["params"][3][1] = intent_bound
                self.write_receipt(receipt)
                claim = mechanics_reconcile.read_receipt(self.path)
                self.assertIn(
                    mechanics_reconcile.ROUNDTRIP_IDENTITY_ISSUE,
                    claim.issues,
                )

    def test_spot_roundtrip_incomplete_fill_coverage_is_unknown(self):
        self.path = os.path.join(
            self.receipt_dir, "roundtrip-abc123.json"
        )
        orders = spot_orders()
        orders["fmlabc123b"]["executedQty"] = "3"
        orders["fmlabc123b"]["cummulativeQuoteQty"] = "30"
        self.write_receipt(spot_roundtrip_receipt())
        transport = ScriptedTransport([], [], spot_trades(), orders)
        result = mechanics_reconcile.signed_reconciliation(
            mechanics_reconcile.read_receipt(self.path), transport
        )
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIsNone(result.total_cost_usdt)
        self.assertEqual(
            [call[2] for call in transport.calls],
            [binance_spot.SPOT_ORDER_PATH,
             binance_spot.SPOT_ORDER_PATH,
             binance_spot.SPOT_MY_TRADES_PATH],
        )

    def test_spot_roundtrip_fill_scope_or_reused_id_stays_unknown(self):
        self.path = os.path.join(
            self.receipt_dir, "roundtrip-abc123.json"
        )
        for case in ("opposite_side_fill", "reused_trade_id"):
            with self.subTest(case=case):
                trades = spot_trades()
                if case == "opposite_side_fill":
                    extra = dict(trades[0])
                    extra["id"] = 503
                    extra["isBuyer"] = False
                    trades.append(extra)
                else:
                    trades[1]["id"] = trades[0]["id"]
                self.write_receipt(spot_roundtrip_receipt())
                transport = ScriptedTransport(
                    [], [], trades, spot_orders()
                )
                result = mechanics_reconcile.signed_reconciliation(
                    mechanics_reconcile.read_receipt(self.path), transport
                )
                self.assertEqual(result.verdict, "UNKNOWN")
                self.assertIsNone(result.total_cost_usdt)

    def test_spot_roundtrip_requires_two_sha_bound_order_intents(self):
        self.path = os.path.join(
            self.receipt_dir, "roundtrip-abc123.json"
        )
        cases = (
            ("missing_client_id", "root", "buy_client_id", None),
            ("shared_client_id", "root", "sell_client_id", "fmlabc123b"),
            ("other_venue", "buy", "venue", "BINANCE_USDM"),
            ("other_operation", "sell", "operation", "TRANSFER"),
            ("other_identity", "buy", "identity", "fmlotherb"),
            ("other_sha", "sell", "git_sha", "b" * 40),
            ("missing_params", "buy", "params", None),
            ("conflicting_params", "sell", "params", [["side", "BUY"]]),
        )
        for name, owner, field, value in cases:
            with self.subTest(case=name):
                receipt = spot_roundtrip_receipt()
                target = receipt if owner == "root" \
                    else receipt[owner]["intent"]
                if value is None:
                    target.pop(field)
                else:
                    target[field] = value
                self.write_receipt(receipt)
                claim = mechanics_reconcile.read_receipt(self.path)
                self.assertIn(
                    mechanics_reconcile.ROUNDTRIP_IDENTITY_ISSUE,
                    claim.issues,
                )
                transport = ScriptedTransport([], [], spot_trades())
                result = mechanics_reconcile.signed_reconciliation(
                    claim, transport
                )
                self.assertEqual(result.verdict, "UNKNOWN")
                self.assertIsNone(result.total_cost_usdt)
                self.assertEqual(transport.calls, [])

    def test_spot_roundtrip_queries_ids_before_trade_ledgers(self):
        self.path = os.path.join(
            self.receipt_dir, "roundtrip-abc123.json"
        )
        for case in ("spliced_order_ids", "other_side"):
            with self.subTest(case=case):
                receipt = spot_roundtrip_receipt()
                orders = spot_orders()
                trades = spot_trades()
                if case == "spliced_order_ids":
                    receipt["buy"]["order_id"] = "901"
                    receipt["sell"]["order_id"] = "902"
                    trades[0]["orderId"] = 901
                    trades[1]["orderId"] = 902
                else:
                    orders["fmlabc123b"]["side"] = "SELL"
                self.write_receipt(receipt)
                transport = ScriptedTransport([], [], trades, orders)
                result = mechanics_reconcile.signed_reconciliation(
                    mechanics_reconcile.read_receipt(self.path), transport
                )
                self.assertEqual(result.verdict, "UNKNOWN")
                self.assertIsNone(result.total_cost_usdt)
                self.assertEqual(
                    [call[2] for call in transport.calls],
                    [binance_spot.SPOT_ORDER_PATH,
                     binance_spot.SPOT_ORDER_PATH],
                )
                output = "\n".join(result.rows)
                self.assertIn("ledger=spot_order_buy kind=unclear", output)

    def test_spot_roundtrip_status_difference_stays_unknown(self):
        self.path = os.path.join(
            self.receipt_dir, "roundtrip-abc123.json"
        )
        cases = (
            ("FILLED", "CANCELED"),
            ("CANCELED", "FILLED"),
        )
        for claimed_status, exchange_status in cases:
            with self.subTest(
                    claimed=claimed_status, exchange=exchange_status):
                receipt = spot_roundtrip_receipt()
                receipt["buy"]["status"] = claimed_status
                orders = spot_orders()
                orders["fmlabc123b"]["status"] = exchange_status
                self.write_receipt(receipt)
                transport = ScriptedTransport([], [], spot_trades(), orders)
                result = mechanics_reconcile.signed_reconciliation(
                    mechanics_reconcile.read_receipt(self.path), transport
                )
                self.assertEqual(result.verdict, "UNKNOWN")
                self.assertIsNone(result.total_cost_usdt)
                output = "\n".join(result.rows)
                self.assertIn("ledger=spot_order_buy kind=unclear", output)

    def test_pending_roundtrip_intents_read_exact_orders_and_fills(self):
        self.path = os.path.join(
            self.receipt_dir, "roundtrip-abc123.json"
        )
        buy_pending = spot_roundtrip_receipt()
        buy_intent = buy_pending["buy"]["intent"]
        buy_pending["buy"] = {
            "submitted_utc": "2026-08-13T23:21:05Z",
            "intent": buy_intent,
        }
        buy_pending.pop("sell")
        buy_pending["verdict"] = "UNKNOWN"
        sell_pending = spot_roundtrip_receipt()
        sell_intent = sell_pending["sell"]["intent"]
        sell_pending["sell"] = {
            "submitted_utc": "2026-08-13T23:21:06Z",
            "quantity": "2",
            "planned_residual": "0",
            "intent": sell_intent,
        }
        sell_pending["verdict"] = "UNKNOWN"
        cases = (
            ("buy", buy_pending, ("fmlabc123b",), ("101",), ("BUY",)),
            ("sell", sell_pending,
             ("fmlabc123b", "fmlabc123s"),
             ("101", "102"), ("BUY", "SELL")),
        )
        for name, receipt, client_ids, order_ids, stages in cases:
            with self.subTest(stage=name):
                self.write_receipt(receipt)
                transport = ScriptedTransport(
                    [], [], spot_trades(), spot_orders()
                )
                result = mechanics_reconcile.signed_reconciliation(
                    mechanics_reconcile.read_receipt(self.path), transport
                )
                self.assertEqual(result.verdict, "UNKNOWN")
                self.assertIsNone(result.total_cost_usdt)
                order_calls = [
                    call for call in transport.calls
                    if call[2] == binance_spot.SPOT_ORDER_PATH
                ]
                fill_calls = [
                    call for call in transport.calls
                    if call[2] == binance_spot.SPOT_MY_TRADES_PATH
                ]
                self.assertEqual(
                    tuple(call[3].get("origClientOrderId")
                          for call in order_calls),
                    client_ids,
                )
                self.assertEqual(
                    tuple(call[3].get("orderId") for call in fill_calls),
                    order_ids,
                )
                output = "\n".join(result.rows)
                for stage in stages:
                    side = stage.lower()
                    self.assertIn(
                        f"ledger=spot_order_{side} kind=ok "
                        "status_exchange=FILLED",
                        output,
                    )
                    fill_row = next(
                        row for row in result.rows
                        if f"| EXCHANGE | FILL | stage={stage} " in row
                    )
                    self.assertIn("quantity_exchange=2", fill_row)

    def test_timezone_less_receipt_time_is_unknown(self):
        receipt = futures_receipt()
        receipt["started_utc"] = "2026-08-13Z"
        self.write_receipt(receipt)
        transport = ScriptedTransport(futures_trades(), income_rows())
        result = mechanics_reconcile.reconcile_directory(
            self.receipt_dir, signed=True, request=transport
        )[0]
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIsNone(result.total_cost_usdt)
        self.assertEqual(transport.calls, [])

    def test_ledger_read_kinds_remain_distinct_and_safe(self):
        self.write_receipt(futures_receipt())
        claim = mechanics_reconcile.read_receipt(self.path)
        for kind in ("api_error", "unclear", "refusal"):
            with self.subTest(kind=kind):
                if kind == "refusal":
                    reader = mock.Mock(side_effect=binance_http.AdapterRefusal(
                        "private-marker"
                    ))
                else:
                    error = binance_http.HttpResult(
                        kind, None, -1000 if kind == "api_error" else None,
                        "private-marker",
                    )
                    reader = mock.Mock(return_value=binance_http.ReadResult(
                        kind, None, error
                    ))
                with mock.patch.object(
                        mechanics_reconcile.binance_futures,
                        "read_user_trades", reader), mock.patch.object(
                        mechanics_reconcile.binance_futures,
                        "read_income", reader):
                    result = mechanics_reconcile.signed_reconciliation(
                        claim, ScriptedTransport([], [])
                    )
                output = "\n".join(result.rows)
                self.assertEqual(result.verdict, "UNKNOWN")
                self.assertIsNone(result.total_cost_usdt)
                self.assertIn(
                    "ledger=usdm_trades kind=" + kind, output
                )
                self.assertIn("ledger=income kind=" + kind, output)
                self.assertNotIn("private-marker", output)

    def test_saturated_ledgers_remain_unknown(self):
        extra_trades = []
        for index in range(998):
            row = dict(futures_trades()[0])
            row.update(id=1000 + index, orderId=1000 + index)
            extra_trades.append(row)
        extra_income = [
            {"symbol": SYMBOL, "incomeType": "TRANSFER", "income": "0",
             "asset": "USDT", "tradeId": "", "tranId": 2000 + index,
             "time": 1786663266000}
            for index in range(997)
        ]
        cases = (
            (futures_trades() + extra_trades, income_rows()),
            (futures_trades(), income_rows() + extra_income),
        )
        for trades, income in cases:
            with self.subTest(rows=(len(trades), len(income))):
                result, _transport, output = self.reconcile(
                    futures_receipt(), trades, income
                )
                self.assertEqual(result.verdict, "UNKNOWN")
                self.assertIsNone(result.total_cost_usdt)
                self.assertIn("measured_round_trip_cost_usdt=UNKNOWN", output)

    def test_receipt_sha_is_lowercase_full_and_retained(self):
        self.write_receipt(futures_receipt())
        claim = mechanics_reconcile.read_receipt(self.path)
        self.assertEqual(claim.git_sha, SHA)
        for invalid in (None, "A" * 40, "short"):
            with self.subTest(sha=invalid):
                receipt = futures_receipt()
                if invalid is None:
                    receipt.pop("sha")
                else:
                    receipt["sha"] = invalid
                result, _transport, _output = self.reconcile(
                    receipt, futures_trades(), income_rows()
                )
                self.assertEqual(result.verdict, "UNKNOWN")
                self.write_receipt(receipt)
                self.assertIsNone(
                    mechanics_reconcile.read_receipt(self.path).git_sha
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
