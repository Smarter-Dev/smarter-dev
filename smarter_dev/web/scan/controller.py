"""Scan subdomain controllers for scan.smarter.dev."""

import logging
from typing import Annotated
from urllib.parse import quote

from litestar import Controller, MediaType, Request, Response, get, post
from litestar.enums import RequestEncodingType
from litestar.exceptions import ClientException, NotAuthorizedException
from litestar.params import Body, Parameter
from litestar.response import Redirect, Template
from litestar.status_codes import HTTP_429_TOO_MANY_REQUESTS
from skrift.auth.guards import auth_guard
from skrift.auth.services import get_user_permissions
from skrift.db.models.user import User
from sqlalchemy import delete
from skrift.lib.markdown import render_markdown
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.scan.citations import process_citations
from smarter_dev.web.scan.crud import ResearchSessionOperations
from smarter_dev.web.models import ScanUserProfile, ResearchSession
from smarter_dev.web.scan.runner import start_pipeline_task

logger = logging.getLogger(__name__)


def _scan_auth_redirect(request: Request, exc: NotAuthorizedException) -> Response:
    """Redirect unauthenticated scan users to login with next= back to scan."""
    current_url = str(request.url)
    # Ensure the next URL points to scan.smarter.dev, not the main domain
    if not current_url.startswith("http"):
        current_url = f"https://scan.smarter.dev{request.url.path}"
    login_url = f"https://smarter.dev/auth/login?next={quote(current_url, safe='')}"
    return Redirect(path=login_url)


ops = ResearchSessionOperations()

WEEKLY_RESEARCH_LIMIT = 25


