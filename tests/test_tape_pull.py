#!/usr/bin/env python3
"""Offline tests for the public historical tape puller."""

import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
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


def agg_trades_csv(extra_bytes=0):
    row = b"1,100,0.1,1,1,1786665600000,false,true\n"
    return row + (b"x" * extra_bytes)


def agg_trades_zip_bytes(symbol=SYMBOL, day=DAY, payload=None):
    output = io.BytesIO()
    member = "%s-aggTrades-%s.csv" % (symbol, day.isoformat())
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            member, payload if payload is not None else agg_trades_csv()
        )
    return output.getvalue()


def write_agg_trades_zip(
    root, symbol=SYMBOL, day=DAY, payload=None, venue=tp.VENUE_SPOT
):
    path = tp.agg_trades_path(Path(root), symbol, day, venue)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(agg_trades_zip_bytes(symbol, day, payload))
    return path


def write_windows_manifest(path, symbols):
    start_ms = tp._day_start_ms(DAY)
    payload = {
        "windows": [
            {
                "symbol": symbol,
                "venue_class": "spot_only",
                "start_ms": start_ms,
                "end_ms": start_ms + 60_000,
                "gap_days": [],
            }
            for symbol in symbols
        ]
    }
    Path(path).write_text(json.dumps(payload), encoding="utf-8")


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


class RepeatingMemberHandle:
    def __init__(self, size, block_size):
        self.remaining = size
        self.block = b"x" * block_size
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.remaining == 0:
            return b""
        requested = self.remaining if size < 0 else size
        count = min(requested, self.remaining, len(self.block))
        self.remaining -= count
        return self.block if count == len(self.block) else self.block[:count]


