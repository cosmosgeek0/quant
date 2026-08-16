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
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any, Callable, Dict, List, Optional, Tuple

import basis_cycle
import basis_executor
import futures_hand
import limits
import margin_orders
import margin_ops
import reverse_mechanics
import set_leverage_1x
import spot_roundtrip
from reverse_mechanics import next_action, plan_reverse_mechanics


COMMON_JOB_REQUIRED_FIELDS = {
    "job_id",
    "op",
    "created_utc",
    "expires_utc",
    "requested_by",
}
TRADING_JOB_REQUIRED_FIELDS = COMMON_JOB_REQUIRED_FIELDS | {
    "symbol", "max_notional",
}
TRADING_JOB_OPTIONAL_FIELDS = {"venue"}
TRADING_JOB_FIELDS = TRADING_JOB_REQUIRED_FIELDS | TRADING_JOB_OPTIONAL_FIELDS
MARGIN_JOB_REQUIRED_FIELDS = COMMON_JOB_REQUIRED_FIELDS | {
    "asset", "principal", "client_tag", "intent_id", "operator_token",
}
MARGIN_JOB_FIELDS = MARGIN_JOB_REQUIRED_FIELDS
REVERSE_OP = "REVERSE_MECHANICS_ROUNDTRIP"
REVERSE_JOB_REQUIRED_FIELDS = COMMON_JOB_REQUIRED_FIELDS | {
    "symbol", "quote_cap", "borrow_asset", "intent_id", "client_tag",
    "operator_token",
}
REVERSE_JOB_FIELDS = REVERSE_JOB_REQUIRED_FIELDS
JOB_REQUIRED_FIELDS = TRADING_JOB_REQUIRED_FIELDS
JOB_OPTIONAL_FIELDS = TRADING_JOB_OPTIONAL_FIELDS
JOB_FIELDS = TRADING_JOB_FIELDS
ACCEPT_ONE_FIELDS = {
    "operator", "created_utc", "expires_utc", "max_notional",
}
OPERATOR_FIELDS = {"operator"}
RESULT_FIELDS = {
    "job_id", "op", "symbol", "verdict", "exit_code", "receipt_path", "utc"
}
MOMENTUM_LEDGER_FIELDS = {"fills", "fees", "realized"}
MOMENTUM_RESULT_FIELDS = RESULT_FIELDS | MOMENTUM_LEDGER_FIELDS
ADVERSE_STOP_REFUSAL_FIELDS = {"kind", "error", "symbol", "utc"}
ADVERSE_STOP_BLOCKED_FIELDS = {"kind", "symbol", "utc"}
MARGIN_GOVERNOR_HALT_FIELDS = {
    "kind", "job_id", "lock_exists", "lock_token", "lock_op",
    "lock_symbol", "runner_receipt_exists", "runner_verdict",
    "source_receipt_exists", "source_verdict", "utc",
}
MARGIN_GOVERNOR_ANOMALY_FIELDS = {
    "kind", "job_id", "artifact", "field", "anomaly_type",
    "lock_exists", "utc",
}
MARGIN_RECONCILED_FIELDS = {
    "kind", "job_id", "op", "asset", "verdict", "reason",
    "transaction_id", "history_watermark", "ledger_confirmation",
    "account_liability_before", "account_liability",
    "snapshot_time_note", "utc",
}
MARGIN_LEGACY_RECONCILED_FIELDS = (
    MARGIN_RECONCILED_FIELDS - {
        "history_watermark", "account_liability_before",
    }
) | {"legacy_exclusivity_proof"}
LEGACY_REPAY_SHAPE = "LEGACY_REPAY_SHAPE"
LEGACY_REPAY_SHAPE_VERSION = 1
# Frozen historical receipt facts measured outside the receipt files.
LEGACY_REPAY_RECEIPTS = (
    (
        "mproofr-2c2a3027d877", "401382497303", "USDT", "2.5000387",
        "4a7e2634361892dccf41137fbd88fe62ae8e3281",
    ),
)
MARGIN_LEGACY_REPAY_RECONCILED_FIELDS = MARGIN_RECONCILED_FIELDS | {
    "receipt_shape", "receipt_shape_version",
}
MARGIN_LEGACY_EXCLUSIVITY_PROOF_FIELDS = {
    "kind", "window_start_utc", "window_end_utc", "row_count",
}
MARGIN_LEGACY_PROOF_FIELDS = {
    "kind", "job_id", "transaction_id", "history_watermark_substitute",
    "account_liability_before_substitute", "legacy_exclusivity_proof",
    "utc",
}
LEGACY_HISTORY_WATERMARK_SUBSTITUTE = \
    "COMPLETE_BOTH_DIRECTION_PAGINATED_HISTORY_EXCLUSIVITY"
LEGACY_ACCOUNT_LIABILITY_BEFORE_SUBSTITUTE = \
    "CURRENT_BORROWED_EQUALS_RECORDED_PRINCIPAL"
