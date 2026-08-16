# HANDOVER-LEAD.md — worker 4.0 seat CLOSED on the owner's wind-down order, 2026-08-16 ~11:35Z

Boot for the next Lead: CLAUDE.md (auto-loads) -> NOW.md -> newest LOGS.md rows -> this file -> lane-logs/cards/. Disk beats anything a chat remembers. Every row below is labeled.

## 1. Where the code is
- LANE HEAD: lane/lead2-v2fix f7cecae (+ the wind-down records commit on top of it) — 40 commits since b9716fa; pushed. | MEASURED
- MAIN HEAD: origin/main f057856 = release 2 + the runner receipt repair f4fa89fa + records; nothing merged after release 2. | MEASURED
- HOST RELEASE: f0578561071f00b45c232065b61aadcca8a9feba (release 2, atomic tool OK, 10 files, gate lifted 10:4xZ); job_runner on host = f4fa89fa's bytes (unchanged by release 2). | MEASURED
- OTHER BRANCHES: shelf/add-transition-v1 c2440df (bespoke ADD, shelved after 5->7->13 findings); lane/add-recut 6794134 (RA+RB+RC re-cut, parked: 10 e2e fixtures still encode the old permissive over-fill; basis_position 7,038 lines there vs 4,151 on main). | MEASURED
- WORKTREE: Desktop/worktrees/lead2-v2fix clean except one untracked file study/tape_signature_precision.py (owner: leave untracked, do not commit). Canonical Desktop/quant is DIVERGED (UI seat) — never run pipelines from it. | MEASURED
- MACHINE: SEALED (STOP-JOBS present, STOP absent), FLAT (borrowed 0, interest 0, liability 0, no futures position, no locks), no live action since 09:39Z. | MEASURED at last host read ~11:00Z

## 2. The two landings the wind-down covered
- MO4 (runner reverse op REVERSE_MECHANICS_ROUNDTRIP consolidated onto the proven paths): lane f7cecae; job_runner 8,295 -> 7,688 non-blank; 242/242 own gate. Review MRV16 = FINDINGS-BLOCK, 2 findings, verbatim: (F1) "reverse dispatch calls _ensure_leverage_1x at src/job_runner.py:6038 before the six-leg loop; a seal injected on its symbol-config GET still allowed POST /fapi/v1/leverage, then the job refused with zero leg POSTs, because this seventh reverse mutation has no final _margin_post_authority check"; (F2) "margin legs POST /sapi/v1/margin/order at src/job_runner.py:5758-5760, but final truth reads standard Spot /api/v3/openOrders at 5636-5639; a supplied resting /sapi/v1/margin/openOrders row was never read and the run returned CLOSED". All else PASS (authority-at-POST on all six legs, disjoint ids, no re-borrow, measured quantities, seals, claim-once, caps, size table). NOT merged, NOT deployed, NOT run. | MEASURED
- FD1 (feed book tickers: futures !bookTicker, dedicated spot ticker socket, connection truth, +1 line): lane 1e1e8aa; 53/53 own gate. Review BR23 = FINDINGS-BLOCK, 1 finding, verbatim: "sustained USD-M !bookTicker frames can arrive faster than the single-event consumer; src/market_feed.py:1022 creates queue.Queue() with maxsize 0, src/market_feed.py:1051-1056 enqueues every frame, and src/market_feed.py:1325-1330 drains one event per loop, so backlog memory is unbounded". Streams/restart/connections/schema/rate PASS. NOT merged, NOT deployed; the host feed still delivers no book tickers. | MEASURED
- No repair cards were dispatched for either (owner order). | MEASURED

