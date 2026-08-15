"""Pure builders for narrowly bounded Margin borrow and repay requests.

The module validates plain job data and returns plain typed rows.  It performs
no I/O, signing, clock reads, or exchange calls.  The caller supplies both the
current time and a whitelist containing exactly one asset.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, List, Literal, Mapping, Sequence, TypedDict, Union


MARGIN_BORROW = "MARGIN_BORROW"
MARGIN_REPAY = "MARGIN_REPAY"
MARGIN_OPERATIONS = frozenset((MARGIN_BORROW, MARGIN_REPAY))
MARGIN_ENDPOINT_PATH = "/sapi/v1/margin/borrow-repay"
PRINCIPAL_CEILING = Decimal("3")
MAX_EXPIRY_AHEAD = timedelta(minutes=15)
MAX_OPERATOR_TOKEN_LENGTH = 256
MAX_PRINCIPAL_TEXT_LENGTH = 64
CLIENT_TAG_PREFIX = "mrg-"
CLIENT_TAG_HEX_LENGTH = 24

JOB_FIELDS = frozenset((
    "op", "asset", "principal", "client_tag", "expires_utc",
    "operator_token",
))
FORBIDDEN_JOB_FIELDS = frozenset((
    "activationPrice", "callbackRate", "closePosition", "goodTillDate",
    "icebergQty", "isIsolated", "leverage", "marginType",
    "newClientOrderId", "newOrderRespType", "orderId", "positionSide",
    "price", "priceProtect", "quantity", "quoteOrderQty", "reduceOnly",
    "selfTradePreventionMode", "side", "stopPrice", "strategyId",
    "strategyType", "symbol", "timeInForce", "type", "venue",
    "workingType",
))
ASSET_RE = re.compile(r"^[A-Z0-9]{1,20}$")
PRINCIPAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
CLIENT_TAG_RE = re.compile(
    r"^" + re.escape(CLIENT_TAG_PREFIX)
    + r"[0-9a-f]{" + str(CLIENT_TAG_HEX_LENGTH) + r"}$"
)
UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


MarginOperation = Literal["MARGIN_BORROW", "MARGIN_REPAY"]
RequestType = Literal["BORROW", "REPAY"]
LedgerStatus = Literal["BORROW_CONFIRMED", "REPAY_CONFIRMED"]


class MarginRequestParams(TypedDict):
    asset: str
    amount: str
    type: RequestType


class ExpectedLedgerConfirmation(TypedDict):
    asset: str
    principal: str
    interest: str
    status: LedgerStatus


class MarginOperationReady(TypedDict):
    verdict: Literal["READY"]
    op: MarginOperation
    client_tag: str
    expires_utc: str
    endpoint_path: str
    params: MarginRequestParams
    expected_ledger_confirmation: ExpectedLedgerConfirmation
    evidence: List[str]


class MarginOperationRefused(TypedDict):
    verdict: Literal["REFUSED"]
    op: str
    reason: str
    fields: List[str]
    evidence: List[str]


MarginOperationRow = Union[MarginOperationReady, MarginOperationRefused]


class _Problem(ValueError):
    def __init__(self, reason: str, fields: Sequence[str],
                 evidence: Sequence[str] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fields = list(fields)
        self.evidence = list(evidence)


def _refused(job: Any, problem: _Problem) -> MarginOperationRefused:
    op = ""
    if isinstance(job, Mapping) and type(job.get("op")) is str:
        candidate = job["op"]
        if candidate in MARGIN_OPERATIONS:
            op = candidate
    return {
        "verdict": "REFUSED",
        "op": op,
        "reason": problem.reason,
        "fields": list(dict.fromkeys(problem.fields)),
        "evidence": list(dict.fromkeys(problem.evidence)),
    }


def _principal(value: Any) -> tuple[Decimal, str]:
    if type(value) is not str or not value \
            or len(value) > MAX_PRINCIPAL_TEXT_LENGTH \
            or PRINCIPAL_RE.fullmatch(value) is None:
        raise _Problem("BAD_PRINCIPAL", ["job.principal"])
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise _Problem("BAD_PRINCIPAL", ["job.principal"]) from None
    if not parsed.is_finite() or parsed <= Decimal("0"):
        raise _Problem("BAD_PRINCIPAL", ["job.principal"])
    if parsed > PRINCIPAL_CEILING:
        raise _Problem("PRINCIPAL_CEILING_EXCEEDED", ["job.principal"],
                       ["principal_ceiling=3"])
    whole, separator, fraction = value.partition(".")
    fraction = fraction.rstrip("0")
    canonical = whole + ((separator + fraction) if fraction else "")
    return parsed, canonical


def _timestamp(value: Any) -> tuple[datetime, str]:
    if type(value) is not str or UTC_RE.fullmatch(value) is None:
        raise _Problem("BAD_EXPIRY", ["job.expires_utc"])
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise _Problem("BAD_EXPIRY", ["job.expires_utc"]) from None
    canonical = parsed.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    return parsed, canonical


def _single_asset(value: Any) -> str:
    if type(value) is str:
        candidates = (value,)
    elif isinstance(value, (frozenset, list, set, tuple)):
        candidates = tuple(value)
    else:
        raise _Problem("BAD_ASSET_WHITELIST", ["asset_whitelist"])
    if len(candidates) != 1 or type(candidates[0]) is not str \
            or ASSET_RE.fullmatch(candidates[0]) is None:
        raise _Problem("BAD_ASSET_WHITELIST", ["asset_whitelist"])
    return candidates[0]


def deterministic_client_tag(op: str, asset: str, principal: str,
                             expires_utc: str) -> str:
    """Return the stable local identity for one validated operation intent."""
    if type(op) is not str or op not in MARGIN_OPERATIONS:
        raise ValueError("operation is not supported")
    if type(asset) is not str or ASSET_RE.fullmatch(asset) is None:
        raise ValueError("asset is malformed")
    try:
        _amount, canonical_principal = _principal(principal)
        _expiry, canonical_expiry = _timestamp(expires_utc)
    except _Problem as problem:
        raise ValueError(problem.reason) from None
    material = json.dumps(
        [op, asset, canonical_principal, canonical_expiry],
        separators=(",", ":"), ensure_ascii=True,
    )
    digest = hashlib.sha256(material.encode("ascii")).hexdigest()
    return CLIENT_TAG_PREFIX + digest[:CLIENT_TAG_HEX_LENGTH]


def _build(job: Any, asset_whitelist: Any, now: Any,
           expected_op: str | None) -> MarginOperationRow:
    evidence: List[str] = []
    try:
        if not isinstance(job, Mapping):
            raise _Problem("BAD_JOB", ["job"])
        if any(type(key) is not str for key in job):
            raise _Problem("BAD_FIELD_NAME", ["job.<non-text-key>"])
        forbidden = sorted(set(job) & FORBIDDEN_JOB_FIELDS)
        if forbidden:
            raise _Problem(
                "FORBIDDEN_FIELD",
                ["job." + name for name in forbidden],
                ["job.%s.present" % name for name in forbidden],
            )
        unknown = sorted(set(job) - JOB_FIELDS)
        if unknown:
            raise _Problem("UNKNOWN_FIELD",
                           ["job." + name for name in unknown])
        missing = sorted(JOB_FIELDS - set(job))
        if missing:
            raise _Problem("MISSING_FIELD",
                           ["job." + name for name in missing])

        op = job["op"]
        if type(op) is not str or op not in MARGIN_OPERATIONS:
            raise _Problem("BAD_OP", ["job.op"])
        if expected_op is not None and op != expected_op:
            raise _Problem("OP_MISMATCH", ["job.op"])
        evidence.append("job.op")

        allowed_asset = _single_asset(asset_whitelist)
        evidence.append("asset_whitelist.single")
        asset = job["asset"]
        if type(asset) is not str or ASSET_RE.fullmatch(asset) is None:
            raise _Problem("BAD_ASSET", ["job.asset"], evidence)
        if asset != allowed_asset:
            raise _Problem("ASSET_NOT_ALLOWED", ["job.asset"], evidence)
        evidence.append("job.asset")

        _amount, principal = _principal(job["principal"])
        evidence.extend(("job.principal", "principal_ceiling=3"))

        expiry, expires_utc = _timestamp(job["expires_utc"])
        if not isinstance(now, datetime) or now.tzinfo is None \
                or now.utcoffset() is None:
            raise _Problem("BAD_NOW", ["now"], evidence)
        current = now.astimezone(timezone.utc)
        if current >= expiry:
            raise _Problem("EXPIRED", ["job.expires_utc"], evidence)
        if expiry - current > MAX_EXPIRY_AHEAD:
            raise _Problem("EXPIRY_WINDOW_EXCEEDED",
                           ["job.expires_utc"], evidence)
        evidence.append("job.expires_utc")

        operator_token = job["operator_token"]
        if type(operator_token) is not str or not operator_token.strip() \
                or len(operator_token) > MAX_OPERATOR_TOKEN_LENGTH:
            raise _Problem("BAD_OPERATOR_TOKEN", ["job.operator_token"],
                           evidence)
        evidence.append("job.operator_token.present")

        client_tag = job["client_tag"]
        if type(client_tag) is not str \
                or CLIENT_TAG_RE.fullmatch(client_tag) is None:
            raise _Problem("BAD_CLIENT_TAG", ["job.client_tag"], evidence)
        expected_tag = deterministic_client_tag(
            op, asset, principal, expires_utc
        )
        if client_tag != expected_tag:
            raise _Problem("CLIENT_TAG_MISMATCH", ["job.client_tag"],
                           evidence)
        evidence.append("job.client_tag")

        request_type: RequestType = (
            "BORROW" if op == MARGIN_BORROW else "REPAY"
        )
        ledger_status: LedgerStatus = (
            "BORROW_CONFIRMED"
            if op == MARGIN_BORROW else "REPAY_CONFIRMED"
        )
        return {
            "verdict": "READY",
            "op": op,
            "client_tag": client_tag,
            "expires_utc": expires_utc,
            "endpoint_path": MARGIN_ENDPOINT_PATH,
            "params": {
                "asset": asset,
                "amount": principal,
                "type": request_type,
            },
            "expected_ledger_confirmation": {
                "asset": asset,
                "principal": principal,
                "interest": "0",
                "status": ledger_status,
            },
            "evidence": evidence,
        }
    except _Problem as problem:
        return _refused(job, problem)


def build_margin_operation(job: Any, asset_whitelist: Any,
                           now: Any) -> MarginOperationRow:
    """Validate either supported operation and return one factual row."""
    return _build(job, asset_whitelist, now, None)


def build_margin_borrow(job: Any, asset_whitelist: Any,
                        now: Any) -> MarginOperationRow:
    """Validate and shape one Margin borrow operation."""
    return _build(job, asset_whitelist, now, MARGIN_BORROW)


def build_margin_repay(job: Any, asset_whitelist: Any,
                       now: Any) -> MarginOperationRow:
    """Validate and shape one Margin repay operation."""
    return _build(job, asset_whitelist, now, MARGIN_REPAY)
