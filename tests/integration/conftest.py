"""Configuration and fixtures for integration tests."""

from __future__ import annotations

import asyncio
import pytest

# Import fixtures from the web API conftest
from tests.web.test_api.conftest import (
    real_api_client,
    real_db_engine,
    real_db_session,
    api_settings,
    bot_headers,
    test_guild_id,
    test_user_id,
    test_user_id_2,
    test_role_id,
    sample_bytes_balance_data,
    sample_bytes_config_data,
    sample_squad_data,
    sample_transaction_data,
)

# Re-export the fixtures so they're available to integration tests
__all__ = [
    "real_api_client",
    "real_db_engine", 
    "real_db_session",
    "api_settings",
    "bot_headers",
    "test_guild_id",
    "test_user_id",
    "test_user_id_2",
    "test_role_id",
    "sample_bytes_balance_data",
    "sample_bytes_config_data",
    "sample_squad_data",
    "sample_transaction_data",
    "test_isolation",
]


@pytest.fixture(autouse=True)
async def test_isolation():
    """Ensure test isolation by adding delays and cleanup."""
    # Small delay before test
    await asyncio.sleep(0.05)
    
    yield
    
    # Small delay after test to ensure cleanup
    await asyncio.sleep(0.05)