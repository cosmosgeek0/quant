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
import json
import os
import re
import secrets
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import basis_cycle
import basis_executor
import futures_hand
import limits
import set_leverage_1x


JOB_FIELDS = {
    "job_id",
    "op",
    "symbol",
    "max_notional",
    "created_utc",
    "expires_utc",
    "requested_by",
}
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
OPS = {
    "FUTURES_ROUNDTRIP", "BASIS_ROUNDTRIP", "MOMENTUM_ROUNDTRIP",
    "MOMENTUM_OPEN", "MOMENTUM_EXIT",
}
ENTRY_OPS = OPS - {"MOMENTUM_EXIT"}
MOMENTUM_OPS = {"MOMENTUM_ROUNDTRIP", "MOMENTUM_OPEN", "MOMENTUM_EXIT"}
MOMENTUM_STANDING = {"OPENING", "OPEN", "CLOSING", "UNKNOWN"}
MAX_JOB_AGE = timedelta(minutes=15)
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
    return datetime.now(timezone.utc).replace(microsecond=0)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


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


def _load_job(path: str) -> Dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                parse_float=Decimal,
                parse_int=Decimal,
                parse_constant=_reject_constant,
                object_pairs_hook=_strict_object,
            )
    except JobRefusal:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise JobRefusal("BAD_JSON") from None
    if not isinstance(value, dict):
        raise JobRefusal("BAD_TYPE")
    return value


def _timestamp(value: str) -> datetime:
    if not UTC_RE.fullmatch(value):
        raise JobRefusal("BAD_TIMESTAMP")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise JobRefusal("BAD_TIMESTAMP") from None
    return parsed.astimezone(timezone.utc)


def _load_marker_object(path: str) -> Dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                parse_float=Decimal,
                parse_int=Decimal,
                parse_constant=_reject_constant,
                object_pairs_hook=_strict_object,
            )
    except FileNotFoundError:
        raise
    except (JobRefusal, OSError, UnicodeError, ValueError,
            json.JSONDecodeError):
        raise ValueError("marker JSON is invalid") from None
    if not isinstance(value, dict):
        raise ValueError("marker JSON is not an object")
    return value


def _load_accept_one(path: str, now: datetime) -> Optional[AcceptOne]:
    try:
        raw = _load_marker_object(path)
    except FileNotFoundError:
        return None
    except ValueError:
        raise JobRefusal("ACCEPT_ONE_INVALID") from None
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
        if now >= expires:
            raise ValueError
    except (JobRefusal, ArithmeticError, ValueError):
        raise JobRefusal("ACCEPT_ONE_INVALID") from None
    return AcceptOne(
        operator=raw["operator"],
        created_utc=created,
        expires_utc=expires,
        max_notional=maximum,
    )


def _load_operator(path: str) -> Optional[str]:
    try:
        raw = _load_marker_object(path)
    except (FileNotFoundError, ValueError):
        return None
    if set(raw) != OPERATOR_FIELDS or type(raw.get("operator")) is not str:
        return None
    return raw["operator"]


def _validate_job(raw: Dict[str, Any], filename_id: str,
                  now: datetime, receipt_dir: str = "") -> Job:
    unknown = set(raw) - JOB_FIELDS
    if unknown:
        raise JobRefusal("UNKNOWN_FIELD")
    missing = JOB_FIELDS - set(raw)
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
        "bare_stop": os.path.join(receipt_dir, "STOP"),
        "stop": os.path.join(receipt_dir, "STOP-JOBS"),
        "accept_one": os.path.join(receipt_dir, "ACCEPT-ONE"),
        "operator": os.path.join(receipt_dir, "OPERATOR"),
        "tick_lock": os.path.join(jobs_dir, ".job-runner-active"),
    }


def _make_dirs(paths: Dict[str, str]) -> None:
    for name in ("jobs", "claims", "receipts"):
        os.makedirs(paths[name], mode=0o700, exist_ok=True)


def _touch_stop(path: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)


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


def _acquire_tick_lock(path: str, now: datetime) -> Tuple[str, bool]:
    token = "%s %s %s" % (os.getpid(), _utc_text(now), secrets.token_hex(8))
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, token.encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        return token, False
    except FileExistsError:
        try:
            with open(path, encoding="ascii") as handle:
                pid = int(handle.read().split()[0])
            os.kill(pid, 0)
            return "", False
        except PermissionError:
            return "", False
        except (OSError, ValueError, IndexError):
            return "", True


