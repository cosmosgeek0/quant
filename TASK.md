# TASK.md — the Lead writes cards here; builders take them

Contract: newest card on top. A builder takes the top OPEN card, marks it TAKEN
with its chat name, and follows BUILDER.md. Only the Lead writes cards; only the
taking builder edits its card's status line. Card states: OPEN → TAKEN → DONE or
BLOCKED. Status lines and BLOCKED rows are written LEAD-CLEAN (defined in
LEAD.md): plain rows, no secrets, no pasted logs.

---

CARD-001 | OPEN | Lane A — the Hand: one real round trip
START WHEN REPO EXISTS, RUN WHEN READY: the repo prerequisite gates the builder's
worktree and first commit; the Binance prerequisite gates only the live run.
The owner's key lands after finished committed code and .env exclusion, not before.
REPO PREREQ (owner, once): create the quant Git repo and give the word to initialize it.
RUN PREREQ (owner, once, ~3 minutes after committed code is ready): create a
Binance sub-account; create an API key scoped to exact Spot + USD-M trade and
account/order read — withdrawals and external transfers OFF, no master access;
pin futures leverage 1x; fund the sub-account with a small amount the owner
accepts losing; put the key in quant/.env (never in any chat, log, or tracked file).
OUTCOME: one small program in quant/src/ that, run once: reads .env, buys the
smallest exchange-valid amount of one liquid Spot symbol, confirms the fill by
querying its own client-order-id, sells back to flat, verifies flat via balances,
and writes one receipt file to quant/receipts/ containing: intent, order ids
last-4, fills, fees, balances before/after, code hash last-8, and the four-way
match verdict. Before .env is read, quant/.gitignore excludes it and Git confirms
the key file is ignored and untracked, outside repository and remote custody.
NAMED FILES: quant/.gitignore, quant/src/**, quant/receipts/** only.
PROOF: the receipt file plus the LOGS.md entry.
STOP: after one complete cycle. No loops, no repeats, no extra features, no UI.
