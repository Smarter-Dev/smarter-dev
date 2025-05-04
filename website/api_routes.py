from typing import List, Optional, Dict, Any
from datetime import datetime
from sqlalchemy import func, desc
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import JSONResponse

from .models import (
    APIKey, Guild, DiscordUser, GuildMember, Kudos, UserNote,
    UserWarning, ModerationCase, PersistentRole, TemporaryRole,
    ChannelLock, BumpStat, CommandUsage
)
from .database import get_db
from .api_auth import create_jwt_token, verify_api_key, generate_api_key

# API Authentication endpoint
async def api_token(request):
    """
    Generate a JWT token from an API key
    """
    if request.method == "POST":
        try:
            data = await request.json()
            api_key = data.get("api_key")

            if not api_key:
                return JSONResponse(
                    {"error": "Missing API key"},
                    status_code=400
                )

            # Verify API key
            db = next(get_db())
            key = verify_api_key(api_key, db)

            if not key:
                return JSONResponse(
                    {"error": "Invalid API key"},
                    status_code=401
                )

            # Generate token
            token = create_jwt_token(key.id, key.name)

            return JSONResponse({
                "token": token,
                "expires_in": 3600  # 1 hour
            })

        except Exception as e:
            return JSONResponse(
                {"error": str(e)},
                status_code=500
            )

    return JSONResponse(
        {"error": "Method not allowed"},
        status_code=405
    )

# Helper function to convert model to dict
def model_to_dict(model, exclude_fields=None):
    """
    Convert a SQLAlchemy model instance to a dictionary
    """
    if exclude_fields is None:
        exclude_fields = []

    result = {}
    for column in model.__table__.columns:
        if column.name not in exclude_fields:
            value = getattr(model, column.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            result[column.name] = value

    return result

# Error handling decorator
def api_error_handler(func):
    """
    Decorator to handle API errors
    """
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            return JSONResponse(
                {"error": str(e)},
                status_code=500
            )

    return wrapper


# Guild API endpoints
@api_error_handler
async def guild_list(request):
    """
    List all guilds
    """
    db = next(get_db())
    guilds = db.query(Guild).all()
    return JSONResponse({
        "guilds": [model_to_dict(guild) for guild in guilds]
    })

@api_error_handler
async def guild_detail(request):
    """
    Get guild details
    """
    guild_id = request.path_params["guild_id"]
    db = next(get_db())

    guild = db.query(Guild).filter(Guild.id == guild_id).first()
    if not guild:
        return JSONResponse({"error": "Guild not found"}, status_code=404)

    return JSONResponse(model_to_dict(guild))

@api_error_handler
async def guild_create(request):
    """
    Create a new guild
    """
    data = await request.json()
    db = next(get_db())

    # Check if guild already exists
    existing = db.query(Guild).filter(Guild.discord_id == data["discord_id"]).first()
    if existing:
        return JSONResponse(model_to_dict(existing))

    # Create new guild
    guild = Guild(
        discord_id=data["discord_id"],
        name=data["name"],
        icon_url=data.get("icon_url"),
        joined_at=datetime.now() if "joined_at" not in data else data["joined_at"]
    )

    db.add(guild)
    db.commit()
    db.refresh(guild)

    return JSONResponse(model_to_dict(guild), status_code=201)

@api_error_handler
async def guild_update(request):
    """
    Update a guild
    """
    guild_id = request.path_params["guild_id"]
    data = await request.json()
    db = next(get_db())

    guild = db.query(Guild).filter(Guild.id == guild_id).first()
    if not guild:
        return JSONResponse({"error": "Guild not found"}, status_code=404)

    # Update fields
    if "name" in data:
        guild.name = data["name"]
    if "icon_url" in data:
        guild.icon_url = data["icon_url"]

    db.commit()
    db.refresh(guild)

    return JSONResponse(model_to_dict(guild))


# User API endpoints
@api_error_handler
async def user_list(request):
    """
    List all users
    """
    db = next(get_db())
    users = db.query(DiscordUser).all()
    return JSONResponse({
        "users": [model_to_dict(user) for user in users]
    })

@api_error_handler
async def user_detail(request):
    """
    Get user details
    """
    user_id = request.path_params["user_id"]
    db = next(get_db())

    user = db.query(DiscordUser).filter(DiscordUser.id == user_id).first()
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)

    return JSONResponse(model_to_dict(user))

