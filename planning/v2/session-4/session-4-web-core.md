# Session 4: Web Application Core

## Objective
Create the main web application structure using Starlette for user-facing pages and a mounted FastAPI sub-application for REST API endpoints. Implement proper error handling, middleware, and resource management.

## Prerequisites
- Completed Session 3 (authentication system exists)
- Understanding of Starlette + FastAPI hybrid architecture
- Database and Redis connections configured

## Task 1: Application Factory

### web/app.py

Create the application factory pattern:

```python
from contextlib import asynccontextmanager
from typing import AsyncGenerator
import redis.asyncio as redis
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
from fastapi import FastAPI
import structlog

from web.config import WebConfig
from web.database import Database
from web.auth.sessions import SessionStore
from web.middleware import LoggingMiddleware, ErrorHandlingMiddleware
from shared.logging import setup_logging

logger = structlog.get_logger()

@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncGenerator:
    """Manage application lifecycle."""
    # Startup
    logger.info("Starting Smarter Dev web application")
    
    # Initialize database
    await app.state.db.connect()
    
    # Initialize Redis
    app.state.redis = redis.from_url(
        app.state.config.redis_url,
        decode_responses=True
    )
    await app.state.redis.ping()
    
    # Initialize session store
    app.state.session_store = SessionStore(app.state.redis)
    
    logger.info("Application started successfully")
    
    yield
    
    # Shutdown
    logger.info("Shutting down application")
    
    await app.state.db.disconnect()
    await app.state.redis.close()
    
    logger.info("Application shut down")

def create_app(config: WebConfig = None) -> Starlette:
    """Create and configure the Starlette application."""
    if config is None:
        config = WebConfig()
    
    # Setup logging
    setup_logging("web", config.log_level, config.dev_mode)
    
    # Create middleware
    middleware = [
        Middleware(
            SessionMiddleware,
            secret_key=config.session_secret.get_secret_value(),
            session_cookie="smarter_session",
            max_age=config.session_lifetime,
            same_site="lax",
            https_only=not config.dev_mode
        ),
        Middleware(LoggingMiddleware),
        Middleware(ErrorHandlingMiddleware, config=config),
    ]
    
    # Create main app
    app = Starlette(
        debug=config.dev_mode,
        middleware=middleware,
        lifespan=lifespan
    )
    
    # Store config and database in app state
    app.state.config = config
    app.state.db = Database(config)
    
    # Mount routes
    from web.routes import setup_routes
    setup_routes(app)
    
    # Mount static files
    if config.serve_static:
        app.mount(
            config.static_url,
            StaticFiles(directory="web/static"),
            name="static"
        )
    
    # Create and mount API
    api = create_api(config)
    app.mount("/api", api)
    
    return app

def create_api(config: WebConfig) -> FastAPI:
    """Create the FastAPI sub-application."""
    # Use same lifespan context
    @asynccontextmanager
    async def api_lifespan(api_app: FastAPI) -> AsyncGenerator:
        # API shares parent app's resources
        yield
    
    api = FastAPI(
        title="Smarter Dev API",
        version="2.0.0",
        docs_url="/docs" if config.dev_mode else None,  # Disable docs in production
        redoc_url="/redoc" if config.dev_mode else None,
        openapi_url="/openapi.json" if config.dev_mode else None,
        lifespan=api_lifespan
    )
    
    # Add CORS for API
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if config.dev_mode else [config.base_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Register API routes
    from web.api import setup_api_routes
    setup_api_routes(api)
    
    return api
```

## Task 2: Middleware

### web/middleware.py

Create custom middleware for logging and error handling:

```python
import time
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException
import structlog
from web.config import WebConfig

logger = structlog.get_logger()

class LoggingMiddleware(BaseHTTPMiddleware):
    """Log all requests with timing information."""
    
    async def dispatch(self, request: Request, call_next):
        # Generate request ID
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        
        # Log request
        logger.info(
            "Request started",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client=request.client.host if request.client else None
        )
        
        # Time the request
        start_time = time.time()
        
        try:
            response = await call_next(request)
            
            # Calculate duration
            duration = time.time() - start_time
            
            # Log response
            logger.info(
                "Request completed",
                request_id=request_id,
                status_code=response.status_code,
                duration=round(duration, 3)
            )
            
            # Add request ID to response headers
            response.headers["X-Request-ID"] = request_id
            
            return response
            
        except Exception as e:
            duration = time.time() - start_time
            
            logger.error(
                "Request failed",
                request_id=request_id,
                error=str(e),
                duration=round(duration, 3),
                exc_info=True
            )
            
            raise

class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """Handle errors appropriately for web vs API."""
    
    def __init__(self, app, config: WebConfig):
        super().__init__(app)
        self.config = config
    
    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
            return response
        except HTTPException as exc:
            # Let HTTPException pass through for proper handling
            raise
        except Exception as exc:
            # Log unexpected errors
            logger.error(
                "Unhandled exception",
                request_id=getattr(request.state, "request_id", "unknown"),
                error=str(exc),
                exc_info=True
            )
            
            # Return 500 error
            if request.url.path.startswith("/api/"):
                # API error response
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "An internal error occurred"
                        }
                    }
                )
            else:
                # HTML error page
                from web.pages.errors import render_error_page
                return await render_error_page(request, 500)
```

## Task 3: Route Setup

### web/routes.py

Organize all routes:

```python
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from web.auth.router import AuthenticatedRouter
from web.pages import public, admin, auth
from web.pages.errors import exception_handlers

def setup_routes(app: Starlette):
    """Configure all application routes."""
    
    # Public routes (no auth required)
    public_routes = [
        Route("/", public.home_page, name="home"),
        Route("/discord", public.discord_redirect, name="discord"),
        Route("/health", public.health_check, name="health"),
    ]
    
    # Auth routes (no auth required)
    auth_routes = auth.auth_routes
    
    # Admin routes (auth required)
    admin_router = AuthenticatedRouter(
        allowed_paths=["/auth/login", "/auth/logout"],
        redirect_url="/auth/login"
    )
    
    # Add admin routes
    admin_router.routes.extend([
        Route("/admin", admin.dashboard, name="admin_dashboard"),
        Route("/admin/guilds", admin.guilds_list, name="admin_guilds"),
        Route("/admin/guilds/{guild_id}", admin.guild_detail, name="admin_guild"),
        Route("/admin/guilds/{guild_id}/bytes", admin.bytes_config, name="admin_bytes"),
        Route("/admin/guilds/{guild_id}/squads", admin.squads_config, name="admin_squads"),
        Route("/admin/guilds/{guild_id}/automod", admin.automod_config, name="admin_automod"),
        Route("/admin/users/{user_id}", admin.user_detail, name="admin_user"),
        Route("/admin/settings", admin.settings, name="admin_settings"),
    ])
    
    # Mount routes
    app.routes.extend(public_routes)
    app.routes.extend(auth_routes)
    app.mount("/admin", admin_router)
    
    # Exception handlers
    for status_code, handler in exception_handlers.items():
        app.add_exception_handler(status_code, handler)
```

## Task 4: Error Pages

### web/pages/errors.py

Create error page handlers:

```python
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates
from starlette.exceptions import HTTPException
import structlog

logger = structlog.get_logger()
templates = Jinja2Templates(directory="web/templates")

async def render_error_page(
    request: Request, 
    status_code: int,
    message: str = None
) -> HTMLResponse:
    """Render an error page."""
    error_messages = {
        404: "Page not found",
        403: "Access forbidden", 
        401: "Authentication required",
        500: "Internal server error",
        503: "Service temporarily unavailable"
    }
    
    context = {
        "request": request,
        "status_code": status_code,
        "message": message or error_messages.get(status_code, "An error occurred")
    }
    
    template_name = f"errors/{status_code}.html"
    
    # Fallback to generic error template
    try:
        return templates.TemplateResponse(
            template_name,
            context,
            status_code=status_code
        )
    except:
        return templates.TemplateResponse(
            "errors/generic.html",
            context,
            status_code=status_code
        )

async def handle_404(request: Request, exc: HTTPException):
    """Handle 404 errors."""
    return await render_error_page(request, 404, exc.detail)

async def handle_403(request: Request, exc: HTTPException):
    """Handle 403 errors."""
    return await render_error_page(request, 403, exc.detail)

async def handle_500(request: Request, exc: Exception):
    """Handle 500 errors."""
    logger.error(
        "Internal server error",
        request_id=getattr(request.state, "request_id", "unknown"),
        path=request.url.path,
        error=str(exc)
    )
    return await render_error_page(request, 500)

# Exception handler mapping
exception_handlers = {
    404: handle_404,
    403: handle_403,
    500: handle_500,
    HTTPException: lambda request, exc: handle_404(request, exc) if exc.status_code == 404 else None
}
```

