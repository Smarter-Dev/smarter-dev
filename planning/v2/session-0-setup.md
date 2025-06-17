# Session 0: Setup & Environment Understanding

## Objective
Understand the existing Smarter Dev platform features, set up the development environment, and create comprehensive documentation for future development sessions.

## Background
You are reimplementing Smarter Dev, a Discord community management platform. This is a complete rewrite with no backwards compatibility requirements. The platform consists of:
- A Discord bot for community interactions
- A web application with admin dashboard
- REST API for bot-web communication

## Task 1: Review Existing Features

Please review the following feature summary and ensure you understand the full scope:

### Core Features
1. **Bytes Economy System**
   - Virtual currency with daily rewards
   - Streak system with multipliers (8=CHAR 2x, 16=SHORT 4x, 32=INT 16x, 64=LONG 256x)
   - Transfer between users with cooldowns
   - Role rewards based on bytes received

2. **Squads System**
   - Users join team roles (squads)
   - Bytes cost for switching squads
   - Single squad membership per guild

3. **Auto-Moderation**
   - Username regex filtering
   - Message rate limiting
   - File extension blocking
   - Configurable actions

4. **Admin Dashboard**
   - Guild configuration
   - User management
   - Analytics and monitoring

## Task 2: Create Project Structure

Create the following directory structure:

```
smarter-dev-v2/
├── bot/
│   ├── __init__.py
│   ├── bot.py
│   ├── config.py
│   ├── plugins/
│   ├── services/
│   ├── utils/
│   └── tests/
├── web/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── models.py
│   ├── api/
│   ├── pages/
│   ├── templates/
│   ├── static/
│   └── tests/
├── shared/
│   ├── __init__.py
│   ├── constants.py
│   └── types.py
├── docker/
│   ├── bot.Dockerfile
│   └── web.Dockerfile
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   └── integration/
├── docker-compose.yml
├── .env.example
├── .gitignore
├── requirements-bot.txt
├── requirements-web.txt
├── requirements-dev.txt
├── pytest.ini
├── CLAUDE.md
└── README.md
```

## Task 3: Set Up Development Environment

### Docker Compose Configuration

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  postgres:
    image: postgres:15-alpine
    environment:
      POSTGRES_USER: smarter
      POSTGRES_PASSWORD: devpassword
      POSTGRES_DB: smarter_dev
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  redis_data:
```

**Note**: The local environment uses `podman compose` which is compatible with docker-compose.yml files.

### Environment Configuration

Create `.env.example` with all necessary variables:

```env
# Environment
DEV_MODE=true

# Database
DATABASE_URL=postgresql+asyncpg://smarter:devpassword@localhost:5432/smarter_dev

# Redis
REDIS_URL=redis://localhost:6379

# Discord Bot
BOT_TOKEN=your_bot_token_here
BOT_APPLICATION_ID=your_app_id_here

# Discord OAuth (Production)
DISCORD_CLIENT_ID=your_client_id_here
DISCORD_CLIENT_SECRET=your_client_secret_here

# Web App
BASE_URL=http://localhost:8000
SESSION_SECRET=your-session-secret-here
ADMIN_DISCORD_IDS=comma,separated,discord,ids

# API
API_KEY_FOR_BOT=generate-a-secure-key
```

## Task 4: Create Requirements Files

### requirements-bot.txt
```
hikari[speedups]>=2.0.0
hikari-lightbulb>=2.3.0
asyncpg>=0.28.0
redis>=5.0.0
httpx>=0.25.0
python-dotenv>=1.0.0
```

### requirements-web.txt
```
starlette>=0.32.0
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
sqlalchemy>=2.0.0
asyncpg>=0.28.0
alembic>=1.12.0
redis>=5.0.0
httpx>=0.25.0
python-dotenv>=1.0.0
jinja2>=3.1.0
python-multipart>=0.0.6
```

### requirements-dev.txt
```
pytest>=7.4.0
pytest-asyncio>=0.21.0
pytest-cov>=4.1.0
httpx>=0.25.0
faker>=20.0.0
black>=23.0.0
ruff>=0.1.0
mypy>=1.7.0
```

## Task 5: Create CLAUDE.md

Create a comprehensive `CLAUDE.md` file that documents:

1. **Project Overview**
   - Architecture diagram
   - Technology choices and rationale
   - Key design decisions

2. **Development Setup**
   - Prerequisites
   - Environment setup steps
   - Running services locally

3. **Testing Strategy**
   - How to test web endpoints using httpx.AsyncClient
   - Bot testing approach with service layer pattern
   - Never include test-specific logic in application code

4. **Code Standards**
   - Type hints everywhere
   - Async/await patterns
   - Error handling conventions
   - Logging standards

5. **Feature Implementation Guide**
   - How features connect between bot and web
   - Redis pub/sub patterns
   - Database query patterns

6. **Common Patterns**
   - Authentication checks
   - Discord API interactions
   - Caching strategies
   - Error responses

## Task 6: Create Test Infrastructure

### Create `tests/conftest.py`:

```python
import pytest
import asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

# Common fixtures for all tests
@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture
async def test_db():
    """Create a test database."""
    # Implementation here

@pytest.fixture
async def test_client():
    """Create test client with app transport."""
    # Use httpx.AsyncClient with app transport
    # No running server needed
```

### Create `pytest.ini`:

```ini
[tool:pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
asyncio_mode = auto
```

## Task 7: Create Initial Documentation

### README.md
- Project description
- Quick start guide
- Architecture overview
- Contributing guidelines

### CLAUDE.md Structure

The CLAUDE.md should include:

1. **Quick Reference**
   - Common commands
   - File locations
   - Testing commands

2. **Architecture Decisions**
   - Why Starlette + FastAPI hybrid
   - Why Hikari over other bot libraries
   - Database design philosophy

3. **Testing Philosophy**
   - Service layer pattern for bot
   - httpx.AsyncClient for web testing
   - Mock strategies

4. **Implementation Patterns**
   - Example: How to add a new bot command
   - Example: How to add a new API endpoint
   - Example: How to add a new admin page

5. **Troubleshooting**
   - Common issues and solutions
   - Debug strategies
   - Performance profiling

## Important Notes

1. **Test-Driven Development**: Write tests first, then implementation
2. **No Test Logic in App Code**: Never add `if testing:` or similar
3. **Service Layer Pattern**: Bot commands should be thin, logic in services
4. **Type Safety**: Use type hints everywhere
5. **Async First**: All I/O operations should be async

## Deliverables

1. Complete project structure created
2. Docker compose file ready
3. All requirements files created
4. Comprehensive CLAUDE.md documentation
5. Test infrastructure set up
6. Initial README.md

This session establishes the foundation for all future development. Take time to understand the existing features and create clear, helpful documentation that will guide the implementation of each component.