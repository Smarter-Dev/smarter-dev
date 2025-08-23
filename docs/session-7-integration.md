# Session 7: Integration and End-to-End Testing

**Goal:** Create comprehensive tests and ensure all components work together

## Task Description

Create integration tests and ensure all components work together seamlessly.

### Requirements
1. Test bot ↔ API communication
2. Test admin → API → Redis → bot flow
3. Test error scenarios
4. Create fixtures for common test data
5. Document testing approach

## Deliverables

### 1. tests/conftest.py - Shared test fixtures:
```python
import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from unittest.mock import AsyncMock, Mock
import fakeredis.aioredis

from web.main import app
from web.models import Base
from shared.config import settings

# Override settings for testing
settings.DATABASE_URL = "postgresql+asyncpg://test:test@localhost/test_db"
settings.REDIS_URL = "redis://localhost/15"

@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="function")
async def db_engine():
    """Create test database engine"""
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_pre_ping=True
    )
    
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    yield engine
    
    # Drop tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    
    await engine.dispose()

@pytest.fixture(scope="function")
async def db_session(db_engine):
    """Create database session with transaction rollback"""
    async with AsyncSession(db_engine) as session:
        async with session.begin():
            yield session
            await session.rollback()

@pytest.fixture
async def redis_client():
    """Create fake Redis client for testing"""
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.close()

@pytest.fixture
async def api_client(db_session, redis_client):
    """Create API test client"""
    # Override dependencies
    app.state.db_session = db_session
    app.state.redis = redis_client
    
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as client:
        yield client

@pytest.fixture
def bot_token():
    """Valid bot token for testing"""
    return "test-bot-token-12345"

@pytest.fixture
def bot_headers(bot_token):
    """Bot authentication headers"""
    return {"Authorization": f"Bearer {bot_token}"}

@pytest.fixture
async def test_guild(db_session):
    """Create test guild configuration"""
    from web.models import BytesConfig
    
    config = BytesConfig(
        guild_id="123456789",
        starting_balance=100,
        daily_amount=10,
        max_transfer=1000,
        transfer_cooldown_hours=0,
        role_rewards={}
    )
    db_session.add(config)
    await db_session.commit()
    return config

@pytest.fixture
async def test_user_balance(db_session, test_guild):
    """Create test user with balance"""
    from web.models import BytesBalance
    
    balance = BytesBalance(
        guild_id=test_guild.guild_id,
        user_id="987654321",
        balance=500,
        total_received=600,
        total_sent=100,
        streak_count=7
    )
    db_session.add(balance)
    await db_session.commit()
    return balance

@pytest.fixture
def mock_discord_api():
    """Mock Discord API responses"""
    mock = AsyncMock()
    
    # Default responses
    mock.get_guild.return_value = {
        "id": "123456789",
        "name": "Test Guild",
        "icon": "abcdef",
        "owner_id": "111111111",
        "roles": [
            {"id": "123456789", "name": "@everyone", "color": 0},
            {"id": "111111111", "name": "Admin", "color": 0xFF0000},
            {"id": "222222222", "name": "Squad Alpha", "color": 0x00FF00},
            {"id": "333333333", "name": "Squad Beta", "color": 0x0000FF}
        ]
    }
    
    mock.get_bot_guilds.return_value = [
        {"id": "123456789", "name": "Test Guild", "icon": "abcdef"},
        {"id": "987654321", "name": "Another Guild", "icon": None}
    ]
    
    return mock

@pytest.fixture
def mock_bot_service():
    """Mock bot service for testing"""
    from bot.services.bytes_service import BytesService
    
    service = Mock(spec=BytesService)
    service.get_balance = AsyncMock()
    service.claim_daily = AsyncMock()
    service.transfer_bytes = AsyncMock()
    service.get_leaderboard = AsyncMock()
    
    return service
```

