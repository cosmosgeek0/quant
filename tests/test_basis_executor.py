"""Deterministic offline tests for src/basis_executor.py — injected transport
only; a module-level socket guard makes any real network attempt fail fast."""

import io
import json
import os
import socket
import stat
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
import basis_cycle  # noqa: E402
import events  # noqa: E402
from basis_executor import ExecutorConfig  # noqa: E402

SYMBOL = "BTCUSDT"
SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
RID = "cafe01"
SPOT_URL = basis_executor.SPOT.base_url
USDM_URL = basis_executor.USDM.base_url

MAX_CYCLE_QUOTE = basis_cycle.MAX_CYCLE_QUOTE
SPOT_ASK = Decimal("100.0")
SPOT_BID = Decimal("99.9")
USDM_BID = Decimal("100.2")
SHARED_STEP = Decimal("0.001")
BASE_FEE_RATE = Decimal("0.001")
WIRE_QUANTUM = Decimal("0.00000001")
HAPPY_PLAN_QTY = basis_cycle.quantize_down(
    min(MAX_CYCLE_QUOTE / (Decimal(1) + basis_cycle.FEE_BUFFER) / SPOT_ASK,
        MAX_CYCLE_QUOTE / USDM_BID),
    SHARED_STEP,
)
HAPPY_SPOT_QUOTE_CAP = (
    HAPPY_PLAN_QTY * SPOT_ASK * (Decimal(1) + basis_cycle.FEE_BUFFER)
).quantize(WIRE_QUANTUM)
HAPPY_SHORT_NOTIONAL = (HAPPY_PLAN_QTY * USDM_BID).quantize(WIRE_QUANTUM)
HAPPY_BASE_FEE = HAPPY_PLAN_QTY * BASE_FEE_RATE
HAPPY_NET_BASE = HAPPY_PLAN_QTY - HAPPY_BASE_FEE
HAPPY_HEDGE_QTY = basis_cycle.quantize_down(HAPPY_NET_BASE, SHARED_STEP)
HAPPY_RESIDUAL = HAPPY_NET_BASE - HAPPY_HEDGE_QTY
HAPPY_BUY_QUOTE = HAPPY_PLAN_QTY * SPOT_ASK
HAPPY_SHORT_QUOTE = HAPPY_HEDGE_QTY * USDM_BID
HAPPY_SELL_QUOTE = HAPPY_HEDGE_QTY * SPOT_BID
HAPPY_SELL_FEE = HAPPY_SELL_QUOTE * BASE_FEE_RATE


def wire(value):
    return format(value.quantize(WIRE_QUANTUM), "f")


_real_getaddrinfo = socket.getaddrinfo


def setUpModule():
    def deny(*args, **kwargs):
        raise AssertionError("network access attempted in offline tests")
    socket.getaddrinfo = deny


def tearDownModule():
    socket.getaddrinfo = _real_getaddrinfo


def term_spot(status, executed, quote, order_id):
    def responder(params):
        return ("ok", {"symbol": SYMBOL,
                       "clientOrderId": params["origClientOrderId"],
                       "status": status, "executedQty": executed,
                       "cummulativeQuoteQty": quote, "orderId": order_id})
    return responder


def term_usdm(status, executed, quote, order_id):
    def responder(params):
        return ("ok", {"symbol": SYMBOL,
                       "clientOrderId": params["origClientOrderId"],
                       "status": status, "executedQty": executed,
                       "cumQuote": quote, "orderId": order_id})
    return responder


def new_order(params):
    stage = params["origClientOrderId"].split("-", 2)[2]
    order_id = {
        "enter_spot": 111222,
        "enter_short": 444555,
        "exit_short": 444556,
        "exit_spot": 111223,
        "unwind_spot": 111224,
    }[stage]
    quote_field = "cummulativeQuoteQty" if "spot" in stage else "cumQuote"
    return ("ok", {
        "symbol": SYMBOL,
        "clientOrderId": params["origClientOrderId"],
        "status": "NEW",
        "executedQty": "0",
        quote_field: "0",
        "orderId": order_id,
    })


NEW_ORDER = new_order
MISSING = ("api_error", {"code": -2013, "msg": "Order does not exist."})


def happy_spec():
    """Full-cycle fixture sized from the shared cap, including fee headroom."""
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
                    {"qty": wire(HAPPY_PLAN_QTY),
                     "commission": wire(HAPPY_BASE_FEE),
                     "commissionAsset": "BTC"}]}),
                "query": [term_spot("FILLED", wire(HAPPY_PLAN_QTY),
                                    wire(HAPPY_BUY_QUOTE), 111222)],
            },
            "enter_short": {
                "submit": ("ok", {}),
                "query": [term_usdm("FILLED", format(HAPPY_HEDGE_QTY, "f"),
                                    wire(HAPPY_SHORT_QUOTE), 444555)],
            },
            "exit_short": {
                "submit": ("ok", {}),
                "query": [term_usdm("FILLED", format(HAPPY_HEDGE_QTY, "f"),
                                    wire(HAPPY_SHORT_QUOTE), 444556)],
            },
            "exit_spot": {
                "submit": ("ok", {"fills": [
                    {"qty": wire(HAPPY_HEDGE_QTY),
                     "commission": wire(HAPPY_SELL_FEE),
                     "commissionAsset": "USDT"}]}),
                "query": [term_spot("FILLED", wire(HAPPY_HEDGE_QTY),
                                    wire(HAPPY_SELL_QUOTE), 111223)],
            },
            "unwind_spot": {
                "submit": ("ok", {"fills": [
                    {"qty": wire(HAPPY_HEDGE_QTY),
                     "commission": wire(HAPPY_SELL_FEE),
                     "commissionAsset": "USDT"}]}),
                "query": [term_spot("FILLED", wire(HAPPY_HEDGE_QTY),
                                    wire(HAPPY_SELL_QUOTE), 111224)],
            },
        },
    }


FLAT_POSITION = [{"symbol": SYMBOL, "positionAmt": "0.000"}]


