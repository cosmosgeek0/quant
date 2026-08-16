# DASHBOARD.md — how the dashboard is wired, what runs where (measured 2026-08-16)

Read this before touching anything under ui/, the box's dashboard services, or
the site. Every row is MEASURED on 2026-08-16 unless marked. Plain rows, no
secrets. Owner order 2026-08-16: this file exists so no worker has to be told
the layout again.

## 1. What it is
- One read-only dashboard: `ui/serve.py` (Python, stdlib only, binds
  127.0.0.1 only) serves `ui/index.html` + `ui/shell.js` + `ui/net_*.js` +
  `ui/net.css` and 14 JSON endpoints. Two pages are built: **Network** and
  **News**. Overview / Basis / Momentum / Machine / Journal / Settings are
  honest empty pages until the owner orders them.
- Design law for every element: `ui/DESIGN.md` (text castes, color only for
  deviation, no dead controls). A control exists only if it hits a real
  endpoint.
- Endpoints (GET): `/api/strip health decisions failures events stream
  candidates position momentum fees pnl news telemetry poll_config`;
  `/api/poll_config` also accepts POST (sampling interval, pause).

## 2. Where it runs — THE BOX (this is the real one)
- Host: the execution EC2 (`i-0e87fe0e7ba2063a9`, ap-south-1), user
  `ssm-user`, Python 3.12. Reached only over SSM with
  `aws-connect/ssmrun.sh` (run a script) and `aws-connect/xfer.sh` (copy a
  file, sha256-verified). No inbound ports.
- Public address: **https://arbitrage.cosmosgeek.org** → Cloudflare Tunnel
  (`cloudflared.service`, token file `/etc/cloudflared/tunnel.token`, ingress
  managed in the Cloudflare dashboard) → `127.0.0.1:8080` on the box.
  Cloudflare Access sits in front: visitors get a login page; the owner is
  logged in and sees the site. `curl` from outside sees a 302 to the login —
  that is the protection working, not an outage.
- Dashboard code on the box: `/home/ssm-user/quant-ui/ui/` (a copy of the
  repo's `ui/`; leftover files from earlier UI versions also sit there).
- Services (systemd, all `User=ssm-user`, `WorkingDirectory=/home/ssm-user/quant-ui`):
  | service | does | since |
  |---|---|---|
  | `quant-ui.service` | `python3 ui/serve.py --port 8080` (loopback) | restarted 2026-08-16 12:45Z with the latest code |
  | `quant-ui-mirror.service` | `local_mirror.sh`: every 5 s copies the machine's receipt files into `ui/live/` (see §4) | 2026-08-15 |
  | `quant-ui-telemetry.service` | `python3 ui/telemetry_recorder.py --loop 15 -q` with `QUANT_UI_URL=http://127.0.0.1:8080` → `ui/live/telemetry.jsonl` | created 2026-08-16 12:45Z |
  | `quant-ui-news.service` | `python3 ui/news_fetcher.py --loop 120 -q` → `ui/live/news.jsonl`, `news_sources.json` | created 2026-08-16 12:45Z |
  | `cloudflared.service` | the tunnel | 2026-07-26 |
- Data the dashboard reads on the box: `/home/ssm-user/quant-ui/ui/live/`
  (filled by the mirror + the two recorders). The dashboard never reads the
  machine's directories directly and never writes into them.
- The box builds its own history from 2026-08-16 12:45Z: Network history
  ("Measuring" until a few samples exist, full heatmap after one hour) and
  News (first fetch within ~2 minutes) start from that moment.

## 3. The Mac side (legacy viewer — still running on 2026-08-16, not the truth)
- `Desktop/quant/ui/` = the UI seat's working copy (local `main`, pushed to
  GitHub as branch `lane/ui-4`; the operational Lead's lanes come from
  `origin/main` and do not contain the UI 3.0/4.0 commits).
- Two local servers: `serve.py` on 8899 (the Mac recorder samples it) and on
  8901 (the owner's local view). Both read `Desktop/quant/ui/live/`.
- `scripts/ui_pull.sh` pulls the box's receipt files over SSM into the Mac's
  `ui/live/` (each pull opens an SSM session, ~50 s; observed cadence ~90 s,
  so every host number on the Mac copy is ~2 minutes old).
- `scripts/ui_sync.sh` uploads the Mac's `ui/` to the box every 4 s (three
  copies were running on 2026-08-16, two orphaned; on 2026-08-16 it did NOT
  carry a code change — the file was pushed by hand with xfer). Treat the
  auto-sync as unverified until it is checked and fixed.
