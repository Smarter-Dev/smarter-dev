"""Scan subdomain controllers for scan.smarter.dev."""

import logging
from typing import Annotated

from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template
from skrift.auth.guards import Permission, auth_guard
from skrift.lib.notifications import subscribe_source
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.scan.agent import generate_session_name
from smarter_dev.web.scan.crud import ResearchSessionOperations
from smarter_dev.web.scan.runner import start_research_task

logger = logging.getLogger(__name__)
ops = ResearchSessionOperations()


class ScanController(Controller):
    """Landing and result pages for the Scan research service."""

    path = "/"

    @get("/")
    async def landing(self, request: Request) -> Template:
        """Scan landing page with search input and topic grid."""
        user_id = request.session.get("user_id") if request.session else None
        return Template("scan/landing.html", context={"user_id": user_id})

    @post(
        "/",
        guards=[auth_guard, Permission("use-scan")],
    )
    async def submit(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Submit a research query."""
        query = data.get("query", "").strip()
        if not query:
            return Redirect(path="/")

        user_id = request.session.get("user_id", "")
        name = await generate_session_name(query)

        research = await ops.create_session(
            db_session, query=query, user_id=user_id, name=name
        )
        await db_session.commit()

        start_research_task(research.id, query, user_id)

        return Redirect(path=f"/r/{research.id}")

    @get("/r/{result_id:str}")
    async def result(
        self, request: Request, result_id: str, db_session: AsyncSession
    ) -> Template:
        """Research result detail page with live updates for running sessions."""
        session_data = await ops.get_session(db_session, result_id)

        if session_data and session_data.status == "running":
            user_id = request.session.get("user_id")
            if user_id:
                await subscribe_source(f"user:{user_id}", f"research:{result_id}")

        return Template(
            "scan/result.html",
            context={
                "result_id": result_id,
                "session": session_data,
            },
        )
