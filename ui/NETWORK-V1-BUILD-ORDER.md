NETWORK V1 — OWNER BUILD ORDER — 2026-08-16

GOAL

Build one real Network instrument running on the AWS execution box and visible
through arbitrage.cosmosgeek.org.

It must answer:

1. What is connected?
2. Is real data moving?
3. Is the Machine actually receiving that data?
4. What deteriorated, when, and why?
5. Which monitoring controls are genuinely available?

The Mac may edit code, but it is not a data source. Production renders only
real AWS, Cloudflare, Binance and runtime evidence. No production mocks.

For this tab, “live proof” means real AWS/Cloudflare/Binance-public traffic.
A trade does not prove Network telemetry and is not part of this build.


SOURCE AND BRANCH LAW

1. Refresh Git before starting and record current heads.
2. Start the build branch from current origin/main.
3. lane/ui-network-v5 is visual/reference material only. It is currently
   heavily behind main. Import only exact UI assets that are deliberately
   retained.
4. Never merge lane/ui-network-v5 wholesale.
5. Never use its stale src/ or scripts/ as runtime truth.
6. Read the real deployed watch/feed_state.json schema before implementing.
7. Preserve News. Network shows one News connection summary linking to News.
8. Move the existing runner/supervisor heartbeat presentation to Machine
   later. It is not Network truth.


CORRECT CONNECTION MODEL

Do not hard-code “ten wires” or “healthy 8/10”.

Continuously or periodically observed paths:

- Browser → hosted site
  Session-only end-to-end fetch time. It exists only while the browser is open.

- Cloudflare edge ↔ cloudflared
  HA connections, active streams, proxy requests, retries, QUIC RTT/loss.

- cloudflared → quant-ui origin
  Loopback origin response time and tunnel origin-request errors.

- EC2 → Binance Spot REST
  Public /api/v3/time only.

- EC2 ↔ Binance Spot WebSocket
  Passive watch/feed_state.json evidence. Never open a duplicate socket.

- EC2 → Binance USD-M REST
  Public /fapi/v1/time only.

- EC2 ↔ Binance USD-M WebSocket
  Passive watch/feed_state.json evidence. Never open a duplicate socket.

- Feed → Machine stream
  Feed snapshot age, published book age, fresh/eligible/accepted symbol counts,
  and recorded source when the runtime exposes it. This is mandatory.

- News sources → News fetcher
  One aggregate connection only.

Collapsed on-demand dependencies:

- Binance signed account reads
- Binance execution
- AWS Secrets Manager
- AWS SSM

On-demand paths show Last used + Last outcome. Inactivity is not an outage.
Never create synthetic signed calls, orders, Secrets reads or SSM sessions
merely to make Network look active.


TRUTH MODEL

Every connection has separate fields:

health:
  Operational | Degraded | Outage | Stale | Unknown | Off

authority:
  Public | Read-only | Signed-read | Trade-capable | Owner-gated

monitoring:
  Running | Paused

mode:
  Continuous | Periodic | Session | On-demand

truth:
  configured: Pass | Fail | Unknown | Not-applicable
  transport:  Pass | Fail | Unknown | Not-applicable
  application: Pass | Fail | Unknown | Not-applicable

Owner-gated is never a health state.

Missing evidence is null + Unknown. Never convert missing data to zero or green.

The summary says:

  Operational required paths / Observed required paths
  Unknown count
  Worst current path
  Oldest required evidence
  Last meaningful transition

Do not use a fixed denominator that includes idle/on-demand connections.


OWNER PAGE

Build four surfaces only.

1. CONNECTION STORY

One large primary instrument immediately below the header.

Its top is a fixed topology:

  Browser → Cloudflare → cloudflared → Quant EC2
                                      ├─ Spot REST
                                      ├─ Spot WSS
                                      ├─ USD-M REST
                                      ├─ USD-M WSS
                                      ├─ News
                                      └─ Feed → Machine

