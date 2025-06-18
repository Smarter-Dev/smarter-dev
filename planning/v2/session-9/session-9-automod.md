# Session 9: Auto-Moderation System

## Objective
Implement a comprehensive auto-moderation system with username filtering, message rate limiting, duplicate detection, and file extension blocking. Focus on performance and configurability.

## Prerequisites
- Completed Session 8 (squad system exists)
- Understanding of Discord events and permissions
- Moderation case logging system ready

## Task 1: AutoMod Service Layer

### bot/services/automod_service.py

Create the auto-moderation business logic:

```python
import re
import hashlib
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
from collections import defaultdict, deque
import hikari
import structlog

from bot.errors import ConfigurationError
from shared.types import ModerationAction, AutoModRuleType

logger = structlog.get_logger()

class MessageTracker:
    """Track user messages for rate limiting."""
    
    def __init__(self):
        # user_id -> deque of (timestamp, channel_id, content_hash)
        self.messages: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self.last_cleanup = datetime.utcnow()
    
    def add_message(
        self,
        user_id: str,
        channel_id: str,
        content: str,
        timestamp: datetime
    ):
        """Add a message to tracking."""
        content_hash = hashlib.md5(content.lower().encode()).hexdigest()
        self.messages[user_id].append((timestamp, channel_id, content_hash))
        
        # Periodic cleanup
        if (datetime.utcnow() - self.last_cleanup).total_seconds() > 300:
            self.cleanup()
    
    def cleanup(self):
        """Remove old messages from tracking."""
        cutoff = datetime.utcnow() - timedelta(minutes=10)
        
        for user_id, messages in list(self.messages.items()):
            # Remove old messages
            while messages and messages[0][0] < cutoff:
                messages.popleft()
            
            # Remove empty entries
            if not messages:
                del self.messages[user_id]
        
        self.last_cleanup = datetime.utcnow()
    
    def get_recent_messages(
        self,
        user_id: str,
        seconds: int
    ) -> List[Tuple[datetime, str, str]]:
        """Get messages from the last N seconds."""
        if user_id not in self.messages:
            return []
        
        cutoff = datetime.utcnow() - timedelta(seconds=seconds)
        return [
            msg for msg in self.messages[user_id]
            if msg[0] >= cutoff
        ]

class AutoModService:
    """Service for auto-moderation operations."""
    
    def __init__(self, api_client):
        self.api = api_client
        self.message_tracker = MessageTracker()
        self._rules_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._cache_time: Dict[str, datetime] = {}
    
    async def get_rules(self, guild_id: str) -> List[Dict[str, Any]]:
        """Get auto-mod rules for guild."""
        cache_key = f"rules:{guild_id}"
        now = datetime.utcnow()
        
        # Check cache
        if cache_key in self._rules_cache:
            cache_time = self._cache_time.get(cache_key)
            if cache_time and (now - cache_time).total_seconds() < 300:
                return self._rules_cache[cache_key]
        
        try:
            # Fetch from API
            rules = await self.api.get_automod_rules(guild_id)
            
            # Filter active rules
            active_rules = [r for r in rules if r.get("is_active", True)]
            
            # Sort by priority
            active_rules.sort(key=lambda r: r.get("priority", 0))
            
            # Cache
            self._rules_cache[cache_key] = active_rules
            self._cache_time[cache_key] = now
            
            return active_rules
            
        except Exception as e:
            logger.error(
                "Failed to fetch automod rules",
                guild_id=guild_id,
                error=str(e)
            )
            return []
    
    async def check_username(
        self,
        member: hikari.Member,
        rules: List[Dict[str, Any]]
    ) -> Optional[Tuple[Dict[str, Any], str]]:
        """Check username against rules."""
        username_rules = [
            r for r in rules
            if r["rule_type"] == AutoModRuleType.USERNAME_REGEX
        ]
        
        for rule in username_rules:
            config = rule.get("config", {})
            
            # Check regex pattern
            pattern = config.get("pattern")
            if pattern:
                try:
                    if re.search(pattern, member.username, re.IGNORECASE):
                        # Check additional conditions
                        
                        # Account age check
                        min_age_days = config.get("min_account_age_days", 0)
                        if min_age_days > 0:
                            account_age = datetime.utcnow() - member.created_at
                            if account_age.days < min_age_days:
                                return rule, f"Username matches pattern and account is too new ({account_age.days} days old)"
                        
                        # Avatar check
                        if config.get("require_avatar", False) and not member.avatar_hash:
                            return rule, "Username matches pattern and no avatar set"
                        
                        # Pattern matched with no additional conditions
                        return rule, f"Username matches forbidden pattern: {pattern}"
                        
                except re.error:
                    logger.error(
                        "Invalid regex pattern",
                        rule_id=rule.get("id"),
                        pattern=pattern
                    )
        
        return None
    
    async def check_message_rate(
        self,
        user_id: str,
        channel_id: str,
        content: str,
        rules: List[Dict[str, Any]]
    ) -> Optional[Tuple[Dict[str, Any], str]]:
        """Check message against rate limit rules."""
        # Track message
        self.message_tracker.add_message(
            user_id,
            channel_id,
            content,
            datetime.utcnow()
        )
        
        rate_rules = [
            r for r in rules
            if r["rule_type"] == AutoModRuleType.MESSAGE_RATE
        ]
        
        for rule in rate_rules:
            config = rule.get("config", {})
            timeframe = config.get("timeframe_seconds", 60)
            
            # Get recent messages
            recent = self.message_tracker.get_recent_messages(user_id, timeframe)
            
            if not recent:
                continue
            
            # Check message count
            max_messages = config.get("max_messages", 10)
            if len(recent) > max_messages:
                return rule, f"Too many messages: {len(recent)} in {timeframe}s (max: {max_messages})"
            
            # Check duplicate messages
            max_duplicates = config.get("max_duplicates", 3)
            if max_duplicates > 0:
                content_hashes = [msg[2] for msg in recent]
                
                # Count duplicates
                for hash_value in set(content_hashes):
                    count = content_hashes.count(hash_value)
                    if count > max_duplicates:
                        return rule, f"Duplicate message spam: {count} copies in {timeframe}s"
            
            # Check channel spam
            max_channels = config.get("max_channels", 5)
            if max_channels > 0:
                channels = set(msg[1] for msg in recent)
                if len(channels) > max_channels:
                    return rule, f"Channel spam: {len(channels)} channels in {timeframe}s"
        
        return None
    
    async def check_file_extension(
        self,
        attachments: List[hikari.Attachment],
        rules: List[Dict[str, Any]]
    ) -> Optional[Tuple[Dict[str, Any], str]]:
        """Check file attachments against rules."""
        if not attachments:
            return None
        
        file_rules = [
            r for r in rules
            if r["rule_type"] == AutoModRuleType.FILE_EXTENSION
        ]
        
        for rule in file_rules:
            config = rule.get("config", {})
            
            # Get blocked extensions
            blocked_extensions = config.get("blocked_extensions", [])
            if not blocked_extensions:
                continue
            
            # Normalize extensions
            blocked_extensions = [ext.lower().strip('.') for ext in blocked_extensions]
            
            for attachment in attachments:
                # Get file extension
                filename = attachment.filename.lower()
                extension = filename.split('.')[-1] if '.' in filename else ''
                
                if extension in blocked_extensions:
                    return rule, f"Blocked file type: .{extension}"
        
        return None
    
    async def execute_action(
        self,
        guild: hikari.Guild,
        member: hikari.Member,
        action: ModerationAction,
        reason: str,
        rule: Dict[str, Any]
    ) -> bool:
        """Execute moderation action."""
        try:
            # Log to database
            await self.api.create_moderation_case(
                guild_id=str(guild.id),
                user_id=str(member.id),
                user_tag=f"{member.username}#{member.discriminator}",
                moderator_id="automod",
                moderator_tag="AutoMod",
                action=action,
                reason=f"[AutoMod] {reason}"
            )
            
            # Execute action
            if action == ModerationAction.BAN:
                await guild.ban(member, reason=reason)
                
            elif action == ModerationAction.KICK:
                await member.kick(reason=reason)
                
            elif action == ModerationAction.TIMEOUT:
                # Get timeout duration from rule config
                duration_minutes = rule.get("config", {}).get("timeout_duration", 10)
                until = datetime.utcnow() + timedelta(minutes=duration_minutes)
                
                await member.edit(
                    communication_disabled_until=until,
                    reason=reason
                )
                
            elif action == ModerationAction.WARN:
                # Just log, no Discord action
                pass
            
            logger.info(
                "AutoMod action executed",
                guild_id=guild.id,
                user_id=member.id,
                action=action,
                reason=reason
            )
            
            return True
            
        except Exception as e:
            logger.error(
                "Failed to execute automod action",
                guild_id=guild.id,
                user_id=member.id,
                action=action,
                error=str(e)
            )
            return False
    
    def clear_cache(self, guild_id: str):
        """Clear rules cache for guild."""
        cache_key = f"rules:{guild_id}"
        self._rules_cache.pop(cache_key, None)
        self._cache_time.pop(cache_key, None)
```

