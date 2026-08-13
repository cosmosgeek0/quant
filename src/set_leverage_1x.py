"""set_leverage_1x.py — one-shot: make one symbol's leverage exactly 1x.

An OWNER-TRIGGERED account change (a signed TRADE-class POST) that runs on
the execution host. Reads the actual configured leverage first; already 1x
means no mutation at all. Otherwise submits exactly one leverage change to
1x and re-reads the configuration to prove it — no retry, no other change.
Factual rows only; exit 0 only when the re-read proves 1x.

env: SYMBOL, BINANCE_API_KEY, BINANCE_API_SECRET.
"""

from __future__ import annotations

import os
import re
import sys

from basis_executor import (
    USDM,
    USDM_SYMBOL_CONFIG_PATH,
    PreflightRefusal,
    live_request_factory,
    symbol_leverage,
)

SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")


def read_leverage(request, symbol: str):
    kind, rows = request("GET", USDM.base_url, USDM_SYMBOL_CONFIG_PATH,
                         {"symbol": symbol}, True)
    if kind != "ok":
        return None, "config read failed: %s" % kind
    try:
        return symbol_leverage(rows, symbol), ""
    except PreflightRefusal as exc:
        return None, str(exc)


def run(symbol: str, request) -> int:
    before, error = read_leverage(request, symbol)
    if before is None:
        print("LEVERAGE | %s | %s" % (symbol, error))
        return 1
    if before == 1:
        print("LEVERAGE | %s | already 1x | no mutation" % symbol)
        return 0
    kind, _payload = request("POST", USDM.base_url, "/fapi/v1/leverage",
                             {"symbol": symbol, "leverage": "1"}, True)
    if kind != "ok":
        print("LEVERAGE | %s | change POST not confirmed (%s) | was %sx | "
              "no retry" % (symbol, kind, before))
        return 1
    after, error = read_leverage(request, symbol)
    if after == 1:
        print("LEVERAGE | %s | changed %sx -> 1x | proven by re-read" % (
            symbol, before))
        return 0
    print("LEVERAGE | %s | UNPROVEN after change | re-read: %s | manual "
          "check required before any trade" % (symbol, error or after))
    return 1


def main() -> int:
    symbol = os.environ.get("SYMBOL", "").upper()
    if not SYMBOL_RE.fullmatch(symbol):
        print("LEVERAGE | SYMBOL env must be 5-20 uppercase letters/digits")
        return 2
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        print("LEVERAGE | missing BINANCE_API_KEY / BINANCE_API_SECRET")
        return 2
    return run(symbol, live_request_factory(api_key, api_secret))


if __name__ == "__main__":
    sys.exit(main())
