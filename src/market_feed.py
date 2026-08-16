#!/usr/bin/env python3
"""Continuous public Binance Spot + USD-M USDT market feed.

Dedicated sockets consume Spot ``!ticker`` and USD-M ``!bookTicker`` books;
shards consume closed ``@kline_1m`` candles without rotation.  Unsigned
``exchangeInfo`` reads provide size constraints while the flat state preserves
:mod:`market_stream`; no signed or account path exists here.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import queue
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from market_stream import (
    DEFAULT_WINDOW,
    DataReadError,
    FEED_STATE_VERSION,
    MAX_WINDOW,
    MarketStreamError,
    NUMERIC_FIELDS,
    PublicJsonReader,
    STATE_FIELDS,
    STATE_PREFIX_FIELDS,
    SYMBOL_PATTERN,
    VENUE_VALUES,
    atomic_write_json,
    decimal_string,
    decimal_value,
    merge_closed_window,
    optional_decimal_string,
    volume_metrics,
)
from runtime_paths import PATHS as RUNTIME_PATHS


SPOT_BASE_URL = "https://api.binance.com"
FAPI_BASE_URL = "https://fapi.binance.com"
SPOT_EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
FAPI_EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"

SPOT_WS_URL = (
    "wss://stream.binance.com:9443/stream"
    "?streams=!ticker@arr"
)
FAPI_WS_URL = (
    "wss://fstream.binance.com/market/stream"
    "?streams=!bookTicker"
)

DEFAULT_STATE_PATH = Path("stream_state.json")
DEFAULT_STOP_PATH = RUNTIME_PATHS.stop
DEFAULT_METADATA_MINUTES = 30.0
DEFAULT_FLUSH_SECONDS = 1.0
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_SOCKET_TIMEOUT = 1.0
DEFAULT_BACKOFF_INITIAL = 1.0
DEFAULT_BACKOFF_MAX = 30.0
MAX_FRAME_BYTES = 32_000_000
MAX_HANDSHAKE_BYTES = 64_000
MAX_STREAMS_PER_CONNECTION = 200
EVENT_QUEUE_MAX = 4096
EVENT_QUEUE_STATUS_RESERVE = 256
TICKER_STALE_MS = 15_000
KLINE_INTERVAL_MS = 60_000
RFC6455_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
FeedEvent = Tuple[str, str, int, int, Optional[str]]

METADATA_FIELDS = (
    "spot_min_qty",
    "spot_step_size",
    "spot_min_notional",
    "perp_min_qty",
    "perp_step_size",
    "perp_min_notional",
)
FEED_FIELDS = STATE_FIELDS + METADATA_FIELDS
REQUIRED_STATE_FIELDS = STATE_PREFIX_FIELDS
UNSUPPORTED_STREAM_FIELDS = (
    "mark",
    "vol_1m_quote",
    "vol_med_1m",
    "surge_ratio",
    "oi",
    "oi_change_pct",
    "funding_rate",
    "next_funding_ms",
)
TICKER_STREAM_NAMES = ("!ticker@arr", "!bookTicker")
STREAM_NAMES = frozenset(name.lower() for name in TICKER_STREAM_NAMES)
KLINE_STREAM_SUFFIX = "@kline_1m"
MAX_KLINES_PER_CONNECTION = MAX_STREAMS_PER_CONNECTION - len(TICKER_STREAM_NAMES)
WINDOW_SNAPSHOT_FIELDS = (
    "window_venue",
    "window_oldest_ms",
    "window_newest_ms",
)


class WebSocketError(MarketStreamError):
    """A transport, handshake, or RFC6455 protocol failure."""


class WebSocketClosed(WebSocketError):
    """The peer closed the WebSocket connection."""


class WebSocketProtocolError(WebSocketError):
    """The peer sent a frame that violates the supported RFC6455 subset."""


class EventQueue(queue.Queue[FeedEvent]):
    def __init__(self) -> None:
        super().__init__(maxsize=EVENT_QUEUE_MAX + EVENT_QUEUE_STATUS_RESERVE)
        self.dropped: Dict[Tuple[int, str], int] = {}

    def offer(self, event: FeedEvent) -> None:
        with self.not_full:
            limit = EVENT_QUEUE_MAX if event[0] == "text" else self.maxsize
            if self._qsize() < limit:
                self._put(event)
                self.unfinished_tasks += 1
                self.not_empty.notify()
                return
            if event[0] == "text":
                for offset, queued in enumerate(reversed(self.queue)):
                    if queued[0] == "text" and queued[1:4] == event[1:4]:
                        del self.queue[-offset - 1]
                        self._put(event)
                        break
            key = (event[3], event[1])
            self.dropped[key] = self.dropped.get(key, 0) + 1


@dataclass(frozen=True)
class WebSocketFrame:
    fin: bool
    opcode: int
    payload: bytes
    masked: bool


@dataclass(frozen=True)
class FeedStats:
    started_utc: str
    ended_utc: str
    elapsed_seconds: float
    symbols_total: int
    tokens_updated: int
    ticker_updates: int
    websocket_messages: int
    metadata_refreshes: int
    reconnects: int
    invalid_messages: int
    handshake_failures: int = 0
    broken_venues: Tuple[str, ...] = ()

    @property
    def update_rate(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.ticker_updates / self.elapsed_seconds


def now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000


def valid_symbol(raw: Any) -> Optional[str]:
    """Return one stream-compatible USDT symbol, otherwise fail closed."""
    if not isinstance(raw, str):
        return None
    if not raw.endswith("USDT") or SYMBOL_PATTERN.fullmatch(raw) is None:
        return None
    return raw


def encode_frame(
    opcode: int,
    payload: bytes = b"",
    *,
    fin: bool = True,
    masked: bool = True,
    mask_key: Optional[bytes] = None,
) -> bytes:
    """Encode one RFC6455 frame; client calls use masking."""
    if opcode not in {0x0, 0x1, 0x2, 0x8, 0x9, 0xA}:
        raise ValueError("unsupported WebSocket opcode")
    if opcode >= 0x8 and (not fin or len(payload) > 125):
        raise ValueError("invalid WebSocket control frame")
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("WebSocket payload exceeds byte limit")
    first = (0x80 if fin else 0) | opcode
    mask_bit = 0x80 if masked else 0
    length = len(payload)
    if length < 126:
        header = bytes((first, mask_bit | length))
    elif length <= 0xFFFF:
        header = bytes((first, mask_bit | 126)) + struct.pack("!H", length)
    else:
        header = bytes((first, mask_bit | 127)) + struct.pack("!Q", length)
    if not masked:
        return header + payload
    key = secrets.token_bytes(4) if mask_key is None else mask_key
    if len(key) != 4:
        raise ValueError("WebSocket mask key must be four bytes")
    masked_payload = bytes(value ^ key[index % 4] for index, value in enumerate(payload))
    return header + key + masked_payload


def parse_frame(
    buffer: bytes, *, max_payload: int = MAX_FRAME_BYTES
) -> Tuple[Optional[WebSocketFrame], bytes]:
    """Parse at most one frame, preserving an incomplete buffer unchanged."""
    if len(buffer) < 2:
        return None, buffer
    first, second = buffer[0], buffer[1]
    if first & 0x70:
        raise WebSocketProtocolError("WebSocket RSV bits are unsupported")
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    if opcode not in {0x0, 0x1, 0x2, 0x8, 0x9, 0xA}:
        raise WebSocketProtocolError("unsupported WebSocket opcode")
    masked = bool(second & 0x80)
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < offset + 2:
            return None, buffer
        length = struct.unpack("!H", buffer[offset : offset + 2])[0]
        offset += 2
    elif length == 127:
        if len(buffer) < offset + 8:
            return None, buffer
        length = struct.unpack("!Q", buffer[offset : offset + 8])[0]
        offset += 8
        if length & (1 << 63):
            raise WebSocketProtocolError("invalid WebSocket payload length")
    if length > max_payload:
        raise WebSocketProtocolError("WebSocket payload exceeds byte limit")
    if opcode >= 0x8 and (not fin or length > 125):
        raise WebSocketProtocolError("invalid WebSocket control frame")
    mask_length = 4 if masked else 0
    end = offset + mask_length + length
    if len(buffer) < end:
        return None, buffer
    mask_key = buffer[offset : offset + 4] if masked else b""
    offset += mask_length
    payload = buffer[offset : offset + length]
    if masked:
        payload = bytes(
            value ^ mask_key[index % 4] for index, value in enumerate(payload)
        )
    return WebSocketFrame(fin, opcode, payload, masked), buffer[end:]


class RFC6455Client:
    """Minimal TLS WebSocket client for text, ping/pong, and close frames."""

    def __init__(self, transport: Any, initial: bytes = b""):
        self.transport = transport
        self._buffer = initial
        self._fragments: List[bytes] = []
        self._fragment_opcode: Optional[int] = None
        self._closed = False
        self._send_lock = threading.Lock()

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        socket_timeout: float = DEFAULT_SOCKET_TIMEOUT,
        socket_factory: Callable[..., Any] = socket.create_connection,
        ssl_context: Optional[ssl.SSLContext] = None,
        key_bytes: Optional[bytes] = None,
    ) -> "RFC6455Client":
        parts = urlsplit(url)
        if parts.scheme != "wss" or not parts.hostname or parts.username is not None:
            raise WebSocketError("only host-based wss URLs are supported")
        port = parts.port or 443
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        host_header = parts.hostname if port == 443 else f"{parts.hostname}:{port}"
        nonce = secrets.token_bytes(16) if key_bytes is None else key_bytes
        if len(nonce) != 16:
            raise ValueError("WebSocket handshake key must be sixteen bytes")
        key = base64.b64encode(nonce).decode("ascii")
        request = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: quant-market-feed/1\r\n"
            "\r\n"
        ).encode("ascii")
        raw_socket: Any = None
        transport: Any = None
        try:
            raw_socket = socket_factory((parts.hostname, port), timeout=connect_timeout)
            context = ssl.create_default_context() if ssl_context is None else ssl_context
            transport = context.wrap_socket(raw_socket, server_hostname=parts.hostname)
            transport.settimeout(connect_timeout)
            transport.sendall(request)
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = transport.recv(4096)
                if not chunk:
                    raise WebSocketError("WebSocket handshake closed early")
                response.extend(chunk)
                if len(response) > MAX_HANDSHAKE_BYTES:
                    raise WebSocketError("WebSocket handshake exceeded byte limit")
            head, initial = bytes(response).split(b"\r\n\r\n", 1)
            try:
                lines = head.decode("iso-8859-1").split("\r\n")
            except UnicodeDecodeError:
                raise WebSocketError("invalid WebSocket handshake headers") from None
            if not lines or not lines[0].startswith("HTTP/1.1 101 "):
                raise WebSocketError("WebSocket upgrade was not accepted")
            headers: Dict[str, str] = {}
            for line in lines[1:]:
                if ":" not in line:
                    raise WebSocketError("invalid WebSocket handshake header")
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
            expected = base64.b64encode(
                hashlib.sha1((key + RFC6455_GUID).encode("ascii")).digest()
            ).decode("ascii")
            connection_tokens = {
                token.strip().lower()
                for token in headers.get("connection", "").split(",")
            }
            if (
                headers.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in connection_tokens
                or headers.get("sec-websocket-accept") != expected
            ):
                raise WebSocketError("invalid WebSocket upgrade response")
            transport.settimeout(socket_timeout)
            return cls(transport, initial)
        except BaseException:
            candidate = transport if transport is not None else raw_socket
            if candidate is not None:
                try:
                    candidate.close()
                except OSError:
                    pass
            raise

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        frame = encode_frame(opcode, payload, masked=True)
        with self._send_lock:
            if self._closed:
                raise WebSocketClosed("WebSocket is closed")
            self.transport.sendall(frame)

    def recv_frame(self) -> WebSocketFrame:
        while True:
            frame, remaining = parse_frame(self._buffer)
            if frame is not None:
                self._buffer = remaining
                if frame.masked:
                    raise WebSocketProtocolError("server WebSocket frame was masked")
                return frame
            chunk = self.transport.recv(65536)
            if not chunk:
                raise WebSocketClosed("WebSocket peer closed transport")
            self._buffer += chunk
            if len(self._buffer) > MAX_FRAME_BYTES + 14:
                raise WebSocketProtocolError("WebSocket buffered data exceeds byte limit")

    def recv_text(self) -> str:
        while True:
            frame = self.recv_frame()
            if frame.opcode == 0x9:
                self.send_frame(0xA, frame.payload)
                continue
            if frame.opcode == 0xA:
                continue
            if frame.opcode == 0x8:
                if len(frame.payload) == 1:
                    raise WebSocketProtocolError("invalid WebSocket close payload")
                try:
                    if not self._closed:
                        self.send_frame(0x8, frame.payload)
                finally:
                    self.close_transport()
                raise WebSocketClosed("WebSocket peer sent close")
            if frame.opcode == 0x2:
                raise WebSocketProtocolError("binary WebSocket messages are unsupported")
            if frame.opcode == 0x1:
                if self._fragment_opcode is not None:
                    raise WebSocketProtocolError("new data frame during fragmentation")
                if frame.fin:
                    payload = frame.payload
                else:
                    self._fragment_opcode = frame.opcode
                    self._fragments = [frame.payload]
                    continue
            elif frame.opcode == 0x0:
                if self._fragment_opcode != 0x1:
                    raise WebSocketProtocolError("unexpected continuation frame")
                self._fragments.append(frame.payload)
                if sum(map(len, self._fragments)) > MAX_FRAME_BYTES:
                    raise WebSocketProtocolError("fragmented message exceeds byte limit")
                if not frame.fin:
                    continue
                payload = b"".join(self._fragments)
                self._fragments = []
                self._fragment_opcode = None
            else:
                continue
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                raise WebSocketProtocolError("WebSocket text is not UTF-8") from None

    def close_transport(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.transport.close()
        except OSError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.send_frame(0x8, struct.pack("!H", 1000))
        except (OSError, WebSocketError):
            pass
        self.close_transport()


def connect_websocket(url: str) -> RFC6455Client:
    return RFC6455Client.connect(url)


def _metadata_decimal(raw: Any, source: str) -> Optional[str]:
    try:
        value = decimal_value(raw, source)
    except DataReadError:
        return None
    if value <= 0:
        return None
    return decimal_string(value)


def _filter_row(filters: Any, filter_type: str) -> Optional[Mapping[str, Any]]:
    if not isinstance(filters, list):
        return None
    for row in filters:
        if isinstance(row, dict) and row.get("filterType") == filter_type:
            return row
    return None


def parse_exchange_metadata(payload: Any, venue: str) -> Dict[str, Dict[str, Optional[str]]]:
    """Parse one exchangeInfo response into venue-prefixed Decimal text."""
    if venue not in {"spot", "perp"}:
        raise ValueError("venue must be spot or perp")
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise DataReadError(f"{venue} exchangeInfo symbols is not a list")
    result: Dict[str, Dict[str, Optional[str]]] = {}
    prefix = "spot" if venue == "spot" else "perp"
    for row in payload["symbols"]:
        if not isinstance(row, dict) or row.get("status") != "TRADING":
            continue
        if row.get("quoteAsset") not in {None, "USDT"}:
            continue
        if venue == "spot" and row.get("isSpotTradingAllowed") is False:
            continue
        if venue == "perp" and row.get("contractType") != "PERPETUAL":
            continue
        symbol = valid_symbol(row.get("symbol"))
        if symbol is None:
            continue
        if symbol in result:
            raise DataReadError(f"{venue} exchangeInfo repeats {symbol}")
        lot = _filter_row(row.get("filters"), "LOT_SIZE")
        notional = _filter_row(row.get("filters"), "NOTIONAL")
        if notional is None:
            notional = _filter_row(row.get("filters"), "MIN_NOTIONAL")
        result[symbol] = {
            f"{prefix}_min_qty": _metadata_decimal(
                None if lot is None else lot.get("minQty"),
                f"{venue} {symbol} LOT_SIZE.minQty",
            ),
            f"{prefix}_step_size": _metadata_decimal(
                None if lot is None else lot.get("stepSize"),
                f"{venue} {symbol} LOT_SIZE.stepSize",
            ),
            f"{prefix}_min_notional": _metadata_decimal(
                None
                if notional is None
                else notional.get("minNotional", notional.get("notional")),
                f"{venue} {symbol} minNotional",
            ),
        }
    return result


def empty_record(utc: str, venues: str) -> Dict[str, Any]:
    record = {field: None for field in FEED_FIELDS}
    record["utc"] = utc
    record["venues"] = venues
    return record


def _basis_gap(record: Mapping[str, Any]) -> Optional[str]:
    if record.get("venues") != "both":
        return None
    spot_ask = record.get("spot_ask")
    perp_bid = record.get("perp_bid")
    if spot_ask is None or perp_bid is None:
        return None
    ask = decimal_value(spot_ask, "spot ask")
    bid = decimal_value(perp_bid, "perpetual bid")
    if ask <= 0:
        return None
    with localcontext() as context:
        context.prec = 50
        return decimal_string((bid - ask) / ask * Decimal(100))


def merge_metadata(
    state: Mapping[str, Mapping[str, Any]],
    spot: Mapping[str, Mapping[str, Optional[str]]],
    perp: Mapping[str, Mapping[str, Optional[str]]],
    utc: str,
) -> Dict[str, Dict[str, Any]]:
    """Merge a complete metadata snapshot without erasing retained prices."""
    merged: Dict[str, Dict[str, Any]] = {}
    for symbol in sorted(set(spot) | set(perp)):
        venues = "both" if symbol in spot and symbol in perp else (
            "spot" if symbol in spot else "perp"
        )
        if symbol in state:
            record = {field: state[symbol].get(field) for field in FEED_FIELDS}
            if not isinstance(record.get("utc"), str):
                record["utc"] = utc
        else:
            record = empty_record(utc, venues)
        record["venues"] = venues
        for field in METADATA_FIELDS:
            record[field] = None
        if symbol in spot:
            record.update(spot[symbol])
        else:
            record["spot_bid"] = None
            record["spot_ask"] = None
        if symbol in perp:
            record.update(perp[symbol])
        else:
            record["perp_bid"] = None
            record["perp_ask"] = None
        for field in UNSUPPORTED_STREAM_FIELDS:
            record[field] = None
        record["basis_gap_pct"] = _basis_gap(record)
        merged[symbol] = {field: record[field] for field in FEED_FIELDS}
    return merged


def load_feed_state(path: Path) -> Dict[str, Dict[str, Any]]:
    """Load either the base market_stream schema or this schema extension."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DataReadError(f"stream state is unreadable: {path}") from None
    if not isinstance(payload, dict):
        raise DataReadError("stream state is not a symbol mapping")
    envelope = payload.get("version") == FEED_STATE_VERSION and "symbols" in payload
    if envelope:
        payload = payload.get("symbols")
        if not isinstance(payload, dict):
            raise DataReadError("feed state symbols is not a mapping")
    state: Dict[str, Dict[str, Any]] = {}
    allowed = set(FEED_FIELDS)
    required = set(REQUIRED_STATE_FIELDS)
    for raw_symbol, raw_record in payload.items():
        symbol = valid_symbol(raw_symbol)
        if symbol is None or not isinstance(raw_record, dict):
            raise DataReadError("stream state has an invalid symbol record")
        record_fields = set(raw_record)
        if envelope:
            record_fields.discard("closed_1m")
            record_fields.difference_update(WINDOW_SNAPSHOT_FIELDS)
        if not required.issubset(raw_record) or not record_fields.issubset(allowed):
            raise DataReadError(f"stream state schema mismatch for {symbol}")
        if not isinstance(raw_record.get("utc"), str):
            raise DataReadError(f"stream state utc is invalid for {symbol}")
        if raw_record.get("venues") not in VENUE_VALUES:
            raise DataReadError(f"stream state venues is invalid for {symbol}")
        record = {field: raw_record.get(field) for field in FEED_FIELDS}
        for field in tuple(NUMERIC_FIELDS) + METADATA_FIELDS:
            value = record[field]
            if value is not None:
                decimal_value(value, f"state {symbol}.{field}")
                if not isinstance(value, str):
                    raise DataReadError(
                        f"state {symbol}.{field} is not a Decimal string or null"
                    )
        for field in UNSUPPORTED_STREAM_FIELDS:
            record[field] = None
        record["basis_gap_pct"] = _basis_gap(record)
        state[symbol] = record
    return state


