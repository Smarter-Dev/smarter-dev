"""Public views for campaigns and challenges."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import markdown
from sqlalchemy import select, func, desc, and_
from sqlalchemy.orm import selectinload
from starlette.requests import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.models import Campaign, Challenge, ChallengeSubmission

templates = Jinja2Templates(directory="templates")

# Add markdown filter to Jinja2
def markdown_filter(text: str) -> str:
    """Convert markdown text to HTML."""
    md = markdown.Markdown(extensions=['codehilite', 'fenced_code', 'tables', 'toc'])
    return md.convert(text)

def strip_markdown_filter(text: str, max_length: int = 200) -> str:
    """Strip markdown formatting and create a text excerpt."""
    # Remove markdown headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove markdown links but keep link text
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    # Remove markdown emphasis
    text = re.sub(r'[*_]{1,2}([^*_]+)[*_]{1,2}', r'\1', text)
    # Remove code blocks and inline code
    text = re.sub(r'```[^`]*```', '', text, flags=re.DOTALL)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove blockquotes
    text = re.sub(r'^>\s+', '', text, flags=re.MULTILINE)
    # Clean up multiple whitespace
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    
    # Truncate to max_length
    if len(text) > max_length:
        text = text[:max_length].rsplit(' ', 1)[0] + '...'
    
    return text

def strftime_filter(value, fmt='%Y-%m-%d'):
    """Format datetime or string as strftime."""
    if isinstance(value, str) and value == 'now':
        return datetime.now().strftime(fmt)
    elif hasattr(value, 'strftime'):
        return value.strftime(fmt)
    return str(value)

templates.env.filters['markdown'] = markdown_filter
templates.env.filters['strip_markdown'] = strip_markdown_filter
templates.env.filters['strftime'] = strftime_filter

# Make settings available in templates
templates.env.globals['config'] = get_settings()


async def campaigns_list(request: Request) -> Response:
    """Display list of all public campaigns."""
    async with get_db_session_context() as session:
        # Get all campaigns with challenge counts and basic stats
        campaigns_query = (
            select(Campaign)
            .where(Campaign.is_active == True)
            .order_by(desc(Campaign.start_time))
            .options(selectinload(Campaign.challenges))
        )
        
        result = await session.execute(campaigns_query)
        campaigns = result.scalars().all()
        
        # Enhance campaigns with additional data
        enhanced_campaigns = []
        for campaign in campaigns:
            # Determine campaign status
            now = datetime.now(timezone.utc)
            if campaign.start_time > now:
                status = 'upcoming'
                next_challenge_time = campaign.start_time
            else:
                # Check if any challenges are still being released
                released_challenges = [c for c in campaign.challenges if c.is_released]
                total_challenges = len(campaign.challenges)
                
                if len(released_challenges) < total_challenges:
                    status = 'active'
                    # Calculate next challenge release time
                    if released_challenges:
                        last_release = max(c.released_at for c in released_challenges if c.released_at)
                        next_challenge_time = last_release
                        # Add release cadence to get next release
                        from datetime import timedelta
                        next_challenge_time += timedelta(hours=campaign.release_cadence_hours)
                    else:
                        next_challenge_time = None
                else:
                    status = 'completed'
                    next_challenge_time = None
            
            # Get submission counts
            submission_count_query = (
                select(func.count(ChallengeSubmission.id))
                .select_from(ChallengeSubmission)
                .join(Challenge)
                .where(Challenge.campaign_id == campaign.id)
            )
            submission_result = await session.execute(submission_count_query)
            submission_count = submission_result.scalar() or 0
            
            # Get participant count (unique users who submitted)
            participant_count_query = (
                select(func.count(func.distinct(ChallengeSubmission.user_id)))
                .select_from(ChallengeSubmission)
                .join(Challenge)
                .where(Challenge.campaign_id == campaign.id)
            )
            participant_result = await session.execute(participant_count_query)
            participant_count = participant_result.scalar() or 0
            
            enhanced_campaigns.append({
                **campaign.__dict__,
                'id': campaign.id,
                'title': campaign.title,
                'description': campaign.description,
                'start_time': campaign.start_time,
                'is_active': campaign.is_active,
                'status': status,
                'next_challenge_time': next_challenge_time,
                'challenge_count': len(campaign.challenges),
                'submission_count': submission_count,
                'participant_count': participant_count,
            })
    
    return templates.TemplateResponse(
        request,
        "campaigns.html",
        {"campaigns": enhanced_campaigns}
    )


async def campaign_detail(request: Request) -> Response:
    """Display campaign details with challenges."""
    campaign_id = request.path_params["campaign_id"]
    
    try:
        campaign_uuid = UUID(campaign_id)
    except ValueError:
        # Invalid UUID format
        return templates.TemplateResponse(
            request,
            "404.html",
            status_code=404
        )
    
    async with get_db_session_context() as session:
        # Get campaign with challenges
        campaign_query = (
            select(Campaign)
            .where(and_(Campaign.id == campaign_uuid, Campaign.is_active == True))
            .options(selectinload(Campaign.challenges))
        )
        
        result = await session.execute(campaign_query)
        campaign = result.scalar_one_or_none()
        
        if not campaign:
            return templates.TemplateResponse(
                request,
                "404.html",
                status_code=404
            )
        
        # Determine campaign status and next challenge time
        now = datetime.now(timezone.utc)
        if campaign.start_time > now:
            status = 'upcoming'
            next_challenge_time = campaign.start_time
        else:
            released_challenges = [c for c in campaign.challenges if c.is_released]
            total_challenges = len(campaign.challenges)
            
            if len(released_challenges) < total_challenges:
                status = 'active'
                if released_challenges:
                    last_release = max(c.released_at for c in released_challenges if c.released_at)
                    from datetime import timedelta
                    next_challenge_time = last_release + timedelta(hours=campaign.release_cadence_hours)
                else:
                    next_challenge_time = None
            else:
                status = 'completed'
                next_challenge_time = None
        
        # Enhance challenges with additional data
        enhanced_challenges = []
        for challenge in sorted(campaign.challenges, key=lambda c: c.order_position):
            # Get submission count for this challenge
            submission_count_query = (
                select(func.count(ChallengeSubmission.id))
                .where(ChallengeSubmission.challenge_id == challenge.id)
            )
            submission_result = await session.execute(submission_count_query)
            submission_count = submission_result.scalar() or 0
            
            # Determine challenge status
            if challenge.is_released:
                challenge_status = 'active' if status == 'active' else 'completed'
            else:
                challenge_status = 'locked'
            
            # Calculate scheduled release time if not released
            scheduled_release_time = None
            if not challenge.is_released and campaign.start_time <= now:
                from datetime import timedelta
                scheduled_release_time = campaign.start_time + timedelta(
                    hours=(challenge.order_position - 1) * campaign.release_cadence_hours
                )
            
            enhanced_challenges.append({
                **challenge.__dict__,
                'id': challenge.id,
                'title': challenge.title,
                'description': challenge.description,
                'order_position': challenge.order_position,
                'points_value': challenge.points_value,
                'is_released': challenge.is_released,
                'released_at': challenge.released_at,
                'status': challenge_status,
                'submission_count': submission_count,
                'scheduled_release_time': scheduled_release_time,
            })
        
        # Get overall campaign stats
        submission_count_query = (
            select(func.count(ChallengeSubmission.id))
            .select_from(ChallengeSubmission)
            .join(Challenge)
            .where(Challenge.campaign_id == campaign.id)
        )
        submission_result = await session.execute(submission_count_query)
        total_submission_count = submission_result.scalar() or 0
        
        participant_count_query = (
            select(func.count(func.distinct(ChallengeSubmission.user_id)))
            .select_from(ChallengeSubmission)
            .join(Challenge)
            .where(Challenge.campaign_id == campaign.id)
        )
        participant_result = await session.execute(participant_count_query)
        total_participant_count = participant_result.scalar() or 0
        
        # Set campaign status
        campaign.status = status
    
    return templates.TemplateResponse(
        request,
        "campaign_detail.html",
        {
            "campaign": campaign,
            "submission_count": total_submission_count,
            "participant_count": total_participant_count,
            "next_challenge_time": next_challenge_time,
        }
    )


async def challenge_detail(request: Request) -> Response:
    """Display challenge details."""
    challenge_id = request.path_params["challenge_id"]
    
    try:
        challenge_uuid = UUID(challenge_id)
    except ValueError:
        return templates.TemplateResponse(
            request,
            "404.html",
            status_code=404
        )
    
    async with get_db_session_context() as session:
        # Get challenge with campaign
        challenge_query = (
            select(Challenge)
            .where(Challenge.id == challenge_uuid)
            .options(selectinload(Challenge.campaign))
        )
        
        result = await session.execute(challenge_query)
        challenge = result.scalar_one_or_none()
        
        if not challenge or not challenge.campaign.is_active:
            return templates.TemplateResponse(
                request,
                "404.html",
                status_code=404
            )
        
        # Get submission count for this challenge
        submission_count_query = (
            select(func.count(ChallengeSubmission.id))
            .where(ChallengeSubmission.challenge_id == challenge.id)
        )
        submission_result = await session.execute(submission_count_query)
        submission_count = submission_result.scalar() or 0
        
        # Get recent submissions for display (limit 10, most recent first)
        recent_submissions_query = (
            select(ChallengeSubmission)
            .where(ChallengeSubmission.challenge_id == challenge.id)
            .order_by(desc(ChallengeSubmission.submitted_at))
            .limit(10)
        )
        
        recent_submissions_result = await session.execute(recent_submissions_query)
        recent_submissions = recent_submissions_result.scalars().all()
        
        # Add squad names to submissions (you'd need to join with Squad table)
        # For now, we'll use a placeholder
        enhanced_submissions = []
        for submission in recent_submissions:
            enhanced_submissions.append({
                **submission.__dict__,
                'squad_name': 'Squad Name',  # TODO: Join with Squad table
            })
        
        # Determine challenge status
        now = datetime.now(timezone.utc)
        if challenge.is_released:
            if challenge.campaign.start_time <= now:
                challenge_status = 'active'
            else:
                challenge_status = 'completed'
        else:
            challenge_status = 'locked'
        
        challenge.status = challenge_status
    
    return templates.TemplateResponse(
        request,
        "challenge_detail.html",
        {
            "challenge": challenge,
            "submission_count": submission_count,
            "recent_submissions": enhanced_submissions,
        }
    )


async def campaign_leaderboard(request: Request) -> Response:
    """Display campaign leaderboard."""
    campaign_id = request.path_params["campaign_id"]
    
    try:
        campaign_uuid = UUID(campaign_id)
    except ValueError:
        return templates.TemplateResponse(
            request,
            "404.html",
            status_code=404
        )
    
    async with get_db_session_context() as session:
        # Get campaign
        campaign_query = (
            select(Campaign)
            .where(and_(Campaign.id == campaign_uuid, Campaign.is_active == True))
            .options(selectinload(Campaign.challenges))
        )
        
        result = await session.execute(campaign_query)
        campaign = result.scalar_one_or_none()
        
        if not campaign:
            return templates.TemplateResponse(
                request,
                "404.html",
                status_code=404
            )
        
        # TODO: Build actual leaderboard from squads and submissions
        # This is a placeholder implementation
        leaderboard = []
        
        # Get challenge breakdown
        challenge_breakdown = []
        for challenge in sorted(campaign.challenges, key=lambda c: c.order_position):
            submission_count_query = (
                select(func.count(ChallengeSubmission.id))
                .where(ChallengeSubmission.challenge_id == challenge.id)
            )
            submission_result = await session.execute(submission_count_query)
            submission_count = submission_result.scalar() or 0
            
            correct_submissions_query = (
                select(func.count(ChallengeSubmission.id))
                .where(and_(
                    ChallengeSubmission.challenge_id == challenge.id,
                    ChallengeSubmission.is_correct == True
                ))
            )
            correct_result = await session.execute(correct_submissions_query)
            correct_submissions = correct_result.scalar() or 0
            
            challenge_breakdown.append({
                **challenge.__dict__,
                'submission_count': submission_count,
                'correct_submissions': correct_submissions,
            })
        
        # Get overall stats
        total_submissions_query = (
            select(func.count(ChallengeSubmission.id))
            .select_from(ChallengeSubmission)
            .join(Challenge)
            .where(Challenge.campaign_id == campaign.id)
        )
        total_submissions_result = await session.execute(total_submissions_query)
        total_submissions = total_submissions_result.scalar() or 0
        
        total_participants_query = (
            select(func.count(func.distinct(ChallengeSubmission.user_id)))
            .select_from(ChallengeSubmission)
            .join(Challenge)
            .where(Challenge.campaign_id == campaign.id)
        )
        total_participants_result = await session.execute(total_participants_query)
        total_participants = total_participants_result.scalar() or 0
        
        # Determine campaign status
        now = datetime.now(timezone.utc)
        if campaign.start_time > now:
            status = 'upcoming'
        else:
            released_challenges = [c for c in campaign.challenges if c.is_released]
            total_challenges = len(campaign.challenges)
            
            if len(released_challenges) < total_challenges:
                status = 'active'
            else:
                status = 'completed'
        
        campaign.status = status
    
    return templates.TemplateResponse(
        request,
        "campaign_leaderboard.html",
        {
            "campaign": campaign,
            "leaderboard": leaderboard,
            "challenge_breakdown": challenge_breakdown,
            "total_challenges": len(campaign.challenges),
            "total_submissions": total_submissions,
            "total_participants": total_participants,
        }
    )