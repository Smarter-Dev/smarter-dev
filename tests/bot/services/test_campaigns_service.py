"""Tests for the campaigns service."""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import Mock, AsyncMock

from smarter_dev.bot.services.campaigns_service import (
    CampaignsService, 
    CampaignInfo, 
    ChallengeInfo,
    SubmissionInfo,
    CampaignStatsInfo
)
from smarter_dev.bot.services.exceptions import ServiceError


class TestCampaignsService:
    """Test suite for campaigns service."""

    @pytest.fixture
    def mock_api_client(self):
        """Create a mock API client."""
        client = Mock()
        client.get = AsyncMock()
        client.post = AsyncMock()
        return client

    @pytest.fixture
    def campaigns_service(self, mock_api_client):
        """Create campaigns service instance."""
        return CampaignsService(mock_api_client, cache_manager=None)

    @pytest.fixture
    def sample_campaign_data(self):
        """Sample campaign data."""
        return {
            "id": str(uuid.uuid4()),
            "title": "Test Campaign",
            "description": "A test campaign",
            "guild_id": "123456789012345678",
            "participant_type": "player",
            "status": "active",
            "start_date": "2025-01-01T00:00:00Z",
            "end_date": "2025-01-31T23:59:59Z",
            "challenge_release_delay_hours": 24,
            "scoring_strategy": "time_based",
            "scoring_config": {"positions": {"first": 100}},
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z"
        }

    @pytest.fixture
    def sample_challenge_data(self):
        """Sample challenge data."""
        return {
            "id": str(uuid.uuid4()),
            "campaign_id": str(uuid.uuid4()),
            "title": "Test Challenge",
            "description": "A test challenge",
            "difficulty": "medium",
            "problem_statement": "Solve this problem",
            "expected_output_format": "Single line output",
            "time_limit_minutes": 60,
            "memory_limit_mb": 256,
            "order_index": 1,
            "release_date": "2025-01-01T12:00:00Z",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z"
        }

    async def test_initialize_success(self, campaigns_service, mock_api_client):
        """Test successful service initialization."""
        await campaigns_service.initialize()
        # Should not raise any exceptions

    async def test_health_check_healthy(self, campaigns_service, mock_api_client):
        """Test health check when service is healthy."""
        mock_api_client.get.return_value = {"items": []}
        
        health = await campaigns_service.health_check()
        
        assert health.service_name == "campaigns"
        assert health.is_healthy is True
        assert health.details["api_connection"] == "ok"
        mock_api_client.get.assert_called_once_with("/campaigns/", params={"size": 1})

    async def test_health_check_unhealthy(self, campaigns_service, mock_api_client):
        """Test health check when service is unhealthy."""
        mock_api_client.get.side_effect = Exception("Connection failed")
        
        health = await campaigns_service.health_check()
        
        assert health.service_name == "campaigns"
        assert health.is_healthy is False
        assert "Connection failed" in health.details["error"]

    async def test_list_campaigns_success(self, campaigns_service, mock_api_client, sample_campaign_data):
        """Test successful campaign listing."""
        mock_api_client.get.return_value = {
            "items": [sample_campaign_data],
            "total": 1,
            "page": 1,
            "pages": 1
        }
        
        campaigns = await campaigns_service.list_campaigns("123456789012345678")
        
        assert len(campaigns) == 1
        campaign = campaigns[0]
        assert isinstance(campaign, CampaignInfo)
        assert campaign.title == "Test Campaign"
        assert campaign.participant_type == "player"
        assert campaign.status == "active"
        
        mock_api_client.get.assert_called_once_with(
            "/campaigns/", 
            params={
                "guild_id": "123456789012345678",
                "page": 1,
                "size": 20
            }
        )

    async def test_list_campaigns_with_filters(self, campaigns_service, mock_api_client):
        """Test campaign listing with filters."""
        mock_api_client.get.return_value = {"items": []}
        
        await campaigns_service.list_campaigns(
            "123456789012345678",
            status="active",
            participant_type="squad",
            page=2,
            size=10
        )
        
        mock_api_client.get.assert_called_once_with(
            "/campaigns/", 
            params={
                "guild_id": "123456789012345678",
                "page": 2,
                "size": 10,
                "status": "active",
                "participant_type": "squad"
            }
        )

    async def test_get_campaign_success(self, campaigns_service, mock_api_client, sample_campaign_data):
        """Test successful campaign retrieval."""
        campaign_id = sample_campaign_data["id"]
        mock_api_client.get.return_value = sample_campaign_data
        
        campaign = await campaigns_service.get_campaign(campaign_id)
        
        assert campaign is not None
        assert isinstance(campaign, CampaignInfo)
        assert campaign.title == "Test Campaign"
        assert str(campaign.id) == campaign_id
        
        mock_api_client.get.assert_called_once_with(f"/campaigns/{campaign_id}")

    async def test_get_campaign_not_found(self, campaigns_service, mock_api_client):
        """Test campaign retrieval when not found."""
        mock_api_client.get.side_effect = Exception("not found")
        
        campaign = await campaigns_service.get_campaign("nonexistent")
        
        assert campaign is None

    async def test_list_challenges_success(self, campaigns_service, mock_api_client, sample_challenge_data):
        """Test successful challenge listing."""
        campaign_id = "test-campaign-id"
        mock_api_client.get.return_value = [sample_challenge_data]
        
        challenges = await campaigns_service.list_challenges(campaign_id)
        
        assert len(challenges) == 1
        challenge = challenges[0]
        assert isinstance(challenge, ChallengeInfo)
        assert challenge.title == "Test Challenge"
        assert challenge.difficulty == "medium"
        
        mock_api_client.get.assert_called_once_with(
            f"/campaigns/{campaign_id}/challenges",
            params={"include_unreleased": False}
        )

    async def test_submit_challenge_answer_correct(self, campaigns_service, mock_api_client):
        """Test successful challenge submission with correct answer."""
        submission_data = {
            "id": str(uuid.uuid4()),
            "challenge_id": str(uuid.uuid4()),
            "participant_id": "123456789012345678",
            "participant_type": "player",
            "submitted_result": "42",
            "is_correct": True,
            "points_awarded": 100,
            "submission_timestamp": "2025-01-01T12:30:00Z"
        }
        
        mock_api_client.post.return_value = submission_data
        
        submission = await campaigns_service.submit_challenge_answer(
            campaign_id="campaign-id",
            challenge_id="challenge-id", 
            participant_id="123456789012345678",
            submitted_result="42"
        )
        
        assert isinstance(submission, SubmissionInfo)
        assert submission.is_correct is True
        assert submission.points_awarded == 100
        assert submission.submitted_result == "42"
        
        mock_api_client.post.assert_called_once_with(
            "/campaigns/campaign-id/challenges/challenge-id/submit",
            json={"submitted_result": "42"},
            headers={"X-User-ID": "123456789012345678"}
        )

    async def test_get_campaign_stats_success(self, campaigns_service, mock_api_client):
        """Test successful campaign statistics retrieval."""
        stats_data = {
            "campaign_id": str(uuid.uuid4()),
            "total_challenges": 5,
            "released_challenges": 3,
            "total_participants": 10,
            "total_submissions": 25,
            "correct_submissions": 15,
            "success_rate": 60.0,
            "avg_points_per_participant": 75.5,
            "top_participants": [{"participant_id": "user123", "points": 100}],
            "challenge_completion_rates": [{"challenge_id": "challenge123", "completion_rate": 80.0}]
        }
        
        campaign_id = stats_data["campaign_id"]
        mock_api_client.get.return_value = stats_data
        
        stats = await campaigns_service.get_campaign_stats(campaign_id)
        
        assert isinstance(stats, CampaignStatsInfo)
        assert stats.total_challenges == 5
        assert stats.released_challenges == 3
        assert stats.success_rate == 60.0
        assert len(stats.top_participants) == 1
        
        mock_api_client.get.assert_called_once_with(f"/campaigns/{campaign_id}/stats")

    async def test_service_error_handling(self, campaigns_service, mock_api_client):
        """Test service error handling."""
        mock_api_client.get.side_effect = Exception("API error")
        
        with pytest.raises(ServiceError, match="Failed to retrieve campaigns"):
            await campaigns_service.list_campaigns("123456789012345678")