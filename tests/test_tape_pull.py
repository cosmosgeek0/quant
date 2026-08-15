#!/usr/bin/env python3
"""Offline tests for the public historical tape puller."""

import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "study"))

import tape_pull as tp


DAY = date(2026, 8, 14)
SYMBOL = "BTCUSDT"


def kline_csv(*quote_volumes):
    rows = []
    for index, volume in enumerate(quote_volumes):
        rows.append(
            "%d,1,2,0.5,1.5,3,%d,%s,4,1,1,0\n"
            % (index * 60_000, index * 60_000 + 59_999, volume)
        )
    return "".join(rows).encode("utf-8")


def write_kline_zip(root, symbol=SYMBOL, day=DAY, payload=None):
    path = tp.kline_path(Path(root), symbol, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    member = "%s-1m-%s.csv" % (symbol, day.isoformat())
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, payload if payload is not None else kline_csv("10"))
    return path


def zip_bytes(symbol=SYMBOL, day=DAY, payload=None):
    output = io.BytesIO()
    member = "%s-1m-%s.csv" % (symbol, day.isoformat())
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, payload if payload is not None else kline_csv("10"))
    return output.getvalue()


class FakeResponse:
    def __init__(self, payload, url, status=200, headers=None):
        self._handle = io.BytesIO(payload)
        self._url = url
        self.status = status
        self.headers = {} if headers is None else headers

    def read(self, size=-1):
        return self._handle.read(size)

    def close(self):
        self._handle.close()

    def geturl(self):
        return self._url


class TapePullTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_url_and_destination_construction(self):
        self.assertEqual(
            tp.kline_url(SYMBOL, DAY),
            "https://data.binance.vision/data/spot/daily/klines/"
            "BTCUSDT/1m/BTCUSDT-1m-2026-08-14.zip",
        )
        self.assertEqual(
            tp.kline_path(self.root, SYMBOL, "2026-08-14"),
            self.root
            / "spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-08-14.zip",
        )

    def test_allowlist_rejects_query_and_other_host(self):
        with self.assertRaises(tp.InputError):
            tp._validate_public_url(tp.SPOT_EXCHANGE_INFO_URL + "?x=1")
        with self.assertRaises(tp.InputError):
            tp._validate_public_url("https://example.com/file.zip")
        with self.assertRaises(tp.InputError):
            tp._validate_public_url(
                "https://data.binance.vision/data/spot/daily/klines/../file.zip"
            )

    def test_existing_intact_archive_is_idempotently_skipped(self):
        expected = write_kline_zip(self.root)
        with mock.patch.object(tp, "_open_get", side_effect=AssertionError("network")):
            result = tp.download_kline(SYMBOL, DAY, self.root)
        self.assertEqual(result.status, "SKIPPED")
        self.assertEqual(result.path, expected)
        tp.verify_kline_zip(expected, SYMBOL, DAY)

    def test_corrupt_existing_zip_fixture_is_rejected_without_network(self):
        path = tp.kline_path(self.root, SYMBOL, DAY)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not a zip fixture")
        with mock.patch.object(tp, "_open_get", side_effect=AssertionError("network")):
            with self.assertRaises(tp.ArchiveError):
                tp.download_kline(SYMBOL, DAY, self.root)

    def test_corrupt_download_is_rejected_and_not_published(self):
        url = tp.kline_url(SYMBOL, DAY)

        def opener(request, timeout):
            self.assertEqual(request.get_method(), "GET")
            return FakeResponse(b"broken zip fixture", url)

        with self.assertRaises(tp.ArchiveError):
            tp.download_kline(SYMBOL, DAY, self.root, opener=opener)
        self.assertFalse(tp.kline_path(self.root, SYMBOL, DAY).exists())
        self.assertEqual(list(self.root.rglob("*.part")), [])

    def test_downloaded_zip_is_verified_and_published(self):
        url = tp.kline_url(SYMBOL, DAY)
        payload = zip_bytes(payload=kline_csv("1.25", "2.75"))

        def opener(request, timeout):
            return FakeResponse(payload, url)

        result = tp.download_kline(SYMBOL, DAY, self.root, opener=opener)
        self.assertEqual(result.status, "DOWNLOADED")
        self.assertEqual(tp.quote_volume_from_zip(result.path, SYMBOL, DAY), Decimal("4.00"))

    def test_429_uses_retry_after_then_get_succeeds(self):
        url = tp.SPOT_EXCHANGE_INFO_URL
        calls = []
        sleeps = []
        payload = json.dumps({"symbols": []}).encode("utf-8")

        def opener(request, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise HTTPError(url, 429, "rate limited", {"Retry-After": "3"}, None)
            return FakeResponse(payload, url)

        self.assertEqual(
            tp.fetch_json(url, opener=opener, sleep_fn=sleeps.append), {"symbols": []}
        )
        self.assertEqual(calls, [url, url])
        self.assertEqual(sleeps, [3])

    def test_cohort_parse_filters_and_ranks_fixture_exchange_info(self):
        spot = {
            "symbols": [
                {"symbol": "AAAUSDT", "status": "TRADING", "quoteAsset": "USDT"},
                {"symbol": "BBBUSDT", "status": "TRADING", "quoteAsset": "USDT"},
                {"symbol": "CCCUSDT", "status": "BREAK", "quoteAsset": "USDT"},
                {
                    "symbol": "DDDUSDT",
                    "status": "TRADING",
                    "quoteAsset": "USDT",
                    "isSpotTradingAllowed": False,
                },
                {"symbol": "EEEBTC", "status": "TRADING", "quoteAsset": "BTC"},
            ]
        }
        futures = {
            "symbols": [
                {
                    "symbol": "AAAUSDT",
                    "status": "TRADING",
                    "quoteAsset": "USDT",
                    "contractType": "PERPETUAL",
                },
                {
                    "symbol": "BBBUSDT",
                    "status": "TRADING",
                    "quoteAsset": "USDT",
                    "contractType": "PERPETUAL",
                },
                {
                    "symbol": "CCCUSDT",
                    "status": "TRADING",
                    "quoteAsset": "USDT",
                    "contractType": "CURRENT_QUARTER",
                },
            ]
        }
        volumes = {"AAAUSDT": Decimal("9"), "BBBUSDT": Decimal("20")}
        self.assertEqual(tp.parse_cohort(spot, futures, volumes, 2), ["BBBUSDT", "AAAUSDT"])
        self.assertEqual(tp.parse_cohort(spot, futures, volumes, 1), ["BBBUSDT"])

    def test_unknown_quote_volume_fails_closed(self):
        with self.assertRaises(tp.InputError):
            tp.rank_cohort(["AAAUSDT"], {}, 1)
        with self.assertRaises(tp.InputError):
            tp.rank_cohort([], {}, 1)

    def test_completed_days_exclude_current_partial_utc_day(self):
        now = datetime(2026, 8, 15, 1, 2, tzinfo=timezone.utc)
        self.assertEqual(
            tp.completed_utc_days(3, now),
            [date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14)],
        )


if __name__ == "__main__":
    unittest.main()
