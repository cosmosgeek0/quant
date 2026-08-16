#!/usr/bin/env python3
"""Read mechanics receipts and reconcile them against signed exchange truth.

Without ``--signed`` this module only reads local receipt JSON.  Signed mode
uses shared GET-only readers for Spot trades, USD-M trades, and the USD-M
income ledger.  It never owns an order or account mutation.
"""

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
    fee_by_asset: Optional[Dict[str, Decimal]]


@dataclass(frozen=True)
class ReceiptClaim:
    path: str
    name: str
    kind: str
    symbol: str
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
class IncomeMeasurement:
    commissions: Optional[Dict[str, Decimal]]
    realized_pnl: Optional[Decimal]
    funding: Optional[Decimal]
    commission_present: bool
    realized_present: bool


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


def _clean(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value).split())
    return text[:limit]


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
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
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError("duplicate receipt field")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ReceiptError("non-finite JSON number")


def _utc_ms(value: Any) -> Optional[int]:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed - epoch
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


def _leg_fees(value: Dict[str, Any], issues: List[str], label: str) \
        -> Optional[Dict[str, Decimal]]:
    for field in ("fee_by_asset", "commission_by_asset", "commissions"):
        if field in value:
            return _fee_map(value.get(field), issues, label + "." + field)
    return None


def _complete_fee_claim(raw: Dict[str, Any], issues: List[str]) \
        -> Optional[Dict[str, Decimal]]:
    candidates = [
        (raw, "fee_by_asset"),
        (raw, "commission_by_asset"),
        (raw, "commissions"),
    ]
    result = raw.get("result")
    if isinstance(result, dict):
        candidates.extend([
            (result, "fee_by_asset"),
            (result, "commission_by_asset"),
            (result, "commissions"),
        ])
    for owner, field in candidates:
        if field in owner:
            return _fee_map(owner.get(field), issues, field)
    return None


def _funding_claim(raw: Dict[str, Any], issues: List[str]) -> Optional[Decimal]:
    owners = [raw]
    if isinstance(raw.get("result"), dict):
        owners.append(raw["result"])
    for owner in owners:
        for field in ("funding_usdt", "funding"):
            if field in owner:
                value = _decimal(owner.get(field))
                if value is None:
                    issues.append(field + " is unreadable")
                return value
    return None


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
    quantity = _nonnegative(value.get("executed_qty", value.get("executedQty")))
    quote = _nonnegative(value.get(
        "cum_quote", value.get("cumQuote", value.get("quote"))
    ))
    if quantity is None:
        issues.append(stage + " quantity is unreadable")
    if quote is None:
        issues.append(stage + " quote is unreadable")
    order_id = value.get("order_id", value.get("orderId", ""))
    last4 = value.get("order_id_last4", "")
    if order_id in (None, "") and last4 in (None, ""):
        issues.append(stage + " order identifier is missing")
    return LegClaim(
        stage=stage,
        market="usdm",
        side=side,
        status=_clean(value.get("status", "UNKNOWN"), 30).upper(),
        quantity=quantity,
        quote=quote,
        order_id=str(order_id) if order_id not in (None, "") else "",
        order_id_last4=str(last4) if last4 not in (None, "") else "",
        fee_by_asset=_leg_fees(value, issues, stage),
    )


def _parse_futures(raw: Dict[str, Any], issues: List[str]) \
        -> Tuple[LegClaim, ...]:
    hand = str(raw.get("hand", "")).lower()
    open_side = "SELL" if hand == "short" else "BUY"
    close_side = "BUY" if open_side == "SELL" else "SELL"
    if hand not in ("long", "short"):
        issues.append("hand is invalid")
    return (
        _futures_leg("OPEN", raw.get("open"), open_side, issues),
        _futures_leg("CLOSE", raw.get("close"), close_side, issues),
    )


