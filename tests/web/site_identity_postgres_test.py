"""Real local PostgreSQL transactions for absent-UUID fence arbitration.

Opt in with SITE_IDENTITY_TEST_POSTGRES_URL pointing at an ephemeral LOCAL DB.
Never use a shared/remote database: this fixture creates and drops test tables.
"""

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from skrift.db.base import Base as SkriftBase
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from smarter_dev.web import site_identity as service
from smarter_dev.web.api_native.errors import BotApiException
from smarter_dev.web.site_identity_models import SiteIdentityFence
from smarter_dev.web.site_identity_models import SiteIdentityKey
from smarter_dev.web.site_identity_models import SiteIdentityState

URL = os.getenv("SITE_IDENTITY_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="ephemeral local PostgreSQL not configured"
)


@pytest.fixture
async def sessions(monkeypatch):
    assert make_url(URL).host in {"127.0.0.1", "localhost"}
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(
            site_identity_fence_key="local-test-only-stable-key-123456789"
        ),
    )
    engine = create_async_engine(URL)
    tables = [
        SiteIdentityKey.__table__,
        SiteIdentityFence.__table__,
        SiteIdentityState.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(SkriftBase.metadata.create_all)
        for table in tables:
            await conn.run_sync(table.create, checkfirst=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        for table in reversed(tables):
            await conn.run_sync(table.drop, checkfirst=True)
        await conn.run_sync(SkriftBase.metadata.drop_all)
    await engine.dispose()


def body(uid):
    return service.IdentitySnapshot(
        revision=1, identities=[{"provider": "github", "subject": str(uid.int)}]
    )


@pytest.mark.parametrize("delete_first", [False, True])
async def test_absent_uuid_create_delete_ordering(sessions, delete_first):
    uid = uuid4()
    async with sessions() as first:
        # Hold the absent-key arbitration lock before starting the competitor.
        if delete_first:
            await service.fence_account_deletion(first, uid)
        else:
            await service.provision_identity(first, uid, body(uid))

        async def contender():
            async with sessions() as second:
                if delete_first:
                    with pytest.raises(BotApiException) as error:
                        await service.provision_identity(second, uid, body(uid))
                    assert error.value.status_code == 410
                else:
                    await service.fence_account_deletion(second, uid)
                    await second.commit()

        task = asyncio.create_task(contender())
        await asyncio.sleep(0.1)
        assert not task.done(), "competing transaction must wait on absent-key fence"
        await first.commit()
        await asyncio.wait_for(task, timeout=10)
    async with sessions() as db:
        assert (await db.get(SiteIdentityFence, service.fence_digest(uid))).deleted
        with pytest.raises(BotApiException) as error:
            await service.provision_identity(db, uid, body(uid))
        assert error.value.status_code == 410


async def test_concurrent_identical_creation_is_idempotent(sessions):
    uid = uuid4()

    async def run():
        async with sessions() as db:
            created, _ = await service.provision_identity(db, uid, body(uid))
            await db.commit()
            return created

    assert sorted(await asyncio.gather(run(), run())) == [False, True]
    async with sessions() as db:
        assert len((await db.scalars(select(SiteIdentityState))).all()) == 1
