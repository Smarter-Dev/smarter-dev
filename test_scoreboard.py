#!/usr/bin/env python3
"""Test script to replicate the scoreboard endpoint logic."""

import asyncio
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.crud import CampaignOperations, ChallengeSubmissionOperations

async def test_scoreboard():
    """Test the scoreboard endpoint logic."""
    guild_id = "733364234141827073"
    
    async with get_db_session_context() as session:
        # Replicate the exact logic from the scoreboard endpoint
        campaign_ops = CampaignOperations(session)
        current_campaign = await campaign_ops.get_most_recent_campaign(guild_id)
        
        if not current_campaign:
            print("No campaign found")
            return
            
        print(f"Scoreboard Campaign: {current_campaign.title}")
        print(f"  ID: {current_campaign.id}")
        print(f"  Is Active: {current_campaign.is_active}")
        print(f"  Start Time: {current_campaign.start_time}")
        
        # Get scoreboard data for the campaign
        submission_ops = ChallengeSubmissionOperations(session)
        scoreboard_data = await submission_ops.get_campaign_scoreboard(current_campaign.id)
        
        print(f"\nScoreboard has {len(scoreboard_data)} entries:")
        for i, entry in enumerate(scoreboard_data[:3]):  # Show first 3 entries
            print(f"Entry {i}: {entry}")

if __name__ == "__main__":
    asyncio.run(test_scoreboard())