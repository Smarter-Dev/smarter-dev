"""Signed, short-lived links from a Discord slash command to the admin page.

`/configure bot` hands a moderator a URL for exactly one channel. The link
carries its own authorization: the bot mints it only after Discord has
confirmed the invoker's permissions, and the web page trusts the signature
rather than a login — the site's admin auth is a global Skrift permission
with no per-guild notion, so a guild moderator has no other way in.

Security properties the web handler depends on:

- the payload is signed with ``api_secret_key`` (shared by the bot and web
  processes), so it cannot be forged or edited;
- it expires (:data:`DEFAULT_MAX_AGE_SECONDS`), so a forwarded link stops
  working;
- it names one guild, one channel and the Discord user it was minted for, so
  a link can only ever configure the channel it was issued for.
"""

from __future__ import annotations

from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from smarter_dev.shared.config import get_settings

# Long enough to walk to a browser and fill the form, short enough that a
# link pasted somewhere it shouldn't be goes stale quickly.
DEFAULT_MAX_AGE_SECONDS = 900
CONFIG_LINK_PATH = "/admin/bot/configure"
_SIGNER_SALT = "proactive-channel-config"


@dataclass(frozen=True)
class ConfigLinkPayload:
    guild_id: str
    channel_id: str
    discord_user_id: str

    def authorizes(self, *, guild_id: str, channel_id: str) -> bool:
        """Whether this link may configure that guild's channel."""
        return self.guild_id == guild_id and self.channel_id == channel_id


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        get_settings().api_secret_key, salt=_SIGNER_SALT
    )


def sign_config_link(
    *, guild_id: str, channel_id: str, discord_user_id: str
) -> str:
    return _serializer().dumps(
        {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "discord_user_id": discord_user_id,
        }
    )


def verify_config_link(
    token: str, *, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
) -> ConfigLinkPayload | None:
    """The link's scope, or ``None`` when it is forged, edited or expired."""
    if not token:
        return None
    try:
        raw = _serializer().loads(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return ConfigLinkPayload(
            guild_id=raw["guild_id"],
            channel_id=raw["channel_id"],
            discord_user_id=raw["discord_user_id"],
        )
    except KeyError:
        return None


def build_config_link_url(
    *, guild_id: str, channel_id: str, discord_user_id: str
) -> str:
    token = sign_config_link(
        guild_id=guild_id,
        channel_id=channel_id,
        discord_user_id=discord_user_id,
    )
    base = get_settings().site_base_url.rstrip("/")
    return f"{base}{CONFIG_LINK_PATH}/{token}"
