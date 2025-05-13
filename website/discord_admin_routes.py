from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union
import json
import httpx
import os
import hikari
import humanize
from sqlalchemy import func, desc
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse, JSONResponse
from starlette.templating import Jinja2Templates

from .discord_rest import get_guild_roles, get_role_name
from .api_client import APIClient

# Initialize API client with no token - we'll set it per request
api_client = APIClient()

from .models import (
    APIKey, Guild, DiscordUser, GuildMember, UserNote,
    UserWarning, ModerationCase, PersistentRole, TemporaryRole,
    ChannelLock, BumpStat, CommandUsage, Bytes, BytesConfig, BytesRole, BytesCooldown,
    AutoModRegexRule, AutoModRateLimit, Squad, SquadMember,
    AutoModFileExtensionRule, FileAttachment
)
from .database import get_db
from .api_auth import generate_api_key

# Set up templates
templates = Jinja2Templates(directory="website/templates")

# Squad management routes
async def admin_discord_squads(request):
    """
    List all squads
    """
    # Get DB session
    db = next(get_db())

    # Get all squads
    squads = db.query(Squad).order_by(Squad.guild_id, Squad.name).all()

    # Get guild info
    guild_ids = set([s.guild_id for s in squads])
    guilds = db.query(Guild).all()

    # Map guild IDs to names
    guild_names = {g.id: g.name for g in guilds}

    return templates.TemplateResponse(
        "admin/discord/squads.html",
        {
            "request": request,
            "squads": squads,
            "guilds": guilds,
            "guild_names": guild_names
        }
    )

async def admin_discord_squad_create(request):
    """
    Create a new squad
    """
    # Get DB session
    db = next(get_db())

    # Get all guilds for the form
    guilds = db.query(Guild).order_by(Guild.name).all()

    if request.method == "POST":
        form_data = await request.form()
        guild_id = int(form_data.get("guild_id"))
        role_id = int(form_data.get("role_id"))
        name = form_data.get("name")
        description = form_data.get("description")
        is_active = form_data.get("is_active") == "on"

        # Create new squad
        squad = Squad(
            guild_id=guild_id,
            role_id=role_id,
            name=name,
            description=description,
            is_active=is_active
        )

        db.add(squad)
        db.commit()

        # Redirect to squads list
        return RedirectResponse(url="/admin/discord/squads", status_code=303)

    return templates.TemplateResponse(
        "admin/discord/squad_form.html",
        {
            "request": request,
            "guilds": guilds,
            "squad": None
        }
    )

async def admin_discord_squad_edit(request):
    """
    Edit a squad
    """
    squad_id = request.path_params["id"]
    db = next(get_db())

    # Get the squad
    squad = db.query(Squad).filter(Squad.id == squad_id).first()
    if not squad:
        return RedirectResponse(url="/admin/discord/squads", status_code=303)

    # Get all guilds for the form
    guilds = db.query(Guild).order_by(Guild.name).all()

    if request.method == "POST":
        form_data = await request.form()
        guild_id = int(form_data.get("guild_id"))
        role_id = int(form_data.get("role_id"))
        name = form_data.get("name")
        description = form_data.get("description")
        is_active = form_data.get("is_active") == "on"

        # Update squad
        squad.guild_id = guild_id
        squad.role_id = role_id
        squad.name = name
        squad.description = description
        squad.is_active = is_active

        db.commit()

        # Redirect to squads list
        return RedirectResponse(url="/admin/discord/squads", status_code=303)

    return templates.TemplateResponse(
        "admin/discord/squad_form.html",
        {
            "request": request,
            "guilds": guilds,
            "squad": squad
        }
    )

async def admin_discord_squad_delete(request):
    """
    Delete a squad
    """
    squad_id = request.path_params["id"]
    db = next(get_db())

    if request.method == "POST":
        # Get the squad
        squad = db.query(Squad).filter(Squad.id == squad_id).first()
        if squad:
            # Delete all squad members first
            db.query(SquadMember).filter(SquadMember.squad_id == squad.id).delete()
            # Then delete the squad
            db.delete(squad)
            db.commit()

    # Redirect to squads list
    return RedirectResponse(url="/admin/discord/squads", status_code=303)