def _parse_basis(raw: Dict[str, Any], issues: List[str]) \
        -> Tuple[LegClaim, ...]:
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
        quantity = _nonnegative(value.get(
            "executed_qty", value.get("executedQty")
        ))
        quote = _nonnegative(value.get(
            "quote", value.get("cum_quote", value.get("cumQuote"))
        ))
        if quantity is None:
            issues.append(stage + " quantity is unreadable")
        if quote is None:
            issues.append(stage + " quote is unreadable")
        order_id = value.get("order_id", value.get("orderId", ""))
        last4 = value.get("order_id_last4", "")
        if order_id in (None, "") and last4 in (None, ""):
            issues.append(stage + " order identifier is missing")
        legs.append(LegClaim(
            stage=stage,
            market=market,
            side=side,
            status=_clean(value.get("status", "UNKNOWN"), 30).upper(),
            quantity=quantity,
            quote=quote,
            order_id=str(order_id) if order_id not in (None, "") else "",
            order_id_last4=str(last4) if last4 not in (None, "") else "",
            fee_by_asset=_leg_fees(value, issues, stage),
        ))
    if not legs:
        raise ReceiptError("basis receipt has no readable legs")
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
    if name.startswith("futures-hand-"):
        kind = "futures"
        legs = _parse_futures(raw, issues)
    elif name.startswith("basis-executor-"):
        kind = "basis"
        legs = _parse_basis(raw, issues)
    else:
        raise ReceiptError("receipt filename is unsupported")
    started = _utc_ms(raw.get("started_utc"))
    finished = _utc_ms(raw.get("finished_utc"))
    if started is None or finished is None or finished < started:
        issues.append("receipt time window is unreadable")
    verdict = raw.get("verdict")
    if not isinstance(verdict, str) or not verdict:
        issues.append("receipt verdict is unreadable")
        verdict = "UNKNOWN"
    return ReceiptClaim(
        path=path,
        name=name,
        kind=kind,
        symbol=symbol,
        started_ms=started,
        finished_ms=finished,
        legs=legs,
        receipt_verdict=verdict,
        complete_fee_claim=_complete_fee_claim(raw, issues),
        funding_claim=_funding_claim(raw, issues),
        issues=tuple(issues),
    )


def receipt_paths(receipt_dir: str) -> List[str]:
    paths = []
    for pattern in ("futures-hand-*.json", "basis-executor-*.json"):
        paths.extend(glob.glob(os.path.join(receipt_dir, pattern)))
    return sorted(path for path in set(paths) if os.path.isfile(path))


def _add_amounts(target: Dict[str, Decimal], values: Dict[str, Decimal]) -> None:
    for asset, amount in values.items():
        target[asset] = target.get(asset, Decimal(0)) + amount


def _claimed_fees(receipt: ReceiptClaim) \
        -> Tuple[Dict[str, Decimal], bool]:
    if receipt.complete_fee_claim is not None:
        return dict(receipt.complete_fee_claim), True
    values: Dict[str, Decimal] = {}
    complete = True
    for leg in receipt.legs:
        if leg.fee_by_asset is None:
            complete = False
        else:
            _add_amounts(values, leg.fee_by_asset)
    return values, complete


def _claimed_pnl(receipt: ReceiptClaim, market: str) -> Optional[Decimal]:
    result = Decimal(0)
    found = False
    for leg in receipt.legs:
        if leg.market != market:
            continue
        if leg.quote is None or leg.side not in ("BUY", "SELL"):
            return None
        result += leg.quote if leg.side == "SELL" else -leg.quote
        found = True
    return result if found else Decimal(0)


