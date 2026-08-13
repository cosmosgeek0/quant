# TASK.md — current work orders

OWNER ORDER 2026-08-13, WAVE 2 (direct, in-channel; supersedes the wave-1
lane manifest; the 2026-08-13 reset facts and cancellations still stand):
- DO NOT arm or deploy the Basis cycle: it has no live executor yet.
  Nothing executes live until the owner's next word.
- The seven cancelled Task-B edits are STASHED in Desktop/quant as
  "CANCELLED-TASK-B-DO-NOT-APPLY" — never apply, never drop. Desktop/quant is
  clean and current again; wave work still runs in separate worktrees.
- Base for every wave-2 lane: main 0cffc89636743a32838d0163aee2ad3e26bd9bd0.

WAVE-2 LANES (one worker, one worktree under Desktop/worktrees/<lane>, branch
lane/<lane>; offline or public-unsigned only; no credentials on the Mac; no
POST, transfer, leverage change, or order anywhere; commit to lane branch, no
push, no merge; one LOGS.md entry each):
CAPABILITY-FIX | src/futures_hand.py | preflight canTrade must come from
  GET /fapi/v1/accountConfig; /fapi/v3/account stays for balances+positions;
  regression tests covering the prior false-negative | offline tests only
BASIS-EXECUTOR | src/basis_executor.py | ONE-SHOT executor connecting the
  existing pieces: live basis read -> Spot buy -> confirm actual net base ->
  Futures short that actual quantity -> confirm actual Futures fill ->
  reduce-only close exact short fill -> sell actual remaining Spot. Partial
  fills handled; uncertain orders queried by client ID; no host_once, timer,
  daemon, or service | offline tests only in this wave
VOLUME-V2      | src/volume_observer.py | closed-candle volume-surge
  detection; restart-safe paper positions; live Spot-USDT universe;
  PAPER ONLY | public unsigned GETs + offline tests
UI-RUNTIME     | ui/ | serve through localhost HTTP; read ACTUAL Spot, Basis,
  Futures, and Volume output files; remove fixture-only and file://
  dependence; read-only | headless evidence

HOST CHECK (parallel, one worker): one signed GET-only read on the execution
host — Futures accountConfig, account+balances, position mode, symbol
configuration, open orders, position risk, Spot balances. No mutation of any
kind. Digest returns plain rows only.

INTEGRATION: the Lead merges passing lanes after adversarial review and
reports: four lane SHAs, merged SHA, canTrade, Spot+Futures USDT, position
mode, leverage, positions, open orders, and the exact remaining blocker.

## EVENT CONTRACT v1 — frozen 2026-08-12; grows only on owner word; 1 page max
One JSON object per line, appended to events.jsonl on the execution host; the
UI reads this and nothing else. Fields: ts (UTC ISO), sha8, kind (run_start |
order_submitted | order_terminal | run_verdict | balance_snapshot | gap_read),
run_id, symbol, side?, client_id_last4?, order_id_last4?, executed?, quote?,
fees{asset:amt}?, balances{asset:free}?, residual?, verdict?, note?.
Factual states only — no derived opinions, no predictions. Optional fields per
kind; nothing else enters v1. Past one page it is a framework: cut it.
