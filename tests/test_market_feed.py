#!/usr/bin/env python3
"""Offline tests for the continuous all-token Binance market feed."""

import base64
import hashlib
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import market_feed as mf


FIXED_UTC = "2026-08-14T12:00:00.000Z"
FIXED_MS = 2_000_000_000


def lot_filters(min_qty="0.001", step="0.001", notional="5"):
    return [
        {
            "filterType": "LOT_SIZE",
            "minQty": min_qty,
            "maxQty": "100000",
            "stepSize": step,
        },
        {"filterType": "MIN_NOTIONAL", "minNotional": notional},
    ]


def spot_info(symbol, **overrides):
    row = {
        "symbol": symbol,
        "status": "TRADING",
        "quoteAsset": "USDT",
        "isSpotTradingAllowed": True,
        "filters": lot_filters(),
    }
    row.update(overrides)
    return row


def perp_info(symbol, **overrides):
    row = {
        "symbol": symbol,
        "status": "TRADING",
        "quoteAsset": "USDT",
        "contractType": "PERPETUAL",
        "filters": lot_filters("0.01", "0.01", "10"),
    }
    row.update(overrides)
    return row


def metadata_reader(spot=(), perp=()):
    def reader(base, unused_path, unused_params):
        symbols, factory = ((spot, spot_info) if base == mf.SPOT_BASE_URL
                            else (perp, perp_info))
        return {"symbols": [factory(symbol) for symbol in symbols]}
    return reader


def book_frame(symbol, bid, source_ms=None, stream=None):
    row = {"s": symbol, "b": str(bid), "a": str(bid + 1)}
    if source_ms is not None:
        row["T"] = source_ms
    return json.dumps({"stream": stream or f"{symbol.lower()}@bookTicker", "data": row})


class FakeTransport:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.sent = []
        self.closed = False
        self.timeouts = []

    def recv(self, size):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def sendall(self, payload):
        self.sent.append(payload)

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def close(self):
        self.closed = True


class FrameParsingTests(unittest.TestCase):
    def test_partial_unmasked_text_frame_preserves_buffer(self):
        encoded = mf.encode_frame(0x1, b'{"ok":true}', masked=False)
        frame, remaining = mf.parse_frame(encoded[:4])
        self.assertIsNone(frame)
        self.assertEqual(remaining, encoded[:4])

        frame, remaining = mf.parse_frame(encoded + b"tail")
        self.assertEqual(frame.opcode, 0x1)
        self.assertTrue(frame.fin)
        self.assertFalse(frame.masked)
        self.assertEqual(frame.payload, b'{"ok":true}')
        self.assertEqual(remaining, b"tail")

    def test_masked_extended_frame_is_unmasked(self):
        payload = b"x" * 300
        encoded = mf.encode_frame(
            0x1, payload, masked=True, mask_key=b"abcd"
        )
        frame, remaining = mf.parse_frame(encoded)
        self.assertTrue(frame.masked)
        self.assertEqual(frame.payload, payload)
        self.assertEqual(remaining, b"")

    def test_ping_fragmented_text_and_close_are_handled(self):
        incoming = b"".join(
            [
                mf.encode_frame(0x9, b"hi", masked=False),
                mf.encode_frame(0x1, b"hel", fin=False, masked=False),
                mf.encode_frame(0x0, b"lo", masked=False),
                mf.encode_frame(0x8, b"\x03\xe8", masked=False),
            ]
        )
        transport = FakeTransport([incoming])
        client = mf.RFC6455Client(transport)
        self.assertEqual(client.recv_text(), "hello")
        pong, trailing = mf.parse_frame(transport.sent[0])
        self.assertEqual((pong.opcode, pong.payload, trailing), (0xA, b"hi", b""))
        with self.assertRaises(mf.WebSocketClosed):
            client.recv_text()
        close, trailing = mf.parse_frame(transport.sent[1])
        self.assertEqual((close.opcode, close.payload, trailing), (0x8, b"\x03\xe8", b""))
        self.assertTrue(transport.closed)


class HandshakeTests(unittest.TestCase):
    def test_futures_ticker_and_kline_use_measured_routes(self):
        self.assertEqual(
            mf.FAPI_WS_URL,
            "wss://fstream.binance.com/stream"
            "?streams=!bookTicker",
        )
        urls = mf.websocket_urls({"BTCUSDT": mf.empty_record(FIXED_UTC, "both")})
        self.assertTrue(urls["perp"][1].startswith(
            "wss://fstream.binance.com/market/stream?streams="))

    def test_tls_upgrade_is_verified_with_injected_socket(self):
        nonce = b"the sample nonce"
        key = base64.b64encode(nonce).decode("ascii")
        accept = base64.b64encode(
            hashlib.sha1((key + mf.RFC6455_GUID).encode("ascii")).digest()
        ).decode("ascii")
        text_frame = mf.encode_frame(0x1, b"ready", masked=False)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: keep-alive, Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii") + text_frame
        transport = FakeTransport([response])
        socket_calls = []

        def socket_factory(address, timeout):
            socket_calls.append((address, timeout))
            return transport

        class Context:
            def wrap_socket(self, raw, server_hostname):
                self.args = (raw, server_hostname)
                return raw

        context = Context()
        spot_url = mf.websocket_urls(
            {"BTCUSDT": mf.empty_record(FIXED_UTC, "spot")})["spot"][0]
        client = mf.RFC6455Client.connect(
            spot_url,
            socket_factory=socket_factory,
            ssl_context=context,
            key_bytes=nonce,
        )
        self.assertEqual(
            socket_calls,
            [(('stream.binance.com', 9443), mf.DEFAULT_CONNECT_TIMEOUT)],
        )
        request = transport.sent[0].decode("ascii")
        self.assertIn(
            "GET /stream?streams=btcusdt@bookTicker HTTP/1.1", request
        )
        self.assertEqual(client.recv_text(), "ready")


