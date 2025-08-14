#!/usr/bin/env python3
"""Script to remove all members from default squads.

This script connects to the production database and removes all users from
default squads. This is needed when users were auto-assigned to default
squads before the role assignment system was working properly.

Usage:
    python scripts/remove_default_squad_members.py --database-url <prod_url>
    
Example:
    python scripts/remove_default_squad_members.py --database-url postgresql+asyncpg://user:pass@host:5432/db
"""

import asyncio
import argparse
import sys
from typing import List, Dict, Any

# Add the project root to Python path
sys.path.insert(0, '.')

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from smarter_dev.web.models import Squad, SquadMembership
from smarter_dev.shared.database import convert_postgres_url_for_asyncpg


async def get_default_squads(session: AsyncSession) -> List[Dict[str, Any]]:
    """Get all default squads with their member counts."""
    stmt = (
        select(
            Squad.id,
            Squad.guild_id, 
            Squad.name,
            func.count(SquadMembership.user_id).label('member_count')
        )
        .select_from(Squad)
        .outerjoin(SquadMembership, Squad.id == SquadMembership.squad_id)
        .where(Squad.is_default == True)
        .group_by(Squad.id, Squad.guild_id, Squad.name)
    )
    
    result = await session.execute(stmt)
    rows = result.fetchall()
    
    return [
        {
            'id': row.id,
            'guild_id': row.guild_id,
            'name': row.name,
            'member_count': row.member_count
        }
        for row in rows
    ]


async def get_default_squad_members(session: AsyncSession, squad_id: str) -> List[Dict[str, Any]]:
    """Get all members of a specific default squad."""
    stmt = (
        select(
            SquadMembership.user_id,
            SquadMembership.guild_id,
            SquadMembership.joined_at
        )
        .where(SquadMembership.squad_id == squad_id)
        .order_by(SquadMembership.joined_at)
    )
    
    result = await session.execute(stmt)
    rows = result.fetchall()
    
    return [
        {
            'user_id': row.user_id,
            'guild_id': row.guild_id, 
            'joined_at': row.joined_at
        }
        for row in rows
    ]


async def remove_squad_members(session: AsyncSession, squad_id: str) -> int:
    """Remove all members from a squad."""
    stmt = delete(SquadMembership).where(SquadMembership.squad_id == squad_id)
    result = await session.execute(stmt)
    return result.rowcount


async def main():
    parser = argparse.ArgumentParser(description='Remove all members from default squads')
    parser.add_argument('--database-url', required=True, help='Production database URL')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be deleted without actually deleting')
    parser.add_argument('--guild-id', help='Only process default squads for specific guild')
    
    args = parser.parse_args()
    
    print(f"🔌 Connecting to database...")
    print(f"🔍 Mode: {'DRY RUN' if args.dry_run else 'LIVE DELETION'}")
    if args.guild_id:
        print(f"🏰 Guild filter: {args.guild_id}")
    
    # Create database connection
    cleaned_url, connect_args = convert_postgres_url_for_asyncpg(args.database_url)
    engine = create_async_engine(
        cleaned_url,
        connect_args=connect_args,
        echo=False,
        pool_pre_ping=True
    )
    
    session_maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    
    try:
        async with session_maker() as session:
            # Get all default squads
            print(f"\n📋 Finding default squads...")
            default_squads = await get_default_squads(session)
            
            if args.guild_id:
                default_squads = [s for s in default_squads if s['guild_id'] == args.guild_id]
            
            if not default_squads:
                print("✅ No default squads found!")
                return
            
            print(f"🔍 Found {len(default_squads)} default squad(s):")
            total_members = 0
            
            for squad in default_squads:
                print(f"  • {squad['name']} (Guild: {squad['guild_id']}) - {squad['member_count']} members")
                total_members += squad['member_count']
            
            if total_members == 0:
                print("✅ No members in default squads!")
                return
            
            print(f"\n⚠️  Total members to remove: {total_members}")
            
            if args.dry_run:
                print("🔍 DRY RUN - Showing detailed member list:")
                for squad in default_squads:
                    if squad['member_count'] > 0:
                        print(f"\n📝 Squad: {squad['name']} (Guild: {squad['guild_id']})")
                        members = await get_default_squad_members(session, str(squad['id']))
                        for i, member in enumerate(members, 1):
                            print(f"  {i:3d}. User ID: {member['user_id']} (joined: {member['joined_at']})")
                
                print(f"\n🔍 DRY RUN COMPLETE - No changes made")
                print(f"💡 Run without --dry-run to actually remove {total_members} members")
                
            else:
                # Confirm deletion
                print(f"\n⚠️  This will PERMANENTLY remove {total_members} users from default squads!")
                confirmation = input("🤔 Are you sure? Type 'DELETE' to confirm: ")
                
                if confirmation != 'DELETE':
                    print("❌ Operation cancelled")
                    return
                
                print(f"\n🗑️  Removing members from default squads...")
                
                total_removed = 0
                for squad in default_squads:
                    if squad['member_count'] > 0:
                        removed = await remove_squad_members(session, str(squad['id']))
                        total_removed += removed
                        print(f"  ✅ Removed {removed} members from '{squad['name']}'")
                
                await session.commit()
                print(f"\n🎉 Successfully removed {total_removed} members from default squads!")
                
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
    
    finally:
        await engine.dispose()
    
    print(f"✅ Operation complete!")


if __name__ == "__main__":
    asyncio.run(main())