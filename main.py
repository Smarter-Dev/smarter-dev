from starlette.applications import Starlette
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
import uvicorn
from sqlalchemy import select

# Import FastAPI app
from smarter_dev.web.api.app import api
# Import admin routes
from smarter_dev.web.admin.routes import admin_routes
# Import settings
from smarter_dev.shared.config import get_settings
# Import security headers middleware
from smarter_dev.web.security_headers import create_security_headers_middleware
# Import HTTP methods middleware
from smarter_dev.web.http_methods_middleware import create_http_methods_middleware
# Import blog models and database
from smarter_dev.web.models import BlogPost
from smarter_dev.shared.database import get_db_session_context
# Import public views
from smarter_dev.web.public_views import campaigns_list, campaign_detail, challenge_detail, campaign_leaderboard

import markdown
import re

templates = Jinja2Templates(directory="templates")

# Add markdown filter to Jinja2
def markdown_filter(text: str) -> str:
    """Convert markdown text to HTML."""
    md = markdown.Markdown(extensions=['codehilite', 'fenced_code', 'tables', 'toc'])
    return md.convert(text)

def strip_markdown_filter(text: str, max_length: int = 200) -> str:
    """Strip markdown formatting and create a text excerpt."""
    # Remove markdown headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove markdown links but keep link text
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    # Remove markdown emphasis
    text = re.sub(r'[*_]{1,2}([^*_]+)[*_]{1,2}', r'\1', text)
    # Remove code blocks and inline code
    text = re.sub(r'```[^`]*```', '', text, flags=re.DOTALL)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove blockquotes
    text = re.sub(r'^>\s+', '', text, flags=re.MULTILINE)
    # Clean up multiple whitespace
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    
    # Truncate to max_length
    if len(text) > max_length:
        text = text[:max_length].rsplit(' ', 1)[0] + '...'
    
    return text

templates.env.filters['markdown'] = markdown_filter
templates.env.filters['strip_markdown'] = strip_markdown_filter

# Make settings available in templates
templates.env.globals['config'] = get_settings()

async def homepage(request: Request):
    try:
        # Get the latest published blog post for the homepage
        async with get_db_session_context() as session:
            result = await session.execute(
                select(BlogPost)
                .where(BlogPost.is_published == True)
                .order_by(BlogPost.published_at.desc())
                .limit(1)
            )
            latest_post = result.scalar_one_or_none()
            
            # Get recent blog posts for the list
            recent_posts_result = await session.execute(
                select(BlogPost)
                .where(BlogPost.is_published == True)
                .order_by(BlogPost.published_at.desc())
                .limit(5)
            )
            recent_posts = recent_posts_result.scalars().all()
            
        return templates.TemplateResponse(
            request, 
            "index.html", 
            {
                "latest_post": latest_post,
                "recent_posts": recent_posts
            }
        )
    except Exception as e:
        # If there's an error, just show the homepage without blog content
        return templates.TemplateResponse(request, "index.html")

async def discord_redirect(request: Request):
    return RedirectResponse(url="https://discord.gg/de8kajxbYS", status_code=302)

async def about_us(request: Request) -> HTMLResponse:
    """Display the About Us page."""
    return templates.TemplateResponse(request, "about.html", {"title": "About Us"})

async def not_found(request: Request, exc: HTTPException):
    return templates.TemplateResponse(request, "404.html", status_code=404)

async def server_error(request: Request, exc: HTTPException):
    return templates.TemplateResponse(request, "500.html", status_code=500)


async def blog_list(request: Request) -> HTMLResponse:
    """Display a list of all published blog posts."""
    try:
        async with get_db_session_context() as session:
            # Get all published blog posts
            result = await session.execute(
                select(BlogPost)
                .where(BlogPost.is_published == True)
                .order_by(BlogPost.published_at.desc())
            )
            blog_posts = result.scalars().all()
            
            return templates.TemplateResponse(
                request,
                "blog_list.html",
                {
                    "blog_posts": blog_posts,
                    "title": "Blog"
                }
            )
    
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")


async def blog_post_detail(request: Request) -> HTMLResponse:
    """Display a single blog post by slug."""
    slug = request.path_params["slug"]
    
    try:
        async with get_db_session_context() as session:
            # Get the blog post by slug
            result = await session.execute(
                select(BlogPost)
                .where(BlogPost.slug == slug, BlogPost.is_published == True)
            )
            blog_post = result.scalar_one_or_none()
            
            if not blog_post:
                raise HTTPException(status_code=404, detail="Blog post not found")
            
            return templates.TemplateResponse(
                request,
                "blog_post.html",
                {
                    "blog_post": blog_post,
                    "title": blog_post.title
                }
            )
    
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")

routes = [
    Route("/", homepage),
    Route("/discord", discord_redirect),
    Route("/about", about_us),
    Route("/blog", blog_list),
    Route("/blog/{slug}", blog_post_detail),
    # Public campaign and challenge routes
    Route("/campaigns", campaigns_list),
    Route("/campaigns/{campaign_id}", campaign_detail),
    Route("/campaigns/{campaign_id}/leaderboard", campaign_leaderboard),
    Route("/challenges/{challenge_id}", challenge_detail),
]

exception_handlers = {
    404: not_found,
    500: server_error,
}

# Get settings for session secret
settings = get_settings()

# Set up middleware
middleware = [
    # HTTP methods middleware (applied first to handle method validation)
    Middleware(create_http_methods_middleware(starlette_compatible=True)),
    
    # Security headers middleware (applied second for all responses)
    Middleware(create_security_headers_middleware(starlette_compatible=True)),
    
    # Session middleware
    Middleware(
        SessionMiddleware,
        secret_key=settings.web_session_secret,
        max_age=86400 * 7,  # 7 days
        same_site="lax",
        https_only=settings.is_production,
    )
]

app = Starlette(
    routes=routes,
    exception_handlers=exception_handlers,
    middleware=middleware,
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# Mount the FastAPI application at /api
app.mount("/api", api)

# Mount admin interface at /admin
app.mount("/admin", Mount("", routes=admin_routes))

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8888, reload=True)
