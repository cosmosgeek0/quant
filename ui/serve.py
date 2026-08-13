#!/usr/bin/env python3
"""Read-only localhost runtime for the quant UI. Stdlib only.

Binds 127.0.0.1 exclusively and serves static ui/ files plus factual GETs over
the files written beneath RECEIPT_DIR:

  /api/stream      stream_state.json, ranked by surge plus every basis gap
  /api/candidates  complete, structurally valid, unexpired proposal JSON files
  /api/position    the current basis-position.json and its supervision fields
  /api/momentum    momentum paper state and recent paper receipt records
  /api/fees        commission-rates.json verbatim
  /api/pnl         explicit P&L blocks in CLOSED/reconcile basis receipts
  /api/events      the recorded spot-run fixture, history only
  /api/health      bind, method, and resolved-source facts

Missing sources are honest empty responses. Malformed input is counted, never
repaired. GET and HEAD are the only accepted methods. The server never writes a
file and never calls the network.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

UI_DIR = Path(__file__).resolve().parent
BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8899
STREAM_TOP_N = 20
RECENT_MOMENTUM_RECEIPTS = 50

PROPOSAL_REQUIRED_FIELDS = frozenset({
    "proposal_id", "created_utc", "expires_utc", "quant_sha",
    "config_digest", "symbol", "direction", "spot_ask", "perp_bid",
    "entry_gap_pct", "spot_bid", "perp_ask", "exit_gap_pct",
    "spot_maker_commission_pct", "spot_taker_commission_pct",
    "perp_maker_commission_pct", "perp_taker_commission_pct",
    "commission_source", "entry_slippage_pct", "exit_slippage_pct",
    "funding_rate", "funding_period_ms", "carry_pct",
    "safety_buffer_pct", "net_edge_pct", "quantity",
})
BASIS_RECEIPT_PREFIXES = (
    "basis-executor-", "basis-position-", "basis-reconcile-",
)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


def default_sources():
    """Resolve writer paths at call time without importing sibling modules."""
    receipt_dir = Path(
        os.path.expanduser(os.environ.get("RECEIPT_DIR", "~/quant_receipts"))
    )
    return {
        "events": UI_DIR / "fixtures" / "events.jsonl",
        "stream_state": receipt_dir / "stream_state.json",
        "stream_log": receipt_dir / "stream_log.jsonl",
        "proposals_dir": receipt_dir / "proposals",
        "basis_position": receipt_dir / "basis-position.json",
        "basis_receipts_dir": receipt_dir,
        "fees": receipt_dir / "commission-rates.json",
        "momentum_state": receipt_dir / "momentum-paper-state.json",
        "momentum_receipts": receipt_dir / "momentum-paper-receipts.jsonl",
    }


def _read_text(path):
    """Return (present, text); absent/unreadable sources are never created."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return True, handle.read()
    except (OSError, UnicodeError):
        return False, None


def _read_json(path):
    """Return (present, value, parse_errors)."""
    present, text = _read_text(path)
    if not present:
        return False, None, 0
    try:
        return True, json.loads(text), 0
    except (json.JSONDecodeError, TypeError):
        return True, None, 1


def _read_jsonl(path):
    """Return (present, records, parse_errors), preserving valid line order."""
    present, text = _read_text(path)
    if not present:
        return False, [], 0
    records, parse_errors = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            parse_errors += 1
    return True, records, parse_errors


def _path_as_of(path):
    """UTC mtime for a read source, used only when the record has no clock."""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _record_as_of(record, fallback=None):
    if isinstance(record, dict):
        for key in ("as_of", "updated_utc", "updated_at", "ts",
                    "timestamp", "finished_utc", "closed_utc", "created_utc"):
            if record.get(key) not in (None, ""):
                return record[key]
    return fallback


def _latest_as_of(values, fallback=None):
    usable = [str(value) for value in values if value not in (None, "")]
    return max(usable) if usable else fallback


