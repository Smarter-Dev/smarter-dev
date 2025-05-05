from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union
import json
from sqlalchemy import func, desc
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse, JSONResponse
from starlette.templating import Jinja2Templates

from .models import (
    APIKey, Guild, DiscordUser, GuildMember, Kudos, UserNote,
    UserWarning, ModerationCase, PersistentRole, TemporaryRole,
    ChannelLock, BumpStat, CommandUsage, Bytes, BytesConfig, BytesRole, BytesCooldown
)
from .database import get_db
from .api_auth import generate_api_key

# Set up templates
templates = Jinja2Templates(directory="website/templates")

# Discord dashboard
async def admin_discord_dashboard(request):
    """
    Discord admin dashboard with key stats
    """
    # Get DB session
    db = next(get_db())

    # Get stats
    total_users = db.query(func.count(DiscordUser.id)).scalar() or 0
    total_guilds = db.query(func.count(Guild.id)).scalar() or 0
    total_warnings = db.query(func.count(UserWarning.id)).scalar() or 0
    total_kudos = db.query(func.count(Kudos.id)).scalar() or 0
    total_bytes = db.query(func.count(Bytes.id)).scalar() or 0
    total_cases = db.query(func.count(ModerationCase.id)).scalar() or 0

    # Get time range for recent activity
    days = 7
    time_range = datetime.now() - timedelta(days=days)

    # Recent activity
    recent_warnings = db.query(func.count(UserWarning.id)).filter(
        UserWarning.warned_at >= time_range
    ).scalar() or 0

    recent_kudos = db.query(func.count(Kudos.id)).filter(
        Kudos.awarded_at >= time_range
    ).scalar() or 0

    recent_bytes = db.query(func.count(Bytes.id)).filter(
        Bytes.awarded_at >= time_range
    ).scalar() or 0

    recent_cases = db.query(func.count(ModerationCase.id)).filter(
        ModerationCase.created_at >= time_range
    ).scalar() or 0

    # Get top users by kudos received
    top_kudos_users = db.query(
        DiscordUser.id,
        DiscordUser.username,
        func.count(Kudos.id).label('kudos_count')
    ).join(
        Kudos, Kudos.receiver_id == DiscordUser.id
    ).group_by(
        DiscordUser.id
    ).order_by(
        desc('kudos_count')
    ).limit(5).all()

    # Get top users by bytes balance
    top_bytes_users = db.query(
        DiscordUser.id,
        DiscordUser.username,
        DiscordUser.bytes_balance.label('bytes_balance')
    ).order_by(
        desc('bytes_balance')
    ).limit(5).all()

    # Get top users by warnings
    top_warned_users = db.query(
        DiscordUser.id,
        DiscordUser.username,
        func.count(UserWarning.id).label('warning_count')
    ).join(
        UserWarning, UserWarning.user_id == DiscordUser.id
    ).group_by(
        DiscordUser.id
    ).order_by(
        desc('warning_count')
    ).limit(5).all()

    # Get recent moderation cases
    recent_mod_cases = db.query(ModerationCase).order_by(
        desc(ModerationCase.created_at)
    ).limit(10).all()

    # Prepare data for charts
    # Kudos over time (last 30 days)
    kudos_dates = []
    kudos_counts = []

    time_range_30d = datetime.now() - timedelta(days=30)

    # Get kudos per day
    for i in range(30):
        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        day_start = datetime.now() - timedelta(days=i, hours=datetime.now().hour, minutes=datetime.now().minute, seconds=datetime.now().second)
        day_end = day_start + timedelta(days=1)

        count = db.query(func.count(Kudos.id)).filter(
            Kudos.awarded_at >= day_start,
            Kudos.awarded_at < day_end
        ).scalar() or 0

        kudos_dates.insert(0, date)
        kudos_counts.insert(0, count)

    # Warnings over time (last 30 days)
    warning_counts = []

    # Get warnings per day
    for i in range(30):
        day_start = datetime.now() - timedelta(days=i, hours=datetime.now().hour, minutes=datetime.now().minute, seconds=datetime.now().second)
        day_end = day_start + timedelta(days=1)

        count = db.query(func.count(UserWarning.id)).filter(
            UserWarning.warned_at >= day_start,
            UserWarning.warned_at < day_end
        ).scalar() or 0

        warning_counts.insert(0, count)

    # Prepare stats for template
    stats = {
        "total_users": total_users,
        "total_guilds": total_guilds,
        "total_warnings": total_warnings,
        "total_kudos": total_kudos,
        "total_cases": total_cases,
        "recent_warnings": recent_warnings,
        "recent_kudos": recent_kudos,
        "recent_cases": recent_cases
    }

    # Prepare chart data
    chart_data = {
        "kudos_dates": kudos_dates,
        "kudos_counts": kudos_counts,
        "warning_counts": warning_counts,
        "top_kudos_users": [user.username for user in top_kudos_users],
        "top_kudos_counts": [user.kudos_count for user in top_kudos_users],
        "top_bytes_users": [user.username for user in top_bytes_users],
        "top_bytes_counts": [user.bytes_balance for user in top_bytes_users],
        "top_warned_users": [user.username for user in top_warned_users],
        "top_warning_counts": [user.warning_count for user in top_warned_users]
    }

    return templates.TemplateResponse(
        "admin/discord/dashboard.html",
        {
            "request": request,
            "stats": stats,
            "chart_data": chart_data,
            "recent_cases": recent_mod_cases
        }
    )

