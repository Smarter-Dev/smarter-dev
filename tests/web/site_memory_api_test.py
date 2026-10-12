"""Site service isolation, privacy and memory wire contract over real storage."""

from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
from uuid import uuid4

import pytest
import yaml
from litestar import Litestar
from litestar.datastructures import State
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import AsyncTestClient
from skrift.auth.services import UserPermissions
from skrift.db.services import api_key_service
from sqlalchemy import select

from smarter_dev.web import chat_agent_page_controller
from smarter_dev.web.api_native import auth
from smarter_dev.web.api_native import rate_limiting
from smarter_dev.web.api_native.site_memory import SiteMemoryController
from smarter_dev.web.chat_bot_opt_out import opt_out
from smarter_dev.web.models import MAX_MEMORY_NOTE_CHARS
from smarter_dev.web.models import MAX_NOTES_PER_GUILD_PER_DAY
from smarter_dev.web.models import ChatAgentGuildMemory
from smarter_dev.web.models import ChatAgentMemoryNote
from smarter_dev.web.models import ChatBotPurgeRequest

GUILD = "123456789012345678"
MEMBER = "111111111111111111"
OTHER = "222222222222222222"
URL = f"/api/site/guilds/{GUILD}/memory"
MEMBER_URL = f"{URL}/members/{MEMBER}"
AT = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
async def site_client(db_session, monkeypatch):
    import skrift.auth.services

    @asynccontextmanager
    async def session_factory():
        yield db_session

    key = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        principal_type="service",
        service_name="site",
        scoped_permission_list=["site-memory"],
        scoped_role_list=[],
        key_prefix="sk_test",
        display_name="site",
    )
    monkeypatch.setattr(api_key_service, "verify_api_key", AsyncMock(return_value=key))
    monkeypatch.setattr(
        skrift.auth.services,
        "get_user_permissions",
        AsyncMock(
            return_value=UserPermissions(
                user_id=str(key.user_id), roles=set(), permissions={"site-memory"}
            )
        ),
    )
    monkeypatch.setattr(auth, "get_db_session_context", session_factory)
    monkeypatch.setattr(rate_limiting, "get_db_session_context", session_factory)
    monkeypatch.setattr(
        rate_limiting,
        "check_rate_limits",
        AsyncMock(
            return_value=rate_limiting.RateLimitDecision(allowed=True, headers={})
        ),
    )
    app = Litestar(
        route_handlers=[SiteMemoryController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: db_session, sync_to_thread=False)},
        state=State({"session_maker_class": session_factory}),
    )
    async with AsyncTestClient(app=app) as client:
        client.headers["Authorization"] = "Bearer sk_test"
        yield client, key


def note_uuid(number):
    # A hex letter prevents SQLite UUID numeric affinity from coercing IDs.
    return UUID(f"a0000000-0000-0000-0000-{number:012x}")


async def seed_note(session, content, *, number=1, guild_id=GUILD, at=AT):
    note = ChatAgentMemoryNote(
        id=note_uuid(number),
        guild_id=guild_id,
        channel_id="discord-channel",
        channel_name="general",
        content=content,
        created_at=at,
        updated_at=at,
    )
    session.add(note)
    await session.commit()
    return note


async def seed_memory(session, **values):
    fields = {
        "guild_id": GUILD,
        "content": f"<{MEMBER}:alice> builds things.\n<{OTHER}:bob> waves.",
        "personality": "Curious.",
        "behavior": "Listen first.",
        "revision": 3,
        "last_dream_at": AT,
        "public_page_enabled": True,
    }
    fields.update(values)
    memory = ChatAgentGuildMemory(**fields)
    session.add(memory)
    await session.commit()
    return memory


