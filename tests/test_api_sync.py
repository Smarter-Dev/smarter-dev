"""
Tests for the API synchronizer.

This module contains tests for the API synchronization functionality.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

# Add the project root to the path so we can import the bot package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.api_sync import APISynchronizer, create_synchronizer


class TestAPISynchronizer(unittest.TestCase):
    """Test cases for the API synchronizer."""

    def setUp(self):
        """Set up test fixtures."""
        # Create a mock API client
        self.mock_api_client = MagicMock()
        self.mock_api_client.get_guilds = AsyncMock(return_value=[])
        self.mock_api_client.get_users = AsyncMock(return_value=[])
        self.mock_api_client.create_guild = AsyncMock()
        self.mock_api_client.update_guild = AsyncMock()
        self.mock_api_client.create_user = AsyncMock()
        self.mock_api_client.update_user = AsyncMock()
        self.mock_api_client.close = AsyncMock()
        
        # Create a synchronizer with the mock client
        self.sync = APISynchronizer("http://test", "test_key")
        self.sync.api_client = self.mock_api_client
    
    @patch('bot.api_sync.APIClient')
    def test_create_synchronizer(self, mock_api_client):
        """Test creating a synchronizer with environment variables."""
        # Set environment variables
        with patch.dict(os.environ, {
            "SMARTER_DEV_API_URL": "http://test.com",
            "SMARTER_DEV_API_KEY": "test_api_key"
        }):
            sync = create_synchronizer()
            self.assertIsNotNone(sync)
            mock_api_client.assert_called_once_with("http://test.com", "test_api_key")
    
    @patch('bot.api_sync.APIClient')
    def test_create_synchronizer_local_mode(self, mock_api_client):
        """Test creating a synchronizer in local development mode."""
        # Set environment variables for local mode
        with patch.dict(os.environ, {
            "SMARTER_DEV_LOCAL": "1",
            "SMARTER_DEV_API_URL": "http://localhost:8000"
        }):
            sync = create_synchronizer()
            self.assertIsNotNone(sync)
            mock_api_client.assert_called_once_with("http://localhost:8000", "TESTING")


if __name__ == "__main__":
    unittest.main()
