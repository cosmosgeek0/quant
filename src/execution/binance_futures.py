"""Strict USD-M Futures adapter; frozen interface rows 051-067."""
from __future__ import annotations
import math as _math, re as _re, time as _time, typing as _t
from dataclasses import dataclass; from decimal import Decimal
from execution import binance_http as _h

__all__ = ["FuturesMarket", "FuturesPreflight", "LeverageResult", "Income", "read_market", "read_preflight", "read_leverage", "set_leverage_1x_once", "build_open_order", "build_reduce_only_close", "query_order", "submit_order_once", "read_user_trades", "read_fills", "read_income"]
_SYMBOL = _re.compile(r"[A-Z0-9]{5,20}"); _CLIENT = _re.compile(r"[.A-Z:/a-z0-9_-]{1,36}"); _IDENTITY = _re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SHA = _re.compile(r"[0-9a-f]{40}"); _ASSET = _re.compile(r"[A-Z0-9]{1,20}"); _WINDOW_MS = 7 * 24 * 60 * 60 * 1000

@dataclass(frozen=True)
class FuturesMarket:
    symbol: str; status: str; contract_type: str; step_size: Decimal; min_quantity: Decimal; min_notional: Decimal; multiplier_up: Decimal; bid: Decimal; ask: Decimal; mark_price: Decimal

@dataclass(frozen=True)
class FuturesPreflight:
    can_trade: bool; one_way: bool; position_quantity: Decimal; account_position_quantity: Decimal; open_order_ids: _t.Tuple[int, ...]; available_balance: Decimal; required_margin: Decimal | None; leverage: int

@dataclass(frozen=True)
class LeverageResult:
    kind: _t.Literal["proven", "api_error", "unclear"]; before: int | None; after: int | None; mutated: bool; error: _h.HttpResult | None

@dataclass(frozen=True)
class Income:
    transaction_id: int; symbol: str; income_type: str; amount: Decimal; asset: str; time_ms: int; trade_id: int | None

def _no(message): raise _h.AdapterRefusal(message)

def _dec(value, zero=False, negative=False):
    if not isinstance(value, (str, Decimal)) or isinstance(value, bool): _no("money is not exact")
    try:
        with _h.exact_guard(): result = +Decimal(value); invalid = not result.is_finite() or (not negative and result < 0) or (not zero and result == 0)
    except ValueError: _no("money is malformed")
    return result if not invalid else _no("money is outside its factual range")

def _int(value, zero=False):
    if type(value) is int: result = value
    elif type(value) is str and _re.fullmatch(r"[0-9]+", value): result = int(value)
    else: _no("integer identity is malformed")
    if result < (0 if zero else 1): _no("integer identity is outside its factual range")
    return result

def _name(value, pattern, label): return value if type(value) is str and pattern.fullmatch(value) else _no(label + " is invalid")
def _base(value): return value if type(value) is str and value else _no("base_url is invalid")
def _input(value, zero=False): return _dec(value, zero) if isinstance(value, Decimal) else _no("money input must be Decimal")

def _bad(label): return _h.ReadResult("unclear", None, _h.HttpResult("unclear", None, None, label))

def _get(transport, base_url, path, params, signed, sleep):
    _base(base_url); result = _h.read(transport, base_url, path, params, signed=signed, sleep=sleep); return (result.payload, None) if result.kind == "ok" else (None, _h.ReadResult(result.kind, None, result))

def _write(writer, intent):
    try: writer(intent)
    except _h.IntentNotDurable: raise
    except Exception: raise _h.IntentNotDurable("intent was not durably written") from None

