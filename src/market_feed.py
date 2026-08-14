#!/usr/bin/env python3
"""Continuous public Binance Spot + USD-M USDT market feed.

Two combined WebSocket connections consume Binance's all-market ``!ticker``
and ``!miniTicker`` arrays.  There is no symbol rotation and there are no
per-symbol HTTP reads.  Unsigned ``exchangeInfo`` GETs provide the tradable
universe and venue-specific order-size constraints.

The public state keeps every field from :mod:`market_stream`.  Fields that
cannot be proved from these ticker streams (mark, funding, open interest, and
one-minute volume analytics) remain null.  Additional flat Decimal-string
fields carry Spot and perpetual LOT_SIZE/minNotional data.  No credential,
signed request, POST, account read, or order path exists in this module.
"""

from __future__ import annotations

import argparse
import base64
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
    DataReadError,
    MarketStreamError,
    NUMERIC_FIELDS,
    PublicJsonReader,
    STATE_FIELDS,
    SYMBOL_PATTERN,
    VENUE_VALUES,
    atomic_write_json,
    decimal_string,
    decimal_value,
    optional_decimal_string,
)


SPOT_BASE_URL = "https://api.binance.com"
FAPI_BASE_URL = "https://fapi.binance.com"
SPOT_EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
FAPI_EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"

SPOT_WS_URL = (
    "wss://stream.binance.com:9443/stream"
    "?streams=!ticker@arr/!miniTicker@arr"
)
FAPI_WS_URL = (
    "wss://fstream.binance.com/market/stream"
    "?streams=!ticker@arr/!miniTicker@arr"
)

DEFAULT_STATE_PATH = Path("stream_state.json")
DEFAULT_STOP_PATH = Path("STOP")
DEFAULT_METADATA_MINUTES = 30.0
DEFAULT_FLUSH_SECONDS = 1.0
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_SOCKET_TIMEOUT = 1.0
DEFAULT_BACKOFF_INITIAL = 1.0
DEFAULT_BACKOFF_MAX = 30.0
MAX_FRAME_BYTES = 32_000_000
MAX_HANDSHAKE_BYTES = 64_000
RFC6455_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

METADATA_FIELDS = (
    "spot_min_qty",
    "spot_step_size",
    "spot_min_notional",
    "perp_min_qty",
    "perp_step_size",
    "perp_min_notional",
)
FEED_FIELDS = STATE_FIELDS + METADATA_FIELDS
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
STREAM_NAMES = frozenset({"!ticker@arr", "!miniticker@arr"})


class WebSocketError(MarketStreamError):
    """A transport, handshake, or RFC6455 protocol failure."""


class WebSocketClosed(WebSocketError):
    """The peer closed the WebSocket connection."""


class WebSocketProtocolError(WebSocketError):
    """The peer sent a frame that violates the supported RFC6455 subset."""


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
    state: Dict[str, Dict[str, Any]] = {}
    allowed = set(FEED_FIELDS)
    required = set(STATE_FIELDS)
    for raw_symbol, raw_record in payload.items():
        symbol = valid_symbol(raw_symbol)
        if symbol is None or not isinstance(raw_record, dict):
            raise DataReadError("stream state has an invalid symbol record")
        if not required.issubset(raw_record) or not set(raw_record).issubset(allowed):
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


