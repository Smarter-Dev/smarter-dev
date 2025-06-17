# Session 2: Database Schema & Models

## Objective
Create the database schema using SQLAlchemy with async support and Alembic for migrations. Focus on minimal data storage - no Discord data duplication.

## Prerequisites
- Completed Session 1 (configuration system exists)
- PostgreSQL running via Docker
- Understanding of the minimal data storage principle

## Task 1: Database Connection Setup

### web/database.py

Create the async database engine and session management:

```python
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import MetaData
from contextlib import asynccontextmanager
from typing import AsyncGenerator
import structlog

from web.config import WebConfig

logger = structlog.get_logger()

# Naming convention for consistent migrations
naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}

metadata = MetaData(naming_convention=naming_convention)
Base = declarative_base(metadata=metadata)

class Database:
    """Database connection manager."""
    
    def __init__(self, config: WebConfig):
        self.config = config
        self.engine = None
        self.session_factory = None
    
    async def connect(self):
        """Initialize database connection."""
        self.engine = create_async_engine(
            self.config.database_url,
            pool_size=self.config.database_pool_size,
            max_overflow=self.config.database_max_overflow,
            pool_pre_ping=True,  # Verify connections are alive
            echo=self.config.dev_mode,  # SQL logging in dev mode
        )
        
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False
        )
        
        logger.info("Database connected", url=self.config.database_url.split("@")[1])
    
    async def disconnect(self):
        """Close database connection."""
        if self.engine:
            await self.engine.dispose()
            logger.info("Database disconnected")
    
    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get a database session."""
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()
    
    async def health_check(self) -> bool:
        """Check if database is accessible."""
        try:
            async with self.session() as session:
                await session.execute("SELECT 1")
                return True
        except Exception as e:
            logger.error("Database health check failed", error=str(e))
            return False

# Global database instance (initialized in app startup)
db: Database = None

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency to get database session."""
    async with db.session() as session:
        yield session
```

## Task 2: Base Model Mixins

### web/models/base.py

Create base model mixins for common fields:

```python
from sqlalchemy import Column, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.declarative import declared_attr
import uuid

class TimestampMixin:
    """Mixin for created_at and updated_at timestamps."""
    
    @declared_attr
    def created_at(cls):
        return Column(
            DateTime(timezone=True),
            server_default=func.now(),
            nullable=False
        )
    
    @declared_attr
    def updated_at(cls):
        return Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False
        )

class UUIDPrimaryKeyMixin:
    """Mixin for UUID primary key."""
    
    @declared_attr
    def id(cls):
        return Column(
            UUID(as_uuid=True),
            primary_key=True,
            default=uuid.uuid4,
            nullable=False
        )

class DiscordSnowflakeMixin:
    """Mixin for Discord ID fields."""
    
    @staticmethod
    def snowflake_column(nullable=False, index=True):
        """Create a Discord snowflake column."""
        return Column(
            String(20),  # Discord IDs are up to 20 characters
            nullable=nullable,
            index=index
        )
```

## Task 3: Core Models

### web/models/bytes.py

Bytes economy models:

```python
from sqlalchemy import Column, Integer, String, Date, Text, JSON, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import UUID
from web.database import Base
from .base import TimestampMixin, UUIDPrimaryKeyMixin, DiscordSnowflakeMixin
import uuid

class BytesBalance(Base, TimestampMixin):
    """User balance tracking per guild."""
    __tablename__ = "bytes_balances"
    
    # Composite primary key
    guild_id = DiscordSnowflakeMixin.snowflake_column(primary_key=True)
    user_id = DiscordSnowflakeMixin.snowflake_column(primary_key=True)
    
    # Balance tracking
    balance = Column(Integer, nullable=False, default=0)
    total_received = Column(Integer, nullable=False, default=0)
    total_sent = Column(Integer, nullable=False, default=0)
    
    # Daily tracking
    last_daily = Column(Date, nullable=True)
    streak_count = Column(Integer, nullable=False, default=0)
    
    # Indexes for common queries
    __table_args__ = (
        Index("ix_bytes_balance_guild_balance", "guild_id", "balance"),  # For leaderboards
        Index("ix_bytes_balance_last_daily", "last_daily"),  # For daily cleanup
    )

class BytesTransaction(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Log of all bytes transfers."""
    __tablename__ = "bytes_transactions"
    
    guild_id = DiscordSnowflakeMixin.snowflake_column()
    
    # Giver info (cached for audit)
    giver_id = DiscordSnowflakeMixin.snowflake_column()
    giver_username = Column(String(100), nullable=False)
    
    # Receiver info (cached for audit)
    receiver_id = DiscordSnowflakeMixin.snowflake_column()
    receiver_username = Column(String(100), nullable=False)
    
    amount = Column(Integer, nullable=False)
    reason = Column(Text, nullable=True)
    
    # Indexes for history queries
    __table_args__ = (
        Index("ix_bytes_transaction_guild_created", "guild_id", "created_at"),
        Index("ix_bytes_transaction_giver", "giver_id", "created_at"),
        Index("ix_bytes_transaction_receiver", "receiver_id", "created_at"),
    )

class BytesConfig(Base, TimestampMixin):
    """Per-guild economy configuration."""
    __tablename__ = "bytes_configs"
    
    guild_id = DiscordSnowflakeMixin.snowflake_column(primary_key=True)
    
    # Economy settings
    starting_balance = Column(Integer, nullable=False, default=100)
    daily_amount = Column(Integer, nullable=False, default=10)
    max_transfer = Column(Integer, nullable=False, default=1000)
    cooldown_hours = Column(Integer, nullable=False, default=24)
    
    # Role rewards: {role_id: bytes_threshold}
    role_rewards = Column(JSON, nullable=False, default=dict)
```

