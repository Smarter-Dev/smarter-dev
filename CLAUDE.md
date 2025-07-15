# Smarter Dev Discord Bot - Development Guide

## Project Overview

The Smarter Dev Discord Bot is a comprehensive system that combines Discord bot functionality with a web API and admin interface. The project implements a bytes economy system and team-based squads feature for Discord servers.

### Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Discord Bot   │    │   Web API       │    │  Admin Interface│
│  (Hikari +      │◄──►│  (FastAPI)      │◄──►│  (Starlette)    │
│   Lightbulb)    │    │                 │    │                 │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         │              ┌─────────────────┐              │
         │              │   PostgreSQL    │              │
         └──────────────┤   Database      │──────────────┘
                        │                 │
                        └─────────────────┘
                                │
                        ┌─────────────────┐
                        │     Redis       │
                        │  (Pub/Sub &     │
                        │   Caching)      │
                        └─────────────────┘
```

**Core Components:**
- **Discord Bot**: Hikari + Lightbulb for Discord interactions
- **Web API**: FastAPI mounted on existing Starlette app
- **Admin Interface**: Authenticated web pages for guild configuration
- **Database**: PostgreSQL with minimal data storage
- **Message Queue**: Redis for real-time updates

### Key Features

1. **Bytes Economy System**
   - Virtual currency for Discord servers
   - Daily rewards with streak bonuses
   - Peer-to-peer transfers with optional reasons
   - Configurable amounts and cooldowns
   - Leaderboards and statistics

2. **Squads System**
   - Team-based groupings using Discord roles
   - Configurable switch costs
   - Member limits and management
   - Admin controls for squad creation/deletion

## Development Setup

### Prerequisites

- Python 3.11+
- Docker/Podman Compose
- uv package manager
- Git

### Initial Setup

1. **Clone and navigate to project**
   ```bash
   git clone <repository-url>
   cd smarter-dev
   ```

2. **Install dependencies**
   ```bash
   uv install
   ```

3. **Set up environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

4. **Start development services**
   ```bash
   docker-compose up -d postgres redis
   ```

5. **Run database migrations**
   ```bash
   uv run alembic upgrade head
   ```

6. **Start the application**
   ```bash
   # Web application
   uv run python main.py

   # Discord bot (separate terminal)
   uv run python -m smarter_dev.bot
   ```

### Development Commands

```bash
# Install dependencies
uv install

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=smarter_dev

# Type checking
uv run mypy smarter_dev/

# Format code
uv run ruff format .

# Lint code
uv run ruff check .

# Database migrations
uv run alembic revision --autogenerate -m "Description"
uv run alembic upgrade head

# Start development environment
docker-compose up -d

# View logs
docker-compose logs -f postgres redis
```

## Testing Strategy

### Architecture for Testability

The project uses a layered architecture that separates Discord-specific code from business logic:

```python
# Layer 1: Discord Event Handlers (thin wrapper)
@plugin.command
async def bytes_balance(ctx: lightbulb.Context) -> None:
    service = ctx.bot.d.bytes_service
    balance = await service.get_balance(str(ctx.guild_id), str(ctx.author.id))
    await ctx.respond(embed=balance.to_embed())

# Layer 2: Service Layer (business logic - fully testable)
class BytesService:
    async def get_balance(self, guild_id: str, user_id: str) -> BytesBalance:
        # All business logic here
        # This can be tested without Discord

# Layer 3: Data Layer (API/Database access - mockable)
class BytesOperations:
    async def get_balance(self, session, guild_id: str, user_id: str) -> BytesBalance:
        # Database operations that can be mocked
```

### Testing Approach

#### 1. Unit Testing Services

Services contain all business logic and can be tested without Discord dependencies:

```python
# tests/bot/test_bytes_service.py
import pytest
from unittest.mock import AsyncMock, Mock
from smarter_dev.bot.services.bytes_service import BytesService