class MetadataTests(unittest.TestCase):
    def test_exchange_info_filters_tradable_usdt_and_keeps_decimal_text(self):
        spot = mf.parse_exchange_metadata(
            {
                "symbols": [
                    spot_info("AAAUSDT"),
                    spot_info("BREAKUSDT", status="BREAK"),
                    spot_info("AAABTC", quoteAsset="BTC"),
                    spot_info("NOUSDT", isSpotTradingAllowed=False),
                ]
            },
            "spot",
        )
        perp = mf.parse_exchange_metadata(
            {
                "symbols": [
                    perp_info("AAAUSDT"),
                    perp_info("QUARTERUSDT", contractType="CURRENT_QUARTER"),
                ]
            },
            "perp",
        )
        self.assertEqual(set(spot), {"AAAUSDT"})
        self.assertEqual(
            spot["AAAUSDT"],
            {
                "spot_min_qty": "0.001",
                "spot_step_size": "0.001",
                "spot_min_notional": "5",
            },
        )
        self.assertEqual(perp["AAAUSDT"]["perp_min_qty"], "0.01")
        self.assertEqual(perp["AAAUSDT"]["perp_min_notional"], "10")

    def test_metadata_merge_preserves_books_changes_venues_and_prunes(self):
        prior = mf.empty_record("old", "both")
        prior.update(
            {
                "spot_bid": "1",
                "spot_ask": "2",
                "perp_bid": "3",
                "perp_ask": "4",
                "mark": "3.5",
            }
        )
        state = {"AAAUSDT": prior, "OLDUSDT": mf.empty_record("old", "spot")}
        spot = mf.parse_exchange_metadata(
            {"symbols": [spot_info("AAAUSDT"), spot_info("NEWUSDT")]}, "spot"
        )
        merged = mf.merge_metadata(state, spot, {}, FIXED_UTC)
        self.assertEqual(set(merged), {"AAAUSDT", "NEWUSDT"})
        self.assertEqual(merged["AAAUSDT"]["venues"], "spot")
        self.assertEqual(merged["AAAUSDT"]["spot_bid"], "1")
        self.assertIsNone(merged["AAAUSDT"]["perp_bid"])
        self.assertIsNone(merged["AAAUSDT"]["mark"])
        self.assertEqual(merged["AAAUSDT"]["spot_step_size"], "0.001")
        self.assertEqual(set(merged["AAAUSDT"]), set(mf.FEED_FIELDS))

    def test_venue_change_drops_windows_and_requires_new_subscription_plan(self):
        phase = {"perp": False}

        def reader(base_url, path, params):
            self.assertEqual(params, {})
            if base_url == mf.SPOT_BASE_URL:
                self.assertEqual(path, mf.SPOT_EXCHANGE_INFO_PATH)
                return {"symbols": [spot_info("AAAUSDT")]}
            self.assertEqual((base_url, path), (mf.FAPI_BASE_URL, mf.FAPI_EXCHANGE_INFO_PATH))
            return {
                "symbols": [perp_info("AAAUSDT")] if phase["perp"] else []
            }

        with tempfile.TemporaryDirectory() as raw:
            feed = mf.MarketFeed(
                Path(raw) / "feed.json",
                Path(raw) / "STOP",
                serve=True,
                window=3,
                reader=reader,
                ms_fn=lambda: FIXED_MS,
            )
            self.assertTrue(feed._refresh_metadata())
            feed.windows["AAAUSDT"] = {
                "closed_1m": [[str(FIXED_MS - 60_000), "10"]],
                "venue": "spot",
            }
            old_urls = mf.websocket_urls(feed.state)
            self.assertTrue(any("aaausdt@kline_1m" in url for url in old_urls["spot"]))

            phase["perp"] = True
            self.assertTrue(feed._refresh_metadata())
            self.assertEqual(feed.state["AAAUSDT"]["venues"], "both")
            self.assertNotIn("AAAUSDT", feed.windows)
            new_urls = mf.websocket_urls(feed.state)
            self.assertFalse(any("aaausdt@kline_1m" in url for url in new_urls["spot"]))
            self.assertTrue(any("aaausdt@kline_1m" in url for url in new_urls["perp"]))

            stale_spot = {
                "s": "AAAUSDT",
                "k": {
                    "s": "AAAUSDT",
                    "i": "1m",
                    "T": FIXED_MS,
                    "q": "99",
                    "x": True,
                },
            }
            self.assertEqual(
                mf.merge_kline_rows(
                    feed.state,
                    "spot",
                    "aaausdt@kline_1m",
                    [stale_spot],
                    feed.windows,
                    3,
                ),
                set(),
            )
            new_perp = json.loads(json.dumps(stale_spot))
            new_perp["k"]["q"] = "20"
            self.assertEqual(
                mf.merge_kline_rows(
                    feed.state,
                    "perp",
                    "aaausdt@kline_1m",
                    [new_perp],
                    feed.windows,
                    3,
                ),
                {"AAAUSDT"},
            )
            self.assertEqual(
                feed.windows["AAAUSDT"],
                {
                    "closed_1m": [[str(FIXED_MS), "20"]],
                    "venue": "perp",
                    "last_volume_close_ms": str(FIXED_MS),
                },
            )


class RecordMergeTests(unittest.TestCase):
    def setUp(self):
        spot = mf.parse_exchange_metadata(
            {"symbols": [spot_info("AAAUSDT"), spot_info("SPOTUSDT"),
                         spot_info("BTCUSDT")]}, "spot"
        )
        perp = mf.parse_exchange_metadata(
            {"symbols": [perp_info("AAAUSDT"), perp_info("PERPUSDT"),
                         perp_info("GUAUSDT")]}, "perp"
        )
        self.state = mf.merge_metadata({}, spot, perp, FIXED_UTC)
        self.seen = {}

    def test_measured_spot_and_futures_book_events_merge(self):
        received_ms = 1_786_883_000_010
        spot_stream, spot_rows = mf.parse_combined_message(json.dumps(
            {"stream": "btcusdt@bookTicker", "data": {
                "u": 98573333943, "s": "BTCUSDT", "b": "63025.35000000",
                "B": "1.96503000", "a": "63025.36000000", "A": "12.97784000"}}
        ))
        changed = mf.merge_ticker_rows(
            self.state,
            "spot",
            spot_stream,
            spot_rows,
            FIXED_UTC,
            received_ms,
            self.seen,
        )
        self.assertEqual(changed, {"BTCUSDT"})
        book = {"e": "bookTicker", "u": 400900217, "s": "GUAUSDT",
                "ps": "GUAUSDT", "b": "0.0424900", "B": "824",
                "a": "0.0425100", "A": "255", "T": 1_786_883_000_008,
                "E": 1_786_883_000_009}
        perp_stream, perp_rows = mf.parse_combined_message(json.dumps(
            {"stream": "!bookTicker", "data": book}
        ))
        changed = mf.merge_ticker_rows(
            self.state,
            "perp",
            perp_stream,
            perp_rows,
            FIXED_UTC,
            received_ms,
            self.seen,
        )
        self.assertEqual(changed, {"GUAUSDT"})
        self.assertEqual(
            (self.state["BTCUSDT"]["spot_bid"],
             self.state["BTCUSDT"]["spot_ask"],
             self.state["GUAUSDT"]["perp_bid"],
             self.state["GUAUSDT"]["perp_ask"]),
            ("63025.35000000", "63025.36000000", "0.0424900", "0.0425100"),
        )
        self.assertEqual(self.seen,
            {"BTCUSDT": {"spot": received_ms},
             "GUAUSDT": {"perp": 1_786_883_000_008}})

    def test_incomplete_or_crossed_book_fails_closed(self):
        rows = {"AAAUSDT": {"s": "AAAUSDT", "b": "3", "B": "1"},
                "SPOTUSDT": {"s": "SPOTUSDT", "b": "3", "B": "1",
                             "a": "2", "A": "1"}}
        for symbol, row in rows.items():
            changed = mf.merge_ticker_rows(self.state, "spot",
                f"{symbol.lower()}@bookTicker", [row], FIXED_UTC, FIXED_MS, self.seen)
            self.assertEqual(changed, {symbol})
            self.assertEqual(
                (self.state[symbol]["spot_bid"], self.state[symbol]["spot_ask"]),
                (None, None),
            )


