"""Challenge scheduling service for automated challenge releases.

This service monitors active campaigns and automatically releases challenges
based on their scheduled release times.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.models import Campaign, Challenge

logger = logging.getLogger(__name__)


class ChallengeScheduler:
    """Service for automated challenge scheduling and release."""
    
    def __init__(self, check_interval_seconds: int = 60):
        """Initialize the scheduler.
        
        Args:
            check_interval_seconds: How often to check for challenges to release
        """
        self.check_interval = check_interval_seconds
        self.running = False
        
    async def start(self):
        """Start the challenge scheduler."""
        if self.running:
            logger.warning("Challenge scheduler is already running")
            return
            
        self.running = True
        logger.info("Starting challenge scheduler")
        
        while self.running:
            try:
                await self._check_and_release_challenges()
                await asyncio.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"Error in challenge scheduler: {e}")
                # Continue running even if there's an error
                await asyncio.sleep(self.check_interval)
    
    async def stop(self):
        """Stop the challenge scheduler."""
        self.running = False
        logger.info("Challenge scheduler stopped")
    
    async def _check_and_release_challenges(self):
        """Check for challenges that need to be released and release them."""
        try:
            async with get_db_session_context() as session:
                # Find all active campaigns
                active_campaigns = await self._get_active_campaigns(session)
                
                for campaign in active_campaigns:
                    await self._process_campaign_releases(session, campaign)
                    
        except Exception as e:
            logger.error(f"Error checking challenges for release: {e}")
    
    async def _get_active_campaigns(self, session: AsyncSession) -> List[Campaign]:
        """Get all active campaigns that may have challenges to release."""
        query = select(Campaign).where(
            and_(
                Campaign.state == "active",
                Campaign.start_date <= datetime.now(timezone.utc)
            )
        )
        result = await session.execute(query)
        return list(result.scalars().all())
    
    async def _process_campaign_releases(self, session: AsyncSession, campaign: Campaign):
        """Process challenge releases for a specific campaign."""
        try:
            # Get all challenges for this campaign ordered by position
            query = select(Challenge).where(
                Challenge.campaign_id == campaign.id
            ).order_by(Challenge.order_position)
            
            result = await session.execute(query)
            challenges = list(result.scalars().all())
            
            current_time = datetime.now(timezone.utc)
            
            for challenge in challenges:
                # Skip already released challenges
                if challenge.is_released:
                    continue
                
                # Check if this challenge should be released now
                if challenge.should_be_released(campaign.start_date, campaign.release_delay_minutes):
                    await self._release_challenge(session, campaign, challenge)
                else:
                    # Since challenges are ordered, we can break early
                    # if we find one that's not ready to release
                    break
                    
        except Exception as e:
            logger.error(f"Error processing releases for campaign {campaign.id}: {e}")
    
    async def _release_challenge(
        self, 
        session: AsyncSession, 
        campaign: Campaign, 
        challenge: Challenge
    ):
        """Release a challenge and send announcements."""
        try:
            # Log the release
            logger.info(
                f"Releasing challenge '{challenge.title}' "
                f"(ID: {challenge.id}) for campaign '{campaign.name}'"
            )
            
            # Mark challenge as released
            challenge.released_at = datetime.now(timezone.utc)
            session.add(challenge)
            await session.commit()
            
            # Send Discord announcement
            await self._send_challenge_announcement(campaign, challenge)
            
            logger.info(f"Challenge {challenge.id} released successfully")
            
        except Exception as e:
            logger.error(f"Error releasing challenge {challenge.id}: {e}")
            # Rollback the transaction if there was an error
            await session.rollback()
    
    async def _send_challenge_announcement(self, campaign: Campaign, challenge: Challenge):
        """Send Discord announcement for a newly released challenge."""
        try:
            logger.info(
                f"Sending announcement for challenge '{challenge.title}' "
                f"in campaign '{campaign.name}' to channel {campaign.announcement_channel_id}"
            )
            
            # Create announcement data that could be picked up by the Discord bot
            # We'll store this in a simple notification table or file system
            announcement_data = {
                "type": "challenge_release",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "campaign_id": str(campaign.id),
                "campaign_name": campaign.name,
                "challenge_id": str(challenge.id),
                "challenge_title": challenge.title,
                "challenge_description": challenge.description,
                "challenge_position": challenge.order_position,
                "channel_id": campaign.announcement_channel_id,
                "guild_id": campaign.guild_id
            }
            
            # Write to a notifications file that the Discord bot can monitor
            import json
            import os
            
            # Ensure notifications directory exists
            notifications_dir = "notifications"
            os.makedirs(notifications_dir, exist_ok=True)
            
            # Write notification file
            notification_file = f"{notifications_dir}/challenge_release_{campaign.id}_{challenge.id}.json"
            with open(notification_file, 'w') as f:
                json.dump(announcement_data, f, indent=2)
            
            logger.info(f"Announcement data written to {notification_file}")
            
        except Exception as e:
            logger.error(f"Error preparing announcement for challenge {challenge.id}: {e}")


class ChallengeReleaseService:
    """Service for managing challenge release states and notifications."""
    
    @staticmethod
    async def get_next_challenge_release(campaign_id: UUID) -> Optional[dict]:
        """Get information about the next challenge to be released."""
        async with get_db_session_context() as session:
            try:
                # Get campaign
                campaign_query = select(Campaign).where(Campaign.id == campaign_id)
                campaign_result = await session.execute(campaign_query)
                campaign = campaign_result.scalar_one_or_none()
                if not campaign:
                    return None
                
                # Get all challenges for this campaign
                challenges_query = select(Challenge).where(
                    Challenge.campaign_id == campaign_id
                ).order_by(Challenge.order_position)
                challenges_result = await session.execute(challenges_query)
                challenges = list(challenges_result.scalars().all())
                
                current_time = datetime.now(timezone.utc)
                
                # Find the next unreleased challenge
                for challenge in sorted(challenges, key=lambda c: c.order_position):
                    # Skip already released challenges
                    if challenge.is_released:
                        continue
                    
                    release_time = challenge.get_release_time(
                        campaign.start_date,
                        campaign.release_delay_minutes
                    )
                    
                    return {
                        "challenge_id": challenge.id,
                        "challenge_title": challenge.title,
                        "order_position": challenge.order_position,
                        "release_time": release_time,
                        "time_until_release": max(0, (release_time - current_time).total_seconds()),
                        "is_ready": release_time <= current_time
                    }
                
                return None  # All challenges are released
                
            except Exception as e:
                logger.error(f"Error getting next challenge release: {e}")
                return None
    
    @staticmethod
    async def get_released_challenges(campaign_id: UUID) -> List[Challenge]:
        """Get all challenges that have been released for a campaign."""
        async with get_db_session_context() as session:
            try:
                # Get campaign to verify it exists
                campaign_query = select(Campaign).where(Campaign.id == campaign_id)
                campaign_result = await session.execute(campaign_query)
                campaign = campaign_result.scalar_one_or_none()
                if not campaign:
                    return []
                
                # Get all challenges for this campaign
                challenges_query = select(Challenge).where(
                    Challenge.campaign_id == campaign_id
                ).order_by(Challenge.order_position)
                challenges_result = await session.execute(challenges_query)
                challenges = list(challenges_result.scalars().all())
                
                # Filter to only released challenges
                released_challenges = [
                    challenge for challenge in challenges 
                    if challenge.is_released
                ]
                
                return sorted(released_challenges, key=lambda c: c.order_position)
                
            except Exception as e:
                logger.error(f"Error getting released challenges: {e}")
                return []


async def run_challenge_scheduler():
    """Entry point for running the challenge scheduler as a standalone service."""
    scheduler = ChallengeScheduler(check_interval_seconds=60)
    
    try:
        await scheduler.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, stopping scheduler...")
        await scheduler.stop()
    except Exception as e:
        logger.error(f"Challenge scheduler error: {e}")
        await scheduler.stop()
        raise


if __name__ == "__main__":
    # Run the scheduler
    import asyncio
    asyncio.run(run_challenge_scheduler())