# Frozen source-receipt shape written by release b188351.
LEGACY_RECEIPT_FIELDS = (
    "kind", "job_id", "sha", "ready", "mutation_attempts", "verdict",
    "reason", "started_utc", "governor_token", "governor_release",
    "authority_checked_utc", "authority_digests",
    "authority_stop_jobs_present", "mutation_started_utc", "submission",
    "finished_utc",
)
MARGIN_RECONCILE_UNCONFIRMED_FIELDS = {
    "kind", "job_id", "reason", "utc",
}
TICK_LOCK_RECOVERY_FIELDS = {
    "kind", "pid", "age_seconds", "utc",
}
TICK_LOCK_FIELDS = {
    "pid", "process_start", "heartbeat_monotonic_ns", "heartbeat_utc",
    "created_utc", "token",
}
TRADING_OPS = {
    "FUTURES_ROUNDTRIP", "BASIS_ROUNDTRIP", "MOMENTUM_ROUNDTRIP",
    "MOMENTUM_OPEN", "MOMENTUM_EXIT",
}
MARGIN_OPS = margin_ops.MARGIN_OPERATIONS
OPS = TRADING_OPS | MARGIN_OPS | {REVERSE_OP}
ENTRY_OPS = {
    "FUTURES_ROUNDTRIP", "BASIS_ROUNDTRIP", "MOMENTUM_ROUNDTRIP",
    "MOMENTUM_OPEN", margin_ops.MARGIN_BORROW, REVERSE_OP,
}
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
MARGIN_ENDPOINT_PATH = "/sapi/v1/margin/borrow-repay"
MARGIN_ACCOUNT_PATH = "/sapi/v1/margin/account"
# Margin history type records request origin, not BORROW or REPAY direction.
MARGIN_HISTORY_ORIGIN_TYPES = frozenset(("MANUAL", "AUTO"))
# Exchange CONFIRMED maps to the direction-bearing internal receipt status.
MARGIN_HISTORY_CONFIRMED_STATUS = {
    "BORROW": "BORROW_CONFIRMED",
    "REPAY": "REPAY_CONFIRMED",
}
# Keep the immediate history read, then retry it at most three times.
MARGIN_HISTORY_CONFIRMATION_RETRY_LIMIT = 3
# Fixed 1s, 2s, and 4s delays bound the added confirmation wait at 7s.
MARGIN_HISTORY_CONFIRMATION_BACKOFF_SECONDS = (1, 2, 4)
MARGIN_LEGACY_HISTORY_PAGE_SIZE = 100
MARGIN_LEGACY_HISTORY_PAGE_CAP = 20
MARGIN_LEGACY_HISTORY_WINDOW_PAD = timedelta(seconds=60)
MARGIN_DECIMAL_TEXT_MAX = 64
MARGIN_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
# A zero-borrow residual must stay below both this USDT ceiling and the
# relative share below before it can be recorded for interest-only follow-up.
MARGIN_REPAY_DUST_BOUND = Decimal("0.0001")
MARGIN_REPAY_RELATIVE_DUST_RATE = Decimal("0.001")
MARGIN_SNAPSHOT_TIME_VERIFIED = "SNAPSHOT_TIME_VERIFIED"
MARGIN_SNAPSHOT_TIME_UNVERIFIED = "SNAPSHOT_TIME_UNVERIFIED"
MARGIN_IDENTITY_KEY_RE = re.compile(
    r"^(?:(?:tran(?:saction)?)|tx)[_-]?id$"
)
MARGIN_TRANSACTION_ID_RE = re.compile(r"^[1-9][0-9]*$")
REVERSE_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
REVERSE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
REVERSE_QUOTE_CAP = Decimal("8")
REVERSE_ENTRY_LEGS = frozenset((
    reverse_mechanics.BORROW,
    reverse_mechanics.MARGIN_SELL,
    reverse_mechanics.FUTURES_LONG_OPEN,
))
# Identity evidence is ambiguous once a response walk exceeds this node budget.
MARGIN_IDENTITY_MAX_NODES = 4096
# Bound the encoded response material examined by one identity walk to 1 MiB.
MARGIN_IDENTITY_MAX_RESPONSE_BYTES = 1024 * 1024
# Durable Margin release evidence uses the same bounded node walk.  A separate
# depth ceiling keeps the standard JSON decoder below its recursion boundary.
MARGIN_ARTIFACT_MAX_NODES = MARGIN_IDENTITY_MAX_NODES
MARGIN_ARTIFACT_MAX_DEPTH = 64
ACCOUNT_GOVERNOR_LOCK_FILE = "account-governor.lock"
JOB_NAME_RE = re.compile(r"^job-([A-Za-z0-9][A-Za-z0-9_-]{0,63})\.json$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
FREEZE_VERDICTS = {"UNKNOWN", "OPEN_EXPOSURE", "BLOCKED_UNRESOLVED"}
MOMENTUM_STATE_VERSION = 1
FIRST_AUTONOMOUS_CLOSE_MARKER = "first_autonomous_close_passed"
RECEIPT_PATTERNS = (
    "futures-hand-*.json",
    "basis-executor-*.json",
    "roundtrip-*.json",
)


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
    client_tag: str = ""
    borrow_asset: str = ""
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
            operation = margin_ops.build_margin_operation(
                _margin_job_projection(raw), MARGIN_ASSET_WHITELIST, now
            )
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
    if requested > MARGIN_RUNNER_NOTIONAL_CAP:
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
    if raw.get("op") == REVERSE_OP:
        unknown = set(raw) - REVERSE_JOB_FIELDS
        if unknown:
            forbidden = TRADING_JOB_FIELDS | MARGIN_JOB_FIELDS | set(
                margin_ops.FORBIDDEN_JOB_FIELDS
            )
            raise JobRefusal(
                "FORBIDDEN_FIELD" if unknown & forbidden else "UNKNOWN_FIELD"
            )
        if REVERSE_JOB_REQUIRED_FIELDS - set(raw):
            raise JobRefusal("MISSING_FIELD")
        string_fields = (
            "job_id", "op", "symbol", "borrow_asset", "quote_cap",
            "created_utc", "expires_utc", "requested_by", "intent_id",
            "client_tag", "operator_token",
        )
        if any(type(raw[name]) is not str for name in string_fields):
            raise JobRefusal("BAD_TYPE")
        if not JOB_ID_RE.fullmatch(raw["job_id"]) \
                or raw["job_id"] != filename_id:
            raise JobRefusal("BAD_JOB_ID")
        if SYMBOL_RE.fullmatch(raw["symbol"]) is None:
            raise JobRefusal("BAD_SYMBOL")
        if margin_ops.ASSET_RE.fullmatch(raw["borrow_asset"]) is None \
                or raw["symbol"] != raw["borrow_asset"] + "USDT":
            raise JobRefusal("BAD_BORROW_ASSET")
        if not raw["requested_by"].strip() \
                or len(raw["requested_by"]) > 128:
            raise JobRefusal("BAD_REQUESTER")
        if not raw["operator_token"].strip() \
                or len(raw["operator_token"]) \
                > margin_ops.MAX_OPERATOR_TOKEN_LENGTH:
            raise JobRefusal("BAD_OPERATOR_TOKEN")
        if REVERSE_ID_RE.fullmatch(raw["intent_id"]) is None:
            raise JobRefusal("BAD_INTENT_ID")
        if REVERSE_ID_RE.fullmatch(raw["client_tag"]) is None:
            raise JobRefusal("BAD_CLIENT_TAG")
        if len(raw["quote_cap"]) > MARGIN_DECIMAL_TEXT_MAX \
                or REVERSE_DECIMAL_RE.fullmatch(raw["quote_cap"]) is None:
            raise JobRefusal("BAD_QUOTE_CAP")
        try:
            quote_cap = Decimal(raw["quote_cap"])
        except (ArithmeticError, ValueError):
            raise JobRefusal("BAD_QUOTE_CAP") from None
        if not quote_cap.is_finite() or quote_cap <= Decimal("0"):
            raise JobRefusal("BAD_QUOTE_CAP")
        if quote_cap > min(
                REVERSE_QUOTE_CAP, limits.FIRST_TRADE_CAP_USDT,
                limits.PER_LEG_CAP_USDT):
            raise JobRefusal(reverse_mechanics.QUOTE_NOTIONAL_CAP_EXCEEDED)
        created = _timestamp(raw["created_utc"])
        expires = _timestamp(raw["expires_utc"])
        if expires <= created or expires - created > MAX_JOB_AGE:
            raise JobRefusal("BAD_EXPIRY_WINDOW")
        if now >= expires:
            raise JobRefusal("EXPIRED")
        return Job(
            job_id=raw["job_id"], op=REVERSE_OP, symbol=raw["symbol"],
            requested_max_notional=quote_cap, effective_notional=quote_cap,
            created_utc=created, expires_utc=expires,
            requested_by=raw["requested_by"],
            operator_token=raw["operator_token"], intent_id=raw["intent_id"],
            client_tag=raw["client_tag"], borrow_asset=raw["borrow_asset"],
        )

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
    if job.op in {margin_ops.MARGIN_BORROW, REVERSE_OP} \
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
                    or _margin_account_liability({
                        "userAssets": [liability]
                    }, row["asset"]) != liability:
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
        and (margin_ops.INTENT_ID_RE.fullmatch(intent_id) is not None
             or REVERSE_ID_RE.fullmatch(intent_id) is not None) \
        and os.path.isfile(_margin_intent_claim_path(paths, intent_id))


def _claim_margin_intent(paths: Dict[str, str], intent_id: str,
                         job_id: str, now: datetime) -> bool:
    if type(intent_id) is not str \
            or (margin_ops.INTENT_ID_RE.fullmatch(intent_id) is None
                and REVERSE_ID_RE.fullmatch(intent_id) is None):
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
    if op in MARGIN_OPS or op == REVERSE_OP:
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


def _ensure_leverage_1x(symbol: str) -> bool:
    key = os.environ.get("BINANCE_API_KEY", "")
    secret = os.environ.get("BINANCE_API_SECRET", "")
    if not key or not secret:
        return False
    try:
        request = set_leverage_1x.live_request_factory(key, secret)
        if set_leverage_1x.run(symbol, request) != 0:
            return False
        after, _error = set_leverage_1x.read_leverage(request, symbol)
        return after == 1
    except Exception:
        return False


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


def _configure_futures_hand(receipt_dir: str) -> None:
    futures_hand.API_KEY = os.environ.get("BINANCE_API_KEY", "")
    futures_hand.API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
    futures_hand.SYMBOL = os.environ["SYMBOL"]
    futures_hand.MAX_NOTIONAL_RAW = os.environ["MAX_NOTIONAL"]
    futures_hand.MAX_SLIPPAGE_BPS_RAW = os.environ.get(
        "MAX_SLIPPAGE_BPS", "10"
    )
    futures_hand.QUANT_SHA = os.environ["QUANT_SHA"]
    futures_hand.RECEIPT_DIR = receipt_dir
    futures_hand.LOCK_PATH = os.path.join(receipt_dir, "futures-hand.lock")
    futures_hand.receipt = {}
    futures_hand.receipt_path = ""
    futures_hand.ctx = {"lock": False, "mutated": False}


def _momentum_open_plan(effective: Decimal) -> Dict[str, Any]:
    try:
        max_notional, slippage_bps = futures_hand.config()
        if max_notional != effective:
            raise MomentumStateError("hand and runner notional caps disagree")
        plan = futures_hand.preflight(max_notional, slippage_bps, "BUY")
    except futures_hand.CapabilityBlocked as error:
        raise MomentumStateError(
            "momentum entry capability is unreadable: %s" % error
        ) from None
    except futures_hand.PreflightError as error:
        raise JobRefusal("MOMENTUM_PREFLIGHT: %s" % error) from None
    if plan.get("current_leverage") != futures_hand.TARGET_LEVERAGE:
        raise MomentumStateError("momentum entry leverage is not proven 1x")
    return plan


def _one_way_position(rows: Any, symbol: str, label: str) -> Decimal:
    if not isinstance(rows, list) or any(
            not isinstance(row, dict) for row in rows):
        raise MomentumStateError("%s is not a position list" % label)
    matches = [row for row in rows if row.get("symbol") == symbol]
    nonzero = []
    for row in matches:
        if row.get("positionSide", "BOTH") != "BOTH":
            raise MomentumStateError("%s reported hedge-side state" % label)
        amount = _momentum_decimal(
            row.get("positionAmt"), label + " positionAmt",
            allow_zero=True, allow_negative=True,
        )
        if amount < 0:
            raise MomentumStateError("%s reported a short position" % label)
        if amount != 0:
            nonzero.append(amount)
    if len(nonzero) > 1:
        raise MomentumStateError(
            "%s returned more than one nonzero symbol row" % label
        )
    return nonzero[0] if nonzero else Decimal(0)


def _position_mark_price(rows: Any, symbol: str) -> Optional[Decimal]:
    matches = [row for row in rows if row.get("symbol") == symbol]
    nonzero = []
    for row in matches:
        amount = _momentum_decimal(
            row.get("positionAmt"), "position-risk positionAmt",
            allow_zero=True, allow_negative=True,
        )
        if amount != 0:
            nonzero.append(row)
    if len(nonzero) != 1:
        return None
    try:
        return _momentum_decimal(
            nonzero[0].get("markPrice"), "position-risk markPrice"
        )
    except MomentumStateError:
        return None


def _momentum_measure(symbol: str) -> Dict[str, Any]:
    try:
        risks = futures_hand.signed_read(
            "/fapi/v3/positionRisk", {"symbol": symbol},
            "momentum position-risk read",
        )
        account = futures_hand.signed_read(
            "/fapi/v3/account", None, "momentum account-position read"
        )
        orders = futures_hand.signed_read(
            "/fapi/v1/openOrders", {"symbol": symbol},
            "momentum open-orders read",
        )
        _configuration, leverage = futures_hand.read_symbol_configuration()
    except (futures_hand.CapabilityBlocked,
            futures_hand.PreflightError) as error:
        raise MomentumStateError(
            "momentum exchange state is unreadable: %s" % error
        ) from None
    if not isinstance(account, dict):
        raise MomentumStateError("momentum account response is not an object")
    risk_qty = _one_way_position(risks, symbol, "position-risk")
    account_qty = _one_way_position(
        account.get("positions"), symbol, "account"
    )
    if risk_qty != account_qty:
        raise MomentumStateError("independent momentum position reads disagree")
    if not isinstance(orders, list) or any(
            not isinstance(order, dict) for order in orders):
        raise MomentumStateError("momentum open-orders response is invalid")
    if type(leverage) is not int or leverage != futures_hand.TARGET_LEVERAGE:
        raise MomentumStateError("momentum leverage is not proven 1x")
    return {
        "qty": risk_qty,
        "price": _position_mark_price(risks, symbol),
        "open_orders": len(orders),
        "leverage": leverage,
    }


def _momentum_submit(intent: Dict[str, Any]) \
        -> Tuple[str, Any, List[Dict[str, Any]], str]:
    """Persisting is the caller's job; this is the one and only order POST."""
    kind, response = futures_hand.http(
        "POST", "/fapi/v1/order", dict(intent), signed=True
    )
    query_state, order = futures_hand.query_order(
        intent["newClientOrderId"]
    )
    response_fills: List[Dict[str, Any]] = []
    if kind == "ok" and isinstance(response, dict) \
            and isinstance(response.get("fills"), list):
        response_fills = list(response["fills"])
    return query_state, order, response_fills, kind


def _momentum_query(client_id: str) -> Tuple[str, Any]:
    """Recovery is query-only: it can never submit a persisted intent."""
    return futures_hand.query_order(client_id)


def _normalise_momentum_fills(raw_fills: Any, order_id: str,
                              executed: Decimal) -> List[Dict[str, str]]:
    if not isinstance(raw_fills, list) or not raw_fills:
        raise MomentumStateError("exchange returned no momentum fills")
    normalised: List[Dict[str, str]] = []
    covered = Decimal(0)
    seen_ids = set()
    for raw in raw_fills:
        if not isinstance(raw, dict):
            raise MomentumStateError("exchange momentum fill is not an object")
        raw_order_id = raw.get("orderId")
        if raw_order_id is not None and str(raw_order_id) != order_id:
            raise MomentumStateError("momentum fill order id mismatches")
        quantity = _momentum_decimal(raw.get("qty"), "fill qty")
        price = _momentum_decimal(raw.get("price"), "fill price")
        commission = _momentum_decimal(
            raw.get("commission"), "fill commission", allow_zero=True
        )
        realized = _momentum_decimal(
            raw.get("realizedPnl"), "fill realizedPnl",
            allow_zero=True, allow_negative=True,
        )
        asset = raw.get("commissionAsset")
        if type(asset) is not str or not asset.strip():
            raise MomentumStateError("fill commission asset is missing")
        trade_id = str(raw.get("id", raw.get("tradeId", "")))
        if trade_id:
            if trade_id in seen_ids:
                raise MomentumStateError("duplicate momentum trade id")
            seen_ids.add(trade_id)
        normalised.append({
            "trade_id": trade_id,
            "order_id": order_id,
            "price": _decimal_text(price),
            "qty": _decimal_text(quantity),
            "realized_pnl": _decimal_text(realized),
            "commission": _decimal_text(commission),
            "commission_asset": asset,
        })
        covered += quantity
    if covered != executed:
        raise MomentumStateError(
            "momentum fills do not cover the exchange order quantity"
        )
    return normalised


def _momentum_fills(order: Dict[str, Any],
                    response_fills: List[Dict[str, Any]]) \
        -> List[Dict[str, str]]:
    order_id = str(order.get("orderId", ""))
    executed = _momentum_decimal(order.get("executedQty"), "executedQty")
    if not order_id:
        raise MomentumStateError("exchange order id is missing")
    try:
        return _normalise_momentum_fills(
            response_fills, order_id, executed
        )
    except MomentumStateError:
        pass
    try:
        payload = futures_hand.signed_read(
            "/fapi/v1/userTrades",
            {"symbol": futures_hand.SYMBOL, "orderId": order_id},
            "momentum fill/commission read",
        )
    except (futures_hand.CapabilityBlocked,
            futures_hand.PreflightError) as error:
        raise MomentumStateError(
            "momentum fill/commission data is unreadable: %s" % error
        ) from None
    return _normalise_momentum_fills(payload, order_id, executed)


def _momentum_order_facts(order: Any, client_id: str,
                          reduce_only: bool) -> Dict[str, Any]:
    if not isinstance(order, dict) \
            or order.get("status") not in futures_hand.TERMINAL:
        raise MomentumStateError("momentum order is not terminal")
    if order.get("clientOrderId") != client_id:
        raise MomentumStateError("momentum order client id mismatches")
    if order.get("symbol", futures_hand.SYMBOL) != futures_hand.SYMBOL:
        raise MomentumStateError("momentum order symbol mismatches")
    expected_side = "SELL" if reduce_only else "BUY"
    if order.get("side", expected_side) != expected_side:
        raise MomentumStateError("momentum order side mismatches")
    if order.get("positionSide", "BOTH") != "BOTH":
        raise MomentumStateError("momentum order reported hedge-side state")
    order_id = str(order.get("orderId", ""))
    if not order_id:
        raise MomentumStateError("momentum exchange order id is missing")
    quantity = _momentum_decimal(
        order.get("executedQty"), "order executedQty", allow_zero=True
    )
    quote = _momentum_decimal(
        order.get("cumQuote"), "order cumQuote", allow_zero=True
    )
    reported_reduce = order.get("reduceOnly") is True or str(
        order.get("reduceOnly", "")
    ).lower() == "true"
    if reported_reduce != reduce_only:
        raise MomentumStateError(
            "momentum order reduceOnly truth mismatches its intent"
        )
    price = Decimal(0)
    if quantity > 0:
        price = _momentum_decimal(order.get("avgPrice"), "order avgPrice")
        if quote <= 0:
            raise MomentumStateError("filled momentum order has no quote")
    return {
        "status": str(order["status"]),
        "order_id": order_id,
        "client_id": client_id,
        "qty": quantity,
        "quote": quote,
        "price": price,
        "reduce_only": reported_reduce,
    }


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


def _spot_request_factory() -> Callable[..., Tuple[str, Any]]:
    return basis_executor.live_request_factory(
        os.environ.get("BINANCE_API_KEY", ""),
        os.environ.get("BINANCE_API_SECRET", ""),
    )


def _spot_state(symbol: str, sha: str, receipt_dir: str, run_id: str,
                request: Optional[Callable[..., Tuple[str, Any]]] = None) \
        -> basis_executor.RunState:
    config = basis_executor.ExecutorConfig(
        symbol=symbol,
        sha=sha,
        receipt_dir=receipt_dir,
        run_id=run_id,
    )
    state = basis_executor.make_state(
        config,
        request or _spot_request_factory(),
        basis_executor.time.sleep,
        receipted=False,
    )
    state.receipt = {"legs": []}
    return state


def _spot_balances(account: Any, *assets: str) -> Dict[str, Decimal]:
    if not isinstance(account, dict):
        raise MomentumStateError("Spot account balances are unreadable")
    rows = account.get("balances")
    if not isinstance(rows, list):
        rows = account.get("userAssets")
    if not isinstance(rows, list) \
            or any(not isinstance(row, dict) for row in rows):
        raise MomentumStateError("Spot account balances are unreadable")
    balances: Dict[str, Decimal] = {}
    for asset in assets:
        matches = [row for row in rows if row.get("asset") == asset]
        if len(matches) != 1 or matches[0].get("free") is None:
            raise MomentumStateError(
                "Spot account has no unique free balance for %s" % asset
            )
        balances[asset] = _momentum_decimal(
            matches[0]["free"], "Spot %s free balance" % asset,
            allow_zero=True,
        )
    if any(not value.is_finite() or value < 0 for value in balances.values()):
        raise MomentumStateError("Spot account balance is outside its range")
    return balances


def _spot_market_context(state: basis_executor.RunState,
                         effective: Decimal,
                         margin_required: bool = False) -> Dict[str, Any]:
    """Proven spot-hand preflight, limited to a quote-bounded BUY."""
    try:
        raw_info = basis_executor.public_read(
            state, basis_executor.SPOT.base_url,
            basis_executor.SPOT.exchange_info_path, "Spot exchangeInfo",
        )
        info = basis_executor.one_symbol(
            raw_info, state.cfg.symbol, "Spot exchangeInfo"
        )
        if info.get("status") != "TRADING" \
                or info.get("isSpotTradingAllowed") is not True:
            raise basis_executor.Refusal(
                "symbol is not live for Spot trading"
            )
        if margin_required and info.get("isMarginTradingAllowed") is not True:
            raise basis_executor.Refusal(
                "symbol is not live for Margin trading"
            )
        if "MARKET" not in info.get("orderTypes", []) \
                or info.get("quoteOrderQtyMarketAllowed") is not True:
            raise basis_executor.Refusal(
                "MARKET quoteOrderQty orders not enabled for symbol"
            )
        if info.get("quoteAsset") != "USDT" \
                or type(info.get("baseAsset")) is not str \
                or not info["baseAsset"]:
            raise basis_executor.Refusal(
                "momentum Spot entry requires a USDT quote asset"
            )
        filters = {
            row["filterType"]: row for row in info.get("filters", [])
        }
        lot = filters["LOT_SIZE"]
        market_lot = filters.get("MARKET_LOT_SIZE", {})
        lot_step = _momentum_decimal(lot.get("stepSize"), "spot lot step")
        market_step = _momentum_decimal(
            market_lot.get("stepSize", "0"), "spot market step",
            allow_zero=True,
        )
        step = lot_step if market_step == 0 else basis_executor.common_step(
            lot_step, market_step
        )
        min_qty = max(
            _momentum_decimal(
                lot.get("minQty"), "spot minimum quantity", allow_zero=True
            ),
            _momentum_decimal(
                market_lot.get("minQty", "0"),
                "spot market minimum quantity", allow_zero=True,
            ),
        )
        notional_filter = filters.get("NOTIONAL") \
            or filters.get("MIN_NOTIONAL") or {}
        if not isinstance(notional_filter, dict) \
                or notional_filter.get("minNotional") in (None, ""):
            raise basis_executor.Refusal(
                "Spot minimum-notional filter is unavailable"
            )
        min_notional = _momentum_decimal(
            notional_filter.get("minNotional", "0"),
            "spot minimum notional", allow_zero=True,
        )
        apply_min_raw = notional_filter.get(
            "applyMinToMarket",
            notional_filter.get("applyToMarket", True),
        )
        if type(apply_min_raw) is not bool:
            raise basis_executor.Refusal(
                "Spot minimum-notional market rule is invalid"
            )
        apply_min = apply_min_raw
        base_precision = info.get("baseAssetPrecision")
        quote_precision = info.get(
            "quoteAssetPrecision", info.get("quotePrecision")
        )
        if isinstance(base_precision, bool) \
                or not isinstance(base_precision, int) \
                or not 0 <= base_precision <= 30 \
                or isinstance(quote_precision, bool) \
                or not isinstance(quote_precision, int) \
                or not 0 <= quote_precision <= 30:
            raise basis_executor.Refusal(
                "Spot asset precision is unavailable or invalid"
            )
        quote_quantum = Decimal(1).scaleb(-quote_precision)
        quote_spend = spot_roundtrip.floor_step(effective, quote_quantum)
        if quote_spend <= 0 \
                or (apply_min and quote_spend < min_notional):
            raise basis_executor.Refusal(
                "effective quote cap is below the Spot minimum"
            )

        account = basis_executor.signed_read(
            state, basis_executor.SPOT.base_url,
            basis_executor.SPOT_ACCOUNT_PATH, None, "Spot account read",
        )
        can_trade = account.get("canTrade") \
            if isinstance(account, dict) else None
        if can_trade is not True:
            raise basis_executor.PreflightRefusal(
                "Spot account canTrade is not true"
            )
        balances = _spot_balances(
            account, info["baseAsset"], info["quoteAsset"]
        )
        if balances[info["quoteAsset"]] < quote_spend:
            raise basis_executor.PreflightRefusal(
                "Spot quote balance is below the effective cap"
            )
        orders = basis_executor.signed_read(
            state, basis_executor.SPOT.base_url,
            basis_executor.SPOT_OPEN_ORDERS_PATH,
            {"symbol": state.cfg.symbol}, "Spot open-orders read",
        )
        if not isinstance(orders, list):
            raise basis_executor.PreflightRefusal(
                "Spot open-orders response is not a list"
            )
        if orders:
            raise basis_executor.PreflightRefusal(
                "Spot open order already stands on the momentum symbol"
            )
        book = basis_executor.public_read(
            state, basis_executor.SPOT.base_url,
            basis_executor.SPOT_BOOK_PATH, "Spot book",
        )
        if not isinstance(book, dict) \
                or book.get("symbol", state.cfg.symbol) != state.cfg.symbol:
            raise basis_executor.Refusal("Spot book is invalid")
        bid = _momentum_decimal(book.get("bidPrice"), "spot bid")
        ask = _momentum_decimal(book.get("askPrice"), "spot ask")
        if bid > ask:
            raise basis_executor.Refusal("Spot book is crossed")
        estimated_net = basis_cycle.quantize_down(
            quote_spend / ask * (Decimal(1) - basis_cycle.FEE_BUFFER),
            step,
        )
        if estimated_net < min_qty \
                or (apply_min and estimated_net * bid < min_notional):
            raise basis_executor.Refusal(
                "effective cap cannot acquire a sellable Spot quantity"
            )
    except basis_executor.PreflightRefusal as error:
        raise JobRefusal("MOMENTUM_SPOT_PREFLIGHT: %s" % error) from None
    except (basis_executor.Refusal, MomentumStateError, ArithmeticError,
            KeyError, TypeError, ValueError) as error:
        raise JobRefusal("MOMENTUM_SPOT_PREFLIGHT: %s" % error) from None
    return {
        "base": info["baseAsset"],
        "quote": info["quoteAsset"],
        "step": step,
        "min_qty": min_qty,
        "min_notional": min_notional,
        "apply_min": apply_min,
        "quote_spend": quote_spend,
        "starting_base_free": balances[info["baseAsset"]],
        "starting_quote_free": balances[info["quoteAsset"]],
        "base_tolerance": Decimal(1).scaleb(-base_precision),
        "bid": bid,
        "ask": ask,
    }


def _spot_plan(symbol: str, quote_spend: Decimal) -> basis_cycle.CyclePlan:
    return basis_cycle.CyclePlan(
        symbol=symbol,
        entry_gap_bps=Decimal(0),
        exit_gap_bps=Decimal(0),
        funding_rate=Decimal(0),
        spot_quantity=Decimal(0),
        short_quantity=Decimal(0),
        spot_quote_cap=quote_spend,
        short_notional=Decimal(0),
        leverage=1,
        reason="spot-only momentum leg",
    )


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
            raw.get("base_tolerance"), "persisted base tolerance"
        ),
    }


