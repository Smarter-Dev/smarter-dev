"""
Squad Health Service - Following SOLID principles.

This service implements ISP by providing segregated interfaces for different concerns:
- Health calculation
- Activity analysis
- Health reporting

Following SRP: Each method has a single responsibility.
Following DIP: Depends on repository abstraction.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter
import statistics
import structlog

from web.repositories.squad_activity_repository import SquadActivityRepository

logger = structlog.get_logger()


class SquadHealthService:
    """
    Service for calculating squad health metrics and analyzing activity patterns.
    
    Implements multiple interfaces following ISP:
    - IHealthCalculator: Health score calculations
    - IActivityAnalyzer: Activity trend analysis  
    - IHealthReporter: Health reporting and recommendations
    """
    
    def __init__(self, activity_repository: SquadActivityRepository):
        """Initialize service with repository dependency injection (DIP)."""
        self.activity_repository = activity_repository
        self._cache = {}  # Simple caching for performance
        self._cache_ttl = 300  # 5 minutes cache TTL
    
    # IHealthCalculator interface implementation
    async def calculate_squad_health_score(
        self,
        squad_id: str,
        days_to_analyze: int = 30
    ) -> float:
        """
        Calculate overall health score for a squad.
        
        Health score is based on:
        - Activity frequency and consistency
        - Member engagement diversity
        - Activity type quality (joins vs leaves)
        - Trend direction
        
        Returns:
            Float between 0.0 (very unhealthy) and 1.0 (very healthy)
        """
        cache_key = f"health_score:{squad_id}:{days_to_analyze}"
        cached_result = self._get_cached_result(cache_key)
        if cached_result is not None:
            return cached_result
        
        try:
            # Get activities for the analysis period
            activities = await self.activity_repository.get_activities_by_squad(
                squad_id=squad_id,
                limit=1000,  # Large enough for comprehensive analysis
                offset=0
            )
            
            # Filter activities to analysis period
            cutoff_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_to_analyze)
            recent_activities = [
                activity for activity in activities
                if activity.created_at >= cutoff_date
            ]
            
            if not recent_activities:
                self._cache_result(cache_key, 0.0)
                return 0.0
            
            # Calculate component scores
            activity_frequency_score = self._calculate_activity_frequency_score(recent_activities, days_to_analyze)
            member_diversity_score = self._calculate_member_diversity_score(recent_activities)
            activity_quality_score = self._calculate_activity_quality_score(recent_activities)
            consistency_score = self._calculate_consistency_score(recent_activities, days_to_analyze)
            
            # Weight the components
            health_score = (
                activity_frequency_score * 0.3 +
                member_diversity_score * 0.25 +
                activity_quality_score * 0.25 +
                consistency_score * 0.2
            )
            
            # Ensure score is between 0.0 and 1.0
            health_score = max(0.0, min(1.0, health_score))
            
            self._cache_result(cache_key, health_score)
            
            logger.info(
                "Squad health score calculated",
                squad_id=squad_id,
                health_score=health_score,
                activity_count=len(recent_activities),
                days_analyzed=days_to_analyze
            )
            
            return health_score
            
        except Exception as e:
            logger.error("Failed to calculate squad health score", error=str(e), squad_id=squad_id)
            raise
    
    async def calculate_engagement_score(
        self,
        squad_id: str,
        days_to_analyze: int = 7
    ) -> float:
        """
        Calculate engagement score based on recent activity.
        
        Engagement score focuses on:
        - Recent activity volume
        - Active member count
        - Activity recency
        - Interaction quality
        
        Returns:
            Float between 0.0 (no engagement) and 1.0 (high engagement)
        """
        cache_key = f"engagement_score:{squad_id}:{days_to_analyze}"
        cached_result = self._get_cached_result(cache_key)
        if cached_result is not None:
            return cached_result
        
        try:
            # Get recent activities
            recent_activities = await self.activity_repository.get_recent_activities(
                guild_id="",  # Will be filtered by squad_id
                hours=days_to_analyze * 24,
                limit=500
            )
            
            # Filter to this squad
            squad_activities = [
                activity for activity in recent_activities
                if str(activity.squad_id) == squad_id
            ]
            
            if not squad_activities:
                self._cache_result(cache_key, 0.0)
                return 0.0
            
            # Calculate engagement metrics
            activity_volume_score = min(1.0, len(squad_activities) / (days_to_analyze * 5))  # 5 activities per day = max score
            
            unique_users = len(set(activity.user_id for activity in squad_activities))
            user_diversity_score = min(1.0, unique_users / 5)  # 5+ active users = max score
            
            # Recency score - more recent activities score higher
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            recency_scores = []
            for activity in squad_activities:
                hours_ago = (now - activity.created_at).total_seconds() / 3600
                recency_score = max(0.0, 1.0 - (hours_ago / (days_to_analyze * 24)))
                recency_scores.append(recency_score)
            
            avg_recency_score = statistics.mean(recency_scores) if recency_scores else 0.0
            
            # Quality score based on activity types
            quality_score = self._calculate_activity_quality_score(squad_activities)
            
            # Combined engagement score
            engagement_score = (
                activity_volume_score * 0.3 +
                user_diversity_score * 0.3 +
                avg_recency_score * 0.2 +
                quality_score * 0.2
            )
            
            engagement_score = max(0.0, min(1.0, engagement_score))
            
            self._cache_result(cache_key, engagement_score)
            
            logger.info(
                "Engagement score calculated",
                squad_id=squad_id,
                engagement_score=engagement_score,
                recent_activities=len(squad_activities),
                unique_users=unique_users
            )
            
            return engagement_score
            
        except Exception as e:
            logger.error("Failed to calculate engagement score", error=str(e), squad_id=squad_id)
            raise
    
    # IActivityAnalyzer interface implementation
    async def analyze_activity_trends(
        self,
        squad_id: str,
        days_to_analyze: int = 30
    ) -> Dict[str, Any]:
        """
        Analyze activity trends for a squad.
        
        Returns:
            Dictionary with trend analysis including direction, growth rate, patterns
        """
        try:
            activities = await self.activity_repository.get_activities_by_squad(
                squad_id=squad_id,
                limit=1000,
                offset=0
            )
            
            # Filter to analysis period
            cutoff_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_to_analyze)
            recent_activities = [
                activity for activity in activities
                if activity.created_at >= cutoff_date
            ]
            
            if not recent_activities:
                return {
                    "trend_direction": "stable",
                    "growth_rate": 0.0,
                    "weekly_activity": {},
                    "confidence": 0.0
                }
            
            # Group activities by week
            weekly_activity = defaultdict(int)
            for activity in recent_activities:
                week_key = activity.created_at.strftime("%Y-W%U")
                weekly_activity[week_key] += 1
            
            # Calculate trend
            sorted_weeks = sorted(weekly_activity.items())
            if len(sorted_weeks) < 2:
                return {
                    "trend_direction": "stable",
                    "growth_rate": 0.0,
                    "weekly_activity": dict(weekly_activity),
                    "confidence": 0.0
                }
            
            # Calculate linear trend
            week_values = [count for _, count in sorted_weeks]
            
            # Simple trend calculation
            first_half_avg = statistics.mean(week_values[:len(week_values)//2])
            second_half_avg = statistics.mean(week_values[len(week_values)//2:])
            
            growth_rate = (second_half_avg - first_half_avg) / max(first_half_avg, 1)
            
            if growth_rate > 0.1:
                trend_direction = "increasing"
            elif growth_rate < -0.1:
                trend_direction = "decreasing"
            else:
                trend_direction = "stable"
            
            # Calculate confidence based on data consistency
            variance = statistics.variance(week_values) if len(week_values) > 1 else 0
            confidence = max(0.0, min(1.0, 1.0 - (variance / max(statistics.mean(week_values), 1))))
            
            return {
                "trend_direction": trend_direction,
                "growth_rate": round(growth_rate, 3),
                "weekly_activity": dict(weekly_activity),
                "confidence": round(confidence, 3),
                "total_activities": len(recent_activities),
                "analysis_period_days": days_to_analyze
            }
            
        except Exception as e:
            logger.error("Failed to analyze activity trends", error=str(e), squad_id=squad_id)
            raise
    
    async def get_activity_patterns(
        self,
        squad_id: str,
        pattern_type: str = "daily"
    ) -> Dict[str, Any]:
        """
        Get activity patterns (daily, weekly, etc.).
        
        Args:
            squad_id: Squad ID to analyze
            pattern_type: Type of pattern analysis ("daily" or "weekly")
            
        Returns:
            Dictionary with pattern analysis
        """
        try:
            # Get last 30 days of activities for pattern analysis
            activities = await self.activity_repository.get_activities_by_squad(
                squad_id=squad_id,
                limit=1000,
                offset=0
            )
            
            cutoff_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
            recent_activities = [
                activity for activity in activities
                if activity.created_at >= cutoff_date
            ]
            
            if pattern_type == "daily":
                return self._analyze_daily_patterns(recent_activities)
            elif pattern_type == "weekly":
                return self._analyze_weekly_patterns(recent_activities)
            else:
                raise ValueError(f"Unsupported pattern type: {pattern_type}")
                
        except Exception as e:
            logger.error("Failed to get activity patterns", error=str(e), squad_id=squad_id)
            raise
    
    # IHealthReporter interface implementation
    async def generate_health_report(
        self,
        squad_id: str,
        include_trends: bool = True
    ) -> Dict[str, Any]:
        """
        Generate comprehensive health report.
        
        Returns:
            Dictionary with complete health analysis and recommendations
        """
        try:
            # Calculate core metrics
            health_score = await self.calculate_squad_health_score(squad_id)
            engagement_score = await self.calculate_engagement_score(squad_id)
            
            # Get activity summary
            activities = await self.activity_repository.get_activities_by_squad(
                squad_id=squad_id,
                limit=100,
                offset=0
            )
            
            activity_summary = self._generate_activity_summary(activities)
            
            report = {
                "health_score": health_score,
                "engagement_score": engagement_score,
                "activity_summary": activity_summary,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "squad_id": squad_id
            }
            
            # Include trends if requested
            if include_trends:
                trends = await self.analyze_activity_trends(squad_id)
                report["trends"] = trends
            
            # Generate recommendations
            recommendations = await self.get_health_recommendations(squad_id, health_score)
            report["recommendations"] = recommendations
            
            logger.info(
                "Health report generated",
                squad_id=squad_id,
                health_score=health_score,
                engagement_score=engagement_score
            )
            
            return report
            
        except Exception as e:
            logger.error("Failed to generate health report", error=str(e), squad_id=squad_id)
            raise
    
    async def get_health_recommendations(
        self,
        squad_id: str,
        health_score: float
    ) -> List[str]:
        """
        Get recommendations based on health score.
        
        Returns:
            List of actionable recommendations
        """
        try:
            recommendations = []
            
            if health_score >= 0.8:
                # High health score - maintenance recommendations
                recommendations.extend([
                    "Excellent squad health! Continue current engagement strategies.",
                    "Consider mentoring other squads with your successful practices.",
                    "Maintain regular activity patterns to sustain high engagement.",
                    "Keep encouraging diverse member participation."
                ])
                
            elif health_score >= 0.6:
                # Medium health score - targeted improvements
                recommendations.extend([
                    "Good squad health with room for optimization.",
                    "Focus on increasing member diversity and participation.",
                    "Consider organizing regular squad events or challenges.",
                    "Encourage more frequent communication among members.",
                    "Target specific activity types that boost engagement."
                ])
                
            elif health_score >= 0.4:
                # Low-medium health score - significant improvements needed
                recommendations.extend([
                    "Squad health needs attention and improvement.",
                    "Increase activity frequency through member incentives.",
                    "Address potential issues causing member disengagement.",
                    "Implement regular check-ins with squad members.",
                    "Consider restructuring squad activities or goals.",
                    "Boost communication and collaboration opportunities."
                ])
                
            else:
                # Low health score - urgent action required
                recommendations.extend([
                    "Urgent action required to improve squad health.",
                    "Investigate and address underlying causes of low engagement.",
                    "Consider squad restructuring or leadership changes.",
                    "Implement immediate member retention strategies.",
                    "Focus on rebuilding community and engagement from scratch.",
                    "Seek feedback from current and former members."
                ])
            
            # Add specific recommendations based on activity analysis
            activities = await self.activity_repository.get_recent_activities(
                guild_id="",  # Will be filtered by squad analysis
                hours=168,  # Last week
                limit=100
            )
            
            if len(activities) < 5:
                recommendations.append("Increase overall activity frequency - aim for daily engagement.")
            
            unique_users = len(set(activity.user_id for activity in activities))
            if unique_users < 3:
                recommendations.append("Encourage more members to participate actively.")
            
            return recommendations
            
        except Exception as e:
            logger.error("Failed to get health recommendations", error=str(e), squad_id=squad_id)
            return ["Unable to generate recommendations due to analysis error."]
    
    # Private helper methods for calculations
    def _calculate_activity_frequency_score(self, activities: List, days: int) -> float:
        """Calculate score based on activity frequency."""
        if not activities:
            return 0.0
        
        activities_per_day = len(activities) / days
        # Normalize: 2+ activities per day = max score
        return min(1.0, activities_per_day / 2.0)
    
    def _calculate_member_diversity_score(self, activities: List) -> float:
        """Calculate score based on member participation diversity."""
        if not activities:
            return 0.0
        
        unique_users = len(set(activity.user_id for activity in activities))
        # Normalize: 5+ unique users = max score
        return min(1.0, unique_users / 5.0)
    
    def _calculate_activity_quality_score(self, activities: List) -> float:
        """Calculate score based on activity types (positive vs negative)."""
        if not activities:
            return 0.0
        
        positive_activities = ["squad_join", "message_sent", "event_participated", "role_assigned"]
        negative_activities = ["squad_leave", "user_timeout", "warning_issued"]
        
        positive_count = sum(1 for activity in activities if activity.activity_type in positive_activities)
        negative_count = sum(1 for activity in activities if activity.activity_type in negative_activities)
        
        if positive_count + negative_count == 0:
            return 0.5  # Neutral if no categorizable activities
        
        quality_ratio = positive_count / (positive_count + negative_count)
        return quality_ratio
    
    def _calculate_consistency_score(self, activities: List, days: int) -> float:
        """Calculate score based on activity consistency over time."""
        if not activities or days < 7:
            return 0.0
        
        # Group activities by day
        daily_counts = defaultdict(int)
        for activity in activities:
            day_key = activity.created_at.strftime("%Y-%m-%d")
            daily_counts[day_key] += 1
        
        if len(daily_counts) < 2:
            return 0.0
        
        # Calculate consistency using coefficient of variation
        counts = list(daily_counts.values())
        avg_count = statistics.mean(counts)
        
        if avg_count == 0:
            return 0.0
        
        std_dev = statistics.stdev(counts) if len(counts) > 1 else 0
        coefficient_of_variation = std_dev / avg_count
        
        # Invert so lower variation = higher score
        consistency_score = max(0.0, 1.0 - min(1.0, coefficient_of_variation))
        return consistency_score
    
    def _analyze_daily_patterns(self, activities: List) -> Dict[str, Any]:
        """Analyze daily activity patterns."""
        hourly_counts = defaultdict(int)
        daily_counts = defaultdict(int)
        
        for activity in activities:
            hour = activity.created_at.hour
            day = activity.created_at.strftime("%Y-%m-%d")
            
            hourly_counts[hour] += 1
            daily_counts[day] += 1
        
        # Find peak hours
        sorted_hours = sorted(hourly_counts.items(), key=lambda x: x[1], reverse=True)
        peak_hours = [hour for hour, count in sorted_hours[:3]]
        
        return {
            "hourly_distribution": dict(hourly_counts),
            "activity_by_day": dict(daily_counts),
            "peak_hours": peak_hours,
            "most_active_hour": sorted_hours[0][0] if sorted_hours else None,
            "total_active_days": len(daily_counts)
        }
    
    def _analyze_weekly_patterns(self, activities: List) -> Dict[str, Any]:
        """Analyze weekly activity patterns."""
        weekday_counts = defaultdict(int)
        weekend_count = 0
        weekday_count = 0
        
        for activity in activities:
            weekday = activity.created_at.weekday()  # 0=Monday, 6=Sunday
            weekday_counts[weekday] += 1
            
            if weekday >= 5:  # Saturday, Sunday
                weekend_count += 1
            else:
                weekday_count += 1
        
        # Convert weekday numbers to names
        weekday_names = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 
                        4: "Friday", 5: "Saturday", 6: "Sunday"}
        
        daily_distribution = {weekday_names[day]: count for day, count in weekday_counts.items()}
        
        # Find most active day
        most_active_day = max(weekday_counts.items(), key=lambda x: x[1]) if weekday_counts else None
        most_active_day_name = weekday_names[most_active_day[0]] if most_active_day else None
        
        return {
            "daily_distribution": daily_distribution,
            "weekday_vs_weekend": {
                "weekday_count": weekday_count,
                "weekend_count": weekend_count,
                "weekday_percentage": round(weekday_count / max(weekday_count + weekend_count, 1) * 100, 1)
            },
            "most_active_day": most_active_day_name,
            "total_activities": len(activities)
        }
    
    def _generate_activity_summary(self, activities: List) -> Dict[str, Any]:
        """Generate summary of recent activities."""
        if not activities:
            return {
                "total_activities": 0,
                "activity_types": {},
                "unique_users": 0,
                "most_recent": None
            }
        
        activity_types = Counter(activity.activity_type for activity in activities)
        unique_users = len(set(activity.user_id for activity in activities))
        most_recent = max(activities, key=lambda x: x.created_at)
        
        return {
            "total_activities": len(activities),
            "activity_types": dict(activity_types),
            "unique_users": unique_users,
            "most_recent": {
                "type": most_recent.activity_type,
                "timestamp": most_recent.created_at.isoformat(),
                "user_id": most_recent.user_id
            }
        }
    
    def _get_cached_result(self, cache_key: str) -> Optional[float]:
        """Get cached result if still valid."""
        if cache_key in self._cache:
            cached_time, result = self._cache[cache_key]
            if (datetime.now() - cached_time).total_seconds() < self._cache_ttl:
                return result
            else:
                del self._cache[cache_key]
        return None
    
    def _cache_result(self, cache_key: str, result: float) -> None:
        """Cache result with timestamp."""
        self._cache[cache_key] = (datetime.now(), result)