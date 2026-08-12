# TASK.md — current work orders

OWNER ORDER 2026-08-13 (direct, in-channel; supersedes TASK B,
OWNER_ORDER_PARALLEL_WAVE_2, and the screenshotted reset note):
- TASK B (host machine-trigger) CANCELLED. OWNER_ORDER_PARALLEL_WAVE_2
  CANCELLED — its L11-L30 plan never governs.
- Owner-fixed facts at reset: main = 72d88b565d67b038c9979facb6845d3e9e188c48;
  no bot deployed; no Futures trade has ever completed.
- The Task-B draft sits uncommitted in Desktop/quant and is QUARANTINED
  unchanged: no merge, delete, deploy, run, or repair of its modified files.
  All wave work runs in clean worktrees from the reset SHA.
- No host runner and no live action anywhere in this wave.
- Process truth at reset (measured 2026-08-13): no Task-B process on the Mac;
  no quant unit, timer, unit file, process, or cron entry on the host.

NOW — PRODUCT WAVE: four lanes, each one worker in one clean worktree under
Desktop/worktrees/<lane>, branch lane/<lane>, base = the reset SHA. Read-only
toward the world: unsigned public GETs only where the lane row names them; no
credentials, no signed calls, no orders, no transfers. Each worker commits to
its lane branch, does not push, does not merge, and prepends one standard
LOGS.md entry inside its own worktree.

BASIS-READ    | src/basis_read.py | executable prices: entry gap = USD-M bid
  minus Spot ask; exit gap = USD-M ask minus Spot bid; plus funding;
  Decimal-safe | evidence: one live public read printed + deterministic
  no-network test
FUTURES-SHORT | src/futures_hand.py | SHORT side: SELL open + reduce-only BUY
  close, capped notional, 1x; OFFLINE proof only — no network at all |
  evidence: deterministic tests pass offline
VOLUME        | src/volume_observer.py | public-data observer + paper
  lifecycle (paper entry, trail out) writing paper receipts inside the
  worktree | evidence: one public observation run + lifecycle test
UI            | ui/ | one read-only screen from real receipts
  (ui/fixtures/events.jsonl, contract v1) + live public prices; real data
  only; honest empty states; no dead controls | evidence: renders from the
  real fixture

INTEGRATION: the Lead builds the smallest src/basis_cycle.py after BASIS-READ
and FUTURES-SHORT land. Volume stays paper; UI stays read-only.

## EVENT CONTRACT v1 — frozen 2026-08-12; grows only on owner word; 1 page max
One JSON object per line, appended to events.jsonl on the execution host; the
UI reads this and nothing else. Fields: ts (UTC ISO), sha8, kind (run_start |
order_submitted | order_terminal | run_verdict | balance_snapshot | gap_read),
run_id, symbol, side?, client_id_last4?, order_id_last4?, executed?, quote?,
fees{asset:amt}?, balances{asset:free}?, residual?, verdict?, note?.
Factual states only — no derived opinions, no predictions. Optional fields per
kind; nothing else enters v1. Past one page it is a framework: cut it.