### 2. tests/integration/test_bytes_flow.py - End-to-end bytes tests:
```python
import pytest
from datetime import date, timedelta

class TestBytesIntegration:
    """Test complete bytes system flow"""
    
    async def test_daily_claim_flow(self, api_client, bot_headers, test_guild, redis_client):
        """Test daily bytes claim from bot to API to database"""
        user_id = "555555555"
        
        # First claim - should succeed
        response = await api_client.post(
            f"/api/guilds/{test_guild.guild_id}/bytes/daily/{user_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["balance"] == 110  # 100 starting + 10 daily
        assert data["streak_count"] == 1
        assert data["last_daily"] == str(date.today())
        
        # Check Redis pub/sub notification
        message = await redis_client.get(f"balance_update:{test_guild.guild_id}:{user_id}")
        assert message is not None
        
        # Second claim same day - should fail
        response = await api_client.post(
            f"/api/guilds/{test_guild.guild_id}/bytes/daily/{user_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 400
        assert "already claimed" in response.json()["detail"]
    
    async def test_streak_calculation(self, api_client, bot_headers, test_guild, db_session):
        """Test streak multiplier calculation"""
        user_id = "666666666"
        
        # Create user with existing streak
        from web.models import BytesBalance
        
        balance = BytesBalance(
            guild_id=test_guild.guild_id,
            user_id=user_id,
            balance=1000,
            total_received=1000,
            total_sent=0,
            streak_count=6,
            last_daily=date.today() - timedelta(days=1)
        )
        db_session.add(balance)
        await db_session.commit()
        
        # Claim daily - should get 2x multiplier at 7 days
        response = await api_client.post(
            f"/api/guilds/{test_guild.guild_id}/bytes/daily/{user_id}",
            headers=bot_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["balance"] == 1020  # 1000 + (10 * 2)
        assert data["streak_count"] == 7
    
    async def test_transfer_flow(self, api_client, bot_headers, test_guild, test_user_balance):
        """Test bytes transfer between users"""
        giver_id = test_user_balance.user_id
        receiver_id = "777777777"
        
        # Create transfer
        transfer_data = {
            "giver_id": giver_id,
            "giver_username": "Giver#1234",
            "receiver_id": receiver_id,
            "receiver_username": "Receiver#5678",
            "amount": 100,
            "reason": "test transfer"
        }
        
        response = await api_client.post(
            f"/api/guilds/{test_guild.guild_id}/bytes/transactions",
            headers=bot_headers,
            json=transfer_data
        )
        
        assert response.status_code == 200
        transaction = response.json()
        assert transaction["amount"] == 100
        assert transaction["reason"] == "test transfer"
        
        # Verify balances updated
        giver_response = await api_client.get(
            f"/api/guilds/{test_guild.guild_id}/bytes/balance/{giver_id}",
            headers=bot_headers
        )
        assert giver_response.json()["balance"] == 400  # 500 - 100
        
        receiver_response = await api_client.get(
            f"/api/guilds/{test_guild.guild_id}/bytes/balance/{receiver_id}",
            headers=bot_headers
        )
        assert receiver_response.json()["balance"] == 200  # 100 starting + 100 received
    
    async def test_leaderboard_caching(self, api_client, bot_headers, test_guild, redis_client):
        """Test leaderboard caching behavior"""
        # First request - hits database
        response1 = await api_client.get(
            f"/api/guilds/{test_guild.guild_id}/bytes/leaderboard?limit=10",
            headers=bot_headers
        )
        assert response1.status_code == 200
        
        # Check Redis cache was set
        cache_key = f"leaderboard:{test_guild.guild_id}:10"
        cached = await redis_client.get(cache_key)
        assert cached is not None
        
        # Second request - should use cache
        response2 = await api_client.get(
            f"/api/guilds/{test_guild.guild_id}/bytes/leaderboard?limit=10",
            headers=bot_headers
        )
        assert response2.status_code == 200
        assert response2.json() == response1.json()
```

### 3. tests/integration/test_squads_flow.py - Squad system integration:
```python
class TestSquadsIntegration:
    async def test_squad_join_with_cost(self, api_client, bot_headers, test_guild, test_user_balance, db_session):
        """Test joining squad with bytes cost deduction"""
        from web.models import Squad
        
        # Create two squads
        squad1 = Squad(
            guild_id=test_guild.guild_id,
            role_id="222222222",
            name="Alpha Squad",
            description="First squad",
            switch_cost=0
        )
        squad2 = Squad(
            guild_id=test_guild.guild_id,
            role_id="333333333",
            name="Beta Squad",
            description="Second squad",
            switch_cost=50
        )
        db_session.add_all([squad1, squad2])
        await db_session.commit()
        
        # Join first squad (free)
        response = await api_client.post(
            f"/api/guilds/{test_guild.guild_id}/squads/{squad1.id}/join",
            headers=bot_headers,
            json={"user_id": test_user_balance.user_id}
        )
        assert response.status_code == 200
        
        # Check balance unchanged
        balance_response = await api_client.get(
            f"/api/guilds/{test_guild.guild_id}/bytes/balance/{test_user_balance.user_id}",
            headers=bot_headers
        )
        assert balance_response.json()["balance"] == 500
        
        # Switch to second squad (costs 50)
        response = await api_client.post(
            f"/api/guilds/{test_guild.guild_id}/squads/{squad2.id}/join",
            headers=bot_headers,
            json={"user_id": test_user_balance.user_id}
        )
        assert response.status_code == 200
        
        # Check balance reduced
        balance_response = await api_client.get(
            f"/api/guilds/{test_guild.guild_id}/bytes/balance/{test_user_balance.user_id}",
            headers=bot_headers
        )
        assert balance_response.json()["balance"] == 450  # 500 - 50
    
    async def test_squad_insufficient_bytes(self, api_client, bot_headers, test_guild, db_session):
        """Test squad join with insufficient bytes"""
        from web.models import Squad, BytesBalance
        
        # Create user with low balance
        user = BytesBalance(
            guild_id=test_guild.guild_id,
            user_id="888888888",
            balance=25,
            total_received=25,
            total_sent=0
        )
        db_session.add(user)
        
        # Create expensive squad
        squad = Squad(
            guild_id=test_guild.guild_id,
            role_id="444444444",
            name="Elite Squad",
            switch_cost=100
        )
        db_session.add(squad)
        await db_session.commit()
        
        # Try to join - should fail
        response = await api_client.post(
            f"/api/guilds/{test_guild.guild_id}/squads/{squad.id}/join",
            headers=bot_headers,
            json={"user_id": user.user_id}
        )
        
        assert response.status_code == 400
        assert "insufficient" in response.json()["detail"].lower()
```