def _finite_decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _parse_utc(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stream_symbols(payload):
    """Accept the planned symbol->record shape and harmless wrapper variants."""
    if not isinstance(payload, dict):
        return {}
    for key in ("symbols", "tokens"):
        wrapped = payload.get(key)
        if isinstance(wrapped, dict):
            return wrapped
    return {symbol: record for symbol, record in payload.items()
            if isinstance(record, dict)}


def _both_venues(record):
    venues = record.get("venues")
    names = set()
    if isinstance(venues, dict):
        names = {str(name).lower() for name, available in venues.items()
                 if available}
    elif isinstance(venues, (list, tuple, set)):
        names = {str(name).lower() for name in venues}
    elif isinstance(venues, str):
        names = {part.strip().lower() for part in venues.split(",")}
    named_both = "spot" in names and bool(
        names.intersection({"perp", "usdm", "futures"}))
    quoted_both = all(record.get(key) is not None for key in (
        "spot_bid", "spot_ask", "perp_bid", "perp_ask"))
    return named_both or quoted_both


def api_events(sources):
    """Historical spot runs only; this is the one deliberately kept fixture."""
    path = sources["events"]
    present, records, parse_errors = _read_jsonl(path)
    return {
        "source": str(path),
        "present": present,
        "as_of": _path_as_of(path) if present else None,
        "historical": True,
        "count": len(records),
        "events": records,
        "parse_errors": parse_errors,
    }


def api_stream(sources):
    state_path = sources["stream_state"]
    log_path = sources["stream_log"]
    present, state, state_errors = _read_json(state_path)
    log_present, log_records, log_errors = _read_jsonl(log_path)
    symbol_map = _stream_symbols(state)

    source_as_of = _record_as_of(state)
    if source_as_of is None:
        for record in reversed(log_records):
            source_as_of = _record_as_of(record)
            if source_as_of is not None:
                break
    if source_as_of is None and present:
        source_as_of = _path_as_of(state_path)

    ranked = []
    basis_symbols = set()
    for symbol, record in symbol_map.items():
        if not isinstance(record, dict):
            state_errors += 1
            continue
        ratio = _finite_decimal(record.get("surge_ratio"))
        if ratio is not None:
            ranked.append((ratio, str(symbol), record))
        gap = _finite_decimal(record.get("basis_gap_pct"))
        if gap is not None and _both_venues(record):
            basis_symbols.add(str(symbol))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    top_symbols = [symbol for _, symbol, _ in ranked[:STREAM_TOP_N]]
    ordered_symbols = top_symbols + sorted(basis_symbols.difference(top_symbols))

    tokens = []
    for symbol in ordered_symbols:
        record = symbol_map[symbol]
        selection = []
        if symbol in top_symbols:
            selection.append("top_surge")
        if symbol in basis_symbols:
            selection.append("basis_gap")
        tokens.append({
            "symbol": symbol,
            "selection": selection,
            "as_of": _record_as_of(record, source_as_of),
            "data": record,
        })
    return {
        "source": str(state_path),
        "log_source": str(log_path),
        "present": present,
        "log_present": log_present,
        "as_of": source_as_of,
        "top_n": STREAM_TOP_N,
        "count": len(tokens),
        "top_surge_count": len(top_symbols),
        "basis_gap_count": len(basis_symbols),
        "tokens": tokens,
        "log_count": len(log_records),
        "parse_errors": state_errors + log_errors,
        "state_parse_errors": state_errors,
        "log_parse_errors": log_errors,
    }


def _proposal_status(payload, now):
    if not isinstance(payload, dict) \
            or not PROPOSAL_REQUIRED_FIELDS.issubset(payload) \
            or any(payload.get(key) is None for key in PROPOSAL_REQUIRED_FIELDS):
        return "invalid"
    expires = _parse_utc(payload.get("expires_utc"))
    if expires is None:
        return "invalid"
    if expires <= now:
        return "expired"
    net = _finite_decimal(payload.get("net_edge_pct"))
    quantity = _finite_decimal(payload.get("quantity"))
    if net is None or net <= 0 or quantity is None or quantity <= 0:
        return "invalid"
    if not isinstance(payload.get("proposal_id"), str) \
            or not isinstance(payload.get("symbol"), str):
        return "invalid"
    return "valid"


def api_candidates(sources):
    directory = sources["proposals_dir"]
    payload = {
        "source": str(directory), "present": False, "as_of": None,
        "count": 0, "scanned_count": 0, "expired_count": 0,
        "invalid_count": 0, "candidates": [], "parse_errors": 0,
    }
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.endswith(".json"))
    except OSError:
        return payload
    payload["present"] = True
    payload["scanned_count"] = len(names)
    as_of_values = []
    now = datetime.now(timezone.utc)
    for name in names:
        path = directory / name
        present, proposal, errors = _read_json(path)
        file_as_of = _path_as_of(path) if present else None
        as_of_values.append(file_as_of)
        payload["parse_errors"] += errors
        if not present or errors:
            payload["invalid_count"] += 1
            continue
        status = _proposal_status(proposal, now)
        if status == "expired":
            payload["expired_count"] += 1
            continue
        if status != "valid":
            payload["invalid_count"] += 1
            continue
        payload["candidates"].append({
            "file": name,
            "as_of": _record_as_of(proposal, file_as_of),
            "proposal": proposal,
        })
    payload["candidates"].sort(
        key=lambda entry: (-_finite_decimal(
            entry["proposal"]["net_edge_pct"]),
                           entry["proposal"]["symbol"]))
    payload["count"] = len(payload["candidates"])
    payload["as_of"] = _latest_as_of(
        as_of_values, _path_as_of(directory))
    return payload