def _step(first, second):
    with _h.exact_guard(): unit = Decimal(1).scaleb(min(first.as_tuple().exponent, second.as_tuple().exponent)); left, right = int(first // unit), int(second // unit); return Decimal(abs(left * right) // _math.gcd(left, right)) * unit

def _market(market):
    if not isinstance(market, FuturesMarket) or market.status != "TRADING" or market.contract_type != "PERPETUAL": _no("market is not a trading perpetual")
    for value in (market.step_size, market.min_quantity, market.min_notional, market.multiplier_up, market.bid, market.ask, market.mark_price): _input(value)
    if market.bid >= market.ask: _no("market book is crossed")

def read_market(transport: _h.Transport, base_url: str, symbol: str, *, sleep: _t.Callable[[float], None] = _time.sleep) -> _h.ReadResult[FuturesMarket]:
    _name(symbol, _SYMBOL, "symbol"); values = []
    specs = (("/fapi/v1/exchangeInfo", None), ("/fapi/v1/ticker/bookTicker", (("symbol", symbol),)), ("/fapi/v1/premiumIndex", (("symbol", symbol),)))
    for path, params in specs:
        value, error = _get(transport, base_url, path, params, False, sleep)
        if error: return error
        values.append(value)
    try:
        rows = values[0]["symbols"]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows): _no("symbols malformed")
        matches = [row for row in rows if row.get("symbol") == symbol];
        if len(matches) != 1: _no("symbol not unique")
        row = matches[0]
        if row.get("status") != "TRADING" or row.get("contractType") != "PERPETUAL": _no("contract unavailable")
        filters = row.get("filters")
        if not isinstance(filters, list) or any(not isinstance(item, dict) or type(item.get("filterType")) is not str for item in filters) or len(filters) != len({item["filterType"] for item in filters}): _no("filters malformed")
        found = {kind: [item for item in filters if item.get("filterType") == kind] for kind in ("LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL", "PERCENT_PRICE")}
        if any(len(items) != 1 for items in found.values()): _no("filter not unique")
        lot, mlot = found["LOT_SIZE"][0], found["MARKET_LOT_SIZE"][0]; step = _step(_dec(lot.get("stepSize")), _dec(mlot.get("stepSize"))); minimum = max(_dec(lot.get("minQty")), _dec(mlot.get("minQty")))
        notional = _dec(found["MIN_NOTIONAL"][0].get("notional")); multiplier = _dec(found["PERCENT_PRICE"][0].get("multiplierUp"))
        book, mark = values[1], values[2]
        if not isinstance(book, dict) or book.get("symbol") != symbol or not isinstance(mark, dict) or mark.get("symbol") != symbol: _no("market symbol mismatch")
        bid, ask, mark_price = _dec(book.get("bidPrice")), _dec(book.get("askPrice")), _dec(mark.get("markPrice"))
        if bid >= ask: _no("book crossed")
        result = FuturesMarket(symbol, "TRADING", "PERPETUAL", step, minimum, notional, multiplier, bid, ask, mark_price)
    except (_h.AdapterRefusal, KeyError, TypeError): return _bad("futures_market_schema")
    return _h.ReadResult("ok", result, None)

def _position(rows, symbol):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows): _no("positions malformed")
    matches = [row for row in rows if row.get("symbol") == symbol]
    if len(matches) != 1 or matches[0].get("positionSide") != "BOTH": _no("position not unique")
    return _dec(matches[0].get("positionAmt"), True, True)

def read_preflight(transport: _h.Transport, base_url: str, market: FuturesMarket, proposed_notional: Decimal | None, *, require_flat: bool, sleep: _t.Callable[[float], None] = _time.sleep) -> _h.ReadResult[FuturesPreflight]:
    _market(market)
    if type(require_flat) is not bool: _no("require_flat is invalid")
    with _h.exact_guard(): required = None if proposed_notional is None else _dec(proposed_notional) * market.multiplier_up
    specs = (("/fapi/v1/accountConfig", None), ("/fapi/v1/positionSide/dual", None), ("/fapi/v3/account", None), ("/fapi/v1/openOrders", (("symbol", market.symbol),)), ("/fapi/v3/positionRisk", (("symbol", market.symbol),)), ("/fapi/v3/balance", None))
    values = []
    for path, params in specs:
        value, error = _get(transport, base_url, path, params, True, sleep)
        if error: return error
        values.append(value)
    leverage = read_leverage(transport, base_url, market.symbol, sleep=sleep)
    if leverage.kind != "ok": return leverage
    try:
        config, mode, account, orders, risks, balances = values
        if not isinstance(config, dict) or config.get("canTrade") is not True: _no("trading permission absent")
        if not isinstance(mode, dict) or mode.get("dualSidePosition") is not False: _no("one-way mode absent")
        if not isinstance(account, dict): _no("account malformed")
        account_qty, risk_qty = _position(account.get("positions"), market.symbol), _position(risks, market.symbol)
        if account_qty != risk_qty: _no("positions disagree")
        if not isinstance(orders, list) or any(not isinstance(row, dict) for row in orders): _no("orders malformed")
        if any(row.get("symbol") != market.symbol for row in orders): _no("order symbol mismatch")
        order_ids = tuple(sorted(_int(row.get("orderId")) for row in orders))
        if len(set(order_ids)) != len(order_ids) or require_flat and (order_ids or risk_qty != 0): _no("flatness absent")
        if not isinstance(balances, list) or any(not isinstance(row, dict) for row in balances): _no("balances malformed")
        matches = [row for row in balances if type(row.get("asset")) is str and market.symbol.endswith(row["asset"])]
        if len(matches) != 1: _no("balance not unique")
        available = _dec(matches[0].get("availableBalance"), True)
        if required is not None and available < required: _no("margin insufficient")
        result = FuturesPreflight(True, True, risk_qty, account_qty, order_ids, available, required, leverage.value)
    except (_h.AdapterRefusal, KeyError, TypeError): return _bad("futures_preflight_schema")
    return _h.ReadResult("ok", result, None)

