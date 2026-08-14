#!/usr/bin/env python3
"""Offline tests for the continuous all-token Binance market feed."""

import base64
import hashlib
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

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
    def test_futures_combined_url_uses_market_stream_route(self):
        self.assertEqual(
            mf.FAPI_WS_URL,
            "wss://fstream.binance.com/market/stream"
            "?streams=!ticker@arr/!miniTicker@arr",
        )

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
        client = mf.RFC6455Client.connect(
            mf.SPOT_WS_URL,
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
            "GET /stream?streams=!ticker@arr/!miniTicker@arr HTTP/1.1", request
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


class RecordMergeTests(unittest.TestCase):
    def setUp(self):
        spot = mf.parse_exchange_metadata(
            {"symbols": [spot_info("AAAUSDT"), spot_info("SPOTUSDT")]}, "spot"
        )
        perp = mf.parse_exchange_metadata(
            {"symbols": [perp_info("AAAUSDT"), perp_info("PERPUSDT")]}, "perp"
        )
        self.state = mf.merge_metadata({}, spot, perp, FIXED_UTC)
        self.seen = {}

    def test_spot_and_perp_tickers_merge_without_rotation_and_compute_basis(self):
        spot_rows = [
            {"s": "AAAUSDT", "b": "10", "a": "11", "E": FIXED_MS - 5},
            {"s": "SPOTUSDT", "b": "20", "a": "21", "E": FIXED_MS - 7},
            {"s": "UNKNOWNUSDT", "b": "1", "a": "2", "E": FIXED_MS},
        ]
        changed = mf.merge_ticker_rows(
            self.state,
            "spot",
            "!ticker@arr",
            spot_rows,
            FIXED_UTC,
            FIXED_MS,
            self.seen,
        )
        self.assertEqual(changed, {"AAAUSDT", "SPOTUSDT"})
        changed = mf.merge_ticker_rows(
            self.state,
            "perp",
            "!ticker@arr",
            [{"s": "AAAUSDT", "b": "12", "a": "13", "E": FIXED_MS - 9}],
            FIXED_UTC,
            FIXED_MS,
            self.seen,
        )
        self.assertEqual(changed, {"AAAUSDT"})
        record = self.state["AAAUSDT"]
        self.assertEqual((record["spot_bid"], record["perp_ask"]), ("10", "13"))
        self.assertEqual(
            record["basis_gap_pct"],
            "9.0909090909090909090909090909090909090909090909091",
        )
        self.assertEqual(record["freshness_ms"], "9")

    def test_mini_ticker_is_not_mislabeled_as_book_or_one_minute_volume(self):
        before = json.loads(json.dumps(self.state))
        changed = mf.merge_ticker_rows(
            self.state,
            "spot",
            "!miniTicker@arr",
            [{"s": "AAAUSDT", "c": "10", "q": "999", "E": FIXED_MS}],
            FIXED_UTC,
            FIXED_MS,
            self.seen,
        )
        self.assertEqual(changed, set())
        self.assertEqual(self.state, before)

    def test_crossed_or_nonpositive_book_fails_closed(self):
        changed = mf.merge_ticker_rows(
            self.state,
            "spot",
            "!ticker@arr",
            [{"s": "AAAUSDT", "b": "3", "a": "2", "E": FIXED_MS}],
            FIXED_UTC,
            FIXED_MS,
            self.seen,
        )
        self.assertEqual(changed, {"AAAUSDT"})
        self.assertIsNone(self.state["AAAUSDT"]["spot_bid"])
        self.assertIsNone(self.state["AAAUSDT"]["spot_ask"])


class RestartTests(unittest.TestCase):
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
    def test_exponential_reconnect_with_injected_clients(self):
        stopped = {"value": False}
        waits = []
        connects = []

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
            initial_backoff=1,
            maximum_backoff=8,
        )
        self.assertEqual(len(connects), 3)
        self.assertEqual(waits, [1, 2])
        self.assertTrue(closing.closed)
        self.assertTrue(final.closed)

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