def _position_supervision(position):
    if not isinstance(position, dict):
        return None
    if "supervision" in position:
        return position["supervision"]
    fields = {}
    for key in ("last_check", "reconcile", "recommendations"):
        if key in position:
            fields[key] = position[key]
    return fields or None


def api_position(sources):
    path = sources["basis_position"]
    present, position, parse_errors = _read_json(path)
    if present and position is not None and not isinstance(position, dict):
        position = None
        parse_errors += 1
    return {
        "source": str(path),
        "present": present,
        "as_of": _record_as_of(position, _path_as_of(path))
        if present else None,
        "position": position,
        "supervision": _position_supervision(position),
        "parse_errors": parse_errors,
    }


def api_momentum(sources):
    state_path = sources["momentum_state"]
    receipts_path = sources["momentum_receipts"]
    state_present, state, state_errors = _read_json(state_path)
    if state_present and state is not None and not isinstance(state, dict):
        state = None
        state_errors += 1
    receipts_present, raw_records, receipt_errors = _read_jsonl(receipts_path)
    records = []
    for record in raw_records:
        if isinstance(record, dict):
            records.append(record)
        else:
            receipt_errors += 1

    positions = {}
    if isinstance(state, dict):
        if isinstance(state.get("positions"), (dict, list)):
            positions = state["positions"]
        elif isinstance(state.get("position"), dict):
            positions = {str(state["position"].get("symbol", "position")):
                         state["position"]}
    state_as_of = _record_as_of(state, _path_as_of(state_path)) \
        if state_present else None
    receipt_as_of = None
    for record in reversed(records):
        receipt_as_of = _record_as_of(record)
        if receipt_as_of is not None:
            break
    if receipt_as_of is None and receipts_present:
        receipt_as_of = _path_as_of(receipts_path)
    recent = records[-RECENT_MOMENTUM_RECEIPTS:]
    return {
        "present": state_present or receipts_present,
        "as_of": _latest_as_of((state_as_of, receipt_as_of)),
        "state": {
            "source": str(state_path), "present": state_present,
            "as_of": state_as_of, "state": state, "positions": positions,
            "parse_errors": state_errors,
        },
        "receipts": {
            "source": str(receipts_path), "present": receipts_present,
            "as_of": receipt_as_of, "count": len(records),
            "recent_count": len(recent), "records": recent,
            "parse_errors": receipt_errors,
        },
        "parse_errors": state_errors + receipt_errors,
    }


def api_fees(sources):
    path = sources["fees"]
    present, rates, parse_errors = _read_json(path)
    if present and rates is not None and not isinstance(rates, dict):
        rates = None
        parse_errors += 1
    return {
        "source": str(path),
        "present": present,
        "as_of": _record_as_of(rates, _path_as_of(path))
        if present else None,
        "rates": rates,
        "parse_errors": parse_errors,
    }


