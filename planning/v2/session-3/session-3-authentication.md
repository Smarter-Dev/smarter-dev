# Session 3: Authentication System

## Objective
Implement a dual-mode authentication system supporting username-based auth for development and Discord OAuth2 for production. Create secure session management and API key authentication for the bot.

## Prerequisites
- Completed Session 2 (database models exist)
- Understanding of OAuth2 flow
- Redis running for session storage

## Task 1: Session Storage

### web/auth/sessions.py

Create Redis-backed session storage:

```python
import json
import secrets
from typing import Optional, Dict, Any
from datetime import timedelta
import redis.asyncio as redis
from shared.utils import utcnow
import structlog

logger = structlog.get_logger()

class SessionStore:
    """Redis-backed session storage."""
    
    def __init__(self, redis_client: redis.Redis, prefix: str = "session"):
        self.redis = redis_client
        self.prefix = prefix
    
    def _key(self, session_id: str) -> str:
        """Generate Redis key for session."""
        return f"{self.prefix}:{session_id}"
    
    async def create(
        self, 
        data: Dict[str, Any], 
        ttl: int = 86400
    ) -> str:
        """Create new session and return ID."""
        session_id = secrets.token_urlsafe(32)
        key = self._key(session_id)
        
        session_data = {
            **data,
            "created_at": utcnow().isoformat(),
            "session_id": session_id
        }
        
        await self.redis.setex(
            key,
            ttl,
            json.dumps(session_data)
        )
        
        logger.info("Session created", session_id=session_id)
        return session_id
    
    async def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session data."""
        if not session_id:
            return None
            
        key = self._key(session_id)
        data = await self.redis.get(key)
        
        if data:
            return json.loads(data)
        return None
    
    async def update(
        self, 
        session_id: str, 
        data: Dict[str, Any],
        extend_ttl: bool = True
    ) -> bool:
        """Update existing session."""
        key = self._key(session_id)
        
        # Get current TTL
        ttl = await self.redis.ttl(key)
        if ttl <= 0:
            return False
        
        current = await self.get(session_id)
        if not current:
            return False
        
        updated = {**current, **data}
        
        if extend_ttl:
            await self.redis.setex(key, 86400, json.dumps(updated))
        else:
            await self.redis.setex(key, ttl, json.dumps(updated))
        
        return True
    
    async def delete(self, session_id: str) -> bool:
        """Delete session."""
        key = self._key(session_id)
        result = await self.redis.delete(key)
        
        if result:
            logger.info("Session deleted", session_id=session_id)
        
        return bool(result)
    
    async def exists(self, session_id: str) -> bool:
        """Check if session exists."""
        key = self._key(session_id)
        return await self.redis.exists(key)
```

## Task 2: Authentication Router

### web/auth/router.py

Create the custom authenticated router:

```python
from starlette.routing import Router, Route
from starlette.responses import RedirectResponse
from starlette.requests import Request
from typing import List, Optional, Callable
from functools import wraps
import structlog

logger = structlog.get_logger()

class AuthenticatedRouter(Router):
    """Router that requires authentication for all routes."""
    
    def __init__(
        self, 
        routes: Optional[List[Route]] = None,
        redirect_url: str = "/auth/login",
        allowed_paths: Optional[List[str]] = None,
        auth_checker: Optional[Callable] = None
    ):
        super().__init__(routes or [])
        self.redirect_url = redirect_url
        self.allowed_paths = allowed_paths or []
        self.auth_checker = auth_checker or self.default_auth_check
    
    async def default_auth_check(self, request: Request) -> bool:
        """Default authentication check."""
        # Check session
        if "user" in request.session:
            return True
        
        # Check if session exists in Redis
        session_id = request.session.get("session_id")
        if session_id and hasattr(request.app.state, "session_store"):
            session_data = await request.app.state.session_store.get(session_id)
            if session_data:
                request.session["user"] = session_data.get("user")
                return True
        
        return False
    
    async def __call__(self, scope, receive, send):
        """Handle request with authentication check."""
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return
        
        path = scope["path"]
        
        # Check if path is allowed without auth
        if any(path.startswith(allowed) for allowed in self.allowed_paths):
            await super().__call__(scope, receive, send)
            return
        
        # Create request to check auth
        request = Request(scope, receive)
        
        # Check authentication
        if not await self.auth_checker(request):
            # Store original URL for redirect after login
            request.session["redirect_after_login"] = str(request.url)
            
            response = RedirectResponse(url=self.redirect_url)
            await response(scope, receive, send)
            return
        
        # User is authenticated
        await super().__call__(scope, receive, send)

def require_auth(redirect_to: str = "/auth/login"):
    """Decorator for individual routes requiring authentication."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(request: Request):
            # Check if user is authenticated
            if "user" not in request.session:
                request.session["redirect_after_login"] = str(request.url)
                return RedirectResponse(url=redirect_to)
            
            # Call original function
            return await func(request)
        
        return wrapper
    return decorator
```

