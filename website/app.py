import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware

from .routes import home, discord_redirect, subscribe
from .database import engine
from .models import Base

# Create database tables
Base.metadata.create_all(bind=engine)

# Define routes
routes = [
    Route("/", home, methods=["GET"]),
    Route("/discord", discord_redirect, methods=["GET"]),
    Route("/api/subscribe", subscribe, methods=["POST"]),
    Mount("/static", app=StaticFiles(directory="website/static"), name="static"),
]

# Middleware
middleware = [
    Middleware(SessionMiddleware, secret_key="smarter-dev-secret-key")
]

# Create Starlette application
app = Starlette(
    debug=True,
    routes=routes,
    middleware=middleware
)

# Run the application if executed directly
if __name__ == "__main__":
    uvicorn.run("website.app:app", host="0.0.0.0", port=8000, reload=True)
