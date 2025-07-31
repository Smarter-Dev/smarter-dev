"""Test cases for the BytesConfig model."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.database import Base


class TestBytesConfig:
    """Test cases for BytesConfig model functionality."""
    
    async def test_bytes_config_model_exists(self):
        """Test that BytesConfig model can be imported."""
        try:
            from smarter_dev.web.models import BytesConfig
            assert BytesConfig is not None
        except ImportError:
            pytest.fail("BytesConfig model does not exist")
    
    async def test_bytes_config_has_guild_id_primary_key(self):
        """Test that BytesConfig has guild_id as primary key."""
        from smarter_dev.web.models import BytesConfig
        
        pk_columns = [col.name for col in BytesConfig.__table__.primary_key.columns]
        assert "guild_id" in pk_columns
        assert len(pk_columns) == 1
    
    async def test_bytes_config_required_fields(self):
        """Test that BytesConfig has all required fields per Session 2 specification."""
        from smarter_dev.web.models import BytesConfig
        
        required_fields = [
            "guild_id", "starting_balance", "daily_amount", "streak_bonuses",
            "max_transfer", "transfer_cooldown_hours", "role_rewards"
        ]
        
        for field_name in required_fields:
            assert hasattr(BytesConfig, field_name), f"Missing field: {field_name}"
    
    async def test_bytes_config_field_defaults(self):
        """Test that BytesConfig has correct default values per specification."""
        from smarter_dev.web.models import BytesConfig
        
        config = BytesConfig(guild_id="123456789")
        
        # Check default values per Session 2 specification
        assert config.starting_balance == 100
        assert config.daily_amount == 10
        assert config.streak_bonuses == {8: 2, 16: 4, 32: 8, 64: 16}
        assert config.max_transfer == 1000
        assert config.transfer_cooldown_hours == 0
        assert config.role_rewards == {}
    
    async def test_bytes_config_creation_and_retrieval(self, test_engine):
        """Test creating and retrieving BytesConfig records."""
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from smarter_dev.shared.database import Base
        from smarter_dev.web.models import BytesConfig
        
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_maker() as session:
            config = BytesConfig(
                guild_id="config_test_123",
                starting_balance=200,
                daily_amount=15,
                streak_bonuses={"10": 3, "20": 5},
                max_transfer=2000,
                transfer_cooldown_hours=2,
                role_rewards={"role_123": 50}
            )
            
            session.add(config)
            await session.commit()
            await session.refresh(config)
            
            # Verify creation per Session 2 specification
            assert config.guild_id == "config_test_123"
            assert config.starting_balance == 200
            assert config.daily_amount == 15
            assert config.streak_bonuses == {"10": 3, "20": 5}
            assert config.max_transfer == 2000
            assert config.transfer_cooldown_hours == 2
            assert config.role_rewards == {"role_123": 50}
    
    async def test_bytes_config_table_name(self):
        """Test that BytesConfig has correct table name."""
        from smarter_dev.web.models import BytesConfig
        
        assert BytesConfig.__tablename__ == "bytes_configs"