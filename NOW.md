# NOW.md — current orders and measured truth (one page, rewritten in place)

## Orders (commander audit 2026-08-14, in force)
- KEEP STOP ACTIVE. No activation until the one vertical live lifecycle
  works end to end.
- BUILD THE VERTICAL, nothing else: momentum OPEN -> HOLD across ticks ->
  strategy-driven REDUCE/EXIT with actual fills feeding state; full-universe
  live volume into momentum; basis_watch actually wired to the supervisor
  on the host; ONE unified runtime control, one STOP, real commit identity,
  failure propagation, UI paths matching runtime truth.
- DEPLOYED PAUSED 2026-08-14 (release 26b5a8d2, superseded same day by
  7dcfd195 with the commander tracks): unified cron live, RELEASE identity
  valid, both STOP interlocks verified, ticks green and honest.
- THEN: the runner — not a human, not a pasted job — takes ONE naturally
  generated capped live trade, holds it across ticks, exits from the
  strategy, and records exact fills, fees, P&L, balances, flatness.
- AUDIT CORRECTIONS ON RECORD: the momentum receipt was queued manually by
  the Lead (autonomous signal-to-trade is NOT yet proven); the old
  MOMENTUM_ROUNDTRIP cannot hold and must never be armed as strategy; the
  "~five cents" cost figure is UNVERIFIED until commissions and ledger
  reconciliation are read signed; prior ws-feed and basis-cron wiring gaps
  are the current build.

## Truth (measured 2026-08-14)
- Runtime ALIVE-PAUSED on host: release 7dcfd195 deployed (RELEASE byte-read
  matches, seat-verified 2026-08-14T03:44Z), cron unified (runner 1m + runtime
  chain 3m), BOTH STOP interlocks present, no ACCEPT-ONE flag anywhere,
  supervisor honest IDLE every 3 min, market feed honestly STOPPED (feed
  stage runs only when STOP absent — runtime_tick.sh:175), no jobs since the
  three CLOSED proofs.
- Commander tracks live (815 tests, seat re-run green in 4s): STOP blocks NEW
  ENTRIES ONLY (exits+reconciliation sacred), account governor (one entry
  lock across strategies, daily budgets 5 entries / 1.00 USDT loss+fees,
  fail-closed), ACCEPT-ONE one-shot entry override consumed atomically by
  the runner then resealed, feed supervised into the runtime, executable
  sizing metadata. Exact acceptance-moment sequence: being pinned from code
  by a codex read — do not accept until it is on record here.
- Built and green: token stream (TRADING+PERPETUAL universe,
  measured live 132 spot-only / 167 perp-only / 357 both = 656), momentum
  lifecycle (APRUSDT fixture passes), stream-fed basis, six live-truth UI
  endpoints, proposal-only entry, full-equation net edge, supervisor with
  7 proven crash boundaries.
- Account (signed reads): spot 84.31 USDT + 0.00096 BNB dust; USD-M 11.03;
  zero positions; zero open orders. Commissions measured: 0.30% round trip.
- FUTURES PROVEN 2026-08-13T22:13Z: first real futures fills — 565 TST short
  opened and reduce-only closed, both FILLED, flat, receipted, ~1.4 cents,
  via the armed job runner with zero owner presses.
- BASIS CYCLE PROVEN 2026-08-13T22:20Z: four real fills (spot 568.1 in/568
  out, short 568 open/reduce-only close), verdict CLOSED, hedged exposure
  none, 0.1 TST dust recorded — the complete mechanics chain is live.
  MOMENTUM PROVEN 2026-08-13T23:21Z (receipt three).
- Money history: one CLOSED spot round trip (d1b204b); one honest
  OPEN_EXPOSURE freeze whose residual was later sold by the ordered
  reconcile; futures fills: six (short proof + basis cycle + momentum, 2026-08-13);
  completed basis cycles: one (mechanics-only); profit proven: none. Not
  guaranteed money — the machine refuses when the full equation is
  negative, which today it is.
