"""Fail-closed Binance Spot adapter, frozen interface rows 037-050."""
from __future__ import annotations

from dataclasses import dataclass as _dataclass, replace as _replace
from decimal import Decimal
import math as _math, re as _re, time as _time
from typing import Callable as _Callable, Literal as _Literal
from execution import binance_http as _http

__all__ = ("SPOT_MY_TRADES_PATH", "SPOT_ORDER_PATH", "SpotMarket", "SpotAccountState", "read_market", "read_account_state", "build_market_order", "query_order", "submit_order_once", "read_my_trades", "read_fills", "fees_by_asset", "net_base_quantity")
_INFO, _BOOK, _ACCOUNT = "/api/v3/exchangeInfo", "/api/v3/ticker/bookTicker", "/api/v3/account"
_OPEN, SPOT_ORDER_PATH, SPOT_MY_TRADES_PATH = "/api/v3/openOrders", "/api/v3/order", "/api/v3/myTrades"
_DAY_MS = 86_400_000
_SYMBOL, _ASSET = _re.compile(r"[A-Z0-9]{5,20}\Z"), _re.compile(r"[A-Z0-9]{1,20}\Z")
_CLIENT_ID = _re.compile(r"[A-Za-z0-9._:/-]{1,36}\Z")
_SHA = _re.compile(r"[0-9a-f]{40}\Z")


@_dataclass(frozen=True)
class SpotMarket:
    symbol: str; base_asset: str; quote_asset: str; status: str
    step_size: Decimal; min_quantity: Decimal; min_notional: Decimal
    bid: Decimal; ask: Decimal


@_dataclass(frozen=True)
class SpotAccountState:
    can_trade: bool; open_order_ids: tuple[int, ...]; balances: tuple[_http.Balance, ...]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value: raise _http.AdapterRefusal(label + " is required")
    return value


def _named(value: object, pattern: _re.Pattern) -> bool: return isinstance(value, str) and pattern.fullmatch(value) is not None


def _money(value: object, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)): raise _http.AdapterRefusal("money is not exact")
    with _http.exact_guard(): amount = +Decimal(value); invalid = not amount.is_finite() or amount < 0 or (positive and amount <= 0)
    if invalid: raise _http.AdapterRefusal("money is outside its bounds")
    return amount


def _input(value: object, positive: bool = True) -> Decimal:
    if not isinstance(value, Decimal): raise _http.AdapterRefusal("money input must be Decimal")
    return _money(value, positive)


def _wire(value: Decimal) -> str:
    text = format(value, "f"); return text.rstrip("0").rstrip(".") if "." in text else text


def _checked_plan(plan: object) -> _http.OrderPlan:
    if not isinstance(plan, _http.OrderPlan) or not isinstance(plan.intent, _http.MutationIntent): raise _http.AdapterRefusal("order plan is invalid")
    intent, quantity, quote, residual = plan.intent, plan.quantity, plan.quote_limit, plan.residual
    if (quantity is None) == (quote is None) or quote is not None and plan.side != "BUY": raise _http.AdapterRefusal("one side-compatible bound is required")
    bound = ("quantity", _wire(_input(quantity))) if quantity is not None else ("quoteOrderQty", _wire(_input(quote))); _input(residual, False)
    expected = (("symbol", plan.symbol), ("side", plan.side), ("type", "MARKET"), bound, ("newClientOrderId", intent.identity), ("newOrderRespType", "FULL"))
    if not _named(plan.symbol, _SYMBOL) or plan.side not in ("BUY", "SELL") or intent.venue != "BINANCE_SPOT" or intent.operation != "ORDER" or not _named(intent.identity, _CLIENT_ID) or not _named(intent.git_sha, _SHA) or intent.params != expected or plan.reduce_only is not False: raise _http.AdapterRefusal("order plan facts do not agree")
    return plan


def _bad(note: str) -> _http.ReadResult: return _http.ReadResult("unclear", None, _http.HttpResult("unclear", None, None, note))


def _failed(result: _http.HttpResult) -> _http.ReadResult: return _http.ReadResult(result.kind, None, result)


