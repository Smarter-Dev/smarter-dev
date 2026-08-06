"""Tests for the Skrift admin chat-memory page.

One read-only view under ``/admin/bot/guilds/{guild_id}/chat-memory`` showing
all three memory layers: the long-term blob, the mid-term notes, and the dream
revision history (with a link out to the conversations dashboard for the
short-term layer). Follows the sibling admin controller tests' pattern of
invoking the route handler's ``.fn`` directly with patched guild resolution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from smarter_dev.web.bot_admin.chat_memory import ChatMemoryAdminController
from smarter_dev.web.crud import list_memory_revisions
from smarter_dev.web.discord_admin_client import (
    DiscordGuildDetail,
    GuildNotFoundError,
)
from smarter_dev.web.models import (
    ChatAgentGuildMemory,
    ChatAgentMemoryNote,
    ChatAgentMemoryRevision,
)

_GUILD = "111111111111111111"
_MODULE = "smarter_dev.web.bot_admin.chat_memory"
# Guild resolution is shared with the campaigns module, so the Discord client is
# patched where the shared helper actually looks it up.
_CAMPAIGNS_MODULE = "smarter_dev.web.bot_admin.campaigns"


def _guild_detail() -> DiscordGuildDetail:
    return DiscordGuildDetail(
        id=_GUILD,
        name="Alpha Guild",
        icon=None,
        owner_id="owner",
        member_count=42,
        description=None,
    )


def _admin_client() -> SimpleNamespace:
    return SimpleNamespace(get_guild=AsyncMock(return_value=_guild_detail()))


async def _seed_blob(
    db_session,
    *,
    guild_id: str = _GUILD,
    content: str = "The guild likes Python.",
    revision: int = 3,
    memory_enabled: bool = True,
) -> ChatAgentGuildMemory:
    blob = ChatAgentGuildMemory(
        guild_id=guild_id,
        content=content,
        revision=revision,
        last_dream_at=datetime.now(UTC) - timedelta(hours=8),
        notes_consumed=4,
        model_name="test-model",
        memory_enabled=memory_enabled,
    )
    db_session.add(blob)
    await db_session.commit()
    await db_session.refresh(blob)
    return blob


async def _seed_note(
    db_session,
    *,
    guild_id: str = _GUILD,
    content: str = "Zech is shipping a memory admin page.",
    created_at: datetime | None = None,
) -> ChatAgentMemoryNote:
    note = ChatAgentMemoryNote(
        guild_id=guild_id,
        channel_id="222222222222222222",
        channel_name="general",
        content=content,
        engagement_id=None,
    )
    db_session.add(note)
    await db_session.commit()
    if created_at is not None:
        note.created_at = created_at
        await db_session.commit()
    await db_session.refresh(note)
    return note


async def _seed_revision(
    db_session,
    *,
    guild_id: str = _GUILD,
    revision: int,
    content: str = "An earlier night's memory.",
) -> ChatAgentMemoryRevision:
    record = ChatAgentMemoryRevision(
        guild_id=guild_id,
        content=content,
        revision=revision,
        notes_consumed=2,
        model_name="test-model",
    )
    db_session.add(record)
    await db_session.commit()
    await db_session.refresh(record)
    return record


def _patched(client=None):
    client = client or _admin_client()
    return (
        patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})),
        patch(f"{_CAMPAIGNS_MODULE}.get_admin_context", new=AsyncMock(return_value={})),
        patch(f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=client),
    )


async def _render(db_session, guild_id: str = _GUILD, client=None):
    a, b, c = _patched(client)
    with a, b, c:
        return await ChatMemoryAdminController.chat_memory_view.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=guild_id,
        )


# --- crud: list_memory_revisions ---------------------------------------------


async def test_list_memory_revisions_newest_first_and_scoped(db_session):
    await _seed_revision(db_session, revision=1)
    await _seed_revision(db_session, revision=3)
    await _seed_revision(db_session, revision=2)
    await _seed_revision(db_session, guild_id="999999999999999999", revision=9)

    revisions = await list_memory_revisions(db_session, _GUILD)

    assert [r.revision for r in revisions] == [3, 2, 1]


# --- controller: view --------------------------------------------------------


async def test_view_renders_all_three_layers(db_session):
    await _seed_blob(db_session, content="Long-term facts about the guild.")
    await _seed_note(db_session, content="A fresh note.")
    await _seed_revision(db_session, revision=1)
    await _seed_revision(db_session, revision=2)

    response = await _render(db_session)

    assert response.template_name == "admin/bot/chat_memory/view.html"
    assert response.context["active_page"] == "chat_memory"
    assert response.context["guild_id"] == _GUILD
    assert response.context["blob"].content == "Long-term facts about the guild."
    assert [n.content for n in response.context["notes"]] == ["A fresh note."]
    assert [r.revision for r in response.context["revisions"]] == [2, 1]
    assert response.context["notes_since"].tzinfo is not None


async def test_view_renders_before_first_dream(db_session):
    response = await _render(db_session)

    assert response.template_name == "admin/bot/chat_memory/view.html"
    assert response.context["blob"] is None
    assert response.context["notes"] == []
    assert response.context["revisions"] == []


async def test_view_shows_notes_from_before_today(db_session):
    """Surviving old notes (a failed dream) must be visible, not day-filtered."""
    await _seed_note(
        db_session,
        content="Left over from two days ago.",
        created_at=datetime.now(UTC) - timedelta(days=2),
    )

    response = await _render(db_session)

    assert [n.content for n in response.context["notes"]] == [
        "Left over from two days ago."
    ]


async def test_view_scopes_to_guild(db_session):
    await _seed_blob(db_session, guild_id="999999999999999999", content="Other guild.")
    await _seed_note(db_session, guild_id="999999999999999999")
    await _seed_revision(db_session, guild_id="999999999999999999", revision=1)

    response = await _render(db_session)

    assert response.context["blob"] is None
    assert response.context["notes"] == []
    assert response.context["revisions"] == []


async def test_view_still_shows_content_when_memory_disabled(db_session):
    """The forget switch hides memory from the bot, never from the admin."""
    await _seed_blob(db_session, content="Withheld from the bot.", memory_enabled=False)

    response = await _render(db_session)

    assert response.context["blob"].content == "Withheld from the bot."
    assert response.context["blob"].memory_enabled is False


async def test_view_guild_not_found_returns_404(db_session):
    client = SimpleNamespace(get_guild=AsyncMock(side_effect=GuildNotFoundError("x")))

    response = await _render(db_session, guild_id="missing", client=client)

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"
