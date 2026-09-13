"""Active energy-policy planning parameters."""

from __future__ import annotations

from datetime import date

POLICY_ID = "energy-credit/v5"
DEBIT_EFFECTIVE_FROM = date(2026, 9, 9)
DAILY_ADJUSTMENT_MKCAL = 200_000
EXCEPTIONAL_BURN_MKCAL = 1_000_000
EXCEPTIONAL_DURATION_MS = 180 * 60_000
REVIEW_AFTER_ELIGIBLE_DAYS = 28
