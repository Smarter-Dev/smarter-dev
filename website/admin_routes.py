from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union
import json
from sqlalchemy import func, desc
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse, JSONResponse
from starlette.templating import Jinja2Templates

from .models import AdminUser, Redirect, RedirectClick, PageView, RouteError
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

# Admin analytics route
async def admin_analytics(request):
    # Get DB session
    db = next(get_db())

    # Get time range from query params (default to last 7 days)
    days = int(request.query_params.get('days', 7))
    time_range = datetime.now() - timedelta(days=days)

    # Get page view stats (excluding bots)
    total_views = db.query(func.count(PageView.id)).filter(
        PageView.is_bot == False
    ).scalar()
    recent_views = db.query(func.count(PageView.id)).filter(
        PageView.timestamp >= time_range,
        PageView.is_bot == False
    ).scalar()

    # Get unique visitors (approximation based on IP, excluding bots)
    unique_visitors = db.query(func.count(func.distinct(PageView.ip_address))).filter(
        PageView.timestamp >= time_range,
        PageView.is_bot == False
    ).scalar()

    # Get error stats
    total_errors = db.query(func.count(RouteError.id)).scalar()
    recent_errors = db.query(func.count(RouteError.id)).filter(
        RouteError.timestamp >= time_range
    ).scalar()

    # Get top pages (excluding bots)
    top_pages = db.query(
        PageView.path,
        func.count(PageView.id).label('view_count')
    ).filter(
        PageView.is_bot == False
    ).group_by(
        PageView.path
    ).order_by(
        desc('view_count')
    ).limit(10).all()

    # Get errors per page
    errors_per_page = db.query(
        RouteError.path,
        func.count(RouteError.id).label('error_count')
    ).filter(
        RouteError.timestamp >= time_range
    ).group_by(
        RouteError.path
    ).order_by(
        desc('error_count')
    ).limit(10).all()

    # Get response time statistics per page (only for pages with at least 5 views, excluding bots)
    response_times_per_page = db.query(
        PageView.path,
        func.min(PageView.response_time).label('min_response_time'),
        func.avg(PageView.response_time).label('avg_response_time'),
        func.max(PageView.response_time).label('max_response_time'),
        func.count(PageView.id).label('view_count')
    ).filter(
        PageView.timestamp >= time_range,
        PageView.is_bot == False
    ).group_by(
        PageView.path
    ).having(
        func.count(PageView.id) >= 5  # Only include pages with at least 5 views
    ).order_by(
        desc(func.max(PageView.response_time) - func.min(PageView.response_time))  # Order by range (max-min)
    ).limit(10).all()

    # Get page views over time (by day, excluding bots)
    views_by_day = db.query(
        func.date(PageView.timestamp).label('date'),
        func.count(PageView.id).label('count')
    ).filter(
        PageView.timestamp >= time_range,
        PageView.is_bot == False
    ).group_by(
        func.date(PageView.timestamp)
    ).all()

    # Get errors over time (by day)
    errors_by_day = db.query(
        func.date(RouteError.timestamp).label('date'),
        func.count(RouteError.id).label('count')
    ).filter(
        RouteError.timestamp >= time_range
    ).group_by(
        func.date(RouteError.timestamp)
    ).all()

    # Format dates and counts for chart
    dates = []
    views = []
    errors = []

    # Create a dict with all dates in the range
    date_dict = {}
    error_dict = {}
    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).date()
        date_key = date.isoformat()
        date_dict[date_key] = 0
        error_dict[date_key] = 0

    # Fill in actual view counts
    for date_obj, count in views_by_day:
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

    # Fill in actual error counts
    for date_obj, count in errors_by_day:
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
            if date_key in error_dict:
                error_dict[date_key] = count
        except Exception as e:
            # Log the error and continue with the next date
            print(f"Error processing date {date_obj}: {e}")
            continue

    # Sort by date and extract lists for the chart
    for date, count in sorted(date_dict.items()):
        dates.append(date)
        views.append(count)
        errors.append(error_dict.get(date, 0))

    # Get recent page views (excluding bots)
    recent_page_views = db.query(PageView).filter(
        PageView.is_bot == False
    ).order_by(
        desc(PageView.timestamp)
    ).limit(20).all()

    # Get recent errors
    recent_route_errors = db.query(RouteError).order_by(
        desc(RouteError.timestamp)
    ).limit(10).all()

    # Prepare data for template
    stats = {
        "total_views": total_views,
        "recent_views": recent_views,
        "unique_visitors": unique_visitors,
        "total_errors": total_errors,
        "recent_errors": recent_errors
    }

    # Calculate the difference between avg and max for stacked bar chart
    avg_to_max_diffs = []
    for p in response_times_per_page:
        avg_time = float(p[2])
        max_time = float(p[3])
        # Calculate the difference between avg and max
        diff = round(max_time - avg_time, 3)
        avg_to_max_diffs.append(diff)

    chart_data = {
        "dates": dates,
        "views": views,
        "errors": errors,
        "top_pages": [p[0] for p in top_pages],
        "top_pages_counts": [p[1] for p in top_pages],
        "errors_per_page": [p[0] for p in errors_per_page],
        "errors_per_page_counts": [p[1] for p in errors_per_page],
        "slow_pages": [p[0] for p in response_times_per_page],
        "slow_pages_min_times": [round(float(p[1]), 3) for p in response_times_per_page],
        "slow_pages_avg_times": [round(float(p[2]), 3) for p in response_times_per_page],
        "slow_pages_max_times": [round(float(p[3]), 3) for p in response_times_per_page],
        "slow_pages_avg_to_max": avg_to_max_diffs,
        "slow_pages_counts": [p[4] for p in response_times_per_page]
    }

    return templates.TemplateResponse(
        "admin/analytics.html",
        {
            "request": request,
            "stats": stats,
            "chart_data": chart_data,
            "recent_page_views": recent_page_views,
            "recent_route_errors": recent_route_errors,
            "days": days
        }
    )