# Discord users list
async def admin_discord_users(request):
    """
    List all Discord users
    """
    # Get DB session
    db = next(get_db())

    # Get users with stats
    users = db.query(DiscordUser).order_by(DiscordUser.username).all()

    # Get additional stats for each user
    user_stats = {}
    for user in users:
        kudos_received = db.query(func.count(Kudos.id)).filter(
            Kudos.receiver_id == user.id
        ).scalar() or 0

        bytes_received = db.query(func.sum(Bytes.amount)).filter(
            Bytes.receiver_id == user.id
        ).scalar() or 0

        warnings = db.query(func.count(UserWarning.id)).filter(
            UserWarning.user_id == user.id
        ).scalar() or 0

        user_stats[user.id] = {
            "kudos_received": kudos_received,
            "bytes_received": bytes_received,
            "bytes_balance": user.bytes_balance,
            "warnings": warnings
        }

    return templates.TemplateResponse(
        "admin/discord/users.html",
        {
            "request": request,
            "users": users,
            "user_stats": user_stats
        }
    )

# Discord user detail
async def admin_discord_user_detail(request):
    """
    Show details for a specific Discord user
    """
    user_id = request.path_params["id"]

    # Get DB session
    db = next(get_db())

    # Get user
    user = db.query(DiscordUser).filter(DiscordUser.id == user_id).first()
    if not user:
        return RedirectResponse(url="/admin/discord/users", status_code=302)

    # Get user stats
    kudos_received = db.query(Kudos).filter(
        Kudos.receiver_id == user.id
    ).order_by(desc(Kudos.awarded_at)).all()

    kudos_given = db.query(Kudos).filter(
        Kudos.giver_id == user.id
    ).order_by(desc(Kudos.awarded_at)).all()

    bytes_received = db.query(Bytes).filter(
        Bytes.receiver_id == user.id
    ).order_by(desc(Bytes.awarded_at)).all()

    bytes_given = db.query(Bytes).filter(
        Bytes.giver_id == user.id
    ).order_by(desc(Bytes.awarded_at)).all()

    warnings = db.query(UserWarning).filter(
        UserWarning.user_id == user.id
    ).order_by(desc(UserWarning.warned_at)).all()

    notes = db.query(UserNote).filter(
        UserNote.user_id == user.id
    ).order_by(desc(UserNote.noted_at)).all()

    mod_cases = db.query(ModerationCase).filter(
        ModerationCase.user_id == user.id
    ).order_by(desc(ModerationCase.created_at)).all()

    # Get guild memberships
    memberships = db.query(GuildMember).filter(
        GuildMember.user_id == user.id
    ).all()

    guild_ids = [m.guild_id for m in memberships]
    guilds = db.query(Guild).filter(Guild.id.in_(guild_ids)).all()

    # Map guild IDs to names
    guild_names = {g.id: g.name for g in guilds}

    # Get all guilds for the bytes form
    all_guilds = db.query(Guild).all()
    all_guild_names = {g.id: g.name for g in all_guilds}

    return templates.TemplateResponse(
        "admin/discord/user_detail.html",
        {
            "request": request,
            "user": user,
            "kudos_received": kudos_received,
            "kudos_given": kudos_given,
            "bytes_received": bytes_received,
            "bytes_given": bytes_given,
            "bytes_balance": user.bytes_balance,
            "warnings": warnings,
            "notes": notes,
            "mod_cases": mod_cases,
            "memberships": memberships,
            "guild_names": all_guild_names
        }
    )

