from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union
import json
from sqlalchemy import func, desc
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse, JSONResponse
from starlette.templating import Jinja2Templates

from .models import AdminUser, Redirect, RedirectClick
from .auth import verify_password, get_password_hash, create_admin_user
from .database import get_db

# Set up templates
templates = Jinja2Templates(directory="website/templates")

# Admin login route
async def admin_login(request):
    if request.method == "POST":
        form_data = await request.form()
        username = form_data.get("username")
        password = form_data.get("password")

        # Get DB session
        db = next(get_db())

        # Check credentials
        user = db.query(AdminUser).filter(AdminUser.username == username).first()
        if user and verify_password(password, user.hashed_password):
            # Set session
            request.session["admin_user_id"] = user.id
            return RedirectResponse(url="/admin", status_code=302)

        # Invalid credentials
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Invalid username or password"}
        )

    return templates.TemplateResponse("admin/login.html", {"request": request})

# Admin logout route
async def admin_logout(request):
    request.session.pop("admin_user_id", None)
    return RedirectResponse(url="/admin/login", status_code=302)

# Admin dashboard route
async def admin_dashboard(request):
    # Get DB session
    db = next(get_db())

    # Get stats
    total_redirects = db.query(func.count(Redirect.id)).scalar()
    total_clicks = db.query(func.count(RedirectClick.id)).scalar()

    # Today's clicks
    today = datetime.now().date()
    today_start = datetime.combine(today, datetime.min.time())
    today_end = datetime.combine(today, datetime.max.time())
    today_clicks = db.query(func.count(RedirectClick.id)).filter(
        RedirectClick.timestamp >= today_start,
        RedirectClick.timestamp <= today_end
    ).scalar()

    # Unique visitors (approximation based on IP)
    unique_visitors = db.query(func.count(func.distinct(RedirectClick.ip_address))).scalar()

    # Chart data - clicks over time (last 30 days)
    thirty_days_ago = datetime.now() - timedelta(days=30)
    clicks_by_day = db.query(
        func.date(RedirectClick.timestamp).label('date'),
        func.count(RedirectClick.id).label('count')
    ).filter(
        RedirectClick.timestamp >= thirty_days_ago
    ).group_by(
        func.date(RedirectClick.timestamp)
    ).all()

    # Format dates and counts for chart
    dates = []
    clicks = []

    # Create a dict with all dates in the last 30 days
    date_dict = {}
    for i in range(30):
        date = (datetime.now() - timedelta(days=i)).date()
        # Store dates as strings in ISO format (YYYY-MM-DD)
        date_dict[date.isoformat()] = 0

    # Fill in actual click counts
    for date_obj, count in clicks_by_day:
        try:
            # Try to convert to string if it's a date object
            if hasattr(date_obj, 'isoformat'):
                date_key = date_obj.isoformat()
            # If it's already a string, use it directly
            elif isinstance(date_obj, str):
                date_key = date_obj
            else:
                # Convert to string using str() as a fallback
                date_key = str(date_obj)

            # Update the count if the date exists in our dictionary
            if date_key in date_dict:
                date_dict[date_key] = count
        except Exception as e:
            # Log the error and continue with the next date
            print(f"Error processing date {date_obj}: {e}")
            continue

    # Sort by date and extract lists for the chart
    for date, count in sorted(date_dict.items()):
        dates.append(date)
        clicks.append(count)

    # Top redirects
    top_redirects = db.query(
        Redirect.id,
        Redirect.name,
        func.count(RedirectClick.id).label('click_count')
    ).join(
        RedirectClick, Redirect.id == RedirectClick.redirect_id
    ).group_by(
        Redirect.id
    ).order_by(
        desc('click_count')
    ).limit(5).all()

    top_redirects_names = [r.name for r in top_redirects]
    top_redirects_clicks = [r.click_count for r in top_redirects]

    # Recent redirects with click counts
    recent_redirects = db.query(
        Redirect,
        func.count(RedirectClick.id).label('click_count')
    ).outerjoin(
        RedirectClick, Redirect.id == RedirectClick.redirect_id
    ).group_by(
        Redirect.id
    ).order_by(
        desc(Redirect.created_at)
    ).limit(10).all()

    # Prepare data for template
    stats = {
        "total_redirects": total_redirects,
        "total_clicks": total_clicks,
        "today_clicks": today_clicks,
        "unique_visitors": unique_visitors
    }

    chart_data = {
        "dates": dates,
        "clicks": clicks,
        "top_redirects_names": top_redirects_names,
        "top_redirects_clicks": top_redirects_clicks
    }

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "stats": stats,
            "chart_data": chart_data,
            "recent_redirects": recent_redirects
        }
    )

# Admin redirects list route
async def admin_redirects(request):
    # Get DB session
    db = next(get_db())

    # Get all redirects with click counts
    redirects = db.query(
        Redirect,
        func.count(RedirectClick.id).label('click_count')
    ).outerjoin(
        RedirectClick, Redirect.id == RedirectClick.redirect_id
    ).group_by(
        Redirect.id
    ).order_by(
        desc(Redirect.created_at)
    ).all()

    return templates.TemplateResponse(
        "admin/redirects.html",
        {"request": request, "redirects": redirects}
    )

