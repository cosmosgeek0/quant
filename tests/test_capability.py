#!/usr/bin/env python3
"""Offline regression tests for the canTrade capability truth source.

The prior preflight read canTrade from GET /fapi/v3/account and produced a
false negative (recorded 2026-08-12: signed read returned canTrade=false
while Futures was enabled). These tests pin the fix: capability truth comes
from GET /fapi/v1/accountConfig alone; /fapi/v3/account stays the source
for balances and positions only. Everything runs against the injected
transport from test_futures_short — no network is possible.
"""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from tests.test_futures_short import (  # noqa: E402
    BOOK,
    BPS10,
    CLOSE_FILL,
    FakeTransport,
    MARK,
    OPEN_FILL_UNDER_CAP,
    OfflineCase,
    ShortExecuteTransport,
    TEN,
    fh,
    symbol_info,
)

PREFLIGHT_READS = {
    "/fapi/v1/time": ("ok", {"serverTime": 1755000000000}),
    "/fapi/v1/exchangeInfo": ("ok", {"symbols": [symbol_info()]}),
    "/fapi/v1/ticker/bookTicker": ("ok", dict(BOOK)),
    "/fapi/v1/premiumIndex": ("ok", dict(MARK)),
    "/fapi/v1/accountConfig": ("ok", {"canTrade": True,
                                      "dualSidePosition": False}),
    # Balances/positions truth only; canTrade is deliberately ABSENT here so
    # any regression back to v3-account capability sourcing blocks loudly.
    "/fapi/v3/account": ("ok", {"availableBalance": "100", "positions": []}),
    "/fapi/v1/positionSide/dual": ("ok", {"dualSidePosition": False}),
    "/fapi/v1/openOrders": ("ok", []),
    "/fapi/v3/positionRisk": ("ok", []),
    "/fapi/v1/symbolConfig": ("ok", [{"symbol": "BTCUSDT", "leverage": "1"}]),
}


def make_transport(overrides=None):
    reads = dict(PREFLIGHT_READS)
    reads.update(overrides or {})
    return FakeTransport(extra=reads)


class PreflightCase(OfflineCase):
    """Runs fh.preflight against an injected transport with credentials set."""

    def run_preflight(self, overrides=None):
        transport = make_transport(overrides)
        out = io.StringIO()
        with mock.patch.object(fh, "API_KEY", "offline-test-key"), \
                mock.patch.object(fh, "API_SECRET", "offline-test-secret"), \
                mock.patch.object(fh, "http", transport), \
                contextlib.redirect_stdout(out):
            plan = fh.preflight(TEN, BPS10, "SELL")
        return transport, plan

    def run_preflight_expecting(self, exception, overrides=None):
        transport = make_transport(overrides)
        out = io.StringIO()
        with mock.patch.object(fh, "API_KEY", "offline-test-key"), \
                mock.patch.object(fh, "API_SECRET", "offline-test-secret"), \
                mock.patch.object(fh, "http", transport), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(exception) as caught:
                fh.preflight(TEN, BPS10, "SELL")
        return transport, caught.exception

    @staticmethod
    def paths(transport):
        return [call[1] for call in transport.calls]


class TestCapabilityFromAccountConfig(PreflightCase):
    """(a) accountConfig canTrade=true is the only capability pass gate."""

    def test_passes_when_v3_account_lacks_can_trade(self):
        transport, plan = self.run_preflight()
        self.assertIs(plan["account_can_trade"], True)
        self.assertEqual(plan["position_mode"], "ONE_WAY")
        paths = self.paths(transport)
        self.assertIn("/fapi/v1/accountConfig", paths)
        self.assertIn("/fapi/v3/account", paths)
        self.assertLess(paths.index("/fapi/v1/accountConfig"),
                        paths.index("/fapi/v3/account"))
        for method, path, params, signed in transport.calls:
            if path == "/fapi/v1/accountConfig":
                self.assertEqual(method, "GET")
                self.assertEqual(params, {})
                self.assertTrue(signed)
        self.assertEqual(transport.posts(), [])

    def test_passes_when_v3_account_contradicts_with_false(self):
        transport, plan = self.run_preflight({
            "/fapi/v3/account": ("ok", {"canTrade": False,
                                        "availableBalance": "100",
                                        "positions": []}),
        })
        self.assertIs(plan["account_can_trade"], True)
        self.assertEqual(transport.posts(), [])