def _spot_measure(position: Dict[str, Any], receipt_dir: str,
                  request: Optional[Callable[..., Tuple[str, Any]]] = None) \
        -> Dict[str, Any]:
    market = _spot_persisted_market(position)
    sha = str(position.get("sha", ""))
    state = _spot_state(
        position["symbol"], sha, receipt_dir,
        str(position.get("run_id", "spot-measure")), request,
    )
    try:
        account = basis_executor.signed_read(
            state, basis_executor.SPOT.base_url,
            basis_executor.SPOT_ACCOUNT_PATH, None, "Spot account read",
        )
        balances = _spot_balances(
            account, market["base"], market["quote"]
        )
        orders = basis_executor.signed_read(
            state, basis_executor.SPOT.base_url,
            basis_executor.SPOT_OPEN_ORDERS_PATH,
            {"symbol": position["symbol"]}, "Spot open-orders read",
        )
        book = basis_executor.public_read(
            state, basis_executor.SPOT.base_url,
            basis_executor.SPOT_BOOK_PATH, "Spot book",
        )
        if not isinstance(orders, list) or not isinstance(book, dict) \
                or book.get("symbol", position["symbol"]) \
                != position["symbol"]:
            raise MomentumStateError("Spot measurement response is invalid")
        price = _momentum_decimal(book.get("bidPrice"), "current Spot bid")
        quantity = balances[market["base"]] - market["starting_base_free"]
        if quantity < -market["base_tolerance"]:
            raise MomentumStateError(
                "Spot balance fell below the pre-entry baseline"
            )
        if quantity.copy_abs() <= market["base_tolerance"]:
            quantity = Decimal(0)
    except (basis_executor.PreflightRefusal, basis_executor.Refusal,
            MomentumStateError, ArithmeticError, KeyError, TypeError,
            ValueError) as error:
        if isinstance(error, MomentumStateError):
            raise
        raise MomentumStateError(
            "momentum Spot state is unreadable: %s" % error
        ) from None
    can_trade = account.get("canTrade") \
        if isinstance(account, dict) else None
    return {
        "qty": quantity,
        "price": price,
        "open_orders": len(orders),
        "can_trade": can_trade is True,
        "base_free": balances[market["base"]],
    }


def _spot_order_facts(order: Any, client_id: str,
                      side: str) -> Dict[str, Any]:
    if not isinstance(order, dict) \
            or order.get("status") not in basis_executor.TERMINAL:
        raise MomentumStateError("Spot momentum order is not terminal")
    if order.get("clientOrderId") != client_id:
        raise MomentumStateError("Spot momentum order client id mismatches")
    if order.get("symbol", os.environ.get("SYMBOL")) \
            != os.environ.get("SYMBOL"):
        raise MomentumStateError("Spot momentum order symbol mismatches")
    if order.get("side", side) != side:
        raise MomentumStateError("Spot momentum order side mismatches")
    order_id = str(order.get("orderId", ""))
    if not order_id:
        raise MomentumStateError("Spot momentum exchange order id is missing")
    quantity = _momentum_decimal(
        order.get("executedQty"), "Spot order executedQty", allow_zero=True
    )
    quote = _momentum_decimal(
        order.get(basis_executor.SPOT.quote_field),
        "Spot order cumulative quote", allow_zero=True,
    )
    price = Decimal(0)
    if quantity > 0:
        if quote <= 0:
            raise MomentumStateError("filled Spot momentum order has no quote")
        price = quote / quantity
    return {
        "status": str(order["status"]),
        "order_id": order_id,
        "client_id": client_id,
        "qty": quantity,
        "quote": quote,
        "price": price,
    }


def _normalise_spot_fills(raw_fills: Any, order_id: str,
                          executed: Decimal) -> List[Dict[str, str]]:
    if not isinstance(raw_fills, list) or not raw_fills:
        raise MomentumStateError("exchange returned no Spot momentum fills")
    normalised: List[Dict[str, str]] = []
    covered = Decimal(0)
    seen_ids = set()
    for raw in raw_fills:
        if not isinstance(raw, dict):
            raise MomentumStateError("exchange Spot fill is not an object")
        raw_order_id = raw.get("orderId")
        if raw_order_id is not None and str(raw_order_id) != order_id:
            raise MomentumStateError("Spot fill order id mismatches")
        quantity = _momentum_decimal(raw.get("qty"), "Spot fill qty")
        price = _momentum_decimal(raw.get("price"), "Spot fill price")
        commission = _momentum_decimal(
            raw.get("commission"), "Spot fill commission", allow_zero=True
        )
        asset = raw.get("commissionAsset")
        if type(asset) is not str or not asset.strip():
            raise MomentumStateError("Spot fill commission asset is missing")
        trade_id = str(raw.get("id", raw.get("tradeId", "")))
        if trade_id:
            if trade_id in seen_ids:
                raise MomentumStateError("duplicate Spot momentum trade id")
            seen_ids.add(trade_id)
        normalised.append({
            "trade_id": trade_id,
            "order_id": order_id,
            "price": _decimal_text(price),
            "qty": _decimal_text(quantity),
            "realized_pnl": "0",
            "commission": _decimal_text(commission),
            "commission_asset": asset,
        })
        covered += quantity
    if covered != executed:
        raise MomentumStateError(
            "Spot fills do not cover the exchange order quantity"
        )
    return normalised


def _spot_fills(state: basis_executor.RunState, order: Dict[str, Any],
                response_fills: List[Dict[str, Any]]) \
        -> List[Dict[str, str]]:
    order_id = str(order.get("orderId", ""))
    executed = _momentum_decimal(
        order.get("executedQty"), "Spot order executedQty"
    )
    try:
        trades, covered = basis_executor.spot_trades(
            state, order_id, executed, response_fills
        )
    except Exception as error:
        raise MomentumStateError(
            "Spot fill/commission data is unreadable: %s" %
            type(error).__name__
        ) from None
    if covered != executed:
        raise MomentumStateError(
            "Spot fill/commission data does not cover the order"
        )
    return _normalise_spot_fills(trades, order_id, executed)


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


def _run_futures(receipt_dir: str,
                 entry: Optional[Callable[[], Any]] = None,
                 short: bool = True) -> Outcome:
    entry = entry or futures_hand.main
    before = set(glob.glob(os.path.join(receipt_dir, "futures-hand-*.json")))
    futures_hand.API_KEY = os.environ.get("BINANCE_API_KEY", "")
    futures_hand.API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
    futures_hand.SYMBOL = os.environ["SYMBOL"]
    futures_hand.MAX_NOTIONAL_RAW = os.environ["MAX_NOTIONAL"]
    futures_hand.MAX_SLIPPAGE_BPS_RAW = os.environ.get("MAX_SLIPPAGE_BPS", "10")
    futures_hand.QUANT_SHA = os.environ["QUANT_SHA"]
    futures_hand.RECEIPT_DIR = receipt_dir
    futures_hand.LOCK_PATH = os.path.join(receipt_dir, "futures-hand.lock")
    futures_hand.receipt = {}
    futures_hand.receipt_path = ""
    futures_hand.ctx = {"lock": False, "mutated": False}
    previous_argv = sys.argv
    sys.argv = ["futures_hand.py"]
    if short:
        sys.argv.append("--short")
    sys.argv.append("--execute")
    returned = None
    code = 0
    try:
        returned = entry()
        if type(returned) is int:
            code = returned
    except SystemExit as stopped:
        code = stopped.code if type(stopped.code) is int else 1
    except Exception:
        return Outcome("UNKNOWN", 4, "", "EXECUTOR_EXCEPTION")
    finally:
        sys.argv = previous_argv
    if isinstance(returned, dict):
        verdict = returned.get("verdict", "")
        receipt_path = returned.get("receipt_path", "")
        result_code = returned.get("exit_code", code)
        if type(verdict) is str and type(receipt_path) is str \
                and type(result_code) is int:
            return Outcome(verdict, result_code, receipt_path)
    receipt_path = _new_receipt(
        receipt_dir, "futures-hand-", before, futures_hand.receipt_path
    )
    verdict = _verdict_from_receipt(receipt_path)
    if not verdict:
        return Outcome("UNKNOWN", 4, receipt_path, "MISSING_EXECUTOR_RECEIPT")
    return Outcome(verdict, code, receipt_path)


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
                           query_state: str, order: Any,
                           response_fills: List[Dict[str, Any]],
                           submission_kind: str, now: datetime,
                           recovering: bool = False) -> Outcome:
    if query_state == "not_found" \
            and (recovering or submission_kind == "api_error"):
        try:
            measured = _momentum_measure(position["symbol"])
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
    if query_state != "terminal":
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_OPEN_%s" % query_state.upper(), now,
        )
    client_id = position["pending_intent"]["client_id"]
    try:
        facts = _momentum_order_facts(order, client_id, reduce_only=False)
    except MomentumStateError as error:
        return _momentum_unknown(
            paths, position, "MOMENTUM_OPEN_ORDER_UNKNOWN: %s" % error, now
        )
    if facts["qty"] == 0:
        try:
            measured = _momentum_measure(position["symbol"])
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
        fills = _momentum_fills(order, response_fills)
        measured = _momentum_measure(position["symbol"])
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
                           query_state: str, order: Any,
                           response_fills: List[Dict[str, Any]],
                           submission_kind: str, now: datetime,
                           recovering: bool = False) -> Outcome:
    pending = position["pending_intent"]
    outcome_reason = pending.get("reason", "")
    if type(outcome_reason) is not str:
        outcome_reason = ""
    measured_before = _momentum_decimal(
        pending.get("measured_qty"), "persisted exit measurement"
    )
    if query_state == "not_found" \
            and (recovering or submission_kind == "api_error"):
        try:
            measured = _momentum_measure(position["symbol"])
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
    if query_state != "terminal":
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_EXIT_%s" % query_state.upper(), now,
        )
    client_id = pending["client_id"]
    try:
        facts = _momentum_order_facts(order, client_id, reduce_only=True)
    except MomentumStateError as error:
        return _momentum_unknown(
            paths, position, "MOMENTUM_EXIT_ORDER_UNKNOWN: %s" % error, now
        )
    if facts["qty"] == 0:
        try:
            measured = _momentum_measure(position["symbol"])
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
        fills = _momentum_fills(order, response_fills)
        measured_after = _momentum_measure(position["symbol"])
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


def _spot_base_fee(fills: List[Dict[str, str]], base: str) -> Decimal:
    return sum((
        Decimal(fill["commission"])
        for fill in fills if fill["commission_asset"] == base
    ), Decimal(0))


def _resolve_spot_momentum_open(
        paths: Dict[str, str], receipt_dir: str,
        position: Dict[str, Any], state: basis_executor.RunState,
        query_state: str, order: Any,
        response_fills: List[Dict[str, Any]], now: datetime,
        request: Optional[Callable[..., Tuple[str, Any]]] = None,
        recovering: bool = False) -> Outcome:
    if query_state in {"rejected", "not_found"}:
        try:
            measured = _spot_measure(position, receipt_dir, request)
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
        facts = _spot_order_facts(order, client_id, "BUY")
    except MomentumStateError as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_OPEN_ORDER_UNKNOWN: %s" % error, now,
        )
    if facts["qty"] == 0:
        try:
            measured = _spot_measure(position, receipt_dir, request)
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
        fills = _spot_fills(state, order, response_fills)
        market = _spot_persisted_market(position)
        base_fee = _spot_base_fee(fills, market["base"])
        net_base = facts["qty"] - base_fee
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
        measured = _spot_measure(position, receipt_dir, request)
    except MomentumStateError as error:
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
        request: Optional[Callable[..., Tuple[str, Any]]] = None) -> Outcome:
    run_id = "mom" + secrets.token_hex(8)
    state = _spot_state(
        job.symbol, os.environ["QUANT_SHA"], receipt_dir, run_id, request
    )
    try:
        market = _spot_market_context(state, job.effective_notional)
    except JobRefusal as error:
        return Outcome(
            "REFUSED", 2, "", error.reason, _empty_momentum_ledger()
        )
    plan = _spot_plan(job.symbol, market["quote_spend"])
    client_id = basis_cycle.client_id(basis_cycle.ENTER_SPOT, run_id)
    intent = basis_cycle.leg_intent(
        basis_cycle.ENTER_SPOT, plan, run_id
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
        "planned_qty": _decimal_text(
            basis_cycle.quantize_down(
                market["quote_spend"] / market["ask"], market["step"]
            )
        ),
        "spot_market": _spot_market_payload(market),
        "pending_intent": {
            "action": "OPEN",
            "client_id": client_id,
            "intent": dict(intent),
            "persisted_utc": _utc_text(now),
            "governor_token": governor_token,
        },
        "entry": None,
        "exit": None,
        "created_utc": _utc_text(now),
    }
    _save_momentum_position(paths, position, now)
    try:
        query_state, order, fills = basis_executor.settle_leg(
            state, basis_cycle.ENTER_SPOT, plan
        )
    except Exception as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_OPEN_EXECUTOR_EXCEPTION: %s" %
            type(error).__name__, now,
        )
    return _resolve_spot_momentum_open(
        paths, receipt_dir, position, state, query_state, order, fills, now,
        request,
    )


def _resolve_spot_momentum_exit(
        paths: Dict[str, str], receipt_dir: str,
        position: Dict[str, Any], state: basis_executor.RunState,
        query_state: str, order: Any,
        response_fills: List[Dict[str, Any]], now: datetime,
        request: Optional[Callable[..., Tuple[str, Any]]] = None) -> Outcome:
    pending = position["pending_intent"]
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
            measured = _spot_measure(position, receipt_dir, request)
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
        facts = _spot_order_facts(order, pending["client_id"], "SELL")
    except MomentumStateError as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_EXIT_ORDER_UNKNOWN: %s" % error, now,
        )
    if facts["qty"] == 0:
        try:
            measured = _spot_measure(position, receipt_dir, request)
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
        fills = _spot_fills(state, order, response_fills)
        sell_base_fee = _spot_base_fee(fills, market["base"])
        residual = measured_before - facts["qty"] - sell_base_fee
        if residual < -market["base_tolerance"]:
            raise MomentumStateError(
                "Spot SELL consumed pre-existing base holdings"
            )
        measured_after = _spot_measure(position, receipt_dir, request)
    except MomentumStateError as error:
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
        request: Optional[Callable[..., Tuple[str, Any]]] = None) -> Outcome:
    market = _spot_persisted_market(position)
    try:
        measured = measurement or _spot_measure(
            position, receipt_dir, request
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
    state = _spot_state(
        job.symbol, os.environ["QUANT_SHA"], receipt_dir, run_id, request
    )
    plan = _spot_plan(job.symbol, Decimal(0))
    client_id = basis_cycle.client_id(basis_cycle.EXIT_SPOT, run_id)
    intent = basis_cycle.leg_intent(
        basis_cycle.EXIT_SPOT, plan, run_id, sell_quantity
    )
    position["state"] = "CLOSING"
    position["job_id"] = job.job_id
    position["run_id"] = run_id
    position["pending_intent"] = {
        "action": "EXIT",
        "client_id": client_id,
        "measured_qty": _decimal_text(measured["qty"]),
        "sell_quantity": _decimal_text(sell_quantity),
        "intent": dict(intent),
        "persisted_utc": _utc_text(now),
    }
    if outcome_reason:
        position["pending_intent"]["reason"] = outcome_reason
    _save_momentum_position(paths, position, now)
    try:
        query_state, order, fills = basis_executor.settle_leg(
            state, basis_cycle.EXIT_SPOT, plan, sell_quantity
        )
    except Exception as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_SPOT_EXIT_EXECUTOR_EXCEPTION: %s" %
            type(error).__name__, now,
        )
    return _resolve_spot_momentum_exit(
        paths, receipt_dir, position, state, query_state, order, fills, now,
        request,
    )


def _run_momentum_open(job: Job, receipt_dir: str,
                       paths: Dict[str, str], now: datetime,
                       governor_token: str = "",
                       spot_request: Optional[
                           Callable[..., Tuple[str, Any]]
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
            spot_request,
        )
    if not _ensure_leverage_1x(job.symbol):
        return Outcome("UNKNOWN", 4, "", "LEVERAGE_UNPROVEN",
                       _empty_momentum_ledger())
    _configure_futures_hand(receipt_dir)
    try:
        plan = _momentum_open_plan(job.effective_notional)
    except JobRefusal as error:
        return Outcome("REFUSED", 2, "", error.reason,
                       _empty_momentum_ledger())
    except MomentumStateError as error:
        return Outcome("UNKNOWN", 4, "", str(error),
                       _empty_momentum_ledger())
    client_id = "l2fm-" + secrets.token_hex(10) + "-o"
    intent = futures_hand.open_intent("BUY", plan, client_id)
    position: Dict[str, Any] = {
        "version": MOMENTUM_STATE_VERSION,
        "revision": prior["revision"] if prior is not None else 0,
        "state": "OPENING",
        "venue": "futures",
        "symbol": job.symbol,
        "job_id": job.job_id,
        "sha": os.environ["QUANT_SHA"],
        "effective_notional": _decimal_text(job.effective_notional),
        "planned_qty": _decimal_text(plan["quantity"]),
        "pending_intent": {
            "action": "OPEN",
            "client_id": client_id,
            "intent": dict(intent),
            "persisted_utc": _utc_text(now),
            "governor_token": governor_token,
        },
        "entry": None,
        "exit": None,
        "created_utc": _utc_text(now),
    }
    _save_momentum_position(paths, position, now)
    try:
        query_state, order, fills, kind = _momentum_submit(intent)
    except Exception as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_OPEN_EXECUTOR_EXCEPTION: %s" %
            type(error).__name__, now,
        )
    return _resolve_momentum_open(
        paths, position, query_state, order, fills, kind, now
    )


