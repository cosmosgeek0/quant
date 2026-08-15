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
import margin_ops
import set_leverage_1x
import spot_roundtrip


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
MARGIN_GOVERNOR_RECOVERY_FIELDS = {
    "kind", "job_id", "op", "symbol", "verdict", "utc",
}
MARGIN_GOVERNOR_RECOVERY_REFUSAL_FIELDS = {
    "kind", "job_id", "op", "symbol", "reason", "utc",
}
TICK_LOCK_RECOVERY_FIELDS = {
    "kind", "pid", "age_seconds", "utc",
}
TRADING_OPS = {
    "FUTURES_ROUNDTRIP", "BASIS_ROUNDTRIP", "MOMENTUM_ROUNDTRIP",
    "MOMENTUM_OPEN", "MOMENTUM_EXIT",
}
MARGIN_OPS = margin_ops.MARGIN_OPERATIONS
OPS = TRADING_OPS | MARGIN_OPS
ENTRY_OPS = {
    "FUTURES_ROUNDTRIP", "BASIS_ROUNDTRIP", "MOMENTUM_ROUNDTRIP",
    "MOMENTUM_OPEN", margin_ops.MARGIN_BORROW,
}
MOMENTUM_OPS = {"MOMENTUM_ROUNDTRIP", "MOMENTUM_OPEN", "MOMENTUM_EXIT"}
MOMENTUM_STANDING = {"OPENING", "OPEN", "CLOSING", "UNKNOWN"}
MAX_JOB_AGE = timedelta(minutes=15)
# Acceptance clocks may differ by no more than two minutes.
ACCEPT_ONE_FORWARD_SKEW = timedelta(seconds=120)
# The runner is scheduled once per minute; a dead lock gets one full interval.
TICK_INTERVAL_SECONDS = 60.0
MARGIN_RUNNER_NOTIONAL_CAP = Decimal("3")
MARGIN_ASSET_WHITELIST = frozenset(("USDT",))
MARGIN_ENDPOINT_PATH = "/sapi/v1/margin/borrow-repay"
MARGIN_ACCOUNT_PATH = "/sapi/v1/margin/account"
MARGIN_DECIMAL_TEXT_MAX = 64
MARGIN_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
MARGIN_IDENTITY_KEY_RE = re.compile(
    r"^(?:(?:tran(?:saction)?)|tx)[_-]?id$"
)
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
            raise JobRefusal("DUPLICATE_FIELD")
        result[key] = value
    return result


def _decode_strict_object(encoded: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(
            encoded.decode("utf-8"),
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
        )
    except JobRefusal:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise ValueError("JSON object is invalid") from None
    if not isinstance(value, dict):
        raise ValueError("JSON value is not an object")
    return value


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
        owner = json.loads(
            encoded.decode("ascii"), object_pairs_hook=_strict_object
        )
    except (JobRefusal, UnicodeError, ValueError, json.JSONDecodeError):
        raise ValueError("runner lock is invalid") from None
    if not isinstance(owner, dict):
        raise ValueError("runner lock is invalid")
    return owner


def _create_tick_lock(path: str, now: datetime) -> str:
    token = secrets.token_hex(16)
    payload = {
        "pid": os.getpid(),
        "monotonic_ns": time.monotonic_ns(),
        "created_utc": _utc_text(now),
        "token": token,
    }
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    descriptor = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
    )
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_parent_directory(path)
    return token


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
            owner = _decode_tick_lock(encoded)
            if set(owner) != {
                    "pid", "monotonic_ns", "created_utc", "token"} \
                    or type(owner.get("pid")) is not int \
                    or owner["pid"] <= 0 \
                    or type(owner.get("monotonic_ns")) is not int \
                    or owner["monotonic_ns"] < 0 \
                    or type(owner.get("created_utc")) is not str \
                    or type(owner.get("token")) is not str \
                    or not owner["token"]:
                return "", True, None
            created = _timestamp(owner["created_utc"])
        except FileNotFoundError:
            continue
        except (JobRefusal, OSError, ValueError):
            return "", True, None
        try:
            os.kill(owner["pid"], 0)
        except PermissionError:
            return "", False, None
        except ProcessLookupError:
            pass
        except OSError:
            return "", True, None
        else:
            return "", False, None

        current_monotonic = time.monotonic_ns()
        if current_monotonic >= owner["monotonic_ns"]:
            age = Decimal(current_monotonic - owner["monotonic_ns"]) \
                / Decimal("1000000000")
        else:
            age = Decimal(str((now - created).total_seconds()))
        if age < 0:
            return "", True, None
        if age <= Decimal(str(TICK_INTERVAL_SECONDS)):
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