class ScriptedTransport:
    """Injected transport with the (kind, payload) contract of the live one.

    Signed preflight reads are served from optional spec keys with tradable
    defaults: usdm_account_config, spot_account, position_mode,
    symbol_config (actual leverage; default exactly 1x), spot_open_orders,
    usdm_open_orders, usdm_account, preflight_position (the
    FIRST positionRisk read); "position" stays the final flatness read. Any
    of them may be a (kind, payload) tuple to script a non-ok transport kind.
    """

    def __init__(self, spec):
        self.spec = spec
        self.calls = []
        self.posts = []
        self.query_counts = {}
        self.position_reads = 0

    def scripted(self, key, default):
        entry = self.spec.get(key, default)
        return entry if isinstance(entry, tuple) else ("ok", entry)

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
        if path == "/fapi/v1/accountConfig":
            return self.scripted("usdm_account_config", {"canTrade": True})
        if path == "/api/v3/account":
            return self.scripted("spot_account", {"canTrade": True})
        if path == "/fapi/v3/account":
            return self.scripted("usdm_account", {"availableBalance": "1000"})
        if path == "/fapi/v1/positionSide/dual":
            return self.scripted("position_mode", {"dualSidePosition": False})
        if path == "/fapi/v1/symbolConfig":
            return self.scripted("symbol_config",
                                 [{"symbol": SYMBOL, "leverage": "1"}])
        if path.endswith("openOrders"):
            key = "spot_open_orders" if base_url == SPOT_URL \
                else "usdm_open_orders"
            return self.scripted(key, [])
        if path.endswith("positionRisk"):
            self.position_reads += 1
            if self.position_reads == 1:
                return self.scripted("preflight_position", FLAT_POSITION)
            return self.scripted("position", FLAT_POSITION)
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


def cycle_plan():
    return basis_cycle.CyclePlan(
        symbol=SYMBOL, entry_gap_bps=Decimal("20"),
        exit_gap_bps=Decimal("10"), funding_rate=Decimal("0.0001"),
        spot_quantity=HAPPY_PLAN_QTY,
        short_quantity=HAPPY_PLAN_QTY,
        spot_quote_cap=HAPPY_SPOT_QUOTE_CAP,
        short_notional=HAPPY_SHORT_NOTIONAL, leverage=1,
        reason="offline executor interface test")


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


