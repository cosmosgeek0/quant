"""Deterministic offline tests for src/basis_scan.py core math — no network."""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import basis_scan  # noqa: E402
from basis_scan import build_rows, cap_feasible, spot_universe, usdm_universe  # noqa: E402


def meta(step="0.001", min_notional="5"):
    return {"step": Decimal(step), "min_notional": Decimal(min_notional)}


def books(bid, ask):
    return {"bid": Decimal(bid), "ask": Decimal(ask)}


class UniverseTests(unittest.TestCase):
    def test_spot_universe_filters_status_quote_and_permission(self):
        info = {"symbols": [
            {"symbol": "AUSDT", "status": "TRADING", "quoteAsset": "USDT",
             "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.01"},
                         {"filterType": "NOTIONAL", "minNotional": "5"}]},
            {"symbol": "BUSDT", "status": "BREAK", "quoteAsset": "USDT",
             "filters": []},
            {"symbol": "CBTC", "status": "TRADING", "quoteAsset": "BTC",
             "filters": []},
        ]}
        self.assertEqual(list(spot_universe(info)), ["AUSDT"])

    def test_usdm_universe_requires_perpetual(self):
        info = {"symbols": [
            {"symbol": "AUSDT", "status": "TRADING", "quoteAsset": "USDT",
             "contractType": "PERPETUAL",
             "filters": [{"filterType": "LOT_SIZE", "stepSize": "1"},
                         {"filterType": "MIN_NOTIONAL", "notional": "5"}]},
            {"symbol": "AUSDT_240101", "status": "TRADING",
             "quoteAsset": "USDT", "contractType": "CURRENT_QUARTER",
             "filters": []},
        ]}
        self.assertEqual(list(usdm_universe(info)), ["AUSDT"])


class RowTests(unittest.TestCase):
    def test_gap_math_and_ranking(self):
        rows = build_rows(
            {"AUSDT": meta(), "BUSDT": meta()},
            {"AUSDT": meta(step="1"), "BUSDT": meta(step="1")},
            {"AUSDT": books("99.9", "100.0"), "BUSDT": books("99.9", "100.0")},
            {"AUSDT": books("100.2", "100.3"), "BUSDT": books("99.0", "99.1")},
            {"AUSDT": Decimal("0.0001")},
        )
        self.assertEqual([row["symbol"] for row in rows], ["AUSDT", "BUSDT"])
        self.assertEqual(rows[0]["entry_gap_bps"], Decimal("20.0000"))
        self.assertEqual(rows[0]["exit_gap_bps"], Decimal("40.0400"))
        self.assertEqual(rows[0]["funding_bps"], Decimal("1.0000"))
        self.assertTrue(rows[1]["entry_gap_bps"] < 0)

    def test_symbols_missing_a_book_are_skipped(self):
        rows = build_rows({"AUSDT": meta()}, {"AUSDT": meta()},
                          {}, {"AUSDT": books("1", "1")}, {})
        self.assertEqual(rows, [])

    def test_min_valid_notional_is_the_stricter_venue(self):
        rows = build_rows(
            {"AUSDT": meta(min_notional="5")},
            {"AUSDT": meta(step="1", min_notional="20")},
            {"AUSDT": books("5.99", "6.00")},
            {"AUSDT": books("6.02", "6.03")}, {})
        self.assertEqual(rows[0]["min_valid_notional"], Decimal("20"))
        self.assertFalse(rows[0]["fits_10_cap"])

    def test_cap_feasibility_uses_the_coarser_step(self):
        # price ~6: one whole contract (futures step 1) costs ~6 USDT — fits.
        self.assertTrue(cap_feasible(Decimal("6.00"), Decimal("6.02"),
                                     meta(step="0.01"), meta(step="1")))
        # price ~15: one whole contract exceeds the 10 USDT cap — no quantity.
        self.assertFalse(cap_feasible(Decimal("15.0"), Decimal("15.1"),
                                      meta(step="0.01"), meta(step="1")))

    def test_module_main_is_the_only_fetch_site(self):
        source = Path(basis_scan.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("fetch_json("), 6)  # 1 def + 5 in main


if __name__ == "__main__":
    unittest.main()
