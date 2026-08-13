"""Send rendered cards with a plain-text fallback when the media service is down.

Card attachments render lazily: :mod:`smarter_dev.bot.utils.image_embeds` hands
back a ``hikari.files.Bytes`` whose data is fetched from the media service while
hikari uploads it. A media outage therefore surfaces as an exception raised from
``respond`` itself, and most error paths wrap ``respond`` in a broad ``except``
meant only to tolerate an already-acknowledged interaction — so the user would
see nothing at all.

These helpers resolve the card *before* talking to Discord. If the media service
is unreachable the message still goes out, as plain text.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

import hikari

from smarter_dev.bot.services.media_client import MediaServiceError
from smarter_dev.bot.utils.image_embeds import get_generator

logger = logging.getLogger(__name__)


async def respond_with_card(
    respond: Callable[..., Awaitable[Any]],
    card: hikari.files.Bytes,
    fallback_text: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Send ``card`` through ``respond``, falling back to ``fallback_text``.

    Args:
        respond: The Discord send call, e.g. ``ctx.respond`` or
            ``interaction.create_initial_response``.
        card: A lazily rendered card attachment.
        fallback_text: Text to send instead if the media service is unavailable.
        *args: Leading positional arguments for ``respond`` (a response type).
        **kwargs: Remaining arguments for ``respond`` (flags, components, ...).
    """
    try:
        await card.read()
    except MediaServiceError:
        logger.warning(
            "Media service unavailable; sending a plain-text message instead",
            exc_info=True,
        )
        return await respond(*args, content=fallback_text, **kwargs)
    return await respond(*args, attachment=card, **kwargs)


async def respond_with_error_card(
    respond: Callable[..., Awaitable[Any]],
    message: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Report ``message`` as an error card, or as plain text if rendering fails."""
    return await respond_with_card(
        respond,
        get_generator().create_error_embed(message),
        message,
        *args,
        **kwargs,
    )