def _run_momentum_exit(job: Job, receipt_dir: str,
                       paths: Dict[str, str], now: datetime,
                       measurement: Optional[Dict[str, Any]] = None,
                       outcome_reason: str = "",
                       spot_request: Optional[
                           Callable[..., Tuple[str, Any]]
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
            measurement, outcome_reason, spot_request,
        )
    if measurement is None:
        if not _ensure_leverage_1x(job.symbol):
            return _momentum_unknown(
                paths, position, "LEVERAGE_UNPROVEN", now
            )
        _configure_futures_hand(receipt_dir)
        try:
            measured = _momentum_measure(job.symbol)
        except MomentumStateError as error:
            return _momentum_unknown(paths, position, str(error), now)
    else:
        measured = measurement
    if measured["open_orders"] != 0 or measured["qty"] != persisted_qty:
        return _momentum_unknown(
            paths, position, "MOMENTUM_POSITION_MEASUREMENT_MISMATCH", now
        )
    client_id = "l2fm-" + secrets.token_hex(10) + "-c"
    intent = futures_hand.close_intent("SELL", measured["qty"], client_id)
    position["state"] = "CLOSING"
    position["job_id"] = job.job_id
    position["pending_intent"] = {
        "action": "EXIT",
        "client_id": client_id,
        "measured_qty": _decimal_text(measured["qty"]),
        "intent": dict(intent),
        "persisted_utc": _utc_text(now),
    }
    if outcome_reason:
        position["pending_intent"]["reason"] = outcome_reason
    _save_momentum_position(paths, position, now)
    try:
        query_state, order, fills, kind = _momentum_submit(intent)
    except Exception as error:
        return _momentum_unknown(
            paths, position,
            "MOMENTUM_EXIT_EXECUTOR_EXCEPTION: %s" %
            type(error).__name__, now,
        )
    return _resolve_momentum_exit(
        paths, position, query_state, order, fills, kind, now
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


def _recover_pending_momentum(paths: Dict[str, str], receipt_dir: str,
                              result_ids: set, now: datetime,
                              spot_request: Optional[
                                  Callable[..., Tuple[str, Any]]
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
            state = _spot_state(
                position["symbol"], sha, receipt_dir,
                str(position.get("run_id", "spot-recovery")), spot_request,
            )
            try:
                query_state, order = basis_executor.query_client_order(
                    state, basis_executor.SPOT, pending["client_id"]
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
                        paths, receipt_dir, position, state, query_state,
                        order, [], now, spot_request, recovering=True,
                    )
                else:
                    outcome = _resolve_spot_momentum_exit(
                        paths, receipt_dir, position, state, query_state,
                        order, [], now, spot_request,
                    )
        else:
            _configure_futures_hand(receipt_dir)
            try:
                query_state, order = _momentum_query(pending["client_id"])
            except Exception as error:
                outcome = _momentum_unknown(
                    paths, position,
                    "MOMENTUM_RECOVERY_EXCEPTION: %s" %
                    type(error).__name__, now,
                )
            else:
                if action == "OPEN":
                    outcome = _resolve_momentum_open(
                        paths, position, query_state, order, [], "recovery",
                        now, recovering=True,
                    )
                else:
                    outcome = _resolve_momentum_exit(
                        paths, position, query_state, order, [], "recovery",
                        now, recovering=True,
                    )
    return job_id, op, position["symbol"], outcome, governor_token


def _persist_margin_source(path: str, payload: Dict[str, Any],
                           create: bool = False) -> None:
    _persist_json_receipt(path, payload, create=create)


def _margin_identity_scalar_size(value: Any, maximum: int) -> Optional[int]:
    if maximum < 0:
        return maximum + 1
    if type(value) is str:
        size = 2
        for character in value:
            codepoint = ord(character)
            if character in {'"', "\\"} \
                    or character in {"\b", "\t", "\n", "\f", "\r"}:
                size += 2
            elif codepoint < 0x20 or codepoint > 0x7f:
                size += 6 if codepoint <= 0xffff else 12
            else:
                size += 1
            if size > maximum:
                return maximum + 1
        return size
    if value is None:
        return 4
    if type(value) is bool:
        return 4 if value else 5
    if type(value) is int:
        if value.bit_length() * 30103 // 100000 + 2 > maximum:
            return maximum + 1
        try:
            return len(str(value))
        except ValueError:
            return None
    if type(value) is not float:
        return None
    try:
        encoded = json.dumps(
            value, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return len(encoded)


def _margin_identity_aliases(payload: Any) -> Optional[List[str]]:
    if not isinstance(payload, dict):
        return None
    values: List[str] = []
    stack = [(payload, True)]
    visited = set()
    scheduled_nodes = 1
    visited_nodes = 0
    response_bytes = 0
    while stack:
        current, top_level = stack.pop()
        visited_nodes += 1
        if visited_nodes > MARGIN_IDENTITY_MAX_NODES:
            return None
        if isinstance(current, dict):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            response_bytes += 2 + max(0, len(current) - 1)
            children = []
            for name, value in current.items():
                if type(name) is not str:
                    return None
                remaining = MARGIN_IDENTITY_MAX_RESPONSE_BYTES \
                    - response_bytes
                name_size = _margin_identity_scalar_size(name, remaining)
                if name_size is None or name_size > remaining:
                    return None
                response_bytes += name_size + 1
                if response_bytes > MARGIN_IDENTITY_MAX_RESPONSE_BYTES:
                    return None
                normalized = unicodedata.normalize("NFKC", name).casefold()
                if MARGIN_IDENTITY_KEY_RE.fullmatch(normalized) is not None:
                    if top_level and name not in {"tranId", "txId"}:
                        return None
                    if type(value) not in (int, str):
                        return None
                    if type(value) is int and value.bit_length() > 430:
                        return None
                    try:
                        text = str(value)
                    except ValueError:
                        return None
                    if not text or len(text) > 128:
                        return None
                    if top_level:
                        values.append(text)
                        if len(set(values)) > 1:
                            return None
                    else:
                        # Providers may repeat the confirmed top-level id in
                        # nested evidence; exact canonical equality adds no
                        # competing ownership claim.
                        if not values or text != values[0]:
                            return None
                children.append((value, False))
            if scheduled_nodes + len(children) > MARGIN_IDENTITY_MAX_NODES:
                return None
            scheduled_nodes += len(children)
            stack.extend(reversed(children))
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            response_bytes += 2 + max(0, len(current) - 1)
            if scheduled_nodes + len(current) > MARGIN_IDENTITY_MAX_NODES:
                return None
            scheduled_nodes += len(current)
            stack.extend((value, False) for value in reversed(current))
        else:
            remaining = MARGIN_IDENTITY_MAX_RESPONSE_BYTES - response_bytes
            scalar_size = _margin_identity_scalar_size(current, remaining)
            if scalar_size is None or scalar_size > remaining:
                return None
            response_bytes += scalar_size
        if response_bytes > MARGIN_IDENTITY_MAX_RESPONSE_BYTES:
            return None
    return values


def _margin_transaction_id(payload: Any) -> str:
    aliases = _margin_identity_aliases(payload)
    if aliases is None or not aliases:
        return ""
    return aliases[0]


def _margin_transaction_number(value: Any) -> Optional[int]:
    if type(value) is not str or len(value) > 128 \
            or MARGIN_TRANSACTION_ID_RE.fullmatch(value) is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _margin_history_transaction_ids(payload: Any, asset: Any) \
        -> Optional[List[int]]:
    try:
        if type(payload) is not dict or type(asset) is not str or not asset:
            return None
        rows = payload.get("rows")
        if type(rows) is not list:
            return None
        identifiers = []
        for row in rows:
            if type(row) is not dict or type(row.get("asset")) is not str \
                    or row["asset"] != asset:
                return None
            aliases = _margin_identity_aliases(row)
            if aliases is None or not aliases:
                return None
            identifier = _margin_transaction_number(aliases[0])
            if identifier is None:
                return None
            identifiers.append(identifier)
        return identifiers
    except Exception:
        return None


def _margin_history_snapshot(
        transport: Callable[..., Tuple[str, Any]], base_url: str,
        endpoint_path: str, asset: str) \
        -> Tuple[str, Dict[str, Any], List[int]]:
    payloads = {}
    identifiers = []
    try:
        for request_type in ("BORROW", "REPAY"):
            result = transport(
                "GET", base_url, endpoint_path,
                {"asset": asset, "type": request_type}, True,
            )
            if type(result) is not tuple or len(result) != 2 \
                    or result[0] != "ok":
                return "MARGIN_CONFIRMATION_UNREADABLE", {}, []
            current = _margin_history_transaction_ids(result[1], asset)
            if current is None:
                return "MARGIN_CONFIRMATION_MISMATCH", {}, []
            payloads[request_type] = result[1]
            identifiers.extend(current)
        return "", payloads, identifiers
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


def _margin_legacy_exclusivity_snapshot(
        transport: Callable[..., Tuple[str, Any]], base_url: str,
        endpoint_path: str, asset: str, transaction_id: str,
        mutation_started_utc: Any, now: datetime) \
        -> Tuple[str, Dict[str, Any], List[int], Optional[Dict[str, Any]]]:
    """Prove one legacy receipt owns the only row in its full window."""
    try:
        try:
            mutation_started = _timestamp(mutation_started_utc)
        except (JobRefusal, TypeError):
            return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
        target = _margin_transaction_number(transaction_id)
        if target is None or mutation_started > now:
            return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
        window_start = mutation_started - MARGIN_LEGACY_HISTORY_WINDOW_PAD
        # Millisecond request bounds cover the full recorded UTC window.
        window_start_ms = str(_margin_history_epoch_milliseconds(
            window_start
        ))
        window_end_ms = str(_margin_history_epoch_milliseconds(
            now, round_up=True
        ))
        payloads = {}
        identifiers = []
        for request_type in ("BORROW", "REPAY"):
            rows = []
            total = None
            complete = False
            for page in range(1, MARGIN_LEGACY_HISTORY_PAGE_CAP + 1):
                result = transport(
                    "GET", base_url, endpoint_path, {
                        "asset": asset,
                        "type": request_type,
                        "startTime": window_start_ms,
                        "endTime": window_end_ms,
                        "current": str(page),
                        "size": str(MARGIN_LEGACY_HISTORY_PAGE_SIZE),
                    }, True,
                )
                if type(result) is not tuple or len(result) != 2 \
                        or result[0] != "ok":
                    return "MARGIN_CONFIRMATION_UNREADABLE", {}, [], None
                payload = result[1]
                if type(payload) is not dict:
                    return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
                page_rows = payload.get("rows", [])
                page_total = payload.get("total")
                if type(page_rows) is not list \
                        or len(page_rows) > MARGIN_LEGACY_HISTORY_PAGE_SIZE \
                        or page_total is not None \
                        and (type(page_total) is not int or page_total < 0):
                    return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
                page_identifiers = _margin_history_transaction_ids(
                    {"rows": page_rows}, asset
                )
                if page_identifiers is None:
                    return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
                if page_total is not None:
                    if total is None:
                        total = page_total
                    elif total != page_total:
                        return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
                rows.extend(page_rows)
                identifiers.extend(page_identifiers)
                if total is not None and len(rows) > total:
                    return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
                complete = len(page_rows) < MARGIN_LEGACY_HISTORY_PAGE_SIZE \
                    or total is not None and len(rows) == total
                if complete:
                    if total is not None and len(rows) != total:
                        return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
                    if page == MARGIN_LEGACY_HISTORY_PAGE_CAP:
                        return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
                    break
            if not complete:
                return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
            payloads[request_type] = {"rows": rows}
        if identifiers != [target]:
            return "LEGACY_WINDOW_NOT_EXCLUSIVE", {}, [], None
        proof = {
            "kind": "LEGACY_EXCLUSIVITY_PROOF",
            "window_start_utc": _utc_text(window_start),
            "window_end_utc": _utc_text(now),
            "row_count": len(identifiers),
        }
        return "", payloads, identifiers, proof
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
        # Account and history reads are not atomic. An actor exactly mirroring
        # our POST timing with an identical amount can remain indistinguishable
        # until its separate transaction id becomes visible.
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


def _margin_post_history_snapshot(
        transport: Callable[..., Tuple[str, Any]], base_url: str,
        endpoint_path: str, asset: str, request_type: str,
        transaction_id: str, history_watermark: int, job_id: str) \
        -> Tuple[str, Dict[str, Any], List[int]]:
    """Retry only while a valid submitted identity is not yet visible."""
    target = _margin_transaction_number(transaction_id)
    delays = (0,) + MARGIN_HISTORY_CONFIRMATION_BACKOFF_SECONDS[
        :MARGIN_HISTORY_CONFIRMATION_RETRY_LIMIT
    ]
    latest: Tuple[str, Dict[str, Any], List[int]] = ("", {}, [])
    for attempt, delay_seconds in enumerate(delays, start=1):
        if delay_seconds:
            try:
                time.sleep(delay_seconds)
            except Exception:
                return "MARGIN_CONFIRMATION_UNREADABLE", {}, []
        history_reason, history_after, history_identifiers = \
            _margin_history_snapshot(
                transport, base_url, endpoint_path, asset
            )
        latest = history_reason, history_after, history_identifiers
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


def _margin_history_confirmation(payload: Any, transaction_id: str,
                                 expected: Dict[str, Any],
                                 request_type: str) \
        -> Optional[Dict[str, str]]:
    try:
        if type(payload) is not dict or type(transaction_id) is not str \
                or not transaction_id or type(expected) is not dict \
                or type(request_type) is not str \
                or request_type not in {"BORROW", "REPAY"}:
            return None
        rows = payload.get("rows")
        expected_asset = expected.get("asset")
        expected_principal = expected.get("principal")
        expected_interest = expected.get("interest")
        expected_status = expected.get("status")
        if type(rows) is not list or any(
                type(value) is not str for value in (
                    expected_asset, expected_principal,
                    expected_interest, expected_status,
                )):
            return None
        matches = []
        for row in rows:
            if type(row) is not dict:
                continue
            aliases = _margin_identity_aliases(row)
            if aliases is None:
                return None
            if aliases and aliases[0] == transaction_id:
                matches.append(row)
        if len(matches) != 1:
            return None
        row = matches[0]
        asset = row.get("asset")
        origin_type = row.get("type")
        principal_value = row.get("principal")
        status = row.get("status")
        if any(type(value) is not str for value in (
                asset, origin_type, principal_value, status)):
            return None
        if asset != expected_asset \
                or origin_type not in MARGIN_HISTORY_ORIGIN_TYPES:
            return None
        principal = _margin_decimal_text(principal_value)
        if principal is None or status != "CONFIRMED":
            return None
        internal_status = MARGIN_HISTORY_CONFIRMED_STATUS[request_type]
        if request_type == "REPAY":
            amount_value = row.get("amount")
            interest_value = row.get("interest")
            row_transaction = _margin_transaction_number(
                _margin_transaction_id({"txId": row.get("txId")})
            )
            expected_transaction = _margin_transaction_number(transaction_id)
            if type(amount_value) is not str \
                    or type(interest_value) is not str \
                    or row_transaction is None \
                    or row_transaction != expected_transaction:
                return None
            amount = _margin_decimal_text(amount_value)
            interest = _margin_decimal_text(interest_value)
            if amount is None or interest is None:
                return None
            with localcontext() as context:
                context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
                if Decimal(principal) + Decimal(interest) != Decimal(amount):
                    return None
            confirmation = {
                "asset": asset,
                "amount": amount,
                "principal": principal,
                "interest": interest,
                "status": internal_status,
            }
            if confirmation != expected:
                return None
            return confirmation
        confirmation = {
            "asset": asset,
            "principal": principal,
            "interest": "0",
            "status": internal_status,
        }
        if confirmation != expected:
            return None
        return confirmation
    except Exception:
        return None


def _margin_account_liability(payload: Any, asset: str) \
        -> Optional[Dict[str, str]]:
    try:
        if type(payload) is not dict or type(asset) is not str or not asset:
            return None
        rows = payload.get("userAssets")
        if type(rows) is not list:
            return None
        matches = []
        for row in rows:
            if type(row) is not dict:
                return None
            row_asset = row.get("asset")
            if type(row_asset) is not str:
                return None
            if row_asset == asset:
                matches.append(row)
        if len(matches) != 1:
            return None
        borrowed_value = matches[0].get("borrowed")
        interest_value = matches[0].get("interest")
        if type(borrowed_value) is not str \
                or type(interest_value) is not str:
            return None
        borrowed = _margin_decimal_text(borrowed_value)
        interest = _margin_decimal_text(interest_value)
        if borrowed is None or interest is None:
            return None
        with localcontext() as context:
            context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
            total = Decimal(borrowed) + Decimal(interest)
        return {
            "asset": asset,
            "borrowed": borrowed,
            "interest": interest,
            "total_liability": _canonical_decimal_text(total),
        }
    except Exception:
        return None


def _margin_account_snapshot_time(payload: Any) \
        -> Tuple[str, Optional[Decimal]]:
    """Return one strict account snapshot time without guessing its unit."""
    try:
        if type(payload) is not dict:
            return "INVALID", None
        values = []
        for field in ("timestamp", "updateTime"):
            if field not in payload:
                continue
            raw = payload[field]
            if type(raw) is int:
                raw = str(raw)
            if type(raw) is not str:
                return "INVALID", None
            canonical = _margin_decimal_text(raw)
            if canonical is None:
                return "INVALID", None
            values.append(Decimal(canonical))
        if not values:
            return "ABSENT", None
        if any(value != values[0] for value in values[1:]):
            return "INVALID", None
        return "PRESENT", values[0]
    except Exception:
        return "INVALID", None


def _margin_snapshot_time_note(before_payload: Any,
                               after_payload: Any) -> Optional[str]:
    before_kind, before_time = _margin_account_snapshot_time(before_payload)
    after_kind, after_time = _margin_account_snapshot_time(after_payload)
    if "INVALID" in {before_kind, after_kind}:
        return None
    if "ABSENT" in {before_kind, after_kind}:
        return MARGIN_SNAPSHOT_TIME_UNVERIFIED
    if before_time is None or after_time is None or after_time <= before_time:
        return None
    return MARGIN_SNAPSHOT_TIME_VERIFIED


def _margin_liability_decimals(value: Any) \
        -> Optional[Tuple[str, Decimal, Decimal, Decimal]]:
    try:
        if type(value) is not dict or type(value.get("asset")) is not str:
            return None
        canonical = [
            _margin_decimal_text(value.get(field))
            for field in ("borrowed", "interest", "total_liability")
        ]
        if any(item is None for item in canonical):
            return None
        borrowed, interest, total = (
            Decimal(item) for item in canonical if item is not None
        )
        with localcontext() as context:
            context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
            if borrowed + interest != total:
                return None
        return value["asset"], borrowed, interest, total
    except Exception:
        return None


def _margin_repay_expected_ledger_confirmation(
        params: Any, liability_before: Any) -> Optional[Dict[str, str]]:
    """Build one exact REPAY split from the pre-mutation account read."""
    try:
        if type(params) is not dict \
                or params.get("type") != "REPAY" \
                or type(params.get("asset")) is not str:
            return None
        amount = _margin_decimal_text(params.get("amount"))
        values = _margin_liability_decimals(liability_before)
        if amount is None or values is None \
                or values[0] != params["asset"]:
            return None
        with localcontext() as context:
            context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
            if values[1] + values[2] != Decimal(amount):
                return None
        return {
            "asset": params["asset"],
            "amount": amount,
            "principal": liability_before["borrowed"],
            "interest": liability_before["interest"],
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


def _margin_repay_principal_above_liability(
        liability: Any, principal: Any) -> Optional[bool]:
    values = _margin_liability_decimals(liability)
    canonical = _margin_decimal_text(principal)
    dust_bound = _margin_repay_dust_bound(principal)
    if values is None or canonical is None or dust_bound is None:
        return None
    with localcontext() as context:
        context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
        maximum = values[3] + dust_bound
    return Decimal(canonical) > maximum


def _margin_repay_proof_values(before: Any, after: Any, principal: Any) \
        -> Optional[Tuple[Decimal, Decimal, Decimal, Decimal, Decimal]]:
    try:
        before_values = _margin_liability_decimals(before)
        after_values = _margin_liability_decimals(after)
        canonical_principal = _margin_decimal_text(principal)
        if before_values is None or after_values is None \
                or canonical_principal is None \
                or before_values[0] != after_values[0]:
            return None
        amount = Decimal(canonical_principal)
        zero = Decimal("0")
        if amount <= zero:
            return None
        before_borrowed = before_values[1]
        before_interest = before_values[2]
        after_borrowed = after_values[1]
        after_interest = after_values[2]
        with localcontext() as context:
            context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
            interest_settled_first = min(amount, before_interest)
            principal_settled = amount - interest_settled_first
            expected_borrowed = max(
                zero, before_borrowed - principal_settled
            )
            liability_decrease = before_values[3] - after_values[3]
        return (
            amount, expected_borrowed, after_borrowed, after_interest,
            liability_decrease,
        )
    except Exception:
        return None


def _margin_liability_confirmation_status(
        before: Any, after: Any, principal: Any, request_type: Any) -> str:
    """Map a direction-bearing liability proof to one internal status."""
    try:
        if type(request_type) is not str \
                or request_type not in {"BORROW", "REPAY"}:
            return ""
        if request_type == "REPAY":
            proof = _margin_repay_proof_values(before, after, principal)
            dust_bound = _margin_repay_dust_bound(principal)
            if proof is None or dust_bound is None:
                return ""
            amount, expected_borrowed, after_borrowed, _after_interest, \
                liability_decrease = proof
            zero = Decimal("0")
            with localcontext() as context:
                context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
                minimum_decrease = amount - dust_bound
            enough_decrease = liability_decrease > zero \
                and liability_decrease >= minimum_decrease
            if enough_decrease and after_borrowed == expected_borrowed:
                return "REPAY_CONFIRMED"
            return ""

        if type(before) is not dict or type(after) is not dict \
                or type(principal) is not str:
            return ""
        before_asset = before.get("asset")
        after_asset = after.get("asset")
        before_borrowed = before.get("borrowed")
        after_borrowed = after.get("borrowed")
        if any(type(value) is not str for value in (
                before_asset, after_asset,
                before_borrowed, after_borrowed)) \
                or before_asset != after_asset:
            return ""
        canonical_principal = _margin_decimal_text(principal)
        canonical_before = _margin_decimal_text(before_borrowed)
        canonical_after = _margin_decimal_text(after_borrowed)
        if canonical_principal is None or canonical_before is None \
                or canonical_after is None:
            return ""
        with localcontext() as context:
            context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
            amount = Decimal(canonical_principal)
            change = Decimal(canonical_after) - Decimal(canonical_before)
        if amount > Decimal("0") and change == amount:
            return "BORROW_CONFIRMED"
        return ""
    except Exception:
        return ""


def _margin_repay_close_verdict(before: Any, after: Any,
                                principal: Any) -> str:
    proof = _margin_repay_proof_values(before, after, principal)
    values = _margin_liability_decimals(after)
    dust_bound = _margin_repay_dust_bound(principal)
    zero = Decimal("0")
    if proof is None or values is None or dust_bound is None \
            or values[1] != zero:
        return ""
    if values[2] == zero:
        status = _margin_liability_confirmation_status(
            before, after, principal, "REPAY"
        )
        return "CLOSED" if status == "REPAY_CONFIRMED" else ""
    amount, _expected_borrowed, _after_borrowed, _after_interest, \
        liability_decrease = proof
    with localcontext() as context:
        context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
        minimum_decrease = amount - dust_bound
    enough_decrease = liability_decrease > zero \
        and liability_decrease >= minimum_decrease
    if values[2] < dust_bound and enough_decrease:
        return "CLOSED-WITH-DUST"
    return ""


def _margin_liability_direction_confirmed(
        before: Any, after: Any, principal: Any, request_type: Any) -> bool:
    if type(request_type) is not str:
        return False
    return _margin_liability_confirmation_status(
        before, after, principal, request_type
    ) == request_type + "_CONFIRMED"


def _evaluate_margin_confirmation(
        history_payload: Any, history_identifiers: Any,
        transaction_id: Any, history_watermark: Any,
        expected: Any, request_type: Any, liability_before: Any,
        account_before_payload: Any, account_after_payload: Any) \
        -> MarginConfirmation:
    """Apply the one confirmation law used by completion and reconciliation."""
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
        confirmation = _margin_history_confirmation(
            history_payload, transaction_id, expected, request_type
        )
        if confirmation is None:
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_CONFIRMATION_MISMATCH"
            )
        liability = _margin_account_liability(
            account_after_payload, expected["asset"]
        )
        if liability is None:
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_CONFIRMATION_MISMATCH"
            )
        snapshot_time_note = _margin_snapshot_time_note(
            account_before_payload, account_after_payload
        )
        if snapshot_time_note is None:
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_SNAPSHOT_TIME_MISMATCH",
                account_liability=liability,
            )
        confirmation_amount = expected["principal"] \
            if request_type == "BORROW" else expected["amount"]
        confirmation_status = _margin_liability_confirmation_status(
            liability_before, liability, confirmation_amount,
            request_type,
        )
        exact_confirmation = confirmation_status == expected["status"]
        if request_type == "BORROW":
            if not exact_confirmation:
                return MarginConfirmation(
                    "UNKNOWN", "MARGIN_LIABILITY_MISMATCH",
                    account_liability=liability,
                    snapshot_time_note=snapshot_time_note,
                )
            return MarginConfirmation(
                "OPEN", expected["status"], confirmation, liability,
                snapshot_time_note,
            )

        repay_verdict = _margin_repay_close_verdict(
            liability_before, liability, confirmation_amount
        )
        if not repay_verdict \
                or repay_verdict == "CLOSED" and not exact_confirmation:
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_LIABILITY_MISMATCH",
                confirmation if exact_confirmation else None, liability,
                snapshot_time_note,
            )
        follow_up = None
        reason = expected["status"]
        if repay_verdict == "CLOSED-WITH-DUST":
            follow_up = {
                "kind": "INTEREST_ONLY_REPAY",
                "asset": liability["asset"],
                "principal": liability["interest"],
            }
            reason = "REPAY_CONFIRMED_WITH_DUST"
        return MarginConfirmation(
            repay_verdict, reason,
            confirmation if exact_confirmation else None, liability,
            snapshot_time_note, follow_up,
        )
    except Exception:
        return MarginConfirmation(
            "UNKNOWN", "MARGIN_CONFIRMATION_UNREADABLE"
        )


def _evaluate_legacy_margin_confirmation(
        history_payload: Any, history_identifiers: Any,
        transaction_id: Any, expected: Any, request_type: Any,
        account_payload: Any, exclusivity_proof: Any) \
        -> MarginConfirmation:
    """Evaluate the two substitutes for the frozen legacy receipt shape."""
    try:
        target = _margin_transaction_number(transaction_id)
        if target is None or history_identifiers != [target] \
                or not _valid_margin_legacy_exclusivity_proof(
                    exclusivity_proof
                ):
            return MarginConfirmation(
                "UNKNOWN", "LEGACY_WINDOW_NOT_EXCLUSIVE"
            )
        confirmation = _margin_history_confirmation(
            history_payload, transaction_id, expected, request_type
        )
        if confirmation is None:
            return MarginConfirmation(
                "UNKNOWN", "MARGIN_CONFIRMATION_MISMATCH"
            )
        liability = _margin_account_liability(
            account_payload, expected["asset"]
        )
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
        payload: Any, asset: str) \
        -> Tuple[Optional[Dict[str, str]], str]:
    if type(payload) is not dict or type(payload.get("userAssets")) is not list:
        return None, "LEGACY_REPAY_CURRENT_ACCOUNT_MISSING"
    matches = [
        row for row in payload["userAssets"]
        if type(row) is dict and row.get("asset") == asset
    ]
    if len(matches) != 1:
        return None, "LEGACY_REPAY_CURRENT_ASSET_MISSING"
    row = matches[0]
    borrowed = _margin_decimal_text(row.get("borrowed"))
    if borrowed is None:
        return None, "LEGACY_REPAY_CURRENT_BORROWED_MISSING"
    interest = _margin_decimal_text(row.get("interest"))
    if interest is None:
        return None, "LEGACY_REPAY_CURRENT_INTEREST_MISSING"
    with localcontext() as context:
        context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
        total = Decimal(borrowed) + Decimal(interest)
    return {
        "asset": asset,
        "borrowed": borrowed,
        "interest": interest,
        "total_liability": _canonical_decimal_text(total),
    }, ""


def _legacy_repay_exchange_provenance(
        history_payload: Any, receipt_facts: Tuple[str, str, str, str, str]) \
        -> str:
    """Name the first exact-id exchange fact that misses the allowlist."""
    try:
        transaction_id = receipt_facts[1]
        asset = receipt_facts[2]
        requested_amount = _margin_decimal_text(receipt_facts[3])
        if requested_amount is None:
            return "ALLOWLIST_REQUESTED_AMOUNT"
        if type(history_payload) is not dict \
                or type(history_payload.get("rows")) is not list \
                or len(history_payload["rows"]) != 1 \
                or type(history_payload["rows"][0]) is not dict:
            return "EXCHANGE_HISTORY_ROW"
        row = history_payload["rows"][0]
        aliases = _margin_identity_aliases(row)
        target = _margin_transaction_number(transaction_id)
        row_target = _margin_transaction_number(
            _margin_transaction_id({"txId": row.get("txId")})
        )
        if target is None or aliases != [transaction_id] \
                or row_target != target:
            return "EXCHANGE_TRANSACTION_ID"
        if type(row.get("asset")) is not str or row["asset"] != asset:
            return "EXCHANGE_ASSET"
        amount = _margin_decimal_text(row.get("amount"))
        if amount is None \
                or Decimal(amount) != Decimal(requested_amount):
            return "EXCHANGE_AMOUNT"
        principal = _margin_decimal_text(row.get("principal"))
        if principal is None:
            return "EXCHANGE_PRINCIPAL"
        interest = _margin_decimal_text(row.get("interest"))
        if interest is None:
            return "EXCHANGE_INTEREST"
        with localcontext() as context:
            context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
            if Decimal(principal) + Decimal(interest) != Decimal(amount):
                return "EXCHANGE_PRINCIPAL_INTEREST_TOTAL"
        return ""
    except Exception:
        return "EXCHANGE_HISTORY_ROW"


def _legacy_repay_history_confirmation(
        history_payload: Any, transaction_id: Any, expected: Any,
        liability_before: Any) \
        -> Tuple[Optional[Dict[str, str]], str]:
    try:
        if type(history_payload) is not dict \
                or type(history_payload.get("rows")) is not list:
            return None, "LEGACY_REPAY_HISTORY_ROWS_MISSING"
        target = _margin_transaction_number(transaction_id)
        rows = history_payload["rows"]
        if target is None or len(rows) != 1 or type(rows[0]) is not dict:
            return None, "LEGACY_REPAY_TX_ID_MISSING"
        row = rows[0]
        aliases = _margin_identity_aliases(row)
        if aliases is None or len(aliases) != 1:
            return None, "LEGACY_REPAY_TX_ID_MISSING"
        row_target = _margin_transaction_number(
            _margin_transaction_id({"txId": row.get("txId")})
        )
        if row_target is None:
            return None, "LEGACY_REPAY_TX_ID_MISSING"
        if aliases[0] != transaction_id or row_target != target:
            return None, "LEGACY_REPAY_TX_ID_MISMATCH"
        for field in ("asset", "amount", "principal", "interest",
                      "status", "type"):
            if type(row.get(field)) is not str:
                return None, "LEGACY_REPAY_%s_MISSING" % field.upper()
        if row["asset"] != expected.get("asset"):
            return None, "LEGACY_REPAY_ASSET_MISMATCH"
        if row["type"] not in MARGIN_HISTORY_ORIGIN_TYPES:
            return None, "LEGACY_REPAY_ORIGIN_TYPE_MISMATCH"
        if row["status"] != "CONFIRMED" \
                or expected.get("status") \
                != MARGIN_HISTORY_CONFIRMED_STATUS["REPAY"]:
            return None, "LEGACY_REPAY_STATUS_MISMATCH"
        amount = _margin_decimal_text(row["amount"])
        principal = _margin_decimal_text(row["principal"])
        interest = _margin_decimal_text(row["interest"])
        expected_amount = _margin_decimal_text(expected.get("principal"))
        if amount is None:
            return None, "LEGACY_REPAY_AMOUNT_MISSING"
        if principal is None:
            return None, "LEGACY_REPAY_PRINCIPAL_MISSING"
        if interest is None:
            return None, "LEGACY_REPAY_INTEREST_MISSING"
        if expected_amount is None \
                or Decimal(amount) != Decimal(expected_amount):
            return None, "LEGACY_REPAY_AMOUNT_MISMATCH"
        before_values = _margin_liability_decimals(liability_before)
        if before_values is None:
            return None, "LEGACY_REPAY_ACCOUNT_LIABILITY_BEFORE_MISSING"
        with localcontext() as context:
            context.prec = (MARGIN_DECIMAL_TEXT_MAX * 2) + 2
            if Decimal(principal) + Decimal(interest) != Decimal(amount):
                return None, "LEGACY_REPAY_ROW_TOTAL_MISMATCH"
        if Decimal(principal) != before_values[1]:
            return None, "LEGACY_REPAY_PRINCIPAL_MISMATCH"
        if Decimal(interest) != before_values[2]:
            return None, "LEGACY_REPAY_INTEREST_MISMATCH"
        return {
            "asset": row["asset"],
            "amount": amount,
            "principal": principal,
            "interest": interest,
            "status": MARGIN_HISTORY_CONFIRMED_STATUS["REPAY"],
        }, ""
    except Exception:
        return None, "MARGIN_CONFIRMATION_UNREADABLE"


def _evaluate_legacy_repay_confirmation(
        history_payload: Any, history_identifiers: Any,
        transaction_id: Any, history_watermark: Any, expected: Any,
        liability_before: Any, account_payload: Any) -> MarginConfirmation:
    """Prove the frozen version-1 REPAY receipt from exchange truth."""
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
            history_payload, transaction_id, expected, liability_before
        )
        if confirmation is None:
            return MarginConfirmation("UNKNOWN", history_reason)
        liability, missing_reason = _legacy_repay_current_liability(
            account_payload, expected["asset"]
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
        post_params: Dict[str, Any], validated_job_digest: str,
        authority_job: Optional[Job] = None,
        reverse_entry: bool = False) \
        -> Tuple[str, datetime, Dict[str, str], bool]:
    governing_job = authority_job or job
    params_digest = _json_authority_digest(post_params)
    digests: Dict[str, str] = {
        "ready_params": params_digest or _authority_digest(None),
        "job_file": validated_job_digest
        if type(validated_job_digest) is str
        and re.fullmatch(r"[0-9a-f]{64}", validated_job_digest)
        else _authority_digest(None),
    }
    job_encoded = accept_encoded = operator_encoded = stop_encoded = None
    read_reason = ""
    try:
        job_encoded = _read_file_bytes(os.path.join(
            paths["jobs"], "job-%s.json" % governing_job.job_id))
    except OSError:
        read_reason = "MARGIN_VALIDATION_UNKNOWN"
    if job_encoded is not None \
            and _authority_digest(job_encoded) != validated_job_digest:
        read_reason = "MARGIN_VALIDATION_UNKNOWN"
    for name, missing_reason in (
            ("accept_one", "ACCEPT_ONE_INVALID"),
            ("operator", "OPERATOR_MISMATCH"),
            ("stop", "MARGIN_VALIDATION_UNKNOWN")):
        try:
            encoded = _read_file_bytes(paths[name])
        except FileNotFoundError:
            encoded = None
        except OSError:
            encoded = None
            read_reason = read_reason or missing_reason
        if name == "accept_one":
            accept_encoded = encoded
        elif name == "operator":
            operator_encoded = encoded
        else:
            stop_encoded = encoded
    verified_now = _utc_now()
    digests.update({
        "accept_one": _authority_digest(accept_encoded),
        "operator": _authority_digest(operator_encoded),
        "stop_jobs": _authority_digest(stop_encoded),
    })
    stopped = stop_encoded is not None
    if read_reason:
        return read_reason, verified_now, digests, stopped
    try:
        fresh_raw = _decode_strict_object(job_encoded or b"")
        for field, digest_name in (
                ("operator_token", "operator_token"),
                ("expires_utc", "job_expiry")):
            value = fresh_raw.get(field)
            if type(value) is str:
                digests[digest_name] = _authority_digest(
                    value.encode("utf-8"))
    except Exception:
        return "MARGIN_VALIDATION_UNKNOWN", verified_now, digests, stopped

    if governing_job.op == REVERSE_OP:
        try:
            fresh_job = _validate_job(
                fresh_raw, governing_job.job_id, verified_now)
        except JobRefusal as error:
            return error.reason, verified_now, digests, stopped
        except Exception:
            return "MARGIN_VALIDATION_UNKNOWN", verified_now, digests, stopped
        if fresh_job != governing_job \
                or fresh_job.operator_token != governing_job.operator_token \
                or not isinstance(operation, dict) \
                or operation.get("params") != post_params:
            return "MARGIN_VALIDATION_UNKNOWN", verified_now, digests, stopped
        stop_blocks = reverse_entry
    else:
        try:
            final_operation = margin_ops.build_margin_operation(
                _margin_job_projection(fresh_raw), MARGIN_ASSET_WHITELIST,
                verified_now)
        except Exception:
            return "MARGIN_VALIDATION_UNKNOWN", verified_now, digests, stopped
        if not isinstance(final_operation, dict):
            return "MARGIN_VALIDATION_UNKNOWN", verified_now, digests, stopped
        if final_operation.get("verdict") == "REFUSED":
            reason = final_operation.get("reason")
            return (reason if type(reason) is str and reason
                    else "MARGIN_VALIDATION_UNKNOWN"), verified_now, digests, stopped
        try:
            fresh_job = _validate_margin_job(
                fresh_raw, job.job_id, verified_now, final_operation)
        except JobRefusal as error:
            return error.reason, verified_now, digests, stopped
        except ValueError:
            return "MARGIN_VALIDATION_UNKNOWN", verified_now, digests, stopped
        if fresh_job.operator_token != job.operator_token:
            return "OPERATOR_MISMATCH", verified_now, digests, stopped
        if fresh_job != job or fresh_job.margin_operation != operation \
                or not isinstance(fresh_job.margin_operation, dict) \
                or fresh_job.margin_operation.get("params") != post_params:
            return "MARGIN_VALIDATION_UNKNOWN", verified_now, digests, stopped
        if job.op != margin_ops.MARGIN_BORROW:
            return "", verified_now, digests, stopped
        stop_blocks = True

    if stop_blocks and stopped:
        return "STOP_JOBS_SEALED", verified_now, digests, stopped
    if accept_encoded is None:
        return "ENTRY_BLOCKED_NOT_ACCEPTED", verified_now, digests, stopped
    try:
        acceptance = _validate_accept_one_object(
            _decode_strict_object(accept_encoded), verified_now)
    except (JobRefusal, ValueError):
        return "ACCEPT_ONE_INVALID", verified_now, digests, stopped
    if operator_encoded is None:
        return "OPERATOR_MISMATCH", verified_now, digests, stopped
    try:
        operator = _validate_operator_object(
            _decode_strict_object(operator_encoded))
    except (JobRefusal, ValueError):
        operator = None
    if operator is None or fresh_job.requested_by != operator \
            or acceptance.operator != operator \
            or fresh_job.operator_token != operator:
        return "OPERATOR_MISMATCH", verified_now, digests, stopped
    if fresh_job.requested_max_notional > acceptance.max_notional:
        return "ACCEPT_ONE_NOTIONAL_EXCEEDED", verified_now, digests, stopped
    return "", verified_now, digests, stopped


