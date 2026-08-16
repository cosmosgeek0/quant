# NETWORK TAB — brief for the next builder (owner review, 2026-08-16, rev 3)

Plain words. Decisions first, reasons after. Rev 2 folded in the Codex review of
rev 1 (ten points); rev 3 folds in Codex's FULL research report (connections
table, status vocabulary, /api/network contract, control classes, exact file
names). Everything from Codex is marked [codex]. Web sweep sources at the end. ui/DESIGN.md still governs look and
voice; this file says WHAT belongs on the Network tab, what leaves, what is
missing, which controls are real, and where to build.

## 0. Where to build
- BUILD FROM the V5 lane: worktree `Desktop/worktrees/net-v5`, branch
  `lane/ui-network-v5` (has `/api/grace`, net_v5.js, this brief). NOT from
  `Desktop/quant/ui` — that copy is UI 4.0, one step older, and lacks the V5
  files [codex].
- The site (arbitrage.cosmosgeek.org) serves from the box; the Mac's 8899/8901
  and the SSM pull are legacy viewers on their way out. `ui/DASHBOARD.md` has
  the wiring and the verified deploy steps.

## 1. What the Network tab is for (one sentence)
The wires: is the box talking to Binance (public REST and websocket, per
venue, plus the signed account and execution paths) fast and cleanly, is the
site reachable (browser → Cloudflare Access/edge → tunnel connector → box), are
the outside sources (news) reachable — one aligned story graph on top, and the
switches to slow down or pause each wire's monitoring or, where real, the
connection itself.
Ownership rule [codex]: Machine owns process start/stop and runtime authority;
Accounts owns balances, permissions and financial caps; Basis/Momentum own
strategy data; News owns articles and categories; Network shows whether the
paths connecting those systems are functioning. Nothing else lives here.

## 2. Verdict on the current page (V5 on the site)
- V5 is a good HEARTBEAT page (loops vs cadence, worst-first table, hour
  raster, state changes). Its own research census named its category
  "cron/heartbeat monitoring". That is MACHINE-tab material: job runner,
  supervisor, basis watch, basis supervisor are the agent's own loops, not
  wires. Wrong door, not wrong work — it moves to Machine intact.
- The MAC / TRANSPORT / HOST grouping is descriptive, not measured topology
  [codex]. It goes.
- The one network that makes money — box ↔ Binance — is measured nowhere
  today (checked 2026-08-16: no REST timing, no rate-limit weight, no error
  counts, no websocket connection state in any recorded file that reaches the
  dashboard).

## 3. The wires — with plane and TODAY's evidence [codex]
| # | connection | plane | what belongs on the wire | evidence today |
|---|---|---|---|---|
| 1 | browser → local serve.py (Mac viewer, legacy) | local | HTTP health, API answer, p50/p95, last success | measured locally |
| 2 | remote browser → Cloudflare edge / Access | ingress | DNS, TLS, Access result, edge time-to-first-byte, status family | unknown until collected |
| 3 | Cloudflare edge → tunnel connector → box origin | ingress | connector count, tunnel protocol, origin reachability, 521–526 errors | not surfaced |
| 4 | Mac mirror ↔ AWS SSM ↔ box receipts (legacy) | management | session result, pull duration, bytes, checksum, freshness | path exists; transport evidence incomplete |
| 5 | box runtime → AWS Secrets Manager | management (collapsed line) | last successful credential fetch + safe error class — never values | code-confirmed, not normalised |
| 6 | box ↔ Binance Spot public (api/stream) | market data | REST timing + weight; WSS shards, last event, ping/pong, reconnects, gaps, subscription count | evidence exists in the feed state, not mirrored |
| 7 | box ↔ Binance USD-M public (fapi/fstream) | market data | separate REST/WSS state, reconnects, continuity, rate budget | evidence exists, not mirrored |
| 8 | box → Binance signed account reads | private | latency, auth outcome, rate limit, freshness | coarse receipts only |
| 9 | box → Binance execution API | private / money | connectivity and reconciliation state ONLY — owner-gated, no action here | receipts only |
| 10 | public sources → news fetcher | public data | success/stale/failure by source, fetch duration | already in News; summarised here |
Rule [codex]: do not invent VPCs, subnets, gateways, geographic hops or other
cloud objects because enterprise consoles show them; render a node only after
the project can prove it exists and collect its state.

## 4. Truth model — every wire, three states + provenance [codex]
- States: CONFIGURED (it is set up) → TRANSPORT (DNS/TCP/TLS or socket
  handshake up) → APPLICATION (data flowing and valid: fresh messages, valid
  responses, subscription acknowledged). "Connected" alone is never healthy.
- Evidence rows per wire: measurement vantage (box or browser), last sample,
  last success, last change, DNS/TCP/TLS/HTTP or websocket timing, p50/p95,
  error classes, reconnects/backoff, last-message age, ping/pong, subscription
  ack, sequence gaps/resyncs, 429/418/5xx counts, rate-limit headroom, clock
  offset vs the exchange (signed requests fail past the recv window — a money
  fact).
