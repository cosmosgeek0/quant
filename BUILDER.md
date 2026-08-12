# BUILDER.md — the worker's ONLY file. Read nothing else.

You are a fresh Codex worker. This file is your whole world: the task and the
rules are below; when your LOGS.md rows are written, you stop. Do not read other
chats, other markdown files, the old repo, or anything not named here.

## THE TASK
Repo: github.com/cosmosgeek0/quant (local checkout: Desktop/quant).
Write the smallest CONFIGURABLE Spot round-trip program in quant/src/:
- Config (env vars or one small config file in quant/src/): symbol and quote
  amount. Nothing hardcoded.
- Flow, run once end to end: buy the smallest exchange-valid amount of the
  configured symbol → confirm the fill by querying your own unique
  client-order-id → sell back to flat → verify flat via balances → report the
  real exchange result: orders, fills, fees, final balances.
- The execution connection already exists: quant/aws-connect/ssmrun.sh runs a
  script on the execution host; xfer.sh transfers files to it. Use it.
  Credentials are handled on the execution host side — if none work there,
  report exactly that as your result and stop; never ask for keys in chat.
- Commit and push your code on main BEFORE the live run. Run once. Write your
  LOGS.md rows. Stop.

## RULES — each one is a paid scar, not ceremony
- Every order carries a unique client-order-id. Unclear response → query that
  id before ANY retry. A blind resend double-buys.
- Never print, paste, copy, or move credential values, anywhere.
- Touch only quant/src/** and LOGS.md.
- The host runs a separate cosmos-app service. Never touch, stop, or
  port-collide with it.
- No new markdown files, no tests, no UI, no frameworks, no extra features.
- Smallest complete program; one chat; report honestly what the exchange
  actually did, including failures. UNKNOWN stays UNKNOWN — never reported as
  success.
- A passing rerun never erases a first failure: name the first red in your rows.

## YOUR LOGS.md ROWS — newest on top, ≤ 10 lines, plain rows, no secrets
what ran | commit hash last-8 | order ids last-4 | fills + fees | final
balances | failures named (or none)
