"""Security utilities for API key generation and validation.

This module provides cryptographically secure functions for generating,
validating, and managing API keys with proper entropy and hashing.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Tuple


def generate_secure_api_key() -> Tuple[str, str, str]:
    """Generate a cryptographically secure API key.
    
    Uses Python's secrets module which leverages the operating system's
    entropy source (os.urandom()) to generate cryptographically secure
    random numbers suitable for security-sensitive applications.
    
    Returns:
        tuple: (full_key, key_hash, key_prefix)
            - full_key: Complete API key with sk- prefix (shown only once)
            - key_hash: SHA-256 hash for secure database storage
            - key_prefix: First 12 characters for display purposes
            
    Example:
        >>> full_key, key_hash, prefix = generate_secure_api_key()
        >>> print(f"Key: {full_key}")  # sk-abc123def456...
        >>> print(f"Prefix: {prefix}")  # sk-abc123de
        >>> print(f"Hash: {key_hash[:16]}...")  # a1b2c3d4e5f6...
    """
    # Generate 32 random bytes using OS entropy
    # This provides 256 bits of entropy, more secure than UUID4 (122 bits)
    key_data = secrets.token_urlsafe(32)
    
    # Create prefixed key for identification and type safety
    full_key = f"sk-{key_data}"
    
    # Generate SHA-256 hash for secure storage
    # Never store the plaintext key in the database
    key_hash = hashlib.sha256(full_key.encode('utf-8')).hexdigest()
    
    # Create prefix for display (first 12 chars including sk-)
    key_prefix = full_key[:12]
    
    return full_key, key_hash, key_prefix


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


LEGACY_API_KEY_PREFIX = "sk-"
SKRIFT_API_KEY_PREFIX = "sk_"

# Legacy keys are exactly sk- + 43 base64url chars from token_urlsafe(32).
LEGACY_API_KEY_LENGTH = 46

# Skrift keys are sk_ + secrets.token_urlsafe(32) (43 chars today). Accept a
# small range around that so a future padding change in the Skrift service
# doesn't lock the bot out, while still requiring >= 240 bits of entropy.
SKRIFT_API_KEY_TOKEN_MIN_LENGTH = 40
SKRIFT_API_KEY_TOKEN_MAX_LENGTH = 64

_BASE64URL_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _is_base64url(token_part: str) -> bool:
    """Check that every character is a valid base64url character."""
    return all(char in _BASE64URL_CHARS for char in token_part)


def validate_api_key_format(api_key: str) -> bool:
    """Validate API key format without revealing timing information.

    Accepts both key shapes in circulation during the legacy sunset:

    - Legacy: ``sk-`` + exactly 43 base64url chars (46 total)
    - Skrift: ``sk_`` + 40-64 base64url chars (token_urlsafe(32) => 43)

    Args:
        api_key: API key to validate

    Returns:
        bool: True if format is valid, False otherwise

    Note:
        Prefix checks use constant-time comparison to avoid
        leaking information about valid key formats.
    """
    # Check basic format requirements
    if not isinstance(api_key, str):
        return False

    # Check for empty or None
    if not api_key:
        return False

    if len(api_key) < 4:
        return False

    prefix = api_key[:3]
    token_part = api_key[3:]

    # LEGACY-FALLBACK: remove the sk- shape after key rotation (see runbook
    # docs/v2/legacy-sunset/runbooks/01-key-rotation.md)
    if secrets.compare_digest(prefix, LEGACY_API_KEY_PREFIX):
        if len(api_key) != LEGACY_API_KEY_LENGTH:
            return False
        return _is_base64url(token_part)

    if secrets.compare_digest(prefix, SKRIFT_API_KEY_PREFIX):
        if not (
            SKRIFT_API_KEY_TOKEN_MIN_LENGTH
            <= len(token_part)
            <= SKRIFT_API_KEY_TOKEN_MAX_LENGTH
        ):
            return False
        return _is_base64url(token_part)

    return False


def secure_compare_hashes(provided_hash: str, stored_hash: str) -> bool:
    """Securely compare two hashes using constant-time comparison.
    
    Args:
        provided_hash: Hash of the provided API key
        stored_hash: Hash stored in the database
        
    Returns:
        bool: True if hashes match, False otherwise
        
    Note:
        Uses secrets.compare_digest() to prevent timing attacks
        that could be used to determine valid key prefixes.
    """
    return secrets.compare_digest(provided_hash, stored_hash)