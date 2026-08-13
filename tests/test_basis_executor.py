"""Deterministic offline tests for src/basis_executor.py — injected transport
only; a module-level socket guard makes any real network attempt fail fast."""

import io
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_executor  # noqa: E402
import events  # noqa: E402
from basis_executor import ExecutorConfig  # noqa: E402

SYMBOL = "BTCUSDT"
SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
RID = "cafe01"
SPOT_URL = basis_executor.SPOT.base_url
USDM_URL = basis_executor.USDM.base_url

_real_getaddrinfo = socket.getaddrinfo


def setUpModule():
    def deny(*args, **kwargs):
        raise AssertionError("network access attempted in offline tests")
    socket.getaddrinfo = deny


def tearDownModule():
    socket.getaddrinfo = _real_getaddrinfo


def term_spot(status, executed, quote, order_id):
    def responder(params):
        return ("ok", {"clientOrderId": params["origClientOrderId"],
                       "status": status, "executedQty": executed,
                       "cummulativeQuoteQty": quote, "orderId": order_id})
    return responder


def term_usdm(status, executed, quote, order_id):
    def responder(params):
        return ("ok", {"clientOrderId": params["origClientOrderId"],
                       "status": status, "executedQty": executed,
                       "cumQuote": quote, "orderId": order_id})
    return responder


NEW_ORDER = ("ok", {"status": "NEW"})
MISSING = ("api_error", {"code": -2013, "msg": "Order does not exist."})