class ScanController(Controller):
    """Landing and result pages for the Scan research service."""

    path = "/"
    exception_handlers = {NotAuthorizedException: _scan_auth_redirect}

    @get("/")
    async def landing(self, request: Request, db_session: AsyncSession) -> Template:
        """Scan landing page with search input and topic grid."""
        user_id = request.session.get("user_id") if request.session else None
        og_meta = {
            "title": "Scan — AI Research Assistant",
            "description": "Get comprehensive, AI-powered research on any topic in seconds. Scan finds sources, analyzes content, and delivers structured insights.",
            "url": "https://scan.smarter.dev/",
            "site_name": "Scan by Smarter Dev",
            "type": "website",
        }
        recent_searches: list = []
        suggested_queries: list = []
        if user_id:
            recent_searches = await ops.list_sessions(db_session, user_id, limit=3)
            profile_result = await db_session.execute(
                select(ScanUserProfile.suggested_queries)
                .where(ScanUserProfile.user_id == user_id)
            )
            suggested_queries = profile_result.scalar_one_or_none() or []
        return Template("scan/landing.html", context={
            "user_id": user_id,
            "og_meta": og_meta,
            "recent_searches": recent_searches,
            "suggested_queries": suggested_queries,
        })

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
        pipeline_mode = data.get("mode", "lite").strip()
        if pipeline_mode not in ("lite", "experimental"):
            pipeline_mode = "lite"

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
            pipeline_mode=pipeline_mode,
        )
        await db_session.commit()

        # Start the unified pipeline — one task handles everything
        start_pipeline_task(research.id, query, user_id, tz=tz, mode=pipeline_mode)

        return Redirect(path=f"/research?s={research.id}")

    @get("/research", guards=[auth_guard])
    async def research_pending(
        self, request: Request, db_session: AsyncSession, s: str = "",
    ) -> Template:
        """In-progress research page — redirected here after submitting a query.

        The session ID is passed as ?s=<uuid>.  Once the meta agent names the
        session, the client pushes /r/{slug} into the URL bar.
        """
        session_data = await ops.get_session(db_session, s) if s else None

        creator_name = ""
        if session_data:
            result = await db_session.execute(
                select(User.name).where(User.id == session_data.user_id)
            )
            creator_name = result.scalar_one_or_none() or ""

        og_meta = {
            "url": "https://scan.smarter.dev/research",
            "site_name": "Scan by Smarter Dev",
            "type": "article",
            "title": "Researching…",
            "description": session_data.query if session_data else "",
        }

        return Template(
            "scan/result.html",
            context={
                "result_id": s,
                "session": session_data,
                "rendered_response": "",
                "og_meta": og_meta,
                "creator_name": creator_name,
            },
        )

    @get("/robots.txt", media_type=MediaType.TEXT)
    async def robots_txt(self) -> str:
        """Serve robots.txt for scan.smarter.dev.

        - Search engines can index the landing and about pages only
        - AI training crawlers are fully blocked
        - LLM retrieval agents (browsing, not training) are allowed
        """
        return (
            "# Search engine crawlers — index landing + about only\n"
            "User-agent: *\n"
            "Allow: /$\n"
            "Allow: /about\n"
            "Disallow: /\n"
            "\n"
            "# Block AI training crawlers\n"
            "User-agent: GPTBot\n"
            "Disallow: /\n"
            "\n"
            "User-agent: ChatGPT-User\n"
            "Allow: /\n"
            "\n"
            "User-agent: Google-Extended\n"
            "Disallow: /\n"
            "\n"
            "User-agent: CCBot\n"
            "Disallow: /\n"
            "\n"
            "User-agent: anthropic-ai\n"
            "Disallow: /\n"
            "\n"
            "User-agent: ClaudeBot\n"
            "Disallow: /\n"
            "\n"
            "User-agent: Claude-Web\n"
            "Allow: /\n"
            "\n"
            "User-agent: cohere-ai\n"
            "Disallow: /\n"
            "\n"
            "User-agent: Bytespider\n"
            "Disallow: /\n"
            "\n"
            "User-agent: Amazonbot\n"
            "Disallow: /\n"
            "\n"
            "User-agent: FacebookBot\n"
            "Disallow: /\n"
            "\n"
            "User-agent: Applebot-Extended\n"
            "Disallow: /\n"
            "\n"
            "User-agent: PerplexityBot\n"
            "Allow: /\n"
        )

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
        canonical_path = (session_data.slug or result_id) if session_data else result_id
        og_meta = {
            "url": f"https://scan.smarter.dev/r/{canonical_path}",
            "site_name": "Scan by Smarter Dev",
            "type": "article",
        }
        if session_data:
            og_meta["title"] = session_data.name or session_data.query or "Research Result"
            og_meta["description"] = session_data.summary or session_data.query or ""
        else:
            og_meta["title"] = "Research Result"
            og_meta["description"] = "AI-powered research result on Scan by Smarter Dev."

        # Look up the creator's display name from the DB
        creator_name = ""
        if session_data:
            result = await db_session.execute(
                select(User.name).where(User.id == session_data.user_id)
            )
            creator_name = result.scalar_one_or_none() or ""

        # Query display format — default to header for older sessions
        ctx = session_data.context if session_data and session_data.context else {}
        query_format = ctx.get("query_format", "simple")
        rendered_query = ""
        if query_format == "complex" and session_data and session_data.query:
            rendered_query = render_markdown(session_data.query)

        return Template(
            "scan/result.html",
            context={
                "result_id": result_id,
                "session": session_data,
                "rendered_response": rendered_response,
                "query_format": query_format,
                "rendered_query": rendered_query,
                "og_meta": og_meta,
                "creator_name": creator_name,
            },
        )

    @get("/history", guards=[auth_guard])
    async def history(
        self,
        request: Request,
        db_session: AsyncSession,
        page: Annotated[int, Parameter(query="page", ge=1)] = 1,
    ) -> Template:
        """User's research history with pagination."""
        user_id = request.session.get("user_id", "")
        per_page = 20
        offset = (page - 1) * per_page

        sessions = await ops.list_sessions(db_session, user_id, limit=per_page + 1, offset=offset)
        has_next = len(sessions) > per_page
        sessions = sessions[:per_page]

        # Total count for display
        total_result = await db_session.execute(
            select(func.count()).select_from(ResearchSession)
            .where(ResearchSession.user_id == user_id)
        )
        total = total_result.scalar_one()

        return Template("scan/history.html", context={
            "sessions": sessions,
            "page": page,
            "has_next": has_next,
            "has_prev": page > 1,
            "total": total,
        })

    @get("/profile", guards=[auth_guard])
    async def profile(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> Template:
        """User's agent profile page."""
        user_id = request.session.get("user_id", "")

        # Fetch profile
        result = await db_session.execute(
            select(ScanUserProfile).where(ScanUserProfile.user_id == user_id)
        )
        user_profile = result.scalar_one_or_none()

        # Fetch user info
        user_result = await db_session.execute(
            select(User).where(User.id == user_id)
        )
        user = user_result.scalar_one_or_none()

        return Template("scan/profile.html", context={
            "profile": user_profile,
            "user": user,
        })

    @post("/profile/reset", guards=[auth_guard])
    async def profile_reset(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Reset the user's profile narrative, technologies, or both."""
        user_id = request.session.get("user_id", "")
        reset_type = data.get("reset_type", "").strip()
        if reset_type not in ("narrative", "technologies", "both"):
            return Redirect(path="/profile")

        result = await db_session.execute(
            select(ScanUserProfile).where(ScanUserProfile.user_id == user_id)
        )
        profile = result.scalar_one_or_none()
        if profile:
            if reset_type in ("narrative", "both"):
                profile.profile = ""
                profile.suggested_queries = None
            if reset_type in ("technologies", "both"):
                profile.technologies = None
            db_session.add(profile)
            await db_session.commit()

        return Redirect(path="/profile")

    @post("/profile/opt-out", guards=[auth_guard])
    async def profile_opt_out(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Toggle profiling opt-out for narrative, technologies, or both."""
        user_id = request.session.get("user_id", "")
        opt_out_type = data.get("opt_out_type", "").strip()
        enable = data.get("enable", "true") == "true"

        if opt_out_type not in ("narrative", "technologies", "both"):
            return Redirect(path="/profile")

        result = await db_session.execute(
            select(ScanUserProfile).where(ScanUserProfile.user_id == user_id)
        )
        profile = result.scalar_one_or_none()
        if not profile:
            # Create a profile with opt-out flags set
            profile = ScanUserProfile(user_id=user_id)
            db_session.add(profile)

        if opt_out_type in ("narrative", "both"):
            profile.opt_out_narrative = enable
            if enable:
                profile.profile = ""
                profile.suggested_queries = None
        if opt_out_type in ("technologies", "both"):
            profile.opt_out_technologies = enable
            if enable:
                profile.technologies = None

        db_session.add(profile)
        await db_session.commit()

        return Redirect(path="/profile")

    @post("/profile/technologies", guards=[auth_guard])
    async def profile_add_tech(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Add a technology to the user's profile."""
        user_id = request.session.get("user_id", "")
        name = data.get("name", "").strip()
        relationship = data.get("relationship", "").strip()
        if not name or relationship not in ("uses", "researching", "both"):
            return Redirect(path="/profile")

        result = await db_session.execute(
            select(ScanUserProfile).where(ScanUserProfile.user_id == user_id)
        )
        profile = result.scalar_one_or_none()
        if not profile:
            return Redirect(path="/profile")

        techs = list(profile.technologies or [])
        # Check for case-insensitive duplicate
        if any(t.get("name", "").lower() == name.lower() for t in techs if isinstance(t, dict)):
            return Redirect(path="/profile")

        techs.append({"name": name, "relationship": relationship})
        profile.technologies = techs
        db_session.add(profile)
        await db_session.commit()

        return Redirect(path="/profile")

    @post("/profile/technologies/remove", guards=[auth_guard])
    async def profile_remove_tech(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Remove a technology from the user's profile."""
        user_id = request.session.get("user_id", "")
        name = data.get("name", "").strip()
        if not name:
            return Redirect(path="/profile")

        result = await db_session.execute(
            select(ScanUserProfile).where(ScanUserProfile.user_id == user_id)
        )
        profile = result.scalar_one_or_none()
        if profile and profile.technologies:
            profile.technologies = [
                t for t in profile.technologies
                if not (isinstance(t, dict) and t.get("name", "").lower() == name.lower())
            ]
            db_session.add(profile)
            await db_session.commit()

        return Redirect(path="/profile")

    @post("/profile/delete-account", guards=[auth_guard])
    async def profile_delete_account(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Soft-delete the user's account and wipe PII."""
        user_id = request.session.get("user_id", "")
        confirm = data.get("confirm", "").strip()
        if confirm != "DELETE":
            return Redirect(path="/profile")

        # Soft-delete user: wipe PII and deactivate
        user_result = await db_session.execute(
            select(User).where(User.id == user_id)
        )
        user = user_result.scalar_one_or_none()
        if user:
            user.name = None
            user.email = None
            user.picture_url = None
            user.is_active = False
            db_session.add(user)

        # Delete profile entirely
        await db_session.execute(
            delete(ScanUserProfile).where(ScanUserProfile.user_id == user_id)
        )

        await db_session.commit()

        # Log them out
        request.session.clear()

        return Redirect(path="/")
