#!/usr/bin/env python3
"""Offline policy tests for the Spot round-trip adapter consumer."""

import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
SPEC = importlib.util.spec_from_file_location(
    "spot_roundtrip_under_test", SRC / "spot_roundtrip.py")
spot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spot)
http = spot.binance_http
adapter = spot.binance_spot

SHA = "a" * 40
SYMBOL = "BTCUSDT"


def ok(value):
    return http.ReadResult("ok", value, None)


def unclear(note="offline uncertainty"):
    error = http.HttpResult("unclear", None, None, note)
    return http.ReadResult("unclear", None, error)


def market(bid="99", ask="100"):
    return adapter.SpotMarket(
        SYMBOL, "BTC", "USDT", "TRADING", Decimal("0.001"),
        Decimal("0.001"), Decimal("1"), Decimal(bid), Decimal(ask))


def account(base="5", quote="100", *, open_ids=(), can_trade=True,
            include_base=True):
    balances = []
    if include_base:
        balances.append(http.Balance("BTC", Decimal(base), Decimal(0), None))
    balances.append(http.Balance("USDT", Decimal(quote), Decimal(0), None))
    return adapter.SpotAccountState(can_trade, tuple(open_ids), tuple(balances))


def order(plan, order_id, executed, quote):
    return http.OrderResult(
        "terminal", http.OrderIdentity(SYMBOL, plan.intent.identity, order_id),
        plan.intent.identity, "FILLED", True, plan.side, False,
        Decimal(executed), Decimal(quote), (), None)


class OfflineTransport:
    def __init__(self, test_results=None):
        rates = {
            "standardCommissionForOrder": {"taker": "0.001"},
            "taxCommissionForOrder": {"taker": "0"},
            "specialCommissionForOrder": {"taker": "0"}}
        self.test_results = list(test_results or [
            http.HttpResult("ok", rates, None, None)])
        self.calls = []

    def sync_time(self, base_url, path):
        self.calls.append(("SYNC", base_url, path, None, False))
        return http.HttpResult("ok", {"serverTime": 1}, None, None)

    def __call__(self, method, base_url, path, params=None, signed=False):
        self.calls.append((method, base_url, path, params, signed))
        if path != "/api/v3/order/test" or not self.test_results:
            raise AssertionError("unexpected offline transport call")
        return self.test_results.pop(0)


class SpotRoundtripCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        output = mock.patch("sys.stdout", new=io.StringIO())
        output.start()
        self.addCleanup(output.stop)
        receipt_dir = self.temporary.name
        values = {
            "BASE_URL": "https://spot.test", "API_KEY": "offline-key",
            "API_SECRET": "offline-secret", "SYMBOL": SYMBOL,
            "MAX_QUOTE_RAW": "10", "QUANT_SHA": SHA,
            "RECEIPT_DIR": receipt_dir,
            "LOCK_PATH": os.path.join(receipt_dir, "run.lock"),
            "receipt": {}, "receipt_path": "", "ctx": {"lock": False},
            "transport": None}
        for name, value in values.items():
            setattr(spot, name, value)

        def deny(*_args, **_kwargs):
            raise AssertionError("offline test attempted a network call")

        for target, name in ((socket, "socket"), (socket, "create_connection"),
                             (http.urllib.request, "urlopen")):
            patcher = mock.patch.object(target, name, deny)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, *, buy_tag="terminal", sell_tag="terminal",
             start_state=None, final_state=None, test_results=None,
             refuse_sell_build=False):
        wire = OfflineTransport(test_results)
        initial = start_state or account()
        final = final_state if final_state is not None else ok(account("5", "99.89"))
        account_results = [ok(initial), final]
        markets = [ok(market()), ok(market("98", "99"))]
        plans, events = [], []
        buy_fill = http.Fill(
            1, 11, Decimal("100"), Decimal("0.1"), Decimal("10"),
            http.Fee("BTC", Decimal("0.001")), "BUY", False, 1, None)
        sell_fill = http.Fill(
            2, 12, Decimal("100"), Decimal("0.099"), Decimal("9.9"),
            http.Fee("USDT", Decimal("0.01")), "SELL", False, 2, None)

        def submit(_transport, _base_url, plan, writer):
            plans.append(plan)
            writer(plan.intent)
            with open(spot.receipt_path, encoding="utf-8") as stream:
                durable = json.load(stream)[plan.side.lower()]["intent"]
            events.append(("durable", durable["identity"]))
            events.append(("wire", plan.intent.identity))
            tag = buy_tag if plan.side == "BUY" else sell_tag
            if tag == "terminal":
                return order(plan, 11 if plan.side == "BUY" else 12,
                             "0.1" if plan.side == "BUY" else "0.099",
                             "10" if plan.side == "BUY" else "9.9")
            error = http.HttpResult("api_error", None, -1013, "filter refusal")
            identity = http.OrderIdentity(SYMBOL, plan.intent.identity, None)
            return http.OrderResult(
                tag, identity, None, None, False, plan.side, False,
                None, None, (), error if tag == "error" else None)

        def fills(_transport, _base_url, result):
            selected = (buy_fill,) if result.side == "BUY" else (sell_fill,)
            executed = result.executed_quantity
            coverage = http.FillCoverage(
                selected, executed, executed, True,
                adapter.fees_by_asset(selected))
            return ok(coverage)

        original_builder = adapter.build_market_order

        def build(*args, **kwargs):
            if refuse_sell_build and args[1] == "SELL":
                raise http.AdapterRefusal("fresh sell filter refusal")
            return original_builder(*args, **kwargs)

        patches = (
            mock.patch.object(spot.binance_http, "BinanceHttp", return_value=wire),
            mock.patch.object(spot.binance_http, "read", return_value=
                              http.HttpResult("ok", {"price": "100"}, None, None)),
            mock.patch.object(spot.binance_spot, "read_market", side_effect=markets),
            mock.patch.object(spot.binance_spot, "read_account_state",
                              side_effect=account_results),
            mock.patch.object(spot.binance_spot, "build_market_order", side_effect=build),
            mock.patch.object(spot.binance_spot, "submit_order_once", side_effect=submit),
            mock.patch.object(spot.binance_spot, "read_fills", side_effect=fills),
            mock.patch.object(spot.time, "time", return_value=1700000000),
            mock.patch.object(spot.secrets, "token_hex", return_value="a1b2c3"))
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], \
                self.assertRaises(SystemExit) as stopped:
            spot.main()
        with open(spot.receipt_path, encoding="utf-8") as stream:
            stored = json.load(stream)
        return stopped.exception.code, stored, plans, events, wire

    def test_config_refusal_precedes_transport_and_receipt(self):
        spot.QUANT_SHA = "short"
        with mock.patch.object(spot.binance_http, "BinanceHttp") as factory, \
                self.assertRaises(SystemExit) as stopped:
            spot.main()
        self.assertEqual(stopped.exception.code, 2)
        factory.assert_not_called()
        self.assertEqual(os.listdir(spot.RECEIPT_DIR), [])

    def test_intent_writer_syncs_file_and_directory_and_keeps_param_order(self):
        run_id, client_id = "owned-run", "owned-buy"
        spot.receipt_path = os.path.join(spot.RECEIPT_DIR, "roundtrip-owned.json")
        spot.receipt = {"run_id": run_id, "buy_client_id": client_id,
                        "sell_client_id": "owned-sell", "buy": {}}
        spot.ctx = {"lock": True}
        with open(spot.LOCK_PATH, "wb") as stream:
            stream.write(run_id.encode())
        params = (("symbol", SYMBOL), ("side", "BUY"),
                  ("newClientOrderId", client_id))
        intent = http.MutationIntent(
            "BINANCE_SPOT", "ORDER", client_id, SHA, params)
        real_fsync = os.fsync
        with mock.patch.object(spot.os, "fsync", wraps=real_fsync) as sync:
            spot.write_intent(intent)
        self.assertEqual(sync.call_count, 2)
        with open(spot.receipt_path, encoding="utf-8") as stream:
            stored = json.load(stream)
        self.assertEqual(stored["buy"]["intent"]["params"],
                         [list(pair) for pair in params])

    def test_closed_flow_uses_fresh_market_and_run_owned_net_base(self):
        code, stored, plans, events, wire = self._run()
        self.assertEqual((code, stored["verdict"]), (0, "CLOSED"))
        self.assertEqual([plan.side for plan in plans], ["BUY", "SELL"])
        self.assertEqual(plans[1].quantity, Decimal("0.099"))
        self.assertEqual(plans[1].residual, Decimal("0.000"))
        self.assertEqual([event[0] for event in events],
                         ["durable", "wire", "durable", "wire"])
        self.assertEqual(sum(call[2] == "/api/v3/order/test" for call in wire.calls), 1)
        self.assertFalse(os.path.exists(spot.LOCK_PATH))
        self.assertEqual(stored["result"]["final_base_free"], "5")

    def test_pending_buy_maps_unknown_and_retains_lock(self):
        code, stored, plans, _events, _wire = self._run(buy_tag="pending")
        self.assertEqual((code, stored["verdict"], len(plans),
                          stored["result"]["residual_base_from_this_run"]),
                         (4, "UNKNOWN", 1, "UNKNOWN"))
        self.assertTrue(os.path.exists(spot.LOCK_PATH))

    def test_pending_sell_keeps_residual_unknown_and_retains_lock(self):
        code, stored, plans, _events, _wire = self._run(sell_tag="pending")
        self.assertEqual((code, stored["verdict"], len(plans),
                          stored["result"]["residual_base_from_this_run"]),
                         (4, "UNKNOWN", 2, "UNKNOWN"))
        self.assertTrue(os.path.exists(spot.LOCK_PATH))

    def test_definite_buy_error_maps_rejected_and_clears_lock(self):
        code, stored, plans, _events, _wire = self._run(buy_tag="error")
        self.assertEqual((code, stored["verdict"], len(plans)),
                         (2, "REJECTED_BEFORE_BUY", 1))
        self.assertFalse(os.path.exists(spot.LOCK_PATH))

    def test_failed_final_account_proof_downgrades_close_to_unknown(self):
        code, stored, _plans, _events, _wire = self._run(final_state=unclear())
        self.assertEqual((code, stored["verdict"]), (4, "UNKNOWN"))
        self.assertEqual(stored["result"]["final_base_free"], "UNKNOWN")
        self.assertTrue(os.path.exists(spot.LOCK_PATH))

    def test_missing_start_balance_is_not_treated_as_zero(self):
        code, stored, plans, _events, _wire = self._run(
            start_state=account(include_base=False))
        self.assertEqual((code, stored["verdict"], plans),
                         (2, "REJECTED_BEFORE_BUY", []))

    def test_fresh_sell_builder_refusal_keeps_proven_base_open(self):
        code, stored, plans, _events, _wire = self._run(refuse_sell_build=True)
        self.assertEqual((code, stored["verdict"], len(plans)),
                         (3, "OPEN_EXPOSURE", 1))
        self.assertEqual(stored["result"]["residual_base_from_this_run"], "0.099")
        self.assertTrue(os.path.exists(spot.LOCK_PATH))

    def test_nonmutating_test_order_repeats_once_then_refuses(self):
        uncertain = http.HttpResult("unclear", None, None, "offline")
        code, stored, plans, _events, wire = self._run(
            test_results=[uncertain, uncertain])
        self.assertEqual((code, stored["verdict"], plans),
                         (2, "REJECTED_BEFORE_BUY", []))
        self.assertEqual(sum(call[2] == "/api/v3/order/test" for call in wire.calls), 2)


if __name__ == "__main__":
    unittest.main()