def happy_spec():
    """Full-cycle fixture: buy 0.099, fee 0.000099 BTC, hedge 0.098, close and
    sell it all back; residual 0.000901 stays below the 0.001 shared step."""
    return {
        "spot_info": {"symbols": [{
            "symbol": SYMBOL, "status": "TRADING", "isSpotTradingAllowed": True,
            "orderTypes": ["LIMIT", "MARKET"],
            "quoteOrderQtyMarketAllowed": True,
            "baseAsset": "BTC", "quoteAsset": "USDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.00010000",
                 "minQty": "0.00010000"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00010000",
                 "minQty": "0.00010000"},
                {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
            ]}]},
        "usdm_info": {"symbols": [{
            "symbol": SYMBOL, "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT", "marginAsset": "USDT",
            "orderTypes": ["LIMIT", "MARKET"],
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001",
                 "minQty": "0.001"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001",
                 "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]}]},
        "spot_book": {"symbol": SYMBOL, "bidPrice": "99.9",
                      "askPrice": "100.0"},
        "usdm_book": {"symbol": SYMBOL, "bidPrice": "100.2",
                      "askPrice": "100.3", "time": 1755000000000},
        "premium": {"symbol": SYMBOL, "lastFundingRate": "0.0001",
                    "nextFundingTime": 1755003600000},
        "my_trades": {},
        "legs": {
            "enter_spot": {
                "submit": ("ok", {"fills": [
                    {"qty": "0.09900000", "commission": "0.00009900",
                     "commissionAsset": "BTC"}]}),
                "query": [term_spot("FILLED", "0.09900000", "9.90000000",
                                    111222)],
            },
            "enter_short": {
                "submit": ("ok", {}),
                "query": [term_usdm("FILLED", "0.098", "9.81960000", 444555)],
            },
            "exit_short": {
                "submit": ("ok", {}),
                "query": [term_usdm("FILLED", "0.098", "9.82000000", 444556)],
            },
            "exit_spot": {
                "submit": ("ok", {"fills": [
                    {"qty": "0.09800000", "commission": "0.00979000",
                     "commissionAsset": "USDT"}]}),
                "query": [term_spot("FILLED", "0.09800000", "9.79020000",
                                    111223)],
            },
            "unwind_spot": {
                "submit": ("ok", {"fills": [
                    {"qty": "0.09800000", "commission": "0.00979000",
                     "commissionAsset": "USDT"}]}),
                "query": [term_spot("FILLED", "0.09800000", "9.79020000",
                                    111224)],
            },
        },
    }


class ScriptedTransport:
    """Injected transport with the (kind, payload) contract of the live one."""

    def __init__(self, spec):
        self.spec = spec
        self.calls = []
        self.posts = []
        self.query_counts = {}

    def __call__(self, method, base_url, path, params=None, signed=False):
        params = dict(params or {})
        self.calls.append((method, base_url, path, params, signed))
        if method == "POST":
            self.posts.append((base_url, params))
            stage = params["newClientOrderId"].split("-", 2)[2]
            entry = self.spec["legs"][stage].get("submit", ("ok", {}))
            return entry(params) if callable(entry) else entry
        if path.endswith("exchangeInfo"):
            key = "spot_info" if base_url == SPOT_URL else "usdm_info"
            return ("ok", self.spec[key])
        if path.endswith("ticker/bookTicker"):
            key = "spot_book" if base_url == SPOT_URL else "usdm_book"
            return ("ok", self.spec[key])
        if path.endswith("premiumIndex"):
            return ("ok", self.spec["premium"])
        if path.endswith("myTrades"):
            trades = self.spec.get("my_trades", {}).get(
                str(params.get("orderId")), [])
            return ("ok", trades)
        if path.endswith("positionRisk"):
            position = self.spec.get(
                "position", [{"symbol": SYMBOL, "positionAmt": "0.000"}])
            return position if isinstance(position, tuple) else ("ok", position)
        if path.endswith("/order"):
            cid = params["origClientOrderId"]
            stage = cid.split("-", 2)[2]
            sequence = self.spec["legs"][stage]["query"]
            index = self.query_counts.get(cid, 0)
            self.query_counts[cid] = index + 1
            entry = sequence[index] if index < len(sequence) else sequence[-1]
            return entry(params) if callable(entry) else entry
        raise AssertionError("unexpected GET %s" % path)


def post_stages(transport):
    return [params["newClientOrderId"].split("-", 2)[2]
            for _, params in transport.posts]


class ExecutorCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = holder.name
        self.cfg = ExecutorConfig(symbol=SYMBOL, sha=SHA,
                                  receipt_dir=self.dir, run_id=RID)

    def run_once(self, spec):
        transport = ScriptedTransport(spec)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = basis_executor.run_once(self.cfg, transport,
                                           sleep=lambda seconds: None)
        return code, transport, buffer.getvalue()

    def receipt(self):
        path = os.path.join(self.dir, "basis-executor-%s.json" % RID)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def events_list(self):
        path = os.path.join(self.dir, "events.jsonl")
        with open(path, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line]
        parsed = [json.loads(line) for line in lines]
        for event in parsed:
            events.validate_event(event)
        return parsed

    def lock_exists(self):
        return os.path.exists(os.path.join(self.dir, "basis-executor.lock"))


class PlanRefusalTests(ExecutorCase):
    def test_gap_below_min_refuses_before_any_order(self):
        spec = happy_spec()
        spec["usdm_book"] = {"symbol": SYMBOL, "bidPrice": "100.001",
                             "askPrice": "100.05", "time": 1755000000000}
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 2)
        self.assertEqual(transport.posts, [])
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "REJECTED_BEFORE_ENTRY")
        kinds = [event["kind"] for event in self.events_list()]
        self.assertEqual(kinds, ["run_start", "gap_read", "run_verdict"])
        self.assertFalse(self.lock_exists())

    def test_min_notional_refusal(self):
        spec = happy_spec()
        spec["spot_info"]["symbols"][0]["filters"][2]["minNotional"] = "50"
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 2)
        self.assertEqual(transport.posts, [])

    def test_min_quantity_refusal(self):
        spec = happy_spec()
        spec["usdm_info"]["symbols"][0]["filters"][0]["minQty"] = "0.5"
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 2)
        self.assertEqual(transport.posts, [])
        self.assertIn("minimum quantity", self.receipt()["verdict_note"])

    def test_crossed_spot_book_refuses(self):
        spec = happy_spec()
        spec["spot_book"] = {"symbol": SYMBOL, "bidPrice": "100.1",
                             "askPrice": "100.0"}
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 2)
        self.assertEqual(transport.posts, [])
        self.assertIn("basis read refused", self.receipt()["verdict_note"])

    def test_non_perpetual_contract_refuses(self):
        spec = happy_spec()
        spec["usdm_info"]["symbols"][0]["contractType"] = "CURRENT_QUARTER"
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 2)
        self.assertEqual(transport.posts, [])
        kinds = [event["kind"] for event in self.events_list()]
        self.assertEqual(kinds, ["run_start", "run_verdict"])


