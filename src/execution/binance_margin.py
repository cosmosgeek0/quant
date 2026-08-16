"""Fail-closed Binance Margin adapter, frozen interface rows 068-080."""
from __future__ import annotations

from dataclasses import dataclass as _dataclass
from decimal import Decimal
import re as _re
import time as _time
from typing import Callable as _Callable, Literal as _Literal

from execution import binance_http as _http
from margin_ops import PRINCIPAL_CEILING as _PRINCIPAL_CEILING

__all__ = ("Liability", "MarginHistoryRow", "MarginOperation", "MarginSubmit", "MarginConfirmation", "build_borrow", "build_repay", "read_history_by_direction", "read_history_by_tx_id", "await_history_by_tx_id", "read_liability", "submit_once", "confirm")

_PATH = "/sapi/v1/margin/borrow-repay"
_ACCOUNT = "/sapi/v1/margin/account"
_DIRECTIONS = frozenset(("BORROW", "REPAY"))
_ORIGINS = frozenset(("MANUAL", "AUTO"))
_ALLOWED_ASSETS = frozenset(("USDT",))
_ASSET = _re.compile(r"[A-Z0-9]{1,20}\Z")
_IDENTITY = _re.compile(r"[A-Za-z0-9._:/-]{1,64}\Z")
_SHA = _re.compile(r"[0-9a-fA-F]{40}\Z")
_ZERO = Decimal("0")


@_dataclass(frozen=True)
class Liability:
    asset: str
    principal: Decimal
    interest: Decimal
    total: Decimal

    def __post_init__(self) -> None:
        _valid_liability(self)


@_dataclass(frozen=True)
class MarginHistoryRow:
    direction: _Literal["BORROW", "REPAY"]
    tx_id: int
    asset: str
    amount: Decimal
    principal: Decimal
    interest: Decimal
    status: str
    origin_type: _Literal["MANUAL", "AUTO"]
    time_ms: int


@_dataclass(frozen=True)
class MarginOperation:
    intent: _http.MutationIntent
    direction: _Literal["BORROW", "REPAY"]
    asset: str
    amount: Decimal
    principal: Decimal
    interest: Decimal


@_dataclass(frozen=True)
class MarginSubmit:
    kind: _Literal["accepted", "api_error", "unclear"]
    tx_id: int | None
    error: _http.HttpResult | None


@_dataclass(frozen=True)
class MarginConfirmation:
    kind: _Literal["confirmed", "unknown"]
    operation: MarginOperation
    tx_id: int
    before: Liability
    after: Liability
    reason: str


def _require(fact: bool, note: str) -> None:
    if not fact:
        raise _http.AdapterRefusal(note)


def _text(value: object, label: str) -> str:
    _require(isinstance(value, str) and bool(value), label + " is required")
    return value


def _asset(value: object, *, allowed: bool = False) -> str:
    value = _text(value, "asset")
    _require(_ASSET.fullmatch(value) is not None and (not allowed or value in _ALLOWED_ASSETS), "asset is outside the allowed set")
    return value


def _money(value: object, *, positive: bool = False, input_only: bool = False) -> Decimal:
    _require(not isinstance(value, bool) and (not input_only or isinstance(value, Decimal)) and isinstance(value, (str, Decimal)), "money value is not exact")
    with _http.exact_guard():
        amount = Decimal(value)
        _require(amount.is_finite() and amount >= _ZERO and (not positive or amount > _ZERO), "money value is outside its bounds")
    return amount