class ReceiptDurabilityTests(ExecutorCase):
    def state(self, receipt=None, request=None):
        state = basis_executor.make_state(
            self.cfg, request or (lambda *_args, **_kwargs: ("ok", {})),
            sleep=lambda _seconds: None)
        state.receipt = receipt or {"legs": []}
        return state

    def write_events(self, path, action):
        events_seen = []
        tmp = path + ".tmp"
        parent_path = os.path.dirname(path) or "."
        parent = os.stat(parent_path)
        parent_identity = (parent.st_dev, parent.st_ino)
        real_replace = os.replace

        def observe(descriptor):
            details = os.fstat(descriptor)
            identity = (details.st_dev, details.st_ino)
            if stat.S_ISREG(details.st_mode):
                temp = os.stat(tmp)
                self.assertEqual(identity, (temp.st_dev, temp.st_ino))
                events_seen.append(("file_fsync", tmp))
            elif stat.S_ISDIR(details.st_mode) \
                    and identity == parent_identity:
                events_seen.append(("parent_fsync", parent_path))
            else:
                self.fail("unexpected receipt fsync descriptor")

        def observe_replace(source, target):
            self.assertEqual((source, target), (tmp, path))
            events_seen.append(("replace", target))
            return real_replace(source, target)

        with mock.patch.object(basis_executor.os, "fsync",
                               side_effect=observe), mock.patch.object(
                                   basis_executor.os, "replace",
                                   side_effect=observe_replace):
            action()
        return events_seen

    def expected_write(self, path):
        return [
            ("file_fsync", path + ".tmp"),
            ("replace", path),
            ("parent_fsync", os.path.dirname(path) or "."),
        ]

    def plan(self):
        return cycle_plan()

    def test_save_orders_file_sync_replace_and_parent_sync(self):
        state = self.state()
        self.assertEqual(
            self.write_events(
                state.receipt_path, lambda: basis_executor.save(state)),
            self.expected_write(state.receipt_path))

    def test_settle_leg_orders_each_receipt_write(self):
        def request(method, _base_url, _path, _params, _signed):
            self.assertEqual(method, "POST")
            return "ok", {"fills": []}

        def query_order(_state, _venue, order_client_id, **_kwargs):
            return "terminal", {
                "symbol": SYMBOL, "clientOrderId": order_client_id,
                "status": "FILLED",
                "executedQty": "0.1", "cummulativeQuoteQty": "10",
                "orderId": 123,
            }

        state = self.state(request=request)
        self.assertEqual(self.write_events(
            state.receipt_path, lambda: basis_executor.settle_leg(
                state, basis_cycle.ENTER_SPOT, self.plan(),
                query_order=query_order)),
            self.expected_write(state.receipt_path) * 2)

    def test_attach_fees_orders_receipt_write(self):
        state = self.state({"legs": [{}]})
        trades = [{"commission": "0.001", "commissionAsset": "BTC"}]
        self.assertEqual(self.write_events(
            state.receipt_path,
            lambda: basis_executor.attach_fees(state, 0, trades)),
            self.expected_write(state.receipt_path))

    def test_mark_source_orders_receipt_write(self):
        path = os.path.join(self.dir, "source.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"run_id": "source"}, handle)
        self.assertEqual(self.write_events(
            path, lambda: basis_executor.mark_source(
                path, {"run_id": "source"}, RID, "sold", "0.1")),
            self.expected_write(path))

    def test_mark_source_replacement_keeps_mode_0600_under_umask_022(self):
        path = os.path.join(self.dir, "source.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"run_id": "source"}, handle)
        os.chmod(path, 0o600)
        prior_umask = os.umask(0o022)
        try:
            basis_executor.mark_source(
                path, {"run_id": "source"}, RID, "sold", "0.1")
        finally:
            os.umask(prior_umask)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_file_fsync_failure_removes_temp_and_preserves_target(self):
        state = self.state({"legs": [{"stage": "new"}]})
        original = b'{"legs": [{"stage": "prior"}]}\n'
        with open(state.receipt_path, "wb") as handle:
            handle.write(original)

        def refuse_file_sync(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("simulated receipt file sync refusal")

        with mock.patch.object(basis_executor.os, "fsync",
                               side_effect=refuse_file_sync):
            with self.assertRaises(
                    basis_executor.ReceiptWriteError) as raised:
                basis_executor.save(state)
        self.assertEqual(raised.exception.phase, "file_fsync")
        self.assertFalse(raised.exception.rename_landed)
        self.assertIn("target untouched", raised.exception.row)
        with open(state.receipt_path, "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertFalse(os.path.exists(state.receipt_path + ".tmp"))

    def test_parent_fsync_failure_reports_landed_rename(self):
        state = self.state({"legs": [{"stage": "new"}]})
        with open(state.receipt_path, "w", encoding="utf-8") as handle:
            json.dump({"legs": [{"stage": "prior"}]}, handle)
        parent_descriptors = []

        def refuse_parent_sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                parent_descriptors.append(descriptor)
                raise OSError("simulated receipt parent sync refusal")

        with mock.patch.object(basis_executor.os, "fsync",
                               side_effect=refuse_parent_sync):
            with self.assertRaises(
                    basis_executor.ReceiptWriteError) as raised:
                basis_executor.save(state)
        self.assertEqual(raised.exception.phase, "parent_fsync")
        self.assertTrue(raised.exception.rename_landed)
        self.assertIn(
            "rename landed but its directory durability is unproven",
            raised.exception.row)
        with open(state.receipt_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), state.receipt)
        self.assertFalse(os.path.exists(state.receipt_path + ".tmp"))
        self.assertEqual(len(parent_descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(parent_descriptors[0])


class EntryPollingInterfaceTests(ExecutorCase):
    def state(self, request):
        state = basis_executor.make_state(
            self.cfg, request, sleep=lambda _seconds: None)
        state.receipt = {"legs": []}
        return state

    def order(self, client, order_id, status="NEW", executed="0",
              quote="0", venue=basis_executor.SPOT):
        return {
            "symbol": SYMBOL,
            "clientOrderId": client,
            "orderId": order_id,
            "status": status,
            "executedQty": executed,
            venue.quote_field: quote,
        }

    def test_entry_poll_keeps_seen_order_when_later_reads_are_missing(self):
        client = "bc-%s-enter_spot" % RID
        replies = [
            ("ok", self.order(client, 710, executed="0.01", quote="1")),
            MISSING,
            MISSING,
        ]
        observations = []

        def request(*_args, **_kwargs):
            return replies.pop(0)

        state = self.state(request)
        with mock.patch.object(basis_executor, "QUERY_TRIES", 3):
            query_state, order = basis_executor.query_client_order_strongest(
                state, basis_executor.SPOT, client,
                observation_sink=lambda role, row: observations.append(
                    (role, row)))
        self.assertEqual(query_state, "pending")
        self.assertEqual(order["orderId"], 710)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0][0], "spot")

    def test_entry_poll_observation_is_durable_before_next_read(self):
        reads = 0
        state_holder = {}
        external = []

        def request(method, _base_url, _path, params, _signed):
            nonlocal reads
            if method == "POST":
                return "ok", {}
            reads += 1
            client = params["origClientOrderId"]
            if reads == 1:
                return "ok", self.order(
                    client, 720, status="PARTIALLY_FILLED",
                    executed="0.01", quote="1", venue=basis_executor.USDM)
            with open(state_holder["state"].receipt_path,
                      encoding="utf-8") as handle:
                durable = json.load(handle)
            saved = durable["legs"][-1]["strongest_order_observation"]
            self.assertEqual(saved["orderId"], 720)
            self.assertEqual(saved["status"], "PARTIALLY_FILLED")
            return MISSING

        state = self.state(request)
        state_holder["state"] = state
        with mock.patch.object(basis_executor, "QUERY_TRIES", 2):
            result, order, _fills = basis_executor.settle_leg(
                state, basis_cycle.ENTER_SHORT, cycle_plan(),
                Decimal("0.02"),
                observation_sink=lambda role, row: external.append(
                    (role, row["orderId"])))
        self.assertEqual(result, "unknown")
        self.assertEqual(order["orderId"], 720)
        self.assertEqual(external, [("short", 720)])

    def test_entry_poll_sink_only_persists_stronger_truth(self):
        client = "bc-%s-enter_spot" % RID
        replies = [
            ("ok", self.order(client, 725)),
            ("ok", self.order(client, 725)),
            ("ok", self.order(
                client, 725, status="PARTIALLY_FILLED",
                executed="0.01", quote="1")),
            MISSING,
        ]
        saved = []
        state = self.state(lambda *_args, **_kwargs: replies.pop(0))
        with mock.patch.object(basis_executor, "QUERY_TRIES", 4):
            query_state, order = basis_executor.query_client_order_strongest(
                state, basis_executor.SPOT, client,
                observation_sink=lambda _role, row: saved.append(row))
        self.assertEqual(query_state, "pending")
        self.assertEqual(order["status"], "PARTIALLY_FILLED")
        self.assertEqual([row["status"] for row in saved],
                         ["NEW", "PARTIALLY_FILLED"])

    def test_entry_poll_requires_submit_and_query_order_id_agreement(self):
        state_holder = {}

        def request(method, _base_url, _path, params, _signed):
            client = params.get("newClientOrderId") \
                or params["origClientOrderId"]
            if method == "POST":
                return "ok", {
                    "symbol": SYMBOL, "clientOrderId": client,
                    "orderId": 730,
                }
            with open(state_holder["state"].receipt_path,
                      encoding="utf-8") as handle:
                durable = json.load(handle)
            self.assertEqual(durable["legs"][-1]["submit_order_id"], "730")
            return "ok", self.order(
                client, 731, status="FILLED", executed="0.01", quote="1")

        state = self.state(request)
        state_holder["state"] = state
        result, order, _fills = basis_executor.settle_leg(
            state, basis_cycle.ENTER_SPOT, cycle_plan())
        self.assertEqual(result, "unknown")
        self.assertEqual(order["orderId"], 731)
        self.assertEqual(state.receipt["first_failure"]["where"],
                         "query_order")
        self.assertIn("order id", state.receipt["first_failure"]["detail"])

        client = "bc-%s-enter_spot" % RID
        replies = [
            ("ok", self.order(client, 732)),
            ("ok", self.order(
                client, 733, status="FILLED", executed="0.01", quote="1")),
        ]
        state = self.state(lambda *_args, **_kwargs: replies.pop(0))
        with mock.patch.object(basis_executor, "QUERY_TRIES", 2):
            query_state, order = basis_executor.query_client_order_strongest(
                state, basis_executor.SPOT, client)
        self.assertEqual(query_state, "identity_mismatch")
        self.assertEqual(order["orderId"], 733)

    def test_entry_each_leg_binds_symbol_client_and_order_id(self):
        client = "bc-%s-enter_spot" % RID
        rows = [
            {**self.order(client, 740), "symbol": "ETHUSDT"},
            {**self.order(client, 740), "clientOrderId": "bc-other"},
            self.order(client, 741),
        ]
        for row in rows:
            with self.subTest(row=row):
                calls = []

                def request(*_args, **_kwargs):
                    calls.append(1)
                    return "ok", row

                state = self.state(request)
                with mock.patch.object(basis_executor, "QUERY_TRIES", 2):
                    query_state, _order = \
                        basis_executor.query_client_order_strongest(
                            state, basis_executor.SPOT, client,
                            expected_order_id="740")
                self.assertEqual(query_state, "identity_mismatch")
                self.assertEqual(len(calls), 1)

    def test_entry_live_or_foreign_observation_cannot_become_absent(self):
        client = "bc-%s-enter_spot" % RID
        replies = [
            ("ok", {**self.order(client, 750),
                    "clientOrderId": "bc-other"}),
            MISSING,
        ]
        calls = []

        def request(*_args, **_kwargs):
            calls.append(1)
            return replies.pop(0)

        state = self.state(request)
        with mock.patch.object(basis_executor, "QUERY_TRIES", 2):
            query_state, order = basis_executor.query_client_order_strongest(
                state, basis_executor.SPOT, client)
        self.assertEqual(query_state, "identity_mismatch")
        self.assertEqual(order["orderId"], 750)
        self.assertEqual(len(calls), 1)

    def test_entry_partial_unwind_then_missing_never_resends(self):
        posts = 0
        reads = 0

        def request(method, _base_url, _path, params, _signed):
            nonlocal posts, reads
            if method == "POST":
                posts += 1
                return "ok", {}
            reads += 1
            if reads == 1:
                return "ok", self.order(
                    params["origClientOrderId"], 760,
                    status="PARTIALLY_FILLED", executed="0.01",
                    quote="1")
            return MISSING

        state = self.state(request)
        with mock.patch.object(basis_executor, "QUERY_TRIES", 2):
            result, order, _fills = basis_executor.settle_leg(
                state, basis_cycle.UNWIND_SPOT, cycle_plan(), Decimal("0.02"))
        self.assertEqual(result, "unknown")
        self.assertEqual(order["status"], "PARTIALLY_FILLED")
        self.assertEqual(posts, 1)

    def test_entry_spot_fill_above_persisted_intent_refuses_commit(self):
        def request(method, _base_url, _path, params, _signed):
            if method == "POST":
                return "ok", {}
            return "ok", self.order(
                params["origClientOrderId"], 770, status="FILLED",
                executed="0.01",
                quote=str(cycle_plan().spot_quote_cap + Decimal("0.01")))

        state = self.state(request)
        result, _order, _fills = basis_executor.settle_leg(
            state, basis_cycle.ENTER_SPOT, cycle_plan())
        self.assertEqual(result, "terminal")
        actual_quote = Decimal(state.receipt["legs"][-1]["quote"])
        self.assertGreater(actual_quote, cycle_plan().spot_quote_cap)
        self.assertNotIn("first_failure", state.receipt)

    def test_entry_actual_fills_use_unrounded_venue_values(self):
        def request(method, _base_url, _path, params, _signed):
            if method == "POST":
                return "ok", {}
            return "ok", self.order(
                params["origClientOrderId"], 780, status="EXPIRED",
                executed="0.0011", quote="0.11011",
                venue=basis_executor.USDM)

        state = self.state(request)
        result, _order, _fills = basis_executor.settle_leg(
            state, basis_cycle.ENTER_SHORT, cycle_plan(), Decimal("0.002"))
        self.assertEqual(result, "terminal")
        self.assertEqual(state.receipt["legs"][-1]["executed_qty"], "0.0011")
        self.assertEqual(state.receipt["legs"][-1]["quote"], "0.11011")


class EntryMarketContextTests(ExecutorCase):
    def context(self, spec):
        transport = ScriptedTransport(spec)
        state = basis_executor.make_state(
            self.cfg, transport, sleep=lambda _seconds: None,
            receipted=False)
        return basis_executor.market_context(state)

    def test_entry_actual_leg_minima_block_rung_commit(self):
        spec = happy_spec()
        spot_filters = spec["spot_info"]["symbols"][0]["filters"]
        futures_filters = spec["usdm_info"]["symbols"][0]["filters"]
        spot_filters[0].update(stepSize="0.0002", minQty="0.0003")
        spot_filters[1].update(stepSize="0.0004", minQty="0.0005")
        spot_filters[2]["minNotional"] = "3"
        futures_filters[0].update(stepSize="0.002", minQty="0.003")
        futures_filters[1].update(stepSize="0.004", minQty="0.005")
        futures_filters[2]["notional"] = "7"
        market = self.context(spec)
        self.assertEqual(market["spot_step"], Decimal("0.0004"))
        self.assertEqual(market["spot_min_qty"], Decimal("0.0005"))
        self.assertEqual(market["spot_min_notional"], Decimal("3"))
        self.assertEqual(market["futures_step"], Decimal("0.004"))
        self.assertEqual(market["futures_min_qty"], Decimal("0.005"))
        self.assertEqual(market["futures_min_notional"], Decimal("7"))

    def test_entry_hedge_quantity_obeys_effective_cap_before_post(self):
        spec = happy_spec()
        spec["usdm_info"]["symbols"][0]["filters"].append({
            "filterType": "PRICE_FILTER", "maxPrice": "101.25",
        })
        market = self.context(spec)
        self.assertEqual(market["futures_execution_price_ceiling"],
                         Decimal("101.25"))
        cap_quantity = basis_cycle.quantize_down(
            Decimal("7") / market["futures_execution_price_ceiling"],
            market["futures_step"])
        self.assertEqual(cap_quantity, Decimal("0.069"))

    def test_entry_cap_to_base_floor_precedes_short_post(self):
        spec = happy_spec()
        market = self.context(spec)
        self.assertIsNone(market["futures_execution_price_ceiling"])
        spec["usdm_info"]["symbols"][0]["filters"].append({
            "filterType": "PRICE_FILTER", "maxPrice": "100.3",
        })
        market = self.context(spec)
        self.assertEqual(market["futures_execution_price_ceiling"],
                         Decimal("100.3"))

    def test_entry_spot_sellability_uses_only_spot_filters(self):
        spec = happy_spec()
        futures_filters = spec["usdm_info"]["symbols"][0]["filters"]
        futures_filters[0].update(stepSize="1", minQty="2")
        futures_filters[1].update(stepSize="1", minQty="3")
        futures_filters[2]["notional"] = "50"
        market = self.context(spec)
        self.assertEqual(market["spot_step"], Decimal("0.0001"))
        self.assertEqual(market["spot_min_qty"], Decimal("0.0001"))
        self.assertEqual(market["spot_min_notional"], Decimal("5.00000000"))
        self.assertEqual(market["futures_step"], Decimal("1"))
        self.assertEqual(market["futures_min_qty"], Decimal("3"))


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
        self.assertEqual(Decimal(acquired["executed_qty"]), HAPPY_PLAN_QTY)
        self.assertEqual(Decimal(acquired["base_fee"]), HAPPY_BASE_FEE)
        self.assertEqual(Decimal(acquired["net_base"]), HAPPY_NET_BASE)
        self.assertEqual(Decimal(acquired["hedge_quantity"]),
                         HAPPY_HEDGE_QTY)
        for stage in ("enter_short", "exit_short", "exit_spot"):
            index = post_stages(transport).index(stage)
            self.assertEqual(
                Decimal(transport.posts[index][1]["quantity"]),
                HAPPY_HEDGE_QTY)
        # The plan estimate is never a dependent-leg order size.
        estimate = self.receipt()["plan"]["estimated_quantity"]
        self.assertEqual(Decimal(estimate), HAPPY_PLAN_QTY)

    def test_wire_shapes_per_leg(self):
        _, transport, _ = self.run_once(happy_spec())
        by_stage = {params["newClientOrderId"].split("-", 2)[2]: params
                    for _, params in transport.posts}
        buy = by_stage["enter_spot"]
        self.assertEqual(buy["side"], "BUY")
        # fee-aware: the cap now carries the 0.2% base-fee headroom
        self.assertEqual(Decimal(buy["quoteOrderQty"]),
                         HAPPY_SPOT_QUOTE_CAP)
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
        self.assertEqual(Decimal(verdict["residual"]), HAPPY_RESIDUAL)
        buy_terminal = listed[3]
        self.assertEqual(buy_terminal["side"], "BUY")
        self.assertEqual(buy_terminal["client_id_last4"], "spot")
        self.assertEqual(Decimal(buy_terminal["executed"]), HAPPY_PLAN_QTY)
        self.assertEqual(buy_terminal["fees"],
                         {"BTC": wire(HAPPY_BASE_FEE)})

    def test_receipt_records_result_and_final_position(self):
        self.run_once(happy_spec())
        receipt = self.receipt()
        self.assertEqual(
            Decimal(receipt["result"]["residual_base_from_this_run"]),
            HAPPY_RESIDUAL)
        self.assertEqual(Decimal(receipt["hard_ceiling_usdt"]),
                         MAX_CYCLE_QUOTE)
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
                         HAPPY_HEDGE_QTY)
        self.assertEqual(Decimal(by_stage["exit_short"]["quantity"]),
                         Decimal("0.060"))
        self.assertEqual(Decimal(by_stage["exit_spot"]["quantity"]),
                         HAPPY_HEDGE_QTY)
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
        self.assertIn(format(HAPPY_HEDGE_QTY, "f"), receipt["exposure"])
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
            HAPPY_NET_BASE - Decimal("0.05"))
        verdict = self.events_list()[-1]
        self.assertEqual(Decimal(verdict["residual"]),
                         HAPPY_NET_BASE - Decimal("0.05"))


def coarse_step_spec():
    """Spot step 0.01 vs futures step 1, so the shared (LCM) step is 1.
    Buy 1.10 base for ~5 USDT, hedge 1; a spot SELL of 1 that fills only
    0.20 strands residual 0.90 — sellable at the spot step 0.01 although
    far below the shared step."""
    spec = happy_spec()
    spec["spot_info"]["symbols"][0]["filters"] = [
        {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01",
         "minQty": "0.01"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
    ]
    spec["usdm_info"]["symbols"][0]["filters"] = [
        {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "1", "minQty": "1"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
    ]
    spec["spot_book"] = {"symbol": SYMBOL, "bidPrice": "4.99",
                         "askPrice": "5.00"}
    spec["usdm_book"] = {"symbol": SYMBOL, "bidPrice": "5.01",
                         "askPrice": "5.02", "time": 1755000000000}
    spec["legs"] = {
        "enter_spot": {
            "submit": ("ok", {"fills": [
                {"qty": "1.10", "commission": "0.0055",
                 "commissionAsset": "USDT"}]}),
            "query": [term_spot("FILLED", "1.10", "5.00", 211222)],
        },
        "enter_short": {
            "submit": ("ok", {}),
            "query": [term_usdm("FILLED", "1", "5.01", 544555)],
        },
        "exit_short": {
            "submit": ("ok", {}),
            "query": [term_usdm("FILLED", "1", "5.02", 544556)],
        },
        "exit_spot": {
            "submit": ("ok", {"fills": [
                {"qty": "0.20", "commission": "0.001",
                 "commissionAsset": "USDT"}]}),
            "query": [term_spot("EXPIRED", "0.20", "1.00", 211223)],
        },
        "unwind_spot": {
            "submit": ("ok", {"fills": [
                {"qty": "0.20", "commission": "0.001",
                 "commissionAsset": "USDT"}]}),
            "query": [term_spot("EXPIRED", "0.20", "1.00", 211224)],
        },
    }
    return spec


class SpotStepResidualTests(ExecutorCase):
    """A spot-sell shortfall is judged at the SPOT step, never the shared
    (LCM) step: residual 0.90 under a shared step of 1 is sellable, not
    dust. Panel repro: sell of 1.00 fills 0.20, residual 0.90 stranded."""

    def test_exit_spot_shortfall_at_spot_step_is_open_exposure(self):
        code, transport, _ = self.run_once(coarse_step_spec())
        self.assertEqual(code, 3)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short", "exit_short",
                          "exit_spot"])
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "OPEN_EXPOSURE")
        self.assertEqual(
            Decimal(receipt["result"]["residual_base_from_this_run"]),
            Decimal("0.90"))
        self.assertIn("0.20", receipt["exposure"])
        self.assertIn("0.90", receipt["exposure"])
        self.assertIn("spot step", receipt["exposure"])
        self.assertTrue(self.lock_exists())
        verdict = self.events_list()[-1]
        self.assertEqual(verdict["verdict"], "OPEN_EXPOSURE")
        self.assertEqual(Decimal(verdict["residual"]), Decimal("0.90"))

    def test_unwind_spot_shortfall_at_spot_step_is_open_exposure(self):
        spec = coarse_step_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("api_error", {"code": -2019,
                                     "msg": "Margin is insufficient."}),
            "query": [MISSING],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 3)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short", "unwind_spot"])
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "OPEN_EXPOSURE")
        self.assertEqual(
            Decimal(receipt["result"]["residual_base_from_this_run"]),
            Decimal("0.90"))
        self.assertIn("0.20", receipt["exposure"])
        self.assertIn("0.90", receipt["exposure"])
        self.assertTrue(self.lock_exists())

    def test_full_fill_residual_below_spot_step_is_recorded_dust(self):
        spec = coarse_step_spec()
        spec["legs"]["enter_spot"] = {
            "submit": ("ok", {"fills": [
                {"qty": "1.005", "commission": "0.005",
                 "commissionAsset": "USDT"}]}),
            "query": [term_spot("FILLED", "1.005", "5.00", 211222)],
        }
        spec["legs"]["exit_spot"] = {
            "submit": ("ok", {"fills": [
                {"qty": "1.00", "commission": "0.005",
                 "commissionAsset": "USDT"}]}),
            "query": [term_spot("FILLED", "1.00", "5.00", 211223)],
        }
        code, _, _ = self.run_once(spec)
        self.assertEqual(code, 0)
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "CLOSED")
        self.assertEqual(
            Decimal(receipt["result"]["residual_base_from_this_run"]),
            Decimal("0.005"))
        self.assertFalse(self.lock_exists())

    def test_entry_dust_uses_spot_step(self):
        spec = coarse_step_spec()
        spec["legs"]["enter_spot"] = {
            "submit": ("ok", {"fills": [
                {"qty": "1.005", "commission": "0.005",
                 "commissionAsset": "USDT"}]}),
            "query": [term_spot("FILLED", "1.005", "5.00", 211222)],
        }
        spec["legs"]["exit_spot"] = {
            "submit": ("ok", {"fills": [
                {"qty": "1.00", "commission": "0.005",
                 "commissionAsset": "USDT"}]}),
            "query": [term_spot("FILLED", "1.00", "5.00", 211223)],
        }
        code, _transport, _ = self.run_once(spec)
        self.assertEqual(code, 0)
        residual = Decimal(
            self.receipt()["result"]["residual_base_from_this_run"])
        self.assertEqual(residual, Decimal("0.005"))
        self.assertLess(residual, Decimal("0.01"))


