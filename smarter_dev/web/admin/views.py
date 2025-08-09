"""Admin interface view handlers."""

from __future__ import annotations

import logging
from typing import Dict, Any, List
from uuid import UUID

from starlette.requests import Request
from starlette.responses import Response, RedirectResponse
from starlette.templating import Jinja2Templates
from sqlalchemy import select, func, distinct
from sqlalchemy.exc import IntegrityError

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.models import (
    BytesBalance,
    BytesTransaction, 
    BytesConfig,
    Squad,
    SquadMembership,
    APIKey,
    HelpConversation,
    BlogPost,
    ForumAgent,
    ForumAgentResponse
)
from smarter_dev.web.crud import BytesOperations, BytesConfigOperations, SquadOperations, APIKeyOperations, ForumAgentOperations
from smarter_dev.web.security import generate_secure_api_key
from smarter_dev.web.admin.discord import (
    get_bot_guilds,
    get_guild_info,
    get_guild_roles,
    GuildNotFoundError,
    DiscordAPIError
)

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")


async def dashboard(request: Request) -> Response:
    """Admin dashboard with overview of all guilds and statistics."""
    try:
        # Get bot guilds from Discord
        guilds = await get_bot_guilds()
        
        # Get overall statistics from database
        async with get_db_session_context() as session:
            # Total unique users across all guilds
            total_users_result = await session.execute(
                select(func.count(distinct(BytesBalance.user_id)))
            )
            total_users = total_users_result.scalar() or 0
            
            # Total transactions
            total_transactions_result = await session.execute(
                select(func.count(BytesTransaction.id))
            )
            total_transactions = total_transactions_result.scalar() or 0
            
            # Total squads
            total_squads_result = await session.execute(
                select(func.count(Squad.id))
            )
            total_squads = total_squads_result.scalar() or 0
            
            # Total bytes in circulation
            total_bytes_result = await session.execute(
                select(func.coalesce(func.sum(BytesBalance.balance), 0))
            )
            total_bytes = total_bytes_result.scalar() or 0
            
            # Help conversation statistics
            total_conversations_result = await session.execute(
                select(func.count(HelpConversation.id))
            )
            total_conversations = total_conversations_result.scalar() or 0
            
            # Total tokens used by help agent
            help_tokens_result = await session.execute(
                select(func.coalesce(func.sum(HelpConversation.tokens_used), 0))
            )
            help_tokens = help_tokens_result.scalar() or 0
            
            # Total tokens used by forum agents
            forum_tokens_result = await session.execute(
                select(func.coalesce(func.sum(ForumAgentResponse.tokens_used), 0))
            )
            forum_tokens = forum_tokens_result.scalar() or 0
            
            # Combined total tokens
            total_tokens = help_tokens + forum_tokens
            
            # Conversations today
            from datetime import datetime, timezone
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            conversations_today_result = await session.execute(
                select(func.count(HelpConversation.id))
                .where(HelpConversation.started_at >= today_start)
            )
            conversations_today = conversations_today_result.scalar() or 0
            
            # Average response time
            avg_response_time_result = await session.execute(
                select(func.avg(HelpConversation.response_time_ms))
                .where(HelpConversation.response_time_ms.is_not(None))
            )
            avg_response_time = avg_response_time_result.scalar()
            avg_response_time_ms = int(avg_response_time) if avg_response_time else None
        
        # Add basic stats to each guild
        guild_stats = []
        async with get_db_session_context() as session:
            for guild in guilds:
                # Get guild-specific stats
                guild_users_result = await session.execute(
                    select(func.count(BytesBalance.user_id))
                    .where(BytesBalance.guild_id == guild.id)
                )
                guild_users = guild_users_result.scalar() or 0
                
                guild_squads_result = await session.execute(
                    select(func.count(Squad.id))
                    .where(Squad.guild_id == guild.id)
                )
                guild_squads = guild_squads_result.scalar() or 0
                
                guild_stats.append({
                    "guild": guild,
                    "user_count": guild_users,
                    "squad_count": guild_squads
                })
        
        return templates.TemplateResponse(
            request,
            "admin/dashboard.html",
            {
                "guilds": guild_stats,
                "total_users": total_users,
                "total_transactions": total_transactions,
                "total_squads": total_squads,
                "total_bytes": total_bytes,
                "total_conversations": total_conversations,
                "total_tokens": total_tokens,
                "help_tokens": help_tokens,
                "forum_tokens": forum_tokens,
                "conversations_today": conversations_today,
                "avg_response_time_ms": avg_response_time_ms
            }
        )
    
    except DiscordAPIError as e:
        logger.error(f"Discord API error in dashboard: {e}")
        return templates.TemplateResponse(
            request,
            "admin/dashboard.html",
            {
                "guilds": [],
                "error": f"Discord API error: {e}",
                "total_users": 0,
                "total_transactions": 0,
                "total_squads": 0,
                "total_bytes": 0,
                "total_conversations": 0,
                "total_tokens": 0,
                "help_tokens": 0,
                "forum_tokens": 0,
                "conversations_today": 0,
                "avg_response_time_ms": None
            }
        )
    except Exception as e:
        logger.error(f"Unexpected error in dashboard: {e}")
        return templates.TemplateResponse(
            request,
            "admin/dashboard.html",
            {
                "guilds": [],
                "error": "An unexpected error occurred while loading the dashboard.",
                "total_users": 0,
                "total_transactions": 0,
                "total_squads": 0,
                "total_bytes": 0,
                "total_conversations": 0,
                "total_tokens": 0,
                "help_tokens": 0,
                "forum_tokens": 0,
                "conversations_today": 0,
                "avg_response_time_ms": None
            }
        )


async def guild_list(request: Request) -> Response:
    """List all guilds with basic information."""
    try:
        guilds = await get_bot_guilds()
        
        return templates.TemplateResponse(
            request,
            "admin/guild_list.html",
            {
                "guilds": guilds
            }
        )
    
    except DiscordAPIError as e:
        logger.error(f"Discord API error in guild list: {e}")
        return templates.TemplateResponse(
            request,
            "admin/guild_list.html",
            {
                "guilds": [],
                "error": f"Discord API error: {e}"
            }
        )