Beneath that topology are aligned time lanes with one shared time cursor.
Every lane has its own unit and scale; never mix milliseconds, message age,
frame drops and request weight on one y-axis.

Time ranges:
  1H | 6H | 24H

Include state ribbons and event markers for:

- connection loss/recovery
- reconnect
- new dropped-frame delta
- stale evidence
- 429 or 418
- origin proxy errors
- collector stop/recovery
- configuration changes
- deployed release change

Clicking any edge or graph lane selects the same connection everywhere.

2. CONNECTION TABLE

Worst first.

Columns:

  Connection
  Health
  Primary reading
  Freshness
  Limit/headroom where applicable
  Last change
  Compact status history

Use:
  60 × 1-minute cells for 1H
  72 × 5-minute cells for 6H
  96 × 15-minute cells for 24H

3. CONNECTION DRAWER

For the selected connection show only applicable facts:

- current status and plain-language reason
- configured / transport / application truth
- direction and what flows
- observation vantage
- last sample, success and transition
- sample count
- p50/p95 only after enough samples
- last-message age
- reconnect and dropped-frame deltas
- REST weight usage/headroom
- evidence source
- controls actually implemented

Unknown must explain why, for example:

  Feed evidence is not mirrored
  Collector is not installed
  No real execution has occurred recently
  Runtime does not expose this measurement

4. TRANSITION LEDGER

Show only changes worth investigating:

  time | connection | transition | reason | duration | evidence

Do not call it a causal ledger unless causality is explicitly recorded.
Do not show raw logs, SHAs, PIDs or every healthy sample.


COLOUR AND MOTION

Permit one small stable muted colour per data series.
Reserve red/amber/green for health.

Colour never acts alone: include status word, glyph and line pattern.

No animated particle or changing edge width unless driven by a real counter.
Skip particles in V1. Reduced-motion must disable movement.


REAL METRICS

Cloudflare:

- cloudflared_tunnel_ha_connections
- cloudflared_tunnel_active_streams
- cloudflared_tunnel_total_requests delta
- cloudflared_tunnel_request_errors delta
- cloudflared_tunnel_timer_retries
- quic_client_latest_rtt
- quic_client_smoothed_rtt
- quic_client_lost_packets delta

Use the exact configured/discovered loopback metrics endpoint. Do not assume
a fixed port and do not scan arbitrary ports.

Binance REST:

- HTTP outcome
- total RTT
- server clock offset using response midpoint
- X-MBX-USED-WEIGHT headers
- Retry-After
- 429 and 418 counters
- consecutive failure count

Use one /time request per venue per cycle. Do not use both ping and time.
Default 15 seconds = eight public calls per minute total.

Binance requires clients to back off after 429; continued violations can
produce an IP ban with status 418. Never automatically retry these responses.

Binance WSS, from the existing feed only:

- expected/connected connection count
- handshake attempts/failures
- reconnect total
- stream count
- last-message age when present
- cumulative and new dropped frames when present
- snapshot age

Do not display ping RTT, bytes/sec, messages/sec, sequence gaps or subscription
acknowledgements until real instrumentation exists.

Feed → Machine:

- feed snapshot age
- published stream book age
- eligible shared symbols
- accepted/fresh symbols
- stale/refused symbols
- source = Feed / REST / Unknown when explicitly recorded

If the socket is fresh but Machine books are stale, the external WSS edge may
be Operational while Feed → Machine is Degraded or Outage. Preserve that
distinction.


BACKEND CONTRACT

Create:

GET /api/network
GET /api/network/history?edge_id=<id>&metric=<metric>&range=1h|6h|24h
GET /api/network/events?after=<cursor>&limit=<bounded>

POST /api/network/config
POST /api/network/probe

The two POSTs may affect monitoring only. They never touch market feed,
trading runtime, Cloudflare lifecycle, systemd, account state or money.

Current response contains:

schema_version
generated_at
collector status
deployed UI release
summary
nodes[]
edges[]
recent bounded events[]

