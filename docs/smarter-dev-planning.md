# Smarter Dev Bytes & Squads Implementation Plan

## Overview

This document outlines the implementation of the bytes economy and squads team systems for the Smarter Dev Discord bot, including web API integration and admin interface. This is a complete rewrite focusing on clean architecture, test-driven development, and maintainable code.

## Architecture Overview

**Core Components:**
1. **Discord Bot** - Hikari + Lightbulb for Discord interactions
2. **Web API** - FastAPI mounted on existing Starlette app
3. **Admin Interface** - Authenticated web pages for guild configuration
4. **Database** - PostgreSQL with minimal data storage
5. **Message Queue** - Redis for real-time updates

**Key Principles:**
- Test-driven development with comprehensive test coverage
- Layered architecture for testability
- No special test logic in application code
- Minimal data storage (only store what Discord doesn't provide)
- Clear separation between bot commands and business logic

## Session Structure

Each session is designed to be a focused coding task that Claude Code can complete effectively. Sessions build upon each other but can be tested independently.

---

## Session 1: Project Setup and Environment Configuration

**Goal:** Understand existing structure, setup development environment, and create comprehensive documentation

```markdown
Analyze the existing Smarter Dev project structure and set up the development environment for implementing bytes and squads systems.

Current structure:
- Existing Starlette application with landing page
- Need to add authenticated admin interface
- Need to add Discord bot with bytes and squads features
- Need to integrate bot with web API

Requirements:
1. Use Python 3.11+ with modern type hints
2. Use uv for package management
3. Use docker compose for local development (note: podman compose is available locally)
4. Test-driven development approach
5. Use httpx.AsyncClient with app transport for testing
6. Never add special test logic in application code

Create:

1. CLAUDE.md - Comprehensive development documentation including:
   - Project overview and architecture
   - Development setup instructions
   - Testing strategy and examples
   - Code style guidelines
   - Common tasks and troubleshooting
   - API documentation structure

2. docker-compose.yml with:
   - PostgreSQL 15+ with proper initialization
   - Redis 7+ for pub/sub and caching
   - Proper networking between services
   - Volume mounts for development
   - Environment variable configuration

3. pyproject.toml with:
   - Project metadata
   - Dependencies grouped by category (bot, web, dev, test)
   - Development scripts
   - Test configuration
   - Code formatting settings

Core dependencies needed:
- hikari[speedups] - Discord bot framework
- hikari-lightbulb - Command framework
- fastapi - API framework
- sqlalchemy[asyncio] - ORM
- asyncpg - PostgreSQL driver
- alembic - Database migrations
- redis - Redis client
- httpx - HTTP client for testing
- pytest - Testing framework
- pytest-asyncio - Async test support
- pytest-cov - Coverage reporting

4. .env.example with all required variables:
   - Database configuration
   - Redis configuration
   - Discord bot token and application ID
   - Web app session secret
   - API authentication keys
   - Development mode flags

5. Project structure:
   ```
   smarter_dev/
   ├── bot/
   │   ├── __init__.py
   │   ├── client.py          # Bot client setup
   │   ├── plugins/           # Bot plugins
   │   └── services/          # Business logic layer
   ├── web/
   │   ├── __init__.py
   │   ├── api/              # FastAPI app
   │   ├── admin/            # Admin pages
   │   └── models.py         # Database models
   ├── shared/
   │   ├── __init__.py
   │   ├── config.py         # Configuration
   │   ├── database.py       # Database setup
   │   └── redis_client.py   # Redis setup
   └── tests/
       ├── conftest.py       # Test fixtures
       ├── bot/              # Bot tests
       └── web/              # Web tests
   ```

6. shared/config.py - Configuration using pydantic-settings:
   - Environment-based configuration
   - Validation for all settings
   - Clear defaults for development

7. Testing strategy documentation:
   - How to test bot event listeners using layered architecture
   - How to test API endpoints using httpx.AsyncClient
   - How to test database operations with transactions
   - How to test Redis pub/sub functionality

The bot architecture should use a layered approach where:
- Event listeners/commands are thin wrappers
- All business logic is in testable service classes
- Services can be tested independently of Discord

Example bot testing approach:
```python
# bot/plugins/bytes.py
@plugin.command
async def bytes(ctx: Context) -> None:
    service = BytesService(db, redis)
    balance = await service.get_balance(ctx.guild_id, ctx.author.id)
    await ctx.respond(embed=balance.to_embed())

# tests/bot/test_bytes_service.py
async def test_get_balance():
    service = BytesService(mock_db, mock_redis)
    balance = await service.get_balance("guild_123", "user_456")
    assert balance.amount == 100
```

Document the testing approach clearly in CLAUDE.md so all future sessions follow the same patterns.
```

---

## Session 2: Database Models and Migrations

**Goal:** Create minimal database schema for bytes and squads systems with proper testing

```markdown
Create database models for the bytes and squads systems using SQLAlchemy with async support.

Design principles:
- Store only necessary data (no Discord data duplication)
- Use Discord snowflake IDs as strings
- Include audit timestamps
- Efficient indexes for common queries
- Test all models and database operations

Models to create:

1. web/models.py - SQLAlchemy models:

BytesBalance:
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

BytesTransaction:
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

BytesConfig:
- guild_id: str (primary key)
- starting_balance: int (default: 100)
- daily_amount: int (default: 10)
- streak_bonuses: JSON (default: {8: 2, 16: 4, 32: 8, 64: 16})
- max_transfer: int (default: 1000)
- transfer_cooldown_hours: int (default: 0)
- role_rewards: JSON ({role_id: min_received_amount})
- created_at: datetime
- updated_at: datetime

Squad:
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

SquadMembership:
- guild_id: str
- user_id: str
- squad_id: UUID
- joined_at: datetime
- Primary key: (guild_id, user_id)
- Foreign key: squad_id -> Squad.id

2. shared/database.py - Database setup:
- Async SQLAlchemy engine configuration
- Session factory with proper transaction handling
- Base model class with common fields
- Connection pool configuration

3. alembic.ini and alembic/env.py:
- Async migrations support
- Auto-generate from models
- Proper naming conventions

4. Initial migration:
- Create all tables with proper constraints
- Add indexes for performance
- Include helpful migration comments

5. tests/test_models.py - Comprehensive model tests:
- Test all model creation
- Test unique constraints
- Test cascade deletes
- Test JSON field serialization
- Test timestamp auto-update

6. tests/conftest.py - Test fixtures:
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

7. web/crud.py - Database operations:
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

8. tests/test_crud.py - Test all database operations:
- Test balance creation and updates
- Test transaction atomicity
- Test leaderboard queries
- Test squad membership changes
- Test concurrent operations

Testing approach:
- Use pytest-asyncio for async tests
- Use database transactions that rollback after each test
- Test both happy paths and error cases
- Verify database constraints work properly
- Test performance with bulk operations

All tests should pass before moving to next session.
```

---

## Session 3: Web API Implementation

**Goal:** Create FastAPI endpoints for bytes and squads systems with comprehensive testing

```markdown
Create REST API endpoints for the bytes and squads systems using FastAPI mounted to the existing Starlette app.

Requirements:
1. Mount FastAPI app at /api on existing Starlette app
2. Use bearer token authentication for bot
3. Comprehensive input validation
4. Proper error responses
5. Test all endpoints using httpx.AsyncClient

Create:

1. web/api/app.py - FastAPI app setup:
```python
from fastapi import FastAPI
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize connections
    await init_database()
    await init_redis()
    yield
    # Cleanup
    await close_database()
    await close_redis()

api = FastAPI(
    title="Smarter Dev API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

# Include routers
api.include_router(auth_router, prefix="/auth", tags=["auth"])
api.include_router(bytes_router, prefix="/guilds/{guild_id}/bytes", tags=["bytes"])
api.include_router(squads_router, prefix="/guilds/{guild_id}/squads", tags=["squads"])
```

2. web/api/dependencies.py - Shared dependencies:
```python
async def get_db_session():
    async with async_session() as session:
        yield session

async def verify_bot_token(
    credentials: HTTPAuthorizationCredentials = Security(HTTPBearer())
):
    # Verify bearer token
    if not is_valid_bot_token(credentials.credentials):
        raise HTTPException(401, "Invalid bot token")
    return credentials.credentials

async def verify_guild_access(
    guild_id: str,
    token: str = Depends(verify_bot_token)
):
    # Verify bot has access to guild
    if not await bot_in_guild(guild_id):
        raise HTTPException(403, "Bot not in guild")
    return guild_id
```

3. web/api/schemas.py - Pydantic models:
```python
class BytesBalanceResponse(BaseModel):
    guild_id: str
    user_id: str
    balance: int
    total_received: int
    total_sent: int
    streak_count: int
    last_daily: Optional[date]

class BytesTransactionCreate(BaseModel):
    giver_id: str
    giver_username: str
    receiver_id: str
    receiver_username: str
    amount: int = Field(gt=0, le=10000)
    reason: Optional[str] = Field(None, max_length=200)

class SquadResponse(BaseModel):
    id: UUID
    guild_id: str
    role_id: str
    name: str
    description: Optional[str]
    switch_cost: int
    member_count: int
    is_active: bool
```

4. web/api/routers/bytes.py - Bytes endpoints:
```python
router = APIRouter()

@router.get("/balance/{user_id}", response_model=BytesBalanceResponse)
async def get_balance(
    guild_id: str = Depends(verify_guild_access),
    user_id: str = Path(...),
    db: AsyncSession = Depends(get_db_session)
):
    """Get user's bytes balance"""

@router.post("/daily/{user_id}", response_model=BytesBalanceResponse)
async def claim_daily(
    guild_id: str = Depends(verify_guild_access),
    user_id: str = Path(...),
    db: AsyncSession = Depends(get_db_session)
):
    """Claim daily bytes with streak calculation"""

@router.post("/transactions", response_model=BytesTransactionResponse)
async def create_transaction(
    guild_id: str = Depends(verify_guild_access),
    transaction: BytesTransactionCreate,
    db: AsyncSession = Depends(get_db_session)
):
    """Create a bytes transaction"""

@router.get("/leaderboard", response_model=List[BytesBalanceResponse])
async def get_leaderboard(
    guild_id: str = Depends(verify_guild_access),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session)
):
    """Get guild bytes leaderboard"""

@router.get("/config", response_model=BytesConfigResponse)
async def get_config(
    guild_id: str = Depends(verify_guild_access),
    db: AsyncSession = Depends(get_db_session)
):
    """Get guild bytes configuration"""

@router.put("/config", response_model=BytesConfigResponse)
async def update_config(
    guild_id: str = Depends(verify_guild_access),
    config: BytesConfigUpdate,
    db: AsyncSession = Depends(get_db_session)
):
    """Update guild bytes configuration"""
```

5. web/api/routers/squads.py - Squad endpoints:
```python
@router.get("/", response_model=List[SquadResponse])
async def list_squads(
    guild_id: str = Depends(verify_guild_access),
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db_session)
):
    """List all squads in guild"""

