#!/usr/bin/env python3
"""Run one owner-armed Binance Spot round trip, then exit.

The shared execution adapters own all exchange wire and schema truth. This CLI
owns configuration, one-run identity, durable receipts, exposure policy, and
plain operator output. It never retries or repairs a live mutation.
"""

import json, os, secrets, sys, time
from decimal import Decimal, DecimalException, InvalidOperation

from execution import binance_http, binance_spot


BASE_URL = os.environ.get("BINANCE_BASE_URL", binance_http.SPOT_BASE_URL)
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
SYMBOL = os.environ.get("SYMBOL", "").upper()
MAX_QUOTE_RAW = os.environ.get("MAX_QUOTE", "")
QUANT_SHA = os.environ.get("QUANT_SHA", "")
RECEIPT_DIR = os.path.expanduser(os.environ.get("RECEIPT_DIR", "~/quant_receipts"))
LOCK_PATH = os.path.join(RECEIPT_DIR, "run.lock")
SLIPPAGE_CUSHION = Decimal("0.001")

receipt, receipt_path, ctx, transport = {}, "", {"lock": False}, None


def d(value):
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)): raise ValueError("money must be exact decimal text")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError): raise ValueError("money is unreadable") from None
    if not amount.is_finite(): raise ValueError("money must be finite")
    return amount


def fmt(value): return format(value, "f")



def balances_of(acct, *assets):
    """TEMPORARY (Lead, W1 landed): kept only for job_runner's Spot-momentum
    path until W6D routes it through execution.binance_spot.read_account_state
    and deletes this use (map rows 006, 007, 121)."""
    free = {b["asset"]: d(b["free"]) for b in acct.get("balances", [])}
    return {a: free.get(a, Decimal(0)) for a in assets}

def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sync_directory(path):
    directory_fd = os.open(path or ".", os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def save():
    if not receipt_path: raise RuntimeError("receipt path is unavailable")
    temporary = receipt_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, receipt_path)
    _sync_directory(os.path.dirname(receipt_path))


def first_fail(where, detail):
    if "first_failure" not in receipt:
        receipt["first_failure"] = {
            "utc": utc(), "where": where, "detail": str(detail)[:200]}
        if receipt_path: save()


def finish(verdict, note, code):
    receipt["verdict"] = verdict
    receipt["verdict_note"] = note
    receipt["finished_utc"] = utc()
    save()
    for key in ("run_id", "sha", "symbol", "max_quote",
                "buy_client_id", "sell_client_id"):
        print(key + ":", receipt.get(key, ""))
    if "first_failure" in receipt: print("first_failure:", json.dumps(receipt["first_failure"]))
    print("VERDICT:", verdict)
    print("NOTE:", note)
    print("RECEIPT:", receipt_path)
    if ctx["lock"] and verdict in ("CLOSED", "REJECTED_BEFORE_BUY"):
        os.unlink(LOCK_PATH)
        _sync_directory(RECEIPT_DIR)
    sys.exit(code)


def refuse(reason):
    finish("REJECTED_BEFORE_BUY", reason, 2)


def _read_note(result):
    error = getattr(result, "error", None)
    if error is None and isinstance(result, binance_http.HttpResult): error = result
    kind = getattr(result, "kind", getattr(result, "tag", "unclear"))
    code, message = getattr(error, "code", None), getattr(error, "msg", None)
    return "%s code=%s message=%s" % (kind, code, message)


def _balance_pair(state, base, quote):
    if not isinstance(state, binance_spot.SpotAccountState): return None
    found = {balance.asset: balance.free for balance in state.balances}
    if base not in found or quote not in found: return None
    return {base: found[base], quote: found[quote]}


def _fee_map(fees): return {fee.asset: fmt(fee.amount) for fee in fees}


def _fee_amount(fees, asset):
    return next((fee.amount for fee in fees if fee.asset == asset), Decimal(0))