class SubscriptionTests(unittest.TestCase):
    def test_large_universe_uses_measured_routes_and_stream_caps(self):
        symbols = [f"S{index:04d}USDT" for index in range(500)]
        state = {
            symbol: mf.empty_record(FIXED_UTC, "both") for symbol in symbols
        }
        urls = mf.websocket_urls(state)
        self.assertEqual(len(urls["spot"]), 3)
        self.assertEqual(len(urls["perp"]), 4)
        self.assertEqual(urls["perp"][0], mf.FAPI_WS_URL)
        spot_streams = []
        for url in urls["spot"]:
            streams = url.split("?streams=", 1)[1].split("/")
            self.assertLessEqual(len(streams), mf.MAX_STREAMS_PER_CONNECTION)
            spot_streams.extend(streams)
        self.assertEqual(spot_streams,
            [f"{symbol.lower()}@bookTicker" for symbol in symbols])
        subscribed_klines = []
        for url in urls["perp"][1:]:
            self.assertTrue(url.startswith(mf.FAPI_KLINE_WS_URL + "?streams="))
            streams = url.split("?streams=", 1)[1].split("/")
            self.assertLessEqual(len(streams), mf.MAX_STREAMS_PER_CONNECTION)
            subscribed_klines.extend(
                stream
                for stream in streams
                if stream.endswith(mf.KLINE_STREAM_SUFFIX)
            )
        self.assertEqual(
            subscribed_klines,
            [f"{symbol.lower()}@kline_1m" for symbol in symbols],
        )


