# NETWORK V1 — PAGE SPEC (integrator, 2026-08-17 ~00:15 IST) — read with ui/DESIGN.md

The Network page is rebuilt from zero inside the existing shell (index.html
sidebar/topbar, shell.js router, tokens in net.css/net_v5.css). Owner words
that bind: no master multi-lane graph; a small fixed path strip on top;
GitHub-style status boxes over time — LEFT the wires, RIGHT the time
(1H/6H/24H); worst-first; click a wire → its reason and facts; a short
"what changed" ledger; controls only where a real endpoint answers; the look
already on the site (DESIGN.md text castes, colour only for deviation, the
V5 muted style and its raster cells). Data comes ONLY from ui/network_data.js
(window.NetworkData) → /api/network, /api/network/history, /api/network/events.
No fixtures, no invented numbers: a missing value renders "—", an unknown
state renders the word Unknown in muted ink.

## 0. Files and interfaces (one owner per file)
- ui/network.css            — every class below (owner: page card)
- ui/network_page.js        — window.NetV1 = { build(container), destroy() } (orchestrator)
- ui/network_strip.js       — window.NetV1Strip = { render(host, current, selectedId, onSelect) → { update(current, selectedId) } }
- ui/network_table.js       — window.NetV1Table = { render(host, model, opts) → { update(model) } }
- ui/network_drawer.js      — window.NetV1Drawer = { render(host, edge, buckets, opts) → { update(edge, buckets, opts) } }
- ui/network_ledger.js      — window.NetV1Ledger = { render(host, events) → { update(events) } }
model (table) = { current, historyByEdge: { edgeId: buckets[] }, range: "1h"|"6h"|"24h",
  filter: "required"|"unhealthy"|"all", selectedId, nowMs, sessionFetchMs }
opts (table) = { onSelect(edgeId) } ; opts (drawer) = { range, metric, onMetric(name), onClose() }
buckets[] = the /api/network/history "buckets" array (t, n, status, avg, max).
Plain browser JS (ES2018), no modules, no libraries, no external calls; each
component builds DOM with document.createElement, never innerHTML with data.
Components keep focus and selection across update(): patch text and classes,
never rebuild a row that already exists (rows keyed by edge id).

## 1. Page anatomy (top → bottom, inside `.st-page`)
1. `.st-head` — `.st-t-title` "Network" · `.st-spacer` · refresh icon button
   (`.st-iconbtn`, same SVG path as the V5 head, title "refresh now") ·
   `.st-master` with `.st-t-mid` "Live view" + the shell's switch look
   (`.n1-switch`, role="switch", aria-checked): ON = page polls; OFF = polling
   paused, `.st-page` gains `paused` (the V5 pale look) — nothing else changes.
   Under the head one `.n1-sub` (T5): "9 paths measured on the box · every 15 s · UTC".
2. `.n1-summary` (one line, T5 words, T3 numbers): "{operational_required} / {required_total}
   required paths operational · {unknown_count} unknown · worst {label} — {health word,
   coloured only if deviation} · oldest evidence {age} · last change {HH:MM:SS} {label} {transition}".
   Missing pieces render "—". Collector stopped/unknown → the line starts with
   "Recorder {status}" in the warn colour.
3. Path strip `.st-card.n1-strip` — TWO lines, no arrows drawn as SVG art, just
   text and hairlines:
   line 1 (site path): `.n1-node` "Browser" · `.n1-edge` [browser_site] · `.n1-node`
   "Cloudflare edge" · `.n1-edge` [cf_connector] · `.n1-node` "Tunnel connector" ·
   `.n1-edge` [connector_origin] · `.n1-node` "Quant UI · box";
   line 2 (from the box): `.n1-node.n1-node-box` "Execution box" then six `.n1-edge`:
   [spot_rest] [spot_wss] [usdm_rest] [usdm_wss] [news_fetch] [feed_machine].
   `.n1-node` = T4 muted text pill (hairline border, no fill). `.n1-edge` = a
   <button type="button"> with `.n1-dot` + `.n1-edge-name` (T4) + `.n1-edge-val`
   (T3 mono, the primary reading of §4, "—" when null); `aria-pressed` when
   selected; `.on` = ink outline; title attribute = "{label} · {health} · {reason}".
   Between items a `.n1-link` hairline (24px wide, 1px). Wraps gracefully at
   narrow widths (flex-wrap), never a horizontal scrollbar.