def _step(first: Decimal, second: Decimal) -> Decimal:
    with _http.exact_guard(): unit = Decimal(1).scaleb(min(first.as_tuple().exponent, second.as_tuple().exponent)); left, right = int(first // unit), int(second // unit); return Decimal(abs(left * right) // _math.gcd(left, right)) * unit


def _filters(rows: object) -> tuple[Decimal, Decimal, Decimal]:
    with _http.exact_guard():
        if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows): raise _http.AdapterRefusal("filters are malformed")
        names = [x.get("filterType") for x in rows]
        if any(not isinstance(x, str) for x in names) or len(names) != len(set(names)): raise _http.AdapterRefusal("filters are not unique")
        filters = dict(zip(names, rows)); lot = filters.get("LOT_SIZE")
        notions = [filters[x] for x in ("NOTIONAL", "MIN_NOTIONAL") if x in filters]
        if not isinstance(lot, dict) or len(notions) != 1: raise _http.AdapterRefusal("required filters are absent")
        step, minimum = _money(lot.get("stepSize"), True), _money(lot.get("minQty"), True)
        market = filters.get("MARKET_LOT_SIZE")
        if market is not None:
            if not isinstance(market, dict): raise _http.AdapterRefusal("market filter is malformed")
            market_step = _money(market.get("stepSize")); minimum = max(minimum, _money(market.get("minQty")))
            if market_step: step = _step(step, market_step)
        return step, minimum, _money(notions[0].get("minNotional"), True)