# Admin bot traffic analytics route
async def admin_bot_analytics(request):
    # Get DB session
    db = next(get_db())

    # Get time range from query params (default to last 7 days)
    days = int(request.query_params.get('days', 7))
    time_range = datetime.now() - timedelta(days=days)

    # Get bot traffic stats
    total_bot_views = db.query(func.count(PageView.id)).filter(
        PageView.is_bot == True
    ).scalar()
    recent_bot_views = db.query(func.count(PageView.id)).filter(
        PageView.timestamp >= time_range,
        PageView.is_bot == True
    ).scalar()

    # Get unique bot visitors (approximation based on IP)
    unique_bot_visitors = db.query(func.count(func.distinct(PageView.ip_address))).filter(
        PageView.timestamp >= time_range,
        PageView.is_bot == True
    ).scalar()

    # Get top bot user agents
    top_bots = db.query(
        PageView.user_agent,
        func.count(PageView.id).label('view_count')
    ).filter(
        PageView.is_bot == True
    ).group_by(
        PageView.user_agent
    ).order_by(
        desc('view_count')
    ).limit(10).all()

    # Get top pages visited by bots
    top_bot_pages = db.query(
        PageView.path,
        func.count(PageView.id).label('view_count')
    ).filter(
        PageView.is_bot == True
    ).group_by(
        PageView.path
    ).order_by(
        desc('view_count')
    ).limit(10).all()

    # Get bot views over time (by day)
    bot_views_by_day = db.query(
        func.date(PageView.timestamp).label('date'),
        func.count(PageView.id).label('count')
    ).filter(
        PageView.timestamp >= time_range,
        PageView.is_bot == True
    ).group_by(
        func.date(PageView.timestamp)
    ).all()

    # Format dates and counts for chart
    dates = []
    bot_views = []

    # Create a dict with all dates in the range
    date_dict = {}
    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).date()
        date_key = date.isoformat()
        date_dict[date_key] = 0

    # Fill in actual bot view counts
    for date_obj, count in bot_views_by_day:
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
        bot_views.append(count)

    # Get recent bot views
    recent_bot_views_list = db.query(PageView).filter(
        PageView.is_bot == True
    ).order_by(
        desc(PageView.timestamp)
    ).limit(20).all()

    # Prepare data for template
    stats = {
        "total_bot_views": total_bot_views,
        "recent_bot_views": recent_bot_views,
        "unique_bot_visitors": unique_bot_visitors
    }

    chart_data = {
        "dates": dates,
        "bot_views": bot_views,
        "top_bots": [b[0] for b in top_bots],
        "top_bots_counts": [b[1] for b in top_bots],
        "top_bot_pages": [p[0] for p in top_bot_pages],
        "top_bot_pages_counts": [p[1] for p in top_bot_pages]
    }

    return templates.TemplateResponse(
        "admin/bot_analytics.html",
        {
            "request": request,
            "stats": stats,
            "chart_data": chart_data,
            "recent_bot_views": recent_bot_views_list,
            "days": days
        }
    )

# Admin error details route
async def admin_error_detail(request):
    error_id = request.path_params["id"]

    # Get DB session
    db = next(get_db())

    # Get error
    error = db.query(RouteError).filter(RouteError.id == error_id).first()
    if not error:
        return RedirectResponse(url="/admin/analytics", status_code=302)

    return templates.TemplateResponse(
        "admin/error_detail.html",
        {"request": request, "error": error}
    )

# Initialize admin user
def init_admin(db: Session, username: str, email: str, password: str):
    return create_admin_user(db, username, email, password)
