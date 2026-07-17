"""Tests for the Skrift admin help-conversations controller.

Covers the list, detail and retention-cleanup pages ported from the legacy
``smarter_dev.web.admin.views`` onto
``smarter_dev.web.bot_admin.help_conversations``.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from litestar.exceptions import NotFoundException

from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.bot_admin.help_conversations import (
    ConversationFilters,
    HelpConversationsAdminController,
    apply_conversation_filters,
    delete_expired_conversations,
    parse_conversation_filters,
    summarize_retention,
)
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    DiscordGuildSummary,
    GuildNotFoundError,
)
from smarter_dev.web.models import HelpConversation

_MODULE = "smarter_dev.web.bot_admin.help_conversations"

_GUILD = "111111111111111111"
_OTHER_GUILD = "222222222222222222"
_USER = "999999999999999999"


def _make_conversation(
    *,
    guild_id: str = _GUILD,
    user_id: str = _USER,
    user_username: str = "alice",
    interaction_type: str = "slash_command",
    is_resolved: bool = False,
    user_question: str = "How do I deploy?",
    bot_response: str = "Run the deploy job.",
    retention_policy: str = "standard",
    expires_at: datetime | None = None,
    started_at: datetime | None = None,
) -> HelpConversation:
    now = started_at or datetime.now(UTC)
    return HelpConversation(
        id=uuid4(),
        session_id=uuid4().hex,
        guild_id=guild_id,
        channel_id="333333333333333333",
        user_id=user_id,
        user_username=user_username,
        started_at=now,
        last_activity_at=now,
        interaction_type=interaction_type,
        is_resolved=is_resolved,
        user_question=user_question,
        bot_response=bot_response,
        tokens_used=42,
        response_time_ms=1200,
        retention_policy=retention_policy,
        expires_at=expires_at,
    )


async def _seed(db_session, *conversations: HelpConversation) -> None:
    for conversation in conversations:
        db_session.add(conversation)
    await db_session.commit()


@contextmanager
def _patch_list(guilds: list[DiscordGuildSummary] | None = None):
    """Patch the collaborators the list handler reaches."""
    client = SimpleNamespace(
        list_bot_guilds=AsyncMock(return_value=guilds or [])
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=client
    ), patch(
        f"{_MODULE}.get_flash_messages", return_value=[]
    ):
        yield


# --- parse_conversation_filters (pure) ---------------------------------------


def test_parse_conversation_filters_blanks_collapse_to_none():
    filters = parse_conversation_filters(
        {
            "guild_id": "  ",
            "user_id": "",
            "interaction_type": None,
            "search": "   ",
            "resolved_only": None,
        }
    )
    assert filters == ConversationFilters(
        guild_id=None,
        user_id=None,
        interaction_type=None,
        search=None,
        resolved_only=False,
    )


def test_parse_conversation_filters_keeps_values_and_strips():
    filters = parse_conversation_filters(
        {
            "guild_id": " 123 ",
            "user_id": "456",
            "interaction_type": "mention",
            "search": " deploy ",
            "resolved_only": "true",
        }
    )
    assert filters == ConversationFilters(
        guild_id="123",
        user_id="456",
        interaction_type="mention",
        search="deploy",
        resolved_only=True,
    )


def test_parse_conversation_filters_resolved_only_requires_true_literal():
    assert parse_conversation_filters({"resolved_only": "1"}).resolved_only is False
    assert parse_conversation_filters({"resolved_only": "on"}).resolved_only is False


# --- apply_conversation_filters (via db) -------------------------------------


async def _run_filter(db_session, filters: ConversationFilters) -> list:
    from sqlalchemy import select

    stmt = apply_conversation_filters(select(HelpConversation), filters)
    rows = await db_session.execute(stmt)
    return list(rows.scalars().all())


async def test_apply_filters_by_guild(db_session):
    keep = _make_conversation(guild_id=_GUILD)
    drop = _make_conversation(guild_id=_OTHER_GUILD)
    await _seed(db_session, keep, drop)

    result = await _run_filter(
        db_session,
        parse_conversation_filters({"guild_id": _GUILD}),
    )
    assert [c.id for c in result] == [keep.id]


async def test_apply_filters_by_type_and_resolved(db_session):
    keep = _make_conversation(interaction_type="mention", is_resolved=True)
    drop_type = _make_conversation(interaction_type="slash_command", is_resolved=True)
    drop_unresolved = _make_conversation(interaction_type="mention", is_resolved=False)
    await _seed(db_session, keep, drop_type, drop_unresolved)

    result = await _run_filter(
        db_session,
        parse_conversation_filters(
            {"interaction_type": "mention", "resolved_only": "true"}
        ),
    )
    assert [c.id for c in result] == [keep.id]


async def test_apply_filters_search_matches_question_response_username(db_session):
    by_question = _make_conversation(user_question="deploy the widget")
    by_response = _make_conversation(
        user_question="unrelated", bot_response="here is the DEPLOY step"
    )
    by_username = _make_conversation(user_question="hi", user_username="deployer")
    miss = _make_conversation(
        user_question="nothing", bot_response="nothing", user_username="bob"
    )
    await _seed(db_session, by_question, by_response, by_username, miss)

    result = await _run_filter(
        db_session, parse_conversation_filters({"search": "deploy"})
    )
    assert {c.id for c in result} == {
        by_question.id,
        by_response.id,
        by_username.id,
    }


# --- summarize_retention / delete_expired ------------------------------------


async def test_summarize_retention_counts_policies_and_expired(db_session):
    now = datetime.now(UTC)
    past = now - timedelta(days=1)
    future = now + timedelta(days=1)
    await _seed(
        db_session,
        _make_conversation(retention_policy="standard", expires_at=future),
        _make_conversation(retention_policy="standard", expires_at=past),
        _make_conversation(retention_policy="minimal", expires_at=past),
        _make_conversation(retention_policy="sensitive", expires_at=None),
    )

    breakdown = await summarize_retention(db_session, now)

    assert breakdown.standard == 2
    assert breakdown.minimal == 1
    assert breakdown.sensitive == 1
    assert breakdown.total == 4
    # Two rows have expires_at in the past; the None-expiry row never expires.
    assert breakdown.expired == 2


async def test_delete_expired_removes_only_expired_and_returns_count(db_session):
    from sqlalchemy import func, select

    now = datetime.now(UTC)
    keep_future = _make_conversation(expires_at=now + timedelta(days=1))
    keep_none = _make_conversation(expires_at=None)
    drop_past = _make_conversation(expires_at=now - timedelta(days=1))
    await _seed(db_session, keep_future, keep_none, drop_past)

    deleted = await delete_expired_conversations(db_session, now)
    await db_session.commit()

    assert deleted == 1
    remaining = await db_session.scalar(select(func.count(HelpConversation.id)))
    assert remaining == 2
    survivors = {
        row.id for row in (await db_session.execute(select(HelpConversation))).scalars()
    }
    assert survivors == {keep_future.id, keep_none.id}


# --- controller: list --------------------------------------------------------


def _list_handler():
    return HelpConversationsAdminController.list_conversations.fn


async def test_list_renders_and_paginates(db_session):
    await _seed(
        db_session,
        *[_make_conversation(user_question=f"q{i}") for i in range(3)],
    )
    with _patch_list(guilds=[DiscordGuildSummary(id=_GUILD, name="Alpha", icon=None)]):
        response = await _list_handler()(
            None,
            request=object(),
            db_session=db_session,
            size=2,
            page=1,
        )

    assert response.template_name == "admin/bot/help_conversations/list.html"
    assert response.context["total"] == 3
    assert response.context["total_pages"] == 2
    assert len(response.context["conversations"]) == 2
    assert response.context["guilds"][0].name == "Alpha"


async def test_list_applies_guild_filter(db_session):
    keep = _make_conversation(guild_id=_GUILD)
    drop = _make_conversation(guild_id=_OTHER_GUILD)
    await _seed(db_session, keep, drop)

    with _patch_list():
        response = await _list_handler()(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
        )

    assert response.context["total"] == 1
    assert response.context["conversations"][0].id == keep.id


async def test_list_survives_discord_outage(db_session):
    await _seed(db_session, _make_conversation())
    client = SimpleNamespace(
        list_bot_guilds=AsyncMock(side_effect=DiscordAdminError("boom"))
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=client
    ), patch(
        f"{_MODULE}.get_flash_messages", return_value=[]
    ):
        response = await _list_handler()(
            None, request=object(), db_session=db_session
        )

    assert response.context["guilds"] == []
    assert response.context["total"] == 1


# --- controller: detail ------------------------------------------------------


def _detail_handler():
    return HelpConversationsAdminController.conversation_detail.fn


async def test_detail_renders_with_guild_name(db_session):
    conversation = _make_conversation()
    await _seed(db_session, conversation)

    client = SimpleNamespace(
        get_guild=AsyncMock(
            return_value=SimpleNamespace(id=_GUILD, name="Alpha Guild")
        )
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=client
    ), patch(
        f"{_MODULE}.get_flash_messages", return_value=[]
    ):
        response = await _detail_handler()(
            None,
            request=object(),
            db_session=db_session,
            conversation_id=conversation.id,
        )

    assert response.template_name == "admin/bot/help_conversations/detail.html"
    assert response.context["conversation"].id == conversation.id
    assert response.context["guild"].name == "Alpha Guild"


async def test_detail_missing_raises_404(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ):
        with pytest.raises(NotFoundException):
            await _detail_handler()(
                None,
                request=object(),
                db_session=db_session,
                conversation_id=uuid4(),
            )


async def test_detail_falls_back_when_guild_unresolved(db_session):
    conversation = _make_conversation()
    await _seed(db_session, conversation)

    client = SimpleNamespace(
        get_guild=AsyncMock(side_effect=GuildNotFoundError("gone"))
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=client
    ), patch(
        f"{_MODULE}.get_flash_messages", return_value=[]
    ):
        response = await _detail_handler()(
            None,
            request=object(),
            db_session=db_session,
            conversation_id=conversation.id,
        )

    assert response.context["guild"].name == f"Guild {_GUILD}"


# --- controller: cleanup -----------------------------------------------------


def _cleanup_form_handler():
    return HelpConversationsAdminController.cleanup_form.fn


def _run_cleanup_handler():
    return HelpConversationsAdminController.run_cleanup.fn


async def test_cleanup_form_reports_counts(db_session):
    now = datetime.now(UTC)
    await _seed(
        db_session,
        _make_conversation(retention_policy="standard", expires_at=now - timedelta(days=1)),
        _make_conversation(retention_policy="minimal", expires_at=now + timedelta(days=1)),
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_flash_messages", return_value=[]
    ):
        response = await _cleanup_form_handler()(
            None, request=object(), db_session=db_session
        )

    assert response.template_name == "admin/bot/help_conversations/cleanup.html"
    assert response.context["total_count"] == 2
    assert response.context["standard_count"] == 1
    assert response.context["minimal_count"] == 1
    assert response.context["expired_count"] == 1


async def test_run_cleanup_deletes_flashes_and_redirects(db_session):
    from sqlalchemy import func, select

    now = datetime.now(UTC)
    await _seed(
        db_session,
        _make_conversation(expires_at=now - timedelta(days=1)),
        _make_conversation(expires_at=now - timedelta(days=2)),
        _make_conversation(expires_at=now + timedelta(days=1)),
    )
    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success):
        response = await _run_cleanup_handler()(
            None, request=object(), db_session=db_session
        )

    assert response.status_code in (302, 303, 307)
    assert response.url == "/admin/help-conversations/cleanup"
    flash_success.assert_called_once()

    remaining = await db_session.scalar(select(func.count(HelpConversation.id)))
    assert remaining == 1


# --- auth wiring -------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        HelpConversationsAdminController.list_conversations,
        HelpConversationsAdminController.cleanup_form,
        HelpConversationsAdminController.run_cleanup,
        HelpConversationsAdminController.conversation_detail,
    ],
)
def test_routes_require_admin(handler):
    guards = handler.guards
    assert auth_guard in guards
    admin_guards = [
        g
        for g in guards
        if isinstance(g, Permission) and g.permission == "administrator"
    ]
    assert admin_guards, "route must require the administrator permission"


async def test_administrator_permission_denies_non_admin_and_allows_admin():
    guard = Permission("administrator")
    non_admin = SimpleNamespace(permissions={"view-drafts"}, roles=set())
    admin = SimpleNamespace(permissions={"administrator"}, roles=set())

    assert await guard.check(non_admin) is False
    assert await guard.check(admin) is True