# Admin new redirect route
async def admin_new_redirect(request):
    if request.method == "POST":
        form_data = await request.form()
        name = form_data.get("name")
        target_url = form_data.get("target_url")
        description = form_data.get("description")
        is_active = form_data.get("is_active") == "on"

        # Get DB session
        db = next(get_db())

        # Check if name already exists
        existing = db.query(Redirect).filter(Redirect.name == name).first()
        if existing:
            return templates.TemplateResponse(
                "admin/redirect_form.html",
                {
                    "request": request,
                    "is_new": True,
                    "error": "A redirect with this name already exists."
                }
            )

        # Create new redirect
        redirect = Redirect(
            name=name,
            target_url=target_url,
            description=description,
            is_active=is_active
        )
        db.add(redirect)
        db.commit()
        db.refresh(redirect)

        # Redirect to redirects list
        return RedirectResponse(url="/admin/redirects", status_code=302)

    return templates.TemplateResponse(
        "admin/redirect_form.html",
        {"request": request, "is_new": True}
    )

# Admin edit redirect route
async def admin_edit_redirect(request):
    redirect_id = request.path_params["id"]

    # Get DB session
    db = next(get_db())

    # Get redirect
    redirect = db.query(Redirect).filter(Redirect.id == redirect_id).first()
    if not redirect:
        return RedirectResponse(url="/admin/redirects", status_code=302)

    if request.method == "POST":
        form_data = await request.form()
        target_url = form_data.get("target_url")
        description = form_data.get("description")
        is_active = form_data.get("is_active") == "on"

        # Update redirect
        redirect.target_url = target_url
        redirect.description = description
        redirect.is_active = is_active
        redirect.updated_at = datetime.now()

        db.commit()

        # Redirect to redirects list
        return RedirectResponse(url="/admin/redirects", status_code=302)

    return templates.TemplateResponse(
        "admin/redirect_form.html",
        {"request": request, "is_new": False, "redirect": redirect}
    )

# Admin delete redirect route
async def admin_delete_redirect(request):
    redirect_id = request.path_params["id"]

    # Get DB session
    db = next(get_db())

    # Get redirect
    redirect = db.query(Redirect).filter(Redirect.id == redirect_id).first()
    if redirect:
        db.delete(redirect)
        db.commit()

    # Redirect to redirects list
    return RedirectResponse(url="/admin/redirects", status_code=302)

# Admin view redirect details route
async def admin_redirect_detail(request):
    redirect_id = request.path_params["id"]

    # Get DB session
    db = next(get_db())

    # Get redirect
    redirect = db.query(Redirect).filter(Redirect.id == redirect_id).first()
    if not redirect:
        return RedirectResponse(url="/admin/redirects", status_code=302)

    # Get stats
    total_clicks = db.query(func.count(RedirectClick.id)).filter(
        RedirectClick.redirect_id == redirect.id
    ).scalar()

    # Today's clicks
    today = datetime.now().date()
    today_start = datetime.combine(today, datetime.min.time())
    today_end = datetime.combine(today, datetime.max.time())
    today_clicks = db.query(func.count(RedirectClick.id)).filter(
        RedirectClick.redirect_id == redirect.id,
        RedirectClick.timestamp >= today_start,
        RedirectClick.timestamp <= today_end
    ).scalar()

    # Unique visitors
    unique_visitors = db.query(func.count(func.distinct(RedirectClick.ip_address))).filter(
        RedirectClick.redirect_id == redirect.id
    ).scalar()

    # Chart data - clicks over time (last 30 days)
    thirty_days_ago = datetime.now() - timedelta(days=30)
    clicks_by_day = db.query(
        func.date(RedirectClick.timestamp).label('date'),
        func.count(RedirectClick.id).label('count')
    ).filter(
        RedirectClick.redirect_id == redirect.id,
        RedirectClick.timestamp >= thirty_days_ago
    ).group_by(
        func.date(RedirectClick.timestamp)
    ).all()

    # Format dates and counts for chart
    dates = []
    clicks = []

    # Create a dict with all dates in the last 30 days
    date_dict = {}
    for i in range(30):
        date = (datetime.now() - timedelta(days=i)).date()
        date_dict[date.isoformat()] = 0

    # Fill in actual click counts
    for date, count in clicks_by_day:
        date_dict[date.isoformat()] = count

    # Sort by date and extract lists for the chart
    for date, count in sorted(date_dict.items()):
        dates.append(date)
        clicks.append(count)

    # Recent clicks
    recent_clicks = db.query(RedirectClick).filter(
        RedirectClick.redirect_id == redirect.id
    ).order_by(
        desc(RedirectClick.timestamp)
    ).limit(50).all()

    # Prepare data for template
    stats = {
        "total_clicks": total_clicks,
        "today_clicks": today_clicks,
        "unique_visitors": unique_visitors
    }

    chart_data = {
        "dates": dates,
        "clicks": clicks
    }

    return templates.TemplateResponse(
        "admin/redirect_detail.html",
        {
            "request": request,
            "redirect": redirect,
            "stats": stats,
            "chart_data": chart_data,
            "recent_clicks": recent_clicks
        }
    )

# Initialize admin user
def init_admin(db: Session, username: str, email: str, password: str):
    return create_admin_user(db, username, email, password)