class SharedSoldLegTests(ExecutorCase):
    def rejected_short_spec(self):
        spec = happy_spec()
        spec["legs"]["enter_short"] = {
            "submit": ("api_error", {"code": -2019,
                                     "msg": "Margin is insufficient."}),
            "query": [MISSING],
        }
        return spec

    def test_entry_unwind_fee_cannot_draw_from_older_rung(self):
        spec = self.rejected_short_spec()
        spec["legs"]["unwind_spot"] = {
            "submit": ("ok", {"fills": [{
                "qty": wire(HAPPY_HEDGE_QTY),
                "commission": "0.00100000",
                "commissionAsset": "BTC",
            }]}),
            "query": [term_spot(
                "FILLED", wire(HAPPY_HEDGE_QTY),
                wire(HAPPY_SELL_QUOTE), 111224)],
        }
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 3)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short", "unwind_spot"])
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "OPEN_EXPOSURE")
        self.assertLess(
            Decimal(receipt["result"]["residual_base_from_this_run"]), 0)
        self.assertIn("pre-existing balance", receipt["exposure"])

    def test_entry_rejected_hedge_uses_measured_spot_unwind(self):
        spec = self.rejected_short_spec()
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 2)
        unwind = transport.posts[-1][1]
        self.assertEqual(unwind["newClientOrderId"],
                         "bc-%s-unwind_spot" % RID)
        self.assertEqual(Decimal(unwind["quantity"]), HAPPY_HEDGE_QTY)
        self.assertNotEqual(Decimal(unwind["quantity"]), HAPPY_PLAN_QTY)

    def test_entry_fee_assets_preserve_base_quote_and_third_asset_units(self):
        spec = self.rejected_short_spec()
        spec["legs"]["unwind_spot"] = {
            "submit": ("ok", {"fills": [
                {"qty": wire(HAPPY_HEDGE_QTY), "commission": "0.01",
                 "commissionAsset": "USDT"},
                {"qty": wire(HAPPY_HEDGE_QTY), "commission": "0.0002",
                 "commissionAsset": "BNB"},
            ]}),
            "query": [term_spot(
                "FILLED", wire(HAPPY_HEDGE_QTY),
                wire(HAPPY_SELL_QUOTE), 111224)],
        }
        code, _transport, _ = self.run_once(spec)
        self.assertEqual(code, 2)
        leg = self.receipt()["legs"][-1]
        self.assertEqual(Decimal(leg["fee_by_asset"]["USDT"]),
                         Decimal("0.01"))
        self.assertEqual(Decimal(leg["fee_by_asset"]["BNB"]),
                         Decimal("0.0002"))
        self.assertEqual(Decimal(
            self.receipt()["result"]["sell_base_fee"]), Decimal("0"))


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
                         HAPPY_HEDGE_QTY)
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
            HAPPY_NET_BASE - Decimal("0.05"))


