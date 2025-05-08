from functools import wraps
import traceback
import time
import re
from datetime import datetime
from contextlib import contextmanager
from starlette.requests import Request
from starlette.responses import Response
from sqlalchemy.orm import Session

from .database import get_db, SessionLocal
from .models import PageView, RouteError

# List of common bot/crawler user agent patterns
BOT_PATTERNS = [
    r'bot',
    r'spider',
    r'crawler',
    r'scraper',
    r'yahoo',
    r'slurp',
    r'baiduspider',
    r'googlebot',
    r'yandex',
    r'bingbot',
    r'facebookexternalhit',
    r'linkedinbot',
    r'twitterbot',
    r'slackbot',
    r'telegrambot',
    r'whatsapp',
    r'ahrefsbot',
    r'semrushbot',
    r'pingdom',
    r'uptimerobot',
    r'newrelicpinger',
    r'dataprovider',
    r'screaming frog',
    r'headlesschrome',
    r'phantomjs',
    r'puppeteer',
    r'selenium',
    r'wget',
    r'curl',
    r'python-requests',
    r'python-urllib',
    r'java/',
    r'apache-httpclient',
    r'php/',
    r'go-http-client',
    r'ruby',
    r'perl',
    r'postman',
    r'insomnia',
]

def is_bot(user_agent):
    """
    Check if a user agent string appears to be from a bot/crawler/spider.
    """
    if not user_agent:
        return False

    user_agent = user_agent.lower()

    # Check against known bot patterns
    for pattern in BOT_PATTERNS:
        if re.search(pattern, user_agent, re.IGNORECASE):
            return True

    return False


@contextmanager
def get_db_session():
    """
    Context manager for database sessions to ensure proper cleanup.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def track_page_view(route_func):
    """
    Decorator to track page views for non-admin routes.
    """
    @wraps(route_func)
    async def wrapper(request: Request, *args, **kwargs):
        # Skip tracking for admin routes
        if request.url.path.startswith('/admin'):
            return await route_func(request, *args, **kwargs)

        start_time = time.time()

        try:
            # Call the original route function
            response = await route_func(request, *args, **kwargs)

            # Calculate response time
            response_time = time.time() - start_time

            # Only track successful responses (not redirects to login, etc.)
            if isinstance(response, Response) and 200 <= response.status_code < 300:
                # Get user agent
                user_agent = request.headers.get("user-agent")

                # Check if it's a bot
                is_bot_request = is_bot(user_agent)

                # Use context manager for DB session
                with get_db_session() as db:
                    # Create page view record
                    page_view = PageView(
                        path=request.url.path,
                        method=request.method,
                        ip_address=request.client.host if request.client else None,
                        user_agent=user_agent,
                        referer=request.headers.get("referer"),
                        response_time=response_time,
                        status_code=response.status_code,
                        is_bot=is_bot_request
                    )

                    db.add(page_view)
                    db.commit()

            return response

        except Exception as e:
            # Calculate response time even for errors
            response_time = time.time() - start_time

            # Log the error
            error_details = traceback.format_exc()

            # Use context manager for DB session
            try:
                with get_db_session() as db:
                    # Create error record
                    route_error = RouteError(
                        path=request.url.path,
                        method=request.method,
                        ip_address=request.client.host if request.client else None,
                        user_agent=request.headers.get("user-agent"),
                        error_type=type(e).__name__,
                        error_message=str(e),
                        error_details=error_details,
                        response_time=response_time
                    )

                    db.add(route_error)
                    db.commit()
            except Exception as db_error:
                # Log the database error but don't prevent the original exception from being raised
                print(f"Error logging route error: {db_error}")

            # Re-raise the exception to let the framework handle it
            raise

    return wrapper

def track_middleware(app):
    """
    Middleware to track all page views and errors.
    This is an alternative to using the decorator on each route.
    """
    @wraps(app)
    async def middleware(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)

        # Create a request object
        request = Request(scope)

        # Skip tracking for admin routes and static files
        if request.url.path.startswith('/admin') or request.url.path.startswith('/static'):
            return await app(scope, receive, send)

        start_time = time.time()

        # Create a response tracker
        response_status = {"status_code": None}

        # Intercept the send function to capture the status code
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                response_status["status_code"] = message["status"]
            await send(message)

        try:
            # Call the original app
            await app(scope, receive, send_wrapper)

            # Calculate response time
            response_time = time.time() - start_time

            # Only track successful responses
            if response_status["status_code"] and 200 <= response_status["status_code"] < 300:
                # Get user agent
                user_agent = request.headers.get("user-agent")

                # Check if it's a bot
                is_bot_request = is_bot(user_agent)

                # Use context manager for DB session
                with get_db_session() as db:
                    # Create page view record
                    page_view = PageView(
                        path=request.url.path,
                        method=request.method,
                        ip_address=request.client.host if request.client else None,
                        user_agent=user_agent,
                        referer=request.headers.get("referer"),
                        response_time=response_time,
                        status_code=response_status["status_code"],
                        is_bot=is_bot_request
                    )

                    db.add(page_view)
                    db.commit()

        except Exception as e:
            # Calculate response time even for errors
            response_time = time.time() - start_time

            # Log the error
            error_details = traceback.format_exc()

            # Use context manager for DB session
            try:
                with get_db_session() as db:
                    # Create error record
                    route_error = RouteError(
                        path=request.url.path,
                        method=request.method,
                        ip_address=request.client.host if request.client else None,
                        user_agent=request.headers.get("user-agent"),
                        error_type=type(e).__name__,
                        error_message=str(e),
                        error_details=error_details,
                        response_time=response_time
                    )

                    db.add(route_error)
                    db.commit()
            except Exception as db_error:
                # Log the database error but don't prevent the original exception from being raised
                print(f"Error logging route error: {db_error}")

            # Re-raise the exception to let the framework handle it
            raise

    return middleware
