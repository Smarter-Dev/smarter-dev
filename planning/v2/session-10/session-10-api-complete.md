# Session 10: API Implementation

## Objective
Complete the FastAPI implementation with all remaining endpoints, proper schemas, comprehensive error handling, and API documentation. Focus on consistency, validation, and performance.

## Prerequisites
- Completed Sessions 7-9 (bytes, squads, automod endpoints exist)
- Understanding of FastAPI and Pydantic
- Database models configured

## Task 1: API Schemas

### web/api/schemas.py

Complete all API schemas with Pydantic:

```python
from pydantic import BaseModel, Field, validator, ConfigDict
from typing import Optional, List, Dict, Any, Union
from datetime import datetime, date
from uuid import UUID
from enum import Enum

from shared.types import ModerationAction, AutoModRuleType

# Base schemas with common fields
class TimestampSchema(BaseModel):
    created_at: datetime
    updated_at: datetime

class DiscordEntitySchema(BaseModel):
    guild_id: str = Field(..., pattern=r"^\d{17,19}$")
    
class UserIdentifierSchema(BaseModel):
    user_id: str = Field(..., pattern=r"^\d{17,19}$")

# Guild schemas
class GuildStatsResponse(BaseModel):
    guild_id: str
    total_users: int
    total_bytes_circulating: int
    active_squads: int
    transactions_today: int
    moderation_cases_week: int

class GuildConfigResponse(BaseModel):
    guild_id: str
    bytes_config: Optional[Dict[str, Any]]
    squad_config: Optional[Dict[str, Any]]
    automod_enabled: bool
    created_at: datetime

# Bytes schemas (extending from Session 7)
class BytesConfigRequest(BaseModel):
    starting_balance: int = Field(100, ge=0, le=10000)
    daily_amount: int = Field(10, ge=1, le=1000)
    max_transfer: int = Field(1000, ge=1, le=100000)
    cooldown_hours: int = Field(24, ge=0, le=168)
    role_rewards: Dict[str, int] = Field(default_factory=dict)
    
    @validator("role_rewards")
    def validate_role_rewards(cls, v):
        # Ensure all values are positive
        for role_id, threshold in v.items():
            if threshold < 0:
                raise ValueError(f"Role reward threshold must be positive")
        return v

class BytesHistoryRequest(BaseModel):
    user_id: Optional[str] = None
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)
    start_date: Optional[date] = None
    end_date: Optional[date] = None

class BytesTransactionResponse(BaseModel):
    id: UUID
    guild_id: str
    giver_id: str
    giver_username: str
    receiver_id: str
    receiver_username: str
    amount: int
    reason: Optional[str]
    created_at: datetime

# Squad schemas (extending from Session 8)
class SquadCreateRequest(BaseModel):
    role_id: str = Field(..., pattern=r"^\d{17,19}$")
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    switch_cost: int = Field(50, ge=0, le=10000)

class SquadUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    switch_cost: Optional[int] = Field(None, ge=0, le=10000)
    is_active: Optional[bool] = None

class SquadResponse(BaseModel):
    id: UUID
    guild_id: str
    role_id: str
    name: str
    description: Optional[str]
    switch_cost: int
    is_active: bool
    members: List["SquadMemberResponse"]
    created_at: datetime
    updated_at: datetime

class SquadMemberResponse(BaseModel):
    user_id: str
    joined_at: datetime

class SquadJoinRequest(BaseModel):
    user_id: str = Field(..., pattern=r"^\d{17,19}$")

class SquadLeaveRequest(BaseModel):
    user_id: str = Field(..., pattern=r"^\d{17,19}$")

# AutoMod schemas (extending from Session 9)
class AutoModRuleCreateRequest(BaseModel):
    rule_type: AutoModRuleType
    config: Dict[str, Any]
    action: ModerationAction
    priority: int = Field(0, ge=0, le=999)
    
    @validator("config")
    def validate_config(cls, v, values):
        rule_type = values.get("rule_type")
        
        if rule_type == AutoModRuleType.USERNAME_REGEX:
            if "pattern" not in v:
                raise ValueError("Username regex requires 'pattern'")
        elif rule_type == AutoModRuleType.MESSAGE_RATE:
            required = ["max_messages", "timeframe_seconds"]
            if not all(k in v for k in required):
                raise ValueError(f"Message rate requires: {', '.join(required)}")
        elif rule_type == AutoModRuleType.FILE_EXTENSION:
            if "blocked_extensions" not in v or not v["blocked_extensions"]:
                raise ValueError("File extension rule requires 'blocked_extensions'")
        
        return v

class AutoModRuleUpdateRequest(BaseModel):
    config: Optional[Dict[str, Any]] = None
    action: Optional[ModerationAction] = None
    priority: Optional[int] = Field(None, ge=0, le=999)
    is_active: Optional[bool] = None

class AutoModRuleResponse(BaseModel):
    id: UUID
    guild_id: str
    rule_type: AutoModRuleType
    config: Dict[str, Any]
    action: ModerationAction
    priority: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

class ModCaseResponse(BaseModel):
    id: UUID
    guild_id: str
    user_id: str
    user_tag: str
    moderator_id: str
    moderator_tag: str
    action: ModerationAction
    reason: str
    expires_at: Optional[datetime]
    resolved: bool
    created_at: datetime

# System/Admin schemas
class APIKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)

class APIKeyResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str]
    key: Optional[str] = None  # Only returned on creation
    last_used: Optional[datetime]
    is_active: bool
    created_at: datetime

class SystemHealthResponse(BaseModel):
    status: str
    timestamp: datetime
    services: Dict[str, bool]
    version: str
    uptime_seconds: float

class ErrorResponse(BaseModel):
    error: Dict[str, Any]
    request_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)

# Pagination schemas
class PaginationParams(BaseModel):
    limit: int = Field(50, ge=1, le=100)
    offset: int = Field(0, ge=0)
    
class PaginatedResponse(BaseModel):
    items: List[Any]
    total: int
    limit: int
    offset: int
    has_more: bool

# Update forward refs
SquadResponse.model_rebuild()
```