class UnknownLegTests(ExecutorCase):
    """For every leg: an unclear submission that the client-id query resolves
    continues the cycle; one that stays unresolved freezes as UNKNOWN."""

    def test_spot_buy_unclear_submission_query_resolves(self):
        spec = happy_spec()
        spec["legs"]["enter_spot"] = {
            "submit": ("unclear", {}),
            "query": [NEW_ORDER,
                      term_spot("FILLED", wire(HAPPY_PLAN_QTY),
                                wire(HAPPY_BUY_QUOTE), 111222)],
        }
        spec["my_trades"]["111222"] = [
            {"qty": wire(HAPPY_PLAN_QTY),
             "commission": wire(HAPPY_BASE_FEE),
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
                      term_usdm("FILLED", format(HAPPY_HEDGE_QTY, "f"),
                                wire(HAPPY_SHORT_QUOTE), 444555)],
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
                      term_usdm("FILLED", format(HAPPY_HEDGE_QTY, "f"),
                                wire(HAPPY_SHORT_QUOTE), 444556)],
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
                      term_spot("FILLED", wire(HAPPY_HEDGE_QTY),
                                wire(HAPPY_SELL_QUOTE), 111223)],
        }
        spec["my_trades"]["111223"] = [
            {"qty": wire(HAPPY_HEDGE_QTY),
             "commission": wire(HAPPY_SELL_FEE),
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
            "query": [term_spot("FILLED", wire(HAPPY_PLAN_QTY),
                                wire(HAPPY_BUY_QUOTE), 111222)],
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


PREFLIGHT_PATHS = (
    "/fapi/v1/accountConfig",
    "/api/v3/account",
    "/fapi/v1/positionSide/dual",
    "/fapi/v1/symbolConfig",
    "/api/v3/openOrders",
    "/fapi/v1/openOrders",
    "/fapi/v3/positionRisk",
    "/fapi/v3/account",
)


class SignedPreflightTests(ExecutorCase):
    """run_once must prove account facts with signed read-only GETs BEFORE
    any order; every unproven fact refuses with the distinct
    PREFLIGHT_REFUSED verdict and exit 5, no order sent, lock released."""

    def assert_preflight_refused(self, spec, note_fragment):
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 5)
        self.assertEqual(transport.posts, [])
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "PREFLIGHT_REFUSED")
        self.assertIn(note_fragment, receipt["verdict_note"])
        self.assertIn("plan", receipt)  # refused after planning, before orders
        self.assertFalse(self.lock_exists())
        return receipt

    def test_signed_preflight_reads_precede_the_first_order(self):
        code, transport, _ = self.run_once(happy_spec())
        self.assertEqual(code, 0)
        first_post = next(index for index, call in enumerate(transport.calls)
                          if call[0] == "POST")
        seen = {}
        for method, _base, path, _params, signed in transport.calls[:first_post]:
            if method == "GET" and path in PREFLIGHT_PATHS:
                seen.setdefault(path, signed)
        self.assertEqual(set(seen), set(PREFLIGHT_PATHS))
        self.assertTrue(all(seen.values()), "preflight reads must be signed")
        preflight = self.receipt()["preflight"]
        self.assertEqual(preflight["position_mode"], "ONE_WAY")
        self.assertTrue(preflight["futures_can_trade"])
        self.assertTrue(preflight["spot_can_trade"])

    def test_hedge_mode_refuses_before_any_order(self):
        spec = happy_spec()
        spec["position_mode"] = {"dualSidePosition": True}
        self.assert_preflight_refused(spec, "ONE_WAY")

    def test_unproven_position_mode_refuses(self):
        spec = happy_spec()
        spec["position_mode"] = {}
        self.assert_preflight_refused(spec, "ONE_WAY")

    def test_futures_can_trade_false_refuses(self):
        spec = happy_spec()
        spec["usdm_account_config"] = {"canTrade": False}
        receipt = self.assert_preflight_refused(spec, "canTrade")
        kinds = [event["kind"] for event in self.events_list()]
        self.assertEqual(kinds, ["run_start", "gap_read", "run_verdict"])
        self.assertEqual(self.events_list()[-1]["verdict"],
                         "PREFLIGHT_REFUSED")
        self.assertEqual(receipt["legs"], [])

    def test_spot_can_trade_false_refuses(self):
        spec = happy_spec()
        spec["spot_account"] = {"canTrade": False}
        self.assert_preflight_refused(spec, "Spot account canTrade")

    def test_margin_short_refuses(self):
        spec = happy_spec()
        spec["usdm_account"] = {"availableBalance": "5"}
        receipt = self.assert_preflight_refused(spec, "short margin")
        self.assertIn(wire(HAPPY_SHORT_NOTIONAL), receipt["verdict_note"])

    def test_existing_position_refuses(self):
        spec = happy_spec()
        spec["preflight_position"] = [
            {"symbol": SYMBOL, "positionAmt": "0.010"}]
        self.assert_preflight_refused(spec, "existing Futures position 0.010")

    def test_hedge_side_position_rows_refuse(self):
        spec = happy_spec()
        spec["preflight_position"] = [
            {"symbol": SYMBOL, "positionAmt": "0.010",
             "positionSide": "LONG"},
            {"symbol": SYMBOL, "positionAmt": "-0.010",
             "positionSide": "SHORT"},
        ]
        self.assert_preflight_refused(spec, "cannot prove flatness")

    def test_open_futures_order_refuses(self):
        spec = happy_spec()
        spec["usdm_open_orders"] = [{"orderId": 9, "symbol": SYMBOL}]
        self.assert_preflight_refused(spec, "open order")

    def test_open_spot_order_refuses(self):
        spec = happy_spec()
        spec["spot_open_orders"] = [{"orderId": 8, "symbol": SYMBOL}]
        self.assert_preflight_refused(spec, "open order")

    def test_unreadable_capability_read_refuses(self):
        spec = happy_spec()
        spec["usdm_account_config"] = ("unclear", {})
        self.assert_preflight_refused(spec, "unavailable")