def offline_reconciliation(receipt: ReceiptClaim) -> Reconciliation:
    rows: List[str] = []
    for leg in receipt.legs:
        rows.append(
            "%s | CLAIMED | FILL | stage=%s market=%s side=%s "
            "status=%s quantity=%s quote=%s" % (
                receipt.name, leg.stage, leg.market, leg.side, leg.status,
                _shown(leg.quantity), _shown(leg.quote),
            )
        )
    fees, complete = _claimed_fees(receipt)
    if fees:
        for asset in sorted(fees):
            rows.append(
                "%s | CLAIMED | COMMISSION | asset=%s amount=%s scope=%s" % (
                    receipt.name, asset, fmt(fees[asset]),
                    "COMPLETE" if complete else "PARTIAL",
                )
            )
    else:
        rows.append(
            "%s | CLAIMED | COMMISSION | asset=UNKNOWN amount=UNKNOWN "
            "scope=%s" % (
                receipt.name, "COMPLETE" if complete else "PARTIAL"
            )
        )
    rows.append(
        "%s | CLAIMED | REALIZED_PNL | market=usdm amount=%s asset=USDT" % (
            receipt.name, _shown(_claimed_pnl(receipt, "usdm"))
        )
    )
    rows.append(
        "%s | CLAIMED | FUNDING | amount=%s asset=USDT" % (
            receipt.name, _shown(receipt.funding_claim)
        )
    )
    rows.append(
        "%s | CLAIMED | VERDICT | receipt=%s reconciliation=UNKNOWN" % (
            receipt.name, _clean(receipt.receipt_verdict, 40)
        )
    )
    rows.append(
        "%s | TOTAL | measured_round_trip_cost_usdt=UNKNOWN | "
        "verdict=UNKNOWN" % receipt.name
    )
    return Reconciliation(receipt.name, "UNKNOWN", None, tuple(rows))


def _matches_trade(fill: binance_http.Fill, leg: LegClaim) -> bool:
    if not isinstance(fill, binance_http.Fill) or fill.side != leg.side:
        return False
    order_id = fill.order_id
    if leg.order_id:
        return str(order_id) == leg.order_id
    if leg.order_id_last4:
        return str(order_id).endswith(leg.order_id_last4)
    return True


def _base_asset(symbol: str) -> Optional[str]:
    if symbol.endswith("USDT") and len(symbol) > 4:
        return symbol[:-4]
    return None


def _measure_leg(leg: LegClaim, symbol: str,
                 payload: Optional[Tuple[binance_http.Fill, ...]],
                 used: set) -> FillMeasurement:
    if payload is None:
        return FillMeasurement(leg, None, None, None, None, None, ())
    indexes = [
        index for index, fill in enumerate(payload)
        if index not in used and _matches_trade(fill, leg)
    ]
    if not indexes:
        return FillMeasurement(leg, None, None, None, None, None, ())
    fills = [payload[index] for index in indexes]
    used.update(indexes)
    if len({fill.order_id for fill in fills}) != 1:
        return FillMeasurement(leg, None, None, None, None, None, ())
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
        if not isinstance(asset, str) or not ASSET_RE.fullmatch(asset.upper()):
            asset = None
        one_realized = Decimal(0) if leg.market == "spot" \
            else _decimal(fill.realized_pnl)
        if qty is None or one_quote is None or commission is None \
                or asset is None or one_realized is None:
            return FillMeasurement(leg, None, None, None, None, None, ())
        trade_id = "" if fill.trade_id is None else str(fill.trade_id)
        if leg.market == "usdm" and not trade_id:
            return FillMeasurement(leg, None, None, None, None, None, ())
        if trade_id:
            if trade_id in seen_ids:
                return FillMeasurement(leg, None, None, None, None, None, ())
            seen_ids.add(trade_id)
            trade_ids.append(trade_id)
        asset = asset.upper()
        quantity += qty
        quote += one_quote
        realized += one_realized
        fees[asset] = fees.get(asset, Decimal(0)) + commission
        if commission == 0:
            pass
        elif asset == "USDT":
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


def _relevant_income(row: binance_futures.Income, symbol: str,
                     trade_ids: set) -> bool:
    if row.symbol != symbol:
        return False
    trade_id = row.trade_id
    if trade_ids and trade_id not in (None, 0):
        return str(trade_id) in trade_ids
    return True


