#!/usr/bin/env python3
"""Offline deterministic tests for the restricted live-test job runner."""

import json
import os
import socket
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

    def tick(self):
        return jr.tick(receipt_dir=self.receipt_dir, now=NOW)

    def results(self):
        path = os.path.join(self.jobs_dir, "results.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def reason(self, row):
        with open(row["receipt_path"], encoding="utf-8") as handle:
            return json.load(handle)["reason"]

    def result_events(self, kind):
        return [row for row in self.results() if row.get("kind") == kind]

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
                    self.assertEqual(
                        self.reason(row), "ENTRY_BLOCKED_NOT_ACCEPTED"
                    )
                    self.assertTrue(os.path.isfile(os.path.join(
                        self.jobs_dir, "claims", "job-%s.claim" % job_id
                    )))
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.basis_calls, [])
        self.assertEqual(self.momentum_intents, [])
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


if __name__ == "__main__":
    unittest.main()
