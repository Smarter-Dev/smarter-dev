# Session 12: Testing Suite and Production Readiness

## Objective
Complete the Smarter Dev v2 platform with comprehensive testing, monitoring, and production deployment setup. This session focuses on ensuring the system is robust, observable, and ready for production use.

## Prerequisites
- Completed Sessions 0-11 (all core features implemented)
- Understanding of testing patterns and deployment strategies
- Familiarity with monitoring and observability concepts

## Context from Previous Sessions

The Smarter Dev v2 platform is a complete rewrite with the following architecture:
- **Backend**: Starlette + FastAPI, Hikari + Lightbulb Discord bot
- **Database**: PostgreSQL with minimal data storage (no Discord data duplication)
- **Cache/Queue**: Redis for bot-web communication and caching
- **Frontend**: Tabler admin theme with Alpine.js interactivity
- **Features**: Bytes economy, Squads system, Auto-moderation, Admin dashboard

Session 11 completed the frontend and landing page implementation. Session 12 now focuses on making the platform production-ready.

## Task 1: Comprehensive Testing Suite

### tests/conftest.py

Create comprehensive test configuration:

```python
import asyncio
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock, MagicMock

from web.main import create_app
from web.database import Base, get_db
from web.config import get_settings
from bot.bot import SmarterDevBot
from shared.config import Settings

# Test database URL
TEST_DATABASE_URL = "postgresql+asyncpg://test:test@localhost/smarter_dev_test"

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """Create test database engine."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    yield engine
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    
    await engine.dispose()

@pytest_asyncio.fixture
async def test_db(test_engine):
    """Create test database session."""
    TestSessionLocal = sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    
    async with TestSessionLocal() as session:
        yield session

@pytest_asyncio.fixture
async def test_settings():
    """Test settings with overrides."""
    return Settings(
        dev_mode=True,
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/1",
        discord_token="test_token",
        secret_key="test_secret_key",
        admin_user_ids=["123456789"]
    )

@pytest_asyncio.fixture
async def test_app(test_settings, test_db):
    """Create test application."""
    app = create_app(test_settings)
    
    # Override database dependency
    async def override_get_db():
        yield test_db
    
    app.dependency_overrides[get_db] = override_get_db
    
    yield app
    
    app.dependency_overrides.clear()

@pytest_asyncio.fixture
async def test_client(test_app):
    """Create test HTTP client."""
    async with AsyncClient(app=test_app, base_url="http://test") as client:
        yield client

@pytest_asyncio.fixture
def mock_discord_client():
    """Mock Discord client for bot tests."""
    client = AsyncMock()
    client.fetch_guild = AsyncMock()
    client.fetch_user = AsyncMock()
    client.fetch_channel = AsyncMock()
    return client

@pytest_asyncio.fixture
def mock_bot(mock_discord_client):
    """Mock Discord bot instance."""
    bot = MagicMock()
    bot.rest = mock_discord_client
    bot.cache = MagicMock()
    return bot

@pytest_asyncio.fixture
async def sample_guild_data():
    """Sample guild data for tests."""
    return {
        "id": "123456789012345678",
        "name": "Test Guild",
        "member_count": 100,
        "owner_id": "987654321098765432"
    }

@pytest_asyncio.fixture
async def sample_user_data():
    """Sample user data for tests."""
    return {
        "id": "111222333444555666",
        "username": "testuser",
        "discriminator": "1234",
        "avatar": "test_avatar_hash"
    }
```

### tests/test_bytes_system.py

Test the bytes economy system:

```python
import pytest
from datetime import date, timedelta
from decimal import Decimal

from bot.services.bytes_service import BytesService
from web.models import BytesBalance, BytesTransaction, BytesConfig
from bot.errors import InsufficientBytesError, CooldownError

@pytest.mark.asyncio
class TestBytesService:
    
    async def test_get_balance_new_user(self, test_db):
        """Test getting balance for new user creates default balance."""
        service = BytesService(test_db)
        
        balance = await service.get_balance("123", "456")
        
        assert balance.balance == 100  # Default starting balance
        assert balance.streak_count == 0
        assert balance.last_daily is None
    
    async def test_award_daily_bytes_first_time(self, test_db):
        """Test awarding daily bytes for first time."""
        service = BytesService(test_db)
        
        result = await service.award_daily_bytes("123", "456", "testuser")
        
        assert result.awarded == 10  # Base daily amount
        assert result.streak_count == 1
        assert result.multiplier == 1
        assert result.new_balance == 110
    
    async def test_award_daily_bytes_with_streak(self, test_db):
        """Test daily bytes with streak multiplier."""
        service = BytesService(test_db)
        
        # Setup existing balance with streak
        balance = BytesBalance(
            guild_id="123",
            user_id="456", 
            balance=200,
            streak_count=7,
            last_daily=date.today() - timedelta(days=1)
        )
        test_db.add(balance)
        await test_db.commit()
        
        result = await service.award_daily_bytes("123", "456", "testuser")
        
        assert result.streak_count == 8
        assert result.multiplier == 2  # 8-day streak = 2x multiplier
        assert result.awarded == 20  # 10 * 2
        assert result.new_balance == 220
    
    async def test_award_daily_bytes_broken_streak(self, test_db):
        """Test streak resets if more than 1 day gap."""
        service = BytesService(test_db)
        
        balance = BytesBalance(
            guild_id="123",
            user_id="456",
            balance=200,
            streak_count=10,
            last_daily=date.today() - timedelta(days=3)
        )
        test_db.add(balance)
        await test_db.commit()
        
        result = await service.award_daily_bytes("123", "456", "testuser")
        
        assert result.streak_count == 1  # Reset
        assert result.multiplier == 1
        assert result.awarded == 10
    
    async def test_transfer_bytes_success(self, test_db):
        """Test successful bytes transfer."""
        service = BytesService(test_db)
        
        # Setup sender with balance
        sender = BytesBalance(
            guild_id="123", user_id="456", balance=500
        )
        test_db.add(sender)
        await test_db.commit()
        
        result = await service.transfer_bytes(
            guild_id="123",
            giver_id="456",
            giver_username="sender",
            receiver_id="789",
            receiver_username="receiver", 
            amount=100,
            reason="Test transfer"
        )
        
        assert result.success is True
        assert result.new_giver_balance == 400
        assert result.new_receiver_balance == 200  # 100 starting + 100 transfer
        
        # Check transaction was logged
        transaction = await test_db.get(BytesTransaction, result.transaction_id)
        assert transaction.amount == 100
        assert transaction.reason == "Test transfer"
    
    async def test_transfer_insufficient_balance(self, test_db):
        """Test transfer with insufficient balance."""
        service = BytesService(test_db)
        
        sender = BytesBalance(
            guild_id="123", user_id="456", balance=50
        )
        test_db.add(sender)
        await test_db.commit()
        
        with pytest.raises(InsufficientBytesError):
            await service.transfer_bytes(
                guild_id="123",
                giver_id="456", 
                giver_username="sender",
                receiver_id="789",
                receiver_username="receiver",
                amount=100
            )
    
    async def test_get_leaderboard(self, test_db):
        """Test leaderboard generation."""
        service = BytesService(test_db)
        
        # Create test balances
        balances = [
            BytesBalance(guild_id="123", user_id="1", balance=1000),
            BytesBalance(guild_id="123", user_id="2", balance=500),
            BytesBalance(guild_id="123", user_id="3", balance=750),
        ]
        for balance in balances:
            test_db.add(balance)
        await test_db.commit()
        
        leaderboard = await service.get_leaderboard("123", limit=3)
        
        assert len(leaderboard) == 3
        assert leaderboard[0].user_id == "1"  # Highest balance first
        assert leaderboard[0].balance == 1000
        assert leaderboard[1].user_id == "3"
        assert leaderboard[2].user_id == "2"
```

### tests/test_api_endpoints.py

Test API endpoints:

```python
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
class TestBytesAPI:
    
    async def test_get_guild_config(self, test_client: AsyncClient):
        """Test getting guild bytes configuration."""
        response = await test_client.get(
            "/api/guilds/123/bytes/config",
            headers={"Authorization": "Bearer test_api_key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "starting_balance" in data
        assert "daily_amount" in data
    
    async def test_update_guild_config(self, test_client: AsyncClient):
        """Test updating guild configuration."""
        config_data = {
            "starting_balance": 200,
            "daily_amount": 15,
            "max_transfer": 1000,
            "cooldown_hours": 24
        }
        
        response = await test_client.put(
            "/api/guilds/123/bytes/config",
            json=config_data,
            headers={"Authorization": "Bearer test_api_key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["starting_balance"] == 200
        assert data["daily_amount"] == 15
    
    async def test_get_leaderboard(self, test_client: AsyncClient):
        """Test leaderboard API endpoint."""
        response = await test_client.get(
            "/api/guilds/123/bytes/leaderboard?limit=10",
            headers={"Authorization": "Bearer test_api_key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "users" in data
        assert "total_count" in data
    
    async def test_unauthorized_request(self, test_client: AsyncClient):
        """Test API request without authentication."""
        response = await test_client.get("/api/guilds/123/bytes/config")
        
        assert response.status_code == 401

@pytest.mark.asyncio 
class TestWebPages:
    
    async def test_landing_page(self, test_client: AsyncClient):
        """Test landing page loads."""
        response = await test_client.get("/")
        
        assert response.status_code == 200
        assert "Smarter Dev" in response.text
        assert "Learn. Code. Grow." in response.text
    
    async def test_admin_requires_auth(self, test_client: AsyncClient):
        """Test admin pages require authentication."""
        response = await test_client.get("/admin")
        
        assert response.status_code == 302  # Redirect to login
    
    async def test_static_assets(self, test_client: AsyncClient):
        """Test static file serving."""
        response = await test_client.get("/static/css/landing.css")
        assert response.status_code == 200
        
        response = await test_client.get("/static/js/landing.js")
        assert response.status_code == 200
```

### tests/test_bot_commands.py

Test Discord bot commands:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
import hikari
import lightbulb

from bot.plugins.bytes import BytesPlugin

