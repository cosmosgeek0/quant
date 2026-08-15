"""Deterministic offline tests for the reverse-basis lifecycle skeleton."""

import json
import sys
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import reverse_basis_lifecycle as lifecycle  # noqa: E402


ASSET = "BTC"
SYMBOL = "BTCUSDT"


def preflight():
    return {"event": "PREFLIGHT_PASSED", "ticket_present": True,
            "stop_present": False, "asset": ASSET, "principal": "2"}


def ledger(status, principal="2", interest="0"):
    return {"asset": ASSET, "principal": principal, "interest": interest,
            "status": status}


def liability():
    return {"verdict": "LIABILITY_OPEN", "asset": ASSET, "borrowed": "2",
            "interest": "0.1", "total_liability": "2.1", "dust": False,
            "evidence": ["account.borrowed", "account.interest", "ledger.borrow"]}


def partial():
    return {"verdict": "PARTIAL_UNWIND", "asset": ASSET,
            "remaining_qty": "0.5", "evidence_order_ids": ["entry", "cover"],
            "evidence": ["orders.entry", "orders.cover"]}


def flat():
    return {"verdict": "RECONCILED_FLAT", "asset": ASSET,
            "evidence_order_ids": ["entry", "hedge", "cover", "close"],
            "evidence": ["account.flat", "ledger.repaid", "orders.terminal"]}


def unknown():
    return {"verdict": "UNKNOWN_FREEZE", "reason": "torn private read",
            "missing_evidence": ["account.interest"],
            "evidence": ["account.borrowed"]}


def fill(order_id, side, quantity, status="FILLED", **extra):
    row = {"id": order_id, "side": side, "symbol": SYMBOL,
           "executedQty": quantity, "cummulativeQuoteQty": "100",
           "status": status, "commission": "0", "commissionAsset": "USDT"}
    row.update(extra)
    return row


def advance_checked(state, event, **kwargs):
    new, intents, row = lifecycle.advance(state, event, **kwargs)
    if not row["evidence"]:
        raise AssertionError("transition row omitted evidence")
    return new, intents, row


def open_path():
    state = lifecycle.LifecycleState()
    visited = {state.stage}
    trail = [state]

    state, intents, _ = advance_checked(state, preflight(), borrow_enabled=True)
    assert intents[0]["kind"] == "BORROW"
    visited.add(state.stage)
    trail.append(state)

    state, intents, _ = advance_checked(state, ledger("BORROWED"))
    assert not intents
    visited.add(state.stage)
    trail.append(state)

    state, intents, _ = advance_checked(state, liability())
    assert intents[0]["kind"] == "MARGIN_SELL"
    visited.add(state.stage)
    trail.append(state)

    state, intents, _ = advance_checked(
        state, fill("entry", "SELL", "1", status="PARTIALLY_FILLED"))
    assert intents[0]["kind"] == "FUTURES_HEDGE"
    visited.add(state.stage)
    trail.append(state)

    state, _, _ = advance_checked(state, fill("hedge-1", "BUY", "1"))
    visited.add(state.stage)
    trail.append(state)

    state, intents, _ = advance_checked(state, fill("entry", "SELL", "2"))
    assert intents[0]["quantity"] == "1"
    visited.add(state.stage)
    trail.append(state)

    state, _, _ = advance_checked(state, fill("hedge-2", "BUY", "1"))
    visited.add(state.stage)
    trail.append(state)
    assert state.stage == lifecycle.OPEN
    return state, visited, trail


def close_path(state):
    visited = {state.stage}
    state, intents, _ = advance_checked(state, {"event": "EXIT_REQUESTED"})
    assert intents[0]["kind"] == "MARGIN_COVER"
    state, intents, _ = advance_checked(state, fill("cover", "BUY", "2"))
    assert intents[0]["kind"] == "FUTURES_CLOSE"
    visited.add(state.stage)
    state, intents, _ = advance_checked(
        state, fill("close", "SELL", "2", reduceOnly=True))
    assert intents[0]["kind"] == "REPAY"
    visited.add(state.stage)
    state, _, _ = advance_checked(state, ledger("REPAID", "2", "0.1"))
    visited.add(state.stage)
    state, _, _ = advance_checked(state, flat())
    visited.add(state.stage)
    return state, visited