## Task 5: API Setup

### web/api/__init__.py

Configure API routes:

```python
from fastapi import FastAPI
from web.api.routers import guilds, bytes, squads, moderation, system
from web.api.middleware import APILoggingMiddleware

def setup_api_routes(app: FastAPI):
    """Configure all API routes."""
    
    # Add API-specific middleware
    app.add_middleware(APILoggingMiddleware)
    
    # Include routers
    app.include_router(
        guilds.router,
        prefix="/v1/guilds",
        tags=["guilds"]
    )
    
    app.include_router(
        bytes.router,
        prefix="/v1/bytes", 
        tags=["bytes"]
    )
    
    app.include_router(
        squads.router,
        prefix="/v1/squads",
        tags=["squads"]
    )
    
    app.include_router(
        moderation.router,
        prefix="/v1/moderation",
        tags=["moderation"]
    )
    
    app.include_router(
        system.router,
        prefix="/v1/system",
        tags=["system"]
    )
```

### web/api/dependencies.py

Common API dependencies:

```python
from typing import Annotated
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from web.database import get_db
from web.auth.api import verify_api_key
from web.models.admin import APIKey

async def get_request_db(request: Request) -> AsyncSession:
    """Get database session from request."""
    # Access parent app's database
    async with request.app.state.db.session() as session:
        yield session

async def get_redis(request: Request):
    """Get Redis connection from request."""
    return request.app.state.redis

# Type aliases for cleaner dependencies
CurrentAPIKey = Annotated[APIKey, Depends(verify_api_key)]
DatabaseSession = Annotated[AsyncSession, Depends(get_request_db)]
RedisConnection = Annotated[redis.Redis, Depends(get_redis)]
```

## Task 6: Templates

### web/templates/base.html

Base template for all pages:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Smarter Dev{% endblock %}</title>
    
    <!-- Tabler CSS -->
    <link rel="stylesheet" href="https://unpkg.com/@tabler/core@latest/dist/css/tabler.min.css">
    
    <!-- Custom CSS -->
    <link rel="stylesheet" href="{{ url_for('static', path='/css/custom.css') }}">
    
    {% block extra_css %}{% endblock %}
</head>
<body>
    {% block body %}
    <div class="page">
        {% block header %}
        {% if request.session.get('user') %}
        <header class="navbar navbar-expand-md navbar-dark d-print-none">
            <div class="container-xl">
                <h1 class="navbar-brand navbar-brand-autodark d-none-navbar-horizontal pe-0 pe-md-3">
                    <a href="/admin">Smarter Dev Admin</a>
                </h1>
                
                <div class="navbar-nav flex-row order-md-last">
                    <div class="nav-item dropdown">
                        <a href="#" class="nav-link d-flex lh-1 text-reset p-0" data-bs-toggle="dropdown">
                            {% if request.session.user.is_dev %}
                                <span class="avatar avatar-sm">{{ request.session.user.username[:2].upper() }}</span>
                            {% else %}
                                <span class="avatar avatar-sm" style="background-image: url(https://cdn.discordapp.com/avatars/{{ request.session.user.id }}/{{ request.session.user.avatar }}.png)"></span>
                            {% endif %}
                            <div class="d-none d-xl-block ps-2">
                                <div>{{ request.session.user.username }}</div>
                                <div class="mt-1 small text-muted">Administrator</div>
                            </div>
                        </a>
                        <div class="dropdown-menu dropdown-menu-end dropdown-menu-arrow">
                            <a href="/admin/settings" class="dropdown-item">Settings</a>
                            <div class="dropdown-divider"></div>
                            <form action="/auth/logout" method="post" class="mb-0">
                                <button type="submit" class="dropdown-item">Logout</button>
                            </form>
                        </div>
                    </div>
                </div>
            </div>
        </header>
        {% endif %}
        {% endblock %}
        
        <div class="page-wrapper">
            {% block content %}{% endblock %}
        </div>
    </div>
    {% endblock %}
    
    <!-- Tabler JS -->
    <script src="https://unpkg.com/@tabler/core@latest/dist/js/tabler.min.js"></script>
    
    <!-- Custom JS -->
    <script src="{{ url_for('static', path='/js/app.js') }}"></script>
    
    {% block extra_js %}{% endblock %}
