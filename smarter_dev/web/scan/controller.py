"""Scan subdomain controllers for scan.smarter.dev."""

import logging
from typing import Annotated

from litestar import Controller, MediaType, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.exceptions import ClientException
from litestar.params import Body
from litestar.response import Redirect, Template
from litestar.status_codes import HTTP_429_TOO_MANY_REQUESTS
from skrift.auth.guards import auth_guard
from skrift.auth.services import get_user_permissions
from skrift.lib.markdown import render_markdown
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.scan.citations import process_citations
from smarter_dev.web.scan.crud import ResearchSessionOperations
from smarter_dev.web.scan.runner import start_pipeline_task

logger = logging.getLogger(__name__)
ops = ResearchSessionOperations()

WEEKLY_RESEARCH_LIMIT = 25


class ScanController(Controller):
    """Landing and result pages for the Scan research service."""

    path = "/"

    @get("/")
    async def landing(self, request: Request) -> Template:
        """Scan landing page with search input and topic grid."""
        user_id = request.session.get("user_id") if request.session else None
        og_meta = {
            "title": "Scan — AI Research Assistant",
            "description": "Get comprehensive, AI-powered research on any topic in seconds. Scan finds sources, analyzes content, and delivers structured insights.",
            "url": "https://scan.smarter.dev/",
            "site_name": "Scan by Smarter Dev",
            "type": "website",
        }
        return Template("scan/landing.html", context={"user_id": user_id, "og_meta": og_meta})

    @post(
        "/",
        guards=[auth_guard],
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
        tz = data.get("tz", "").strip() or None

        # Rate limit: 25 lite researches per week per user (admins exempt)
        user_perms = await get_user_permissions(db_session, user_id)
        is_admin = "administrator" in user_perms.permissions
        recent_count = await ops.count_recent_sessions(db_session, user_id)
        if not is_admin and recent_count >= WEEKLY_RESEARCH_LIMIT:
            raise ClientException(
                detail=f"Weekly research limit reached ({WEEKLY_RESEARCH_LIMIT}/week). Try again next week.",
                status_code=HTTP_429_TOO_MANY_REQUESTS,
            )

        research = await ops.create_session(
            db_session, query=query, user_id=user_id,
            pipeline_mode="lite",
        )
        await db_session.commit()

        # Start the unified pipeline — one task handles everything
        start_pipeline_task(research.id, query, user_id, tz=tz)

        return Redirect(path=f"/r/{research.id}")

    @get("/robots.txt", media_type=MediaType.TEXT)
    async def robots_txt(self) -> str:
        """Serve robots.txt for scan.smarter.dev."""
        return "User-agent: *\nAllow: /\n\nSitemap: https://scan.smarter.dev/sitemap.xml\n"

    @get("/r/{result_id:str}")
    async def result(
        self, request: Request, result_id: str, db_session: AsyncSession
    ) -> Template:
        """Research result detail page with live updates for running sessions."""
        session_data = await ops.get_session(db_session, result_id)

        # Pre-render response with citation processing for completed sessions
        rendered_response = ""
        if session_data and session_data.status == "complete" and session_data.response:
            rendered_response = process_citations(
                render_markdown(session_data.response)
            )

        # Open Graph metadata for social previews
        og_meta = {
            "url": f"https://scan.smarter.dev/r/{result_id}",
            "site_name": "Scan by Smarter Dev",
            "type": "article",
        }
        if session_data:
            og_meta["title"] = session_data.name or session_data.query or "Research Result"
            og_meta["description"] = session_data.summary or session_data.query or ""
        else:
            og_meta["title"] = "Research Result"
            og_meta["description"] = "AI-powered research result on Scan by Smarter Dev."

        return Template(
            "scan/result.html",
            context={
                "result_id": result_id,
                "session": session_data,
                "rendered_response": rendered_response,
                "og_meta": og_meta,
            },
        )
