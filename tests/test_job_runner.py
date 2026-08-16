#!/usr/bin/env python3
"""Offline deterministic tests for the restricted live-test job runner."""

import hashlib
import json
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import job_runner as jr  # noqa: E402


NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
SHA = "7ac26aa771937f802930dac5eb017d6583d968d8"
STANDING_REPAY_FACTS = {
    "job_id": "mproofr-2c2a3027d877",
    "release_sha": "4a7e2634361892dccf41137fbd88fe62ae8e3281",
    "ready_params": {
        "asset": "USDT", "amount": "2.5000387", "type": "REPAY",
    },
    "ready_expected": {
        "asset": "USDT", "principal": "2.5000387", "interest": "0",
        "status": "REPAY_CONFIRMED",
    },
    "account_liability_before": {
        "asset": "USDT", "borrowed": "2.5", "interest": "0.0000387",
        "total_liability": "2.5000387",
    },
    "submission": {
        "kind": "ok", "transaction_id": "401382497303",
    },
    "history_watermark": 401344857108,
    "history_row": {
        "timestamp": 1786865269293,
        "txId": 401382497303,
        "asset": "USDT",
        "amount": "2.5000387",
        "principal": "2.5",
        "interest": "0.0000387",
        "status": "CONFIRMED",
        "clientTag": "",
        "type": "MANUAL",
    },
    "account_after": {
        "asset": "USDT", "borrowed": "0", "interest": "0",
    },
}


def utc(value):
    return value.isoformat().replace("+00:00", "Z")


def execution_services(transport, sleep=lambda _delay: None):
    return jr.ExecutionServices(
        transport=transport,
        spot_base_url=jr.binance_http.SPOT_BASE_URL,
        futures_base_url=jr.binance_http.FUTURES_BASE_URL,
        sleep=sleep,
        margin_build_borrow=jr.binance_margin.build_borrow,
        margin_build_repay=jr.binance_margin.build_repay,
        margin_read_history_by_direction=
        jr.binance_margin.read_history_by_direction,
        margin_read_history_by_tx_id=
        jr.binance_margin.read_history_by_tx_id,
        margin_read_liability=jr.binance_margin.read_liability,
        margin_submit_once=jr.binance_margin.submit_once,
        margin_confirm=jr.binance_margin.confirm,
        futures_read_market=jr.binance_futures.read_market,
        futures_read_preflight=jr.binance_futures.read_preflight,
        futures_set_leverage_1x_once=
        jr.binance_futures.set_leverage_1x_once,
        futures_build_open_order=jr.binance_futures.build_open_order,
        futures_build_reduce_only_close=
        jr.binance_futures.build_reduce_only_close,
        futures_query_order=jr.binance_futures.query_order,
        futures_submit_order_once=jr.binance_futures.submit_order_once,
        futures_read_fills=jr.binance_futures.read_fills,
        futures_read_income=jr.binance_futures.read_income,
        spot_read_market=jr.binance_spot.read_market,
        spot_read_account_state=jr.binance_spot.read_account_state,
        spot_build_market_order=jr.binance_spot.build_market_order,
        spot_query_order=jr.binance_spot.query_order,
        spot_submit_order_once=jr.binance_spot.submit_order_once,
        spot_read_fills=jr.binance_spot.read_fills,
        spot_fees_by_asset=jr.binance_spot.fees_by_asset,
        spot_net_base_quantity=jr.binance_spot.net_base_quantity,
    )


def momentum_symbol_info():
    return {
        "symbol": "TSTUSDT",
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "orderTypes": ["LIMIT", "MARKET"],
        "timeInForce": ["GTC", "IOC"],
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "tickSize": "0.01",
                "minPrice": "0.01",
                "maxPrice": "1000000",
            },
            {
                "filterType": "LOT_SIZE",
                "stepSize": "0.01",
                "minQty": "0.01",
                "maxQty": "1000",
            },
            {
                "filterType": "MARKET_LOT_SIZE",
                "stepSize": "0.01",
                "minQty": "0.01",
                "maxQty": "500",
            },
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
            {
                "filterType": "PERCENT_PRICE",
                "multiplierUp": "1.05",
                "multiplierDown": "0.95",
            },
        ],
    }


class LeverageTransport:
    def __init__(self, events):
        self.calls = []
        self.events = events
        self.current = 1

    def __call__(self, method, base_url, path, params=None, signed=False):
        values = dict(params or ())
        call = (method, path, values, signed)
        self.calls.append(call)
        self.events.append(("leverage", call))
        if base_url != jr.binance_http.FUTURES_BASE_URL:
            raise AssertionError("leverage used the wrong venue")
        if method == "GET" and path == "/fapi/v1/symbolConfig":
            symbol = values.get("symbol", "")
            return jr.binance_http.HttpResult(
                "ok", [{"symbol": symbol, "leverage": str(self.current)}],
                None, None,
            )
        if method == "POST" and path == "/fapi/v1/leverage" \
                and values.get("leverage") == "1":
            self.current = 1
            return jr.binance_http.HttpResult(
                "ok", {"leverage": 1}, None, None
            )
        raise AssertionError("unexpected leverage transport call")


class MarginTransport:
    def __init__(self):
        self.calls = []
        self.operation = None
        self.post_kind = "ok"
        self.post_payload = {"tranId": 7301}
        self.history_kind = "ok"
        self.history_payload = None
        self.history_before_payloads = {}
        self.history_after_payloads = {}
        self.history_after_sequences = {}
        self.account_kind = "ok"
        self.account_before_payload = None
        self.account_payload = None

    def __call__(self, method, base_url, path, params=None, signed=False):
        params = dict(params or {})
        call = (method, base_url, path, params, signed)
        self.calls.append(call)
        if base_url != jr.binance_http.SPOT_BASE_URL or not signed:
            raise AssertionError("margin request was not a signed Spot call")
        if method == "POST" and path == jr.MARGIN_ENDPOINT_PATH:
            self.operation = dict(params)
            return self.result(self.post_kind, self.post_payload)
        if method == "GET" and path == jr.MARGIN_ENDPOINT_PATH:
            request_type = params.get("type")
            if request_type not in {"BORROW", "REPAY"}:
                raise AssertionError("margin history direction was missing")
            if self.operation is None:
                payload = self.history_before_payloads.get(
                    request_type, {"rows": []}
                )
                return self.result(
                    self.history_kind, self.history_payload_for(payload, params)
                )
            sequence = self.history_after_sequences.get(request_type)
            if sequence:
                return self.result(
                    self.history_kind,
                    self.history_payload_for(sequence.pop(0), params),
                )
            if request_type in self.history_after_payloads:
                return self.result(
                    self.history_kind, self.history_payload_for(
                        self.history_after_payloads[request_type], params
                    ),
                )
            if self.history_payload is not None:
                payload = self.history_payload \
                    if request_type == self.operation["type"] \
                    else {"rows": []}
                return self.result(
                    self.history_kind, self.history_payload_for(payload, params)
                )
            if request_type != self.operation["type"]:
                return self.result(
                    self.history_kind, {"rows": [], "total": 0}
                )
            transaction_id = self.post_payload.get(
                "tranId", self.post_payload.get("txId", "")
            )
            row = {
                "txId": transaction_id,
                "asset": self.operation["asset"],
                "status": "CONFIRMED",
                "clientTag": "",
                "type": "MANUAL",
            }
            if self.operation["type"] == "REPAY":
                before_rows = (
                    self.account_before_payload or {"userAssets": []}
                ).get("userAssets", [])
                before = next(
                    value for value in before_rows
                    if value.get("asset") == self.operation["asset"]
                )
                row.update({
                    "amount": self.operation["amount"],
                    "principal": before["borrowed"],
                    "interest": before["interest"],
                })
            else:
                row.update({
                    "amount": self.operation["amount"],
                    "principal": self.operation["amount"], "interest": "0",
                })
            row["timestamp"] = int(NOW.timestamp() * 1000)
            return self.result(
                self.history_kind, {"rows": [row], "total": 1}
            )
        if method == "GET" and path == "/sapi/v1/margin/account":
            if self.operation is None:
                if self.account_before_payload is not None:
                    return self.result(
                        self.account_kind, self.account_before_payload
                    )
                return self.result(self.account_kind, {"userAssets": [{
                    "asset": "USDT",
                    "borrowed": "0",
                    "interest": "0",
                }]})
            if self.account_payload is not None:
                return self.result(self.account_kind, self.account_payload)
            borrowed = self.operation["amount"] \
                if self.operation["type"] == "BORROW" else "0"
            return self.result(self.account_kind, {"userAssets": [{
                "asset": self.operation["asset"],
                "borrowed": borrowed,
                "interest": "0",
            }]})
        raise AssertionError("unexpected margin request")

    @staticmethod
    def history_payload_for(payload, params):
        if not isinstance(payload, dict) or not isinstance(
                payload.get("rows"), list):
            return payload
        converted = dict(payload)
        rows = []
        for value in payload["rows"]:
            if not isinstance(value, dict):
                rows.append(value)
                continue
            row = dict(value)
            if type(row.get("txId")) is str and row["txId"].isdigit():
                row["txId"] = int(row["txId"])
            if type(row.get("txId")) is int \
                    and type(row.get("asset")) is str \
                    and type(row.get("status")) is str \
                    and type(row.get("type")) is str:
                row.setdefault("timestamp", int(NOW.timestamp() * 1000))
                direction = params.get("type")
                if direction == "BORROW":
                    principal = row.get("principal", row.get("amount"))
                    interest = row.get("interest", "0")
                    if type(principal) is str and type(interest) is str:
                        try:
                            amount = Decimal(principal) + Decimal(interest)
                        except ArithmeticError:
                            pass
                        else:
                            row.setdefault("principal", principal)
                            row.setdefault("interest", interest)
                            row.setdefault("amount", format(amount, "f"))
            rows.append(row)
        if "txId" in params:
            rows = [row for row in rows if isinstance(row, dict)
                    and str(row.get("txId")) == str(params["txId"])]
        converted["rows"] = rows
        converted["total"] = len(rows)
        return converted

    @staticmethod
    def result(kind, payload):
        if kind == "ok":
            return jr.binance_http.HttpResult("ok", payload, None, None)
        code = payload.get("code") if isinstance(payload, dict) else None
        code = code if type(code) is int else None
        message = payload.get("msg") if isinstance(payload, dict) else None
        message = message if type(message) is str else "offline_result"
        if kind == "api_error":
            return jr.binance_http.HttpResult(
                "api_error", None, code, message
            )
        return jr.binance_http.HttpResult("unclear", None, code, message)


class FuturesTransport:
    """Serve complete typed Futures rows through one injected transport."""

    def __init__(self, owner, leverage):
        self.owner = owner
        self.leverage = leverage
        self.calls = []
        self.snapshots = []
        self.active = None
        self.position_failures = []
        self.quantity = Decimal("0")
        self.orders = {}

    def queue(self, risks, positions, orders=(), leverage=1):
        self.snapshots.append({
            "/fapi/v3/positionRisk": [dict(row) for row in risks],
            "/fapi/v3/account": {
                "positions": [dict(row) for row in positions],
            },
            "/fapi/v1/openOrders": [dict(row) for row in orders],
            "/fapi/v1/symbolConfig": [{
                "symbol": "TSTUSDT",
                "leverage": str(leverage),
            }],
        })

    def queue_position_failure(self, kind, payload, count=1):
        self.position_failures.extend(
            (kind, dict(payload)) for _unused in range(count)
        )

    def current_snapshot(self):
        row = {
            "symbol": "TSTUSDT", "positionSide": "BOTH",
            "positionAmt": format(self.quantity, "f"),
            "markPrice": "200",
        }
        return {
            "/fapi/v3/positionRisk": [dict(row)],
            "/fapi/v3/account": {"positions": [dict(row)]},
            "/fapi/v1/openOrders": [],
            "/fapi/v1/symbolConfig": [{
                "symbol": "TSTUSDT", "leverage": str(self.leverage.current),
            }],
        }

    def start_snapshot(self):
        if self.active is None:
            if self.position_failures:
                self.active = self.current_snapshot()
            elif self.snapshots:
                self.active = self.snapshots.pop(0)
            else:
                self.active = self.current_snapshot()

    @staticmethod
    def result(payload):
        return jr.binance_http.HttpResult("ok", payload, None, None)

    def order_row(self, wire):
        close = wire["reduceOnly"] == "true"
        quantity = Decimal(wire["quantity"])
        price = Decimal("205") if close else Decimal("200")
        quote = quantity * price
        if not close and self.owner.open_quote_override is not None:
            quote = Decimal(self.owner.open_quote_override)
        row = {
            "symbol": "TSTUSDT", "clientOrderId": wire["newClientOrderId"],
            "orderId": 2202 if close else 1101, "status": "FILLED",
            "side": wire["side"], "reduceOnly": close,
            "executedQty": format(quantity, "f"),
            "cumQuote": format(quote, "f"),
            "trade_id": 92 if close else 91, "price": price,
        }
        return row

    def __call__(self, method, base_url, path, params=None, signed=False):
        if base_url != jr.binance_http.FUTURES_BASE_URL:
            raise AssertionError("Futures request used the wrong venue")
        call = (method, path, dict(params or {}), signed)
        self.calls.append(call)
        self.owner.momentum_http_calls.append(call)
        wire = call[2]
        if method == "GET" and path == "/fapi/v1/exchangeInfo":
            return self.result({"symbols": [momentum_symbol_info()]})
        if method == "GET" and path == "/fapi/v1/ticker/bookTicker":
            return self.result({
                "symbol": "TSTUSDT", "bidPrice": "199.99",
                "askPrice": "200",
            })
        if method == "GET" and path == "/fapi/v1/premiumIndex":
            mark = "200"
            if self.snapshots:
                rows = self.snapshots[0]["/fapi/v3/positionRisk"]
                if rows and rows[0].get("markPrice") is not None:
                    mark = rows[0]["markPrice"]
            return self.result({"symbol": "TSTUSDT", "markPrice": mark})
        if method == "GET" and path == "/fapi/v1/accountConfig":
            return self.result({"canTrade": True})
        if method == "GET" and path == "/fapi/v1/positionSide/dual":
            return self.result({"dualSidePosition": False})
        if method == "GET" and path == "/fapi/v3/balance":
            return self.result([{"asset": "USDT", "availableBalance": "100"}])
        if method == "GET" and path == "/fapi/v3/account":
            self.start_snapshot()
            return self.result(self.active[path])
        if method == "GET" and path == "/fapi/v1/openOrders":
            self.start_snapshot()
            return self.result(self.active[path])
        if path == "/fapi/v3/positionRisk" and self.position_failures:
            kind, payload = self.position_failures.pop(0)
            if not self.position_failures:
                self.active = None
            return jr.binance_http.HttpResult(
                kind, None, payload.get("code"), "offline_position_read"
            )
        if method == "GET" and path == "/fapi/v3/positionRisk":
            self.start_snapshot()
            return self.result(self.active[path])
        if method == "GET" and path == "/fapi/v1/symbolConfig" \
                and self.active is not None:
            payload = self.active[path]
            self.active = None
            return self.result(payload)
        if path == "/fapi/v1/symbolConfig":
            return self.leverage(method, base_url, path, params, signed)
        if method == "POST" and path == "/fapi/v1/leverage":
            return self.leverage(method, base_url, path, params, signed)
        if method == "POST" and path == "/fapi/v1/order":
            self.owner.momentum_intents.append(dict(wire))
            state = json.loads(Path(
                jr._paths(self.owner.receipt_dir)["momentum_position"]
            ).read_text(encoding="utf-8"))
            if state["pending_intent"]["job_op"] in {
                    "FUTURES_ROUNDTRIP", "MOMENTUM_ROUNDTRIP"} \
                    and state["pending_intent"]["action"] == "OPEN":
                self.owner.hand_calls.append({"intent": dict(wire)})
                self.owner.events.append(("futures", dict(wire)))
            if self.owner.momentum_query_state != "terminal" \
                    or self.owner.hand_verdict != "CLOSED":
                return jr.binance_http.HttpResult(
                    "unclear", None, None, "offline_pending_order"
                )
            order = self.order_row(wire)
            self.orders[wire["newClientOrderId"]] = order
            self.owner.momentum_order = dict(order)
            self.quantity = Decimal("0") \
                if order["reduceOnly"] else Decimal(order["executedQty"])
            return self.result({
                "clientOrderId": wire["newClientOrderId"],
                "reduceOnly": order["reduceOnly"],
            })
        if method == "GET" and path == "/fapi/v1/order":
            order = self.orders.get(wire.get("origClientOrderId"))
            if order is None:
                return jr.binance_http.HttpResult(
                    "unclear", None, None, "offline_order_pending"
                )
            return self.result(dict(order))
        if method == "GET" and path == "/fapi/v1/userTrades":
            order_id = int(wire["orderId"])
            order = next(row for row in self.orders.values()
                         if row["orderId"] == order_id)
            return self.result([{
                "symbol": "TSTUSDT", "id": order["trade_id"],
                "orderId": order_id, "price": format(order["price"], "f"),
                "qty": order["executedQty"], "quoteQty": order["cumQuote"],
                "commission": "0.002" if order["reduceOnly"] else "0.003",
                "commissionAsset": "USDT", "side": order["side"],
                "buyer": order["side"] == "BUY", "maker": False,
                "time": 1, "realizedPnl": "0.25"
                if order["reduceOnly"] else "0",
            }])
        if method == "GET" and path == "/fapi/v1/income":
            order = next(row for row in self.orders.values()
                         if row["reduceOnly"])
            return self.result([{
                "tranId": 501, "symbol": "TSTUSDT",
                "incomeType": "REALIZED_PNL", "income": "0.25",
                "asset": "USDT", "time": int(wire["startTime"]) + 1,
                "tradeId": order["trade_id"],
            }])
        raise AssertionError("unexpected Futures request: %s %s" % (
            method, path,
        ))


class ExecutionTransport:
    def __init__(self, margin, futures):
        self.margin = margin
        self.futures = futures

    def __call__(self, method, base_url, path, params=None, signed=False):
        target = self.margin if base_url == jr.binance_http.SPOT_BASE_URL \
            else self.futures
        return target(method, base_url, path, params, signed)


_SAME_POSITION = object()


class JobRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.receipt_dir = temporary.name
        self.jobs_dir = os.path.join(self.receipt_dir, "jobs")
        os.makedirs(self.jobs_dir)
        self.write_operator()
        self.write_accept_one()
        self.hand_calls = []
        self.basis_calls = []
        self.events = []
        self.hand_verdict = "CLOSED"
        self.hand_exit = 0
        self.leverage = LeverageTransport(self.events)
        self.margin = MarginTransport()
        self.momentum_intents = []
        self.momentum_order = None
        self.momentum_query_state = "terminal"
        self.momentum_http_calls = []
        self.open_quote_override = None
        self.momentum_reads = FuturesTransport(self, self.leverage)
        self.margin_sleep_delays = []
        self.execution_services = execution_services(
            ExecutionTransport(self.margin, self.momentum_reads),
            self.margin_sleep_delays.append,
        )
        self.real_build_execution_services = jr._build_execution_services
        service_builder = mock.patch.object(
            jr, "_build_execution_services",
            side_effect=lambda: self.execution_services,
        )
        service_builder.start()
        self.addCleanup(service_builder.stop)
        self.governor_budget = (True, "")
        self.governor_budget_calls = []
        self.governor_acquire_calls = []
        self.governor_acquire_refusal = ""
        self.governor_releases = []
        self.governor_realized = []

        class FakeGovernorRefusal(Exception):
            def __init__(inner_self, reason):
                super().__init__(reason)
                inner_self.reason = reason

        governor = types.ModuleType("account_governor")
        governor.GovernorRefusal = FakeGovernorRefusal

        def budgets_allow(receipt_dir):
            self.governor_budget_calls.append(receipt_dir)
            return self.governor_budget

        def acquire_entry_slot(op, symbol, receipt_dir):
            self.governor_acquire_calls.append(
                (op, symbol, receipt_dir)
            )
            if self.governor_acquire_refusal:
                raise FakeGovernorRefusal(self.governor_acquire_refusal)
            return "governor-token-%s" % len(self.governor_acquire_calls)

        def release_entry_slot(token, receipt_dir):
            self.governor_releases.append((token, receipt_dir))
            lock_path = os.path.join(
                receipt_dir, jr.ACCOUNT_GOVERNOR_LOCK_FILE
            )
            if os.path.isfile(lock_path):
                with open(lock_path, encoding="utf-8") as handle:
                    owner = json.load(handle)
                if owner.get("token") == token:
                    os.unlink(lock_path)

        def record_realized(net_usdt, fees_usdt, receipt_dir):
            self.governor_realized.append(
                (net_usdt, fees_usdt, receipt_dir)
            )

        governor.budgets_allow = budgets_allow
        governor.acquire_entry_slot = acquire_entry_slot
        governor.release_entry_slot = release_entry_slot
        governor.record_realized = record_realized
        governor_patch = mock.patch.dict(
            sys.modules, {"account_governor": governor}
        )
        governor_patch.start()
        self.addCleanup(governor_patch.stop)

        env = mock.patch.dict(os.environ, {
            "RECEIPT_DIR": self.receipt_dir,
            "QUANT_SHA": SHA,
            "BINANCE_API_KEY": "offline-key",
            "BINANCE_API_SECRET": "offline-secret",
            "MAX_SLIPPAGE_BPS": "0",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)

        basis = mock.patch.object(jr.basis_executor, "main", self.fake_basis)
        basis.start()
        self.addCleanup(basis.stop)

        def deny(*_args, **_kwargs):
            raise AssertionError("offline job-runner test attempted network")

        for target, name in ((urllib.request, "urlopen"),
                             (socket, "socket"),
                             (socket, "create_connection")):
            patcher = mock.patch.object(target, name, deny)
            patcher.start()
            self.addCleanup(patcher.stop)

    def job(self, job_id, **overrides):
        value = {
            "job_id": job_id,
            "op": "FUTURES_ROUNDTRIP",
            "symbol": "TSTUSDT",
            "max_notional": 6,
            "created_utc": utc(NOW - timedelta(minutes=1)),
            "expires_utc": utc(NOW + timedelta(minutes=10)),
            "requested_by": "lead",
        }
        value.update(overrides)
        path = os.path.join(self.jobs_dir, "job-%s.json" % job_id)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return path

    def margin_job(self, job_id, op="MARGIN_BORROW", **overrides):
        value = {
            "job_id": job_id,
            "op": op,
            "asset": "USDT",
            "principal": "3",
            "intent_id": "mrgintent-" + hashlib.sha256(
                job_id.encode("ascii")
            ).hexdigest()[:24],
            "created_utc": utc(NOW - timedelta(minutes=1)),
            "expires_utc": utc(NOW + timedelta(minutes=10)),
            "requested_by": "lead",
            "operator_token": "lead",
        }
        explicit_tag = overrides.pop("client_tag", None)
        value.update(overrides)
        if explicit_tag is None:
            try:
                amount = jr._canonical_decimal_text(
                    Decimal(str(value["principal"]))
                )
                value["client_tag"] = jr._margin_client_tag(
                    value["op"], value["asset"], amount,
                    value["expires_utc"],
                )
            except ValueError:
                value["client_tag"] = "mrg-" + ("0" * 24)
        else:
            value["client_tag"] = explicit_tag
        path = os.path.join(self.jobs_dir, "job-%s.json" % job_id)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return path

    def write_operator(self, operator="lead"):
        path = os.path.join(self.receipt_dir, "OPERATOR")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"operator": operator}, handle)
        return path

    def write_accept_one(self, operator="lead", **overrides):
        value = {
            "operator": operator,
            "created_utc": utc(NOW - timedelta(minutes=1)),
            "expires_utc": utc(NOW + timedelta(minutes=10)),
            "max_notional": "32",
        }
        value.update(overrides)
        path = os.path.join(self.receipt_dir, "ACCEPT-ONE")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return value

    def fake_basis(self, argv):
        index = len(self.basis_calls) + 1
        self.basis_calls.append({
            "argv": list(argv),
            "symbol": os.environ.get("SYMBOL"),
            "max_notional": os.environ.get("MAX_NOTIONAL"),
            "executor_cap": str(jr.basis_executor.MAX_CYCLE_QUOTE),
            "cycle_cap": str(jr.basis_cycle.MAX_CYCLE_QUOTE),
        })
        path = os.path.join(
            self.receipt_dir, "basis-executor-offline%s.json" % index
        )
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"verdict": "CLOSED"}, handle)
        return 0

    def tick(self, **kwargs):
        kwargs.setdefault("execution_services", self.execution_services)
        return jr.tick(receipt_dir=self.receipt_dir, now=NOW, **kwargs)

    def margin_services(self, request):
        return execution_services(request, self.margin_sleep_delays.append)

    def reconcile_margin(self, job_id, request=None, services=None):
        return jr.reconcile_margin(
            job_id, True, receipt_dir=self.receipt_dir, now=NOW,
            execution_services=services or self.margin_services(
                request or self.margin
            ),
        )

    @staticmethod
    def adapter_margin_row(**overrides):
        row = {
            "txId": 7301, "timestamp": int(NOW.timestamp() * 1000),
            "asset": "USDT", "amount": "3", "principal": "3",
            "interest": "0", "status": "CONFIRMED", "type": "MANUAL",
        }
        row.update(overrides)
        return row

    @staticmethod
    def read_margin_history(rows, direction="BORROW", tx_id=7301):
        def transport(*_args, **_kwargs):
            return jr.binance_http.HttpResult(
                "ok", {"rows": rows, "total": len(rows)}, None, None
            )
        return jr.binance_margin.read_history_by_tx_id(
            transport, jr.binance_http.SPOT_BASE_URL, "USDT", direction,
            tx_id, sleep=lambda _delay: None,
        )

    @staticmethod
    def read_margin_liability(payload):
        def transport(*_args, **_kwargs):
            return jr.binance_http.HttpResult("ok", payload, None, None)
        return jr.binance_margin.read_liability(
            transport, jr.binance_http.SPOT_BASE_URL, "USDT",
            sleep=lambda _delay: None,
        )

    def test_execution_service_fixture_binds_the_frozen_adapter_surface(self):
        transport = lambda *_args, **_kwargs: jr.binance_http.HttpResult(
            "unclear", None, None, "offline"
        )
        services = execution_services(transport)
        self.assertEqual(tuple(services.__dataclass_fields__), (
            "transport", "spot_base_url", "futures_base_url", "sleep",
            "margin_build_borrow", "margin_build_repay",
            "margin_read_history_by_direction",
            "margin_read_history_by_tx_id", "margin_read_liability",
            "margin_submit_once", "margin_confirm",
            "futures_read_market", "futures_read_preflight",
            "futures_set_leverage_1x_once", "futures_build_open_order",
            "futures_build_reduce_only_close", "futures_query_order",
            "futures_submit_order_once", "futures_read_fills",
            "futures_read_income", "spot_read_market",
            "spot_read_account_state", "spot_build_market_order",
            "spot_query_order", "spot_submit_order_once",
            "spot_read_fills", "spot_fees_by_asset",
            "spot_net_base_quantity",
        ))
        self.assertIs(services.margin_submit_once,
                      jr.binance_margin.submit_once)
        self.assertIs(services.futures_submit_order_once,
                      jr.binance_futures.submit_order_once)
        self.assertIs(services.spot_submit_order_once,
                      jr.binance_spot.submit_order_once)
        one_call_fault = lambda *_args, **_kwargs: None
        faulted = replace(services, margin_submit_once=one_call_fault)
        self.assertIs(faulted.margin_submit_once, one_call_fault)
        self.assertIs(faulted.transport, transport)

    def test_execution_service_builder_syncs_each_needed_venue_once(self):
        clients = []

        class FakeClient:
            def __init__(inner_self, key, secret):
                inner_self.credentials = (key, secret)
                inner_self.syncs = []
                inner_self.calls = []
                clients.append(inner_self)

            def sync_time(inner_self, base_url, path):
                inner_self.syncs.append((base_url, path))
                return jr.binance_http.HttpResult(
                    "ok", {"serverTime": 1}, None, None
                )

            def __call__(inner_self, method, base_url, path,
                         params=None, signed=False):
                inner_self.calls.append(
                    (method, base_url, path, params, signed)
                )
                return jr.binance_http.HttpResult("ok", {}, None, None)

        with mock.patch.object(jr.binance_http, "BinanceHttp", FakeClient):
            services = self.real_build_execution_services()
        spot = services.spot_base_url
        futures = services.futures_base_url
        services.transport("GET", spot, "/api/v3/exchangeInfo")
        services.transport("GET", spot, "/api/v3/account", (), True)
        services.transport("GET", spot, "/api/v3/openOrders", (), True)
        services.transport("GET", futures, "/fapi/v3/account", (), True)
        self.assertEqual(clients[0].credentials,
                         ("offline-key", "offline-secret"))
        self.assertEqual(clients[0].syncs, [
            (spot, "/api/v3/time"),
            (futures, "/fapi/v1/time"),
        ])

    def test_tick_forwards_one_injected_table_to_each_current_seam(self):
        transport = lambda *_args, **_kwargs: jr.binance_http.HttpResult(
            "unclear", None, None, "offline"
        )
        services = execution_services(transport)
        self.job("service-seam")
        outcome = jr.Outcome("REFUSED", 2, "", "SERVICE_SEAM_TEST")
        with mock.patch.object(
                jr, "_recover_pending_momentum", return_value=None
        ) as recover, mock.patch.object(
                jr, "_max_adverse_stop", return_value=(None, None, False)
        ) as adverse, mock.patch.object(
                jr, "_execute", return_value=outcome
        ) as execute:
            self.assertEqual(self.tick(execution_services=services), 2)
        self.assertIs(recover.call_args.args[-1], services)
        self.assertIs(adverse.call_args.args[-1], services)
        self.assertIs(execute.call_args.kwargs["execution_services"],
                      services)
        self.assertEqual(jr.reconcile_margin(
            "service-seam", False, receipt_dir=self.receipt_dir, now=NOW,
            execution_services=services,
        ), 3)

    def margin_tick(self, mutation_now=NOW, request=None, services=None):
        transport = request or self.margin
        if isinstance(transport, MarginTransport):
            transport.operation = None
        with mock.patch.object(jr, "_utc_now", return_value=mutation_now), \
                mock.patch.object(
                    jr.time, "sleep",
                    side_effect=self.margin_sleep_delays.append,
                ):
            return self.tick(
                execution_services=services or self.margin_services(transport),
            )

    def results(self):
        path = os.path.join(self.jobs_dir, "results.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def reason(self, row):
        with open(row["receipt_path"], encoding="utf-8") as handle:
            return json.load(handle)["reason"]

    def margin_source(self, row):
        with open(row["receipt_path"], encoding="utf-8") as handle:
            runner_receipt = json.load(handle)
        with open(
                runner_receipt["source_receipt_path"],
                encoding="utf-8") as handle:
            return json.load(handle)

    def result_events(self, kind):
        return [row for row in self.results() if row.get("kind") == kind]

    def latest_margin_anomaly(self):
        path = jr._paths(self.receipt_dir)["results"]
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        return json.loads(lines[-1])

    def standing_margin_artifacts(self, job_id):
        row, lock_path = self.interrupt_margin_governor_release(job_id)
        runner_path = row["receipt_path"]
        with open(runner_path, encoding="utf-8") as handle:
            source_path = json.load(handle)["source_receipt_path"]
        paths = {
            "RESULT": jr._paths(self.receipt_dir)["results"],
            "RUNNER_RECEIPT": runner_path,
            "SOURCE_RECEIPT": source_path,
            "JOB_FILE": os.path.join(
                self.jobs_dir, "job-%s.json" % job_id
            ),
            "LOCK": lock_path,
        }
        return paths, {
            path: Path(path).read_bytes() for path in paths.values()
        }

    def restore_margin_artifacts(self, originals):
        for path, encoded in originals.items():
            Path(path).write_bytes(encoded)
        stop = os.path.join(self.receipt_dir, "STOP-JOBS")
        if os.path.exists(stop):
            os.unlink(stop)
        self.governor_releases.clear()

    @staticmethod
    def replace_json_field(path, field, value):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        payload[field] = value
        Path(path).write_text(
            json.dumps(payload, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def replace_with_nested_json(path, field, levels, wide_count=0):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        payload.pop(field)
        prefix = json.dumps(payload, separators=(",", ":"))[:-1]
        if wide_count:
            nested = "[" + ",".join("null" for _unused in range(
                wide_count
            )) + "]"
        else:
            nested = "[" * levels + '"value"' + "]" * levels
        encoded = "%s,%s:%s}\n" % (
            prefix, json.dumps(field), nested
        )
        Path(path).write_text(encoded, encoding="utf-8")

    @staticmethod
    def add_duplicate_json_field(path, field):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        encoded = json.dumps(payload, separators=(",", ":"))[:-1]
        encoded += ",%s:%s}\n" % (
            json.dumps(field),
            json.dumps(payload[field], separators=(",", ":")),
        )
        Path(path).write_text(encoded, encoding="utf-8")

    def assert_margin_anomaly_halt(self, job_id, lock_path, artifact,
                                   field, anomaly_type):
        event = self.latest_margin_anomaly()
        self.assertEqual(event, {
            "kind": "margin_governor_artifact_halted",
            "job_id": job_id,
            "artifact": artifact,
            "field": field,
            "anomaly_type": anomaly_type,
            "lock_exists": True,
            "utc": utc(NOW),
        })
        self.assertTrue(os.path.isfile(lock_path))
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))
        self.assertEqual(self.governor_releases, [])

    def write_tick_lock(self, **overrides):
        paths = jr._paths(self.receipt_dir)
        jr._make_dirs(paths)
        value = {
            "pid": 424242,
            "process_start": "test-owner-start",
            "heartbeat_monotonic_ns": 1_000_000_000,
            "heartbeat_utc": utc(NOW - timedelta(seconds=1)),
            "created_utc": utc(NOW - timedelta(seconds=1)),
            "token": "previous-tick-token",
        }
        value.update(overrides)
        with open(paths["tick_lock"], "w", encoding="ascii") as handle:
            json.dump(value, handle)
        return paths

    def interrupt_margin_governor_release(self, job_id, measured_sha=SHA):
        self.margin_job(job_id)
        lock_path = os.path.join(
            self.receipt_dir, jr.ACCOUNT_GOVERNOR_LOCK_FILE
        )

        class ReleaseInterrupted(Exception):
            pass

        def pause_release(_release, token, _receipt_dir):
            with open(lock_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "version": 1,
                    "token": token,
                    "op": "MARGIN_BORROW",
                    "symbol": "USDT",
                    "utc": utc(NOW),
                }, handle)
            raise ReleaseInterrupted

        with mock.patch.dict(os.environ, {"QUANT_SHA": measured_sha}), \
                mock.patch.object(
                    jr, "_release_governor", side_effect=pause_release):
            with self.assertRaises(ReleaseInterrupted):
                self.margin_tick()
        return self.results()[0], lock_path

    def prepare_unknown_margin_reconcile(self, job_id):
        self.margin.post_payload = {"tranId": 401344857108}
        self.margin.history_payload = {"rows": []}
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "0", "interest": "0",
        }]}
        self.margin.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "2.5",
            "interest": "0.00002726",
        }]}
        self.margin_job(job_id, principal="2.5")
        self.assertEqual(self.margin_tick(), 4)
        row = self.results()[0]
        runner_path = row["receipt_path"]
        with open(runner_path, encoding="utf-8") as handle:
            runner = json.load(handle)
        source_path = runner["source_receipt_path"]
        with open(source_path, encoding="utf-8") as handle:
            source = json.load(handle)
        self.assertEqual(source["history_watermark"], 0)
        lock_path = os.path.join(
            self.receipt_dir, jr.ACCOUNT_GOVERNOR_LOCK_FILE
        )
        with open(lock_path, "w", encoding="utf-8") as handle:
            json.dump({
                "version": 1,
                "token": runner["governor_token"],
                "op": "MARGIN_BORROW",
                "symbol": "USDT",
                "utc": utc(NOW),
            }, handle)
        return {
            "row": row,
            "runner_path": runner_path,
            "source_path": source_path,
            "lock_path": lock_path,
        }

    def prepare_standing_legacy_repay_reconcile(
            self, bind_legacy_release=True, job_id=None):
        facts = json.loads(json.dumps(STANDING_REPAY_FACTS))
        if job_id is not None:
            facts["job_id"] = job_id
        job_id = facts["job_id"]
        self.margin.post_payload = {
            "txId": int(facts["submission"]["transaction_id"]),
        }
        self.margin.history_payload = {"rows": []}
        before = facts["account_liability_before"]
        self.margin.account_before_payload = {"userAssets": [{
            "asset": before["asset"],
            "borrowed": before["borrowed"],
            "interest": before["interest"],
        }]}
        self.margin.account_payload = {
            "userAssets": [facts["account_after"]],
        }
        self.margin_job(
            job_id, op="MARGIN_REPAY",
            principal=facts["ready_params"]["amount"],
        )
        self.assertEqual(self.margin_tick(), 4)
        unknown = self.results()[0]
        with open(unknown["receipt_path"], encoding="utf-8") as handle:
            source_path = json.load(handle)["source_receipt_path"]
        source = json.loads(Path(source_path).read_text(encoding="utf-8"))
        source["ready"]["params"] = facts["ready_params"]
        source["ready"]["expected_ledger_confirmation"] = \
            facts["ready_expected"]
        source["account_liability_before"] = before
        source["submission"] = facts["submission"]
        source["history_watermark"] = facts["history_watermark"]
        source["verdict"] = "UNKNOWN"
        source["reason"] = "MARGIN_CONFIRMATION_MISMATCH"
        if bind_legacy_release:
            source["sha"] = facts["release_sha"]
        Path(source_path).write_text(
            json.dumps(source, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        if bind_legacy_release:
            runner_path = unknown["receipt_path"]
            runner = json.loads(Path(runner_path).read_text(
                encoding="utf-8"
            ))
            runner["sha"] = facts["release_sha"]
            runner["measured_sha"] = facts["release_sha"]
            runner["sha_valid"] = True
            Path(runner_path).write_text(
                json.dumps(runner, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
        return facts, unknown, source_path

    @staticmethod
    def make_margin_reconcile_receipt_legacy(artifacts):
        source_path = artifacts["source_path"]
        source = json.loads(Path(source_path).read_text(encoding="utf-8"))
        source = {
            field: source[field] for field in jr.LEGACY_RECEIPT_FIELDS
        }
        Path(source_path).write_text(
            json.dumps(source, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return source

    @staticmethod
    def margin_halt_row(job_id, token, verdict, source_exists):
        return {
            "kind": "margin_governor_release_halted", "job_id": job_id,
            "lock_exists": True, "lock_token": token,
            "lock_op": "MARGIN_BORROW", "lock_symbol": "USDT",
            "runner_receipt_exists": True, "runner_verdict": verdict,
            "source_receipt_exists": source_exists,
            "source_verdict": verdict if source_exists else "ABSENT",
            "utc": utc(NOW),
        }

    def momentum_markers(self):
        Path(os.path.join(
            self.jobs_dir, "futures_proof_passed"
        )).touch()
        Path(os.path.join(
            self.jobs_dir, "basis_proof_passed"
        )).touch()

    @staticmethod
    def momentum_row(quantity, position_side="BOTH", mark_price=None):
        row = {
            "symbol": "TSTUSDT",
            "positionSide": position_side,
            "positionAmt": str(quantity),
        }
        if mark_price is not None:
            row["markPrice"] = str(mark_price)
        return row

    def queue_momentum_measurement(
            self, risk_quantity, account_quantity=_SAME_POSITION,
            risk_side="BOTH", account_side="BOTH", leverage=1,
            mark_price="200"):
        risks = [] if risk_quantity is None else [self.momentum_row(
            risk_quantity, risk_side, mark_price
        )]
        if account_quantity is _SAME_POSITION:
            account_quantity = risk_quantity
        positions = [] if account_quantity is None else [self.momentum_row(
            account_quantity, account_side
        )]
        self.momentum_reads.queue(
            risks, positions, leverage=leverage
        )

    @contextmanager
    def momentum_runtime(self):
        yield

    @contextmanager
    def momentum_http_runtime(self):
        yield

    def momentum_state(self):
        with open(os.path.join(
                self.receipt_dir, "momentum-position.json"
        ), encoding="utf-8") as handle:
            return json.load(handle)

    def open_momentum(self, job_id="open", max_notional=6):
        self.momentum_markers()
        self.write_accept_one(max_notional=str(max_notional))
        marker = os.path.join(
            self.receipt_dir, "first_autonomous_close_passed"
        )
        cap = Decimal("32") if os.path.isfile(marker) else Decimal("8")
        effective = min(Decimal(str(max_notional)), cap)
        self.job(
            job_id, op="MOMENTUM_OPEN", max_notional=max_notional
        )
        with self.momentum_runtime():
            code = self.tick()
        self.assertEqual(code, 0)
        return effective / Decimal("200")

    def open_momentum_http(self, job_id="http-open", max_notional=6):
        self.momentum_markers()
        self.write_accept_one(max_notional=str(max_notional))
        marker = os.path.join(
            self.receipt_dir, "first_autonomous_close_passed"
        )
        cap = Decimal("32") if os.path.isfile(marker) else Decimal("8")
        effective = min(Decimal(str(max_notional)), cap)
        quantity = effective / Decimal("200")
        self.job(job_id, op="MOMENTUM_OPEN", max_notional=max_notional)
        with mock.patch.dict(
                os.environ, {"MAX_SLIPPAGE_BPS": "0"}), \
                self.momentum_http_runtime():
            code = self.tick()
        self.assertEqual(code, 0)
        self.assertEqual(self.momentum_state()["state"], "OPEN")
        return quantity

    def test_unknown_field_is_recorded_and_never_executed(self):
        self.job("unknown", extra="not allowed")
        self.assertEqual(self.tick(), 2)
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.results()[0]["verdict"], "REFUSED")
        self.assertEqual(self.reason(self.results()[0]), "UNKNOWN_FIELD")

    def test_bad_type_is_recorded_and_never_executed(self):
        self.job("badtype", max_notional="6")
        self.assertEqual(self.tick(), 2)
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.reason(self.results()[0]), "BAD_TYPE")

    def test_bad_op_is_recorded_and_never_executed(self):
        self.job("badop", op="TRANSFER")
        self.assertEqual(self.tick(), 2)
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.reason(self.results()[0]), "BAD_OP")

    def test_oversize_notional_is_clamped_to_first_trade_cap(self):
        self.write_accept_one(max_notional="800")
        self.job("clamped", max_notional=800)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual(
            [intent["reduceOnly"] for intent in self.momentum_intents],
            ["false", "true"],
        )
        self.assertEqual(self.momentum_intents[0]["quantity"], "0.04")
        self.assertEqual(self.momentum_state()["effective_notional"], "8")
        self.assertEqual(len(self.leverage.calls), 2)

    def test_missing_expiry_is_recorded_and_never_executed(self):
        path = self.job("noexpiry")
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        del value["expires_utc"]
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        self.assertEqual(self.tick(), 2)
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.reason(self.results()[0]), "MISSING_FIELD")

    def test_expired_job_is_recorded_and_never_executed(self):
        self.job(
            "expired",
            created_utc=utc(NOW - timedelta(minutes=14)),
            expires_utc=utc(NOW - timedelta(minutes=1)),
        )
        self.assertEqual(self.tick(), 2)
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.reason(self.results()[0]), "EXPIRED")

    def test_claim_once_same_file_never_executes_twice(self):
        self.job("once")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual(len(self.results()), 1)
        claim = os.path.join(self.jobs_dir, "claims", "job-once.claim")
        self.assertTrue(os.path.isfile(claim))

    def test_dead_tick_lock_is_recovered_without_waiting(self):
        paths = self.write_tick_lock()
        with mock.patch.object(
                jr.os, "kill", side_effect=ProcessLookupError), \
                mock.patch.object(
                    jr.time, "monotonic_ns", return_value=2_000_000_000):
            self.assertEqual(self.tick(), 0)

        self.assertFalse(os.path.exists(paths["tick_lock"]))
        events = self.result_events("runner_stale_lock_recovered")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["pid"], "424242")
        self.assertEqual(events[0]["age_seconds"], "1")
        result_ids, bad_results, unresolved = jr._result_ids(paths["results"])
        self.assertEqual(result_ids, set())
        self.assertFalse(bad_results)
        self.assertFalse(unresolved)

    def test_zero_byte_tick_lock_is_reclaimed_as_dead_evidence(self):
        paths = jr._paths(self.receipt_dir)
        Path(paths["tick_lock"]).touch()

        self.assertEqual(self.tick(), 0)

        self.assertFalse(os.path.exists(paths["tick_lock"]))
        self.assertEqual(self.result_events("runner_stale_lock_recovered"), [{
            "kind": "runner_stale_lock_recovered", "pid": "UNKNOWN",
            "age_seconds": "UNKNOWN", "utc": utc(NOW),
        }])

    def test_matching_live_tick_owner_is_never_recovered(self):
        paths = self.write_tick_lock(
            heartbeat_monotonic_ns=0,
            heartbeat_utc=utc(NOW - timedelta(hours=1)),
            created_utc=utc(NOW - timedelta(hours=1)),
        )
        with mock.patch.object(jr.os, "kill", return_value=None), \
                mock.patch.object(
                    jr, "_process_start_marker",
                    return_value="test-owner-start"), \
                mock.patch.object(
                    jr.time, "monotonic_ns",
                    return_value=3_600_000_000_000):
            self.assertEqual(self.tick(), 0)

        self.assertTrue(os.path.isfile(paths["tick_lock"]))
        self.assertEqual(self.results(), [])

    def test_tick_lock_pid_reuse_is_recovered(self):
        paths = self.write_tick_lock()

        def process_start(pid):
            return "replacement-start" if pid == 424242 else "current-start"

        with mock.patch.object(jr.os, "kill", return_value=None), \
                mock.patch.object(
                    jr, "_process_start_marker", side_effect=process_start), \
                mock.patch.object(
                    jr.time, "monotonic_ns", return_value=2_000_000_000):
            self.assertEqual(self.tick(), 0)

        self.assertFalse(os.path.exists(paths["tick_lock"]))
        events = self.result_events("runner_stale_lock_recovered")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["pid"], "424242")

    def test_unconfirmed_live_lock_uses_three_interval_boundary(self):
        threshold_ns = int(
            jr.TICK_INTERVAL_SECONDS
            * jr.TICK_LOCK_HEARTBEAT_INTERVALS
            * 1_000_000_000
        )
        paths = self.write_tick_lock(
            heartbeat_utc=utc(NOW - timedelta(seconds=180)),
            created_utc=utc(NOW - timedelta(seconds=180)),
        )

        def process_start(pid):
            return None if pid == 424242 else "current-start"

        with mock.patch.object(jr.os, "kill", return_value=None), \
                mock.patch.object(
                    jr, "_process_start_marker", side_effect=process_start), \
                mock.patch.object(
                    jr.time, "monotonic_ns",
                    return_value=1_000_000_000 + threshold_ns):
            self.assertEqual(self.tick(), 0)
        self.assertTrue(os.path.isfile(paths["tick_lock"]))

        os.unlink(paths["tick_lock"])
        self.write_tick_lock(
            heartbeat_utc=utc(NOW - timedelta(seconds=180)),
            created_utc=utc(NOW - timedelta(seconds=180)),
        )
        with mock.patch.object(jr.os, "kill", return_value=None), \
                mock.patch.object(
                    jr, "_process_start_marker", side_effect=process_start), \
                mock.patch.object(
                    jr.time, "monotonic_ns",
                    return_value=1_000_000_001 + threshold_ns):
            self.assertEqual(self.tick(), 0)
        self.assertFalse(os.path.exists(paths["tick_lock"]))
        self.assertEqual(
            self.result_events("runner_stale_lock_recovered")[0][
                "age_seconds"
            ],
            "180.000000001",
        )

    def test_monotonic_reset_uses_utc_heartbeat_for_reclaim(self):
        paths = self.write_tick_lock(
            heartbeat_monotonic_ns=10_000_000_000,
            heartbeat_utc=utc(NOW - timedelta(seconds=181)),
            created_utc=utc(NOW - timedelta(seconds=181)),
        )

        def process_start(pid):
            return None if pid == 424242 else "current-start"

        with mock.patch.object(jr.os, "kill", return_value=None), \
                mock.patch.object(
                    jr, "_process_start_marker", side_effect=process_start), \
                mock.patch.object(
                    jr.time, "monotonic_ns", return_value=1_000_000_000):
            self.assertEqual(self.tick(), 0)

        self.assertFalse(os.path.exists(paths["tick_lock"]))
        self.assertEqual(
            self.result_events("runner_stale_lock_recovered")[0][
                "age_seconds"
            ],
            "181",
        )

    def test_tick_refreshes_its_owned_lock_heartbeat(self):
        with mock.patch.object(
                jr, "_refresh_tick_lock",
                wraps=jr._refresh_tick_lock) as refresh:
            self.assertEqual(self.tick(), 0)
        self.assertEqual(refresh.call_count, 1)

    def test_owner_marker_failure_during_creation_halts_factually(self):
        paths = jr._paths(self.receipt_dir)

        with mock.patch.object(
                jr, "_process_start_marker", return_value=None), \
                mock.patch("builtins.print") as output:
            self.assertEqual(self.tick(), 3)

        output.assert_called_once_with(
            "JOB-RUNNER | HALTED | runner heartbeat unavailable"
        )
        self.assertTrue(os.path.isfile(paths["stop"]))
        self.assertFalse(os.path.exists(paths["tick_lock"]))
        self.assertEqual(self.results(), [])

    def test_tick_consumes_at_most_one_due_job(self):
        self.job("a")
        self.job("b")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual([row["job_id"] for row in self.results()], ["a"])
        self.write_accept_one()
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.hand_calls), 2)
        self.assertEqual([row["job_id"] for row in self.results()], ["a", "b"])

    def test_every_entry_class_requires_ticket_armed_or_sealed(self):
        os.unlink(os.path.join(self.receipt_dir, "ACCEPT-ONE"))
        sequence = 0
        for marker in (None, "STOP", "STOP-JOBS"):
            for op in sorted(jr.ENTRY_OPS):
                with self.subTest(marker=marker, op=op):
                    sequence += 1
                    job_id = "stopped-%s" % sequence
                    if op in jr.MARGIN_OPS:
                        self.margin_job(job_id, op=op)
                    else:
                        self.job(job_id, op=op)
                    for name in ("STOP", "STOP-JOBS"):
                        try:
                            os.unlink(os.path.join(self.receipt_dir, name))
                        except FileNotFoundError:
                            pass
                    if marker:
                        Path(os.path.join(self.receipt_dir, marker)).touch()

                    self.assertEqual(self.tick(), 2)

                    row = self.results()[-1]
                    self.assertEqual(row["job_id"], job_id)
                    self.assertEqual(row["verdict"], "REFUSED")
                    expected_reason = "STOP_JOBS_SEALED" \
                        if marker == "STOP-JOBS" and op in jr.MARGIN_OPS \
                        else "ENTRY_BLOCKED_NOT_ACCEPTED"
                    self.assertEqual(
                        self.reason(row), expected_reason
                    )
                    self.assertTrue(os.path.isfile(os.path.join(
                        self.jobs_dir, "claims", "job-%s.claim" % job_id
                    )))
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.basis_calls, [])
        self.assertEqual(self.momentum_intents, [])
        self.assertEqual(self.margin.calls, [])
        self.assertEqual(self.governor_budget_calls, [])

    def test_accept_one_allows_sealed_entry_and_is_claimed_on_fill(self):
        self.job("accepted-a")
        self.job("accepted-b")
        stop = os.path.join(self.receipt_dir, "STOP")
        accept = os.path.join(self.receipt_dir, "ACCEPT-ONE")
        Path(stop).touch()
        acceptance = self.write_accept_one()

        self.assertEqual(self.tick(), 0)

        self.assertEqual(len(self.hand_calls), 1)
        self.assertFalse(os.path.exists(accept))
        claimed = accept + ".claimed-accepted-a"
        self.assertTrue(os.path.isfile(claimed))
        with open(claimed, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), acceptance)
        self.assertFalse(os.path.exists(accept + ".claimed"))
        self.assertTrue(os.path.isfile(stop))
        self.assertEqual(self.results()[0]["job_id"], "accepted-a")
        self.assertEqual(len(self.governor_releases), 1)

        self.assertEqual(self.tick(), 2)
        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual(self.results()[-1]["job_id"], "accepted-b")
        self.assertEqual(
            self.reason(self.results()[-1]),
            "ENTRY_BLOCKED_NOT_ACCEPTED",
        )

    def test_accept_one_atomic_consumer_wins_once_under_contention(self):
        marker = os.path.join(self.receipt_dir, "ACCEPT-ONE")
        barrier = threading.Barrier(3)
        outcomes = []

        def consume():
            barrier.wait()
            outcomes.append(jr._consume_accept_one(marker, "race"))

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertFalse(os.path.exists(marker))
        self.assertTrue(os.path.isfile(marker + ".claimed-race"))

    def test_invalid_accept_one_forms_refuse_without_consuming(self):
        valid = self.write_accept_one()
        cases = [
            "{not-json",
            {key: value for key, value in valid.items()
             if key != "expires_utc"},
            dict(
                valid,
                created_utc=utc(NOW - timedelta(minutes=10)),
                expires_utc=utc(NOW - timedelta(seconds=1)),
            ),
            dict(
                valid,
                created_utc=utc(NOW - timedelta(minutes=1)),
                expires_utc=utc(NOW + timedelta(minutes=15)),
            ),
            dict(valid, max_notional=32),
        ]
        for index, marker in enumerate(cases):
            with self.subTest(index=index):
                accept = os.path.join(self.receipt_dir, "ACCEPT-ONE")
                with open(accept, "w", encoding="utf-8") as handle:
                    if isinstance(marker, str):
                        handle.write(marker)
                    else:
                        json.dump(marker, handle)
                job_id = "invalid-accept-%s" % index
                self.job(job_id)

                self.assertEqual(self.tick(), 2)

                row = self.results()[-1]
                self.assertEqual(row["verdict"], "REFUSED")
                self.assertEqual(self.reason(row), "ACCEPT_ONE_INVALID")
                self.assertTrue(os.path.isfile(accept))
                self.assertFalse(os.path.exists(
                    accept + ".claimed-" + job_id
                ))
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.governor_budget_calls, [])

    def test_operator_file_ticket_and_requester_must_match(self):
        cases = [
            ("missing", None, "lead", "lead"),
            ("ticket", "lead", "outsider", "lead"),
            ("request", "lead", "lead", "outsider"),
        ]
        operator_path = os.path.join(self.receipt_dir, "OPERATOR")
        accept = os.path.join(self.receipt_dir, "ACCEPT-ONE")
        for suffix, operator, accepted_by, requested_by in cases:
            with self.subTest(suffix=suffix):
                if operator is None:
                    try:
                        os.unlink(operator_path)
                    except FileNotFoundError:
                        pass
                else:
                    self.write_operator(operator)
                self.write_accept_one(accepted_by)
                job_id = "operator-" + suffix
                self.job(job_id, requested_by=requested_by)

                self.assertEqual(self.tick(), 2)

                row = self.results()[-1]
                self.assertEqual(row["verdict"], "REFUSED")
                self.assertEqual(self.reason(row), "OPERATOR_MISMATCH")
                self.assertTrue(os.path.isfile(accept))
                self.assertFalse(os.path.exists(
                    accept + ".claimed-" + job_id
                ))
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.governor_budget_calls, [])

    def test_margin_whitelist_accepts_exact_borrow_and_repay_jobs(self):
        for op in sorted(jr.MARGIN_OPS):
            with self.subTest(op=op):
                job_id = "margin-shape-" + op.lower().replace("_", "-")
                path = self.margin_job(job_id, op=op)
                raw = jr._load_job(path)

                job = jr._validate_job(
                    raw, job_id, NOW, self.receipt_dir
                )

                self.assertEqual(job.op, op)
                self.assertEqual(job.symbol, "USDT")
                self.assertEqual(job.requested_max_notional, Decimal("3"))
                self.assertEqual(job.effective_notional, Decimal("3"))
                self.assertEqual(job.margin_operation["verdict"], "READY")
                self.assertEqual(set(raw), jr.MARGIN_JOB_FIELDS)

    def test_margin_runner_requires_one_well_formed_intent_id(self):
        missing_path = self.margin_job("margin-intent-missing")
        with open(missing_path, encoding="utf-8") as handle:
            missing = json.load(handle)
        missing.pop("intent_id")
        with open(missing_path, "w", encoding="utf-8") as handle:
            json.dump(missing, handle)

        self.assertEqual(self.margin_tick(), 2)
        self.assertEqual(self.reason(self.results()[-1]), "MISSING_FIELD")

        self.margin_job(
            "margin-intent-malformed",
            intent_id="mrgintent-" + ("A" * 24),
        )
        self.assertEqual(self.margin_tick(), 2)
        self.assertEqual(self.reason(self.results()[-1]), "BAD_INTENT_ID")
        self.assertEqual(self.margin.calls, [])

    def test_margin_runner_refuses_non_usdt_asset(self):
        self.margin_job("margin-asset", asset="BTC")

        self.assertEqual(self.margin_tick(), 2)

        self.assertEqual(self.margin.calls, [])
        self.assertEqual(
            self.reason(self.results()[0]), "ASSET_NOT_ALLOWED"
        )

    def test_margin_runner_clamp_refuses_before_request(self):
        self.margin_job("margin-cap", principal="3.01")

        self.assertEqual(self.margin_tick(), 2)

        self.assertEqual(jr.MARGIN_RUNNER_NOTIONAL_CAP, Decimal("3"))
        self.assertEqual(self.margin.calls, [])
        self.assertEqual(
            self.reason(self.results()[0]),
            "PRINCIPAL_CEILING_EXCEEDED",
        )

    def test_margin_runner_clamp_is_independent_of_margin_ops(self):
        self.margin_job("margin-runner-cap", principal="3.01")

        def permissive_builder(projection, _whitelist, _now):
            request_type = "BORROW" \
                if projection["op"] == "MARGIN_BORROW" else "REPAY"
            return {
                "verdict": "READY",
                "op": projection["op"],
                "client_tag": projection["client_tag"],
                "intent_id": projection["intent_id"],
                "expires_utc": projection["expires_utc"],
                "endpoint_path": jr.MARGIN_ENDPOINT_PATH,
                "params": {
                    "asset": projection["asset"],
                    "amount": projection["principal"],
                    "type": request_type,
                },
                "expected_ledger_confirmation": {
                    "asset": projection["asset"],
                    "principal": projection["principal"],
                    "interest": "0",
                    "status": request_type + "_CONFIRMED",
                },
                "evidence": [],
            }

        with mock.patch.object(
                jr.margin_ops, "build_margin_operation",
                side_effect=permissive_builder) as builder:
            self.assertEqual(self.margin_tick(), 2)

        self.assertEqual(builder.call_count, 1)
        self.assertEqual(self.margin.calls, [])
        self.assertEqual(
            self.reason(self.results()[0]),
            "PRINCIPAL_CEILING_EXCEEDED",
        )

    def test_margin_trading_fields_are_forbidden(self):
        os.unlink(os.path.join(self.receipt_dir, "ACCEPT-ONE"))
        cases = {
            "leverage": "1",
            "max_notional": "3",
            "symbol": "TSTUSDT",
            "venue": "spot",
        }
        for index, (field, value) in enumerate(sorted(cases.items())):
            with self.subTest(field=field):
                job_id = "margin-field-%s" % index
                self.margin_job(job_id, **{field: value})

                self.assertEqual(self.margin_tick(), 2)

                row = self.results()[-1]
                self.assertEqual(row["symbol"], "USDT")
                self.assertEqual(self.reason(row), "FORBIDDEN_FIELD")
                self.assertFalse(os.path.exists(os.path.join(
                    self.receipt_dir, "ACCEPT-ONE"
                )))
        self.assertEqual(self.margin.calls, [])

    def test_margin_expiry_rebuild_reaches_microsecond_post_boundary(self):
        expiry = NOW + timedelta(seconds=1, microseconds=400000)
        boundary = expiry + timedelta(microseconds=1)
        self.margin_job(
            "margin-mutation-expiry", expires_utc=utc(expiry)
        )

        with mock.patch.object(
                jr, "_utc_now", side_effect=(NOW, boundary)), \
                mock.patch.object(
                    jr.margin_ops, "build_margin_operation",
                    wraps=jr.margin_ops.build_margin_operation) as builder:
            code = self.tick()

        self.assertEqual(code, 2)
        self.assertEqual(
            [call.args[2] for call in builder.call_args_list],
            [NOW, boundary],
        )
        self.assertEqual(
            [call for call in self.margin.calls if call[0] == "POST"], []
        )
        self.assertEqual(self.reason(self.results()[0]), "EXPIRED")
        self.assertEqual(
            self.margin_source(self.results()[0])["authority_checked_utc"],
            utc(boundary),
        )
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "ACCEPT-ONE"
        )))
        self.assertEqual(len(self.governor_releases), 1)

    def test_margin_accept_one_is_rechecked_at_post_boundary(self):
        self.write_accept_one(
            expires_utc=utc(NOW + timedelta(seconds=1))
        )
        self.margin_job("margin-ticket-freshness")

        code = self.margin_tick(
            mutation_now=NOW + timedelta(seconds=2)
        )

        self.assertEqual(code, 2)
        self.assertEqual(
            [call for call in self.margin.calls if call[0] == "POST"], []
        )
        result = self.results()[0]
        self.assertEqual(self.reason(result), "ACCEPT_ONE_INVALID")
        source = self.margin_source(result)
        self.assertEqual(source["mutation_attempts"], 1)
        self.assertEqual(
            source["authority_checked_utc"],
            utc(NOW + timedelta(seconds=2)),
        )
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "ACCEPT-ONE"
        )))

    def test_margin_operator_is_rechecked_at_post_boundary(self):
        self.margin_job("margin-operator-freshness")
        persist = jr._persist_margin_source

        def change_operator_after_create(path, payload, create=False):
            persist(path, payload, create=create)
            if create:
                self.write_operator("replacement-operator")

        with mock.patch.object(
                jr, "_persist_margin_source",
                side_effect=change_operator_after_create):
            code = self.margin_tick()

        self.assertEqual(code, 2)
        self.assertEqual(
            [call for call in self.margin.calls if call[0] == "POST"], []
        )
        result = self.results()[0]
        self.assertEqual(self.reason(result), "OPERATOR_MISMATCH")
        self.assertEqual(
            self.margin_source(result)["authority_checked_utc"], utc(NOW)
        )
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "ACCEPT-ONE"
        )))

    def test_margin_borrow_rechecks_stop_jobs_at_post_boundary(self):
        self.margin_job("margin-stop-freshness")
        claim = jr._claim_margin_intent

        def seal_after_claim(*args, **kwargs):
            result = claim(*args, **kwargs)
            Path(os.path.join(self.receipt_dir, "STOP-JOBS")).touch()
            return result

        with mock.patch.object(
                jr, "_claim_margin_intent", side_effect=seal_after_claim):
            code = self.margin_tick()

        self.assertEqual(code, 2)
        self.assertEqual(
            [call for call in self.margin.calls if call[0] == "POST"], []
        )
        result = self.results()[0]
        self.assertEqual(self.reason(result), "STOP_JOBS_SEALED")
        self.assertEqual(
            self.margin_source(result)["mutation_attempts"], 1
        )
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "ACCEPT-ONE"
        )))

    def test_margin_final_authority_read_rejects_replaced_job_and_markers(self):
        job_path = self.margin_job("margin-authority-replaced")
        persist = jr._persist_margin_source

        def replace_authority_after_ready(path, payload, create=False):
            persist(path, payload, create=create)
            if not create:
                return
            with open(job_path, encoding="utf-8") as handle:
                replacement = json.load(handle)
            replacement["op"] = "MARGIN_REPAY"
            replacement["client_tag"] = \
                jr.margin_ops.deterministic_client_tag(
                    replacement["op"], replacement["asset"],
                    replacement["principal"], replacement["expires_utc"],
                )
            with open(job_path, "w", encoding="utf-8") as handle:
                json.dump(replacement, handle)
            self.write_operator("replacement-operator")
            self.write_accept_one(operator="replacement-operator")
            Path(os.path.join(self.receipt_dir, "STOP-JOBS")).touch()

        with mock.patch.object(
                jr, "_persist_margin_source",
                side_effect=replace_authority_after_ready):
            code = self.margin_tick()

        self.assertEqual(code, 2)
        self.assertEqual(
            [call for call in self.margin.calls if call[0] == "POST"], []
        )
        result = self.results()[0]
        self.assertEqual(
            self.reason(result), "MARGIN_VALIDATION_UNKNOWN"
        )
        source = self.margin_source(result)
        self.assertEqual(source["mutation_attempts"], 1)
        self.assertEqual(source["authority_checked_utc"], utc(NOW))
        self.assertTrue(source["authority_stop_jobs_present"])

    def test_margin_borrow_operator_token_must_match_owner(self):
        self.margin_job("margin-owner", operator_token="another-operator")

        self.assertEqual(self.margin_tick(), 2)

        self.assertEqual(self.margin.calls, [])
        self.assertEqual(
            self.reason(self.results()[0]), "OPERATOR_MISMATCH"
        )
        self.assertEqual(self.governor_budget_calls, [])

    def test_confirmed_margin_borrow_posts_once_and_claims_ticket(self):
        job_path = self.margin_job("margin-borrow")
        expected_authority = {
            "ready_params": jr._json_authority_digest({
                "asset": "USDT", "amount": "3", "type": "BORROW",
            }),
            "job_file": hashlib.sha256(Path(job_path).read_bytes()).hexdigest(),
            "accept_one": hashlib.sha256(Path(os.path.join(
                self.receipt_dir, "ACCEPT-ONE"
            )).read_bytes()).hexdigest(),
            "operator": hashlib.sha256(Path(os.path.join(
                self.receipt_dir, "OPERATOR"
            )).read_bytes()).hexdigest(),
            "operator_token": hashlib.sha256(b"lead").hexdigest(),
            "job_expiry": hashlib.sha256(
                utc(NOW + timedelta(minutes=10)).encode("utf-8")
            ).hexdigest(),
            "stop_jobs": hashlib.sha256(b"ABSENT").hexdigest(),
        }

        self.assertEqual(self.margin_tick(), 0)
        self.assertEqual(self.margin_tick(), 0)

        result = self.results()[0]
        self.assertEqual((result["verdict"], result["symbol"]),
                         ("OPEN", "USDT"))
        posts = [call for call in self.margin.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][3], {
            "asset": "USDT", "amount": "3", "type": "BORROW",
        })
        self.assertEqual(self.leverage.calls, [])
        source = self.margin_source(result)
        with open(result["receipt_path"], encoding="utf-8") as handle:
            runner = json.load(handle)
        self.assertEqual((source["sha"], runner["sha"]), (SHA, SHA))
        self.assertEqual(source["mutation_attempts"], 1)
        self.assertEqual(source["history_watermark"], 0)
        self.assertEqual(source["authority_checked_utc"], utc(NOW))
        self.assertFalse(source["authority_stop_jobs_present"])
        self.assertEqual(source["authority_digests"], expected_authority)
        self.assertEqual(runner["authority_digests"], expected_authority)
        self.assertTrue(all(
            re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in source["authority_digests"].values()
        ))
        self.assertEqual(
            source["authority_digests"]["operator_token"],
            hashlib.sha256(b"lead").hexdigest(),
        )
        self.assertEqual(
            source["authority_digests"]["stop_jobs"],
            hashlib.sha256(b"ABSENT").hexdigest(),
        )
        self.assertEqual(source["ready"]["params"], posts[0][3])
        self.assertEqual(
            source["ledger_confirmation"],
            source["ready"]["expected_ledger_confirmation"],
        )
        self.assertEqual(source["account_liability"], {
            "asset": "USDT", "borrowed": "3", "interest": "0",
            "total_liability": "3",
        })
        self.assertEqual(source["account_liability_before"], {
            "asset": "USDT", "borrowed": "0", "interest": "0",
            "total_liability": "0",
        })
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "ACCEPT-ONE.claimed-margin-borrow"
        )))

    def test_margin_source_and_runner_receipts_carry_full_sha(self):
        self.margin_job("margin-receipt-sha")

        self.assertEqual(self.margin_tick(), 0)

        result = self.results()[0]
        source = self.margin_source(result)
        with open(result["receipt_path"], encoding="utf-8") as handle:
            runner = json.load(handle)
        for receipt in (source, runner):
            self.assertEqual(receipt["sha"], SHA)
            self.assertRegex(receipt["sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(runner["measured_sha"], SHA)
        self.assertIs(runner["sha_valid"], True)

    def test_bad_quant_sha_receipt_keeps_exact_measurement(self):
        self.margin_job("margin-bad-sha")
        measured = "Short-Evidence"

        with mock.patch.dict(os.environ, {"QUANT_SHA": measured}):
            self.assertEqual(self.margin_tick(), 2)

        row = self.results()[0]
        self.assertEqual(self.reason(row), "BAD_QUANT_SHA")
        with open(row["receipt_path"], encoding="utf-8") as handle:
            runner = json.load(handle)
        self.assertEqual(runner["measured_sha"], measured)
        self.assertIs(runner["sha_valid"], False)
        self.assertNotIn("sha", runner)
        self.assertEqual(self.margin.calls, [])

    def test_pre_intent_refusal_leaves_intent_available_for_one_reissue(self):
        intent_id = "mrgintent-" + ("c" * 24)
        self.margin_job(
            "margin-intent-refusal-a", intent_id=intent_id,
            expires_utc=utc(NOW + timedelta(minutes=9)),
        )
        with mock.patch.dict(os.environ, {"QUANT_SHA": "not-a-full-sha"}):
            self.assertEqual(self.margin_tick(), 2)

        paths = jr._paths(self.receipt_dir)
        intent_path = jr._margin_intent_claim_path(paths, intent_id)
        self.assertFalse(os.path.exists(intent_path))
        self.margin_job(
            "margin-intent-refusal-b", intent_id=intent_id,
            expires_utc=utc(NOW + timedelta(minutes=10)),
        )

        self.assertEqual(self.margin_tick(), 0)

        self.assertEqual(
            [row["verdict"] for row in self.results()],
            ["REFUSED", "OPEN"],
        )
        self.assertEqual(
            len([call for call in self.margin.calls if call[0] == "POST"]),
            1,
        )
        with open(intent_path, encoding="utf-8") as handle:
            self.assertEqual(
                json.load(handle)["job_id"], "margin-intent-refusal-b"
            )

    def test_early_margin_refusals_publish_receipt_backed_result_rows(self):
        missing = self.margin_job("receipt-early-a")
        with open(missing, encoding="utf-8") as handle:
            missing_job = json.load(handle)
        missing_job.pop("intent_id")
        with open(missing, "w", encoding="utf-8") as handle:
            json.dump(missing_job, handle)
        self.assertEqual(self.margin_tick(), 2)

        self.margin_job("receipt-early-b", asset="BTC")
        self.assertEqual(self.margin_tick(), 2)

        self.margin_job("receipt-early-c")
        with mock.patch.dict(os.environ, {"QUANT_SHA": "short-sha"}):
            self.assertEqual(self.margin_tick(), 2)

        self.governor_acquire_refusal = "ACCOUNT_FROZEN"
        self.margin_job("receipt-early-d")
        self.assertEqual(self.margin_tick(), 2)

        rows = self.results()
        self.assertEqual(
            [self.reason(row) for row in rows],
            [
                "MISSING_FIELD", "ASSET_NOT_ALLOWED", "BAD_QUANT_SHA",
                "ACCOUNT_FROZEN",
            ],
        )
        for row in rows:
            with self.subTest(job_id=row["job_id"]):
                self.assertTrue(row["receipt_path"])
                self.assertTrue(os.path.isfile(row["receipt_path"]))
                with open(row["receipt_path"], encoding="utf-8") as handle:
                    receipt = json.load(handle)
                self.assertEqual(receipt["job_id"], row["job_id"])
                self.assertEqual(receipt["verdict"], "REFUSED")

    def test_claim_and_receipt_paths_fsync_their_parent_directories(self):
        paths = jr._paths(self.receipt_dir)
        jr._make_dirs(paths)
        claim_path = os.path.join(
            paths["claims"], "job-margin-durable.claim"
        )
        intent_id = "mrgintent-" + ("a" * 24)
        intent_path = jr._margin_intent_claim_path(paths, intent_id)
        source_path = os.path.join(
            paths["receipts"], "margin-margin-durable.json"
        )
        cleanup_path = os.path.join(
            paths["receipts"], "margin-cleanup-check.json"
        )
        runner_path = os.path.join(
            paths["receipts"], "job-margin-durable.json"
        )
        stop_path = paths["stop"]
        real_fsync = os.fsync

        def directory_syncs(action):
            observed = []

            def observe(descriptor):
                details = os.fstat(descriptor)
                if stat.S_ISDIR(details.st_mode):
                    observed.append((details.st_dev, details.st_ino))
                return real_fsync(descriptor)

            with mock.patch.object(jr.os, "fsync", side_effect=observe):
                action()
            return observed

        def assert_parent_synced(path, action, minimum=1):
            parent = os.stat(os.path.dirname(path))
            identity = (parent.st_dev, parent.st_ino)
            self.assertGreaterEqual(
                directory_syncs(action).count(identity), minimum
            )

        assert_parent_synced(
            claim_path,
            lambda: self.assertTrue(
                jr._claim(paths, "margin-durable", NOW)
            ),
        )
        assert_parent_synced(
            intent_path,
            lambda: self.assertTrue(jr._claim_margin_intent(
                paths, intent_id, "margin-durable", NOW
            )),
        )
        assert_parent_synced(
            source_path,
            lambda: jr._persist_margin_source(
                source_path, {"state": "READY"}, create=True
            ),
        )
        assert_parent_synced(
            source_path,
            lambda: jr._persist_margin_source(
                source_path, {"state": "FINISHED"}
            ),
            minimum=2,
        )

        def failed_replace_cleanup():
            with mock.patch.object(
                    jr.os, "replace", side_effect=OSError(
                        "replace unavailable"
                    )):
                with self.assertRaises(OSError):
                    jr._persist_margin_source(
                        cleanup_path, {"state": "READY"}
                    )

        assert_parent_synced(
            cleanup_path, failed_replace_cleanup, minimum=2
        )
        self.assertEqual(list(Path(paths["receipts"]).glob(
            "margin-cleanup-check.json.tmp-*"
        )), [])
        assert_parent_synced(
            runner_path,
            lambda: jr._write_runner_receipt(
                paths, "margin-durable", "FUTURES_ROUNDTRIP", "USDT",
                "REFUSED", "OFFLINE_TEST", NOW,
            ),
            minimum=2,
        )
        assert_parent_synced(
            paths["results"],
            lambda: jr._append_results_row(
                paths, {"kind": "durability_check"}
            ),
        )
        assert_parent_synced(stop_path, lambda: jr._touch_stop(stop_path))

    def test_margin_claim_replay_without_result_id_never_reposts(self):
        self.margin_job("margin-claim-replay")

        class FinishInterrupted(Exception):
            pass

        with mock.patch.object(
                jr, "_finish_claimed", side_effect=FinishInterrupted):
            with self.assertRaises(FinishInterrupted):
                self.margin_tick()

        result_ids, bad_results, unresolved = jr._result_ids(
            jr._paths(self.receipt_dir)["results"]
        )
        self.assertEqual(result_ids, set())
        self.assertFalse(bad_results)
        self.assertFalse(unresolved)
        self.assertEqual(
            len([call for call in self.margin.calls if call[0] == "POST"]),
            1,
        )

        self.assertEqual(self.margin_tick(), 3)

        self.assertEqual(
            len([call for call in self.margin.calls if call[0] == "POST"]),
            1,
        )
        self.assertEqual(self.results(), [])

    def test_same_margin_intent_refuses_across_job_tag_and_expiry_changes(self):
        intent_id = "mrgintent-" + ("b" * 24)
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "3", "interest": "0",
        }]}
        first_path = self.margin_job(
            "margin-intent-a", op="MARGIN_REPAY", intent_id=intent_id,
            expires_utc=utc(NOW + timedelta(minutes=9)),
        )
        with open(first_path, encoding="utf-8") as handle:
            first = json.load(handle)
        second_path = self.margin_job(
            "margin-intent-b", op="MARGIN_REPAY", intent_id=intent_id,
            expires_utc=utc(NOW + timedelta(minutes=10)),
        )
        with open(second_path, encoding="utf-8") as handle:
            second = json.load(handle)
        self.assertNotEqual(first["client_tag"], second["client_tag"])

        self.assertEqual(self.margin_tick(), 0)
        self.assertEqual(self.margin_tick(), 2)
        self.assertEqual(self.margin_tick(), 0)

        rows = self.results()
        self.assertEqual(
            [(row["job_id"], row["verdict"]) for row in rows],
            [
                ("margin-intent-a", "CLOSED"),
                ("margin-intent-b", "REFUSED"),
            ],
        )
        self.assertEqual(self.reason(rows[1]), "INTENT_ALREADY_CLAIMED")
        self.assertEqual(
            len([call for call in self.margin.calls if call[0] == "POST"]),
            1,
        )
        intent_path = jr._margin_intent_claim_path(
            jr._paths(self.receipt_dir), intent_id
        )
        self.assertTrue(os.path.isfile(intent_path))
        with open(intent_path, encoding="utf-8") as handle:
            intent_claim = json.load(handle)
        self.assertEqual(intent_claim["intent_id"], intent_id)
        self.assertEqual(intent_claim["job_id"], "margin-intent-a")

    def test_margin_history_disagreement_freezes_without_repost(self):
        self.margin.history_payload = {"rows": [{
            "txId": "7301",
            "asset": "USDT",
            "principal": "2",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "MANUAL",
        }]}
        self.margin_job("margin-disagree-a")
        self.margin_job("margin-disagree-b")

        self.assertEqual(self.margin_tick(), 4)
        self.assertEqual(self.margin_tick(), 3)

        self.assertEqual(self.results()[0]["verdict"], "UNKNOWN")
        self.assertEqual(
            self.reason(self.results()[0]), "MARGIN_CONFIRMATION_MISMATCH"
        )
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))
        self.assertEqual(
            len([call for call in self.margin.calls if call[0] == "POST"]),
            1,
        )

    def test_mrv11_concurrent_activity_guard_is_typed(self):
        self.assertEqual(
            jr._margin_history_fence_status(
                [7200, 7201, 7301], "7301", 7201
            ),
            "CONFIRMED",
        )
        self.assertEqual(
            jr._margin_history_fence_status(
                [7200, 7201, 7301, 7302], "7301", 7201
            ),
            "CONCURRENT_MARGIN_ACTIVITY",
        )

    def test_mrv11_target_history_row_alone_confirms(self):
        old_borrow = self.adapter_margin_row(txId=7200, amount="1",
                                              principal="1")
        old_repay = self.adapter_margin_row(txId=7201, amount="1",
                                             principal="1")
        target = self.adapter_margin_row()
        self.margin.history_before_payloads = {
            "BORROW": {"rows": [old_borrow]},
            "REPAY": {"rows": [old_repay]},
        }
        self.margin.history_after_payloads = {
            "BORROW": {"rows": [old_borrow, target]},
            "REPAY": {"rows": [old_repay]},
        }
        self.margin_job("mrv11-target-alone")

        self.assertEqual(self.margin_tick(), 0)

        result = self.results()[0]
        self.assertEqual(
            (result["verdict"], self.reason(result)),
            ("OPEN", "BORROW_CONFIRMED"),
        )
        self.assertEqual(
            self.margin_source(result)["history_watermark"], 7201
        )

    def test_mrv11_later_unrelated_repay_row_is_unknown(self):
        old_borrow = self.adapter_margin_row(txId=7200, amount="1",
                                              principal="1")
        old_repay = self.adapter_margin_row(txId=7201, amount="1",
                                             principal="1")
        target = self.adapter_margin_row(amount="2.5", principal="2.5")
        unrelated_repay = self.adapter_margin_row(
            txId=7302, amount="5", principal="5"
        )
        self.margin.history_before_payloads = {
            "BORROW": {"rows": [old_borrow]},
            "REPAY": {"rows": [old_repay]},
        }
        self.margin.history_after_payloads = {
            "BORROW": {"rows": [old_borrow]},
            "REPAY": {
                "rows": [old_repay, target, unrelated_repay],
            },
        }
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "2.5", "interest": "0",
        }]}
        self.margin.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "0", "interest": "0",
        }]}
        self.margin_job(
            "mrv11-later-repay", op="MARGIN_REPAY", principal="2.5"
        )

        self.assertEqual(self.margin_tick(), 4)

        result = self.results()[0]
        self.assertEqual(
            (result["verdict"], self.reason(result)),
            ("UNKNOWN", "CONCURRENT_MARGIN_ACTIVITY"),
        )
        self.assertEqual(
            len([call for call in self.margin.calls if call[0] == "POST"]),
            1,
        )

    def test_margin_history_confirms_real_binance_row_shape(self):
        expected = {
            "asset": "USDT",
            "principal": "2.5",
            "interest": "0",
            "status": "BORROW_CONFIRMED",
        }
        real_row = {
            "txId": 401344857108,
            "timestamp": int(NOW.timestamp() * 1000),
            "asset": "USDT",
            "amount": "2.5",
            "principal": "2.5",
            "interest": "0",
            "status": "CONFIRMED",
            "type": "MANUAL",
        }
        for origin in ("MANUAL", "AUTO"):
            read = self.read_margin_history(
                [dict(real_row, type=origin)], tx_id=401344857108
            )
            self.assertEqual(read.kind, "ok")
            self.assertEqual(read.value.origin_type, origin)

        self.margin.post_payload = {"tranId": 401344857108}
        self.margin.history_payload = {"rows": [real_row]}
        self.margin_job("margin-real-history", principal="2.5")

        self.assertEqual(self.margin_tick(), 0)
        result = self.results()[0]
        self.assertEqual((result["verdict"], self.reason(result)), (
            "OPEN", "BORROW_CONFIRMED",
        ))
        self.assertEqual(
            self.margin_source(result)["ledger_confirmation"], expected
        )
        history_calls = [
            call for call in self.margin.calls
            if call[0] == "GET" and call[2] == jr.MARGIN_ENDPOINT_PATH
        ]
        self.assertEqual(len(history_calls), 4)
        self.assertEqual([call[3] for call in history_calls], [
            {"asset": "USDT", "type": "BORROW"},
            {"asset": "USDT", "type": "REPAY"},
            {"asset": "USDT", "type": "BORROW"},
            {"asset": "USDT", "type": "REPAY"},
        ])

    def test_margin_history_retry_then_visible_confirms(self):
        target = {
            "txId": 7301,
            "asset": "USDT",
            "principal": "3",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "MANUAL",
        }
        self.margin.history_after_sequences["BORROW"] = [
            {"rows": []},
            {"rows": [target]},
        ]
        self.margin_job("margin-history-retry-visible")

        with mock.patch("builtins.print") as output:
            self.assertEqual(self.margin_tick(), 0)

        attempts = []
        for call in output.call_args_list:
            if len(call.args) != 1 or type(call.args[0]) is not str \
                    or not call.args[0].startswith("{"):
                continue
            row = json.loads(call.args[0])
            if row.get("kind") == "margin_history_confirmation_attempt":
                attempts.append(row)
        self.assertEqual(self.margin_sleep_delays, [1])
        self.assertEqual(attempts, [{
            "kind": "margin_history_confirmation_attempt",
            "job_id": "margin-history-retry-visible",
            "direction": "BORROW",
            "transaction_id": "7301",
            "attempt": 1,
            "max_attempts": 4,
            "delay_seconds": 0,
            "status": "IDENTITY_NOT_VISIBLE",
            "utc": utc(NOW),
        }, {
            "kind": "margin_history_confirmation_attempt",
            "job_id": "margin-history-retry-visible",
            "direction": "BORROW",
            "transaction_id": "7301",
            "attempt": 2,
            "max_attempts": 4,
            "delay_seconds": 1,
            "status": "IDENTITY_VISIBLE",
            "utc": utc(NOW),
        }])
        result = self.results()[0]
        self.assertEqual(
            (result["verdict"], self.reason(result)),
            ("OPEN", "BORROW_CONFIRMED"),
        )

    def test_margin_history_never_visible_stays_unknown(self):
        self.margin.history_payload = {"rows": []}
        self.margin_job("margin-history-never-visible")

        with mock.patch("builtins.print") as output:
            self.assertEqual(self.margin_tick(), 4)

        attempts = []
        for call in output.call_args_list:
            if len(call.args) != 1 or type(call.args[0]) is not str \
                    or not call.args[0].startswith("{"):
                continue
            row = json.loads(call.args[0])
            if row.get("kind") == "margin_history_confirmation_attempt":
                attempts.append(row)
        self.assertEqual(self.margin_sleep_delays, [1, 2, 4])
        self.assertEqual(
            [row["attempt"] for row in attempts], [1, 2, 3, 4]
        )
        self.assertEqual(
            [row["delay_seconds"] for row in attempts], [0, 1, 2, 4]
        )
        self.assertEqual(
            [row["status"] for row in attempts],
            ["IDENTITY_NOT_VISIBLE"] * 4,
        )
        result = self.results()[0]
        self.assertEqual(
            (result["verdict"], self.reason(result)),
            ("UNKNOWN", "MARGIN_CONFIRMATION_MISMATCH"),
        )
        self.assertEqual(
            len([call for call in self.margin.calls if call[0] == "POST"]),
            1,
        )

    def test_margin_history_rejects_target_identity_conflicts_and_duplicates(
            self):
        valid = self.adapter_margin_row()
        cases = {
            "conflict": [
                dict(valid),
                dict(valid, tranId="8301"),
            ],
            "duplicate": [dict(valid), dict(valid)],
        }
        for name, rows in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    self.read_margin_history(rows).kind, "unclear"
                )

    def test_margin_submit_requires_one_exact_integer_identity(self):
        operation = jr.binance_margin.build_borrow(
            "USDT", Decimal("3"), "intent-7301", SHA
        )
        cases = (
            ({"tranId": 7301}, "accepted"),
            ({"tranId": 7301, "txId": 7301}, "unclear"),
            ({"tranId": 7301.0}, "unclear"),
            ({"tranId": True}, "unclear"),
            ({"detail": {"tranId": 7301}}, "unclear"),
        )
        for payload, kind in cases:
            with self.subTest(payload=payload):
                transport = lambda *_args, value=payload, **_kwargs: \
                    jr.binance_http.HttpResult("ok", value, None, None)
                result = jr.binance_margin.submit_once(
                    transport, jr.binance_http.SPOT_BASE_URL, operation,
                    lambda _intent: None,
                )
                self.assertEqual(result.kind, kind)

    def test_margin_history_adapter_enforces_its_row_bound(self):
        def read(count):
            rows = [self.adapter_margin_row(txId=index + 1)
                    for index in range(count)]
            def transport(*_args, **_kwargs):
                return jr.binance_http.HttpResult(
                    "ok", {"rows": rows, "total": count}, None, None
                )
            return jr.binance_margin.read_history_by_direction(
                transport, jr.binance_http.SPOT_BASE_URL,
                "USDT", "BORROW", sleep=lambda _delay: None,
            )
        self.assertEqual(read(100).kind, "ok")
        self.assertEqual(read(101).kind, "unclear")

    def test_margin_history_requires_the_exact_type_field(self):
        without_type = self.adapter_margin_row()
        del without_type["type"]
        with_near_miss = dict(without_type, typ="MANUAL")
        for row in (without_type, with_near_miss):
            self.assertEqual(self.read_margin_history([row]).kind, "unclear")

    def test_margin_history_refuses_unknown_origin_type(self):
        row = self.adapter_margin_row(type="BORROW")
        self.assertEqual(self.read_margin_history([row]).kind, "unclear")

    def test_margin_confirmation_readers_are_total_for_non_text_fields(self):
        row = self.adapter_margin_row()
        row_cases = (
            dict(row, asset=[]),
            dict(row, principal={}),
            dict(row, status=[]),
            dict(row, type=1),
            dict(row, type=None),
        )
        for candidate in row_cases:
            with self.subTest(history_field=next(
                    name for name in row if candidate[name] != row[name]
            )):
                self.assertEqual(
                    self.read_margin_history([candidate]).kind, "unclear"
                )

        liability = {
            "asset": "USDT", "borrowed": "2.5", "interest": "0",
        }
        for field, value in (
                ("asset", []), ("borrowed", {}), ("interest", [])):
            with self.subTest(account_field=field):
                self.assertEqual(self.read_margin_liability({
                    "userAssets": [dict(liability, **{field: value})]
                }).kind, "unclear")

    def test_non_text_margin_origin_publishes_unknown_receipt(self):
        self.margin.history_payload = {"rows": [{
            "txId": "7301",
            "asset": "USDT",
            "principal": "3",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": [],
        }]}
        self.margin_job("margin-origin-shape")

        self.assertEqual(self.margin_tick(), 4)

        result = self.results()[0]
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIs(type(result["exit_code"]), int)
        self.assertEqual(
            self.reason(result), "MARGIN_CONFIRMATION_UNREADABLE"
        )
        source = self.margin_source(result)
        self.assertEqual(source["verdict"], "UNKNOWN")
        self.assertIs(type(source["reason"]), str)
        self.assertEqual(source["mutation_attempts"], 1)
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_unexpected_margin_confirmation_error_is_contained(self):
        self.margin_job("margin-confirmation-guard")
        services = replace(
            self.execution_services,
            margin_confirm=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("offline confirmation check")
            ),
        )
        self.assertEqual(self.margin_tick(services=services), 4)

        result = self.results()[0]
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertEqual(
            self.reason(result), "MARGIN_CONFIRMATION_UNREADABLE"
        )
        source = self.margin_source(result)
        self.assertEqual(source["verdict"], "UNKNOWN")
        self.assertEqual(source["mutation_attempts"], 1)
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_margin_borrow_refuses_opposite_liability_change(self):
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "5", "interest": "0.001",
        }]}
        self.margin.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "2.5", "interest": "0.002",
        }]}
        self.margin_job("margin-opposite-change", principal="2.5")

        self.assertEqual(self.margin_tick(), 4)

        result = self.results()[0]
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertEqual(
            self.reason(result), "MARGIN_LIABILITY_MISMATCH"
        )
        source = self.margin_source(result)
        self.assertEqual(source["account_liability_before"]["borrowed"], "5")
        self.assertEqual(source["account_liability"]["borrowed"], "2.5")
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_margin_borrow_confirms_manual_row_and_exact_positive_change(self):
        self.margin.post_payload = {"tranId": 401344857108}
        self.margin.history_payload = {"rows": [{
            "txId": 401344857108,
            "asset": "USDT",
            "principal": "2.5",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "MANUAL",
        }]}
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "1.25", "interest": "0.001",
        }]}
        self.margin.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "3.75", "interest": "0.002",
        }]}
        self.margin_job("margin-positive-change", principal="2.5")

        self.assertEqual(self.margin_tick(), 0)

        result = self.results()[0]
        self.assertEqual(
            (result["verdict"], self.reason(result)),
            ("OPEN", "BORROW_CONFIRMED"),
        )
        source = self.margin_source(result)
        self.assertEqual(source["account_liability_before"], {
            "asset": "USDT", "borrowed": "1.25", "interest": "0.001",
            "total_liability": "1.251",
        })
        self.assertEqual(source["account_liability"], {
            "asset": "USDT", "borrowed": "3.75", "interest": "0.002",
            "total_liability": "3.752",
        })
        account_indexes = [
            index for index, call in enumerate(self.margin.calls)
            if call[0] == "GET" and call[2] == "/sapi/v1/margin/account"
        ]
        post_index = next(
            index for index, call in enumerate(self.margin.calls)
            if call[0] == "POST"
        )
        self.assertEqual(len(account_indexes), 2)
        self.assertLess(account_indexes[0], post_index)
        self.assertGreater(account_indexes[1], post_index)

    def test_margin_history_identity_conflict_freezes_unknown(self):
        valid = {
            "txId": "7301",
            "asset": "USDT",
            "principal": "3",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "MANUAL",
        }
        self.margin.history_payload = {"rows": [
            valid,
            dict(valid, tranId="8301"),
        ]}
        self.margin_job("margin-identity-conflict")

        self.assertEqual(self.margin_tick(), 4)

        self.assertEqual(self.results()[0]["verdict"], "UNKNOWN")
        self.assertEqual(
            self.reason(self.results()[0]), "MARGIN_CONFIRMATION_UNREADABLE"
        )
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_unreadable_margin_history_freezes(self):
        self.margin.history_kind = "unclear"
        self.margin.history_payload = {"http": 502}
        self.margin_job("margin-history-read")

        self.assertEqual(self.margin_tick(), 4)

        self.assertEqual(
            self.reason(self.results()[0]),
            "MARGIN_CONFIRMATION_UNREADABLE",
        )
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_margin_repay_direct_algebra_accounts_for_interest_first(self):
        liability = jr.binance_margin.Liability
        before = liability(
            "USDT", Decimal("2.5"), Decimal("0.000005"),
            Decimal("2.500005")
        )
        operation = jr.binance_margin.build_repay(
            before, "intent-repay-algebra", SHA
        )
        history = jr.binance_margin.MarginHistoryRow(
            "REPAY", 7301, "USDT", Decimal("2.500005"),
            Decimal("2.5"), Decimal("0.000005"), "CONFIRMED",
            "MANUAL", int(NOW.timestamp() * 1000),
        )
        zero = liability("USDT", Decimal("0"), Decimal("0"), Decimal("0"))
        residual = liability(
            "USDT", Decimal("0"), Decimal("0.000001"),
            Decimal("0.000001")
        )
        self.assertEqual(jr.binance_margin.confirm(
            operation, 7301, history, before, zero
        ).kind, "confirmed")
        self.assertEqual(jr.binance_margin.confirm(
            operation, 7301, history, before, residual
        ).kind, "unknown")

    def test_mrv11_relative_dust_is_strict_for_small_principals(self):
        standing_before = {
            "asset": "USDT", "borrowed": "2.5", "interest": "0.000005",
            "total_liability": "2.500005",
        }
        standing_after = {
            "asset": "USDT", "borrowed": "0", "interest": "0.000001",
            "total_liability": "0.000001",
        }
        small_before = {
            "asset": "USDT", "borrowed": "0.00004",
            "interest": "0.00001", "total_liability": "0.00005",
        }
        small_partial = {
            "asset": "USDT", "borrowed": "0",
            "interest": "0.000049", "total_liability": "0.000049",
        }
        relative_boundary_before = {
            "asset": "USDT", "borrowed": "0.05",
            "interest": "0.00005", "total_liability": "0.05005",
        }
        relative_boundary_after = {
            "asset": "USDT", "borrowed": "0",
            "interest": "0.00005", "total_liability": "0.00005",
        }

        self.assertEqual(
            jr._margin_repay_close_verdict(
                standing_before, standing_after, "2.500005"
            ),
            "CLOSED-WITH-DUST",
        )
        self.assertEqual(
            jr._margin_repay_close_verdict(
                small_before, small_partial, "0.00005"
            ),
            "",
        )
        self.assertEqual(
            jr._margin_repay_close_verdict(
                relative_boundary_before, relative_boundary_after, "0.05"
            ),
            "",
        )

    def test_mrv11_small_principal_partial_repay_is_unknown(self):
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "0.00004",
            "interest": "0.00001",
        }]}
        self.margin.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "0",
            "interest": "0.000049",
        }]}
        self.margin_job(
            "mrv11-small-partial", op="MARGIN_REPAY",
            principal="0.00005",
        )

        self.assertEqual(self.margin_tick(), 4)

        result = self.results()[0]
        self.assertEqual(
            (result["verdict"], self.reason(result)),
            ("UNKNOWN", "MARGIN_LIABILITY_MISMATCH"),
        )

    def test_mrv11_residual_in_borrowed_is_unknown(self):
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "2.5", "interest": "0.000005",
        }]}
        self.margin.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "0.000001", "interest": "0",
        }]}
        self.margin_job(
            "mrv11-borrowed-residual", op="MARGIN_REPAY",
            principal="2.500005",
        )

        self.assertEqual(self.margin_tick(), 4)

        result = self.results()[0]
        self.assertEqual(
            (result["verdict"], self.reason(result)),
            ("UNKNOWN", "MARGIN_LIABILITY_MISMATCH"),
        )

    def test_mrv11_rejected_margin_post_stays_unknown(self):
        self.margin.post_kind = "api_error"
        self.margin.post_payload = {
            "code": -1, "msg": "request was not accepted",
        }
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "3", "interest": "0",
        }]}
        self.margin_job("mrv11-rejected", op="MARGIN_REPAY")

        self.assertEqual(self.margin_tick(), 4)

        result = self.results()[0]
        self.assertEqual(
            (result["verdict"], self.reason(result)),
            ("UNKNOWN", "MARGIN_TRANSACTION_MISSING"),
        )
        self.assertEqual(self.margin_source(result)["mutation_attempts"], 1)
        self.assertEqual(self.margin.calls[-1][0], "POST")

    def test_mrv11_above_liability_repay_refuses_before_post(self):
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "1", "interest": "0",
        }]}
        self.margin_job(
            "mrv11-above-liability", op="MARGIN_REPAY",
            principal="1.001",
        )

        self.assertEqual(self.margin_tick(), 2)

        result = self.results()[0]
        self.assertEqual(
            (result["verdict"], self.reason(result)),
            ("REFUSED", "PRINCIPAL_EXCEEDS_LIABILITY"),
        )
        self.assertEqual(self.margin_source(result)["mutation_attempts"], 0)
        self.assertEqual(
            [call for call in self.margin.calls if call[0] == "POST"], []
        )

    def test_margin_repay_split_must_equal_requested_amount_before_post(self):
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "1", "interest": "0.001",
        }]}
        self.margin_job(
            "margin-repay-split", op="MARGIN_REPAY", principal="1",
        )

        self.assertEqual(self.margin_tick(), 2)

        result = self.results()[0]
        self.assertEqual((result["verdict"], self.reason(result)), (
            "REFUSED", "REPAY_PRINCIPAL_INTEREST_MISMATCH",
        ))
        self.assertEqual(
            [call for call in self.margin.calls if call[0] == "POST"], []
        )

    def test_margin_repay_measured_total_closes_cleanly(self):
        facts = json.loads(json.dumps(STANDING_REPAY_FACTS))
        self.margin.post_payload = {
            "txId": facts["history_row"]["txId"],
        }
        self.margin.history_payload = {"rows": [facts["history_row"]]}
        before = facts["account_liability_before"]
        self.margin.account_before_payload = {"userAssets": [{
            "asset": before["asset"], "borrowed": before["borrowed"],
            "interest": before["interest"],
        }]}
        self.margin.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "0", "interest": "0",
        }]}
        self.margin_job(
            "margin-repay-measured-total", op="MARGIN_REPAY",
            principal=facts["ready_params"]["amount"],
        )

        self.assertEqual(self.margin_tick(), 0)

        result = self.results()[0]
        self.assertEqual(
            (result["verdict"], self.reason(result)),
            ("CLOSED", "REPAY_CONFIRMED"),
        )
        source = self.margin_source(result)
        modern_expected = {
            "asset": "USDT",
            "amount": "2.5000387",
            "principal": "2.5",
            "interest": "0.0000387",
            "status": "REPAY_CONFIRMED",
        }
        self.assertEqual(
            source["ready"]["expected_ledger_confirmation"],
            modern_expected,
        )
        self.assertEqual(source["ledger_confirmation"], modern_expected)
        self.assertEqual(
            source["snapshot_time_note"],
            jr.MARGIN_SNAPSHOT_TIME_UNVERIFIED,
        )
        self.assertNotIn("follow_up_repay", source)

    def test_margin_repay_fresh_liability_can_exceed_borrow_cap(self):
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "3", "interest": "0.000005",
        }]}
        self.margin_job(
            "margin-repay-over-borrow-cap", op="MARGIN_REPAY",
            principal="3.000005",
        )

        self.assertEqual(self.margin_tick(), 0)

        result = self.results()[0]
        self.assertEqual((result["verdict"], self.reason(result)), (
            "CLOSED", "REPAY_CONFIRMED",
        ))
        self.assertEqual([
            call[3]["amount"] for call in self.margin.calls
            if call[0] == "POST"
        ], ["3.000005"])

    def test_margin_repay_principal_only_history_row_is_mismatch(self):
        principal_only = dict(
            STANDING_REPAY_FACTS["history_row"],
            principal="2.5000387", interest="0",
        )
        before = jr.binance_margin.Liability(
            "USDT", Decimal("2.5"), Decimal("0.0000387"),
            Decimal("2.5000387")
        )
        operation = jr.binance_margin.build_repay(
            before, "principal-only", SHA
        )
        read = self.read_margin_history(
            [principal_only], "REPAY", 401382497303
        )
        zero = jr.binance_margin.Liability(
            "USDT", Decimal("0"), Decimal("0"), Decimal("0")
        )
        self.assertEqual(read.kind, "ok")
        self.assertEqual(jr.binance_margin.confirm(
            operation, 401382497303, read.value, before, zero
        ).kind, "unknown")

    def test_margin_repay_residual_interest_has_typed_dust_verdict(self):
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "2.5", "interest": "0.000005",
        }]}
        self.margin.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "0", "interest": "0.000006",
        }]}
        self.margin_job(
            "margin-repay-dust", op="MARGIN_REPAY",
            principal="2.500005",
        )

        self.assertEqual(self.margin_tick(), 0)

        result = self.results()[0]
        self.assertEqual(
            (result["verdict"], self.reason(result)),
            ("CLOSED-WITH-DUST", "REPAY_CONFIRMED_WITH_DUST"),
        )
        source = self.margin_source(result)
        self.assertEqual(source["ledger_confirmation"], {
            "asset": "USDT", "amount": "2.500005",
            "principal": "2.5", "interest": "0.000005",
            "status": "REPAY_CONFIRMED",
        })
        self.assertEqual(source["account_liability"], {
            "asset": "USDT", "borrowed": "0", "interest": "0.000006",
            "total_liability": "0.000006",
        })
        self.assertEqual(source["follow_up_repay"], {
            "kind": "INTEREST_ONLY_REPAY",
            "asset": "USDT",
            "principal": "0.000006",
        })
        self.assertFalse(os.path.exists(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_margin_history_snapshots_bracket_account_delta(self):
        self.margin.account_before_payload = {
            "updateTime": "1000",
            "userAssets": [{
                "asset": "USDT", "borrowed": "0", "interest": "0",
            }],
        }
        self.margin.account_payload = {
            "timestamp": 1001,
            "userAssets": [{
                "asset": "USDT", "borrowed": "3", "interest": "0",
            }],
        }
        self.margin_job("margin-snapshot-order")

        self.assertEqual(self.margin_tick(), 0)

        calls = self.margin.calls
        history_indexes = [
            index for index, call in enumerate(calls)
            if call[0] == "GET" and call[2] == jr.MARGIN_ENDPOINT_PATH
        ]
        account_indexes = [
            index for index, call in enumerate(calls)
            if call[0] == "GET" and call[2] == "/sapi/v1/margin/account"
        ]
        post_index = next(
            index for index, call in enumerate(calls)
            if call[0] == "POST"
        )
        self.assertEqual(len(history_indexes), 4)
        self.assertLess(history_indexes[1], account_indexes[0])
        self.assertLess(account_indexes[0], post_index)
        self.assertLess(post_index, account_indexes[1])
        self.assertLess(account_indexes[1], history_indexes[2])
        self.assertEqual(
            self.margin_source(self.results()[0])["snapshot_time_note"],
            jr.MARGIN_SNAPSHOT_TIME_UNVERIFIED,
        )

    def test_margin_snapshot_time_is_explicitly_unverified(self):
        self.margin.account_before_payload = {
            "timestamp": 1000,
            "userAssets": [{
                "asset": "USDT", "borrowed": "0", "interest": "0",
            }],
        }
        self.margin.account_payload = {
            "updateTime": "1000",
            "userAssets": [{
                "asset": "USDT", "borrowed": "3", "interest": "0",
            }],
        }
        self.margin_job("margin-snapshot-time")

        self.assertEqual(self.margin_tick(), 0)

        result = self.results()[0]
        self.assertEqual(result["verdict"], "OPEN")
        self.assertEqual(
            self.margin_source(result)["snapshot_time_note"],
            jr.MARGIN_SNAPSHOT_TIME_UNVERIFIED,
        )

    def test_margin_repay_closes_only_after_explicit_zero_liability(self):
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "3", "interest": "0",
        }]}
        self.margin_job("margin-repay-zero", op="MARGIN_REPAY")

        self.assertEqual(self.margin_tick(), 0)

        zero_result = self.results()[0]
        self.assertEqual(zero_result["verdict"], "CLOSED")
        self.assertEqual(
            self.margin_source(zero_result)["account_liability"][
                "total_liability"
            ],
            "0",
        )
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "ACCEPT-ONE"
        )))
        self.assertEqual(self.governor_budget_calls, [])

        nonzero = MarginTransport()
        nonzero.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "0.02", "interest": "0",
        }]}
        nonzero.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "0.01", "interest": "0",
        }]}
        self.margin_job("margin-repay-open", op="MARGIN_REPAY",
                        principal="0.02")

        self.assertEqual(self.margin_tick(request=nonzero), 4)

        open_result = self.results()[-1]
        self.assertEqual(open_result["verdict"], "UNKNOWN")
        self.assertEqual(
            self.reason(open_result), "MARGIN_LIABILITY_MISMATCH"
        )

    def test_stop_jobs_seals_borrow_and_allows_repay_without_ticket_claim(self):
        Path(os.path.join(self.receipt_dir, "STOP-JOBS")).touch()
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "3", "interest": "0",
        }]}
        self.margin_job("margin-seal-borrow", op="MARGIN_BORROW")
        self.margin_job("margin-seal-repay", op="MARGIN_REPAY")

        self.assertEqual(self.margin_tick(), 2)
        self.assertEqual(self.margin_tick(), 0)

        self.assertEqual(
            [self.reason(row) for row in self.results()],
            ["STOP_JOBS_SEALED", "REPAY_CONFIRMED_UNDER_STOP_JOBS"],
        )
        self.assertEqual(
            [row["verdict"] for row in self.results()],
            ["REFUSED", "CLOSED"],
        )
        self.assertEqual(
            self.margin_source(self.results()[1])["execution_mode"],
            "STOP_JOBS_REPAY",
        )
        self.assertEqual(
            len([call for call in self.margin.calls if call[0] == "POST"]),
            1,
        )
        self.assertEqual(self.governor_budget_calls, [])
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "ACCEPT-ONE"
        )))
        self.assertFalse(os.path.exists(os.path.join(
            self.receipt_dir, "ACCEPT-ONE.claimed-margin-seal-borrow"
        )))
        self.assertFalse(os.path.exists(os.path.join(
            self.receipt_dir, "ACCEPT-ONE.claimed-margin-seal-repay"
        )))
        for job_id in ("margin-seal-borrow", "margin-seal-repay"):
            self.assertTrue(os.path.isfile(os.path.join(
                self.jobs_dir, "claims", "job-%s.claim" % job_id
            )))

    def test_margin_claim_collision_keeps_governor_slot_on_final_unknown(self):
        job_id = "margin-governor-collision"
        Path(os.path.join(
            self.receipt_dir, "ACCEPT-ONE.claimed-" + job_id
        )).touch()
        self.margin_job(job_id)

        self.assertEqual(self.margin_tick(), 4)

        self.assertEqual(self.results()[0]["verdict"], "UNKNOWN")
        self.assertEqual(
            self.reason(self.results()[0]), "ACCEPT_ONE_CLAIM_FAILED"
        )
        self.assertEqual(len(self.governor_acquire_calls), 1)
        self.assertEqual(self.governor_releases, [])
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "ACCEPT-ONE"
        )))
        self.assertEqual(
            len([call for call in self.margin.calls if call[0] == "POST"]),
            1,
        )

    def test_margin_release_boundary_is_same_tick_only(self):
        same_tick_id = "margin-same-tick-release"
        self.margin_job(same_tick_id)

        with mock.patch.object(
                jr, "_release_final_outcome_governor",
                wraps=jr._release_final_outcome_governor) as final_release:
            self.assertEqual(self.margin_tick(), 0)

        final_release.assert_called_once()
        self.assertEqual(self.governor_releases, [(
            "governor-token-1", self.receipt_dir,
        )])
        same_tick_row = self.results()[0]
        with open(same_tick_row["receipt_path"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["governor_release"], "RELEASED")
        self.assertEqual(
            self.margin_source(same_tick_row)["governor_release"], "RELEASED"
        )

        self.write_accept_one()
        standing_id = "margin-prior-tick-lock"
        _row, lock_path = self.interrupt_margin_governor_release(standing_id)
        releases_before_later_tick = list(self.governor_releases)

        self.assertEqual(self.tick(), 3)

        self.assertEqual(self.governor_releases, releases_before_later_tick)
        self.assertTrue(os.path.isfile(lock_path))
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_reconcile_margin_legacy_receipt_refuses_unproved_window(self):
        job_id = "mproof-b668c154de2c"
        artifacts = self.prepare_unknown_margin_reconcile(job_id)
        self.make_margin_reconcile_receipt_legacy(artifacts)
        originals = {name: Path(artifacts[name]).read_bytes()
                     for name in ("source_path", "runner_path", "lock_path")}
        self.margin.history_payload = {"rows": [self.adapter_margin_row(
            txId=401344857108, amount="2.5", principal="2.5"
        )]}

        self.assertEqual(self.reconcile_margin(job_id), 3)

        self.assertEqual(
            self.results()[-1]["reason"], "LEGACY_WINDOW_NOT_EXCLUSIVE"
        )
        for name, encoded in originals.items():
            self.assertEqual(Path(artifacts[name]).read_bytes(), encoded)
        self.assertEqual(self.governor_releases, [])

    def test_reconcile_margin_legacy_liability_cannot_bypass_window(self):
        job_id = "margin-reconcile-legacy-liability"
        artifacts = self.prepare_unknown_margin_reconcile(job_id)
        self.make_margin_reconcile_receipt_legacy(artifacts)
        self.margin.history_payload = {"rows": [{
            "txId": 401344857108,
            "asset": "USDT",
            "principal": "2.5",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "MANUAL",
        }]}
        self.margin.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "2.4", "interest": "0.00003",
        }]}
        originals = {
            name: Path(artifacts[name]).read_bytes()
            for name in ("source_path", "runner_path", "lock_path")
        }

        self.assertEqual(self.reconcile_margin(job_id), 3)

        self.assertEqual(
            self.results()[-1]["reason"], "LEGACY_WINDOW_NOT_EXCLUSIVE"
        )
        for name, encoded in originals.items():
            self.assertEqual(Path(artifacts[name]).read_bytes(), encoded)
        self.assertEqual(self.governor_releases, [])

    def test_reconcile_margin_legacy_second_transaction_refuses(self):
        job_id = "margin-reconcile-legacy-second"
        artifacts = self.prepare_unknown_margin_reconcile(job_id)
        self.make_margin_reconcile_receipt_legacy(artifacts)
        target = {
            "txId": 401344857108,
            "asset": "USDT",
            "principal": "2.5",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "MANUAL",
        }
        unrelated = dict(target, txId=401344857109, principal="1")
        self.margin.history_payload = {"rows": [target, unrelated]}
        originals = {
            name: Path(artifacts[name]).read_bytes()
            for name in ("source_path", "runner_path", "lock_path")
        }

        self.assertEqual(self.reconcile_margin(job_id), 3)

        self.assertEqual(
            self.results()[-1]["reason"], "LEGACY_WINDOW_NOT_EXCLUSIVE"
        )
        for name, encoded in originals.items():
            self.assertEqual(Path(artifacts[name]).read_bytes(), encoded)
        self.assertEqual(self.governor_releases, [])

    def test_reconcile_margin_legacy_bounded_read_refuses(self):
        job_id = "margin-reconcile-legacy-page-cap"
        artifacts = self.prepare_unknown_margin_reconcile(job_id)
        self.make_margin_reconcile_receipt_legacy(artifacts)
        self.margin.history_payload = {"rows": [
            self.adapter_margin_row(txId=401344857108 + index)
            for index in range(101)
        ]}

        self.assertEqual(self.reconcile_margin(job_id), 3)

        self.assertEqual(
            self.results()[-1]["reason"], "MARGIN_CONFIRMATION_UNREADABLE"
        )
        self.assertTrue(os.path.isfile(artifacts["lock_path"]))
        self.assertEqual(self.governor_releases, [])

    def test_reconcile_margin_modern_receipt_skips_legacy_branch(self):
        job_id = "margin-reconcile-modern"
        artifacts = self.prepare_unknown_margin_reconcile(job_id)
        self.margin.history_payload = {"rows": [{
            "txId": 401344857108,
            "asset": "USDT",
            "principal": "2.5",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "MANUAL",
        }]}

        self.assertEqual(self.reconcile_margin(job_id), 0)
        with open(artifacts["source_path"], encoding="utf-8") as handle:
            source = json.load(handle)
        self.assertEqual((source["verdict"], source["reason"]), (
            "OPEN", "BORROW_CONFIRMED",
        ))
        self.assertEqual(source["history_watermark"], 0)
        self.assertNotIn("legacy_exclusivity_proof", source)
        event = self.result_events("RECONCILED_FROM_EXCHANGE_TRUTH")[0]
        self.assertEqual(event["history_watermark"], 0)
        self.assertNotIn("legacy_exclusivity_proof", event)

    def test_reconcile_margin_standing_repay_closes_and_appends_result(self):
        facts, unknown, source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        job_id = facts["job_id"]
        transaction_id = facts["submission"]["transaction_id"]
        liability_before = facts["account_liability_before"]
        liability_after = {
            "asset": "USDT",
            "borrowed": "0",
            "interest": "0",
            "total_liability": "0",
        }
        self.assertEqual((unknown["verdict"], self.reason(unknown)), (
            "UNKNOWN", "MARGIN_CONFIRMATION_MISMATCH",
        ))
        source_before = json.loads(
            Path(source_path).read_text(encoding="utf-8")
        )
        runner_before = json.loads(
            Path(unknown["receipt_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(
            jr.LEGACY_REPAY_RECEIPTS, ((
                facts["job_id"], facts["submission"]["transaction_id"],
                facts["ready_params"]["asset"],
                facts["ready_params"]["amount"], facts["release_sha"],
            ),)
        )
        self.assertEqual(source_before["sha"], facts["release_sha"])
        self.assertEqual((
            runner_before["sha"], runner_before["sha_valid"],
            runner_before["measured_sha"],
        ), (
            facts["release_sha"], True, facts["release_sha"],
        ))
        self.assertNotEqual(set(source_before), set(jr.LEGACY_RECEIPT_FIELDS))
        self.assertEqual(source_before["ready"]["params"],
                         facts["ready_params"])
        self.assertEqual(
            source_before["ready"]["expected_ledger_confirmation"],
            facts["ready_expected"],
        )
        self.assertEqual(source_before["submission"], facts["submission"])
        self.assertEqual(
            source_before["account_liability_before"], liability_before
        )

        self.margin.history_payload = {"rows": [facts["history_row"]]}
        with mock.patch.object(
                jr, "_evaluate_legacy_repay_confirmation",
                wraps=jr._evaluate_legacy_repay_confirmation,
                ) as evaluator:
            self.assertEqual(self.reconcile_margin(job_id), 0)

        evaluator.assert_called_once()
        self.assertEqual(evaluator.call_args.args[5], liability_before)
        rows = self.results()
        self.assertEqual(
            [row.get("kind", "RESULT") for row in rows],
            ["RESULT", "RECONCILED_FROM_EXCHANGE_TRUTH", "RESULT"],
        )
        event = rows[1]
        final = rows[2]
        self.assertEqual((event["verdict"], event["reason"]), (
            "CLOSED", "REPAY_CONFIRMED",
        ))
        self.assertEqual(event["transaction_id"], transaction_id)
        self.assertEqual(event["account_liability_before"], liability_before)
        self.assertEqual(event["account_liability"], liability_after)
        self.assertEqual(event["receipt_shape"], jr.LEGACY_REPAY_SHAPE)
        self.assertEqual(
            event["receipt_shape_version"], jr.LEGACY_REPAY_SHAPE_VERSION
        )
        self.assertEqual(event["ledger_confirmation"], {
            "asset": "USDT", "amount": "2.5000387",
            "principal": "2.5", "interest": "0.0000387",
            "status": "REPAY_CONFIRMED",
        })
        self.assertEqual(final, {
            "job_id": job_id,
            "op": "MARGIN_REPAY",
            "symbol": "USDT",
            "verdict": "CLOSED",
            "exit_code": 0,
            "receipt_path": unknown["receipt_path"],
            "utc": utc(NOW),
        })
        source_final = self.margin_source(final)
        self.assertEqual((source_final["verdict"], source_final["reason"]), (
            "CLOSED", "REPAY_CONFIRMED",
        ))
        self.assertEqual(
            source_final["ready"]["expected_ledger_confirmation"],
            facts["ready_expected"],
        )
        self.assertEqual(source_final["account_liability"], liability_after)
        self.assertEqual(
            jr._result_ids(jr._paths(self.receipt_dir)["results"]),
            ({job_id}, False, False),
        )
        self.assertEqual(self.governor_releases, [])

    def test_reconcile_margin_four_field_repay_with_other_writer_is_unconfirmed(
            self):
        facts, unknown, source_path = \
            self.prepare_standing_legacy_repay_reconcile(
                bind_legacy_release=False
            )
        self.margin.history_payload = {"rows": [facts["history_row"]]}
        source_before = Path(source_path).read_bytes()
        runner_before = Path(unknown["receipt_path"]).read_bytes()
        calls_before = list(self.margin.calls)

        with mock.patch.object(
                jr, "_evaluate_legacy_repay_confirmation",
                wraps=jr._evaluate_legacy_repay_confirmation,
                ) as legacy_evaluator, mock.patch("builtins.print") as output:
            self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        legacy_evaluator.assert_not_called()
        output.assert_any_call(
            "JOB-RUNNER | RECONCILE UNCONFIRMED | "
            "LEGACY_PROVENANCE_MISMATCH | WRITER_RELEASE_SHA"
        )
        self.assertEqual(self.margin.calls, calls_before)
        self.assertEqual(self.results()[-1], {
            "kind": "RECONCILE_UNCONFIRMED",
            "job_id": facts["job_id"],
            "reason": "LEGACY_PROVENANCE_MISMATCH",
            "utc": utc(NOW),
        })
        self.assertEqual(Path(source_path).read_bytes(), source_before)
        self.assertEqual(
            Path(unknown["receipt_path"]).read_bytes(), runner_before
        )

    def test_reconcile_margin_nonallowlisted_edited_receipt_is_unconfirmed(
            self):
        facts, unknown, source_path = \
            self.prepare_standing_legacy_repay_reconcile(
                job_id="mrv13-edited-repay"
            )
        runner = json.loads(Path(unknown["receipt_path"]).read_text(
            encoding="utf-8"
        ))
        source = json.loads(Path(source_path).read_text(encoding="utf-8"))
        self.assertEqual(source["sha"], facts["release_sha"])
        self.assertEqual(
            source["ready"]["expected_ledger_confirmation"],
            facts["ready_expected"],
        )
        self.assertEqual((
            runner["measured_sha"], runner["sha_valid"], runner["sha"],
        ), (
            facts["release_sha"], True, facts["release_sha"],
        ))
        calls_before = list(self.margin.calls)

        with mock.patch.object(
                jr, "_evaluate_legacy_repay_confirmation",
                wraps=jr._evaluate_legacy_repay_confirmation,
                ) as legacy_evaluator, mock.patch("builtins.print") as output:
            self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        legacy_evaluator.assert_not_called()
        output.assert_any_call(
            "JOB-RUNNER | RECONCILE UNCONFIRMED | "
            "LEGACY_PROVENANCE_MISMATCH | JOB_ID"
        )
        self.assertEqual(self.margin.calls, calls_before)
        self.assertEqual(
            self.results()[-1]["reason"], "LEGACY_PROVENANCE_MISMATCH"
        )

    def test_reconcile_margin_legacy_repay_ignores_runner_sha_metadata(self):
        facts, unknown, _source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        runner = json.loads(Path(unknown["receipt_path"]).read_text(
            encoding="utf-8"
        ))
        runner["sha"] = SHA
        runner["measured_sha"] = SHA
        runner["sha_valid"] = False
        Path(unknown["receipt_path"]).write_text(
            json.dumps(runner, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        self.margin.history_payload = {"rows": [facts["history_row"]]}

        with mock.patch.object(
                jr, "_evaluate_legacy_repay_confirmation",
                wraps=jr._evaluate_legacy_repay_confirmation,
                ) as legacy_evaluator:
            self.assertEqual(self.reconcile_margin(facts["job_id"]), 0)

        legacy_evaluator.assert_called_once()
        self.assertEqual(
            (self.results()[-1]["verdict"], self.results()[-1]["exit_code"]),
            ("CLOSED", 0),
        )

    def test_reconcile_margin_allowlisted_job_with_other_tx_is_unconfirmed(
            self):
        facts, _unknown, source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        source = json.loads(Path(source_path).read_text(encoding="utf-8"))
        source["submission"]["transaction_id"] = "401382497304"
        Path(source_path).write_text(
            json.dumps(source, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        calls_before = list(self.margin.calls)

        with mock.patch("builtins.print") as output:
            self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        output.assert_any_call(
            "JOB-RUNNER | RECONCILE UNCONFIRMED | "
            "LEGACY_PROVENANCE_MISMATCH | SUBMISSION_TRANSACTION_ID"
        )
        self.assertEqual(self.margin.calls, calls_before)
        self.assertEqual(
            self.results()[-1]["reason"], "LEGACY_PROVENANCE_MISMATCH"
        )

    def test_reconcile_margin_allowlisted_job_with_other_amount_is_unconfirmed(
            self):
        facts, _unknown, source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        source = json.loads(Path(source_path).read_text(encoding="utf-8"))
        source["ready"]["params"]["amount"] = "2.5000388"
        source["ready"]["expected_ledger_confirmation"]["principal"] = \
            "2.5000388"
        Path(source_path).write_text(
            json.dumps(source, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        calls_before = list(self.margin.calls)

        with mock.patch("builtins.print") as output:
            self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        output.assert_any_call(
            "JOB-RUNNER | RECONCILE UNCONFIRMED | "
            "LEGACY_PROVENANCE_MISMATCH | REQUESTED_AMOUNT"
        )
        self.assertEqual(self.margin.calls, calls_before)
        self.assertEqual(
            self.results()[-1]["reason"], "LEGACY_PROVENANCE_MISMATCH"
        )

    def test_reconcile_margin_allowlisted_job_without_ledger_row_is_unconfirmed(
            self):
        facts, _unknown, _source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        results_path = jr._paths(self.receipt_dir)["results"]
        other_row = self.results()[0]
        other_row["job_id"] = "other-ledger-job"
        Path(results_path).write_text(
            json.dumps(other_row, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        calls_before = list(self.margin.calls)

        with mock.patch("builtins.print") as output:
            self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        output.assert_any_call(
            "JOB-RUNNER | RECONCILE UNCONFIRMED | "
            "LEGACY_PROVENANCE_MISMATCH | RESULT_LEDGER_JOB_ID"
        )
        self.assertEqual(self.margin.calls, calls_before)
        self.assertEqual(
            self.results()[-1]["reason"], "LEGACY_PROVENANCE_MISMATCH"
        )

    def test_legacy_repay_result_ledger_binds_operation_and_basename(self):
        facts, _unknown, _source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        results_path = jr._paths(self.receipt_dir)["results"]
        row = self.results()[0]
        self.assertEqual(jr._legacy_repay_result_ledger_fact(
            results_path, facts["job_id"]
        ), "")
        for changed, fact in (
                ({"op": "MARGIN_BORROW"}, "RESULT_LEDGER_OPERATION"),
                ({"receipt_path": os.path.join(
                    os.path.dirname(row["receipt_path"]), "job-other.json",
                )}, "RESULT_LEDGER_RECEIPT_PATH")):
            with self.subTest(fact=fact):
                candidate = dict(row)
                candidate.update(changed)
                Path(results_path).write_text(
                    json.dumps(candidate, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                self.assertEqual(jr._legacy_repay_result_ledger_fact(
                    results_path, facts["job_id"]
                ), fact)

    def test_reconcile_margin_legacy_repay_tx_id_mismatch_is_unconfirmed(
            self):
        facts, _unknown, _source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        self.margin.history_payload = {"rows": [dict(
            facts["history_row"],
            txId=facts["history_row"]["txId"] + 1,
        )]}

        with mock.patch("builtins.print") as output:
            self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        self.assertEqual(
            self.results()[-1]["reason"], "MARGIN_CONFIRMATION_MISMATCH"
        )
        output.assert_any_call(
            "JOB-RUNNER | RECONCILE UNCONFIRMED | "
            "MARGIN_CONFIRMATION_MISMATCH"
        )

    def test_reconcile_margin_adapter_rejects_exchange_asset_mismatch(self):
        facts, _unknown, _source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        self.margin.history_payload = {"rows": [dict(
            facts["history_row"], asset="BUSD"
        )]}

        with mock.patch("builtins.print") as output:
            self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        self.assertEqual(
            self.results()[-1]["reason"], "MARGIN_CONFIRMATION_UNREADABLE"
        )
        output.assert_any_call(
            "JOB-RUNNER | RECONCILE UNCONFIRMED | "
            "MARGIN_CONFIRMATION_UNREADABLE"
        )

    def test_reconcile_margin_legacy_exchange_amount_is_provenance_bound(self):
        facts, _unknown, _source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        self.margin.history_payload = {"rows": [dict(
            facts["history_row"], amount="2.5000388"
        )]}

        with mock.patch("builtins.print") as output:
            self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        self.assertEqual(
            self.results()[-1]["reason"], "LEGACY_PROVENANCE_MISMATCH"
        )
        output.assert_any_call(
            "JOB-RUNNER | RECONCILE UNCONFIRMED | "
            "LEGACY_PROVENANCE_MISMATCH | EXCHANGE_AMOUNT"
        )

    def test_reconcile_margin_legacy_exchange_split_is_provenance_bound(self):
        facts, _unknown, _source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        self.margin.history_payload = {"rows": [dict(
            facts["history_row"], principal="2.4"
        )]}

        with mock.patch("builtins.print") as output:
            self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        self.assertEqual(
            self.results()[-1]["reason"], "LEGACY_PROVENANCE_MISMATCH"
        )
        output.assert_any_call(
            "JOB-RUNNER | RECONCILE UNCONFIRMED | "
            "LEGACY_PROVENANCE_MISMATCH | EXCHANGE_PRINCIPAL_INTEREST_TOTAL"
        )

    def test_reconcile_margin_legacy_repay_status_mismatch_is_unconfirmed(
            self):
        facts, _unknown, _source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        self.margin.history_payload = {"rows": [dict(
            facts["history_row"], status="PENDING"
        )]}

        self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        self.assertEqual(
            self.results()[-1]["reason"], "LEGACY_REPAY_STATUS_MISMATCH"
        )

    def test_reconcile_margin_legacy_repay_principal_mismatch_is_unconfirmed(
            self):
        facts, _unknown, source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        source = json.loads(Path(source_path).read_text(encoding="utf-8"))
        source["account_liability_before"] = {
            "asset": "USDT",
            "borrowed": "2.5",
            "interest": "0.1000387",
            "total_liability": "2.6000387",
        }
        Path(source_path).write_text(
            json.dumps(source, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        self.margin.history_payload = {"rows": [dict(
            facts["history_row"],
            principal="2.4", interest="0.1000387",
        )]}

        self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        self.assertEqual(
            self.results()[-1]["reason"], "LEGACY_REPAY_PRINCIPAL_MISMATCH"
        )

    def test_reconcile_margin_legacy_repay_current_interest_is_unconfirmed(
            self):
        facts, _unknown, _source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        self.margin.history_payload = {"rows": [facts["history_row"]]}
        self.margin.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "0", "interest": "0.000001",
        }]}

        self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        self.assertEqual(
            self.results()[-1]["reason"],
            "LEGACY_REPAY_CURRENT_INTEREST_NOT_ZERO",
        )

    def test_reconcile_margin_standing_repay_nonzero_account_is_unconfirmed(
            self):
        facts, unknown, source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        self.margin.history_payload = {"rows": [facts["history_row"]]}
        self.margin.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "0.1", "interest": "0",
        }]}
        source_before = Path(source_path).read_bytes()
        runner_before = Path(unknown["receipt_path"]).read_bytes()

        self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        self.assertEqual(self.results()[-1], {
            "kind": "RECONCILE_UNCONFIRMED",
            "job_id": facts["job_id"],
            "reason": "LEGACY_REPAY_CURRENT_BORROWED_NOT_ZERO",
            "utc": utc(NOW),
        })
        self.assertEqual(Path(source_path).read_bytes(), source_before)
        self.assertEqual(
            Path(unknown["receipt_path"]).read_bytes(), runner_before
        )

    def test_reconcile_margin_adapter_rejects_missing_exchange_interest(
            self):
        facts, _unknown, _source_path = \
            self.prepare_standing_legacy_repay_reconcile()
        row = dict(facts["history_row"])
        row.pop("interest")
        self.margin.history_payload = {"rows": [row]}

        with mock.patch("builtins.print") as output:
            self.assertEqual(self.reconcile_margin(facts["job_id"]), 3)

        self.assertEqual(
            self.results()[-1]["reason"], "MARGIN_CONFIRMATION_UNREADABLE"
        )
        output.assert_any_call(
            "JOB-RUNNER | RECONCILE UNCONFIRMED | "
            "MARGIN_CONFIRMATION_UNREADABLE"
        )

    def test_reconcile_margin_modern_missing_field_stays_modern(self):
        job_id = "margin-reconcile-modern-missing"
        artifacts = self.prepare_unknown_margin_reconcile(job_id)
        source_path = artifacts["source_path"]
        source = json.loads(Path(source_path).read_text(encoding="utf-8"))
        source.pop("history_watermark")
        self.assertNotEqual(
            set(source), set(jr.LEGACY_RECEIPT_FIELDS)
        )
        Path(source_path).write_text(
            json.dumps(source, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        calls_before = list(self.margin.calls)

        self.assertEqual(self.reconcile_margin(job_id), 3)
        self.assertEqual(self.margin.calls, calls_before)
        self.assertEqual(
            self.results()[-1]["reason"], "HISTORY_WATERMARK_MISSING"
        )

    def test_reconcile_margin_without_transaction_id_refuses_before_reads(self):
        job_id = "margin-reconcile-no-transaction"
        artifacts = self.prepare_unknown_margin_reconcile(job_id)
        with open(artifacts["source_path"], encoding="utf-8") as handle:
            source = json.load(handle)
        source["submission"]["transaction_id"] = ""
        Path(artifacts["source_path"]).write_text(
            json.dumps(source), encoding="utf-8"
        )
        calls_before = list(self.margin.calls)

        self.assertEqual(self.reconcile_margin(job_id), 3)

        self.assertEqual(self.margin.calls, calls_before)
        self.assertEqual(self.results()[-1], {
            "kind": "RECONCILE_UNCONFIRMED",
            "job_id": job_id,
            "reason": "MARGIN_TRANSACTION_MISSING",
            "utc": utc(NOW),
        })
        self.assertTrue(os.path.isfile(artifacts["lock_path"]))
        self.assertEqual(self.governor_releases, [])

    def test_reconcile_margin_non_unknown_source_refuses_before_reads(self):
        job_id = "margin-reconcile-final"
        artifacts = self.prepare_unknown_margin_reconcile(job_id)
        with open(artifacts["source_path"], encoding="utf-8") as handle:
            source = json.load(handle)
        source["verdict"] = "OPEN"
        Path(artifacts["source_path"]).write_text(
            json.dumps(source), encoding="utf-8"
        )
        calls_before = list(self.margin.calls)

        self.assertEqual(self.reconcile_margin(job_id), 3)

        self.assertEqual(self.margin.calls, calls_before)
        self.assertEqual(
            self.results()[-1]["reason"], "SOURCE_RECEIPT_NOT_UNKNOWN"
        )
        self.assertTrue(os.path.isfile(artifacts["lock_path"]))

    def test_reconcile_margin_unconfirmed_reads_preserve_artifacts(self):
        job_id = "margin-reconcile-unconfirmed"
        artifacts = self.prepare_unknown_margin_reconcile(job_id)
        originals = {
            name: Path(artifacts[name]).read_bytes()
            for name in ("source_path", "runner_path", "lock_path")
        }
        self.margin.history_payload = {"rows": []}

        self.assertEqual(self.reconcile_margin(job_id), 3)

        for name, encoded in originals.items():
            self.assertEqual(Path(artifacts[name]).read_bytes(), encoded)
        self.assertEqual(self.results()[-1], {
            "kind": "RECONCILE_UNCONFIRMED",
            "job_id": job_id,
            "reason": "MARGIN_CONFIRMATION_MISMATCH",
            "utc": utc(NOW),
        })
        self.assertEqual(self.governor_releases, [])
        result_ids, bad_results, unresolved = jr._result_ids(
            jr._paths(self.receipt_dir)["results"]
        )
        self.assertEqual(result_ids, {job_id})
        self.assertFalse(bad_results)
        self.assertTrue(unresolved)

    def test_reconcile_margin_keeps_cross_direction_activity_fence(self):
        job_id = "margin-reconcile-concurrent"
        artifacts = self.prepare_unknown_margin_reconcile(job_id)
        originals = {
            name: Path(artifacts[name]).read_bytes()
            for name in ("source_path", "runner_path", "lock_path")
        }
        target = self.adapter_margin_row(
            txId=401344857108, amount="2.5", principal="2.5"
        )
        unrelated = self.adapter_margin_row(
            txId=401344857109, amount="1", principal="1"
        )
        self.margin.history_after_payloads = {
            "BORROW": {"rows": [target]},
            "REPAY": {"rows": [unrelated]},
        }

        self.assertEqual(self.reconcile_margin(job_id), 3)

        self.assertEqual(
            self.results()[-1]["reason"], "CONCURRENT_MARGIN_ACTIVITY"
        )
        for name, encoded in originals.items():
            self.assertEqual(Path(artifacts[name]).read_bytes(), encoded)
        self.assertEqual(self.governor_releases, [])

    def test_reconcile_margin_repay_retains_dust_verdict(self):
        job_id = "margin-reconcile-repay-dust"
        self.margin.post_payload = {"tranId": 7301}
        self.margin.history_payload = {"rows": []}
        self.margin.account_before_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "2.5",
            "interest": "0.000005",
        }]}
        self.margin.account_payload = {"userAssets": [{
            "asset": "USDT", "borrowed": "0",
            "interest": "0.000001",
        }]}
        self.margin_job(
            job_id, op="MARGIN_REPAY", principal="2.500005"
        )
        self.assertEqual(self.margin_tick(), 4)
        self.margin.history_payload = {"rows": [{
            "txId": 7301,
            "asset": "USDT",
            "amount": "2.500005",
            "principal": "2.5",
            "interest": "0.000005",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "MANUAL",
        }]}

        self.assertEqual(self.reconcile_margin(job_id), 0)

        rows = self.results()
        self.assertEqual((rows[1]["kind"], rows[1]["verdict"]), (
            "RECONCILED_FROM_EXCHANGE_TRUTH", "CLOSED-WITH-DUST",
        ))
        self.assertEqual((rows[2]["verdict"], rows[2]["exit_code"]), (
            "CLOSED-WITH-DUST", 0,
        ))
        source = self.margin_source(rows[2])
        self.assertEqual(source["follow_up_repay"], {
            "kind": "INTEREST_ONLY_REPAY",
            "asset": "USDT",
            "principal": "0.000001",
        })
        self.assertEqual(self.governor_releases, [])

    def test_reconcile_margin_operator_digest_change_refuses_before_reads(self):
        job_id = "margin-reconcile-operator"
        artifacts = self.prepare_unknown_margin_reconcile(job_id)
        originals = {
            name: Path(artifacts[name]).read_bytes()
            for name in ("source_path", "runner_path", "lock_path")
        }
        self.write_operator("another-operator")
        calls_before = list(self.margin.calls)

        self.assertEqual(self.reconcile_margin(job_id), 3)

        self.assertEqual(self.margin.calls, calls_before)
        for name, encoded in originals.items():
            self.assertEqual(Path(artifacts[name]).read_bytes(), encoded)
        self.assertEqual(
            self.results()[-1]["reason"], "OPERATOR_DIGEST_MISMATCH"
        )
        self.assertEqual(self.governor_releases, [])

    def test_tick_never_calls_margin_reconciliation(self):
        job_id = "margin-reconcile-tick"
        self.prepare_unknown_margin_reconcile(job_id)

        with mock.patch.object(jr, "reconcile_margin") as reconcile:
            self.assertEqual(self.tick(), 3)

        reconcile.assert_not_called()

    def test_reconcile_margin_cli_requires_explicit_confirmation(self):
        job_id = "margin-reconcile-cli"
        with mock.patch("sys.stderr"), \
                self.assertRaises(SystemExit) as refused:
            jr.main(["reconcile-margin", "--job-id", job_id])
        self.assertEqual(refused.exception.code, 2)

        with mock.patch.object(
                jr, "reconcile_margin", return_value=0) as reconcile:
            self.assertEqual(jr.main([
                "reconcile-margin", "--job-id", job_id,
                "--confirm-reconcile",
            ]), 0)
        reconcile.assert_called_once_with(job_id, True)

    def test_exact_margin_artifact_anomalies_halt_tick_and_manual(self):
        job_id = "margin-exact-anomalies"
        paths, originals = self.standing_margin_artifacts(job_id)
        lock_path = paths["LOCK"]
        cases = (
            (
                "RESULT", "verdict", "TYPE", "RESULT_VERDICT_TYPE",
                lambda: self.replace_json_field(
                    paths["RESULT"], "verdict", []
                ),
            ),
            (
                "LOCK", "token", "TYPE", "LOCK_TOKEN_TYPE",
                lambda: self.replace_json_field(paths["LOCK"], "token", []),
            ),
            (
                "RUNNER_RECEIPT", "reason", "DEPTH_LIMIT",
                "RUNNER_RECEIPT_STRUCTURE_LIMIT",
                lambda: self.replace_with_nested_json(
                    paths["RUNNER_RECEIPT"], "reason", 1200
                ),
            ),
        )
        for artifact, field, anomaly_type, reason, mutate in cases:
            for command in ("tick", "manual"):
                with self.subTest(
                        artifact=artifact, field=field, command=command):
                    self.restore_margin_artifacts(originals)
                    mutate()
                    if command == "tick":
                        self.assertEqual(self.tick(), 3)
                    else:
                        with mock.patch("builtins.print") as output:
                            self.assertEqual(jr.release_stuck_lock(
                                job_id, True, self.receipt_dir, NOW
                            ), 3)
                        output.assert_called_once_with(
                            "JOB-RUNNER | RELEASE REFUSED | " + reason
                        )
                    self.assert_margin_anomaly_halt(
                        job_id, lock_path, artifact, field, anomaly_type
                    )

    def test_manual_release_types_every_margin_artifact_field(self):
        job_id = "margin-artifact-types"
        paths, originals = self.standing_margin_artifacts(job_id)
        lock_path = paths["LOCK"]
        cases = (
            ("RESULT", "verdict"),
            ("RUNNER_RECEIPT", "verdict"),
            ("SOURCE_RECEIPT", "verdict"),
            ("JOB_FILE", "created_utc"),
            ("LOCK", "token"),
        )
        for artifact, field in cases:
            with self.subTest(artifact=artifact, field=field):
                self.restore_margin_artifacts(originals)
                self.replace_json_field(paths[artifact], field, [])

                self.assertEqual(jr.release_stuck_lock(
                    job_id, True, self.receipt_dir, NOW
                ), 3)

                self.assert_margin_anomaly_halt(
                    job_id, lock_path, artifact, field, "TYPE"
                )

    def test_manual_release_bounds_depth_in_every_margin_artifact(self):
        job_id = "margin-artifact-depth"
        paths, originals = self.standing_margin_artifacts(job_id)
        lock_path = paths["LOCK"]
        cases = (
            ("RESULT", "verdict"),
            ("RUNNER_RECEIPT", "reason"),
            ("SOURCE_RECEIPT", "reason"),
            ("JOB_FILE", "requested_by"),
            ("LOCK", "token"),
        )
        for artifact, field in cases:
            with self.subTest(artifact=artifact, field=field):
                self.restore_margin_artifacts(originals)
                self.replace_with_nested_json(
                    paths[artifact], field, 1200
                )

                self.assertEqual(jr.release_stuck_lock(
                    job_id, True, self.receipt_dir, NOW
                ), 3)

                self.assert_margin_anomaly_halt(
                    job_id, lock_path, artifact, field, "DEPTH_LIMIT"
                )

    def test_manual_release_bounds_margin_artifact_node_count(self):
        job_id = "margin-artifact-nodes"
        paths, originals = self.standing_margin_artifacts(job_id)
        lock_path = paths["LOCK"]
        self.restore_margin_artifacts(originals)
        self.replace_with_nested_json(
            paths["RUNNER_RECEIPT"], "reason", 0,
            wide_count=jr.MARGIN_ARTIFACT_MAX_NODES + 1,
        )

        self.assertEqual(jr.release_stuck_lock(
            job_id, True, self.receipt_dir, NOW
        ), 3)

        self.assert_margin_anomaly_halt(
            job_id, lock_path, "RUNNER_RECEIPT", "reason", "NODE_LIMIT"
        )

    def test_manual_release_types_duplicates_and_unparseable_lock(self):
        job_id = "margin-artifact-json"
        paths, originals = self.standing_margin_artifacts(job_id)
        lock_path = paths["LOCK"]
        duplicate_cases = (
            ("RESULT", "verdict"),
            ("SOURCE_RECEIPT", "reason"),
            ("JOB_FILE", "requested_by"),
            ("LOCK", "token"),
        )
        for artifact, field in duplicate_cases:
            with self.subTest(artifact=artifact, field=field):
                self.restore_margin_artifacts(originals)
                self.add_duplicate_json_field(paths[artifact], field)

                self.assertEqual(jr.release_stuck_lock(
                    job_id, True, self.receipt_dir, NOW
                ), 3)

                self.assert_margin_anomaly_halt(
                    job_id, lock_path, artifact, field, "DUPLICATE_FIELD"
                )

        self.restore_margin_artifacts(originals)
        Path(lock_path).write_text("{", encoding="utf-8")

        self.assertEqual(jr.release_stuck_lock(
            job_id, True, self.receipt_dir, NOW
        ), 3)

        self.assert_margin_anomaly_halt(
            job_id, lock_path, "LOCK", "$", "INVALID_JSON"
        )

    def test_manual_release_is_idempotent_after_receipts_are_released(self):
        job_id = "margin-repeat-manual"
        row, lock_path = self.interrupt_margin_governor_release(job_id)

        self.assertEqual(jr.release_stuck_lock(
            job_id, True, self.receipt_dir, NOW
        ), 0)
        releases_after_first = list(self.governor_releases)
        with mock.patch("builtins.print") as output:
            self.assertEqual(jr.release_stuck_lock(
                job_id, True, self.receipt_dir, NOW
            ), 0)

        output.assert_called_once_with(
            "JOB-RUNNER | ALREADY_RELEASED | " + job_id
        )
        self.assertEqual(self.governor_releases, releases_after_first)
        self.assertFalse(os.path.exists(lock_path))
        with open(row["receipt_path"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["governor_release"], "RELEASED")
        self.assertEqual(
            self.margin_source(row)["governor_release"], "RELEASED"
        )

    def test_sourceless_final_refusal_halts_with_exact_durable_facts(self):
        job_id = "margin-sourceless-recovery"
        row, lock_path = self.interrupt_margin_governor_release(
            job_id, measured_sha="invalid-sha"
        )
        with open(row["receipt_path"], encoding="utf-8") as handle:
            before = json.load(handle)
        token = before["governor_token"]

        self.assertEqual(self.tick(), 3)

        self.assertEqual(self.governor_releases, [])
        self.assertTrue(os.path.isfile(lock_path))
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))
        self.assertEqual(
            self.result_events("margin_governor_release_halted"),
            [self.margin_halt_row(job_id, token, "REFUSED", False)],
        )
        with open(row["receipt_path"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["governor_release"], "PENDING")

    def test_manual_release_refuses_duplicate_receipt_names(self):
        job_id = "margin-runner-duplicate"
        row, lock_path = self.interrupt_margin_governor_release(
            job_id, measured_sha="invalid-sha"
        )
        raw = Path(row["receipt_path"]).read_text(encoding="utf-8")
        Path(row["receipt_path"]).write_text(
            raw.rstrip()[:-1] + ',"mutation_attempts":0}\n',
            encoding="utf-8",
        )

        with mock.patch("builtins.print") as output:
            self.assertEqual(jr.release_stuck_lock(
                job_id, True, self.receipt_dir, NOW
            ), 3)

        self.assertEqual(self.governor_releases, [])
        self.assertTrue(os.path.isfile(lock_path))
        output.assert_called_once_with(
            "JOB-RUNNER | RELEASE REFUSED | "
            "RUNNER_RECEIPT_DUPLICATE_FIELD"
        )

    def test_manual_release_uses_the_validated_job_digest_binding(self):
        job_id = "margin-job-digest"
        _row, lock_path = self.interrupt_margin_governor_release(
            job_id, measured_sha="invalid-sha"
        )
        job_path = os.path.join(self.jobs_dir, "job-%s.json" % job_id)
        with open(job_path, "a", encoding="utf-8") as handle:
            handle.write("\n")

        with mock.patch("builtins.print") as output:
            self.assertEqual(jr.release_stuck_lock(
                job_id, True, self.receipt_dir, NOW
            ), 3)

        self.assertEqual(self.governor_releases, [])
        self.assertTrue(os.path.isfile(lock_path))
        output.assert_called_once_with(
            "JOB-RUNNER | RELEASE REFUSED | JOB_DIGEST_MISMATCH"
        )

    def test_source_backed_final_halts_idempotently_without_manual_call(self):
        job_id = "margin-release-halt"
        row, lock_path = self.interrupt_margin_governor_release(job_id)
        with open(row["receipt_path"], encoding="utf-8") as handle:
            runner = json.load(handle)
        token = runner["governor_token"]

        with mock.patch.object(jr, "release_stuck_lock") as manual:
            self.assertEqual(self.tick(), 3)
            self.assertEqual(self.tick(), 3)

        manual.assert_not_called()
        self.assertEqual(self.governor_releases, [])
        self.assertTrue(os.path.isfile(lock_path))
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))
        self.assertEqual(
            self.result_events("margin_governor_release_halted"),
            [self.margin_halt_row(job_id, token, "OPEN", True)],
        )
        source_path = runner["source_receipt_path"]
        with open(source_path, encoding="utf-8") as handle:
            source = json.load(handle)
        source["verdict"] = "REFUSED"
        Path(source_path).write_text(json.dumps(source), encoding="utf-8")

        self.assertEqual(self.tick(), 3)

        changed = self.result_events("margin_governor_release_halted")
        self.assertEqual(len(changed), 2)
        self.assertEqual(changed[1]["source_verdict"], "REFUSED")

    def test_manual_release_cli_requires_confirmation_and_attests_source(self):
        job_id = "margin-manual-source"
        row, lock_path = self.interrupt_margin_governor_release(job_id)
        self.assertEqual(self.tick(), 3)
        with mock.patch("sys.stderr"), \
                self.assertRaises(SystemExit) as refused:
            jr.main(["release-stuck-lock", "--job-id", job_id])
        self.assertEqual(refused.exception.code, 2)
        self.assertTrue(os.path.isfile(lock_path))

        self.assertEqual(jr.main([
            "release-stuck-lock", "--job-id", job_id, "--confirm-release",
        ]), 0)

        self.assertFalse(os.path.exists(lock_path))
        with open(row["receipt_path"], encoding="utf-8") as handle:
            runner = json.load(handle)
        self.assertEqual(runner["governor_release"], "RELEASED")
        self.assertEqual(self.margin_source(row)["governor_release"], "RELEASED")

    def test_manual_release_attests_runner_only_refusal(self):
        job_id = "margin-manual-runner"
        row, lock_path = self.interrupt_margin_governor_release(
            job_id, measured_sha="invalid-sha"
        )
        self.assertEqual(jr.release_stuck_lock(
            job_id, True, self.receipt_dir, NOW
        ), 0)

        self.assertFalse(os.path.exists(lock_path))
        with open(row["receipt_path"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["governor_release"], "RELEASED")

    def test_manual_release_names_each_cross_artifact_disagreement(self):
        job_id = "margin-manual-disagreements"
        row, lock_path = self.interrupt_margin_governor_release(job_id)
        runner_path = row["receipt_path"]
        with open(runner_path, encoding="utf-8") as handle:
            runner = json.load(handle)
        source_path = runner["source_receipt_path"]
        with open(source_path, encoding="utf-8") as handle:
            source = json.load(handle)
        with open(lock_path, encoding="utf-8") as handle:
            lock = json.load(handle)
        results_path = jr._paths(self.receipt_dir)["results"]
        result = json.loads(Path(results_path).read_text(encoding="utf-8"))

        def refuses(reason):
            with mock.patch("builtins.print") as output:
                self.assertEqual(jr.release_stuck_lock(
                    job_id, True, self.receipt_dir, NOW
                ), 3)
            output.assert_called_once_with(
                "JOB-RUNNER | RELEASE REFUSED | " + reason
            )
            self.assertEqual(self.governor_releases, [])
            self.assertTrue(os.path.isfile(lock_path))

        cases = (
            (results_path, result, ("symbol",), "BTC", "RESULT_SYMBOL_MISMATCH"),
            (results_path, result, ("exit_code",), False, "RESULT_OUTCOME_MISMATCH"),
            (runner_path, runner, ("verdict",), "REFUSED", "RUNNER_VERDICT_MISMATCH"),
            (source_path, source, ("ready", "params", "amount"), "999", "SOURCE_READY_MISMATCH"),
            (source_path, source, ("ready", "params", "type"), "REPAY", "SOURCE_READY_MISMATCH"),
            (source_path, source, ("ready", "endpoint_path"), "/wrong", "SOURCE_READY_MISMATCH"),
            (source_path, source, ("mutation_attempts",), True,
             "SOURCE_MUTATION_ATTEMPTS_MISMATCH"),
            (source_path, source, ("governor_token",), "other", "SOURCE_GOVERNOR_TOKEN_MISMATCH"),
            (lock_path, lock, ("token",), "other", "LOCK_TOKEN_MISMATCH"),
        )
        for path, original, names, value, reason in cases:
            with self.subTest(reason=reason, names=names):
                changed = json.loads(json.dumps(original))
                target = changed
                for name in names[:-1]:
                    target = target[name]
                target[names[-1]] = value
                Path(path).write_text(json.dumps(changed), encoding="utf-8")
                refuses(reason)
                Path(path).write_text(json.dumps(original), encoding="utf-8")

    def test_unknown_touches_stop_and_blocks_next_job(self):
        self.job("a")
        self.job("b")
        self.hand_verdict = "UNKNOWN"
        self.hand_exit = 4
        self.assertEqual(self.tick(), 4)
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "ACCEPT-ONE"
        )))
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))
        self.hand_verdict = "CLOSED"
        self.hand_exit = 0
        self.assertEqual(self.tick(), 3)
        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual([row["job_id"] for row in self.results()], ["a"])

    def test_governor_refusal_blocks_entry_before_executor(self):
        self.governor_acquire_refusal = "ACCOUNT_FROZEN"
        self.job("governor-refused")

        self.assertEqual(self.tick(), 2)

        self.assertEqual(self.hand_calls, [])
        self.assertEqual(len(self.governor_budget_calls), 1)
        self.assertEqual(len(self.governor_acquire_calls), 1)
        self.assertEqual(self.governor_releases, [])
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "ACCEPT-ONE"
        )))
        self.assertEqual(
            self.reason(self.results()[0]), "ACCOUNT_FROZEN"
        )

    def test_governor_budget_refusal_blocks_entry_before_claim_slot(self):
        self.governor_budget = (False, "DAILY_BUDGET_EXHAUSTED")
        self.job("budget-refused")

        self.assertEqual(self.tick(), 2)

        self.assertEqual(self.hand_calls, [])
        self.assertEqual(len(self.governor_budget_calls), 1)
        self.assertEqual(self.governor_acquire_calls, [])
        self.assertEqual(
            self.reason(self.results()[0]), "DAILY_BUDGET_EXHAUSTED"
        )

    def test_governor_absence_fails_entry_closed(self):
        self.job("governor-absent")

        with mock.patch.dict(sys.modules, {"account_governor": None}):
            self.assertEqual(self.tick(), 2)

        self.assertEqual(self.hand_calls, [])
        self.assertEqual(
            self.reason(self.results()[0]), "GOVERNOR_ABSENT"
        )

    def test_clearing_stop_cannot_bypass_unreconciled_unknown_receipt(self):
        self.job("a")
        self.job("b")
        self.hand_verdict = "UNKNOWN"
        self.hand_exit = 4
        self.assertEqual(self.tick(), 4)
        os.unlink(os.path.join(self.receipt_dir, "STOP-JOBS"))
        self.hand_verdict = "CLOSED"
        self.hand_exit = 0
        self.assertEqual(self.tick(), 3)
        self.assertEqual(len(self.hand_calls), 1)
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_unreconciled_lock_at_start_freezes_queue(self):
        self.job("locked")
        Path(os.path.join(self.receipt_dir, "futures-hand.lock")).touch()
        self.assertEqual(self.tick(), 3)
        self.assertEqual(self.hand_calls, [])
        self.assertTrue(os.path.exists(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_basis_is_sequence_locked_then_accepted_after_marker(self):
        self.write_accept_one(max_notional="99")
        self.job("basis1", op="BASIS_ROUNDTRIP", max_notional=99)
        self.assertEqual(self.tick(), 2)
        first = self.results()[0]
        self.assertEqual(first["verdict"], "REFUSED")
        self.assertEqual(self.reason(first), "SEQUENCE_LOCKED")
        self.assertEqual(self.basis_calls, [])

        Path(os.path.join(self.jobs_dir, "futures_proof_passed")).touch()
        self.job("basis2", op="BASIS_ROUNDTRIP", max_notional=99)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.basis_calls), 1)
        self.assertEqual(self.basis_calls[0]["argv"], ["--execute"])
        self.assertEqual(self.basis_calls[0]["executor_cap"], "8")
        self.assertEqual(self.basis_calls[0]["cycle_cap"], "8")
        self.assertTrue(os.path.isfile(os.path.join(
            self.jobs_dir, "basis_proof_passed"
        )))

    def test_momentum_is_sequence_locked_before_both_markers(self):
        self.job("momentum-locked", op="MOMENTUM_ROUNDTRIP")
        self.assertEqual(self.tick(), 2)
        row = self.results()[0]
        self.assertEqual(row["verdict"], "REFUSED")
        self.assertEqual(self.reason(row), "SEQUENCE_LOCKED")
        self.assertEqual(self.hand_calls, [])

    def test_closed_basis_result_backfills_marker_and_unlocks_momentum(self):
        Path(os.path.join(self.jobs_dir, "futures_proof_passed")).touch()
        proof_utc = utc(NOW - timedelta(minutes=2))
        receipt = os.path.join(
            self.receipt_dir, "basis-executor-prior.json"
        )
        with open(receipt, "w", encoding="utf-8") as handle:
            json.dump({"verdict": "CLOSED"}, handle)
        prior = {
            "job_id": "basis-live",
            "op": "BASIS_ROUNDTRIP",
            "symbol": "TSTUSDT",
            "verdict": "CLOSED",
            "exit_code": 0,
            "receipt_path": receipt,
            "utc": proof_utc,
        }
        with open(os.path.join(self.jobs_dir, "results.jsonl"), "w",
                  encoding="utf-8") as handle:
            handle.write(json.dumps(prior) + "\n")
        self.job("momentum-backfill", op="MOMENTUM_ROUNDTRIP")

        with mock.patch("builtins.print") as output:
            self.assertEqual(self.tick(), 0)

        marker = os.path.join(self.jobs_dir, "basis_proof_passed")
        with open(marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "basis-live %s\n" % proof_utc)
        self.assertTrue(any(
            call.args and "BASIS_PROOF_BACKFILLED" in call.args[0]
            for call in output.call_args_list
        ))
        self.assertEqual(
            self.hand_calls[0]["intent"]["reduceOnly"], "false"
        )

    def test_momentum_uses_long_path_clamped_cap_after_forcing_one_x(self):
        Path(os.path.join(self.jobs_dir, "futures_proof_passed")).touch()
        Path(os.path.join(self.jobs_dir, "basis_proof_passed")).touch()
        self.leverage.current = 5
        self.write_accept_one(max_notional="800")
        self.job(
            "momentum-clamped", op="MOMENTUM_ROUNDTRIP", max_notional=800
        )

        self.assertEqual(self.tick(), 0)

        self.assertEqual(self.hand_calls[0]["intent"]["quantity"], "0.04")
        self.assertEqual(self.leverage.calls[1], (
            "POST", "/fapi/v1/leverage",
            {"symbol": "TSTUSDT", "leverage": "1"}, True,
        ))
        self.assertEqual(self.leverage.current, 1)
        self.assertEqual(
            [event[0] for event in self.events],
            ["leverage", "leverage", "leverage", "futures", "leverage"],
        )

    def test_unknown_momentum_touches_stop_and_blocks_next_job(self):
        Path(os.path.join(self.jobs_dir, "futures_proof_passed")).touch()
        Path(os.path.join(self.jobs_dir, "basis_proof_passed")).touch()
        self.job("momentum-a", op="MOMENTUM_ROUNDTRIP")
        self.job("momentum-b", op="MOMENTUM_ROUNDTRIP")
        self.hand_verdict = "UNKNOWN"
        self.hand_exit = 4

        self.assertEqual(self.tick(), 4)
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))
        self.hand_verdict = "CLOSED"
        self.hand_exit = 0
        self.assertEqual(self.tick(), 3)
        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual(
            [row["job_id"] for row in self.results()], ["momentum-a"]
        )

    def test_momentum_open_is_capped_atomic_and_records_exchange_fill(self):
        quantity = self.open_momentum("position-open", max_notional=800)

        self.assertEqual(quantity, Decimal("0.04"))
        self.assertEqual(self.momentum_state()["effective_notional"], "8")
        self.assertEqual({
            path for method, path, _params, _signed
            in self.momentum_reads.calls if method == "GET"
        }, {
            "/fapi/v1/symbolConfig", "/fapi/v1/exchangeInfo",
            "/fapi/v1/ticker/bookTicker", "/fapi/v1/premiumIndex",
            "/fapi/v1/accountConfig", "/fapi/v1/positionSide/dual",
            "/fapi/v3/account", "/fapi/v1/openOrders",
            "/fapi/v3/positionRisk", "/fapi/v3/balance",
            "/fapi/v1/order", "/fapi/v1/userTrades",
        })
        self.assertEqual(len(self.momentum_intents), 1)
        intent = self.momentum_intents[0]
        self.assertEqual(intent["side"], "BUY")
        self.assertEqual(intent["reduceOnly"], "false")
        self.assertEqual(intent["quantity"], "0.04")

        state = self.momentum_state()
        self.assertEqual(state["state"], "OPEN")
        self.assertEqual(state["measured_qty"], "0.04")
        self.assertEqual(state["entry"]["qty"], "0.04")
        self.assertEqual(state["entry"]["price"], "200")
        self.assertEqual(state["entry"]["order_id"], "1101")
        self.assertEqual(
            state["entry"]["client_id"], intent["newClientOrderId"]
        )
        self.assertEqual(state["entry"]["fills"][0]["commission"], "0.003")
        self.assertEqual(list(Path(self.receipt_dir).glob(
            "momentum-position.json.tmp-*"
        )), [])

        row = self.results()[0]
        self.assertEqual(row["verdict"], "OPEN")
        self.assertEqual(row["fills"][0]["qty"], "0.04")
        self.assertEqual(row["fees"][0]["commission_asset"], "USDT")
        self.assertEqual(row["realized"]["net_usdt"], "UNKNOWN")

    def test_momentum_open_and_roundtrip_refuse_while_position_stands(self):
        quantity = self.open_momentum("first-open")
        before_intents = len(self.momentum_intents)
        before_leverage = len(self.leverage.calls)
        self.write_accept_one()
        self.job("second-open", op="MOMENTUM_OPEN")
        self.queue_momentum_measurement(quantity)
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 2)
        self.assertEqual(len(self.momentum_intents), before_intents)
        self.assertEqual(len(self.leverage.calls), before_leverage)
        self.assertEqual(
            self.reason(self.results()[-1]),
            "MOMENTUM_POSITION_ALREADY_STANDS",
        )

        self.job("standing-roundtrip", op="MOMENTUM_ROUNDTRIP")
        self.queue_momentum_measurement(quantity)
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 2)
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(
            self.reason(self.results()[-1]),
            "MOMENTUM_POSITION_ALREADY_STANDS",
        )

    def test_momentum_exit_refuses_when_none_without_exchange_action(self):
        os.unlink(os.path.join(self.receipt_dir, "ACCEPT-ONE"))
        os.unlink(os.path.join(self.receipt_dir, "OPERATOR"))
        self.job("exit-none", op="MOMENTUM_EXIT")
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 2)
        self.assertEqual(self.momentum_intents, [])
        self.assertEqual(self.momentum_reads.calls, [])
        self.assertEqual(self.leverage.calls, [])
        self.assertEqual(
            self.reason(self.results()[0]), "NO_MOMENTUM_POSITION"
        )

    def _assert_momentum_exit_runs_under(self, marker):
        quantity = self.open_momentum("stop-exit-open")
        os.unlink(os.path.join(self.receipt_dir, "OPERATOR"))
        Path(os.path.join(self.receipt_dir, marker)).touch()
        self.job("stop-exit-close", op="MOMENTUM_EXIT")
        self.queue_momentum_measurement(quantity)
        self.queue_momentum_measurement(0)

        with self.momentum_runtime():
            self.assertEqual(self.tick(), 0)

        self.assertEqual(self.results()[-1]["verdict"], "CLOSED")
        self.assertEqual(self.momentum_intents[-1]["reduceOnly"], "true")
        self.assertEqual(self.momentum_state()["state"], "CLOSED")
        self.assertEqual(len(self.governor_realized), 1)

    def test_momentum_exit_runs_under_stop(self):
        self._assert_momentum_exit_runs_under("STOP")

    def test_momentum_exit_runs_under_stop_jobs(self):
        self._assert_momentum_exit_runs_under("STOP-JOBS")

    def test_closed_momentum_exit_writes_first_close_marker_once(self):
        quantity = self.open_momentum_http("marker-open")
        self.job("marker-exit", op="MOMENTUM_EXIT")
        self.queue_momentum_measurement(quantity)
        self.queue_momentum_measurement(0)

        original_finish = jr._finish_claimed
        replay = {}

        class CrashAfterMarker(Exception):
            pass

        def crash_finish(*args, **kwargs):
            replay["args"] = args
            replay["kwargs"] = kwargs
            with mock.patch.object(
                    jr, "_append_result", side_effect=CrashAfterMarker):
                return original_finish(*args, **kwargs)

        with self.momentum_http_runtime(), \
                mock.patch.object(
                    jr, "_finish_claimed", side_effect=crash_finish
                ):
            with self.assertRaises(CrashAfterMarker):
                self.tick()

        marker = os.path.join(
            self.receipt_dir, "first_autonomous_close_passed"
        )
        with open(marker, encoding="utf-8") as handle:
            before = handle.read()
        before_stat = os.stat(marker)
        self.assertEqual(before, "marker-exit %s\n" % utc(NOW))
        stop = os.path.join(self.receipt_dir, "STOP-JOBS")
        self.assertTrue(os.path.isfile(stop))
        before_stop_stat = os.stat(stop)

        self.assertEqual(
            original_finish(*replay["args"], **replay["kwargs"]), 0
        )

        with open(marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)
        after_stat = os.stat(marker)
        self.assertEqual(after_stat.st_ino, before_stat.st_ino)
        self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)
        after_stop_stat = os.stat(stop)
        self.assertEqual(after_stop_stat.st_ino, before_stop_stat.st_ino)
        self.assertEqual(
            after_stop_stat.st_mtime_ns, before_stop_stat.st_mtime_ns
        )
        self.assertEqual([
            row["job_id"] for row in self.results()
            if row.get("job_id") == "marker-exit"
        ], ["marker-exit"])

    @unittest.skipUnless(
        hasattr(jr.limits, "MEASURED_ROUNDTRIP_FEE_PCT"),
        "fee-aware adverse-stop interface has not landed",
    )
    def test_max_adverse_stop_includes_fee_and_never_marks_stop_out_clean(self):
        quantity = self.open_momentum_http("adverse-fee-open")
        before_intents = len(self.momentum_intents)
        marker = os.path.join(
            self.receipt_dir, "first_autonomous_close_passed"
        )
        self.queue_momentum_measurement(quantity, mark_price="196.70")

        with self.momentum_http_runtime():
            self.assertEqual(self.tick(), 0)

        self.assertEqual(len(self.momentum_intents), before_intents)
        self.assertEqual(self.momentum_state()["state"], "OPEN")
        self.assertFalse(os.path.exists(marker))

        self.queue_momentum_measurement(quantity, mark_price="196.50")
        self.queue_momentum_measurement(0)
        with self.momentum_http_runtime():
            self.assertEqual(self.tick(), 0)

        close = self.momentum_intents[-1]
        self.assertEqual(close["reduceOnly"], "true")
        self.assertEqual(close["quantity"], str(quantity))
        self.assertEqual(self.momentum_state()["state"], "CLOSED")
        row = self.results()[-1]
        self.assertEqual(row["op"], "MOMENTUM_EXIT")
        self.assertEqual(row["verdict"], "CLOSED")
        self.assertEqual(self.reason(row), "max_adverse_stop")
        self.assertFalse(os.path.exists(marker))

    @unittest.skipUnless(
        hasattr(jr.limits, "MEASURED_ROUNDTRIP_FEE_PCT"),
        "adverse-stop continuation interface has not landed",
    )
    def test_adverse_stop_http_502_does_not_starve_queued_exit(self):
        quantity = self.open_momentum_http("adverse-502-open")
        before_intents = len(self.momentum_intents)
        self.job("adverse-502-rescue", op="MOMENTUM_EXIT")
        self.momentum_reads.queue_position_failure(
            "unclear", {"http": 502}, count=3
        )
        self.queue_momentum_measurement(quantity)
        self.queue_momentum_measurement(0)

        with mock.patch("builtins.print") as output, \
                self.momentum_http_runtime():
            self.assertEqual(self.tick(), 0)

        events = self.result_events("adverse_stop_refusal")
        self.assertEqual(events, [{
            "kind": "adverse_stop_refusal",
            "error": "MomentumStateError",
            "symbol": "TSTUSDT",
            "utc": utc(NOW),
        }])
        self.assertTrue(any(
            "adverse_stop_refusal" in " ".join(map(str, call.args))
            for call in output.call_args_list
        ))
        self.assertEqual(len(self.momentum_intents), before_intents + 1)
        self.assertEqual(self.momentum_intents[-1]["reduceOnly"], "true")
        rescue = [
            row for row in self.results()
            if row.get("job_id") == "adverse-502-rescue"
        ]
        self.assertEqual(len(rescue), 1)
        self.assertEqual(rescue[0]["verdict"], "CLOSED")

    @unittest.skipUnless(
        hasattr(jr.limits, "MEASURED_ROUNDTRIP_FEE_PCT"),
        "adverse-stop continuation interface has not landed",
    )
    def test_adverse_stop_leverage_drift_does_not_starve_queued_exit(self):
        quantity = self.open_momentum_http("adverse-drift-open")
        before_intents = len(self.momentum_intents)
        self.job("adverse-drift-rescue", op="MOMENTUM_EXIT")
        self.queue_momentum_measurement(quantity, leverage=2)
        self.queue_momentum_measurement(quantity)
        self.queue_momentum_measurement(0)

        with self.momentum_http_runtime():
            self.assertEqual(self.tick(), 0)

        self.assertEqual(self.result_events("adverse_stop_refusal"), [{
            "kind": "adverse_stop_refusal",
            "error": "MomentumStateError",
            "symbol": "TSTUSDT",
            "utc": utc(NOW),
        }])
        self.assertEqual(len(self.momentum_intents), before_intents + 1)
        self.assertEqual(self.momentum_intents[-1]["reduceOnly"], "true")
        rescue = [
            row for row in self.results()
            if row.get("job_id") == "adverse-drift-rescue"
        ]
        self.assertEqual(len(rescue), 1)
        self.assertEqual(rescue[0]["verdict"], "CLOSED")

    @unittest.skipUnless(
        hasattr(jr.limits, "MEASURED_ROUNDTRIP_FEE_PCT"),
        "adverse-stop unknown-position interface has not landed",
    )
    def test_unknown_standing_position_seals_reports_and_never_orders(self):
        self.open_momentum_http("adverse-unknown-open")
        state = self.momentum_state()
        state["state"] = "UNKNOWN"
        state["failure_reason"] = "offline unknown-position fixture"
        with open(os.path.join(
                self.receipt_dir, "momentum-position.json"
        ), "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        before_intents = list(self.momentum_intents)

        with mock.patch("builtins.print") as output, \
                self.momentum_http_runtime():
            self.assertEqual(self.tick(), 0)

        events = self.result_events("adverse_stop_blocked_unknown")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["symbol"], "TSTUSDT")
        self.assertEqual(events[0]["utc"], utc(NOW))
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))
        self.assertEqual(self.momentum_intents, before_intents)
        printed = [" ".join(map(str, call.args))
                   for call in output.call_args_list]
        self.assertTrue(any(
            "adverse_stop_blocked_unknown" in line for line in printed
        ))
        self.assertNotIn(
            "JOB-RUNNER | IDLE | no due unclaimed job", printed
        )

    def _assert_persisted_adverse_stop_refusal(
            self, job_id, expected_error, field=None, value=None,
            corrupt=False):
        quantity = self.open_momentum_http(job_id + "-open")
        state_path = os.path.join(
            self.receipt_dir, "momentum-position.json"
        )
        if corrupt:
            with open(state_path, "w", encoding="utf-8") as handle:
                handle.write("{not-json")
        else:
            state = self.momentum_state()
            state["entry"][field] = value
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            self.queue_momentum_measurement(quantity)
        before_intents = list(self.momentum_intents)

        with self.momentum_http_runtime():
            self.assertEqual(self.tick(), 2)

        events = self.result_events("adverse_stop_refusal")
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0].get("symbol"), str)
        event = dict(events[0])
        del event["symbol"]
        self.assertEqual(event, {
            "kind": "adverse_stop_refusal",
            "error": expected_error,
            "utc": utc(NOW),
        })
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))
        self.assertEqual(self.momentum_intents, before_intents)

    @unittest.skipUnless(
        hasattr(jr, "_max_adverse_stop"),
        "max-adverse-stop interface has not landed",
    )
    def test_corrupt_position_file_writes_typed_refusal_and_seals(self):
        self._assert_persisted_adverse_stop_refusal(
            "adverse-corrupt", "UNKNOWN_POSITION", corrupt=True
        )

    @unittest.skipUnless(
        hasattr(jr, "_max_adverse_stop"),
        "max-adverse-stop interface has not landed",
    )
    def test_bad_persisted_price_writes_typed_refusal_and_seals(self):
        self._assert_persisted_adverse_stop_refusal(
            "adverse-bad-price", "UNKNOWN_ENTRY_PRICE",
            field="price", value="not-a-decimal",
        )

    @unittest.skipUnless(
        hasattr(jr, "_max_adverse_stop"),
        "max-adverse-stop interface has not landed",
    )
    def test_unknown_side_writes_typed_refusal_and_seals(self):
        self._assert_persisted_adverse_stop_refusal(
            "adverse-unknown-side", "UNKNOWN_SIDE",
            field="side", value="SIDEWAYS",
        )

    def test_max_adverse_stop_runs_while_stop_seals_entries(self):
        quantity = self.open_momentum("adverse-stop-open")
        Path(os.path.join(self.receipt_dir, "STOP")).touch()
        self.queue_momentum_measurement(quantity, mark_price="196")
        self.queue_momentum_measurement(0)

        with self.momentum_http_runtime():
            self.assertEqual(self.tick(), 0)

        self.assertEqual(self.momentum_intents[-1]["reduceOnly"], "true")
        self.assertEqual(self.momentum_state()["state"], "CLOSED")
        self.assertEqual(
            self.reason(self.results()[-1]), "max_adverse_stop"
        )

    def test_momentum_exit_uses_measured_qty_and_records_realized_fees(self):
        quantity = self.open_momentum("fee-open")
        self.job(
            "fee-exit", op="MOMENTUM_EXIT", max_notional=0.5
        )
        self.queue_momentum_measurement(quantity)
        self.queue_momentum_measurement(0)
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 0)

        close = self.momentum_intents[-1]
        self.assertEqual(close["side"], "SELL")
        self.assertEqual(close["reduceOnly"], "true")
        self.assertEqual(close["quantity"], "0.03")
        self.assertNotEqual(close["quantity"], "0.5")
        state = self.momentum_state()
        self.assertEqual(state["state"], "CLOSED")
        self.assertEqual(state["measured_qty"], "0")
        self.assertTrue(state["exit"]["reduce_only"])

        row = self.results()[-1]
        self.assertEqual(row["verdict"], "CLOSED")
        self.assertEqual(row["fills"][0]["realized_pnl"], "0.25")
        self.assertEqual(row["fees"], [{
            "trade_id": "92",
            "order_id": "2202",
            "commission": "0.002",
            "commission_asset": "USDT",
        }])
        self.assertEqual(row["realized"]["gross_usdt"], "0.25")
        self.assertEqual(row["realized"]["net_usdt"], "0.245")
        self.assertEqual(
            row["realized"]["commission_by_asset"], {"USDT": "0.005"}
        )
        self.assertEqual(self.governor_realized, [(
            Decimal("0.245"), Decimal("0.005"), self.receipt_dir,
        )])

    def test_momentum_exit_accepts_sparse_account_flat_response(self):
        quantity = self.open_momentum("sparse-open")
        self.job("sparse-exit", op="MOMENTUM_EXIT")
        self.queue_momentum_measurement(quantity)
        self.queue_momentum_measurement(0)

        with self.momentum_runtime():
            self.assertEqual(self.tick(), 0)

        self.assertEqual(self.results()[-1]["verdict"], "CLOSED")
        self.assertEqual(self.momentum_state()["state"], "CLOSED")
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_momentum_measure_enforces_exchange_truth_guards(self):
        cases = [
            (
                "disagreement",
                [self.momentum_row("0.03")],
                [self.momentum_row("0.02")],
                1,
                "momentum exchange state is unreadable",
            ),
            (
                "hedge side",
                [self.momentum_row("0.03", "LONG")],
                [self.momentum_row("0.03")],
                1,
                "momentum exchange state is unreadable",
            ),
            (
                "short",
                [self.momentum_row("-0.03")],
                [self.momentum_row("-0.03")],
                1,
                "momentum exchange state is unproved",
            ),
            (
                "duplicate exposure",
                [self.momentum_row("0.01"), self.momentum_row("0.02")],
                [self.momentum_row("0.03")],
                1,
                "momentum exchange state is unreadable",
            ),
            (
                "leverage",
                [self.momentum_row("0.03")],
                [self.momentum_row("0.03")],
                2,
                "momentum exchange state is unproved",
            ),
        ]
        for name, risks, positions, leverage, reason in cases:
            with self.subTest(name=name):
                self.momentum_reads.queue(
                    risks, positions, leverage=leverage
                )
                posts = len(self.momentum_intents)
                with self.assertRaisesRegex(jr.MomentumStateError, reason):
                    jr._futures_measure(
                        "TSTUSDT", self.execution_services
                    )
                self.assertEqual(len(self.momentum_intents), posts)

    def test_momentum_exit_refuses_unmeasured_quantity(self):
        quantity = self.open_momentum("measure-open")
        before_intents = len(self.momentum_intents)
        self.job("measure-exit", op="MOMENTUM_EXIT")
        self.queue_momentum_measurement(quantity / 2)
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 4)

        self.assertEqual(len(self.momentum_intents), before_intents)
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))
        self.assertEqual(
            self.reason(self.results()[-1]),
            "MOMENTUM_POSITION_MEASUREMENT_MISMATCH",
        )

    def test_momentum_exit_does_not_claim_closed_until_measured_flat(self):
        quantity = self.open_momentum("not-flat-open")
        self.job("not-flat-exit", op="MOMENTUM_EXIT")
        self.queue_momentum_measurement(quantity)
        self.queue_momentum_measurement("0.001")
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 3)

        self.assertEqual(self.results()[-1]["verdict"], "OPEN_EXPOSURE")
        self.assertEqual(
            self.reason(self.results()[-1]), "MOMENTUM_EXIT_NOT_FLAT"
        )
        self.assertEqual(self.momentum_state()["state"], "UNKNOWN")
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_unknown_momentum_open_persists_intent_and_freezes(self):
        self.momentum_markers()
        self.job("pending-open", op="MOMENTUM_OPEN")
        self.momentum_query_state = "pending"
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 4)

        state = self.momentum_state()
        self.assertEqual(state["state"], "UNKNOWN")
        self.assertEqual(state["pending_intent"]["action"], "OPEN")
        self.assertEqual(self.results()[0]["verdict"], "UNKNOWN")
        self.job("after-unknown", op="MOMENTUM_OPEN")
        self.assertEqual(self.tick(), 3)
        self.assertEqual(len(self.momentum_intents), 1)

    def test_momentum_open_exchange_cap_breach_freezes(self):
        self.momentum_markers()
        self.job("cap-breach", op="MOMENTUM_OPEN", max_notional=6)
        self.open_quote_override = "6.0001"
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 3)
        self.assertEqual(self.results()[0]["verdict"], "OPEN_EXPOSURE")
        self.assertEqual(
            self.reason(self.results()[0]), "MOMENTUM_OPEN_HARD_CAP_BREACH"
        )
        self.assertEqual(self.momentum_state()["state"], "UNKNOWN")

    def test_crash_after_intent_before_post_recovers_query_only(self):
        self.momentum_markers()
        self.job("crash-open", op="MOMENTUM_OPEN")

        def crash_submit(_transport, _base_url, plan, writer, **_kwargs):
            writer(plan.intent)
            raise KeyboardInterrupt

        crashed = replace(
            self.execution_services,
            futures_submit_order_once=crash_submit,
        )
        with self.assertRaises(KeyboardInterrupt):
            self.tick(execution_services=crashed)

        pending = self.momentum_state()
        self.assertEqual(pending["state"], "OPENING")
        client_id = pending["pending_intent"]["client_id"]
        governor_token = pending["pending_intent"]["governor_token"]
        self.assertTrue(governor_token)
        self.assertEqual(self.results(), [])
        self.assertFalse(os.path.exists(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))
        Path(os.path.join(self.receipt_dir, "STOP")).touch()

        queried = []

        def query_only(_transport, _base_url, symbol, value, **_kwargs):
            queried.append(value)
            identity = jr.binance_http.OrderIdentity(symbol, value, None)
            return jr.binance_http.OrderResult(
                "not_found", identity, None, None, False, None, None,
                None, None, (), None,
            )

        posts = []

        def no_post(*_args, **_kwargs):
            posts.append(True)
            raise AssertionError("recovery reached a submit service")

        self.queue_momentum_measurement(0)
        recovery = replace(
            self.execution_services,
            futures_query_order=query_only,
            futures_submit_order_once=no_post,
        )
        self.assertEqual(self.tick(execution_services=recovery), 2)
        self.assertEqual(posts, [])
        self.assertEqual(queried, [client_id])
        self.assertEqual(self.momentum_state()["state"], "NONE")
        self.assertEqual(self.results()[0]["verdict"], "REFUSED")
        self.assertEqual(
            self.reason(self.results()[0]), "MOMENTUM_OPEN_NOT_ACCEPTED"
        )
        self.assertEqual(
            self.governor_releases,
            [(governor_token, self.receipt_dir)],
        )

    def test_closed_futures_writes_proof_marker(self):
        self.job("proof")
        self.assertEqual(self.tick(), 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.jobs_dir, "futures_proof_passed"
        )))

    def test_results_line_has_exact_required_shape(self):
        self.job("shape")
        self.assertEqual(self.tick(), 0)
        row = self.results()[0]
        self.assertEqual(set(row), {
            "job_id", "op", "symbol", "verdict", "exit_code",
            "receipt_path", "utc",
        })
        self.assertEqual(row["job_id"], "shape")
        self.assertEqual(row["op"], "FUTURES_ROUNDTRIP")
        self.assertEqual(row["symbol"], "TSTUSDT")
        self.assertEqual(row["verdict"], "CLOSED")
        self.assertEqual(row["exit_code"], 0)
        self.assertTrue(os.path.isfile(row["receipt_path"]))


