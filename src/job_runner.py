#!/usr/bin/env python3
"""Consume at most one strictly bounded live-mechanics job per tick.

Jobs are immutable-by-convention JSON files in ``RECEIPT_DIR/jobs``.  A
persistent O_EXCL claim makes every job id single-use.  The runner never
transfers funds and exposes only the three compiled round-trip operations.
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
from typing import Any, Callable, Dict, Optional, Tuple

import basis_cycle
import basis_executor
import futures_hand
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
RESULT_FIELDS = {
    "job_id", "op", "symbol", "verdict", "exit_code", "receipt_path", "utc"
}
OPS = {"FUTURES_ROUNDTRIP", "BASIS_ROUNDTRIP", "MOMENTUM_ROUNDTRIP"}
HARD_MAX_NOTIONAL = Decimal("8")
MAX_JOB_AGE = timedelta(minutes=15)
JOB_NAME_RE = re.compile(r"^job-([A-Za-z0-9][A-Za-z0-9_-]{0,63})\.json$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
FREEZE_VERDICTS = {"UNKNOWN", "OPEN_EXPOSURE", "BLOCKED_UNRESOLVED"}
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
class Outcome:
    verdict: str
    exit_code: int
    receipt_path: str
    reason: str = ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


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


def _validate_job(raw: Dict[str, Any], filename_id: str,
                  now: datetime) -> Job:
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
    return Job(
        job_id=raw["job_id"],
        op=raw["op"],
        symbol=raw["symbol"],
        requested_max_notional=requested,
        effective_notional=min(requested, HARD_MAX_NOTIONAL),
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
        "stop": os.path.join(receipt_dir, "STOP-JOBS"),
        "tick_lock": os.path.join(jobs_dir, ".job-runner-active"),
    }


def _make_dirs(paths: Dict[str, str]) -> None:
    for name in ("jobs", "claims", "receipts"):
        os.makedirs(paths[name], mode=0o700, exist_ok=True)


def _touch_stop(path: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)


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
                if not isinstance(row, dict) or set(row) != RESULT_FIELDS:
                    return set(), True, False
                if type(row["job_id"]) is not str or row["job_id"] in identifiers:
                    return set(), True, False
                if type(row["exit_code"]) is not int:
                    return set(), True, False
                if any(type(row[name]) is not str for name in (
                    "op", "symbol", "verdict", "receipt_path", "utc"
                )):
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
                          now: datetime, source_path: str = "") -> str:
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
    temporary = path + ".tmp-%s" % secrets.token_hex(6)
    with open(temporary, "x", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


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
    line = json.dumps(row, separators=(",", ":")) + "\n"
    descriptor = os.open(paths["results"],
                         os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.write(descriptor, line.encode("utf-8"))
    os.fsync(descriptor)
    os.close(descriptor)


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


def _execute(job: Job, receipt_dir: str, paths: Dict[str, str],
             futures_entry: Optional[Callable[[], Any]],
             basis_entry: Optional[Callable[[Any], Any]]) -> Outcome:
    sha = os.environ.get("QUANT_SHA", "").lower()
    if not SHA_RE.fullmatch(sha):
        return Outcome("REFUSED", 2, "", "BAD_QUANT_SHA")
    if not os.environ.get("BINANCE_API_KEY") or not os.environ.get(
            "BINANCE_API_SECRET"):
        return Outcome("REFUSED", 2, "", "MISSING_CREDENTIALS")
    if job.op == "BASIS_ROUNDTRIP" and not os.path.isfile(paths["proof"]):
        return Outcome("REFUSED", 2, "", "SEQUENCE_LOCKED")
    if job.op == "MOMENTUM_ROUNDTRIP":
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
        if not _ensure_leverage_1x(job.symbol):
            return Outcome("UNKNOWN", 4, "", "LEVERAGE_UNPROVEN")
        if job.op == "FUTURES_ROUNDTRIP":
            return _run_futures(receipt_dir, futures_entry)
        if job.op == "MOMENTUM_ROUNDTRIP":
            return _run_futures(receipt_dir, futures_entry, short=False)
        return _run_basis(receipt_dir, job.effective_notional, basis_entry)


def _finish_claimed(paths: Dict[str, str], receipt_dir: str, job_id: str,
                    op: str, symbol: str, outcome: Outcome,
                    now: datetime) -> int:
    if outcome.reason:
        receipt_path = _write_runner_receipt(
            paths, job_id, op, symbol, outcome.verdict, outcome.reason, now,
            outcome.receipt_path,
        )
        outcome = Outcome(outcome.verdict, outcome.exit_code, receipt_path)
    if outcome.verdict == "CLOSED":
        proof_path = {
            "FUTURES_ROUNDTRIP": paths["proof"],
            "BASIS_ROUNDTRIP": paths["basis_proof"],
        }.get(op)
        if proof_path:
            _write_proof_marker(proof_path, job_id, _utc_text(now))
    if outcome.verdict in FREEZE_VERDICTS:
        _touch_stop(paths["stop"])
    _append_result(paths, job_id, op, symbol, outcome, now)
    print("JOB | %s | %s | %s | exit %s" % (
        job_id, op or "INVALID", outcome.verdict, outcome.exit_code
    ))
    return outcome.exit_code


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
    if os.path.exists(paths["stop"]):
        print("JOB-RUNNER | HALTED | STOP-JOBS present")
        return 0

    token, stale = _acquire_tick_lock(paths["tick_lock"], now)
    if stale:
        _touch_stop(paths["stop"])
        print("JOB-RUNNER | HALTED | unresolved runner lock")
        return 3
    if not token:
        print("JOB-RUNNER | BUSY | another tick is active")
        return 0
    try:
        if os.path.exists(paths["stop"]):
            print("JOB-RUNNER | HALTED | STOP-JOBS present")
            return 0
        result_ids, bad_results, unresolved_result = _result_ids(paths["results"])
        if bad_results or unresolved_result \
                or not _claims_are_resolved(paths, result_ids) \
                or _unresolved_account_state(receipt_dir):
            _touch_stop(paths["stop"])
            print("JOB-RUNNER | HALTED | unreconciled receipt or lock")
            return 3

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
                job = _validate_job(raw, filename_id, now)
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
            outcome = _execute(
                job, receipt_dir, paths, futures_entry, basis_entry
            )
            return _finish_claimed(
                paths, receipt_dir, job.job_id, job.op, job.symbol, outcome, now
            )
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
