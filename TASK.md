# TASK.md — current work orders

FIRST — FIX THE BLOCKER (found 2026-08-13, LOGS.md): src/futures_hand.py's
flat-truth check demands exactly one position row, but Binance USD-M V3
position responses are documented SPARSE — a flat position may legitimately
return zero rows, so the code cannot prove flat and honestly blocks. ONE
Codex worker, code work only, no live act: repair on main — treat an
absent/zero-row response as flat-compatible truth per the documented V3
shape; change nothing else; keep every other guard; commit+push; LOGS row.
The idle task branch dd474b26 stays unmerged. THEN the NOW order below
proceeds.

NOW — LIVE PROOF: FUTURES MINIMUM OPEN/CLOSE (owner reported Futures enabled
2026-08-12; exchange capability remains UNVERIFIED until fresh human-executor
preflight). ONE Codex worker chat, ONE
worktree, committed tree only (record exact main SHA as QUANT_SHA).
Mandatory worker instruction: "One atomic task or named manifest lane, one
worker chat, one worktree; a multi-lane order uses a separate worker/worktree
per named lane. Inside that worker, saturate every available child-subagent
slot with safe, useful, independent work now and refill after each finish,
milestone, and compaction. Serial only for true dependencies or reconciliation
of one human-initiated live lifecycle. Codex never initiates live money actions.
Do not invent gates. Use gpt-5.6-sol, ultra reasoning, standard speed, and full
local access where settings exist. Record any new mistake before continuing:
write only within named scope, or if strict read-only/no ownership, first relay
one FABLE-CLEAN correction to the canonical owner. Final report must be
FABLE-CLEAN."
Execution boundary: the authorized human-controlled executor owns steps 1-3 below. The
Codex worker owns preparation, non-mutating tests/audits, and step 4 receipt
reconciliation; it says this boundary before accepting work and never initiates
or mutates that live lifecycle.
1. HUMAN EXECUTOR: run the committed signed GET-only capability preflight. If
   canTrade is not true, stop before transfer and record BLOCKED.
2. HUMAN EXECUTOR: only after canTrade=true, use src/wallet.py for one receipted
   Spot→USD-M internal transfer inside its 10-USDT ceiling.
3. HUMAN EXECUTOR: src/futures_hand.py ONE minimum open + reduce-only close on
   its validated plan symbol; own client-ID prefix; notional <= 10 USDT;
   leverage 1x; its own preflight re-measures canTrade before anything.
4. CODEX WORKER: reconcile one receipt per act on the host + LOGS.md rows
   (LEAD-CLEAN). STOP after one open/close. No repeat or basis cycle here.
Disjointness: the authorized human-controlled executor owns the plan coin and wallet action
until flat and receipted; the Codex worker never initiates or mutates that live
lifecycle and only reconciles its evidence. UNKNOWN
binds: never resend blind, report exposure, no repair trades.
NEXT BUILD after this proof lands: the coordinated basis cycle (spot leg +
futures leg), cut as a fresh task.

DONE 2026-08-12: first money loop CLOSED at d1b204b (numbers in LOGS.md).
Codex never re-runs src/spot_roundtrip.py live; owner permission does not
override the platform boundary.

STANDING PERMISSION (owner law 2026-08-12): inside the funded sub-account, an
eligible authorized executor runs committed code live without another owner word.
For live-account capability, owner words cover adding money and new powers
(borrow, leverage, a new key permission). Other owner-controlled project changes
remain governed by their named rules. Parallel live execution is allowed under
DISJOINTNESS: no two authorized human-controlled live processes use one coin or
wallet action; each lifecycle has its own client-ID prefix and ceiling; an open
position keeps its coin exclusive until flat.

## EVENT CONTRACT v1 — frozen 2026-08-12; grows only on owner word; 1 page max
One JSON object per line, appended to events.jsonl on the execution host; the
UI reads this and nothing else. Fields: ts (UTC ISO), sha8, kind (run_start |
order_submitted | order_terminal | run_verdict | balance_snapshot | gap_read),
run_id, symbol, side?, client_id_last4?, order_id_last4?, executed?, quote?,
fees{asset:amt}?, balances{asset:free}?, residual?, verdict?, note?.
Factual states only — no derived opinions, no predictions. Optional fields per
kind; nothing else enters v1. Past one page it is a framework: cut it.

## LANE MANIFEST — frozen 2026-08-12; newest direct owner word may revise
Dispatch rules: each named manifest lane is a separate atomic task with ONE
worker chat and ONE worktree under Desktop/worktrees/<lane>; L1-L10 therefore
do NOT belong in one chat. Inside each lane, the worker uses child subagents and
never splits that lane into peer chats. Base = main HEAD at dispatch. Each
dispatch names lane, base SHA, exact files, and expected evidence, and includes
the mandatory worker instruction above.
Look-ahead lanes create no new repo documents; the integrator writes their
required LOGS.md row after disposition and records any relayed mistake first.
A completed incorporated task, or a
recorded deliberate non-merge, retires by recording disposition, verifying
clean, moving its exact worktree folder to Trash, pruning the stale Git
registration, confirming branch/commits remain, THEN archiving. One LOGS.md row per lane.

L1 MARKET-READS   | src/market_reads.py | live Spot + USD-M mark/funding reads,
   computed basis-gap rows | evidence: one read-only run's printed output
L2 FUTURES-HAND   | src/futures_hand.py | minimum USD-M open + reduce-only
   close pair, client-id + query-before-retry discipline; NO live order
   in-lane (live proof belongs to the authorized human-controlled executor) |
   evidence: code + validation as far as the key allows; honest BLOCKED row
   if futures permission is not yet on the key
L3 WALLET         | src/wallet.py | Spot+futures balance reads; internal
   transfer mechanics coded and receipted, NOT executed in-lane |
   evidence: one read-only balance snapshot
L4 EVENT-EMITTER  | src/events.py | emit contract-v1 lines from a run receipt |
   evidence: events.jsonl produced from the real receipt fixture
L5 CORRECTNESS    | strict read-only | adversarial re-check of spot_roundtrip.py
   and L1-L4 order discipline | evidence: FABLE-CLEAN final relay; integrator logs
L6 LOOK-AHEAD reverse-basis  | strict read-only | FABLE-CLEAN final relay; integrator logs disposition
L7 LOOK-AHEAD volume map     | strict read-only | FABLE-CLEAN final relay; integrator logs disposition
L8 LOOK-AHEAD old-UI survey  | read-only vs github.com/cosmosgeek0/
   arbitrage-cosmos | candidates relayed in FABLE-CLEAN final; integrator logs
   disposition; NOTHING imported
   without the owner's per-item permit (owner word 2026-08-12)
L9 UI GROUNDWORK  | the UI WORK chat, designing WITH the owner | fixture =
   the real receipt + contract v1; no generic cards; every control hits a
   real endpoint; factual states only; "ready enough", not the whole UI
L10 INTEGRATOR    | merges lane branches to ONE candidate SHA after L5
   reports | evidence: candidate SHA + clean build

LIVE PROOF ORDER — step one is the NOW order at the top (owner reported enabled;
capability remains UNVERIFIED until fresh preflight); step two, the basis cycle, is cut as a
fresh task after step one's receipt lands.
