# Network V1 — current status, end result, and corrected build order

Checked against GitHub and the four screenshots on 2026-08-16 at about 21:20 IST.

## Verdict

The product direction is mostly correct. The schedule and operating ceremony are not.

- A static picture can be made in 1–3 hours, but that would be another mock.
- A useful page with real AWS/Binance/Cloudflare numbers should be visible in roughly 4–8 focused hours.
- The complete V1 described in the 552-line order—history, drawer, event ledger, real controls, deployment and failure/recovery acceptance—is roughly 8–12 active hours if there is no unexpected host problem. That is one concentrated day or part of a second day.
- “2–4 days” comes mainly from seven sequential cards, deploying after every card, and stopping for owner permission between cards. It is not four days of coding.
- Waiting 24 hours for the 24H chart to fill is not build time. The page ships with the available history and says `Collecting history` until more samples exist.

The new Network builder has not started implementation yet. It is seated and waiting for `go NET-01`.

## GitHub truth now

- `main`: `e8bb3e65efbe0ced62b51f9c92fca6f405f34434`
- Latest main change: the Network V1 build-order document and corrections only.
- No `NET-01` commit exists.
- No new Network V1 implementation branch exists.
- The only network-named branch is `lane/ui-network-v5`; it is currently 58 commits ahead and 122 behind main. It is a visual donor, not a safe backend base.
- The current public site still serves Network V5 from the old UI lane. That page grades eight process/heartbeat loops; it is not the requested connection instrument.
- The deployed feed code is still the separate `lane/lead5-feed` release. It reports useful WSS connection evidence, but GitHub records that Feed → Machine books were still REST-sourced/stale at the last measured handover.
- Worker 6 has no new branch or commit visible after the Worker 5 handover. GitHub therefore cannot prove what Worker 6 is presently grinding locally. Network V1 must not wait on it.

## What the finished page will show—in plain words

It will **not** show a full AWS datacenter, VPC map, subnets, routers, or every server process.

It will show the few connections this product actually depends on:

1. **Your browser → protected website** — can the owner reach the site and how long did it take?
2. **Cloudflare → EC2 UI** — is the tunnel connected, receiving requests, retrying, or losing packets?
3. **EC2 → Binance Spot REST** — is the public Spot API answering, how fast, and is Binance rate-limiting it?
4. **EC2 ↔ Binance Spot WebSocket** — is live Spot book data connected and fresh; are frames being dropped or reconnecting?
5. **EC2 → Binance Futures REST** — the same for USD-M public REST.
6. **EC2 ↔ Binance Futures WebSocket** — the same for USD-M live books.
7. **Feed → Machine** — the crucial internal handoff: is the trading machine receiving fresh books, or is the socket healthy while the machine still sees stale prices?
8. **News fetcher** — one simple aggregate status, linking to the News page.

Signed account reads, orders, Secrets Manager and SSM appear only as `last used / last result` when genuine activity occurs. The Network page does not create fake signed calls or trades to keep them green.

### Visible layout

- **Top summary:** overall state, worst connection, unknown count, oldest evidence, and last real change.
- **Main connection story:** Browser → Cloudflare → AWS → Spot/Futures/News → Feed → Machine. Each real wire is green, amber, red, stale or unknown with a reason.
- **Time lanes:** 1H, 6H and 24H history for latency, message age, reconnects, frame drops, rate-limit events and stale periods. Each wire keeps its own units.
- **Worst-first table:** the unhealthy or unknown connection rises to the top.
- **Click drawer:** click a connection to see what flows through it, its last sample, why it has that status, and the evidence source.
- **Change ledger:** only meaningful transitions such as disconnect, recovery, stale data, 429/418, dropped-frame increase or collector stop.
- **Monitoring controls:** view refresh, history range, monitor interval, pause one monitor and one public probe-now action. These control monitoring only; they do not disconnect Binance, restart the tunnel, or touch trading.

## What exists now versus what must be built

### Already exists