def _release_tick_lock(path: str, token: str) -> None:
    if not token:
        return
    try:
        with open(path, encoding="ascii") as handle:
            current = handle.read()
        if current == token:
            os.unlink(path)
    except OSError:
        pass


def _result_ids(path: str) -> Tuple[set, bool, bool]:
    if not os.path.exists(path):
        return set(), False, False
    identifiers = set()
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
    os.write(descriptor, body.encode("utf-8"))
    os.fsync(descriptor)
    os.close(descriptor)
    return True


def _write_runner_receipt(paths: Dict[str, str], job_id: str, op: str,
                          symbol: str, verdict: str, reason: str,
                          now: datetime, source_path: str = "",
                          ledger: Optional[Dict[str, Any]] = None) -> str:
    path = os.path.join(paths["receipts"], "job-%s.json" % job_id)
    payload = {
        "job_id": job_id,
        "op": op,
        "symbol": symbol,
        "verdict": verdict,
        "reason": reason,
        "utc": _utc_text(now),
    }
    if source_path:
        payload["source_receipt_path"] = source_path
    if ledger is not None:
        payload.update({
            "fills": list(ledger.get("fills", [])),
            "fees": list(ledger.get("fees", [])),
            "realized": dict(ledger.get("realized", {})),
        })
    temporary = path + ".tmp-%s" % secrets.token_hex(6)
    with open(temporary, "x", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def _append_results_row(paths: Dict[str, str], row: Dict[str, Any]) -> None:
    line = json.dumps(row, separators=(",", ":")) + "\n"
    descriptor = os.open(paths["results"],
                         os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.write(descriptor, line.encode("utf-8"))
    os.fsync(descriptor)
    os.close(descriptor)


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


def _run_momentum_open(job: Job, receipt_dir: str,
                       paths: Dict[str, str], now: datetime,
                       governor_token: str = "") -> Outcome:
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
                       outcome_reason: str = "") -> Outcome:
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
                              result_ids: set, now: datetime) \
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
                    paths, position, query_state, order, [], "recovery", now,
                    recovering=True,
                )
            else:
                outcome = _resolve_momentum_exit(
                    paths, position, query_state, order, [], "recovery", now,
                    recovering=True,
                )
    return job_id, op, position["symbol"], outcome, governor_token