# Give bytes to a user
async def admin_discord_give_bytes(request):
    """
    Give bytes to a user from the admin interface
    """
    user_id = request.path_params["id"]

    # Get form data
    form_data = await request.form()
    amount = int(form_data.get("amount", 10))
    reason = form_data.get("reason", "Admin award")
    guild_id = int(form_data.get("guild_id"))

    # Get DB session
    db = next(get_db())

    # Get user
    user = db.query(DiscordUser).filter(DiscordUser.id == user_id).first()
    if not user:
        return RedirectResponse(url="/admin/discord/users", status_code=302)

    # Get admin user (use a system user with discord_id of 0 for admin actions)
    admin_user = db.query(DiscordUser).filter(DiscordUser.discord_id == 0).first()
    if not admin_user:
        # Create a system user if it doesn't exist
        admin_user = DiscordUser(
            discord_id=0,
            username="System",
            bytes_balance=999999  # System user has unlimited bytes
        )
        db.add(admin_user)
        db.commit()
        db.refresh(admin_user)

    # Create bytes transaction
    bytes_obj = Bytes(
        giver_id=admin_user.id,
        receiver_id=user.id,
        guild_id=guild_id,
        amount=amount,
        reason=f"[Admin] {reason}",
        awarded_at=datetime.now()
    )

    # Update user's bytes balance
    user.bytes_balance += amount

    # Save to database
    db.add(bytes_obj)
    db.commit()

    # Print debug information
    print(f"Admin gave {amount} bytes to {user.username} (ID: {user.id})")
    print(f"New balance: {user.bytes_balance}")

    # Redirect back to user detail page
    return RedirectResponse(url=f"/admin/discord/users/{user_id}", status_code=302)

# Discord warnings list
async def admin_discord_warnings(request):
    """
    List all warnings
    """
    # Get DB session
    db = next(get_db())

    # Get warnings
    warnings = db.query(UserWarning).order_by(desc(UserWarning.warned_at)).all()

    # Get user and moderator info
    user_ids = set([w.user_id for w in warnings] + [w.mod_id for w in warnings])
    users = db.query(DiscordUser).filter(DiscordUser.id.in_(user_ids)).all()

    # Map user IDs to usernames
    usernames = {u.id: u.username for u in users}

    # Get guild info
    guild_ids = set([w.guild_id for w in warnings])
    guilds = db.query(Guild).filter(Guild.id.in_(guild_ids)).all()

    # Map guild IDs to names
    guild_names = {g.id: g.name for g in guilds}

    return templates.TemplateResponse(
        "admin/discord/warnings.html",
        {
            "request": request,
            "warnings": warnings,
            "usernames": usernames,
            "guild_names": guild_names
        }
    )

# Discord kudos list
async def admin_discord_kudos(request):
    """
    List all kudos
    """
    # Get DB session
    db = next(get_db())

    # Get kudos
    kudos = db.query(Kudos).order_by(desc(Kudos.awarded_at)).all()

    # Get user info
    user_ids = set([k.giver_id for k in kudos] + [k.receiver_id for k in kudos])
    users = db.query(DiscordUser).filter(DiscordUser.id.in_(user_ids)).all()

    # Map user IDs to usernames
    usernames = {u.id: u.username for u in users}

    # Get guild info
    guild_ids = set([k.guild_id for k in kudos])
    guilds = db.query(Guild).filter(Guild.id.in_(guild_ids)).all()

    # Map guild IDs to names
    guild_names = {g.id: g.name for g in guilds}

    return templates.TemplateResponse(
        "admin/discord/kudos.html",
        {
            "request": request,
            "kudos": kudos,
            "usernames": usernames,
            "guild_names": guild_names
        }
    )

# Discord moderation cases
async def admin_discord_moderation(request):
    """
    List all moderation cases
    """
    # Get DB session
    db = next(get_db())

    # Get cases
    cases = db.query(ModerationCase).order_by(desc(ModerationCase.created_at)).all()

    # Get user and moderator info
    user_ids = set([c.user_id for c in cases] + [c.mod_id for c in cases])
    users = db.query(DiscordUser).filter(DiscordUser.id.in_(user_ids)).all()

    # Map user IDs to usernames
    usernames = {u.id: u.username for u in users}

    # Get guild info
    guild_ids = set([c.guild_id for c in cases])
    guilds = db.query(Guild).filter(Guild.id.in_(guild_ids)).all()

    # Map guild IDs to names
    guild_names = {g.id: g.name for g in guilds}

    return templates.TemplateResponse(
        "admin/discord/moderation.html",
        {
            "request": request,
            "cases": cases,
            "usernames": usernames,
            "guild_names": guild_names
        }
    )

# Discord Bytes
async def admin_discord_bytes(request):
    """
    List all bytes transactions
    """
    # Get DB session
    db = next(get_db())

    # Get bytes
    bytes_list = db.query(Bytes).order_by(desc(Bytes.awarded_at)).all()

    # Get user info
    user_ids = set([b.giver_id for b in bytes_list] + [b.receiver_id for b in bytes_list])
    users = db.query(DiscordUser).filter(DiscordUser.id.in_(user_ids)).all()

    # Map user IDs to usernames
    usernames = {u.id: u.username for u in users}

    # Get guild info
    guild_ids = set([b.guild_id for b in bytes_list])
    guilds = db.query(Guild).filter(Guild.id.in_(guild_ids)).all()

    # Map guild IDs to names
    guild_names = {g.id: g.name for g in guilds}

    return templates.TemplateResponse(
        "admin/discord/bytes.html",
        {
            "request": request,
            "bytes": bytes_list,
            "usernames": usernames,
            "guild_names": guild_names
        }
    )