def read_market(transport: _http.Transport, base_url: str, symbol: str, *, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[SpotMarket]:
    _text(base_url, "base_url")
    if not _named(symbol, _SYMBOL): raise _http.AdapterRefusal("symbol is invalid")
    params = (("symbol", symbol),)
    info = _http.read(transport, base_url, _INFO, params, sleep=sleep)
    if info.kind != "ok": return _failed(info)
    try:
        rows = info.payload.get("symbols") if isinstance(info.payload, dict) else None
        if not isinstance(rows, list): raise _http.AdapterRefusal("symbol rows are malformed")
        matches = [x for x in rows if isinstance(x, dict) and x.get("symbol") == symbol]
        if len(matches) != 1 or matches[0].get("status") != "TRADING": raise _http.AdapterRefusal("symbol is not tradable")
        row = matches[0]; base, quote = row.get("baseAsset"), row.get("quoteAsset")
        if not _named(base, _ASSET) or not _named(quote, _ASSET): raise _http.AdapterRefusal("asset identity is invalid")
        step, minimum, notional = _filters(row.get("filters"))
    except _http.AdapterRefusal: return _bad("spot_exchange_info_schema")
    book = _http.read(transport, base_url, _BOOK, params, sleep=sleep)
    if book.kind != "ok": return _failed(book)
    try:
        if not isinstance(book.payload, dict) or book.payload.get("symbol") != symbol: raise _http.AdapterRefusal("book identity is not exact")
        bid, ask = _money(book.payload.get("bidPrice"), True), _money(book.payload.get("askPrice"), True)
        if bid >= ask: raise _http.AdapterRefusal("book is crossed")
    except _http.AdapterRefusal: return _bad("spot_book_schema")
    return _http.ReadResult("ok", SpotMarket(symbol, base, quote, "TRADING", step, minimum, notional, bid, ask), None)


def read_account_state(transport: _http.Transport, base_url: str, symbol: str, *, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[SpotAccountState]:
    _text(base_url, "base_url")
    if not _named(symbol, _SYMBOL): raise _http.AdapterRefusal("symbol is invalid")
    account = _http.read(transport, base_url, _ACCOUNT, signed=True, sleep=sleep)
    if account.kind != "ok": return _failed(account)
    try:
        payload = account.payload
        if not isinstance(payload, dict) or not isinstance(payload.get("canTrade"), bool) or not isinstance(payload.get("balances"), list): raise _http.AdapterRefusal("account is malformed")
        balances, assets = [], set()
        for row in payload["balances"]:
            asset = row.get("asset") if isinstance(row, dict) else None
            if not _named(asset, _ASSET): raise _http.AdapterRefusal("balance asset is invalid")
            if asset in assets: raise _http.AdapterRefusal("balance assets are not unique")
            assets.add(asset); balances.append(_http.Balance(asset, _money(row.get("free")), _money(row.get("locked")), None))
    except _http.AdapterRefusal: return _bad("spot_account_schema")
    orders = _http.read(transport, base_url, _OPEN, (("symbol", symbol),), signed=True, sleep=sleep)
    if orders.kind != "ok": return _failed(orders)
    try:
        if not isinstance(orders.payload, list): raise _http.AdapterRefusal("open orders are malformed")
        ids = [x.get("orderId") for x in orders.payload if isinstance(x, dict) and x.get("symbol") == symbol]
        if len(ids) != len(orders.payload) or any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in ids) or len(ids) != len(set(ids)): raise _http.AdapterRefusal("open-order identity is not exact")
    except _http.AdapterRefusal: return _bad("spot_open_orders_schema")
    return _http.ReadResult("ok", SpotAccountState(payload["canTrade"], tuple(sorted(ids)), tuple(balances)), None)


def build_market_order(market: SpotMarket, side: _Literal["BUY", "SELL"], client_id: str, git_sha: str, *, quantity: Decimal | None = None, quote_limit: Decimal | None = None) -> _http.OrderPlan:
    with _http.exact_guard():
        if not isinstance(market, SpotMarket) or market.status != "TRADING": raise _http.AdapterRefusal("market is not tradable")
        for value in (market.step_size, market.min_quantity, market.min_notional, market.bid, market.ask): _input(value)
        if not _named(market.symbol, _SYMBOL) or not _named(market.base_asset, _ASSET) or not _named(market.quote_asset, _ASSET) or market.bid >= market.ask: raise _http.AdapterRefusal("market is invalid")
        residual = Decimal(0)
        if quantity is not None:
            requested = _input(quantity); quantity = requested // market.step_size * market.step_size; residual = requested - quantity
            if quantity < market.min_quantity or quantity * (market.ask if side == "BUY" else market.bid) < market.min_notional: raise _http.AdapterRefusal("order is below filters")
            bound = ("quantity", _wire(quantity))
        else:
            quote_limit = _input(quote_limit)
            if quote_limit < market.min_notional or quote_limit < market.min_quantity * market.ask: raise _http.AdapterRefusal("order is below filters")
            bound = ("quoteOrderQty", _wire(quote_limit))
        params = (("symbol", market.symbol), ("side", side), ("type", "MARKET"), bound, ("newClientOrderId", client_id), ("newOrderRespType", "FULL"))
        intent = _http.MutationIntent("BINANCE_SPOT", "ORDER", client_id, git_sha, params); return _checked_plan(_http.OrderPlan(intent, market.symbol, side, quantity, quote_limit, residual, False))


def _empty(tag: str, symbol: str, client_id: str, error: _http.HttpResult | None = None) -> _http.OrderResult:
    return _http.OrderResult(tag, _http.OrderIdentity(symbol, client_id, None), None, None, False, None, False, None, None, (), error)


def _parsed(payload: object, symbol: str, client_id: str) -> _http.OrderResult | None:
    try:
        if not isinstance(payload, dict) or payload.get("symbol") != symbol or payload.get("clientOrderId") != client_id: raise _http.AdapterRefusal("order identity is not exact")
        order_id, status, side = payload.get("orderId"), payload.get("status"), payload.get("side")
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0 or not isinstance(status, str) or side not in ("BUY", "SELL"): raise _http.AdapterRefusal("order facts are malformed")
        executed, quote = _money(payload.get("executedQty")), _money(payload.get("cummulativeQuoteQty"))
        if (executed == 0) != (quote == 0) or (status == "FILLED" and executed == 0): raise _http.AdapterRefusal("order quantities do not agree")
    except _http.AdapterRefusal: return None
    terminal = status in _http.TERMINAL_ORDER_STATUSES
    return _http.OrderResult("terminal" if terminal else "pending", _http.OrderIdentity(symbol, client_id, order_id), client_id, status, terminal, side, False, executed, quote, (), None)


def query_order(transport: _http.Transport, base_url: str, symbol: str, client_id: str, *, sleep: _Callable[[float], None] = _time.sleep) -> _http.OrderResult:
    _text(base_url, "base_url")
    if not _named(symbol, _SYMBOL) or not _named(client_id, _CLIENT_ID): raise _http.AdapterRefusal("order identity is invalid")
    params = (("symbol", symbol), ("origClientOrderId", client_id)); missing, last = 0, None
    for attempt in range(_http.ORDER_QUERY_ATTEMPTS):
        result = transport("GET", base_url, SPOT_ORDER_PATH, params, True)
        parsed = _parsed(result.payload, symbol, client_id) if isinstance(result, _http.HttpResult) and result.kind == "ok" else None
        if parsed is not None:
            missing, last = 0, parsed
            if parsed.terminal: return parsed
        elif isinstance(result, _http.HttpResult) and result.kind == "api_error":
            if type(result.code) is not int or result.code != -2013: return _empty("error", symbol, client_id, result)
            missing += 1
            if missing == _http.ORDER_NOT_FOUND_STREAK: return last or _empty("not_found", symbol, client_id)
        else: missing = 0
        if attempt + 1 < _http.ORDER_QUERY_ATTEMPTS: sleep(_http.ORDER_QUERY_DELAY_SECONDS)
    return last or _empty("pending", symbol, client_id)


def _full_fills(payload: object, order_id: int, side: str | None) -> tuple[_http.Fill, ...]:
    rows = payload.get("fills", []) if isinstance(payload, dict) and payload.get("orderId") == order_id else None
    if not isinstance(rows, list): return ()
    found = []
    try:
        for row in rows:
            trade_id = row.get("tradeId") if isinstance(row, dict) else None
            if isinstance(trade_id, bool) or not isinstance(trade_id, int) or trade_id < 0 or any(x.trade_id == trade_id for x in found): raise _http.AdapterRefusal("fill identity is invalid")
            buyer, maker, when = (row["isBuyer"] if "isBuyer" in row else side == "BUY"), row.get("isMaker"), row.get("time"); fill_side = "BUY" if buyer is True else "SELL" if buyer is False else None
            if side not in ("BUY", "SELL") or type(buyer) is not bool or fill_side != side or type(maker) is not bool or type(when) is not int or when < 0: raise _http.AdapterRefusal("fill facts are invalid")
            price, quantity = _money(row.get("price"), True), _money(row.get("qty"), True)
            if not _named(row.get("commissionAsset"), _ASSET): raise _http.AdapterRefusal("fee asset is invalid")
            fee = _http.Fee(row["commissionAsset"], _money(row.get("commission")))
            with _http.exact_guard(): found.append(_http.Fill(trade_id, order_id, price, quantity, price * quantity, fee, fill_side, maker, when, None))
    except _http.AdapterRefusal: return ()
    return tuple(found)


def submit_order_once(transport: _http.Transport, base_url: str, plan: _http.OrderPlan, write_intent: _http.IntentWriter, *, sleep: _Callable[[float], None] = _time.sleep) -> _http.OrderResult:
    _text(base_url, "base_url")
    plan = _checked_plan(plan)
    try: write_intent(plan.intent)
    except _http.IntentNotDurable: raise
    except Exception: raise _http.IntentNotDurable("intent was not durably written") from None
    posted = transport("POST", base_url, SPOT_ORDER_PATH, plan.intent.params, True)
    returned = posted.payload.get("clientOrderId") if posted.kind == "ok" and isinstance(posted.payload, dict) else None
    mismatch = posted.kind == "ok" and returned != plan.intent.identity
    queried = query_order(transport, base_url, plan.symbol, plan.intent.identity, sleep=sleep)
    if mismatch or (queried.tag == "terminal" and queried.side != plan.side):
        error = _http.HttpResult("unclear", None, None, "returned_client_id_mismatch" if mismatch else "returned_order_side_mismatch")
        return _replace(queried, tag="pending", terminal=False, returned_client_id=(returned if isinstance(returned, str) else None) if mismatch else queried.returned_client_id, response_fills=(), error=error)
    if queried.tag == "terminal":
        fills = _full_fills(posted.payload, queried.identity.order_id, queried.side) if posted.kind == "ok" else ()
        return _replace(queried, response_fills=fills)
    if posted.kind == "api_error" and queried.tag == "not_found": return _empty("error", plan.symbol, plan.intent.identity, posted)
    return _replace(queried, tag="pending", terminal=False, returned_client_id=returned if isinstance(returned, str) else None, error=posted if posted.kind != "ok" else queried.error)


def _trade(row: object, symbol: str, order_id: int | None, start_ms: int | None, end_ms: int | None) -> _http.Fill:
    if not isinstance(row, dict) or row.get("symbol") != symbol: raise _http.AdapterRefusal("trade symbol is not exact")
    oid, trade_id = row.get("orderId"), row.get("id")
    if isinstance(oid, bool) or not isinstance(oid, int) or oid <= 0 or isinstance(trade_id, bool) or not isinstance(trade_id, int) or trade_id < 0 or (order_id is not None and oid != order_id): raise _http.AdapterRefusal("trade identity is not exact")
    buyer, maker, when = row.get("isBuyer"), row.get("isMaker"), row.get("time")
    if type(buyer) is not bool or type(maker) is not bool or type(when) is not int or when < 0 or start_ms is not None and not start_ms <= when <= end_ms: raise _http.AdapterRefusal("trade facts are incomplete")
    price, quantity = _money(row.get("price"), True), _money(row.get("qty"), True)
    if not _named(row.get("commissionAsset"), _ASSET): raise _http.AdapterRefusal("fee asset is invalid")
    fee = _http.Fee(row["commissionAsset"], _money(row.get("commission")))
    return _http.Fill(trade_id, oid, price, quantity, _money(row.get("quoteQty"), True), fee, "BUY" if buyer else "SELL", maker, when, None)


def read_my_trades(transport: _http.Transport, base_url: str, symbol: str, *, order_id: int | None = None, start_ms: int | None = None, end_ms: int | None = None, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[tuple[_http.Fill, ...]]:
    _text(base_url, "base_url")
    if not _named(symbol, _SYMBOL): raise _http.AdapterRefusal("symbol is invalid")
    id_mode = isinstance(order_id, int) and not isinstance(order_id, bool) and order_id > 0 and start_ms is None and end_ms is None
    window = order_id is None and all(isinstance(x, int) and not isinstance(x, bool) for x in (start_ms, end_ms))
    if id_mode == window or (window and (start_ms < 0 or end_ms < start_ms or end_ms - start_ms > _DAY_MS)): raise _http.AdapterRefusal("one bounded trade selector is required")
    params = (("symbol", symbol), ("orderId", str(order_id)), ("limit", "1000")) if id_mode else (("symbol", symbol), ("startTime", str(start_ms)), ("endTime", str(end_ms)), ("limit", "1000"))
    result = _http.read(transport, base_url, SPOT_MY_TRADES_PATH, params, signed=True, sleep=sleep)
    if result.kind != "ok": return _failed(result)
    try:
        if not isinstance(result.payload, list) or len(result.payload) > 1000: raise _http.AdapterRefusal("trade ledger is malformed")
        fills = tuple(_trade(x, symbol, order_id if id_mode else None, start_ms if window else None, end_ms if window else None) for x in result.payload)
        if len(fills) != len({x.trade_id for x in fills}): raise _http.AdapterRefusal("trade identities are not unique")
    except _http.AdapterRefusal: return _bad("spot_my_trades_schema")
    return _http.ReadResult("ok", fills, None)


def _merge(found: dict, fills: tuple[_http.Fill, ...], order_id: int) -> None:
    for fill in fills:
        if (not isinstance(fill, _http.Fill) or fill.order_id != order_id or isinstance(fill.trade_id, bool) or not isinstance(fill.trade_id, int) or fill.trade_id < 0 or not isinstance(fill.fee, _http.Fee) or not _named(fill.fee.asset, _ASSET) or
                fill.side not in ("BUY", "SELL") or type(fill.is_maker) is not bool or type(fill.time_ms) is not int or fill.time_ms < 0 or fill.realized_pnl is not None): raise _http.AdapterRefusal("fill identity or facts are invalid")
        _input(fill.price); _input(fill.quantity); _input(fill.quote_quantity, False); _input(fill.fee.amount, False)
        if fill.trade_id in found and found[fill.trade_id] != fill: raise _http.AdapterRefusal("fill identity conflicts")
        found[fill.trade_id] = fill


def read_fills(transport: _http.Transport, base_url: str, order: _http.OrderResult, response_fills: tuple[_http.Fill, ...] = (), *, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[_http.FillCoverage]:
    if not isinstance(order, _http.OrderResult) or not order.terminal or order.identity.order_id is None or not isinstance(order.executed_quantity, Decimal) or not isinstance(order.response_fills, tuple) or not isinstance(response_fills, tuple): return _bad("spot_order_fill_identity")
    found = {}
    try:
        executed = _money(order.executed_quantity); _merge(found, tuple(order.response_fills) + tuple(response_fills), order.identity.order_id)
        for attempt in range(6):
            fills = tuple(found.values())
            with _http.exact_guard(): covered = sum((x.quantity for x in fills), Decimal(0))
            if covered > executed: return _bad("spot_fill_coverage_conflict")
            if covered == executed or attempt == 5:
                value = _http.FillCoverage(fills, covered, executed, covered == executed, fees_by_asset(fills)); return _http.ReadResult("ok", value, None)
            sleep(1); readback = read_my_trades(transport, base_url, order.identity.symbol, order_id=order.identity.order_id, sleep=sleep)
            if readback.kind == "ok": _merge(found, readback.value, order.identity.order_id)
    except (TypeError, _http.AdapterRefusal): return _bad("spot_fill_arithmetic_or_schema")


def fees_by_asset(fills: tuple[_http.Fill, ...]) -> tuple[_http.Fee, ...]:
    if not isinstance(fills, tuple): raise _http.AdapterRefusal("fills must be immutable")
    totals = {}
    try:
        with _http.exact_guard():
            for fill in fills:
                if not isinstance(fill, _http.Fill) or not isinstance(fill.fee, _http.Fee) or not isinstance(fill.fee.amount, Decimal): raise _http.AdapterRefusal("fill commission is missing")
                asset = fill.fee.asset
                if not _named(asset, _ASSET): raise _http.AdapterRefusal("fee asset is missing")
                totals[asset] = totals.get(asset, Decimal(0)) + _money(fill.fee.amount)
    except TypeError: raise _http.AdapterRefusal("fills must be immutable") from None
    return tuple(_http.Fee(asset, totals[asset]) for asset in sorted(totals))


def net_base_quantity(executed_quantity: Decimal, base_asset: str, fees: tuple[_http.Fee, ...]) -> Decimal:
    if not isinstance(executed_quantity, Decimal): raise _http.AdapterRefusal("executed quantity must be Decimal")
    if not _named(base_asset, _ASSET) or not isinstance(fees, tuple): raise _http.AdapterRefusal("base asset or fees are invalid")
    try:
        with _http.exact_guard():
            executed, base_fee = _money(executed_quantity), Decimal(0)
            for fee in fees:
                if not isinstance(fee, _http.Fee) or not isinstance(fee.amount, Decimal): raise _http.AdapterRefusal("fee is unproved")
                if not _named(fee.asset, _ASSET): raise _http.AdapterRefusal("fee asset is unproved")
                amount = _money(fee.amount); base_fee += amount if fee.asset == base_asset else Decimal(0)
            if base_fee > executed: raise _http.AdapterRefusal("base fee exceeds execution")
            return executed - base_fee
    except TypeError: raise _http.AdapterRefusal("fees must be immutable") from None
