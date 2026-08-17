# NOW.md — current orders and measured truth (one page, rewritten in place; worker 7.0, 2026-08-17 ~08:0xZ)

## Orders in force
- MACHINE STAYS SEALED (STOP-JOBS present) and FLAT. No lane landing authorizes a deploy, order, transfer, leverage change, or live proof. Each deploy and each live proof runs on ONE owner line. | MEASURED owner word
- GOVERNING DOC: Downloads/QUANT-FRESH-DIRECTION-2026-08-17.md (owner-adopted audit). Network V1 CLOSED and FROZEN at e5e9893b. Worker 6.0 seat closed (usage limit); worker 7.0 is the sole money Lead. Worker 10 = read-only reviewer unless it declares a unique Git artifact. Shelves (lane/lead5-hand, shelf/*) are donors, never merged wholesale. | MEASURED owner word
- REVERSE LIVE result 2026-08-17: the 4th owner press RAN — job claimed by the deployed runner, refused at its first fact gate, receipt written, machine self-resealed, account FLAT (see truth). Reconciliation of the UNKNOWN receipt and the borrow-facts read defect are OPEN and need owner words; no repair was performed per the money-only order. | MEASURED
- NEXT PHASE on the owner's word only: reconciliation/repair card for the receipt gate, then the canonicalization plan from the fresh-direction doc (exact proven release onto one branch, no behavior change, no wholesale merges). | MEASURED owner word

## Measured truth
- RECEIPT reverse-rproof-051e4f11103e.json (sha 585e0ecda9a7ef239cf52625fe6d17ead79bc725): op REVERSE_MECHANICS_ROUNDTRIP ZROUSDT cap 8, intent …7ab6, client tag …e5f6; started 07:10:06.45Z finished 07:10:07.66Z (1.2 s); verdict UNKNOWN, reason REVERSE_BORROW_FACTS_UNREADABLE; mutation_attempts 0, legs [], plan null, market {} — refused BEFORE any borrow, order, or transfer; governor_release PENDING. | MEASURED receipt read
- PRESS CHAIN 07:09:30Z (owner's press, repaired detached launcher): guards ok, signed flat read FLAT, ACCEPT-ONE written (max 8, 15 min), job composed + placed, ARMED (STOP-JOBS lifted). Tick claimed it 07:10:01Z. STOP-JOBS re-placed 07:10:07Z — same second as the receipt. The 2026-08-16 composer repair (output capture, OUTPUT_UNAVAILABLE) held: composer produced the job cleanly. | MEASURED fire log fire-20260817T0709xxZ
- RUNNER since 07:11Z: every one-minute tick logs JOB-RUNNER HALTED unreconciled receipt or lock, exit 1 — freeze-on-UNKNOWN posture holding; no new job can run until the receipt is reconciled. | MEASURED jobs.log
- FLAT read ~07:5xZ (worker 7, signed GET-only, preflight_sr3.py verified GET-only by code read): spot USDT 84.36656520 (+0.01643800 vs the 2026-08-16 19:00Z read — cause not established, no trade rows exist anywhere; LDUSDT 883.79 Earn distributions are a plausible source, INFERRED), USD-M USDT 10.97436273, cross-margin borrowed 0 interest 0 liability 0 marginLevel 999; 0 open orders and 0 positions on spot, USD-M, and cross-margin. VERDICT FLAT. | MEASURED
- VERIFY (worker 7 + one codex row): host RELEASE = 585e0ecd; deployed hashes job_runner 15f6d26a / margin_orders 0e160f6a / reverse_mechanics 3c5f5e6f all match repo blobs; worktree lead6-rev clean at exactly 585e0ecd = origin/lane/lead6-rev; host probe scripts byte-match the staged copies (fire_reverse_once 331fa59a, launch 96fcf6b7, poll badc3fdc). | MEASURED
- Stale on disk, inert: ACCEPT-ONE created 07:09:30Z expired 07:24:30Z (fire script removes stale tickets on the next press); job file + claim file for …103e remain as the frozen job's record. | MEASURED
- Feed/stream state unchanged from 2026-08-16 evening: spot dropped_frames 0, gap rows OK 36/400, book_age floor ~2-3 s (feed 1 s write cadence + snapshot lag). Shadow still evaluates (latest row 06:14Z, would_intent False). | MEASURED earlier proofs, not re-measured today

## Known limitations
- The borrow-facts read the runner refused on (REVERSE_BORROW_FACTS_UNREADABLE) is undiagnosed — receipt shows the typed refusal only; the preflight's own maxBorrowable GET read fine minutes later (ZEN 1.99). Diagnosis is a separate card on the owner's word. | MEASURED refusal, UNVERIFIED cause
- Runner reconciliation path for an UNKNOWN pre-mutation receipt has never been exercised; unfreezing procedure needs its own careful card, not an ad-hoc poke. | INFERRED
- Sub-second book age remains out of reach by design (feed cadence + snapshot lag); an unchanged quiet book still reads as old age. | MEASURED earlier

## Source basis
- Host reads over SSM 2026-08-17 ~07:5x-08:0xZ (w7_verify/w7_receipt/w7_rcpt2 transcripts in Desktop/worktrees/lead6-rev/lane-logs/host/), fire logs, jobs.log, receipt JSON; local git + sha256; codex read-only digests c1-c3 (scratchpad). | MEASURED
