# TASK.md — current work orders

WAVE 3R — RESTART-SAFE RUNTIME VERTICAL (commander order 2026-08-13, in
force; wave-3 completion verdict REJECTED; no arming of scripts/basis_auto.sh,
no live mutation anywhere):
1. Persist an IMMUTABLE proposal: id, TTL, config/SHA, executable entry and
   intended-exit economics, four measured commissions, entry+exit slippage,
   funding, safety buffer, final net edge, quantity.
2. basis_position --open accepts ONLY a valid proposal id; raw-gap entry
   removed/refused.
3. STOP is a GLOBAL entry interlock; every read-only position/order/balance/
   margin check completes BEFORE any leverage change.
4. ONE supervisor: claim proposal -> open -> continuous reconcile/check ->
   automatic close on exit or emergency -> safe crash-resume. UNKNOWN and
   ONE_LEG freeze new entry and reconcile.
5. The full supervision ladder wired into the real runtime; executable exit
   prices plus current funding drive exits.
6. End-to-end injected tests: STOP, negative-net entry refusal, proposal
   expiry, exit routing, emergency close, every crash boundary.
7. Deploy paused/GET-only only; commit sanitized raw evidence of deployed
   SHA, STOP, process/cron state, balances, positions, open orders.
Recorded state until this passes: WAVE3_COMPONENTS_BUILT__RUNTIME_NOT_READY.