class ShortCeilingTests(ExecutorCase):
    """The short entry's exchange-reported cumQuote is measured against
    MAX_CYCLE_QUOTE post-fill; a breach is recorded in the receipt and the
    first-failure mechanism while the ordered reduce-only close contains it."""

    def test_cum_quote_breach_is_recorded_and_contained(self):
        spec = happy_spec()
        breach_quote = MAX_CYCLE_QUOTE + Decimal("0.5")
        spec["legs"]["enter_short"]["query"] = [
            term_usdm("FILLED", format(HAPPY_HEDGE_QTY, "f"),
                      wire(breach_quote), 444555)]
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 0)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short", "exit_short",
                          "exit_spot"])
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "CLOSED")
        short_leg = [leg for leg in receipt["legs"]
                     if leg["stage"] == "ENTER_SHORT"][0]
        self.assertEqual(Decimal(short_leg["ceiling_breach"]["cum_quote"]),
                         breach_quote)
        self.assertEqual(
            Decimal(short_leg["ceiling_breach"]["hard_ceiling_usdt"]),
            MAX_CYCLE_QUOTE)
        self.assertEqual(receipt["first_failure"]["where"],
                         "enter_short_ceiling")
        self.assertIn(wire(breach_quote),
                      receipt["first_failure"]["detail"])

    def test_no_breach_records_nothing(self):
        code, _, _ = self.run_once(happy_spec())
        self.assertEqual(code, 0)
        receipt = self.receipt()
        short_leg = [leg for leg in receipt["legs"]
                     if leg["stage"] == "ENTER_SHORT"][0]
        self.assertNotIn("ceiling_breach", short_leg)
        self.assertNotIn("first_failure", receipt)


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

    def test_cross_lane_verdictless_receipt_blocks(self):
        # A spot_roundtrip-lane receipt with no verdict must block this lane.
        with open(os.path.join(self.dir, "roundtrip-old.json"), "w") as f:
            json.dump({"run_id": "old", "buy_client_id": "fmloldb"}, f)
        code, transport, _ = self.run_once(happy_spec())
        self.assertEqual(code, 3)
        self.assertEqual(transport.posts, [])
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "BLOCKED_UNRESOLVED")
        self.assertIn("any lane", receipt["exposure"])
        self.assertTrue(self.lock_exists())

    def test_cross_lane_receipt_with_verdict_does_not_block(self):
        with open(os.path.join(self.dir, "futures-hand-x.json"), "w") as f:
            json.dump({"run_id": "x", "verdict": "CLOSED"}, f)
        code, transport, _ = self.run_once(happy_spec())
        self.assertEqual(code, 0)
        self.assertEqual(self.receipt()["verdict"], "CLOSED")
        self.assertEqual(len(transport.posts), 4)

    def test_unreadable_cross_lane_json_blocks(self):
        with open(os.path.join(self.dir, "mystery.json"), "w") as f:
            f.write("{not json")
        code, transport, _ = self.run_once(happy_spec())
        self.assertEqual(code, 3)
        self.assertEqual(transport.posts, [])
        self.assertEqual(self.receipt()["verdict"], "BLOCKED_UNRESOLVED")
        self.assertEqual(self.receipt()["first_failure"]["where"],
                         "prior_receipt")

    def test_non_object_json_blocks_instead_of_crashing(self):
        with open(os.path.join(self.dir, "scalar.json"), "w") as f:
            f.write("5")
        code, transport, _ = self.run_once(happy_spec())
        self.assertEqual(code, 3)
        self.assertEqual(transport.posts, [])
        self.assertEqual(self.receipt()["verdict"], "BLOCKED_UNRESOLVED")

    def test_non_json_names_do_not_block(self):
        # .jsonl event streams and lock files are not receipts.
        with open(os.path.join(self.dir, "events.jsonl"), "w") as f:
            f.write("")
        code, _, _ = self.run_once(happy_spec())
        self.assertEqual(code, 0)


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


