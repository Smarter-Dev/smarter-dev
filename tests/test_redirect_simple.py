import pytest
from starlette_testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import RedirectResponse, JSONResponse

# Mock redirect handler
async def mock_redirect_handler(request):
    path = request.path_params["path"]
    
    # Simple mock redirect logic
    if path == "test":
        return RedirectResponse(url="https://example.com", status_code=302)
    elif path == "newtest":
        return RedirectResponse(url="https://newexample.com", status_code=302)
    else:
        return RedirectResponse(url="/", status_code=302)

# Create a test app with the redirect handler
test_app = Starlette(
    debug=True,
    routes=[
        Route("/{path:path}", mock_redirect_handler, methods=["GET"]),
    ]
)

@pytest.fixture
def client():
    """
    Create a test client for the app
    """
    with TestClient(test_app) as client:
        yield client

def test_valid_redirect(client):
    """
    Test that a valid redirect works
    """
    response = client.get("/test", allow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com"

def test_new_redirect(client):
    """
    Test that a new redirect works
    """
    response = client.get("/newtest", allow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://newexample.com"

def test_invalid_redirect(client):
    """
    Test that an invalid redirect returns to home
    """
    response = client.get("/nonexistent", allow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"