Each edge contains:

id, source, target, label, direction, mode, required
health, health_reason
authority, monitoring
configured/transport/application truth
observed_at, fresh_until, cadence_ms, sample_count
metrics{}, counters{}
evidence source and source age
implemented control IDs

History is fetched only when edge/range/metric changes.
Never resend a full day of history every five seconds.
Maximum 720 returned buckets per history request.


STORAGE

Use:

ui/live/network_current.json
ui/live/network_history.sqlite3

SQLite uses WAL mode.

Tables:

samples:
  timestamp, edge_id, status, metrics JSON

events:
  event_id, timestamp, edge_id, transition, severity, reason, evidence JSON

Retention:

- raw samples: 48 hours
- transition events: 30 days
- prune hourly
- database maximum target: 100 MB

Do not create another giant JSONL that gets repeatedly reparsed.


CONTROLS IN V1

Real controls only:

- Live view: pauses browser refresh only
- Range: 1H / 6H / 24H
- Filter: Required / Unhealthy / All
- Browser refresh: 2 / 5 / 10 / 30 seconds
- WSS evidence read: 5 / 10 / 15 seconds
- REST probe: 15 / 30 / 60 seconds
- Cloudflare/origin monitor: 15 / 30 / 60 seconds
- News monitor: 120 / 300 / 600 seconds
- Pause one monitoring collector
- Probe now: one public GET, no retry, minimum cooldown 30 seconds

Every persistent change:

- is allowlisted
- shows old and new value
- writes one monitoring receipt
- rereads the saved state
- produces a transition event

Use honest names:

Live view, not Network live
Monitor every, not Cadence
Pause monitor, not Disconnect Binance

Do not build in V1:

- feed reconnect/resync
- tunnel restart
- DNS/Cloudflare route modification
- News stop
- SSM actions
- Secrets retrieval
- signed probes
- execution probes
- orders/transfers/leverage
- adjustable arbitrary failure thresholds


BUILD ORDER

Every card is narrow and ends with real AWS evidence before the next card.
Do not accumulate thousands of lines before deployment.

NET-01 — CANONICAL EVIDENCE

- Import the exact host mirror script into Git; it is currently host-only.
- Preserve every existing mirror operation.
- Atomically mirror watch/feed_state.json into ui/live/feed_state.json.
- Record source timestamp, checksum and copy error.
- Malformed/missing input must preserve the previous good copy and report error.
- Fix singular/plural pull-manifest error handling.
- Deploy to AWS.
- Prove source and mirrored hash/bytes/freshness match.

NET-02 — NETWORK RECORDER

- New ui/network_recorder.py, approximately 500 production lines.
- Read mirrored feed and stream evidence.
- Probe only the named public Binance time endpoints.
- Scrape the exact cloudflared loopback metrics URL.
- Probe the configured loopback quant-ui origin.
- Summarize News.
- Write atomic current state and bounded SQLite history.
- No credential, signed call, duplicate WebSocket or runtime mutation.
- Add and version quant-ui-network.service.
- Deploy it.
- Show real network_current.json and database rows from AWS.

NET-03 — NETWORK API

- Put normalization/query logic in ui/network_api.py.
- Keep serve.py changes to small route registration and request validation.
- Current, history and events responses are separately bounded.
- Status derivation happens server-side.
- Deploy.
- Verify real responses on AWS and through the Cloudflare-protected site.

NET-04 — CONNECTION STORY

- Build the topology plus aligned historical lanes.
- Use only real NET-03 responses.
- No production fixtures.
- Add synchronized click/cursor/selection behavior.
- Deploy and visually inspect on the real protected URL.

NET-05 — TABLE, DRAWER, LEDGER

- Worst-first table and history raster.
- Selected-connection drawer.
- Transition-only ledger.
- Preserve focus and selection during refresh.
- Mobile uses an ordered connection list, not a crushed topology.
- Deploy and verify using real degraded/Unknown states.

