# NETWORK TAB — brief for the next builder (owner review, 2026-08-16, rev 2)

Plain words. Decisions first, reasons after. Rev 2 folds in the Codex review of
rev 1 (ten points, all accepted; the ones that changed this brief are marked
[codex]). Web sweep sources at the end. ui/DESIGN.md still governs look and
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

## 3. The wires (one row/lane/card each) [codex — this list replaces rev 1's]
1. Site ingress: browser → Cloudflare Access/edge (round-trip, auth state)
2. Cloudflare edge → tunnel connector (cloudflared health: healthy = four edge
   connections / degraded / down / inactive — Cloudflare's own words)
3. Tunnel connector → box origin (127.0.0.1:8080 answer time, errors)
4. Binance Spot public REST (api.binance.com)
5. Binance Spot public websocket (stream.binance.com)
6. Binance USD-M public REST (fapi.binance.com)
7. Binance USD-M public websocket (fstream.binance.com)
8. Binance signed account-read HTTPS (balances, positions, open orders — GET)
9. Binance execution HTTPS (orders, margin ops — the runner's path; shown as a
   wire with its own timing/error/limit rows, never a control)
10. News-source fan-out (104 public sources)
Collapsed dependencies (one line each, never the main path): AWS Secrets
retrieval, SSM administration, the box's 5-second local file mirror (that is
local freshness, not a network) [codex].

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
2. Actual path strip — Browser → Cloudflare edge → tunnel → box → Binance,
   plus box → news; each hop shows its own number; UNKNOWN when unmeasured.
3. Large aligned story graph — one shared time cursor, one lane per wire, each
   lane its own unit and colour (milliseconds, message age, percentages never
   share one Y-axis) [codex]; hovering/clicking a moment shows the linked event
   beside it; 1H/6H/24H.
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
- Mirror the feed-state file: `market_feed.py` writes connection evidence into
  its state file, but neither the box's `local_mirror.sh` nor the Mac printer
  copies it into `ui/live/` — add it to the mirror list (read-only copy).
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
PAUSE MONITORING (dashboard-side, safe, exists or trivial): Network live
(page polling), range, wire visibility, monitor interval per wire
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
