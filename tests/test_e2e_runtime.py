"""End-to-end crash-boundary tests for the restart-safe basis runtime.

These tests keep the real watch proposal writer, supervisor tick driver, and
position machine in the path.  Only exchange/network transport is injected.
Every test owns a fresh receipt directory and every simulated exchange keeps
an exact client-id ledger across fresh transport objects.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_position as bp  # noqa: E402
import basis_supervisor as bs  # noqa: E402
import basis_watch  # noqa: E402
from basis_position import PositionConfig  # noqa: E402
from tests.test_basis_position import (  # noqa: E402
    SHA,
    SYMBOL,
    T_OPEN,
    ScriptedTransport,
    happy_spec,
    proposal_payload,
)
from tests.test_basis_watch import (  # noqa: E402
    FakeReads,
    StubKernel,
    scenario_bundle,
    universe_row,
)


class SimulatedCrash(BaseException):
    """A process kill: application exception handlers must not absorb it."""


class CrashAfterClaimClock:
    """First timestamp lets the claim persist; the open's timestamp kills."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls == 2:
            raise SimulatedCrash("after claim")
        return T_OPEN


class ExchangeLedger:
    """Exchange truth shared by otherwise fresh injected transports."""

    def __init__(self):
        self.orders = {}
        self.trades = {}
        self.posted_client_ids = []
        self.post_counts = {}
        self.base_free = Decimal("0")
        self.short_amount = Decimal("0")
        self.hidden_clients = set()
        self.next_order_id = 700000
        self.exit_spot_filled = False

    @property
    def stages(self):
        return [client.split("-", 2)[2]
                for client in self.posted_client_ids]

    def record_post(self, client_id):
        count = self.post_counts.get(client_id, 0) + 1
        self.post_counts[client_id] = count
        if count != 1:
            raise AssertionError("client id POSTed more than once: %s"
                                 % client_id)
        self.posted_client_ids.append(client_id)