# Discord Bytes Configuration
async def admin_discord_bytes_config(request):
    """
    Manage bytes configuration
    """
    # Get DB session
    db = next(get_db())

    # Handle form submission
    if request.method == "POST":
        form_data = await request.form()
        guild_id = int(form_data.get("guild_id"))

        # Get or create config
        config = db.query(BytesConfig).filter(BytesConfig.guild_id == guild_id).first()
        if not config:
            config = BytesConfig(guild_id=guild_id)
            db.add(config)

        # Update config
        config.starting_balance = int(form_data.get("starting_balance", 100))
        config.daily_earning = int(form_data.get("daily_earning", 10))
        config.max_give_amount = int(form_data.get("max_give_amount", 50))
        config.cooldown_minutes = int(form_data.get("cooldown_minutes", 1440))

        db.commit()
        return RedirectResponse(url="/admin/discord/bytes/config", status_code=302)

    # Get all configs
    configs = db.query(BytesConfig).all()

    # Get guild info
    guild_ids = set([c.guild_id for c in configs])
    guilds = db.query(Guild).all()

    # Map guild IDs to names
    guild_names = {g.id: g.name for g in guilds}

    return templates.TemplateResponse(
        "admin/discord/bytes_config.html",
        {
            "request": request,
            "configs": configs,
            "guilds": guilds,
            "guild_names": guild_names
        }
    )

# Discord Bytes Roles
async def admin_discord_bytes_roles(request):
    """
    Manage bytes roles
    """
    # Get DB session
    db = next(get_db())

    # Handle form submission
    if request.method == "POST":
        form_data = await request.form()
        action = form_data.get("action")

        if action == "create":
            # Create new role
            role = BytesRole(
                guild_id=int(form_data.get("guild_id")),
                role_id=int(form_data.get("role_id")),
                role_name=form_data.get("role_name"),
                bytes_required=int(form_data.get("bytes_required"))
            )
            db.add(role)
            db.commit()
        elif action == "delete":
            # Delete role
            role_id = int(form_data.get("role_id"))
            role = db.query(BytesRole).filter(BytesRole.id == role_id).first()
            if role:
                db.delete(role)
                db.commit()

        return RedirectResponse(url="/admin/discord/bytes/roles", status_code=302)

    # Get all roles
    roles = db.query(BytesRole).order_by(BytesRole.guild_id, BytesRole.bytes_required).all()

    # Get guild info
    guild_ids = set([r.guild_id for r in roles])
    guilds = db.query(Guild).all()

    # Map guild IDs to names
    guild_names = {g.id: g.name for g in guilds}

    return templates.TemplateResponse(
        "admin/discord/bytes_roles.html",
        {
            "request": request,
            "roles": roles,
            "guilds": guilds,
            "guild_names": guild_names
        }
    )

# API key management
async def admin_discord_api_keys(request):
    """
    List all API keys
    """
    # Get DB session
    db = next(get_db())

    # Get API keys
    api_keys = db.query(APIKey).order_by(desc(APIKey.created_at)).all()

    return templates.TemplateResponse(
        "admin/discord/api_keys.html",
        {
            "request": request,
            "api_keys": api_keys
        }
    )

# Create API key
async def admin_discord_api_key_create(request):
    """
    Create a new API key
    """
    if request.method == "POST":
        form_data = await request.form()
        name = form_data.get("name")

        if not name:
            return templates.TemplateResponse(
                "admin/discord/api_key_form.html",
                {
                    "request": request,
                    "error": "Name is required"
                }
            )

        # Generate a new API key
        key_value = generate_api_key()

        # Create new API key
        db = next(get_db())
        api_key = APIKey(
            key=key_value,
            name=name,
            is_active=True
        )

        db.add(api_key)
        db.commit()

        # Show the key value only once
        return templates.TemplateResponse(
            "admin/discord/api_key_created.html",
            {
                "request": request,
                "api_key": api_key,
                "key_value": key_value
            }
        )

    return templates.TemplateResponse(
        "admin/discord/api_key_form.html",
        {
            "request": request
        }
    )

# Delete API key
async def admin_discord_api_key_delete(request):
    """
    Delete an API key
    """
    if request.method == "POST":
        key_id = request.path_params["id"]

        # Delete the key
        db = next(get_db())
        key = db.query(APIKey).filter(APIKey.id == key_id).first()

        if key:
            db.delete(key)
            db.commit()

        return RedirectResponse(url="/admin/discord/api-keys", status_code=302)

    return RedirectResponse(url="/admin/discord/api-keys", status_code=302)
