#!/usr/bin/env python3
"""Debug script to investigate campaign selection issue."""

import asyncio
from datetime import datetime, timezone
from sqlalchemy import and_, desc, select
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.models import Campaign

async def debug_campaigns():
    """Debug campaign selection for guild 733364234141827073."""
    guild_id = "733364234141827073"
    
    async with get_db_session_context() as session:
        # Get ALL campaigns for this guild
        print("=== ALL CAMPAIGNS FOR GUILD ===")
        query = select(Campaign).where(Campaign.guild_id == guild_id).order_by(desc(Campaign.start_time))
        result = await session.execute(query)
        all_campaigns = result.scalars().all()
        
        for campaign in all_campaigns:
            print(f"Campaign: {campaign.title}")
            print(f"  ID: {campaign.id}")
            print(f"  Is Active: {campaign.is_active}")
            print(f"  Start Time: {campaign.start_time}")
            print(f"  Start Time <= Now: {campaign.start_time <= datetime.now(timezone.utc)}")
            print(f"  Created At: {campaign.created_at}")
            print()
        
        # Now test the active campaign query
        print("=== ACTIVE CAMPAIGN QUERY ===")
        query = select(Campaign).where(
            and_(
                Campaign.guild_id == guild_id,
                Campaign.start_time <= datetime.now(timezone.utc),
                Campaign.is_active.is_(True)
            )
        ).order_by(desc(Campaign.start_time)).limit(1)
        
        result = await session.execute(query)
        active_campaign = result.scalar_one_or_none()
        
        if active_campaign:
            print(f"Active campaign found: {active_campaign.title} (ID: {active_campaign.id})")
        else:
            print("No active campaign found")
            
        # Test the fallback query
        print("\n=== FALLBACK QUERY (MOST RECENT STARTED) ===")
        query = select(Campaign).where(
            and_(
                Campaign.guild_id == guild_id,
                Campaign.start_time <= datetime.now(timezone.utc)
            )
        ).order_by(desc(Campaign.start_time)).limit(1)
        
        result = await session.execute(query)
        fallback_campaign = result.scalar_one_or_none()
        
        if fallback_campaign:
            print(f"Fallback campaign: {fallback_campaign.title} (ID: {fallback_campaign.id})")
            print(f"  Is Active: {fallback_campaign.is_active}")
        else:
            print("No fallback campaign found")
            
        print(f"\nCurrent UTC time: {datetime.now(timezone.utc)}")

if __name__ == "__main__":
    asyncio.run(debug_campaigns())