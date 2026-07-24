"""Tests for the protected chat-agent error log admin pages."""

from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest
from litestar.exceptions import NotFoundException
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard

from smarter_dev.web.chat_conversations_admin import ChatConversationsAdminController
from smarter_dev.web.models import ChatAgentError

_MODULE = "smarter_dev.web.chat_conversations_admin"


def _error(**overrides) -> ChatAgentError:
    fields = {
        "id": uuid4(),
        "engagement_id": None,
        "request_id": "abcd1234",
        "guild_id": "111",
        "channel_id": "222",
        "model_name": "kimi-k2.6",
        "reasoning_level": "medium",
        "error_type": "pydantic_ai.exceptions.ModelHTTPError",
        "error_message": "status_code: 503",
        "traceback": "Traceback...",
        "provider_status_code": 503,
        "provider_body": '{"error":{"message":"overloaded"}}',
        "error_context": {"first_activation": True},
    }
    fields.update(overrides)
    return ChatAgentError(**fields)


async def _seed(db_session, *errors: ChatAgentError) -> None:
    db_session.add_all(errors)
    await db_session.commit()


async def test_error_list_renders_and_filters(db_session):
    keep = _error()
    drop = _error(guild_id="other", model_name="other-model")
    await _seed(db_session, keep, drop)

    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_flash_messages", return_value=[]):
        response = await ChatConversationsAdminController.list_errors.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id="111",
            model_name="kimi-k2.6",
            page=1,
        )

    assert response.template_name == "admin/chat-errors/list.html"
    assert response.context["total"] == 1
    assert [error.id for error in response.context["errors"]] == [keep.id]


async def test_error_detail_renders_full_record(db_session):
    error = _error()
    await _seed(db_session, error)

    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_flash_messages", return_value=[]):
        response = await ChatConversationsAdminController.error_detail.fn(
            None,
            request=object(),
            db_session=db_session,
            error_id=error.id,
        )

    assert response.template_name == "admin/chat-errors/detail.html"
    assert response.context["error"].traceback == "Traceback..."
    assert "overloaded" in response.context["error"].provider_body


async def test_error_detail_missing_raises_404(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ):
        with pytest.raises(NotFoundException):
            await ChatConversationsAdminController.error_detail.fn(
                None,
                request=object(),
                db_session=db_session,
                error_id=uuid4(),
            )


@pytest.mark.parametrize(
    "handler",
    [
        ChatConversationsAdminController.list_errors,
        ChatConversationsAdminController.error_detail,
    ],
)
def test_error_routes_require_manage_bot_permission(handler):
    guards = handler.guards

    assert auth_guard in guards
    assert any(
        isinstance(guard, Permission) and guard.permission == "manage-bot"
        for guard in guards
    )
