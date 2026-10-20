"""The gateway's migration builds the tables its models describe.

The gateway's other tests run on a SQLite database built from the models
(``tests/moderation_fakes.py``), so a migration missing a column the models
have passes all of them and fails in production on the first insert. Two
guards here:

* a parity check, run everywhere: the columns migration ``e4c8b2f6a9d1``
  creates against the models' columns, table by table;
* the real thing, run when ``POSTGRES_MIGRATION_TEST_URL`` names a Postgres
  server the test may create databases on: both migration trees run on a new
  database; alembic compares every app table's columns with its model (that
  found ``spam_ring_incidents`` without ``updated_at`` too); then the gateway
  acts, races, sweeps and serves a deletion request on it, the spam ring
  saves a transcript, and the hourly retention sweep runs over every table.

    docker run -d --rm --name pg17 -e POSTGRES_PASSWORD=pw -e POSTGRES_USER=u \\
        -e POSTGRES_DB=db -p 127.0.0.1:55417:5432 postgres:17
    POSTGRES_MIGRATION_TEST_URL=postgresql+asyncpg://u:pw@127.0.0.1:55417/db \\
        pytest tests/web/moderation_gateway_migration_test.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import secrets
import subprocess
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from smarter_dev.shared.database import Base
from smarter_dev.web.models import ModerationAction
from smarter_dev.web.models import ModerationIncidentEntry
from smarter_dev.web.models import ModerationMemberLock
from smarter_dev.web.models import ModerationOperation
from smarter_dev.web.models import SpamRingIncident
from smarter_dev.web.moderation_gateway import ModerationGateway
from smarter_dev.web.moderation_gateway import Proposal
from smarter_dev.web.moderation_gateway import Trigger
from smarter_dev.web.moderation_gateway import types as t
from smarter_dev.web.moderation_gateway.locks import MemberLocks
from smarter_dev.web.moderation_gateway.locks import _LocalLocks
from smarter_dev.web.moderation_gateway.privacy import delete_member_moderation_content
from smarter_dev.web.retention import run_retention_sweep
from tests.moderation_fakes import GUILD
from tests.moderation_fakes import REPORT_CHANNEL
from tests.moderation_fakes import FakeModerationDiscord
from tests.test_migration_ownership import MAIN_ENV_PATH
from tests.test_migration_ownership import load_ownership_frozenset

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPO_ROOT
    / "alembic/main/versions/20261019_210000_e4c8b2f6a9d1_moderation_gateway.py"
)
SPAM_RING_MIGRATION = (
    REPO_ROOT
    / "alembic/main/versions/20261019_230000_0a337101a0e8_spam_ring_incidents_updated_at.py"
)
SHADOW_MIGRATION = (
    REPO_ROOT
    / "alembic/main/versions/20261020_010000_b7d2e5a8c3f4_moderation_shadow_decisions.py"
)
CREATED = (
    "moderation_incidents",
    "moderation_incident_links",
    "moderation_incident_entries",
    "moderation_operations",
    "moderation_outbox",
    "moderation_member_locks",
)
SCAMMER = "5001"
CHANNEL = "7001"
MESSAGE = "8001"


class _RecordingOp:
    """Stands in for ``alembic.op``: keeps the columns, ignores the rest."""

    def __init__(self, dialect: str = "recording") -> None:
        self.dialect = dialect
        self.created: dict[str, dict[str, sa.Column]] = {}
        self.added: dict[str, dict[str, sa.Column]] = {}
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, statement, *_args, **_kw):
        self.calls.append(("execute", (str(statement),)))

    def create_table(self, name, *items, **_kw):
        self.calls.append(("create_table", (name,)))
        self.created[name] = {
            item.name: item for item in items if isinstance(item, sa.Column)
        }

    def add_column(self, table, column, **_kw):
        self.calls.append(("add_column", (table,)))
        self.added.setdefault(table, {})[column.name] = column

    def get_context(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    def __getattr__(self, name):
        return lambda *args, **_kw: self.calls.append((name, args))


def _run(path: Path, step: str = "upgrade", dialect: str = "recording") -> _RecordingOp:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = _RecordingOp(dialect)
    module.op = recorder
    getattr(module, step)()
    return recorder


def _upgrade_columns() -> _RecordingOp:
    return _run(MIGRATION)


def _shape(columns) -> dict[str, bool]:
    return {column.name: bool(column.nullable) for column in columns}


def test_the_migration_creates_every_column_the_models_have():
    recorded = _upgrade_columns()

    assert set(recorded.created) == set(CREATED)
    for table in CREATED:
        assert _shape(recorded.created[table].values()) == _shape(
            Base.metadata.tables[table].columns
        ), table
    model_actions = _shape(Base.metadata.tables["moderation_actions"].columns)
    for name, nullable in _shape(recorded.added["moderation_actions"].values()).items():
        assert model_actions.get(name) == nullable, name


def test_the_shadow_migration_creates_every_column_the_model_has():
    recorded = _run(SHADOW_MIGRATION)
    table = "moderation_shadow_decisions"
    assert set(recorded.created) == {table}
    assert _shape(recorded.created[table].values()) == _shape(
        Base.metadata.tables[table].columns
    )


@pytest.mark.parametrize("step", ["upgrade", "downgrade"])
@pytest.mark.parametrize(
    "path", [MIGRATION, SPAM_RING_MIGRATION, SHADOW_MIGRATION], ids=lambda p: p.stem
)
def test_lock_waits_are_bounded_before_any_change(path, step):
    calls = _run(path, step, dialect="postgresql").calls
    assert calls[0] == ("execute", ("SET LOCAL lock_timeout = '5s'",))
    assert len(calls) > 1


# ---------------------------------------------------------------------------
# On Postgres
# ---------------------------------------------------------------------------

SERVER_URL = os.environ.get("POSTGRES_MIGRATION_TEST_URL")
needs_postgres = pytest.mark.skipif(
    not SERVER_URL, reason="POSTGRES_MIGRATION_TEST_URL is not set"
)

#: app.yaml interpolates these when the migrations load Skrift's settings;
#: only their presence matters here.
_SETTINGS_PLACEHOLDERS = dict.fromkeys(
    (
        "DISCORD_APPLICATION_ID",
        "DISCORD_APPLICATION_SECRET",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "SPACES_ACCESS_KEY",
        "SPACES_SECRET_KEY",
    ),
    "unused",
)


def _migrate(database_url: str) -> None:
    env = {
        **os.environ,
        **_SETTINGS_PLACEHOLDERS,
        "DATABASE_URL": database_url,
        "ENVIRONMENT": "development",
        "REDIS_URL": "redis://127.0.0.1:1/0",
    }
    env.pop("TEST_DATABASE_URL", None)
    for step in ("skrift-main", "main"):
        done = subprocess.run(
            [sys.executable, "scripts/migrate.py", "--only", step],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert done.returncode == 0, f"{step}:\n{done.stdout}\n{done.stderr}"


@pytest.fixture
async def migrated():
    """A new database on the server with both migration trees applied."""
    server = make_url(SERVER_URL)
    name = f"modgw_{secrets.token_hex(6)}"
    admin = create_async_engine(
        server, poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = server.set(database=name)
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": "skrift,public"}},
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE SCHEMA skrift"))
        await asyncio.to_thread(_migrate, url.render_as_string(hide_password=False))
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


#: Differences in what a table holds, as alembic names them; the rest it
#: reports (foreign keys to Skrift's tables, indexes made in raw SQL) are
#: blind spots of its comparison, not drift.
_COLUMN_CHANGES = {
    "add_table",
    "remove_table",
    "add_column",
    "remove_column",
    "modify_type",
    "modify_nullable",
}


def _flatten(differences):
    for difference in differences:
        if isinstance(difference, list):
            yield from _flatten(difference)
        else:
            yield difference


def _column_differences(connection) -> list:
    main_tables = load_ownership_frozenset(MAIN_ENV_PATH, "MAIN_TABLES")
    return [
        difference
        for difference in _flatten(_differences(connection, main_tables))
        if difference[0] in _COLUMN_CHANGES
    ]


def _differences(connection, tables) -> list:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    def include(obj, name, type_, _reflected, _compare_to):
        if type_ == "table":
            return name in tables
        parent = getattr(obj, "table", None)
        return parent is None or parent.name in tables

    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "include_object": include},
    )
    return compare_metadata(context, Base.metadata)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).timestamp()

    def __call__(self) -> float:
        return self.now


def _trigger(actor: str) -> Trigger:
    return Trigger(
        guild_id=GUILD,
        actor=actor,
        reason="mass mention, scam link",
        channel_id=CHANNEL,
        message_id=MESSAGE,
        author_id=SCAMMER,
        content="free nitro https://scam.example @everyone",
        report_channel_id=REPORT_CHANNEL,
    )


@needs_postgres
async def test_every_app_table_has_its_models_columns(migrated):
    async with migrated.connect() as conn:
        assert await conn.run_sync(_column_differences) == []

    db = async_sessionmaker(migrated, expire_on_commit=False)
    async with db() as session:
        row = SpamRingIncident(
            guild_id=GUILD,
            channel_id=CHANNEL,
            trigger_message_id=MESSAGE,
            participant_user_ids=[SCAMMER],
            transcript="[5001] join our hackathon",
        )
        session.add(row)
        await session.commit()
        row.transcript += "\n[5002] me too"
        await session.commit()


@needs_postgres
async def test_the_gateway_runs_on_the_migrated_postgres_schema(migrated):
    gateway_tables = {*CREATED, "moderation_actions"}
    async with migrated.connect() as conn:
        assert await conn.run_sync(_differences, gateway_tables) == []

    db = async_sessionmaker(migrated, expire_on_commit=False, autoflush=False)
    clock = _Clock()
    discord = FakeModerationDiscord(clock=clock)
    discord.add_member(SCAMMER)
    discord.post(CHANNEL, MESSAGE, SCAMMER, "free nitro https://scam.example")

    def gateway() -> ModerationGateway:
        # Each its own process: they meet only at the database's lock row.
        locks = MemberLocks(db, clock=clock, local=_LocalLocks())
        return ModerationGateway(discord, session_factory=db, clock=clock, locks=locks)

    first = await gateway().act(
        _trigger("spam_engine"),
        [
            Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400),
            Proposal(
                kind=t.DELETE,
                target_user_id=SCAMMER,
                channel_id=CHANNEL,
                message_ids=(MESSAGE,),
            ),
        ],
    )
    assert [o.state for o in first.outcomes] == [t.CONFIRMED, t.CONFIRMED]

    raced = await asyncio.gather(
        *(
            gateway().act(
                _trigger(actor), [Proposal(kind=t.BAN, target_user_id=SCAMMER)]
            )
            for actor in ("handler:scam-banner", "spam_ring")
        )
    )
    assert {r.incident_id for r in raced} == {first.incident_id}
    assert discord.count("ban") == 1
    assert len(discord.reports()) == 1

    await gateway().sweep()
    async with db() as session:
        kinds = (
            (
                await session.execute(
                    select(ModerationAction.action_type).order_by(
                        ModerationAction.action_type
                    )
                )
            )
            .scalars()
            .all()
        )
        assert kinds == ["ban", "delete", "timeout"]
        assert (
            await session.execute(
                select(func.count()).select_from(ModerationMemberLock)
            )
        ).scalar_one() == 0

        await run_retention_sweep(session)

        deleted = await delete_member_moderation_content(session, SCAMMER)
        await session.commit()
        assert deleted.entries >= 1
        assert (
            await session.execute(
                select(func.count())
                .select_from(ModerationIncidentEntry)
                .where(ModerationIncidentEntry.kind == t.ENTRY_EVIDENCE)
            )
        ).scalar_one() == 0
        assert (
            await session.execute(select(func.count()).select_from(ModerationOperation))
        ).scalar_one() >= 3


@needs_postgres
async def test_the_coordinator_shadow_runs_on_the_migrated_postgres_schema(
    migrated, monkeypatch
):
    """#239: the shadow's row, its legacy comparison, the author history it
    reads, and the deletion request and retention over its table, on Postgres."""
    from datetime import timedelta

    from smarter_dev.bot.moderation_coordinator.shadow import BotReads
    from smarter_dev.bot.moderation_coordinator.shadow import LegacyComparer
    from smarter_dev.bot.moderation_coordinator.shadow import ShadowRecorder
    from smarter_dev.web.models import MemberActivity
    from smarter_dev.web.models import ModerationShadowDecision
    from smarter_dev.web.moderation_gateway import gateway as gateway_module
    from smarter_dev.web.retention import prune_moderation_shadow_decisions

    db = async_sessionmaker(migrated, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(gateway_module, "SESSION_FACTORY_OVERRIDE", db)
    discord = FakeModerationDiscord()
    discord.add_member(SCAMMER)
    discord.post(CHANNEL, MESSAGE, SCAMMER, "free nitro https://scam.example")
    await ModerationGateway(discord, session_factory=db).act(
        _trigger("spam_engine"),
        [Proposal(kind=t.TIMEOUT, target_user_id=SCAMMER, duration_seconds=86400)],
    )
    now = datetime.now(UTC)
    recorded = await ShadowRecorder().record(
        ModerationShadowDecision(
            guild_id=GUILD,
            channel_id=CHANNEL,
            message_id=MESSAGE,
            revision="create",
            is_edit=False,
            author_id=SCAMMER,
            message_at=now,
            strongest_action="ban",
            target_ids=[SCAMMER],
            staff_excluded={},
            labels={"scam": {"outcome": "answered", "answer": True, "confidence": 0.9}},
            classifier={"backend": "haiku", "requests": 1},
        )
    )
    assert recorded
    # The daily cap's read-back (#255), on Postgres's JSON operators.
    assert await ShadowRecorder().requests_today([GUILD], now.timestamp()) == {
        GUILD: 1
    }
    async with db() as session:
        session.add(
            MemberActivity(
                guild_id=GUILD,
                user_id=SCAMMER,
                first_message_at=now - timedelta(days=40),
                last_message_at=now - timedelta(days=31),
            )
        )
        await session.commit()

    history = await BotReads(None).author_history(GUILD, SCAMMER, now)
    assert (history.is_first_message, history.days_since_last_message) == (False, 31)
    assert [a.action_type for a in history.actions] == ["timeout"]

    later = datetime.now(UTC).timestamp() + 300
    assert await LegacyComparer(clock=lambda: later).sweep() == 1
    async with db() as session:
        row = (await session.execute(select(ModerationShadowDecision))).scalar_one()
        assert row.legacy_strongest_action == "timeout"
        assert row.agreement == "shadow_stronger"

        assert await prune_moderation_shadow_decisions(session, now, now) == 0
        deleted = await delete_member_moderation_content(session, SCAMMER)
        assert deleted.shadow == 1
        await session.commit()

    # Retention, on a row of its own: the one above is gone (#255).
    assert await ShadowRecorder().record(
        ModerationShadowDecision(
            guild_id=GUILD,
            channel_id=CHANNEL,
            message_id="9" + MESSAGE[1:],
            revision="create",
            is_edit=False,
            author_id="1" + SCAMMER[1:],
            message_at=now,
        )
    )
    async with db() as session:
        far = now + timedelta(days=31)
        assert await prune_moderation_shadow_decisions(session, now, now) == 0
        assert await prune_moderation_shadow_decisions(session, far, far) == 1
        await session.commit()