## Task 3: OAuth2 Implementation

### web/auth/oauth.py

Discord OAuth2 implementation:

```python
import httpx
import secrets
from typing import Optional, Dict, Any
from urllib.parse import urlencode
from web.config import WebConfig
import structlog

logger = structlog.get_logger()

class DiscordOAuth:
    """Discord OAuth2 client."""
    
    def __init__(self, config: WebConfig):
        self.config = config
        self.client_id = config.discord_client_id
        self.client_secret = config.discord_client_secret
        self.redirect_uri = f"{config.base_url}/auth/callback"
        self.api_base = "https://discord.com/api/v10"
    
    def get_authorization_url(self, state: str) -> str:
        """Generate OAuth2 authorization URL."""
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "identify guilds",
            "state": state
        }
        return f"{self.api_base}/oauth2/authorize?{urlencode(params)}"
    
    async def exchange_code(self, code: str) -> Optional[Dict[str, Any]]:
        """Exchange authorization code for tokens."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.api_base}/oauth2/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri
                }
            )
            
            if response.status_code == 200:
                return response.json()
            
            logger.error(
                "Token exchange failed",
                status=response.status_code,
                error=response.text
            )
            return None
    
    async def get_user_info(self, access_token: str) -> Optional[Dict[str, Any]]:
        """Get user information using access token."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.api_base}/users/@me",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            if response.status_code == 200:
                return response.json()
            
            logger.error(
                "User info fetch failed",
                status=response.status_code
            )
            return None
    
    async def get_user_guilds(self, access_token: str) -> Optional[List[Dict[str, Any]]]:
        """Get user's guilds."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.api_base}/users/@me/guilds",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            if response.status_code == 200:
                return response.json()
            
            return None
    
    async def refresh_token(self, refresh_token: str) -> Optional[Dict[str, Any]]:
        """Refresh access token."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.api_base}/oauth2/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token
                }
            )
            
            if response.status_code == 200:
                return response.json()
            
            return None
```

## Task 4: Authentication Pages

### web/pages/auth.py

Authentication route handlers:

```python
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import RedirectResponse, HTMLResponse
from starlette.templating import Jinja2Templates
import secrets
from web.config import WebConfig
from web.auth.oauth import DiscordOAuth
from web.models.admin import AdminUser
from web.database import get_db
from sqlalchemy import select
import structlog

logger = structlog.get_logger()
templates = Jinja2Templates(directory="web/templates")

async def login_page(request: Request) -> HTMLResponse:
    """Show login page."""
    config: WebConfig = request.app.state.config
    error = request.query_params.get("error")
    
    if config.dev_mode:
        # Development mode - username form
        return templates.TemplateResponse(
            "auth/login_dev.html",
            {
                "request": request,
                "error": error
            }
        )
    else:
        # Production mode - Discord OAuth
        state = secrets.token_urlsafe(32)
        request.session["oauth_state"] = state
        
        oauth = DiscordOAuth(config)
        discord_url = oauth.get_authorization_url(state)
        
        return templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "discord_url": discord_url,
                "error": error
            }
        )

async def login_dev(request: Request) -> RedirectResponse:
    """Handle development mode login."""
    if request.method != "POST":
        return RedirectResponse(url="/auth/login", status_code=303)
    
    form = await request.form()
    username = form.get("username", "").strip()
    
    if not username or len(username) < 3:
        return RedirectResponse(
            url="/auth/login?error=invalid_username",
            status_code=303
        )
    
    # Create admin user if doesn't exist
    async for db in get_db():
        result = await db.execute(
            select(AdminUser).where(AdminUser.username == username)
        )
        admin = result.scalar_one_or_none()
        
        if not admin:
            admin = AdminUser(username=username)
            db.add(admin)
            await db.commit()
    
    # Create session
    session_store = request.app.state.session_store
    session_id = await session_store.create({
        "user": {
            "id": str(admin.id),
            "username": admin.username,
            "is_dev": True
        }
    })
    
    request.session["session_id"] = session_id
    request.session["user"] = {
        "id": str(admin.id),
        "username": admin.username,
        "is_dev": True
    }
    
    # Redirect to original URL or dashboard
    redirect_url = request.session.pop("redirect_after_login", "/admin")
    return RedirectResponse(url=redirect_url, status_code=303)

async def oauth_callback(request: Request) -> RedirectResponse:
    """Handle Discord OAuth callback."""
    config: WebConfig = request.app.state.config
    
    # Verify state
    state = request.query_params.get("state")
    if not state or state != request.session.get("oauth_state"):
        logger.warning("OAuth state mismatch")
        return RedirectResponse(
            url="/auth/login?error=invalid_state",
            status_code=303
        )
    
    # Get authorization code
    code = request.query_params.get("code")
    if not code:
        return RedirectResponse(
            url="/auth/login?error=no_code",
            status_code=303
        )
    
    # Exchange code for tokens
    oauth = DiscordOAuth(config)
    tokens = await oauth.exchange_code(code)
    
    if not tokens:
        return RedirectResponse(
            url="/auth/login?error=token_exchange_failed",
            status_code=303
        )
    
    # Get user info
    user_info = await oauth.get_user_info(tokens["access_token"])
    
    if not user_info:
        return RedirectResponse(
            url="/auth/login?error=user_fetch_failed",
            status_code=303
        )
    
    # Check if user is admin
    if user_info["id"] not in config.admin_discord_ids:
        logger.warning(
            "Unauthorized login attempt",
            user_id=user_info["id"],
            username=user_info["username"]
        )
        return RedirectResponse(
            url="/auth/login?error=unauthorized",
            status_code=303
        )
    
    # Create session
    session_store = request.app.state.session_store
    session_id = await session_store.create({
        "user": {
            "id": user_info["id"],
            "username": user_info["username"],
            "discriminator": user_info["discriminator"],
            "avatar": user_info["avatar"],
            "is_dev": False
        },
        "tokens": tokens
    })
    
    request.session["session_id"] = session_id
    request.session["user"] = {
        "id": user_info["id"],
        "username": user_info["username"],
        "is_dev": False
    }
    
    # Redirect to original URL or dashboard
    redirect_url = request.session.pop("redirect_after_login", "/admin")
    return RedirectResponse(url=redirect_url, status_code=303)

async def logout(request: Request) -> RedirectResponse:
    """Handle logout."""
    # Delete session from Redis
    session_id = request.session.get("session_id")
    if session_id:
        session_store = request.app.state.session_store
        await session_store.delete(session_id)
    
    # Clear session
    request.session.clear()
    
    return RedirectResponse(url="/", status_code=303)

# Auth routes
auth_routes = [
    Route("/auth/login", login_page, methods=["GET"]),
    Route("/auth/login", login_dev, methods=["POST"]),  # Dev mode only
    Route("/auth/callback", oauth_callback, methods=["GET"]),
    Route("/auth/logout", logout, methods=["POST"]),
]
```

## Task 5: API Authentication

### web/auth/api.py

API key authentication for the bot:

```python
from typing import Optional
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from web.models.admin import APIKey
from web.database import get_db
from shared.utils import utcnow
import structlog

logger = structlog.get_logger()

security = HTTPBearer()

async def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
    db: AsyncSession = Depends(get_db)
) -> APIKey:
    """Verify API key from Bearer token."""
    token = credentials.credentials
    key_hash = APIKey.hash_key(token)
    
    # Look up API key
    result = await db.execute(
        select(APIKey).where(
            APIKey.key_hash == key_hash,
            APIKey.is_active == True
        )
    )
    api_key = result.scalar_one_or_none()
    
    if not api_key:
        logger.warning("Invalid API key attempted", key_hash=key_hash[:8])
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Update last used
    await db.execute(
        update(APIKey)
        .where(APIKey.id == api_key.id)
        .values(last_used=utcnow())
    )
    await db.commit()
    
    return api_key

# Optional: Rate limiting decorator
from functools import wraps
import time
from collections import defaultdict

rate_limit_storage = defaultdict(list)

def rate_limit(max_calls: int = 100, window: int = 60):
    """Rate limit API calls per key."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Get API key from kwargs
            api_key = kwargs.get("api_key")
            if not api_key:
                return await func(*args, **kwargs)
            
            key = str(api_key.id)
            now = time.time()
            
            # Clean old entries
            rate_limit_storage[key] = [
                timestamp for timestamp in rate_limit_storage[key]
                if now - timestamp < window
            ]
            
            # Check rate limit
            if len(rate_limit_storage[key]) >= max_calls:
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit exceeded: {max_calls} calls per {window} seconds"
                )
            
            # Record call
            rate_limit_storage[key].append(now)
            
            return await func(*args, **kwargs)
        
        return wrapper
    return decorator
```

## Task 6: Templates

### web/templates/auth/login.html

Production login page with Discord:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Smarter Dev Admin</title>
    <link rel="stylesheet" href="https://unpkg.com/@tabler/core@latest/dist/css/tabler.min.css">
</head>
<body class="d-flex flex-column">
    <div class="page page-center">
        <div class="container container-tight py-4">
            <div class="text-center mb-4">
                <a href="/" class="navbar-brand navbar-brand-autodark">
                    <h1>Smarter Dev Admin</h1>
                </a>
            </div>
            
            <div class="card card-md">
                <div class="card-body">
                    <h2 class="h2 text-center mb-4">Admin Login</h2>
                    
                    {% if error %}
                    <div class="alert alert-danger" role="alert">
                        {% if error == "unauthorized" %}
                            You are not authorized to access the admin panel.
                        {% elif error == "invalid_state" %}
                            Invalid OAuth state. Please try again.
                        {% elif error == "token_exchange_failed" %}
                            Failed to authenticate with Discord. Please try again.
                        {% else %}
                            An error occurred. Please try again.
                        {% endif %}
                    </div>
                    {% endif %}
                    
                    <div class="text-center">
                        <a href="{{ discord_url }}" class="btn btn-primary w-100">
                            <svg xmlns="http://www.w3.org/2000/svg" class="icon" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none">
                                <path stroke="none" d="M0 0h24v24H0z" fill="none"/>
                                <circle cx="9" cy="12" r="1" />
                                <circle cx="15" cy="12" r="1" />
                                <path d="M7.5 7.5c3.5 -1 5.5 -1 9 0" />
                                <path d="M7 16.5c3.5 1 6.5 1 10 0" />
                                <path d="M15.5 17c0 1 1.5 3 2 3c1.5 0 2.833 -1.667 3.5 -3c.667 -1.667 .5 -5.833 -1.5 -11.5c-1.457 -1.015 -3 -1.34 -4.5 -1.5l-1 2.5" />
                                <path d="M8.5 17c0 1 -1.356 3 -1.832 3c-1.429 0 -2.698 -1.667 -3.333 -3c-.635 -1.667 -.476 -5.833 1.428 -11.5c1.388 -1.015 2.782 -1.34 4.237 -1.5l1 2.5" />
                            </svg>
                            Login with Discord
                        </a>
                    </div>
                    
                    <div class="hr-text">requirements</div>
                    
                    <div class="text-muted text-center">
                        You must be an authorized administrator to access this panel.
                    </div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
