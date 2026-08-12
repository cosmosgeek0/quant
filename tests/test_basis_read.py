#!/usr/bin/env python3
"""Deterministic, no-network tests for src/basis_read.py.

Every payload below is an injected fixture shaped like the real public
responses; nothing here opens a socket.  The suite proves the executable
gap math (entry gap = USD-M bid minus Spot ask; exit gap = USD-M ask minus
Spot bid), the named bps bases, and that prices and derived values stay
Decimal end to end.
"""

import contextlib
import io
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
)
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import basis_read


def spot_book_payload(**overrides):
    payload = {
        "symbol": "BTCUSDT",
        "bidPrice": "100.00",
        "bidQty": "3.18397000",
        "askPrice": "100.10",
        "askQty": "3.17394000",
    }
    payload.update(overrides)
    return payload


def usdm_book_payload(**overrides):
    payload = {
        "symbol": "BTCUSDT",
        "bidPrice": "100.30",
        "bidQty": "3.288",
        "askPrice": "100.40",
        "askQty": "10.794",
        "time": 1700000000000,
        "lastUpdateId": 11274182597048,
    }
    payload.update(overrides)
    return payload


def usdm_premium_payload(**overrides):
    payload = {
        "symbol": "BTCUSDT",
        "markPrice": "100.35000000",
        "indexPrice": "100.32000000",
        "estimatedSettlePrice": "100.33000000",
        "lastFundingRate": "0.00010000",
        "interestRate": "0.00010000",
        "nextFundingTime": 1700006400000,
        "time": 1700000000000,
    }
    payload.update(overrides)
    return payload


def build_row(spot=None, book=None, premium=None, symbol="BTCUSDT"):
    return basis_read.build_basis_read_row(
        symbol,
        spot if spot is not None else spot_book_payload(),
        book if book is not None else usdm_book_payload(),
        premium if premium is not None else usdm_premium_payload(),
        "2026-08-13T00:00:00.000Z",
    )


class GapMathTest(unittest.TestCase):
    def test_entry_and_exit_gaps_are_exact_decimal(self):
        row = build_row()
        # Float arithmetic gives 100.30 - 100.10 = 0.19999999999999857.
        self.assertEqual(basis_read.format_decimal(row.entry_gap_quote), "0.20")
        self.assertEqual(basis_read.format_decimal(row.exit_gap_quote), "0.40")
        self.assertEqual(
            basis_read.format_decimal(row.entry_gap_bps_vs_spot_ask), "19.98001998"
        )
        self.assertEqual(
            basis_read.format_decimal(row.exit_gap_bps_vs_spot_bid), "40.00000000"
        )

    def test_gap_directions_use_executable_sides(self):
        row = build_row(
            spot=spot_book_payload(bidPrice="100.00", askPrice="101.00"),
            book=usdm_book_payload(bidPrice="103.00", askPrice="104.00"),
        )
        self.assertEqual(row.entry_gap_quote, Decimal("103.00") - Decimal("101.00"))
        self.assertEqual(row.exit_gap_quote, Decimal("104.00") - Decimal("100.00"))

    def test_negative_gaps_are_reported_factually(self):
        row = build_row(
            book=usdm_book_payload(bidPrice="99.90", askPrice="99.95"),
        )
        self.assertEqual(basis_read.format_decimal(row.entry_gap_quote), "-0.20")
        self.assertEqual(basis_read.format_decimal(row.exit_gap_quote), "-0.05")
        self.assertEqual(
            basis_read.format_decimal(row.entry_gap_bps_vs_spot_ask), "-19.98001998"
        )

    def test_real_scale_strings_preserve_decimal_places(self):
        row = build_row(
            spot=spot_book_payload(
                bidPrice="63419.92000000", askPrice="63419.93000000"
            ),
            book=usdm_book_payload(bidPrice="63392.70", askPrice="63392.80"),
        )
        self.assertEqual(
            basis_read.format_decimal(row.entry_gap_quote), "-27.23000000"
        )
        self.assertEqual(
            basis_read.format_decimal(row.exit_gap_quote), "-27.12000000"
        )

    def test_all_numeric_fields_are_decimal_instances(self):
        row = build_row()
        for name in (
            "spot_bid",
            "spot_ask",
            "usdm_bid",
            "usdm_ask",
            "entry_gap_quote",
            "entry_gap_bps_vs_spot_ask",
            "exit_gap_quote",
            "exit_gap_bps_vs_spot_bid",
            "funding_rate",
        ):
            value = getattr(row, name)
            self.assertIsInstance(value, Decimal, name)
            self.assertNotIsInstance(value, float, name)

    def test_funding_fields_pass_through(self):
        row = build_row()
        self.assertEqual(basis_read.format_decimal(row.funding_rate), "0.00010000")
        self.assertEqual(row.next_funding_utc, "2023-11-15T00:00:00.000Z")
        self.assertEqual(row.usdm_book_time_utc, "2023-11-14T22:13:20.000Z")