def read_leverage(transport: _h.Transport, base_url: str, symbol: str, *, sleep: _t.Callable[[float], None] = _time.sleep) -> _h.ReadResult[int]:
    _name(symbol, _SYMBOL, "symbol")
    value, error = _get(transport, base_url, "/fapi/v1/symbolConfig", (("symbol", symbol),), True, sleep)
    if error: return error
    try:
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value): _no("configuration malformed")
        matches = [row for row in value if row.get("symbol") == symbol]
        if len(matches) != 1: _no("leverage not unique")
        leverage = _int(matches[0].get("leverage"))
    except (_h.AdapterRefusal, TypeError): return _bad("futures_leverage_schema")
    return _h.ReadResult("ok", leverage, None)

def set_leverage_1x_once(transport: _h.Transport, base_url: str, symbol: str, mutation_id: str, git_sha: str, write_intent: _h.IntentWriter, *, sleep: _t.Callable[[float], None] = _time.sleep) -> LeverageResult:
    _name(symbol, _SYMBOL, "symbol"); _name(mutation_id, _IDENTITY, "mutation id"); _name(git_sha, _SHA, "git sha")
    before = read_leverage(transport, base_url, symbol, sleep=sleep)
    if before.kind != "ok": return LeverageResult(before.kind, None, None, False, before.error)
    if before.value == 1: return LeverageResult("proven", 1, 1, False, None)
    params = (("symbol", symbol), ("leverage", "1"))
    _write(write_intent, _h.MutationIntent("BINANCE_USDM", "SET_LEVERAGE", mutation_id, git_sha, params))
    posted = transport("POST", base_url, "/fapi/v1/leverage", params, True)
    after = read_leverage(transport, base_url, symbol, sleep=sleep)
    if after.kind == "ok" and after.value == 1: return LeverageResult("proven", before.value, 1, True, None)
    kind = "api_error" if posted.kind == "api_error" else "unclear"; error = posted if posted.kind != "ok" else after.error or _bad("leverage_not_1x").error
    return LeverageResult(kind, before.value, after.value if after.kind == "ok" else None, True, error)

def _plan(market, side, client_id, git_sha, quantity, reduce_only, profile, limit_price=None):
    with _h.exact_guard():
        _market(market)
        if side not in ("BUY", "SELL") or profile not in ("MARKET", "LIMIT_IOC"): _no("order shape invalid")
        original = _input(quantity); sized = original if reduce_only else original // market.step_size * market.step_size
        if sized <= 0 or sized < market.min_quantity or reduce_only and sized % market.step_size: _no("quantity filter failed")
        if profile == "LIMIT_IOC": price = _input(limit_price)
        elif limit_price is not None: _no("market order has a price")
        else: price = market.ask if side == "BUY" else market.bid
        if profile == "LIMIT_IOC" and price > market.mark_price * market.multiplier_up: _no("percent-price filter failed")
        if sized * price < market.min_notional: _no("notional filter failed")
        pairs = [("symbol", market.symbol), ("side", side), ("positionSide", "BOTH"), ("type", "LIMIT" if profile == "LIMIT_IOC" else "MARKET")] + ([("timeInForce", "IOC")] if profile == "LIMIT_IOC" else []) + [("quantity", format(sized, "f"))] + ([("price", format(price, "f"))] if profile == "LIMIT_IOC" else []) + [("reduceOnly", "true" if reduce_only else "false"), ("newClientOrderId", client_id), ("newOrderRespType", "RESULT")]
        intent = _h.MutationIntent("BINANCE_USDM", "ORDER", client_id, git_sha, tuple(pairs)); return _checked_plan(_h.OrderPlan(intent, market.symbol, side, sized, None, original - sized, reduce_only))

