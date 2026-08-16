#!/usr/bin/env python3
"""Read local mechanics receipts and optionally reconcile signed GET truth."""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from execution import binance_futures, binance_http, binance_spot

WINDOW_PAD_MS = 5000
SYMBOL_RE = re.compile(r"[A-Z0-9]{5,20}")
ASSET_RE = re.compile(r"[A-Z0-9]{1,20}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
ROUNDTRIP_IDENTITY_ISSUE = "roundtrip order intents are not bound"


class ReceiptError(ValueError):
    """A local receipt cannot safely identify one mechanics run."""


@dataclass(frozen=True)
class LegClaim:
    stage: str
    market: str
    side: str
    status: str
    quantity: Optional[Decimal]
    quote: Optional[Decimal]
    order_id: str
    order_id_last4: str
    client_id: str
    fee_by_asset: Optional[Dict[str, Decimal]]


@dataclass(frozen=True)
class ReceiptClaim:
    path: str
    name: str
    kind: str
    symbol: str
    git_sha: Optional[str]
    started_ms: Optional[int]
    finished_ms: Optional[int]
    legs: Tuple[LegClaim, ...]
    receipt_verdict: str
    complete_fee_claim: Optional[Dict[str, Decimal]]
    funding_claim: Optional[Decimal]
    issues: Tuple[str, ...]


@dataclass(frozen=True)
class FillMeasurement:
    leg: LegClaim
    quantity: Optional[Decimal]
    quote: Optional[Decimal]
    fees: Optional[Dict[str, Decimal]]
    fee_usdt: Optional[Decimal]
    realized_pnl: Optional[Decimal]
    trade_ids: Tuple[str, ...]


@dataclass(frozen=True)
class Reconciliation:
    name: str
    verdict: str
    total_cost_usdt: Optional[Decimal]
    rows: Tuple[str, ...]


def fmt(value: Decimal) -> str:
    return format(value, "f")


def _shown(value: Optional[Decimal]) -> str:
    return "UNKNOWN" if value is None else fmt(value)


def _difference(left: Optional[Decimal], right: Optional[Decimal]) -> Optional[Decimal]:
    return left - right if left is not None and right is not None else None


def _delta_verdict(*deltas: Optional[Decimal]) -> str:
    if any(delta is None for delta in deltas):
        return "UNKNOWN"
    return "MISMATCH" if any(delta != 0 for delta in deltas) else "MATCHED"


def _clean(value: Any, limit: int = 120) -> str:
    return " ".join(str(value).split())[:limit]


def _decimal(value: Any, text: bool = False) -> Optional[Decimal]:
    if value is None or isinstance(value, bool) or (text and type(value) is not str):
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _nonnegative(value: Any) -> Optional[Decimal]:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _strict_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ReceiptError("duplicate receipt field")
    return result


def _reject_constant(value: str):
    raise ReceiptError("non-finite JSON number")


def _utc_ms(value: Any) -> Optional[int]:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        return None
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    return ((delta.days * 86400 + delta.seconds) * 1000
            + delta.microseconds // 1000)


def _fee_map(value: Any, issues: List[str], label: str) \
        -> Optional[Dict[str, Decimal]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        issues.append(label + " is not an object")
        return None
    result: Dict[str, Decimal] = {}
    for raw_asset, raw_amount in value.items():
        asset = raw_asset.upper() if isinstance(raw_asset, str) else ""
        amount = _nonnegative(raw_amount)
        if not ASSET_RE.fullmatch(asset) or amount is None:
            issues.append(label + " contains an invalid commission")
            return None
        result[asset] = result.get(asset, Decimal(0)) + amount
    return result


def _claim_owners(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], ...]:
    result = raw.get("result")
    return (raw, result) if isinstance(result, dict) else (raw,)


def _fee_claim(owners: Sequence[Dict[str, Any]], issues: List[str],
               label: str = "") \
        -> Optional[Dict[str, Decimal]]:
    for owner in owners:
        for field in ("fee_by_asset", "commission_by_asset", "commissions"):
            if field in owner:
                issue_label = label + "." + field if label else field
                return _fee_map(owner.get(field), issues, issue_label)
    return None


def _funding_claim(raw: Dict[str, Any], issues: List[str]) -> Optional[Decimal]:
    for owner in _claim_owners(raw):
        for field in ("funding_usdt", "funding"):
            if field in owner:
                value = _decimal(owner.get(field))
                if value is None:
                    issues.append(field + " is unreadable")
                return value
    return None


def _leg_claim(stage: str, market: str, side: str, value: Dict[str, Any],
               quote_value: Any,
               issues: List[str]) -> LegClaim:
    quantity = _nonnegative(value.get("executed_qty", value.get("executedQty")))
    quote = _nonnegative(quote_value)
    if quantity is None:
        issues.append(stage + " quantity is unreadable")
    if quote is None:
        issues.append(stage + " quote is unreadable")
    order_id = value.get("order_id", value.get("orderId", ""))
    last4 = value.get("order_id_last4", "")
    if order_id in (None, "") and last4 in (None, ""):
        issues.append(stage + " order identifier is missing")
    return LegClaim(
        stage, market, side, _clean(value.get("status", "UNKNOWN"), 30).upper(),
        quantity, quote, str(order_id) if order_id not in (None, "") else "",
        str(last4) if last4 not in (None, "") else "",
        value.get("client_id", ""), _fee_claim((value,), issues, stage),
    )


def _futures_leg(stage: str, value: Any, default_side: str,
                 issues: List[str]) -> LegClaim:
    if not isinstance(value, dict):
        issues.append(stage + " leg is missing")
        value = {}
    params = value.get("params")
    params = params if isinstance(params, dict) else {}
    side = str(params.get("side", value.get("side", default_side))).upper()
    if side not in ("BUY", "SELL"):
        issues.append(stage + " side is invalid")
        side = "UNKNOWN"
    quote_value = value.get(
        "cum_quote", value.get("cumQuote", value.get("quote"))
    )
    return _leg_claim(stage, "usdm", side, value, quote_value, issues)


def _parse_futures(raw: Dict[str, Any], issues: List[str]) -> Tuple[LegClaim, ...]:
    hand = str(raw.get("hand", "")).lower()
    open_side = "SELL" if hand == "short" else "BUY"
    close_side = "BUY" if open_side == "SELL" else "SELL"
    if hand not in ("long", "short"):
        issues.append("hand is invalid")
    return (
        _futures_leg("OPEN", raw.get("open"), open_side, issues),
        _futures_leg("CLOSE", raw.get("close"), close_side, issues),
    )


def _parse_basis(raw: Dict[str, Any], issues: List[str]) -> Tuple[LegClaim, ...]:
    raw_legs = raw.get("legs")
    if not isinstance(raw_legs, list) or not raw_legs:
        raise ReceiptError("basis receipt has no legs")
    legs: List[LegClaim] = []
    for index, value in enumerate(raw_legs):
        if not isinstance(value, dict):
            issues.append("basis leg is not an object")
            continue
        stage = _clean(value.get("stage", "LEG_%d" % index), 40).upper()
        market = str(value.get("market", "")).lower()
        if market not in ("spot", "usdm"):
            issues.append(stage + " market is invalid")
            market = "unknown"
        side = str(value.get("side", "")).upper()
        if side not in ("BUY", "SELL"):
            issues.append(stage + " side is invalid")
            side = "UNKNOWN"
        quote_value = value.get("quote", value.get(
            "cum_quote", value.get("cumQuote")
        ))
        legs.append(_leg_claim(stage, market, side, value, quote_value, issues))
    if not legs:
        raise ReceiptError("basis receipt has no readable legs")
    return tuple(legs)


def _roundtrip_intent_is_bound(raw: Dict[str, Any], side: str,
                               client_id: Any) -> Optional[bool]:
    value = raw.get(side)
    intent = value.get("intent") if isinstance(value, dict) else None
    if not isinstance(intent, dict):
        return False if isinstance(value, dict) and "intent" in value else None
    bound_name = "quoteOrderQty" if side == "buy" else "quantity"
    bound_value = raw.get("max_quote") if side == "buy" \
        else value.get("quantity")
    try:
        names, values = zip(*intent.get("params", ()))
    except (TypeError, ValueError):
        return False
    return tuple(intent.get(field) for field in (
        "venue", "operation", "identity", "git_sha"
    )) == ("BINANCE_SPOT", "ORDER", client_id, raw.get("sha")) \
        and names == (
            "symbol", "side", "type", bound_name,
            "newClientOrderId", "newOrderRespType",
        ) \
        and values[:3] + values[4:] == (
            raw.get("symbol"), side.upper(), "MARKET", client_id, "FULL"
        ) \
        and _decimal(values[3], text=True) is not None \
        and _decimal(values[3], text=True) > Decimal(0) \
        and _decimal(values[3], text=True) == _decimal(bound_value, text=True)


def _parse_roundtrip(raw: Dict[str, Any], issues: List[str]) -> Tuple[LegClaim, ...]:
    client_ids = tuple(raw.get(side + "_client_id") for side in ("buy", "sell"))
    bindings = tuple(_roundtrip_intent_is_bound(raw, side, client_id)
                     for side, client_id in zip(("buy", "sell"), client_ids))
    if not all(isinstance(value, str) and value for value in client_ids) \
            or client_ids[0] == client_ids[1] or False in bindings \
            or raw.get("verdict") == "CLOSED" and not all(bindings):
        issues.append(ROUNDTRIP_IDENTITY_ISSUE)
    if isinstance(raw.get("legs"), list):
        return _parse_basis(raw, issues)
    legs = []
    for binding, (stage, field, side, quote_field) in zip(bindings, (
        ("BUY", "buy", "BUY", "quote_spent"),
        ("SELL", "sell", "SELL", "quote_received"),
    )):
        value = raw.get(field)
        value = dict(value) if isinstance(value, dict) else {}
        value["client_id"] = raw.get(field + "_client_id", "") if binding else ""
        legs.append(
            _leg_claim(stage, "spot", side, value, value.get(quote_field), issues)
        )
    return tuple(legs)


def read_receipt(path: str) -> ReceiptClaim:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(
                handle,
                parse_float=Decimal,
                parse_constant=_reject_constant,
                object_pairs_hook=_strict_object,
            )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ReceiptError("receipt is unreadable") from error
    if not isinstance(raw, dict):
        raise ReceiptError("receipt is not an object")
    name = os.path.basename(path)
    issues: List[str] = []
    symbol = raw.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        raise ReceiptError("receipt symbol is invalid")
    git_sha = raw.get("sha")
    if not isinstance(git_sha, str) or not SHA_RE.fullmatch(git_sha):
        issues.append("receipt git sha is unreadable")
        git_sha = None
    parsers = (
        ("futures-hand-", "futures", _parse_futures),
        ("basis-executor-", "basis", _parse_basis),
        ("roundtrip-", "spot", _parse_roundtrip),
    )
    selected = next((item for item in parsers if name.startswith(item[0])), None)
    if selected is None:
        raise ReceiptError("receipt filename is unsupported")
    _prefix, kind, parser = selected
    legs = parser(raw, issues)
    started = _utc_ms(raw.get("started_utc"))
    finished = _utc_ms(raw.get("finished_utc"))
    if started is None or finished is None or finished < started:
        issues.append("receipt time window is unreadable")
    verdict = raw.get("verdict")
    if not isinstance(verdict, str) or not verdict:
        issues.append("receipt verdict is unreadable")
        verdict = "UNKNOWN"
    return ReceiptClaim(
        path, name, kind, symbol, git_sha, started, finished, legs, verdict,
        _fee_claim(_claim_owners(raw), issues), _funding_claim(raw, issues),
        tuple(issues),
    )


def receipt_paths(receipt_dir: str) -> List[str]:
    patterns = ("futures-hand-*.json", "basis-executor-*.json",
                "roundtrip-*.json")
    paths = [path for pattern in patterns
             for path in glob.glob(os.path.join(receipt_dir, pattern))]
    return sorted(path for path in set(paths) if os.path.isfile(path))


def _add_amounts(target: Dict[str, Decimal], values: Dict[str, Decimal]) -> None:
    for asset, amount in values.items():
        target[asset] = target.get(asset, Decimal(0)) + amount


def _claimed_fees(receipt: ReceiptClaim) \
        -> Tuple[Dict[str, Decimal], bool]:
    if receipt.complete_fee_claim is not None:
        return dict(receipt.complete_fee_claim), True
    values: Dict[str, Decimal] = {}
    complete = all(leg.fee_by_asset is not None for leg in receipt.legs)
    for leg in receipt.legs:
        if leg.fee_by_asset is not None:
            _add_amounts(values, leg.fee_by_asset)
    return values, complete


def _claimed_pnl(receipt: ReceiptClaim, market: str) -> Optional[Decimal]:
    result = Decimal(0)
    for leg in receipt.legs:
        if leg.market != market:
            continue
        if leg.quote is None or leg.side not in ("BUY", "SELL"):
            return None
        result += leg.quote if leg.side == "SELL" else -leg.quote
    return result


def offline_reconciliation(receipt: ReceiptClaim) -> Reconciliation:
    rows: List[str] = []
    for leg in receipt.legs:
        rows.append(
            f"{receipt.name} | CLAIMED | FILL | stage={leg.stage} "
            f"market={leg.market} side={leg.side} status={leg.status} "
            f"quantity={_shown(leg.quantity)} quote={_shown(leg.quote)}"
        )
    fees, complete = _claimed_fees(receipt)
    scope = "COMPLETE" if complete else "PARTIAL"
    if fees:
        for asset in sorted(fees):
            rows.append(
                f"{receipt.name} | CLAIMED | COMMISSION | asset={asset} "
                f"amount={fmt(fees[asset])} scope={scope}"
            )
    else:
        rows.append(
            f"{receipt.name} | CLAIMED | COMMISSION | asset=UNKNOWN "
            f"amount=UNKNOWN scope={scope}"
        )
    rows.append(f"{receipt.name} | CLAIMED | REALIZED_PNL | market=usdm "
                f"amount={_shown(_claimed_pnl(receipt, 'usdm'))} asset=USDT")
    rows.append(f"{receipt.name} | CLAIMED | FUNDING | "
                f"amount={_shown(receipt.funding_claim)} asset=USDT")
    rows.append(f"{receipt.name} | CLAIMED | VERDICT | "
                f"receipt={_clean(receipt.receipt_verdict, 40)} "
                "reconciliation=UNKNOWN")
    rows.append(f"{receipt.name} | TOTAL | "
                "measured_round_trip_cost_usdt=UNKNOWN | verdict=UNKNOWN")
    return Reconciliation(receipt.name, "UNKNOWN", None, tuple(rows))


def _matches_trade(fill: binance_http.Fill, leg: LegClaim) -> bool:
    if not isinstance(fill, binance_http.Fill) or fill.side != leg.side:
        return False
    if leg.order_id:
        return str(fill.order_id) == leg.order_id
    return not leg.order_id_last4 \
        or str(fill.order_id).endswith(leg.order_id_last4)


def _base_asset(symbol: str) -> Optional[str]:
    return symbol[:-4] if symbol.endswith("USDT") and len(symbol) > 4 else None


def _measure_leg(leg: LegClaim, symbol: str,
                 payload: Optional[Tuple[binance_http.Fill, ...]],
                 used: set) -> FillMeasurement:
    unknown = FillMeasurement(leg, None, None, None, None, None, ())
    if payload is None:
        return unknown
    indexes = [
        index for index, fill in enumerate(payload)
        if index not in used and _matches_trade(fill, leg)
    ]
    fills = [payload[index] for index in indexes]
    if not fills or len({fill.order_id for fill in fills}) != 1:
        return unknown
    used.update(indexes)
    quantity = Decimal(0)
    quote = Decimal(0)
    realized = Decimal(0)
    fees: Dict[str, Decimal] = {}
    fee_usdt = Decimal(0)
    unvalued_fee = False
    trade_ids: List[str] = []
    seen_ids = set()
    base_asset = _base_asset(symbol)
    for fill in fills:
        qty = _nonnegative(fill.quantity)
        price = _nonnegative(fill.price)
        one_quote = _nonnegative(fill.quote_quantity)
        commission = _nonnegative(fill.fee.amount)
        asset = fill.fee.asset
        asset = asset.upper() if isinstance(asset, str) else ""
        one_realized = Decimal(0) if leg.market == "spot" \
            else _decimal(fill.realized_pnl)
        if qty is None or one_quote is None or commission is None \
                or not ASSET_RE.fullmatch(asset) or one_realized is None:
            return unknown
        trade_id = "" if fill.trade_id is None else str(fill.trade_id)
        if (leg.market == "usdm" and not trade_id) \
                or trade_id in seen_ids:
            return unknown
        if trade_id:
            seen_ids.add(trade_id)
            trade_ids.append(trade_id)
        quantity += qty
        quote += one_quote
        realized += one_realized
        fees[asset] = fees.get(asset, Decimal(0)) + commission
        if commission != 0:
            if asset == "USDT":
                fee_usdt += commission
            elif asset == base_asset and price is not None:
                fee_usdt += commission * price
            else:
                unvalued_fee = True
    return FillMeasurement(
        leg, quantity, quote, fees, None if unvalued_fee else fee_usdt,
        realized if leg.market == "usdm" else Decimal(0),
        tuple(trade_ids),
    )


def _measure_income(payload: Optional[Tuple[binance_futures.Income, ...]],
                    symbol: str,
                    trade_ids: set) \
        -> Tuple[Optional[Dict[str, Decimal]], Optional[Decimal],
                 Optional[Decimal], bool]:
    unknown = (None, None, None, False)
    if payload is None:
        return unknown
    commissions: Dict[str, Decimal] = {}
    realized = Decimal(0)
    funding = Decimal(0)
    realized_present = False
    for row in payload:
        income_type = row.income_type
        if income_type not in ("COMMISSION", "REALIZED_PNL", "FUNDING_FEE"):
            continue
        trade_id = row.trade_id
        if trade_ids and income_type in ("COMMISSION", "REALIZED_PNL") \
                and trade_id in (None, 0):
            return unknown
        unrelated = row.symbol != symbol
        unrelated = unrelated or trade_ids and trade_id not in (None, 0) \
            and str(trade_id) not in trade_ids
        if unrelated:
            continue
        value = _decimal(row.amount)
        if value is None:
            return unknown
        if income_type == "COMMISSION":
            asset = row.asset
            if not isinstance(asset, str) \
                    or not ASSET_RE.fullmatch(asset.upper()):
                return unknown
            asset = asset.upper()
            commissions[asset] = commissions.get(asset, Decimal(0)) - value
        elif row.asset != "USDT":
            return unknown
        elif income_type == "REALIZED_PNL":
            realized += value
            realized_present = True
        else:
            funding += value
    return commissions, realized, funding, realized_present


def _sum_measure_fees(measurements: Sequence[FillMeasurement],
                      market: Optional[str] = None) \
        -> Optional[Dict[str, Decimal]]:
    result: Dict[str, Decimal] = {}
    for measurement in measurements:
        if market is not None and measurement.leg.market != market:
            continue
        if measurement.fees is None:
            return None
        _add_amounts(result, measurement.fees)
    return result


def _sum_pnl(measurements: Sequence[FillMeasurement], market: str) \
        -> Optional[Decimal]:
    result = Decimal(0)
    for measurement in measurements:
        if measurement.leg.market != market:
            continue
        value = measurement.quote if market == "spot" \
            else measurement.realized_pnl
        if value is None:
            return None
        if market == "spot" and measurement.leg.side == "BUY":
            value = -value
        result += value
    return result


def _covered_fee_totals(measurements: Sequence[FillMeasurement]) \
        -> Tuple[Dict[str, Decimal], Dict[str, Decimal]]:
    claimed: Dict[str, Decimal] = {}
    exchange: Dict[str, Decimal] = {}
    for measurement in measurements:
        if measurement.leg.fee_by_asset is None \
                or measurement.fees is None:
            continue
        _add_amounts(claimed, measurement.leg.fee_by_asset)
        _add_amounts(exchange, measurement.fees)
    return claimed, exchange


def _read_value(reader: Callable[..., binance_http.ReadResult[Any]],
                *args: Any, **kwargs: Any) \
        -> Tuple[str, Optional[Tuple[Any, ...]]]:
    try:
        result = reader(*args, **kwargs)
    except binance_http.AdapterRefusal:
        return "refusal", None
    if isinstance(result, binance_http.ReadResult):
        if result.kind in ("api_error", "unclear"):
            return result.kind, None
        if result.kind == "ok" and isinstance(result.value, tuple):
            return ("ok", result.value) if len(result.value) < 1000 \
                else ("unclear", None)
    return "refusal", None


def _ledger_value(read_kinds: Dict[str, str], ledger: str,
                  reader: Callable[..., binance_http.ReadResult[Any]],
                  *args: Any, **kwargs: Any) \
        -> Optional[Tuple[Any, ...]]:
    kind, payload = _read_value(reader, *args, **kwargs)
    read_kinds[ledger] = kind
    return payload


def _read_spot_order(request: binance_http.Transport, symbol: str,
                     leg: LegClaim) \
        -> Tuple[str, Optional[binance_http.OrderResult]]:
    try:
        order = binance_spot.query_order(
            request, binance_http.SPOT_BASE_URL, symbol, leg.client_id
        )
    except binance_http.AdapterRefusal:
        return "refusal", None
    if not isinstance(order, binance_http.OrderResult) \
            or not isinstance(order.identity, binance_http.OrderIdentity):
        return "refusal", None
    if order.tag != "terminal":
        error = order.error
        kind = error.kind if isinstance(error, binance_http.HttpResult) else "unclear"
        return (kind, None) if kind in ("api_error", "unclear") else ("unclear", None)
    identity = order.identity
    agrees = identity.symbol == symbol and identity.client_id == leg.client_id
    agrees = agrees and order.returned_client_id == leg.client_id
    agrees = agrees and order.side == leg.side
    agrees = agrees and (not leg.order_id or str(identity.order_id) == leg.order_id)
    agrees = agrees and (leg.status == "UNKNOWN" or order.status == leg.status)
    return ("ok", order) if agrees else ("unclear", None)


def _spot_roundtrip_payload(
        receipt: ReceiptClaim,
        request: binance_http.Transport,
        read_kinds: Dict[str, str],
) -> Optional[Tuple[binance_http.Fill, ...]]:
    legs = [leg for leg in receipt.legs if leg.client_id]
    orders = []
    for leg in legs:
        kind, order = _read_spot_order(request, receipt.symbol, leg)
        status = order.status if order is not None else "UNKNOWN"
        ledger = "spot_order_" + leg.side.lower()
        read_kinds[ledger] = f"{kind} status_exchange={status}"
        orders.append(order)
    if any(order is None and (leg.order_id or leg.status != "UNKNOWN")
           for leg, order in zip(legs, orders)):
        return None
    fills = []
    for leg, order in zip(legs, orders):
        if order is None:
            continue
        payload = _ledger_value(
            read_kinds, "spot_trades_" + leg.side.lower(),
            binance_spot.read_my_trades, request,
            binance_http.SPOT_BASE_URL, receipt.symbol,
            order_id=order.identity.order_id,
        )
        measurement = _measure_leg(leg, receipt.symbol, payload, set())
        prior_ids = {str(fill.trade_id) for fill in fills}
        if payload is None \
                or len(measurement.trade_ids) != len(payload) \
                or prior_ids.intersection(measurement.trade_ids) \
                or (measurement.quantity, measurement.quote) != (
                order.executed_quantity, order.quote_quantity):
            return None
        fills.extend(payload)
    return tuple(fills) if fills else None


def signed_reconciliation(receipt: ReceiptClaim,
                          request: binance_http.Transport) -> Reconciliation:
    rows: List[str] = []
    read_kinds: Dict[str, str] = {}
    mismatch = False
    unknown = bool(receipt.issues) or receipt.receipt_verdict != "CLOSED"
    if ROUNDTRIP_IDENTITY_ISSUE in receipt.issues \
            or receipt.started_ms is None or receipt.finished_ms is None:
        spot_payload = None
        usdm_payload = None
        income_payload = None
    else:
        start_ms = max(0, receipt.started_ms - WINDOW_PAD_MS)
        end_ms = receipt.finished_ms + WINDOW_PAD_MS
        spot_payload = ()
        usdm_payload = ()
        income_payload = ()
        if receipt.kind == "spot":
            spot_payload = _spot_roundtrip_payload(
                receipt, request, read_kinds
            )
        elif any(leg.market == "spot" for leg in receipt.legs):
            spot_payload = _ledger_value(
                read_kinds, "spot_trades",
                binance_spot.read_my_trades, request,
                binance_http.SPOT_BASE_URL, receipt.symbol,
                start_ms=start_ms, end_ms=end_ms,
            )
        if any(leg.market == "usdm" for leg in receipt.legs):
            usdm_payload = _ledger_value(
                read_kinds, "usdm_trades",
                binance_futures.read_user_trades, request,
                binance_http.FUTURES_BASE_URL, receipt.symbol,
                start_ms=start_ms, end_ms=end_ms,
            )
            income_payload = _ledger_value(
                read_kinds, "income",
                binance_futures.read_income, request,
                binance_http.FUTURES_BASE_URL, receipt.symbol,
                start_ms, end_ms,
            )

    for ledger, kind in read_kinds.items():
        rows.append(f"{receipt.name} | EXCHANGE | READ | "
                    f"ledger={ledger} kind={kind}")

    used = {"spot": set(), "usdm": set()}
    measurements: List[FillMeasurement] = []
    for leg in receipt.legs:
        payload = spot_payload if leg.market == "spot" else usdm_payload
        measurement = _measure_leg(
            leg, receipt.symbol, payload, used.get(leg.market, set())
        )
        measurements.append(measurement)
        quantity_delta = _difference(measurement.quantity, leg.quantity)
        quote_delta = _difference(measurement.quote, leg.quote)
        fill_verdict = _delta_verdict(quantity_delta, quote_delta)
        unknown = unknown or fill_verdict == "UNKNOWN"
        mismatch = mismatch or fill_verdict == "MISMATCH"
        rows.append(
            f"{receipt.name} | EXCHANGE | FILL | stage={leg.stage} "
            f"market={leg.market} status_claimed={leg.status} "
            f"quantity_claimed={_shown(leg.quantity)} "
            f"quantity_exchange={_shown(measurement.quantity)} "
            f"quantity_delta={_shown(quantity_delta)} "
            f"quote_claimed={_shown(leg.quote)} "
            f"quote_exchange={_shown(measurement.quote)} "
            f"quote_delta={_shown(quote_delta)} verdict={fill_verdict}"
        )

    claimed_fees, claim_complete = _claimed_fees(receipt)
    exchange_fees = _sum_measure_fees(measurements)
    futures_fees = _sum_measure_fees(measurements, "usdm")
    covered_claimed, covered_exchange = _covered_fee_totals(measurements)
    trade_ids = {
        trade_id for measurement in measurements
        if measurement.leg.market == "usdm"
        for trade_id in measurement.trade_ids
    }
    income_commissions, ledger_realized, funding, realized_present = \
        _measure_income(income_payload, receipt.symbol, trade_ids)

    if exchange_fees is None or futures_fees is None \
            or income_commissions is None:
        unknown = True
    income_fee_complete = False
    ledger_fee_mismatch = False
    if futures_fees is not None and income_commissions is not None:
        income_fee_complete = all(
            asset in income_commissions or value == 0
            for asset, value in futures_fees.items()
        )
        ledger_fee_mismatch = any(
            asset in income_commissions and value != income_commissions[asset]
            for asset, value in futures_fees.items()
        )
        ledger_fee_mismatch = ledger_fee_mismatch or any(
            asset not in futures_fees and value != 0
            for asset, value in income_commissions.items()
        )
        unknown = unknown or not income_fee_complete
        mismatch = mismatch or ledger_fee_mismatch

    if receipt.complete_fee_claim is not None and exchange_fees is not None:
        covered_claimed = claimed_fees
        covered_exchange = exchange_fees
    covered_assets = set(covered_claimed) | set(covered_exchange)
    mismatch = mismatch or any(
        covered_exchange.get(asset, Decimal(0))
        != covered_claimed.get(asset, Decimal(0))
        for asset in covered_assets
    )

    assets = sorted(set(claimed_fees) | set(exchange_fees or {})
                    | set(income_commissions or {}))
    if not assets:
        assets = ["UNKNOWN"]
    unvalued_assets = {
        asset for measurement in measurements
        for asset, amount in (measurement.fees or {}).items()
        if amount != 0 and asset not in ("USDT", _base_asset(receipt.symbol))
    }
    for asset in assets:
        claimed = claimed_fees.get(
            asset, Decimal(0) if claim_complete else None
        ) if asset != "UNKNOWN" else None
        exchange = (exchange_fees or {}).get(asset, Decimal(0)) \
            if exchange_fees is not None and asset != "UNKNOWN" else None
        delta = _difference(exchange, claimed) if claim_complete else None
        covered_delta = None
        if asset != "UNKNOWN" and asset in covered_assets:
            covered_delta = _difference(
                covered_exchange.get(asset, Decimal(0)),
                covered_claimed.get(asset, Decimal(0)),
            )
        futures_value = (futures_fees or {}).get(asset, Decimal(0)) \
            if futures_fees is not None and asset != "UNKNOWN" else None
        ledger = None
        if income_commissions is not None and asset != "UNKNOWN":
            ledger = income_commissions.get(
                asset, Decimal(0) if income_fee_complete else None
            )
        ledger_delta = _difference(ledger, futures_value)
        if (delta is not None and delta != 0) \
                or (covered_delta is not None and covered_delta != 0) \
                or (ledger_delta is not None and ledger_delta != 0):
            row_verdict = "MISMATCH"
        elif exchange is None or ledger_delta is None \
                or asset in unvalued_assets:
            row_verdict = "UNKNOWN"
        else:
            row_verdict = "MATCHED"
        rows.append(
            f"{receipt.name} | EXCHANGE | COMMISSION | asset={asset} "
            f"claimed={_shown(claimed)} exchange={_shown(exchange)} "
            f"delta={_shown(delta)} covered_delta={_shown(covered_delta)} "
            f"income={_shown(ledger)} income_delta={_shown(ledger_delta)} "
            f"verdict={row_verdict}"
        )

    claimed_realized = _claimed_pnl(receipt, "usdm")
    exchange_realized = _sum_pnl(measurements, "usdm")
    if exchange_realized is not None and exchange_realized == 0 \
            and not realized_present and ledger_realized is not None:
        ledger_realized = Decimal(0)
    if exchange_realized is not None and exchange_realized != 0 \
            and not realized_present:
        ledger_realized = None
        unknown = True
    realized_delta = _difference(exchange_realized, claimed_realized)
    income_realized_delta = _difference(ledger_realized, exchange_realized)
    realized_verdict = _delta_verdict(realized_delta, income_realized_delta)
    unknown = unknown or realized_verdict == "UNKNOWN"
    mismatch = mismatch or realized_verdict == "MISMATCH"
    rows.append(
        f"{receipt.name} | EXCHANGE | REALIZED_PNL | market=usdm "
        f"claimed={_shown(claimed_realized)} "
        f"exchange={_shown(exchange_realized)} delta={_shown(realized_delta)} "
        f"income={_shown(ledger_realized)} "
        f"income_delta={_shown(income_realized_delta)} "
        f"verdict={realized_verdict}"
    )

    funding_delta = _difference(funding, receipt.funding_claim)
    if funding is None:
        funding_verdict = "UNKNOWN"
        unknown = True
    elif funding_delta is not None and funding_delta != 0:
        funding_verdict = "MISMATCH"
        mismatch = True
    else:
        funding_verdict = "MATCHED"
    rows.append(
        f"{receipt.name} | EXCHANGE | FUNDING | "
        f"claimed={_shown(receipt.funding_claim)} exchange={_shown(funding)} "
        f"delta={_shown(funding_delta)} asset=USDT verdict={funding_verdict}"
    )

    fee_usdt: Optional[Decimal] = Decimal(0)
    for measurement in measurements:
        if measurement.fee_usdt is None:
            fee_usdt = None
            unknown = True
            break
        fee_usdt += measurement.fee_usdt
    spot_pnl = _sum_pnl(measurements, "spot")
    total = None
    if spot_pnl is not None and exchange_realized is not None \
            and funding is not None and fee_usdt is not None \
            and income_fee_complete and not ledger_fee_mismatch \
            and not unknown and income_realized_delta == 0:
        total = -(spot_pnl + exchange_realized + funding) + fee_usdt

    verdict = "MISMATCH" if mismatch else "UNKNOWN" if unknown else "MATCHED"
    claimed_verdict = _clean(receipt.receipt_verdict, 40)
    rows.append(f"{receipt.name} | EXCHANGE | VERDICT | "
                f"claimed={claimed_verdict} reconciliation={verdict}")
    rows.append(f"{receipt.name} | TOTAL | "
                f"measured_round_trip_cost_usdt={_shown(total)} | "
                f"verdict={verdict}")
    return Reconciliation(receipt.name, verdict, total, tuple(rows))


def unreadable_reconciliation(path: str) -> Reconciliation:
    name = _clean(os.path.basename(path), 120)
    rows = (
        "%s | CLAIMED | VERDICT | receipt=UNKNOWN "
        "reconciliation=UNKNOWN note=unreadable_receipt" % name,
        "%s | TOTAL | measured_round_trip_cost_usdt=UNKNOWN | "
        "verdict=UNKNOWN" % name,
    )
    return Reconciliation(name, "UNKNOWN", None, rows)


def reconcile_directory(receipt_dir: str, signed: bool = False,
                        request: Optional[binance_http.Transport] = None) \
        -> List[Reconciliation]:
    results: List[Reconciliation] = []
    for path in receipt_paths(receipt_dir):
        try:
            receipt = read_receipt(path)
        except ReceiptError:
            results.append(unreadable_reconciliation(path))
            continue
        if signed:
            if request is None:
                raise ValueError("signed reconciliation requires a transport")
            results.append(signed_reconciliation(receipt, request))
        else:
            results.append(offline_reconciliation(receipt))
    return results


def run(receipt_dir: str, signed: bool = False,
        request: Optional[binance_http.Transport] = None,
        emit: Callable[[str], None] = print) -> int:
    results = reconcile_directory(receipt_dir, signed=signed, request=request)
    if not results:
        emit("MECHANICS-RECONCILE | NO_RECEIPTS | verdict=UNKNOWN")
        return 2
    for result in results:
        for row in result.rows:
            emit(row)
    if not signed:
        return 0
    verdicts = {result.verdict for result in results}
    return 3 if "MISMATCH" in verdicts else 4 if "UNKNOWN" in verdicts else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="GET-only mechanics receipt reader")
    parser.add_argument("--receipt-dir", required=True)
    parser.add_argument("--signed", action="store_true")
    args = parser.parse_args(argv)
    request = None
    if args.signed:
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        if not api_key or not api_secret:
            print("MECHANICS-RECONCILE | BLOCKED | missing "
                  "BINANCE_API_KEY / BINANCE_API_SECRET")
            return 2
        request = binance_http.BinanceHttp(
            api_key, api_secret, timeout_seconds=binance_http.HTTP_TIMEOUT_SECONDS
        )
        request.sync_time(binance_http.SPOT_BASE_URL, "/api/v3/time")
        request.sync_time(binance_http.FUTURES_BASE_URL, "/fapi/v1/time")
    return run(args.receipt_dir, signed=args.signed, request=request)


if __name__ == "__main__":
    raise SystemExit(main())