## Task 2: AutoMod Plugin

### bot/plugins/automod.py

Create the auto-moderation plugin:

```python
import hikari
import lightbulb
from typing import Optional
import structlog

from bot.plugins.base import BasePlugin
from bot.services.automod_service import AutoModService
from bot.utils.embeds import EmbedBuilder
from shared.types import ModerationAction

logger = structlog.get_logger()

class AutoModPlugin(BasePlugin):
    """Plugin for auto-moderation."""
    
    def __init__(self):
        super().__init__("automod", "Automatic moderation system")
        self.service: Optional[AutoModService] = None
    
    def load(self, bot: lightbulb.BotApp) -> None:
        """Load the plugin."""
        super().load(bot)
        self.service = AutoModService(bot.api)
        
        # Subscribe to events
        bot.subscribe(hikari.MemberCreateEvent, self.on_member_join)
        bot.subscribe(hikari.GuildMessageCreateEvent, self.on_message)
    
    async def on_member_join(self, event: hikari.MemberCreateEvent) -> None:
        """Check new members against username rules."""
        # Get rules
        rules = await self.service.get_rules(str(event.guild_id))
        if not rules:
            return
        
        # Check username
        result = await self.service.check_username(event.member, rules)
        if result:
            rule, reason = result
            action = ModerationAction(rule["action"])
            
            # Execute action
            await self.service.execute_action(
                event.get_guild(),
                event.member,
                action,
                reason,
                rule
            )
            
            # Log to mod channel if configured
            await self.send_to_mod_log(
                event.guild_id,
                event.member,
                action,
                reason
            )
    
    async def on_message(self, event: hikari.GuildMessageCreateEvent) -> None:
        """Check messages against rules."""
        # Ignore bots and system messages
        if event.is_bot or not event.content:
            return
        
        # Ignore members with manage messages permission
        member = event.member
        if not member:
            return
        
        perms = lightbulb.utils.permissions_for(member)
        if hikari.Permissions.MANAGE_MESSAGES in perms:
            return
        
        # Get rules
        rules = await self.service.get_rules(str(event.guild_id))
        if not rules:
            return
        
        # Check message rate
        rate_result = await self.service.check_message_rate(
            str(event.author_id),
            str(event.channel_id),
            event.content,
            rules
        )
        
        if rate_result:
            rule, reason = rate_result
            action = ModerationAction(rule["action"])
            
            # Delete message if action is not just warn
            if action != ModerationAction.WARN:
                try:
                    await event.message.delete()
                except:
                    pass
            
            # Execute action
            await self.service.execute_action(
                event.get_guild(),
                member,
                action,
                reason,
                rule
            )
            
            await self.send_to_mod_log(
                event.guild_id,
                member,
                action,
                reason
            )
            return
        
        # Check file extensions
        if event.message.attachments:
            file_result = await self.service.check_file_extension(
                event.message.attachments,
                rules
            )
            
            if file_result:
                rule, reason = file_result
                action = ModerationAction(rule["action"])
                
                # Always delete message with blocked files
                try:
                    await event.message.delete()
                except:
                    pass
                
                # Execute additional action if not DELETE
                if action != ModerationAction.DELETE:
                    await self.service.execute_action(
                        event.get_guild(),
                        member,
                        action,
                        reason,
                        rule
                    )
                
                # Notify user
                try:
                    await event.message.respond(
                        embed=EmbedBuilder.error(
                            "File Blocked",
                            f"Your message was removed: {reason}"
                        ),
                        flags=hikari.MessageFlag.EPHEMERAL,
                        delete_after=10
                    )
                except:
                    pass
                
                await self.send_to_mod_log(
                    event.guild_id,
                    member,
                    ModerationAction.DELETE,
                    reason
                )
    
    async def send_to_mod_log(
        self,
        guild_id: int,
        member: hikari.Member,
        action: ModerationAction,
        reason: str
    ):
        """Send automod action to mod log channel."""
        # This would check guild config for mod log channel
        # For now, just log
        logger.info(
            "AutoMod action",
            guild_id=guild_id,
            user=f"{member.username}#{member.discriminator}",
            action=action.value,
            reason=reason
        )

automod_plugin = AutoModPlugin()

# Admin commands for testing rules
@automod_plugin.command
@lightbulb.app_command_permissions(dm_enabled=False)
@lightbulb.add_checks(lightbulb.has_guild_permissions(hikari.Permissions.MANAGE_GUILD))
@lightbulb.command("automod", "Auto-moderation commands")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def automod_group(ctx: lightbulb.SlashContext) -> None:
    """AutoMod command group."""
    pass

@automod_group.child
@lightbulb.command("test", "Test automod rules")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def test_rules(ctx: lightbulb.SlashContext) -> None:
    """Test current automod configuration."""
    await ctx.respond(hikari.ResponseType.DEFERRED, flags=hikari.MessageFlag.EPHEMERAL)
    
    try:
        rules = await automod_plugin.service.get_rules(str(ctx.guild_id))
        
        if not rules:
            await ctx.respond(
                embed=EmbedBuilder.info(
                    "AutoMod Status",
                    "No active auto-moderation rules configured."
                )
            )
            return
        
        embed = hikari.Embed(
            title="🛡️ AutoMod Configuration",
            description=f"**{len(rules)}** active rules",
            color=0x3B82F6,
            timestamp=datetime.utcnow()
        )
        
        # Group by type
        by_type = {}
        for rule in rules:
            rule_type = rule["rule_type"]
            if rule_type not in by_type:
                by_type[rule_type] = []
            by_type[rule_type].append(rule)
        
        # Add fields for each type
        type_names = {
            AutoModRuleType.USERNAME_REGEX: "Username Filters",
            AutoModRuleType.MESSAGE_RATE: "Rate Limits",
            AutoModRuleType.FILE_EXTENSION: "File Filters"
        }
        
        for rule_type, type_rules in by_type.items():
            type_name = type_names.get(rule_type, rule_type)
            
            value_lines = []
            for rule in type_rules[:3]:  # Show max 3 per type
                action = rule["action"]
                config = rule.get("config", {})
                
                if rule_type == AutoModRuleType.USERNAME_REGEX:
                    pattern = config.get("pattern", "No pattern")
                    value_lines.append(f"• `{pattern}` → {action}")
                elif rule_type == AutoModRuleType.MESSAGE_RATE:
                    max_msg = config.get("max_messages", 10)
                    timeframe = config.get("timeframe_seconds", 60)
                    value_lines.append(f"• {max_msg} msgs/{timeframe}s → {action}")
                elif rule_type == AutoModRuleType.FILE_EXTENSION:
                    exts = config.get("blocked_extensions", [])
                    ext_str = ", ".join(f".{e}" for e in exts[:5])
                    value_lines.append(f"• {ext_str} → {action}")
            
            if len(type_rules) > 3:
                value_lines.append(f"*+{len(type_rules) - 3} more*")
            
            embed.add_field(
                type_name,
                "\n".join(value_lines) or "None",
                inline=False
            )
        
        await ctx.respond(embed=embed)
        
    except Exception as e:
        logger.error("Failed to test automod", error=str(e))
        await automod_plugin.send_error(
            ctx,
            "Failed to fetch automod configuration."
        )

@automod_group.child
@lightbulb.command("reload", "Reload automod rules from server")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def reload_rules(ctx: lightbulb.SlashContext) -> None:
    """Force reload of automod rules."""
    automod_plugin.service.clear_cache(str(ctx.guild_id))
    
    await ctx.respond(
        embed=EmbedBuilder.success(
            "Rules Reloaded",
            "AutoMod rules have been refreshed from the server."
        ),
        flags=hikari.MessageFlag.EPHEMERAL
    )

def load(bot: lightbulb.BotApp) -> None:
    """Load the plugin."""
    bot.add_plugin(automod_plugin)

def unload(bot: lightbulb.BotApp) -> None:
    """Unload the plugin."""
    bot.remove_plugin(automod_plugin)
```

