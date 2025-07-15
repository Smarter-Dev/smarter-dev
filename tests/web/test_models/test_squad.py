"""Test cases for the Squad model."""

from __future__ import annotations

import pytest
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import Base


class TestSquad:
    """Test cases for Squad model functionality."""
    
    async def test_squad_model_exists(self):
        """Test that Squad model can be imported."""
        try:
            from smarter_dev.web.models import Squad
            assert Squad is not None
        except ImportError:
            pytest.fail("Squad model does not exist")
    
    async def test_squad_has_uuid_primary_key(self):
        """Test that Squad has UUID primary key."""
        from smarter_dev.web.models import Squad
        
        pk_columns = [col.name for col in Squad.__table__.primary_key.columns]
        assert "id" in pk_columns
        assert len(pk_columns) == 1
    
    async def test_squad_required_fields(self):
        """Test that Squad has all required fields."""
        from smarter_dev.web.models import Squad
        
        required_fields = [
            "id", "guild_id", "role_id", "name", "description", 
            "switch_cost", "max_members", "is_active"
        ]
        
        for field_name in required_fields:
            assert hasattr(Squad, field_name), f"Missing field: {field_name}"
    
    async def test_squad_creation_and_retrieval(self, test_engine):
        """Test creating and retrieving Squad records."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import Squad
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            squad = Squad(
                guild_id="squad_guild_123",
                role_id="squad_role_456", 
                name="Test Squad",
                description="A test squad",
                switch_cost=50,
                max_members=20,
                is_active=True
            )
            
            session.add(squad)
            await session.commit()
            await session.refresh(squad)
            
            # Verify creation
            assert squad.guild_id == "squad_guild_123"
            assert squad.role_id == "squad_role_456"
            assert squad.name == "Test Squad"
            assert squad.description == "A test squad"
            assert squad.switch_cost == 50
            assert squad.max_members == 20
            assert squad.is_active is True
    
    async def test_squad_table_name(self):
        """Test that Squad has correct table name."""
        from smarter_dev.web.models import Squad
        
        assert Squad.__tablename__ == "squads"