def _run_margin_operation(
        job: Job, paths: Dict[str, str],
        request: Optional[Callable[..., Tuple[str, Any]]] = None,
        governor_token: str = "", sha: str = "",
        validated_job_digest: str = "",
        authority_job: Optional[Job] = None,
        reverse_entry: bool = False,
        asset_whitelist: Optional[frozenset] = None,
        source_suffix: str = "", claim_intent: bool = True) -> Outcome:
    fresh_now = _utc_now()
    principal = _canonical_decimal_text(job.requested_max_notional)
    projection = {
        "op": job.op,
        "asset": job.symbol,
        "principal": principal,
        "client_tag": (
            job.margin_operation.get("client_tag", "")
            if isinstance(job.margin_operation, dict) else ""
        ),
        "intent_id": job.intent_id,
        "expires_utc": _utc_text(job.expires_utc),
        "operator_token": job.operator_token,
    }
    try:
        operation = margin_ops.build_margin_operation(
            projection, asset_whitelist or MARGIN_ASSET_WHITELIST, fresh_now
        )
    except Exception:
        return Outcome("REFUSED", 2, "", "MARGIN_VALIDATION_UNKNOWN")
    if not isinstance(operation, dict):
        return Outcome("REFUSED", 2, "", "MARGIN_VALIDATION_UNKNOWN")
    if operation.get("verdict") == "REFUSED":
        reason = operation.get("reason")
        if type(reason) is not str or not reason:
            reason = "MARGIN_VALIDATION_UNKNOWN"
        return Outcome("REFUSED", 2, "", reason)
    if job.requested_max_notional > MARGIN_RUNNER_NOTIONAL_CAP \
            or operation != job.margin_operation:
        return Outcome("REFUSED", 2, "", "MARGIN_VALIDATION_UNKNOWN")
    ready_operation = operation

    suffix = "-" + source_suffix if source_suffix else ""
    source_path = os.path.join(
        paths["receipts"], "margin-%s%s.json" % (job.job_id, suffix)
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

    base_url = basis_executor.SPOT.base_url
    try:
        transport = request or basis_executor.live_request_factory(
            os.environ.get("BINANCE_API_KEY", ""),
            os.environ.get("BINANCE_API_SECRET", ""),
        )
    except Exception:
        return _finish_margin_before_post(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_POST_UNREADABLE", fresh_now,
        )

    post_params = dict(operation["params"])
    authority_refusal, authority_now, authority_digests, stop_present = \
        _margin_post_authority(
            job, paths, operation, post_params, validated_job_digest,
            authority_job, reverse_entry,
        )
    receipt["authority_checked_utc"] = _utc_text(authority_now)
    receipt["authority_digests"] = authority_digests
    receipt["authority_stop_jobs_present"] = stop_present
    if authority_refusal:
        return _finish_margin_before_post(
            receipt, source_path, "REFUSED", 2,
            authority_refusal, authority_now,
        )

    params = operation["params"]
    history_reason, _history_before, history_ids_before = \
        _margin_history_snapshot(
            transport, base_url, operation["endpoint_path"],
            params["asset"],
        )
    if history_reason:
        return _finish_margin_before_post(
            receipt, source_path, "UNKNOWN", 4,
            history_reason, authority_now,
        )
    history_watermark = max(history_ids_before, default=0)

    try:
        account_before_result = transport(
            "GET", base_url, MARGIN_ACCOUNT_PATH, {}, True,
        )
        account_before_readable = type(account_before_result) is tuple \
            and len(account_before_result) == 2 \
            and account_before_result[0] == "ok"
        liability_before = _margin_account_liability(
            account_before_result[1], params["asset"]
        ) if account_before_readable else None
    except Exception:
        return _finish_margin_before_post(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_CONFIRMATION_UNREADABLE", authority_now,
        )
    if not account_before_readable:
        return _finish_margin_before_post(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_CONFIRMATION_UNREADABLE", authority_now,
        )
    if liability_before is None:
        return _finish_margin_before_post(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_CONFIRMATION_MISMATCH", authority_now,
        )
    repay_expected = None
    if job.op == margin_ops.MARGIN_REPAY:
        principal_above_liability = \
            _margin_repay_principal_above_liability(
                liability_before, principal
            )
        if principal_above_liability is None:
            return _finish_margin_before_post(
                receipt, source_path, "UNKNOWN", 4,
                "MARGIN_CONFIRMATION_MISMATCH", authority_now,
            )
        if principal_above_liability:
            return _finish_margin_before_post(
                receipt, source_path, "REFUSED", 2,
                "PRINCIPAL_EXCEEDS_LIABILITY", authority_now,
            )
        repay_expected = _margin_repay_expected_ledger_confirmation(
            params, liability_before
        )
        if repay_expected is None:
            return _finish_margin_before_post(
                receipt, source_path, "REFUSED", 2,
                "REPAY_PRINCIPAL_INTEREST_MISMATCH", authority_now,
            )

    authority_refusal, authority_now, authority_digests, stop_present = \
        _margin_post_authority(
            job, paths, operation, post_params, validated_job_digest,
            authority_job, reverse_entry,
        )
    receipt["authority_checked_utc"] = _utc_text(authority_now)
    receipt["authority_digests"] = authority_digests
    receipt["authority_stop_jobs_present"] = stop_present
    if authority_refusal:
        return _finish_margin_before_post(
            receipt, source_path, "REFUSED", 2,
            authority_refusal, authority_now,
        )
    if repay_expected is not None:
        ready_operation = dict(operation)
        ready_operation["expected_ledger_confirmation"] = repay_expected
        receipt["ready"] = ready_operation

    if claim_intent:
        try:
            intent_claimed = _claim_margin_intent(
                paths, operation["intent_id"], job.job_id, authority_now
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
    sealed_repay = job.op == margin_ops.MARGIN_REPAY and stop_present
    if sealed_repay:
        receipt["execution_mode"] = "STOP_JOBS_REPAY"
    receipt["mutation_attempts"] = 1
    receipt["mutation_started_utc"] = _utc_text(authority_now)
    receipt["history_watermark"] = history_watermark
    receipt["account_liability_before"] = liability_before
    try:
        _persist_margin_source(source_path, receipt)
    except Exception:
        return Outcome(
            "UNKNOWN", 4, source_path, "MARGIN_READY_PERSISTENCE_FAILED"
        )

    # These separate file reads and the socket write cannot be one OS-level
    # transaction.  The remaining read-to-write window is irreducible; the
    # durable timestamp and digests above record it without claiming otherwise.
    try:
        post_result = transport(
            "POST", base_url, operation["endpoint_path"],
            post_params, True,
        )
    except Exception:
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_POST_UNREADABLE", fresh_now,
        )
    if type(post_result) is not tuple or len(post_result) != 2:
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_POST_UNREADABLE", fresh_now,
        )
    try:
        post_kind, post_payload = post_result
        transaction_id = _margin_transaction_id(post_payload) \
            if post_kind == "ok" else ""
        receipt["submission"] = {
            "kind": post_kind if type(post_kind) is str else "unreadable",
            "transaction_id": transaction_id,
        }
    except Exception:
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_CONFIRMATION_UNREADABLE", fresh_now,
        )
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

    expected = ready_operation["expected_ledger_confirmation"]
    # The post-state account read stays inside the two history snapshots so
    # visible activity contributing to its delta is checked by the fence.
    try:
        account_result = transport(
            "GET", base_url, MARGIN_ACCOUNT_PATH, {}, True,
        )
    except Exception:
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_CONFIRMATION_UNREADABLE", fresh_now,
        )
    if type(account_result) is not tuple or len(account_result) != 2 \
            or account_result[0] != "ok":
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_CONFIRMATION_UNREADABLE", fresh_now,
        )

    history_reason, history_after, history_ids_after = \
        _margin_post_history_snapshot(
            transport, base_url, operation["endpoint_path"],
            params["asset"], params["type"], transaction_id,
            history_watermark, job.job_id,
        )
    if history_reason:
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            history_reason, fresh_now,
        )
    confirmation = _evaluate_margin_confirmation(
        history_after.get(params["type"]), history_ids_after,
        transaction_id, history_watermark, expected, params["type"],
        liability_before, account_before_result[1], account_result[1],
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
    if sealed_repay:
        success_reason = "REPAY_CONFIRMED_WITH_DUST_UNDER_STOP_JOBS" \
            if confirmation.verdict == "CLOSED-WITH-DUST" \
            else "REPAY_CONFIRMED_UNDER_STOP_JOBS"
    return _finish_margin_source(
        receipt, source_path, confirmation.verdict, 0, success_reason,
        fresh_now, confirmation.ledger_confirmation,
        confirmation.account_liability,
    )


class ReverseReadUnknown(Exception):
    """One required reverse-mechanics exchange fact is unreadable."""


def _reverse_read(transport: Callable[..., Tuple[str, Any]],
                  base_url: str, path: str,
                  params: Optional[Dict[str, str]], signed: bool,
                  label: str) -> Any:
    try:
        result = transport("GET", base_url, path, params or {}, signed)
    except Exception:
        raise ReverseReadUnknown(label + "_UNREADABLE") from None
    if type(result) is not tuple or len(result) != 2 \
            or result[0] != "ok":
        raise ReverseReadUnknown(label + "_UNREADABLE")
    return result[1]


def _reverse_plan_row(plan: reverse_mechanics.ReversePlan) -> Dict[str, Any]:
    return {
        "symbol": plan.symbol,
        "intent_id": plan.intent_id,
        "client_tag": plan.client_tag,
        "base_qty": _decimal_text(plan.base_qty),
        "borrow_asset": plan.borrow_asset,
        "quote_notional": _decimal_text(plan.quote_notional),
        "dust": _decimal_text(plan.dust),
        "legs": [{
            "name": leg.name,
            "client_id": leg.client_id_seed,
            "base_qty": _decimal_text(leg.base_qty),
            "reduce_only": leg.reduce_only,
        } for leg in plan.legs],
    }


def _reverse_margin_fill_row(fill: margin_orders.MarginFill) \
        -> Tuple[List[Dict[str, str]], Dict[str, str]]:
    rows = []
    totals: Dict[str, Decimal] = {}
    for index, (price, quantity, commission, asset) in enumerate(fill.fills or ()):
        rows.append({
            "trade_id": "margin-%s-%s" % (fill.order_id, index + 1),
            "order_id": str(fill.order_id),
            "price": _decimal_text(price),
            "qty": _decimal_text(quantity),
            "realized_pnl": "0",
            "commission": _decimal_text(commission),
            "commission_asset": asset,
        })
        totals[asset] = totals.get(asset, Decimal("0")) + commission
    return rows, {
        asset: _decimal_text(amount)
        for asset, amount in sorted(totals.items())
    }


def _reverse_start_leg(receipt: Dict[str, Any], path: str, leg: str,
                       client_id: str,
                       params: Optional[Dict[str, str]] = None) \
        -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "leg": leg, "client_id": client_id, "status": "PENDING",
        "started_utc": _utc_text(_utc_now()), "mutation_attempts": 0,
        "fills": [], "fees_by_asset": {},
    }
    if params is not None:
        row["params"] = dict(params)
    receipt["legs"].append(row)
    _persist_json_receipt(path, receipt)
    return row