- Cloudflare Access and Tunnel to the EC2 box.
- The protected website and application shell.
- A current Network V5 page, though it shows process heartbeats rather than the product connection story.
- A host-side mirror, telemetry service, News service and UI service.
- Real feed connection evidence in `watch/feed_state.json` on the host.
- Real Spot and Futures WSS traffic in the deployed feed lane.
- Deployment scripts over SSM.
- UI reference assets on the V5 lane.

### Still absent

- Mirroring `feed_state.json` into the UI-safe data directory.
- A canonical Network recorder collecting Cloudflare, Binance REST, feed and origin measurements.
- Bounded history storage for those measurements.
- `/api/network`, history and event endpoints.
- The new connection story, table, drawer and ledger wired to those endpoints.
- Real monitoring controls and receipts.
- A deployed failure/recovery acceptance run.

## Two mistakes to prevent

### 1. Do not replace the live V5 server with main's older server during NET-03

The new builder correctly found that current `main` lacks routes already served by the deployed V5 `serve.py`. Deploying main's server early could remove the existing telemetry, grace and polling endpoints.

Correct handling:

- Build from fresh `main` for runtime truth.
- Import only the exact current UI shell/server features required from `lane/ui-network-v5`.
- Deploy NET-01 mirror and NET-02 recorder without replacing the current web server.
- Run the combined new server on an unused loopback port and verify all old and new endpoints.
- Cut over the live 8080 service only when the combined page is ready; preserve a rollback copy.

### 2. Remove the per-card owner waiting

The builder currently plans to stop after NET-01 and wait for `go NET-02`. Repeating that seven times is why a one-day build becomes 2–4 days.

There should be only two owner gates:

1. Approve the static visual arrangement before the real page wiring is finalized.
2. Approve the final list of monitoring controls before those controls are enabled.

Everything else should continue automatically when the previous card has real AWS evidence and its stated checks pass.

## Corrected fast sequence

### Block A — real data foundation, about 1–3 hours

1. Read the actual deployed `feed_state.json` schema.
2. Mirror it atomically and fix the manifest field mismatch.
3. Freeze the `/api/network` contract.
4. In parallel, build the recorder/history module and the API/query module against that frozen contract.
5. Deploy the recorder and prove current-state/history rows on AWS.

### Block B — useful visible page, about 3–6 hours total elapsed

1. Keep the existing application shell and News page.
2. Replace only the inner Network page.
3. Build the connection story and worst-first table first.
4. Wire them directly to real `/api/network` data.
5. Add the drawer and transition ledger.
6. Deploy to the protected URL. Missing evidence must appear as `Unknown`, never fake green.

At this point the owner already has the useful product.

### Block C — controls and acceptance, about 6–12 hours total elapsed

1. Add only monitoring controls with real endpoints and receipts.
2. Stop only the Network recorder and prove the page becomes `Stale`.
3. Restart it and prove recovery after two successes.
4. Measure CPU, memory, database growth and endpoint size.
5. Verify through `arbitrage.cosmosgeek.org` and record the sanitized transcript.

The 6H and 24H views continue filling after deployment; they do not block completion.

## Paste to the Network builder

> Go NET-01 and continue automatically through NET-03 when each prior card has real AWS evidence; do not wait for a separate owner word between those cards. Freeze the API contract first, then run the recorder/API/static visual work in parallel on disjoint files. Preserve the currently deployed V5 routes: do not replace live `serve.py` with main's older server. Stage and verify the combined server on another loopback port, then cut over atomically. Show me the static connection-story arrangement once before final NET-04 wiring, and show me the monitoring-control list once before NET-06. The useful target is real connection data on the protected site today; 24-hour history fills afterward and is not a completion gate. Report only current card, real artifact, first failure, final result, and next move.

## Bottom line

They are pointed at the correct product, but nothing new has been built yet. The scope is a focused connection cockpit, not a datacenter console. One to three hours is realistic only for a visual mock; four to eight focused hours is realistic for a useful real-data page, and roughly one concentrated day is reasonable for the complete V1. Two to four days is ceremony and waiting, not an unavoidable engineering requirement.
