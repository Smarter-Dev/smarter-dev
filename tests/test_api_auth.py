import pytest
import os
import sys
import json
from starlette_testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse

# Add the project root to the path so we can import the website package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from website.app import app
from website.api_auth import create_jwt_token, decode_jwt_token

@pytest.fixture
def client():
    """
    Create a test client for the app
    """
    with TestClient(app) as client:
        yield client

def test_api_token_missing_key(client):
    """
    Test that the API token endpoint returns an error when no API key is provided
    """
    response = client.post("/api/auth/token", json={})
    assert response.status_code == 400
    assert response.json()["error"] == "Missing API key"

def test_api_token_invalid_key(client):
    """
    Test that the API token endpoint returns an error when an invalid API key is provided
    """
    response = client.post("/api/auth/token", json={"api_key": "invalid-key"})
    assert response.status_code == 401
    assert response.json()["error"] == "Invalid API key"

def test_api_token_testing_key_local_mode():
    """
    Test that the API token endpoint returns a valid token when SMARTER_DEV_LOCAL=1 and api_key=TESTING
    """
    # Set the environment variable
    os.environ["SMARTER_DEV_LOCAL"] = "1"

    try:
        with TestClient(app) as client:
            response = client.post("/api/auth/token", json={"api_key": "TESTING"})
            assert response.status_code == 200

            # Check that we got a token
            data = response.json()
            assert "token" in data
            assert "expires_in" in data
            assert data["expires_in"] == 3600

            # Verify the token is valid
            token = data["token"]
            payload = decode_jwt_token(token)
            assert payload is not None
            assert payload["sub"] == "999"
            assert payload["name"] == "Testing API Key"
    finally:
        # Clean up the environment variable
        if "SMARTER_DEV_LOCAL" in os.environ:
            del os.environ["SMARTER_DEV_LOCAL"]

def test_api_token_testing_key_not_local_mode():
    """
    Test that the API token endpoint does not accept TESTING key when SMARTER_DEV_LOCAL is not set
    """
    # Make sure the environment variable is not set
    if "SMARTER_DEV_LOCAL" in os.environ:
        del os.environ["SMARTER_DEV_LOCAL"]

    with TestClient(app) as client:
        response = client.post("/api/auth/token", json={"api_key": "TESTING"})
        assert response.status_code == 401
        assert response.json()["error"] == "Invalid API key"