ROUTES = [
    ("GET", "/blocks"),
    ("GET", f"/members/{MEMBER}"),
    ("POST", f"/members/{MEMBER}/notes"),
    ("GET", "/public"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize("kind", ["missing", "bot", "user", "privacy-only", "unscoped"])
async def test_each_endpoint_rejects_non_site_memory_credentials(
    site_client, method, path, kind
):
    client, key = site_client
    headers = {}
    if kind == "missing":
        headers["Authorization"] = ""
    elif kind == "bot":
        # Even an accidentally over-granted bot key is not the site principal.
        key.service_name = "discord-bot"
    elif kind == "user":
        key.principal_type = "user"
    elif kind == "privacy-only":
        key.scoped_permission_list = ["site-privacy"]
    elif kind == "unscoped":
        key.scoped_permission_list = []
    kwargs = {"json": {"content": "a note"}} if method == "POST" else {}
    response = await client.request(method, URL + path, headers=headers, **kwargs)
    assert response.status_code == 401


async def test_blocks_remove_opted_out_lines_and_keep_metadata(site_client, db_session):
    client, _ = site_client
    await seed_memory(db_session, personality=f"Warm.\n<{OTHER}:bob> likes jokes.")
    await opt_out(db_session, OTHER)
    await db_session.commit()
    response = await client.get(URL + "/blocks")
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == f"<{MEMBER}:alice> builds things."
    assert body["personality"] == "Warm."
    assert body["behavior"] == "Listen first."
    assert body["revision"] == 3
    assert body["last_dream_at"].startswith("2026-10-01")
    assert response.headers["cache-control"] == "private, no-store"


async def test_empty_and_paused_blocks(site_client, db_session):
    client, _ = site_client
    body = (await client.get(URL + "/blocks")).json()
    assert body["content"] is None and body["revision"] is None
    await seed_memory(db_session, memory_enabled=False)
    body = (await client.get(URL + "/blocks")).json()
    assert body["memory_enabled"] is False
    assert body["content"] is None and body["personality"] is None


@pytest.mark.parametrize("viewer", ["", MEMBER])
async def test_public_matches_page_and_hides_unsafe_block(
    site_client, db_session, viewer
):
    client, _ = site_client
    memory = await seed_memory(db_session, behavior="<script>unsafe</script>")
    await opt_out(db_session, OTHER)
    await db_session.commit()
    chat_agent_page_controller._blocks_cache.clear()
    page_blocks = await chat_agent_page_controller._cached_public_blocks(
        db_session, memory
    )
    response = await client.get(URL + "/public", params={"viewer_discord_ids": viewer})
    assert response.status_code == 200
    body = response.json()
    ids = frozenset([viewer]) if viewer else frozenset()
    for name, block in page_blocks.items():
        assert body[name] == block.shown(ids)
    assert body["behavior"] == ""
    assert "bob" not in body["memory"]
    assert ("alice" in body["memory"]) == bool(viewer)
    assert response.headers["cache-control"] == "private, no-store"
    assert body["revision"] == 3


async def test_public_404_until_this_guild_is_enabled(site_client, db_session):
    client, _ = site_client
    assert (await client.get(URL + "/public")).status_code == 404
    memory = await seed_memory(db_session, public_page_enabled=False)
    assert (await client.get(URL + "/public")).status_code == 404
    memory.public_page_enabled = True
    await db_session.commit()
    assert (await client.get(URL.replace(GUILD, OTHER) + "/public")).status_code == 404


async def test_member_pagination_exact_tags_guild_isolation_and_tied_times(
    site_client, db_session
):
    client, _ = site_client
    for number, content in enumerate(
        [
            f"<{MEMBER}:alice> first",
            f"alice (id {MEMBER}) second",
            f"<@!{MEMBER}> third",
            f"Bare ID {MEMBER}",
            f"<9{MEMBER}:someone else>",
            f"<{OTHER}:bob> unrelated",
        ],
        1,
    ):
        await seed_note(db_session, content, number=number)
    await seed_note(
        db_session, f"<{MEMBER}:alice> other guild", number=7, guild_id=OTHER
    )
    response = await client.get(MEMBER_URL, params={"limit": 2})
    assert response.status_code == 200, response.text
    first = response.json()
    assert [note["id"] for note in first["notes"]] == [
        str(note_uuid(3)),
        str(note_uuid(2)),
    ]
    assert first["notes"][0]["channel_name"] == "general"
    assert first["notes"][0]["created_at"].startswith("2026-10-01")
    second = (
        await client.get(
            MEMBER_URL, params={"limit": 2, "before": first["next_before"]}
        )
    ).json()
    assert [note["id"] for note in second["notes"]] == [str(note_uuid(1))]
    assert second["next_before"] is None
    # Cursor position survives the dream deleting the previous page's anchor.
    await db_session.delete(await db_session.get(ChatAgentMemoryNote, note_uuid(2)))
    await db_session.commit()
    assert (
        await client.get(MEMBER_URL, params={"before": first["next_before"]})
    ).json() == second


async def test_member_notes_hide_lines_about_other_opted_out_members(
    site_client, db_session
):
    client, _ = site_client
    await seed_note(db_session, f"<{MEMBER}:alice> builds.\n<{OTHER}:bob> secret")
    await seed_note(db_session, f"<{MEMBER}:alice> and <{OTHER}:bob> secret", number=2)
    await opt_out(db_session, OTHER)
    await db_session.commit()
    body = (await client.get(MEMBER_URL)).json()
    assert len(body["notes"]) == 1
    assert body["notes"][0]["content"] == f"<{MEMBER}:alice> builds."


@pytest.mark.parametrize("kind", ["opt_out", "purge"])
async def test_opted_out_or_open_purge_member_refused_on_read_and_write(
    site_client, db_session, kind
):
    client, _ = site_client
    if kind == "opt_out":
        await opt_out(db_session, MEMBER)
    else:
        # Exercise the independent purge check without a block-list row.
        db_session.add(ChatBotPurgeRequest(discord_user_id=MEMBER, status="queued"))
    await db_session.commit()
    assert (await client.get(MEMBER_URL)).status_code == 404
    assert (
        await client.post(MEMBER_URL + "/notes", json={"content": "remember me"})
    ).status_code == 404
    assert not (await db_session.scalars(select(ChatAgentMemoryNote))).all()


async def test_site_note_is_member_scoped_and_duplicates_are_not_saved(
    site_client, db_session
):
    client, _ = site_client
    result = await client.post(
        MEMBER_URL + "/notes", json={"content": "I learned about shaders."}
    )
    assert result.status_code == 200
    assert result.json()["saved"] is True
    note = await db_session.get(ChatAgentMemoryNote, UUID(result.json()["id"]))
    assert note.channel_id == note.channel_name == "site"
    assert note.engagement_id is None
    assert note.content == f"<@{MEMBER}> I learned about shaders."
    listed = (await client.get(MEMBER_URL)).json()["notes"]
    assert listed[0]["source"] == "site" and listed[0]["content"] == note.content
    duplicate = await client.post(
        MEMBER_URL + "/notes", json={"content": "I learned about shaders."}
    )
    assert duplicate.json()["reason"] == "duplicate"


async def test_site_note_refuses_other_opted_out_member(site_client, db_session):
    client, _ = site_client
    await opt_out(db_session, OTHER)
    await db_session.commit()
    response = await client.post(
        MEMBER_URL + "/notes", json={"content": f"<{OTHER}:bob> secret"}
    )
    assert response.status_code == 404


async def test_site_note_daily_cap(site_client, db_session):
    client, _ = site_client
    now = datetime.now(UTC)
    db_session.add_all(
        ChatAgentMemoryNote(
            guild_id=GUILD,
            channel_id="site",
            content=f"note {i}",
            created_at=now,
            updated_at=now,
        )
        for i in range(MAX_NOTES_PER_GUILD_PER_DAY)
    )
    await db_session.commit()
    response = await client.post(
        MEMBER_URL + "/notes", json={"content": "one too many"}
    )
    assert response.json()["reason"] == "daily_cap"


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"before": "broken"}])
async def test_invalid_pagination_refused(site_client, params):
    client, _ = site_client
    assert (await client.get(MEMBER_URL, params=params)).status_code == 422


