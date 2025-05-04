"""
Tests for the Discord bot.

This module contains tests for the Discord bot implementation.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Add the project root to the path so we can import the bot package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.bot import create_bot


class TestBot(unittest.TestCase):
    """Test cases for the Discord bot."""

    @patch.dict(os.environ, {"SMARTER_DEV_BOT_TOKEN": "test_token"})
    def test_create_bot(self):
        """Test that the bot can be created with a token from environment variable."""
        # This should not raise an exception
        bot = create_bot()
        # Just check that the bot was created successfully
        self.assertIsNotNone(bot)

    @patch.dict(os.environ, {})
    def test_create_bot_no_token(self):
        """Test that creating the bot without a token raises an exception."""
        # Remove the token from environment variables
        if "SMARTER_DEV_BOT_TOKEN" in os.environ:
            del os.environ["SMARTER_DEV_BOT_TOKEN"]

        # This should raise a ValueError
        with self.assertRaises(ValueError):
            create_bot()


if __name__ == "__main__":
    unittest.main()