def _result_ids(path: str) -> Tuple[set, bool, bool]:
    if not os.path.exists(path):
        return set(), False, False
    identifiers = set()
    margin_recovery_ids = set()
    margin_recovery_refusal_ids = set()
    unresolved = False
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    return set(), True, False
                row = json.loads(line)
                if not isinstance(row, dict):
                    return set(), True, False
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
                            or not row["pid"].isdigit() \
                            or _margin_decimal_text(
                                row["age_seconds"]
                            ) is None:
                        return set(), True, False
                    continue
                if fields == MARGIN_GOVERNOR_RECOVERY_FIELDS:
                    job_id = row.get("job_id")
                    if row.get("kind") != "margin_governor_release_recovered" \
                            or row.get("op") != margin_ops.MARGIN_BORROW \
                            or type(job_id) is not str \
                            or job_id not in identifiers \
                            or job_id in margin_recovery_ids \
                            or job_id in margin_recovery_refusal_ids \
                            or row.get("verdict") in FREEZE_VERDICTS \
                            or any(type(row.get(name)) is not str for name in (
                                "symbol", "verdict", "utc"
                            )):
                        return set(), True, False
                    margin_recovery_ids.add(job_id)
                    continue
                if fields == MARGIN_GOVERNOR_RECOVERY_REFUSAL_FIELDS:
                    job_id = row.get("job_id")
                    if row.get("kind") \
                            != "margin_governor_recovery_refused" \
                            or row.get("op") != margin_ops.MARGIN_BORROW \
                            or type(job_id) is not str \
                            or job_id not in identifiers \
                            or job_id in margin_recovery_refusal_ids \
                            or job_id in margin_recovery_ids \
                            or any(type(row.get(name)) is not str for name in (
                                "symbol", "reason", "utc"
                            )) \
                            or not row["reason"]:
                        return set(), True, False
                    margin_recovery_refusal_ids.add(job_id)
                    continue
                if fields not in (RESULT_FIELDS, MOMENTUM_RESULT_FIELDS):
                    return set(), True, False
                if type(row["job_id"]) is not str or row["job_id"] in identifiers:
                    return set(), True, False
                if type(row["exit_code"]) is not int:
                    return set(), True, False
                if any(type(row[name]) is not str for name in (
                    "op", "symbol", "verdict", "receipt_path", "utc"
                )):
                    return set(), True, False
                if set(row) == MOMENTUM_RESULT_FIELDS:
                    if row["op"] not in MOMENTUM_OPS \
                            or not isinstance(row["fills"], list) \
                            or not isinstance(row["fees"], list) \
                            or not isinstance(row["realized"], dict):
                        return set(), True, False
                receipt_path = row["receipt_path"]
                if not receipt_path or not os.path.isfile(receipt_path):
                    return set(), True, False
                try:
                    with open(receipt_path, encoding="utf-8") as receipt_handle:
                        receipt = json.load(receipt_handle)
                except (OSError, ValueError, UnicodeError):
                    return set(), True, False
                if not isinstance(receipt, dict) \
                        or receipt.get("verdict") != row["verdict"]:
                    return set(), True, False
                unresolved = unresolved or _receipt_is_unresolved(receipt_path)
                identifiers.add(row["job_id"])
    except (OSError, ValueError, UnicodeError):
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
    try:
        with open(path, encoding="utf-8") as handle:
            receipt = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return True
    if not isinstance(receipt, dict) or type(receipt.get("verdict")) is not str:
        return True
    verdict = receipt["verdict"]
    if verdict == "OPEN_EXPOSURE" and receipt.get("reconciled_by"):
        note = str(receipt.get("reconciled_note", ""))
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