@pytest.mark.asyncio
class TestBytesCommands:
    
    @pytest.fixture
    def mock_context(self, mock_bot):
        """Create mock command context."""
        ctx = MagicMock()
        ctx.bot = mock_bot
        ctx.guild_id = hikari.Snowflake(123456789)
        ctx.author = MagicMock()
        ctx.author.id = hikari.Snowflake(987654321)
        ctx.author.username = "testuser"
        ctx.respond = AsyncMock()
        return ctx
    
    async def test_bytes_command_new_user(self, mock_context, test_db):
        """Test bytes command for new user."""
        plugin = BytesPlugin()
        plugin.bot = mock_context.bot
        
        # Mock API client
        plugin.api_client = AsyncMock()
        plugin.api_client.get_balance.return_value = {
            "balance": 100,
            "streak_count": 0,
            "daily_available": True
        }
        
        await plugin.bytes(mock_context)
        
        # Check that response was sent
        mock_context.respond.assert_called_once()
        call_args = mock_context.respond.call_args
        embed = call_args[1]['embed']
        
        assert "100" in embed.description  # Balance shown
        assert "Daily reward available" in embed.description
    
    async def test_bytes_send_command(self, mock_context, test_db):
        """Test sending bytes to another user."""
        plugin = BytesPlugin()
        plugin.bot = mock_context.bot
        
        # Mock target user
        target_user = MagicMock()
        target_user.id = hikari.Snowflake(111222333)
        target_user.username = "target"
        
        # Mock API client
        plugin.api_client = AsyncMock()
        plugin.api_client.transfer_bytes.return_value = {
            "success": True,
            "new_giver_balance": 400,
            "new_receiver_balance": 200,
            "transaction_id": "test-uuid"
        }
        
        await plugin.bytes_send(mock_context, target_user, 100, "Test transfer")
        
        # Verify API was called
        plugin.api_client.transfer_bytes.assert_called_once()
        
        # Verify response
        mock_context.respond.assert_called_once()
        call_args = mock_context.respond.call_args
        embed = call_args[1]['embed']
        
        assert "100 bytes" in embed.description
        assert "target" in embed.description
    
    async def test_bytes_leaderboard(self, mock_context, test_db):
        """Test leaderboard command."""
        plugin = BytesPlugin()
        plugin.bot = mock_context.bot
        
        # Mock leaderboard data
        plugin.api_client = AsyncMock()
        plugin.api_client.get_leaderboard.return_value = {
            "users": [
                {"user_id": "123", "balance": 1000, "rank": 1},
                {"user_id": "456", "balance": 500, "rank": 2}
            ],
            "total_count": 2
        }
        
        # Mock Discord user fetching
        mock_user1 = MagicMock()
        mock_user1.username = "user1"
        mock_user2 = MagicMock() 
        mock_user2.username = "user2"
        
        mock_context.bot.rest.fetch_user = AsyncMock()
        mock_context.bot.rest.fetch_user.side_effect = [mock_user1, mock_user2]
        
        await plugin.bytes_leaderboard(mock_context)
        
        # Verify response
        mock_context.respond.assert_called_once()
        call_args = mock_context.respond.call_args
        embed = call_args[1]['embed']
        
        assert "1000" in embed.description
        assert "user1" in embed.description
```

## Task 2: Performance Monitoring

### monitoring/metrics.py

Create performance monitoring:

```python
import time
import asyncio
import logging
from typing import Dict, Any
from dataclasses import dataclass, field
from collections import defaultdict, deque
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

@dataclass
class MetricData:
    """Store metric data with timestamps."""
    value: float
    timestamp: datetime = field(default_factory=datetime.utcnow)
    labels: Dict[str, str] = field(default_factory=dict)

class MetricsCollector:
    """Collect and store application metrics."""
    
    def __init__(self, retention_hours: int = 24):
        self.retention_hours = retention_hours
        self.metrics: Dict[str, deque] = defaultdict(lambda: deque(maxlen=10000))
        self.counters: Dict[str, float] = defaultdict(float)
        self.gauges: Dict[str, float] = defaultdict(float)
        self.histograms: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        
        # Start cleanup task
        self._cleanup_task = asyncio.create_task(self._cleanup_old_metrics())
    
    def counter(self, name: str, value: float = 1, labels: Dict[str, str] = None):
        """Increment a counter metric."""
        key = self._make_key(name, labels)
        self.counters[key] += value
        self.metrics[key].append(MetricData(self.counters[key], labels=labels or {}))
        logger.debug(f"Counter {name}: {self.counters[key]} (+{value})")
    
    def gauge(self, name: str, value: float, labels: Dict[str, str] = None):
        """Set a gauge metric."""
        key = self._make_key(name, labels)
        self.gauges[key] = value
        self.metrics[key].append(MetricData(value, labels=labels or {}))
        logger.debug(f"Gauge {name}: {value}")
    
    def histogram(self, name: str, value: float, labels: Dict[str, str] = None):
        """Record a histogram value."""
        key = self._make_key(name, labels)
        self.histograms[key].append(value)
        self.metrics[key].append(MetricData(value, labels=labels or {}))
        logger.debug(f"Histogram {name}: {value}")
    
    def timing(self, name: str, duration: float, labels: Dict[str, str] = None):
        """Record a timing metric in milliseconds."""
        self.histogram(f"{name}_duration_ms", duration * 1000, labels)
    
    def get_metrics(self, name: str = None) -> Dict[str, Any]:
        """Get current metric values."""
        if name:
            pattern_keys = [k for k in self.metrics.keys() if k.startswith(name)]
            return {k: list(self.metrics[k])[-10:] for k in pattern_keys}
        
        return {
            "counters": dict(self.counters),
            "gauges": dict(self.gauges),
            "histograms": {k: list(v)[-10:] for k, v in self.histograms.items()}
        }
    
    def get_summary(self) -> Dict[str, Any]:
        """Get metrics summary for health check."""
        return {
            "total_metrics": len(self.metrics),
            "active_counters": len(self.counters),
            "active_gauges": len(self.gauges),
            "active_histograms": len(self.histograms),
            "oldest_metric": min(
                (metric[0].timestamp for metric in self.metrics.values() if metric),
                default=datetime.utcnow()
            ).isoformat()
        }
    
    def _make_key(self, name: str, labels: Dict[str, str] = None) -> str:
        """Create metric key with labels."""
        if not labels:
            return name
        
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"
    
    async def _cleanup_old_metrics(self):
        """Remove old metric data."""
        while True:
            try:
                cutoff = datetime.utcnow() - timedelta(hours=self.retention_hours)
                
                for metric_name, data in self.metrics.items():
                    # Remove old entries
                    while data and data[0].timestamp < cutoff:
                        data.popleft()
                
                logger.debug(f"Cleaned up metrics older than {cutoff}")
                await asyncio.sleep(3600)  # Run every hour
                
            except Exception as e:
                logger.error(f"Metrics cleanup error: {e}")
                await asyncio.sleep(300)  # Retry in 5 minutes