- The Mac copy's telemetry and news are the Mac's own; they are not the box's
  numbers. The Mac is a viewer. The site is the truth.

## 4. What the pages mean
- **Network**: 8 loops sampled every 15 s, each graded against its own
  cadence — Web server (Mac or box, whichever serves), News fetch loop, Data
  mirror, Market feed, Supervisor, Basis watch, Basis supervisor, Job runner.
  Strip and health rows come from the live files; the history table, heatmap
  and per-loop charts come from `telemetry.jsonl`. On the box the "Data
  mirror" is the 5-second local mirror (`pull_manifest.json` interval 5000).
- **News**: items from 104 public sources with impact High/Medium/Low, day
  histogram, category and impact filters, a sources board with per-source
  live/delayed/error status. Master toggle "News on" is a real control.
- Files the mirror copies from the machine (all read-only): `health.jsonl`,
  `decisions.jsonl`, `stream_state.json`, `basis-position.json`,
  `momentum-position.json`, `momentum-paper-receipts.jsonl`,
  `commission-rates.json`, `jobs/results.jsonl`, the tail of `jobs.log`,
  receipt `*.json` files, the `STOP` / `STOP-JOBS` / governor-lock flags,
  `account-governor-*.jsonl`.

## 5. How to put a UI change on the site (the way that worked on 2026-08-16)
1. Edit under `ui/` on the Mac, run the focused tests, commit.
2. Copy each changed file to the box:
   `aws-connect/xfer.sh ui/<file> /home/ssm-user/quant-ui/ui/<file>` — wait
   for `XFER_VERIFIED`. If `remote_sha` equals
   `e3b0c442…` the file landed EMPTY (empty-file sha) — copy again before
   starting or restarting anything.
3. Static files (html/js/css) are served fresh — refresh the page. A change
   to `serve.py`, `telemetry_recorder.py`, or `news_fetcher.py` needs the
   matching service restarted: put
   `sudo systemctl restart quant-ui` (or `quant-ui-telemetry`,
   `quant-ui-news`) in a small script, copy it with xfer, run it with
   `aws-connect/ssmrun.sh <script> 20`. The PTY drops heredocs and long
   lines — never type long commands into the session; copy a script, then
   run it.
4. Verify on the box: `systemctl is-active quant-ui quant-ui-mirror
   quant-ui-telemetry quant-ui-news cloudflared`;
   `curl -s http://127.0.0.1:8080/api/strip | head -c 300`;
   `curl -s -o /dev/null -w '%{http_code} %{size_download}'
   http://127.0.0.1:8080/` (size must equal the local `index.html` size).
5. Then the owner refreshes arbitrage.cosmosgeek.org. A change is "live"
   only when it is verified on the box or seen on the site — never from a
   local page.

## 6. Rules for anyone touching the dashboard
- The dashboard is read-only toward the machine. UI processes write only
  under `/home/ssm-user/quant-ui/ui/live/`. Never write into
  `/home/ssm-user/quant_receipts` or `/home/ssm-user/quant-run`.
- Never touch `cloudflared.service`, the tunnel token, Cloudflare Access, or
  `quant-ui-mirror.service` without the owner's word.
- Never print or copy a secret; the dashboard has none and needs none.
- Every element obeys `ui/DESIGN.md`; every control hits a real endpoint;
  nothing invented, nothing decorative.
- Report a change with: files, +/− lines, what was verified on the box (the
  four checks above), and what was not.

## 7. Known limits on 2026-08-16 (honest, open)
- Network loops table grades host loops on raw age (transport included)
  while `/api/health` grades on attributed lag; on the Mac copy the job
  runner showed false "Degraded" flaps because of the 2-minute pull. On the
  box the mirror is 5 s, so this mostly disappears; the table should still
  be moved to the attributed lag.
- News: aggregator duplicates (same headline via two sources) and some
  category mislabels; needs a dedupe rule and a stricter category rule.
- `tests/test_ui_telemetry.py`: one test fails and failed before 2026-08-16
  (`test_only_consecutive_double_failures_are_suppressed`).
- The Mac→box auto-sync is unverified (see §3); the Mac pull/sync loops are
  still running and should be retired once the box is the only viewer.
