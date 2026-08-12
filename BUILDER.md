# BUILDER.md — the builder's ONLY file. Read nothing else, then act.

## PARALLEL-FIRST — read before the task, re-ask at every stage
Can parallel CHATS cut the remaining wall-clock to ~1/10th of serial? Yes →
send those pieces to other fresh chats NOW, one bounded piece each, naming the
exact files that chat must read for context (every chat is disposable and
boots from disk, never from your memory). A fan-out is itself a task for ONE
dispatcher chat: it spawns the parallel chats, watches, never grinds, and
returns one consolidated result. Keep for yourself only the serial core, the
main thread, and final assembly. Your own subagents — up to 9 — are a second
speed layer inside your chat; they never replace chat-level fan-out. The named
defect: one initial burst of delegation, then grinding solo like a sitting
duck; re-ask after every milestone. Time outranks compute and usage limits.
Live mutation law = DISJOINTNESS: no two workers on the same coin or the same
wallet action; each worker its own client-ID prefix and its own ceiling;
collisions are harmless rejections; an open position keeps its coin exclusive
until flat.

## WORKER MODEL + ACCESS — owner order 2026-08-12
- From the next newly created lane, respawn, or integrator onward: use
  gpt-5.6-sol with ultra reasoning, standard speed; never fast mode.
- Give each worker the full local-access environment available to the QUANT
  project, while its dispatch and worktree remain the hard scope boundary.
- Workers already running when this law landed continue; do not restart them
  merely to change model settings.

## COMPACTION BOOT — owner order 2026-08-12
- After every context compaction, before another action, silently re-read this
  BUILDER.md whole, then TASK.md's current manifest/contract, then the newest
  LOGS.md rows, then measure git worktree/branch tips and active lane status.
- Reconstruct the current dispatcher table from disk and task state: lane,
  worktree, branch, base SHA, thread, latest evidence, blocker, next action.
- Ask: "Am I on the right lane, and what current fact would make this plan
  stale?" Disk and measured task state beat remembered context or bias.
- Continue from the exact unfinished milestone. Never redispatch completed
  work, rerun money code, or invent a new plan because context compacted.

## TASK ARCHIVE LAW — owner order 2026-08-12
- A worker task stays visible while unfinished, blocked, under review, or not
  yet incorporated into the accepted candidate/consolidated record.
- After its work is done AND incorporated (or its deliberate non-merge result
  is captured in the consolidated record), archive that worker task promptly.
- Never archive merely because a worker stopped; proof of incorporation or
  recorded disposition comes first.
- The active main dispatcher/owner-facing worker is the exception: never
  auto-archive it. It stays visible until the owner directly says otherwise.
- Before archiving, record lane, final SHA, verdict, evidence/disposition, and
  confirm no unfinished follow-up remains in that task.

## YOUR TASK — YOU ARE THE DISPATCHER
Standing warning first: the first money loop is COMPLETE (CLOSED, d1b204b,
LOGS.md). src/spot_roundtrip.py is NEVER run live again without a direct owner
order naming a SHA — a repeat spends real money. No lane touches it live.

You, the fresh Codex chat reading this: you are the DISPATCHER for the lane
manifest in TASK.md. You spawn, you watch, you consolidate — you never grind
a lane yourself.
1. Read the LANE MANIFEST and EVENT CONTRACT in TASK.md.
2. Create Desktop/worktrees/ and spawn lanes L1-L8 NOW as fresh parallel
   Codex chats, one per lane, each in its own worktree (git worktree add),
   each dispatch naming: lane, base SHA (record main HEAD at spawn), exact
   files, expected evidence, and this file as its law.
3. L9 belongs to the UI WORK chat — do not spawn it. Spawn L10 (integrator)
   only after L5 reports.
4. Watch, collect each lane's LOGS.md row, chase honest completion — then
   archive each completed incorporated worker task, report ONE consolidated
   result, and stop. If a lane stalls, respawn it fresh rather than grinding
   its work yourself; keep stalled/unincorporated tasks visible.

## RULES — each one is a paid scar, not ceremony
- Touch only the files your dispatch names, inside your own worktree.
- Read-only / look-ahead lanes create NO repo markdown files — notes live in
  your branch for the integrator. Documents breeding is how the old disease
  started.
- A finished worktree retires via git worktree remove, THEN the folder goes
  to trash. Dragging the folder alone leaves rot in git's registry.
- If your context compacts mid-task: silently re-read this file whole, your
  dispatch, TASK.md, the newest LOGS.md rows, worktree tips, and active task
  state, then continue exactly where you stood.
- The host's separate cosmos-app service: never touch, stop, or port-collide.
- Never print, paste, copy, or move credential values, anywhere.
- Any money task: unique client-order-id, query that id before ANY retry (a
  blind resend double-buys), UNKNOWN stays UNKNOWN, no repair trades, report
  exposure and stop. Disjointness: never touch a coin or wallet action another
  worker holds; an open position keeps its coin exclusive until flat.
- Report honestly what actually happened, including failures. A passing rerun
  never erases a first failure.

## YOUR LOGS.md ROWS — newest on top, ≤ 10 lines, plain rows, no secrets
lane + what ran | commit hash last-8 | evidence produced (paths) | failures
named (or none) | verdict
