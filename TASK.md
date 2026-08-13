# TASK.md — current work orders

SINGLE TASK (owner GO 2026-08-13, Fable-direct, no workers or subagents):
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
