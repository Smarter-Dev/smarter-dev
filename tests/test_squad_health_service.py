"""
TDD Tests for Squad Health Service - Following SOLID principles.

Testing health metrics calculation and squad analytics.
Service should follow ISP - segregated interfaces for different concerns.
Tests MUST fail initially to enforce true TDD approach.
"""

import pytest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Protocol
from uuid import uuid4
from unittest.mock import Mock, AsyncMock

from web.repositories.squad_activity_repository import SquadActivityRepository


# Segregated interfaces following ISP
class IHealthCalculator(Protocol):
    """Interface for health score calculations only."""
    
    async def calculate_squad_health_score(
        self,
        squad_id: str,
        days_to_analyze: int = 30
    ) -> float:
        """Calculate overall health score for a squad."""
        ...
    
    async def calculate_engagement_score(
        self,
        squad_id: str,
        days_to_analyze: int = 7
    ) -> float:
        """Calculate engagement score based on recent activity."""
        ...


class IActivityAnalyzer(Protocol):
    """Interface for activity analysis only."""
    
    async def analyze_activity_trends(
        self,
        squad_id: str,
        days_to_analyze: int = 30
    ) -> Dict[str, Any]:
        """Analyze activity trends for a squad."""
        ...
    
    async def get_activity_patterns(
        self,
        squad_id: str,
        pattern_type: str = "daily"
    ) -> Dict[str, Any]:
        """Get activity patterns (daily, weekly, etc.)."""
        ...


class IHealthReporter(Protocol):
    """Interface for health reporting only."""
    
    async def generate_health_report(
        self,
        squad_id: str,
        include_trends: bool = True
    ) -> Dict[str, Any]:
        """Generate comprehensive health report."""
        ...
    
    async def get_health_recommendations(
        self,
        squad_id: str,
        health_score: float
    ) -> List[str]:
        """Get recommendations based on health score."""
        ...


@pytest.fixture
def mock_activity_repository():
    """Mock activity repository for testing."""
    mock_repo = AsyncMock(spec=SquadActivityRepository)
    return mock_repo


@pytest.fixture
def sample_squad_data():
    """Sample squad data for testing."""
    return {
        "id": str(uuid4()),
        "guild_id": "123456789",
        "name": "Test Squad",
        "member_count": 10,
        "created_at": datetime.now(timezone.utc) - timedelta(days=60)
    }