async def guild_detail(request: Request) -> Response:
    """Detailed view of a specific guild with analytics."""
    guild_id = request.path_params["guild_id"]
    
    try:
        # Fetch guild info from Discord
        guild = await get_guild_info(guild_id)
        
        # Get guild statistics from database
        async with get_db_session_context() as session:
            bytes_ops = BytesOperations()
            config_ops = BytesConfigOperations()
            squad_ops = SquadOperations()
            
            # Get top users by balance
            try:
                top_users = await bytes_ops.get_leaderboard(session, guild_id, limit=10)
            except Exception as e:
                logger.warning(f"Failed to get leaderboard: {e}")
                top_users = []
            
            # Get recent transactions
            recent_transactions_result = await session.execute(
                select(BytesTransaction)
                .where(BytesTransaction.guild_id == guild_id)
                .order_by(BytesTransaction.created_at.desc())
                .limit(20)
            )
            recent_transactions = recent_transactions_result.scalars().all()
            
            # Get guild configuration
            try:
                config = await config_ops.get_config(session, guild_id)
            except Exception:
                config = BytesConfig.get_defaults(guild_id)
            
            # Get squads
            try:
                squads = await squad_ops.get_guild_squads(session, guild_id)
            except Exception as e:
                logger.warning(f"Failed to get guild squads: {e}")
                squads = []
            
            # Get overall guild stats
            guild_stats_result = await session.execute(
                select(
                    func.count(distinct(BytesBalance.user_id)).label("total_users"),
                    func.coalesce(func.sum(BytesBalance.balance), 0).label("total_balance"),
                    func.count(BytesTransaction.id).label("total_transactions")
                )
                .select_from(BytesBalance)
                .outerjoin(BytesTransaction, BytesBalance.guild_id == BytesTransaction.guild_id)
                .where(BytesBalance.guild_id == guild_id)
            )
            stats = guild_stats_result.first()
        
        return templates.TemplateResponse(
            request,
            "admin/guild_detail.html",
            {
                "guild": guild,
                "top_users": top_users,
                "recent_transactions": recent_transactions,
                "config": config,
                "squads": squads,
                "stats": {
                    "total_users": stats.total_users if stats else 0,
                    "total_balance": stats.total_balance if stats else 0,
                    "total_transactions": stats.total_transactions if stats else 0,
                    "squad_count": len(squads)
                }
            }
        )
    
    except GuildNotFoundError:
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": f"Guild {guild_id} not found or bot is not a member.",
                "error_code": 404
            },
            status_code=404
        )
    except DiscordAPIError as e:
        logger.error(f"Discord API error in guild detail: {e}")
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": f"Discord API error: {e}",
                "error_code": 503
            },
            status_code=503
        )
    except Exception as e:
        logger.error(f"Unexpected error in guild detail: {e}")
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "An unexpected error occurred while loading guild details.",
                "error_code": 500
            },
            status_code=500
        )


async def bytes_config(request: Request) -> Response:
    """Bytes economy configuration for a guild."""
    guild_id = request.path_params["guild_id"]
    
    try:
        # Verify guild exists and get info
        guild = await get_guild_info(guild_id)
        
        async with get_db_session_context() as session:
            config_ops = BytesConfigOperations()
            
            if request.method == "GET":
                # Get current configuration
                try:
                    config = await config_ops.get_config(session, guild_id)
                except Exception:
                    # Create default config if none exists
                    try:
                        config = await config_ops.create_config(session, guild_id)
                        await session.commit()
                    except Exception:
                        # If creation fails, return defaults without saving
                        config = BytesConfig.get_defaults(guild_id)
                
                return templates.TemplateResponse(
                    request,
                    "admin/bytes_config.html",
                    {
                        "guild": guild,
                        "config": config
                    }
                )
            
            # POST - Update configuration
            form = await request.form()
            
            try:
                # Parse form data
                config_data = {
                    "starting_balance": int(form.get("starting_balance", 100)),
                    "daily_amount": int(form.get("daily_amount", 10)),
                    "max_transfer": int(form.get("max_transfer", 1000)),
                    "transfer_cooldown_hours": int(form.get("transfer_cooldown_hours", 0))
                }
                
                # Parse streak bonuses
                streak_bonuses = {}
                for key, value in form.items():
                    if key.startswith("streak_") and key.endswith("_bonus"):
                        days = key.replace("streak_", "").replace("_bonus", "")
                        if value and value.isdigit():
                            streak_bonuses[int(days)] = int(value)
                
                if streak_bonuses:
                    config_data["streak_bonuses"] = streak_bonuses
                
                # Parse role rewards
                role_rewards = {}
                for key, value in form.items():
                    if key.startswith("role_reward_"):
                        role_id = key.replace("role_reward_", "")
                        if value and value.isdigit():
                            role_rewards[role_id] = int(value)
                
                if role_rewards:
                    config_data["role_rewards"] = role_rewards
                
                # Update or create configuration
                try:
                    config = await config_ops.update_config(session, guild_id, **config_data)
                except Exception:
                    # Create new config if it doesn't exist
                    config = await config_ops.create_config(session, guild_id, **config_data)
                await session.commit()
                
                # Notify bot via Redis pub/sub
                try:
                    redis_client = await get_redis_client()
                    await redis_client.publish(
                        f"config_update:{guild_id}",
                        f'{{"type": "bytes", "guild_id": "{guild_id}"}}'
                    )
                    logger.info(f"Published bytes config update notification for guild {guild_id}")
                except Exception as e:
                    logger.warning(f"Failed to notify bot of config update: {e}")
                
                logger.info(f"Updated bytes config for guild {guild_id}")
                
                return templates.TemplateResponse(
                    request,
                    "admin/bytes_config.html",
                    {
                        "guild": guild,
                        "config": config,
                        "success": "Configuration updated successfully!"
                    }
                )
                
            except (ValueError, TypeError) as e:
                logger.warning(f"Invalid form data in bytes config: {e}")
                try:
                    config = await config_ops.get_config(session, guild_id)
                except Exception:
                    config = BytesConfig.get_defaults(guild_id)
                return templates.TemplateResponse(
                    request,
                    "admin/bytes_config.html",
                    {
                        "guild": guild,
                        "config": config,
                        "error": "Invalid configuration values. Please check your input."
                    },
                    status_code=400
                )
    
    except GuildNotFoundError:
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": f"Guild {guild_id} not found or bot is not a member.",
                "error_code": 404
            },
            status_code=404
        )
    except Exception as e:
        logger.error(f"Unexpected error in bytes config: {e}")
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "An unexpected error occurred while managing bytes configuration.",
                "error_code": 500
            },
            status_code=500
        )