4. `.n1-filters` — left `.st-seg` Required | Unhealthy | All (default Required);
   right `.st-seg` 1H | 6H | 24H (default 1H) · `.n1-refresh` label "Refresh" +
   `.st-select` 2 s / 5 s / 10 s / 30 s (default 5 s; page-only).
5. The instrument `.st-card.n1-table` — one <table class="v5-table n1-grid">,
   worst-first (rank Outage 6 > Stale 5 > Degraded 4 > Unknown 3 > Off 2 >
   Operational 1; ties keep contract order). Columns:
   Connection (`.v5-td-name`: `.nm` T4 label + `.n1-detail` T5 "what flows", §4) ·
   Status (`.n1-dot` + `.v5-word` health word; class warn/bad/off per §5) ·
   Reading (T3 mono, primary reading §4) · Fresh (T3, age of observed_at like "3 s",
   "2 min"; "—" when null) · Headroom (T3: REST "1 / 6000"; else "—") ·
   Last change (T5 relative from the newest event of that edge in current.events
   or "—") · History (`.n1-cells` <canvas> per row, §6). Row `.on` when selected;
   click anywhere on the row selects (`opts.onSelect`); rows are keyboard
   reachable (tabindex 0, Enter/Space select). Under the table one axis row
   `.n1-axis` (T5, four UTC labels: window start, 1/3, 2/3, "now") aligned to
   the History column. Filter: required → the 8 required edges; unhealthy →
   any edge whose health is not Operational (including Unknown); all → all 13
   (browser_site reading = model.sessionFetchMs → "{n} ms", on-demand rows show
   their last used / last outcome as the reading).
