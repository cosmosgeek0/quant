# TASK.md — current work orders

ARMED, OWNER'S TRIGGER (2026-08-13): the one capped BNBUSDT plumbing
sequence sits deployed and verified on the host (leverage 1x one-shot ->
receipted 9 USDT transfer -> full signed validate -> ONE capped cycle;
stops honestly at any step). No seat presses it; the owner runs
`bash aws-connect/ssmrun.sh scripts/run_plumbing_on_host.sh 240` from
Desktop/quant. Code 4971faad; sequence commit fca93576.

COMPLETED same day (owner GO, Fable-direct):
- basis_executor.py signed-reads the plan symbol's ACTUAL leverage
  (/fapi/v1/symbolConfig) and refuses before every order unless it is
  exactly 1x. The module never changes leverage.
- --validate-only runs the COMPLETE signed preflight, including actual
  symbol leverage, with zero POSTs and zero account changes.
- --plan-only stays unsigned/public.
- Focused offline tests; commit, push; run plan-only for BTCUSDT, ETHUSDT,
  SOLUSDT and validate-only for any symbol whose plan passes; report the SHA
  and measured results; then STOP.
- No orders, no leverage changes, no UI, Volume, panel, or lane work.

(Event contract v1 unchanged; its frozen text is preserved in TASK.md at
commit 5a53ddbb and stays binding.)
