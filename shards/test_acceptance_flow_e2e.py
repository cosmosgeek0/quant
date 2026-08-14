"""Offline cross-module tests for the one-acceptance entry lifecycle.

The shard keeps the momentum adapter, runner, durable position, result ledger,
and basis supervisor in their real paths.  Only exchange/account transports
are injected.  Capability guards make an incomplete sibling-lane merge skip;
once the frozen interface is present, every lifecycle assertion is active.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import socket
import sys
import tempfile
import types
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _optional_module(name):
    try:
        return importlib.import_module(name)
    except (ImportError, SyntaxError):
        return None


def _code_mentions(code, text):
    for value in code.co_consts:
        if isinstance(value, str) and text in value:
            return True
        if isinstance(value, types.CodeType) and _code_mentions(value, text):
            return True
    return False


def _module_mentions(module, text):
    if module is None:
        return False
    for value in vars(module).values():
        code = getattr(value, "__code__", None)
        if isinstance(code, types.CodeType) and _code_mentions(code, text):
            return True
    return False


jr = _optional_module("job_runner")
live = _optional_module("momentum_live")
lifecycle = _optional_module("momentum_lifecycle")
bp = _optional_module("basis_position")
bs = _optional_module("basis_supervisor")

RUNNER_ACCEPTANCE_READY = bool(
    jr is not None
    and hasattr(jr, "_consume_accept_one")
    and _module_mentions(jr, "ENTRY_BLOCKED_NOT_ACCEPTED")
)
MANAGE_ONLY_READY = bool(
    live is not None
    and hasattr(live, "_manage_only_refusal_row")
    and hasattr(live, "run_tick")
)
BASIS_ACCEPTANCE_READY = bool(
    bs is not None
    and hasattr(bs, "tick")
    and hasattr(bs, "_entry_ticket_refusal")
    and (
        hasattr(bs, "_consume_accept_one")
        or _module_mentions(bs, "ENTRY_BLOCKED_NOT_ACCEPTED")
    )
)

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-08-14T12:00:00.000Z"
SHA = "7ac26aa771937f802930dac5eb017d6583d968d8"
SYMBOL = "AAAUSDT"
OPERATOR = "acceptance-e2e"


def _utc(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _governor_module():
    module = types.ModuleType("account_governor")

    class GovernorRefusal(Exception):
        def __init__(self, reason):
            super().__init__(reason)
            self.reason = reason

    module.GovernorRefusal = GovernorRefusal
    module.budgets_allow = lambda _receipt_dir: (True, "")
    module.acquire_entry_slot = (
        lambda _op, _symbol, _receipt_dir: "acceptance-slot"
    )
    module.release_entry_slot = lambda _token, _receipt_dir: None
    module.record_realized = (
        lambda _net_usdt, _fees_usdt, _receipt_dir: None
    )
    return module


class _LeverageTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, _base_url, path, params=None, signed=False):
        self.calls.append((method, path, dict(params or {}), signed))
        if method == "GET" and path == "/fapi/v1/symbolConfig":
            return "ok", [{"symbol": SYMBOL, "leverage": "1"}]
        raise AssertionError("unexpected leverage transport call")


class _MomentumExchange:
    """One deterministic position ledger behind futures_hand.http."""

    def __init__(self):
        self.quantity = Decimal("0")
        self.mark_price = Decimal("108")
        self.orders = {}
        self.posts = []
        self.next_order_id = 4100

    @staticmethod
    def _text(value):
        return format(Decimal(value), "f")

    def _position_row(self, include_price=False):
        row = {
            "symbol": SYMBOL,
            "positionSide": "BOTH",
            "positionAmt": self._text(self.quantity),
        }
        if include_price:
            row["markPrice"] = self._text(self.mark_price)
        return row

    def _submit(self, params):
        intent = dict(params)
        self.posts.append(intent)
        self.next_order_id += 1
        quantity = Decimal(intent["quantity"])
        reduce_only = str(intent.get("reduceOnly", "")).lower() == "true"
        if reduce_only:
            if quantity != self.quantity:
                raise AssertionError("exit did not use the standing quantity")
            price = Decimal("106")
            self.quantity = Decimal("0")
            realized = Decimal("-0.10")
            commission = Decimal("0.002")
        else:
            if self.quantity != 0:
                raise AssertionError("a second entry reached the exchange")
            price = Decimal("108")
            self.quantity = quantity
            realized = Decimal("0")
            commission = Decimal("0.003")
        order_id = str(self.next_order_id)
        client_id = intent["newClientOrderId"]
        order = {
            "symbol": SYMBOL,
            "side": intent["side"],
            "positionSide": "BOTH",
            "status": "FILLED",
            "executedQty": self._text(quantity),
            "cumQuote": self._text(quantity * price),
            "avgPrice": self._text(price),
            "reduceOnly": reduce_only,
            "clientOrderId": client_id,
            "orderId": order_id,
        }
        fill = {
            "id": "trade-" + order_id,
            "orderId": order_id,
            "qty": self._text(quantity),
            "price": self._text(price),
            "commission": self._text(commission),
            "commissionAsset": "USDT",
            "realizedPnl": self._text(realized),
        }
        self.orders[client_id] = dict(order)
        response = dict(order)
        response["fills"] = [fill]
        return "ok", response

    def __call__(self, method, path, params=None, signed=False):
        params = dict(params or {})
        if not signed:
            raise AssertionError("runner exchange call was not signed")
        if method == "POST" and path == "/fapi/v1/order":
            return self._submit(params)
        if method == "GET" and path == "/fapi/v1/order":
            client_id = params.get("origClientOrderId")
            order = self.orders.get(client_id)
            if order is None:
                return "api_error", {
                    "code": -2013,
                    "msg": "Order does not exist.",
                }
            return "ok", dict(order)
        if method == "GET" and path == "/fapi/v3/positionRisk":
            return "ok", [self._position_row(include_price=True)]
        if method == "GET" and path == "/fapi/v3/account":
            return "ok", {"positions": [self._position_row()]}
        if method == "GET" and path == "/fapi/v1/openOrders":
            return "ok", []
        if method == "GET" and path == "/fapi/v1/symbolConfig":
            return "ok", [{"symbol": SYMBOL, "leverage": "1"}]
        raise AssertionError("unexpected momentum transport call: " + path)


class _OfflineCase(unittest.TestCase):
    def deny_network(self):
        def deny(*_args, **_kwargs):
            raise AssertionError("acceptance E2E attempted network access")

        for target, name in (
            (urllib.request, "urlopen"),
            (socket, "socket"),
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
        ):
            patcher = mock.patch.object(target, name, deny)
            patcher.start()
            self.addCleanup(patcher.stop)

    def install_governor(self):
        patcher = mock.patch.dict(
            sys.modules, {"account_governor": _governor_module()}
        )
        patcher.start()
        self.addCleanup(patcher.stop)


@unittest.skipUnless(
    RUNNER_ACCEPTANCE_READY,
    "reworked runner acceptance interface has not landed",
)
@unittest.skipUnless(
    MANAGE_ONLY_READY,
    "momentum manage-only interface has not landed",
)
class AcceptanceLifecycleE2ETests(_OfflineCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.jobs = self.root / "jobs"
        self.stream = self.root / "stream_state.json"
        self.state = self.root / "momentum_state.json"
        self.receipts = self.root / "momentum_receipts.jsonl"
        self.decisions = self.root / "momentum_decisions.jsonl"
        self.position = self.root / "momentum-position.json"
        self.results_path = self.jobs / "results.jsonl"
        self.exchange = _MomentumExchange()
        self.leverage = _LeverageTransport()
        self.deny_network()
        self.install_governor()

        env = mock.patch.dict(os.environ, {
            "RECEIPT_DIR": str(self.root),
            "QUANT_SHA": SHA,
            "BINANCE_API_KEY": "offline-key",
            "BINANCE_API_SECRET": "offline-secret",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)

        patches = (
            mock.patch.object(
                jr.set_leverage_1x,
                "live_request_factory",
                return_value=self.leverage,
            ),
            mock.patch.object(
                jr, "_momentum_open_plan", side_effect=self.open_plan
            ),
            mock.patch.object(
                jr.futures_hand, "http", side_effect=self.exchange
            ),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.jobs.mkdir(mode=0o700)
        (self.jobs / "futures_proof_passed").touch()
        (self.jobs / "basis_proof_passed").touch()
        (self.root / "STOP-JOBS").touch()
        self.write_operator()
        self.write_accept_one()

    @staticmethod
    def open_plan(effective):
        if effective > Decimal("8"):
            raise AssertionError("first accepted entry exceeded its clamp")
        return {
            "quantity": Decimal("0.05"),
            "limit_price": Decimal("108"),
            "current_leverage": 1,
        }

    def write_operator(self):
        (self.root / "OPERATOR").write_text(
            json.dumps({"operator": OPERATOR}), encoding="utf-8"
        )

    def write_accept_one(self):
        (self.root / "ACCEPT-ONE").write_text(
            json.dumps({
                "operator": OPERATOR,
                "created_utc": _utc(NOW - timedelta(minutes=1)),
                "expires_utc": _utc(NOW + timedelta(minutes=10)),
                "max_notional": "8",
            }),
            encoding="utf-8",
        )

    def live_tick(self, payload, *, manage_only=False):
        from tests.test_momentum_live import test_config

        self.stream.write_text(
            json.dumps({SYMBOL: payload}), encoding="utf-8"
        )
        return live.run_tick(
            self.stream,
            state_path=self.state,
            receipt_path=self.receipts,
            decisions_path=self.decisions,
            jobs_dir=self.jobs,
            position_path=self.position,
            results_path=self.results_path,
            config=test_config(),
            now_fn=lambda: NOW_TEXT,
            max_job_notional=Decimal("8"),
            requested_by=OPERATOR,
            manage_only=manage_only,
        )

    def runner_tick(self, now=NOW):
        output = io.StringIO()
        with redirect_stdout(output):
            code = jr.tick(receipt_dir=str(self.root), now=now)
        return code, output.getvalue()

    def result_rows(self):
        if not self.results_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.results_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]

    @staticmethod
    def result_reason(row):
        direct = row.get("reason")
        if isinstance(direct, str) and direct:
            return direct
        receipt_path = row.get("receipt_path")
        if not isinstance(receipt_path, str) or not receipt_path:
            return ""
        with open(receipt_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        reason = payload.get("reason")
        return reason if isinstance(reason, str) else ""

    def write_job(self, job_id, op):
        payload = {
            "job_id": job_id,
            "op": op,
            "symbol": SYMBOL,
            "max_notional": 8,
            "created_utc": _utc(NOW + timedelta(minutes=1)),
            "expires_utc": _utc(NOW + timedelta(minutes=10)),
            "requested_by": OPERATOR,
        }
        path = self.jobs / ("job-" + job_id + ".json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_accept_once_manage_hold_exit_flat_reseal_then_refuse(self):
        from tests.test_momentum_live import stream_payload

        signalled = [
            self.live_tick(stream_payload(0, "100", "2", "100", "1000")),
            self.live_tick(stream_payload(1, "105", "70", "7000", "1050")),
            self.live_tick(stream_payload(2, "108", "75", "7600", "1070")),
        ]
        self.assertEqual(
            [item.decisions[0]["action"] for item in signalled],
            [
                lifecycle.ACTION_WATCH,
                lifecycle.ACTION_WATCH,
                lifecycle.ACTION_ENTER_SMALL,
            ],
        )
        open_paths = sorted(self.jobs.glob("job-*.json"))
        self.assertEqual(len(open_paths), 1)
        open_job = json.loads(open_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(open_job["op"], live.JOB_OPEN_OP)
        self.assertEqual(open_job["requested_by"], OPERATOR)

        code, output = self.runner_tick()
        self.assertEqual(code, 0, output)
        self.assertEqual(
            [str(row.get("reduceOnly", "")).lower()
             for row in self.exchange.posts],
            ["false"],
        )
        self.assertFalse((self.root / "ACCEPT-ONE").exists())
        claimed = self.root / (
            "ACCEPT-ONE.claimed-" + open_job["job_id"]
        )
        self.assertTrue(claimed.is_file())
        self.assertEqual(
            list(self.root.glob("ACCEPT-ONE.claimed-*")), [claimed]
        )
        self.assertTrue((self.root / "STOP-JOBS").is_file())
        position = json.loads(self.position.read_text(encoding="utf-8"))
        self.assertEqual(position["state"], "OPEN")

        held = self.live_tick(
            stream_payload(3, "109", "74", "7500", "1065"),
            manage_only=True,
        )
        self.assertEqual(held.decisions[-1]["action"], lifecycle.ACTION_HOLD)
        self.assertEqual(len(list(self.jobs.glob("job-*.json"))), 1)
        self.assertTrue((self.root / "STOP-JOBS").is_file())

        exiting = self.live_tick(
            stream_payload(4, "85", "30", "3000", "900"),
            manage_only=True,
        )
        self.assertEqual(exiting.decisions[-1]["action"], lifecycle.ACTION_EXIT)
        job_payloads = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.jobs.glob("job-*.json"))
        ]
        exit_jobs = [
            payload for payload in job_payloads
            if payload["op"] == live.JOB_EXIT_OP
        ]
        self.assertEqual(len(exit_jobs), 1)

        code, output = self.runner_tick(NOW + timedelta(minutes=1))
        self.assertEqual(code, 0, output)
        self.assertEqual(
            [str(row.get("reduceOnly", "")).lower()
             for row in self.exchange.posts],
            ["false", "true"],
        )
        self.assertEqual(
            json.loads(self.position.read_text(encoding="utf-8"))["state"],
            "CLOSED",
        )

        self.live_tick(
            stream_payload(5, "84", "2", "100", "800"),
            manage_only=True,
        )
        token = lifecycle.load_state(self.state).tokens[SYMBOL]
        self.assertEqual(token["position_status"], "FLAT")
        self.assertEqual(Decimal(token["qty"]), Decimal("0"))
        self.assertTrue((self.root / "STOP-JOBS").is_file())
        self.assertTrue(
            (self.root / "first_autonomous_close_passed").is_file()
        )

        second_id = "acceptance-second-open"
        self.write_job(second_id, live.JOB_OPEN_OP)
        code, output = self.runner_tick(NOW + timedelta(minutes=2))
        self.assertEqual(code, 2, output)
        rows = self.result_rows()
        second = [row for row in rows if row["job_id"] == second_id]
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["verdict"], "REFUSED")
        self.assertEqual(
            self.result_reason(second[0]), "ENTRY_BLOCKED_NOT_ACCEPTED"
        )
        self.assertEqual(len(self.exchange.posts), 2)


@unittest.skipUnless(
    RUNNER_ACCEPTANCE_READY and BASIS_ACCEPTANCE_READY,
    "basis universal acceptance gate has not landed",
)
class BasisAcceptanceE2ETests(_OfflineCase):
    def test_basis_tick_without_accept_one_refuses_before_transport(self):
        from tests.test_basis_position import (
            SHA as BASIS_SHA,
            ScriptedTransport,
            T_OPEN,
            happy_spec,
            proposal_payload,
        )

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        receipt_dir = Path(holder.name)
        self.deny_network()
        self.install_governor()
        env = mock.patch.dict(
            os.environ, {"QUANT_SHA": BASIS_SHA}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)

        (receipt_dir / "OPERATOR").write_text(
            json.dumps({"operator": OPERATOR}), encoding="utf-8"
        )
        proposal_id = "basis-without-acceptance"
        proposal_dir = receipt_dir / bp.PROPOSAL_DIR
        proposal_dir.mkdir(mode=0o700)
        (proposal_dir / ("proposal-" + proposal_id + ".json")).write_text(
            json.dumps(proposal_payload(proposal_id), sort_keys=True),
            encoding="utf-8",
        )
        cfg = bp.PositionConfig(
            symbol="",
            sha=BASIS_SHA,
            receipt_dir=str(receipt_dir),
            max_hold_ms=86400000,
            min_remaining_edge_pct=Decimal("0.01"),
            max_adverse_move_pct=Decimal("0.5"),
        )
        transport = ScriptedTransport(happy_spec())
        output = io.StringIO()
        with redirect_stdout(output):
            code = bs.tick(
                cfg,
                transport,
                sleep=lambda _seconds: None,
                now_ms=lambda: T_OPEN,
                claimant=OPERATOR,
            )

        text = output.getvalue()
        self.assertNotEqual(code, bs.EXIT_ACTED, text)
        self.assertIn("ENTRY_BLOCKED_NOT_ACCEPTED", text)
        self.assertEqual(transport.calls, [])
        self.assertIsNone(bp.load_position(str(receipt_dir)))
        self.assertFalse((receipt_dir / "ACCEPT-ONE").exists())


if __name__ == "__main__":
    unittest.main()
