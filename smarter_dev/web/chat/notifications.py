"""Skrift notification publishing that also works outside the ASGI app."""

from __future__ import annotations

from typing import Any

from skrift.notifications import NotificationMode
from skrift.notifications import notifications
from skrift.notifications import notify_user

from smarter_dev.shared.database import get_session_maker


async def ensure_notification_backend() -> None:
    """Start Skrift's configured backend when running outside the ASGI app.

    ``skrift workers run`` owns the backend lifecycle as of Skrift 0.2.0a18;
    this covers scripts and other nonstandard entrypoints. Idempotent no-op
    once a backend is running.
    """
    await notifications.ensure_backend_started(session_maker=get_session_maker())


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
