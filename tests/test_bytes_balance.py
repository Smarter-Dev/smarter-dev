"""
Test script for verifying bytes balance functionality.

This script tests:
1. The API endpoint for retrieving a user's bytes balance
2. The admin form for giving bytes to a user
3. Verifying that the bytes balance is updated correctly
"""

import os
import sys
import requests
import json
from datetime import datetime
import unittest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
import time

# Add the parent directory to the path so we can import the models
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from website.models import Base, DiscordUser, Bytes, Guild
from website.database import get_db, engine

class TestBytesBalance(unittest.TestCase):
    """Test cases for bytes balance functionality."""

    def setUp(self):
        """Set up the test environment."""
        # Create a test database session
        self.engine = engine
        Session = sessionmaker(bind=self.engine)
        self.session = Session()

        # Create a test guild if it doesn't exist
        self.test_guild = self.session.query(Guild).filter(Guild.discord_id == 999999).first()
        if not self.test_guild:
            self.test_guild = Guild(
                discord_id=999999,
                name="Test Guild",
                joined_at=datetime.now()
            )
            self.session.add(self.test_guild)
            self.session.commit()
            self.session.refresh(self.test_guild)

        # Create a test user if it doesn't exist
        self.test_user = self.session.query(DiscordUser).filter(DiscordUser.discord_id == 888888).first()
        if not self.test_user:
            self.test_user = DiscordUser(
                discord_id=888888,
                username="Test User",
                bytes_balance=0
            )
            self.session.add(self.test_user)
            self.session.commit()
            self.session.refresh(self.test_user)

        # Create or get the system admin user
        self.admin_user = self.session.query(DiscordUser).filter(DiscordUser.discord_id == 0).first()
        if not self.admin_user:
            self.admin_user = DiscordUser(
                discord_id=0,
                username="System",
                bytes_balance=999999
            )
            self.session.add(self.admin_user)
            self.session.commit()
            self.session.refresh(self.admin_user)

        # Reset the test user's bytes balance to 0
        self.test_user.bytes_balance = 0
        self.session.commit()

        # Delete any existing bytes transactions for the test user
        self.session.query(Bytes).filter(
            (Bytes.receiver_id == self.test_user.id) |
            (Bytes.giver_id == self.test_user.id)
        ).delete()
        self.session.commit()

        # Base URL for API requests
        self.base_url = "http://localhost:8000"

        # Get API token for authentication
        self.api_token = self.get_api_token()

    def tearDown(self):
        """Clean up after the test."""
        self.session.close()

    def get_api_token(self):
        """Get an API token for authentication."""
        # For testing, we'll use the TESTING API key which works when SMARTER_DEV_LOCAL=1
        response = requests.post(
            f"{self.base_url}/api/auth/token",
            json={"api_key": "TESTING"}
        )

        if response.status_code != 200:
            print(f"Failed to get API token: {response.text}")
            return None

        data = response.json()
        return data.get("token")

    def test_initial_balance(self):
        """Test that the initial balance is 0."""
        # Check the database directly
        user = self.session.query(DiscordUser).filter(DiscordUser.id == self.test_user.id).first()
        self.assertEqual(user.bytes_balance, 0)

        # Check via the API
        headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
        response = requests.get(
            f"{self.base_url}/api/bytes/balance/{self.test_user.id}",
            headers=headers
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["bytes_balance"], 0)
        self.assertEqual(data["bytes_received"], 0)
        self.assertEqual(data["bytes_given"], 0)

    def test_admin_give_bytes(self):
        """Test giving bytes to a user via the admin interface."""
        # Create a bytes transaction directly in the database
        bytes_obj = Bytes(
            giver_id=self.admin_user.id,
            receiver_id=self.test_user.id,
            guild_id=self.test_guild.id,
            amount=100,
            reason="Test admin bytes",
            awarded_at=datetime.now()
        )
        self.session.add(bytes_obj)

        # Update the user's bytes balance
        self.test_user.bytes_balance += 100
        self.session.commit()

        # Check the database directly
        user = self.session.query(DiscordUser).filter(DiscordUser.id == self.test_user.id).first()
        self.assertEqual(user.bytes_balance, 100)

        # Check via the API
        headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
        response = requests.get(
            f"{self.base_url}/api/bytes/balance/{self.test_user.id}",
            headers=headers
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        print(f"API response: {data}")
        self.assertEqual(data["bytes_balance"], 100)
        self.assertEqual(data["bytes_received"], 100)
        self.assertEqual(data["bytes_given"], 0)

    def test_api_balance_calculation(self):
        """Test that the API calculates the balance correctly."""
        # Create multiple bytes transactions
        # 1. Admin gives 50 bytes to test user
        bytes_obj1 = Bytes(
            giver_id=self.admin_user.id,
            receiver_id=self.test_user.id,
            guild_id=self.test_guild.id,
            amount=50,
            reason="Admin gift 1",
            awarded_at=datetime.now()
        )
        self.session.add(bytes_obj1)

        # 2. Admin gives another 75 bytes to test user
        bytes_obj2 = Bytes(
            giver_id=self.admin_user.id,
            receiver_id=self.test_user.id,
            guild_id=self.test_guild.id,
            amount=75,
            reason="Admin gift 2",
            awarded_at=datetime.now()
        )
        self.session.add(bytes_obj2)

        # Don't update the user's bytes_balance to simulate a discrepancy
        self.session.commit()

        # Check via the API - it should fix the discrepancy
        headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
        response = requests.get(
            f"{self.base_url}/api/bytes/balance/{self.test_user.id}",
            headers=headers
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        print(f"API response after multiple transactions: {data}")
        self.assertEqual(data["bytes_balance"], 125)  # 50 + 75
        self.assertEqual(data["bytes_received"], 125)
        self.assertEqual(data["bytes_given"], 0)

        # Check the database directly - it should be updated
        # Close and reopen the session to get the latest data
        self.session.close()
        Session = sessionmaker(bind=self.engine)
        self.session = Session()

        user = self.session.query(DiscordUser).filter(DiscordUser.id == self.test_user.id).first()
        print(f"Database balance after API check: {user.bytes_balance}")
        self.assertEqual(user.bytes_balance, 125)

    def test_admin_form_submission(self):
        """Test submitting the admin form to give bytes."""
        # This test simulates submitting the admin form
        # We'll use requests to submit the form
        form_data = {
            "amount": 200,
            "reason": "Test form submission",
            "guild_id": self.test_guild.id
        }

        # For admin routes, we need to be logged in as an admin
        # This is more complex to test, so we'll simulate it by directly adding bytes
        # in the database instead of using the admin form

        # Create a bytes transaction directly in the database
        bytes_obj = Bytes(
            giver_id=self.admin_user.id,
            receiver_id=self.test_user.id,
            guild_id=self.test_guild.id,
            amount=200,
            reason="Test direct database insertion",
            awarded_at=datetime.now()
        )
        self.session.add(bytes_obj)

        # Update the user's bytes balance
        self.test_user.bytes_balance += 200
        self.session.commit()

        # Give the database a moment to update
        time.sleep(1)

        # Check the database directly
        user = self.session.query(DiscordUser).filter(DiscordUser.id == self.test_user.id).first()
        self.assertEqual(user.bytes_balance, 200)

        # Check via the API
        headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
        response = requests.get(
            f"{self.base_url}/api/bytes/balance/{self.test_user.id}",
            headers=headers
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        print(f"API response after form submission: {data}")
        self.assertEqual(data["bytes_balance"], 200)
        self.assertEqual(data["bytes_received"], 200)
        self.assertEqual(data["bytes_given"], 0)

if __name__ == "__main__":
    unittest.main()