class LedgerTransport:
    """Deterministic transport with durable orders and injectable kills."""

    def __init__(self, ledger, *, crash=None, spot_ask="100.0",
                 perp_bid="100.2"):
        self.ledger = ledger
        self.crash = crash
        self.crashed = False
        self.calls = []
        self.posts = []
        spec = happy_spec()
        spec["spot_book"] = {
            "symbol": SYMBOL, "bidPrice": "99.9", "askPrice": spot_ask,
        }
        spec["usdm_book"] = {
            "symbol": SYMBOL, "bidPrice": perp_bid,
            "askPrice": str(max(Decimal(perp_bid), Decimal("100.3"))),
            "time": T_OPEN,
        }
        spec["symbol_config_seq"] = [
            [{"symbol": SYMBOL, "leverage": "10"}],
            [{"symbol": SYMBOL, "leverage": "10"}],
            [{"symbol": SYMBOL, "leverage": "1"}],
        ]
        self.base = ScriptedTransport(spec)

    def _kill(self, label):
        if self.crash == label and not self.crashed:
            self.crashed = True
            raise SimulatedCrash(label)

    @staticmethod
    def _number(value):
        return format(Decimal(value), "f")

    def _submit(self, base_url, params):
        client = params["newClientOrderId"]
        stage = client.split("-", 2)[2]
        self._kill("before_post:%s" % stage)
        self.ledger.record_post(client)
        self.posts.append((base_url, dict(params)))
        self.ledger.next_order_id += 1
        order_id = self.ledger.next_order_id

        fills = []
        if stage == "enter_spot":
            executed = Decimal("0.09900000")
            quote = Decimal("9.90000000")
            commission = Decimal("0.00009900")
            self.ledger.base_free += executed - commission
            fills = [{"qty": self._number(executed),
                      "commission": self._number(commission),
                      "commissionAsset": "BTC"}]
            quote_field = "cummulativeQuoteQty"
        elif stage == "enter_short":
            executed = Decimal(params["quantity"])
            quote = executed * Decimal("100.2")
            self.ledger.short_amount -= executed
            quote_field = "cumQuote"
        elif stage == "exit_short":
            executed = Decimal(params["quantity"])
            quote = executed * Decimal("100.1")
            self.ledger.short_amount += executed
            if abs(self.ledger.short_amount) < Decimal("1e-12"):
                self.ledger.short_amount = Decimal("0")
            quote_field = "cumQuote"
        elif stage in ("exit_spot", "unwind_spot"):
            executed = Decimal(params["quantity"])
            quote = executed * Decimal("99.9")
            self.ledger.base_free -= executed
            if abs(self.ledger.base_free) < Decimal("1e-12"):
                self.ledger.base_free = Decimal("0")
            commission = quote * Decimal("0.001")
            fills = [{"qty": self._number(executed),
                      "commission": self._number(commission),
                      "commissionAsset": "USDT"}]
            quote_field = "cummulativeQuoteQty"
            if stage == "exit_spot":
                self.ledger.exit_spot_filled = True
        else:  # pragma: no cover - a new real lifecycle stage must be explicit
            raise AssertionError("unexpected order stage %s" % stage)

        order = {
            "symbol": SYMBOL, "clientOrderId": client,
            "status": "FILLED",
            "executedQty": self._number(executed),
            quote_field: self._number(quote),
            "orderId": order_id,
        }
        self.ledger.orders[client] = order
        self.ledger.trades[str(order_id)] = list(fills)
        response_fills = [] if self.crash == "after_spot_fill" \
            and stage == "enter_spot" else fills
        self._kill("after_post:%s" % stage)
        return "ok", dict(order, fills=response_fills)

    def __call__(self, method, base_url, path, params=None, signed=False):
        params = dict(params or {})
        self.calls.append((method, base_url, path, params, signed))
        if method == "POST" and path == "/fapi/v1/leverage":
            return self.base(method, base_url, path, params, signed)
        if method == "POST" and "newClientOrderId" in params:
            return self._submit(base_url, params)
        if method == "GET" and path.endswith("/order"):
            client = params["origClientOrderId"]
            if client in self.ledger.hidden_clients:
                return "unclear", {}
            order = self.ledger.orders.get(client)
            if order is None:
                return "api_error", {
                    "code": -2013, "msg": "Order does not exist.",
                }
            return "ok", dict(order)
        if method == "GET" and path.endswith("positionRisk"):
            if self.ledger.exit_spot_filled:
                self._kill("after_close_fill")
            return "ok", [{"symbol": SYMBOL,
                            "positionAmt": self._number(
                                self.ledger.short_amount)}]
        if method == "GET" and path == "/api/v3/account":
            return "ok", {
                "canTrade": True,
                "balances": [
                    {"asset": "USDT", "free": "1000", "locked": "0"},
                    {"asset": "BTC",
                     "free": self._number(self.ledger.base_free),
                     "locked": "0"},
                ],
            }
        if method == "GET" and path.endswith("myTrades"):
            self._kill("after_spot_fill")
            return "ok", list(self.ledger.trades.get(
                str(params.get("orderId")), []))
        if method == "GET" and path.endswith("ticker/bookTicker"):
            self._kill("before_first_check")
        return self.base(method, base_url, path, params, signed)


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.receipt_dir = holder.name
        self.ledger = ExchangeLedger()
        env = mock.patch.dict(os.environ, {"QUANT_SHA": SHA})
        env.start()
        self.addCleanup(env.stop)
        self.write_entry_authorization()

    def write_entry_authorization(self, operator="e2e-supervisor",
                                  now=T_OPEN):
        created = datetime.fromtimestamp(
            now / 1000, timezone.utc).replace(microsecond=0)
        expires = created + timedelta(minutes=10)
        acceptance = {
            "operator": operator,
            "created_utc": created.isoformat().replace("+00:00", "Z"),
            "expires_utc": expires.isoformat().replace("+00:00", "Z"),
            "max_notional": "32",
        }
        with open(os.path.join(self.receipt_dir, bs.ACCEPT_ONE_FILE), "w",
                  encoding="utf-8") as handle:
            json.dump(acceptance, handle, sort_keys=True)
        with open(os.path.join(self.receipt_dir, "OPERATOR"), "w",
                  encoding="utf-8") as handle:
            json.dump({"operator": operator}, handle, sort_keys=True)

    def config(self, **changes):
        values = {
            "symbol": "",
            "sha": SHA,
            "receipt_dir": self.receipt_dir,
            "max_hold_ms": 86400000,
            "min_remaining_edge_pct": Decimal("0.01"),
            "max_adverse_move_pct": Decimal("0.5"),
        }
        values.update(changes)
        return PositionConfig(**values)

    def publish_watch_proposal(self, ttl_seconds=120):
        state_path = os.path.join(
            self.receipt_dir, "watch", "basis_watch_state.json")
        cfg = basis_watch.watch_config({
            "persist_ticks": 1,
            "proposal_ttl_seconds": ttl_seconds,
        })
        watch = basis_watch.Watch(state_path, self.receipt_dir, cfg)
        reads = FakeReads(
            [universe_row(SYMBOL)],
            {SYMBOL: scenario_bundle(T_OPEN, rate="0.01")},
        )
        with mock.patch.object(basis_watch, "basis_kernel", StubKernel()):
            rows = watch.run_pass(reads, T_OPEN)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposal_status"], "written")
        proposal_path = os.path.join(
            self.receipt_dir, bp.PROPOSAL_DIR,
            "proposal-%s.json" % rows[0]["proposal_id"])
        with open(proposal_path, encoding="utf-8") as handle:
            self.assert_full_equation_net(json.load(handle))
        return rows[0]["proposal_id"]

    def assert_full_equation_net(self, payload):
        intended_exit_gap = max(
            Decimal("0"), Decimal(payload["exit_gap_pct"]))
        four_commissions = (
            Decimal("2") * Decimal(payload["spot_taker_commission_pct"])
            + Decimal("2") * Decimal(payload["perp_taker_commission_pct"])
        )
        expected = (
            Decimal(payload["entry_gap_pct"]) - intended_exit_gap
            + Decimal(payload["carry_pct"]) - four_commissions
            - Decimal(payload["entry_slippage_pct"])
            - Decimal(payload["exit_slippage_pct"])
            - Decimal(payload["safety_buffer_pct"])
        )
        self.assertEqual(Decimal(payload["net_edge_pct"]), expected)

    def write_hand_built_proposal(self, proposal_id, **changes):
        payload = proposal_payload(proposal_id)
        payload.update(changes)
        self.assert_full_equation_net(payload)
        directory = os.path.join(self.receipt_dir, bp.PROPOSAL_DIR)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "proposal-%s.json" % proposal_id)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        return path

    def tick(self, transport, now=T_OPEN, claimant="e2e-supervisor"):
        output = io.StringIO()
        with redirect_stdout(output):
            code = bs.tick(
                self.config(), transport, sleep=lambda _seconds: None,
                now_ms=lambda: now, claimant=claimant)
        return code, output.getvalue()

    def crash_tick(self, transport, now=T_OPEN,
                   claimant="e2e-supervisor"):
        output = io.StringIO()
        with self.assertRaises(SimulatedCrash):
            with redirect_stdout(output):
                bs.tick(self.config(), transport,
                        sleep=lambda _seconds: None,
                        now_ms=lambda: now, claimant=claimant)
        return output.getvalue()

    def position(self):
        return bp.load_position(self.receipt_dir)

    def claim_path(self, proposal_id):
        return os.path.join(
            self.receipt_dir, bp.PROPOSAL_DIR,
            "proposal-%s.claim" % proposal_id)

    def assert_posts_are_unique_and_receipted(self):
        self.assertTrue(all(count == 1
                            for count in self.ledger.post_counts.values()))
        receipted = set()
        for path in Path(self.receipt_dir).glob("basis-position-*.json"):
            with path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
            for leg in payload.get("legs", []):
                client = leg.get("client_id")
                if client:
                    receipted.add(client)
        position = self.position()
        if position:
            receipted.update(position.get("client_ids", {}))
        self.assertLessEqual(set(self.ledger.posted_client_ids), receipted)

    def opening_transport(self, **changes):
        return LedgerTransport(
            self.ledger, spot_ask="100.02", perp_bid="100.800156",
            **changes)

    def assert_revalidation_precedes_leverage(self, transport):
        leverage_indexes = [
            index for index, call in enumerate(transport.calls)
            if call[0] == "POST" and call[2] == "/fapi/v1/leverage"
        ]
        self.assertEqual(len(leverage_indexes), 1)
        leverage_index = leverage_indexes[0]
        book_reads = [
            (index, call[2]) for index, call in enumerate(transport.calls)
            if call[0] == "GET" and call[2].endswith("ticker/bookTicker")
        ]
        self.assertEqual(
            [path for _index, path in book_reads],
            ["/api/v3/ticker/bookTicker",
             "/fapi/v1/ticker/bookTicker"])
        self.assertTrue(
            all(index < leverage_index for index, _path in book_reads),
            "entry revalidation must precede the leverage mutation")

    def open_from_watch(self):
        proposal_id = self.publish_watch_proposal()
        transport = self.opening_transport()
        code, output = self.tick(transport)
        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assert_revalidation_precedes_leverage(transport)
        self.assertEqual(self.position()["state"], bp.ST_OPEN)
        self.assertEqual(self.position()["proposal_id"], proposal_id)
        self.assertEqual(self.ledger.stages, ["enter_spot", "enter_short"])
        return proposal_id


