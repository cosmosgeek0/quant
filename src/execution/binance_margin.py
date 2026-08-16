"""Fail-closed Binance Margin adapter, frozen interface rows 068-080."""
from __future__ import annotations

from dataclasses import dataclass as _dataclass
from decimal import Decimal, InvalidOperation, localcontext
import re as _re
import time as _time
from typing import Callable as _Callable, Literal as _Literal

from execution import binance_http as _http
from margin_ops import PRINCIPAL_CEILING as _PRINCIPAL_CEILING

__all__ = ("Liability", "MarginHistoryRow", "MarginOperation", "MarginSubmit", "MarginConfirmation",
           "build_borrow", "build_repay", "read_history_by_direction", "read_history_by_tx_id", "await_history_by_tx_id", "read_liability", "submit_once", "confirm")

_PATH, _ACCOUNT = "/sapi/v1/margin/borrow-repay", "/sapi/v1/margin/account"; _DIRECTIONS, _ORIGINS, _ALLOWED_ASSETS = frozenset(("BORROW", "REPAY")), frozenset(("MANUAL", "AUTO")), frozenset(("USDT",))
_ASSET = _re.compile(r"[A-Z0-9]{1,20}\Z")
_IDENTITY = _re.compile(r"[A-Za-z0-9._:/-]{1,64}\Z")
_SHA = _re.compile(r"[0-9a-fA-F]{40}\Z")
_ZERO = Decimal("0")


@_dataclass(frozen=True)
class Liability:
    asset: str; principal: Decimal; interest: Decimal; total: Decimal

    def __post_init__(self) -> None: _valid_liability(self)


@_dataclass(frozen=True)
class MarginHistoryRow:
    direction: _Literal["BORROW", "REPAY"]
    tx_id: int; asset: str; amount: Decimal; principal: Decimal; interest: Decimal
    status: str; origin_type: _Literal["MANUAL", "AUTO"]; time_ms: int


@_dataclass(frozen=True)
class MarginOperation:
    intent: _http.MutationIntent; direction: _Literal["BORROW", "REPAY"]
    asset: str; amount: Decimal; principal: Decimal; interest: Decimal


@_dataclass(frozen=True)
class MarginSubmit:
    kind: _Literal["accepted", "api_error", "unclear"]
    tx_id: int | None; error: _http.HttpResult | None


@_dataclass(frozen=True)
class MarginConfirmation:
    kind: _Literal["confirmed", "unknown"]
    operation: MarginOperation; tx_id: int; before: Liability; after: Liability; reason: str


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value: raise _http.AdapterRefusal(label + " is required")
    return value


def _asset(value: object, *, allowed: bool = False) -> str:
    value = _text(value, "asset")
    if _ASSET.fullmatch(value) is None or allowed and value not in _ALLOWED_ASSETS: raise _http.AdapterRefusal("asset is outside the allowed set")
    return value


def _money(value: object, *, positive: bool = False, input_only: bool = False) -> Decimal:
    if isinstance(value, bool) or input_only and not isinstance(value, Decimal) or not isinstance(value, (str, Decimal)): raise _http.AdapterRefusal("money value is not exact")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise _http.AdapterRefusal("money value is unreadable") from None
    if not amount.is_finite() or amount < _ZERO or positive and amount <= _ZERO: raise _http.AdapterRefusal("money value is outside its bounds")
    return amount


