#!/usr/bin/env python3
"""Test runner to check if tests can run and get basic status."""

import subprocess
import sys
import os

def check_pytest_available():
    """Check if pytest is available."""
    try:
        result = subprocess.run([sys.executable, "-m", "pytest", "--version"], 
                              capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout
    except Exception as e:
        return False, str(e)

def run_test_collection():
    """Try to collect tests without running them."""
    try:
        result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], 
                              capture_output=True, text=True, timeout=60)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Test collection timed out"
    except Exception as e:
        return -2, "", str(e)

def run_simple_test():
    """Try to run a simple, fast test."""
    try:
        # Try to run a single test file that's likely to be fast
        result = subprocess.run([sys.executable, "-m", "pytest", "tests/test_constants.py", "-v"], 
                              capture_output=True, text=True, timeout=30)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Test execution timed out"
    except Exception as e:
        return -2, "", str(e)

def main():
    """Main test runner check."""
    print("🔍 Checking test environment...")
    
    # Check if we're in the right directory
    if not os.path.exists("pytest.ini"):
        print("❌ pytest.ini not found - not in project root?")
        return 1
    
    # Check pytest availability
    pytest_available, pytest_version = check_pytest_available()
    if not pytest_available:
        print(f"❌ pytest not available: {pytest_version}")
        return 1
    
    print(f"✅ pytest available: {pytest_version.strip()}")
    
    # Try test collection
    print("\n🔍 Collecting tests...")
    collection_code, collection_out, collection_err = run_test_collection()
    
    if collection_code == 0:
        print("✅ Test collection successful")
        lines = collection_out.strip().split('\n')
        if lines:
            last_line = lines[-1]
            if "collected" in last_line:
                print(f"📊 {last_line}")
    else:
        print(f"❌ Test collection failed (code: {collection_code})")
        print("STDOUT:", collection_out[-500:])
        print("STDERR:", collection_err[-500:])
        return 1
    
    # Try running a simple test
    print("\n🔍 Running simple test...")
    test_code, test_out, test_err = run_simple_test()
    
    if test_code == 0:
        print("✅ Simple test execution successful")
    else:
        print(f"❌ Simple test failed (code: {test_code})")
        if test_out:
            print("STDOUT:", test_out[-500:])
        if test_err:
            print("STDERR:", test_err[-500:])
    
    return 0

if __name__ == "__main__":
    sys.exit(main())