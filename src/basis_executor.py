"""One-shot normal-basis cycle using the shared Binance execution hand."""

from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import math
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import events
from basis_cycle import (
    ENTER_SHORT,
    ENTER_SPOT,
    EXIT_SHORT,
    EXIT_SPOT,
    MAX_CYCLE_QUOTE,
    UNWIND_SPOT,
    CyclePlan,
    client_id,
    plan_cycle,
    quantize_down,
    run_cycle,
)
from basis_read import (
    SPOT_BOOK_PATH,
    USDM_BOOK_PATH,
    USDM_PREMIUM_PATH,
    BasisReadError,
    build_basis_read_row,
    now_utc,
)
from execution import binance_futures as futures
from execution import binance_http as http
from execution import binance_spot as spot


def d(value: Any) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise ArithmeticError("money is not exact")
    try:
        with http.exact_guard():
            result = +Decimal(value)
    except (ValueError, http.AdapterRefusal) as error:
        raise ArithmeticError("money is malformed") from error
    if not result.is_finite():
        raise ArithmeticError("money is not finite")
    return result


def fmt(value: Decimal) -> str: return format(value, "f")


def clean(value: Any, limit: int = 160) -> str: return " ".join(str(value).split())[:limit]


def common_step(first: Decimal, second: Decimal) -> Decimal:
    with http.exact_guard():
        if first <= 0 or second <= 0:
            raise http.AdapterRefusal("quantity step must be positive")
        unit = Decimal(1).scaleb(min(first.as_tuple().exponent,
                                    second.as_tuple().exponent))
        left, right = int(first // unit), int(second // unit)
        return Decimal(abs(left * right) // math.gcd(left, right)) * unit

ALL_STAGES = (ENTER_SPOT, ENTER_SHORT, EXIT_SHORT, EXIT_SPOT, UNWIND_SPOT)
HTTP_TIMEOUT = http.HTTP_TIMEOUT_SECONDS
SYMBOL_RE = re.compile(r"[A-Z0-9]{5,20}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
EXIT_CODE = {
    "CLOSED": 0,
    "REJECTED_BEFORE_ENTRY": 2,
    "OPEN_EXPOSURE": 3,
    "BLOCKED_UNRESOLVED": 3,
    "UNKNOWN": 4,
    "PREFLIGHT_REFUSED": 5,
}
USDM_POSITION_PATH = "/fapi/v3/positionRisk"
USDM_SYMBOL_CONFIG_PATH = "/fapi/v1/symbolConfig"
USDM_ACCOUNT_CONFIG_PATH = "/fapi/v1/accountConfig"
USDM_POSITION_MODE_PATH = "/fapi/v1/positionSide/dual"
USDM_OPEN_ORDERS_PATH = "/fapi/v1/openOrders"
USDM_ACCOUNT_PATH = "/fapi/v3/account"
SPOT_ACCOUNT_PATH = "/api/v3/account"
SPOT_OPEN_ORDERS_PATH = "/api/v3/openOrders"


@dataclass(frozen=True)
class Venue:
    name: str; base_url: str; quote_field: str


SPOT = Venue("spot", http.SPOT_BASE_URL, "cummulativeQuoteQty")
USDM = Venue("usdm", http.FUTURES_BASE_URL, "cumQuote")
VENUES = {"spot": SPOT, "usdm": USDM}


class ExecutorExit(Exception):
    def __init__(self, code: int):
        super().__init__(code); self.code = code


class Blocked(Exception): pass


class Refusal(Exception): pass


class PreflightRefusal(Exception): pass


def _typed_transport(request: Callable[..., Any]) -> http.Transport:
    def transport(method: str, base_url: str, path: str,
                  params: Optional[http.Params] = None,
                  signed: bool = False) -> http.HttpResult:
        legacy_params = dict(params) if params is not None else None
        try:
            result = request(method, base_url, path, legacy_params, signed)
        except Exception:
            return http.HttpResult("unclear", None, None, "transport_error")
        if isinstance(result, http.HttpResult): return result
        if not isinstance(result, tuple) or len(result) != 2:
            return http.HttpResult("unclear", None, None, "transport_result_invalid")
        kind, payload = result
        if kind == "ok": return http.HttpResult("ok", payload, None, None)
        code = payload.get("code") if isinstance(payload, dict) else None
        msg = payload.get("msg") if isinstance(payload, dict) else None
        code = code if type(code) is int else None; msg = clean(msg, 160) if isinstance(msg, str) else None
        if kind == "api_error" and code is not None:
            return http.HttpResult("api_error", None, code, msg)
        return http.HttpResult("unclear", None, code, msg or str(kind))
    return transport


@dataclass(frozen=True)
class ExecutorConfig:
    symbol: str; sha: str; receipt_dir: str
    min_entry_bps: Decimal = Decimal("1"); events_path: str = ""; run_id: str = ""


@dataclass
class RunState:
    cfg: ExecutorConfig; request: Callable[..., Tuple[str, Any]]
    sleep: Callable[[float], None]; run_id: str; receipt_path: str
    lock_path: str; events_path: str; client_ids: Tuple[str, ...] = ()
    receipt: Dict[str, Any] = field(default_factory=dict); events: List[Dict[str, Any]] = field(default_factory=list)
    outcomes: Dict[str, str] = field(default_factory=dict); locked: bool = False
    partial_short: Optional[Decimal] = None
    transport: http.Transport = field(init=False, repr=False)
    spot_market: Optional[spot.SpotMarket] = field(default=None, repr=False)
    futures_market: Optional[futures.FuturesMarket] = field(default=None, repr=False)

    def __post_init__(self) -> None: self.transport = _typed_transport(self.request)


def utc() -> str: return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_run_id() -> str: return format(int(time.time()), "x") + secrets.token_hex(3)


def save(state: RunState) -> None:
    if not state.receipt_path: return
    tmp = state.receipt_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state.receipt, handle, indent=2, default=str); handle.flush()
        os.fchmod(handle.fileno(), 0o600); os.fsync(handle.fileno())
    os.replace(tmp, state.receipt_path)
    descriptor = os.open(os.path.dirname(state.receipt_path) or ".", os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally: os.close(descriptor)


def first_fail(state: RunState, where: str, detail: Any) -> None:
    if "first_failure" not in state.receipt:
        state.receipt["first_failure"] = {
            "utc": utc(), "where": clean(where, 60), "detail": clean(detail, 200),
        }
        save(state)


def contract_note(text: Any, identifiers: Tuple[str, ...]) -> str:
    note = " ".join(str(text).split())
    for identifier in sorted({i for i in identifiers if len(i) > 4}, key=len, reverse=True):
        note = note.replace(identifier, identifier[-4:])
    note = events.LONG_ID_RE.sub(lambda match: match.group(0)[-4:], note); note = events.OPAQUE_TOKEN_RE.sub(lambda match: match.group(0)[-4:], note)
    return note[:480]


def base_event(state: RunState, kind: str) -> Dict[str, Any]:
    return {"ts": utc(), "sha8": state.cfg.sha[:8].lower(), "kind": kind,
            "run_id": state.run_id, "symbol": state.cfg.symbol}


def write_events(state: RunState) -> None:
    if not state.events: return
    try:
        events.write_jsonl(state.events, state.events_path, append=True)
    except (events.ReceiptError, OSError) as exc:
        first_fail(state, "events_contract", exc); state.receipt["events_error"] = clean(exc, 200); save(state)


def finish(state: RunState, verdict: str, exposure: str, note: str) -> None:
    state.receipt.update({
        "outcomes": dict(state.outcomes),
        "verdict": verdict,
        "exposure": clean(exposure, 300),
        "verdict_note": clean(note, 300),
        "finished_utc": utc(),
    })
    save(state); verdict_event = base_event(state, "run_verdict"); verdict_event["verdict"] = verdict
    residual = state.receipt.get("result", {}).get("residual_base_from_this_run")
    if residual not in (None, "UNKNOWN"): verdict_event["residual"] = residual
    text = exposure if exposure and exposure != "none" else note
    if text: verdict_event["note"] = contract_note(text, state.client_ids)
    state.events.append(verdict_event); write_events(state)
    if state.locked and verdict in ("CLOSED", "REJECTED_BEFORE_ENTRY",
                                    "PREFLIGHT_REFUSED"):
        os.unlink(state.lock_path); state.locked = False
    print("lane: BASIS-EXECUTOR"); print("run_id:", state.run_id)
    print("sha8:", state.cfg.sha[:8]); print("symbol:", state.cfg.symbol)
    for leg in state.receipt.get("legs", []):
        print("leg: %s %s executed=%s client_last4=%s" % (
            leg.get("stage"), leg.get("status", "-"),
            leg.get("executed_qty", "-"), leg.get("client_id", "")[-4:]))
    if "first_failure" in state.receipt: print("first_failure:", json.dumps(state.receipt["first_failure"]))
    print("VERDICT:", verdict); print("EXPOSURE:", clean(exposure, 300))
    print("NOTE:", clean(note, 300)); print("RECEIPT:", state.receipt_path); print("EVENTS:", state.events_path)
    raise ExecutorExit(EXIT_CODE[verdict])


# _W7BasisPositionCompat names delegate to typed execution services.
def get_retry(state: RunState, base_url: str, path: str,
              params: Optional[Dict[str, str]] = None, signed: bool = False,
              tries: int = 3) -> Tuple[str, Any]:
    if tries != http.READ_ATTEMPTS: raise Refusal("read attempts must use the shared bound")
    canonical = tuple((str(key), str(value)) for key, value in params.items()) \
        if params is not None else None
    result = http.read(state.transport, base_url, path, canonical,
                       signed=signed, sleep=state.sleep)
    if result.kind == "ok": return "ok", result.payload
    payload = {"code": result.code, "msg": result.msg} if result.kind == "api_error" else {}
    return result.kind, payload


def _read_payload(state: RunState, base_url: str, path: str,
                  params: Optional[http.Params], label: str,
                  *, signed: bool = False) -> Any:
    result = http.read(state.transport, base_url, path, params, signed=signed,
                       sleep=state.sleep)
    if result.kind != "ok": raise Refusal(label + " read failed")
    return result.payload


def _spot_market(state: RunState) -> spot.SpotMarket:
    if state.spot_market is None:
        result = spot.read_market(state.transport, SPOT.base_url, state.cfg.symbol, sleep=state.sleep)
        if result.kind != "ok" or result.value is None:
            raise Refusal("basis read refused: Spot market failed shared validation")
        state.spot_market = result.value
    return state.spot_market


def _futures_market(state: RunState) -> futures.FuturesMarket:
    if state.futures_market is None:
        result = futures.read_market(state.transport, USDM.base_url, state.cfg.symbol, sleep=state.sleep)
        if result.kind != "ok" or result.value is None: raise Refusal("USD-M market read failed shared validation")
        state.futures_market = result.value
    return state.futures_market


def market_context(state: RunState) -> Dict[str, Any]:
    spot_market, futures_market = _spot_market(state), _futures_market(state)
    if spot_market.quote_asset != "USDT": raise Refusal("cycle accepts only USDT-quoted Spot symbols")
    if futures_market.quote_asset != "USDT" or \
            futures_market.margin_asset != "USDT":
        raise Refusal("cycle accepts only USDT-quoted, USDT-margined contracts")
    try:
        with http.exact_guard():
            shared_step = common_step(spot_market.step_size, futures_market.step_size)
            min_notional = max(spot_market.min_notional, futures_market.min_notional)
            min_quantity = max(spot_market.min_quantity, futures_market.min_quantity)
    except http.AdapterRefusal as error:
        raise Refusal(str(error)) from None
    return {"base": spot_market.base_asset, "shared_step": shared_step,
            "spot_step": spot_market.step_size, "min_notional": min_notional,
            "min_qty": min_quantity, "band_up": futures_market.multiplier_up,
            "spot_min_notional": spot_market.min_notional,
            "spot_min_qty": spot_market.min_quantity,
            "spot_market": spot_market, "futures_market": futures_market}


def gap_row(state: RunState, market: Optional[Dict[str, Any]] = None):
    symbol = state.cfg.symbol; market = market or market_context(state)
    spot_market = market["spot_market"]; futures_market = market["futures_market"]
    raw_book = _read_payload(
        state, USDM.base_url, USDM_BOOK_PATH, (("symbol", symbol),),
        "USD-M book")
    premium = _read_payload(
        state, USDM.base_url, USDM_PREMIUM_PATH, (("symbol", symbol),),
        "USD-M premium")
    if not isinstance(raw_book, dict) or raw_book.get("symbol") != symbol: raise Refusal("USD-M book identity is unproved")
    spot_book = {"symbol": symbol, "bidPrice": fmt(spot_market.bid),
                 "askPrice": fmt(spot_market.ask)}
    usdm_book = {"symbol": symbol, "bidPrice": fmt(futures_market.bid),
                 "askPrice": fmt(futures_market.ask),
                 "time": raw_book.get("time")}
    try:
        row = build_basis_read_row(symbol, spot_book, usdm_book, premium,
                                   completed_utc=now_utc())
    except BasisReadError as exc:
        raise Refusal(f"basis read refused: {exc}") from None
    state.receipt["gap"] = {
        name: fmt(value) if isinstance(value, Decimal) else value
        for name, value in dataclasses.asdict(row).items()}
    save(state); gap_event = base_event(state, "gap_read")
    gap_event["note"] = contract_note(
        "entry %s bps exit %s bps funding %s" % (
            fmt(row.entry_gap_bps_vs_spot_ask),
            fmt(row.exit_gap_bps_vs_spot_bid), fmt(row.funding_rate)),
        state.client_ids)
    state.events.append(gap_event); return row


def _order_payload(result: http.OrderResult, venue: Venue) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "symbol": result.identity.symbol, "clientOrderId": result.returned_client_id or result.identity.client_id,
        "status": result.status, "side": result.side,
        "executedQty": fmt(result.executed_quantity) if result.executed_quantity is not None else None,
        venue.quote_field: fmt(result.quote_quantity) if result.quote_quantity is not None else None}
    if result.identity.order_id is not None: payload["orderId"] = result.identity.order_id
    if venue.name == "usdm": payload["reduceOnly"] = result.reduce_only
    return payload


def _error_note(result: Optional[http.HttpResult]) -> str:
    if result is None: return "shared order truth remained pending"
    code = str(result.code) if result.code is not None else result.kind
    return clean("%s %s" % (code, result.msg or ""), 200)


def query_client_order(state: RunState, venue: Venue,
                       order_client_id: str) -> Tuple[str, Any]:
    service = spot.query_order if venue.name == "spot" else futures.query_order
    result = service(state.transport, venue.base_url, state.cfg.symbol,
                     order_client_id, sleep=state.sleep)
    if result.tag == "error": first_fail(state, "query_order", _error_note(result.error))
    payload = _order_payload(result, venue) if result.identity else None
    return result.tag, payload


def _leg_plan(state: RunState, stage: str, plan: CyclePlan,
              quantity: Optional[Decimal]) -> Tuple[Venue, http.OrderPlan]:
    identity = client_id(stage, state.run_id)
    if stage == ENTER_SPOT:
        return SPOT, spot.build_market_order(_spot_market(state), "BUY", identity, state.cfg.sha, quote_limit=plan.spot_quote_cap)
    if stage in (EXIT_SPOT, UNWIND_SPOT):
        return SPOT, spot.build_market_order(_spot_market(state), "SELL", identity, state.cfg.sha, quantity=quantity)
    if stage == ENTER_SHORT:
        return USDM, futures.build_open_order(_futures_market(state), "SELL", identity, state.cfg.sha, quantity, profile="MARKET")
    if stage == EXIT_SHORT:
        return USDM, futures.build_reduce_only_close(_futures_market(state), "BUY", identity, state.cfg.sha, quantity)
    raise http.AdapterRefusal("basis stage is invalid")


def _record_coverage(state: RunState, leg: Dict[str, Any],
                     coverage: Optional[http.FillCoverage]) -> None:
    if coverage is None:
        leg["fill_coverage"] = "UNKNOWN"; save(state); return
    fee_map = {fee.asset: fmt(fee.amount) for fee in coverage.fees}
    leg.update({"fill_covered_qty": fmt(coverage.covered_quantity),
                "fill_executed_qty": fmt(coverage.executed_quantity),
                "fill_coverage_complete": coverage.complete,
                "fee_by_asset": fee_map})
    save(state)
    if fee_map and state.events and state.events[-1]["kind"] == "order_terminal": state.events[-1]["fees"] = fee_map


def settle_leg(state: RunState, stage: str, plan: CyclePlan,
               quantity: Optional[Decimal] = None):
    try:
        venue, order_plan = _leg_plan(state, stage, plan, quantity)
    except (http.AdapterRefusal, Refusal) as error:
        first_fail(state, stage.lower(), clean(error, 200)); return "rejected", {"msg": clean(error, 200)}, None
    order_client_id = order_plan.intent.identity
    leg: Dict[str, Any] = {
        "stage": stage, "market": venue.name, "side": order_plan.side,
        "client_id": order_client_id, "submitted_utc": utc(),
    }
    if order_plan.quantity is not None: leg["quantity"] = fmt(order_plan.quantity)
    if order_plan.quote_limit is not None: leg["quote_order_qty"] = fmt(order_plan.quote_limit)
    state.receipt["legs"].append(leg); save(state)
    def write_intent(intent: http.MutationIntent) -> None:
        if intent != order_plan.intent: raise http.IntentNotDurable("intent identity changed")
        leg["intent"] = {
            "stage": stage, "venue": intent.venue, "identity": intent.identity,
            "operation": intent.operation, "git_sha": intent.git_sha,
            "params": [list(pair) for pair in intent.params],
            "quantity": fmt(order_plan.quantity) if order_plan.quantity is not None else None,
            "quote_limit": fmt(order_plan.quote_limit) if order_plan.quote_limit is not None else None}
        save(state); submitted = base_event(state, "order_submitted")
        submitted["side"] = order_plan.side; submitted["client_id_last4"] = order_client_id[-4:]
        state.events.append(submitted)

    submit = spot.submit_order_once if venue.name == "spot" \
        else futures.submit_order_once
    try:
        result = submit(state.transport, venue.base_url, order_plan,
                        write_intent, sleep=state.sleep)
    except (http.IntentNotDurable, http.AdapterRefusal) as error:
        first_fail(state, stage.lower(), clean(error, 200)); leg.update({
            "status": "NOT_SUBMITTED", "reason": "intent was not durable or valid"})
        save(state); return "rejected", {"msg": clean(error, 200)}, None

    order = _order_payload(result, venue)
    if result.tag == "terminal":
        executed = result.executed_quantity; quote = result.quote_quantity; order_id = str(result.identity.order_id)
        leg.update({"status": result.status, "order_id": order_id,
                    "executed_qty": fmt(executed), "quote": fmt(quote),
                    "terminal_utc": utc()})
        save(state); terminal = base_event(state, "order_terminal")
        terminal.update({"side": order_plan.side,
                         "client_id_last4": order_client_id[-4:],
                         "order_id_last4": order_id[-4:],
                         "executed": fmt(executed), "quote": fmt(quote),
                         "note": contract_note(
                             "%s %s" % (stage, result.status),
                             state.client_ids)})
        state.events.append(terminal)
        fills = (spot.read_fills if venue.name == "spot" else futures.read_fills)(state.transport, venue.base_url, result, sleep=state.sleep)
        coverage = fills.value if fills.kind == "ok" else None
        _record_coverage(state, leg, coverage); return "terminal", order, coverage

    if result.tag == "error":
        reason = _error_note(result.error)
        leg.update({"status": "REJECTED", "reason": reason, "terminal_utc": utc()})
        save(state); terminal = base_event(state, "order_terminal")
        terminal.update({"side": order_plan.side,
                         "client_id_last4": order_client_id[-4:],
                         "executed": "0",
                         "note": contract_note(
                             "%s REJECTED %s" % (stage, reason),
                             state.client_ids)})
        state.events.append(terminal); return "rejected", order, None

    where = "query_order" if result.error is not None and result.error.kind == "api_error" else stage.lower()
    first_fail(state, where, _error_note(result.error)); leg.update({"status": "UNKNOWN", "query_state": result.tag})
    save(state); return "unknown", order, None


def spot_trades(state: RunState, order_id: str, executed: Decimal,
                response_fills: Optional[http.FillCoverage]):
    if not isinstance(response_fills, http.FillCoverage):
        first_fail(state, "order_trades", "fill coverage is unproved"); return (), None
    if not response_fills.complete:
        first_fail(state, "order_trades", "fills cover %s of %s" % (
            fmt(response_fills.covered_quantity), fmt(executed)))
    return response_fills.fills, response_fills.covered_quantity


def base_fee_of(trades: Tuple[http.Fill, ...], base_asset: str) -> Decimal:
    fees = spot.fees_by_asset(tuple(trades))
    return next((fee.amount for fee in fees if fee.asset == base_asset), Decimal(0))


def attach_fees(state: RunState, leg_index: int, trades) -> None:
    fees = spot.fees_by_asset(tuple(trades))
    fee_map = {fee.asset: fmt(fee.amount) for fee in fees}
    state.receipt["legs"][leg_index]["fee_by_asset"] = fee_map; save(state)
    if fee_map and state.events and state.events[-1]["kind"] == "order_terminal": state.events[-1]["fees"] = fee_map


def futures_position(value: Any, symbol: str) -> Optional[Decimal]:
    if not isinstance(value, tuple) or len(value) != 2: return None
    market, facts = value; return facts.position_quantity if isinstance(market, futures.FuturesMarket) and isinstance(facts, futures.FuturesPreflight) and market.symbol == symbol else None


def signed_read(state: RunState, base_url: str, path: str,
                params: Optional[Dict[str, str]], label: str) -> Any:
    try:
        kind, payload = get_retry(state, base_url, path, params, signed=True)
    except Refusal as error: raise PreflightRefusal(str(error)) from None
    if kind != "ok": raise PreflightRefusal("%s unavailable: %s" % (label, kind))
    return payload


def symbol_leverage(rows: Any, symbol: str) -> int:
    def one_read(_method, _base, _path, _params=None, _signed=False):
        return http.HttpResult("ok", rows, None, None)
    result = futures.read_leverage(one_read, USDM.base_url, symbol, sleep=lambda _seconds: None)
    if result.kind != "ok" or result.value is None:
        raise PreflightRefusal(
            "symbol configuration did not return exactly one readable row; "
            "symbol leverage is missing or unreadable")
    return result.value


def parse_trade_capability(payload: Any) -> None:
    if not isinstance(payload, futures.FuturesPreflight) or not payload.can_trade: raise PreflightRefusal("Futures account canTrade is not true")


def signed_preflight(state: RunState, plan: CyclePlan,
                     market: Optional[Dict[str, Any]] = None) -> None:
    market = market or market_context(state)
    spot_state = spot.read_account_state(state.transport, SPOT.base_url, state.cfg.symbol, sleep=state.sleep)
    if spot_state.kind != "ok" or spot_state.value is None: raise PreflightRefusal("Spot account read unavailable")
    if not spot_state.value.can_trade: raise PreflightRefusal("Spot account canTrade is not true")
    if spot_state.value.open_order_ids: raise PreflightRefusal("Spot open order already stands on the plan symbol")
    quote_balance = next((item for item in spot_state.value.balances if item.asset == market["spot_market"].quote_asset), None)
    if quote_balance is None: raise PreflightRefusal("Spot quote balance is unavailable")
    if quote_balance.free < plan.spot_quote_cap: raise PreflightRefusal("available Spot balance %s is below the quote cap %s USDT" % (fmt(quote_balance.free), fmt(plan.spot_quote_cap)))

    leverage = futures.read_leverage(state.transport, USDM.base_url, state.cfg.symbol, sleep=state.sleep)
    if leverage.kind != "ok" or leverage.value is None:
        raise PreflightRefusal(
            "Futures symbol-configuration read unavailable or did not return "
            "exactly one row; symbol leverage is missing or unreadable")
    if leverage.value != plan.leverage:
        raise PreflightRefusal("actual symbol leverage is %sx, not exactly %sx; refusing before any order (this module never changes leverage)" % (leverage.value, plan.leverage))

    checked = futures.read_preflight(state.transport, USDM.base_url, market["futures_market"], plan.short_notional, require_flat=True, sleep=state.sleep)
    facts = checked.value if checked.kind == "ok" else None
    if facts is None:
        diagnostic = futures.read_preflight(state.transport, USDM.base_url, market["futures_market"], None, require_flat=False, sleep=state.sleep)
        facts = diagnostic.value if diagnostic.kind == "ok" else None
        if facts is None:
            raise PreflightRefusal("Futures preflight unavailable: shared reads leave canTrade, ONE_WAY, open-order absence, and flatness unproved; cannot prove flatness")
        if facts.open_order_ids:
            raise PreflightRefusal("%s Futures open order(s) already stand on the plan symbol" % len(facts.open_order_ids))
        if facts.position_quantity != 0:
            raise PreflightRefusal("existing Futures position %s on the plan symbol; attribution unsafe" % fmt(facts.position_quantity))
        with http.exact_guard():
            required = plan.short_notional * market["band_up"]
        if facts.available_balance < required:
            raise PreflightRefusal(
                "available Futures balance %s is below the banded short margin "
                "%s USDT (notional %s x band %s at %sx)" % (
                    fmt(facts.available_balance), fmt(required),
                    fmt(plan.short_notional), fmt(market["band_up"]),
                    plan.leverage))
        raise PreflightRefusal("Futures preflight could not prove flatness")

    required = facts.required_margin
    state.receipt["preflight"] = {"futures_can_trade": facts.can_trade,
        "spot_can_trade": spot_state.value.can_trade, "position_mode": "ONE_WAY",
        "symbol_leverage": facts.leverage, "open_orders": len(facts.open_order_ids) + len(spot_state.value.open_order_ids),
        "position_amount": fmt(facts.position_quantity), "available_balance": fmt(facts.available_balance),
        "required_short_margin_1x": fmt(required), "margin_band_up": fmt(market["band_up"]), "verified_utc": utc()}
    save(state)


def conclude(state: RunState, plan: CyclePlan,
             hedge: Optional[Decimal]) -> None:
    result = run_cycle(plan, state.run_id, state.outcomes, acquired_base=hedge)
    exposure = result.exposure
    if state.partial_short is not None and exposure != "none":
        exposure += "; short leg actual fill %s" % fmt(state.partial_short)
    finish(state, result.verdict, exposure, result.note)


def sold_leg_result(state: RunState, stage: str, order, fills,
                    net_base: Decimal, hedge: Decimal,
                    market: Dict[str, Any]) -> Decimal:
    base_asset = market["base"]
    shared_step = market["shared_step"]
    spot_step = market["spot_step"]
    sold = d(order.get("executedQty"))
    quote_received = d(order.get(SPOT.quote_field))
    trades, covered = spot_trades(
        state, state.receipt["legs"][-1].get("order_id", ""), sold, fills)
    if covered is not None:
        attach_fees(state, len(state.receipt["legs"]) - 1, trades)
    if covered is None or covered != sold:
        state.receipt["result"] = {"residual_base_from_this_run": "UNKNOWN"}
        save(state)
        finish(state, "UNKNOWN",
               "%s executed but its trade and fee data never loaded; residual "
               "unknowable" % stage,
               "reconcile the %s client id before trusting balances" % stage)
    sell_base_fee = base_fee_of(trades, base_asset)
    with http.exact_guard():
        residual = net_base - sold - sell_base_fee
    state.receipt["result"] = {
        "stage": stage,
        "sold_qty": fmt(sold),
        "quote_received": fmt(quote_received),
        "sell_base_fee": fmt(sell_base_fee),
        "residual_base_from_this_run": fmt(residual),
    }
    save(state)
    if residual < 0:
        finish(state, "OPEN_EXPOSURE",
               "sale consumed %s %s beyond this run's acquisition; "
               "pre-existing balance was touched" % (fmt(-residual), base_asset),
               "report to owner; no repair trade")
    if sold < hedge and residual >= spot_step:
        finish(state, "OPEN_EXPOSURE",
               "%s sold %s of the submitted %s; residual %s %s at or above "
               "the spot step %s is sellable"
               % (stage, fmt(sold), fmt(hedge), fmt(residual), base_asset,
                  fmt(spot_step)),
               "partial spot sale left a sellable remainder; executor reports "
               "and stops; no repair trade")
    if residual >= shared_step:
        finish(state, "OPEN_EXPOSURE",
               "%s sold %s of %s; residual %s %s at or above the shared step"
               % (stage, fmt(sold), fmt(hedge), fmt(residual), base_asset),
               "spot sale left a remainder at or above the shared step; "
               "executor reports and stops; no repair trade")
    return residual


def execute_legs(state: RunState, plan: CyclePlan,
                 market: Dict[str, Any]) -> None:
    base_asset = market["base"]
    shared_step = market["shared_step"]

    res, order, fills = settle_leg(state, ENTER_SPOT, plan)
    if res == "rejected":
        state.outcomes[ENTER_SPOT] = "REJECTED"
        conclude(state, plan, None)
    if res == "unknown":
        state.outcomes[ENTER_SPOT] = "UNKNOWN"
        conclude(state, plan, None)
    executed = d(order.get("executedQty"))
    quote_spent = d(order.get(SPOT.quote_field))
    if executed <= 0:
        state.receipt["legs"][-1]["note"] = "terminal zero fill; nothing acquired"
        save(state)
        state.outcomes[ENTER_SPOT] = "REJECTED"
        conclude(state, plan, None)
    state.outcomes[ENTER_SPOT] = "FILLED"

    trades, covered = spot_trades(
        state, state.receipt["legs"][-1].get("order_id", ""), executed, fills)
    if covered is not None:
        attach_fees(state, len(state.receipt["legs"]) - 1, trades)
    if covered is None or covered != executed:
        state.receipt["acquired"] = {"executed_qty": fmt(executed),
                                     "held_worst_case": fmt(executed)}
        save(state)
        finish(state, "OPEN_EXPOSURE",
               "holding up to %s %s from the spot BUY" % (fmt(executed),
                                                          base_asset),
               "trade and fee data never covered the spot fill; sizing a later "
               "leg on an assumed fee is forbidden; no repair trade")
    fees = spot.fees_by_asset(tuple(trades))
    buy_base_fee = base_fee_of(trades, base_asset)
    net_base = spot.net_base_quantity(executed, base_asset, fees)
    hedge = quantize_down(net_base, shared_step)
    state.receipt["acquired"] = {
        "executed_qty": fmt(executed), "quote_spent": fmt(quote_spent),
        "base_fee": fmt(buy_base_fee), "net_base": fmt(net_base),
        "shared_step": fmt(shared_step), "hedge_quantity": fmt(hedge),
    }
    save(state)
    if hedge <= 0:
        finish(state, "OPEN_EXPOSURE",
               "holding %s %s dust below the shared step; cannot hedge or "
               "sell it back" % (fmt(net_base), base_asset),
               "net base after fees floors to zero at the shared step; "
               "executor reports and stops; no repair trade")

    short_filled: Optional[Decimal] = None
    if hedge < market["min_qty"]:
        state.outcomes[ENTER_SHORT] = "REJECTED"
        state.receipt["legs"].append({
            "stage": ENTER_SHORT, "market": "usdm", "side": "SELL",
            "client_id": client_id(ENTER_SHORT, state.run_id),
            "status": "NOT_SUBMITTED",
            "reason": "net quantity below venue minimum quantity",
        })
        save(state)
        first_fail(state, "enter_short",
                   "net quantity below venue minimum; short not submitted")
    else:
        res, order, short_coverage = settle_leg(
            state, ENTER_SHORT, plan, hedge)
        if res == "unknown":
            state.outcomes[ENTER_SHORT] = "UNKNOWN"
            conclude(state, plan, hedge)
        if res == "rejected":
            state.outcomes[ENTER_SHORT] = "REJECTED"
        else:
            short_filled = d(order.get("executedQty"))
            if short_filled <= 0:
                state.receipt["legs"][-1]["note"] = "terminal zero fill"
                save(state)
                state.outcomes[ENTER_SHORT] = "REJECTED"
                short_filled = None
            else:
                state.outcomes[ENTER_SHORT] = "FILLED"
                if short_coverage is None or not short_coverage.complete:
                    finish(state, "UNKNOWN",
                           "short entry is terminal but complete Futures fill "
                           "and fee truth is unavailable",
                           "no dependent close was sent; reconcile the exact "
                           "entry client id")
                short_quote = d(order.get(USDM.quote_field))
                if short_quote > MAX_CYCLE_QUOTE:
                    state.receipt["legs"][-1]["ceiling_breach"] = {
                        "cum_quote": fmt(short_quote),
                        "hard_ceiling_usdt": fmt(MAX_CYCLE_QUOTE),
                    }
                    save(state)
                    first_fail(state, "enter_short_ceiling",
                               "short entry filled %s USDT above the %s USDT "
                               "ceiling; ordered reduce-only close contains "
                               "it" % (fmt(short_quote), fmt(MAX_CYCLE_QUOTE)))
                if short_filled != hedge:
                    state.partial_short = short_filled
                    first_fail(state, "enter_short", "short filled %s of %s" % (
                        fmt(short_filled), fmt(hedge)))

    if state.outcomes[ENTER_SHORT] == "REJECTED":
        res, order, fills = settle_leg(state, UNWIND_SPOT, plan, hedge)
        if res == "unknown":
            state.outcomes[UNWIND_SPOT] = "UNKNOWN"
            conclude(state, plan, hedge)
        if res == "rejected":
            state.outcomes[UNWIND_SPOT] = "REJECTED"
            conclude(state, plan, hedge)
        sold = d(order.get("executedQty"))
        if sold <= 0:
            state.receipt["legs"][-1]["note"] = "terminal zero fill"
            save(state)
            state.outcomes[UNWIND_SPOT] = "REJECTED"
            conclude(state, plan, hedge)
        sold_leg_result(state, UNWIND_SPOT, order, fills, net_base, hedge,
                        market)
        state.outcomes[UNWIND_SPOT] = "FILLED"
        conclude(state, plan, hedge)

    res, order, close_coverage = settle_leg(
        state, EXIT_SHORT, plan, short_filled)
    if res == "unknown":
        state.outcomes[EXIT_SHORT] = "UNKNOWN"
        conclude(state, plan, hedge)
    if res == "rejected":
        state.outcomes[EXIT_SHORT] = "REJECTED"
        conclude(state, plan, hedge)
    closed = d(order.get("executedQty"))
    if closed <= 0:
        state.receipt["legs"][-1]["note"] = "terminal zero fill"
        save(state)
        state.outcomes[EXIT_SHORT] = "REJECTED"
        conclude(state, plan, hedge)
    if close_coverage is None or not close_coverage.complete:
        finish(state, "UNKNOWN",
               "short close is terminal but complete Futures fill and fee "
               "truth is unavailable",
               "spot sale was not sent; reconcile the exact close client id")
    if closed < short_filled:
        first_fail(state, "exit_short", "closed %s of short %s" % (
            fmt(closed), fmt(short_filled)))
        finish(state, "OPEN_EXPOSURE",
               "reduce-only close executed %s of short %s; short remains open "
               "with spot %s %s held" % (fmt(closed), fmt(short_filled),
                                         fmt(hedge), base_asset),
               "partial reduce-only close; executor reports and stops; "
               "no repair trade")
    state.outcomes[EXIT_SHORT] = "FILLED"

    res, order, fills = settle_leg(state, EXIT_SPOT, plan, hedge)
    if res == "unknown":
        state.outcomes[EXIT_SPOT] = "UNKNOWN"
        conclude(state, plan, hedge)
    if res == "rejected":
        state.outcomes[EXIT_SPOT] = "REJECTED"
        conclude(state, plan, hedge)
    sold = d(order.get("executedQty"))
    if sold <= 0:
        state.receipt["legs"][-1]["note"] = "terminal zero fill"
        save(state)
        state.outcomes[EXIT_SPOT] = "REJECTED"
        conclude(state, plan, hedge)
    sold_leg_result(state, EXIT_SPOT, order, fills, net_base, hedge, market)
    state.outcomes[EXIT_SPOT] = "FILLED"

    final = futures.read_preflight(
        state.transport, USDM.base_url, _futures_market(state), None,
        require_flat=False, sleep=state.sleep)
    amount = final.value.position_quantity \
        if final.kind == "ok" and final.value is not None else None
    state.receipt["final_futures_position"] = (
        fmt(amount) if amount is not None else "UNKNOWN")
    save(state)
    if amount is None:
        first_fail(state, "final_position_read", "position read failed")
        finish(state, "UNKNOWN",
               "all legs terminal but the final Futures position is unreadable",
               "nothing resent; reconcile from the receipt client ids")
    if amount != 0:
        finish(state, "OPEN_EXPOSURE",
               "Futures position %s remains after the reduce-only close"
               % fmt(amount),
               "executor reports and stops; no repair trade")
    conclude(state, plan, hedge)


def acquire(state: RunState) -> None:
    receipt_dir = os.path.dirname(state.receipt_path)
    os.makedirs(receipt_dir, mode=0o700, exist_ok=True)
    try:
        descriptor = os.open(state.lock_path,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, state.run_id.encode())
        os.close(descriptor)
        state.locked = True
    except FileExistsError:
        raise Blocked("basis-executor lock exists; another or unreconciled run "
                      "owns the lane") from None
    state.receipt.update({
        "run_id": state.run_id,
        "sha": state.cfg.sha,
        "symbol": state.cfg.symbol,
        "client_ids": {stage: client_id(stage, state.run_id)
                       for stage in ALL_STAGES},
        "hard_ceiling_usdt": fmt(MAX_CYCLE_QUOTE),
        "started_utc": utc(),
        "legs": [],
    })
    save(state)
    start = base_event(state, "run_start")
    start["note"] = contract_note("ceiling %s USDT per leg"
                                  % fmt(MAX_CYCLE_QUOTE), state.client_ids)
    state.events.append(start)
    own_name = os.path.basename(state.receipt_path)
    for name in sorted(os.listdir(receipt_dir)):
        if not name.endswith(".json") or name == own_name:
            continue
        try:
            with open(os.path.join(receipt_dir, name), encoding="utf-8") as f:
                prior = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            first_fail(state, "prior_receipt", type(exc).__name__)
            finish(state, "BLOCKED_UNRESOLVED",
                   "a prior receipt in the receipt directory is unreadable",
                   "reconcile the prior run before any new trade")
        if not isinstance(prior, dict) or "verdict" not in prior:
            finish(state, "BLOCKED_UNRESOLVED",
                   "a prior run in the receipt directory has no verdict; "
                   "any lane blocks",
                   "reconcile its client ids before any new trade")


def read_and_plan(state: RunState):
    market = market_context(state)
    row = gap_row(state, market)
    plan = plan_cycle(row, state.cfg.min_entry_bps, market["shared_step"],
                      market["min_notional"])
    if plan is None:
        raise Refusal(
            "plan refused: entry gap %s bps below min %s bps or size below "
            "minimum notional" % (fmt(row.entry_gap_bps_vs_spot_ask),
                                  fmt(state.cfg.min_entry_bps)))
    if plan.spot_quantity < market["min_qty"]:
        raise Refusal("planned quantity %s below venue minimum quantity %s"
                      % (fmt(plan.spot_quantity), fmt(market["min_qty"])))
    return row, plan, market


def make_state(cfg: ExecutorConfig, request, sleep,
               receipted: bool = True) -> RunState:
    run_id = cfg.run_id or new_run_id()
    receipt_path = os.path.join(cfg.receipt_dir,
                                "basis-executor-%s.json" % run_id)
    return RunState(
        cfg=cfg, request=request, sleep=sleep, run_id=run_id,
        receipt_path=receipt_path if receipted else "",
        lock_path=os.path.join(cfg.receipt_dir, "basis-executor.lock"),
        events_path=cfg.events_path or os.path.join(cfg.receipt_dir,
                                                    "events.jsonl"),
        client_ids=tuple(client_id(stage, run_id) for stage in ALL_STAGES),
    )


def run_once(cfg: ExecutorConfig, request,
             sleep: Callable[[float], None] = time.sleep) -> int:
    state = make_state(cfg, request, sleep)
    try:
        acquire(state)
        try:
            _row, plan, market = read_and_plan(state)
            state.receipt["plan"] = {
                "entry_gap_bps": fmt(plan.entry_gap_bps),
                "exit_gap_bps": fmt(plan.exit_gap_bps),
                "funding_rate": fmt(plan.funding_rate),
                "estimated_quantity": fmt(plan.spot_quantity),
                "spot_quote_cap": fmt(plan.spot_quote_cap),
                "estimated_short_notional": fmt(plan.short_notional),
                "leverage": plan.leverage,
                "shared_step": fmt(market["shared_step"]),
                "spot_step": fmt(market["spot_step"]),
                "min_notional": fmt(market["min_notional"]),
                "min_quantity": fmt(market["min_qty"]),
                "reason": plan.reason,
            }
            save(state)
        except Refusal as refusal:
            finish(state, "REJECTED_BEFORE_ENTRY", "none", str(refusal))
        try:
            signed_preflight(state, plan, market)
        except PreflightRefusal as refusal:
            finish(state, "PREFLIGHT_REFUSED", "none", str(refusal))
        execute_legs(state, plan, market)
        raise AssertionError("execute_legs must conclude every path")
    except Blocked as blocked:
        print("lane: BASIS-EXECUTOR")
        print("VERDICT: BLOCKED")
        print("NOTE:", clean(blocked, 300))
        return 3
    except ExecutorExit as done:
        return done.code


def plan_only(cfg: ExecutorConfig, request,
              sleep: Callable[[float], None] = time.sleep) -> int:
    state = make_state(cfg, request, sleep, receipted=False)
    try:
        _row, plan, market = read_and_plan(state)
    except Refusal as refusal:
        print("REFUSED | %s | %s" % (cfg.symbol, clean(refusal, 300)))
        return 2
    print("PLAN | %s | est qty %s | spot spend cap %s USDT (quoteOrderQty) | "
          "est short notional %s USDT | shared step %s | leverage %sx | %s" % (
              plan.symbol, fmt(plan.spot_quantity), fmt(plan.spot_quote_cap),
              fmt(plan.short_notional), fmt(market["shared_step"]),
              plan.leverage, plan.reason))
    return 0


def pending_exposure(receipt_dir: str):
    pattern = os.path.join(receipt_dir, "basis-executor-*.json")
    for path in sorted(glob.glob(pattern), reverse=True):
        try:
            with open(path, encoding="utf-8") as handle:
                receipt = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(receipt, dict) and receipt.get("kind") != "reconcile" \
                and receipt.get("verdict") == "OPEN_EXPOSURE":
            note = str(receipt.get("reconciled_note", ""))
            if not receipt.get("reconciled_by") \
                    or note.startswith("dust_accepted"):
                return path, receipt
    return None, None


def source_symbol_from_events(events_path: str, run_id: str) -> str:
    try:
        with open(events_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("run_id") == run_id \
                        and event.get("symbol"):
                    return str(event["symbol"])
    except OSError:
        pass
    return ""


def mark_source(path: str, receipt: Dict[str, Any], run_id: str,
                how: str, amount: str) -> None:
    receipt = dict(receipt)
    receipt["reconciled_by"] = run_id
    receipt["reconciled_note"] = "%s %s at %s" % (how, amount, utc())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2, default=str)
    os.replace(tmp, path)


def reconcile_once(cfg: ExecutorConfig, request,
                   sleep: Callable[[float], None] = time.sleep) -> int:
    state = make_state(cfg, request, sleep)
    state.receipt["kind"] = "reconcile"
    state.receipt["legs"] = []
    source_path, source = pending_exposure(cfg.receipt_dir)
    lock_exists = os.path.exists(state.lock_path)
    if source is None:
        if lock_exists:
            print("RECONCILE | lock held but no pending exposure receipt | "
                  "human check required")
            return 3
        print("RECONCILE | nothing pending")
        return 0
    source_run = os.path.basename(source_path)[len("basis-executor-"):
                                               -len(".json")]
    symbol = source_symbol_from_events(state.events_path, source_run)
    if not symbol or symbol != cfg.symbol:
        print("RECONCILE | source run %s traded %s, SYMBOL env is %s | "
              "refusing mismatch" % (source_run, symbol or "UNKNOWN",
                                     cfg.symbol))
        return 3
    acquired = source.get("acquired") or {}
    try:
        net_base = d(acquired.get("net_base", "not-a-number"))
    except ArithmeticError:
        net_base = None
    if net_base is None or not net_base.is_finite() or net_base <= 0:
        print("RECONCILE | %s | source receipt has no usable net_base | "
              "human check required" % source_run)
        return 3
    state.receipt["source_run"] = source_run
    save(state)
    try:
        market = market_context(state)
    except Refusal as refusal:
        print("RECONCILE | market context refused: %s" % clean(refusal, 200))
        return 3
    bid = market["spot_market"].bid
    sell_qty = quantize_down(net_base, market["spot_step"])
    if sell_qty <= 0 or sell_qty < market["spot_min_qty"] \
            or sell_qty * bid < market["spot_min_notional"]:
        mark_source(source_path, source, state.run_id, "spot_dust_accepted",
                    fmt(net_base))
        state.receipt.update({"verdict": "RECONCILED_DUST",
                              "residual": fmt(net_base),
                              "finished_utc": utc()})
        save(state)
        if lock_exists:
            os.unlink(state.lock_path)
        print("RECONCILE | %s | residual %s %s below the sellable minimum | "
              "dust accepted | lane freed" % (symbol, fmt(net_base),
                                              market["base"]))
        return 0
    plan_stub = CyclePlan(
        symbol=symbol, entry_gap_bps=Decimal(0), exit_gap_bps=Decimal(0),
        funding_rate=Decimal(0), spot_quantity=sell_qty,
        short_quantity=sell_qty, spot_quote_cap=Decimal(0),
        short_notional=Decimal(0), leverage=1,
        reason="reconcile: ordered sale of recorded exposure")
    outcome, order, _fills = settle_leg(state, EXIT_SPOT, plan_stub, sell_qty)
    if outcome == "terminal":
        executed = d(order.get("executedQty"))
        residual = net_base - executed
        if executed > 0 and residual < market["spot_step"]:
            mark_source(source_path, source, state.run_id, "sold",
                        fmt(executed))
            state.receipt.update({"verdict": "RECONCILED_SOLD",
                                  "sold": fmt(executed),
                                  "residual": fmt(residual),
                                  "finished_utc": utc()})
            save(state)
            write_events(state)
            if lock_exists:
                os.unlink(state.lock_path)
            print("RECONCILE | %s | sold %s %s | residual %s | lane freed" % (
                symbol, fmt(executed), market["base"], fmt(residual)))
            return 0
        state.receipt.update({"verdict": "RECONCILE_PARTIAL",
                              "sold": fmt(executed),
                              "residual": fmt(residual),
                              "finished_utc": utc()})
        save(state)
        write_events(state)
        print("RECONCILE | %s | partial sale %s of %s | residual %s still "
              "sellable | lock kept" % (symbol, fmt(executed), fmt(sell_qty),
                                        fmt(residual)))
        return 3
    if outcome == "rejected":
        state.receipt.update({"verdict": "RECONCILE_REJECTED",
                              "finished_utc": utc()})
        save(state)
        write_events(state)
        print("RECONCILE | %s | sale rejected | lock kept | human check "
              "required" % symbol)
        return 3
    state.receipt.update({"verdict": "UNKNOWN", "finished_utc": utc()})
    save(state)
    write_events(state)
    print("RECONCILE | %s | sale UNKNOWN | query %s before anything | lock "
          "kept" % (symbol, client_id(EXIT_SPOT, state.run_id)))
    return 4


def validate_only(cfg: ExecutorConfig, request,
                  sleep: Callable[[float], None] = time.sleep) -> int:
    def get_only(method, base_url, path, params=None, signed=False):
        if method != "GET":
            raise AssertionError("validate-only attempted a non-GET call")
        return request(method, base_url, path, params, signed)

    state = make_state(cfg, get_only, sleep, receipted=False)
    plan = None
    market = None
    plan_note = ""
    try:
        _row, plan, market = read_and_plan(state)
    except Refusal as refusal:
        plan_note = clean(refusal, 300)
        print("VALIDATE | %s | PLAN_REFUSED | %s" % (cfg.symbol, plan_note))
        print("VALIDATE | %s | signed preflight continues with conservative "
              "margin requirement %s USDT" % (cfg.symbol,
                                              fmt(MAX_CYCLE_QUOTE)))
    if plan is None:
        full_ceiling = MAX_CYCLE_QUOTE.quantize(Decimal("0.00000001"))
        plan = CyclePlan(
            symbol=cfg.symbol, entry_gap_bps=Decimal(0),
            exit_gap_bps=Decimal(0), funding_rate=Decimal(0),
            spot_quantity=Decimal(0), short_quantity=Decimal(0),
            spot_quote_cap=full_ceiling, short_notional=full_ceiling,
            leverage=1,
            reason="validate-only fallback after plan refusal")
    try:
        signed_preflight(state, plan, market)
    except PreflightRefusal as refusal:
        print("VALIDATE | %s | PREFLIGHT_REFUSED | %s" % (cfg.symbol,
                                                          clean(refusal, 300)))
        return 5
    proofs = state.receipt.get("preflight", {})
    print("VALIDATE | %s | PREFLIGHT_OK | leverage %sx | mode ONE_WAY | "
          "available %s USDT | required %s USDT | no orders, no position" % (
              cfg.symbol, proofs.get("symbol_leverage"),
              proofs.get("available_balance"),
              proofs.get("required_short_margin_1x")))
    return 2 if plan_note else 0


def live_request_factory(api_key: str, api_secret: str,
                         timeout: float = HTTP_TIMEOUT):
    if isinstance(timeout, bool) or int(timeout) != timeout:
        raise http.AdapterRefusal("timeout must be a whole number of seconds")
    signed_enabled = bool(api_key and api_secret)
    transport = http.BinanceHttp(api_key or "UNSIGNED_ONLY",
                                 api_secret or "UNSIGNED_ONLY",
                                 timeout_seconds=int(timeout))
    synchronized = set()

    def legacy_result(result: http.HttpResult) -> Tuple[str, Any]:
        if result.kind == "ok":
            return "ok", result.payload
        if result.kind == "api_error":
            return "api_error", {"code": result.code, "msg": result.msg}
        return "unclear", {}

    def request(method: str, base_url: str, path: str,
                params: Optional[Dict[str, str]] = None,
                signed: bool = False) -> Tuple[str, Any]:
        if signed and not signed_enabled:
            return "unclear", {}
        canonical = tuple((str(key), str(value))
                          for key, value in (params or {}).items())
        if signed and base_url not in synchronized:
            time_path = {SPOT.base_url: "/api/v3/time",
                         USDM.base_url: "/fapi/v1/time"}.get(base_url)
            if time_path is None:
                return "unclear", {}
            synced = transport.sync_time(base_url, time_path)
            if synced.kind != "ok":
                return legacy_result(synced)
            synchronized.add(base_url)
        return legacy_result(transport(method, base_url, path, canonical,
                                       signed))
    return request


def config_from_env(env) -> ExecutorConfig:
    symbol = env.get("SYMBOL", "").upper(); sha = env.get("QUANT_SHA", "").lower()
    if not SYMBOL_RE.fullmatch(symbol): raise Blocked("SYMBOL must be 5-20 uppercase letters/digits")
    if not SHA_RE.fullmatch(sha): raise Blocked("QUANT_SHA must be a full 40-character git SHA")
    raw_bps = env.get("MIN_ENTRY_BPS", "1")
    try:
        min_entry_bps = Decimal(raw_bps)
    except ArithmeticError:
        raise Blocked("MIN_ENTRY_BPS is invalid") from None
    if not min_entry_bps.is_finite() or min_entry_bps < 0:
        raise Blocked("MIN_ENTRY_BPS must be finite and non-negative")
    return ExecutorConfig(
        symbol=symbol, sha=sha,
        receipt_dir=os.path.expanduser(env.get("RECEIPT_DIR",
                                               "~/quant_receipts")),
        min_entry_bps=min_entry_bps)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="one-shot basis-cycle executor (no timer, no loop)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan-only", action="store_true",
                      help="unsigned public reads and the plan decision only")
    mode.add_argument("--validate-only", action="store_true",
                      help="complete signed read-only preflight incl. actual "
                           "symbol leverage; zero POSTs, no account change "
                           "(needs keys)")
    mode.add_argument("--execute", action="store_true",
                      help="run the full four-leg cycle once (needs keys)")
    mode.add_argument("--reconcile", action="store_true",
                      help="flatten one recorded OPEN_EXPOSURE through the "
                           "ordered path, then free the lane (needs keys)")
    args = parser.parse_args(argv)
    try:
        cfg = config_from_env(os.environ)
    except Blocked as blocked:
        print("lane: BASIS-EXECUTOR")
        print("VERDICT: BLOCKED")
        print("NOTE:", clean(blocked, 300))
        return 2
    if not (args.execute or args.validate_only or args.reconcile):
        return plan_only(cfg, live_request_factory("", ""))
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        print("lane: BASIS-EXECUTOR")
        print("VERDICT: BLOCKED")
        print("NOTE: missing BINANCE_API_KEY / BINANCE_API_SECRET in environment")
        return 2
    factory = live_request_factory(api_key, api_secret)
    if args.validate_only:
        return validate_only(cfg, factory)
    if args.reconcile:
        return reconcile_once(cfg, factory)
    return run_once(cfg, factory)


if __name__ == "__main__":
    raise SystemExit(main())
