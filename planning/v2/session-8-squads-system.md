# Session 8: Squads System Implementation

## Objective
Implement the team-based squad system allowing users to join Discord role-based teams, with bytes cost for switching squads. Focus on clear UI, role management, and preventing abuse.

## Prerequisites
- Completed Session 7 (bytes system exists)
- Understanding of Discord role management
- API endpoints for bytes transfers

## Task 1: Squad Service Layer

### bot/services/squad_service.py

Create the squad business logic service:

```python
from typing import Optional, Dict, Any, List
from datetime import datetime
import hikari
import structlog

from bot.config import BotConfig
from bot.errors import InsufficientBytesError, SquadError
from shared.constants import DEFAULT_SQUAD_SWITCH_COST, MIN_BYTES_FOR_SQUAD

logger = structlog.get_logger()

class SquadService:
    """Service for squad operations."""
    
    def __init__(self, api_client, bytes_service):
        self.api = api_client
        self.bytes_service = bytes_service
        self._squad_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._cache_time: Dict[str, datetime] = {}
    
    async def get_squads(self, guild_id: str) -> List[Dict[str, Any]]:
        """Get all active squads for a guild."""
        cache_key = f"squads:{guild_id}"
        now = datetime.utcnow()
        
        # Check cache
        if cache_key in self._squad_cache:
            cache_time = self._cache_time.get(cache_key)
            if cache_time and (now - cache_time).total_seconds() < 300:  # 5 min
                return self._squad_cache[cache_key]
        
        try:
            # Fetch from API
            squads = await self.api.get_squads(guild_id)
            
            # Filter active squads
            active_squads = [s for s in squads if s.get("is_active", True)]
            
            # Cache it
            self._squad_cache[cache_key] = active_squads
            self._cache_time[cache_key] = now
            
            return active_squads
            
        except Exception as e:
            logger.error(
                "Failed to fetch squads",
                guild_id=guild_id,
                error=str(e)
            )
            return []
    
    async def get_user_squad(
        self,
        guild_id: str,
        user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get user's current squad."""
        squads = await self.get_squads(guild_id)
        
        for squad in squads:
            members = squad.get("members", [])
            if user_id in [m["user_id"] for m in members]:
                return squad
        
        return None
    
    async def validate_squad_role(
        self,
        guild: hikari.Guild,
        squad: Dict[str, Any]
    ) -> Optional[hikari.Role]:
        """Validate squad role exists and is manageable."""
        role_id = int(squad["role_id"])
        
        # Check if role exists
        role = guild.get_role(role_id)
        if not role:
            logger.warning(
                "Squad role not found",
                guild_id=str(guild.id),
                squad_id=squad["id"],
                role_id=role_id
            )
            return None
        
        # Check if bot can manage the role
        bot_member = guild.get_my_member()
        if not bot_member:
            return None
        
        # Bot's highest role position
        bot_top_role = max(
            (guild.get_role(r) for r in bot_member.role_ids),
            key=lambda r: r.position
        )
        
        if role.position >= bot_top_role.position:
            logger.warning(
                "Cannot manage squad role - position too high",
                role=role.name,
                role_position=role.position,
                bot_position=bot_top_role.position
            )
            return None
        
        return role
    
    async def check_eligibility(
        self,
        guild_id: str,
        user_id: str,
        username: str
    ) -> Tuple[bool, str]:
        """Check if user is eligible for squad operations."""
        # Check bytes balance
        balance_data = await self.bytes_service.check_balance(
            guild_id, user_id, username
        )
        
        if balance_data["balance"] < MIN_BYTES_FOR_SQUAD:
            return False, f"You need at least **{MIN_BYTES_FOR_SQUAD}** bytes to join a squad."
        
        return True, ""
    
    async def calculate_switch_cost(
        self,
        guild_id: str,
        user_id: str,
        target_squad: Dict[str, Any]
    ) -> int:
        """Calculate cost to switch to a squad."""
        # Check if user is already in a squad
        current_squad = await self.get_user_squad(guild_id, user_id)
        
        if not current_squad:
            # First squad is free
            return 0
        
        if current_squad["id"] == target_squad["id"]:
            # Already in this squad
            return 0
        
        # Switching costs bytes
        return target_squad.get("switch_cost", DEFAULT_SQUAD_SWITCH_COST)
    
    async def join_squad(
        self,
        guild: hikari.Guild,
        member: hikari.Member,
        squad_id: str
    ) -> Dict[str, Any]:
        """Join a squad."""
        guild_id = str(guild.id)
        user_id = str(member.id)
        username = member.username
        
        # Get squad
        squads = await self.get_squads(guild_id)
        target_squad = next((s for s in squads if s["id"] == squad_id), None)
        
        if not target_squad:
            raise SquadError("Squad not found or inactive.")
        
        # Check eligibility
        eligible, reason = await self.check_eligibility(guild_id, user_id, username)
        if not eligible:
            raise SquadError(reason)
        
        # Validate role
        role = await self.validate_squad_role(guild, target_squad)
        if not role:
            raise SquadError("Squad role is invalid or cannot be managed.")
        
        # Calculate cost
        switch_cost = await self.calculate_switch_cost(guild_id, user_id, target_squad)
        
        # Check balance if switching
        if switch_cost > 0:
            balance_data = await self.bytes_service.check_balance(
                guild_id, user_id, username
            )
            
            if balance_data["balance"] < switch_cost:
                raise InsufficientBytesError(
                    current=balance_data["balance"],
                    required=switch_cost
                )
        
        # Get current squad for role removal
        current_squad = await self.get_user_squad(guild_id, user_id)
        
        try:
            # Execute squad change via API
            result = await self.api.join_squad(guild_id, user_id, squad_id)
            
            # Handle Discord roles
            if current_squad:
                # Remove old role
                old_role_id = int(current_squad["role_id"])
                if old_role_id in member.role_ids:
                    await member.remove_role(old_role_id, reason="Left squad")
            
            # Add new role
            await member.add_role(role, reason="Joined squad")
            
            # Deduct switch cost if applicable
            if switch_cost > 0:
                await self.bytes_service.transfer(
                    guild_id=guild_id,
                    giver_id=user_id,
                    giver_username=username,
                    receiver_id="system",
                    receiver_username="Squad System",
                    amount=switch_cost,
                    reason=f"Squad switch to {target_squad['name']}"
                )
            
            # Clear cache
            self.clear_cache(guild_id)
            
            logger.info(
                "User joined squad",
                guild_id=guild_id,
                user_id=user_id,
                squad_id=squad_id,
                cost=switch_cost
            )
            
            return {
                "squad": target_squad,
                "cost_paid": switch_cost,
                "previous_squad": current_squad
            }
            
        except Exception as e:
            logger.error(
                "Failed to join squad",
                guild_id=guild_id,
                user_id=user_id,
                squad_id=squad_id,
                error=str(e)
            )
            raise SquadError("Failed to join squad. Please try again.")
    
    async def leave_squad(
        self,
        guild: hikari.Guild,
        member: hikari.Member
    ) -> Optional[Dict[str, Any]]:
        """Leave current squad."""
        guild_id = str(guild.id)
        user_id = str(member.id)
        
        # Get current squad
        current_squad = await self.get_user_squad(guild_id, user_id)
        
        if not current_squad:
            raise SquadError("You're not in a squad.")
        
        try:
            # Leave via API
            await self.api.leave_squad(guild_id, user_id)
            
            # Remove Discord role
            role_id = int(current_squad["role_id"])
            if role_id in member.role_ids:
                await member.remove_role(role_id, reason="Left squad")
            
            # Clear cache
            self.clear_cache(guild_id)
            
            logger.info(
                "User left squad",
                guild_id=guild_id,
                user_id=user_id,
                squad_id=current_squad["id"]
            )
            
            return current_squad
            
        except Exception as e:
            logger.error(
                "Failed to leave squad",
                guild_id=guild_id,
                user_id=user_id,
                error=str(e)
            )
            raise SquadError("Failed to leave squad. Please try again.")
    
    def clear_cache(self, guild_id: str):
        """Clear squad cache for guild."""
        cache_key = f"squads:{guild_id}"
        self._squad_cache.pop(cache_key, None)
        self._cache_time.pop(cache_key, None)
```

