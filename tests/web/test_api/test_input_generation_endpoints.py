"""Tests for input generation API endpoints.

This module tests the input generation management REST API endpoints including
manual input generation and cache management.
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
from web.repositories.campaign_repository import CampaignRepository
from web.repositories.challenge_repository import ChallengeRepository
from web.repositories.submission_repository import SubmissionRepository
from web.services.input_generation_service import InputGenerationService, InputGenerationStatus, InputGenerationResult


class TestInputGenerationAPI:
    """Test suite for input generation API endpoints."""

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
            description="A test campaign",
            guild_id="123456789012345678",
            campaign_type="player",
            state="active",
            start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
            release_delay_minutes=1440,  # 24 hours in minutes
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
            description="A test challenge",
            generation_script="import json\nprint(json.dumps({'input': 42}))",
            order_position=1,
            categories=["math"],
            difficulty_level=5
        )

    def test_generate_challenge_input_success(self, client, api_headers, sample_campaign, sample_challenge, monkeypatch):
        """Test successful challenge input generation."""
        mock_session = AsyncMock()
        
        # Mock repositories
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        mock_challenge_repo = Mock()
        mock_challenge_repo.get_challenge_by_id = AsyncMock(return_value=sample_challenge)
        
        mock_submission_repo = Mock()
        mock_submission_repo.invalidate_input_cache = AsyncMock(return_value=0)
        
        # Mock input generation service
        mock_generation_result = InputGenerationResult(
            status=InputGenerationStatus.SUCCESS,
            input_data={"input": 42},
            expected_result="42",
            execution_time_ms=150,
            cached=False,
            error_message=None,
            script_output='{"input": 42}'
        )
        
        # Setup monkeypatches
        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_challenge_repo_init(session):
            return mock_challenge_repo
        def mock_submission_repo_init(session):
            return mock_submission_repo

        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read", "campaigns:write"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", mock_challenge_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        # Mock InputGenerationService
        mock_input_service = Mock()
        mock_input_service.generate_input = AsyncMock(return_value=mock_generation_result)
        monkeypatch.setattr("web.services.input_generation_service.InputGenerationService", lambda **kwargs: mock_input_service)
        
        response = client.post(
            f"/campaigns/{sample_campaign.id}/challenges/{sample_challenge.id}/generate-input",
            params={
                "participant_id": "test_player_123",
                "participant_type": "player",
                "force_regenerate": False
            },
            headers=api_headers
        )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "success"
        assert data["input_data"] == {"input": 42}
        assert data["expected_result"] == "42"
        assert data["execution_time_ms"] == 150
        assert data["cached"] is False
        
        # Verify service calls
        mock_campaign_repo.get_campaign_by_id.assert_called_once()
        mock_challenge_repo.get_challenge_by_id.assert_called_once()
        mock_input_service.generate_input.assert_called_once()

    def test_generate_challenge_input_force_regenerate(self, client, api_headers, sample_campaign, sample_challenge, monkeypatch):
        """Test input generation with force regenerate flag."""
        mock_session = AsyncMock()
        
        # Mock repositories
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        mock_challenge_repo = Mock()
        mock_challenge_repo.get_challenge_by_id = AsyncMock(return_value=sample_challenge)
        
        mock_submission_repo = Mock()
        mock_submission_repo.invalidate_input_cache = AsyncMock(return_value=2)
        
        # Mock input generation service
        mock_generation_result = InputGenerationResult(
            status=InputGenerationStatus.SUCCESS,
            input_data={"input": 100},
            expected_result="100",
            execution_time_ms=200,
            cached=False
        )
        
        # Setup monkeypatches
        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_challenge_repo_init(session):
            return mock_challenge_repo
        def mock_submission_repo_init(session):
            return mock_submission_repo

        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read", "campaigns:write"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", mock_challenge_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        # Mock InputGenerationService
        mock_input_service = Mock()
        mock_input_service.generate_input = AsyncMock(return_value=mock_generation_result)
        monkeypatch.setattr("web.services.input_generation_service.InputGenerationService", lambda **kwargs: mock_input_service)
        
        response = client.post(
            f"/campaigns/{sample_campaign.id}/challenges/{sample_challenge.id}/generate-input",
            params={
                "participant_id": "test_player_456", 
                "participant_type": "player",
                "force_regenerate": True
            },
            headers=api_headers
        )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "success"
        assert data["input_data"] == {"input": 100}
        
        # Verify cache invalidation was called
        mock_submission_repo.invalidate_input_cache.assert_called_once_with(
            challenge_id=sample_challenge.id,
            participant_id="test_player_456",
            participant_type="player"
        )

    def test_invalidate_input_cache_success(self, client, api_headers, sample_campaign, sample_challenge, monkeypatch):
        """Test successful input cache invalidation."""
        mock_session = AsyncMock()
        
        # Mock repositories
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        mock_challenge_repo = Mock()
        mock_challenge_repo.get_challenge_by_id = AsyncMock(return_value=sample_challenge)
        
        mock_submission_repo = Mock()
        mock_submission_repo.invalidate_input_cache = AsyncMock(return_value=5)
        
        # Setup monkeypatches
        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_challenge_repo_init(session):
            return mock_challenge_repo
        def mock_submission_repo_init(session):
            return mock_submission_repo

        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read", "campaigns:write"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", mock_challenge_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        response = client.delete(
            f"/campaigns/{sample_campaign.id}/challenges/{sample_challenge.id}/input-cache",
            headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert "Invalidated 5 cached input entries" in data["message"]
        assert data["challenge_id"] == str(sample_challenge.id)
        assert data["invalidated_count"] == 5
        
        # Verify cache invalidation was called for all participants
        mock_submission_repo.invalidate_input_cache.assert_called_once_with(
            challenge_id=sample_challenge.id,
            participant_id=None,
            participant_type=None
        )

    def test_invalidate_input_cache_specific_participant(self, client, api_headers, sample_campaign, sample_challenge, monkeypatch):
        """Test input cache invalidation for specific participant."""
        mock_session = AsyncMock()
        
        # Mock repositories
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        mock_challenge_repo = Mock()
        mock_challenge_repo.get_challenge_by_id = AsyncMock(return_value=sample_challenge)
        
        mock_submission_repo = Mock()
        mock_submission_repo.invalidate_input_cache = AsyncMock(return_value=1)
        
        # Setup monkeypatches
        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_challenge_repo_init(session):
            return mock_challenge_repo
        def mock_submission_repo_init(session):
            return mock_submission_repo

        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read", "campaigns:write"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", mock_challenge_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        response = client.delete(
            f"/campaigns/{sample_campaign.id}/challenges/{sample_challenge.id}/input-cache",
            params={
                "participant_id": "specific_player",
                "participant_type": "player"
            },
            headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["invalidated_count"] == 1
        
        # Verify cache invalidation was called for specific participant
        mock_submission_repo.invalidate_input_cache.assert_called_once_with(
            challenge_id=sample_challenge.id,
            participant_id="specific_player",
            participant_type="player"
        )

    def test_generate_input_invalid_participant_type(self, client, api_headers, sample_campaign, sample_challenge, monkeypatch):
        """Test input generation with invalid participant type."""
        mock_session = AsyncMock()
        
        # Mock repositories
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        mock_challenge_repo = Mock()
        mock_challenge_repo.get_challenge_by_id = AsyncMock(return_value=sample_challenge)

        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read", "campaigns:write"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", lambda session: mock_campaign_repo)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", lambda session: mock_challenge_repo)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        response = client.post(
            f"/campaigns/{sample_campaign.id}/challenges/{sample_challenge.id}/generate-input",
            params={
                "participant_id": "test_player",
                "participant_type": "invalid_type"
            },
            headers=api_headers
        )

        assert response.status_code == 422  # Validation error

    def test_generate_input_challenge_not_found(self, client, api_headers, sample_campaign, monkeypatch):
        """Test input generation when challenge doesn't exist."""
        mock_session = AsyncMock()
        
        # Mock repositories
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        mock_challenge_repo = Mock()
        mock_challenge_repo.get_challenge_by_id = AsyncMock(return_value=None)

        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read", "campaigns:write"]
        mock_api_key.is_active = True

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", lambda session: mock_campaign_repo)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", lambda session: mock_challenge_repo)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        nonexistent_challenge_id = uuid.uuid4()
        response = client.post(
            f"/campaigns/{sample_campaign.id}/challenges/{nonexistent_challenge_id}/generate-input",
            params={
                "participant_id": "test_player",
                "participant_type": "player"
            },
            headers=api_headers
        )

        assert response.status_code == 404