"""Fail-closed Binance Transfer adapter, frozen interface rows 081-088."""
from __future__ import annotations
from dataclasses import make_dataclass as _make; from decimal import Decimal; import time as _time
from typing import Callable as _Callable, Literal as _Literal, Optional as _Optional; from execution import binance_http as _http; import limits as _limits

__all__ = ("TransferBalances", "TransferRow", "TransferSubmission", "TransferQuery", "build_transfer", "read_balances", "read_transfer_history", "submit_transfer_once", "query_transfer"); _PATH = "/sapi/v1/asset/transfer"; _TYPES = {"spot_to_usdm": "MAIN_UMFUTURE", "usdm_to_spot": "UMFUTURE_MAIN"}; _ZERO = Decimal("0")

TransferBalances = _make("TransferBalances", (("asset", str), ("spot", Decimal), ("usdm", Decimal)), namespace={"__module__": __name__}, frozen=True); TransferRow = _make("TransferRow", (("transfer_type", str), ("asset", str), ("amount", Decimal), ("tran_id", int), ("status", str), ("time_ms", int)), namespace={"__module__": __name__}, frozen=True)
TransferSubmission = _make("TransferSubmission", (("kind", _Literal["accepted", "api_error", "unclear"]), ("tran_id", _Optional[int]), ("error", _Optional[_http.HttpResult])), namespace={"__module__": __name__}, frozen=True); TransferQuery = _make("TransferQuery", (("kind", _Literal["found", "ambiguous", "not_found", "error", "pending"]), ("row", _Optional[TransferRow]), ("error", _Optional[_http.HttpResult])), namespace={"__module__": __name__}, frozen=True)

def _require(condition: bool, note: str) -> None:
    if not condition: raise _http.AdapterRefusal(note)
def _schema(note: str) -> _http.ReadResult: return _http.ReadResult("unclear", None, _http.HttpResult("unclear", None, None, note))
def _money(value: object, positive: bool = False, cap: _Optional[Decimal] = None) -> Decimal:
    with _http.exact_guard(): _require(not isinstance(value, bool) and isinstance(value, (str, Decimal)) and (cap is None or isinstance(cap, Decimal)), "money value is not exact"); amount, ceiling = +Decimal(value), +cap if cap is not None else None; _require(amount.is_finite() and (amount > _ZERO if positive else amount >= _ZERO) and (ceiling is None or ceiling.is_finite() and amount <= ceiling), "money value is outside its bounds"); return amount
def _wire(value: Decimal) -> str: text = format(value, "f"); return text.rstrip("0").rstrip(".") if "." in text else text
def _parts(intent: _http.MutationIntent) -> tuple[str, str, Decimal]:
    _require(isinstance(intent, _http.MutationIntent) and type(intent.venue) is str and intent.venue == "BINANCE_TRANSFER" and type(intent.operation) is str and intent.operation == "TRANSFER", "transfer intent is invalid"); _require(type(intent.identity) is str and 0 < len(intent.identity) <= 64 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/-" for c in intent.identity) and type(intent.git_sha) is str and len(intent.git_sha) == 40 and all(c in "0123456789abcdefABCDEF" for c in intent.git_sha), "transfer identity is invalid"); _require(type(intent.params) is tuple and len(intent.params) == 3 and all(type(pair) is tuple and len(pair) == 2 and type(pair[0]) is str for pair in intent.params), "transfer parameters are invalid"); values = dict(intent.params); kind, asset, amount = values.get("type"), values.get("asset"), _money(values.get("amount"), True, _limits.PER_LEG_CAP_USDT); _require(type(kind) is str and kind in _TYPES.values() and type(asset) is str and asset == "USDT" and intent.params == (("type", kind), ("asset", asset), ("amount", _wire(amount))), "transfer parameters are invalid"); return kind, asset, amount
def _row(value: object, kind: str) -> TransferRow:
    asset = value.get("asset") if isinstance(value, dict) else None; _require(isinstance(value, dict) and value.get("type") == kind and isinstance(asset, str) and 0 < len(asset) <= 20 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for c in asset), "transfer row is invalid"); tran_id, when, status = value.get("tranId"), value.get("timestamp"), value.get("status"); _require(type(tran_id) is int and tran_id > 0 and type(when) is int and when >= 0 and isinstance(status, str) and bool(status), "transfer row identity is invalid"); return TransferRow(kind, asset, _money(value.get("amount"), True), tran_id, status, when)
def _balance(payload: object, key: str | None, field: str, asset: str) -> Decimal:
    rows = payload.get(key) if key is not None and isinstance(payload, dict) else payload if key is None else None; _require(isinstance(rows, list) and all(isinstance(row, dict) and isinstance(row.get("asset"), str) for row in rows), "balance rows are invalid"); matches = [row for row in rows if row["asset"] == asset]; _require(len(matches) == 1, "asset balance is not unique"); return _money(matches[0].get(field))

def build_transfer(direction: _Literal["spot_to_usdm", "usdm_to_spot"], asset: str, amount: Decimal, request_id: str, git_sha: str) -> _http.MutationIntent:
    _require(type(direction) is str and direction in _TYPES and type(asset) is str and asset == "USDT" and isinstance(amount, Decimal), "transfer request is invalid"); amount = _money(amount, True, _limits.PER_LEG_CAP_USDT); intent = _http.MutationIntent("BINANCE_TRANSFER", "TRANSFER", request_id, git_sha, (("type", _TYPES[direction]), ("asset", asset), ("amount", _wire(amount)))); _parts(intent); return intent
