# NOW.md — current orders and measured truth (one page, rewritten in place)

## Orders (wave 4 standing, commander 2026-08-13; owner-transmitted)
- FUTURES MECHANICS PROOF: staged as scripts/run_futures_proof_on_host.sh
  (existing hand, TSTUSDT at 1x, MAX_NOTIONAL=8, reduce-only close,
  EXECUTION_MECHANICS_ONLY). Fires ONLY on the owner's press. Whatever
  Binance answers becomes the next repair.
- Operating loop: tiny live attempt → actual failure → repair → one
  targeted regression → rerun live. Test totals are not progress.
- Serial only: dependent orders, same-symbol mutations, unclear-response
  reconciliation. Everything else concurrent.
- Pending owner/commander words: acceptance of wave-3R + wave-4; activation
  words for stream/momentum live modes; UI WORK chat to build on the six
  runtime endpoints.

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
- Money history: one CLOSED spot round trip (d1b204b); one honest
  OPEN_EXPOSURE freeze whose residual was later sold by the ordered
  reconcile; zero futures fills ever; zero completed basis cycles; profit
  proven: none. Not guaranteed money — the machine refuses when the full
  equation is negative, which today it is.
