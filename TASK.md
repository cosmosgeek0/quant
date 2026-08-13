# TASK.md — current work orders

WAVE 4 — ONE TOKEN STREAM, TWO TRADES, ONE HAND (commander order 2026-08-13,
owner-transmitted; simultaneous lanes, no lane waits on another's tests; test
totals are not progress; operating loop = tiny live attempt -> actual Binance
failure -> repair -> ONE targeted regression -> rerun live):
1. FUTURES MECHANICS PROOF: the existing futures hand runs ONE capped live
   short + reduce-only close on the host (zero new code, MAX_NOTIONAL=8,
   TSTUSDT already 1x, EXECUTION_MECHANICS_ONLY — never called profit).
   Trigger prepared; fires on the owner's press only.
2. STREAM: one continuous Spot+Futures universe stream — every USDT market
   (490 spot, 527 perps, incl. the 169 futures-only the old scanners were
   blind to; APRUSDT class). One live record per token: price, volume +
   surge ratio, OI, funding, basis gap where both venues exist. No
   alphabetical caps. Restart-safe.
3. MOMENTUM: dynamic entry/add/reduce/exit lifecycle against the shared
   stream (ignition = volume surge + persistence; add only on strengthening;
   trail on deterioration; exit on break). Terminology law: funding and
   basis are different numbers; momentum and basis are different trades on
   the same token record; the system decides which trade a token offers.
4. BASIS-CONNECT: basis reads the SAME token stream (private scans retired)
   and executes through the SAME one hand.
5. UI: serves actual candidates, orders, positions, fees, funding, realized
   PnL from runtime truth — not fixtures.
Serial only: dependent orders, same-symbol mutations, unclear-response
reconciliation. Everything else concurrent. Not guaranteed money: APR
collapsed 36.5% after its peak; chasing late loses — the machine trades
transitions, never regret.
