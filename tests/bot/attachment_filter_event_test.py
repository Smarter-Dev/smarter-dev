"""The attachment filter's deletes, through the moderation gateway.

A blocked attachment's delete is one gateway operation: one ``moderation_actions``
row, announced once (which writes the bot's short-term event log). A warn-only
tier touches nobody's message and is deliberately NOT remembered as a delete,
and a delete that did not happen is never remembered either.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import hikari
import pytest

from smarter_dev.bot import attachment_filter
from smarter_dev.web.models import AttachmentFilterConfig
from tests.bot.fake_discord import FakeDiscord
from tests.bot.fake_discord import server_error

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


MESSAGE_ID = 300


class _FakeRest:
    def __init__(self) -> None:
        self.created_messages: list[tuple] = []
        self.deleted: list[int] = []
        self.delete_error: Exception | None = None
        self.discord: FakeDiscord | None = None

    async def create_message(self, channel, content=None, **kwargs):
        self.created_messages.append((int(channel), content, kwargs))
        return SimpleNamespace(id=1)

    async def delete_message(self, channel, message, **kwargs):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(int(message))

    async def fetch_member(self, guild, user):
        return await self.discord.rest.fetch_member(guild, user)


class _FakeBot:
    def __init__(self) -> None:
        self.rest = _FakeRest()
        self.d: dict = {}
        # The guild the staff-protection check reads; the uploader is an
        # ordinary member unless a test says otherwise.
        self.discord = FakeDiscord(guild_id=GUILD_ID)
        self.discord.add_member(AUTHOR_ID)
        self.rest.discord = self.discord
        self.cache = self.discord.cache

    def get_me(self):
        return self.discord.get_me()


def _event(*filenames: str):
    message = SimpleNamespace(
        attachments=[SimpleNamespace(filename=filename) for filename in filenames],
        content="",
    )
    return SimpleNamespace(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        message_id=MESSAGE_ID,
        message=message,
        author=SimpleNamespace(id=AUTHOR_ID, username="uploader", is_bot=False),
        get_guild=lambda: None,
    )


@pytest.fixture
def filter_env(monkeypatch):
    """Patches the config read and captures every announced action."""
    from smarter_dev.bot import moderation_port

    state = SimpleNamespace(config=_config(), announced=[])

    async def fake_get_config(self, session, guild_id):
        return state.config

    async def capture(action, *, bot=None) -> None:
        state.announced.append(action)

    @contextlib.asynccontextmanager
    async def fake_session_context():
        yield SimpleNamespace()

    monkeypatch.setattr(
        attachment_filter.AttachmentFilterConfigOperations,
        "get_config",
        fake_get_config,
    )
    monkeypatch.setattr(moderation_port, "dispatch_mod_action", capture)
    monkeypatch.setattr(
        attachment_filter, "get_db_session_context", fake_session_context
    )
    return state


async def test_blocked_attachment_delete_is_recorded_and_announced(filter_env):
    bot = _FakeBot()
    event = _event("payload.exe")

    assert await attachment_filter.check_attachment_filter(bot, event) is True

    assert bot.rest.deleted == [MESSAGE_ID]
    [action] = filter_env.announced
    assert action.action_type == "delete"
    assert action.guild_id == str(GUILD_ID)
    assert action.target_username == "uploader"
    assert action.channel_id == str(CHANNEL_ID)
    assert "payload.exe" in action.reason
    assert action.source == "handler"
    assert action.actor == "attachment_filter"
    # The notice still goes out to the uploader.
    assert len(bot.rest.created_messages) == 1


async def test_a_message_another_detector_already_removed_gets_no_second_notice(
    filter_env,
):
    bot = _FakeBot()
    event = _event("payload.exe")
    await attachment_filter.check_attachment_filter(bot, event)
    bot.rest.created_messages.clear()

    assert await attachment_filter.check_attachment_filter(bot, event) is False

    assert bot.rest.deleted == [MESSAGE_ID]
    assert bot.rest.created_messages == []


async def test_warn_only_attachment_is_not_remembered_as_a_delete(filter_env):
    bot = _FakeBot()
    event = _event("bundle.zip")

    assert await attachment_filter.check_attachment_filter(bot, event) is True

    assert bot.rest.deleted == []
    assert filter_env.announced == []


async def test_ignored_attachment_records_nothing(filter_env):
    bot = _FakeBot()

    assert await attachment_filter.check_attachment_filter(bot, _event("shot.png")) is False

    assert filter_env.announced == []


async def test_a_delete_that_failed_is_never_remembered(filter_env):
    """The message is still there — the bot must not claim it removed it."""
    bot = _FakeBot()
    bot.rest.delete_error = hikari.ForbiddenError(
        url="url", headers={}, raw_body=b"", message="no permission"
    )

    assert await attachment_filter.check_attachment_filter(bot, _event("payload.exe")) is False

    assert filter_env.announced == []
    assert bot.rest.created_messages == []


async def test_staff_get_the_notice_but_keep_their_message(filter_env):
    bot = _FakeBot()
    bot.discord.roles[901] = hikari.Permissions.MANAGE_WEBHOOKS
    bot.discord.add_member(AUTHOR_ID, role_ids=(901,))
    event = _event("payload.exe")

    assert await attachment_filter.check_attachment_filter(bot, event) is True

    assert bot.rest.deleted == []
    assert filter_env.announced == []
    assert len(bot.rest.created_messages) == 1


async def test_an_uploader_who_cannot_be_checked_keeps_their_message(filter_env):
    bot = _FakeBot()
    bot.discord.cached = False

    async def discord_down(*args, **kwargs):
        raise server_error()

    bot.rest.fetch_member = discord_down
    event = _event("payload.exe")

    assert await attachment_filter.check_attachment_filter(bot, event) is True

    assert bot.rest.deleted == []
    assert len(bot.rest.created_messages) == 1
