#!/usr/bin/env python3
"""One Binance USD-M Futures long open and reduce-only close, then exit.

Default/no-argument mode is read-only validation. Only ``--execute`` can
change leverage or reach ``POST /fapi/v1/order``. Before an order, the executor
reads the symbol configuration and sets initial leverage to the compiled 1x
target once when needed, then re-reads it. The opening order is BUY LIMIT IOC
so its price and quantity can be bounded by the compiled 10 USDT lane ceiling;
the closing order is SELL MARKET with ``reduceOnly=true`` for exactly the
opened quantity.

Required environment (values are read but never printed): BINANCE_API_KEY,
BINANCE_API_SECRET, SYMBOL, MAX_NOTIONAL, and QUANT_SHA. Optional:
MAX_SLIPPAGE_BPS (default 10) and RECEIPT_DIR (default ~/quant_receipts).

This file deliberately has one mutating HTTP call site and no mutation retry.
After every submission attempt, order truth is queried by the exact same
client order id. An unclear result stays UNKNOWN; no repair order is sent.
"""

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext

getcontext().prec = 28

BASE_URL = "https://fapi.binance.com"
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
SYMBOL = os.environ.get("SYMBOL", "").upper()
MAX_NOTIONAL_RAW = os.environ.get("MAX_NOTIONAL", "")
MAX_SLIPPAGE_BPS_RAW = os.environ.get("MAX_SLIPPAGE_BPS", "10")
QUANT_SHA = os.environ.get("QUANT_SHA", "").lower()
RECEIPT_DIR = os.path.expanduser(
    os.environ.get("RECEIPT_DIR", "~/quant_receipts")
)
LOCK_PATH = os.path.join(RECEIPT_DIR, "futures-hand.lock")

LANE_PREFIX = "l2fh-"
LANE_HARD_CEILING = Decimal("10")  # USDT; never configurable upward
TARGET_LEVERAGE = 1
MAX_ALLOWED_LEVERAGE = 2  # Owner law: anything above 2x needs a new order.
CLOSE_PRICE_FLOOR = Decimal("0.99")  # require 1% room above close MIN_NOTIONAL
RECV_WINDOW = "5000"
HTTP_TIMEOUT = 15
POLL_SECONDS = 90
POLL_SLEEP = 1.0
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}

_time_offset_ms = 0
receipt = {}
receipt_path = ""
ctx = {"lock": False, "mutated": False}


class PreflightError(Exception):
    """A definite local or exchange state prevents starting."""


class CapabilityBlocked(PreflightError):
    """Current credentials/account access cannot prove the needed capability."""


def d(value):
    return Decimal(str(value))


def fmt(value):
    return format(value, "f")


def floor_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def common_step(a, b):
    """Smallest decimal quantity aligned to both exchange step sizes."""
    if a <= 0 or b <= 0:
        raise ValueError("quantity step must be positive")
    exponent = min(a.as_tuple().exponent, b.as_tuple().exponent)
    unit = Decimal(1).scaleb(exponent)
    ai, bi = int(a / unit), int(b / unit)
    return d(abs(ai * bi) // math.gcd(ai, bi)) * unit


def now_ms():
    return int(time.time() * 1000) + _time_offset_ms


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def clean(value, limit=160):
    return " ".join(str(value).split())[:limit]


def api_reason(payload):
    if not isinstance(payload, dict):
        return "non-object API response"
    return "%s %s" % (payload.get("code", "?"), clean(payload.get("msg", "")))


def save():
    if not receipt_path:
        return
    tmp = receipt_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2, default=str)
    os.chmod(tmp, 0o600)
    os.replace(tmp, receipt_path)


def first_fail(where, detail):
    if "first_failure" not in receipt:
        receipt["first_failure"] = {
            "utc": utc(),
            "where": clean(where, 60),
            "detail": clean(detail, 200),
        }
        save()


