# NOW.md — current orders and measured truth (one page, rewritten in place)

## Orders (owner + commander 2026-08-14: MONEY FIRST, LIVE FIRST)
- FEATURE FREEZE: no new scanners, test waves, panels, UI expansion, or
  documentation until three live receipts exist.
- THE RAIL: deterministic job runner on the host — owner arms ONCE
  (scripts/arm_jobs_on_host.sh), then the Lead queues restricted job records
  and reads receipts; no per-test press, no pasted commands.
- SEQUENCE, live receipts gating each step: (1) FUTURES_ROUNDTRIP job — $8
  short + reduce-only close, two real fills, flat, receipted; a Binance
  rejection is repaired exactly, one regression, redeploy, rerun. (2) BASIS
  mechanics cycle — four real fills + reconciliation, mechanics-only, cents
  accepted (runner unlocks it only after the futures proof passes).
  (3) Momentum made real — smallest adapter to the existing hands, one real
  tiny entry + exit; futures-only tokens get the directional futures path.
  (4) Only then: parallel build resumes, every new money capability tested
  live in the same task.
- Serial only: dependent orders, same-symbol mutations, unclear-response
  reconciliation. Everything else concurrent.

## Truth (measured 2026-08-14)
- Runtime ALIVE-PAUSED on host: cron fires the supervisor every 2 min, STOP
  seals entry, first tick measured honest IDLE. Deployed modules sha-verified
  host == local 4/4 (code state as of commit 4433830).
- Built and green (696 tests): token stream (TRADING+PERPETUAL universe,
  measured live 132 spot-only / 167 perp-only / 357 both = 656), momentum
  lifecycle (APRUSDT fixture passes), stream-fed basis, six live-truth UI
  endpoints, proposal-only entry, full-equation net edge, supervisor with
  7 proven crash boundaries.
- Account (signed reads): spot 84.31 USDT + 0.00096 BNB dust; USD-M 11.03;
  zero positions; zero open orders. Commissions measured: 0.30% round trip.
- FUTURES PROVEN 2026-08-13T22:13Z: first real futures fills — 565 TST short
  opened and reduce-only closed, both FILLED, flat, receipted, ~1.4 cents,
  via the armed job runner with zero owner presses. Basis mechanics job
  queued next in sequence.
- Money history: one CLOSED spot round trip (d1b204b); one honest
  OPEN_EXPOSURE freeze whose residual was later sold by the ordered
  reconcile; futures fills: two (the 2026-08-13T22:13Z proof); zero completed basis cycles; profit
  proven: none. Not guaranteed money — the machine refuses when the full
  equation is negative, which today it is.
