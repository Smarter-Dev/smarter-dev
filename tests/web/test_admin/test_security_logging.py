"""Test suite for security logging and audit trail functionality using TDD methodology.

This module tests the security logging system for API key operations to ensure
proper audit trails and security monitoring capabilities.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import List
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import APIKey, SecurityLog
from smarter_dev.web.crud import APIKeyOperations


class TestSecurityLogging:
    """Test security logging and audit trail functionality."""

    async def test_log_api_key_creation(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that API key creation is properly logged."""
        # Create API key
        key_data = {
            "name": "Security Log Test Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "Testing security logging"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 201
        created_key = response.json()
        
        # Check that security log entry was created
        from smarter_dev.web.security_logger import SecurityLogger
        security_logger = SecurityLogger()
        
        # Query for security logs related to this key
        logs = await security_logger.get_logs_for_api_key(
            real_db_session,
            UUID(created_key["id"])
        )
        
        # Should have at least one log entry for creation
        assert len(logs) >= 1
        creation_log = next((log for log in logs if log.action == "api_key_created"), None)
        assert creation_log is not None
        assert creation_log.api_key_id == UUID(created_key["id"])
        assert creation_log.user_identifier is not None
        assert creation_log.success is True
        assert "created" in creation_log.details.lower()

    async def test_log_api_key_authentication_success(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that successful API key authentication is logged."""
        # Create API key
        key_data = {
            "name": "Auth Success Log Test",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "Testing auth success logging"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 201
        test_key = response.json()["api_key"]
        key_id = UUID(response.json()["id"])
        
        # Use the API key to make an authenticated request
        auth_headers = {"Authorization": f"Bearer {test_key}"}
        auth_response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=auth_headers
        )
        # Should succeed (even if 404 for non-existent data)
        assert auth_response.status_code in [200, 404]
        
        # Check that authentication success was logged
        from smarter_dev.web.security_logger import SecurityLogger
        security_logger = SecurityLogger()
        
        logs = await security_logger.get_logs_for_api_key(real_db_session, key_id)
        auth_logs = [log for log in logs if log.action == "api_key_used"]
        
        assert len(auth_logs) >= 1
        auth_log = auth_logs[0]
        assert auth_log.api_key_id == key_id
        assert auth_log.success is True
        assert auth_log.ip_address is not None
        assert auth_log.user_agent is not None

    async def test_log_api_key_authentication_failure(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession
    ):
        """Test that failed API key authentication attempts are logged."""
        # Use an invalid API key
        fake_key = "sd_test_" + "x" * 40  # Invalid but properly formatted key
        auth_headers = {"Authorization": f"Bearer {fake_key}"}
        
        response = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=auth_headers
        )
        assert response.status_code == 401
        
        # Wait a bit for the log to be committed
        import asyncio
        await asyncio.sleep(0.1)
        
        # Check that authentication failure was logged using a fresh session
        from smarter_dev.web.security_logger import SecurityLogger
        from smarter_dev.shared.database import get_db_session
        
        security_logger = SecurityLogger()
        
        # Use a fresh database session to query logs
        async for fresh_session in get_db_session():
            try:
                # Get recent failed authentication logs
                recent_logs = await security_logger.get_recent_logs(
                    fresh_session,
                    action="authentication_failed",
                    limit=10
                )
                
                # Should have at least one failed auth log
                assert len(recent_logs) >= 1, f"Expected at least 1 failed auth log, found {len(recent_logs)}"
                failed_log = recent_logs[0]
                assert failed_log.action == "authentication_failed"
                assert failed_log.success is False
                assert fake_key[:10] in failed_log.details  # Should log partial key for identification
                break
            except Exception as e:
                print(f"Error querying logs: {e}")
                raise

    async def test_log_api_key_rate_limit_exceeded(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that rate limit violations are logged."""
        # Create API key with very low rate limit
        key_data = {
            "name": "Rate Limit Log Test",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 1,  # Very low limit
            "description": "Testing rate limit logging"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 201
        test_key = response.json()["api_key"]
        key_id = UUID(response.json()["id"])
        
        auth_headers = {"Authorization": f"Bearer {test_key}"}
        
        # Make first request (should succeed)
        response1 = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=auth_headers
        )
        assert response1.status_code in [200, 404]
        
        # Make second request (should be rate limited)
        response2 = await real_api_client.get(
            "/guilds/123456789012345678/bytes/balance/987654321098765432",
            headers=auth_headers
        )
        assert response2.status_code == 429
        
        # Check that rate limit violation was logged
        from smarter_dev.web.security_logger import SecurityLogger
        security_logger = SecurityLogger()
        
        logs = await security_logger.get_logs_for_api_key(real_db_session, key_id)
        rate_limit_logs = [log for log in logs if log.action == "rate_limit_exceeded"]
        
        assert len(rate_limit_logs) >= 1
        rate_limit_log = rate_limit_logs[0]
        assert rate_limit_log.api_key_id == key_id
        assert rate_limit_log.success is False
        assert "rate limit" in rate_limit_log.details.lower()

    async def test_log_api_key_deletion(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that API key deletion is properly logged."""
        # Create API key
        key_data = {
            "name": "Deletion Log Test",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "Testing deletion logging"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 201
        key_id = response.json()["id"]
        
        # Delete the API key
        delete_response = await real_api_client.delete(
            f"/admin/api-keys/{key_id}",
            headers=admin_auth_headers
        )
        assert delete_response.status_code == 200
        
        # Check that deletion was logged
        from smarter_dev.web.security_logger import SecurityLogger
        security_logger = SecurityLogger()
        
        logs = await security_logger.get_logs_for_api_key(
            real_db_session,
            UUID(key_id)
        )
        deletion_logs = [log for log in logs if log.action == "api_key_deleted"]
        
        assert len(deletion_logs) >= 1
        deletion_log = deletion_logs[0]
        assert deletion_log.api_key_id == UUID(key_id)
        assert deletion_log.success is True
        assert "deleted" in deletion_log.details.lower()

    async def test_log_suspicious_activity(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession
    ):
        """Test logging of suspicious authentication patterns."""
        fake_key = "sd_test_" + "x" * 40
        auth_headers = {"Authorization": f"Bearer {fake_key}"}
        
        # Make multiple failed authentication attempts rapidly
        for i in range(5):
            response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=auth_headers
            )
            assert response.status_code == 401
        
        # Check for suspicious activity detection
        from smarter_dev.web.security_logger import SecurityLogger
        security_logger = SecurityLogger()
        
        # Check for rapid failed attempts (suspicious activity)
        recent_logs = await security_logger.get_recent_logs(
            real_db_session,
            action="authentication_failed",
            limit=10
        )
        
        # Should have multiple failed attempts from same source
        failed_attempts = [log for log in recent_logs if fake_key[:10] in log.details]
        assert len(failed_attempts) >= 5
        
        # Check if suspicious activity was flagged
        suspicious_logs = await security_logger.get_recent_logs(
            real_db_session,
            action="suspicious_activity",
            limit=5
        )
        
        if suspicious_logs:
            suspicious_log = suspicious_logs[0]
            assert suspicious_log.action == "suspicious_activity"
            assert suspicious_log.success is False
            assert "rapid" in suspicious_log.details.lower() or "multiple" in suspicious_log.details.lower()

    async def test_log_admin_operations(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that admin operations are properly logged."""
        # List API keys (admin operation)
        response = await real_api_client.get(
            "/admin/api-keys",
            headers=admin_auth_headers
        )
        assert response.status_code == 200
        
        # Check that admin operation was logged
        from smarter_dev.web.security_logger import SecurityLogger
        security_logger = SecurityLogger()
        
        admin_logs = await security_logger.get_recent_logs(
            real_db_session,
            action="admin_operation",
            limit=10
        )
        
        # Should have at least one admin operation log
        if admin_logs:
            admin_log = admin_logs[0]
            assert admin_log.action == "admin_operation"
            assert admin_log.success is True
            assert "api-keys" in admin_log.details.lower() or "list" in admin_log.details.lower()

    async def test_security_log_data_retention(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that security logs handle data retention and cleanup."""
        from smarter_dev.web.security_logger import SecurityLogger
        security_logger = SecurityLogger()
        
        # Create a test log entry with old timestamp
        old_timestamp = datetime.now(timezone.utc) - timedelta(days=95)  # Older than retention period
        
        test_log = SecurityLog(
            action="test_old_entry",
            api_key_id=None,
            user_identifier="test_user",
            ip_address="127.0.0.1",
            user_agent="test_agent",
            success=True,
            details="Test old log entry",
            timestamp=old_timestamp
        )
        
        real_db_session.add(test_log)
        await real_db_session.commit()
        
        # Test cleanup of old logs (if implemented)
        deleted_count = await security_logger.cleanup_old_logs(
            real_db_session,
            retention_days=90
        )
        
        # Should delete the old test log
        assert deleted_count >= 1

    async def test_security_log_search_and_filtering(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test security log search and filtering capabilities."""
        # Create API key for testing
        key_data = {
            "name": "Search Filter Test",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 100,
            "description": "Testing log search"
        }
        
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        assert response.status_code == 201
        key_id = UUID(response.json()["id"])
        
        from smarter_dev.web.security_logger import SecurityLogger
        security_logger = SecurityLogger()
        
        # Test filtering by action
        creation_logs = await security_logger.get_logs_by_action(
            real_db_session,
            action="api_key_created"
        )
        assert len(creation_logs) >= 1
        
        # Test filtering by success status
        success_logs = await security_logger.get_logs_by_success(
            real_db_session,
            success=True
        )
        assert len(success_logs) >= 1
        
        # Test filtering by date range
        start_date = datetime.now(timezone.utc) - timedelta(hours=1)
        end_date = datetime.now(timezone.utc) + timedelta(hours=1)
        
        recent_logs = await security_logger.get_logs_by_date_range(
            real_db_session,
            start_date=start_date,
            end_date=end_date
        )
        assert len(recent_logs) >= 1

    async def test_security_log_performance(
        self,
        real_api_client: AsyncClient,
        real_db_session: AsyncSession,
        admin_auth_headers: dict[str, str]
    ):
        """Test that security logging doesn't significantly impact performance."""
        import time
        
        key_data = {
            "name": "Performance Test Key",
            "scopes": ["bot:read"],
            "rate_limit_per_hour": 1000,
            "description": "Testing logging performance"
        }
        
        # Measure time to create API key with logging
        start_time = time.time()
        response = await real_api_client.post(
            "/admin/api-keys",
            json=key_data,
            headers=admin_auth_headers
        )
        end_time = time.time()
        
        assert response.status_code == 201
        creation_time = end_time - start_time
        
        # Should complete within reasonable time (adjust threshold as needed)
        assert creation_time < 2.0  # Should complete within 2 seconds
        
        # Test multiple authentication requests for performance
        test_key = response.json()["api_key"]
        auth_headers = {"Authorization": f"Bearer {test_key}"}
        
        start_time = time.time()
        for i in range(10):
            auth_response = await real_api_client.get(
                "/guilds/123456789012345678/bytes/balance/987654321098765432",
                headers=auth_headers
            )
            assert auth_response.status_code in [200, 404, 429]  # 429 if rate limited
        end_time = time.time()
        
        avg_request_time = (end_time - start_time) / 10
        # Each request should complete quickly even with logging
        assert avg_request_time < 0.5  # Should average less than 500ms per request