def load_feed_windows(
    path: Path,
    window: int,
    now_ms: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    """Load only window-horizon candles from a served snapshot."""
    path = Path(path)
    if not path.exists():
        return {}
    restored_at_ms = epoch_ms() if now_ms is None else now_ms
    if isinstance(restored_at_ms, bool) or restored_at_ms < 0:
        raise ValueError("window restore time must be a non-negative integer")
    oldest_allowed_ms = max(0, restored_at_ms - window * KLINE_INTERVAL_MS)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DataReadError(f"stream state is unreadable: {path}") from None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != FEED_STATE_VERSION
        or not isinstance(payload.get("symbols"), dict)
    ):
        return {}
    windows: Dict[str, Dict[str, Any]] = {}
    for raw_symbol, raw_record in payload["symbols"].items():
        symbol = valid_symbol(raw_symbol)
        if symbol is None or not isinstance(raw_record, dict):
            raise DataReadError("feed state has an invalid symbol window")
        raw_closed = raw_record.get("closed_1m", [])
        if not isinstance(raw_closed, list):
            raise DataReadError(f"feed state closed_1m is invalid for {symbol}")
        venues = raw_record.get("venues")
        if venues not in VENUE_VALUES:
            raise DataReadError(f"feed state venues is invalid for {symbol}")
        preferred_venue = "perp" if venues in {"perp", "both"} else "spot"
        window_venue = raw_record.get("window_venue")
        if window_venue is None:
            window_venue = preferred_venue
        if window_venue not in {"spot", "perp"}:
            raise DataReadError(f"feed state window venue is invalid for {symbol}")
        if window_venue != preferred_venue:
            raise DataReadError(f"feed state window venue is stale for {symbol}")
        incoming: List[Tuple[int, Decimal]] = []
        previous_close = -1
        for pair in raw_closed:
            if not isinstance(pair, list) or len(pair) != 2:
                raise DataReadError(f"feed state candle is invalid for {symbol}")
            try:
                close_ms = int(str(pair[0]))
            except (TypeError, ValueError):
                raise DataReadError(
                    f"feed state candle time is invalid for {symbol}"
                ) from None
            volume = decimal_value(pair[1], f"feed state {symbol} volume")
            if close_ms <= previous_close or volume < 0:
                raise DataReadError(f"feed state candle is invalid for {symbol}")
            previous_close = close_ms
            if oldest_allowed_ms <= close_ms <= restored_at_ms:
                incoming.append((close_ms, volume))
        closed = merge_closed_window([], incoming, window)
        if closed:
            windows[symbol] = {
                "closed_1m": closed,
                "last_volume_close_ms": closed[-1][0],
                "venue": window_venue,
            }
    return windows