def _write_runner_receipt(paths: Dict[str, str], job_id: str, op: str,
                          symbol: str, verdict: str, reason: str,
                          now: datetime, source_path: str = "",
                          ledger: Optional[Dict[str, Any]] = None,
                          governor_token: str = "") -> str:
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
    line = json.dumps(row, separators=(",", ":")) + "\n"
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
    if not isinstance(account, dict) \
            or not isinstance(account.get("balances"), list) \
            or any(not isinstance(row, dict)
                   for row in account.get("balances", [])):
        raise MomentumStateError("Spot account balances are unreadable")
    rows = account["balances"]
    for asset in assets:
        matches = [row for row in rows if row.get("asset") == asset]
        if len(matches) != 1 or matches[0].get("free") is None:
            raise MomentumStateError(
                "Spot account has no unique free balance for %s" % asset
            )
    try:
        balances = spot_roundtrip.balances_of(account, *assets)
    except (ArithmeticError, KeyError, TypeError, ValueError):
        raise MomentumStateError("Spot account balances are unreadable") \
            from None
    if any(not value.is_finite() or value < 0 for value in balances.values()):
        raise MomentumStateError("Spot account balance is outside its range")
    return balances


def _spot_market_context(state: basis_executor.RunState,
                         effective: Decimal) -> Dict[str, Any]:
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


def _margin_identity_aliases(payload: Any) -> Optional[List[str]]:
    if not isinstance(payload, dict):
        return None
    values: List[str] = []
    stack = [(payload, True)]
    visited = set()
    while stack:
        current, top_level = stack.pop()
        if isinstance(current, dict):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            for name, value in current.items():
                if type(name) is str:
                    normalized = unicodedata.normalize("NFKC", name).casefold()
                    if MARGIN_IDENTITY_KEY_RE.fullmatch(normalized) is not None:
                        if not top_level or name not in {"tranId", "txId"}:
                            return None
                        if type(value) not in (int, str):
                            return None
                        text = str(value)
                        if not text or len(text) > 128:
                            return None
                        values.append(text)
                if isinstance(value, (dict, list, tuple)):
                    stack.append((value, False))
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            for value in current:
                if isinstance(value, (dict, list, tuple)):
                    stack.append((value, False))
    # Exact top-level aliases may mix int and str only when both render to
    # the identical canonical text.
    if len(set(values)) > 1:
        return None
    return values


def _margin_transaction_id(payload: Any) -> str:
    aliases = _margin_identity_aliases(payload)
    if aliases is None or not aliases:
        return ""
    return aliases[0]


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
    if not isinstance(payload, dict) or not isinstance(
            payload.get("rows"), list):
        return None
    matches = []
    for row in payload["rows"]:
        if not isinstance(row, dict):
            continue
        aliases = _margin_identity_aliases(row)
        if aliases is None:
            return None
        if aliases and aliases[0] == transaction_id:
            matches.append(row)
    if len(matches) != 1:
        return None
    row = matches[0]
    if row.get("asset") != expected["asset"]:
        return None
    if "type" not in row or row.get("type") != request_type:
        return None
    principal = _margin_decimal_text(row.get("principal"))
    interest = _margin_decimal_text(row.get("interest"))
    status = row.get("status")
    normalized_status = request_type + "_CONFIRMED" \
        if status == "CONFIRMED" else status
    confirmation = {
        "asset": row["asset"],
        "principal": principal,
        "interest": interest,
        "status": normalized_status,
    }
    if principal is None or interest is None or confirmation != expected:
        return None
    return confirmation


def _margin_account_liability(payload: Any, asset: str) \
        -> Optional[Dict[str, str]]:
    if not isinstance(payload, dict) or not isinstance(
            payload.get("userAssets"), list):
        return None
    matches = [
        row for row in payload["userAssets"]
        if isinstance(row, dict) and row.get("asset") == asset
    ]
    if len(matches) != 1:
        return None
    borrowed = _margin_decimal_text(matches[0].get("borrowed"))
    interest = _margin_decimal_text(matches[0].get("interest"))
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