## Task 2: Squad Plugin

### bot/plugins/squads.py

Create the squad commands plugin:

```python
import hikari
import lightbulb
from typing import Optional, List
import structlog

from bot.plugins.base import BasePlugin
from bot.services.squad_service import SquadService
from bot.services.bytes_service import BytesService
from bot.utils.embeds import EmbedBuilder
from bot.utils.checks import guild_only
from bot.errors import InsufficientBytesError, SquadError
from bot.views.squad_views import SquadSelectView, SquadConfirmView

logger = structlog.get_logger()

class SquadPlugin(BasePlugin):
    """Plugin for squad commands."""
    
    def __init__(self):
        super().__init__("squads", "Team-based squad system")
        self.service: Optional[SquadService] = None
    
    def load(self, bot: lightbulb.BotApp) -> None:
        """Load the plugin."""
        super().load(bot)
        
        # Initialize services
        bytes_service = BytesService(bot.api)
        self.service = SquadService(bot.api, bytes_service)
    
    async def handle_update(self, guild_id: str, update_data: Dict[str, Any]):
        """Handle squad updates from Redis."""
        # Clear cache when squads are updated
        self.service.clear_cache(guild_id)
        
        logger.info(
            "Squad cache cleared due to update",
            guild_id=guild_id
        )

squad_plugin = SquadPlugin()

@squad_plugin.command
@lightbulb.app_command_permissions(dm_enabled=False)
@lightbulb.command("squads", "Squad team commands", aliases=["squad"])
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def squad_group(ctx: lightbulb.SlashContext) -> None:
    """Squad command group."""
    pass

@squad_group.child
@lightbulb.command("list", "View all available squads")
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
async def list_squads(ctx: lightbulb.SlashContext) -> None:
    """List all available squads."""
    await ctx.respond(hikari.ResponseType.DEFERRED)
    
    try:
        # Get squads
        squads = await squad_plugin.service.get_squads(str(ctx.guild_id))
        
        if not squads:
            await squad_plugin.send_error(
                ctx,
                "No squads are available in this server."
            )
            return
        
        # Get user's current squad
        user_squad = await squad_plugin.service.get_user_squad(
            str(ctx.guild_id),
            str(ctx.author.id)
        )
        
        # Build embed
        embed = hikari.Embed(
            title="🏆 Available Squads",
            description="Join a squad to represent your team!",
            color=0x3B82F6,
            timestamp=datetime.utcnow()
        )
        
        for squad in squads:
            # Get role for color
            role = ctx.get_guild().get_role(int(squad["role_id"]))
            
            # Format field
            member_count = len(squad.get("members", []))
            is_current = user_squad and user_squad["id"] == squad["id"]
            
            field_name = squad["name"]
            if is_current:
                field_name += " ✅"
            
            field_value = f"{squad.get('description', 'No description')}\n"
            field_value += f"Members: **{member_count}**"
            
            if not is_current and squad.get("switch_cost", 0) > 0:
                field_value += f" • Cost: **{squad['switch_cost']}** bytes"
            
            embed.add_field(
                field_name,
                field_value,
                inline=True
            )
        
        if user_squad:
            embed.set_footer(f"You're currently in: {user_squad['name']}")
        else:
            embed.set_footer("You're not in a squad. Join one for free!")
        
        await ctx.respond(embed=embed)
        
    except Exception as e:
        logger.error("Failed to list squads", error=str(e))
        await squad_plugin.send_error(
            ctx,
            "Failed to fetch squads. Please try again."
        )

@squad_group.child
@lightbulb.command("join", "Join a squad")
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
async def join_squad(ctx: lightbulb.SlashContext) -> None:
    """Join a squad with interactive selection."""
    # Get squads
    squads = await squad_plugin.service.get_squads(str(ctx.guild_id))
    
    if not squads:
        await squad_plugin.send_error(
            ctx,
            "No squads are available in this server."
        )
        return
    
    # Check eligibility first
    eligible, reason = await squad_plugin.service.check_eligibility(
        str(ctx.guild_id),
        str(ctx.author.id),
        ctx.author.username
    )
    
    if not eligible:
        await squad_plugin.send_error(ctx, reason)
        return
    
    # Get current squad
    user_squad = await squad_plugin.service.get_user_squad(
        str(ctx.guild_id),
        str(ctx.author.id)
    )
    
    # Filter out current squad
    available_squads = [
        s for s in squads
        if not user_squad or s["id"] != user_squad["id"]
    ]
    
    if not available_squads:
        await squad_plugin.send_error(
            ctx,
            "You're already in the only available squad!"
        )
        return
    
    # Create selection view
    view = SquadSelectView(
        squads=available_squads,
        current_squad=user_squad,
        user=ctx.author,
        timeout=60
    )
    
    embed = hikari.Embed(
        title="🎯 Select a Squad",
        description="Choose which squad you want to join:",
        color=0x3B82F6
    )
    
    if user_squad:
        embed.add_field(
            "Current Squad",
            user_squad["name"],
            inline=False
        )
    
    resp = await ctx.respond(
        embed=embed,
        components=view,
        flags=hikari.MessageFlag.EPHEMERAL
    )
    
    # Wait for selection
    await view.start(await resp.message())
    await view.wait()
    
    if not view.selected_squad:
        return
    
    # Get switch cost
    switch_cost = await squad_plugin.service.calculate_switch_cost(
        str(ctx.guild_id),
        str(ctx.author.id),
        view.selected_squad
    )
    
    # If there's a cost, show confirmation
    if switch_cost > 0:
        confirm_view = SquadConfirmView(
            squad=view.selected_squad,
            cost=switch_cost,
            user=ctx.author,
            timeout=30
        )
        
        embed = hikari.Embed(
            title="💰 Confirm Squad Switch",
            description=(
                f"Switching to **{view.selected_squad['name']}** will cost "
                f"**{switch_cost}** bytes.\n\n"
                "Do you want to proceed?"
            ),
            color=0xF59E0B
        )
        
        await ctx.edit_last_response(
            embed=embed,
            components=confirm_view
        )
        
        await confirm_view.wait()
        
        if not confirm_view.confirmed:
            return
    
    # Join the squad
    try:
        result = await squad_plugin.service.join_squad(
            ctx.get_guild(),
            ctx.member,
            view.selected_squad["id"]
        )
        
        # Success embed
        embed = EmbedBuilder.success(
            "Squad Joined!",
            f"You've successfully joined **{result['squad']['name']}**!",
            fields=[
                ("Cost Paid", f"{result['cost_paid']} bytes" if result['cost_paid'] > 0 else "Free", True)
            ]
        )
        
        await ctx.edit_last_response(
            embed=embed,
            components=[]
        )
        
    except InsufficientBytesError as e:
        await ctx.edit_last_response(
            embed=EmbedBuilder.error(description=e.user_message),
            components=[]
        )
    except SquadError as e:
        await ctx.edit_last_response(
            embed=EmbedBuilder.error(description=str(e)),
            components=[]
        )
    except Exception as e:
        logger.error("Failed to join squad", error=str(e))
        await ctx.edit_last_response(
            embed=EmbedBuilder.error(
                description="Failed to join squad. Please try again."
            ),
            components=[]
        )

@squad_group.child
@lightbulb.command("leave", "Leave your current squad")
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
async def leave_squad(ctx: lightbulb.SlashContext) -> None:
    """Leave current squad."""
    try:
        # Check current squad
        user_squad = await squad_plugin.service.get_user_squad(
            str(ctx.guild_id),
            str(ctx.author.id)
        )
        
        if not user_squad:
            await squad_plugin.send_error(
                ctx,
                "You're not in a squad!",
                ephemeral=True
            )
            return
        
        # Show confirmation
        confirm_view = SquadConfirmView(
            squad=user_squad,
            cost=0,
            user=ctx.author,
            timeout=30,
            action="leave"
        )
        
        embed = hikari.Embed(
            title="🚪 Leave Squad?",
            description=(
                f"Are you sure you want to leave **{user_squad['name']}**?\n\n"
                "You can join another squad afterward."
            ),
            color=0xF59E0B
        )
        
        resp = await ctx.respond(
            embed=embed,
            components=confirm_view,
            flags=hikari.MessageFlag.EPHEMERAL
        )
        
        await confirm_view.wait()
        
        if not confirm_view.confirmed:
            await ctx.edit_last_response(
                embed=EmbedBuilder.info("Squad Leave Cancelled"),
                components=[]
            )
            return
        
        # Leave squad
        left_squad = await squad_plugin.service.leave_squad(
            ctx.get_guild(),
            ctx.member
        )
        
        embed = EmbedBuilder.success(
            "Left Squad",
            f"You've left **{left_squad['name']}**. You can join another squad anytime!"
        )
        
        await ctx.edit_last_response(
            embed=embed,
            components=[]
        )
        
    except SquadError as e:
        await squad_plugin.send_error(ctx, str(e), ephemeral=True)
    except Exception as e:
        logger.error("Failed to leave squad", error=str(e))
        await squad_plugin.send_error(
            ctx,
            "Failed to leave squad. Please try again.",
            ephemeral=True
        )

@squad_group.child
@lightbulb.option(
    "user",
    "User to check (defaults to yourself)",
    type=hikari.User,
    required=False
)
@lightbulb.command("info", "Check squad membership")
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
async def squad_info(ctx: lightbulb.SlashContext) -> None:
    """Check user's squad information."""
    user = ctx.options.user or ctx.author
    
    if user.is_bot:
        await squad_plugin.send_error(ctx, "Bots can't be in squads!")
        return
    
    await ctx.respond(hikari.ResponseType.DEFERRED)
    
    try:
        # Get user's squad
        user_squad = await squad_plugin.service.get_user_squad(
            str(ctx.guild_id),
            str(user.id)
        )
        
        if not user_squad:
            embed = EmbedBuilder.info(
                f"{user.username}'s Squad Status",
                f"{user.mention} is not in any squad.",
                thumbnail=user.display_avatar_url.url
            )
        else:
            # Get role for color
            role = ctx.get_guild().get_role(int(user_squad["role_id"]))
            color = role.color if role else 0x3B82F6
            
            embed = hikari.Embed(
                title=f"{user.username}'s Squad",
                color=color,
                timestamp=datetime.utcnow()
            )
            
            embed.set_thumbnail(user.display_avatar_url.url)
            
            embed.add_field(
                "Squad",
                f"**{user_squad['name']}**",
                inline=True
            )
            
            embed.add_field(
                "Members",
                f"{len(user_squad.get('members', []))} members",
                inline=True
            )
            
            if user_squad.get("description"):
                embed.add_field(
                    "Description",
                    user_squad["description"],
                    inline=False
                )
        
        await ctx.respond(embed=embed)
        
    except Exception as e:
        logger.error("Failed to get squad info", error=str(e))
        await squad_plugin.send_error(
            ctx,
            "Failed to fetch squad information."
        )

@squad_group.child
@lightbulb.option(
    "squad",
    "Squad name to list members for",
    type=str,
    required=True,
    autocomplete=True
)
@lightbulb.command("members", "List members of a squad")
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
async def squad_members(ctx: lightbulb.SlashContext) -> None:
    """List members of a specific squad."""
    squad_name = ctx.options.squad
    
    await ctx.respond(hikari.ResponseType.DEFERRED)
    
    try:
        # Find squad by name
        squads = await squad_plugin.service.get_squads(str(ctx.guild_id))
        squad = next(
            (s for s in squads if s["name"].lower() == squad_name.lower()),
            None
        )
        
        if not squad:
            await squad_plugin.send_error(
                ctx,
                f"Squad '{squad_name}' not found."
            )
            return
        
        # Get members
        members = squad.get("members", [])
        
        if not members:
            embed = EmbedBuilder.info(
                f"{squad['name']} Members",
                "This squad has no members yet."
            )
        else:
            # Get role for color
            role = ctx.get_guild().get_role(int(squad["role_id"]))
            color = role.color if role else 0x3B82F6
            
            embed = hikari.Embed(
                title=f"{squad['name']} Members",
                description=f"Total: {len(members)} members",
                color=color,
                timestamp=datetime.utcnow()
            )
            
            # Format member list
            member_list = []
            for i, member_data in enumerate(members[:25]):  # Limit to 25
                user = ctx.bot.cache.get_user(int(member_data["user_id"]))
                if user:
                    member_list.append(f"{i+1}. {user.mention}")
                else:
                    member_list.append(f"{i+1}. <@{member_data['user_id']}>")
            
            embed.add_field(
                "Members",
                "\n".join(member_list),
                inline=False
            )
            
            if len(members) > 25:
                embed.set_footer(f"Showing 25 of {len(members)} members")
        
        await ctx.respond(embed=embed)
        
    except Exception as e:
        logger.error("Failed to list squad members", error=str(e))
        await squad_plugin.send_error(
            ctx,
            "Failed to fetch squad members."
        )

# Autocomplete for squad names
@squad_members.autocomplete("squad")
async def squad_autocomplete(
    option: hikari.AutocompleteInteractionOption,
    interaction: hikari.AutocompleteInteraction
) -> List[str]:
    """Autocomplete squad names."""
    try:
        service = squad_plugin.service
        if not service:
            return []
        
        squads = await service.get_squads(str(interaction.guild_id))
        squad_names = [s["name"] for s in squads]
        
        # Filter by input
        value = option.value.lower() if option.value else ""
        filtered = [
            name for name in squad_names
            if value in name.lower()
        ]
        
        return filtered[:25]  # Discord limit
        
    except Exception:
        return []

def load(bot: lightbulb.BotApp) -> None:
    """Load the plugin."""
    bot.add_plugin(squad_plugin)

def unload(bot: lightbulb.BotApp) -> None:
    """Unload the plugin."""
    bot.remove_plugin(squad_plugin)
```

