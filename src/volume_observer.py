#!/usr/bin/env python3
"""Volume-lane observer v2: public observation rows plus a PAPER-ONLY lifecycle.

Strategy 2 groundwork, paper only end to end.

Part 1 (observe): unsigned, credential-free public GETs to api.binance.com
/api/v3/ticker/24hr over a small fixed symbol list.  Prints plain factual
observation rows (volume, quote volume, price change).  Factual states only —
no predictions and no scores presented as facts.

Part 2 (paper): a deterministic paper lifecycle driven by a supplied price
series — one paper entry at a small fixed paper notional, then one trailing
exit — appending paper receipts as JSON lines.  Every receipt carries
"paper": true.

Part 3 (scan, v2): CLOSED-CANDLE volume-surge detection over /api/v3/klines.
Only candles whose close time has already passed are evaluated; the candle
still forming is never used.  The surge rule is factual and parameter-explicit:

    surge iff closed-candle quote volume >= surge_multiple *
        median(quote volume of the prior baseline_candles closed candles)

No predictions and no scores-as-facts: every printed value is an observed
number, a stated parameter, or arithmetic on those, and "surged" is only the
stated comparison's truth value.

Part 4 (universe, v2): live SPOT-USDT universe discovery from
/api/v3/exchangeInfo — status TRADING, quote asset USDT, spot trading allowed.
The whole universe costs exactly one unsigned GET; the number of follow-up
klines GETs per scan is capped (cap stated in the output) and requests are
spaced — never hammered.

Part 5 (paper positions, v2): restart-safe PAPER positions.  Open paper
positions persist in ONE JSON state file inside the worktree, are reloaded on
start, and survive kill/restart without duplication or loss: the state file is
written atomically (temp file + os.replace), a per-symbol closed-candle
watermark makes every action idempotent (re-scanning an already-processed
candle performs no action), and an existing open position blocks a second
entry for the same symbol.  Every stored record carries "paper": true.

There is no real order path anywhere in this module: no credentials, no
signing, no POST, no order endpoint; the only network calls are unsigned
public GETs to api.binance.com, and the paper lifecycle itself never touches
the network at all.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PUBLIC_BASE_URL = "https://api.binance.com"
TICKER_24H_PATH = "/api/v3/ticker/24hr"
KLINES_PATH = "/api/v3/klines"
EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
FIXED_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 1_000_000
MAX_UNIVERSE_RESPONSE_BYTES = 16_000_000
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{5,30}")
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
EXIT_INPUT = 2
EXIT_HTTP = 3
EXIT_DATA = 4

DEFAULT_PAPER_NOTIONAL = Decimal("10")
MAX_PAPER_NOTIONAL = Decimal("100")
DEFAULT_TRAIL_PCT = Decimal("0.01")
QTY_QUANTUM = Decimal("0.00000001")
MAX_PRICES = 10_000
DEFAULT_RECEIPT_PATH = Path(__file__).resolve().parent.parent / "paper_receipts.jsonl"
DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "paper_state.json"
PAPER_NOTE = "paper only; no real order path"
STATE_VERSION = 1

KLINE_INTERVALS = (
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
)
DEFAULT_INTERVAL = "5m"
DEFAULT_BASELINE_CANDLES = 20
MIN_BASELINE_CANDLES = 3
MAX_BASELINE_CANDLES = 500
DEFAULT_SURGE_MULTIPLE = Decimal("3")
MIN_SURGE_MULTIPLE = Decimal("1")
MAX_SURGE_MULTIPLE = Decimal("100")
DEFAULT_SCAN_SYMBOLS = 6
MAX_SCAN_SYMBOLS = 25
REQUEST_SPACING_SECONDS = 0.25

SURGE_RULE_TEXT = (
    "surge iff closed_candle_quote_volume >= surge_multiple * "
    "median(quote_volume of prior baseline_candles closed candles); "
    "open (still-forming) candles are never evaluated"
)

STATUS_EVALUATED = "evaluated"
STATUS_INSUFFICIENT = "insufficient_closed_candles"
STATUS_BASELINE_ZERO = "baseline_zero"

ACTION_NONE = "no_action"
ACTION_ENTRY = "paper_entry"
ACTION_EXIT = "paper_exit"
ACTION_UPDATED = "position_updated"
ACTION_SKIPPED = "skipped_candle_already_processed"
ACTION_DISABLED = "paper_disabled"


class VolumeObserverError(Exception):
    """Expected failure with a stable process exit code."""

    exit_code = 1


class HttpReadError(VolumeObserverError):
    exit_code = EXIT_HTTP


class DataReadError(VolumeObserverError):
    exit_code = EXIT_DATA


class PaperInputError(VolumeObserverError):
    exit_code = EXIT_INPUT


@dataclass(frozen=True)
class ObservationRow:
    """One plain factual 24h-window observation for one symbol."""

    completed_utc: str
    window_open_utc: str
    window_close_utc: str
    symbol: str
    last_price: Decimal
    price_change: Decimal
    price_change_percent: Decimal
    high_price: Decimal
    low_price: Decimal
    base_volume: Decimal
    quote_volume: Decimal
    trade_count: int


@dataclass(frozen=True)
class Candle:
    """One validated kline row from /api/v3/klines."""

    open_time_ms: int
    close_time_ms: int
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    base_volume: Decimal
    quote_volume: Decimal
    trade_count: int


@dataclass(frozen=True)
class SurgeCheck:
    """Factual result of the stated closed-candle surge rule for one symbol.

    "surged" is exactly the truth value of the stated comparison — not a
    prediction and not a score.
    """

    checked_utc: str
    symbol: str
    interval: str
    status: str
    closed_candle_count: int
    baseline_candles: int
    surge_multiple: Decimal
    candle_open_utc: Optional[str]
    candle_close_utc: Optional[str]
    candle_quote_volume: Optional[Decimal]
    baseline_median_quote_volume: Optional[Decimal]
    threshold_quote_volume: Optional[Decimal]
    surged: bool


@dataclass
class PaperState:
    """Restart-safe PAPER position state backed by one JSON file.

    positions: symbol -> open paper position record (every record paper:true).
    candle_marks: symbol -> close_time_ms of the newest closed candle already
    processed for that symbol; re-scanning a candle at or below the mark is a
    no-op, which is what makes kill/restart free of duplicated actions.
    """

    positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    candle_marks: Dict[str, int] = field(default_factory=dict)


def fmt(value: Decimal) -> str:
    return format(value, "f")


def fmt_opt(value: Optional[Any]) -> str:
    if value is None:
        return "none"
    if isinstance(value, Decimal):
        return fmt(value)
    return str(value)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def now_epoch_ms() -> int:
    return int(time.time() * 1000)


def utc_from_epoch_ms(epoch_ms: int, source: str) -> str:
    try:
        value = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            milliseconds=epoch_ms
        )
    except (OverflowError, ValueError):
        raise DataReadError(f"{source} response has out-of-range timestamp") from None
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def decimal_field(payload: Dict[str, Any], field_name: str, source: str) -> Decimal:
    """Return one finite Decimal field or identify the malformed source."""
    raw = payload.get(field_name)
    if raw is None or isinstance(raw, bool):
        raise DataReadError(f"{source} response has invalid {field_name}")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise DataReadError(f"{source} response has invalid {field_name}") from None
    if not value.is_finite():
        raise DataReadError(f"{source} response has non-finite {field_name}")
    return value


def integer_field(payload: Dict[str, Any], field_name: str, source: str) -> int:
    raw = payload.get(field_name)
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, str) and raw.isdigit():
        value = int(raw)
    else:
        raise DataReadError(f"{source} response has invalid {field_name}")
    if value < 0:
        raise DataReadError(f"{source} response has invalid {field_name}")
    return value


def fetch_public_payload(
    path: str,
    params: Dict[str, Any],
    timeout: float,
    source: str,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> Any:
    """Perform one bounded, unsigned public GET and return parsed JSON.

    GET only — this module has no POST, no signing, and no credentials.
    """
    url = PUBLIC_BASE_URL + path
    if params:
        url += "?" + urlencode(params)
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "quant-volume-observer/2"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
    except HTTPError as exc:
        raise HttpReadError(f"public HTTP {exc.code} for {source}") from None
    except (
        URLError,
        TimeoutError,
        socket.timeout,
        OSError,
        http.client.HTTPException,
    ) as exc:
        raise HttpReadError(
            f"public network failure for {source}: {type(exc).__name__}"
        ) from None

    if len(raw) > max_bytes:
        raise DataReadError(f"public response too large for {source}")
    try:
        return json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DataReadError(f"public response is invalid JSON for {source}") from None


def fetch_public_json(path: str, symbol: str, timeout: float) -> Dict[str, Any]:
    """One unsigned public GET for one symbol, requiring a JSON object."""
    payload = fetch_public_payload(path, {"symbol": symbol}, timeout, symbol)
    if not isinstance(payload, dict):
        raise DataReadError(f"public response is a non-object for {symbol}")
    return payload


def build_observation_row(
    symbol: str, payload: Dict[str, Any], completed_utc: str
) -> ObservationRow:
    """Validate one 24h-ticker response into a plain factual row."""
    if payload.get("symbol") != symbol:
        raise DataReadError(f"24h response symbol does not match {symbol}")

    last_price = decimal_field(payload, "lastPrice", "24h")
    price_change = decimal_field(payload, "priceChange", "24h")
    price_change_percent = decimal_field(payload, "priceChangePercent", "24h")
    high_price = decimal_field(payload, "highPrice", "24h")
    low_price = decimal_field(payload, "lowPrice", "24h")
    base_volume = decimal_field(payload, "volume", "24h")
    quote_volume = decimal_field(payload, "quoteVolume", "24h")
    open_time_ms = integer_field(payload, "openTime", "24h")
    close_time_ms = integer_field(payload, "closeTime", "24h")
    trade_count = integer_field(payload, "count", "24h")

    if last_price <= 0 or high_price <= 0 or low_price <= 0:
        raise DataReadError(f"non-positive 24h price for {symbol}")
    if high_price < low_price:
        raise DataReadError(f"24h high below 24h low for {symbol}")
    if base_volume < 0 or quote_volume < 0:
        raise DataReadError(f"negative 24h volume for {symbol}")
    if close_time_ms < open_time_ms:
        raise DataReadError(f"24h window closes before it opens for {symbol}")

    return ObservationRow(
        completed_utc=completed_utc,
        window_open_utc=utc_from_epoch_ms(open_time_ms, "24h"),
        window_close_utc=utc_from_epoch_ms(close_time_ms, "24h"),
        symbol=symbol,
        last_price=last_price,
        price_change=price_change,
        price_change_percent=price_change_percent,
        high_price=high_price,
        low_price=low_price,
        base_volume=base_volume,
        quote_volume=quote_volume,
        trade_count=trade_count,
    )


def print_observation_rows(rows: Iterable[ObservationRow]) -> None:
    print("source=" + PUBLIC_BASE_URL + TICKER_24H_PATH)
    print("window=binance_24h_rolling")
    print("rows=factual_observations_only")
    fields = tuple(ObservationRow.__dataclass_fields__)
    print("\t".join(fields))
    for row in rows:
        values = []
        for field_name in fields:
            value = getattr(row, field_name)
            values.append(fmt(value) if isinstance(value, Decimal) else str(value))
        print("\t".join(values))


def observe(timeout: float) -> List[ObservationRow]:
    """Read the fixed symbol list once; factual rows only."""
    rows = []
    for symbol in FIXED_SYMBOLS:
        payload = fetch_public_json(TICKER_24H_PATH, symbol, timeout)
        rows.append(build_observation_row(symbol, payload, now_utc()))
    return rows


# ---------------------------------------------------------------------------
# v2 part 3 — closed-candle klines and the surge rule
# ---------------------------------------------------------------------------


def _kline_cell_decimal(row: Sequence[Any], index: int, symbol: str) -> Decimal:
    try:
        raw = row[index]
    except IndexError:
        raise DataReadError(f"klines row too short for {symbol}") from None
    if raw is None or isinstance(raw, bool):
        raise DataReadError(f"klines cell {index} invalid for {symbol}")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise DataReadError(f"klines cell {index} invalid for {symbol}") from None
    if not value.is_finite():
        raise DataReadError(f"klines cell {index} non-finite for {symbol}")
    return value


def _kline_cell_int(row: Sequence[Any], index: int, symbol: str) -> int:
    try:
        raw = row[index]
    except IndexError:
        raise DataReadError(f"klines row too short for {symbol}") from None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise DataReadError(f"klines cell {index} invalid for {symbol}")
    if raw < 0:
        raise DataReadError(f"klines cell {index} invalid for {symbol}")
    return raw


def parse_kline_rows(payload: Any, symbol: str) -> List[Candle]:
    """Validate a /api/v3/klines response into chronological Candle rows."""
    if not isinstance(payload, list):
        raise DataReadError(f"klines response is not a list for {symbol}")
    candles: List[Candle] = []
    previous_open = -1
    for row in payload:
        if not isinstance(row, (list, tuple)):
            raise DataReadError(f"klines row is not a list for {symbol}")
        open_time_ms = _kline_cell_int(row, 0, symbol)
        open_price = _kline_cell_decimal(row, 1, symbol)
        high_price = _kline_cell_decimal(row, 2, symbol)
        low_price = _kline_cell_decimal(row, 3, symbol)
        close_price = _kline_cell_decimal(row, 4, symbol)
        base_volume = _kline_cell_decimal(row, 5, symbol)
        close_time_ms = _kline_cell_int(row, 6, symbol)
        quote_volume = _kline_cell_decimal(row, 7, symbol)
        trade_count = _kline_cell_int(row, 8, symbol)

        if open_price <= 0 or high_price <= 0 or low_price <= 0 or close_price <= 0:
            raise DataReadError(f"non-positive kline price for {symbol}")
        if high_price < low_price:
            raise DataReadError(f"kline high below low for {symbol}")
        if base_volume < 0 or quote_volume < 0:
            raise DataReadError(f"negative kline volume for {symbol}")
        if close_time_ms <= open_time_ms:
            raise DataReadError(f"kline closes at or before open for {symbol}")
        if open_time_ms <= previous_open:
            raise DataReadError(f"klines out of order for {symbol}")
        previous_open = open_time_ms

        candles.append(
            Candle(
                open_time_ms=open_time_ms,
                close_time_ms=close_time_ms,
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                base_volume=base_volume,
                quote_volume=quote_volume,
                trade_count=trade_count,
            )
        )
    return candles


def closed_candles_only(candles: Sequence[Candle], now_ms: int) -> List[Candle]:
    """Only candles whose close time has already passed; never the forming one."""
    return [candle for candle in candles if candle.close_time_ms < now_ms]


def decimal_median(values: Sequence[Decimal]) -> Decimal:
    """Deterministic median of Decimals (mean of the two middles when even)."""
    if not values:
        raise PaperInputError("median of empty sequence")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    with localcontext() as context:
        context.prec = 50
        return (ordered[middle - 1] + ordered[middle]) / 2


def fetch_klines(
    symbol: str, interval: str, limit: int, timeout: float
) -> List[Candle]:
    """One unsigned public GET of recent klines for one symbol."""
    if interval not in KLINE_INTERVALS:
        raise PaperInputError(f"unsupported interval: {interval!r}")
    if not 1 <= limit <= 1000:
        raise PaperInputError("klines limit must be between 1 and 1000")
    payload = fetch_public_payload(
        KLINES_PATH,
        {"symbol": symbol, "interval": interval, "limit": limit},
        timeout,
        f"klines {symbol}",
    )
    return parse_kline_rows(payload, symbol)


def evaluate_surge(
    symbol: str,
    interval: str,
    closed: Sequence[Candle],
    baseline_candles: int,
    surge_multiple: Decimal,
    checked_utc: str,
) -> SurgeCheck:
    """Apply the stated surge rule to the newest CLOSED candle.

    Factual outcome only: with fewer than baseline_candles + 1 closed candles
    the status is insufficient_closed_candles; with a zero baseline median the
    comparison is degenerate and reported as baseline_zero with surged false.
    """
    if not MIN_BASELINE_CANDLES <= baseline_candles <= MAX_BASELINE_CANDLES:
        raise PaperInputError(
            f"baseline_candles must be between {MIN_BASELINE_CANDLES} "
            f"and {MAX_BASELINE_CANDLES}"
        )
    if (
        not isinstance(surge_multiple, Decimal)
        or not surge_multiple.is_finite()
        or not MIN_SURGE_MULTIPLE < surge_multiple <= MAX_SURGE_MULTIPLE
    ):
        raise PaperInputError(
            f"surge_multiple must be a Decimal above {fmt(MIN_SURGE_MULTIPLE)} "
            f"and at most {fmt(MAX_SURGE_MULTIPLE)}"
        )
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise PaperInputError(f"invalid symbol: {symbol!r}")

    if len(closed) < baseline_candles + 1:
        return SurgeCheck(
            checked_utc=checked_utc,
            symbol=symbol,
            interval=interval,
            status=STATUS_INSUFFICIENT,
            closed_candle_count=len(closed),
            baseline_candles=baseline_candles,
            surge_multiple=surge_multiple,
            candle_open_utc=None,
            candle_close_utc=None,
            candle_quote_volume=None,
            baseline_median_quote_volume=None,
            threshold_quote_volume=None,
            surged=False,
        )

    candle = closed[-1]
    baseline = closed[-1 - baseline_candles : -1]
    baseline_median = decimal_median([c.quote_volume for c in baseline])
    candle_open_utc = utc_from_epoch_ms(candle.open_time_ms, "klines")
    candle_close_utc = utc_from_epoch_ms(candle.close_time_ms, "klines")

    if baseline_median <= 0:
        return SurgeCheck(
            checked_utc=checked_utc,
            symbol=symbol,
            interval=interval,
            status=STATUS_BASELINE_ZERO,
            closed_candle_count=len(closed),
            baseline_candles=baseline_candles,
            surge_multiple=surge_multiple,
            candle_open_utc=candle_open_utc,
            candle_close_utc=candle_close_utc,
            candle_quote_volume=candle.quote_volume,
            baseline_median_quote_volume=baseline_median,
            threshold_quote_volume=None,
            surged=False,
        )

    with localcontext() as context:
        context.prec = 50
        threshold = baseline_median * surge_multiple
    return SurgeCheck(
        checked_utc=checked_utc,
        symbol=symbol,
        interval=interval,
        status=STATUS_EVALUATED,
        closed_candle_count=len(closed),
        baseline_candles=baseline_candles,
        surge_multiple=surge_multiple,
        candle_open_utc=candle_open_utc,
        candle_close_utc=candle_close_utc,
        candle_quote_volume=candle.quote_volume,
        baseline_median_quote_volume=baseline_median,
        threshold_quote_volume=threshold,
        surged=candle.quote_volume >= threshold,
    )


# ---------------------------------------------------------------------------
# v2 part 4 — live SPOT-USDT universe from exchangeInfo
# ---------------------------------------------------------------------------


def parse_spot_usdt_universe(payload: Any) -> List[str]:
    """TRADING symbols quoted in USDT with spot trading allowed, sorted.

    Type corruption is fatal.  A well-typed symbol outside the scannable
    A-Z0-9 charset (live examples measured 2026-08-13: 3-4 character quote
    tickers, CJK-named listings) is a real entry but not scannable here, so
    it is excluded rather than treated as an error.
    """
    if not isinstance(payload, dict):
        raise DataReadError("exchangeInfo response is not an object")
    symbols = payload.get("symbols")
    if not isinstance(symbols, list):
        raise DataReadError("exchangeInfo response has no symbols list")
    universe = set()
    for entry in symbols:
        if not isinstance(entry, dict):
            raise DataReadError("exchangeInfo symbols entry is not an object")
        symbol = entry.get("symbol")
        status = entry.get("status")
        quote_asset = entry.get("quoteAsset")
        if not isinstance(symbol, str):
            raise DataReadError("exchangeInfo entry has non-string symbol")
        if not isinstance(status, str) or not isinstance(quote_asset, str):
            raise DataReadError(f"exchangeInfo entry invalid for {symbol!r}")
        if status != "TRADING" or quote_asset != "USDT":
            continue
        if entry.get("isSpotTradingAllowed") is False:
            continue
        if not SYMBOL_PATTERN.fullmatch(symbol):
            continue
        universe.add(symbol)
    return sorted(universe)


UNIVERSE_REQUEST_PARAMS = {
    "showPermissionSets": "false",
    "symbolStatus": "TRADING",
}


def fetch_spot_usdt_universe(timeout: float) -> List[str]:
    """Discover the live SPOT-USDT universe with exactly one unsigned GET.

    The reduced payload is requested (no permission sets, TRADING only) —
    measured 2026-08-13: full payload 17.5MB, reduced 2.5MB.  The parser
    still re-checks status/quote/spot-allowed per entry.
    """
    payload = fetch_public_payload(
        EXCHANGE_INFO_PATH,
        dict(UNIVERSE_REQUEST_PARAMS),
        timeout,
        "exchangeInfo",
        max_bytes=MAX_UNIVERSE_RESPONSE_BYTES,
    )
    universe = parse_spot_usdt_universe(payload)
    if not universe:
        raise DataReadError("exchangeInfo yielded an empty SPOT-USDT universe")
    return universe


# ---------------------------------------------------------------------------
# paper sizing shared by the price-series lifecycle and the position store
# ---------------------------------------------------------------------------


def paper_quantity(notional: Decimal, entry_price: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        qty = (notional / entry_price).quantize(QTY_QUANTUM, rounding=ROUND_DOWN)
    if qty <= 0:
        raise PaperInputError(
            "paper notional too small for one quantum at the entry price"
        )
    return qty


def validate_positive_decimal(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise PaperInputError(f"{name} must be a finite positive Decimal")


def validate_paper_inputs(
    symbol: str,
    prices: Sequence[Decimal],
    notional: Decimal,
    trail_pct: Decimal,
    run_id: str,
) -> None:
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise PaperInputError(f"invalid symbol: {symbol!r}")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise PaperInputError("invalid run_id")
    if not prices:
        raise PaperInputError("price series is empty")
    if len(prices) > MAX_PRICES:
        raise PaperInputError(f"price series longer than {MAX_PRICES}")
    for index, price in enumerate(prices):
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise PaperInputError(f"price at index {index} is not a positive Decimal")
    validate_positive_decimal(notional, "paper notional")
    if notional > MAX_PAPER_NOTIONAL:
        raise PaperInputError(
            f"paper notional above fixed cap {fmt(MAX_PAPER_NOTIONAL)}"
        )
    if not isinstance(trail_pct, Decimal) or not trail_pct.is_finite() or not (
        0 < trail_pct < 1
    ):
        raise PaperInputError("trail_pct must be a Decimal strictly between 0 and 1")


def default_run_id() -> str:
    return "paper" + format(int(time.time()), "x") + secrets.token_hex(3)


def append_receipts(receipt_path: Path, records: Iterable[Dict[str, Any]]) -> None:
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with open(receipt_path, "a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def run_paper_lifecycle(
    symbol: str,
    prices: Sequence[Decimal],
    notional: Decimal = DEFAULT_PAPER_NOTIONAL,
    trail_pct: Decimal = DEFAULT_TRAIL_PCT,
    receipt_path: Optional[Path] = None,
    run_id: Optional[str] = None,
    now_fn: Callable[[], str] = now_utc,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Deterministic PAPER entry then trailing PAPER exit over a price series.

    Entry is at the first price for the fixed paper notional.  The trailing
    stop follows the highest price seen since entry; the paper exit happens at
    the first price at or below peak * (1 - trail_pct), else at the final
    price with exit_reason "series_end".  Both receipts are appended to
    receipt_path as JSON lines and every record carries "paper": true.  No
    network, no credentials, no real order path.
    """
    resolved_run_id = run_id if run_id is not None else default_run_id()
    validate_paper_inputs(symbol, prices, notional, trail_pct, resolved_run_id)
    resolved_path = Path(receipt_path) if receipt_path else DEFAULT_RECEIPT_PATH

    entry_price = prices[0]
    qty = paper_quantity(notional, entry_price)
    with localcontext() as context:
        context.prec = 50
        cost = qty * entry_price

        peak_price = entry_price
        exit_index = len(prices) - 1
        exit_price = prices[-1]
        exit_reason = "series_end"
        for index in range(1, len(prices)):
            price = prices[index]
            if price > peak_price:
                peak_price = price
            trail_stop = peak_price * (1 - trail_pct)
            if price <= trail_stop:
                exit_index = index
                exit_price = price
                exit_reason = "trail_stop"
                break
        trail_stop = peak_price * (1 - trail_pct)
        proceeds = qty * exit_price
        pnl_quote = proceeds - cost

    entry_record = {
        "paper": True,
        "kind": "paper_entry",
        "ts": now_fn(),
        "run_id": resolved_run_id,
        "symbol": symbol,
        "side": "BUY",
        "seq": 0,
        "price": fmt(entry_price),
        "qty": fmt(qty),
        "paper_notional_requested": fmt(notional),
        "paper_cost": fmt(cost),
        "trail_pct": fmt(trail_pct),
        "note": PAPER_NOTE,
    }
    exit_record = {
        "paper": True,
        "kind": "paper_exit",
        "ts": now_fn(),
        "run_id": resolved_run_id,
        "symbol": symbol,
        "side": "SELL",
        "seq": exit_index,
        "price": fmt(exit_price),
        "qty": fmt(qty),
        "exit_reason": exit_reason,
        "peak_price": fmt(peak_price),
        "trail_stop_price": fmt(trail_stop),
        "paper_proceeds": fmt(proceeds),
        "paper_pnl_quote": fmt(pnl_quote),
        "note": PAPER_NOTE,
    }
    append_receipts(resolved_path, (entry_record, exit_record))
    return entry_record, exit_record