class RollingWindowTests(unittest.TestCase):
    def setUp(self):
        spot = mf.parse_exchange_metadata(
            {"symbols": [spot_info("AAAUSDT"), spot_info("SPOTUSDT")]}, "spot"
        )
        perp = mf.parse_exchange_metadata(
            {"symbols": [perp_info("AAAUSDT")]}, "perp"
        )
        self.state = mf.merge_metadata({}, spot, perp, FIXED_UTC)

    @staticmethod
    def kline(symbol, close_ms, volume, closed=True):
        return {
            "s": symbol,
            "k": {
                "s": symbol,
                "i": "1m",
                "T": close_ms,
                "q": str(volume),
                "x": closed,
            },
        }

    def test_closed_kline_windows_are_exact_bounded_and_prefer_perp(self):
        windows = {}
        ignored = mf.merge_kline_rows(
            self.state,
            "spot",
            "aaausdt@kline_1m",
            [self.kline("AAAUSDT", 1_000_000, "99")],
            windows,
            3,
        )
        self.assertEqual(ignored, set())
        for index, volume in enumerate(("10", "20", "30", "90")):
            changed = mf.merge_kline_rows(
                self.state,
                "perp",
                "aaausdt@kline_1m",
                [self.kline("AAAUSDT", 1_000_000 + index * 60_000, volume)],
                windows,
                3,
            )
            self.assertEqual(changed, {"AAAUSDT"})
        mf.merge_kline_rows(
            self.state,
            "perp",
            "aaausdt@kline_1m",
            [self.kline("AAAUSDT", 1_240_000, "999", closed=False)],
            windows,
            3,
        )
        self.assertEqual(
            windows["AAAUSDT"]["closed_1m"],
            [["1060000", "20"], ["1120000", "30"], ["1180000", "90"]],
        )

    def test_serve_snapshot_round_trips_windows_and_whole_universe_urls(self):
        self.state["AAAUSDT"].update(
            {
                "spot_bid": "10",
                "spot_ask": "11",
                "perp_bid": "12",
                "perp_ask": "13",
                "freshness_ms": "0",
            }
        )
        windows = {
            "AAAUSDT": {
                "closed_1m": [
                    [str(FIXED_MS - 120_000), "10"],
                    [str(FIXED_MS - 60_000), "20"],
                    [str(FIXED_MS), "90"],
                ],
                "venue": "perp",
            }
        }
        payload = mf.feed_snapshot_payload(self.state, windows, 3, FIXED_MS)
        self.assertEqual(payload["symbols"]["AAAUSDT"]["surge_ratio"], "4.5")
        self.assertEqual(payload["window_oldest_ms"], str(FIXED_MS - 120_000))
        self.assertEqual(payload["window_newest_ms"], str(FIXED_MS))
        self.assertEqual(
            payload["symbols"]["AAAUSDT"]["window_venue"], "perp"
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "feed.json"
            mf.atomic_write_json(path, payload)
            self.assertEqual(
                mf.load_feed_windows(path, 3, FIXED_MS)["AAAUSDT"]["closed_1m"],
                windows["AAAUSDT"]["closed_1m"],
            )
            self.assertEqual(
                mf.load_feed_state(path)["AAAUSDT"]["venues"], "both"
            )
            restarted = mf.MarketFeed(
                path,
                Path(raw) / "STOP",
                serve=True,
                window=3,
                ms_fn=lambda: FIXED_MS,
            )
            self.assertIsNone(restarted.state["AAAUSDT"]["spot_bid"])
            self.assertEqual(
                restarted.windows["AAAUSDT"]["closed_1m"],
                windows["AAAUSDT"]["closed_1m"],
            )
        urls = mf.websocket_urls(self.state)
        self.assertTrue(any("aaausdt@kline_1m" in url for url in urls["perp"]))
        self.assertFalse(any("aaausdt@kline_1m" in url for url in urls["spot"]))
        self.assertTrue(any("spotusdt@kline_1m" in url for url in urls["spot"]))
        self.assertTrue(mf.argument_parser().parse_args(["--serve"]).serve)

    def test_restart_filters_stale_windows_and_snapshot_reports_window_age(self):
        windows = {
            "AAAUSDT": {
                "closed_1m": [
                    [str(FIXED_MS - 180_001), "1"],
                    [str(FIXED_MS - 120_000), "2"],
                    [str(FIXED_MS - 60_000), "3"],
                ],
                "venue": "perp",
            },
            "SPOTUSDT": {
                "closed_1m": [[str(FIXED_MS - 180_001), "4"]],
                "venue": "spot",
            },
        }
        payload = mf.feed_snapshot_payload(self.state, windows, 3, FIXED_MS - 1)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "feed.json"
            mf.atomic_write_json(path, payload)
            restored = mf.load_feed_windows(path, 3, FIXED_MS)
        self.assertEqual(
            restored["AAAUSDT"]["closed_1m"],
            [
                [str(FIXED_MS - 120_000), "2"],
                [str(FIXED_MS - 60_000), "3"],
            ],
        )
        self.assertNotIn("SPOTUSDT", restored)
        fresh = mf.feed_snapshot_payload(
            self.state, restored, 3, FIXED_MS
        )
        self.assertEqual(fresh["written_ms"], str(FIXED_MS))
        self.assertEqual(fresh["window_oldest_ms"], str(FIXED_MS - 120_000))
        self.assertEqual(fresh["window_newest_ms"], str(FIXED_MS - 60_000))
        self.assertEqual(
            fresh["symbols"]["AAAUSDT"]["window_oldest_ms"],
            str(FIXED_MS - 120_000),
        )
        self.assertEqual(
            fresh["symbols"]["AAAUSDT"]["window_newest_ms"],
            str(FIXED_MS - 60_000),
        )

    def test_combined_closed_kline_message_is_parsed(self):
        data = self.kline("AAAUSDT", 1_000_000, "10")
        stream, rows = mf.parse_combined_message(
            json.dumps({"stream": "aaausdt@kline_1m", "data": data})
        )
        self.assertEqual(stream, "aaausdt@kline_1m")
        self.assertEqual(rows, [data])


class RestartTests(unittest.TestCase):
    def test_default_stop_path_is_runtime_receipt_stop(self):
        expected = mf.RUNTIME_PATHS.receipt_dir / "STOP"
        self.assertEqual(mf.DEFAULT_STOP_PATH, expected)
        self.assertEqual(mf.argument_parser().parse_args([]).stop_file, expected)

    def test_extended_state_round_trips_and_base_state_is_upgraded(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "stream_state.json"
            base = mf.empty_record(FIXED_UTC, "spot")
            base = {field: base[field] for field in mf.STATE_FIELDS}
            base["spot_bid"] = "1"
            base["spot_ask"] = "2"
            path.write_text(json.dumps({"AAAUSDT": base}), encoding="utf-8")
            loaded = mf.load_feed_state(path)
            self.assertEqual(set(loaded["AAAUSDT"]), set(mf.FEED_FIELDS))
            self.assertIsNone(loaded["AAAUSDT"]["spot_step_size"])
            mf.atomic_write_json(path, loaded)
            self.assertEqual(mf.load_feed_state(path), loaded)

    def test_pre_tail_state_restarts_with_unknown_book_age(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "stream_state.json"
            record = mf.empty_record(FIXED_UTC, "spot")
            record = {field: record[field] for field in mf.REQUIRED_STATE_FIELDS}
            record["spot_bid"] = "1"
            record["spot_ask"] = "2"
            path.write_text(json.dumps({"AAAUSDT": record}), encoding="utf-8")

            restarted = mf.MarketFeed(path, root / "STOP")

            self.assertEqual(set(restarted.state["AAAUSDT"]), set(mf.FEED_FIELDS))
            self.assertIsNone(restarted.state["AAAUSDT"]["book_age_ms"])

    def test_existing_stop_file_prevents_reads_connections_and_writes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stop = root / "STOP"
            stop.write_text("stop\n", encoding="utf-8")
            calls = []

            def refused(*args, **kwargs):
                calls.append((args, kwargs))
                raise AssertionError("external action attempted")

            stats = mf.MarketFeed(
                root / "stream_state.json",
                stop,
                reader=refused,
                connector=refused,
                utc_fn=lambda: FIXED_UTC,
            ).run()
            self.assertEqual(calls, [])
            self.assertEqual(stats.tokens_updated, 0)
            self.assertFalse((root / "stream_state.json").exists())


class ReconnectTests(unittest.TestCase):
    def test_bounded_queue_preserves_connection_order_and_latest_book(self):
        def reader(base, unused_path, unused_params):
            symbol = (spot_info("AAAUSDT") if base == mf.SPOT_BASE_URL
                      else perp_info("AAAUSDT"))
            return {"symbols": [symbol]}

        def flood(unused_url, on_text, **callbacks):
            def frame(value):
                return json.dumps({"stream": "!bookTicker", "data": {
                    "s": "AAAUSDT", "b": str(value + 1), "a": str(value + 2),
                    "T": FIXED_MS + value,
                }})

            for value in range(mf.EVENT_QUEUE_MAX):
                on_text(frame(value))
            callbacks["on_attempt"]()
            callbacks["on_connect"]()
            on_text(frame(mf.EVENT_QUEUE_MAX))
            callbacks["on_disconnect"]()
            callbacks["on_attempt"]()
            callbacks["on_connect"]()

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            feed = mf.MarketFeed(root / "feed.json", root / "STOP", serve=True,
                window=3, reader=reader, utc_fn=lambda: FIXED_UTC,
                ms_fn=lambda: FIXED_MS + mf.EVENT_QUEUE_MAX + 2)
            feed.events.subscriptions = {(1, "perp", -1, "AAAUSDT")}
            with mock.patch.object(mf, "run_reconnect_loop", side_effect=flood):
                worker = threading.Thread(target=feed._worker,
                    args=("perp", -1, mf.FAPI_WS_URL, 1, threading.Event()),
                    daemon=True)
                worker.start()
                worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual((feed.events.qsize(), len(feed.events.pending_books),
                              feed.events.maxsize),
                (5, 1,
                 mf.EVENT_QUEUE_MAX + mf.EVENT_QUEUE_STATUS_RESERVE))
            status_queue = mf.EventQueue()
            status = ("attempt", "spot", -1, 1, None, FIXED_MS)
            for unused in range(status_queue.maxsize + 1):
                status_queue.offer(status)
            self.assertEqual((status_queue.qsize(), status_queue.dropped),
                (status_queue.maxsize, {(1, "spot"): 1}))
            feed._start_workers = lambda *unused: []
            stats = feed.run(0.1)
            payload = json.loads((root / "feed.json").read_text(encoding="utf-8"))
        perp = payload["connections"]["perp"]
        self.assertEqual((perp["dropped_frames"], perp["ticker"]["attempts"],
                          perp["ticker"]["successful_handshakes"],
                          perp["ticker"]["last_message_ms"],
                          perp["ticker"]["status"]),
                         (0, 2, 2, None, "stale"))
        self.assertEqual(stats.websocket_messages, 1)
        self.assertEqual((payload["symbols"]["AAAUSDT"]["perp_bid"],
                          payload["symbols"]["AAAUSDT"]["perp_ask"]),
                         (str(mf.EVENT_QUEUE_MAX + 1),
                          str(mf.EVENT_QUEUE_MAX + 2)))

    def test_full_status_queue_keeps_new_cycle_message(self):
        clock = {
            "monotonic": 0.0,
            "ms": FIXED_MS,
            "started": False,
            "injected": False,
        }
        feed_ref = {}

        def monotonic():
            feed = feed_ref.get("feed")
            if (
                feed is not None
                and clock["started"]
                and not clock["injected"]
                and feed.events.qsize() == 0
            ):
                clock["ms"] = FIXED_MS + 200
                feed.events.offer(
                    ("connected", "perp", -1, 1, None, clock["ms"])
                )
                feed.events.offer((
                    "text", "perp", -1, 1,
                    book_frame("AAAUSDT", 20, clock["ms"], "!bookTicker"),
                    clock["ms"],
                ))
                clock["injected"] = True
            value = clock["monotonic"]
            clock["monotonic"] += 0.1
            return value

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            feed = mf.MarketFeed(
                root / "feed.json",
                root / "STOP",
                serve=True,
                window=3,
                metadata_minutes=100,
                flush_seconds=100,
                reader=metadata_reader(perp=("AAAUSDT",)),
                utc_fn=lambda: FIXED_UTC,
                ms_fn=lambda: clock["ms"],
                monotonic_fn=monotonic,
            )
            feed_ref["feed"] = feed
            queue_state = []

            def start_without_socket(unused_urls, generation, unused_stop):
                feed.events.maxsize = 4
                feed.events.subscriptions = {
                    (generation, "perp", -1, "AAAUSDT")
                }
                for event in (
                    ("attempt", "perp", -1, generation, None, FIXED_MS),
                    ("connected", "perp", -1, generation, None, FIXED_MS),
                    ("disconnected", "perp", -1, generation, None, FIXED_MS),
                    ("connected", "spot", 0, generation, None, FIXED_MS),
                ):
                    feed.events.offer(event)
                feed.events.offer((
                    "text", "perp", -1, generation,
                    book_frame("AAAUSDT", 10, FIXED_MS, "!bookTicker"),
                    FIXED_MS,
                ))
                feed.events.offer(
                    ("attempt", "perp", -1, generation, None, FIXED_MS + 1)
                )
                queue_state.extend((
                    feed.events.qsize(),
                    feed.events.maxsize,
                    dict(feed.events.dropped),
                ))
                clock["started"] = True
                return []

            connected_message_times = []
            record_event = feed._record_connection_event

            def observe(kind, venue, shard, message_ms=None):
                record_event(kind, venue, shard, message_ms)
                if kind == "connected" and venue == "perp" and shard == -1:
                    connected_message_times.append(
                        feed.connections["perp"]["ticker"]["shards"][0][
                            "last_message_ms"
                        ]
                    )

            feed._record_connection_event = observe
            feed._start_workers = start_without_socket
            stats = feed.run(0.7)
            payload = json.loads((root / "feed.json").read_text(encoding="utf-8"))
        ticker = payload["connections"]["perp"]["ticker"]
        self.assertEqual(queue_state, [4, 4, {(1, "perp"): 1}])
        self.assertEqual(connected_message_times, [None, None])
        self.assertEqual((stats.reconnects, stats.websocket_messages,
                          payload["connections"]["perp"]["dropped_frames"],
                          payload["symbols"]["AAAUSDT"]["perp_bid"],
                          ticker["last_message_ms"], ticker["status"]),
                         (1, 2, 1, "20", str(FIXED_MS + 200), "healthy"))

    def test_book_coalescing_keeps_each_symbol_fresh_and_open_kline_out(self):
        def reader(base, unused_path, unused_params):
            symbols = ([spot_info("AAAUSDT"), spot_info("BBBUSDT")]
                       if base == mf.SPOT_BASE_URL else [])
            return {"symbols": symbols}

        def book_frame(symbol, value):
            return json.dumps({"stream": f"{symbol.lower()}@bookTicker", "data": {
                "s": symbol, "b": str(value), "a": str(value + 1)}})

        open_kline = json.dumps({"stream": "aaausdt@kline_1m", "data": {"k": {
            "s": "AAAUSDT", "i": "1m", "x": False, "T": FIXED_MS, "q": "1"}}})
        status_kinds = ["attempt", "connected", "disconnected", "attempt", "connected"]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            feed = mf.MarketFeed(root / "feed.json", root / "STOP", serve=True,
                window=3, reader=reader, utc_fn=lambda: FIXED_UTC,
                ms_fn=lambda: FIXED_MS + 10)
            with mock.patch.object(mf, "EVENT_QUEUE_MAX", 2):
                feed.events = mf.EventQueue()
                feed.events.subscriptions = {
                    (1, "spot", -1, "AAAUSDT"),
                    (1, "spot", -2, "BBBUSDT"),
                }
                for cycle in range(6):
                    feed.events.offer(("text", "spot", -1, 1,
                                       book_frame("AAAUSDT", 100 + cycle), FIXED_MS + 10))
                    feed.events.offer(("text", "spot", -2, 1,
                                       book_frame("BBBUSDT", 200 + cycle), FIXED_MS + 10))
                    if cycle < len(status_kinds):
                        feed.events.offer((status_kinds[cycle], "spot", -1, 1,
                                           None, FIXED_MS + 10))
                queued_before = feed.events.qsize()
                feed.events.offer(("text", "spot", 0, 1, open_kline, FIXED_MS + 10))
            self.assertEqual(feed.events.maxsize, 2 + mf.EVENT_QUEUE_STATUS_RESERVE)
            self.assertEqual(feed.events.qsize(), queued_before)
            self.assertNotIn(open_kline, [event[4] for event in feed.events.queue])
            self.assertEqual(set(feed.events.pending_books),
                {(1, "spot", "AAAUSDT"), (1, "spot", "BBBUSDT")})
            self.assertEqual(feed.events.dropped, {})
            observed = []
            record_event = feed._record_connection_event

            def observe(kind, venue, shard, message_ms=None):
                observed.append(kind)
                record_event(kind, venue, shard, message_ms)

            feed._record_connection_event = observe
            feed._start_workers = lambda *unused: []
            stats = feed.run(0.01)
            payload = json.loads((root / "feed.json").read_text(encoding="utf-8"))
        self.assertEqual(observed, status_kinds + ["message", "message"])
        self.assertEqual(stats.websocket_messages, 2)
        self.assertEqual(payload["connections"]["spot"]["dropped_frames"], 0)
        aaa, bbb = payload["symbols"]["AAAUSDT"], payload["symbols"]["BBBUSDT"]
        self.assertEqual((aaa["spot_bid"], aaa["freshness_ms"]), ("105", "0"))
        self.assertEqual((bbb["spot_bid"], bbb["freshness_ms"]), ("205", "0"))

    def test_pre_disconnect_book_does_not_credit_reconnected_cycle(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            feed = mf.MarketFeed(root / "feed.json", root / "STOP", serve=True,
                window=3, reader=metadata_reader(perp=("AAAUSDT",)),
                utc_fn=lambda: FIXED_UTC, ms_fn=lambda: FIXED_MS + 10)
            feed.events.subscriptions = {(1, "perp", -1, "AAAUSDT")}
            events = [
                ("attempt", "perp", -1, 1, None, FIXED_MS),
                ("connected", "perp", -1, 1, None, FIXED_MS + 1),
                ("text", "perp", -1, 1,
                 book_frame("AAAUSDT", 10, FIXED_MS + 2, "!bookTicker"), FIXED_MS + 2),
                ("disconnected", "perp", -1, 1, None, FIXED_MS + 3),
                ("attempt", "perp", -1, 1, None, FIXED_MS + 4),
                ("connected", "perp", -1, 1, None, FIXED_MS + 5),
            ]
            for event in events:
                feed.events.offer(event)
            feed._start_workers = lambda *unused: []
            stats = feed.run(0.01)
            payload = json.loads((root / "feed.json").read_text(encoding="utf-8"))
        ticker = payload["connections"]["perp"]["ticker"]
        self.assertEqual((ticker["attempts"], ticker["successful_handshakes"],
                          ticker["last_message_ms"], ticker["status"]),
                         (2, 2, None, "stale"))
        self.assertEqual((stats.websocket_messages,
                          payload["symbols"]["AAAUSDT"]["perp_bid"]), (1, "10"))

    def test_coalescing_validates_stream_and_perp_source_order(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            feed = mf.MarketFeed(root / "feed.json", root / "STOP", serve=True,
                window=3, reader=metadata_reader(
                    spot=("AAAUSDT", "BBBUSDT"), perp=("BBBUSDT",)),
                utc_fn=lambda: FIXED_UTC, ms_fn=lambda: FIXED_MS + 300)
            feed.events.subscriptions = {
                (1, "spot", -1, "AAAUSDT"),
                (1, "spot", -1, "BBBUSDT"),
                (1, "perp", -1, "BBBUSDT"),
            }
            feed.events.offer(("text", "spot", -1, 1,
                book_frame("BBBUSDT", 20), FIXED_MS))
            feed.events.offer(("text", "spot", -1, 1,
                book_frame("BBBUSDT", 90, stream="aaausdt@bookTicker"), FIXED_MS + 1))
            feed.events.offer(("text", "perp", -1, 1,
                book_frame("BBBUSDT", 200, FIXED_MS + 200, "!bookTicker"), FIXED_MS + 2))
            feed.events.offer(("text", "perp", -1, 1,
                book_frame("BBBUSDT", 100, FIXED_MS + 100, "!bookTicker"), FIXED_MS + 3))
            spot_pending = feed.events.pending_books[(1, "spot", "BBBUSDT")][1]
            perp_pending = feed.events.pending_books[(1, "perp", "BBBUSDT")][1]
            self.assertEqual((json.loads(spot_pending[4])["data"]["b"],
                              json.loads(perp_pending[4])["data"]["b"],
                              feed.events.qsize()), ("20", "200", 1))
            feed._start_workers = lambda *unused: []
            stats = feed.run(0.01)
            payload = json.loads((root / "feed.json").read_text(encoding="utf-8"))
        bbb = payload["symbols"]["BBBUSDT"]
        self.assertEqual((stats.invalid_messages, bbb["spot_bid"], bbb["perp_bid"]),
                         (1, "20", "200"))

    def test_non_ascii_perp_source_uses_receipt_and_worker_continues(self):
        continued = []

        def receive_two(unused_url, on_text, **callbacks):
            callbacks["on_attempt"]()
            callbacks["on_connect"]()
            on_text(book_frame("AAAUSDT", 10, "²", "!bookTicker"))
            continued.append("first")
            on_text(book_frame("BBBUSDT", 20, FIXED_MS + 1, "!bookTicker"))
            continued.append("second")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            feed = mf.MarketFeed(
                root / "feed.json",
                root / "STOP",
                serve=True,
                window=3,
                ms_fn=lambda: FIXED_MS,
            )
            feed.events.subscriptions = {
                (1, "perp", -1, "AAAUSDT"),
                (1, "perp", -1, "BBBUSDT"),
            }
            with mock.patch.object(
                mf, "run_reconnect_loop", side_effect=receive_two
            ):
                feed._worker(
                    "perp", -1, mf.FAPI_WS_URL, 1, threading.Event()
                )
        self.assertEqual(continued, ["first", "second"])
        self.assertEqual(
            (feed.events.pending_books[(1, "perp", "AAAUSDT")][0],
             feed.events.pending_books[(1, "perp", "BBBUSDT")][0]),
            (FIXED_MS, FIXED_MS + 1),
        )

    def test_perp_source_floor_and_reconnect_credit_survive_coalescing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            feed = mf.MarketFeed(root / "feed.json", root / "STOP", serve=True,
                window=3, reader=metadata_reader(perp=("AAAUSDT",)),
                utc_fn=lambda: FIXED_UTC, ms_fn=lambda: FIXED_MS + 400)
            feed._refresh_metadata()
            stream, rows = mf.parse_combined_message(
                book_frame("AAAUSDT", 300, FIXED_MS + 300, "!bookTicker"))
            mf.merge_ticker_rows(feed.state, "perp", stream, rows, FIXED_UTC,
                                 FIXED_MS + 300, feed.seen_ms)
            feed.events.subscriptions = {(1, "perp", -1, "AAAUSDT")}
            for event in [
                ("attempt", "perp", -1, 1, None, FIXED_MS),
                ("connected", "perp", -1, 1, None, FIXED_MS + 1),
                ("text", "perp", -1, 1,
                 book_frame("AAAUSDT", 200, FIXED_MS + 200, "!bookTicker"), FIXED_MS + 2),
                ("disconnected", "perp", -1, 1, None, FIXED_MS + 3),
                ("attempt", "perp", -1, 1, None, FIXED_MS + 4),
                ("connected", "perp", -1, 1, None, FIXED_MS + 5),
                ("text", "perp", -1, 1,
                 book_frame("AAAUSDT", 100, FIXED_MS + 100, "!bookTicker"), FIXED_MS + 6),
            ]:
                feed.events.offer(event)
            retained = feed.events.pending_books[(1, "perp", "AAAUSDT")][1]
            credited = feed.events.pending_messages[(1, "perp", -1)]
            self.assertEqual((json.loads(retained[4])["data"]["b"],
                              json.loads(credited[4])["data"]["b"]), ("200", "100"))
            feed._start_workers = lambda *unused: []
            stats = feed.run(0.01)
            payload = json.loads((root / "feed.json").read_text(encoding="utf-8"))
        ticker = payload["connections"]["perp"]["ticker"]
        self.assertEqual((payload["symbols"]["AAAUSDT"]["perp_bid"],
                          feed.seen_ms["AAAUSDT"]["perp"],
                          ticker["last_message_ms"], ticker["status"],
                          stats.websocket_messages),
                         ("300", FIXED_MS + 300, str(FIXED_MS + 400), "healthy", 1))

    def test_metadata_restart_accounts_pending_book_and_admits_joiner(self):
        spot_reads = {"count": 0}

        def reader(base, unused_path, unused_params):
            if base != mf.SPOT_BASE_URL:
                return {"symbols": []}
            spot_reads["count"] += 1
            symbols = ("AAAUSDT",) if spot_reads["count"] == 1 else ("AAAUSDT", "BBBUSDT")
            return {"symbols": [spot_info(symbol) for symbol in symbols]}

        ticks = iter((0.0, 0.0, 0.0, 2.0, 2.5, 4.0, 5.0))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            feed = mf.MarketFeed(root / "feed.json", root / "STOP", serve=True,
                window=3, metadata_minutes=1 / 60, flush_seconds=100,
                reader=reader, utc_fn=lambda: FIXED_UTC,
                ms_fn=lambda: FIXED_MS, monotonic_fn=lambda: next(ticks))
            start_workers = feed._start_workers
            generations = []

            def start_without_sockets(urls, generation, worker_stop):
                generations.append(generation)
                with mock.patch.object(threading.Thread, "start"):
                    start_workers(urls, generation, worker_stop)
                if generation == 1:
                    feed.events.offer(("text", "spot", -1, 1,
                                       book_frame("AAAUSDT", 10), FIXED_MS))
                else:
                    feed.events.offer(("attempt", "spot", -1, generation,
                                       None, FIXED_MS))
                return []

            feed._start_workers = start_without_sockets
            stats = feed.run(3.0)
            payload = json.loads((root / "feed.json").read_text(encoding="utf-8"))
        self.assertEqual((generations, stats.metadata_refreshes,
                          payload["connections"]["spot"]["dropped_frames"]),
                         ([1, 2], 2, 1))
        self.assertIn("BBBUSDT", payload["symbols"])
        self.assertIn((2, "spot", -1, "BBBUSDT"), feed.events.subscriptions)
        self.assertEqual(feed.events.pending_books, {})

    def test_stale_generation_drop_reaches_venue_total(self):
        spot_reads = {"count": 0}

        def reader(base, unused_path, unused_params):
            if base != mf.SPOT_BASE_URL:
                return {"symbols": []}
            spot_reads["count"] += 1
            symbols = (
                ("AAAUSDT",)
                if spot_reads["count"] == 1
                else ("AAAUSDT", "BBBUSDT")
            )
            return {"symbols": [spot_info(symbol) for symbol in symbols]}

        ticks = iter((0.0, 0.0, 0.0, 2.0, 2.5, 4.0, 5.0))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            feed = mf.MarketFeed(
                root / "feed.json",
                root / "STOP",
                serve=True,
                window=3,
                metadata_minutes=1 / 60,
                flush_seconds=100,
                reader=reader,
                utc_fn=lambda: FIXED_UTC,
                ms_fn=lambda: FIXED_MS,
                monotonic_fn=lambda: next(ticks),
            )
            start_workers = feed._start_workers

            def start_without_sockets(urls, generation, worker_stop):
                with mock.patch.object(threading.Thread, "start"):
                    start_workers(urls, generation, worker_stop)
                if generation == 2:
                    feed.events.offer((
                        "text", "spot", -1, 2,
                        book_frame("AAAUSDT", 20), FIXED_MS + 2,
                    ))
                    feed.events.offer((
                        "text", "spot", -1, 1,
                        book_frame("AAAUSDT", 5), FIXED_MS + 1,
                    ))
                return []

            feed._start_workers = start_without_sockets
            stats = feed.run(3.0)
            payload = json.loads((root / "feed.json").read_text(encoding="utf-8"))
        self.assertEqual((stats.websocket_messages,
                          payload["symbols"]["AAAUSDT"]["spot_bid"],
                          payload["connections"]["spot"]["dropped_frames"]),
                         (1, "20", 1))

    def test_unsubscribed_shard_book_cannot_replace_subscribed_book(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            feed = mf.MarketFeed(root / "feed.json", root / "STOP", serve=True,
                window=3, utc_fn=lambda: FIXED_UTC, ms_fn=lambda: FIXED_MS)
            feed.state = {"AAAUSDT": mf.empty_record(FIXED_UTC, "spot")}
            feed.state.update({f"S{index:03d}USDT": mf.empty_record(FIXED_UTC, "spot")
                               for index in range(mf.MAX_STREAMS_PER_CONNECTION)})
            with mock.patch.object(threading.Thread, "start"):
                feed._start_workers(mf.websocket_urls(feed.state), 1, threading.Event())
            self.assertIn((1, "spot", -1, "AAAUSDT"), feed.events.subscriptions)
            self.assertNotIn((1, "spot", -2, "AAAUSDT"), feed.events.subscriptions)
            feed.events.offer(("text", "spot", -1, 1,
                               book_frame("AAAUSDT", 10), FIXED_MS))
            feed.events.offer(("text", "spot", -2, 1,
                               book_frame("AAAUSDT", 5), FIXED_MS + 1))
            pending = feed.events.pending_books[(1, "spot", "AAAUSDT")][1]
        self.assertEqual((json.loads(pending[4])["data"]["b"],
                          set(feed.events.pending_messages), feed.events.dropped),
                         ("10", {(1, "spot", -1)}, {(1, "spot"): 1}))

    def test_spot_queue_residence_preserves_receipt_ms(self):
        clock = {"ms": FIXED_MS}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            feed = mf.MarketFeed(root / "feed.json", root / "STOP", serve=True,
                window=3, reader=metadata_reader(spot=("AAAUSDT",)),
                utc_fn=lambda: FIXED_UTC, ms_fn=lambda: clock["ms"])
            feed.events.subscriptions = {(1, "spot", -1, "AAAUSDT")}

            def emit_one(unused_url, on_text, **callbacks):
                callbacks["on_attempt"]()
                callbacks["on_connect"]()
                on_text(book_frame("AAAUSDT", 30))

            with mock.patch.object(mf, "run_reconnect_loop", side_effect=emit_one):
                feed._worker("spot", -1, mf.SPOT_WS_URL, 1, threading.Event())
            clock["ms"] += 250
            feed._start_workers = lambda *unused: []
            feed.run(0.01)
            payload = json.loads((root / "feed.json").read_text(encoding="utf-8"))
        ticker = payload["connections"]["spot"]["ticker"]
        self.assertEqual(feed.seen_ms["AAAUSDT"]["spot"], FIXED_MS)
        self.assertEqual((payload["symbols"]["AAAUSDT"]["freshness_ms"],
                          ticker["last_message_ms"]), ("250", str(FIXED_MS + 250)))

    def test_unsubscribed_books_are_bounded_and_accounted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            feed = mf.MarketFeed(root / "feed.json", root / "STOP", serve=True,
                window=3, reader=metadata_reader(spot=("AAAUSDT",)),
                utc_fn=lambda: FIXED_UTC, ms_fn=lambda: FIXED_MS)
            feed._refresh_metadata()
            with mock.patch.object(threading.Thread, "start"):
                feed._start_workers(mf.websocket_urls(feed.state), 1, threading.Event())
            for index in range(3):
                symbol = f"X{index:03d}USDT"
                feed.events.offer(("text", "spot", -1, 1,
                                   book_frame(symbol, index + 1), FIXED_MS + index))
            self.assertEqual((feed.events.pending_books, feed.events.qsize(),
                              feed.events.dropped), ({}, 0, {(1, "spot"): 3}))
            feed._start_workers = lambda *unused: []
            feed.run(0.01)
            payload = json.loads((root / "feed.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["connections"]["spot"]["dropped_frames"], 3)

    def test_exponential_reconnect_with_injected_clients(self):
        stopped = {"value": False}
        waits = []
        connects = []
        attempts = []
        failures = []

        class ClosingClient:
            def __init__(self, message=None):
                self.message = message
                self.closed = False

            def recv_text(self):
                if self.message is None:
                    raise mf.WebSocketClosed("injected close")
                message, self.message = self.message, None
                return message

            def close(self):
                self.closed = True

        closing = ClosingClient()
        final = ClosingClient("payload")
        outcomes = [OSError("injected connect failure"), closing, final]

        def connector(url):
            connects.append(url)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def on_text(text):
            self.assertEqual(text, "payload")
            stopped["value"] = True

        mf.run_reconnect_loop(
            "wss://example.invalid/stream",
            on_text,
            connector=connector,
            stop_requested=lambda: stopped["value"],
            wait_fn=lambda delay: waits.append(delay) or False,
            on_attempt=lambda: attempts.append(True),
            on_connect_failure=lambda: failures.append(True),
            initial_backoff=1,
            maximum_backoff=8,
        )
        self.assertEqual(len(connects), 3)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(failures), 1)
        self.assertEqual(waits, [1, 2])
        self.assertTrue(closing.closed)
        self.assertTrue(final.closed)

    def test_connection_truth_reports_ticker_silence_and_shard_failures(self):
        with tempfile.TemporaryDirectory() as raw:
            feed = mf.MarketFeed(Path(raw) / "feed.json", Path(raw) / "STOP",
                serve=True, window=3, ms_fn=lambda: FIXED_MS,
            )
            state = {f"S{index:04d}USDT": mf.empty_record(FIXED_UTC, "both")
                     for index in range(mf.MAX_STREAMS_PER_CONNECTION + 1)}
            urls = mf.websocket_urls(state)
            feed._reset_connections(urls)
            feed.connections["spot"]["dropped_frames"] = 7
            feed._reset_connections(urls)
            self.assertEqual(feed.connections["spot"]["dropped_frames"], 7)
            self.assertEqual(set(feed.connections), {"spot", "perp"})
            for venue in feed.connections:
                ticker = feed.connections[venue]["ticker"]
                self.assertEqual((ticker["status"], ticker["attempts"],
                                  ticker["successful_handshakes"],
                                  ticker["last_message_ms"]), ("broken", 0, 0, None))
                for index in range(len(ticker["shards"])):
                    feed._record_connection_event("attempt", venue, -index - 1)
                    feed._record_connection_event("connected", venue, -index - 1)
            old_ms = FIXED_MS - mf.TICKER_STALE_MS - 1
            feed._record_connection_event("message", "spot", -1, old_ms)
            feed._record_connection_event("message", "spot", -2, FIXED_MS)
            feed._record_connection_event("message", "perp", -1, old_ms)
            for unused in range(3):
                feed._record_connection_event("attempt", "perp", 0)
                feed._record_connection_event("handshake_failed", "perp", 0)
            payload = mf.feed_snapshot_payload({}, {}, 3, FIXED_MS, feed.connections)
        spot = payload["connections"]["spot"]
        self.assertEqual((spot["expected_connections"], spot["ticker"]["status"],
            spot["ticker"]["attempts"], spot["ticker"]["successful_handshakes"],
            spot["ticker"]["last_message_ms"], spot["status"]),
            (2, "stale", 2, 2, str(FIXED_MS), "degraded"))
        self.assertEqual([row["status"] for row in spot["ticker"]["shards"]],
                         ["stale", "healthy"])
        perp = payload["connections"]["perp"]
        self.assertEqual((perp["expected_connections"], perp["ticker"]["status"],
            perp["ticker"]["attempts"], perp["ticker"]["last_message_ms"]),
            (3, "stale", 1, str(old_ms)))
        self.assertEqual((perp["handshake_failures"], perp["reconnects"]), (3, 2))
        self.assertEqual((perp["shards"][0]["handshake_failures"],
                          perp["shards"][0]["reconnects"]), (3, 2))

    def test_socket_timeout_checks_stop_without_reconnect(self):
        stopped = {"value": False}
        connects = []

        class TimingOutClient:
            def recv_text(self):
                stopped["value"] = True
                raise socket.timeout()

            def close(self):
                pass

        mf.run_reconnect_loop(
            "wss://example.invalid/stream",
            lambda text: None,
            connector=lambda url: connects.append(url) or TimingOutClient(),
            stop_requested=lambda: stopped["value"],
            wait_fn=lambda delay: False,
        )
        self.assertEqual(len(connects), 1)


if __name__ == "__main__":
    unittest.main()
