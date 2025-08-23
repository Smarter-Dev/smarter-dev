# Session 7.4: Configuration and Caching - Bytes Economy Management

## Overview
Implement configuration management and caching pipeline for bytes system settings, ensuring efficient configuration retrieval and consistent guild-specific behavior.

## Key Components
- Guild configuration caching system
- Default configuration management
- Configuration validation and fallbacks
- Cache invalidation strategies
- Performance optimization through caching

## Implementation Details

### Configuration Service with Caching
Core configuration management with intelligent caching:

```python
async def get_config(self, guild_id: str) -> Dict[str, Any]:
    """Get guild bytes configuration with caching."""
    cache_key = f"config:{guild_id}"
    now = datetime.utcnow()
    
    # Check cache
    if cache_key in self._config_cache:
        cache_time = self._config_cache_time.get(cache_key)
        if cache_time and (now - cache_time).total_seconds() < 300:  # 5 min cache
            return self._config_cache[cache_key]
    
    try:
        # Fetch from API
        config = await self.api.get_bytes_config(guild_id)
        
        # Cache it
        self._config_cache[cache_key] = config
        self._config_cache_time[cache_key] = now
        
        return config
        
    except Exception as e:
        logger.error(
            "Failed to fetch bytes config",
            guild_id=guild_id,
            error=str(e)
        )
        
        # Return defaults
        return {
            "starting_balance": DEFAULT_STARTING_BALANCE,
            "daily_amount": DEFAULT_DAILY_AMOUNT,
            "max_transfer": 1000,
            "cooldown_hours": 24,
            "role_rewards": {}
        }
```

### API Configuration Endpoint
Backend endpoint for retrieving guild-specific configurations:

```python
@router.get("/guilds/{guild_id}/config/bytes")
async def get_bytes_config(
    guild_id: str,
    api_key: CurrentAPIKey,
    db: DatabaseSession
):
    """Get guild bytes configuration."""
    config = await db.execute(
        select(BytesConfig).where(BytesConfig.guild_id == guild_id)
    )
    config_data = config.scalar_one_or_none()
    
    if not config_data:
        # Return defaults
        return {
            "starting_balance": 100,
            "daily_amount": 10,
            "max_transfer": 1000,
            "cooldown_hours": 24,
            "role_rewards": {}
        }
    
    return {
        "starting_balance": config_data.starting_balance,
        "daily_amount": config_data.daily_amount,
        "max_transfer": config_data.max_transfer,
        "cooldown_hours": config_data.cooldown_hours,
        "role_rewards": config_data.role_rewards
    }
```

### Configuration Model
Database model for storing guild-specific settings:

```python
class BytesConfig(Base):
    """Guild bytes configuration."""
    __tablename__ = "bytes_configs"
    
    guild_id = Column(String, primary_key=True)
    starting_balance = Column(Integer, default=100, nullable=False)
    daily_amount = Column(Integer, default=10, nullable=False)
    max_transfer = Column(Integer, default=1000, nullable=False)
    cooldown_hours = Column(Integer, default=24, nullable=False)
    role_rewards = Column(JSON, default=dict, nullable=False)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    def __repr__(self):
        return f"<BytesConfig guild_id={self.guild_id} daily={self.daily_amount}>"
```

### Cache Management Service
Service for managing configuration cache lifecycle:

```python
class ConfigCache:
    """Configuration cache manager."""
    
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_time: Dict[str, datetime] = {}
        self._default_ttl = 300  # 5 minutes
    
    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Get cached config if not expired."""
        if key not in self._cache:
            return None
        
        cache_time = self._cache_time.get(key)
        if not cache_time:
            return None
        
        # Check if expired
        if (datetime.utcnow() - cache_time).total_seconds() > self._default_ttl:
            self.invalidate(key)
            return None
        
        return self._cache[key]
    
    def set(self, key: str, value: Dict[str, Any]) -> None:
        """Cache configuration data."""
        self._cache[key] = value
        self._cache_time[key] = datetime.utcnow()
    
    def invalidate(self, key: str) -> None:
        """Remove cached config."""
        self._cache.pop(key, None)
        self._cache_time.pop(key, None)
    
    def invalidate_all(self) -> None:
        """Clear all cached configs."""
        self._cache.clear()
        self._cache_time.clear()
```

### Configuration Validation
Validation pipeline for ensuring configuration integrity:

```python
def validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and sanitize configuration."""
    validated = {}
    
    # Starting balance validation
    starting_balance = config.get("starting_balance", 100)
    validated["starting_balance"] = max(0, min(starting_balance, 10000))
    
    # Daily amount validation
    daily_amount = config.get("daily_amount", 10)
    validated["daily_amount"] = max(1, min(daily_amount, 1000))
    
    # Max transfer validation
    max_transfer = config.get("max_transfer", 1000)
    validated["max_transfer"] = max(1, min(max_transfer, 100000))
    
    # Cooldown hours validation
    cooldown_hours = config.get("cooldown_hours", 24)
    validated["cooldown_hours"] = max(1, min(cooldown_hours, 168))  # Max 1 week
    
    # Role rewards validation
    role_rewards = config.get("role_rewards", {})
    if isinstance(role_rewards, dict):
        validated["role_rewards"] = {
            str(role_id): max(0, threshold)
            for role_id, threshold in role_rewards.items()
            if isinstance(threshold, int)
        }
    else:
        validated["role_rewards"] = {}
    
    return validated
```

### Processing Pipeline Integration
Integration with the main bytes processing pipeline:

```python
async def process_daily_award(
    self,
    guild_id: str,
    user_id: str,
    username: str
) -> Tuple[int, int, StreakMultiplier]:
    """Process daily award with configuration."""
    # Get cached configuration
    config = await self.get_config(guild_id)
    
    # Validate configuration
    config = validate_config(config)
    
    # Process award with validated config
    base_amount = config["daily_amount"]
    
    # Get current streak
    balance_data = await self.check_balance(guild_id, user_id, username)
    current_streak = balance_data.get("streak_count", 0)
    
    # Calculate amount with multiplier
    amount, multiplier = self.calculate_daily_amount(base_amount, current_streak)
    
    # Award through API
    result = await self.api.award_daily_bytes(guild_id, user_id, username)
    
    return amount, result["streak_count"], multiplier
```

### Admin Configuration Interface
Admin interface for managing guild configurations:

```javascript
// Frontend configuration management
async function updateBytesConfig(guildId, configData) {
    try {
        const response = await fetch(`/api/v1/guilds/${guildId}/config/bytes`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${API_KEY}`
            },
            body: JSON.stringify(configData)
        });
        
        if (response.ok) {
            // Invalidate cache
            await fetch(`/api/v1/guilds/${guildId}/config/bytes/cache`, {
                method: 'DELETE',
                headers: {
                    'Authorization': `Bearer ${API_KEY}`
                }
            });
            
            showSuccess('Configuration updated successfully!');
        } else {
            showError('Failed to update configuration');
        }
    } catch (error) {
        showError('Network error occurred');
    }
}
```

### Performance Monitoring
Monitoring configuration cache performance:

```python
class ConfigMetrics:
    """Configuration cache metrics."""
    
    def __init__(self):
        self.cache_hits = 0
        self.cache_misses = 0
        self.api_calls = 0
        self.error_count = 0
    
    def hit(self):
        self.cache_hits += 1
    
    def miss(self):
        self.cache_misses += 1
    
    def api_call(self):
        self.api_calls += 1
    
    def error(self):
        self.error_count += 1
    
    @property
    def hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0
    
    def report(self) -> Dict[str, Any]:
        return {
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "hit_rate": self.hit_rate,
            "api_calls": self.api_calls,
            "errors": self.error_count
        }
```

## Related Files
- `bot/services/bytes_service.py` - Configuration service integration
- `web/api/routers/bytes.py` - Configuration API endpoints
- `web/models/bytes.py` - BytesConfig database model
- `shared/constants.py` - Default configuration values
- `bot/utils/cache.py` - Cache management utilities

## Goals Achieved
- **Performance Optimization**: 5-minute cache reduces API calls
- **Reliability**: Fallback to defaults prevents service interruption
- **Flexibility**: Guild-specific configuration customization
- **Validation**: Ensures configuration integrity
- **Monitoring**: Tracks cache performance metrics

## Dependencies
- Database model for configuration storage
- API client for configuration retrieval
- Cache management system
- Configuration validation utilities
- Admin interface for configuration management

## Processing Pipeline Flow
1. **Request Configuration**: Service requests guild config
2. **Cache Check**: Check if valid cached config exists
3. **API Fetch**: Fetch from database if cache miss
4. **Validation**: Validate and sanitize configuration
5. **Cache Store**: Store validated config in cache
6. **Return Config**: Return configuration to caller

## Testing Strategy
```python
@pytest.mark.asyncio
async def test_config_cache_hit(bytes_service):
    """Test configuration cache hit."""
    # Prime cache
    config1 = await bytes_service.get_config("guild1")
    
    # Second call should hit cache
    config2 = await bytes_service.get_config("guild1")
    
    assert config1 == config2
    # Verify only one API call was made
    bytes_service.api.get_bytes_config.assert_called_once()
```

This configuration and caching system ensures efficient, reliable, and configurable bytes system operation through intelligent caching and robust configuration management.