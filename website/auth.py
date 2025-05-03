from passlib.context import CryptContext
from starlette.authentication import AuthCredentials, AuthenticationBackend, SimpleUser
from starlette.requests import Request
from starlette.responses import RedirectResponse

from .database import SessionLocal
from .models import AdminUser

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

# Authentication backend
class AdminAuthBackend(AuthenticationBackend):
    async def authenticate(self, request):
        session = request.session

        if "admin_user_id" not in session:
            return None

        user_id = session["admin_user_id"]

        # Get user from database
        db = SessionLocal()
        try:
            user = db.query(AdminUser).filter(AdminUser.id == user_id, AdminUser.is_active == True).first()
            if user:
                return AuthCredentials(["authenticated"]), SimpleUser(user.username)
        finally:
            db.close()

        return None

# Authentication middleware class
class AdminAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Create a request object to access the path and session
        request = Request(scope)
        path = request.url.path

        # Skip auth for login page and static files
        if path.startswith("/admin/login") or path.startswith("/static"):
            await self.app(scope, receive, send)
            return

        # Check if user is authenticated for admin routes
        if path.startswith("/admin"):
            if "session" in scope and "admin_user_id" not in scope["session"]:
                response = RedirectResponse(url="/admin/login", status_code=302)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)

# Create initial admin user
def create_admin_user(db, username, email, password):
    # Check if user already exists
    user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not user:
        hashed_password = get_password_hash(password)
        user = AdminUser(username=username, email=email, hashed_password=hashed_password)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    return None
