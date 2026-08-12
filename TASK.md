# TASK.md — current work orders

DONE 2026-08-12: first money loop CLOSED at d1b204b (numbers in LOGS.md).
Never re-run src/spot_roundtrip.py live without a direct owner order naming a
SHA — a repeat spends real money.

THE ONE APPROVAL (owner law 2026-08-12): only NEW UNPROVEN code touching real
money for the FIRST time needs the owner's word. Reading the exchange, writing
code, running lanes, building UI, merging — no permission, ever. A capability
proven live once trades on its own inside its balance. One sub-account = one
live hand today; parallel live hands need more sub-accounts (owner act each).

## EVENT CONTRACT v1 — frozen 2026-08-12; grows only on owner word; 1 page max
One JSON object per line, appended to events.jsonl on the execution host; the
UI reads this and nothing else. Fields: ts (UTC ISO), sha8, kind (run_start |
order_submitted | order_terminal | run_verdict | balance_snapshot | gap_read),
run_id, symbol, side?, client_id_last4?, order_id_last4?, executed?, quote?,
fees{asset:amt}?, balances{asset:free}?, residual?, verdict?, note?.
Factual states only — no derived opinions, no predictions. Optional fields per
kind; nothing else enters v1. Past one page it is a framework: cut it.

## LANE MANIFEST — frozen 2026-08-12 by the Lead; commander text may correct later
Dispatch rules: ONE dispatcher chat spawns every lane as a fresh parallel
Codex chat in its own worktree under Desktop/worktrees/<lane>, base = main
HEAD at dispatch (dispatcher records the exact SHA per lane). Each dispatch
names: lane, base SHA, exact files, expected evidence. Look-ahead lanes are
read-only and create NO repo markdown — notes live in their branch. Finished
worktree: git worktree remove, THEN trash the folder. One LOGS.md row per lane.

L1 MARKET-READS   | src/market_reads.py | live Spot + USD-M mark/funding reads,
   computed basis-gap rows | evidence: one read-only run's printed output
L2 FUTURES-HAND   | src/futures_hand.py | minimum USD-M open + reduce-only
   close pair, client-id + query-before-retry discipline; NO live order
   in-lane (live proof belongs to the owner-worded money-toucher) |
   evidence: code + validation as far as the key allows; honest BLOCKED row
   if futures permission is not yet on the key
L3 WALLET         | src/wallet.py | Spot+futures balance reads; internal
   transfer mechanics coded and receipted, NOT executed in-lane |
   evidence: one read-only balance snapshot
L4 EVENT-EMITTER  | src/events.py | emit contract-v1 lines from a run receipt |
   evidence: events.jsonl produced from the real receipt fixture
L5 CORRECTNESS    | no new files | adversarial re-check of spot_roundtrip.py
   and L1-L4 order discipline | evidence: findings list in branch notes
L6 LOOK-AHEAD reverse-basis  | read-only | spec notes in branch only
L7 LOOK-AHEAD volume map     | read-only | dead-token ignition map, branch only
L8 LOOK-AHEAD old-UI survey  | read-only vs github.com/cosmosgeek0/
   arbitrage-cosmos | a candidate-import LIST only; NOTHING is imported
   without the owner's per-item permit (owner word 2026-08-12)
L9 UI GROUNDWORK  | the UI WORK chat, designing WITH the owner | fixture =
   the real receipt + contract v1; no generic cards; every control hits a
   real endpoint; factual states only; "ready enough", not the whole UI
L10 INTEGRATOR    | merges lane branches to ONE candidate SHA after L5
   reports | evidence: candidate SHA + clean build

LIVE PROOF ORDER after integration — owner-worded, ONE money-toucher:
minimum futures open/close, then one complete minimum-size basis cycle.
OWNER ACT LATER (Old Lead calls the timing): futures permission on the key.
