#!/usr/bin/env python3
"""Emit EVENT CONTRACT v1 JSONL from a spot-roundtrip receipt.

The receipt is treated as data.  Only contract-v1 fields are selected, event
times come from recorded UTC timestamps, and exchange identifiers are reduced
to their final four characters before serialization.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping


ALLOWED_FIELDS = frozenset(
    {
        "ts",
        "sha8",
        "kind",
        "run_id",
        "symbol",
        "side",
        "client_id_last4",
        "order_id_last4",
        "executed",
        "quote",
        "fees",
        "balances",
        "residual",
        "verdict",
        "note",
    }
)
ALLOWED_KINDS = frozenset(
    {
        "run_start",
        "order_submitted",
        "order_terminal",
        "run_verdict",
        "balance_snapshot",
        "gap_read",
    }
)
BASE_FIELDS = frozenset({"ts", "sha8", "kind", "run_id", "symbol"})
ASSET_RE = re.compile(r"^[A-Z0-9]{1,16}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{8,64}$")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,32}$")
STATE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SECRET_NOTE_RE = re.compile(
    r"(?i)\b(?:api[_ -]?(?:key|secret)|authorization|bearer|password|"
    r"signature|access[_ -]?token|session[_ -]?token)\b"
)
URL_QUERY_RE = re.compile(r"(?i)https?://\S*[?&]\S+")
LONG_ID_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?:[0-9a-fA-F]{10,}|[0-9]{8,})(?![A-Za-z0-9.])"
)
OPAQUE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9._:-])[A-Za-z0-9._:-]{20,}(?![A-Za-z0-9._:-])"
)
KNOWN_QUOTE_ASSETS = (
    "FDUSD",
    "USDT",
    "USDC",
    "TUSD",
    "BUSD",
    "DAI",
    "EUR",
    "GBP",
    "AUD",
    "TRY",
    "BRL",
    "BTC",
    "ETH",
    "BNB",
)


class ReceiptError(ValueError):
    """The receipt cannot be represented safely as contract-v1 events."""


def _mapping(value: Any, field: str, *, required: bool = False) -> Mapping[str, Any]:
    if value is None and not required:
        return {}
    if not isinstance(value, Mapping):
        raise ReceiptError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ReceiptError(f"{field} must be non-empty printable text")
    return value


def _utc(value: Any, field: str) -> str:
    text = _text(value, field)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ReceiptError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ReceiptError(f"{field} must be UTC")
    return parsed.isoformat().replace("+00:00", "Z")


def _amount(value: Any, field: str, *, non_negative: bool = False) -> str:
    if isinstance(value, bool) or value is None:
        raise ReceiptError(f"{field} must be a finite decimal amount")
    text = str(value)
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ReceiptError(f"{field} must be a finite decimal amount") from exc
    if not number.is_finite() or (non_negative and number < 0):
        raise ReceiptError(f"{field} must be a valid decimal amount")
    return text


def _amount_map(value: Any, field: str) -> dict[str, str]:
    source = _mapping(value, field)
    result: dict[str, str] = {}
    for asset, amount in source.items():
        if not isinstance(asset, str) or not ASSET_RE.fullmatch(asset):
            raise ReceiptError(f"{field} contains an invalid asset name")
        result[asset] = _amount(amount, f"{field}.{asset}", non_negative=True)
    return result


def _last4(value: Any, field: str) -> str:
    text = _text(str(value) if value is not None else value, field)
    return text[-4:]


def _known_identifiers(receipt: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[Any] = [receipt.get("buy_client_id"), receipt.get("sell_client_id")]
    for side in ("buy", "sell"):
        values.append(_mapping(receipt.get(side), side).get("order_id"))
    return tuple(str(value) for value in values if value is not None and str(value))


def _safe_note(value: Any, field: str, identifiers: Iterable[str]) -> str:
    note = " ".join(_text(value, field).split())
    for identifier in sorted(set(identifiers), key=len, reverse=True):
        if len(identifier) > 4:
            note = note.replace(identifier, identifier[-4:])
    note = LONG_ID_RE.sub(lambda match: match.group(0)[-4:], note)
    note = OPAQUE_TOKEN_RE.sub(lambda match: match.group(0)[-4:], note)
    if len(note) > 500:
        raise ReceiptError(f"{field} is longer than 500 characters")
    if SECRET_NOTE_RE.search(note) or URL_QUERY_RE.search(note) or "-----BEGIN " in note:
        raise ReceiptError(f"{field} contains secret-like material")
    return note


def _state(value: Any, field: str) -> str:
    state = _text(value, field)
    if not STATE_RE.fullmatch(state):
        raise ReceiptError(f"{field} must be an uppercase factual state")
    return state


def _assets(receipt: Mapping[str, Any], symbol: str) -> tuple[str | None, str | None]:
    explicit_base = receipt.get("base_asset")
    explicit_quote = receipt.get("quote_asset")
    if explicit_base is not None or explicit_quote is not None:
        base = _text(explicit_base, "base_asset")
        quote = _text(explicit_quote, "quote_asset")
        if not ASSET_RE.fullmatch(base) or not ASSET_RE.fullmatch(quote):
            raise ReceiptError("base_asset and quote_asset must be uppercase asset names")
        if base + quote != symbol:
            raise ReceiptError("base_asset and quote_asset do not match symbol")
        return base, quote

    fee_assets: set[str] = set()
    for side in ("buy", "sell"):
        fees = _mapping(_mapping(receipt.get(side), side).get("fee_by_asset"), f"{side}.fee_by_asset")
        fee_assets.update(asset for asset in fees if isinstance(asset, str))
    result_fees = _mapping(
        _mapping(receipt.get("result"), "result").get("commissions"),
        "result.commissions",
    )
    fee_assets.update(asset for asset in result_fees if isinstance(asset, str))
    candidates = [
        (symbol[:split], symbol[split:])
        for split in range(1, len(symbol))
        if symbol[:split] in fee_assets and symbol[split:] in fee_assets
    ]
    if len(candidates) == 1:
        return candidates[0]

    for quote in KNOWN_QUOTE_ASSETS:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return None, None


def _base_event(receipt: Mapping[str, Any], kind: str, ts: Any) -> dict[str, Any]:
    sha = _text(receipt.get("sha"), "sha")
    if not SHA_RE.fullmatch(sha):
        raise ReceiptError("sha must contain 8 to 64 hexadecimal characters")
    run_id = _text(receipt.get("run_id"), "run_id")
    if not IDENTIFIER_RE.fullmatch(run_id):
        raise ReceiptError("run_id contains unsupported characters")
    symbol = _text(receipt.get("symbol"), "symbol")
    if not SYMBOL_RE.fullmatch(symbol):
        raise ReceiptError("symbol must be uppercase letters and digits")
    return {
        "ts": _utc(ts, f"{kind}.ts"),
        "sha8": sha[:8].lower(),
        "kind": kind,
        "run_id": run_id,
        "symbol": symbol,
    }


def _terminal_time(receipt: Mapping[str, Any], side: str, order: Mapping[str, Any]) -> Any:
    for field in ("terminal_utc", "updated_utc"):
        if order.get(field) is not None:
            return order[field]
    if side == "buy":
        sell = _mapping(receipt.get("sell"), "sell")
        if sell.get("submitted_utc") is not None:
            return sell["submitted_utc"]
    return receipt.get("finished_utc", order.get("submitted_utc"))


def events_from_receipt(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Select and validate contract-v1 events from one receipt object."""
    receipt = _mapping(receipt, "receipt", required=True)
    identifiers = _known_identifiers(receipt)
    symbol = _text(receipt.get("symbol"), "symbol")
    base_asset, quote_asset = _assets(receipt, symbol)
    events: list[dict[str, Any]] = []

    started = receipt.get("started_utc")
    start_event = _base_event(receipt, "run_start", started)
    starting_state = _mapping(receipt.get("starting_state"), "starting_state")
    if base_asset and quote_asset and starting_state:
        balances: dict[str, str] = {}
        if starting_state.get("base_free") not in (None, "UNKNOWN"):
            balances[base_asset] = _amount(
                starting_state["base_free"], "starting_state.base_free", non_negative=True
            )
        if starting_state.get("quote_free") not in (None, "UNKNOWN"):
            balances[quote_asset] = _amount(
                starting_state["quote_free"], "starting_state.quote_free", non_negative=True
            )
        if balances:
            start_event["balances"] = balances
    if receipt.get("max_quote") is not None:
        ceiling = _amount(receipt["max_quote"], "max_quote", non_negative=True)
        suffix = f" {quote_asset}" if quote_asset else ""
        start_event["note"] = f"ceiling {ceiling}{suffix}"
    events.append(start_event)

    for side in ("buy", "sell"):
        order = _mapping(receipt.get(side), side)
        if not order:
            continue
        side_upper = side.upper()
        client_id = receipt.get(f"{side}_client_id")
        if order.get("submitted_utc") is not None:
            submitted = _base_event(receipt, "order_submitted", order["submitted_utc"])
            submitted["side"] = side_upper
            if client_id is not None:
                submitted["client_id_last4"] = _last4(client_id, f"{side}_client_id")
            events.append(submitted)

        if order.get("status") is None:
            continue
        terminal = _base_event(receipt, "order_terminal", _terminal_time(receipt, side, order))
        terminal["side"] = side_upper
        if client_id is not None:
            terminal["client_id_last4"] = _last4(client_id, f"{side}_client_id")
        if order.get("order_id") is not None:
            terminal["order_id_last4"] = _last4(order["order_id"], f"{side}.order_id")
        if order.get("executed_qty") is not None:
            terminal["executed"] = _amount(
                order["executed_qty"], f"{side}.executed_qty", non_negative=True
            )
        quote_field = "quote_spent" if side == "buy" else "quote_received"
        if order.get(quote_field) is not None:
            terminal["quote"] = _amount(order[quote_field], f"{side}.{quote_field}", non_negative=True)
        fees = _amount_map(order.get("fee_by_asset"), f"{side}.fee_by_asset")
        if fees:
            terminal["fees"] = fees
        terminal["note"] = _state(order["status"], f"{side}.status")
        events.append(terminal)

    result = _mapping(receipt.get("result"), "result")
    finished = receipt.get("finished_utc")
    if result and finished is not None and base_asset and quote_asset:
        balances = {}
        if result.get("final_base_free") not in (None, "UNKNOWN"):
            balances[base_asset] = _amount(
                result["final_base_free"], "result.final_base_free", non_negative=True
            )
        if result.get("final_quote_free") not in (None, "UNKNOWN"):
            balances[quote_asset] = _amount(
                result["final_quote_free"], "result.final_quote_free", non_negative=True
            )
        if balances:
            snapshot = _base_event(receipt, "balance_snapshot", finished)
            snapshot["balances"] = balances
            events.append(snapshot)

    if receipt.get("verdict") is not None:
        verdict_event = _base_event(receipt, "run_verdict", finished)
        verdict_event["verdict"] = _state(receipt["verdict"], "verdict")
        if result.get("residual_base_from_this_run") not in (None, "UNKNOWN"):
            verdict_event["residual"] = _amount(
                result["residual_base_from_this_run"],
                "result.residual_base_from_this_run",
            )
        notes: list[str] = []
        if result.get("net_quote_cost_incl_fees") not in (None, "UNKNOWN"):
            cost = _amount(result["net_quote_cost_incl_fees"], "result.net_quote_cost_incl_fees")
            suffix = f" {quote_asset}" if quote_asset else ""
            notes.append(f"net quote cost incl fees {cost}{suffix}")
        if receipt.get("verdict_note") is not None:
            notes.append(_safe_note(receipt["verdict_note"], "verdict_note", identifiers))
        if notes:
            verdict_event["note"] = "; ".join(notes)
        events.append(verdict_event)

    for event in events:
        validate_event(event)
    return events


