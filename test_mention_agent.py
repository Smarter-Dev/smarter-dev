#!/usr/bin/env python3
"""Test runner script for mention agent contextual filtering tests.

Usage:
    python test_mention_agent.py                    # Run unit tests only (no LLM)
    python test_mention_agent.py --with-llm         # Run all tests including LLM evaluation
    python test_mention_agent.py --llm-only         # Run only LLM evaluation tests
    python test_mention_agent.py --help             # Show help
"""

import argparse
import subprocess
import sys
from pathlib import Path


def run_tests(test_type: str) -> int:
    """Run the specified type of tests.
    
    Args:
        test_type: Type of tests to run ('unit', 'llm', or 'all')
        
    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    test_files = [
        "tests/bot/test_mention_agent_llm_judge.py"
    ]
    
    if test_type == "unit":
        # Run tests excluding LLM markers, no coverage for these specific tests
        cmd = ["uv", "run", "python", "-m", "pytest"] + test_files + ["-v", "-m", "not llm", "--no-cov"]
        print("🔧 Running unit tests (excluding LLM evaluation tests)...")
    elif test_type == "llm":
        # Run only LLM tests, no coverage
        cmd = ["uv", "run", "python", "-m", "pytest"] + test_files + ["-v", "-m", "llm", "--no-cov"]
        print("🤖 Running LLM evaluation tests (this may be slow and use API credits)...")
    else:  # all
        # Run all tests, no coverage
        cmd = ["uv", "run", "python", "-m", "pytest"] + test_files + ["-v", "--no-cov"]
        print("🚀 Running all tests (including LLM evaluation)...")
    
    print(f"Command: {' '.join(cmd)}")
    print("-" * 60)
    
    try:
        result = subprocess.run(cmd, cwd=Path(__file__).parent)
        return result.returncode
    except KeyboardInterrupt:
        print("\\n❌ Tests interrupted by user")
        return 1
    except Exception as e:
        print(f"❌ Error running tests: {e}")
        return 1


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Test runner for mention agent contextual filtering tests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_mention_agent.py                 # Run unit tests only
  python test_mention_agent.py --with-llm      # Run all tests including LLM
  python test_mention_agent.py --llm-only      # Run only LLM tests
  
The LLM evaluation tests use Gemini 2.5 Flash Lite to evaluate response quality.
They require GEMINI_API_KEY in .env and will consume API credits.
        """
    )
    
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--with-llm", 
        action="store_true",
        help="Run all tests including LLM evaluation tests"
    )
    group.add_argument(
        "--llm-only",
        action="store_true", 
        help="Run only LLM evaluation tests"
    )
    
    args = parser.parse_args()
    
    # Determine test type
    if args.llm_only:
        test_type = "llm"
    elif args.with_llm:
        test_type = "all"
    else:
        test_type = "unit"
    
    # Check if test files exist
    test_files = [
        Path("tests/bot/test_mention_agent_llm_judge.py")
    ]
    
    for test_file in test_files:
        if not test_file.exists():
            print(f"❌ Test file not found: {test_file}")
            print("Make sure you're running from the project root directory.")
            return 1
    
    # Show warning for LLM tests
    if test_type in ["llm", "all"]:
        print("⚠️  WARNING: LLM evaluation tests will:")
        print("   - Use API credits (Gemini 2.5 Flash Lite)")
        print("   - Take longer to run")
        print("   - Require GEMINI_API_KEY in .env file")
        print()
        
        if test_type == "llm":
            response = input("Continue with LLM tests? (y/N): ").lower().strip()
            if response != 'y':
                print("Cancelled.")
                return 0
    
    # Run the tests
    exit_code = run_tests(test_type)
    
    # Summary
    if exit_code == 0:
        print("\\n✅ All tests passed!")
    else:
        print(f"\\n❌ Tests failed with exit code {exit_code}")
        
        if test_type == "unit":
            print("\\nTip: Run with --with-llm to include LLM evaluation tests")
    
    return exit_code


if __name__ == "__main__":
    sys.exit(main())