class MarginBandTests(ExecutorCase):
    """The margin proof must cover notional x the futures price band —
    the exchange reserves worst-case market-order margin."""

    def with_band(self, spec, multiplier_up, available):
        spec["usdm_info"]["symbols"][0]["filters"].append(
            {"filterType": "PERCENT_PRICE", "multiplierUp": multiplier_up,
             "multiplierDown": "0.85", "multiplierDecimal": "4"})
        spec["usdm_account"] = {"availableBalance": available}
        return spec

    def test_banded_margin_refuses_at_the_unbanded_cap(self):
        spec = self.with_band(happy_spec(), "1.15",
                              format(MAX_CYCLE_QUOTE, "f"))
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 5)
        self.assertEqual(transport.posts, [])
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "PREFLIGHT_REFUSED")
        self.assertIn("banded short margin", receipt["verdict_note"])
        expected = (HAPPY_SHORT_NOTIONAL * Decimal("1.15")).quantize(
            WIRE_QUANTUM)
        self.assertIn(wire(expected), receipt["verdict_note"])

    def test_banded_margin_passes_with_headroom(self):
        required = (HAPPY_SHORT_NOTIONAL * Decimal("1.15")).quantize(
            WIRE_QUANTUM)
        spec = self.with_band(happy_spec(), "1.15",
                              format(required + Decimal("0.01"), "f"))
        code, _, _ = self.run_once(spec)
        self.assertEqual(code, 0)
        preflight = self.receipt()["preflight"]
        self.assertEqual(preflight["margin_band_up"], "1.15")
        self.assertEqual(Decimal(preflight["required_short_margin_1x"]),
                         required)

    def test_missing_band_uses_conservative_default(self):
        spec = happy_spec()
        spec["usdm_account"] = {
            "availableBalance": format(MAX_CYCLE_QUOTE, "f")}
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 5)
        self.assertEqual(transport.posts, [])
        self.assertIn("1.5", self.receipt()["verdict_note"])