### web/models/squads.py

Squad system models:

```python
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from web.database import Base
from .base import TimestampMixin, UUIDPrimaryKeyMixin, DiscordSnowflakeMixin

class Squad(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Team definitions per guild."""
    __tablename__ = "squads"
    
    guild_id = DiscordSnowflakeMixin.snowflake_column()
    role_id = DiscordSnowflakeMixin.snowflake_column()  # Discord role
    
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    switch_cost = Column(Integer, nullable=False, default=50)
    is_active = Column(Boolean, nullable=False, default=True)
    
    # Relationships
    members = relationship("SquadMembership", back_populates="squad", cascade="all, delete-orphan")
    
    __table_args__ = (
        UniqueConstraint("guild_id", "role_id", name="uq_squad_guild_role"),
        Index("ix_squad_guild_active", "guild_id", "is_active"),
    )

class SquadMembership(Base, TimestampMixin):
    """Track squad membership."""
    __tablename__ = "squad_memberships"
    
    # Composite primary key
    guild_id = DiscordSnowflakeMixin.snowflake_column(primary_key=True)
    user_id = DiscordSnowflakeMixin.snowflake_column(primary_key=True)
    
    squad_id = Column(UUID(as_uuid=True), ForeignKey("squads.id"), nullable=False)
    
    # Relationships
    squad = relationship("Squad", back_populates="members")
    
    __table_args__ = (
        Index("ix_squad_membership_squad", "squad_id"),
    )
```

### web/models/moderation.py

Moderation system models:

```python
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum, JSON, Index
from sqlalchemy.dialects.postgresql import UUID
from web.database import Base
from .base import TimestampMixin, UUIDPrimaryKeyMixin, DiscordSnowflakeMixin
from shared.types import ModerationAction, AutoModRuleType
import enum

class ModerationCase(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Log of moderation actions."""
    __tablename__ = "moderation_cases"
    
    guild_id = DiscordSnowflakeMixin.snowflake_column()
    
    # User info (cached for history)
    user_id = DiscordSnowflakeMixin.snowflake_column()
    user_tag = Column(String(100), nullable=False)  # Username#0000 at time
    
    # Moderator info (cached)
    moderator_id = DiscordSnowflakeMixin.snowflake_column()
    moderator_tag = Column(String(100), nullable=False)
    
    action = Column(Enum(ModerationAction), nullable=False)
    reason = Column(String(500), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)  # For timeouts
    resolved = Column(Boolean, nullable=False, default=False)
    
    __table_args__ = (
        Index("ix_moderation_case_guild_created", "guild_id", "created_at"),
        Index("ix_moderation_case_user", "user_id"),
        Index("ix_moderation_case_resolved", "resolved", "expires_at"),
    )

class AutoModRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Auto-moderation rule definitions."""
    __tablename__ = "automod_rules"
    
    guild_id = DiscordSnowflakeMixin.snowflake_column()
    
    rule_type = Column(Enum(AutoModRuleType), nullable=False)
    config = Column(JSON, nullable=False)  # Rule-specific configuration
    action = Column(Enum(ModerationAction), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    
    # Rule ordering
    priority = Column(Integer, nullable=False, default=0)
    
    __table_args__ = (
        Index("ix_automod_rule_guild_active", "guild_id", "is_active", "priority"),
    )
```

### web/models/admin.py

Admin and API models:

```python
from sqlalchemy import Column, String, Boolean, DateTime, Index
from sqlalchemy.dialects.postgresql import UUID
from web.database import Base
from .base import TimestampMixin, UUIDPrimaryKeyMixin
import hashlib

class AdminUser(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Admin users for development mode."""
    __tablename__ = "admin_users"
    
    username = Column(String(50), unique=True, nullable=False)
    
    __table_args__ = (
        Index("ix_admin_user_username", "username"),
    )

class APIKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """API keys for bot authentication."""
    __tablename__ = "api_keys"
    
    key_hash = Column(String(64), unique=True, nullable=False)  # SHA256 hash
    name = Column(String(100), nullable=False)
    last_used = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    
    @staticmethod
    def hash_key(key: str) -> str:
        """Hash an API key for storage."""
        return hashlib.sha256(key.encode()).hexdigest()
    
    __table_args__ = (
        Index("ix_api_key_hash", "key_hash"),
        Index("ix_api_key_active", "is_active"),
    )
```

## Task 4: Alembic Setup

### alembic.ini

Create Alembic configuration:

```ini
[alembic]
script_location = web/migrations
prepend_sys_path = .
version_path_separator = os
sqlalchemy.url = driver://user:pass@localhost/dbname  # Overridden by env.py

[post_write_hooks]
hooks = black
black.type = console_subprocess
black.entrypoint = black
black.options = -l 88

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

### web/migrations/env.py

Configure Alembic environment:

```python
import asyncio
from logging.config import fileConfig
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context
import os
import sys

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from web.config import WebConfig
from web.database import Base

# Import all models to ensure they're registered
from web.models import bytes, squads, moderation, admin

config = context.config
fileConfig(config.config_file_name)

# Load our config
web_config = WebConfig()
config.set_main_option("sqlalchemy.url", web_config.database_url)

target_metadata = Base.metadata

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()

def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

## Task 5: Create Initial Migration

Run these commands to create the initial migration:

```bash
# Initialize Alembic
alembic init web/migrations

# Create initial migration
alembic revision --autogenerate -m "Initial schema"

# Apply migration
alembic upgrade head
```

## Task 6: Database Access Layer

### web/crud/base.py

Create base CRUD operations:

```python
from typing import TypeVar, Generic, Type, Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func
from web.database import Base

ModelType = TypeVar("ModelType", bound=Base)

class CRUDBase(Generic[ModelType]):
    """Base class for CRUD operations."""
    
    def __init__(self, model: Type[ModelType]):
        self.model = model
    
    async def get(self, db: AsyncSession, **kwargs) -> Optional[ModelType]:
        """Get single record by filters."""
        query = select(self.model).filter_by(**kwargs)
        result = await db.execute(query)
        return result.scalar_one_or_none()
    
    async def get_multi(
        self, 
        db: AsyncSession, 
        skip: int = 0, 
        limit: int = 100,
        **kwargs
    ) -> List[ModelType]:
        """Get multiple records."""
        query = select(self.model).filter_by(**kwargs).offset(skip).limit(limit)
        result = await db.execute(query)
        return result.scalars().all()
    
    async def create(self, db: AsyncSession, **kwargs) -> ModelType:
        """Create new record."""
        db_obj = self.model(**kwargs)
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj
    
    async def update(
        self, 
        db: AsyncSession, 
        db_obj: ModelType, 
        **kwargs
    ) -> ModelType:
        """Update existing record."""
        for field, value in kwargs.items():
            setattr(db_obj, field, value)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj
    
    async def delete(self, db: AsyncSession, db_obj: ModelType) -> None:
        """Delete record."""
        await db.delete(db_obj)
        await db.commit()
    
    async def count(self, db: AsyncSession, **kwargs) -> int:
        """Count records matching filters."""
        query = select(func.count()).select_from(self.model).filter_by(**kwargs)
        result = await db.execute(query)
        return result.scalar()
```

### web/crud/bytes.py

Bytes-specific database operations:

```python
from typing import Optional, List, Tuple
from datetime import date
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, and_, desc
from web.models.bytes import BytesBalance, BytesTransaction, BytesConfig
from .base import CRUDBase

class BytesCRUD(CRUDBase[BytesBalance]):
    """CRUD operations for bytes system."""
    
    async def get_or_create_balance(
        self, 
        db: AsyncSession, 
        guild_id: str, 
        user_id: str,
        starting_balance: int = 0
    ) -> BytesBalance:
        """Get or create user balance."""
        balance = await self.get(db, guild_id=guild_id, user_id=user_id)
        if not balance:
            balance = await self.create(
                db,
                guild_id=guild_id,
                user_id=user_id,
                balance=starting_balance,
                total_received=0,
                total_sent=0
            )
        return balance
    
    async def update_balance(
        self,
        db: AsyncSession,
        guild_id: str,
        user_id: str,
        amount: int,
        is_received: bool = True
    ) -> BytesBalance:
        """Update user balance and totals."""
        balance = await self.get_or_create_balance(db, guild_id, user_id)
        
        balance.balance += amount
        if is_received:
            balance.total_received += amount
        else:
            balance.total_sent += abs(amount)
        
        await db.commit()
        return balance
    
    async def award_daily(
        self,
        db: AsyncSession,
        guild_id: str,
        user_id: str,
        amount: int,
        today: date
    ) -> Tuple[BytesBalance, int]:
        """Award daily bytes and update streak."""
        balance = await self.get_or_create_balance(db, guild_id, user_id)
        
        # Calculate streak
        if balance.last_daily:
            days_diff = (today - balance.last_daily).days
            if days_diff == 1:
                balance.streak_count += 1
            elif days_diff > 1:
                balance.streak_count = 1
        else:
            balance.streak_count = 1
        
        # Update balance
        balance.balance += amount
        balance.total_received += amount
        balance.last_daily = today
        
        await db.commit()
        return balance, balance.streak_count
    
    async def get_leaderboard(
        self,
        db: AsyncSession,
        guild_id: str,
        limit: int = 10
    ) -> List[BytesBalance]:
        """Get top users by balance."""
        query = (
            select(BytesBalance)
            .where(BytesBalance.guild_id == guild_id)
            .order_by(desc(BytesBalance.balance))
            .limit(limit)
        )
        result = await db.execute(query)
        return result.scalars().all()

bytes_crud = BytesCRUD(BytesBalance)
transaction_crud = CRUDBase(BytesTransaction)
config_crud = CRUDBase(BytesConfig)
```

## Task 7: Create Tests

### tests/test_models.py

```python
import pytest
from datetime import date
from sqlalchemy.ext.asyncio import AsyncSession
from web.models.bytes import BytesBalance, BytesTransaction
from web.crud.bytes import bytes_crud
from shared.utils import utctoday

@pytest.mark.asyncio
async def test_bytes_balance_creation(test_db: AsyncSession):
    """Test creating bytes balance."""
    balance = await bytes_crud.create(
        test_db,
        guild_id="123",
        user_id="456",
        balance=100
    )
    
    assert balance.guild_id == "123"
    assert balance.user_id == "456"
    assert balance.balance == 100
    assert balance.total_received == 0
    assert balance.total_sent == 0

@pytest.mark.asyncio
async def test_daily_bytes_streak(test_db: AsyncSession):
    """Test daily bytes and streak calculation."""
    guild_id = "123"
    user_id = "456"
    today = utctoday()
    
    # First daily
    balance, streak = await bytes_crud.award_daily(
        test_db, guild_id, user_id, 10, today
    )
    assert balance.balance == 10
    assert streak == 1
    
    # Consecutive daily
    tomorrow = date(today.year, today.month, today.day + 1)
    balance, streak = await bytes_crud.award_daily(
        test_db, guild_id, user_id, 10, tomorrow
    )
    assert balance.balance == 20
    assert streak == 2

@pytest.mark.asyncio
async def test_leaderboard_query(test_db: AsyncSession):
    """Test leaderboard query."""
    guild_id = "123"
    
    # Create multiple users
    for i in range(5):
        await bytes_crud.create(
            test_db,
            guild_id=guild_id,
            user_id=str(i),
            balance=i * 100
        )
    
    # Get top 3
    leaderboard = await bytes_crud.get_leaderboard(test_db, guild_id, limit=3)
    
    assert len(leaderboard) == 3
    assert leaderboard[0].balance == 400
    assert leaderboard[1].balance == 300
    assert leaderboard[2].balance == 200
```

## Deliverables

1. **Database Setup**
   - Async SQLAlchemy engine
   - Session management
   - Health check functionality

2. **Model Definitions**
   - All tables with proper types
   - Indexes for performance
   - Relationships configured

3. **Alembic Configuration**
   - Migration environment
   - Initial migration created
   - Async migration support

4. **CRUD Operations**
   - Base CRUD class
   - Bytes-specific operations
   - Type-safe queries

5. **Test Coverage**
   - Model creation tests
   - Business logic tests
   - Query performance tests

## Important Notes

1. No Discord data duplication - only store IDs
2. All timestamps are UTC with timezone
3. Use composite indexes for common queries
4. Cache usernames only for audit purposes
5. Test with realistic data volumes
6. Consider query performance from the start

This schema provides the minimal data storage needed while maintaining audit trails and enabling all features.