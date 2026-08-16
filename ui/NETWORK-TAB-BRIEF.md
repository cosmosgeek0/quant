# NETWORK TAB — brief for the next builder (owner review pass, 2026-08-16)

Plain words. Decisions first, reasons after. Web sweep done 2026-08-16 (sources
at the end). This does not replace ui/DESIGN.md (look and voice) — it says WHAT
belongs on the Network tab, what leaves, what is missing, and which controls are
real.

## 1. What the Network tab is for (one sentence)
The wires: is the box talking to Binance (REST and websocket) fast and cleanly,
is the site reachable (browser → Cloudflare → tunnel → box), are the outside
sources (news) reachable — with one big story graph on top and the switches to
slow down or pause each wire.

## 2. Verdict on the current page (V5 on the site)
- V5 is a good HEARTBEAT page (loops vs their own cadence, worst-first table,
  hour raster, state changes). Its own research census named its category
  "cron/heartbeat monitoring". That is MACHINE-tab material: the job runner,
  supervisor, basis watch, basis supervisor are the agent's own loops, not
  connections. Wrong door, not wrong work.
- The one network that makes money — box ↔ Binance — is NOT measured anywhere
  today (checked on 2026-08-16: no latency, no rate-limit weight, no error
  counts, no websocket connection state in any recorded file; the feed's
  connection report exists only in the FD1 lane, not yet on the host).

## 3. STRIP from Network (move, do not delete)
| item | goes to | why |
|---|---|---|
| Job runner, Supervisor, Basis watch, Basis supervisor rows, cards, raster rows | Machine tab → "Heartbeats" (keep V5's table/raster/state-changes there intact) | they are the machine's loops, not wires |
| "Data mirror / TRANSPORT" | gone once the Mac viewer retires (on the box it is a 5-second local copy, not a network) | not a wire |
| MAC / TRANSPORT / HOST control-path panel | replaced by the real path (see §4) | describes the old Mac plumbing |
| grey "beat" charts | replaced by colored single charts (owner's word) | owner: "they don't look like a graph" |
| dead per-card toggles (any switch without an endpoint) | removed | DESIGN.md §4 |

## 4. KEEP / RESHAPE
- One big story graph on top (owner's word): one time axis; colored lines, one
  per wire (Binance REST round-trip, websocket last-message age, rate-limit
  weight used %, site round-trip, news fetch time). Click a point → the state
  change / event at that moment shown beside it. Zoom 1H/6H/24H stays.
- Under it, one colored single chart per wire (the older look), each with its
  status word, current value, interval select, grace select, and a pause where
  a real pause exists.
- Hour raster (one row per WIRE) and the state-changes log stay — they are the
  "what went wrong and when" answer — but for wires only.
- Path strip becomes the real path: Browser → Cloudflare → Tunnel → Box →
  Binance, plus Box → News sources. Each hop shows its own number.

## 5. ADD (the missing wires — build order)
1. **Binance REST from the box**: round-trip time (ping endpoint every N s),
   error rate (timeouts, 5xx, 4xx), rate-limit weight used per minute vs the
   limit (from the X-MBX-USED-WEIGHT-1M response header; order-count limits
   likewise), and 429/418 events (ban risk). This is the single most useful
   thing this tab can show.
2. **Binance websocket from the box**: per socket connected/handshake, reconnect
   count, last-message age, book age. Binance pings every 20 s and drops
   silent connections after a minute; a connection lives at most 24 h — the
   daily reconnect must be visible, not a mystery. FD1's connection report is
   the source; deploy it.
3. **Tunnel / site**: cloudflared health (healthy = four edge connections,
   degraded, down — the same words Cloudflare uses) and browser → site
   round-trip (exists as "web server ping").
4. **News sources**: reachable / live / delayed / error per source (exists in
   the sources board), fetch duration, interval and pause (control).
5. Box network bytes in/out (cheap, one line) — optional.

## 6. CONTROLS — only real ones (each names its endpoint)
| control | today | endpoint |
|---|---|---|
| Network live (pause the page's own polling) | exists | client + /api/poll_config |
| sampling interval per wire | exists | /api/poll_config |
| grace per wire | exists (V5) | /api/grace |
| news loop: interval, pause | BUILD | fetcher reads a small control file the dashboard writes (ui/live only) |
| feed: pass interval slow/fast | BUILD, owner word | host control file the feed honors |
| "shut all networks" | NOT here as a fake button | the real one is the machine's STOP (Machine tab). Network shows STOP state read-only with a link. Pausing the market feed blinds the machine (it fail-closes) — that is a Machine decision, not a Network toggle |

## 7. Look
Color per wire, area fill under each line, ink text, status color only on
deviation (DESIGN.md). One graph tells the story; small charts repeat its
colors; raster and log below; controls on each card.

## 8. What the references do that we copy (web sweep 2026-08-16)
- Cloudflare tunnel health words: healthy / degraded / down / inactive by number
  of edge connections — copy the words and the meaning.
- Uptime Kuma: per monitor a heartbeat bar + response-time chart + interval +
  retries + pause + maintenance — the closest shape to "one card per wire".
- Datadog network table: RTT, retransmits, volume between two endpoints, and a
  pivot from a network spike to the traces at that time — our pivot is graph
  point → state change / log row.
- Grafana state timeline / status history: our raster is that panel.
- Statuspage 90-day bars: hover a day → duration and incidents — our raster
  cells should say the same on hover.
- Binance rules to measure against: weight headers per interval, 429 then 418
  ban, ping every 20 s, 24-hour socket life.
- IBKR TWS "Data connections": one colored status per data farm — one colored
  status per wire. Hummingbot: `connect` status per exchange connector,
  `gateway ping` latency — same idea.

## 9. Sources
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
