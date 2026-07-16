"""Tests for API key format validation in the bot's APIClient.

During the legacy key sunset the bot must accept BOTH the legacy ``sk-`` key
(exactly 46 chars) and Skrift-native ``sk_`` keys, so the bot does not need a
redeploy sequenced with the production key swap.
"""

from __future__ import annotations

import pytest

from smarter_dev.bot.services.api_client import APIClient


class TestAPIClientKeyValidation:
    """Construction-time API key format validation."""

    def test_accepts_legacy_sk_dash_key(self):
        """A legacy sk- key (sk- + 43 chars = 46 total) is accepted."""
        client = APIClient(base_url="http://test", api_key="sk-" + "a" * 43)
        assert client.api_key == "sk-" + "a" * 43

    def test_accepts_skrift_sk_underscore_key(self):
        """A Skrift sk_ key is accepted (token_urlsafe(32) => 43 chars)."""
        skrift_key = "sk_" + "b" * 43
        client = APIClient(base_url="http://test", api_key=skrift_key)
        assert client.api_key == skrift_key

    def test_accepts_skrift_key_with_urlsafe_charset(self):
        """Skrift keys may contain base64url chars including - and _."""
        skrift_key = "sk_Ab-cD_ef" + "gH1" * 12
        client = APIClient(base_url="http://test", api_key=skrift_key)
        assert client.api_key == skrift_key

    def test_rejects_missing_prefix(self):
        with pytest.raises(ValueError, match="Invalid API key format"):
            APIClient(base_url="http://test", api_key="nope-" + "a" * 43)

    def test_rejects_legacy_prefix_wrong_length(self):
        """sk- keys must be exactly 46 chars; a short one is rejected."""
        with pytest.raises(ValueError, match="Invalid API key format"):
            APIClient(base_url="http://test", api_key="sk-tooShort")

    def test_rejects_too_short_skrift_key(self):
        with pytest.raises(ValueError, match="Invalid API key format"):
            APIClient(base_url="http://test", api_key="sk_short")

    def test_rejects_too_long_key(self):
        with pytest.raises(ValueError, match="Invalid API key format"):
            APIClient(base_url="http://test", api_key="sk_" + "a" * 500)

    def test_rejects_non_base64url_characters(self):
        with pytest.raises(ValueError, match="Invalid API key format"):
            APIClient(base_url="http://test", api_key="sk_" + "a" * 20 + "!!!" + "a" * 20)

    def test_rejects_empty_key(self):
        with pytest.raises(ValueError, match="Invalid API key format"):
            APIClient(base_url="http://test", api_key="")
