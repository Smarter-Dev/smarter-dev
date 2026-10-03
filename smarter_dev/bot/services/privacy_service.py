"""Bot-API calls for the per-user privacy purge (#79).

Two endpoints on the web app, both behind the existing bot-API key:

- ``GET /api/privacy/blocked-users`` -> ``{"revision": int, "user_ids": [...]}``
- ``POST /api/privacy/purges/{run_id}/acks`` -> ``{"accepted": true}``; 404
  for an unknown run (the consumer then XACKs and drops the command).

Neither request nor response is ever logged: the list is the ids of people
who asked to be forgotten.
"""

from __future__ import annotations

from pydantic import ValidationError

from smarter_dev.bot.privacy.blocked_users import BlockedUsersSnapshot
from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.shared.privacy_purge import BlockedUsers
from smarter_dev.shared.privacy_purge import PurgeAck

BLOCKED_USERS_PATH = "/privacy/blocked-users"


class MalformedBlockedUsersResponse(ValueError):
    """The blocked-users response did not match the contract."""


class PrivacyApiService:
    """The bot's side of the privacy endpoints."""

    def __init__(self, api_client: APIClient):
        self._api_client = api_client

    async def fetch_blocked_users(self) -> BlockedUsersSnapshot:
        response = await self._api_client.get(BLOCKED_USERS_PATH)
        return parse_blocked_users(response.json())

    async def post_purge_ack(self, run_id: str, ack: PurgeAck) -> bool:
        """Post one guild's ack. False when the run is unknown (404)."""
        try:
            await self._api_client.post(
                f"/privacy/purges/{run_id}/acks",
                json_data=ack.model_dump(mode="json"),
            )
        except APIError as error:
            if error.status_code == 404:
                return False
            raise
        return True


def parse_blocked_users(payload: object) -> BlockedUsersSnapshot:
    """Validate a blocked-users response without echoing its contents."""
    try:
        parsed = BlockedUsers.model_validate(payload)
    except ValidationError:
        # The error text would quote the offending ids; report only the fact.
        raise MalformedBlockedUsersResponse(
            "blocked-users response does not match the contract"
        ) from None
    return BlockedUsersSnapshot(
        revision=parsed.revision, user_ids=frozenset(parsed.user_ids)
    )