### 4. tests/integration/test_admin_flow.py - Admin interface integration:
```python
class TestAdminIntegration:
    async def test_config_update_notification(self, admin_client, mock_discord_api, redis_client, db_session):
        """Test config update flows to Redis for bot notification"""
        guild_id = "123456789"
        
        # Mock Discord API
        admin_client.app.state.discord = mock_discord_api
        
        # Update bytes config
        response = await admin_client.post(
            f"/admin/guilds/{guild_id}/bytes",
            data={
                "starting_balance": "200",
                "daily_amount": "20",
                "max_transfer": "2000",
                "transfer_cooldown_hours": "1"
            }
        )
        
        assert response.status_code == 200
        
        # Check Redis notification
        messages = await redis_client.get(f"config_update:{guild_id}")
        assert messages is not None
        
        # Verify database updated
        from web.models import BytesConfig
        config = await db_session.get(BytesConfig, guild_id)
        assert config.starting_balance == 200
        assert config.daily_amount == 20
    
    async def test_squad_creation_flow(self, admin_client, mock_discord_api, db_session):
        """Test creating squad through admin interface"""
        guild_id = "123456789"
        
        admin_client.app.state.discord = mock_discord_api
        
        response = await admin_client.post(
            f"/admin/guilds/{guild_id}/squads",
            data={
                "action": "create",
                "role_id": "222222222",
                "name": "New Squad",
                "description": "Test squad",
                "switch_cost": "75"
            }
        )
        
        assert response.status_code == 200
        assert b"Squad created successfully!" in response.content
        
        # Verify in database
        from web.models import Squad
        squad = await db_session.execute(
            select(Squad).where(
                Squad.guild_id == guild_id,
                Squad.name == "New Squad"
            )
        )
        squad = squad.scalar_one()
        assert squad.switch_cost == 75
```

### 5. tests/integration/test_error_scenarios.py - Error handling tests:
```python
class TestErrorScenarios:
    async def test_invalid_guild_handling(self, api_client, bot_headers):
        """Test handling of invalid guild IDs"""
        response = await api_client.get(
            "/api/guilds/invalid_guild/bytes/balance/123",
            headers=bot_headers
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
    
    async def test_rate_limiting(self, api_client, bot_headers, test_guild):
        """Test API rate limiting"""
        # Make many rapid requests
        responses = []
        for i in range(100):
            response = await api_client.get(
                f"/api/guilds/{test_guild.guild_id}/bytes/leaderboard",
                headers=bot_headers
            )
            responses.append(response.status_code)
        
        # Should hit rate limit
        assert 429 in responses
    
    async def test_database_error_handling(self, api_client, bot_headers, monkeypatch):
        """Test graceful handling of database errors"""
        # Mock database error
        async def mock_execute(*args, **kwargs):
            raise Exception("Database connection lost")
        
        monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncSession.execute", mock_execute)
        
        response = await api_client.get(
            "/api/guilds/123/bytes/balance/456",
            headers=bot_headers
        )
        
        assert response.status_code == 500
        assert "internal" in response.json()["detail"].lower()
```

### 6. Documentation addition for CLAUDE.md:
```markdown
## Testing Strategy

### Test Structure
- Unit tests: Test individual functions and methods
- Integration tests: Test component interactions
- End-to-end tests: Test complete user flows

### Running Tests
```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=web --cov=bot --cov-report=html

# Run specific test file
uv run pytest tests/integration/test_bytes_flow.py -v

# Run tests in parallel
uv run pytest -n auto
```

### Test Database
Tests use a separate PostgreSQL database that is created and destroyed for each test run.
Transactions are rolled back after each test to ensure isolation.

### Mocking Strategy
- Discord API calls are mocked to avoid external dependencies
- Redis is replaced with fakeredis for testing
- Bot services can be mocked for testing plugins independently

### Writing New Tests
1. Use async fixtures for database and Redis setup
2. Mock external services (Discord, etc.)
3. Test both success and failure paths
4. Verify side effects (Redis pub/sub, database changes)
5. Use meaningful test names that describe the scenario
```

## Quality Requirements
All integration tests should:
- Test complete user flows
- Verify all side effects
- Handle error scenarios
- Use proper mocking
- Be independent and repeatable