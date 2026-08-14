"""basis_scan.py — one public GET-only sweep of the whole common universe.

Every symbol that trades on BOTH Binance Spot (USDT quote) and USD-M
perpetuals gets one row: executable entry gap (futures bid minus spot ask),
executable exit gap (futures ask minus spot bid), funding, the minimum valid
notional on the stricter venue, and whether a cycle fits under the compiled
per-leg cap. Rows rank by entry gap. Decimal only.

Five unsigned public GETs total (two exchangeInfo, two all-symbol book
tickers, one all-symbol premiumIndex). No credentials, no POST, no account
touch, no order. Fee columns are ESTIMATED from standard taker defaults
(spot 10 bps, USD-M 5 bps per side) because real account rates are not
public; they are labeled as estimates in the output.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, localcontext
from typing import Any, Dict, List, Optional

try:
    from limits import PER_LEG_CAP_USDT
except ImportError:
    PER_LEG_CAP_USDT = Decimal("32")

SPOT_BASE = "https://api.binance.com"
USDM_BASE = "https://fapi.binance.com"
MAX_BYTES = 8_000_000
TIMEOUT = 30.0
CAP_USDT = PER_LEG_CAP_USDT
FEE_BUFFER = Decimal("0.002")   # mirrors basis_cycle: spot fee headroom
BPS_Q = Decimal("0.0001")
EST_SPOT_TAKER_BPS = Decimal("10")   # ESTIMATED default, per side
EST_USDM_TAKER_BPS = Decimal("5")    # ESTIMATED default, per side
EST_ROUNDTRIP_COST_BPS = (EST_SPOT_TAKER_BPS + EST_USDM_TAKER_BPS) * 2


class ScanError(Exception):
    pass


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ScanError("response exceeded %d bytes: %s" % (MAX_BYTES, url))
    return json.loads(raw.decode())


def dec(value: Any) -> Optional[Decimal]:
    try:
        result = Decimal(str(value))
    except ArithmeticError:
        return None
    return result if result.is_finite() else None


def filter_value(filters: Any, filter_type: str, *keys: str) -> Optional[Decimal]:
    if not isinstance(filters, list):
        return None
    for row in filters:
        if isinstance(row, dict) and row.get("filterType") == filter_type:
            for key in keys:
                if key in row:
                    return dec(row[key])
    return None


def spot_universe(info: Any) -> Dict[str, Dict[str, Decimal]]:
    """TRADING, USDT-quoted, spot-allowed symbols -> min notional + lot step."""
    out: Dict[str, Dict[str, Decimal]] = {}
    for row in (info.get("symbols") if isinstance(info, dict) else []) or []:
        if not isinstance(row, dict) or row.get("status") != "TRADING" \
                or row.get("quoteAsset") != "USDT" \
                or row.get("isSpotTradingAllowed") is False:
            continue
        step = filter_value(row.get("filters"), "LOT_SIZE", "stepSize")
        min_notional = filter_value(row.get("filters"), "NOTIONAL",
                                    "minNotional") \
            or filter_value(row.get("filters"), "MIN_NOTIONAL", "minNotional")
        if step and step > 0 and min_notional is not None:
            out[row["symbol"]] = {"step": step, "min_notional": min_notional}
    return out


def usdm_universe(info: Any) -> Dict[str, Dict[str, Decimal]]:
    """TRADING USDT perpetuals -> min notional + lot step."""
    out: Dict[str, Dict[str, Decimal]] = {}
    for row in (info.get("symbols") if isinstance(info, dict) else []) or []:
        if not isinstance(row, dict) or row.get("status") != "TRADING" \
                or row.get("quoteAsset") != "USDT" \
                or row.get("contractType") != "PERPETUAL":
            continue
        step = filter_value(row.get("filters"), "LOT_SIZE", "stepSize")
        min_notional = filter_value(row.get("filters"), "MIN_NOTIONAL",
                                    "notional", "minNotional")
        if step and step > 0 and min_notional is not None:
            out[row["symbol"]] = {"step": step, "min_notional": min_notional}
    return out


def book_map(payload: Any) -> Dict[str, Dict[str, Decimal]]:
    out: Dict[str, Dict[str, Decimal]] = {}
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            continue
        bid, ask = dec(row.get("bidPrice")), dec(row.get("askPrice"))
        if bid and ask and bid > 0 and ask > 0 and bid <= ask:
            out[str(row.get("symbol"))] = {"bid": bid, "ask": ask}
    return out


def funding_map(payload: Any) -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for row in payload if isinstance(payload, list) else []:
        if isinstance(row, dict):
            rate = dec(row.get("lastFundingRate"))
            if rate is not None:
                out[str(row.get("symbol"))] = rate
    return out


def cap_feasible(spot_ask: Decimal, fut_bid: Decimal,
                 spot_meta: Dict[str, Decimal],
                 fut_meta: Dict[str, Decimal]) -> bool:
    """Could one hedged pair fit under the compiled per-leg cap right now?

    Fee-aware, mirroring basis_cycle: the cap must also cover the spot
    base-fee headroom, or the net acquired floors below one futures lot
    (the exact live failure of 2026-08-13 on BNBUSDT).
    """
    step = max(spot_meta["step"], fut_meta["step"])
    usable = CAP_USDT / (Decimal(1) + FEE_BUFFER)
    quantity = (usable / spot_ask / step).to_integral_value(
        rounding=ROUND_DOWN) * step
    if quantity <= 0:
        return False
    return quantity * spot_ask >= spot_meta["min_notional"] and \
        quantity * fut_bid >= fut_meta["min_notional"]


def build_rows(spot_meta: Dict[str, Dict[str, Decimal]],
               fut_meta: Dict[str, Dict[str, Decimal]],
               spot_books: Dict[str, Dict[str, Decimal]],
               fut_books: Dict[str, Dict[str, Decimal]],
               funding: Dict[str, Decimal]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with localcontext() as context:
        context.prec = 50
        for symbol in sorted(set(spot_meta) & set(fut_meta)):
            spot, fut = spot_books.get(symbol), fut_books.get(symbol)
            if not spot or not fut:
                continue
            entry_bps = ((fut["bid"] - spot["ask"]) / spot["ask"]
                         * Decimal(10_000)).quantize(BPS_Q, ROUND_HALF_EVEN)
            exit_bps = ((fut["ask"] - spot["bid"]) / spot["bid"]
                        * Decimal(10_000)).quantize(BPS_Q, ROUND_HALF_EVEN)
            rows.append({
                "symbol": symbol,
                "entry_gap_bps": entry_bps,
                "exit_gap_bps": exit_bps,
                "funding_bps": (funding.get(symbol, Decimal(0))
                                * Decimal(10_000)).quantize(BPS_Q,
                                                            ROUND_HALF_EVEN),
                "min_valid_notional": max(spot_meta[symbol]["min_notional"],
                                          fut_meta[symbol]["min_notional"]),
                "fits_10_cap": cap_feasible(spot["ask"], fut["bid"],
                                            spot_meta[symbol],
                                            fut_meta[symbol]),
            })
    rows.sort(key=lambda row: row["entry_gap_bps"], reverse=True)
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--best-min-bps", default="",
                        help="print one machine line — BEST|SYMBOL|bps for "
                             "the top cap-fitting symbol at or above this "
                             "entry gap, else BEST|NONE — and exit")
    args = parser.parse_args(argv)

    spot_meta = spot_universe(fetch_json(
        SPOT_BASE + "/api/v3/exchangeInfo?symbolStatus=TRADING"
                    "&showPermissionSets=false"))
    fut_meta = usdm_universe(fetch_json(USDM_BASE + "/fapi/v1/exchangeInfo"))
    spot_books = book_map(fetch_json(SPOT_BASE + "/api/v3/ticker/bookTicker"))
    fut_books = book_map(fetch_json(USDM_BASE + "/fapi/v1/ticker/bookTicker"))
    funding = funding_map(fetch_json(USDM_BASE + "/fapi/v1/premiumIndex"))

    rows = build_rows(spot_meta, fut_meta, spot_books, fut_books, funding)
    if args.best_min_bps:
        floor = Decimal(args.best_min_bps)
        for row in rows:
            if row["fits_10_cap"] and row["entry_gap_bps"] >= floor:
                print("BEST|%s|%s" % (row["symbol"], row["entry_gap_bps"]))
                return 0
        print("BEST|NONE")
        return 0
    positive = [row for row in rows if row["entry_gap_bps"] > 0]
    qualifying = [row for row in positive if row["fits_10_cap"]]
    print("SCAN | spot USDT symbols %d | USDT perpetuals %d | common with "
          "books %d | positive entry gap %d | positive AND fits %s USDT cap "
          "%d" % (len(spot_meta), len(fut_meta), len(rows), len(positive),
                  CAP_USDT, len(qualifying)))
    print("SCAN | costs are ESTIMATED defaults: spot taker %s bps + USD-M "
          "taker %s bps per side = %s bps round trip; real account rates "
          "unread" % (EST_SPOT_TAKER_BPS, EST_USDM_TAKER_BPS,
                      EST_ROUNDTRIP_COST_BPS))
    header = ("rank | symbol | entry bps | exit bps | funding bps | "
              "min notional | fits %s cap" % CAP_USDT)
    print(header)
    for index, row in enumerate(rows[:max(args.top, 0)], start=1):
        print("%d | %s | %s | %s | %s | %s | %s" % (
            index, row["symbol"], row["entry_gap_bps"], row["exit_gap_bps"],
            row["funding_bps"], row["min_valid_notional"],
            "YES" if row["fits_10_cap"] else "no"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
