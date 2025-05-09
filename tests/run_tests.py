#!/usr/bin/env python
"""
Test runner script for the bytes plugin tests.
"""

import pytest
import sys

if __name__ == "__main__":
    # Run the tests
    exit_code = pytest.main(["-xvs", "tests/test_api_endpoints.py", "tests/test_bytes_plugin.py"])
    sys.exit(exit_code)
