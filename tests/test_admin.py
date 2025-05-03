import pytest
from starlette_testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.authentication import AuthCredentials, AuthenticationBackend, SimpleUser
from datetime import datetime

# Set up templates
templates = Jinja2Templates(directory="website/templates")

# Simple authentication backend for testing
class TestAuthBackend(AuthenticationBackend):
    async def authenticate(self, request):
        # Always authenticate as test user
        return AuthCredentials(["authenticated"]), SimpleUser("testuser")

# Mock admin routes
async def mock_admin_login(request):
    if request.method == "POST":
        form_data = await request.form()
        username = form_data.get("username")
        password = form_data.get("password")

        # Simple mock authentication
        if username == "testadmin" and password == "testpassword":
            return RedirectResponse(url="/admin", status_code=302)
        else:
            return templates.TemplateResponse(
                "admin/login.html",
                {"request": request, "error": "Invalid username or password"}
            )

    return templates.TemplateResponse("admin/login.html", {"request": request})

async def mock_admin_dashboard(request):
    # Mock dashboard data
    stats = {
        "total_redirects": 5,
        "total_clicks": 100,
        "today_clicks": 10,
        "unique_visitors": 50
    }

    chart_data = {
        "dates": ["2023-01-01", "2023-01-02", "2023-01-03"],
        "clicks": [10, 20, 30],
        "top_redirects_names": ["test", "github", "linkedin"],
        "top_redirects_clicks": [50, 30, 20]
    }

    recent_redirects = [
        {"id": 1, "name": "test", "target_url": "https://example.com", "click_count": 50, "created_at": datetime(2023, 1, 1)}
    ]

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "stats": stats,
            "chart_data": chart_data,
            "recent_redirects": recent_redirects
        }
    )

async def mock_admin_redirects(request):
    # Mock redirects data
    redirects = [
        {"id": 1, "name": "test", "target_url": "https://example.com", "click_count": 50, "created_at": datetime(2023, 1, 1), "is_active": True}
    ]

    return templates.TemplateResponse(
        "admin/redirects.html",
        {"request": request, "redirects": redirects}
    )

async def mock_admin_redirect_detail(request):
    # Mock redirect data
    redirect = {
        "id": 1,
        "name": "test",
        "target_url": "https://example.com",
        "description": "Test redirect",
        "created_at": datetime(2023, 1, 1),
        "updated_at": datetime(2023, 1, 1),
        "is_active": True
    }

    stats = {
        "total_clicks": 50,
        "today_clicks": 5,
        "unique_visitors": 30
    }

    chart_data = {
        "dates": ["2023-01-01", "2023-01-02", "2023-01-03"],
        "clicks": [10, 20, 20]
    }

    recent_clicks = [
        {"timestamp": datetime(2023, 1, 3, 12, 30), "ip_address": "127.0.0.1", "user_agent": "Mozilla", "referer": "google.com"}
    ]

    return templates.TemplateResponse(
        "admin/redirect_detail.html",
        {
            "request": request,
            "redirect": redirect,
            "stats": stats,
            "chart_data": chart_data,
            "recent_clicks": recent_clicks
        }
    )

# Create a test app with admin routes and static files
test_app = Starlette(
    debug=True,
    routes=[
        Route("/admin/login", mock_admin_login, methods=["GET", "POST"]),
        Route("/admin", mock_admin_dashboard, methods=["GET"]),
        Route("/admin/redirects", mock_admin_redirects, methods=["GET"]),
        Route("/admin/redirects/{id:int}", mock_admin_redirect_detail, methods=["GET"]),
        Mount("/static", app=StaticFiles(directory="website/static"), name="static"),
    ],
    middleware=[
        Middleware(AuthenticationMiddleware, backend=TestAuthBackend())
    ]
)

@pytest.fixture
def client():
    """
    Create a test client for the app
    """
    with TestClient(test_app) as client:
        yield client

def test_admin_login_page(client):
    """
    Test that the admin login page loads successfully
    """
    response = client.get("/admin/login")
    assert response.status_code == 200
    assert "Admin Login" in response.text

def test_admin_login(client):
    """
    Test that admin login works
    """
    response = client.post(
        "/admin/login",
        data={"username": "testadmin", "password": "testpassword"},
        allow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/admin"

def test_admin_login_invalid(client):
    """
    Test that invalid login credentials are rejected
    """
    response = client.post(
        "/admin/login",
        data={"username": "testadmin", "password": "wrongpassword"},
        allow_redirects=False
    )
    assert response.status_code == 200  # Stays on login page
    assert "Invalid username or password" in response.text

def test_admin_dashboard(client):
    """
    Test that the admin dashboard loads
    """
    response = client.get("/admin")
    assert response.status_code == 200
    assert "Dashboard" in response.text

def test_admin_redirects_page(client):
    """
    Test that the redirects page loads
    """
    response = client.get("/admin/redirects")
    assert response.status_code == 200
    assert "Redirects" in response.text
    assert "test" in response.text  # Our test redirect should be listed

def test_admin_redirect_detail(client):
    """
    Test that the redirect detail page loads
    """
    # Access redirect detail page
    response = client.get("/admin/redirects/1")
    assert response.status_code == 200
    assert "test" in response.text
    assert "https://example.com" in response.text
