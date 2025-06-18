You're absolutely right! That's a brilliant approach - we can use Starlette for the user-facing site with proper HTML error pages, and mount a FastAPI sub-application for the API endpoints. This gives us the best of both worlds.

## Revised Architecture: Starlette + Mounted FastAPI

### Application Structure

```python
# main.py
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.middleware.sessions import SessionMiddleware
from fastapi import FastAPI
import uvicorn

# Create the main Starlette app for user-facing pages
app = Starlette(debug=True)
app.add_middleware(SessionMiddleware, secret_key="your-secret-key")

# Create FastAPI sub-app for API
api = FastAPI(
    title="Smarter Dev API",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Mount the API under /api
app.mount("/api", api)

# Starlette routes for user-facing pages
from routes import web_routes
app.routes.extend(web_routes)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### Project Structure

```
smarter-dev/
├── bot/
│   ├── __init__.py
│   ├── bot.py                 # Hikari bot setup
│   ├── config.py              # Bot configuration
│   ├── plugins/
│   │   ├── bytes.py           # Bytes system plugin
│   │   ├── squads.py          # Squads plugin
│   │   └── automod.py         # Auto-moderation plugin
│   └── utils/
│       ├── cache.py           # Caching utilities
│       └── api_client.py      # API client for bot
│
├── web/
│   ├── __init__.py
│   ├── main.py                # Main app setup (Starlette + FastAPI)
│   ├── config.py              # Web configuration
│   ├── database.py            # Database setup
│   ├── models.py              # SQLAlchemy models
│   ├── auth.py                # Authentication logic
│   │
│   ├── api/                   # FastAPI endpoints
│   │   ├── __init__.py
│   │   ├── dependencies.py    # Shared dependencies
│   │   ├── routers/
│   │   │   ├── guilds.py      # Guild endpoints
│   │   │   ├── bytes.py       # Bytes endpoints
│   │   │   ├── squads.py      # Squad endpoints
│   │   │   └── moderation.py  # Moderation endpoints
│   │   └── schemas.py         # Pydantic models
│   │
│   ├── pages/                 # Starlette page routes
│   │   ├── __init__.py
│   │   ├── public.py          # Landing page, login
│   │   ├── admin.py           # Admin dashboard pages
│   │   └── errors.py          # Error page handlers
│   │
│   ├── templates/             # Jinja2 templates
│   │   ├── base.html
│   │   ├── landing.html
│   │   ├── admin/
│   │   │   ├── dashboard.html
│   │   │   └── guild.html
│   │   └── errors/
│   │       ├── 404.html
│   │       └── 500.html
│   │
│   └── static/                # Static assets
│       ├── css/
│       ├── js/
│       └── img/
│
├── shared/                    # Shared between bot and web
│   ├── redis_client.py
│   ├── discord_client.py
│   └── constants.py
│
├── docker-compose.yml
├── requirements.txt
└── .env
```

### Implementation Example

```python
# web/main.py
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles
from fastapi import FastAPI
from contextlib import asynccontextmanager

from database import init_db, close_db
from pages import public_routes, admin_routes
from api import api_router
from shared.redis_client import init_redis

@asynccontextmanager
async def lifespan(app):
    # Startup
    await init_db()
    await init_redis()
    yield
    # Shutdown
    await close_db()

# Main Starlette app
app = Starlette(
    debug=True,
    lifespan=lifespan
)

# Middleware
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Page routes (Starlette)
app.routes.extend(public_routes)
app.routes.extend(admin_routes)

# API app (FastAPI)
api = FastAPI(
    title="Smarter Dev API",
    version="2.0.0",
    lifespan=lifespan  # Share the same lifespan
)

api.include_router(api_router, prefix="/v1")

# Mount API to main app
app.mount("/api", api)
```

```python
# web/pages/admin.py
from starlette.routing import Route
from starlette.responses import HTMLResponse
from starlette.exceptions import HTTPException
from .base import templates, require_auth
from shared.discord_client import discord

async def guild_dashboard(request):
    """Starlette view for guild dashboard"""
    guild_id = request.path_params['guild_id']
    
    try:
        # Fetch real-time data from Discord
        guild = await discord.fetch_guild(guild_id)
        
        # Fetch our stored data
        config = await db.get_bytes_config(guild_id)
        stats = await db.get_guild_stats(guild_id)
        
        return templates.TemplateResponse('admin/guild.html', {
            'request': request,
            'guild': guild,
            'config': config,
            'stats': stats
        })
    except Exception as e:
        raise HTTPException(status_code=404, detail="Guild not found")

# Routes with proper error handling
admin_routes = [
    Route('/admin', dashboard, methods=['GET']),
    Route('/admin/guilds/{guild_id}', guild_dashboard, methods=['GET']),
    Route('/admin/settings', settings, methods=['GET', 'POST']),
]
```

```python
# web/api/routers/guilds.py
from fastapi import APIRouter, Depends, HTTPException
from typing import List
from ..schemas import GuildConfig, BytesTransaction
from ..dependencies import get_current_bot, verify_guild_access

router = APIRouter(prefix="/guilds", tags=["guilds"])

@router.get("/{guild_id}/config", response_model=GuildConfig)
async def get_guild_config(
    guild_id: str,
    bot = Depends(get_current_bot),
    access = Depends(verify_guild_access)
):
    """Get guild configuration"""
    config = await db.get_bytes_config(guild_id)
    if not config:
        raise HTTPException(404, "Configuration not found")
    return config

