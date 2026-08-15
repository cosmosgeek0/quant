"""Pure reconciliation of reverse-basis Margin liability and fills.

Inputs are lists of plain dicts: account rows have asset, free, locked,
borrowed, interest, netAsset; orders have id, side, symbol, executedQty,
cummulativeQuoteQty, status and fee fields; ledger rows have asset, principal,
interest and a BORROW[ED|_CONFIRMED] or REPAY[ED|_CONFIRMED] status.  Numeric
fields are decimal strings.  Only FILLED/PARTIALLY_FILLED changes exposure;
base fees adjust the fill.  Missing or ambiguous truth returns UNKNOWN_FREEZE,
never zero.  Each typed verdict dict lists its exact source fields in evidence.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Literal, Mapping, Sequence, TypedDict, Union


ZERO = Decimal("0")
ASSET_RE = re.compile(r"[A-Z0-9]{1,20}")
SYMBOL_RE = re.compile(r"[A-Z0-9]{5,30}")
FILL_STATUSES = frozenset(("FILLED", "PARTIALLY_FILLED"))
WORKING_STATUSES = frozenset(("NEW", "PENDING_NEW", "ACCEPTED"))
ZERO_FILL_STATUSES = WORKING_STATUSES | frozenset((
    "REJECTED", "CANCELED", "CANCELLED", "EXPIRED", "EXPIRED_IN_MATCH"))
BORROW_STATUSES = frozenset(("BORROW", "BORROWED", "BORROW_CONFIRMED"))
REPAY_STATUSES = frozenset(("REPAY", "REPAID", "REPAY_CONFIRMED"))


class ReconciledFlat(TypedDict):
    verdict: Literal["RECONCILED_FLAT"]
    asset: str
    evidence_order_ids: List[str]
    evidence: List[str]


class LiabilityOpen(TypedDict):
    verdict: Literal["LIABILITY_OPEN"]
    asset: str
    borrowed: str
    interest: str
    total_liability: str
    dust: bool
    evidence: List[str]


class PartialUnwind(TypedDict):
    verdict: Literal["PARTIAL_UNWIND"]
    asset: str
    remaining_qty: str
    evidence_order_ids: List[str]
    evidence: List[str]


class UnknownFreeze(TypedDict):
    verdict: Literal["UNKNOWN_FREEZE"]
    reason: str
    missing_evidence: List[str]
    evidence: List[str]


VerdictRow = Union[ReconciledFlat, LiabilityOpen, PartialUnwind, UnknownFreeze]


class _Problem(ValueError):
    def __init__(self, reason: str, missing: Sequence[str], evidence: Sequence[str] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.missing = list(missing)
        self.evidence = list(evidence)


def _unknown(problem: _Problem) -> List[VerdictRow]:
    return [{
        "verdict": "UNKNOWN_FREEZE",
        "reason": problem.reason,
        "missing_evidence": list(dict.fromkeys(problem.missing)),
        "evidence": list(dict.fromkeys(problem.evidence)),
    }]


def _field(row: Mapping[str, Any], key: str, path: str, evidence: List[str]) -> Any:
    if key not in row:
        raise _Problem("required input field is missing", [path], evidence)
    evidence.append(path)
    return row[key]


def _text_field(row: Mapping[str, Any], key: str, path: str, evidence: List[str]) -> str:
    value = _field(row, key, path, evidence)
    if not isinstance(value, str) or not value:
        raise _Problem("required text field is malformed", [path], evidence)
    return value


def _decimal_field(row: Mapping[str, Any], key: str, path: str,
                   evidence: List[str], nonnegative: bool = True) -> Decimal:
    value = _field(row, key, path, evidence)
    if not isinstance(value, str) or not value:
        raise _Problem("decimal field is not an exact string", [path], evidence)
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise _Problem("decimal field is malformed", [path], evidence) from None
    if not parsed.is_finite() or (nonnegative and parsed < ZERO):
        raise _Problem("decimal field is outside its allowed bounds", [path], evidence)
    return parsed


def _rows(value: Any, name: str) -> Sequence[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        raise _Problem("input collection is malformed", [name])
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise _Problem("input row is malformed", ["%s[%d]" % (name, index)])
    return value


def _accounts(raw: Any) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for index, row in enumerate(_rows(raw, "account_snapshot")):
        prefix = "account_snapshot[%d]" % index
        evidence: List[str] = []
        asset = _text_field(row, "asset", prefix + ".asset", evidence)
        if ASSET_RE.fullmatch(asset) is None:
            raise _Problem("account asset is malformed", [prefix + ".asset"], evidence)
        if asset in result:
            raise _Problem("account asset row is duplicated",
                           [prefix + ".asset", result[asset]["asset_path"]],
                           evidence)
        free = _decimal_field(row, "free", prefix + ".free", evidence)
        locked = _decimal_field(row, "locked", prefix + ".locked", evidence)
        borrowed = _decimal_field(row, "borrowed", prefix + ".borrowed", evidence)
        interest = _decimal_field(row, "interest", prefix + ".interest", evidence)
        net_asset = _decimal_field(row, "netAsset", prefix + ".netAsset",
                                   evidence, nonnegative=False)
        calculated = free + locked - borrowed - interest
        if net_asset < calculated or net_asset > calculated:
            raise _Problem("account snapshot arithmetic is contradictory",
                           [prefix + ".netAsset identity"], evidence)
        result[asset] = {
            "asset_path": prefix + ".asset", "borrowed": borrowed,
            "interest": interest, "evidence": evidence,
        }
    return result


def _ledger(raw: Any) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for index, row in enumerate(_rows(raw, "ledger_records")):
        prefix = "ledger_records[%d]" % index
        evidence: List[str] = []
        asset = _text_field(row, "asset", prefix + ".asset", evidence)
        if ASSET_RE.fullmatch(asset) is None:
            raise _Problem("ledger asset is malformed", [prefix + ".asset"], evidence)
        principal = _decimal_field(row, "principal", prefix + ".principal",
                                   evidence)
        interest = _decimal_field(row, "interest", prefix + ".interest",
                                  evidence)
        status = _text_field(row, "status", prefix + ".status", evidence).upper()
        if status not in BORROW_STATUSES | REPAY_STATUSES:
            raise _Problem("ledger status is not a confirmed borrow or repay",
                           [prefix + ".status"], evidence)
        totals = result.setdefault(asset, {
            "borrow_principal": ZERO, "borrow_interest": ZERO,
            "repay_principal": ZERO, "repay_interest": ZERO,
            "borrow_count": 0, "evidence": [],
        })
        if status in BORROW_STATUSES:
            totals["borrow_principal"] += principal
            totals["borrow_interest"] += interest
            totals["borrow_count"] += 1
        else:
            totals["repay_principal"] += principal
            totals["repay_interest"] += interest
        totals["evidence"].extend(evidence)
    return result


def _fee_map(row: Mapping[str, Any], prefix: str, evidence: List[str]) -> Dict[str, Decimal]:
    candidates: List[Dict[str, Decimal]] = []
    if "fee_by_asset" in row:
        raw = _field(row, "fee_by_asset", prefix + ".fee_by_asset", evidence)
        if not isinstance(raw, Mapping):
            raise _Problem("fee map is malformed", [prefix + ".fee_by_asset"], evidence)
        parsed: Dict[str, Decimal] = {}
        for asset, value in raw.items():
            path = prefix + ".fee_by_asset.%s" % asset
            if not isinstance(asset, str) or ASSET_RE.fullmatch(asset) is None:
                raise _Problem("fee asset is malformed", [path], evidence)
            parsed[asset] = _decimal_field({"value": value}, "value", path,
                                           evidence)
        candidates.append(parsed)
    for amount_key, asset_key in (("commission", "commissionAsset"),
                                  ("fee", "feeAsset"),
                                  ("feeQty", "feeAsset")):
        present = amount_key in row or asset_key in row
        if not present:
            continue
        amount = _decimal_field(row, amount_key, prefix + "." + amount_key,
                                evidence)
        asset = _text_field(row, asset_key, prefix + "." + asset_key,
                            evidence)
        if ASSET_RE.fullmatch(asset) is None:
            raise _Problem("fee asset is malformed", [prefix + "." + asset_key],
                           evidence)
        candidates.append({asset: amount})
    if not candidates:
        raise _Problem("fill fee evidence is missing",
                       [prefix + ".fee_by_asset or commission fields"], evidence)
    first = candidates[0]
    if any(candidate != first for candidate in candidates[1:]):
        raise _Problem("fill fee evidence is contradictory",
                       [prefix + ".fee fields agreement"], evidence)
    return first


def _orders(raw: Any, assets: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    totals = {asset: {"opened": ZERO, "covered": ZERO, "ids": [],
                      "working": [], "evidence": [], "fill_count": 0}
              for asset in assets}
    seen_ids = set()
    for index, row in enumerate(_rows(raw, "order_records")):
        prefix = "order_records[%d]" % index
        evidence: List[str] = []
        raw_id = _field(row, "id", prefix + ".id", evidence)
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            raise _Problem("order id is malformed", [prefix + ".id"], evidence)
        order_id = str(raw_id)
        if not order_id or order_id in seen_ids:
            raise _Problem("order id is missing or duplicated",
                           [prefix + ".id uniqueness"], evidence)
        seen_ids.add(order_id)
        side = _text_field(row, "side", prefix + ".side", evidence).upper()
        symbol = _text_field(row, "symbol", prefix + ".symbol", evidence).upper()
        status = _text_field(row, "status", prefix + ".status", evidence).upper()
        if side not in ("BUY", "SELL") or SYMBOL_RE.fullmatch(symbol) is None:
            raise _Problem("order side or symbol is malformed",
                           [prefix + ".side", prefix + ".symbol"], evidence)
        matches = [asset for asset in assets
                   if symbol.startswith(asset) and len(symbol) > len(asset)]
        if len(matches) != 1:
            raise _Problem("order symbol cannot identify exactly one ledger asset",
                           [prefix + ".symbol binding"], evidence)
        asset = matches[0]
        target = totals[asset]
        if status not in FILL_STATUSES:
            if status not in ZERO_FILL_STATUSES:
                raise _Problem("order status is unsupported",
                               [prefix + ".status"], evidence)
            if "executedQty" in row:
                quantity = _decimal_field(row, "executedQty",
                                          prefix + ".executedQty", evidence)
                if quantity > ZERO:
                    raise _Problem("non-fill status carries executed quantity",
                                   [prefix + ".status/executedQty agreement"],
                                   evidence)
            if status in WORKING_STATUSES:
                target["working"].append(order_id)
            target["evidence"].extend(evidence)
            continue
        quantity = _decimal_field(row, "executedQty",
                                  prefix + ".executedQty", evidence)
        quote_key = "cummulativeQuoteQty" if "cummulativeQuoteQty" in row \
            else "cumulativeQuoteQty"
        _decimal_field(row, quote_key, prefix + "." + quote_key, evidence)
        if quantity <= ZERO:
            raise _Problem("fill has no positive executed quantity",
                           [prefix + ".executedQty"], evidence)
        fees = _fee_map(row, prefix, evidence)
        base_fee = fees.get(asset, ZERO)
        if side == "SELL":
            target["opened"] += quantity + base_fee
        else:
            if base_fee > quantity:
                raise _Problem("base fee exceeds BUY fill quantity",
                               [prefix + ".fee fields"], evidence)
            target["covered"] += quantity - base_fee
        target["ids"].append(order_id)
        target["fill_count"] += 1
        target["evidence"].extend(evidence)
    return totals


def reconcile_margin(account_snapshot: Any, order_records: Any, ledger_records: Any,
                     dust_threshold: Decimal = Decimal("0")) \
        -> List[VerdictRow]:
    """Return exact, fail-closed reconciliation verdict rows."""
    try:
        if not isinstance(dust_threshold, Decimal) \
                or not dust_threshold.is_finite() or dust_threshold < ZERO:
            raise _Problem("dust threshold is malformed", ["dust_threshold"])
        accounts = _accounts(account_snapshot)
        ledger = _ledger(ledger_records)
        liability_assets = {
            asset for asset, row in accounts.items()
            if row["borrowed"] + row["interest"] > ZERO
        }
        assets = sorted(set(ledger) | liability_assets)
        if not assets:
            raise _Problem("no asset has affirmative reconciliation evidence",
                           ["ledger_records confirmed asset row"])
        for asset in assets:
            if asset not in accounts:
                raise _Problem("ledger asset has no account snapshot row",
                               ["account_snapshot[%s]" % asset],
                               ledger[asset]["evidence"])
            if asset not in ledger:
                raise _Problem("account liability has no ledger row",
                               ["ledger_records[%s]" % asset],
                               accounts[asset]["evidence"])
            journal = ledger[asset]
            if journal["borrow_count"] < 1:
                raise _Problem("repay ledger has no confirmed borrow origin",
                               ["ledger_records[%s].BORROW" % asset],
                               journal["evidence"])
            outstanding = (journal["borrow_principal"]
                           - journal["repay_principal"])
            if outstanding < ZERO:
                raise _Problem("repaid principal exceeds borrowed principal",
                               ["ledger_records[%s].principal bounds" % asset],
                               journal["evidence"])
            borrowed = accounts[asset]["borrowed"]
            if borrowed < outstanding or borrowed > outstanding:
                raise _Problem("account principal and ledger principal disagree",
                               ["account_snapshot[%s].borrowed ledger bound" % asset],
                               accounts[asset]["evidence"] + journal["evidence"])
            interest_floor = max(
                ZERO, journal["borrow_interest"] - journal["repay_interest"])
            if accounts[asset]["interest"] < interest_floor:
                raise _Problem("account interest is below the ledger lower bound",
                               ["account_snapshot[%s].interest lower bound" % asset],
                               accounts[asset]["evidence"] + journal["evidence"])
        orders = _orders(order_records, assets)
        verdicts: List[VerdictRow] = []
        for asset in assets:
            account = accounts[asset]
            journal = ledger[asset]
            fill = orders[asset]
            evidence = list(dict.fromkeys(
                account["evidence"] + journal["evidence"] + fill["evidence"]))
            if fill["covered"] > fill["opened"]:
                raise _Problem("BUY fills exceed reverse-basis SELL fills",
                               ["order_records[%s] fill-side bounds" % asset],
                               evidence)
            remaining = fill["opened"] - fill["covered"]
            liability = account["borrowed"] + account["interest"]
            if liability > ZERO:
                verdicts.append({
                    "verdict": "LIABILITY_OPEN", "asset": asset,
                    "borrowed": format(account["borrowed"], "f"),
                    "interest": format(account["interest"], "f"),
                    "total_liability": format(liability, "f"),
                    "dust": (account["borrowed"] > ZERO
                             and account["borrowed"] < dust_threshold),
                    "evidence": list(dict.fromkeys(
                        account["evidence"] + journal["evidence"]
                        + ["dust_threshold"])),
                })
            if remaining > ZERO:
                verdicts.append({
                    "verdict": "PARTIAL_UNWIND", "asset": asset,
                    "remaining_qty": format(remaining, "f"),
                    "evidence_order_ids": list(fill["ids"]),
                    "evidence": evidence,
                })
            elif liability <= ZERO:
                if fill["fill_count"] < 1:
                    raise _Problem("flatness has no exchange fill evidence",
                                   ["order_records[%s].reconciled fills" % asset],
                                   evidence)
                if fill["working"]:
                    raise _Problem("working order prevents terminal flatness",
                                   ["order_records[%s].terminal status" % asset],
                                   evidence)
                verdicts.append({
                    "verdict": "RECONCILED_FLAT", "asset": asset,
                    "evidence_order_ids": list(fill["ids"]),
                    "evidence": evidence,
                })
        if not verdicts:
            raise _Problem("inputs do not prove a reconciliation verdict",
                           ["account, ledger, and fill agreement"])
        return verdicts
    except _Problem as problem:
        return _unknown(problem)
