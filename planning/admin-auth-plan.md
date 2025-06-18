Absolutely! Creating a custom authenticated router is a clean pattern for Starlette. Here's how to implement a secure admin dashboard with proper authentication:

## Custom Authenticated Router Implementation

### Authentication Router Class

```python
# web/auth/router.py
from starlette.routing import Router, Match
from starlette.responses import RedirectResponse
from starlette.exceptions import HTTPException
from functools import wraps

class AuthenticatedRouter(Router):
    """Router that requires authentication for all routes"""
    
    def __init__(self, routes=None, redirect_url="/admin/login", allowed_paths=None):
        super().__init__(routes)
        self.redirect_url = redirect_url
        self.allowed_paths = allowed_paths or []
    
    async def __call__(self, scope, receive, send):
        # Only check auth for HTTP requests
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return
            
        # Check if path is in allowed list (like login/logout)
        path = scope["path"]
        if any(path.startswith(allowed) for allowed in self.allowed_paths):
            await super().__call__(scope, receive, send)
            return
        
        # Check authentication from session
        from starlette.requests import Request
        request = Request(scope, receive)
        
        # Development mode: username auth
        if config.DEV_MODE:
            if "user_id" not in request.session:
                response = RedirectResponse(url=self.redirect_url)
                await response(scope, receive, send)
                return
        else:
            # Production: Check Discord OAuth session
            if "discord_user" not in request.session:
                response = RedirectResponse(url=self.redirect_url)
                await response(scope, receive, send)
                return
            
            # Verify session is still valid
            session_data = await redis.get(f"session:{request.session['discord_user']['id']}")
            if not session_data:
                # Session expired - clear and redirect
                request.session.clear()
                response = RedirectResponse(url=self.redirect_url)
                await response(scope, receive, send)
                return
        
        # User is authenticated, proceed normally
        await super().__call__(scope, receive, send)

# Alternative: Decorator-based approach for individual routes
def require_auth(redirect_to="/admin/login"):
    def decorator(func):
        @wraps(func)
        async def wrapper(request):
            # Check auth
            if config.DEV_MODE:
                if "user_id" not in request.session:
                    return RedirectResponse(url=redirect_to)
            else:
                if "discord_user" not in request.session:
                    return RedirectResponse(url=redirect_to)
                    
                # Verify session validity
                session_data = await redis.get(f"session:{request.session['discord_user']['id']}")
                if not session_data:
                    request.session.clear()
                    return RedirectResponse(url=redirect_to)
            
            # Call original function
            return await func(request)
        return wrapper
    return decorator
```

### Application Setup with Protected Routes

```python
# web/main.py
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.middleware.sessions import SessionMiddleware
from auth.router import AuthenticatedRouter
from pages import admin, auth

app = Starlette(debug=True)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY)

# Public routes (no auth required)
public_routes = [
    Route("/", homepage),
    Route("/discord", discord_redirect),
]

# Auth routes (no auth required)
auth_routes = [
    Route("/admin/login", auth.login_page, methods=["GET", "POST"]),
    Route("/admin/logout", auth.logout, methods=["POST"]),
    Route("/auth/discord/callback", auth.discord_callback),  # OAuth callback
]

# Protected admin routes (auth required)
admin_router = AuthenticatedRouter(
    allowed_paths=["/admin/login", "/admin/logout"],  # Exceptions
    redirect_url="/admin/login"
)

# Add protected routes to the authenticated router
admin_router.routes.extend([
    Route("/admin", admin.dashboard),
    Route("/admin/guilds", admin.guilds_list),
    Route("/admin/guilds/{guild_id}", admin.guild_detail),
    Route("/admin/guilds/{guild_id}/bytes", admin.bytes_config, methods=["GET", "POST"]),
    Route("/admin/guilds/{guild_id}/squads", admin.squads_config, methods=["GET", "POST"]),
    Route("/admin/guilds/{guild_id}/automod", admin.automod_config, methods=["GET", "POST"]),
    Route("/admin/users/{user_id}", admin.user_detail),
    Route("/admin/settings", admin.settings, methods=["GET", "POST"]),
])

# Mount everything
app.routes.extend(public_routes)
app.routes.extend(auth_routes)
app.mount("/admin", admin_router)  # All /admin/* routes require auth except login/logout

# Mount API
app.mount("/api", api)
```

### Authentication Implementation