## Task 2: Error Handling and Middleware

### web/api/exceptions.py

API-specific exceptions and handlers:

```python
from fastapi import Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError, HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException
from typing import Union
import structlog

from shared.exceptions import (
    SmarterDevException,
    BusinessRuleError,
    ValidationError,
    RateLimitError
)

logger = structlog.get_logger()

async def api_exception_handler(
    request: Request,
    exc: Union[Exception, HTTPException]
) -> JSONResponse:
    """Handle all API exceptions consistently."""
    request_id = getattr(request.state, "request_id", "unknown")
    
    # Handle custom exceptions
    if isinstance(exc, SmarterDevException):
        status_code = 400
        if isinstance(exc, RateLimitError):
            status_code = 429
        elif isinstance(exc, ValidationError):
            status_code = 422
            
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "details": exc.details
                },
                "request_id": request_id
            }
        )
    
    # Handle FastAPI/Starlette HTTP exceptions
    elif isinstance(exc, (HTTPException, StarletteHTTPException)):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"HTTP_{exc.status_code}",
                    "message": exc.detail
                },
                "request_id": request_id
            }
        )
    
    # Handle validation errors
    elif isinstance(exc, RequestValidationError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request data",
                    "details": exc.errors()
                },
                "request_id": request_id
            }
        )
    
    # Handle unexpected errors
    else:
        logger.error(
            "Unexpected API error",
            request_id=request_id,
            error=str(exc),
            exc_info=True
        )
        
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected error occurred"
                },
                "request_id": request_id
            }
        )

def register_exception_handlers(app):
    """Register all exception handlers for the API."""
    app.add_exception_handler(SmarterDevException, api_exception_handler)
    app.add_exception_handler(HTTPException, api_exception_handler)
    app.add_exception_handler(RequestValidationError, api_exception_handler)
    app.add_exception_handler(Exception, api_exception_handler)
```

### web/api/middleware.py

API-specific middleware:

```python
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
import time
import uuid
import structlog

logger = structlog.get_logger()

class APILoggingMiddleware(BaseHTTPMiddleware):
    """Log API requests with timing."""
    
    async def dispatch(self, request: Request, call_next):
        # Generate request ID if not present
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        request.state.request_id = request_id
        
        # Start timing
        start_time = time.time()
        
        # Log request
        logger.info(
            "API request started",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client=request.client.host if request.client else None
        )
        
        # Process request
        response = await call_next(request)
        
        # Calculate duration
        duration = time.time() - start_time
        
        # Add headers
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{duration:.3f}"
        
        # Log response
        logger.info(
            "API request completed",
            request_id=request_id,
            status_code=response.status_code,
            duration=round(duration, 3)
        )
        
        return response

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple rate limiting middleware."""
    
    def __init__(self, app, requests_per_minute: int = 100):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.requests = {}  # Simple in-memory storage
    
    async def dispatch(self, request: Request, call_next):
        # Get client identifier (API key or IP)
        client_id = None
        
        # Check for API key first
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            client_id = f"key:{auth_header[7:][:8]}"  # First 8 chars of key
        else:
            # Fall back to IP
            client_id = f"ip:{request.client.host}" if request.client else "unknown"
        
        # Check rate limit
        now = time.time()
        minute_ago = now - 60
        
        # Clean old requests
        if client_id in self.requests:
            self.requests[client_id] = [
                t for t in self.requests[client_id]
                if t > minute_ago
            ]
        else:
            self.requests[client_id] = []
        
        # Check limit
        if len(self.requests[client_id]) >= self.requests_per_minute:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": f"Rate limit exceeded: {self.requests_per_minute} requests per minute"
                    }
                },
                headers={
                    "X-RateLimit-Limit": str(self.requests_per_minute),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(minute_ago + 60))
                }
            )
        
        # Record request
        self.requests[client_id].append(now)
        
        # Add rate limit headers
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.requests_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(
            self.requests_per_minute - len(self.requests[client_id])
        )
        
        return response
```

## Task 3: Complete API Routers

### web/api/routers/guilds.py

Guild management endpoints:

```python
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from web.api.dependencies import CurrentAPIKey, DatabaseSession, get_discord_client
from web.api.schemas import (
    GuildStatsResponse,
    GuildConfigResponse,
    BytesConfigRequest
)
from web.models.bytes import BytesBalance, BytesTransaction, BytesConfig
from web.models.squads import Squad
from web.models.moderation import ModerationCase
from shared.utils import utcnow
from datetime import timedelta
import structlog

logger = structlog.get_logger()
router = APIRouter(prefix="/guilds", tags=["guilds"])

@router.get("/{guild_id}/stats", response_model=GuildStatsResponse)
async def get_guild_stats(
    guild_id: str,
    api_key: CurrentAPIKey,
    db: DatabaseSession,
    discord = Depends(get_discord_client)
) -> GuildStatsResponse:
    """Get comprehensive guild statistics."""
    # Total users with bytes
    users_result = await db.execute(
        select(func.count(BytesBalance.user_id))
        .where(BytesBalance.guild_id == guild_id)
    )
    total_users = users_result.scalar() or 0
    
    # Total bytes in circulation
    bytes_result = await db.execute(
        select(func.sum(BytesBalance.balance))
        .where(BytesBalance.guild_id == guild_id)
    )
    total_bytes = bytes_result.scalar() or 0
    
    # Active squads
    squads_result = await db.execute(
        select(func.count(Squad.id))
        .where(Squad.guild_id == guild_id, Squad.is_active == True)
    )
    active_squads = squads_result.scalar() or 0
    
    # Transactions today
    today_start = utcnow().replace(hour=0, minute=0, second=0)
    tx_result = await db.execute(
        select(func.count(BytesTransaction.id))
        .where(
            BytesTransaction.guild_id == guild_id,
            BytesTransaction.created_at >= today_start
        )
    )
    transactions_today = tx_result.scalar() or 0
    
    # Moderation cases this week
    week_ago = utcnow() - timedelta(days=7)
    cases_result = await db.execute(
        select(func.count(ModerationCase.id))
        .where(
            ModerationCase.guild_id == guild_id,
            ModerationCase.created_at >= week_ago
        )
    )
    moderation_cases_week = cases_result.scalar() or 0
    
    return GuildStatsResponse(
        guild_id=guild_id,
        total_users=total_users,
        total_bytes_circulating=total_bytes,
        active_squads=active_squads,
        transactions_today=transactions_today,
        moderation_cases_week=moderation_cases_week
    )

@router.get("/{guild_id}/config", response_model=GuildConfigResponse)
async def get_guild_config(
    guild_id: str,
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> GuildConfigResponse:
    """Get all guild configuration."""
    # Get bytes config
    bytes_config = await db.execute(
        select(BytesConfig).where(BytesConfig.guild_id == guild_id)
    )
    bytes_data = bytes_config.scalar_one_or_none()
    
    # Get squad config
    squads = await db.execute(
        select(Squad)
        .where(Squad.guild_id == guild_id, Squad.is_active == True)
    )
    squad_count = len(squads.scalars().all())
    
    # Check if automod is enabled
    from web.models.moderation import AutoModRule
    automod = await db.execute(
        select(func.count(AutoModRule.id))
        .where(AutoModRule.guild_id == guild_id, AutoModRule.is_active == True)
    )
    automod_enabled = (automod.scalar() or 0) > 0
    
    return GuildConfigResponse(
        guild_id=guild_id,
        bytes_config=bytes_data.dict() if bytes_data else None,
        squad_config={"active_squads": squad_count},
        automod_enabled=automod_enabled,
        created_at=bytes_data.created_at if bytes_data else utcnow()
    )

@router.put("/{guild_id}/config/bytes")
async def update_bytes_config(
    guild_id: str,
    config: BytesConfigRequest,
    api_key: CurrentAPIKey,
    db: DatabaseSession
):
    """Update guild bytes configuration."""
    # Get or create config
    result = await db.execute(
        select(BytesConfig).where(BytesConfig.guild_id == guild_id)
    )
    bytes_config = result.scalar_one_or_none()
    
    if not bytes_config:
        bytes_config = BytesConfig(guild_id=guild_id, **config.dict())
        db.add(bytes_config)
    else:
        for key, value in config.dict().items():
            setattr(bytes_config, key, value)
    
    await db.commit()
    
    # Notify bot via Redis
    redis = request.app.state.redis
    await redis.publish(f"config_update:{guild_id}", "bytes")
    
    logger.info("Bytes config updated", guild_id=guild_id)
    
    return {"status": "updated"}
```

