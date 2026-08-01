from __future__ import annotations

from types import SimpleNamespace

import pytest
from skrift.notifications import NotificationService

from smarter_dev.web.chat import notifications as chat_notifications


@pytest.mark.asyncio
async def test_standalone_entrypoint_starts_skrift_notification_backend(monkeypatch):
    service = NotificationService()
    created: list = []

    class FakeBackend:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            created.append(self)

        def on_remote_message(self, callback):
            self.callback = callback

        async def start(self):
            self.started = True

    settings = SimpleNamespace(
        notifications=SimpleNamespace(backend="example:FakeBackend")
    )
    monkeypatch.setattr(chat_notifications, "notifications", service)
    monkeypatch.setattr("skrift.config.get_settings", lambda: settings)
    monkeypatch.setattr(
        "skrift.lib.notification_backends.load_backend", lambda _spec: FakeBackend
    )
    monkeypatch.setattr(chat_notifications, "get_session_maker", lambda: "sessions")

    assert not service.backend_started

    await chat_notifications.ensure_notification_backend()

    assert service.backend_started
    assert len(created) == 1
    assert created[0].started
    assert created[0].kwargs == {"settings": settings, "session_maker": "sessions"}

    # Idempotent: a second call must not build a second backend.
    await chat_notifications.ensure_notification_backend()
    assert len(created) == 1
