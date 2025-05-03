import pytest
from starlette_testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.responses import HTMLResponse

# Set up templates
templates = Jinja2Templates(directory="website/templates")

# Mock home route
async def mock_home(request):
    return templates.TemplateResponse("index.html", {"request": request})

# Create a test app with the home route and static files
test_app = Starlette(
    debug=True,
    routes=[
        Route("/", mock_home, methods=["GET"]),
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

def test_home_page(client):
    """
    Test that the home page loads successfully
    """
    response = client.get("/")
    assert response.status_code == 200
    assert "Smarter Dev" in response.text