class HappyPathTests(ExecutorCase):
    def test_full_cycle_closes(self):
        code, transport, _ = self.run_once(happy_spec())
        self.assertEqual(code, 0)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short", "exit_short",
                          "exit_spot"])
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "CLOSED")
        self.assertEqual(receipt["exposure"], "none")
        self.assertFalse(self.lock_exists())
        self.assertEqual(
            [base for base, _ in transport.posts],
            [SPOT_URL, USDM_URL, USDM_URL, SPOT_URL])

    def test_fee_aware_net_base_sizes_every_dependent_leg(self):
        code, transport, _ = self.run_once(happy_spec())
        self.assertEqual(code, 0)
        acquired = self.receipt()["acquired"]
        self.assertEqual(Decimal(acquired["executed_qty"]), Decimal("0.099"))
        self.assertEqual(Decimal(acquired["base_fee"]), Decimal("0.000099"))
        self.assertEqual(Decimal(acquired["net_base"]), Decimal("0.098901"))
        self.assertEqual(Decimal(acquired["hedge_quantity"]), Decimal("0.098"))
        for stage in ("enter_short", "exit_short", "exit_spot"):
            index = post_stages(transport).index(stage)
            self.assertEqual(
                Decimal(transport.posts[index][1]["quantity"]),
                Decimal("0.098"))
        # the plan estimate (0.099) is never an order size
        estimate = self.receipt()["plan"]["estimated_quantity"]
        self.assertEqual(Decimal(estimate), Decimal("0.099"))

    def test_wire_shapes_per_leg(self):
        _, transport, _ = self.run_once(happy_spec())
        by_stage = {params["newClientOrderId"].split("-", 2)[2]: params
                    for _, params in transport.posts}
        buy = by_stage["enter_spot"]
        self.assertEqual(buy["side"], "BUY")
        self.assertEqual(buy["quoteOrderQty"], "9.90000000")
        self.assertNotIn("quantity", buy)
        self.assertEqual(buy["newOrderRespType"], "FULL")
        self.assertNotIn("market", buy)
        short = by_stage["enter_short"]
        self.assertEqual(short["side"], "SELL")
        self.assertNotIn("reduceOnly", short)
        self.assertEqual(short["newOrderRespType"], "RESULT")
        close = by_stage["exit_short"]
        self.assertEqual(close["side"], "BUY")
        self.assertEqual(close["reduceOnly"], "true")
        sell = by_stage["exit_spot"]
        self.assertEqual(sell["side"], "SELL")
        self.assertEqual(sell["newOrderRespType"], "FULL")

    def test_client_ids_unique_and_bc_prefixed(self):
        _, transport, _ = self.run_once(happy_spec())
        ids = [params["newClientOrderId"] for _, params in transport.posts]
        self.assertEqual(len(set(ids)), 4)
        for value in ids:
            self.assertTrue(value.startswith("bc-%s-" % RID))
        self.assertEqual(len(self.receipt()["client_ids"]), 5)

    def test_events_are_contract_v1_and_ordered(self):
        code, _, _ = self.run_once(happy_spec())
        self.assertEqual(code, 0)
        listed = self.events_list()
        kinds = [event["kind"] for event in listed]
        self.assertEqual(kinds, [
            "run_start", "gap_read",
            "order_submitted", "order_terminal",
            "order_submitted", "order_terminal",
            "order_submitted", "order_terminal",
            "order_submitted", "order_terminal",
            "run_verdict"])
        for event in listed:
            self.assertEqual(event["sha8"], SHA[:8])
            self.assertEqual(event["run_id"], RID)
            self.assertEqual(event["symbol"], SYMBOL)
        verdict = listed[-1]
        self.assertEqual(verdict["verdict"], "CLOSED")
        self.assertEqual(Decimal(verdict["residual"]), Decimal("0.000901"))
        buy_terminal = listed[3]
        self.assertEqual(buy_terminal["side"], "BUY")
        self.assertEqual(buy_terminal["client_id_last4"], "spot")
        self.assertEqual(Decimal(buy_terminal["executed"]), Decimal("0.099"))
        self.assertEqual(buy_terminal["fees"], {"BTC": "0.00009900"})

    def test_receipt_records_result_and_final_position(self):
        self.run_once(happy_spec())
        receipt = self.receipt()
        self.assertEqual(
            Decimal(receipt["result"]["residual_base_from_this_run"]),
            Decimal("0.000901"))
        self.assertEqual(Decimal(receipt["final_futures_position"]),
                         Decimal("0"))
        self.assertEqual(receipt["outcomes"], {
            "ENTER_SPOT": "FILLED", "ENTER_SHORT": "FILLED",
            "EXIT_SHORT": "FILLED", "EXIT_SPOT": "FILLED"})