## Task 3: Squad Views

### bot/views/squad_views.py

Create interactive views for squad operations:

```python
import hikari
import asyncio
from typing import Optional, List, Dict, Any
import structlog

logger = structlog.get_logger()

class SquadSelectView(hikari.api.InteractionResponseBuilder):
    """View for selecting a squad to join."""
    
    def __init__(
        self,
        squads: List[Dict[str, Any]],
        current_squad: Optional[Dict[str, Any]],
        user: hikari.User,
        *,
        timeout: float = 60.0
    ):
        self.squads = squads
        self.current_squad = current_squad
        self.user = user
        self.timeout = timeout
        self.selected_squad: Optional[Dict[str, Any]] = None
        self._message: Optional[hikari.Message] = None
        self._task: Optional[asyncio.Task] = None
    
    def build(self) -> List[hikari.api.MessageActionRowBuilder]:
        """Build the select menu."""
        rows = []
        
        # Create select menu
        row = hikari.api.MessageActionRowBuilder()
        select = row.add_text_menu("squad_select")
        select.set_placeholder("Choose a squad...")
        
        for squad in self.squads[:25]:  # Discord limit
            # Calculate cost
            cost = 0
            if self.current_squad:
                cost = squad.get("switch_cost", 50)
            
            description = f"{len(squad.get('members', []))} members"
            if cost > 0:
                description += f" • {cost} bytes"
            
            select.add_option(
                squad["name"],
                squad["id"],
                description=description
            )
        
        rows.append(row)
        
        # Add cancel button
        button_row = hikari.api.MessageActionRowBuilder()
        button_row.add_interactive_button(
            hikari.ButtonStyle.SECONDARY,
            "cancel_squad",
            label="Cancel"
        )
        rows.append(button_row)
        
        return rows
    
    async def start(self, message: hikari.Message) -> None:
        """Start listening for interactions."""
        self._message = message
        self._task = asyncio.create_task(self._wait_for_interaction())
    
    async def wait(self) -> None:
        """Wait for the view to finish."""
        if self._task:
            await self._task
    
    async def _wait_for_interaction(self) -> None:
        """Wait for menu selection."""
        try:
            with self._message.app.stream(
                hikari.InteractionCreateEvent,
                timeout=self.timeout
            ).filter(
                lambda e: (
                    isinstance(e.interaction, hikari.ComponentInteraction) and
                    e.interaction.message.id == self._message.id and
                    e.interaction.user.id == self.user.id
                )
            ) as stream:
                async for event in stream:
                    interaction = event.interaction
                    
                    if interaction.custom_id == "squad_select":
                        # Get selected squad
                        squad_id = interaction.values[0]
                        self.selected_squad = next(
                            (s for s in self.squads if s["id"] == squad_id),
                            None
                        )
                        
                        # Update message
                        if self.selected_squad:
                            await interaction.create_initial_response(
                                hikari.ResponseType.MESSAGE_UPDATE,
                                content=f"Selected: **{self.selected_squad['name']}**",
                                embed=None,
                                components=[]
                            )
                        return
                    
                    elif interaction.custom_id == "cancel_squad":
                        await interaction.create_initial_response(
                            hikari.ResponseType.MESSAGE_UPDATE,
                            content="Squad selection cancelled.",
                            embed=None,
                            components=[]
                        )
                        return
                        
        except asyncio.TimeoutError:
            # Timeout
            try:
                await self._message.edit(
                    content="Squad selection timed out.",
                    embed=None,
                    components=[]
                )
            except:
                pass

class SquadConfirmView(hikari.api.InteractionResponseBuilder):
    """Confirmation view for squad operations."""
    
    def __init__(
        self,
        squad: Dict[str, Any],
        cost: int,
        user: hikari.User,
        *,
        timeout: float = 30.0,
        action: str = "join"
    ):
        self.squad = squad
        self.cost = cost
        self.user = user
        self.timeout = timeout
        self.action = action
        self.confirmed = False
        self._task: Optional[asyncio.Task] = None
    
    def build(self) -> hikari.api.MessageActionRowBuilder:
        """Build confirmation buttons."""
        row = hikari.api.MessageActionRowBuilder()
        
        # Confirm button
        confirm_label = "Confirm" if self.action == "join" else "Leave Squad"
        row.add_interactive_button(
            hikari.ButtonStyle.SUCCESS if self.action == "join" else hikari.ButtonStyle.DANGER,
            "confirm_squad",
            label=confirm_label
        )
        
        # Cancel button
        row.add_interactive_button(
            hikari.ButtonStyle.SECONDARY,
            "cancel_squad",
            label="Cancel"
        )
        
        return row
    
    async def wait(self) -> None:
        """Wait for confirmation."""
        self._task = asyncio.create_task(self._wait_for_interaction())
        await self._task
    
    async def _wait_for_interaction(self) -> None:
        """Wait for button press."""
        try:
            # Implementation similar to bytes transfer view
            # but adapted for squad confirmation
            pass
        except asyncio.TimeoutError:
            self.confirmed = False
```

