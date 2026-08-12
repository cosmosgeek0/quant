# LOGS.md — builders write what was achieved; the Lead and the owner read it

Contract: newest entry on top, exact shape defined in BUILDER.md, written
LEAD-CLEAN (defined in LEAD.md): plain rows, no secrets (last-4 only), no raw
logs or error dumps — evidence referenced by path. One entry per task. No prose
beyond the shape. The Lead translates these for the owner in plain words on
every step.

---

2026-08-12T16:01:11Z–16:01:13Z | run 6a7c98c70019a8 | receipt /home/ssm-user/quant_receipts/roundtrip-6a7c98c70019a8.json
full commit SHA | d1b204bc4c82c5ffc8e495b7d4faa9085c71f0f9
symbol + approved ceiling | BTCUSDT | 10 USDT
BUY client + order ids (last-4) | 9a8b | 8130
SELL client + order ids (last-4) | 9a8s | 8167
terminal order states | BUY FILLED | SELL FILLED
executed quantities + quote totals | BUY 0.00015000 BTC / 9.51796950 USDT | SELL 0.00014000 BTC / 8.88343680 USDT
commissions grouped by asset | BTC 0.00000015 | USDT 0.00888344
starting and final base/quote balances | BTC 0.00000000 → 0.00000985 | USDT 95.62398534 → 94.98056920
net quote cost including USDT fees | 0.64341614 USDT
base introduced by this run remaining after SELL | 0.00000985 BTC dust
remaining open orders | 0
first failure | none
verdict | CLOSED

2026-08-12T15:52:45Z–15:52:46Z | run 6a7c96cdb1754b | receipt /home/ssm-user/quant_receipts/roundtrip-6a7c96cdb1754b.json
full commit SHA | d1b204bc4c82c5ffc8e495b7d4faa9085c71f0f9
symbol + approved ceiling | BTCUSDT | 5 USDT
BUY client + order ids (last-4) | 54bb | not submitted
SELL client + order ids (last-4) | 54bs | not submitted
terminal order states | BUY not submitted | SELL not submitted
executed quantities + quote totals | BUY 0 BTC / 0 USDT | SELL 0 BTC / 0 USDT
commissions grouped by asset | none
starting and final base/quote balances | start 0.00000000 BTC / 95.62398534 USDT | final not read; no exchange mutation
base introduced by this run remaining after SELL | 0 BTC
remaining open orders | 0 at preflight; no orders submitted
first failure | none
verdict | REJECTED_BEFORE_BUY
reason | approved 5 USDT too small to sell back after fees and step rounding; sufficient minimum ~6.282538382382565130260521042 USDT
