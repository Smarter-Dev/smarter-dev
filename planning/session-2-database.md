# Session 2: Database Models and Migrations

**Goal:** Create minimal database schema for bytes and squads systems with proper testing

## Task Description

Create database models for the bytes and squads systems using SQLAlchemy with async support.

### Design Principles
- Store only necessary data (no Discord data duplication)
- Use Discord snowflake IDs as strings
- Include audit timestamps
- Efficient indexes for common queries
- Test all models and database operations

## Deliverables

### 1. web/models.py - SQLAlchemy models:

#### BytesBalance:
- guild_id: str (Discord snowflake)
- user_id: str (Discord snowflake)
- balance: int (current balance)
- total_received: int (lifetime received)
- total_sent: int (lifetime sent)
- last_daily: date (last daily claim)
- streak_count: int (consecutive days)
- created_at: datetime
- updated_at: datetime
- Primary key: (guild_id, user_id)

#### BytesTransaction:
- id: UUID
- guild_id: str
- giver_id: str
- giver_username: str (cached for audit)
- receiver_id: str
- receiver_username: str (cached for audit)
- amount: int
- reason: str (optional, max 200 chars)
- created_at: datetime
- Indexes: guild_id, created_at, giver_id, receiver_id

#### BytesConfig:
- guild_id: str (primary key)
- starting_balance: int (default: 100)
- daily_amount: int (default: 10)
- streak_bonuses: JSON (default: {8: 2, 16: 4, 32: 8, 64: 16})
- max_transfer: int (default: 1000)
- transfer_cooldown_hours: int (default: 0)
- role_rewards: JSON ({role_id: min_received_amount})
- created_at: datetime
- updated_at: datetime

#### Squad:
- id: UUID
- guild_id: str
- role_id: str (Discord role ID)
- name: str (max 100 chars)
- description: str (optional, max 500 chars)
- switch_cost: int (default: 50)
- max_members: int (optional)
- is_active: bool (default: true)
- created_at: datetime
- updated_at: datetime
- Unique: (guild_id, role_id)

#### SquadMembership:
- guild_id: str
- user_id: str
- squad_id: UUID
- joined_at: datetime
- Primary key: (guild_id, user_id)
- Foreign key: squad_id -> Squad.id

### 2. shared/database.py - Database setup:
- Async SQLAlchemy engine configuration
- Session factory with proper transaction handling
- Base model class with common fields
- Connection pool configuration

### 3. alembic.ini and alembic/env.py:
- Async migrations support
- Auto-generate from models
- Proper naming conventions

### 4. Initial migration:
- Create all tables with proper constraints
- Add indexes for performance
- Include helpful migration comments

### 5. tests/test_models.py - Comprehensive model tests:
- Test all model creation
- Test unique constraints
- Test cascade deletes
- Test JSON field serialization
- Test timestamp auto-update

### 6. tests/conftest.py - Test fixtures:
```python
@pytest.fixture
async def db_session():
    """Create a test database session with transaction rollback"""
    async with engine.begin() as conn:
        async with async_session(bind=conn) as session:
            yield session
            await session.rollback()

@pytest.fixture
async def test_data(db_session):
    """Create test data for bytes and squads"""
    # Create test guild configs
    # Create test balances
    # Create test squads
    return TestData(...)
```

### 7. web/crud.py - Database operations:
```python
class BytesOperations:
    async def get_balance(self, session, guild_id: str, user_id: str) -> BytesBalance:
        """Get or create user balance"""
        
    async def create_transaction(self, session, transaction: BytesTransactionCreate) -> BytesTransaction:
        """Create transaction and update balances atomically"""
        
    async def get_leaderboard(self, session, guild_id: str, limit: int = 10):
        """Get top users by balance"""

class SquadOperations:
    async def join_squad(self, session, guild_id: str, user_id: str, squad_id: UUID):
        """Join squad with bytes cost deduction"""
        
    async def get_user_squad(self, session, guild_id: str, user_id: str) -> Optional[Squad]:
        """Get user's current squad"""
```

### 8. tests/test_crud.py - Test all database operations:
- Test balance creation and updates
- Test transaction atomicity
- Test leaderboard queries
- Test squad membership changes
- Test concurrent operations

## Testing Approach
- Use pytest-asyncio for async tests
- Use database transactions that rollback after each test
- Test both happy paths and error cases
- Verify database constraints work properly
- Test performance with bulk operations

All tests should pass before moving to next session.