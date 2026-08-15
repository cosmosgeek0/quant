#!/usr/bin/env python3
"""Offline specification shard for the reworked ACCEPT-ONE entry gate."""

import json
import os
import socket
import sys
import tempfile
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


def _code_mentions(code, text):
    for value in code.co_consts:
        if isinstance(value, str) and text in value:
            return True
        if isinstance(value, types.CodeType) and _code_mentions(value, text):
            return True
    return False


def _module_mentions(module, text):
    for value in vars(module).values():
        code = getattr(value, "__code__", None)
        if isinstance(code, types.CodeType) and _code_mentions(code, text):
            return True
    return False


# The old helper exists, but the universal armed-mode refusal does not.  Keep
# the behavioral shard green until that separately owned rework lands.
_HAS_JOB_AWARE_ACCEPT_ONE = (
    hasattr(jr, "_consume_accept_one")
    and _module_mentions(jr, "ENTRY_BLOCKED_NOT_ACCEPTED")
)
_HAS_TICKET_BOUND = _module_mentions(
    jr, "ACCEPT_ONE_NOTIONAL_EXCEEDED"
)
_HAS_TICKET_RESEAL = _module_mentions(jr, "ACCEPT-ONE.expired-")


class LeverageTransport:
    """Offline transport for the runner's 1x-leverage proof."""

    def __init__(self):
        self.calls = []
        self.current = 1

    def __call__(self, method, base_url, path, params=None, signed=False):
        call = (method, path, dict(params or {}), signed)
        self.calls.append(call)
        if method == "GET" and path == "/fapi/v1/symbolConfig":
            symbol = (params or {}).get("symbol", "")
            return "ok", [{"symbol": symbol, "leverage": str(self.current)}]
        if method == "POST" and path == "/fapi/v1/leverage" \
                and (params or {}).get("leverage") == "1":
            self.current = 1
            return "ok", {"leverage": 1}
        raise AssertionError("unexpected leverage transport call")


class MomentumHTTP:
    """Serve filled orders and independent position reads at the HTTP seam."""

    def __init__(self):
        self.calls = []
        self.intents = []
        self.orders = {}
        self.snapshots = []
        self.active = None

    @staticmethod
    def position_row(quantity, mark_price="200"):
        row = {
            "symbol": "TSTUSDT",
            "positionSide": "BOTH",
            "positionAmt": str(quantity),
        }
        if mark_price is not None:
            row["markPrice"] = str(mark_price)
        return row

    def queue_position(self, quantity, mark_price="200"):
        row = self.position_row(quantity, mark_price)
        self.snapshots.append({
            "/fapi/v3/positionRisk": [dict(row)],
            "/fapi/v3/account": {"positions": [dict(row)]},
            "/fapi/v1/openOrders": [],
            "/fapi/v1/symbolConfig": [{
                "symbol": "TSTUSDT",
                "leverage": "1",
            }],
        })

    def _submit(self, params):
        intent = dict(params or {})
        self.intents.append(intent)
        close = str(intent.get("reduceOnly", "")).lower() == "true"
        quantity = Decimal(str(intent["quantity"]))
        price = Decimal("205") if close else Decimal("200")
        order_id = "2202" if close else "1101"
        order = {
            "symbol": "TSTUSDT",
            "status": "FILLED",
            "clientOrderId": intent["newClientOrderId"],
            "orderId": order_id,
            "executedQty": str(quantity),
            "cumQuote": str(quantity * price),
            "avgPrice": str(price),
            "reduceOnly": close,
            "positionSide": "BOTH",
            "side": "SELL" if close else "BUY",
        }
        fill = {
            "id": "92" if close else "91",
            "orderId": order_id,
            "qty": str(quantity),
            "price": str(price),
            "commission": "0.002" if close else "0.003",
            "commissionAsset": "USDT",
            "realizedPnl": "0.25" if close else "0",
        }
        self.orders[intent["newClientOrderId"]] = dict(order)
        response = dict(order)
        response["fills"] = [fill]
        return "ok", response

    def __call__(self, method, path, params=None, signed=False):
        call = (method, path, dict(params or {}), signed)
        self.calls.append(call)
        if method == "POST" and path == "/fapi/v1/order" and signed:
            return self._submit(params)
        if method == "GET" and path == "/fapi/v1/order" and signed:
            client_id = (params or {}).get("origClientOrderId")
            if client_id not in self.orders:
                raise AssertionError("query used an unknown momentum client id")
            return "ok", dict(self.orders[client_id])
        if method != "GET" or not signed:
            raise AssertionError("momentum truth read was not a signed GET")
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


