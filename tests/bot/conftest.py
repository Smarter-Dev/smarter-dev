"""Shared fixtures for the bot tests."""

from __future__ import annotations

import pytest

from smarter_dev.bot.privacy.blocked_users import get_blocked_users


@pytest.fixture(autouse=True)
def blocked_users_list_loaded():
    """Production refuses Discord model input until the blocked-users list
    loads; most tests run as if an empty list had loaded. Cold-start tests
    call ``get_blocked_users().reset()`` themselves."""
    blocked = get_blocked_users()
    blocked.load(0, [])
    yield blocked
    blocked.reset()