def build_open_order(market: FuturesMarket, side: _t.Literal["BUY", "SELL"], client_id: str, git_sha: str, quantity: Decimal, *, profile: _t.Literal["MARKET", "LIMIT_IOC"], limit_price: Decimal | None = None) -> _h.OrderPlan:
    return _plan(market, side, client_id, git_sha, quantity, False, profile, limit_price)

def build_reduce_only_close(market: FuturesMarket, side: _t.Literal["BUY", "SELL"], client_id: str, git_sha: str, quantity: Decimal) -> _h.OrderPlan:
    return _plan(market, side, client_id, git_sha, quantity, True, "MARKET")

def _checked_plan(plan):
    if not isinstance(plan, _h.OrderPlan) or not isinstance(plan.intent, _h.MutationIntent): _no("order plan invalid")
    intent, params = plan.intent, plan.intent.params
    if type(params) is not tuple or any(type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str or not pair[0] or type(pair[1]) is not str for pair in params): _no("order params invalid")
    wire = dict(params); _name(plan.symbol, _SYMBOL, "symbol"); _name(intent.identity, _CLIENT, "client id"); _name(intent.git_sha, _SHA, "git sha")
    if plan.side not in ("BUY", "SELL") or type(plan.reduce_only) is not bool or plan.quote_limit is not None: _no("order plan invalid")
    quantity, residual, kind = _input(plan.quantity), _input(plan.residual, True), wire.get("type"); keys = ("symbol", "side", "positionSide", "type") + (("timeInForce",) if kind == "LIMIT" else ()) + ("quantity",) + (("price",) if kind == "LIMIT" else ()) + ("reduceOnly", "newClientOrderId", "newOrderRespType")
    if len(wire) != len(params) or tuple(wire) != keys or intent.venue != "BINANCE_USDM" or intent.operation != "ORDER" or wire.get("symbol") != plan.symbol or wire.get("side") != plan.side or wire.get("positionSide") != "BOTH" or kind not in ("MARKET", "LIMIT") or plan.reduce_only and kind != "MARKET" or wire.get("reduceOnly") != ("true" if plan.reduce_only else "false") or wire.get("newClientOrderId") != intent.identity or wire.get("newOrderRespType") != "RESULT" or wire["quantity"] != format(quantity, "f") or kind == "LIMIT" and (wire["timeInForce"] != "IOC" or wire["price"] != format(_dec(wire["price"]), "f")) or plan.reduce_only and residual != 0: _no("order plan inconsistent")
    return plan

def _result(tag, symbol, client_id, order_id=None, returned=None, status=None, side=None, reduce=None, quantity=None, quote=None, error=None): return _h.OrderResult(tag, _h.OrderIdentity(symbol, client_id, order_id), returned, status, tag == "terminal", side, reduce, quantity, quote, (), error)

def _order(payload, symbol, client_id):
    if not isinstance(payload, dict) or payload.get("symbol") != symbol or payload.get("clientOrderId") != client_id: _no("order identity mismatch")
    status, side, reduce = payload.get("status"), payload.get("side"), payload.get("reduceOnly")
    if type(status) is not str or side not in ("BUY", "SELL") or type(reduce) is not bool: _no("order state malformed")
    tag = "terminal" if status in _h.TERMINAL_ORDER_STATUSES else "pending"
    return _result(tag, symbol, client_id, _int(payload.get("orderId")), client_id, status, side, reduce, _dec(payload.get("executedQty"), True), _dec(payload.get("cumQuote"), True))

