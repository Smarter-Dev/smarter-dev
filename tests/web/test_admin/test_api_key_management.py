"""Tests for admin interface API key management using TDD approach.

This module defines the expected behavior for admin API key management before implementation.
Tests cover creation, listing, revocation, and security features.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient
from unittest.mock import patch

from smarter_dev.web.models import APIKey


class TestAdminAPIKeyCreation:
    """Test API key creation through admin interface."""
    
    async def test_create_api_key_success(
        self,
        real_api_client: AsyncClient,
        real_db_session,
        admin_auth_headers: dict[str, str]
    ):
        """Test successful API key creation with valid data."""
        key_data = {
            "name": "Test Bot Key",
            "scopes": ["bot:read", "bot:write"],
            "rate_limit_per_hour": 1000,
            "expires_at": None,
            "description": "Test key for automation"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        
        assert response.status_code == 201
        data = response.json()
        
        # Should return the full key only once
        assert "api_key" in data
        assert data["api_key"].startswith("sk-")
        assert len(data["api_key"]) == 46
        
        # Should return key metadata
        assert data["name"] == key_data["name"]
        assert data["scopes"] == key_data["scopes"]
        assert data["rate_limit_per_hour"] == key_data["rate_limit_per_hour"]
        assert data["is_active"] is True
        assert "id" in data
        assert "key_prefix" in data
        assert "created_at" in data
    
    async def test_create_api_key_with_expiration(
        self,
        real_api_client: AsyncClient,
        admin_auth_headers: dict[str, str]
    ):
        """Test API key creation with expiration date."""
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        
        key_data = {
            "name": "Temporary Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 500,
            "expires_at": expires_at
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        
        assert response.status_code == 201
        data = response.json()
        assert data["expires_at"] is not None
    
    async def test_create_api_key_invalid_scopes(
        self,
        real_api_client: AsyncClient,
        admin_auth_headers: dict[str, str]
    ):
        """Test API key creation with invalid scopes."""
        key_data = {
            "name": "Invalid Key",
            "scopes": ["invalid:scope", "bad:permission"],
            "rate_limit_per_hour": 1000
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        
        assert response.status_code == 422
        data = response.json()
        assert "scopes" in data["detail"].lower()
    
    async def test_create_api_key_missing_name(
        self,
        real_api_client: AsyncClient,
        admin_auth_headers: dict[str, str]
    ):
        """Test API key creation without required name field."""
        key_data = {
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 1000
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        
        assert response.status_code == 422
    
    async def test_create_api_key_unauthorized(
        self,
        real_api_client: AsyncClient
    ):
        """Test API key creation without admin authentication."""
        key_data = {
            "name": "Unauthorized Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 1000
        }
        
        response = await real_api_client.post("/admin/api-keys", json=key_data)
        
        assert response.status_code in [401, 403]


class TestAdminAPIKeyListing:
    """Test API key listing through admin interface."""
    
    async def test_list_api_keys_success(
        self,
        real_api_client: AsyncClient,
        real_db_session,
        admin_auth_headers: dict[str, str]
    ):
        """Test successful API key listing."""
        # Create some test keys first
        from smarter_dev.web.security import generate_secure_api_key
        
        for i in range(3):
            full_key, key_hash, key_prefix = generate_secure_api_key()
            api_key = APIKey(
                name=f"Test Key {i+1}",
                key_hash=key_hash,
                key_prefix=key_prefix,
                scopes=["bot:read"],
                rate_limit_per_hour=1000,
                created_by="admin",
                is_active=True
            )
            real_db_session.add(api_key)
        
        await real_db_session.commit()
        
        response = await real_api_client.get(
            "/admin/api-keys",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert "items" in data
        assert "total" in data
        assert "page" in data
        assert "size" in data
        
        # Should have at least the test keys we created
        assert data["total"] >= 3
        assert len(data["items"]) >= 3
        
        # Each key should have safe metadata (no full key or hash)
        for key_item in data["items"]:
            assert "id" in key_item
            assert "name" in key_item
            assert "key_prefix" in key_item
            assert "scopes" in key_item
            assert "is_active" in key_item
            assert "created_at" in key_item
            assert "last_used_at" in key_item
            assert "usage_count" in key_item
            
            # Should NOT contain sensitive data
            assert "key_hash" not in key_item
            assert "api_key" not in key_item
    
    async def test_list_api_keys_pagination(
        self,
        real_api_client: AsyncClient,
        admin_auth_headers: dict[str, str]
    ):
        """Test API key listing with pagination."""
        response = await real_api_client.get(
            "/admin/api-keys?page=1&size=2",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["page"] == 1
        assert data["size"] == 2
        assert len(data["items"]) <= 2
    
    async def test_list_api_keys_filter_active(
        self,
        real_api_client: AsyncClient,
        admin_auth_headers: dict[str, str]
    ):
        """Test API key listing with active status filter."""
        response = await real_api_client.get(
            "/admin/api-keys?active_only=true",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # All returned keys should be active
        for key_item in data["items"]:
            assert key_item["is_active"] is True
    
    async def test_list_api_keys_unauthorized(
        self,
        real_api_client: AsyncClient
    ):
        """Test API key listing without admin authentication."""
        response = await real_api_client.get("/admin/api-keys")
        
        assert response.status_code in [401, 403]


class TestAdminAPIKeyRevocation:
    """Test API key revocation through admin interface."""
    
    async def test_revoke_api_key_success(
        self,
        real_api_client: AsyncClient,
        real_db_session,
        admin_auth_headers: dict[str, str]
    ):
        """Test successful API key revocation."""
        # Create a test key
        from smarter_dev.web.security import generate_secure_api_key
        
        full_key, key_hash, key_prefix = generate_secure_api_key()
        api_key = APIKey(
            name="Key to Revoke",
            key_hash=key_hash,
            key_prefix=key_prefix,
            scopes=["bot:read"],
            rate_limit_per_hour=1000,
            created_by="admin",
            is_active=True
        )
        real_db_session.add(api_key)
        await real_db_session.commit()
        await real_db_session.refresh(api_key)
        
        key_id = str(api_key.id)
        
        response = await real_api_client.delete(
            f"/admin/api-keys/{key_id}",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "API key revoked successfully"
        assert data["key_id"] == key_id
        
        # Verify key is deactivated in database
        await real_db_session.refresh(api_key)
        assert api_key.is_active is False
        assert api_key.revoked_at is not None
    
    async def test_revoke_nonexistent_key(
        self,
        real_api_client: AsyncClient,
        admin_auth_headers: dict[str, str]
    ):
        """Test revocation of non-existent API key."""
        fake_id = "12345678-1234-1234-1234-123456789012"
        
        response = await real_api_client.delete(
            f"/admin/api-keys/{fake_id}",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 404
    
    async def test_revoke_already_revoked_key(
        self,
        real_api_client: AsyncClient,
        real_db_session,
        admin_auth_headers: dict[str, str]
    ):
        """Test revocation of already revoked API key."""
        # Create a revoked test key
        from smarter_dev.web.security import generate_secure_api_key
        
        full_key, key_hash, key_prefix = generate_secure_api_key()
        api_key = APIKey(
            name="Already Revoked Key",
            key_hash=key_hash,
            key_prefix=key_prefix,
            scopes=["bot:read"],
            rate_limit_per_hour=1000,
            created_by="admin",
            is_active=False,
            revoked_at=datetime.now(timezone.utc)
        )
        real_db_session.add(api_key)
        await real_db_session.commit()
        await real_db_session.refresh(api_key)
        
        key_id = str(api_key.id)
        
        response = await real_api_client.delete(
            f"/admin/api-keys/{key_id}",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 409  # Conflict
        data = response.json()
        assert "already revoked" in data["detail"].lower()
    
    async def test_revoke_api_key_unauthorized(
        self,
        real_api_client: AsyncClient
    ):
        """Test API key revocation without admin authentication."""
        fake_id = "12345678-1234-1234-1234-123456789012"
        
        response = await real_api_client.delete(f"/admin/api-keys/{fake_id}")
        
        assert response.status_code in [401, 403]


class TestAdminAPIKeyDetails:
    """Test API key detail viewing through admin interface."""
    
    async def test_get_api_key_details_success(
        self,
        real_api_client: AsyncClient,
        real_db_session,
        admin_auth_headers: dict[str, str]
    ):
        """Test successful API key detail retrieval."""
        # Create a test key with usage
        from smarter_dev.web.security import generate_secure_api_key
        
        full_key, key_hash, key_prefix = generate_secure_api_key()
        api_key = APIKey(
            name="Detailed Key",
            key_hash=key_hash,
            key_prefix=key_prefix,
            scopes=["bot:read", "bot:write"],
            rate_limit_per_hour=1000,
            created_by="admin",
            is_active=True,
            usage_count=42,
            last_used_at=datetime.now(timezone.utc)
        )
        real_db_session.add(api_key)
        await real_db_session.commit()
        await real_db_session.refresh(api_key)
        
        key_id = str(api_key.id)
        
        response = await real_api_client.get(
            f"/admin/api-keys/{key_id}",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["id"] == key_id
        assert data["name"] == "Detailed Key"
        assert data["scopes"] == ["bot:read", "bot:write"]
        assert data["usage_count"] == 42
        assert data["is_active"] is True
        
        # Should NOT contain sensitive data
        assert "key_hash" not in data
        assert "api_key" not in data
    
    async def test_get_api_key_details_not_found(
        self,
        real_api_client: AsyncClient,
        admin_auth_headers: dict[str, str]
    ):
        """Test API key detail retrieval for non-existent key."""
        fake_id = "12345678-1234-1234-1234-123456789012"
        
        response = await real_api_client.get(
            f"/admin/api-keys/{fake_id}",
            headers=admin_auth_headers
        )
        
        assert response.status_code == 404


class TestAdminAPIKeyUpdate:
    """Test API key updating through admin interface."""
    
    async def test_update_api_key_success(
        self,
        real_api_client: AsyncClient,
        real_db_session,
        admin_auth_headers: dict[str, str]
    ):
        """Test successful API key update."""
        # Create a test key
        from smarter_dev.web.security import generate_secure_api_key
        
        full_key, key_hash, key_prefix = generate_secure_api_key()
        api_key = APIKey(
            name="Original Name",
            key_hash=key_hash,
            key_prefix=key_prefix,
            scopes=["bot:read"],
            rate_limit_per_hour=1000,
            created_by="admin",
            is_active=True
        )
        real_db_session.add(api_key)
        await real_db_session.commit()
        await real_db_session.refresh(api_key)
        
        key_id = str(api_key.id)
        
        update_data = {
            "name": "Updated Name",
            "scopes": ["bot:read", "bot:write"],
            "rate_limit_per_hour": 2000,
            "description": "Updated description"
        }
        
        response = await real_api_client.put(
            f"/admin/api-keys/{key_id}",
            json=update_data,
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["name"] == "Updated Name"
        assert data["scopes"] == ["bot:read", "bot:write"]
        assert data["rate_limit_per_hour"] == 2000
    
    async def test_update_api_key_partial(
        self,
        real_api_client: AsyncClient,
        real_db_session,
        admin_auth_headers: dict[str, str]
    ):
        """Test partial API key update."""
        # Create a test key
        from smarter_dev.web.security import generate_secure_api_key
        
        full_key, key_hash, key_prefix = generate_secure_api_key()
        api_key = APIKey(
            name="Original Name",
            key_hash=key_hash,
            key_prefix=key_prefix,
            scopes=["bot:read"],
            rate_limit_per_hour=1000,
            created_by="admin",
            is_active=True
        )
        real_db_session.add(api_key)
        await real_db_session.commit()
        await real_db_session.refresh(api_key)
        
        key_id = str(api_key.id)
        
        # Only update name
        update_data = {
            "name": "Partially Updated Name"
        }
        
        response = await real_api_client.patch(
            f"/admin/api-keys/{key_id}",
            json=update_data,
            headers=admin_auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["name"] == "Partially Updated Name"
        assert data["scopes"] == ["bot:read"]  # Should remain unchanged
        assert data["rate_limit_per_hour"] == 1000  # Should remain unchanged


class TestAdminAPIKeySecurity:
    """Test security aspects of admin API key management."""
    
    async def test_admin_auth_required_for_all_operations(
        self,
        real_api_client: AsyncClient
    ):
        """Test that all admin operations require authentication."""
        endpoints = [
            ("GET", "/admin/api-keys"),
            ("POST", "/admin/api-keys"),
            ("GET", "/admin/api-keys/fake-id"),
            ("PUT", "/admin/api-keys/fake-id"),
            ("PATCH", "/admin/api-keys/fake-id"),
            ("DELETE", "/admin/api-keys/fake-id"),
        ]
        
        for method, endpoint in endpoints:
            response = await real_api_client.request(method, endpoint)
            assert response.status_code in [401, 403], f"{method} {endpoint} should require auth"
    
    async def test_rate_limiting_on_admin_endpoints(
        self,
        real_api_client: AsyncClient,
        admin_auth_headers: dict[str, str]
    ):
        """Test rate limiting on admin endpoints."""
        # This test will be expanded when rate limiting is implemented
        # For now, just verify the endpoint responds
        response = await real_api_client.get(
            "/admin/api-keys",
            headers=admin_auth_headers
        )
        
        assert response.status_code in [200, 429]  # Either success or rate limited
    
    async def test_audit_logging_on_key_operations(
        self,
        real_api_client: AsyncClient,
        admin_auth_headers: dict[str, str]
    ):
        """Test that key operations are logged for audit trail."""
        # This test will be expanded when audit logging is implemented
        # For now, create a key and verify the operation completes
        key_data = {
            "name": "Audit Test Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 1000
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        
        # Verify operation completes (audit logging will be tested separately)
        assert response.status_code == 201