async def squads_config(request: Request) -> Response:
    """Squad management configuration for a guild."""
    guild_id = request.path_params["guild_id"]
    
    try:
        # Verify guild exists and get info
        guild = await get_guild_info(guild_id)
        guild_roles = await get_guild_roles(guild_id)
        
        async with get_db_session_context() as session:
            squad_ops = SquadOperations()
            
            if request.method == "GET":
                # Get current squads
                try:
                    squads = await squad_ops.get_guild_squads(session, guild_id)
                except Exception as e:
                    logger.warning(f"Failed to get guild squads: {e}")
                    squads = []
                
                return templates.TemplateResponse(
                    request,
                    "admin/squads_config.html",
                    {
                        "guild": guild,
                        "guild_roles": guild_roles,
                        "squads": squads
                    }
                )
            
            # POST - Handle squad actions
            form = await request.form()
            action = form.get("action")
            success_message = None
            
            try:
                if action == "create":
                    await squad_ops.create_squad(
                        session,
                        guild_id=guild_id,
                        role_id=form.get("role_id"),
                        name=form.get("name"),
                        description=form.get("description") or None,
                        welcome_message=form.get("welcome_message") or None,
                        switch_cost=int(form.get("switch_cost", 50)),
                        max_members=int(form.get("max_members")) if form.get("max_members") else None
                    )
                    await session.commit()
                    success_message = "Squad created successfully!"
                    logger.info(f"Created squad '{form.get('name')}' in guild {guild_id}")
                
                elif action == "update":
                    squad_id = UUID(form.get("squad_id"))
                    updates = {
                        "name": form.get("name"),
                        "description": form.get("description") or None,
                        "welcome_message": form.get("welcome_message") or None,
                        "switch_cost": int(form.get("switch_cost")),
                        "max_members": int(form.get("max_members")) if form.get("max_members") else None,
                        "is_active": form.get("is_active") == "on"
                    }
                    
                    await squad_ops.update_squad(session, squad_id, updates)
                    await session.commit()
                    success_message = "Squad updated successfully!"
                    logger.info(f"Updated squad {squad_id} in guild {guild_id}")
                
                elif action == "delete":
                    squad_id = UUID(form.get("squad_id"))
                    await squad_ops.delete_squad(session, squad_id)
                    await session.commit()
                    success_message = "Squad deleted successfully!"
                    logger.info(f"Deleted squad {squad_id} in guild {guild_id}")
                
                # Refresh squads list
                squads = await squad_ops.get_guild_squads(session, guild_id)
                
                return templates.TemplateResponse(
                    request,
                    "admin/squads_config.html",
                    {
                        "guild": guild,
                        "guild_roles": guild_roles,
                        "squads": squads,
                        "success": success_message
                    }
                )
                
            except (ValueError, TypeError) as e:
                logger.warning(f"Invalid form data in squads config: {e}")
                squads = await squad_ops.get_guild_squads(session, guild_id)
                return templates.TemplateResponse(
                    request,
                    "admin/squads_config.html",
                    {
                        "guild": guild,
                        "guild_roles": guild_roles,
                        "squads": squads,
                        "error": "Invalid squad configuration. Please check your input."
                    },
                    status_code=400
                )
            except IntegrityError as e:
                logger.warning(f"Database integrity error in squads config: {e}")
                await session.rollback()
                squads = await squad_ops.get_guild_squads(session, guild_id)
                return templates.TemplateResponse(
                    request,
                    "admin/squads_config.html",
                    {
                        "guild": guild,
                        "guild_roles": guild_roles,
                        "squads": squads,
                        "error": "Squad configuration conflict. The role may already be assigned to another squad."
                    },
                    status_code=400
                )
    
    except GuildNotFoundError:
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": f"Guild {guild_id} not found or bot is not a member.",
                "error_code": 404
            },
            status_code=404
        )
    except Exception as e:
        logger.error(f"Unexpected error in squads config: {e}")
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "An unexpected error occurred while managing squad configuration.",
                "error_code": 500
            },
            status_code=500
        )


async def api_keys_list(request: Request) -> Response:
    """Display list of API keys."""
    try:
        async with get_db_session_context() as session:
            api_key_ops = APIKeyOperations()
            
            # Get all API keys
            keys, total = await api_key_ops.list_api_keys(
                db=session,
                offset=0,
                limit=100,
                active_only=False
            )
            
            return templates.TemplateResponse(
                request,
                "admin/api_keys.html",
                {
                    "api_keys": keys,
                    "total": total
                }
            )
    
    except Exception as e:
        logger.error(f"Error loading API keys: {e}")
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "Failed to load API keys.",
                "error_code": 500
            },
            status_code=500
        )


async def api_keys_create(request: Request) -> Response:
    """Create new API key."""
    if request.method == "GET":
        return templates.TemplateResponse(
            request,
            "admin/api_keys_create.html"
        )
    
    # POST - Create API key
    try:
        form = await request.form()
        name = form.get("name", "").strip()
        description = form.get("description", "").strip()
        scopes = form.getlist("scopes")
        rate_limit = int(form.get("rate_limit", "1000"))
        
        if not name:
            return templates.TemplateResponse(
                request,
                "admin/api_keys_create.html",
                {
                    "error": "API key name is required.",
                    "form_data": {
                        "name": name,
                        "description": description,
                        "scopes": scopes,
                        "rate_limit": rate_limit
                    }
                },
                status_code=400
            )
        
        if not scopes:
            scopes = ["bot:read", "bot:write"]  # Default scopes for bot
        
        # Generate secure API key
        full_key, key_hash, key_prefix = generate_secure_api_key()
        
        # Create API key record
        async with get_db_session_context() as session:
            api_key = APIKey(
                name=name,
                description=description,
                key_hash=key_hash,
                key_prefix=key_prefix,
                scopes=scopes,
                rate_limit_per_hour=rate_limit,
                created_by=request.session.get("username", "admin"),
                is_active=True,
                usage_count=0
            )
            
            session.add(api_key)
            await session.commit()
            await session.refresh(api_key)
        
        # Show the API key (only displayed once)
        return templates.TemplateResponse(
            request,
            "admin/api_keys_created.html",
            {
                "api_key": api_key,
                "full_key": full_key
            }
        )
    
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "admin/api_keys_create.html",
            {
                "error": f"Invalid input: {e}",
                "form_data": dict(form)
            },
            status_code=400
        )
    except Exception as e:
        logger.error(f"Error creating API key: {e}")
        return templates.TemplateResponse(
            request,
            "admin/api_keys_create.html",
            {
                "error": "Failed to create API key. Please try again.",
                "form_data": dict(form) if 'form' in locals() else {}
            },
            status_code=500
        )


