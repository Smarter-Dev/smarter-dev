"""Tests for the local-harness seed data.

Regression coverage for the quests-daily-current harness failure: the seed
used the machine's LOCAL calendar day (``date.today()``) while the
``/quests/daily/current`` endpoint filters on ``get_date_provider().today()``
(the configured ``quest_timezone``, UTC by default).  Whenever the local day
differed from the quest-timezone day (e.g. 20:00-24:00 US/Eastern) the seeded
DailyQuest was invisible to the endpoint.  The seed must derive every seeded
calendar date from the same date provider the app uses.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from scripts.local_harness import config, seed
from smarter_dev.shared.date_provider import (
    MockDateProvider,
    reset_date_provider,
    set_date_provider,
)
from smarter_dev.web.models import BytesBalance, DailyQuest


@pytest.fixture(autouse=True)
def restore_date_provider():
    """Leave the global date provider untouched for other tests."""
    yield
    reset_date_provider()


def _seeded_daily_quest() -> DailyQuest:
    daily_quests = [row for row in seed._main_rows() if isinstance(row, DailyQuest)]
    assert len(daily_quests) == 1, "seed must create exactly one DailyQuest"
    return daily_quests[0]


def _seeded_primary_bytes_balance() -> BytesBalance:
    balances = [
        row
        for row in seed._adopted_bot_rows()
        if isinstance(row, BytesBalance) and row.user_id == config.USER_ID
    ]
    assert len(balances) == 1, "seed must create exactly one BytesBalance for the primary user"
    return balances[0]


def test_daily_quest_active_date_uses_date_provider_not_local_date():
    """The seeded active_date must match the provider's today, not date.today()."""
    provider_today = date(2030, 1, 2)
    assert provider_today != date.today()
    set_date_provider(MockDateProvider(fixed_date=provider_today))

    daily_quest = _seeded_daily_quest()

    assert daily_quest.active_date == provider_today


def test_daily_quest_matches_current_endpoint_filter_with_default_provider():
    """With the default (UTC) provider the seeded row satisfies the endpoint filter.

    /quests/daily/current selects DailyQuest rows where
    active_date == get_date_provider().today(), is_active is true, and
    expires_at is in the future — assert the seed meets all three.
    """
    reset_date_provider()

    daily_quest = _seeded_daily_quest()

    assert daily_quest.active_date == datetime.now(timezone.utc).date()
    assert daily_quest.is_active is True
    assert daily_quest.expires_at > datetime.now(timezone.utc)
    assert daily_quest.guild_id == config.GUILD_ID


def test_bytes_balance_last_daily_is_provider_yesterday():
    """last_daily must be provider-yesterday so the daily-claim check succeeds."""
    provider_today = date(2030, 1, 2)
    set_date_provider(MockDateProvider(fixed_date=provider_today))

    balance = _seeded_primary_bytes_balance()

    assert balance.last_daily == provider_today - timedelta(days=1)
