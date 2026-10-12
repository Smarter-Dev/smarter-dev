"""Real PostgreSQL capture/termination races; disposable loopback DB only."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.capture_identity_receipts import capture_rows
from scripts.capture_identity_receipts import persist_snapshot
from scripts.capture_identity_receipts import reconcile_captures

URL = os.getenv("SITE_IDENTITY_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="disposable local PostgreSQL required")


@pytest.fixture
async def engine():
    assert make_url(URL).host in {"localhost", "127.0.0.1"}
    db = create_async_engine(URL)
    async with db.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS skrift"))
        await conn.execute(
            text(
                "CREATE TABLE skrift.account_deletion_requests (id uuid PRIMARY KEY, user_id uuid UNIQUE NOT NULL, status text NOT NULL, subscription_ids json NOT NULL DEFAULT '[]')"
            )
        )
    yield db
    async with db.begin() as conn:
        await conn.execute(text("DROP TABLE skrift.account_deletion_requests"))
    await db.dispose()


async def receipt(engine):
    rid, uid = uuid4(), uuid4()
    async with engine.begin() as db:
        await db.execute(
            text(
                "INSERT INTO skrift.account_deletion_requests (id,user_id,status) VALUES (:r,:u,'running')"
            ),
            {"r": rid, "u": uid},
        )
    return rid, uid


async def anonymize(db, rid):
    await db.execute(
        text(
            "UPDATE skrift.account_deletion_requests SET user_id=:u, status='complete' WHERE id=:r"
        ),
        {"u": uuid4(), "r": rid},
    )


@pytest.mark.parametrize("commit_during_capture", [False, True])
async def test_snapshot_sees_original_for_purge_active_before_capture(
    engine, tmp_path, commit_during_capture
):
    rid, uid = await receipt(engine)
    async with engine.connect() as purge, engine.connect() as db:
        await anonymize(purge, rid)  # UPDATE is already in flight before snapshot
        db = await db.execution_options(isolation_level="REPEATABLE READ")
        async with db.begin():
            await db.execute(text("SET TRANSACTION READ ONLY"))

            class Connection:
                async def execute(self, query):
                    result = await db.execute(query)
                    if "pg_current_snapshot" in str(query) and commit_during_capture:
                        await purge.commit()  # commits between boundary and row SELECT
                    return result

            first = await capture_rows(Connection())
            assert first["receipts"] == [
                {"id": str(rid), "user_id": str(uid), "recoverable": True}
            ]
            persist_snapshot(tmp_path / "initial.json", first)
        await purge.commit()
    async with engine.connect() as db:
        final = await capture_rows(db)
    assert not final["receipts"][0]["recoverable"]
    assert reconcile_captures(first, final) == [str(uid)]


async def test_share_lock_waits_behind_purge_and_cannot_preserve_uuid(engine):
    rid, uid = await receipt(engine)
    async with engine.connect() as purge, engine.connect() as lock:
        await anonymize(purge, rid)
        await lock.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(DBAPIError):
            await lock.execute(
                text("LOCK TABLE skrift.account_deletion_requests IN SHARE MODE")
            )
        await lock.rollback()
        await purge.commit()
        await lock.execute(
            text("LOCK TABLE skrift.account_deletion_requests IN SHARE MODE")
        )
        rows = await capture_rows(lock)
        assert rows["receipts"][0]["user_id"] != str(uid)
        assert not rows["receipts"][0]["recoverable"]
        await lock.rollback()


@pytest.mark.parametrize(
    "operation",
    [
        "INSERT INTO skrift.account_deletion_requests (id,user_id,status) VALUES (:r,:u,'pending')",
        "UPDATE skrift.account_deletion_requests SET status='pending' WHERE id=:r",
        "DELETE FROM skrift.account_deletion_requests WHERE id=:r",
    ],
)
async def test_share_lock_blocks_all_receipt_writes_and_loss_releases_them(
    engine, operation
):
    rid, uid = await receipt(engine)
    if operation.startswith("INSERT"):
        rid, uid = uuid4(), uuid4()
    async with (
        engine.connect() as lock,
        engine.connect() as writer,
        engine.connect() as killer,
    ):
        pid = await lock.scalar(text("SELECT pg_backend_pid()"))
        await lock.execute(
            text("LOCK TABLE skrift.account_deletion_requests IN SHARE MODE")
        )
        task = asyncio.create_task(
            writer.execute(text(operation), {"r": rid, "u": uid})
        )
        await asyncio.sleep(0.1)
        assert not task.done()
        # Simulate lock-session loss, not a graceful release.
        assert await killer.scalar(
            text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
        )
        await asyncio.wait_for(task, 5)
        await writer.commit()
        await lock.invalidate()  # terminated connection has no live transaction


async def test_terminated_purge_rolls_back_uuid_then_can_retry(engine):
    rid, uid = await receipt(engine)
    async with engine.connect() as purge, engine.connect() as killer:
        pid = await purge.scalar(text("SELECT pg_backend_pid()"))
        await anonymize(purge, rid)
        assert await killer.scalar(
            text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
        )
        await purge.invalidate()
    async with engine.begin() as db:
        before = await capture_rows(db)
        assert before["receipts"][0]["user_id"] == str(uid)
        await anonymize(db, rid)
    async with engine.connect() as db:
        after = await capture_rows(db)
    assert reconcile_captures(before, after) == [str(uid)]


async def test_worker_stopped_new_web_receipts_remain_reconcilable(engine):
    await receipt(engine)
    async with engine.connect() as db:
        before = await capture_rows(db)
    # Old web admission may continue; no purge consumer can anonymize these.
    _, new_uid = await receipt(engine)
    async with engine.connect() as db:
        after = await capture_rows(db)
    assert str(new_uid) in reconcile_captures(before, after)


async def test_new_receipt_purged_during_stop_is_transition_failure(engine):
    async with engine.connect() as db:
        before = await capture_rows(db)
    rid, _ = await receipt(engine)
    async with engine.begin() as db:
        await anonymize(db, rid)
    async with engine.connect() as db:
        after = await capture_rows(db)
    with pytest.raises(RuntimeError, match="during transition"):
        reconcile_captures(before, after)


async def test_migration_checks_actual_claim_expiry_with_database_clock(
    engine, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from scripts.migrate_and_pin_identity import assert_claims_clear
    from smarter_dev.shared import database

    monkeypatch.setattr(
        database, "get_session_maker", lambda: async_sessionmaker(engine)
    )
    async with engine.begin() as db:
        await db.execute(
            text(
                "CREATE TABLE skrift.worker_queue (queue text, claim_token text, claim_expires_at timestamptz)"
            )
        )
    try:
        await assert_claims_clear()
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO skrift.worker_queue VALUES ('agents','active',clock_timestamp()+interval '1800 seconds')"
                )
            )
        with pytest.raises(RuntimeError, match="not cleared"):
            await assert_claims_clear()
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE skrift.worker_queue SET claim_expires_at=clock_timestamp()-interval '1 second'"
                )
            )
        await assert_claims_clear()  # expired row preserved for normal reclamation
        async with engine.begin() as db:
            assert (
                await db.scalar(text("SELECT count(*) FROM skrift.worker_queue")) == 1
            )
            await db.execute(
                text("UPDATE skrift.worker_queue SET claim_expires_at=NULL")
            )
        with pytest.raises(RuntimeError):
            await assert_claims_clear()
    finally:
        async with engine.begin() as db:
            await db.execute(text("DROP TABLE skrift.worker_queue"))
