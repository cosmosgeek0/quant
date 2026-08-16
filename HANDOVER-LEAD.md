# HANDOVER-LEAD.md — seat transfer, 2026-08-16 ~07:50Z (outgoing operational Lead → worker 4.0)

You are the operational Lead now. Boot: CLAUDE.md (auto-loads) → NOW.md → newest LOGS.md rows → this file → lane-logs/cards/. Disk beats anything a chat remembers.

## 1. The one method that worked all day — grind exactly like this
- YOU NEVER GRIND. Codex grinds. `codex exec --skip-git-repo-check -s workspace-write -C <worktree> -c sandbox_workspace_write.network_access=false < lane-logs/cards/<ID>-task.txt > lane-logs/<ID>.log 2>&1` in the background. Drop the network flag ONLY for cards that name public data.binance.vision GETs. Every card starts with `/fast`.
- CARD SHAPE (copy any file in lane-logs/cards/): one worker, one narrow ~15-20 min scope, EXACT files it may modify, the defect or question in one sentence with the measured fact that motivates it, hard boundaries, "run yourself" test line, "REPORT lane-logs/<ID>-report.txt max N rows". Language section of AGENTS.md binds every worker (clean, soft — the owner's hard law).
- FLEET LAW: the PreToolUse hook (~/.claude/hooks/fleet-reminder.sh) counts live codex before every Bash/Write/Edit and tells you when it is lean (<4). When it says lean, DISPATCH before doing anything else. The owner has said this eight times; the hook now says it for him. Aim for 6-9 live; the day peaked at 9.
- VERIFY WITH YOUR OWN HANDS: a worker report is a hypothesis. Before ANY commit: run the named tests yourself (`python3 -m unittest <modules>` — the exit-checked gate pattern: capture the summary line, `grep -q "^OK"` or refuse to commit), read the diff for money files, then commit with a message that states what was measured. Push lane branch; push to main only what has passed review.
- MONEY FILES (src/job_runner.py, src/basis_position.py, src/margin_ops.py, src/basis_*.py) NEVER reach main on one pair of eyes: worker builds → you verify → fresh-eyes codex reviewer (card pattern: MRV*/BR* — attack list, CONFIRMED/SUSPECTED/PASS with file:line + trigger, MERGE-SAFE or FINDINGS-BLOCK) → repair card from the findings → re-review. Convergence signal: findings shrink round over round (6→3→1→0). Divergence signal (6→6→6): the SHAPE is wrong — stop patching, redesign (the additive position schema was shelved this way; the ledger rebuild converged in three rounds).
- DEPLOY (host = EC2 via aws-connect/ssmrun.sh + xfer.sh; PTY drops heredocs — always xfer a script then run it): stage files under scratchpad, manifest rows `sha256 relpath [mode]` (ONE space, D2 tool), xfer to /home/ssm-user/deploy-stageN/, run /home/ssm-user/host_deploy_atomic.sh <staging> /home/ssm-user/quant-run/wave2-validate-h2 <manifest> <sha> — it gates the tick, renames atomically, verifies hashes AND modes, writes RELEASE last. Then verify RELEASE-NOW and hashes in the transcript. Never deploy multi-file without the tool (the tick raced a bare transfer once; a bare rename dropped an executable bit once — both scars are in evidence/).
- WAITING LAW: never block a turn on a wait; background it and let the notification wake you. Foreground SSM calls with sleeps time out at 2 min — background them.
- REPORT TO OWNER: plain words, numbers, MEASURED/INFERRED labels, and a progress bar block at every stop (format below). No victory tone. State limits out loud.

## 2. Live state at handover (MEASURED)
- MACHINE: SEALED (STOP-JOBS present), FLAT. Host RELEASE = fe4445b (MEASURED at 07:48Z: HOST-DEPLOY OK, runner sha 36403137…, mode 644, job_tick.sh -rwx------, D2 tool live). No governor lock stands.
- OPEN RECEIPT LOOP (the ONE residual of the margin act): repay job mproofr-2c2a3027d877 posted (txId 401382497303) and the account is flat, but its receipt stands UNKNOWN; reconcile-margin returned RECONCILE_UNCONFIRMED MARGIN_CONFIRMATION_MISMATCH at 07:48Z even on release fe4445b. Root cause NOT yet diagnosed — first probe for you: run a signed GET-only history read filtered type=REPAY (script /home/ssm-user/margin-truth.py — add a REPAY-filtered call) and compare the row's fields to the receipt's expected_ledger_confirmation; the likely culprits, in order: (a) the repay history row's `principal` field is the amount incl. interest (2.5000387) vs the runner's expected principal-only form, (b) the row's origin/type field, (c) the reconcile evaluator's REPAY branch requiring a pre-snapshot delta the receipt cannot satisfy against a now-flat account. Whichever it is: repair card MJ17 (src/job_runner.py + tests, pattern of MJ9-MJ16), fresh-eyes verify, one-file deploy via the atomic tool, reconcile-margin again. This is receipt hygiene, NOT money risk — the exchange shows liability 0.
- FIRST MARGIN ACT: CLOSED. Exchange truth: borrowed 0, interest 0, liability 0. Evidence: evidence/margin-proof-attempt1-2026-08-16.txt (b51224f). Cost 0.0000387 USDT. Margin key proven both directions.
- MAIN = origin/main at ffad36e+ (records) with d79ecb3 (AB28) on top. Worktree lead2-v2fix carries lane commits; branch lane/lead2-v2fix. Canonical Desktop/quant is DIVERGED (UI seat's local commits) — never run pipelines from canonical; use the worktree.
- CODEX LIVE at handover: B11 (BUILD-04 the ADD transition, src/basis_position.py — money-adjacent, needs BR-review before merge), BF2 (basis lock-in step 1: src/executable_gap.py, new file), plus the V2 volume chain (policy_sim → validate; output study/out/v2/v2_result.json when done — commit it + evidence + LOGS row; read it as a blind-spot map per the engine-completeness law). Their reports land in lane-logs/<ID>-report.txt.
- HOOK: ~/.claude/hooks/fleet-reminder.sh wired in ~/.claude/settings.json PreToolUse Bash|Write|Edit. Leave it.

## 3. The design words (owner's to say — you carry the numbers)
- BASIS: BUILD, REVERSE-FIRST. Measured: 524 executable lock-ins net median +0.51% after fees+slippage, 94% positive, indifferent to held/collapsed; 522/546 were REVERSE (spot above perp during pumps → short spot borrowed + long perp). Spec: study/design/BASIS-LOCKIN-SPEC.txt (123 rows). Assets: src/reverse_basis_lifecycle.py (14 states, reviewed), src/margin_ops.py + runner margin ops (live-proven), src/basis_ladder.py + basis_events.py (landed), BF2's executable_gap.py (in flight). Build order after BF2: executable-gap live reader wired to the stream → reverse rung intent → margin borrow (runner op, proven) → both legs through the canonical entry chain → LIFO unwind on gap close → repay. Each step tiny-live at ≤8 USDT through the job runner IN THE SAME CARD (money-first law). The reverse gate in basis_ladder opens ONLY on the owner's explicit word.
- VOLUME: PARK vs ONE PROBE — the owner decides from study/design/VOLUME-VERDICT.txt (76 rows). Every entry surface loses at 1m-candle exits (OF3: 0/9 cells positive; best p99 R3 = -0.0108 pp/symbol-day). The one untested door: seconds-resolution tape exits on the OF2 p99 fire set. Do NOT run more volume studies without the owner's word — the record explicitly says so.

## 4. Open work, in priority order (cards exist or are one line from existing)
1. Finish the M2 receipt loop: confirm swap6 landed, repay receipt CLOSED, lock absent, evidence updated (add a CLOSE-2 line), NOW.md limitation row removed.
2. B11 → verify → BR8 review card (copy BR6/BR7 shape: attack the ADD — double-execution on crash, STOP refusal, dust/stale-reconcile refusals, cap from schedule) → repair loop → merge BUILD-04.
3. BF2 → verify → land; then BF3 (wire executable_gap to the live stream, read-only) → BF4 (reverse rung intent, offline) → BF5 (first tiny live reverse lock-in through the runner — needs the owner's reverse word AND a dislocation event; the machine hunts sealed until then).
4. V2 chain result → commit + evidence + LOGS.
5. Records: LOGS row per landing; NOW.md rewrite at each state change (N1 card shape).
6. Owner words still open: permit numbers vs study/donor/PORT-PLANS.txt (2 ready, 9 heavy, 1 redesign), BNB fee float, re-arm criterion, both design words.

## 5. Scars to never repeat (all recorded in evidence/ or LOGS)
tick raced a multi-file bare transfer (deploy race); atomic rename dropped executable bit (mode regression); receipts from older releases lack newer fields (legacy shape → frozen field set + stricter substitute proof, never loosened checks); Binance history row "type" = origin class MANUAL/AUTO, direction is the query param; history rows lag the account by seconds (retry envelope 1/2/4s); relative-path launches from the Desktop cwd (always absolute paths in background commands); a study whose result looks too good (99% win) is conditional on something — find the survivorship before banking it.

## 6. Progress bar format (print at every stop)
```
MARGIN PROOF   ████████████████████ 100%
POSITION RAIL  ██████████████████░░  88%  BUILD-04 building
BASIS BUILD    ██░░░░░░░░░░░░░░░░░░  10%  step 1 (BF2) building — reverse-first
VOLUME         ████████████████████ 100%  answered; owner's park/probe word open
DESIGN WORDS   ███████████████████░  95%
──────────────────────────────────────────
OVERALL        ███████████████████░  ~95%   fleet: N codex live
```

Outgoing Lead: frozen after this push. History questions welcome via the record.
