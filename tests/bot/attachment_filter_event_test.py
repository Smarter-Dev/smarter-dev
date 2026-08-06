"""The attachment filter's contribution to the bot's short-term event log.

An attachment delete writes no ``ModerationAction`` row, so it never reaches the
``mod_action`` dispatch — the filter captures it directly. A warn-only tier
touches nobody's message and is deliberately NOT remembered as a delete.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import hikari
import pytest

from smarter_dev.bot import attachment_filter
from smarter_dev.web.models import AttachmentFilterConfig

GUILD_ID = 100
CHANNEL_ID = 200
AUTHOR_ID = 400


def _config(**overrides) -> AttachmentFilterConfig:
    fields = {
        "guild_id": str(GUILD_ID),
        "is_active": True,
        "ignored_extensions": [".png"],
        "warn_extensions": [".zip"],
    }
    fields.update(overrides)
    return AttachmentFilterConfig(**fields)


class _FakeRest:
    def __init__(self) -> None:
        self.created_messages: list[tuple] = []

    async def create_message(self, channel, content=None, **kwargs):
        self.created_messages.append((int(channel), content, kwargs))
        return SimpleNamespace(id=1)


class _FakeBot:
    def __init__(self) -> None:
        self.rest = _FakeRest()
        self.d: dict = {}


class _FakeMessage:
    def __init__(self, filenames: tuple[str, ...], delete_error: Exception | None):
        self.attachments = [
            SimpleNamespace(filename=filename) for filename in filenames
        ]
        self._delete_error = delete_error
        self.deleted = False

    async def delete(self) -> None:
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted = True


def _event(*filenames: str, delete_error: Exception | None = None):
    message = _FakeMessage(filenames, delete_error)
    return SimpleNamespace(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        message=message,
        author=SimpleNamespace(id=AUTHOR_ID, username="uploader"),
        get_guild=lambda: None,
    )


@pytest.fixture
def filter_env(monkeypatch):
    """Patches the config read and captures every recorded guild event."""
    state = SimpleNamespace(config=_config(), recorded_events=[])

    async def fake_get_config(self, session, guild_id):
        return state.config

    async def capture_event(bot, event, **kwargs) -> None:
        state.recorded_events.append(event)

    @contextlib.asynccontextmanager
    async def fake_session_context():
        yield SimpleNamespace()

    monkeypatch.setattr(
        attachment_filter.AttachmentFilterConfigOperations,
        "get_config",
        fake_get_config,
    )
    monkeypatch.setattr(attachment_filter, "record_guild_event", capture_event)
    monkeypatch.setattr(
        attachment_filter, "get_db_session_context", fake_session_context
    )
    return state


async def test_blocked_attachment_delete_is_written_to_the_event_log(filter_env):
    bot = _FakeBot()
    event = _event("payload.exe")

    assert await attachment_filter.check_attachment_filter(bot, event) is True

    assert event.message.deleted is True
    assert len(filter_env.recorded_events) == 1
    recorded = filter_env.recorded_events[0]
    assert recorded.kind == "mod_action"
    assert recorded.guild_id == str(GUILD_ID)
    assert recorded.action == "delete"
    assert recorded.target_username == "uploader"
    assert recorded.channel_id == str(CHANNEL_ID)
    assert "payload.exe" in recorded.reason
    assert recorded.source == "handler"


async def test_warn_only_attachment_is_not_remembered_as_a_delete(filter_env):
    bot = _FakeBot()
    event = _event("bundle.zip")

    assert await attachment_filter.check_attachment_filter(bot, event) is True

    assert event.message.deleted is False
    assert filter_env.recorded_events == []


async def test_ignored_attachment_records_nothing(filter_env):
    bot = _FakeBot()

    assert await attachment_filter.check_attachment_filter(bot, _event("shot.png")) is False

    assert filter_env.recorded_events == []


async def test_a_delete_that_failed_is_never_remembered(filter_env):
    """The message is still there — the bot must not claim it removed it."""
    bot = _FakeBot()
    event = _event(
        "payload.exe",
        delete_error=hikari.ForbiddenError(
            url="url", headers={}, raw_body=b"", message="no permission"
        ),
    )

    assert await attachment_filter.check_attachment_filter(bot, event) is False

    assert filter_env.recorded_events == []