@pytest.mark.parametrize(
    "content",
    ["", "  ", "x" * (MAX_MEMORY_NOTE_CHARS + 1), "x" * MAX_MEMORY_NOTE_CHARS],
)
async def test_note_bound_includes_generated_member_reference(site_client, content):
    client, _ = site_client
    assert (
        await client.post(MEMBER_URL + "/notes", json={"content": content})
    ).status_code == 422


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_all_routes_apply_rate_limit(site_client, monkeypatch, method, path):
    client, _ = site_client
    monkeypatch.setattr(
        rate_limiting,
        "check_rate_limits",
        AsyncMock(
            return_value=rate_limiting.RateLimitDecision(
                allowed=False, headers={"Retry-After": "1"}, retry_after_seconds=1
            )
        ),
    )
    kwargs = {"json": {"content": "a note"}} if method == "POST" else {}
    response = await client.request(method, URL + path, **kwargs)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"


def test_controller_registered_in_both_apps():
    root = Path(__file__).resolve().parents[2]
    for name in ("app.yaml", "app.development.yaml"):
        config = yaml.safe_load((root / name).read_text())
        assert (
            "smarter_dev.web.api_native.site_memory:SiteMemoryController"
            in config["controllers"]
        )


@pytest.mark.parametrize(
    "moment", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]
)
async def test_cursor_utc_overflow_is_422(site_client, moment):
    import base64

    client, _ = site_client
    before = base64.urlsafe_b64encode(f"{moment}|{note_uuid(1)}".encode()).decode()
    response = await client.get(MEMBER_URL, params={"before": before})
    assert response.status_code == 422


