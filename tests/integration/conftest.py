"""Fixtures for integration tests."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# Import fixtures from admin test configuration to maintain compatibility
from tests.web.test_admin.conftest import (
    admin_auth_headers,
    real_api_client,
    real_db_session,
    admin_api_settings,
    real_db_engine
)

# Re-export all necessary fixtures so integration tests can use them
__all__ = [
    'admin_auth_headers',
    'real_api_client', 
    'real_db_session',
    'admin_api_settings',
    'real_db_engine'
]