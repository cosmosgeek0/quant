#!/usr/bin/env python3
"""spot_roundtrip.py - one real Binance Spot round trip. Runs once, then exits.

Owner instruction: FABLE_FIRST_MONEY_LOOP (2026-08-12). One market BUY capped
by MAX_QUOTE, confirmed by its own client order id; one market SELL of only
what this run acquired; balances verified; one JSON receipt outside git;
sanitized output only. No repetition, no repair trades, no extra features.

Environment (read at start, never printed): BINANCE_API_KEY, BINANCE_API_SECRET
(host-side only), SYMBOL, MAX_QUOTE (hard ceiling, never raised), QUANT_SHA
(commit being run), optional BINANCE_BASE_URL, RECEIPT_DIR (default
~/quant_receipts, outside any git tree).

Size note: over the 350-line target because a 4-lens adversarial review found
11 blockers whose fixes are safety, not features - single-runner lock, order
truth established only by client-id query, full state reporting on every
post-mutation exit, and balance-reconciled verdicts. Reason recorded here per
the instruction's expansion rule.
"""

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN, getcontext

getcontext().prec = 28

BASE_URL = os.environ.get("BINANCE_BASE_URL", "https://api.binance.com")
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
SYMBOL = os.environ.get("SYMBOL", "").upper()
MAX_QUOTE_RAW = os.environ.get("MAX_QUOTE", "")
QUANT_SHA = os.environ.get("QUANT_SHA", "")
RECEIPT_DIR = os.path.expanduser(os.environ.get("RECEIPT_DIR", "~/quant_receipts"))
LOCK_PATH = os.path.join(RECEIPT_DIR, "run.lock")
RECV_WINDOW = "5000"
HTTP_TIMEOUT = 15
POLL_SECONDS = 180           # wall-clock budget per order confirmation
POLL_SLEEP = 2.0
SLIPPAGE_CUSHION = Decimal("0.001")  # added to Binance's exact BUY taker fees
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}

_time_offset_ms = 0
receipt = {}
receipt_path = ""
ctx = {"lock": False}  # shared run state; no classes by owner instruction


def d(x):
    return Decimal(str(x))


def fmt(x):
    return format(x, "f")


