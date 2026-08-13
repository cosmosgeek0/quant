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
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_position as bp  # noqa: E402
import basis_supervisor as bs  # noqa: E402
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


_real_getaddrinfo = socket.getaddrinfo


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
            symbol=SYMBOL, sha=SHA, receipt_dir=self.dir,
            max_hold_ms=86400000,
            min_remaining_edge_pct=Decimal("0.01"),
            max_adverse_move_pct=Decimal("0.5"))
        self.write_proposal()

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

    def run_tick(self, transport, claimant="test-supervisor"):
        output = io.StringIO()
        with redirect_stdout(output):
            code = bs.tick(
                self.cfg, transport, sleep=lambda _seconds: None,
                now_ms=lambda: T_OPEN, claimant=claimant)
        return code, output.getvalue()

    def open_directly(self, spec=None):
        output = io.StringIO()
        transport = ScriptedTransport(spec or happy_spec())
        with redirect_stdout(output):
            code = bp.cmd_open(
                self.cfg, transport, sleep=lambda _seconds: None,
                now_ms=lambda: T_OPEN, proposal_id=PROPOSAL_ID)
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

        code, output = self.run_tick(transport)

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
                self.cfg, ScriptedTransport(unknown_spec),
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
        with mock.patch.object(bs, "_claim", side_effect=synchronized_claim):
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