NET-06 — CONTROLS

- Implement only the allowlisted monitoring controls above.
- No dead toggle.
- Every saved change produces a receipt and post-write reread.
- Deploy.
- Change each control from the real page and prove the collector obeyed it.

NET-07 — LIVE ACCEPTANCE

On AWS:

1. Record exact deployed UI and recorder SHAs.
2. Compare feed connection counts, last-message time, reconnects and dropped
   frames with the source feed_state at one timestamp.
3. Verify real Spot REST, Spot WSS, USD-M REST and USD-M WSS.
4. Verify Cloudflare connector and origin are distinct.
5. Verify Feed → Machine exposes the current fresh-feed/stale-book condition.
6. Stop only quant-ui-network long enough to cross freshness.
7. Confirm the page becomes Stale, not Outage or green.
8. Restart it and require two successes before Operational.
9. Confirm the recovery transition appears in history.
10. Measure ten-minute CPU, RSS, disk growth and request count.
11. Verify /api/network <100 KB and locally <50 ms p95.
12. Verify 24H history <1 MB and locally <250 ms p95.
13. Search responses for secrets, keys, signatures, account identifiers and
    raw receipts.
14. Verify the finished page through arbitrage.cosmosgeek.org, not localhost.
15. Commit one raw sanitized acceptance transcript.

Targets:

- recorder average CPU below 1% of one core over 15 minutes
- recorder RSS below 60 MB
- retained database below 100 MB
- default Binance public probes no more than eight requests/minute
- browser current-state refresh every five seconds
- zero duplicate WebSocket feeds


REPORTING

After each card report only:

card
files changed
production lines added/deleted
real AWS artifact produced
live URL/source checked
first failure
final result
what remains Unknown

Tests support the build but “tests green” is never completion.
AWS evidence is completion.

Stop after NET-07. Do not expand Network into a cloud administration console.

ADDENDUM — CORRECTIONS ACCEPTED (Codex review of this order, 2026-08-16; folded by the UI seat)

1. Storage: the SQLite history and any bulk store live OUTSIDE the web root
   (e.g. /home/ssm-user/quant-ui/data/), never under ui/ — serve.py serves any
   file inside ui/, so a database there would be downloadable. Only bounded
   API queries expose it. ui/live/network_current.json stays (small, public
   by design).
2. Authority: workers build named files only. Git refresh/branching, xfer,
   host restarts and every AWS deployment are integrator-only (the seated
   Lead), verified by hand, recorded under evidence/. Workers never run git or
   touch the host (AGENTS.md).
3. Health derivation table, per edge, before NET-03 ships: required signals,
   freshness TTL, partial-failure rule, outage rule, recovery quorum (two
   successes before Operational), minimum sample count before p50/p95. Two
   builders must derive the same state from the same evidence.
4. POST endpoints (config, probe): authentication + authorization, Origin /
   CSRF check, per-control cooldown, idempotency key, receipt schema, audit
   retention. Registering the routes is not enough.
5. Evidence clarity: browser timing is session evidence only (cannot prove
   Cloudflare Access when no browser is open); cloudflared local metrics
   prove connector behaviour, not DNS/TLS/Access policy; discover cloudflared
   metric names from the installed version and treat them as candidates
   until verified.
6. Engineering: stable edge IDs; UTC timestamps plus monotonic durations;
   migrations/indexes; hard DB size cap; corruption recovery; exact p50/p95
   sample minimum. Line counts are per-card BUDGETS reported +/− (owner size
   law), never a target to hit.
7. Fixtures: deterministic test fixtures are required for tests and can never
   feed production.
8. Particles/animation: skipped in V1 (as ordered); topology + synchronized
   lanes first; motion only when a genuine counter exists.

PRECEDENCE: this order supersedes ui/NETWORK-TAB-BRIEF.md wherever they
differ (branch law, wire list, storage, controls). The brief remains the
design background and the record of the research.