class PartialFillTests(ExecutorCase):
    def test_partial_spot_fill_sizes_from_exchange_truth(self):
        spec = happy_spec()
        spec["legs"]["enter_spot"] = {
            "submit": ("ok", {"fills": [
                {"qty": "0.05000000", "commission": "0.00005000",
                 "commissionAsset": "BTC"}]}),
            "query": [term_spot("EXPIRED", "0.05000000", "5.00000000",
                                111222)],
        }
        spec["legs"]["enter_short"]["query"] = [
            term_usdm("FILLED", "0.049", "4.90980000", 444555)]
        spec["legs"]["exit_short"]["query"] = [
            term_usdm("FILLED", "0.049", "4.91000000", 444556)]
        spec["legs"]["exit_spot"] = {
            "submit": ("ok", {"fills": [
                {"qty": "0.04900000", "commission": "0.00489000",
                 "commissionAsset": "USDT"}]}),
            "query": [term_spot("FILLED", "0.04900000", "4.89510000",
                                111223)],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 0)
        by_stage = {params["newClientOrderId"].split("-", 2)[2]: params
                    for _, params in transport.posts}
        self.assertEqual(Decimal(by_stage["enter_short"]["quantity"]),
                         Decimal("0.049"))
        acquired = self.receipt()["acquired"]
        self.assertEqual(Decimal(acquired["net_base"]), Decimal("0.04995"))
        self.assertEqual(Decimal(acquired["hedge_quantity"]),
                         Decimal("0.049"))

    def test_partial_short_fill_closes_exact_fill(self):
        spec = happy_spec()
        spec["legs"]["enter_short"]["query"] = [
            term_usdm("EXPIRED", "0.060", "6.01200000", 444555)]
        spec["legs"]["exit_short"]["query"] = [
            term_usdm("FILLED", "0.060", "6.01300000", 444556)]
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 0)
        by_stage = {params["newClientOrderId"].split("-", 2)[2]: params
                    for _, params in transport.posts}
        self.assertEqual(Decimal(by_stage["enter_short"]["quantity"]),
                         Decimal("0.098"))
        self.assertEqual(Decimal(by_stage["exit_short"]["quantity"]),
                         Decimal("0.060"))
        self.assertEqual(Decimal(by_stage["exit_spot"]["quantity"]),
                         Decimal("0.098"))
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "CLOSED")
        self.assertEqual(receipt["first_failure"]["where"], "enter_short")

    def test_partial_reduce_only_close_freezes_without_spot_sale(self):
        spec = happy_spec()
        spec["legs"]["exit_short"]["query"] = [
            term_usdm("EXPIRED", "0.030", "3.00600000", 444556)]
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 3)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short", "exit_short"])
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "OPEN_EXPOSURE")
        self.assertIn("0.030", receipt["exposure"])
        self.assertIn("0.098", receipt["exposure"])
        self.assertTrue(self.lock_exists())

    def test_partial_exit_sell_reports_residual_exposure(self):
        spec = happy_spec()
        spec["legs"]["exit_spot"] = {
            "submit": ("ok", {"fills": [
                {"qty": "0.05000000", "commission": "0.00500000",
                 "commissionAsset": "USDT"}]}),
            "query": [term_spot("EXPIRED", "0.05000000", "4.99500000",
                                111223)],
        }
        code, _, _ = self.run_once(spec)
        self.assertEqual(code, 3)
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "OPEN_EXPOSURE")
        self.assertEqual(
            Decimal(receipt["result"]["residual_base_from_this_run"]),
            Decimal("0.048901"))
        verdict = self.events_list()[-1]
        self.assertEqual(Decimal(verdict["residual"]), Decimal("0.048901"))


