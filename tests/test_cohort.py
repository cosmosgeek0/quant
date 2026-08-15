#!/usr/bin/env python3
"""Offline fixture tests for the Volume Dataset V2 cohort manifest."""

import io
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


from study import cohort


SPOT_FIXTURE = {
    "symbols": [
        {
            "symbol": "AAAUSDT",
            "status": "TRADING",
            "quoteAsset": "USDT",
            "isSpotTradingAllowed": True,
            "onboardDate": 1_700_000_000_000,
        },
        {
            "symbol": "CCCUSDT",
            "status": "TRADING",
            "quoteAsset": "USDT",
            "isSpotTradingAllowed": True,
        },
        {
            "symbol": "NOVOLUMEUSDT",
            "status": "TRADING",
            "quoteAsset": "USDT",
            "isSpotTradingAllowed": True,
        },
        {
            "symbol": "币安人生USDT",
            "status": "TRADING",
            "quoteAsset": "USDT",
            "isSpotTradingAllowed": True,
        },
        {
            "symbol": "DISABLEDUSDT",
            "status": "TRADING",
            "quoteAsset": "USDT",
            "isSpotTradingAllowed": False,
        },
        {"symbol": "BREAKUSDT", "status": "BREAK", "quoteAsset": "USDT"},
        {"symbol": "AAABTC", "status": "TRADING", "quoteAsset": "BTC"},
    ]
}

FUTURES_FIXTURE = {
    "symbols": [
        {
            "symbol": "BBBUSDT",
            "status": "TRADING",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "onboardDate": 1_710_000_000_000,
        },
        {
            "symbol": "CCCUSDT",
            "status": "TRADING",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "onboardDate": 1_720_000_000_000,
        },
        {
            "symbol": "BTCUSDT_260925",
            "status": "TRADING",
            "quoteAsset": "USDT",
            "contractType": "CURRENT_QUARTER",
        },
        {
            "symbol": "SETTLEDUSDT",
            "status": "SETTLING",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
        },
    ]
}


class FakeResponse:
    def __init__(self, payload, url, status=200):
        self._handle = io.BytesIO(payload)
        self._url = url
        self.status = status

    def read(self, size=-1):
        return self._handle.read(size)

    def close(self):
        self._handle.close()

    def geturl(self):
        return self._url


class CohortTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def touch_kline(self, venue, symbol, day):
        path = cohort.kline_path(self.root / "data", venue, symbol, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_full_universe_is_classified_without_volume_ranking(self):
        members = cohort.classify_universe(SPOT_FIXTURE, FUTURES_FIXTURE)
        self.assertEqual(
            [(row.symbol, row.venue_class) for row in members],
            [
                ("AAAUSDT", "spot_only"),
                ("BBBUSDT", "futures_only"),
                ("CCCUSDT", "both"),
                ("NOVOLUMEUSDT", "spot_only"),
                ("币安人生USDT", "spot_only"),
            ],
        )
        self.assertEqual(members[0].listed_since, date(2023, 11, 14))
        self.assertEqual(members[1].listed_since, date(2024, 3, 9))
        self.assertEqual(members[2].listed_since, date(2024, 7, 3))

    def test_manifest_probes_exact_paths_and_reports_every_gap(self):
        days = (date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14))
        self.touch_kline("spot", "AAAUSDT", days[0])
        self.touch_kline("spot", "AAAUSDT", days[2])
        self.touch_kline("futures", "BBBUSDT", days[1])
        self.touch_kline("spot", "CCCUSDT", days[0])
        self.touch_kline("futures", "CCCUSDT", days[0])
        self.touch_kline("spot", "CCCUSDT", days[1])
        self.touch_kline("futures", "CCCUSDT", days[2])
        stray = self.root / "data" / "CCCUSDT-1m-2026-08-13.zip"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.touch()

        rows = cohort.build_manifest(
            SPOT_FIXTURE, FUTURES_FIXTURE, self.root / "data", days
        )
        by_symbol = {row["symbol"]: row for row in rows}

        self.assertEqual(by_symbol["AAAUSDT"]["days_available"], 2)
        self.assertEqual(by_symbol["AAAUSDT"]["gap_days"], ["2026-08-13"])
        self.assertEqual(by_symbol["BBBUSDT"]["days_available"], 1)
        self.assertEqual(
            by_symbol["BBBUSDT"]["gap_days"],
            ["2026-08-12", "2026-08-14"],
        )
        self.assertEqual(by_symbol["CCCUSDT"]["days_available"], 1)
        self.assertEqual(
            by_symbol["CCCUSDT"]["gap_days"],
            ["2026-08-13", "2026-08-14"],
        )
        self.assertEqual(by_symbol["NOVOLUMEUSDT"]["days_available"], 0)
        self.assertEqual(
            by_symbol["NOVOLUMEUSDT"]["gap_days"],
            ["2026-08-12", "2026-08-13", "2026-08-14"],
        )
        self.assertNotIn("listed_since", by_symbol["NOVOLUMEUSDT"])
        self.assertEqual(by_symbol["币安人生USDT"]["days_available"], 0)

    def test_coverage_defaults_to_preceding_completed_utc_days(self):
        now = datetime(2026, 8, 15, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(
            cohort.coverage_days(3, now=now),
            (
                date(2026, 8, 12),
                date(2026, 8, 13),
                date(2026, 8, 14),
            ),
        )
        self.assertEqual(
            cohort.coverage_days(2, end_day="2026-01-01"),
            (date(2025, 12, 31), date(2026, 1, 1)),
        )

    def test_malformed_metadata_and_duplicate_days_fail_closed(self):
        duplicate = {
            "symbols": [SPOT_FIXTURE["symbols"][0], SPOT_FIXTURE["symbols"][0]]
        }
        with self.assertRaises(cohort.InputError):
            cohort.classify_universe(duplicate, {"symbols": []})
        with self.assertRaises(cohort.InputError):
            cohort.build_manifest(
                SPOT_FIXTURE,
                FUTURES_FIXTURE,
                self.root / "data",
                [date(2026, 8, 14), date(2026, 8, 14)],
            )
        malformed = {
            "symbols": [
                {
                    "symbol": "AAAUSDT",
                    "status": "TRADING",
                    "quoteAsset": "USDT",
                    "onboardDate": "unknown",
                }
            ]
        }
        with self.assertRaises(cohort.InputError):
            cohort.classify_universe(malformed, {"symbols": []})

    def test_public_reader_allows_only_exact_get_endpoints(self):
        payload = json.dumps({"symbols": []}).encode("utf-8")
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, request.get_method(), timeout))
            return FakeResponse(payload, request.full_url)

        self.assertEqual(
            cohort.fetch_json(cohort.SPOT_EXCHANGE_INFO_URL, opener=opener),
            {"symbols": []},
        )
        self.assertEqual(calls[0][1], "GET")
        with self.assertRaises(cohort.InputError):
            cohort.fetch_json(
                cohort.SPOT_EXCHANGE_INFO_URL + "?symbol=BTCUSDT", opener=opener
            )
        with self.assertRaises(cohort.InputError):
            cohort.fetch_json("https://example.com/api/v3/exchangeInfo", opener=opener)

    def test_generate_fetches_two_fixtures_and_writes_deterministic_json(self):
        output = self.root / "out" / "cohort_manifest.json"
        calls = []
        emitted = []

        def fetcher(url):
            calls.append(url)
            return {
                cohort.SPOT_EXCHANGE_INFO_URL: SPOT_FIXTURE,
                cohort.FUTURES_EXCHANGE_INFO_URL: FUTURES_FIXTURE,
            }[url]

        returned = cohort.generate_manifest(
            data_dir=self.root / "data",
            output=output,
            days=2,
            end_day="2026-08-14",
            fetcher=fetcher,
            emit=emitted.append,
        )

        self.assertEqual(
            calls,
            [cohort.SPOT_EXCHANGE_INFO_URL, cohort.FUTURES_EXCHANGE_INFO_URL],
        )
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), returned)
        self.assertTrue(output.read_text(encoding="utf-8").endswith("\n"))
        self.assertEqual(
            emitted[0],
            "COHORT | total=5 | spot_only=3 | futures_only=1 | both=1",
        )
        self.assertIn("requested_days=2", emitted[1])
        self.assertEqual(
            sorted(path.name for path in output.parent.iterdir()),
            ["cohort_manifest.json"],
        )


if __name__ == "__main__":
    unittest.main()