@router.post("/", response_model=SquadResponse)
async def create_squad(
    guild_id: str = Depends(verify_guild_access),
    squad: SquadCreate,
    db: AsyncSession = Depends(get_db_session)
):
    """Create a new squad"""

@router.post("/{squad_id}/join")
async def join_squad(
    guild_id: str = Depends(verify_guild_access),
    squad_id: UUID,
    user_id: str = Body(...),
    db: AsyncSession = Depends(get_db_session)
):
    """Join a squad (handles bytes cost)"""

@router.delete("/{squad_id}/leave")
async def leave_squad(
    guild_id: str = Depends(verify_guild_access),
    squad_id: UUID,
    user_id: str = Body(...),
    db: AsyncSession = Depends(get_db_session)
):
    """Leave current squad"""

@router.get("/members/{user_id}")
async def get_user_squad(
    guild_id: str = Depends(verify_guild_access),
    user_id: str,
    db: AsyncSession = Depends(get_db_session)
):
    """Get user's current squad"""
```

6. Mount API to existing Starlette app:
```python
# In existing main.py
from web.api.app import api

# Mount the API
app.mount("/api", api)
```

7. tests/web/test_api.py - Comprehensive API tests:
```python
@pytest.fixture
async def api_client(app):
    """Create test client with app transport"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test"
    ) as client:
        yield client

@pytest.fixture
def bot_headers():
    """Valid bot authentication headers"""
    return {"Authorization": "Bearer test-bot-token"}

class TestBytesAPI:
    async def test_get_balance(self, api_client, bot_headers, test_data):
        response = await api_client.get(
            f"/api/guilds/{test_data.guild_id}/bytes/balance/{test_data.user_id}",
            headers=bot_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["balance"] == 100

    async def test_claim_daily(self, api_client, bot_headers, test_data):
        # Test first claim
        response = await api_client.post(
            f"/api/guilds/{test_data.guild_id}/bytes/daily/{test_data.user_id}",
            headers=bot_headers
        )
        assert response.status_code == 200
        assert response.json()["balance"] == 110  # 100 + 10 daily

        # Test duplicate claim
        response = await api_client.post(
            f"/api/guilds/{test_data.guild_id}/bytes/daily/{test_data.user_id}",
            headers=bot_headers
        )
        assert response.status_code == 400
        assert "already claimed" in response.json()["detail"]

    async def test_create_transaction(self, api_client, bot_headers, test_data):
        transaction = {
            "giver_id": test_data.user_id,
            "giver_username": "TestUser",
            "receiver_id": "987654321",
            "receiver_username": "OtherUser",
            "amount": 50,
            "reason": "test transfer"
        }
        response = await api_client.post(
            f"/api/guilds/{test_data.guild_id}/bytes/transactions",
            headers=bot_headers,
            json=transaction
        )
        assert response.status_code == 200

    async def test_invalid_transaction(self, api_client, bot_headers, test_data):
        # Test insufficient balance
        transaction = {
            "giver_id": test_data.user_id,
            "giver_username": "TestUser",
            "receiver_id": "987654321",
            "receiver_username": "OtherUser",
            "amount": 1000  # More than balance
        }
        response = await api_client.post(
            f"/api/guilds/{test_data.guild_id}/bytes/transactions",
            headers=bot_headers,
            json=transaction
        )
        assert response.status_code == 400
        assert "insufficient balance" in response.json()["detail"]
```

8. tests/web/test_squads_api.py - Squad API tests:
- Test squad CRUD operations
- Test join/leave with bytes costs
- Test validation errors
- Test concurrent squad operations

All endpoints should have:
- Proper authentication
- Input validation
- Error handling
- Comprehensive tests
- OpenAPI documentation
```

---

## Session 4: Bot Service Layer

**Goal:** Create testable service layer for bot business logic

```markdown
Create service layer for the Discord bot that separates business logic from Discord-specific code.

Architecture:
- Services contain all business logic
- Services are Discord-agnostic and fully testable
- Bot plugins are thin wrappers that call services
- All external calls (API, Redis) are mockable

Create:

1. bot/services/base.py - Base service class:
```python
class BaseService:
    def __init__(self, api_client: APIClient, redis_client: Redis):
        self.api = api_client
        self.redis = redis_client
        self._cache = {}
    
    async def invalidate_cache(self, key: str):
        self._cache.pop(key, None)
        await self.redis.delete(f"cache:{key}")
```

2. bot/services/bytes_service.py - Bytes business logic:
```python
class BytesService(BaseService):
    STREAK_MULTIPLIERS = {8: 2, 16: 4, 32: 8, 64: 16}
    
    async def get_balance(self, guild_id: str, user_id: str) -> BytesBalance:
        """Get user balance from API"""
        cache_key = f"balance:{guild_id}:{user_id}"
        
        # Check cache first
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        response = await self.api.get(
            f"/guilds/{guild_id}/bytes/balance/{user_id}"
        )
        balance = BytesBalance(**response.json())
        
        # Cache for 5 minutes
        self._cache[cache_key] = balance
        return balance
    
    async def claim_daily(self, guild_id: str, user_id: str, username: str) -> DailyClaimResult:
        """Claim daily bytes with streak calculation"""
        response = await self.api.post(
            f"/guilds/{guild_id}/bytes/daily/{user_id}",
            json={"username": username}
        )
        
        if response.status_code == 400:
            error = response.json()
            if "already claimed" in error.get("detail", ""):
                return DailyClaimResult(
                    success=False,
                    reason="You've already claimed your daily bytes today!"
                )
        
        balance = BytesBalance(**response.json())
        
        # Calculate what they earned
        config = await self.get_config(guild_id)
        base_amount = config.daily_amount
        multiplier = self._calculate_multiplier(balance.streak_count)
        earned = base_amount * multiplier
        
        return DailyClaimResult(
            success=True,
            balance=balance,
            earned=earned,
            streak=balance.streak_count,
            multiplier=multiplier
        )
    
    async def transfer_bytes(
        self, 
        guild_id: str, 
        giver: User, 
        receiver: User, 
        amount: int, 
        reason: Optional[str] = None
    ) -> TransferResult:
        """Transfer bytes between users"""
        # Validate transfer
        if giver.id == receiver.id:
            return TransferResult(
                success=False,
                reason="You can't send bytes to yourself!"
            )
        
        if amount <= 0:
            return TransferResult(
                success=False,
                reason="Amount must be positive!"
            )
        
        # Check balance
        balance = await self.get_balance(guild_id, giver.id)
        if balance.balance < amount:
            return TransferResult(
                success=False,
                reason=f"Insufficient balance! You have {balance.balance} bytes."
            )
        
        # Create transaction
        response = await self.api.post(
            f"/guilds/{guild_id}/bytes/transactions",
            json={
                "giver_id": giver.id,
                "giver_username": str(giver),
                "receiver_id": receiver.id,
                "receiver_username": str(receiver),
                "amount": amount,
                "reason": reason
            }
        )
        
        if response.status_code != 200:
            return TransferResult(
                success=False,
                reason="Transfer failed. Please try again."
            )
        
        # Invalidate caches
        await self.invalidate_cache(f"balance:{guild_id}:{giver.id}")
        await self.invalidate_cache(f"balance:{guild_id}:{receiver.id}")
        
        return TransferResult(
            success=True,
            transaction=BytesTransaction(**response.json())
        )
    
    async def get_leaderboard(self, guild_id: str, limit: int = 10) -> List[LeaderboardEntry]:
        """Get guild leaderboard"""
        cache_key = f"leaderboard:{guild_id}:{limit}"
        
        # Check cache
        cached = await self.redis.get(cache_key)
        if cached:
            return [LeaderboardEntry(**entry) for entry in json.loads(cached)]
        
        response = await self.api.get(
            f"/guilds/{guild_id}/bytes/leaderboard",
            params={"limit": limit}
        )
        
        entries = [
            LeaderboardEntry(
                rank=idx + 1,
                user_id=data["user_id"],
                balance=data["balance"],
                total_received=data["total_received"]
            )
            for idx, data in enumerate(response.json())
        ]
        
        # Cache for 1 minute
        await self.redis.setex(
            cache_key,
            60,
            json.dumps([e.dict() for e in entries])
        )
        
        return entries
    
    def _calculate_multiplier(self, streak: int) -> int:
        """Calculate streak multiplier"""
        for threshold, multiplier in sorted(
            self.STREAK_MULTIPLIERS.items(), 
            reverse=True
        ):
            if streak >= threshold:
                return multiplier
        return 1
```

3. bot/services/squads_service.py - Squad business logic:
```python
class SquadsService(BaseService):
    async def list_squads(self, guild_id: str, include_inactive: bool = False) -> List[Squad]:
        """List available squads"""
        response = await self.api.get(
            f"/guilds/{guild_id}/squads",
            params={"include_inactive": include_inactive}
        )
        return [Squad(**squad) for squad in response.json()]
    
    async def get_user_squad(self, guild_id: str, user_id: str) -> Optional[Squad]:
        """Get user's current squad"""
        response = await self.api.get(
            f"/guilds/{guild_id}/squads/members/{user_id}"
        )
        
        if response.status_code == 404:
            return None
        
        return Squad(**response.json())
    
    async def join_squad(
        self, 
        guild_id: str, 
        user_id: str, 
        squad_id: UUID,
        current_balance: int
    ) -> JoinSquadResult:
        """Join a squad with validation"""
        # Check if user is already in this squad
        current_squad = await self.get_user_squad(guild_id, user_id)
        if current_squad and current_squad.id == squad_id:
            return JoinSquadResult(
                success=False,
                reason="You're already in this squad!"
            )
        
        # Get target squad
        squads = await self.list_squads(guild_id)
        target_squad = next((s for s in squads if s.id == squad_id), None)
        
        if not target_squad:
            return JoinSquadResult(
                success=False,
                reason="Squad not found!"
            )
        
        # Check switch cost
        cost = target_squad.switch_cost if current_squad else 0
        if cost > current_balance:
            return JoinSquadResult(
                success=False,
                reason=f"Insufficient bytes! Switching costs {cost} bytes."
            )
        
        # Join squad
        response = await self.api.post(
            f"/guilds/{guild_id}/squads/{squad_id}/join",
            json={"user_id": user_id}
        )
        
        if response.status_code != 200:
            return JoinSquadResult(
                success=False,
                reason="Failed to join squad. Please try again."
            )
        
        return JoinSquadResult(
            success=True,
            squad=target_squad,
            cost=cost,
            previous_squad=current_squad
        )