async def api_keys_delete(request: Request) -> Response:
    """Delete/revoke an API key."""
    try:
        key_id = request.path_params["key_id"]
        
        async with get_db_session_context() as session:
            api_key_ops = APIKeyOperations()
            
            # Get the API key
            api_key = await api_key_ops.get_api_key_by_id(session, UUID(key_id))
            
            if not api_key:
                return templates.TemplateResponse(
                    request,
                    "admin/error.html",
                    {
                        "error": "API key not found.",
                        "error_code": 404
                    },
                    status_code=404
                )
            
            # Revoke the key
            from datetime import datetime, timezone
            api_key.is_active = False
            api_key.revoked_at = datetime.now(timezone.utc)
            api_key.updated_at = datetime.now(timezone.utc)
            
            await session.commit()
        
        # Redirect back to API keys list
        from starlette.responses import RedirectResponse
        return RedirectResponse(url="/admin/api-keys", status_code=303)
    
    except ValueError:
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "Invalid API key ID.",
                "error_code": 400
            },
            status_code=400
        )
    except Exception as e:
        logger.error(f"Error deleting API key: {e}")
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "Failed to delete API key.",
                "error_code": 500
            },
            status_code=500
        )


async def conversations_list(request: Request) -> Response:
    """List help conversations with filtering and pagination."""
    try:
        # Get query parameters
        page = int(request.query_params.get("page", 1))
        size = min(int(request.query_params.get("size", 20)), 100)
        guild_id = request.query_params.get("guild_id")
        user_id = request.query_params.get("user_id")
        interaction_type = request.query_params.get("interaction_type")
        search = request.query_params.get("search")
        resolved_only = request.query_params.get("resolved_only") == "true"
        
        async with get_db_session_context() as session:
            # Build query with filters
            query = select(HelpConversation)
            count_query = select(func.count(HelpConversation.id))
            
            # Apply filters
            if guild_id:
                query = query.where(HelpConversation.guild_id == guild_id)
                count_query = count_query.where(HelpConversation.guild_id == guild_id)
            
            if user_id:
                query = query.where(HelpConversation.user_id == user_id)
                count_query = count_query.where(HelpConversation.user_id == user_id)
                
            if interaction_type:
                query = query.where(HelpConversation.interaction_type == interaction_type)
                count_query = count_query.where(HelpConversation.interaction_type == interaction_type)
                
            if resolved_only:
                query = query.where(HelpConversation.is_resolved == True)
                count_query = count_query.where(HelpConversation.is_resolved == True)
                
            if search:
                from sqlalchemy import or_
                search_filter = or_(
                    HelpConversation.user_question.ilike(f"%{search}%"),
                    HelpConversation.bot_response.ilike(f"%{search}%"),
                    HelpConversation.user_username.ilike(f"%{search}%")
                )
                query = query.where(search_filter)
                count_query = count_query.where(search_filter)
            
            # Apply pagination and ordering
            offset = (page - 1) * size
            query = query.order_by(HelpConversation.started_at.desc()).offset(offset).limit(size)
            
            # Execute queries
            result = await session.execute(query)
            conversations = result.scalars().all()
            
            count_result = await session.execute(count_query)
            total = count_result.scalar()
            
            # Calculate pagination info
            total_pages = max(1, (total + size - 1) // size)
            
            # Get all guilds for the filter dropdown
            guilds = await get_bot_guilds()
            
            return templates.TemplateResponse(
                request,
                "admin/conversations.html",
                {
                    "conversations": conversations,
                    "total": total,
                    "page": page,
                    "size": size,
                    "total_pages": total_pages,
                    "guilds": guilds,
                    "filters": {
                        "guild_id": guild_id,
                        "user_id": user_id,
                        "interaction_type": interaction_type,
                        "search": search,
                        "resolved_only": resolved_only
                    }
                }
            )
            
    except Exception as e:
        logger.error(f"Error listing conversations: {e}")
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "Failed to load conversations.",
                "error_code": 500
            },
            status_code=500
        )


async def conversation_detail(request: Request) -> Response:
    """View details of a specific help conversation."""
    try:
        conversation_id = request.path_params["conversation_id"]
        
        async with get_db_session_context() as session:
            # Get conversation by ID
            query = select(HelpConversation).where(HelpConversation.id == conversation_id)
            result = await session.execute(query)
            conversation = result.scalar_one_or_none()
            
            if not conversation:
                return templates.TemplateResponse(
                    request,
                    "admin/error.html",
                    {
                        "error": "Conversation not found.",
                        "error_code": 404
                    },
                    status_code=404
                )
            
            # Get guild info for context
            try:
                guild_info = await get_guild_info(conversation.guild_id)
            except (GuildNotFoundError, DiscordAPIError):
                guild_info = {"name": f"Guild {conversation.guild_id}", "id": conversation.guild_id}
            
            return templates.TemplateResponse(
                request,
                "admin/conversation_detail.html",
                {
                    "conversation": conversation,
                    "guild": guild_info
                }
            )
            
    except ValueError:
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "Invalid conversation ID.",
                "error_code": 400
            },
            status_code=400
        )
    except Exception as e:
        logger.error(f"Error viewing conversation: {e}")
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "Failed to load conversation.",
                "error_code": 500
            },
            status_code=500
        )


async def cleanup_expired_conversations(request: Request) -> Response:
    """Clean up expired help conversations based on retention policies."""
    try:
        if request.method == "POST":
            async with get_db_session_context() as session:
                from datetime import datetime, timezone
                
                now = datetime.now(timezone.utc)
                
                # Find expired conversations
                expired_query = select(HelpConversation).where(
                    HelpConversation.expires_at <= now
                )
                result = await session.execute(expired_query)
                expired_conversations = result.scalars().all()
                
                # Delete expired conversations
                for conversation in expired_conversations:
                    await session.delete(conversation)
                
                await session.commit()
                
                logger.info(f"Cleaned up {len(expired_conversations)} expired conversations")
                
                return templates.TemplateResponse(
                    request,
                    "admin/cleanup_result.html",
                    {
                        "success": True,
                        "cleaned_count": len(expired_conversations),
                        "message": f"Successfully cleaned up {len(expired_conversations)} expired conversations."
                    }
                )
        
        # GET request - show cleanup interface
        async with get_db_session_context() as session:
            from datetime import datetime, timezone
            
            now = datetime.now(timezone.utc)
            
            # Count conversations by retention policy
            standard_count_result = await session.execute(
                select(func.count(HelpConversation.id))
                .where(HelpConversation.retention_policy == "standard")
            )
            standard_count = standard_count_result.scalar() or 0
            
            minimal_count_result = await session.execute(
                select(func.count(HelpConversation.id))
                .where(HelpConversation.retention_policy == "minimal")
            )
            minimal_count = minimal_count_result.scalar() or 0
            
            sensitive_count_result = await session.execute(
                select(func.count(HelpConversation.id))
                .where(HelpConversation.retention_policy == "sensitive")
            )
            sensitive_count = sensitive_count_result.scalar() or 0
            
            # Count expired conversations
            expired_count_result = await session.execute(
                select(func.count(HelpConversation.id))
                .where(HelpConversation.expires_at <= now)
            )
            expired_count = expired_count_result.scalar() or 0
            
            return templates.TemplateResponse(
                request,
                "admin/conversation_cleanup.html",
                {
                    "standard_count": standard_count,
                    "minimal_count": minimal_count,
                    "sensitive_count": sensitive_count,
                    "expired_count": expired_count,
                    "total_count": standard_count + minimal_count + sensitive_count
                }
            )
    
    except Exception as e:
        logger.error(f"Error in conversation cleanup: {e}")
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "Failed to perform conversation cleanup.",
                "error_code": 500
            },
            status_code=500
        )