class TestBytesService:
    @pytest.fixture
    def mock_db_ops(self):
        return AsyncMock()
    
    @pytest.fixture
    def mock_redis(self):
        return AsyncMock()
    
    @pytest.fixture
    def service(self, mock_db_ops, mock_redis):
        return BytesService(mock_db_ops, mock_redis)
    
    async def test_calculate_streak_bonus(self, service):
        """Test pure business logic"""
        assert service._calculate_streak_bonus(0) == 1
        assert service._calculate_streak_bonus(7) == 2
        assert service._calculate_streak_bonus(14) == 4
    
    async def test_get_balance_with_caching(self, service, mock_db_ops, mock_redis):
        """Test service behavior with mocked dependencies"""
        # Mock database response
        mock_db_ops.get_balance.return_value = Mock(
            balance=100,
            streak_count=5,
            last_daily=None
        )
        
        # Mock Redis cache miss
        mock_redis.get.return_value = None
        
        # First call
        balance = await service.get_balance("123", "456")
        assert balance.balance == 100
        
        # Verify cache was set
        mock_redis.set.assert_called_once()
```

#### 2. Integration Testing Commands

```python
# tests/bot/test_commands.py
import pytest
from unittest.mock import AsyncMock, Mock
from smarter_dev.bot.plugins.bytes import bytes_balance

def create_mock_context(guild_id: str = "123", author_id: str = "456") -> Mock:
    """Create a mock command context"""
    ctx = Mock()
    ctx.guild_id = guild_id
    ctx.author = Mock(id=author_id, username="TestUser")
    ctx.respond = AsyncMock()
    return ctx

class TestBytesCommands:
    @pytest.fixture
    def mock_service(self):
        service = Mock()
        service.get_balance = AsyncMock()
        return service
    
    async def test_balance_command(self, mock_service):
        """Test balance command calls service correctly"""
        ctx = create_mock_context()
        ctx.bot = Mock()
        ctx.bot.d.bytes_service = mock_service
        
        # Mock service response
        mock_service.get_balance.return_value = Mock(
            balance=100,
            to_embed=Mock(return_value=Mock())
        )
        
        # Execute command
        await bytes_balance(ctx)
        
        # Verify service was called
        mock_service.get_balance.assert_called_once_with("123", "456")
        ctx.respond.assert_called_once()
```

#### 3. API Testing

```python
# tests/web/test_api.py
import pytest
from httpx import AsyncClient
from smarter_dev.web.api.app import api

@pytest.fixture
async def client():
    async with AsyncClient(app=api, base_url="http://test") as client:
        yield client