class EntryBoundaryE2ETests(RuntimeCase):
    def test_stop_blocks_all_entry_ticks_then_removal_claims_and_opens(self):
        proposal_id = self.publish_watch_proposal()
        stop = os.path.join(self.receipt_dir, bp.STOP_FILE)
        with open(stop, "w", encoding="utf-8") as handle:
            handle.write("owner stop\n")

        for offset in (10, 20):
            transport = LedgerTransport(self.ledger)
            code, output = self.tick(transport, T_OPEN + offset)
            self.assertEqual(code, bs.EXIT_IDLE, output)
            self.assertEqual(transport.calls, [])
            self.assertFalse(os.path.exists(self.claim_path(proposal_id)))
        os.unlink(stop)

        transport = self.opening_transport()
        code, output = self.tick(transport, T_OPEN + 30)
        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assert_revalidation_precedes_leverage(transport)
        self.assertTrue(os.path.exists(self.claim_path(proposal_id)))
        self.assertEqual(self.position()["state"], bp.ST_OPEN)
        self.assertEqual(self.ledger.stages, ["enter_spot", "enter_short"])
        self.assert_posts_are_unique_and_receipted()

    def test_hand_built_non_positive_net_proposal_is_refused_on_disk(self):
        proposal_id = "negative-net"
        proposal = self.write_hand_built_proposal(
            proposal_id, funding_rate="0.004604004004",
            carry_pct="0.4604004004", net_edge_pct="0")
        self.assertTrue(os.path.exists(proposal))
        for offset in (0, 1000):
            transport = LedgerTransport(self.ledger)
            code, output = self.tick(transport, T_OPEN + offset)
            self.assertEqual(code, bs.EXIT_IDLE, output)
            self.assertEqual(transport.calls, [])
            self.assertIn("net_edge_pct must be positive", output)
        self.assertFalse(os.path.exists(self.claim_path(proposal_id)))
        self.assertIsNone(self.position())
        self.assert_posts_are_unique_and_receipted()

    def test_expired_watch_proposal_is_skipped_forever(self):
        proposal_id = self.publish_watch_proposal(ttl_seconds=1)
        for offset in (1001, 60000):
            transport = LedgerTransport(self.ledger)
            code, output = self.tick(transport, T_OPEN + offset)
            self.assertEqual(code, bs.EXIT_IDLE, output)
            self.assertEqual(transport.calls, [])
            self.assertIn("expired", output)
        self.assertFalse(os.path.exists(self.claim_path(proposal_id)))
        self.assertIsNone(self.position())
        self.assert_posts_are_unique_and_receipted()