```python
# web/pages/auth.py
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException
import secrets
import httpx
from urllib.parse import urlencode

async def login_page(request):
    """Login page - different for dev vs production"""
    if request.method == "GET":
        if config.DEV_MODE:
            # Simple username form for development
            return templates.TemplateResponse("admin/login_dev.html", {
                "request": request,
                "error": request.query_params.get("error")
            })
        else:
            # Discord OAuth button for production
            return templates.TemplateResponse("admin/login.html", {
                "request": request,
                "discord_url": get_discord_auth_url(request)
            })
    
    # POST - Development mode only
    if config.DEV_MODE:
        form = await request.form()
        username = form.get("username")
        
        if username and len(username) >= 3:
            # Store in session
            request.session["user_id"] = username
            
            # Redirect to originally requested page or dashboard
            next_url = request.query_params.get("next", "/admin")
            return RedirectResponse(url=next_url, status_code=303)
        
        return templates.TemplateResponse("admin/login_dev.html", {
            "request": request,
            "error": "Username must be at least 3 characters"
        })
    
    # Production doesn't use POST
    raise HTTPException(405)

async def logout(request):
    """Logout - clear session"""
    if "discord_user" in request.session:
        # Clear Redis session
        await redis.delete(f"session:{request.session['discord_user']['id']}")
    
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)

def get_discord_auth_url(request):
    """Generate Discord OAuth URL"""
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    
    params = {
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": f"{config.BASE_URL}/auth/discord/callback",
        "response_type": "code",
        "scope": "identify guilds",
        "state": state
    }
    
    return f"https://discord.com/api/oauth2/authorize?{urlencode(params)}"

async def discord_callback(request):
    """Handle Discord OAuth callback"""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    
    # Verify state
    if not state or state != request.session.get("oauth_state"):
        return RedirectResponse(url="/admin/login?error=invalid_state")
    
    # Exchange code for token
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": config.DISCORD_CLIENT_ID,
                "client_secret": config.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{config.BASE_URL}/auth/discord/callback"
            }
        )
        
        if token_response.status_code != 200:
            return RedirectResponse(url="/admin/login?error=token_exchange_failed")
        
        tokens = token_response.json()
        
        # Get user info
        user_response = await client.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        
        if user_response.status_code != 200:
            return RedirectResponse(url="/admin/login?error=user_fetch_failed")
        
        user_data = user_response.json()
        
        # Check if user is allowed (you might want to check against a whitelist)
        if not await is_user_allowed(user_data["id"]):
            return RedirectResponse(url="/admin/login?error=unauthorized")
        
        # Store in session and Redis
        request.session["discord_user"] = {
            "id": user_data["id"],
            "username": user_data["username"],
            "discriminator": user_data["discriminator"],
            "avatar": user_data["avatar"]
        }
        
        # Store extended session data in Redis with TTL
        await redis.setex(
            f"session:{user_data['id']}", 
            3600 * 24,  # 24 hour TTL
            json.dumps({
                **user_data,
                "tokens": tokens,
                "login_time": datetime.utcnow().isoformat()
            })
        )
        
        # Redirect to dashboard
        return RedirectResponse(url="/admin")

async def is_user_allowed(discord_user_id: str) -> bool:
    """Check if Discord user is allowed to access admin"""
    # Option 1: Hardcoded list
    if discord_user_id in config.ADMIN_DISCORD_IDS:
        return True
    
    # Option 2: Check if user is admin in any guild with the bot
    # This is more complex but more flexible
    guilds = await get_user_admin_guilds(discord_user_id)
    return len(guilds) > 0
```

### Middleware for Request Enhancement

```python
# web/middleware/auth.py
from starlette.middleware.base import BaseHTTPMiddleware

class AuthContextMiddleware(BaseHTTPMiddleware):
    """Add user context to all requests"""
    
    async def dispatch(self, request, call_next):
        # Add user to request object
        request.state.user = None
        
        if config.DEV_MODE:
            if "user_id" in request.session:
                request.state.user = {
                    "id": request.session["user_id"],
                    "username": request.session["user_id"],
                    "is_dev": True
                }
        else:
            if "discord_user" in request.session:
                # Get extended data from Redis
                session_data = await redis.get(f"session:{request.session['discord_user']['id']}")
                if session_data:
                    request.state.user = json.loads(session_data)
        
        response = await call_next(request)
        return response

# Add to main app
app.add_middleware(AuthContextMiddleware)
```