# ---------------------------------------------------------------------------
# v2 part 5 — restart-safe PAPER positions in one JSON state file
# ---------------------------------------------------------------------------


def _state_decimal(record: Dict[str, Any], field_name: str, symbol: str) -> Decimal:
    value = decimal_field(record, field_name, f"state {symbol}")
    if value <= 0:
        raise DataReadError(f"state {symbol} has non-positive {field_name}")
    return value


def _validate_position_record(symbol: str, record: Any) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise DataReadError(f"state position for {symbol} is not an object")
    if record.get("paper") is not True:
        raise DataReadError(f"state position for {symbol} is not marked paper")
    if record.get("symbol") != symbol:
        raise DataReadError(f"state position symbol mismatch for {symbol}")
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise DataReadError(f"state has invalid symbol {symbol!r}")
    _state_decimal(record, "entry_price", symbol)
    _state_decimal(record, "qty", symbol)
    _state_decimal(record, "peak_price", symbol)
    _state_decimal(record, "paper_cost", symbol)
    trail_pct = decimal_field(record, "trail_pct", f"state {symbol}")
    if not 0 < trail_pct < 1:
        raise DataReadError(f"state {symbol} has out-of-range trail_pct")
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise DataReadError(f"state {symbol} has invalid run_id")
    if not isinstance(record.get("entry_ts"), str):
        raise DataReadError(f"state {symbol} has invalid entry_ts")
    entry_close = record.get("entry_candle_close_ms")
    if isinstance(entry_close, bool) or not isinstance(entry_close, int) or (
        entry_close < 0
    ):
        raise DataReadError(f"state {symbol} has invalid entry_candle_close_ms")
    return record