### web/api/routers/system.py

System and admin endpoints:

```python
from fastapi import APIRouter, Depends, HTTPException, Security
from typing import List
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import psutil
import platform

from web.api.dependencies import CurrentAPIKey, DatabaseSession
from web.api.schemas import (
    APIKeyCreateRequest,
    APIKeyResponse,
    SystemHealthResponse
)
from web.models.admin import APIKey
from shared.utils import generate_token, utcnow
import structlog

logger = structlog.get_logger()
router = APIRouter(prefix="/system", tags=["system"])

# Special admin verification
async def verify_admin_key(api_key: CurrentAPIKey = Depends()) -> APIKey:
    """Verify API key has admin privileges."""
    if not api_key.name.startswith("admin_"):
        raise HTTPException(403, "Admin privileges required")
    return api_key

@router.get("/health", response_model=SystemHealthResponse)
async def health_check(
    db: DatabaseSession,
    redis = Depends(lambda r: r.app.state.redis)
) -> SystemHealthResponse:
    """System health check."""
    start_time = getattr(health_check, "_start_time", utcnow())
    health_check._start_time = start_time
    
    # Check services
    services = {}
    
    # Database
    try:
        await db.execute("SELECT 1")
        services["database"] = True
    except Exception:
        services["database"] = False
    
    # Redis
    try:
        await redis.ping()
        services["redis"] = True
    except Exception:
        services["redis"] = False
    
    # Discord API (would check bot status)
    services["discord_bot"] = True  # Placeholder
    
    # Overall status
    all_healthy = all(services.values())
    
    return SystemHealthResponse(
        status="healthy" if all_healthy else "degraded",
        timestamp=utcnow(),
        services=services,
        version="2.0.0",
        uptime_seconds=(utcnow() - start_time).total_seconds()
    )

@router.post("/api-keys", response_model=APIKeyResponse)
async def create_api_key(
    request: APIKeyCreateRequest,
    admin_key: APIKey = Depends(verify_admin_key),
    db: DatabaseSession
) -> APIKeyResponse:
    """Create new API key (admin only)."""
    # Generate key
    key = generate_token(32)
    key_hash = APIKey.hash_key(key)
    
    # Create record
    api_key = APIKey(
        key_hash=key_hash,
        name=request.name,
        description=request.description
    )
    
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)
    
    logger.info(
        "API key created",
        key_id=str(api_key.id),
        name=api_key.name,
        created_by=admin_key.name
    )
    
    # Return with actual key (only time it's shown)
    return APIKeyResponse(
        id=api_key.id,
        name=api_key.name,
        description=api_key.description,
        key=key,  # Only returned on creation
        last_used=None,
        is_active=True,
        created_at=api_key.created_at
    )

@router.get("/api-keys", response_model=List[APIKeyResponse])
async def list_api_keys(
    admin_key: APIKey = Depends(verify_admin_key),
    db: DatabaseSession
) -> List[APIKeyResponse]:
    """List all API keys (admin only)."""
    result = await db.execute(
        select(APIKey).order_by(APIKey.created_at.desc())
    )
    keys = result.scalars().all()
    
    return [
        APIKeyResponse(
            id=key.id,
            name=key.name,
            description=key.description,
            key=None,  # Never show actual key
            last_used=key.last_used,
            is_active=key.is_active,
            created_at=key.created_at
        )
        for key in keys
    ]

@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: UUID,
    admin_key: APIKey = Depends(verify_admin_key),
    db: DatabaseSession
):
    """Revoke API key (admin only)."""
    result = await db.execute(
        select(APIKey).where(APIKey.id == key_id)
    )
    api_key = result.scalar_one_or_none()
    
    if not api_key:
        raise HTTPException(404, "API key not found")
    
    api_key.is_active = False
    await db.commit()
    
    logger.info(
        "API key revoked",
        key_id=str(key_id),
        revoked_by=admin_key.name
    )
    
    return {"status": "revoked"}

@router.get("/metrics")
async def get_metrics(
    admin_key: APIKey = Depends(verify_admin_key)
):
    """Get system metrics (admin only)."""
    # CPU usage
    cpu_percent = psutil.cpu_percent(interval=1)
    
    # Memory usage
    memory = psutil.virtual_memory()
    
    # Disk usage
    disk = psutil.disk_usage('/')
    
    # Process info
    process = psutil.Process()
    
    return {
        "system": {
            "platform": platform.system(),
            "python_version": platform.python_version(),
            "cpu_count": psutil.cpu_count(),
            "cpu_percent": cpu_percent,
            "memory": {
                "total": memory.total,
                "available": memory.available,
                "percent": memory.percent
            },
            "disk": {
                "total": disk.total,
                "used": disk.used,
                "percent": disk.percent
            }
        },
        "process": {
            "memory_mb": process.memory_info().rss / 1024 / 1024,
            "cpu_percent": process.cpu_percent(),
            "threads": process.num_threads(),
            "open_files": len(process.open_files()),
            "connections": len(process.connections())
        }
    }
```

