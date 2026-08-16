"""basis_executor.py — one-shot command-line executor for the smallest basis cycle.

Connects the proven pieces and adds only the missing plumbing:
- basis_read supplies the live executable-gap read (build_basis_read_row and
  the public Spot/USD-M endpoints);
- basis_cycle supplies the lifecycle: plan_cycle refuses or proceeds, leg_intent
  and client_id shape every order with unique bc- ids, run_cycle is the verdict
  oracle over the confirmed leg outcomes and the acquired-base seam;
- futures_hand lends the short discipline (query the exact client id after the
  one submission; TERMINAL states; common_step; one mutating call, no retry);
- spot_roundtrip lends the spot discipline (quoteOrderQty-bounded BUY, fills
  and commissions from exchange truth via fees_by_asset, receipt + lock shape);
- events emits EVENT CONTRACT v1 JSONL locally.

Flow when it one day runs live (this wave is OFFLINE ONLY — every test injects
transport; nothing here was run against the network in-lane):
one basis read -> refuse or proceed per plan_cycle -> signed READ-ONLY account
preflight (futures accountConfig canTrade, spot account canTrade, position
mode proven ONE_WAY, no position and no open orders on the plan symbol on
either venue, futures available balance covering the planned short margin at
1x) -> Spot BUY bounded by quoteOrderQty -> confirm ACTUAL net base =
exchange-reported fills minus base-asset fees, floored to the shared step ->
Futures SELL short exactly that quantity -> confirm the actual short fill ->
reduce-only BUY close of the EXACT short fill -> Spot SELL of the actual
remaining base.

Discipline (each rule is a paid scar):
- one submission per leg; order truth is established ONLY by querying that
  leg's client order id; a blind resend double-buys, so nothing is ever resent;
- partial fills are handled from exchange-reported executed quantities;
- UNKNOWN stays UNKNOWN: freeze, report exposure with the exact client id to
  reconcile, exit nonzero; no repair trades ever;
- a JSON receipt is saved before and after every mutation; contract-v1 events
  are appended to a local events.jsonl at the end of the run.

Exit codes: 0 CLOSED | 2 REJECTED_BEFORE_ENTRY or config refusal |
3 OPEN_EXPOSURE / BLOCKED / BLOCKED_UNRESOLVED | 4 UNKNOWN |
5 PREFLIGHT_REFUSED (a signed read-only account proof failed; nothing sent).
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
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
    leg_intent,
    plan_cycle,
    quantize_down,
    run_cycle,
)
from basis_read import (
    SPOT_BASE_URL,
    SPOT_BOOK_PATH,
    USDM_BASE_URL,
    USDM_BOOK_PATH,
    USDM_PREMIUM_PATH,
    BasisReadError,
    build_basis_read_row,
    now_utc,
)
from futures_hand import (
    TERMINAL,
    PreflightError as FuturesPreflightError,
    api_reason,
    clean,
    common_step,
    d,
    fmt,
    parse_trade_capability,
)
from spot_roundtrip import fees_by_asset

ALL_STAGES = (ENTER_SPOT, ENTER_SHORT, EXIT_SHORT, EXIT_SPOT, UNWIND_SPOT)
RECV_WINDOW = "5000"
HTTP_TIMEOUT = 15
QUERY_TRIES = 45          # x QUERY_SLEEP = the live per-order confirmation budget
QUERY_SLEEP = 2.0
MISSING_LIMIT = 8         # consecutive -2013 answers = the order never existed
TRADE_TRIES = 5
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
QTY_QUANTUM_MARGIN = Decimal("0.00000001")


@dataclass(frozen=True)
class Venue:
    name: str
    base_url: str
    exchange_info_path: str
    order_path: str
    trades_path: str
    quote_field: str
    resp_type: str


SPOT = Venue("spot", SPOT_BASE_URL, "/api/v3/exchangeInfo", "/api/v3/order",
             "/api/v3/myTrades", "cummulativeQuoteQty", "FULL")
USDM = Venue("usdm", USDM_BASE_URL, "/fapi/v1/exchangeInfo", "/fapi/v1/order",
             "", "cumQuote", "RESULT")
VENUES = {"spot": SPOT, "usdm": USDM}


class ExecutorExit(Exception):
    """Carries the process exit code out of the one finish() call."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


class Blocked(Exception):
    """A local precondition (config, lock) refuses the run before any receipt."""


class Refusal(Exception):
    """A pre-order refusal inside a receipted run; nothing was submitted."""


class PreflightRefusal(Exception):
    """A signed read-only account proof failed before any order was sent."""


class ReceiptWriteError(OSError):
    """A receipt write could not prove its documented durability boundary."""

    def __init__(self, path: str, phase: str, rename_landed: bool):
        self.path = path
        self.phase = phase
        self.rename_landed = rename_landed
        if rename_landed:
            detail = "rename landed but its directory durability is unproven"
        else:
            detail = "file fsync failed; temp removed; target untouched"
        self.row = "RECEIPT_WRITE | %s | target=%s" % (detail, path)
        super().__init__(self.row)


@dataclass(frozen=True)
class ExecutorConfig:
    symbol: str
    sha: str
    receipt_dir: str
    min_entry_bps: Decimal = Decimal("1")
    events_path: str = ""     # default: <receipt_dir>/events.jsonl
    run_id: str = ""          # default: generated per run


@dataclass
class RunState:
    cfg: ExecutorConfig
    request: Callable[..., Tuple[str, Any]]
    sleep: Callable[[float], None]
    run_id: str
    receipt_path: str
    lock_path: str
    events_path: str
    client_ids: Tuple[str, ...] = ()
    receipt: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    outcomes: Dict[str, str] = field(default_factory=dict)
    locked: bool = False
    partial_short: Optional[Decimal] = None


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_run_id() -> str:
    return format(int(time.time()), "x") + secrets.token_hex(3)


