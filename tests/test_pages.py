import pytest
from starlette.testclient import TestClient

def test_home_page(client):
    """
    Test that the home page loads successfully
    """
    response = client.get("/")
    assert response.status_code == 200
    assert "Smarter Dev" in response.text

def test_discord_redirect(client):
    """
    Test that the Discord redirect works
    """
    response = client.get("/discord", allow_redirects=False)
    assert response.status_code == 302  # Redirect status code

def test_custom_redirect(client, test_db):
    """
    Test that a custom redirect works
    """
    response = client.get("/test", allow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com"

def test_nonexistent_redirect(client):
    """
    Test that a nonexistent redirect returns to home
    """
    response = client.get("/nonexistent", allow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"
