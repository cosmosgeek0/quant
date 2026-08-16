#!/usr/bin/env python3
"""Offline regressions for the job-runner Futures adapter migration."""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import job_runner as jr  # noqa: E402


NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
SHA = "7ac26aa771937f802930dac5eb017d6583d968d8"
SYMBOL = "TSTUSDT"
D = Decimal


def ok(payload):
    return jr.binance_http.HttpResult("ok", payload, None, None)


def symbol_row():
    return {
        "symbol": SYMBOL,
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "orderTypes": ["LIMIT", "MARKET"],
        "timeInForce": ["IOC", "GTC"],
        "filters": [
            {
                "filterType": "PRICE_FILTER", "tickSize": "0.01",
                "minPrice": "0.01", "maxPrice": "1000000",
            },
            {
                "filterType": "LOT_SIZE", "stepSize": "0.001",
                "minQty": "0.001", "maxQty": "1000",
            },
            {
                "filterType": "MARKET_LOT_SIZE", "stepSize": "0.001",
                "minQty": "0.001", "maxQty": "500",
            },
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
            {
                "filterType": "PERCENT_PRICE",
                "multiplierUp": "1.05", "multiplierDown": "0.95",
            },
        ],
    }


class FuturesTransport:
    def __init__(self, paths, leverage=1):
        self.paths = paths
        self.leverage = leverage
        self.quantity = D("0")
        self.calls = []
        self.orders = {}
        self.posts = []
        self.durable_before_post = []
        self.pending_before_post = []
        self.income_trade_shift = 0

    def position(self):
        return {
            "symbol": SYMBOL, "positionSide": "BOTH",
            "positionAmt": format(self.quantity, "f"),
            "markPrice": "200",
        }

    def __call__(self, method, _base_url, path, params=None, signed=False):
        wire = dict(params or ())
        self.calls.append((method, path, wire, signed))
        if method == "GET" and path == "/fapi/v1/exchangeInfo":
            return ok({"symbols": [symbol_row()]})
        if method == "GET" and path == "/fapi/v1/ticker/bookTicker":
            return ok({
                "symbol": SYMBOL, "bidPrice": "199.99", "askPrice": "200",
            })
        if method == "GET" and path == "/fapi/v1/premiumIndex":
            return ok({"symbol": SYMBOL, "markPrice": "200"})
        if method == "GET" and path == "/fapi/v1/accountConfig":
            return ok({"canTrade": True})
        if method == "GET" and path == "/fapi/v1/positionSide/dual":
            return ok({"dualSidePosition": False})
        if method == "GET" and path == "/fapi/v3/account":
            return ok({"positions": [self.position()]})
        if method == "GET" and path == "/fapi/v3/positionRisk":
            return ok([self.position()])
        if method == "GET" and path == "/fapi/v1/openOrders":
            return ok([])
        if method == "GET" and path == "/fapi/v3/balance":
            return ok([{"asset": "USDT", "availableBalance": "100"}])
        if method == "GET" and path == "/fapi/v1/symbolConfig":
            return ok([{"symbol": SYMBOL, "leverage": str(self.leverage)}])
        if method == "POST" and path == "/fapi/v1/leverage":
            receipt = Path(
                self.paths["receipts"], "job-leverage-leverage-intent.json"
            )
            self.durable_before_post.append(receipt.is_file())
            self.posts.append((path, wire))
            self.leverage = 1
            return ok({"leverage": 1})
        if method == "POST" and path == "/fapi/v1/order":
            state = json.loads(Path(
                self.paths["momentum_position"]
            ).read_text(encoding="utf-8"))
            pending = state["pending_intent"]
            self.pending_before_post.append(dict(pending))
            self.durable_before_post.append(
                pending["client_id"] == wire["newClientOrderId"]
                and pending["intent"] == wire
                and pending["adapter_intent"]["identity"]
                == wire["newClientOrderId"]
            )
            self.posts.append((path, wire))
            quantity = D(wire["quantity"])
            close = wire["reduceOnly"] == "true"
            price = D("205") if close else D("200")
            order_id = 202 if close else 101
            trade_id = 92 if close else 91
            order = {
                "symbol": SYMBOL,
                "clientOrderId": wire["newClientOrderId"],
                "orderId": order_id,
                "status": "FILLED",
                "side": wire["side"],
                "reduceOnly": close,
                "executedQty": format(quantity, "f"),
                "cumQuote": format(quantity * price, "f"),
                "trade_id": trade_id,
                "price": price,
            }
            self.orders[wire["newClientOrderId"]] = order
            self.quantity = D("0") if close else quantity
            return ok({
                "clientOrderId": wire["newClientOrderId"],
                "reduceOnly": close,
            })
        if method == "GET" and path == "/fapi/v1/order":
            return ok(dict(self.orders[wire["origClientOrderId"]]))
        if method == "GET" and path == "/fapi/v1/userTrades":
            order_id = int(wire["orderId"])
            order = next(
                value for value in self.orders.values()
                if value["orderId"] == order_id
            )
            commission = "0.002" if order["reduceOnly"] else "0.003"
            return ok([{
                "symbol": SYMBOL, "id": order["trade_id"],
                "orderId": order_id, "price": format(order["price"], "f"),
                "qty": order["executedQty"], "quoteQty": order["cumQuote"],
                "commission": commission, "commissionAsset": "USDT",
                "side": order["side"],
                "buyer": order["side"] == "BUY", "maker": False,
                "time": 1,
                "realizedPnl": "0.25" if order["reduceOnly"] else "0",
            }])
        if method == "GET" and path == "/fapi/v1/income":
            close = next(
                value for value in self.orders.values()
                if value["reduceOnly"]
            )
            return ok([{
                "tranId": 501, "symbol": SYMBOL,
                "incomeType": "REALIZED_PNL", "income": "0.25",
                "asset": "USDT", "time": int(wire["startTime"]) + 1,
                "tradeId": close["trade_id"] + self.income_trade_shift,
            }])
        raise AssertionError("unexpected offline call %s %s" % (method, path))


class FuturesMomentumAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.receipt_dir = temporary.name
        self.paths = jr._paths(self.receipt_dir)
        jr._make_dirs(self.paths)
        environment = mock.patch.dict(os.environ, {
            "BINANCE_API_KEY": "offline-key",
            "BINANCE_API_SECRET": "offline-secret",
            "QUANT_SHA": SHA,
            "MAX_SLIPPAGE_BPS": "0",
        }, clear=False)
        environment.start()
        self.addCleanup(environment.stop)

    def services(self, transport):
        return replace(
            jr._build_execution_services(), transport=transport,
            sleep=lambda _delay: None,
        )

    def job(self, job_id, operation):
        return jr.Job(
            job_id=job_id, op=operation, symbol=SYMBOL,
            requested_max_notional=D("6"), effective_notional=D("6"),
            created_utc=NOW - timedelta(minutes=1),
            expires_utc=NOW + timedelta(minutes=10),
            requested_by="lead",
        )

    def test_runner_has_no_futures_proof_module_or_old_wire_helpers(self):
        source = Path(jr.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import futures_hand", source)
        self.assertNotIn("import set_leverage_1x", source)
        for name in (
                "_ensure_leverage_1x", "_configure_futures_hand",
                "_one_way_position", "_position_mark_price",
                "_momentum_submit", "_momentum_query",
                "_normalise_momentum_fills", "_momentum_fills",
                "_momentum_open_plan", "_momentum_measure",
                "_momentum_order_facts", "_run_futures"):
            self.assertNotIn("def " + name + "(", source)

    def test_leverage_intent_is_durable_before_the_only_post(self):
        transport = FuturesTransport(self.paths, leverage=3)
        services = self.services(transport)
        returned = jr._prove_futures_leverage(
            self.paths, "leverage", SYMBOL, SHA, services, NOW
        )
        self.assertIs(returned, services)
        self.assertEqual(transport.durable_before_post, [True])
        self.assertEqual(len(transport.posts), 1)
        receipt = json.loads(Path(
            self.paths["receipts"], "job-leverage-leverage-intent.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(receipt["intent"]["git_sha"], SHA)
        self.assertEqual(receipt["intent"]["params"], [
            ["symbol", SYMBOL], ["leverage", "1"],
        ])

    def test_open_and_exit_use_typed_orders_fills_and_income(self):
        transport = FuturesTransport(self.paths)
        services = self.services(transport)
        opened = jr._run_momentum_open(
            self.job("open", "MOMENTUM_OPEN"), self.receipt_dir,
            self.paths, NOW, execution_services=services,
        )
        self.assertEqual((opened.verdict, opened.exit_code), ("OPEN", 0))
        closed = jr._run_momentum_exit(
            self.job("close", "MOMENTUM_EXIT"), self.receipt_dir,
            self.paths, NOW, governor_token="exit-token",
            execution_services=services,
        )
        self.assertEqual((closed.verdict, closed.exit_code), ("CLOSED", 0))
        self.assertEqual(transport.durable_before_post, [True, True])
        self.assertEqual(len(transport.posts), 2)
        self.assertEqual(
            (transport.pending_before_post[1]["job_op"],
             transport.pending_before_post[1]["governor_token"]),
            ("MOMENTUM_EXIT", "exit-token"),
        )
        self.assertEqual(closed.ledger["realized"], {
            "gross_usdt": "0.25", "net_usdt": "0.245",
            "commission_by_asset": {"USDT": "0.005"},
        })
        state = json.loads(Path(
            self.paths["momentum_position"]
        ).read_text(encoding="utf-8"))
        self.assertEqual((state["state"], state["measured_qty"]),
                         ("CLOSED", "0"))

    def test_recovery_queries_only_and_accepts_old_pending_shape(self):
        position = {
            "state": "OPENING", "venue": "futures", "symbol": SYMBOL,
            "job_id": "recover", "sha": SHA, "effective_notional": "6",
            "pending_intent": {
                "action": "OPEN", "client_id": "l2fm-recover-o",
                "intent": {"symbol": SYMBOL},
                "persisted_utc": NOW.isoformat().replace("+00:00", "Z"),
            },
        }
        jr._save_momentum_position(self.paths, position, NOW)
        Path(self.paths["claims"], "job-recover.claim").touch()
        transport = FuturesTransport(self.paths)
        services = self.services(transport)
        identity = jr.binance_http.OrderIdentity(
            SYMBOL, "l2fm-recover-o", None
        )
        absent = jr.binance_http.OrderResult(
            "not_found", identity, None, None, False, None, None,
            None, None, (), None,
        )
        mutations = []
        def no_mutation(*_args, **_kwargs):
            mutations.append(True)
            raise AssertionError("recovery reached a mutation service")
        services = replace(
            services,
            futures_query_order=lambda *_args, **_kwargs: absent,
            futures_build_open_order=no_mutation,
            futures_build_reduce_only_close=no_mutation,
            futures_submit_order_once=no_mutation,
            futures_set_leverage_1x_once=no_mutation,
        )
        recovered = jr._recover_pending_momentum(
            self.paths, self.receipt_dir, set(), NOW,
            execution_services=services,
        )
        self.assertEqual(recovered[3].verdict, "REFUSED")
        self.assertEqual(mutations, [])
        state = json.loads(Path(
            self.paths["momentum_position"]
        ).read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "NONE")

    def test_roundtrip_recovery_preserves_owner_token_without_reposting(self):
        transport = FuturesTransport(self.paths)
        services = self.services(transport)

        def stop_after_wire(actual_transport, base_url, plan, write_intent,
                            **_kwargs):
            write_intent(plan.intent)
            actual_transport(
                "POST", base_url, jr.binance_futures.FUTURES_ORDER_PATH,
                plan.intent.params, True,
            )
            raise SystemExit(75)

        services = replace(
            services, futures_submit_order_once=stop_after_wire,
        )
        with self.assertRaisesRegex(SystemExit, "75"):
            jr._run_momentum_open(
                self.job("recover-roundtrip", "FUTURES_ROUNDTRIP"),
                self.receipt_dir, self.paths, NOW, "owner-token",
                execution_services=services,
            )
        Path(self.paths["claims"], "job-recover-roundtrip.claim").touch()
        recovered = jr._recover_pending_momentum(
            self.paths, self.receipt_dir, set(), NOW,
            execution_services=services,
        )
        self.assertEqual(recovered[:3], (
            "recover-roundtrip", "FUTURES_ROUNDTRIP", SYMBOL,
        ))
        self.assertEqual(recovered[3].verdict, "OPEN_EXPOSURE")
        self.assertEqual(recovered[4], "owner-token")
        self.assertEqual(len(transport.posts), 1)

    def test_exit_stays_unknown_when_income_does_not_bind_its_fill(self):
        transport = FuturesTransport(self.paths)
        services = self.services(transport)
        opened = jr._run_momentum_open(
            self.job("income-open", "MOMENTUM_OPEN"), self.receipt_dir,
            self.paths, NOW, execution_services=services,
        )
        self.assertEqual(opened.verdict, "OPEN")
        transport.income_trade_shift = 1
        closed = jr._run_momentum_exit(
            self.job("income-close", "MOMENTUM_EXIT"), self.receipt_dir,
            self.paths, NOW, execution_services=services,
        )
        self.assertEqual((closed.verdict, closed.exit_code), ("UNKNOWN", 4))
        state = json.loads(Path(
            self.paths["momentum_position"]
        ).read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
