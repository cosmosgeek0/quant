#!/usr/bin/env python3
"""Volume-lane groundwork: public observation rows plus a PAPER-ONLY lifecycle.

Strategy 2 groundwork in two parts, paper only end to end.

Part 1 (observe): unsigned, credential-free public GETs to api.binance.com
/api/v3/ticker/24hr over a small fixed symbol list.  Prints plain factual
observation rows (volume, quote volume, price change).  Factual states only —
no predictions and no scores presented as facts.

Part 2 (paper): a deterministic paper lifecycle driven by a supplied price
series — one paper entry at a small fixed paper notional, then one trailing
exit — appending paper receipts as JSON lines.  Every receipt carries
"paper": true.  There is no real order path anywhere in this module: no
credentials, no signing, no POST, no order endpoint; the only network call is
the unsigned public GET above, and the paper lifecycle itself never touches
the network at all.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import secrets
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PUBLIC_BASE_URL = "https://api.binance.com"
TICKER_24H_PATH = "/api/v3/ticker/24hr"
FIXED_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 1_000_000
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
PAPER_NOTE = "paper only; no real order path"


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


def fmt(value: Decimal) -> str:
    return format(value, "f")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def utc_from_epoch_ms(epoch_ms: int, source: str) -> str:
    try:
        value = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            milliseconds=epoch_ms
        )
    except (OverflowError, ValueError):
        raise DataReadError(f"{source} response has out-of-range timestamp") from None
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
        raise DataReadError(f"{source} response has invalid {field}")
    if value < 0:
        raise DataReadError(f"{source} response has invalid {field}")
    return value


def fetch_public_json(path: str, symbol: str, timeout: float) -> Dict[str, Any]:
    """Perform one bounded, unsigned public GET and require a JSON object."""
    url = PUBLIC_BASE_URL + path + "?" + urlencode({"symbol": symbol})
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "quant-volume-observer/1"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise HttpReadError(f"public HTTP {exc.code} for {symbol}") from None
    except (
        URLError,
        TimeoutError,
        socket.timeout,
        OSError,
        http.client.HTTPException,
    ) as exc:
        raise HttpReadError(
            f"public network failure for {symbol}: {type(exc).__name__}"
        ) from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise DataReadError(f"public response too large for {symbol}")
    try:
        payload = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DataReadError(f"public response is invalid JSON for {symbol}") from None
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
        for field in fields:
            value = getattr(row, field)
            values.append(fmt(value) if isinstance(value, Decimal) else str(value))
        print("\t".join(values))


def observe(timeout: float) -> List[ObservationRow]:
    """Read the fixed symbol list once; factual rows only."""
    rows = []
    for symbol in FIXED_SYMBOLS:
        payload = fetch_public_json(TICKER_24H_PATH, symbol, timeout)
        rows.append(build_observation_row(symbol, payload, now_utc()))
    return rows


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
    with localcontext() as context:
        context.prec = 50
        qty = (notional / entry_price).quantize(QTY_QUANTUM, rounding=ROUND_DOWN)
        if qty <= 0:
            raise PaperInputError(
                "paper notional too small for one quantum at the entry price"
            )
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
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = argument_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "observe":
            print_observation_rows(observe(args.timeout))
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
