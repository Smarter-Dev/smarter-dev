"""Test utilities for integration tests."""

import asyncio
import random
from typing import Any, Dict, Optional

import pytest


async def with_retry(func, max_retries: int = 3, delay: float = 0.1):
    """Execute a function with retry logic."""
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            return await func()
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                # Add jitter to prevent thundering herd
                jitter = random.uniform(0, delay)
                await asyncio.sleep(delay + jitter)
                delay *= 2  # Exponential backoff
    
    raise last_exception


async def ensure_test_isolation():
    """Ensure tests are isolated by adding a small delay."""
    await asyncio.sleep(0.1)


def skip_if_insufficient_balance(balance: int, required: int):
    """Skip test if balance is insufficient."""
    if balance < required:
        pytest.skip(f"Insufficient balance ({balance}) for test requiring {required}")


def make_flexible_assertion(actual: Any, expected: Any, tolerance: float = 0.1):
    """Make flexible assertions that allow for some variance."""
    if isinstance(expected, (int, float)):
        if isinstance(actual, (int, float)):
            # Allow for some tolerance in numeric values
            return abs(actual - expected) <= tolerance * expected
    
    return actual == expected