class ValidationTest(unittest.TestCase):
    def test_symbol_mismatch_rejected_per_source(self):
        for kwargs in (
            {"spot": spot_book_payload(symbol="ETHUSDT")},
            {"book": usdm_book_payload(symbol="ETHUSDT")},
            {"premium": usdm_premium_payload(symbol="ETHUSDT")},
        ):
            with self.assertRaises(basis_read.DataReadError):
                build_row(**kwargs)

    def test_crossed_spot_book_rejected(self):
        with self.assertRaises(basis_read.DataReadError):
            build_row(spot=spot_book_payload(bidPrice="100.20", askPrice="100.10"))

    def test_crossed_usdm_book_rejected(self):
        with self.assertRaises(basis_read.DataReadError):
            build_row(book=usdm_book_payload(bidPrice="100.50", askPrice="100.40"))

    def test_non_positive_price_rejected(self):
        with self.assertRaises(basis_read.DataReadError):
            build_row(spot=spot_book_payload(bidPrice="0", askPrice="100.10"))

    def test_zero_timestamps_rejected(self):
        with self.assertRaises(basis_read.DataReadError):
            build_row(premium=usdm_premium_payload(nextFundingTime=0))
        with self.assertRaises(basis_read.DataReadError):
            build_row(book=usdm_book_payload(time=0))

    def test_decimal_field_rejects_bad_values(self):
        for bad in (None, True, 1.23, "not-a-number", "NaN", "Infinity"):
            with self.assertRaises(basis_read.DataReadError):
                basis_read.decimal_field({"bidPrice": bad}, "bidPrice", "Spot book")

    def test_missing_funding_rate_rejected(self):
        payload = usdm_premium_payload()
        del payload["lastFundingRate"]
        with self.assertRaises(basis_read.DataReadError):
            build_row(premium=payload)


class ParseJsonObjectTest(unittest.TestCase):
    def test_json_floats_become_decimal(self):
        payload = basis_read.parse_json_object(
            b'{"markPrice": 100.1}', "USD-M premium", "BTCUSDT"
        )
        self.assertIsInstance(payload["markPrice"], Decimal)
        self.assertNotIsInstance(payload["markPrice"], float)
        self.assertEqual(payload["markPrice"], Decimal("100.1"))

    def test_non_object_rejected(self):
        with self.assertRaises(basis_read.DataReadError):
            basis_read.parse_json_object(b"[1, 2]", "Spot book", "BTCUSDT")

    def test_invalid_json_rejected(self):
        with self.assertRaises(basis_read.DataReadError):
            basis_read.parse_json_object(b"not json", "Spot book", "BTCUSDT")

    def test_oversized_body_rejected(self):
        raw = b"x" * (basis_read.MAX_RESPONSE_BYTES + 1)
        with self.assertRaises(basis_read.DataReadError):
            basis_read.parse_json_object(raw, "Spot book", "BTCUSDT")