def _execute(job: Job, receipt_dir: str, paths: Dict[str, str],
             futures_entry: Optional[Callable[[], Any]],
             basis_entry: Optional[Callable[[Any], Any]],
             now: datetime, governor_token: str = "",
             momentum_measurement: Optional[Dict[str, Any]] = None) -> Outcome:
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

    values = {
        "SYMBOL": job.symbol,
        "MAX_NOTIONAL": _decimal_text(job.effective_notional),
        "QUANT_SHA": sha,
        "RECEIPT_DIR": receipt_dir,
    }
    with _environment(values):
        if job.op == "MOMENTUM_OPEN":
            return _run_momentum_open(
                job, receipt_dir, paths, now, governor_token
            )
        if job.op == "MOMENTUM_EXIT":
            return _run_momentum_exit(
                job, receipt_dir, paths, now,
                measurement=momentum_measurement,
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


def _release_recovered_governor(token: str, receipt_dir: str) -> bool:
    if not token:
        return True
    try:
        _acquire, release, _record, _budgets, _refusal = \
            _account_governor_api()
    except JobRefusal:
        return False
    return _release_governor(release, token, receipt_dir)


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
                    now: datetime) -> int:
    if op in ENTRY_OPS and _entry_outcome_succeeded(outcome) \
            and not _consume_accept_one(paths["accept_one"], job_id):
        outcome = Outcome(
            "UNKNOWN", 4, outcome.receipt_path,
            "ACCEPT_ONE_CLAIM_FAILED", outcome.ledger,
        )
    if outcome.reason or outcome.ledger is not None:
        receipt_path = _write_runner_receipt(
            paths, job_id, op, symbol, outcome.verdict, outcome.reason, now,
            outcome.receipt_path, outcome.ledger,
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
        if op == "MOMENTUM_EXIT" and published_job:
            proof_path = paths["first_autonomous_close"]
        if proof_path:
            _write_proof_marker(proof_path, job_id, _utc_text(now))
        if op == "MOMENTUM_EXIT":
            _touch_stop(paths["stop"])
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
                      now: datetime) \
        -> Tuple[Optional[int], Optional[Dict[str, Any]], bool]:
    try:
        position = _load_momentum_position(paths)
    except MomentumStateError:
        print("JOB-RUNNER | MAX_ADVERSE_STOP | REFUSED | UNKNOWN_POSITION")
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
        print("JOB-RUNNER | MAX_ADVERSE_STOP | REFUSED | UNKNOWN_CONFIG")
        return 2, None, False
    if not SHA_RE.fullmatch(sha):
        print("JOB-RUNNER | MAX_ADVERSE_STOP | REFUSED | UNKNOWN_CONFIG")
        return 2, None, False

    values = {
        "SYMBOL": position["symbol"],
        "MAX_NOTIONAL": _decimal_text(effective),
        "QUANT_SHA": sha,
        "RECEIPT_DIR": receipt_dir,
    }
    with _environment(values):
        _configure_futures_hand(receipt_dir)
        try:
            measured = _momentum_measure(position["symbol"])
        except Exception as error:
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
            print(
                "JOB-RUNNER | MAX_ADVERSE_STOP | REFUSED | "
                "UNKNOWN_ENTRY_PRICE"
            )
            return 2, None, False
        try:
            current_price = _momentum_decimal(
                measured.get("price"), "current mark price"
            )
        except MomentumStateError as error:
            _append_adverse_stop_event(
                paths, "adverse_stop_refusal", position["symbol"], now,
                type(error).__name__,
            )
            return 2, None, False

        side = entry.get("side", "LONG")
        # _one_way_position rejects negative amounts, so only LONG is reachable.
        if side not in ("LONG", "BUY"):
            print("JOB-RUNNER | MAX_ADVERSE_STOP | REFUSED | UNKNOWN_SIDE")
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
        )
        outcome = _run_momentum_exit(
            job, receipt_dir, paths, now, measurement=measured,
            outcome_reason="max_adverse_stop",
        )
    code = _finish_claimed(
        paths, receipt_dir, job.job_id, job.op, job.symbol, outcome, now
    )
    return code, None, False


def tick(receipt_dir: Optional[str] = None, now: Optional[datetime] = None,
         futures_entry: Optional[Callable[[], Any]] = None,
         basis_entry: Optional[Callable[[Any], Any]] = None) -> int:
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

    token, stale = _acquire_tick_lock(paths["tick_lock"], now)
    if stale:
        _touch_stop(paths["stop"])
        print("JOB-RUNNER | HALTED | unresolved runner lock")
        return 3
    if not token:
        print("JOB-RUNNER | BUSY | another tick is active")
        return 0
    try:
        result_ids, bad_results, unresolved_result = _result_ids(paths["results"])
        if bad_results or unresolved_result \
                or _unresolved_account_state(receipt_dir):
            _touch_stop(paths["stop"])
            print("JOB-RUNNER | HALTED | unreconciled receipt or lock")
            return 3
        recovery = _recover_pending_momentum(
            paths, receipt_dir, result_ids, now
        )
        if recovery is not None:
            job_id, op, symbol, outcome, governor_token = recovery
            code = _finish_claimed(
                paths, receipt_dir, job_id, op, symbol, outcome, now
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
                paths, receipt_dir, now
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
                symbol = _safe_text(raw, "symbol")
                outcome = Outcome("REFUSED", 2, "", refusal)
                return _finish_claimed(
                    paths, receipt_dir, filename_id, op, symbol, outcome, now
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
                    )
            else:
                outcome = _execute(
                    job, receipt_dir, paths, futures_entry,
                    basis_entry, now,
                    momentum_measurement=standing_measurement,
                )
            code = _finish_claimed(
                paths, receipt_dir, job.job_id, job.op, job.symbol,
                outcome, now
            )
            if governor_token and _entry_outcome_terminal(outcome) \
                    and not _release_governor(
                        release_governor, governor_token, receipt_dir
                    ):
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
