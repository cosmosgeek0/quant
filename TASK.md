# TASK.md — current work orders; no cards, no ceremony (owner word 2026-08-12)

Contract: newest order on top. A builder takes the top order, does exactly it,
writes its LOGS.md entry (shape in BUILDER.md, LEAD-CLEAN), stops. Only the Lead
writes orders.

---

NOW — THE HAND, the first money path:
One small program in quant/src/, run once end to end:
reads .env → buys the smallest exchange-valid amount of BTCUSDT (symbol pinned,
ruling 2026-08-12 — no drift room) →
confirms the fill by querying its own client-order-id → sells back to flat →
verifies flat via balances → writes ONE receipt to quant/receipts/ containing:
intent, order ids last-4, fills, fees, balances before/after, code hash last-8,
four-way match verdict.
All orders leave through ONE executor module; strategy code never calls the
exchange. Unknown order result → query the client-order-id before any retry.
Repo is live (github.com/cosmosgeek0/quant, main) — live runs happen only on a
committed tree; the receipt's hash must point at the code that traded.
RUN NEEDS (owner, ~3 minutes, only after committed code is ready): Binance
sub-account; API key scoped exact Spot trade + account/order read ONLY — USD-M
trade is NOT granted yet, it is added only when basis work begins (wall 9);
withdrawals/external transfers OFF, no master access; leverage pinned 1x; small
funding the owner accepts losing; key into quant/.env — never in any chat, log,
or tracked file.
Codex builds it. Worker rail if needed: quant/aws-connect. Stop after one clean
cycle — no loops, no extra features, no UI.

THEN: basis read — the quant computes one live spot↔USD-M gap itself.
THEN: one full basis cycle at minimum size — partial fills, one-leg cancel +
ordered close, every futures close reduce-only.
