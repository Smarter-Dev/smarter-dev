"""Parity tests for the native (Litestar) squad + sale-event API.

Ported one-for-one from ``tests/web/test_api/test_squads.py`` (the FastAPI
suite), plus coverage for the sale-event read endpoints (``squad_sale_events``)
which had no dedicated FastAPI test file. Paths carry the final ``/api`` prefix
because the native controllers declare their mounted path; the FastAPI app was
itself mounted at ``/api``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

from litestar.testing import TestClient

from smarter_dev.web.crud import ConflictError, DatabaseOperationError, NotFoundError


def _squad_mock(data: dict[str, Any], **overrides: Any) -> Mock:
    """Build a squad-like mock with id + timestamps for schema validation."""
    squad = Mock()
    for key, value in {**data, **overrides}.items():
        setattr(squad, key, value)
    if "id" not in overrides:
        squad.id = uuid4()
    squad.created_at = datetime.now(timezone.utc)
    squad.updated_at = datetime.now(timezone.utc)
    return squad


def _membership_mock(squad_id, user_id: str, guild_id: str) -> Mock:
    """Build a membership-like mock for schema validation."""
    membership = Mock()
    membership.squad_id = squad_id
    membership.user_id = user_id
    membership.guild_id = guild_id
    membership.joined_at = datetime.now(timezone.utc)
    return membership


def _sale_event_mock(guild_id: str, **overrides: Any) -> Mock:
    """Build a sale-event-like mock with all response + computed fields."""
    event = Mock()
    event.id = overrides.get("id", uuid4())
    event.guild_id = guild_id
    event.name = overrides.get("name", "Summer Sale")
    event.description = overrides.get("description", "Discounts abound")
    event.start_time = datetime.now(timezone.utc)
    event.duration_hours = 48
    event.join_discount_percent = 25
    event.switch_discount_percent = 10
    event.is_active = True
    event.created_by = "admin"
    event.created_at = datetime.now(timezone.utc)
    event.updated_at = datetime.now(timezone.utc)
    # Computed properties re-read by the serializer.
    event.end_time = datetime.now(timezone.utc)
    event.is_currently_active = True
    event.has_started = True
    event.has_ended = False
    event.time_remaining_hours = 12
    event.days_until_start = 0
    return event


class TestSquadListing:
    def test_list_squads_success(
        self, squad_client: TestClient, guild_id, squad_ops_mock, sample_squad_data
    ):
        squads = [_squad_mock(sample_squad_data, name=f"Squad {i}") for i in range(3)]
        squad_ops_mock.get_guild_squads.return_value = squads
        squad_ops_mock._get_squad_member_count.return_value = 2

        response = squad_client.get(f"/api/guilds/{guild_id}/squads/")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        assert all(squad["member_count"] == 2 for squad in data)

    def test_list_squads_with_inactive(
        self, squad_client: TestClient, guild_id, squad_ops_mock
    ):
        squad_ops_mock.get_guild_squads.return_value = []

        response = squad_client.get(f"/api/guilds/{guild_id}/squads/?include_inactive=true")

        assert response.status_code == 200
        squad_ops_mock.get_guild_squads.assert_called_with(
            squad_ops_mock.get_guild_squads.call_args[0][0], guild_id, active_only=False
        )

    def test_list_squads_empty(self, squad_client: TestClient, guild_id, squad_ops_mock):
        squad_ops_mock.get_guild_squads.return_value = []

        response = squad_client.get(f"/api/guilds/{guild_id}/squads/")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_squads_invalid_guild_id(self, squad_client: TestClient, squad_ops_mock):
        response = squad_client.get("/api/guilds/not-a-guild/squads/")

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid guild ID"


class TestSquadCreation:
    def test_create_squad_success(
        self, squad_client: TestClient, guild_id, squad_ops_mock, sample_squad_data
    ):
        squad_ops_mock.create_squad.return_value = _squad_mock(sample_squad_data)

        create_data = {
            "role_id": sample_squad_data["role_id"],
            "name": sample_squad_data["name"],
            "description": sample_squad_data["description"],
            "max_members": sample_squad_data["max_members"],
            "switch_cost": sample_squad_data["switch_cost"],
        }
        response = squad_client.post(f"/api/guilds/{guild_id}/squads/", json=create_data)

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == sample_squad_data["name"]
        assert data["member_count"] == 0

    def test_create_squad_role_already_used(
        self, squad_client: TestClient, guild_id, squad_ops_mock, sample_squad_data
    ):
        squad_ops_mock.create_squad.side_effect = ConflictError(
            "Role already associated with a squad"
        )
        response = squad_client.post(
            f"/api/guilds/{guild_id}/squads/",
            json={"role_id": sample_squad_data["role_id"], "name": sample_squad_data["name"]},
        )

        assert response.status_code == 400
        assert "Role already associated" in response.json()["detail"]

    def test_create_squad_invalid_data(self, squad_client: TestClient, guild_id):
        response = squad_client.post(
            f"/api/guilds/{guild_id}/squads/",
            json={"role_id": "invalid_role", "name": "", "max_members": -1, "switch_cost": -10},
        )

        assert response.status_code == 422


class TestSquadRetrieval:
    def test_get_squad_success(
        self, squad_client: TestClient, guild_id, squad_ops_mock, sample_squad_data
    ):
        squad_id = uuid4()
        squad_ops_mock.get_squad.return_value = _squad_mock(sample_squad_data, id=squad_id)
        squad_ops_mock._get_squad_member_count.return_value = 5

        response = squad_client.get(f"/api/guilds/{guild_id}/squads/{squad_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(squad_id)
        assert data["member_count"] == 5

    def test_get_squad_not_found(self, squad_client: TestClient, guild_id, squad_ops_mock):
        squad_ops_mock.get_squad.side_effect = NotFoundError("Squad not found")

        response = squad_client.get(f"/api/guilds/{guild_id}/squads/{uuid4()}")

        assert response.status_code == 404
        assert "Squad not found" in response.json()["detail"]

    def test_get_squad_wrong_guild(
        self, squad_client: TestClient, guild_id, squad_ops_mock, sample_squad_data
    ):
        squad_ops_mock.get_squad.return_value = _squad_mock(
            sample_squad_data, guild_id="different_guild_id"
        )

        response = squad_client.get(f"/api/guilds/{guild_id}/squads/{uuid4()}")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_squad_invalid_uuid(self, squad_client: TestClient, guild_id):
        response = squad_client.get(f"/api/guilds/{guild_id}/squads/invalid-uuid")

        assert response.status_code == 422


class TestSquadUpdate:
    def test_update_squad_success(
        self, squad_client: TestClient, guild_id, squad_ops_mock, sample_squad_data
    ):
        squad_id = uuid4()
        squad_ops_mock.get_squad.return_value = _squad_mock(sample_squad_data, id=squad_id)
        squad_ops_mock._get_squad_member_count.return_value = 3

        response = squad_client.put(
            f"/api/guilds/{guild_id}/squads/{squad_id}",
            json={"name": "Updated Squad Name", "switch_cost": 75},
        )

        assert response.status_code == 200
        assert response.json()["member_count"] == 3

    def test_update_squad_empty_data(
        self, squad_client: TestClient, guild_id, squad_ops_mock, sample_squad_data
    ):
        squad_ops_mock.get_squad.return_value = _squad_mock(sample_squad_data)

        response = squad_client.put(f"/api/guilds/{guild_id}/squads/{uuid4()}", json={})

        assert response.status_code == 400
        assert "No valid squad updates provided" in response.json()["detail"]


class TestSquadMembership:
    def test_join_squad_success(
        self, squad_client: TestClient, guild_id, user_id, squad_ops_mock, sample_squad_data
    ):
        squad_id = uuid4()
        squad_ops_mock.join_squad.return_value = _membership_mock(squad_id, user_id, guild_id)
        squad_ops_mock.get_squad.return_value = _squad_mock(sample_squad_data, id=squad_id)
        squad_ops_mock._get_squad_member_count.return_value = 1

        response = squad_client.post(
            f"/api/guilds/{guild_id}/squads/{squad_id}/join", json={"user_id": user_id}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == user_id
        assert data["squad_id"] == str(squad_id)
        assert data["squad"]["member_count"] == 1

    def test_join_squad_already_member(
        self, squad_client: TestClient, guild_id, user_id, squad_ops_mock
    ):
        squad_ops_mock.join_squad.side_effect = ConflictError("User already in squad Test Squad")

        response = squad_client.post(
            f"/api/guilds/{guild_id}/squads/{uuid4()}/join", json={"user_id": user_id}
        )

        assert response.status_code == 400
        assert "already in squad" in response.json()["detail"]

    def test_join_squad_insufficient_balance(
        self, squad_client: TestClient, guild_id, user_id, squad_ops_mock
    ):
        squad_ops_mock.join_squad.side_effect = ConflictError("Insufficient balance: 25 < 50")

        response = squad_client.post(
            f"/api/guilds/{guild_id}/squads/{uuid4()}/join", json={"user_id": user_id}
        )

        assert response.status_code == 400
        assert "Insufficient balance" in response.json()["detail"]

    def test_join_squad_invalid_user_id(self, squad_client: TestClient, guild_id):
        response = squad_client.post(
            f"/api/guilds/{guild_id}/squads/{uuid4()}/join", json={"user_id": "invalid_user_id"}
        )

        assert response.status_code == 422

    def test_leave_squad_success(
        self, squad_client: TestClient, guild_id, user_id, squad_ops_mock
    ):
        response = squad_client.request(
            "DELETE",
            f"/api/guilds/{guild_id}/squads/leave",
            json={"user_id": user_id},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert user_id in data["message"]
        squad_ops_mock.leave_squad.assert_called_once()

    def test_leave_squad_not_member(
        self, squad_client: TestClient, guild_id, user_id, squad_ops_mock
    ):
        squad_ops_mock.leave_squad.side_effect = NotFoundError("User not in any squad")

        response = squad_client.request(
            "DELETE",
            f"/api/guilds/{guild_id}/squads/leave",
            json={"user_id": user_id},
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


class TestUserSquadInfo:
    def test_get_user_squad_success(
        self,
        squad_client: TestClient,
        guild_id,
        user_id,
        squad_ops_mock,
        session_mock,
        sample_squad_data,
    ):
        squad_id = uuid4()
        squad_ops_mock.get_user_squad.return_value = _squad_mock(sample_squad_data, id=squad_id)
        squad_ops_mock._get_squad_member_count.return_value = 3

        membership = _membership_mock(squad_id, user_id, guild_id)
        result_mock = Mock()
        result_mock.scalar_one.return_value = membership
        session_mock.execute.return_value = result_mock

        response = squad_client.get(f"/api/guilds/{guild_id}/squads/members/{user_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == user_id
        assert data["squad"]["id"] == str(squad_id)
        assert data["membership"] is not None

    def test_get_user_squad_no_squad(
        self, squad_client: TestClient, guild_id, user_id, squad_ops_mock
    ):
        squad_ops_mock.get_user_squad.return_value = None

        response = squad_client.get(f"/api/guilds/{guild_id}/squads/members/{user_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["squad"] is None
        assert data["membership"] is None

    def test_get_user_squad_invalid_user_id(self, squad_client: TestClient, guild_id):
        response = squad_client.get(f"/api/guilds/{guild_id}/squads/members/invalid_user_id")

        assert response.status_code == 400
        assert "Invalid user ID format" in response.json()["detail"]["detail"]


class TestSquadMembers:
    def test_get_squad_members_success(
        self, squad_client: TestClient, guild_id, squad_ops_mock, sample_squad_data
    ):
        squad_id = uuid4()
        squad_ops_mock.get_squad.return_value = _squad_mock(sample_squad_data, id=squad_id)
        squad_ops_mock.get_squad_members.return_value = [
            _membership_mock(squad_id, f"user_{i}", guild_id) for i in range(2)
        ]

        response = squad_client.get(f"/api/guilds/{guild_id}/squads/{squad_id}/members")

        assert response.status_code == 200
        data = response.json()
        assert data["squad"]["id"] == str(squad_id)
        assert len(data["members"]) == 2
        assert data["total_members"] == 2

    def test_get_squad_members_wrong_guild(
        self, squad_client: TestClient, guild_id, squad_ops_mock, sample_squad_data
    ):
        squad_ops_mock.get_squad.return_value = _squad_mock(
            sample_squad_data, guild_id="different_guild_id"
        )

        response = squad_client.get(f"/api/guilds/{guild_id}/squads/{uuid4()}/members")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_squad_members_empty(
        self, squad_client: TestClient, guild_id, squad_ops_mock, sample_squad_data
    ):
        squad_id = uuid4()
        squad_ops_mock.get_squad.return_value = _squad_mock(sample_squad_data, id=squad_id)
        squad_ops_mock.get_squad_members.return_value = []

        response = squad_client.get(f"/api/guilds/{guild_id}/squads/{squad_id}/members")

        assert response.status_code == 200
        data = response.json()
        assert len(data["members"]) == 0
        assert data["total_members"] == 0


class TestSquadErrorHandling:
    def test_database_error_handling(self, squad_client: TestClient, guild_id, squad_ops_mock):
        squad_ops_mock.get_guild_squads.side_effect = DatabaseOperationError(
            "Database connection failed"
        )

        response = squad_client.get(f"/api/guilds/{guild_id}/squads/")

        assert response.status_code == 500
        assert "Database error" in response.json()["detail"]


class TestSaleEventListing:
    def test_list_sale_events_success(
        self, squad_client: TestClient, guild_id, sale_event_ops_mock
    ):
        events = [_sale_event_mock(guild_id, name=f"Sale {i}") for i in range(2)]
        sale_event_ops_mock.get_sale_events_by_guild.return_value = (events, 2)

        response = squad_client.get(f"/api/guilds/{guild_id}/squad-sale-events/")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["is_currently_active"] is True
        assert data[0]["time_remaining_hours"] == 12

    def test_list_sale_events_empty(
        self, squad_client: TestClient, guild_id, sale_event_ops_mock
    ):
        sale_event_ops_mock.get_sale_events_by_guild.return_value = ([], 0)

        response = squad_client.get(f"/api/guilds/{guild_id}/squad-sale-events/")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_sale_events_database_error(
        self, squad_client: TestClient, guild_id, sale_event_ops_mock
    ):
        sale_event_ops_mock.get_sale_events_by_guild.side_effect = DatabaseOperationError(
            "boom"
        )

        response = squad_client.get(f"/api/guilds/{guild_id}/squad-sale-events/")

        assert response.status_code == 500
        assert response.json()["detail"] == "Internal server error"

    def test_list_sale_events_invalid_guild(self, squad_client: TestClient):
        response = squad_client.get("/api/guilds/not-a-guild/squad-sale-events/")

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid guild ID"


class TestSaleEventRetrieval:
    def test_get_sale_event_success(
        self, squad_client: TestClient, guild_id, sale_event_ops_mock
    ):
        event_id = uuid4()
        sale_event_ops_mock.get_sale_event_by_id.return_value = _sale_event_mock(
            guild_id, id=event_id
        )

        response = squad_client.get(f"/api/guilds/{guild_id}/squad-sale-events/{event_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(event_id)
        assert data["has_started"] is True

    def test_get_sale_event_not_found_is_swallowed_to_500(
        self, squad_client: TestClient, guild_id, sale_event_ops_mock
    ):
        # Faithful port: the legacy router's own not-found HTTPException is caught
        # by its ``except Exception`` block and re-raised as a 500.
        sale_event_ops_mock.get_sale_event_by_id.return_value = None

        response = squad_client.get(f"/api/guilds/{guild_id}/squad-sale-events/{uuid4()}")

        assert response.status_code == 500
        assert response.json()["detail"] == "Internal server error"

    def test_get_sale_event_invalid_uuid(self, squad_client: TestClient, guild_id):
        response = squad_client.get(f"/api/guilds/{guild_id}/squad-sale-events/not-a-uuid")

        assert response.status_code == 422
