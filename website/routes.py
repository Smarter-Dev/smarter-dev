from starlette.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.templating import Jinja2Templates
from starlette.requests import Request
from sqlalchemy.orm import Session

from .models import Subscriber
from .database import get_db

# Set up templates
templates = Jinja2Templates(directory="website/templates")

# Home page route
async def home(request):
    return templates.TemplateResponse("index.html", {"request": request})

# API route for subscribing (for future use)
async def subscribe(request):
    form_data = await request.form()
    email = form_data.get("email")
    name = form_data.get("name", "")
    
    # Get DB session
    db = next(get_db())
    
    # Check if email already exists
    existing_subscriber = db.query(Subscriber).filter(Subscriber.email == email).first()
    if existing_subscriber:
        return JSONResponse({"success": False, "message": "Email already subscribed"}, status_code=400)
    
    # Create new subscriber
    new_subscriber = Subscriber(email=email, name=name)
    db.add(new_subscriber)
    db.commit()
    
    return JSONResponse({"success": True, "message": "Subscribed successfully"})
