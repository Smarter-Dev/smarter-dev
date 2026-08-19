"""Tests for the signed per-channel configuration links.

The link IS the authorization: the bot mints it only for a moderator who
just proved their permissions in Discord, and the web page trusts nothing
else. So the signature, the expiry and the exact scope are the security
boundary and get tested as such.
"""

from __future__ import annotations

import pytest

from smarter_dev.shared import config_links

GUILD = "644299523686006834"
CHANNEL = "644299524151443487"
USER = "266000000000000001"


def test_round_trip_carries_the_full_scope():
    token = config_links.sign_config_link(
        guild_id=GUILD, channel_id=CHANNEL, discord_user_id=USER
    )
    payload = config_links.verify_config_link(token)
    assert payload == config_links.ConfigLinkPayload(
        guild_id=GUILD, channel_id=CHANNEL, discord_user_id=USER
    )


def test_tampered_token_is_rejected():
    token = config_links.sign_config_link(
        guild_id=GUILD, channel_id=CHANNEL, discord_user_id=USER
    )
    # Flip one character anywhere in the token; the signature no longer matches.
    index = len(token) // 3
    swapped = "A" if token[index] != "A" else "B"
    tampered = token[:index] + swapped + token[index + 1 :]
    assert config_links.verify_config_link(tampered) is None


def test_token_signed_with_another_secret_is_rejected(monkeypatch):
    token = config_links.sign_config_link(
        guild_id=GUILD, channel_id=CHANNEL, discord_user_id=USER
    )

    class _OtherSecret:
        api_secret_key = "a-completely-different-secret"

    monkeypatch.setattr(config_links, "get_settings", lambda: _OtherSecret())
    assert config_links.verify_config_link(token) is None


def test_expired_token_is_rejected():
    token = config_links.sign_config_link(
        guild_id=GUILD, channel_id=CHANNEL, discord_user_id=USER
    )
    assert config_links.verify_config_link(token, max_age_seconds=-1) is None


def test_garbage_is_rejected_without_raising():
    assert config_links.verify_config_link("not-a-token") is None
    assert config_links.verify_config_link("") is None


def test_link_url_is_built_from_the_public_base_url(monkeypatch):
    class _Settings:
        api_secret_key = "test-secret"
        site_base_url = "https://smarter.dev/"

    monkeypatch.setattr(config_links, "get_settings", lambda: _Settings())
    url = config_links.build_config_link_url(
        guild_id=GUILD, channel_id=CHANNEL, discord_user_id=USER
    )
    assert url.startswith("https://smarter.dev/admin/bot/configure/")
    assert not url.startswith("https://smarter.dev//")
    token = url.rsplit("/", 1)[1]
    assert config_links.verify_config_link(token).channel_id == CHANNEL


def test_a_link_for_one_channel_does_not_authorize_another():
    """The scope check the web handler relies on."""
    token = config_links.sign_config_link(
        guild_id=GUILD, channel_id=CHANNEL, discord_user_id=USER
    )
    payload = config_links.verify_config_link(token)
    assert payload.authorizes(guild_id=GUILD, channel_id=CHANNEL)
    assert not payload.authorizes(guild_id=GUILD, channel_id="999")
    assert not payload.authorizes(guild_id="999", channel_id=CHANNEL)