async def blog_list(request: Request) -> Response:
    """List all blog posts in admin interface."""
    try:
        async with get_db_session_context() as session:
            # Get all blog posts ordered by creation date
            result = await session.execute(
                select(BlogPost)
                .order_by(BlogPost.created_at.desc())
            )
            blog_posts = result.scalars().all()
            
            return templates.TemplateResponse(
                request,
                "admin/blog_list.html",
                {
                    "blog_posts": blog_posts
                }
            )
    
    except Exception as e:
        logger.error(f"Error in blog list: {e}")
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "Failed to load blog posts.",
                "error_code": 500
            },
            status_code=500
        )


async def blog_create(request: Request) -> Response:
    """Create a new blog post."""
    if request.method == "POST":
        try:
            form_data = await request.form()
            title = form_data.get("title", "").strip()
            body = form_data.get("body", "").strip()
            author = form_data.get("author", "").strip()
            is_published = form_data.get("is_published") == "on"
            
            # Basic validation
            if not title or not body or not author:
                return templates.TemplateResponse(
                    request,
                    "admin/blog_create.html",
                    {
                        "error": "Title, body, and author are required.",
                        "title": title,
                        "body": body,
                        "author": author,
                        "is_published": is_published
                    }
                )
            
            # Generate slug from title
            slug = _generate_slug(title)
            
            async with get_db_session_context() as session:
                # Check if slug already exists
                existing_result = await session.execute(
                    select(BlogPost).where(BlogPost.slug == slug)
                )
                if existing_result.scalar_one_or_none():
                    return templates.TemplateResponse(
                        request,
                        "admin/blog_create.html",
                        {
                            "error": f"A blog post with slug '{slug}' already exists.",
                            "title": title,
                            "body": body,
                            "author": author,
                            "is_published": is_published
                        }
                    )
                
                # Create new blog post
                from datetime import datetime, timezone
                blog_post = BlogPost(
                    title=title,
                    slug=slug,
                    body=body,
                    author=author,
                    is_published=is_published,
                    published_at=datetime.now(timezone.utc) if is_published else None
                )
                
                session.add(blog_post)
                await session.commit()
                
                return templates.TemplateResponse(
                    request,
                    "admin/blog_create.html",
                    {
                        "success": f"Blog post '{title}' created successfully!",
                        "blog_post": blog_post
                    }
                )
        
        except IntegrityError:
            return templates.TemplateResponse(
                request,
                "admin/blog_create.html",
                {
                    "error": "A blog post with this slug already exists.",
                    "title": title,
                    "body": body,
                    "author": author,
                    "is_published": is_published
                }
            )
        except Exception as e:
            logger.error(f"Error creating blog post: {e}")
            return templates.TemplateResponse(
                request,
                "admin/blog_create.html",
                {
                    "error": "Failed to create blog post.",
                    "title": title,
                    "body": body,
                    "author": author,
                    "is_published": is_published
                }
            )
    
    # GET request - show create form
    return templates.TemplateResponse(
        request,
        "admin/blog_create.html",
        {}
    )


async def blog_edit(request: Request) -> Response:
    """Edit an existing blog post."""
    blog_id = request.path_params["blog_id"]
    
    try:
        async with get_db_session_context() as session:
            # Get the blog post
            result = await session.execute(
                select(BlogPost).where(BlogPost.id == UUID(blog_id))
            )
            blog_post = result.scalar_one_or_none()
            
            if not blog_post:
                return templates.TemplateResponse(
                    request,
                    "admin/error.html",
                    {
                        "error": "Blog post not found.",
                        "error_code": 404
                    },
                    status_code=404
                )
            
            if request.method == "POST":
                form_data = await request.form()
                title = form_data.get("title", "").strip()
                body = form_data.get("body", "").strip()
                author = form_data.get("author", "").strip()
                is_published = form_data.get("is_published") == "on"
                
                # Basic validation
                if not title or not body or not author:
                    return templates.TemplateResponse(
                        request,
                        "admin/blog_edit.html",
                        {
                            "error": "Title, body, and author are required.",
                            "blog_post": blog_post,
                            "title": title,
                            "body": body,
                            "author": author,
                            "is_published": is_published
                        }
                    )
                
                # Generate slug from title if title changed
                new_slug = _generate_slug(title)
                if new_slug != blog_post.slug:
                    # Check if new slug already exists
                    existing_result = await session.execute(
                        select(BlogPost).where(
                            BlogPost.slug == new_slug,
                            BlogPost.id != blog_post.id
                        )
                    )
                    if existing_result.scalar_one_or_none():
                        return templates.TemplateResponse(
                            request,
                            "admin/blog_edit.html",
                            {
                                "error": f"A blog post with slug '{new_slug}' already exists.",
                                "blog_post": blog_post,
                                "title": title,
                                "body": body,
                                "author": author,
                                "is_published": is_published
                            }
                        )
                
                # Update blog post
                from datetime import datetime, timezone
                blog_post.title = title
                blog_post.slug = new_slug
                blog_post.body = body
                blog_post.author = author
                
                # Handle publishing status
                if is_published and not blog_post.is_published:
                    # Publishing for first time
                    blog_post.is_published = True
                    blog_post.published_at = datetime.now(timezone.utc)
                elif not is_published and blog_post.is_published:
                    # Unpublishing
                    blog_post.is_published = False
                    # Keep published_at for historical reference
                else:
                    blog_post.is_published = is_published
                
                await session.commit()
                
                return templates.TemplateResponse(
                    request,
                    "admin/blog_edit.html",
                    {
                        "success": f"Blog post '{title}' updated successfully!",
                        "blog_post": blog_post
                    }
                )
            
            # GET request - show edit form
            return templates.TemplateResponse(
                request,
                "admin/blog_edit.html",
                {
                    "blog_post": blog_post
                }
            )
    
    except ValueError:
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "Invalid blog post ID.",
                "error_code": 400
            },
            status_code=400
        )
    except Exception as e:
        logger.error(f"Error editing blog post: {e}")
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "Failed to edit blog post.",
                "error_code": 500
            },
            status_code=500
        )


