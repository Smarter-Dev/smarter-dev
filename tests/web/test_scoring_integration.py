"""Integration tests for the new challenge scoring system."""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from smarter_dev.web.scoring import calculate_challenge_points


class TestScoringIntegration:
    """Integration tests for the scoring system."""
    
    def test_real_world_scenario_quick_solver(self):
        """Test a participant who solves quickly (30 minutes)."""
        # Campaign with 5 challenges, 24-hour cadence = 5 days total
        campaign_start = datetime(2025, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        num_challenges = 5
        cadence_hours = 24
        challenge_end = campaign_start + timedelta(hours=num_challenges * cadence_hours)
        
        # Participant requests input 2 hours after challenge released
        input_time = datetime(2025, 1, 2, 11, 0, 0, tzinfo=timezone.utc)
        # Solves in 30 minutes
        submission_time = input_time + timedelta(minutes=30)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        
        # Should get high points in logarithmic phase (steeper curve)
        assert points == 2612  # With steeper curve: 4096 - 2048 * [log2(1.5)]^0.6
        print(f"Quick solver (30 min): {points} points")
    
    def test_real_world_scenario_average_solver(self):
        """Test a participant who takes 3 hours to solve."""
        campaign_start = datetime(2025, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        num_challenges = 5
        cadence_hours = 24
        challenge_end = campaign_start + timedelta(hours=num_challenges * cadence_hours)
        
        # Participant requests input 1 day into campaign
        input_time = datetime(2025, 1, 2, 9, 0, 0, tzinfo=timezone.utc)
        # Takes 3 hours (1 hour into linear phase)
        submission_time = input_time + timedelta(hours=3)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        
        # 3 hours = 1 hour into linear phase, with 96 hours total (94 hours linear)
        # Points = 2048 * (93/94) ≈ 2026
        assert 2000 < points < 2050
        print(f"Average solver (3 hrs): {points} points")
    
    def test_real_world_scenario_late_starter(self):
        """Test a participant who starts very late in the campaign."""
        campaign_start = datetime(2025, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        num_challenges = 5
        cadence_hours = 24
        challenge_end = campaign_start + timedelta(hours=num_challenges * cadence_hours)
        
        # Requests input 2 hours before campaign ends
        input_time = challenge_end - timedelta(hours=2)
        # Solves in 1 hour
        submission_time = input_time + timedelta(hours=1)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        
        # Pure linear with only 2 hours total, solved halfway
        assert points == 2048
        print(f"Late starter (1 hr with 2 hrs left): {points} points")
    
    def test_campaign_with_short_cadence(self):
        """Test scoring with a 6-hour release cadence."""
        campaign_start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        num_challenges = 10
        cadence_hours = 6  # Short cadence
        challenge_end = campaign_start + timedelta(hours=num_challenges * cadence_hours)
        
        # Request input 12 hours into campaign
        input_time = campaign_start + timedelta(hours=12)
        # Solve in 90 minutes
        submission_time = input_time + timedelta(minutes=90)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        
        # Past logarithmic phase (90 min > 60 min), now in linear phase
        # 30 min into linear phase with 46.5 hours remaining
        assert 2000 < points < 2050
        print(f"Short cadence campaign (90 min): {points} points")
    
    def test_scoring_fairness_comparison(self):
        """Compare scoring fairness between fast and slow solvers."""
        campaign_start = datetime(2025, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        num_challenges = 7
        cadence_hours = 24
        challenge_end = campaign_start + timedelta(hours=num_challenges * cadence_hours)
        
        # Both start at same time
        input_time = datetime(2025, 1, 3, 9, 0, 0, tzinfo=timezone.utc)
        
        # Fast solver: 15 minutes
        fast_submission = input_time + timedelta(minutes=15)
        fast_points = calculate_challenge_points(input_time, fast_submission, challenge_end)
        
        # Medium solver: 1 hour
        medium_submission = input_time + timedelta(hours=1)
        medium_points = calculate_challenge_points(input_time, medium_submission, challenge_end)
        
        # Slow solver: 4 hours
        slow_submission = input_time + timedelta(hours=4)
        slow_points = calculate_challenge_points(input_time, slow_submission, challenge_end)
        
        print(f"\nScoring fairness comparison:")
        print(f"  Fast (15 min): {fast_points} points")
        print(f"  Medium (1 hr): {medium_points} points")
        print(f"  Slow (4 hrs): {slow_points} points")
        
        # Verify proper decay
        assert fast_points > medium_points > slow_points
        assert fast_points > 3000  # Still high reward for fast (steeper curve)
        assert slow_points > 1900   # Still significant points for slow
    
    def test_maximum_points_scenarios(self):
        """Test scenarios that should give maximum points."""
        challenge_end = datetime(2025, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        
        scenarios = [
            ("Instant submission", timedelta(0)),
            ("5 second submission", timedelta(seconds=5)),
            ("9 second submission", timedelta(seconds=9)),
        ]
        
        for description, delay in scenarios:
            input_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            submission_time = input_time + delay
            points = calculate_challenge_points(input_time, submission_time, challenge_end)
            assert points == 4096, f"{description} should give max points"
            print(f"{description}: {points} points")


if __name__ == "__main__":
    # Run tests directly for manual verification
    test = TestScoringIntegration()
    test.test_real_world_scenario_quick_solver()
    test.test_real_world_scenario_average_solver()
    test.test_real_world_scenario_late_starter()
    test.test_campaign_with_short_cadence()
    test.test_scoring_fairness_comparison()
    test.test_maximum_points_scenarios()