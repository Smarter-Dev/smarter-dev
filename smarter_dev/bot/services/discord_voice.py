"""Discord voice message service."""

from __future__ import annotations

import asyncio
import base64
import math
import re
import shutil
import struct
import subprocess
import tempfile
import wave
from datetime import UTC
from datetime import datetime
from pathlib import Path

import aiohttp
from google.genai import types

from smarter_dev.bot.services.base import APIClientProtocol
from smarter_dev.bot.services.base import BaseService
from smarter_dev.bot.services.base import CacheManagerProtocol
from smarter_dev.bot.services.models import ServiceHealth
from smarter_dev.llm_config import get_gemini_client_for_tts
from smarter_dev.shared.config import Settings
from smarter_dev.shared.config import get_settings

DISCORD_API_BASE = "https://discord.com/api/v10"
VOICE_MESSAGE_FLAG = 8192
MARKDOWN_RE = re.compile(r"[*_`>#]")


async def discord_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    token: str,
    *,
    json: dict | None = None,
) -> dict:
    async with session.request(
        method,
        url,
        json=json,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        },
    ) as resp:
        text = await resp.text()
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(f"Discord error {resp.status}: {text}")
        return await resp.json()


async def send_voice_message(
    token: str,
    channel_id: int,
    ogg_bytes: bytes,
    duration: float,
    waveform: str,
    reply_to_message_id: int | None = None,
) -> None:
    file_size = len(ogg_bytes)

    async with aiohttp.ClientSession() as session:
        res = await discord_request(
            session,
            "POST",
            f"{DISCORD_API_BASE}/channels/{channel_id}/attachments",
            token,
            json={
                "files": [{
                    "filename": "voice-message.ogg",
                    "file_size": file_size,
                    "id": "0",
                }]
            },
        )
        attachment = res["attachments"][0]

        async with session.put(
            attachment["upload_url"],
            data=ogg_bytes,
            headers={"Content-Type": "audio/ogg"},
        ) as up:
            if up.status < 200 or up.status >= 300:
                raise RuntimeError(await up.text())

        message_payload = {
            "flags": VOICE_MESSAGE_FLAG,
            "attachments": [{
                "id": "0",
                "filename": "voice-message.ogg",
                "uploaded_filename": attachment["upload_filename"],
                "duration_secs": duration,
                "waveform": waveform,
            }],
        }
        if reply_to_message_id:
            message_payload["message_reference"] = {
                "channel_id": str(channel_id),
                "message_id": str(reply_to_message_id),
            }

        await discord_request(
            session,
            "POST",
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            token,
            json=message_payload,
        )


def write_wave_file(
    filename: Path,
    pcm: bytes,
    sample_rate: int,
    channels: int,
    sample_width: int,
) -> None:
    with wave.open(str(filename), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def pcm_duration_secs(
    pcm: bytes,
    sample_rate: int,
    channels: int,
    sample_width: int,
) -> float:
    bytes_per_second = sample_rate * channels * sample_width
    return len(pcm) / bytes_per_second


async def convert_wav_to_opus_ogg(
    wav_path: Path,
    ogg_path: Path,
    bitrate: str,
) -> None:
    ffmpeg = get_ffmpeg_exe()
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-i",
        str(wav_path),
        "-c:a",
        "libopus",
        "-b:a",
        bitrate,
        "-vbr",
        "on",
        str(ogg_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        error = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"ffmpeg failed: {error}")


def get_ffmpeg_exe() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg

    try:
        import imageio_ffmpeg
    except ImportError as e:
        raise RuntimeError(
            "ffmpeg is required for Discord voice messages. Install ffmpeg or "
            "the imageio-ffmpeg package."
        ) from e

    return imageio_ffmpeg.get_ffmpeg_exe()


def waveform_from_pcm(
    pcm: bytes,
    sample_width: int,
    samples: int = 128,
) -> str:
    if not pcm:
        return base64.b64encode(bytes([0] * samples)).decode("ascii")

    frame_count = len(pcm) // sample_width
    amplitudes = struct.unpack(
        f"<{frame_count}h",
        pcm[: frame_count * sample_width],
    )
    bucket_size = max(1, math.ceil(frame_count / samples))
    waveform = []

    for offset in range(0, frame_count, bucket_size):
        bucket = amplitudes[offset : offset + bucket_size]
        if bucket:
            peak = max(abs(sample) for sample in bucket)
            waveform.append(min(255, round((peak / 32767) * 255)))

    waveform = waveform[:samples]
    if len(waveform) < samples:
        waveform.extend([0] * (samples - len(waveform)))

    return base64.b64encode(bytes(waveform)).decode("ascii")


def generate_tts_pcm(
    transcript: str,
    model_name: str,
    voice_name: str,
) -> bytes:
    client, model = get_gemini_client_for_tts(model_name)
    prompt = f"Synthesize speech:\n{transcript}"
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name,
                    )
                )
            ),
        ),
    )

    part = response.candidates[0].content.parts[0]

    if not part.inline_data:
        raise ValueError(f"No audio returned: {response}")

    data = part.inline_data.data
    if isinstance(data, bytes):
        return data

    data += "=" * (-len(data) % 4)
    return base64.b64decode(data)