@api_error_handler
async def user_create(request):
    """
    Create a new user
    """
    data = await request.json()
    db = next(get_db())

    # Check if user already exists
    existing = db.query(DiscordUser).filter(DiscordUser.discord_id == data["discord_id"]).first()
    if existing:
        return JSONResponse(model_to_dict(existing))

    # Create new user
    user = DiscordUser(
        discord_id=data["discord_id"],
        username=data["username"],
        discriminator=data.get("discriminator"),
        avatar_url=data.get("avatar_url")
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return JSONResponse(model_to_dict(user), status_code=201)

@api_error_handler
async def user_update(request):
    """
    Update a user
    """
    user_id = request.path_params["user_id"]
    data = await request.json()
    db = next(get_db())

    user = db.query(DiscordUser).filter(DiscordUser.id == user_id).first()
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)

    # Update fields
    if "username" in data:
        user.username = data["username"]
    if "discriminator" in data:
        user.discriminator = data["discriminator"]
    if "avatar_url" in data:
        user.avatar_url = data["avatar_url"]

    db.commit()
    db.refresh(user)

    return JSONResponse(model_to_dict(user))


# Kudos API endpoints
@api_error_handler
async def kudos_list(request):
    """
    List all kudos, with optional filtering
    """
    db = next(get_db())
    query = db.query(Kudos)

    # Filter by guild
    guild_id = request.query_params.get("guild_id")
    if guild_id:
        query = query.filter(Kudos.guild_id == guild_id)

    # Filter by user (giver or receiver)
    user_id = request.query_params.get("user_id")
    if user_id:
        query = query.filter(
            (Kudos.giver_id == user_id) | (Kudos.receiver_id == user_id)
        )

    # Filter by receiver only
    receiver_id = request.query_params.get("receiver_id")
    if receiver_id:
        query = query.filter(Kudos.receiver_id == receiver_id)

    # Filter by giver only
    giver_id = request.query_params.get("giver_id")
    if giver_id:
        query = query.filter(Kudos.giver_id == giver_id)

    # Order by most recent
    kudos = query.order_by(desc(Kudos.awarded_at)).all()

    return JSONResponse({
        "kudos": [model_to_dict(k) for k in kudos]
    })

@api_error_handler
async def kudos_detail(request):
    """
    Get kudos details
    """
    kudos_id = request.path_params["kudos_id"]
    db = next(get_db())

    kudos = db.query(Kudos).filter(Kudos.id == kudos_id).first()
    if not kudos:
        return JSONResponse({"error": "Kudos not found"}, status_code=404)

    return JSONResponse(model_to_dict(kudos))

@api_error_handler
async def kudos_create(request):
    """
    Create a new kudos award
    """
    data = await request.json()
    db = next(get_db())

    # Validate required fields
    required_fields = ["giver_id", "receiver_id", "guild_id"]
    for field in required_fields:
        if field not in data:
            return JSONResponse({"error": f"Missing required field: {field}"}, status_code=400)

    # Create new kudos
    kudos = Kudos(
        giver_id=data["giver_id"],
        receiver_id=data["receiver_id"],
        guild_id=data["guild_id"],
        amount=data.get("amount", 1),
        reason=data.get("reason")
    )

    db.add(kudos)
    db.commit()
    db.refresh(kudos)

    return JSONResponse(model_to_dict(kudos), status_code=201)


# Warning API endpoints
@api_error_handler
async def warning_list(request):
    """
    List all warnings, with optional filtering
    """
    db = next(get_db())
    query = db.query(UserWarning)

    # Filter by guild
    guild_id = request.query_params.get("guild_id")
    if guild_id:
        query = query.filter(UserWarning.guild_id == guild_id)

    # Filter by user
    user_id = request.query_params.get("user_id")
    if user_id:
        query = query.filter(UserWarning.user_id == user_id)

    # Filter by moderator
    mod_id = request.query_params.get("mod_id")
    if mod_id:
        query = query.filter(UserWarning.mod_id == mod_id)

    # Order by most recent
    warnings = query.order_by(desc(UserWarning.warned_at)).all()

    return JSONResponse({
        "warnings": [model_to_dict(w) for w in warnings]
    })

@api_error_handler
async def warning_detail(request):
    """
    Get warning details
    """
    warning_id = request.path_params["warning_id"]
    db = next(get_db())

    warning = db.query(UserWarning).filter(UserWarning.id == warning_id).first()
    if not warning:
        return JSONResponse({"error": "Warning not found"}, status_code=404)

    return JSONResponse(model_to_dict(warning))

@api_error_handler
async def warning_create(request):
    """
    Create a new warning
    """
    data = await request.json()
    db = next(get_db())

    # Validate required fields
    required_fields = ["user_id", "mod_id", "guild_id"]
    for field in required_fields:
        if field not in data:
            return JSONResponse({"error": f"Missing required field: {field}"}, status_code=400)

    # Create new warning
    warning = UserWarning(
        user_id=data["user_id"],
        mod_id=data["mod_id"],
        guild_id=data["guild_id"],
        reason=data.get("reason")
    )

    db.add(warning)
    db.commit()
    db.refresh(warning)

    return JSONResponse(model_to_dict(warning), status_code=201)


