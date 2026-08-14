"""Offline integration tests for the stateless basis supervisor."""

import dataclasses
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_position as bp  # noqa: E402
import basis_supervisor as bs  # noqa: E402
import basis_watch as bw  # noqa: E402
from basis_position import PositionConfig  # noqa: E402
from tests.test_basis_position import (  # noqa: E402
    FLAT_POSITION,
    MISSING,
    PROPOSAL_ID,
    SHA,
    SYMBOL,
    T_OPEN,
    ScriptedTransport,
    happy_spec,
    post_stages,
    proposal_payload,
)
from tests.test_basis_watch import (  # noqa: E402
    FakeReads,
    StubKernel,
    scenario_bundle,
    stream_record,
)


_real_getaddrinfo = socket.getaddrinfo
ALT_SYMBOL = "ETHUSDT"
ALT_BASE = "ETH"


def setUpModule():
    def deny(*args, **kwargs):
        raise AssertionError("network access attempted in offline tests")
    socket.getaddrinfo = deny


def tearDownModule():
    socket.getaddrinfo = _real_getaddrinfo


class PositionSequenceTransport:
    """Delegate transport with ordered positionRisk observations."""

    def __init__(self, spec, positions):
        self.base = ScriptedTransport(spec)
        self.positions = list(positions)

    @property
    def calls(self):
        return self.base.calls

    @property
    def posts(self):
        return self.base.posts

    def __call__(self, method, base_url, path, params=None, signed=False):
        if method == "GET" and path.endswith("positionRisk"):
            params = dict(params or {})
            self.base.calls.append((method, base_url, path, params, signed))
            value = self.positions.pop(0) if len(self.positions) > 1 \
                else self.positions[0]
            return ("ok", value)
        return self.base(method, base_url, path, params, signed)


class BasisSupervisorTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = holder.name
        self.cfg = PositionConfig(
            symbol="", sha=SHA, receipt_dir=self.dir,
            max_hold_ms=86400000,
            min_remaining_edge_pct=Decimal("0.01"),
            max_adverse_move_pct=Decimal("0.5"))
        self.governor_jobs = []
        self.governor_releases = []
        self.write_proposal()

    def acquire_governor(self, job, receipt_dir):
        self.governor_jobs.append((job, receipt_dir))

        def release(token, released_dir):
            self.governor_releases.append((token, released_dir))

        return "basis-governor-token", release, ""

    def cfg_for(self, symbol):
        return dataclasses.replace(self.cfg, symbol=symbol)

    def spec_for(self, symbol, base_asset):
        def replace_symbols(value):
            if isinstance(value, dict):
                return {key: replace_symbols(item)
                        for key, item in value.items()}
            if isinstance(value, list):
                return [replace_symbols(item) for item in value]
            if isinstance(value, tuple):
                return tuple(replace_symbols(item) for item in value)
            if isinstance(value, str):
                return value.replace(SYMBOL, symbol).replace("BTC", base_asset)
            return value

        spec = replace_symbols(happy_spec())
        spec.update({
            "spot_commission": {
                "symbol": symbol,
                "standardCommission": {
                    "maker": "0.00100000", "taker": "0.00100000"}},
            "usdm_commission": {
                "symbol": symbol, "makerCommissionRate": "0.000200",
                "takerCommissionRate": "0.000400"},
            "symbol_config": [{"symbol": symbol, "leverage": "1"}],
            "leverage_post": {"symbol": symbol, "leverage": 1},
            "position": [{"symbol": symbol, "positionAmt": "0.000"}],
        })
        return spec

    def write_proposal(self, payload=None, proposal_id=PROPOSAL_ID):
        directory = os.path.join(self.dir, bp.PROPOSAL_DIR)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "proposal-%s.json" % proposal_id)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload or proposal_payload(proposal_id), handle,
                      sort_keys=True)
        return path

    def claim_path(self, proposal_id=PROPOSAL_ID):
        return os.path.join(self.dir, bp.PROPOSAL_DIR,
                            "proposal-%s.claim" % proposal_id)

    def position(self):
        return bp.load_position(self.dir)

    def write_accept_one(self, operator="test-supervisor"):
        created = datetime.fromtimestamp(
            T_OPEN / 1000, timezone.utc).replace(microsecond=0)
        expires = created + timedelta(minutes=10)
        payload = {
            "operator": operator,
            "created_utc": created.isoformat().replace("+00:00", "Z"),
            "expires_utc": expires.isoformat().replace("+00:00", "Z"),
            "max_notional": "32",
        }
        with open(os.path.join(self.dir, bs.ACCEPT_ONE_FILE), "w",
                  encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        with open(os.path.join(self.dir, "OPERATOR"), "w",
                  encoding="utf-8") as handle:
            json.dump({"operator": operator}, handle, sort_keys=True)

    def run_tick(self, transport, claimant="test-supervisor", *,
                 accept_result=True, governor_result=None,
                 patch_entry_gates=True):
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            if patch_entry_gates:
                if accept_result is not None:
                    stack.enter_context(mock.patch.object(
                        bs, "_entry_ticket_refusal",
                        return_value="" if accept_result else
                        "ENTRY_BLOCKED_NOT_ACCEPTED"))
                    stack.enter_context(mock.patch.object(
                        bs, "_consume_accept_one",
                        return_value=accept_result))
                if governor_result is None:
                    stack.enter_context(mock.patch.object(
                        bs, "_acquire_governor",
                        side_effect=self.acquire_governor))
                else:
                    stack.enter_context(mock.patch.object(
                        bs, "_acquire_governor",
                        return_value=governor_result))
            code = bs.tick(
                self.cfg, transport, sleep=lambda _seconds: None,
                now_ms=lambda: T_OPEN, claimant=claimant)
        return code, output.getvalue()

    def open_directly(self, spec=None, symbol=SYMBOL,
                      proposal_id=PROPOSAL_ID):
        output = io.StringIO()
        transport = ScriptedTransport(spec or happy_spec())
        with redirect_stdout(output):
            code = bp.cmd_open(
                self.cfg_for(symbol), transport,
                sleep=lambda _seconds: None,
                now_ms=lambda: T_OPEN, proposal_id=proposal_id)
        self.assertEqual(code, 0, output.getvalue())

    def test_claim_open_happy_path_from_valid_proposal(self):
        transport = ScriptedTransport(happy_spec())
        code, output = self.run_tick(transport)

        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short"])
        self.assertEqual(self.position()["state"], bp.ST_OPEN)
        self.assertEqual(self.position()["proposal_id"], PROPOSAL_ID)
        with open(self.claim_path(), encoding="utf-8") as handle:
            claim = json.load(handle)
        self.assertEqual(claim["claimant"], "test-supervisor")
        self.assertTrue(claim["utc"].endswith("Z"))
        self.assertLess(output.index("SUPERVISOR | CLAIM | CREATED"),
                        output.index("SUPERVISOR | OPEN | DONE"))

    def test_entry_without_accept_one_is_held_before_governor_or_open(self):
        transport = ScriptedTransport(happy_spec())
        with mock.patch.object(bs, "cmd_open") as open_call, \
                mock.patch.object(bs, "_acquire_governor") as acquire:
            code, output = self.run_tick(
                transport, patch_entry_gates=False)

        self.assertEqual(code, bs.EXIT_HELD, output)
        self.assertIn(
            "SUPERVISOR | ENTRY | HELD | ENTRY_BLOCKED_NOT_ACCEPTED",
            output)
        self.assertEqual(transport.calls, [])
        open_call.assert_not_called()
        acquire.assert_not_called()

    def test_accept_one_is_consumed_and_entry_proceeds(self):
        self.write_accept_one()
        transport = ScriptedTransport(happy_spec())

        code, output = self.run_tick(transport, accept_result=None)

        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertFalse(os.path.exists(os.path.join(
            self.dir, bs.ACCEPT_ONE_FILE)))
        self.assertTrue(os.path.exists(os.path.join(
            self.dir, bs.ACCEPT_ONE_FILE + ".claimed-" + PROPOSAL_ID)))
        self.assertEqual(post_stages(transport),
                         ["enter_spot", "enter_short"])
        spot_entry = transport.posts[0][1]
        self.assertLessEqual(
            Decimal(spot_entry["quoteOrderQty"]),
            bs.FIRST_TRADE_CAP_USDT)
        self.assertEqual(len(self.governor_jobs), 1)
        job, receipt_dir = self.governor_jobs[0]
        self.assertEqual((job.op, job.symbol, receipt_dir),
                         ("BASIS_ROUNDTRIP", SYMBOL, self.dir))
        self.assertEqual(job.effective_notional,
                         bs.FIRST_TRADE_CAP_USDT)
        self.assertEqual(self.governor_releases,
                         [("basis-governor-token", self.dir)])

    def test_governor_refusal_holds_without_calling_open(self):
        self.write_accept_one()
        transport = ScriptedTransport(happy_spec())
        with mock.patch.object(bs, "cmd_open") as open_call:
            code, output = self.run_tick(
                transport,
                accept_result=None,
                governor_result=("", None, "ACCOUNT_FROZEN"))

        self.assertEqual(code, bs.EXIT_HELD, output)
        self.assertIn("SUPERVISOR | ENTRY | HELD | ACCOUNT_FROZEN", output)
        self.assertEqual(transport.calls, [])
        open_call.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(
            self.dir, bs.ACCEPT_ONE_FILE)))
        self.assertEqual(self.governor_releases, [])

    def test_governor_slot_is_retained_for_unknown_entry_outcome(self):
        unknown_spec = happy_spec()
        unknown_spec["legs"]["enter_spot"] = {
            "submit": ("unclear", {"http": 504}),
            "query": [lambda _params: ("unclear", {})],
        }

        code, output = self.run_tick(ScriptedTransport(unknown_spec))

        self.assertEqual(code, bs.EXIT_FROZEN, output)
        self.assertEqual(self.position()["state"], bp.ST_UNKNOWN)
        self.assertEqual(len(self.governor_jobs), 1)
        self.assertEqual(self.governor_releases, [])

    def test_reconciled_unknown_releases_persisted_governor_slot(self):
        unknown_spec = happy_spec()
        unknown_spec["legs"]["enter_spot"] = {
            "submit": ("unclear", {"http": 504}),
            "query": [lambda _params: ("unclear", {})],
        }
        code, output = self.run_tick(ScriptedTransport(unknown_spec))
        self.assertEqual(code, bs.EXIT_FROZEN, output)
        with open(self.claim_path(), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["governor_token"],
                             "basis-governor-token")
        unknown_client = next(iter(self.position()["client_ids"]))
        resolved_base = ScriptedTransport(happy_spec())

        def resolving_transport(method, base_url, path, params=None,
                                signed=False):
            if method == "GET" and path.endswith("/order") \
                    and (params or {}).get(
                        "origClientOrderId") == unknown_client:
                resolved_base.calls.append(
                    (method, base_url, path, dict(params or {}), signed))
                return MISSING
            return resolved_base(method, base_url, path, params, signed)

        with mock.patch.object(
                bs, "_release_recovered_governor",
                return_value=True) as recovered_release:
            code, output = self.run_tick(resolving_transport)

        self.assertEqual(code, bs.EXIT_IDLE, output)
        recovered_release.assert_called_once_with(
            "basis-governor-token", self.dir)
        with open(self.claim_path(), encoding="utf-8") as handle:
            self.assertNotIn("governor_token", json.load(handle))

    def test_first_trade_clamp_is_an_in_memory_proposal_view(self):
        proposal_path = self.write_proposal()
        with open(proposal_path, "rb") as handle:
            original = handle.read()
        observed = []

        def inspect_open(cfg, _request, sleep, now_ms, proposal_id):
            accepted = bp.load_entry_proposal(
                cfg.receipt_dir, proposal_id, cfg.symbol, cfg.sha, now_ms())
            observed.append(bs._accepted_notionals(accepted))
            return bp.EXIT_CODE["REFUSED"]

        with mock.patch.object(bs, "cmd_open", side_effect=inspect_open):
            code, output = self.run_tick(ScriptedTransport(happy_spec()))

        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertEqual(len(observed), 1)
        self.assertEqual(max(observed[0]), bs.FIRST_TRADE_CAP_USDT)
        self.assertTrue(all(value <= bs.FIRST_TRADE_CAP_USDT
                            for value in observed[0]))
        with open(proposal_path, "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertEqual(self.governor_jobs[0][0].effective_notional,
                         bs.FIRST_TRADE_CAP_USDT)
        self.assertEqual(self.governor_releases,
                         [("basis-governor-token", self.dir)])

    def test_first_trade_clamp_is_lifted_after_close_marker(self):
        Path(self.dir, bs.FIRST_AUTONOMOUS_CLOSE_MARKER).touch()
        observed = []

        def inspect_open(cfg, _request, sleep, now_ms, proposal_id):
            accepted = bp.load_entry_proposal(
                cfg.receipt_dir, proposal_id, cfg.symbol, cfg.sha, now_ms())
            observed.append(bs._accepted_notionals(accepted))
            return bp.EXIT_CODE["REFUSED"]

        with mock.patch.object(bs, "cmd_open", side_effect=inspect_open):
            code, output = self.run_tick(ScriptedTransport(happy_spec()))

        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertEqual(len(observed), 1)
        self.assertGreater(max(observed[0]), bs.FIRST_TRADE_CAP_USDT)
        job = self.governor_jobs[0][0]
        self.assertEqual(job.effective_notional, job.requested_max_notional)

    def test_stream_watch_builds_and_tick_claims_with_shared_health(self):
        os.unlink(os.path.join(
            self.dir, bp.PROPOSAL_DIR,
            "proposal-%s.json" % PROPOSAL_ID))
        stream = stream_record("0.78")
        stream.update({
            "spot_step_size": "0.0001", "perp_step_size": "0.001",
            "spot_min_notional": "5", "perp_min_notional": "5",
        })
        feed_dir = os.path.join(self.dir, "feed")
        os.makedirs(feed_dir, exist_ok=True)
        stream_path = os.path.join(feed_dir, "stream_state.json")
        with open(stream_path, "w", encoding="utf-8") as handle:
            json.dump({SYMBOL: stream}, handle)
        watch_cfg = bw.watch_config({"persist_ticks": 1})
        watch_state = os.path.join(
            self.dir, bw.WATCH_STATE_DIR_NAME, bw.STATE_FILE_NAME)
        watch = bw.Watch(watch_state, self.dir, watch_cfg)
        reads = bw.StreamStateReads(
            stream_path,
            FakeReads([], {SYMBOL: scenario_bundle(T_OPEN, rate="0.01")}),
            watch_cfg["stale_ms"])

        with mock.patch.object(bw, "basis_kernel", StubKernel()), \
                mock.patch.dict(os.environ, {"QUANT_SHA": SHA}):
            built = watch.run_pass(reads, T_OPEN)[0]

        self.assertEqual(built["proposal_status"], "written")
        proposal_id = built["proposal_id"]
        proposal_path = os.path.join(
            self.dir, bp.PROPOSAL_DIR,
            "proposal-%s.json" % proposal_id)
        with open(proposal_path, encoding="utf-8") as handle:
            proposal = json.load(handle)
        spot_ask = Decimal("100.02")
        perp_bid = spot_ask * (1 + Decimal("0.78") / 100)
        expected_qty = min(
            bw.PER_LEG_CAP_USDT / (1 + bw.FEE_BUFFER) / spot_ask,
            bw.PER_LEG_CAP_USDT / perp_bid,
        ).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
        self.assertEqual(Decimal(proposal["quantity"]), expected_qty)

        open_spec = happy_spec()
        open_spec["spot_book"] = {
            "symbol": SYMBOL, "bidPrice": proposal["spot_bid"],
            "askPrice": proposal["spot_ask"],
        }
        open_spec["usdm_book"] = {
            "symbol": SYMBOL, "bidPrice": proposal["perp_bid"],
            "askPrice": proposal["perp_ask"], "time": T_OPEN,
        }
        code, output = self.run_tick(ScriptedTransport(open_spec))

        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertTrue(os.path.exists(self.claim_path(proposal_id)))
        self.assertEqual(self.position()["proposal_id"], proposal_id)
        self.assertEqual(self.position()["state"], bp.ST_OPEN)
        with open(os.path.join(self.dir, bs.HEALTH_FILE_NAME),
                  encoding="utf-8") as handle:
            health = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual([row["component"] for row in health],
                         ["basis_watch", "basis_supervisor"])
        self.assertEqual(health[0]["state_counts"]["PROPOSED"], 1)
        self.assertEqual(health[1]["state_counts"][bp.ST_OPEN], 1)
        self.assertEqual(health[1]["last_pass_age_ms"], 0)
        self.assertEqual(health[1]["reject_histogram"], {})
        self.assertEqual(health[1]["status"], "OK")

    def test_best_net_proposal_across_symbols_opens_its_symbol(self):
        better_id = "eth-better"
        better = proposal_payload(better_id)
        better.update({"symbol": ALT_SYMBOL, "net_edge_pct": "0.50"})
        self.write_proposal(better, better_id)
        transport = ScriptedTransport(self.spec_for(ALT_SYMBOL, ALT_BASE))

        code, output = self.run_tick(transport)

        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertTrue(os.path.exists(self.claim_path(better_id)))
        self.assertFalse(os.path.exists(self.claim_path(PROPOSAL_ID)))
        self.assertEqual(self.position()["proposal_id"], better_id)
        self.assertEqual(self.position()["symbol"], ALT_SYMBOL)
        symbol_params = [params["symbol"]
                         for _method, _url, _path, params, _signed
                         in transport.calls if "symbol" in params]
        self.assertTrue(symbol_params)
        self.assertEqual(set(symbol_params), {ALT_SYMBOL})

    def test_standing_position_on_one_symbol_blocks_other_symbol_claim(self):
        self.open_directly()
        other_id = "eth-next"
        other = proposal_payload(other_id)
        other.update({"symbol": ALT_SYMBOL, "net_edge_pct": "0.90"})
        self.write_proposal(other, other_id)
        transport = PositionSequenceTransport(
            happy_spec(),
            [[{"symbol": SYMBOL, "positionAmt": "-0.098"}]])

        code, output = self.run_tick(transport)

        self.assertEqual(code, bs.EXIT_HELD, output)
        self.assertFalse(os.path.exists(self.claim_path(other_id)))
        self.assertNotIn("SUPERVISOR | CLAIM", output)
        self.assertEqual(self.position()["symbol"], SYMBOL)

    def test_resume_check_and_close_use_persisted_symbol_without_filter(self):
        proposal_id = "eth-position"
        proposal = proposal_payload(proposal_id)
        proposal["symbol"] = ALT_SYMBOL
        self.write_proposal(proposal, proposal_id)
        alt_spec = self.spec_for(ALT_SYMBOL, ALT_BASE)
        self.open_directly(alt_spec, ALT_SYMBOL, proposal_id)
        with open(os.path.join(self.dir, bp.EMERGENCY_FILE), "w",
                  encoding="utf-8") as handle:
            handle.write("owner emergency\n")
        transport = PositionSequenceTransport(
            alt_spec,
            [[{"symbol": ALT_SYMBOL, "positionAmt": "-0.098"}],
             [{"symbol": ALT_SYMBOL, "positionAmt": "0.000"}]])

        code, output = self.run_tick(transport)

        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertEqual(self.position()["state"], bp.ST_CLOSED)
        self.assertEqual(self.position()["symbol"], ALT_SYMBOL)
        symbol_params = [params["symbol"]
                         for _method, _url, _path, params, _signed
                         in transport.calls if "symbol" in params]
        self.assertTrue(symbol_params)
        self.assertEqual(set(symbol_params), {ALT_SYMBOL})
        self.assertIn("SUPERVISOR | CLOSE | AUTOMATIC", output)

    def test_environment_symbol_is_only_a_proposal_whitelist(self):
        better_id = "eth-filtered"
        better = proposal_payload(better_id)
        better.update({"symbol": ALT_SYMBOL, "net_edge_pct": "0.90"})
        self.write_proposal(better, better_id)
        env = {
            "SYMBOL": SYMBOL,
            "QUANT_SHA": SHA,
            "RECEIPT_DIR": self.dir,
            "MAX_HOLD_MS": "86400000",
            "MIN_REMAINING_EDGE_PCT": "0.01",
            "MAX_ADVERSE_MOVE_PCT": "0.5",
        }
        cfg = bs._config_from_env(env, "")
        transport = ScriptedTransport(happy_spec())
        output = io.StringIO()

        with redirect_stdout(output), \
                mock.patch.object(bs, "_entry_ticket_refusal",
                                  return_value=""), \
                mock.patch.object(bs, "_consume_accept_one",
                                  return_value=True), \
                mock.patch.object(bs, "_acquire_governor",
                                  side_effect=self.acquire_governor):
            code = bs.tick(
                cfg, transport, sleep=lambda _seconds: None,
                now_ms=lambda: T_OPEN, claimant="test-supervisor")

        text = output.getvalue()
        self.assertEqual(code, bs.EXIT_ACTED, text)
        self.assertEqual(self.position()["symbol"], SYMBOL)
        self.assertTrue(os.path.exists(self.claim_path(PROPOSAL_ID)))
        self.assertFalse(os.path.exists(self.claim_path(better_id)))
        self.assertIn("SUPERVISOR | PROPOSAL | FILTERED", text)

    def test_expired_proposal_is_skipped_once(self):
        expired = proposal_payload()
        expired["expires_utc"] = "2025-08-12T01:00:00Z"
        self.write_proposal(expired)
        transport = ScriptedTransport(happy_spec())

        code, output = self.run_tick(transport)

        self.assertEqual(code, bs.EXIT_IDLE, output)
        self.assertEqual(transport.calls, [])
        self.assertFalse(os.path.exists(self.claim_path()))
        self.assertEqual(
            output.count("SUPERVISOR | PROPOSAL | SKIPPED"), 1)
        self.assertIn("expired", output)

    def test_existing_claim_is_contention_and_skips_open(self):
        original = b'{"claimant":"another","claimed_utc":"prior"}\n'
        with open(self.claim_path(), "wb") as handle:
            handle.write(original)
        transport = ScriptedTransport(happy_spec())

        code, output = self.run_tick(transport)

        self.assertEqual(code, bs.EXIT_IDLE, output)
        self.assertEqual(transport.calls, [])
        with open(self.claim_path(), "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertIn("already owned", output)

    def test_stop_blocks_entry_but_open_position_checks_and_auto_closes(self):
        self.open_directly()
        with open(os.path.join(self.dir, bp.STOP_FILE), "w",
                  encoding="utf-8") as handle:
            handle.write("owner stop\n")
        spec = happy_spec()
        spec["spot_book"] = {"symbol": SYMBOL, "bidPrice": "100.0",
                             "askPrice": "100.0"}
        spec["usdm_book"] = {"symbol": SYMBOL, "bidPrice": "100.0",
                             "askPrice": "100.1", "time": T_OPEN}
        transport = PositionSequenceTransport(
            spec, [[{"symbol": SYMBOL, "positionAmt": "-0.098"}],
                   FLAT_POSITION])

        with mock.patch.object(
                bs, "_entry_ticket_refusal",
                side_effect=AssertionError("exit consulted entry ticket")), \
                mock.patch.object(
                bs, "_consume_accept_one",
                side_effect=AssertionError("exit consulted ACCEPT-ONE")), \
                mock.patch.object(
                    bs, "_acquire_governor",
                    side_effect=AssertionError("exit consulted governor")):
            code, output = self.run_tick(
                transport, patch_entry_gates=False)

        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertEqual(post_stages(transport.base),
                         ["exit_short", "exit_spot"])
        self.assertEqual(self.position()["state"], bp.ST_CLOSED)
        self.assertIn("DECISION: REQUEST_EXIT", output)
        self.assertIn("SUPERVISOR | CLOSE | AUTOMATIC", output)
        self.assertNotIn("SUPERVISOR | ENTRY", output)

    def test_emergency_file_forces_automatic_close(self):
        self.open_directly()
        with open(os.path.join(self.dir, bp.EMERGENCY_FILE), "w",
                  encoding="utf-8") as handle:
            handle.write("owner emergency\n")
        transport = PositionSequenceTransport(
            happy_spec(),
            [[{"symbol": SYMBOL, "positionAmt": "-0.098"}],
             FLAT_POSITION])

        code, output = self.run_tick(transport)

        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertEqual(self.position()["state"], bp.ST_CLOSED)
        self.assertEqual(post_stages(transport.base),
                         ["exit_short", "exit_spot"])
        public_paths = [path for method, _url, path, _params, signed
                        in transport.calls
                        if method == "GET" and not signed]
        self.assertFalse([path for path in public_paths
                          if path.endswith("ticker/bookTicker")
                          or path.endswith("premiumIndex")])
        self.assertIn("EMERGENCY_FILE_PRESENT", output)
        self.assertIn("REQUEST_EMERGENCY_UNWIND", output)

    def test_persisted_exit_survives_crash_between_check_and_close(self):
        self.open_directly()
        position = self.position()
        position["last_check"] = {
            "utc": "2025-08-12T00:00:00Z", "now_ms": T_OPEN,
            "current_gap_pct": "0.1", "exit_fees_pct": "0.15",
            "fees_source": "spec_default", "decision": bp.REQUEST_EXIT,
            "why": "THESIS_TARGET_REACHED",
        }
        bp.save_position(self.dir, position)
        transport = PositionSequenceTransport(
            happy_spec(),
            [[{"symbol": SYMBOL, "positionAmt": "-0.098"}],
             FLAT_POSITION])

        code, output = self.run_tick(transport)

        self.assertEqual(code, bs.EXIT_ACTED, output)
        self.assertIn("DECISION: HOLD", output)
        self.assertIn("SUPERVISOR | CLOSE | AUTOMATIC", output)
        self.assertEqual(self.position()["state"], bp.ST_CLOSED)

    def test_unknown_freezes_repeated_ticks_until_resume_resolves(self):
        unknown_spec = happy_spec()
        unknown_spec["legs"]["enter_spot"] = {
            "submit": ("unclear", {"http": 504}),
            "query": [lambda _params: ("unclear", {})],
        }
        output = io.StringIO()
        with redirect_stdout(output):
            code = bp.cmd_open(
                self.cfg_for(SYMBOL), ScriptedTransport(unknown_spec),
                sleep=lambda _seconds: None, now_ms=lambda: T_OPEN,
                proposal_id=PROPOSAL_ID)
        self.assertEqual(code, bp.EXIT_CODE["UNKNOWN"], output.getvalue())
        unknown_client = next(iter(self.position()["client_ids"]))

        next_id = "next123"
        next_proposal = proposal_payload(next_id)
        next_proposal["created_utc"] = "2025-08-12T00:30:00Z"
        self.write_proposal(next_proposal, next_id)

        for _tick in range(2):
            unresolved = happy_spec()
            unresolved["legs"]["enter_spot"] = {
                "query": [lambda _params: ("unclear", {})]}
            transport = ScriptedTransport(unresolved)
            code, tick_output = self.run_tick(transport)
            self.assertEqual(code, bs.EXIT_FROZEN, tick_output)
            self.assertEqual(transport.posts, [])
            self.assertIn("reconciliation is the only action", tick_output)
            self.assertNotIn("SUPERVISOR | CHECK", tick_output)
            self.assertNotIn("SUPERVISOR | CLAIM", tick_output)
            self.assertFalse(os.path.exists(self.claim_path(next_id)))

        resolved_base = ScriptedTransport(happy_spec())

        def resolving_transport(method, base_url, path, params=None,
                                signed=False):
            if method == "GET" and path.endswith("/order") \
                    and (params or {}).get("origClientOrderId") == unknown_client:
                resolved_base.calls.append(
                    (method, base_url, path, dict(params or {}), signed))
                return MISSING
            return resolved_base(method, base_url, path, params, signed)

        code, tick_output = self.run_tick(resolving_transport)
        self.assertEqual(code, bs.EXIT_ACTED, tick_output)
        self.assertTrue(os.path.exists(self.claim_path(next_id)))
        self.assertEqual(self.position()["state"], bp.ST_OPEN)
        self.assertEqual(self.position()["proposal_id"], next_id)

    def test_two_supervisors_race_exactly_one_claims_and_opens(self):
        barrier = threading.Barrier(2)
        real_claim = bs._claim

        def synchronized_claim(*args, **kwargs):
            barrier.wait(timeout=5)
            return real_claim(*args, **kwargs)

        configs = [dataclasses.replace(self.cfg, run_id="racea"),
                   dataclasses.replace(self.cfg, run_id="raceb")]
        transports = [ScriptedTransport(happy_spec()),
                      ScriptedTransport(happy_spec())]
        results = []
        failures = []

        def worker(index):
            try:
                results.append(bs.tick(
                    configs[index], transports[index],
                    sleep=lambda _seconds: None, now_ms=lambda: T_OPEN,
                    claimant="racer-%s" % index))
            except BaseException as error:  # relay a thread failure to unittest
                failures.append(error)

        output = io.StringIO()
        with mock.patch.object(bs, "_claim", side_effect=synchronized_claim), \
                mock.patch.object(bs, "_entry_ticket_refusal",
                                  return_value=""), \
                mock.patch.object(bs, "_consume_accept_one",
                                  return_value=True), \
                mock.patch.object(bs, "_acquire_governor",
                                  side_effect=self.acquire_governor):
            with redirect_stdout(output):
                threads = [threading.Thread(target=worker, args=(index,))
                           for index in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

        self.assertFalse(failures, failures)
        self.assertFalse([thread for thread in threads if thread.is_alive()])
        self.assertEqual(sorted(results),
                         [bs.EXIT_ACTED, bs.EXIT_IDLE], output.getvalue())
        self.assertEqual(sum(len(transport.posts)
                             for transport in transports), 2)
        self.assertEqual(self.position()["state"], bp.ST_OPEN)
        with open(self.claim_path(), encoding="utf-8") as handle:
            self.assertIn(json.load(handle)["claimant"],
                          ("racer-0", "racer-1"))


if __name__ == "__main__":
    unittest.main()
