"""Owner word 2026-08-14: the single per-leg money-cap authority."""

import os
from decimal import Decimal


PER_LEG_CAP_USDT = Decimal("32")
FIRST_TRADE_CAP_USDT = Decimal("8")  # first autonomous trade runs at proven size
MAX_ADVERSE_PCT = Decimal("2")  # hard per-trade adverse stop, percent from entry
MEASURED_ROUNDTRIP_FEE_PCT = Decimal("0.30")


def _positive_decimal_env(name, default):
    try:
        value = Decimal(os.environ.get(name) or default)
    except (ArithmeticError, TypeError, ValueError):
        return Decimal(default)
    if not value.is_finite() or value <= Decimal("0"):
        return Decimal(default)
    return value


def _positive_int_env(name, default):
    try:
        value = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return int(default)
    if value <= 0:
        return int(default)
    return value


# Env knobs clamp to sane ceilings: a host env typo must never lift the
# budgets by orders of magnitude (auditor hardening, 2026-08-15).
MOMENTUM_IGNITION_SURGE_RATIO = min(
    max(_positive_decimal_env("QUANT_IGNITION_SURGE", "3"), Decimal("1")),
    Decimal("100"),
)
MAX_ENTRIES_PER_UTC_DAY = min(_positive_int_env("QUANT_MAX_ENTRIES_DAY", "10"), 24)
DAILY_LOSS_PLUS_FEES_USDT = min(
    _positive_decimal_env("QUANT_DAILY_LOSS_CAP", "2.00"), Decimal("10.00")
)
