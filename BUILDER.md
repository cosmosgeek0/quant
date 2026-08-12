# BUILDER.md — the builder's ONLY file. Read nothing else, then act.

You are ONE fresh Codex builder. For this milestone you are also the SOLE
AWS/Binance mutator: the AWS DEPLOY chat stays idle, and no other chat may
mutate this Binance account or the host while you work. One task, then stop.

## THE TASK — review the baseline, then one real run
Baseline: src/spot_roundtrip.py at commit d7d2b8a3ce5d691877186977b2ee38ab5685132c
(github.com/cosmosgeek0/quant, local checkout Desktop/quant). It is a complete
one-file program: one Binance Spot market BUY capped by MAX_QUOTE, confirmed by
its own client order id; one market SELL of only what that BUY acquired; one
JSON receipt outside git; truth-gated verdict CLOSED / OPEN_EXPOSURE /
REJECTED_BEFORE_BUY / UNKNOWN. It exceeds the 350-line target because an
adversarial review's 11 blocker fixes went in (reason recorded in its
docstring); do not trim it back at the cost of any of those behaviors.

1. REVIEW the baseline against the real Binance Spot REST docs:
   https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade
   https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account
   https://developers.binance.com/en/docs/products/spot/filters
   https://developers.binance.com/en/docs/products/spot/faqs/commission_faq
   Fix ONLY defects that block the real loop: wrong endpoint/param/signing,
   filter misreads, Decimal errors, lifecycle holes. No refactors, no renames,
   no features, no new files, no tests, no classes. A Binance test-order call
   is already inline; it is never a milestone — the same run continues to the
   real order.
2. COMMIT and PUSH on main.
3. STOP and ask the owner for exactly two values:
   SYMBOL=<exact Spot symbol>   MAX_QUOTE=<max quote the BUY may spend>
   Then show the exact committed SHA and ask one question, verbatim:
   "Execute this SHA once on SYMBOL with a BUY ceiling of MAX_QUOTE?"
   Read-only inspection is allowed before the answer. No real order is.
4. On the owner's yes: run the committed version ONCE on the execution host
   through the existing helpers (quant/aws-connect/ssmrun.sh runs a script on
   the host; xfer.sh transfers files). Export SYMBOL, MAX_QUOTE, and
   QUANT_SHA=<the full committed SHA> for the process; credentials already
   live on the host side — never open, print, copy, or relay them. MAX_QUOTE
   is a hard ceiling: if filters make it invalid or too small to sell back,
   the program stops before the BUY and reports the sufficient minimum —
   relay that to the owner and STOP. Never raise the amount yourself.
5. Write the real result into LOGS.md (shape below). Stop. After the first
   live attempt — success, rejection, exposure, or unknown — no refactor, no
   retry, no second run. The owner picks the next task from what the exchange
   actually taught.

## UNKNOWN-RESULT RULE — binding
A timeout or error response does not prove Binance rejected an order. Never
blindly resend a mutating request; query the same client order id — it is a
reconciliation key, not permission to resend. If Binance truth cannot be
established: report UNKNOWN, submit nothing else, state the possible remaining
exposure, stop. A failed SELL or unresolved BUY never justifies an invented
repair trade — report the exact exposure and wait for the owner.

## HANDS OFF
- The host's separate cosmos-app service: never touch, stop, or port-collide.
- Touch only src/spot_roundtrip.py and LOGS.md. No transfers, Futures, Margin,
  strategies, repetition, or infrastructure of any kind.
- The program refuses to run over an unreconciled prior receipt or a standing
  run.lock in RECEIPT_DIR — that refusal is law, not an error to code around.

## YOUR LOGS.md ROWS — newest on top, ≤ 20 lines, plain rows, no secrets
full commit SHA | symbol + approved ceiling | BUY and SELL client + order ids
(last-4) | terminal order states | executed quantities + quote totals |
commissions grouped by asset | starting and final base/quote balances | base
introduced by this run remaining after the SELL | remaining open orders |
first failure, if any | verdict: CLOSED / OPEN_EXPOSURE /
REJECTED_BEFORE_BUY / UNKNOWN
