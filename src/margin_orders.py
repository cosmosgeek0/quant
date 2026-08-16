"""Pure cross-Margin MARKET order shapes and response parsing.

The module performs no network, signing, retry, clock, or persistence work.
Callers may pass their existing five-argument transport to the query helper;
only one signed GET for the exact client order id is made.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_DOWN
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple


MARGIN_BASE_URL = "https://api.binance.com"
MARGIN_ORDER_PATH = "/sapi/v1/margin/order"
MARGIN_ORDER_QUERY_PATH = "/sapi/v1/margin/order"
CLIENT_ID_PREFIX = "rmo-"

SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
CLIENT_ID_RE = re.compile(
    r"^" + re.escape(CLIENT_ID_PREFIX) + r"[A-Za-z0-9._:-]{1,32}$"
)
ASSET_RE = re.compile(r"^[A-Z0-9]{1,20}$")
ORDER_SIDES = frozenset(("SELL", "BUY"))
NON_TERMINAL_STATUSES = frozenset(("NEW", "PARTIALLY_FILLED"))
TERMINAL_STATUSES = frozenset((
    "FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH",
))

OrderSide = Literal["SELL", "BUY"]
FillRow = Tuple[Decimal, Decimal, Decimal, str]


@dataclass(frozen=True)
class MarginOrderReady:
    """One validated cross-Margin MARKET order request."""

    symbol: str
    side: OrderSide
    quantity: Decimal
    client_id: str
    params: Dict[str, str]
    verdict: Literal["READY"] = field(default="READY", init=False)
    endpoint_path: str = field(default=MARGIN_ORDER_PATH, init=False)


@dataclass(frozen=True)
class MarginOrderRefused:
    """A local refusal made before any transport is available."""

    reason: str
    fields: Tuple[str, ...]
    verdict: Literal["REFUSED"] = field(default="REFUSED", init=False)


@dataclass(frozen=True)
class MarginFill:
    status: str
    executed_qty: Decimal
    cum_quote: Decimal
    fills: Optional[List[FillRow]]
    order_id: int
    client_id: str


@dataclass(frozen=True)
class MarginOrderError:
    """Typed exchange-truth result that is not a terminal parsed order."""

    status: str
    reason: str
    client_id: str
    fields: Tuple[str, ...] = ()
    verdict: Literal["ERROR"] = field(default="ERROR", init=False)


@dataclass(frozen=True)
class MarginOrderIdentityMismatch(MarginOrderError):
    pass


@dataclass(frozen=True)
class MarginOrderNonTerminal(MarginOrderError):
    pass


@dataclass(frozen=True)
class MarginOrderNotFound(MarginOrderError):
    pass


@dataclass(frozen=True)
class MarginOrderParseError(MarginOrderError):
    pass


@dataclass(frozen=True)
class MarginOrderTransportError(MarginOrderError):
    pass


@dataclass(frozen=True)
class MarginFeeUnknown(MarginOrderError):
    pass


def _refused(reason: str, *fields: str) -> MarginOrderRefused:
    return MarginOrderRefused(reason, tuple(dict.fromkeys(fields)))


def _valid_decimal(value: Any, *, positive: bool) -> bool:
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    return value > Decimal("0") if positive else value >= Decimal("0")


def build_margin_order(
    symbol: str,
    side: Literal["SELL", "BUY"],
    quantity: Decimal,
    client_id: str,
    step: Decimal,
    min_notional: Decimal,
    ref_price: Decimal,
) -> MarginOrderReady | MarginOrderRefused:
    """Validate and shape one quantity-bounded cross-Margin MARKET order."""
    if type(symbol) is not str or SYMBOL_RE.fullmatch(symbol) is None:
        return _refused("BAD_SYMBOL", "symbol")
    if type(side) is not str or side not in ORDER_SIDES:
        return _refused("BAD_SIDE", "side")
    if not _valid_decimal(quantity, positive=True):
        return _refused("BAD_QUANTITY", "quantity")
    if type(client_id) is not str or CLIENT_ID_RE.fullmatch(client_id) is None:
        return _refused("BAD_CLIENT_ID", "client_id")
    if not _valid_decimal(step, positive=True):
        return _refused("BAD_STEP", "step")
    if not _valid_decimal(min_notional, positive=True):
        return _refused("BAD_MIN_NOTIONAL", "min_notional")
    if not _valid_decimal(ref_price, positive=True):
        return _refused("BAD_REF_PRICE", "ref_price")

    try:
        units = (quantity / step).to_integral_value(rounding=ROUND_DOWN)
        floored = units * step
        notional = floored * ref_price
    except DecimalException:
        return _refused("BAD_QUANTITY", "quantity", "step")
    if not floored.is_finite() or floored <= Decimal("0"):
        return _refused("QUANTITY_BELOW_STEP", "quantity", "step")
    if notional < min_notional:
        return _refused(
            "MIN_NOTIONAL_NOT_MET", "quantity", "step", "min_notional",
            "ref_price",
        )

    side_value: OrderSide = side
    params = {
        "symbol": symbol,
        "side": side_value,
        "type": "MARKET",
        "quantity": format(floored, "f"),
        "newClientOrderId": client_id,
        "newOrderRespType": "FULL",
        "sideEffectType": "NO_SIDE_EFFECT",
        "isIsolated": "FALSE",
    }
    return MarginOrderReady(symbol, side_value, floored, client_id, params)


def _error(
    error_type,
    status: str,
    reason: str,
    client_id: str,
    *fields: str,
) -> MarginOrderError:
    return error_type(status, reason, client_id, tuple(dict.fromkeys(fields)))


def _decimal_field(payload: Mapping[str, Any], key: str) -> Decimal:
    value = payload.get(key)
    if type(value) is not str or not value:
        raise ValueError(key)
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(key) from None
    if not parsed.is_finite() or parsed < Decimal("0"):
        raise ValueError(key)
    return parsed


def _parse_fills(payload: Mapping[str, Any]) -> List[FillRow]:
    raw = payload.get("fills")
    if not isinstance(raw, list):
        raise ValueError("fills")
    parsed: List[FillRow] = []
    for index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise ValueError("fills[%d]" % index)
        price = _decimal_field(row, "price")
        quantity = _decimal_field(row, "qty")
        commission = _decimal_field(row, "commission")
        asset = row.get("commissionAsset")
        if price <= Decimal("0") or quantity <= Decimal("0"):
            raise ValueError("fills[%d].price/qty" % index)
        if type(asset) is not str or ASSET_RE.fullmatch(asset) is None:
            raise ValueError("fills[%d].commissionAsset" % index)
        parsed.append((price, quantity, commission, asset))
    return parsed


def _parse_for_identity(
    payload: Any, expected_symbol: str, expected_client_id: str,
    *, require_fills: bool,
) -> MarginFill | MarginOrderError:
    if not isinstance(payload, Mapping):
        return _error(
            MarginOrderParseError, "MALFORMED", "PAYLOAD_NOT_OBJECT",
            expected_client_id, "payload",
        )
    mismatched = []
    if payload.get("symbol") != expected_symbol:
        mismatched.append("symbol")
    if payload.get("clientOrderId") != expected_client_id:
        mismatched.append("clientOrderId")
    if mismatched:
        return _error(
            MarginOrderIdentityMismatch, "IDENTITY_MISMATCH",
            "ORDER_IDENTITY_MISMATCH", expected_client_id, *mismatched,
        )

    status = payload.get("status")
    if status in NON_TERMINAL_STATUSES:
        return _error(
            MarginOrderNonTerminal, status, "NON_TERMINAL",
            expected_client_id, "status",
        )
    if status not in TERMINAL_STATUSES:
        return _error(
            MarginOrderParseError, "MALFORMED", "BAD_STATUS",
            expected_client_id, "status",
        )
    order_id = payload.get("orderId")
    if isinstance(order_id, bool) or not isinstance(order_id, int):
        return _error(
            MarginOrderParseError, "MALFORMED", "BAD_ORDER_ID",
            expected_client_id, "orderId",
        )
    try:
        executed = _decimal_field(payload, "executedQty")
        cum_quote = _decimal_field(payload, "cummulativeQuoteQty")
        fills = _parse_fills(payload) if require_fills \
            or payload.get("fills") is not None else None
    except ValueError as problem:
        return _error(
            MarginOrderParseError, "MALFORMED", "BAD_FILL_DATA",
            expected_client_id, str(problem),
        )
    covered = None if fills is None else sum(
        (row[1] for row in fills), Decimal("0"),
    )
    if covered is not None and executed > Decimal("0") and covered < executed:
        return _error(
            MarginOrderParseError, "MALFORMED", "INCOMPLETE_FILL_DATA",
            expected_client_id, "fills",
        )
    if status == "FILLED" and executed <= Decimal("0"):
        return _error(
            MarginOrderParseError, "MALFORMED", "ZERO_FILLED_QUANTITY",
            expected_client_id, "executedQty",
        )
    return MarginFill(
        status, executed, cum_quote, fills, order_id, expected_client_id,
    )


def parse_margin_order_response(
    payload: dict, expected: MarginOrderReady,
) -> MarginFill | MarginOrderError:
    """Parse one response only after checking both exchange identities."""
    if not isinstance(expected, MarginOrderReady):
        return _error(
            MarginOrderParseError, "MALFORMED", "BAD_EXPECTED_ORDER", "",
            "expected",
        )
    return _parse_for_identity(
        payload, expected.symbol, expected.client_id, require_fills=True,
    )


def query_margin_order(
    transport, symbol: str, client_id: str,
) -> MarginFill | MarginOrderError:
    """Read one exact cross-Margin order id once; never submit or resend."""
    if type(symbol) is not str or SYMBOL_RE.fullmatch(symbol) is None:
        return _error(
            MarginOrderParseError, "MALFORMED", "BAD_SYMBOL", str(client_id),
            "symbol",
        )
    if type(client_id) is not str or CLIENT_ID_RE.fullmatch(client_id) is None:
        return _error(
            MarginOrderParseError, "MALFORMED", "BAD_CLIENT_ID", "",
            "client_id",
        )
    if not callable(transport):
        return _error(
            MarginOrderTransportError, "UNKNOWN", "BAD_TRANSPORT", client_id,
            "transport",
        )
    params = {
        "symbol": symbol,
        "origClientOrderId": client_id,
        "isIsolated": "FALSE",
    }
    try:
        response = transport(
            "GET", MARGIN_BASE_URL, MARGIN_ORDER_QUERY_PATH, params, True,
        )
    except Exception:
        return _error(
            MarginOrderTransportError, "UNKNOWN", "TRANSPORT_EXCEPTION",
            client_id, "transport",
        )
    if not isinstance(response, tuple) or len(response) != 2:
        return _error(
            MarginOrderTransportError, "UNKNOWN", "BAD_TRANSPORT_RESULT",
            client_id, "transport",
        )
    kind, payload = response
    if kind == "ok":
        return _parse_for_identity(
            payload, symbol, client_id, require_fills=False,
        )
    if kind == "api_error" and isinstance(payload, Mapping) \
            and payload.get("code") == -2013:
        return _error(
            MarginOrderNotFound, "NOT_FOUND", "ORDER_NOT_FOUND", client_id,
            "origClientOrderId",
        )
    return _error(
        MarginOrderTransportError, "UNKNOWN", "QUERY_UNRESOLVED", client_id,
        "transport",
    )


def net_base_after_fees(
    fill: MarginFill, base_asset: str,
) -> Decimal | MarginFeeUnknown:
    """Return fee-net base, or typed UNKNOWN when fills were not returned."""
    if not isinstance(fill, MarginFill):
        raise ValueError("fill must be a MarginFill")
    if type(base_asset) is not str or ASSET_RE.fullmatch(base_asset) is None:
        raise ValueError("base_asset is malformed")
    if fill.fills is None:
        return _error(
            MarginFeeUnknown, "UNKNOWN", "FILL_DATA_UNKNOWN",
            fill.client_id, "fills",
        )
    base_fee = sum(
        (commission for _price, _qty, commission, asset in fill.fills
         if asset == base_asset),
        Decimal("0"),
    )
    net = fill.executed_qty - base_fee
    if not net.is_finite() or net < Decimal("0"):
        raise ValueError("base commission exceeds executed quantity")
    return net
