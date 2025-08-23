# Session 8: Production Readiness

**Goal:** Prepare the application for production deployment

## Task Description

Prepare the Smarter Dev application for production deployment.

### Requirements
1. Environment configuration for production
2. Logging and monitoring setup
3. Performance optimizations
4. Security hardening
5. Deployment configuration

## Deliverables

### 1. shared/logging.py - Comprehensive logging setup:
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

### 2. web/middleware/monitoring.py - Request monitoring:
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

### 3. web/middleware/security.py - Security headers:
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

### 4. bot/monitoring.py - Bot health checks:
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

### 5. .env.production - Production environment template:
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

### 6. docker-compose.production.yml:
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

### 7. Dockerfile.web - Web application container:
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

### 8. Performance optimizations in web/main.py:
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

### 9. scripts/deploy.sh - Deployment script:
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

### 10. web/health.py - Health check endpoints:
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

## Quality Requirements
This production setup includes:
- Comprehensive logging and monitoring
- Security hardening with proper headers
- Performance optimizations (connection pooling, compression)
- Docker containers for easy deployment
- Health check endpoints
- Automated deployment script
- Proper error handling and recovery