# Global metrics collector
metrics = MetricsCollector()

class TimingContext:
    """Context manager for timing operations."""
    
    def __init__(self, metric_name: str, labels: Dict[str, str] = None):
        self.metric_name = metric_name
        self.labels = labels or {}
        self.start_time = None
    
    def __enter__(self):
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.start_time:
            duration = time.time() - self.start_time
            metrics.timing(self.metric_name, duration, self.labels)
            
            if exc_type:
                metrics.counter(f"{self.metric_name}_errors", labels=self.labels)

def timed(metric_name: str, labels: Dict[str, str] = None):
    """Decorator for timing function calls."""
    def decorator(func):
        if asyncio.iscoroutinefunction(func):
            async def async_wrapper(*args, **kwargs):
                with TimingContext(metric_name, labels):
                    return await func(*args, **kwargs)
            return async_wrapper
        else:
            def sync_wrapper(*args, **kwargs):
                with TimingContext(metric_name, labels):
                    return func(*args, **kwargs)
            return sync_wrapper
    return decorator
```

### monitoring/health.py

Health check endpoints:

```python
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import httpx
import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from web.database import get_db
from monitoring.metrics import metrics
from shared.config import get_settings

logger = logging.getLogger(__name__)

class HealthChecker:
    """Centralized health checking for all services."""
    
    def __init__(self):
        self.settings = get_settings()
        self._redis_client: Optional[redis.Redis] = None
        self._last_discord_check: Optional[datetime] = None
        self._discord_status = {"healthy": False, "latency": 0}
    
    async def check_all(self) -> Dict[str, Any]:
        """Run all health checks."""
        checks = {
            "database": await self.check_database(),
            "redis": await self.check_redis(),
            "discord_api": await self.check_discord(),
            "metrics": await self.check_metrics(),
        }
        
        overall_healthy = all(check["healthy"] for check in checks.values())
        
        return {
            "healthy": overall_healthy,
            "timestamp": datetime.utcnow().isoformat(),
            "checks": checks,
            "version": "2.0.0",
            "uptime": self._get_uptime()
        }
    
    async def check_database(self) -> Dict[str, Any]:
        """Check database connectivity and performance."""
        try:
            start_time = asyncio.get_event_loop().time()
            
            async for db in get_db():
                # Simple query to test connection
                result = await db.execute(text("SELECT 1"))
                await result.fetchone()
                
                # Check connection pool
                pool_info = {
                    "size": db.bind.pool.size() if hasattr(db.bind, 'pool') else 0,
                    "checked_out": db.bind.pool.checkedout() if hasattr(db.bind, 'pool') else 0
                }
                
                duration = (asyncio.get_event_loop().time() - start_time) * 1000
                
                return {
                    "healthy": True,
                    "response_time_ms": round(duration, 2),
                    "pool_info": pool_info
                }
                
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return {
                "healthy": False,
                "error": str(e),
                "response_time_ms": None
            }
    
    async def check_redis(self) -> Dict[str, Any]:
        """Check Redis connectivity and performance."""
        try:
            if not self._redis_client:
                self._redis_client = redis.from_url(self.settings.redis_url)
            
            start_time = asyncio.get_event_loop().time()
            
            # Ping Redis
            await self._redis_client.ping()
            
            # Test set/get
            test_key = "health_check"
            await self._redis_client.set(test_key, "ok", ex=60)
            value = await self._redis_client.get(test_key)
            
            duration = (asyncio.get_event_loop().time() - start_time) * 1000
            
            # Get Redis info
            info = await self._redis_client.info()
            
            return {
                "healthy": value == b"ok",
                "response_time_ms": round(duration, 2),
                "connected_clients": info.get("connected_clients", 0),
                "used_memory": info.get("used_memory_human", "unknown"),
                "keyspace_hits": info.get("keyspace_hits", 0),
                "keyspace_misses": info.get("keyspace_misses", 0)
            }
            
        except Exception as e:
            logger.error(f"Redis health check failed: {e}")
            return {
                "healthy": False,
                "error": str(e),
                "response_time_ms": None
            }
    
    async def check_discord(self) -> Dict[str, Any]:
        """Check Discord API connectivity."""
        # Cache Discord check for 30 seconds to avoid rate limits
        if (self._last_discord_check and 
            datetime.utcnow() - self._last_discord_check < timedelta(seconds=30)):
            return self._discord_status
        
        try:
            start_time = asyncio.get_event_loop().time()
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://discord.com/api/v10/gateway",
                    headers={"Authorization": f"Bot {self.settings.discord_token}"},
                    timeout=10.0
                )
                
                duration = (asyncio.get_event_loop().time() - start_time) * 1000
                
                if response.status_code == 200:
                    data = response.json()
                    self._discord_status = {
                        "healthy": True,
                        "response_time_ms": round(duration, 2),
                        "gateway_url": data.get("url", "unknown")
                    }
                else:
                    self._discord_status = {
                        "healthy": False,
                        "response_time_ms": round(duration, 2),
                        "status_code": response.status_code,
                        "error": "Discord API returned non-200 status"
                    }
                    
        except Exception as e:
            logger.error(f"Discord health check failed: {e}")
            self._discord_status = {
                "healthy": False,
                "error": str(e),
                "response_time_ms": None
            }
        
        self._last_discord_check = datetime.utcnow()
        return self._discord_status
    
    async def check_metrics(self) -> Dict[str, Any]:
        """Check metrics collection system."""
        try:
            summary = metrics.get_summary()
            
            return {
                "healthy": True,
                "total_metrics": summary["total_metrics"],
                "active_counters": summary["active_counters"],
                "active_gauges": summary["active_gauges"],
                "oldest_metric": summary["oldest_metric"]
            }
            
        except Exception as e:
            logger.error(f"Metrics health check failed: {e}")
            return {
                "healthy": False,
                "error": str(e)
            }
    
    def _get_uptime(self) -> str:
        """Get application uptime."""
        # This would need to be set when the application starts
        # For now, return a placeholder
        return "unknown"
    
    async def close(self):
        """Close connections."""
        if self._redis_client:
            await self._redis_client.close()