## Task 3: AutoMod API Endpoints

### web/api/routers/automod.py

Create API endpoints for automod configuration:

```python
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional
from datetime import datetime
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from web.api.dependencies import CurrentAPIKey, DatabaseSession
from web.api.schemas import (
    AutoModRuleResponse,
    AutoModRuleCreateRequest,
    AutoModRuleUpdateRequest,
    ModCaseResponse
)
from web.models.moderation import AutoModRule, ModerationCase
from shared.types import AutoModRuleType, ModerationAction
import structlog

logger = structlog.get_logger()
router = APIRouter()

@router.get("/guilds/{guild_id}/automod/rules", response_model=List[AutoModRuleResponse])
async def get_automod_rules(
    guild_id: str,
    include_inactive: bool = False,
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> List[AutoModRuleResponse]:
    """Get automod rules for guild."""
    query = select(AutoModRule).where(AutoModRule.guild_id == guild_id)
    
    if not include_inactive:
        query = query.where(AutoModRule.is_active == True)
    
    query = query.order_by(AutoModRule.priority, AutoModRule.created_at)
    
    result = await db.execute(query)
    rules = result.scalars().all()
    
    return [
        AutoModRuleResponse(
            id=str(rule.id),
            guild_id=rule.guild_id,
            rule_type=rule.rule_type,
            config=rule.config,
            action=rule.action,
            priority=rule.priority,
            is_active=rule.is_active,
            created_at=rule.created_at.isoformat(),
            updated_at=rule.updated_at.isoformat()
        )
        for rule in rules
    ]

@router.post("/guilds/{guild_id}/automod/rules", response_model=AutoModRuleResponse)
async def create_automod_rule(
    guild_id: str,
    request: AutoModRuleCreateRequest,
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> AutoModRuleResponse:
    """Create new automod rule."""
    # Validate rule configuration
    if request.rule_type == AutoModRuleType.USERNAME_REGEX:
        if not request.config.get("pattern"):
            raise HTTPException(400, "Username regex rule requires 'pattern' in config")
        
        # Validate regex
        import re
        try:
            re.compile(request.config["pattern"])
        except re.error:
            raise HTTPException(400, "Invalid regex pattern")
    
    elif request.rule_type == AutoModRuleType.MESSAGE_RATE:
        required = ["max_messages", "timeframe_seconds"]
        if not all(k in request.config for k in required):
            raise HTTPException(
                400,
                f"Message rate rule requires: {', '.join(required)}"
            )
    
    elif request.rule_type == AutoModRuleType.FILE_EXTENSION:
        if not request.config.get("blocked_extensions"):
            raise HTTPException(
                400,
                "File extension rule requires 'blocked_extensions' list"
            )
    
    # Create rule
    rule = AutoModRule(
        guild_id=guild_id,
        rule_type=request.rule_type,
        config=request.config,
        action=request.action,
        priority=request.priority,
        is_active=True
    )
    
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    
    logger.info(
        "AutoMod rule created",
        guild_id=guild_id,
        rule_id=str(rule.id),
        rule_type=rule.rule_type.value
    )
    
    return AutoModRuleResponse(
        id=str(rule.id),
        guild_id=rule.guild_id,
        rule_type=rule.rule_type,
        config=rule.config,
        action=rule.action,
        priority=rule.priority,
        is_active=rule.is_active,
        created_at=rule.created_at.isoformat(),
        updated_at=rule.updated_at.isoformat()
    )

@router.put("/guilds/{guild_id}/automod/rules/{rule_id}")
async def update_automod_rule(
    guild_id: str,
    rule_id: UUID,
    request: AutoModRuleUpdateRequest,
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> AutoModRuleResponse:
    """Update automod rule."""
    # Get rule
    result = await db.execute(
        select(AutoModRule).where(
            AutoModRule.id == rule_id,
            AutoModRule.guild_id == guild_id
        )
    )
    rule = result.scalar_one_or_none()
    
    if not rule:
        raise HTTPException(404, "Rule not found")
    
    # Update fields
    if request.config is not None:
        rule.config = request.config
    if request.action is not None:
        rule.action = request.action
    if request.priority is not None:
        rule.priority = request.priority
    if request.is_active is not None:
        rule.is_active = request.is_active
    
    await db.commit()
    await db.refresh(rule)
    
    logger.info(
        "AutoMod rule updated",
        guild_id=guild_id,
        rule_id=str(rule.id)
    )
    
    return AutoModRuleResponse(
        id=str(rule.id),
        guild_id=rule.guild_id,
        rule_type=rule.rule_type,
        config=rule.config,
        action=rule.action,
        priority=rule.priority,
        is_active=rule.is_active,
        created_at=rule.created_at.isoformat(),
        updated_at=rule.updated_at.isoformat()
    )

@router.delete("/guilds/{guild_id}/automod/rules/{rule_id}")
async def delete_automod_rule(
    guild_id: str,
    rule_id: UUID,
    api_key: CurrentAPIKey,
    db: DatabaseSession
):
    """Delete automod rule."""
    result = await db.execute(
        select(AutoModRule).where(
            AutoModRule.id == rule_id,
            AutoModRule.guild_id == guild_id
        )
    )
    rule = result.scalar_one_or_none()
    
    if not rule:
        raise HTTPException(404, "Rule not found")
    
    await db.delete(rule)
    await db.commit()
    
    logger.info(
        "AutoMod rule deleted",
        guild_id=guild_id,
        rule_id=str(rule_id)
    )
    
    return {"status": "deleted"}

@router.post("/guilds/{guild_id}/moderation/cases", response_model=ModCaseResponse)
async def create_moderation_case(
    guild_id: str,
    request: dict,  # Define proper schema
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> ModCaseResponse:
    """Create moderation case (used by automod)."""
    case = ModerationCase(
        guild_id=guild_id,
        user_id=request["user_id"],
        user_tag=request["user_tag"],
        moderator_id=request["moderator_id"],
        moderator_tag=request["moderator_tag"],
        action=ModerationAction(request["action"]),
        reason=request["reason"],
        expires_at=request.get("expires_at")
    )
    
    db.add(case)
    await db.commit()
    await db.refresh(case)
    
    return ModCaseResponse(
        id=str(case.id),
        guild_id=case.guild_id,
        user_id=case.user_id,
        user_tag=case.user_tag,
        moderator_id=case.moderator_id,
        moderator_tag=case.moderator_tag,
        action=case.action,
        reason=case.reason,
        expires_at=case.expires_at.isoformat() if case.expires_at else None,
        resolved=case.resolved,
        created_at=case.created_at.isoformat()
    )

@router.get("/guilds/{guild_id}/moderation/cases")
async def get_moderation_cases(
    guild_id: str,
    user_id: Optional[str] = None,
    resolved: Optional[bool] = None,
    limit: int = Query(50, le=100),
    api_key: CurrentAPIKey,
    db: DatabaseSession
):
    """Get moderation cases."""
    query = select(ModerationCase).where(ModerationCase.guild_id == guild_id)
    
    if user_id:
        query = query.where(ModerationCase.user_id == user_id)
    
    if resolved is not None:
        query = query.where(ModerationCase.resolved == resolved)
    
    query = query.order_by(ModerationCase.created_at.desc()).limit(limit)
    
    result = await db.execute(query)
    cases = result.scalars().all()
    
    return {
        "cases": [
            {
                "id": str(case.id),
                "user_id": case.user_id,
                "user_tag": case.user_tag,
                "action": case.action.value,
                "reason": case.reason,
                "moderator_tag": case.moderator_tag,
                "created_at": case.created_at.isoformat(),
                "resolved": case.resolved
            }
            for case in cases
        ]
    }
```