class ExitRoutingE2ETests(RuntimeCase):
    def assert_automatic_exit(self, *, now, perp_bid, trigger):
        self.open_from_watch()
        before = len(self.ledger.posted_client_ids)
        transport = LedgerTransport(self.ledger, perp_bid=perp_bid)
        code, output = self.tick(transport, now)
        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertEqual(self.ledger.stages[before:],
                         ["exit_short", "exit_spot"])
        position = self.position()
        self.assertEqual(position["state"], bp.ST_CLOSED)
        self.assertEqual(position["last_check"]["decision"], bp.REQUEST_EXIT)
        self.assertEqual(position["last_check"]["why"], trigger)
        self.assertIn("SUPERVISOR | CLOSE | AUTOMATIC", output)
        self.assert_posts_are_unique_and_receipted()

    def test_gap_below_watch_routes_to_receipted_automatic_close(self):
        self.assert_automatic_exit(
            now=T_OPEN + 1000, perp_bid="100.05",
            trigger="THESIS_TARGET_REACHED")

    def test_max_hold_routes_to_receipted_automatic_close(self):
        self.assert_automatic_exit(
            now=T_OPEN + 86400000, perp_bid="100.2",
            trigger="MAXIMUM_HOLDING_HORIZON")

    def test_edge_inversion_routes_to_receipted_automatic_close(self):
        self.assert_automatic_exit(
            now=T_OPEN + 1000, perp_bid="100.16",
            trigger="REMAINING_EDGE_EXHAUSTED")

    def test_emergency_file_closes_on_next_tick_without_economic_reads(self):
        self.open_from_watch()
        emergency = os.path.join(self.receipt_dir, bp.EMERGENCY_FILE)
        with open(emergency, "w", encoding="utf-8") as handle:
            handle.write("owner emergency\n")
        before = len(self.ledger.posted_client_ids)
        transport = LedgerTransport(self.ledger)
        code, output = self.tick(transport, T_OPEN + 1000)
        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertEqual(self.ledger.stages[before:],
                         ["exit_short", "exit_spot"])
        self.assertEqual(self.position()["state"], bp.ST_CLOSED)
        public_books = [path for method, _base, path, _params, signed
                        in transport.calls
                        if method == "GET" and not signed
                        and (path.endswith("ticker/bookTicker")
                             or path.endswith("premiumIndex"))]
        self.assertEqual(public_books, [])
        self.assertIn("EMERGENCY_FILE_PRESENT", output)
        self.assertIn("REQUEST_EMERGENCY_UNWIND", output)
        self.assert_posts_are_unique_and_receipted()


