# Session 1: Project Foundation & Configuration

## Objective
Create the foundational configuration system, logging setup, and shared utilities that both the bot and web services will use.

## Prerequisites
- Completed Session 0 (project structure exists)
- Docker environment running (PostgreSQL and Redis)
- Understanding of the dual-service architecture

## Task 1: Shared Configuration System

Create a configuration system using Pydantic Settings that supports both development and production modes.

### shared/config.py

Create a base configuration class that both bot and web can extend:

```python
from pydantic_settings import BaseSettings
from pydantic import Field, validator
from typing import Optional, List
import os

class BaseConfig(BaseSettings):
    """Base configuration shared between bot and web services."""
    
    # Environment
    dev_mode: bool = Field(default=False, alias="DEV_MODE")
    log_level: str = Field(default="INFO")
    
    # Database
    database_url: str = Field(..., alias="DATABASE_URL")
    database_pool_size: int = Field(default=20)
    database_max_overflow: int = Field(default=0)
    
    # Redis
    redis_url: str = Field(..., alias="REDIS_URL")
    redis_pool_size: int = Field(default=10)
    
    # Discord
    discord_client_id: Optional[str] = Field(None, alias="DISCORD_CLIENT_ID")
    discord_client_secret: Optional[str] = Field(None, alias="DISCORD_CLIENT_SECRET")
    
    class Config:
        env_file = ".env"
        case_sensitive = False
        
    @validator("database_url")
    def validate_database_url(cls, v):
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("Database URL must use asyncpg driver")
        return v
```

### bot/config.py

Bot-specific configuration:

```python
from shared.config import BaseConfig
from pydantic import Field, validator
from typing import Optional

class BotConfig(BaseConfig):
    """Discord bot configuration."""
    
    # Bot credentials
    bot_token: str = Field(..., alias="BOT_TOKEN")
    bot_application_id: str = Field(..., alias="BOT_APPLICATION_ID")
    
    # API configuration
    api_base_url: str = Field(default="http://localhost:8000/api")
    api_key: str = Field(..., alias="API_KEY_FOR_BOT")
    
    # Bot settings
    command_prefix: str = Field(default="/")
    default_guild_ids: Optional[List[int]] = Field(None)
    
    # Caching
    cache_ttl: int = Field(default=300)  # 5 minutes
    
    @validator("bot_token")
    def validate_token(cls, v):
        if not v or len(v) < 50:
            raise ValueError("Invalid bot token")
        return v
```

### web/config.py

Web-specific configuration:

```python
from shared.config import BaseConfig
from pydantic import Field, validator, SecretStr
from typing import List, Set

class WebConfig(BaseConfig):
    """Web application configuration."""
    
    # Web app settings
    base_url: str = Field(default="http://localhost:8000", alias="BASE_URL")
    session_secret: SecretStr = Field(..., alias="SESSION_SECRET")
    session_lifetime: int = Field(default=86400)  # 24 hours
    
    # Admin settings
    admin_discord_ids: Set[str] = Field(set(), alias="ADMIN_DISCORD_IDS")
    
    # API settings
    api_rate_limit: int = Field(default=100)  # requests per minute
    api_key_length: int = Field(default=32)
    
    # Static files
    serve_static: bool = Field(default=True)
    static_url: str = Field(default="/static")
    
    @validator("admin_discord_ids", pre=True)
    def parse_admin_ids(cls, v):
        if isinstance(v, str):
            return set(id.strip() for id in v.split(",") if id.strip())
        return v
    
    @validator("session_secret")
    def validate_session_secret(cls, v):
        if len(v.get_secret_value()) < 32:
            raise ValueError("Session secret must be at least 32 characters")
        return v
```

## Task 2: Logging Configuration

Create a unified logging system that both services can use.

### shared/logging.py

```python
import logging
import sys
from typing import Optional
import structlog
from structlog.processors import JSONRenderer, TimeStamper, add_log_level

def setup_logging(
    service_name: str, 
    log_level: str = "INFO",
    dev_mode: bool = False
) -> structlog.BoundLogger:
    """
    Configure structured logging for the application.
    
    Args:
        service_name: Name of the service (bot/web)
        log_level: Logging level
        dev_mode: Whether to use console renderer for development
    
    Returns:
        Configured logger instance
    """
    
    # Configure stdlib logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper())
    )
    
    # Structlog processors
    processors = [
        TimeStamper(fmt="iso"),
        add_log_level,
        structlog.processors.add_logger_name,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    
    # Use console renderer in dev mode for readability
    if dev_mode:
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(JSONRenderer())
    
    # Configure structlog
    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper())
        ),
        cache_logger_on_first_use=True,
    )
    
    # Return logger for the service
    return structlog.get_logger(service_name)
```

