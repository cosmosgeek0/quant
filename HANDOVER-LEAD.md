# HANDOVER-LEAD.md — seat transfer, 2026-08-15 ~11:20Z (outgoing operational Lead)

## SHAs / releases
- repo main == origin/main == 71ccd26 (this handover commit lands on top; verify with git log).
- Host RELEASE: 4ba03977a0cc71eb5fdac651fd91aae4f25b70f3 (recorder deploy, hash-gated x4,
  transcript evidence/…; funnel release 18b193f preceded it). accept_one.py FROZEN pinned to 4ba0397.
- Suite at handover: 1032/1032 (Lead-run, this hour). Worktree clean at 71ccd26.

## Machine / seals / money
- SEALED + FLAT since the OPENUSDT receipt (evidence/first-autonomous-trade-2026-08-15.txt).
  Both interlocks present; ACCEPT-ONE consumed/expired — none standing; first_autonomous_close_passed
  EXISTS → 32 cap unlocked per physics. Governor knobs live: 10/day, 2.00 USDT, ignition 3 (env-clamped).
- Tuner advisory-only (marker TUNER_ADVISORY on host + code honors it). Aliveness watchdog cron */2 live.
- OWNER WORD today: reverse-basis borrow AUTHORIZED; capability MEASURED (borrowEnabled true, margin
  account accessible; key margin-TRADE permission UNVERIFIED until first real margin action).
  Never-list narrowed for reverse basis only. LOGS row 727b279.
- Re-arm criterion standing: a config must clear fees+slippage on replay with margin → smallest cap same hour.

## V2 pipeline (commander milestone) — exact stage
- Tape COMPLETE: full-universe cohort 659 (358 both / 169 futures-only / 132 spot-only), 30d klines,
  gaps=459 honest (study/out/v2/cohort_manifest.json;  21,052 pulled + 8,999 reused this round).
- Stage A DONE 11:16:23Z on the FULL tape: 659 symbols, 27,781,995 minutes, 199,095 labelled
  events -> study/out/v2/events.jsonl (935 MB, on the Mac, gitignored).
- WINDOWS stage CRASHED at start: "events.jsonl line 197191 symbol is invalid" — one Stage-A
  row carries a symbol the windower's validator rejects (almost certainly the non-ASCII listing
  in the cohort; the tape pull logged it as a futures venue symbol). Precise, small: either
  widen the windower's symbol charset or make Stage A emit an exchange-safe alias. NOT fixed —
  frozen per the handover order. Pipeline script: /tmp/v2-pipeline.sh (stages gated, restart
  from WINDOWS; Stage A output is good and need not rerun).
- Remaining stages after that fix: windows -> selective aggTrades pull (tape_pull
  --windows-file) -> stage_b -> policy_sim -> validate -> v2_result.json + evidence report.
  The commander's deliverable rules are in study/ORDER-VOLUME-V2.txt; interface in
  study/INTERFACE-V2.txt.

## Basis engine state
- src/basis_ladder.py + src/basis_events.py landed and green (laddered rungs, LIFO unwind,
  reverse gated on borrow_power; violent-move dislocation events; intents only, no execution).
  NOT integrated into the runtime; integration must honor the 16 DO-NOT-REBUILD gates
  (study/donor/MINE-LESSONS.txt), especially: held state advances only on reconciled fills;
  one execution path, never forked money truth; borrow capability is not reverse readiness.
- Donor digests committed: study/donor/MINE-{BASIS,VOLUME,LESSONS}.txt. 12-item pull-permit
  menu awaits the owner's per-item numbers (9 basis, 3 volume; candidates + costs inside).

## Live plumbing / rails / loops
- Host rails: aws-connect ssmrun sleep=10 committed (baac939). PTY drops bulk paste — files
  >32KB go as tgz bundles <32KB or 8KB chunks with host-side assembly + sha gates. sudoers
  use_pty drains buffered stdin after the first sudo — batch privileged steps into ONE
  root-side script, always xfer scripts then run, never inline heredocs.
- Mac loops ui_sync.sh (Desktop/quant ui/ -> host, live-edit zone) and ui_pull.sh (host ->
  ui/live, interval_ms declared) are DEAD as of handover (app restarts kill nohup children).
  Restart both from /Users/cosmosgeek/Desktop/quant:
    HOST_UI_ROOT=/home/ssm-user/quant-ui/ui nohup bash scripts/ui_sync.sh &
    nohup bash scripts/ui_pull.sh &
- Host services: quant-ui (8080, behind the owner's Cloudflare Access), quant-ui-mirror (5s),
  cloudflared, cron runtime_tick */3 + job_tick 1m + aliveness */2 + knob_tuner hourly
  (advisory). Old cosmos app in /opt/trash-cosmos-app-2026-08-14 (restore = move back+enable).

## Monitors / background (this session's, all end with the session)
- Pipeline monitor ended after reporting the WINDOWS crash. No live monitors carry over —
  the successor arms its own. The deep-tape and pipeline logs live in this worktree's
  lane-logs/ (gitignored): v2-tape.log, v2-pipeline.log, MINE-*.txt originals.

## Open owner words / queue
- Permit menu numbers (12 donor pulls). BNB fee-float purchase (live buy — owner act only).
  Re-arm word (bound by the standing criterion). Tuner stays advisory until changed.
- In another seat's flight per its own message: R1 margin/borrow reconciliation core.

## Worker-fleet operations note
- codex workers die at report time under account rotation but their disk work has been
  complete and green in every wave this session: verify from disk (compile + own test runs +
  full suite), then commit with "workers died pre-report, Lead-verified" honesty. Detect true
  completion by "tokens used" within the LAST 120 bytes of the lane log — prompt echo lies.

Outgoing Lead: frozen after this push. Machine sealed; no dispatches, writes, or messages
from the old seat. History questions welcome.