```

4. bot/services/models.py - Service layer models:
```python
@dataclass
class BytesBalance:
    guild_id: str
    user_id: str
    balance: int
    total_received: int
    total_sent: int
    streak_count: int
    last_daily: Optional[date]

@dataclass
class DailyClaimResult:
    success: bool
    balance: Optional[BytesBalance] = None
    earned: Optional[int] = None
    streak: Optional[int] = None
    multiplier: Optional[int] = None
    reason: Optional[str] = None

@dataclass
class TransferResult:
    success: bool
    transaction: Optional[BytesTransaction] = None
    reason: Optional[str] = None
```

5. bot/client.py - API client:
```python
class APIClient:
    def __init__(self, base_url: str, bot_token: str):
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {bot_token}"}
        self._session: Optional[httpx.AsyncClient] = None
    
    async def __aenter__(self):
        self._session = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=30.0
        )
        return self
    
    async def __aexit__(self, *args):
        await self._session.aclose()
    
    async def get(self, path: str, **kwargs):
        return await self._session.get(path, **kwargs)
    
    async def post(self, path: str, **kwargs):
        return await self._session.post(path, **kwargs)
```

6. tests/bot/test_bytes_service.py - Service tests:
```python
class TestBytesService:
    @pytest.fixture
    def mock_api(self):
        return AsyncMock()
    
    @pytest.fixture
    def mock_redis(self):
        return AsyncMock()
    
    @pytest.fixture
    def service(self, mock_api, mock_redis):
        return BytesService(mock_api, mock_redis)
    
    async def test_get_balance_cached(self, service, mock_api):
        # Setup
        mock_api.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: {
                "guild_id": "123",
                "user_id": "456",
                "balance": 100,
                "total_received": 150,
                "total_sent": 50,
                "streak_count": 5,
                "last_daily": "2024-01-01"
            }
        )
        
        # First call hits API
        balance1 = await service.get_balance("123", "456")
        assert balance1.balance == 100
        assert mock_api.get.call_count == 1
        
        # Second call uses cache
        balance2 = await service.get_balance("123", "456")
        assert balance2.balance == 100
        assert mock_api.get.call_count == 1  # No additional call
    
    async def test_transfer_validation(self, service, mock_api):
        # Test self-transfer
        user = Mock(id="123", __str__=lambda self: "TestUser")
        result = await service.transfer_bytes(
            "guild_123", user, user, 100
        )
        assert not result.success
        assert "yourself" in result.reason
        
        # Test negative amount
        other_user = Mock(id="456", __str__=lambda self: "OtherUser")
        result = await service.transfer_bytes(
            "guild_123", user, other_user, -50
        )
        assert not result.success
        assert "positive" in result.reason
    
    async def test_calculate_multiplier(self, service):
        assert service._calculate_multiplier(0) == 1
        assert service._calculate_multiplier(7) == 2
        assert service._calculate_multiplier(14) == 4
        assert service._calculate_multiplier(30) == 10
        assert service._calculate_multiplier(60) == 20
        assert service._calculate_multiplier(100) == 20  # Max multiplier
