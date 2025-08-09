"""Test cases for the ForumAgent model."""

from __future__ import annotations

import pytest
from uuid import UUID
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from smarter_dev.shared.database import Base


class TestForumAgent:
    """Test cases for ForumAgent model functionality."""
    
    async def test_forum_agent_model_exists(self):
        """Test that ForumAgent model can be imported."""
        try:
            from smarter_dev.web.models import ForumAgent
            assert ForumAgent is not None
        except ImportError:
            pytest.fail("ForumAgent model does not exist")
    
    async def test_forum_agent_has_uuid_primary_key(self):
        """Test that ForumAgent has UUID primary key."""
        from smarter_dev.web.models import ForumAgent
        
        pk_columns = [col.name for col in ForumAgent.__table__.primary_key.columns]
        assert "id" in pk_columns
        assert len(pk_columns) == 1
    
    async def test_forum_agent_required_fields(self):
        """Test that ForumAgent has all required fields."""
        from smarter_dev.web.models import ForumAgent
        
        required_fields = [
            "id", "guild_id", "name", "description", "system_prompt",
            "monitored_forums", "is_active", "response_threshold", 
            "max_responses_per_hour", "created_at", "updated_at", "created_by"
        ]
        
        for field_name in required_fields:
            assert hasattr(ForumAgent, field_name), f"Missing field: {field_name}"
    
    async def test_forum_agent_creation_and_retrieval(self, test_engine):
        """Test creating and retrieving ForumAgent records."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            agent = ForumAgent(
                guild_id="forum_guild_123",
                name="Test Forum Agent",
                description="A test forum monitoring agent",
                system_prompt="Monitor posts and respond when needed",
                monitored_forums=["123456789", "987654321"],
                is_active=True,
                response_threshold=0.8,
                max_responses_per_hour=10,
                created_by="test_admin"
            )
            
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
            
            # Verify creation
            assert agent.guild_id == "forum_guild_123"
            assert agent.name == "Test Forum Agent"
            assert agent.description == "A test forum monitoring agent"
            assert agent.system_prompt == "Monitor posts and respond when needed"
            assert agent.monitored_forums == ["123456789", "987654321"]
            assert agent.is_active is True
            assert agent.response_threshold == 0.8
            assert agent.max_responses_per_hour == 10
            assert agent.created_by == "test_admin"
            assert isinstance(agent.id, UUID)
            assert isinstance(agent.created_at, datetime)
            assert isinstance(agent.updated_at, datetime)
    
    async def test_forum_agent_table_name(self):
        """Test that ForumAgent has correct table name."""
        from smarter_dev.web.models import ForumAgent
        
        assert ForumAgent.__tablename__ == "forum_agents"
    
    async def test_forum_agent_default_values(self, test_engine):
        """Test that ForumAgent has correct default values."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            agent = ForumAgent(
                guild_id="forum_guild_123",
                name="Test Agent",
                system_prompt="Test prompt",
                created_by="test_admin"
            )
            
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
            
            # Test defaults
            assert agent.description == ""
            assert agent.monitored_forums == []
            assert agent.is_active is True
            assert agent.response_threshold == 0.7
            assert agent.max_responses_per_hour == 5
    
    async def test_forum_agent_guild_name_unique_constraint(self, test_engine):
        """Test that agent names must be unique within a guild."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Create first agent
            agent1 = ForumAgent(
                guild_id="forum_guild_123",
                name="Duplicate Name",
                system_prompt="First agent",
                created_by="test_admin"
            )
            session.add(agent1)
            await session.commit()
            
            # Try to create second agent with same guild_id and name
            agent2 = ForumAgent(
                guild_id="forum_guild_123",
                name="Duplicate Name",
                system_prompt="Second agent",
                created_by="test_admin"
            )
            session.add(agent2)
            
            with pytest.raises(IntegrityError):
                await session.commit()
    
    async def test_forum_agent_allows_same_name_different_guild(self, test_engine):
        """Test that agent names can be same across different guilds."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Create agents with same name but different guilds
            agent1 = ForumAgent(
                guild_id="forum_guild_123",
                name="Same Name",
                system_prompt="First agent",
                created_by="test_admin"
            )
            
            agent2 = ForumAgent(
                guild_id="forum_guild_456",
                name="Same Name",
                system_prompt="Second agent",
                created_by="test_admin"
            )
            
            session.add(agent1)
            session.add(agent2)
            await session.commit()
            
            # Should succeed - both agents created
            await session.refresh(agent1)
            await session.refresh(agent2)
            assert agent1.id != agent2.id
            assert agent1.guild_id != agent2.guild_id
            assert agent1.name == agent2.name
    
    async def test_forum_agent_json_fields_serialization(self, test_engine):
        """Test that JSON fields properly serialize and deserialize."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            test_forums = ["123456789", "987654321", "555555555"]
            
            agent = ForumAgent(
                guild_id="forum_guild_123",
                name="JSON Test Agent",
                system_prompt="Test JSON serialization",
                monitored_forums=test_forums,
                created_by="test_admin"
            )
            
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
            
            # Verify JSON field is properly stored and retrieved
            assert agent.monitored_forums == test_forums
            assert isinstance(agent.monitored_forums, list)
            assert len(agent.monitored_forums) == 3
    
    async def test_forum_agent_validation_constraints(self, test_engine):
        """Test that validation constraints are enforced."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Test response_threshold bounds
            agent = ForumAgent(
                guild_id="forum_guild_123",
                name="Validation Test",
                system_prompt="Test validation",
                response_threshold=1.5,  # Invalid - should be 0.0-1.0
                created_by="test_admin"
            )
            
            session.add(agent)
            
            with pytest.raises(IntegrityError):
                await session.commit()
    
    async def test_forum_agent_updated_at_auto_update(self, test_engine):
        """Test that updated_at is automatically updated on changes."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent
        import asyncio
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            agent = ForumAgent(
                guild_id="forum_guild_123",
                name="Update Test Agent",
                system_prompt="Test auto-update",
                created_by="test_admin"
            )
            
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
            
            original_updated_at = agent.updated_at
            
            # Wait a small amount to ensure timestamp difference
            await asyncio.sleep(0.01)
            
            # Update the agent
            agent.description = "Updated description"
            await session.commit()
            await session.refresh(agent)
            
            # updated_at should be different (newer)
            assert agent.updated_at > original_updated_at