### web/api/routers/search.py

Search and query endpoints:

```python
from fastapi import APIRouter, Depends, Query
from typing import List, Optional
from sqlalchemy import select, or_, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date

from web.api.dependencies import CurrentAPIKey, DatabaseSession
from web.api.schemas import (
    BytesTransactionResponse,
    PaginatedResponse,
    PaginationParams
)
from web.models.bytes import BytesTransaction, BytesBalance
from web.models.moderation import ModerationCase
import structlog

logger = structlog.get_logger()
router = APIRouter(prefix="/search", tags=["search"])

@router.get("/transactions", response_model=PaginatedResponse)
async def search_transactions(
    guild_id: Optional[str] = None,
    user_id: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    min_amount: Optional[int] = None,
    reason_contains: Optional[str] = None,
    pagination: PaginationParams = Depends(),
    api_key: CurrentAPIKey = Depends(),
    db: DatabaseSession = Depends()
) -> PaginatedResponse:
    """Search bytes transactions with filters."""
    query = select(BytesTransaction)
    
    # Build filters
    filters = []
    
    if guild_id:
        filters.append(BytesTransaction.guild_id == guild_id)
    
    if user_id:
        filters.append(
            or_(
                BytesTransaction.giver_id == user_id,
                BytesTransaction.receiver_id == user_id
            )
        )
    
    if start_date:
        filters.append(BytesTransaction.created_at >= start_date)
    
    if end_date:
        filters.append(BytesTransaction.created_at <= end_date)
    
    if min_amount:
        filters.append(BytesTransaction.amount >= min_amount)
    
    if reason_contains:
        filters.append(
            BytesTransaction.reason.ilike(f"%{reason_contains}%")
        )
    
    # Apply filters
    if filters:
        query = query.where(and_(*filters))
    
    # Get total count
    count_query = select(func.count()).select_from(
        query.subquery()
    )
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    
    # Apply pagination
    query = query.order_by(BytesTransaction.created_at.desc())
    query = query.offset(pagination.offset).limit(pagination.limit)
    
    # Execute query
    result = await db.execute(query)
    transactions = result.scalars().all()
    
    # Format response
    items = [
        BytesTransactionResponse(
            id=tx.id,
            guild_id=tx.guild_id,
            giver_id=tx.giver_id,
            giver_username=tx.giver_username,
            receiver_id=tx.receiver_id,
            receiver_username=tx.receiver_username,
            amount=tx.amount,
            reason=tx.reason,
            created_at=tx.created_at
        )
        for tx in transactions
    ]
    
    return PaginatedResponse(
        items=items,
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        has_more=(pagination.offset + len(items)) < total
    )

@router.get("/users/top-givers")
async def get_top_givers(
    guild_id: str,
    limit: int = Query(10, ge=1, le=50),
    days: int = Query(30, ge=1, le=365),
    api_key: CurrentAPIKey = Depends(),
    db: DatabaseSession = Depends()
):
    """Get users who have given the most bytes."""
    from datetime import timedelta
    cutoff = utcnow() - timedelta(days=days)
    
    # Query for top givers
    query = (
        select(
            BytesTransaction.giver_id,
            BytesTransaction.giver_username,
            func.sum(BytesTransaction.amount).label("total_given"),
            func.count(BytesTransaction.id).label("transaction_count")
        )
        .where(
            BytesTransaction.guild_id == guild_id,
            BytesTransaction.created_at >= cutoff,
            BytesTransaction.giver_id != "system"  # Exclude system
        )
        .group_by(BytesTransaction.giver_id, BytesTransaction.giver_username)
        .order_by(func.sum(BytesTransaction.amount).desc())
        .limit(limit)
    )
    
    result = await db.execute(query)
    
    return {
        "guild_id": guild_id,
        "days": days,
        "top_givers": [
            {
                "user_id": row.giver_id,
                "username": row.giver_username,
                "total_given": row.total_given,
                "transaction_count": row.transaction_count
            }
            for row in result
        ]
    }

@router.get("/moderation/recent-cases")
async def get_recent_cases(
    guild_id: Optional[str] = None,
    action: Optional[ModerationAction] = None,
    moderator_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    api_key: CurrentAPIKey = Depends(),
    db: DatabaseSession = Depends()
):
    """Get recent moderation cases."""
    query = select(ModerationCase)
    
    # Filters
    filters = []
    if guild_id:
        filters.append(ModerationCase.guild_id == guild_id)
    if action:
        filters.append(ModerationCase.action == action)
    if moderator_id:
        filters.append(ModerationCase.moderator_id == moderator_id)
    
    if filters:
        query = query.where(and_(*filters))
    
    query = query.order_by(ModerationCase.created_at.desc()).limit(limit)
    
    result = await db.execute(query)
    cases = result.scalars().all()
    
    return {
        "cases": [
            {
                "id": str(case.id),
                "guild_id": case.guild_id,
                "user_id": case.user_id,
                "user_tag": case.user_tag,
                "moderator_id": case.moderator_id,
                "moderator_tag": case.moderator_tag,
                "action": case.action.value,
                "reason": case.reason,
                "created_at": case.created_at.isoformat(),
                "resolved": case.resolved
            }
            for case in cases
        ]
    }
```