def clean_transcript_for_tts(text: str) -> str:
    text = MARKDOWN_RE.sub("", text)
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace("$", " dollars ").replace("%", " percent ")
    lines = [line.strip(" -•\t") for line in text.splitlines()]
    return " ".join(line for line in lines if line).strip()


class VoiceService(BaseService):
    def __init__(
        self,
        api_client: APIClientProtocol,
        cache_manager: CacheManagerProtocol | None = None,
        settings: Settings | None = None,
    ):
        super().__init__(api_client, cache_manager, service_name="VoiceService")
        self._settings = settings or get_settings()

    async def synthesize_and_send(
        self,
        bot,
        channel_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> None:
        self._log_operation("synthesize_and_send", channel_id=channel_id)

        text = text.strip()
        if not text:
            raise ValueError("Cannot synthesize an empty voice response")
        text = clean_transcript_for_tts(text)[: self._settings.voice_max_input_chars]
        token = getattr(bot, "_token", None) or getattr(bot, "token", None)
        if not token:
            token = self._settings.discord_bot_token

        try:
            pcm = await asyncio.to_thread(
                generate_tts_pcm,
                text,
                self._settings.voice_tts_model,
                self._settings.voice_tts_voice,
            )
        except Exception:
            self._logger.warning("TTS generation failed, retrying shorter text", exc_info=True)
            pcm = await asyncio.to_thread(
                generate_tts_pcm,
                text[:400],
                self._settings.voice_tts_model,
                self._settings.voice_tts_voice,
            )

        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "voice.wav"
            ogg = Path(tmp) / "voice.ogg"

            await asyncio.to_thread(
                write_wave_file,
                wav,
                pcm,
                self._settings.voice_tts_sample_rate,
                self._settings.voice_tts_channels,
                self._settings.voice_tts_sample_width,
            )
            await convert_wav_to_opus_ogg(
                wav,
                ogg,
                self._settings.voice_opus_bitrate,
            )

            await send_voice_message(
                token=str(token),
                channel_id=channel_id,
                ogg_bytes=ogg.read_bytes(),
                duration=pcm_duration_secs(
                    pcm,
                    self._settings.voice_tts_sample_rate,
                    self._settings.voice_tts_channels,
                    self._settings.voice_tts_sample_width,
                ),
                waveform=waveform_from_pcm(
                    pcm,
                    self._settings.voice_tts_sample_width,
                ),
                reply_to_message_id=reply_to_message_id,
            )

    async def health_check(self) -> ServiceHealth:
        try:
            _, model = get_gemini_client_for_tts(self._settings.voice_tts_model)
            get_ffmpeg_exe()
            return ServiceHealth(
                service_name=self.service_name,
                is_healthy=True,
                last_check=datetime.now(UTC),
                details={
                    "tts_model": model,
                    "tts_voice": self._settings.voice_tts_voice,
                    "max_input_chars": self._settings.voice_max_input_chars,
                },
            )
        except Exception as e:
            return ServiceHealth(
                service_name=self.service_name,
                is_healthy=False,
                last_check=datetime.now(UTC),
                details={"error": str(e)},
            )


_voice_service: VoiceService | None = None


def initialize_voice_service() -> VoiceService:
    global _voice_service

    from smarter_dev.bot.services.api_client import APIClient

    settings = get_settings()

    _voice_service = VoiceService(
        api_client=APIClient(
            base_url=settings.api_base_url,
            api_key=settings.bot_api_key,
        ),
        settings=settings,
    )

    return _voice_service


def get_voice_service() -> VoiceService:
    global _voice_service

    if _voice_service is None:
        _voice_service = initialize_voice_service()

    return _voice_service