def _measure_income(payload: Optional[Tuple[binance_futures.Income, ...]],
                    symbol: str,
                    trade_ids: set) -> IncomeMeasurement:
    if payload is None:
        return IncomeMeasurement(None, None, None, False, False)
    commissions: Dict[str, Decimal] = {}
    realized = Decimal(0)
    funding = Decimal(0)
    commission_present = False
    realized_present = False
    for row in payload:
        income_type = row.income_type
        if income_type not in ("COMMISSION", "REALIZED_PNL", "FUNDING_FEE"):
            continue
        trade_id = row.trade_id
        if trade_ids and income_type in ("COMMISSION", "REALIZED_PNL") \
                and trade_id in (None, 0):
            return IncomeMeasurement(None, None, None, False, False)
        if not _relevant_income(row, symbol, trade_ids):
            continue
        value = _decimal(row.amount)
        if value is None:
            return IncomeMeasurement(None, None, None, False, False)
        if income_type == "COMMISSION":
            asset = row.asset
            if not isinstance(asset, str) \
                    or not ASSET_RE.fullmatch(asset.upper()):
                return IncomeMeasurement(None, None, None, False, False)
            asset = asset.upper()
            commissions[asset] = commissions.get(asset, Decimal(0)) - value
            commission_present = True
        elif income_type == "REALIZED_PNL":
            if row.asset != "USDT":
                return IncomeMeasurement(None, None, None, False, False)
            realized += value
            realized_present = True
        else:
            if row.asset != "USDT":
                return IncomeMeasurement(None, None, None, False, False)
            funding += value
    return IncomeMeasurement(
        commissions, realized, funding,
        commission_present, realized_present,
    )


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


def _sum_realized(measurements: Sequence[FillMeasurement]) \
        -> Optional[Decimal]:
    result = Decimal(0)
    for measurement in measurements:
        if measurement.leg.market != "usdm":
            continue
        if measurement.realized_pnl is None:
            return None
        result += measurement.realized_pnl
    return result


def _sum_spot_pnl(measurements: Sequence[FillMeasurement]) \
        -> Optional[Decimal]:
    result = Decimal(0)
    for measurement in measurements:
        if measurement.leg.market != "spot":
            continue
        if measurement.quote is None:
            return None
        result += measurement.quote \
            if measurement.leg.side == "SELL" else -measurement.quote
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
                *args: Any, **kwargs: Any) -> Any:
    try:
        result = reader(*args, **kwargs)
    except binance_http.AdapterRefusal:
        return None
    if not isinstance(result, binance_http.ReadResult) or result.kind != "ok":
        return None
    return result.value