async def blog_delete(request: Request) -> Response:
    """Delete a blog post."""
    blog_id = request.path_params["blog_id"]
    
    try:
        async with get_db_session_context() as session:
            # Get the blog post
            result = await session.execute(
                select(BlogPost).where(BlogPost.id == UUID(blog_id))
            )
            blog_post = result.scalar_one_or_none()
            
            if not blog_post:
                return templates.TemplateResponse(
                    request,
                    "admin/error.html",
                    {
                        "error": "Blog post not found.",
                        "error_code": 404
                    },
                    status_code=404
                )
            
            # Delete the blog post
            await session.delete(blog_post)
            await session.commit()
            
            # Redirect to blog list (we'll handle this with a simple template response for now)
            return templates.TemplateResponse(
                request,
                "admin/blog_list.html",
                {
                    "success": f"Blog post '{blog_post.title}' deleted successfully!",
                    "blog_posts": []  # We could reload the list here
                }
            )
    
    except ValueError:
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "Invalid blog post ID.",
                "error_code": 400
            },
            status_code=400
        )
    except Exception as e:
        logger.error(f"Error deleting blog post: {e}")
        return templates.TemplateResponse(
            request,
            "admin/error.html",
            {
                "error": "Failed to delete blog post.",
                "error_code": 500
            },
            status_code=500
        )


def _generate_slug(title: str) -> str:
    """Generate a URL-friendly slug from a title."""
    import re
    
    # Convert to lowercase and replace spaces with hyphens
    slug = title.lower().strip()
    
    # Remove or replace special characters
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[-\s]+', '-', slug)
    
    # Remove leading/trailing hyphens
    slug = slug.strip('-')
    
    # Ensure slug is not empty
    if not slug:
        from datetime import datetime
        slug = f"post-{int(datetime.now().timestamp())}"
    
    # Limit length
    if len(slug) > 180:  # Leave room for uniqueness suffixes
        slug = slug[:180].rstrip('-')
    
    return slug