async def admin_discord_squad_members(request):
    """
    List all members of a squad
    """
    squad_id = request.path_params["id"]
    db = next(get_db())

    # Get the squad
    squad = db.query(Squad).filter(Squad.id == squad_id).first()
    if not squad:
        return RedirectResponse(url="/admin/discord/squads", status_code=303)

    # Get the guild
    guild = db.query(Guild).filter(Guild.id == squad.guild_id).first()

    # Get all members with user details
    members = db.query(SquadMember, DiscordUser).join(
        DiscordUser, SquadMember.user_id == DiscordUser.id
    ).filter(SquadMember.squad_id == squad.id).all()

    return templates.TemplateResponse(
        "admin/discord/squad_members.html",
        {
            "request": request,
            "squad": squad,
            "guild": guild,
            "members": members
        }
    )

# Discord Guilds management
async def admin_discord_guilds(request):
    """
    Manage Discord guilds
    """

    # Get DB session
    db = next(get_db())

    # Get all guilds
    guilds = db.query(Guild).order_by(Guild.name).all()

    # Create a mapping of role IDs to role names
    role_names = {}
    for guild in guilds:
        if guild.moderator_role_id:
            try:
                # Fetch role name
                role_name = await get_role_name(guild.discord_id, guild.moderator_role_id)
                if role_name:
                    role_names[guild.moderator_role_id] = role_name
            except Exception as e:
                print(f"Error fetching role name for guild {guild.discord_id}: {e}")

    # Format dates with humanize
    for guild in guilds:
        if guild.joined_at:
            guild.joined_at_humanized = humanize.naturaltime(guild.joined_at)
        else:
            guild.joined_at_humanized = "Unknown"

    return templates.TemplateResponse(
        "admin/discord/guilds.html",
        {
            "request": request,
            "guilds": guilds,
            "role_names": role_names
        }
    )

async def admin_discord_guild_edit(request):
    """
    Edit a guild
    """
    guild_id = request.path_params["id"]
    db = next(get_db())

    # Get the guild
    guild = db.query(Guild).filter(Guild.id == guild_id).first()
    if not guild:
        return RedirectResponse(url="/admin/discord/guilds", status_code=303)

    if request.method == "POST":
        form_data = await request.form()
        name = form_data.get("name")
        moderator_role_id = form_data.get("moderator_role_id")

        # Update guild
        guild.name = name
        if moderator_role_id:
            guild.moderator_role_id = int(moderator_role_id)
        else:
            guild.moderator_role_id = None

        db.commit()

        # Redirect to guilds list
        return RedirectResponse(url="/admin/discord/guilds", status_code=303)

    return RedirectResponse(url="/admin/discord/guilds", status_code=303)

async def admin_discord_guild_roles(request):
    """
    View roles for a guild
    """

    guild_id = request.path_params["id"]
    db = next(get_db())

    # Get the guild
    guild = db.query(Guild).filter(Guild.id == guild_id).first()
    if not guild:
        return RedirectResponse(url="/admin/discord/guilds", status_code=303)

    # Fetch roles from Discord
    roles = []
    error_message = None
    try:
        print(f"Fetching roles for guild ID: {guild_id}, discord_id: {guild.discord_id}")
        roles = await get_guild_roles(guild.discord_id)
        if not roles:
            error_message = "No roles found or unable to fetch roles from Discord."
    except Exception as e:
        error_message = f"Error fetching roles: {e}"
        print(error_message)

    return templates.TemplateResponse(
        "admin/discord/guild_roles.html",
        {
            "request": request,
            "guild": guild,
            "roles": roles,
            "error_message": error_message
        }
    )