def _wire(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _sum(left: Decimal, right: Decimal) -> Decimal:
    with _http.exact_guard():
        return left + right


def _schema(note: str) -> _http.ReadResult:
    return _http.ReadResult("unclear", None, _http.HttpResult("unclear", None, None, note))


def _identity(identity: object, git_sha: object) -> None:
    _require(isinstance(identity, str) and isinstance(git_sha, str) and _IDENTITY.fullmatch(identity) is not None and _SHA.fullmatch(git_sha) is not None, "mutation identity is invalid")


def _valid_liability(value: object) -> Liability:
    _require(isinstance(value, Liability), "liability is required")
    _asset(value.asset)
    _require(_sum(_money(value.principal, input_only=True), _money(value.interest, input_only=True)) == _money(value.total, input_only=True), "liability total is not exact")
    return value


def _validated_operation(direction: str, asset: object, amount: object, principal: object, interest: object, identity: object, git_sha: object) -> MarginOperation:
    _require(isinstance(direction, str) and direction in _DIRECTIONS, "margin operation is invalid")
    asset = _asset(asset, allowed=True)
    _identity(identity, git_sha)
    amount, principal, interest = _money(amount, positive=True, input_only=True), _money(principal, positive=direction == "BORROW", input_only=True), _money(interest, input_only=True)
    with _http.exact_guard():
        _require(direction != "BORROW" or principal <= _PRINCIPAL_CEILING, "principal exceeds its owned ceiling")
        _require(amount == principal and interest == _ZERO if direction == "BORROW" else amount == principal + interest, "margin operation facts do not agree")
    params = (("asset", asset), ("amount", _wire(amount)), ("type", direction))
    return MarginOperation(_http.MutationIntent("BINANCE_MARGIN", direction, identity, git_sha, params), direction, asset, amount, principal, interest)


def _operation(value: object) -> MarginOperation:
    _require(isinstance(value, MarginOperation) and isinstance(value.intent, _http.MutationIntent), "margin operation is invalid")
    _require(value == _validated_operation(value.direction, value.asset, value.amount, value.principal, value.interest, value.intent.identity, value.intent.git_sha), "margin operation facts do not agree")
    return value


def build_borrow(asset: str, principal: Decimal, intent_id: str, git_sha: str) -> MarginOperation:
    return _validated_operation("BORROW", asset, principal, principal, _ZERO, intent_id, git_sha)


def build_repay(liability: Liability, intent_id: str, git_sha: str) -> MarginOperation:
    liability = _valid_liability(liability)
    return _validated_operation("REPAY", liability.asset, liability.total, liability.principal, liability.interest, intent_id, git_sha)


def _history_row(value: object, asset: str, direction: str) -> MarginHistoryRow:
    _require(isinstance(value, dict) and "tranId" not in value, "history row is malformed")
    tx_id, when, origin = value.get("txId"), value.get("timestamp"), value.get("type")
    _require(type(tx_id) is int and tx_id > 0 and type(when) is int and when >= 0 and value.get("asset") == asset and isinstance(origin, str) and origin in _ORIGINS, "history identity is not exact")
    status = _text(value.get("status"), "history status")
    return MarginHistoryRow(direction, tx_id, asset, _money(value.get("amount")), _money(value.get("principal")), _money(value.get("interest")), status, origin, when)


def _history(transport: _http.Transport, base_url: str, asset: str, direction: str, tx_id: int | None, start_ms: int | None, end_ms: int | None, sleep: _Callable[[float], None]) -> _http.ReadResult:
    base_url, asset = _text(base_url, "base_url"), _asset(asset)
    _require(direction in _DIRECTIONS and (tx_id is None or type(tx_id) is int and tx_id > 0), "history selector is invalid")
    _require(not any(value is not None and (type(value) is not int or value < 0) for value in (start_ms, end_ms)) and (start_ms is None or end_ms is None or start_ms <= end_ms), "history window is invalid")
    selectors = (("txId", tx_id), ("startTime", start_ms), ("endTime", end_ms))
    params = (("asset", asset), ("type", direction)) + tuple((name, str(value)) for name, value in selectors if value is not None)
    result = _http.read(transport, base_url, _PATH, params, signed=True, sleep=sleep)
    if result.kind != "ok":
        return _http.ReadResult(result.kind, None, result)
    try:
        _require(isinstance(result.payload, dict), "history is malformed")
        rows, total = result.payload.get("rows"), result.payload.get("total")
        rows = [] if rows is None and total == 0 else rows
        _require(isinstance(rows, list) and len(rows) <= 100 and (total is None or type(total) is int and total >= len(rows)) and (tx_id is None or type(total) is int and total == len(rows)), "history is outside its bounds")
        parsed = tuple(_history_row(row, asset, direction) for row in rows)
        _require(len({row.tx_id for row in parsed}) == len(parsed), "history identities are not unique")
        _require(tx_id is None or not parsed or len(parsed) == 1 and parsed[0].tx_id == tx_id, "history identity conflicts")
        return _http.ReadResult("ok", parsed if tx_id is None else parsed[0] if parsed else None, None)
    except _http.AdapterRefusal:
        return _schema("margin_history_schema")


def read_history_by_direction(transport: _http.Transport, base_url: str, asset: str, direction: _Literal["BORROW", "REPAY"], *, start_ms: int | None = None, end_ms: int | None = None, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[tuple[MarginHistoryRow, ...]]:
    return _history(transport, base_url, asset, direction, None, start_ms, end_ms, sleep)


def read_history_by_tx_id(transport: _http.Transport, base_url: str, asset: str, direction: _Literal["BORROW", "REPAY"], tx_id: int, *, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[MarginHistoryRow]:
    return _history(transport, base_url, asset, direction, tx_id, None, None, sleep)


def await_history_by_tx_id(transport: _http.Transport, base_url: str, asset: str, direction: _Literal["BORROW", "REPAY"], tx_id: int, *, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[MarginHistoryRow]:
    for delay in (0, 1, 2, 4):
        if delay:
            sleep(delay)
        result = read_history_by_tx_id(transport, base_url, asset, direction, tx_id, sleep=sleep)
        if result.kind != "ok" or result.value is not None:
            return result
    return _schema("history_not_visible")


def read_liability(transport: _http.Transport, base_url: str, asset: str, *, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[Liability]:
    base_url, asset = _text(base_url, "base_url"), _asset(asset)
    result = _http.read(transport, base_url, _ACCOUNT, signed=True, sleep=sleep)
    if result.kind != "ok":
        return _http.ReadResult(result.kind, None, result)
    try:
        rows = result.payload.get("userAssets") if isinstance(result.payload, dict) else None
        _require(isinstance(rows, list) and not any(not isinstance(row, dict) for row in rows), "liability rows are malformed")
        assets = tuple(_asset(row.get("asset")) for row in rows)
        _require(len(set(assets)) == len(assets) and assets.count(asset) == 1, "liability asset is not unique")
        row = rows[assets.index(asset)]
        principal, interest = _money(row.get("borrowed")), _money(row.get("interest"))
        return _http.ReadResult("ok", Liability(asset, principal, interest, _sum(principal, interest)), None)
    except _http.AdapterRefusal:
        return _schema("margin_liability_schema")


def submit_once(transport: _http.Transport, base_url: str, operation: MarginOperation, write_intent: _http.IntentWriter) -> MarginSubmit:
    base_url, operation = _text(base_url, "base_url"), _operation(operation)
    try:
        write_intent(operation.intent)
    except _http.IntentNotDurable:
        raise
    except Exception:
        raise _http.IntentNotDurable("intent was not durably written") from None
    result = transport("POST", base_url, _PATH, operation.intent.params, True)
    aliases = [result.payload[name] for name in ("txId", "tranId") if isinstance(result.payload, dict) and name in result.payload] if result.kind == "ok" else []
    accepted = result.kind == "ok" and len(aliases) == 1 and type(aliases[0]) is int and aliases[0] > 0
    return MarginSubmit("accepted" if accepted else "unclear" if result.kind == "ok" else result.kind, aliases[0] if accepted else None, None if accepted else result)


def confirm(operation: MarginOperation, tx_id: int, history: MarginHistoryRow, before: Liability, after: Liability) -> MarginConfirmation:
    try:
        operation, before, after = _operation(operation), _valid_liability(before), _valid_liability(after)
        _require(type(tx_id) is int and tx_id > 0 and isinstance(history, MarginHistoryRow) and type(history.tx_id) is int and history.tx_id > 0, "history identity is not exact")
        tuple(_money(value, input_only=True) for value in (history.amount, history.principal, history.interest))
        with _http.exact_guard():
            observed = (history.direction, history.tx_id, history.asset, history.status, history.amount, history.principal, history.interest)
            expected = (operation.direction, tx_id, operation.asset, "CONFIRMED", operation.amount, operation.principal, operation.interest)
            repay = operation.direction == "REPAY"
            facts = history.origin_type in _ORIGINS and observed == expected and operation.asset == before.asset == after.asset and (not repay or (history.amount, before.principal, before.interest, before.total) == (_sum(history.principal, history.interest), operation.principal, operation.interest, operation.amount))
            movement = after.principal == after.interest == after.total == _ZERO if repay else after.principal == _sum(before.principal, operation.amount) and after.interest >= before.interest
        kind, reason = ("confirmed", operation.direction + "_CONFIRMED") if facts and movement else ("unknown", "MARGIN_CONFIRMATION_MISMATCH" if not facts else "MARGIN_LIABILITY_MISMATCH")
        return MarginConfirmation(kind, operation, tx_id, before, after, reason)
    except (AttributeError, TypeError, _http.AdapterRefusal):
        return MarginConfirmation("unknown", operation, tx_id, before, after, "MARGIN_CONFIRMATION_MISMATCH")