@pytest.mark.parametrize("length", [21, 22])
@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_guild_id_cannot_exceed_storage_bound(
    site_client, db_session, length, method, path
):
    client, _ = site_client
    kwargs = {"json": {"content": "a note"}} if method == "POST" else {}
    response = await client.request(
        method, URL.replace(GUILD, "1" * length) + path, **kwargs
    )
    assert response.status_code == 422
    assert not (await db_session.scalars(select(ChatAgentMemoryNote))).all()


async def test_real_skrift_site_key_authorization_on_every_route(
    test_engine, monkeypatch
):
    """Real role sync, key storage/verification and permission resolution."""
    from skrift.auth.services import assign_role_to_user
    from skrift.auth.services import invalidate_user_permissions_cache
    from skrift.auth.services import sync_roles_to_database
    from skrift.db.base import Base as SkriftBase
    from skrift.db.models.user import User

    import smarter_dev.web.roles  # noqa: F401 — sync the actual site-service role
    from smarter_dev.shared.database import async_sessionmaker

    async with test_engine.begin() as connection:
        await connection.run_sync(SkriftBase.metadata.create_all)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as session:
        owner = User(email="site-memory@example.test", name="Site", is_active=True)
        denied_owner = User(
            email="no-memory@example.test", name="No memory", is_active=True
        )
        session.add_all([owner, denied_owner])
        await session.commit()
        await sync_roles_to_database(session)
        assert await assign_role_to_user(session, owner.id, "site-service")
        await seed_memory(session)
        keys = {}
        variants = {
            "site": {},
            "bot": {"service_name": "discord-bot"},
            "user": {"principal_type": "user"},
            "missing-scope": {"scoped_permissions": ["site-privacy"]},
            "unscoped": {"scoped_permissions": None},
            "role-only": {"scoped_permissions": None, "scoped_roles": ["site-service"]},
            "owner-denied": {"user_id": denied_owner.id},
        }
        for name, overrides in variants.items():
            options = {
                "user_id": owner.id,
                "display_name": name,
                "principal_type": "service",
                "service_name": "site",
                "scoped_permissions": ["site-memory"],
            }
            options.update(overrides)
            _, keys[name], _ = await api_key_service.create_api_key(session, **options)

    async def provide_db_session():
        async with maker() as session:
            yield session

    monkeypatch.setattr(auth, "get_db_session_context", maker)
    monkeypatch.setattr(rate_limiting, "get_db_session_context", maker)
    monkeypatch.setattr(
        rate_limiting,
        "check_rate_limits",
        AsyncMock(
            return_value=rate_limiting.RateLimitDecision(allowed=True, headers={})
        ),
    )
    app = Litestar(
        route_handlers=[SiteMemoryController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(provide_db_session)},
        state=State({"session_maker_class": maker}),
    )
    invalidate_user_permissions_cache()
    try:
        async with AsyncTestClient(app=app) as client:
            for name, key in keys.items():
                for method, path in ROUTES:
                    kwargs = (
                        {"json": {"content": "Real site key note"}}
                        if method == "POST"
                        else {}
                    )
                    response = await client.request(
                        method,
                        URL + path,
                        headers={"Authorization": f"Bearer {key}"},
                        **kwargs,
                    )
                    assert response.status_code == (200 if name == "site" else 401), (
                        name,
                        method,
                        path,
                        response.text,
                    )
    finally:
        invalidate_user_permissions_cache()
