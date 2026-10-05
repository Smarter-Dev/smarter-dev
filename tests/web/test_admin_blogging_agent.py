"""The blogging admin keeps its pipeline runs pages; the blog ideas pages are gone."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from litestar.handlers import HTTPRouteHandler
from skrift.db.models.page import Page
from skrift.db.models.user import User

from smarter_dev.web.blogging_agent_admin import BloggingAgentAdminController
from smarter_dev.web.models import AuthoringPipelineRun

_MODULE = "smarter_dev.web.blogging_agent_admin"


def _paths() -> set[str]:
    return {
        path
        for value in vars(BloggingAgentAdminController).values()
        if isinstance(value, HTTPRouteHandler)
        for path in value.paths
    }


def test_the_topics_routes_are_gone():
    assert not any(path.startswith("/topics") for path in _paths())


def test_the_runs_routes_remain():
    assert {"/runs", "/runs/{run_id:uuid}"} <= _paths()


async def test_the_runs_list_renders(db_session):
    connection = await db_session.connection()
    for table in (User.__table__, Page.__table__):
        await connection.run_sync(table.create, checkfirst=True)
    db_session.add(AuthoringPipelineRun(id=uuid4(), status="completed", stage_session_ids={}))
    await db_session.commit()
    request = SimpleNamespace(session={})

    with (
        patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})),
        patch(f"{_MODULE}.get_flash_messages", return_value=[]),
    ):
        response = await BloggingAgentAdminController.list_runs.fn(
            None, request=request, db_session=db_session, page=1
        )

    assert response.template_name == "admin/blogging-agent/runs_list.html"
    assert response.context["total"] == 1
