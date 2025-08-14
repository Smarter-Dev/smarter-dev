"""Tests for submission API endpoints.

This module tests the submission management REST API endpoints including
challenge submission processing and submission retrieval.
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
from smarter_dev.web.models import Campaign, Challenge, ChallengeSubmission, APIKey
from web.repositories.campaign_repository import CampaignRepository
from web.repositories.challenge_repository import ChallengeRepository
from web.repositories.submission_repository import SubmissionRepository


class TestSubmissionAPI:
    """Test suite for submission API endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(api)

    @pytest.fixture
    def api_headers(self):
        """Create API headers with valid key and user ID."""
        return {
            "X-API-Key": "test_api_key",
            "X-User-ID": "123456789012345678",
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
            generation_script="print('42')",
            order_position=1,
            categories=["math"],
            difficulty_level=5
        )

    @pytest.fixture
    def sample_submission(self, sample_challenge):
        """Sample submission model."""
        return ChallengeSubmission(
            id=uuid.uuid4(),
            challenge_id=sample_challenge.id,
            participant_id="123456789012345678",
            participant_type="player",
            submitted_result="42",
            is_correct=True,
            points_awarded=100,
            submission_timestamp=datetime.now(timezone.utc)
        )

    def test_submit_challenge_answer_success(self, client, api_headers, sample_campaign, sample_challenge, monkeypatch):
        """Test successful challenge answer submission."""
        # Mock all the dependencies
        mock_session = AsyncMock()
        
        # Mock repositories
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        
        mock_challenge_repo = Mock()  
        mock_challenge_repo.get_challenge_by_id = AsyncMock(return_value=sample_challenge)
        
        mock_submission_repo = Mock()
        mock_submission_repo.create_submission = AsyncMock(return_value=ChallengeSubmission(
            id=uuid.uuid4(),
            challenge_id=sample_challenge.id,
            participant_id="123456789012345678",
            participant_type="player",
            submitted_result="42",
            is_correct=True,
            points_awarded=100,
            submission_timestamp=datetime.now(timezone.utc)
        ))
        mock_submission_repo.get_submissions_by_challenge = AsyncMock(return_value=[])

        # Mock services
        mock_rate_limiter = Mock()
        mock_rate_limiter.check_submission_limit = AsyncMock()
        mock_rate_limiter.record_submission = AsyncMock()
        
        mock_input_generator = Mock()
        mock_generation_result = Mock()
        mock_generation_result.success = True
        mock_generation_result.output = "42"
        mock_input_generator.generate_inputs = AsyncMock(return_value=mock_generation_result)
        
        mock_validator = Mock()
        mock_validation_result = Mock()
        mock_validation_result.is_correct = True
        mock_validator.validate_submission = AsyncMock(return_value=mock_validation_result)
        
        mock_scoring_strategy = Mock()
        mock_scoring_result = Mock()
        mock_scoring_result.points = 100
        mock_scoring_strategy.calculate_score = Mock(return_value=mock_scoring_result)

        # Setup monkeypatches
        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_challenge_repo_init(session):
            return mock_challenge_repo  
        def mock_submission_repo_init(session):
            return mock_submission_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", mock_challenge_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read", "campaigns:write"]
        mock_api_key.is_active = True
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        
        # Mock services
        mock_release_service = Mock()
        mock_release_service.is_challenge_released = AsyncMock(return_value=True)
        
        monkeypatch.setattr("web.services.RateLimitingService", lambda: mock_rate_limiter)
        monkeypatch.setattr("web.services.InputGenerationService", lambda: mock_input_generator)
        monkeypatch.setattr("web.services.SubmissionValidationService", lambda: mock_validator)
        monkeypatch.setattr("web.services.create_scoring_strategy", lambda x, y: mock_scoring_strategy)
        monkeypatch.setattr("web.services.ChallengeReleaseService", lambda: mock_release_service)

        submission_data = {"submitted_result": "42"}
        
        response = client.post(
            f"/campaigns/{sample_campaign.id}/challenges/{sample_challenge.id}/submit",
            json=submission_data,
            headers=api_headers
        )

        if response.status_code != 201:
            print(f"Response status: {response.status_code}")
            print(f"Response content: {response.text}")
        assert response.status_code == 201
        data = response.json()
        assert data["submitted_result"] == "42"
        assert data["is_correct"] is True
        assert data["points_awarded"] == 100
        
        # Verify all the service calls were made
        mock_campaign_repo.get_campaign_by_id.assert_called_once()
        mock_challenge_repo.get_challenge_by_id.assert_called_once()
        mock_rate_limiter.check_submission_limit.assert_called_once()
        mock_input_generator.generate_inputs.assert_called_once()
        mock_validator.validate_submission.assert_called_once()
        mock_submission_repo.create_submission.assert_called_once()

    def test_submit_challenge_answer_missing_user_id(self, client, sample_campaign, sample_challenge):
        """Test submission fails without user ID in headers."""
        headers = {
            "X-API-Key": "test_api_key",
            "Content-Type": "application/json"
        }
        
        response = client.post(
            f"/campaigns/{sample_campaign.id}/challenges/{sample_challenge.id}/submit",
            json={"submitted_result": "42"},
            headers=headers
        )

        assert response.status_code == 422  # Validation error

    def test_submit_unreleased_challenge(self, client, api_headers, sample_campaign, sample_challenge, monkeypatch):
        """Test submission fails for unreleased challenge."""
        # Make campaign start in the future so challenge is unreleased
        sample_campaign.start_date = datetime.now(timezone.utc) + timedelta(hours=1)
        
        mock_session = AsyncMock()
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        mock_challenge_repo = Mock()
        mock_challenge_repo.get_challenge_by_id = AsyncMock(return_value=sample_challenge)
        
        # Mock release service to return False
        mock_release_service = Mock()
        mock_release_service.is_challenge_released = AsyncMock(return_value=False)
        
        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_challenge_repo_init(session):
            return mock_challenge_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", mock_challenge_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read", "campaigns:write"]
        mock_api_key.is_active = True
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        monkeypatch.setattr("web.services.ChallengeReleaseService", lambda: mock_release_service)

        response = client.post(
            f"/campaigns/{sample_campaign.id}/challenges/{sample_challenge.id}/submit",
            json={"submitted_result": "42"},
            headers=api_headers
        )

        assert response.status_code == 422  # Validation error

    def test_get_campaign_submissions_success(self, client, api_headers, sample_campaign, sample_submission, monkeypatch):
        """Test successful campaign submissions retrieval."""
        mock_session = AsyncMock()
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        mock_submission_repo = Mock()
        mock_submission_repo.get_campaign_submissions = AsyncMock(return_value=[sample_submission])

        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_submission_repo_init(session):
            return mock_submission_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read", "campaigns:write"]
        mock_api_key.is_active = True
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)

        response = client.get(f"/campaigns/{sample_campaign.id}/submissions", headers=api_headers)

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["submitted_result"] == "42"
        assert data[0]["is_correct"] is True

    def test_get_campaign_submissions_with_filters(self, client, api_headers, sample_campaign, monkeypatch):
        """Test campaign submissions retrieval with filters."""
        mock_session = AsyncMock()
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        mock_submission_repo = Mock()
        mock_submission_repo.get_campaign_submissions = AsyncMock(return_value=[])

        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_submission_repo_init(session):
            return mock_submission_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read", "campaigns:write"]
        mock_api_key.is_active = True
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)

        response = client.get(
            f"/campaigns/{sample_campaign.id}/submissions?participant_id=user123&correct_only=true&limit=10",
            headers=api_headers
        )

        assert response.status_code == 200
        mock_submission_repo.get_campaign_submissions.assert_called_once_with(
            campaign_id=sample_campaign.id,
            participant_id="user123",
            challenge_id=None,
            correct_only=True,
            limit=10
        )

    def test_get_challenge_submissions_success(self, client, api_headers, sample_campaign, sample_challenge, sample_submission, monkeypatch):
        """Test successful challenge submissions retrieval."""
        mock_session = AsyncMock()
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        mock_challenge_repo = Mock()
        mock_challenge_repo.get_challenge_by_id = AsyncMock(return_value=sample_challenge)
        mock_submission_repo = Mock()
        mock_submission_repo.get_submissions_by_challenge = AsyncMock(return_value=[sample_submission])

        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_challenge_repo_init(session):
            return mock_challenge_repo
        def mock_submission_repo_init(session):
            return mock_submission_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", mock_challenge_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.SubmissionRepository", mock_submission_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read", "campaigns:write"]
        mock_api_key.is_active = True
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)

        response = client.get(
            f"/campaigns/{sample_campaign.id}/challenges/{sample_challenge.id}/submissions",
            headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["submitted_result"] == "42"

    def test_rate_limit_exceeded(self, client, api_headers, sample_campaign, sample_challenge, monkeypatch):
        """Test submission fails when rate limit is exceeded."""
        mock_session = AsyncMock()
        mock_campaign_repo = Mock()
        mock_campaign_repo.get_campaign_by_id = AsyncMock(return_value=sample_campaign)
        mock_challenge_repo = Mock()
        mock_challenge_repo.get_challenge_by_id = AsyncMock(return_value=sample_challenge)
        
        # Mock rate limiter to raise an exception
        mock_rate_limiter = Mock()
        mock_rate_limiter.check_submission_limit = AsyncMock(side_effect=Exception("Rate limit exceeded"))

        # Mock release service to return True (released)
        mock_release_service = Mock()
        mock_release_service.is_challenge_released = AsyncMock(return_value=True)

        def mock_campaign_repo_init(session):
            return mock_campaign_repo
        def mock_challenge_repo_init(session):
            return mock_challenge_repo

        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.CampaignRepository", mock_campaign_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.ChallengeRepository", mock_challenge_repo_init)
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.get_database_session", lambda: mock_session)
        # Create mock API key
        mock_api_key = Mock()
        mock_api_key.name = "test_key"
        mock_api_key.scopes = ["campaigns:read", "campaigns:write"]
        mock_api_key.is_active = True
        monkeypatch.setattr("smarter_dev.web.api.routers.campaigns.verify_api_key", lambda x, y, z: mock_api_key)
        monkeypatch.setattr("web.services.RateLimitingService", lambda: mock_rate_limiter)
        monkeypatch.setattr("web.services.ChallengeReleaseService", lambda: mock_release_service)

        response = client.post(
            f"/campaigns/{sample_campaign.id}/challenges/{sample_challenge.id}/submit",
            json={"submitted_result": "42"},
            headers=api_headers
        )

        assert response.status_code == 429  # Too Many Requests