```

7. tests/bot/test_squads_service.py - Squad service tests:
- Test squad listing
- Test join validation
- Test cost calculation
- Test error handling

All services should be:
- Fully testable without Discord
- Mockable for all external calls
- Type-safe with proper models
- Well-documented
```

---

## Session 5: Discord Bot Implementation

**Goal:** Create Discord bot with Hikari and Lightbulb using the service layer

```markdown
Create Discord bot that uses the service layer for all business logic.

Architecture:
- Bot plugins are thin wrappers around services
- All Discord-specific formatting in plugins
- Services handle all business logic
- Embeds and views for rich interactions

Create:

1. bot/bot.py - Bot setup and configuration:
```python
import hikari
import lightbulb
from shared.config import settings
from bot.client import APIClient
from bot.services.bytes_service import BytesService
from bot.services.squads_service import SquadsService

bot = lightbulb.BotApp(
    token=settings.DISCORD_TOKEN,
    intents=hikari.Intents.GUILDS | hikari.Intents.GUILD_MESSAGES,
    banner=None,
)

# Initialize services on startup
@bot.listen(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent):
    # Create API client
    bot.d.api_client = APIClient(
        base_url=settings.API_BASE_URL,
        bot_token=settings.BOT_API_TOKEN
    )
    
    # Create Redis client
    bot.d.redis = await create_redis_client()
    
    # Create services
    bot.d.bytes_service = BytesService(bot.d.api_client, bot.d.redis)
    bot.d.squads_service = SquadsService(bot.d.api_client, bot.d.redis)

# Load plugins
bot.load_extensions("bot.plugins.bytes", "bot.plugins.squads")

def run():
    bot.run()
```

2. bot/plugins/bytes.py - Bytes commands:
```python
import hikari
import lightbulb
from bot.utils.embeds import create_balance_embed, create_error_embed
from bot.utils.converters import parse_amount

plugin = lightbulb.Plugin("bytes", "Bytes economy commands")

@plugin.command
@lightbulb.command("bytes", "Bytes economy commands")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def bytes_group(ctx: lightbulb.Context) -> None:
    pass

@bytes_group.child
@lightbulb.command("balance", "Check your bytes balance")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def balance(ctx: lightbulb.SlashContext) -> None:
    service = ctx.bot.d.bytes_service
    
    # Get balance (may award daily)
    balance = await service.get_balance(str(ctx.guild_id), str(ctx.author.id))
    
    # Try to claim daily
    daily_result = await service.claim_daily(
        str(ctx.guild_id), 
        str(ctx.author.id),
        str(ctx.author)
    )
    
    # Create embed
    if daily_result.success:
        embed = create_balance_embed(
            balance=daily_result.balance,
            daily_earned=daily_result.earned,
            streak=daily_result.streak,
            multiplier=daily_result.multiplier
        )
        embed.title = "💰 Daily Bytes Claimed!"
        embed.color = hikari.Color(0x22c55e)  # Green for success
    else:
        embed = create_balance_embed(balance)
        embed.title = "💰 Your Bytes Balance"
    
    await ctx.respond(embed=embed)

@bytes_group.child
@lightbulb.option("reason", "Reason for sending bytes", required=False)
@lightbulb.option("amount", "Amount to send", type=int, min_value=1)
@lightbulb.option("user", "User to send bytes to", type=hikari.User)
@lightbulb.command("send", "Send bytes to another user")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def send(ctx: lightbulb.SlashContext) -> None:
    service = ctx.bot.d.bytes_service
    
    # Get options
    receiver = ctx.options.user
    amount = ctx.options.amount
    reason = ctx.options.reason
    
    # Validate receiver is in guild
    member = ctx.get_guild().get_member(receiver.id)
    if not member:
        embed = create_error_embed("That user is not in this server!")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    # Process transfer
    result = await service.transfer_bytes(
        str(ctx.guild_id),
        ctx.author,
        receiver,
        amount,
        reason
    )
    
    if not result.success:
        embed = create_error_embed(result.reason)
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    # Success embed
    embed = hikari.Embed(
        title="✅ Bytes Sent!",
        description=f"Successfully sent **{amount}** bytes to {receiver.mention}",
        color=hikari.Color(0x22c55e)
    )
    
    if reason:
        embed.add_field("Reason", reason, inline=False)
    
    await ctx.respond(embed=embed)

@bytes_group.child
@lightbulb.option("limit", "Number of users to show", type=int, default=10, min_value=1, max_value=25)
@lightbulb.command("leaderboard", "View the bytes leaderboard")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def leaderboard(ctx: lightbulb.SlashContext) -> None:
    service = ctx.bot.d.bytes_service
    
    # Get leaderboard
    entries = await service.get_leaderboard(str(ctx.guild_id), ctx.options.limit)
    
    if not entries:
        embed = create_error_embed("No leaderboard data yet!")
        await ctx.respond(embed=embed)
        return
    
    # Create embed
    embed = hikari.Embed(
        title="🏆 Bytes Leaderboard",
        color=hikari.Color(0x3b82f6)
    )
    
    # Build leaderboard text
    lines = []
    for entry in entries:
        # Try to get member
        member = ctx.get_guild().get_member(int(entry.user_id))
        name = member.display_name if member else f"User {entry.user_id}"
        
        # Medal for top 3
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(entry.rank, "🏅")
        
        lines.append(
            f"{medal} **{entry.rank}.** {name} - "
            f"**{entry.balance:,}** bytes (received: {entry.total_received:,})"
        )
    
    embed.description = "\n".join(lines)
    embed.set_footer(f"Showing top {len(entries)} users")
    
    await ctx.respond(embed=embed)

