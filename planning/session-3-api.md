# Session 3: Web API Implementation

**Goal:** Create FastAPI endpoints for bytes and squads systems with comprehensive testing

## Task Description

Create REST API endpoints for the bytes and squads systems using FastAPI mounted to the existing Starlette app.

### Requirements
1. Mount FastAPI app at /api on existing Starlette app
2. Use bearer token authentication for bot
3. Comprehensive input validation
4. Proper error responses
5. Test all endpoints using httpx.AsyncClient

## Deliverables

### 1. web/api/app.py - FastAPI app setup:
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

### 2. web/api/dependencies.py - Shared dependencies:
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

### 3. web/api/schemas.py - Pydantic models:
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

### 4. web/api/routers/bytes.py - Bytes endpoints:
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

### 5. web/api/routers/squads.py - Squad endpoints:
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

### 6. Mount API to existing Starlette app:
```python
# In existing main.py
from web.api.app import api

# Mount the API
app.mount("/api", api)
```

### 7. tests/web/test_api.py - Comprehensive API tests:
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

### 8. tests/web/test_squads_api.py - Squad API tests:
- Test squad CRUD operations
- Test join/leave with bytes costs
- Test validation errors
- Test concurrent squad operations

## Quality Requirements
All endpoints should have:
- Proper authentication
- Input validation
- Error handling
- Comprehensive tests
- OpenAPI documentation