def _fill_rows(fills):
    return [{"trade_id": f.trade_id, "order_id": f.order_id, "price": fmt(f.price),
             "quantity": fmt(f.quantity), "quote_quantity": fmt(f.quote_quantity),
             "commission_asset": f.fee.asset, "commission": fmt(f.fee.amount), "side": f.side,
             "is_maker": f.is_maker, "time_ms": f.time_ms} for f in fills]


def _record_coverage(side, order, coverage):
    fills = coverage.fills if coverage is not None else order.response_fills
    with binance_http.exact_context(): full_quantity = sum((fill.quantity for fill in order.response_fills), Decimal(0))
    fees = coverage.fees if coverage is not None else binance_spot.fees_by_asset(fills)
    complete = bool(coverage is not None and coverage.complete)
    receipt[side].update({"fills": _fill_rows(fills), "fills_complete": complete,
        "covered_quantity": fmt(coverage.covered_quantity if coverage else full_quantity),
        "coverage_source": "FULL" if full_quantity == order.executed_quantity else "myTrades",
        "fee_by_asset": _fee_map(fees)})
    ctx[side + "_fills"], ctx[side + "_coverage_complete"] = fills, complete
    save(); return fees


def write_intent(intent):
    """Durably attach one adapter intent to its owned side before any wire."""
    try:
        if not isinstance(intent, binance_http.MutationIntent): raise ValueError("intent type is invalid")
        if not ctx.get("lock") or intent.git_sha != QUANT_SHA: raise ValueError("run ownership is unavailable")
        sides = {receipt.get("buy_client_id"): "buy",
                 receipt.get("sell_client_id"): "sell"}
        side = sides.get(intent.identity)
        if side is None or intent.venue != "BINANCE_SPOT" or intent.operation != "ORDER": raise ValueError("intent identity is not owned by this run")
        with open(LOCK_PATH, "rb") as stream:
            lock_owner = stream.read()
        if lock_owner != str(receipt.get("run_id", "")).encode(): raise ValueError("run lock ownership does not agree")
        target = receipt.get(side)
        if not isinstance(target, dict) or "intent" in target: raise ValueError("intent side is unavailable")
        target["intent"] = {
            "venue": intent.venue, "operation": intent.operation,
            "identity": intent.identity, "git_sha": intent.git_sha,
            "params": [list(pair) for pair in intent.params]}
        save()
    except Exception:
        raise binance_http.IntentNotDurable(
            "intent was not durably written") from None