# Global health checker instance
health_checker = HealthChecker()
```

## Task 3: Production Configuration

### docker/Dockerfile.web

Web application container:

```dockerfile
# Multi-stage build for web application
FROM python:3.11-slim as builder

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create app user
RUN groupadd -r app && useradd -r -g app app

# Set work directory
WORKDIR /app

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim as production

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.local/bin:$PATH"

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create app user
RUN groupadd -r app && useradd -r -g app app

# Set work directory
WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=app:app . .

# Create necessary directories
RUN mkdir -p /app/logs && chown app:app /app/logs

# Switch to app user
USER app

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Expose port
EXPOSE 8000

# Run application
CMD ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

### docker/Dockerfile.bot

Discord bot container:

```dockerfile
# Multi-stage build for Discord bot
FROM python:3.11-slim as builder

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create app user
RUN groupadd -r bot && useradd -r -g bot bot

# Set work directory
WORKDIR /app

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim as production

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Create app user
RUN groupadd -r bot && useradd -r -g bot bot

# Set work directory
WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=bot:bot . .

# Create necessary directories
RUN mkdir -p /app/logs && chown bot:bot /app/logs

# Switch to bot user
USER bot

# Health check (bot exposes HTTP endpoint for health)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import asyncio; import aiohttp; asyncio.run(aiohttp.ClientSession().get('http://localhost:8001/health').close())" || exit 1

# Expose health check port
EXPOSE 8001

# Run bot
CMD ["python", "-m", "bot.bot"]
```

### docker-compose.prod.yml

Production deployment configuration:

```yaml
version: '3.8'

services:
  postgres:
    image: postgres:15-alpine
    environment:
      POSTGRES_DB: smarter_dev
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./backups:/backups
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER} -d smarter_dev"]
      interval: 30s
      timeout: 10s
      retries: 3
    restart: unless-stopped
    networks:
      - backend

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes --requirepass ${REDIS_PASSWORD}
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "--raw", "incr", "ping"]
      interval: 30s
      timeout: 10s
      retries: 3
    restart: unless-stopped
    networks:
      - backend

  web:
    build:
      context: .
      dockerfile: docker/Dockerfile.web
    environment:
      - DATABASE_URL=postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@postgres:5432/smarter_dev
      - REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
      - SECRET_KEY=${SECRET_KEY}
      - DISCORD_CLIENT_ID=${DISCORD_CLIENT_ID}
      - DISCORD_CLIENT_SECRET=${DISCORD_CLIENT_SECRET}
      - BASE_URL=${BASE_URL}
      - DEV_MODE=false
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    restart: unless-stopped
    networks:
      - backend
      - frontend
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.web.rule=Host(`${DOMAIN}`)"
      - "traefik.http.routers.web.tls=true"
      - "traefik.http.routers.web.tls.certresolver=letsencrypt"

  bot:
    build:
      context: .
      dockerfile: docker/Dockerfile.bot
    environment:
      - DISCORD_TOKEN=${DISCORD_TOKEN}
      - DATABASE_URL=postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@postgres:5432/smarter_dev
      - REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
      - API_BASE_URL=http://web:8000
      - API_KEY=${BOT_API_KEY}
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      web:
        condition: service_started
    restart: unless-stopped
    networks:
      - backend

  traefik:
    image: traefik:v3.0
    command:
      - "--api.dashboard=true"
      - "--providers.docker=true"
      - "--providers.docker.exposedbydefault=false"
      - "--entrypoints.web.address=:80"
      - "--entrypoints.websecure.address=:443"
      - "--certificatesresolvers.letsencrypt.acme.tlschallenge=true"
      - "--certificatesresolvers.letsencrypt.acme.email=${ACME_EMAIL}"
      - "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json"
    ports:
      - "80:80"
      - "443:443"
      - "8080:8080"  # Traefik dashboard
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - letsencrypt_data:/letsencrypt
    networks:
      - frontend
    restart: unless-stopped

networks:
  frontend:
    driver: bridge
  backend:
    driver: bridge

volumes:
  postgres_data:
  redis_data:
  letsencrypt_data:
```

### scripts/deploy.sh

Deployment script:

```bash
#!/bin/bash

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.prod"
BACKUP_DIR="./backups"

log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')] $1${NC}"
}

warn() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING: $1${NC}"
}

error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $1${NC}"
    exit 1
}

# Check requirements
check_requirements() {
    log "Checking requirements..."
    
    if ! command -v docker &> /dev/null; then
        error "Docker is not installed"
    fi
    
    if ! command -v docker-compose &> /dev/null; then
        error "Docker Compose is not installed"
    fi
    
    if [[ ! -f "$ENV_FILE" ]]; then
        error "Environment file $ENV_FILE not found"
    fi
    
    log "Requirements check passed"
}

# Create backup
create_backup() {
    log "Creating backup..."
    
    if [[ ! -d "$BACKUP_DIR" ]]; then
        mkdir -p "$BACKUP_DIR"
    fi
    
    # Database backup
    BACKUP_FILE="$BACKUP_DIR/backup_$(date +%Y%m%d_%H%M%S).sql"
    
    if docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T postgres pg_dump -U "${DB_USER}" smarter_dev > "$BACKUP_FILE" 2>/dev/null; then
        log "Database backup created: $BACKUP_FILE"
        
        # Compress backup
        gzip "$BACKUP_FILE"
        log "Backup compressed: ${BACKUP_FILE}.gz"
        
        # Clean old backups (keep last 7 days)
        find "$BACKUP_DIR" -name "backup_*.sql.gz" -mtime +7 -delete
        log "Old backups cleaned"
    else
        warn "Database backup failed - continuing with deployment"
    fi
}

# Deploy application
deploy() {
    log "Starting deployment..."
    
    # Pull latest images
    log "Pulling latest images..."
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" pull
    
    # Build custom images
    log "Building application images..."
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build --no-cache
    
    # Stop old containers
    log "Stopping old containers..."
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" down
    
    # Start new containers
    log "Starting new containers..."
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d
    
    # Wait for services to be healthy
    log "Waiting for services to be healthy..."
    sleep 30
    
    # Check service health
    check_health
    
    # Run database migrations
    log "Running database migrations..."
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T web alembic upgrade head
    
    log "Deployment completed successfully!"
}

# Check service health
check_health() {
    log "Checking service health..."
    
    local services=("web" "bot" "postgres" "redis")
    local unhealthy_services=()
    
    for service in "${services[@]}"; do
        if ! docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps "$service" | grep -q "healthy\|Up"; then
            unhealthy_services+=("$service")
        fi
    done
    
    if [[ ${#unhealthy_services[@]} -gt 0 ]]; then
        error "Unhealthy services: ${unhealthy_services[*]}"
    fi
    
    # Test web endpoint
    if ! curl -f http://localhost/health > /dev/null 2>&1; then
        warn "Web health check endpoint not responding"
    fi
    
    log "All services are healthy"
}

# Show logs
show_logs() {
    log "Showing service logs..."
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" logs --tail=50 -f
}

# Rollback to previous version
rollback() {
    warn "Rolling back to previous version..."
    
    # This would restore from backup and restart services
    # Implementation depends on your rollback strategy
    
    error "Rollback not implemented - manual intervention required"
}

# Main deployment process
main() {
    local command="${1:-deploy}"
    
    case "$command" in
        "deploy")
            check_requirements
            create_backup
            deploy
            ;;
        "health")
            check_health
            ;;
        "logs")
            show_logs
            ;;
        "rollback")
            rollback
            ;;
        *)
            echo "Usage: $0 {deploy|health|logs|rollback}"
            exit 1
            ;;
    esac
}

# Load environment variables
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

main "$@"
```

## Task 4: Monitoring Dashboard

### monitoring/dashboard.py

Create monitoring dashboard endpoint:

```python
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates
from starlette.routing import Route
import json
from datetime import datetime, timedelta

from monitoring.metrics import metrics
from monitoring.health import health_checker

templates = Jinja2Templates(directory="web/templates")

async def monitoring_dashboard(request: Request) -> HTMLResponse:
    """Monitoring dashboard page."""
    
    # Get health status
    health_status = await health_checker.check_all()
    
    # Get metrics summary
    metrics_data = metrics.get_metrics()
    
    # Get recent performance metrics
    recent_metrics = {
        "request_count": len(metrics.get_metrics("http_requests") or []),
        "error_rate": _calculate_error_rate(),
        "avg_response_time": _calculate_avg_response_time(),
        "active_users": len(metrics.get_metrics("active_sessions") or [])
    }
    
    return templates.TemplateResponse(
        "monitoring/dashboard.html",
        {
            "request": request,
            "health_status": health_status,
            "metrics_data": metrics_data,
            "recent_metrics": recent_metrics,
            "refresh_interval": 30  # seconds
        }
    )

async def metrics_api(request: Request):
    """Metrics API endpoint for Prometheus/Grafana."""
    
    metrics_data = metrics.get_metrics()
    
    # Convert to Prometheus format
    prometheus_metrics = []
    
    for counter_name, value in metrics_data.get("counters", {}).items():
        prometheus_metrics.append(f"# TYPE {counter_name} counter")
        prometheus_metrics.append(f"{counter_name} {value}")
    
    for gauge_name, value in metrics_data.get("gauges", {}).items():
        prometheus_metrics.append(f"# TYPE {gauge_name} gauge")
        prometheus_metrics.append(f"{gauge_name} {value}")
    
    return HTMLResponse(
        content="\n".join(prometheus_metrics),
        headers={"Content-Type": "text/plain"}
    )

def _calculate_error_rate() -> float:
    """Calculate error rate from metrics."""
    error_metrics = metrics.get_metrics("http_errors") or []
    total_metrics = metrics.get_metrics("http_requests") or []
    
    if not total_metrics:
        return 0.0
    
    recent_errors = len([m for m in error_metrics if m.timestamp > datetime.utcnow() - timedelta(minutes=5)])
    recent_total = len([m for m in total_metrics if m.timestamp > datetime.utcnow() - timedelta(minutes=5)])
    
    return (recent_errors / recent_total * 100) if recent_total > 0 else 0.0

def _calculate_avg_response_time() -> float:
    """Calculate average response time."""
    timing_metrics = metrics.get_metrics("http_request_duration_ms") or []
    recent_timings = [m.value for m in timing_metrics if m.timestamp > datetime.utcnow() - timedelta(minutes=5)]
    
    return sum(recent_timings) / len(recent_timings) if recent_timings else 0.0

# Routes
monitoring_routes = [
    Route("/monitoring", monitoring_dashboard),
    Route("/metrics", metrics_api),
]
```