def read_balances(transport: _http.Transport, spot_base_url: str, futures_base_url: str, asset: str, *, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[TransferBalances]:
    _require(isinstance(spot_base_url, str) and bool(spot_base_url) and isinstance(futures_base_url, str) and bool(futures_base_url) and isinstance(asset, str) and 0 < len(asset) <= 20 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for c in asset), "balance selector is invalid")
    if (spot := _http.read(transport, spot_base_url, "/api/v3/account", signed=True, sleep=sleep)).kind != "ok": return _http.ReadResult(spot.kind, None, spot)
    if (futures := _http.read(transport, futures_base_url, "/fapi/v3/balance", signed=True, sleep=sleep)).kind != "ok": return _http.ReadResult(futures.kind, None, futures)
    try: value = TransferBalances(asset, _balance(spot.payload, "balances", "free", asset), _balance(futures.payload, None, "availableBalance", asset))
    except _http.AdapterRefusal: return _schema("transfer_balance_schema")
    return _http.ReadResult("ok", value, None)
def read_transfer_history(transport: _http.Transport, base_url: str, transfer_type: str, start_ms: int, end_ms: int, *, sleep: _Callable[[float], None] = _time.sleep) -> _http.ReadResult[tuple[TransferRow, ...]]:
    _require(isinstance(base_url, str) and bool(base_url) and type(transfer_type) is str and transfer_type in _TYPES.values() and type(start_ms) is int and type(end_ms) is int and 0 <= start_ms <= end_ms, "history selector is invalid"); found = []; expected_total = None
    for page in range(1, 21):
        params = (("type", transfer_type), ("startTime", str(start_ms)), ("endTime", str(end_ms)), ("current", str(page)), ("size", "100"))
        if (reply := _http.read(transport, base_url, _PATH, params, signed=True, sleep=sleep)).kind != "ok": return _http.ReadResult(reply.kind, None, reply)
        try:
            _require(isinstance(reply.payload, dict), "history page is invalid"); total, rows = reply.payload.get("total"), reply.payload.get("rows"); rows = [] if rows is None and total == 0 else rows; _require(isinstance(rows, list) and len(rows) <= 100 and type(total) is int and total >= 0 and (expected_total is None or total == expected_total), "history page is invalid"); parsed = [_row(row, transfer_type) for row in rows]; _require(all(start_ms <= row.time_ms <= end_ms for row in parsed), "history row is outside requested window")
        except _http.AdapterRefusal: return _schema("transfer_history_schema")
        expected_total = total; found.extend(parsed)
        if len({row.tran_id for row in found}) != len(found) or len(found) > total: return _schema("transfer_history_schema")
        if len(found) == total: return _http.ReadResult("ok", tuple(found), None)
        if len(rows) < 100: return _schema("transfer_history_schema")
    return _schema("transfer_history_page_limit")
def submit_transfer_once(transport: _http.Transport, base_url: str, intent: _http.MutationIntent, write_intent: _http.IntentWriter) -> TransferSubmission:
    _require(isinstance(base_url, str) and bool(base_url), "base_url is required"); _parts(intent)
    try: write_intent(intent)
    except Exception: raise _http.IntentNotDurable("intent was not durably written") from None
    if (reply := transport("POST", base_url, _PATH, intent.params, True)).kind != "ok": return TransferSubmission(reply.kind, None, reply)
    if type(tran_id := reply.payload.get("tranId") if isinstance(reply.payload, dict) else None) is not int or tran_id <= 0: error = _http.HttpResult("unclear", None, None, "transfer_submit_schema"); return TransferSubmission("unclear", None, error)
    return TransferSubmission("accepted", tran_id, None)
def query_transfer(transport: _http.Transport, base_url: str, intent: _http.MutationIntent, baseline_ids: frozenset[int], tran_id: int | None, start_ms: int, end_ms: int, *, sleep: _Callable[[float], None] = _time.sleep) -> TransferQuery:
    kind, asset, amount = _parts(intent); _require(type(baseline_ids) is frozenset and all(type(value) is int and value > 0 for value in baseline_ids) and (tran_id is None or type(tran_id) is int and tran_id > 0 and tran_id not in baseline_ids), "transfer identity selector is invalid"); last = None
    for attempt in range(8):
        if (last := read_transfer_history(transport, base_url, kind, start_ms, end_ms, sleep=sleep)).kind == "api_error": return TransferQuery("error", None, last.error)
        if last.kind == "ok":
            with _http.exact_guard(): matches = [row for row in last.value if row.asset == asset and row.amount == amount and (row.tran_id == tran_id if tran_id is not None else row.tran_id not in baseline_ids)]
            if len(matches) == 1: return TransferQuery("found", matches[0], None)
            if len(matches) > 1: return TransferQuery("ambiguous", None, None)
            if tran_id is not None and any(row.tran_id == tran_id for row in last.value): return TransferQuery("pending", None, _http.HttpResult("unclear", None, None, "transfer_identity_conflict"))
        if attempt < 7: sleep(2)
    return TransferQuery("not_found", None, None) if last.kind == "ok" else TransferQuery("pending", None, last.error)