def feed_snapshot_payload(
    state: Mapping[str, Mapping[str, Any]],
    windows: Mapping[str, Mapping[str, Any]],
    window: int,
    written_ms: int,
    connections: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the atomic serve contract consumed by market_stream."""
    symbols: Dict[str, Dict[str, Any]] = {}
    all_closes: List[int] = []
    for symbol in sorted(state):
        record = {field: state[symbol].get(field) for field in FEED_FIELDS}
        meta = windows.get(symbol, {})
        preferred_venue = (
            "perp"
            if state[symbol].get("venues") in {"perp", "both"}
            else "spot"
        )
        window_venue = meta.get("venue")
        if window_venue not in {"spot", "perp"}:
            window_venue = preferred_venue
        closed = (
            list(meta.get("closed_1m", []))[-window:]
            if window_venue == preferred_venue
            else []
        )
        vol, med, surge = volume_metrics(closed, window)
        record["vol_1m_quote"] = vol
        record["vol_med_1m"] = med
        record["surge_ratio"] = surge
        record["closed_1m"] = closed
        closes = [int(pair[0]) for pair in closed]
        record["window_venue"] = window_venue if closes else None
        record["window_oldest_ms"] = str(closes[0]) if closes else None
        record["window_newest_ms"] = str(closes[-1]) if closes else None
        all_closes.extend(closes)
        symbols[symbol] = record
    connection_payload = copy.deepcopy(connections or {})
    for venue_state in connection_payload.values():
        ticker = venue_state.get("ticker")
        last_ms = ticker.get("last_message_ms") if ticker else None
        if ticker and ticker.get("connected") and (
            last_ms is None or written_ms - int(last_ms) > TICKER_STALE_MS
        ):
            ticker["status"], venue_state["status"] = "stale", "degraded"
    return {
        "version": FEED_STATE_VERSION,
        "written_ms": str(written_ms),
        "window_oldest_ms": str(min(all_closes)) if all_closes else None,
        "window_newest_ms": str(max(all_closes)) if all_closes else None,
        "window": window,
        "connections": connection_payload,
        "symbols": symbols,
    }


def parse_combined_message(text: str) -> Tuple[str, List[Mapping[str, Any]]]:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        raise DataReadError("WebSocket message is not valid JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("stream"), str):
        raise DataReadError("WebSocket message is not a combined stream object")
    stream_name = payload["stream"].lower()
    rows = payload.get("data")
    if stream_name in STREAM_NAMES:
        expected = list if stream_name.endswith("@arr") else dict
        if not isinstance(rows, expected):
            raise DataReadError("WebSocket all-market payload shape is invalid")
        return stream_name, ([rows] if isinstance(rows, dict) else
                             [row for row in rows if isinstance(row, dict)])
    if stream_name.endswith(KLINE_STREAM_SUFFIX):
        symbol = valid_symbol(stream_name[: -len(KLINE_STREAM_SUFFIX)].upper())
        if symbol is not None and isinstance(rows, dict):
            return stream_name, [rows]
    raise DataReadError("WebSocket message has an unexpected stream")


def merge_ticker_rows(
    state: Dict[str, Dict[str, Any]],
    venue: str,
    stream_name: str,
    rows: Iterable[Mapping[str, Any]],
    utc: str,
    received_ms: int,
    seen_ms: Dict[str, Dict[str, int]],
) -> set[str]:
    """Apply one all-market array and return records with new book truth."""
    if venue not in {"spot", "perp"}:
        raise ValueError("venue must be spot or perp")
    normalized_stream = stream_name.lower()
    expected_stream = "!ticker@arr" if venue == "spot" else "!bookticker"
    if normalized_stream != expected_stream:
        raise DataReadError("unexpected Binance stream name")
    updated: set[str] = set()
    bid_field, ask_field = (
        ("spot_bid", "spot_ask") if venue == "spot" else ("perp_bid", "perp_ask")
    )
    for row in rows:
        symbol = valid_symbol(row.get("s"))
        record = state.get(symbol) if symbol is not None else None
        if record is None or record["venues"] not in {venue, "both"}:
            continue
        bid = optional_decimal_string(row, "b", f"{venue} {symbol}", positive=True)
        ask = optional_decimal_string(row, "a", f"{venue} {symbol}", positive=True)
        if (bid is None or ask is None or
                decimal_value(bid, "ticker bid") > decimal_value(ask, "ticker ask")):
            bid, ask = None, None
        record[bid_field] = bid
        record[ask_field] = ask
        event_ms = row.get("T", row.get("E")) if venue == "perp" else row.get("E")
        try:
            source_ms = int(str(event_ms))
        except (TypeError, ValueError):
            source_ms = received_ms
        if source_ms < 0:
            source_ms = received_ms
        seen_ms.setdefault(symbol, {})[venue] = source_ms
        record["utc"] = utc
        record["basis_gap_pct"] = _basis_gap(record)
        _set_freshness(record, symbol, received_ms, seen_ms)
        updated.add(symbol)
    return updated


def merge_kline_rows(
    state: Mapping[str, Mapping[str, Any]],
    venue: str,
    stream_name: str,
    rows: Iterable[Mapping[str, Any]],
    windows: Dict[str, Dict[str, Any]],
    window: int,
) -> set[str]:
    """Merge exact, closed one-minute quote-volume candles."""
    if venue not in {"spot", "perp"}:
        raise ValueError("venue must be spot or perp")
    normalized = stream_name.lower()
    if not normalized.endswith(KLINE_STREAM_SUFFIX):
        raise DataReadError("unexpected Binance kline stream name")
    stream_symbol = valid_symbol(
        normalized[: -len(KLINE_STREAM_SUFFIX)].upper()
    )
    if stream_symbol is None:
        raise DataReadError("invalid Binance kline stream symbol")
    changed: set[str] = set()
    for row in rows:
        raw_kline = row.get("k")
        if not isinstance(raw_kline, dict):
            continue
        symbol = valid_symbol(raw_kline.get("s", row.get("s")))
        record = state.get(symbol) if symbol is not None else None
        if symbol != stream_symbol or record is None:
            continue
        preferred_venue = (
            "perp" if record.get("venues") in {"perp", "both"} else "spot"
        )
        if venue != preferred_venue:
            continue
        if raw_kline.get("i") != "1m" or raw_kline.get("x") is not True:
            continue
        try:
            close_ms = int(str(raw_kline.get("T")))
            quote_volume = decimal_value(
                raw_kline.get("q"), f"{venue} {symbol} closed 1m quote volume"
            )
        except (TypeError, ValueError, DataReadError):
            continue
        if close_ms < 0 or quote_volume < 0:
            continue
        meta = windows.get(symbol)
        if meta is None or meta.get("venue") not in {None, venue}:
            meta = {"closed_1m": [], "venue": venue}
            windows[symbol] = meta
        else:
            meta["venue"] = venue
        meta["closed_1m"] = merge_closed_window(
            meta.get("closed_1m", []), [(close_ms, quote_volume)], window
        )
        meta["last_volume_close_ms"] = meta["closed_1m"][-1][0]
        changed.add(symbol)
    return changed


def websocket_urls(state: Mapping[str, Mapping[str, Any]]) -> Dict[str, List[str]]:
    """Build one ticker socket plus kline shards within Binance's cap."""
    spot_symbols = sorted(
        symbol for symbol, record in state.items() if record.get("venues") == "spot"
    )
    perp_symbols = sorted(
        symbol
        for symbol, record in state.items()
        if record.get("venues") in {"perp", "both"}
    )

    def with_klines(base: str, symbols: Sequence[str]) -> List[str]:
        shards: List[str] = []
        for offset in range(0, len(symbols), MAX_KLINES_PER_CONNECTION):
            chunk = symbols[offset : offset + MAX_KLINES_PER_CONNECTION]
            streams = "/".join(
                f"{symbol.lower()}{KLINE_STREAM_SUFFIX}" for symbol in chunk
            )
            shards.append(base.split("?streams=", 1)[0] + "?streams=" + streams)
        return shards

    return {
        "spot": [SPOT_WS_URL, *with_klines(SPOT_WS_URL, spot_symbols)],
        "perp": [FAPI_WS_URL, *with_klines(FAPI_WS_URL, perp_symbols)],
    }


def _set_freshness(
    record: Dict[str, Any],
    symbol: str,
    now_ms: int,
    seen_ms: Mapping[str, Mapping[str, int]],
) -> None:
    ages: List[int] = []
    timestamps = seen_ms.get(symbol, {})
    if record.get("spot_bid") is not None and "spot" in timestamps:
        ages.append(max(0, now_ms - timestamps["spot"]))
    if record.get("perp_bid") is not None and "perp" in timestamps:
        ages.append(max(0, now_ms - timestamps["perp"]))
    record["freshness_ms"] = str(max(ages)) if ages else None


def refresh_freshness(
    state: Mapping[str, Dict[str, Any]],
    seen_ms: Mapping[str, Mapping[str, int]],
    now_ms: int,
) -> None:
    for symbol, record in state.items():
        _set_freshness(record, symbol, now_ms, seen_ms)


def run_reconnect_loop(
    url: str,
    on_text: Callable[[str], None],
    *,
    connector: Callable[[str], Any],
    stop_requested: Callable[[], bool],
    wait_fn: Callable[[float], bool],
    on_attempt: Optional[Callable[[], None]] = None,
    on_connect: Optional[Callable[[], None]] = None,
    on_connect_failure: Optional[Callable[[], None]] = None,
    on_disconnect: Optional[Callable[[], None]] = None,
    initial_backoff: float = DEFAULT_BACKOFF_INITIAL,
    maximum_backoff: float = DEFAULT_BACKOFF_MAX,
) -> None:
    """Receive forever, reconnecting with bounded exponential backoff."""
    delay = initial_backoff
    while not stop_requested():
        client: Any = None
        received = False
        try:
            if on_attempt is not None:
                on_attempt()
            client = connector(url)
            if on_connect is not None:
                on_connect()
            while not stop_requested():
                try:
                    text = client.recv_text()
                except socket.timeout:
                    continue
                on_text(text)
                received = True
                delay = initial_backoff
        except (OSError, WebSocketError):
            if client is None and on_connect_failure is not None:
                on_connect_failure()
        finally:
            if client is not None:
                try:
                    client.close()
                except (OSError, WebSocketError):
                    pass
        if stop_requested():
            break
        if client is not None and on_disconnect is not None:
            on_disconnect()
        if wait_fn(delay):
            break
        if not received:
            delay = min(maximum_backoff, delay * 2)


class MarketFeed:
    """Coordinate metadata refresh, sharded sockets, and state writes."""

    def __init__(
        self,
        state_path: Path = DEFAULT_STATE_PATH,
        stop_path: Path = DEFAULT_STOP_PATH,
        *,
        serve: bool = False,
        window: int = DEFAULT_WINDOW,
        metadata_minutes: float = DEFAULT_METADATA_MINUTES,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
        reader: Optional[Callable[[str, str, Mapping[str, str]], Any]] = None,
        connector: Callable[[str], Any] = connect_websocket,
        utc_fn: Callable[[], str] = now_utc,
        ms_fn: Callable[[], int] = epoch_ms,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ):
        if metadata_minutes <= 0 or flush_seconds <= 0:
            raise ValueError("metadata and flush intervals must be positive")
        if not 2 <= window <= MAX_WINDOW:
            raise ValueError(f"window must be between 2 and {MAX_WINDOW}")
        self.state_path = Path(state_path)
        self.stop_path = Path(stop_path)
        self.serve = serve
        self.window = window
        self.metadata_seconds = metadata_minutes * 60
        self.flush_seconds = flush_seconds
        self.reader = PublicJsonReader() if reader is None else reader
        self.connector = connector
        self.utc_fn = utc_fn
        self.ms_fn = ms_fn
        self.monotonic_fn = monotonic_fn
        self.state = load_feed_state(self.state_path)
        self.windows = (
            load_feed_windows(self.state_path, self.window, self.ms_fn())
            if self.serve
            else {}
        )
        if self.serve:
            for record in self.state.values():
                for field in ("spot_bid", "spot_ask", "perp_bid", "perp_ask"):
                    record[field] = None
                record["basis_gap_pct"] = None
                record["freshness_ms"] = None
        self.seen_ms: Dict[str, Dict[str, int]] = {}
        self.connections: Dict[str, Dict[str, Any]] = {}
        self.events = EventQueue()
        self.stop_event = threading.Event()

    def _stopped(self) -> bool:
        return self.stop_event.is_set() or self.stop_path.exists()

    def _wait(self, delay: float,
              worker_stop: Optional[threading.Event] = None) -> bool:
        deadline = self.monotonic_fn() + delay
        while not self._stopped() and not (
            worker_stop is not None and worker_stop.is_set()):
            remaining = deadline - self.monotonic_fn()
            if remaining <= 0:
                return False
            self.stop_event.wait(min(remaining, 0.25))
        return True

    def _worker(self, venue: str, shard: int, url: str, generation: int,
                worker_stop: threading.Event) -> None:
        def emit(kind: str, text: Optional[str] = None) -> None:
            self.events.offer((kind, venue, shard, generation, text))

        run_reconnect_loop(
            url,
            lambda text: emit("text", text),
            connector=self.connector,
            stop_requested=lambda: worker_stop.is_set() or self._stopped(),
            wait_fn=lambda delay: self._wait(delay, worker_stop),
            on_attempt=lambda: emit("attempt"),
            on_connect=lambda: emit("connected"),
            on_connect_failure=lambda: emit("handshake_failed"),
            on_disconnect=lambda: emit("disconnected"),
        )

    def _refresh_metadata(self) -> bool:
        """Refresh metadata and report whether socket shards must restart."""
        old_urls = websocket_urls(self.state)
        old_venues = {
            symbol: record.get("venues") for symbol, record in self.state.items()
        }
        spot_payload = self.reader(SPOT_BASE_URL, SPOT_EXCHANGE_INFO_PATH, {})
        perp_payload = self.reader(FAPI_BASE_URL, FAPI_EXCHANGE_INFO_PATH, {})
        spot = parse_exchange_metadata(spot_payload, "spot")
        perp = parse_exchange_metadata(perp_payload, "perp")
        self.state = merge_metadata(self.state, spot, perp, self.utc_fn())
        active = set(self.state)
        venue_changed = {
            symbol
            for symbol, record in self.state.items()
            if symbol in old_venues and old_venues[symbol] != record.get("venues")
        }
        self.seen_ms = {
            symbol: stamps
            for symbol, stamps in self.seen_ms.items()
            if symbol in active and symbol not in venue_changed
        }
        self.windows = {
            symbol: meta
            for symbol, meta in self.windows.items()
            if symbol in active and symbol not in venue_changed
        }
        return old_urls != websocket_urls(self.state)

    def _reset_connections(self, urls: Mapping[str, Sequence[str]]) -> None:
        prior_connections = self.connections
        self.connections = {}
        for venue in ("spot", "perp"):
            venue_urls = list(urls.get(venue, ()))
            common = {
                "attempts": 0, "successful_handshakes": 0,
                "handshake_failures": 0, "reconnects": 0,
                "connected": False, "status": "broken",
            }
            ticker = {**common, "connection": "ticker", "stream_count": 1,
                      "last_message_ms": None}
            shards = [
                {**common, "connection": index,
                 "stream_count": len(url.split("?streams=", 1)[-1].split("/"))}
                for index, url in enumerate(venue_urls[1:])
            ]
            self.connections[venue] = {
                "status": "broken",
                "expected_connections": len(venue_urls),
                "successful_handshakes": 0,
                "handshake_failures": 0, "reconnects": 0,
                "dropped_frames": prior_connections.get(venue, {}).get(
                    "dropped_frames", 0),
                "ticker": ticker,
                "shards": shards,
            }

    def _record_connection_event(self, kind: str, venue: str, shard: int,
                                 message_ms: Optional[int] = None) -> None:
        venue_state = self.connections.get(venue)
        if venue_state is None:
            return
        shards = venue_state.get("shards", [])
        if shard < -1 or shard >= len(shards):
            return
        connection = venue_state["ticker"] if shard == -1 else shards[shard]
        if kind == "attempt":
            connection["attempts"] += 1
            connection["reconnects"] = max(0, connection["attempts"] - 1)
            if shard == -1:
                connection["last_message_ms"] = None
        elif kind == "connected":
            connection["successful_handshakes"] += 1
            connection["connected"] = True
        elif kind == "handshake_failed":
            connection["handshake_failures"] += 1
            connection["connected"] = False
        elif kind == "disconnected":
            connection["connected"] = False
        elif kind == "message" and shard == -1 and message_ms is not None:
            connection["last_message_ms"] = str(message_ms)
        else:
            return
        if connection["connected"]:
            has_message = shard >= 0 or connection["last_message_ms"]
            connection["status"] = "healthy" if has_message else "stale"
        elif connection["successful_handshakes"] == 0:
            connection["status"] = "broken"
        else:
            connection["status"] = "disconnected"
        all_connections = [venue_state["ticker"], *shards]
        for key in ("successful_handshakes", "handshake_failures", "reconnects"):
            venue_state[key] = sum(row[key] for row in all_connections)
        if venue_state["successful_handshakes"] == 0:
            venue_state["status"] = "broken"
        elif all(row["status"] == "healthy" for row in all_connections):
            venue_state["status"] = "healthy"
        else:
            venue_state["status"] = "degraded"

    def _start_workers(self, urls: Mapping[str, Sequence[str]], generation: int,
                       worker_stop: threading.Event) -> List[threading.Thread]:
        workers = [
            threading.Thread(
                target=self._worker,
                args=(venue, shard, url, generation, worker_stop),
                name=f"market-feed-{venue}-{'ticker' if shard < 0 else shard}",
                daemon=True,
            )
            for venue in ("spot", "perp")
            for shard, url in enumerate(urls.get(venue, ()), -1)
        ]
        for worker in workers:
            worker.start()
        return workers

    def _write_state(self) -> None:
        payload: Any = (feed_snapshot_payload(
            self.state, self.windows, self.window, self.ms_fn(), self.connections
        ) if self.serve else self.state)
        atomic_write_json(self.state_path, payload)

    def run(self, duration_seconds: Optional[float] = None) -> FeedStats:
        if duration_seconds is not None and duration_seconds <= 0:
            raise ValueError("duration must be positive")
        started = self.monotonic_fn()
        started_utc = self.utc_fn()
        if self._stopped():
            return FeedStats(
                started_utc, self.utc_fn(), 0.0, len(self.state), 0, 0, 0, 0, 0, 0
            )
        self._refresh_metadata()
        metadata_refreshes = 1
        urls = websocket_urls(self.state)
        generation = 1
        worker_stop = threading.Event()
        self._reset_connections(urls)
        self._write_state()
        next_metadata = self.monotonic_fn() + self.metadata_seconds
        next_flush = self.monotonic_fn() + self.flush_seconds
        deadline = None if duration_seconds is None else started + duration_seconds
        workers = self._start_workers(urls, generation, worker_stop)
        updated_symbols: set[str] = set()
        ticker_updates = 0
        websocket_messages = 0
        invalid_messages = 0
        handshake_failures = 0
        reconnects = 0
        attempt_counts: Dict[Tuple[int, str, int], int] = {}
        dirty = False

        def handle_event(event: FeedEvent) -> None:
            nonlocal dirty, handshake_failures, invalid_messages
            nonlocal reconnects, ticker_updates, websocket_messages
            kind, venue, shard, event_generation, text = event
            connection_key = (event_generation, venue, shard)
            if kind == "attempt":
                prior_attempts = attempt_counts.get(connection_key, 0)
                attempt_counts[connection_key] = prior_attempts + 1
                if prior_attempts:
                    reconnects += 1
            elif kind == "handshake_failed":
                handshake_failures += 1
            if event_generation != generation:
                return
            if kind in {
                "attempt",
                "connected",
                "handshake_failed",
                "disconnected",
            }:
                self._record_connection_event(kind, venue, shard)
                dirty = True
                return
            if kind != "text" or text is None:
                return
            websocket_messages += 1
            if shard == -1:
                self._record_connection_event("message", venue, shard, self.ms_fn())
                dirty = True
            try:
                stream_name, rows = parse_combined_message(text)
                if stream_name.endswith(KLINE_STREAM_SUFFIX):
                    changed = merge_kline_rows(
                        self.state,
                        venue,
                        stream_name,
                        rows,
                        self.windows,
                        self.window,
                    )
                    ticker_changed = False
                else:
                    changed = merge_ticker_rows(
                        self.state,
                        venue,
                        stream_name,
                        rows,
                        self.utc_fn(),
                        self.ms_fn(),
                        self.seen_ms,
                    )
                    ticker_changed = True
            except MarketStreamError:
                invalid_messages += 1
            else:
                if changed:
                    updated_symbols.update(changed)
                    if ticker_changed:
                        ticker_updates += len(changed)
                    dirty = True

        def drain_events(timeout: float = 0.0) -> None:
            nonlocal dirty
            with self.events.not_empty:
                if not self.events._qsize():
                    self.events.not_empty.wait(timeout)
                batch_size = self.events._qsize()
            for unused in range(batch_size):
                handle_event(self.events.get_nowait())
            with self.events.mutex:
                dropped, self.events.dropped = self.events.dropped, {}
            for (drop_generation, venue), count in dropped.items():
                if drop_generation == generation and venue in self.connections:
                    self.connections[venue]["dropped_frames"] += count
                    dirty = True

        try:
            while not self._stopped():
                now = self.monotonic_fn()
                if deadline is not None and now >= deadline:
                    break
                if now >= next_metadata:
                    try:
                        subscriptions_changed = self._refresh_metadata()
                    except MarketStreamError:
                        invalid_messages += 1
                    else:
                        metadata_refreshes += 1
                        dirty = True
                        if subscriptions_changed:
                            worker_stop.set()
                            for worker in workers:
                                worker.join(timeout=DEFAULT_SOCKET_TIMEOUT + 1.0)
                            generation += 1
                            urls = websocket_urls(self.state)
                            worker_stop = threading.Event()
                            self._reset_connections(urls)
                            self._write_state()
                            dirty = False
                            workers = self._start_workers(urls, generation, worker_stop)
                    next_metadata = now + self.metadata_seconds
                timeout = min(0.25, max(0.0, next_flush - now))
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - now))
                drain_events(timeout)
                now = self.monotonic_fn()
                if now >= next_flush:
                    if dirty:
                        refresh_freshness(self.state, self.seen_ms, self.ms_fn())
                        self._write_state()
                        dirty = False
                    next_flush = now + self.flush_seconds
        finally:
            self.stop_event.set()
            worker_stop.set()
            for worker in workers:
                worker.join(timeout=DEFAULT_SOCKET_TIMEOUT + 1.0)
            drain_events()
            if dirty:
                refresh_freshness(self.state, self.seen_ms, self.ms_fn())
                self._write_state()
        ended = self.monotonic_fn()
        broken_venues = tuple(
            venue
            for venue in ("spot", "perp")
            if self.connections.get(venue, {}).get("status") == "broken"
        )
        return FeedStats(
            started_utc=started_utc,
            ended_utc=self.utc_fn(),
            elapsed_seconds=max(0.0, ended - started),
            symbols_total=len(self.state),
            tokens_updated=len(updated_symbols),
            ticker_updates=ticker_updates,
            websocket_messages=websocket_messages,
            metadata_refreshes=metadata_refreshes,
            reconnects=reconnects,
            invalid_messages=invalid_messages,
            handshake_failures=handshake_failures,
            broken_venues=broken_venues,
        )


def positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be numeric") from None
    if not value > 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def window_size(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("window must be an integer") from None
    if not 2 <= value <= MAX_WINDOW:
        raise argparse.ArgumentTypeError(
            f"window must be between 2 and {MAX_WINDOW}"
        )
    return value


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain a continuous all-USDT Binance market feed."
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="write a versioned live snapshot with rolling closed 1m windows",
    )
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--stop-file", type=Path, default=DEFAULT_STOP_PATH)
    parser.add_argument("--window", type=window_size, default=DEFAULT_WINDOW)
    parser.add_argument(
        "--metadata-minutes", type=positive_float, default=DEFAULT_METADATA_MINUTES
    )
    parser.add_argument(
        "--flush-seconds", type=positive_float, default=DEFAULT_FLUSH_SECONDS
    )
    parser.add_argument(
        "--duration",
        type=positive_float,
        help="optional measured run duration in seconds",
    )
    return parser


def print_stats(stats: FeedStats) -> None:
    print(f"feed_started_utc={stats.started_utc}")
    print(f"feed_ended_utc={stats.ended_utc}")
    print(
        f"symbols_total={stats.symbols_total} "
        f"tokens_updated={stats.tokens_updated} "
        f"ticker_updates={stats.ticker_updates}"
    )
    print(
        f"elapsed_seconds={stats.elapsed_seconds:.3f} "
        f"update_rate={stats.update_rate:.3f}_symbols_per_second"
    )
    print(
        f"websocket_messages={stats.websocket_messages} "
        f"metadata_refreshes={stats.metadata_refreshes} "
        f"reconnects={stats.reconnects} "
        f"handshake_failures={stats.handshake_failures} "
        f"broken_venues={','.join(stats.broken_venues) or 'none'} "
        f"invalid_messages={stats.invalid_messages}"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = argument_parser().parse_args(argv)
    try:
        stats = MarketFeed(
            state_path=args.state_file,
            stop_path=args.stop_file,
            serve=args.serve,
            window=args.window,
            metadata_minutes=args.metadata_minutes,
            flush_seconds=args.flush_seconds,
        ).run(args.duration)
        print_stats(stats)
        return 0
    except KeyboardInterrupt:
        return 130
    except MarketStreamError as exc:
        print(f"market_feed error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"market_feed filesystem error: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
