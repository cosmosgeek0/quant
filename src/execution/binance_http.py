"""binance_http — signer, ONE-attempt HTTP transport, bounded reads, and the
shared result types of the Binance hand (frozen interface rows 016-036 of
study/design/EXECUTION-HAND-INTERFACE.txt).

Types and constants below are the frozen public surface (rows 016-027); the
transport itself (BinanceHttp, sync_time, __call__, read — rows 028-036) is
implemented by shard S1 in this same file. Money values are Decimal; unproved
truth stays None; nothing here retries a mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Generic, Literal, Optional, Tuple, TypeVar

# ---- constants (row 025-027; binance_http is the sole owner) ---------------
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

# ---- wire and callback types (rows 016-017) --------------------------------
Params = Tuple[Tuple[str, str], ...]
"""Canonical, ordered, duplicate-key-free wire data."""

T = TypeVar("T")


class AdapterRefusal(ValueError):
    """The sole caller/schema exception of every adapter (row 012)."""


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
"""method, base_url, path, params, signed -> HttpResult (row 016)."""

IntentWriter = Callable[[MutationIntent], None]
"""Returns only after file + parent-directory durability (row 017)."""


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

# ---- transport (rows 028-036): implemented by shard S1 below this line ------