def http(method, path, params=None, signed=False):
    """Make one HTTP attempt; mutating calls must never be wrapped in retry."""
    params = dict(params or {})
    if signed:
        params["timestamp"] = str(now_ms())
        params["recvWindow"] = RECV_WINDOW
        query = urllib.parse.urlencode(params)
        signature = hmac.new(
            API_SECRET.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        query += "&signature=" + signature
    else:
        query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        BASE_URL + path + ("?" + query if query else ""), method=method
    )
    if API_KEY:
        request.add_header("X-MBX-APIKEY", API_KEY)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return "ok", json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        if error.code >= 500 or error.code in (408, 409):
            return "unclear", {"http": error.code}
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and "code" in parsed:
                return "api_error", {
                    "code": parsed.get("code"),
                    "msg": clean(parsed.get("msg", "")),
                }
        except ValueError:
            pass
        return "unclear", {"http": error.code}
    except Exception as error:  # never stringify an exception containing the URL
        return "unclear", {"error": type(error).__name__}


def get_retry(path, params=None, signed=False, tries=3):
    """GET requests may retry; this helper cannot send a mutation."""
    kind, payload = "unclear", {}
    for attempt in range(tries):
        kind, payload = http("GET", path, params, signed)
        if kind != "unclear":
            return kind, payload
        if attempt + 1 < tries:
            time.sleep(1 + attempt)
    return kind, payload


def finish(verdict, note, code, release_lock=False):
    receipt["verdict"] = verdict
    receipt["verdict_note"] = clean(note, 300)
    receipt["finished_utc"] = utc()
    save()
    print("lane: L2 FUTURES-HAND")
    print("run_id:", receipt.get("run_id", ""))
    print("sha8:", receipt.get("sha", "")[:8])
    print("symbol:", receipt.get("symbol", ""))
    print("hard_ceiling_usdt:", receipt.get("hard_ceiling_usdt", ""))
    print("open_client_id_last4:", receipt.get("open_client_id", "")[-4:])
    print("close_client_id_last4:", receipt.get("close_client_id", "")[-4:])
    if "first_failure" in receipt:
        print("first_failure:", json.dumps(receipt["first_failure"]))
    print("VERDICT:", verdict)
    print("NOTE:", clean(note, 300))
    print("RECEIPT:", receipt_path)
    if release_lock and ctx["lock"]:
        os.unlink(LOCK_PATH)
        ctx["lock"] = False
    raise SystemExit(code)


def refuse(note):
    finish("REJECTED_BEFORE_OPEN", note, 2, release_lock=True)


def query_order(client_id):
    """Query one exact client id until terminal, definitely absent, or unknown."""
    deadline = time.monotonic() + POLL_SECONDS
    last, missing = None, 0
    while time.monotonic() < deadline:
        kind, order = get_retry(
            "/fapi/v1/order",
            {"symbol": SYMBOL, "origClientOrderId": client_id},
            signed=True,
            tries=1,
        )
        if kind == "ok":
            last, missing = order, 0
            if order.get("status") in TERMINAL:
                return "terminal", order
        elif kind == "api_error":
            if order.get("code") == -2013:
                missing += 1
                if missing >= 8:
                    return "not_found", None
            else:
                first_fail("query_order", api_reason(order))
                return "error", order
        time.sleep(POLL_SLEEP)
    return "pending", last


def position_amount(rows):
    if not isinstance(rows, list):
        raise PreflightError("position response is not a list")
    matches = [
        row for row in rows
        if isinstance(row, dict) and row.get("symbol") == SYMBOL
    ]
    if len(matches) != 1:
        raise PreflightError(
            "position response did not return exactly one contract"
        )
    row = matches[0]
    if row.get("positionSide", "BOTH") != "BOTH":
        raise PreflightError("one-way position response contained a hedge side")
    return d(row.get("positionAmt", "0"))