class TestBytesAPI:
    async def test_get_balance(self, client, mock_db_session):
        """Test API endpoint"""
        headers = {"Authorization": "Bearer test-token"}
        
        # Mock database response
        mock_db_session.return_value.get_balance.return_value = Mock(
            balance=100,
            streak_count=5
        )
        
        response = await client.get(
            "/guilds/123/bytes/balance/456",
            headers=headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["balance"] == 100
        assert data["streak_count"] == 5
```

#### 4. Database Testing

```python
# tests/test_database.py
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from smarter_dev.web.models import BytesBalance

@pytest.fixture
async def db_session():
    """Create a test database session with transaction rollback"""
    async with test_engine.begin() as conn:
        async with async_session(bind=conn) as session:
            yield session
            await session.rollback()

class TestBytesOperations:
    async def test_create_balance(self, db_session: AsyncSession):
        """Test balance creation"""
        balance = BytesBalance(
            guild_id="123",
            user_id="456",
            balance=100
        )
        
        db_session.add(balance)
        await db_session.commit()
        
        # Verify creation
        result = await db_session.get(BytesBalance, ("123", "456"))
        assert result.balance == 100
```

### Testing Principles

1. **Separate Concerns**: Keep Discord-specific code minimal, put business logic in services
2. **Mock External Dependencies**: Mock Discord objects, database, and Redis
3. **Test Services Thoroughly**: Services should have comprehensive unit tests
4. **Test Integration Points**: Verify commands/endpoints call services correctly
5. **Never Test Library Code**: Don't test Hikari/Lightbulb/FastAPI functionality
6. **Use Transactions**: Database tests should use rollback for isolation

## Code Style Guidelines

### Python Style

- Use Python 3.11+ features (type hints, dataclasses, etc.)
- Follow PEP 8 with 88-character line length
- Use type hints for all function signatures
- Use dataclasses for simple data containers
- Use Pydantic models for validation

### Imports

```python
# Standard library
import asyncio
from datetime import datetime
from typing import Optional, List

# Third-party
import hikari
import lightbulb
from fastapi import FastAPI, Depends
from sqlalchemy import select

# Local
from smarter_dev.shared.config import settings
from smarter_dev.bot.services.bytes_service import BytesService
```

### Error Handling

```python
# Bot commands
@plugin.command
async def command(ctx: lightbulb.Context) -> None:
    try:
        result = await service.operation()
        await ctx.respond(result.to_embed())
    except ServiceError as e:
        await ctx.respond(f"Error: {e.message}", flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.exception("Unexpected error in command")
        await ctx.respond("An unexpected error occurred.", flags=hikari.MessageFlag.EPHEMERAL)

# API endpoints
@router.get("/endpoint")
async def endpoint():
    try:
        result = await service.operation()
        return result
    except ServiceError as e:
        raise HTTPException(status_code=400, detail=e.message)
```

### Logging

```python
import logging

logger = logging.getLogger(__name__)

# Use structured logging
logger.info("User balance retrieved", extra={
    "guild_id": guild_id,
    "user_id": user_id,
    "balance": balance.amount
})
```

## Common Tasks

### Adding a New Bot Command

1. **Create the service method**
   ```python
   # smarter_dev/bot/services/bytes_service.py
   async def new_operation(self, guild_id: str, user_id: str) -> Result:
       # Business logic here
       pass
   ```

2. **Add the command**
   ```python
   # smarter_dev/bot/plugins/bytes.py
   @plugin.command
   async def new_command(ctx: lightbulb.Context) -> None:
       service = ctx.bot.d.bytes_service
       result = await service.new_operation(str(ctx.guild_id), str(ctx.author.id))
       await ctx.respond(result.to_embed())
   ```

3. **Write tests**
   ```python
   # tests/bot/test_bytes_service.py
   async def test_new_operation(self, service):
       result = await service.new_operation("123", "456")
       assert result.success
   ```

### Adding a New API Endpoint

1. **Create Pydantic schemas**
   ```python
   # smarter_dev/web/api/schemas.py
   class NewRequest(BaseModel):
       field: str
   
   class NewResponse(BaseModel):
       result: str
   ```

2. **Add the endpoint**
   ```python
   # smarter_dev/web/api/routers/bytes.py
   @router.post("/new", response_model=NewResponse)
   async def new_endpoint(request: NewRequest):
       result = await service.new_operation(request.field)
       return NewResponse(result=result)
   ```

3. **Write tests**
   ```python
   # tests/web/test_bytes_api.py
   async def test_new_endpoint(self, client):
       response = await client.post("/new", json={"field": "value"})
       assert response.status_code == 200
   ```

### Database Migrations

1. **Make model changes**
   ```python
   # smarter_dev/web/models.py
   class BytesBalance(Base):
       # Add new field
       new_field: str = Column(String, nullable=True)
   ```

2. **Generate migration**
   ```bash
   uv run alembic revision --autogenerate -m "Add new_field to BytesBalance"
   ```

3. **Review and run migration**
   ```bash
   # Review generated migration file
   uv run alembic upgrade head
   ```

## Project Structure

```
smarter_dev/
├── bot/                      # Discord bot code
│   ├── __init__.py
│   ├── client.py            # Bot client setup
│   ├── plugins/             # Bot command plugins
│   │   ├── __init__.py
│   │   ├── bytes.py         # Bytes economy commands
│   │   └── squads.py        # Squad management commands
│   ├── services/            # Business logic layer
│   │   ├── __init__.py
│   │   ├── bytes_service.py # Bytes business logic
│   │   └── squads_service.py # Squad business logic
│   └── views/               # Discord UI components
│       ├── __init__.py
│       └── squad_views.py   # Squad selection views
├── web/                     # Web application code
│   ├── __init__.py
│   ├── api/                 # FastAPI application
│   │   ├── __init__.py
│   │   ├── app.py          # FastAPI app setup
│   │   ├── dependencies.py  # Shared dependencies
│   │   ├── schemas.py      # Pydantic models
│   │   └── routers/        # API route handlers
│   │       ├── __init__.py
│   │       ├── auth.py     # Authentication routes
│   │       ├── bytes.py    # Bytes API routes
│   │       └── squads.py   # Squad API routes
│   ├── admin/              # Admin interface
│   │   ├── __init__.py
│   │   └── routes.py       # Admin web pages
│   ├── models.py           # SQLAlchemy models
│   └── crud.py             # Database operations
├── shared/                  # Shared utilities
│   ├── __init__.py
│   ├── config.py           # Configuration management
│   ├── database.py         # Database setup
│   └── redis_client.py     # Redis client setup
├── tests/                   # Test suites
│   ├── conftest.py         # Test fixtures
│   ├── bot/                # Bot tests
│   │   ├── test_services/  # Service unit tests
│   │   ├── test_commands/  # Command integration tests
│   │   └── test_views/     # UI component tests
│   └── web/                # Web tests
│       ├── test_api/       # API endpoint tests
│       └── test_models/    # Database model tests
├── alembic/                # Database migrations
│   ├── env.py
│   └── versions/
├── docker-compose.yml      # Development environment
├── pyproject.toml          # Project configuration
├── .env.example           # Environment template
└── CLAUDE.md              # This file
```

## API Documentation

The API uses FastAPI with automatic OpenAPI documentation available at:
- Swagger UI: `http://localhost:8000/api/docs`
- ReDoc: `http://localhost:8000/api/redoc`

### Authentication

API endpoints require Bearer token authentication:
```http
Authorization: Bearer <bot-token>
```

### Base URL Structure

```
/api/auth/                   # Authentication endpoints
/api/guilds/{guild_id}/bytes/ # Bytes economy endpoints
/api/guilds/{guild_id}/squads/ # Squad management endpoints
```

## Troubleshooting

### Common Issues

1. **Database Connection Issues**
   ```bash
   # Check if PostgreSQL is running
   docker-compose ps postgres
   
   # View PostgreSQL logs
   docker-compose logs postgres
   
   # Reset database
   docker-compose down postgres
   docker-compose up -d postgres
   ```

2. **Redis Connection Issues**
   ```bash
   # Check Redis status
   docker-compose ps redis
   
   # Test Redis connection
   docker-compose exec redis redis-cli ping
   ```

3. **Bot Not Responding**
   ```bash
   # Check bot logs
   docker-compose logs bot
   
   # Verify bot token in .env
   echo $DISCORD_BOT_TOKEN
   ```

4. **Migration Issues**
   ```bash
   # Check migration status
   uv run alembic current
   
   # View migration history
   uv run alembic history
   
   # Rollback if needed
   uv run alembic downgrade -1
   ```

### Development Tips

1. **Use `uv run` for all commands** to ensure correct environment
2. **Run tests frequently** with `uv run pytest`
3. **Check type hints** with `uv run mypy smarter_dev/`
4. **Format code** with `uv run ruff format .`
5. **Keep services testable** by avoiding Discord dependencies

### Environment Variables

Required environment variables (see `.env.example`):
- `DATABASE_URL`: PostgreSQL connection string
- `REDIS_URL`: Redis connection string
- `DISCORD_BOT_TOKEN`: Discord bot token
- `DISCORD_APPLICATION_ID`: Discord application ID
- `API_SECRET_KEY`: API authentication secret
- `WEB_SESSION_SECRET`: Web session secret

## Contributing

1. **Follow the testing strategy** - write tests for all new code
2. **Use the layered architecture** - keep Discord code thin
3. **Run the full test suite** before submitting changes
4. **Follow the code style guidelines**
5. **Update documentation** for new features

## Next Steps

This documentation covers Session 1 setup. Subsequent sessions will add:
- Session 2: Database models and migrations
- Session 3: Web API implementation
- Session 4: Discord bot commands
- Session 5: Admin interface
- Session 6: Integration and deployment