#!/usr/bin/env python
"""
Test runner script for the bytes roles tests.
"""

import pytest
import sys
import os

if __name__ == "__main__":
    # Set the SMARTER_DEV_LOCAL environment variable for testing
    os.environ["SMARTER_DEV_LOCAL"] = "1"

    # Run the mock tests
    print("Running mock tests...")
    mock_exit_code = pytest.main(["-xvs", "tests/test_bytes_roles.py"])

    # Run the async API tests
    print("\nRunning async API tests...")
    async_exit_code = pytest.main(["-xvs", "tests/test_bytes_roles_api_async.py"])

    # Exit with non-zero code if any tests failed
    sys.exit(mock_exit_code or async_exit_code)