def plan_open(symbol_info, book, mark_payload, max_notional, slippage_bps):
    """Build a BUY LIMIT IOC whose worst fill notional is at the hard cap."""
    if symbol_info.get("status") != "TRADING":
        raise PreflightError("symbol is not TRADING")
    if symbol_info.get("contractType") != "PERPETUAL":
        raise PreflightError("symbol is not a PERPETUAL contract")
    if symbol_info.get("quoteAsset") != "USDT" or \
            symbol_info.get("marginAsset") != "USDT":
        raise PreflightError("lane accepts only USDT-quoted, USDT-margined symbols")
    if not {"LIMIT", "MARKET"}.issubset(set(symbol_info.get("orderTypes", []))):
        raise PreflightError("symbol does not support LIMIT open and MARKET close")
    if "IOC" not in symbol_info.get("timeInForce", []):
        raise PreflightError("symbol does not support IOC")

    filters = {item["filterType"]: item for item in symbol_info.get("filters", [])}
    try:
        price_filter = filters["PRICE_FILTER"]
        lot = filters["LOT_SIZE"]
        market_lot = filters["MARKET_LOT_SIZE"]
        tick = d(price_filter["tickSize"])
        step = common_step(d(lot["stepSize"]), d(market_lot["stepSize"]))
        min_qty = max(d(lot["minQty"]), d(market_lot["minQty"]))
        max_qty = min(d(lot["maxQty"]), d(market_lot["maxQty"]))
        ask = d(book["askPrice"])
        mark = d(mark_payload["markPrice"])
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        raise PreflightError("symbol filters or market prices are unusable") from error
    if tick <= 0 or ask <= 0 or mark <= 0 or min_qty <= 0 or max_qty < min_qty:
        raise PreflightError("symbol filters or market prices are non-positive")

    limit_price = ceil_step(
        ask * (Decimal(1) + slippage_bps / Decimal(10000)), tick
    )
    min_price = d(price_filter.get("minPrice", "0"))
    max_price = d(price_filter.get("maxPrice", "0"))
    if limit_price < min_price or (max_price > 0 and limit_price > max_price):
        raise PreflightError("capped IOC price is outside PRICE_FILTER")
    percent = filters.get("PERCENT_PRICE", {})
    multiplier_up = d(percent.get("multiplierUp", "0"))
    if multiplier_up > 0 and limit_price > mark * multiplier_up:
        raise PreflightError("capped IOC price is outside PERCENT_PRICE")

    quantity = floor_step(min(max_notional / limit_price, max_qty), step)
    notional = quantity * limit_price
    min_notional = d(filters.get("MIN_NOTIONAL", {}).get("notional", "0"))
    if quantity < min_qty:
        raise PreflightError("hard ceiling is below the common minimum quantity")
    if min_notional > 0 and notional < min_notional:
        raise PreflightError("hard ceiling is below MIN_NOTIONAL")
    close_notional_floor = quantity * mark * CLOSE_PRICE_FLOOR
    if min_notional > 0 and close_notional_floor < min_notional:
        raise PreflightError("hard ceiling leaves no 1% MIN_NOTIONAL close cushion")
    if notional > max_notional or max_notional > LANE_HARD_CEILING:
        raise PreflightError("planned order exceeds the lane hard ceiling")
    if quantity % d(lot["stepSize"]) or quantity % d(market_lot["stepSize"]):
        raise PreflightError("planned quantity is not valid for both order legs")
    return {
        "quantity": quantity,
        "limit_price": limit_price,
        "max_fill_notional": notional,
        "min_notional": min_notional,
        "close_notional_floor": close_notional_floor,
        "step": step,
        "ask": ask,
        "mark": mark,
    }


def signed_read(path, params, label):
    kind, payload = get_retry(path, params, signed=True)
    if kind != "ok":
        reason = api_reason(payload) if kind == "api_error" else kind
        raise CapabilityBlocked("%s unavailable: %s" % (label, reason))
    return payload


def parse_symbol_configuration(payload):
    if not isinstance(payload, list):
        raise PreflightError("symbol configuration response is not a list")
    if any(not isinstance(row, dict) for row in payload):
        raise PreflightError("symbol configuration contains a non-object row")
    matches = [row for row in payload if row.get("symbol") == SYMBOL]
    if len(matches) != 1:
        raise PreflightError(
            "symbol configuration did not return exactly one contract"
        )
    try:
        leverage = int(str(matches[0]["leverage"]))
    except (KeyError, TypeError, ValueError) as error:
        raise PreflightError("symbol leverage is missing or invalid") from error
    if leverage < 1:
        raise PreflightError("symbol leverage must be positive")
    return matches[0], leverage


def read_symbol_configuration():
    payload = signed_read(
        "/fapi/v1/symbolConfig", {"symbol": SYMBOL},
        "symbol configuration read",
    )
    return parse_symbol_configuration(payload)