def validate_event(event: Mapping[str, Any]) -> None:
    """Validate one already-selected EVENT CONTRACT v1 object."""
    event = _mapping(event, "event", required=True)
    extra = set(event) - ALLOWED_FIELDS
    missing = BASE_FIELDS - set(event)
    if extra:
        raise ReceiptError("event contains fields outside EVENT CONTRACT v1")
    if missing:
        raise ReceiptError("event is missing required EVENT CONTRACT v1 fields")
    if event["kind"] not in ALLOWED_KINDS:
        raise ReceiptError("event kind is outside EVENT CONTRACT v1")
    if _utc(event["ts"], "event.ts") != event["ts"]:
        raise ReceiptError("event.ts must be canonical UTC ISO")
    if not isinstance(event["sha8"], str) or not re.fullmatch(r"[0-9a-f]{8}", event["sha8"]):
        raise ReceiptError("event.sha8 must be eight lowercase hexadecimal characters")
    if not isinstance(event["run_id"], str) or not IDENTIFIER_RE.fullmatch(event["run_id"]):
        raise ReceiptError("event.run_id contains unsupported characters")
    if not isinstance(event["symbol"], str) or not SYMBOL_RE.fullmatch(event["symbol"]):
        raise ReceiptError("event.symbol must be uppercase letters and digits")
    for field in ("client_id_last4", "order_id_last4"):
        if field in event and (not isinstance(event[field], str) or not 1 <= len(event[field]) <= 4):
            raise ReceiptError(f"event.{field} must contain at most four characters")
    if "side" in event and event["side"] not in ("BUY", "SELL"):
        raise ReceiptError("event.side must be BUY or SELL")
    for field in ("executed", "quote", "residual"):
        if field in event:
            _amount(event[field], f"event.{field}")
    for field in ("fees", "balances"):
        if field in event and _amount_map(event[field], f"event.{field}") != event[field]:
            raise ReceiptError(f"event.{field} must contain string decimal amounts")
    if "note" in event and _safe_note(event["note"], "event.note", ()) != event["note"]:
        raise ReceiptError("event.note must be normalized secret-free text")