def parse_combined_message(text: str) -> Tuple[str, List[Mapping[str, Any]]]:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        raise DataReadError("WebSocket message is not valid JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("stream"), str):
        raise DataReadError("WebSocket message is not a combined stream object")
    stream_name = payload["stream"].lower()
    if stream_name not in STREAM_NAMES:
        raise DataReadError("WebSocket message has an unexpected stream")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise DataReadError("WebSocket all-market payload is not a list")
    return stream_name, [row for row in rows if isinstance(row, dict)]


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
    if normalized_stream not in STREAM_NAMES:
        raise DataReadError("unexpected Binance stream name")
    # miniTicker contains last trade and 24h volume, neither of which is a
    # frozen-schema bid/ask, mark, or one-minute quote-volume measurement.
    if normalized_stream == "!miniticker@arr":
        return set()
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
        if bid is not None and ask is not None:
            if decimal_value(bid, "ticker bid") > decimal_value(ask, "ticker ask"):
                bid, ask = None, None
        record[bid_field] = bid
        record[ask_field] = ask
        event_ms = row.get("E")
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
    on_connect: Optional[Callable[[], None]] = None,
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
            pass
        finally:
            if client is not None:
                try:
                    client.close()
                except (OSError, WebSocketError):
                    pass
        if stop_requested():
            break
        if on_disconnect is not None:
            on_disconnect()
        if wait_fn(delay):
            break
        if not received:
            delay = min(maximum_backoff, delay * 2)


class MarketFeed:
    """Coordinate metadata refresh, two reconnecting sockets, and state writes."""

    def __init__(
        self,
        state_path: Path = DEFAULT_STATE_PATH,
        stop_path: Path = DEFAULT_STOP_PATH,
        *,
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
        self.state_path = Path(state_path)
        self.stop_path = Path(stop_path)
        self.metadata_seconds = metadata_minutes * 60
        self.flush_seconds = flush_seconds
        self.reader = PublicJsonReader() if reader is None else reader
        self.connector = connector
        self.utc_fn = utc_fn
        self.ms_fn = ms_fn
        self.monotonic_fn = monotonic_fn
        self.state = load_feed_state(self.state_path)
        self.seen_ms: Dict[str, Dict[str, int]] = {}
        self.events: queue.Queue[Tuple[str, str, Optional[str]]] = queue.Queue()
        self.stop_event = threading.Event()

    def _stopped(self) -> bool:
        return self.stop_event.is_set() or self.stop_path.exists()

    def _wait(self, delay: float) -> bool:
        deadline = self.monotonic_fn() + delay
        while not self._stopped():
            remaining = deadline - self.monotonic_fn()
            if remaining <= 0:
                return False
            self.stop_event.wait(min(remaining, 0.25))
        return True

    def _worker(self, venue: str, url: str) -> None:
        run_reconnect_loop(
            url,
            lambda text: self.events.put(("text", venue, text)),
            connector=self.connector,
            stop_requested=self._stopped,
            wait_fn=self._wait,
            on_connect=lambda: self.events.put(("connected", venue, None)),
            on_disconnect=lambda: self.events.put(("disconnected", venue, None)),
        )

    def _refresh_metadata(self) -> None:
        spot_payload = self.reader(SPOT_BASE_URL, SPOT_EXCHANGE_INFO_PATH, {})
        perp_payload = self.reader(FAPI_BASE_URL, FAPI_EXCHANGE_INFO_PATH, {})
        spot = parse_exchange_metadata(spot_payload, "spot")
        perp = parse_exchange_metadata(perp_payload, "perp")
        self.state = merge_metadata(self.state, spot, perp, self.utc_fn())
        active = set(self.state)
        self.seen_ms = {
            symbol: stamps for symbol, stamps in self.seen_ms.items() if symbol in active
        }

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
        atomic_write_json(self.state_path, self.state)
        next_metadata = self.monotonic_fn() + self.metadata_seconds
        next_flush = self.monotonic_fn() + self.flush_seconds
        deadline = None if duration_seconds is None else started + duration_seconds
        workers = [
            threading.Thread(
                target=self._worker,
                args=(venue, url),
                name=f"market-feed-{venue}",
                daemon=True,
            )
            for venue, url in (("spot", SPOT_WS_URL), ("perp", FAPI_WS_URL))
        ]
        for worker in workers:
            worker.start()
        updated_symbols: set[str] = set()
        ticker_updates = 0
        websocket_messages = 0
        invalid_messages = 0
        connection_counts = {"spot": 0, "perp": 0}
        dirty = False
        try:
            while not self._stopped():
                now = self.monotonic_fn()
                if deadline is not None and now >= deadline:
                    break
                if now >= next_metadata:
                    try:
                        self._refresh_metadata()
                    except MarketStreamError:
                        invalid_messages += 1
                    else:
                        metadata_refreshes += 1
                        dirty = True
                    next_metadata = now + self.metadata_seconds
                timeout = min(0.25, max(0.0, next_flush - now))
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - now))
                try:
                    kind, venue, text = self.events.get(timeout=timeout)
                except queue.Empty:
                    kind = "idle"
                    venue = ""
                    text = None
                if kind == "connected":
                    connection_counts[venue] += 1
                elif kind == "text" and text is not None:
                    websocket_messages += 1
                    try:
                        stream_name, rows = parse_combined_message(text)
                        changed = merge_ticker_rows(
                            self.state,
                            venue,
                            stream_name,
                            rows,
                            self.utc_fn(),
                            self.ms_fn(),
                            self.seen_ms,
                        )
                    except MarketStreamError:
                        invalid_messages += 1
                    else:
                        if changed:
                            updated_symbols.update(changed)
                            ticker_updates += len(changed)
                            dirty = True
                now = self.monotonic_fn()
                if now >= next_flush:
                    if dirty:
                        refresh_freshness(self.state, self.seen_ms, self.ms_fn())
                        atomic_write_json(self.state_path, self.state)
                        dirty = False
                    next_flush = now + self.flush_seconds
        finally:
            self.stop_event.set()
            if dirty:
                refresh_freshness(self.state, self.seen_ms, self.ms_fn())
                atomic_write_json(self.state_path, self.state)
            for worker in workers:
                worker.join(timeout=DEFAULT_SOCKET_TIMEOUT + 1.0)
        ended = self.monotonic_fn()
        reconnects = sum(max(0, count - 1) for count in connection_counts.values())
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
        )


def positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be numeric") from None
    if not value > 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain a continuous all-USDT Binance market feed."
    )
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--stop-file", type=Path, default=DEFAULT_STOP_PATH)
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
        f"reconnects={stats.reconnects} invalid_messages={stats.invalid_messages}"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = argument_parser().parse_args(argv)
    try:
        stats = MarketFeed(
            state_path=args.state_file,
            stop_path=args.stop_file,
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
