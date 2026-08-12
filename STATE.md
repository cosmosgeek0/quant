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
HOST (measured by the AWS DEPLOY seat 2026-08-12, owner-relayed): t3a.large
…63a9 running, health OK; restricted IAM operator (StartSession only, cannot
see wider account); zero inbound rules, Cloudflare Tunnel active; CloudWatch +
SSM agents on; 200-GiB encrypted volume ~13 GiB used. A separate cosmos-app
Node service (the old project) runs on 127.0.0.1:8080 there — never confused
with quant, untouched until the owner directly orders. Quant itself: NOT
deployed. AWS DEPLOY (persistent Codex task) owns all AWS/host execution and
deploys only an owner-named committed SHA, keeping rollback.
Product code: src/spot_roundtrip.py (baseline, never run live). Nothing has
traded in this program. Exchange credentials: workers' domain on the execution
host — their state there unverified by seats.

CURRENT WORK (owner instruction FABLE_FIRST_MONEY_LOOP, consumed 2026-08-12):
src/spot_roundtrip.py — the complete one-file first money loop, written by the
Lead as a baseline and adversarially verified. BUILDER.md is now the bounded
review-and-run task for ONE fresh Codex builder: review the baseline against
the Binance docs, fix only loop-blocking defects, commit+push, then stop and
ask the owner for SYMBOL and MAX_QUOTE plus one execute-question naming the
exact SHA. That builder is the SOLE AWS/Binance mutator for this milestone;
AWS DEPLOY stays idle. Receipt goes outside git. Nothing has run live.

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
Desktop, Mac ports quiet (measured 2026-08-12) — but its cosmos-app service
still RUNS on the EC2 host (127.0.0.1:8080, Cloudflare Tunnel): AWS DEPLOY's
domain, untouched. Standing
owner prohibition survives in every location: _TRASH and the two forbidden
files (FINAL_M0_MEGA_BURST_CONTROL_ORDER.md, M0_PRODUCT_LOCK_MAP.md) are never
listed, opened, or quoted by any seat.

OWNER'S STANDING WORDS: money first; owner approves funding, cap raises, and new
powers; owner gets plain-word updates at every step; no ceremony anywhere. Most
work runs through Codex builders on proper small tasks; the operational Lead
(replaced constantly) keeps that stream moving and translates; a standing
milestone lead moves bigger milestones; the Lead seat stays grounded — self-check
before every dispatch, write, and DONE (LEAD.md wall 19).