### Templates with User Context

```html
<!-- templates/admin/base.html -->
<!DOCTYPE html>
<html>
<head>
    <title>{% block title %}Smarter Dev Admin{% endblock %}</title>
    <link rel="stylesheet" href="https://unpkg.com/@tabler/core@latest/dist/css/tabler.min.css">
</head>
<body>
    <header class="navbar navbar-expand-md navbar-dark">
        <div class="container-xl">
            <h1 class="navbar-brand">Smarter Dev Admin</h1>
            
            <div class="navbar-nav flex-row order-md-last">
                <div class="nav-item dropdown">
                    <a href="#" class="nav-link d-flex lh-1 text-reset p-0" data-bs-toggle="dropdown">
                        {% if request.state.user.is_dev %}
                            <span class="avatar avatar-sm">{{ request.state.user.username[:2].upper() }}</span>
                        {% else %}
                            <img src="https://cdn.discordapp.com/avatars/{{ request.state.user.id }}/{{ request.state.user.avatar }}.png" 
                                 class="avatar avatar-sm" alt="{{ request.state.user.username }}">
                        {% endif %}
                        <div class="d-none d-xl-block ps-2">
                            <div>{{ request.state.user.username }}</div>
                            <div class="mt-1 small text-muted">
                                {% if request.state.user.is_dev %}Developer{% else %}Admin{% endif %}
                            </div>
                        </div>
                    </a>
                    <div class="dropdown-menu dropdown-menu-end">
                        <a href="/admin/settings" class="dropdown-item">Settings</a>
                        <div class="dropdown-divider"></div>
                        <form action="/admin/logout" method="post">
                            <button type="submit" class="dropdown-item">Logout</button>
                        </form>
                    </div>
                </div>
            </div>
        </div>
    </header>
    
    <div class="page-wrapper">
        {% block content %}{% endblock %}
    </div>
</body>
</html>
```

### Security Enhancements

```python
# web/auth/security.py
import hashlib
import hmac
from datetime import datetime, timedelta

class SessionSecurity:
    """Additional session security measures"""
    
    @staticmethod
    def generate_csrf_token(session_id: str) -> str:
        """Generate CSRF token tied to session"""
        return hmac.new(
            config.SECRET_KEY.encode(),
            f"{session_id}:{datetime.utcnow().date()}".encode(),
            hashlib.sha256
        ).hexdigest()
    
    @staticmethod
    def verify_csrf_token(session_id: str, token: str) -> bool:
        """Verify CSRF token"""
        expected = SessionSecurity.generate_csrf_token(session_id)
        return hmac.compare_digest(expected, token)
    
    @staticmethod
    async def check_session_validity(request) -> bool:
        """Additional session validation checks"""
        if "discord_user" not in request.session:
            return False
        
        # Check session age
        session_data = await redis.get(f"session:{request.session['discord_user']['id']}")
        if not session_data:
            return False
        
        data = json.loads(session_data)
        login_time = datetime.fromisoformat(data["login_time"])
        
        # Force re-login after 7 days
        if datetime.utcnow() - login_time > timedelta(days=7):
            return False
        
        # Check IP address change (optional)
        if config.CHECK_IP_CHANGES:
            stored_ip = data.get("ip_address")
            current_ip = request.client.host
            if stored_ip and stored_ip != current_ip:
                # Log suspicious activity
                await log_security_event("ip_change", request)
                return False
        
        return True

# Add CSRF protection to forms
@require_auth()
async def bytes_config(request):
    if request.method == "POST":
        form = await request.form()
        csrf_token = form.get("csrf_token")
        
        if not SessionSecurity.verify_csrf_token(request.session.get("session_id"), csrf_token):
            raise HTTPException(403, "Invalid CSRF token")
        
        # Process form...
```

This setup provides:

1. **Clean Route Protection**: All `/admin/*` routes are automatically protected except login/logout
2. **Flexible Authentication**: Easy switch between dev mode (username) and production (Discord OAuth)
3. **Session Security**: Redis-backed sessions with TTL, CSRF protection, and IP validation
4. **User Context**: User information available in all templates via `request.state.user`
5. **Graceful Redirects**: Users are redirected to login when needed, then back to their original destination

The `AuthenticatedRouter` pattern keeps your code DRY and makes it easy to add new admin routes without worrying about authentication checks.
