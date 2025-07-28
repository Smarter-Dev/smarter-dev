"""Comprehensive edge case tests for API key system using TDD methodology.

This module tests edge cases, boundary conditions, and error scenarios
for the API key authentication and authorization system to ensure robust
security and reliability under various conditions.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import List
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import APIKey
from smarter_dev.web.crud import APIKeyOperations


class TestAPIKeyEdgeCases:
    """Test edge cases and boundary conditions for API key system."""

    async def test_malformed_api_key_formats(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession
    ):
        """Test handling of various malformed API key formats."""
        malformed_keys = [
            "",  # Empty key
            "invalid",  # Too short
            "sk-" + "x" * 50,  # Too long (correct prefix but too long)
            "wrong_prefix_" + "x" * 40,  # Wrong prefix
            "sk-" + "Z" * 43,  # Invalid characters (uppercase not in base64url)
            "sk-" + "!" * 43,  # Invalid special characters
            "sk-" + "0" * 42,  # One character short (45 total instead of 46)
            "sk-" + "0" * 44,  # One character long (47 total instead of 46)
            None,  # None value (will be handled by FastAPI)
        ]
        
        for malformed_key in malformed_keys:
            if malformed_key is None:
                # Test missing Authorization header
                response = await real_api_client.get(
                    "/guilds/123456789012345678/bytes/balance/987654321098765432"
                )
                assert response.status_code == 403  # No auth header
            else:
                # Test malformed key
                headers = {"Authorization": f"Bearer {malformed_key}"}
                response = await real_api_client.get(
                    "/guilds/123456789012345678/bytes/balance/987654321098765432",
                    headers=headers
                )
                # Empty string and very short keys may return 403 (no proper Bearer format)
                # while malformed but properly formatted keys return 401 (invalid key)
                if malformed_key == "" or len(malformed_key) < 10:
                    assert response.status_code in [401, 403], f"Malformed key should be rejected: '{malformed_key}'"
                else:
                    assert response.status_code == 401, f"Malformed key should be rejected: '{malformed_key}'"
                
                # Verify error message is appropriate (but allow FastAPI default messages)
                error_data = response.json()
                detail_lower = error_data["detail"].lower()
                assert any(keyword in detail_lower for keyword in [
                    "invalid", "format", "unauthorized", "not authenticated", "malformed"
                ]), f"Error message should indicate authentication failure: {error_data['detail']}"

    async def test_authorization_scheme_edge_cases(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession
    ):
        """Test various authorization scheme formats and edge cases."""
        valid_key = "sk-" + "a" * 43  # Valid format but invalid key
        
        auth_schemes = [
            f"bearer {valid_key}",  # Lowercase bearer
            f"BEARER {valid_key}",  # Uppercase bearer
            f"Basic {valid_key}",   # Wrong scheme
            f"Token {valid_key}",   # Wrong scheme
            f"Bearer{valid_key}",   # Missing space
            f"Bearer  {valid_key}", # Extra space
            f" Bearer {valid_key}", # Leading space
            f"Bearer {valid_key} ", # Trailing space
            valid_key,              # No scheme
        ]
        
        for auth_scheme in auth_schemes:
            headers = {"Authorization": auth_scheme}
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=headers
            )
            
            if auth_scheme == f"Bearer {valid_key}":
                # Only exact format should potentially work (though key is invalid)
                assert response.status_code == 401  # Invalid key
            else:
                # All other formats should be rejected for scheme issues
                assert response.status_code in [401, 403], f"Invalid auth scheme should be rejected: {auth_scheme}"

    async def test_concurrent_api_key_operations(
        self,
        real_api_client: AsyncClient, 
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test concurrent API key creation, usage, and deletion."""
        # Create multiple API keys concurrently
        key_creation_tasks = []
        num_keys = 5
        
        for i in range(num_keys):
            key_data = {
                "name": f"Concurrent Test Key {i}",
                "scopes": ["bot:read"],
                "rate_limit_per_hour": 100,
                "description": f"Concurrent test key {i}"
            }
            
            task = real_api_client.post(
                "/admin/api-keys",
                json=key_data,
                headers=admin_auth_headers
            )
            key_creation_tasks.append(task)
        
        # Execute all creations concurrently
        creation_responses = await asyncio.gather(*key_creation_tasks)
        
        # Verify all creations succeeded
        created_keys = []
        for response in creation_responses:
            assert response.status_code == 201
            created_keys.append(response.json())
        
        # Test concurrent usage of the keys
        usage_tasks = []
        for key_data in created_keys:
            api_key = key_data["api_key"]
            headers = {"Authorization": f"Bearer {api_key}"}
            
            task = real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=headers
            )
            usage_tasks.append(task)
        
        # Execute all usages concurrently
        usage_responses = await asyncio.gather(*usage_tasks)
        
        # Verify all usages work (even if returning 404 for non-existent data)
        for response in usage_responses:
            assert response.status_code in [200, 404]
        
        # Test concurrent deletion
        deletion_tasks = []
        for key_data in created_keys:
            key_id = key_data["id"]
            
            task = real_api_client.delete(
                f"/admin/api-keys/{key_id}",
                headers=admin_auth_headers
            )
            deletion_tasks.append(task)
        
        # Execute all deletions concurrently
        deletion_responses = await asyncio.gather(*deletion_tasks)
        
        # Verify all deletions succeeded
        for response in deletion_responses:
            assert response.status_code == 200

    async def test_api_key_expiration_edge_cases(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test API key expiration boundary conditions."""
        # Test key that expires in 1 second
        near_future = datetime.now(timezone.utc) + timedelta(seconds=1)
        
        key_data = {
            "name": "Soon to Expire Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "Key that expires very soon",
            "expires_at": near_future.isoformat()
        }
        
        # Create the key
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 201
        api_key = response.json()["api_key"]
        
        # Use the key immediately (should work)
        headers = {"Authorization": f"Bearer {api_key}"}
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        assert response.status_code in [200, 404]  # Should work
        
        # Wait for expiration
        await asyncio.sleep(2)
        
        # Try to use expired key
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        assert response.status_code == 401  # Should be expired
        
        error_data = response.json()
        assert "expired" in error_data["detail"].lower()

    async def test_api_key_scope_boundary_conditions(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test API key scope validation edge cases."""
        # Test with empty scopes
        key_data = {
            "name": "Empty Scopes Key",
            "scopes": [],
            "rate_limit_per_hour": 100,
            "description": "Key with no scopes"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        # Should either reject empty scopes or accept them
        assert response.status_code in [201, 400]
        
        # Test with invalid scope format
        invalid_scope_data = {
            "name": "Invalid Scopes Key",
            "scopes": ["invalid_scope_format", "another:invalid:scope"],
            "rate_limit_per_hour": 100,
            "description": "Key with invalid scopes"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=invalid_scope_data,
            headers=admin_auth_headers
        )
        # Should validate scope format
        assert response.status_code in [201, 400]
        
        # Test with maximum number of scopes
        max_scopes_data = {
            "name": "Max Scopes Key",
            "scopes": [f"bot:action_{i}" for i in range(50)],  # Large number of scopes
            "rate_limit_per_hour": 100,
            "description": "Key with many scopes"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=max_scopes_data,
            headers=admin_auth_headers
        )
        # Should handle large scope lists
        assert response.status_code in [201, 400]

    async def test_rate_limit_boundary_conditions(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test rate limiting edge cases and boundary values."""
        # Test with rate limit of 0
        zero_limit_data = {
            "name": "Zero Rate Limit Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 0,
            "description": "Key with zero rate limit"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=zero_limit_data,
            headers=admin_auth_headers
        )
        
        if response.status_code == 201:
            api_key = response.json()["api_key"]
            headers = {"Authorization": f"Bearer {api_key}"}
            
            # First request should be rate limited immediately
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=headers
            )
            assert response.status_code == 429  # Should be rate limited
        
        # Test with extremely high rate limit
        high_limit_data = {
            "name": "High Rate Limit Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 1000000,  # Very high limit
            "description": "Key with very high rate limit"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=high_limit_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 201
        
        # Test with negative rate limit
        negative_limit_data = {
            "name": "Negative Rate Limit Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": -1,
            "description": "Key with negative rate limit"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=negative_limit_data,
            headers=admin_auth_headers
        )
        # Should reject negative rate limits
        assert response.status_code == 400

    async def test_api_key_name_and_description_edge_cases(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test API key name and description validation edge cases."""
        # Test with empty name
        empty_name_data = {
            "name": "",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "Key with empty name"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=empty_name_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 422  # Should reject empty name (validation error)
        
        # Test with very long name
        long_name_data = {
            "name": "A" * 300,  # Very long name
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "Key with very long name"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=long_name_data,
            headers=admin_auth_headers
        )
        # Should handle long names appropriately
        assert response.status_code in [201, 422]  # 422 for validation errors
        
        # Test with special characters in name
        special_chars_data = {
            "name": "Key with 特殊字符 and émojis 🔑",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "Key with unicode characters"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=special_chars_data,
            headers=admin_auth_headers
        )
        # Should handle unicode characters
        assert response.status_code == 201
        
        # Test with SQL injection attempt in name
        sql_injection_data = {
            "name": "'; DROP TABLE api_keys; --",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "SQL injection test"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=sql_injection_data,
            headers=admin_auth_headers
        )
        # Should safely handle potential SQL injection
        assert response.status_code == 201

    async def test_database_constraint_violations(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test handling of database constraint violations."""
        # Create a key first
        key_data = {
            "name": "Constraint Test Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "Key for testing constraints"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 201
        
        # Try to create another key with the same name
        duplicate_response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        # Should either allow duplicate names or reject appropriately
        assert duplicate_response.status_code in [201, 400, 409]

    async def test_api_key_deletion_edge_cases(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test edge cases in API key deletion."""
        # Try to delete non-existent key
        fake_uuid = str(uuid4())
        response = await real_api_client.delete(
            f"/admin/api-keys/{fake_uuid}",
            headers=admin_auth_headers
        )
        assert response.status_code == 404
        
        # Try to delete key with invalid UUID format
        response = await real_api_client.delete(
            "/admin/api-keys/invalid-uuid",
            headers=admin_auth_headers
        )
        assert response.status_code == 400  # Invalid UUID format
        
        # Create and delete key, then try to delete again
        key_data = {
            "name": "Delete Test Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "Key for deletion testing"
        }
        
        create_response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert create_response.status_code == 201
        key_id = create_response.json()["id"]
        
        # Delete the key
        delete_response = await real_api_client.delete(
            f"/admin/api-keys/{key_id}",
            headers=admin_auth_headers
        )
        assert delete_response.status_code == 200
        
        # Try to delete again
        double_delete_response = await real_api_client.delete(
            f"/admin/api-keys/{key_id}",
            headers=admin_auth_headers
        )
        assert double_delete_response.status_code == 409  # Already deleted

    async def test_api_key_usage_after_deletion(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that deleted API keys cannot be used."""
        # Create a key
        key_data = {
            "name": "Usage After Delete Test",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "Test key usage after deletion"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 201
        created_key = response.json()
        api_key = created_key["api_key"]
        key_id = created_key["id"]
        
        # Use the key (should work)
        headers = {"Authorization": f"Bearer {api_key}"}
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        assert response.status_code in [200, 404]
        
        # Delete the key
        delete_response = await real_api_client.delete(
            f"/admin/api-keys/{key_id}",
            headers=admin_auth_headers
        )
        assert delete_response.status_code == 200
        
        # Try to use the deleted key
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=headers
        )
        assert response.status_code == 401  # Should be unauthorized
        
        error_data = response.json()
        assert "Invalid" in error_data["detail"] or "revoked" in error_data["detail"]

    async def test_high_frequency_requests(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test system behavior under high-frequency API requests."""
        # Create a key with high rate limit
        key_data = {
            "name": "High Frequency Test Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 1000,
            "description": "Key for high frequency testing"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 201
        api_key = response.json()["api_key"]
        
        headers = {"Authorization": f"Bearer {api_key}"}
        
        # Make many concurrent requests
        request_tasks = []
        num_requests = 50
        
        for i in range(num_requests):
            task = real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=headers
            )
            request_tasks.append(task)
        
        # Execute all requests concurrently
        responses = await asyncio.gather(*request_tasks, return_exceptions=True)
        
        # Count successful responses
        successful_responses = 0
        rate_limited_responses = 0
        
        for response in responses:
            if isinstance(response, Exception):
                continue  # Skip exceptions
                
            if response.status_code in [200, 404]:
                successful_responses += 1
            elif response.status_code == 429:
                rate_limited_responses += 1
        
        # Should handle high frequency requests gracefully
        assert successful_responses > 0  # Some should succeed
        assert successful_responses + rate_limited_responses >= num_requests * 0.8  # Most should be handled

    async def test_invalid_json_payloads(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test handling of invalid JSON payloads in API key creation."""
        # Test with malformed JSON
        response = await real_api_client.post(
            "/admin/api-keys",
            content='{"name": "test", "scopes": ["bot:read"], "rate_limit_per_hour": 100, "description": "test"',  # Missing closing brace
            headers={**admin_auth_headers, "Content-Type": "application/json"}
        )
        assert response.status_code == 400  # Bad request
        
        # Test with missing required fields
        incomplete_data = {
            "name": "Incomplete Key"
            # Missing scopes and rate_limit_per_hour
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=incomplete_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 400  # Validation error
        
        # Test with wrong data types
        wrong_types_data = {
            "name": 123,  # Should be string
            "scopes": "not_a_list",  # Should be list
            "rate_limit_per_hour": "not_a_number",  # Should be integer
            "description": ["not", "a", "string"]  # Should be string
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=wrong_types_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 400  # Validation error

    async def test_memory_and_resource_limits(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test system behavior with large payloads and resource constraints."""
        # Test with very large description
        large_description = "A" * 10000  # 10KB description
        
        large_payload_data = {
            "name": "Large Payload Test Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": large_description
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=large_payload_data,
            headers=admin_auth_headers
        )
        # Should handle large payloads appropriately
        assert response.status_code in [201, 400, 413]  # Created, bad request, or payload too large
        
        # Test pagination limits
        response = await real_api_client.get(
            "/admin/api-keys?page=1&size=1000",  # Very large page size
            headers=admin_auth_headers
        )
        # Should enforce reasonable pagination limits
        assert response.status_code in [200, 400]
        
        if response.status_code == 200:
            data = response.json()
            # Should limit the actual number of results returned
            assert len(data.get("keys", [])) <= 100  # Reasonable limit