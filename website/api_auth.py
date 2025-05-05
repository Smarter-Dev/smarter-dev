import jwt
import time
import os
from datetime import datetime, timedelta
from starlette.authentication import AuthCredentials, AuthenticationBackend, SimpleUser
from starlette.requests import Request
from starlette.responses import JSONResponse
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import APIKey

# JWT settings
JWT_SECRET = "smarter-dev-api-secret-key"  # In production, use environment variable
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION = 3600  # 1 hour in seconds

def create_jwt_token(api_key_id: int, name: str) -> str:
    """
    Create a JWT token for API authentication
    """
    payload = {
        "sub": str(api_key_id),
        "name": name,
        "exp": time.time() + JWT_EXPIRATION,
        "iat": time.time()
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_jwt_token(token: str):
    """
    Decode a JWT token
    """
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None

def verify_api_key(key: str, db: Session) -> APIKey:
    """
    Verify an API key against the database
    """
    api_key = db.query(APIKey).filter(APIKey.key == key, APIKey.is_active == True).first()
    if api_key:
        # Update last used timestamp
        api_key.last_used_at = datetime.now()
        db.commit()
    return api_key

class APIAuthBackend(AuthenticationBackend):
    """
    Authentication backend for API requests
    """
    async def authenticate(self, request):
        # Check for Authorization header
        if "Authorization" not in request.headers:
            return None

        auth = request.headers["Authorization"]

        # Check for Bearer token
        if not auth.startswith("Bearer "):
            return None

        token = auth.replace("Bearer ", "")

        # Decode token
        payload = decode_jwt_token(token)
        if not payload:
            return None

        # Return credentials
        return AuthCredentials(["authenticated", "api"]), SimpleUser(payload["name"])

async def api_auth_middleware(request, call_next):
    """
    Middleware to authenticate API requests
    """
    # Skip auth for non-API routes
    if not request.url.path.startswith("/api/"):
        return await call_next(request)

    # Skip auth for API key validation endpoint
    if request.url.path == "/api/auth/token":
        return await call_next(request)

    # Skip auth for bytes balance endpoint in local development mode
    if request.url.path.startswith("/api/bytes/balance/") and os.environ.get("SMARTER_DEV_LOCAL") == "1":
        print("Skipping auth for bytes balance endpoint in local development mode")
        return await call_next(request)

    # Check for Authorization header
    if "Authorization" not in request.headers:
        return JSONResponse(
            {"error": "Missing Authorization header"},
            status_code=401
        )

    auth = request.headers["Authorization"]

    # Check for Bearer token
    if not auth.startswith("Bearer "):
        return JSONResponse(
            {"error": "Invalid Authorization header format"},
            status_code=401
        )

    token = auth.replace("Bearer ", "")

    # Decode token
    payload = decode_jwt_token(token)
    if not payload:
        return JSONResponse(
            {"error": "Invalid or expired token"},
            status_code=401
        )

    # Continue with the request
    return await call_next(request)

# API key generation
def generate_api_key():
    """
    Generate a random API key
    """
    import secrets
    return secrets.token_urlsafe(32)