class LeverageGateTests(ExecutorCase):
    """The plan symbol's ACTUAL leverage is measured and must be exactly 1x."""

    def refuse(self, spec, fragment):
        code, transport, _ = self.run_once(spec)
        self.assertEqual(code, 5)
        self.assertEqual(transport.posts, [])
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "PREFLIGHT_REFUSED")
        self.assertIn(fragment, receipt["verdict_note"])
        self.assertFalse(self.lock_exists())
        return receipt

    def test_leverage_10x_refuses_before_any_order(self):
        spec = happy_spec()
        spec["symbol_config"] = [{"symbol": SYMBOL, "leverage": "10"}]
        self.refuse(spec, "10x, not exactly 1x")

    def test_leverage_2x_refuses_even_inside_the_wall_cap(self):
        spec = happy_spec()
        spec["symbol_config"] = [{"symbol": SYMBOL, "leverage": "2"}]
        self.refuse(spec, "2x, not exactly 1x")

    def test_leverage_exactly_1x_passes_and_is_recorded(self):
        code, _, _ = self.run_once(happy_spec())
        self.assertEqual(code, 0)
        self.assertEqual(self.receipt()["preflight"]["symbol_leverage"], 1)

    def test_missing_symbol_row_refuses(self):
        spec = happy_spec()
        spec["symbol_config"] = [{"symbol": "OTHERUSDT", "leverage": "1"}]
        self.refuse(spec, "exactly one row")

    def test_unreadable_leverage_refuses(self):
        spec = happy_spec()
        spec["symbol_config"] = [{"symbol": SYMBOL, "leverage": None}]
        self.refuse(spec, "missing or unreadable")

    def test_unavailable_symbol_config_read_refuses(self):
        spec = happy_spec()
        spec["symbol_config"] = ("unclear", {"error": "TimeoutError"})
        self.refuse(spec, "symbol-configuration read unavailable")


class ValidateOnlyTests(ExecutorCase):
    """--validate-only: the complete signed preflight, zero POSTs, zero
    account changes, no lock taken, no receipt file written."""

    def validate(self, spec, cfg=None):
        transport = ScriptedTransport(spec)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = basis_executor.validate_only(cfg or self.cfg, transport,
                                                sleep=lambda seconds: None)
        return code, transport, buffer.getvalue()

    def test_all_proofs_pass_with_zero_posts_and_no_files(self):
        code, transport, out = self.validate(happy_spec())
        self.assertEqual(code, 0)
        self.assertEqual(transport.posts, [])
        self.assertTrue(all(call[0] == "GET" for call in transport.calls))
        self.assertIn("PREFLIGHT_OK", out)
        self.assertIn("leverage 1x", out)
        self.assertFalse(self.lock_exists())
        self.assertEqual(os.listdir(self.dir), [])

    def test_signed_preflight_paths_all_run_signed(self):
        _, transport, _ = self.validate(happy_spec())
        seen = {path: signed for _m, _b, path, _p, signed in transport.calls
                if path in PREFLIGHT_PATHS}
        self.assertEqual(set(seen), set(PREFLIGHT_PATHS))
        self.assertTrue(all(seen.values()))

    def test_wrong_leverage_refuses_with_exit_5(self):
        spec = happy_spec()
        spec["symbol_config"] = [{"symbol": SYMBOL, "leverage": "5"}]
        code, transport, out = self.validate(spec)
        self.assertEqual(code, 5)
        self.assertEqual(transport.posts, [])
        self.assertIn("PREFLIGHT_REFUSED", out)
        self.assertIn("5x, not exactly 1x", out)
        self.assertEqual(os.listdir(self.dir), [])

    def test_plan_refusal_still_runs_full_signed_preflight(self):
        cfg = ExecutorConfig(symbol=SYMBOL, sha=SHA, receipt_dir=self.dir,
                             run_id=RID, min_entry_bps=Decimal("1000000"))
        spec = happy_spec()
        spec["usdm_account"] = {
            "availableBalance": format(MAX_CYCLE_QUOTE, "f")}
        code, transport, out = self.validate(spec, cfg=cfg)
        self.assertEqual(code, 2)
        self.assertIn("PLAN_REFUSED", out)
        self.assertIn("PREFLIGHT_OK", out)
        self.assertIn("required %s USDT" % wire(MAX_CYCLE_QUOTE), out)
        seen = {path for _m, _b, path, _p, signed in transport.calls if signed}
        self.assertEqual(seen, set(PREFLIGHT_PATHS))
        self.assertEqual(transport.posts, [])
        self.assertEqual(os.listdir(self.dir), [])

    def test_plan_refusal_with_wrong_leverage_still_refuses_exit_5(self):
        cfg = ExecutorConfig(symbol=SYMBOL, sha=SHA, receipt_dir=self.dir,
                             run_id=RID, min_entry_bps=Decimal("1000000"))
        spec = happy_spec()
        spec["symbol_config"] = [{"symbol": SYMBOL, "leverage": "10"}]
        code, transport, out = self.validate(spec, cfg=cfg)
        self.assertEqual(code, 5)
        self.assertIn("PLAN_REFUSED", out)
        self.assertIn("10x, not exactly 1x", out)
        self.assertEqual(transport.posts, [])


if __name__ == "__main__":
    unittest.main()