def jsonl(events: Iterable[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for event in events:
        validate_event(event)
        lines.append(json.dumps(event, separators=(",", ":"), ensure_ascii=True))
    return "".join(line + "\n" for line in lines)


def write_jsonl(events: Iterable[Mapping[str, Any]], output: str, *, append: bool = False) -> None:
    payload = jsonl(events)
    if output == "-":
        if append:
            raise ReceiptError("--append requires a file output")
        sys.stdout.write(payload)
        return

    path = Path(output)
    if append:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
        return

    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_receipt(path: str) -> Mapping[str, Any]:
    if path == "-":
        value = json.load(sys.stdin)
    else:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    return _mapping(value, "receipt", required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="emit EVENT CONTRACT v1 JSONL from a run receipt")
    parser.add_argument("receipt", help="receipt JSON path, or - for stdin")
    parser.add_argument("-o", "--output", default="-", help="JSONL path, or - for stdout")
    parser.add_argument("--append", action="store_true", help="append atomically-opened lines to output")
    args = parser.parse_args(argv)

    if args.receipt != "-" and args.output != "-" and os.path.abspath(args.receipt) == os.path.abspath(args.output):
        parser.error("receipt and output must be different paths")
    try:
        receipt = _load_receipt(args.receipt)
        write_jsonl(events_from_receipt(receipt), args.output, append=args.append)
    except (OSError, json.JSONDecodeError, ReceiptError) as exc:
        print(f"events.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