class ShortRejectionTests(ExecutorCase):
    def rejecting_short(self):
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("api_error", {"code": -2019,
                                     "msg": "Margin is insufficient."}),
            "query": [MISSING],
        }
        return spec

    def test_short_rejected_unwinds_spot(self):
        code, transport, _ = self.run_once(self.rejecting_short())
        self.assertEqual(code, 2)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short", "unwind_spot"])
        self.assertEqual(transport.posts[-1][0], SPOT_URL)
        self.assertEqual(Decimal(transport.posts[-1][1]["quantity"]),
                         Decimal("0.098"))
        self.assertEqual(transport.posts[-1][1]["side"], "SELL")
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "REJECTED_BEFORE_ENTRY")
        self.assertIn("unwound", receipt["verdict_note"])
        self.assertFalse(self.lock_exists())

    def test_short_zero_fill_unwinds(self):
        spec = happy_spec()
        spec["legs"]["enter_short"]["query"] = [
            term_usdm("EXPIRED", "0.000", "0", 444555)]
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 2)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short", "unwind_spot"])
        self.assertEqual(self.receipt()["verdict"], "REJECTED_BEFORE_ENTRY")

    def test_unwind_rejected_reports_open_exposure(self):
        spec = self.rejecting_short()
        spec["legs"]["unwind_spot"] = {
            "submit": ("api_error", {"code": -2010,
                                     "msg": "Account has insufficient balance."}),
            "query": [MISSING],
        }
        code, _, _ = self.run_once(spec)
        self.assertEqual(code, 3)
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "OPEN_EXPOSURE")
        self.assertIn("no hedge", receipt["exposure"])
        self.assertTrue(self.lock_exists())

    def test_partial_unwind_reports_open_exposure(self):
        spec = self.rejecting_short()
        spec["legs"]["unwind_spot"] = {
            "submit": ("ok", {"fills": [
                {"qty": "0.05000000", "commission": "0.00500000",
                 "commissionAsset": "USDT"}]}),
            "query": [term_spot("EXPIRED", "0.05000000", "4.99500000",
                                111224)],
        }
        code, _, _ = self.run_once(spec)
        self.assertEqual(code, 3)
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "OPEN_EXPOSURE")
        self.assertEqual(
            Decimal(receipt["result"]["residual_base_from_this_run"]),
            Decimal("0.048901"))