def _reverse_finish_leg(receipt: Dict[str, Any], path: str,
                        row: Dict[str, Any], status: str, reason: str,
                        fills: Optional[List[Dict[str, str]]] = None,
                        fees: Optional[Dict[str, str]] = None) -> None:
    row.update({
        "status": status, "reason": reason,
        "finished_utc": _utc_text(_utc_now()),
        "fills": list(fills or []),
        "fees_by_asset": dict(fees or {}),
    })
    _persist_json_receipt(path, receipt)


def _reverse_action_row(action: Any, now: datetime) -> Dict[str, Any]:
    row = {"kind": getattr(action, "kind", "UNKNOWN"), "utc": _utc_text(now)}
    if isinstance(action, reverse_mechanics.NextLeg):
        row["leg"] = action.leg.name
        row["quantity"] = _decimal_text(action.leg.base_qty)
    elif isinstance(action, reverse_mechanics.Unwind):
        row["legs"] = list(action.sequence)
        row["leg_quantities"] = [{
            "leg": leg.name, "quantity": _decimal_text(leg.base_qty)
        } for leg in action.leg_sequence]
    elif isinstance(action, reverse_mechanics.Freeze):
        row["reason"] = action.reason
    return row


def _reverse_exposure_reason(
        truth: reverse_mechanics.ExchangeTruth,
        plan: reverse_mechanics.ReversePlan,
        spot_open_orders: int = 0, futures_open_orders: int = 0) -> str:
    standing = []
    if truth.futures_position and truth.futures_position > Decimal("0"):
        standing.append(reverse_mechanics.FUTURES_CLOSE)
    if truth.base_balance_delta is not None \
            and abs(truth.base_balance_delta) >= plan.dust:
        standing.append("BASE_BALANCE_DELTA")
    if truth.liability and truth.liability > Decimal("0"):
        standing.append(reverse_mechanics.REPAY)
    if spot_open_orders:
        standing.append("SPOT_OPEN_ORDERS")
    if futures_open_orders:
        standing.append("FUTURES_OPEN_ORDERS")
    return "OPEN_EXPOSURE:" + (",".join(standing) if standing else "KNOWN")