def _margin_post_authority(
        job: Job, paths: Dict[str, str], operation: Dict[str, Any],
        post_params: Dict[str, Any]) \
        -> Tuple[str, datetime, Dict[str, str], bool]:
    digests: Dict[str, str] = {}
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
        "job_file": _authority_digest(job_encoded),
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
        final_operation = margin_ops.build_margin_operation(
            _margin_job_projection(fresh_raw), MARGIN_ASSET_WHITELIST,
            verified_now,
        )
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
        request: Optional[Callable[..., Tuple[str, Any]]] = None,
        governor_token: str = "", sha: str = "") -> Outcome:
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
            projection, MARGIN_ASSET_WHITELIST, fresh_now
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
        _margin_post_authority(job, paths, operation, post_params)
    receipt["authority_checked_utc"] = _utc_text(authority_now)
    receipt["authority_digests"] = authority_digests
    receipt["authority_stop_jobs_present"] = stop_present
    if authority_refusal:
        return _finish_margin_before_post(
            receipt, source_path, "REFUSED", 2,
            authority_refusal, authority_now,
        )

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
    sealed_repay = job.op == margin_ops.MARGIN_REPAY and stop_present
    if sealed_repay:
        receipt["execution_mode"] = "STOP_JOBS_REPAY"
    receipt["mutation_attempts"] = 1
    receipt["mutation_started_utc"] = _utc_text(authority_now)
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
    post_kind, post_payload = post_result
    transaction_id = _margin_transaction_id(post_payload) \
        if post_kind == "ok" else ""
    receipt["submission"] = {
        "kind": post_kind if type(post_kind) is str else "unreadable",
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

    params = operation["params"]
    history_params = {
        "asset": params["asset"],
        "type": params["type"],
        "txId": transaction_id,
    }
    try:
        history_result = transport(
            "GET", base_url, operation["endpoint_path"],
            history_params, True,
        )
        account_result = transport(
            "GET", base_url, MARGIN_ACCOUNT_PATH, {}, True,
        )
    except Exception:
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_CONFIRMATION_UNREADABLE", fresh_now,
        )
    if type(history_result) is not tuple or len(history_result) != 2 \
            or type(account_result) is not tuple \
            or len(account_result) != 2 \
            or history_result[0] != "ok" or account_result[0] != "ok":
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_CONFIRMATION_UNREADABLE", fresh_now,
        )

    expected = operation["expected_ledger_confirmation"]
    confirmation = _margin_history_confirmation(
        history_result[1], transaction_id, expected, params["type"]
    )
    liability = _margin_account_liability(
        account_result[1], expected["asset"]
    )
    if confirmation is None or liability is None:
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_CONFIRMATION_MISMATCH", fresh_now,
        )
    if job.op == margin_ops.MARGIN_BORROW:
        confirmed = liability["borrowed"] == expected["principal"] \
            and liability["interest"] == expected["interest"]
        verdict = "OPEN"
    else:
        confirmed = liability["borrowed"] == "0" \
            and liability["interest"] == "0" \
            and liability["total_liability"] == "0"
        verdict = "CLOSED"
    if not confirmed:
        return _finish_margin_source(
            receipt, source_path, "UNKNOWN", 4,
            "MARGIN_LIABILITY_MISMATCH", fresh_now,
            confirmation, liability,
        )
    success_reason = expected["status"]
    if sealed_repay:
        success_reason = "REPAY_CONFIRMED_UNDER_STOP_JOBS"
    return _finish_margin_source(
        receipt, source_path, verdict, 0, success_reason, fresh_now,
        confirmation, liability,
    )


