"""Test cases for the SquadMembership model."""

from __future__ import annotations

import pytest
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import Base


class TestSquadMembership:
    """Test cases for SquadMembership model functionality."""
    
    async def test_squad_membership_model_exists(self):
        """Test that SquadMembership model can be imported."""
        try:
            from smarter_dev.web.models import SquadMembership
            assert SquadMembership is not None
        except ImportError:
            pytest.fail("SquadMembership model does not exist")
    
    async def test_squad_membership_has_compound_primary_key(self):
        """Test that SquadMembership has compound primary key."""
        from smarter_dev.web.models import SquadMembership
        
        pk_columns = [col.name for col in SquadMembership.__table__.primary_key.columns]
        assert "squad_id" in pk_columns
        assert "user_id" in pk_columns
        assert len(pk_columns) == 2
    
    async def test_squad_membership_required_fields(self):
        """Test that SquadMembership has all required fields per Session 2 specification."""
        from smarter_dev.web.models import SquadMembership
        
        required_fields = [
            "squad_id", "user_id", "guild_id", "joined_at"
        ]
        
        for field_name in required_fields:
            assert hasattr(SquadMembership, field_name), f"Missing field: {field_name}"
    
    async def test_squad_membership_creation_and_retrieval(self, test_engine):
        """Test creating and retrieving SquadMembership records."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import SquadMembership
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            squad_id = uuid4()
            membership = SquadMembership(
                squad_id=squad_id,
                user_id="membership_user_123",
                guild_id="membership_guild_456"
            )
            
            session.add(membership)
            await session.commit()
            await session.refresh(membership)
            
            # Verify creation per Session 2 specification
            assert membership.squad_id == squad_id
            assert membership.user_id == "membership_user_123"
            assert membership.guild_id == "membership_guild_456"
            assert membership.joined_at is not None  # Should be auto-set by server_default
    
    async def test_squad_membership_table_name(self):
        """Test that SquadMembership has correct table name."""
        from smarter_dev.web.models import SquadMembership
        
        assert SquadMembership.__tablename__ == "squad_memberships"