class SpotMomentumJobRunnerTests(unittest.TestCase):
    """Spot-only momentum uses the proven basis spot leg, entirely offline."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.receipt_dir = temporary.name
        self.jobs_dir = os.path.join(self.receipt_dir, "jobs")
        os.makedirs(self.jobs_dir)
        Path(os.path.join(
            self.jobs_dir, "futures_proof_passed"
        )).touch()
        Path(os.path.join(
            self.jobs_dir, "basis_proof_passed"
        )).touch()
        with open(os.path.join(
                self.receipt_dir, "OPERATOR"
        ), "w", encoding="utf-8") as handle:
            json.dump({"operator": "lead"}, handle)
        self.write_accept_one()

        self.base_free = Decimal("5")
        self.quote_free = Decimal("100")
        self.spot_bid = Decimal("99")
        self.spot_ask = Decimal("100")
        self.posts = []
        self.orders = {}
        self.unknown_submission = False
        self.governor_acquires = []
        self.governor_releases = []
        self.hand_calls = []
        self.hand_verdict = "CLOSED"
        self.events = []
        self.momentum_intents = []
        self.momentum_query_state = "terminal"
        self.momentum_http_calls = []
        self.momentum_order = None
        self.open_quote_override = None
        self.leverage = LeverageTransport(self.events)
        self.futures = FuturesTransport(self, self.leverage)
        self.execution_services = execution_services(
            ExecutionTransport(self.spot_request, self.futures)
        )

        class FakeGovernorRefusal(Exception):
            def __init__(inner_self, reason):
                super().__init__(reason)
                inner_self.reason = reason

        governor = types.ModuleType("account_governor")
        governor.GovernorRefusal = FakeGovernorRefusal
        governor.budgets_allow = lambda receipt_dir: (True, "")

        def acquire(op, symbol, receipt_dir):
            self.governor_acquires.append((op, symbol, receipt_dir))
            return "spot-governor-token"

        def release(token, receipt_dir):
            self.governor_releases.append((token, receipt_dir))

        governor.acquire_entry_slot = acquire
        governor.release_entry_slot = release
        governor.record_realized = lambda *_args: None
        governor_patch = mock.patch.dict(
            sys.modules, {"account_governor": governor}
        )
        governor_patch.start()
        self.addCleanup(governor_patch.stop)

        env = mock.patch.dict(os.environ, {
            "RECEIPT_DIR": self.receipt_dir,
            "QUANT_SHA": SHA,
            "BINANCE_API_KEY": "offline-key",
            "BINANCE_API_SECRET": "offline-secret",
            "MAX_SLIPPAGE_BPS": "0",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)

        def deny(*_args, **_kwargs):
            raise AssertionError("spot momentum test attempted network")

        for target, name in ((urllib.request, "urlopen"),
                             (socket, "socket"),
                             (socket, "create_connection")):
            patcher = mock.patch.object(target, name, deny)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_accept_one(self, maximum="32"):
        path = os.path.join(self.receipt_dir, "ACCEPT-ONE")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({
                "operator": "lead",
                "created_utc": utc(NOW - timedelta(minutes=1)),
                "expires_utc": utc(NOW + timedelta(minutes=10)),
                "max_notional": maximum,
            }, handle)

    def job(self, job_id, op="MOMENTUM_OPEN", maximum=20, spot=False):
        payload = {
            "job_id": job_id,
            "op": op,
            "symbol": "TSTUSDT",
            "max_notional": maximum,
            "created_utc": utc(NOW - timedelta(minutes=1)),
            "expires_utc": utc(NOW + timedelta(minutes=10)),
            "requested_by": "lead",
        }
        if spot:
            payload["venue"] = "spot"
        with open(os.path.join(
                self.jobs_dir, "job-%s.json" % job_id
        ), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    @staticmethod
    def spot_info():
        return {
            "symbol": "TSTUSDT",
            "status": "TRADING",
            "isSpotTradingAllowed": True,
            "baseAsset": "TST",
            "quoteAsset": "USDT",
            "baseAssetPrecision": 8,
            "quoteAssetPrecision": 8,
            "orderTypes": ["MARKET"],
            "quoteOrderQtyMarketAllowed": True,
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "stepSize": "0.001",
                    "minQty": "0.001",
                },
                {
                    "filterType": "MARKET_LOT_SIZE",
                    "stepSize": "0.001",
                    "minQty": "0.001",
                },
                {
                    "filterType": "MIN_NOTIONAL",
                    "minNotional": "5",
                    "applyToMarket": True,
                },
            ],
        }

    def spot_request(self, method, base_url, path, params=None,
                     signed=False):
        self.assertEqual(base_url, jr.binance_http.SPOT_BASE_URL)
        params = dict(params or {})
        if method == "GET" and path == "/api/v3/exchangeInfo":
            self.assertFalse(signed)
            return jr.binance_http.HttpResult(
                "ok", {"symbols": [self.spot_info()]}, None, None
            )
        if method == "GET" and path == "/api/v3/ticker/bookTicker":
            self.assertFalse(signed)
            return jr.binance_http.HttpResult("ok", {
                "symbol": "TSTUSDT",
                "bidPrice": str(self.spot_bid),
                "askPrice": str(self.spot_ask),
            }, None, None)
        if method == "GET" and path == "/api/v3/account":
            self.assertTrue(signed)
            return jr.binance_http.HttpResult("ok", {
                "canTrade": True,
                "balances": [
                    {"asset": "TST", "free": str(self.base_free),
                     "locked": "0"},
                    {"asset": "USDT", "free": str(self.quote_free),
                     "locked": "0"},
                ],
            }, None, None)
        if method == "GET" and path == "/api/v3/openOrders":
            self.assertTrue(signed)
            return jr.binance_http.HttpResult("ok", [], None, None)
        if method == "POST" and path == "/api/v3/order":
            self.assertTrue(signed)
            self.posts.append(params)
            if self.unknown_submission:
                return jr.binance_http.HttpResult(
                    "unclear", None, None, "offline submission"
                )
            side = params["side"]
            if side == "BUY":
                quote = Decimal(params["quoteOrderQty"])
                quantity = quote / self.spot_ask
                commission = Decimal("0.001")
                commission_asset = "TST"
                price = self.spot_ask
                self.base_free += quantity - commission
                self.quote_free -= quote
            else:
                quantity = Decimal(params["quantity"])
                price = self.spot_bid
                quote = quantity * price
                commission = Decimal("0.001")
                commission_asset = "USDT"
                self.base_free -= quantity
                self.quote_free += quote - commission
            order_id = len(self.posts)
            order = {
                "symbol": "TSTUSDT",
                "status": "FILLED",
                "side": side,
                "clientOrderId": params["newClientOrderId"],
                "orderId": order_id,
                "executedQty": str(quantity),
                "cummulativeQuoteQty": str(quote),
            }
            fill = {
                "tradeId": order_id, "orderId": order_id,
                "qty": str(quantity),
                "price": str(price),
                "commission": str(commission),
                "commissionAsset": commission_asset,
                "isBuyer": side == "BUY", "isMaker": False,
                "time": int(NOW.timestamp() * 1000),
            }
            self.orders[params["newClientOrderId"]] = dict(order)
            return jr.binance_http.HttpResult(
                "ok", {**order, "fills": [fill]}, None, None
            )
        if method == "GET" and path == "/api/v3/order":
            self.assertTrue(signed)
            if self.unknown_submission:
                return jr.binance_http.HttpResult(
                    "api_error", None, -1003, "offline"
                )
            return jr.binance_http.HttpResult(
                "ok", dict(self.orders[params["origClientOrderId"]]),
                None, None,
            )
        raise AssertionError("unexpected spot request %s %s" % (
            method, path,
        ))

    def tick(self, **kwargs):
        return jr.tick(
            receipt_dir=self.receipt_dir,
            now=NOW,
            execution_services=self.execution_services,
            **kwargs,
        )

    def state(self):
        with open(os.path.join(
                self.receipt_dir, "momentum-position.json"
        ), encoding="utf-8") as handle:
            return json.load(handle)

    def results(self):
        with open(os.path.join(
                self.jobs_dir, "results.jsonl"
        ), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def open_spot(self, job_id="spot-open"):
        self.job(job_id, spot=True)
        self.assertEqual(self.tick(), 0)

    def test_spot_open_routes_proven_leg_through_ticket_governor_and_clamp(self):
        self.open_spot()

        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["side"], "BUY")
        self.assertEqual(self.posts[0]["quoteOrderQty"], "8")
        self.assertEqual(self.governor_acquires, [(
            "MOMENTUM_OPEN", "TSTUSDT", self.receipt_dir,
        )])
        self.assertEqual(self.governor_releases, [(
            "spot-governor-token", self.receipt_dir,
        )])
        self.assertFalse(os.path.exists(os.path.join(
            self.receipt_dir, "ACCEPT-ONE"
        )))
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "ACCEPT-ONE.claimed-spot-open"
        )))
        state = self.state()
        self.assertEqual(state["venue"], "spot")
        self.assertEqual(state["state"], "OPEN")
        self.assertEqual(state["measured_qty"], "0.079")

    def test_default_momentum_open_remains_on_futures_path(self):
        self.job("futures-default", maximum=6)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.state()["venue"], "futures")
        self.assertEqual(self.results()[0]["verdict"], "OPEN")
        self.assertEqual(self.posts, [])

    def test_spot_exit_sells_only_net_acquired_not_existing_balance(self):
        self.open_spot("spot-net-open")
        self.job("spot-net-exit", op="MOMENTUM_EXIT", maximum=6)

        self.assertEqual(self.tick(), 0)

        self.assertEqual(len(self.posts), 2)
        sell = self.posts[1]
        self.assertEqual(sell["side"], "SELL")
        self.assertEqual(sell["quantity"], "0.079")
        self.assertNotEqual(sell["quantity"], "5.079")
        self.assertEqual(self.base_free, Decimal("5.000"))
        state = self.state()
        self.assertEqual(state["state"], "CLOSED")
        self.assertEqual(state["exit"]["submitted_qty"], "0.079")

    def test_spot_adverse_stop_uses_the_same_net_acquired_exit(self):
        self.open_spot("spot-adverse-open")
        self.spot_bid = Decimal("97")
        self.spot_ask = Decimal("98")

        self.assertEqual(self.tick(), 0)

        self.assertEqual([row["side"] for row in self.posts], ["BUY", "SELL"])
        self.assertEqual(self.posts[-1]["quantity"], "0.079")
        self.assertEqual(self.state()["state"], "CLOSED")
        self.assertFalse(os.path.exists(os.path.join(
            self.receipt_dir, "first_autonomous_close_passed"
        )))

    def test_spot_unknown_submission_freezes_without_retry(self):
        self.unknown_submission = True
        self.job("spot-unknown", spot=True)

        self.assertEqual(self.tick(), 4)

        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.state()["state"], "UNKNOWN")
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "ACCEPT-ONE"
        )))
        self.assertEqual(self.governor_releases, [])
        self.assertEqual(self.results()[0]["verdict"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
