"""HTTP client for the internal media service.

The media service owns every heavy rendering job the bot used to do in-process:
LaTeX rasterisation, WAV -> Ogg/Opus transcoding, and the 14 Discord card
images. There is exactly one code path — if the service cannot be reached the
call raises, it never falls back to a local renderer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from typing import Any

import httpx

from smarter_dev.shared.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15.0
CONNECT_TIMEOUT_SECONDS = 3.0
RETRY_DELAY_SECONDS = 0.25

LATEX_PATH = "/v1/latex"
AUDIO_PATH = "/v1/audio/opus-ogg"
HEALTH_PATH = "/health"

# Filenames the service sets on Content-Disposition. Kept here only as the
# fallback for a response that omits the header.
LATEX_FILENAME = "latex.png"
DEFAULT_CARD_FILENAME = "embed.png"
CARD_FILENAMES = {
    "balance": "balance.png",
    "transfer-success": "transfer_success.png",
}


@dataclass(frozen=True)
class RenderedMedia:
    """One rendered asset, ready to become a Discord attachment."""

    data: bytes
    filename: str
    mime_type: str


class MediaServiceError(RuntimeError):
    """Base for every media-service failure."""


class MediaServiceUnavailableError(MediaServiceError):
    """The service could not be reached, timed out, or returned 5xx."""


class MediaRenderError(MediaServiceError):
    """The service rejected the request. Carries the service's error code."""

    def __init__(self, message: str, *, code: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _isoformat_if_temporal(value: Any) -> Any:
    """Return ISO-8601 text for date/datetime values, other values unchanged."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _attribute_payload(
    source: Any,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Read named attributes off ``source`` into a JSON-ready dict.

    ``optional`` names mirror the ``hasattr`` guards the Pillow generator used:
    an attribute the object does not carry is left out of the body entirely so
    the service can branch on presence exactly where Python did.
    """
    payload = {
        name: _isoformat_if_temporal(getattr(source, name)) for name in required
    }
    for name in optional:
        if hasattr(source, name):
            payload[name] = _isoformat_if_temporal(getattr(source, name))
    return payload


def _leaderboard_entry_payload(entry: Any) -> dict[str, Any]:
    return _attribute_payload(entry, ("rank", "user_id", "balance", "streak_count"))


def _transaction_payload(transaction: Any) -> dict[str, Any]:
    return _attribute_payload(
        transaction,
        (
            "created_at",
            "giver_id",
            "giver_username",
            "receiver_id",
            "receiver_username",
            "amount",
            "reason",
        ),
    )


def _bytes_config_payload(config: Any) -> dict[str, Any]:
    return _attribute_payload(
        config,
        (
            "daily_amount",
            "starting_balance",
            "max_transfer",
            "transfer_cooldown_hours",
            "streak_bonuses",
        ),
    )


def _squad_payload(squad: Any) -> dict[str, Any]:
    return _attribute_payload(
        squad,
        ("name", "description", "member_count", "max_members", "switch_cost", "is_active"),
        ("current_join_cost", "has_join_sale", "role_id", "is_default"),
    )


def _squad_member_payload(member: Any) -> dict[str, Any]:
    return _attribute_payload(member, ("user_id", "username", "joined_at"))


def _user_member_info_payload(info: Any) -> dict[str, Any] | None:
    if info is None:
        return None
    return _attribute_payload(info, ("member_since",))


class MediaClient:
    """Async client for the media service, with typed failures."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delay_seconds: float = RETRY_DELAY_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._api_key = api_key
        self._transport = transport
        self._retry_delay_seconds = retry_delay_seconds
        self._http_client: httpx.AsyncClient | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> MediaClient:
        """Build a client from settings, failing immediately if misconfigured."""
        base_url = (settings.media_service_url or "").strip()
        api_key = (settings.media_api_key or "").strip()
        if not base_url:
            raise ValueError("MEDIA_SERVICE_URL is not configured")
        if not api_key:
            raise ValueError("MEDIA_API_KEY is not configured")
        return cls(
            base_url,
            api_key,
            timeout_seconds=settings.media_request_timeout_seconds,
        )

    async def health(self) -> dict[str, Any]:
        """Return the service's health document, raising if it is not healthy."""
        response = await self._send("GET", HEALTH_PATH)
        try:
            return response.json()
        except ValueError as exc:
            raise MediaServiceUnavailableError(
                f"Media service at {self.base_url} returned an unreadable health "
                f"document"
            ) from exc

    async def render_latex(self, source: str) -> RenderedMedia:
        """Render one TeX expression to a PNG attachment."""
        return await self._render_json(LATEX_PATH, {"source": source}, LATEX_FILENAME)

    async def transcode_wav_to_opus_ogg(
        self, wav: bytes, *, bitrate: str = "48k"
    ) -> bytes:
        """Transcode WAV bytes to Ogg/Opus bytes."""
        response = await self._send(
            "POST",
            AUDIO_PATH,
            content=wav,
            headers={"Content-Type": "audio/wav"},
            params={"bitrate": bitrate},
        )
        return response.content

    async def create_simple_embed(
        self,
        title: str,
        description: str,
        embed_type: str = "default",
    ) -> RenderedMedia:
        return await self._render_card(
            "simple",
            {"title": title, "description": description, "embed_type": embed_type},
        )

    async def create_error_embed(self, message: str) -> RenderedMedia:
        return await self._render_card("error", {"message": message})

    async def create_success_embed(
        self, title: str, description: str
    ) -> RenderedMedia:
        return await self._render_card(
            "success", {"title": title, "description": description}
        )

    async def create_info_embed(self, title: str, description: str) -> RenderedMedia:
        return await self._render_card(
            "info", {"title": title, "description": description}
        )

    async def create_cooldown_embed(
        self,
        message: str,
        cooldown_end_timestamp: int | None = None,
    ) -> RenderedMedia:
        return await self._render_card(
            "cooldown",
            {
                "message": message,
                "cooldown_end_timestamp": cooldown_end_timestamp,
            },
        )

    async def create_leaderboard_embed(
        self,
        entries: list,
        guild_name: str,
        user_display_names: dict,
    ) -> RenderedMedia:
        return await self._render_card(
            "leaderboard",
            {
                "entries": [_leaderboard_entry_payload(entry) for entry in entries],
                "guild_name": guild_name,
                "user_display_names": dict(user_display_names or {}),
            },
        )

    async def create_history_embed(
        self, transactions: list, user_id: str
    ) -> RenderedMedia:
        return await self._render_card(
            "history",
            {
                "transactions": [
                    _transaction_payload(transaction) for transaction in transactions
                ],
                "user_id": user_id,
            },
        )

    async def create_config_embed(self, config, guild_name: str) -> RenderedMedia:
        return await self._render_card(
            "config",
            {"config": _bytes_config_payload(config), "guild_name": guild_name},
        )

    async def create_squad_list_embed(
        self,
        squads: list,
        guild_name: str,
        current_squad_id: str | None = None,
        guild_roles: dict[str, int] | None = None,
        has_active_campaign: bool = False,
    ) -> RenderedMedia:
        return await self._render_card(
            "squad-list",
            {
                "squads": [_squad_payload(squad) for squad in squads],
                "guild_name": guild_name,
                "current_squad_id": (
                    str(current_squad_id) if current_squad_id is not None else None
                ),
                "guild_roles": dict(guild_roles or {}),
                "has_active_campaign": has_active_campaign,
            },
        )

    async def create_squad_info_embed(
        self,
        squad,
        members: list,
        user_member_info=None,
    ) -> RenderedMedia:
        return await self._render_card(
            "squad-info",
            {
                "squad": _squad_payload(squad),
                "members": [_squad_member_payload(member) for member in members],
                "user_member_info": _user_member_info_payload(user_member_info),
            },
        )

    async def create_squad_members_embed(self, squad, members: list) -> RenderedMedia:
        return await self._render_card(
            "squad-members",
            {
                "squad": _squad_payload(squad),
                "members": [_squad_member_payload(member) for member in members],
            },
        )

    async def create_squad_join_selector_embed(
        self,
        user_balance: int,
        current_squad_name: str | None = None,
        available_squads_count: int = 0,
    ) -> RenderedMedia:
        return await self._render_card(
            "squad-join-selector",
            {
                "user_balance": user_balance,
                "current_squad_name": current_squad_name,
                "available_squads_count": available_squads_count,
            },
        )

    async def create_balance_embed(
        self,
        username: str,
        balance: int,
        streak_count: int = 0,
        last_daily: str | None = None,
        total_received: int = 0,
        total_sent: int = 0,
    ) -> RenderedMedia:
        return await self._render_card(
            "balance",
            {
                "username": username,
                "balance": balance,
                "streak_count": streak_count,
                "last_daily": _isoformat_if_temporal(last_daily),
                "total_received": total_received,
                "total_sent": total_sent,
            },
        )

    async def create_transfer_success_embed(
        self,
        giver_name: str,
        receiver_name: str,
        amount: int,
        reason: str | None = None,
        new_balance: int | None = None,
    ) -> RenderedMedia:
        return await self._render_card(
            "transfer-success",
            {
                "giver_name": giver_name,
                "receiver_name": receiver_name,
                "amount": amount,
                "reason": reason,
                "new_balance": new_balance,
            },
        )

    async def aclose(self) -> None:
        """Dispose the underlying HTTP connection pool."""
        http_client = self._http_client
        self._http_client = None
        if http_client is not None:
            await http_client.aclose()

    async def _render_card(self, card: str, body: dict[str, Any]) -> RenderedMedia:
        return await self._render_json(
            f"/v1/cards/{card}",
            body,
            CARD_FILENAMES.get(card, DEFAULT_CARD_FILENAME),
        )

    async def _render_json(
        self,
        path: str,
        body: dict[str, Any],
        default_filename: str,
    ) -> RenderedMedia:
        response = await self._send("POST", path, json=body)
        return RenderedMedia(
            data=response.content,
            filename=_filename_from_response(response, default_filename),
            mime_type=response.headers.get("Content-Type", "application/octet-stream"),
        )

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self.base_url,
                transport=self._transport,
                http2=False,
                headers={"Authorization": f"Bearer {self._api_key}"},
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                timeout=httpx.Timeout(
                    self.timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS
                ),
            )
        return self._http_client

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send one request, retrying once on a dropped connection."""
        client = self._client()
        try:
            response = await client.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ReadError) as exc:
            logger.warning(
                "Media service connection to %s failed (%s); retrying once",
                self.base_url,
                exc,
            )
            await asyncio.sleep(self._retry_delay_seconds)
            try:
                response = await client.request(method, path, **kwargs)
            except httpx.TransportError as retry_exc:
                raise self._unavailable(retry_exc) from retry_exc
        except httpx.TransportError as exc:
            raise self._unavailable(exc) from exc

        if response.is_success:
            return response
        raise self._failure(response)

    def _unavailable(self, exc: Exception) -> MediaServiceUnavailableError:
        return MediaServiceUnavailableError(
            f"Media service at {self.base_url} is unreachable: {exc}"
        )

    def _failure(self, response: httpx.Response) -> MediaServiceError:
        code, message = _parse_error_envelope(response)
        if code is None:
            return MediaServiceUnavailableError(
                f"Media service at {self.base_url} returned an unexpected "
                f"{response.status_code} response"
            )
        if response.status_code >= 500:
            return MediaServiceUnavailableError(
                f"Media service at {self.base_url} failed with {code}: {message}"
            )
        return MediaRenderError(
            message, code=code, status_code=response.status_code
        )


def _parse_error_envelope(response: httpx.Response) -> tuple[str | None, str]:
    """Pull ``code``/``message`` out of the service's error envelope."""
    try:
        payload = response.json()
    except ValueError:
        return None, response.text
    if not isinstance(payload, dict):
        return None, response.text
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, response.text
    code = error.get("code")
    if not isinstance(code, str) or not code:
        return None, response.text
    return code, str(error.get("message") or code)


def _filename_from_response(response: httpx.Response, default: str) -> str:
    """Read the attachment filename off Content-Disposition."""
    disposition = response.headers.get("Content-Disposition", "")
    for part in disposition.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key.lower() == "filename":
            filename = value.strip().strip('"')
            if filename:
                return filename
    return default