## Task 4: AutoMod Configuration UI

### web/templates/admin/guilds/tabs/automod.html

AutoMod configuration interface:

```html
<div class="row">
    <div class="col-12">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h3>Auto-Moderation Rules</h3>
            <button class="btn btn-primary" onclick="showAddRuleModal()">
                <svg xmlns="http://www.w3.org/2000/svg" class="icon" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none">
                    <path stroke="none" d="M0 0h24v24H0z" fill="none"/>
                    <line x1="12" y1="5" x2="12" y2="19" />
                    <line x1="5" y1="12" x2="19" y2="12" />
                </svg>
                Add Rule
            </button>
        </div>
        
        <!-- Rules List -->
        <div class="row row-cards">
            {% for rule in automod_rules %}
            <div class="col-12">
                <div class="card">
                    <div class="card-header">
                        <h3 class="card-title">
                            {% if rule.rule_type == "username_regex" %}
                                🔤 Username Filter
                            {% elif rule.rule_type == "message_rate" %}
                                ⏱️ Rate Limit
                            {% elif rule.rule_type == "file_extension" %}
                                📎 File Filter
                            {% endif %}
                        </h3>
                        <div class="card-actions">
                            <div class="form-check form-switch">
                                <input class="form-check-input" type="checkbox" 
                                       {% if rule.is_active %}checked{% endif %}
                                       onchange="toggleRule('{{ rule.id }}', this.checked)">
                            </div>
                            <div class="dropdown">
                                <a href="#" class="btn-action" data-bs-toggle="dropdown">
                                    <svg xmlns="http://www.w3.org/2000/svg" class="icon" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none">
                                        <path stroke="none" d="M0 0h24v24H0z" fill="none"/>
                                        <circle cx="12" cy="12" r="1" />
                                        <circle cx="12" cy="5" r="1" />
                                        <circle cx="12" cy="19" r="1" />
                                    </svg>
                                </a>
                                <div class="dropdown-menu dropdown-menu-end">
                                    <a class="dropdown-item" href="#" onclick="editRule('{{ rule.id }}')">
                                        Edit
                                    </a>
                                    <a class="dropdown-item text-danger" href="#" 
                                       onclick="deleteRule('{{ rule.id }}')"
                                       data-confirm="Delete this rule?">
                                        Delete
                                    </a>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="card-body">
                        <div class="row">
                            <div class="col-md-8">
                                {% if rule.rule_type == "username_regex" %}
                                    <div class="mb-2">
                                        <strong>Pattern:</strong> <code>{{ rule.config.pattern }}</code>
                                    </div>
                                    {% if rule.config.min_account_age_days %}
                                    <div class="mb-2">
                                        <strong>Min Account Age:</strong> {{ rule.config.min_account_age_days }} days
                                    </div>
                                    {% endif %}
                                    {% if rule.config.require_avatar %}
                                    <div class="mb-2">
                                        <strong>Require Avatar:</strong> Yes
                                    </div>
                                    {% endif %}
                                    
                                {% elif rule.rule_type == "message_rate" %}
                                    <div class="mb-2">
                                        <strong>Limit:</strong> {{ rule.config.max_messages }} messages 
                                        per {{ rule.config.timeframe_seconds }} seconds
                                    </div>
                                    {% if rule.config.max_duplicates %}
                                    <div class="mb-2">
                                        <strong>Max Duplicates:</strong> {{ rule.config.max_duplicates }}
                                    </div>
                                    {% endif %}
                                    {% if rule.config.max_channels %}
                                    <div class="mb-2">
                                        <strong>Max Channels:</strong> {{ rule.config.max_channels }}
                                    </div>
                                    {% endif %}
                                    
                                {% elif rule.rule_type == "file_extension" %}
                                    <div class="mb-2">
                                        <strong>Blocked Extensions:</strong>
                                        {% for ext in rule.config.blocked_extensions %}
                                            <span class="badge bg-red">.{{ ext }}</span>
                                        {% endfor %}
                                    </div>
                                {% endif %}
                            </div>
                            <div class="col-md-4 text-end">
                                <div class="mb-2">
                                    <strong>Action:</strong>
                                    <span class="badge bg-{{ rule.action|action_color }}">
                                        {{ rule.action|upper }}
                                    </span>
                                </div>
                                <div class="text-muted small">
                                    Priority: {{ rule.priority }}
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
        
        {% if not automod_rules %}
        <div class="empty">
            <p class="empty-title">No auto-moderation rules</p>
            <p class="empty-subtitle text-muted">
                Add rules to automatically moderate your server.
            </p>
        </div>
        {% endif %}
    </div>
</div>

<!-- Add Rule Modal -->
<div class="modal modal-blur fade" id="addRuleModal" tabindex="-1">
    <div class="modal-dialog modal-lg modal-dialog-centered">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title">Add Auto-Moderation Rule</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body">
                <!-- Rule Type Selection -->
                <div class="mb-3">
                    <label class="form-label">Rule Type</label>
                    <select id="ruleType" class="form-select" onchange="updateRuleForm()">
                        <option value="">Select rule type...</option>
                        <option value="username_regex">Username Filter</option>
                        <option value="message_rate">Message Rate Limit</option>
                        <option value="file_extension">File Extension Block</option>
                    </select>
                </div>
                
                <!-- Dynamic Rule Configuration -->
                <div id="ruleConfig" style="display: none;">
                    <!-- Username Regex Config -->
                    <div id="usernameConfig" style="display: none;">
                        <div class="mb-3">
                            <label class="form-label">Regex Pattern</label>
                            <input type="text" id="regexPattern" class="form-control" 
                                   placeholder="e.g., discord\.gg|bit\.ly">
                            <small class="form-hint">Case-insensitive regex pattern to match usernames</small>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">Minimum Account Age (days)</label>
                            <input type="number" id="minAccountAge" class="form-control" 
                                   min="0" value="0">
                            <small class="form-hint">0 = no age requirement</small>
                        </div>
                        <div class="mb-3">
                            <label class="form-check">
                                <input type="checkbox" id="requireAvatar" class="form-check-input">
                                <span class="form-check-label">Require custom avatar</span>
                            </label>
                        </div>
                    </div>
                    
                    <!-- Message Rate Config -->
                    <div id="messageRateConfig" style="display: none;">
                        <div class="row">
                            <div class="col-md-6">
                                <div class="mb-3">
                                    <label class="form-label">Max Messages</label>
                                    <input type="number" id="maxMessages" class="form-control" 
                                           min="1" value="10">
                                </div>
                            </div>
                            <div class="col-md-6">
                                <div class="mb-3">
                                    <label class="form-label">Time Window (seconds)</label>
                                    <input type="number" id="timeframeSeconds" class="form-control" 
                                           min="1" value="60">
                                </div>
                            </div>
                        </div>
                        <div class="row">
                            <div class="col-md-6">
                                <div class="mb-3">
                                    <label class="form-label">Max Duplicate Messages</label>
                                    <input type="number" id="maxDuplicates" class="form-control" 
                                           min="0" value="3">
                                    <small class="form-hint">0 = no duplicate checking</small>
                                </div>
                            </div>
                            <div class="col-md-6">
                                <div class="mb-3">
                                    <label class="form-label">Max Channels</label>
                                    <input type="number" id="maxChannels" class="form-control" 
                                           min="0" value="5">
                                    <small class="form-hint">0 = no channel spam checking</small>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <!-- File Extension Config -->
                    <div id="fileExtConfig" style="display: none;">
                        <div class="mb-3">
                            <label class="form-label">Blocked Extensions</label>
                            <input type="text" id="blockedExtensions" class="form-control" 
                                   placeholder="exe, scr, bat, cmd">
                            <small class="form-hint">Comma-separated list of file extensions (without dots)</small>
                        </div>
                    </div>
                    
                    <!-- Common Settings -->
                    <div class="mb-3">
                        <label class="form-label">Action</label>
                        <select id="ruleAction" class="form-select">
                            <option value="warn">Warn</option>
                            <option value="delete">Delete Message</option>
                            <option value="timeout">Timeout User</option>
                            <option value="kick">Kick User</option>
                            <option value="ban">Ban User</option>
                        </select>
                    </div>
                    
                    <div id="timeoutDuration" class="mb-3" style="display: none;">
                        <label class="form-label">Timeout Duration (minutes)</label>
                        <input type="number" id="timeoutMinutes" class="form-control" 
                               min="1" max="40320" value="10">
                    </div>
                    
                    <div class="mb-3">
                        <label class="form-label">Priority</label>
                        <input type="number" id="rulePriority" class="form-control" 
                               min="0" value="0">
                        <small class="form-hint">Lower numbers = higher priority</small>
                    </div>
                </div>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn me-auto" data-bs-dismiss="modal">Cancel</button>
                <button type="button" class="btn btn-primary" onclick="saveRule()">
                    Save Rule
                </button>
            </div>
        </div>
    </div>
</div>

<script>
function updateRuleForm() {
    const ruleType = document.getElementById('ruleType').value;
    document.getElementById('ruleConfig').style.display = ruleType ? 'block' : 'none';
    
    // Hide all configs
    document.getElementById('usernameConfig').style.display = 'none';
    document.getElementById('messageRateConfig').style.display = 'none';
    document.getElementById('fileExtConfig').style.display = 'none';
    
    // Show selected config
    if (ruleType === 'username_regex') {
        document.getElementById('usernameConfig').style.display = 'block';
    } else if (ruleType === 'message_rate') {
        document.getElementById('messageRateConfig').style.display = 'block';
    } else if (ruleType === 'file_extension') {
        document.getElementById('fileExtConfig').style.display = 'block';
    }
    
    // Show/hide timeout duration
    document.getElementById('ruleAction').addEventListener('change', (e) => {
        const showTimeout = e.target.value === 'timeout';
        document.getElementById('timeoutDuration').style.display = showTimeout ? 'block' : 'none';
    });
}

async function saveRule() {
    const ruleType = document.getElementById('ruleType').value;
    const action = document.getElementById('ruleAction').value;
    const priority = parseInt(document.getElementById('rulePriority').value);
    
    let config = {};
    
    // Build config based on rule type
    if (ruleType === 'username_regex') {
        config = {
            pattern: document.getElementById('regexPattern').value,
            min_account_age_days: parseInt(document.getElementById('minAccountAge').value),
            require_avatar: document.getElementById('requireAvatar').checked
        };
    } else if (ruleType === 'message_rate') {
        config = {
            max_messages: parseInt(document.getElementById('maxMessages').value),
            timeframe_seconds: parseInt(document.getElementById('timeframeSeconds').value),
            max_duplicates: parseInt(document.getElementById('maxDuplicates').value),
            max_channels: parseInt(document.getElementById('maxChannels').value)
        };
    } else if (ruleType === 'file_extension') {
        const extensions = document.getElementById('blockedExtensions').value
            .split(',')
            .map(ext => ext.trim().toLowerCase())
            .filter(ext => ext);
        config = { blocked_extensions: extensions };
    }
    
    // Add timeout duration if needed
    if (action === 'timeout') {
        config.timeout_duration = parseInt(document.getElementById('timeoutMinutes').value);
    }
    
    try {
        const response = await fetch(`/api/v1/guilds/{{ guild.id }}/automod/rules`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${API_KEY}`
            },
            body: JSON.stringify({
                rule_type: ruleType,
                config: config,
                action: action,
                priority: priority
            })
        });
        
        if (response.ok) {
            window.location.reload();
        } else {
            const error = await response.json();
            alert(error.detail || 'Failed to create rule');
        }
    } catch (error) {
        alert('Failed to create rule');
    }
}