class CrashBoundaryE2ETests(RuntimeCase):
    def test_crash_after_claim_before_open_is_adopted_once(self):
        self.write_entry_authorization(operator="stable-supervisor")
        proposal_id = self.publish_watch_proposal()
        output = io.StringIO()
        with self.assertRaises(SimulatedCrash):
            with redirect_stdout(output):
                bs.tick(self.config(), LedgerTransport(self.ledger),
                        sleep=lambda _seconds: None,
                        now_ms=CrashAfterClaimClock(),
                        claimant="stable-supervisor")
        self.assertTrue(os.path.exists(self.claim_path(proposal_id)))
        self.assertIsNone(self.position())
        self.assertEqual(self.ledger.posted_client_ids, [])

        transport = self.opening_transport()
        code, recovered = self.tick(
            transport, claimant="stable-supervisor")
        self.assertEqual(code, bs.EXIT_ACTED, recovered)
        self.assert_revalidation_precedes_leverage(transport)
        self.assertIn("SUPERVISOR | CLAIM | RECOVERED", recovered)
        self.assertEqual(self.position()["state"], bp.ST_OPEN)
        self.assertEqual(self.ledger.stages, ["enter_spot", "enter_short"])
        self.assert_posts_are_unique_and_receipted()

    def test_crash_after_spot_intent_before_post_reconciles_definite_absence(self):
        self.publish_watch_proposal()
        crashing = self.opening_transport(
            crash="before_post:enter_spot")
        self.crash_tick(crashing)
        self.assert_revalidation_precedes_leverage(crashing)
        position = self.position()
        self.assertEqual(position["state"], bp.ST_OPENING)
        self.assertEqual(position["pending_intent"]["stage"],
                         "ENTER_SPOT")
        self.assertEqual(self.ledger.posted_client_ids, [])

        code, output = self.tick(LedgerTransport(self.ledger))
        self.assertEqual(code, bs.EXIT_IDLE, output)
        self.assertEqual(self.position()["state"], bp.ST_NONE)
        self.assertEqual(
            set(self.position()["reconcile"]["truths"].values()),
            {"not_found"})
        self.assertEqual(self.ledger.posted_client_ids, [])
        self.assert_posts_are_unique_and_receipted()

    def test_crash_after_spot_fill_before_short_intent_freezes_one_leg(self):
        self.publish_watch_proposal()
        crashing = self.opening_transport(crash="after_spot_fill")
        self.crash_tick(crashing)
        self.assert_revalidation_precedes_leverage(crashing)
        self.assertEqual(self.ledger.stages, ["enter_spot"])
        self.assertEqual(self.position()["state"], bp.ST_OPENING)
        self.assertFalse(any(meta["stage"] == "enter_short"
                             for meta in self.position()["client_ids"].values()))

        code, output = self.tick(LedgerTransport(self.ledger))
        self.assertEqual(code, bs.EXIT_FROZEN, output)
        self.assertEqual(self.position()["state"], bp.ST_ONE_LEG)
        self.assertGreater(self.ledger.base_free, Decimal("0"))
        self.assertEqual(self.ledger.short_amount, Decimal("0"))
        self.assertEqual(self.ledger.stages, ["enter_spot"])
        self.assert_posts_are_unique_and_receipted()

    def test_crash_after_short_post_freezes_unknown_then_reconcile_resolves(self):
        self.publish_watch_proposal()
        crashing = self.opening_transport(crash="after_post:enter_short")
        self.crash_tick(crashing)
        self.assert_revalidation_precedes_leverage(crashing)
        short_client = self.ledger.posted_client_ids[-1]
        self.ledger.hidden_clients.add(short_client)
        self.assertEqual(self.position()["state"], bp.ST_OPENING)
        self.assertEqual(self.position()["pending_intent"]["client_id"],
                         short_client)

        before = list(self.ledger.posted_client_ids)
        code, frozen = self.tick(LedgerTransport(self.ledger))
        self.assertEqual(code, bs.EXIT_FROZEN, frozen)
        self.assertEqual(self.position()["state"], bp.ST_UNKNOWN)
        self.assertEqual(
            self.position()["reconcile"]["truths"][short_client],
            "pending")
        self.assertEqual(self.ledger.posted_client_ids, before)

        self.ledger.hidden_clients.remove(short_client)
        code, resolved = self.tick(LedgerTransport(self.ledger))
        self.assertEqual(code, bs.EXIT_HELD, resolved)
        self.assertEqual(self.position()["state"], bp.ST_OPEN)
        self.assertEqual(self.ledger.posted_client_ids, before)
        self.assert_posts_are_unique_and_receipted()

    def test_crash_after_open_complete_before_first_check_recovers_open_truth(self):
        self.open_from_watch()
        before = list(self.ledger.posted_client_ids)
        self.crash_tick(LedgerTransport(
            self.ledger, crash="before_first_check"), now=T_OPEN + 1000)
        self.assertEqual(self.position()["state"], bp.ST_OPEN)
        self.assertEqual(self.ledger.posted_client_ids, before)

        code, output = self.tick(
            LedgerTransport(self.ledger), T_OPEN + 2000)
        self.assertEqual(code, bs.EXIT_HELD, output)
        self.assertEqual(self.position()["state"], bp.ST_OPEN)
        self.assertEqual(self.position()["last_check"]["decision"], bp.HOLD)
        self.assertEqual(self.ledger.posted_client_ids, before)
        self.assert_posts_are_unique_and_receipted()

    def test_crash_after_close_intent_before_post_resumes_and_closes(self):
        self.open_from_watch()
        before = list(self.ledger.posted_client_ids)
        self.crash_tick(LedgerTransport(
            self.ledger, crash="before_post:exit_short",
            perp_bid="100.05"), now=T_OPEN + 1000)
        crashed_position = self.position()
        self.assertEqual(crashed_position["state"], bp.ST_CLOSING)
        old_close_client = crashed_position["pending_intent"]["client_id"]
        self.assertNotIn(old_close_client, self.ledger.posted_client_ids)

        code, output = self.tick(
            LedgerTransport(self.ledger), T_OPEN + 2000)
        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertEqual(self.position()["state"], bp.ST_CLOSED)
        self.assertEqual(self.ledger.stages[len(before):],
                         ["exit_short", "exit_spot"])
        self.assertNotIn(old_close_client, self.ledger.posted_client_ids)
        self.assert_posts_are_unique_and_receipted()

    def test_crash_after_close_fill_before_final_reconcile_recovers_closed(self):
        self.open_from_watch()
        closing = LedgerTransport(
            self.ledger, crash="after_close_fill", perp_bid="100.05")
        self.crash_tick(closing, now=T_OPEN + 1000)
        before = list(self.ledger.posted_client_ids)
        self.assertEqual(self.ledger.stages,
                         ["enter_spot", "enter_short",
                          "exit_short", "exit_spot"])
        self.assertEqual(self.position()["state"], bp.ST_CLOSING)
        self.assertEqual(self.ledger.short_amount, Decimal("0"))
        self.assertLess(self.ledger.base_free, Decimal("0.0001"))

        code, output = self.tick(
            LedgerTransport(self.ledger), T_OPEN + 2000)
        self.assertEqual(code, bs.EXIT_IDLE, output)
        self.assertEqual(self.position()["state"], bp.ST_CLOSED)
        self.assertEqual(self.ledger.posted_client_ids, before)
        self.assert_posts_are_unique_and_receipted()


if __name__ == "__main__":
    unittest.main()
