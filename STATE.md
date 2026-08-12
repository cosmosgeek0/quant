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

QUARANTINE: the Task-B draft — src/host_once.py, deploy/ unit templates,
tests/, edits to wallet.py, futures_hand.py, events.py, plus 10 LOGS rows —
sits uncommitted in Desktop/quant, held unchanged: no merge, delete, deploy,
run, or repair. All wave work runs in clean worktrees from the reset SHA. One
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

FUTURES: capability last measured 2026-08-12 canTrade=false; the owner has
since reported enabling it — UNVERIFIED, no fresh preflight run. No futures
order, live or test, has ever been sent. USD-M wallet held 1.00000000 USDT at
the last snapshot; its origin was a 2026-07-20 internal transfer (LOGS).

NOW RUNNING — PRODUCT WAVE (orders in TASK.md): four lanes in clean worktrees
from the reset SHA — BASIS-READ, FUTURES-SHORT (offline), VOLUME (paper), UI
(read-only). No host runner, no live action this wave. The Lead integrates
the smallest src/basis_cycle.py after BASIS-READ and FUTURES-SHORT land.

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
