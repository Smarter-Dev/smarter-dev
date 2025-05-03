from starlette.requests import Request
from starlette.responses import RedirectResponse
from sqlalchemy.orm import Session

from .models import Redirect, RedirectClick
from .database import get_db

async def handle_redirect(request):
    # Get the redirect name from the path
    path = request.url.path.strip("/")
    
    # Get DB session
    db = next(get_db())
    
    # Look up the redirect
    redirect = db.query(Redirect).filter(Redirect.name == path, Redirect.is_active == True).first()
    
    if redirect:
        # Track the click
        click = RedirectClick(
            redirect_id=redirect.id,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            referer=request.headers.get("referer")
        )
        db.add(click)
        db.commit()
        
        # Redirect to the target URL
        return RedirectResponse(url=redirect.target_url, status_code=302)
    
    # If no redirect found, return to home page
    return RedirectResponse(url="/", status_code=302)
