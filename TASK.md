# TASK.md — current work pointer

DONE 2026-08-12: the first money loop — CLOSED round trip on BTCUSDT at commit
d1b204b, verdict and numbers in LOGS.md. DO NOT REPEAT IT: no worker runs
src/spot_roundtrip.py against the live exchange without a direct owner order
naming a SHA. A repeat spends real money.

NEXT PHASE — the commander's parallel plan, adopted 2026-08-12 (Old Lead
verdict, owner word): ~12 disjoint lanes as parallel Codex chats from the
Desktop worktrees folder; ONE dispatcher chat spawns and watches all lanes and
returns one consolidated result (it never grinds); ONE integrator merges to
one candidate SHA; ONE single live money-toucher, owner-ordered only. Live
proof order: minimum futures open/close, then one complete minimum-size basis
cycle. Look-ahead lanes (reverse-basis spec, volume map, old-UI extraction)
are read-only and create NO repo markdown — notes live in their branches for
the integrator. A finished worktree retires via git worktree remove, then the
folder goes to trash. UI WORK: gate opens on the owner's word; builds from
the real receipt as fixture, factual states only.

PENDING BEFORE DISPATCH: freeze the tiny event contract (one page max) and
the lane manifest — every lane gets a base SHA, exact files, and expected
evidence — from the commander's plan text; then the dispatcher goes out.

OWNER ACT, LATER (the Old Lead will call the timing): add futures permission
to the key before the futures leg runs live (~2 minutes; deliberately
spot-only today).