def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)

def unload(bot: lightbulb.BotApp) -> None:
    bot.remove_plugin(plugin)
```

3. bot/plugins/squads.py - Squad commands:
```python
from bot.views.squad_views import SquadSelectView, SquadConfirmView

@plugin.command
@lightbulb.command("squads", "Squad commands")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def squads_group(ctx: lightbulb.Context) -> None:
    pass

@squads_group.child
@lightbulb.command("list", "View available squads")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def list_squads(ctx: lightbulb.SlashContext) -> None:
    service = ctx.bot.d.squads_service
    
    squads = await service.list_squads(str(ctx.guild_id))
    
    if not squads:
        embed = create_error_embed("No squads have been created yet!")
        await ctx.respond(embed=embed)
        return
    
    # Get user's current squad
    user_squad = await service.get_user_squad(str(ctx.guild_id), str(ctx.author.id))
    
    embed = hikari.Embed(
        title="🏆 Available Squads",
        color=hikari.Color(0x3b82f6)
    )
    
    for squad in squads:
        # Get role color
        role = ctx.get_guild().get_role(int(squad.role_id))
        
        name = f"{'✅ ' if user_squad and user_squad.id == squad.id else ''}{squad.name}"
        value = squad.description or "No description"
        
        if user_squad and user_squad.id != squad.id:
            value += f"\n💰 Switch cost: **{squad.switch_cost}** bytes"
        
        embed.add_field(name, value, inline=False)
    
    await ctx.respond(embed=embed)

@squads_group.child
@lightbulb.command("join", "Join a squad")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def join_squad(ctx: lightbulb.SlashContext) -> None:
    service = ctx.bot.d.squads_service
    bytes_service = ctx.bot.d.bytes_service
    
    # Get available squads
    squads = await service.list_squads(str(ctx.guild_id))
    
    if not squads:
        embed = create_error_embed("No squads available to join!")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    # Get user's balance and current squad
    balance = await bytes_service.get_balance(str(ctx.guild_id), str(ctx.author.id))
    current_squad = await service.get_user_squad(str(ctx.guild_id), str(ctx.author.id))
    
    # Create squad selection view
    view = SquadSelectView(
        squads=squads,
        current_squad=current_squad,
        user_balance=balance.balance,
        timeout=60
    )
    
    embed = hikari.Embed(
        title="🏆 Select a Squad",
        description="Choose a squad to join. You have 60 seconds to decide.",
        color=hikari.Color(0x3b82f6)
    )
    
    embed.add_field("Your Balance", f"**{balance.balance:,}** bytes", inline=True)
    
    if current_squad:
        embed.add_field("Current Squad", current_squad.name, inline=True)
    
    message = await ctx.respond(embed=embed, components=view, flags=hikari.MessageFlag.EPHEMERAL)
    
    # Wait for selection
    await view.wait()
    
    if view.selected_squad_id is None:
        embed = create_error_embed("Squad selection timed out!")
        await message.edit(embed=embed, components=[])
        return
    
    # Process squad join
    result = await service.join_squad(
        str(ctx.guild_id),
        str(ctx.author.id),
        view.selected_squad_id,
        balance.balance
    )
    
    if not result.success:
        embed = create_error_embed(result.reason)
        await message.edit(embed=embed, components=[])
        return
    
    # Success!
    embed = hikari.Embed(
        title="✅ Squad Joined!",
        description=f"You've successfully joined **{result.squad.name}**!",
        color=hikari.Color(0x22c55e)
    )
    
    if result.cost > 0:
        embed.add_field("Cost", f"**{result.cost}** bytes", inline=True)
        new_balance = balance.balance - result.cost
        embed.add_field("New Balance", f"**{new_balance:,}** bytes", inline=True)
    
    await message.edit(embed=embed, components=[])
```

4. bot/views/squad_views.py - Interactive components:
```python
import hikari
from typing import List, Optional, UUID

class SquadSelectView(hikari.impl.ActionRowBuilder):
    def __init__(self, squads: List[Squad], current_squad: Optional[Squad], 
                 user_balance: int, timeout: int = 60):
        super().__init__()
        self.squads = squads
        self.current_squad = current_squad
        self.user_balance = user_balance
        self.selected_squad_id: Optional[UUID] = None
        
        # Create select menu
        options = []
        for squad in squads[:25]:  # Discord limit
            # Calculate cost
            cost = squad.switch_cost if current_squad and current_squad.id != squad.id else 0
            can_afford = user_balance >= cost
            
            option = hikari.SelectMenuOption(
                label=squad.name,
                value=str(squad.id),
                description=f"Cost: {cost} bytes" if cost > 0 else "Free to join!",
                emoji="✅" if current_squad and current_squad.id == squad.id else None,
                is_default=current_squad and current_squad.id == squad.id
            )
            
            if not can_afford and cost > 0:
                option.description = f"⚠️ Need {cost} bytes (you have {user_balance})"
            
            options.append(option)
        
        self.add_select_menu(
            custom_id="squad_select",
            options=options,
            placeholder="Choose a squad..."
        )
```

5. bot/utils/embeds.py - Embed builders:
```python
def create_balance_embed(
    balance: BytesBalance,
    daily_earned: Optional[int] = None,
    streak: Optional[int] = None,
    multiplier: Optional[int] = None
) -> hikari.Embed:
    embed = hikari.Embed(color=hikari.Color(0x3b82f6))
    
    # Main balance
    embed.add_field("Balance", f"**{balance.balance:,}** bytes", inline=True)
    embed.add_field("Total Received", f"{balance.total_received:,}", inline=True)
    embed.add_field("Total Sent", f"{balance.total_sent:,}", inline=True)
    
    # Daily info if provided
    if daily_earned is not None:
        embed.add_field(
            "Daily Earned", 
            f"**+{daily_earned}** bytes", 
            inline=True
        )
        
        if streak and streak > 1:
            streak_name = get_streak_name(streak)
            embed.add_field(
                "Streak", 
                f"🔥 **{streak}** days ({streak_name})", 
                inline=True
            )
            
            if multiplier and multiplier > 1:
                embed.add_field(
                    "Multiplier", 
                    f"**{multiplier}x**", 
                    inline=True
                )
    
    return embed

def get_streak_name(days: int) -> str:
    if days >= 60:
        return "LEGENDARY"
    elif days >= 30:
        return "EPIC"
    elif days >= 14:
        return "RARE"
    elif days >= 7:
        return "COMMON"
    return "BUILDING"
```

6. tests/bot/test_bot_integration.py - Integration tests:
```python
@pytest.mark.asyncio
class TestBotIntegration:
    async def test_bytes_balance_command(self, bot_app, mock_service):
        # Mock service response
        mock_service.get_balance.return_value = BytesBalance(
            guild_id="123",
            user_id="456",
            balance=100,
            total_received=150,
            total_sent=50,
            streak_count=5,
            last_daily=date.today()
        )
        
        mock_service.claim_daily.return_value = DailyClaimResult(
            success=False,
            reason="Already claimed"
        )
        
        # Simulate command
        ctx = create_mock_context(
            guild_id="123",
            author_id="456"
        )
        
        await balance(ctx)
        
        # Verify response
        assert ctx.respond.called
        embed = ctx.respond.call_args[1]["embed"]
        assert "100" in str(embed.fields[0].value)