def load_paper_state(path: Path) -> PaperState:
    """Reload persisted paper positions; a missing file is an empty state.

    A malformed state file raises instead of being silently replaced — the
    no-loss half of the restart guarantee.
    """
    if not path.exists():
        return PaperState()
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DataReadError(f"paper state file unreadable: {path}") from None
    if not isinstance(payload, dict):
        raise DataReadError("paper state file is not an object")
    if payload.get("version") != STATE_VERSION:
        raise DataReadError("paper state file has unsupported version")
    if payload.get("paper") is not True:
        raise DataReadError("paper state file is not marked paper")
    raw_positions = payload.get("positions")
    raw_marks = payload.get("candle_marks")
    if not isinstance(raw_positions, dict) or not isinstance(raw_marks, dict):
        raise DataReadError("paper state file has invalid sections")
    positions: Dict[str, Dict[str, Any]] = {}
    for symbol, record in raw_positions.items():
        positions[symbol] = _validate_position_record(symbol, record)
    candle_marks: Dict[str, int] = {}
    for symbol, mark in raw_marks.items():
        if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
            raise DataReadError("paper state file has invalid mark symbol")
        if isinstance(mark, bool) or not isinstance(mark, int) or mark < 0:
            raise DataReadError(f"paper state file has invalid mark for {symbol}")
        candle_marks[symbol] = mark
    return PaperState(positions=positions, candle_marks=candle_marks)


