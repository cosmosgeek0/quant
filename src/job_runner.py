#!/usr/bin/env python3
"""Consume at most one strictly bounded live-mechanics job per tick.

Jobs are immutable-by-convention JSON files in ``RECEIPT_DIR/jobs``.  A
persistent O_EXCL claim makes every job id single-use.  The runner never
transfers funds.  Momentum entry/exit uses one atomic, query-first position
state while the original round-trip operations remain available for mechanics
proofs.
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import hashlib
import json
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any, Callable, Dict, List, Optional, Tuple

import basis_cycle
import basis_executor
import limits
import margin_ops
from execution import binance_futures, binance_http, binance_margin, binance_spot


class _RetiredFixtureHooks:
    main = http = live_request_factory = None
    receipt_path = ""
    SYMBOL = MAX_NOTIONAL_RAW = ""


futures_hand = _RetiredFixtureHooks()
set_leverage_1x = _RetiredFixtureHooks()


COMMON_JOB_REQUIRED_FIELDS = {"job_id", "op", "created_utc", "expires_utc", "requested_by"}
TRADING_JOB_REQUIRED_FIELDS = COMMON_JOB_REQUIRED_FIELDS | {"symbol", "max_notional"}
TRADING_JOB_OPTIONAL_FIELDS = {"venue"}
TRADING_JOB_FIELDS = TRADING_JOB_REQUIRED_FIELDS | TRADING_JOB_OPTIONAL_FIELDS
MARGIN_JOB_REQUIRED_FIELDS = COMMON_JOB_REQUIRED_FIELDS | {"asset", "principal", "client_tag", "intent_id", "operator_token"}
MARGIN_JOB_FIELDS = MARGIN_JOB_REQUIRED_FIELDS
JOB_REQUIRED_FIELDS = TRADING_JOB_REQUIRED_FIELDS
JOB_OPTIONAL_FIELDS = TRADING_JOB_OPTIONAL_FIELDS
JOB_FIELDS = TRADING_JOB_FIELDS
ACCEPT_ONE_FIELDS = {"operator", "created_utc", "expires_utc", "max_notional"}
OPERATOR_FIELDS = {"operator"}
RESULT_FIELDS = {"job_id", "op", "symbol", "verdict", "exit_code", "receipt_path", "utc"}
MOMENTUM_LEDGER_FIELDS = {"fills", "fees", "realized"}
MOMENTUM_RESULT_FIELDS = RESULT_FIELDS | MOMENTUM_LEDGER_FIELDS
ADVERSE_STOP_REFUSAL_FIELDS = {"kind", "error", "symbol", "utc"}
ADVERSE_STOP_BLOCKED_FIELDS = {"kind", "symbol", "utc"}
MARGIN_GOVERNOR_HALT_FIELDS = {"kind", "job_id", "lock_exists", "lock_token", "lock_op", "lock_symbol", "runner_receipt_exists", "runner_verdict", "source_receipt_exists", "source_verdict", "utc"}
MARGIN_GOVERNOR_ANOMALY_FIELDS = {"kind", "job_id", "artifact", "field", "anomaly_type", "lock_exists", "utc"}
MARGIN_RECONCILED_FIELDS = {"kind", "job_id", "op", "asset", "verdict", "reason", "transaction_id", "history_watermark", "ledger_confirmation", "account_liability_before", "account_liability", "snapshot_time_note", "utc"}
MARGIN_LEGACY_RECONCILED_FIELDS = (MARGIN_RECONCILED_FIELDS - {"history_watermark", "account_liability_before"}) | {"legacy_exclusivity_proof"}
LEGACY_REPAY_SHAPE = "LEGACY_REPAY_SHAPE"
LEGACY_REPAY_SHAPE_VERSION = 1
# Frozen historical receipt facts measured outside the receipt files.
LEGACY_REPAY_RECEIPTS = (("mproofr-2c2a3027d877", "401382497303", "USDT", "2.5000387", "4a7e2634361892dccf41137fbd88fe62ae8e3281"),)
MARGIN_LEGACY_REPAY_RECONCILED_FIELDS = MARGIN_RECONCILED_FIELDS | {"receipt_shape", "receipt_shape_version"}
MARGIN_LEGACY_EXCLUSIVITY_PROOF_FIELDS = {"kind", "window_start_utc", "window_end_utc", "row_count"}
MARGIN_LEGACY_PROOF_FIELDS = {"kind", "job_id", "transaction_id", "history_watermark_substitute", "account_liability_before_substitute", "legacy_exclusivity_proof", "utc"}
LEGACY_HISTORY_WATERMARK_SUBSTITUTE = "COMPLETE_BOTH_DIRECTION_PAGINATED_HISTORY_EXCLUSIVITY"
LEGACY_ACCOUNT_LIABILITY_BEFORE_SUBSTITUTE = "CURRENT_BORROWED_EQUALS_RECORDED_PRINCIPAL"
# Frozen source-receipt shape written by release b188351.
LEGACY_RECEIPT_FIELDS = ("kind", "job_id", "sha", "ready", "mutation_attempts", "verdict", "reason", "started_utc", "governor_token", "governor_release", "authority_checked_utc", "authority_digests", "authority_stop_jobs_present", "mutation_started_utc", "submission", "finished_utc")
MARGIN_RECONCILE_UNCONFIRMED_FIELDS = {"kind", "job_id", "reason", "utc"}
TICK_LOCK_RECOVERY_FIELDS = {"kind", "pid", "age_seconds", "utc"}
TICK_LOCK_FIELDS = {"pid", "process_start", "heartbeat_monotonic_ns", "heartbeat_utc", "created_utc", "token"}
TRADING_OPS = {"FUTURES_ROUNDTRIP", "BASIS_ROUNDTRIP", "MOMENTUM_ROUNDTRIP", "MOMENTUM_OPEN", "MOMENTUM_EXIT"}
MARGIN_OPS = margin_ops.MARGIN_OPERATIONS
OPS = TRADING_OPS | MARGIN_OPS
ENTRY_OPS = {"FUTURES_ROUNDTRIP", "BASIS_ROUNDTRIP", "MOMENTUM_ROUNDTRIP", "MOMENTUM_OPEN", margin_ops.MARGIN_BORROW}
MOMENTUM_OPS = {"MOMENTUM_ROUNDTRIP", "MOMENTUM_OPEN", "MOMENTUM_EXIT"}
MOMENTUM_STANDING = {"OPENING", "OPEN", "CLOSING", "UNKNOWN"}
MAX_JOB_AGE = timedelta(minutes=15)
# Acceptance clocks may differ by no more than two minutes.
ACCEPT_ONE_FORWARD_SKEW = timedelta(seconds=120)
# The runner is scheduled once per minute.
TICK_INTERVAL_SECONDS = 60.0
# An owner whose OS identity cannot be confirmed gets three heartbeat intervals.
TICK_LOCK_HEARTBEAT_INTERVALS = 3
MARGIN_RUNNER_NOTIONAL_CAP = Decimal("3")
MARGIN_ASSET_WHITELIST = frozenset(("USDT",))
MARGIN_ENDPOINT_PATH = margin_ops.MARGIN_ENDPOINT_PATH
# Exchange CONFIRMED maps to the direction-bearing internal receipt status.
MARGIN_HISTORY_CONFIRMED_STATUS = {"BORROW": "BORROW_CONFIRMED", "REPAY": "REPAY_CONFIRMED"}
# Keep the immediate history read, then retry it at most three times.
MARGIN_HISTORY_CONFIRMATION_RETRY_LIMIT = 3
# Fixed 1s, 2s, and 4s delays bound the added confirmation wait at 7s.
MARGIN_HISTORY_CONFIRMATION_BACKOFF_SECONDS = (1, 2, 4)
MARGIN_LEGACY_HISTORY_WINDOW_PAD = timedelta(seconds=60)
MARGIN_DECIMAL_TEXT_MAX = 64
MARGIN_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
# A zero-borrow residual must stay below both this USDT ceiling and the
# relative share below before it can be recorded for interest-only follow-up.
MARGIN_REPAY_DUST_BOUND = Decimal("0.0001")
MARGIN_REPAY_RELATIVE_DUST_RATE = Decimal("0.001")
MARGIN_SNAPSHOT_TIME_VERIFIED = "SNAPSHOT_TIME_VERIFIED"
MARGIN_SNAPSHOT_TIME_UNVERIFIED = "SNAPSHOT_TIME_UNVERIFIED"
MARGIN_TRANSACTION_ID_RE = re.compile(r"^[1-9][0-9]*$")
# Durable Margin release evidence has a bounded node walk.  A separate depth
# ceiling keeps the standard JSON decoder below its recursion boundary.
MARGIN_ARTIFACT_MAX_NODES = 4096
MARGIN_ARTIFACT_MAX_DEPTH = 64
ACCOUNT_GOVERNOR_LOCK_FILE = "account-governor.lock"
JOB_NAME_RE = re.compile(r"^job-([A-Za-z0-9][A-Za-z0-9_-]{0,63})\.json$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
FREEZE_VERDICTS = {"UNKNOWN", "OPEN_EXPOSURE", "BLOCKED_UNRESOLVED"}
MOMENTUM_STATE_VERSION = 1
FIRST_AUTONOMOUS_CLOSE_MARKER = "first_autonomous_close_passed"
RECEIPT_PATTERNS = ("futures-hand-*.json", "basis-executor-*.json", "roundtrip-*.json")


class JobRefusal(Exception):
    """A definite local validation reason prevents execution."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LegacyProvenanceMismatch(JobRefusal):
    """One named fact did not bind the frozen legacy REPAY receipt."""

    def __init__(self, fact: str):
        super().__init__("LEGACY_PROVENANCE_MISMATCH")
        self.fact = fact


class DuplicateField(JobRefusal):
    """Strict JSON found one repeated object member name."""

    def __init__(self, field: str):
        super().__init__("DUPLICATE_FIELD")
        self.field = field


class ArtifactAnomaly(JobRefusal):
    """One durable release artifact has a typed, bounded-read anomaly."""

    def __init__(self, artifact: str, field: str, anomaly_type: str,
                 reason: str, job_id: str = "UNKNOWN"):
        super().__init__(reason)
        self.artifact = artifact
        self.field = field
        self.anomaly_type = anomaly_type
        self.job_id = job_id


class MomentumStateError(Exception):
    """Persistent or exchange momentum truth is unreadable or inconsistent."""


@dataclass(frozen=True)
class Job:
    job_id: str
    op: str
    symbol: str
    requested_max_notional: Decimal
    effective_notional: Decimal
    created_utc: datetime
    expires_utc: datetime
    requested_by: str
    venue: str = "futures"
    operator_token: str = ""
    intent_id: str = ""
    margin_operation: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class AcceptOne:
    operator: str
    created_utc: datetime
    expires_utc: datetime
    max_notional: Decimal


@dataclass(frozen=True)
class Outcome:
    verdict: str
    exit_code: int
    receipt_path: str
    reason: str = ""
    ledger: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ExecutionServices:
    transport: Callable[..., Any]; spot_base_url: str; futures_base_url: str
    sleep: Callable[[float], None]
    margin_build_borrow: Callable[..., Any]; margin_build_repay: Callable[..., Any]
    margin_read_history_by_direction: Callable[..., Any]; margin_read_history_by_tx_id: Callable[..., Any]
    margin_read_liability: Callable[..., Any]; margin_submit_once: Callable[..., Any]; margin_confirm: Callable[..., Any]
    futures_read_market: Callable[..., Any]; futures_read_preflight: Callable[..., Any]; futures_set_leverage_1x_once: Callable[..., Any]
    futures_build_open_order: Callable[..., Any]; futures_build_reduce_only_close: Callable[..., Any]
    futures_query_order: Callable[..., Any]; futures_submit_order_once: Callable[..., Any]
    futures_read_fills: Callable[..., Any]; futures_read_income: Callable[..., Any]
    spot_read_market: Callable[..., Any]; spot_read_account_state: Callable[..., Any]; spot_build_market_order: Callable[..., Any]
    spot_query_order: Callable[..., Any]; spot_submit_order_once: Callable[..., Any]; spot_read_fills: Callable[..., Any]
    spot_fees_by_asset: Callable[..., Any]; spot_net_base_quantity: Callable[..., Any]


def _execution_services(transport: Callable[..., Any], sleep: Callable[[float], None]) -> ExecutionServices:
    return ExecutionServices(transport, binance_http.SPOT_BASE_URL, binance_http.FUTURES_BASE_URL, sleep, binance_margin.build_borrow, binance_margin.build_repay, binance_margin.read_history_by_direction, binance_margin.read_history_by_tx_id, binance_margin.read_liability, binance_margin.submit_once, binance_margin.confirm, binance_futures.read_market, binance_futures.read_preflight, binance_futures.set_leverage_1x_once, binance_futures.build_open_order, binance_futures.build_reduce_only_close, binance_futures.query_order, binance_futures.submit_order_once, binance_futures.read_fills, binance_futures.read_income, binance_spot.read_market, binance_spot.read_account_state, binance_spot.build_market_order, binance_spot.query_order, binance_spot.submit_order_once, binance_spot.read_fills, binance_spot.fees_by_asset, binance_spot.net_base_quantity)


def _momentum_open_plan(_effective: Decimal) -> Dict[str, Any]:
    raise MomentumStateError("retired fixture plan is unavailable")


class _RetiredFuturesTransport:
    def __init__(self) -> None:
        self.risk = None; self.fills: Dict[int, List[Dict[str, Any]]] = {}

    @staticmethod
    def _result(value: Any) -> binance_http.HttpResult:
        if not isinstance(value, tuple) or len(value) != 2: return binance_http.HttpResult("unclear", None, None, "retired fixture result")
        kind, payload = value
        if kind == "ok": return binance_http.HttpResult("ok", payload, None, None)
        code = payload.get("code") if isinstance(payload, dict) else None
        message = payload.get("msg") if isinstance(payload, dict) else None
        return binance_http.HttpResult(kind if kind == "api_error" else "unclear", None, code if type(code) is int else None, message if type(message) is str else "retired fixture result")

    def _call(self, method: str, path: str, params: Dict[str, str]) -> binance_http.HttpResult:
        try:
            return self._result(futures_hand.http(method, path, params, signed=True))
        except Exception: return binance_http.HttpResult("unclear", None, None, "retired fixture call")

    def _order(self, payload: Any) -> Any:
        if not isinstance(payload, dict): return payload
        row = dict(payload)
        try:
            order_id = int(str(row["orderId"]))
        except (KeyError, TypeError, ValueError): return row
        row["orderId"] = order_id; row["reduceOnly"] = row.get("reduceOnly") is True or str(row.get("reduceOnly", "")).lower() == "true"
        found = []
        for raw in row.get("fills", []):
            if not isinstance(raw, dict): continue
            price, quantity = Decimal(str(raw.get("price"))), Decimal(str(raw.get("qty")))
            found.append({"symbol": row.get("symbol"), "id": order_id, "orderId": order_id, "price": format(price, "f"), "qty": format(quantity, "f"), "quoteQty": format(price * quantity, "f"), "commission": str(raw.get("commission")), "commissionAsset": raw.get("commissionAsset"), "side": row.get("side"), "buyer": row.get("side") == "BUY", "maker": False, "time": 1786708800000, "realizedPnl": str(raw.get("realizedPnl", "0"))})
        if found: self.fills[order_id] = found
        return row

    def __call__(self, method: str, _base_url: str, path: str, params: Any = None, signed: bool = False) -> binance_http.HttpResult:
        wire = dict(params or ())
        if path == "/fapi/v1/accountConfig": return binance_http.HttpResult("ok", {"canTrade": True}, None, None)
        if path == "/fapi/v1/positionSide/dual": return binance_http.HttpResult("ok", {"dualSidePosition": False}, None, None)
        if path == "/fapi/v3/balance": return binance_http.HttpResult("ok", [{"asset": "USDT", "availableBalance": "1000"}], None, None)
        if path == "/fapi/v3/account":
            self.risk = self._call("GET", "/fapi/v3/positionRisk", {"symbol": os.environ["SYMBOL"]})
            return self._call(method, path, wire)
        if path == "/fapi/v3/positionRisk" and self.risk is not None: return self.risk
        if path == "/fapi/v1/symbolConfig":
            result = self._call(method, path, wire); self.risk = None
            if result.kind == "ok": return result
            try:
                request = set_leverage_1x.live_request_factory(os.environ.get("BINANCE_API_KEY", ""), os.environ.get("BINANCE_API_SECRET", ""))
                return self._result(request(method, binance_http.FUTURES_BASE_URL, path, wire, signed))
            except Exception: return result
        if path in (binance_futures.FUTURES_ORDER_PATH, "/fapi/v1/openOrders"):
            result = self._call(method, path, wire)
            return replace(result, payload=self._order(result.payload)) if result.kind == "ok" and path == binance_futures.FUTURES_ORDER_PATH else result
        if path == binance_futures.FUTURES_USER_TRADES_PATH:
            return binance_http.HttpResult("ok", self.fills.get(int(wire["orderId"]), []), None, None)
        if path == binance_futures.FUTURES_INCOME_PATH:
            rows = self.fills.get(next((key for key in reversed(self.fills) if self.fills[key][0]["side"] == "SELL"), 0), [])
            payload = [{"tranId": row["orderId"], "symbol": row["symbol"], "incomeType": "REALIZED_PNL", "income": row["realizedPnl"], "asset": "USDT", "time": int(wire["startTime"]) + 1, "tradeId": row["id"]} for row in rows]
            return binance_http.HttpResult("ok", payload, None, None)
        return self._call(method, path, wire)


def _retired_futures_market(_transport: Any, _base_url: str, symbol: str, **_kwargs: Any) -> binance_http.ReadResult:
    plan = _momentum_open_plan(Decimal(os.environ["MAX_NOTIONAL"])); quantity = _momentum_decimal(plan.get("quantity"), "retired fixture quantity"); price = _momentum_decimal(plan.get("limit_price"), "retired fixture price")
    market = binance_futures.FuturesMarket(symbol, "TRADING", "PERPETUAL", quantity, quantity, Decimal("0.000001"), Decimal("2"), price - Decimal("1.01"), price - Decimal("1"), price, Decimal("0.01"), Decimal("0.01"), price * Decimal("10"), quantity, Decimal("0.5"), "USDT", "USDT", ("LIMIT", "MARKET"), ("IOC",))
    return binance_http.ReadResult("ok", market, None)


def _retired_futures_preflight(transport: Any, base_url: str, market: Any, proposed_notional: Any, *, require_flat: bool, sleep: Callable[[float], None]) -> binance_http.ReadResult:
    if proposed_notional is not None: return binance_http.ReadResult("ok", binance_futures.FuturesPreflight(True, True, Decimal(0), Decimal(0), (), Decimal("1000"), proposed_notional, 1), None)
    return binance_futures.read_preflight(transport, base_url, market, None, require_flat=require_flat, sleep=sleep)


def _retired_execution_services() -> ExecutionServices:
    transport = _RetiredFuturesTransport()
    return replace(_execution_services(transport, lambda _delay: None), futures_read_market=_retired_futures_market, futures_read_preflight=_retired_futures_preflight)


def _build_execution_services() -> ExecutionServices:
    """Build one per-call adapter table with lazy venue clock sync."""
    if callable(futures_hand.http):
        return _retired_execution_services()
    client = binance_http.BinanceHttp(
        os.environ.get("BINANCE_API_KEY", ""),
        os.environ.get("BINANCE_API_SECRET", ""),
    )
    synchronized = set()
    time_paths = {
        binance_http.SPOT_BASE_URL: "/api/v3/time",
        binance_http.FUTURES_BASE_URL: "/fapi/v1/time",
    }

    def transport(method: str, base_url: str, path: str,
                  params: Any = None, signed: bool = False) \
            -> binance_http.HttpResult:
        if signed and base_url not in synchronized:
            time_path = time_paths.get(base_url)
            if time_path is None:
                return binance_http.HttpResult(
                    "unclear", None, None, "unsupported_base_url"
                )
            synchronized_result = client.sync_time(base_url, time_path)
            if synchronized_result.kind != "ok":
                return synchronized_result
            synchronized.add(base_url)
        return client(method, base_url, path, params, signed)

    return _execution_services(transport, time.sleep)


@dataclass(frozen=True)
class MarginConfirmation:
    verdict: str
    reason: str
    ledger_confirmation: Optional[Dict[str, str]] = None
    account_liability: Optional[Dict[str, str]] = None
    snapshot_time_note: str = ""
    follow_up_repay: Optional[Dict[str, str]] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _canonical_decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _empty_momentum_ledger() -> Dict[str, Any]:
    return {
        "fills": [],
        "fees": [],
        "realized": {
            "gross_usdt": "UNKNOWN",
            "net_usdt": "UNKNOWN",
        },
    }


def _reject_constant(value: str):
    raise ValueError("non-finite JSON number: " + value)


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateField(key)
        result[key] = value
    return result


def _decode_strict_object(encoded: bytes,
                          decimal_numbers: bool = True) -> Dict[str, Any]:
    number_types = {"parse_float": Decimal, "parse_int": Decimal} \
        if decimal_numbers else {}
    try:
        value = json.loads(
            encoded.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
            **number_types,
        )
    except JobRefusal:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise ValueError("JSON object is invalid") from None
    if not isinstance(value, dict):
        raise ValueError("JSON value is not an object")
    return value


def _decode_strict_plain_object(encoded: bytes) -> Dict[str, Any]:
    return _decode_strict_object(encoded, decimal_numbers=False)


