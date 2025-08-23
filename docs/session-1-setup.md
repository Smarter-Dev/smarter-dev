# Session 1: Project Setup and Environment Configuration

**Goal:** Understand existing structure, setup development environment, and create comprehensive documentation

## Task Description

Analyze the existing Smarter Dev project structure and set up the development environment for implementing bytes and squads systems.

### Current Structure
- Existing Starlette application with landing page
- Need to add authenticated admin interface
- Need to add Discord bot with bytes and squads features
- Need to integrate bot with web API

### Requirements
1. Use Python 3.11+ with modern type hints
2. Use uv for package management
3. Use docker compose for local development (note: podman compose is available locally)
4. Test-driven development approach
5. Use httpx.AsyncClient with app transport for testing
6. Never add special test logic in application code

## Deliverables

### 1. CLAUDE.md - Comprehensive development documentation including:
- Project overview and architecture
- Development setup instructions
- Testing strategy and examples
- Code style guidelines
- Common tasks and troubleshooting
- API documentation structure

### 2. docker-compose.yml with:
- PostgreSQL 15+ with proper initialization
- Redis 7+ for pub/sub and caching
- Proper networking between services
- Volume mounts for development
- Environment variable configuration

### 3. pyproject.toml with:
- Project metadata
- Dependencies grouped by category (bot, web, dev, test)
- Development scripts
- Test configuration
- Code formatting settings

### Core dependencies needed:
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

### 4. .env.example with all required variables:
- Database configuration
- Redis configuration
- Discord bot token and application ID
- Web app session secret
- API authentication keys
- Development mode flags

### 5. Project structure:
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

### 6. shared/config.py - Configuration using pydantic-settings:
- Environment-based configuration
- Validation for all settings
- Clear defaults for development

### 7. Testing strategy documentation:
- How to test bot event listeners using layered architecture
- How to test API endpoints using httpx.AsyncClient
- How to test database operations with transactions
- How to test Redis pub/sub functionality

## Architecture Approach

The bot architecture should use a layered approach where:
- Event listeners/commands are thin wrappers
- All business logic is in testable service classes
- Services can be tested independently of Discord

### Example bot testing approach:
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