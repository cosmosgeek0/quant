#!/usr/bin/env python3
"""Run one owner-armed USD-M Futures open and reduce-only close.

The shared execution adapters own exchange wire and schema truth. This proof
CLI owns exact sizing policy, one-run identity, durable receipts, verdicts,
and plain operator output. It never retries or repairs a mutation.
"""

import json, os, re, secrets, sys, time
from decimal import Decimal, DecimalException, InvalidOperation
from execution import binance_futures as bf
from execution import binance_http as bh

try:
    from limits import PER_LEG_CAP_USDT
except ImportError:
    from src.limits import PER_LEG_CAP_USDT


API_KEY, API_SECRET = os.environ.get("BINANCE_API_KEY", ""), os.environ.get("BINANCE_API_SECRET", "")
SYMBOL, QUANT_SHA = os.environ.get("SYMBOL", "").upper(), os.environ.get("QUANT_SHA", "").lower()
MAX_NOTIONAL_RAW, MAX_SLIPPAGE_BPS_RAW = os.environ.get("MAX_NOTIONAL", ""), os.environ.get("MAX_SLIPPAGE_BPS", "10")
RECEIPT_DIR = os.path.expanduser(os.environ.get("RECEIPT_DIR", "~/quant_receipts"))
LOCK_PATH = os.path.join(RECEIPT_DIR, "futures-hand.lock")
LANE_PREFIX, SHORT_LANE_PREFIX = "l2fh-", "l2fs-"
HANDS = {
    "long": {"prefix": LANE_PREFIX, "open_side": "BUY", "close_side": "SELL"},
    "short": {"prefix": SHORT_LANE_PREFIX, "open_side": "SELL", "close_side": "BUY"}}
LANE_HARD_CEILING, TARGET_LEVERAGE, MAX_ALLOWED_LEVERAGE = PER_LEG_CAP_USDT, 1, 2
CLOSE_PRICE_FLOOR = Decimal("0.99")
receipt, receipt_path = {}, ""
ctx, transport, _sleep = {"lock": False, "mutated": False}, None, time.sleep


class PreflightError(Exception): pass
class CapabilityBlocked(PreflightError): pass


def d(value):
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
        raise PreflightError("money must be exact Decimal text")
    try:
        with bh.exact_context(): result = +Decimal(value)
    except (DecimalException, InvalidOperation, ValueError):
        raise PreflightError("money is unreadable") from None
    if not result.is_finite(): raise PreflightError("money must be finite")
    return result


def fmt(value): return format(value, "f")


