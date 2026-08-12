# STATE.md — current truth, one page, overwritten in place

PROJECT: quant — the owner's money system. Strategies: basis first, volume second.
BORN: 2026-08-10. Folder: Desktop/quant. Git: LIVE since 2026-08-12 — first commit
5fa5fea "Initial quant project" on main, pushed to github.com/cosmosgeek0/quant,
working tree clean, .gitignore excludes .env from birth (verified: .env ignored
before any key exists; no secret has ever entered a commit or the remote). The
one-device open risk is CLOSED — unique work now has history and a remote.

WHAT EXISTS: the five governance files (LEAD, BUILDER, TASK, LOGS, STATE).
Also on disk: aws-connect/ — owner-placed WORKERS' tools (ssmrun.sh, xfer.sh:
run scripts / transfer files on the EC2 execution host over SSM). Rail verified
2026-08-12 from this Mac (account …7436, instance …63a9, ap-south-1; remote
user ssm-user). xfer.sh's dead helper path fixed 2026-08-12 — now self-locating.
No product code yet. Nothing has traded in this program. Exchange credentials:
workers' domain on the execution host — their state there unverified by seats.

CURRENT WORK: the smallest configurable Spot round-trip program — spec lives in
BUILDER.md, the worker's ONLY file. Execution runs on the existing EC2
connection (quant/aws-connect); credentials are the workers' domain on that
host. Commit+push before the live run, run once, real exchange result into
LOGS.md. No gates.

WHAT IS NEXT AFTER THE HAND: basis read (the quant computes one live
spot↔USD-M gap itself); then one full basis cycle at minimum size, including
partial-fill and one-leg cancel/close handling and a reduce-only futures close.
Volume stays paper until basis proves the hand; go-live or scale also needs an
untouched, parameter-frozen holdout with multiplicity accounted for.

THE OLD PROJECT: preserved at github.com/cosmosgeek0/arbitrage-cosmos (owner
word 2026-08-12) — ~a million lines; most of it does not work right; its tests,
proofs, and receipts are inside. That repo is the donor shelf: WORKERS pull
single donor pieces from it when their task needs one (donor law, LEAD.md wall
17). The Lead does not touch the old repo for now. Locally it is gone — off the
Desktop entirely, old site down, ports quiet (measured 2026-08-12). Standing
owner prohibition survives in every location: _TRASH and the two forbidden
files (FINAL_M0_MEGA_BURST_CONTROL_ORDER.md, M0_PRODUCT_LOCK_MAP.md) are never
listed, opened, or quoted by any seat.

OWNER'S STANDING WORDS: money first; owner approves funding, cap raises, and new
powers; owner gets plain-word updates at every step; no ceremony anywhere. Most
work runs through Codex builders on proper small tasks; the operational Lead
(replaced constantly) keeps that stream moving and translates; a standing
milestone lead moves bigger milestones; the Lead seat stays grounded — self-check
before every dispatch, write, and DONE (LEAD.md wall 19).