def _run_reverse_mechanics(
        job: Job, paths: Dict[str, str],
        request: Optional[Callable[..., Tuple[str, Any]]],
        governor_token: str, sha: str, validated_job_digest: str) -> Outcome:
    source_path = os.path.join(paths["receipts"], "reverse-%s.json" % job.job_id)
    receipt: Dict[str, Any] = {
        "kind": "reverse_mechanics_roundtrip", "job_id": job.job_id,
        "op": REVERSE_OP, "symbol": job.symbol, "borrow_asset": job.borrow_asset,
        "quote_cap": _decimal_text(job.effective_notional),
        "intent_id": job.intent_id, "client_tag": job.client_tag, "sha": sha,
        "started_utc": _utc_text(_utc_now()), "mutation_attempts": 0,
        "verdict": "PREFLIGHT", "reason": "", "plan": None, "market": {},
        "ladder_actions": [], "legs": [],
    }
    if governor_token:
        receipt.update({"governor_token": governor_token,
                        "governor_release": "PENDING"})
    try:
        _persist_margin_source(source_path, receipt, create=True)
    except Exception:
        return Outcome("UNKNOWN", 4, source_path if os.path.isfile(source_path)
                       else "", "REVERSE_READY_PERSISTENCE_FAILED")
    trade_fills: List[Dict[str, str]] = []

    def finish(verdict: str, code: int, reason: str) -> Outcome:
        result = _finish_margin_source(
            receipt, source_path, verdict, code, reason, _utc_now())
        return Outcome(
            result.verdict, result.exit_code, result.receipt_path, result.reason,
            _momentum_ledger(trade_fills, closed=result.verdict == "CLOSED"))

    try:
        transport = request or basis_executor.live_request_factory(
            os.environ.get("BINANCE_API_KEY", ""),
            os.environ.get("BINANCE_API_SECRET", ""))
    except Exception:
        return finish("UNKNOWN", 4, "REVERSE_TRANSPORT_UNREADABLE")

    previous_http = futures_hand.http

    def reverse_http(method, path, params=None, signed=False):
        return transport(method, basis_executor.USDM.base_url, path,
                         params or {}, signed)

    futures_hand.http = reverse_http
    try:
        try:
            spot = _spot_market_context(_spot_state(
                job.symbol, sha, paths["receipts"], "reverse-" + job.job_id,
                transport), job.effective_notional, margin_required=True)
            account = _reverse_read(
                transport, basis_executor.SPOT.base_url, MARGIN_ACCOUNT_PATH,
                {}, True, "REVERSE_MARGIN_ACCOUNT")
            liability = _margin_account_liability(account, job.borrow_asset)
            balances = _spot_balances(account, job.borrow_asset)
            rows = account.get("userAssets") if isinstance(account, dict) else ()
            matches = [row for row in rows if isinstance(row, dict)
                       and row.get("asset") == job.borrow_asset]
            if liability is None or len(matches) != 1:
                raise ReverseReadUnknown("REVERSE_MARGIN_ACCOUNT_UNREADABLE")
            asset_row = matches[0]
            borrowable = [_momentum_decimal(asset_row[name], "reverse borrowable")
                          for name in ("maxBorrowable", "borrowable",
                                      "maxBorrowableAmount")
                          if name in asset_row]
            interest = [_momentum_decimal(
                asset_row[name], "reverse hourly interest", allow_zero=True)
                for name in ("interestRateHourly", "hourlyInterestRate",
                             "interestRate") if name in asset_row]
            if not borrowable or len(set(borrowable)) != 1 \
                    or not interest or len(set(interest)) != 1:
                raise ReverseReadUnknown("REVERSE_BORROW_FACTS_UNREADABLE")
            _configure_futures_hand(paths["receipts"])
            try:
                hand_plan = futures_hand.preflight(
                    job.effective_notional,
                    Decimal(os.environ.get("MAX_SLIPPAGE_BPS", "10")), "BUY")
            except futures_hand.CapabilityBlocked as error:
                raise ReverseReadUnknown(
                    "REVERSE_FUTURES_PREFLIGHT_UNREADABLE") from error
            except futures_hand.PreflightError as error:
                raise JobRefusal("REVERSE_FUTURES_PREFLIGHT: %s" % error) \
                    from None
            if hand_plan.get("current_leverage") != futures_hand.TARGET_LEVERAGE:
                raise JobRefusal("REVERSE_FUTURES_LEVERAGE_REFUSED")
            plan_result = plan_reverse_mechanics(
                job.symbol, job.effective_notional,
                {"step": spot["step"], "min_notional": spot["min_notional"],
                 "price": spot["ask"]},
                {"step": hand_plan["step"],
                 "min_notional": hand_plan["min_notional"],
                 "price": hand_plan["limit_price"]},
                job.borrow_asset, borrowable[0], interest[0], job.intent_id,
                job.client_tag, limits)
            if not isinstance(plan_result, reverse_mechanics.ReversePlan):
                reason = getattr(plan_result, "reason", "REVERSE_PLAN_REFUSED")
                raise JobRefusal(reason if type(reason) is str
                                 else "REVERSE_PLAN_REFUSED")
            plan = plan_result
            if plan.base_qty > hand_plan["quantity"]:
                raise JobRefusal("REVERSE_FUTURES_QUANTITY_REFUSED")
            receipt["plan"] = _reverse_plan_row(plan)
            if Decimal(liability["total_liability"]) != 0:
                raise JobRefusal("EXISTING_MARGIN_LIABILITY")
        except JobRefusal as error:
            return finish("REFUSED", 2, error.reason)
        except (MomentumStateError, ReverseReadUnknown) as error:
            return finish("UNKNOWN", 4, str(error))
        except Exception:
            return finish("UNKNOWN", 4, "REVERSE_PREFLIGHT_UNREADABLE")

        initial_base_free = balances[job.borrow_asset]
        hand_plan = dict(hand_plan)
        hand_plan.update({
            "quantity": plan.base_qty,
            "max_fill_notional": plan.base_qty * hand_plan["limit_price"],
        })
        receipt["market"] = {
            "spot_bid": _decimal_text(spot["bid"]),
            "spot_ask": _decimal_text(spot["ask"]),
            "spot_step": _decimal_text(spot["step"]),
            "spot_min_notional": _decimal_text(spot["min_notional"]),
            "futures_limit_price": _decimal_text(hand_plan["limit_price"]),
        }
        try:
            _persist_margin_source(source_path, receipt)
        except Exception:
            return Outcome("UNKNOWN", 4, source_path,
                           "REVERSE_RECEIPT_PERSISTENCE_FAILED")

        def exchange_truth():
            current = _reverse_read(
                transport, basis_executor.SPOT.base_url, MARGIN_ACCOUNT_PATH,
                {}, True, "REVERSE_MARGIN_ACCOUNT")
            current_liability = _margin_account_liability(
                current, job.borrow_asset)
            if current_liability is None:
                raise ReverseReadUnknown("REVERSE_MARGIN_ACCOUNT_UNREADABLE")
            current_free = _spot_balances(
                current, job.borrow_asset)[job.borrow_asset]
            try:
                futures = _momentum_measure(job.symbol)
            except MomentumStateError as error:
                raise ReverseReadUnknown(
                    "REVERSE_FUTURES_POSITION_UNREADABLE") from error
            spot_orders = _reverse_read(
                transport, basis_executor.SPOT.base_url,
                basis_executor.SPOT_OPEN_ORDERS_PATH, {"symbol": job.symbol},
                True, "REVERSE_SPOT_OPEN_ORDERS")
            if not isinstance(spot_orders, list):
                raise ReverseReadUnknown(
                    "REVERSE_SPOT_OPEN_ORDERS_UNREADABLE")
            return reverse_mechanics.ExchangeTruth(
                Decimal(current_liability["total_liability"]),
                current_free - initial_base_free, futures["qty"]), {
                "spot_open_orders": len(spot_orders),
                "futures_open_orders": futures["open_orders"],
            }

        def final_authority(row, leg, params):
            reason, checked, digests, stopped = _margin_post_authority(
                job, paths, {"params": dict(params)}, dict(params),
                validated_job_digest,
                reverse_entry=leg.name in REVERSE_ENTRY_LEGS)
            authority = {
                "authority_checked_utc": _utc_text(checked),
                "authority_digests": digests,
                "authority_stop_jobs_present": stopped,
            }
            row.update(authority)
            receipt.update(authority)
            return reason

        def close_leg(row, status, reason, amounts=(), fills=None, fees=None,
                      net=None):
            if isinstance(net, Decimal):
                row["net_base_after_fees"] = _decimal_text(net)
            authority_names = ("authority_checked_utc", "authority_digests",
                               "authority_stop_jobs_present")
            if "authority_checked_utc" in row:
                receipt.update({name: row[name] for name in authority_names})
            _reverse_finish_leg(
                receipt, source_path, row, status, reason, fills, fees)
            return status, tuple(amounts), reason

        def margin_mutation(leg):
            request_type = "BORROW" if leg.name == reverse_mechanics.BORROW \
                else "REPAY"
            op = "MARGIN_" + request_type
            amount = _canonical_decimal_text(leg.base_qty)
            intent = "mrgintent-" + hashlib.sha256(
                (job.intent_id + ":" + leg.name).encode("utf-8")
            ).hexdigest()[:24]
            try:
                tag = margin_ops.deterministic_client_tag(
                    op, job.borrow_asset, amount, _utc_text(job.expires_utc))
                operation = margin_ops.build_margin_operation({
                    "op": op, "asset": job.borrow_asset, "principal": amount,
                    "client_tag": tag, "intent_id": intent,
                    "expires_utc": _utc_text(job.expires_utc),
                    "operator_token": job.operator_token,
                }, frozenset((job.borrow_asset,)), _utc_now())
                if not isinstance(operation, dict) \
                        or operation.get("verdict") != "READY":
                    raise ValueError
                margin_job = Job(
                    job.job_id, op, job.borrow_asset, leg.base_qty,
                    leg.base_qty, job.created_utc, job.expires_utc,
                    job.requested_by, operator_token=job.operator_token,
                    intent_id=intent, client_tag=tag,
                    margin_operation=operation)
            except Exception:
                row = _reverse_start_leg(
                    receipt, source_path, leg.name, leg.client_id_seed)
                return close_leg(row, reverse_mechanics.FAILED,
                                 "MARGIN_VALIDATION_UNKNOWN")
            row = _reverse_start_leg(
                receipt, source_path, leg.name, leg.client_id_seed,
                dict(operation["params"]))
            outcome = _run_margin_operation(
                margin_job, paths, transport, sha=sha,
                validated_job_digest=validated_job_digest, authority_job=job,
                reverse_entry=leg.name in REVERSE_ENTRY_LEGS,
                asset_whitelist=frozenset((job.borrow_asset,)),
                source_suffix=leg.name.lower(), claim_intent=False)
            source = _load_plain_receipt(outcome.receipt_path)
            if isinstance(source, dict):
                names = (
                    "mutation_attempts", "mutation_started_utc",
                    "history_watermark", "account_liability_before",
                    "submission", "ledger_confirmation", "account_liability",
                    "snapshot_time_note", "authority_checked_utc",
                    "authority_digests", "authority_stop_jobs_present",
                )
                row.update({name: source[name] for name in names if name in source})
            status = reverse_mechanics.DONE_STATUS if outcome.exit_code == 0 \
                else (reverse_mechanics.UNKNOWN
                      if outcome.verdict in FREEZE_VERDICTS
                      else reverse_mechanics.FAILED)
            amounts = (leg.base_qty,) \
                if status == reverse_mechanics.DONE_STATUS else ()
            fills = [{"quantity": amount}] if amounts else []
            return close_leg(row, status, outcome.reason, amounts, fills, {})

        def margin_order(leg):
            side = "SELL" if leg.name == reverse_mechanics.MARGIN_SELL else "BUY"
            price = spot["bid"] if side == "SELL" else spot["ask"]
            ready = margin_orders.build_margin_order(
                job.symbol, side, leg.base_qty, leg.client_id_seed,
                spot["step"], spot["min_notional"], price)
            row = _reverse_start_leg(
                receipt, source_path, leg.name, leg.client_id_seed,
                ready.params if isinstance(
                    ready, margin_orders.MarginOrderReady) else None)
            if not isinstance(ready, margin_orders.MarginOrderReady):
                return close_leg(row, reverse_mechanics.FAILED, str(
                    getattr(ready, "reason", "MARGIN_ORDER_REFUSED")))
            if side == "BUY" and ready.quantity * price > plan.quote_cap:
                return close_leg(row, reverse_mechanics.FAILED,
                                 "BUY_BACK_QUOTE_CAP_EXCEEDED")
            refusal = final_authority(row, leg, ready.params)
            if refusal:
                return close_leg(row, reverse_mechanics.FAILED, refusal)
            row.update({"mutation_attempts": 1,
                        "submitted_utc": _utc_text(_utc_now())})
            _persist_margin_source(source_path, receipt)
            try:
                post = transport(
                    "POST", basis_executor.SPOT.base_url,
                    margin_orders.MARGIN_ORDER_PATH, dict(ready.params), True)
            except Exception:
                post = ("unclear", {})
            post_kind = post[0] if isinstance(post, tuple) and len(post) == 2 \
                else "unreadable"
            parsed: Any = margin_orders.parse_margin_order_response(
                post[1], ready) if post_kind == "ok" \
                and isinstance(post[1], dict) else None
            if not isinstance(parsed, margin_orders.MarginFill):
                parsed = margin_orders.query_margin_order(
                    transport, job.symbol, leg.client_id_seed)
                row["resolved_by_client_id_query"] = True
            if not isinstance(parsed, margin_orders.MarginFill):
                rejected = isinstance(
                    parsed, margin_orders.MarginOrderNotFound
                ) and post_kind == "api_error"
                return close_leg(
                    row, reverse_mechanics.FAILED if rejected
                    else reverse_mechanics.UNKNOWN,
                    "MARGIN_ORDER_REJECTED" if rejected else str(
                        getattr(parsed, "reason", "MARGIN_ORDER_UNKNOWN")))
            amounts = (parsed.executed_qty,) if parsed.executed_qty > 0 else ()
            if parsed.fills is None:
                return close_leg(row, reverse_mechanics.UNKNOWN,
                                 "MARGIN_ORDER_FILL_DATA_UNKNOWN", amounts)
            fills, fees = _reverse_margin_fill_row(parsed)
            trade_fills.extend(fills)
            net = margin_orders.net_base_after_fees(parsed, job.borrow_asset)
            row.update({
                "order_id": parsed.order_id, "exchange_status": parsed.status,
                "executed_qty": _decimal_text(parsed.executed_qty),
                "cum_quote": _decimal_text(parsed.cum_quote),
            })
            if not isinstance(net, Decimal):
                status, reason = (reverse_mechanics.UNKNOWN,
                                  "MARGIN_ORDER_NET_UNKNOWN")
            elif parsed.status != "FILLED" \
                    or parsed.executed_qty != ready.quantity:
                status, reason = (reverse_mechanics.FAILED,
                                  "MARGIN_ORDER_NOT_FULL")
            elif parsed.cum_quote > plan.quote_cap:
                status = reverse_mechanics.DONE_STATUS if side == "BUY" \
                    else reverse_mechanics.FAILED
                reason = "EXIT_QUOTE_CAP_BREACH_CONTAINED" if side == "BUY" \
                    else "QUOTE_CAP_EXCEEDED"
            else:
                status, reason = reverse_mechanics.DONE_STATUS, "FILLED"
            return close_leg(row, status, reason, amounts, fills, fees, net)

        def futures_order(leg):
            try:
                if leg.name == reverse_mechanics.FUTURES_LONG_OPEN:
                    order_plan = dict(hand_plan)
                    notional = leg.base_qty * order_plan["limit_price"]
                    if leg.base_qty > order_plan["quantity"] \
                            or notional < order_plan["min_notional"] \
                            or notional > plan.quote_cap:
                        raise futures_hand.PreflightError(
                            "reverse long quantity is outside its bounds")
                    order_plan["quantity"] = leg.base_qty
                    intent = futures_hand.open_intent(
                        "BUY", order_plan, leg.client_id_seed)
                else:
                    intent = futures_hand.close_intent(
                        "SELL", leg.base_qty, leg.client_id_seed)
            except (ArithmeticError, futures_hand.PreflightError,
                    TypeError, ValueError):
                row = _reverse_start_leg(
                    receipt, source_path, leg.name, leg.client_id_seed)
                return close_leg(row, reverse_mechanics.FAILED,
                                 "FUTURES_ORDER_REFUSED")
            row = _reverse_start_leg(
                receipt, source_path, leg.name, leg.client_id_seed, intent)
            refusal = final_authority(row, leg, intent)
            if refusal:
                return close_leg(row, reverse_mechanics.FAILED, refusal)
            row.update({"mutation_attempts": 1,
                        "submitted_utc": _utc_text(_utc_now())})
            _persist_margin_source(source_path, receipt)
            query, order, response_fills, post_kind = _momentum_submit(intent)
            if query != "terminal":
                rejected = query == "not_found" and post_kind == "api_error"
                return close_leg(
                    row, reverse_mechanics.FAILED if rejected
                    else reverse_mechanics.UNKNOWN,
                    "FUTURES_ORDER_REJECTED" if rejected
                    else "FUTURES_ORDER_UNKNOWN")
            reduce_only = leg.name == reverse_mechanics.FUTURES_CLOSE
            try:
                facts = _momentum_order_facts(
                    order, leg.client_id_seed, reduce_only)
            except MomentumStateError as error:
                return close_leg(
                    row, reverse_mechanics.UNKNOWN, str(error))
            amounts = (facts["qty"],) if facts["qty"] > 0 else ()
            try:
                fills = _momentum_fills(
                    order, response_fills) if amounts else []
            except MomentumStateError as error:
                return close_leg(
                    row, reverse_mechanics.UNKNOWN, str(error), amounts)
            trade_fills.extend(fills)
            fees = _momentum_ledger(
                fills)["realized"]["commission_by_asset"]
            row.update({
                "order_id": facts["order_id"],
                "exchange_status": facts["status"],
                "executed_qty": _decimal_text(facts["qty"]),
                "cum_quote": _decimal_text(facts["quote"]),
                "reduce_only": facts["reduce_only"],
            })
            if facts["status"] != "FILLED" or facts["qty"] != leg.base_qty:
                status, reason = (reverse_mechanics.FAILED,
                                  "FUTURES_ORDER_NOT_FULL")
            elif facts["quote"] > plan.quote_cap:
                status = reverse_mechanics.DONE_STATUS if reduce_only \
                    else reverse_mechanics.FAILED
                reason = "EXIT_QUOTE_CAP_BREACH_CONTAINED" if reduce_only \
                    else "QUOTE_CAP_EXCEEDED"
            else:
                status, reason = reverse_mechanics.DONE_STATUS, "FILLED"
            return close_leg(row, status, reason, amounts, fills, fees)

        ledger: Dict[str, Any] = {}
        attempts: Dict[str, int] = {}
        entry_failure = ""
        entry_completed = intent_claimed = False
        for _step in range(18):
            try:
                truth, detail = exchange_truth()
            except (MomentumStateError, ReverseReadUnknown) as error:
                action = reverse_mechanics.FREEZE(str(error))
                receipt["ladder_actions"].append(
                    _reverse_action_row(action, _utc_now()))
                return finish("UNKNOWN", 4, str(error))
            action = next_action(plan, ledger, truth)
            receipt["ladder_actions"].append(
                _reverse_action_row(action, _utc_now()))
            try:
                _persist_margin_source(source_path, receipt)
            except Exception:
                return Outcome("UNKNOWN", 4, source_path,
                               "REVERSE_RECEIPT_PERSISTENCE_FAILED")
            open_counts = (detail["spot_open_orders"],
                           detail["futures_open_orders"])
            if isinstance(action, reverse_mechanics.Freeze):
                if action.reason in {"TERMINAL_TRUTH_NOT_FLAT",
                                     "RECOVERY_TRUTH_NOT_FLAT"}:
                    return finish("OPEN_EXPOSURE", 3,
                                  _reverse_exposure_reason(
                                      truth, plan, *open_counts))
                return finish("UNKNOWN", 4, action.reason)
            if isinstance(action, reverse_mechanics.DoneAction):
                flat = truth.liability == truth.futures_position == 0 \
                    and abs(truth.base_balance_delta) < plan.dust \
                    and sum(open_counts) == 0
                if not flat:
                    return finish("OPEN_EXPOSURE", 3,
                                  _reverse_exposure_reason(
                                      truth, plan, *open_counts))
                receipt["final_truth"] = {
                    "liability": _canonical_decimal_text(truth.liability),
                    "futures_position": _canonical_decimal_text(
                        truth.futures_position),
                    "base_balance_delta": _canonical_decimal_text(
                        truth.base_balance_delta),
                    "spot_open_orders": 0, "futures_open_orders": 0,
                    "dust": _canonical_decimal_text(plan.dust),
                }
                if entry_failure and not entry_completed:
                    return finish("REFUSED", 2, entry_failure)
                reason = "REVERSE_MECHANICS_CLOSED" if not entry_failure \
                    else "REVERSE_MECHANICS_RECOVERED:" + entry_failure
                return finish("CLOSED", 0, reason)
            leg = action.leg if isinstance(
                action, reverse_mechanics.NextLeg) else action.leg_sequence[0]
            if attempts.get(leg.name):
                standing = action.sequence if isinstance(
                    action, reverse_mechanics.Unwind) else (leg.name,)
                return finish("OPEN_EXPOSURE", 3,
                              "OPEN_EXPOSURE:" + ",".join(standing))
            attempts[leg.name] = 1
            if not intent_claimed:
                try:
                    intent_claimed = _claim_margin_intent(
                        paths, job.intent_id, job.job_id, _utc_now())
                except Exception:
                    return finish(
                        "UNKNOWN", 4,
                        "REVERSE_INTENT_CLAIM_PERSISTENCE_FAILED")
                if not intent_claimed:
                    return finish("REFUSED", 2, "INTENT_ALREADY_CLAIMED")
            if leg.name in {reverse_mechanics.BORROW,
                            reverse_mechanics.REPAY}:
                status, amounts, reason = margin_mutation(leg)
            elif leg.name in {reverse_mechanics.MARGIN_SELL,
                              reverse_mechanics.MARGIN_BUY}:
                status, amounts, reason = margin_order(leg)
            else:
                status, amounts, reason = futures_order(leg)
            receipt["mutation_attempts"] = sum(
                int(row.get("mutation_attempts", 0)) for row in receipt["legs"]
                if isinstance(row, dict))
            ledger_row: Dict[str, Any] = {
                "status": status, "fills": amounts,
                "executed_qty": sum(amounts, Decimal("0")),
            }
            if leg.name in {reverse_mechanics.MARGIN_SELL,
                            reverse_mechanics.MARGIN_BUY}:
                net = receipt["legs"][-1].get("net_base_after_fees")
                if type(net) is str:
                    try:
                        ledger_row["net_base_after_fees"] = Decimal(net)
                    except ArithmeticError:
                        pass
            ledger[leg.name] = ledger_row
            if leg.name in REVERSE_ENTRY_LEGS \
                    and (status == reverse_mechanics.DONE_STATUS or amounts):
                entry_completed = True
            if status == reverse_mechanics.FAILED \
                    and leg.name in REVERSE_ENTRY_LEGS:
                entry_failure = reason
        return finish("UNKNOWN", 4, "REVERSE_LADDER_LIMIT_REACHED")
    finally:
        futures_hand.http = previous_http


