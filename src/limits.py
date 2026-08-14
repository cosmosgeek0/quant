"""Owner word 2026-08-14: the single per-leg money-cap authority."""

from decimal import Decimal


PER_LEG_CAP_USDT = Decimal("32")
FIRST_TRADE_CAP_USDT = Decimal("8")  # first autonomous trade runs at proven size
MAX_ADVERSE_PCT = Decimal("2")  # hard per-trade adverse stop, percent from entry
MEASURED_ROUNDTRIP_FEE_PCT = Decimal("0.30")