def _read_file_bytes(path: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


def _load_strict_object_and_digest(path: str) \
        -> Tuple[Dict[str, Any], str]:
    try:
        encoded = _read_file_bytes(path)
        value = _decode_strict_object(encoded)
    except FileNotFoundError:
        raise
    except JobRefusal:
        raise
    except (OSError, ValueError):
        raise ValueError("JSON object is invalid") from None
    return value, hashlib.sha256(encoded).hexdigest()


def _load_job_with_digest(path: str) -> Tuple[Dict[str, Any], str]:
    try:
        return _load_strict_object_and_digest(path)
    except FileNotFoundError:
        raise JobRefusal("BAD_JSON") from None
    except ValueError:
        raise JobRefusal("BAD_JSON") from None


def _load_job(path: str) -> Dict[str, Any]:
    return _load_job_with_digest(path)[0]


def _timestamp(value: str) -> datetime:
    if not UTC_RE.fullmatch(value):
        raise JobRefusal("BAD_TIMESTAMP")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise JobRefusal("BAD_TIMESTAMP") from None
    return parsed.astimezone(timezone.utc)


def _load_marker_object(path: str) -> Dict[str, Any]:
    try:
        return _load_strict_object_and_digest(path)[0]
    except FileNotFoundError:
        raise
    except (JobRefusal, ValueError):
        raise ValueError("marker JSON is invalid") from None


def _validate_accept_one_object(raw: Dict[str, Any],
                                now: datetime) -> AcceptOne:
    try:
        if set(raw) != ACCEPT_ONE_FIELDS:
            raise ValueError
        if any(type(raw[name]) is not str for name in ACCEPT_ONE_FIELDS):
            raise ValueError
        created = _timestamp(raw["created_utc"])
        expires = _timestamp(raw["expires_utc"])
        maximum = Decimal(raw["max_notional"])
        if not maximum.is_finite() or maximum <= 0:
            raise ValueError
        if expires <= created or expires - created > MAX_JOB_AGE:
            raise ValueError
        if created > now + ACCEPT_ONE_FORWARD_SKEW or now >= expires:
            raise ValueError
    except (JobRefusal, ArithmeticError, ValueError):
        raise JobRefusal("ACCEPT_ONE_INVALID") from None
    return AcceptOne(
        operator=raw["operator"],
        created_utc=created,
        expires_utc=expires,
        max_notional=maximum,
    )


def _load_accept_one(path: str, now: datetime) -> Optional[AcceptOne]:
    try:
        raw = _load_marker_object(path)
    except FileNotFoundError:
        return None
    except ValueError:
        raise JobRefusal("ACCEPT_ONE_INVALID") from None
    return _validate_accept_one_object(raw, now)


def _validate_operator_object(raw: Dict[str, Any]) -> Optional[str]:
    if set(raw) != OPERATOR_FIELDS or type(raw.get("operator")) is not str:
        return None
    return raw["operator"]


def _load_operator(path: str) -> Optional[str]:
    try:
        raw = _load_marker_object(path)
    except (FileNotFoundError, ValueError):
        return None
    return _validate_operator_object(raw)


def _margin_job_projection(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        name: raw[name]
        for name in (
            "op", "asset", "principal", "client_tag", "intent_id",
            "expires_utc", "operator_token",
        )
    }


def _margin_client_tag(op: str, asset: str, principal: str,
                       expires_utc: str) -> str:
    material = json.dumps([op, asset, principal, expires_utc], separators=(",", ":"), ensure_ascii=True)
    return margin_ops.CLIENT_TAG_PREFIX + hashlib.sha256(material.encode("ascii")).hexdigest()[:margin_ops.CLIENT_TAG_HEX_LENGTH]


def _build_margin_job_operation(raw: Dict[str, Any], now: datetime) \
        -> Dict[str, Any]:
    projection = _margin_job_projection(raw)
    if raw.get("op") != margin_ops.MARGIN_REPAY: return margin_ops.build_margin_operation(projection, MARGIN_ASSET_WHITELIST, now)
    principal = raw.get("principal")
    if type(principal) is not str or len(principal) > MARGIN_DECIMAL_TEXT_MAX or MARGIN_DECIMAL_RE.fullmatch(principal) is None: return {"verdict": "REFUSED", "reason": "BAD_PRINCIPAL"}
    try:
        amount = Decimal(principal)
    except ArithmeticError: return {"verdict": "REFUSED", "reason": "BAD_PRINCIPAL"}
    if not amount.is_finite() or amount <= 0: return {"verdict": "REFUSED", "reason": "BAD_PRINCIPAL"}
    canonical, probe = _canonical_decimal_text(amount), dict(projection)
    probe["principal"] = "1"
    try:
        probe["client_tag"] = margin_ops.deterministic_client_tag(margin_ops.MARGIN_REPAY, projection["asset"], "1", projection["expires_utc"])
        operation = margin_ops.build_margin_operation(probe, MARGIN_ASSET_WHITELIST, now)
    except Exception:
        return {"verdict": "REFUSED", "reason": "MARGIN_VALIDATION_UNKNOWN"}
    if operation.get("verdict") != "READY": return operation
    expected_tag = _margin_client_tag(margin_ops.MARGIN_REPAY, projection["asset"], canonical, operation["expires_utc"])
    if type(projection.get("client_tag")) is not str or margin_ops.CLIENT_TAG_RE.fullmatch(projection["client_tag"]) is None: return {"verdict": "REFUSED", "reason": "BAD_CLIENT_TAG"}
    if projection["client_tag"] != expected_tag: return {"verdict": "REFUSED", "reason": "CLIENT_TAG_MISMATCH"}
    operation.update({
        "client_tag": expected_tag,
        "params": {"asset": projection["asset"], "amount": canonical, "type": "REPAY"},
        "expected_ledger_confirmation": {"asset": projection["asset"], "principal": canonical, "interest": "0", "status": "REPAY_CONFIRMED"},
        "evidence": [value for value in operation["evidence"] if value != "principal_ceiling=3"],
    })
    return operation


def _validate_margin_job(raw: Dict[str, Any], filename_id: str,
                         now: datetime,
                         prebuilt_operation: Optional[Dict[str, Any]] = None) \
        -> Job:
    unknown = set(raw) - MARGIN_JOB_FIELDS
    if unknown:
        trading_fields = TRADING_JOB_FIELDS | set(
            margin_ops.FORBIDDEN_JOB_FIELDS
        )
        reason = "FORBIDDEN_FIELD" if unknown & trading_fields \
            else "UNKNOWN_FIELD"
        raise JobRefusal(reason)
    missing = MARGIN_JOB_REQUIRED_FIELDS - set(raw)
    if missing:
        raise JobRefusal("MISSING_FIELD")

    common_string_fields = (
        "job_id", "op", "created_utc", "expires_utc", "requested_by"
    )
    if any(type(raw[name]) is not str for name in common_string_fields):
        raise JobRefusal("BAD_TYPE")
    if not JOB_ID_RE.fullmatch(raw["job_id"]) \
            or raw["job_id"] != filename_id:
        raise JobRefusal("BAD_JOB_ID")
    if raw["op"] not in MARGIN_OPS:
        raise JobRefusal("BAD_OP")
    if type(raw["intent_id"]) is not str \
            or margin_ops.INTENT_ID_RE.fullmatch(raw["intent_id"]) is None:
        raise JobRefusal("BAD_INTENT_ID")
    if not raw["requested_by"].strip() \
            or len(raw["requested_by"]) > 128:
        raise JobRefusal("BAD_REQUESTER")

    created = _timestamp(raw["created_utc"])
    expires = _timestamp(raw["expires_utc"])
    if expires <= created or expires - created > MAX_JOB_AGE:
        raise JobRefusal("BAD_EXPIRY_WINDOW")
    if now >= expires:
        raise JobRefusal("EXPIRED")

    if prebuilt_operation is None:
        try:
            operation = _build_margin_job_operation(raw, now)
        except Exception:
            raise JobRefusal("MARGIN_VALIDATION_UNKNOWN") from None
    else:
        operation = prebuilt_operation
    if not isinstance(operation, dict):
        raise JobRefusal("MARGIN_VALIDATION_UNKNOWN")
    if operation.get("verdict") == "REFUSED":
        reason = operation.get("reason")
        if type(reason) is not str or not reason:
            reason = "MARGIN_VALIDATION_UNKNOWN"
        raise JobRefusal(reason)
    try:
        requested = Decimal(raw["principal"])
    except (ArithmeticError, TypeError, ValueError):
        raise JobRefusal("BAD_PRINCIPAL") from None
    if not requested.is_finite() or requested <= 0:
        raise JobRefusal("BAD_PRINCIPAL")
    if raw["op"] == margin_ops.MARGIN_BORROW \
            and requested > MARGIN_RUNNER_NOTIONAL_CAP:
        raise JobRefusal("PRINCIPAL_CEILING_EXCEEDED")

    principal = _canonical_decimal_text(requested)
    request_type = "BORROW" if raw["op"] == margin_ops.MARGIN_BORROW \
        else "REPAY"
    expected_confirmation = {
        "asset": raw["asset"],
        "principal": principal,
        "interest": "0",
        "status": request_type + "_CONFIRMED",
    }
    if set(operation) != {
            "verdict", "op", "client_tag", "intent_id", "expires_utc",
            "endpoint_path", "params", "expected_ledger_confirmation",
            "evidence"} \
            or operation.get("verdict") != "READY" \
            or operation.get("op") != raw["op"] \
            or operation.get("client_tag") != raw["client_tag"] \
            or operation.get("intent_id") != raw["intent_id"] \
            or operation.get("expires_utc") != _utc_text(expires) \
            or operation.get("endpoint_path") != MARGIN_ENDPOINT_PATH \
            or operation.get("params") != {
                "asset": raw["asset"],
                "amount": principal,
                "type": request_type,
            } \
            or operation.get("expected_ledger_confirmation") \
            != expected_confirmation \
            or not isinstance(operation.get("evidence"), list):
        raise JobRefusal("MARGIN_VALIDATION_UNKNOWN")
    return Job(
        job_id=raw["job_id"],
        op=raw["op"],
        symbol=raw["asset"],
        requested_max_notional=requested,
        effective_notional=requested,
        created_utc=created,
        expires_utc=expires,
        requested_by=raw["requested_by"],
        operator_token=raw["operator_token"],
        intent_id=raw["intent_id"],
        margin_operation=operation,
    )


def _validate_job(raw: Dict[str, Any], filename_id: str,
                  now: datetime, receipt_dir: str = "") -> Job:
    if type(raw.get("op")) is str and raw["op"] in MARGIN_OPS:
        return _validate_margin_job(raw, filename_id, now)

    unknown = set(raw) - JOB_FIELDS
    if unknown:
        raise JobRefusal("UNKNOWN_FIELD")
    missing = JOB_REQUIRED_FIELDS - set(raw)
    if missing:
        raise JobRefusal("MISSING_FIELD")

    string_fields = (
        "job_id", "op", "symbol", "created_utc", "expires_utc", "requested_by"
    )
    if any(type(raw[name]) is not str for name in string_fields):
        raise JobRefusal("BAD_TYPE")
    if type(raw["max_notional"]) is not Decimal:
        raise JobRefusal("BAD_TYPE")
    if not JOB_ID_RE.fullmatch(raw["job_id"]) or raw["job_id"] != filename_id:
        raise JobRefusal("BAD_JOB_ID")
    if raw["op"] not in OPS:
        raise JobRefusal("BAD_OP")
    venue = raw.get("venue", "futures")
    if type(venue) is not str:
        raise JobRefusal("BAD_TYPE")
    if "venue" in raw and (
            raw["op"] != "MOMENTUM_OPEN" or venue != "spot"):
        raise JobRefusal("BAD_VENUE")
    if not SYMBOL_RE.fullmatch(raw["symbol"]):
        raise JobRefusal("BAD_SYMBOL")
    if not raw["requested_by"].strip() or len(raw["requested_by"]) > 128:
        raise JobRefusal("BAD_REQUESTER")

    requested = raw["max_notional"]
    if not requested.is_finite() or requested <= 0:
        raise JobRefusal("BAD_NOTIONAL")
    created = _timestamp(raw["created_utc"])
    expires = _timestamp(raw["expires_utc"])
    if expires <= created or expires - created > MAX_JOB_AGE:
        raise JobRefusal("BAD_EXPIRY_WINDOW")
    if now >= expires:
        raise JobRefusal("EXPIRED")
    notional_cap = limits.FIRST_TRADE_CAP_USDT
    if receipt_dir and os.path.isfile(os.path.join(
            receipt_dir, FIRST_AUTONOMOUS_CLOSE_MARKER)):
        notional_cap = limits.PER_LEG_CAP_USDT
    return Job(
        job_id=raw["job_id"],
        op=raw["op"],
        symbol=raw["symbol"],
        requested_max_notional=requested,
        effective_notional=min(requested, notional_cap),
        created_utc=created,
        expires_utc=expires,
        requested_by=raw["requested_by"],
        venue=venue,
    )


def _safe_text(raw: Dict[str, Any], name: str, limit: int = 64) -> str:
    value = raw.get(name, "") if isinstance(raw, dict) else ""
    if type(value) is not str:
        return ""
    return " ".join(value.split())[:limit]


def _paths(receipt_dir: str) -> Dict[str, str]:
    jobs_dir = os.path.join(receipt_dir, "jobs")
    return {
        "jobs": jobs_dir,
        "claims": os.path.join(jobs_dir, "claims"),
        "receipts": os.path.join(jobs_dir, "receipts"),
        "results": os.path.join(jobs_dir, "results.jsonl"),
        "proof": os.path.join(jobs_dir, "futures_proof_passed"),
        "basis_proof": os.path.join(jobs_dir, "basis_proof_passed"),
        "first_autonomous_close": os.path.join(
            receipt_dir, FIRST_AUTONOMOUS_CLOSE_MARKER
        ),
        "momentum_position": os.path.join(
            receipt_dir, "momentum-position.json"
        ),
        "stop": os.path.join(receipt_dir, "STOP-JOBS"),
        "accept_one": os.path.join(receipt_dir, "ACCEPT-ONE"),
        "operator": os.path.join(receipt_dir, "OPERATOR"),
        "tick_lock": os.path.join(jobs_dir, ".job-runner-active"),
    }


def _make_dirs(paths: Dict[str, str]) -> None:
    for name in ("jobs", "claims", "receipts"):
        os.makedirs(paths[name], mode=0o700, exist_ok=True)


def _fsync_parent_directory(path: str, best_effort: bool = False) -> None:
    """Make one created or replaced directory entry durable."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(os.path.dirname(os.path.abspath(path)), flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        if not best_effort:
            raise


def _persist_json_receipt(path: str, payload: Dict[str, Any],
                          create: bool = False) -> None:
    """Persist one JSON receipt and its directory entry before returning."""
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if create:
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_parent_directory(path)
        return
    temporary = path + ".tmp-" + secrets.token_hex(6)
    try:
        with open(temporary, "xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        _fsync_parent_directory(temporary)
        os.replace(temporary, path)
        _fsync_parent_directory(path)
    finally:
        removed = False
        try:
            os.unlink(temporary)
            removed = True
        except FileNotFoundError:
            pass
        if removed:
            _fsync_parent_directory(temporary, best_effort=True)


def _touch_stop(path: str) -> None:
    created = False
    try:
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        created = True
    except FileExistsError:
        descriptor = os.open(path, os.O_WRONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    if created:
        _fsync_parent_directory(path)


def _entry_ticket_refusal(job: Job, paths: Dict[str, str],
                          now: datetime) -> str:
    try:
        acceptance = _load_accept_one(paths["accept_one"], now)
    except JobRefusal as error:
        return error.reason
    if acceptance is None:
        return "ENTRY_BLOCKED_NOT_ACCEPTED"
    operator = _load_operator(paths["operator"])
    if operator is None or job.requested_by != operator \
            or acceptance.operator != operator:
        return "OPERATOR_MISMATCH"
    if job.op == margin_ops.MARGIN_BORROW \
            and job.operator_token != operator:
        return "OPERATOR_MISMATCH"
    if job.requested_max_notional > acceptance.max_notional:
        return "ACCEPT_ONE_NOTIONAL_EXCEEDED"
    return ""


def _consume_accept_one(path: str, job_id: str) -> bool:
    """Atomically rename one filled entry ticket to its job claim."""
    if type(job_id) is not str or not JOB_ID_RE.fullmatch(job_id):
        return False
    claim_path = path + ".claimed-" + job_id
    if os.path.lexists(claim_path):
        return False
    try:
        os.rename(path, claim_path)
    except OSError:
        return False
    try:
        directory = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        pass
    return True


def _expire_accept_one(path: str, now: datetime) -> bool:
    """Atomically retire any ticket standing when a lifecycle seals flat."""
    expired_path = os.path.join(
        os.path.dirname(path), "ACCEPT-ONE.expired-" + _utc_text(now)
    )
    try:
        os.rename(path, expired_path)
    except OSError:
        return False
    try:
        directory = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        pass
    return True


def _account_governor_api():
    """Resolve the sibling governor only at a money-entry boundary."""
    try:
        import account_governor
        from account_governor import (
            acquire_entry_slot,
            budgets_allow,
            record_realized,
            release_entry_slot,
        )
    except ModuleNotFoundError as error:
        reason = "GOVERNOR_ABSENT" \
            if error.name == "account_governor" else "GOVERNOR_ERROR"
        raise JobRefusal(reason) from None
    except (ImportError, AttributeError):
        raise JobRefusal("GOVERNOR_ERROR") from None
    except Exception:
        raise JobRefusal("GOVERNOR_ERROR") from None
    governor_refusal = getattr(account_governor, "GovernorRefusal", None)
    if not isinstance(governor_refusal, type) \
            or not issubclass(governor_refusal, Exception):
        raise JobRefusal("GOVERNOR_ABSENT")
    return (
        acquire_entry_slot,
        release_entry_slot,
        record_realized,
        budgets_allow,
        governor_refusal,
    )


def _governor_reason(value: Any, fallback: str) -> str:
    if type(value) is str and value.strip():
        return " ".join(value.split())[:128]
    return fallback


def _acquire_governor(job: Job, receipt_dir: str) \
        -> Tuple[str, Optional[Callable[..., Any]], str]:
    try:
        acquire, release, _record, budgets, refusal_type = \
            _account_governor_api()
    except JobRefusal as error:
        return "", None, error.reason
    try:
        budget_result = budgets(receipt_dir)
    except refusal_type as error:
        return "", None, _governor_reason(
            getattr(error, "reason", ""), "GOVERNOR_REFUSAL"
        )
    except Exception:
        return "", None, "GOVERNOR_ERROR"
    if type(budget_result) is not tuple or len(budget_result) != 2 \
            or type(budget_result[0]) is not bool:
        return "", None, "GOVERNOR_ERROR"
    allowed, reason = budget_result
    if not allowed:
        return "", None, _governor_reason(reason, "GOVERNOR_REFUSAL")
    try:
        token = acquire(job.op, job.symbol, receipt_dir)
    except refusal_type as error:
        return "", None, _governor_reason(
            getattr(error, "reason", ""), "GOVERNOR_REFUSAL"
        )
    except Exception:
        return "", None, "GOVERNOR_ERROR"
    if type(token) is not str or not token:
        return "", None, "GOVERNOR_ERROR"
    return token, release, ""


def _release_governor(release: Optional[Callable[..., Any]], token: str,
                      receipt_dir: str) -> bool:
    if release is None or not token:
        return False
    try:
        release(token, receipt_dir)
    except Exception:
        return False
    return True


def _decode_tick_lock(encoded: bytes) -> Dict[str, Any]:
    try:
        encoded.decode("ascii")
        owner = _decode_strict_plain_object(encoded)
    except (JobRefusal, UnicodeError, ValueError, json.JSONDecodeError):
        raise ValueError("runner lock is invalid") from None
    return owner


def _process_start_marker(pid: int) -> Optional[str]:
    """Read one stable process-start marker from the local operating system."""
    if type(pid) is not int or pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            encoded = _read_file_bytes("/proc/%s/stat" % pid)
            text = encoded.decode("ascii")
            closing = text.rfind(")")
            fields = text[closing + 2:].split() if closing >= 0 else []
            start_ticks = fields[19]
            if not start_ticks.isdigit():
                return None
            return "linux:" + start_ticks
        except (IndexError, OSError, UnicodeError):
            return None
    if sys.platform == "darwin":
        class _ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        try:
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            info = _ProcBsdInfo()
            size = ctypes.sizeof(info)
            read_size = library.proc_pidinfo(
                pid, 3, 0, ctypes.byref(info), size
            )
        except (AttributeError, OSError):
            return None
        if read_size != size or info.pbi_pid != pid \
                or info.pbi_start_tvsec <= 0:
            return None
        return "darwin:%s:%s" % (
            info.pbi_start_tvsec, info.pbi_start_tvusec
        )
    return None


def _valid_tick_lock(owner: Dict[str, Any]) -> bool:
    return set(owner) == TICK_LOCK_FIELDS \
        and type(owner.get("pid")) is int \
        and owner["pid"] > 0 \
        and type(owner.get("process_start")) is str \
        and bool(owner["process_start"]) \
        and len(owner["process_start"]) <= 128 \
        and type(owner.get("heartbeat_monotonic_ns")) is int \
        and owner["heartbeat_monotonic_ns"] >= 0 \
        and type(owner.get("heartbeat_utc")) is str \
        and type(owner.get("created_utc")) is str \
        and type(owner.get("token")) is str \
        and bool(owner["token"])


def _create_tick_lock(path: str, now: datetime) -> str:
    token = secrets.token_hex(16)
    pid = os.getpid()
    process_start = _process_start_marker(pid)
    if process_start is None:
        raise OSError("runner process start is unavailable")
    payload = {
        "pid": pid,
        "process_start": process_start,
        "heartbeat_monotonic_ns": time.monotonic_ns(),
        "heartbeat_utc": _utc_text(now),
        "created_utc": _utc_text(now),
        "token": token,
    }
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    created = False
    try:
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        created = True
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("runner lock write was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_parent_directory(path)
    except OSError:
        if created:
            try:
                os.unlink(path)
                _fsync_parent_directory(path, best_effort=True)
            except OSError:
                pass
        raise
    return token


def _refresh_tick_lock(path: str, token: str, now: datetime) -> bool:
    """Refresh the heartbeat only while this process still owns the lock."""
    try:
        owner = _decode_tick_lock(_read_file_bytes(path))
        process_start = _process_start_marker(os.getpid())
        if not _valid_tick_lock(owner) \
                or owner.get("token") != token \
                or owner.get("pid") != os.getpid() \
                or process_start is None \
                or owner.get("process_start") != process_start:
            return False
        owner["heartbeat_monotonic_ns"] = time.monotonic_ns()
        owner["heartbeat_utc"] = _utc_text(now)
        _persist_json_receipt(path, owner)
    except (OSError, ValueError):
        return False
    return True


def _acquire_tick_lock(path: str, now: datetime) \
        -> Tuple[str, bool, Optional[Dict[str, str]]]:
    recovered = None
    for attempt in range(2):
        try:
            return _create_tick_lock(path, now), False, recovered
        except FileExistsError:
            if attempt:
                return "", False, None
        try:
            encoded = _read_file_bytes(path)
        except FileNotFoundError:
            continue
        except OSError:
            return "", True, None
        try:
            owner = _decode_tick_lock(encoded)
            if not _valid_tick_lock(owner):
                raise ValueError("runner lock is invalid")
            created = _timestamp(owner["created_utc"])
            heartbeat = _timestamp(owner["heartbeat_utc"])
        except (JobRefusal, ValueError):
            try:
                if _read_file_bytes(path) != encoded:
                    return "", False, None
                os.unlink(path)
                _fsync_parent_directory(path)
            except FileNotFoundError:
                continue
            except OSError:
                return "", True, None
            recovered = {
                "kind": "runner_stale_lock_recovered",
                "pid": "UNKNOWN",
                "age_seconds": "UNKNOWN",
                "utc": _utc_text(now),
            }
            continue

        current_monotonic = time.monotonic_ns()
        if current_monotonic >= owner["heartbeat_monotonic_ns"]:
            age = Decimal(
                current_monotonic - owner["heartbeat_monotonic_ns"]
            ) / Decimal("1000000000")
        else:
            age = Decimal(str((now - heartbeat).total_seconds()))
        if created > heartbeat or age < 0:
            return "", True, None

        pid_alive = True
        try:
            os.kill(owner["pid"], 0)
        except PermissionError:
            pass
        except ProcessLookupError:
            pid_alive = False
        except OSError:
            return "", True, None
        if pid_alive:
            current_start = _process_start_marker(owner["pid"])
            if current_start == owner["process_start"]:
                return "", False, None
            if current_start is None:
                stale_after = Decimal(str(TICK_INTERVAL_SECONDS)) \
                    * TICK_LOCK_HEARTBEAT_INTERVALS
                if age <= stale_after:
                    return "", False, None
        try:
            if _read_file_bytes(path) != encoded:
                return "", False, None
            os.unlink(path)
            _fsync_parent_directory(path)
        except FileNotFoundError:
            continue
        except OSError:
            return "", True, None
        recovered = {
            "kind": "runner_stale_lock_recovered",
            "pid": str(owner["pid"]),
            "age_seconds": _canonical_decimal_text(age),
            "utc": _utc_text(now),
        }
    return "", False, None


def _release_tick_lock(path: str, token: str) -> None:
    if not token:
        return
    try:
        current = _decode_tick_lock(_read_file_bytes(path))
        if current.get("token") == token:
            os.unlink(path)
            _fsync_parent_directory(path, best_effort=True)
    except (OSError, ValueError):
        pass


def _valid_margin_legacy_exclusivity_proof(proof: Any) -> bool:
    try:
        if type(proof) is not dict \
                or set(proof) != MARGIN_LEGACY_EXCLUSIVITY_PROOF_FIELDS \
                or proof.get("kind") != "LEGACY_EXCLUSIVITY_PROOF" \
                or type(proof.get("window_start_utc")) is not str \
                or type(proof.get("window_end_utc")) is not str \
                or type(proof.get("row_count")) is not int \
                or proof["row_count"] != 1:
            return False
        return _timestamp(proof["window_start_utc"]) \
            <= _timestamp(proof["window_end_utc"])
    except Exception:
        return False


def _legacy_current_liability_matches(
        liability: Any, principal: Any) -> bool:
    """Match the legacy current borrowed amount to its recorded principal."""
    try:
        values = _margin_liability_decimals(liability)
        canonical_principal = _margin_decimal_text(principal)
        if values is None or canonical_principal is None:
            return False
        _asset, borrowed, interest, _total = values
        return borrowed == Decimal(canonical_principal) \
            and interest >= Decimal("0")
    except Exception:
        return False


def _valid_margin_legacy_proof_row(row: Dict[str, Any]) -> bool:
    try:
        proof = row.get("legacy_exclusivity_proof")
        return set(row) == MARGIN_LEGACY_PROOF_FIELDS \
            and row.get("kind") == "LEGACY_PROOF" \
            and all(type(row.get(name)) is str for name in (
                "job_id", "transaction_id", "history_watermark_substitute",
                "account_liability_before_substitute", "utc",
            )) \
            and JOB_ID_RE.fullmatch(row["job_id"]) is not None \
            and _margin_transaction_number(row["transaction_id"]) \
            is not None \
            and row["history_watermark_substitute"] \
            == LEGACY_HISTORY_WATERMARK_SUBSTITUTE \
            and row["account_liability_before_substitute"] \
            == LEGACY_ACCOUNT_LIABILITY_BEFORE_SUBSTITUTE \
            and _valid_margin_legacy_exclusivity_proof(proof) \
            and proof["window_end_utc"] == row["utc"]
    except Exception:
        return False


def _valid_margin_reconciled_row(row: Dict[str, Any]) -> bool:
    try:
        fields = set(row)
        legacy = fields == MARGIN_LEGACY_RECONCILED_FIELDS
        legacy_repay = fields == MARGIN_LEGACY_REPAY_RECONCILED_FIELDS
        transaction_number = _margin_transaction_number(
            row.get("transaction_id")
        )
        if fields not in (
                MARGIN_RECONCILED_FIELDS,
                MARGIN_LEGACY_RECONCILED_FIELDS,
                MARGIN_LEGACY_REPAY_RECONCILED_FIELDS) \
                or row.get("kind") != "RECONCILED_FROM_EXCHANGE_TRUTH" \
                or any(type(row.get(name)) is not str for name in (
                    "job_id", "op", "asset", "verdict", "reason",
                    "transaction_id", "snapshot_time_note", "utc",
                )) \
                or JOB_ID_RE.fullmatch(row["job_id"]) is None \
                or row["op"] not in MARGIN_OPS \
                or row["asset"] not in MARGIN_ASSET_WHITELIST \
                or not row["reason"] \
                or transaction_number is None \
                or row["snapshot_time_note"] not in {
                    MARGIN_SNAPSHOT_TIME_VERIFIED,
                    MARGIN_SNAPSHOT_TIME_UNVERIFIED,
                }:
            return False
        if legacy:
            proof = row.get("legacy_exclusivity_proof")
            if not _valid_margin_legacy_exclusivity_proof(proof) \
                    or proof["window_end_utc"] != row["utc"] \
                    or row["snapshot_time_note"] \
                    != MARGIN_SNAPSHOT_TIME_UNVERIFIED:
                return False
        elif type(row.get("history_watermark")) is not int \
                or row["history_watermark"] < 0 \
                or transaction_number <= row["history_watermark"]:
            return False
        if legacy_repay and (
                row.get("receipt_shape") != LEGACY_REPAY_SHAPE
                or type(row.get("receipt_shape_version")) is not int
                or row["receipt_shape_version"]
                != LEGACY_REPAY_SHAPE_VERSION):
            return False
        request_type = "BORROW" \
            if row["op"] == margin_ops.MARGIN_BORROW else "REPAY"
        if row["verdict"] not in (
                {"OPEN"} if request_type == "BORROW"
                else {"CLOSED", "CLOSED-WITH-DUST"}):
            return False
        expected = row.get("ledger_confirmation")
        expected_fields = {"asset", "principal", "interest", "status"} \
            if request_type == "BORROW" else {
                "asset", "amount", "principal", "interest", "status",
            }
        if type(expected) is not dict or set(expected) != expected_fields \
                or any(type(expected.get(name)) is not str
                       for name in expected) \
                or expected["asset"] != row["asset"] \
                or expected["status"] != request_type + "_CONFIRMED":
            return False
        liability_names = ("account_liability",) if legacy else (
            "account_liability_before", "account_liability",
        )
        for name in liability_names:
            liability = row.get(name)
            if type(liability) is not dict \
                    or _margin_liability_from_receipt(
                        liability, row["asset"]
                    ) is None:
                return False
        after = row["account_liability"]
        if legacy:
            return request_type == "BORROW" \
                and row["reason"] == expected["status"] \
                and _legacy_current_liability_matches(
                    after, expected["principal"]
                )
        before = row["account_liability_before"]
        if request_type == "BORROW":
            if row["reason"] != expected["status"] \
                    or not _margin_liability_direction_confirmed(
                        before, after, expected["principal"], request_type
                    ):
                return False
        else:
            before_values = _margin_liability_decimals(before)
            amount = _margin_decimal_text(expected["amount"])
            principal = _margin_decimal_text(expected["principal"])
            interest = _margin_decimal_text(expected["interest"])
            if before_values is None \
                    or amount is None or principal is None or interest is None \
                    or Decimal(principal) + Decimal(interest) \
                    != Decimal(amount) \
                    or Decimal(principal) != before_values[1] \
                    or Decimal(interest) != before_values[2]:
                return False
            close_verdict = _margin_repay_close_verdict(
                before, after, amount
            )
            expected_reason = "REPAY_CONFIRMED_WITH_DUST" \
                if close_verdict == "CLOSED-WITH-DUST" \
                else expected["status"]
            if close_verdict != row["verdict"] \
                    or row["reason"] != expected_reason:
                return False
            if legacy_repay:
                after_values = _margin_liability_decimals(after)
                if after_values is None \
                        or after_values[1] != Decimal("0") \
                        or after_values[2] != Decimal("0") \
                        or row["verdict"] != "CLOSED":
                    return False
        return True
    except Exception:
        return False


def _result_ids(path: str) -> Tuple[set, bool, bool]:
    if not os.path.exists(path):
        return set(), False, False
    try:
        with open(path, encoding="utf-8") as handle:
            encoded_rows = [line for line in handle]
        if any(not line.strip() for line in encoded_rows):
            return set(), True, False
        rows = [
            _decode_strict_plain_object(line.encode("utf-8"))
            for line in encoded_rows
        ]
    except (JobRefusal, OSError, ValueError, UnicodeError, RecursionError):
        return set(), True, False

    reconciled: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    legacy_proofs: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        if set(row) == MARGIN_LEGACY_PROOF_FIELDS:
            job_id = row.get("job_id")
            if not _valid_margin_legacy_proof_row(row) \
                    or job_id in legacy_proofs:
                return set(), True, False
            legacy_proofs[job_id] = (index, row)
            continue
        if set(row) not in (
                MARGIN_RECONCILED_FIELDS,
                MARGIN_LEGACY_RECONCILED_FIELDS,
                MARGIN_LEGACY_REPAY_RECONCILED_FIELDS):
            continue
        if not _valid_margin_reconciled_row(row) \
                or row["job_id"] in reconciled:
            return set(), True, False
        reconciled[row["job_id"]] = (index, row)

    result_rows: Dict[str, List[Tuple[int, Dict[str, Any]]]] = {}
    for index, row in enumerate(rows):
        fields = set(row)
        if fields == ADVERSE_STOP_REFUSAL_FIELDS:
            if row["kind"] != "adverse_stop_refusal" or any(
                    type(row[name]) is not str for name in fields):
                return set(), True, False
            continue
        if fields == ADVERSE_STOP_BLOCKED_FIELDS:
            if row["kind"] != "adverse_stop_blocked_unknown" or any(
                    type(row[name]) is not str for name in fields):
                return set(), True, False
            continue
        if fields == TICK_LOCK_RECOVERY_FIELDS:
            if row.get("kind") != "runner_stale_lock_recovered" \
                    or any(type(row.get(name)) is not str
                           for name in fields) \
                    or not (
                        row["pid"] == "UNKNOWN"
                        and row["age_seconds"] == "UNKNOWN"
                        or row["pid"].isdigit()
                        and _margin_decimal_text(
                            row["age_seconds"]
                        ) is not None
                    ):
                return set(), True, False
            continue
        if fields == MARGIN_GOVERNOR_HALT_FIELDS:
            if row.get("kind") != "margin_governor_release_halted" \
                    or type(row.get("job_id")) is not str \
                    or any(type(row.get(name)) is not bool for name in (
                        "lock_exists", "runner_receipt_exists",
                        "source_receipt_exists",
                    )) \
                    or any(type(row.get(name)) is not str for name in (
                        "lock_token", "lock_op", "lock_symbol",
                        "runner_verdict", "source_verdict", "utc",
                    )):
                return set(), True, False
            continue
        if fields == MARGIN_GOVERNOR_ANOMALY_FIELDS:
            if row.get("kind") != "margin_governor_artifact_halted" \
                    or type(row.get("lock_exists")) is not bool \
                    or any(type(row.get(name)) is not str for name in (
                        "job_id", "artifact", "field", "anomaly_type",
                        "utc",
                    )):
                return set(), True, False
            continue
        if fields in (
                MARGIN_RECONCILED_FIELDS,
                MARGIN_LEGACY_RECONCILED_FIELDS,
                MARGIN_LEGACY_REPAY_RECONCILED_FIELDS):
            continue
        if fields == MARGIN_LEGACY_PROOF_FIELDS:
            continue
        if fields == MARGIN_RECONCILE_UNCONFIRMED_FIELDS:
            if row.get("kind") != "RECONCILE_UNCONFIRMED" \
                    or any(type(row.get(name)) is not str for name in fields) \
                    or JOB_ID_RE.fullmatch(row["job_id"]) is None \
                    or not row["reason"]:
                return set(), True, False
            continue
        if fields not in (RESULT_FIELDS, MOMENTUM_RESULT_FIELDS) \
                or type(row.get("job_id")) is not str \
                or JOB_ID_RE.fullmatch(row["job_id"]) is None \
                or type(row.get("exit_code")) is not int \
                or any(type(row.get(name)) is not str for name in (
                    "op", "symbol", "verdict", "receipt_path", "utc",
                )):
            return set(), True, False
        if fields == MOMENTUM_RESULT_FIELDS and (
                row["op"] not in MOMENTUM_OPS
                or not isinstance(row["fills"], list)
                or not isinstance(row["fees"], list)
                or not isinstance(row["realized"], dict)):
            return set(), True, False
        result_rows.setdefault(row["job_id"], []).append((index, row))

    identifiers = set()
    unresolved = False
    for job_id, entries in result_rows.items():
        reconciliation = reconciled.get(job_id)
        if reconciliation is None:
            if len(entries) != 1:
                return set(), True, False
            current = entries[0][1]
        else:
            if len(entries) != 2:
                return set(), True, False
            first_index, first = entries[0]
            final_index, current = entries[1]
            event_index, event = reconciliation
            proof_entry = legacy_proofs.get(job_id)
            legacy_event = set(event) == MARGIN_LEGACY_RECONCILED_FIELDS
            if legacy_event:
                proof_matches = proof_entry is not None \
                    and event_index < proof_entry[0] < final_index \
                    and proof_entry[1]["transaction_id"] \
                    == event["transaction_id"] \
                    and proof_entry[1]["legacy_exclusivity_proof"] \
                    == event["legacy_exclusivity_proof"]
            else:
                proof_matches = proof_entry is None
            if not first_index < event_index < final_index \
                    or not proof_matches \
                    or set(first) != RESULT_FIELDS \
                    or set(current) != RESULT_FIELDS \
                    or (first["verdict"], first["exit_code"]) \
                    != ("UNKNOWN", 4) \
                    or (current["verdict"], current["exit_code"]) \
                    != (event["verdict"], 0) \
                    or any(first[name] != current[name] for name in (
                        "job_id", "op", "symbol", "receipt_path",
                    )) \
                    or current["op"] != event["op"] \
                    or current["symbol"] != event["asset"]:
                return set(), True, False
        receipt_path = current["receipt_path"]
        if not receipt_path or not os.path.isfile(receipt_path):
            return set(), True, False
        try:
            receipt = _decode_strict_plain_object(
                _read_file_bytes(receipt_path)
            )
        except (JobRefusal, OSError, ValueError, UnicodeError,
                RecursionError):
            return set(), True, False
        if not isinstance(receipt, dict) \
                or receipt.get("verdict") != current["verdict"]:
            return set(), True, False
        unresolved = unresolved or _receipt_is_unresolved(receipt_path)
        identifiers.add(job_id)
    if any(job_id not in result_rows for job_id in reconciled) \
            or any(job_id not in reconciled for job_id in legacy_proofs):
        return set(), True, False
    return identifiers, False, unresolved


def _claims_are_resolved(paths: Dict[str, str], result_ids: set) -> bool:
    for path in glob.glob(os.path.join(paths["claims"], "job-*.claim")):
        name = os.path.basename(path)
        identifier = name[len("job-"):-len(".claim")]
        if identifier not in result_ids:
            return False
    return True


def _receipt_is_unresolved(path: str) -> bool:
    receipt = _load_plain_receipt(path)
    if not isinstance(receipt, dict) or type(receipt.get("verdict")) is not str:
        return True
    verdict = receipt["verdict"]
    if verdict == "OPEN_EXPOSURE" and receipt.get("reconciled_by"):
        note = receipt.get("reconciled_note", "")
        if type(note) is not str:
            return True
        return note.startswith("dust_accepted")
    return verdict in FREEZE_VERDICTS


def _unresolved_account_state(receipt_dir: str) -> bool:
    if glob.glob(os.path.join(receipt_dir, "*.lock")):
        return True
    seen = set()
    for pattern in RECEIPT_PATTERNS:
        for path in glob.glob(os.path.join(receipt_dir, pattern)):
            if path not in seen and _receipt_is_unresolved(path):
                return True
            seen.add(path)
    return False


def _claim(paths: Dict[str, str], job_id: str, now: datetime) -> bool:
    path = os.path.join(paths["claims"], "job-%s.claim" % job_id)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    body = json.dumps({"job_id": job_id, "claimed_utc": _utc_text(now)},
                      separators=(",", ":")) + "\n"
    try:
        os.write(descriptor, body.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_parent_directory(path)
    return True


def _margin_intent_claim_path(paths: Dict[str, str], intent_id: str) -> str:
    return os.path.join(paths["claims"], "intent-%s.claim" % intent_id)


def _margin_intent_is_claimed(paths: Dict[str, str], intent_id: str) -> bool:
    return type(intent_id) is str \
        and margin_ops.INTENT_ID_RE.fullmatch(intent_id) is not None \
        and os.path.isfile(_margin_intent_claim_path(paths, intent_id))


def _claim_margin_intent(paths: Dict[str, str], intent_id: str,
                         job_id: str, now: datetime) -> bool:
    if type(intent_id) is not str \
            or margin_ops.INTENT_ID_RE.fullmatch(intent_id) is None:
        return False
    path = _margin_intent_claim_path(paths, intent_id)
    try:
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
    except FileExistsError:
        return False
    body = json.dumps({
        "intent_id": intent_id,
        "job_id": job_id,
        "claimed_utc": _utc_text(now),
    }, separators=(",", ":")) + "\n"
    try:
        os.write(descriptor, body.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_parent_directory(path)
    return True


def _runner_authority_digests(source_path: str,
                              validated_job_digest: str) \
        -> Dict[str, str]:
    digests: Dict[str, str] = {}
    if source_path:
        source = _load_plain_receipt(source_path)
        recorded = source.get("authority_digests") \
            if isinstance(source, dict) else None
        if isinstance(recorded, dict) and all(
                type(name) is str
                and type(value) is str
                and re.fullmatch(r"[0-9a-f]{64}", value) is not None
                for name, value in recorded.items()):
            digests.update(recorded)
    if type(validated_job_digest) is str \
            and re.fullmatch(r"[0-9a-f]{64}", validated_job_digest):
        digests["job_file"] = validated_job_digest
    return digests


def _write_runner_receipt(paths: Dict[str, str], job_id: str, op: str,
                          symbol: str, verdict: str, reason: str,
                          now: datetime, source_path: str = "",
                          ledger: Optional[Dict[str, Any]] = None,
                          governor_token: str = "",
                          validated_job_digest: str = "") -> str:
    path = os.path.join(paths["receipts"], "job-%s.json" % job_id)
    measured_sha = os.environ.get("QUANT_SHA", "")
    sha_valid = SHA_RE.fullmatch(measured_sha.lower()) is not None
    payload = {
        "job_id": job_id,
        "op": op,
        "symbol": symbol,
        "verdict": verdict,
        "reason": reason,
        "utc": _utc_text(now),
        "measured_sha": measured_sha,
        "sha_valid": sha_valid,
    }
    if source_path:
        payload["source_receipt_path"] = source_path
    if op in MARGIN_OPS:
        authority_digests = _runner_authority_digests(
            source_path, validated_job_digest
        )
        if authority_digests:
            payload["authority_digests"] = authority_digests
        if not source_path:
            payload["mutation_attempts"] = 0
        if sha_valid:
            payload["sha"] = measured_sha.lower()
        if governor_token:
            payload["governor_token"] = governor_token
            payload["governor_release"] = "PENDING"
    if ledger is not None:
        payload.update({
            "fills": list(ledger.get("fills", [])),
            "fees": list(ledger.get("fees", [])),
            "realized": dict(ledger.get("realized", {})),
        })
    _persist_json_receipt(path, payload)
    return path


def _append_results_row(paths: Dict[str, str], row: Dict[str, Any]) -> None:
    separator = ""
    try:
        existing = _read_file_bytes(paths["results"])
        if existing and not existing.endswith(b"\n"):
            separator = "\n"
    except (FileNotFoundError, OSError):
        pass
    line = separator + json.dumps(row, separators=(",", ":")) + "\n"
    created = False
    try:
        descriptor = os.open(
            paths["results"],
            os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_WRONLY,
            0o600,
        )
        created = True
    except FileExistsError:
        descriptor = os.open(paths["results"], os.O_APPEND | os.O_WRONLY)
    try:
        os.write(descriptor, line.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if created:
        _fsync_parent_directory(paths["results"])


def _append_adverse_stop_event(paths: Dict[str, str], kind: str, symbol: str,
                               now: datetime, error: str = "") -> None:
    row = {"kind": kind}
    if error:
        row["error"] = error
    row.update({
        "symbol": symbol,
        "utc": _utc_text(now),
    })
    _append_results_row(paths, row)
    print(json.dumps(row, separators=(",", ":")))


def _append_result(paths: Dict[str, str], job_id: str, op: str, symbol: str,
                   outcome: Outcome, now: datetime) -> None:
    row = {
        "job_id": job_id,
        "op": op,
        "symbol": symbol,
        "verdict": outcome.verdict,
        "exit_code": outcome.exit_code,
        "receipt_path": outcome.receipt_path,
        "utc": _utc_text(now),
    }
    if op in MOMENTUM_OPS:
        ledger = outcome.ledger or _empty_momentum_ledger()
        row.update({
            "fills": list(ledger["fills"]),
            "fees": list(ledger["fees"]),
            "realized": dict(ledger["realized"]),
        })
    _append_results_row(paths, row)


def _write_proof_marker(path: str, job_id: str, utc: str) -> bool:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, (job_id + " " + utc + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _basis_proof_passed(paths: Dict[str, str]) -> bool:
    if os.path.isfile(paths["basis_proof"]):
        return True
    try:
        with open(paths["results"], encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if not isinstance(row, dict):
                    return False
                if row.get("op") == "BASIS_ROUNDTRIP" \
                        and row.get("verdict") == "CLOSED" \
                        and type(row.get("job_id")) is str \
                        and type(row.get("utc")) is str:
                    created = _write_proof_marker(
                        paths["basis_proof"], row["job_id"], row["utc"]
                    )
                    if created:
                        print("JOB-RUNNER | BASIS_PROOF_BACKFILLED | %s" % (
                            row["job_id"]
                        ))
                    return created or os.path.isfile(paths["basis_proof"])
    except (OSError, ValueError, UnicodeError):
        return False
    return False


@contextmanager
def _environment(values: Dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _load_momentum_position(paths: Dict[str, str]) \
        -> Optional[Dict[str, Any]]:
    path = paths["momentum_position"]
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(
                handle, parse_constant=_reject_constant,
                object_pairs_hook=_strict_object,
            )
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError,
            JobRefusal):
        raise MomentumStateError(
            "momentum-position.json exists but is unreadable"
        ) from None
    if not isinstance(value, dict):
        raise MomentumStateError("momentum position state is not an object")
    if type(value.get("version")) is not int \
            or value["version"] != MOMENTUM_STATE_VERSION:
        raise MomentumStateError("momentum position version is invalid")
    if value.get("state") not in {
            "NONE", "OPENING", "OPEN", "CLOSING", "CLOSED", "UNKNOWN"}:
        raise MomentumStateError("momentum position state is invalid")
    if value.get("venue", "futures") not in {"futures", "spot"}:
        raise MomentumStateError("momentum position venue is invalid")
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        raise MomentumStateError("momentum position revision is invalid")
    symbol = value.get("symbol")
    if type(symbol) is not str or not SYMBOL_RE.fullmatch(symbol):
        raise MomentumStateError("momentum position symbol is invalid")
    job_id = value.get("job_id")
    if type(job_id) is not str or not JOB_ID_RE.fullmatch(job_id):
        raise MomentumStateError("momentum position job id is invalid")
    pending = value.get("pending_intent")
    if value["state"] in ("OPENING", "CLOSING") \
            and not isinstance(pending, dict):
        raise MomentumStateError("momentum pending intent is missing")
    if isinstance(pending, dict):
        if pending.get("action") not in ("OPEN", "EXIT") \
                or type(pending.get("client_id")) is not str \
                or not isinstance(pending.get("intent"), dict):
            raise MomentumStateError("momentum pending intent is invalid")
    return value


def _momentum_venue(position: Dict[str, Any]) -> str:
    venue = position.get("venue", "futures")
    if venue not in {"futures", "spot"}:
        raise MomentumStateError("momentum position venue is invalid")
    return venue


def _save_momentum_position(paths: Dict[str, str],
                            position: Dict[str, Any],
                            now: datetime) -> None:
    """Atomically replace one complete momentum position revision."""
    current = _load_momentum_position(paths)
    revision = current["revision"] if current is not None else 0
    payload = dict(position)
    payload["version"] = MOMENTUM_STATE_VERSION
    payload["revision"] = revision + 1
    payload["updated_utc"] = _utc_text(now)
    path = paths["momentum_position"]
    temporary = path + ".tmp-" + secrets.token_hex(6)
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        try:
            descriptor = os.open(os.path.dirname(path), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    position.clear()
    position.update(payload)


def _momentum_decimal(value: Any, label: str,
                      allow_zero: bool = False,
                      allow_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise MomentumStateError("%s is missing" % label)
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError):
        raise MomentumStateError("%s is not decimal exchange truth" % label) \
            from None
    if not parsed.is_finite() or (
            not allow_negative
            and (parsed < 0 if allow_zero else parsed <= 0)):
        raise MomentumStateError("%s is outside its factual range" % label)
    return parsed


def _futures_services(services: Optional[ExecutionServices]) -> ExecutionServices:
    if services is None:
        try: services = _build_execution_services()
        except Exception:
            raise MomentumStateError("Futures execution services are unavailable") from None
    if not isinstance(services, ExecutionServices):
        raise MomentumStateError("Futures execution services are invalid")
    return services


def _typed_value(result: Any, value_type: Any, label: str) -> Any:
    if not isinstance(result, binance_http.ReadResult) or result.kind != "ok" or not isinstance(result.value, value_type):
        raise MomentumStateError(label + " is unreadable")
    return result.value


def _adapter_intent_payload(intent: Any) -> Dict[str, Any]:
    if not isinstance(intent, binance_http.MutationIntent) or type(intent.params) is not tuple or any(type(pair) is not tuple or len(pair) != 2 or any(type(value) is not str for value in pair) for pair in intent.params):
        raise binance_http.IntentNotDurable("adapter intent is invalid")
    return {"venue": intent.venue, "operation": intent.operation, "identity": intent.identity, "git_sha": intent.git_sha, "params": [list(pair) for pair in intent.params]}


def _leverage_writer(paths: Dict[str, str], job_id: str, symbol: str, now: datetime) -> Callable[[binance_http.MutationIntent], None]:
    path = os.path.join(paths["receipts"], "job-%s-leverage-intent.json" % job_id)
    def write(intent: binance_http.MutationIntent) -> None:
        _persist_json_receipt(path, {"kind": "futures_leverage_intent", "job_id": job_id, "symbol": symbol, "intent": _adapter_intent_payload(intent), "persisted_utc": _utc_text(now)}, create=True)
    return write


def _prove_futures_leverage(paths: Dict[str, str], job_id: str, symbol: str, sha: str, services: Optional[ExecutionServices], now: datetime) -> ExecutionServices:
    services = _futures_services(services)
    mutation_id = "lev-" + hashlib.sha256((job_id + ":" + symbol).encode("ascii")).hexdigest()[:24]
    result = services.futures_set_leverage_1x_once(services.transport, services.futures_base_url, symbol, mutation_id, sha, _leverage_writer(paths, job_id, symbol, now), sleep=services.sleep)
    if not isinstance(result, binance_futures.LeverageResult) or result.kind != "proven" or result.after != 1:
        raise MomentumStateError("momentum leverage is not proven 1x")
    return services


def _futures_market(symbol: str, services: ExecutionServices) -> binance_futures.FuturesMarket:
    return _typed_value(services.futures_read_market(services.transport, services.futures_base_url, symbol, sleep=services.sleep), binance_futures.FuturesMarket, "momentum market truth")


def _futures_measure(symbol: str, services: ExecutionServices) -> Dict[str, Any]:
    market = _futures_market(symbol, services); state = _typed_value(services.futures_read_preflight(services.transport, services.futures_base_url, market, None, require_flat=False, sleep=services.sleep), binance_futures.FuturesPreflight, "momentum exchange state")
    if state.leverage != 1 or not state.can_trade or not state.one_way or state.position_quantity != state.account_position_quantity or state.position_quantity < 0:
        raise MomentumStateError("momentum exchange state is unproved")
    quantity = state.position_quantity
    return {"qty": quantity, "price": market.mark_price if quantity else None, "open_orders": len(state.open_order_ids), "leverage": state.leverage, "market": market}


def _futures_open_context(symbol: str, effective: Decimal, services: ExecutionServices) -> Tuple[binance_futures.FuturesMarket, Decimal, Decimal]:
    market = _futures_market(symbol, services); state = _typed_value(services.futures_read_preflight(services.transport, services.futures_base_url, market, effective, require_flat=True, sleep=services.sleep), binance_futures.FuturesPreflight, "momentum entry preflight")
    if state.leverage != 1 or market.quote_asset != "USDT" or market.margin_asset != "USDT": raise MomentumStateError("momentum entry capability is unproved")
    slippage = _momentum_decimal(os.environ.get("MAX_SLIPPAGE_BPS", "10"), "MAX_SLIPPAGE_BPS", allow_zero=True)
    if slippage > 100: raise JobRefusal("MOMENTUM_PREFLIGHT: MAX_SLIPPAGE_BPS exceeds 100")
    try:
        with binance_http.exact_guard():
            raw_price = market.ask * (Decimal(1) + slippage / Decimal(10000)); price = (raw_price // market.tick_size) * market.tick_size
            if price != raw_price: price += market.tick_size
            unit_notional = market.step_size * price
            quantity = min((effective // unit_notional) * market.step_size, market.max_quantity)
            close_floor = quantity * market.mark_price * Decimal("0.99")
    except ArithmeticError:
        raise JobRefusal("MOMENTUM_PREFLIGHT: sizing is not exact") from None
    if quantity < market.min_quantity or quantity * price < market.min_notional or close_floor < market.min_notional:
        raise JobRefusal("MOMENTUM_PREFLIGHT: effective cap is below Futures filters")
    return market, quantity, price


def _pending_order_writer(paths: Dict[str, str], expected: binance_http.MutationIntent) -> Callable[[binance_http.MutationIntent], None]:
    projection, wire = _adapter_intent_payload(expected), dict(expected.params)
    def write(intent: binance_http.MutationIntent) -> None:
        if _adapter_intent_payload(intent) != projection:
            raise binance_http.IntentNotDurable("adapter intent changed")
        position = _load_momentum_position(paths)
        pending = position.get("pending_intent") if isinstance(position, dict) else None
        if not isinstance(pending, dict) or pending.get("intent") != wire or pending.get("adapter_intent") != projection:
            raise binance_http.IntentNotDurable("pending order intent is not durable")
        _fsync_parent_directory(paths["momentum_position"])
    return write


def _futures_order_facts(order: Any, client_id: str, symbol: str, reduce_only: bool) -> Dict[str, Any]:
    expected_side = "SELL" if reduce_only else "BUY"
    if not isinstance(order, binance_http.OrderResult) or order.tag != "terminal" or order.terminal is not True or order.identity.symbol != symbol or order.identity.client_id != client_id or type(order.identity.order_id) is not int or order.returned_client_id not in (None, client_id) or order.side != expected_side or order.reduce_only is not reduce_only or type(order.status) is not str:
        raise MomentumStateError("momentum order truth is unproved")
    quantity, quote = order.executed_quantity, order.quote_quantity
    if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity < 0 or not isinstance(quote, Decimal) or not quote.is_finite() or quote < 0 or quantity > 0 and quote <= 0:
        raise MomentumStateError("momentum order money is unproved")
    return {"status": order.status, "order_id": str(order.identity.order_id), "client_id": client_id, "qty": quantity, "quote": quote, "price": None, "reduce_only": reduce_only}


def _futures_fill_rows(order: binance_http.OrderResult, position: Dict[str, Any], closed: bool, services: ExecutionServices) -> Tuple[List[Dict[str, str]], Decimal]:
    coverage = _typed_value(services.futures_read_fills(services.transport, services.futures_base_url, order, sleep=services.sleep), binance_http.FillCoverage, "momentum fills")
    if not coverage.complete or not coverage.fills or any(not isinstance(fill, binance_http.Fill) or not isinstance(fill.fee, binance_http.Fee) or fill.order_id != order.identity.order_id for fill in coverage.fills): raise MomentumStateError("momentum fills are unproved")
    realized: Dict[int, Decimal] = {}
    if closed:
        try: start = _timestamp(position["pending_intent"]["persisted_utc"])
        except (KeyError, JobRefusal, TypeError):
            raise MomentumStateError("momentum income window is unproved") from None
        start_ms, end_ms = int(start.timestamp() * 1000), int((start + MAX_JOB_AGE).timestamp() * 1000)
        income = _typed_value(services.futures_read_income(services.transport, services.futures_base_url, order.identity.symbol, start_ms, end_ms, sleep=services.sleep), tuple, "momentum realized income")
        for row in income:
            if not isinstance(row, binance_futures.Income): raise MomentumStateError("momentum realized income conflicts")
            if row.income_type != "REALIZED_PNL": continue
            if row.asset != "USDT" or type(row.trade_id) is not int or row.trade_id in realized or not isinstance(row.amount, Decimal) or not row.amount.is_finite(): raise MomentumStateError("momentum realized income conflicts")
            realized[row.trade_id] = row.amount
    try:
        with binance_http.exact_guard():
            quote = sum((fill.quote_quantity for fill in coverage.fills), Decimal(0))
            quantity = sum((fill.quantity for fill in coverage.fills), Decimal(0))
            price = quote / quantity
    except ArithmeticError:
        raise MomentumStateError("momentum fill price is unproved") from None
    if quantity != order.executed_quantity or quote != order.quote_quantity: raise MomentumStateError("momentum fill totals do not match order")
    rows = []
    for fill in coverage.fills:
        if closed and fill.trade_id not in realized:
            raise MomentumStateError("momentum fill identity is unproved")
        rows.append({"trade_id": "" if fill.trade_id is None else str(fill.trade_id), "order_id": str(fill.order_id), "price": _decimal_text(fill.price), "qty": _decimal_text(fill.quantity), "realized_pnl": _decimal_text(realized[fill.trade_id] if closed else Decimal(0)), "commission": _decimal_text(fill.fee.amount), "commission_asset": fill.fee.asset})
    return rows, price




def _fees_from_fills(fills: List[Dict[str, str]]) \
        -> List[Dict[str, str]]:
    return [{
        "trade_id": fill["trade_id"],
        "order_id": fill["order_id"],
        "commission": fill["commission"],
        "commission_asset": fill["commission_asset"],
    } for fill in fills]


def _momentum_ledger(fills: List[Dict[str, str]],
                     entry_fees: Optional[List[Dict[str, str]]] = None,
                     closed: bool = False) -> Dict[str, Any]:
    fees = _fees_from_fills(fills)
    gross = sum(
        (Decimal(fill["realized_pnl"]) for fill in fills), Decimal(0)
    )
    all_fees = list(entry_fees or []) + fees
    totals: Dict[str, Decimal] = {}
    for fee in all_fees:
        asset = fee["commission_asset"]
        totals[asset] = totals.get(asset, Decimal(0)) + Decimal(
            fee["commission"]
        )
    realized: Dict[str, Any] = {
        "gross_usdt": _decimal_text(gross),
        "net_usdt": "UNKNOWN",
        "commission_by_asset": {
            asset: _decimal_text(value)
            for asset, value in sorted(totals.items())
        },
    }
    if closed and set(totals).issubset({"USDT"}):
        realized["net_usdt"] = _decimal_text(
            gross - totals.get("USDT", Decimal(0))
        )
    return {"fills": fills, "fees": fees, "realized": realized}


def _spot_services(execution_services: Optional[ExecutionServices]) \
        -> ExecutionServices:
    return execution_services or _build_execution_services()


def _spot_value(result: Any, label: str) -> Any:
    if not isinstance(result, binance_http.ReadResult) or result.kind != "ok" \
            or result.value is None:
        raise MomentumStateError(label + " is unreadable")
    return result.value


def _spot_free_balances(account: Any, *assets: str) -> Dict[str, Decimal]:
    if not isinstance(account, binance_spot.SpotAccountState):
        raise MomentumStateError("Spot account state is unreadable")
    rows = {row.asset: row.free for row in account.balances
            if isinstance(row, binance_http.Balance)}
    if len(rows) != len(account.balances) \
            or any(asset not in rows for asset in assets):
        raise MomentumStateError("Spot free balance is unproved")
    values = {asset: rows[asset] for asset in assets}
    if any(not isinstance(value, Decimal) or not value.is_finite() or value < 0
           for value in values.values()):
        raise MomentumStateError("Spot free balance is outside its range")
    return values


def _spot_market_context(services: ExecutionServices, symbol: str,
                         effective: Decimal) -> Dict[str, Any]:
    """Apply runner entry policy to typed Spot market and account facts."""
    try:
        market = _spot_value(services.spot_read_market(
            services.transport, services.spot_base_url, symbol,
            sleep=services.sleep,
        ), "Spot market")
        if not isinstance(market, binance_spot.SpotMarket) \
                or market.symbol != symbol or market.quote_asset != "USDT":
            raise MomentumStateError("Spot market identity is unproved")
        account = _spot_value(services.spot_read_account_state(
            services.transport, services.spot_base_url, symbol,
            sleep=services.sleep,
        ), "Spot account")
        if not isinstance(account, binance_spot.SpotAccountState) \
                or account.can_trade is not True:
            raise MomentumStateError("Spot account canTrade is not true")
        if account.open_order_ids:
            raise MomentumStateError("Spot open order stands on the symbol")
        balances = _spot_free_balances(account, market.base_asset,
                                       market.quote_asset)
        if not isinstance(effective, Decimal) or not effective.is_finite() \
                or effective <= 0 or effective > limits.PER_LEG_CAP_USDT:
            raise MomentumStateError("effective Spot quote cap is invalid")
        if balances[market.quote_asset] < effective:
            raise MomentumStateError("Spot quote balance is below the cap")
        with binance_http.exact_guard():
            estimated_net = basis_cycle.quantize_down(
                effective / market.ask *
                (Decimal(1) - basis_cycle.FEE_BUFFER), market.step_size,
            )
            sellable = estimated_net >= market.min_quantity \
                and estimated_net * market.bid >= market.min_notional
        if not sellable:
            raise MomentumStateError("cap cannot acquire sellable Spot")
    except Exception as error:
        raise JobRefusal("MOMENTUM_SPOT_PREFLIGHT: %s" % error) from None
    return {
        "adapter_market": market,
        "base": market.base_asset,
        "quote": market.quote_asset,
        "step": market.step_size,
        "min_qty": market.min_quantity,
        "min_notional": market.min_notional,
        "apply_min": True,
        "quote_spend": effective,
        "starting_base_free": balances[market.base_asset],
        "starting_quote_free": balances[market.quote_asset],
        "base_tolerance": Decimal(0),
        "bid": market.bid,
        "ask": market.ask,
    }


def _spot_persisted_market(position: Dict[str, Any]) -> Dict[str, Any]:
    raw = position.get("spot_market")
    if not isinstance(raw, dict):
        raise MomentumStateError("persisted Spot market context is missing")
    base = raw.get("base")
    quote = raw.get("quote")
    if type(base) is not str or not base \
            or type(quote) is not str or not quote:
        raise MomentumStateError("persisted Spot assets are invalid")
    return {
        "base": base,
        "quote": quote,
        "step": _momentum_decimal(raw.get("step"), "persisted Spot step"),
        "min_qty": _momentum_decimal(
            raw.get("min_qty"), "persisted Spot minimum quantity",
            allow_zero=True,
        ),
        "min_notional": _momentum_decimal(
            raw.get("min_notional"), "persisted Spot minimum notional",
            allow_zero=True,
        ),
        "apply_min": raw.get("apply_min") is True,
        "starting_base_free": _momentum_decimal(
            raw.get("starting_base_free"), "persisted starting base balance",
            allow_zero=True,
        ),
        "base_tolerance": _momentum_decimal(
            raw.get("base_tolerance"), "persisted base tolerance",
            allow_zero=True,
        ),
    }


def _spot_measure(position: Dict[str, Any],
                  services: ExecutionServices) -> Dict[str, Any]:
    market = _spot_persisted_market(position)
    try:
        fresh_market = _spot_value(services.spot_read_market(
            services.transport, services.spot_base_url, position["symbol"],
            sleep=services.sleep,
        ), "Spot market")
        account = _spot_value(services.spot_read_account_state(
            services.transport, services.spot_base_url, position["symbol"],
            sleep=services.sleep,
        ), "Spot account")
        if not isinstance(fresh_market, binance_spot.SpotMarket) \
                or fresh_market.symbol != position["symbol"] \
                or fresh_market.base_asset != market["base"] \
                or fresh_market.quote_asset != market["quote"]:
            raise MomentumStateError("Spot market identity changed")
        balances = _spot_free_balances(account, market["base"],
                                       market["quote"])
        with binance_http.exact_guard():
            quantity = balances[market["base"]] \
                - market["starting_base_free"]
        if quantity < -market["base_tolerance"]:
            raise MomentumStateError("Spot balance fell below baseline")
        if quantity.copy_abs() <= market["base_tolerance"]:
            quantity = Decimal(0)
    except Exception as error:
        if isinstance(error, MomentumStateError):
            raise
        raise MomentumStateError("momentum Spot state is unreadable: %s" %
                                 error) from None
    return {
        "qty": quantity,
        "price": fresh_market.bid,
        "open_orders": len(account.open_order_ids),
        "can_trade": account.can_trade is True,
        "base_free": balances[market["base"]],
        "adapter_market": fresh_market,
    }


def _spot_result_state(order: Any, submitted: bool) -> str:
    if not isinstance(order, binance_http.OrderResult): return "pending"
    if order.tag == "terminal" and order.terminal: return "terminal"
    if order.tag == "not_found": return "not_found"
    if submitted and order.tag == "error": return "rejected"
    return order.tag


def _spot_result_facts(order: Any, client_id: str,
                       side: str) -> Dict[str, Any]:
    if not isinstance(order, binance_http.OrderResult) or not order.terminal \
            or order.tag != "terminal" \
            or order.status not in binance_http.TERMINAL_ORDER_STATUSES:
        raise MomentumStateError("Spot momentum order is not terminal")
    if order.identity.client_id != client_id \
            or order.returned_client_id not in (None, client_id):
        raise MomentumStateError("Spot momentum order client id mismatches")
    if order.identity.symbol != os.environ.get("SYMBOL"):
        raise MomentumStateError("Spot momentum order symbol mismatches")
    if order.side != side or order.reduce_only is not False:
        raise MomentumStateError("Spot momentum order side mismatches")
    if isinstance(order.identity.order_id, bool) \
            or not isinstance(order.identity.order_id, int) \
            or order.identity.order_id <= 0:
        raise MomentumStateError("Spot momentum exchange order id is missing")
    quantity, quote = order.executed_quantity, order.quote_quantity
    if not isinstance(quantity, Decimal) or not quantity.is_finite() \
            or quantity < 0 or not isinstance(quote, Decimal) \
            or not quote.is_finite() or quote < 0:
        raise MomentumStateError("Spot order quantity is unproved")
    price = None
    if quantity > 0:
        if quote <= 0:
            raise MomentumStateError("filled Spot momentum order has no quote")
        try:
            with binance_http.exact_guard():
                price = quote / quantity
        except binance_http.AdapterRefusal:
            raise MomentumStateError("Spot average price is not exact") \
                from None
    return {"status": order.status,
            "order_id": str(order.identity.order_id),
            "client_id": client_id, "qty": quantity, "quote": quote,
            "price": price}


def _spot_fill_projection(
        services: ExecutionServices,
        order: binance_http.OrderResult) \
        -> Tuple[List[Dict[str, str]], Tuple[binance_http.Fee, ...]]:
    try:
        result = services.spot_read_fills(
            services.transport, services.spot_base_url, order,
            sleep=services.sleep,
        )
        coverage = _spot_value(result, "Spot fill coverage")
        if not isinstance(coverage, binance_http.FillCoverage) \
                or not coverage.complete \
                or coverage.covered_quantity != coverage.executed_quantity \
                or coverage.executed_quantity != order.executed_quantity:
            raise MomentumStateError("Spot fill coverage is incomplete")
        fees = services.spot_fees_by_asset(coverage.fills)
        if not isinstance(fees, tuple):
            raise MomentumStateError("Spot fees are unreadable")
        rows = [{
            "trade_id": "" if fill.trade_id is None else str(fill.trade_id),
            "order_id": str(fill.order_id),
            "price": _decimal_text(fill.price),
            "qty": _decimal_text(fill.quantity),
            "realized_pnl": "0",
            "commission": _decimal_text(fill.fee.amount),
            "commission_asset": fill.fee.asset,
        } for fill in coverage.fills]
    except Exception as error:
        raise MomentumStateError("Spot fill data is unreadable: %s" %
                                 type(error).__name__) from None
    return rows, fees


def _spot_momentum_ledger(
        fills: List[Dict[str, str]],
        entry_fees: Optional[List[Dict[str, str]]] = None,
        entry_quote: Optional[Decimal] = None,
        exit_quote: Optional[Decimal] = None) -> Dict[str, Any]:
    fees = _fees_from_fills(fills)
    all_fees = list(entry_fees or []) + fees
    totals: Dict[str, Decimal] = {}
    for fee in all_fees:
        asset = fee["commission_asset"]
        totals[asset] = totals.get(asset, Decimal(0)) + Decimal(
            fee["commission"]
        )
    realized: Dict[str, Any] = {
        "gross_usdt": "UNKNOWN",
        "net_usdt": "UNKNOWN",
        "commission_by_asset": {
            asset: _decimal_text(value)
            for asset, value in sorted(totals.items())
        },
    }
    if entry_quote is not None and exit_quote is not None:
        gross = exit_quote - entry_quote
        realized["gross_usdt"] = _decimal_text(gross)
        if set(totals).issubset({"USDT"}):
            realized["net_usdt"] = _decimal_text(
                gross - totals.get("USDT", Decimal(0))
            )
    return {"fills": fills, "fees": fees, "realized": realized}


def _new_receipt(receipt_dir: str, prefix: str, before: set,
                 preferred: str = "") -> str:
    if preferred and os.path.isfile(preferred):
        return preferred
    after = set(glob.glob(os.path.join(receipt_dir, prefix + "*.json")))
    created = sorted(after - before)
    return created[-1] if len(created) == 1 else ""


def _verdict_from_receipt(path: str) -> str:
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            receipt = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return ""
    verdict = receipt.get("verdict") if isinstance(receipt, dict) else None
    return verdict if type(verdict) is str and verdict else ""


def _run_basis(receipt_dir: str, effective: Decimal,
               entry: Optional[Callable[[Any], Any]] = None) -> Outcome:
    entry = entry or basis_executor.main
    before = set(glob.glob(os.path.join(receipt_dir, "basis-executor-*.json")))
    old_executor_cap = basis_executor.MAX_CYCLE_QUOTE
    old_cycle_cap = basis_cycle.MAX_CYCLE_QUOTE
    basis_executor.MAX_CYCLE_QUOTE = effective
    basis_cycle.MAX_CYCLE_QUOTE = effective
    try:
        returned = entry(["--execute"])
        code = returned if type(returned) is int else 0
    except SystemExit as stopped:
        code = stopped.code if type(stopped.code) is int else 1
        returned = None
    except Exception:
        return Outcome("UNKNOWN", 4, "", "EXECUTOR_EXCEPTION")
    finally:
        basis_executor.MAX_CYCLE_QUOTE = old_executor_cap
        basis_cycle.MAX_CYCLE_QUOTE = old_cycle_cap
    if isinstance(returned, dict):
        verdict = returned.get("verdict", "")
        receipt_path = returned.get("receipt_path", "")
        result_code = returned.get("exit_code", code)
        if type(verdict) is str and type(receipt_path) is str \
                and type(result_code) is int:
            return Outcome(verdict, result_code, receipt_path)
    receipt_path = _new_receipt(receipt_dir, "basis-executor-", before)
    verdict = _verdict_from_receipt(receipt_path)
    if not verdict:
        return Outcome("UNKNOWN", 4, receipt_path, "MISSING_EXECUTOR_RECEIPT")
    return Outcome(verdict, code, receipt_path)


def _momentum_unknown(paths: Dict[str, str], position: Dict[str, Any],
                      reason: str, now: datetime,
                      verdict: str = "UNKNOWN", code: int = 4) -> Outcome:
    position["state"] = "UNKNOWN"
    position["failure_reason"] = reason
    _save_momentum_position(paths, position, now)
    return Outcome(
        verdict, code, "", reason, _empty_momentum_ledger()
    )


def _momentum_flat(measurement: Dict[str, Any]) -> bool:
    return measurement["qty"] == 0 and measurement["open_orders"] == 0


def _resolve_momentum_open(paths: Dict[str, str],
                           position: Dict[str, Any],
                           order: binance_http.OrderResult,
                           services: ExecutionServices, now: datetime,
                           recovering: bool = False) -> Outcome:
    not_accepted = isinstance(order, binance_http.OrderResult) and (
        recovering and order.tag == "not_found"
        or not recovering and order.tag == "error"
    )
    if not_accepted:
        try:
            measured = _futures_measure(position["symbol"], services)
        except MomentumStateError as error:
            return _momentum_unknown(
                paths, position, "OPEN_NOT_FOUND_STATE_UNKNOWN: %s" % error,
                now,
            )
        if not _momentum_flat(measured):
            return _momentum_unknown(
                paths, position,
                "OPEN_NOT_FOUND_BUT_ACCOUNT_NOT_FLAT", now,
            )
        position.update({
            "state": "NONE",
            "pending_intent": None,
            "measured_qty": "0",
            "failure_reason": "",
        })
        _save_momentum_position(paths, position, now)
        return Outcome(
            "REFUSED", 2, "", "MOMENTUM_OPEN_NOT_ACCEPTED",
            _empty_momentum_ledger(),
        )
    if not isinstance(order, binance_http.OrderResult) \
            or order.tag != "terminal":
        state = order.tag if isinstance(
            order, binance_http.OrderResult
        ) else "pending"
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_OPEN_%s" % state.upper(), now,
        )
    client_id = position["pending_intent"]["client_id"]
    try:
        facts = _futures_order_facts(
            order, client_id, position["symbol"], reduce_only=False
        )
    except MomentumStateError as error:
        return _momentum_unknown(
            paths, position, "MOMENTUM_OPEN_ORDER_UNKNOWN: %s" % error, now
        )
    if facts["qty"] == 0:
        try:
            measured = _futures_measure(position["symbol"], services)
        except MomentumStateError as error:
            return _momentum_unknown(
                paths, position, "ZERO_OPEN_STATE_UNKNOWN: %s" % error, now
            )
        if not _momentum_flat(measured):
            return _momentum_unknown(
                paths, position, "ZERO_OPEN_BUT_ACCOUNT_NOT_FLAT", now
            )
        position.update({
            "state": "NONE",
            "pending_intent": None,
            "measured_qty": "0",
            "open_order": {
                key: _decimal_text(value) if isinstance(value, Decimal)
                else value for key, value in facts.items()
            },
        })
        _save_momentum_position(paths, position, now)
        return Outcome(
            "OPEN_NOT_FILLED", 2, "", "MOMENTUM_OPEN_ZERO_FILL",
            _empty_momentum_ledger(),
        )
    cap = _momentum_decimal(
        position.get("effective_notional"), "persisted effective notional"
    )
    planned = _momentum_decimal(
        position.get("planned_qty"), "persisted planned quantity"
    )
    if cap > limits.PER_LEG_CAP_USDT or facts["qty"] > planned \
            or facts["quote"] > cap:
        position["observed_order"] = {
            key: _decimal_text(value) if isinstance(value, Decimal) else value
            for key, value in facts.items()
        }
        return _momentum_unknown(
            paths, position, "MOMENTUM_OPEN_HARD_CAP_BREACH", now,
            verdict="OPEN_EXPOSURE", code=3,
        )
    try:
        fills, facts["price"] = _futures_fill_rows(
            order, position, False, services
        )
        measured = _futures_measure(position["symbol"], services)
    except MomentumStateError as error:
        return _momentum_unknown(
            paths, position, "MOMENTUM_OPEN_TRUTH_UNKNOWN: %s" % error, now
        )
    if measured["open_orders"] != 0 or measured["qty"] != facts["qty"]:
        return _momentum_unknown(
            paths, position, "MOMENTUM_OPEN_POSITION_MISMATCH", now,
            verdict="OPEN_EXPOSURE", code=3,
        )
    ledger = _momentum_ledger(fills)
    position.update({
        "state": "OPEN",
        "pending_intent": None,
        "measured_qty": _decimal_text(measured["qty"]),
        "entry": {
            "side": "LONG",
            "qty": _decimal_text(facts["qty"]),
            "price": _decimal_text(facts["price"]),
            "quote": _decimal_text(facts["quote"]),
            "order_id": facts["order_id"],
            "client_id": facts["client_id"],
            "status": facts["status"],
            "fills": list(fills),
            "fees": list(ledger["fees"]),
            "opened_utc": _utc_text(now),
        },
        "failure_reason": "",
    })
    _save_momentum_position(paths, position, now)
    return Outcome("OPEN", 0, "", "", ledger)


def _resolve_momentum_exit(paths: Dict[str, str],
                           position: Dict[str, Any],
                           order: binance_http.OrderResult,
                           services: ExecutionServices, now: datetime,
                           recovering: bool = False) -> Outcome:
    pending = position["pending_intent"]
    outcome_reason = pending.get("reason", "")
    if type(outcome_reason) is not str:
        outcome_reason = ""
    measured_before = _momentum_decimal(
        pending.get("measured_qty"), "persisted exit measurement"
    )
    not_accepted = isinstance(order, binance_http.OrderResult) and (
        recovering and order.tag == "not_found"
        or not recovering and order.tag == "error"
    )
    if not_accepted:
        try:
            measured = _futures_measure(position["symbol"], services)
        except MomentumStateError as error:
            return _momentum_unknown(
                paths, position, "EXIT_NOT_FOUND_STATE_UNKNOWN: %s" % error,
                now,
            )
        if measured["open_orders"] != 0 \
                or measured["qty"] != measured_before:
            return _momentum_unknown(
                paths, position,
                "EXIT_NOT_FOUND_POSITION_CHANGED", now,
            )
        position.update({
            "state": "OPEN",
            "pending_intent": None,
            "measured_qty": _decimal_text(measured["qty"]),
            "failure_reason": "",
        })
        _save_momentum_position(paths, position, now)
        return Outcome(
            "REFUSED", 2, "", "MOMENTUM_EXIT_NOT_ACCEPTED",
            _empty_momentum_ledger(),
        )
    if not isinstance(order, binance_http.OrderResult) \
            or order.tag != "terminal":
        state = order.tag if isinstance(
            order, binance_http.OrderResult
        ) else "pending"
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_EXIT_%s" % state.upper(), now,
        )
    client_id = pending["client_id"]
    try:
        facts = _futures_order_facts(
            order, client_id, position["symbol"], reduce_only=True
        )
    except MomentumStateError as error:
        return _momentum_unknown(
            paths, position, "MOMENTUM_EXIT_ORDER_UNKNOWN: %s" % error, now
        )
    if facts["qty"] == 0:
        try:
            measured = _futures_measure(position["symbol"], services)
        except MomentumStateError as error:
            return _momentum_unknown(
                paths, position, "ZERO_EXIT_STATE_UNKNOWN: %s" % error, now
            )
        if measured["open_orders"] != 0 \
                or measured["qty"] != measured_before:
            return _momentum_unknown(
                paths, position, "ZERO_EXIT_POSITION_CHANGED", now
            )
        position.update({
            "state": "OPEN",
            "pending_intent": None,
            "measured_qty": _decimal_text(measured["qty"]),
        })
        _save_momentum_position(paths, position, now)
        return Outcome(
            "EXIT_NOT_FILLED", 2, "", "MOMENTUM_EXIT_ZERO_FILL",
            _empty_momentum_ledger(),
        )
    if facts["qty"] != measured_before:
        return _momentum_unknown(
            paths, position, "MOMENTUM_EXIT_QUANTITY_MISMATCH", now,
            verdict="OPEN_EXPOSURE", code=3,
        )
    try:
        fills, facts["price"] = _futures_fill_rows(
            order, position, True, services
        )
        measured_after = _futures_measure(position["symbol"], services)
    except MomentumStateError as error:
        return _momentum_unknown(
            paths, position, "MOMENTUM_EXIT_TRUTH_UNKNOWN: %s" % error, now
        )
    if not _momentum_flat(measured_after):
        position["measured_qty"] = _decimal_text(measured_after["qty"])
        return _momentum_unknown(
            paths, position, "MOMENTUM_EXIT_NOT_FLAT", now,
            verdict="OPEN_EXPOSURE", code=3,
        )
    entry = position.get("entry")
    entry_fees = entry.get("fees") if isinstance(entry, dict) else None
    if not isinstance(entry_fees, list):
        return _momentum_unknown(
            paths, position, "MOMENTUM_ENTRY_FEES_MISSING", now
        )
    ledger = _momentum_ledger(fills, entry_fees, closed=True)
    position.update({
        "state": "CLOSED",
        "pending_intent": None,
        "measured_qty": "0",
        "exit": {
            "qty": _decimal_text(facts["qty"]),
            "price": _decimal_text(facts["price"]),
            "quote": _decimal_text(facts["quote"]),
            "order_id": facts["order_id"],
            "client_id": facts["client_id"],
            "status": facts["status"],
            "reduce_only": True,
            "fills": list(fills),
            "fees": list(ledger["fees"]),
            "closed_utc": _utc_text(now),
        },
        "realized": dict(ledger["realized"]),
        "failure_reason": "",
    })
    _save_momentum_position(paths, position, now)
    return Outcome("CLOSED", 0, "", outcome_reason, ledger)


def _spot_market_payload(market: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "base": market["base"],
        "quote": market["quote"],
        "step": _decimal_text(market["step"]),
        "min_qty": _decimal_text(market["min_qty"]),
        "min_notional": _decimal_text(market["min_notional"]),
        "apply_min": market["apply_min"],
        "starting_base_free": _decimal_text(
            market["starting_base_free"]
        ),
        "starting_quote_free": _decimal_text(
            market["starting_quote_free"]
        ),
        "base_tolerance": _decimal_text(market["base_tolerance"]),
    }


def _spot_intent_payload(intent: binance_http.MutationIntent) \
        -> Dict[str, Any]:
    return {"venue": intent.venue, "operation": intent.operation,
            "identity": intent.identity, "git_sha": intent.git_sha,
            "params": [list(pair) for pair in intent.params]}


def _spot_intent_writer(
        paths: Dict[str, str], position: Dict[str, Any],
        expected: binance_http.MutationIntent, now: datetime) \
        -> binance_http.IntentWriter:
    payload = _spot_intent_payload(expected)

    def write_intent(intent: binance_http.MutationIntent) -> None:
        pending = position.get("pending_intent")
        if intent != expected or not isinstance(pending, dict) \
                or pending.get("client_id") != expected.identity \
                or pending.get("intent") != payload:
            raise binance_http.IntentNotDurable("Spot intent differs")
        _save_momentum_position(paths, position, now)

    return write_intent


def _resolve_spot_momentum_open(
        paths: Dict[str, str], position: Dict[str, Any], order: Any,
        now: datetime, services: ExecutionServices,
        recovering: bool = False) -> Outcome:
    query_state = _spot_result_state(order, not recovering)
    if query_state in {"rejected", "not_found"}:
        try:
            measured = _spot_measure(position, services)
        except MomentumStateError as error:
            return _momentum_unknown(
                paths, position,
                "SPOT_OPEN_NOT_FOUND_STATE_UNKNOWN: %s" % error, now,
            )
        if measured["qty"] != 0 or measured["open_orders"] != 0:
            return _momentum_unknown(
                paths, position,
                "SPOT_OPEN_NOT_FOUND_BUT_ACCOUNT_CHANGED", now,
            )
        position.update({
            "state": "NONE",
            "pending_intent": None,
            "measured_qty": "0",
            "failure_reason": "",
        })
        _save_momentum_position(paths, position, now)
        return Outcome(
            "REFUSED", 2, "", "MOMENTUM_SPOT_OPEN_NOT_ACCEPTED",
            _empty_momentum_ledger(),
        )
    if query_state != "terminal":
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_OPEN_%s" % query_state.upper(), now,
        )
    pending = position["pending_intent"]
    client_id = pending["client_id"]
    try:
        facts = _spot_result_facts(order, client_id, "BUY")
    except MomentumStateError as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_OPEN_ORDER_UNKNOWN: %s" % error, now,
        )
    if facts["qty"] == 0:
        try:
            measured = _spot_measure(position, services)
        except MomentumStateError as error:
            return _momentum_unknown(
                paths, position,
                "ZERO_SPOT_OPEN_STATE_UNKNOWN: %s" % error, now,
            )
        if measured["qty"] != 0 or measured["open_orders"] != 0:
            return _momentum_unknown(
                paths, position, "ZERO_SPOT_OPEN_BUT_ACCOUNT_CHANGED", now
            )
        position.update({
            "state": "NONE",
            "pending_intent": None,
            "measured_qty": "0",
        })
        _save_momentum_position(paths, position, now)
        return Outcome(
            "OPEN_NOT_FILLED", 2, "", "MOMENTUM_SPOT_OPEN_ZERO_FILL",
            _empty_momentum_ledger(),
        )
    cap = _momentum_decimal(
        position.get("effective_notional"), "persisted effective notional"
    )
    if cap > limits.PER_LEG_CAP_USDT or facts["quote"] > cap:
        position["observed_order"] = {
            key: _decimal_text(value) if isinstance(value, Decimal) else value
            for key, value in facts.items()
        }
        return _momentum_unknown(
            paths, position, "MOMENTUM_SPOT_OPEN_HARD_CAP_BREACH", now,
            verdict="OPEN_EXPOSURE", code=3,
        )
    try:
        fills, fees = _spot_fill_projection(services, order)
        market = _spot_persisted_market(position)
        net_base = services.spot_net_base_quantity(
            facts["qty"], market["base"], fees
        )
        if net_base <= 0:
            raise MomentumStateError(
                "Spot BUY acquired no positive base after fees"
            )
        sell_quantity = basis_cycle.quantize_down(net_base, market["step"])
        if sell_quantity < market["min_qty"] \
                or (market["apply_min"] and
                    sell_quantity * facts["price"] < market["min_notional"]):
            raise MomentumStateError(
                "Spot BUY did not acquire a sellable net quantity"
            )
        ledger = _spot_momentum_ledger(fills)
        entry = {
            "side": "LONG",
            "venue": "spot",
            "qty": _decimal_text(facts["qty"]),
            "net_base_acquired": _decimal_text(net_base),
            "sell_quantity": _decimal_text(sell_quantity),
            "price": _decimal_text(facts["price"]),
            "quote": _decimal_text(facts["quote"]),
            "order_id": facts["order_id"],
            "client_id": facts["client_id"],
            "status": facts["status"],
            "fills": list(fills),
            "fees": list(ledger["fees"]),
            "opened_utc": _utc_text(now),
        }
        position["entry"] = entry
        measured = _spot_measure(position, services)
    except (binance_http.AdapterRefusal, MomentumStateError,
            ArithmeticError, AttributeError, TypeError, ValueError) as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_OPEN_TRUTH_UNKNOWN: %s" % error, now,
            verdict="OPEN_EXPOSURE", code=3,
        )
    if measured["open_orders"] != 0 \
            or (measured["qty"] - net_base).copy_abs() \
            > market["base_tolerance"]:
        return _momentum_unknown(
            paths, position, "MOMENTUM_SPOT_OPEN_POSITION_MISMATCH", now,
            verdict="OPEN_EXPOSURE", code=3,
        )
    position.update({
        "state": "OPEN",
        "pending_intent": None,
        "measured_qty": _decimal_text(net_base),
        "entry": entry,
        "failure_reason": "",
    })
    _save_momentum_position(paths, position, now)
    return Outcome("OPEN", 0, "", "", ledger)


def _run_spot_momentum_open(
        job: Job, receipt_dir: str, paths: Dict[str, str], now: datetime,
        governor_token: str, prior: Optional[Dict[str, Any]],
        execution_services: Optional[ExecutionServices] = None) -> Outcome:
    run_id = "mom" + secrets.token_hex(8)
    try:
        services = _spot_services(execution_services)
        market = _spot_market_context(
            services, job.symbol, job.effective_notional
        )
        client_id = basis_cycle.client_id(basis_cycle.ENTER_SPOT, run_id)
        plan = services.spot_build_market_order(
            market["adapter_market"], "BUY", client_id,
            os.environ["QUANT_SHA"], quote_limit=market["quote_spend"],
        )
        with binance_http.exact_guard():
            planned_qty = basis_cycle.quantize_down(
                market["quote_spend"] / market["ask"], market["step"]
            )
    except JobRefusal as error:
        return Outcome(
            "REFUSED", 2, "", error.reason, _empty_momentum_ledger()
        )
    except (binance_http.AdapterRefusal, ArithmeticError,
            AttributeError, TypeError, ValueError) as error:
        return Outcome(
            "REFUSED", 2, "", "MOMENTUM_SPOT_PREFLIGHT: %s" % error,
            _empty_momentum_ledger(),
        )
    position: Dict[str, Any] = {
        "version": MOMENTUM_STATE_VERSION,
        "revision": prior["revision"] if prior is not None else 0,
        "state": "OPENING",
        "venue": "spot",
        "symbol": job.symbol,
        "job_id": job.job_id,
        "sha": os.environ["QUANT_SHA"],
        "run_id": run_id,
        "effective_notional": _decimal_text(job.effective_notional),
        "planned_qty": _decimal_text(planned_qty),
        "spot_market": _spot_market_payload(market),
        "pending_intent": {
            "action": "OPEN",
            "job_op": job.op,
            "client_id": client_id,
            "intent": _spot_intent_payload(plan.intent),
            "persisted_utc": _utc_text(now),
            "governor_token": governor_token,
        },
        "entry": None,
        "exit": None,
        "created_utc": _utc_text(now),
    }
    _save_momentum_position(paths, position, now)
    try:
        order = services.spot_submit_order_once(
            services.transport, services.spot_base_url, plan,
            _spot_intent_writer(paths, position, plan.intent, now),
            sleep=services.sleep,
        )
    except Exception as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_OPEN_EXECUTOR_EXCEPTION: %s" %
            type(error).__name__, now,
        )
    return _resolve_spot_momentum_open(
        paths, position, order, now, services,
    )


def _resolve_spot_momentum_exit(
        paths: Dict[str, str], position: Dict[str, Any], order: Any,
        now: datetime, services: ExecutionServices,
        recovering: bool = False) -> Outcome:
    pending = position["pending_intent"]
    query_state = _spot_result_state(order, not recovering)
    outcome_reason = pending.get("reason", "")
    if type(outcome_reason) is not str:
        outcome_reason = ""
    measured_before = _momentum_decimal(
        pending.get("measured_qty"), "persisted Spot exit measurement"
    )
    submitted = _momentum_decimal(
        pending.get("sell_quantity"), "persisted Spot sell quantity"
    )
    market = _spot_persisted_market(position)
    if query_state in {"rejected", "not_found"}:
        try:
            measured = _spot_measure(position, services)
        except MomentumStateError as error:
            return _momentum_unknown(
                paths, position,
                "SPOT_EXIT_NOT_FOUND_STATE_UNKNOWN: %s" % error, now,
            )
        if measured["open_orders"] != 0 \
                or (measured["qty"] - measured_before).copy_abs() \
                > market["base_tolerance"]:
            return _momentum_unknown(
                paths, position, "SPOT_EXIT_NOT_FOUND_POSITION_CHANGED", now
            )
        position.update({
            "state": "OPEN",
            "pending_intent": None,
            "measured_qty": _decimal_text(measured["qty"]),
            "failure_reason": "",
        })
        _save_momentum_position(paths, position, now)
        return Outcome(
            "REFUSED", 2, "", "MOMENTUM_SPOT_EXIT_NOT_ACCEPTED",
            _empty_momentum_ledger(),
        )
    if query_state != "terminal":
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_EXIT_%s" % query_state.upper(), now,
        )
    try:
        facts = _spot_result_facts(order, pending["client_id"], "SELL")
    except MomentumStateError as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_EXIT_ORDER_UNKNOWN: %s" % error, now,
        )
    if facts["qty"] == 0:
        try:
            measured = _spot_measure(position, services)
        except MomentumStateError as error:
            return _momentum_unknown(
                paths, position,
                "ZERO_SPOT_EXIT_STATE_UNKNOWN: %s" % error, now,
            )
        if measured["open_orders"] != 0 \
                or (measured["qty"] - measured_before).copy_abs() \
                > market["base_tolerance"]:
            return _momentum_unknown(
                paths, position, "ZERO_SPOT_EXIT_POSITION_CHANGED", now
            )
        position.update({
            "state": "OPEN",
            "pending_intent": None,
            "measured_qty": _decimal_text(measured["qty"]),
        })
        _save_momentum_position(paths, position, now)
        return Outcome(
            "EXIT_NOT_FILLED", 2, "", "MOMENTUM_SPOT_EXIT_ZERO_FILL",
            _empty_momentum_ledger(),
        )
    if facts["qty"] > submitted:
        return _momentum_unknown(
            paths, position, "MOMENTUM_SPOT_EXIT_EXCEEDED_ACQUISITION", now,
            verdict="OPEN_EXPOSURE", code=3,
        )
    try:
        fills, fees = _spot_fill_projection(services, order)
        net_executed = services.spot_net_base_quantity(
            facts["qty"], market["base"], fees
        )
        with binance_http.exact_guard():
            sell_base_fee = facts["qty"] - net_executed
            residual = measured_before - facts["qty"] - sell_base_fee
        if residual < -market["base_tolerance"]:
            raise MomentumStateError(
                "Spot SELL consumed pre-existing base holdings"
            )
        measured_after = _spot_measure(position, services)
    except (binance_http.AdapterRefusal, MomentumStateError,
            ArithmeticError, AttributeError, TypeError, ValueError) as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_EXIT_TRUTH_UNKNOWN: %s" % error, now,
            verdict="OPEN_EXPOSURE", code=3,
        )
    if measured_after["open_orders"] != 0 \
            or (measured_after["qty"] - residual).copy_abs() \
            > market["base_tolerance"] \
            or facts["qty"] != submitted \
            or residual >= market["step"]:
        position["measured_qty"] = _decimal_text(measured_after["qty"])
        return _momentum_unknown(
            paths, position, "MOMENTUM_SPOT_EXIT_NOT_FLAT", now,
            verdict="OPEN_EXPOSURE", code=3,
        )
    entry = position.get("entry")
    entry_fees = entry.get("fees") if isinstance(entry, dict) else None
    if not isinstance(entry_fees, list):
        return _momentum_unknown(
            paths, position, "MOMENTUM_SPOT_ENTRY_FEES_MISSING", now
        )
    try:
        entry_quote = _momentum_decimal(
            entry.get("quote"), "persisted Spot entry quote"
        )
    except MomentumStateError as error:
        return _momentum_unknown(paths, position, str(error), now)
    ledger = _spot_momentum_ledger(
        fills, entry_fees, entry_quote, facts["quote"]
    )
    position.update({
        "state": "CLOSED",
        "pending_intent": None,
        "measured_qty": "0",
        "exit": {
            "venue": "spot",
            "qty": _decimal_text(facts["qty"]),
            "submitted_qty": _decimal_text(submitted),
            "residual_base": _decimal_text(max(residual, Decimal(0))),
            "price": _decimal_text(facts["price"]),
            "quote": _decimal_text(facts["quote"]),
            "order_id": facts["order_id"],
            "client_id": facts["client_id"],
            "status": facts["status"],
            "fills": list(fills),
            "fees": list(ledger["fees"]),
            "closed_utc": _utc_text(now),
        },
        "realized": dict(ledger["realized"]),
        "failure_reason": "",
    })
    _save_momentum_position(paths, position, now)
    return Outcome("CLOSED", 0, "", outcome_reason, ledger)


def _run_spot_momentum_exit(
        job: Job, receipt_dir: str, paths: Dict[str, str], now: datetime,
        position: Dict[str, Any], persisted_qty: Decimal,
        measurement: Optional[Dict[str, Any]] = None,
        outcome_reason: str = "",
        execution_services: Optional[ExecutionServices] = None) -> Outcome:
    try:
        services = _spot_services(execution_services)
    except (binance_http.AdapterRefusal, AttributeError, TypeError,
            ValueError) as error:
        return _momentum_unknown(paths, position, str(error), now)
    market = _spot_persisted_market(position)
    try:
        measured = measurement or _spot_measure(
            position, services
        )
    except MomentumStateError as error:
        return _momentum_unknown(paths, position, str(error), now)
    if measured.get("can_trade") is not True:
        return _momentum_unknown(
            paths, position, "SPOT_TRADING_UNPROVEN", now
        )
    if measured["open_orders"] != 0 \
            or (measured["qty"] - persisted_qty).copy_abs() \
            > market["base_tolerance"]:
        return _momentum_unknown(
            paths, position, "MOMENTUM_SPOT_POSITION_MEASUREMENT_MISMATCH",
            now,
        )
    entry = position.get("entry")
    if not isinstance(entry, dict):
        return _momentum_unknown(
            paths, position, "MOMENTUM_SPOT_ENTRY_MISSING", now
        )
    try:
        sell_quantity = _momentum_decimal(
            entry.get("sell_quantity"), "persisted Spot sell quantity"
        )
    except MomentumStateError as error:
        return _momentum_unknown(paths, position, str(error), now)
    expected_sell = basis_cycle.quantize_down(
        persisted_qty, market["step"]
    )
    if sell_quantity != expected_sell or sell_quantity > persisted_qty \
            or sell_quantity < market["min_qty"] \
            or (market["apply_min"] and
                sell_quantity * measured["price"] < market["min_notional"]):
        return _momentum_unknown(
            paths, position, "MOMENTUM_SPOT_SELL_QUANTITY_UNPROVEN", now
        )
    run_id = "mom" + secrets.token_hex(8)
    client_id = basis_cycle.client_id(basis_cycle.EXIT_SPOT, run_id)
    try:
        adapter_market = measured.get("adapter_market")
        if not isinstance(adapter_market, binance_spot.SpotMarket):
            measured = _spot_measure(position, services)
            adapter_market = measured["adapter_market"]
        plan = services.spot_build_market_order(
            adapter_market, "SELL", client_id, os.environ["QUANT_SHA"],
            quantity=sell_quantity,
        )
    except (binance_http.AdapterRefusal, MomentumStateError,
            ArithmeticError, AttributeError, TypeError, ValueError) as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_SELL_QUANTITY_UNPROVEN: %s" % error, now,
        )
    position["state"] = "CLOSING"
    position["job_id"] = job.job_id
    position["run_id"] = run_id
    position["pending_intent"] = {
        "action": "EXIT",
        "client_id": client_id,
        "measured_qty": _decimal_text(measured["qty"]),
        "sell_quantity": _decimal_text(sell_quantity),
        "intent": _spot_intent_payload(plan.intent),
        "persisted_utc": _utc_text(now),
    }
    if outcome_reason:
        position["pending_intent"]["reason"] = outcome_reason
    _save_momentum_position(paths, position, now)
    try:
        order = services.spot_submit_order_once(
            services.transport, services.spot_base_url, plan,
            _spot_intent_writer(paths, position, plan.intent, now),
            sleep=services.sleep,
        )
    except Exception as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_EXIT_EXECUTOR_EXCEPTION: %s" %
            type(error).__name__, now,
        )
    return _resolve_spot_momentum_exit(
        paths, position, order, now, services,
    )


def _run_momentum_open(job: Job, receipt_dir: str,
                       paths: Dict[str, str], now: datetime,
                       governor_token: str = "",
                       execution_services: Optional[
                           ExecutionServices
                       ] = None) -> Outcome:
    try:
        prior = _load_momentum_position(paths)
    except MomentumStateError as error:
        return Outcome("UNKNOWN", 4, "", str(error),
                       _empty_momentum_ledger())
    if prior is not None and prior["state"] in ("OPENING", "CLOSING",
                                                "UNKNOWN"):
        return Outcome(
            "UNKNOWN", 4, "", "MOMENTUM_POSITION_NOT_SETTLED",
            _empty_momentum_ledger(),
        )
    if prior is not None and prior["state"] == "OPEN":
        return Outcome(
            "REFUSED", 2, "", "MOMENTUM_POSITION_ALREADY_STANDS",
            _empty_momentum_ledger(),
        )
    if job.venue == "spot":
        return _run_spot_momentum_open(
            job, receipt_dir, paths, now, governor_token, prior,
            execution_services,
        )
    try:
        sha = os.environ["QUANT_SHA"]
        services = _prove_futures_leverage(
            paths, job.job_id, job.symbol, sha, execution_services, now
        )
        market, quantity, price = _futures_open_context(
            job.symbol, job.effective_notional, services
        )
        client_id = "l2fm-" + secrets.token_hex(10) + "-o"
        plan = services.futures_build_open_order(
            market, "BUY", client_id, sha, quantity,
            profile="LIMIT_IOC", limit_price=price,
        )
        if not isinstance(plan, binance_http.OrderPlan) \
                or not isinstance(plan.quantity, Decimal):
            raise MomentumStateError("momentum open plan is unproved")
    except JobRefusal as error:
        return Outcome("REFUSED", 2, "", error.reason,
                       _empty_momentum_ledger())
    except binance_http.AdapterRefusal as error:
        return Outcome(
            "REFUSED", 2, "", "MOMENTUM_PREFLIGHT: %s" % error,
            _empty_momentum_ledger(),
        )
    except MomentumStateError as error:
        return Outcome("UNKNOWN", 4, "", str(error),
                       _empty_momentum_ledger())
    intent = plan.intent
    position: Dict[str, Any] = {
        "version": MOMENTUM_STATE_VERSION,
        "revision": prior["revision"] if prior is not None else 0,
        "state": "OPENING",
        "venue": "futures",
        "symbol": job.symbol,
        "job_id": job.job_id,
        "sha": sha,
        "effective_notional": _decimal_text(job.effective_notional),
        "planned_qty": _decimal_text(plan.quantity),
        "pending_intent": {
            "action": "OPEN",
            "job_op": job.op,
            "client_id": client_id,
            "intent": dict(intent.params),
            "adapter_intent": _adapter_intent_payload(intent),
            "persisted_utc": _utc_text(now),
            "governor_token": governor_token,
        },
        "entry": None,
        "exit": None,
        "created_utc": _utc_text(now),
    }
    _save_momentum_position(paths, position, now)
    try:
        order = services.futures_submit_order_once(
            services.transport, services.futures_base_url, plan,
            _pending_order_writer(paths, intent), sleep=services.sleep,
        )
    except Exception as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_OPEN_EXECUTOR_EXCEPTION: %s" %
            type(error).__name__, now,
        )
    return _resolve_momentum_open(
        paths, position, order, services, now
    )


def _run_momentum_exit(job: Job, receipt_dir: str,
                       paths: Dict[str, str], now: datetime,
                       measurement: Optional[Dict[str, Any]] = None,
                       outcome_reason: str = "", governor_token: str = "",
                       execution_services: Optional[
                           ExecutionServices
                       ] = None) -> Outcome:
    try:
        position = _load_momentum_position(paths)
    except MomentumStateError as error:
        return Outcome("UNKNOWN", 4, "", str(error),
                       _empty_momentum_ledger())
    if position is None or position["state"] in ("NONE", "CLOSED"):
        return Outcome(
            "REFUSED", 2, "", "NO_MOMENTUM_POSITION",
            _empty_momentum_ledger(),
        )
    if position["state"] != "OPEN":
        return Outcome(
            "UNKNOWN", 4, "", "MOMENTUM_POSITION_NOT_SETTLED",
            _empty_momentum_ledger(),
        )
    if position["symbol"] != job.symbol:
        return Outcome(
            "REFUSED", 2, "", "MOMENTUM_POSITION_SYMBOL_MISMATCH",
            _empty_momentum_ledger(),
        )
    try:
        persisted_qty = _momentum_decimal(
            position.get("measured_qty"), "persisted momentum quantity"
        )
    except MomentumStateError as error:
        return _momentum_unknown(paths, position, str(error), now)
    try:
        venue = _momentum_venue(position)
    except MomentumStateError as error:
        return _momentum_unknown(paths, position, str(error), now)
    if venue == "spot":
        return _run_spot_momentum_exit(
            job, receipt_dir, paths, now, position, persisted_qty,
            measurement, outcome_reason, execution_services,
        )
    try:
        sha = os.environ["QUANT_SHA"]
        services = _prove_futures_leverage(
            paths, job.job_id, job.symbol, sha, execution_services, now
        )
        measured = measurement or _futures_measure(job.symbol, services)
    except MomentumStateError as error:
        return _momentum_unknown(paths, position, str(error), now)
    if measured["open_orders"] != 0 or measured["qty"] != persisted_qty:
        return _momentum_unknown(
            paths, position, "MOMENTUM_POSITION_MEASUREMENT_MISMATCH", now
        )
    client_id = "l2fm-" + secrets.token_hex(10) + "-c"
    try:
        market = measured.get("market")
        if not isinstance(market, binance_futures.FuturesMarket):
            market = _futures_market(job.symbol, services)
        plan = services.futures_build_reduce_only_close(
            market, "SELL", client_id, sha, measured["qty"]
        )
        if not isinstance(plan, binance_http.OrderPlan):
            raise MomentumStateError("momentum close plan is unproved")
    except (binance_http.AdapterRefusal, MomentumStateError) as error:
        return _momentum_unknown(paths, position, str(error), now)
    intent = plan.intent
    position["state"] = "CLOSING"
    position["job_id"] = job.job_id
    position["pending_intent"] = {
        "action": "EXIT",
        "job_op": job.op,
        "client_id": client_id,
        "governor_token": governor_token,
        "measured_qty": _decimal_text(measured["qty"]),
        "intent": dict(intent.params),
        "adapter_intent": _adapter_intent_payload(intent),
        "persisted_utc": _utc_text(now),
    }
    if outcome_reason:
        position["pending_intent"]["reason"] = outcome_reason
    _save_momentum_position(paths, position, now)
    try:
        order = services.futures_submit_order_once(
            services.transport, services.futures_base_url, plan,
            _pending_order_writer(paths, intent), sleep=services.sleep,
        )
    except Exception as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_EXIT_EXECUTOR_EXCEPTION: %s" %
            type(error).__name__, now,
        )
    return _resolve_momentum_exit(
        paths, position, order, services, now
    )


def _momentum_roundtrip_allowed(paths: Dict[str, str]) -> Optional[Outcome]:
    try:
        position = _load_momentum_position(paths)
    except MomentumStateError as error:
        return Outcome("UNKNOWN", 4, "", str(error),
                       _empty_momentum_ledger())
    if position is not None and position["state"] in ("OPENING", "CLOSING",
                                                     "UNKNOWN"):
        return Outcome(
            "UNKNOWN", 4, "", "MOMENTUM_POSITION_NOT_SETTLED",
            _empty_momentum_ledger(),
        )
    if position is not None and position["state"] == "OPEN":
        return Outcome(
            "REFUSED", 2, "", "MOMENTUM_POSITION_ALREADY_STANDS",
            _empty_momentum_ledger(),
        )
    return None


def _run_retired_futures_fixture(receipt_dir: str) -> Outcome:
    before = set(glob.glob(os.path.join(receipt_dir, "futures-hand-*.json")))
    futures_hand.SYMBOL = os.environ["SYMBOL"]; futures_hand.MAX_NOTIONAL_RAW = os.environ["MAX_NOTIONAL"]; futures_hand.receipt_path = ""
    previous_argv = sys.argv; returned, code = None, 0
    sys.argv = ["futures_hand.py", "--short", "--execute"]
    try:
        returned = futures_hand.main(); code = returned if type(returned) is int else 0
    except SystemExit as stopped: code = stopped.code if type(stopped.code) is int else 1
    except Exception: return Outcome("UNKNOWN", 4, "", "EXECUTOR_EXCEPTION")
    finally: sys.argv = previous_argv
    if isinstance(returned, dict) and type(returned.get("verdict")) is str and type(returned.get("receipt_path")) is str and type(returned.get("exit_code", code)) is int:
        return Outcome(returned["verdict"], returned.get("exit_code", code), returned["receipt_path"])
    receipt_path = _new_receipt(receipt_dir, "futures-hand-", before, futures_hand.receipt_path)
    verdict = _verdict_from_receipt(receipt_path)
    return Outcome(verdict, code, receipt_path) if verdict else Outcome("UNKNOWN", 4, receipt_path, "MISSING_EXECUTOR_RECEIPT")


def _run_futures_roundtrip(
        job: Job, receipt_dir: str, paths: Dict[str, str], now: datetime,
        governor_token: str,
        execution_services: Optional[ExecutionServices]) -> Outcome:
    if callable(futures_hand.main) and not callable(futures_hand.http):
        return _run_retired_futures_fixture(receipt_dir)
    opened = _run_momentum_open(
        job, receipt_dir, paths, now, governor_token,
        execution_services=execution_services,
    )
    if opened.verdict != "OPEN" or opened.exit_code != 0:
        return opened
    return _run_momentum_exit(
        job, receipt_dir, paths, now, governor_token=governor_token,
        execution_services=execution_services,
    )


def _recover_pending_momentum(paths: Dict[str, str], receipt_dir: str,
                              result_ids: set, now: datetime,
                              execution_services: Optional[
                                  ExecutionServices
                              ] = None) \
        -> Optional[Tuple[str, str, str, Outcome, str]]:
    try:
        position = _load_momentum_position(paths)
    except MomentumStateError:
        return None
    if position is None or position["state"] not in ("OPENING", "CLOSING"):
        return None
    job_id = position["job_id"]
    if job_id in result_ids:
        return None
    claim = os.path.join(paths["claims"], "job-%s.claim" % job_id)
    if not os.path.isfile(claim):
        return None
    pending = position["pending_intent"]
    action = pending["action"]
    governor_token = pending.get("governor_token", "")
    if type(governor_token) is not str:
        governor_token = ""
    op = pending.get("job_op")
    if op not in MOMENTUM_OPS | {"FUTURES_ROUNDTRIP"}:
        op = "MOMENTUM_OPEN" if action == "OPEN" else "MOMENTUM_EXIT"
    if not os.environ.get("BINANCE_API_KEY") \
            or not os.environ.get("BINANCE_API_SECRET"):
        outcome = _momentum_unknown(
            paths, position, "MISSING_CREDENTIALS_DURING_RECOVERY", now
        )
        return job_id, op, position["symbol"], outcome, governor_token
    sha = str(position.get("sha", "")).lower()
    effective = str(position.get("effective_notional", ""))
    if not SHA_RE.fullmatch(sha) or not effective:
        outcome = _momentum_unknown(
            paths, position, "INVALID_PERSISTED_RECOVERY_CONFIG", now
        )
        return job_id, op, position["symbol"], outcome, governor_token
    values = {
        "SYMBOL": position["symbol"],
        "MAX_NOTIONAL": effective,
        "QUANT_SHA": sha,
        "RECEIPT_DIR": receipt_dir,
    }
    with _environment(values):
        venue = _momentum_venue(position)
        if venue == "spot":
            try:
                services = _spot_services(execution_services)
                order = services.spot_query_order(
                    services.transport, services.spot_base_url,
                    position["symbol"], pending["client_id"],
                    sleep=services.sleep,
                )
            except Exception as error:
                outcome = _momentum_unknown(
                    paths, position,
                    "MOMENTUM_SPOT_RECOVERY_EXCEPTION: %s" %
                    type(error).__name__, now,
                )
            else:
                if action == "OPEN":
                    outcome = _resolve_spot_momentum_open(
                        paths, position, order, now, services,
                        recovering=True,
                    )
                else:
                    outcome = _resolve_spot_momentum_exit(
                        paths, position, order, now, services,
                        recovering=True,
                    )
        else:
            try:
                services = _futures_services(execution_services)
                order = services.futures_query_order(
                    services.transport, services.futures_base_url,
                    position["symbol"], pending["client_id"],
                    sleep=services.sleep,
                )
            except Exception as error:
                outcome = _momentum_unknown(
                    paths, position,
                    "MOMENTUM_RECOVERY_EXCEPTION: %s" %
                    type(error).__name__, now,
                )
            else:
                if action == "OPEN":
                    outcome = _resolve_momentum_open(
                        paths, position, order, services, now,
                        recovering=True,
                    )
                else:
                    outcome = _resolve_momentum_exit(
                        paths, position, order, services, now,
                        recovering=True,
                    )
        if op in {"FUTURES_ROUNDTRIP", "MOMENTUM_ROUNDTRIP"} \
                and action == "OPEN" and outcome.verdict == "OPEN":
            outcome = _momentum_unknown(
                paths, position, "ROUNDTRIP_RECOVERY_REQUIRES_OWNER", now,
                verdict="OPEN_EXPOSURE", code=3,
            )
    return job_id, op, position["symbol"], outcome, governor_token


def _persist_margin_source(path: str, payload: Dict[str, Any],
                           create: bool = False) -> None:
    _persist_json_receipt(path, payload, create=create)


def _margin_transaction_number(value: Any) -> Optional[int]:
    if type(value) is not str or len(value) > 128 \
            or MARGIN_TRANSACTION_ID_RE.fullmatch(value) is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _margin_history_rows(
        result: Any, direction: str) \
        -> Optional[Tuple[binance_margin.MarginHistoryRow, ...]]:
    if not isinstance(result, binance_http.ReadResult) \
            or result.kind != "ok" or type(result.value) is not tuple:
        return None
    rows = result.value
    if any(
            not isinstance(row, binance_margin.MarginHistoryRow)
            or row.direction != direction
            for row in rows):
        return None
    return rows


def _read_margin_history_snapshot(
        services: ExecutionServices, asset: str,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None) \
        -> Tuple[
            str, Dict[str, Tuple[binance_margin.MarginHistoryRow, ...]],
            List[int],
        ]:
    histories = {}
    identifiers = []
    try:
        for direction in ("BORROW", "REPAY"):
            result = services.margin_read_history_by_direction(
                services.transport, services.spot_base_url, asset, direction,
                start_ms=start_ms, end_ms=end_ms, sleep=services.sleep,
            )
            rows = _margin_history_rows(result, direction)
            if rows is None:
                return "MARGIN_CONFIRMATION_UNREADABLE", {}, []
            histories[direction] = rows
            identifiers.extend(row.tx_id for row in rows)
        return "", histories, identifiers
    except Exception:
        return "MARGIN_CONFIRMATION_UNREADABLE", {}, []


def _margin_history_epoch_milliseconds(
        value: datetime, round_up: bool = False) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = value.astimezone(timezone.utc) - epoch
    microseconds = (
        (delta.days * 86400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
    return (microseconds + 999) // 1000 if round_up \
        else microseconds // 1000


def _read_margin_legacy_exclusivity(
        services: ExecutionServices, asset: str, transaction_id: str,
        mutation_started_utc: Any, now: datetime) \
        -> Tuple[
            str, Dict[str, Tuple[binance_margin.MarginHistoryRow, ...]],
            List[int], Optional[Dict[str, Any]],
        ]:
    """Read the frozen legacy window without overstating exclusivity."""
    try:
        mutation_started = _timestamp(mutation_started_utc)
        target = _margin_transaction_number(transaction_id)
        if target is None or mutation_started > now:
            return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
        window_start = mutation_started - MARGIN_LEGACY_HISTORY_WINDOW_PAD
        reason, histories, identifiers = _read_margin_history_snapshot(
            services, asset,
            _margin_history_epoch_milliseconds(window_start),
            _margin_history_epoch_milliseconds(now, round_up=True),
        )
        if reason:
            return reason, {}, [], None
        if identifiers != [target]:
            return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
        # The frozen typed surface does not return pagination completeness.
        # One page cannot prove that the retained legacy window has one row.
        return "LEGACY_WINDOW_NOT_EXCLUSIVE", histories, identifiers, None
    except Exception:
        return "MARGIN_CONFIRMATION_UNREADABLE", {}, [], None


def _margin_history_fence_status(
        identifiers: Any, transaction_id: Any, watermark: Any) -> str:
    try:
        if type(identifiers) is not list \
                or any(type(value) is not int or value <= 0
                       for value in identifiers) \
                or type(watermark) is not int or watermark < 0:
            return ""
        target = _margin_transaction_number(transaction_id)
        if target is None:
            return ""
        newer = {value for value in identifiers if value > watermark}
        if any(value != target for value in newer):
            return "CONCURRENT_MARGIN_ACTIVITY"
        if target <= watermark or identifiers.count(target) != 1:
            return ""
        return "CONFIRMED"
    except Exception:
        return ""


def _margin_history_confirmation_attempt(
        job_id: str, request_type: str, transaction_id: str,
        attempt: int, delay_seconds: int, status: str) -> None:
    row = {
        "kind": "margin_history_confirmation_attempt",
        "job_id": job_id,
        "direction": request_type,
        "transaction_id": transaction_id,
        "attempt": attempt,
        "max_attempts": MARGIN_HISTORY_CONFIRMATION_RETRY_LIMIT + 1,
        "delay_seconds": delay_seconds,
        "status": status,
        "utc": _utc_text(_utc_now()),
    }
    print(json.dumps(row, separators=(",", ":")))


def _read_margin_post_history(
        services: ExecutionServices, asset: str, request_type: str,
        transaction_id: str, history_watermark: int, job_id: str) \
        -> Tuple[
            str, Dict[str, Tuple[binance_margin.MarginHistoryRow, ...]],
            List[int],
        ]:
    """Retry typed snapshots only while the submitted identity is absent."""
    target = _margin_transaction_number(transaction_id)
    delays = (0,) + MARGIN_HISTORY_CONFIRMATION_BACKOFF_SECONDS[
        :MARGIN_HISTORY_CONFIRMATION_RETRY_LIMIT
    ]
    latest = ("", {}, [])
    for attempt, delay_seconds in enumerate(delays, start=1):
        if delay_seconds:
            try:
                services.sleep(delay_seconds)
            except Exception:
                return "MARGIN_CONFIRMATION_UNREADABLE", {}, []
        latest = _read_margin_history_snapshot(services, asset)
        history_reason, _history_after, history_identifiers = latest
        fence_status = ""
        if not history_reason:
            fence_status = _margin_history_fence_status(
                history_identifiers, transaction_id, history_watermark
            )
        if history_reason:
            status = history_reason
        elif target is None or type(history_watermark) is not int \
                or target <= history_watermark:
            status = "MARGIN_CONFIRMATION_MISMATCH"
        elif fence_status == "CONCURRENT_MARGIN_ACTIVITY":
            status = fence_status
        elif target in history_identifiers:
            status = "IDENTITY_VISIBLE"
        else:
            status = "IDENTITY_NOT_VISIBLE"
        _margin_history_confirmation_attempt(
            job_id, request_type, transaction_id, attempt,
            delay_seconds, status,
        )
        if status != "IDENTITY_NOT_VISIBLE":
            return latest
    return latest


def _margin_decimal_text(value: Any) -> Optional[str]:
    if type(value) is not str or not value \
            or len(value) > MARGIN_DECIMAL_TEXT_MAX \
            or MARGIN_DECIMAL_RE.fullmatch(value) is None:
        return None
    try:
        parsed = Decimal(value)
    except (ArithmeticError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return _canonical_decimal_text(parsed)


def _margin_liability_receipt(
        liability: Any) -> Optional[Dict[str, str]]:
    if not isinstance(liability, binance_margin.Liability):
        return None
    try:
        values = (
            liability.principal, liability.interest, liability.total
        )
        if any(
                not isinstance(value, Decimal) or not value.is_finite()
                or value < Decimal("0")
                for value in values):
            return None
        if liability.principal + liability.interest != liability.total:
            return None
        return {
            "asset": liability.asset,
            "borrowed": _canonical_decimal_text(liability.principal),
            "interest": _canonical_decimal_text(liability.interest),
            "total_liability": _canonical_decimal_text(liability.total),
        }
    except Exception:
        return None


def _margin_liability_from_receipt(
        value: Any, asset: Optional[str] = None) \
        -> Optional[binance_margin.Liability]:
    try:
        if type(value) is not dict or set(value) != {
                "asset", "borrowed", "interest", "total_liability"
        } or type(value.get("asset")) is not str \
                or asset is not None and value["asset"] != asset:
            return None
        canonical = [
            _margin_decimal_text(value.get(field))
            for field in ("borrowed", "interest", "total_liability")
        ]
        if any(item is None for item in canonical):
            return None
        liability = binance_margin.Liability(
            value["asset"], Decimal(canonical[0]),
            Decimal(canonical[1]), Decimal(canonical[2]),
        )
        return liability \
            if _margin_liability_receipt(liability) == value else None
    except Exception:
        return None


def _margin_operation_ledger_confirmation(
        operation: Any) -> Optional[Dict[str, str]]:
    if not isinstance(operation, binance_margin.MarginOperation):
        return None
    row = {
        "asset": operation.asset,
        "principal": _canonical_decimal_text(operation.principal),
        "interest": _canonical_decimal_text(operation.interest),
        "status": operation.direction + "_CONFIRMED",
    }
    if operation.direction == "REPAY":
        row["amount"] = _canonical_decimal_text(operation.amount)
    return row


def _margin_history_ledger_confirmation(
        row: Any) -> Optional[Dict[str, str]]:
    if not isinstance(row, binance_margin.MarginHistoryRow):
        return None
    confirmation = {
        "asset": row.asset,
        "principal": _canonical_decimal_text(row.principal),
        "interest": _canonical_decimal_text(row.interest),
        "status": row.direction + "_CONFIRMED",
    }
    if row.direction == "REPAY":
        confirmation["amount"] = _canonical_decimal_text(row.amount)
    return confirmation


def _margin_target_history(
        histories: Any, direction: str, tx_id: int) \
        -> Optional[binance_margin.MarginHistoryRow]:
    try:
        if type(histories) is not dict \
                or set(histories) != {"BORROW", "REPAY"}:
            return None
        rows = histories.get(direction)
        if type(rows) is not tuple:
            return None
        matches = [
            row for row in rows
            if isinstance(row, binance_margin.MarginHistoryRow)
            and row.tx_id == tx_id
        ]
        return matches[0] if len(matches) == 1 else None
    except Exception:
        return None


def _read_margin_liability(
        services: ExecutionServices, asset: str) \
        -> Tuple[str, Optional[binance_margin.Liability]]:
    try:
        result = services.margin_read_liability(
            services.transport, services.spot_base_url, asset,
            sleep=services.sleep,
        )
        if not isinstance(result, binance_http.ReadResult) \
                or result.kind != "ok" \
                or not isinstance(result.value, binance_margin.Liability):
            return "MARGIN_CONFIRMATION_UNREADABLE", None
        return "", result.value
    except Exception:
        return "MARGIN_CONFIRMATION_UNREADABLE", None


def _read_margin_history_identity(
        services: ExecutionServices, asset: str, direction: str,
        transaction_id: str) \
        -> Tuple[str, Optional[binance_margin.MarginHistoryRow]]:
    target = _margin_transaction_number(transaction_id)
    if target is None:
        return "MARGIN_TRANSACTION_MISSING", None
    try:
        result = services.margin_read_history_by_tx_id(
            services.transport, services.spot_base_url, asset,
            direction, target, sleep=services.sleep,
        )
        if not isinstance(result, binance_http.ReadResult) \
                or result.kind != "ok":
            return "MARGIN_CONFIRMATION_UNREADABLE", None
        if not isinstance(result.value, binance_margin.MarginHistoryRow) \
                or result.value.direction != direction \
                or result.value.tx_id != target:
            return "MARGIN_CONFIRMATION_MISMATCH", None
        return "", result.value
    except Exception:
        return "MARGIN_CONFIRMATION_UNREADABLE", None


def _build_typed_margin_operation(
        services: ExecutionServices, job: Job, ready: Dict[str, Any],
        liability_before: binance_margin.Liability, sha: str) \
        -> Tuple[Optional[binance_margin.MarginOperation], str, bool]:
    """Build one adapter operation and bind it to fresh runner authority."""
    try:
        params = ready["params"]
        if params["type"] == "BORROW":
            operation = services.margin_build_borrow(
                params["asset"], job.requested_max_notional,
                job.intent_id, sha,
            )
        else:
            if job.requested_max_notional > liability_before.total:
                return None, "PRINCIPAL_EXCEEDS_LIABILITY", True
            if job.requested_max_notional != liability_before.total:
                return None, "REPAY_PRINCIPAL_INTEREST_MISMATCH", True
            operation = services.margin_build_repay(
                liability_before, job.intent_id, sha
            )
        expected_params = tuple(
            (name, params[name]) for name in ("asset", "amount", "type")
        )
        if not isinstance(operation, binance_margin.MarginOperation) \
                or operation.direction != params["type"] \
                or operation.asset != params["asset"] \
                or operation.amount != Decimal(params["amount"]) \
                or operation.intent.venue != "BINANCE_MARGIN" \
                or operation.intent.operation != params["type"] \
                or operation.intent.identity != job.intent_id \
                or operation.intent.git_sha != sha \
                or operation.intent.params != expected_params:
            return None, "MARGIN_VALIDATION_UNKNOWN", False
        return operation, "", False
    except Exception:
        return None, "MARGIN_VALIDATION_UNKNOWN", False


def _margin_operation_from_receipt(
        services: ExecutionServices, ready: Dict[str, Any],
        liability_before: Any, sha: Any) \
        -> Optional[binance_margin.MarginOperation]:
    """Rebuild a modern typed plan from read-only durable facts."""
    try:
        params = ready["params"]
        if type(sha) is not str or SHA_RE.fullmatch(sha) is None:
            return None
        if params["type"] == "BORROW":
            operation = services.margin_build_borrow(
                params["asset"], Decimal(params["amount"]),
                ready["intent_id"], sha,
            )
        else:
            before = _margin_liability_from_receipt(
                liability_before, params["asset"]
            )
            if before is None or before.total != Decimal(params["amount"]):
                return None
            operation = services.margin_build_repay(
                before, ready["intent_id"], sha
            )
        expected_params = tuple(
            (name, params[name]) for name in ("asset", "amount", "type")
        )
        if not isinstance(operation, binance_margin.MarginOperation) \
                or operation.intent.params != expected_params \
                or operation.intent.identity != ready["intent_id"] \
                or _margin_operation_ledger_confirmation(operation) \
                != ready["expected_ledger_confirmation"]:
            return None
        return operation
    except Exception:
        return None


def _margin_liability_decimals(value: Any) \
        -> Optional[Tuple[str, Decimal, Decimal, Decimal]]:
    liability = _margin_liability_from_receipt(value)
    if liability is None:
        return None
    return (
        liability.asset, liability.principal,
        liability.interest, liability.total,
    )


def _margin_repay_receipt_expected(
        params: Any, liability_before: Any) -> Optional[Dict[str, str]]:
    """Validate one persisted REPAY split without reading exchange schema."""
    try:
        if type(params) is not dict \
                or params.get("type") != "REPAY" \
                or type(params.get("asset")) is not str:
            return None
        amount = _margin_decimal_text(params.get("amount"))
        liability = _margin_liability_from_receipt(
            liability_before, params["asset"]
        )
        if amount is None or liability is None \
                or liability.total != Decimal(amount):
            return None
        return {
            "asset": params["asset"],
            "amount": amount,
            "principal": _canonical_decimal_text(liability.principal),
            "interest": _canonical_decimal_text(liability.interest),
            "status": MARGIN_HISTORY_CONFIRMED_STATUS["REPAY"],
        }
    except Exception:
        return None


def _margin_repay_dust_bound(principal: Any) -> Optional[Decimal]:
    canonical = _margin_decimal_text(principal)
    if canonical is None:
        return None
    amount = Decimal(canonical)
    if amount <= Decimal("0"):
        return None
    with localcontext() as context:
        context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
        relative = amount * MARGIN_REPAY_RELATIVE_DUST_RATE
    return min(MARGIN_REPAY_DUST_BOUND, relative)


def _margin_repay_close_verdict(
        before: Any, after: Any, principal: Any) -> str:
    before_values = _margin_liability_decimals(before)
    after_values = _margin_liability_decimals(after)
    canonical = _margin_decimal_text(principal)
    dust_bound = _margin_repay_dust_bound(principal)
    if before_values is None or after_values is None or canonical is None \
            or dust_bound is None or before_values[0] != after_values[0]:
        return ""
    amount = Decimal(canonical)
    zero = Decimal("0")
    if amount <= zero or after_values[1] != zero:
        return ""
    with localcontext() as context:
        context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
        principal_settled = amount - min(amount, before_values[2])
        expected_borrowed = max(zero, before_values[1] - principal_settled)
        liability_decrease = before_values[3] - after_values[3]
        minimum_decrease = amount - dust_bound
    enough_decrease = liability_decrease > zero \
        and liability_decrease >= minimum_decrease \
        and after_values[1] == expected_borrowed
    if enough_decrease and after_values[2] == zero:
        return "CLOSED"
    if enough_decrease and after_values[2] < dust_bound:
        return "CLOSED-WITH-DUST"
    return ""


def _margin_liability_direction_confirmed(
        before: Any, after: Any, principal: Any, request_type: Any) -> bool:
    if request_type == "REPAY":
        return bool(_margin_repay_close_verdict(before, after, principal))
    before_values = _margin_liability_decimals(before)
    after_values = _margin_liability_decimals(after)
    canonical = _margin_decimal_text(principal)
    if request_type != "BORROW" or before_values is None \
            or after_values is None or canonical is None \
            or before_values[0] != after_values[0]:
        return False
    return after_values[1] - before_values[1] == Decimal(canonical)


def _evaluate_typed_margin_confirmation(
        services: ExecutionServices, histories: Any,
        history_identifiers: Any, transaction_id: Any,
        history_watermark: Any, operation: Any,
        liability_before: Any, liability_after: Any) -> MarginConfirmation:
    """Apply runner fences and outcomes to one typed adapter confirmation."""
    try:
        fence_status = _margin_history_fence_status(
            history_identifiers, transaction_id, history_watermark
        )
        if fence_status == "CONCURRENT_MARGIN_ACTIVITY":
            return MarginConfirmation(
                "UNKNOWN", "CONCURRENT_MARGIN_ACTIVITY"
            )
        if fence_status != "CONFIRMED":
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_CONFIRMATION_MISMATCH"
            )
        target = _margin_transaction_number(transaction_id)
        if target is None \
                or not isinstance(operation, binance_margin.MarginOperation) \
                or not isinstance(liability_before, binance_margin.Liability) \
                or not isinstance(liability_after, binance_margin.Liability):
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_CONFIRMATION_MISMATCH"
            )
        history = _margin_target_history(
            histories, operation.direction, target
        )
        if history is None:
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_CONFIRMATION_MISMATCH"
            )
        adapter_confirmation = services.margin_confirm(
            operation, target, history, liability_before, liability_after
        )
        if not isinstance(
                adapter_confirmation, binance_margin.MarginConfirmation) \
                or adapter_confirmation.operation != operation \
                or adapter_confirmation.tx_id != target \
                or adapter_confirmation.before != liability_before \
                or adapter_confirmation.after != liability_after:
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_CONFIRMATION_UNREADABLE"
            )
        before_receipt = _margin_liability_receipt(liability_before)
        after_receipt = _margin_liability_receipt(liability_after)
        ledger = _margin_operation_ledger_confirmation(operation)
        if before_receipt is None or after_receipt is None or ledger is None:
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_CONFIRMATION_UNREADABLE"
            )
        snapshot_time_note = MARGIN_SNAPSHOT_TIME_UNVERIFIED
        if operation.direction == "BORROW":
            if adapter_confirmation.kind != "confirmed":
                return MarginConfirmation(
                    "UNKNOWN", adapter_confirmation.reason,
                    account_liability=after_receipt,
                    snapshot_time_note=snapshot_time_note,
                )
            return MarginConfirmation(
                "OPEN", ledger["status"], ledger, after_receipt,
                snapshot_time_note,
            )

        if adapter_confirmation.kind == "confirmed":
            repay_verdict = "CLOSED"
        elif adapter_confirmation.kind == "unknown" \
                and adapter_confirmation.reason \
                == "MARGIN_LIABILITY_MISMATCH":
            repay_verdict = _margin_repay_close_verdict(
                before_receipt, after_receipt,
                _canonical_decimal_text(operation.amount),
            )
        else:
            return MarginConfirmation(
                "UNKNOWN", adapter_confirmation.reason,
                account_liability=after_receipt,
                snapshot_time_note=snapshot_time_note,
            )
        if not repay_verdict:
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_LIABILITY_MISMATCH",
                account_liability=after_receipt,
                snapshot_time_note=snapshot_time_note,
            )
        follow_up = None
        reason = ledger["status"]
        if repay_verdict == "CLOSED-WITH-DUST":
            follow_up = {
                "kind": "INTEREST_ONLY_REPAY",
                "asset": after_receipt["asset"],
                "principal": after_receipt["interest"],
            }
            reason = "REPAY_CONFIRMED_WITH_DUST"
        return MarginConfirmation(
            repay_verdict, reason, ledger, after_receipt,
            snapshot_time_note, follow_up,
        )
    except Exception:
        return MarginConfirmation(
            "UNKNOWN", "MARGIN_CONFIRMATION_UNREADABLE"
        )


def _evaluate_legacy_margin_confirmation(
        history: Any, history_identifiers: Any,
        transaction_id: Any, expected: Any, request_type: Any,
        liability_value: Any, exclusivity_proof: Any) \
        -> MarginConfirmation:
    """Evaluate the frozen BORROW receipt from typed read-only facts."""
    try:
        target = _margin_transaction_number(transaction_id)
        if target is None or history_identifiers != [target] \
                or not _valid_margin_legacy_exclusivity_proof(
                    exclusivity_proof
                ) or not isinstance(
                    history, binance_margin.MarginHistoryRow
                ) or history.tx_id != target \
                or history.direction != request_type:
            return MarginConfirmation(
                "UNKNOWN", "LEGACY_WINDOW_NOT_EXCLUSIVE"
            )
        confirmation = _margin_history_ledger_confirmation(history)
        if confirmation != expected or history.status != "CONFIRMED":
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_CONFIRMATION_MISMATCH"
            )
        liability = _margin_liability_receipt(liability_value)
        if request_type != "BORROW" \
                or not _legacy_current_liability_matches(
                    liability, expected["principal"]
                ):
            return MarginConfirmation(
                "UNKNOWN", "LEGACY_LIABILITY_MISMATCH",
                account_liability=liability,
            )
        return MarginConfirmation(
            "OPEN", expected["status"], confirmation, liability,
            MARGIN_SNAPSHOT_TIME_UNVERIFIED,
        )
    except Exception:
        return MarginConfirmation(
            "UNKNOWN", "MARGIN_CONFIRMATION_UNREADABLE"
        )


def _legacy_repay_current_liability(
        liability_value: Any) -> Tuple[Optional[Dict[str, str]], str]:
    liability = _margin_liability_receipt(liability_value)
    if liability is None:
        return None, "LEGACY_REPAY_CURRENT_ACCOUNT_MISSING"
    return liability, ""


def _legacy_repay_exchange_provenance(
        history: Any, receipt_facts: Tuple[str, str, str, str, str]) \
        -> str:
    """Name the first typed exact-id fact that misses the allowlist."""
    try:
        transaction_id = receipt_facts[1]
        requested_amount = _margin_decimal_text(receipt_facts[3])
        target = _margin_transaction_number(transaction_id)
        if requested_amount is None:
            return "ALLOWLIST_REQUESTED_AMOUNT"
        if not isinstance(history, binance_margin.MarginHistoryRow):
            return "EXCHANGE_HISTORY_ROW"
        if target is None or history.tx_id != target:
            return "EXCHANGE_TRANSACTION_ID"
        if history.asset != receipt_facts[2]:
            return "EXCHANGE_ASSET"
        if history.amount != Decimal(requested_amount):
            return "EXCHANGE_AMOUNT"
        if history.principal + history.interest != history.amount:
            return "EXCHANGE_PRINCIPAL_INTEREST_TOTAL"
        return ""
    except Exception:
        return "EXCHANGE_HISTORY_ROW"


def _legacy_repay_history_confirmation(
        history: Any, transaction_id: Any, expected: Any,
        liability_before: Any) \
        -> Tuple[Optional[Dict[str, str]], str]:
    try:
        target = _margin_transaction_number(transaction_id)
        if target is None \
                or not isinstance(history, binance_margin.MarginHistoryRow):
            return None, "LEGACY_REPAY_TX_ID_MISSING"
        if history.tx_id != target:
            return None, "LEGACY_REPAY_TX_ID_MISMATCH"
        if history.asset != expected.get("asset"):
            return None, "LEGACY_REPAY_ASSET_MISMATCH"
        if history.direction != "REPAY" or history.status != "CONFIRMED" \
                or expected.get("status") \
                != MARGIN_HISTORY_CONFIRMED_STATUS["REPAY"]:
            return None, "LEGACY_REPAY_STATUS_MISMATCH"
        expected_amount = _margin_decimal_text(expected.get("principal"))
        if expected_amount is None \
                or history.amount != Decimal(expected_amount):
            return None, "LEGACY_REPAY_AMOUNT_MISMATCH"
        before_values = _margin_liability_decimals(liability_before)
        if before_values is None:
            return None, "LEGACY_REPAY_ACCOUNT_LIABILITY_BEFORE_MISSING"
        if history.principal + history.interest != history.amount:
            return None, "LEGACY_REPAY_ROW_TOTAL_MISMATCH"
        if history.principal != before_values[1]:
            return None, "LEGACY_REPAY_PRINCIPAL_MISMATCH"
        if history.interest != before_values[2]:
            return None, "LEGACY_REPAY_INTEREST_MISMATCH"
        confirmation = _margin_history_ledger_confirmation(history)
        return confirmation, "" if confirmation is not None \
            else "MARGIN_CONFIRMATION_UNREADABLE"
    except Exception:
        return None, "MARGIN_CONFIRMATION_UNREADABLE"


def _evaluate_legacy_repay_confirmation(
        history: Any, history_identifiers: Any,
        transaction_id: Any, history_watermark: Any, expected: Any,
        liability_before: Any, liability_value: Any) -> MarginConfirmation:
    """Prove the frozen version-1 REPAY receipt from typed exchange truth."""
    try:
        fence_status = _margin_history_fence_status(
            history_identifiers, transaction_id, history_watermark
        )
        if fence_status == "CONCURRENT_MARGIN_ACTIVITY":
            return MarginConfirmation(
                "UNKNOWN", "CONCURRENT_MARGIN_ACTIVITY"
            )
        if fence_status != "CONFIRMED":
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_CONFIRMATION_MISMATCH"
            )
        confirmation, history_reason = _legacy_repay_history_confirmation(
            history, transaction_id, expected, liability_before
        )
        if confirmation is None:
            return MarginConfirmation("UNKNOWN", history_reason)
        liability, missing_reason = _legacy_repay_current_liability(
            liability_value
        )
        if liability is None:
            return MarginConfirmation("UNKNOWN", missing_reason)
        if Decimal(liability["borrowed"]) != Decimal("0"):
            return MarginConfirmation(
                "UNKNOWN", "LEGACY_REPAY_CURRENT_BORROWED_NOT_ZERO",
                account_liability=liability,
            )
        if Decimal(liability["interest"]) != Decimal("0"):
            return MarginConfirmation(
                "UNKNOWN", "LEGACY_REPAY_CURRENT_INTEREST_NOT_ZERO",
                account_liability=liability,
            )
        return MarginConfirmation(
            "CLOSED", confirmation["status"], confirmation, liability,
            MARGIN_SNAPSHOT_TIME_UNVERIFIED,
        )
    except Exception:
        return MarginConfirmation(
            "UNKNOWN", "MARGIN_CONFIRMATION_UNREADABLE"
        )


def _finish_margin_source(receipt: Dict[str, Any], path: str,
                          verdict: str, exit_code: int, reason: str,
                          now: datetime,
                          confirmation: Optional[Dict[str, str]] = None,
                          liability: Optional[Dict[str, str]] = None) \
        -> Outcome:
    receipt.update({
        "verdict": verdict,
        "reason": reason,
        "finished_utc": _utc_text(now),
    })
    if confirmation is not None:
        receipt["ledger_confirmation"] = confirmation
    if liability is not None:
        receipt["account_liability"] = liability
    try:
        _persist_margin_source(path, receipt)
    except Exception:
        return Outcome(
            "UNKNOWN", 4, path, "MARGIN_RECEIPT_PERSISTENCE_FAILED"
        )
    return Outcome(verdict, exit_code, path, reason)


def _finish_margin_before_post(receipt: Dict[str, Any], path: str,
                               verdict: str, exit_code: int, reason: str,
                               now: datetime) -> Outcome:
    receipt["mutation_attempts"] = 0
    receipt.pop("mutation_started_utc", None)
    return _finish_margin_source(
        receipt, path, verdict, exit_code, reason, now
    )


def _authority_digest(encoded: Optional[bytes]) -> str:
    material = encoded if encoded is not None else b"ABSENT"
    return hashlib.sha256(material).hexdigest()


def _json_authority_digest(value: Any) -> Optional[str]:
    try:
        encoded = json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _margin_post_authority(
        job: Job, paths: Dict[str, str], operation: Dict[str, Any],
        post_params: Dict[str, Any], validated_job_digest: str) \
        -> Tuple[str, datetime, Dict[str, str], bool]:
    ready_params_digest = _json_authority_digest(post_params)
    if ready_params_digest is None:
        ready_params_digest = _authority_digest(None)
    digests: Dict[str, str] = {
        "ready_params": ready_params_digest,
        "job_file": validated_job_digest
        if type(validated_job_digest) is str
        and re.fullmatch(r"[0-9a-f]{64}", validated_job_digest)
        else _authority_digest(None),
    }
    job_encoded = None
    accept_encoded = None
    operator_encoded = None
    stop_encoded = None
    read_reason = ""
    job_path = os.path.join(paths["jobs"], "job-%s.json" % job.job_id)
    try:
        job_encoded = _read_file_bytes(job_path)
    except OSError:
        read_reason = "MARGIN_VALIDATION_UNKNOWN"
    if job_encoded is not None and _authority_digest(job_encoded) \
            != validated_job_digest:
        read_reason = "MARGIN_VALIDATION_UNKNOWN"
    try:
        accept_encoded = _read_file_bytes(paths["accept_one"])
    except FileNotFoundError:
        pass
    except OSError:
        read_reason = read_reason or "ACCEPT_ONE_INVALID"
    try:
        operator_encoded = _read_file_bytes(paths["operator"])
    except FileNotFoundError:
        pass
    except OSError:
        read_reason = read_reason or "OPERATOR_MISMATCH"
    try:
        stop_encoded = _read_file_bytes(paths["stop"])
    except FileNotFoundError:
        pass
    except OSError:
        read_reason = read_reason or "MARGIN_VALIDATION_UNKNOWN"

    verified_now = _utc_now()
    digests.update({
        "accept_one": _authority_digest(accept_encoded),
        "operator": _authority_digest(operator_encoded),
        "stop_jobs": _authority_digest(stop_encoded),
    })
    stop_present = stop_encoded is not None
    if read_reason:
        return read_reason, verified_now, digests, stop_present

    try:
        fresh_raw = _decode_strict_object(job_encoded or b"")
        token = fresh_raw.get("operator_token")
        expiry = fresh_raw.get("expires_utc")
        if type(token) is str:
            digests["operator_token"] = _authority_digest(
                token.encode("utf-8")
            )
        if type(expiry) is str:
            digests["job_expiry"] = _authority_digest(
                expiry.encode("utf-8")
            )
        final_operation = _build_margin_job_operation(fresh_raw, verified_now)
    except (JobRefusal, KeyError, TypeError, ValueError):
        return "MARGIN_VALIDATION_UNKNOWN", verified_now, digests, stop_present
    except Exception:
        return "MARGIN_VALIDATION_UNKNOWN", verified_now, digests, stop_present
    if not isinstance(final_operation, dict):
        return "MARGIN_VALIDATION_UNKNOWN", verified_now, digests, stop_present
    if final_operation.get("verdict") == "REFUSED":
        reason = final_operation.get("reason")
        if type(reason) is not str or not reason:
            reason = "MARGIN_VALIDATION_UNKNOWN"
        return reason, verified_now, digests, stop_present
    try:
        fresh_job = _validate_margin_job(
            fresh_raw, job.job_id, verified_now, final_operation
        )
    except JobRefusal as error:
        return error.reason, verified_now, digests, stop_present
    except ValueError:
        return "MARGIN_VALIDATION_UNKNOWN", verified_now, digests, stop_present
    if fresh_job.operator_token != job.operator_token:
        return "OPERATOR_MISMATCH", verified_now, digests, stop_present
    if fresh_job != job or fresh_job.margin_operation != operation \
            or not isinstance(fresh_job.margin_operation, dict) \
            or fresh_job.margin_operation.get("params") != post_params:
        return (
            "MARGIN_VALIDATION_UNKNOWN", verified_now, digests, stop_present
        )
    if job.op == margin_ops.MARGIN_BORROW and stop_present:
        return "STOP_JOBS_SEALED", verified_now, digests, stop_present
    if job.op != margin_ops.MARGIN_BORROW:
        return "", verified_now, digests, stop_present
    if accept_encoded is None:
        return (
            "ENTRY_BLOCKED_NOT_ACCEPTED", verified_now, digests, stop_present
        )
    try:
        acceptance = _validate_accept_one_object(
            _decode_strict_object(accept_encoded), verified_now
        )
    except (JobRefusal, ValueError):
        return "ACCEPT_ONE_INVALID", verified_now, digests, stop_present
    if operator_encoded is None:
        return "OPERATOR_MISMATCH", verified_now, digests, stop_present
    try:
        operator = _validate_operator_object(
            _decode_strict_object(operator_encoded)
        )
    except (JobRefusal, ValueError):
        operator = None
    if operator is None or fresh_job.requested_by != operator \
            or acceptance.operator != operator \
            or fresh_job.operator_token != operator:
        return "OPERATOR_MISMATCH", verified_now, digests, stop_present
    if fresh_job.requested_max_notional > acceptance.max_notional:
        return (
            "ACCEPT_ONE_NOTIONAL_EXCEEDED", verified_now, digests,
            stop_present,
        )
    return "", verified_now, digests, stop_present


def _run_margin_operation(
        job: Job, paths: Dict[str, str],
        governor_token: str = "", sha: str = "",
        validated_job_digest: str = "",
        execution_services: Optional[ExecutionServices] = None) -> Outcome:
    fresh_now = _utc_now()
    operation = job.margin_operation
    if job.op == margin_ops.MARGIN_BORROW \
            and job.requested_max_notional > MARGIN_RUNNER_NOTIONAL_CAP \
            or not isinstance(operation, dict):
        return Outcome("REFUSED", 2, "", "MARGIN_VALIDATION_UNKNOWN")
    source_path = os.path.join(
        paths["receipts"], "margin-%s.json" % job.job_id
    )
    receipt = {
        "kind": "margin_operation",
        "job_id": job.job_id,
        "sha": sha,
        "ready": operation,
        "mutation_attempts": 0,
        "verdict": "READY",
        "reason": "",
        "started_utc": _utc_text(fresh_now),
    }
    if governor_token:
        receipt["governor_token"] = governor_token
        receipt["governor_release"] = "PENDING"
    try:
        _persist_margin_source(source_path, receipt, create=True)
    except Exception:
        return Outcome(
            "UNKNOWN", 4,
            source_path if os.path.isfile(source_path) else "",
            "MARGIN_READY_PERSISTENCE_FAILED",
        )

    try:
        services = execution_services or _build_execution_services()
        if not isinstance(services, ExecutionServices):
            raise ValueError("execution services are unavailable")
    except Exception:
        return _finish_margin_before_post(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_POST_UNREADABLE", fresh_now,
        )

    post_params = dict(operation["params"])
    authority_now = fresh_now
    params = operation["params"]
    history_reason, _history_before, history_ids_before = \
        _read_margin_history_snapshot(services, params["asset"])
    if history_reason:
        return _finish_margin_before_post(
            receipt, source_path, "UNKNOWN", 4,
            history_reason, authority_now,
        )
    history_watermark = max(history_ids_before, default=0)

    liability_reason, liability_before = _read_margin_liability(
        services, params["asset"]
    )
    if liability_reason or liability_before is None:
        return _finish_margin_before_post(
            receipt, source_path, "UNKNOWN", 4,
            liability_reason or "MARGIN_CONFIRMATION_UNREADABLE",
            authority_now,
        )
    liability_before_receipt = _margin_liability_receipt(liability_before)
    if liability_before_receipt is None:
        return _finish_margin_before_post(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_CONFIRMATION_UNREADABLE", authority_now,
        )

    adapter_operation, build_reason, definite_refusal = \
        _build_typed_margin_operation(
            services, job, operation, liability_before, sha
        )
    if adapter_operation is None:
        return _finish_margin_before_post(
            receipt, source_path,
            "REFUSED" if definite_refusal else "UNKNOWN",
            2 if definite_refusal else 4, build_reason, authority_now,
        )
    adapter_expected = _margin_operation_ledger_confirmation(
        adapter_operation
    )
    if adapter_expected is None:
        return _finish_margin_before_post(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_VALIDATION_UNKNOWN", authority_now,
        )
    if params["type"] == "BORROW":
        if adapter_expected != operation["expected_ledger_confirmation"]:
            return _finish_margin_before_post(
                receipt, source_path, "UNKNOWN", 4,
                "MARGIN_VALIDATION_UNKNOWN", authority_now,
            )
    else:
        ready_operation = dict(operation)
        ready_operation["expected_ledger_confirmation"] = adapter_expected
        receipt["ready"] = ready_operation

    intent_id = operation["intent_id"]
    try:
        intent_claimed = _claim_margin_intent(
            paths, intent_id, job.job_id, authority_now
        )
    except Exception:
        return _finish_margin_before_post(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_INTENT_CLAIM_PERSISTENCE_FAILED", authority_now,
        )
    if not intent_claimed:
        return _finish_margin_before_post(
            receipt, source_path, "REFUSED", 2,
            "INTENT_ALREADY_CLAIMED", authority_now,
        )
    receipt["mutation_attempts"] = 1
    receipt["mutation_started_utc"] = _utc_text(authority_now)
    receipt["history_watermark"] = history_watermark
    receipt["account_liability_before"] = liability_before_receipt
    receipt["adapter_intent"] = _adapter_intent_payload(
        adapter_operation.intent
    )
    try:
        _persist_margin_source(source_path, receipt)
    except Exception:
        return Outcome(
            "UNKNOWN", 4, source_path, "MARGIN_READY_PERSISTENCE_FAILED"
        )

    writer_called = False

    def write_intent(intent: binance_http.MutationIntent) -> None:
        nonlocal writer_called
        if writer_called or intent != adapter_operation.intent:
            raise binance_http.IntentNotDurable(
                "margin intent does not match the durable receipt"
            )
        durable = _load_plain_receipt(source_path)
        if not isinstance(durable, dict) \
                or durable.get("mutation_attempts") != 1 \
                or durable.get("adapter_intent") != _adapter_intent_payload(
                    intent
                ):
            raise binance_http.IntentNotDurable(
                "margin intent receipt was not durable"
            )
        writer_called = True

    def record_authority() -> None:
        receipt.update({
            "authority_checked_utc": _utc_text(authority_now),
            "authority_digests": authority_digests,
            "authority_stop_jobs_present": stop_present,
        })
        if job.op == margin_ops.MARGIN_REPAY and stop_present:
            receipt["execution_mode"] = "STOP_JOBS_REPAY"

    authority_refusal, authority_now, authority_digests, stop_present = \
        _margin_post_authority(
            job, paths, operation, post_params, validated_job_digest
        )
    if authority_refusal:
        record_authority()
        return _finish_margin_source(
            receipt, source_path, "REFUSED", 2,
            authority_refusal, authority_now,
        )
    try:
        submission = services.margin_submit_once(
            services.transport, services.spot_base_url,
            adapter_operation, write_intent,
        )
    except binance_http.IntentNotDurable:
        record_authority()
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_READY_PERSISTENCE_FAILED", fresh_now,
        )
    except Exception:
        record_authority()
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_POST_UNREADABLE", fresh_now,
        )
    record_authority()
    if not writer_called or not isinstance(
            submission, binance_margin.MarginSubmit):
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_POST_UNREADABLE", fresh_now,
        )

    transaction_id = ""
    if submission.kind == "accepted" \
            and type(submission.tx_id) is int and submission.tx_id > 0:
        transaction_id = str(submission.tx_id)
    receipt["submission"] = {
        "kind": "ok" if transaction_id else submission.kind,
        "transaction_id": transaction_id,
    }
    try:
        _persist_margin_source(source_path, receipt)
    except Exception:
        return Outcome(
            "UNKNOWN", 4, source_path,
            "MARGIN_RECEIPT_PERSISTENCE_FAILED",
        )
    if not transaction_id:
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_TRANSACTION_MISSING", fresh_now,
        )

    liability_reason, liability_after = _read_margin_liability(
        services, params["asset"]
    )
    if liability_reason or liability_after is None:
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            liability_reason or "MARGIN_CONFIRMATION_UNREADABLE",
            fresh_now,
        )
    history_reason, history_after, history_ids_after = \
        _read_margin_post_history(
            services, params["asset"], params["type"], transaction_id,
            history_watermark, job.job_id,
        )
    if history_reason:
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            history_reason, fresh_now,
        )
    confirmation = _evaluate_typed_margin_confirmation(
        services, history_after, history_ids_after,
        transaction_id, history_watermark, adapter_operation,
        liability_before, liability_after,
    )
    if confirmation.snapshot_time_note:
        receipt["snapshot_time_note"] = confirmation.snapshot_time_note
    if confirmation.follow_up_repay is not None:
        receipt["follow_up_repay"] = confirmation.follow_up_repay
    if confirmation.verdict == "UNKNOWN":
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4, confirmation.reason,
            fresh_now, confirmation.ledger_confirmation,
            confirmation.account_liability,
        )
    success_reason = confirmation.reason
    if job.op == margin_ops.MARGIN_REPAY and stop_present:
        success_reason = "REPAY_CONFIRMED_WITH_DUST_UNDER_STOP_JOBS" \
            if confirmation.verdict == "CLOSED-WITH-DUST" \
            else "REPAY_CONFIRMED_UNDER_STOP_JOBS"
    return _finish_margin_source(
        receipt, source_path, confirmation.verdict, 0, success_reason,
        fresh_now, confirmation.ledger_confirmation,
        confirmation.account_liability,
    )


def _execute(job: Job, receipt_dir: str, paths: Dict[str, str],
             basis_entry: Optional[Callable[[Any], Any]],
             now: datetime, governor_token: str = "",
             validated_job_digest: str = "",
             momentum_measurement: Optional[Dict[str, Any]] = None,
             execution_services: Optional[
                 ExecutionServices
             ] = None) -> Outcome:
    sha = os.environ.get("QUANT_SHA", "").lower()
    if not SHA_RE.fullmatch(sha):
        return Outcome("REFUSED", 2, "", "BAD_QUANT_SHA")
    if not os.environ.get("BINANCE_API_KEY") or not os.environ.get(
            "BINANCE_API_SECRET"):
        return Outcome("REFUSED", 2, "", "MISSING_CREDENTIALS")
    if job.op == "BASIS_ROUNDTRIP" and not os.path.isfile(paths["proof"]):
        return Outcome("REFUSED", 2, "", "SEQUENCE_LOCKED")
    if job.op in ("MOMENTUM_ROUNDTRIP", "MOMENTUM_OPEN"):
        basis_proof = _basis_proof_passed(paths)
        if not os.path.isfile(paths["proof"]) or not basis_proof:
            return Outcome("REFUSED", 2, "", "SEQUENCE_LOCKED")
    if job.op in MARGIN_OPS:
        return _run_margin_operation(
            job, paths, governor_token, sha, validated_job_digest,
            execution_services,
        )

    values = {
        "SYMBOL": job.symbol,
        "MAX_NOTIONAL": _decimal_text(job.effective_notional),
        "QUANT_SHA": sha,
        "RECEIPT_DIR": receipt_dir,
    }
    with _environment(values):
        if job.op == "MOMENTUM_OPEN":
            return _run_momentum_open(
                job, receipt_dir, paths, now, governor_token,
                execution_services,
            )
        if job.op == "MOMENTUM_EXIT":
            return _run_momentum_exit(
                job, receipt_dir, paths, now,
                measurement=momentum_measurement,
                execution_services=execution_services,
            )
        if job.op == "MOMENTUM_ROUNDTRIP":
            blocked = _momentum_roundtrip_allowed(paths)
            if blocked is not None:
                return blocked
        if job.op in ("FUTURES_ROUNDTRIP", "MOMENTUM_ROUNDTRIP"):
            return _run_futures_roundtrip(
                job, receipt_dir, paths, now, governor_token,
                execution_services,
            )
        try:
            _prove_futures_leverage(
                paths, job.job_id, job.symbol, sha,
                execution_services, now,
            )
        except MomentumStateError:
            return Outcome("UNKNOWN", 4, "", "LEVERAGE_UNPROVEN")
        return _run_basis(receipt_dir, job.effective_notional, basis_entry)


def _entry_outcome_terminal(outcome: Outcome) -> bool:
    return outcome.verdict not in FREEZE_VERDICTS


def _entry_outcome_succeeded(outcome: Outcome) -> bool:
    return outcome.exit_code == 0 and outcome.verdict in {"OPEN", "CLOSED"}


def _final_entry_outcome(paths: Dict[str, str], job_id: str, op: str,
                         outcome: Outcome) -> Outcome:
    if op in ENTRY_OPS and _entry_outcome_succeeded(outcome) \
            and not _consume_accept_one(paths["accept_one"], job_id):
        return Outcome(
            "UNKNOWN", 4, outcome.receipt_path,
            "ACCEPT_ONE_CLAIM_FAILED", outcome.ledger,
        )
    return outcome


def _release_recovered_governor(token: str, receipt_dir: str) -> bool:
    if not token:
        return True
    try:
        _acquire, release, _record, _budgets, _refusal = \
            _account_governor_api()
    except JobRefusal:
        return False
    return _release_governor(release, token, receipt_dir)


def _release_final_outcome_governor(
        paths: Dict[str, str], receipt_dir: str, job_id: str, op: str,
        source_path: str, governor_token: str, now: datetime,
        release: Optional[Callable[..., Any]] = None) -> bool:
    """Release and mark one terminal outcome through the common boundary."""
    if not governor_token:
        return False
    if release is None:
        try:
            _acquire, release, _record, _budgets, _refusal = \
                _account_governor_api()
        except JobRefusal:
            return False
    released = _release_governor(release, governor_token, receipt_dir)
    if released and op == margin_ops.MARGIN_BORROW:
        released = _mark_margin_governor_released(
            paths, job_id, source_path, governor_token, now, "RELEASED"
        )
    return released


def _load_plain_receipt(path: str) -> Optional[Dict[str, Any]]:
    try:
        payload = _decode_strict_plain_object(_read_file_bytes(path))
    except (JobRefusal, OSError, UnicodeError, ValueError,
            json.JSONDecodeError, RecursionError):
        return None
    return payload


def _safe_artifact_field(value: Any) -> str:
    if type(value) is str \
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value) is not None:
        return value
    return "$"


def _artifact_reason(artifact: str, field: str, anomaly_type: str) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", field).strip("_").upper()
    parts = [artifact]
    if name:
        parts.append(name)
    parts.append(anomaly_type)
    return "_".join(parts)


def _artifact_anomaly(artifact: str, field: str, anomaly_type: str,
                      reason: str = "", job_id: str = "UNKNOWN") \
        -> ArtifactAnomaly:
    safe_field = _safe_artifact_field(field)
    return ArtifactAnomaly(
        artifact, safe_field, anomaly_type,
        reason or _artifact_reason(artifact, safe_field, anomaly_type),
        job_id if type(job_id) is str and job_id else "UNKNOWN",
    )


def _preflight_json_depth(encoded: bytes, artifact: str,
                          reason_label: str) -> None:
    """Bound JSON nesting iteratively and retain its top-level field name."""
    try:
        text = encoded.decode("utf-8")
    except UnicodeError:
        raise _artifact_anomaly(
            artifact, "$", "INVALID_JSON", reason_label + "_INVALID_JSON"
        ) from None
    stack: List[str] = []
    in_string = False
    escaped = False
    string_start = 0
    root_key_expected = False
    pending_root_field = ""
    root_field = "$"
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
                if len(stack) == 1 and stack[0] == "{" \
                        and root_key_expected:
                    try:
                        decoded = json.loads(text[string_start:index + 1])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        decoded = ""
                    pending_root_field = _safe_artifact_field(decoded)
                    root_key_expected = False
            continue
        if character == '"':
            in_string = True
            string_start = index
            continue
        if character in "{[":
            stack.append(character)
            if len(stack) == 1 and character == "{":
                root_key_expected = True
            if len(stack) > MARGIN_ARTIFACT_MAX_DEPTH:
                raise _artifact_anomaly(
                    artifact, root_field, "DEPTH_LIMIT",
                    reason_label + "_STRUCTURE_LIMIT",
                )
            continue
        if character == ":" and len(stack) == 1 and stack[0] == "{":
            root_field = pending_root_field or "$"
            continue
        if character == "," and len(stack) == 1 and stack[0] == "{":
            pending_root_field = ""
            root_field = "$"
            root_key_expected = True
            continue
        if character in "}]" and stack:
            stack.pop()
            if not stack:
                root_field = "$"


def _bound_json_structure(payload: Dict[str, Any], artifact: str,
                          reason_label: str) -> None:
    """Walk decoded JSON without recursion under fixed depth and node bounds."""
    stack: List[Tuple[Any, int, str]] = [(payload, 0, "$")]
    visited = set()
    scheduled_nodes = 1
    visited_nodes = 0
    while stack:
        current, depth, top_field = stack.pop()
        visited_nodes += 1
        if visited_nodes > MARGIN_ARTIFACT_MAX_NODES:
            raise _artifact_anomaly(
                artifact, top_field, "NODE_LIMIT",
                reason_label + "_STRUCTURE_LIMIT",
            )
        if depth > MARGIN_ARTIFACT_MAX_DEPTH:
            raise _artifact_anomaly(
                artifact, top_field, "DEPTH_LIMIT",
                reason_label + "_STRUCTURE_LIMIT",
            )
        if isinstance(current, dict):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            if scheduled_nodes + len(current) > MARGIN_ARTIFACT_MAX_NODES:
                raise _artifact_anomaly(
                    artifact, top_field, "NODE_LIMIT",
                    reason_label + "_STRUCTURE_LIMIT",
                )
            children = []
            for name, value in current.items():
                child_field = _safe_artifact_field(name) \
                    if depth == 0 else top_field
                children.append((value, depth + 1, child_field))
            scheduled_nodes += len(children)
            stack.extend(reversed(children))
        elif isinstance(current, list):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            if scheduled_nodes + len(current) > MARGIN_ARTIFACT_MAX_NODES:
                raise _artifact_anomaly(
                    artifact, top_field, "NODE_LIMIT",
                    reason_label + "_STRUCTURE_LIMIT",
                )
            scheduled_nodes += len(current)
            stack.extend(
                (value, depth + 1, top_field)
                for value in reversed(current)
            )


def _decode_bounded_plain_object(encoded: bytes, artifact: str,
                                 reason_label: Optional[str] = None) \
        -> Dict[str, Any]:
    label = reason_label or artifact
    _preflight_json_depth(encoded, artifact, label)
    try:
        payload = _decode_strict_plain_object(encoded)
    except DuplicateField as error:
        raise _artifact_anomaly(
            artifact, error.field, "DUPLICATE_FIELD",
            label + "_DUPLICATE_FIELD",
        ) from None
    except RecursionError:
        raise _artifact_anomaly(
            artifact, "$", "DEPTH_LIMIT", label + "_STRUCTURE_LIMIT"
        ) from None
    except (JobRefusal, UnicodeError, ValueError, json.JSONDecodeError):
        raise _artifact_anomaly(
            artifact, "$", "INVALID_JSON", label + "_INVALID_JSON"
        ) from None
    _bound_json_structure(payload, artifact, label)
    return payload


def _artifact_value(payload: Dict[str, Any], artifact: str, field: str,
                    expected_type: type, job_id: str = "UNKNOWN",
                    optional: bool = False) -> Any:
    if field not in payload:
        if optional:
            return None
        raise _artifact_anomaly(
            artifact, field, "MISSING", job_id=job_id
        )
    value = payload[field]
    if type(value) is not expected_type:
        raise _artifact_anomaly(
            artifact, field, "TYPE", job_id=job_id
        )
    return value


def _validate_known_margin_artifact_fields(
        payload: Dict[str, Any], artifact: str, job_id: str) -> None:
    string_fields = {
        "RESULT": (
            "job_id", "op", "symbol", "verdict", "receipt_path", "utc",
        ),
        "RUNNER_RECEIPT": (
            "job_id", "op", "symbol", "verdict", "reason", "utc",
            "measured_sha", "sha", "source_receipt_path",
            "governor_token", "governor_release", "governor_released_utc",
        ),
        "SOURCE_RECEIPT": (
            "kind", "job_id", "sha", "verdict", "reason", "started_utc",
            "finished_utc", "governor_token", "governor_release",
            "governor_released_utc", "authority_checked_utc",
            "mutation_started_utc", "snapshot_time_note",
        ),
        "JOB_FILE": tuple(sorted(MARGIN_JOB_REQUIRED_FIELDS)),
        "LOCK": ("token", "op", "symbol", "utc"),
    }.get(artifact, ())
    for field in string_fields:
        if field in payload:
            _artifact_value(
                payload, artifact, field, str, job_id, optional=True
            )
    exact_types = {
        "RESULT": {},
        "RUNNER_RECEIPT": {
            "sha_valid": bool, "authority_digests": dict,
            "mutation_attempts": int,
        },
        "SOURCE_RECEIPT": {
            "ready": dict,
            "adapter_intent": dict,
            "submission": dict, "ledger_confirmation": dict,
            "account_liability_before": dict,
            "account_liability": dict, "authority_digests": dict,
            "authority_stop_jobs_present": bool,
            "history_watermark": int,
            "legacy_exclusivity_proof": dict,
        },
        "LOCK": {"version": int},
    }.get(artifact, {})
    for field, expected_type in exact_types.items():
        if field in payload:
            _artifact_value(
                payload, artifact, field, expected_type, job_id, optional=True
            )
    if artifact == "RESULT" and "exit_code" in payload \
            and type(payload["exit_code"]) not in (int, bool):
        raise _artifact_anomaly(
            artifact, "exit_code", "TYPE", job_id=job_id
        )
    if artifact == "SOURCE_RECEIPT" and "mutation_attempts" in payload \
            and type(payload["mutation_attempts"]) not in (int, bool):
        raise _artifact_anomaly(
            artifact, "mutation_attempts", "TYPE", job_id=job_id
        )


def _validate_artifact_types_like(actual: Any, expected: Any,
                                  artifact: str, field: str,
                                  job_id: str) -> None:
    stack = [(actual, expected, field)]
    while stack:
        current, model, current_field = stack.pop()
        if isinstance(model, dict):
            if type(current) is not dict:
                raise _artifact_anomaly(
                    artifact, current_field, "TYPE", job_id=job_id
                )
            for name, model_value in model.items():
                child_field = current_field + "." + name
                if name not in current:
                    raise _artifact_anomaly(
                        artifact, child_field, "MISSING", job_id=job_id
                    )
                stack.append((current[name], model_value, child_field))
        elif isinstance(model, list):
            if type(current) is not list:
                raise _artifact_anomaly(
                    artifact, current_field, "TYPE", job_id=job_id
                )
            if model and len(current) == len(model):
                stack.extend(
                    (value, model_value, current_field)
                    for value, model_value in zip(current, model)
                )
        elif type(current) is not type(model):
            raise _artifact_anomaly(
                artifact, current_field, "TYPE", job_id=job_id
            )


def _validate_artifact_string_map(payload: Dict[str, Any], artifact: str,
                                  field: str, job_id: str) -> None:
    values = _artifact_value(
        payload, artifact, field, dict, job_id
    )
    for name, value in values.items():
        child = field + "." + _safe_artifact_field(name)
        if type(name) is not str or type(value) is not str:
            raise _artifact_anomaly(
                artifact, child, "TYPE", job_id=job_id
            )


def _mark_margin_governor_released(
        paths: Dict[str, str], job_id: str, source_path: str,
        governor_token: str, now: datetime, state: str) -> bool:
    runner_path = os.path.join(paths["receipts"], "job-%s.json" % job_id)
    expected_source = os.path.join(
        paths["receipts"], "margin-%s.json" % job_id
    )
    if source_path and os.path.abspath(source_path) != os.path.abspath(
            expected_source):
        return False
    targets = [
        ("SOURCE_RECEIPT", expected_source),
        ("RUNNER_RECEIPT", runner_path),
    ] if source_path else [("RUNNER_RECEIPT", runner_path)]
    for artifact, path in targets:
        try:
            payload = _decode_bounded_plain_object(
                _read_file_bytes(path), artifact
            )
            _validate_known_margin_artifact_fields(
                payload, artifact, job_id
            )
        except (ArtifactAnomaly, OSError):
            return False
        if payload.get("job_id") != job_id \
                or payload.get("governor_token") != governor_token:
            return False
        payload["governor_release"] = state
        payload["governor_released_utc"] = _utc_text(now)
        try:
            _persist_json_receipt(path, payload)
        except Exception:
            return False
    return True


def _margin_governor_halt_rows(paths: Dict[str, str]) \
        -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    final_rows: Dict[str, Dict[str, Any]] = {}
    halt_rows: Dict[str, Dict[str, Any]] = {}
    try:
        encoded = _read_file_bytes(paths["results"])
        for line in encoded.splitlines():
            if not line:
                raise _artifact_anomaly(
                    "RESULT", "$", "INVALID_JSON", "RESULTS_INVALID_JSON"
                )
            row = _decode_bounded_plain_object(
                line, "RESULT", "RESULTS"
            )
            if set(row) == MARGIN_GOVERNOR_HALT_FIELDS \
                    and row.get("kind") == "margin_governor_release_halted" \
                    and type(row.get("job_id")) is str:
                for name in (
                        "lock_exists", "runner_receipt_exists",
                        "source_receipt_exists"):
                    if type(row.get(name)) is not bool:
                        raise _artifact_anomaly(
                            "RESULT", name, "TYPE", job_id=row["job_id"]
                        )
                for name in (
                        "lock_token", "lock_op", "lock_symbol",
                        "runner_verdict", "source_verdict", "utc"):
                    if type(row.get(name)) is not str:
                        raise _artifact_anomaly(
                            "RESULT", name, "TYPE", job_id=row["job_id"]
                        )
                halt_rows[row["job_id"]] = row
            elif set(row) == RESULT_FIELDS:
                row_job_id = row.get("job_id") \
                    if type(row.get("job_id")) is str else "UNKNOWN"
                _validate_known_margin_artifact_fields(
                    row, "RESULT", row_job_id
                )
                if row["op"] == margin_ops.MARGIN_BORROW \
                        and row["verdict"] not in FREEZE_VERDICTS:
                    final_rows[row["job_id"]] = row
    except FileNotFoundError:
        return {}, {}
    except OSError:
        raise _artifact_anomaly(
            "RESULT", "$", "READ_ERROR", "RESULTS_READ_ERROR"
        ) from None
    return final_rows, halt_rows


def _margin_receipt_fact(path: str, artifact: str, job_id: str) \
        -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    exists = os.path.exists(path)
    if not exists:
        return False, "ABSENT", None
    try:
        encoded = _read_file_bytes(path)
    except OSError:
        raise _artifact_anomaly(
            artifact, "$", "READ_ERROR", job_id=job_id
        ) from None
    payload = _decode_bounded_plain_object(encoded, artifact)
    _validate_known_margin_artifact_fields(payload, artifact, job_id)
    verdict = _artifact_value(payload, artifact, "verdict", str, job_id)
    return True, verdict, payload


def _margin_lock_fact(path: str, job_id: str) \
        -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        encoded = _read_file_bytes(path)
    except OSError:
        raise _artifact_anomaly(
            "LOCK", "$", "READ_ERROR", job_id=job_id
        ) from None
    lock = _decode_bounded_plain_object(encoded, "LOCK")
    _validate_known_margin_artifact_fields(lock, "LOCK", job_id)
    for field in ("token", "op", "symbol", "utc"):
        _artifact_value(lock, "LOCK", field, str, job_id)
    _artifact_value(lock, "LOCK", "version", int, job_id)
    return lock


def _append_margin_governor_anomaly(
        paths: Dict[str, str], receipt_dir: str, job_id: str,
        anomaly: ArtifactAnomaly, now: datetime, emit: bool = False) -> bool:
    event = {
        "kind": "margin_governor_artifact_halted",
        "job_id": job_id if type(job_id) is str and job_id else "UNKNOWN",
        "artifact": anomaly.artifact,
        "field": anomaly.field,
        "anomaly_type": anomaly.anomaly_type,
        "lock_exists": os.path.exists(os.path.join(
            receipt_dir, ACCOUNT_GOVERNOR_LOCK_FILE
        )),
        "utc": _utc_text(now),
    }
    facts = {name: value for name, value in event.items() if name != "utc"}
    found = False
    try:
        for line in _read_file_bytes(paths["results"]).splitlines():
            try:
                prior = _decode_bounded_plain_object(
                    line, "RESULT", "RESULTS"
                )
            except ArtifactAnomaly:
                continue
            if set(prior) == MARGIN_GOVERNOR_ANOMALY_FIELDS and {
                    name: value for name, value in prior.items()
                    if name != "utc"} == facts:
                found = True
                break
    except (FileNotFoundError, OSError):
        pass
    if not found:
        try:
            _append_results_row(paths, event)
        except Exception:
            return False
        if emit:
            print(json.dumps(event, separators=(",", ":")))
    return True


def _halt_interrupted_margin_governor(
        paths: Dict[str, str], receipt_dir: str, now: datetime) \
        -> Optional[bool]:
    lock_path = os.path.join(receipt_dir, ACCOUNT_GOVERNOR_LOCK_FILE)
    lock_exists = os.path.exists(lock_path)
    current_job_id = "UNKNOWN"
    try:
        final_rows, prior_halts = _margin_governor_halt_rows(paths)
        if len(final_rows) == 1:
            current_job_id = next(iter(final_rows))
        lock = _margin_lock_fact(lock_path, current_job_id) \
            if lock_exists else None
        for job_id in sorted(final_rows):
            current_job_id = job_id
            row = final_rows[job_id]
            runner_path = os.path.join(
                paths["receipts"], "job-%s.json" % job_id
            )
            source_path = os.path.join(
                paths["receipts"], "margin-%s.json" % job_id
            )
            runner_exists, runner_verdict, runner = _margin_receipt_fact(
                runner_path, "RUNNER_RECEIPT", job_id
            )
            source_exists, source_verdict, source = _margin_receipt_fact(
                source_path, "SOURCE_RECEIPT", job_id
            )
            pending = (
                isinstance(runner, dict)
                and runner.get("governor_release") == "PENDING"
            ) or (
                isinstance(source, dict)
                and source.get("governor_release") == "PENDING"
            )
            tokens = {
                payload.get("governor_token")
                for payload in (runner, source)
                if isinstance(payload, dict)
                and type(payload.get("governor_token")) is str
                and payload.get("governor_token")
            }
            lock_owned = isinstance(lock, dict) \
                and lock["token"] in tokens
            result_names_runner = os.path.abspath(row["receipt_path"]) \
                == os.path.abspath(runner_path)
            unreadable_evidence = lock_exists and result_names_runner and (
                runner_exists and runner is None
                or source_exists and source is None
                or lock is None
            )
            if not pending and not lock_owned and not unreadable_evidence:
                continue
            absent = "ABSENT" if not lock_exists else "UNKNOWN"
            event = {
                "kind": "margin_governor_release_halted",
                "job_id": job_id,
                "lock_exists": lock_exists,
                "lock_token": lock["token"]
                if isinstance(lock, dict) else absent,
                "lock_op": lock["op"]
                if isinstance(lock, dict) else absent,
                "lock_symbol": lock["symbol"]
                if isinstance(lock, dict) else absent,
                "runner_receipt_exists": runner_exists,
                "runner_verdict": runner_verdict,
                "source_receipt_exists": source_exists,
                "source_verdict": source_verdict,
                "utc": _utc_text(now),
            }
            prior = prior_halts.get(job_id, {})
            facts = {
                name: value for name, value in event.items() if name != "utc"
            }
            prior_facts = {
                name: value for name, value in prior.items() if name != "utc"
            }
            if facts != prior_facts:
                try:
                    _append_results_row(paths, event)
                except Exception:
                    return False
                print(json.dumps(event, separators=(",", ":")))
            return True
    except ArtifactAnomaly as anomaly:
        anomaly_job_id = current_job_id \
            if current_job_id != "UNKNOWN" else anomaly.job_id
        return _append_margin_governor_anomaly(
            paths, receipt_dir, anomaly_job_id, anomaly, now, emit=True
        )
    return None


def _release_artifact(path: str, label: str) \
        -> Tuple[Dict[str, Any], bytes]:
    try:
        encoded = _read_file_bytes(path)
        return _decode_bounded_plain_object(encoded, label), encoded
    except FileNotFoundError:
        raise _artifact_anomaly(
            label, "$", "MISSING", label + "_MISSING"
        ) from None
    except OSError:
        raise _artifact_anomaly(
            label, "$", "READ_ERROR", label + "_INVALID_JSON"
        ) from None


def _release_result(paths: Dict[str, str], job_id: str) -> Dict[str, Any]:
    try:
        lines = _read_file_bytes(paths["results"]).splitlines()
        if any(not line for line in lines):
            raise _artifact_anomaly(
                "RESULT", "$", "INVALID_JSON", "RESULTS_INVALID_JSON",
                job_id,
            )
        rows = [
            _decode_bounded_plain_object(line, "RESULT", "RESULTS")
            for line in lines
        ]
    except FileNotFoundError:
        raise _artifact_anomaly(
            "RESULT", "$", "MISSING", "RESULT_MISSING", job_id
        ) from None
    except OSError:
        raise _artifact_anomaly(
            "RESULT", "$", "READ_ERROR", "RESULTS_INVALID_JSON", job_id
        ) from None
    matches = []
    reconciliations = []
    legacy_proofs = []
    for index, row in enumerate(rows):
        if set(row) == MARGIN_LEGACY_PROOF_FIELDS \
                and row.get("job_id") == job_id:
            if not _valid_margin_legacy_proof_row(row):
                raise JobRefusal("RESULT_RECONCILIATION_INVALID")
            legacy_proofs.append((index, row))
            continue
        if set(row) in (
                MARGIN_RECONCILED_FIELDS,
                MARGIN_LEGACY_RECONCILED_FIELDS,
                MARGIN_LEGACY_REPAY_RECONCILED_FIELDS) \
                and row.get("job_id") == job_id:
            if not _valid_margin_reconciled_row(row):
                raise JobRefusal("RESULT_RECONCILIATION_INVALID")
            reconciliations.append((index, row))
            continue
        if set(row) != RESULT_FIELDS:
            continue
        row_job_id = row.get("job_id") \
            if type(row.get("job_id")) is str else job_id
        _validate_known_margin_artifact_fields(row, "RESULT", row_job_id)
        if row["job_id"] == job_id:
            matches.append((index, row))
    if len(matches) == 2 and len(reconciliations) == 1:
        first_index, first = matches[0]
        final_index, final = matches[1]
        event_index, event = reconciliations[0]
        legacy_event = set(event) == MARGIN_LEGACY_RECONCILED_FIELDS
        if legacy_event:
            proof_matches = len(legacy_proofs) == 1 \
                and event_index < legacy_proofs[0][0] < final_index \
                and legacy_proofs[0][1]["transaction_id"] \
                == event["transaction_id"] \
                and legacy_proofs[0][1]["legacy_exclusivity_proof"] \
                == event["legacy_exclusivity_proof"]
        else:
            proof_matches = not legacy_proofs
        if first_index < event_index < final_index \
                and proof_matches \
                and (first.get("verdict"), first.get("exit_code")) \
                == ("UNKNOWN", 4) \
                and (final.get("verdict"), final.get("exit_code")) \
                == (event["verdict"], 0) \
                and all(first.get(name) == final.get(name) for name in (
                    "job_id", "op", "symbol", "receipt_path",
                )) \
                and final.get("op") == event["op"] \
                and final.get("symbol") == event["asset"]:
            return final
        raise JobRefusal("RESULT_RECONCILIATION_INVALID")
    if len(matches) != 1 or reconciliations or legacy_proofs:
        raise JobRefusal("RESULT_MISSING" if not matches else "RESULT_DUPLICATE")
    return matches[0][1]


def _release_require(condition: bool, reason: str) -> None:
    if not condition:
        raise JobRefusal(reason)


def _release_compare(label: str, actual: Dict[str, Any],
                     expected: Dict[str, Any]) -> None:
    for name, value in expected.items():
        _release_require(actual.get(name) == value,
                         "%s_%s_MISMATCH" % (label, name.upper()))


def _margin_reconcile_unconfirmed(
        paths: Dict[str, str], job_id: str, reason: str,
        now: datetime, fact: str = "") -> int:
    row = {
        "kind": "RECONCILE_UNCONFIRMED",
        "job_id": job_id,
        "reason": reason,
        "utc": _utc_text(now),
    }
    try:
        _append_results_row(paths, row)
    except Exception:
        print("JOB-RUNNER | RECONCILE REFUSED | EVIDENCE_PERSISTENCE_FAILED")
        return 3
    detail = reason if not fact else reason + " | " + fact
    print("JOB-RUNNER | RECONCILE UNCONFIRMED | " + detail)
    return 3


def _reconcile_unknown_result(
        paths: Dict[str, str], job_id: str, op: str, asset: str,
        runner_path: str) -> None:
    try:
        lines = _read_file_bytes(paths["results"]).splitlines()
        if any(not line for line in lines):
            raise JobRefusal("RESULTS_INVALID")
        rows = [
            _decode_bounded_plain_object(line, "RESULT", "RESULTS")
            for line in lines
        ]
    except FileNotFoundError:
        raise JobRefusal("RESULT_MISSING") from None
    except (ArtifactAnomaly, OSError):
        raise JobRefusal("RESULTS_INVALID") from None
    matches = [
        row for row in rows
        if set(row) == RESULT_FIELDS and row.get("job_id") == job_id
    ]
    _release_require(len(matches) == 1, "RESULT_UNKNOWN_MISMATCH")
    result = matches[0]
    _release_require(
        result.get("op") == op
        and result.get("symbol") == asset
        and result.get("verdict") == "UNKNOWN"
        and type(result.get("exit_code")) is int
        and result["exit_code"] == 4
        and type(result.get("receipt_path")) is str
        and os.path.abspath(result["receipt_path"])
        == os.path.abspath(runner_path),
        "RESULT_UNKNOWN_MISMATCH",
    )


def _legacy_repay_allowlisted_facts(
        job_id: Any, transaction_id: Any, asset: Any,
        requested_amount: Any, writer_sha: Any) \
        -> Tuple[str, str, str, str, str]:
    matches = [
        facts for facts in LEGACY_REPAY_RECEIPTS
        if facts[0] == job_id
    ]
    if len(matches) != 1:
        raise LegacyProvenanceMismatch("JOB_ID")
    facts = matches[0]
    for fact, actual, expected in (
            ("SUBMISSION_TRANSACTION_ID", transaction_id, facts[1]),
            ("ASSET", asset, facts[2]),
            ("REQUESTED_AMOUNT", requested_amount, facts[3]),
            ("WRITER_RELEASE_SHA", writer_sha, facts[4])):
        if actual != expected:
            raise LegacyProvenanceMismatch(fact)
    return facts


def _legacy_repay_result_ledger_fact(
        results_path: str, job_id: str) -> str:
    try:
        lines = _read_file_bytes(results_path).splitlines()
        if not lines or any(not line for line in lines):
            return "RESULT_LEDGER_READ"
        rows = [
            _decode_bounded_plain_object(line, "RESULT", "RESULTS")
            for line in lines
        ]
    except (ArtifactAnomaly, JobRefusal, OSError):
        return "RESULT_LEDGER_READ"
    job_rows = [
        row for row in rows
        if set(row) == RESULT_FIELDS and row.get("job_id") == job_id
    ]
    if not job_rows:
        return "RESULT_LEDGER_JOB_ID"
    operation_rows = [
        row for row in job_rows if row.get("op") == margin_ops.MARGIN_REPAY
    ]
    if not operation_rows:
        return "RESULT_LEDGER_OPERATION"
    expected_basename = "job-%s.json" % job_id
    if not any(
            type(row.get("receipt_path")) is str
            and os.path.basename(row["receipt_path"]) == expected_basename
            for row in operation_rows):
        return "RESULT_LEDGER_RECEIPT_PATH"
    return ""


def _load_margin_reconcile_facts(
        paths: Dict[str, str], receipt_dir: str, job_id: str) \
        -> Tuple[
            str, str, Dict[str, Any], Dict[str, Any], Dict[str, Any],
            Dict[str, str], Dict[str, str], Optional[Dict[str, str]],
            str, Optional[int], str,
            Optional[Tuple[str, str, str, str, str]],
        ]:
    source_path = os.path.join(
        paths["receipts"], "margin-%s.json" % job_id
    )
    runner_path = os.path.join(
        paths["receipts"], "job-%s.json" % job_id
    )
    source = _release_artifact(source_path, "SOURCE_RECEIPT")[0]
    runner = _release_artifact(runner_path, "RUNNER_RECEIPT")[0]
    legacy_receipt = set(source) == set(LEGACY_RECEIPT_FIELDS)
    _validate_known_margin_artifact_fields(
        source, "SOURCE_RECEIPT", job_id
    )
    _validate_known_margin_artifact_fields(
        runner, "RUNNER_RECEIPT", job_id
    )
    _release_require(source.get("job_id") == job_id, "SOURCE_JOB_MISMATCH")
    _release_require(
        source.get("verdict") == "UNKNOWN", "SOURCE_RECEIPT_NOT_UNKNOWN"
    )
    _release_require(
        runner.get("job_id") == job_id
        and runner.get("verdict") == "UNKNOWN",
        "RUNNER_RECEIPT_NOT_UNKNOWN",
    )
    ready = _artifact_value(
        source, "SOURCE_RECEIPT", "ready", dict, job_id
    )
    ready_fields = {
        "verdict", "op", "client_tag", "intent_id", "expires_utc",
        "endpoint_path", "params", "expected_ledger_confirmation",
        "evidence",
    }
    _release_require(
        set(ready) == ready_fields and ready.get("verdict") == "READY"
        and type(ready.get("op")) is str and ready["op"] in MARGIN_OPS
        and type(ready.get("endpoint_path")) is str
        and ready["endpoint_path"] == MARGIN_ENDPOINT_PATH
        and all(type(ready.get(name)) is str for name in (
            "client_tag", "intent_id", "expires_utc",
        ))
        and type(ready.get("evidence")) is list
        and all(type(value) is str for value in ready["evidence"]),
        "SOURCE_READY_MISMATCH",
    )
    params = ready.get("params")
    expected = ready.get("expected_ledger_confirmation")
    _release_require(
        type(params) is dict
        and set(params) == {"asset", "amount", "type"}
        and all(type(params.get(name)) is str for name in params)
        and params["asset"] in MARGIN_ASSET_WHITELIST
        and params["type"] in {"BORROW", "REPAY"}
        and ready["op"] == "MARGIN_" + params["type"]
        and _margin_decimal_text(params["amount"]) == params["amount"]
        and Decimal(params["amount"]) > Decimal("0"),
        "SOURCE_READY_MISMATCH",
    )
    expected_row = {
        "asset": params["asset"],
        "principal": params["amount"],
        "interest": "0",
        "status": params["type"] + "_CONFIRMED",
    }
    _release_require(type(expected) is dict, "SOURCE_READY_MISMATCH")
    submission = _artifact_value(
        source, "SOURCE_RECEIPT", "submission", dict, job_id
    )
    transaction_id = submission.get("transaction_id")
    _release_require(
        set(submission) == {"kind", "transaction_id"}
        and submission.get("kind") == "ok"
        and type(transaction_id) is str
        and _margin_transaction_number(transaction_id) is not None,
        "MARGIN_TRANSACTION_MISSING",
    )
    history_watermark = None
    if not legacy_receipt:
        _release_require(
            "history_watermark" in source, "HISTORY_WATERMARK_MISSING"
        )
        history_watermark = source["history_watermark"]
        _release_require(
            type(history_watermark) is int and history_watermark >= 0,
            "HISTORY_WATERMARK_MISSING",
        )
    _release_require(
        "legacy_exclusivity_proof" not in source,
        "SOURCE_LEGACY_EXCLUSIVITY_PROOF_UNEXPECTED",
    )
    _release_require(
        type(source.get("mutation_attempts")) is int
        and source["mutation_attempts"] == 1,
        "SOURCE_MUTATION_ATTEMPTS_MISMATCH",
    )
    liability_before = None
    if not legacy_receipt:
        liability_before = _artifact_value(
            source, "SOURCE_RECEIPT", "account_liability_before", dict,
            job_id,
        )
        _release_require(
            _margin_liability_from_receipt(
                liability_before, params["asset"]
            ) is not None,
            "SOURCE_ACCOUNT_LIABILITY_BEFORE_MISMATCH",
        )
    legacy_repay = None
    if params["type"] == "REPAY" and not legacy_receipt:
        if expected == expected_row:
            legacy_repay = _legacy_repay_allowlisted_facts(
                source.get("job_id"), transaction_id, params["asset"],
                params["amount"], source.get("sha"),
            )
            ledger_fact = _legacy_repay_result_ledger_fact(
                paths["results"], job_id
            )
            if ledger_fact:
                raise LegacyProvenanceMismatch(ledger_fact)
        else:
            modern_expected = _margin_repay_receipt_expected(
                params, liability_before
            )
            _release_require(
                modern_expected is not None and expected == modern_expected,
                "SOURCE_READY_MISMATCH",
            )
    else:
        _release_require(expected == expected_row, "SOURCE_READY_MISMATCH")
    _release_require(
        runner.get("op") == ready["op"]
        and runner.get("symbol") == params["asset"]
        and type(runner.get("source_receipt_path")) is str
        and os.path.abspath(runner["source_receipt_path"])
        == os.path.abspath(source_path),
        "RUNNER_RECEIPT_MISMATCH",
    )
    governor_token = source.get("governor_token", "")
    if ready["op"] == margin_ops.MARGIN_BORROW:
        _release_require(
            type(governor_token) is str and bool(governor_token)
            and runner.get("governor_token") == governor_token
            and source.get("governor_release") == "PENDING"
            and runner.get("governor_release") == "PENDING",
            "GOVERNOR_RECEIPT_MISMATCH",
        )
        lock_path = os.path.join(
            receipt_dir, ACCOUNT_GOVERNOR_LOCK_FILE
        )
        lock = _margin_lock_fact(lock_path, job_id)
        _release_require(
            isinstance(lock, dict)
            and set(lock) == {"version", "token", "op", "symbol", "utc"}
            and lock.get("version") == 1
            and lock.get("token") == governor_token
            and lock.get("op") == ready["op"]
            and lock.get("symbol") == params["asset"],
            "LOCK_MISMATCH",
        )
    else:
        _release_require(
            governor_token in {"", None}, "GOVERNOR_RECEIPT_MISMATCH"
        )
        governor_token = ""
    digests = _artifact_value(
        source, "SOURCE_RECEIPT", "authority_digests", dict, job_id
    )
    _validate_artifact_string_map(
        source, "SOURCE_RECEIPT", "authority_digests", job_id
    )
    operator_digest = digests.get("operator")
    _release_require(
        type(operator_digest) is str
        and re.fullmatch(r"[0-9a-f]{64}", operator_digest) is not None,
        "OPERATOR_DIGEST_MISSING",
    )
    try:
        operator_encoded = _read_file_bytes(paths["operator"])
    except FileNotFoundError:
        operator_encoded = None
    except OSError:
        raise JobRefusal("OPERATOR_DIGEST_MISMATCH") from None
    _release_require(
        _authority_digest(operator_encoded) == operator_digest,
        "OPERATOR_DIGEST_MISMATCH",
    )
    _reconcile_unknown_result(
        paths, job_id, ready["op"], params["asset"], runner_path
    )
    return (
        source_path, runner_path, source, runner, ready, params, expected,
        liability_before, transaction_id, history_watermark,
        governor_token, legacy_repay,
    )


def reconcile_margin(
        job_id: str, confirm_reconcile: bool,
        receipt_dir: Optional[str] = None,
        now: Optional[datetime] = None,
        execution_services: Optional[ExecutionServices] = None) -> int:
    if confirm_reconcile is not True:
        print("JOB-RUNNER | RECONCILE REFUSED | CONFIRM_RECONCILE_REQUIRED")
        return 3
    if type(job_id) is not str or JOB_ID_RE.fullmatch(job_id) is None:
        print("JOB-RUNNER | RECONCILE REFUSED | BAD_JOB_ID")
        return 3
    receipt_dir = os.path.abspath(os.path.expanduser(
        receipt_dir or os.environ.get("RECEIPT_DIR", "~/quant_receipts")
    ))
    paths = _paths(receipt_dir)
    now = now or _utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    try:
        facts = _load_margin_reconcile_facts(
            paths, receipt_dir, job_id
        )
    except ArtifactAnomaly as anomaly:
        return _margin_reconcile_unconfirmed(
            paths, job_id, anomaly.reason, now
        )
    except LegacyProvenanceMismatch as mismatch:
        return _margin_reconcile_unconfirmed(
            paths, job_id, mismatch.reason, now, mismatch.fact
        )
    except JobRefusal as refusal:
        return _margin_reconcile_unconfirmed(
            paths, job_id, refusal.reason, now
        )
    (
        source_path, runner_path, source, runner, ready, params, expected,
        liability_before, transaction_id, history_watermark,
        governor_token, legacy_repay,
    ) = facts
    legacy_receipt = set(source) == set(LEGACY_RECEIPT_FIELDS)
    legacy_exclusivity_proof = None

    try:
        services = execution_services or _build_execution_services()
        if not isinstance(services, ExecutionServices):
            raise ValueError("execution services are unavailable")
        history_reason, exact_history = _read_margin_history_identity(
            services, params["asset"], params["type"], transaction_id
        )
        if history_reason or exact_history is None:
            return _margin_reconcile_unconfirmed(
                paths, job_id,
                history_reason or "MARGIN_CONFIRMATION_MISMATCH", now
            )
        if legacy_repay:
            provenance_fact = _legacy_repay_exchange_provenance(
                exact_history, legacy_repay
            )
            if provenance_fact:
                return _margin_reconcile_unconfirmed(
                    paths, job_id, "LEGACY_PROVENANCE_MISMATCH", now,
                    provenance_fact,
                )
            history_confirmation, history_fact_reason = \
                _legacy_repay_history_confirmation(
                    exact_history, transaction_id, expected,
                    liability_before,
                )
            if history_confirmation is None:
                return _margin_reconcile_unconfirmed(
                    paths, job_id, history_fact_reason, now
                )

        liability_reason, liability_after = _read_margin_liability(
            services, params["asset"]
        )
        if liability_reason or liability_after is None:
            return _margin_reconcile_unconfirmed(
                paths, job_id,
                liability_reason or "MARGIN_CONFIRMATION_UNREADABLE", now
            )
        if legacy_receipt:
            history_reason, history_after, history_identifiers, \
                legacy_exclusivity_proof = \
                _read_margin_legacy_exclusivity(
                    services, params["asset"], transaction_id,
                    source.get("mutation_started_utc"), now,
                )
        else:
            history_reason, history_after, history_identifiers = \
                _read_margin_history_snapshot(
                    services, params["asset"]
                )
        if history_reason:
            return _margin_reconcile_unconfirmed(
                paths, job_id, history_reason, now
            )
        target = _margin_transaction_number(transaction_id)
        snapshot_history = _margin_target_history(
            history_after, params["type"], target or 0
        )
        if snapshot_history != exact_history:
            return _margin_reconcile_unconfirmed(
                paths, job_id, "MARGIN_CONFIRMATION_MISMATCH", now
            )
    except Exception:
        return _margin_reconcile_unconfirmed(
            paths, job_id, "MARGIN_CONFIRMATION_UNREADABLE", now
        )

    if legacy_repay:
        confirmation = _evaluate_legacy_repay_confirmation(
            exact_history, history_identifiers, transaction_id,
            history_watermark, expected, liability_before,
            liability_after,
        )
    elif legacy_receipt:
        confirmation = _evaluate_legacy_margin_confirmation(
            exact_history, history_identifiers, transaction_id, expected,
            params["type"], liability_after, legacy_exclusivity_proof,
        )
    else:
        adapter_operation = _margin_operation_from_receipt(
            services, ready, liability_before, source.get("sha")
        )
        typed_before = _margin_liability_from_receipt(
            liability_before, params["asset"]
        )
        if adapter_operation is None or typed_before is None:
            return _margin_reconcile_unconfirmed(
                paths, job_id, "SOURCE_READY_MISMATCH", now
            )
        confirmation = _evaluate_typed_margin_confirmation(
            services, history_after, history_identifiers,
            transaction_id, history_watermark, adapter_operation,
            typed_before, liability_after,
        )
    if confirmation.verdict == "UNKNOWN" \
            or confirmation.ledger_confirmation is None \
            or confirmation.account_liability is None \
            or not confirmation.snapshot_time_note:
        return _margin_reconcile_unconfirmed(
            paths, job_id, confirmation.reason, now
        )

    source_final = dict(source)
    source_final.update({
        "verdict": confirmation.verdict,
        "reason": confirmation.reason,
        "finished_utc": _utc_text(now),
        "ledger_confirmation": confirmation.ledger_confirmation,
        "account_liability": confirmation.account_liability,
        "snapshot_time_note": confirmation.snapshot_time_note,
    })
    if confirmation.follow_up_repay is None:
        source_final.pop("follow_up_repay", None)
    else:
        source_final["follow_up_repay"] = confirmation.follow_up_repay
    if legacy_exclusivity_proof is not None:
        source_final["legacy_exclusivity_proof"] = \
            legacy_exclusivity_proof
    runner_final = dict(runner)
    runner_final.update({
        "verdict": confirmation.verdict,
        "reason": confirmation.reason,
        "utc": _utc_text(now),
    })
    event = {
        "kind": "RECONCILED_FROM_EXCHANGE_TRUTH",
        "job_id": job_id,
        "op": ready["op"],
        "asset": params["asset"],
        "verdict": confirmation.verdict,
        "reason": confirmation.reason,
        "transaction_id": transaction_id,
        "ledger_confirmation": confirmation.ledger_confirmation,
        "account_liability": confirmation.account_liability,
        "snapshot_time_note": confirmation.snapshot_time_note,
        "utc": _utc_text(now),
    }
    legacy_event = None
    if not legacy_receipt:
        event["history_watermark"] = history_watermark
        event["account_liability_before"] = liability_before
        if legacy_repay:
            event["receipt_shape"] = LEGACY_REPAY_SHAPE
            event["receipt_shape_version"] = LEGACY_REPAY_SHAPE_VERSION
    else:
        event["legacy_exclusivity_proof"] = legacy_exclusivity_proof
        legacy_event = {
            "kind": "LEGACY_PROOF",
            "job_id": job_id,
            "transaction_id": transaction_id,
            "history_watermark_substitute":
                LEGACY_HISTORY_WATERMARK_SUBSTITUTE,
            "account_liability_before_substitute":
                LEGACY_ACCOUNT_LIABILITY_BEFORE_SUBSTITUTE,
            "legacy_exclusivity_proof": legacy_exclusivity_proof,
            "utc": _utc_text(now),
        }
    try:
        _persist_margin_source(source_path, source_final)
        _persist_json_receipt(runner_path, runner_final)
        _append_results_row(paths, event)
        if legacy_event is not None:
            _append_results_row(paths, legacy_event)
        _append_result(
            paths, job_id, ready["op"], params["asset"],
            Outcome(confirmation.verdict, 0, runner_path), now,
        )
    except Exception:
        _touch_stop(paths["stop"])
        print("JOB-RUNNER | RECONCILE REFUSED | PERSISTENCE_FAILED")
        return 3

    if governor_token and not _release_final_outcome_governor(
            paths, receipt_dir, job_id, ready["op"], source_path,
            governor_token, now):
        _touch_stop(paths["stop"])
        print("JOB-RUNNER | RECONCILED | GOVERNOR_RELEASE_PENDING")
        return 3
    print("JOB-RUNNER | RECONCILED | " + job_id)
    return 0


def _attest_stuck_margin_lock(
        paths: Dict[str, str], receipt_dir: str, job_id: str) \
        -> Tuple[str, str, bool]:
    _release_require(type(job_id) is str and JOB_ID_RE.fullmatch(job_id),
                     "BAD_JOB_ID")
    runner_path = os.path.join(paths["receipts"], "job-%s.json" % job_id)
    source_path = os.path.join(paths["receipts"], "margin-%s.json" % job_id)
    job_path = os.path.join(paths["jobs"], "job-%s.json" % job_id)
    lock_path = os.path.join(receipt_dir, ACCOUNT_GOVERNOR_LOCK_FILE)
    result = _release_result(paths, job_id)
    runner, _encoded = _release_artifact(runner_path, "RUNNER_RECEIPT")
    _validate_known_margin_artifact_fields(
        runner, "RUNNER_RECEIPT", job_id
    )
    source_exists = os.path.exists(source_path)
    _release_require(source_exists == ("source_receipt_path" in runner),
                     "SOURCE_RECEIPT_PRESENCE_MISMATCH")
    source = _release_artifact(source_path, "SOURCE_RECEIPT")[0] \
        if source_exists else None
    if source is not None:
        _validate_known_margin_artifact_fields(
            source, "SOURCE_RECEIPT", job_id
        )
    job, job_encoded = _release_artifact(job_path, "JOB_FILE")
    _validate_known_margin_artifact_fields(job, "JOB_FILE", job_id)
    for field in sorted(MARGIN_JOB_REQUIRED_FIELDS):
        _artifact_value(job, "JOB_FILE", field, str, job_id)

    _release_require(result["op"] == margin_ops.MARGIN_BORROW,
                     "RESULT_OP_MISMATCH")
    _release_require(type(result["exit_code"]) is int
                     and (result["verdict"], result["exit_code"])
                     in {("OPEN", 0), ("REFUSED", 2)},
                     "RESULT_OUTCOME_MISMATCH")
    _release_require(result["symbol"] == job["asset"],
                     "RESULT_SYMBOL_MISMATCH")
    _release_require(os.path.abspath(result["receipt_path"])
                     == os.path.abspath(runner_path),
                     "RESULT_RECEIPT_PATH_MISMATCH")
    release_state = _artifact_value(
        runner, "RUNNER_RECEIPT", "governor_release", str, job_id
    )
    sha_valid = _artifact_value(
        runner, "RUNNER_RECEIPT", "sha_valid", bool, job_id
    )
    _release_require(release_state in {"PENDING", "RELEASED"},
                     "RUNNER_GOVERNOR_RELEASE_MISMATCH")
    already_released = release_state == "RELEASED"
    runner_fields = {
        "job_id", "op", "symbol", "verdict", "reason", "utc",
        "measured_sha", "sha_valid", "authority_digests", "governor_token",
        "governor_release",
    } | ({"sha", "source_receipt_path"} if source_exists
         else {"mutation_attempts"})
    if already_released:
        runner_fields.add("governor_released_utc")
    if sha_valid:
        runner_fields.add("sha")
    _release_require(set(runner) == runner_fields,
                     "RUNNER_RECEIPT_SHAPE_MISMATCH")
    _release_compare("RUNNER", runner, {
        "job_id": job_id, "op": margin_ops.MARGIN_BORROW,
        "symbol": job["asset"], "verdict": result["verdict"],
        "utc": result["utc"], "governor_release": release_state,
    })
    token = _artifact_value(
        runner, "RUNNER_RECEIPT", "governor_token", str, job_id
    )
    _release_require(bool(token), "RUNNER_TOKEN_INVALID")
    measured_sha = _artifact_value(
        runner, "RUNNER_RECEIPT", "measured_sha", str, job_id
    )
    _release_require(
        sha_valid
        and SHA_RE.fullmatch(measured_sha.lower()) is not None
        and runner.get("sha") == measured_sha.lower()
        or not sha_valid
        and SHA_RE.fullmatch(measured_sha.lower()) is None
        and "sha" not in runner,
        "RUNNER_SHA_MISMATCH",
    )
    _validate_artifact_string_map(
        runner, "RUNNER_RECEIPT", "authority_digests", job_id
    )
    digests = runner["authority_digests"]
    _release_require(digests.get("job_file")
                     == hashlib.sha256(job_encoded).hexdigest(),
                     "JOB_DIGEST_MISMATCH")
    try:
        validated = _validate_margin_job(
            job, job_id, _timestamp(job["created_utc"])
        )
    except JobRefusal:
        raise JobRefusal("JOB_FILE_INVALID") from None

    released_utc = ""
    if already_released:
        released_utc = _artifact_value(
            runner, "RUNNER_RECEIPT", "governor_released_utc", str, job_id
        )
        try:
            _timestamp(released_utc)
        except JobRefusal:
            raise JobRefusal("RUNNER_RELEASE_UTC_INVALID") from None

    if source is None:
        _release_require(set(digests) == {"job_file"},
                         "RUNNER_DIGESTS_SHAPE_MISMATCH")
        _release_require(result["verdict"] == "REFUSED"
                         and type(runner.get("mutation_attempts")) is int
                         and runner["mutation_attempts"] == 0
                         and type(runner.get("reason")) is str
                         and bool(runner["reason"]),
                         "RUNNER_ONLY_OUTCOME_MISMATCH")
    else:
        legacy_final_fields = set(LEGACY_RECEIPT_FIELDS) | {
            "ledger_confirmation", "account_liability",
            "snapshot_time_note", "legacy_exclusivity_proof",
        }
        if already_released:
            legacy_final_fields.add("governor_released_utc")
        legacy_source = result["verdict"] == "OPEN" \
            and set(source) == legacy_final_fields
        source_fields = set(legacy_final_fields) if legacy_source else {
            "kind", "job_id", "sha", "ready", "mutation_attempts",
            "verdict", "reason", "started_utc", "finished_utc",
            "governor_token", "governor_release", "authority_checked_utc",
            "authority_digests", "authority_stop_jobs_present",
            "adapter_intent",
        }
        has_history_watermark = "history_watermark" in source
        has_legacy_exclusivity_proof = \
            "legacy_exclusivity_proof" in source
        if has_history_watermark:
            source_fields.add("history_watermark")
            _release_require(
                type(source["history_watermark"]) is int
                and source["history_watermark"] >= 0,
                "SOURCE_HISTORY_WATERMARK_MISMATCH",
            )
        if has_legacy_exclusivity_proof:
            source_fields.add("legacy_exclusivity_proof")
            _release_require(
                _valid_margin_legacy_exclusivity_proof(
                    source["legacy_exclusivity_proof"]),
                "SOURCE_LEGACY_EXCLUSIVITY_PROOF_MISMATCH",
            )
        if already_released and not legacy_source:
            source_fields.add("governor_released_utc")
        if result["verdict"] == "OPEN" and not legacy_source:
            source_fields |= {
                "mutation_started_utc", "submission", "ledger_confirmation",
                "account_liability_before", "account_liability",
                "snapshot_time_note",
            }
        _release_require(set(source) == source_fields,
                         "SOURCE_RECEIPT_SHAPE_MISMATCH")
        _validate_artifact_types_like(
            source["ready"], validated.margin_operation,
            "SOURCE_RECEIPT", "ready", job_id,
        )
        _validate_artifact_string_map(
            source, "SOURCE_RECEIPT", "authority_digests", job_id
        )
        _release_compare("SOURCE", source, {
            "kind": "margin_operation", "job_id": job_id,
            "sha": runner.get("sha"), "ready": validated.margin_operation,
            "verdict": result["verdict"], "reason": runner["reason"],
            "governor_token": token, "governor_release": release_state,
            "authority_digests": digests,
        })
        _release_require(runner["source_receipt_path"] == source_path,
                         "SOURCE_RECEIPT_PATH_MISMATCH")
        if already_released:
            source_released_utc = _artifact_value(
                source, "SOURCE_RECEIPT", "governor_released_utc", str,
                job_id,
            )
            _release_require(source_released_utc == released_utc,
                             "SOURCE_RELEASE_UTC_MISMATCH")
            try:
                _timestamp(source_released_utc)
            except JobRefusal:
                raise JobRefusal("SOURCE_RELEASE_UTC_INVALID") from None
        digest_fields = {
            "ready_params", "job_file", "accept_one", "operator",
            "stop_jobs", "operator_token", "job_expiry",
        }
        _release_require(set(digests) == digest_fields and all(
            re.fullmatch(r"[0-9a-f]{64}", value) is not None
            for value in digests.values()
        ), "SOURCE_DIGESTS_SHAPE_MISMATCH")
        _release_require(source["authority_stop_jobs_present"] is
                         (digests.get("stop_jobs") != _authority_digest(None)),
                         "SOURCE_STOP_JOBS_DIGEST_MISMATCH")
        ready = source["ready"]
        _release_compare("SOURCE_DIGEST", digests, {
            "ready_params": _json_authority_digest(ready.get("params")),
            "operator_token": _authority_digest(
                job["operator_token"].encode("utf-8")
            ),
            "job_expiry": _authority_digest(
                job["expires_utc"].encode("utf-8")
            ),
        })
        if result["verdict"] == "OPEN":
            principal = _canonical_decimal_text(validated.requested_max_notional)
            submission = source["submission"]
            _release_require(
                legacy_source
                and not has_history_watermark
                and has_legacy_exclusivity_proof
                or not legacy_source
                and has_history_watermark
                and not has_legacy_exclusivity_proof,
                "SOURCE_HISTORY_PROOF_MISMATCH",
            )
            if has_legacy_exclusivity_proof:
                proof = source["legacy_exclusivity_proof"]
                try:
                    expected_window_start = _utc_text(
                        _timestamp(source["mutation_started_utc"])
                        - MARGIN_LEGACY_HISTORY_WINDOW_PAD
                    )
                except (JobRefusal, TypeError):
                    raise JobRefusal(
                        "SOURCE_LEGACY_EXCLUSIVITY_PROOF_MISMATCH"
                    ) from None
                _release_require(
                    proof["window_start_utc"] == expected_window_start
                    and proof["window_end_utc"] == source["finished_utc"],
                    "SOURCE_LEGACY_EXCLUSIVITY_PROOF_MISMATCH",
                )
            _release_require(
                source["snapshot_time_note"]
                == MARGIN_SNAPSHOT_TIME_UNVERIFIED
                if legacy_source else source["snapshot_time_note"] in {
                    MARGIN_SNAPSHOT_TIME_VERIFIED,
                    MARGIN_SNAPSHOT_TIME_UNVERIFIED,
                },
                "SOURCE_SNAPSHOT_TIME_NOTE_MISMATCH",
            )
            _release_require(runner["reason"]
                             == ready["expected_ledger_confirmation"]["status"],
                             "RUNNER_REASON_MISMATCH")
            _release_require(type(source["mutation_attempts"]) is int,
                             "SOURCE_MUTATION_ATTEMPTS_MISMATCH")
            expected_facts = {
                "mutation_attempts": 1,
                "ledger_confirmation": ready["expected_ledger_confirmation"],
            }
            _validate_artifact_types_like(
                source["ledger_confirmation"],
                expected_facts["ledger_confirmation"],
                "SOURCE_RECEIPT", "ledger_confirmation", job_id,
            )
            _validate_artifact_types_like(
                source["account_liability"],
                {
                    "asset": validated.symbol, "borrowed": "",
                    "interest": "", "total_liability": "",
                },
                "SOURCE_RECEIPT", "account_liability", job_id,
            )
            _release_compare("SOURCE", source, expected_facts)
            after_liability = source["account_liability"]
            if legacy_source:
                _release_require(
                    _margin_liability_from_receipt(
                        after_liability, validated.symbol
                    ) is not None
                    and _legacy_current_liability_matches(
                        after_liability, principal
                    ),
                    "SOURCE_ACCOUNT_LIABILITY_MISMATCH",
                )
            else:
                before_liability = source["account_liability_before"]
                _validate_artifact_types_like(
                    before_liability,
                    {
                        "asset": validated.symbol, "borrowed": "",
                        "interest": "", "total_liability": "",
                    },
                    "SOURCE_RECEIPT", "account_liability_before", job_id,
                )
                _release_require(
                    _margin_liability_from_receipt(
                        before_liability, validated.symbol
                    ) is not None
                    and _margin_liability_from_receipt(
                        after_liability, validated.symbol
                    ) is not None
                    and _margin_liability_direction_confirmed(
                        before_liability, after_liability, principal, "BORROW"
                    ),
                    "SOURCE_ACCOUNT_LIABILITY_MISMATCH",
                )
            _validate_artifact_types_like(
                submission, {"kind": "ok", "transaction_id": ""},
                "SOURCE_RECEIPT", "submission", job_id,
            )
            _release_require(
                set(submission) == {"kind", "transaction_id"}
                and submission.get("kind") == "ok"
                and bool(submission["transaction_id"]),
                "SOURCE_SUBMISSION_MISMATCH",
            )
        else:
            _release_require(source["mutation_attempts"] == 0
                             and bool(source["reason"]),
                             "SOURCE_REFUSAL_MISMATCH")

    if already_released:
        _release_require(not os.path.exists(lock_path),
                         "LOCK_PRESENT_AFTER_RELEASE")
        return token, source_path if source_exists else "", True

    lock = _release_artifact(lock_path, "LOCK")[0]
    _validate_known_margin_artifact_fields(lock, "LOCK", job_id)
    for field in ("token", "op", "symbol", "utc"):
        _artifact_value(lock, "LOCK", field, str, job_id)
    _artifact_value(lock, "LOCK", "version", int, job_id)
    _release_require(set(lock) == {"version", "token", "op", "symbol", "utc"}
                     and lock["version"] == 1, "LOCK_SHAPE_MISMATCH")
    _release_compare("LOCK", lock, {
        "token": token, "op": margin_ops.MARGIN_BORROW,
        "symbol": validated.symbol,
    })
    try:
        _timestamp(lock["utc"])
    except JobRefusal:
        raise JobRefusal("LOCK_UTC_INVALID") from None
    return token, source_path if source_exists else "", False


def release_stuck_lock(job_id: str, confirm_release: bool,
                       receipt_dir: Optional[str] = None,
                       now: Optional[datetime] = None) -> int:
    if confirm_release is not True:
        print("JOB-RUNNER | RELEASE REFUSED | CONFIRM_RELEASE_REQUIRED")
        return 3
    receipt_dir = os.path.abspath(os.path.expanduser(
        receipt_dir or os.environ.get("RECEIPT_DIR", "~/quant_receipts")
    ))
    paths = _paths(receipt_dir)
    now = now or _utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    try:
        token, source_path, already_released = _attest_stuck_margin_lock(
            paths, receipt_dir, job_id
        )
    except ArtifactAnomaly as anomaly:
        _touch_stop(paths["stop"])
        _append_margin_governor_anomaly(
            paths, receipt_dir, job_id, anomaly, now
        )
        print("JOB-RUNNER | RELEASE REFUSED | " + anomaly.reason)
        return 3
    except JobRefusal as refusal:
        _touch_stop(paths["stop"])
        print("JOB-RUNNER | RELEASE REFUSED | " + refusal.reason)
        return 3
    if already_released:
        print("JOB-RUNNER | ALREADY_RELEASED | " + job_id)
        return 0
    if not _release_recovered_governor(token, receipt_dir):
        reason = "GOVERNOR_RELEASE_FAILED"
    elif os.path.exists(os.path.join(receipt_dir, ACCOUNT_GOVERNOR_LOCK_FILE)):
        reason = "LOCK_RELEASE_UNCONFIRMED"
    elif not _mark_margin_governor_released(
            paths, job_id, source_path, token, now, "RELEASED"):
        reason = "RECEIPT_MARKING_FAILED"
    else:
        print("JOB-RUNNER | RELEASED | " + job_id)
        return 0
    _touch_stop(paths["stop"])
    print("JOB-RUNNER | RELEASE REFUSED | " + reason)
    return 3


def _realized_close_values(outcome: Outcome) \
        -> Optional[Tuple[Decimal, Decimal]]:
    if outcome.verdict != "CLOSED" or not isinstance(outcome.ledger, dict):
        return None
    realized = outcome.ledger.get("realized")
    if not isinstance(realized, dict):
        return None
    net_raw = realized.get("net_usdt")
    totals = realized.get("commission_by_asset")
    if type(net_raw) is not str or not isinstance(totals, dict) \
            or any(type(key) is not str or type(value) is not str
                   for key, value in totals.items()) \
            or not set(totals).issubset({"USDT"}):
        return None
    try:
        net = Decimal(net_raw)
        fees = Decimal(totals.get("USDT", "0"))
    except (ArithmeticError, ValueError):
        return None
    if not net.is_finite() or not fees.is_finite() or fees < 0:
        return None
    return net, fees


def _record_realized_close(outcome: Outcome, receipt_dir: str) -> bool:
    values = _realized_close_values(outcome)
    if values is None:
        return True
    try:
        _acquire, _release, record, _budgets, _refusal = \
            _account_governor_api()
        record(values[0], values[1], receipt_dir)
    except Exception:
        return False
    return True


def _finish_claimed(paths: Dict[str, str], receipt_dir: str, job_id: str,
                    op: str, symbol: str, outcome: Outcome,
                    now: datetime, accept_one_checked: bool = False,
                    governor_token: str = "",
                    validated_job_digest: str = "") -> int:
    if not accept_one_checked:
        outcome = _final_entry_outcome(paths, job_id, op, outcome)
    if outcome.reason or outcome.ledger is not None:
        receipt_path = _write_runner_receipt(
            paths, job_id, op, symbol, outcome.verdict, outcome.reason, now,
            outcome.receipt_path, outcome.ledger, governor_token,
            validated_job_digest,
        )
        outcome = Outcome(
            outcome.verdict, outcome.exit_code, receipt_path, "",
            outcome.ledger,
        )
    if outcome.verdict == "CLOSED":
        proof_path = {
            "FUTURES_ROUNDTRIP": paths["proof"],
            "BASIS_ROUNDTRIP": paths["basis_proof"],
        }.get(op)
        published_job = os.path.isfile(os.path.join(
            paths["jobs"], "job-%s.json" % job_id
        ))
        clean_momentum_close = op == "MOMENTUM_EXIT" and published_job
        if clean_momentum_close:
            proof_path = paths["first_autonomous_close"]
        if proof_path:
            _write_proof_marker(proof_path, job_id, _utc_text(now))
        if op == "MOMENTUM_EXIT":
            _touch_stop(paths["stop"])
        if clean_momentum_close:
            _expire_accept_one(paths["accept_one"], now)
    if outcome.verdict in FREEZE_VERDICTS:
        _touch_stop(paths["stop"])
    _append_result(paths, job_id, op, symbol, outcome, now)
    if not _record_realized_close(outcome, receipt_dir):
        _touch_stop(paths["stop"])
        print("JOB-RUNNER | GOVERNOR_RECORD_FAILED | entries stopped")
    print("JOB | %s | %s | %s | exit %s" % (
        job_id, op or "INVALID", outcome.verdict, outcome.exit_code
    ))
    return outcome.exit_code


def _max_adverse_stop(paths: Dict[str, str], receipt_dir: str,
                      now: datetime,
                      execution_services: Optional[
                          ExecutionServices
                      ] = None) \
        -> Tuple[Optional[int], Optional[Dict[str, Any]], bool]:
    try:
        position = _load_momentum_position(paths)
    except MomentumStateError:
        _touch_stop(paths["stop"])
        _append_adverse_stop_event(
            paths, "adverse_stop_refusal", "", now,
            "UNKNOWN_POSITION",
        )
        return 2, None, False
    if position is not None and position["state"] == "UNKNOWN":
        _touch_stop(paths["stop"])
        _append_adverse_stop_event(
            paths, "adverse_stop_blocked_unknown", position["symbol"], now
        )
        return None, None, True
    if position is None or position["state"] != "OPEN":
        return None, None, False

    sha = str(position.get("sha", "")).lower()
    try:
        effective = _momentum_decimal(
            position.get("effective_notional"),
            "persisted effective notional",
        )
    except MomentumStateError:
        _touch_stop(paths["stop"])
        _append_adverse_stop_event(
            paths, "adverse_stop_refusal", position["symbol"], now,
            "UNKNOWN_CONFIG",
        )
        return 2, None, False
    if not SHA_RE.fullmatch(sha):
        _touch_stop(paths["stop"])
        _append_adverse_stop_event(
            paths, "adverse_stop_refusal", position["symbol"], now,
            "UNKNOWN_CONFIG",
        )
        return 2, None, False

    values = {
        "SYMBOL": position["symbol"],
        "MAX_NOTIONAL": _decimal_text(effective),
        "QUANT_SHA": sha,
        "RECEIPT_DIR": receipt_dir,
    }
    with _environment(values):
        active_services = execution_services
        try:
            venue = _momentum_venue(position)
            if venue == "spot":
                services = _spot_services(execution_services)
                measured = _spot_measure(position, services)
            else:
                active_services = _futures_services(execution_services)
                measured = _futures_measure(
                    position["symbol"], active_services
                )
        except Exception as error:
            _touch_stop(paths["stop"])
            _append_adverse_stop_event(
                paths, "adverse_stop_refusal", position["symbol"], now,
                type(error).__name__,
            )
            return 2, None, False

        entry = position.get("entry")
        try:
            entry_price = _momentum_decimal(
                entry.get("price") if isinstance(entry, dict) else None,
                "persisted entry price",
            )
        except MomentumStateError:
            _touch_stop(paths["stop"])
            _append_adverse_stop_event(
                paths, "adverse_stop_refusal", position["symbol"], now,
                "UNKNOWN_ENTRY_PRICE",
            )
            return 2, None, False
        try:
            current_price = _momentum_decimal(
                measured.get("price"), "current mark price"
            )
        except MomentumStateError as error:
            _touch_stop(paths["stop"])
            _append_adverse_stop_event(
                paths, "adverse_stop_refusal", position["symbol"], now,
                type(error).__name__,
            )
            return 2, None, False

        side = entry.get("side", "LONG")
        # The typed preflight rejects a short position for this long-only path.
        if side not in ("LONG", "BUY"):
            _touch_stop(paths["stop"])
            _append_adverse_stop_event(
                paths, "adverse_stop_refusal", position["symbol"], now,
                "UNKNOWN_SIDE",
            )
            return 2, None, False
        adverse_pct = (
            (entry_price - current_price) * Decimal("100") / entry_price
        )
        if adverse_pct + limits.MEASURED_ROUNDTRIP_FEE_PCT \
                < limits.MAX_ADVERSE_PCT:
            return None, measured, False

        job_id = "max-adverse-" + secrets.token_hex(8)
        if not _claim(paths, job_id, now):
            print("JOB-RUNNER | MAX_ADVERSE_STOP | REFUSED | CLAIM_FAILED")
            return 3, None, False
        job = Job(
            job_id=job_id,
            op="MOMENTUM_EXIT",
            symbol=position["symbol"],
            requested_max_notional=effective,
            effective_notional=effective,
            created_utc=now,
            expires_utc=now + MAX_JOB_AGE,
            requested_by="job-runner",
            venue=venue,
        )
        outcome = _run_momentum_exit(
            job, receipt_dir, paths, now, measurement=measured,
            outcome_reason="max_adverse_stop",
            execution_services=active_services,
        )
    code = _finish_claimed(
        paths, receipt_dir, job.job_id, job.op, job.symbol, outcome, now
    )
    return code, None, False


def tick(receipt_dir: Optional[str] = None, now: Optional[datetime] = None,
         basis_entry: Optional[Callable[[Any], Any]] = None,
         execution_services: Optional[ExecutionServices] = None) -> int:
    """Consume at most one due job and return its factual exit code."""
    receipt_dir = os.path.abspath(os.path.expanduser(
        receipt_dir or os.environ.get("RECEIPT_DIR", "~/quant_receipts")
    ))
    now = now or _utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    paths = _paths(receipt_dir)
    _make_dirs(paths)
    if execution_services is None \
            and os.environ.get("BINANCE_API_KEY") \
            and os.environ.get("BINANCE_API_SECRET"):
        try:
            execution_services = _build_execution_services()
        except binance_http.AdapterRefusal:
            execution_services = None

    try:
        token, stale, stale_recovery = _acquire_tick_lock(
            paths["tick_lock"], now
        )
    except OSError:
        _touch_stop(paths["stop"])
        print("JOB-RUNNER | HALTED | runner heartbeat unavailable")
        return 3
    if stale:
        _touch_stop(paths["stop"])
        print("JOB-RUNNER | HALTED | unresolved runner lock")
        return 3
    if not token:
        print("JOB-RUNNER | BUSY | another tick is active")
        return 0
    try:
        if not _refresh_tick_lock(paths["tick_lock"], token, now):
            _touch_stop(paths["stop"])
            print("JOB-RUNNER | HALTED | runner heartbeat unavailable")
            return 3
        if stale_recovery is not None:
            try:
                _append_results_row(paths, stale_recovery)
            except Exception:
                _touch_stop(paths["stop"])
                print("JOB-RUNNER | HALTED | stale lock evidence unavailable")
                return 3
            print(json.dumps(stale_recovery, separators=(",", ":")))
        # Release boundary: a Margin governor lock that survived an earlier
        # tick is never released by a later tick.  Review its durable halt row,
        # then use release-stuck-lock with the job id and confirmation.
        margin_governor_halt = _halt_interrupted_margin_governor(
            paths, receipt_dir, now
        )
        if margin_governor_halt is not None:
            _touch_stop(paths["stop"])
            if not margin_governor_halt:
                print("JOB-RUNNER | HALTED | governor evidence unavailable")
            else:
                print("JOB-RUNNER | HALTED | governor release is pending")
            return 3
        result_ids, bad_results, unresolved_result = _result_ids(paths["results"])
        if bad_results:
            _touch_stop(paths["stop"])
            print("JOB-RUNNER | HALTED | unreconciled receipt or lock")
            return 3
        if unresolved_result or _unresolved_account_state(receipt_dir):
            _touch_stop(paths["stop"])
            print("JOB-RUNNER | HALTED | unreconciled receipt or lock")
            return 3
        recovery = _recover_pending_momentum(
            paths, receipt_dir, result_ids, now, execution_services,
        )
        if recovery is not None:
            job_id, op, symbol, outcome, governor_token = recovery
            outcome = _final_entry_outcome(paths, job_id, op, outcome)
            code = _finish_claimed(
                paths, receipt_dir, job_id, op, symbol, outcome, now,
                accept_one_checked=True,
            )
            if governor_token and _entry_outcome_terminal(outcome) \
                    and not _release_recovered_governor(
                        governor_token, receipt_dir
                    ):
                _touch_stop(paths["stop"])
            return code
        if not _claims_are_resolved(paths, result_ids):
            _touch_stop(paths["stop"])
            print("JOB-RUNNER | HALTED | unreconciled receipt or lock")
            return 3
        adverse_code, standing_measurement, adverse_blocked_unknown = \
            _max_adverse_stop(
                paths, receipt_dir, now, execution_services
            )

        for path in sorted(glob.glob(os.path.join(paths["jobs"], "job-*.json"))):
            match = JOB_NAME_RE.fullmatch(os.path.basename(path))
            if not match:
                continue
            filename_id = match.group(1)
            if filename_id in result_ids:
                continue
            raw: Dict[str, Any] = {}
            refusal = ""
            job = None
            validated_job_digest = ""
            try:
                raw, validated_job_digest = _load_job_with_digest(path)
                job = _validate_job(raw, filename_id, now, receipt_dir)
                if job.created_utc > now:
                    continue
            except JobRefusal as rejected:
                refusal = rejected.reason
            if not _claim(paths, filename_id, now):
                return 0
            if refusal:
                op = _safe_text(raw, "op")
                symbol = _safe_text(raw, "asset") if op in MARGIN_OPS \
                    else _safe_text(raw, "symbol")
                outcome = Outcome("REFUSED", 2, "", refusal)
                return _finish_claimed(
                    paths, receipt_dir, filename_id, op, symbol, outcome, now,
                    validated_job_digest=validated_job_digest,
                )
            if job.op in MARGIN_OPS \
                    and _margin_intent_is_claimed(paths, job.intent_id):
                outcome = Outcome(
                    "REFUSED", 2, "", "INTENT_ALREADY_CLAIMED"
                )
                return _finish_claimed(
                    paths, receipt_dir, job.job_id, job.op,
                    job.symbol, outcome, now,
                    validated_job_digest=validated_job_digest,
                )
            if job.op == margin_ops.MARGIN_BORROW \
                    and os.path.isfile(paths["stop"]):
                outcome = Outcome("REFUSED", 2, "", "STOP_JOBS_SEALED")
                return _finish_claimed(
                    paths, receipt_dir, job.job_id, job.op,
                    job.symbol, outcome, now,
                    validated_job_digest=validated_job_digest,
                )
            if job.op in ENTRY_OPS:
                refusal = _entry_ticket_refusal(job, paths, now)
                if refusal:
                    outcome = Outcome("REFUSED", 2, "", refusal)
                    return _finish_claimed(
                        paths, receipt_dir, job.job_id, job.op,
                        job.symbol, outcome, now,
                        validated_job_digest=validated_job_digest,
                    )
            governor_token = ""
            release_governor = None
            if job.op in ENTRY_OPS:
                governor_token, release_governor, refusal = \
                    _acquire_governor(job, receipt_dir)
                if refusal:
                    outcome = Outcome("REFUSED", 2, "", refusal)
                else:
                    outcome = _execute(
                        job, receipt_dir, paths, basis_entry, now,
                        governor_token,
                        validated_job_digest,
                        momentum_measurement=standing_measurement,
                        execution_services=execution_services,
                    )
            else:
                outcome = _execute(
                    job, receipt_dir, paths, basis_entry, now,
                    validated_job_digest=validated_job_digest,
                    momentum_measurement=standing_measurement,
                    execution_services=execution_services,
                )
            outcome = _final_entry_outcome(
                paths, job.job_id, job.op, outcome
            )
            code = _finish_claimed(
                paths, receipt_dir, job.job_id, job.op, job.symbol,
                outcome, now, accept_one_checked=True,
                governor_token=governor_token,
                validated_job_digest=validated_job_digest,
            )
            # A token acquired by this tick is released inline when that same
            # tick reaches a terminal outcome, as for every other entry op.
            if governor_token and _entry_outcome_terminal(outcome):
                governor_released = _release_final_outcome_governor(
                    paths, receipt_dir, job.job_id, job.op,
                    outcome.receipt_path, governor_token, now,
                    release_governor,
                )
                if not governor_released:
                    _touch_stop(paths["stop"])
            return code
        if adverse_blocked_unknown:
            return 0
        if adverse_code is not None:
            return adverse_code
        print("JOB-RUNNER | IDLE | no due unclaimed job")
        return 0
    finally:
        _release_tick_lock(paths["tick_lock"], token)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="consume one strictly validated live-test job"
    )
    parser.add_argument("--tick", action="store_true",
                        help="consume at most one due job")
    commands = parser.add_subparsers(dest="command")
    release = commands.add_parser(
        "release-stuck-lock",
        help="manually release one fully attested governor lock",
    )
    release.add_argument("--job-id", required=True)
    release.add_argument(
        "--confirm-release", action="store_true", required=True
    )
    reconcile = commands.add_parser(
        "reconcile-margin",
        help="reconcile one UNKNOWN Margin receipt from exchange reads",
    )
    reconcile.add_argument("--job-id", required=True)
    reconcile.add_argument(
        "--confirm-reconcile", action="store_true", required=True
    )
    args = parser.parse_args(argv)
    if args.command == "release-stuck-lock":
        if args.tick:
            parser.error("choose either --tick or release-stuck-lock")
        return release_stuck_lock(args.job_id, args.confirm_release)
    if args.command == "reconcile-margin":
        if args.tick:
            parser.error("choose either --tick or reconcile-margin")
        return reconcile_margin(args.job_id, args.confirm_reconcile)
    if args.tick:
        return tick()
    parser.error(
        "--tick, release-stuck-lock, or reconcile-margin is required"
    )


if __name__ == "__main__":
    raise SystemExit(main())
