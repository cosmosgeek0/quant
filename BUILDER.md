# BUILDER.md — the builder's ONLY file. Read nothing else, then act.

## PARALLEL-FIRST — read before the task, re-ask at every stage
Can parallel CHATS cut the remaining wall-clock to ~1/10th of serial? Yes →
one owner work command gets ONE worker chat and ONE worktree. That worker uses
up to 9 subagents inside its own chat for every independent piece that can cut
wall-clock: code reads, AWS reads, checks, tests, evidence, and verification.
Do not split one task into peer worker chats. Separate worker chats exist only
for separate owner-named tasks or manifest lanes, each with its own worktree.
The worker does not fan out once and then grind a long serial tail: before the
first action and AGAIN after every milestone, it asks what remaining work is
independent and sends all of it to available subagents immediately. Keep only
true dependencies and one live position/action lifecycle serial. Time outranks
compute and usage limits. TASK.md's named blockers are the gates; never invent
extra gates or substitute readiness ceremony for the ordered work.

Every worker dispatch/work command MUST repeat this operating instruction:
"One task, one worker chat, one worktree. Use up to 9 subagents throughout;
fan out every independent piece now and re-fan-out after every milestone. Keep
only true dependencies/live lifecycle serial. Do not invent gates."
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
- Retirement order is mandatory for every app-managed worker: while the task
  is still unarchived, verify and record the clean worktree, move its exact
  folder to /Users/cosmosgeek/.Trash/, prune only its stale Git registration,
  confirm branch/commits remain, and ONLY THEN archive the task. Never archive
  first: the app may remove its temporary worktree before it reaches Trash.

## WORKER CONTACT LAW — owner order 2026-08-12
- Before sending any mid-task message to a worker, read or wait for its newest
  state and check the last message already sent to it.
- Do not interrupt a running worker with repeated instructions, status nudges,
  encouragement, or facts it already has. Let it reach its next milestone.
- If a genuinely new correction, safety boundary, or blocker cannot wait, send
  one concise consolidated message containing all of it; never drip-feed or
  "slam-dunk" spam a worker with multiple follow-ups.
- Prefer one dispatch, quiet observation, and one requested correction. A
  worker asking for owner input may be surfaced, but it must not be answered by
  another worker pretending to be the owner.

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
- After its task is done and incorporated/recorded, verify the worktree is
  clean and its branch, final SHA, and disposition are recorded. Move that
  exact worktree folder to a unique path inside /Users/cosmosgeek/.Trash/;
  never use rm and never permanently delete the folder.
- Only after the folder is safely present in Trash, clean its stale Git
  worktree registration with the standard Git worktree-prune flow and confirm
  the branch and commits remain. Never prune a live or unfinished worktree.
- Archive the worker task only after that Trash-and-prune proof succeeds.
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