async function toggleRule(ruleId, isActive) {
    try {
        await fetch(`/api/v1/guilds/{{ guild.id }}/automod/rules/${ruleId}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${API_KEY}`
            },
            body: JSON.stringify({ is_active: isActive })
        });
    } catch (error) {
        alert('Failed to update rule');
    }
}

async function deleteRule(ruleId) {
    if (!confirm('Delete this auto-moderation rule?')) return;
    
    try {
        const response = await fetch(`/api/v1/guilds/{{ guild.id }}/automod/rules/${ruleId}`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${API_KEY}`
            }
        });
        
        if (response.ok) {
            window.location.reload();
        }
    } catch (error) {
        alert('Failed to delete rule');
    }
}
</script>
```

## Task 5: Create Tests

### tests/test_automod.py

Test the auto-moderation system:

```python
import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, AsyncMock
import hikari

from bot.services.automod_service import AutoModService, MessageTracker
from shared.types import AutoModRuleType, ModerationAction

@pytest.fixture
def automod_service(mock_api):
    """Create automod service."""
    return AutoModService(mock_api)

@pytest.fixture
def message_tracker():
    """Create message tracker."""
    return MessageTracker()

def test_message_tracker_add_and_retrieve(message_tracker):
    """Test message tracking."""
    now = datetime.utcnow()
    
    # Add messages
    message_tracker.add_message("user1", "channel1", "Hello", now)
    message_tracker.add_message("user1", "channel2", "Hello", now)
    message_tracker.add_message("user1", "channel1", "World", now - timedelta(seconds=30))
    
    # Get recent messages
    recent = message_tracker.get_recent_messages("user1", 60)
    assert len(recent) == 3
    
    # Get only very recent
    very_recent = message_tracker.get_recent_messages("user1", 20)
    assert len(very_recent) == 2

def test_message_tracker_cleanup(message_tracker):
    """Test old message cleanup."""
    old_time = datetime.utcnow() - timedelta(minutes=15)
    recent_time = datetime.utcnow()
    
    # Add old and new messages
    message_tracker.add_message("user1", "channel1", "Old", old_time)
    message_tracker.add_message("user1", "channel1", "New", recent_time)
    
    # Force cleanup
    message_tracker.cleanup()
    
    # Only recent message should remain
    messages = message_tracker.get_recent_messages("user1", 3600)
    assert len(messages) == 1
    assert messages[0][2] == hashlib.md5(b"new").hexdigest()

@pytest.mark.asyncio
async def test_username_regex_check(automod_service):
    """Test username regex matching."""
    member = Mock(spec=hikari.Member)
    member.username = "JoinMyServer_discord.gg/abc123"
    member.created_at = datetime.utcnow() - timedelta(days=1)
    member.avatar_hash = None
    
    rules = [{
        "id": "rule1",
        "rule_type": AutoModRuleType.USERNAME_REGEX,
        "config": {
            "pattern": r"discord\.gg/\w+",
            "min_account_age_days": 7,
            "require_avatar": True
        },
        "action": ModerationAction.BAN
    }]
    
    result = await automod_service.check_username(member, rules)
    assert result is not None
    
    rule, reason = result
    assert rule["id"] == "rule1"
    assert "account is too new" in reason

@pytest.mark.asyncio
async def test_message_rate_limit(automod_service):
    """Test message rate limiting."""
    rules = [{
        "id": "rate1",
        "rule_type": AutoModRuleType.MESSAGE_RATE,
        "config": {
            "max_messages": 5,
            "timeframe_seconds": 10,
            "max_duplicates": 2
        },
        "action": ModerationAction.TIMEOUT
    }]
    
    # Spam messages
    for i in range(6):
        await automod_service.check_message_rate(
            "user1", "channel1", f"Message {i}", rules
        )
    
    # Should trigger rate limit
    result = await automod_service.check_message_rate(
        "user1", "channel1", "Another message", rules
    )
    
    assert result is not None
    rule, reason = result
    assert "Too many messages" in reason

@pytest.mark.asyncio
async def test_duplicate_detection(automod_service):
    """Test duplicate message detection."""
    rules = [{
        "id": "dup1",
        "rule_type": AutoModRuleType.MESSAGE_RATE,
        "config": {
            "max_messages": 10,
            "timeframe_seconds": 60,
            "max_duplicates": 2
        },
        "action": ModerationAction.DELETE
    }]
    
    # Send same message 3 times
    for _ in range(3):
        result = await automod_service.check_message_rate(
            "user1", "channel1", "Buy cheap gold now!", rules
        )
    
    # Third duplicate should trigger
    assert result is not None
    rule, reason = result
    assert "Duplicate message spam" in reason

@pytest.mark.asyncio
async def test_file_extension_blocking(automod_service):
    """Test file extension filtering."""
    rules = [{
        "id": "file1",
        "rule_type": AutoModRuleType.FILE_EXTENSION,
        "config": {
            "blocked_extensions": ["exe", "scr", "bat"]
        },
        "action": ModerationAction.DELETE
    }]
    
    # Create mock attachments
    attachments = [
        Mock(filename="document.pdf"),
        Mock(filename="virus.exe"),
        Mock(filename="image.png")
    ]
    
    result = await automod_service.check_file_extension(attachments, rules)
    
    assert result is not None
    rule, reason = result
    assert "Blocked file type: .exe" in reason

@pytest.mark.asyncio
async def test_action_execution(automod_service):
    """Test moderation action execution."""
    guild = Mock(spec=hikari.Guild)
    member = Mock(spec=hikari.Member)
    member.id = 123
    member.username = "TestUser"
    member.discriminator = "0001"
    
    # Mock API call
    automod_service.api.create_moderation_case = AsyncMock()
    
    # Test timeout action
    rule = {
        "config": {"timeout_duration": 10}
    }
    
    result = await automod_service.execute_action(
        guild, member, ModerationAction.TIMEOUT, "Test reason", rule
    )
    
    # Verify API was called
    automod_service.api.create_moderation_case.assert_called_once()
    
    # Verify member edit was called for timeout
    member.edit.assert_called_once()
    call_kwargs = member.edit.call_args.kwargs
    assert "communication_disabled_until" in call_kwargs
```

### tests/test_automod_api.py

Test automod API endpoints:

```python
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_create_username_rule(auth_api_client: AsyncClient):
    """Test creating username filter rule."""
    response = await auth_api_client.post(
        "/api/v1/guilds/123/automod/rules",
        json={
            "rule_type": "username_regex",
            "config": {
                "pattern": "discord\\.gg",
                "min_account_age_days": 7,
                "require_avatar": True
            },
            "action": "ban",
            "priority": 0
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["rule_type"] == "username_regex"
    assert data["is_active"] is True

@pytest.mark.asyncio
async def test_invalid_regex_pattern(auth_api_client: AsyncClient):
    """Test invalid regex pattern rejection."""
    response = await auth_api_client.post(
        "/api/v1/guilds/123/automod/rules",
        json={
            "rule_type": "username_regex",
            "config": {
                "pattern": "[invalid regex"
            },
            "action": "warn",
            "priority": 0
        }
    )
    
    assert response.status_code == 400
    assert "Invalid regex" in response.json()["detail"]

@pytest.mark.asyncio
async def test_update_rule_status(auth_api_client: AsyncClient, test_db):
    """Test enabling/disabling rules."""
    # Create rule
    create_resp = await auth_api_client.post(
        "/api/v1/guilds/123/automod/rules",
        json={
            "rule_type": "message_rate",
            "config": {
                "max_messages": 10,
                "timeframe_seconds": 60
            },
            "action": "timeout",
            "priority": 1
        }
    )
    rule_id = create_resp.json()["id"]
    
    # Disable rule
    update_resp = await auth_api_client.put(
        f"/api/v1/guilds/123/automod/rules/{rule_id}",
        json={"is_active": False}
    )
    
    assert update_resp.status_code == 200
    assert update_resp.json()["is_active"] is False

@pytest.mark.asyncio
async def test_moderation_case_creation(auth_api_client: AsyncClient):
    """Test creating moderation case."""
    response = await auth_api_client.post(
        "/api/v1/guilds/123/moderation/cases",
        json={
            "user_id": "456",
            "user_tag": "TestUser#0001",
            "moderator_id": "automod",
            "moderator_tag": "AutoMod",
            "action": "timeout",
            "reason": "[AutoMod] Message spam detected"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "timeout"
    assert data["moderator_id"] == "automod"

@pytest.mark.asyncio
async def test_get_moderation_cases(auth_api_client: AsyncClient, test_db):
    """Test fetching moderation cases."""
    # Create some cases
    for i in range(3):
        await auth_api_client.post(
            "/api/v1/guilds/123/moderation/cases",
            json={
                "user_id": f"user{i}",
                "user_tag": f"User{i}#000{i}",
                "moderator_id": "automod",
                "moderator_tag": "AutoMod",
                "action": "warn",
                "reason": f"Test warning {i}"
            }
        )
    
    # Get cases
    response = await auth_api_client.get(
        "/api/v1/guilds/123/moderation/cases?limit=10"
    )
    
    assert response.status_code == 200
    data = response.json()
    assert len(data["cases"]) == 3
    assert all(case["action"] == "warn" for case in data["cases"])
```

## Deliverables

1. **AutoMod Service Layer**
   - Message tracking with cleanup
   - Username regex matching
   - Rate limit detection
   - Duplicate spam detection
   - File extension checking
   - Action execution

2. **Discord Integration**
   - Member join checking
   - Message monitoring
   - Attachment filtering
   - Permission checks
   - Action execution

3. **API Endpoints**
   - Rule CRUD operations
   - Rule validation
   - Moderation case logging
   - Case querying

4. **Admin UI**
   - Rule builder interface
   - Visual rule configuration
   - Priority management
   - Enable/disable toggles
   - Testing capabilities

5. **Test Coverage**
   - Message tracking tests
   - Rule matching tests
   - Action execution tests
   - API validation tests

## Important Notes

1. Rules are cached for 5 minutes for performance
2. Message tracking uses in-memory storage with automatic cleanup
3. Bot ignores users with MANAGE_MESSAGES permission
4. All actions are logged as moderation cases
5. Regex patterns are validated before saving
6. Priority determines rule evaluation order

This auto-moderation system provides comprehensive protection while remaining configurable and performant.