class TestCapabilityHonestBlock(PreflightCase):
    """(b) accountConfig canTrade=false or unprovable blocks honestly."""

    def test_blocks_on_false_even_when_v3_account_says_true(self):
        transport, error = self.run_preflight_expecting(fh.CapabilityBlocked, {
            "/fapi/v1/accountConfig": ("ok", {"canTrade": False}),
            "/fapi/v3/account": ("ok", {"canTrade": True,
                                        "availableBalance": "100",
                                        "positions": []}),
        })
        self.assertIn("accountConfig canTrade", str(error))
        self.assertEqual(transport.posts(), [])
        # The gate refuses before any v3-account read is consulted.
        self.assertNotIn("/fapi/v3/account", self.paths(transport))

    def test_blocks_when_account_config_lacks_can_trade(self):
        transport, error = self.run_preflight_expecting(fh.CapabilityBlocked, {
            "/fapi/v1/accountConfig": ("ok", {"feeTier": 0}),
        })
        self.assertIn("accountConfig canTrade", str(error))
        self.assertEqual(transport.posts(), [])

    def test_blocks_when_account_config_read_is_rejected(self):
        transport, error = self.run_preflight_expecting(fh.CapabilityBlocked, {
            "/fapi/v1/accountConfig": ("api_error", {
                "code": -2015,
                "msg": "Invalid API-key, IP, or permissions for action.",
            }),
        })
        self.assertIn("account-configuration read unavailable", str(error))
        self.assertEqual(transport.posts(), [])


class TestBalancesPositionsStayOnV3Account(PreflightCase):
    """(c) balances and positions still come from /fapi/v3/account."""

    def test_low_v3_available_balance_still_blocks(self):
        transport, error = self.run_preflight_expecting(fh.PreflightError, {
            "/fapi/v3/account": ("ok", {"availableBalance": "5",
                                        "positions": []}),
        })
        self.assertIn("available Futures balance", str(error))
        self.assertEqual(transport.posts(), [])

    def test_v3_account_position_rows_still_block(self):
        transport, error = self.run_preflight_expecting(fh.PreflightError, {
            "/fapi/v3/account": ("ok", {
                "availableBalance": "100",
                "positions": [{"symbol": "BTCUSDT", "positionSide": "BOTH",
                               "positionAmt": "0.5"}],
            }),
        })
        self.assertIn("existing position", str(error))
        self.assertEqual(transport.posts(), [])


class TestMainHonestBlockRouting(OfflineCase):
    """(b) at main() level: false capability never reaches an order POST."""

    def test_execute_refuses_before_any_order_post(self):
        transport = ShortExecuteTransport(OPEN_FILL_UNDER_CAP, CLOSE_FILL)
        transport.reads["/fapi/v1/accountConfig"] = ("ok", {"canTrade": False})
        out = io.StringIO()
        argv = ["futures_hand.py", "--short", "--execute"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(fh, "API_KEY", "offline-test-key"), \
                mock.patch.object(fh, "API_SECRET", "offline-test-secret"), \
                mock.patch.object(fh, "http", transport), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                fh.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(fh.receipt["verdict"], "REJECTED_BEFORE_OPEN")
        self.assertIn("accountConfig canTrade", fh.receipt["verdict_note"])
        self.assertEqual(transport.order_posts(), [])
        self.assertFalse(os.path.exists(fh.LOCK_PATH))

    def test_validate_only_reports_blocked_without_v3_account_read(self):
        transport = make_transport({
            "/fapi/v1/accountConfig": ("ok", {"canTrade": False}),
        })
        out = io.StringIO()
        argv = ["futures_hand.py", "--short", "--validate-only"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(fh, "API_KEY", "offline-test-key"), \
                mock.patch.object(fh, "API_SECRET", "offline-test-secret"), \
                mock.patch.object(fh, "http", transport), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                fh.main()
        self.assertEqual(caught.exception.code, 3)
        text = out.getvalue()
        self.assertIn("VERDICT: BLOCKED", text)
        self.assertIn("accountConfig canTrade", text)
        self.assertEqual(transport.posts(), [])
        self.assertNotIn("/fapi/v3/account",
                         [call[1] for call in transport.calls])


class TestParseTradeCapability(unittest.TestCase):
    """Pure parser truth table for the accountConfig payload."""

    def test_true_values_pass(self):
        self.assertIs(fh.parse_trade_capability({"canTrade": True}), True)
        self.assertIs(fh.parse_trade_capability({"canTrade": "true"}), True)
        self.assertIs(fh.parse_trade_capability({"canTrade": "True"}), True)

    def test_non_true_values_block(self):
        for value in (False, "false", "", None, 0, 1, "yes"):
            with self.assertRaisesRegex(fh.CapabilityBlocked,
                                        "accountConfig canTrade"):
                fh.parse_trade_capability({"canTrade": value})
        with self.assertRaisesRegex(fh.CapabilityBlocked,
                                    "accountConfig canTrade"):
            fh.parse_trade_capability({})

    def test_non_object_payload_is_a_preflight_error(self):
        for payload in ([], [{"canTrade": True}], "canTrade", None):
            with self.assertRaisesRegex(fh.PreflightError, "not an object"):
                fh.parse_trade_capability(payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
