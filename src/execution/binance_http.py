"""Shared Binance types, exact-money context, and one-attempt HTTP transport."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import (Context, Decimal, DecimalException, DivisionByZero, Inexact,
                     InvalidOperation, Overflow, Rounded, localcontext)
from typing import Callable, Generic, Literal, Optional, Tuple, TypeVar

SPOT_BASE_URL = "https://api.binance.com"
FUTURES_BASE_URL = "https://fapi.binance.com"
HTTP_TIMEOUT_SECONDS = 15
RECV_WINDOW_MS = 5000
READ_ATTEMPTS = 3
READ_BACKOFF_SECONDS = (1, 2)
ORDER_QUERY_ATTEMPTS = 45
ORDER_QUERY_DELAY_SECONDS = 2
ORDER_NOT_FOUND_STREAK = 8
TERMINAL_ORDER_STATUSES = frozenset(
    {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"})
AMBIGUOUS_API_CODES = frozenset({-1006, -1007})

Params = Tuple[Tuple[str, str], ...]

T = TypeVar("T")


class AdapterRefusal(ValueError): pass


_EXACT_TRAPS = (Inexact, Rounded, InvalidOperation, DivisionByZero, Overflow)


def exact_context():
    return localcontext(Context(prec=60, traps=list(_EXACT_TRAPS)))


@contextmanager
def exact_guard():
    try:
        with exact_context():
            yield
    except DecimalException as error:
        raise AdapterRefusal("decimal operation is not exact") from error


class IntentNotDurable(RuntimeError):
    """The sole receipt exception: the intent could not be durably written (row 012)."""


@dataclass(frozen=True)
class HttpResult:  # row 018
    kind: Literal["ok", "api_error", "unclear"]
    payload: object | None
    code: int | None
    msg: str | None


@dataclass(frozen=True)
class ReadResult(Generic[T]):  # row 019
    kind: Literal["ok", "api_error", "unclear"]
    value: T | None
    error: HttpResult | None


@dataclass(frozen=True)
class MutationIntent:  # row 020
    venue: str
    operation: str
    identity: str
    git_sha: str
    params: Params


Transport = Callable[[str, str, str, Optional[Params], bool], HttpResult]

IntentWriter = Callable[[MutationIntent], None]


@dataclass(frozen=True)
class OrderPlan:  # row 021
    intent: MutationIntent
    symbol: str
    side: str
    quantity: Decimal | None
    quote_limit: Decimal | None
    residual: Decimal
    reduce_only: bool


@dataclass(frozen=True)
class OrderIdentity:  # row 022
    symbol: str
    client_id: str
    order_id: int | None


@dataclass(frozen=True)
class Fee:  # row 022
    asset: str
    amount: Decimal


@dataclass(frozen=True)
class Balance:  # row 022
    asset: str
    free: Decimal
    locked: Decimal
    available: Decimal | None


@dataclass(frozen=True)
class Fill:  # row 022
    trade_id: int | None
    order_id: int
    price: Decimal
    quantity: Decimal
    quote_quantity: Decimal
    fee: Fee


@dataclass(frozen=True)
class OrderResult:  # row 023
    tag: Literal["terminal", "not_found", "error", "pending"]
    identity: OrderIdentity
    returned_client_id: str | None
    status: str | None
    terminal: bool
    side: str | None
    reduce_only: bool | None
    executed_quantity: Decimal | None
    quote_quantity: Decimal | None
    response_fills: Tuple[Fill, ...]
    error: HttpResult | None


@dataclass(frozen=True)
class FillCoverage:  # row 024
    fills: Tuple[Fill, ...]
    covered_quantity: Decimal
    executed_quantity: Decimal
    complete: bool
    fees: Tuple[Fee, ...]

import hashlib, hmac, json, time, urllib.error, urllib.parse, urllib.request

_MAX_TIMEOUT_SECONDS, _MAX_RECV_WINDOW_MS = 60, 60000
_RESERVED_PARAMS = frozenset({"signature", "timestamp", "recvWindow"})
_PRIVATE_MSG_PARTS = ("www.", "x-mbx-apikey", "signature", "authorization", "credential", "api-key", "api_key", "header")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args): return None


def system_now_ms() -> int: return int(time.time() * 1000)


def _unclear(msg: str, code: int | None = None) -> HttpResult: return HttpResult("unclear", None, code, msg)


def _reject_constant(_value: str) -> None: raise ValueError("non-finite JSON number")


def _unique_object(pairs):
    if len(pairs) != len(dict(pairs)): raise ValueError("duplicate JSON key")
    return dict(pairs)


def _clean_api_msg(value: object, secrets: Tuple[str, str]) -> str | None:
    if type(value) is not str: return None
    words = "".join(char if char.isprintable() else " " for char in value).split()
    private = _PRIVATE_MSG_PARTS + tuple(secret.lower() for secret in secrets)
    cleaned = " ".join(word for word in words if "/" not in word and not any(part in word.lower() for part in private))
    return cleaned[:160] or None


def _classify(status: object, body: object, secrets: Tuple[str, str]) -> HttpResult:
    if type(status) is not int: return _unclear("invalid_http_status")
    if 300 <= status < 400: return _unclear("http_%d" % status, status)
    if status in (408, 409) or status >= 500: return _unclear("http_%d" % status)
    try:
        with exact_context():
            payload = json.loads(body, parse_float=Decimal, parse_int=int,
                                 parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (DecimalException, TypeError, ValueError, UnicodeError): return _unclear("invalid_json")
    code = payload.get("code") if isinstance(payload, dict) else None
    if type(code) is not int: code = None
    msg = _clean_api_msg(payload.get("msg"), secrets) if code is not None else None
    if code in AMBIGUOUS_API_CODES: return _unclear(msg or "ambiguous_api_result", code)
    if 200 <= status < 300:
        if code is not None and code < 0: return HttpResult("api_error", None, code, msg) if msg is not None else _unclear("api_error_incomplete", code)
        return HttpResult("ok", payload, None, None)
    if 400 <= status < 500:
        if code is not None and code < 0: return HttpResult("api_error", None, code, msg) if msg is not None else _unclear("api_error_incomplete", code)
        return _unclear("http_response_unclear")
    return _unclear("http_response_unclear")


def _canonical_params(params: Params | None) -> Params:
    if params is None: return ()
    if type(params) is not tuple: raise AdapterRefusal("params must be a canonical tuple")
    seen = set()
    for pair in params:
        if (type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str or not pair[0] or type(pair[1]) is not str):
            raise AdapterRefusal("params must contain string pairs")
        if pair[0] in seen or pair[0] in _RESERVED_PARAMS: raise AdapterRefusal("params contain a duplicate or reserved key")
        seen.add(pair[0])
    return params


class BinanceHttp:
    def __init__(self, api_key: str, api_secret: str, *, timeout_seconds: int = HTTP_TIMEOUT_SECONDS, recv_window_ms: int = RECV_WINDOW_MS,
                 now_ms: Callable[[], int] = system_now_ms):
        if type(api_key) is not str or not api_key.strip() or type(api_secret) is not str or not api_secret.strip(): raise AdapterRefusal("nonempty credentials are required")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS: raise AdapterRefusal("timeout_seconds is outside its bound")
        if type(recv_window_ms) is not int or not 1 <= recv_window_ms <= _MAX_RECV_WINDOW_MS: raise AdapterRefusal("recv_window_ms is outside its bound")
        if not callable(now_ms): raise AdapterRefusal("now_ms must be callable")
        self._api_key, self._api_secret = api_key, api_secret
        self._timeout, self._recv_window = timeout_seconds, recv_window_ms
        self._now_ms, self._offsets, self._open = now_ms, {}, urllib.request.build_opener(_NoRedirect()).open

    def _clock(self) -> int | None:
        try:
            value = self._now_ms()
        except Exception:
            return None
        return value if type(value) is int and value >= 0 else None

    def sync_time(self, base_url: str, time_path: str) -> HttpResult:
        result = self("GET", base_url, time_path)
        if result.kind != "ok" or not isinstance(result.payload, dict):
            return result if result.kind != "ok" else _unclear("invalid_server_time")
        server_time, local_time = result.payload.get("serverTime"), self._clock()
        if type(server_time) is not int or server_time <= 0 or local_time is None: return _unclear("invalid_server_time")
        self._offsets[base_url] = server_time - local_time
        return result

    def __call__(self, method: str, base_url: str, path: str,
                 params: Params | None = None, signed: bool = False) -> HttpResult:
        if (type(method) is not str or not method or type(base_url) is not str
                or not base_url or type(path) is not str or not path.startswith("/")
                or "?" in path or "#" in path or type(signed) is not bool):
            raise AdapterRefusal("method, base_url, and path must be valid strings")
        values = list(_canonical_params(params))
        if signed:
            if base_url not in self._offsets: return _unclear("clock_not_synchronized")
            local_time = self._clock()
            if local_time is None: return _unclear("clock_invalid")
            timestamp = local_time + self._offsets[base_url]
            if timestamp <= 0: return _unclear("clock_invalid")
            values.extend((("timestamp", str(timestamp)),
                           ("recvWindow", str(self._recv_window))))
        try:
            query = urllib.parse.urlencode(values)
            if signed:
                signature = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
                query += "&signature=" + signature
            url = base_url + path + ("?" + query if query else "")
            request = urllib.request.Request(url, method=method)
            if signed: request.add_header("X-MBX-APIKEY", self._api_key)
            with self._open(request, timeout=self._timeout) as response:
                status = getattr(response, "status", None)
                status = response.getcode() if status is None else status
                return _classify(status, response.read(), (self._api_key, self._api_secret))
        except urllib.error.HTTPError as error:
            try:
                return _classify(error.code, error.read(), (self._api_key, self._api_secret))
            except Exception:
                return _unclear("transport_error")
        except Exception:
            return _unclear("transport_error")


def read(transport: Transport, base_url: str, path: str,
         params: Params | None = None, *, signed: bool = False,
         sleep: Callable[[float], None] = time.sleep) -> HttpResult:
    for attempt in range(READ_ATTEMPTS):
        result = transport("GET", base_url, path, params, signed)
        if result.kind != "unclear": return result
        if attempt < len(READ_BACKOFF_SECONDS): sleep(READ_BACKOFF_SECONDS[attempt])
    return result