def query_order(transport: _h.Transport, base_url: str, symbol: str, client_id: str, *, sleep: _t.Callable[[float], None] = _time.sleep) -> _h.OrderResult:
    _base(base_url); _name(symbol, _SYMBOL, "symbol"); _name(client_id, _CLIENT, "client id")
    params = (("symbol", symbol), ("origClientOrderId", client_id)); missing = 0; last = None
    for attempt in range(_h.ORDER_QUERY_ATTEMPTS):
        response = transport("GET", base_url, "/fapi/v1/order", params, True)
        if not isinstance(response, _h.HttpResult): response = _h.HttpResult("unclear", None, None, "order_query_result")
        if response.kind == "ok":
            missing = 0
            try: last = _order(response.payload, symbol, client_id)
            except (_h.AdapterRefusal, TypeError): last = None
            if last is not None and last.tag == "terminal": return last
        elif response.kind == "api_error" and response.code == -2013:
            missing += 1
            if missing >= _h.ORDER_NOT_FOUND_STREAK: return _result("not_found", symbol, client_id)
        elif response.kind == "api_error": return _result("error", symbol, client_id, error=response)
        else: missing = 0
        if attempt + 1 < _h.ORDER_QUERY_ATTEMPTS: sleep(_h.ORDER_QUERY_DELAY_SECONDS)
    return last or _result("pending", symbol, client_id)

def submit_order_once(transport: _h.Transport, base_url: str, plan: _h.OrderPlan, write_intent: _h.IntentWriter, *, sleep: _t.Callable[[float], None] = _time.sleep) -> _h.OrderResult:
    _base(base_url); plan = _checked_plan(plan); params = plan.intent.params
    _write(write_intent, plan.intent); posted = transport("POST", base_url, "/fapi/v1/order", params, True); conflict = None
    returned = posted.payload.get("clientOrderId") if posted.kind == "ok" and isinstance(posted.payload, dict) else None
    if posted.kind == "ok" and (returned != plan.intent.identity or plan.reduce_only and posted.payload.get("reduceOnly") is not True):
        conflict = _h.HttpResult("unclear", None, None, "order_response_conflict")
    queried = query_order(transport, base_url, plan.symbol, plan.intent.identity, sleep=sleep)
    if conflict is None and plan.reduce_only and queried.tag == "terminal" and queried.reduce_only is not True: conflict = _h.HttpResult("unclear", None, None, "order_query_conflict")
    if queried.tag == "terminal" and conflict is None: return queried
    if queried.tag == "not_found" and posted.kind == "api_error": return _result("error", plan.symbol, plan.intent.identity, error=posted)
    error = conflict or queried.error or (posted if posted.kind != "ok" else None)
    shown = returned if isinstance(returned, str) else queried.returned_client_id if conflict is None else None
    return _result("pending", plan.symbol, plan.intent.identity, queried.identity.order_id, shown, queried.status, queried.side, queried.reduce_only, queried.executed_quantity, queried.quote_quantity, error)

def _valid_fills(fills, expected=None):
    seen = set() if type(fills) is tuple else _no("fills must be immutable")
    for fill in fills:
        if not isinstance(fill, _h.Fill) or not isinstance(fill.fee, _h.Fee): _no("fill shape invalid")
        trade, order = _int(fill.trade_id, True), _int(fill.order_id)
        if trade in seen or expected is not None and order != expected: _no("trade identity mismatch")
        _input(fill.price); _input(fill.quantity); _input(fill.quote_quantity); _name(fill.fee.asset, _ASSET, "commission asset"); _input(fill.fee.amount, True); seen.add(trade)
    return fills

def _fills(payload, symbol, expected=None):
    result = [] if isinstance(payload, list) and all(isinstance(row, dict) for row in payload) else _no("trades malformed")
    for row in payload:
        if row.get("symbol") != symbol: _no("trade symbol mismatch")
        result.append(_h.Fill(_int(row.get("id"), True), _int(row.get("orderId")), _dec(row.get("price")), _dec(row.get("qty")), _dec(row.get("quoteQty")), _h.Fee(row.get("commissionAsset"), _dec(row.get("commission"), True))))
    return _valid_fills(tuple(result), expected)

