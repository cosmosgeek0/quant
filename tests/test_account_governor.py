"""Offline tests for the shared durable account entry governor."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import account_governor as governor  # noqa: E402


DAY = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


class AccountGovernorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.receipt_dir = temporary.name
        self.root = Path(self.receipt_dir)

    def lock_path(self):
        return self.root / governor.ENTRY_LOCK_FILE

    def ledger_rows(self, day=DAY):
        path = self.root / (
            governor.DAILY_LEDGER_PREFIX
            + day.date().isoformat()
            + governor.DAILY_LEDGER_SUFFIX
        )
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_two_contending_acquires_have_exactly_one_winner(self):
        def contend(op):
            try:
                token = governor.acquire_entry_slot(
                    op, "BTCUSDT", self.receipt_dir
                )
                return "won", token
            except governor.GovernorRefusal as error:
                return "refused", error.reason

        with mock.patch.object(governor, "_utc_now", return_value=DAY):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(contend, ("BASIS", "MOMENTUM")))

        winners = [value for status, value in results if status == "won"]
        refusals = [value for status, value in results if status == "refused"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(refusals), 1)
        self.assertTrue(refusals[0])
        owner = json.loads(self.lock_path().read_text())
        self.assertEqual(owner["utc"], "2026-08-14T12:00:00Z")
        self.assertIn(owner["op"], {"BASIS", "MOMENTUM"})
        self.assertEqual(owner["symbol"], "BTCUSDT")
        self.assertEqual(len(self.ledger_rows()), 1)
        governor.release_entry_slot(winners[0], self.receipt_dir)

    def test_entry_count_limit_rolls_on_the_next_utc_day(self):
        with mock.patch.object(governor, "_utc_now", return_value=DAY):
            for index in range(governor.DEFAULT_MAX_ENTRIES_PER_UTC_DAY):
                token = governor.acquire_entry_slot(
                    f"ENTRY_{index}", "ETHUSDT", self.receipt_dir
                )
                governor.release_entry_slot(token, self.receipt_dir)
            allowed, reason = governor.budgets_allow(self.receipt_dir)
            self.assertFalse(allowed)
            cap = governor.DEFAULT_MAX_ENTRIES_PER_UTC_DAY
            self.assertIn("%d of %d" % (cap, cap), reason)
            with self.assertRaises(governor.GovernorRefusal) as refused:
                governor.acquire_entry_slot("ENTRY_6", "ETHUSDT",
                                            self.receipt_dir)
            self.assertIn("daily entry limit reached", refused.exception.reason)

        tomorrow = DAY + timedelta(days=1)
        with mock.patch.object(governor, "_utc_now", return_value=tomorrow):
            allowed, reason = governor.budgets_allow(self.receipt_dir)
            self.assertTrue(allowed, reason)
            token = governor.acquire_entry_slot(
                "NEXT_DAY", "ETHUSDT", self.receipt_dir
            )
            governor.release_entry_slot(token, self.receipt_dir)
        self.assertEqual(
            len(self.ledger_rows(DAY)), governor.DEFAULT_MAX_ENTRIES_PER_UTC_DAY
        )
        self.assertEqual(len(self.ledger_rows(tomorrow)), 1)

    def test_realized_loss_plus_fees_trips_and_stays_tripped_for_day(self):
        with mock.patch.object(governor, "_utc_now", return_value=DAY):
            loss_cap = governor.DEFAULT_DAILY_LOSS_PLUS_FEES_USDT
            governor.record_realized(
                -(loss_cap - Decimal("0.40")), Decimal("0.20"), self.receipt_dir
            )
            allowed, reason = governor.budgets_allow(self.receipt_dir)
            self.assertTrue(allowed, reason)
            governor.record_realized(
                Decimal("-0.21"), Decimal("0"), self.receipt_dir
            )
            allowed, reason = governor.budgets_allow(self.receipt_dir)
            self.assertFalse(allowed)
            self.assertIn(
                "%s USDT" % governor.DEFAULT_DAILY_LOSS_PLUS_FEES_USDT, reason
            )
            governor.record_realized(
                Decimal("10"), Decimal("0"), self.receipt_dir
            )
            self.assertFalse(governor.budgets_allow(self.receipt_dir)[0])
            with self.assertRaises(governor.GovernorRefusal) as refused:
                governor.acquire_entry_slot("BASIS", "SOLUSDT",
                                            self.receipt_dir)
            self.assertIn("loss-plus-fees", refused.exception.reason)

        with mock.patch.object(
            governor, "_utc_now", return_value=DAY + timedelta(days=1)
        ):
            self.assertTrue(governor.budgets_allow(self.receipt_dir)[0])

    def test_stale_lock_surfaces_without_being_broken(self):
        old = DAY - timedelta(hours=2, seconds=1)
        with mock.patch.object(governor, "_utc_now", return_value=old):
            token = governor.acquire_entry_slot(
                "MOMENTUM", "SOLUSDT", self.receipt_dir
            )

        with mock.patch.object(governor, "_utc_now", return_value=DAY):
            allowed, reason = governor.budgets_allow(self.receipt_dir)
            self.assertFalse(allowed)
            self.assertIn("stale account entry lock", reason)
            self.assertIn("human or reconcile", reason)
            self.assertTrue(self.lock_path().exists())

            (self.root / "momentum-position.json").write_text(
                json.dumps({"state": "OPEN"}), encoding="utf-8"
            )
            allowed, reason = governor.budgets_allow(self.receipt_dir)
            self.assertTrue(allowed, reason)
            self.assertTrue(self.lock_path().exists())
        governor.release_entry_slot(token, self.receipt_dir)

    def test_release_is_idempotent_and_wrong_token_never_unlocks(self):
        with mock.patch.object(governor, "_utc_now", return_value=DAY):
            token = governor.acquire_entry_slot(
                "BASIS", "BTCUSDT", self.receipt_dir
            )
        with self.assertRaises(governor.GovernorRefusal):
            governor.release_entry_slot("not-the-owner", self.receipt_dir)
        self.assertTrue(self.lock_path().exists())
        governor.release_entry_slot(token, self.receipt_dir)
        governor.release_entry_slot(token, self.receipt_dir)
        self.assertFalse(self.lock_path().exists())


if __name__ == "__main__":
    unittest.main()