- Missing evidence renders UNKNOWN — never green, never zero.
- Status words, one per wire [codex]: Operational (every required truth fresh
  and successful) · Degraded (partial shards, excess latency/loss,
  reconnecting, reduced capacity) · Outage (confirmed failure) · Stale
  (evidence older than its freshness contract) · Unknown (absent or
  contradictory evidence) · Off (intentionally disabled, with a receipt) ·
  Owner gated (an authority class, shown separately from health). A running
  cron loop proves none of the three truths; a tunnel marked healthy does not
  prove the connector reaches the origin — Cloudflare itself separates the two.
- Selected-wire drawer fields [codex]: protocol/port, direction, purpose,
  evidence source, last success, last transition, p50/p95/p99, loss or
  failed-check %, request/message rate, bytes, reconnects, sequence gaps,
  rate-limit headroom, failure reason, authority, allowed controls.

## 5. What to measure on the Binance wires (from Binance's own rules)
- REST: round-trip to the ping/time endpoint every N s; error rate by class;
  used weight per interval from the X-MBX-USED-WEIGHT-* response headers vs
  the limit (headroom %); order-count limits likewise; 429 then 418 (ban)
  events; clock offset from the server-time endpoint.
- Websocket: connected/handshake per socket, reconnect count and backoff,
  last-message age, ping/pong health (server pings every 20 s, drops after a
  silent minute), the 24-hour socket life (the daily reconnect must be visible,
  not a mystery), subscription acks, sequence gaps/resyncs, book age.
- Source that already exists: `market_feed.py` (FD1/FD2 lane) writes per-venue
  `connections` (status, attempts, handshakes, last_message_ms, dropped
  frames) into its feed-state file — see §7 for why the dashboard cannot see
  it yet.

## 6. Page anatomy (top to bottom) [codex, adopted]
1. KPI strip — truthful only (wires healthy N/10, worst wire, Binance
   round-trip p50/p95, weight headroom, freshest evidence).
2. Compact trust-zone topology [codex]: five rows — LOCAL UI, HOSTED UI
   (browser → Cloudflare edge/Access → tunnel → box origin), MIRROR (legacy),
   MARKET (box ↔ Binance public ↔ account API), NEWS. Nodes are neutral;
   the STATE sits on the link; arrows show direction; each hop shows its own
   number; UNKNOWN when unmeasured.
3. Large aligned story graph — OWNER'S WORD, kept: one shared time cursor, one
   lane per wire, each lane its own unit and colour (milliseconds, message age,
   percentages never share one Y-axis) [codex]; hovering/clicking a moment
   shows the linked event beside it; 1H/6H/24H. (Codex's own mock omits this
   graph and goes topology → table → drawer; the owner asked for the graph, so
   it stays, placed here.)
4. Worst-first wire table — status word (three-state), value, ×limit/headroom,
   late %, trend.
5. Selected-wire evidence drawer — the click-through: the §4 rows for the wire
   you selected.
6. Wire-only state-change ledger.
7. Coloured single-wire charts (the older look the owner asked for), each with
   status word, value, interval, grace, and a pause where a real pause exists.
Machine heartbeats move intact to Machine; News source detail opens News.
Green requires fresh evidence. Every chart and control labelled and keyboard
reachable.

## 7. Backend prerequisites (the tab cannot be honest without these) [codex]
- Mirror the feed-state file: `market_feed.py` writes per-venue/shard
  connection evidence (state, attempts, handshakes, failures, reconnects) into
  `RECEIPT_DIR/watch/feed_state.json` [codex — exact path]; the box's
  `local_mirror.sh` and the Mac printer (`host_print_live.sh`) copy
  `stream_state.json` but not that file — add it to the mirror list
  (read-only copy).
- Health identity: host failure rows carry a `stage` field that `/api/health`
  does not recognise as the service identity [codex] — recognise it.
- Mac mirror wire (legacy, while it lives): normalise SSM session result,
  pull duration, bytes, checksum result into one row shape [codex].
- ONE read-only contract for the page [codex]: `/api/network` returning
  `nodes[]` and `edges[]` (Grafana node-graph shape). Each edge: source,
  target, plane, direction, transport, the three truth states, reason,
  evidence time and source, freshness, latency, reconnect/gap/rate data,
  authority, allowed controls. Missing values are null with state Unknown.
  The page renders from this one contract; no page-side guessing.
- Key mismatch: the pull manifest writer emits `errors` (a list) while
  `serve.py` reads `error` (a string) — the manifest error is silently
  invisible. Fix on the serve side (read both), one test.
- Runtime `stage` failure rows are not normalised into one shape — normalise
  before they feed a wire.
- New collectors (none exist): Binance REST timing + weight headers + error
  classes (a small probe on the box, public endpoints, GET only, its own log
  file under `ui/live/`); site/edge round-trip; cloudflared health (from its
  local metrics endpoint or the Cloudflare API, read-only).
