"""Auth-guard parity tests for the native bytes controller.

Unlike the endpoint tests these use the REAL controller guards
(``[auth_guard, APIKeyOnly(), Permission("bot-api")]``). They cover the
credential-shape failures that short-circuit before any database lookup:
missing bearer and a non-``sk_`` bearer both reject with 401.

NOTE — intentional status change vs. the FastAPI mount: the legacy
``HTTPBearer(auto_error=True)`` returned **403** for a missing ``Authorization``
header, whereas the Skrift ``auth_guard`` raises ``NotAuthorizedException`` →
**401**. The harness ``auth-missing-key-401`` check accepts ``(401, 403)`` and
``auth-malformed-key-401`` accepts ``(401,)``; see
docs/v2/legacy-sunset/04-api-rewrite.md ("401-parity"). Unknown *well-formed*
``sk_`` keys are rejected via the DB path and are covered by the harness
(``auth-unknown-skrift-key-401``), not here.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient, create_test_client

from smarter_dev.web.api_native.activity import ActivityController
from smarter_dev.web.api_native.bytes import BytesController
from smarter_dev.web.api_native.challenges import ChallengeController
from smarter_dev.web.api_native.chat_conversations import ChatConversationController
from smarter_dev.web.api_native.members import MemberController
from smarter_dev.web.api_native.forum import (
    ForumAgentController,
    ForumNotificationController,
)
from smarter_dev.web.api_native.image_quota import ImageQuotaController
from smarter_dev.web.api_native.model_overrides import ChannelModelOverrideController
from smarter_dev.web.api_native.quests import QuestController
from smarter_dev.web.api_native.squads import (
    SquadController,
    SquadSaleEventController,
)

_GUILD = "123456789012345678"
_FORUM = "222222222222222222"
_CHANNEL = "555000111222333444"


@pytest.fixture
def guarded_client() -> Iterator[TestClient]:
    """Client serving the bytes controller with its real auth guards."""
    with create_test_client(
        route_handlers=[BytesController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as client:
        yield client


def test_missing_authorization_header_rejected(guarded_client: TestClient):
    response = guarded_client.get(f"/api/guilds/{_GUILD}/bytes/config")
    assert response.status_code == 401


def test_non_sk_bearer_rejected(guarded_client: TestClient):
    response = guarded_client.get(
        f"/api/guilds/{_GUILD}/bytes/config",
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401


def test_session_cookie_does_not_authenticate_api(guarded_client: TestClient):
    # APIKeyOnly means a session identity must never satisfy the guard.
    response = guarded_client.get(
        f"/api/guilds/{_GUILD}/bytes/config",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 401


@pytest.fixture
def guarded_squad_client() -> Iterator[TestClient]:
    """Client serving the squad + sale-event controllers with real auth guards."""
    with create_test_client(
        route_handlers=[SquadController, SquadSaleEventController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as client:
        yield client


def test_squad_missing_authorization_header_rejected(guarded_squad_client: TestClient):
    response = guarded_squad_client.get(f"/api/guilds/{_GUILD}/squads/")
    assert response.status_code == 401


def test_squad_non_sk_bearer_rejected(guarded_squad_client: TestClient):
    response = guarded_squad_client.get(
        f"/api/guilds/{_GUILD}/squads/",
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401


def test_sale_events_missing_authorization_header_rejected(guarded_squad_client: TestClient):
    response = guarded_squad_client.get(f"/api/guilds/{_GUILD}/squad-sale-events/")
    assert response.status_code == 401


@pytest.fixture
def guarded_quest_client() -> Iterator[TestClient]:
    """Client serving the quest controller with its real auth guards."""
    with create_test_client(
        route_handlers=[QuestController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as client:
        yield client


def test_quest_missing_authorization_header_rejected(guarded_quest_client: TestClient):
    response = guarded_quest_client.get(f"/api/quests/daily/current?guild_id={_GUILD}")
    assert response.status_code == 401


def test_quest_non_sk_bearer_rejected(guarded_quest_client: TestClient):
    response = guarded_quest_client.get(
        f"/api/quests/daily/current?guild_id={_GUILD}",
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401


@pytest.fixture
def guarded_challenge_client() -> Iterator[TestClient]:
    """Client serving the challenge controller with its real auth guards."""
    with create_test_client(
        route_handlers=[ChallengeController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as client:
        yield client


def test_challenge_missing_authorization_header_rejected(
    guarded_challenge_client: TestClient,
):
    response = guarded_challenge_client.get("/api/challenges/pending-announcements")
    assert response.status_code == 401


def test_challenge_non_sk_bearer_rejected(guarded_challenge_client: TestClient):
    response = guarded_challenge_client.get(
        "/api/challenges/pending-announcements",
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401


@pytest.fixture
def guarded_forum_client() -> Iterator[TestClient]:
    """Client serving the forum controllers with their real auth guards."""
    with create_test_client(
        route_handlers=[ForumAgentController, ForumNotificationController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as client:
        yield client


def test_forum_agents_missing_authorization_header_rejected(
    guarded_forum_client: TestClient,
):
    response = guarded_forum_client.get(f"/api/guilds/{_GUILD}/forum-agents")
    assert response.status_code == 401


def test_forum_agents_non_sk_bearer_rejected(guarded_forum_client: TestClient):
    response = guarded_forum_client.get(
        f"/api/guilds/{_GUILD}/forum-agents",
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401


def test_forum_notification_topics_missing_authorization_header_rejected(
    guarded_forum_client: TestClient,
):
    response = guarded_forum_client.get(
        f"/api/guilds/{_GUILD}/forum-channels/{_FORUM}/notification-topics"
    )
    assert response.status_code == 401


@pytest.fixture
def guarded_model_override_client() -> Iterator[TestClient]:
    """Client serving the model-override controller with its real auth guards."""
    with create_test_client(
        route_handlers=[ChannelModelOverrideController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as client:
        yield client


def _override_url() -> str:
    return f"/api/guilds/{_GUILD}/channels/{_CHANNEL}/model-override"


def test_model_override_get_missing_authorization_header_rejected(
    guarded_model_override_client: TestClient,
):
    response = guarded_model_override_client.get(_override_url())
    assert response.status_code == 401


def test_model_override_put_missing_authorization_header_rejected(
    guarded_model_override_client: TestClient,
):
    response = guarded_model_override_client.put(
        _override_url(), json={"model_key": "kimi-k2-6"}
    )
    assert response.status_code == 401


def test_model_override_delete_missing_authorization_header_rejected(
    guarded_model_override_client: TestClient,
):
    response = guarded_model_override_client.delete(_override_url())
    assert response.status_code == 401


def test_model_override_non_sk_bearer_rejected(
    guarded_model_override_client: TestClient,
):
    response = guarded_model_override_client.get(
        _override_url(), headers={"Authorization": "Bearer not-a-skrift-key"}
    )
    assert response.status_code == 401


@pytest.fixture
def guarded_image_quota_client() -> Iterator[TestClient]:
    """Client serving the image-quota controller with its real auth guards."""
    with create_test_client(
        route_handlers=[ImageQuotaController],
        plugins=[PydanticPlugin()],
    ) as client:
        yield client


def test_image_quota_missing_authorization_header_rejected(
    guarded_image_quota_client: TestClient,
):
    response = guarded_image_quota_client.get(
        f"/api/image-generations/quota?guild_id={_GUILD}"
    )
    assert response.status_code == 401


def test_image_quota_reserve_missing_authorization_header_rejected(
    guarded_image_quota_client: TestClient,
):
    response = guarded_image_quota_client.post(
        "/api/image-generations/reserve", json={"guild_id": _GUILD}
    )
    assert response.status_code == 401


def test_image_quota_non_sk_bearer_rejected(
    guarded_image_quota_client: TestClient,
):
    response = guarded_image_quota_client.get(
        f"/api/image-generations/quota?guild_id={_GUILD}",
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401


@pytest.fixture
def guarded_chat_client() -> Iterator[TestClient]:
    """Client serving the chat-conversation controller with its real guards.

    Covers both guard tiers: the leaderboard read carries ``Permission(
    "bot-api")`` and the engagement write carries ``Permission("bot-api-admin")``
    — a credential-shape failure short-circuits both before any DB lookup.
    """
    with create_test_client(
        route_handlers=[ChatConversationController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as client:
        yield client


def test_chat_leaderboard_missing_authorization_header_rejected(
    guarded_chat_client: TestClient,
):
    response = guarded_chat_client.get(
        f"/api/chat-conversations/usage-leaderboard?guild_id={_GUILD}"
    )
    assert response.status_code == 401


def test_chat_leaderboard_non_sk_bearer_rejected(guarded_chat_client: TestClient):
    response = guarded_chat_client.get(
        f"/api/chat-conversations/usage-leaderboard?guild_id={_GUILD}",
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401


def test_chat_engagement_write_missing_authorization_header_rejected(
    guarded_chat_client: TestClient,
):
    response = guarded_chat_client.post(
        "/api/chat-conversations/engagements", json={}
    )
    assert response.status_code == 401


@pytest.fixture
def guarded_activity_client() -> Iterator[TestClient]:
    """Client serving the activity controller with its real auth guards."""
    with create_test_client(
        route_handlers=[ActivityController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as client:
        yield client


def test_activity_batch_missing_authorization_header_rejected(
    guarded_activity_client: TestClient,
):
    response = guarded_activity_client.post("/api/activity/batch", json={"events": []})
    assert response.status_code == 401


def test_activity_batch_non_sk_bearer_rejected(guarded_activity_client: TestClient):
    response = guarded_activity_client.post(
        "/api/activity/batch",
        json={"events": []},
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401


@pytest.fixture
def guarded_member_client() -> Iterator[TestClient]:
    """Client serving the member controller with its real auth guards."""
    with create_test_client(
        route_handlers=[MemberController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as client:
        yield client


def test_member_delete_missing_authorization_header_rejected(
    guarded_member_client: TestClient,
):
    response = guarded_member_client.delete(f"/api/guilds/{_GUILD}/members/{_GUILD}")
    assert response.status_code == 401


def test_member_delete_non_sk_bearer_rejected(guarded_member_client: TestClient):
    response = guarded_member_client.delete(
        f"/api/guilds/{_GUILD}/members/{_GUILD}",
        headers={"Authorization": "Bearer not-a-skrift-key"},
    )
    assert response.status_code == 401
