"""Skrift notification publishing that also works in standalone worker CLIs."""

from __future__ import annotations

import asyncio
from typing import Any

from skrift.config import get_settings as get_skrift_settings
from skrift.lib.notification_backends import InMemoryBackend
from skrift.lib.notification_backends import load_backend
from skrift.notifications import NotificationMode
from skrift.notifications import notifications
from skrift.notifications import notify_user

from smarter_dev.shared.database import get_session_maker

_backend_lock = asyncio.Lock()


def _backend_is_started() -> bool:
    backend = notifications._backend  # noqa: SLF001 - Skrift exposes no status API.
    if backend is None or isinstance(backend, InMemoryBackend):
        return False
    # Production uses RedisBackend. Its client exists only after start().
    return getattr(backend, "_client", None) is not None


async def ensure_notification_backend() -> None:
    """Start Skrift's configured backend when running outside the ASGI app.

    Skrift's ASGI lifecycle starts this backend, but ``skrift workers run``
    currently does not. Without this bridge, notify_user() in a worker falls
    back to an in-memory backend and never reaches browser SSE connections.
    """
    if _backend_is_started():
        return
    async with _backend_lock:
        if _backend_is_started():
            return
        settings = get_skrift_settings()
        if not settings.notifications.backend:
            return
        backend_cls = load_backend(settings.notifications.backend)
        backend = backend_cls(
            settings=settings,
            session_maker=get_session_maker(),
        )
        notifications.set_backend(backend)
        await backend.start()


async def notify_chat_user(
    user_id: str,
    event_type: str,
    *,
    mode: NotificationMode = NotificationMode.EPHEMERAL,
    **payload: Any,
) -> None:
    """Publish a Chat event through Skrift's cross-replica backend."""
    await ensure_notification_backend()
    await notify_user(user_id, event_type, mode=mode, **payload)