def preflight(max_notional, slippage_bps):
    global _time_offset_ms
    kind, server = get_retry("/fapi/v1/time")
    if kind != "ok":
        raise PreflightError("public Futures server-time read failed")
    _time_offset_ms = server["serverTime"] - int(time.time() * 1000)
    print("public_futures_time: ok")
    kind, exchange = get_retry("/fapi/v1/exchangeInfo")
    if kind != "ok":
        raise PreflightError("Futures exchangeInfo read failed")
    matches = [item for item in exchange.get("symbols", [])
               if item.get("symbol") == SYMBOL]
    if len(matches) != 1:
        raise PreflightError("symbol lookup did not return exactly one contract")
    kind, book = get_retry("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
    if kind != "ok":
        raise PreflightError("Futures book ticker read failed")
    kind, mark = get_retry("/fapi/v1/premiumIndex", {"symbol": SYMBOL})
    if kind != "ok":
        raise PreflightError("Futures mark-price read failed")
    plan = plan_open(matches[0], book, mark, max_notional, slippage_bps)
    print("public_symbol_plan: ok")
    print("public_max_fill_notional:", fmt(plan["max_fill_notional"]))
    print("public_plan_under_hard_ceiling: true")

    if not API_KEY or not API_SECRET:
        raise CapabilityBlocked("host session has no Binance credential environment")
    account = signed_read("/fapi/v3/account", None, "Futures account read")
    if not account.get("canTrade", False):
        raise CapabilityBlocked("Futures account canTrade is false")

    mode = signed_read("/fapi/v1/positionSide/dual", None, "position-mode read")
    if mode.get("dualSidePosition") is True or \
            str(mode.get("dualSidePosition", "")).lower() == "true":
        raise PreflightError("account is in Hedge Mode; reduceOnly is unavailable")
    orders = signed_read(
        "/fapi/v1/openOrders", {"symbol": SYMBOL}, "open-orders read"
    )
    if not isinstance(orders, list):
        raise PreflightError("open-orders response is not a list")
    if orders:
        raise PreflightError("existing open order on symbol makes attribution unsafe")
    risks = signed_read(
        "/fapi/v3/positionRisk", {"symbol": SYMBOL}, "position read"
    )
    risk_amount = position_amount(risks)
    account_amount = position_amount(account.get("positions", []))
    if risk_amount != 0 or account_amount != 0:
        raise PreflightError("existing position on symbol makes attribution unsafe")
    symbol_configuration, current_leverage = read_symbol_configuration()
    available = d(account.get("availableBalance", "0"))
    if available < plan["max_fill_notional"]:
        raise PreflightError("available Futures balance is below the capped notional")
    plan.update({
        "available_balance_sufficient": True,
        "account_can_trade": True,
        "position_mode": "ONE_WAY",
        "open_orders": 0,
        "position_amount": Decimal(0),
        "current_leverage": current_leverage,
        "target_leverage": TARGET_LEVERAGE,
        "leverage_change_required": current_leverage != TARGET_LEVERAGE,
        "margin_type": symbol_configuration.get("marginType", "UNKNOWN"),
    })
    return plan


def final_state():
    kind, risks = get_retry(
        "/fapi/v3/positionRisk", {"symbol": SYMBOL}, signed=True
    )
    if kind != "ok":
        first_fail("final_position_read", api_reason(risks) if kind == "api_error" else kind)
        return None
    kind, orders = get_retry(
        "/fapi/v1/openOrders", {"symbol": SYMBOL}, signed=True
    )
    if kind != "ok" or not isinstance(orders, list):
        first_fail("final_open_orders_read", api_reason(orders) if kind == "api_error" else kind)
        return None
    try:
        amount = position_amount(risks)
        _, leverage = read_symbol_configuration()
    except PreflightError as error:
        first_fail("final_position_parse", error)
        return None
    except CapabilityBlocked as error:
        first_fail("final_leverage_read", error)
        return None
    return {
        "position_amount": amount,
        "open_orders": len(orders),
        "leverage": leverage,
    }


def wrap_up(verdict, note, code):
    state = final_state()
    receipt["final_state"] = state or {
        "position_amount": "UNKNOWN", "open_orders": "UNKNOWN"
    }
    if verdict == "CLOSED":
        if state is None:
            verdict, code = "UNKNOWN", 4
            note = "orders terminal but final position/open-orders state is unreadable"
        elif state["position_amount"] != 0 or state["open_orders"] != 0:
            verdict, code = "OPEN_EXPOSURE", 3
            note = "planned pair ended with position %s and %s open order(s); no repair" % (
                fmt(state["position_amount"]), state["open_orders"]
            )
        elif state["leverage"] != TARGET_LEVERAGE:
            verdict, code = "UNKNOWN", 4
            note = "orders are flat but final leverage is not the compiled 1x target"
    finish(verdict, note, code, release_lock=(verdict == "CLOSED"))


def enforce_target_leverage(plan):
    """Set 1x at most once, then prove the factual symbol configuration."""
    before = plan["current_leverage"]
    receipt["leverage"] = {
        "before": before,
        "target": TARGET_LEVERAGE,
        "mutation_attempts": 0,
    }
    save()
    if TARGET_LEVERAGE > MAX_ALLOWED_LEVERAGE:
        first_fail("leverage", "compiled target exceeds owner hard cap")
        refuse("compiled leverage target exceeds the owner hard cap")
    if before == TARGET_LEVERAGE:
        receipt["leverage"].update({
            "after": before,
            "verified_utc": utc(),
            "note": "already at compiled target; no leverage mutation sent",
        })
        save()
        return

    receipt["leverage"]["submitted_utc"] = utc()
    receipt["leverage"]["mutation_attempts"] = 1
    save()
    kind, response = http(
        "POST", "/fapi/v1/leverage",
        {"symbol": SYMBOL, "leverage": str(TARGET_LEVERAGE)},
        signed=True,
    )
    ctx["mutated"] = True
    receipt["leverage"]["submission_kind"] = kind
    if kind == "ok" and isinstance(response, dict):
        receipt["leverage"]["reported_leverage"] = response.get("leverage")
    elif kind == "api_error":
        first_fail("leverage_submit", api_reason(response))
        refuse("1x leverage request was rejected: %s" % api_reason(response))
    else:
        first_fail("leverage_submit", "submission result was unclear")
    save()

    try:
        _, after = read_symbol_configuration()
    except (CapabilityBlocked, PreflightError) as error:
        first_fail("leverage_verify", error)
        finish(
            "UNKNOWN",
            "1x leverage result is unreadable; no order was sent and nothing retried",
            4,
        )
    receipt["leverage"].update({"after": after, "verified_utc": utc()})
    save()
    if after != TARGET_LEVERAGE:
        first_fail("leverage_verify", "symbol remains at %sx" % after)
        finish(
            "REJECTED_BEFORE_OPEN",
            "symbol leverage is %sx, not the compiled 1x target; "
            "no order sent" % after,
            3,
            release_lock=True,
        )


def submit_once(stage, params, exposure):
    """Submit exactly once, then query that same id; never blindly resend."""
    kind, response = http("POST", "/fapi/v1/order", params, signed=True)
    ctx["mutated"] = True
    state, order = query_order(params["newClientOrderId"])
    if state == "terminal":
        if order.get("clientOrderId") != params["newClientOrderId"]:
            first_fail("query_order", "returned client id did not match")
            wrap_up("UNKNOWN", "%s order identity mismatch; no retry" % stage, 4)
        return order
    if state == "not_found" and kind == "api_error":
        message = "%s rejected by Binance: %s" % (stage, api_reason(response))
        if stage == "OPEN":
            refuse(message)
        wrap_up("OPEN_EXPOSURE", message + "; exposure " + exposure, 3)
    descriptions = {
        "not_found": "submission unclear and client id not visible",
        "error": "client-id query returned a non-order error",
        "pending": "client-id query did not reach terminal state",
    }
    wrap_up(
        "UNKNOWN",
        "%s %s; possible exposure %s; same id queried, nothing resent" % (
            stage, descriptions[state], exposure
        ),
        4,
    )


def acquire_run(run_id, open_id, close_id, max_notional):
    global receipt_path
    os.makedirs(RECEIPT_DIR, mode=0o700, exist_ok=True)
    try:
        descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, run_id.encode())
        os.close(descriptor)
        ctx["lock"] = True
    except FileExistsError as error:
        raise CapabilityBlocked(
            "futures-hand lock exists; another or unresolved run owns the lane"
        ) from error
    receipt_path = os.path.join(RECEIPT_DIR, "futures-hand-" + run_id + ".json")
    receipt.update({
        "run_id": run_id,
        "open_client_id": open_id,
        "close_client_id": close_id,
        "sha": QUANT_SHA,
        "symbol": SYMBOL,
        "requested_max_notional": fmt(max_notional),
        "hard_ceiling_usdt": fmt(LANE_HARD_CEILING),
        "started_utc": utc(),
    })
    save()
    for name in os.listdir(RECEIPT_DIR):
        if not name.startswith("futures-hand-") or not name.endswith(".json"):
            continue
        if name == os.path.basename(receipt_path):
            continue
        try:
            with open(os.path.join(RECEIPT_DIR, name), encoding="utf-8") as handle:
                prior = json.load(handle)
        except Exception as error:
            first_fail("prior_receipt", type(error).__name__)
            finish("BLOCKED_UNRESOLVED", "prior Futures receipt is unreadable", 3)
        if "verdict" not in prior:
            finish(
                "BLOCKED_UNRESOLVED",
                "prior Futures run has no verdict; reconcile its client ids first",
                3,
            )