</body>
</html>
```

### web/templates/errors/404.html

404 error page:

```html
{% extends "base.html" %}

{% block title %}Page Not Found - Smarter Dev{% endblock %}

{% block body %}
<div class="page page-center">
    <div class="container-tight py-4">
        <div class="empty">
            <div class="empty-header">404</div>
            <p class="empty-title">Page not found</p>
            <p class="empty-subtitle text-muted">
                {{ message or "The page you were looking for doesn't exist." }}
            </p>
            <div class="empty-action">
                <a href="/" class="btn btn-primary">
                    <svg xmlns="http://www.w3.org/2000/svg" class="icon" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none">
                        <path stroke="none" d="M0 0h24v24H0z" fill="none"/>
                        <line x1="5" y1="12" x2="19" y2="12" />
                        <line x1="5" y1="12" x2="11" y2="18" />
                        <line x1="5" y1="12" x2="11" y2="6" />
                    </svg>
                    Take me home
                </a>
            </div>
        </div>
    </div>
</div>
{% endblock %}
```

### web/templates/errors/500.html

500 error page:

```html
{% extends "base.html" %}

{% block title %}Server Error - Smarter Dev{% endblock %}

{% block body %}
<div class="page page-center">
    <div class="container-tight py-4">
        <div class="empty">
            <div class="empty-header">500</div>
            <p class="empty-title">Internal Server Error</p>
            <p class="empty-subtitle text-muted">
                {{ message or "Something went wrong. We're working on fixing it." }}
            </p>
            <div class="empty-action">
                <a href="/" class="btn btn-primary">
                    <svg xmlns="http://www.w3.org/2000/svg" class="icon" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none">
                        <path stroke="none" d="M0 0h24v24H0z" fill="none"/>
                        <line x1="5" y1="12" x2="19" y2="12" />
                        <line x1="5" y1="12" x2="11" y2="18" />
                        <line x1="5" y1="12" x2="11" y2="6" />
                    </svg>
                    Go back home
                </a>
            </div>
        </div>
    </div>
</div>
{% endblock %}
```

## Task 7: Static Files

### web/static/css/custom.css

Custom styles:

```css
/* Custom styles for Smarter Dev */

:root {
    --smarter-primary: #3b82f6;
    --smarter-accent: #22c55e;
    --smarter-dark: #1a1a1a;
}

/* Development mode indicator */
.dev-mode-banner {
    background: var(--tblr-warning);
    color: var(--tblr-warning-fg);
    text-align: center;
    padding: 0.5rem;
    font-weight: 600;
}