## Task 4: Squad API Endpoints

### web/api/routers/squads.py

Create API endpoints for squad management:

```python
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from datetime import datetime
from sqlalchemy import select, delete, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from web.api.dependencies import CurrentAPIKey, DatabaseSession
from web.api.schemas import (
    SquadResponse,
    SquadCreateRequest,
    SquadUpdateRequest,
    SquadJoinRequest,
    SquadLeaveRequest,
    SquadMemberResponse
)
from web.models.squads import Squad, SquadMembership
import structlog

logger = structlog.get_logger()
router = APIRouter()

@router.get("/guilds/{guild_id}/squads", response_model=List[SquadResponse])
async def get_guild_squads(
    guild_id: str,
    include_inactive: bool = False,
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> List[SquadResponse]:
    """Get all squads for a guild."""
    query = select(Squad).where(Squad.guild_id == guild_id)
    
    if not include_inactive:
        query = query.where(Squad.is_active == True)
    
    # Include member count
    query = query.options(selectinload(Squad.members))
    
    result = await db.execute(query.order_by(Squad.name))
    squads = result.scalars().all()
    
    return [
        SquadResponse(
            id=str(squad.id),
            guild_id=squad.guild_id,
            role_id=squad.role_id,
            name=squad.name,
            description=squad.description,
            switch_cost=squad.switch_cost,
            is_active=squad.is_active,
            members=[
                SquadMemberResponse(
                    user_id=member.user_id,
                    joined_at=member.created_at.isoformat()
                )
                for member in squad.members
            ]
        )
        for squad in squads
    ]

@router.post("/guilds/{guild_id}/squads", response_model=SquadResponse)
async def create_squad(
    guild_id: str,
    request: SquadCreateRequest,
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> SquadResponse:
    """Create a new squad."""
    # Check if role is already used
    existing = await db.execute(
        select(Squad).where(
            Squad.guild_id == guild_id,
            Squad.role_id == request.role_id
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "This role is already assigned to a squad")
    
    # Create squad
    squad = Squad(
        guild_id=guild_id,
        role_id=request.role_id,
        name=request.name,
        description=request.description,
        switch_cost=request.switch_cost,
        is_active=True
    )
    
    db.add(squad)
    await db.commit()
    await db.refresh(squad)
    
    logger.info(
        "Squad created",
        guild_id=guild_id,
        squad_id=str(squad.id),
        name=squad.name
    )
    
    return SquadResponse(
        id=str(squad.id),
        guild_id=squad.guild_id,
        role_id=squad.role_id,
        name=squad.name,
        description=squad.description,
        switch_cost=squad.switch_cost,
        is_active=squad.is_active,
        members=[]
    )

@router.post("/guilds/{guild_id}/squads/{squad_id}/join")
async def join_squad(
    guild_id: str,
    squad_id: str,
    request: SquadJoinRequest,
    api_key: CurrentAPIKey,
    db: DatabaseSession
):
    """Join a squad."""
    # Get squad
    result = await db.execute(
        select(Squad).where(
            Squad.id == squad_id,
            Squad.guild_id == guild_id,
            Squad.is_active == True
        )
    )
    squad = result.scalar_one_or_none()
    
    if not squad:
        raise HTTPException(404, "Squad not found or inactive")
    
    # Check if already in this squad
    existing = await db.execute(
        select(SquadMembership).where(
            SquadMembership.guild_id == guild_id,
            SquadMembership.user_id == request.user_id,
            SquadMembership.squad_id == squad.id
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Already in this squad")
    
    # Leave current squad if any
    await db.execute(
        delete(SquadMembership).where(
            SquadMembership.guild_id == guild_id,
            SquadMembership.user_id == request.user_id
        )
    )
    
    # Join new squad
    membership = SquadMembership(
        guild_id=guild_id,
        user_id=request.user_id,
        squad_id=squad.id
    )
    
    db.add(membership)
    await db.commit()
    
    logger.info(
        "User joined squad",
        guild_id=guild_id,
        user_id=request.user_id,
        squad_id=str(squad.id)
    )
    
    return {"status": "joined", "squad_id": str(squad.id)}

@router.post("/guilds/{guild_id}/squads/leave")
async def leave_squad(
    guild_id: str,
    request: SquadLeaveRequest,
    api_key: CurrentAPIKey,
    db: DatabaseSession
):
    """Leave current squad."""
    # Remove membership
    result = await db.execute(
        delete(SquadMembership).where(
            SquadMembership.guild_id == guild_id,
            SquadMembership.user_id == request.user_id
        ).returning(SquadMembership.squad_id)
    )
    
    deleted = result.scalar_one_or_none()
    if not deleted:
        raise HTTPException(400, "User not in any squad")
    
    await db.commit()
    
    logger.info(
        "User left squad",
        guild_id=guild_id,
        user_id=request.user_id,
        squad_id=str(deleted)
    )
    
    return {"status": "left", "previous_squad_id": str(deleted)}

@router.put("/guilds/{guild_id}/squads/{squad_id}")
async def update_squad(
    guild_id: str,
    squad_id: str,
    request: SquadUpdateRequest,
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> SquadResponse:
    """Update squad settings."""
    # Get squad
    result = await db.execute(
        select(Squad)
        .where(Squad.id == squad_id, Squad.guild_id == guild_id)
        .options(selectinload(Squad.members))
    )
    squad = result.scalar_one_or_none()
    
    if not squad:
        raise HTTPException(404, "Squad not found")
    
    # Update fields
    if request.name is not None:
        squad.name = request.name
    if request.description is not None:
        squad.description = request.description
    if request.switch_cost is not None:
        squad.switch_cost = request.switch_cost
    if request.is_active is not None:
        squad.is_active = request.is_active
    
    await db.commit()
    await db.refresh(squad)
    
    logger.info(
        "Squad updated",
        guild_id=guild_id,
        squad_id=str(squad.id)
    )
    
    return SquadResponse(
        id=str(squad.id),
        guild_id=squad.guild_id,
        role_id=squad.role_id,
        name=squad.name,
        description=squad.description,
        switch_cost=squad.switch_cost,
        is_active=squad.is_active,
        members=[
            SquadMemberResponse(
                user_id=member.user_id,
                joined_at=member.created_at.isoformat()
            )
            for member in squad.members
        ]
    )
```