class UnknownLegTests(ExecutorCase):
    """For every leg: an unclear submission that the client-id query resolves
    continues the cycle; one that stays unresolved freezes as UNKNOWN."""

    def test_spot_buy_unclear_submission_query_resolves(self):
        spec = happy_spec()
        spec["legs"]["enter_spot"] = {
            "submit": ("unclear", {}),
            "query": [NEW_ORDER,
                      term_spot("FILLED", "0.09900000", "9.90000000", 111222)],
        }
        spec["my_trades"]["111222"] = [
            {"qty": "0.09900000", "commission": "0.00009900",
             "commissionAsset": "BTC"}]
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 0)
        self.assertEqual(self.receipt()["verdict"], "CLOSED")
        self.assertEqual(len(transport.posts), 4)

    def test_spot_buy_unknown_freezes(self):
        spec = happy_spec()
        spec["legs"]["enter_spot"] = {
            "submit": ("unclear", {}),
            "query": [NEW_ORDER],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 4)
        self.assertEqual(len(transport.posts), 1)
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "UNKNOWN")
        self.assertIn("bc-%s-enter_spot" % RID, receipt["exposure"])
        self.assertIn("quoteOrderQty", receipt["exposure"])
        self.assertTrue(self.lock_exists())
        self.assertEqual(self.events_list()[-1]["verdict"], "UNKNOWN")

    def test_short_unclear_submission_query_resolves(self):
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("unclear", {}),
            "query": [NEW_ORDER,
                      term_usdm("FILLED", "0.098", "9.81960000", 444555)],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 0)
        self.assertEqual(len(transport.posts), 4)

    def test_short_unknown_freezes_without_unwind(self):
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("unclear", {}),
            "query": [NEW_ORDER],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 4)
        self.assertEqual(post_stages(transport), ["enter_spot", "enter_short"])
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "UNKNOWN")
        self.assertIn("either", receipt["exposure"])
        self.assertTrue(self.lock_exists())

    def test_close_unclear_submission_query_resolves(self):
        spec = happy_spec()
        spec["legs"]["exit_short"] = {
            "submit": ("unclear", {}),
            "query": [NEW_ORDER,
                      term_usdm("FILLED", "0.098", "9.82000000", 444556)],
        }
        code, _, _ = self.run_once(spec)
        self.assertEqual(code, 0)

    def test_close_unknown_freezes_without_spot_sale(self):
        spec = happy_spec()
        spec["legs"]["exit_short"] = {
            "submit": ("unclear", {}),
            "query": [NEW_ORDER],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 4)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short", "exit_short"])
        self.assertIn("either", self.receipt()["exposure"])

    def test_exit_sell_unclear_submission_query_resolves(self):
        spec = happy_spec()
        spec["legs"]["exit_spot"] = {
            "submit": ("unclear", {}),
            "query": [NEW_ORDER,
                      term_spot("FILLED", "0.09800000", "9.79020000", 111223)],
        }
        spec["my_trades"]["111223"] = [
            {"qty": "0.09800000", "commission": "0.00979000",
             "commissionAsset": "USDT"}]
        code, _, _ = self.run_once(spec)
        self.assertEqual(code, 0)
        self.assertEqual(self.receipt()["verdict"], "CLOSED")

    def test_exit_sell_unknown_freezes(self):
        spec = happy_spec()
        spec["legs"]["exit_spot"] = {
            "submit": ("unclear", {}),
            "query": [NEW_ORDER],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 4)
        self.assertEqual(len(transport.posts), 4)
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "UNKNOWN")
        self.assertIn("either flat or holding", receipt["exposure"])

    def test_unwind_unknown_freezes(self):
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("api_error", {"code": -2019,
                                     "msg": "Margin is insufficient."}),
            "query": [MISSING],
        }
        spec["legs"]["unwind_spot"] = {
            "submit": ("unclear", {}),
            "query": [NEW_ORDER],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 4)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short", "unwind_spot"])
        self.assertIn("either", self.receipt()["exposure"])

    def test_query_error_is_unknown_never_a_resend(self):
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("ok", {}),
            "query": [("api_error", {"code": -1003,
                                     "msg": "Too many requests."})],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 4)
        self.assertEqual(post_stages(transport), ["enter_spot", "enter_short"])
        self.assertEqual(self.receipt()["first_failure"]["where"],
                         "query_order")


class SpotBuyEdgeTests(ExecutorCase):
    def test_zero_fill_spot_buy_rejects_cleanly(self):
        spec = happy_spec()
        spec["legs"]["enter_spot"] = {
            "submit": ("ok", {}),
            "query": [term_spot("EXPIRED", "0.00000000", "0.00000000",
                                111222)],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 2)
        self.assertEqual(len(transport.posts), 1)
        self.assertEqual(self.receipt()["verdict"], "REJECTED_BEFORE_ENTRY")
        self.assertFalse(self.lock_exists())

    def test_spot_buy_definite_rejection(self):
        spec = happy_spec()
        spec["legs"]["enter_spot"] = {
            "submit": ("api_error", {"code": -2010,
                                     "msg": "Account has insufficient balance."}),
            "query": [MISSING],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 2)
        self.assertEqual(len(transport.posts), 1)
        self.assertEqual(self.receipt()["verdict"], "REJECTED_BEFORE_ENTRY")

    def test_uncovered_buy_trades_freeze_holding(self):
        spec = happy_spec()
        spec["legs"]["enter_spot"] = {
            "submit": ("ok", {}),
            "query": [term_spot("FILLED", "0.09900000", "9.90000000",
                                111222)],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 3)
        self.assertEqual(len(transport.posts), 1)
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "OPEN_EXPOSURE")
        self.assertIn("assumed fee is forbidden", receipt["verdict_note"])
        self.assertTrue(self.lock_exists())

    def test_dust_net_base_freezes_before_short(self):
        spec = happy_spec()
        spec["legs"]["enter_spot"] = {
            "submit": ("ok", {"fills": [
                {"qty": "0.00100000", "commission": "0.00000100",
                 "commissionAsset": "BTC"}]}),
            "query": [term_spot("EXPIRED", "0.00100000", "0.10000000",
                                111222)],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 3)
        self.assertEqual(len(transport.posts), 1)
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "OPEN_EXPOSURE")
        self.assertIn("dust below the shared step", receipt["exposure"])

    def test_below_futures_minimum_unwinds_without_usdm_order(self):
        spec = happy_spec()
        spec["usdm_info"]["symbols"][0]["filters"][0]["minQty"] = "0.09"
        spec["usdm_info"]["symbols"][0]["filters"][1]["minQty"] = "0.09"
        spec["legs"]["enter_spot"] = {
            "submit": ("ok", {"fills": [
                {"qty": "0.08000000", "commission": "0.00008000",
                 "commissionAsset": "BTC"}]}),
            "query": [term_spot("EXPIRED", "0.08000000", "8.00000000",
                                111222)],
        }
        spec["legs"]["unwind_spot"] = {
            "submit": ("ok", {"fills": [
                {"qty": "0.07900000", "commission": "0.00790000",
                 "commissionAsset": "USDT"}]}),
            "query": [term_spot("FILLED", "0.07900000", "7.89210000",
                                111224)],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 2)
        self.assertEqual(post_stages(transport), ["enter_spot", "unwind_spot"])
        self.assertNotIn(USDM_URL, [base for base, _ in transport.posts])
        legs = self.receipt()["legs"]
        self.assertEqual(legs[1]["stage"], "ENTER_SHORT")
        self.assertEqual(legs[1]["status"], "NOT_SUBMITTED")


