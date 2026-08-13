# TASK.md — current work orders

WAVE 3 — FULL NORMAL-BASIS STRATEGY (owner GO 2026-08-13, adopting the
commander's FABLE_FULL_BASIS_ORDER with Lead corrections: base = current
main, old-engine files digested by workers as behavioral specs per donor
law, Task-B references moot — already Trash-disposed):
- Build the complete vertical: continuous universe observer -> economically
  valid candidate (entry - exit + expected funding - four MEASURED
  commissions - slippage - buffer, all in bps) -> Spot long + USD-M short ->
  persistent restart-safe HOLD -> measured exit (convergence, max-hold,
  edge-inversion, emergency) -> reconciliation and P&L.
- Preserve the proven execution mechanics (basis_executor discipline,
  basis_scan first pass, wallet/futures/spot hands). No wholesale old-code
  copy; behaviors ported one at a time with reasons; differential fixtures
  prove extraction.
- Commission truth from signed GET /api/v3/account/commission and
  /fapi/v1/commissionRate for finalists only. Leverage: set-and-verify 1x on
  the selected symbol only.
- End states: CLOSED (genuine qualifying cycle, owner-activated) |
  READY_WAITING_FOR_NET_EDGE | EXECUTION_MECHANICS_ONLY (only if the owner
  separately orders a paid mechanics proof) | UNKNOWN.
- The cron loop's TRADING is PAUSED (STOP file) until the new gate replaces
  its crude bar; live entry only on owner activation. Checkpoint commits
  throughout; no UI, no Volume, no new laws.

(Event contract v1 unchanged; its frozen text is preserved in TASK.md at
commit 5a53ddbb and stays binding.)