def wrap_up(verdict, note, code):
    """Read one combined final account proof and finish after a mutation."""
    base, quote = ctx["base"], ctx["quote"]
    try:
        account = binance_spot.read_account_state(transport, BASE_URL, SYMBOL)
    except binance_http.AdapterRefusal as error:
        account = None
        first_fail("final_account_state", error)
    state = account.value if account is not None and account.kind == "ok" else None
    final = _balance_pair(state, base, quote)
    open_after = state.open_order_ids if state is not None else None
    if final is None or open_after is None:
        first_fail("final_account_state", _read_note(account) if account else "unproved")
    fills_complete = bool(ctx.get("buy_coverage_complete") and ctx.get("sell_coverage_complete"))
    all_fills = tuple(ctx.get("buy_fills", ())) + tuple(ctx.get("sell_fills", ()))
    try:
        known_fees = binance_spot.fees_by_asset(all_fills); commissions = _fee_map(known_fees)
    except binance_http.AdapterRefusal as error:
        known_fees, commissions = None, {}; first_fail("final_fees", error)
    fees = known_fees if fills_complete else None
    quote_spent, quote_received = ctx.get("quote_spent"), ctx.get("quote_recv")
    net_cost = None
    if (isinstance(quote_spent, Decimal) and isinstance(quote_received, Decimal)
            and fees is not None):
        try:
            with binance_http.exact_context():
                net_cost = quote_spent - quote_received + _fee_amount(fees, quote)
        except DecimalException: first_fail("final_quote_cost", "arithmetic is not exact")
    result = {"commissions": commissions, "fills_complete": fills_complete,
        "quote_spent": fmt(quote_spent) if isinstance(quote_spent, Decimal) else "UNKNOWN",
        "quote_received": fmt(quote_received) if isinstance(quote_received, Decimal) else "UNKNOWN",
        "net_quote_cost_incl_fees": fmt(net_cost) if net_cost is not None else "UNKNOWN",
        "residual_base_from_this_run": fmt(ctx["residual"]) if isinstance(ctx.get("residual"), Decimal) else "UNKNOWN",
        "final_base_free": fmt(final[base]) if final else "UNKNOWN",
        "final_quote_free": fmt(final[quote]) if final else "UNKNOWN",
        "open_orders_after": len(open_after) if open_after is not None else "UNKNOWN"}
    receipt["result"] = result
    receipt["legs"] = [{"stage": side.upper(), "market": "spot", "side": side.upper(),
         "status": leg.get("status") or "UNKNOWN", "executed_qty": leg.get("executed_qty", "UNKNOWN"),
         "quote": leg.get("quote_spent", leg.get("quote_received", "UNKNOWN")),
         "order_id": leg.get("order_id", ""), "fee_by_asset": leg.get("fee_by_asset", {})}
        for side in ("buy", "sell") for leg in (receipt.get(side),) if isinstance(leg, dict)]
    save()
    for side in ("buy", "sell"):
        order = receipt.get(side, {})
        print("%s: status=%s order_id=%s executed=%s quote=%s" %
              (side, order.get("status", "-"), order.get("order_id", "-"),
               order.get("executed_qty", "-"),
               order.get("quote_spent", order.get("quote_received", "-"))))
    print("commissions:", result["commissions"])
    print("balances %s: %s -> %s | %s: %s -> %s" %
          (base, fmt(ctx["start"][base]), result["final_base_free"],
           quote, fmt(ctx["start"][quote]), result["final_quote_free"]))
    print("residual base from this run:", result["residual_base_from_this_run"])
    print("open orders after:", result["open_orders_after"])
    if verdict == "CLOSED":
        if final is None or open_after is None:
            verdict, code = "UNKNOWN", 4
            note = "orders are terminal but the final account proof is unavailable; reconcile the receipt ids"
        elif open_after:
            verdict, code = "OPEN_EXPOSURE", 3
            note = "%s open order(s) remain on %s; no repair order was sent" % (len(open_after), SYMBOL)
        elif fees is None:
            verdict, code = "UNKNOWN", 4
            note = "orders are terminal but complete fill and fee proof is unavailable"
        else:
            residual = ctx.get("residual")
            try:
                with binance_http.exact_context():
                    expected_quote = quote_received - quote_spent - _fee_amount(fees, quote)
                    base_ok = isinstance(residual, Decimal) and final[base] - ctx["start"][base] == residual
                    quote_ok = final[quote] - ctx["start"][quote] == expected_quote
            except DecimalException: base_ok = quote_ok = False
            if not base_ok or not quote_ok:
                verdict, code = "UNKNOWN", 4
                note = "balance deltas do not exactly match the proven fills and fees"
    ctx["final"], ctx["open_after"] = final, open_after
    finish(verdict, note, code)


def _config_exit(message): print(message); sys.exit(2)


