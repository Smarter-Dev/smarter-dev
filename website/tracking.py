from functools import wraps
import traceback
import time
from datetime import datetime
from starlette.requests import Request
from starlette.responses import Response
from sqlalchemy.orm import Session

from .database import get_db
from .models import PageView, RouteError

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
                # Get DB session
                db = next(get_db())
                
                # Create page view record
                page_view = PageView(
                    path=request.url.path,
                    method=request.method,
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                    referer=request.headers.get("referer"),
                    response_time=response_time,
                    status_code=response.status_code
                )
                
                db.add(page_view)
                db.commit()
            
            return response
            
        except Exception as e:
            # Calculate response time even for errors
            response_time = time.time() - start_time
            
            # Log the error
            error_details = traceback.format_exc()
            
            # Get DB session
            db = next(get_db())
            
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
                # Get DB session
                db = next(get_db())
                
                # Create page view record
                page_view = PageView(
                    path=request.url.path,
                    method=request.method,
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                    referer=request.headers.get("referer"),
                    response_time=response_time,
                    status_code=response_status["status_code"]
                )
                
                db.add(page_view)
                db.commit()
            
        except Exception as e:
            # Calculate response time even for errors
            response_time = time.time() - start_time
            
            # Log the error
            error_details = traceback.format_exc()
            
            # Get DB session
            db = next(get_db())
            
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
            
            # Re-raise the exception to let the framework handle it
            raise
    
    return middleware