def _wire(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _sum(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(1, max(left.adjusted(), right.adjusted()) - min(left.as_tuple().exponent, right.as_tuple().exponent) + 2); return left + right


def _error(note: str) -> _http.HttpResult: return _http.HttpResult("unclear", None, None, note)


def _schema(note: str) -> _http.ReadResult: return _http.ReadResult("unclear", None, _error(note))


def _read_error(result: _http.HttpResult) -> _http.ReadResult: return _http.ReadResult(result.kind, None, result)


def _tx_ids(value: object) -> list[object]:
    found = []
    if isinstance(value, dict):
        for name, item in value.items():
            if name in ("txId", "tranId"): found.append(item)
            if isinstance(item, (dict, list, tuple)): found.extend(_tx_ids(item))
    elif isinstance(value, (list, tuple)):
        for item in value: found.extend(_tx_ids(item))
    return found


def _identity(identity: str, git_sha: str) -> None:
    if _IDENTITY.fullmatch(identity or "") is None or _SHA.fullmatch(git_sha or "") is None: raise _http.AdapterRefusal("mutation identity is invalid")


def _valid_liability(value: object) -> Liability:
    if not isinstance(value, Liability): raise _http.AdapterRefusal("liability is required")
    _asset(value.asset); principal = _money(value.principal, input_only=True)
    interest = _money(value.interest, input_only=True); total = _money(value.total, input_only=True)
    if _sum(principal, interest) != total: raise _http.AdapterRefusal("liability total is not exact")
    return value


def _operation(value: object) -> MarginOperation:
    if not isinstance(value, MarginOperation) or value.direction not in _DIRECTIONS or not isinstance(value.intent, _http.MutationIntent): raise _http.AdapterRefusal("margin operation is invalid")
    _asset(value.asset, allowed=True); _identity(value.intent.identity, value.intent.git_sha)
    amount = _money(value.amount, positive=True, input_only=True); principal = _money(value.principal, positive=True, input_only=True); interest = _money(value.interest, input_only=True)
    split_ok = amount == principal and interest == _ZERO if value.direction == "BORROW" else amount == _sum(principal, interest)
    params = (("asset", value.asset), ("amount", _wire(amount)), ("type", value.direction))
    if not split_ok or value.intent != _http.MutationIntent("BINANCE_MARGIN", value.direction, value.intent.identity, value.intent.git_sha, params): raise _http.AdapterRefusal("margin operation facts do not agree")
    return value


def build_borrow(asset: str, principal: Decimal, intent_id: str, git_sha: str) -> MarginOperation:
    asset = _asset(asset, allowed=True); principal = _money(principal, positive=True, input_only=True); _identity(intent_id, git_sha)
    if principal > _PRINCIPAL_CEILING: raise _http.AdapterRefusal("principal exceeds its owned ceiling")
    params = (("asset", asset), ("amount", _wire(principal)), ("type", "BORROW"))
    intent = _http.MutationIntent("BINANCE_MARGIN", "BORROW", intent_id, git_sha, params)
    return MarginOperation(intent, "BORROW", asset, principal, principal, _ZERO)


def build_repay(liability: Liability, intent_id: str, git_sha: str) -> MarginOperation:
    liability = _valid_liability(liability); _asset(liability.asset, allowed=True)
    _identity(intent_id, git_sha); amount = _sum(liability.principal, liability.interest)
    if amount <= _ZERO: raise _http.AdapterRefusal("repay liability is not positive")
    params = (("asset", liability.asset), ("amount", _wire(amount)), ("type", "REPAY"))
    intent = _http.MutationIntent("BINANCE_MARGIN", "REPAY", intent_id, git_sha, params)
    return MarginOperation(intent, "REPAY", liability.asset, amount, liability.principal, liability.interest)


def _history_row(value: object, asset: str, direction: str) -> MarginHistoryRow:
    if not isinstance(value, dict) or "tranId" in value: raise _http.AdapterRefusal("history row is malformed")
    tx_id, when = value.get("txId"), value.get("timestamp")
    if type(tx_id) is not int or tx_id <= 0 or type(when) is not int or when < 0 or value.get("asset") != asset or value.get("type") not in _ORIGINS: raise _http.AdapterRefusal("history identity is not exact")
    status = _text(value.get("status"), "history status")
    return MarginHistoryRow(direction, tx_id, asset, _money(value.get("amount")),
                            _money(value.get("principal")), _money(value.get("interest")), status, value["type"], when)


def _history(transport: _http.Transport, base_url: str, asset: str, direction: str,
             tx_id: int | None, start_ms: int | None, end_ms: int | None,
             sleep: _Callable[[float], None]) -> _http.ReadResult:
    _text(base_url, "base_url"); asset = _asset(asset)
    if direction not in _DIRECTIONS or tx_id is not None and (type(tx_id) is not int or tx_id <= 0):
        raise _http.AdapterRefusal("history selector is invalid")
    if any(value is not None and (type(value) is not int or value < 0)
           for value in (start_ms, end_ms)) or start_ms is not None and end_ms is not None and start_ms > end_ms:
        raise _http.AdapterRefusal("history window is invalid")
    params = [("asset", asset), ("type", direction)]
    if tx_id is not None: params.append(("txId", str(tx_id)))
    if start_ms is not None: params.append(("startTime", str(start_ms)))
    if end_ms is not None: params.append(("endTime", str(end_ms)))
    result = _http.read(transport, base_url, _PATH, tuple(params), signed=True, sleep=sleep)
    if result.kind != "ok": return _read_error(result)
    try:
        if not isinstance(result.payload, dict): raise _http.AdapterRefusal("history is malformed")
        rows, total = result.payload.get("rows"), result.payload.get("total")
        if rows is None and total == 0: rows = []
        if not isinstance(rows, list) or len(rows) > 100 or total is not None and (type(total) is not int or total < len(rows)): raise _http.AdapterRefusal("history is outside its bounds")
        parsed = tuple(_history_row(row, asset, direction) for row in rows)
        if len({row.tx_id for row in parsed}) != len(parsed): raise _http.AdapterRefusal("history identities are not unique")
        if tx_id is None: return _http.ReadResult("ok", parsed, None)
        matches = tuple(row for row in parsed if row.tx_id == tx_id)
        if not parsed: return _http.ReadResult("ok", None, None)
        if len(parsed) != 1 or len(matches) != 1: raise _http.AdapterRefusal("history identity conflicts")
        return _http.ReadResult("ok", matches[0], None)
    except _http.AdapterRefusal:
        return _schema("margin_history_schema")


def read_history_by_direction(transport: _http.Transport, base_url: str, asset: str, direction: _Literal["BORROW", "REPAY"], *, start_ms: int | None = None, end_ms: int | None = None, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[tuple[MarginHistoryRow, ...]]:
    return _history(transport, base_url, asset, direction, None, start_ms, end_ms, sleep)


def read_history_by_tx_id(transport: _http.Transport, base_url: str, asset: str, direction: _Literal["BORROW", "REPAY"], tx_id: int, *, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[MarginHistoryRow]:
    return _history(transport, base_url, asset, direction, tx_id, None, None, sleep)


def await_history_by_tx_id(transport: _http.Transport, base_url: str, asset: str, direction: _Literal["BORROW", "REPAY"], tx_id: int, *, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[MarginHistoryRow]:
    for delay in (0, 1, 2, 4):
        if delay: sleep(delay)
        result = read_history_by_tx_id(transport, base_url, asset, direction, tx_id, sleep=sleep)
        if result.kind != "ok" or result.value is not None: return result
    return _schema("history_not_visible")


def read_liability(transport: _http.Transport, base_url: str, asset: str, *, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[Liability]:
    _text(base_url, "base_url"); asset = _asset(asset)
    result = _http.read(transport, base_url, _ACCOUNT, signed=True, sleep=sleep)
    if result.kind != "ok": return _read_error(result)
    try:
        rows = result.payload.get("userAssets") if isinstance(result.payload, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows): raise _http.AdapterRefusal("liability rows are malformed")
        assets = [_asset(row.get("asset")) for row in rows]
        if len(set(assets)) != len(assets): raise _http.AdapterRefusal("assets are not unique")
        matches = [row for row in rows if row["asset"] == asset]
        if len(matches) != 1: raise _http.AdapterRefusal("liability asset is not unique")
        principal = _money(matches[0].get("borrowed")); interest = _money(matches[0].get("interest"))
        return _http.ReadResult("ok", Liability(asset, principal, interest, _sum(principal, interest)), None)
    except _http.AdapterRefusal:
        return _schema("margin_liability_schema")


def submit_once(transport: _http.Transport, base_url: str, operation: MarginOperation,
                write_intent: _http.IntentWriter) -> MarginSubmit:
    _text(base_url, "base_url"); operation = _operation(operation)
    try: write_intent(operation.intent)
    except _http.IntentNotDurable: raise
    except Exception: raise _http.IntentNotDurable("intent was not durably written") from None
    result = transport("POST", base_url, _PATH, operation.intent.params, True)
    if result.kind != "ok": return MarginSubmit(result.kind, None, result)
    aliases = _tx_ids(result.payload)
    if len(aliases) != 1 or type(aliases[0]) is not int or aliases[0] <= 0:
        error = _error("margin_submit_schema"); return MarginSubmit("unclear", None, error)
    return MarginSubmit("accepted", aliases[0], None)


def confirm(operation: MarginOperation, tx_id: int, history: MarginHistoryRow,
            before: Liability, after: Liability) -> MarginConfirmation:
    def answer(kind: str, reason: str) -> MarginConfirmation: return MarginConfirmation(kind, operation, tx_id, before, after, reason)
    try:
        operation = _operation(operation); _valid_liability(before); _valid_liability(after)
        if type(tx_id) is not int or tx_id <= 0 or not isinstance(history, MarginHistoryRow):
            return answer("unknown", "MARGIN_CONFIRMATION_MISMATCH")
        for value in (history.amount, history.principal, history.interest):
            _money(value, input_only=True)
        facts = history.direction == operation.direction and history.tx_id == tx_id \
            and history.asset == operation.asset == before.asset == after.asset \
            and history.status == "CONFIRMED" and history.origin_type in _ORIGINS \
            and history.amount == operation.amount and history.principal == operation.principal \
            and history.interest == operation.interest
        if operation.direction == "REPAY":
            facts = facts and history.amount == _sum(history.principal, history.interest) \
                and before.principal == operation.principal \
                and before.interest == operation.interest and before.total == operation.amount
            movement = after.principal == after.interest == after.total == _ZERO
        else:
            movement = after.principal == _sum(before.principal, operation.amount) \
                and after.interest >= before.interest
        if not facts: return answer("unknown", "MARGIN_CONFIRMATION_MISMATCH")
        if not movement: return answer("unknown", "MARGIN_LIABILITY_MISMATCH")
        return answer("confirmed", operation.direction + "_CONFIRMED")
    except (AttributeError, TypeError, _http.AdapterRefusal):
        return answer("unknown", "MARGIN_CONFIRMATION_MISMATCH")