def _pnl_blocks(receipt):
    if not isinstance(receipt, dict):
        return []
    blocks = []
    for key in ("pnl", "realized_pnl"):
        if isinstance(receipt.get(key), dict):
            blocks.append(receipt[key])
    listed = receipt.get("realized_pnl_blocks")
    if isinstance(listed, list):
        blocks.extend(block for block in listed if isinstance(block, dict))
    for parent_key in ("close", "result", "reconcile"):
        parent = receipt.get(parent_key)
        if not isinstance(parent, dict):
            continue
        for key in ("pnl", "realized_pnl"):
            if isinstance(parent.get(key), dict):
                blocks.append(parent[key])
    unique, seen = [], set()
    for block in blocks:
        marker = json.dumps(block, sort_keys=True, default=str)
        if marker not in seen:
            seen.add(marker)
            unique.append(block)
    return unique


def _partial_fee_facts(block):
    keys = (
        "total_usdt_status", "fee_status", "partial_fees", "partial_fee",
        "fees_complete", "excluded_spot_fees_by_asset",
    )
    facts = {key: block[key] for key in keys if key in block}
    text = " ".join(str(value).upper() for value in facts.values())
    partial = bool(facts.get("excluded_spot_fees_by_asset")) \
        or facts.get("partial_fees") is True \
        or facts.get("partial_fee") is True \
        or facts.get("fees_complete") is False \
        or any(word in text for word in ("PARTIAL", "EXCLUDED", "UNKNOWN"))
    return facts, partial


def _is_closed_or_reconcile(receipt):
    verdict = str(receipt.get("verdict", "")).upper()
    state = str(receipt.get("state", "")).upper()
    kind = str(receipt.get("kind", "")).lower()
    return verdict == "CLOSED" or state == "CLOSED" or "reconcile" in kind


def _append_pnl_records(rows, name, receipt, file_as_of):
    if not isinstance(receipt, dict) or not _is_closed_or_reconcile(receipt):
        return
    as_of = _record_as_of(receipt, file_as_of)
    for block in _pnl_blocks(receipt):
        flags, partial = _partial_fee_facts(block)
        rows.append({
            "file": name,
            "run_id": receipt.get("run_id"),
            "symbol": receipt.get("symbol"),
            "kind": receipt.get("kind"),
            "verdict": receipt.get("verdict") or receipt.get("state"),
            "as_of": as_of,
            "pnl": block,
            "partial_fee_flags": flags,
            "partial_fees": partial,
        })


def api_pnl(sources):
    directory = sources["basis_receipts_dir"]
    position_path = sources["basis_position"]
    payload = {
        "source": str(directory), "position_source": str(position_path),
        "present": False, "as_of": None, "count": 0,
        "scanned_receipts": 0, "records": [], "parse_errors": 0,
    }
    as_of_values = []
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.endswith(".json")
                       and name.startswith(BASIS_RECEIPT_PREFIXES))
    except OSError:
        names = []
    else:
        payload["present"] = True
    payload["scanned_receipts"] = len(names)
    for name in names:
        path = directory / name
        present, receipt, errors = _read_json(path)
        file_as_of = _path_as_of(path) if present else None
        as_of_values.append(file_as_of)
        payload["parse_errors"] += errors
        if present and not errors:
            if not isinstance(receipt, dict):
                payload["parse_errors"] += 1
            else:
                _append_pnl_records(
                    payload["records"], name, receipt, file_as_of)

    state_present, position, position_errors = _read_json(position_path)
    if state_present:
        payload["present"] = True
        state_as_of = _path_as_of(position_path)
        as_of_values.append(state_as_of)
        payload["parse_errors"] += position_errors
        if not position_errors:
            if isinstance(position, dict):
                _append_pnl_records(
                    payload["records"], position_path.name, position,
                    state_as_of)
            else:
                payload["parse_errors"] += 1

    payload["records"].sort(
        key=lambda row: (str(row.get("as_of") or ""), row["file"]),
        reverse=True)
    payload["count"] = len(payload["records"])
    payload["as_of"] = _latest_as_of(
        [row.get("as_of") for row in payload["records"]] + as_of_values,
        _path_as_of(directory) if payload["present"] else None)
    return payload


