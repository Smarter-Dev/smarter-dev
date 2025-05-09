#!/usr/bin/env python
"""
Test runner script for the bytes roles async tests.
"""

import pytest
import sys
import os

if __name__ == "__main__":
    # Set the SMARTER_DEV_LOCAL environment variable for testing
    os.environ["SMARTER_DEV_LOCAL"] = "1"
    
    # Run the pytest tests
    print("Running async API tests...")
    exit_code = pytest.main(["-xvs", "tests/test_bytes_roles_api_async.py"])
    
    # Exit with non-zero code if any tests failed
    sys.exit(exit_code)