def _execute(job: Job, receipt_dir: str, paths: Dict[str, str],
             futures_entry: Optional[Callable[[], Any]],
             basis_entry: Optional[Callable[[Any], Any]],
             now: datetime, governor_token: str = "",
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
            job, paths, margin_request, governor_token, sha
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


def _load_plain_receipt(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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
    targets = [expected_source, runner_path] if source_path else [runner_path]
    for path in targets:
        payload = _load_plain_receipt(path)
        if payload is None or payload.get("job_id") != job_id \
                or payload.get("governor_token") != governor_token:
            return False
        payload["governor_release"] = state
        payload["governor_released_utc"] = _utc_text(now)
        try:
            _persist_json_receipt(path, payload)
        except Exception:
            return False
    return True


def _margin_governor_recovery_rows(paths: Dict[str, str]) \
        -> Tuple[Dict[str, Dict[str, Any]], set, set]:
    final_rows: Dict[str, Dict[str, Any]] = {}
    recovered_ids = set()
    refused_ids = set()
    try:
        with open(paths["results"], encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if not isinstance(row, dict):
                    return {}, set(), set()
                if row.get("kind") == "margin_governor_release_recovered":
                    job_id = row.get("job_id")
                    if type(job_id) is str:
                        recovered_ids.add(job_id)
                    continue
                if row.get("kind") == "margin_governor_recovery_refused":
                    job_id = row.get("job_id")
                    if type(job_id) is str:
                        refused_ids.add(job_id)
                    continue
                if row.get("op") == margin_ops.MARGIN_BORROW \
                        and row.get("verdict") not in FREEZE_VERDICTS \
                        and type(row.get("job_id")) is str:
                    final_rows[row["job_id"]] = row
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return {}, set(), set()
    return final_rows, recovered_ids, refused_ids


def _refuse_margin_governor_recovery(
        paths: Dict[str, str], row: Dict[str, Any], now: datetime,
        already_refused: bool) -> bool:
    if already_refused:
        return False
    event = {
        "kind": "margin_governor_recovery_refused",
        "job_id": str(row.get("job_id", "")),
        "op": margin_ops.MARGIN_BORROW,
        "symbol": str(row.get("symbol", "")),
        "reason": "DURABLE_OWNERSHIP_MISMATCH",
        "utc": _utc_text(now),
    }
    try:
        _append_results_row(paths, event)
    except Exception:
        return False
    print(json.dumps(event, separators=(",", ":")))
    return False


def _recover_final_margin_governor(
        paths: Dict[str, str], receipt_dir: str, now: datetime) \
        -> Optional[bool]:
    final_rows, recovered_ids, refused_ids = \
        _margin_governor_recovery_rows(paths)
    for job_id in sorted(final_rows):
        row = final_rows[job_id]
        runner_path = os.path.join(
            paths["receipts"], "job-%s.json" % job_id
        )
        source_path = os.path.join(
            paths["receipts"], "margin-%s.json" % job_id
        )
        job_path = os.path.join(paths["jobs"], "job-%s.json" % job_id)
        lock_path = os.path.join(receipt_dir, ACCOUNT_GOVERNOR_LOCK_FILE)
        runner = _load_plain_receipt(runner_path)
        source = _load_plain_receipt(source_path)
        pending = (
            isinstance(runner, dict)
            and runner.get("governor_release") == "PENDING"
        ) or (
            isinstance(source, dict)
            and source.get("governor_release") == "PENDING"
        )
        if not pending:
            continue
        lock = _load_plain_receipt(lock_path)
        job = _load_plain_receipt(job_path)
        governor_token = runner.get("governor_token") \
            if isinstance(runner, dict) else None
        ready = source.get("ready") if isinstance(source, dict) else None
        params = ready.get("params") if isinstance(ready, dict) else None
        measured_sha = runner.get("measured_sha") \
            if isinstance(runner, dict) else None
        ownership_agrees = (
            os.path.abspath(str(row.get("receipt_path", "")))
            == os.path.abspath(runner_path)
            and isinstance(runner, dict)
            and isinstance(source, dict)
            and isinstance(lock, dict)
            and isinstance(job, dict)
            and runner.get("job_id") == job_id
            and runner.get("op") == margin_ops.MARGIN_BORROW
            and runner.get("symbol") == row.get("symbol")
            and runner.get("verdict") == row.get("verdict")
            and runner.get("source_receipt_path") == source_path
            and runner.get("governor_release") == "PENDING"
            and type(governor_token) is str
            and bool(governor_token)
            and source.get("kind") == "margin_operation"
            and source.get("job_id") == job_id
            and source.get("verdict") == row.get("verdict")
            and source.get("governor_release") == "PENDING"
            and source.get("governor_token") == governor_token
            and source.get("sha") == runner.get("sha")
            and SHA_RE.fullmatch(str(runner.get("sha", ""))) is not None
            and runner.get("sha_valid") is True
            and type(measured_sha) is str
            and measured_sha.lower() == runner.get("sha")
            and set(lock) == {"version", "token", "op", "symbol", "utc"}
            and lock.get("version") == 1
            and lock.get("token") == governor_token
            and lock.get("op") == margin_ops.MARGIN_BORROW
            and lock.get("symbol") == row.get("symbol")
            and isinstance(ready, dict)
            and ready.get("op") == margin_ops.MARGIN_BORROW
            and isinstance(params, dict)
            and params.get("asset") == row.get("symbol")
            and job.get("job_id") == job_id
            and job.get("op") == margin_ops.MARGIN_BORROW
            and job.get("asset") == row.get("symbol")
            and job.get("intent_id") == ready.get("intent_id")
            and job.get("client_tag") == ready.get("client_tag")
        )
        if not ownership_agrees:
            return _refuse_margin_governor_recovery(
                paths, row, now, job_id in refused_ids
            )
        if not _release_recovered_governor(governor_token, receipt_dir):
            return False
        if os.path.exists(lock_path):
            return False
        if not _mark_margin_governor_released(
                paths, job_id, source_path, governor_token, now, "RECOVERED"):
            return False
        if job_id not in recovered_ids:
            event = {
                "kind": "margin_governor_release_recovered",
                "job_id": job_id,
                "op": margin_ops.MARGIN_BORROW,
                "symbol": row["symbol"],
                "verdict": row["verdict"],
                "utc": _utc_text(now),
            }
            try:
                _append_results_row(paths, event)
            except Exception:
                return False
            print(json.dumps(event, separators=(",", ":")))
        return True
    return None


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
                    governor_token: str = "") -> int:
    if not accept_one_checked:
        outcome = _final_entry_outcome(paths, job_id, op, outcome)
    if outcome.reason or outcome.ledger is not None:
        receipt_path = _write_runner_receipt(
            paths, job_id, op, symbol, outcome.verdict, outcome.reason, now,
            outcome.receipt_path, outcome.ledger, governor_token,
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

    token, stale, stale_recovery = _acquire_tick_lock(
        paths["tick_lock"], now
    )
    if stale:
        _touch_stop(paths["stop"])
        print("JOB-RUNNER | HALTED | unresolved runner lock")
        return 3
    if not token:
        print("JOB-RUNNER | BUSY | another tick is active")
        return 0
    try:
        if stale_recovery is not None:
            try:
                _append_results_row(paths, stale_recovery)
            except Exception:
                _touch_stop(paths["stop"])
                print("JOB-RUNNER | HALTED | stale lock evidence unavailable")
                return 3
            print(json.dumps(stale_recovery, separators=(",", ":")))
        result_ids, bad_results, unresolved_result = _result_ids(paths["results"])
        if bad_results:
            _touch_stop(paths["stop"])
            print("JOB-RUNNER | HALTED | unreconciled receipt or lock")
            return 3
        margin_governor_recovery = _recover_final_margin_governor(
            paths, receipt_dir, now
        )
        if margin_governor_recovery is not None:
            if not margin_governor_recovery:
                _touch_stop(paths["stop"])
                print("JOB-RUNNER | HALTED | governor recovery incomplete")
                return 3
            return 0
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
            try:
                raw = _load_job(path)
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
                    paths, receipt_dir, filename_id, op, symbol, outcome, now
                )
            if job.op in MARGIN_OPS \
                    and _margin_intent_is_claimed(paths, job.intent_id):
                outcome = Outcome(
                    "REFUSED", 2, "", "INTENT_ALREADY_CLAIMED"
                )
                return _finish_claimed(
                    paths, receipt_dir, job.job_id, job.op,
                    job.symbol, outcome, now
                )
            if job.op == margin_ops.MARGIN_BORROW \
                    and os.path.isfile(paths["stop"]):
                outcome = Outcome("REFUSED", 2, "", "STOP_JOBS_SEALED")
                return _finish_claimed(
                    paths, receipt_dir, job.job_id, job.op,
                    job.symbol, outcome, now
                )
            if job.op in ENTRY_OPS:
                refusal = _entry_ticket_refusal(job, paths, now)
                if refusal:
                    outcome = Outcome("REFUSED", 2, "", refusal)
                    return _finish_claimed(
                        paths, receipt_dir, job.job_id, job.op,
                        job.symbol, outcome, now
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
                        momentum_measurement=standing_measurement,
                        spot_request=spot_request,
                        margin_request=margin_request,
                    )
            else:
                outcome = _execute(
                    job, receipt_dir, paths, futures_entry,
                    basis_entry, now,
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
            )
            if governor_token and _entry_outcome_terminal(outcome):
                governor_released = _release_governor(
                    release_governor, governor_token, receipt_dir
                )
                if governor_released and job.op == margin_ops.MARGIN_BORROW:
                    governor_released = _mark_margin_governor_released(
                        paths, job.job_id, outcome.receipt_path,
                        governor_token, now, "RELEASED",
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
    args = parser.parse_args(argv)
    if not args.tick:
        parser.error("--tick is required")
    return tick()


if __name__ == "__main__":
    raise SystemExit(main())
