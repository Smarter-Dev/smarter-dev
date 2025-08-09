"""Test cases for the ForumAgentResponse model."""

from __future__ import annotations

import pytest
from uuid import UUID
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from smarter_dev.shared.database import Base


class TestForumAgentResponse:
    """Test cases for ForumAgentResponse model functionality."""
    
    async def test_forum_agent_response_model_exists(self):
        """Test that ForumAgentResponse model can be imported."""
        try:
            from smarter_dev.web.models import ForumAgentResponse
            assert ForumAgentResponse is not None
        except ImportError:
            pytest.fail("ForumAgentResponse model does not exist")
    
    async def test_forum_agent_response_has_uuid_primary_key(self):
        """Test that ForumAgentResponse has UUID primary key."""
        from smarter_dev.web.models import ForumAgentResponse
        
        pk_columns = [col.name for col in ForumAgentResponse.__table__.primary_key.columns]
        assert "id" in pk_columns
        assert len(pk_columns) == 1
    
    async def test_forum_agent_response_required_fields(self):
        """Test that ForumAgentResponse has all required fields."""
        from smarter_dev.web.models import ForumAgentResponse
        
        required_fields = [
            "id", "agent_id", "guild_id", "channel_id", "thread_id",
            "post_title", "post_content", "author_display_name", 
            "post_tags", "attachments", "decision_reason", 
            "confidence_score", "response_content", "tokens_used", 
            "response_time_ms", "created_at", "responded", "responded_at"
        ]
        
        for field_name in required_fields:
            assert hasattr(ForumAgentResponse, field_name), f"Missing field: {field_name}"
    
    async def test_forum_agent_response_creation_and_retrieval(self, test_engine):
        """Test creating and retrieving ForumAgentResponse records."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent, ForumAgentResponse
        from uuid import uuid4
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # First create a forum agent to reference
            agent = ForumAgent(
                guild_id="forum_guild_123",
                name="Test Agent",
                system_prompt="Test prompt",
                created_by="test_admin"
            )
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
            
            # Create forum agent response
            response = ForumAgentResponse(
                agent_id=agent.id,
                guild_id="forum_guild_123",
                channel_id="123456789",
                thread_id="987654321",
                post_title="Test Forum Post",
                post_content="This is a test post content",
                author_display_name="TestUser",
                post_tags=["help", "question"],
                attachments=["image1.png", "document.pdf"],
                decision_reason="High confidence response warranted",
                confidence_score=0.85,
                response_content="Here's my helpful response",
                tokens_used=150,
                response_time_ms=1200,
                responded=True
            )
            
            session.add(response)
            await session.commit()
            await session.refresh(response)
            
            # Verify creation
            assert response.agent_id == agent.id
            assert response.guild_id == "forum_guild_123"
            assert response.channel_id == "123456789"
            assert response.thread_id == "987654321"
            assert response.post_title == "Test Forum Post"
            assert response.post_content == "This is a test post content"
            assert response.author_display_name == "TestUser"
            assert response.post_tags == ["help", "question"]
            assert response.attachments == ["image1.png", "document.pdf"]
            assert response.decision_reason == "High confidence response warranted"
            assert response.confidence_score == 0.85
            assert response.response_content == "Here's my helpful response"
            assert response.tokens_used == 150
            assert response.response_time_ms == 1200
            assert response.responded is True
            assert isinstance(response.id, UUID)
            assert isinstance(response.created_at, datetime)
    
    async def test_forum_agent_response_table_name(self):
        """Test that ForumAgentResponse has correct table name."""
        from smarter_dev.web.models import ForumAgentResponse
        
        assert ForumAgentResponse.__tablename__ == "forum_agent_responses"
    
    async def test_forum_agent_response_default_values(self, test_engine):
        """Test that ForumAgentResponse has correct default values."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent, ForumAgentResponse
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Create agent first
            agent = ForumAgent(
                guild_id="forum_guild_123",
                name="Test Agent",
                system_prompt="Test prompt",
                created_by="test_admin"
            )
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
            
            # Create response with minimal fields
            response = ForumAgentResponse(
                agent_id=agent.id,
                guild_id="forum_guild_123",
                channel_id="123456789",
                thread_id="987654321",
                post_title="Test Post",
                post_content="Test content",
                author_display_name="TestUser",
                decision_reason="Test decision",
                confidence_score=0.5,
                tokens_used=100,
                response_time_ms=1000
            )
            
            session.add(response)
            await session.commit()
            await session.refresh(response)
            
            # Test defaults
            assert response.post_tags == []
            assert response.attachments == []
            assert response.response_content == ""
            assert response.responded is False
            assert response.responded_at is None
    
    async def test_forum_agent_response_foreign_key_constraint(self, test_engine):
        """Test that agent_id foreign key constraint is enforced."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgentResponse
        from uuid import uuid4
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Try to create response with non-existent agent_id
            response = ForumAgentResponse(
                agent_id=uuid4(),  # Non-existent agent
                guild_id="forum_guild_123",
                channel_id="123456789",
                thread_id="987654321",
                post_title="Test Post",
                post_content="Test content",
                author_display_name="TestUser",
                decision_reason="Test decision",
                confidence_score=0.5,
                tokens_used=100,
                response_time_ms=1000
            )
            
            session.add(response)
            
            with pytest.raises(IntegrityError):
                await session.commit()
    
    async def test_forum_agent_response_cascade_delete(self, test_engine):
        """Test that responses are deleted when agent is deleted."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from sqlalchemy import select
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent, ForumAgentResponse
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Create agent and response
            agent = ForumAgent(
                guild_id="forum_guild_123",
                name="Test Agent",
                system_prompt="Test prompt",
                created_by="test_admin"
            )
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
            
            response = ForumAgentResponse(
                agent_id=agent.id,
                guild_id="forum_guild_123",
                channel_id="123456789",
                thread_id="987654321",
                post_title="Test Post",
                post_content="Test content",
                author_display_name="TestUser",
                decision_reason="Test decision",
                confidence_score=0.5,
                tokens_used=100,
                response_time_ms=1000
            )
            session.add(response)
            await session.commit()
            
            # Verify response exists
            result = await session.execute(
                select(ForumAgentResponse).where(ForumAgentResponse.agent_id == agent.id)
            )
            assert result.scalar_one() is not None
            
            # Delete the agent
            await session.delete(agent)
            await session.commit()
            
            # Verify response was also deleted (cascade)
            result = await session.execute(
                select(ForumAgentResponse).where(ForumAgentResponse.agent_id == agent.id)
            )
            assert result.scalar_one_or_none() is None
    
    async def test_forum_agent_response_json_fields_serialization(self, test_engine):
        """Test that JSON fields properly serialize and deserialize."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent, ForumAgentResponse
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Create agent
            agent = ForumAgent(
                guild_id="forum_guild_123",
                name="Test Agent",
                system_prompt="Test prompt",
                created_by="test_admin"
            )
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
            
            # Test complex JSON data
            test_tags = ["help", "question", "urgent"]
            test_attachments = ["screenshot.png", "logs.txt", "config.json"]
            
            response = ForumAgentResponse(
                agent_id=agent.id,
                guild_id="forum_guild_123",
                channel_id="123456789",
                thread_id="987654321",
                post_title="JSON Test Post",
                post_content="Test JSON serialization",
                author_display_name="TestUser",
                post_tags=test_tags,
                attachments=test_attachments,
                decision_reason="Test decision",
                confidence_score=0.8,
                tokens_used=100,
                response_time_ms=1000
            )
            
            session.add(response)
            await session.commit()
            await session.refresh(response)
            
            # Verify JSON fields are properly stored and retrieved
            assert response.post_tags == test_tags
            assert response.attachments == test_attachments
            assert isinstance(response.post_tags, list)
            assert isinstance(response.attachments, list)
            assert len(response.post_tags) == 3
            assert len(response.attachments) == 3
    
    async def test_forum_agent_response_confidence_score_validation(self, test_engine):
        """Test that confidence_score validation is enforced."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent, ForumAgentResponse
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Create agent
            agent = ForumAgent(
                guild_id="forum_guild_123",
                name="Test Agent",
                system_prompt="Test prompt",
                created_by="test_admin"
            )
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
            
            # Test invalid confidence score (> 1.0)
            response = ForumAgentResponse(
                agent_id=agent.id,
                guild_id="forum_guild_123",
                channel_id="123456789",
                thread_id="987654321",
                post_title="Test Post",
                post_content="Test content",
                author_display_name="TestUser",
                decision_reason="Test decision",
                confidence_score=1.5,  # Invalid - should be 0.0-1.0
                tokens_used=100,
                response_time_ms=1000
            )
            
            session.add(response)
            
            with pytest.raises(IntegrityError):
                await session.commit()
    
    async def test_forum_agent_response_responded_at_auto_set(self, test_engine):
        """Test that responded_at is automatically set when responded is True."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import ForumAgent, ForumAgentResponse
        import asyncio
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Create agent
            agent = ForumAgent(
                guild_id="forum_guild_123",
                name="Test Agent",
                system_prompt="Test prompt",
                created_by="test_admin"
            )
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
            
            # Create response without responding
            response = ForumAgentResponse(
                agent_id=agent.id,
                guild_id="forum_guild_123",
                channel_id="123456789",
                thread_id="987654321",
                post_title="Test Post",
                post_content="Test content",
                author_display_name="TestUser",
                decision_reason="Test decision",
                confidence_score=0.8,
                tokens_used=100,
                response_time_ms=1000,
                responded=False
            )
            
            session.add(response)
            await session.commit()
            await session.refresh(response)
            
            assert response.responded is False
            assert response.responded_at is None
            
            # Wait a small amount to ensure timestamp difference
            await asyncio.sleep(0.01)
            
            # Update to responded
            response.responded = True
            response.response_content = "My response"
            await session.commit()
            await session.refresh(response)
            
            # responded_at should be automatically set
            assert response.responded is True
            assert response.responded_at is not None
            assert isinstance(response.responded_at, datetime)