from starlette.applications import Starlette
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

# Import FastAPI app
from smarter_dev.web.api.app import api
# Import admin routes
from smarter_dev.web.admin.routes import admin_routes
# Import settings
from smarter_dev.shared.config import get_settings
# Import security headers middleware
from smarter_dev.web.security_headers import create_security_headers_middleware
# Import HTTP methods middleware
from smarter_dev.web.http_methods_middleware import create_http_methods_middleware

templates = Jinja2Templates(directory="templates")

async def homepage(request: Request):
    return templates.TemplateResponse(request, "index.html")

async def discord_redirect(request: Request):
    return RedirectResponse(url="https://discord.gg/de8kajxbYS", status_code=302)

async def not_found(request: Request, exc: HTTPException):
    return templates.TemplateResponse(request, "404.html", status_code=404)

async def server_error(request: Request, exc: HTTPException):
    return templates.TemplateResponse(request, "500.html", status_code=500)

routes = [
    Route("/", homepage),
    Route("/discord", discord_redirect),
]

exception_handlers = {
    404: not_found,
    500: server_error,
}

# Get settings for session secret
settings = get_settings()

# Set up middleware
middleware = [
    # HTTP methods middleware (applied first to handle method validation)
    Middleware(create_http_methods_middleware(starlette_compatible=True)),
    
    # Security headers middleware (applied second for all responses)
    Middleware(create_security_headers_middleware(starlette_compatible=True)),
    
    # Session middleware
    Middleware(
        SessionMiddleware,
        secret_key=settings.web_session_secret,
        max_age=86400 * 7,  # 7 days
        same_site="lax",
        https_only=settings.is_production,
    )
]

app = Starlette(
    routes=routes,
    exception_handlers=exception_handlers,
    middleware=middleware,
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# Mount the FastAPI application at /api
app.mount("/api", api)

# Mount admin interface at /admin
app.mount("/admin", Mount("", routes=admin_routes))

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
