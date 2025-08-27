"""Test cases for the challenge scoring system."""

from __future__ import annotations

import math
import pytest
from datetime import datetime, timedelta, timezone

from smarter_dev.web.scoring import calculate_challenge_points


class TestScoringSystem:
    """Test cases for the dual-phase challenge scoring system."""
    
    def test_max_points_for_instant_submission(self):
        """Test that instant submission gives maximum points."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        submission_time = input_time  # Instant submission
        challenge_end = input_time + timedelta(hours=5)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        assert points == 4096
    
    def test_max_points_for_very_quick_submission(self):
        """Test that submission within a few seconds gives maximum points."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        submission_time = input_time + timedelta(seconds=5)
        challenge_end = input_time + timedelta(hours=5)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        assert points == 4096
    
    def test_logarithmic_phase_at_30_minutes(self):
        """Test logarithmic decay at 30 minute mark when ≥2 hours remain."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        submission_time = input_time + timedelta(minutes=30)
        challenge_end = input_time + timedelta(hours=3)  # 3 hours remain at input
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        # At 30 minutes with steeper curve: 4096 - 2048 * [log2(1.5)]^0.6 ≈ 2612
        assert points == 2612
        assert 2600 < points < 2650  # Sanity check range
    
    def test_logarithmic_phase_at_1_hour_exact(self):
        """Test that exactly 1 hour gives exactly 2048 points."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        submission_time = input_time + timedelta(hours=1)
        challenge_end = input_time + timedelta(hours=3)  # 3 hours remain at input
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        assert points == 2048
    
    def test_linear_phase_after_1_hour(self):
        """Test linear decay after the 1-hour logarithmic phase."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        submission_time = input_time + timedelta(hours=1.5)  # 30 min into linear phase
        challenge_end = input_time + timedelta(hours=3)  # 3 hours total
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        # Linear phase: 2 hours total linear time, 30 min elapsed
        # 2048 * (1.5/2) = 1536
        assert points == 1536
    
    def test_linear_phase_at_challenge_end(self):
        """Test that submission at challenge end gives 0 points."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=5)
        submission_time = challenge_end  # Submit exactly at end
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        assert points == 0
    
    def test_linear_phase_just_before_end(self):
        """Test that submission just before end gives very few points."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=3)
        submission_time = challenge_end - timedelta(minutes=1)  # 1 minute before end
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        # With 1 minute left of 180 minutes total (120 linear):
        # 2048 * (1/120) = 17.066... rounds up to 18
        assert points == 18
    
    def test_pure_logarithmic_when_less_than_2_hours_remain(self):
        """Test pure logarithmic decay when <2 hours remain at input request."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=1.5)  # Only 1.5 hours remain
        submission_time = input_time + timedelta(minutes=30)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        # Pure logarithmic: 4096 - 4096 * [log2(1 + 30/90)]^0.6
        # log2(1.333) ≈ 0.415, 0.415^0.6 ≈ 0.590
        # 4096 - 4096 * 0.590 ≈ 1680
        assert points == 1680
    
    def test_exactly_2_hours_remaining_uses_dual_phase(self):
        """Test that exactly 2 hours remaining uses dual-phase scoring."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=2)  # Exactly 2 hours
        submission_time = input_time + timedelta(minutes=30)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        # Should use logarithmic phase for first 30 minutes (steeper curve)
        assert points == 2612
    
    def test_submission_after_challenge_end(self):
        """Test that submission after challenge end gives 0 points."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=5)
        submission_time = challenge_end + timedelta(minutes=10)  # 10 minutes late
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        assert points == 0
    
    def test_linear_phase_halfway_point(self):
        """Test linear phase at the halfway point."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=5)  # 5 hours total
        # 1 hour log phase + 2 hours into 4-hour linear phase
        submission_time = input_time + timedelta(hours=3)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        # At halfway through linear: 2048 * 0.5 = 1024
        assert points == 1024
    
    def test_very_long_challenge_duration(self):
        """Test scoring with a very long challenge duration (24 hours)."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=24)
        submission_time = input_time + timedelta(hours=1)  # End of log phase
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        assert points == 2048
        
        # Test middle of linear phase (12 hours in)
        submission_time = input_time + timedelta(hours=12)
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        # 11 hours into 23-hour linear phase: 2048 * (12/23) ≈ 1069
        assert 1060 < points < 1080
    
    def test_fractional_minutes_in_linear_phase(self):
        """Test that fractional minutes are handled correctly in linear phase."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=3)
        # Submit at 1.5 hours (30 min into linear phase)
        submission_time = input_time + timedelta(hours=1, minutes=30)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        # 1.5 hours remain of 2-hour linear phase: 2048 * (1.5/2) = 1536
        assert points == 1536
    
    def test_timezone_handling(self):
        """Test that different timezones are handled correctly."""
        # Use different timezone representations
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        submission_time = datetime(2025, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        challenge_end = datetime(2025, 1, 1, 15, 0, 0, tzinfo=timezone.utc)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        
        # Same times but without timezone (should still work)
        input_time_naive = input_time.replace(tzinfo=None)
        submission_time_naive = submission_time.replace(tzinfo=None)
        challenge_end_naive = challenge_end.replace(tzinfo=None)
        
        points_naive = calculate_challenge_points(
            input_time_naive, submission_time_naive, challenge_end_naive
        )
        assert points == points_naive
    
    def test_edge_case_1_hour_59_minutes_remaining(self):
        """Test edge case with just under 2 hours remaining."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=1, minutes=59)
        submission_time = input_time + timedelta(minutes=30)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        # Should use pure logarithmic: 4096 - 4096 * [log2(1 + 30/119)]^0.6
        # log2(1.252) ≈ 0.324, 0.324^0.6 ≈ 0.509
        # 4096 - 4096 * 0.509 ≈ 2012
        assert points == 2012
    
    def test_edge_case_2_hours_1_minute_remaining(self):
        """Test edge case with just over 2 hours remaining."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=2, minutes=1)
        submission_time = input_time + timedelta(minutes=30)
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        # Should use logarithmic phase (steeper curve with ceiling)
        assert points == 2612
    
    def test_ceiling_behavior_in_linear_phase(self):
        """Test that fractional points are rounded up (ceiling) in linear phase."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=3)  # 1hr log + 2hr linear
        
        # Test with 2 minutes left (should give more than floor would)
        submission_time = challenge_end - timedelta(minutes=2)
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        # 2048 * (2/120) = 34.133... rounds up to 35
        assert points == 35
        
        # Test with 10 seconds left (very small but not 0)
        submission_time = challenge_end - timedelta(seconds=10)
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        # 2048 * (10/(120*60)) = 2.844... rounds up to 3
        assert points == 3
    
    def test_2048_points_at_dual_phase_transition(self):
        """Test that completing at 1 hour with dual-phase scoring gives exactly 2048 points."""
        # Start with 2hrs, complete in 1hr (end of log phase)
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=2)  # 2 hours remain
        submission_time = input_time + timedelta(hours=1)  # Complete after 1 hour
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        
        # Should give exactly 2048 points at the transition
        assert points == 2048, f"Should give 2048 at 1hr transition, got {points}"
    
    def test_pure_logarithmic_with_1_hour_remaining(self):
        """Test pure logarithmic scoring with 1 hour total time."""
        input_time = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        challenge_end = input_time + timedelta(hours=1)  # 1 hour remains
        submission_time = input_time + timedelta(minutes=30)  # Complete after 30 min
        
        points = calculate_challenge_points(input_time, submission_time, challenge_end)
        
        # Pure logarithmic: 4096 - 4096 * [log2(1 + 0.5)]^0.6
        # log2(1.5) ≈ 0.585, 0.585^0.6 ≈ 0.707
        # 4096 - 4096 * 0.707 ≈ 1200
        assert 1100 < points < 1300, f"Expected ~1200 points, got {points}"