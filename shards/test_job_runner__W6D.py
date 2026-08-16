#!/usr/bin/env python3
"""Focused regressions for the job-runner Spot adapter migration."""

import json
import os
import sys
import tempfile
import types
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


NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
SHA = "7ac26aa771937f802930dac5eb017d6583d968d8"


class SpotTransport:
    def __init__(self, receipt_dir):
        self.receipt_dir = receipt_dir
        self.base_free = Decimal("5")
        self.quote_free = Decimal("100")
        self.bid = Decimal("99")
        self.ask = Decimal("100")
        self.orders = {}
        self.posts = []
        self.post_revisions = []

    @staticmethod
    def result(payload):
        return jr.binance_http.HttpResult("ok", payload, None, None)

    def __call__(self, method, base_url, path, params=None, signed=False):
        self.assert_base(base_url)
        values = dict(params or ())
        if method == "GET" and path == "/api/v3/exchangeInfo":
            return self.result({"symbols": [{
                "symbol": "TSTUSDT", "status": "TRADING",
                "baseAsset": "TST", "quoteAsset": "USDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001",
                     "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                ],
            }]})
        if method == "GET" and path == "/api/v3/ticker/bookTicker":
            return self.result({
                "symbol": "TSTUSDT", "bidPrice": str(self.bid),
                "askPrice": str(self.ask),
            })
        if method == "GET" and path == "/api/v3/account":
            return self.result({
                "canTrade": True,
                "balances": [
                    {"asset": "TST", "free": str(self.base_free),
                     "locked": "0"},
                    {"asset": "USDT", "free": str(self.quote_free),
                     "locked": "0"},
                ],
            })
        if method == "GET" and path == "/api/v3/openOrders":
            return self.result([])
        if method == "POST" and path == "/api/v3/order":
            with open(os.path.join(
                    self.receipt_dir, "momentum-position.json"
            ), encoding="utf-8") as handle:
                pending = json.load(handle)
            self.post_revisions.append(pending["revision"])
            self.assert_intent(pending, values)
            self.posts.append(values)
            side = values["side"]
            if side == "BUY":
                quote = Decimal(values["quoteOrderQty"])
                quantity, price = quote / self.ask, self.ask
                fee, fee_asset = Decimal("0.001"), "TST"
                self.base_free += quantity - fee
                self.quote_free -= quote
            else:
                quantity, price = Decimal(values["quantity"]), self.bid
                quote = quantity * price
                fee, fee_asset = Decimal("0.001"), "USDT"
                self.base_free -= quantity
                self.quote_free += quote - fee
            order_id = len(self.posts)
            order = {
                "symbol": "TSTUSDT", "status": "FILLED", "side": side,
                "clientOrderId": values["newClientOrderId"],
                "orderId": order_id, "executedQty": str(quantity),
                "cummulativeQuoteQty": str(quote),
            }
            fill = {
                "tradeId": order_id, "orderId": order_id,
                "qty": str(quantity), "price": str(price),
                "commission": str(fee), "commissionAsset": fee_asset,
                "isBuyer": side == "BUY", "isMaker": False,
                "time": 1786708800000,
            }
            self.orders[values["newClientOrderId"]] = order
            return self.result({**order, "fills": [fill]})
        if method == "GET" and path == "/api/v3/order":
            return self.result(self.orders[values["origClientOrderId"]])
        raise AssertionError("unexpected Spot adapter call")

    @staticmethod
    def assert_base(base_url):
        if base_url != jr.binance_http.SPOT_BASE_URL:
            raise AssertionError("unexpected Spot base URL")

    @staticmethod
    def assert_intent(position, params):
        intent = position["pending_intent"]["intent"]
        if intent["operation"] != "ORDER" or intent["git_sha"] != SHA:
            raise AssertionError("durable intent facts are incomplete")
        if intent["identity"] != params["newClientOrderId"]:
            raise AssertionError("durable intent identity differs")
        if intent["params"] != [list(pair) for pair in params.items()]:
            raise AssertionError("durable canonical parameters differ")


