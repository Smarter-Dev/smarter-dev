"""Deterministic fixed-window and shared-overage calculations for web Chat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

ZERO = Decimal("0")
NORMAL_OVERAGE_RATE = Decimal("0.15")
ULTRA_OVERAGE_RATE = Decimal("0.25")


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def four_hour_bounds(first_message_at: datetime) -> tuple[datetime, datetime]:
    start = as_utc(first_message_at)
    return start, start + timedelta(hours=4)


def daily_bounds(at: datetime) -> tuple[datetime, datetime]:
    at = as_utc(at)
    start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def weekly_bounds(at: datetime) -> tuple[datetime, datetime]:
    """Return Sunday-aligned UTC half-open bounds containing ``at``."""
    at = as_utc(at)
    day = at.replace(hour=0, minute=0, second=0, microsecond=0)
    # datetime.weekday(): Monday=0; Sunday therefore moves back (6 + 1) % 7.
    start = day - timedelta(days=(day.weekday() + 1) % 7)
    return start, start + timedelta(days=7)


def total_tokens(input_tokens: int, output_tokens: int, **_: int) -> int:
    """Cache token counts are subsets of input and must never be added again."""
    return max(int(input_tokens or 0), 0) + max(int(output_tokens or 0), 0)


def overage_allowance(four_hour_limit: Decimal, *, ultra: bool) -> Decimal:
    rate = ULTRA_OVERAGE_RATE if ultra else NORMAL_OVERAGE_RATE
    return max(Decimal(four_hour_limit), ZERO) * rate


def shared_overage_used(
    *,
    four_hour_spend: Decimal,
    daily_spend: Decimal,
    weekly_spend: Decimal,
    four_hour_limit: Decimal,
    daily_limit: Decimal,
    weekly_limit: Decimal,
) -> Decimal:
    """One shared allowance: use the maximum excess, never sum window excesses."""
    return max(
        Decimal(four_hour_spend) - Decimal(four_hour_limit),
        Decimal(daily_spend) - Decimal(daily_limit),
        Decimal(weekly_spend) - Decimal(weekly_limit),
        ZERO,
    )


@dataclass(frozen=True, slots=True)
class SpendDecision:
    allowed: bool
    in_overage: bool
    hard_cutoff: bool
    overage_used: Decimal
    overage_allowance: Decimal
    limiting_windows: tuple[str, ...]


WIND_DOWN_WARNING = (
    "\n\n[Usage warning: this Chat is using its shared overage allowance. "
    "Wind down tool use and conclude with the best answer available.]"
)
USAGE_LIMIT_RESULT = (
    "Usage limit reached. Do not call more tools. Respond immediately with a "
    "concise final answer using the information already available."
)


def evaluate_spend(
    *,
    four_hour_spend: Decimal,
    daily_spend: Decimal,
    weekly_spend: Decimal,
    four_hour_limit: Decimal,
    daily_limit: Decimal,
    weekly_limit: Decimal,
    pending_cost: Decimal = ZERO,
    ultra: bool = False,
) -> SpendDecision:
    """Evaluate admission using committed/reserved spend plus ``pending_cost``.

    Equality with a base limit enters overage; equality with the hard limit is
    exhausted and denied. Zero limits therefore fail closed.
    """
    spends = {
        "four_hour": Decimal(four_hour_spend) + Decimal(pending_cost),
        "daily": Decimal(daily_spend) + Decimal(pending_cost),
        "weekly": Decimal(weekly_spend) + Decimal(pending_cost),
    }
    limits = {
        "four_hour": max(Decimal(four_hour_limit), ZERO),
        "daily": max(Decimal(daily_limit), ZERO),
        "weekly": max(Decimal(weekly_limit), ZERO),
    }
    limiting = tuple(name for name in spends if spends[name] >= limits[name])
    used = max(*(spends[name] - limits[name] for name in spends), ZERO)
    allowance = overage_allowance(limits["four_hour"], ultra=ultra)
    cutoff = bool(limiting) and used >= allowance
    return SpendDecision(
        allowed=not cutoff,
        in_overage=bool(limiting) and not cutoff,
        hard_cutoff=cutoff,
        overage_used=max(used, ZERO),
        overage_allowance=allowance,
        limiting_windows=limiting,
    )


def append_wind_down(result: str, *, in_overage: bool) -> str:
    return f"{result}{WIND_DOWN_WARNING}" if in_overage else result