## Task 4: API Documentation

### web/api/docs.py

Enhanced API documentation:

```python
from fastapi import FastAPI

def customize_openapi(app: FastAPI):
    """Customize OpenAPI schema."""
    if app.openapi_schema:
        return app.openapi_schema
    
    from fastapi.openapi.utils import get_openapi
    
    openapi_schema = get_openapi(
        title="Smarter Dev API",
        version="2.0.0",
        description="""
# Smarter Dev API Documentation

The Smarter Dev API provides programmatic access to all platform features.

## Authentication

All endpoints require authentication using a Bearer token:

```
Authorization: Bearer YOUR_API_KEY
```

## Rate Limiting

API requests are rate limited to 100 requests per minute per API key.

Rate limit headers are included in all responses:
- `X-RateLimit-Limit`: Maximum requests per minute
- `X-RateLimit-Remaining`: Requests remaining in current window
- `X-RateLimit-Reset`: Unix timestamp when the window resets

## Error Responses

All errors follow a consistent format:

```json
{
    "error": {
        "code": "ERROR_CODE",
        "message": "Human readable error message",
        "details": {}
    },
    "request_id": "uuid"
}
```

## Endpoints

### Guilds
- Guild statistics and configuration

### Bytes
- Economy system management
- Balance checking and transfers
- Transaction history

### Squads
- Team management
- Member operations

### AutoMod
- Rule configuration
- Moderation case logging

### System
- Health checks
- API key management
- Metrics (admin only)
        """,
        routes=app.routes,
        tags=[
            {
                "name": "guilds",
                "description": "Guild management and statistics"
            },
            {
                "name": "bytes",
                "description": "Bytes economy system"
            },
            {
                "name": "squads",
                "description": "Squad team management"
            },
            {
                "name": "automod",
                "description": "Auto-moderation configuration"
            },
            {
                "name": "search",
                "description": "Search and query endpoints"
            },
            {
                "name": "system",
                "description": "System administration"
            }
        ]
    )
    
    # Add security scheme
    openapi_schema["components"]["securitySchemes"] = {
        "Bearer": {
            "type": "http",
            "scheme": "bearer",
            "description": "API key authentication"
        }
    }
    
    # Apply security to all operations
    for path in openapi_schema["paths"].values():
        for operation in path.values():
            operation["security"] = [{"Bearer": []}]
    
    app.openapi_schema = openapi_schema
    return app.openapi_schema
```

