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
NEVER parallel: any live exchange mutation — one mutator, one hand, always.

## STATUS — NO OPEN TASK IN THIS FILE
The first money loop is COMPLETE: verdict CLOSED, 2026-08-12, commit d1b204b,
top entry in LOGS.md. src/spot_roundtrip.py is NEVER run against the live
exchange again without a direct owner order naming a SHA — a repeat spends
real money. If you are a fresh chat reading this without a dispatch message
naming your lane: STOP. Do nothing. Reply that no task is open here.

Next-phase lane tasks arrive as DISPATCH MESSAGES naming: your lane, your base
SHA, your worktree, your exact files, and the evidence you must return. Your
dispatch is your task; this file is your law.

## RULES — each one is a paid scar, not ceremony
- Touch only the files your dispatch names, inside your own worktree.
- Read-only / look-ahead lanes create NO repo markdown files — notes live in
  your branch for the integrator. Documents breeding is how the old disease
  started.
- A finished worktree retires via git worktree remove, THEN the folder goes
  to trash. Dragging the folder alone leaves rot in git's registry.
- If your context compacts mid-task: silently re-read this file whole, your
  dispatch, and your last LOGS.md row, then continue exactly where you stood.
- The host's separate cosmos-app service: never touch, stop, or port-collide.
- Never print, paste, copy, or move credential values, anywhere.
- Any future money task: unique client-order-id, query that id before ANY
  retry (a blind resend double-buys), UNKNOWN stays UNKNOWN, no repair trades,
  report exposure and stop. One mutator, one hand, single-runner lock.
- Report honestly what actually happened, including failures. A passing rerun
  never erases a first failure.

## YOUR LOGS.md ROWS — newest on top, ≤ 10 lines, plain rows, no secrets
lane + what ran | commit hash last-8 | evidence produced (paths) | failures
named (or none) | verdict
