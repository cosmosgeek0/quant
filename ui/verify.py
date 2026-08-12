#!/usr/bin/env python3
"""Headless evidence check for the quant read-only UI.

Usage: python3 verify.py <base_url>  (e.g. http://127.0.0.1:8901)

Checks, against a locally served copy of ui/:
  1. index.html, app.js, style.css, fixtures/events.jsonl all serve HTTP 200 (curl).
  2. The served fixture parses as JSONL; per-run and per-kind counts are reported.
  3. The page's own render function (app.js, run in Node) produces exactly one
     non-empty row per fixture event — every event is accounted for on screen.
  4. index.html has exactly one button (the refresh control) and wires app.js
     and the fixture path.
Exits non-zero on the first failed assertion. Prints counts.
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def curl(url):
    p = subprocess.run(
        ["curl", "-sS", "-o", "-", "-w", "\n%{http_code}", url],
        capture_output=True, text=True, timeout=30)
    body, _, code = p.stdout.rpartition("\n")
    return int(code), body


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: verify.py <base_url>")
    base = sys.argv[1].rstrip("/")

    # 1. HTTP 200 on every page asset and the fixture
    pages = {}
    for path in ("/index.html", "/app.js", "/style.css", "/fixtures/events.jsonl"):
        code, body = curl(base + path)
        assert code == 200, f"FAIL: {path} returned HTTP {code}"
        pages[path] = body
        print(f"OK  HTTP 200 {path} ({len(body)} bytes)")

    # 2. Fixture accounting
    lines = [l for l in pages["/fixtures/events.jsonl"].splitlines() if l.strip()]
    events = [json.loads(l) for l in lines]
    runs, kinds = {}, {}
    for ev in events:
        runs.setdefault(ev["run_id"], []).append(ev)
        kinds[ev["kind"]] = kinds.get(ev["kind"], 0) + 1
    print(f"OK  fixture events total: {len(events)}")
    for rid, evs in runs.items():
        verdicts = [e.get("verdict") for e in evs if e["kind"] == "run_verdict"]
        print(f"OK  run {rid}: {len(evs)} events, verdict {verdicts}")
    print(f"OK  kinds: {json.dumps(kinds, sort_keys=True)}")
    closed = [rid for rid, evs in runs.items()
              if any(e.get("verdict") == "CLOSED" for e in evs)]
    assert len(closed) == 1, "FAIL: expected exactly one CLOSED run"
    print(f"OK  CLOSED run {closed[0]} has {len(runs[closed[0]])} events")

    # 3. Run the page's own render function over the served fixture in Node
    node_js = (
        "const m=require(process.argv[1]);"
        "const evs=m.parseJsonl(require('fs').readFileSync(0,'utf8'));"
        "const rows=evs.map(m.eventRow);"
        "const bad=rows.filter(r=>!r.ts||!r.kind||!r.detail);"
        "const grouped=m.groupRuns(evs);"
        "console.log(JSON.stringify({rows:rows.length,bad:bad.length,"
        "runs:grouped.map(g=>({run_id:g.run_id,n:g.events.length}))}));"
    )
    p = subprocess.run(
        ["node", "-e", node_js, str(HERE / "app.js")],
        input=pages["/fixtures/events.jsonl"],
        capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, f"FAIL: node render errored: {p.stderr.strip()[:200]}"
    out = json.loads(p.stdout)
    assert out["rows"] == len(events), \
        f"FAIL: rendered {out['rows']} rows for {len(events)} events"
    assert out["bad"] == 0, f"FAIL: {out['bad']} rows missing ts/kind/detail"
    print(f"OK  render accounting: {out['rows']} rows for {len(events)} events, "
          f"0 empty; per-run {out['runs']}")

    # 4. Static page checks: one control, correct wiring, honest empty states
    html = pages["/index.html"]
    n_buttons = html.count("<button")
    assert n_buttons == 1, f"FAIL: expected exactly 1 button, found {n_buttons}"
    assert 'id="refresh"' in html, "FAIL: refresh control missing"
    assert 'src="app.js"' in html, "FAIL: app.js not wired"
    assert html.count("nothing yet") >= 2, "FAIL: empty states missing"
    js = pages["/app.js"]
    assert "fixtures/events.jsonl" in js, "FAIL: fixture path not wired in app.js"
    assert "api.binance.com/api/v3/ticker/price" in js, "FAIL: price endpoint not wired"
    print("OK  static: 1 refresh control, app.js + fixture + price endpoint wired, "
          "2 honest empty states")

    print("VERDICT: PASS")


if __name__ == "__main__":
    main()