def validate_forum_agent_data(data: Dict[str, Any]) -> tuple[bool, List[str]]:
    """Validate forum agent form data.
    
    Args:
        data: Form data dictionary
        
    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    errors = []
    
    # Required fields
    if not data.get("name", "").strip():
        errors.append("Agent name is required")
    elif len(data["name"]) > 100:
        errors.append("Agent name must be 100 characters or less")
    
    system_prompt = data.get("system_prompt", "").strip()
    if not system_prompt:
        errors.append("System prompt is required")
    elif len(system_prompt) < 10:
        errors.append("System prompt must be at least 10 characters")
    elif len(system_prompt) > 10000:
        errors.append("System prompt must be 10,000 characters or less")
    
    # Response threshold validation
    try:
        threshold = float(data.get("response_threshold", 0.7))
        if threshold < 0.0 or threshold > 1.0:
            errors.append("Response threshold must be between 0.0 and 1.0")
    except (ValueError, TypeError):
        errors.append("Response threshold must be a valid number")
    
    # Rate limit validation
    try:
        rate_limit = int(data.get("max_responses_per_hour", 5))
        if rate_limit < 0:
            errors.append("Rate limit cannot be negative")
        elif rate_limit > 100:
            errors.append("Rate limit cannot exceed 100 responses per hour")
    except (ValueError, TypeError):
        errors.append("Rate limit must be a valid number")
    
    # Monitored forums validation (should be a list of channel IDs)
    monitored_forums = data.get("monitored_forums", [])
    if not isinstance(monitored_forums, list):
        errors.append("Monitored forums must be a list")
    else:
        # Filter out empty strings from the list (empty form fields)
        monitored_forums = [forum.strip() for forum in monitored_forums if forum and forum.strip()]
        # Update the data with cleaned forums list
        data["monitored_forums"] = monitored_forums
        # Note: Empty list is allowed - it means monitor all forum channels
    
    return len(errors) == 0, errors


async def forum_agents_list(request: Request) -> Response:
    """List all forum agents for a guild."""
    guild_id = request.path_params["guild_id"]
    
    try:
        # Get guild information
        guild_info = await get_guild_info(guild_id)
        
        # Get forum agents from database
        async with get_db_session_context() as session:
            forum_ops = ForumAgentOperations(session)
            agents = await forum_ops.list_agents(guild_id)
        
        context = {
            "request": request,
            "guild": guild_info,
            "agents": agents,
            "title": f"Forum Agents - {guild_info.name}",
        }
        
        return templates.TemplateResponse("admin/forum_agents_list.html", context)
        
    except GuildNotFoundError:
        context = {
            "request": request,
            "error": "Guild not found",
            "title": "Error"
        }
        return templates.TemplateResponse("admin/error.html", context, status_code=404)
    except DiscordAPIError as e:
        context = {
            "request": request,
            "error": f"Discord API error: {e}",
            "title": "Error"
        }
        return templates.TemplateResponse("admin/error.html", context, status_code=503)
    except Exception as e:
        logger.error(f"Error listing forum agents for guild {guild_id}: {e}")
        context = {
            "request": request,
            "error": "Database error occurred",
            "title": "Error"
        }
        return templates.TemplateResponse("admin/error.html", context, status_code=500)


async def forum_agent_create(request: Request) -> Response:
    """Create a new forum agent."""
    guild_id = request.path_params["guild_id"]
    
    try:
        # Get guild information
        guild_info = await get_guild_info(guild_id)
        
        if request.method == "GET":
            # Show create form
            context = {
                "request": request,
                "guild": guild_info,
                "title": f"Create Forum Agent - {guild_info.name}",
            }
            return templates.TemplateResponse("admin/forum_agent_create.html", context)
        
        elif request.method == "POST":
            # Process form submission
            form_data = await request.form()
            data = {
                "name": form_data.get("name", "").strip(),
                "description": form_data.get("description", "").strip(),
                "system_prompt": form_data.get("system_prompt", "").strip(),
                "response_threshold": form_data.get("response_threshold", "0.7"),
                "max_responses_per_hour": form_data.get("max_responses_per_hour", "5"),
                "monitored_forums": form_data.getlist("monitored_forums[]"),
                "is_active": form_data.get("is_active") == "on",
            }
            
            # Validate data
            is_valid, errors = validate_forum_agent_data(data)
            
            # Debug logging
            logger.info(f"Forum agent form data: {data}")
            logger.info(f"Validation result: valid={is_valid}, errors={errors}")
            
            if not is_valid:
                context = {
                    "request": request,
                    "guild": guild_info,
                    "errors": errors,
                    "form_data": data,
                    "title": f"Create Forum Agent - {guild_info.name}",
                }
                return templates.TemplateResponse("admin/forum_agent_create.html", context, status_code=400)
            
            # Create agent
            async with get_db_session_context() as session:
                try:
                    forum_ops = ForumAgentOperations(session)
                    await forum_ops.create_agent(
                        guild_id=guild_id,
                        name=data["name"],
                        description=data.get("description") or None,
                        system_prompt=data["system_prompt"],
                        monitored_forums=data["monitored_forums"],
                        response_threshold=float(data["response_threshold"]),
                        max_responses_per_hour=int(data["max_responses_per_hour"]),
                        is_active=data.get("is_active", True),
                        created_by="admin"  # TODO: Get from authenticated user
                    )
                    
                    # Redirect to agents list
                    return RedirectResponse(
                        url=f"/admin/guilds/{guild_id}/forum-agents",
                        status_code=303
                    )
                    
                except Exception as e:
                    logger.error(f"Error creating forum agent: {e}")
                    context = {
                        "request": request,
                        "guild": guild_info,
                        "errors": [f"Failed to create agent: {e}"],
                        "form_data": data,
                        "title": f"Create Forum Agent - {guild_info.name}",
                    }
                    return templates.TemplateResponse("admin/forum_agent_create.html", context, status_code=500)
        
    except GuildNotFoundError:
        context = {
            "request": request,
            "error": "Guild not found",
            "title": "Error"
        }
        return templates.TemplateResponse("admin/error.html", context, status_code=404)
    except Exception as e:
        logger.error(f"Error in forum agent create for guild {guild_id}: {e}")
        context = {
            "request": request,
            "error": "An unexpected error occurred",
            "title": "Error"
        }
        return templates.TemplateResponse("admin/error.html", context, status_code=500)


async def forum_agent_edit(request: Request) -> Response:
    """Edit an existing forum agent."""
    guild_id = request.path_params["guild_id"]
    agent_id = request.path_params["agent_id"]
    
    try:
        # Get guild information
        guild_info = await get_guild_info(guild_id)
        
        async with get_db_session_context() as session:
            forum_ops = ForumAgentOperations(session)
            
            # Get the agent
            agent = await forum_ops.get_agent(UUID(agent_id), guild_id)
            if not agent:
                context = {
                    "request": request,
                    "error": "Forum agent not found",
                    "title": "Error"
                }
                return templates.TemplateResponse("admin/error.html", context, status_code=404)
            
            if request.method == "GET":
                # Show edit form
                context = {
                    "request": request,
                    "guild": guild_info,
                    "agent": agent,
                    "title": f"Edit Forum Agent: {agent.name}",
                }
                return templates.TemplateResponse("admin/forum_agent_edit.html", context)
            
            elif request.method == "POST":
                # Process form submission
                form_data = await request.form()
                data = {
                    "name": form_data.get("name", "").strip(),
                    "description": form_data.get("description", "").strip(),
                    "system_prompt": form_data.get("system_prompt", "").strip(),
                    "response_threshold": form_data.get("response_threshold", "0.7"),
                    "max_responses_per_hour": form_data.get("max_responses_per_hour", "5"),
                    "monitored_forums": form_data.getlist("monitored_forums[]"),
                    "is_active": form_data.get("is_active") == "on",
                }
                
                # Validate data
                is_valid, errors = validate_forum_agent_data(data)
                
                # Debug logging
                logger.info(f"Forum agent EDIT form data: {data}")
                logger.info(f"EDIT validation result: valid={is_valid}, errors={errors}")
                
                if not is_valid:
                    context = {
                        "request": request,
                        "guild": guild_info,
                        "agent": agent,
                        "errors": errors,
                        "form_data": data,
                        "title": f"Edit Forum Agent: {agent.name}",
                    }
                    return templates.TemplateResponse("admin/forum_agent_edit.html", context, status_code=400)
                
                # Update agent
                try:
                    await forum_ops.update_agent(
                        agent_id=UUID(agent_id),
                        guild_id=guild_id,
                        name=data["name"],
                        description=data.get("description") or None,
                        system_prompt=data["system_prompt"],
                        monitored_forums=data["monitored_forums"],
                        response_threshold=float(data["response_threshold"]),
                        max_responses_per_hour=int(data["max_responses_per_hour"]),
                        is_active=data.get("is_active", True)
                    )
                    
                    # Redirect to agents list
                    return RedirectResponse(
                        url=f"/admin/guilds/{guild_id}/forum-agents",
                        status_code=303
                    )
                    
                except Exception as e:
                    logger.error(f"Error updating forum agent: {e}")
                    context = {
                        "request": request,
                        "guild": guild_info,
                        "agent": agent,
                        "errors": [f"Failed to update agent: {e}"],
                        "form_data": data,
                        "title": f"Edit Forum Agent: {agent.name}",
                    }
                    return templates.TemplateResponse("admin/forum_agent_edit.html", context, status_code=500)
        
    except GuildNotFoundError:
        context = {
            "request": request,
            "error": "Guild not found",
            "title": "Error"
        }
        return templates.TemplateResponse("admin/error.html", context, status_code=404)
    except Exception as e:
        logger.error(f"Error in forum agent edit for guild {guild_id}, agent {agent_id}: {e}")
        context = {
            "request": request,
            "error": "An unexpected error occurred",
            "title": "Error"
        }
        return templates.TemplateResponse("admin/error.html", context, status_code=500)


async def forum_agent_delete(request: Request) -> Response:
    """Delete a forum agent."""
    guild_id = request.path_params["guild_id"]
    agent_id = request.path_params["agent_id"]
    
    try:
        async with get_db_session_context() as session:
            forum_ops = ForumAgentOperations(session)
            
            deleted = await forum_ops.delete_agent(UUID(agent_id), guild_id)
            
            if not deleted:
                context = {
                    "request": request,
                    "error": "Forum agent not found",
                    "title": "Error"
                }
                return templates.TemplateResponse("admin/error.html", context, status_code=404)
        
        # Redirect to agents list
        return RedirectResponse(
            url=f"/admin/guilds/{guild_id}/forum-agents",
            status_code=303
        )
        
    except Exception as e:
        logger.error(f"Error deleting forum agent {agent_id}: {e}")
        context = {
            "request": request,
            "error": "Failed to delete forum agent",
            "title": "Error"
        }
        return templates.TemplateResponse("admin/error.html", context, status_code=500)


async def forum_agent_toggle(request: Request) -> Response:
    """Toggle forum agent active status."""
    guild_id = request.path_params["guild_id"]
    agent_id = request.path_params["agent_id"]
    
    try:
        async with get_db_session_context() as session:
            forum_ops = ForumAgentOperations(session)
            
            agent = await forum_ops.toggle_agent(UUID(agent_id), guild_id)
            
            if not agent:
                context = {
                    "request": request,
                    "error": "Forum agent not found",
                    "title": "Error"
                }
                return templates.TemplateResponse("admin/error.html", context, status_code=404)
        
        # Redirect to agents list
        return RedirectResponse(
            url=f"/admin/guilds/{guild_id}/forum-agents",
            status_code=303
        )
        
    except Exception as e:
        logger.error(f"Error toggling forum agent {agent_id}: {e}")
        context = {
            "request": request,
            "error": "Failed to toggle forum agent",
            "title": "Error"
        }
        return templates.TemplateResponse("admin/error.html", context, status_code=500)


async def forum_agent_analytics(request: Request) -> Response:
    """Show forum agent analytics."""
    guild_id = request.path_params["guild_id"]
    agent_id = request.path_params["agent_id"]
    
    try:
        # Get guild information
        guild_info = await get_guild_info(guild_id)
        
        async with get_db_session_context() as session:
            forum_ops = ForumAgentOperations(session)
            
            # Get analytics
            analytics = await forum_ops.get_agent_analytics(UUID(agent_id), guild_id)
            
            if not analytics:
                context = {
                    "request": request,
                    "error": "Forum agent not found",
                    "title": "Error"
                }
                return templates.TemplateResponse("admin/error.html", context, status_code=404)
        
        # Handle empty analytics (agent not found)
        if not analytics or 'agent' not in analytics:
            context = {
                "request": request,
                "error": f"Forum agent {agent_id} not found in guild {guild_id}",
                "title": "Error"
            }
            return templates.TemplateResponse("admin/error.html", context, status_code=404)
        
        # Flatten and adapt analytics structure for template convenience
        stats = analytics['statistics']
        flattened_analytics = {
            "total_evaluations": stats['total_evaluations'],
            "total_responses": stats['total_responses'], 
            "response_rate": stats['response_rate'],
            "total_tokens": stats['total_tokens_used'],  # Template expects 'total_tokens'
            "average_confidence": stats['average_confidence'] if stats['average_confidence'] is not None else "N/A",
            "average_response_time_ms": stats['average_response_time_ms'] if stats['average_response_time_ms'] is not None else "N/A",
            "agent": analytics['agent']
        }
        
        context = {
            "request": request,
            "guild": guild_info,
            "agent": analytics['agent'],  # Extract agent data for template
            "analytics": flattened_analytics,  # Flattened for easier template access
            "recent_responses": analytics.get('recent_responses', []),  # Add recent responses for activity table
            "title": f"Analytics: {analytics['agent']['name']}",
        }
        
        return templates.TemplateResponse("admin/forum_agent_analytics.html", context)
        
    except GuildNotFoundError:
        context = {
            "request": request,
            "error": "Guild not found",
            "title": "Error"
        }
        return templates.TemplateResponse("admin/error.html", context, status_code=404)
    except Exception as e:
        import traceback
        logger.error(f"Error getting forum agent analytics for {agent_id}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        context = {
            "request": request,
            "error": "Failed to load analytics",
            "title": "Error"
        }
        return templates.TemplateResponse("admin/error.html", context, status_code=500)


async def get_forum_response_details(request: Request) -> Response:
    """Get detailed information about a forum agent response."""
    response_id = request.path_params["response_id"]
    
    try:
        async with get_db_session_context() as session:
            forum_ops = ForumAgentOperations(session)
            
            # Get the forum response
            result = await session.execute(
                select(ForumAgentResponse, ForumAgent)
                .join(ForumAgent, ForumAgentResponse.agent_id == ForumAgent.id)
                .where(ForumAgentResponse.id == UUID(response_id))
            )
            row = result.first()
            
            if not row:
                return Response(
                    content='{"error": "Response not found"}',
                    media_type="application/json",
                    status_code=404
                )
            
            response, agent = row
            
            # Format the response data
            response_data = {
                "id": str(response.id),
                "agent_name": agent.name,
                "post_title": response.post_title or "Untitled",
                "post_content": response.post_content or "",
                "author_display_name": response.author_display_name or "Unknown",
                "post_tags": response.post_tags or [],
                "confidence_score": response.confidence_score,
                "decision_reasoning": response.decision_reason or "",
                "responded": response.responded,
                "response_content": response.response_content or "",
                "tokens_used": response.tokens_used or 0,
                "response_time_ms": response.response_time_ms,
                "created_at": response.created_at.isoformat() if response.created_at else "",
                "responded_at": response.responded_at.isoformat() if response.responded_at else ""
            }
            
            import json
            return Response(
                content=json.dumps(response_data),
                media_type="application/json"
            )
            
    except Exception as e:
        logger.error(f"Error getting response details for {response_id}: {e}")
        return Response(
            content='{"error": "Failed to load response details"}',
            media_type="application/json",
            status_code=500
        )


async def forum_agents_bulk(request: Request) -> Response:
    """Perform bulk operations on forum agents."""
    guild_id = request.path_params["guild_id"]
    
    try:
        form_data = await request.form()
        action = form_data.get("action", "")
        agent_ids = form_data.getlist("agent_ids")
        
        if not action or not agent_ids:
            context = {
                "request": request,
                "error": "Invalid bulk operation request",
                "title": "Error"
            }
            return templates.TemplateResponse("admin/error.html", context, status_code=400)
        
        # Convert string UUIDs to UUID objects
        uuid_agent_ids = [UUID(aid) for aid in agent_ids]
        
        async with get_db_session_context() as session:
            forum_ops = ForumAgentOperations(session)
            
            modified_count = await forum_ops.bulk_update_agents(
                agent_ids=uuid_agent_ids,
                guild_id=guild_id,
                action=action
            )
        
        # Redirect to agents list with success message
        return RedirectResponse(
            url=f"/admin/guilds/{guild_id}/forum-agents",
            status_code=303
        )
        
    except Exception as e:
        logger.error(f"Error in bulk forum agent operation: {e}")
        context = {
            "request": request,
            "error": "Failed to perform bulk operation",
            "title": "Error"
        }
        return templates.TemplateResponse("admin/error.html", context, status_code=500)