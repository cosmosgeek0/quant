#!/usr/bin/env python3
"""Print public Binance Spot/USD-M executable basis gaps and funding rows.

Only unsigned, credential-free GET endpoints are used.  Gaps are computed
from EXECUTABLE book prices, not marks: the entry gap is USD-M best bid
minus Spot best ask (sell futures, buy spot); the exit gap is USD-M best
ask minus Spot best bid (buy futures back, sell spot).  Each gap is also
printed in basis points with its base named: the entry gap bps base is
spot_ask and the exit gap bps base is spot_bid.  Prices and derived values
use Decimal arithmetic throughout.
"""

import argparse
import http.client
import json
import re
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SPOT_BASE_URL = "https://api.binance.com"
USDM_BASE_URL = "https://fapi.binance.com"
SPOT_BOOK_PATH = "/api/v3/ticker/bookTicker"
USDM_BOOK_PATH = "/fapi/v1/ticker/bookTicker"
USDM_PREMIUM_PATH = "/fapi/v1/premiumIndex"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 1_000_000
MAX_SYMBOLS = 20
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{5,30}")
EXIT_HTTP = 3
EXIT_DATA = 4
BPS_QUANTUM = Decimal("0.00000001")


class BasisReadError(Exception):
    """Expected read failure with a stable process exit code."""

    exit_code = 1


class HttpReadError(BasisReadError):
    exit_code = EXIT_HTTP


class DataReadError(BasisReadError):
    exit_code = EXIT_DATA


@dataclass(frozen=True)
class BasisReadRow:
    completed_utc: str
    usdm_book_time_utc: str
    symbol: str
    spot_bid: Decimal
    spot_ask: Decimal
    usdm_bid: Decimal
    usdm_ask: Decimal
    entry_gap_quote: Decimal
    entry_gap_bps_vs_spot_ask: Decimal
    exit_gap_quote: Decimal
    exit_gap_bps_vs_spot_bid: Decimal
    funding_rate: Decimal
    next_funding_utc: str


def decimal_field(payload: Dict[str, Any], field: str, source: str) -> Decimal:
    """Return one finite Decimal field or identify the malformed source."""
    raw = payload.get(field)
    if raw is None or isinstance(raw, (bool, float)):
        raise DataReadError(f"{source} response has invalid {field}")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise DataReadError(f"{source} response has invalid {field}") from None
    if not value.is_finite():
        raise DataReadError(f"{source} response has non-finite {field}")
    return value


def integer_field(payload: Dict[str, Any], field: str, source: str) -> int:
    raw = payload.get(field)
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, str) and raw.isdigit():
        value = int(raw)
    else:
        raise DataReadError(f"{source} response has invalid {field}") from None
    if value < 0:
        raise DataReadError(f"{source} response has invalid {field}")
    return value


def utc_from_epoch_ms(epoch_ms: int, source: str) -> str:
    try:
        value = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            milliseconds=epoch_ms
        )
    except (OverflowError, ValueError):
        raise DataReadError(f"{source} response has out-of-range timestamp") from None
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_json_object(raw: bytes, source: str, symbol: str) -> Dict[str, Any]:
    """Decode one bounded response body into a JSON object; floats stay Decimal."""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DataReadError(f"{source} response too large for {symbol}")
    try:
        payload = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DataReadError(f"{source} returned invalid JSON for {symbol}") from None
    if not isinstance(payload, dict):
        raise DataReadError(f"{source} returned a non-object response for {symbol}")
    return payload


def fetch_public_json(
    source: str, base_url: str, path: str, symbol: str, timeout: float
) -> Dict[str, Any]:
    """Perform one bounded, unsigned GET and require a JSON object response."""
    url = base_url + path + "?" + urlencode({"symbol": symbol})
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "quant-basis-read/1"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise HttpReadError(f"{source} HTTP {exc.code} for {symbol}") from None
    except (
        URLError,
        TimeoutError,
        socket.timeout,
        OSError,
        http.client.HTTPException,
    ) as exc:
        raise HttpReadError(
            f"{source} network failure for {symbol}: {type(exc).__name__}"
        ) from None

    return parse_json_object(raw, source, symbol)


