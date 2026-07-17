"""Parity tests for the native (Litestar) image-quota API (unit U8).

Assert the wire contract of the ported ``routers/image_quota.py`` directly:
exact status codes and JSON for GET ``/quota``, POST ``/reserve``, and POST
``/release``, including the minute-precision ``resets_at`` rendering and the
``{"released": guild_id}`` release body. The controller runs the real
``ImageQuotaLimiter`` against an in-memory fake Redis (see ``conftest``), so the
counting / window semantics are exercised end-to-end. Paths carry the final
``/api`` prefix and mirror what the bot sends from
``smarter_dev/bot/agents/chat_tools.py``.
"""

from __future__ import annotations

import re

from litestar.testing import TestClient

from smarter_dev.web.image_quota import IMAGES_PER_HOUR

_GUILD = "123456789012345678"
_RESETS_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$")


class TestGetQuota:
    def test_fresh_guild_reports_full_budget(self, image_quota_client: TestClient):
        response = image_quota_client.get(
            "/api/image-generations/quota", params={"guild_id": _GUILD}
        )

        assert response.status_code == 200
        body = response.json()
        assert body == {
            "guild_id": _GUILD,
            "limit": IMAGES_PER_HOUR,
            "remaining": IMAGES_PER_HOUR,
            "resets_at": None,
            "retry_after_seconds": None,
            "granted": True,
        }

    def test_peek_does_not_spend(self, image_quota_client: TestClient):
        for _ in range(3):
            image_quota_client.get(
                "/api/image-generations/quota", params={"guild_id": _GUILD}
            )
        body = image_quota_client.get(
            "/api/image-generations/quota", params={"guild_id": _GUILD}
        ).json()
        assert body["remaining"] == IMAGES_PER_HOUR

    def test_missing_guild_id_is_422(self, image_quota_client: TestClient):
        response = image_quota_client.get("/api/image-generations/quota")
        assert response.status_code == 422


class TestReserve:
    def test_reserve_spends_and_fixes_window(self, image_quota_client: TestClient):
        response = image_quota_client.post(
            "/api/image-generations/reserve", json={"guild_id": _GUILD}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["guild_id"] == _GUILD
        assert body["granted"] is True
        assert body["remaining"] == IMAGES_PER_HOUR - 1
        # A window opened on first spend, so a reset time is now reported.
        assert _RESETS_AT.match(body["resets_at"])
        assert body["retry_after_seconds"] == 3600

    def test_reserve_denies_when_exhausted(self, image_quota_client: TestClient):
        for _ in range(IMAGES_PER_HOUR):
            image_quota_client.post(
                "/api/image-generations/reserve", json={"guild_id": _GUILD}
            )

        response = image_quota_client.post(
            "/api/image-generations/reserve", json={"guild_id": _GUILD}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["granted"] is False
        assert body["remaining"] == 0

    def test_missing_guild_id_is_422(self, image_quota_client: TestClient):
        response = image_quota_client.post(
            "/api/image-generations/reserve", json={}
        )
        assert response.status_code == 422


class TestRelease:
    def test_release_refunds_a_slot(self, image_quota_client: TestClient):
        for _ in range(IMAGES_PER_HOUR):
            image_quota_client.post(
                "/api/image-generations/reserve", json={"guild_id": _GUILD}
            )
        assert (
            image_quota_client.get(
                "/api/image-generations/quota", params={"guild_id": _GUILD}
            ).json()["remaining"]
            == 0
        )

        response = image_quota_client.post(
            "/api/image-generations/release", json={"guild_id": _GUILD}
        )

        assert response.status_code == 200
        assert response.json() == {"released": _GUILD}
        assert (
            image_quota_client.get(
                "/api/image-generations/quota", params={"guild_id": _GUILD}
            ).json()["remaining"]
            == 1
        )

    def test_release_when_nothing_reserved_is_noop(self, image_quota_client: TestClient):
        response = image_quota_client.post(
            "/api/image-generations/release", json={"guild_id": _GUILD}
        )

        assert response.status_code == 200
        assert response.json() == {"released": _GUILD}
        assert (
            image_quota_client.get(
                "/api/image-generations/quota", params={"guild_id": _GUILD}
            ).json()["remaining"]
            == IMAGES_PER_HOUR
        )

    def test_missing_guild_id_is_422(self, image_quota_client: TestClient):
        response = image_quota_client.post(
            "/api/image-generations/release", json={}
        )
        assert response.status_code == 422
