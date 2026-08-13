"""Voice messages transcode through the media service, not a local ffmpeg."""

from __future__ import annotations

import io
import wave
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from smarter_dev.bot.services import discord_voice
from smarter_dev.bot.services.discord_voice import TTSResult
from smarter_dev.bot.services.discord_voice import TTSUsage
from smarter_dev.bot.services.discord_voice import VoiceService
from smarter_dev.bot.services.discord_voice import build_wave_bytes
from smarter_dev.bot.services.media_client import MediaServiceUnavailableError
from smarter_dev.shared.config import Settings

PCM = b"\x01\x02" * 1200


def test_build_wave_bytes_produces_a_readable_wav_container():
    wav = build_wave_bytes(PCM, sample_rate=24000, channels=1, sample_width=2)

    assert wav.startswith(b"RIFF")
    with wave.open(io.BytesIO(wav), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 24000
        assert reader.readframes(reader.getnframes()) == PCM


def test_local_ffmpeg_helpers_are_gone():
    for removed in ("convert_wav_to_opus_ogg", "get_ffmpeg_exe", "write_wave_file"):
        assert not hasattr(discord_voice, removed)


def _voice_service(media_client) -> VoiceService:
    return VoiceService(
        api_client=MagicMock(),
        settings=Settings(
            media_service_url="http://media.test:8080",
            media_api_key="mk_test",
            voice_opus_bitrate="48k",
        ),
        media_client=media_client,
    )


async def test_synthesize_and_send_transcodes_through_the_media_service(monkeypatch):
    media_client = AsyncMock()
    media_client.transcode_wav_to_opus_ogg.return_value = b"OggS-audio"
    sent: list[dict] = []

    monkeypatch.setattr(
        discord_voice,
        "generate_tts",
        lambda *args, **kwargs: TTSResult(pcm=PCM, usage=TTSUsage(model_name="m")),
    )

    async def fake_send(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(discord_voice, "send_voice_message", fake_send)

    bot = MagicMock()
    bot._token = "bot-token"
    usage = await _voice_service(media_client).synthesize_and_send(
        bot, channel_id=7, text="hello there"
    )

    assert usage.model_name == "m"
    call = media_client.transcode_wav_to_opus_ogg.await_args
    assert call.args[0].startswith(b"RIFF")
    assert call.kwargs == {"bitrate": "48k"}
    assert sent[0]["ogg_bytes"] == b"OggS-audio"
    assert sent[0]["channel_id"] == 7


async def test_health_check_reports_the_media_service(monkeypatch):
    media_client = AsyncMock()
    media_client.health.return_value = {"status": "ok"}
    monkeypatch.setattr(
        discord_voice,
        "get_gemini_client_for_tts",
        lambda model: (MagicMock(), model),
    )

    health = await _voice_service(media_client).health_check()

    assert health.is_healthy is True
    media_client.health.assert_awaited_once()


async def test_health_check_fails_when_media_is_unreachable(monkeypatch):
    media_client = AsyncMock()
    media_client.health.side_effect = MediaServiceUnavailableError("down")
    monkeypatch.setattr(
        discord_voice,
        "get_gemini_client_for_tts",
        lambda model: (MagicMock(), model),
    )

    health = await _voice_service(media_client).health_check()

    assert health.is_healthy is False
    assert "down" in health.details["error"]


def test_voice_service_builds_a_media_client_from_settings_when_not_given():
    service = VoiceService(
        api_client=MagicMock(),
        settings=Settings(
            media_service_url="http://media.test:8080", media_api_key="mk_test"
        ),
    )

    assert service.media_client.base_url == "http://media.test:8080"


def test_voice_service_fails_fast_when_media_is_not_configured():
    service = VoiceService(
        api_client=MagicMock(),
        settings=Settings(media_service_url="", media_api_key=""),
    )

    with pytest.raises(ValueError):
        service.media_client