class FinalStateTests(ExecutorCase):
    def test_nonzero_final_position_downgrades_to_exposure(self):
        spec = happy_spec()
        spec["position"] = [{"symbol": SYMBOL, "positionAmt": "-0.002"}]
        code, _, _ = self.run_once(spec)
        self.assertEqual(code, 3)
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "OPEN_EXPOSURE")
        self.assertIn("-0.002", receipt["exposure"])

    def test_unreadable_final_position_is_unknown(self):
        spec = happy_spec()
        spec["position"] = ("unclear", {})
        code, _, _ = self.run_once(spec)
        self.assertEqual(code, 4)
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "UNKNOWN")
        self.assertEqual(receipt["final_futures_position"], "UNKNOWN")

    def test_hedge_mode_rows_never_prove_flatness(self):
        spec = happy_spec()
        spec["position"] = [
            {"symbol": SYMBOL, "positionAmt": "0.010", "positionSide": "LONG"},
            {"symbol": SYMBOL, "positionAmt": "-0.010",
             "positionSide": "SHORT"},
        ]
        code, _, _ = self.run_once(spec)
        self.assertEqual(code, 4)
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "UNKNOWN")
        self.assertEqual(receipt["final_futures_position"], "UNKNOWN")


class LockAndGateTests(ExecutorCase):
    def test_standing_lock_blocks_run(self):
        with open(os.path.join(self.dir, "basis-executor.lock"), "w") as f:
            f.write("prior")
        code, transport, output = self.run_once(happy_spec())
        self.assertEqual(code, 3)
        self.assertEqual(transport.posts, [])
        self.assertIn("BLOCKED", output)
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "basis-executor-%s.json" % RID)))

    def test_prior_unresolved_receipt_blocks(self):
        with open(os.path.join(self.dir, "basis-executor-old.json"), "w") as f:
            json.dump({"run_id": "old"}, f)
        code, transport, _ = self.run_once(happy_spec())
        self.assertEqual(code, 3)
        self.assertEqual(transport.posts, [])
        self.assertEqual(self.receipt()["verdict"], "BLOCKED_UNRESOLVED")
        self.assertTrue(self.lock_exists())