## Task 5: Final Integration and Documentation

### web/templates/monitoring/dashboard.html

Monitoring dashboard template:

```html
{% extends "admin/layout.html" %}

{% block title %}System Monitoring{% endblock %}

{% block head %}
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<meta http-equiv="refresh" content="{{ refresh_interval }}">
{% endblock %}

{% block content %}
<div class="page-header d-print-none">
    <div class="container-xl">
        <div class="row g-2 align-items-center">
            <div class="col">
                <h2 class="page-title">System Monitoring</h2>
            </div>
            <div class="col-auto">
                <div class="btn-list">
                    <span class="badge bg-{{ 'success' if health_status.healthy else 'danger' }}">
                        {{ 'Healthy' if health_status.healthy else 'Unhealthy' }}
                    </span>
                </div>
            </div>
        </div>
    </div>
</div>

<div class="page-body">
    <div class="container-xl">
        <!-- Health Overview Cards -->
        <div class="row row-deck row-cards mb-4">
            <div class="col-sm-6 col-lg-3">
                <div class="card">
                    <div class="card-body">
                        <div class="d-flex align-items-center">
                            <div class="subheader">Database</div>
                            <div class="ms-auto">
                                <div class="badge bg-{{ 'success' if health_status.checks.database.healthy else 'danger' }}"></div>
                            </div>
                        </div>
                        <div class="h1 mb-0">{{ health_status.checks.database.response_time_ms or 'N/A' }}ms</div>
                    </div>
                </div>
            </div>
            
            <div class="col-sm-6 col-lg-3">
                <div class="card">
                    <div class="card-body">
                        <div class="d-flex align-items-center">
                            <div class="subheader">Redis</div>
                            <div class="ms-auto">
                                <div class="badge bg-{{ 'success' if health_status.checks.redis.healthy else 'danger' }}"></div>
                            </div>
                        </div>
                        <div class="h1 mb-0">{{ health_status.checks.redis.response_time_ms or 'N/A' }}ms</div>
                    </div>
                </div>
            </div>
            
            <div class="col-sm-6 col-lg-3">
                <div class="card">
                    <div class="card-body">
                        <div class="d-flex align-items-center">
                            <div class="subheader">Discord API</div>
                            <div class="ms-auto">
                                <div class="badge bg-{{ 'success' if health_status.checks.discord_api.healthy else 'danger' }}"></div>
                            </div>
                        </div>
                        <div class="h1 mb-0">{{ health_status.checks.discord_api.response_time_ms or 'N/A' }}ms</div>
                    </div>
                </div>
            </div>
            
            <div class="col-sm-6 col-lg-3">
                <div class="card">
                    <div class="card-body">
                        <div class="d-flex align-items-center">
                            <div class="subheader">Error Rate</div>
                            <div class="ms-auto">
                                <div class="badge bg-{{ 'success' if recent_metrics.error_rate < 1 else 'warning' if recent_metrics.error_rate < 5 else 'danger' }}"></div>
                            </div>
                        </div>
                        <div class="h1 mb-0">{{ "%.2f"|format(recent_metrics.error_rate) }}%</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Performance Charts -->
        <div class="row">
            <div class="col-lg-8">
                <div class="card">
                    <div class="card-header">
                        <h3 class="card-title">Response Time</h3>
                    </div>
                    <div class="card-body">
                        <canvas id="responseTimeChart" height="100"></canvas>
                    </div>
                </div>
            </div>
            
            <div class="col-lg-4">
                <div class="card">
                    <div class="card-header">
                        <h3 class="card-title">System Info</h3>
                    </div>
                    <div class="card-body">
                        <div class="datagrid">
                            <div class="datagrid-item">
                                <div class="datagrid-title">Version</div>
                                <div class="datagrid-content">{{ health_status.version }}</div>
                            </div>
                            <div class="datagrid-item">
                                <div class="datagrid-title">Uptime</div>
                                <div class="datagrid-content">{{ health_status.uptime }}</div>
                            </div>
                            <div class="datagrid-item">
                                <div class="datagrid-title">Total Metrics</div>
                                <div class="datagrid-content">{{ health_status.checks.metrics.total_metrics }}</div>
                            </div>
                            <div class="datagrid-item">
                                <div class="datagrid-title">Last Check</div>
                                <div class="datagrid-content">{{ health_status.timestamp[:19] }}</div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
// Response time chart
const ctx = document.getElementById('responseTimeChart').getContext('2d');
const responseTimeChart = new Chart(ctx, {
    type: 'line',
    data: {
        labels: [], // Will be populated with timestamps
        datasets: [{
            label: 'Response Time (ms)',
            data: [], // Will be populated with response times
            borderColor: 'rgb(75, 192, 192)',
            backgroundColor: 'rgba(75, 192, 192, 0.2)',
            tension: 0.1
        }]
    },
    options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
            y: {
                beginAtZero: true,
                title: {
                    display: true,
                    text: 'Response Time (ms)'
                }
            },
            x: {
                title: {
                    display: true,
                    text: 'Time'
                }
            }
        }
    }
});

// Auto-refresh functionality
let refreshInterval = {{ refresh_interval }} * 1000;
setTimeout(() => {
    location.reload();
}, refreshInterval);
</script>
{% endblock %}
```

### README.md

Update the main README with complete setup instructions:

```markdown
# Smarter Dev Platform v2.0

A comprehensive Discord community management platform with economy system, squad management, and auto-moderation features.

## Features

- **Bytes Economy System**: Virtual currency with daily rewards, streak multipliers, and role rewards
- **Squad System**: Team-based roles with switching costs and management
- **Auto-Moderation**: Configurable rules for username filtering, rate limiting, and spam detection
- **Admin Dashboard**: Web interface for managing guilds and configurations
- **Modern Architecture**: Clean separation between Discord bot and web application

## Architecture

- **Discord Bot**: Hikari + Lightbulb for Discord interactions
- **Web Application**: Starlette + FastAPI for user interface and API
- **Database**: PostgreSQL with minimal data storage (no Discord data duplication)
- **Cache/Queue**: Redis for bot-web communication and caching
- **Frontend**: Tabler admin theme with Alpine.js for interactivity

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 12+
- Redis 6+
- Docker and Docker Compose (recommended)

### Development Setup

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd smarter-dev-v2
   ```

2. **Setup environment**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

3. **Start services with Docker**
   ```bash
   docker-compose up -d postgres redis
   ```

4. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

5. **Run database migrations**
   ```bash
   cd web && alembic upgrade head
   ```

6. **Start the web application**
   ```bash
   uvicorn web.main:app --reload --port 8000
   ```

7. **Start the Discord bot**
   ```bash
   python -m bot.bot
   ```

### Production Deployment

1. **Configure environment**
   ```bash
   cp .env.example .env.prod
   # Edit .env.prod with production values
   ```

2. **Deploy with Docker Compose**
   ```bash
   ./scripts/deploy.sh
   ```

3. **Monitor the deployment**
   ```bash
   ./scripts/deploy.sh health
   ./scripts/deploy.sh logs
   ```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DEV_MODE` | Enable development mode | `true` |
| `DATABASE_URL` | PostgreSQL connection URL | Required |
| `REDIS_URL` | Redis connection URL | Required |
| `DISCORD_TOKEN` | Discord bot token | Required |
| `DISCORD_CLIENT_ID` | Discord application client ID | Required |
| `DISCORD_CLIENT_SECRET` | Discord OAuth client secret | Required |
| `SECRET_KEY` | Session encryption key | Required |
| `BASE_URL` | Base URL for the web application | Required |
| `ADMIN_USER_IDS` | Comma-separated Discord user IDs with admin access | Optional |

### Bot Configuration

The bot can be configured through the admin dashboard or directly via the database. Key settings include:

- **Bytes Economy**: Starting balance, daily amounts, transfer limits
- **Squad System**: Available squads, switch costs, role mappings
- **Auto-Moderation**: Rule types, actions, exemptions

## API Documentation

The API documentation is available at `/docs` when running the web application. Key endpoints include:

- `GET /api/guilds/{guild_id}/bytes/config` - Get guild bytes configuration
- `POST /api/guilds/{guild_id}/bytes/transactions` - Create bytes transaction
- `GET /api/guilds/{guild_id}/squads` - List available squads
- `POST /api/guilds/{guild_id}/automod/rules` - Create auto-moderation rule

## Monitoring

### Health Checks

- Web application: `GET /health`
- Metrics endpoint: `GET /metrics` (Prometheus format)
- Monitoring dashboard: `GET /monitoring`

### Logging

All services use structured logging with the following levels:
- `DEBUG`: Detailed debugging information
- `INFO`: General operational messages
- `WARNING`: Warning conditions
- `ERROR`: Error conditions
- `CRITICAL`: Critical errors requiring immediate attention

## Testing

### Run Tests

```bash
# Install test dependencies
pip install -r requirements-dev.txt

# Run all tests
pytest

# Run with coverage
pytest --cov=. --cov-report=html

# Run specific test categories
pytest tests/test_bytes_system.py  # Bytes system tests
pytest tests/test_api_endpoints.py  # API tests
pytest tests/test_bot_commands.py   # Bot command tests
```

### Test Categories

- **Unit Tests**: Test individual functions and classes
- **Integration Tests**: Test component interactions
- **API Tests**: Test HTTP endpoints
- **Bot Tests**: Test Discord command handling

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Add tests for new functionality
5. Ensure all tests pass (`pytest`)
6. Commit your changes (`git commit -m 'Add amazing feature'`)
7. Push to the branch (`git push origin feature/amazing-feature`)
8. Open a Pull Request

## Troubleshooting

### Common Issues

1. **Database connection errors**
   - Verify PostgreSQL is running
   - Check DATABASE_URL configuration
   - Ensure database exists

2. **Redis connection errors**
   - Verify Redis is running
   - Check REDIS_URL configuration
   - Test Redis connectivity

3. **Discord bot not responding**
   - Verify DISCORD_TOKEN is correct
   - Check bot permissions in Discord guild
   - Review bot logs for errors

4. **Web application not loading**
   - Check if port 8000 is available
   - Verify all environment variables are set
   - Check web application logs

### Getting Help

- Check the [troubleshooting guide](docs/TROUBLESHOOTING.md)
- Review application logs
- Open an issue on GitHub

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [Hikari](https://github.com/hikari-py/hikari) - Discord API library
- [Starlette](https://www.starlette.io/) - ASGI web framework
- [Tabler](https://tabler.io/) - Admin dashboard theme
- [FastAPI](https://fastapi.tiangolo.com/) - API framework
```

<function_calls>
<invoke name="TodoWrite">
<parameter name="todos">[{"id": "1", "content": "Review existing planning documents in planning/ directory", "status": "completed", "priority": "high"}, {"id": "2", "content": "Evaluate v2 planning content and requirements", "status": "completed", "priority": "high"}, {"id": "3", "content": "Create claude-latest-session.md for session 12", "status": "completed", "priority": "medium"}, {"id": "4", "content": "Commit the session 12 changes", "status": "in_progress", "priority": "low"}]