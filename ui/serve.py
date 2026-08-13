#!/usr/bin/env python3
"""Read-only localhost runtime for the quant UI. Stdlib only.

Binds 127.0.0.1 exclusively; the port is configurable (--port, 0 lets the OS
pick and the chosen port is printed). Serves the static ui/ files plus
read-only GET endpoints over the ACTUAL output files the quant writes:

  /api/health   server facts: bind, read-only flag, resolved source paths
  /api/events   contract-v1 events. ui/fixtures/events.jsonl is real recorded
                data and remains the spot source until host outputs are wired;
                --events-file re-points this endpoint when they are.
  /api/basis    basis_read printed output as recorded (default: the evidence
                capture of the live public read)
  /api/futures  futures-hand-*.json receipts from RECEIPT_DIR
                (default ~/quant_receipts, same as src/futures_hand.py)
  /api/volume   volume paper receipts (paper_receipts.jsonl, same default as
                src/volume_observer.py) and the volume state file if present

Honesty rules: a missing source answers {"present": false} with empty rows —
never fabricated data, never an invented placeholder row. Unparseable lines
are counted in parse_errors, not silently repaired. GET and HEAD are the only
methods; everything else answers 405. This process never creates, writes,
deletes, or renames any file, and it never calls the network.
"""

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

UI_DIR = Path(__file__).resolve().parent
REPO_DIR = UI_DIR.parent
BIND_HOST = "127.0.0.1"  # localhost only; deliberately not configurable
DEFAULT_PORT = 8899

FUTURES_RECEIPT_PREFIX = "futures-hand-"   # exact convention of futures_hand.py
FUTURES_RECEIPT_SUFFIX = ".json"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


def default_sources():
    """Where the quant actually writes today, resolved at call time."""
    futures_dir = Path(
        os.path.expanduser(os.environ.get("RECEIPT_DIR", "~/quant_receipts"))
    )
    return {
        "events": UI_DIR / "fixtures" / "events.jsonl",
        "basis": REPO_DIR / "evidence" / "basis-read-live-run.txt",
        "futures_dir": futures_dir,
        "volume_receipts": REPO_DIR / "paper_receipts.jsonl",
        "volume_state": REPO_DIR / "volume_state.json",
    }


def _read_text(path):
    """(present, text). present is True only when the file was actually read."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return True, handle.read()
    except OSError:
        return False, None


def _read_jsonl(path):
    """(present, records, parse_errors); absent -> (False, [], 0)."""
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


def api_events(sources):
    path = sources["events"]
    present, records, parse_errors = _read_jsonl(path)
    return {
        "source": str(path),
        "present": present,
        "count": len(records),
        "events": records,
        "parse_errors": parse_errors,
    }


def api_basis(sources):
    path = sources["basis"]
    present, text = _read_text(path)
    payload = {
        "source": str(path),
        "present": present,
        "text": text,
        "formulas": [],
        "columns": [],
        "rows": [],
        "parse_errors": 0,
    }
    if not present:
        return payload
    tab_lines = []
    for line in text.splitlines():
        if "\t" in line:
            tab_lines.append(line)
        elif "=" in line and line.strip():
            payload["formulas"].append(line.strip())
    if tab_lines:
        payload["columns"] = tab_lines[0].split("\t")
        width = len(payload["columns"])
        for line in tab_lines[1:]:
            values = line.split("\t")
            if len(values) == width:
                payload["rows"].append(dict(zip(payload["columns"], values)))
            else:
                payload["parse_errors"] += 1
    return payload


def api_futures(sources):
    directory = sources["futures_dir"]
    payload = {
        "source": str(directory),
        "present": False,
        "count": 0,
        "receipts": [],
        "parse_errors": 0,
    }
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return payload  # absent dir stays absent; never created here
    payload["present"] = True
    for name in names:
        if not (name.startswith(FUTURES_RECEIPT_PREFIX)
                and name.endswith(FUTURES_RECEIPT_SUFFIX)):
            continue
        payload["count"] += 1
        entry = {"file": name, "receipt": None}
        try:
            with open(directory / name, "r", encoding="utf-8") as handle:
                entry["receipt"] = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            entry["error"] = type(error).__name__
            payload["parse_errors"] += 1
        payload["receipts"].append(entry)
    return payload


def api_volume(sources):
    receipts_path = sources["volume_receipts"]
    present, records, parse_errors = _read_jsonl(receipts_path)
    state_path = sources["volume_state"]
    state_present, state_text = _read_text(state_path)
    state_obj, state_errors = None, 0
    if state_present:
        try:
            state_obj = json.loads(state_text)
        except json.JSONDecodeError:
            state_errors = 1
    return {
        "receipts": {
            "source": str(receipts_path),
            "present": present,
            "count": len(records),
            "records": records,
            "parse_errors": parse_errors,
        },
        "state": {
            "source": str(state_path),
            "present": state_present,
            "state": state_obj,
            "parse_errors": state_errors,
        },
    }


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
    "/api/basis": api_basis,
    "/api/futures": api_futures,
    "/api/volume": api_volume,
}


def resolve_static(url_path):
    """Map a URL path to a file inside ui/, or None. Traversal is refused."""
    clean = unquote(urlsplit(url_path).path)
    if clean in ("", "/"):
        clean = "/index.html"
    parts = [p for p in clean.split("/") if p]
    if any(p in ("..", ".") for p in parts):
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
    server_version = "QuantReadOnlyUI/1"
    sources = None  # set by make_server on the subclass

    # -- responses ---------------------------------------------------------
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

    def log_message(self, format, *args):  # quiet by default; facts on stderr
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def make_server(sources=None, port=DEFAULT_PORT):
    """Build the bound server (127.0.0.1 only). Caller runs serve_forever."""
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
                        help="contract-v1 events jsonl (spot source)")
    parser.add_argument("--basis-file", type=Path, default=None,
                        help="recorded basis_read output text")
    parser.add_argument("--futures-dir", type=Path, default=None,
                        help="futures receipts dir (default RECEIPT_DIR "
                             "or ~/quant_receipts)")
    parser.add_argument("--volume-receipts", type=Path, default=None,
                        help="volume paper receipts jsonl")
    parser.add_argument("--volume-state", type=Path, default=None,
                        help="volume state json (served if present)")
    args = parser.parse_args(argv)

    overrides = {}
    if args.events_file is not None:
        overrides["events"] = args.events_file
    if args.basis_file is not None:
        overrides["basis"] = args.basis_file
    if args.futures_dir is not None:
        overrides["futures_dir"] = args.futures_dir
    if args.volume_receipts is not None:
        overrides["volume_receipts"] = args.volume_receipts
    if args.volume_state is not None:
        overrides["volume_state"] = args.volume_state

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
