"""The LaTeX renderer delegates to the media service."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from smarter_dev.bot.services.latex_renderer import MAX_LATEX_SOURCE_CHARS
from smarter_dev.bot.services.latex_renderer import MAX_RENDERED_BYTES
from smarter_dev.bot.services.latex_renderer import LatexRenderer
from smarter_dev.bot.services.latex_renderer import LatexRenderError
from smarter_dev.bot.services.media_client import MediaRenderError
from smarter_dev.bot.services.media_client import MediaServiceUnavailableError
from smarter_dev.bot.services.media_client import RenderedMedia
from smarter_dev.shared.config import Settings

PNG = b"\x89PNG\r\n\x1a\nfake"


def _media_client(data: bytes = PNG) -> AsyncMock:
    client = AsyncMock()
    client.render_latex.return_value = RenderedMedia(
        data=data, filename="latex.png", mime_type="image/png"
    )
    return client


async def test_render_sends_the_trimmed_source_and_returns_the_png():
    client = _media_client()

    rendered = await LatexRenderer(client).render("  E = mc^2\n")

    client.render_latex.assert_awaited_once_with("E = mc^2")
    assert rendered.data == PNG
    assert rendered.filename == "latex.png"
    assert rendered.mime_type == "image/png"


async def test_empty_source_never_reaches_the_service():
    client = _media_client()

    with pytest.raises(LatexRenderError):
        await LatexRenderer(client).render("   ")

    client.render_latex.assert_not_awaited()


async def test_oversized_source_never_reaches_the_service():
    client = _media_client()

    with pytest.raises(LatexRenderError):
        await LatexRenderer(client).render("x" * (MAX_LATEX_SOURCE_CHARS + 1))

    client.render_latex.assert_not_awaited()


async def test_render_rejection_becomes_a_latex_render_error():
    client = AsyncMock()
    client.render_latex.side_effect = MediaRenderError(
        "TeX parse error", code="render_failed", status_code=422
    )

    with pytest.raises(LatexRenderError, match="TeX parse error"):
        await LatexRenderer(client).render("\\foo")


async def test_service_outage_propagates_untranslated():
    client = AsyncMock()
    client.render_latex.side_effect = MediaServiceUnavailableError("media is down")

    with pytest.raises(MediaServiceUnavailableError):
        await LatexRenderer(client).render("x")


async def test_empty_image_is_rejected():
    client = _media_client(data=b"")

    with pytest.raises(LatexRenderError):
        await LatexRenderer(client).render("x")


async def test_oversized_image_is_rejected():
    client = _media_client(data=b"0" * (MAX_RENDERED_BYTES + 1))

    with pytest.raises(LatexRenderError):
        await LatexRenderer(client).render("x")


async def test_initialize_health_checks_the_service():
    client = _media_client()

    await LatexRenderer(client).initialize()

    client.health.assert_awaited_once()
    client.render_latex.assert_not_awaited()


async def test_close_leaves_an_injected_client_alone():
    client = _media_client()

    await LatexRenderer(client).close()

    client.aclose.assert_not_awaited()


async def test_close_disposes_a_client_it_built_itself(monkeypatch):
    client = _media_client()
    monkeypatch.setattr(
        "smarter_dev.bot.services.latex_renderer.MediaClient.from_settings",
        classmethod(lambda cls, settings: client),
    )

    renderer = LatexRenderer(
        settings=Settings(
            media_service_url="http://media.test:8080", media_api_key="mk_test"
        )
    )
    await renderer.render("x")
    await renderer.close()

    client.aclose.assert_awaited_once()