## 3. The finding that matters most (for whoever continues)
- The websocket feed has NEVER delivered book tickers: alive since 08-14 06:10, 656 symbols, every record's spot/perp bid/ask None ("with any ticker: 0"); the stream reports feed_used=yes yet sources every quote from the REST pass (~8-9 s old, every 2-3 min). All basis gaps and executable reads to date were REST-aged. Release 2's shadows therefore refuse every symbol as stale (357/357 SPOT_QUOTE_STALE on book_age_ms 7,708-9,172 ms vs the 5 s bound) — honestly. FD1 is the fix; it needs BR23's queue bound, then a fresh-eyes pass, then deploy + daemon restart, then verify "with any ticker" > 0 and sub-second book_age_ms. | MEASURED
- RELEASE-2-PLAN (lane-logs/cards/RELEASE-2-PLAN.txt): EXECUTED except the two executor commits (B14/B15) which were dropped from main because B15's test relies on a hook from the shelved B13 lineage; executor on main == host. | MEASURED

## 4. Where the size and duplication facts live
- SZ1 (delete-first audit of job_runner.py + basis_position.py): lane-logs/SZ1-report.txt — function-level table, duplicate guards, unread receipt fields, recovery branches no live event took, deletion plan (~700 runner lines named incl. a 373-line manual attested-release pair never used live). Runner: 36.5% execute / 63.5% other. | MEASURED
- MRV15 (lane-logs/MRV15-report.txt) L2 tables 1-3: helper-by-helper duplication map that MO4 executed (-607). | MEASURED
- Owner SIZE law is in AGENTS.md (Size section, commit on lane): line budgets on every card, +/- reported, repairs net-negative or flat. | MEASURED
- ADD re-cut interface map: study/design/ADD-RECUT-INTERFACE.txt (BA1, 120 rows). | MEASURED

## 5. Owner words still open (never assume)
- Reverse gate: borrow power was AUTHORIZED 08-15 for reverse basis; the live reverse round trip has NOT run; the machine hunts sealed. A live proof (<= 8 USDT, ICPUSDT chosen from the mirror: spot ask 2.277, both minimums 5, common qty 3 ICP ~ 6.83 USDT; EGLD fallback) needs: MRV16's two findings repaired -> fresh eyes -> main -> deploy -> the choreography lane-logs/cards/reverse-choreo.sh with scripts/reverse_proof_job.py (RQ1). | MEASURED plan; live UNVERIFIED
- Re-arm: criterion unchanged (a configuration clears fees + slippage on replay first, smallest cap same hour). | MEASURED record
- Volume: PARK vs one bounded seconds-exit probe — owner's word; the tape diagnostic was wound down (zero output after 1h40m; SSD reads stopped); plain-words note at study/design/VOLUME-TEST-PLAIN.txt. | MEASURED
- Donor permit numbers vs study/donor/PORT-PLANS.txt; BNB fee float; tuner stays advisory. | MEASURED record

## 6. Scars of this shift (each recorded in evidence/ or LOGS)
- macOS tar shipped ._ AppleDouble strays -> atomic deploy refused MANIFEST_STAGE_MISMATCH; the refused pre-flight left the DEPLOYING gate ~4 min (D3 moved pre-flight before the marker; tool re-shipped to host); build payloads with COPYFILE_DISABLE=1. | MEASURED
- Codex's own content filter killed a review card phrased "adversary can edit any byte / forgery lens" (MRV13b) — phrase such reviews as file-integrity / editable-field analysis. | MEASURED
- The bespoke ADD transition drew 5 -> 7 -> 13 confirmed findings over three review rounds; the pre-committed tripwire fired and it was shelved (re-cut parked). Divergent finding counts mean the shape, not the patches. | MEASURED
- freshness_ms is a composite (incl. OI-read age); book_age_ms was added so consumers can read quote freshness — and then the feed finding showed the quotes were REST anyway. | MEASURED
- Runner grew +1,310 lines for one op before the size law; consolidation took -607 back in one card. Budgets belong on every card. | MEASURED
- Fleet width: peak 9 codex live; 63 cards this seat; money files chain by review law — width lives in disjoint lanes and review lenses. | MEASURED

Outgoing seat (worker 4.0): closed. History questions: the record on disk.
