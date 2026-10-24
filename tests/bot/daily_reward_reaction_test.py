"""The daily reward reaction falls back to unicode when the custom emoji is gone."""

from __future__ import annotations

import hikari
import pytest

from smarter_dev.bot import client


class FakeMessage:
    """Records reactions; refuses the ones in ``errors`` with the given error."""

    def __init__(self, errors: dict[str, Exception] | None = None) -> None:
        self.errors = errors or {}
        self.attempts: list[str] = []

    async def add_reaction(self, emoji: str) -> None:
        self.attempts.append(emoji)
        if emoji in self.errors:
            raise self.errors[emoji]


def bad_request(code: int, message: str) -> hikari.BadRequestError:
    return hikari.BadRequestError("https://discord.test", {}, b"", message, code)


@pytest.fixture(autouse=True)
def reset_missing_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_daily_reward_emoji_missing", False)


async def test_uses_the_custom_emoji_when_discord_accepts_it() -> None:
    message = FakeMessage()
    await client.add_daily_reward_reaction(message)
    assert message.attempts == [client.DAILY_REWARD_EMOJI]


async def test_unknown_emoji_falls_back_to_unicode_and_stops_retrying() -> None:
    unknown = bad_request(client.UNKNOWN_EMOJI_CODE, "Unknown Emoji")
    first = FakeMessage({client.DAILY_REWARD_EMOJI: unknown})
    await client.add_daily_reward_reaction(first)
    assert first.attempts == [client.DAILY_REWARD_EMOJI, "🎉"]

    # Later claims go straight to the fallback instead of failing first.
    second = FakeMessage({client.DAILY_REWARD_EMOJI: unknown})
    await client.add_daily_reward_reaction(second)
    assert second.attempts == ["🎉"]


async def test_other_bad_requests_propagate_without_fallback() -> None:
    message = FakeMessage(
        {client.DAILY_REWARD_EMOJI: bad_request(90001, "Reaction Blocked")}
    )
    with pytest.raises(hikari.BadRequestError):
        await client.add_daily_reward_reaction(message)
    assert message.attempts == [client.DAILY_REWARD_EMOJI]
    assert client._daily_reward_emoji_missing is False
