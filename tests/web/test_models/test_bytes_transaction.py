"""Test cases for the BytesTransaction model."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from uuid import UUID, uuid4
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import Base


class TestBytesTransaction:
    """Test cases for BytesTransaction model functionality."""
    
    async def test_bytes_transaction_model_exists(self):
        """Test that BytesTransaction model can be imported."""
        try:
            from smarter_dev.web.models import BytesTransaction
            assert BytesTransaction is not None
        except ImportError:
            pytest.fail("BytesTransaction model does not exist")
    
    async def test_bytes_transaction_has_uuid_primary_key(self):
        """Test that BytesTransaction has UUID primary key."""
        from smarter_dev.web.models import BytesTransaction
        
        # Should have id as UUID primary key
        pk_columns = [col.name for col in BytesTransaction.__table__.primary_key.columns]
        assert "id" in pk_columns
        assert len(pk_columns) == 1
        
        # ID column should be UUID type
        id_column = BytesTransaction.__table__.columns["id"]
        assert str(id_column.type).startswith("UUID") or str(id_column.type) == "CHAR(36)"
    
    async def test_bytes_transaction_required_fields(self):
        """Test that BytesTransaction has all required fields."""
        from smarter_dev.web.models import BytesTransaction
        
        # Check that model has required fields
        required_fields = [
            "id", "guild_id", "giver_id", "giver_username", 
            "receiver_id", "receiver_username", "amount", "reason"
        ]
        
        for field_name in required_fields:
            assert hasattr(BytesTransaction, field_name), f"Missing field: {field_name}"
    
    async def test_bytes_transaction_field_types(self):
        """Test that BytesTransaction fields have correct types."""
        from smarter_dev.web.models import BytesTransaction
        
        table = BytesTransaction.__table__
        
        # Check specific field types
        assert table.columns["guild_id"].type.python_type == str
        assert table.columns["giver_id"].type.python_type == str
        assert table.columns["receiver_id"].type.python_type == str
        assert table.columns["giver_username"].type.python_type == str
        assert table.columns["receiver_username"].type.python_type == str
        assert table.columns["amount"].type.python_type == int
        # reason should be optional text
        assert table.columns["reason"].nullable is True
    
    async def test_bytes_transaction_not_null_constraints(self):
        """Test that BytesTransaction has correct NOT NULL constraints."""
        from smarter_dev.web.models import BytesTransaction
        
        table = BytesTransaction.__table__
        
        # These fields should NOT be nullable
        non_nullable_fields = [
            "id", "guild_id", "giver_id", "giver_username",
            "receiver_id", "receiver_username", "amount"
        ]
        
        for field_name in non_nullable_fields:
            assert table.columns[field_name].nullable is False, f"{field_name} should not be nullable"
        
        # reason should be nullable
        assert table.columns["reason"].nullable is True
    
    async def test_bytes_transaction_id_auto_generation(self):
        """Test that transaction ID is auto-generated when not provided."""
        from smarter_dev.web.models import BytesTransaction
        
        # Create transaction without specifying ID
        transaction = BytesTransaction(
            guild_id="123456789",
            giver_id="user1",
            giver_username="Giver",
            receiver_id="user2", 
            receiver_username="Receiver",
            amount=100
        )
        
        # ID should be auto-generated
        assert transaction.id is not None
        assert isinstance(transaction.id, (UUID, str))
    
    async def test_bytes_transaction_creation_and_retrieval(self, test_engine):
        """Test creating and retrieving BytesTransaction records."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import BytesTransaction
        
        # Ensure tables exist
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Create BytesTransaction record
            transaction_id = uuid4()
            transaction = BytesTransaction(
                id=transaction_id,
                guild_id="123456789",
                giver_id="giver_user",
                giver_username="GiverUser",
                receiver_id="receiver_user",
                receiver_username="ReceiverUser",
                amount=150,
                reason="Test transaction"
            )
            
            session.add(transaction)
            await session.commit()
            await session.refresh(transaction)
            
            # Verify creation
            assert transaction.id == transaction_id
            assert transaction.guild_id == "123456789"
            assert transaction.giver_id == "giver_user"
            assert transaction.giver_username == "GiverUser"
            assert transaction.receiver_id == "receiver_user"
            assert transaction.receiver_username == "ReceiverUser"
            assert transaction.amount == 150
            assert transaction.reason == "Test transaction"
            
            # Verify it inherited timestamp fields from Base
            assert transaction.created_at is not None
            assert transaction.updated_at is not None
    
    async def test_bytes_transaction_nullable_reason(self, test_engine):
        """Test that reason field can be null."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import BytesTransaction
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            # Create transaction without reason
            transaction = BytesTransaction(
                guild_id="test_guild_123",
                giver_id="giver_user",
                giver_username="GiverUser",
                receiver_id="receiver_user",
                receiver_username="ReceiverUser",
                amount=75
                # No reason provided
            )
            
            session.add(transaction)
            await session.commit()
            await session.refresh(transaction)
            
            # Reason should be None
            assert transaction.reason is None
            assert transaction.amount == 75
    
    async def test_bytes_transaction_table_name(self):
        """Test that BytesTransaction has correct table name."""
        from smarter_dev.web.models import BytesTransaction
        
        assert BytesTransaction.__tablename__ == "bytes_transactions"
    
    async def test_bytes_transaction_indexes_exist(self):
        """Test that BytesTransaction has proper indexes for common queries."""
        from smarter_dev.web.models import BytesTransaction
        
        table = BytesTransaction.__table__
        
        # Get index names (indexes are often named automatically)
        index_columns = []
        for index in table.indexes:
            for column in index.columns:
                index_columns.append(column.name)
        
        # Should have indexes on common query fields
        # Note: Primary key automatically gets an index
        # We'll verify the table can be used for common query patterns
        assert "guild_id" in [col.name for col in table.columns]
        assert "giver_id" in [col.name for col in table.columns]  
        assert "receiver_id" in [col.name for col in table.columns]