class ReverseBasisLifecycleTests(unittest.TestCase):
    def test_exact_state_vocabulary_and_every_state_reachable(self):
        expected = (
            "BLOCKED", "PREFLIGHTED", "BORROWING", "LIABILITY_CONFIRMED",
            "ENTRY_MARGIN_SELL_WORKING", "HEDGE_WORKING", "OPEN", "EXITING",
            "REPAYING", "CLOSED", "RECOVERY_REQUIRED", "RECOVERING",
            "RECOVERED", "MANUAL_INTERVENTION",
        )
        opened, visited, _ = open_path()
        closed, close_states = close_path(opened)
        self.assertEqual(closed.stage, lifecycle.CLOSED)
        visited.update(close_states)

        recovery, _, _ = advance_checked(opened, partial())
        visited.add(recovery.stage)
        recovery, intents, _ = advance_checked(
            recovery, {"event": "RECOVERY_APPROVED", "risk_reducing": True,
                       "quantity": "0.5"})
        self.assertEqual(intents[0]["kind"], "RECOVERY_REDUCE")
        recovering, _, _ = advance_checked(
            recovery, fill("recovery", "BUY", "0.5", risk_reducing=True))
        visited.add(recovering.stage)
        recovered, _, _ = advance_checked(recovering, flat())
        visited.add(recovered.stage)

        manual, _, _ = advance_checked(
            advance_checked(opened, partial())[0], {"event": "MANUAL_REQUIRED"})
        visited.add(manual.stage)
        self.assertEqual(lifecycle.STATE_NAMES, expected)
        self.assertEqual(visited, set(expected))

    def test_intent_only_and_non_fill_records_never_advance(self):
        state = lifecycle.LifecycleState()
        same, intents, row = advance_checked(
            state, {"event": "ORDER_INTENT", "intent": {"side": "SELL"}})
        self.assertEqual(same, state)
        self.assertEqual(intents, [])
        self.assertEqual(row["reason"], "INTENT_ONLY_EVIDENCE")

        same, intents, row = advance_checked(
            state, {"id": "working", "side": "SELL", "status": "NEW",
                    "executedQty": "0"})
        self.assertEqual(same, state)
        self.assertEqual(intents, [])
        self.assertEqual(row["reason"], "INTENT_ONLY_EVIDENCE")

    def test_unknown_freezes_and_only_fresh_reconciliation_releases(self):
        opened, _, _ = open_path()
        frozen, _, row = advance_checked(opened, unknown())
        self.assertEqual(frozen.stage, lifecycle.MANUAL_INTERVENTION)
        self.assertEqual(row["verdict"], "FROZEN")

        still, _, row = advance_checked(frozen, fill("cover", "BUY", "1"))
        self.assertEqual(still, frozen)
        self.assertEqual(row["reason"], "FROZEN_AWAITING_FRESH_RECONCILIATION")

        stale, _, _ = advance_checked(
            frozen, {"event": "MARGIN_RECONCILE", "rows": [liability()],
                     "fresh": False})
        self.assertEqual(stale, frozen)
        resumed, _, _ = advance_checked(
            frozen, {"event": "MARGIN_RECONCILE", "rows": [liability()],
                     "fresh": True})
        self.assertEqual(resumed.stage, lifecycle.RECOVERY_REQUIRED)

    def test_borrow_capability_defaults_false(self):
        state = lifecycle.LifecycleState()
        same, intents, row = advance_checked(state, preflight())
        self.assertEqual(same, state)
        self.assertEqual(intents, [])
        self.assertEqual(row["reason"], "BORROW_CAPABILITY_DISABLED")

        progressed, intents, _ = advance_checked(
            state, preflight(), borrow_enabled=True)
        self.assertEqual(progressed.stage, lifecycle.PREFLIGHTED)
        self.assertEqual(intents[0]["kind"], "BORROW")

    def test_stop_seals_entries_but_does_not_block_exit(self):
        stopped, _, _ = advance_checked(
            lifecycle.LifecycleState(), {"event": "STOP", "present": True})
        refused, intents, row = advance_checked(
            stopped, preflight(), borrow_enabled=True)
        self.assertEqual(refused.stage, lifecycle.BLOCKED)
        self.assertEqual(intents, [])
        self.assertEqual(row["reason"], "STOP_SEALS_ENTRY")

        opened, _, _ = open_path()
        stopped_open, _, _ = advance_checked(
            opened, {"event": "STOP", "present": True})
        exiting, intents, row = advance_checked(
            stopped_open, {"event": "EXIT_REQUESTED"})
        self.assertEqual(exiting.stage, lifecycle.OPEN)
        self.assertEqual(intents[0]["kind"], "MARGIN_COVER")
        self.assertEqual(row["reason"], "EXIT_ALLOWED_REGARDLESS_OF_STOP")
        exiting, intents, _ = advance_checked(
            exiting, fill("cover-stop", "BUY", "2"))
        self.assertEqual(exiting.stage, lifecycle.EXITING)
        self.assertEqual(intents[0]["kind"], "FUTURES_CLOSE")

    def test_persistence_is_json_safe_and_exact_round_trip(self):
        opened, _, _ = open_path()
        encoded = json.dumps(opened.to_row(), sort_keys=True)
        restored = lifecycle.from_row(json.loads(encoded))
        self.assertEqual(restored, opened)
        self.assertTrue(all(isinstance(fact[2], str) for fact in restored.fills))

    def test_resume_mismatch_and_unknown_freeze(self):
        state = lifecycle.LifecycleState(
            stage=lifecycle.BORROWING, transition_count=2, asset=ASSET,
            requested_principal="2", borrow_capability=True)
        reconciled, _, _ = advance_checked(state, liability())
        matched = lifecycle.derive_resume([reconciled.to_row()], [liability()])
        self.assertEqual(matched, reconciled)

        mismatched = lifecycle.derive_resume([reconciled.to_row()], [flat()])
        self.assertEqual(mismatched.stage, lifecycle.MANUAL_INTERVENTION)
        self.assertEqual(mismatched.frozen_from, lifecycle.LIABILITY_CONFIRMED)
        unknown_resume = lifecycle.derive_resume(
            [reconciled.to_row()], [unknown()])
        self.assertEqual(unknown_resume.stage, lifecycle.MANUAL_INTERVENTION)

    def test_deterministic_intent_ids_stable_across_replay(self):
        state = lifecycle.LifecycleState()
        first = advance_checked(state, preflight(), borrow_enabled=True)[1][0]
        replay = advance_checked(state, preflight(), borrow_enabled=True)[1][0]
        self.assertEqual(first["intent_id"], replay["intent_id"])
        self.assertEqual(first["execution_authority"], "EMIT_ONLY")

        opened, _, _ = open_path()
        first_exit = advance_checked(opened, {"event": "EXIT_REQUESTED"})[1][0]
        replay_exit = advance_checked(opened, {"event": "EXIT_REQUESTED"})[1][0]
        self.assertEqual(first_exit["intent_id"], replay_exit["intent_id"])

    def test_repay_requires_exact_principal_plus_interest(self):
        opened, _, _ = open_path()
        state, _ = close_path_until_repay(opened)
        recovery, _, row = advance_checked(state, ledger("REPAID", "2", "0.09"))
        self.assertEqual(recovery.stage, lifecycle.RECOVERY_REQUIRED)
        self.assertEqual(row["reason"], "REPAY_AMOUNT_MISMATCH_REQUIRES_RECOVERY")


def close_path_until_repay(state):
    state, _, _ = advance_checked(state, {"event": "EXIT_REQUESTED"})
    state, _, _ = advance_checked(state, fill("cover-exact", "BUY", "2"))
    state, intents, _ = advance_checked(
        state, fill("close-exact", "SELL", "2", reduceOnly=True))
    return state, intents


if __name__ == "__main__":
    unittest.main()