async def admin_discord_guild_delete(request):
    """
    Delete a guild (bot leaves the server)
    """
    guild_id = request.path_params["id"]
    db = next(get_db())

    if request.method == "POST":
        # Get the guild
        guild = db.query(Guild).filter(Guild.id == guild_id).first()
        if guild:
            # Delete all related data first
            db.query(BytesConfig).filter(BytesConfig.guild_id == guild.id).delete()
            db.query(BytesRole).filter(BytesRole.guild_id == guild.id).delete()
            db.query(BytesCooldown).filter(BytesCooldown.guild_id == guild.id).delete()
            db.query(AutoModRegexRule).filter(AutoModRegexRule.guild_id == guild.id).delete()
            db.query(AutoModRateLimit).filter(AutoModRateLimit.guild_id == guild.id).delete()
            db.query(AutoModFileExtensionRule).filter(AutoModFileExtensionRule.guild_id == guild.id).delete()
            db.query(FileAttachment).filter(FileAttachment.guild_id == guild.id).delete()
            db.query(Squad).filter(Squad.guild_id == guild.id).delete()
            db.query(GuildMember).filter(GuildMember.guild_id == guild.id).delete()
            
            # Delete the guild
            db.delete(guild)
            db.commit()

            # TODO: Add Discord API call to leave the server
            # This would require the bot token and Discord API integration

    return RedirectResponse(url="/admin/discord/guilds", status_code=303)

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
    total_bytes = db.query(func.count(Bytes.id)).scalar() or 0
    total_cases = db.query(func.count(ModerationCase.id)).scalar() or 0

    # Get time range for recent activity
    days = 7
    time_range = datetime.now() - timedelta(days=days)

    # Recent activity
    recent_warnings = db.query(func.count(UserWarning.id)).filter(
        UserWarning.warned_at >= time_range
    ).scalar() or 0

    recent_bytes = db.query(func.count(Bytes.id)).filter(
        Bytes.awarded_at >= time_range
    ).scalar() or 0

    recent_cases = db.query(func.count(ModerationCase.id)).filter(
        ModerationCase.created_at >= time_range
    ).scalar() or 0



    # Get top users by bytes balance (excluding system user with discord_id=0)
    top_bytes_users = db.query(
        DiscordUser.id,
        DiscordUser.username,
        DiscordUser.bytes_balance.label('bytes_balance')
    ).filter(
        DiscordUser.discord_id != 0  # Exclude system user
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
    # Dates for the last 30 days
    dates = []
    for i in range(30):
        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        dates.insert(0, date)

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
        "total_cases": total_cases,
        "recent_warnings": recent_warnings,
        "recent_cases": recent_cases
    }

    # Prepare chart data
    chart_data = {
        "dates": dates,
        "warning_counts": warning_counts,
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
        bytes_received = db.query(func.sum(Bytes.amount)).filter(
            Bytes.receiver_id == user.id
        ).scalar() or 0

        warnings = db.query(func.count(UserWarning.id)).filter(
            UserWarning.user_id == user.id
        ).scalar() or 0

        user_stats[user.id] = {
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
        config.squad_join_bytes_required = int(form_data.get("squad_join_bytes_required", 100))

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

async def refresh_api_token(request) -> Optional[str]:
    """
    Refresh the API token if needed
    """
    db = next(get_db())
    admin_key = db.query(APIKey).filter(APIKey.name == "Admin Interface").first()
    if not admin_key:
        return None

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/api/auth/token",
            json={"api_key": admin_key.key}
        )
        if response.status_code == 200:
            data = response.json()
            request.session["api_token"] = data["token"]
            return data["token"]
    return None

# Auto Moderation
async def admin_discord_automod(request):
    """
    Admin page for Discord auto moderation settings
    """
    # Get all guilds
    db = next(get_db())
    guilds = db.query(Guild).all()

    # Get selected guild
    selected_guild_id = request.query_params.get("guild_id")
    selected_guild = None
    if selected_guild_id:
        selected_guild = db.query(Guild).filter(Guild.id == selected_guild_id).first()

    # Get file extension rules for the selected guild
    file_extension_rules = []
    if selected_guild:
        # Get or refresh API token
        api_token = request.session.get('api_token')
        if not api_token:
            api_token = await refresh_api_token(request)
            if not api_token:
                return templates.TemplateResponse(
                    "admin/discord/automod.html",
                    {
                        "request": request,
                        "guilds": guilds,
                        "selected_guild": selected_guild,
                        "file_extension_rules": [],
                        "rate_limits": [],
                        "regex_rules": [],
                        "error": "No API token found. Please log in again."
                    }
                )

        # Set API token for this request
        api_client.api_token = api_token

        # Use the Discord guild ID to fetch rules
        response = await api_client.get_file_extension_rules(selected_guild.discord_id)
        if response.status_code == 401:
            # Token expired, try to refresh
            api_token = await refresh_api_token(request)
            if api_token:
                api_client.api_token = api_token
                response = await api_client.get_file_extension_rules(selected_guild.discord_id)

        if response.status_code == 200:
            data = response.json()
            print("File extension rules response:", data)  # Debug print
            for rule in data.get("rules", []):
                # Parse the created_at field from ISO format to datetime
                if "created_at" in rule:
                    rule["created_at"] = datetime.fromisoformat(rule["created_at"].replace("Z", "+00:00"))
                file_extension_rules.append(rule)

    # Get rate limits for the selected guild
    rate_limits = []
    if selected_guild:
        response = await api_client.get_rate_limits(selected_guild.discord_id)
        if response.status_code == 200:
            data = response.json()
            for limit in data.get("limits", []):
                # Parse the created_at field from ISO format to datetime
                if "created_at" in limit:
                    limit["created_at"] = datetime.fromisoformat(limit["created_at"].replace("Z", "+00:00"))
                rate_limits.append(limit)

    # Get regex rules for the selected guild
    regex_rules = []
    if selected_guild:
        response = await api_client.get_regex_rules(selected_guild.discord_id)
        if response.status_code == 200:
            data = response.json()
            for rule in data.get("rules", []):
                # Parse the created_at field from ISO format to datetime
                if "created_at" in rule:
                    rule["created_at"] = datetime.fromisoformat(rule["created_at"].replace("Z", "+00:00"))
                regex_rules.append(rule)

    return templates.TemplateResponse(
        "admin/discord/automod.html",
        {
            "request": request,
            "guilds": guilds,
            "selected_guild": selected_guild,
            "file_extension_rules": file_extension_rules,
            "rate_limits": rate_limits,
            "regex_rules": regex_rules,
            "api_token": request.session.get('api_token')  # Pass the API token to the template
        }
    )

async def admin_discord_file_attachments(request):
    """
    View file attachment history
    """
    # Get DB session
    db = next(get_db())

    # Get all guilds for the form
    guilds = db.query(Guild).order_by(Guild.name).all()

    # Get selected guild
    guild_id = request.query_params.get("guild_id")
    selected_guild = None
    attachments = []
    total = 0

    if guild_id:
        selected_guild = db.query(Guild).filter(Guild.id == guild_id).first()
        if selected_guild:
            # Get query parameters
            limit = int(request.query_params.get("limit", 50))
            offset = int(request.query_params.get("offset", 0))
            extension = request.query_params.get("extension")
            was_allowed = request.query_params.get("was_allowed")
            was_deleted = request.query_params.get("was_deleted")

            # Build query
            query = db.query(FileAttachment).filter(
                FileAttachment.guild_id == selected_guild.id
            )

            if extension:
                query = query.filter(FileAttachment.extension == extension)
            if was_allowed is not None:
                query = query.filter(FileAttachment.was_allowed == (was_allowed.lower() == "true"))
            if was_deleted is not None:
                query = query.filter(FileAttachment.was_deleted == (was_deleted.lower() == "true"))

            # Get total count
            total = query.count()

            # Get paginated results
            attachments = query.order_by(FileAttachment.created_at.desc()).offset(offset).limit(limit).all()

    return templates.TemplateResponse(
        "admin/discord/file_attachments.html",
        {
            "request": request,
            "guilds": guilds,
            "selected_guild": selected_guild,
            "attachments": attachments,
            "total": total,
            "limit": int(request.query_params.get("limit", 50)),
            "offset": int(request.query_params.get("offset", 0)),
            "extension": request.query_params.get("extension"),
            "was_allowed": request.query_params.get("was_allowed"),
            "was_deleted": request.query_params.get("was_deleted")
        }
    )