6. Drawer `.st-card.n1-drawer` — hidden until a selection exists (the page
   selects the worst required wire on load so the drawer is never empty).
   Head: `.n1-dot` + `.n1-dr-name` (T4) + `.v5-word` health + `.n1-dr-reason` (T5)
   + close button (`.st-iconbtn`, ×). Body `.n1-dr-grid` two columns:
   LEFT `.n1-kv` rows (label T2 caps, value T3/T5): Truth "configured Pass ·
   transport Pass · application Fail" (each word coloured only when Fail);
   Direction "{source label} → {target label}" (↔ when direction "<->") · Flows
   "{what flows}" (§4); Vantage = edge.evidence.source; Mode/authority "Periodic
   · Public"; Last sample "18:11:07 (3 s ago)"; Last change "18:05:12 Unknown →
   Operational" (newest event for the edge, else "—"); Samples "108"; Monitor
   "Running · every 15 s"; then Metrics: every metric key of the edge whose value
   is not null, labelled by §4's dictionary with its unit ("Round trip 147 ms",
   "Weight used 1 / 6000", …); the on-demand rows show Last used / Last op /
   Last outcome. Controls implemented: the words of edge.controls joined
   ("pause monitor · monitor every · probe now") or "none". RIGHT `.n1-chart`:
   `.n1-chart-head` metric `.st-select` (numeric metric keys of the edge with
   labels; default = the primary reading's key) + T5 caption "{range} · avg per
   {bucket} cell · max as dots · UTC"; a <canvas> line chart of buckets: x =
   t, y = avg (line, ink 1.25px), max as small dots (muted); left axis min/max
   labels (T5), right edge = now; empty → the T5 line "Collecting history —
   {n} cells so far". No animation.
7. Ledger `.st-card.n1-ledger` — head `.st-card-head`: "What changed" (T4/500) +
   T5 "transitions only · newest first"; <table class="v5-table n1-ledger-t">
   rows: Time (T3 mono HH:MM:SS UTC) · Connection (T4, "—" for null edge_id →
   "Recorder") · Change (T5; the transition text; the new status word coloured
   when deviation) · Reason (T5). Newest first, at most 20; empty state T5
   "No changes recorded yet."
8. `.n1-foot` (T5): "recorder {recorder_release[:7]} · ui {deployed_ui_release[:7]}
   · collector {status} · {generated_at} UTC".

## 2. Behaviour (page orchestrator)
- On build: NetworkData.fetchCurrent(); NetworkData.startPolling(refreshMs,
  onCurrent); history for the visible edges via NetworkData.fetchHistory(id,
  "status", range) (the cache refetches at most once per bucket); the drawer's
  metric history likewise; events come with current.events (no separate poll
  in V1). Live view OFF → NetworkData.pause(); ON → resume().
- Range change → refetch history for visible edges + drawer metric; filter
  change → rows added/removed, no full rebuild; refresh select → NetworkData.
  setInterval; refresh icon → immediate fetchCurrent + history refresh.
- Selection: strip button, table row, ledger row (edge_id) all call select(id);
  select updates strip `.on`, table `.on`, drawer content; the same edge is
  highlighted everywhere. Deselect (drawer ×) hides the drawer.
- Session edge: once per minute the page measures NetworkData.measureFetchMs()
  and stores model.sessionFetchMs (browser_site reading, drawer metric fetch_ms).
- Errors: NetworkData "error" events → one T5 warn line under the summary:
  "Server unreachable — showing the last answer ({age})" ; cleared on success.
- Leaving the page (container detached / route change): destroy() stops
  polling and timers (check document.body.contains(container) on each tick).
- Reduced motion: no transitions on any state change; hover states only.

## 3. Colours and words (DESIGN.md §2/§3 applied)
- Health word: Operational → ink (dot #16a34a); Degraded → warn #d4a406 (word +
  dot); Stale → warn (word "Stale"); Outage → bad #cc2f3c; Unknown → muted word
  + grey dot (#9aa0a6); Off → muted word + hollow grey dot. Classes: `.warn`,
  `.bad`, `.off` (Unknown/Off) on `.n1-dot` and `.v5-word`.
- History cells (canvas): Operational sage #57b47a at alpha 0.32; Degraded
  #d4a406 alpha 1; Stale #d4a406 alpha 0.55; Outage #cc2f3c alpha 1; Unknown/Off
  #9aa0a6 alpha 0.35; no data neutral (--hair) alpha 0.5. Cell = the bucket's
  worst status; hover shows `.n1-tip` "18:05–18:06 UTC · Degraded · 4 samples"
  (title attribute is acceptable if a tooltip element is too costly).
- Nothing else is coloured. Selected = ink outline, hover = flat --hover.
- Text castes exactly DESIGN.md §1: T1 26px/650 · T2 9.75px/500 caps · T3 mono
  tabular 12.5px (tiles 17px) · T4 13.5px/500 · T5 11px muted · T6 12.5px/500.

## 4. Per-edge dictionary (labels, primary reading, "what flows", metric labels + units)
| edge | label | flows (T5) | primary reading | headroom |
|---|---|---|---|---|
| browser_site | Browser → site | UI requests through Cloudflare Access | sessionFetchMs "{n} ms" | — |
| cf_connector | Cloudflare connector | tunnel connections and proxied requests | "{ha_connections}/4 conns · {latest_rtt_ms_max} ms" | — |
| connector_origin | Connector → origin | proxied requests to the loopback UI | "{rtt_ms} ms" | — |
| spot_rest | Spot REST | public time probe | "{rtt_ms} ms" | "{used_weight_1m} / {weight_limit_1m}" |
| spot_wss | Spot WSS | book ticker + kline streams | "{last_message_age_ms→s} s ago" | — |
| usdm_rest | USD-M REST | public time probe | "{rtt_ms} ms" | "{used_weight_1m} / {weight_limit_1m}" |
| usdm_wss | USD-M WSS | book ticker + kline streams | "{last_message_age_ms→s} s ago" | — |
| feed_machine | Feed → Machine | published books into the stream stage | "books {book_age_p50_ms→s} s old" | — |
| news_fetch | News fetch | public sources → news fetcher | "{sources_live} / {sources_available} live" | — |
| binance_account | Binance account reads | signed account reads (on demand) | "—" | — |
| binance_exec | Binance execution | orders and job receipts (on demand) | "{last_op} {last_outcome}" | — |
| aws_secrets | AWS Secrets Manager | credential reads (on demand) | "—" | — |
| aws_ssm | AWS SSM | operator sessions (on demand) | "—" | — |
Metric labels (units): rtt_ms "Round trip" ms · http_status "HTTP" · clock_offset_ms
"Clock offset" ms · used_weight_1m "Weight used" (with weight_limit_1m as "/ N") ·
retry_after_s "Retry after" s · consecutive_failures "Consecutive failures" ·
expected_connections "Sockets expected" · connected_count "Sockets connected" ·
successful_handshakes "Handshakes" · handshake_failures "Handshake failures" ·
reconnects "Reconnects" · stream_count "Streams" · last_message_age_ms "Last
message" (as s) · snapshot_age_ms "Snapshot age" (as s) · dropped_frames_total
"Frames dropped" · dropped_frames_delta "Dropped this sample" · feed_status
"Feed status" · feed_snapshot_age_ms "Feed snapshot" (s) · stream_row_age_ms
"Stream row" (s) · symbols_total "Symbols" · book_age_p50_ms "Book age p50" (s)
· book_age_p95_ms "Book age p95" (s) · book_age_max_ms "Book age max" (s) ·
symbols_book_le_5s "Symbols under 5 s" · freshness_p50_ms "Freshness p50" (s) ·
source "Source" · ha_connections "HA connections" · ready_connections "Ready" ·
ready_status "Ready HTTP" · total_requests_delta "Requests this sample" ·
concurrent_requests "Concurrent" · latest_rtt_ms_max "QUIC RTT" ms ·
smoothed_rtt_ms_max "QUIC RTT smoothed" ms · min_rtt_ms_min "QUIC RTT min" ms ·
lost_packets_delta "Lost packets" · connect_streams_errors_delta "Stream errors" ·
register_success_total "Registrations" · metrics_url_port "Metrics port" ·
size_bytes "Bytes" · request_errors_delta "Proxy errors" · response_5xx_delta
"5xx" · sources_available "Sources" · sources_live "Live" · sources_pending
"Pending" · sources_error "Errors" · as_of_age_s "As of" s · last_fetch_age_s
"Last fetch" s · last_used_at "Last used" · last_op "Last op" · last_outcome
"Last outcome" · fetch_ms "Fetch" ms. Ages in ms render as seconds with one
decimal under 10 s, else whole seconds / "m s" above 60 s.

## 5. Status class mapping
Operational → "" · Degraded → "warn" · Stale → "warn" · Outage → "bad" ·
Unknown → "off" · Off → "off". Word text is always the health word itself.

## 6. History cells
Cells = the range's grid: 1H 60 · 6H 72 · 24H 96 (bucket_s from the response).
Draw right-aligned (newest right) into the row's canvas: cell width = floor of
available / N with a 1px gap; height 14px; devicePixelRatio aware. Cells with
no bucket → "no data" colour. Colour by bucket.status per §3. Hover → tooltip.
The canvas re-draws on update() only when the buckets reference changed or the
width changed (ResizeObserver per canvas, disconnected on destroy).
