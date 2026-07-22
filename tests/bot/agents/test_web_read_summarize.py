"""Tests for ``web_read``'s instruction-guided summarization path.

The fetch backends and the summarizer model are mocked — these verify the
plumbing: content is routed to the summarizer with the caller's instruction,
huge reads are truncated, and failure/empty cases short-circuit without
calling the (paid) summarizer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from smarter_dev.bot.agents import chat_tools
from smarter_dev.bot.agents import web_summarizer
from smarter_dev.bot.agents.chat_tools import MAX_READ_CHARS
from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.chat_tools import web_read


def _ctx() -> SimpleNamespace:
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = AsyncMock()
    return SimpleNamespace(deps=ChatDeps(bot=bot, channel_id=1, guild_id=2))


def test_web_summarizer_prompt_favors_quotes_for_extraction():
    assert "default to brief verbatim excerpts" in web_summarizer.SYSTEM_PROMPT
    assert "enclosed in quotation marks" in web_summarizer.SYSTEM_PROMPT
    assert "Do not silently paraphrase" in web_summarizer.SYSTEM_PROMPT
    assert "MUST contain at least one relevant" in web_summarizer.SYSTEM_PROMPT
    assert "without quotation marks does not satisfy" in (web_summarizer.SYSTEM_PROMPT)
    assert "find, extract, identify, or report specific details" in (
        web_summarizer.SYSTEM_PROMPT
    )
    assert "explicitly asks for a summary" in web_summarizer.SYSTEM_PROMPT


def test_web_summarizer_uses_laguna_primary_and_low_thinking_gemini_fallback():
    primary = MagicMock()
    fallback = MagicMock()
    with (
        patch.object(
            web_summarizer, "build_model_for", side_effect=[primary, fallback]
        ),
        patch.object(web_summarizer, "Agent") as agent_class,
    ):
        web_summarizer._build_agent(web_summarizer.PRIMARY_MODEL_KEY)
        web_summarizer._build_agent(
            web_summarizer.FALLBACK_MODEL_KEY,
            reasoning_level=web_summarizer.ReasoningLevel.LOW,
        )

    assert primary is not fallback
    primary_call, fallback_call = agent_class.call_args_list
    assert primary_call.args == (primary,)
    assert primary_call.kwargs["model_settings"] is None
    assert fallback_call.args == (fallback,)
    assert fallback_call.kwargs["model_settings"]["google_thinking_config"] == {
        "thinking_level": "LOW"
    }


@pytest.mark.asyncio
async def test_web_read_summarizes_page():
    ctx = _ctx()
    with (
        patch.object(
            chat_tools.web_fetch,
            "fetch_via_jina",
            AsyncMock(
                return_value={
                    "title": "T",
                    "description": "d",
                    "content": "BODY",
                    "url": "u",
                }
            ),
        ),
        patch.object(
            chat_tools, "summarize_web_content", AsyncMock(return_value="SUMMARY")
        ) as summ,
    ):
        out = await web_read(ctx, "https://example.com", "find X")

    assert out == {"url": "https://example.com", "title": "T", "summary": "SUMMARY"}
    summ.assert_awaited_once()
    kwargs = summ.call_args.kwargs
    assert kwargs["instruction"] == "find X"
    assert kwargs["content"] == "BODY"
    assert kwargs["title"] == "T"


@pytest.mark.asyncio
async def test_web_read_truncates_over_limit():
    ctx = _ctx()
    big = "x" * (MAX_READ_CHARS + 500)
    with (
        patch.object(
            chat_tools.web_fetch,
            "fetch_via_jina",
            AsyncMock(
                return_value={
                    "title": "",
                    "description": "",
                    "content": big,
                    "url": "u",
                }
            ),
        ),
        patch.object(
            chat_tools, "summarize_web_content", AsyncMock(return_value="S")
        ) as summ,
    ):
        await web_read(ctx, "https://e.com", "i")

    assert len(summ.call_args.kwargs["content"]) == MAX_READ_CHARS


@pytest.mark.asyncio
async def test_web_read_at_limit_not_truncated():
    ctx = _ctx()
    exact = "y" * MAX_READ_CHARS
    with (
        patch.object(
            chat_tools.web_fetch,
            "fetch_via_jina",
            AsyncMock(
                return_value={
                    "title": "",
                    "description": "",
                    "content": exact,
                    "url": "u",
                }
            ),
        ),
        patch.object(
            chat_tools, "summarize_web_content", AsyncMock(return_value="S")
        ) as summ,
    ):
        await web_read(ctx, "https://e.com", "i")

    assert len(summ.call_args.kwargs["content"]) == MAX_READ_CHARS


@pytest.mark.asyncio
async def test_web_read_fetch_failure_skips_summary():
    ctx = _ctx()
    with (
        patch.object(
            chat_tools.web_fetch, "fetch_via_jina", AsyncMock(return_value=None)
        ),
        patch.object(chat_tools, "summarize_web_content", AsyncMock()) as summ,
    ):
        out = await web_read(ctx, "https://e.com", "i")

    assert out["error"] == "fetch_failed"
    assert out["summary"] == ""
    summ.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_read_empty_content_skips_summary():
    ctx = _ctx()
    with (
        patch.object(
            chat_tools.web_fetch,
            "fetch_via_jina",
            AsyncMock(
                return_value={
                    "title": "T",
                    "description": "",
                    "content": "   ",
                    "url": "u",
                }
            ),
        ),
        patch.object(chat_tools, "summarize_web_content", AsyncMock()) as summ,
    ):
        out = await web_read(ctx, "https://e.com", "i")

    assert out["error"] == "no_content"
    summ.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_read_youtube_summarizes_description():
    ctx = _ctx()
    with (
        patch.object(chat_tools.web_fetch, "is_youtube_url", return_value=True),
        patch.object(
            chat_tools.web_fetch,
            "fetch_youtube_metadata",
            AsyncMock(return_value={"title": "V", "description": "desc"}),
        ),
        patch.object(
            chat_tools, "summarize_web_content", AsyncMock(return_value="S")
        ) as summ,
    ):
        out = await web_read(ctx, "https://youtu.be/x", "i")

    assert out["title"] == "V"
    assert out["summary"] == "S"
    assert summ.call_args.kwargs["content"] == "desc"


@pytest.mark.asyncio
async def test_web_read_pdf_passes_limit_and_summarizes():
    ctx = _ctx()
    fetch_pdf = AsyncMock(return_value="pdf text")
    with (
        patch.object(chat_tools.web_fetch, "fetch_pdf_text", fetch_pdf),
        patch.object(
            chat_tools, "summarize_web_content", AsyncMock(return_value="S")
        ) as summ,
    ):
        out = await web_read(ctx, "https://e.com/file.PDF", "i")

    fetch_pdf.assert_awaited_once()
    assert fetch_pdf.call_args.kwargs.get("max_chars") == MAX_READ_CHARS
    assert summ.call_args.kwargs["content"] == "pdf text"
    assert out["summary"] == "S"


@pytest.mark.asyncio
async def test_web_read_resolves_registered_escaped_url():
    """A URL we escaped when rendering resolves back to its exact original."""
    from smarter_dev.bot.agents.url_registry import register_escaped_url

    ctx = _ctx()
    captured: dict = {}

    async def fake_fetch(u, **kw):
        captured["url"] = u
        return (b"AUD", "audio/ogg")

    original = "https://cdn.discordapp.com/a/voice.ogg?ex=1&is=2&hm=abc"
    escaped = "https://cdn.discordapp.com/a/voice.ogg?ex=1&amp;is=2&amp;hm=abc"
    register_escaped_url(original)

    with (
        patch.object(chat_tools.web_fetch, "fetch_bytes", fake_fetch),
        patch.object(chat_tools, "describe_media", AsyncMock(return_value="ok")),
    ):
        out = await web_read(ctx, escaped, "transcribe")

    assert captured["url"] == original
    assert out["url"] == original


@pytest.mark.asyncio
async def test_web_read_passes_unregistered_url_through_untouched():
    """A URL we never escaped (e.g. from search) is fetched verbatim — a
    legitimate &amp; in it must not be mangled."""
    ctx = _ctx()
    captured: dict = {}

    async def fake_fetch(u, **kw):
        captured["url"] = u
        return (b"IMG", "image/png")

    # Never registered — contains a literal &amp; that is part of the real URL.
    weird = "https://example.com/img.png?a=1&amp;b=2"
    with (
        patch.object(chat_tools.web_fetch, "fetch_bytes", fake_fetch),
        patch.object(chat_tools, "describe_media", AsyncMock(return_value="ok")),
    ):
        await web_read(ctx, weird, "describe")

    assert captured["url"] == weird


@pytest.mark.asyncio
async def test_web_read_image_routes_to_media_reader():
    ctx = _ctx()
    with (
        patch.object(
            chat_tools.web_fetch,
            "fetch_bytes",
            AsyncMock(return_value=(b"IMG", "image/png")),
        ),
        patch.object(
            chat_tools, "describe_media", AsyncMock(return_value="a red square")
        ) as dm,
    ):
        out = await web_read(ctx, "https://cdn.discord/x/pic.png?ex=1", "what is this")

    assert out == {
        "url": "https://cdn.discord/x/pic.png?ex=1",
        "kind": "image",
        "summary": "a red square",
    }
    kwargs = dm.call_args.kwargs
    assert kwargs["kind"] == "image"
    assert kwargs["media_type"] == "image/png"
    assert kwargs["data"] == b"IMG"
    assert kwargs["instruction"] == "what is this"


@pytest.mark.asyncio
async def test_web_read_audio_routes_to_media_reader():
    ctx = _ctx()
    with (
        patch.object(
            chat_tools.web_fetch,
            "fetch_bytes",
            AsyncMock(return_value=(b"AUD", "audio/ogg")),
        ),
        patch.object(
            chat_tools, "describe_media", AsyncMock(return_value="someone says hi")
        ) as dm,
    ):
        out = await web_read(
            ctx, "https://cdn.discord/x/voice-message.ogg?ex=1", "transcribe"
        )

    assert out["kind"] == "audio"
    assert out["summary"] == "someone says hi"
    assert dm.call_args.kwargs["media_type"] == "audio/ogg"


@pytest.mark.asyncio
async def test_web_read_media_fetch_failure_skips_reader():
    ctx = _ctx()
    with (
        patch.object(chat_tools.web_fetch, "fetch_bytes", AsyncMock(return_value=None)),
        patch.object(chat_tools, "describe_media", AsyncMock()) as dm,
    ):
        out = await web_read(ctx, "https://x/pic.png", "i")

    assert out["error"] == "fetch_failed"
    assert out["kind"] == "image"
    dm.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_read_media_reader_error_is_caught():
    ctx = _ctx()
    with (
        patch.object(
            chat_tools.web_fetch,
            "fetch_bytes",
            AsyncMock(return_value=(b"x", "image/png")),
        ),
        patch.object(
            chat_tools, "describe_media", AsyncMock(side_effect=RuntimeError("boom"))
        ),
    ):
        out = await web_read(ctx, "https://x/pic.png", "i")

    assert out["error"] == "media_read_failed"


@pytest.mark.asyncio
async def test_web_read_media_type_falls_back_to_extension():
    ctx = _ctx()
    with (
        patch.object(
            chat_tools.web_fetch, "fetch_bytes", AsyncMock(return_value=(b"x", ""))
        ),
        patch.object(chat_tools, "describe_media", AsyncMock(return_value="ok")) as dm,
    ):
        await web_read(ctx, "https://x/pic.jpg", "i")

    assert dm.call_args.kwargs["media_type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_summarize_web_content_builds_prompt_and_calls_agent():
    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=SimpleNamespace(output="OUT"))
    with patch.object(
        web_summarizer, "get_web_summarizer_agent", return_value=fake_agent
    ):
        res = await web_summarizer.summarize_web_content(
            instruction="look for prices", content="BODY", title="T", url="U"
        )

    assert res == "OUT"
    prompt = fake_agent.run.call_args.args[0]
    assert "look for prices" in prompt
    assert "BODY" in prompt
    assert "U" in prompt


@pytest.mark.asyncio
async def test_summarize_web_content_fails_over_to_gemini_and_logs_critical(caplog):
    primary_agent = MagicMock()
    primary_agent.run = AsyncMock(side_effect=RuntimeError("Laguna unavailable"))
    fallback_agent = MagicMock()
    fallback_agent.run = AsyncMock(return_value=SimpleNamespace(output="FALLBACK"))

    with (
        patch.object(
            web_summarizer, "get_web_summarizer_agent", return_value=primary_agent
        ),
        patch.object(
            web_summarizer,
            "get_web_summarizer_fallback_agent",
            return_value=fallback_agent,
        ),
        patch.object(web_summarizer, "record_llm_failover") as record_failover,
        caplog.at_level("CRITICAL", logger=web_summarizer.__name__),
    ):
        result = await web_summarizer.summarize_web_content(
            instruction="summarize",
            content="BODY",
            title="Example",
            url="https://e.test",
        )

    assert result == "FALLBACK"
    fallback_agent.run.assert_awaited_once_with(primary_agent.run.call_args.args[0])
    assert "WEB SUMMARIZER FAILOVER" in caplog.text
    assert "Laguna S 2.1 failed" in caplog.text
    assert "using Gemini 3.1 Flash Lite" in caplog.text
    assert "https://e.test" in caplog.text
    assert "Laguna unavailable" in caplog.text
    record_failover.assert_called_once()
    assert record_failover.call_args.kwargs == {
        "operation": "web_summarizer",
        "primary_model": "poolside/laguna-s-2.1",
        "fallback_model": "gemini-3.1-flash-lite",
        "error": primary_agent.run.side_effect,
    }
