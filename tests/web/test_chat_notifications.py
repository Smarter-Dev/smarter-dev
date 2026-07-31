from __future__ import annotations

from types import SimpleNamespace

import pytest

from smarter_dev.web.chat import notifications as chat_notifications


@pytest.mark.asyncio
async def test_standalone_worker_starts_skrift_notification_backend(monkeypatch):
    service = SimpleNamespace(_backend=None)
    service.set_backend = lambda backend: setattr(service, "_backend", backend)

    class FakeBackend:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._client = None

        async def start(self):
            self._client = object()

    settings = SimpleNamespace(
        notifications=SimpleNamespace(backend="example:FakeBackend")
    )
    monkeypatch.setattr(chat_notifications, "notifications", service)
    monkeypatch.setattr(chat_notifications, "get_skrift_settings", lambda: settings)
    monkeypatch.setattr(chat_notifications, "load_backend", lambda _spec: FakeBackend)
    monkeypatch.setattr(chat_notifications, "get_session_maker", lambda: "sessions")

    await chat_notifications.ensure_notification_backend()

    assert isinstance(service._backend, FakeBackend)
    assert service._backend._client is not None
    assert service._backend.kwargs == {
        "settings": settings,
        "session_maker": "sessions",
    }
