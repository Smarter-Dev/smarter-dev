"""Parity tests for the native (Litestar) daily-quest API (unit U5).

The legacy ``routers/quests.py`` had no dedicated FastAPI test file, so these
assert the wire contract directly: exact status codes and JSON bodies for happy
paths, the plain ``{"detail": ...}`` error shapes, 404/403 branches, the
``UndefinedTableError`` degradations, and the 422 on a malformed quest UUID.
Paths carry the final ``/api`` prefix because the native controller declares its
mounted path (the FastAPI app was itself mounted at ``/api``).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

from litestar.testing import TestClient

from smarter_dev.web.crud import DatabaseOperationError

_GUILD = "123456789012345678"
_USER = "987654321098765432"


def _quest_mock(**overrides) -> Mock:
    """Build a Quest-like mock for response serialization."""
    quest = Mock()
    quest.id = overrides.get("id", uuid4())
    quest.title = overrides.get("title", "The First Quest")
    quest.prompt = overrides.get("prompt", "Solve the riddle")
    quest.quest_type = overrides.get("quest_type", "daily")
    quest.input_generator_script = overrides.get("input_generator_script", None)
    return quest


def _daily_quest_mock(**overrides) -> Mock:
    """Build a DailyQuest-like mock with a joined quest."""
    daily = Mock()
    daily.id = overrides.get("id", uuid4())
    daily.guild_id = overrides.get("guild_id", _GUILD)
    daily.is_active = overrides.get("is_active", True)
    daily.active_date = overrides.get("active_date", date(2026, 7, 17))
    daily.expires_at = overrides.get("expires_at", datetime(2026, 7, 18, tzinfo=timezone.utc))
    daily.quest = overrides.get("quest", _quest_mock())
    return daily


def _squad_mock(name: str = "Alpha Squad") -> Mock:
    squad = Mock()
    squad.id = uuid4()
    squad.name = name
    return squad


class TestCurrentDailyQuest:
    def test_returns_active_quest(self, quest_client: TestClient, quest_ops_mock):
        daily = _daily_quest_mock()
        quest_ops_mock.get_daily_quest.return_value = daily

        response = quest_client.get(f"/api/quests/daily/current?guild_id={_GUILD}")

        assert response.status_code == 200
        body = response.json()["quest"]
        assert body["id"] == str(daily.id)
        assert body["title"] == "The First Quest"
        assert body["quest_type"] == "daily"
        assert body["hint"] == "Once you're ready, submit with /daily submit"

    def test_returns_null_when_no_quest(self, quest_client: TestClient, quest_ops_mock):
        quest_ops_mock.get_daily_quest.return_value = None

        response = quest_client.get(f"/api/quests/daily/current?guild_id={_GUILD}")

        assert response.status_code == 200
        assert response.json() == {
            "quest": None,
            "message": "No daily quest available yet",
        }

    def test_returns_null_when_inactive(self, quest_client: TestClient, quest_ops_mock):
        quest_ops_mock.get_daily_quest.return_value = _daily_quest_mock(is_active=False)

        response = quest_client.get(f"/api/quests/daily/current?guild_id={_GUILD}")

        assert response.status_code == 200
        assert response.json()["quest"] is None

    def test_missing_tables_degrades(self, quest_client: TestClient, quest_ops_mock):
        quest_ops_mock.get_daily_quest.side_effect = DatabaseOperationError(
            "psycopg.errors.UndefinedTableError: relation does not exist"
        )

        response = quest_client.get(f"/api/quests/daily/current?guild_id={_GUILD}")

        assert response.status_code == 200
        assert response.json()["message"] == "Quest tables not yet created"

    def test_generic_db_error_is_500(self, quest_client: TestClient, quest_ops_mock):
        quest_ops_mock.get_daily_quest.side_effect = DatabaseOperationError("boom")

        response = quest_client.get(f"/api/quests/daily/current?guild_id={_GUILD}")

        assert response.status_code == 500
        assert response.json() == {"detail": "Failed to retrieve daily quest"}


class TestSubmitDailyQuest:
    def _body(self, solution: str = "42") -> dict:
        return {"guild_id": _GUILD, "user_id": _USER, "submitted_solution": solution}

    def test_submit_success(
        self,
        quest_client: TestClient,
        quest_ops_mock,
        quest_squad_ops_mock,
        quest_submission_ops_mock,
    ):
        quest_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        quest_ops_mock.get_daily_quest_by_id.return_value = _daily_quest_mock()
        quest_submission_ops_mock.submit_solution.return_value = (True, True, 20)

        response = quest_client.post(
            f"/api/quests/{uuid4()}/submit", json=self._body()
        )

        assert response.status_code == 200
        assert response.json() == {
            "is_correct": True,
            "is_first_success": True,
            "points_earned": 20,
        }

    def test_submit_no_squad_404(
        self, quest_client: TestClient, quest_squad_ops_mock
    ):
        quest_squad_ops_mock.get_user_squad.return_value = None

        response = quest_client.post(
            f"/api/quests/{uuid4()}/submit", json=self._body()
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "User is not a member of any squad"}

    def test_submit_quest_not_found_404(
        self, quest_client: TestClient, quest_ops_mock, quest_squad_ops_mock
    ):
        quest_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        quest_ops_mock.get_daily_quest_by_id.return_value = None

        response = quest_client.post(
            f"/api/quests/{uuid4()}/submit", json=self._body()
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "Daily quest not found"}

    def test_submit_inactive_quest_403(
        self, quest_client: TestClient, quest_ops_mock, quest_squad_ops_mock
    ):
        quest_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        quest_ops_mock.get_daily_quest_by_id.return_value = _daily_quest_mock(
            is_active=False
        )

        response = quest_client.post(
            f"/api/quests/{uuid4()}/submit", json=self._body()
        )

        assert response.status_code == 403
        assert response.json() == {"detail": "Daily quest is not active"}

    def test_submit_malformed_uuid_422(
        self, quest_client: TestClient, quest_squad_ops_mock
    ):
        response = quest_client.post(
            "/api/quests/not-a-uuid/submit", json=self._body()
        )

        assert response.status_code == 422


class TestDailyQuestInput:
    def test_input_static_when_no_script(
        self, quest_client: TestClient, quest_ops_mock, quest_squad_ops_mock
    ):
        quest_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        quest_ops_mock.get_daily_quest_by_id.return_value = _daily_quest_mock()

        quest_id = uuid4()
        response = quest_client.get(
            f"/api/quests/{quest_id}/input?guild_id={_GUILD}&user_id={_USER}"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["input_data"] == "No input required for this quest."
        assert body["metadata"]["has_existing_input"] is True
        assert body["squad"]["name"] == "Alpha Squad"

    def test_input_generated_when_script(
        self,
        quest_client: TestClient,
        quest_ops_mock,
        quest_squad_ops_mock,
        quest_input_ops_mock,
    ):
        quest = _quest_mock(input_generator_script="print('x')")
        quest_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        quest_ops_mock.get_daily_quest_by_id.return_value = _daily_quest_mock(quest=quest)
        quest_input_ops_mock.get_or_create_input.return_value = ("the-input", "the-result")

        response = quest_client.get(
            f"/api/quests/{uuid4()}/input?guild_id={_GUILD}&user_id={_USER}"
        )

        assert response.status_code == 200
        assert response.json()["input_data"] == "the-input"

    def test_input_no_squad_404(self, quest_client: TestClient, quest_squad_ops_mock):
        quest_squad_ops_mock.get_user_squad.return_value = None

        response = quest_client.get(
            f"/api/quests/{uuid4()}/input?guild_id={_GUILD}&user_id={_USER}"
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "User is not a member of any squad"}

    def test_input_wrong_guild_403(
        self, quest_client: TestClient, quest_ops_mock, quest_squad_ops_mock
    ):
        quest_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        quest_ops_mock.get_daily_quest_by_id.return_value = _daily_quest_mock(
            guild_id="999999999999999999"
        )

        response = quest_client.get(
            f"/api/quests/{uuid4()}/input?guild_id={_GUILD}&user_id={_USER}"
        )

        assert response.status_code == 403
        assert response.json() == {
            "detail": "Quest does not belong to the specified guild"
        }


class TestQuestScoreboard:
    def test_scoreboard_success(
        self, quest_client: TestClient, quest_ops_mock, quest_submission_ops_mock
    ):
        daily = _daily_quest_mock()
        quest_ops_mock.get_daily_quest.return_value = daily
        quest_submission_ops_mock.get_daily_quest_scoreboard.return_value = [
            {"squad_id": str(uuid4()), "squad_name": "Alpha", "points": 20}
        ]

        response = quest_client.get(f"/api/quests/scoreboard?guild_id={_GUILD}")

        assert response.status_code == 200
        body = response.json()
        assert body["quest"]["id"] == str(daily.id)
        assert body["scoreboard"][0]["squad_name"] == "Alpha"

    def test_scoreboard_no_quest(self, quest_client: TestClient, quest_ops_mock):
        quest_ops_mock.get_daily_quest.return_value = None

        response = quest_client.get(f"/api/quests/scoreboard?guild_id={_GUILD}")

        assert response.status_code == 200
        assert response.json() == {"quest": None, "scoreboard": []}

    def test_scoreboard_route_wins_over_uuid_param(
        self, quest_client: TestClient, quest_ops_mock
    ):
        # '/scoreboard' is a literal segment; it must not be captured by the
        # '/{daily_quest_id}/...' param routes.
        quest_ops_mock.get_daily_quest.return_value = None

        response = quest_client.get(f"/api/quests/scoreboard?guild_id={_GUILD}")

        assert response.status_code == 200
        assert "scoreboard" in response.json()


class TestDetailedScoreboard:
    def test_detailed_no_quest(self, quest_client: TestClient, quest_ops_mock):
        quest_ops_mock.get_daily_quest.return_value = None

        response = quest_client.get(
            f"/api/quests/detailed-scoreboard?guild_id={_GUILD}"
        )

        assert response.status_code == 200
        assert response.json() == {
            "quest": None,
            "detailed_scoreboard": [],
            "total_submissions": 0,
            "total_challenges": 0,
        }

    def test_detailed_with_quest_returns_null(
        self, quest_client: TestClient, quest_ops_mock
    ):
        # Faithful port of the legacy bug: once a daily quest exists the handler
        # falls off the end without a return, serializing as JSON null.
        quest_ops_mock.get_daily_quest.return_value = _daily_quest_mock()

        response = quest_client.get(
            f"/api/quests/detailed-scoreboard?guild_id={_GUILD}"
        )

        assert response.status_code == 200
        assert response.json() is None


class TestUpcomingAnnouncements:
    def test_upcoming_success(self, quest_client: TestClient, quest_ops_mock):
        daily = _daily_quest_mock()
        quest_ops_mock.get_upcoming_daily_quests.return_value = [daily]

        response = quest_client.get("/api/quests/upcoming-announcements?seconds=60")

        assert response.status_code == 200
        quests = response.json()["quests"]
        assert len(quests) == 1
        assert quests[0]["id"] == str(daily.id)
        assert quests[0]["quest_id"] == str(daily.quest.id)

    def test_upcoming_missing_tables_empty(
        self, quest_client: TestClient, quest_ops_mock
    ):
        quest_ops_mock.get_upcoming_daily_quests.side_effect = DatabaseOperationError(
            "UndefinedTableError"
        )

        response = quest_client.get("/api/quests/upcoming-announcements")

        assert response.status_code == 200
        assert response.json() == {"quests": []}


class TestMarkQuest:
    def test_mark_announced_success(self, quest_client: TestClient, quest_ops_mock):
        quest_ops_mock.mark_daily_quest_announced.return_value = True

        response = quest_client.post(f"/api/quests/{uuid4()}/mark-announced")

        assert response.status_code == 200
        assert response.json() == {"success": True}

    def test_mark_announced_not_found(self, quest_client: TestClient, quest_ops_mock):
        quest_ops_mock.mark_daily_quest_announced.return_value = False

        response = quest_client.post(f"/api/quests/{uuid4()}/mark-announced")

        assert response.status_code == 404
        assert response.json() == {"detail": "Daily quest not found"}

    def test_mark_active_success(self, quest_client: TestClient, quest_ops_mock):
        quest_ops_mock.mark_daily_quest_active.return_value = True

        response = quest_client.post(f"/api/quests/{uuid4()}/mark-active")

        assert response.status_code == 200
        assert response.json() == {"success": True}

    def test_mark_active_missing_tables_404(
        self, quest_client: TestClient, quest_ops_mock
    ):
        quest_ops_mock.mark_daily_quest_active.side_effect = DatabaseOperationError(
            "UndefinedTableError"
        )

        response = quest_client.post(f"/api/quests/{uuid4()}/mark-active")

        assert response.status_code == 404
        assert response.json() == {"detail": "Quest tables not yet created"}
