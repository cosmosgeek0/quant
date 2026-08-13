#!/usr/bin/env python3
"""Headless evidence check for the quant read-only UI runtime (ui/serve.py).

Usage:
  python3 verify.py <base_url>            # server over the real sources
  python3 verify.py <base_url> --absent   # server whose sources all point
                                          # at nonexistent paths

Present mode checks, against a running serve.py:
  1. Static page assets answer HTTP 200; index.html still has exactly one
     control (the refresh button) and wires app.js; app.js fetches every
     /api/ endpoint plus the public price endpoint, and no longer fetches
     the fixture path directly (no fixture-only assumption).
  2. /api/health reports read_only with all five sources.
  3. /api/events serves the real recorded rows (>= 9 events, exactly one
     CLOSED run) and the page's own render function (app.js, run in Node)
     produces one non-empty row per event.
  4. /api/basis serves the recorded basis_read output (13 columns, >= 2 rows).
  5. /api/futures answers honestly (present false => zero rows, no invention).
  6. /api/volume serves the real paper receipts (>= 2, all paper:true) with
     one non-empty rendered row each; absent state is reported absent.
  7. POST answers 405: no mutating endpoint exists.

Absent mode asserts every endpoint answers the honest empty shape.
Exits non-zero on the first failed assertion. Prints counts.
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def curl(url, method="GET"):
    p = subprocess.run(
        ["curl", "-sS", "-X", method, "-o", "-", "-w", "\n%{http_code}", url],
        capture_output=True, text=True, timeout=30)
    body, _, code = p.stdout.rpartition("\n")
    return int(code), body


def get_json(base, path):
    code, body = curl(base + path)
    assert code == 200, f"FAIL: {path} returned HTTP {code}"
    return json.loads(body)


def node_rows(export_name, records):
    """Run the page's own row function over records in Node; return rows."""
    js = (
        "const m=require(process.argv[1]);"
        "const recs=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        f"const rows=recs.map(m.{export_name});"
        "const bad=rows.filter(r=>!r.ts||!r.kind||!r.detail);"
        "console.log(JSON.stringify({rows:rows.length,bad:bad.length}));"
    )
    p = subprocess.run(["node", "-e", js, str(HERE / "app.js")],
                       input=json.dumps(records),
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, f"FAIL: node render errored: {p.stderr.strip()[:200]}"
    return json.loads(p.stdout)


def check_read_only(base):
    code, body = curl(base + "/api/events", method="POST")
    assert code == 405, f"FAIL: POST /api/events returned HTTP {code}, want 405"
    assert "read-only" in body, "FAIL: 405 body does not state read-only"
    print("OK  POST /api/events -> 405 (read-only server, no mutating endpoint)")


def check_absent(base):
    ev = get_json(base, "/api/events")
    assert ev["present"] is False and ev["count"] == 0 and ev["events"] == [], \
        "FAIL: absent events file must answer present:false with zero rows"
    print("OK  /api/events absent -> present:false, 0 events, none invented")

    ba = get_json(base, "/api/basis")
    assert ba["present"] is False and ba["rows"] == [] and ba["text"] is None, \
        "FAIL: absent basis file must answer present:false with no rows"
    print("OK  /api/basis absent -> present:false, 0 rows, text null")

    fu = get_json(base, "/api/futures")
    assert fu["present"] is False and fu["count"] == 0 and fu["receipts"] == [], \
        "FAIL: absent futures dir must answer present:false with zero receipts"
    print("OK  /api/futures absent -> present:false, 0 receipts, none invented")

    vo = get_json(base, "/api/volume")
    assert vo["receipts"]["present"] is False and vo["receipts"]["count"] == 0 \
        and vo["receipts"]["records"] == [], \
        "FAIL: absent volume receipts must answer present:false with zero rows"
    assert vo["state"]["present"] is False and vo["state"]["state"] is None, \
        "FAIL: absent volume state must answer present:false, state null"
    print("OK  /api/volume absent -> receipts present:false 0 rows, state null")

    check_read_only(base)
    print("VERDICT: PASS (absent sources answered honestly)")


def check_present(base):
    # 1. static assets + page wiring
    pages = {}
    for path in ("/", "/index.html", "/app.js", "/style.css"):
        code, body = curl(base + path)
        assert code == 200, f"FAIL: {path} returned HTTP {code}"
        pages[path] = body
        print(f"OK  HTTP 200 {path} ({len(body)} bytes)")
    html = pages["/index.html"]
    n_buttons = html.count("<button")
    assert n_buttons == 1, f"FAIL: expected exactly 1 button, found {n_buttons}"
    assert 'id="refresh"' in html, "FAIL: refresh control missing"
    assert 'src="app.js"' in html, "FAIL: app.js not wired"
    js = pages["/app.js"]
    for url in ("/api/events", "/api/basis", "/api/futures", "/api/volume"):
        assert f'"{url}"' in js, f"FAIL: {url} not wired in app.js"
    assert "api.binance.com/api/v3/ticker/price" in js, \
        "FAIL: price endpoint not wired"
    assert 'fetch("fixtures/events.jsonl")' not in js and \
        "FIXTURE_PATH" not in js, \
        "FAIL: fixture-only assumption still present in app.js"
    print("OK  static: 1 refresh control; app.js wired to all 4 /api/ "
          "endpoints + price endpoint; no direct fixture fetch")

    # 2. health
    health = get_json(base, "/api/health")
    assert health["ok"] is True and health["read_only"] is True, \
        "FAIL: /api/health must report ok + read_only"
    assert health["bind"] == "127.0.0.1", "FAIL: server must bind loopback"
    assert sorted(health["sources"]) == \
        ["basis", "events", "futures_dir", "volume_receipts", "volume_state"], \
        "FAIL: /api/health sources incomplete"
    print("OK  /api/health: read_only, bind 127.0.0.1, sources:")
    for name, path in sorted(health["sources"].items()):
        print(f"      {name} = {path}")

    # 3. events: the real recorded rows, rendered by the page's own function
    ev = get_json(base, "/api/events")
    assert ev["present"] is True, "FAIL: events source must be present"
    assert ev["parse_errors"] == 0, "FAIL: recorded events must parse cleanly"
    assert ev["count"] >= 9, f"FAIL: expected >= 9 real events, got {ev['count']}"
    runs = {}
    for event in ev["events"]:
        runs.setdefault(event["run_id"], []).append(event)
    closed = [rid for rid, evs in runs.items()
              if any(e.get("verdict") == "CLOSED" for e in evs)]
    assert len(closed) == 1, "FAIL: expected exactly one CLOSED run"
    out = node_rows("eventRow", ev["events"])
    assert out["rows"] == ev["count"], \
        f"FAIL: rendered {out['rows']} rows for {ev['count']} events"
    assert out["bad"] == 0, f"FAIL: {out['bad']} event rows empty"
    print(f"OK  /api/events: {ev['count']} real events, "
          f"{len(runs)} runs, CLOSED run {closed[0]} "
          f"({len(runs[closed[0]])} events); {out['rows']} rendered rows, 0 empty")

    # 4. basis: the recorded live read
    ba = get_json(base, "/api/basis")
    assert ba["present"] is True, "FAIL: basis source must be present"
    assert len(ba["columns"]) == 13, \
        f"FAIL: expected 13 basis columns, got {len(ba['columns'])}"
    assert len(ba["formulas"]) == 4, "FAIL: expected the 4 formula lines"
    assert len(ba["rows"]) >= 2 and ba["parse_errors"] == 0, \
        "FAIL: expected >= 2 recorded basis rows parsing cleanly"
    symbols = [row["symbol"] for row in ba["rows"]]
    print(f"OK  /api/basis: {len(ba['rows'])} recorded rows {symbols}, "
          "13 columns, 4 formula lines")

    # 5. futures: honest either way, no invention
    fu = get_json(base, "/api/futures")
    if fu["present"]:
        assert fu["count"] == len(fu["receipts"]), "FAIL: futures count mismatch"
        print(f"OK  /api/futures: dir present, {fu['count']} receipts")
    else:
        assert fu["count"] == 0 and fu["receipts"] == [], \
            "FAIL: absent futures dir answered with invented rows"
        print(f"OK  /api/futures: dir absent ({fu['source']}), "
              "honest 0 receipts, none invented")

    # 6. volume: real paper receipts, rendered; honest state
    vo = get_json(base, "/api/volume")
    receipts = vo["receipts"]
    assert receipts["present"] is True, "FAIL: paper receipts must be present"
    assert receipts["count"] >= 2, \
        f"FAIL: expected >= 2 real paper receipts, got {receipts['count']}"
    assert all(r.get("paper") is True for r in receipts["records"]), \
        "FAIL: every volume record must carry paper:true"
    out = node_rows("paperReceiptRow", receipts["records"])
    assert out["rows"] == receipts["count"] and out["bad"] == 0, \
        "FAIL: paper receipt rows did not all render"
    state = vo["state"]
    if state["present"]:
        assert state["state"] is not None, "FAIL: present state must be shown"
        print(f"OK  /api/volume: {receipts['count']} paper receipts rendered; "
              "state file present")
    else:
        assert state["state"] is None, "FAIL: absent state must stay null"
        print(f"OK  /api/volume: {receipts['count']} paper receipts "
              "(all paper:true) rendered; state honestly absent")

    # 7. read-only proof
    check_read_only(base)
    print("VERDICT: PASS")


def main():
    args = [a for a in sys.argv[1:]]
    absent = "--absent" in args
    if absent:
        args.remove("--absent")
    if len(args) != 1:
        sys.exit("usage: verify.py <base_url> [--absent]")
    base = args[0].rstrip("/")
    if absent:
        check_absent(base)
    else:
        check_present(base)


if __name__ == "__main__":
    main()