def main():
    global receipt_path, transport
    if not API_KEY or not API_SECRET: _config_exit("missing BINANCE_API_KEY / BINANCE_API_SECRET in environment")
    if not BASE_URL or not SYMBOL or not MAX_QUOTE_RAW or not QUANT_SHA: _config_exit("missing base URL / SYMBOL / MAX_QUOTE / QUANT_SHA in environment")
    try:
        max_quote = d(MAX_QUOTE_RAW)
    except ValueError: _config_exit("MAX_QUOTE must be a finite Decimal")
    if max_quote <= 0: _config_exit("MAX_QUOTE must be positive")
    if len(QUANT_SHA) != 40 or any(char not in "0123456789abcdef" for char in QUANT_SHA): _config_exit("QUANT_SHA must be a full lowercase 40-character hex value")
    try:
        transport = binance_http.BinanceHttp(API_KEY, API_SECRET)
    except binance_http.AdapterRefusal: _config_exit("Binance transport configuration was refused")

    receipt.clear()
    ctx.clear()
    ctx["lock"] = False
    os.makedirs(RECEIPT_DIR, exist_ok=True)
    rid = format(int(time.time()), "x") + secrets.token_hex(3)
    buy_id, sell_id = "fml" + rid + "b", "fml" + rid + "s"
    receipt_path = os.path.join(RECEIPT_DIR, "roundtrip-" + rid + ".json")
    receipt.update({
        "run_id": rid, "buy_client_id": buy_id, "sell_client_id": sell_id,
        "sha": QUANT_SHA, "symbol": SYMBOL, "max_quote": fmt(max_quote),
        "started_utc": utc(), "base_url": BASE_URL})
    save()
    for filename in os.listdir(RECEIPT_DIR):
        if filename.endswith(".json") and filename != os.path.basename(receipt_path):
            try:
                with open(os.path.join(RECEIPT_DIR, filename), encoding="utf-8") as stream:
                    prior = json.load(stream)
            except Exception:
                prior = {}
            if not isinstance(prior, dict) or "verdict" not in prior: refuse("a prior run has no verdict; reconcile it before a new trade")
    try:
        lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(lock_fd, rid.encode())
            os.fsync(lock_fd)
        finally:
            os.close(lock_fd)
        _sync_directory(RECEIPT_DIR)
        ctx["lock"] = True
    except FileExistsError: refuse("the run lock already exists; reconcile it before a new trade")
    except Exception: refuse("the run lock could not be made durable; no order was sent")

    clock = transport.sync_time(BASE_URL, "/api/v3/time")
    if clock.kind != "ok": refuse("server time could not be synchronized")
    try:
        market_read = binance_spot.read_market(transport, BASE_URL, SYMBOL)
    except binance_http.AdapterRefusal: refuse("Spot market configuration was refused")
    if market_read.kind != "ok" or market_read.value is None: refuse("fresh Spot market facts are unavailable")
    market = market_read.value
    base, quote = market.base_asset, market.quote_asset
    ctx.update({"base": base, "quote": quote, "step": market.step_size})
    receipt["base_asset"], receipt["quote_asset"] = base, quote
    account_read = binance_spot.read_account_state(transport, BASE_URL, SYMBOL)
    state = account_read.value if account_read.kind == "ok" else None
    start = _balance_pair(state, base, quote)
    if state is None or start is None: refuse("the starting account state is unproved")
    if not state.can_trade: refuse("account canTrade is false")
    if state.open_order_ids: refuse("existing open orders make the round trip ambiguous")
    quote_spend = max_quote
    if start[quote] < quote_spend: refuse("quote balance is below the approved spend")
    ctx["start"] = start
    average = binance_http.read(
        transport, BASE_URL, "/api/v3/avgPrice", (("symbol", SYMBOL),), signed=False)
    try:
        price = d(average.payload.get("price")) if average.kind == "ok" and isinstance(average.payload, dict) else None
    except ValueError: price = None
    if price is None or price <= 0: refuse("the average price is unproved")
    receipt["starting_state"] = {
        "base_free": fmt(start[base]), "quote_free": fmt(start[quote]),
        "avg_price": fmt(price), "open_orders": 0,
        "filters": {"step": fmt(market.step_size),
                    "min_qty": fmt(market.min_quantity),
                    "market_min_qty": "UNKNOWN",
                    "min_notional": fmt(market.min_notional),
                    "apply_min_to_market": True,
                    "base_balance_tolerance": "0"},
        "planned_spend": fmt(quote_spend)}
    save()
    try:
        buy_plan = binance_spot.build_market_order(
            market, "BUY", buy_id, QUANT_SHA, quote_limit=quote_spend)
    except binance_http.AdapterRefusal: refuse("the approved BUY does not satisfy fresh Spot filters")
    test_params = buy_plan.intent.params + (("computeCommissionRates", "true"),)
    test_result = transport("POST", BASE_URL, "/api/v3/order/test", test_params, True)
    if test_result.kind == "unclear":
        first_fail("order_test", _read_note(test_result))
        test_result = transport("POST", BASE_URL, "/api/v3/order/test", test_params, True)
    if test_result.kind == "api_error": refuse("test order was refused: %s %s" % (test_result.code, test_result.msg))
    if test_result.kind != "ok": refuse("the non-mutating test order remained unclear")
    try:
        payload = test_result.payload
        with binance_http.exact_context():
            buy_fee_rate = sum((d(payload[name]["taker"]) for name in (
                "standardCommissionForOrder", "taxCommissionForOrder",
                "specialCommissionForOrder")), Decimal(0))
            sellability_cushion = buy_fee_rate + SLIPPAGE_CUSHION
    except (KeyError, TypeError, ValueError, DecimalException): refuse("the test order returned no usable commission rates")
    if buy_fee_rate < 0 or sellability_cushion >= 1: refuse("the test order returned an invalid commission rate")
    receipt["starting_state"].update({
        "buy_taker_commission_rate": fmt(buy_fee_rate),
        "sellability_cushion": fmt(sellability_cushion)})
    save()
    try:
        with binance_http.exact_context():
            estimated_buy = binance_spot.build_market_order(
                market, "SELL", sell_id, QUANT_SHA, quantity=quote_spend / price)
            binance_spot.build_market_order(market, "SELL", sell_id, QUANT_SHA,
                quantity=estimated_buy.quantity * (Decimal(1) - sellability_cushion))
    except (ArithmeticError, binance_http.AdapterRefusal): refuse("approved amount is too small to sell back after fees and fresh filters")

    receipt["buy"] = {"submitted_utc": utc()}
    save()
    try:
        buy = binance_spot.submit_order_once(
            transport, BASE_URL, buy_plan, write_intent)
    except (binance_http.AdapterRefusal, binance_http.IntentNotDurable):
        refuse("the BUY intent was not accepted durably; no order was sent")
    if buy.tag == "error": refuse("the BUY was refused and exact-id absence was proved: %s" % _read_note(buy))
    if buy.tag != "terminal": wrap_up("UNKNOWN", "the BUY outcome is unresolved; reconcile its exact client id before another run", 4)
    executed, quote_spent = buy.executed_quantity, buy.quote_quantity
    if not isinstance(executed, Decimal) or not isinstance(quote_spent, Decimal):
        wrap_up("UNKNOWN", "the terminal BUY lacks exact quantities", 4)
    ctx["quote_spent"] = quote_spent
    receipt["buy"].update({
        "order_id": buy.identity.order_id, "status": buy.status, "terminal_utc": utc(),
        "executed_qty": fmt(executed), "quote_spent": fmt(quote_spent)})
    save()
    if executed == 0: finish("REJECTED_BEFORE_BUY", "the terminal BUY executed zero quantity", 2)
    buy_fill_read = binance_spot.read_fills(transport, BASE_URL, buy)
    buy_coverage = buy_fill_read.value if buy_fill_read.kind == "ok" else None
    try:
        buy_fees = _record_coverage("buy", buy, buy_coverage)
    except (binance_http.AdapterRefusal, DecimalException) as error:
        ctx.pop("residual", None); first_fail("buy_fees", error)
        wrap_up("UNKNOWN", "the BUY fee proof is unavailable", 4)
    if buy_coverage is None:
        ctx.pop("residual", None)
        first_fail("buy_fills", _read_note(buy_fill_read))
        wrap_up("UNKNOWN", "the BUY executed but complete fill and fee proof is unavailable", 4)
    if not buy_coverage.complete:
        ctx.pop("residual", None)
        first_fail("buy_fills", "fill coverage is incomplete")
        wrap_up("UNKNOWN", "the BUY fill ledger is incomplete, so no SELL was sized", 4)
    try:
        net_base = binance_spot.net_base_quantity(executed, base, buy_fees)
    except binance_http.AdapterRefusal as error:
        ctx.pop("residual", None)
        first_fail("buy_net_base", error)
        wrap_up("OPEN_EXPOSURE", "the run-owned net base quantity is unproved", 3)
    receipt["buy"]["net_base_acquired"] = fmt(net_base)
    save()

    ctx["residual"] = net_base
    fresh_read = binance_spot.read_market(transport, BASE_URL, SYMBOL)
    fresh_market = fresh_read.value if fresh_read.kind == "ok" else None
    if (fresh_market is None or fresh_market.symbol != market.symbol
            or fresh_market.base_asset != base or fresh_market.quote_asset != quote):
        wrap_up("OPEN_EXPOSURE", "fresh SELL market facts are unavailable or disagree", 3)
    try:
        sell_plan = binance_spot.build_market_order(
            fresh_market, "SELL", sell_id, QUANT_SHA, quantity=net_base)
    except binance_http.AdapterRefusal:
        wrap_up("OPEN_EXPOSURE", "the run-owned net base cannot satisfy fresh SELL filters", 3)
    receipt["sell"] = {"submitted_utc": utc(), "quantity": fmt(sell_plan.quantity),
                       "planned_residual": fmt(sell_plan.residual),
                       "fresh_step": fmt(fresh_market.step_size)}
    save()
    try:
        sell = binance_spot.submit_order_once(
            transport, BASE_URL, sell_plan, write_intent)
    except binance_http.IntentNotDurable:
        wrap_up("OPEN_EXPOSURE", "the SELL intent was not durable, so no SELL was sent", 3)
    except binance_http.AdapterRefusal:
        wrap_up("OPEN_EXPOSURE", "the SELL plan was refused before wire", 3)
    if sell.tag == "error":
        wrap_up("OPEN_EXPOSURE", "the SELL was refused and the acquired base remains", 3)
    if sell.tag != "terminal": ctx.pop("residual", None); wrap_up("UNKNOWN", "the SELL outcome is unresolved; reconcile its exact client id", 4)
    sold, quote_received = sell.executed_quantity, sell.quote_quantity
    if not isinstance(sold, Decimal) or not isinstance(quote_received, Decimal):
        ctx.pop("residual", None)
        wrap_up("UNKNOWN", "the terminal SELL lacks exact quantities", 4)
    ctx["quote_recv"] = quote_received
    receipt["sell"].update({
        "order_id": sell.identity.order_id, "status": sell.status, "terminal_utc": utc(),
        "executed_qty": fmt(sold), "quote_received": fmt(quote_received)})
    save()
    if sold == 0:
        wrap_up("OPEN_EXPOSURE", "the terminal SELL executed zero quantity", 3)
    sell_fill_read = binance_spot.read_fills(transport, BASE_URL, sell)
    sell_coverage = sell_fill_read.value if sell_fill_read.kind == "ok" else None
    try:
        sell_fees = _record_coverage("sell", sell, sell_coverage)
    except (binance_http.AdapterRefusal, DecimalException) as error:
        ctx.pop("residual", None); first_fail("sell_fees", error)
        wrap_up("UNKNOWN", "the SELL fee proof is unavailable", 4)
    if sell_coverage is None:
        ctx.pop("residual", None)
        first_fail("sell_fills", _read_note(sell_fill_read))
        wrap_up("UNKNOWN", "the SELL executed but its fill and fee proof is unavailable", 4)
    if not sell_coverage.complete:
        ctx.pop("residual", None)
        first_fail("sell_fills", "fill coverage is incomplete")
        wrap_up("UNKNOWN", "the SELL fill ledger is incomplete", 4)
    try:
        with binance_http.exact_context(): residual = net_base - sold - _fee_amount(sell_fees, base)
    except DecimalException:
        ctx.pop("residual", None)
        wrap_up("UNKNOWN", "the run residual cannot be computed exactly", 4)
    ctx["residual"] = residual
    if residual < 0:
        wrap_up("OPEN_EXPOSURE", "the SELL consumed base beyond this run's acquisition", 3)
    if residual >= fresh_market.step_size:
        wrap_up("OPEN_EXPOSURE", "the run residual is at or above the fresh SELL step", 3)
    wrap_up("CLOSED", "the round trip is complete and exact balance changes agree", 0)


if __name__ == "__main__":
    main()