def floor_step(qty, step):
    return (qty // step) * step if step > 0 else qty


def now_ms():
    return int(time.time() * 1000) + _time_offset_ms


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def save():
    tmp = receipt_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(receipt, f, indent=2, default=str)
    os.replace(tmp, receipt_path)


def first_fail(where, detail):
    """Record the FIRST degradation of the run (required in the done-list)."""
    if "first_failure" not in receipt:
        receipt["first_failure"] = {"utc": utc(), "where": where,
                                    "detail": str(detail)[:200]}
        if receipt_path:
            save()


def http(method, path, params=None, signed=False):
    """One HTTP attempt. 'ok' = 2xx json; 'api_error' = a real Binance
    {code,msg} answer; 'unclear' = timeout/network/5xx/unparseable - proves
    NOTHING about whether the request was applied. Mutations never retry."""
    params = dict(params or {})
    if signed:
        params["timestamp"] = str(now_ms())
        params["recvWindow"] = RECV_WINDOW
        query = urllib.parse.urlencode(params)
        sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        query = query + "&signature=" + sig
    else:
        query = urllib.parse.urlencode(params)
    url = BASE_URL + path + ("?" + query if query else "")
    req = urllib.request.Request(url, method=method)
    if API_KEY:
        req.add_header("X-MBX-APIKEY", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return "ok", json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code >= 500 or e.code in (408, 409):
            return "unclear", {"http": e.code, "body": body[:200]}
        try:
            parsed = json.loads(body)
            if "code" in parsed:
                return "api_error", parsed
        except ValueError:
            pass
        # A 4xx Binance did not generate (proxy/edge) proves nothing.
        return "unclear", {"http": e.code, "body": body[:200]}
    except Exception as e:
        detail = str(e).split("signature=")[0][:160]  # never leak a signature
        return "unclear", {"error": type(e).__name__, "detail": detail}


def get_retry(path, params=None, signed=False, tries=3):
    """Reads may retry; mutations NEVER go through here."""
    kind, payload = "unclear", {}
    for i in range(tries):
        kind, payload = http("GET", path, params, signed)
        if kind != "unclear":
            return kind, payload
        time.sleep(1 + i)
    return kind, payload


def finish(verdict, note, code):
    receipt["verdict"] = verdict
    receipt["verdict_note"] = note
    receipt["finished_utc"] = utc()
    save()
    for k in ("run_id", "sha", "symbol", "max_quote",
              "buy_client_id", "sell_client_id"):
        print(k + ":", receipt.get(k, ""))
    if "first_failure" in receipt:
        print("first_failure:", json.dumps(receipt["first_failure"]))
    print("VERDICT:", verdict)
    print("NOTE:", note)
    print("RECEIPT:", receipt_path)
    # The lock stays on disk after exposure/unknown so a re-run cannot trade
    # before a human reconciles; it clears only when nothing is at risk.
    if ctx["lock"] and verdict in ("CLOSED", "REJECTED_BEFORE_BUY"):
        os.unlink(LOCK_PATH)
    sys.exit(code)


def refuse(reason):
    """Pre-mutation refusal: nothing was sent to the exchange."""
    finish("REJECTED_BEFORE_BUY", reason, 2)


def query_order(client_id):
    """Poll one client order id to a known terminal state within the budget.
    Returns ('terminal', order) | ('not_found', None) | ('error', payload)
    | ('pending', last). 'error' = Binance answered the QUERY with a definite
    non -2013 error (rate limit, auth, ban) - order truth is NOT established."""
    deadline = time.monotonic() + POLL_SECONDS
    last, missing = None, 0
    while time.monotonic() < deadline:
        kind, o = get_retry("/api/v3/order",
                            {"symbol": SYMBOL, "origClientOrderId": client_id},
                            signed=True, tries=1)
        if kind == "ok":
            last, missing = o, 0
            if o.get("status") in TERMINAL:
                return "terminal", o
        elif kind == "api_error":
            if o.get("code") == -2013:
                missing += 1
                if missing >= 10:
                    return "not_found", None
            else:
                first_fail("query_order", o)
                return "error", o  # incl. -1003/429/418: stop hammering
        time.sleep(POLL_SLEEP)
    return "pending", last


def order_trades(order_id, executed, resp_fills):
    """Real fills for one order: FULL-response fills first, then myTrades
    until they cover the executed quantity. Returns (trades, covered)."""
    trades = resp_fills or []
    covered = sum((d(x["qty"]) for x in trades), Decimal(0))
    for _ in range(5):
        if covered >= executed:
            return trades, covered
        time.sleep(1)
        kind, t = get_retry("/api/v3/myTrades",
                            {"symbol": SYMBOL, "orderId": order_id}, signed=True)
        if kind == "ok" and t:
            trades = t
            covered = sum((d(x["qty"]) for x in trades), Decimal(0))
    if covered < executed:
        first_fail("order_trades", "trades cover %s of %s" % (covered, executed))
    return trades, covered


def fees_by_asset(*trade_lists):
    agg = {}
    for trades in trade_lists:
        for t in trades:
            a = t.get("commissionAsset", "?")
            agg[a] = fmt(d(agg.get(a, "0")) + d(t["commission"]))
    return agg


def fee_in(agg, asset):
    return d(agg.get(asset, "0"))


def balances_of(acct, *assets):
    free = {b["asset"]: d(b["free"]) for b in acct.get("balances", [])}
    return {a: free.get(a, Decimal(0)) for a in assets}


def wrap_up(verdict, note, code):
    """Every exit after a confirmed mutation reports full exchange state."""
    base, quote = ctx["base"], ctx["quote"]
    kind, acct = get_retry("/api/v3/account", signed=True)
    final = balances_of(acct, base, quote) if kind == "ok" else None
    if final is None:
        first_fail("final_account_read", acct)
    kind, oo = get_retry("/api/v3/openOrders", {"symbol": SYMBOL}, signed=True)
    open_after = oo if kind == "ok" else None
    if open_after is None:
        first_fail("final_openorders_read", oo)
    fees = fees_by_asset(ctx.get("buy_trades", []), ctx.get("sell_trades", []))
    res = {"commissions": fees,
           "quote_spent": fmt(ctx.get("quote_spent", Decimal(0))),
           "quote_received": fmt(ctx.get("quote_recv", Decimal(0))),
           "net_quote_cost_incl_fees": fmt(
               ctx.get("quote_spent", Decimal(0)) - ctx.get("quote_recv", Decimal(0))
               + fee_in(fees, quote)),
           "residual_base_from_this_run": fmt(ctx["residual"]) if "residual" in ctx
           else "UNKNOWN",
           "final_base_free": fmt(final[base]) if final else "UNKNOWN",
           "final_quote_free": fmt(final[quote]) if final else "UNKNOWN",
           "open_orders_after": len(open_after) if open_after is not None
           else "UNKNOWN"}
    receipt["result"] = res
    save()
    for side in ("buy", "sell"):
        o = receipt.get(side, {})
        print("%s: status=%s order_id=%s executed=%s quote=%s" %
              (side, o.get("status", "-"), o.get("order_id", "-"),
               o.get("executed_qty", "-"),
               o.get("quote_spent", o.get("quote_received", "-"))))
    print("commissions:", res["commissions"])
    print("balances %s: %s -> %s | %s: %s -> %s" %
          (base, fmt(ctx["start"][base]), res["final_base_free"],
           quote, fmt(ctx["start"][quote]), res["final_quote_free"]))
    print("residual base from this run:", res["residual_base_from_this_run"])
    print("open orders after:", res["open_orders_after"])
    # Verdict truth: CLOSED only on observed exchange state, never on a model.
    if verdict == "CLOSED":
        if final is None or open_after is None:
            verdict, code = "UNKNOWN", 4
            note = "orders terminal but final account/open-orders state could " \
                   "not be read; nothing resent; reconcile from the receipt ids"
        elif open_after:
            verdict, code = "OPEN_EXPOSURE", 3
            note = "%s open order(s) remain on %s; account is not closed; " \
                   "no repair trade" % (len(open_after), SYMBOL)
    ctx["final"], ctx["open_after"] = final, open_after
    finish(verdict, note, code)


def place_confirmed(side, params, exposure):
    """Submit ONE order, then establish its truth by client id. Returns
    (terminal_order, resp_fills). Every unresolved path exits with the
    exposure stated; a mutating request is NEVER resent."""
    kind, resp = http("POST", "/api/v3/order", params, signed=True)
    state, order = query_order(params["newClientOrderId"])
    if state == "terminal":
        fills = resp.get("fills") if (kind == "ok" and isinstance(resp, dict)) else []
        return order, fills or []
    if state == "not_found" and kind == "api_error":
        # Binance rejected it and it never existed: definite, nothing placed.
        msg = "%s rejected by Binance: %s %s" % (side, resp.get("code"), resp.get("msg"))
        if side == "BUY":
            refuse(msg)
        wrap_up("OPEN_EXPOSURE", msg + "; exposure " + exposure +
                "; no repair trade", 3)
    notes = {"not_found": "submission unclear and order not visible",
             "error": "status query got a definite non-order error",
             "pending": "order not terminal within the poll budget"}
    wrap_up("UNKNOWN", "%s %s; possible exposure up to %s; DO NOT resend - "
            "reconcile client id manually" % (side, notes[state], exposure), 4)


def main():
    global _time_offset_ms, receipt_path
    if not API_KEY or not API_SECRET:
        print("missing BINANCE_API_KEY / BINANCE_API_SECRET in environment")
        sys.exit(2)
    if not SYMBOL or not MAX_QUOTE_RAW or not QUANT_SHA:
        print("missing SYMBOL / MAX_QUOTE / QUANT_SHA in environment")
        sys.exit(2)
    max_quote = d(MAX_QUOTE_RAW)
    if max_quote <= 0:
        print("MAX_QUOTE must be positive")
        sys.exit(2)

    # One runner at a time, ever: a prior unfinished receipt or a standing
    # lock means unreconciled ids may exist - trading again could double-buy.
    os.makedirs(RECEIPT_DIR, exist_ok=True)
    rid = format(int(time.time()), "x") + secrets.token_hex(3)
    buy_id, sell_id = "fml" + rid + "b", "fml" + rid + "s"
    receipt_path = os.path.join(RECEIPT_DIR, "roundtrip-" + rid + ".json")
    receipt.update({"run_id": rid, "buy_client_id": buy_id,
                    "sell_client_id": sell_id, "sha": QUANT_SHA,
                    "symbol": SYMBOL, "max_quote": fmt(max_quote),
                    "started_utc": utc(), "base_url": BASE_URL})
    save()
    for fn in os.listdir(RECEIPT_DIR):
        if fn.endswith(".json") and fn != os.path.basename(receipt_path):
            try:
                prior = json.load(open(os.path.join(RECEIPT_DIR, fn)))
            except Exception:
                prior = {}
            if "verdict" not in prior:
                refuse("prior run %s has no verdict (ids %s / %s); reconcile "
                       "it before any new trade" % (prior.get("run_id", fn),
                                                    prior.get("buy_client_id"),
                                                    prior.get("sell_client_id")))
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, rid.encode())
        os.close(fd)
        ctx["lock"] = True
    except FileExistsError:
        refuse("run lock %s exists: another run is live or ended in exposure; "
               "reconcile, then remove the lock by hand" % LOCK_PATH)

    # Ground truth, read-only.
    kind, t = get_retry("/api/v3/time")
    if kind != "ok":
        refuse("no API connectivity (server time)")
    _time_offset_ms = t["serverTime"] - int(time.time() * 1000)

    kind, info = get_retry("/api/v3/exchangeInfo", {"symbol": SYMBOL})
    if kind != "ok" or not info.get("symbols"):
        refuse("symbol lookup failed")
    sym = info["symbols"][0]
    base, quote = sym["baseAsset"], sym["quoteAsset"]
    ctx["base"], ctx["quote"] = base, quote
    if sym.get("status") != "TRADING" or not sym.get("isSpotTradingAllowed", False):
        refuse("symbol is not live for Spot trading")
    if "MARKET" not in sym.get("orderTypes", []) or \
            not sym.get("quoteOrderQtyMarketAllowed", False):
        refuse("MARKET quoteOrderQty orders not enabled for symbol")
    f = {x["filterType"]: x for x in sym.get("filters", [])}
    lot_step = d(f["LOT_SIZE"]["stepSize"])
    min_qty = d(f["LOT_SIZE"]["minQty"])
    mkt = f.get("MARKET_LOT_SIZE", {})
    mkt_min = d(mkt.get("minQty", "0"))
    mkt_step = d(mkt.get("stepSize", "0"))
    step = max(lot_step, mkt_step)  # market SELL must satisfy both filters
    ctx["step"] = step
    nf = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    min_notional = d(nf.get("minNotional", "0"))
    apply_mkt = bool(nf.get("applyMinToMarket", nf.get("applyToMarket", True)))
    qprec = int(sym.get("quoteAssetPrecision", sym.get("quotePrecision", 8)))
    quote_spend = max_quote.quantize(Decimal(1).scaleb(-qprec), rounding=ROUND_DOWN)

    kind, acct = get_retry("/api/v3/account", signed=True)
    if kind != "ok":
        refuse("account read failed")
    if not acct.get("canTrade", False):
        refuse("account canTrade is false")
    start = balances_of(acct, base, quote)
    ctx["start"] = start
    if start[quote] < quote_spend:
        refuse("quote balance %s %s below spend %s" %
               (fmt(start[quote]), quote, fmt(quote_spend)))

    kind, open_orders = get_retry("/api/v3/openOrders", {"symbol": SYMBOL}, signed=True)
    if kind != "ok":
        refuse("open-orders read failed")
    if open_orders:
        refuse("existing open orders on symbol make the result ambiguous")

    # Binance checks MARKET notional against the rolling average price.
    kind, avg = get_retry("/api/v3/avgPrice", {"symbol": SYMBOL})
    if kind != "ok":
        refuse("average price read failed")
    price = d(avg["price"])

    # A BUY below the exchange minimum cannot start under any fee schedule.
    if apply_mkt and quote_spend < min_notional:
        refuse("MAX_QUOTE %s below minimum BUY notional %s %s" %
               (fmt(quote_spend), fmt(min_notional), quote))
    need_qty = max(min_qty, mkt_min)

    receipt["starting_state"] = {
        "base_free": fmt(start[base]), "quote_free": fmt(start[quote]),
        "avg_price": fmt(price), "open_orders": 0,
        "filters": {"step": fmt(step), "min_qty": fmt(min_qty),
                    "market_min_qty": fmt(mkt_min),
                    "min_notional": fmt(min_notional),
                    "apply_min_to_market": apply_mkt},
        "planned_spend": fmt(quote_spend)}
    save()

    # Non-mutating test order (inline validation only, never a milestone).
    buy_params = {"symbol": SYMBOL, "side": "BUY", "type": "MARKET",
                  "quoteOrderQty": fmt(quote_spend), "newClientOrderId": buy_id,
                  "newOrderRespType": "FULL"}
    test_params = dict(buy_params)
    test_params["computeCommissionRates"] = "true"
    kind, resp = http("POST", "/api/v3/order/test", test_params, signed=True)
    if kind == "unclear":
        first_fail("order_test", resp)
        kind, resp = http("POST", "/api/v3/order/test", test_params, signed=True)
    if kind == "api_error":
        refuse("test order rejected: %s %s" % (resp.get("code"), resp.get("msg")))
    if kind == "unclear":
        refuse("connectivity unclear on non-mutating test order; not starting")

    # Binance's test-order response contains the taker rate for this exact BUY,
    # including account-specific standard, tax, and special commissions. Assume
    # all can be deducted from received base; BNB payment can only improve this.
    try:
        buy_fee_rate = sum((d(resp[k]["taker"]) for k in (
            "standardCommissionForOrder", "taxCommissionForOrder",
            "specialCommissionForOrder")), Decimal(0))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        refuse("test order returned no usable commission rates; not starting")
    sellability_cushion = buy_fee_rate + SLIPPAGE_CUSHION
    if not buy_fee_rate.is_finite() or buy_fee_rate < 0 or \
            sellability_cushion >= 1:
        refuse("test order returned an invalid commission rate; not starting")
    receipt["starting_state"].update({
        "buy_taker_commission_rate": fmt(buy_fee_rate),
        "sellability_cushion": fmt(sellability_cushion)})
    save()

    # Feasibility at the approved ceiling, modeling the REAL two-floor path:
    # the BUY returns a step-multiple, its worst-case base fee is subtracted,
    # then the SELL floors again. Never raise the amount; report the sufficient
    # minimum instead.
    bought_est = floor_step(quote_spend / price, step)
    est_sell = floor_step(bought_est * (1 - sellability_cushion), step)
    if est_sell < need_qty or (apply_mkt and est_sell * price < min_notional):
        req_sell = max(need_qty,
                       (min_notional / price) if apply_mkt else Decimal(0))
        req_quote = (req_sell + 2 * step) * price / (1 - sellability_cushion)
        refuse("approved amount too small to sell back after fees and step "
               "rounding; sufficient minimum ~ %s %s" % (fmt(req_quote), quote))

    # The BUY. One submission; truth established only by its client id.
    receipt["buy"] = {"submitted_utc": utc()}
    save()
    buy_final, buy_resp_fills = place_confirmed(
        "BUY", buy_params, "%s %s" % (fmt(quote_spend), quote))
    executed = d(buy_final.get("executedQty", "0"))
    quote_spent = d(buy_final.get("cummulativeQuoteQty", "0"))
    ctx["quote_spent"] = quote_spent
    receipt["buy"].update({"order_id": buy_final.get("orderId"),
                           "status": buy_final.get("status"),
                           "executed_qty": fmt(executed),
                           "quote_spent": fmt(quote_spent)})
    save()
    if executed == 0:
        finish("REJECTED_BEFORE_BUY", "buy terminal %s with zero executed; "
               "nothing acquired, nothing spent" % buy_final.get("status"), 2)

    # Real fills and commissions - the SELL is sized from exchange truth ONLY.
    buy_trades, covered = order_trades(buy_final.get("orderId"), executed,
                                       buy_resp_fills)
    ctx["buy_trades"] = buy_trades
    receipt["buy"]["fee_by_asset"] = fees_by_asset(buy_trades)
    if covered < executed:
        ctx["residual"] = executed  # worst case: all of it still held
        wrap_up("OPEN_EXPOSURE", "bought %s %s but trade/commission data "
                "never covered the fill; holding; selling on an assumed fee "
                "is forbidden; no repair trade" % (fmt(executed), base), 3)
    buy_fee_base = sum((d(x["commission"]) for x in buy_trades
                        if x.get("commissionAsset") == base), Decimal(0))
    net_base = executed - buy_fee_base
    receipt["buy"]["net_base_acquired"] = fmt(net_base)
    save()

    # Sell ONLY what this run acquired, floored to the effective step.
    sell_qty = floor_step(net_base, step)
    fill_price = quote_spent / executed  # realized price, minutes-fresh
    ctx["residual"] = net_base  # until a SELL executes, all of it is held
    if sell_qty < need_qty or (apply_mkt and sell_qty * fill_price < min_notional):
        wrap_up("OPEN_EXPOSURE", "acquired %s %s cannot be sold back within "
                "filters; holding it; no repair trade" % (fmt(net_base), base), 3)
    sell_params = {"symbol": SYMBOL, "side": "SELL", "type": "MARKET",
                   "quantity": fmt(sell_qty), "newClientOrderId": sell_id,
                   "newOrderRespType": "FULL"}
    receipt["sell"] = {"submitted_utc": utc(), "quantity": fmt(sell_qty)}
    save()
    sell_final, sell_resp_fills = place_confirmed(
        "SELL", sell_params, "%s %s" % (fmt(sell_qty), base))
    sold = d(sell_final.get("executedQty", "0"))
    quote_recv = d(sell_final.get("cummulativeQuoteQty", "0"))
    ctx["quote_recv"] = quote_recv
    receipt["sell"].update({"order_id": sell_final.get("orderId"),
                            "status": sell_final.get("status"),
                            "executed_qty": fmt(sold),
                            "quote_received": fmt(quote_recv)})
    save()
    if sold == 0:
        wrap_up("OPEN_EXPOSURE", "sell terminal %s with zero executed; "
                "exposure %s %s; no repair trade"
                % (sell_final.get("status"), fmt(sell_qty), base), 3)
    sell_trades, sell_covered = order_trades(sell_final.get("orderId"), sold,
                                             sell_resp_fills)
    ctx["sell_trades"] = sell_trades
    receipt["sell"]["fee_by_asset"] = fees_by_asset(sell_trades)
    if sell_covered < sold:
        ctx.pop("residual", None)  # cannot be computed from exchange truth
        wrap_up("UNKNOWN", "sell executed but its trade/commission data never "
                "loaded; residual unknowable; reconcile the sell client id", 4)
    sell_fee_base = sum((d(x["commission"]) for x in sell_trades
                         if x.get("commissionAsset") == base), Decimal(0))
    residual = net_base - sold - sell_fee_base
    ctx["residual"] = residual

    # CLOSED only on observed exchange truth: both orders known, no open
    # orders seen, non-negative dust residual, and the balance deltas agree
    # with the fills and fees (wrap_up downgrades to UNKNOWN if reads fail).
    if residual < 0:
        wrap_up("OPEN_EXPOSURE", "sell consumed %s %s beyond this run's "
                "acquisition - pre-existing base was touched; report to owner"
                % (fmt(-residual), base), 3)
    if residual >= step:
        wrap_up("OPEN_EXPOSURE", "residual %s %s from this run is at or above "
                "the sell step; not dust; no repair trade"
                % (fmt(residual), base), 3)
    kind, acct2 = get_retry("/api/v3/account", signed=True)
    if kind != "ok":
        wrap_up("UNKNOWN", "orders complete but final balances unreadable; "
                "nothing resent", 4)
    final = balances_of(acct2, base, quote)
    fees = fees_by_asset(buy_trades, sell_trades)
    base_delta_ok = ((final[base] - start[base]) - residual).copy_abs() < step
    q_tol = Decimal(1).scaleb(-qprec) * 2
    exp_quote = quote_recv - quote_spent - fee_in(fees, quote)
    quote_delta_ok = ((final[quote] - start[quote]) - exp_quote).copy_abs() <= q_tol
    if not (base_delta_ok and quote_delta_ok):
        wrap_up("UNKNOWN", "balance deltas do not match fills+fees (base ok: "
                "%s, quote ok: %s); reconcile before trusting this run"
                % (base_delta_ok, quote_delta_ok), 4)
    wrap_up("CLOSED", "round trip complete; fills and fees explain the "
            "observed balance changes; residual is dust below the sell step", 0)


if __name__ == "__main__":
    main()