## Task 5: API Setup Integration

### web/api/__init__.py

Complete API setup with all routers:

```python
from fastapi import FastAPI
from web.api.routers import guilds, bytes, squads, automod, system, search
from web.api.middleware import APILoggingMiddleware, RateLimitMiddleware
from web.api.exceptions import register_exception_handlers
from web.api.docs import customize_openapi

def setup_api_routes(app: FastAPI):
    """Configure all API routes and middleware."""
    
    # Add middleware (order matters - executed in reverse)
    app.add_middleware(RateLimitMiddleware, requests_per_minute=100)
    app.add_middleware(APILoggingMiddleware)
    
    # Register exception handlers
    register_exception_handlers(app)
    
    # Include routers
    routers = [
        (guilds.router, {"prefix": "/v1"}),
        (bytes.router, {"prefix": "/v1"}),
        (squads.router, {"prefix": "/v1"}),
        (automod.router, {"prefix": "/v1"}),
        (system.router, {"prefix": "/v1"}),
        (search.router, {"prefix": "/v1"}),
    ]
    
    for router, kwargs in routers:
        app.include_router(router, **kwargs)
    
    # Customize OpenAPI
    app.openapi = lambda: customize_openapi(app)
    
    # Root endpoint
    @app.get("/")
    async def api_root():
        """API root endpoint."""
        return {
            "name": "Smarter Dev API",
            "version": "2.0.0",
            "documentation": "/docs",
            "health": "/v1/system/health"
        }
    
    # Version endpoint
    @app.get("/v1")
    async def api_v1():
        """API v1 information."""
        return {
            "version": "1.0",
            "endpoints": {
                "guilds": "/v1/guilds",
                "bytes": "/v1/bytes",
                "squads": "/v1/squads",
                "automod": "/v1/automod",
                "system": "/v1/system",
                "search": "/v1/search"
            }
        }
```

## Task 6: Create Tests

### tests/test_api_complete.py

Comprehensive API tests:

```python
import pytest
from httpx import AsyncClient
from datetime import date, timedelta

@pytest.mark.asyncio
async def test_guild_stats(auth_api_client: AsyncClient, test_db):
    """Test guild statistics endpoint."""
    # Create test data
    from web.crud.bytes import bytes_crud
    await bytes_crud.create(
        test_db,
        guild_id="123",
        user_id="456",
        balance=1000
    )
    
    response = await auth_api_client.get("/api/v1/guilds/123/stats")
    
    assert response.status_code == 200
    data = response.json()
    assert data["guild_id"] == "123"
    assert data["total_users"] == 1
    assert data["total_bytes_circulating"] == 1000

@pytest.mark.asyncio
async def test_search_transactions(auth_api_client: AsyncClient, test_db):
    """Test transaction search."""
    # Create transactions
    from web.models.bytes import BytesTransaction
    for i in range(5):
        tx = BytesTransaction(
            guild_id="123",
            giver_id="111",
            giver_username="Giver",
            receiver_id="222",
            receiver_username="Receiver",
            amount=100 + i * 10,
            reason=f"Test {i}"
        )
        test_db.add(tx)
    await test_db.commit()
    
    # Search with filters
    response = await auth_api_client.get(
        "/api/v1/search/transactions",
        params={
            "guild_id": "123",
            "min_amount": 120,
            "limit": 10
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3  # Only 3 transactions >= 120
    assert len(data["items"]) == 3
    assert all(tx["amount"] >= 120 for tx in data["items"])

@pytest.mark.asyncio
async def test_rate_limiting(auth_api_client: AsyncClient):
    """Test API rate limiting."""
    # Make many requests quickly
    responses = []
    for _ in range(101):
        resp = await auth_api_client.get("/api/v1/system/health")
        responses.append(resp)
    
    # Should hit rate limit
    assert any(r.status_code == 429 for r in responses)
    
    # Check rate limit headers
    limited = next(r for r in responses if r.status_code == 429)
    assert "X-RateLimit-Limit" in limited.headers
    assert limited.headers["X-RateLimit-Remaining"] == "0"

@pytest.mark.asyncio
async def test_api_key_management(admin_api_client: AsyncClient):
    """Test API key creation and revocation."""
    # Create key
    response = await admin_api_client.post(
        "/api/v1/system/api-keys",
        json={
            "name": "test_key",
            "description": "Test API key"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "key" in data  # Actual key returned
    assert data["name"] == "test_key"
    key_id = data["id"]
    
    # List keys
    list_response = await admin_api_client.get("/api/v1/system/api-keys")
    assert response.status_code == 200
    keys = list_response.json()
    assert any(k["id"] == key_id for k in keys)
    
    # Revoke key
    revoke_response = await admin_api_client.delete(
        f"/api/v1/system/api-keys/{key_id}"
    )
    assert revoke_response.status_code == 200

@pytest.mark.asyncio
async def test_error_format(auth_api_client: AsyncClient):
    """Test consistent error response format."""
    # 404 error
    response = await auth_api_client.get("/api/v1/guilds/nonexistent/stats")
    assert response.status_code == 404
    
    error = response.json()
    assert "error" in error
    assert "code" in error["error"]
    assert "message" in error["error"]
    assert "request_id" in error

@pytest.mark.asyncio
async def test_pagination(auth_api_client: AsyncClient, test_db):
    """Test pagination parameters."""
    # Create many transactions
    from web.models.bytes import BytesTransaction
    for i in range(25):
        tx = BytesTransaction(
            guild_id="123",
            giver_id="111",
            giver_username="User",
            receiver_id="222",
            receiver_username="Other",
            amount=100
        )
        test_db.add(tx)
    await test_db.commit()
    
    # First page
    page1 = await auth_api_client.get(
        "/api/v1/search/transactions",
        params={"guild_id": "123", "limit": 10, "offset": 0}
    )
    
    assert page1.status_code == 200
    data1 = page1.json()
    assert len(data1["items"]) == 10
    assert data1["has_more"] is True
    
    # Second page
    page2 = await auth_api_client.get(
        "/api/v1/search/transactions",
        params={"guild_id": "123", "limit": 10, "offset": 10}
    )
    
    data2 = page2.json()
    assert len(data2["items"]) == 10
    
    # Items should be different
    page1_ids = {str(item["id"]) for item in data1["items"]}
    page2_ids = {str(item["id"]) for item in data2["items"]}
    assert page1_ids.isdisjoint(page2_ids)
```

## Deliverables

1. **Complete API Schemas**
   - All request/response models
   - Comprehensive validation
   - Clear documentation
   - Consistent naming

2. **Error Handling**
   - Custom exception types
   - Consistent error format
   - Request ID tracking
   - Proper status codes

3. **Middleware**
   - Request logging
   - Rate limiting
   - Timing information
   - Error formatting

4. **All API Endpoints**
   - Guild management
   - Complete bytes system
   - Squad operations
   - AutoMod configuration
   - Search functionality
   - System administration

5. **API Documentation**
   - OpenAPI/Swagger UI
   - Authentication guide
   - Error code reference
   - Example requests

6. **Test Coverage**
   - All endpoints tested
   - Error cases covered
   - Rate limiting tested
   - Pagination tested

## Important Notes

1. All endpoints require authentication except health check
2. Rate limiting applies per API key
3. Consistent error format across all endpoints
4. Request IDs for debugging
5. Comprehensive input validation
6. Efficient database queries with proper indexing

This complete API implementation provides a robust, well-documented interface for all platform features.