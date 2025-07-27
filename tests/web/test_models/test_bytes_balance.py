"""Test cases for the BytesBalance model."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, date
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from smarter_dev.shared.database import Base


class TestBytesBalance:
    """Test cases for BytesBalance model functionality."""
    
    async def test_bytes_balance_model_exists(self):
        """Test that BytesBalance model can be imported."""
        # This test will fail until we create the BytesBalance model
        try:
            from smarter_dev.web.models import BytesBalance
            assert BytesBalance is not None
        except ImportError:
            pytest.fail("BytesBalance model does not exist")
    
    async def test_bytes_balance_has_compound_primary_key(self):
        """Test that BytesBalance has compound primary key (guild_id, user_id)."""
        from smarter_dev.web.models import BytesBalance
        
        # Should have guild_id and user_id as primary key components
        pk_columns = [col.name for col in BytesBalance.__table__.primary_key.columns]
        assert "guild_id" in pk_columns
        assert "user_id" in pk_columns
        assert len(pk_columns) == 2
    
    async def test_bytes_balance_required_fields(self):
        """Test that BytesBalance has all required fields."""
        from smarter_dev.web.models import BytesBalance
        
        # Check that model has required fields
        required_fields = [
            "guild_id", "user_id", "balance", "total_received", 
            "total_sent", "streak_count", "last_daily"
        ]
        
        for field_name in required_fields:
            assert hasattr(BytesBalance, field_name), f"Missing field: {field_name}"
    
    async def test_bytes_balance_field_types(self):
        """Test that BytesBalance fields have correct types."""
        from smarter_dev.web.models import BytesBalance
        
        # Get table columns
        table = BytesBalance.__table__
        
        # Check specific field types
        assert table.columns["guild_id"].type.python_type == str
        assert table.columns["user_id"].type.python_type == str
        assert table.columns["balance"].type.python_type == int
        assert table.columns["total_received"].type.python_type == int
        assert table.columns["total_sent"].type.python_type == int
        assert table.columns["streak_count"].type.python_type == int
        # last_daily should be nullable datetime
        assert table.columns["last_daily"].nullable is True
    
    async def test_bytes_balance_field_defaults(self):
        """Test that BytesBalance fields have correct default values."""
        from smarter_dev.web.models import BytesBalance
        
        # Create instance without specifying optional fields
        balance = BytesBalance(guild_id="123", user_id="456")
        
        # Check default values
        assert balance.balance == 0
        assert balance.total_received == 0
        assert balance.total_sent == 0
        assert balance.streak_count == 0
        assert balance.last_daily is None
    
    async def test_bytes_balance_not_null_constraints(self):
        """Test that BytesBalance has correct NOT NULL constraints."""
        from smarter_dev.web.models import BytesBalance
        
        table = BytesBalance.__table__
        
        # These fields should NOT be nullable
        non_nullable_fields = [
            "guild_id", "user_id", "balance", "total_received", 
            "total_sent", "streak_count"
        ]
        
        for field_name in non_nullable_fields:
            assert table.columns[field_name].nullable is False, f"{field_name} should not be nullable"
        
        # last_daily should be nullable
        assert table.columns["last_daily"].nullable is True
    
    async def test_bytes_balance_creation_and_retrieval(self, test_engine):
        """Test creating and retrieving BytesBalance records."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import BytesBalance
        
        # Ensure tables exist
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Create BytesBalance record
            balance = BytesBalance(
                guild_id="123456789",
                user_id="987654321",
                balance=100,
                total_received=200,
                total_sent=100,
                streak_count=5,
                last_daily=date.today()
            )
            
            session.add(balance)
            await session.commit()
            await session.refresh(balance)
            
            # Verify creation
            assert balance.guild_id == "123456789"
            assert balance.user_id == "987654321"
            assert balance.balance == 100
            assert balance.total_received == 200
            assert balance.total_sent == 100
            assert balance.streak_count == 5
            assert balance.last_daily is not None
            
            # Verify it inherited timestamp fields from Base
            assert balance.created_at is not None
            assert balance.updated_at is not None
    
    async def test_bytes_balance_compound_pk_uniqueness(self, test_engine, unique_guild_id, unique_user_id):
        """Test that compound primary key enforces uniqueness."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import BytesBalance
        from sqlalchemy.exc import IntegrityError
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Create first record
            balance1 = BytesBalance(guild_id=unique_guild_id, user_id=unique_user_id, balance=100)
            session.add(balance1)
            await session.commit()
            
            # Clear identity map to avoid conflicts
            session.expunge_all()
            
            # Try to create duplicate record with same guild_id and user_id
            balance2 = BytesBalance(guild_id=unique_guild_id, user_id=unique_user_id, balance=200)
            session.add(balance2)
            
            # Should raise IntegrityError due to primary key violation
            with pytest.raises(IntegrityError):
                await session.commit()
    
    async def test_bytes_balance_allows_same_user_different_guilds(self, test_engine):
        """Test that same user can have balances in different guilds."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import BytesBalance
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Create records for same user in different guilds
            balance1 = BytesBalance(guild_id="guild_test_123", user_id="same_user_456", balance=100)
            balance2 = BytesBalance(guild_id="guild_test_789", user_id="same_user_456", balance=200)
            
            session.add(balance1)
            session.add(balance2)
            await session.commit()
            
            # Both should be created successfully
            await session.refresh(balance1)
            await session.refresh(balance2)
            
            assert balance1.guild_id == "guild_test_123"
            assert balance1.user_id == "same_user_456"
            assert balance1.balance == 100
            
            assert balance2.guild_id == "guild_test_789"
            assert balance2.user_id == "same_user_456"
            assert balance2.balance == 200
    
    async def test_bytes_balance_table_name(self):
        """Test that BytesBalance has correct table name."""
        from smarter_dev.web.models import BytesBalance
        
        assert BytesBalance.__tablename__ == "bytes_balances"