```

### web/templates/auth/login_dev.html

Development login page:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dev Login - Smarter Dev Admin</title>
    <link rel="stylesheet" href="https://unpkg.com/@tabler/core@latest/dist/css/tabler.min.css">
</head>
<body class="d-flex flex-column">
    <div class="page page-center">
        <div class="container container-tight py-4">
            <div class="text-center mb-4">
                <a href="/" class="navbar-brand navbar-brand-autodark">
                    <h1>Smarter Dev Admin</h1>
                </a>
                <div class="badge bg-yellow text-yellow-fg">Development Mode</div>
            </div>
            
            <form class="card card-md" method="post" autocomplete="off">
                <div class="card-body">
                    <h2 class="h2 text-center mb-4">Development Login</h2>
                    
                    {% if error %}
                    <div class="alert alert-danger" role="alert">
                        {% if error == "invalid_username" %}
                            Username must be at least 3 characters long.
                        {% else %}
                            Invalid username. Please try again.
                        {% endif %}
                    </div>
                    {% endif %}
                    
                    <div class="mb-3">
                        <label class="form-label">Username</label>
                        <input type="text" name="username" class="form-control" 
                               placeholder="Enter any username" required minlength="3">
                        <small class="form-hint">
                            In development mode, any username works.
                        </small>
                    </div>
                    
                    <div class="form-footer">
                        <button type="submit" class="btn btn-primary w-100">
                            Login
                        </button>
                    </div>
                </div>
            </form>
            
            <div class="text-center text-muted mt-3">
                <div class="alert alert-warning">
                    <strong>Development Mode:</strong> Authentication is simplified for testing.
                    In production, Discord OAuth will be required.
                </div>
            </div>
        </div>
    </div>
</body>
</html>
```

## Task 7: Create Tests

### tests/test_auth.py

```python
import pytest
from httpx import AsyncClient
from starlette.applications import Starlette

@pytest.mark.asyncio
async def test_dev_login(test_client: AsyncClient):
    """Test development mode login."""
    # Get login page
    response = await test_client.get("/auth/login")
    assert response.status_code == 200
    assert "Development Mode" in response.text
    
    # Submit login
    response = await test_client.post(
        "/auth/login",
        data={"username": "testuser"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"

@pytest.mark.asyncio
async def test_dev_login_validation(test_client: AsyncClient):
    """Test username validation."""
    response = await test_client.post(
        "/auth/login",
        data={"username": "ab"}  # Too short
    )
    assert response.status_code == 303
    assert "error=invalid_username" in response.headers["location"]

@pytest.mark.asyncio
async def test_protected_route_redirect(test_client: AsyncClient):
    """Test redirect to login for protected routes."""
    response = await test_client.get("/admin")
    assert response.status_code == 302
    assert response.headers["location"] == "/auth/login"

@pytest.mark.asyncio
async def test_api_key_auth(test_client: AsyncClient, test_api_key: str):
    """Test API key authentication."""
    # Without auth
    response = await test_client.get("/api/guilds")
    assert response.status_code == 403
    
    # With invalid auth
    response = await test_client.get(
        "/api/guilds",
        headers={"Authorization": "Bearer invalid"}
    )
    assert response.status_code == 401
    
    # With valid auth
    response = await test_client.get(
        "/api/guilds",
        headers={"Authorization": f"Bearer {test_api_key}"}
    )
    assert response.status_code == 200
```

## Deliverables

1. **Session Management**
   - Redis-backed sessions
   - Session creation/update/deletion
   - TTL support

2. **Authentication Router**
   - Custom router with auth checking
   - Redirect to login when needed
   - Allow-list for public routes

3. **OAuth2 Implementation**
   - Discord OAuth2 flow
   - Token exchange
   - User info fetching

4. **Authentication Pages**
   - Dual-mode login (dev/production)
   - OAuth callback handling
   - Logout functionality

5. **API Authentication**
   - Bearer token verification
   - API key management
   - Rate limiting

6. **Templates**
   - Professional login pages
   - Error handling
   - Development mode indicators

7. **Test Coverage**
   - Login flow tests
   - Protected route tests
   - API authentication tests

## Important Notes

1. Development mode uses simple username auth
2. Production requires Discord OAuth2
3. Sessions stored in Redis with TTL
4. API keys hashed before storage
5. Rate limiting prevents abuse
6. All auth failures logged

This authentication system provides security while maintaining developer convenience.