def save_paper_state(
    path: Path, state: PaperState, now_fn: Callable[[], str] = now_utc
) -> None:
    """Atomically persist the paper state: temp file then os.replace.

    A kill at any moment leaves either the previous complete file or the new
    complete file at path — never a partial write.
    """
    payload = {
        "version": STATE_VERSION,
        "paper": True,
        "updated_utc": now_fn(),
        "positions": state.positions,
        "candle_marks": state.candle_marks,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=1)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def apply_paper_candle(
    state: PaperState,
    check: SurgeCheck,
    last_closed: Candle,
    notional: Decimal,
    trail_pct: Decimal,
    receipt_path: Path,
    run_id: str,
    now_fn: Callable[[], str] = now_utc,
) -> str:
    """One idempotent PAPER action for one symbol from one CLOSED candle.

    Per-symbol watermark: a candle at or below the recorded mark is skipped,
    so a killed and restarted scan never duplicates an entry, update, or exit.
    An open position blocks a second entry for the same symbol.  Order: an
    existing position is updated/exited first; an entry happens only for a
    symbol with no open position, and only on a surged evaluated candle.
    Every receipt and every stored record carries paper:true.
    """
    symbol = check.symbol
    validate_positive_decimal(notional, "paper notional")
    if notional > MAX_PAPER_NOTIONAL:
        raise PaperInputError(
            f"paper notional above fixed cap {fmt(MAX_PAPER_NOTIONAL)}"
        )
    if not isinstance(trail_pct, Decimal) or not trail_pct.is_finite() or not (
        0 < trail_pct < 1
    ):
        raise PaperInputError("trail_pct must be a Decimal strictly between 0 and 1")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise PaperInputError("invalid run_id")

    mark = state.candle_marks.get(symbol, -1)
    if last_closed.close_time_ms <= mark:
        return ACTION_SKIPPED

    candle_close_utc = utc_from_epoch_ms(last_closed.close_time_ms, "klines")
    position = state.positions.get(symbol)
    if position is not None:
        with localcontext() as context:
            context.prec = 50
            peak = Decimal(position["peak_price"])
            if last_closed.close_price > peak:
                peak = last_closed.close_price
            position_trail = Decimal(position["trail_pct"])
            stop = peak * (1 - position_trail)
            qty = Decimal(position["qty"])
            cost = Decimal(position["paper_cost"])
            proceeds = qty * last_closed.close_price
            pnl_quote = proceeds - cost
        position["peak_price"] = fmt(peak)
        if last_closed.close_price <= stop:
            exit_record = {
                "paper": True,
                "kind": "paper_exit",
                "ts": now_fn(),
                "run_id": run_id,
                "entry_run_id": position["run_id"],
                "symbol": symbol,
                "side": "SELL",
                "price": fmt(last_closed.close_price),
                "qty": fmt(qty),
                "exit_reason": "trail_stop_closed_candle",
                "peak_price": fmt(peak),
                "trail_stop_price": fmt(stop),
                "paper_proceeds": fmt(proceeds),
                "paper_pnl_quote": fmt(pnl_quote),
                "candle_close_utc": candle_close_utc,
                "candle_close_ms": last_closed.close_time_ms,
                "note": PAPER_NOTE,
            }
            del state.positions[symbol]
            state.candle_marks[symbol] = last_closed.close_time_ms
            append_receipts(receipt_path, (exit_record,))
            return ACTION_EXIT
        state.candle_marks[symbol] = last_closed.close_time_ms
        return ACTION_UPDATED

    if check.status == STATUS_EVALUATED and check.surged:
        entry_price = last_closed.close_price
        qty = paper_quantity(notional, entry_price)
        with localcontext() as context:
            context.prec = 50
            cost = qty * entry_price
        entry_record = {
            "paper": True,
            "kind": "paper_entry",
            "ts": now_fn(),
            "run_id": run_id,
            "symbol": symbol,
            "side": "BUY",
            "price": fmt(entry_price),
            "qty": fmt(qty),
            "paper_notional_requested": fmt(notional),
            "paper_cost": fmt(cost),
            "trail_pct": fmt(trail_pct),
            "entry_source": "closed_candle_surge",
            "candle_close_utc": candle_close_utc,
            "candle_close_ms": last_closed.close_time_ms,
            "note": PAPER_NOTE,
        }
        state.positions[symbol] = {
            "paper": True,
            "run_id": run_id,
            "symbol": symbol,
            "entry_ts": entry_record["ts"],
            "entry_candle_close_ms": last_closed.close_time_ms,
            "entry_price": fmt(entry_price),
            "qty": fmt(qty),
            "paper_notional_requested": fmt(notional),
            "paper_cost": fmt(cost),
            "trail_pct": fmt(trail_pct),
            "peak_price": fmt(entry_price),
            "note": PAPER_NOTE,
        }
        state.candle_marks[symbol] = last_closed.close_time_ms
        append_receipts(receipt_path, (entry_record,))
        return ACTION_ENTRY

    state.candle_marks[symbol] = last_closed.close_time_ms
    return ACTION_NONE