/* Loading states */
.loading-skeleton {
    background: linear-gradient(90deg, #f0f0f0 25%, #e0e0e0 50%, #f0f0f0 75%);
    background-size: 200% 100%;
    animation: loading 1.5s infinite;
}

@keyframes loading {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
}

/* API response formatting */
.api-response {
    background: var(--tblr-dark);
    color: var(--tblr-light);
    padding: 1rem;
    border-radius: var(--tblr-border-radius);
    font-family: var(--tblr-font-monospace);
    font-size: 0.875rem;
    overflow-x: auto;
}

/* Discord-style elements */
.discord-embed {
    border-left: 4px solid var(--smarter-primary);
    background: var(--tblr-gray-100);
    padding: 1rem;
    border-radius: 0 var(--tblr-border-radius) var(--tblr-border-radius) 0;
}

/* Responsive utilities */
@media (max-width: 768px) {
    .hide-mobile {
        display: none !important;
    }
}
```

### web/static/js/app.js

Base JavaScript:

```javascript
// Smarter Dev Admin JavaScript

class SmarterDevApp {
    constructor() {
        this.initializeTheme();
        this.initializeTooltips();
        this.initializeModals();
    }
    
    initializeTheme() {
        // Check for saved theme preference
        const savedTheme = localStorage.getItem('theme') || 'light';
        document.documentElement.setAttribute('data-bs-theme', savedTheme);
    }
    
    initializeTooltips() {
        // Initialize Bootstrap tooltips
        const tooltips = document.querySelectorAll('[data-bs-toggle="tooltip"]');
        tooltips.forEach(el => new bootstrap.Tooltip(el));
    }
    
    initializeModals() {
        // Handle modal events
        document.addEventListener('show.bs.modal', (event) => {
            const modal = event.target;
            const trigger = event.relatedTarget;
            
            if (trigger && trigger.dataset.action) {
                this.handleModalAction(modal, trigger.dataset.action, trigger.dataset);
            }
        });
    }
    
    handleModalAction(modal, action, data) {
        // Handle different modal actions
        switch (action) {
            case 'confirm-delete':
                this.setupDeleteConfirmation(modal, data);
                break;
            // Add more actions as needed
        }
    }
    
    setupDeleteConfirmation(modal, data) {
        const confirmBtn = modal.querySelector('.btn-danger');
        if (confirmBtn) {
            confirmBtn.onclick = () => {
                this.deleteResource(data.url, data.redirect);
            };
        }
    }
    
    async deleteResource(url, redirectUrl) {
        try {
            const response = await fetch(url, {
                method: 'DELETE',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest'
                }
            });
            
            if (response.ok) {
                window.location.href = redirectUrl || '/admin';
            } else {
                this.showError('Failed to delete resource');
            }
        } catch (error) {
            this.showError('Network error occurred');
        }
    }
    
    showError(message) {
        // Show error notification
        const alert = document.createElement('div');
        alert.className = 'alert alert-danger alert-dismissible';
        alert.innerHTML = `
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        `;
        
        document.querySelector('.page-wrapper').prepend(alert);
        
        // Auto-dismiss after 5 seconds
        setTimeout(() => alert.remove(), 5000);
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.smarterDev = new SmarterDevApp();
});
```

## Task 8: Create Tests

### tests/test_web_core.py

```python
import pytest
from httpx import AsyncClient
from starlette.testclient import TestClient

@pytest.mark.asyncio
async def test_app_startup(test_app):
    """Test application starts correctly."""
    # App should have required state
    assert hasattr(test_app.state, "db")
    assert hasattr(test_app.state, "redis")
    assert hasattr(test_app.state, "config")

@pytest.mark.asyncio
async def test_health_endpoint(test_client: AsyncClient):
    """Test health check endpoint."""
    response = await test_client.get("/health")
    assert response.status_code == 200
    
    data = response.json()
    assert data["status"] == "healthy"
    assert "database" in data
    assert "redis" in data

@pytest.mark.asyncio
async def test_static_files(test_client: AsyncClient):
    """Test static file serving."""
    response = await test_client.get("/static/css/custom.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]

@pytest.mark.asyncio
async def test_404_page(test_client: AsyncClient):
    """Test 404 error page."""
    response = await test_client.get("/nonexistent")
    assert response.status_code == 404
    assert "Page not found" in response.text
    assert "404" in response.text

@pytest.mark.asyncio
async def test_api_404_json(test_client: AsyncClient):
    """Test API returns JSON for 404."""
    response = await test_client.get("/api/v1/nonexistent")
    assert response.status_code == 404
    
    data = response.json()
    assert "detail" in data

@pytest.mark.asyncio
async def test_request_id_header(test_client: AsyncClient):
    """Test request ID is added to responses."""
    response = await test_client.get("/health")
    assert "x-request-id" in response.headers
```

## Deliverables

1. **Application Factory**
   - Clean app creation pattern
   - Lifecycle management
   - Configuration injection

2. **Middleware**
   - Request logging with IDs
   - Error handling for web vs API
   - Performance monitoring

3. **Route Organization**
   - Clear separation of concerns
   - Authenticated admin routes
   - Public routes

4. **Error Handling**
   - Beautiful error pages
   - Appropriate responses (HTML vs JSON)
   - Comprehensive logging

5. **Templates**
   - Base template with navigation
   - Error page templates
   - Responsive design

6. **Static Files**
   - Custom CSS
   - Base JavaScript
   - Asset organization

7. **API Structure**
   - Mounted FastAPI app
   - Shared resources
   - OpenAPI docs in dev mode

8. **Test Coverage**
   - Application startup
   - Route testing
   - Error handling

## Important Notes

1. Starlette handles all user-facing routes
2. FastAPI handles all API endpoints
3. Resources shared between apps
4. HTML errors for web, JSON for API
5. Request IDs for tracing
6. Static files only in development

This architecture provides the best user experience while maintaining clean API design.