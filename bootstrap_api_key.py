#!/usr/bin/env python3
"""Bootstrap script to create initial API key for bot authentication.

This script creates the first API key needed for the Discord bot to authenticate
with the web API. It should be run after the database is set up but before
starting the bot.
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import get_engine, get_session_maker, Base
from smarter_dev.web.models import APIKey
from smarter_dev.web.security import generate_secure_api_key


async def create_bootstrap_api_key():
    """Create an initial API key for bot authentication."""
    settings = get_settings()
    
    print("🔧 Setting up database connection...")
    engine = get_engine()
    session_maker = get_session_maker()
    
    # Create all tables if they don't exist
    print("📊 Creating database tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Generate secure API key
    print("🔐 Generating secure API key...")
    full_key, key_hash, key_prefix = generate_secure_api_key()
    
    # Create API key record
    api_key = APIKey(
        name="Bootstrap Bot API Key",
        description="Initial API key for Discord bot authentication",
        key_hash=key_hash,
        key_prefix=key_prefix,
        scopes=["bot:read", "bot:write"],
        rate_limit_per_hour=10000,  # High limit for bot
        created_by="bootstrap_script",
        is_active=True,
        usage_count=0
    )
    
    # Save to database
    print("💾 Saving API key to database...")
    async with session_maker() as session:
        # Check if we already have a bootstrap key
        from sqlalchemy import select
        existing_key = await session.scalar(
            select(APIKey).where(APIKey.name == "Bootstrap Bot API Key")
        )
        
        if existing_key:
            print("⚠️  Bootstrap API key already exists!")
            print(f"   Key ID: {existing_key.id}")
            print(f"   Created: {existing_key.created_at}")
            print(f"   Active: {existing_key.is_active}")
            return None
        
        session.add(api_key)
        await session.commit()
        await session.refresh(api_key)
    
    print("✅ Successfully created bootstrap API key!")
    print(f"   Key ID: {api_key.id}")
    print(f"   API Key: {full_key}")
    print(f"   Scopes: {api_key.scopes}")
    print(f"   Rate Limit: {api_key.rate_limit_per_hour}/hour")
    print()
    print("🔧 Next steps:")
    print("1. Set the BOT_API_KEY environment variable:")
    print(f"   export BOT_API_KEY='{full_key}'")
    print("2. Or add it to your .env file:")
    print(f"   BOT_API_KEY={full_key}")
    print("3. Start the web server: python main.py")
    print("4. Start the Discord bot: python -m smarter_dev.bot")
    
    return full_key


async def main():
    """Main bootstrap function."""
    try:
        api_key = await create_bootstrap_api_key()
        if api_key:
            print(f"\n🎯 IMPORTANT: Save this API key securely!")
            print(f"   {api_key}")
    except Exception as e:
        print(f"❌ Error creating bootstrap API key: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())