```

All bot code should:
- Use services for business logic
- Handle errors gracefully
- Provide rich Discord interactions
- Be testable through mocked services
```

---

## Session 6: Admin Interface Implementation

**Goal:** Create authenticated admin interface for guild configuration

```markdown
Create web admin interface for configuring bytes and squads per guild.

Requirements:
1. Integrate with existing Starlette app
2. Use session-based authentication
3. Fetch Discord data on-demand (no duplication)
4. Clean UI with proper error handling
5. Test admin routes and authentication

Create:

1. web/admin/auth.py - Authentication for admin:
```python
from starlette.authentication import requires
from starlette.responses import RedirectResponse
from functools import wraps

def admin_required(func):
    """Decorator to require admin authentication"""
    @wraps(func)
    async def wrapper(request):
        # Check if user is authenticated
        if not request.user.is_authenticated:
            return RedirectResponse(url="/admin/login", status_code=303)
        
        # In production, check Discord OAuth
        # In dev mode, just check session
        if not request.session.get("is_admin"):
            return RedirectResponse(url="/admin/login", status_code=303)
        
        return await func(request)
    return wrapper

async def login(request):
    """Admin login page"""
    if request.method == "GET":
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request}
        )
    
    # POST - Dev mode only
    form = await request.form()
    username = form.get("username")
    
    if username and len(username) >= 3:
        request.session["user_id"] = username
        request.session["is_admin"] = True
        
        # Redirect to requested page or dashboard
        next_url = request.query_params.get("next", "/admin")
        return RedirectResponse(url=next_url, status_code=303)
    
    return templates.TemplateResponse(
        "admin/login.html",
        {"request": request, "error": "Invalid username"}
    )

async def logout(request):
    """Admin logout"""
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
```

2. web/admin/routes.py - Admin routes:
```python
from starlette.routing import Route, Mount
from web.admin.views import *
from web.admin.auth import admin_required, login, logout

admin_routes = [
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/", admin_required(dashboard), name="admin_dashboard"),
    Route("/guilds", admin_required(guild_list), name="admin_guilds"),
    Route("/guilds/{guild_id}", admin_required(guild_detail), name="admin_guild_detail"),
    Route("/guilds/{guild_id}/bytes", admin_required(bytes_config), methods=["GET", "POST"]),
    Route("/guilds/{guild_id}/squads", admin_required(squads_config), methods=["GET", "POST"]),
]

# Mount to main app
app.mount("/admin", Mount(routes=admin_routes))
```

3. web/admin/views.py - Admin view handlers:
```python
from web.admin.discord import get_bot_guilds, get_guild_info
from web.crud import BytesOperations, SquadOperations

async def dashboard(request):
    """Admin dashboard with overview"""
    # Get bot guilds from Discord
    guilds = await get_bot_guilds()
    
    # Get stats from database
    async with get_db_session() as session:
        total_users = await session.execute(
            select(func.count(distinct(BytesBalance.user_id)))
        )
        total_transactions = await session.execute(
            select(func.count(BytesTransaction.id))
        )
    
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "guilds": guilds,
            "total_users": total_users.scalar(),
            "total_transactions": total_transactions.scalar()
        }
    )

async def guild_detail(request):
    """Guild detail page"""
    guild_id = request.path_params["guild_id"]
    
    # Fetch guild info from Discord
    try:
        guild = await get_guild_info(guild_id)
    except GuildNotFoundError:
        return templates.TemplateResponse(
            "admin/error.html",
            {"request": request, "error": "Guild not found"},
            status_code=404
        )
    
    # Get guild stats
    async with get_db_session() as session:
        ops = BytesOperations()
        
        # Get top users
        top_users = await ops.get_leaderboard(session, guild_id, limit=5)
        
        # Get recent transactions
        recent_transactions = await session.execute(
            select(BytesTransaction)
            .where(BytesTransaction.guild_id == guild_id)
            .order_by(BytesTransaction.created_at.desc())
            .limit(10)
        )
        
        # Get config
        config = await ops.get_config(session, guild_id)
    
    return templates.TemplateResponse(
        "admin/guild_detail.html",
        {
            "request": request,
            "guild": guild,
            "top_users": top_users,
            "recent_transactions": recent_transactions.scalars().all(),
            "config": config
        }
    )

async def bytes_config(request):
    """Bytes configuration for guild"""
    guild_id = request.path_params["guild_id"]
    
    # Verify guild exists
    try:
        guild = await get_guild_info(guild_id)
    except GuildNotFoundError:
        return templates.TemplateResponse(
            "admin/error.html",
            {"request": request, "error": "Guild not found"},
            status_code=404
        )
    
    async with get_db_session() as session:
        ops = BytesOperations()
        
        if request.method == "GET":
            config = await ops.get_config(session, guild_id)
            
            return templates.TemplateResponse(
                "admin/bytes_config.html",
                {
                    "request": request,
                    "guild": guild,
                    "config": config or BytesConfig.get_defaults(guild_id)
                }
            )
        
        # POST - Update config
        form = await request.form()
        
        config_data = {
            "starting_balance": int(form.get("starting_balance", 100)),
            "daily_amount": int(form.get("daily_amount", 10)),
            "max_transfer": int(form.get("max_transfer", 1000)),
            "transfer_cooldown_hours": int(form.get("transfer_cooldown_hours", 0))
        }
        
        # Parse role rewards
        role_rewards = {}
        for key, value in form.items():
            if key.startswith("role_reward_"):
                role_id = key.replace("role_reward_", "")
                if value:
                    role_rewards[role_id] = int(value)
        
        config_data["role_rewards"] = role_rewards
        
        # Update config
        config = await ops.update_config(session, guild_id, config_data)
        await session.commit()
        
        # Notify bot via Redis
        await redis.publish(
            f"config_update:{guild_id}",
            json.dumps({"type": "bytes", "guild_id": guild_id})
        )
        
        return templates.TemplateResponse(
            "admin/bytes_config.html",
            {
                "request": request,
                "guild": guild,
                "config": config,
                "success": "Configuration updated successfully!"
            }
        )

async def squads_config(request):
    """Squad management for guild"""
    guild_id = request.path_params["guild_id"]
    
    # Verify guild exists
    try:
        guild = await get_guild_info(guild_id)
        guild_roles = await get_guild_roles(guild_id)
    except GuildNotFoundError:
        return templates.TemplateResponse(
            "admin/error.html",
            {"request": request, "error": "Guild not found"},
            status_code=404
        )
    
    async with get_db_session() as session:
        ops = SquadOperations()
        
        if request.method == "GET":
            squads = await ops.list_squads(session, guild_id)
            
            return templates.TemplateResponse(
                "admin/squads_config.html",
                {
                    "request": request,
                    "guild": guild,
                    "guild_roles": guild_roles,
                    "squads": squads
                }
            )
        
        # POST - Handle squad actions
        form = await request.form()
        action = form.get("action")
        
        if action == "create":
            squad_data = {
                "guild_id": guild_id,
                "role_id": form.get("role_id"),
                "name": form.get("name"),
                "description": form.get("description"),
                "switch_cost": int(form.get("switch_cost", 50)),
                "max_members": int(form.get("max_members")) if form.get("max_members") else None
            }
            
            await ops.create_squad(session, squad_data)
            await session.commit()
            
            success = "Squad created successfully!"
        
        elif action == "update":
            squad_id = UUID(form.get("squad_id"))
            updates = {
                "name": form.get("name"),
                "description": form.get("description"),
                "switch_cost": int(form.get("switch_cost")),
                "is_active": form.get("is_active") == "on"
            }
            
            await ops.update_squad(session, squad_id, updates)
            await session.commit()
            
            success = "Squad updated successfully!"
        
        elif action == "delete":
            squad_id = UUID(form.get("squad_id"))
            await ops.delete_squad(session, squad_id)
            await session.commit()
            
            success = "Squad deleted successfully!"
        
        # Refresh squads list
        squads = await ops.list_squads(session, guild_id)
        
        return templates.TemplateResponse(
            "admin/squads_config.html",
            {
                "request": request,
                "guild": guild,
                "guild_roles": guild_roles,
                "squads": squads,
                "success": success
            }
        )