def services(transport):
    return jr.ExecutionServices(
        transport=transport,
        spot_base_url=jr.binance_http.SPOT_BASE_URL,
        futures_base_url=jr.binance_http.FUTURES_BASE_URL,
        sleep=lambda _delay: None,
        margin_build_borrow=jr.binance_margin.build_borrow,
        margin_build_repay=jr.binance_margin.build_repay,
        margin_read_history_by_direction=
        jr.binance_margin.read_history_by_direction,
        margin_read_history_by_tx_id=jr.binance_margin.read_history_by_tx_id,
        margin_read_liability=jr.binance_margin.read_liability,
        margin_submit_once=jr.binance_margin.submit_once,
        margin_confirm=jr.binance_margin.confirm,
        futures_read_market=jr.binance_futures.read_market,
        futures_read_preflight=jr.binance_futures.read_preflight,
        futures_set_leverage_1x_once=jr.binance_futures.set_leverage_1x_once,
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


class SpotMomentumAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.receipt_dir = temporary.name
        self.paths = jr._paths(self.receipt_dir)
        jr._make_dirs(self.paths)
        Path(self.paths["proof"]).touch()
        Path(self.paths["basis_proof"]).touch()
        with open(os.path.join(
                self.receipt_dir, "OPERATOR"
        ), "w", encoding="utf-8") as handle:
            json.dump({"operator": "lead"}, handle)
        self.transport = SpotTransport(self.receipt_dir)
        self.services = services(self.transport)
        environment = mock.patch.dict(os.environ, {
            "BINANCE_API_KEY": "offline-key",
            "BINANCE_API_SECRET": "offline-secret",
            "QUANT_SHA": SHA,
            "SYMBOL": "TSTUSDT",
            "MAX_NOTIONAL": "8",
            "RECEIPT_DIR": self.receipt_dir,
        }, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        governor = types.ModuleType("account_governor")
        governor.GovernorRefusal = RuntimeError
        governor.budgets_allow = lambda _receipt_dir: (True, "")
        governor.acquire_entry_slot = \
            lambda _operation, _symbol, _receipt_dir: "governor-token"
        governor.release_entry_slot = lambda _token, _receipt_dir: None
        governor.record_realized = lambda *_args: None
        governor_patch = mock.patch.dict(
            sys.modules, {"account_governor": governor}
        )
        governor_patch.start()
        self.addCleanup(governor_patch.stop)

    @staticmethod
    def job(job_id, operation):
        return jr.Job(
            job_id=job_id, op=operation, symbol="TSTUSDT",
            requested_max_notional=Decimal("8"),
            effective_notional=Decimal("8"),
            created_utc=NOW, expires_utc=NOW + timedelta(minutes=10),
            requested_by="lead", venue="spot",
        )

    def position(self):
        return jr._load_momentum_position(self.paths)

    def write_job(self, job_id, operation="MOMENTUM_OPEN"):
        if operation == "MOMENTUM_OPEN":
            with open(os.path.join(
                    self.receipt_dir, "ACCEPT-ONE"
            ), "w", encoding="utf-8") as handle:
                json.dump({
                    "operator": "lead", "max_notional": "8",
                    "created_utc": (NOW - timedelta(minutes=1)).isoformat()
                    .replace("+00:00", "Z"),
                    "expires_utc": (NOW + timedelta(minutes=10)).isoformat()
                    .replace("+00:00", "Z"),
                }, handle)
        with open(os.path.join(
                self.paths["jobs"], "job-%s.json" % job_id
        ), "w", encoding="utf-8") as handle:
            json.dump({
                "job_id": job_id, "op": operation, "symbol": "TSTUSDT",
                "max_notional": 8, "venue": "spot",
                "created_utc": (NOW - timedelta(minutes=1)).isoformat()
                .replace("+00:00", "Z"),
                "expires_utc": (NOW + timedelta(minutes=10)).isoformat()
                .replace("+00:00", "Z"),
                "requested_by": "lead",
            }, handle)

    def test_tick_forwards_the_frozen_spot_services(self):
        self.write_job("spot-service-tick")
        code = jr.tick(
            receipt_dir=self.receipt_dir, now=NOW,
            execution_services=self.services,
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.position()["state"], "OPEN")
        self.assertEqual(len(self.transport.posts), 1)

    def test_open_and_exit_use_durable_adapter_intents_and_owned_net_base(self):
        opened = jr._run_spot_momentum_open(
            self.job("spot-adapter-open", "MOMENTUM_OPEN"),
            self.receipt_dir, self.paths, NOW, "governor-token", None,
            self.services,
        )
        self.assertEqual((opened.verdict, opened.exit_code), ("OPEN", 0))
        position = self.position()
        self.assertEqual(position["measured_qty"], "0.079")
        self.assertEqual(self.transport.post_revisions, [2])
        self.assertEqual(self.transport.posts[0]["quoteOrderQty"], "8")

        closed = jr._run_spot_momentum_exit(
            self.job("spot-adapter-exit", "MOMENTUM_EXIT"),
            self.receipt_dir, self.paths, NOW, position,
            Decimal(position["measured_qty"]),
            execution_services=self.services,
        )
        self.assertEqual((closed.verdict, closed.exit_code), ("CLOSED", 0))
        self.assertEqual([row["side"] for row in self.transport.posts],
                         ["BUY", "SELL"])
        self.assertEqual(self.transport.posts[-1]["quantity"], "0.079")
        self.assertEqual(self.transport.base_free, Decimal("5.000"))
        self.assertEqual(self.transport.post_revisions, [2, 5])
        self.assertEqual(self.position()["state"], "CLOSED")

    def test_recovery_queries_only_and_clears_exact_absence(self):
        market = jr.binance_spot.SpotMarket(
            "TSTUSDT", "TST", "USDT", "TRADING", Decimal("0.001"),
            Decimal("0.001"), Decimal("5"), Decimal("99"), Decimal("100"),
        )
        plan = jr.binance_spot.build_market_order(
            market, "BUY", "l2f-recovery-enter_spot", SHA,
            quote_limit=Decimal("8"),
        )
        position = {
            "version": jr.MOMENTUM_STATE_VERSION, "revision": 0,
            "state": "OPENING", "venue": "spot", "symbol": "TSTUSDT",
            "job_id": "spot-recovery", "sha": SHA, "run_id": "recovery",
            "effective_notional": "8", "planned_qty": "0.08",
            "spot_market": {
                "base": "TST", "quote": "USDT", "step": "0.001",
                "min_qty": "0.001", "min_notional": "5",
                "apply_min": True, "starting_base_free": "5",
                "starting_quote_free": "100", "base_tolerance": "0",
            },
            "pending_intent": {
                "action": "OPEN", "client_id": plan.intent.identity,
                "intent": jr._spot_intent_payload(plan.intent),
                "persisted_utc": "2026-08-14T12:00:00Z",
                "governor_token": "governor-token",
            },
            "entry": None, "exit": None,
            "created_utc": "2026-08-14T12:00:00Z",
        }
        jr._save_momentum_position(self.paths, position, NOW)
        Path(os.path.join(
            self.paths["claims"], "job-spot-recovery.claim"
        )).touch()
        queries = []

        def query_only(_transport, _base, symbol, client_id, **_kwargs):
            queries.append((symbol, client_id))
            return jr.binance_http.OrderResult(
                "not_found", jr.binance_http.OrderIdentity(
                    symbol, client_id, None
                ), None, None, False, None, False, None, None, (), None,
            )

        def no_mutation(*_args, **_kwargs):
            raise AssertionError("recovery attempted a Spot mutation")

        recovery_services = replace(
            self.services, spot_query_order=query_only,
            spot_build_market_order=no_mutation,
            spot_submit_order_once=no_mutation,
        )
        recovered = jr._recover_pending_momentum(
            self.paths, self.receipt_dir, set(), NOW,
            execution_services=recovery_services,
        )
        self.assertEqual(queries, [("TSTUSDT", plan.intent.identity)])
        self.assertEqual((recovered[0], recovered[3].verdict),
                         ("spot-recovery", "REFUSED"))
        self.assertEqual(self.position()["state"], "NONE")

    def test_preflight_keeps_balance_and_open_order_fences(self):
        def account(free, orders):
            value = jr.binance_spot.SpotAccountState(
                True, orders, (
                    jr.binance_http.Balance(
                        "TST", Decimal("5"), Decimal(0), None
                    ),
                    jr.binance_http.Balance(
                        "USDT", free, Decimal(0), None
                    ),
                ),
            )
            return jr.binance_http.ReadResult("ok", value, None)

        for value in (account(Decimal("7"), ()),
                      account(Decimal("100"), (41,))):
            unavailable = replace(
                self.services,
                spot_read_account_state=lambda *_args, row=value,
                **_kwargs: row,
            )
            with self.assertRaises(jr.JobRefusal):
                jr._spot_market_context(
                    unavailable, "TSTUSDT", Decimal("8")
                )

    def test_unproved_market_read_does_not_become_zero(self):
        unclear = jr.binance_http.ReadResult(
            "unclear", None,
            jr.binance_http.HttpResult("unclear", None, None, "offline"),
        )
        unavailable = replace(
            self.services, spot_read_market=lambda *_args, **_kwargs: unclear
        )
        with self.assertRaises(jr.JobRefusal):
            jr._spot_market_context(
                unavailable, "TSTUSDT", Decimal("8")
            )

    def test_zero_fill_does_not_invent_an_average_price(self):
        order = jr.binance_http.OrderResult(
            "terminal", jr.binance_http.OrderIdentity(
                "TSTUSDT", "spot-zero-fill", 71
            ), "spot-zero-fill", "FILLED", True, "BUY", False,
            Decimal(0), Decimal(0), (), None,
        )
        facts = jr._spot_result_facts(order, "spot-zero-fill", "BUY")
        self.assertIsNone(facts["price"])


if __name__ == "__main__":
    unittest.main()