@router.put("/{guild_id}/config", response_model=GuildConfig)
async def update_guild_config(
    guild_id: str,
    config: GuildConfig,
    bot = Depends(get_current_bot),
    access = Depends(verify_guild_access)
):
    """Update guild configuration"""
    updated = await db.update_bytes_config(guild_id, config)
    
    # Notify bot via Redis
    await redis.publish(f"config_update:{guild_id}", config.json())
    
    return updated

@router.get("/{guild_id}/bytes/transactions", response_model=List[BytesTransaction])
async def get_transactions(
    guild_id: str,
    limit: int = 100,
    offset: int = 0,
    bot = Depends(get_current_bot)
):
    """Get recent bytes transactions"""
    return await db.get_transactions(guild_id, limit, offset)
```

```python
# web/pages/errors.py
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse

async def handle_404(request, exc):
    """Custom 404 page for Starlette routes"""
    return templates.TemplateResponse('errors/404.html', {
        'request': request,
        'message': str(exc.detail) if hasattr(exc, 'detail') else 'Page not found'
    }, status_code=404)

async def handle_500(request, exc):
    """Custom 500 page for Starlette routes"""
    return templates.TemplateResponse('errors/500.html', {
        'request': request,
        'message': 'Internal server error'
    }, status_code=500)

# Exception handlers for the Starlette app
exception_handlers = {
    404: handle_404,
    500: handle_500,
    HTTPException: handle_http_exception
}
```

### Authentication Strategy

```python
# web/auth.py
from starlette.authentication import AuthenticationBackend, SimpleUser
from starlette.middleware.authentication import AuthenticationMiddleware
from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# For Starlette pages - session-based
class SessionAuthBackend(AuthenticationBackend):
    async def authenticate(self, request):
        if "user_id" in request.session:
            # Dev mode: username auth
            if config.DEV_MODE:
                return SimpleUser(request.session["user_id"]), ["authenticated"]
            
            # Production: Discord OAuth
            user_data = await redis.get(f"session:{request.session['user_id']}")
            if user_data:
                return SimpleUser(user_data["id"]), ["authenticated"]
        
        return None, []

# For FastAPI - Bearer token
security = HTTPBearer()

async def get_current_bot(credentials: HTTPAuthorizationCredentials = Security(security)):
    """Verify bot API token"""
    token = credentials.credentials
    
    # Verify token in database
    api_key = await db.verify_api_key(token)
    if not api_key:
        raise HTTPException(401, "Invalid API key")
    
    return api_key
```

### Error Handling Benefits

```python
# Users see nice HTML error pages on the main site
# GET /admin/guilds/invalid-id
# Returns: Beautiful 404.html page

# API consumers get proper JSON errors with FastAPI validation
# GET /api/v1/guilds/invalid-id
# Returns: {"detail": "Guild not found"}

# FastAPI automatic validation
# POST /api/v1/guilds/123/config with invalid body
# Returns: {"detail": [{"loc": ["body", "daily_amount"], "msg": "ensure this value is greater than 0"}]}
```

### Frontend Integration

```javascript
// admin/static/js/api.js
class SmarterDevAPI {
    constructor() {
        this.baseURL = '/api/v1';
        this.token = localStorage.getItem('api_token');
    }
    
    async fetchGuildConfig(guildId) {
        const response = await fetch(`${this.baseURL}/guilds/${guildId}/config`, {
            headers: {
                'Authorization': `Bearer ${this.token}`
            }
        });
        
        if (!response.ok) {
            // FastAPI returns nice error structure
            const error = await response.json();
            throw new Error(error.detail);
        }
        
        return response.json();
    }
    
    async updateGuildConfig(guildId, config) {
        const response = await fetch(`${this.baseURL}/guilds/${guildId}/config`, {
            method: 'PUT',
            headers: {
                'Authorization': `Bearer ${this.token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(config)
        });
        
        if (!response.ok) {
            const error = await response.json();
            // FastAPI validation errors have detailed structure
            if (error.detail && Array.isArray(error.detail)) {
                const messages = error.detail.map(e => e.msg).join(', ');
                throw new Error(`Validation failed: ${messages}`);
            }
            throw new Error(error.detail || 'Update failed');
        }
        
        return response.json();
    }
}
```

### Key Advantages

1. **Best of Both Worlds**
   - Starlette handles user-facing pages with proper HTML errors
   - FastAPI handles API with automatic validation and documentation

2. **Shared Resources**
   - Both apps share the same database connection pool
   - Same Redis connection for pub/sub
   - Shared Discord client instance

3. **Developer Experience**
   - FastAPI's automatic OpenAPI docs at `/api/docs`
   - Pydantic validation for API requests
   - Type hints and IDE support

4. **Clean Separation**
   - `/admin/*` routes return HTML (Starlette)
   - `/api/*` routes return JSON (FastAPI)
   - Clear error handling for each context

5. **Easy Testing**
   - FastAPI's TestClient for API testing
   - Starlette's TestClient for page testing
   - Can test both in same test suite

This hybrid approach gives you a production-ready setup where users never see raw JSON errors, while API consumers get properly structured JSON responses with automatic validation. The bot can consume the API with confidence knowing it will get consistent JSON responses, while administrators get a polished web experience.