def read_user_trades(transport: _h.Transport, base_url: str, symbol: str, *, order_id: int | None = None, start_ms: int | None = None, end_ms: int | None = None, sleep: _t.Callable[[float], None] = _time.sleep) -> _h.ReadResult[_t.Tuple[_h.Fill, ...]]:
    _name(symbol, _SYMBOL, "symbol")
    if order_id is not None:
        order_id = _int(order_id)
        if start_ms is not None or end_ms is not None: _no("trade query modes mixed")
        params = (("symbol", symbol), ("orderId", str(order_id)), ("limit", "1000"))
    else:
        if type(start_ms) is not int or type(end_ms) is not int or start_ms < 0 or end_ms < start_ms or end_ms - start_ms > _WINDOW_MS: _no("trade window invalid")
        params = (("symbol", symbol), ("startTime", str(start_ms)), ("endTime", str(end_ms)), ("limit", "1000"))
    value, error = _get(transport, base_url, "/fapi/v1/userTrades", params, True, sleep)
    if error: return error
    try: result = _fills(value, symbol, order_id)
    except (_h.AdapterRefusal, TypeError): return _bad("futures_trades_schema")
    if len(result) > 1000 or order_id is None and any(type(row.get("time")) is not int or not start_ms <= row["time"] <= end_ms for row in value): return _bad("futures_trades_schema")
    return _h.ReadResult("ok", result, None)

def _coverage(fills, executed):
    with _h.exact_guard():
        covered = sum((fill.quantity for fill in fills), Decimal(0)); totals = {}
        for fill in fills: totals[fill.fee.asset] = totals.get(fill.fee.asset, Decimal(0)) + fill.fee.amount
        fees = tuple(_h.Fee(asset, totals[asset]) for asset in sorted(totals)); return _h.FillCoverage(tuple(fills), covered, executed, covered == executed, fees)

def read_fills(transport: _h.Transport, base_url: str, order: _h.OrderResult, response_fills: _t.Tuple[_h.Fill, ...] = (), *, sleep: _t.Callable[[float], None] = _time.sleep) -> _h.ReadResult[_h.FillCoverage]:
    executed = _dec(order.executed_quantity, True) if isinstance(order, _h.OrderResult) and order.terminal and order.identity.order_id is not None and isinstance(order.executed_quantity, Decimal) else _no("order identity unproved")
    try: local = _valid_fills(order.response_fills + response_fills, order.identity.order_id)
    except (TypeError, _h.AdapterRefusal): return _bad("futures_response_fills_schema")
    result = _coverage(local, executed)
    if result.covered_quantity > executed: return _bad("futures_fill_coverage")
    if result.complete: return _h.ReadResult("ok", result, None)
    trades = read_user_trades(transport, base_url, order.identity.symbol, order_id=order.identity.order_id, sleep=sleep)
    if trades.kind != "ok": return trades
    result = _coverage(trades.value, executed)
    return _bad("futures_fill_coverage") if result.covered_quantity > executed else _h.ReadResult("ok", result, None)

def read_income(transport: _h.Transport, base_url: str, symbol: str, start_ms: int, end_ms: int, *, sleep: _t.Callable[[float], None] = _time.sleep) -> _h.ReadResult[_t.Tuple[Income, ...]]:
    _name(symbol, _SYMBOL, "symbol")
    if type(start_ms) is not int or type(end_ms) is not int or start_ms < 0 or end_ms < start_ms or end_ms - start_ms > _WINDOW_MS: _no("income window invalid")
    params = (("symbol", symbol), ("startTime", str(start_ms)), ("endTime", str(end_ms)), ("limit", "1000"))
    value, error = _get(transport, base_url, "/fapi/v1/income", params, True, sleep)
    if error: return error
    try:
        if not isinstance(value, list) or len(value) > 1000 or any(not isinstance(row, dict) for row in value): _no("income malformed")
        result = []; seen = set()
        for row in value:
            transaction = _int(row.get("tranId")); asset, kind = row.get("asset"), row.get("incomeType")
            if transaction in seen or row.get("symbol") != symbol or type(asset) is not str or _ASSET.fullmatch(asset) is None or type(kind) is not str or not kind or type(row.get("time")) is not int or not start_ms <= row["time"] <= end_ms: _no("income identity invalid")
            trade = row.get("tradeId"); trade = None if trade in (None, "") else _int(trade, True); seen.add(transaction)
            result.append(Income(transaction, symbol, kind, _dec(row.get("income"), True, True), asset, _int(row.get("time"), True), trade))
    except (_h.AdapterRefusal, TypeError): return _bad("futures_income_schema")
    return _h.ReadResult("ok", tuple(result), None)