def _save_receipt(path: str, receipt: Dict[str, Any], *, private: bool) -> None:
    """Replace a receipt through a same-directory mode-0600 temp.

    The temp is flushed and file-fsynced before the rename. A file-fsync
    failure raises ReceiptWriteError after removing the temp, with the target
    untouched. The parent directory is fsynced after the rename; a failure
    there raises ReceiptWriteError whose row states that the rename landed but
    its directory durability is unproven. ``private`` remains for caller
    compatibility; every receipt temp is private.
    """
    tmp = path + ".tmp"
    descriptor = os.open(
        tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        os.unlink(tmp)
        raise
    try:
        with handle:
            json.dump(receipt, handle, indent=2, default=str)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError as exc:
                raise ReceiptWriteError(path, "file_fsync", False) from exc
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    parent = os.path.dirname(path) or "."
    try:
        descriptor = os.open(parent, os.O_RDONLY)
    except OSError as exc:
        raise ReceiptWriteError(path, "parent_open", True) from exc
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise ReceiptWriteError(path, "parent_fsync", True) from exc
    finally:
        os.close(descriptor)


def save(state: RunState) -> None:
    if not state.receipt_path:
        return
    _save_receipt(state.receipt_path, state.receipt, private=True)


def first_fail(state: RunState, where: str, detail: Any) -> None:
    if "first_failure" not in state.receipt:
        state.receipt["first_failure"] = {
            "utc": utc(), "where": clean(where, 60), "detail": clean(detail, 200),
        }
        save(state)


def contract_note(text: Any, identifiers: Tuple[str, ...]) -> str:
    """Normalize free text into an EVENT CONTRACT v1 note (a _safe_note fixed
    point): known ids to last-4, then the contract's own token reductions."""
    note = " ".join(str(text).split())
    for identifier in sorted({i for i in identifiers if len(i) > 4},
                             key=len, reverse=True):
        note = note.replace(identifier, identifier[-4:])
    note = events.LONG_ID_RE.sub(lambda match: match.group(0)[-4:], note)
    note = events.OPAQUE_TOKEN_RE.sub(lambda match: match.group(0)[-4:], note)
    return note[:480]


def base_event(state: RunState, kind: str) -> Dict[str, Any]:
    return {"ts": utc(), "sha8": state.cfg.sha[:8].lower(), "kind": kind,
            "run_id": state.run_id, "symbol": state.cfg.symbol}


def write_events(state: RunState) -> None:
    if not state.events:
        return
    try:
        events.write_jsonl(state.events, state.events_path, append=True)
    except (events.ReceiptError, OSError) as exc:
        first_fail(state, "events_contract", exc)
        state.receipt["events_error"] = clean(exc, 200)
        save(state)


def finish(state: RunState, verdict: str, exposure: str, note: str) -> None:
    state.receipt.update({
        "outcomes": dict(state.outcomes),
        "verdict": verdict,
        "exposure": clean(exposure, 300),
        "verdict_note": clean(note, 300),
        "finished_utc": utc(),
    })
    save(state)
    verdict_event = base_event(state, "run_verdict")
    verdict_event["verdict"] = verdict
    residual = state.receipt.get("result", {}).get("residual_base_from_this_run")
    if residual not in (None, "UNKNOWN"):
        verdict_event["residual"] = residual
    text = exposure if exposure and exposure != "none" else note
    if text:
        verdict_event["note"] = contract_note(text, state.client_ids)
    state.events.append(verdict_event)
    write_events(state)
    if state.locked and verdict in ("CLOSED", "REJECTED_BEFORE_ENTRY",
                                    "PREFLIGHT_REFUSED"):
        os.unlink(state.lock_path)
        state.locked = False
    print("lane: BASIS-EXECUTOR")
    print("run_id:", state.run_id)
    print("sha8:", state.cfg.sha[:8])
    print("symbol:", state.cfg.symbol)
    for leg in state.receipt.get("legs", []):
        print("leg: %s %s executed=%s client_last4=%s" % (
            leg.get("stage"), leg.get("status", "-"),
            leg.get("executed_qty", "-"), leg.get("client_id", "")[-4:]))
    if "first_failure" in state.receipt:
        print("first_failure:", json.dumps(state.receipt["first_failure"]))
    print("VERDICT:", verdict)
    print("EXPOSURE:", clean(exposure, 300))
    print("NOTE:", clean(note, 300))
    print("RECEIPT:", state.receipt_path)
    print("EVENTS:", state.events_path)
    raise ExecutorExit(EXIT_CODE[verdict])


def get_retry(state: RunState, base_url: str, path: str,
              params: Optional[Dict[str, str]] = None, signed: bool = False,
              tries: int = 3) -> Tuple[str, Any]:
    """Reads may retry; mutations NEVER go through here."""
    kind, payload = "unclear", {}
    for attempt in range(tries):
        kind, payload = state.request("GET", base_url, path, params, signed)
        if kind != "unclear":
            return kind, payload
        if attempt + 1 < tries:
            state.sleep(1 + attempt)
    return kind, payload


def public_read(state: RunState, base_url: str, path: str, label: str) -> Any:
    kind, payload = get_retry(state, base_url, path,
                              {"symbol": state.cfg.symbol}, signed=False)
    if kind != "ok":
        raise Refusal(f"{label} read failed")
    return payload


def one_symbol(payload: Any, symbol: str, label: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise Refusal(f"{label} returned a non-object response")
    matches = [row for row in payload.get("symbols", [])
               if isinstance(row, dict) and row.get("symbol") == symbol]
    if len(matches) != 1:
        raise Refusal(f"{label} did not return exactly one symbol")
    return matches[0]


def market_context(state: RunState) -> Dict[str, Any]:
    """Read both venues' filters; derive shared and per-venue constraints."""
    symbol = state.cfg.symbol
    spot_info = one_symbol(
        public_read(state, SPOT.base_url, SPOT.exchange_info_path,
                    "Spot exchangeInfo"), symbol, "Spot exchangeInfo")
    usdm_info = one_symbol(
        public_read(state, USDM.base_url, USDM.exchange_info_path,
                    "USD-M exchangeInfo"), symbol, "USD-M exchangeInfo")

    if spot_info.get("status") != "TRADING" or \
            not spot_info.get("isSpotTradingAllowed", False):
        raise Refusal("symbol is not live for Spot trading")
    if "MARKET" not in spot_info.get("orderTypes", []) or \
            not spot_info.get("quoteOrderQtyMarketAllowed", False):
        raise Refusal("MARKET quoteOrderQty orders not enabled for symbol")
    if spot_info.get("quoteAsset") != "USDT":
        raise Refusal("cycle accepts only USDT-quoted Spot symbols")
    if usdm_info.get("status") != "TRADING":
        raise Refusal("USD-M symbol is not TRADING")
    if usdm_info.get("contractType") != "PERPETUAL":
        raise Refusal("USD-M symbol is not a PERPETUAL contract")
    if usdm_info.get("quoteAsset") != "USDT" or \
            usdm_info.get("marginAsset") != "USDT":
        raise Refusal("cycle accepts only USDT-quoted, USDT-margined contracts")
    if "MARKET" not in usdm_info.get("orderTypes", []):
        raise Refusal("USD-M symbol does not support MARKET orders")

    try:
        spot_filters = {row["filterType"]: row
                        for row in spot_info.get("filters", [])}
        usdm_filters = {row["filterType"]: row
                        for row in usdm_info.get("filters", [])}
        spot_steps = [d(spot_filters["LOT_SIZE"]["stepSize"])]
        futures_steps = [d(usdm_filters["LOT_SIZE"]["stepSize"])]
        spot_min_qtys = [d(spot_filters["LOT_SIZE"]["minQty"])]
        futures_min_qtys = [d(usdm_filters["LOT_SIZE"]["minQty"])]
        for filters, venue_steps, venue_min_qtys in (
                (spot_filters, spot_steps, spot_min_qtys),
                (usdm_filters, futures_steps, futures_min_qtys)):
            market_lot = filters.get("MARKET_LOT_SIZE", {})
            if market_lot:
                market_step = d(market_lot["stepSize"])
                market_min_qty = d(market_lot["minQty"])
                if market_step > 0:
                    venue_steps.append(market_step)
                venue_min_qtys.append(market_min_qty)
        spot_notional = spot_filters.get("NOTIONAL") or \
            spot_filters.get("MIN_NOTIONAL") or {}
        usdm_notional = usdm_filters.get("MIN_NOTIONAL", {})
        spot_min_notional = d(spot_notional["minNotional"])
        futures_min_notional = d(usdm_notional["notional"])
        spot_step = spot_steps[0]
        for step in spot_steps[1:]:
            spot_step = common_step(spot_step, step)
        futures_step = futures_steps[0]
        for step in futures_steps[1:]:
            futures_step = common_step(futures_step, step)
        shared_step = common_step(spot_step, futures_step)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise Refusal("symbol filters are unusable on one venue") from None
    filter_values = (
        spot_step, futures_step, spot_min_notional,
        futures_min_notional, *spot_min_qtys, *futures_min_qtys,
    )
    if any(not value.is_finite() for value in filter_values) \
            or shared_step <= 0 or spot_step <= 0 or futures_step <= 0 \
            or spot_min_notional < 0 or futures_min_notional < 0 \
            or min(spot_min_qtys) < 0 or min(futures_min_qtys) < 0:
        raise Refusal("symbol filters contain an invalid venue constraint")
    # Worst-case futures price band for a market order: the exchange reserves
    # margin at roughly notional x the up-multiplier. Missing band data falls
    # back to a conservative 1.5; the notional itself comes from the shared
    # cycle cap through plan_cycle.
    band = usdm_filters.get("PERCENT_PRICE", {})
    band_by_side = usdm_filters.get("PERCENT_PRICE_BY_SIDE", {})
    ups = []
    for source, key in ((band, "multiplierUp"),
                        (band_by_side, "askMultiplierUp"),
                        (band_by_side, "bidMultiplierUp")):
        try:
            value = d(source.get(key, "0"))
        except (ArithmeticError, TypeError, ValueError):
            continue
        if value.is_finite() and value > 1:
            ups.append(value)
    band_up = max(ups) if ups else Decimal("1.5")
    futures_execution_price_ceiling = None
    try:
        raw_ceiling = usdm_filters.get("PRICE_FILTER", {}).get("maxPrice")
        if raw_ceiling not in (None, ""):
            parsed_ceiling = d(raw_ceiling)
            if parsed_ceiling.is_finite() and parsed_ceiling > 0:
                futures_execution_price_ceiling = parsed_ceiling
    except (ArithmeticError, TypeError, ValueError):
        futures_execution_price_ceiling = None
    min_notional = max(spot_min_notional, futures_min_notional)
    min_qty = max(*spot_min_qtys, *futures_min_qtys)
    return {
        "base": spot_info.get("baseAsset", ""),
        "shared_step": shared_step,
        "spot_step": spot_step,
        "futures_step": futures_step,
        "min_notional": min_notional,
        "min_qty": min_qty,
        "band_up": band_up,
        "spot_min_notional": spot_min_notional,
        "spot_min_qty": max(spot_min_qtys),
        "futures_min_notional": futures_min_notional,
        "futures_min_qty": max(futures_min_qtys),
        "futures_execution_price_ceiling":
            futures_execution_price_ceiling,
    }


def gap_row(state: RunState):
    """One basis read through the injected transport; contract of basis_read."""
    symbol = state.cfg.symbol
    spot_book = public_read(state, SPOT.base_url, SPOT_BOOK_PATH, "Spot book")
    usdm_book = public_read(state, USDM.base_url, USDM_BOOK_PATH, "USD-M book")
    premium = public_read(state, USDM.base_url, USDM_PREMIUM_PATH,
                          "USD-M premium")
    try:
        row = build_basis_read_row(symbol, spot_book, usdm_book, premium,
                                   completed_utc=now_utc())
    except BasisReadError as exc:
        raise Refusal(f"basis read refused: {exc}") from None
    state.receipt["gap"] = {
        name: fmt(value) if isinstance(value, Decimal) else value
        for name, value in dataclasses.asdict(row).items()}
    save(state)
    gap_event = base_event(state, "gap_read")
    gap_event["note"] = contract_note(
        "entry %s bps exit %s bps funding %s" % (
            fmt(row.entry_gap_bps_vs_spot_ask),
            fmt(row.exit_gap_bps_vs_spot_bid), fmt(row.funding_rate)),
        state.client_ids)
    state.events.append(gap_event)
    return row


def query_client_order(state: RunState, venue: Venue,
                       order_client_id: str) -> Tuple[str, Any]:
    """Poll one exact client id until terminal, definitely absent, or unknown.

    Mirrors futures_hand.query_order over the injected transport: a definite
    non -2013 query error stops the polling because order truth is NOT
    established; nothing is ever resent from here.
    """
    last, missing = None, 0
    for _ in range(QUERY_TRIES):
        kind, order = state.request(
            "GET", venue.base_url, venue.order_path,
            {"symbol": state.cfg.symbol, "origClientOrderId": order_client_id},
            True)
        if kind == "ok" and isinstance(order, dict):
            last, missing = order, 0
            if order.get("status") in TERMINAL:
                return "terminal", order
        elif kind == "api_error":
            if isinstance(order, dict) and order.get("code") == -2013:
                missing += 1
                if missing >= MISSING_LIMIT:
                    return "not_found", None
            else:
                first_fail(state, "query_order", api_reason(order))
                return "error", order
        state.sleep(QUERY_SLEEP)
    return "pending", last


def _query_order_observation_strength(
        venue: Venue, order: Dict[str, Any]) -> Tuple[int, Decimal, Decimal]:
    """Order nonterminal observations without weakening Decimal truth."""
    status = str(order.get("status", ""))
    if status == "NEW":
        status_rank = 1
    elif status == "PARTIALLY_FILLED":
        status_rank = 2
    elif status in TERMINAL:
        status_rank = 4 if status == "FILLED" else 3
    else:
        status_rank = 0
    try:
        executed = d(order.get("executedQty", "0"))
        quote = d(order.get(venue.quote_field, "0"))
    except (ArithmeticError, TypeError, ValueError):
        executed, quote = Decimal(-1), Decimal(-1)
    if not executed.is_finite():
        executed = Decimal(-1)
    if not quote.is_finite():
        quote = Decimal(-1)
    return status_rank, executed, quote


def _order_id(value: Any) -> Optional[str]:
    """Normalize one exchange order id without inventing a missing identity."""
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value)
    return normalized if normalized else None


def _order_identity(
        state: RunState, order: Any, order_client_id: str,
        expected_order_id: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (order_id, error) for one complete query observation."""
    if not isinstance(order, dict):
        return None, "returned order was not an object"
    if order.get("symbol") != state.cfg.symbol:
        return None, "returned order symbol did not match"
    if order.get("clientOrderId") != order_client_id:
        return None, "returned order client id did not match"
    observed_order_id = _order_id(order.get("orderId"))
    if observed_order_id is None:
        return None, "returned order id was missing"
    if expected_order_id is not None \
            and observed_order_id != expected_order_id:
        return observed_order_id, "returned order id did not match"
    return observed_order_id, None


def query_client_order_strongest(
        state: RunState, venue: Venue, client_id: str,
        *, expected_order_id=None, prior_order=None,
        observation_sink=None) -> Tuple[str, Any]:
    """Poll an exact identity without letting later absence erase a sighting.

    Exact absence requires -2013 on the complete retry envelope and no order
    observation in this or an earlier invocation. Symbol, client id, and the
    submit or first-seen order id are checked on every returned row. Each
    stronger observation reaches the supplied durable sink before another GET.
    """
    bound_order_id = _order_id(expected_order_id)
    if expected_order_id is not None and bound_order_id is None:
        first_fail(state, "query_order", "expected order id was unreadable")
        return "identity_mismatch", prior_order
    strongest = None
    if prior_order is not None and not isinstance(prior_order, dict):
        first_fail(state, "query_order", "prior order was not an object")
        return "identity_mismatch", prior_order
    if isinstance(prior_order, dict):
        observed_order_id, identity_error = _order_identity(
            state, prior_order, client_id, bound_order_id)
        if identity_error is not None:
            first_fail(state, "query_order", identity_error)
            return "identity_mismatch", prior_order
        if bound_order_id is None:
            bound_order_id = observed_order_id
        strongest = dict(prior_order)
        if strongest.get("status") in TERMINAL:
            return "terminal", strongest
    all_missing = strongest is None
    for _ in range(QUERY_TRIES):
        kind, order = state.request(
            "GET", venue.base_url, venue.order_path,
            {"symbol": state.cfg.symbol,
             "origClientOrderId": client_id}, True)
        if kind == "ok" and isinstance(order, dict):
            all_missing = False
            observed_order_id, identity_error = _order_identity(
                state, order, client_id, bound_order_id)
            if identity_error is not None:
                first_fail(state, "query_order", identity_error)
                return "identity_mismatch", order
            if bound_order_id is None:
                bound_order_id = observed_order_id
            if strongest is None or _query_order_observation_strength(
                    venue, order) > _query_order_observation_strength(
                        venue, strongest):
                strongest = dict(order)
                if observation_sink is not None:
                    observation_sink(venue.name, dict(strongest))
            if order.get("status") in TERMINAL:
                return "terminal", order
        elif kind == "api_error" and isinstance(order, dict) \
                and order.get("code") == -2013:
            pass
        elif kind == "api_error":
            all_missing = False
            first_fail(state, "query_order", api_reason(order))
            return "error", order
        else:
            all_missing = False
        state.sleep(QUERY_SLEEP)
    if strongest is not None:
        return "pending", strongest
    if all_missing:
        return "not_found", None
    return "pending", None


def settle_leg(state: RunState, stage: str, plan: CyclePlan,
               quantity: Optional[Decimal] = None, *,
               query_order=query_client_order_strongest,
               observation_sink=None):
    """Submit ONE order for a stage, then establish its truth by client id.

    Returns ("terminal", order, response_fills) | ("rejected", payload, []) |
    ("unknown", last_seen, []). The caller decides; nothing is resent here.
    """
    intent = leg_intent(stage, plan, state.run_id, quantity)
    venue = VENUES[intent.pop("market")]
    wire = dict(intent)
    wire["newOrderRespType"] = venue.resp_type
    order_client_id = wire["newClientOrderId"]
    leg: Dict[str, Any] = {
        "stage": stage, "market": venue.name, "side": wire["side"],
        "client_id": order_client_id, "submitted_utc": utc(),
    }
    if "quantity" in wire:
        leg["quantity"] = wire["quantity"]
    if "quoteOrderQty" in wire:
        leg["quote_order_qty"] = wire["quoteOrderQty"]
    state.receipt["legs"].append(leg)
    save(state)
    submitted = base_event(state, "order_submitted")
    submitted["side"] = wire["side"]
    submitted["client_id_last4"] = order_client_id[-4:]
    state.events.append(submitted)

    kind, response = state.request("POST", venue.base_url, venue.order_path,
                                   dict(wire), True)
    submit_order_id = None
    submit_identity_error = None
    if kind == "ok" and isinstance(response, dict):
        returned_symbol = response.get("symbol")
        returned_client_id = response.get("clientOrderId")
        if returned_symbol is not None and returned_symbol != state.cfg.symbol:
            submit_identity_error = "submit response symbol did not match"
        elif returned_client_id is not None \
                and returned_client_id != order_client_id:
            submit_identity_error = "submit response client id did not match"
        raw_submit_order_id = response.get("orderId")
        if raw_submit_order_id is not None:
            submit_order_id = _order_id(raw_submit_order_id)
            if submit_order_id is None:
                submit_identity_error = "submit response order id was unreadable"
            else:
                leg["submit_order_id"] = submit_order_id
                save(state)
    if submit_identity_error is not None:
        first_fail(state, "submit_order", submit_identity_error)

    role = {
        ENTER_SPOT: "spot",
        ENTER_SHORT: "short",
        UNWIND_SPOT: "unwind",
        EXIT_SHORT: "short_close",
        EXIT_SPOT: "spot_close",
    }.get(stage, stage.lower())

    def persist_observation(_venue_role, observed):
        leg["strongest_order_observation"] = dict(observed)
        leg["confirmed_symbol"] = observed.get("symbol")
        leg["observed_order_id"] = _order_id(observed.get("orderId"))
        save(state)
        if observation_sink is not None:
            observation_sink(role, dict(observed))

    poll_order = query_client_order_strongest if query_order is None \
        else query_order
    if poll_order is query_client_order:
        query_state, order = poll_order(state, venue, order_client_id)
    else:
        query_state, order = poll_order(
            state, venue, order_client_id,
            expected_order_id=submit_order_id, prior_order=None,
            observation_sink=persist_observation)

    if query_state == "terminal":
        if poll_order is query_client_order:
            returned_order_id = _order_id(order.get("orderId")) \
                if isinstance(order, dict) else None
            identity_error = None if isinstance(order, dict) \
                and order.get("clientOrderId") == order_client_id \
                else "returned order client id did not match"
        else:
            returned_order_id, identity_error = _order_identity(
                state, order, order_client_id, submit_order_id)
        if submit_identity_error is not None or identity_error is not None:
            if identity_error is not None:
                first_fail(state, "query_order", identity_error)
            leg.update({"status": "UNKNOWN",
                        "detail": "order identity did not match"})
            save(state)
            return "unknown", order, []
        try:
            executed = d(order.get("executedQty"))
            quote = d(order.get(venue.quote_field))
        except (ArithmeticError, TypeError, ValueError):
            executed, quote = Decimal("NaN"), Decimal("NaN")
        if not executed.is_finite() or not quote.is_finite() \
                or executed < 0 or quote < 0:
            first_fail(state, "query_order",
                       "returned terminal quantities were unreadable")
            leg.update({"status": "UNKNOWN",
                        "detail": "terminal quantities were unreadable"})
            save(state)
            return "unknown", order, []
        order_id = returned_order_id or ""
        leg.update({"status": str(order.get("status")), "order_id": order_id,
                    "executed_qty": fmt(executed), "quote": fmt(quote),
                    "terminal_utc": utc()})
        save(state)
        terminal = base_event(state, "order_terminal")
        terminal["side"] = wire["side"]
        terminal["client_id_last4"] = order_client_id[-4:]
        if order_id:
            terminal["order_id_last4"] = order_id[-4:]
        terminal["executed"] = fmt(executed)
        terminal["quote"] = fmt(quote)
        terminal["note"] = contract_note(
            "%s %s" % (stage, order.get("status")), state.client_ids)
        state.events.append(terminal)
        fills = response.get("fills") if (
            kind == "ok" and isinstance(response, dict)) else None
        return "terminal", order, list(fills or [])

    if query_state == "not_found" and kind == "api_error":
        reason = api_reason(response)
        leg.update({"status": "REJECTED", "reason": clean(reason, 200),
                    "terminal_utc": utc()})
        save(state)
        terminal = base_event(state, "order_terminal")
        terminal["side"] = wire["side"]
        terminal["client_id_last4"] = order_client_id[-4:]
        terminal["executed"] = "0"
        terminal["note"] = contract_note("%s REJECTED %s" % (stage, reason),
                                         state.client_ids)
        state.events.append(terminal)
        return "rejected", response, []

    first_fail(state, stage.lower(),
               "submission %s; query %s" % (kind, query_state))
    leg.update({"status": "UNKNOWN", "submission_kind": kind,
                "query_state": query_state})
    save(state)
    return "unknown", order, []


def spot_trades(state: RunState, order_id: str, executed: Decimal,
                response_fills: List[Dict[str, Any]]):
    """Real fills for one spot order: FULL-response fills first, then myTrades
    until they cover the executed quantity (spot_roundtrip.order_trades)."""
    trades = list(response_fills or [])
    covered = sum((d(t.get("qty", "0")) for t in trades), Decimal(0))
    for _ in range(TRADE_TRIES):
        if covered >= executed:
            break
        state.sleep(1)
        kind, payload = get_retry(
            state, SPOT.base_url, SPOT.trades_path,
            {"symbol": state.cfg.symbol, "orderId": order_id}, signed=True)
        if kind == "ok" and isinstance(payload, list) and payload:
            trades = payload
            covered = sum((d(t.get("qty", "0")) for t in trades), Decimal(0))
    if covered < executed:
        first_fail(state, "order_trades",
                   "trades cover %s of %s" % (fmt(covered), fmt(executed)))
    return trades, covered


def base_fee_of(trades: List[Dict[str, Any]], base_asset: str) -> Decimal:
    return sum((d(t.get("commission", "0")) for t in trades
                if t.get("commissionAsset") == base_asset), Decimal(0))


def attach_fees(state: RunState, leg_index: int, trades) -> None:
    fee_map = fees_by_asset(trades)
    state.receipt["legs"][leg_index]["fee_by_asset"] = fee_map
    save(state)
    if fee_map and state.events and state.events[-1]["kind"] == "order_terminal":
        state.events[-1]["fees"] = fee_map


def futures_position(rows: Any, symbol: str) -> Optional[Decimal]:
    """Net one-way position, or None when the read cannot prove flatness.

    A hedge-side row (positionSide LONG/SHORT) makes attribution unsafe —
    offsetting dual-side positions would sum to zero — so it is treated as
    unreadable, exactly like futures_hand's one-way discipline."""
    if not isinstance(rows, list):
        return None
    total = Decimal(0)
    for row in rows:
        if isinstance(row, dict) and row.get("symbol") == symbol:
            if row.get("positionSide", "BOTH") != "BOTH":
                return None
            try:
                total += d(row.get("positionAmt", "0"))
            except ArithmeticError:
                return None
    return total


def signed_read(state: RunState, base_url: str, path: str,
                params: Optional[Dict[str, str]], label: str) -> Any:
    """One signed read-only GET; anything but 'ok' refuses the preflight."""
    kind, payload = get_retry(state, base_url, path, params, signed=True)
    if kind != "ok":
        reason = api_reason(payload) if kind == "api_error" else kind
        raise PreflightRefusal("%s unavailable: %s" % (label, reason))
    return payload


def symbol_leverage(rows: Any, symbol: str) -> int:
    """The plan symbol's ACTUAL configured leverage from one symbolConfig row."""
    if not isinstance(rows, list) or \
            any(not isinstance(row, dict) for row in rows):
        raise PreflightRefusal("symbol-configuration response is malformed")
    matches = [row for row in rows if row.get("symbol") == symbol]
    if len(matches) != 1:
        raise PreflightRefusal("symbol configuration did not return exactly "
                               "one row for the plan symbol")
    try:
        return int(str(matches[0].get("leverage")))
    except (TypeError, ValueError):
        raise PreflightRefusal(
            "symbol leverage is missing or unreadable") from None


def signed_preflight(state: RunState, plan: CyclePlan,
                     market: Optional[Dict[str, Any]] = None) -> None:
    """Signed READ-ONLY account proofs before ANY order is submitted.

    Must prove, in the futures_hand discipline: futures canTrade=true from
    /fapi/v1/accountConfig alone (the /fapi/v3/account snapshot once returned
    a false negative and is never consulted for capability), spot account
    canTrade, position mode explicitly ONE_WAY (cycle intents send no
    positionSide), the plan symbol's ACTUAL configured leverage exactly 1x
    (owner order 2026-08-13: measured, never assumed; this module never
    changes leverage), no open orders on the plan symbol on either venue, no
    existing futures position on the plan symbol, and futures available
    balance covering the planned short margin at 1x. Any unproven fact raises
    PreflightRefusal; nothing is ever submitted from here.
    """
    symbol = state.cfg.symbol
    capability = signed_read(state, USDM.base_url, USDM_ACCOUNT_CONFIG_PATH,
                             None, "Futures account-configuration read")
    try:
        parse_trade_capability(capability)
    except FuturesPreflightError as exc:
        raise PreflightRefusal(str(exc)) from None

    spot_account = signed_read(state, SPOT.base_url, SPOT_ACCOUNT_PATH, None,
                               "Spot account read")
    can_trade = spot_account.get("canTrade") \
        if isinstance(spot_account, dict) else None
    if not (can_trade is True or str(can_trade).strip().lower() == "true"):
        raise PreflightRefusal("Spot account canTrade is not true")

    mode = signed_read(state, USDM.base_url, USDM_POSITION_MODE_PATH, None,
                       "Futures position-mode read")
    dual = mode.get("dualSidePosition") if isinstance(mode, dict) else None
    if not (dual is False or str(dual).strip().lower() == "false"):
        raise PreflightRefusal("Futures position mode is not proven ONE_WAY; "
                               "cycle intents send no positionSide")

    config_rows = signed_read(state, USDM.base_url, USDM_SYMBOL_CONFIG_PATH,
                              {"symbol": symbol},
                              "Futures symbol-configuration read")
    leverage = symbol_leverage(config_rows, symbol)
    if leverage != plan.leverage:
        raise PreflightRefusal(
            "actual symbol leverage is %sx, not exactly %sx; refusing before "
            "any order (this module never changes leverage)" % (
                leverage, plan.leverage))

    for venue, orders_path in ((SPOT, SPOT_OPEN_ORDERS_PATH),
                               (USDM, USDM_OPEN_ORDERS_PATH)):
        orders = signed_read(state, venue.base_url, orders_path,
                             {"symbol": symbol},
                             "%s open-orders read" % venue.name)
        if not isinstance(orders, list):
            raise PreflightRefusal(
                "%s open-orders response is not a list" % venue.name)
        if orders:
            raise PreflightRefusal(
                "%s open order(s) already standing on %s for the plan "
                "symbol; attribution unsafe" % (len(orders), venue.name))

    rows = signed_read(state, USDM.base_url, USDM_POSITION_PATH,
                       {"symbol": symbol}, "Futures position read")
    amount = futures_position(rows, symbol)
    if amount is None:
        raise PreflightRefusal("Futures position read cannot prove flatness "
                               "on the plan symbol")
    if amount != 0:
        raise PreflightRefusal("existing Futures position %s on the plan "
                               "symbol; attribution unsafe" % fmt(amount))

    account = signed_read(state, USDM.base_url, USDM_ACCOUNT_PATH, None,
                          "Futures balance read")
    try:
        available = d(account.get("availableBalance", "0")) \
            if isinstance(account, dict) else Decimal(0)
    except ArithmeticError:
        raise PreflightRefusal(
            "Futures available balance is unreadable") from None
    band_up = market["band_up"] if market else Decimal(1)
    required = (plan.short_notional * band_up).quantize(QTY_QUANTUM_MARGIN)
    if available < required:
        raise PreflightRefusal(
            "available Futures balance %s is below the banded short margin "
            "%s USDT (notional %s x band %s at %sx) — the exchange reserves "
            "worst-case market-order margin" % (
                fmt(available), fmt(required), fmt(plan.short_notional),
                fmt(band_up), plan.leverage))
    state.receipt["preflight"] = {
        "futures_can_trade": True,
        "spot_can_trade": True,
        "position_mode": "ONE_WAY",
        "symbol_leverage": leverage,
        "open_orders": 0,
        "position_amount": "0",
        "available_balance": fmt(available),
        "required_short_margin_1x": fmt(required),
        "margin_band_up": fmt(band_up),
        "verified_utc": utc(),
    }
    save(state)


def conclude(state: RunState, plan: CyclePlan,
             hedge: Optional[Decimal]) -> None:
    """Feed the confirmed outcomes to the proven lifecycle for the verdict."""
    result = run_cycle(plan, state.run_id, state.outcomes, acquired_base=hedge)
    exposure = result.exposure
    if state.partial_short is not None and exposure != "none":
        exposure += "; short leg actual fill %s" % fmt(state.partial_short)
    finish(state, result.verdict, exposure, result.note)


def sold_leg_result(state: RunState, stage: str, order, fills,
                    net_base: Decimal, hedge: Decimal,
                    market: Dict[str, Any]) -> Decimal:
    """Confirm a spot sale from exchange truth; freeze on uncoverable fees.

    Dust is judged at the SPOT venue's own step, never the shared (LCM) step:
    a shortfall (executed < submitted) whose residual is at or above the spot
    step is a sellable remainder and freezes as OPEN_EXPOSURE with the exact
    quantities. Only a residual below the spot step is dust, and it is still
    recorded in the receipt. Returns the residual base from this run once it
    is exactly computable.
    """
    base_asset = market["base"]
    shared_step = market["shared_step"]
    spot_step = market["spot_step"]
    sold = d(order.get("executedQty", "0"))
    quote_received = d(order.get(SPOT.quote_field, "0"))
    trades, covered = spot_trades(
        state, state.receipt["legs"][-1].get("order_id", ""), sold, fills)
    attach_fees(state, len(state.receipt["legs"]) - 1, trades)
    if covered < sold:
        state.receipt["result"] = {"residual_base_from_this_run": "UNKNOWN"}
        save(state)
        finish(state, "UNKNOWN",
               "%s executed but its trade and fee data never loaded; residual "
               "unknowable" % stage,
               "reconcile the %s client id before trusting balances" % stage)
    sell_base_fee = base_fee_of(trades, base_asset)
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
    """Drive the four-leg cycle; every path ends in finish()/conclude()."""
    base_asset = market["base"]
    shared_step = market["shared_step"]

    # Leg 1 — Spot BUY bounded by quoteOrderQty.
    res, order, fills = settle_leg(state, ENTER_SPOT, plan)
    if res == "rejected":
        state.outcomes[ENTER_SPOT] = "REJECTED"
        conclude(state, plan, None)
    if res == "unknown":
        state.outcomes[ENTER_SPOT] = "UNKNOWN"
        conclude(state, plan, None)
    executed = d(order.get("executedQty", "0"))
    quote_spent = d(order.get(SPOT.quote_field, "0"))
    if executed <= 0:
        state.receipt["legs"][-1]["note"] = "terminal zero fill; nothing acquired"
        save(state)
        state.outcomes[ENTER_SPOT] = "REJECTED"
        conclude(state, plan, None)
    state.outcomes[ENTER_SPOT] = "FILLED"

    # Acquired exchange truth: fills minus base-asset fees, floored to step.
    trades, covered = spot_trades(
        state, state.receipt["legs"][-1].get("order_id", ""), executed, fills)
    attach_fees(state, len(state.receipt["legs"]) - 1, trades)
    if covered < executed:
        state.receipt["acquired"] = {"executed_qty": fmt(executed),
                                     "held_worst_case": fmt(executed)}
        save(state)
        finish(state, "OPEN_EXPOSURE",
               "holding up to %s %s from the spot BUY" % (fmt(executed),
                                                          base_asset),
               "trade and fee data never covered the spot fill; sizing a later "
               "leg on an assumed fee is forbidden; no repair trade")
    buy_base_fee = base_fee_of(trades, base_asset)
    net_base = executed - buy_base_fee
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

    # Leg 2 — Futures SELL short exactly the acquired quantity.
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
        res, order, _ = settle_leg(state, ENTER_SHORT, plan, hedge)
        if res == "unknown":
            state.outcomes[ENTER_SHORT] = "UNKNOWN"
            conclude(state, plan, hedge)
        if res == "rejected":
            state.outcomes[ENTER_SHORT] = "REJECTED"
        else:
            short_filled = d(order.get("executedQty", "0"))
            if short_filled <= 0:
                state.receipt["legs"][-1]["note"] = "terminal zero fill"
                save(state)
                state.outcomes[ENTER_SHORT] = "REJECTED"
                short_filled = None
            else:
                state.outcomes[ENTER_SHORT] = "FILLED"
                # Exchange-reported entry notional vs the compiled ceiling,
                # measured post-fill. A breach is recorded loudly (receipt +
                # first-failure); the flow's own reduce-only close remains
                # the containment — never silent, never a repair trade.
                short_quote = d(order.get(USDM.quote_field, "0"))
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
        # Unwind: Spot SELL of the actual remaining base through the ordered path.
        res, order, fills = settle_leg(state, UNWIND_SPOT, plan, hedge)
        if res == "unknown":
            state.outcomes[UNWIND_SPOT] = "UNKNOWN"
            conclude(state, plan, hedge)
        if res == "rejected":
            state.outcomes[UNWIND_SPOT] = "REJECTED"
            conclude(state, plan, hedge)
        sold = d(order.get("executedQty", "0"))
        if sold <= 0:
            state.receipt["legs"][-1]["note"] = "terminal zero fill"
            save(state)
            state.outcomes[UNWIND_SPOT] = "REJECTED"
            conclude(state, plan, hedge)
        sold_leg_result(state, UNWIND_SPOT, order, fills, net_base, hedge,
                        market)
        state.outcomes[UNWIND_SPOT] = "FILLED"
        conclude(state, plan, hedge)

    # Leg 3 — reduce-only BUY close of the EXACT short fill.
    res, order, _ = settle_leg(state, EXIT_SHORT, plan, short_filled)
    if res == "unknown":
        state.outcomes[EXIT_SHORT] = "UNKNOWN"
        conclude(state, plan, hedge)
    if res == "rejected":
        state.outcomes[EXIT_SHORT] = "REJECTED"
        conclude(state, plan, hedge)
    closed = d(order.get("executedQty", "0"))
    if closed <= 0:
        state.receipt["legs"][-1]["note"] = "terminal zero fill"
        save(state)
        state.outcomes[EXIT_SHORT] = "REJECTED"
        conclude(state, plan, hedge)
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

    # Leg 4 — Spot SELL of the actual remaining base.
    res, order, fills = settle_leg(state, EXIT_SPOT, plan, hedge)
    if res == "unknown":
        state.outcomes[EXIT_SPOT] = "UNKNOWN"
        conclude(state, plan, hedge)
    if res == "rejected":
        state.outcomes[EXIT_SPOT] = "REJECTED"
        conclude(state, plan, hedge)
    sold = d(order.get("executedQty", "0"))
    if sold <= 0:
        state.receipt["legs"][-1]["note"] = "terminal zero fill"
        save(state)
        state.outcomes[EXIT_SPOT] = "REJECTED"
        conclude(state, plan, hedge)
    sold_leg_result(state, EXIT_SPOT, order, fills, net_base, hedge, market)
    state.outcomes[EXIT_SPOT] = "FILLED"

    # CLOSED only on observed exchange state: the short must really be flat.
    kind, rows = get_retry(state, USDM.base_url, USDM_POSITION_PATH,
                           {"symbol": state.cfg.symbol}, signed=True)
    amount = futures_position(rows, state.cfg.symbol) if kind == "ok" else None
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
    """Single-runner lock + prior-receipt gate, then the receipt skeleton."""
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
    # Any verdict-less *.json in the receipt directory blocks, whatever lane
    # wrote it (spot_roundtrip's own discipline): an unresolved run in any
    # lane means unreconciled ids may exist against the same account.
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
    """Reads + plan; raises Refusal when the gap, size, or filters refuse."""
    market = market_context(state)
    row = gap_row(state)
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
    """The one-shot cycle: lock, read, plan, signed read-only preflight,
    four legs, receipt, events, exit."""
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
    """Unsigned public reads and the plan decision only; no receipt, no orders."""
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
    """Newest own OPEN_EXPOSURE receipt not yet reconciled: (path, receipt)."""
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
            # Legacy "dust_accepted" marks used cross-venue minimums and may
            # have written off spot-sellable coins; reopen those once. The
            # corrected mark, "spot_dust_accepted", is terminal.
            if not receipt.get("reconciled_by") \
                    or note.startswith("dust_accepted"):
                return path, receipt
    return None, None


def source_symbol_from_events(events_path: str, run_id: str) -> str:
    """The symbol a recorded run traded, from its own event lines."""
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
    _save_receipt(path, receipt, private=False)


def reconcile_once(cfg: ExecutorConfig, request,
                   sleep: Callable[[float], None] = time.sleep) -> int:
    """Flatten ONE recorded OPEN_EXPOSURE through the ordered path.

    Sells exactly the source receipt's recorded net acquired base, floored to
    the SPOT step, with one order proven by client id — never a repair trade,
    never a retry, never futures. A residual below the sellable minimum is
    recorded as accepted dust. The lane lock is released only when the
    exposure is sold or proven dust; anything unproven keeps the lock and
    exits nonzero for a human. Exit 0 done/none, 3 human check, 4 UNKNOWN.
    """
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
    kind, book = state.request("GET", SPOT.base_url,
                               "/api/v3/ticker/bookTicker",
                               {"symbol": symbol}, False)
    bid = d(book.get("bidPrice", "0")) \
        if kind == "ok" and isinstance(book, dict) else Decimal(0)
    if bid <= 0:
        print("RECONCILE | no usable spot bid | try again later")
        return 3
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
    outcome, order, _fills = settle_leg(
        state, EXIT_SPOT, plan_stub, sell_qty,
        query_order=query_client_order)
    if outcome == "terminal":
        executed = d(order.get("executedQty", "0"))
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
    """The COMPLETE signed read-only preflight with zero POSTs.

    Public reads attempt the plan, then every signed preflight proof runs
    REGARDLESS of the plan outcome (owner order 2026-08-13): a refused plan
    only swaps in a conservative full-ceiling margin requirement, so account
    readiness is always measured. The transport is wrapped so any non-GET
    attempt dies before leaving the process; the trade lock is never taken
    and no receipt file is written. Exit 0 = plan and every proof held;
    exit 5 = preflight refused; exit 2 = account proofs held but the plan
    itself refused.
    """
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
        plan = CyclePlan(
            symbol=cfg.symbol, entry_gap_bps=Decimal(0),
            exit_gap_bps=Decimal(0), funding_rate=Decimal(0),
            spot_quantity=Decimal(0), short_quantity=Decimal(0),
            spot_quote_cap=MAX_CYCLE_QUOTE, short_notional=MAX_CYCLE_QUOTE,
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
    """One transport for both venues, in the futures_hand style: 'ok' = 2xx
    JSON; 'api_error' = a real Binance {code,msg}; 'unclear' proves NOTHING.
    Mutations are submitted exactly once by the caller and never retried."""
    offsets: Dict[str, int] = {}

    def sync(base_url: str) -> None:
        path = "/api/v3/time" if base_url == SPOT_BASE_URL else "/fapi/v1/time"
        kind, payload = request("GET", base_url, path, None, False)
        if kind == "ok" and isinstance(payload, dict) and \
                isinstance(payload.get("serverTime"), int):
            offsets[base_url] = payload["serverTime"] - int(time.time() * 1000)
        else:
            offsets[base_url] = 0

    def request(method: str, base_url: str, path: str,
                params: Optional[Dict[str, str]] = None,
                signed: bool = False) -> Tuple[str, Any]:
        params = dict(params or {})
        if signed:
            if base_url not in offsets:
                sync(base_url)
            params["timestamp"] = str(int(time.time() * 1000)
                                      + offsets.get(base_url, 0))
            params["recvWindow"] = RECV_WINDOW
            query = urllib.parse.urlencode(params)
            signature = hmac.new(api_secret.encode(), query.encode(),
                                 hashlib.sha256).hexdigest()
            query += "&signature=" + signature
        else:
            query = urllib.parse.urlencode(params)
        http_request = urllib.request.Request(
            base_url + path + ("?" + query if query else ""), method=method)
        if api_key:
            http_request.add_header("X-MBX-APIKEY", api_key)
        try:
            with urllib.request.urlopen(http_request,
                                        timeout=timeout) as response:
                return "ok", json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors="replace")
            if error.code >= 500 or error.code in (408, 409):
                return "unclear", {"http": error.code}
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict) and "code" in parsed:
                    return "api_error", {"code": parsed.get("code"),
                                         "msg": clean(parsed.get("msg", ""))}
            except ValueError:
                pass
            return "unclear", {"http": error.code}
        except Exception as error:  # never stringify an exception with the URL
            return "unclear", {"error": type(error).__name__}

    return request


def config_from_env(env) -> ExecutorConfig:
    symbol = env.get("SYMBOL", "").upper()
    sha = env.get("QUANT_SHA", "").lower()
    if not SYMBOL_RE.fullmatch(symbol):
        raise Blocked("SYMBOL must be 5-20 uppercase letters/digits")
    if not SHA_RE.fullmatch(sha):
        raise Blocked("QUANT_SHA must be a full 40-character git SHA")
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
