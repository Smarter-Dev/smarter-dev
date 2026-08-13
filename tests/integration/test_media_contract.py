"""End-to-end contract test against a running media service.

The drift guard from the media-service design (§9.2): for each of the 14 cards
plus latex and audio, POST the same fixture body that
``services/media/test/fixtures/cards/*.json`` holds through ``MediaClient`` and
assert a PNG/Ogg comes back.

Skipped unless ``MEDIA_SERVICE_URL`` is set. Run manually against a
locally-built media container before merging media-contract changes:

    podman compose up -d media
    MEDIA_SERVICE_URL=http://localhost:8083 \
    MEDIA_API_KEY=dev-media-key-not-secret \
    uv run pytest tests/integration/test_media_contract.py -q --no-cov -m integration
"""

from __future__ import annotations

import io
import json
import math
import os
import struct
import wave
from pathlib import Path

import pytest

from smarter_dev.bot.services.media_client import MediaClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("MEDIA_SERVICE_URL"),
        reason="MEDIA_SERVICE_URL is not set; needs a running media service",
    ),
]

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CARD_FIXTURES_DIR = REPOSITORY_ROOT / "services" / "media" / "test" / "fixtures" / "cards"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
OGG_MAGIC = b"OggS"

# Fixture file stem -> MediaClient method name. The request-body field names in
# each fixture are exactly the Python keyword arguments.
CARD_METHODS = {
    "simple": "create_simple_embed",
    "error": "create_error_embed",
    "success": "create_success_embed",
    "info": "create_info_embed",
    "cooldown": "create_cooldown_embed",
    "leaderboard": "create_leaderboard_embed",
    "history": "create_history_embed",
    "config": "create_config_embed",
    "squad-list": "create_squad_list_embed",
    "squad-info": "create_squad_info_embed",
    "squad-members": "create_squad_members_embed",
    "squad-join-selector": "create_squad_join_selector_embed",
    "balance": "create_balance_embed",
    "transfer-success": "create_transfer_success_embed",
}


class AttributeBag:
    """Wraps a fixture dict so attribute access mirrors the Python models.

    Only the top level becomes attributes: nested values like
    ``config.streak_bonuses`` stay plain dicts, exactly as on the real models.
    """

    def __init__(self, values: dict) -> None:
        for key, value in values.items():
            setattr(self, key, value)


def _card_kwargs(card: str, body: dict) -> dict:
    """Turn a fixture request body into MediaClient keyword arguments."""
    object_fields = {"config", "squad", "user_member_info"}
    object_list_fields = {"entries", "members", "squads", "transactions"}
    kwargs = {}
    for key, value in body.items():
        if key in object_fields and isinstance(value, dict):
            kwargs[key] = AttributeBag(value)
        elif key in object_list_fields and isinstance(value, list):
            kwargs[key] = [AttributeBag(item) for item in value]
        else:
            kwargs[key] = value
    return kwargs


@pytest.fixture
async def media_client():
    client = MediaClient(
        os.environ["MEDIA_SERVICE_URL"],
        os.environ.get("MEDIA_API_KEY", ""),
    )
    yield client
    await client.aclose()


async def test_health(media_client):
    health = await media_client.health()
    assert health["status"] == "ok"


@pytest.mark.parametrize("card", sorted(CARD_METHODS))
async def test_card_fixture_renders_png(media_client, card):
    body = json.loads((CARD_FIXTURES_DIR / f"{card}.json").read_text())
    method = getattr(media_client, CARD_METHODS[card])
    rendered = await method(**_card_kwargs(card, body))
    assert rendered.data.startswith(PNG_MAGIC)
    assert rendered.mime_type == "image/png"
    assert rendered.filename.endswith(".png")


async def test_latex_renders_png(media_client):
    rendered = await media_client.render_latex("E = mc^2")
    assert rendered.data.startswith(PNG_MAGIC)
    assert rendered.filename == "latex.png"


async def test_audio_transcodes_to_ogg(media_client):
    sample_rate = 24_000
    samples = [
        int(12_000 * math.sin(2 * math.pi * 440 * n / sample_rate))
        for n in range(sample_rate)
    ]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    ogg = await media_client.transcode_wav_to_opus_ogg(
        buffer.getvalue(), bitrate="48k"
    )
    assert ogg.startswith(OGG_MAGIC)