def api_health(sources):
    return {
        "ok": True,
        "read_only": True,
        "bind": BIND_HOST,
        "methods": ["GET", "HEAD"],
        "sources": {name: str(path) for name, path in sources.items()},
    }


API_ROUTES = {
    "/api/health": api_health,
    "/api/events": api_events,
    "/api/stream": api_stream,
    "/api/candidates": api_candidates,
    "/api/position": api_position,
    "/api/momentum": api_momentum,
    "/api/fees": api_fees,
    "/api/pnl": api_pnl,
}


def resolve_static(url_path):
    """Map a URL path to a file inside ui/, or None. Traversal is refused."""
    clean = unquote(urlsplit(url_path).path)
    if clean in ("", "/"):
        clean = "/index.html"
    parts = [part for part in clean.split("/") if part]
    if any(part in ("..", ".") for part in parts):
        return None
    candidate = UI_DIR.joinpath(*parts)
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if UI_DIR not in resolved.parents and resolved != UI_DIR:
        return None
    if not resolved.is_file():
        return None
    return resolved


class ReadOnlyHandler(BaseHTTPRequestHandler):
    server_version = "QuantReadOnlyUI/2"
    sources = None

    def _send(self, status, content_type, body, head_only=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_json(self, status, payload, head_only=False):
        body = json.dumps(payload, indent=1).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body, head_only)

    def _handle(self, head_only):
        path = urlsplit(self.path).path
        route = API_ROUTES.get(path)
        if route is not None:
            self._send_json(200, route(type(self).sources), head_only)
            return
        static = resolve_static(self.path)
        if static is None:
            self._send_json(404, {"error": "not found", "path": path}, head_only)
            return
        content_type = CONTENT_TYPES.get(
            static.suffix, "application/octet-stream")
        with open(static, "rb") as handle:
            self._send(200, content_type, handle.read(), head_only)

    def do_GET(self):
        self._handle(head_only=False)

    def do_HEAD(self):
        self._handle(head_only=True)

    def _refuse(self):
        self._send_json(405, {
            "error": "read-only server: GET and HEAD only; "
                     "no mutating endpoint exists",
            "method": self.command,
        })

    do_POST = _refuse
    do_PUT = _refuse
    do_DELETE = _refuse
    do_PATCH = _refuse
    do_OPTIONS = _refuse
    do_CONNECT = _refuse
    do_TRACE = _refuse

    def log_message(self, format, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def make_server(sources=None, port=DEFAULT_PORT):
    """Build the loopback-only server; the caller owns its lifecycle."""
    resolved = dict(default_sources())
    if sources:
        resolved.update(sources)
    handler = type("BoundHandler", (ReadOnlyHandler,), {"sources": resolved})
    return ThreadingHTTPServer((BIND_HOST, port), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="port on 127.0.0.1 (0 = OS-assigned, printed)")
    parser.add_argument("--events-file", type=Path, default=None,
                        help="historical spot event JSONL")
    parser.add_argument("--stream-state", type=Path, default=None)
    parser.add_argument("--stream-log", type=Path, default=None)
    parser.add_argument("--proposals-dir", type=Path, default=None)
    parser.add_argument("--basis-position", type=Path, default=None)
    parser.add_argument("--basis-receipts-dir", type=Path, default=None)
    parser.add_argument("--fees-file", type=Path, default=None)
    parser.add_argument("--momentum-state", type=Path, default=None)
    parser.add_argument("--momentum-receipts", type=Path, default=None)
    args = parser.parse_args(argv)

    overrides = {}
    for argument, source in (
            (args.events_file, "events"),
            (args.stream_state, "stream_state"),
            (args.stream_log, "stream_log"),
            (args.proposals_dir, "proposals_dir"),
            (args.basis_position, "basis_position"),
            (args.basis_receipts_dir, "basis_receipts_dir"),
            (args.fees_file, "fees"),
            (args.momentum_state, "momentum_state"),
            (args.momentum_receipts, "momentum_receipts")):
        if argument is not None:
            overrides[source] = argument

    server = make_server(overrides, args.port)
    host, port = server.server_address[0], server.server_address[1]
    print("serving quant ui (read-only, GET/HEAD only) on http://%s:%d/"
          % (host, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