def build_basis_read_row(
    symbol: str,
    spot_book: Dict[str, Any],
    usdm_book: Dict[str, Any],
    usdm_premium: Dict[str, Any],
    completed_utc: str,
) -> BasisReadRow:
    """Validate three public responses and compute Decimal-safe executable gaps."""
    if spot_book.get("symbol") != symbol:
        raise DataReadError(f"Spot book response symbol does not match {symbol}")
    if usdm_book.get("symbol") != symbol:
        raise DataReadError(f"USD-M book response symbol does not match {symbol}")
    if usdm_premium.get("symbol") != symbol:
        raise DataReadError(f"USD-M premium response symbol does not match {symbol}")

    spot_bid = decimal_field(spot_book, "bidPrice", "Spot book")
    spot_ask = decimal_field(spot_book, "askPrice", "Spot book")
    usdm_bid = decimal_field(usdm_book, "bidPrice", "USD-M book")
    usdm_ask = decimal_field(usdm_book, "askPrice", "USD-M book")
    usdm_book_time_ms = integer_field(usdm_book, "time", "USD-M book")
    funding_rate = decimal_field(usdm_premium, "lastFundingRate", "USD-M premium")
    next_funding_ms = integer_field(usdm_premium, "nextFundingTime", "USD-M premium")

    if spot_bid <= 0 or spot_ask <= 0 or usdm_bid <= 0 or usdm_ask <= 0:
        raise DataReadError(f"non-positive market price for {symbol}")
    if spot_bid > spot_ask:
        raise DataReadError(f"crossed Spot book for {symbol}")
    if usdm_bid > usdm_ask:
        raise DataReadError(f"crossed USD-M book for {symbol}")
    if next_funding_ms == 0 or usdm_book_time_ms == 0:
        raise DataReadError(f"zero USD-M timestamp for {symbol}")

    with localcontext() as context:
        context.prec = 50
        entry_gap_quote = usdm_bid - spot_ask
        exit_gap_quote = usdm_ask - spot_bid
        entry_gap_bps_vs_spot_ask = (
            (entry_gap_quote / spot_ask) * Decimal(10_000)
        ).quantize(BPS_QUANTUM, rounding=ROUND_HALF_EVEN)
        exit_gap_bps_vs_spot_bid = (
            (exit_gap_quote / spot_bid) * Decimal(10_000)
        ).quantize(BPS_QUANTUM, rounding=ROUND_HALF_EVEN)

    return BasisReadRow(
        completed_utc=completed_utc,
        usdm_book_time_utc=utc_from_epoch_ms(usdm_book_time_ms, "USD-M book"),
        symbol=symbol,
        spot_bid=spot_bid,
        spot_ask=spot_ask,
        usdm_bid=usdm_bid,
        usdm_ask=usdm_ask,
        entry_gap_quote=entry_gap_quote,
        entry_gap_bps_vs_spot_ask=entry_gap_bps_vs_spot_ask,
        exit_gap_quote=exit_gap_quote,
        exit_gap_bps_vs_spot_bid=exit_gap_bps_vs_spot_bid,
        funding_rate=funding_rate,
        next_funding_utc=utc_from_epoch_ms(next_funding_ms, "USD-M premium"),
    )


def format_decimal(value: Decimal) -> str:
    return format(value, "f")


def print_rows(rows: Iterable[BasisReadRow]) -> None:
    print("entry_gap_quote_formula=usdm_bid-spot_ask")
    print("exit_gap_quote_formula=usdm_ask-spot_bid")
    print("entry_gap_bps_formula=entry_gap_quote/spot_ask*10000;bps_base=spot_ask")
    print("exit_gap_bps_formula=exit_gap_quote/spot_bid*10000;bps_base=spot_bid")
    fields = tuple(BasisReadRow.__dataclass_fields__)
    print("\t".join(fields))
    for row in rows:
        values = []
        for field in fields:
            value = getattr(row, field)
            values.append(format_decimal(value) if isinstance(value, Decimal) else value)
        print("\t".join(values))


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


def normalized_symbols(
    raw_symbols: Iterable[str], parser: argparse.ArgumentParser
) -> List[str]:
    symbols = []
    seen = set()
    for raw in raw_symbols:
        symbol = raw.upper()
        if not SYMBOL_PATTERN.fullmatch(symbol):
            parser.error(f"invalid Binance symbol: {raw}")
        if symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    if len(symbols) > MAX_SYMBOLS:
        parser.error(f"at most {MAX_SYMBOLS} unique symbols may be read")
    return symbols


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read public Binance Spot/USD-M executable basis gaps and funding."
    )
    parser.add_argument(
        "symbols",
        nargs="*",
        default=["BTCUSDT", "ETHUSDT"],
        help="symbols (default: BTCUSDT ETHUSDT)",
    )
    parser.add_argument(
        "--timeout",
        type=timeout_seconds,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"per-request timeout, maximum {MAX_TIMEOUT_SECONDS:g} (default: 10)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = argument_parser()
    args = parser.parse_args(argv)
    symbols = normalized_symbols(args.symbols, parser)
    rows = []
    try:
        for symbol in symbols:
            spot_book = fetch_public_json(
                "Spot book", SPOT_BASE_URL, SPOT_BOOK_PATH, symbol, args.timeout
            )
            usdm_book = fetch_public_json(
                "USD-M book", USDM_BASE_URL, USDM_BOOK_PATH, symbol, args.timeout
            )
            usdm_premium = fetch_public_json(
                "USD-M premium", USDM_BASE_URL, USDM_PREMIUM_PATH, symbol, args.timeout
            )
            rows.append(
                build_basis_read_row(
                    symbol, spot_book, usdm_book, usdm_premium, now_utc()
                )
            )
    except BasisReadError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code

    print_rows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
