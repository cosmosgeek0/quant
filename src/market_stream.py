#!/usr/bin/env python3
"""One restart-safe Binance Spot + USD-M perpetual market truth stream.

The per-pass universe comes only from the two bulk book-ticker endpoints.  It
therefore has no alphabetical cap or fixed symbol list: every symbol ending in
USDT from either venue is represented, including Spot-only, perpetual-only,
and shared markets.  Mark price and funding are joined from one additional
bulk USD-M premium-index read.

Expensive per-symbol reads are bounded by ``focus_cap``.  Half of the focus
slots (rounded down) revisit the largest absolute price moves measurable from
the previous state; the remaining slots walk a persisted, sorted circular
cursor.  Consequently a stable universe of M symbols is sampled completely
within ``ceil(M / rotation_slots)`` passes.  The output states that bound.

For one-volume-field semantics, USD-M 1m klines are used whenever a perpetual
exists; Spot 1m klines are used for Spot-only symbols.  Only closed candles are
accepted.  The rolling history and rotation cursor live in a schema-private
``stream_windows.json`` sidecar so ``stream_state.json`` stays exactly the
frozen consumer schema.  All HTTP calls are unsigned public GETs.  There is no
credential, signing, POST, order, or account path in this module.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from statistics import median
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SPOT_BASE_URL = "https://api.binance.com"
FAPI_BASE_URL = "https://fapi.binance.com"
SPOT_BOOK_PATH = "/api/v3/ticker/bookTicker"
FAPI_BOOK_PATH = "/fapi/v1/ticker/bookTicker"
FAPI_PREMIUM_PATH = "/fapi/v1/premiumIndex"
SPOT_KLINES_PATH = "/api/v3/klines"
FAPI_KLINES_PATH = "/fapi/v1/klines"
FAPI_OI_PATH = "/fapi/v1/openInterest"

DEFAULT_STATE_PATH = Path("stream_state.json")
DEFAULT_LOG_PATH = Path("stream_log.jsonl")
DEFAULT_WINDOWS_NAME = "stream_windows.json"
DEFAULT_WINDOW = 240
DEFAULT_FOCUS_CAP = 12
DEFAULT_THRESHOLDS = (Decimal("2"), Decimal("3"), Decimal("5"))
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEST_SPACING_SECONDS = 0.05
MAX_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 32_000_000
MAX_WINDOW = 998  # request asks for window + 2, below Binance's 1000 limit
MAX_FOCUS_CAP = 200
SIDECAR_VERSION = 1
EXIT_INPUT = 2
EXIT_HTTP = 3
EXIT_DATA = 4
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{5,40}")

STATE_FIELDS = (
    "utc",
    "venues",
    "spot_bid",
    "spot_ask",
    "perp_bid",
    "perp_ask",
    "mark",
    "vol_1m_quote",
    "vol_med_1m",
    "surge_ratio",
    "oi",
    "oi_change_pct",
    "funding_rate",
    "next_funding_ms",
    "basis_gap_pct",
    "freshness_ms",
)
NUMERIC_FIELDS = frozenset(STATE_FIELDS) - {"utc", "venues"}
VENUE_VALUES = frozenset({"spot", "perp", "both"})


class MarketStreamError(Exception):
    """Expected stream failure with a stable process exit code."""

    exit_code = 1


class HttpReadError(MarketStreamError):
    exit_code = EXIT_HTTP


class DataReadError(MarketStreamError):
    exit_code = EXIT_DATA


@dataclass(frozen=True)
class FocusSelection:
    symbols: Tuple[str, ...]
    next_cursor: int
    mover_slots: int
    rotation_slots: int
    coverage_passes: int


@dataclass(frozen=True)
class PassStats:
    utc: str
    total: int
    spot_only: int
    perp_only: int
    both: int
    request_count: int
    request_budget: int
    bulk_requests: int
    kline_requests: int
    oi_requests: int
    focus_failures: int
    focus_cap: int
    focus_selected: int
    mover_slots: int
    rotation_slots: int
    coverage_passes: int
    window: int
    top_surges: Tuple[Tuple[str, str], ...]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000


def decimal_value(raw: Any, source: str) -> Decimal:
    if raw is None or isinstance(raw, bool):
        raise DataReadError(f"{source} is not a decimal")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise DataReadError(f"{source} is not a decimal") from None
    if not value.is_finite():
        raise DataReadError(f"{source} is not finite")
    return value


def decimal_string(value: Decimal) -> str:
    """Serialize a finite Decimal without exponent notation."""
    if not value.is_finite():
        raise DataReadError("cannot serialize a non-finite decimal")
    return format(value, "f")


def optional_decimal_string(
    row: Mapping[str, Any], field: str, source: str, *, positive: bool = False
) -> Optional[str]:
    try:
        value = decimal_value(row.get(field), f"{source}.{field}")
    except DataReadError:
        return None
    if positive and value <= 0:
        return None
    return decimal_string(value)


def integer_string(row: Mapping[str, Any], field: str, source: str) -> Optional[str]:
    raw = row.get(field)
    if isinstance(raw, bool):
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    if value < 0 or str(raw).strip() not in {str(value), f"{value}.0"}:
        return None
    return str(value)


def _valid_symbol(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    if not raw.endswith("USDT") or not SYMBOL_PATTERN.fullmatch(raw):
        return None
    return raw


class PublicJsonReader:
    """One-attempt unsigned public JSON GET reader."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.timeout = timeout

    def __call__(
        self, base_url: str, path: str, params: Mapping[str, str]
    ) -> Any:
        query = urlencode(params)
        url = base_url + path + (("?" + query) if query else "")
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "quant-market-stream/1",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise HttpReadError(f"public GET {path} returned HTTP {exc.code}") from None
        except (
            URLError,
            TimeoutError,
            socket.timeout,
            OSError,
            http.client.HTTPException,
        ) as exc:
            raise HttpReadError(
                f"public GET {path} failed: {type(exc).__name__}"
            ) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DataReadError(f"public GET {path} response exceeded byte limit")
        try:
            return json.loads(raw.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DataReadError(f"public GET {path} returned invalid JSON") from None


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write complete JSON to a same-directory temporary, then atomically rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def append_events(path: Path, events: Iterable[Mapping[str, Any]]) -> None:
    rows = list(events)
    if not rows:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for event in rows:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_json_file(path: Path, description: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DataReadError(f"{description} is unreadable: {path}") from None


def _validate_numeric_text(raw: Any, source: str) -> None:
    if raw is None:
        return
    if not isinstance(raw, str):
        raise DataReadError(f"{source} is not a Decimal string or null")
    decimal_value(raw, source)


def load_state(path: Path) -> Dict[str, Dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return {}
    payload = _read_json_file(path, "stream state")
    if not isinstance(payload, dict):
        raise DataReadError("stream state is not a symbol mapping")
    state: Dict[str, Dict[str, Any]] = {}
    expected = set(STATE_FIELDS)
    for raw_symbol, raw_record in payload.items():
        symbol = _valid_symbol(raw_symbol)
        if symbol is None or not isinstance(raw_record, dict):
            raise DataReadError("stream state has an invalid symbol record")
        if set(raw_record) != expected:
            raise DataReadError(f"stream state schema mismatch for {symbol}")
        if not isinstance(raw_record["utc"], str):
            raise DataReadError(f"stream state utc is invalid for {symbol}")
        if raw_record["venues"] not in VENUE_VALUES:
            raise DataReadError(f"stream state venues is invalid for {symbol}")
        for field in NUMERIC_FIELDS:
            _validate_numeric_text(raw_record[field], f"state {symbol}.{field}")
        if raw_record["venues"] != "both" and raw_record["basis_gap_pct"] is not None:
            raise DataReadError(f"stream state basis is invalid for {symbol}")
        state[symbol] = dict(raw_record)
    return state


def _empty_sidecar(window: int) -> Dict[str, Any]:
    return {
        "version": SIDECAR_VERSION,
        "window": window,
        "rotation_cursor": 0,
        "pass_number": 0,
        "symbols": {},
    }


def load_sidecar(path: Path, window: int) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return _empty_sidecar(window)
    payload = _read_json_file(path, "stream rolling-window sidecar")
    if not isinstance(payload, dict) or payload.get("version") != SIDECAR_VERSION:
        raise DataReadError("stream rolling-window sidecar has unsupported version")
    raw_symbols = payload.get("symbols")
    cursor = payload.get("rotation_cursor")
    pass_number = payload.get("pass_number")
    if (
        not isinstance(raw_symbols, dict)
        or isinstance(cursor, bool)
        or not isinstance(cursor, int)
        or cursor < 0
        or isinstance(pass_number, bool)
        or not isinstance(pass_number, int)
        or pass_number < 0
    ):
        raise DataReadError("stream rolling-window sidecar has invalid metadata")

    clean_symbols: Dict[str, Dict[str, Any]] = {}
    for raw_symbol, raw_meta in raw_symbols.items():
        symbol = _valid_symbol(raw_symbol)
        if symbol is None or not isinstance(raw_meta, dict):
            raise DataReadError("stream rolling-window sidecar has invalid symbol")
        raw_closed = raw_meta.get("closed_1m", [])
        if not isinstance(raw_closed, list):
            raise DataReadError(f"sidecar closed_1m is invalid for {symbol}")
        by_close: Dict[int, str] = {}
        for pair in raw_closed:
            if not isinstance(pair, list) or len(pair) != 2:
                raise DataReadError(f"sidecar candle is invalid for {symbol}")
            try:
                close_ms = int(str(pair[0]))
            except (TypeError, ValueError):
                raise DataReadError(f"sidecar candle time is invalid for {symbol}") from None
            volume = decimal_value(pair[1], f"sidecar {symbol} volume")
            if close_ms < 0 or volume < 0:
                raise DataReadError(f"sidecar candle is invalid for {symbol}")
            by_close[close_ms] = decimal_string(volume)
        clean_meta: Dict[str, Any] = {
            "closed_1m": [
                [str(close_ms), by_close[close_ms]]
                for close_ms in sorted(by_close)[-window:]
            ]
        }
        for field in ("last_volume_close_ms", "last_oi_ms"):
            raw_value = raw_meta.get(field)
            if raw_value is not None:
                try:
                    value = int(str(raw_value))
                except (TypeError, ValueError):
                    raise DataReadError(f"sidecar {field} is invalid for {symbol}") from None
                if value < 0:
                    raise DataReadError(f"sidecar {field} is invalid for {symbol}")
                clean_meta[field] = str(value)
        clean_symbols[symbol] = clean_meta
    return {
        "version": SIDECAR_VERSION,
        "window": window,
        "rotation_cursor": cursor,
        "pass_number": pass_number,
        "symbols": clean_symbols,
    }


def _require_list(payload: Any, source: str) -> List[Any]:
    if not isinstance(payload, list):
        raise DataReadError(f"{source} bulk response is not a list")
    return payload


def book_map(payload: Any, source: str) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for raw in _require_list(payload, source):
        if not isinstance(raw, dict):
            continue
        symbol = _valid_symbol(raw.get("symbol"))
        if symbol is None:
            continue
        if symbol in result:
            raise DataReadError(f"{source} bulk response repeats {symbol}")
        result[symbol] = dict(raw)
    return result


def premium_map(payload: Any) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for raw in _require_list(payload, "USD-M premium index"):
        if not isinstance(raw, dict):
            continue
        symbol = _valid_symbol(raw.get("symbol"))
        if symbol is not None:
            result[symbol] = dict(raw)
    return result


def venue_for(symbol: str, spot: Mapping[str, Any], perp: Mapping[str, Any]) -> str:
    if symbol in spot and symbol in perp:
        return "both"
    if symbol in spot:
        return "spot"
    return "perp"


def _book_prices(row: Optional[Mapping[str, Any]], source: str) -> Tuple[Optional[str], Optional[str]]:
    if row is None:
        return None, None
    bid = optional_decimal_string(row, "bidPrice", source, positive=True)
    ask = optional_decimal_string(row, "askPrice", source, positive=True)
    if bid is not None and ask is not None:
        if decimal_value(bid, source) > decimal_value(ask, source):
            return None, None
    return bid, ask


def select_focus(
    symbols: Sequence[str],
    move_scores: Mapping[str, Decimal],
    cap: int,
    cursor: int,
) -> FocusSelection:
    """Choose bounded movers plus a starvation-free circular rotation."""
    ordered = sorted(set(symbols))
    if not ordered or cap <= 0:
        return FocusSelection((), 0, 0, 0, 0)
    selected_cap = min(cap, len(ordered))
    mover_slots = selected_cap // 2
    rotation_slots = selected_cap - mover_slots
    start = cursor % len(ordered)
    rotating = tuple(
        ordered[(start + offset) % len(ordered)] for offset in range(rotation_slots)
    )
    rotating_set = set(rotating)
    ranked = sorted(
        (symbol for symbol in ordered if symbol not in rotating_set),
        key=lambda symbol: (-move_scores.get(symbol, Decimal(0)), symbol),
    )
    movers = tuple(ranked[:mover_slots])
    selected = rotating + movers
    return FocusSelection(
        symbols=selected,
        next_cursor=(start + rotation_slots) % len(ordered),
        mover_slots=len(movers),
        rotation_slots=rotation_slots,
        coverage_passes=math.ceil(len(ordered) / rotation_slots),
    )


def parse_closed_klines(payload: Any, now_ms: int, source: str) -> List[Tuple[int, Decimal]]:
    rows = _require_list(payload, source)
    by_close: Dict[int, Decimal] = {}
    for raw in rows:
        if not isinstance(raw, list) or len(raw) < 8:
            continue
        try:
            close_ms = int(str(raw[6]))
            quote_volume = decimal_value(raw[7], f"{source} quote volume")
        except (TypeError, ValueError, DataReadError):
            continue
        if 0 <= close_ms < now_ms and quote_volume >= 0:
            by_close[close_ms] = quote_volume
    if not by_close:
        raise DataReadError(f"{source} has no valid closed 1m candle")
    return [(close_ms, by_close[close_ms]) for close_ms in sorted(by_close)]


def merge_closed_window(
    existing: Sequence[Sequence[Any]],
    incoming: Sequence[Tuple[int, Decimal]],
    window: int,
) -> List[List[str]]:
    merged: Dict[int, Decimal] = {}
    for pair in existing:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            try:
                merged[int(str(pair[0]))] = decimal_value(pair[1], "stored volume")
            except (TypeError, ValueError, DataReadError):
                continue
    for close_ms, volume in incoming:
        merged[close_ms] = volume
    return [
        [str(close_ms), decimal_string(merged[close_ms])]
        for close_ms in sorted(merged)[-window:]
    ]


def volume_metrics(
    closed: Sequence[Sequence[Any]], window: int
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if not closed:
        return None, None, None
    volumes = [decimal_value(pair[1], "rolling quote volume") for pair in closed]
    latest = volumes[-1]
    if len(volumes) < window:
        return decimal_string(latest), None, None
    sample = volumes[-window:]
    med = median(sample)
    if med == 0:
        ratio = None
    else:
        with localcontext() as context:
            context.prec = 50
            ratio = latest / med
    return (
        decimal_string(latest),
        decimal_string(med),
        None if ratio is None else decimal_string(ratio),
    )


def _reference_price(record: Mapping[str, Any]) -> Optional[Decimal]:
    for field in ("perp_bid", "spot_bid"):
        raw = record.get(field)
        if raw is not None:
            try:
                value = decimal_value(raw, field)
            except DataReadError:
                continue
            if value > 0:
                return value
    return None


def move_score(current: Mapping[str, Any], previous: Optional[Mapping[str, Any]]) -> Decimal:
    if previous is None:
        return Decimal(0)
    current_price = _reference_price(current)
    previous_price = _reference_price(previous)
    if current_price is None or previous_price is None or previous_price == 0:
        return Decimal(0)
    with localcontext() as context:
        context.prec = 50
        return abs((current_price - previous_price) / previous_price)


def _basis_gap(spot_ask: Optional[str], perp_bid: Optional[str]) -> Optional[str]:
    if spot_ask is None or perp_bid is None:
        return None
    ask = decimal_value(spot_ask, "spot ask")
    bid = decimal_value(perp_bid, "perpetual bid")
    if ask <= 0:
        return None
    with localcontext() as context:
        context.prec = 50
        return decimal_string((bid - ask) / ask * Decimal(100))


def _oi_value(payload: Any, symbol: str) -> str:
    if not isinstance(payload, dict) or payload.get("symbol") != symbol:
        raise DataReadError(f"USD-M open interest response mismatch for {symbol}")
    value = decimal_value(payload.get("openInterest"), f"{symbol} open interest")
    if value < 0:
        raise DataReadError(f"USD-M open interest is negative for {symbol}")
    return decimal_string(value)


def _oi_change(current: str, previous: Optional[Any]) -> Optional[str]:
    if previous is None:
        return None
    try:
        old = decimal_value(previous, "previous open interest")
        new = decimal_value(current, "current open interest")
    except DataReadError:
        return None
    if old == 0:
        return None
    with localcontext() as context:
        context.prec = 50
        return decimal_string((new - old) / old * Decimal(100))


def _threshold_events(
    utc: str,
    symbol: str,
    previous: Optional[Any],
    current: Optional[Any],
    thresholds: Sequence[Decimal],
) -> List[Dict[str, Any]]:
    if previous is None or current is None:
        return []
    try:
        old = decimal_value(previous, "previous surge ratio")
        new = decimal_value(current, "current surge ratio")
    except DataReadError:
        return []
    events: List[Dict[str, Any]] = []
    for threshold in thresholds:
        direction: Optional[str] = None
        if old < threshold <= new:
            direction = "up"
        elif old >= threshold > new:
            direction = "down"
        if direction is not None:
            events.append(
                {
                    "utc": utc,
                    "symbol": symbol,
                    "transition": "surge_threshold_crossed",
                    "direction": direction,
                    "threshold": decimal_string(threshold),
                    "from_ratio": decimal_string(old),
                    "to_ratio": decimal_string(new),
                }
            )
    return events


class MarketStream:
    """Stateful pass runner with injected reads and clocks for offline tests."""

    def __init__(
        self,
        state_path: Path = DEFAULT_STATE_PATH,
        log_path: Path = DEFAULT_LOG_PATH,
        windows_path: Optional[Path] = None,
        *,
        window: int = DEFAULT_WINDOW,
        focus_cap: int = DEFAULT_FOCUS_CAP,
        thresholds: Sequence[Decimal] = DEFAULT_THRESHOLDS,
        reader: Optional[Callable[[str, str, Mapping[str, str]], Any]] = None,
        utc_fn: Callable[[], str] = now_utc,
        ms_fn: Callable[[], int] = epoch_ms,
        request_spacing: float = DEFAULT_REQUEST_SPACING_SECONDS,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        if not 2 <= window <= MAX_WINDOW:
            raise ValueError(f"window must be between 2 and {MAX_WINDOW}")
        if not 1 <= focus_cap <= MAX_FOCUS_CAP:
            raise ValueError(f"focus_cap must be between 1 and {MAX_FOCUS_CAP}")
        normalized_thresholds = tuple(sorted(set(thresholds)))
        if not normalized_thresholds or any(
            not threshold.is_finite() or threshold <= 0
            for threshold in normalized_thresholds
        ):
            raise ValueError("surge thresholds must be finite and positive")
        if request_spacing < 0:
            raise ValueError("request_spacing cannot be negative")

        self.state_path = Path(state_path)
        self.log_path = Path(log_path)
        self.windows_path = (
            Path(windows_path)
            if windows_path is not None
            else self.state_path.with_name(DEFAULT_WINDOWS_NAME)
        )
        self.window = window
        self.focus_cap = focus_cap
        self.thresholds = normalized_thresholds
        self.reader = reader if reader is not None else PublicJsonReader()
        self.utc_fn = utc_fn
        self.ms_fn = ms_fn
        self.request_spacing = request_spacing
        self.sleep_fn = sleep_fn
        self.state = load_state(self.state_path)
        self.sidecar = load_sidecar(self.windows_path, self.window)
        self._request_count = 0
        self._request_kinds: Dict[str, int] = {"bulk": 0, "kline": 0, "oi": 0}
        self._last_request_monotonic: Optional[float] = None

    def _read(
        self, kind: str, base_url: str, path: str, params: Mapping[str, str]
    ) -> Any:
        if self._last_request_monotonic is not None and self.request_spacing:
            elapsed = time.monotonic() - self._last_request_monotonic
            remaining = self.request_spacing - elapsed
            if remaining > 0:
                self.sleep_fn(remaining)
        try:
            return self.reader(base_url, path, params)
        finally:
            self._request_count += 1
            self._request_kinds[kind] += 1
            self._last_request_monotonic = time.monotonic()

    def _base_record(
        self,
        symbol: str,
        venue: str,
        spot_row: Optional[Mapping[str, Any]],
        perp_row: Optional[Mapping[str, Any]],
        premium_row: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        spot_bid, spot_ask = _book_prices(spot_row, f"Spot {symbol}")
        perp_bid, perp_ask = _book_prices(perp_row, f"USD-M {symbol}")
        mark: Optional[str] = None
        funding_rate: Optional[str] = None
        next_funding_ms: Optional[str] = None
        if venue in {"perp", "both"} and premium_row is not None:
            mark = optional_decimal_string(
                premium_row, "markPrice", f"USD-M premium {symbol}", positive=True
            )
            funding_rate = optional_decimal_string(
                premium_row, "lastFundingRate", f"USD-M premium {symbol}"
            )
            next_funding_ms = integer_string(
                premium_row, "nextFundingTime", f"USD-M premium {symbol}"
            )
        meta = self.sidecar["symbols"].get(symbol, {})
        vol, vol_med, surge = volume_metrics(meta.get("closed_1m", []), self.window)
        previous = self.state.get(symbol, {})
        previous_oi = previous.get("oi") if venue in {"perp", "both"} else None
        return {
            "utc": "",
            "venues": venue,
            "spot_bid": spot_bid if venue in {"spot", "both"} else None,
            "spot_ask": spot_ask if venue in {"spot", "both"} else None,
            "perp_bid": perp_bid if venue in {"perp", "both"} else None,
            "perp_ask": perp_ask if venue in {"perp", "both"} else None,
            "mark": mark,
            "vol_1m_quote": vol,
            "vol_med_1m": vol_med,
            "surge_ratio": surge,
            "oi": previous_oi,
            "oi_change_pct": None,
            "funding_rate": funding_rate,
            "next_funding_ms": next_funding_ms,
            "basis_gap_pct": _basis_gap(spot_ask, perp_bid) if venue == "both" else None,
            "freshness_ms": "0",
        }

    def _focus_symbol(
        self,
        symbol: str,
        record: Dict[str, Any],
        pass_now_ms: int,
    ) -> int:
        failures = 0
        meta = self.sidecar["symbols"].setdefault(symbol, {"closed_1m": []})
        if record["venues"] in {"perp", "both"}:
            kline_base, kline_path = FAPI_BASE_URL, FAPI_KLINES_PATH
        else:
            kline_base, kline_path = SPOT_BASE_URL, SPOT_KLINES_PATH
        try:
            payload = self._read(
                "kline",
                kline_base,
                kline_path,
                {"symbol": symbol, "interval": "1m", "limit": str(self.window + 2)},
            )
            closed = parse_closed_klines(payload, pass_now_ms, f"1m klines {symbol}")
            meta["closed_1m"] = merge_closed_window(
                meta.get("closed_1m", []), closed, self.window
            )
            meta["last_volume_close_ms"] = str(int(meta["closed_1m"][-1][0]))
            vol, vol_med, surge = volume_metrics(meta["closed_1m"], self.window)
            record["vol_1m_quote"] = vol
            record["vol_med_1m"] = vol_med
            record["surge_ratio"] = surge
        except MarketStreamError:
            failures += 1

        if record["venues"] in {"perp", "both"}:
            try:
                payload = self._read(
                    "oi", FAPI_BASE_URL, FAPI_OI_PATH, {"symbol": symbol}
                )
                current_oi = _oi_value(payload, symbol)
                previous_oi = self.state.get(symbol, {}).get("oi")
                record["oi"] = current_oi
                record["oi_change_pct"] = _oi_change(current_oi, previous_oi)
                meta["last_oi_ms"] = str(pass_now_ms)
            except MarketStreamError:
                failures += 1
        else:
            record["oi"] = None
            record["oi_change_pct"] = None
        return failures

    def run_once(self) -> PassStats:
        self._request_count = 0
        self._request_kinds = {"bulk": 0, "kline": 0, "oi": 0}
        self._last_request_monotonic = None
        pass_start_ms = self.ms_fn()

        spot_payload = self._read("bulk", SPOT_BASE_URL, SPOT_BOOK_PATH, {})
        perp_payload = self._read("bulk", FAPI_BASE_URL, FAPI_BOOK_PATH, {})
        premium_payload = self._read("bulk", FAPI_BASE_URL, FAPI_PREMIUM_PATH, {})
        spot = book_map(spot_payload, "Spot book ticker")
        perp = book_map(perp_payload, "USD-M book ticker")
        premium = premium_map(premium_payload)
        universe = sorted(set(spot) | set(perp))
        if not universe:
            raise DataReadError("bulk reads produced an empty USDT universe")

        records: Dict[str, Dict[str, Any]] = {}
        for symbol in universe:
            venue = venue_for(symbol, spot, perp)
            records[symbol] = self._base_record(
                symbol,
                venue,
                spot.get(symbol),
                perp.get(symbol),
                premium.get(symbol),
            )
        scores = {
            symbol: move_score(records[symbol], self.state.get(symbol))
            for symbol in universe
        }
        selection = select_focus(
            universe,
            scores,
            self.focus_cap,
            self.sidecar["rotation_cursor"],
        )
        focus_failures = 0
        focus_now_ms = self.ms_fn()
        for symbol in selection.symbols:
            focus_failures += self._focus_symbol(symbol, records[symbol], focus_now_ms)

        completed_ms = self.ms_fn()
        completed_utc = self.utc_fn()
        for symbol, record in records.items():
            meta = self.sidecar["symbols"].get(symbol, {})
            ages = [max(0, completed_ms - pass_start_ms)]
            if record["vol_1m_quote"] is not None and meta.get("last_volume_close_ms"):
                ages.append(max(0, completed_ms - int(meta["last_volume_close_ms"])))
            if record["oi"] is not None and meta.get("last_oi_ms"):
                ages.append(max(0, completed_ms - int(meta["last_oi_ms"])))
            record["utc"] = completed_utc
            record["freshness_ms"] = str(max(ages))
            # Construction order and key set are frozen for downstream consumers.
            records[symbol] = {field: record[field] for field in STATE_FIELDS}

        events: List[Dict[str, Any]] = []
        for symbol in universe:
            old = self.state.get(symbol)
            new = records[symbol]
            if old is None:
                events.append(
                    {
                        "utc": completed_utc,
                        "symbol": symbol,
                        "transition": "new_listing_seen",
                        "venues": new["venues"],
                    }
                )
            elif old["venues"] != new["venues"]:
                events.append(
                    {
                        "utc": completed_utc,
                        "symbol": symbol,
                        "transition": "venue_change",
                        "from_venues": old["venues"],
                        "to_venues": new["venues"],
                    }
                )
            events.extend(
                _threshold_events(
                    completed_utc,
                    symbol,
                    None if old is None else old.get("surge_ratio"),
                    new.get("surge_ratio"),
                    self.thresholds,
                )
            )

        self.sidecar["rotation_cursor"] = selection.next_cursor
        self.sidecar["pass_number"] += 1
        # A disappeared symbol is omitted from public state, but its bounded
        # history is retained in the sidecar in case the listing returns.
        atomic_write_json(self.windows_path, self.sidecar)
        atomic_write_json(self.state_path, records)
        append_events(self.log_path, events)
        self.state = records

        counts = {
            venue: sum(record["venues"] == venue for record in records.values())
            for venue in VENUE_VALUES
        }
        ranked_surges = sorted(
            (
                (symbol, record["surge_ratio"])
                for symbol, record in records.items()
                if record["surge_ratio"] is not None
            ),
            key=lambda item: (-decimal_value(item[1], "surge ratio"), item[0]),
        )
        selected_cap = min(self.focus_cap, len(universe))
        return PassStats(
            utc=completed_utc,
            total=len(records),
            spot_only=counts["spot"],
            perp_only=counts["perp"],
            both=counts["both"],
            request_count=self._request_count,
            request_budget=3 + (2 * selected_cap),
            bulk_requests=self._request_kinds["bulk"],
            kline_requests=self._request_kinds["kline"],
            oi_requests=self._request_kinds["oi"],
            focus_failures=focus_failures,
            focus_cap=self.focus_cap,
            focus_selected=len(selection.symbols),
            mover_slots=selection.mover_slots,
            rotation_slots=selection.rotation_slots,
            coverage_passes=selection.coverage_passes,
            window=self.window,
            top_surges=tuple(ranked_surges[:5]),
        )


def print_stats(stats: PassStats) -> None:
    print(f"pass_utc={stats.utc}")
    print(
        "symbols_seen "
        f"total={stats.total} spot_only={stats.spot_only} "
        f"perp_only={stats.perp_only} both={stats.both}"
    )
    print(
        "requests "
        f"actual={stats.request_count} budget={stats.request_budget} "
        f"bulk={stats.bulk_requests} klines={stats.kline_requests} "
        f"oi={stats.oi_requests} focus_failures={stats.focus_failures}"
    )
    print(
        "focus_rotation "
        f"cap={stats.focus_cap} selected={stats.focus_selected} "
        f"mover_slots={stats.mover_slots} rotation_slots={stats.rotation_slots} "
        f"stable_universe_coverage_passes={stats.coverage_passes}"
    )
    print(
        f"volume window_closed_1m={stats.window} "
        "source=perp_when_available_else_spot"
    )
    if stats.top_surges:
        rendered = ",".join(f"{symbol}:{ratio}" for symbol, ratio in stats.top_surges)
    else:
        rendered = "none_window_short_or_unsampled"
    print(f"top_surge_ratios={rendered}")


def positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be numeric") from None
    if not 0 < value <= MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"value must be greater than 0 and at most {MAX_TIMEOUT_SECONDS:g}"
        )
    return value


def loop_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("loop interval must be numeric") from None
    if not 0 < value <= 86_400:
        raise argparse.ArgumentTypeError(
            "loop interval must be greater than 0 and at most 86400 seconds"
        )
    return value


def nonnegative_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be numeric") from None
    if value < 0 or value > 5:
        raise argparse.ArgumentTypeError("value must be between 0 and 5")
    return value


def bounded_int(low: int, high: int, label: str) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{label} must be an integer") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f"{label} must be between {low} and {high}"
            )
        return value

    return parse


def threshold_list(raw: str) -> Tuple[Decimal, ...]:
    values: List[Decimal] = []
    for part in raw.split(","):
        try:
            value = Decimal(part.strip())
        except InvalidOperation:
            raise argparse.ArgumentTypeError("thresholds must be comma-separated decimals") from None
        if not value.is_finite() or value <= 0:
            raise argparse.ArgumentTypeError("thresholds must be finite and positive")
        values.append(value)
    if not values:
        raise argparse.ArgumentTypeError("at least one threshold is required")
    return tuple(sorted(set(values)))


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain one public Spot + USD-M USDT market stream."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run exactly one pass")
    mode.add_argument(
        "--loop",
        type=loop_seconds,
        metavar="SECONDS",
        help="run repeatedly at this pass-start interval",
    )
    parser.add_argument("--stop-file", type=Path, help="loop exits when this path exists")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--windows-file", type=Path)
    parser.add_argument(
        "--window",
        type=bounded_int(2, MAX_WINDOW, "window"),
        default=DEFAULT_WINDOW,
    )
    parser.add_argument(
        "--focus-cap",
        type=bounded_int(1, MAX_FOCUS_CAP, "focus cap"),
        default=DEFAULT_FOCUS_CAP,
    )
    parser.add_argument(
        "--surge-thresholds",
        type=threshold_list,
        default=DEFAULT_THRESHOLDS,
        metavar="RATIOS",
    )
    parser.add_argument(
        "--timeout", type=positive_float, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--request-spacing",
        type=nonnegative_float,
        default=DEFAULT_REQUEST_SPACING_SECONDS,
        metavar="SECONDS",
    )
    return parser


def _wait_until_next(started: float, interval: float, stop_file: Optional[Path]) -> bool:
    deadline = started + interval
    while True:
        if stop_file is not None and stop_file.exists():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(remaining, 0.25))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argument_parser()
    args = parser.parse_args(argv)
    if args.once and args.stop_file is not None:
        parser.error("--stop-file is only valid with --loop")
    windows_path = args.windows_file
    if windows_path is None:
        windows_path = args.state_file.with_name(DEFAULT_WINDOWS_NAME)
    try:
        stream = MarketStream(
            state_path=args.state_file,
            log_path=args.log_file,
            windows_path=windows_path,
            window=args.window,
            focus_cap=args.focus_cap,
            thresholds=args.surge_thresholds,
            reader=PublicJsonReader(args.timeout),
            request_spacing=args.request_spacing,
        )
        if args.once:
            print_stats(stream.run_once())
            return 0
        while args.stop_file is None or not args.stop_file.exists():
            started = time.monotonic()
            print_stats(stream.run_once())
            if not _wait_until_next(started, args.loop, args.stop_file):
                break
        return 0
    except KeyboardInterrupt:
        return 130
    except MarketStreamError as exc:
        print(f"market_stream error: {exc}", file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print(f"market_stream filesystem error: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
