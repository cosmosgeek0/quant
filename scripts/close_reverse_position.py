#!/usr/bin/env python3
"""Close the frozen reverse position the plain way: buy back the borrowed
base on cross margin, repay the full liability, print receipt rows.

Written in the first-script style from live-proven primitives: signed reads,
one market buy with a unique client id (query that id before judging any
unclear result; never resend blind), one repay, confirmation reads, honest
rows. Single attempt per mutation; anything unclear stops with the facts.
Committed code, owner-pressed. Symbol and asset are fixed to the live case.
"""
import hashlib
import json
import os
import sys
import time
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN

sys.path.insert(0, "/home/ssm-user/quant-run/wave2-validate-h2/src")
import basis_executor as be  # noqa: E402

SYMBOL = "ZROUSDT"
ASSET = "ZRO"
STEP = Decimal("0.01")            # ZROUSDT spot/margin LOT_SIZE step
FEE_HEADROOM = Decimal("1.002")   # covers a base-denominated taker fee of 0.1% twice over
SPEND_CEILING = Decimal("12")     # hard stop: never spend more than this many USDT


def row(text):
    print("CLOSE | " + text, flush=True)


def main():
    key = os.environ.get("BINANCE_API_KEY", "")
    secret = os.environ.get("BINANCE_API_SECRET", "")
    if not key or not secret:
        row("REFUSED | credentials unavailable")
        return 2
    request = be.live_request_factory(key, secret)
    spot = be.SPOT.base_url

    def get(path, params=None):
        kind, payload = request("GET", spot, path, params or {}, True)
        return kind, payload

    def post(path, params):
        kind, payload = request("POST", spot, path, params, True)
        return kind, payload

    def margin_rows():
        kind, acct = get("/sapi/v1/margin/account")
        if kind != "ok" or not isinstance(acct, dict):
            return None, None
        zro = usdt = None
        for entry in acct.get("userAssets", []):
            if entry.get("asset") == ASSET:
                zro = entry
            if entry.get("asset") == "USDT":
                usdt = entry
        return zro, usdt

    zro, usdt = margin_rows()
    if zro is None or usdt is None:
        row("REFUSED | margin account unreadable")
        return 2
    borrowed = Decimal(zro["borrowed"])
    interest = Decimal(zro["interest"])
    liability = borrowed + interest
    free_base = Decimal(zro["free"])
    free_usdt = Decimal(usdt["free"])
    row("state | borrowed %s interest %s liability %s | free %s %s | free USDT %s"
        % (borrowed, interest, liability, ASSET, free_base, free_usdt))
    if liability == 0:
        row("VERDICT | ALREADY-FLAT | nothing borrowed")
        return 0

    shortfall = liability - free_base
    if shortfall > 0:
        need = (shortfall * FEE_HEADROOM / STEP).to_integral_value(
            rounding=ROUND_CEILING) * STEP
        kind, book = request(
            "GET", spot, "/api/v3/ticker/bookTicker", {"symbol": SYMBOL}, False)
        if kind != "ok" or not isinstance(book, dict):
            row("REFUSED | book unreadable")
            return 2
        ask = Decimal(book["askPrice"])
        est_cost = need * ask
        row("plan | buy %s %s at ~%s (~%s USDT)" % (need, ASSET, ask, est_cost))
        if est_cost > SPEND_CEILING or est_cost > free_usdt:
            row("REFUSED | cost %s exceeds ceiling %s or free USDT %s"
                % (est_cost, SPEND_CEILING, free_usdt))
            return 2
        client_id = "w7close-" + hashlib.sha256(
            (SYMBOL + str(liability) + str(need)).encode()).hexdigest()[:18]
        kind, order = post(MARGIN_ORDER_PATH := "/sapi/v1/margin/order", {
            "symbol": SYMBOL, "side": "BUY", "type": "MARKET",
            "quantity": format(need.normalize(), "f"),
            "isIsolated": "FALSE", "sideEffectType": "NO_SIDE_EFFECT",
            "newClientOrderId": client_id,
        })
        if kind != "ok" or not isinstance(order, dict):
            row("buy submit unclear | querying id %s before any judgement" % client_id)
            time.sleep(2)
            kind, order = get(MARGIN_ORDER_PATH, {
                "symbol": SYMBOL, "origClientOrderId": client_id,
                "isIsolated": "FALSE"})
            if kind != "ok" or not isinstance(order, dict):
                row("VERDICT | UNKNOWN | buy result unreadable — stop, nothing resent")
                return 4
        status = order.get("status")
        filled = Decimal(order.get("executedQty", "0"))
        quote = Decimal(order.get("cummulativeQuoteQty", "0"))
        row("buy | status %s | filled %s %s | spent %s USDT | order %s"
            % (status, filled, ASSET, quote, order.get("orderId")))
        if status != "FILLED":
            row("VERDICT | UNKNOWN | buy not fully filled — stop, repay not attempted")
            return 4
        time.sleep(2)

    zro, _ = margin_rows()
    if zro is None:
        row("VERDICT | UNKNOWN | post-buy account unreadable")
        return 4
    liability_now = Decimal(zro["borrowed"]) + Decimal(zro["interest"])
    free_now = Decimal(zro["free"])
    repay_amount = min(liability_now, free_now)
    if repay_amount <= 0:
        row("VERDICT | UNKNOWN | nothing available to repay (free %s liability %s)"
            % (free_now, liability_now))
        return 4
    row("repay | amount %s against liability %s" % (repay_amount, liability_now))
    kind, result = post("/sapi/v1/margin/borrow-repay", {
        "asset": ASSET, "amount": format(repay_amount.normalize(), "f"),
        "type": "REPAY", "isIsolated": "FALSE",
    })
    if kind != "ok":
        row("repay submit unclear | re-reading account before any judgement")
    time.sleep(2)
    zro, usdt = margin_rows()
    if zro is None:
        row("VERDICT | UNKNOWN | final account unreadable")
        return 4
    final_liability = Decimal(zro["borrowed"]) + Decimal(zro["interest"])
    row("final | borrowed %s interest %s | free %s %s | free USDT %s"
        % (zro["borrowed"], zro["interest"], ASSET, zro["free"], usdt["free"]))
    if final_liability == 0:
        row("VERDICT | CLOSED | liability zero — position dead")
        return 0
    row("VERDICT | PARTIAL | remaining liability %s — report, nothing retried"
        % final_liability)
    return 3


if __name__ == "__main__":
    sys.exit(main())