# ---------------------------------------------------------------------------
# v2 scan command — universe + closed-candle surge + restart-safe paper step
# ---------------------------------------------------------------------------

SCAN_ROW_FIELDS = (
    "checked_utc",
    "symbol",
    "interval",
    "status",
    "closed_candle_count",
    "candle_open_utc",
    "candle_close_utc",
    "candle_quote_volume",
    "baseline_median_quote_volume",
    "threshold_quote_volume",
    "surged",
    "paper_action",
)


def print_scan_header(
    universe_size: Optional[int],
    scan_symbols: Sequence[str],
    interval: str,
    baseline_candles: int,
    surge_multiple: Decimal,
    max_symbols: int,
    paper_enabled: bool,
    state_path: Optional[Path],
) -> None:
    print("source=" + PUBLIC_BASE_URL + KLINES_PATH)
    if universe_size is None:
        print("universe=explicit_symbol_list")
    else:
        print("universe=" + PUBLIC_BASE_URL + EXCHANGE_INFO_PATH)
        print("universe_request_params=" + urlencode(UNIVERSE_REQUEST_PARAMS))
        print(
            "universe_filter=status_TRADING quote_USDT spot_trading_allowed"
            " symbol_charset_A-Z0-9"
        )
        print(f"universe_size={universe_size}")
        print("universe_selection=first_max_symbols_alphabetical")
    if universe_size is None:
        budget = "request_budget={0}_klines_gets".format(len(scan_symbols))
    else:
        budget = "request_budget=1_exchangeinfo_get+{0}_klines_gets".format(
            len(scan_symbols)
        )
    print(
        budget
        + "; max_symbols_cap={0}; spacing_seconds={1}".format(
            max_symbols, REQUEST_SPACING_SECONDS
        )
    )
    print("rule=" + SURGE_RULE_TEXT)
    print(
        "parameters=interval:{0} baseline_candles:{1} surge_multiple:{2}".format(
            interval, baseline_candles, fmt(surge_multiple)
        )
    )
    print("rows=factual_observations_only")
    if paper_enabled:
        print("paper_actions=enabled; " + PAPER_NOTE)
        print("paper_state_path=" + str(state_path))
    else:
        print("paper_actions=disabled(observe_only)")
    print("\t".join(SCAN_ROW_FIELDS))