def config():
    if not re.fullmatch(r"[A-Z0-9]{5,20}", SYMBOL):
        raise PreflightError("SYMBOL must be 5-20 uppercase letters/digits")
    if not re.fullmatch(r"[0-9a-f]{40}", QUANT_SHA):
        raise PreflightError("QUANT_SHA must be a full 40-character git SHA")
    try:
        max_notional = d(MAX_NOTIONAL_RAW)
        slippage_bps = d(MAX_SLIPPAGE_BPS_RAW)
    except ArithmeticError as error:
        raise PreflightError("MAX_NOTIONAL / MAX_SLIPPAGE_BPS is invalid") from error
    if not max_notional.is_finite() or max_notional <= 0:
        raise PreflightError("MAX_NOTIONAL must be positive and finite")
    if max_notional > LANE_HARD_CEILING:
        raise PreflightError("MAX_NOTIONAL exceeds the compiled 10 USDT lane ceiling")
    if not slippage_bps.is_finite() or not (0 <= slippage_bps <= 100):
        raise PreflightError("MAX_SLIPPAGE_BPS must be between 0 and 100")
    return max_notional, slippage_bps


def main():
    if sys.argv[1:] in ([], ["--validate-only"]):
        execute = False
    elif sys.argv[1:] == ["--execute"]:
        execute = True
    else:
        print("usage: futures_hand.py [--validate-only | --execute]")
        raise SystemExit(2)
    try:
        max_notional, slippage_bps = config()
    except PreflightError as error:
        print("VERDICT: BLOCKED")
        print("NOTE:", clean(error, 300))
        raise SystemExit(3)

    run_id = format(int(time.time()), "x") + secrets.token_hex(3)
    open_id = LANE_PREFIX + run_id + "-o"
    close_id = LANE_PREFIX + run_id + "-c"
    if execute:
        try:
            acquire_run(run_id, open_id, close_id, max_notional)
        except CapabilityBlocked as error:
            print("VERDICT: BLOCKED")
            print("NOTE:", clean(error, 300))
            raise SystemExit(3)
    try:
        plan = preflight(max_notional, slippage_bps)
    except CapabilityBlocked as error:
        if execute:
            refuse(str(error))
        print("lane: L2 FUTURES-HAND")
        print("symbol:", SYMBOL)
        print("hard_ceiling_usdt:", fmt(LANE_HARD_CEILING))
        print("VERDICT: BLOCKED")
        print("NOTE:", clean(error, 300))
        raise SystemExit(3)
    except PreflightError as error:
        if execute:
            refuse(str(error))
        print("lane: L2 FUTURES-HAND")
        print("symbol:", SYMBOL)
        print("hard_ceiling_usdt:", fmt(LANE_HARD_CEILING))
        print("VERDICT: BLOCKED")
        print("NOTE:", clean(error, 300))
        raise SystemExit(3)

    if not execute:
        print("lane: L2 FUTURES-HAND")
        print("symbol:", SYMBOL)
        print("planned_quantity:", fmt(plan["quantity"]))
        print("limit_price:", fmt(plan["limit_price"]))
        print("max_fill_notional:", fmt(plan["max_fill_notional"]))
        print("hard_ceiling_usdt:", fmt(LANE_HARD_CEILING))
        print("position_mode: ONE_WAY")
        print("open_orders: 0")
        print("position_amount: 0")
        print("account_can_trade: true")
        print("current_leverage:", plan["current_leverage"])
        print("target_leverage:", TARGET_LEVERAGE)
        print("leverage_change_required:", str(
            plan["leverage_change_required"]
        ).lower())
        print("key_trade_permission: UNVERIFIED_READ_ONLY_ONLY")
        print("VERDICT: READ_ONLY_VALIDATED")
        print("NOTE: signed USER_DATA reads passed; no order/test endpoint was called")
        raise SystemExit(0)

    receipt["starting_state"] = {
        key: fmt(value) if isinstance(value, Decimal) else value
        for key, value in plan.items()
    }
    save()
    enforce_target_leverage(plan)
    open_params = {
        "symbol": SYMBOL,
        "side": "BUY",
        "positionSide": "BOTH",
        "type": "LIMIT",
        "timeInForce": "IOC",
        "quantity": fmt(plan["quantity"]),
        "price": fmt(plan["limit_price"]),
        "reduceOnly": "false",
        "newClientOrderId": open_id,
        "newOrderRespType": "RESULT",
    }
    receipt["open"] = {"submitted_utc": utc(), "params": {
        key: value for key, value in open_params.items()
        if key != "newClientOrderId"
    }}
    save()
    opened = submit_once(
        "OPEN", open_params, "up to %s USDT" % fmt(plan["max_fill_notional"])
    )
    opened_qty = d(opened.get("executedQty", "0"))
    open_quote = d(opened.get("cumQuote", "0"))
    receipt["open"].update({
        "client_id_last4": open_id[-4:],
        "order_id_last4": str(opened.get("orderId", ""))[-4:],
        "status": opened.get("status"),
        "executed_qty": fmt(opened_qty),
        "cum_quote": fmt(open_quote),
    })
    save()
    if opened_qty <= 0:
        finish(
            "OPEN_NOT_FILLED",
            "IOC open reached %s with zero execution; no position created" %
            opened.get("status"),
            2,
            release_lock=True,
        )
    if opened_qty > plan["quantity"] or open_quote > max_notional:
        first_fail("hard_ceiling", "exchange-reported open exceeded the planned cap")

    close_params = {
        "symbol": SYMBOL,
        "side": "SELL",
        "positionSide": "BOTH",
        "type": "MARKET",
        "quantity": fmt(opened_qty),
        "reduceOnly": "true",
        "newClientOrderId": close_id,
        "newOrderRespType": "RESULT",
    }
    receipt["close"] = {"submitted_utc": utc(), "params": {
        key: value for key, value in close_params.items()
        if key != "newClientOrderId"
    }}
    save()
    closed = submit_once(
        "CLOSE", close_params, "%s %s" % (fmt(opened_qty), SYMBOL)
    )
    closed_qty = d(closed.get("executedQty", "0"))
    receipt["close"].update({
        "client_id_last4": close_id[-4:],
        "order_id_last4": str(closed.get("orderId", ""))[-4:],
        "status": closed.get("status"),
        "executed_qty": fmt(closed_qty),
        "cum_quote": fmt(d(closed.get("cumQuote", "0"))),
        "reduce_only": closed.get("reduceOnly"),
    })
    save()
    reduce_only = closed.get("reduceOnly") is True or \
        str(closed.get("reduceOnly", "")).lower() == "true"
    if not reduce_only:
        first_fail("close_order", "query did not confirm reduceOnly=true")
        wrap_up("UNKNOWN", "close order reduceOnly flag was not confirmed", 4)
    if closed_qty != opened_qty:
        first_fail("close_quantity", "%s closed of %s opened" % (
            fmt(closed_qty), fmt(opened_qty)
        ))
        wrap_up("OPEN_EXPOSURE", "reduce-only close did not execute the full open quantity", 3)
    if "first_failure" in receipt:
        wrap_up("UNKNOWN", "planned pair ended but a safety invariant failed", 4)
    wrap_up(
        "CLOSED",
        "capped IOC long open and same-quantity reduce-only market close are terminal",
        0,
    )


if __name__ == "__main__":
    main()