def _execute(job: Job, receipt_dir: str, paths: Dict[str, str],
             futures_entry: Optional[Callable[[], Any]],
             basis_entry: Optional[Callable[[Any], Any]],
             now: datetime, governor_token: str = "",
             validated_job_digest: str = "",
             momentum_measurement: Optional[Dict[str, Any]] = None,
             spot_request: Optional[
                 Callable[..., Tuple[str, Any]]
             ] = None,
             margin_request: Optional[
                 Callable[..., Tuple[str, Any]]
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
            job, paths, margin_request, governor_token, sha,
            validated_job_digest,
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
                job, receipt_dir, paths, now, governor_token, spot_request
            )
        if job.op == "MOMENTUM_EXIT":
            return _run_momentum_exit(
                job, receipt_dir, paths, now,
                measurement=momentum_measurement,
                spot_request=spot_request,
            )
        if job.op == "MOMENTUM_ROUNDTRIP":
            blocked = _momentum_roundtrip_allowed(paths)
            if blocked is not None:
                return blocked
        if not _ensure_leverage_1x(job.symbol):
            return Outcome("UNKNOWN", 4, "", "LEVERAGE_UNPROVEN")
        if job.op == REVERSE_OP:
            return _run_reverse_mechanics(
                job, paths, margin_request, governor_token, sha,
                validated_job_digest,
            )
        if job.op == "FUTURES_ROUNDTRIP":
            return _run_futures(receipt_dir, futures_entry)
        if job.op == "MOMENTUM_ROUNDTRIP":
            return _run_futures(receipt_dir, futures_entry, short=False)
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
    if released and op in {margin_ops.MARGIN_BORROW, REVERSE_OP}:
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
    margin_source = os.path.join(
        paths["receipts"], "margin-%s.json" % job_id
    )
    reverse_source = os.path.join(
        paths["receipts"], "reverse-%s.json" % job_id
    )
    allowed_sources = {os.path.abspath(margin_source)}
    if source_path and os.path.abspath(source_path) == os.path.abspath(
            reverse_source):
        allowed_sources.add(os.path.abspath(reverse_source))
    if source_path and os.path.abspath(source_path) not in allowed_sources:
        return False
    expected_source = source_path or margin_source
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
            _margin_account_liability({
                "userAssets": [liability_before]
            }, params["asset"]) == liability_before,
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
            modern_expected = _margin_repay_expected_ledger_confirmation(
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
        request: Optional[Callable[..., Tuple[str, Any]]] = None) -> int:
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
        transport = request or basis_executor.live_request_factory(
            os.environ.get("BINANCE_API_KEY", ""),
            os.environ.get("BINANCE_API_SECRET", ""),
        )
        history_result = transport(
            "GET", basis_executor.SPOT.base_url,
            ready["endpoint_path"], {
                "asset": params["asset"],
                "type": params["type"],
                "txId": transaction_id,
            }, True,
        )
        if type(history_result) is not tuple \
                or len(history_result) != 2 \
                or history_result[0] != "ok":
            return _margin_reconcile_unconfirmed(
                paths, job_id, "MARGIN_CONFIRMATION_UNREADABLE", now
            )
        history_payload = history_result[1]
        if legacy_repay:
            provenance_fact = _legacy_repay_exchange_provenance(
                history_payload, legacy_repay
            )
            if provenance_fact:
                return _margin_reconcile_unconfirmed(
                    paths, job_id, "LEGACY_PROVENANCE_MISMATCH", now,
                    provenance_fact,
                )
            history_confirmation, history_fact_reason = \
                _legacy_repay_history_confirmation(
                    history_payload, transaction_id, expected,
                    liability_before,
                )
            if history_confirmation is None:
                return _margin_reconcile_unconfirmed(
                    paths, job_id, history_fact_reason, now
                )
            history_identifiers = []
        else:
            history_identifiers = _margin_history_transaction_ids(
                history_payload, params["asset"]
            )
            if history_identifiers is None \
                    or _margin_history_confirmation(
                        history_payload, transaction_id, expected,
                        params["type"],
                    ) is None:
                return _margin_reconcile_unconfirmed(
                    paths, job_id, "MARGIN_CONFIRMATION_MISMATCH", now
                )
        account_result = transport(
            "GET", basis_executor.SPOT.base_url,
            MARGIN_ACCOUNT_PATH, {}, True,
        )
        if type(account_result) is not tuple \
                or len(account_result) != 2 \
                or account_result[0] != "ok":
            return _margin_reconcile_unconfirmed(
                paths, job_id, "MARGIN_CONFIRMATION_UNREADABLE", now
            )
        # The exact-id read binds the target row. Only the frozen allowlist,
        # job ledger, and exchange facts may use the four-field REPAY law.
        if legacy_receipt:
            history_reason, _history_after, history_identifiers, \
                legacy_exclusivity_proof = \
                _margin_legacy_exclusivity_snapshot(
                    transport, basis_executor.SPOT.base_url,
                    ready["endpoint_path"], params["asset"], transaction_id,
                    source.get("mutation_started_utc"), now,
                )
        else:
            history_reason, _history_after, history_identifiers = \
                _margin_history_snapshot(
                    transport, basis_executor.SPOT.base_url,
                    ready["endpoint_path"], params["asset"],
                )
        if history_reason:
            return _margin_reconcile_unconfirmed(
                paths, job_id, history_reason, now
            )
    except Exception:
        return _margin_reconcile_unconfirmed(
            paths, job_id, "MARGIN_CONFIRMATION_UNREADABLE", now
        )

    if legacy_repay:
        confirmation = _evaluate_legacy_repay_confirmation(
            history_payload, history_identifiers, transaction_id,
            history_watermark, expected, liability_before,
            account_result[1],
        )
    elif legacy_receipt:
        confirmation = _evaluate_legacy_margin_confirmation(
            history_payload, history_identifiers, transaction_id, expected,
            params["type"], account_result[1], legacy_exclusivity_proof,
        )
    else:
        account_before_payload = {"userAssets": [liability_before]}
        confirmation = _evaluate_margin_confirmation(
            history_payload, history_identifiers, transaction_id,
            history_watermark, expected, params["type"], liability_before,
            account_before_payload, account_result[1],
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
                    _margin_account_liability({
                        "userAssets": [after_liability]
                    }, validated.symbol) == after_liability
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
                    _margin_account_liability({
                        "userAssets": [before_liability]
                    }, validated.symbol) == before_liability
                    and _margin_account_liability({
                        "userAssets": [after_liability]
                    }, validated.symbol) == after_liability
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
                      spot_request: Optional[
                          Callable[..., Tuple[str, Any]]
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
        try:
            venue = _momentum_venue(position)
            if venue == "spot":
                measured = _spot_measure(
                    position, receipt_dir, spot_request
                )
            else:
                _configure_futures_hand(receipt_dir)
                measured = _momentum_measure(position["symbol"])
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
        # _one_way_position rejects negative amounts, so only LONG is reachable.
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
            spot_request=spot_request,
        )
    code = _finish_claimed(
        paths, receipt_dir, job.job_id, job.op, job.symbol, outcome, now
    )
    return code, None, False


def tick(receipt_dir: Optional[str] = None, now: Optional[datetime] = None,
         futures_entry: Optional[Callable[[], Any]] = None,
         basis_entry: Optional[Callable[[Any], Any]] = None,
         spot_request: Optional[
             Callable[..., Tuple[str, Any]]
         ] = None,
         margin_request: Optional[
             Callable[..., Tuple[str, Any]]
         ] = None) -> int:
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
            paths, receipt_dir, result_ids, now, spot_request
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
                paths, receipt_dir, now, spot_request
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
                raw_op = raw.get("op") if type(raw.get("op")) is str else ""
                if raw_op == REVERSE_OP \
                        and os.path.isfile(paths["stop"]):
                    refusal = "STOP_JOBS_SEALED"
                elif raw_op == REVERSE_OP \
                        and not os.path.exists(paths["accept_one"]):
                    refusal = "ENTRY_BLOCKED_NOT_ACCEPTED"
                else:
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
            if job.op in MARGIN_OPS or job.op == REVERSE_OP:
                if _margin_intent_is_claimed(paths, job.intent_id):
                    outcome = Outcome(
                        "REFUSED", 2, "", "INTENT_ALREADY_CLAIMED"
                    )
                    return _finish_claimed(
                        paths, receipt_dir, job.job_id, job.op,
                        job.symbol, outcome, now,
                        validated_job_digest=validated_job_digest,
                    )
            if job.op in {margin_ops.MARGIN_BORROW, REVERSE_OP} \
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
                        job, receipt_dir, paths, futures_entry,
                        basis_entry, now, governor_token,
                        validated_job_digest,
                        momentum_measurement=standing_measurement,
                        spot_request=spot_request,
                        margin_request=margin_request,
                    )
            else:
                outcome = _execute(
                    job, receipt_dir, paths, futures_entry,
                    basis_entry, now,
                    validated_job_digest=validated_job_digest,
                    momentum_measurement=standing_measurement,
                    spot_request=spot_request,
                    margin_request=margin_request,
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
