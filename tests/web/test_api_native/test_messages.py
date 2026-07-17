"""Parity tests for the native (Litestar) scheduled/repeating message API (U6).

The legacy ``routers/scheduled_messages.py`` and ``routers/repeating_messages.py``
had no dedicated FastAPI test files, so these assert the wire contract directly:
exact status codes and JSON bodies for happy paths, the plain ``{"detail": ...}``
error shapes, 404 branches, the 500 per-endpoint details, and the 422 on a
malformed message UUID. Paths carry the final ``/api`` prefix because the native
controller declares its mounted path (the FastAPI app was itself mounted at
``/api``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

from litestar.testing import TestClient

from smarter_dev.web.crud import DatabaseOperationError

_GUILD = "123456789012345678"
_CHANNEL = "222222222222222222"
_ROLE = "333333333333333333"


def _campaign_mock() -> Mock:
    campaign = Mock()
    campaign.id = uuid4()
    campaign.guild_id = _GUILD
    campaign.title = "Launch Week"
    campaign.is_active = True
    campaign.announcement_channels = [_CHANNEL]
    return campaign


def _scheduled_message_mock(**overrides) -> Mock:
    message = Mock()
    message.id = overrides.get("id", uuid4())
    message.title = overrides.get("title", "Day 1 Reveal")
    message.description = overrides.get("description", "The first announcement")
    message.announcement_channel_message = overrides.get(
        "announcement_channel_message", "Go check the channel!"
    )
    message.scheduled_time = overrides.get(
        "scheduled_time", datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    )
    message.is_sent = overrides.get("is_sent", False)
    message.sent_at = overrides.get("sent_at", None)
    message.created_at = overrides.get(
        "created_at", datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    )
    message.campaign = overrides.get("campaign", _campaign_mock())
    return message


def _repeating_message_mock(**overrides) -> Mock:
    message = Mock()
    message.id = overrides.get("id", uuid4())
    message.guild_id = overrides.get("guild_id", _GUILD)
    message.channel_id = overrides.get("channel_id", _CHANNEL)
    message.message_content = overrides.get("message_content", "Remember to stretch!")
    message.role_id = overrides.get("role_id", _ROLE)
    message.start_time = overrides.get(
        "start_time", datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)
    )
    message.interval_minutes = overrides.get("interval_minutes", 60)
    message.next_send_time = overrides.get(
        "next_send_time", datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
    )
    message.is_active = overrides.get("is_active", True)
    message.total_sent = overrides.get("total_sent", 4)
    message.last_sent_at = overrides.get(
        "last_sent_at", datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)
    )
    message.created_by = overrides.get("created_by", "adminuser")
    message.created_at = overrides.get(
        "created_at", datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    )
    message.updated_at = overrides.get(
        "updated_at", datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    )
    message.get_formatted_message = Mock(
        return_value=overrides.get("formatted", "Remember to stretch! <@&333333333333333333>")
    )
    return message


# --------------------------------------------------------------------------- #
# Scheduled messages
# --------------------------------------------------------------------------- #


class TestUpcomingScheduledMessages:
    def test_upcoming_success(self, scheduled_client: TestClient, scheduled_ops_mock):
        message = _scheduled_message_mock()
        scheduled_ops_mock.get_upcoming_scheduled_messages.return_value = [message]

        response = scheduled_client.get("/api/scheduled-messages/upcoming?seconds=45")

        assert response.status_code == 200
        items = response.json()["scheduled_messages"]
        assert len(items) == 1
        item = items[0]
        assert item["id"] == str(message.id)
        assert item["title"] == "Day 1 Reveal"
        assert item["scheduled_time"] == message.scheduled_time.isoformat()
        assert item["guild_id"] == _GUILD
        assert item["announcement_channels"] == [_CHANNEL]
        assert item["campaign"]["title"] == "Launch Week"
        assert item["campaign"]["is_active"] is True

    def test_upcoming_default_window_empty(
        self, scheduled_client: TestClient, scheduled_ops_mock
    ):
        scheduled_ops_mock.get_upcoming_scheduled_messages.return_value = []

        response = scheduled_client.get("/api/scheduled-messages/upcoming")

        assert response.status_code == 200
        assert response.json() == {"scheduled_messages": []}

    def test_upcoming_db_error_500(
        self, scheduled_client: TestClient, scheduled_ops_mock
    ):
        scheduled_ops_mock.get_upcoming_scheduled_messages.side_effect = (
            DatabaseOperationError("boom")
        )

        response = scheduled_client.get("/api/scheduled-messages/upcoming")

        assert response.status_code == 500
        assert response.json() == {
            "detail": "Failed to retrieve upcoming scheduled messages"
        }

    def test_upcoming_unexpected_error_500(
        self, scheduled_client: TestClient, scheduled_ops_mock
    ):
        scheduled_ops_mock.get_upcoming_scheduled_messages.side_effect = RuntimeError(
            "kaboom"
        )

        response = scheduled_client.get("/api/scheduled-messages/upcoming")

        assert response.status_code == 500
        assert response.json() == {"detail": "Internal server error"}


class TestPendingScheduledMessages:
    def test_pending_success(self, scheduled_client: TestClient, scheduled_ops_mock):
        message = _scheduled_message_mock()
        scheduled_ops_mock.get_pending_scheduled_messages.return_value = [message]

        response = scheduled_client.get("/api/scheduled-messages/pending")

        assert response.status_code == 200
        items = response.json()["scheduled_messages"]
        assert len(items) == 1
        assert items[0]["id"] == str(message.id)

    def test_pending_db_error_500(
        self, scheduled_client: TestClient, scheduled_ops_mock
    ):
        scheduled_ops_mock.get_pending_scheduled_messages.side_effect = (
            DatabaseOperationError("boom")
        )

        response = scheduled_client.get("/api/scheduled-messages/pending")

        assert response.status_code == 500
        assert response.json() == {
            "detail": "Failed to retrieve pending scheduled messages"
        }


class TestMarkScheduledMessageSent:
    def test_mark_sent_success(self, scheduled_client: TestClient, scheduled_ops_mock):
        scheduled_ops_mock.mark_scheduled_message_sent.return_value = True

        response = scheduled_client.post(
            f"/api/scheduled-messages/{uuid4()}/mark-sent"
        )

        assert response.status_code == 200
        assert response.json() == {"success": True}

    def test_mark_sent_not_found_404(
        self, scheduled_client: TestClient, scheduled_ops_mock
    ):
        scheduled_ops_mock.mark_scheduled_message_sent.return_value = False

        response = scheduled_client.post(
            f"/api/scheduled-messages/{uuid4()}/mark-sent"
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "Scheduled message not found"}

    def test_mark_sent_malformed_uuid_422(self, scheduled_client: TestClient):
        response = scheduled_client.post(
            "/api/scheduled-messages/not-a-uuid/mark-sent"
        )

        assert response.status_code == 422

    def test_mark_sent_db_error_500(
        self, scheduled_client: TestClient, scheduled_ops_mock
    ):
        scheduled_ops_mock.mark_scheduled_message_sent.side_effect = (
            DatabaseOperationError("boom")
        )

        response = scheduled_client.post(
            f"/api/scheduled-messages/{uuid4()}/mark-sent"
        )

        assert response.status_code == 500
        assert response.json() == {
            "detail": "Failed to mark scheduled message as sent"
        }


class TestGetScheduledMessage:
    def test_get_success(self, scheduled_client: TestClient, scheduled_ops_mock):
        message = _scheduled_message_mock(is_sent=True)
        message.sent_at = datetime(2026, 7, 17, 12, 5, tzinfo=timezone.utc)
        scheduled_ops_mock.get_scheduled_message_with_campaign.return_value = message

        response = scheduled_client.get(f"/api/scheduled-messages/{message.id}")

        assert response.status_code == 200
        body = response.json()["scheduled_message"]
        assert body["id"] == str(message.id)
        assert body["is_sent"] is True
        assert body["sent_at"] == message.sent_at.isoformat()
        assert body["campaign"]["title"] == "Launch Week"

    def test_get_sent_at_null_when_unsent(
        self, scheduled_client: TestClient, scheduled_ops_mock
    ):
        message = _scheduled_message_mock(is_sent=False, sent_at=None)
        scheduled_ops_mock.get_scheduled_message_with_campaign.return_value = message

        response = scheduled_client.get(f"/api/scheduled-messages/{message.id}")

        assert response.status_code == 200
        assert response.json()["scheduled_message"]["sent_at"] is None

    def test_get_not_found_404(
        self, scheduled_client: TestClient, scheduled_ops_mock
    ):
        scheduled_ops_mock.get_scheduled_message_with_campaign.return_value = None

        response = scheduled_client.get(f"/api/scheduled-messages/{uuid4()}")

        assert response.status_code == 404
        assert response.json() == {"detail": "Scheduled message not found"}

    def test_get_malformed_uuid_422(self, scheduled_client: TestClient):
        response = scheduled_client.get("/api/scheduled-messages/not-a-uuid")

        assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Repeating messages
# --------------------------------------------------------------------------- #


class TestDueRepeatingMessages:
    def test_due_success(self, repeating_client: TestClient, repeating_ops_mock):
        message = _repeating_message_mock()
        repeating_ops_mock.get_due_repeating_messages.return_value = [message]

        response = repeating_client.get("/api/repeating-messages/due")

        assert response.status_code == 200
        items = response.json()["repeating_messages"]
        assert len(items) == 1
        item = items[0]
        assert item["id"] == str(message.id)
        assert item["message_content"] == "Remember to stretch! <@&333333333333333333>"
        assert item["role_id"] == _ROLE
        assert item["interval_minutes"] == 60
        assert item["next_send_time"] == message.next_send_time.isoformat()
        assert item["total_sent"] == 4

    def test_due_empty(self, repeating_client: TestClient, repeating_ops_mock):
        repeating_ops_mock.get_due_repeating_messages.return_value = []

        response = repeating_client.get("/api/repeating-messages/due")

        assert response.status_code == 200
        assert response.json() == {"repeating_messages": []}

    def test_due_db_error_500(self, repeating_client: TestClient, repeating_ops_mock):
        repeating_ops_mock.get_due_repeating_messages.side_effect = (
            DatabaseOperationError("boom")
        )

        response = repeating_client.get("/api/repeating-messages/due")

        assert response.status_code == 500
        assert response.json() == {
            "detail": "Failed to retrieve due repeating messages"
        }


class TestMarkRepeatingMessageSent:
    def test_mark_sent_success(self, repeating_client: TestClient, repeating_ops_mock):
        repeating_ops_mock.mark_message_sent.return_value = True

        response = repeating_client.post(
            f"/api/repeating-messages/{uuid4()}/mark-sent"
        )

        assert response.status_code == 200
        assert response.json() == {"success": True}

    def test_mark_sent_not_found_404(
        self, repeating_client: TestClient, repeating_ops_mock
    ):
        repeating_ops_mock.mark_message_sent.return_value = False

        response = repeating_client.post(
            f"/api/repeating-messages/{uuid4()}/mark-sent"
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "Repeating message not found"}


class TestCreateRepeatingMessage:
    def _body(self) -> dict:
        return {
            "guild_id": _GUILD,
            "channel_id": _CHANNEL,
            "message_content": "Remember to stretch!",
            "role_id": _ROLE,
            "start_time": "2026-07-17T08:00:00+00:00",
            "interval_minutes": 60,
            "created_by": "adminuser",
        }

    def test_create_success_returns_200(
        self, repeating_client: TestClient, repeating_ops_mock
    ):
        message = _repeating_message_mock()
        repeating_ops_mock.create_repeating_message.return_value = message

        response = repeating_client.post(
            "/api/repeating-messages/", json=self._body()
        )

        # FastAPI POST defaulted to 200 (not 201) — parity requires 200.
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(message.id)
        assert body["guild_id"] == _GUILD
        assert body["interval_minutes"] == 60
        assert body["is_active"] is True
        assert body["total_sent"] == 4
        assert body["created_by"] == "adminuser"

    def test_create_validation_error_422(self, repeating_client: TestClient):
        bad_body = self._body()
        bad_body["interval_minutes"] = 0  # violates ge=1

        response = repeating_client.post("/api/repeating-messages/", json=bad_body)

        assert response.status_code == 422

    def test_create_db_error_500(
        self, repeating_client: TestClient, repeating_ops_mock
    ):
        repeating_ops_mock.create_repeating_message.side_effect = (
            DatabaseOperationError("boom")
        )

        response = repeating_client.post(
            "/api/repeating-messages/", json=self._body()
        )

        assert response.status_code == 500
        assert response.json() == {"detail": "Failed to create repeating message"}


class TestGuildRepeatingMessages:
    def test_guild_success(self, repeating_client: TestClient, repeating_ops_mock):
        message = _repeating_message_mock()
        repeating_ops_mock.get_guild_repeating_messages.return_value = [message]

        response = repeating_client.get(f"/api/repeating-messages/guild/{_GUILD}")

        assert response.status_code == 200
        items = response.json()["repeating_messages"]
        assert len(items) == 1
        assert items[0]["id"] == str(message.id)
        assert items[0]["message_content"] == "Remember to stretch!"

    def test_guild_active_only_passed_through(
        self, repeating_client: TestClient, repeating_ops_mock
    ):
        repeating_ops_mock.get_guild_repeating_messages.return_value = []

        response = repeating_client.get(
            f"/api/repeating-messages/guild/{_GUILD}?active_only=true"
        )

        assert response.status_code == 200
        assert response.json() == {"repeating_messages": []}
        _, kwargs = repeating_ops_mock.get_guild_repeating_messages.call_args
        assert kwargs["active_only"] is True


class TestGetRepeatingMessage:
    def test_get_success(self, repeating_client: TestClient, repeating_ops_mock):
        message = _repeating_message_mock()
        repeating_ops_mock.get_repeating_message.return_value = message

        response = repeating_client.get(f"/api/repeating-messages/{message.id}")

        assert response.status_code == 200
        assert response.json()["id"] == str(message.id)

    def test_get_not_found_404(
        self, repeating_client: TestClient, repeating_ops_mock
    ):
        repeating_ops_mock.get_repeating_message.return_value = None

        response = repeating_client.get(f"/api/repeating-messages/{uuid4()}")

        assert response.status_code == 404
        assert response.json() == {"detail": "Repeating message not found"}

    def test_get_malformed_uuid_422(self, repeating_client: TestClient):
        # A single-segment non-uuid does not collide with the '/due' or
        # '/guild/{id}' literal routes and reaches the '/{message_id}' handler.
        response = repeating_client.get("/api/repeating-messages/not-a-uuid")

        assert response.status_code == 422


class TestUpdateRepeatingMessage:
    def test_update_success(self, repeating_client: TestClient, repeating_ops_mock):
        repeating_ops_mock.update_repeating_message.return_value = True

        response = repeating_client.put(
            f"/api/repeating-messages/{uuid4()}",
            json={"message_content": "New text", "is_active": False},
        )

        assert response.status_code == 200
        assert response.json() == {"success": True}
        args, kwargs = repeating_ops_mock.update_repeating_message.call_args
        assert kwargs == {"message_content": "New text", "is_active": False}

    def test_update_no_fields_400(
        self, repeating_client: TestClient, repeating_ops_mock
    ):
        response = repeating_client.put(
            f"/api/repeating-messages/{uuid4()}", json={}
        )

        assert response.status_code == 400
        assert response.json() == {"detail": "No fields to update"}

    def test_update_not_found_404(
        self, repeating_client: TestClient, repeating_ops_mock
    ):
        repeating_ops_mock.update_repeating_message.return_value = False

        response = repeating_client.put(
            f"/api/repeating-messages/{uuid4()}",
            json={"message_content": "New text"},
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "Repeating message not found"}


class TestDeleteRepeatingMessage:
    def test_delete_success(self, repeating_client: TestClient, repeating_ops_mock):
        repeating_ops_mock.delete_repeating_message.return_value = True

        response = repeating_client.delete(f"/api/repeating-messages/{uuid4()}")

        assert response.status_code == 200
        assert response.json() == {"success": True}

    def test_delete_not_found_404(
        self, repeating_client: TestClient, repeating_ops_mock
    ):
        repeating_ops_mock.delete_repeating_message.return_value = False

        response = repeating_client.delete(f"/api/repeating-messages/{uuid4()}")

        assert response.status_code == 404
        assert response.json() == {"detail": "Repeating message not found"}


class TestToggleRepeatingMessage:
    def test_toggle_success(self, repeating_client: TestClient, repeating_ops_mock):
        repeating_ops_mock.toggle_repeating_message.return_value = True

        response = repeating_client.post(
            f"/api/repeating-messages/{uuid4()}/toggle?is_active=false"
        )

        assert response.status_code == 200
        assert response.json() == {"success": True}
        args, _ = repeating_ops_mock.toggle_repeating_message.call_args
        assert args[1] is False

    def test_toggle_not_found_404(
        self, repeating_client: TestClient, repeating_ops_mock
    ):
        repeating_ops_mock.toggle_repeating_message.return_value = False

        response = repeating_client.post(
            f"/api/repeating-messages/{uuid4()}/toggle?is_active=true"
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "Repeating message not found"}

    def test_toggle_missing_query_param_400(self, repeating_client: TestClient):
        # ``is_active`` is a required query parameter (FastAPI declared it
        # without a default); omitting it is a validation failure.
        response = repeating_client.post(
            f"/api/repeating-messages/{uuid4()}/toggle"
        )

        assert response.status_code in (400, 422)
