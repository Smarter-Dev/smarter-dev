# Session 4: Bot Service Layer

**Goal:** Create testable service layer for bot business logic

## Task Description

Create service layer for the Discord bot that separates business logic from Discord-specific code.

### Architecture
- Services contain all business logic
- Services are Discord-agnostic and fully testable
- Bot plugins are thin wrappers that call services
- All external calls (API, Redis) are mockable

## Deliverables

### 1. bot/services/base.py - Base service class:
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

### 2. bot/services/bytes_service.py - Bytes business logic:
```python
class BytesService(BaseService):
    STREAK_MULTIPLIERS = {7: 2, 14: 4, 30: 10, 60: 20}
    
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

### 3. bot/services/squads_service.py - Squad business logic:
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

### 4. bot/services/models.py - Service layer models:
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

### 5. bot/client.py - API client:
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

### 6. tests/bot/test_bytes_service.py - Service tests:
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

### 7. tests/bot/test_squads_service.py - Squad service tests:
- Test squad listing
- Test join validation
- Test cost calculation
- Test error handling

## Quality Requirements
All services should be:
- Fully testable without Discord
- Mockable for all external calls
- Type-safe with proper models
- Well-documented