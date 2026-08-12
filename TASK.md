# TASK.md — current work orders

THE OWNER IS NEVER THE EXECUTOR (owner word 2026-08-13). The machine runs the
money path: live lifecycles start from the execution host itself — a
timer/service running committed code under standing permission. No human
run-steps, no per-trade judgment by any chat. Human-executor structures are
legacy ceremony: revert immediately.

TASK A CANCELLED (2026-08-13, measured by the Lead): the claimed sparse-V3 bug
is FALSE — position_amount() already returns zero for a zero-row response,
which is flat. No code fix exists to make. The prior BLOCK verdict was wrong.

NOW — TASK B, one worker: the machine
trigger. One small host-side runner (timer/service on the EC2 host) that
executes the committed sequence ONCE — signed GET preflight (stop + record
BLOCKED if canTrade is not true) → src/wallet.py one receipted Spot→USD-M
transfer inside its 10-USDT ceiling → src/futures_hand.py ONE minimum open +
reduce-only close, validated plan symbol, own client-ID prefix, notional <=
10 USDT, leverage 1x — receipts + events.jsonl on the host. Arm it; the HOST
executes; no chat and no human presses the trigger. Then reconcile receipts
into LOGS rows. Committed tree only (QUANT_SHA = main HEAD at arm time).
UNKNOWN rule binds; disjointness binds: this lifecycle owns its plan coin and
the wallet action until flat and receipted.

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
   in-lane (live proof belongs to the host runner) |
   evidence: code + validation as far as the key allows; honest BLOCKED row
   if futures permission is not yet on the key
L3 WALLET         | src/wallet.py | Spot+futures balance reads; internal
   transfer mechanics coded and receipted, NOT executed in-lane |
   evidence: one read-only balance snapshot
L4 EVENT-EMITTER  | src/events.py | emit contract-v1 lines from a run receipt |
   evidence: events.jsonl produced from the real receipt fixture
L5 CORRECTNESS    | strict read-only | independent skeptical re-check of spot_roundtrip.py
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