def print_scan_row(check: SurgeCheck, paper_action: str) -> None:
    values = (
        check.checked_utc,
        check.symbol,
        check.interval,
        check.status,
        str(check.closed_candle_count),
        fmt_opt(check.candle_open_utc),
        fmt_opt(check.candle_close_utc),
        fmt_opt(check.candle_quote_volume),
        fmt_opt(check.baseline_median_quote_volume),
        fmt_opt(check.threshold_quote_volume),
        "true" if check.surged else "false",
        paper_action,
    )
    print("\t".join(values))


def run_scan(
    symbols: Optional[Sequence[str]],
    use_universe: bool,
    interval: str,
    baseline_candles: int,
    surge_multiple: Decimal,
    max_symbols: int,
    timeout: float,
    paper_enabled: bool,
    notional: Decimal,
    trail_pct: Decimal,
    receipt_path: Path,
    state_path: Path,
    run_id: Optional[str],
    now_fn: Callable[[], str] = now_utc,
    now_ms_fn: Callable[[], int] = now_epoch_ms,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> List[Tuple[SurgeCheck, str]]:
    """One bounded scan pass: capped klines GETs, factual rows, paper step.

    State is loaded before any action and saved after every mutating paper
    action, so a kill at any point restarts cleanly without duplication.
    """
    if not 1 <= max_symbols <= MAX_SCAN_SYMBOLS:
        raise PaperInputError(
            f"max_symbols must be between 1 and {MAX_SCAN_SYMBOLS}"
        )
    resolved_run_id = run_id if run_id is not None else default_run_id()
    if not RUN_ID_PATTERN.fullmatch(resolved_run_id):
        raise PaperInputError("invalid run_id")

    universe_size: Optional[int] = None
    if use_universe:
        universe = fetch_spot_usdt_universe(timeout)
        universe_size = len(universe)
        scan_symbols = universe[:max_symbols]
    else:
        if not symbols:
            raise PaperInputError("no symbols given and universe disabled")
        seen = []
        for raw_symbol in symbols:
            symbol = raw_symbol.upper()
            if not SYMBOL_PATTERN.fullmatch(symbol):
                raise PaperInputError(f"invalid symbol: {raw_symbol!r}")
            if symbol not in seen:
                seen.append(symbol)
        scan_symbols = seen[:max_symbols]

    state = load_paper_state(state_path) if paper_enabled else PaperState()
    print_scan_header(
        universe_size,
        scan_symbols,
        interval,
        baseline_candles,
        surge_multiple,
        max_symbols,
        paper_enabled,
        state_path if paper_enabled else None,
    )

    results: List[Tuple[SurgeCheck, str]] = []
    for index, symbol in enumerate(scan_symbols):
        if index > 0:
            sleep_fn(REQUEST_SPACING_SECONDS)
        candles = fetch_klines(symbol, interval, baseline_candles + 2, timeout)
        closed = closed_candles_only(candles, now_ms_fn())
        check = evaluate_surge(
            symbol, interval, closed, baseline_candles, surge_multiple, now_fn()
        )
        if not paper_enabled:
            action = ACTION_DISABLED
        elif not closed:
            action = ACTION_NONE
        else:
            action = apply_paper_candle(
                state,
                check,
                closed[-1],
                notional,
                trail_pct,
                receipt_path,
                resolved_run_id,
                now_fn,
            )
            if action in (ACTION_ENTRY, ACTION_EXIT, ACTION_UPDATED, ACTION_NONE):
                save_paper_state(state_path, state, now_fn)
        print_scan_row(check, action)
        results.append((check, action))

    if paper_enabled:
        print(f"open_paper_positions={len(state.positions)}")
        print("paper_receipts_path=" + str(receipt_path))
    return results


def print_paper_state(state_path: Path) -> None:
    """Factual read-only print of the persisted paper positions."""
    state = load_paper_state(state_path)
    print("paper_state_path=" + str(state_path))
    print("rows=factual_paper_state_only; " + PAPER_NOTE)
    print(f"open_paper_positions={len(state.positions)}")
    for symbol in sorted(state.positions):
        record = state.positions[symbol]
        print(
            "\t".join(
                (
                    symbol,
                    "entry_price=" + str(record["entry_price"]),
                    "qty=" + str(record["qty"]),
                    "peak_price=" + str(record["peak_price"]),
                    "trail_pct=" + str(record["trail_pct"]),
                    "entry_ts=" + str(record["entry_ts"]),
                    "paper=true",
                )
            )
        )
    marks = state.candle_marks
    print(f"candle_marks={len(marks)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def timeout_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a number") from None
    if not 0 < value <= MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"timeout must be greater than 0 and at most {MAX_TIMEOUT_SECONDS:g} seconds"
        )
    return value


