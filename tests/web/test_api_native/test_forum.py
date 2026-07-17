"""Parity tests for the native forum bot API controllers (unit U7).

Exercise the ported ``ForumAgentController`` and ``ForumNotificationController``
in isolation with a stubbed session and mocked ``ForumAgentOperations``. Assert
the same status codes and response bodies the legacy FastAPI
``routers/forum_agents_simple.py`` and ``routers/forum_notifications.py``
produced — the wire contract the bot's ``forum_agent_service`` and
``forum_notifications`` plugin depend on.

Auth-guard rejection is covered by ``test_auth.py``; these tests bypass guards
(see ``forum_client`` / ``forum_notification_client`` in ``conftest.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from litestar.testing import TestClient

_AGENT_ID = "11111111-1111-1111-1111-111111111111"


def _execute_result(
    *,
    scalars_all: list | None = None,
    scalar_one_or_none: object = None,
    scalar: object = None,
) -> Mock:
    """Build a mock SQLAlchemy ``Result`` for the raw-``execute`` code paths."""
    result = Mock()
    scalars = Mock()
    scalars.all = Mock(return_value=scalars_all or [])
    result.scalars = Mock(return_value=scalars)
    result.scalar_one_or_none = Mock(return_value=scalar_one_or_none)
    result.scalar = Mock(return_value=scalar)
    return result


def _agent(guild_id: str) -> SimpleNamespace:
    """A representative forum agent row (attribute access, like the ORM model)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        guild_id=guild_id,
        name="Helper Agent",
        description="Answers questions",
        system_prompt="Be helpful",
        monitored_forums=["222222222222222222"],
        is_active=True,
        enable_responses=True,
        enable_user_tagging=False,
        response_threshold=0.7,
        max_responses_per_hour=5,
        created_by="admin",
        created_at=now,
        updated_at=now,
    )


# --------------------------------------------------------------------------- #
# GET /api/guilds/{guild_id}/forum-agents
# --------------------------------------------------------------------------- #


def test_list_forum_agents_returns_serialized_dicts(
    forum_client: TestClient, forum_agent_ops_mock: Mock, guild_id: str
):
    agent = _agent(guild_id)
    forum_agent_ops_mock.list_agents.return_value = [agent]

    response = forum_client.get(f"/api/guilds/{guild_id}/forum-agents")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert body[0]["id"] == str(agent.id)
    assert body[0]["guild_id"] == guild_id
    assert body[0]["name"] == "Helper Agent"
    assert body[0]["response_threshold"] == 0.7
    assert body[0]["created_at"] == agent.created_at.isoformat()
    forum_agent_ops_mock.list_agents.assert_awaited_once_with(guild_id)


def test_list_forum_agents_empty(
    forum_client: TestClient, forum_agent_ops_mock: Mock, guild_id: str
):
    forum_agent_ops_mock.list_agents.return_value = []
    response = forum_client.get(f"/api/guilds/{guild_id}/forum-agents")
    assert response.status_code == 200
    assert response.json() == []


def test_list_forum_agents_invalid_guild_id_400(
    forum_client: TestClient, forum_agent_ops_mock: Mock
):
    response = forum_client.get("/api/guilds/not-a-snowflake/forum-agents")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["type"] == "validation_error"
    assert detail["detail"] == "Invalid guild_id format"
    assert detail["request_id"] is None


def test_list_forum_agents_db_failure_500(
    forum_client: TestClient, forum_agent_ops_mock: Mock, guild_id: str
):
    forum_agent_ops_mock.list_agents.side_effect = RuntimeError("boom")
    response = forum_client.get(f"/api/guilds/{guild_id}/forum-agents")
    assert response.status_code == 500
    assert response.json()["detail"].startswith("Failed to retrieve forum agents:")


# --------------------------------------------------------------------------- #
# POST /api/guilds/{guild_id}/forum-agents/{agent_id}/responses
# --------------------------------------------------------------------------- #