@pytest.fixture
def sample_activities_data():
    """Sample activities data for testing."""
    activities = []
    base_time = datetime.now(timezone.utc).replace(tzinfo=None)
    
    # Create varied activity patterns for testing
    for i in range(30):
        # Simulate declining activity over time
        activity_count = max(1, 10 - i // 5)
        
        for j in range(activity_count):
            activities.append({
                "guild_id": "123456789",
                "squad_id": str(uuid4()),
                "user_id": f"user_{j % 5}",  # 5 different users
                "activity_type": "squad_join" if j % 2 == 0 else "message_sent",
                "created_at": base_time - timedelta(days=i),
                "metadata": {"engagement_score": 5 - (i // 10)}
            })
    
    return activities


class TestSquadHealthCalculator:
    """Test health calculation interface following ISP."""
    
    @pytest.mark.asyncio
    async def test_calculate_squad_health_score_basic(self, mock_activity_repository, sample_squad_data):
        """Test basic health score calculation."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock activity data for last 30 days
        mock_activities = [
            Mock(
                activity_type="squad_join",
                created_at=datetime.now(timezone.utc) - timedelta(days=i),
                metadata={"engagement": "high"}
            )
            for i in range(10)
        ]
        
        mock_activity_repository.get_activities_by_squad.return_value = mock_activities
        mock_activity_repository.get_activity_count_by_type.return_value = 10
        
        health_service = SquadHealthService(mock_activity_repository)
        
        health_score = await health_service.calculate_squad_health_score(
            squad_id=sample_squad_data["id"],
            days_to_analyze=30
        )
        
        # Health score should be between 0.0 and 1.0
        assert 0.0 <= health_score <= 1.0
        assert isinstance(health_score, float)

    @pytest.mark.asyncio
    async def test_calculate_health_score_with_no_activity(self, mock_activity_repository, sample_squad_data):
        """Test health score calculation with no recent activity."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock no activities
        mock_activity_repository.get_activities_by_squad.return_value = []
        mock_activity_repository.get_activity_count_by_type.return_value = 0
        
        health_service = SquadHealthService(mock_activity_repository)
        
        health_score = await health_service.calculate_squad_health_score(
            squad_id=sample_squad_data["id"],
            days_to_analyze=30
        )
        
        # No activity should result in low health score
        assert health_score <= 0.2
        assert health_score >= 0.0

    @pytest.mark.asyncio
    async def test_calculate_engagement_score_recent_activity(self, mock_activity_repository, sample_squad_data):
        """Test engagement score calculation based on recent activity."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock high recent activity
        recent_activities = [
            Mock(
                activity_type="message_sent",
                created_at=datetime.now(timezone.utc) - timedelta(hours=i),
                user_id=f"user_{i % 3}",  # 3 different users
                metadata={"message_length": 50}
            )
            for i in range(20)
        ]
        
        mock_activity_repository.get_recent_activities.return_value = recent_activities
        
        health_service = SquadHealthService(mock_activity_repository)
        
        engagement_score = await health_service.calculate_engagement_score(
            squad_id=sample_squad_data["id"],
            days_to_analyze=7
        )
        
        # High recent activity should result in high engagement
        assert 0.0 <= engagement_score <= 1.0
        assert engagement_score > 0.5  # Should be high with 20 recent activities

    @pytest.mark.asyncio
    async def test_calculate_engagement_score_low_activity(self, mock_activity_repository, sample_squad_data):
        """Test engagement score with low activity."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock low recent activity
        low_activities = [
            Mock(
                activity_type="squad_join",
                created_at=datetime.now(timezone.utc) - timedelta(days=5),
                user_id="user_1"
            )
        ]
        
        mock_activity_repository.get_recent_activities.return_value = low_activities
        
        health_service = SquadHealthService(mock_activity_repository)
        
        engagement_score = await health_service.calculate_engagement_score(
            squad_id=sample_squad_data["id"],
            days_to_analyze=7
        )
        
        # Low activity should result in low engagement
        assert 0.0 <= engagement_score <= 1.0
        assert engagement_score < 0.5

    @pytest.mark.asyncio
    async def test_health_score_considers_member_diversity(self, mock_activity_repository, sample_squad_data):
        """Test that health score considers diversity of active members."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock activities from diverse users
        diverse_activities = []
        for i in range(10):
            diverse_activities.append(Mock(
                activity_type="message_sent",
                created_at=datetime.now(timezone.utc) - timedelta(days=i),
                user_id=f"user_{i}",  # Different user each time
                metadata={}
            ))
        
        mock_activity_repository.get_activities_by_squad.return_value = diverse_activities
        mock_activity_repository.get_activity_count_by_type.return_value = 10
        
        health_service = SquadHealthService(mock_activity_repository)
        
        diverse_health = await health_service.calculate_squad_health_score(
            squad_id=sample_squad_data["id"],
            days_to_analyze=30
        )
        
        # Mock activities from few users (less diverse)
        concentrated_activities = []
        for i in range(10):
            concentrated_activities.append(Mock(
                activity_type="message_sent",
                created_at=datetime.now(timezone.utc) - timedelta(days=i),
                user_id="user_1",  # Same user every time
                metadata={}
            ))
        
        mock_activity_repository.get_activities_by_squad.return_value = concentrated_activities
        
        concentrated_health = await health_service.calculate_squad_health_score(
            squad_id=sample_squad_data["id"],
            days_to_analyze=30
        )
        
        # Diverse activity should result in higher health score
        assert diverse_health > concentrated_health

    @pytest.mark.asyncio
    async def test_health_score_considers_activity_types(self, mock_activity_repository, sample_squad_data):
        """Test that health score weighs different activity types appropriately."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock high-value activities (joins, engagement)
        high_value_activities = [
            Mock(
                activity_type="squad_join",
                created_at=datetime.now(timezone.utc) - timedelta(days=i),
                user_id=f"user_{i}",
                metadata={}
            )
            for i in range(5)
        ]
        
        mock_activity_repository.get_activities_by_squad.return_value = high_value_activities
        mock_activity_repository.get_activity_count_by_type.return_value = 5
        
        health_service = SquadHealthService(mock_activity_repository)
        
        high_value_health = await health_service.calculate_squad_health_score(
            squad_id=sample_squad_data["id"],
            days_to_analyze=30
        )
        
        # Mock low-value activities (leaves, inactivity)
        low_value_activities = [
            Mock(
                activity_type="squad_leave",
                created_at=datetime.now(timezone.utc) - timedelta(days=i),
                user_id=f"user_{i}",
                metadata={}
            )
            for i in range(5)
        ]
        
        mock_activity_repository.get_activities_by_squad.return_value = low_value_activities
        
        low_value_health = await health_service.calculate_squad_health_score(
            squad_id=sample_squad_data["id"],
            days_to_analyze=30
        )
        
        # High-value activities should result in higher health score
        assert high_value_health > low_value_health


class TestSquadActivityAnalyzer:
    """Test activity analysis interface following ISP."""
    
    @pytest.mark.asyncio
    async def test_analyze_activity_trends_growth(self, mock_activity_repository, sample_squad_data):
        """Test activity trend analysis showing growth."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock increasing activity over time
        trend_activities = []
        for week in range(4):
            activity_count = (week + 1) * 5  # Increasing activity
            for i in range(activity_count):
                trend_activities.append(Mock(
                    activity_type="message_sent",
                    created_at=datetime.now(timezone.utc) - timedelta(weeks=week, days=i),
                    user_id=f"user_{i % 3}",
                    metadata={}
                ))
        
        mock_activity_repository.get_activities_by_squad.return_value = trend_activities
        
        health_service = SquadHealthService(mock_activity_repository)
        
        trends = await health_service.analyze_activity_trends(
            squad_id=sample_squad_data["id"],
            days_to_analyze=30
        )
        
        assert "trend_direction" in trends
        assert "weekly_activity" in trends
        assert "growth_rate" in trends
        assert trends["trend_direction"] in ["increasing", "decreasing", "stable"]
        assert isinstance(trends["growth_rate"], (int, float))

    @pytest.mark.asyncio
    async def test_analyze_activity_trends_decline(self, mock_activity_repository, sample_squad_data):
        """Test activity trend analysis showing decline."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock decreasing activity over time
        trend_activities = []
        for week in range(4):
            activity_count = max(1, (4 - week) * 5)  # Decreasing activity
            for i in range(activity_count):
                trend_activities.append(Mock(
                    activity_type="message_sent",
                    created_at=datetime.now(timezone.utc) - timedelta(weeks=week, days=i),
                    user_id=f"user_{i % 3}",
                    metadata={}
                ))
        
        mock_activity_repository.get_activities_by_squad.return_value = trend_activities
        
        health_service = SquadHealthService(mock_activity_repository)
        
        trends = await health_service.analyze_activity_trends(
            squad_id=sample_squad_data["id"],
            days_to_analyze=30
        )
        
        assert trends["trend_direction"] == "decreasing"
        assert trends["growth_rate"] < 0

    @pytest.mark.asyncio
    async def test_get_activity_patterns_daily(self, mock_activity_repository, sample_squad_data):
        """Test daily activity pattern analysis."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock activities with daily patterns
        pattern_activities = []
        for day in range(7):
            for hour in [9, 12, 15, 18]:  # Peak hours
                pattern_activities.append(Mock(
                    activity_type="message_sent",
                    created_at=datetime.now(timezone.utc).replace(hour=hour) - timedelta(days=day),
                    user_id=f"user_{day % 3}",
                    metadata={}
                ))
        
        mock_activity_repository.get_activities_by_squad.return_value = pattern_activities
        
        health_service = SquadHealthService(mock_activity_repository)
        
        patterns = await health_service.get_activity_patterns(
            squad_id=sample_squad_data["id"],
            pattern_type="daily"
        )
        
        assert "hourly_distribution" in patterns
        assert "peak_hours" in patterns
        assert "activity_by_day" in patterns
        assert isinstance(patterns["hourly_distribution"], dict)
        assert isinstance(patterns["peak_hours"], list)

    @pytest.mark.asyncio
    async def test_get_activity_patterns_weekly(self, mock_activity_repository, sample_squad_data):
        """Test weekly activity pattern analysis."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock activities with weekly patterns
        pattern_activities = []
        for week in range(4):
            for day in [1, 2, 3, 4, 5]:  # Weekdays more active
                for _ in range(3):
                    pattern_activities.append(Mock(
                        activity_type="message_sent",
                        created_at=datetime.now(timezone.utc) - timedelta(weeks=week, days=day),
                        user_id=f"user_{week % 3}",
                        metadata={}
                    ))
        
        mock_activity_repository.get_activities_by_squad.return_value = pattern_activities
        
        health_service = SquadHealthService(mock_activity_repository)
        
        patterns = await health_service.get_activity_patterns(
            squad_id=sample_squad_data["id"],
            pattern_type="weekly"
        )
        
        assert "daily_distribution" in patterns
        assert "weekday_vs_weekend" in patterns
        assert "most_active_day" in patterns
        assert isinstance(patterns["daily_distribution"], dict)


class TestSquadHealthReporter:
    """Test health reporting interface following ISP."""
    
    @pytest.mark.asyncio
    async def test_generate_health_report_comprehensive(self, mock_activity_repository, sample_squad_data):
        """Test comprehensive health report generation."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock diverse activity data
        mock_activities = [
            Mock(
                activity_type="squad_join",
                created_at=datetime.now(timezone.utc) - timedelta(days=i),
                user_id=f"user_{i % 5}",
                metadata={"engagement": "high"}
            )
            for i in range(15)
        ]
        
        mock_activity_repository.get_activities_by_squad.return_value = mock_activities
        mock_activity_repository.get_recent_activities.return_value = mock_activities[:5]
        mock_activity_repository.get_activity_count_by_type.return_value = 15
        
        health_service = SquadHealthService(mock_activity_repository)
        
        report = await health_service.generate_health_report(
            squad_id=sample_squad_data["id"],
            include_trends=True
        )
        
        # Verify report structure
        assert "health_score" in report
        assert "engagement_score" in report
        assert "activity_summary" in report
        assert "trends" in report
        assert "recommendations" in report
        
        # Verify data types
        assert isinstance(report["health_score"], float)
        assert isinstance(report["engagement_score"], float)
        assert isinstance(report["activity_summary"], dict)
        assert isinstance(report["recommendations"], list)

    @pytest.mark.asyncio
    async def test_generate_health_report_without_trends(self, mock_activity_repository, sample_squad_data):
        """Test health report generation without trend analysis."""
        from web.services.squad_health_service import SquadHealthService
        
        mock_activities = [
            Mock(
                activity_type="message_sent",
                created_at=datetime.now(timezone.utc) - timedelta(days=i),
                user_id=f"user_{i % 3}",
                metadata={}
            )
            for i in range(10)
        ]
        
        mock_activity_repository.get_activities_by_squad.return_value = mock_activities
        mock_activity_repository.get_recent_activities.return_value = mock_activities[:3]
        mock_activity_repository.get_activity_count_by_type.return_value = 10
        
        health_service = SquadHealthService(mock_activity_repository)
        
        report = await health_service.generate_health_report(
            squad_id=sample_squad_data["id"],
            include_trends=False
        )
        
        assert "health_score" in report
        assert "engagement_score" in report
        assert "activity_summary" in report
        assert "trends" not in report or report["trends"] is None

    @pytest.mark.asyncio
    async def test_get_health_recommendations_high_score(self, mock_activity_repository, sample_squad_data):
        """Test recommendations for high health score."""
        from web.services.squad_health_service import SquadHealthService
        
        health_service = SquadHealthService(mock_activity_repository)
        
        recommendations = await health_service.get_health_recommendations(
            squad_id=sample_squad_data["id"],
            health_score=0.9
        )
        
        assert isinstance(recommendations, list)
        assert len(recommendations) > 0
        
        # High score should have maintenance recommendations
        rec_text = " ".join(recommendations).lower()
        assert any(word in rec_text for word in ["maintain", "continue", "excellent", "keep"])

    @pytest.mark.asyncio
    async def test_get_health_recommendations_low_score(self, mock_activity_repository, sample_squad_data):
        """Test recommendations for low health score."""
        from web.services.squad_health_service import SquadHealthService
        
        health_service = SquadHealthService(mock_activity_repository)
        
        recommendations = await health_service.get_health_recommendations(
            squad_id=sample_squad_data["id"],
            health_score=0.2
        )
        
        assert isinstance(recommendations, list)
        assert len(recommendations) > 0
        
        # Low score should have improvement recommendations
        rec_text = " ".join(recommendations).lower()
        assert any(word in rec_text for word in ["improve", "increase", "encourage", "boost"])

    @pytest.mark.asyncio
    async def test_get_health_recommendations_medium_score(self, mock_activity_repository, sample_squad_data):
        """Test recommendations for medium health score."""
        from web.services.squad_health_service import SquadHealthService
        
        health_service = SquadHealthService(mock_activity_repository)
        
        recommendations = await health_service.get_health_recommendations(
            squad_id=sample_squad_data["id"],
            health_score=0.6
        )
        
        assert isinstance(recommendations, list)
        assert len(recommendations) > 0
        
        # Medium score should have targeted improvement recommendations
        rec_text = " ".join(recommendations).lower()
        assert any(word in rec_text for word in ["optimize", "focus", "target", "enhance"])


class TestSquadHealthServiceIntegration:
    """Test service integration and SOLID compliance."""
    
    @pytest.mark.asyncio
    async def test_service_implements_all_interfaces(self, mock_activity_repository):
        """Test that service implements all segregated interfaces (ISP compliance)."""
        from web.services.squad_health_service import SquadHealthService
        
        service = SquadHealthService(mock_activity_repository)
        
        # Verify it implements all interfaces
        assert isinstance(service, IHealthCalculator)
        assert isinstance(service, IActivityAnalyzer)
        assert isinstance(service, IHealthReporter)
        
        # Verify all interface methods exist
        health_methods = ["calculate_squad_health_score", "calculate_engagement_score"]
        analyzer_methods = ["analyze_activity_trends", "get_activity_patterns"]
        reporter_methods = ["generate_health_report", "get_health_recommendations"]
        
        all_methods = health_methods + analyzer_methods + reporter_methods
        
        for method_name in all_methods:
            assert hasattr(service, method_name)
            assert callable(getattr(service, method_name))

    @pytest.mark.asyncio
    async def test_service_dependency_injection(self):
        """Test service follows DIP with dependency injection."""
        from web.services.squad_health_service import SquadHealthService
        
        # Should accept any repository that implements the interface
        mock_repo = AsyncMock()
        service = SquadHealthService(mock_repo)
        
        assert service.activity_repository is mock_repo

    @pytest.mark.asyncio
    async def test_service_error_handling(self, mock_activity_repository, sample_squad_data):
        """Test service handles repository errors gracefully."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock repository to raise exceptions
        mock_activity_repository.get_activities_by_squad.side_effect = Exception("Database error")
        
        service = SquadHealthService(mock_activity_repository)
        
        # Service should handle errors gracefully
        with pytest.raises(Exception):  # Or specific service exception
            await service.calculate_squad_health_score(sample_squad_data["id"])

    @pytest.mark.asyncio
    async def test_service_caching_behavior(self, mock_activity_repository, sample_squad_data):
        """Test service caching to avoid redundant calculations."""
        from web.services.squad_health_service import SquadHealthService
        
        mock_activities = [Mock(activity_type="test", created_at=datetime.now(), user_id="user1")]
        mock_activity_repository.get_activities_by_squad.return_value = mock_activities
        mock_activity_repository.get_activity_count_by_type.return_value = 1
        
        service = SquadHealthService(mock_activity_repository)
        
        # Calculate health score twice
        score1 = await service.calculate_squad_health_score(sample_squad_data["id"])
        score2 = await service.calculate_squad_health_score(sample_squad_data["id"])
        
        # Should be the same result
        assert score1 == score2
        
        # Repository should be called efficiently (implementation detail)
        # This tests caching if implemented

    @pytest.mark.asyncio
    async def test_service_performance_with_large_dataset(self, mock_activity_repository, sample_squad_data):
        """Test service performance with large activity datasets."""
        from web.services.squad_health_service import SquadHealthService
        
        # Mock large dataset
        large_activities = [
            Mock(
                activity_type="message_sent",
                created_at=datetime.now(timezone.utc) - timedelta(days=i // 10),
                user_id=f"user_{i % 100}",
                metadata={}
            )
            for i in range(1000)
        ]
        
        mock_activity_repository.get_activities_by_squad.return_value = large_activities
        mock_activity_repository.get_recent_activities.return_value = large_activities[:100]
        mock_activity_repository.get_activity_count_by_type.return_value = 1000
        
        service = SquadHealthService(mock_activity_repository)
        
        # Should handle large datasets efficiently
        start_time = datetime.now()
        health_score = await service.calculate_squad_health_score(sample_squad_data["id"])
        processing_time = (datetime.now() - start_time).total_seconds()
        
        assert 0.0 <= health_score <= 1.0
        assert processing_time < 5.0  # Should complete within reasonable time