def positive_decimal_arg(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a decimal number: {raw}") from None
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a finite positive number: {raw}")
    return value


def trail_pct_arg(raw: str) -> Decimal:
    value = positive_decimal_arg(raw)
    if not value < 1:
        raise argparse.ArgumentTypeError("trail-pct must be strictly below 1")
    return value


def baseline_arg(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("baseline must be an integer") from None
    if not MIN_BASELINE_CANDLES <= value <= MAX_BASELINE_CANDLES:
        raise argparse.ArgumentTypeError(
            f"baseline must be between {MIN_BASELINE_CANDLES} and {MAX_BASELINE_CANDLES}"
        )
    return value


def surge_multiple_arg(raw: str) -> Decimal:
    value = positive_decimal_arg(raw)
    if not MIN_SURGE_MULTIPLE < value <= MAX_SURGE_MULTIPLE:
        raise argparse.ArgumentTypeError(
            f"multiple must be above {fmt(MIN_SURGE_MULTIPLE)} "
            f"and at most {fmt(MAX_SURGE_MULTIPLE)}"
        )
    return value


def max_symbols_arg(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("max-symbols must be an integer") from None
    if not 1 <= value <= MAX_SCAN_SYMBOLS:
        raise argparse.ArgumentTypeError(
            f"max-symbols must be between 1 and {MAX_SCAN_SYMBOLS}"
        )
    return value


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Public volume observation rows and a PAPER-ONLY lifecycle."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    observe_parser = commands.add_parser(
        "observe",
        help=f"print factual 24h rows for the fixed list {', '.join(FIXED_SYMBOLS)}",
    )
    observe_parser.add_argument(
        "--timeout",
        type=timeout_seconds,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"per-request timeout, maximum {MAX_TIMEOUT_SECONDS:g} (default: 10)",
    )

    paper_parser = commands.add_parser(
        "paper", help="run one deterministic PAPER lifecycle over given prices"
    )
    paper_parser.add_argument("--symbol", default="BTCUSDT", help="symbol label")
    paper_parser.add_argument(
        "--prices",
        type=positive_decimal_arg,
        nargs="+",
        required=True,
        metavar="PRICE",
        help="price series driving the paper lifecycle",
    )
    paper_parser.add_argument(
        "--notional",
        type=positive_decimal_arg,
        default=DEFAULT_PAPER_NOTIONAL,
        help=f"fixed paper notional in quote units (default: {fmt(DEFAULT_PAPER_NOTIONAL)})",
    )
    paper_parser.add_argument(
        "--trail-pct",
        type=trail_pct_arg,
        default=DEFAULT_TRAIL_PCT,
        help=f"trailing stop fraction, 0 < x < 1 (default: {fmt(DEFAULT_TRAIL_PCT)})",
    )
    paper_parser.add_argument(
        "--receipts",
        type=Path,
        default=DEFAULT_RECEIPT_PATH,
        metavar="PATH",
        help="paper receipts JSONL path (default: paper_receipts.jsonl in repo root)",
    )
    paper_parser.add_argument("--run-id", default=None, help="explicit paper run id")

    scan_parser = commands.add_parser(
        "scan",
        help="closed-candle volume-surge scan; optional restart-safe PAPER step",
    )
    symbol_source = scan_parser.add_mutually_exclusive_group(required=True)
    symbol_source.add_argument(
        "--symbols",
        nargs="+",
        metavar="SYMBOL",
        help="explicit symbol list to scan",
    )
    symbol_source.add_argument(
        "--universe",
        action="store_true",
        help="discover live SPOT-USDT TRADING symbols from exchangeInfo (1 GET)",
    )
    scan_parser.add_argument(
        "--interval",
        choices=KLINE_INTERVALS,
        default=DEFAULT_INTERVAL,
        help=f"kline interval (default: {DEFAULT_INTERVAL})",
    )
    scan_parser.add_argument(
        "--baseline",
        type=baseline_arg,
        default=DEFAULT_BASELINE_CANDLES,
        metavar="N",
        help="trailing baseline of closed candles "
        f"(default: {DEFAULT_BASELINE_CANDLES})",
    )
    scan_parser.add_argument(
        "--multiple",
        type=surge_multiple_arg,
        default=DEFAULT_SURGE_MULTIPLE,
        metavar="X",
        help="surge multiple over the baseline median "
        f"(default: {fmt(DEFAULT_SURGE_MULTIPLE)})",
    )
    scan_parser.add_argument(
        "--max-symbols",
        type=max_symbols_arg,
        default=DEFAULT_SCAN_SYMBOLS,
        metavar="K",
        help="cap on klines GETs this run "
        f"(default: {DEFAULT_SCAN_SYMBOLS}, hard cap {MAX_SCAN_SYMBOLS})",
    )
    scan_parser.add_argument(
        "--timeout",
        type=timeout_seconds,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"per-request timeout, maximum {MAX_TIMEOUT_SECONDS:g} (default: 10)",
    )
    scan_parser.add_argument(
        "--paper",
        action="store_true",
        help="enable the restart-safe PAPER position step (paper only)",
    )
    scan_parser.add_argument(
        "--notional",
        type=positive_decimal_arg,
        default=DEFAULT_PAPER_NOTIONAL,
        help=f"fixed paper notional in quote units (default: {fmt(DEFAULT_PAPER_NOTIONAL)})",
    )
    scan_parser.add_argument(
        "--trail-pct",
        type=trail_pct_arg,
        default=DEFAULT_TRAIL_PCT,
        help=f"trailing stop fraction, 0 < x < 1 (default: {fmt(DEFAULT_TRAIL_PCT)})",
    )
    scan_parser.add_argument(
        "--receipts",
        type=Path,
        default=DEFAULT_RECEIPT_PATH,
        metavar="PATH",
        help="paper receipts JSONL path (default: paper_receipts.jsonl in repo root)",
    )
    scan_parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        metavar="PATH",
        help="paper position state JSON path (default: paper_state.json in repo root)",
    )
    scan_parser.add_argument("--run-id", default=None, help="explicit scan run id")

    state_parser = commands.add_parser(
        "state", help="print the persisted PAPER positions (read-only)"
    )
    state_parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        metavar="PATH",
        help="paper position state JSON path (default: paper_state.json in repo root)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = argument_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "observe":
            print_observation_rows(observe(args.timeout))
        elif args.command == "scan":
            run_scan(
                symbols=args.symbols,
                use_universe=args.universe,
                interval=args.interval,
                baseline_candles=args.baseline,
                surge_multiple=args.multiple,
                max_symbols=args.max_symbols,
                timeout=args.timeout,
                paper_enabled=args.paper,
                notional=args.notional,
                trail_pct=args.trail_pct,
                receipt_path=args.receipts,
                state_path=args.state,
                run_id=args.run_id,
            )
        elif args.command == "state":
            print_paper_state(args.state)
        else:
            symbol = args.symbol.upper()
            entry_record, exit_record = run_paper_lifecycle(
                symbol,
                args.prices,
                notional=args.notional,
                trail_pct=args.trail_pct,
                receipt_path=args.receipts,
                run_id=args.run_id,
            )
            print("paper_receipts_path=" + str(args.receipts))
            print(json.dumps(entry_record, sort_keys=True))
            print(json.dumps(exit_record, sort_keys=True))
    except VolumeObserverError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