```

4. web/admin/discord.py - Discord API helpers:
```python
import httpx
from typing import List, Dict
from shared.config import settings

class DiscordClient:
    def __init__(self, bot_token: str):
        self.headers = {"Authorization": f"Bot {bot_token}"}
        self.base_url = "https://discord.com/api/v10"
    
    async def get_bot_guilds(self) -> List[Dict]:
        """Get all guilds the bot is in"""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/users/@me/guilds",
                headers=self.headers
            )
            response.raise_for_status()
            return response.json()
    
    async def get_guild(self, guild_id: str) -> Dict:
        """Get guild information"""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/guilds/{guild_id}",
                headers=self.headers
            )
            
            if response.status_code == 404:
                raise GuildNotFoundError(f"Guild {guild_id} not found")
            
            response.raise_for_status()
            return response.json()
    
    async def get_guild_roles(self, guild_id: str) -> List[Dict]:
        """Get guild roles"""
        guild = await self.get_guild(guild_id)
        return guild.get("roles", [])

# Create singleton
discord_client = DiscordClient(settings.DISCORD_BOT_TOKEN)

async def get_bot_guilds():
    return await discord_client.get_bot_guilds()

async def get_guild_info(guild_id: str):
    return await discord_client.get_guild(guild_id)

async def get_guild_roles(guild_id: str):
    return await discord_client.get_guild_roles(guild_id)
```

5. web/templates/admin/base.html - Base admin template:
```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Smarter Dev Admin{% endblock %}</title>
    <link rel="stylesheet" href="https://unpkg.com/@tabler/core@latest/dist/css/tabler.min.css">
</head>
<body>
    <div class="page">
        <header class="navbar navbar-expand-md navbar-dark navbar-overlap d-print-none">
            <div class="container-xl">
                <h1 class="navbar-brand navbar-brand-autodark d-none-navbar-horizontal pe-0 pe-md-3">
                    <a href="/admin">Smarter Dev Admin</a>
                </h1>
                <div class="navbar-nav flex-row order-md-last">
                    <div class="nav-item dropdown">
                        <a href="#" class="nav-link d-flex lh-1 text-reset p-0" data-bs-toggle="dropdown">
                            <span class="avatar avatar-sm">{{ request.session.user_id[:2].upper() }}</span>
                        </a>
                        <div class="dropdown-menu dropdown-menu-end dropdown-menu-arrow">
                            <form action="/admin/logout" method="post">
                                <button type="submit" class="dropdown-item">Logout</button>
                            </form>
                        </div>
                    </div>
                </div>
            </div>
        </header>
        
        <div class="page-wrapper">
            <div class="container-xl">
                {% block content %}{% endblock %}
            </div>
        </div>
    </div>
    
    <script src="https://unpkg.com/@tabler/core@latest/dist/js/tabler.min.js"></script>
    {% block scripts %}{% endblock %}
</body>
</html>
```

6. tests/web/test_admin.py - Admin interface tests:
```python
class TestAdminInterface:
    @pytest.fixture
    async def admin_client(self, client):
        """Client with admin session"""
        client.session["user_id"] = "test_admin"
        client.session["is_admin"] = True
        return client
    
    async def test_dashboard_requires_auth(self, client):
        response = await client.get("/admin/")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"
    
    async def test_dashboard_with_auth(self, admin_client, mock_discord):
        mock_discord.get_bot_guilds.return_value = [
            {"id": "123", "name": "Test Guild", "icon": None}
        ]
        
        response = await admin_client.get("/admin/")
        assert response.status_code == 200
        assert b"Test Guild" in response.content
    
    async def test_bytes_config_update(self, admin_client, mock_discord):
        mock_discord.get_guild.return_value = {
            "id": "123",
            "name": "Test Guild",
            "icon": None
        }
        
        response = await admin_client.post(
            "/admin/guilds/123/bytes",
            data={
                "starting_balance": "200",
                "daily_amount": "20",
                "max_transfer": "2000",
                "transfer_cooldown_hours": "0"
            }
        )
        
        assert response.status_code == 200
        assert b"Configuration updated successfully!" in response.content
```

All admin pages should:
- Require authentication
- Fetch Discord data on-demand
- Handle errors gracefully
- Provide clear feedback
- Be fully tested
```

---

## Session 7: Integration and End-to-End Testing

**Goal:** Create comprehensive tests and ensure all components work together

```markdown
Create integration tests and ensure all components work together seamlessly.

Requirements:
1. Test bot ↔ API communication
2. Test admin → API → Redis → bot flow
3. Test error scenarios
4. Create fixtures for common test data
5. Document testing approach

Create:

1. tests/conftest.py - Shared test fixtures:
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

2. tests/integration/test_bytes_flow.py - End-to-end bytes tests:
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

3. tests/integration/test_squads_flow.py - Squad system integration:
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

4. tests/integration/test_admin_flow.py - Admin interface integration:
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

5. tests/integration/test_error_scenarios.py - Error handling tests:
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

6. Documentation in CLAUDE.md addition:
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

All integration tests should:
- Test complete user flows
- Verify all side effects
- Handle error scenarios
- Use proper mocking
- Be independent and repeatable
```

---

## Session 8: Production Readiness

**Goal:** Prepare the application for production deployment

```markdown
Prepare the Smarter Dev application for production deployment.

Requirements:
1. Environment configuration for production
2. Logging and monitoring setup
3. Performance optimizations
4. Security hardening
5. Deployment configuration

Create:

1. shared/logging.py - Comprehensive logging setup:
```python
import logging
import sys
from logging.handlers import RotatingFileHandler
from shared.config import settings

def setup_logging(name: str, level: str = "INFO") -> logging.Logger:
    """Setup logging with console and file handlers"""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console_handler.setFormatter(console_format)
    
    # File handler (production only)
    if not settings.DEV_MODE:
        file_handler = RotatingFileHandler(
            f"logs/{name}.log",
            maxBytes=10_000_000,  # 10MB
            backupCount=5
        )
        file_format = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
        )
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)
    
    logger.addHandler(console_handler)
    return logger

# Create loggers
bot_logger = setup_logging("bot", settings.LOG_LEVEL)
web_logger = setup_logging("web", settings.LOG_LEVEL)
```

2. web/middleware/monitoring.py - Request monitoring:
```python
import time
from starlette.middleware.base import BaseHTTPMiddleware
from shared.logging import web_logger

class MonitoringMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start_time = time.time()
        
        # Add request ID
        request.state.request_id = generate_request_id()
        
        try:
            response = await call_next(request)
            
            # Log request
            duration = time.time() - start_time
            web_logger.info(
                f"Request {request.state.request_id} - "
                f"{request.method} {request.url.path} - "
                f"Status: {response.status_code} - "
                f"Duration: {duration:.3f}s"
            )
            
            # Add headers
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers["X-Response-Time"] = f"{duration:.3f}"
            
            return response
            
        except Exception as e:
            duration = time.time() - start_time
            web_logger.error(
                f"Request {request.state.request_id} failed - "
                f"{request.method} {request.url.path} - "
                f"Error: {str(e)} - "
                f"Duration: {duration:.3f}s",
                exc_info=True
            )
            raise
```

3. web/middleware/security.py - Security headers:
```python
from starlette.middleware.base import BaseHTTPMiddleware

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        
        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        
        # HSTS (only in production with HTTPS)
        if not settings.DEV_MODE:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        
        return response
```

4. bot/monitoring.py - Bot health checks:
```python
import asyncio
from datetime import datetime
from shared.logging import bot_logger

class BotMonitor:
    def __init__(self, bot, redis_client):
        self.bot = bot
        self.redis = redis_client
        self.start_time = datetime.utcnow()
    
    async def start_heartbeat(self):
        """Send heartbeat to Redis every 30 seconds"""
        while True:
            try:
                await self.redis.setex(
                    "bot:heartbeat",
                    60,  # TTL 60 seconds
                    json.dumps({
                        "timestamp": datetime.utcnow().isoformat(),
                        "guilds": len(self.bot.cache.get_guilds_view()),
                        "uptime": (datetime.utcnow() - self.start_time).total_seconds(),
                        "version": settings.VERSION
                    })
                )
                bot_logger.debug("Heartbeat sent")
            except Exception as e:
                bot_logger.error(f"Heartbeat failed: {e}")
            
            await asyncio.sleep(30)
    
    async def log_command_usage(self, ctx: lightbulb.Context):
        """Log command usage for analytics"""
        try:
            await self.redis.hincrby(
                f"stats:commands:{datetime.utcnow().strftime('%Y-%m-%d')}",
                f"{ctx.guild_id}:{ctx.command.name}",
                1
            )
        except Exception as e:
            bot_logger.error(f"Failed to log command usage: {e}")
```

5. .env.production - Production environment template:
```bash
# Application
DEV_MODE=false
LOG_LEVEL=INFO
VERSION=1.0.0

# Database
DATABASE_URL=postgresql+asyncpg://smarter:password@postgres:5432/smarter_dev

# Redis
REDIS_URL=redis://redis:6379/0

# Discord
DISCORD_TOKEN=your-bot-token
DISCORD_APPLICATION_ID=your-app-id
DISCORD_CLIENT_ID=your-client-id
DISCORD_CLIENT_SECRET=your-client-secret

# Web
SESSION_SECRET=generate-a-secure-random-secret
API_BASE_URL=https://api.smarter.dev
BOT_API_TOKEN=generate-a-secure-token

# Admin (Production uses Discord OAuth)
ADMIN_DISCORD_IDS=["your-discord-id"]
```

6. docker-compose.production.yml:
```yaml
version: '3.8'

services:
  postgres:
    image: postgres:15-alpine
    environment:
      POSTGRES_DB: smarter_dev
      POSTGRES_USER: smarter
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped
    networks:
      - backend

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data
    restart: unless-stopped
    networks:
      - backend

  web:
    build:
      context: .
      dockerfile: Dockerfile.web
    environment:
      - DEV_MODE=false
    env_file:
      - .env.production
    ports:
      - "8000:8000"
    depends_on:
      - postgres
      - redis
    restart: unless-stopped
    networks:
      - backend
    volumes:
      - ./logs:/app/logs

  bot:
    build:
      context: .
      dockerfile: Dockerfile.bot
    environment:
      - DEV_MODE=false
    env_file:
      - .env.production
    depends_on:
      - postgres
      - redis
      - web
    restart: unless-stopped
    networks:
      - backend
    volumes:
      - ./logs:/app/logs

volumes:
  postgres_data:
  redis_data:

networks:
  backend:
    driver: bridge
```

7. Dockerfile.web - Web application container:
```dockerfile
FROM python:3.11-slim as builder

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install uv

# Copy dependency files
COPY pyproject.toml .
COPY uv.lock .

# Install dependencies
RUN uv sync --no-dev

FROM python:3.11-slim

WORKDIR /app

# Copy from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application
COPY web web/
COPY shared shared/
COPY alembic alembic/
COPY alembic.ini .

# Create logs directory
RUN mkdir -p logs

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Run migrations and start server
CMD ["/app/.venv/bin/python", "-m", "alembic", "upgrade", "head", "&&", \
     "/app/.venv/bin/uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

8. Performance optimizations in web/main.py:
```python
from starlette.middleware.gzip import GZipMiddleware

# Add compression
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Configure connection pooling
@asynccontextmanager
async def lifespan(app):
    # Create connection pools
    app.state.db_engine = create_async_engine(
        settings.DATABASE_URL,
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=3600
    )
    
    app.state.redis = await aioredis.create_redis_pool(
        settings.REDIS_URL,
        minsize=5,
        maxsize=20
    )
    
    yield
    
    # Cleanup
    await app.state.db_engine.dispose()
    app.state.redis.close()
    await app.state.redis.wait_closed()
```

9. scripts/deploy.sh - Deployment script:
```bash
#!/bin/bash
set -e

echo "Starting deployment..."

# Run tests
echo "Running tests..."
uv run pytest

# Build containers
echo "Building containers..."
docker-compose -f docker-compose.production.yml build

# Run migrations
echo "Running migrations..."
docker-compose -f docker-compose.production.yml run --rm web \
    /app/.venv/bin/python -m alembic upgrade head

# Start services
echo "Starting services..."
docker-compose -f docker-compose.production.yml up -d

# Health check
echo "Waiting for services to be healthy..."
sleep 10

# Check web health
curl -f http://localhost:8000/health || exit 1

echo "Deployment complete!"
```

10. web/health.py - Health check endpoints:
```python
from starlette.responses import JSONResponse

async def health_check(request):
    """Basic health check"""
    return JSONResponse({"status": "healthy", "version": settings.VERSION})

async def detailed_health(request):
    """Detailed health check for monitoring"""
    checks = {
        "database": False,
        "redis": False,
        "bot": False
    }
    
    # Check database
    try:
        async with get_db_session() as session:
            await session.execute("SELECT 1")
        checks["database"] = True
    except:
        pass
    
    # Check Redis
    try:
        await request.app.state.redis.ping()
        checks["redis"] = True
    except:
        pass
    
    # Check bot heartbeat
    try:
        heartbeat = await request.app.state.redis.get("bot:heartbeat")
        if heartbeat:
            data = json.loads(heartbeat)
            last_beat = datetime.fromisoformat(data["timestamp"])
            if (datetime.utcnow() - last_beat).seconds < 90:
                checks["bot"] = True
    except:
        pass
    
    all_healthy = all(checks.values())
    
    return JSONResponse(
        {
            "status": "healthy" if all_healthy else "unhealthy",
            "checks": checks,
            "version": settings.VERSION,
            "timestamp": datetime.utcnow().isoformat()
        },
        status_code=200 if all_healthy else 503
    )

# Add to routes
app.add_route("/health", health_check)
app.add_route("/health/detailed", detailed_health)
```

This production setup includes:
- Comprehensive logging and monitoring
- Security hardening with proper headers
- Performance optimizations (connection pooling, compression)
- Docker containers for easy deployment
- Health check endpoints
- Automated deployment script
- Proper error handling and recovery
```