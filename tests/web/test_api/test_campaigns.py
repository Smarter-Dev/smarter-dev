"""Tests for campaign API endpoints.

This module tests the campaign management REST API endpoints including
campaign CRUD operations, challenge management, and statistics.
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
from smarter_dev.web.api.routers.campaigns import router
from smarter_dev.web.models import Campaign, Challenge, ChallengeSubmission as Submission
from web.repositories.campaign_repository import CampaignRepository
from web.repositories.challenge_repository import ChallengeRepository
from web.repositories.submission_repository import SubmissionRepository


class TestCampaignAPI:
    """Test suite for campaign API endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(api)

    @pytest.fixture
    def mock_session(self):
        """Create mock database session."""
        session = Mock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        return session

    @pytest.fixture
    def api_headers(self):
        """Create API headers with valid key."""
        return {
            "X-API-Key": "test_api_key",
            "Content-Type": "application/json"
        }

    @pytest.fixture
    def sample_campaign_data(self):
        """Sample campaign creation data."""
        return {
            "title": "Test Campaign",
            "description": "A test campaign for unit testing",
            "guild_id": "123456789012345678",
            "participant_type": "player",
            "start_date": "2025-01-01T00:00:00Z",
            "end_date": "2025-01-31T23:59:59Z",
            "challenge_release_delay_hours": 24,
            "scoring_strategy": "time_based",
            "scoring_config": {
                "positions": {"first": 100, "second": 75, "third": 50, "other": 25}
            }
        }

    @pytest.fixture
    def sample_campaign(self):
        """Sample campaign model."""
        campaign_id = uuid.uuid4()
        return Campaign(
            id=campaign_id,
            title="Test Campaign",
            description="A test campaign",
            guild_id="123456789012345678",
            participant_type="player",
            status="active",
            start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2025, 1, 31, tzinfo=timezone.utc),
            challenge_release_delay_hours=24,
            scoring_strategy="time_based",
            scoring_config={"positions": {"first": 100, "second": 75}},
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )

    @pytest.fixture
    def sample_challenge(self, sample_campaign):
        """Sample challenge model."""
        return Challenge(
            id=uuid.uuid4(),
            campaign_id=sample_campaign.id,
            title="Test Challenge",
            description="A test challenge",
            difficulty="medium",
            problem_statement="Solve this problem",
            generation_script="print('test input')",
            expected_output_format="Single line output",
            time_limit_minutes=60,
            memory_limit_mb=256,
            order_index=1,
            release_date=datetime.now(timezone.utc),
            script_updated_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )

    def test_create_campaign_success(self, client, api_headers, sample_campaign_data, mock_session, monkeypatch):
        """Test successful campaign creation."""
        # Mock dependencies
        mock_repo = Mock()
        mock_repo.create_campaign = AsyncMock(return_value=Mock(
            id=uuid.uuid4(),
            title=sample_campaign_data["title"],
            description=sample_campaign_data["description"],
            guild_id=sample_campaign_data["guild_id"],
            participant_type=sample_campaign_data["participant_type"],
            status="draft",
            start_date=datetime.fromisoformat(sample_campaign_data["start_date"].replace('Z', '+00:00')),
            end_date=datetime.fromisoformat(sample_campaign_data["end_date"].replace('Z', '+00:00')),
            challenge_release_delay_hours=sample_campaign_data["challenge_release_delay_hours"],
            scoring_strategy=sample_campaign_data["scoring_strategy"],
            scoring_config=sample_campaign_data["scoring_config"],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        ))

        def mock_campaign_repo_init(session):
            return mock_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: Mock())
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_request_metadata", lambda x: {})

        response = client.post("/campaigns/", json=sample_campaign_data, headers=api_headers)

        assert response.status_code == 201
        data = response.json()
        assert data["title"] == sample_campaign_data["title"]
        assert data["participant_type"] == sample_campaign_data["participant_type"]
        assert data["scoring_strategy"] == sample_campaign_data["scoring_strategy"]
        mock_repo.create_campaign.assert_called_once()

    def test_create_campaign_invalid_participant_type(self, client, api_headers, sample_campaign_data):
        """Test campaign creation with invalid participant type."""
        sample_campaign_data["participant_type"] = "invalid"

        response = client.post("/campaigns/", json=sample_campaign_data, headers=api_headers)

        assert response.status_code == 422
        data = response.json()
        assert "validation" in data["type"]

    def test_create_campaign_invalid_scoring_strategy(self, client, api_headers, sample_campaign_data):
        """Test campaign creation with invalid scoring strategy."""
        sample_campaign_data["scoring_strategy"] = "invalid"

        response = client.post("/campaigns/", json=sample_campaign_data, headers=api_headers)

        assert response.status_code == 422
        data = response.json()
        assert "validation" in data["type"]

    def test_list_campaigns_success(self, client, api_headers, sample_campaign, mock_session, monkeypatch):
        """Test successful campaign listing."""
        # Mock dependencies
        mock_repo = Mock()
        mock_repo.list_campaigns = AsyncMock(return_value=([sample_campaign], 1))

        def mock_campaign_repo_init(session):
            return mock_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: Mock())

        response = client.get("/campaigns/", headers=api_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["title"] == sample_campaign.title
        mock_repo.list_campaigns.assert_called_once()

    def test_list_campaigns_with_filters(self, client, api_headers, sample_campaign, mock_session, monkeypatch):
        """Test campaign listing with filters."""
        # Mock dependencies
        mock_repo = Mock()
        mock_repo.list_campaigns = AsyncMock(return_value=([sample_campaign], 1))

        def mock_campaign_repo_init(session):
            return mock_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: Mock())
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.validate_discord_id", lambda x, y: None)

        response = client.get(
            "/campaigns/?guild_id=123456789012345678&status=active&participant_type=player&page=1&size=10",
            headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        mock_repo.list_campaigns.assert_called_once_with(
            guild_id="123456789012345678",
            status="active",
            participant_type="player",
            limit=10,
            offset=0
        )

    def test_get_campaign_success(self, client, api_headers, sample_campaign, mock_session, monkeypatch):
        """Test successful campaign retrieval."""
        # Mock dependencies
        mock_repo = Mock()
        mock_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)

        def mock_campaign_repo_init(session):
            return mock_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: Mock())

        response = client.get(f"/campaigns/{sample_campaign.id}", headers=api_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(sample_campaign.id)
        assert data["title"] == sample_campaign.title
        mock_repo.get_campaign_by_id.assert_called_once_with(sample_campaign.id)

    def test_get_campaign_not_found(self, client, api_headers, mock_session, monkeypatch):
        """Test campaign retrieval when not found."""
        # Mock dependencies
        mock_repo = Mock()
        mock_repo.get_campaign_by_id = AsyncMock(return_value=None)

        def mock_campaign_repo_init(session):
            return mock_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: Mock())

        campaign_id = uuid.uuid4()
        response = client.get(f"/campaigns/{campaign_id}", headers=api_headers)

        assert response.status_code == 404
        mock_repo.get_campaign_by_id.assert_called_once_with(campaign_id)

    def test_update_campaign_success(self, client, api_headers, sample_campaign, mock_session, monkeypatch):
        """Test successful campaign update."""
        # Mock dependencies
        mock_repo = Mock()
        mock_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        updated_campaign = Mock()
        updated_campaign.id = sample_campaign.id
        updated_campaign.title = "Updated Campaign"
        updated_campaign.description = sample_campaign.description
        updated_campaign.guild_id = sample_campaign.guild_id
        updated_campaign.participant_type = sample_campaign.participant_type
        updated_campaign.status = sample_campaign.status
        updated_campaign.start_date = sample_campaign.start_date
        updated_campaign.end_date = sample_campaign.end_date
        updated_campaign.challenge_release_delay_hours = sample_campaign.challenge_release_delay_hours
        updated_campaign.scoring_strategy = sample_campaign.scoring_strategy
        updated_campaign.scoring_config = sample_campaign.scoring_config
        updated_campaign.created_at = sample_campaign.created_at
        updated_campaign.updated_at = datetime.now(timezone.utc)

        mock_repo.update_campaign = AsyncMock(return_value=updated_campaign)

        def mock_campaign_repo_init(session):
            return mock_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: Mock())

        update_data = {"title": "Updated Campaign"}
        response = client.put(f"/campaigns/{sample_campaign.id}", json=update_data, headers=api_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Updated Campaign"
        mock_repo.update_campaign.assert_called_once()

    def test_delete_campaign_success(self, client, api_headers, sample_campaign, mock_session, monkeypatch):
        """Test successful campaign deletion."""
        # Mock dependencies
        mock_repo = Mock()
        mock_repo.delete_campaign = AsyncMock(return_value=True)

        def mock_campaign_repo_init(session):
            return mock_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: Mock())

        response = client.delete(f"/campaigns/{sample_campaign.id}", headers=api_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Campaign deleted successfully"
        mock_repo.delete_campaign.assert_called_once_with(sample_campaign.id)

    def test_create_challenge_success(self, client, api_headers, sample_campaign, sample_challenge, mock_session, monkeypatch):
        """Test successful challenge creation."""
        # Mock dependencies
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)

        mock_challenge_repo = Mock()
        mock_challenge_repo.create_challenge = AsyncMock(return_value=sample_challenge)

        def mock_campaign_repo_init(session):
            return mock_campaign_repo

        def mock_challenge_repo_init(session):
            return mock_challenge_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", mock_challenge_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: Mock())

        challenge_data = {
            "title": "Test Challenge",
            "description": "A test challenge",
            "difficulty": "medium",
            "problem_statement": "Solve this problem",
            "generation_script": "print('test input')",
            "expected_output_format": "Single line output",
            "time_limit_minutes": 60,
            "memory_limit_mb": 256,
            "order_index": 1
        }

        response = client.post(
            f"/campaigns/{sample_campaign.id}/challenges",
            json=challenge_data,
            headers=api_headers
        )

        assert response.status_code == 201
        data = response.json()
        assert data["title"] == challenge_data["title"]
        assert data["difficulty"] == challenge_data["difficulty"]
        mock_campaign_repo.get_campaign_by_id.assert_called_once_with(sample_campaign.id)
        mock_challenge_repo.create_challenge.assert_called_once()

    def test_list_challenges_success(self, client, api_headers, sample_campaign, sample_challenge, mock_session, monkeypatch):
        """Test successful challenge listing."""
        # Mock dependencies
        mock_challenge_repo = Mock()
        mock_challenge_repo.get_challenges_by_campaign = AsyncMock(return_value=[sample_challenge])

        def mock_challenge_repo_init(session):
            return mock_challenge_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", mock_challenge_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: Mock())

        response = client.get(f"/campaigns/{sample_campaign.id}/challenges", headers=api_headers)

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["title"] == sample_challenge.title
        mock_challenge_repo.get_challenges_by_campaign.assert_called_once_with(
            sample_campaign.id,
            released_only=True
        )

    def test_get_challenge_success(self, client, api_headers, sample_campaign, sample_challenge, mock_session, monkeypatch):
        """Test successful challenge retrieval."""
        # Mock dependencies
        mock_challenge_repo = Mock()
        mock_challenge_repo.get_challenge_by_id = AsyncMock(return_value=sample_challenge)

        def mock_challenge_repo_init(session):
            return mock_challenge_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", mock_challenge_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: Mock())

        response = client.get(
            f"/campaigns/{sample_campaign.id}/challenges/{sample_challenge.id}",
            headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(sample_challenge.id)
        assert data["title"] == sample_challenge.title
        mock_challenge_repo.get_challenge_by_id.assert_called_once_with(sample_challenge.id)

    def test_get_campaign_stats_success(self, client, api_headers, sample_campaign, mock_session, monkeypatch):
        """Test successful campaign statistics retrieval."""
        # Mock dependencies
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)

        mock_submission_repo = Mock()
        mock_submission_repo.get_campaign_statistics = AsyncMock(return_value={
            'total_challenges': 5,
            'released_challenges': 3,
            'total_participants': 10,
            'total_submissions': 25,
            'correct_submissions': 15,
            'success_rate': 60.0,
            'avg_points_per_participant': 75.5,
            'top_participants': [
                {'participant_id': 'user123', 'points': 100},
                {'participant_id': 'user456', 'points': 85}
            ],
            'challenge_completion_rates': [
                {'challenge_id': str(uuid.uuid4()), 'completion_rate': 80.0}
            ]
        })

        def mock_campaign_repo_init(session):
            return mock_campaign_repo

        def mock_submission_repo_init(session):
            return mock_submission_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: Mock())

        response = client.get(f"/campaigns/{sample_campaign.id}/stats", headers=api_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["campaign_id"] == str(sample_campaign.id)
        assert data["total_challenges"] == 5
        assert data["released_challenges"] == 3
        assert data["total_participants"] == 10
        assert data["success_rate"] == 60.0
        assert len(data["top_participants"]) == 2
        mock_campaign_repo.get_campaign_by_id.assert_called_once_with(sample_campaign.id)
        mock_submission_repo.get_campaign_statistics.assert_called_once_with(sample_campaign.id)