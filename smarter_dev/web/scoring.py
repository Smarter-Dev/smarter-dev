"""Challenge scoring system with dual-phase decay.

This module implements a sophisticated scoring system that rewards quick
problem solving while providing fair scoring windows for all participants.

Scoring phases:
1. Logarithmic decay (first 1 hour if ≥2 hours remain)
2. Linear fractional reduction (remaining time)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone


def calculate_challenge_points(
    input_generated_at: datetime,
    submission_time: datetime,
    challenge_end_time: datetime
) -> int:
    """Calculate points for a challenge submission using dual-phase scoring.
    
    The scoring system uses:
    - Fixed maximum of 4096 points
    - Logarithmic decay for first 1 hour (if ≥2 hours remain when input requested)
    - Linear fractional reduction for remaining time
    - Pure linear decay if <2 hours remain when input requested
    
    Args:
        input_generated_at: When the participant requested challenge input (timer start)
        submission_time: When the solution was submitted
        challenge_end_time: When the challenge/campaign ends
        
    Returns:
        Integer points earned (0-4096)
    """
    MAX_POINTS = 4096
    LOG_PHASE_DURATION = 3600  # 1 hour in seconds
    MIN_TIME_FOR_DUAL_PHASE = 7200  # 2 hours in seconds
    
    # Ensure all times are timezone-aware for consistent calculations
    if input_generated_at.tzinfo is None:
        input_generated_at = input_generated_at.replace(tzinfo=timezone.utc)
    if submission_time.tzinfo is None:
        submission_time = submission_time.replace(tzinfo=timezone.utc)
    if challenge_end_time.tzinfo is None:
        challenge_end_time = challenge_end_time.replace(tzinfo=timezone.utc)
    
    # Calculate time windows
    elapsed_seconds = (submission_time - input_generated_at).total_seconds()
    time_remaining_at_input = (challenge_end_time - input_generated_at).total_seconds()
    
    # Handle edge cases
    if elapsed_seconds <= 0:
        return MAX_POINTS
    
    # Give max points for very quick submissions (under 10 seconds)
    if elapsed_seconds < 10:
        return MAX_POINTS
    
    if submission_time >= challenge_end_time:
        return 0
    
    # Determine scoring mode based on time remaining when input was requested
    if time_remaining_at_input >= MIN_TIME_FOR_DUAL_PHASE:
        # Dual-phase scoring: logarithmic then linear
        return _calculate_dual_phase_points(
            elapsed_seconds, 
            time_remaining_at_input,
            MAX_POINTS,
            LOG_PHASE_DURATION
        )
    else:
        # Pure linear scoring (not enough time for dual-phase)
        return _calculate_linear_points(
            elapsed_seconds,
            time_remaining_at_input,
            MAX_POINTS
        )


def _calculate_dual_phase_points(
    elapsed_seconds: float,
    time_remaining_at_input: float,
    max_points: int,
    log_phase_duration: float
) -> int:
    """Calculate points using dual-phase (logarithmic + linear) scoring.
    
    Args:
        elapsed_seconds: Time taken to solve (in seconds)
        time_remaining_at_input: Total time available when input requested
        max_points: Maximum possible points (4096)
        log_phase_duration: Duration of logarithmic phase (3600 seconds = 1 hour)
        
    Returns:
        Integer points earned
    """
    if elapsed_seconds <= log_phase_duration:
        # Logarithmic phase: steeper decay from 4096 to 2048 over 1 hour
        # Formula: points = max_points - (max_points/2) * [log2(1 + elapsed/duration)]^0.6
        # The 0.6 power creates a much steeper initial drop
        log_factor = math.log2(1 + elapsed_seconds / log_phase_duration)
        # Apply power to make the curve steeper at the start
        steep_factor = log_factor ** 0.6
        points_raw = max_points - (max_points / 2) * steep_factor
        # Round up to be generous (except if we're exactly at a whole number)
        points = math.ceil(points_raw) if points_raw != int(points_raw) else int(points_raw)
        return points
    else:
        # Linear phase: fractional reduction from 2048 to 0
        points_at_transition = max_points // 2  # 2048
        time_in_linear = elapsed_seconds - log_phase_duration
        total_linear_time = time_remaining_at_input - log_phase_duration
        
        if total_linear_time <= 0:
            return 0
        
        # Calculate fraction of linear time remaining
        fraction_remaining = max(0, 1 - (time_in_linear / total_linear_time))
        points_raw = points_at_transition * fraction_remaining
        
        # Round up to be generous, but return 0 if time is truly up
        if fraction_remaining == 0:
            return 0
        points = math.ceil(points_raw)
        return points


def _calculate_linear_points(
    elapsed_seconds: float,
    time_remaining_at_input: float,
    max_points: int
) -> int:
    """Calculate points using pure linear scoring.
    
    Used when less than 2 hours remain at input request time.
    
    Args:
        elapsed_seconds: Time taken to solve (in seconds)
        time_remaining_at_input: Total time available when input requested
        max_points: Maximum possible points (4096)
        
    Returns:
        Integer points earned
    """
    if time_remaining_at_input <= 0:
        return 0
    
    # Linear decay from max_points to 0 over the available time
    fraction_remaining = max(0, 1 - (elapsed_seconds / time_remaining_at_input))
    
    # Round up to be generous, but return 0 if time is truly up
    if fraction_remaining == 0:
        return 0
    points_raw = max_points * fraction_remaining
    points = math.ceil(points_raw)
    return points