def test_record_agent_response_success(
    forum_client: TestClient,
    forum_agent_ops_mock: Mock,
    session_mock: AsyncMock,
    guild_id: str,
):
    forum_agent_ops_mock.get_agent.return_value = _agent(guild_id)

    response = forum_client.post(
        f"/api/guilds/{guild_id}/forum-agents/{_AGENT_ID}/responses",
        json={
            "channel_id": "222222222222222222",
            "thread_id": "333333333333333333",
            "post_title": "Title",
            "post_content": "Content",
            "author_display_name": "Alice",
            "responded": True,
            "response_content": "answer",
            "tokens_used": 10,
            "response_time_ms": 100,
            "confidence_score": 0.5,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "id" in body
    assert "created_at" in body
    session_mock.add.assert_called_once()
    session_mock.commit.assert_awaited_once()


def test_record_agent_response_agent_not_found_404(
    forum_client: TestClient, forum_agent_ops_mock: Mock, guild_id: str
):
    forum_agent_ops_mock.get_agent.return_value = None

    response = forum_client.post(
        f"/api/guilds/{guild_id}/forum-agents/{_AGENT_ID}/responses",
        json={"post_title": "x"},
    )

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["type"] == "not_found_error"
    assert detail["detail"] == "Forum agent not found"
    assert detail["request_id"] is None


def test_record_agent_response_malformed_agent_id_422(
    forum_client: TestClient, forum_agent_ops_mock: Mock, guild_id: str
):
    response = forum_client.post(
        f"/api/guilds/{guild_id}/forum-agents/not-a-uuid/responses",
        json={"post_title": "x"},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# GET /api/guilds/{guild_id}/forum-agents/{agent_id}/responses/count
# --------------------------------------------------------------------------- #


def test_agent_response_count_success(
    forum_client: TestClient,
    forum_agent_ops_mock: Mock,
    session_mock: AsyncMock,
    guild_id: str,
):
    forum_agent_ops_mock.get_agent.return_value = _agent(guild_id)
    session_mock.execute = AsyncMock(return_value=_execute_result(scalar=3))

    response = forum_client.get(
        f"/api/guilds/{guild_id}/forum-agents/{_AGENT_ID}/responses/count?hours=2"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert body["hours"] == 2
    assert "cutoff_time" in body


def test_agent_response_count_defaults_hours_to_one(
    forum_client: TestClient,
    forum_agent_ops_mock: Mock,
    session_mock: AsyncMock,
    guild_id: str,
):
    forum_agent_ops_mock.get_agent.return_value = _agent(guild_id)
    session_mock.execute = AsyncMock(return_value=_execute_result(scalar=None))

    response = forum_client.get(
        f"/api/guilds/{guild_id}/forum-agents/{_AGENT_ID}/responses/count"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["hours"] == 1


def test_agent_response_count_agent_not_found_404(
    forum_client: TestClient, forum_agent_ops_mock: Mock, guild_id: str
):
    forum_agent_ops_mock.get_agent.return_value = None
    response = forum_client.get(
        f"/api/guilds/{guild_id}/forum-agents/{_AGENT_ID}/responses/count"
    )
    assert response.status_code == 404
    assert response.json()["detail"]["detail"] == "Forum agent not found"


# --------------------------------------------------------------------------- #
# Forum notification topics + subscriptions
# --------------------------------------------------------------------------- #


def _topic(guild_id: str, forum_channel_id: str) -> SimpleNamespace:
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        guild_id=guild_id,
        forum_channel_id=forum_channel_id,
        topic_name="general-help",
        topic_description="General help topics",
        created_at=now,
        updated_at=now,
    )


def _subscription(
    guild_id: str, user_id: str, forum_channel_id: str, *, is_expired: bool = False
) -> SimpleNamespace:
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        guild_id=guild_id,
        user_id=user_id,
        username="Alice",
        forum_channel_id=forum_channel_id,
        subscribed_topics=["general-help"],
        notification_hours=12,
        created_at=now,
        updated_at=now,
        is_expired=is_expired,
    )


def test_get_notification_topics(
    forum_notification_client: TestClient,
    session_mock: AsyncMock,
    guild_id: str,
    forum_channel_id: str,
):
    topic = _topic(guild_id, forum_channel_id)
    session_mock.execute = AsyncMock(
        return_value=_execute_result(scalars_all=[topic])
    )

    response = forum_notification_client.get(
        f"/api/guilds/{guild_id}/forum-channels/{forum_channel_id}/notification-topics"
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["topic_name"] == "general-help"
    assert body[0]["forum_channel_id"] == forum_channel_id


def test_get_notification_topics_db_failure_500(
    forum_notification_client: TestClient,
    session_mock: AsyncMock,
    guild_id: str,
    forum_channel_id: str,
):
    session_mock.execute = AsyncMock(side_effect=RuntimeError("boom"))
    response = forum_notification_client.get(
        f"/api/guilds/{guild_id}/forum-channels/{forum_channel_id}/notification-topics"
    )
    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to fetch notification topics"


def test_get_user_subscriptions_filters_expired(
    forum_notification_client: TestClient,
    session_mock: AsyncMock,
    guild_id: str,
    user_id: str,
    forum_channel_id: str,
):
    active = _subscription(guild_id, user_id, forum_channel_id, is_expired=False)
    expired = _subscription(guild_id, "999", forum_channel_id, is_expired=True)
    session_mock.execute = AsyncMock(
        return_value=_execute_result(scalars_all=[active, expired])
    )

    response = forum_notification_client.get(
        f"/api/guilds/{guild_id}/forum-channels/{forum_channel_id}/user-subscriptions"
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["user_id"] == user_id


def test_get_user_forum_subscription_found(
    forum_notification_client: TestClient,
    session_mock: AsyncMock,
    guild_id: str,
    user_id: str,
    forum_channel_id: str,
):
    subscription = _subscription(guild_id, user_id, forum_channel_id)
    session_mock.execute = AsyncMock(
        return_value=_execute_result(scalar_one_or_none=subscription)
    )

    response = forum_notification_client.get(
        f"/api/guilds/{guild_id}/users/{user_id}/forum-subscriptions/{forum_channel_id}"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == user_id
    assert body["notification_hours"] == 12


def test_get_user_forum_subscription_missing_404(
    forum_notification_client: TestClient,
    session_mock: AsyncMock,
    guild_id: str,
    user_id: str,
    forum_channel_id: str,
):
    session_mock.execute = AsyncMock(
        return_value=_execute_result(scalar_one_or_none=None)
    )
    response = forum_notification_client.get(
        f"/api/guilds/{guild_id}/users/{user_id}/forum-subscriptions/{forum_channel_id}"
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "User subscription not found"


def test_get_user_forum_subscription_expired_404(
    forum_notification_client: TestClient,
    session_mock: AsyncMock,
    guild_id: str,
    user_id: str,
    forum_channel_id: str,
):
    subscription = _subscription(guild_id, user_id, forum_channel_id, is_expired=True)
    session_mock.execute = AsyncMock(
        return_value=_execute_result(scalar_one_or_none=subscription)
    )
    response = forum_notification_client.get(
        f"/api/guilds/{guild_id}/users/{user_id}/forum-subscriptions/{forum_channel_id}"
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "User subscription has expired"


def test_put_user_forum_subscription_creates(
    forum_notification_client: TestClient,
    session_mock: AsyncMock,
    guild_id: str,
    user_id: str,
    forum_channel_id: str,
):
    session_mock.execute = AsyncMock(
        return_value=_execute_result(scalar_one_or_none=None)
    )

    response = forum_notification_client.put(
        f"/api/guilds/{guild_id}/users/{user_id}/forum-subscriptions/{forum_channel_id}",
        json={
            "user_id": user_id,
            "username": "Alice",
            "forum_channel_id": forum_channel_id,
            "subscribed_topics": ["general-help"],
            "notification_hours": 12,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == user_id
    assert body["username"] == "Alice"
    assert body["notification_hours"] == 12
    session_mock.add.assert_called_once()
    session_mock.commit.assert_awaited_once()


def test_put_user_forum_subscription_updates_existing(
    forum_notification_client: TestClient,
    session_mock: AsyncMock,
    guild_id: str,
    user_id: str,
    forum_channel_id: str,
):
    existing = _subscription(guild_id, user_id, forum_channel_id)
    session_mock.execute = AsyncMock(
        return_value=_execute_result(scalar_one_or_none=existing)
    )

    response = forum_notification_client.put(
        f"/api/guilds/{guild_id}/users/{user_id}/forum-subscriptions/{forum_channel_id}",
        json={
            "user_id": user_id,
            "username": "Alice Updated",
            "forum_channel_id": forum_channel_id,
            "subscribed_topics": ["general-help", "bugs"],
            "notification_hours": 48,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "Alice Updated"
    assert body["notification_hours"] == 48
    assert body["subscribed_topics"] == ["general-help", "bugs"]
    # Update path does not add a new row.
    session_mock.add.assert_not_called()
    session_mock.commit.assert_awaited_once()


def test_put_user_forum_subscription_db_failure_500(
    forum_notification_client: TestClient,
    session_mock: AsyncMock,
    guild_id: str,
    user_id: str,
    forum_channel_id: str,
):
    session_mock.execute = AsyncMock(side_effect=RuntimeError("boom"))

    response = forum_notification_client.put(
        f"/api/guilds/{guild_id}/users/{user_id}/forum-subscriptions/{forum_channel_id}",
        json={
            "user_id": user_id,
            "username": "Alice",
            "forum_channel_id": forum_channel_id,
            "subscribed_topics": ["general-help"],
            "notification_hours": 12,
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to create or update user subscription"
    session_mock.rollback.assert_awaited_once()