## Task 5: Squad Management UI

### web/templates/admin/guilds/tabs/squads.html

Squad management interface:

```html
<div class="row">
    <div class="col-12">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h3>Squad Management</h3>
            <button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#createSquadModal">
                <svg xmlns="http://www.w3.org/2000/svg" class="icon" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none">
                    <path stroke="none" d="M0 0h24v24H0z" fill="none"/>
                    <line x1="12" y1="5" x2="12" y2="19" />
                    <line x1="5" y1="12" x2="19" y2="12" />
                </svg>
                Create Squad
            </button>
        </div>
        
        {% if squads %}
        <div class="table-responsive">
            <table class="table table-vcenter card-table">
                <thead>
                    <tr>
                        <th>Squad</th>
                        <th>Role</th>
                        <th>Members</th>
                        <th>Switch Cost</th>
                        <th>Status</th>
                        <th class="w-1"></th>
                    </tr>
                </thead>
                <tbody>
                    {% for squad in squads %}
                    <tr>
                        <td>
                            <div>
                                <div class="font-weight-medium">{{ squad.name }}</div>
                                <div class="text-muted small">{{ squad.description or "No description" }}</div>
                            </div>
                        </td>
                        <td>
                            {% set role = roles|selectattr("id", "equalto", squad.role_id)|first %}
                            {% if role %}
                            <span class="badge" style="background-color: #{{ '%06x' % role.color }}">
                                {{ role.name }}
                            </span>
                            {% else %}
                            <span class="text-muted">Role not found</span>
                            {% endif %}
                        </td>
                        <td>{{ squad.members|length }}</td>
                        <td>{{ squad.switch_cost }} bytes</td>
                        <td>
                            {% if squad.is_active %}
                            <span class="badge bg-green">Active</span>
                            {% else %}
                            <span class="badge bg-red">Inactive</span>
                            {% endif %}
                        </td>
                        <td>
                            <div class="btn-list flex-nowrap">
                                <button class="btn btn-sm" onclick="editSquad('{{ squad.id }}')">
                                    Edit
                                </button>
                                <button class="btn btn-sm btn-danger" 
                                        onclick="deleteSquad('{{ squad.id }}')"
                                        data-confirm="Delete this squad? Members will be removed.">
                                    Delete
                                </button>
                            </div>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <div class="empty">
            <p class="empty-title">No squads configured</p>
            <p class="empty-subtitle text-muted">
                Create squads to allow users to join teams.
            </p>
            <div class="empty-action">
                <button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#createSquadModal">
                    Create first squad
                </button>
            </div>
        </div>
        {% endif %}
    </div>
</div>

<!-- Create Squad Modal -->
<div class="modal modal-blur fade" id="createSquadModal" tabindex="-1">
    <div class="modal-dialog modal-dialog-centered">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title">Create Squad</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <form id="createSquadForm">
                <div class="modal-body">
                    <div class="mb-3">
                        <label class="form-label">Squad Name</label>
                        <input type="text" name="name" class="form-control" required maxlength="100">
                    </div>
                    
                    <div class="mb-3">
                        <label class="form-label">Description</label>
                        <textarea name="description" class="form-control" rows="2" maxlength="500"></textarea>
                    </div>
                    
                    <div class="mb-3">
                        <label class="form-label">Discord Role</label>
                        <select name="role_id" class="form-select" required>
                            <option value="">Select a role...</option>
                            {% for role in roles %}
                            {% if not role.managed and role.name != "@everyone" %}
                            <option value="{{ role.id }}" style="color: #{{ '%06x' % role.color }}">
                                {{ role.name }}
                            </option>
                            {% endif %}
                            {% endfor %}
                        </select>
                        <small class="form-hint">Users will receive this role when joining the squad</small>
                    </div>
                    
                    <div class="mb-3">
                        <label class="form-label">Switch Cost (bytes)</label>
                        <input type="number" name="switch_cost" class="form-control" value="50" min="0" required>
                        <small class="form-hint">Cost to switch from another squad (0 for free)</small>
                    </div>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn me-auto" data-bs-dismiss="modal">Cancel</button>
                    <button type="submit" class="btn btn-primary">Create Squad</button>
                </div>
            </form>
        </div>
    </div>
</div>

<script>
// Squad management functions
document.getElementById('createSquadForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    
    const formData = new FormData(e.target);
    const data = Object.fromEntries(formData);
    
    try {
        const response = await fetch(`/api/v1/guilds/{{ guild.id }}/squads`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${API_KEY}`
            },
            body: JSON.stringify(data)
        });
        
        if (response.ok) {
            window.location.reload();
        } else {
            const error = await response.json();
            alert(error.detail || 'Failed to create squad');
        }
    } catch (error) {
        alert('Failed to create squad');
    }
});

