import uvicorn
import os
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.authentication import AuthenticationMiddleware

from .routes import home, subscribe
from .admin_routes import (
    admin_login, admin_logout, admin_dashboard, admin_redirects,
    admin_new_redirect, admin_edit_redirect, admin_delete_redirect,
    admin_redirect_detail, init_admin
)
from .redirect_handler import handle_redirect
from .auth import AdminAuthBackend, AdminAuthMiddleware
from .database import engine, get_db
from .models import Base

# Create database tables
Base.metadata.create_all(bind=engine)

# Define routes
routes = [
    Route("/", home, methods=["GET"]),
    Route("/api/subscribe", subscribe, methods=["POST"]),

    # Admin routes
    Route("/admin/login", admin_login, methods=["GET", "POST"]),
    Route("/admin/logout", admin_logout, methods=["GET"]),
    Route("/admin", admin_dashboard, methods=["GET"]),
    Route("/admin/redirects", admin_redirects, methods=["GET"]),
    Route("/admin/redirects/new", admin_new_redirect, methods=["GET", "POST"]),
    Route("/admin/redirects/{id:int}", admin_redirect_detail, methods=["GET"]),
    Route("/admin/redirects/{id:int}/edit", admin_edit_redirect, methods=["GET", "POST"]),
    Route("/admin/redirects/{id:int}/delete", admin_delete_redirect, methods=["POST"]),

    # Static files - must be before the catch-all redirect handler
    Mount("/static", app=StaticFiles(directory="website/static"), name="static"),

    # Redirect handler - must be last to catch all other paths
    Route("/{path:path}", handle_redirect, methods=["GET"]),
]

# Middleware
middleware = [
    Middleware(SessionMiddleware, secret_key="smarter-dev-secret-key"),
    Middleware(AuthenticationMiddleware, backend=AdminAuthBackend()),
    Middleware(AdminAuthMiddleware)
]

# Create Starlette application
app = Starlette(
    debug=True,
    routes=routes,
    middleware=middleware
)

# We're using middleware classes instead of this custom middleware function
# The SessionMiddleware and AuthenticationMiddleware are already in the middleware stack

# Initialize admin user if not exists
@app.on_event("startup")
def startup_event():
    # Get DB session
    db = next(get_db())

    # Create admin user with environment variables or defaults
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@smarter.dev")
    admin_password = os.environ.get("ADMIN_PASSWORD", "smarterdev2024")

    init_admin(db, admin_username, admin_email, admin_password)

# Run the application if executed directly
if __name__ == "__main__":
    uvicorn.run("website.app:app", host="0.0.0.0", port=8000, reload=True)