- Nothing here writes into the machine's directories; collectors write only
  under `ui/live/`.

## 8. Controls — real ones only, two kinds [codex]
Control classes [codex]: VIEW (time range, refresh, filters, show unknown,
unhealthy-only, selected metric, pause live view) · MONITORING (probe
frequency, alert threshold, expiring mute, run one read-only GET/TCP probe) ·
PUBLIC LIFECYCLE (reconnect/resubscribe/resync the public Binance feed;
pause/resume mirror or news ingest — only after an authenticated backend
returns a receipt and the state is re-read) · OWNER NETWORK ACTION (restart
the tunnel connector, change a Cloudflare route/DNS/Access rule or an AWS
network policy — through impact preview, reason, confirmation, receipt and
post-action verification; never a casual toggle) · FORBIDDEN HERE (order,
cancel, transfer, leverage, notional, loss caps, ACCEPT-ONE, blind retry).
Rename today's misleading switches [codex]: "Network live" → "Live view" (it
pauses only the page's polling), the per-wire "cadence" select → "Monitor
every" (it sets the recorder's sampling, not any host loop), chart toggles →
"Show".
PAUSE MONITORING (dashboard-side, safe, exists or trivial): Live view (page
polling), range, wire visibility, monitor interval per wire
(`/api/poll_config`), grace per wire (`/api/grace`), probe on/off.
PAUSE / TOUCH THE CONNECTION (box-side): news loop interval + pause; public
websocket reconnect / resubscribe / resync; feed pass interval. These change a
process on the box, so they go through a separate authenticated controller
with blast-radius text, confirmation, a receipt row, and a state re-read after
— or they are a link to the Machine tab. Build order: news loop first (lowest
risk), websocket reconnect second, feed interval on the owner's word.
NEVER HERE: "shut all networks" (that is the machine's STOP on Machine; pausing
the market feed blinds the machine — a Machine decision; Network shows STOP
read-only with a link), orders, transfers, leverage.

## 9. STRIP from Network (move, do not delete)
| item | goes to | why |
|---|---|---|
| Job runner, Supervisor, Basis watch, Basis supervisor rows/cards/raster rows | Machine → Heartbeats (V5's table/raster/ledger intact) | machine loops, not wires |
| Data mirror / TRANSPORT | collapsed dependency line; gone with the Mac viewer | local copy, not a network |
| MAC / TRANSPORT / HOST panel | replaced by the actual path strip | descriptive, not measured |
| grey beat charts | coloured single charts | owner's word |
| any switch without an endpoint | removed | DESIGN.md §4 |
| dead files `net_multi.js`, `net_table.js` (not loaded by index.html), plus the box's leftovers `app.js`, `style.css`, `net_spark.js`, `verify.py` | deleted (git history keeps them) | not loaded, confuse the next builder [codex] |

## 10. Look
Colour per wire, area fill under each line, ink text, status colour only on
deviation (DESIGN.md). One aligned graph tells the story; the small charts
repeat its colours; raster and ledger below; controls on each card.

## 11. What the references do that we copy (web sweep 2026-08-16)
Cloudflare tunnel status words · Uptime Kuma: per monitor a heartbeat bar +
response-time chart + interval + retries + pause + maintenance · Datadog
network table: RTT, retransmits, and the pivot from a spike to the events at
that time · Grafana state timeline / status history (our raster) · Statuspage
90-day bars: hover a day → duration and incidents · Binance rules (weight
headers, 429 → 418, 20-s ping, 24-h socket) as the things to measure · IBKR
TWS "Data connections": one coloured status per data farm · Hummingbot
`connect` status per connector, `gateway ping` latency.
Named by Codex's research [codex]: Datadog's directional network map + exact
source→destination table (strongest operational pattern); ThousandEyes path
visualisation (honest unknown/incomplete hops); Grafana Node Graph (formal
node/edge contract → our `/api/network`); AWS Cloud WAN dashboards (topology,
routes, events, metrics as separate layers, not one colourful graph); Azure
Connection Monitor (endpoint + test, RTT, failed-check %, explicit failure
reasons); Cloudflare Tunnel monitoring (connector health separate from
origin behaviour).

## 12. Sources
- https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/
- https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-wan/configuration/common-settings/check-tunnel-health-dashboard/
- https://ramnode.com/guides/series/monitoring/uptime-kuma
- https://github.com/louislam/uptime-kuma/pull/5916
- https://docs.datadoghq.com/network_monitoring/cloud_network_monitoring/network_analytics/
- https://www.datadoghq.com/blog/cloud-network-monitoring-datadog/
- https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/state-timeline/
- https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/status-history/
- https://support.atlassian.com/statuspage/docs/display-historical-uptime-of-components/
- https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits
- https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams
- https://www.interactivebrokers.com/docs/tws-api/doc/error-handling/common-error-resolution/market-data-farm-connection-is-ok
- https://hummingbot.org/client/connect/ · https://hummingbot.org/gateway/rpc/
- https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/viewing_metrics_with_cloudwatch.html
