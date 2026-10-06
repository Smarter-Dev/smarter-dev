"""HTTP-level tests for the privacy bot API (``/api/privacy``), real guards on.

Every route sits behind the bot-API key. A missing key, an unknown ``sk_`` key
and a valid key without the ``bot-api`` permission are all refused before the
handler runs. Skrift's guard answers 401 for each, including "insufficient
permissions" (it raises ``NotAuthorizedException``, never 403).

Also: an ack for an unknown run deletes that run's stream entry before 404.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import fakeredis.aioredis
import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient
from litestar.testing import create_test_client
from skrift.auth.services import UserPermissions

from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.shared.privacy_purge import PURGE_STREAM
from smarter_dev.web.api_native import privacy as privacy_module
from smarter_dev.web.api_native.privacy import PrivacyController

_GUILD = "123456789012345678"
_GOOD_KEY = "sk_" + "g" * 40
_WRONG_KEY = "sk_" + "w" * 40
_ACK = {"component": "bot", "guild_id": _GUILD, "outcome": "purged"}


@contextlib.asynccontextmanager
async def _null_session():
    yield Mock()


@pytest.fixture
def guarded_client() -> Iterator[TestClient]:
    async def verify(db_session, bearer, client_ip=None):
        return SimpleKey() if bearer == _GOOD_KEY else None

    class SimpleKey:
        user_id = uuid.uuid4()
        scoped_permission_list: list[str] = []
        scoped_role_list: list[str] = []

    async def permissions(session, user_id):
        # A real key, but its user lacks ``bot-api``.
        return UserPermissions(user_id=str(user_id), permissions={"view-drafts"})

    logger_mock = Mock()
    logger_mock.log_authentication_failed = AsyncMock(return_value=None)
    with (
        patch("skrift.db.services.api_key_service.verify_api_key", side_effect=verify),
        patch("skrift.auth.services.get_user_permissions", side_effect=permissions),
        patch("smarter_dev.web.security_logger.get_security_logger", return_value=logger_mock),
        create_test_client(
            route_handlers=[PrivacyController],
            plugins=[PydanticPlugin()],
            dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
        ) as client,
    ):
        client.app.state.session_maker_class = _null_session
        yield client


_ROUTES = [
    ("get", "/api/privacy/blocked-users", None),
    ("post", f"/api/privacy/purges/{uuid.uuid4()}/acks", _ACK),
    ("post", "/api/privacy/opt-out/state", {"discord_user_id": "1" * 18}),
    ("put", "/api/privacy/opt-out", {"discord_user_id": "1" * 18, "opted_out": True}),
]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    _ROUTES,
    ids=["blocked-users", "acks", "opt-out-state", "opt-out-change"],
)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": f"Bearer {_WRONG_KEY}"}, {"Authorization": f"Bearer {_GOOD_KEY}"}],
    ids=["no-key", "wrong-key", "key-without-bot-api"],
)
def test_every_route_refuses_without_a_bot_api_key(guarded_client, method, path, body, headers):
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    response = getattr(guarded_client, method)(path, **kwargs)
    assert response.status_code == 401


async def test_an_ack_for_an_unknown_run_deletes_its_command_before_404(test_engine):
    redis = fakeredis.aioredis.FakeRedis()
    run_id = uuid.uuid4()
    await redis.xadd(
        PURGE_STREAM,
        {"payload": json.dumps({"request_id": str(uuid.uuid4()), "run_id": str(run_id)})},
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def session():
        async with maker() as db_session:
            yield db_session

    original = list(privacy_module.BOT_API_GUARDS)
    privacy_module.BOT_API_GUARDS.clear()
    try:
        with (
            patch.object(privacy_module, "get_redis_client", return_value=redis),
            create_test_client(
                route_handlers=[PrivacyController],
                plugins=[PydanticPlugin()],
                dependencies={"db_session": Provide(session)},
            ) as client,
        ):
            response = client.post(f"/api/privacy/purges/{run_id}/acks", json=_ACK)
    finally:
        privacy_module.BOT_API_GUARDS.extend(original)

    assert response.status_code == 404
    assert await redis.xlen(PURGE_STREAM) == 0
    await redis.aclose()