## Task 3: Shared Types and Constants

### shared/types.py

Define common types used across the application:

```python
from enum import Enum
from typing import TypedDict, Optional, Literal
from datetime import datetime

class StreakMultiplier(Enum):
    """Bytes streak multipliers with programming type themes."""
    NONE = (1, 1, "No Streak")
    CHAR = (8, 2, "CHAR (8 days)")
    SHORT = (16, 4, "SHORT (16 days)")
    INT = (32, 16, "INT (32 days)")
    LONG = (64, 256, "LONG (64 days)")
    
    def __init__(self, days: int, multiplier: int, display: str):
        self.days = days
        self.multiplier = multiplier
        self.display = display
    
    @classmethod
    def from_streak(cls, streak: int) -> "StreakMultiplier":
        """Get multiplier for a given streak count."""
        for multiplier in reversed(list(cls)):
            if streak >= multiplier.days:
                return multiplier
        return cls.NONE

class ModerationAction(str, Enum):
    """Available moderation actions."""
    BAN = "ban"
    KICK = "kick"
    TIMEOUT = "timeout"
    WARN = "warn"
    DELETE = "delete"

class AutoModRuleType(str, Enum):
    """Types of auto-moderation rules."""
    USERNAME_REGEX = "username_regex"
    MESSAGE_RATE = "message_rate"
    FILE_EXTENSION = "file_extension"
    DUPLICATE_MESSAGE = "duplicate_message"

class UserContext(TypedDict):
    """User context passed between services."""
    user_id: str
    username: str
    guild_id: Optional[str]
    is_admin: bool
```

### shared/constants.py

```python
"""Shared constants across the application."""

# Bytes system defaults
DEFAULT_STARTING_BALANCE = 100
DEFAULT_DAILY_AMOUNT = 10
DEFAULT_MAX_TRANSFER = 1000
DEFAULT_COOLDOWN_HOURS = 24

# Squad system defaults
DEFAULT_SQUAD_SWITCH_COST = 50
MIN_BYTES_FOR_SQUAD = 10

# Cache TTLs (seconds)
USER_CACHE_TTL = 300  # 5 minutes
CONFIG_CACHE_TTL = 300
LEADERBOARD_CACHE_TTL = 60  # 1 minute

# Pagination
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# Discord limits
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_LIMIT = 1024
EMBED_FIELDS_LIMIT = 25

# Colors (as integers for Discord)
COLOR_SUCCESS = 0x22C55E  # Green
COLOR_ERROR = 0xEF4444    # Red
COLOR_INFO = 0x3B82F6     # Blue
COLOR_WARNING = 0xF59E0B  # Amber

# Rate limits
API_RATE_LIMIT_REQUESTS = 100
API_RATE_LIMIT_WINDOW = 60  # seconds
```

## Task 4: Error Handling

### shared/exceptions.py

Create custom exceptions for better error handling:

```python
from typing import Optional, Dict, Any

class SmarterDevException(Exception):
    """Base exception for all custom exceptions."""
    
    def __init__(
        self, 
        message: str, 
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        super().__init__(message)
        self.code = code or self.__class__.__name__
        self.details = details or {}

class ConfigurationError(SmarterDevException):
    """Raised when configuration is invalid."""
    pass

class DatabaseError(SmarterDevException):
    """Raised when database operations fail."""
    pass

class DiscordAPIError(SmarterDevException):
    """Raised when Discord API calls fail."""
    pass

class AuthenticationError(SmarterDevException):
    """Raised when authentication fails."""
    pass

class ValidationError(SmarterDevException):
    """Raised when input validation fails."""
    pass

class BusinessRuleError(SmarterDevException):
    """Raised when business rules are violated."""
    pass

class RateLimitError(SmarterDevException):
    """Raised when rate limits are exceeded."""
    pass

# Error response helpers
def error_to_dict(error: SmarterDevException) -> Dict[str, Any]:
    """Convert exception to API error response."""
    return {
        "error": {
            "code": error.code,
            "message": str(error),
            "details": error.details
        }
    }
```

## Task 5: Utility Functions

### shared/utils.py

Common utility functions:

```python
import secrets
import string
from datetime import datetime, timezone, date
from typing import Optional, TypeVar, Callable, Any
import functools
import asyncio

T = TypeVar("T")

def generate_token(length: int = 32) -> str:
    """Generate a secure random token."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def utcnow() -> datetime:
    """Get current UTC datetime."""
    return datetime.now(timezone.utc)

def utctoday() -> date:
    """Get current UTC date."""
    return utcnow().date()

def snowflake_to_datetime(snowflake: str) -> datetime:
    """Convert Discord snowflake to datetime."""
    timestamp = ((int(snowflake) >> 22) + 1420070400000) / 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)

def safe_int(value: Any, default: int = 0) -> int:
    """Safely convert value to int."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

def chunk_list(lst: list, chunk_size: int) -> list[list]:
    """Split list into chunks."""
    return [lst[i:i + chunk_size] for i in range(0, len(lst), chunk_size)]

def ttl_cache(ttl: int):
    """Simple TTL cache decorator."""
    def decorator(func: Callable) -> Callable:
        cache = {}
        cache_time = {}
        
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            key = str(args) + str(kwargs)
            now = utcnow().timestamp()
            
            if key in cache and now - cache_time[key] < ttl:
                return cache[key]
            
            result = await func(*args, **kwargs)
            cache[key] = result
            cache_time[key] = now
            
            return result
        
        wrapper.clear_cache = lambda: (cache.clear(), cache_time.clear())
        return wrapper
    
    return decorator

class AsyncContextManager:
    """Base class for async context managers."""
    
    async def __aenter__(self):
        await self.startup()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.shutdown()
    
    async def startup(self):
        """Override to implement startup logic."""
        pass
    
    async def shutdown(self):
        """Override to implement shutdown logic."""
        pass
```

## Task 6: Create Tests

### tests/test_config.py

```python
import pytest
from shared.config import BaseConfig
from bot.config import BotConfig
from web.config import WebConfig

def test_base_config_loads_env(monkeypatch):
    """Test base configuration loads from environment."""
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    
    config = BaseConfig()
    assert config.dev_mode is True
    assert "asyncpg" in config.database_url

def test_bot_config_validates_token(monkeypatch):
    """Test bot config validates token."""
    monkeypatch.setenv("BOT_TOKEN", "short")
    monkeypatch.setenv("BOT_APPLICATION_ID", "123456")
    monkeypatch.setenv("API_KEY_FOR_BOT", "test-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    
    with pytest.raises(ValueError, match="Invalid bot token"):
        BotConfig()

def test_web_config_parses_admin_ids(monkeypatch):
    """Test web config parses admin Discord IDs."""
    monkeypatch.setenv("SESSION_SECRET", "a" * 32)
    monkeypatch.setenv("ADMIN_DISCORD_IDS", "123,456,789")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    
    config = WebConfig()
    assert config.admin_discord_ids == {"123", "456", "789"}
```

### tests/test_utils.py

```python
import pytest
from datetime import datetime, timezone
from shared.utils import snowflake_to_datetime, chunk_list, ttl_cache
import asyncio

def test_snowflake_conversion():
    """Test Discord snowflake to datetime conversion."""
    # Known Discord snowflake
    snowflake = "175928847299117063"
    dt = snowflake_to_datetime(snowflake)
    
    assert dt.year == 2016
    assert dt.month == 4
    assert dt.tzinfo == timezone.utc

def test_chunk_list():
    """Test list chunking."""
    items = list(range(10))
    chunks = chunk_list(items, 3)
    
    assert len(chunks) == 4
    assert chunks[0] == [0, 1, 2]
    assert chunks[-1] == [9]

@pytest.mark.asyncio
async def test_ttl_cache():
    """Test TTL cache decorator."""
    call_count = 0
    
    @ttl_cache(ttl=1)
    async def cached_func(x):
        nonlocal call_count
        call_count += 1
        return x * 2
    
    # First call
    result1 = await cached_func(5)
    assert result1 == 10
    assert call_count == 1
    
    # Second call (cached)
    result2 = await cached_func(5)
    assert result2 == 10
    assert call_count == 1
    
    # Wait for TTL
    await asyncio.sleep(1.1)
    
    # Third call (cache expired)
    result3 = await cached_func(5)
    assert result3 == 10
    assert call_count == 2
```

## Deliverables

1. **Configuration System**
   - Base configuration class
   - Bot-specific configuration
   - Web-specific configuration
   - Environment variable validation

2. **Logging Setup**
   - Structured logging configuration
   - Development vs production modes
   - Service-specific loggers

3. **Shared Types and Constants**
   - Common enums and types
   - Application constants
   - Discord-specific limits

4. **Error Handling**
   - Custom exception hierarchy
   - Error response helpers
   - Consistent error codes

5. **Utility Functions**
   - Common helpers
   - TTL cache decorator
   - Discord utilities

6. **Test Coverage**
   - Configuration tests
   - Utility function tests
   - 100% coverage goal

## Important Notes

1. All configuration uses environment variables
2. Pydantic validates all settings at startup
3. Logging uses structlog for structured output
4. All timestamps are UTC
5. Never hardcode configuration values
6. Test-driven development approach

This foundation will be used by all subsequent sessions. Ensure the configuration is flexible and the utilities are well-tested.