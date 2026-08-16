# HANDOVER-LEAD.md — worker 5.0 seat CLOSED on the owner's STOP-WIDTH order, 2026-08-16 ~14:2xZ → next Lead (worker 6.0)

Boot: CLAUDE.md (auto-loads when the chat's cwd is inside a checkout) → NOW.md → newest LOGS.md rows → this file → lane-logs/ of the two lead5 worktrees. Disk beats anything a chat remembers. Every row below is labeled.

## 1. Law you must hold first (visible in the law files, not hidden)
- CLAUDE.md top section WIDTH (owner word 2026-08-16): width is a few workers, one bit each, about 500 lines per card, net-negative or flat; never a flood; one file owner per card; the Lead verifies each landing with its own hands and commits only what is verified AND reviewed clean. The earlier "PARALLEL ARMY 10-50 workers" section was Lead-invented and is gone from lane and main. | MEASURED (CLAUDE.md on main 795e23f)
- The owner's paste IS the width. Do not widen on your own reading of urgency. | MEASURED owner order
- Machine SEALED (STOP-JOBS present) and FLAT; nothing on any lane authorizes a deploy, order, transfer, leverage change or live proof; every first live proof of a migrated seam runs on the deployed host on ONE owner line. | MEASURED record

## 2. Where the code is
- origin/main 795e23f = release 2 + runner receipt repair f4fa89fa + records + OWNER-CAPABILITIES.md + WIDTH law + NOW/LOGS truth. No code merged from lead5 lanes. | MEASURED
- lane/lead5-feed 7b283a0 (worktree Desktop/worktrees/lead5-feed) = main + FD1 (book tickers) + FD2/FD3 (one bounded FIFO event queue 4096+256, single drain, cumulative drop counter) + FD4 (perp !bookTicker on wss://fstream.binance.com/stream, perp klines stay on /market/stream, spot per-symbol <sym>@bookTicker shards ≤200). Reviews: FR2 3 → FR5 0 MERGE-SAFE, FR3 DEPLOY-SAFE, FR4 MERGE-SAFE, FR6 DEPLOY-SAFE. DEPLOYED to the host (see 3). Not merged to main. | MEASURED
- lane/lead5-hand 769b732 (worktree Desktop/worktrees/lead5-hand) = SHELF, NOT FOR MERGE. Contains: study/design/EXECUTION-HAND-INTERFACE.txt (frozen interface v1, 131 rows incl. 8 Lead decisions), src/execution/ (binance_http 220, binance_spot 259, binance_futures 259, binance_margin 193, binance_transfer 59 non-blank; 102 tests green), migrated callers spot_roundtrip 512→418, wallet 688→543, mechanics_reconcile 961→861, basis_position 4470→4274 (SZ3, SR1 MERGE-SAFE), aliveness.sh +40 (AL1 feed-blind alarm, not deployed), OWNER-CAPABILITIES.md, TSP1/TSP2 pins, maps HX0-HX5/TS1/TR1, plans W1p-W7p, shards/set_leverage_1x__W8.py. Committed src+scripts delta vs main: +509 non-blank. | MEASURED
- STASH in worktree lead5-hand (stash@{0}), also pushed as ref shelf/lead5-hand-wip 16609ba — UNREVIEWED: job_runner 6985→6437 (−548: margin/futures/spot paths on the adapters, proof-module imports gone, WR6B F1/F3 closed, fixtures retargeted, 226/227 with one e2e fixture red), basis_executor 1341→1091 (−250, gate red: pre-W7 basis_position fixtures), futures_hand shard 834→539 (−295, 57/57 in shard), spot_roundtrip WR1b repairs (43/43), shards/job_runner__W6B/C/D.patch. Landing all of it would take the shelf lane to ≈ −570 net. | MEASURED
- Two TEMPORARY shims are committed on the shelf: basis_executor.fees_by_asset (7 lines, dies with W4) and spot_roundtrip.balances_of (3 lines, dies with W6D). | MEASURED
- Other lanes: lane/lead2-v2fix 71eb195 (worker 4.0's, frozen history: reverse op MO4 with MRV16 F1/F2 open, FD1 origin), shelf/add-transition-v1, lane/add-recut, worktrees ui-* (UI seat). Canonical Desktop/quant is DIVERGED (UI seat) — never run pipelines there. | MEASURED
- Stale worktree registration Desktop/quant/worktrees/rec-main (b9716fa, worker 4.0's temp) — prune when convenient. | MEASURED

## 3. Host state (MEASURED by host reads; last proof 13:22Z)
- RELEASE 7b283a06d2db7cb773827e74d2f25ec13e10f405 (feed FD1-FD4), deployed by the atomic tool 13:1xZ; feed daemon restarted on it 13:18Z via the stop file (old exited ~1 s; tick relaunched in 75 s; RESTARTED health row). job_runner bytes on host = f4fa89fa (unchanged by the feed releases). | MEASURED
- Feed DELIVERS: spot bid+ask on 480 symbols, perp on 524; both ticker sockets healthy with fresh last_message_ms; snapshot age ~1.3 s; dropped_frames spot 356 / perp 62 in ~4 min under the bounded queue. | MEASURED
- STREAM STILL REST-SOURCED: stream_state book_age_ms 10.7-15.7 s, executable_gap_watch 357/357 SPOT_QUOTE_STALE, reverse shadow blind. Cause NOT diagnosed (suspects: feed max-age 5000 ms at pass time, spot receipt-time vs perp exchange-time skew gate, same-source rule). THIS IS THE NEXT SMALLEST MOVE (one card on src/market_stream.py book selection, ≤50 lines, prove sub-second book_age_ms + OK gap rows on host). | MEASURED symptom; cause UNVERIFIED
- Measured stream shapes (host probes 12:1x-12:3xZ, keep): perp !bookTicker delivers only on /stream; perp klines only on /market/stream; spot !ticker@arr silent on every host/port/path; spot <sym>@bookTicker and <sym>@ticker deliver in ~1 s; a 200-stream spot bookTicker shard ≈ 324 frames/s. | MEASURED
- Machine SEALED (STOP-JOBS present) + FLAT at the last read (11:00Z: borrowed 0, interest 0, liability 0, no position); this seat placed no job/order/transfer/leverage/ticket; no fresh signed flat read was taken — take one before any live word. | MEASURED / UNVERIFIED as labeled

## 4. Open reviews and what is NOT clean
- No adapter has a clean round-2 verdict: round-1 findings HTTP 5 (SRS1a/b), Spot 2+5 (SRS2a/b), Futures 4+6 (SRS3a/b), Margin 4 (SRS4a), Transfer 4 (SRS4b); every repair landed (S1r/S1r2/S1r3, S2r/S2r2/S2r3, S3r/S3r3, S4rm, S4rt) with suites green — a fresh-eyes round-2 pass per adapter is owed before any seam. | MEASURED
- Migrations: spot_roundtrip WR1a MERGE-SAFE, WR1b 5 findings (repair W1r in the stash); wallet (W3b) and mechanics (W5b) unreviewed; runner (W6A/W6A2) WR6B 3 → 2 closed, no re-review; executor (W4b) unreviewed and red; futures_hand (W2b) shard unreviewed. | MEASURED
- Owner words open: reverse gate / borrow for the live reverse round trip (MRV16 F1 leverage-POST authority, F2 cross-margin open orders — repairs NOT started; the reverse op lives on lane/lead2-v2fix); re-arm; volume PARK vs one probe (TR1 move list: 33 volume-only study files / 17,365 lines — nothing moved); donor permit numbers; BNB float; tuner advisory. | MEASURED record

## 5. The order the owner set for what comes next
- Seams from the shelf reach main one per owner paste, each re-running its proven receipt live on the host, only AFTER the reverse round trip. Suggested seam order when the owner opens it: (a) feed→stream acceptance (small, unlocks the shadows), (b) reverse round trip repairs + live proof (owner line), (c) shared hand seams smallest-first: HTTP+Spot with the BTC round-trip re-proof, Futures with the TST re-proof, Margin/Transfer, then runner and executor. | INFERRED (owner's stated order for (a)/(b); seam order is Lead judgment)

## 6. How to grind (unchanged method, narrower width)
- Cards: `codex exec --skip-git-repo-check -s workspace-write -C <worktree> -c sandbox_workspace_write.network_access=false < lane-logs/cards/<ID>-task.txt > lane-logs/<ID>.log 2>&1` in the background; every card starts with `/fast`; one file owner, ~500 lines, exact files, net-negative or flat, +/- reported; report file ≤10 rows. Verify with your own hands (run the named suites, read the diff for money files) before any commit; commit only verified AND reviewed clean; fresh-eyes review cards for money files (attack list, CONFIRMED/PASS with file:line + trigger, MERGE-SAFE / FINDINGS-BLOCK). Language law: clean words everywhere incl. cards. | MEASURED method
- Anything that cannot land at once builds in a shard file (shards/…) — the shared worktree with parallel writers on import-coupled files broke importers transiently today (in-place W8; W1's deleted exports). Type maps must carry every field the live callers read (map rows 128-129 were revisions after refusals). | MEASURED scars
- Host rails: aws-connect/xfer.sh (files) + ssmrun.sh (short scripts; WAIT ≥ runtime); deploy = scripts/host_deploy_atomic.sh via manifest `sha256 relpath mode`, payload built with COPYFILE_DISABLE=1, RELEASE = 40-hex SHA of a pushed commit; feed restart = touch watch/feed.stop → wait exit → rm → tick relaunches; proof = the Lead's proof-feed.sh pattern (feed_state connections + counts, stream_state book_age_ms, health rows, gaps ledger). | MEASURED

## 7. Progress bar at close (print at every stop)
```
FEED           ████████████████░░░░  80%  delivering on host (release 7b283a06); stream acceptance = next move
HAND (shelf)   ██████████░░░░░░░░░░  50%  adapters built + repaired, no clean round-2; 4 callers migrated (3 committed, runner/executor/futures_hand stashed)
REVERSE LIVE   ░░░░░░░░░░░░░░░░░░░░   0%  MRV16 F1/F2 repairs not started; borrow gate = owner word
VOLUME         ████████████████████ 100%  parked; move list ready
──────────────────────────────────────────
OVERALL        ███████████░░░░░░░░░  ~55%   fleet: 0
```
Outgoing seat (worker 5.0): closed. History questions: the record on disk (NOW.md, LOGS.md, lane-logs/*-report.txt in the two lead5 worktrees).