async function deleteSquad(squadId) {
    if (!confirm('Delete this squad? All members will be removed.')) {
        return;
    }
    
    try {
        const response = await fetch(`/api/v1/guilds/{{ guild.id }}/squads/${squadId}`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${API_KEY}`
            }
        });
        
        if (response.ok) {
            window.location.reload();
        } else {
            alert('Failed to delete squad');
        }
    } catch (error) {
        alert('Failed to delete squad');
    }
}
</script>
```

## Task 6: Create Tests

### tests/test_squad_system.py

Test squad functionality:

```python
import pytest
from unittest.mock import Mock, AsyncMock
import hikari

from bot.services.squad_service import SquadService
from bot.errors import InsufficientBytesError, SquadError

@pytest.fixture
def mock_bytes_service():
    """Mock bytes service."""
    service = AsyncMock()
    service.check_balance.return_value = {
        "balance": 100,
        "total_received": 200,
        "total_sent": 100
    }
    return service

@pytest.fixture
def squad_service(mock_api, mock_bytes_service):
    """Create squad service."""
    return SquadService(mock_api, mock_bytes_service)

@pytest.mark.asyncio
async def test_calculate_switch_cost(squad_service):
    """Test switch cost calculation."""
    # First squad is free
    cost = await squad_service.calculate_switch_cost(
        "guild1", "user1", {"id": "squad1", "switch_cost": 50}
    )
    assert cost == 0
    
    # Mock user already in squad
    squad_service.get_user_squad = AsyncMock(
        return_value={"id": "squad2", "name": "Old Squad"}
    )
    
    # Switching costs bytes
    cost = await squad_service.calculate_switch_cost(
        "guild1", "user1", {"id": "squad1", "switch_cost": 75}
    )
    assert cost == 75
    
    # Same squad costs nothing
    cost = await squad_service.calculate_switch_cost(
        "guild1", "user1", {"id": "squad2", "switch_cost": 50}
    )
    assert cost == 0

@pytest.mark.asyncio
async def test_eligibility_check(squad_service, mock_bytes_service):
    """Test squad eligibility checking."""
    # Sufficient bytes
    eligible, reason = await squad_service.check_eligibility(
        "guild1", "user1", "TestUser"
    )
    assert eligible is True
    assert reason == ""
    
    # Insufficient bytes
    mock_bytes_service.check_balance.return_value = {"balance": 5}
    eligible, reason = await squad_service.check_eligibility(
        "guild1", "user1", "TestUser"
    )
    assert eligible is False
    assert "at least" in reason

@pytest.mark.asyncio
async def test_join_squad_validation(squad_service):
    """Test squad join validation."""
    guild = Mock(spec=hikari.Guild)
    member = Mock(spec=hikari.Member)
    
    # Squad not found
    squad_service.get_squads = AsyncMock(return_value=[])
    
    with pytest.raises(SquadError, match="Squad not found"):
        await squad_service.join_squad(guild, member, "invalid_id")

@pytest.mark.asyncio
async def test_role_validation(squad_service):
    """Test Discord role validation."""
    guild = Mock(spec=hikari.Guild)
    
    # Role doesn't exist
    guild.get_role.return_value = None
    squad = {"id": "squad1", "role_id": "123"}
    
    role = await squad_service.validate_squad_role(guild, squad)
    assert role is None
    
    # Role position too high
    bot_role = Mock(position=5)
    squad_role = Mock(position=10)
    
    guild.get_role.return_value = squad_role
    guild.get_my_member.return_value = Mock(role_ids=[1])
    guild.get_role = Mock(side_effect=lambda r: bot_role if r == 1 else squad_role)
    
    role = await squad_service.validate_squad_role(guild, squad)
    assert role is None
```

### tests/test_squad_api.py

Test squad API endpoints:

```python
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_create_squad(auth_api_client: AsyncClient, test_db):
    """Test creating a squad."""
    response = await auth_api_client.post(
        "/api/v1/guilds/123/squads",
        json={
            "role_id": "456",
            "name": "Test Squad",
            "description": "A test squad",
            "switch_cost": 50
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Test Squad"
    assert data["switch_cost"] == 50
    assert data["is_active"] is True

@pytest.mark.asyncio
async def test_duplicate_role_error(auth_api_client: AsyncClient, test_db):
    """Test creating squad with duplicate role."""
    # Create first squad
    await auth_api_client.post(
        "/api/v1/guilds/123/squads",
        json={
            "role_id": "456",
            "name": "Squad 1",
            "switch_cost": 50
        }
    )
    
    # Try to create second with same role
    response = await auth_api_client.post(
        "/api/v1/guilds/123/squads",
        json={
            "role_id": "456",
            "name": "Squad 2",
            "switch_cost": 50
        }
    )
    
    assert response.status_code == 400
    assert "already assigned" in response.json()["detail"]

@pytest.mark.asyncio
async def test_join_leave_squad(auth_api_client: AsyncClient, test_db):
    """Test joining and leaving squads."""
    # Create squad
    create_response = await auth_api_client.post(
        "/api/v1/guilds/123/squads",
        json={
            "role_id": "456",
            "name": "Test Squad",
            "switch_cost": 0
        }
    )
    squad_id = create_response.json()["id"]
    
    # Join squad
    join_response = await auth_api_client.post(
        f"/api/v1/guilds/123/squads/{squad_id}/join",
        json={"user_id": "789"}
    )
    
    assert join_response.status_code == 200
    assert join_response.json()["status"] == "joined"
    
    # Leave squad
    leave_response = await auth_api_client.post(
        "/api/v1/guilds/123/squads/leave",
        json={"user_id": "789"}
    )
    
    assert leave_response.status_code == 200
    assert leave_response.json()["status"] == "left"

@pytest.mark.asyncio
async def test_squad_members_list(auth_api_client: AsyncClient, test_db):
    """Test listing squad members."""
    # Create squad and add members
    create_response = await auth_api_client.post(
        "/api/v1/guilds/123/squads",
        json={
            "role_id": "456",
            "name": "Popular Squad",
            "switch_cost": 0
        }
    )
    squad_id = create_response.json()["id"]
    
    # Add multiple members
    for user_id in ["101", "102", "103"]:
        await auth_api_client.post(
            f"/api/v1/guilds/123/squads/{squad_id}/join",
            json={"user_id": user_id}
        )
    
    # Get squads with members
    response = await auth_api_client.get("/api/v1/guilds/123/squads")
    
    assert response.status_code == 200
    squads = response.json()
    squad = next(s for s in squads if s["id"] == squad_id)
    assert len(squad["members"]) == 3
```

## Deliverables

1. **Squad Service Layer**
   - Squad fetching with caching
   - User squad lookup
   - Role validation
   - Eligibility checking
   - Cost calculation
   - Join/leave operations

2. **Discord Commands**
   - `/squads list` - View all squads
   - `/squads join` - Interactive join
   - `/squads leave` - Leave current squad
   - `/squads info` - Check membership
   - `/squads members` - List squad members

3. **Interactive Views**
   - Squad selection menu
   - Cost confirmation dialog
   - Leave confirmation

4. **API Endpoints**
   - Squad CRUD operations
   - Join/leave endpoints
   - Member listing
   - Configuration updates

5. **Admin UI**
   - Squad creation/editing
   - Role assignment
   - Cost configuration
   - Member management

6. **Test Coverage**
   - Service logic tests
   - API endpoint tests
   - Role validation tests
   - Cost calculation tests

## Important Notes

1. First squad join is always free
2. Switching squads costs bytes (configurable)
3. Discord role management requires proper permissions
4. Members can only be in one squad per guild
5. Squad changes are atomic with role updates
6. 5-minute cache on squad data for performance

This squad system creates team identity and engagement through Discord roles while preventing rapid switching through economic barriers.