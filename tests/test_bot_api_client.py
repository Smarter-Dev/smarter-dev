"""
Tests for the API client.
"""

import os
import sys
import time
import json
import pytest
import httpx
from unittest.mock import patch, MagicMock, AsyncMock

# Add the project root to the path so we can import the bot package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.api_client import APIClient, TokenResponse

# Base URL for testing
BASE_URL = "http://localhost:8000"
API_KEY = "TESTING"

@pytest.mark.asyncio
async def test_get_token():
    """Test getting a token"""
    client = APIClient(BASE_URL, API_KEY)
    
    # Create a proper AsyncMock for the response
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.text = "{'token': 'test-token', 'expires_in': 3600}"
    mock_response.json = AsyncMock(return_value={
        "token": "test-token",
        "expires_in": 3600
    })
    
    try:
        # Mock the post method
        with patch.object(client.client, 'post', new=AsyncMock(return_value=mock_response)):
            token = await client.get_token()
            
            # Check that we got a token
            assert token.token == "test-token"
            assert token.expires_in == 3600
            assert token.expires_at > time.time()
            
            # Check that the token is cached
            assert client.token is token
    finally:
        await client.close()

@pytest.mark.asyncio
async def test_token_refresh():
    """Test that tokens are refreshed when expired"""
    client = APIClient(BASE_URL, API_KEY)
    
    # Create proper AsyncMocks for the responses
    mock_response1 = AsyncMock()
    mock_response1.status_code = 200
    mock_response1.text = "{'token': 'test-token-1', 'expires_in': 3600}"
    mock_response1.json = AsyncMock(return_value={
        "token": "test-token-1",
        "expires_in": 3600
    })
    
    mock_response2 = AsyncMock()
    mock_response2.status_code = 200
    mock_response2.text = "{'token': 'test-token-2', 'expires_in': 3600}"
    mock_response2.json = AsyncMock(return_value={
        "token": "test-token-2",
        "expires_in": 3600
    })
    
    try:
        # Mock the post method for the first call
        with patch.object(client.client, 'post', new=AsyncMock(return_value=mock_response1)):
            token1 = await client.get_token()
            assert token1.token == "test-token-1"
        
        # Manually expire the token
        client.token.expires_at = time.time() - 1
        
        # Mock the post method for the second call
        with patch.object(client.client, 'post', new=AsyncMock(return_value=mock_response2)):
            token2 = await client.get_token()
            assert token2.token == "test-token-2"
        
        # Check that we got a new token
        assert token2.token != token1.token
    finally:
        await client.close()

@pytest.mark.asyncio
async def test_request_with_token():
    """Test making a request with a token"""
    client = APIClient(BASE_URL, API_KEY)
    try:
        # Create a proper AsyncMock for the token response
        token_response = AsyncMock()
        token_response.status_code = 200
        token_response.text = "{'token': 'test-token', 'expires_in': 3600}"
        token_response.json = AsyncMock(return_value={
            "token": "test-token",
            "expires_in": 3600
        })
        
        # Create a proper AsyncMock for the API response
        api_response = AsyncMock()
        api_response.status_code = 200
        api_response.text = "{'success': true}"
        api_response.json = AsyncMock(return_value={"success": True})
        
        # First mock the token request
        with patch.object(client.client, 'post', new=AsyncMock(return_value=token_response)):
            # Then mock the API request
            with patch.object(client.client, 'request', new=AsyncMock(return_value=api_response)):
                response = await client._request("GET", "/api/test")
                
                # Check that the request was made with the token
                assert response.status_code == 200
                response_data = await client._get_json(response)
                assert response_data == {"success": True}
    finally:
        await client.close()
