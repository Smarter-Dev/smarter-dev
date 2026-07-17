"""Parity tests for the native (Litestar) challenge API (unit U4).

The legacy ``routers/challenges.py`` had no dedicated FastAPI test file, so these
assert the wire contract directly: exact status codes and JSON bodies for happy
paths, the plain ``{"detail": ...}`` error shapes, 404/403 branches, the 422 on a
malformed challenge UUID, and the route-order guarantee that the static segments
(``/scoreboard`` etc.) win over the ``/{challenge_id}`` catch-all. Paths carry the
final ``/api`` prefix because the native controller declares its mounted path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

from litestar.testing import TestClient

from smarter_dev.web.crud import DatabaseOperationError, ScriptExecutionError

_GUILD = "123456789012345678"
_USER = "987654321098765432"


def _campaign_mock(**overrides) -> Mock:
    campaign = Mock()
    campaign.id = overrides.get("id", uuid4())
    campaign.title = overrides.get("title", "Summer Campaign")
    campaign.description = overrides.get("description", "A grand campaign")
    campaign.guild_id = overrides.get("guild_id", _GUILD)
    campaign.start_time = overrides.get(
        "start_time", datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    )
    campaign.release_cadence_hours = overrides.get("release_cadence_hours", 24)
    campaign.is_active = overrides.get("is_active", True)
    campaign.announcement_channels = overrides.get("announcement_channels", ["111"])
    return campaign


def _challenge_mock(**overrides) -> Mock:
    challenge = Mock()
    challenge.id = overrides.get("id", uuid4())
    challenge.title = overrides.get("title", "Challenge One")
    challenge.description = overrides.get("description", "Do the thing")
    challenge.order_position = overrides.get("order_position", 1)
    challenge.is_released = overrides.get("is_released", True)
    challenge.is_announced = overrides.get("is_announced", False)
    challenge.released_at = overrides.get("released_at", None)
    challenge.announced_at = overrides.get("announced_at", None)
    challenge.created_at = overrides.get(
        "created_at", datetime(2026, 6, 1, tzinfo=timezone.utc)
    )
    challenge.input_generator_script = overrides.get("input_generator_script", None)
    challenge.campaign = overrides.get("campaign", _campaign_mock())
    return challenge


def _squad_mock(name: str = "Alpha Squad") -> Mock:
    squad = Mock()
    squad.id = uuid4()
    squad.name = name
    return squad


class TestUpcomingAnnouncements:
    def test_success(self, challenge_client: TestClient, campaign_ops_mock):
        challenge = _challenge_mock()
        campaign_ops_mock.get_upcoming_announcements.return_value = [challenge]

        response = challenge_client.get(
            "/api/challenges/upcoming-announcements?seconds=60"
        )

        assert response.status_code == 200
        items = response.json()["challenges"]
        assert len(items) == 1
        assert items[0]["id"] == str(challenge.id)
        assert items[0]["campaign"]["id"] == str(challenge.campaign.id)

    def test_db_error_500(self, challenge_client: TestClient, campaign_ops_mock):
        campaign_ops_mock.get_upcoming_announcements.side_effect = (
            DatabaseOperationError("boom")
        )

        response = challenge_client.get("/api/challenges/upcoming-announcements")

        assert response.status_code == 500
        assert response.json() == {"detail": "Failed to retrieve upcoming announcements"}


class TestPendingAnnouncements:
    def test_success(self, challenge_client: TestClient, campaign_ops_mock):
        challenge = _challenge_mock(
            released_at=datetime(2026, 7, 2, tzinfo=timezone.utc)
        )
        campaign_ops_mock.get_pending_announcements.return_value = [challenge]

        response = challenge_client.get("/api/challenges/pending-announcements")

        assert response.status_code == 200
        items = response.json()["challenges"]
        assert items[0]["released_at"] == challenge.released_at.isoformat()


class TestMarkChallenge:
    def test_mark_released_success(self, challenge_client: TestClient, campaign_ops_mock):
        campaign_ops_mock.mark_challenge_released.return_value = True

        response = challenge_client.post(f"/api/challenges/{uuid4()}/mark-released")

        assert response.status_code == 200
        assert response.json() == {"success": True}

    def test_mark_released_not_found(self, challenge_client: TestClient, campaign_ops_mock):
        campaign_ops_mock.mark_challenge_released.return_value = False

        response = challenge_client.post(f"/api/challenges/{uuid4()}/mark-released")

        assert response.status_code == 404
        assert response.json() == {"detail": "Challenge not found"}

    def test_mark_announced_success(self, challenge_client: TestClient, campaign_ops_mock):
        campaign_ops_mock.mark_challenge_announced.return_value = True

        response = challenge_client.post(f"/api/challenges/{uuid4()}/mark-announced")

        assert response.status_code == 200
        assert response.json() == {"success": True}

    def test_mark_malformed_uuid_422(self, challenge_client: TestClient):
        response = challenge_client.post("/api/challenges/not-a-uuid/mark-released")

        assert response.status_code == 422


class TestScoreboard:
    def test_scoreboard_success(
        self,
        challenge_client: TestClient,
        campaign_ops_mock,
        challenge_submission_ops_mock,
    ):
        campaign = _campaign_mock()
        campaign_ops_mock.get_most_recent_campaign.return_value = campaign
        challenge_submission_ops_mock.get_campaign_scoreboard.return_value = [
            {
                "squad_name": "Alpha",
                "total_points": 30,
                "successful_submissions": 2,
                "squad_id": uuid4(),
            }
        ]
        challenge_submission_ops_mock.get_campaign_submission_count.return_value = 5
        campaign_ops_mock.get_campaign_challenge_count.return_value = 3

        response = challenge_client.get(f"/api/challenges/scoreboard?guild_id={_GUILD}")

        assert response.status_code == 200
        body = response.json()
        assert body["campaign"]["id"] == str(campaign.id)
        assert body["campaign"]["num_challenges"] == 3
        assert body["total_submissions"] == 5
        assert body["scoreboard"][0]["squad_name"] == "Alpha"
        assert body["scoreboard"][0]["total_points"] == 30

    def test_scoreboard_no_campaign(self, challenge_client: TestClient, campaign_ops_mock):
        campaign_ops_mock.get_most_recent_campaign.return_value = None

        response = challenge_client.get(f"/api/challenges/scoreboard?guild_id={_GUILD}")

        assert response.status_code == 200
        assert response.json() == {
            "campaign": None,
            "scoreboard": [],
            "total_submissions": 0,
            "total_challenges": 0,
        }

    def test_scoreboard_route_wins_over_uuid_param(
        self, challenge_client: TestClient, campaign_ops_mock
    ):
        # '/scoreboard' is a literal segment; it must resolve to the scoreboard
        # handler, not '/{challenge_id}'. A '/{challenge_id}' capture would try to
        # parse 'scoreboard' as a UUID and 422.
        campaign_ops_mock.get_most_recent_campaign.return_value = None

        response = challenge_client.get(f"/api/challenges/scoreboard?guild_id={_GUILD}")

        assert response.status_code == 200
        assert "scoreboard" in response.json()


class TestUpcomingCampaign:
    def test_no_upcoming(self, challenge_client: TestClient, session_mock):
        result = Mock()
        result.scalar_one_or_none.return_value = None
        session_mock.execute.return_value = result

        response = challenge_client.get(
            f"/api/challenges/upcoming-campaign?guild_id={_GUILD}"
        )

        assert response.status_code == 200
        assert response.json() == {"campaign": None}

    def test_upcoming_present(self, challenge_client: TestClient, session_mock):
        campaign = _campaign_mock()
        result = Mock()
        result.scalar_one_or_none.return_value = campaign
        session_mock.execute.return_value = result

        response = challenge_client.get(
            f"/api/challenges/upcoming-campaign?guild_id={_GUILD}"
        )

        assert response.status_code == 200
        assert response.json()["campaign"]["id"] == str(campaign.id)


class TestDetailedScoreboard:
    def test_detailed_no_campaign(self, challenge_client: TestClient, campaign_ops_mock):
        campaign_ops_mock.get_most_recent_campaign.return_value = None

        response = challenge_client.get(
            f"/api/challenges/detailed-scoreboard?guild_id={_GUILD}"
        )

        assert response.status_code == 200
        assert response.json() == {
            "campaign": None,
            "detailed_scoreboard": [],
            "total_submissions": 0,
            "total_challenges": 0,
        }

    def test_detailed_success(
        self,
        challenge_client: TestClient,
        campaign_ops_mock,
        challenge_submission_ops_mock,
    ):
        campaign = _campaign_mock()
        campaign_ops_mock.get_most_recent_campaign.return_value = campaign
        challenge_submission_ops_mock.get_detailed_campaign_scoreboard.return_value = {
            "squads": []
        }
        challenge_submission_ops_mock.get_campaign_submission_count.return_value = 0
        campaign_ops_mock.get_campaign_challenge_count.return_value = 4

        response = challenge_client.get(
            f"/api/challenges/detailed-scoreboard?guild_id={_GUILD}"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["detailed_scoreboard"] == {"squads": []}
        assert body["campaign"]["num_challenges"] == 4


class TestGetChallenge:
    def test_get_challenge_success(self, challenge_client: TestClient, campaign_ops_mock):
        challenge = _challenge_mock()
        campaign_ops_mock.get_challenge_with_campaign.return_value = challenge

        response = challenge_client.get(f"/api/challenges/{challenge.id}")

        assert response.status_code == 200
        body = response.json()["challenge"]
        assert body["id"] == str(challenge.id)
        assert body["is_released"] is True
        assert body["campaign"]["id"] == str(challenge.campaign.id)

    def test_get_challenge_not_found(self, challenge_client: TestClient, campaign_ops_mock):
        campaign_ops_mock.get_challenge_with_campaign.return_value = None

        response = challenge_client.get(f"/api/challenges/{uuid4()}")

        assert response.status_code == 404
        assert response.json() == {"detail": "Challenge not found"}

    def test_get_challenge_malformed_uuid_422(self, challenge_client: TestClient):
        # A non-UUID, non-static segment routes to '/{challenge_id}' and 422s on
        # the UUID parse (matching the FastAPI UUID path-param validation).
        response = challenge_client.get("/api/challenges/not-a-uuid")

        assert response.status_code == 422


class TestChallengeInputExists:
    def test_exists_true(
        self,
        challenge_client: TestClient,
        challenge_squad_ops_mock,
        challenge_input_ops_mock,
    ):
        challenge_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        challenge_input_ops_mock.get_existing_input.return_value = Mock()

        response = challenge_client.get(
            f"/api/challenges/{uuid4()}/input-exists?guild_id={_GUILD}&user_id={_USER}"
        )

        assert response.status_code == 200
        assert response.json() == {"exists": True}

    def test_exists_false(
        self,
        challenge_client: TestClient,
        challenge_squad_ops_mock,
        challenge_input_ops_mock,
    ):
        challenge_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        challenge_input_ops_mock.get_existing_input.return_value = None

        response = challenge_client.get(
            f"/api/challenges/{uuid4()}/input-exists?guild_id={_GUILD}&user_id={_USER}"
        )

        assert response.status_code == 200
        assert response.json() == {"exists": False}

    def test_no_squad_404(
        self, challenge_client: TestClient, challenge_squad_ops_mock
    ):
        challenge_squad_ops_mock.get_user_squad.return_value = None

        response = challenge_client.get(
            f"/api/challenges/{uuid4()}/input-exists?guild_id={_GUILD}&user_id={_USER}"
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "User is not a member of any squad"}


class TestChallengeInput:
    def test_input_generated(
        self,
        challenge_client: TestClient,
        campaign_ops_mock,
        challenge_squad_ops_mock,
        challenge_input_ops_mock,
    ):
        challenge = _challenge_mock(input_generator_script="print('x')")
        challenge_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        campaign_ops_mock.get_challenge_with_campaign.return_value = challenge
        challenge_input_ops_mock.get_or_create_input.return_value = ("in", "out")

        response = challenge_client.get(
            f"/api/challenges/{uuid4()}/input?guild_id={_GUILD}&user_id={_USER}"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["input_data"] == "in"
        assert body["challenge"]["id"] == str(challenge.id)

    def test_input_wrong_guild_403(
        self,
        challenge_client: TestClient,
        campaign_ops_mock,
        challenge_squad_ops_mock,
    ):
        challenge = _challenge_mock(campaign=_campaign_mock(guild_id="999"))
        challenge_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        campaign_ops_mock.get_challenge_with_campaign.return_value = challenge

        response = challenge_client.get(
            f"/api/challenges/{uuid4()}/input?guild_id={_GUILD}&user_id={_USER}"
        )

        assert response.status_code == 403
        assert response.json() == {
            "detail": "Challenge does not belong to the specified guild"
        }

    def test_input_not_released_403(
        self,
        challenge_client: TestClient,
        campaign_ops_mock,
        challenge_squad_ops_mock,
    ):
        challenge = _challenge_mock(is_released=False)
        challenge_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        campaign_ops_mock.get_challenge_with_campaign.return_value = challenge

        response = challenge_client.get(
            f"/api/challenges/{uuid4()}/input?guild_id={_GUILD}&user_id={_USER}"
        )

        assert response.status_code == 403
        assert response.json() == {"detail": "Challenge has not been released yet"}

    def test_input_no_generator_404(
        self,
        challenge_client: TestClient,
        campaign_ops_mock,
        challenge_squad_ops_mock,
    ):
        challenge = _challenge_mock(input_generator_script=None)
        challenge_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        campaign_ops_mock.get_challenge_with_campaign.return_value = challenge

        response = challenge_client.get(
            f"/api/challenges/{uuid4()}/input?guild_id={_GUILD}&user_id={_USER}"
        )

        assert response.status_code == 404
        assert "input generation configured" in response.json()["detail"]

    def test_input_script_error_500(
        self,
        challenge_client: TestClient,
        campaign_ops_mock,
        challenge_squad_ops_mock,
        challenge_input_ops_mock,
    ):
        challenge = _challenge_mock(input_generator_script="print('x')")
        challenge_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        campaign_ops_mock.get_challenge_with_campaign.return_value = challenge
        challenge_input_ops_mock.get_or_create_input.side_effect = ScriptExecutionError(
            "bad script"
        )

        response = challenge_client.get(
            f"/api/challenges/{uuid4()}/input?guild_id={_GUILD}&user_id={_USER}"
        )

        assert response.status_code == 500
        assert response.json() == {
            "detail": "Failed to generate challenge input due to script execution error"
        }


class TestSubmitSolution:
    def _body(self) -> dict:
        return {
            "guild_id": _GUILD,
            "user_id": _USER,
            "username": "Tester",
            "submitted_solution": "42",
        }

    def test_submit_success(
        self,
        challenge_client: TestClient,
        campaign_ops_mock,
        challenge_squad_ops_mock,
        challenge_submission_ops_mock,
    ):
        challenge = _challenge_mock()
        squad = _squad_mock()
        challenge_squad_ops_mock.get_user_squad.return_value = squad
        campaign_ops_mock.get_challenge_with_campaign.return_value = challenge
        challenge_submission_ops_mock.submit_solution.return_value = (True, True, 10)

        response = challenge_client.post(
            f"/api/challenges/{uuid4()}/submit-solution", json=self._body()
        )

        assert response.status_code == 200
        body = response.json()
        assert body["is_correct"] is True
        assert body["points_earned"] == 10
        assert body["challenge"]["id"] == str(challenge.id)
        assert body["squad"]["id"] == str(squad.id)
        assert body["submitted_at"] == "just_now"

    def test_submit_no_squad_404(
        self, challenge_client: TestClient, challenge_squad_ops_mock
    ):
        challenge_squad_ops_mock.get_user_squad.return_value = None

        response = challenge_client.post(
            f"/api/challenges/{uuid4()}/submit-solution", json=self._body()
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "User is not a member of any squad"}

    def test_submit_not_released_403(
        self,
        challenge_client: TestClient,
        campaign_ops_mock,
        challenge_squad_ops_mock,
    ):
        challenge_squad_ops_mock.get_user_squad.return_value = _squad_mock()
        campaign_ops_mock.get_challenge_with_campaign.return_value = _challenge_mock(
            is_released=False
        )

        response = challenge_client.post(
            f"/api/challenges/{uuid4()}/submit-solution", json=self._body()
        )

        assert response.status_code == 403
        assert response.json() == {"detail": "Challenge has not been released yet"}