class RepeatingMemberArchive:
    def __init__(self, size, block_size):
        self.handle = RepeatingMemberHandle(size, block_size)

    def open(self, member, mode):
        if mode != "r" or member.file_size != self.handle.remaining:
            raise AssertionError("unexpected synthetic member open")
        return self.handle


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

    def test_futures_and_funding_url_and_destination_construction(self):
        self.assertEqual(
            tp.kline_url(SYMBOL, DAY, "futures"),
            "https://data.binance.vision/data/futures/um/daily/klines/"
            "BTCUSDT/1m/BTCUSDT-1m-2026-08-14.zip",
        )
        self.assertEqual(
            tp.agg_trades_url(SYMBOL, DAY, "perp"),
            "https://data.binance.vision/data/futures/um/daily/aggTrades/"
            "BTCUSDT/BTCUSDT-aggTrades-2026-08-14.zip",
        )
        self.assertEqual(
            tp.kline_path(self.root, SYMBOL, DAY, "futures"),
            self.root
            / "futures/um/daily/klines/BTCUSDT/1m/"
            "BTCUSDT-1m-2026-08-14.zip",
        )
        start_ms = tp._day_start_ms(DAY)
        end_ms = start_ms + 86_400_000 - 1
        funding_url = tp.funding_rate_url(SYMBOL, start_ms, end_ms)
        self.assertEqual(
            funding_url,
            "https://fapi.binance.com/fapi/v1/fundingRate?"
            "symbol=BTCUSDT&startTime=1786665600000&"
            "endTime=1786751999999&limit=1000",
        )
        tp._validate_public_url(funding_url)
        self.assertEqual(
            tp.funding_path(self.root, SYMBOL, DAY, DAY),
            self.root / "funding/BTCUSDT-fundingRate-2026-08-14-2026-08-14.json",
        )

    def test_full_universe_unicode_symbol_is_safely_percent_encoded(self):
        symbol = "币安人生USDT"
        url = tp.kline_url(symbol, DAY)
        encoded = "%E5%B8%81%E5%AE%89%E4%BA%BA%E7%94%9FUSDT"
        self.assertEqual(
            url,
            "https://data.binance.vision/data/spot/daily/klines/"
            f"{encoded}/1m/{encoded}-1m-2026-08-14.zip",
        )
        tp._validate_public_url(url)
        self.assertEqual(
            tp.kline_path(self.root, symbol, DAY).name,
            "币安人生USDT-1m-2026-08-14.zip",
        )

    def test_cli_mode_construction(self):
        parser = tp.build_parser()
        cohort = parser.parse_args(["--cohort-file", "cohort.json"])
        windows = parser.parse_args(["--windows-file", "windows.json"])
        funding = parser.parse_args(
            ["--cohort-file", "cohort.json", "--funding"]
        )
        self.assertEqual(tp.selected_mode(cohort), "cohort_file")
        self.assertEqual(tp.selected_mode(windows), "windows_file")
        self.assertEqual(tp.selected_mode(funding), "funding")
        with self.assertRaises(tp.InputError):
            tp.selected_mode(parser.parse_args(["--funding"]))

    def test_cohort_manifest_expands_every_declared_venue(self):
        entries = tp.parse_cohort_manifest(
            [
                {"symbol": "AAAUSDT", "venue_class": "spot_only", "gap_days": []},
                {
                    "symbol": "BBBUSDT",
                    "venue_class": "futures_only",
                    "gap_days": [],
                },
                {"symbol": "CCCUSDT", "venue_class": "both", "gap_days": []},
            ]
        )
        self.assertEqual(
            tp.cohort_archive_requests(entries, [DAY]),
            (
                tp.ArchiveRequest("AAAUSDT", "spot", DAY),
                tp.ArchiveRequest("BBBUSDT", "futures", DAY),
                tp.ArchiveRequest("CCCUSDT", "spot", DAY),
                tp.ArchiveRequest("CCCUSDT", "futures", DAY),
            ),
        )

    def test_window_days_are_cross_day_venue_aware_and_deduplicated(self):
        next_day = DAY + timedelta(days=1)
        payload = {
            "windows": [
                {
                    "symbol": "AAAUSDT",
                    "venue_class": "both",
                    "window_start_utc": "2026-08-14T23:59:00Z",
                    "window_end_utc": "2026-08-15T00:01:00Z",
                },
                {
                    "symbol": "AAAUSDT",
                    "venue_class": "both",
                    "start_ms": tp._day_start_ms(DAY) + 60_000,
                    "end_ms": tp._day_start_ms(DAY) + 120_000,
                },
                {
                    "symbol": "BBBUSDT",
                    "venue_class": "futures_only",
                    "window": {
                        "start": "2026-08-14T12:00:00+00:00",
                        "end": "2026-08-14T15:00:00+00:00",
                    },
                },
                {
                    "symbol": "CCCUSDT",
                    "venue_class": "spot_only",
                    "start_utc": "2026-08-14T23:00:00Z",
                    "end_utc": "2026-08-15T00:00:00Z",
                },
            ]
        }
        self.assertEqual(
            tp.window_archive_requests(payload),
            (
                tp.ArchiveRequest("AAAUSDT", "spot", DAY),
                tp.ArchiveRequest("AAAUSDT", "futures", DAY),
                tp.ArchiveRequest("AAAUSDT", "spot", next_day),
                tp.ArchiveRequest("AAAUSDT", "futures", next_day),
                tp.ArchiveRequest("BBBUSDT", "futures", DAY),
                tp.ArchiveRequest("CCCUSDT", "spot", DAY),
            ),
        )

    def test_cohort_gap_days_are_reconciled_for_attempted_days(self):
        manifest = self.root / "cohort.json"
        manifest.write_text(
            json.dumps(
                [
                    {
                        "symbol": SYMBOL,
                        "venue_class": "both",
                        "days_available": 2,
                        "gap_days": ["2026-08-13"],
                    }
                ]
            ),
            encoding="utf-8",
        )
        attempts = (
            tp.ArchiveRequest(SYMBOL, "spot", DAY),
            tp.ArchiveRequest(SYMBOL, "futures", DAY),
        )
        tp.update_cohort_gap_days(manifest, attempts, [attempts[1]])
        first = json.loads(manifest.read_text(encoding="utf-8"))[0]
        self.assertEqual(first["gap_days"], ["2026-08-13", "2026-08-14"])
        self.assertEqual(first["days_available"], 1)
        tp.update_cohort_gap_days(manifest, attempts, [])
        second = json.loads(manifest.read_text(encoding="utf-8"))[0]
        self.assertEqual(second["gap_days"], ["2026-08-13"])
        self.assertEqual(second["days_available"], 2)

    def test_funding_history_download_is_verified_and_idempotent(self):
        funding_time = tp._day_start_ms(DAY) + 8 * 60 * 60 * 1_000
        payload = json.dumps(
            [
                {
                    "symbol": SYMBOL,
                    "fundingTime": funding_time,
                    "fundingRate": "0.00010000",
                    "markPrice": "60000.12500000",
                }
            ]
        ).encode("utf-8")
        requested_urls = []

        def opener(request, timeout):
            requested_urls.append(request.full_url)
            return FakeResponse(payload, request.full_url)

        first = tp.download_funding_history(
            SYMBOL, DAY, DAY, self.root, opener=opener
        )
        self.assertEqual(first.status, "DOWNLOADED")
        self.assertEqual(first.records, 1)
        self.assertEqual(len(requested_urls), 1)
        with mock.patch.object(tp, "_open_get", side_effect=AssertionError("network")):
            second = tp.download_funding_history(SYMBOL, DAY, DAY, self.root)
        self.assertEqual(second.status, "SKIPPED")
        self.assertEqual(second.records, 1)

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

    def test_under_bound_agg_trades_member_streams_and_is_published(self):
        payload = agg_trades_csv(17)
        member_bound = len(payload) + 1
        url = tp.agg_trades_url(SYMBOL, DAY)

        def opener(request, timeout):
            return FakeResponse(
                agg_trades_zip_bytes(payload=payload), request.full_url
            )

        with mock.patch.object(tp, "READ_CHUNK_BYTES", 7):
            result = tp.download_agg_trades(
                SYMBOL,
                DAY,
                self.root,
                opener=opener,
                max_member_bytes=member_bound,
            )
        self.assertEqual(result.status, "DOWNLOADED")
        self.assertEqual(url, tp.agg_trades_url(result.symbol, result.day))
        tp.verify_agg_trades_zip(
            result.path,
            SYMBOL,
            DAY,
            max_member_bytes=member_bound,
        )

    def test_raised_ceiling_streams_above_a_smaller_injected_policy(self):
        self.assertEqual(tp.MAX_MEMBER_BYTES, 1_073_741_824)
        payload = agg_trades_csv(33)
        path = write_agg_trades_zip(self.root, payload=payload)
        smaller_bound = len(payload) - 1
        with self.assertRaises(tp.ArchiveError) as raised:
            tp.verify_agg_trades_zip(
                path,
                SYMBOL,
                DAY,
                max_member_bytes=smaller_bound,
            )
        self.assertEqual(raised.exception.measured_value, len(payload))
        self.assertEqual(raised.exception.bound, smaller_bound)
        with mock.patch.object(tp, "READ_CHUNK_BYTES", 5):
            tp.verify_agg_trades_zip(
                path,
                SYMBOL,
                DAY,
                max_member_bytes=len(payload) + 1,
            )

    def test_public_member_reader_streams_above_retired_private_bound(self):
        retired_bound = 512_000_000
        declared_size = retired_bound + 1
        block_size = 4 * 1024 * 1024
        member = zipfile.ZipInfo(
            "%s-aggTrades-%s.csv" % (SYMBOL, DAY.isoformat())
        )
        member.file_size = declared_size
        archive = RepeatingMemberArchive(declared_size, block_size)

        with mock.patch.object(tp, "READ_CHUNK_BYTES", block_size):
            streamed_size = sum(
                len(chunk)
                for chunk in tp.stream_verified_member(
                    archive, member, tp.MAX_MEMBER_BYTES
                )
            )

        self.assertEqual(streamed_size, declared_size)
        self.assertGreater(len(archive.handle.read_sizes), 2)
        self.assertLessEqual(max(archive.handle.read_sizes), block_size)

    def test_public_member_reader_refuses_declared_size_above_one_gibibyte(self):
        member = zipfile.ZipInfo(
            "%s-aggTrades-%s.csv" % (SYMBOL, DAY.isoformat())
        )
        member.file_size = tp.MAX_MEMBER_BYTES + 1
        archive = mock.Mock()

        with self.assertRaises(tp.ArchiveError) as raised:
            list(
                tp.stream_verified_member(
                    archive, member, tp.MAX_MEMBER_BYTES
                )
            )

        self.assertEqual(raised.exception.measured_value, tp.MAX_MEMBER_BYTES + 1)
        self.assertEqual(raised.exception.bound, tp.MAX_MEMBER_BYTES)
        archive.open.assert_not_called()

    def test_above_bound_archive_is_identified_and_batch_continues(self):
        large_symbol = "LARGEUSDT"
        small_symbol = "SMALLUSDT"
        manifest = self.root / "windows.json"
        write_windows_manifest(manifest, [large_symbol, small_symbol])
        small_payload = agg_trades_csv()
        member_bound = len(small_payload) + 1
        large_payload = agg_trades_csv(64)

        def opener(request, timeout):
            symbol = (
                large_symbol
                if "/%s/" % large_symbol in request.full_url
                else small_symbol
            )
            payload = large_payload if symbol == large_symbol else small_payload
            return FakeResponse(
                agg_trades_zip_bytes(symbol, DAY, payload), request.full_url
            )

        rows = []
        summary = tp.pull_windows_file(
            manifest,
            self.root,
            opener=opener,
            emit=rows.append,
            max_member_bytes=member_bound,
        )
        self.assertEqual(summary.requests, 2)
        self.assertEqual(summary.downloaded, 1)
        self.assertEqual(summary.skipped, 0)
        self.assertEqual(summary.gaps, ())
        self.assertEqual(len(summary.failures), 1)
        self.assertEqual(summary.failures[0].request.symbol, large_symbol)
        error_row = next(row for row in rows if row.startswith("ERROR |"))
        self.assertIn("ERROR | ArchiveError |", error_row)
        self.assertIn("symbol=%s" % large_symbol, error_row)
        self.assertIn("date=%s" % DAY.isoformat(), error_row)
        self.assertIn("venue=spot", error_row)
        self.assertIn("measured=%d" % len(large_payload), error_row)
        self.assertIn("bound=%d" % member_bound, error_row)
        self.assertTrue(rows[-1].endswith("gaps=0 | failures=1"))
        self.assertFalse(
            tp.agg_trades_path(self.root, large_symbol, DAY).exists()
        )
        self.assertTrue(
            tp.agg_trades_path(self.root, small_symbol, DAY).exists()
        )
        manifest_rows = {
            row["symbol"]: row
            for row in json.loads(manifest.read_text(encoding="utf-8"))["windows"]
        }
        self.assertEqual(
            manifest_rows[large_symbol]["gap_days"], [DAY.isoformat()]
        )
        self.assertEqual(manifest_rows[small_symbol]["gap_days"], [])

    def test_windows_resume_keeps_verified_archive_skipped(self):
        manifest = self.root / "windows.json"
        write_windows_manifest(manifest, [SYMBOL])
        expected = write_agg_trades_zip(self.root)
        rows = []
        with mock.patch.object(
            tp, "_open_get", side_effect=AssertionError("network")
        ):
            summary = tp.pull_windows_file(
                manifest, self.root, emit=rows.append
            )
        self.assertEqual(summary.downloaded, 0)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.failures, ())
        self.assertEqual(
            tp.agg_trades_path(self.root, SYMBOL, DAY), expected
        )
        self.assertTrue(any(row.startswith("SKIPPED | %s |" % SYMBOL) for row in rows))

    def test_completed_failure_exit_code_is_distinct_from_clean(self):
        request = tp.ArchiveRequest(SYMBOL, tp.VENUE_SPOT, DAY)
        failure = tp.ArchiveFailure(
            request,
            "ArchiveError",
            "archive member size is outside bounds",
            "81",
            "80",
        )
        failed = tp.ManifestPullSummary(
            "windows_file", (SYMBOL,), 1, 0, 0, (), (failure,)
        )
        clean = tp.ManifestPullSummary(
            "windows_file", (SYMBOL,), 1, 1, 0, ()
        )
        arguments = [
            "--windows-file",
            str(self.root / "windows.json"),
            "--dest",
            str(self.root),
        ]
        with mock.patch.object(tp, "pull_windows_file", return_value=failed):
            self.assertEqual(
                tp.main(arguments), tp.COMPLETED_WITH_FAILURES_EXIT_CODE
            )
        with mock.patch.object(tp, "pull_windows_file", return_value=clean):
            self.assertEqual(tp.main(arguments), 0)

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
