"""Render TeX expressions through the internal media service."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from smarter_dev.bot.services.media_client import MediaClient
from smarter_dev.bot.services.media_client import MediaRenderError
from smarter_dev.shared.config import Settings
from smarter_dev.shared.config import get_settings

logger = logging.getLogger(__name__)

MAX_LATEX_SOURCE_CHARS = 1_800
MAX_RENDERED_BYTES = 8 * 1024 * 1024


class LatexRenderError(RuntimeError):
    """Raised when the media service cannot produce a usable PNG."""


@dataclass(frozen=True)
class RenderedLatex:
    """An in-memory Discord attachment produced from one fenced equation."""

    data: bytes
    filename: str = "latex.png"
    mime_type: str = "image/png"


class LatexRenderer:
    """Turns fenced ``latex`` blocks into PNGs via the media service."""

    def __init__(
        self,
        media_client: MediaClient | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._injected_client = media_client
        self._settings = settings
        self._owned_client: MediaClient | None = None

    @property
    def media_client(self) -> MediaClient:
        """The media service client this renderer sends requests through."""
        if self._injected_client is not None:
            return self._injected_client
        if self._owned_client is None:
            self._owned_client = MediaClient.from_settings(
                self._settings or get_settings()
            )
        return self._owned_client

    async def initialize(self) -> None:
        """Confirm the media service is reachable before serving chat."""
        await self.media_client.health()
        logger.info("Media service is reachable for LaTeX rendering")

    async def render(self, source: str) -> RenderedLatex:
        """Render ``source`` as a PNG attachment."""
        source = source.strip()
        if not source:
            raise LatexRenderError("LaTeX source must not be empty")
        if len(source) > MAX_LATEX_SOURCE_CHARS:
            raise LatexRenderError(
                f"LaTeX source exceeds {MAX_LATEX_SOURCE_CHARS} characters"
            )

        try:
            rendered = await self.media_client.render_latex(source)
        except MediaRenderError as exc:
            raise LatexRenderError(str(exc)) from exc

        if not rendered.data or len(rendered.data) > MAX_RENDERED_BYTES:
            raise LatexRenderError("Rendered LaTeX image has an invalid size")
        return RenderedLatex(
            data=rendered.data,
            filename=rendered.filename,
            mime_type=rendered.mime_type,
        )

    async def close(self) -> None:
        """Dispose the media client, if this renderer built its own."""
        owned_client = self._owned_client
        self._owned_client = None
        if owned_client is not None:
            await owned_client.aclose()
