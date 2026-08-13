# STATE.md — current truth, one page, overwritten in place

PROJECT: quant — the owner's money system. Strategies: basis first, volume
second. BORN: 2026-08-10. Folder: Desktop/quant. Git LIVE since 2026-08-12:
first commit 5fa5fea on main, remote github.com/cosmosgeek0/quant. .gitignore
has excluded .env from birth; no secret has ever entered a commit or the
remote; no credential exists on this Mac.

RESET 2026-08-13 (direct owner order, in-channel): TASK B cancelled;
OWNER_ORDER_PARALLEL_WAVE_2 cancelled. Owner-fixed facts: main =
72d88b565d67b038c9979facb6845d3e9e188c48 at reset; NO bot deployed; NO
Futures trade has ever completed. The old Task-A/Task-B worker is stopped and
was never the lead; a fresh operational Lead holds the seat. Process truth
(measured 2026-08-13): no Task-B process on the Mac; no quant unit, timer,
unit file, process, or cron entry on the host.

TASK-B DISPOSITION (owner word 2026-08-13): the cancelled draft was first
stashed as CANCELLED-TASK-B-DO-NOT-APPLY, then on the owner's removal order
preserved whole to ~/.Trash/task-b-cancelled-*/ (tracked patch + untracked
files) and dropped from the repo. It exists nowhere in the live tree, index,
or stash (verified). The old worker chat must never press Undo. One
un-retired worktree remains: ~/.codex/worktrees/2ae9/quant @ dd474b2 (blocked
futures task; its branch is pushed to origin); retirement awaits a later word.

WHAT EXISTS: five governance files; aws-connect/ SSM tools (rail verified
2026-08-12; account …7436, instance …63a9, ap-south-1). EC2 host healthy,
zero inbound, tunnel active; the old project's separate cosmos-app service
runs there untouched; quant itself NOT deployed. Committed product code:
src/spot_roundtrip.py (PROVEN LIVE), market_reads.py, wallet.py,
futures_hand.py, events.py; ui/fixtures/events.jsonl (contract v1).
Credentials live only on the host, read only by the running program.

FIRST MONEY MOVED — 2026-08-12T16:01Z, verdict CLOSED (LOGS.md; receipt on
the host). One real BTCUSDT round trip at d1b204b under a 10 USDT ceiling;
true round-trip friction ≈ 0.02 USDT plus ~0.62 retained dust value; an
earlier 5 USDT attempt was correctly REFUSED before the BUY. The spot hand is
PROVEN by the four-way match.

FUTURES, measured 2026-08-13 by signed GET-only host read: canTrade=TRUE via
/fapi/v1/accountConfig — the 2026-08-12 canTrade=false was a /fapi/v3/account
endpoint false-negative, now fixed in code. USD-M USDT 1.00000000
wallet/available; zero positions, zero open orders; position mode ONE_WAY;
exchange-side symbolConfig BTCUSDT 10x, ETH/SOL 5x CROSSED (the hand sets 1x
one-shot before any order). No futures order, live or test, has ever been
sent; trade capability stays unproven until a real order.

WAVE-3 LANDED (2026-08-13, measured): the full normal-basis strategy is
built, panel-hardened (12/12 findings repaired), extraction-proven against
the old engine (116/125 differential fixtures), and production-validated —
continuous live observer with restart recovery, measured commission truth
(0.30% round trip), complete signed preflight for finalists. Release
382b8b96204ab9e4f501bfb11cc8a5487bd1db03; modules sha-verified on the host.
STATE: WAVE3_COMPONENTS_BUILT__RUNTIME_NOT_READY (commander verdict): the
components are built and hardened, but the restart-safe runtime vertical —
immutable proposals, proposal-only entry, STOP interlock, one supervisor
with crash-resume, e2e crash tests, paused GET-only deploy evidence — is
NOT built. No live mutation; basis_auto stays unarmed.
Wave-1/2 history preserved in LOGS.

PARKED, awaiting owner words someday: L6 reverse-basis spec (needs borrow
power); L8's seven old-project import candidates (per-item permits); the
Codex worktree retirement above.

THE OLD PROJECT: preserved at github.com/cosmosgeek0/arbitrage-cosmos — the
donor shelf (LEAD.md wall 17); the Lead does not touch it. Its cosmos-app
service still runs on the EC2 host: AWS DEPLOY's domain, untouched. Standing
prohibition survives everywhere: _TRASH and the two forbidden files are never
listed, opened, or quoted by any seat.

OWNER'S STANDING WORDS: money first; owner approves funding, cap raises, and
new powers; plain-word updates at every step; no ceremony; workers build the
pieces while the Lead — this wave — is also the integrator; the Lead stays
grounded: self-check before every dispatch, write, and DONE (LEAD.md wall 19).