# Moderation Case API endpoints
@api_error_handler
async def moderation_case_list(request):
    """
    List all moderation cases, with optional filtering
    """
    db = next(get_db())
    query = db.query(ModerationCase)

    # Filter by guild
    guild_id = request.query_params.get("guild_id")
    if guild_id:
        query = query.filter(ModerationCase.guild_id == guild_id)

    # Filter by user
    user_id = request.query_params.get("user_id")
    if user_id:
        query = query.filter(ModerationCase.user_id == user_id)

    # Filter by moderator
    mod_id = request.query_params.get("mod_id")
    if mod_id:
        query = query.filter(ModerationCase.mod_id == mod_id)

    # Filter by action type
    action = request.query_params.get("action")
    if action:
        query = query.filter(ModerationCase.action == action)

    # Filter by status (resolved or not)
    resolved = request.query_params.get("resolved")
    if resolved is not None:
        if resolved.lower() == "true":
            query = query.filter(ModerationCase.resolved_at != None)
        elif resolved.lower() == "false":
            query = query.filter(ModerationCase.resolved_at == None)

    # Order by most recent
    cases = query.order_by(desc(ModerationCase.created_at)).all()

    return JSONResponse({
        "cases": [model_to_dict(c) for c in cases]
    })

@api_error_handler
async def moderation_case_detail(request):
    """
    Get moderation case details
    """
    case_id = request.path_params["case_id"]
    db = next(get_db())

    case = db.query(ModerationCase).filter(ModerationCase.id == case_id).first()
    if not case:
        return JSONResponse({"error": "Moderation case not found"}, status_code=404)

    return JSONResponse(model_to_dict(case))

@api_error_handler
async def moderation_case_create(request):
    """
    Create a new moderation case
    """
    data = await request.json()
    db = next(get_db())

    # Validate required fields
    required_fields = ["guild_id", "user_id", "mod_id", "action"]
    for field in required_fields:
        if field not in data:
            return JSONResponse({"error": f"Missing required field: {field}"}, status_code=400)

    # Get the next case number for this guild
    last_case = db.query(ModerationCase).filter(
        ModerationCase.guild_id == data["guild_id"]
    ).order_by(desc(ModerationCase.case_number)).first()

    case_number = 1
    if last_case:
        case_number = last_case.case_number + 1

    # Create new case
    case = ModerationCase(
        case_number=case_number,
        guild_id=data["guild_id"],
        user_id=data["user_id"],
        mod_id=data["mod_id"],
        action=data["action"],
        reason=data.get("reason"),
        duration_sec=data.get("duration_sec")
    )

    db.add(case)
    db.commit()
    db.refresh(case)

    return JSONResponse(model_to_dict(case), status_code=201)

@api_error_handler
async def moderation_case_update(request):
    """
    Update a moderation case (e.g., to resolve it)
    """
    case_id = request.path_params["case_id"]
    data = await request.json()
    db = next(get_db())

    case = db.query(ModerationCase).filter(ModerationCase.id == case_id).first()
    if not case:
        return JSONResponse({"error": "Moderation case not found"}, status_code=404)

    # Update fields
    if "resolved_at" in data:
        case.resolved_at = data["resolved_at"]
    elif "resolve" in data and data["resolve"]:
        case.resolved_at = datetime.now()

    if "resolution_note" in data:
        case.resolution_note = data["resolution_note"]

    db.commit()
    db.refresh(case)

    return JSONResponse(model_to_dict(case))


# API Key management endpoints (admin only)
@api_error_handler
async def api_key_list(request):
    """
    List all API keys (admin only)
    """
    db = next(get_db())
    keys = db.query(APIKey).all()

    # Don't expose the actual key in the list
    return JSONResponse({
        "api_keys": [model_to_dict(k, exclude_fields=["key"]) for k in keys]
    })

@api_error_handler
async def api_key_create(request):
    """
    Create a new API key (admin only)
    """
    data = await request.json()
    db = next(get_db())

    # Validate required fields
    if "name" not in data:
        return JSONResponse({"error": "Missing required field: name"}, status_code=400)

    # Generate a new API key
    key_value = generate_api_key()

    # Create new API key
    api_key = APIKey(
        key=key_value,
        name=data["name"],
        is_active=data.get("is_active", True)
    )

    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    # Return the key value only once
    result = model_to_dict(api_key)
    result["key"] = key_value  # Include the actual key in the response

    return JSONResponse(result, status_code=201)

@api_error_handler
async def api_key_delete(request):
    """
    Delete an API key (admin only)
    """
    key_id = request.path_params["key_id"]
    db = next(get_db())

    key = db.query(APIKey).filter(APIKey.id == key_id).first()
    if not key:
        return JSONResponse({"error": "API key not found"}, status_code=404)

    db.delete(key)
    db.commit()

    return JSONResponse({"success": True})