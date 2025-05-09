import os
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from sqlalchemy import func, desc
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import JSONResponse

from .models import (
    APIKey, Guild, DiscordUser, GuildMember, UserNote,
    UserWarning, ModerationCase, PersistentRole, TemporaryRole,
    ChannelLock, BumpStat, CommandUsage, Bytes, BytesConfig, BytesRole, BytesCooldown,
    AutoModRegexRule, AutoModRateLimit
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

            # Check for local development mode with TESTING key
            if os.environ.get("SMARTER_DEV_LOCAL") == "1" and api_key == "TESTING":
                # Generate a token with a dummy key ID and name for testing
                token = create_jwt_token(999, "Testing API Key")
                return JSONResponse({
                    "token": token,
                    "expires_in": 3600  # 1 hour
                })

            # Verify API key in database
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
    # Use the parse_datetime helper for joined_at
    joined_at = parse_datetime(data.get("joined_at")) or datetime.now()

    guild = Guild(
        discord_id=data["discord_id"],
        name=data["name"],
        icon_url=data.get("icon_url"),
        joined_at=joined_at
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
    List all users, with optional filtering by discord_id
    """
    db = next(get_db())

    # Initialize query
    query = db.query(DiscordUser)

    # Filter by discord_id if provided
    discord_id = request.query_params.get("discord_id")
    if discord_id:
        try:
            discord_id = int(discord_id)
            query = query.filter(DiscordUser.discord_id == discord_id)
            print(f"Filtering users by discord_id: {discord_id}")
        except ValueError:
            # If discord_id is not a valid integer, ignore the filter
            print(f"Invalid discord_id parameter: {discord_id}")

    # Execute query
    users = query.all()

    user_data = {
        "users": [model_to_dict(user) for user in users]
    }
    print(f"\n\nUser data: {user_data}\n\n")
    return JSONResponse(user_data)

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
        avatar_url=data.get("avatar_url"),
        # Handle any datetime fields
        created_at=parse_datetime(data.get("created_at"))
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return JSONResponse(model_to_dict(user), status_code=201)

@api_error_handler
async def users_batch_create(request):
    """
    Create multiple users in a single request
    """
    data = await request.json()
    db = next(get_db())

    if not isinstance(data, list):
        return JSONResponse({"error": "Expected a list of users"}, status_code=400)

    if len(data) > 500:  # Limit batch size to prevent abuse
        return JSONResponse({"error": "Batch size exceeds maximum of 500 users"}, status_code=400)

    created_users = []
    updated_users = []

    for user_data in data:
        # Validate required fields
        if "discord_id" not in user_data or "username" not in user_data:
            return JSONResponse({"error": "Missing required fields: discord_id, username"}, status_code=400)

        # Check if user already exists
        existing = db.query(DiscordUser).filter(DiscordUser.discord_id == user_data["discord_id"]).first()

        if existing:
            # Update fields if needed
            updated = False

            if "username" in user_data and existing.username != user_data["username"]:
                existing.username = user_data["username"]
                updated = True

            if "discriminator" in user_data and existing.discriminator != user_data.get("discriminator"):
                existing.discriminator = user_data.get("discriminator")
                updated = True

            if "avatar_url" in user_data and existing.avatar_url != user_data.get("avatar_url"):
                existing.avatar_url = user_data.get("avatar_url")
                updated = True

            if updated:
                updated_users.append(existing)

            created_users.append(existing)
        else:
            # Create new user
            user = DiscordUser(
                discord_id=user_data["discord_id"],
                username=user_data["username"],
                discriminator=user_data.get("discriminator"),
                avatar_url=user_data.get("avatar_url"),
                # Handle any datetime fields
                created_at=parse_datetime(user_data.get("created_at"))
            )

            db.add(user)
            created_users.append(user)

    # Commit all changes at once for better performance
    db.commit()

    # Refresh all created users to get their IDs
    for user in created_users:
        if not hasattr(user, 'id') or user.id is None:
            db.refresh(user)

    return JSONResponse({
        "users": [model_to_dict(user) for user in created_users],
        "created": len(created_users) - len(updated_users),
        "updated": len(updated_users),
        "total": len(created_users)
    }, status_code=201)

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
    if "last_active_day" in data:
        user.last_active_day = data["last_active_day"]
    if "streak_count" in data:
        user.streak_count = data["streak_count"]
    if "last_daily_bytes" in data:
        user.last_daily_bytes = parse_datetime(data["last_daily_bytes"])

    db.commit()
    db.refresh(user)

    return JSONResponse(model_to_dict(user))


@api_error_handler
async def guild_member_get(request):
    """
    Get a guild member
    """
    user_id = request.path_params["user_id"]
    guild_id = request.path_params["guild_id"]
    db = next(get_db())

    # Get the user by ID
    user = db.query(DiscordUser).filter(DiscordUser.id == user_id).first()
    if not user:
        # Try by Discord ID
        user = db.query(DiscordUser).filter(DiscordUser.discord_id == user_id).first()
        if not user:
            return JSONResponse({"error": "User not found"}, status_code=404)

    # Get the guild by ID
    guild = db.query(Guild).filter(Guild.id == guild_id).first()
    if not guild:
        # Try by Discord ID
        guild = db.query(Guild).filter(Guild.discord_id == guild_id).first()
        if not guild:
            return JSONResponse({"error": "Guild not found"}, status_code=404)

    # Get the guild member
    guild_member = db.query(GuildMember).filter(
        GuildMember.user_id == user.id,
        GuildMember.guild_id == guild.id
    ).first()

    if not guild_member:
        return JSONResponse({"error": "Guild member not found"}, status_code=404)

    return JSONResponse(model_to_dict(guild_member))


@api_error_handler
async def guild_member_create(request):
    """
    Create a new guild member
    """
    try:
        user_id = request.path_params["user_id"]
        data = await request.json()
        db = next(get_db())

        # Get the user by ID
        user = db.query(DiscordUser).filter(DiscordUser.id == user_id).first()
        if not user:
            # Try by Discord ID
            user = db.query(DiscordUser).filter(DiscordUser.discord_id == user_id).first()
            if not user:
                return JSONResponse({"error": "User not found"}, status_code=404)

        # Get the guild by ID
        guild_id = data.get("guild_id")
        if not guild_id:
            return JSONResponse({"error": "Missing required field: guild_id"}, status_code=400)

        guild = db.query(Guild).filter(Guild.id == guild_id).first()
        if not guild:
            # Try by Discord ID
            guild = db.query(Guild).filter(Guild.discord_id == guild_id).first()
            if not guild:
                # Create the guild if it doesn't exist
                guild = Guild(
                    discord_id=guild_id,
                    name=f"Guild {guild_id}",  # Default name
                    joined_at=datetime.now()
                )
                db.add(guild)
                db.commit()
                db.refresh(guild)

        # Check if guild member already exists
        existing = db.query(GuildMember).filter(
            GuildMember.user_id == user.id,
            GuildMember.guild_id == guild.id
        ).first()

        if existing:
            return JSONResponse(model_to_dict(existing))

        # Create new guild member
        guild_member = GuildMember(
            user_id=user.id,
            guild_id=guild.id,
            nickname=data.get("nickname"),
            joined_at=parse_datetime(data.get("joined_at")),
            is_active=data.get("is_active", True),
            last_active_day=data.get("last_active_day"),
            streak_count=data.get("streak_count", 0),
            last_daily_bytes=parse_datetime(data.get("last_daily_bytes"))
        )

        db.add(guild_member)
        db.commit()
        db.refresh(guild_member)

        return JSONResponse(model_to_dict(guild_member), status_code=201)
    except Exception as e:
        print(f"Error creating guild member: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": f"Internal server error: {str(e)}"}, status_code=500)


@api_error_handler
async def guild_member_update(request):
    """
    Update a guild member
    """
    user_id = request.path_params["user_id"]
    guild_id = request.path_params["guild_id"]
    data = await request.json()
    db = next(get_db())

    # Get the user by ID
    user = db.query(DiscordUser).filter(DiscordUser.id == user_id).first()
    if not user:
        # Try by Discord ID
        user = db.query(DiscordUser).filter(DiscordUser.discord_id == user_id).first()
        if not user:
            return JSONResponse({"error": "User not found"}, status_code=404)

    # Get the guild by ID
    guild = db.query(Guild).filter(Guild.id == guild_id).first()
    if not guild:
        # Try by Discord ID
        guild = db.query(Guild).filter(Guild.discord_id == guild_id).first()
        if not guild:
            return JSONResponse({"error": "Guild not found"}, status_code=404)

    # Get the guild member
    guild_member = db.query(GuildMember).filter(
        GuildMember.user_id == user.id,
        GuildMember.guild_id == guild.id
    ).first()

    if not guild_member:
        return JSONResponse({"error": "Guild member not found"}, status_code=404)

    # Update fields
    if "nickname" in data:
        guild_member.nickname = data["nickname"]
    if "joined_at" in data:
        guild_member.joined_at = parse_datetime(data["joined_at"])
    if "is_active" in data:
        guild_member.is_active = data["is_active"]
    if "last_active_day" in data:
        guild_member.last_active_day = data["last_active_day"]
    if "streak_count" in data:
        guild_member.streak_count = data["streak_count"]
    if "last_daily_bytes" in data:
        guild_member.last_daily_bytes = parse_datetime(data["last_daily_bytes"])

    db.commit()
    db.refresh(guild_member)

    return JSONResponse(model_to_dict(guild_member))


# Helper function to parse datetime strings
def parse_datetime(dt_str):
    """
    Parse a datetime string to a datetime object
    """
    if not dt_str:
        return None
    if isinstance(dt_str, str):
        try:
            return datetime.fromisoformat(dt_str)
        except ValueError:
            return datetime.now()
    return dt_str


# Helper function to convert model to dictionary
def model_to_dict(model):
    """
    Convert a SQLAlchemy model to a dictionary
    """
    result = {}
    for column in model.__table__.columns:
        value = getattr(model, column.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        result[column.name] = value
    return result





# Bytes API endpoints
@api_error_handler
async def bytes_list(request):
    """
    List all bytes, with optional filtering
    """
    db = next(get_db())
    query = db.query(Bytes)

    # Filter by guild
    guild_id = request.query_params.get("guild_id")
    if guild_id:
        query = query.filter(Bytes.guild_id == guild_id)

    # Filter by user (giver or receiver)
    user_id = request.query_params.get("user_id")
    if user_id:
        query = query.filter(
            (Bytes.giver_id == user_id) | (Bytes.receiver_id == user_id)
        )

    # Filter by receiver only
    receiver_id = request.query_params.get("receiver_id")
    if receiver_id:
        query = query.filter(Bytes.receiver_id == receiver_id)

    # Filter by giver only
    giver_id = request.query_params.get("giver_id")
    if giver_id:
        query = query.filter(Bytes.giver_id == giver_id)

    # Order by most recent
    bytes_list = query.order_by(desc(Bytes.awarded_at)).all()

    return JSONResponse({
        "bytes": [model_to_dict(b) for b in bytes_list]
    })

@api_error_handler
async def bytes_recent(request):
    """
    Get recent bytes transactions with advanced filtering

    Query parameters:
    - receiver_id: Filter by receiver Discord ID
    - guild_id: Filter by guild ID
    - since: Filter by transactions after this timestamp (ISO format)
    - reason: Filter by reason (partial match)
    """
    db = next(get_db())
    query = db.query(Bytes)

    # Filter by receiver Discord ID
    receiver_discord_id = request.query_params.get("receiver_id")
    if receiver_discord_id:
        # First get the internal user ID from the Discord ID
        receiver = db.query(DiscordUser).filter(DiscordUser.discord_id == receiver_discord_id).first()
        if receiver:
            query = query.filter(Bytes.receiver_id == receiver.id)
        else:
            # If user not found, return empty result
            return JSONResponse({"transactions": []})

    # Filter by guild ID
    guild_id = request.query_params.get("guild_id")
    if guild_id:
        query = query.filter(Bytes.guild_id == guild_id)

    # Filter by timestamp
    since = request.query_params.get("since")
    if since:
        try:
            since_dt = parse_datetime(since)
            query = query.filter(Bytes.awarded_at >= since_dt)
        except (ValueError, TypeError):
            # If invalid timestamp, ignore this filter
            pass

    # Filter by reason (partial match)
    reason = request.query_params.get("reason")
    if reason:
        query = query.filter(Bytes.reason.like(f"%{reason}%"))

    # Order by most recent and limit to 100 results
    transactions = query.order_by(desc(Bytes.awarded_at)).limit(100).all()

    return JSONResponse({
        "transactions": [model_to_dict(t) for t in transactions]
    })

@api_error_handler
async def bytes_detail(request):
    """
    Get bytes details
    """
    bytes_id = request.path_params["bytes_id"]
    db = next(get_db())

    bytes_obj = db.query(Bytes).filter(Bytes.id == bytes_id).first()
    if not bytes_obj:
        return JSONResponse({"error": "Bytes not found"}, status_code=404)

    return JSONResponse(model_to_dict(bytes_obj))

@api_error_handler
async def bytes_create(request):
    """
    Create a new bytes award
    """
    data = await request.json()
    db = next(get_db())

    # Validate required fields
    required_fields = ["giver_id", "receiver_id", "guild_id"]
    for field in required_fields:
        if field not in data:
            return JSONResponse({"error": f"Missing required field: {field}"}, status_code=400)

    # Get the users by Discord ID
    giver = db.query(DiscordUser).filter(DiscordUser.discord_id == data["giver_id"]).first()
    receiver = db.query(DiscordUser).filter(DiscordUser.discord_id == data["receiver_id"]).first()

    if not giver or not receiver:
        return JSONResponse({"error": "Giver or receiver not found"}, status_code=404)

    # Convert Discord IDs to internal IDs for database operations
    internal_giver_id = giver.id
    internal_receiver_id = receiver.id

    # Check if giver has enough bytes
    amount = data.get("amount", 1)
    if giver.bytes_balance < amount:
        return JSONResponse({"error": "Insufficient bytes balance"}, status_code=400)

    # Check cooldown
    cooldown = db.query(BytesCooldown).filter(
        BytesCooldown.user_id == internal_giver_id,
        BytesCooldown.guild_id == data["guild_id"]
    ).first()

    # Get guild config
    config = db.query(BytesConfig).filter(BytesConfig.guild_id == data["guild_id"]).first()
    if not config:
        # Create default config
        config = BytesConfig(guild_id=data["guild_id"])
        db.add(config)
        db.commit()
        db.refresh(config)

    # Check if cooldown has passed
    if cooldown and False:
        cooldown_minutes = config.cooldown_minutes
        cooldown_delta = datetime.now() - cooldown.last_given_at
        if cooldown_delta.total_seconds() < cooldown_minutes * 60:
            minutes_left = cooldown_minutes - (cooldown_delta.total_seconds() / 60)
            return JSONResponse({
                "error": f"Cooldown still active. Try again in {int(minutes_left)} minutes.",
                "minutes_left": int(minutes_left)
            }, status_code=400)

        # Update cooldown
        cooldown.last_given_at = datetime.now()
    else:
        # Create new cooldown
        cooldown = BytesCooldown(
            user_id=internal_giver_id,
            guild_id=data["guild_id"],
            last_given_at=datetime.now()
        )
        db.add(cooldown)

    # Create new bytes transaction
    bytes_obj = Bytes(
        giver_id=internal_giver_id,
        receiver_id=internal_receiver_id,
        guild_id=data["guild_id"],
        amount=amount,
        reason=data.get("reason"),
        awarded_at=parse_datetime(data.get("awarded_at")) or datetime.now()
    )

    # Update balances
    giver.bytes_balance -= amount
    receiver.bytes_balance += amount

    db.add(bytes_obj)
    db.commit()
    db.refresh(bytes_obj)

    # Get total bytes received by the user
    bytes_received = db.query(func.sum(Bytes.amount)).filter(Bytes.receiver_id == receiver.id).scalar() or 0

    # Check if receiver has earned any roles based on total bytes received
    roles = db.query(BytesRole).filter(
        BytesRole.guild_id == data["guild_id"],
        BytesRole.bytes_required <= bytes_received
    ).order_by(BytesRole.bytes_required.desc()).all()

    print(roles, bytes_received, data["guild_id"])

    earned_roles = []
    if roles:
        earned_roles = [model_to_dict(role) for role in roles]

    return JSONResponse({
        "bytes": model_to_dict(bytes_obj),
        "giver_balance": giver.bytes_balance,
        "receiver_balance": receiver.bytes_balance,
        "earned_roles": earned_roles
    }, status_code=201)

@api_error_handler
async def bytes_config_get(request):
    """
    Get bytes configuration for a guild
    """
    guild_id = request.path_params["guild_id"]
    db = next(get_db())

    config = db.query(BytesConfig).filter(BytesConfig.guild_id == guild_id).first()
    if not config:
        # Return default config
        config = BytesConfig(guild_id=int(guild_id))
        db.add(config)
        db.commit()
        db.refresh(config)

    return JSONResponse(model_to_dict(config))

@api_error_handler
async def bytes_config_create(request):
    """
    Create or update bytes configuration
    """
    data = await request.json()
    db = next(get_db())

    # Validate required fields
    if "guild_id" not in data:
        return JSONResponse({"error": "Missing required field: guild_id"}, status_code=400)

    # Check if config already exists
    config = db.query(BytesConfig).filter(BytesConfig.guild_id == data["guild_id"]).first()
    if config:
        # Update existing config
        if "starting_balance" in data:
            config.starting_balance = data["starting_balance"]
        if "daily_earning" in data:
            config.daily_earning = data["daily_earning"]
        if "max_give_amount" in data:
            config.max_give_amount = data["max_give_amount"]
        if "cooldown_minutes" in data:
            config.cooldown_minutes = data["cooldown_minutes"]
    else:
        # Create new config
        config = BytesConfig(
            guild_id=data["guild_id"],
            starting_balance=data.get("starting_balance", 100),
            daily_earning=data.get("daily_earning", 10),
            max_give_amount=data.get("max_give_amount", 50),
            cooldown_minutes=data.get("cooldown_minutes", 1440)
        )
        db.add(config)

    db.commit()
    db.refresh(config)

    return JSONResponse(model_to_dict(config), status_code=201)

@api_error_handler
async def bytes_config_update(request):
    """
    Update bytes configuration
    """
    guild_id = request.path_params["guild_id"]
    data = await request.json()
    db = next(get_db())

    config = db.query(BytesConfig).filter(BytesConfig.guild_id == guild_id).first()
    if not config:
        return JSONResponse({"error": "Config not found"}, status_code=404)

    # Update fields
    if "starting_balance" in data:
        config.starting_balance = data["starting_balance"]
    if "daily_earning" in data:
        config.daily_earning = data["daily_earning"]
    if "max_give_amount" in data:
        config.max_give_amount = data["max_give_amount"]
    if "cooldown_minutes" in data:
        config.cooldown_minutes = data["cooldown_minutes"]

    db.commit()
    db.refresh(config)

    return JSONResponse(model_to_dict(config))

@api_error_handler
async def bytes_roles_list(request):
    """
    List all bytes roles for a guild
    """
    guild_id = request.path_params["guild_id"]
    db = next(get_db())

    # Convert Discord guild ID to internal database guild ID if needed
    try:
        # Check if guild_id is a Discord guild ID (usually a large number)
        discord_guild_id = int(guild_id)
        guild = db.query(Guild).filter(Guild.discord_id == discord_guild_id).first()
        if guild:
            # Use the internal database guild ID
            internal_guild_id = guild.id
        else:
            # If no guild found with this Discord ID, assume it's already an internal ID
            internal_guild_id = discord_guild_id
    except ValueError:
        # If guild_id is not a valid integer, use it as is
        internal_guild_id = guild_id

    roles = db.query(BytesRole).filter(BytesRole.guild_id == internal_guild_id).order_by(BytesRole.bytes_required).all()

    return JSONResponse({
        "roles": [model_to_dict(role) for role in roles]
    })

@api_error_handler
async def bytes_role_create(request):
    """
    Create a new bytes role
    """
    data = await request.json()
    db = next(get_db())

    # Validate required fields
    required_fields = ["guild_id", "role_id", "role_name", "bytes_required"]
    for field in required_fields:
        if field not in data:
            return JSONResponse({"error": f"Missing required field: {field}"}, status_code=400)

    # Create new role
    role = BytesRole(
        guild_id=data["guild_id"],
        role_id=data["role_id"],
        role_name=data["role_name"],
        bytes_required=data["bytes_required"]
    )

    db.add(role)
    db.commit()
    db.refresh(role)

    return JSONResponse(model_to_dict(role), status_code=201)

@api_error_handler
async def bytes_role_update(request):
    """
    Update a bytes role
    """
    role_id = request.path_params["role_id"]
    data = await request.json()
    db = next(get_db())

    role = db.query(BytesRole).filter(BytesRole.id == role_id).first()
    if not role:
        return JSONResponse({"error": "Role not found"}, status_code=404)

    # Update fields
    if "role_name" in data:
        role.role_name = data["role_name"]
    if "bytes_required" in data:
        role.bytes_required = data["bytes_required"]

    db.commit()
    db.refresh(role)

    return JSONResponse(model_to_dict(role))

@api_error_handler
async def bytes_role_delete(request):
    """
    Delete a bytes role
    """
    role_id = request.path_params["role_id"]
    db = next(get_db())

    role = db.query(BytesRole).filter(BytesRole.id == role_id).first()
    if not role:
        return JSONResponse({"error": "Role not found"}, status_code=404)

    db.delete(role)
    db.commit()

    return JSONResponse({"success": True})

@api_error_handler
async def bytes_cooldown_get(request):
    """
    Get bytes cooldown for a user in a guild
    """
    user_id = request.path_params["user_id"]
    guild_id = request.path_params["guild_id"]
    db = next(get_db())

    # Try to parse user_id as integer (could be either internal ID or Discord ID)
    try:
        user_id_int = int(user_id)
        # First try to find user by Discord ID (preferred method)
        user = db.query(DiscordUser).filter(DiscordUser.discord_id == user_id_int).first()

        # If not found, fall back to internal ID for backward compatibility
        if not user:
            user = db.query(DiscordUser).filter(DiscordUser.id == user_id_int).first()
            if user:
                internal_user_id = user.id
            else:
                return JSONResponse({"error": "User not found"}, status_code=404)
        else:
            internal_user_id = user.id
    except ValueError:
        # If user_id is not a valid integer, return error
        return JSONResponse({"error": "Invalid user ID"}, status_code=400)

    # Try to convert guild_id to internal ID if it's a Discord guild ID
    try:
        guild_id_int = int(guild_id)
        guild = db.query(Guild).filter(Guild.discord_id == guild_id_int).first()
        if guild:
            internal_guild_id = guild.id
        else:
            # If no guild found with this Discord ID, assume it's already an internal ID
            internal_guild_id = guild_id_int
    except ValueError:
        # If guild_id is not a valid integer, return error
        return JSONResponse({"error": "Invalid guild ID"}, status_code=400)

    cooldown = db.query(BytesCooldown).filter(
        BytesCooldown.user_id == internal_user_id,
        BytesCooldown.guild_id == internal_guild_id
    ).first()

    if not cooldown:
        return JSONResponse({"error": "Cooldown not found"}, status_code=404)

    # Get guild config for cooldown minutes
    config = db.query(BytesConfig).filter(BytesConfig.guild_id == internal_guild_id).first()
    cooldown_minutes = config.cooldown_minutes if config else 1440

    # Calculate time left
    cooldown_delta = datetime.now() - cooldown.last_given_at
    minutes_passed = cooldown_delta.total_seconds() / 60
    minutes_left = max(0, cooldown_minutes - minutes_passed)

    cooldown_data = model_to_dict(cooldown)
    cooldown_data["minutes_left"] = int(minutes_left)
    cooldown_data["cooldown_active"] = minutes_left > 0

    return JSONResponse(cooldown_data)

@api_error_handler
async def user_bytes_balance(request):
    """
    Get a user's bytes balance
    """
    user_id = request.path_params["user_id"]
    db = next(get_db())

    # Try to parse user_id as integer (could be either internal ID or Discord ID)
    try:
        user_id_int = int(user_id)
        # First try to find user by Discord ID (preferred method)
        user = db.query(DiscordUser).filter(DiscordUser.discord_id == user_id_int).first()

        # If not found, fall back to internal ID for backward compatibility
        if not user:
            user = db.query(DiscordUser).filter(DiscordUser.id == user_id_int).first()
    except ValueError:
        # If user_id is not a valid integer, return error
        return JSONResponse({"error": "Invalid user ID"}, status_code=400)

    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)

    # Get bytes received (including from system admin)
    bytes_received = db.query(func.sum(Bytes.amount)).filter(Bytes.receiver_id == user.id).scalar() or 0

    # Get bytes given to other users
    bytes_given = db.query(func.sum(Bytes.amount)).filter(Bytes.giver_id == user.id).scalar() or 0

    # Special case for system admin user (discord_id=0)
    # This user has an artificial balance and doesn't follow normal accounting rules
    if user.discord_id == 0:
        # Don't update the balance for the system user
        pass
    else:
        # Calculate the expected balance based on transactions
        # This should match the bytes_balance field in the user model
        expected_balance = bytes_received - bytes_given

        # If there's a discrepancy between the stored balance and calculated balance,
        # update the stored balance to match the calculated balance
        if user.bytes_balance != expected_balance:
            print(f"Fixing bytes balance discrepancy for user {user.username} (ID: {user.id}): "
                  f"Stored: {user.bytes_balance}, Calculated: {expected_balance}")
            user.bytes_balance = expected_balance
            db.commit()
            db.refresh(user)  # Refresh the user object to ensure it has the updated balance

    # Get guild roles if guild_id is provided
    guild_id = request.query_params.get("guild_id")
    earned_roles = []

    if guild_id:
        # Convert Discord guild ID to internal database guild ID
        guild = db.query(Guild).filter(Guild.discord_id == guild_id).first()
        if guild:
            # Use bytes_received instead of bytes_balance for role eligibility
            roles = db.query(BytesRole).filter(
                BytesRole.guild_id == guild.id,
                BytesRole.bytes_required <= bytes_received
            ).order_by(BytesRole.bytes_required.desc()).all()

            if roles:
                earned_roles = [model_to_dict(role) for role in roles]

    return JSONResponse({
        "user_id": user.id,
        "discord_id": user.discord_id,
        "username": user.username,
        "bytes_balance": user.bytes_balance,
        "bytes_received": bytes_received,
        "bytes_given": bytes_given,
        "earned_roles": earned_roles
    })

@api_error_handler
async def bytes_leaderboard(request):
    """
    Get bytes leaderboard for a guild, only considering bytes from the past 180 days
    """
    guild_id = request.path_params["guild_id"]
    db = next(get_db())

    # Get limit from query params, default to 10
    limit = request.query_params.get("limit", "10")
    try:
        limit = int(limit)
    except ValueError:
        limit = 10

    # Calculate the date 180 days ago
    cutoff_date = datetime.now() - timedelta(days=180)

    # Get all users in this guild
    users_with_bytes = db.query(DiscordUser).join(
        Bytes,
        ((Bytes.receiver_id == DiscordUser.id) | (Bytes.giver_id == DiscordUser.id))
    ).filter(
        Bytes.guild_id == guild_id
    ).distinct().all()

    # Format the response
    leaderboard = []
    for user in users_with_bytes:
        # Calculate bytes received in the past 180 days
        bytes_received = db.query(func.sum(Bytes.amount)).filter(
            Bytes.receiver_id == user.id,
            Bytes.guild_id == guild_id,
            Bytes.awarded_at >= cutoff_date
        ).scalar() or 0

        # Calculate bytes given in the past 180 days
        bytes_given = db.query(func.sum(Bytes.amount)).filter(
            Bytes.giver_id == user.id,
            Bytes.guild_id == guild_id,
            Bytes.awarded_at >= cutoff_date
        ).scalar() or 0

        # Calculate the balance for the past 180 days
        bytes_balance = bytes_received - bytes_given

        # Include all users with a positive bytes balance in the past 180 days
        if bytes_balance > 0:
            leaderboard.append({
                "user_id": user.id,
                "discord_id": user.discord_id,
                "username": user.username,
                "bytes_balance": bytes_balance,
                "avatar_url": user.avatar_url
            })

    # Sort by bytes balance
    leaderboard = sorted(leaderboard, key=lambda x: x["bytes_balance"], reverse=True)[:limit]

    return JSONResponse({
        "guild_id": int(guild_id),
        "leaderboard": leaderboard
    })


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
        reason=data.get("reason"),
        # Handle datetime fields
        warned_at=parse_datetime(data.get("warned_at"))
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
        duration_sec=data.get("duration_sec"),
        # Handle datetime fields
        created_at=parse_datetime(data.get("created_at")),
        resolved_at=parse_datetime(data.get("resolved_at")),
        resolution_note=data.get("resolution_note")
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
        case.resolved_at = parse_datetime(data["resolved_at"])
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


# Auto Moderation Regex Rules API endpoints
@api_error_handler
async def automod_regex_rules_list(request):
    """
    List all auto moderation regex rules, with optional filtering
    """
    print("Received request for automod regex rules")
    db = next(get_db())
    query = db.query(AutoModRegexRule)

    # Filter by guild
    guild_id = request.query_params.get("guild_id")
    if guild_id:
        try:
            guild_id_int = int(guild_id)
            print(f"Filtering by guild_id: {guild_id_int}")
            query = query.filter(AutoModRegexRule.guild_id == guild_id_int)

            # Debug: Print all rules in the database
            all_rules = db.query(AutoModRegexRule).all()
            print(f"All rules in database: {len(all_rules)}")
            for rule in all_rules:
                print(f"Rule {rule.id}: guild_id={rule.guild_id}, pattern={rule.pattern}, is_active={rule.is_active}")
        except ValueError:
            print(f"Invalid guild_id parameter: {guild_id}")

    # Filter by active status
    is_active = request.query_params.get("is_active")
    if is_active is not None:
        print(f"Filtering by is_active: {is_active}")
        if is_active.lower() == "true":
            query = query.filter(AutoModRegexRule.is_active == True)
        elif is_active.lower() == "false":
            query = query.filter(AutoModRegexRule.is_active == False)

    # Order by guild and creation date
    rules = query.order_by(AutoModRegexRule.guild_id, AutoModRegexRule.created_at).all()
    print(f"Found {len(rules)} rules")
    for rule in rules:
        print(f"Rule: {rule.id}, guild_id: {rule.guild_id}, pattern: {rule.pattern}, is_active: {rule.is_active}")

    response_data = {
        "rules": [model_to_dict(r) for r in rules]
    }
    print(f"Returning response: {response_data}")
    return JSONResponse(response_data)

@api_error_handler
async def automod_regex_rule_detail(request):
    """
    Get auto moderation regex rule details
    """
    rule_id = request.path_params["rule_id"]
    db = next(get_db())

    rule = db.query(AutoModRegexRule).filter(AutoModRegexRule.id == rule_id).first()
    if not rule:
        return JSONResponse({"error": "Auto moderation regex rule not found"}, status_code=404)

    return JSONResponse(model_to_dict(rule))


# Auto Moderation Rate Limits API endpoints
@api_error_handler
async def automod_rate_limits_list(request):
    """
    List all auto moderation rate limits, with optional filtering
    """
    db = next(get_db())
    query = db.query(AutoModRateLimit)

    # Filter by guild
    guild_id = request.query_params.get("guild_id")
    if guild_id:
        query = query.filter(AutoModRateLimit.guild_id == guild_id)

    # Filter by active status
    is_active = request.query_params.get("is_active")
    if is_active is not None:
        if is_active.lower() == "true":
            query = query.filter(AutoModRateLimit.is_active == True)
        elif is_active.lower() == "false":
            query = query.filter(AutoModRateLimit.is_active == False)

    # Filter by limit type
    limit_type = request.query_params.get("limit_type")
    if limit_type:
        query = query.filter(AutoModRateLimit.limit_type == limit_type)

    # Order by guild and creation date
    limits = query.order_by(AutoModRateLimit.guild_id, AutoModRateLimit.created_at).all()

    return JSONResponse({
        "limits": [model_to_dict(l) for l in limits]
    })

@api_error_handler
async def automod_rate_limit_detail(request):
    """
    Get auto moderation rate limit details
    """
    limit_id = request.path_params["limit_id"]
    db = next(get_db())

    limit = db.query(AutoModRateLimit).filter(AutoModRateLimit.id == limit_id).first()
    if not limit:
        return JSONResponse({"error": "Auto moderation rate limit not found"}, status_code=404)

    return JSONResponse(model_to_dict(limit))