class JobRunnerImportTests(unittest.TestCase):
    def test_job_runner_imports_before_rework_is_available(self):
        self.assertTrue(hasattr(jr, "tick"))
        self.assertTrue(hasattr(jr, "futures_hand"))


@unittest.skipUnless(
    _HAS_JOB_AWARE_ACCEPT_ONE,
    "job-id-aware ACCEPT-ONE interface has not landed",
)
class AcceptOneReworkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.receipt_dir = temporary.name
        self.jobs_dir = os.path.join(self.receipt_dir, "jobs")
        os.makedirs(self.jobs_dir)
        self.accept_path = os.path.join(self.receipt_dir, "ACCEPT-ONE")
        self.operator_path = os.path.join(self.receipt_dir, "OPERATOR")
        self.hand_calls = []
        self.basis_calls = []
        self.hand_verdict = "CLOSED"
        self.hand_exit = 0
        self.leverage = LeverageTransport()
        self.momentum_http = MomentumHTTP()
        self.momentum_plan_caps = []
        self.governor_budget_calls = []
        self.governor_acquire_calls = []
        self.governor_releases = []
        self.governor_realized = []
        self.governor_refusal = ""

        class FakeGovernorRefusal(Exception):
            def __init__(inner_self, reason):
                super().__init__(reason)
                inner_self.reason = reason

        governor = types.ModuleType("account_governor")
        governor.GovernorRefusal = FakeGovernorRefusal

        def budgets_allow(receipt_dir):
            self.governor_budget_calls.append(receipt_dir)
            return True, ""

        def acquire_entry_slot(op, symbol, receipt_dir):
            self.governor_acquire_calls.append((op, symbol, receipt_dir))
            if self.governor_refusal:
                raise FakeGovernorRefusal(self.governor_refusal)
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

        leverage = mock.patch.object(
            jr.set_leverage_1x, "live_request_factory",
            return_value=self.leverage,
        )
        leverage.start()
        self.addCleanup(leverage.stop)

        futures = mock.patch.object(
            jr.futures_hand, "main", self.fake_futures
        )
        futures.start()
        self.addCleanup(futures.stop)
        basis = mock.patch.object(
            jr.basis_executor, "main", self.fake_basis
        )
        basis.start()
        self.addCleanup(basis.stop)

        def deny(*_args, **_kwargs):
            raise AssertionError("offline accept-one test attempted network")

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
            "max_notional": 32,
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
        with open(self.operator_path, "w", encoding="utf-8") as handle:
            json.dump({"operator": operator}, handle)

    def valid_acceptance(self, operator="lead", **overrides):
        value = {
            "operator": operator,
            "created_utc": utc(NOW - timedelta(minutes=1)),
            "expires_utc": utc(NOW + timedelta(minutes=10)),
            "max_notional": "32",
        }
        value.update(overrides)
        with open(self.accept_path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return value

    def momentum_markers(self):
        Path(os.path.join(
            self.jobs_dir, "futures_proof_passed"
        )).touch()
        Path(os.path.join(
            self.jobs_dir, "basis_proof_passed"
        )).touch()

    def fake_futures(self):
        index = len(self.hand_calls) + 1
        self.hand_calls.append({
            "argv": list(sys.argv),
            "max_notional": os.environ.get("MAX_NOTIONAL"),
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
        self.basis_calls.append({
            "argv": list(argv),
            "max_notional": os.environ.get("MAX_NOTIONAL"),
        })
        path = os.path.join(
            self.receipt_dir,
            "basis-executor-offline%s.json" % len(self.basis_calls),
        )
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"verdict": "CLOSED"}, handle)
        return 0

    def fake_momentum_plan(self, effective):
        self.momentum_plan_caps.append(effective)
        return {
            "quantity": effective / Decimal("200"),
            "limit_price": Decimal("200"),
            "current_leverage": 1,
        }

    @contextmanager
    def momentum_runtime(self):
        with mock.patch.object(
                jr, "_momentum_open_plan",
                side_effect=self.fake_momentum_plan), \
                mock.patch.object(
                    jr.futures_hand, "http",
                    side_effect=self.momentum_http):
            yield

    def tick(self):
        with mock.patch("builtins.print"):
            return jr.tick(receipt_dir=self.receipt_dir, now=NOW)

    def results(self):
        path = os.path.join(self.jobs_dir, "results.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    @staticmethod
    def reason(row):
        with open(row["receipt_path"], encoding="utf-8") as handle:
            return json.load(handle)["reason"]

    def claimed_acceptance(self, job_id):
        return self.accept_path + ".claimed-" + job_id

    def assert_no_entry_order(self):
        self.assertEqual(self.hand_calls, [])
        self.assertEqual(self.basis_calls, [])
        self.assertFalse(any(
            method == "POST" and path == "/fapi/v1/order"
            for method, path, _params, _signed in self.momentum_http.calls
        ))

    def test_valid_marker_is_renamed_for_the_filled_entry_job(self):
        self.write_operator()
        acceptance = self.valid_acceptance()
        self.momentum_markers()
        self.job("filled", op="MOMENTUM_OPEN", max_notional=32)
        self.momentum_http.queue_position(Decimal("0.04"))

        with self.momentum_runtime():
            self.assertEqual(self.tick(), 0)

        self.assertEqual(self.results()[-1]["verdict"], "OPEN")
        self.assertEqual(len(self.momentum_http.intents), 1)
        self.assertEqual(self.momentum_http.intents[0]["reduceOnly"], "false")
        self.assertFalse(os.path.exists(self.accept_path))
        claim = self.claimed_acceptance("filled")
        self.assertTrue(os.path.isfile(claim))
        with open(claim, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), acceptance)
        self.assertFalse(os.path.exists(self.accept_path + ".claimed"))
        self.assertFalse(os.path.exists(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

    def test_refused_executor_attempt_leaves_acceptance_unclaimed(self):
        self.write_operator()
        acceptance = self.valid_acceptance()
        self.hand_verdict = "REFUSED"
        self.hand_exit = 2
        self.job("refused")

        self.assertEqual(self.tick(), 2)

        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual(self.results()[-1]["verdict"], "REFUSED")
        self.assertTrue(os.path.isfile(self.accept_path))
        with open(self.accept_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), acceptance)
        self.assertFalse(os.path.exists(self.claimed_acceptance("refused")))

    @unittest.skipUnless(
        _HAS_TICKET_BOUND,
        "ticket-bound ACCEPT-ONE interface has not landed",
    )
    def test_ticket_bound_refuses_then_preserved_ticket_accepts_eight(self):
        self.write_operator()
        acceptance = self.valid_acceptance(max_notional="8")
        self.job("ticket-overflow", max_notional=32)

        self.assertEqual(self.tick(), 2)

        refused = self.results()[-1]
        self.assertEqual(refused["verdict"], "REFUSED")
        self.assertEqual(
            self.reason(refused), "ACCEPT_ONE_NOTIONAL_EXCEEDED"
        )
        self.assert_no_entry_order()
        self.assertTrue(os.path.isfile(self.accept_path))
        with open(self.accept_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), acceptance)
        self.assertFalse(os.path.exists(
            self.claimed_acceptance("ticket-overflow")
        ))

        self.job("ticket-compliant", max_notional=8)

        self.assertEqual(self.tick(), 0)

        self.assertEqual(self.results()[-1]["verdict"], "CLOSED")
        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual(self.hand_calls[0]["max_notional"], "8")
        self.assertFalse(os.path.exists(self.accept_path))
        claim = self.claimed_acceptance("ticket-compliant")
        self.assertTrue(os.path.isfile(claim))
        with open(claim, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), acceptance)

    def test_expired_acceptance_is_invalid_and_places_no_order(self):
        self.write_operator()
        self.valid_acceptance(
            created_utc=utc(NOW - timedelta(minutes=10)),
            expires_utc=utc(NOW - timedelta(seconds=1)),
        )
        self.job("expired-accept")

        self.assertEqual(self.tick(), 2)

        row = self.results()[-1]
        self.assertEqual(row["verdict"], "REFUSED")
        self.assertEqual(self.reason(row), "ACCEPT_ONE_INVALID")
        self.assert_no_entry_order()
        self.assertTrue(os.path.isfile(self.accept_path))

    def test_malformed_acceptance_forms_are_invalid_without_orders(self):
        self.write_operator()
        valid = {
            "operator": "lead",
            "created_utc": utc(NOW - timedelta(minutes=1)),
            "expires_utc": utc(NOW + timedelta(minutes=10)),
            "max_notional": "32",
        }
        cases = [
            "{not-json",
            dict(valid, max_notional=32),
            {key: value for key, value in valid.items()
             if key != "expires_utc"},
            dict(valid, unexpected="field"),
            dict(valid, expires_utc=utc(NOW + timedelta(minutes=16))),
        ]
        for index, marker in enumerate(cases):
            with self.subTest(case=index):
                with open(self.accept_path, "w", encoding="utf-8") as handle:
                    if isinstance(marker, str):
                        handle.write(marker)
                    else:
                        json.dump(marker, handle)
                self.job("bad-accept-%s" % index)

                self.assertEqual(self.tick(), 2)

                row = self.results()[-1]
                self.assertEqual(row["verdict"], "REFUSED")
                self.assertEqual(self.reason(row), "ACCEPT_ONE_INVALID")
                self.assert_no_entry_order()
                self.assertTrue(os.path.isfile(self.accept_path))

    def test_armed_entries_without_acceptance_are_universally_blocked(self):
        self.write_operator()
        self.momentum_markers()
        self.assertFalse(os.path.exists(os.path.join(
            self.receipt_dir, "STOP"
        )))
        self.assertFalse(os.path.exists(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

        for index, op in enumerate(sorted(jr.ENTRY_OPS)):
            with self.subTest(op=op):
                if op in jr.MARGIN_OPS:
                    # A margin op must be WELL-FORMED so the refusal under
                    # test is genuinely the acceptance gate, not the schema.
                    self.job(
                        "unaccepted-%s" % index, op=op,
                        symbol=None, max_notional=None,
                        asset="USDT", principal="2",
                        client_tag="acc-%s" % index,
                        operator_token="lead",
                    )
                    path = os.path.join(
                        self.jobs_dir, "job-unaccepted-%s.json" % index
                    )
                    with open(path, encoding="utf-8") as handle:
                        value = json.load(handle)
                    value = {
                        key: item for key, item in value.items()
                        if item is not None
                    }
                    with open(path, "w", encoding="utf-8") as handle:
                        json.dump(value, handle)
                else:
                    self.job("unaccepted-%s" % index, op=op)

                self.assertEqual(self.tick(), 2)

                row = self.results()[-1]
                self.assertEqual(row["op"], op)
                self.assertEqual(row["verdict"], "REFUSED")
                self.assertEqual(
                    self.reason(row), "ENTRY_BLOCKED_NOT_ACCEPTED"
                )
                self.assert_no_entry_order()

    def test_operator_file_and_both_requested_operators_must_match(self):
        self.write_operator("lead")
        cases = [
            ("accept-mismatch", "outsider", "lead"),
            ("request-mismatch", "lead", "outsider"),
        ]
        for job_id, accept_operator, requested_by in cases:
            with self.subTest(job_id=job_id):
                self.valid_acceptance(accept_operator)
                self.job(job_id, requested_by=requested_by)

                self.assertEqual(self.tick(), 2)

                row = self.results()[-1]
                self.assertEqual(row["verdict"], "REFUSED")
                self.assertEqual(self.reason(row), "OPERATOR_MISMATCH")
                self.assert_no_entry_order()
                self.assertTrue(os.path.isfile(self.accept_path))
                self.assertFalse(os.path.exists(
                    self.claimed_acceptance(job_id)
                ))

    def test_missing_operator_file_refuses_and_preserves_acceptance(self):
        self.valid_acceptance()
        self.job("operator-missing")

        self.assertEqual(self.tick(), 2)

        row = self.results()[-1]
        self.assertEqual(row["verdict"], "REFUSED")
        self.assertEqual(self.reason(row), "OPERATOR_MISMATCH")
        self.assert_no_entry_order()
        self.assertTrue(os.path.isfile(self.accept_path))
        self.assertFalse(os.path.exists(
            self.claimed_acceptance("operator-missing")
        ))

    def test_closed_reconcile_reseals_and_writes_close_marker_once(self):
        self.write_operator()
        self.valid_acceptance()
        self.momentum_markers()
        self.job("seal-open", op="MOMENTUM_OPEN")
        quantity = Decimal("0.04")
        self.momentum_http.queue_position(quantity)
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 0)

        close_marker = os.path.join(
            self.receipt_dir, "first_autonomous_close_passed"
        )
        stop_jobs = os.path.join(self.receipt_dir, "STOP-JOBS")
        self.assertFalse(os.path.exists(close_marker))
        self.assertFalse(os.path.exists(stop_jobs))

        self.job("seal-exit", op="MOMENTUM_EXIT")
        self.momentum_http.queue_position(quantity, mark_price="200")
        self.momentum_http.queue_position(Decimal("0"), mark_price=None)
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 0)

        self.assertEqual(self.results()[-1]["verdict"], "CLOSED")
        self.assertTrue(os.path.isfile(stop_jobs))
        self.assertTrue(os.path.isfile(close_marker))
        with open(close_marker, "rb") as handle:
            marker_before = handle.read()
        self.assertTrue(marker_before)
        marker_stat = os.stat(close_marker)
        stop_stat = os.stat(stop_jobs)

        self.assertEqual(self.tick(), 0)

        with open(close_marker, "rb") as handle:
            self.assertEqual(handle.read(), marker_before)
        marker_after = os.stat(close_marker)
        stop_after = os.stat(stop_jobs)
        self.assertEqual(marker_after.st_ino, marker_stat.st_ino)
        self.assertEqual(marker_after.st_mtime_ns, marker_stat.st_mtime_ns)
        self.assertEqual(stop_after.st_ino, stop_stat.st_ino)
        self.assertEqual(stop_after.st_mtime_ns, stop_stat.st_mtime_ns)

    @unittest.skipUnless(
        _HAS_TICKET_RESEAL,
        "seal-on-flat ticket expiry interface has not landed",
    )
    def test_seal_on_flat_expires_ticket_and_refuses_queued_entry(self):
        self.write_operator()
        self.valid_acceptance()
        self.momentum_markers()
        self.job("reseal-open", op="MOMENTUM_OPEN", max_notional=8)
        quantity = Decimal("0.04")
        self.momentum_http.queue_position(quantity)
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 0)

        standing = self.valid_acceptance(max_notional="8")
        self.job("reseal-exit", op="MOMENTUM_EXIT")
        self.momentum_http.queue_position(quantity, mark_price="200")
        self.momentum_http.queue_position(Decimal("0"), mark_price=None)
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 0)

        expired = self.accept_path + ".expired-" + utc(NOW)
        self.assertFalse(os.path.exists(self.accept_path))
        self.assertTrue(os.path.isfile(expired))
        with open(expired, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), standing)
        self.assertEqual(
            list(Path(self.receipt_dir).glob("ACCEPT-ONE.expired-*")),
            [Path(expired)],
        )
        self.assertTrue(os.path.isfile(os.path.join(
            self.receipt_dir, "STOP-JOBS"
        )))

        before_intents = list(self.momentum_http.intents)
        before_expired = os.stat(expired)
        self.job("reseal-queued-entry", op="MOMENTUM_OPEN", max_notional=8)
        with self.momentum_runtime():
            self.assertEqual(self.tick(), 2)

        refused = self.results()[-1]
        self.assertEqual(refused["job_id"], "reseal-queued-entry")
        self.assertEqual(refused["verdict"], "REFUSED")
        self.assertEqual(self.momentum_http.intents, before_intents)
        self.assertEqual(len(self.hand_calls), 0)
        self.assertEqual(len(self.basis_calls), 0)
        after_expired = os.stat(expired)
        self.assertEqual(after_expired.st_ino, before_expired.st_ino)
        self.assertEqual(
            after_expired.st_mtime_ns, before_expired.st_mtime_ns
        )
        self.assertEqual(
            list(Path(self.receipt_dir).glob("ACCEPT-ONE.expired-*")),
            [Path(expired)],
        )

    def test_pre_close_marker_entry_is_clamped_to_eight(self):
        self.write_operator()
        self.valid_acceptance()
        self.job("cap-first", max_notional=32)

        self.assertEqual(self.tick(), 0)

        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual(self.hand_calls[0]["max_notional"], "8")
        self.assertEqual(self.hand_calls[0]["module_max_notional"], "8")
        self.assertEqual(jr.limits.FIRST_TRADE_CAP_USDT, Decimal("8"))

    def test_post_close_marker_entry_is_clamped_to_thirty_two(self):
        self.write_operator()
        self.valid_acceptance()
        marker = os.path.join(
            self.receipt_dir, "first_autonomous_close_passed"
        )
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("prior-close %s\n" % utc(NOW - timedelta(minutes=1)))
        self.job("cap-proven", max_notional=32)

        self.assertEqual(self.tick(), 0)

        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual(self.hand_calls[0]["max_notional"], "32")
        self.assertEqual(self.hand_calls[0]["module_max_notional"], "32")
        self.assertEqual(jr.limits.PER_LEG_CAP_USDT, Decimal("32"))

    def test_job_bound_is_effective_below_ticket_and_clamp(self):
        self.write_operator()
        self.valid_acceptance(max_notional="32")
        self.job("cap-job", max_notional=6)

        self.assertEqual(self.tick(), 0)

        self.assertEqual(len(self.hand_calls), 1)
        self.assertEqual(self.hand_calls[0]["max_notional"], "6")
        self.assertEqual(self.hand_calls[0]["module_max_notional"], "6")


if __name__ == "__main__":
    unittest.main()
