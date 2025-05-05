"""
Script to run the bytes balance tests and fix any issues found.
"""

import os
import sys
import unittest
import requests
import json
from datetime import datetime

# Add the parent directory to the path so we can import the models
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from website.models import Base, DiscordUser, Bytes, Guild
from website.database import get_db, engine
from sqlalchemy.orm import sessionmaker

def check_server_running():
    """Check if the server is running."""
    try:
        response = requests.get("http://localhost:8000/")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        return False

def main():
    """Run the tests and fix any issues."""
    # Check if the server is running
    if not check_server_running():
        print("Error: The server is not running. Please start the server first.")
        sys.exit(1)

    # Set the SMARTER_DEV_LOCAL environment variable for testing
    os.environ["SMARTER_DEV_LOCAL"] = "1"

    # Run the tests
    from tests.test_bytes_balance import TestBytesBalance

    # Create a test suite
    suite = unittest.TestLoader().loadTestsFromTestCase(TestBytesBalance)

    # Run the tests
    result = unittest.TextTestRunner(verbosity=2).run(suite)

    # Check if all tests passed
    if result.wasSuccessful():
        print("\nAll tests passed! The bytes balance functionality is working correctly.")
    else:
        print("\nSome tests failed. Please check the output above for details.")

        # Print suggestions for fixing the issues
        print("\nSuggestions for fixing the issues:")
        print("1. Check the user_bytes_balance function in api_routes.py")
        print("2. Check the admin_discord_give_bytes function in discord_admin_routes.py")
        print("3. Make sure the bytes_balance field is being updated correctly")
        print("4. Check that the API is calculating the balance correctly")

if __name__ == "__main__":
    main()