def floor_step(value, step):
    if not isinstance(value, Decimal) or not isinstance(step, Decimal) or step <= 0:
        raise PreflightError("quantity and step must be positive Decimals")
    try:
        with bh.exact_context(): return (value // step) * step
    except DecimalException:
        raise PreflightError("step arithmetic is not exact") from None


def ceil_step(value, step):
    floor = floor_step(value, step)
    try:
        with bh.exact_context(): return floor if floor == value else floor + step
    except DecimalException:
        raise PreflightError("step arithmetic is not exact") from None


def plan_open(market, max_notional, slippage_bps, open_side="BUY"):
    """Apply the hand's cap and close-cushion policy to typed market facts."""
    if open_side not in ("BUY", "SELL"): raise PreflightError("open side must be BUY or SELL")
    if not isinstance(market, bf.FuturesMarket): raise PreflightError("a typed Futures market is required")
    if market.status != "TRADING": raise PreflightError("symbol is not TRADING")
    if market.contract_type != "PERPETUAL": raise PreflightError("symbol is not a PERPETUAL contract")
    if market.quote_asset != "USDT" or market.margin_asset != "USDT": raise PreflightError("lane accepts only USDT-quoted, USDT-margined symbols")
    if not {"LIMIT", "MARKET"}.issubset(set(market.order_types)): raise PreflightError("symbol does not support LIMIT open and MARKET close")
    if "IOC" not in market.time_in_force: raise PreflightError("symbol does not support IOC")
    if not isinstance(max_notional, Decimal) or not isinstance(slippage_bps, Decimal): raise PreflightError("sizing inputs must be Decimal")
    values = (
        market.tick_size, market.step_size, market.min_quantity,
        market.max_quantity, market.bid, market.ask, market.mark_price,
        market.multiplier_up, market.multiplier_down,
    )
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in values): raise PreflightError("symbol filters or market prices are unusable")
    if (market.tick_size <= 0 or market.step_size <= 0 or market.ask <= 0
            or market.bid <= 0 or market.mark_price <= 0
            or market.min_quantity <= 0 or market.max_quantity < market.min_quantity):
        raise PreflightError("symbol filters or market prices are non-positive")
    if max_notional <= 0 or max_notional > LANE_HARD_CEILING: raise PreflightError("planned order exceeds the lane hard ceiling")
    if not slippage_bps.is_finite() or not Decimal(0) <= slippage_bps <= Decimal(100): raise PreflightError("slippage basis points are outside the allowed range")
    try:
        with bh.exact_context():
            ratio = slippage_bps / Decimal(10000)
            cap_price = ceil_step(market.ask * (Decimal(1) + ratio), market.tick_size)
            if open_side == "BUY": limit_price = cap_price
            else:
                limit_price = floor_step(market.bid * (Decimal(1) - ratio), market.tick_size)
                if limit_price <= 0: raise PreflightError("floored SELL limit price is not positive")
            if limit_price < market.min_price or (
                    market.max_price > 0 and limit_price > market.max_price):
                raise PreflightError("capped IOC price is outside PRICE_FILTER")
            if open_side == "BUY" and limit_price > market.mark_price * market.multiplier_up: raise PreflightError("capped IOC price is outside PERCENT_PRICE")
            if open_side == "SELL" and limit_price < market.mark_price * market.multiplier_down: raise PreflightError("floored IOC price is outside PERCENT_PRICE")
            cap_quantity = (max_notional // (cap_price * market.step_size)) * market.step_size
            market_quantity = (market.max_quantity // market.step_size) * market.step_size
            quantity = min(cap_quantity, market_quantity)
            planned_notional = quantity * cap_price
            entry_floor = quantity * limit_price
            close_notional_floor = quantity * market.mark_price * CLOSE_PRICE_FLOOR
    except DecimalException:
        raise PreflightError("sizing arithmetic is not exact") from None
    if quantity < market.min_quantity: raise PreflightError("hard ceiling is below the common minimum quantity")
    if entry_floor < market.min_notional: raise PreflightError("hard ceiling is below MIN_NOTIONAL")
    if close_notional_floor < market.min_notional: raise PreflightError("hard ceiling leaves no 1% MIN_NOTIONAL close cushion")
    if planned_notional > max_notional or quantity % market.step_size: raise PreflightError("planned order exceeds the lane hard ceiling")
    return {
        "open_side": open_side,
        "quantity": quantity,
        "limit_price": limit_price,
        "max_fill_notional": planned_notional,
        "min_notional": market.min_notional,
        "close_notional_floor": close_notional_floor, "step": market.step_size,
        "ask": market.ask, "bid": market.bid, "mark": market.mark_price}


def plan_short_open(market, max_notional, slippage_bps): return plan_open(market, max_notional, slippage_bps, open_side="SELL")
def utc(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
def clean(value, limit=160): return " ".join(str(value).split())[:limit]


def _sync_directory(path):
    descriptor = os.open(path or ".", os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def save():
    if not receipt_path: return
    temporary = receipt_path + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(receipt, handle, indent=2, default=str); handle.flush(); os.fsync(handle.fileno())
    finally:
        if descriptor >= 0: os.close(descriptor)
    os.replace(temporary, receipt_path); _sync_directory(os.path.dirname(receipt_path))


def first_fail(where, detail):
    if "first_failure" not in receipt:
        receipt["first_failure"] = {"utc": utc(), "where": clean(where, 60), "detail": clean(detail, 200)}; save()


def finish(verdict, note, code, release_lock=False):
    receipt.update({"verdict": verdict, "verdict_note": clean(note, 300), "finished_utc": utc()}); save()
    print("lane: L2 FUTURES-HAND"); print("hand:", receipt.get("hand", "")); print("run_id:", receipt.get("run_id", ""))
    print("sha8:", receipt.get("sha", "")[:8]); print("symbol:", receipt.get("symbol", "")); print("hard_ceiling_usdt:", receipt.get("hard_ceiling_usdt", ""))
    print("open_client_id_last4:", receipt.get("open_client_id", "")[-4:]); print("close_client_id_last4:", receipt.get("close_client_id", "")[-4:])
    if "first_failure" in receipt: print("first_failure:", json.dumps(receipt["first_failure"]))
    print("VERDICT:", verdict); print("NOTE:", clean(note, 300)); print("RECEIPT:", receipt_path)
    if release_lock and ctx.get("lock"):
        os.unlink(LOCK_PATH); _sync_directory(RECEIPT_DIR); ctx["lock"] = False
    raise SystemExit(code)


def refuse(note): finish("REJECTED_BEFORE_OPEN", note, 2, release_lock=True)


def _result_note(result):
    error = getattr(result, "error", None)
    if error is None and isinstance(result, bh.HttpResult): error = result
    kind = getattr(result, "kind", getattr(result, "tag", "unclear"))
    code, message = getattr(error, "code", None), getattr(error, "msg", None)
    return clean("%s code=%s message=%s" % (kind, code, message), 200)


def intent_writer(stage, expected_id):
    """Return the run-owned durable writer required by one adapter mutation."""
    operations = {"leverage": "SET_LEVERAGE", "open": "ORDER", "close": "ORDER"}
    identity_fields = {"leverage": "leverage_client_id", "open": "open_client_id", "close": "close_client_id"}
    expected_operation = operations.get(stage)

    def write(intent):
        try:
            if expected_operation is None or not isinstance(intent, bh.MutationIntent): raise ValueError("intent shape is invalid")
            if (intent.venue != "BINANCE_USDM" or intent.operation != expected_operation
                    or intent.identity != expected_id or intent.git_sha != QUANT_SHA
                    or not re.fullmatch(r"[0-9a-f]{40}", intent.git_sha)):
                raise ValueError("intent ownership does not agree")
            if receipt.get(identity_fields[stage]) != expected_id or not ctx.get("lock"): raise ValueError("run identity is not owned")
            with open(LOCK_PATH, "rb") as handle:
                if handle.read() != str(receipt.get("run_id", "")).encode(): raise ValueError("run lock ownership does not agree")
            params = intent.params
            if (type(params) is not tuple or any(
                    type(pair) is not tuple or len(pair) != 2
                    or type(pair[0]) is not str or not pair[0]
                    or type(pair[1]) is not str for pair in params)):
                raise ValueError("intent params are not canonical")
            keys = tuple(pair[0] for pair in params)
            if len(keys) != len(set(keys)): raise ValueError("intent params contain duplicate keys")
            wire = dict(params)
            if expected_operation == "SET_LEVERAGE":
                canonical = ("symbol", "leverage")
                if wire.get("symbol") != SYMBOL or wire.get("leverage") != "1": raise ValueError("leverage intent is not exact")
            elif wire.get("type") == "LIMIT":
                canonical = ("symbol", "side", "positionSide", "type", "timeInForce",
                             "quantity", "price", "reduceOnly", "newClientOrderId",
                             "newOrderRespType")
            else:
                canonical = ("symbol", "side", "positionSide", "type", "quantity",
                             "reduceOnly", "newClientOrderId", "newOrderRespType")
            if keys != canonical or (expected_operation == "ORDER" and (
                    wire.get("symbol") != SYMBOL
                    or wire.get("newClientOrderId") != expected_id)):
                raise ValueError("intent params are not canonical")
            if expected_operation == "ORDER" and (
                    wire.get("side") not in ("BUY", "SELL") or wire.get("positionSide") != "BOTH"
                    or wire.get("newOrderRespType") != "RESULT" or wire.get("type") not in ("LIMIT", "MARKET")
                    or wire.get("reduceOnly") != ("false" if stage == "open" else "true")
                    or stage == "close" and wire.get("type") != "MARKET"
                    or wire.get("type") == "LIMIT" and wire.get("timeInForce") != "IOC"):
                raise ValueError("order intent facts do not agree")
            lowered = tuple(key.lower() for key in keys)
            if any(key in {"signature", "timestamp", "recvwindow"}
                   or any(word in key for word in ("apikey", "api_key", "secret",
                                                   "credential", "authorization"))
                   for key in lowered):
                raise ValueError("intent contains a reserved field")
            if any(secret and secret == value for _, value in params
                   for secret in (API_KEY, API_SECRET)): raise ValueError("intent contains credential material")
            intents = receipt.setdefault("intents", {})
            if not isinstance(intents, dict) or stage in intents: raise ValueError("intent stage is already used")
            intents[stage] = {
                "venue": intent.venue, "operation": intent.operation,
                "identity": intent.identity, "git_sha": intent.git_sha,
                "params": [list(pair) for pair in params],
            }
            save(); ctx["mutated"] = True
        except Exception:
            raise bh.IntentNotDurable("intent was not durably written") from None

    return write


def preflight(max_notional, slippage_bps, open_side="BUY"):
    if transport is None: raise CapabilityBlocked("Binance transport is unavailable")
    try:
        clock = transport.sync_time(bh.FUTURES_BASE_URL, "/fapi/v1/time")
    except Exception:
        raise PreflightError("public Futures server-time read failed") from None
    if not isinstance(clock, bh.HttpResult) or clock.kind != "ok": raise PreflightError("public Futures server-time read failed")
    print("public_futures_time: ok")
    try:
        market_read = bf.read_market(transport, bh.FUTURES_BASE_URL, SYMBOL, sleep=_sleep)
    except bh.AdapterRefusal:
        raise PreflightError("Futures market facts were refused") from None
    if market_read.kind != "ok" or market_read.value is None: raise PreflightError("Futures market facts are unavailable")
    market = market_read.value
    plan = plan_open(market, max_notional, slippage_bps, open_side)
    print("public_symbol_plan: ok"); print("public_max_fill_notional:", fmt(plan["max_fill_notional"]))
    print("public_plan_under_hard_ceiling: true")
    try:
        state_read = bf.read_preflight(
            transport, bh.FUTURES_BASE_URL, market, plan["max_fill_notional"],
            require_flat=True, sleep=_sleep)
    except bh.AdapterRefusal:
        raise CapabilityBlocked("Futures account preflight was refused") from None
    if state_read.kind != "ok" or state_read.value is None: raise CapabilityBlocked("Futures account preflight is unproved")
    state = state_read.value
    ctx["market"] = market
    plan.update({
        "available_balance_sufficient": True,
        "available_balance": state.available_balance,
        "required_margin": state.required_margin,
        "account_can_trade": state.can_trade,
        "position_mode": "ONE_WAY" if state.one_way else None,
        "open_orders": len(state.open_order_ids),
        "position_amount": state.position_quantity,
        "account_position_amount": state.account_position_quantity,
        "current_leverage": state.leverage,
        "target_leverage": TARGET_LEVERAGE,
        "leverage_change_required": state.leverage != TARGET_LEVERAGE,
        "margin_type": None})
    return plan


def final_state():
    market = ctx.get("market")
    if transport is None or not isinstance(market, bf.FuturesMarket):
        first_fail("final_preflight", "typed runtime state is unavailable"); return None
    try:
        state_read = bf.read_preflight(
            transport, bh.FUTURES_BASE_URL, market, None,
            require_flat=False, sleep=_sleep)
    except bh.AdapterRefusal as error:
        first_fail("final_preflight", error); return None
    if state_read.kind != "ok" or state_read.value is None:
        first_fail("final_preflight", _result_note(state_read)); return None
    state = state_read.value
    return {"position_amount": state.position_quantity,
            "account_position_amount": state.account_position_quantity,
            "open_orders": len(state.open_order_ids), "leverage": state.leverage}


def wrap_up(verdict, note, code):
    state = final_state()
    receipt["final_state"] = state or {
        "position_amount": None, "account_position_amount": None,
        "open_orders": None, "leverage": None}
    if verdict in ("CLOSED", "CEILING_BREACH_CONTAINED"):
        if state is None:
            verdict, code, note = "UNKNOWN", 4, "orders are terminal but final Futures state is unproved"
        elif (state["position_amount"] != 0
              or state["account_position_amount"] != 0
              or state["open_orders"] != 0):
            verdict, code, note = "OPEN_EXPOSURE", 3, "the planned pair did not end with two proved-flat positions and no orders"
        elif state["leverage"] != TARGET_LEVERAGE:
            verdict, code, note = "UNKNOWN", 4, "orders are flat but final leverage is not the compiled 1x target"
    finish(verdict, note, code, release_lock=(verdict == "CLOSED"))


def enforce_target_leverage(plan, leverage_id):
    receipt["leverage"] = {
        "before": plan["current_leverage"], "target": TARGET_LEVERAGE,
        "mutation_attempts": 0}; save()
    if TARGET_LEVERAGE > MAX_ALLOWED_LEVERAGE:
        first_fail("leverage", "compiled target exceeds owner hard cap"); refuse("compiled leverage target exceeds the owner hard cap")
    try:
        result = bf.set_leverage_1x_once(
            transport, bh.FUTURES_BASE_URL, SYMBOL, leverage_id, QUANT_SHA,
            intent_writer("leverage", leverage_id), sleep=_sleep)
    except (bh.AdapterRefusal, bh.IntentNotDurable) as error:
        first_fail("leverage", error); refuse("the 1x leverage intent was refused before an order")
    receipt["leverage"].update({
        "before": result.before, "after": result.after,
        "mutated": result.mutated,
        "mutation_attempts": 1 if result.mutated else 0,
        "verified_utc": utc(), "result_kind": result.kind})
    if result.mutated: ctx["mutated"] = True
    save()
    if result.kind == "proven" and result.after == TARGET_LEVERAGE: return
    first_fail("leverage_verify", _result_note(result))
    if result.kind == "api_error": refuse("the 1x leverage request was rejected and no order was sent")
    finish("UNKNOWN", "1x leverage is unproved; no order was sent and nothing retried", 4)


def _submit_order(stage, plan, exposure):
    key = stage.lower()
    try:
        result = bf.submit_order_once(
            transport, bh.FUTURES_BASE_URL, plan,
            intent_writer(key, plan.intent.identity), sleep=_sleep)
    except (bh.AdapterRefusal, bh.IntentNotDurable) as error:
        first_fail(key + "_intent", error)
        if stage == "OPEN": refuse("the open intent was refused before confirmation")
        wrap_up("OPEN_EXPOSURE", "the close intent was refused; exposure " + exposure, 3)
    if result.tag == "terminal":
        if result.side != plan.side or result.reduce_only is not plan.reduce_only:
            first_fail("query_order", "terminal order facts do not match the intent"); wrap_up("UNKNOWN", stage + " order facts conflict with its intent; no retry", 4)
        return result
    if result.tag == "error":
        message = "%s rejected by Binance: %s" % (stage, _result_note(result))
        first_fail(key + "_submit", message)
        if stage == "OPEN": refuse(message)
        wrap_up("OPEN_EXPOSURE", message + "; exposure " + exposure, 3)
    first_fail("query_order", _result_note(result)); wrap_up(
        "UNKNOWN", "%s exact-id query stayed unresolved; possible exposure %s; nothing resent" % (stage, exposure), 4)


def _record_order(stage, order):
    target = receipt[stage]
    target.update({
        "client_id_last4": order.identity.client_id[-4:],
        "order_id_last4": str(order.identity.order_id or "")[-4:],
        "status": order.status,
        "executed_qty": fmt(order.executed_quantity),
        "cum_quote": fmt(order.quote_quantity),
        "side": order.side,
        "reduce_only": order.reduce_only})
    try:
        coverage_read = bf.read_fills(
            transport, bh.FUTURES_BASE_URL, order, sleep=_sleep)
    except bh.AdapterRefusal as error:
        coverage_read = None; first_fail(stage + "_fills", error)
    coverage = (coverage_read.value if coverage_read is not None
                and coverage_read.kind == "ok" else None)
    complete = bool(coverage is not None and coverage.complete)
    if not complete: first_fail(stage + "_fills", _result_note(coverage_read) if coverage_read is not None else "fill coverage is unproved")
    target["fills_complete"] = complete
    target["fees"] = ([{"asset": fee.asset, "amount": fmt(fee.amount)}
                       for fee in coverage.fees] if coverage is not None else None)
    target["fills"] = ([{
        "trade_id": fill.trade_id, "order_id_last4": str(fill.order_id)[-4:],
        "price": fmt(fill.price), "quantity": fmt(fill.quantity),
        "quote_quantity": fmt(fill.quote_quantity),
        "fee_asset": fill.fee.asset, "fee_amount": fmt(fill.fee.amount),
        "side": fill.side, "is_maker": fill.is_maker,
        "time_ms": fill.time_ms,
        "realized_pnl": (fmt(fill.realized_pnl) if fill.realized_pnl is not None else None),
    } for fill in coverage.fills] if coverage is not None else None)
    save(); return complete


def acquire_run(run_id, open_id, close_id, leverage_id, max_notional):
    global receipt_path
    os.makedirs(RECEIPT_DIR, mode=0o700, exist_ok=True)
    try:
        descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, run_id.encode()); os.fsync(descriptor)
        finally: os.close(descriptor)
        _sync_directory(RECEIPT_DIR); ctx["lock"] = True
    except FileExistsError as error:
        raise CapabilityBlocked(
            "futures-hand lock exists; another or unresolved run owns the lane") from error
    receipt_path = os.path.join(RECEIPT_DIR, "futures-hand-" + run_id + ".json")
    receipt.update({
        "run_id": run_id, "open_client_id": open_id,
        "close_client_id": close_id, "leverage_client_id": leverage_id,
        "sha": QUANT_SHA, "symbol": SYMBOL,
        "requested_max_notional": fmt(max_notional),
        "hard_ceiling_usdt": fmt(LANE_HARD_CEILING),
        "started_utc": utc(), "intents": {}})
    save()
    for name in os.listdir(RECEIPT_DIR):
        if (not name.startswith("futures-hand-") or not name.endswith(".json")
                or name == os.path.basename(receipt_path)):
            continue
        try:
            with open(os.path.join(RECEIPT_DIR, name), encoding="utf-8") as handle:
                prior = json.load(handle)
        except Exception as error:
            first_fail("prior_receipt", type(error).__name__); finish("BLOCKED_UNRESOLVED", "prior Futures receipt is unreadable", 3)
        if not isinstance(prior, dict) or "verdict" not in prior:
            finish("BLOCKED_UNRESOLVED",
                   "prior Futures run has no verdict; reconcile its client ids first", 3)


def config():
    if not re.fullmatch(r"[A-Z0-9]{5,20}", SYMBOL): raise PreflightError("SYMBOL must be 5-20 uppercase letters/digits")
    if not re.fullmatch(r"[0-9a-f]{40}", QUANT_SHA): raise PreflightError("QUANT_SHA must be a full 40-character git SHA")
    max_notional, slippage_bps = d(MAX_NOTIONAL_RAW), d(MAX_SLIPPAGE_BPS_RAW)
    if max_notional <= 0: raise PreflightError("MAX_NOTIONAL must be positive and finite")
    if max_notional > LANE_HARD_CEILING:
        raise PreflightError(
            "MAX_NOTIONAL exceeds the compiled %s USDT lane ceiling"
            % fmt(LANE_HARD_CEILING))
    if not Decimal(0) <= slippage_bps <= Decimal(100): raise PreflightError("MAX_SLIPPAGE_BPS must be between 0 and 100")
    return max_notional, slippage_bps


def _new_transport(): return bh.BinanceHttp(API_KEY, API_SECRET)


def main():
    global transport
    args = sys.argv[1:]
    hand = "long"
    if args[:1] == ["--short"]:
        hand, args = "short", args[1:]
    if args in ([], ["--validate-only"]):
        execute = False
    elif args == ["--execute"]:
        execute = True
    else:
        print("usage: futures_hand.py [--short] [--validate-only | --execute]"); raise SystemExit(2)
    try:
        max_notional, slippage_bps = config()
    except PreflightError as error:
        print("VERDICT: BLOCKED"); print("NOTE:", clean(error, 300)); raise SystemExit(3)

    receipt.clear(); ctx.clear(); ctx.update({"lock": False, "mutated": False})
    sides = HANDS[hand]
    run_id = format(int(time.time()), "x") + secrets.token_hex(3)
    open_id = sides["prefix"] + run_id + "-o"
    close_id = sides["prefix"] + run_id + "-c"
    leverage_id = sides["prefix"] + run_id + "-l"
    receipt["hand"] = hand
    if execute:
        try:
            acquire_run(run_id, open_id, close_id, leverage_id, max_notional)
        except CapabilityBlocked as error:
            print("VERDICT: BLOCKED"); print("NOTE:", clean(error, 300)); raise SystemExit(3)
    try:
        if not API_KEY or not API_SECRET:
            raise CapabilityBlocked("host session has no Binance credential environment")
        transport = _new_transport()
        plan = preflight(max_notional, slippage_bps, sides["open_side"])
    except (bh.AdapterRefusal, CapabilityBlocked) as error:
        if execute: refuse(str(error))
        print("lane: L2 FUTURES-HAND"); print("hand:", hand); print("symbol:", SYMBOL)
        print("hard_ceiling_usdt:", fmt(LANE_HARD_CEILING)); print("VERDICT: BLOCKED"); print("NOTE:", clean(error, 300))
        raise SystemExit(3)
    except PreflightError as error:
        if execute: refuse(str(error))
        print("lane: L2 FUTURES-HAND"); print("hand:", hand); print("symbol:", SYMBOL)
        print("hard_ceiling_usdt:", fmt(LANE_HARD_CEILING)); print("VERDICT: BLOCKED"); print("NOTE:", clean(error, 300))
        raise SystemExit(3)

    if not execute:
        print("lane: L2 FUTURES-HAND"); print("hand:", hand); print("symbol:", SYMBOL); print("open_side:", sides["open_side"])
        print("planned_quantity:", fmt(plan["quantity"])); print("limit_price:", fmt(plan["limit_price"])); print("max_fill_notional:", fmt(plan["max_fill_notional"])); print("hard_ceiling_usdt:", fmt(LANE_HARD_CEILING))
        print("position_mode: ONE_WAY"); print("open_orders: 0"); print("position_amount: 0"); print("account_can_trade: true")
        print("current_leverage:", plan["current_leverage"])
        print("target_leverage:", TARGET_LEVERAGE)
        print("leverage_change_required:",
              str(plan["leverage_change_required"]).lower())
        print("key_trade_permission: UNVERIFIED_READ_ONLY_ONLY"); print("VERDICT: READ_ONLY_VALIDATED"); print("NOTE: signed USER_DATA reads passed; no order endpoint was called")
        raise SystemExit(0)

    receipt["starting_state"] = {
        key: fmt(value) if isinstance(value, Decimal) else value
        for key, value in plan.items()
    }
    save()
    enforce_target_leverage(plan, leverage_id)
    market = ctx["market"]
    try:
        open_plan = bf.build_open_order(
            market, sides["open_side"], open_id, QUANT_SHA, plan["quantity"],
            profile="LIMIT_IOC", limit_price=plan["limit_price"])
    except bh.AdapterRefusal as error:
        first_fail("open_plan", error)
        refuse("the typed open plan was refused")
    if open_plan.residual != 0:
        first_fail("open_plan", "open quantity left an unproved residual")
        refuse("the typed open plan is not exactly step aligned")
    receipt["open"] = {
        "submitted_utc": utc(),
        "planned_params": [list(pair) for pair in open_plan.intent.params],
    }
    save()
    opened = _submit_order(
        "OPEN", open_plan, "up to %s USDT" % fmt(plan["max_fill_notional"]))
    open_fills_complete = _record_order("open", opened)
    opened_qty, open_quote = opened.executed_quantity, opened.quote_quantity
    if not isinstance(opened_qty, Decimal) or not isinstance(open_quote, Decimal):
        first_fail("open_order", "terminal order money is unproved")
        wrap_up("UNKNOWN", "the open terminal state has unproved money fields", 4)
    if opened_qty <= 0:
        finish("OPEN_NOT_FILLED",
               "IOC open reached %s with zero execution; no position created"
               % opened.status, 2, release_lock=True)
    notional_breach = open_quote > max_notional
    if opened_qty > plan["quantity"] or notional_breach:
        first_fail("hard_ceiling", "exchange-reported open exceeded the planned cap")

    try:
        close_plan = bf.build_reduce_only_close(
            market, sides["close_side"], close_id, QUANT_SHA, opened_qty)
    except bh.AdapterRefusal as error:
        first_fail("close_plan", error)
        wrap_up("OPEN_EXPOSURE", "the exact reduce-only close plan was refused", 3)
    if close_plan.residual != 0 or not close_plan.reduce_only:
        first_fail("close_plan", "close is not exact and reduce-only")
        wrap_up("OPEN_EXPOSURE", "the exact reduce-only close plan is unproved", 3)
    receipt["close"] = {
        "submitted_utc": utc(),
        "planned_params": [list(pair) for pair in close_plan.intent.params],
    }
    save()
    closed = _submit_order("CLOSE", close_plan, "%s %s" % (fmt(opened_qty), SYMBOL))
    close_fills_complete = _record_order("close", closed)
    closed_qty = closed.executed_quantity
    if not isinstance(closed_qty, Decimal):
        first_fail("close_order", "terminal close quantity is unproved")
        wrap_up("UNKNOWN", "the close terminal quantity is unproved", 4)
    if closed.reduce_only is not True:
        first_fail("close_order", "query did not confirm reduceOnly=true")
        wrap_up("UNKNOWN", "close order reduceOnly flag was not confirmed", 4)
    if closed_qty != opened_qty:
        first_fail("close_quantity", "%s closed of %s opened" % (
            fmt(closed_qty), fmt(opened_qty)))
        wrap_up("OPEN_EXPOSURE",
                "reduce-only close did not execute the full open quantity", 3)
    if not open_fills_complete or not close_fills_complete:
        wrap_up("UNKNOWN", "orders are flat but exact fill and fee coverage is unproved", 4)
    if notional_breach:
        wrap_up(
            "CEILING_BREACH_CONTAINED",
            "%s open filled %s USDT, above the %s USDT cap; the full filled "
            "quantity was flattened by the ordered reduce-only close" % (
                hand, fmt(open_quote), fmt(max_notional)),
            3,
        )
    if "first_failure" in receipt:
        wrap_up("UNKNOWN", "planned pair ended but a safety invariant failed", 4)
    wrap_up(
        "CLOSED",
        "capped IOC %s open and same-quantity reduce-only market close are terminal"
        % hand,
        0,
    )


if __name__ == "__main__":
    main()
