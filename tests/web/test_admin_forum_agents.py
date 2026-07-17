"""Tests for the Skrift admin forum-agents controller."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from litestar.datastructures import FormMultiDict

from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.bot_admin.forum_agents import (
    ForumAgentsAdminController,
    flatten_agent_analytics,
    format_response_details,
    read_forum_agent_form,
    validate_forum_agent_form,
)
from smarter_dev.web.crud import ForumAgentOperations
from smarter_dev.web.discord_admin_client import (
    DiscordGuildDetail,
    GuildNotFoundError,
)
from smarter_dev.web.models import (
    ForumAgent,
    ForumAgentResponse,
)

_GUILD = "111111111111111111"
_FORUM = "222222222222222222"
_MODULE = "smarter_dev.web.bot_admin.forum_agents"


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


def _form(pairs: list[tuple[str, str]]) -> FormMultiDict:
    return FormMultiDict(pairs)


async def _seed_agent(
    db_session,
    *,
    name: str = "Helper",
    is_active: bool = True,
    monitored_forums: list[str] | None = None,
) -> ForumAgent:
    agent = ForumAgent(
        guild_id=_GUILD,
        name=name,
        description="",
        system_prompt="You are a helpful forum assistant.",
        monitored_forums=monitored_forums or [_FORUM],
        response_threshold=0.7,
        max_responses_per_hour=5,
        is_active=is_active,
        created_by="admin",
        enable_user_tagging=False,
        enable_responses=True,
        notification_topics=[],
    )
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


async def _seed_response(
    db_session,
    agent: ForumAgent,
    *,
    responded: bool,
    confidence: float,
    tokens: int,
) -> ForumAgentResponse:
    response = ForumAgentResponse(
        agent_id=agent.id,
        guild_id=_GUILD,
        channel_id=_FORUM,
        thread_id="333",
        post_title="How do I center a div?",
        post_content="Please help.",
        author_display_name="Curious",
        post_tags=["css"],
        attachments=[],
        decision_reason="Question is answerable.",
        confidence_score=confidence,
        response_content="Use flexbox." if responded else "",
        tokens_used=tokens,
        response_time_ms=1200,
        responded=responded,
    )
    db_session.add(response)
    await db_session.commit()
    await db_session.refresh(response)
    return response


# --- pure: read_forum_agent_form ---------------------------------------------


def test_read_forum_agent_form_extracts_scalars_and_lists():
    data = read_forum_agent_form(
        _form(
            [
                ("name", "  Helper  "),
                ("system_prompt", "  You are a bot.  "),
                ("response_threshold", "0.8"),
                ("max_responses_per_hour", "9"),
                ("monitored_forums[]", "111"),
                ("monitored_forums[]", "222"),
                ("is_active", "on"),
                ("enable_responses", "on"),
                ("notification_topics[]", "CSS"),
                ("notification_topic_descriptions[]", "Styling"),
            ]
        )
    )
    assert data["name"] == "Helper"
    assert data["system_prompt"] == "You are a bot."
    assert data["response_threshold"] == "0.8"
    assert data["max_responses_per_hour"] == "9"
    assert data["monitored_forums"] == ["111", "222"]
    assert data["is_active"] is True
    assert data["enable_responses"] == "on"
    assert data["enable_user_tagging"] is None
    assert data["notification_topics"] == ["CSS"]
    assert data["notification_topic_descriptions"] == ["Styling"]


def test_read_forum_agent_form_defaults_for_blank_form():
    data = read_forum_agent_form(_form([]))
    assert data["name"] == ""
    assert data["response_threshold"] == "0.7"
    assert data["max_responses_per_hour"] == "5"
    assert data["monitored_forums"] == []
    assert data["is_active"] is False


# --- pure: validate_forum_agent_form -----------------------------------------


def _valid_raw() -> dict:
    return {
        "name": "Helper",
        "description": "A helper",
        "system_prompt": "You are a helpful forum assistant.",
        "response_threshold": "0.7",
        "max_responses_per_hour": "5",
        "monitored_forums": ["111", "  ", ""],
        "is_active": True,
        "enable_responses": "on",
        "enable_user_tagging": None,
        "notification_topics": [],
        "notification_topic_descriptions": [],
    }


def test_validate_happy_path_cleans_forums_and_types():
    ok, errors, cleaned = validate_forum_agent_form(_valid_raw())
    assert ok is True
    assert errors == []
    assert cleaned["monitored_forums"] == ["111"]
    assert cleaned["response_threshold"] == 0.7
    assert cleaned["max_responses_per_hour"] == 5
    assert cleaned["enable_responses"] is True
    assert cleaned["enable_user_tagging"] is False


def test_validate_does_not_mutate_input():
    raw = _valid_raw()
    validate_forum_agent_form(raw)
    assert raw["enable_responses"] == "on"
    assert raw["monitored_forums"] == ["111", "  ", ""]


def test_validate_requires_name():
    raw = _valid_raw()
    raw["name"] = "   "
    ok, errors, _ = validate_forum_agent_form(raw)
    assert ok is False
    assert any("name is required" in e.lower() for e in errors)


def test_validate_rejects_short_system_prompt():
    raw = _valid_raw()
    raw["system_prompt"] = "short"
    ok, errors, _ = validate_forum_agent_form(raw)
    assert ok is False
    assert any("at least 10 characters" in e for e in errors)


def test_validate_requires_at_least_one_mode():
    raw = _valid_raw()
    raw["enable_responses"] = None
    raw["enable_user_tagging"] = None
    ok, errors, _ = validate_forum_agent_form(raw)
    assert ok is False
    assert any("at least one mode" in e.lower() for e in errors)


def test_validate_rejects_out_of_range_threshold():
    raw = _valid_raw()
    raw["response_threshold"] = "1.5"
    ok, errors, _ = validate_forum_agent_form(raw)
    assert ok is False
    assert any("threshold must be between" in e.lower() for e in errors)


def test_validate_rejects_non_numeric_rate_limit():
    raw = _valid_raw()
    raw["max_responses_per_hour"] = "abc"
    ok, errors, _ = validate_forum_agent_form(raw)
    assert ok is False
    assert any("rate limit must be a valid number" in e.lower() for e in errors)


def test_validate_tagging_requires_topics():
    raw = _valid_raw()
    raw["enable_user_tagging"] = "on"
    raw["notification_topics"] = ["  ", ""]
    ok, errors, _ = validate_forum_agent_form(raw)
    assert ok is False
    assert any("at least one notification topic" in e.lower() for e in errors)


def test_validate_tagging_pads_descriptions_to_topics():
    raw = _valid_raw()
    raw["enable_user_tagging"] = "on"
    raw["notification_topics"] = ["CSS", "HTML"]
    raw["notification_topic_descriptions"] = ["Styling"]
    ok, errors, cleaned = validate_forum_agent_form(raw)
    assert ok is True
    assert cleaned["notification_topics"] == ["CSS", "HTML"]
    assert cleaned["notification_topic_descriptions"] == ["Styling", ""]


# --- pure: analytics + response details --------------------------------------


def test_flatten_agent_analytics_lifts_statistics():
    analytics = {
        "agent": {"name": "Helper"},
        "statistics": {
            "total_evaluations": 10,
            "total_responses": 4,
            "response_rate": 0.4,
            "total_tokens_used": 500,
            "average_confidence": 0.75,
            "average_response_time_ms": None,
        },
    }
    flat = flatten_agent_analytics(analytics)
    assert flat["total_evaluations"] == 10
    assert flat["total_responses"] == 4
    assert flat["total_tokens"] == 500
    assert flat["avg_confidence"] == 0.75
    assert flat["average_response_time_ms"] == "N/A"


def test_format_response_details_shapes_payload():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    response = SimpleNamespace(
        id=uuid4(),
        post_title="Title",
        post_content="Body",
        author_display_name="Author",
        post_tags=["x"],
        confidence_score=0.9,
        decision_reason="reason",
        responded=True,
        response_content="answer",
        tokens_used=12,
        response_time_ms=100,
        created_at=now,
        responded_at=now,
    )
    agent = SimpleNamespace(name="Helper")
    payload = format_response_details(response, agent)
    assert payload["agent_name"] == "Helper"
    assert payload["post_title"] == "Title"
    assert payload["decision_reasoning"] == "reason"
    assert payload["created_at"] == now.isoformat()


# --- controller: list --------------------------------------------------------


async def test_list_renders_agents(db_session):
    await _seed_agent(db_session, name="Helper")
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await ForumAgentsAdminController.forum_agents_list.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.template_name == "admin/bot/forum_agents/list.html"
    assert response.context["active_page"] == "forum_agents"
    assert [a.name for a in response.context["agents"]] == ["Helper"]


async def test_list_guild_not_found_returns_404(db_session):
    client = SimpleNamespace(get_guild=AsyncMock(side_effect=GuildNotFoundError("x")))
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=client):
        response = await ForumAgentsAdminController.forum_agents_list.fn(
            None, request=object(), db_session=db_session, guild_id="missing"
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"


# --- controller: create ------------------------------------------------------


async def test_create_get_renders_blank_form(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await ForumAgentsAdminController.forum_agent_create_form.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.template_name == "admin/bot/forum_agents/create.html"
    assert response.context["form_data"] is None


async def _run_create(db_session, form):
    request = SimpleNamespace(form=AsyncMock(return_value=form))
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        return await ForumAgentsAdminController.forum_agent_create.fn(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )


async def test_create_post_persists_and_redirects(db_session):
    response = await _run_create(
        db_session,
        _form(
            [
                ("name", "Bravo"),
                ("system_prompt", "You are a helpful bot for forums."),
                ("enable_responses", "on"),
                ("is_active", "on"),
                ("monitored_forums[]", _FORUM),
            ]
        ),
    )

    assert response.status_code in (302, 303, 307)
    assert response.url == f"/admin/bot/guilds/{_GUILD}/forum-agents"
    agents = await ForumAgentOperations(db_session).list_agents(_GUILD)
    assert [a.name for a in agents] == ["Bravo"]


async def test_create_post_invalid_rerenders_400_with_errors(db_session):
    response = await _run_create(
        db_session,
        _form([("name", ""), ("system_prompt", "short")]),
    )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/forum_agents/create.html"
    assert response.context["errors"]
    assert response.context["form_data"]["name"] == ""
    agents = await ForumAgentOperations(db_session).list_agents(_GUILD)
    assert agents == []


async def test_create_post_duplicate_name_rerenders_400(db_session):
    await _seed_agent(db_session, name="Dup")
    response = await _run_create(
        db_session,
        _form(
            [
                ("name", "Dup"),
                ("system_prompt", "You are a helpful bot for forums."),
                ("enable_responses", "on"),
            ]
        ),
    )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/forum_agents/create.html"
    assert response.context["errors"]


# --- controller: edit --------------------------------------------------------


async def test_edit_get_renders_prefilled(db_session):
    agent = await _seed_agent(db_session, name="Helper")
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await ForumAgentsAdminController.forum_agent_edit_form.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            agent_id=agent.id,
        )

    assert response.template_name == "admin/bot/forum_agents/edit.html"
    assert response.context["agent"].id == agent.id


async def test_edit_get_missing_agent_returns_404(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await ForumAgentsAdminController.forum_agent_edit_form.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            agent_id=uuid4(),
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"


async def test_edit_post_updates_and_redirects(db_session):
    agent = await _seed_agent(db_session, name="Helper")
    request = SimpleNamespace(
        form=AsyncMock(
            return_value=_form(
                [
                    ("name", "Renamed"),
                    ("system_prompt", "You are the renamed forum assistant."),
                    ("response_threshold", "0.5"),
                    ("max_responses_per_hour", "8"),
                    ("enable_responses", "on"),
                    ("is_active", "on"),
                    ("monitored_forums[]", _FORUM),
                ]
            )
        )
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await ForumAgentsAdminController.forum_agent_edit.fn(
            None,
            request=request,
            db_session=db_session,
            guild_id=_GUILD,
            agent_id=agent.id,
        )

    assert response.status_code in (302, 303, 307)
    refreshed = await ForumAgentOperations(db_session).get_agent(agent.id, _GUILD)
    assert refreshed.name == "Renamed"
    assert refreshed.response_threshold == 0.5
    assert refreshed.max_responses_per_hour == 8


async def test_edit_post_invalid_rerenders_400(db_session):
    agent = await _seed_agent(db_session, name="Helper")
    request = SimpleNamespace(
        form=AsyncMock(return_value=_form([("name", ""), ("system_prompt", "x")]))
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await ForumAgentsAdminController.forum_agent_edit.fn(
            None,
            request=request,
            db_session=db_session,
            guild_id=_GUILD,
            agent_id=agent.id,
        )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/forum_agents/edit.html"
    assert response.context["errors"]


# --- controller: delete / toggle ---------------------------------------------


async def test_delete_removes_agent(db_session):
    agent = await _seed_agent(db_session, name="Doomed")
    with patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})):
        response = await ForumAgentsAdminController.forum_agent_delete.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            agent_id=agent.id,
        )

    assert response.status_code in (302, 303, 307)
    agents = await ForumAgentOperations(db_session).list_agents(_GUILD)
    assert agents == []


async def test_delete_missing_returns_404(db_session):
    with patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})):
        response = await ForumAgentsAdminController.forum_agent_delete.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            agent_id=uuid4(),
        )

    assert response.status_code == 404


async def test_toggle_flips_status(db_session):
    agent = await _seed_agent(db_session, name="Toggler", is_active=True)
    with patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})):
        response = await ForumAgentsAdminController.forum_agent_toggle.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            agent_id=agent.id,
        )

    assert response.status_code in (302, 303, 307)
    refreshed = await ForumAgentOperations(db_session).get_agent(agent.id, _GUILD)
    assert refreshed.is_active is False


async def test_toggle_missing_returns_404(db_session):
    with patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})):
        response = await ForumAgentsAdminController.forum_agent_toggle.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            agent_id=uuid4(),
        )

    assert response.status_code == 404


# --- controller: bulk --------------------------------------------------------


async def test_bulk_disable_updates_all(db_session):
    a1 = await _seed_agent(db_session, name="A1", is_active=True)
    a2 = await _seed_agent(db_session, name="A2", is_active=True)
    request = SimpleNamespace(
        form=AsyncMock(
            return_value=_form(
                [
                    ("action", "disable"),
                    ("agent_ids", str(a1.id)),
                    ("agent_ids", str(a2.id)),
                ]
            )
        )
    )
    response = await ForumAgentsAdminController.forum_agents_bulk.fn(
        None, request=request, db_session=db_session, guild_id=_GUILD
    )

    assert response.status_code in (302, 303, 307)
    ops = ForumAgentOperations(db_session)
    assert (await ops.get_agent(a1.id, _GUILD)).is_active is False
    assert (await ops.get_agent(a2.id, _GUILD)).is_active is False


async def test_bulk_missing_ids_returns_400(db_session):
    request = SimpleNamespace(
        form=AsyncMock(return_value=_form([("action", "disable")]))
    )
    with patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})):
        response = await ForumAgentsAdminController.forum_agents_bulk.fn(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/guilds/error.html"


async def test_bulk_invalid_uuid_returns_400(db_session):
    request = SimpleNamespace(
        form=AsyncMock(
            return_value=_form([("action", "disable"), ("agent_ids", "not-a-uuid")])
        )
    )
    with patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})):
        response = await ForumAgentsAdminController.forum_agents_bulk.fn(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    assert response.status_code == 400


# --- controller: analytics ---------------------------------------------------


async def test_analytics_aggregates_responses(db_session):
    agent = await _seed_agent(db_session, name="Analyzed")
    await _seed_response(db_session, agent, responded=True, confidence=0.8, tokens=100)
    await _seed_response(db_session, agent, responded=False, confidence=0.4, tokens=50)
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await ForumAgentsAdminController.forum_agent_analytics.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            agent_id=agent.id,
        )

    assert response.template_name == "admin/bot/forum_agents/analytics.html"
    analytics = response.context["analytics"]
    assert analytics["total_evaluations"] == 2
    assert analytics["total_responses"] == 1
    assert analytics["total_tokens"] == 150
    assert len(response.context["recent_responses"]) == 2


async def test_analytics_missing_agent_returns_404(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await ForumAgentsAdminController.forum_agent_analytics.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            agent_id=uuid4(),
        )

    assert response.status_code == 404


# --- controller: response details JSON ---------------------------------------


async def test_response_details_returns_json(db_session):
    agent = await _seed_agent(db_session, name="Detailed")
    resp = await _seed_response(
        db_session, agent, responded=True, confidence=0.9, tokens=42
    )
    response = await ForumAgentsAdminController.forum_response_details.fn(
        None, db_session=db_session, response_id=resp.id
    )

    assert response.status_code == 200
    assert response.content["agent_name"] == "Detailed"
    assert response.content["tokens_used"] == 42
    assert response.content["responded"] is True


async def test_response_details_missing_returns_404(db_session):
    response = await ForumAgentsAdminController.forum_response_details.fn(
        None, db_session=db_session, response_id=uuid4()
    )

    assert response.status_code == 404
    assert response.content["error"] == "Response not found"


# --- auth wiring -------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        ForumAgentsAdminController.forum_agents_list,
        ForumAgentsAdminController.forum_agent_create_form,
        ForumAgentsAdminController.forum_agent_create,
        ForumAgentsAdminController.forum_agent_edit_form,
        ForumAgentsAdminController.forum_agent_edit,
        ForumAgentsAdminController.forum_agent_delete,
        ForumAgentsAdminController.forum_agent_toggle,
        ForumAgentsAdminController.forum_agents_bulk,
        ForumAgentsAdminController.forum_agent_analytics,
        ForumAgentsAdminController.forum_response_details,
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
