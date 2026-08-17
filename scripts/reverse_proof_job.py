#!/usr/bin/env python3
"""Compose one offline reverse-mechanics proof job for the live runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


OP = "REVERSE_MECHANICS_ROUNDTRIP"
MAX_QUOTE_CAP = Decimal("30")
MAX_EXPIRY_MINUTES = 15
JOB_FIELDS = frozenset((
    "job_id", "op", "symbol", "quote_cap", "borrow_asset",
    "intent_id", "client_tag", "created_utc", "expires_utc",
    "requested_by", "operator_token",
))
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
ASSET_RE = re.compile(r"^[A-Z0-9]{1,20}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class JobRefusal(ValueError):
    """One typed reason why no job record may be emitted."""


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None \
            or value.utcoffset() is None:
        raise JobRefusal("BAD_NOW")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat() \
        .replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime:
    if type(value) is not str or UTC_RE.fullmatch(value) is None:
        raise JobRefusal("BAD_TIMESTAMP")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise JobRefusal("BAD_TIMESTAMP") from None


def _quote_cap(value: Any) -> str:
    if type(value) is not str or DECIMAL_RE.fullmatch(value) is None:
        raise JobRefusal("BAD_QUOTE_CAP")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise JobRefusal("BAD_QUOTE_CAP") from None
    if not amount.is_finite() or amount <= 0:
        raise JobRefusal("BAD_QUOTE_CAP")
    if amount > MAX_QUOTE_CAP:
        raise JobRefusal("QUOTE_CAP_EXCEEDED")
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _operator(path: os.PathLike[str] | str) -> str:
    try:
        encoded = Path(path).read_text(encoding="utf-8")
        value = json.loads(encoded)
    except (OSError, UnicodeError, ValueError):
        raise JobRefusal("OPERATOR_TOKEN_UNREADABLE") from None
    if not isinstance(value, dict) or set(value) != {"operator"}:
        raise JobRefusal("OPERATOR_TOKEN_INVALID")
    operator = value.get("operator")
    if type(operator) is not str or not operator.strip() \
            or operator != operator.strip() or len(operator) > 128:
        raise JobRefusal("OPERATOR_TOKEN_INVALID")
    return operator


def _digest(domain: str, facts: Sequence[str]) -> str:
    material = json.dumps(
        [domain, *facts], separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(material.encode("ascii")).hexdigest()


def _identities(facts: Sequence[str]) -> tuple[str, str, str]:
    job_id = "rproof-" + _digest("reverse-job-v1", facts)[:12]
    intent_id = "reverse-" + _digest("reverse-intent-v1", facts)[:24]
    client_tag = "reverse-client-" \
        + _digest("reverse-client-v1", facts)[:24]
    return job_id, intent_id, client_tag


def validate_job(job: Any, now: datetime) -> Dict[str, str]:
    """Return a copy of one exact runner-shaped job or raise JobRefusal."""
    if not isinstance(job, Mapping):
        raise JobRefusal("BAD_JOB")
    if set(job) != JOB_FIELDS:
        extra = set(job) - JOB_FIELDS
        raise JobRefusal("UNKNOWN_FIELD" if extra else "MISSING_FIELD")
    if any(type(key) is not str or type(value) is not str
           for key, value in job.items()):
        raise JobRefusal("BAD_TYPE")
    if job["op"] != OP:
        raise JobRefusal("BAD_OP")
    if SYMBOL_RE.fullmatch(job["symbol"]) is None:
        raise JobRefusal("BAD_SYMBOL")
    if ASSET_RE.fullmatch(job["borrow_asset"]) is None \
            or job["symbol"] != job["borrow_asset"] + "USDT":
        raise JobRefusal("BAD_BORROW_ASSET")
    quote_cap = _quote_cap(job["quote_cap"])
    if quote_cap != job["quote_cap"]:
        raise JobRefusal("NON_CANONICAL_QUOTE_CAP")
    if not job["requested_by"].strip() or len(job["requested_by"]) > 128 \
            or job["operator_token"] != job["requested_by"]:
        raise JobRefusal("OPERATOR_MISMATCH")
    if len(job["operator_token"]) > 256:
        raise JobRefusal("BAD_OPERATOR_TOKEN")
    created = _parse_utc(job["created_utc"])
    expires = _parse_utc(job["expires_utc"])
    current = _parse_utc(_utc_text(now))
    if created > current or expires <= current:
        raise JobRefusal("EXPIRED_OR_FUTURE_JOB")
    if expires <= created \
            or expires - created > timedelta(minutes=MAX_EXPIRY_MINUTES):
        raise JobRefusal("BAD_EXPIRY_WINDOW")
    facts = (
        OP, job["symbol"], quote_cap, job["borrow_asset"],
        job["created_utc"], job["expires_utc"], job["requested_by"],
    )
    expected = _identities(facts)
    if job["job_id"] != expected[0] \
            or JOB_ID_RE.fullmatch(job["job_id"]) is None:
        raise JobRefusal("BAD_JOB_ID")
    if job["intent_id"] != expected[1] \
            or ID_RE.fullmatch(job["intent_id"]) is None:
        raise JobRefusal("BAD_INTENT_ID")
    if job["client_tag"] != expected[2] \
            or ID_RE.fullmatch(job["client_tag"]) is None:
        raise JobRefusal("BAD_CLIENT_TAG")
    return dict(job)


def build_job(symbol: str, quote_cap: str, borrow_asset: str,
              expiry_minutes: int, operator_token_path: os.PathLike[str] | str,
              now: Optional[datetime] = None) -> Dict[str, str]:
    """Build and validate one deterministic job without performing I/O."""
    if type(expiry_minutes) is not int or isinstance(expiry_minutes, bool) \
            or not 1 <= expiry_minutes <= MAX_EXPIRY_MINUTES:
        raise JobRefusal("BAD_EXPIRY_MINUTES")
    if type(symbol) is not str or SYMBOL_RE.fullmatch(symbol) is None:
        raise JobRefusal("BAD_SYMBOL")
    if type(borrow_asset) is not str \
            or ASSET_RE.fullmatch(borrow_asset) is None \
            or symbol != borrow_asset + "USDT":
        raise JobRefusal("BAD_BORROW_ASSET")
    cap = _quote_cap(quote_cap)
    operator = _operator(operator_token_path)
    created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) \
        .replace(microsecond=0)
    created = _utc_text(created_at)
    expires = _utc_text(created_at + timedelta(minutes=expiry_minutes))
    facts = (OP, symbol, cap, borrow_asset, created, expires, operator)
    job_id, intent_id, client_tag = _identities(facts)
    job = {
        "job_id": job_id, "op": OP, "symbol": symbol,
        "quote_cap": cap, "borrow_asset": borrow_asset,
        "intent_id": intent_id, "client_tag": client_tag,
        "created_utc": created, "expires_utc": expires,
        "requested_by": operator, "operator_token": operator,
    }
    return validate_job(job, created_at)


def write_job(path: os.PathLike[str] | str, job: Mapping[str, str]) -> str:
    encoded = json.dumps(dict(job), separators=(",", ":")) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(os.fspath(path), flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(os.fspath(path))
        except OSError:
            pass
        raise
    os.chmod(os.fspath(path), 0o600)
    return encoded


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--quote-cap", required=True)
    parser.add_argument("--borrow-asset", required=True)
    parser.add_argument("--expiry-minutes", type=int, required=True)
    parser.add_argument("--operator-token-path", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        job = build_job(
            args.symbol, args.quote_cap, args.borrow_asset,
            args.expiry_minutes, args.operator_token_path,
        )
        encoded = write_job(args.output, job)
    except (JobRefusal, OSError) as error:
        reason = str(error) if isinstance(error, JobRefusal) \
            else "OUTPUT_UNAVAILABLE"
        print("REVERSE-PROOF-JOB | REFUSED | " + reason, file=sys.stderr)
        return 2
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
