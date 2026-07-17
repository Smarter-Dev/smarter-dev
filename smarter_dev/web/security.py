"""Security utilities for API key hashing.

Key generation and format validation live in the Skrift key system
(``skrift.db.services.api_key_service`` / ``skrift.auth.guards``); this
module only keeps the SHA-256 hashing helper shared by seed tooling and
key-correlation code.
"""

from __future__ import annotations

import hashlib


def hash_api_key(api_key: str) -> str:
    """Hash an API key for secure comparison.

    Args:
        api_key: The API key to hash

    Returns:
        str: SHA-256 hash of the API key

    Note:
        This function should be used when validating API keys
        to compare against stored hashes.
    """
    return hashlib.sha256(api_key.encode('utf-8')).hexdigest()