def signed_reconciliation(receipt: ReceiptClaim,
                          request: binance_http.Transport) -> Reconciliation:
    rows: List[str] = []
    mismatch = False
    unknown = bool(receipt.issues) or receipt.receipt_verdict != "CLOSED"
    if receipt.started_ms is None or receipt.finished_ms is None:
        spot_payload = None
        usdm_payload = None
        income_payload = None
    else:
        start_ms = max(0, receipt.started_ms - WINDOW_PAD_MS)
        end_ms = receipt.finished_ms + WINDOW_PAD_MS
        has_spot = any(leg.market == "spot" for leg in receipt.legs)
        has_usdm = any(leg.market == "usdm" for leg in receipt.legs)
        spot_payload = _read_value(
            binance_spot.read_my_trades, request,
            binance_http.SPOT_BASE_URL, receipt.symbol,
            start_ms=start_ms, end_ms=end_ms,
        ) if has_spot else ()
        usdm_payload = _read_value(
            binance_futures.read_user_trades, request,
            binance_http.FUTURES_BASE_URL, receipt.symbol,
            start_ms=start_ms, end_ms=end_ms,
        ) if has_usdm else ()
        income_payload = _read_value(
            binance_futures.read_income, request,
            binance_http.FUTURES_BASE_URL, receipt.symbol, start_ms, end_ms,
        ) if has_usdm else ()

    used = {"spot": set(), "usdm": set()}
    measurements: List[FillMeasurement] = []
    for leg in receipt.legs:
        payload = spot_payload if leg.market == "spot" else usdm_payload
        measurement = _measure_leg(
            leg, receipt.symbol, payload, used.get(leg.market, set())
        )
        measurements.append(measurement)
        quantity_delta = measurement.quantity - leg.quantity \
            if measurement.quantity is not None and leg.quantity is not None \
            else None
        quote_delta = measurement.quote - leg.quote \
            if measurement.quote is not None and leg.quote is not None \
            else None
        if quantity_delta is None or quote_delta is None:
            fill_verdict = "UNKNOWN"
            unknown = True
        elif quantity_delta != 0 or quote_delta != 0:
            fill_verdict = "MISMATCH"
            mismatch = True
        else:
            fill_verdict = "MATCHED"
        rows.append(
            "%s | EXCHANGE | FILL | stage=%s market=%s "
            "status_claimed=%s quantity_claimed=%s quantity_exchange=%s "
            "quantity_delta=%s "
            "quote_claimed=%s quote_exchange=%s quote_delta=%s "
            "verdict=%s" % (
                receipt.name, leg.stage, leg.market, leg.status,
                _shown(leg.quantity), _shown(measurement.quantity),
                _shown(quantity_delta), _shown(leg.quote),
                _shown(measurement.quote), _shown(quote_delta), fill_verdict,
            )
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
    income = _measure_income(income_payload, receipt.symbol, trade_ids)

    if exchange_fees is None or futures_fees is None \
            or income.commissions is None:
        unknown = True
    ledger_commissions = dict(income.commissions or {})
    income_fee_complete = False
    ledger_fee_mismatch = False
    if futures_fees is not None and income.commissions is not None:
        income_fee_complete = True
        for asset, trade_value in futures_fees.items():
            if asset not in income.commissions:
                if trade_value != 0:
                    unknown = True
                    income_fee_complete = False
                else:
                    ledger_commissions[asset] = Decimal(0)
                continue
            if trade_value != income.commissions[asset]:
                mismatch = True
                ledger_fee_mismatch = True
        for asset, ledger_value in income.commissions.items():
            if asset not in futures_fees and ledger_value != 0:
                mismatch = True
                ledger_fee_mismatch = True

    if receipt.complete_fee_claim is not None and exchange_fees is not None:
        covered_claimed = claimed_fees
        covered_exchange = exchange_fees
    for asset in set(covered_claimed) | set(covered_exchange):
        if covered_exchange.get(asset, Decimal(0)) \
                != covered_claimed.get(asset, Decimal(0)):
            mismatch = True

    assets = sorted(
        set(claimed_fees)
        | set(exchange_fees or {})
        | set(ledger_commissions)
    )
    if not assets:
        assets = ["UNKNOWN"]
    base_asset = _base_asset(receipt.symbol)
    unvalued_assets = {
        asset for measurement in measurements
        for asset, amount in (measurement.fees or {}).items()
        if amount != 0 and asset not in ("USDT", base_asset)
    }
    for asset in assets:
        claimed = claimed_fees.get(
            asset, Decimal(0) if claim_complete else None
        ) if asset != "UNKNOWN" else None
        exchange = (exchange_fees or {}).get(asset, Decimal(0)) \
            if exchange_fees is not None and asset != "UNKNOWN" else None
        delta = exchange - claimed \
            if claim_complete and exchange is not None and claimed is not None \
            else None
        covered_delta = None
        if asset != "UNKNOWN" and asset in set(covered_claimed) \
                | set(covered_exchange):
            covered_delta = covered_exchange.get(asset, Decimal(0)) \
                - covered_claimed.get(asset, Decimal(0))
        futures_value = (futures_fees or {}).get(asset, Decimal(0)) \
            if futures_fees is not None and asset != "UNKNOWN" else None
        ledger = None
        if income.commissions is not None and asset != "UNKNOWN":
            ledger = ledger_commissions.get(
                asset, Decimal(0) if income_fee_complete else None
            )
        ledger_delta = ledger - futures_value \
            if ledger is not None and futures_value is not None else None
        row_mismatch = (delta is not None and delta != 0) \
            or (covered_delta is not None and covered_delta != 0) \
            or (ledger_delta is not None and ledger_delta != 0)
        if row_mismatch:
            row_verdict = "MISMATCH"
        elif exchange is None or ledger_delta is None \
                or asset in unvalued_assets:
            row_verdict = "UNKNOWN"
        else:
            row_verdict = "MATCHED"
        rows.append(
            "%s | EXCHANGE | COMMISSION | asset=%s claimed=%s exchange=%s "
            "delta=%s covered_delta=%s income=%s income_delta=%s "
            "verdict=%s" % (
                receipt.name, asset, _shown(claimed), _shown(exchange),
                _shown(delta), _shown(covered_delta), _shown(ledger),
                _shown(ledger_delta), row_verdict,
            )
        )

    claimed_realized = _claimed_pnl(receipt, "usdm")
    exchange_realized = _sum_realized(measurements)
    ledger_realized = income.realized_pnl
    if exchange_realized is not None and exchange_realized == 0 \
            and not income.realized_present and ledger_realized is not None:
        ledger_realized = Decimal(0)
    if exchange_realized is not None and exchange_realized != 0 \
            and not income.realized_present:
        ledger_realized = None
        unknown = True
    realized_delta = exchange_realized - claimed_realized \
        if exchange_realized is not None and claimed_realized is not None \
        else None
    income_realized_delta = ledger_realized - exchange_realized \
        if ledger_realized is not None and exchange_realized is not None \
        else None
    if realized_delta is None or income_realized_delta is None:
        realized_verdict = "UNKNOWN"
        unknown = True
    elif realized_delta != 0 or income_realized_delta != 0:
        realized_verdict = "MISMATCH"
        mismatch = True
    else:
        realized_verdict = "MATCHED"
    rows.append(
        "%s | EXCHANGE | REALIZED_PNL | market=usdm claimed=%s "
        "exchange=%s delta=%s income=%s income_delta=%s verdict=%s" % (
            receipt.name, _shown(claimed_realized),
            _shown(exchange_realized), _shown(realized_delta),
            _shown(ledger_realized), _shown(income_realized_delta),
            realized_verdict,
        )
    )

    funding = income.funding
    funding_delta = funding - receipt.funding_claim \
        if funding is not None and receipt.funding_claim is not None else None
    if funding is None:
        funding_verdict = "UNKNOWN"
        unknown = True
    elif funding_delta is not None and funding_delta != 0:
        funding_verdict = "MISMATCH"
        mismatch = True
    else:
        funding_verdict = "MATCHED"
    rows.append(
        "%s | EXCHANGE | FUNDING | claimed=%s exchange=%s delta=%s "
        "asset=USDT verdict=%s" % (
            receipt.name, _shown(receipt.funding_claim), _shown(funding),
            _shown(funding_delta), funding_verdict,
        )
    )

    fee_usdt: Optional[Decimal] = Decimal(0)
    for measurement in measurements:
        if measurement.fee_usdt is None:
            fee_usdt = None
            unknown = True
            break
        fee_usdt += measurement.fee_usdt
    spot_pnl = _sum_spot_pnl(measurements)
    ledger_consistent = income_fee_complete \
        and not ledger_fee_mismatch and income_realized_delta == 0
    total = None
    if spot_pnl is not None and exchange_realized is not None \
            and funding is not None and fee_usdt is not None \
            and ledger_consistent:
        total = -(spot_pnl + exchange_realized + funding) + fee_usdt

    verdict = "MISMATCH" if mismatch else "UNKNOWN" if unknown else "MATCHED"
    rows.append(
        "%s | EXCHANGE | VERDICT | claimed=%s reconciliation=%s" % (
            receipt.name, _clean(receipt.receipt_verdict, 40), verdict
        )
    )
    rows.append(
        "%s | TOTAL | measured_round_trip_cost_usdt=%s | verdict=%s" % (
            receipt.name, _shown(total), verdict
        )
    )
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
    if any(result.verdict == "MISMATCH" for result in results):
        return 3
    if any(result.verdict == "UNKNOWN" for result in results):
        return 4
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="GET-only mechanics receipt reconciliation reader"
    )
    parser.add_argument("--receipt-dir", required=True)
    parser.add_argument("--signed", action="store_true")
    args = parser.parse_args(argv)
    request = None
    if args.signed:
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        if not api_key or not api_secret:
            print(
                "MECHANICS-RECONCILE | BLOCKED | missing "
                "BINANCE_API_KEY / BINANCE_API_SECRET"
            )
            return 2
        request = binance_http.BinanceHttp(
            api_key, api_secret,
            timeout_seconds=binance_http.HTTP_TIMEOUT_SECONDS,
        )
        request.sync_time(binance_http.SPOT_BASE_URL, "/api/v3/time")
        request.sync_time(binance_http.FUTURES_BASE_URL, "/fapi/v1/time")
    return run(args.receipt_dir, signed=args.signed, request=request)


if __name__ == "__main__":
    raise SystemExit(main())
