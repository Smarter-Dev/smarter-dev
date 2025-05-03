import pytest
from starlette_testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.responses import HTMLResponse

# Set up templates
templates = Jinja2Templates(directory="website/templates")

# Mock admin login route
async def mock_admin_login(request):
    if request.method == "POST":
        form_data = await request.form()
        username = form_data.get("username")
        password = form_data.get("password")

        # Simple mock authentication
        if username == "admin" and password == "password":
            return HTMLResponse("Login successful")
        else:
            return templates.TemplateResponse(
                "admin/login.html",
                {"request": request, "error": "Invalid username or password"}
            )

    return templates.TemplateResponse("admin/login.html", {"request": request})

# Create a test app with the login route and static files
test_app = Starlette(
    debug=True,
    routes=[
        Route("/admin/login", mock_admin_login, methods=["GET", "POST"]),
        Mount("/static", app=StaticFiles(directory="website/static"), name="static"),
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

def test_admin_login_success(client):
    """
    Test that admin login works with correct credentials
    """
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "password"}
    )
    assert response.status_code == 200
    assert "Login successful" in response.text

def test_admin_login_failure(client):
    """
    Test that admin login fails with incorrect credentials
    """
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "wrongpassword"}
    )
    assert response.status_code == 200
    assert "Invalid username or password" in response.text
