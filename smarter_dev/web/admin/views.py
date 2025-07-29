"""Admin interface view handlers."""

from __future__ import annotations

import logging
from typing import Dict, Any, List
from uuid import UUID

from starlette.requests import Request
from starlette.responses import Response
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
    APIKey
)
from smarter_dev.web.crud import BytesOperations, BytesConfigOperations, SquadOperations, APIKeyOperations
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
                "total_bytes": total_bytes
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
                "total_bytes": 0
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
                "total_bytes": 0
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