class MainOfflineTest(unittest.TestCase):
    def fake_fetch(self, calls):
        payloads = {
            "Spot book": spot_book_payload(),
            "USD-M book": usdm_book_payload(),
            "USD-M premium": usdm_premium_payload(),
        }

        def fetch(source, base_url, path, symbol, timeout):
            calls.append((source, base_url, path, symbol))
            return dict(payloads[source], symbol=symbol)

        return fetch

    def test_main_prints_expected_rows_without_network(self):
        calls = []
        stdout = io.StringIO()
        with mock.patch.object(
            basis_read, "fetch_public_json", side_effect=self.fake_fetch(calls)
        ), mock.patch.object(
            basis_read, "now_utc", return_value="2026-08-13T00:00:00.000Z"
        ), contextlib.redirect_stdout(stdout):
            exit_code = basis_read.main(["BTCUSDT"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [
                (
                    "Spot book",
                    "https://api.binance.com",
                    "/api/v3/ticker/bookTicker",
                    "BTCUSDT",
                ),
                (
                    "USD-M book",
                    "https://fapi.binance.com",
                    "/fapi/v1/ticker/bookTicker",
                    "BTCUSDT",
                ),
                (
                    "USD-M premium",
                    "https://fapi.binance.com",
                    "/fapi/v1/premiumIndex",
                    "BTCUSDT",
                ),
            ],
        )
        lines = stdout.getvalue().splitlines()
        self.assertEqual(lines[0], "entry_gap_quote_formula=usdm_bid-spot_ask")
        self.assertEqual(lines[1], "exit_gap_quote_formula=usdm_ask-spot_bid")
        self.assertEqual(
            lines[2],
            "entry_gap_bps_formula=entry_gap_quote/spot_ask*10000;bps_base=spot_ask",
        )
        self.assertEqual(
            lines[3],
            "exit_gap_bps_formula=exit_gap_quote/spot_bid*10000;bps_base=spot_bid",
        )
        self.assertEqual(
            lines[4].split("\t"),
            [
                "completed_utc",
                "usdm_book_time_utc",
                "symbol",
                "spot_bid",
                "spot_ask",
                "usdm_bid",
                "usdm_ask",
                "entry_gap_quote",
                "entry_gap_bps_vs_spot_ask",
                "exit_gap_quote",
                "exit_gap_bps_vs_spot_bid",
                "funding_rate",
                "next_funding_utc",
            ],
        )
        self.assertEqual(
            lines[5].split("\t"),
            [
                "2026-08-13T00:00:00.000Z",
                "2023-11-14T22:13:20.000Z",
                "BTCUSDT",
                "100.00",
                "100.10",
                "100.30",
                "100.40",
                "0.20",
                "19.98001998",
                "0.40",
                "40.00000000",
                "0.00010000",
                "2023-11-15T00:00:00.000Z",
            ],
        )
        self.assertEqual(len(lines), 6)

    def test_main_defaults_to_btc_and_eth(self):
        calls = []
        stdout = io.StringIO()
        with mock.patch.object(
            basis_read, "fetch_public_json", side_effect=self.fake_fetch(calls)
        ), mock.patch.object(
            basis_read, "now_utc", return_value="2026-08-13T00:00:00.000Z"
        ), contextlib.redirect_stdout(stdout):
            exit_code = basis_read.main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [(source, symbol) for source, _, _, symbol in calls],
            [
                ("Spot book", "BTCUSDT"),
                ("USD-M book", "BTCUSDT"),
                ("USD-M premium", "BTCUSDT"),
                ("Spot book", "ETHUSDT"),
                ("USD-M book", "ETHUSDT"),
                ("USD-M premium", "ETHUSDT"),
            ],
        )
        self.assertEqual(len(stdout.getvalue().splitlines()), 7)

    def test_main_reports_http_failure_with_stable_exit_code(self):
        stderr = io.StringIO()
        with mock.patch.object(
            basis_read,
            "fetch_public_json",
            side_effect=basis_read.HttpReadError("Spot book HTTP 451 for BTCUSDT"),
        ), contextlib.redirect_stderr(stderr):
            exit_code = basis_read.main(["BTCUSDT"])

        self.assertEqual(exit_code, basis_read.EXIT_HTTP)
        self.assertIn("Spot book HTTP 451 for BTCUSDT", stderr.getvalue())

    def test_main_reports_data_failure_with_stable_exit_code(self):
        stderr = io.StringIO()
        with mock.patch.object(
            basis_read,
            "fetch_public_json",
            side_effect=self.fake_fetch([]),
        ), mock.patch.object(
            basis_read, "now_utc", return_value="2026-08-13T00:00:00.000Z"
        ), contextlib.redirect_stderr(stderr):
            with mock.patch.object(
                basis_read,
                "build_basis_read_row",
                side_effect=basis_read.DataReadError("crossed Spot book for BTCUSDT"),
            ):
                exit_code = basis_read.main(["BTCUSDT"])

        self.assertEqual(exit_code, basis_read.EXIT_DATA)
        self.assertIn("crossed Spot book for BTCUSDT", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
