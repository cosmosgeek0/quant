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


def utc(value):
    return value.isoformat().replace("+00:00", "Z")


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
                "stepSize": "0.001",
                "minQty": "0.001",
                "maxQty": "1000",
            },
            {
                "filterType": "MARKET_LOT_SIZE",
                "stepSize": "0.001",
                "minQty": "0.001",
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
        call = (method, path, dict(params or {}), signed)
        self.calls.append(call)
        self.events.append(("leverage", call))
        if method == "GET" and path == "/fapi/v1/symbolConfig":
            symbol = (params or {}).get("symbol", "")
            return "ok", [{"symbol": symbol, "leverage": str(self.current)}]
        if method == "POST" and path == "/fapi/v1/leverage" \
                and (params or {}).get("leverage") == "1":
            self.current = 1
            return "ok", {"leverage": 1}
        raise AssertionError("unexpected leverage transport call")


class MarginTransport:
    def __init__(self):
        self.calls = []
        self.operation = None
        self.post_kind = "ok"
        self.post_payload = {"tranId": 7301}
        self.history_kind = "ok"
        self.history_payload = None
        self.account_kind = "ok"
        self.account_before_payload = None
        self.account_payload = None

    def __call__(self, method, base_url, path, params=None, signed=False):
        params = dict(params or {})
        call = (method, base_url, path, params, signed)
        self.calls.append(call)
        if base_url != jr.basis_executor.SPOT.base_url or not signed:
            raise AssertionError("margin request was not a signed Spot call")
        if method == "POST" and path == jr.MARGIN_ENDPOINT_PATH:
            self.operation = dict(params)
            return self.post_kind, self.post_payload
        if method == "GET" and path == jr.MARGIN_ENDPOINT_PATH:
            if self.history_payload is not None:
                return self.history_kind, self.history_payload
            transaction_id = self.post_payload.get(
                "tranId", self.post_payload.get("txId", "")
            )
            return self.history_kind, {"rows": [{
                "txId": transaction_id,
                "asset": self.operation["asset"],
                "principal": self.operation["amount"],
                "status": "CONFIRMED",
                "clientTag": "",
                "type": "MANUAL",
            }]}
        if method == "GET" and path == jr.MARGIN_ACCOUNT_PATH:
            if self.operation is None:
                if self.account_before_payload is not None:
                    return self.account_kind, self.account_before_payload
                return self.account_kind, {"userAssets": [{
                    "asset": "USDT",
                    "borrowed": "0",
                    "interest": "0",
                }]}
            if self.account_payload is not None:
                return self.account_kind, self.account_payload
            borrowed = self.operation["amount"] \
                if self.operation["type"] == "BORROW" else "0"
            return self.account_kind, {"userAssets": [{
                "asset": self.operation["asset"],
                "borrowed": borrowed,
                "interest": "0",
            }]}
        raise AssertionError("unexpected margin request")


class MomentumReadTransport:
    """Serve complete signed momentum snapshots at the HTTP boundary."""

    def __init__(self, events):
        self.calls = []
        self.events = events
        self.snapshots = []
        self.active = None
        self.position_failures = []

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

    def __call__(self, method, path, params=None, signed=False):
        call = (method, path, dict(params or {}), signed)
        self.calls.append(call)
        self.events.append(("momentum-read", call))
        if method != "GET" or not signed:
            raise AssertionError("momentum truth read was not a signed GET")
        if path == "/fapi/v3/positionRisk" and self.position_failures:
            return self.position_failures.pop(0)
        if path == "/fapi/v3/positionRisk":
            if self.active is not None or not self.snapshots:
                raise AssertionError("unexpected momentum snapshot start")
            self.active = self.snapshots.pop(0)
        if self.active is None or path not in self.active:
            raise AssertionError("unexpected momentum truth read: " + path)
        payload = self.active[path]
        if path == "/fapi/v1/symbolConfig":
            self.active = None
        return "ok", payload


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
        self.momentum_reads = MomentumReadTransport(self.events)
        self.momentum_intents = []
        self.momentum_order = None
        self.momentum_plan_caps = []
        self.momentum_query_state = "terminal"
        self.momentum_http_calls = []
        self.momentum_preflight = False
        self.open_quote_override = None
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
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)

        factory = mock.patch.object(
            jr.set_leverage_1x, "live_request_factory",
            return_value=self.leverage,
        )
        factory.start()
        self.addCleanup(factory.stop)

        futures = mock.patch.object(jr.futures_hand, "main", self.fake_futures)
        futures.start()
        self.addCleanup(futures.stop)
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
                value["client_tag"] = jr.margin_ops.deterministic_client_tag(
                    value["op"], value["asset"], value["principal"],
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

    def fake_futures(self):
        index = len(self.hand_calls) + 1
        self.events.append(("futures", list(sys.argv)))
        self.hand_calls.append({
            "argv": list(sys.argv),
            "symbol": os.environ.get("SYMBOL"),
            "max_notional": os.environ.get("MAX_NOTIONAL"),
            "sha": os.environ.get("QUANT_SHA"),
            "module_symbol": jr.futures_hand.SYMBOL,
            "module_max_notional": jr.futures_hand.MAX_NOTIONAL_RAW,
        })
        path = os.path.join(
            self.receipt_dir, "futures-hand-offline%s.json" % index
        )
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"verdict": self.hand_verdict}, handle)
        jr.futures_hand.receipt_path = path
        raise SystemExit(self.hand_exit)

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
        return jr.tick(receipt_dir=self.receipt_dir, now=NOW, **kwargs)

    def margin_tick(self, mutation_now=NOW, request=None):
        transport = request or self.margin
        transport.operation = None
        with mock.patch.object(jr, "_utc_now", return_value=mutation_now):
            return self.tick(margin_request=transport)

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

    def fake_momentum_plan(self, effective):
        self.momentum_plan_caps.append(effective)
        return {
            "quantity": effective / Decimal("200"),
            "limit_price": Decimal("200"),
            "current_leverage": 1,
        }

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

    def fake_momentum_submit(self, intent):
        self.momentum_intents.append(dict(intent))
        if self.momentum_query_state != "terminal":
            return self.momentum_query_state, None, [], "unclear"
        close = str(intent["reduceOnly"]).lower() == "true"
        quantity = Decimal(intent["quantity"])
        price = Decimal("205") if close else Decimal("200")
        quote = quantity * price
        if not close and self.open_quote_override is not None:
            quote = Decimal(self.open_quote_override)
        order_id = "2202" if close else "1101"
        commission = Decimal("0.002") if close else Decimal("0.003")
        realized = Decimal("0.25") if close else Decimal("0")
        order = {
            "status": "FILLED",
            "clientOrderId": intent["newClientOrderId"],
            "orderId": order_id,
            "executedQty": str(quantity),
            "cumQuote": str(quote),
            "avgPrice": str(price),
            "reduceOnly": close,
        }
        fills = [{
            "id": "92" if close else "91",
            "orderId": order_id,
            "qty": str(quantity),
            "price": str(price),
            "commission": str(commission),
            "commissionAsset": "USDT",
            "realizedPnl": str(realized),
        }]
        return "terminal", order, fills, "ok"

    def momentum_http(self, method, path, params=None, signed=False):
        call = (method, path, dict(params or {}), signed)
        self.momentum_http_calls.append(call)
        if method == "POST" and path == "/fapi/v1/order" and signed:
            query_state, order, fills, kind = self.fake_momentum_submit(params)
            self.assertEqual(query_state, "terminal")
            self.momentum_order = dict(order)
            response = dict(order)
            response["fills"] = list(fills)
            return kind, response
        if method == "GET" and path == "/fapi/v1/order" and signed:
            self.assertIsNotNone(self.momentum_order)
            self.assertEqual(
                (params or {}).get("origClientOrderId"),
                self.momentum_order["clientOrderId"],
            )
            return "ok", dict(self.momentum_order)
        if method == "GET" and path == "/fapi/v1/time" and not signed:
            self.momentum_preflight = True
            return "ok", {"serverTime": 1786708800000}
        if self.momentum_preflight:
            preflight = {
                "/fapi/v1/exchangeInfo": {
                    "symbols": [momentum_symbol_info()],
                },
                "/fapi/v1/ticker/bookTicker": {
                    "bidPrice": "199.99",
                    "askPrice": "200",
                },
                "/fapi/v1/premiumIndex": {"markPrice": "200"},
                "/fapi/v1/accountConfig": {"canTrade": True},
                "/fapi/v3/account": {
                    "availableBalance": "100",
                    "positions": [],
                },
                "/fapi/v1/positionSide/dual": {
                    "dualSidePosition": False,
                },
                "/fapi/v1/openOrders": [],
                "/fapi/v3/positionRisk": [],
                "/fapi/v1/symbolConfig": [{
                    "symbol": "TSTUSDT",
                    "leverage": "1",
                    "marginType": "CROSSED",
                }],
            }
            if method != "GET" or path not in preflight:
                raise AssertionError(
                    "unexpected momentum preflight request: %s %s" % (
                        method, path,
                    )
                )
            if path == "/fapi/v1/symbolConfig":
                self.momentum_preflight = False
            return "ok", preflight[path]
        return self.momentum_reads(method, path, params, signed)

    @contextmanager
    def momentum_runtime(self):
        with mock.patch.object(
                jr, "_momentum_open_plan",
                side_effect=self.fake_momentum_plan), \
                mock.patch.object(
                    jr.futures_hand, "http", self.momentum_reads), \
                mock.patch.object(
                    jr, "_momentum_submit",
                    side_effect=self.fake_momentum_submit):
            yield

    @contextmanager
    def momentum_http_runtime(self):
        with mock.patch.object(
                jr.futures_hand, "http", side_effect=self.momentum_http), \
                mock.patch.object(
                    jr.futures_hand.time, "sleep", return_value=None
                ):
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
        self.queue_momentum_measurement(effective / Decimal("200"))
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
        self.queue_momentum_measurement(quantity)
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
        self.assertEqual(self.hand_calls[0]["argv"], [
            "futures_hand.py", "--short", "--execute"
        ])
        self.assertEqual(self.hand_calls[0]["max_notional"], "8")
        self.assertEqual(self.hand_calls[0]["module_max_notional"], "8")
        self.assertEqual(self.hand_calls[0]["sha"], SHA)
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
            code = self.tick(margin_request=self.margin)

        self.assertEqual(code, 2)
        self.assertEqual(
            [call.args[2] for call in builder.call_args_list],
            [NOW, NOW, boundary],
        )
        self.assertEqual(self.margin.calls, [])
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
        self.assertEqual(self.margin.calls, [])
        result = self.results()[0]
        self.assertEqual(self.reason(result), "ACCEPT_ONE_INVALID")
        source = self.margin_source(result)
        self.assertEqual(source["mutation_attempts"], 0)
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
        self.assertEqual(self.margin.calls, [])
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
        persist = jr._persist_margin_source

        def seal_after_create(path, payload, create=False):
            persist(path, payload, create=create)
            if create:
                Path(os.path.join(
                    self.receipt_dir, "STOP-JOBS"
                )).touch()

        with mock.patch.object(
                jr, "_persist_margin_source",
                side_effect=seal_after_create):
            code = self.margin_tick()

        self.assertEqual(code, 2)
        self.assertEqual(self.margin.calls, [])
        result = self.results()[0]
        self.assertEqual(self.reason(result), "STOP_JOBS_SEALED")
        self.assertEqual(
            self.margin_source(result)["mutation_attempts"], 0
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
        self.assertEqual(self.margin.calls, [])
        result = self.results()[0]
        self.assertEqual(
            self.reason(result), "MARGIN_VALIDATION_UNKNOWN"
        )
        source = self.margin_source(result)
        self.assertEqual(source["mutation_attempts"], 0)
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

    def test_margin_history_confirms_real_binance_row_shape(self):
        expected = {
            "asset": "USDT",
            "principal": "2.5",
            "interest": "0",
            "status": "BORROW_CONFIRMED",
        }
        real_row = {
            "txId": 401344857108,
            "asset": "USDT",
            "principal": "2.5",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "MANUAL",
        }

        self.assertEqual(
            jr._margin_history_confirmation(
                {"rows": [real_row]}, "401344857108", expected, "BORROW"
            ),
            expected,
        )
        self.assertEqual(
            jr._margin_history_confirmation(
                {"rows": [dict(real_row, type="AUTO")]},
                "401344857108", expected, "BORROW",
            ),
            expected,
        )

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
        self.assertEqual(len(history_calls), 1)
        self.assertEqual(history_calls[0][3], {
            "asset": "USDT",
            "type": "BORROW",
            "txId": "401344857108",
        })

    def test_margin_history_rejects_target_identity_conflicts_and_duplicates(
            self):
        expected = {
            "asset": "USDT",
            "principal": "3",
            "interest": "0",
            "status": "BORROW_CONFIRMED",
        }
        valid = {
            "txId": "7301",
            "asset": "USDT",
            "principal": "3",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "MANUAL",
        }
        cases = {
            "conflict": [
                dict(valid),
                dict(valid, tranId="8301"),
            ],
            "duplicate": [dict(valid), dict(valid)],
        }
        for name, rows in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(jr._margin_history_confirmation(
                    {"rows": rows}, "7301", expected, "BORROW"
                ))

    def test_margin_history_rejects_near_miss_and_non_exact_identities(self):
        expected = {
            "asset": "USDT",
            "principal": "3",
            "interest": "0",
            "status": "BORROW_CONFIRMED",
        }
        valid = {
            "txId": "7301",
            "asset": "USDT",
            "principal": "3",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "MANUAL",
        }
        cases = {
            "near_miss": [dict(valid, tranID="7301")],
            "float_shadow": [dict(valid), dict(valid, txId=7301.0)],
            "boolean": [dict(valid, txId=True)],
            "nested_conflict": [dict(
                valid, details={"events": [{"tranId": "8301"}]}
            )],
            "normalized_key": [dict(valid, **{"ｔｒａｎＩｄ": "8301"})],
        }
        for name, rows in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(jr._margin_history_confirmation(
                    {"rows": rows}, "7301", expected, "BORROW"
                ))
        self.assertEqual(jr._margin_transaction_id({"tranId": 7301}), "7301")
        self.assertEqual(jr._margin_transaction_id({
            "tranId": 7301, "txId": "7301"
        }), "7301")
        self.assertEqual(jr._margin_transaction_id({
            "tranId": 7301,
            "detail": {"events": [{"txId": "7301"}]},
        }), "7301")
        self.assertEqual(jr._margin_transaction_id({
            "tranId": 7301, "detail": {"txId": "8301"}
        }), "")
        self.assertEqual(jr._margin_transaction_id({"tranId": 7301.0}), "")
        self.assertEqual(jr._margin_transaction_id({"tranId": True}), "")

    def test_margin_identity_scan_enforces_node_and_byte_bounds(self):
        within_nodes = {
            "tranId": "7301",
            "items": [None] * (jr.MARGIN_IDENTITY_MAX_NODES - 3),
        }
        over_nodes = {
            "tranId": "7301",
            "items": [None] * (jr.MARGIN_IDENTITY_MAX_NODES - 2),
        }
        self.assertEqual(
            jr._margin_identity_aliases(within_nodes), ["7301"]
        )
        self.assertIsNone(jr._margin_identity_aliases(over_nodes))

        empty = {"tranId": "7301", "padding": ""}
        overhead = len(json.dumps(
            empty, ensure_ascii=True, separators=(",", ":")
        ).encode("utf-8"))
        exact_bytes = dict(
            empty,
            padding="x" * (
                jr.MARGIN_IDENTITY_MAX_RESPONSE_BYTES - overhead
            ),
        )
        over_bytes = dict(
            exact_bytes, padding=exact_bytes["padding"] + "x"
        )
        self.assertEqual(
            jr._margin_identity_aliases(exact_bytes), ["7301"]
        )
        self.assertIsNone(jr._margin_identity_aliases(over_bytes))

    def test_margin_history_requires_the_exact_type_field(self):
        expected = {
            "asset": "USDT",
            "principal": "3",
            "interest": "0",
            "status": "BORROW_CONFIRMED",
        }
        without_type = {
            "txId": "7301",
            "asset": "USDT",
            "principal": "3",
            "interest": "0",
            "status": "CONFIRMED",
        }
        with_near_miss = dict(without_type, typ="MANUAL")
        for row in (without_type, with_near_miss):
            self.assertIsNone(jr._margin_history_confirmation(
                {"rows": [row]}, "7301", expected, "BORROW"
            ))

    def test_margin_history_refuses_unknown_origin_type(self):
        expected = {
            "asset": "USDT",
            "principal": "2.5",
            "interest": "0",
            "status": "BORROW_CONFIRMED",
        }
        row = {
            "txId": 401344857108,
            "asset": "USDT",
            "principal": "2.5",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "BORROW",
        }

        self.assertIsNone(jr._margin_history_confirmation(
            {"rows": [row]}, "401344857108", expected, "BORROW"
        ))

    def test_margin_confirmation_readers_are_total_for_non_text_fields(self):
        expected = {
            "asset": "USDT",
            "principal": "2.5",
            "interest": "0",
            "status": "BORROW_CONFIRMED",
        }
        row = {
            "txId": "7301",
            "asset": "USDT",
            "principal": "2.5",
            "status": "CONFIRMED",
            "clientTag": "",
            "type": "MANUAL",
        }
        row_cases = (
            dict(row, asset=[]),
            dict(row, principal={}),
            dict(row, status=[]),
            dict(row, type=[]),
            dict(row, type={}),
        )
        for candidate in row_cases:
            with self.subTest(history_field=next(
                    name for name in row if candidate[name] != row[name]
            )):
                self.assertIsNone(jr._margin_history_confirmation(
                    {"rows": [candidate]}, "7301", expected, "BORROW"
                ))
        for field, value in (
                ("asset", []), ("principal", {}),
                ("interest", []), ("status", {})):
            with self.subTest(expected_field=field):
                self.assertIsNone(jr._margin_history_confirmation(
                    {"rows": [row]}, "7301",
                    dict(expected, **{field: value}), "BORROW",
                ))
        self.assertIsNone(jr._margin_history_confirmation(
            {"rows": [row]}, [], expected, "BORROW"
        ))
        self.assertIsNone(jr._margin_history_confirmation(
            {"rows": [row]}, "7301", expected, {}
        ))

        liability = {
            "asset": "USDT", "borrowed": "2.5", "interest": "0",
        }
        for field, value in (
                ("asset", []), ("borrowed", {}), ("interest", [])):
            with self.subTest(account_field=field):
                self.assertIsNone(jr._margin_account_liability(
                    {"userAssets": [dict(liability, **{field: value})]},
                    "USDT",
                ))
        self.assertIsNone(jr._margin_account_liability(
            {"userAssets": [liability]}, []
        ))

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
            self.reason(result), "MARGIN_CONFIRMATION_MISMATCH"
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

        with mock.patch.object(
                jr, "_margin_history_confirmation",
                side_effect=RuntimeError("offline confirmation check")):
            self.assertEqual(self.margin_tick(), 4)

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
            if call[0] == "GET" and call[2] == jr.MARGIN_ACCOUNT_PATH
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
            self.reason(self.results()[0]), "MARGIN_CONFIRMATION_MISMATCH"
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
                        principal="0.01")

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

        self.assertEqual(self.margin_tick(), 0)

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
        self.assertEqual(self.hand_calls[0]["argv"], [
            "futures_hand.py", "--execute"
        ])

    def test_momentum_uses_long_path_clamped_cap_after_forcing_one_x(self):
        Path(os.path.join(self.jobs_dir, "futures_proof_passed")).touch()
        Path(os.path.join(self.jobs_dir, "basis_proof_passed")).touch()
        self.leverage.current = 5
        self.write_accept_one(max_notional="800")
        self.job(
            "momentum-clamped", op="MOMENTUM_ROUNDTRIP", max_notional=800
        )

        self.assertEqual(self.tick(), 0)

        self.assertEqual(self.hand_calls[0]["argv"], [
            "futures_hand.py", "--execute"
        ])
        self.assertEqual(self.hand_calls[0]["max_notional"], "8")
        self.assertEqual(self.hand_calls[0]["module_max_notional"], "8")
        self.assertEqual(self.leverage.calls[1], (
            "POST", "/fapi/v1/leverage",
            {"symbol": "TSTUSDT", "leverage": "1"}, True,
        ))
        self.assertEqual(self.leverage.current, 1)
        self.assertEqual(
            [event[0] for event in self.events],
            ["leverage", "leverage", "leverage", "leverage", "futures"],
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

        self.assertEqual(self.momentum_plan_caps, [Decimal("8")])
        self.assertEqual(quantity, Decimal("0.04"))
        self.assertEqual(self.momentum_reads.calls, [
            ("GET", "/fapi/v3/positionRisk",
             {"symbol": "TSTUSDT"}, True),
            ("GET", "/fapi/v3/account", {}, True),
            ("GET", "/fapi/v1/openOrders",
             {"symbol": "TSTUSDT"}, True),
            ("GET", "/fapi/v1/symbolConfig",
             {"symbol": "TSTUSDT"}, True),
        ])
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
        self.queue_momentum_measurement(0, account_quantity=None)

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
                "independent momentum position reads disagree",
            ),
            (
                "hedge side",
                [self.momentum_row("0.03", "LONG")],
                [self.momentum_row("0.03")],
                1,
                "position-risk reported hedge-side state",
            ),
            (
                "short",
                [self.momentum_row("-0.03")],
                [self.momentum_row("-0.03")],
                1,
                "position-risk reported a short position",
            ),
            (
                "duplicate exposure",
                [self.momentum_row("0.01"), self.momentum_row("0.02")],
                [self.momentum_row("0.03")],
                1,
                "position-risk returned more than one nonzero symbol row",
            ),
            (
                "leverage",
                [self.momentum_row("0.03")],
                [self.momentum_row("0.03")],
                2,
                "momentum leverage is not proven 1x",
            ),
        ]
        with mock.patch.object(jr.futures_hand, "SYMBOL", "TSTUSDT"), \
                mock.patch.object(
                    jr.futures_hand, "http", self.momentum_reads
                ):
            for name, risks, positions, leverage, reason in cases:
                with self.subTest(name=name):
                    self.momentum_reads.queue(
                        risks, positions, leverage=leverage
                    )
                    with self.assertRaisesRegex(
                            jr.MomentumStateError, reason):
                        jr._momentum_measure("TSTUSDT")

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
        with mock.patch.object(
                jr, "_momentum_open_plan",
                side_effect=self.fake_momentum_plan), \
                mock.patch.object(
                    jr, "_momentum_submit",
                    side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()

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

        def query_only(value):
            queried.append(value)
            return "not_found", None

        self.queue_momentum_measurement(0)
        with mock.patch.object(
                jr, "_momentum_query", side_effect=query_only), \
                mock.patch.object(
                    jr.futures_hand, "http", self.momentum_reads), \
                mock.patch.object(jr, "_momentum_submit") as no_post:
            self.assertEqual(self.tick(), 2)
        no_post.assert_not_called()
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
        self.assertEqual(base_url, jr.basis_executor.SPOT.base_url)
        params = dict(params or {})
        if method == "GET" and path == "/api/v3/exchangeInfo":
            self.assertFalse(signed)
            return "ok", {"symbols": [self.spot_info()]}
        if method == "GET" and path == "/api/v3/ticker/bookTicker":
            self.assertFalse(signed)
            return "ok", {
                "symbol": "TSTUSDT",
                "bidPrice": str(self.spot_bid),
                "askPrice": str(self.spot_ask),
            }
        if method == "GET" and path == "/api/v3/account":
            self.assertTrue(signed)
            return "ok", {
                "canTrade": True,
                "balances": [
                    {"asset": "TST", "free": str(self.base_free)},
                    {"asset": "USDT", "free": str(self.quote_free)},
                ],
            }
        if method == "GET" and path == "/api/v3/openOrders":
            self.assertTrue(signed)
            return "ok", []
        if method == "POST" and path == "/api/v3/order":
            self.assertTrue(signed)
            self.posts.append(params)
            if self.unknown_submission:
                return "unclear", {"http": 502}
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
            order_id = str(len(self.posts))
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
                "id": order_id,
                "orderId": order_id,
                "qty": str(quantity),
                "price": str(price),
                "commission": str(commission),
                "commissionAsset": commission_asset,
            }
            self.orders[params["newClientOrderId"]] = dict(order)
            return "ok", {**order, "fills": [fill]}
        if method == "GET" and path == "/api/v3/order":
            self.assertTrue(signed)
            if self.unknown_submission:
                return "api_error", {"code": -1003, "msg": "offline"}
            return "ok", dict(self.orders[params["origClientOrderId"]])
        raise AssertionError("unexpected spot request %s %s" % (
            method, path,
        ))

    def tick(self, **kwargs):
        return jr.tick(
            receipt_dir=self.receipt_dir,
            now=NOW,
            spot_request=self.spot_request,
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
        with mock.patch.object(
                jr, "_ensure_leverage_1x",
                side_effect=AssertionError("spot path attempted Futures")):
            self.assertEqual(self.tick(), 0)

    def test_spot_open_routes_proven_leg_through_ticket_governor_and_clamp(self):
        self.open_spot()

        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["side"], "BUY")
        self.assertEqual(self.posts[0]["quoteOrderQty"], "8.00000000")
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
        self.assertEqual(state["measured_qty"], "0.07900000")

    def test_default_momentum_open_remains_on_futures_path(self):
        self.job("futures-default", maximum=6)
        measured = {"qty": Decimal("0.03"), "price": Decimal("200"),
                    "open_orders": 0, "leverage": 1}
        order = {
            "status": "FILLED",
            "clientOrderId": "unused",
            "orderId": "77",
            "executedQty": "0.03",
            "cumQuote": "6",
            "avgPrice": "200",
            "reduceOnly": False,
        }

        def submit(intent):
            order["clientOrderId"] = intent["newClientOrderId"]
            fill = {
                "id": "1", "orderId": "77", "qty": "0.03",
                "price": "200", "commission": "0.003",
                "commissionAsset": "USDT", "realizedPnl": "0",
            }
            return "terminal", dict(order), [fill], "ok"

        with mock.patch.object(jr, "_ensure_leverage_1x", return_value=True), \
                mock.patch.object(jr, "_momentum_open_plan", return_value={
                    "quantity": Decimal("0.03"),
                    "limit_price": Decimal("200"),
                    "current_leverage": 1,
                }), \
                mock.patch.object(jr, "_momentum_submit", side_effect=submit), \
                mock.patch.object(jr, "_momentum_measure", return_value=measured), \
                mock.patch.object(jr, "_run_spot_momentum_open") as spot_path:
            self.assertEqual(self.tick(), 0)

        spot_path.assert_not_called()
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