class PlanOnlyAndMainTests(ExecutorCase):
    def test_plan_only_makes_no_orders_and_no_receipt(self):
        transport = ScriptedTransport(happy_spec())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = basis_executor.plan_only(self.cfg, transport,
                                            sleep=lambda seconds: None)
        self.assertEqual(code, 0)
        self.assertEqual(transport.posts, [])
        self.assertIn("PLAN |", buffer.getvalue())
        self.assertEqual(os.listdir(self.dir), [])

    def test_plan_only_refusal_prints_refused(self):
        spec = happy_spec()
        spec["usdm_book"] = {"symbol": SYMBOL, "bidPrice": "100.001",
                             "askPrice": "100.05", "time": 1755000000000}
        transport = ScriptedTransport(spec)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = basis_executor.plan_only(self.cfg, transport,
                                            sleep=lambda seconds: None)
        self.assertEqual(code, 2)
        self.assertIn("REFUSED |", buffer.getvalue())
        self.assertEqual(os.listdir(self.dir), [])

    def test_config_from_env_validates(self):
        with self.assertRaises(basis_executor.Blocked):
            basis_executor.config_from_env({"SYMBOL": "bad!", "QUANT_SHA": SHA})
        with self.assertRaises(basis_executor.Blocked):
            basis_executor.config_from_env({"SYMBOL": SYMBOL,
                                            "QUANT_SHA": "short"})
        with self.assertRaises(basis_executor.Blocked):
            basis_executor.config_from_env({
                "SYMBOL": SYMBOL, "QUANT_SHA": SHA, "MIN_ENTRY_BPS": "nope"})
        cfg = basis_executor.config_from_env({
            "SYMBOL": SYMBOL, "QUANT_SHA": SHA, "MIN_ENTRY_BPS": "2.5",
            "RECEIPT_DIR": self.dir})
        self.assertEqual(cfg.min_entry_bps, Decimal("2.5"))
        self.assertEqual(cfg.receipt_dir, self.dir)

    def test_main_blocks_on_missing_env(self):
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with redirect_stdout(buffer):
                code = basis_executor.main([])
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED", buffer.getvalue())

    def test_main_execute_requires_keys(self):
        env = {"SYMBOL": SYMBOL, "QUANT_SHA": SHA, "RECEIPT_DIR": self.dir}
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            with redirect_stdout(buffer):
                code = basis_executor.main(["--execute"])
        self.assertEqual(code, 2)
        self.assertIn("BINANCE_API_KEY", buffer.getvalue())

    def test_main_execute_runs_cycle_through_injected_factory(self):
        transport = ScriptedTransport(happy_spec())
        env = {"SYMBOL": SYMBOL, "QUANT_SHA": SHA, "RECEIPT_DIR": self.dir,
               "BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(basis_executor, "live_request_factory",
                                   return_value=transport):
                with redirect_stdout(io.StringIO()):
                    code = basis_executor.main(["--execute"])
        self.assertEqual(code, 0)
        self.assertEqual(len(transport.posts), 4)
        names = os.listdir(self.dir)
        self.assertTrue(any(name.startswith("basis-executor-")
                            for name in names))
        self.assertIn("events.jsonl", names)


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class LiveTransportOfflineTests(unittest.TestCase):
    """The live plumbing exercised entirely through a patched urlopen."""

    def test_signed_request_signs_and_sends_key_header(self):
        captured = []

        def fake_urlopen(request, timeout=None):
            captured.append(request)
            return FakeHTTPResponse({"serverTime": 1755000000000})

        live = basis_executor.live_request_factory("key-abc", "secret-xyz")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            kind, payload = live("GET", SPOT_URL, "/api/v3/order",
                                 {"symbol": SYMBOL}, True)
        self.assertEqual(kind, "ok")
        self.assertEqual(len(captured), 2)  # lazy time sync, then the call
        self.assertIn("/api/v3/time", captured[0].full_url)
        signed_url = captured[1].full_url
        self.assertIn("timestamp=", signed_url)
        self.assertIn("signature=", signed_url)
        self.assertEqual(captured[1].get_header("X-mbx-apikey"), "key-abc")

    def test_http_5xx_is_unclear(self):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 502, "bad", None,
                                         io.BytesIO(b"gateway"))

        live = basis_executor.live_request_factory("k", "s")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            kind, payload = live("GET", SPOT_URL, "/api/v3/time")
        self.assertEqual(kind, "unclear")
        self.assertEqual(payload, {"http": 502})

    def test_definite_api_error_is_parsed(self):
        body = b'{"code":-1121,"msg":"Invalid symbol."}'

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 400, "bad", None,
                                         io.BytesIO(body))

        live = basis_executor.live_request_factory("k", "s")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            kind, payload = live("GET", SPOT_URL, "/api/v3/exchangeInfo")
        self.assertEqual(kind, "api_error")
        self.assertEqual(payload["code"], -1121)

    def test_network_exception_is_unclear_and_nameless(self):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        live = basis_executor.live_request_factory("k", "s")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            kind, payload = live("GET", SPOT_URL, "/api/v3/time")
        self.assertEqual(kind, "unclear")
        self.assertEqual(payload, {"error": "URLError"})


if __name__ == "__main__":
    unittest.main()
