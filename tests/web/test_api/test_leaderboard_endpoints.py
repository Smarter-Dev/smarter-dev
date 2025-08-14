"""Tests for leaderboard and statistics API endpoints.

This module tests the campaign leaderboard, challenge leaderboard,
and participant statistics REST API endpoints.
"""

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from unittest.mock import Mock, AsyncMock

import pytest
from fastapi.testclient import TestClient
from uuid import UUID

from smarter_dev.web.api.app import api
from smarter_dev.web.models import Campaign, Challenge, APIKey


class TestLeaderboardAPI:
    """Test suite for leaderboard and statistics API endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(api)

    @pytest.fixture
    def api_headers(self):
        """Create API headers with valid key."""
        return {
            "X-API-Key": "test_api_key",
            "Content-Type": "application/json"
        }

    @pytest.fixture
    def sample_campaign(self):
        """Sample campaign model."""
        campaign_id = uuid.uuid4()
        return Campaign(
            id=campaign_id,
            name="Test Campaign",
            description="A test campaign for leaderboards",
            guild_id="123456789012345678",
            campaign_type="player",
            state="active",
            start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
            release_delay_minutes=1440,
            scoring_type="time_based",
            starting_points=100,
            points_decrease_step=10,
            announcement_channel_id="123456789012345678"
        )

    @pytest.fixture
    def sample_challenge(self, sample_campaign):
        """Sample challenge model."""
        return Challenge(
            id=uuid.uuid4(),
            campaign_id=sample_campaign.id,
            title="Test Challenge",
            description="A test challenge for leaderboards",
            generation_script="import json\nprint(json.dumps({'input': 42, 'expected': '42'}))",
            order_position=1,
            categories=["math"],
            difficulty_level=5
        )

    @pytest.fixture
    def sample_leaderboard_data(self):
        """Sample leaderboard data."""
        return [
            {
                "rank": 1,
                "participant_id": "user123",
                "participant_type": "player",
                "total_points": 280,
                "completed_challenges": 3,
                "first_completion": "2025-01-15T10:30:00Z"
            },
            {
                "rank": 2,
                "participant_id": "squad456",
                "participant_type": "squad",
                "total_points": 240,
                "completed_challenges": 2,
                "first_completion": "2025-01-15T11:00:00Z"
            },
            {
                "rank": 3,
                "participant_id": "user789",
                "participant_type": "player",
                "total_points": 180,
                "completed_challenges": 2,
                "first_completion": "2025-01-15T12:00:00Z"
            }
        ]

    def test_get_campaign_leaderboard_success(self, client, api_headers, sample_campaign, sample_leaderboard_data, monkeypatch):
        """Test successful campaign leaderboard retrieval."""
        mock_session = AsyncMock()
        
        # Mock repositories
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        mock_submission_repo = Mock()
        mock_submission_repo.get_leaderboard_data = AsyncMock(return_value=sample_leaderboard_data)
        
        # Setup monkeypatches
        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_submission_repo_init(session):
            return mock_submission_repo

        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        response = client.get(
            f"/campaigns/{sample_campaign.id}/leaderboard",
            headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert "leaderboard" in data
        assert len(data["leaderboard"]) == 3
        assert data["leaderboard"][0]["rank"] == 1
        assert data["leaderboard"][0]["participant_id"] == "user123"
        assert data["leaderboard"][0]["total_points"] == 280
        
        # Verify repository calls
        mock_campaign_repo.get_campaign_by_id.assert_called_once()
        mock_submission_repo.get_leaderboard_data.assert_called_once()

    def test_get_campaign_leaderboard_with_participant_filter(self, client, api_headers, sample_campaign, sample_leaderboard_data, monkeypatch):
        """Test campaign leaderboard with participant type filter."""
        mock_session = AsyncMock()
        
        # Filter data for players only
        player_data = [entry for entry in sample_leaderboard_data if entry["participant_type"] == "player"]
        
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        mock_submission_repo = Mock()
        mock_submission_repo.get_leaderboard_data = AsyncMock(return_value=player_data)
        
        # Setup monkeypatches
        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_submission_repo_init(session):
            return mock_submission_repo

        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        response = client.get(
            f"/campaigns/{sample_campaign.id}/leaderboard",
            params={"participant_type": "player"},
            headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["leaderboard"]) == 2  # Only players
        for entry in data["leaderboard"]:
            assert entry["participant_type"] == "player"

    def test_get_challenge_leaderboard_success(self, client, api_headers, sample_campaign, sample_challenge, monkeypatch):
        """Test successful challenge leaderboard retrieval."""
        mock_session = AsyncMock()
        
        challenge_leaderboard_data = [
            {
                "rank": 1,
                "participant_id": "user123",
                "participant_type": "player",
                "points_awarded": 100,
                "solve_time_minutes": 5.5,
                "submission_timestamp": "2025-01-15T10:35:30Z"
            },
            {
                "rank": 2,
                "participant_id": "user456",
                "participant_type": "player",
                "points_awarded": 90,
                "solve_time_minutes": 8.2,
                "submission_timestamp": "2025-01-15T10:38:12Z"
            }
        ]
        
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        mock_challenge_repo = Mock()
        mock_challenge_repo.get_challenge_by_id = AsyncMock(return_value=sample_challenge)
        
        mock_submission_repo = Mock()
        mock_submission_repo.get_leaderboard_data = AsyncMock(return_value=challenge_leaderboard_data)
        
        # Setup monkeypatches
        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_challenge_repo_init(session):
            return mock_challenge_repo
        def mock_submission_repo_init(session):
            return mock_submission_repo

        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", mock_challenge_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        response = client.get(
            f"/campaigns/{sample_campaign.id}/challenges/{sample_challenge.id}/leaderboard",
            headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert "leaderboard" in data
        assert len(data["leaderboard"]) == 2
        assert data["leaderboard"][0]["rank"] == 1
        assert data["leaderboard"][0]["solve_time_minutes"] == 5.5

    def test_get_participant_stats_success(self, client, api_headers, sample_campaign, monkeypatch):
        """Test successful participant statistics retrieval."""
        mock_session = AsyncMock()
        
        participant_stats = {
            "participant_id": "user123",
            "participant_type": "player",
            "rank": 1,
            "total_points": 280,
            "completed_challenges": 3,
            "total_submissions": 5,
            "success_rate": 60.0,
            "avg_solve_time_minutes": 6.8,
            "fastest_solve_minutes": 4.2,
            "first_submission_date": "2025-01-15T10:30:00Z",
            "latest_submission_date": "2025-01-16T14:22:15Z",
            "performance_trend": "improving",
            "recent_submissions": [
                {
                    "challenge_id": str(uuid.uuid4()),
                    "challenge_title": "Array Sum",
                    "is_correct": True,
                    "points_awarded": 90,
                    "submission_timestamp": "2025-01-16T14:22:15Z"
                },
                {
                    "challenge_id": str(uuid.uuid4()),
                    "challenge_title": "Binary Search",
                    "is_correct": False,
                    "points_awarded": 0,
                    "submission_timestamp": "2025-01-16T13:45:30Z"
                }
            ]
        }
        
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        mock_submission_repo = Mock()
        mock_submission_repo.get_campaign_statistics = AsyncMock(return_value=participant_stats)
        
        # Setup monkeypatches
        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_submission_repo_init(session):
            return mock_submission_repo

        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        response = client.get(
            f"/campaigns/{sample_campaign.id}/participant/user123/stats",
            headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["participant_id"] == "user123"
        assert data["rank"] == 1
        assert data["total_points"] == 280
        assert data["success_rate"] == 60.0
        assert len(data["recent_submissions"]) == 2

    def test_get_leaderboard_campaign_not_found(self, client, api_headers, monkeypatch):
        """Test leaderboard retrieval when campaign doesn't exist."""
        mock_session = AsyncMock()
        
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=None)

        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", lambda session: mock_campaign_repo)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        nonexistent_campaign_id = uuid.uuid4()
        response = client.get(
            f"/campaigns/{nonexistent_campaign_id}/leaderboard",
            headers=api_headers
        )

        assert response.status_code == 404

    def test_get_leaderboard_invalid_participant_type(self, client, api_headers, sample_campaign, monkeypatch):
        """Test leaderboard retrieval with invalid participant type."""
        mock_session = AsyncMock()
        
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)

        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", lambda session: mock_campaign_repo)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        response = client.get(
            f"/campaigns/{sample_campaign.id}/leaderboard",
            params={"participant_type": "invalid_type"},
            headers=api_headers
        )

        assert response.status_code == 422  # Validation error

    def test_get_participant_stats_not_found(self, client, api_headers, sample_campaign, monkeypatch):
        """Test participant statistics when participant has no data."""
        mock_session = AsyncMock()
        
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        mock_submission_repo = Mock()
        mock_submission_repo.get_campaign_statistics = AsyncMock(return_value=None)

        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", lambda session: mock_campaign_repo)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", lambda session: mock_submission_repo)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        response = client.get(
            f"/campaigns/{sample_campaign.id}/participant/nonexistent_user/stats",
            headers=api_headers
        )

        assert response.status_code == 404

    def test_get_leaderboard_unauthorized(self, client, sample_campaign):
        """Test leaderboard retrieval without proper authentication."""
        response = client.get(f"/campaigns/{sample_campaign.id}/leaderboard")
        assert response.status_code == 401