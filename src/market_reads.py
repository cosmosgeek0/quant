#!/usr/bin/env python3
"""Print public Binance Spot and USD-M mark/funding basis-gap rows.

Only unsigned, credential-free GET endpoints are used.  For each symbol the
basis gap is defined as USD-M mark price minus the Spot best-bid/best-ask
midpoint.  Prices and derived values use Decimal arithmetic throughout.
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


SPOT_BASE_URL = "https://data-api.binance.vision"
USDM_BASE_URL = "https://fapi.binance.com"
SPOT_BOOK_PATH = "/api/v3/ticker/bookTicker"
USDM_MARK_PATH = "/fapi/v1/premiumIndex"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 1_000_000
MAX_SYMBOLS = 20
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{5,30}")
EXIT_HTTP = 3
EXIT_DATA = 4
BPS_QUANTUM = Decimal("0.00000001")


class MarketReadError(Exception):
    """Expected read failure with a stable process exit code."""

    exit_code = 1


class HttpReadError(MarketReadError):
    exit_code = EXIT_HTTP


class DataReadError(MarketReadError):
    exit_code = EXIT_DATA


@dataclass(frozen=True)
class BasisRow:
    completed_utc: str
    usdm_time_utc: str
    symbol: str
    spot_bid: Decimal
    spot_ask: Decimal
    spot_mid: Decimal
    usdm_mark: Decimal
    usdm_index: Decimal
    funding_rate: Decimal
    next_funding_utc: str
    basis_gap_quote: Decimal
    basis_gap_bps: Decimal


def decimal_field(payload: Dict[str, Any], field: str, source: str) -> Decimal:
    """Return one finite Decimal field or identify the malformed source."""
    raw = payload.get(field)
    if raw is None or isinstance(raw, bool):
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


def fetch_public_json(
    source: str, base_url: str, path: str, symbol: str, timeout: float
) -> Dict[str, Any]:
    """Perform one bounded, unsigned GET and require a JSON object response."""
    url = base_url + path + "?" + urlencode({"symbol": symbol})
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "quant-market-reads/1"},
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

    if len(raw) > MAX_RESPONSE_BYTES:
        raise DataReadError(f"{source} response too large for {symbol}")
    try:
        payload = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DataReadError(f"{source} returned invalid JSON for {symbol}") from None
    if not isinstance(payload, dict):
        raise DataReadError(f"{source} returned a non-object response for {symbol}")
    return payload


def build_basis_row(
    symbol: str, spot: Dict[str, Any], usdm: Dict[str, Any], completed_utc: str
) -> BasisRow:
    """Validate two public responses and compute their Decimal-safe basis gap."""
    if spot.get("symbol") != symbol:
        raise DataReadError(f"Spot response symbol does not match {symbol}")
    if usdm.get("symbol") != symbol:
        raise DataReadError(f"USD-M response symbol does not match {symbol}")

    spot_bid = decimal_field(spot, "bidPrice", "Spot")
    spot_ask = decimal_field(spot, "askPrice", "Spot")
    usdm_mark = decimal_field(usdm, "markPrice", "USD-M")
    usdm_index = decimal_field(usdm, "indexPrice", "USD-M")
    funding_rate = decimal_field(usdm, "lastFundingRate", "USD-M")
    next_funding_ms = integer_field(usdm, "nextFundingTime", "USD-M")
    usdm_time_ms = integer_field(usdm, "time", "USD-M")

    if spot_bid <= 0 or spot_ask <= 0 or usdm_mark <= 0 or usdm_index <= 0:
        raise DataReadError(f"non-positive market price for {symbol}")
    if spot_bid > spot_ask:
        raise DataReadError(f"crossed Spot book for {symbol}")
    if next_funding_ms == 0 or usdm_time_ms == 0:
        raise DataReadError(f"zero USD-M timestamp for {symbol}")

    with localcontext() as context:
        context.prec = 50
        spot_mid = (spot_bid + spot_ask) / Decimal(2)
        basis_gap_quote = usdm_mark - spot_mid
        basis_gap_bps = (
            (basis_gap_quote / spot_mid) * Decimal(10_000)
        ).quantize(BPS_QUANTUM, rounding=ROUND_HALF_EVEN)

    return BasisRow(
        completed_utc=completed_utc,
        usdm_time_utc=utc_from_epoch_ms(usdm_time_ms, "USD-M"),
        symbol=symbol,
        spot_bid=spot_bid,
        spot_ask=spot_ask,
        spot_mid=spot_mid,
        usdm_mark=usdm_mark,
        usdm_index=usdm_index,
        funding_rate=funding_rate,
        next_funding_utc=utc_from_epoch_ms(next_funding_ms, "USD-M"),
        basis_gap_quote=basis_gap_quote,
        basis_gap_bps=basis_gap_bps,
    )


def format_decimal(value: Decimal) -> str:
    return format(value, "f")


def print_rows(rows: Iterable[BasisRow]) -> None:
    print("basis_gap_quote_formula=usdm_mark-spot_mid")
    print("basis_gap_bps_formula=basis_gap_quote/spot_mid*10000")
    fields = tuple(BasisRow.__dataclass_fields__)
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
        description="Read public Binance Spot and USD-M mark/funding basis gaps."
    )
    parser.add_argument(
        "symbols", nargs="*", default=["BTCUSDT"], help="symbols (default: BTCUSDT)"
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
            spot = fetch_public_json(
                "Spot", SPOT_BASE_URL, SPOT_BOOK_PATH, symbol, args.timeout
            )
            usdm = fetch_public_json(
                "USD-M", USDM_BASE_URL, USDM_MARK_PATH, symbol, args.timeout
            )
            rows.append(build_basis_row(symbol, spot, usdm, now_utc()))
    except MarketReadError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code

    print_rows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
