# Session 5: Discord Bot Implementation

**Goal:** Create Discord bot with Hikari and Lightbulb using the service layer

## Task Description

Create Discord bot that uses the service layer for all business logic.

### Architecture
- Bot plugins are thin wrappers around services
- All Discord-specific formatting in plugins
- Services handle all business logic
- Embeds and views for rich interactions

## Deliverables

### 1. bot/bot.py - Bot setup and configuration:
```python
import hikari
import lightbulb
from shared.config import settings
from bot.client import APIClient
from bot.services.bytes_service import BytesService
from bot.services.squads_service import SquadsService

bot = lightbulb.BotApp(
    token=settings.DISCORD_TOKEN,
    intents=hikari.Intents.GUILDS | hikari.Intents.GUILD_MESSAGES,
    banner=None,
)

# Initialize services on startup
@bot.listen(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent):
    # Create API client
    bot.d.api_client = APIClient(
        base_url=settings.API_BASE_URL,
        bot_token=settings.BOT_API_TOKEN
    )
    
    # Create Redis client
    bot.d.redis = await create_redis_client()
    
    # Create services
    bot.d.bytes_service = BytesService(bot.d.api_client, bot.d.redis)
    bot.d.squads_service = SquadsService(bot.d.api_client, bot.d.redis)

# Load plugins
bot.load_extensions("bot.plugins.bytes", "bot.plugins.squads")

def run():
    bot.run()
```

### 2. bot/plugins/bytes.py - Bytes commands:
```python
import hikari
import lightbulb
from bot.utils.embeds import create_balance_embed, create_error_embed
from bot.utils.converters import parse_amount

plugin = lightbulb.Plugin("bytes", "Bytes economy commands")

@plugin.command
@lightbulb.command("bytes", "Bytes economy commands")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def bytes_group(ctx: lightbulb.Context) -> None:
    pass

@bytes_group.child
@lightbulb.command("balance", "Check your bytes balance")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def balance(ctx: lightbulb.SlashContext) -> None:
    service = ctx.bot.d.bytes_service
    
    # Get balance (may award daily)
    balance = await service.get_balance(str(ctx.guild_id), str(ctx.author.id))
    
    # Try to claim daily
    daily_result = await service.claim_daily(
        str(ctx.guild_id), 
        str(ctx.author.id),
        str(ctx.author)
    )
    
    # Create embed
    if daily_result.success:
        embed = create_balance_embed(
            balance=daily_result.balance,
            daily_earned=daily_result.earned,
            streak=daily_result.streak,
            multiplier=daily_result.multiplier
        )
        embed.title = "💰 Daily Bytes Claimed!"
        embed.color = hikari.Color(0x22c55e)  # Green for success
    else:
        embed = create_balance_embed(balance)
        embed.title = "💰 Your Bytes Balance"
    
    await ctx.respond(embed=embed)

@bytes_group.child
@lightbulb.option("reason", "Reason for sending bytes", required=False)
@lightbulb.option("amount", "Amount to send", type=int, min_value=1)
@lightbulb.option("user", "User to send bytes to", type=hikari.User)
@lightbulb.command("send", "Send bytes to another user")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def send(ctx: lightbulb.SlashContext) -> None:
    service = ctx.bot.d.bytes_service
    
    # Get options
    receiver = ctx.options.user
    amount = ctx.options.amount
    reason = ctx.options.reason
    
    # Validate receiver is in guild
    member = ctx.get_guild().get_member(receiver.id)
    if not member:
        embed = create_error_embed("That user is not in this server!")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    # Process transfer
    result = await service.transfer_bytes(
        str(ctx.guild_id),
        ctx.author,
        receiver,
        amount,
        reason
    )
    
    if not result.success:
        embed = create_error_embed(result.reason)
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    # Success embed
    embed = hikari.Embed(
        title="✅ Bytes Sent!",
        description=f"Successfully sent **{amount}** bytes to {receiver.mention}",
        color=hikari.Color(0x22c55e)
    )
    
    if reason:
        embed.add_field("Reason", reason, inline=False)
    
    await ctx.respond(embed=embed)

@bytes_group.child
@lightbulb.option("limit", "Number of users to show", type=int, default=10, min_value=1, max_value=25)
@lightbulb.command("leaderboard", "View the bytes leaderboard")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def leaderboard(ctx: lightbulb.SlashContext) -> None:
    service = ctx.bot.d.bytes_service
    
    # Get leaderboard
    entries = await service.get_leaderboard(str(ctx.guild_id), ctx.options.limit)
    
    if not entries:
        embed = create_error_embed("No leaderboard data yet!")
        await ctx.respond(embed=embed)
        return
    
    # Create embed
    embed = hikari.Embed(
        title="🏆 Bytes Leaderboard",
        color=hikari.Color(0x3b82f6)
    )
    
    # Build leaderboard text
    lines = []
    for entry in entries:
        # Try to get member
        member = ctx.get_guild().get_member(int(entry.user_id))
        name = member.display_name if member else f"User {entry.user_id}"
        
        # Medal for top 3
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(entry.rank, "🏅")
        
        lines.append(
            f"{medal} **{entry.rank}.** {name} - "
            f"**{entry.balance:,}** bytes (received: {entry.total_received:,})"
        )
    
    embed.description = "\n".join(lines)
    embed.set_footer(f"Showing top {len(entries)} users")
    
    await ctx.respond(embed=embed)

def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)

def unload(bot: lightbulb.BotApp) -> None:
    bot.remove_plugin(plugin)
```

### 3. bot/plugins/squads.py - Squad commands:
```python
from bot.views.squad_views import SquadSelectView, SquadConfirmView

@plugin.command
@lightbulb.command("squads", "Squad commands")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def squads_group(ctx: lightbulb.Context) -> None:
    pass

@squads_group.child
@lightbulb.command("list", "View available squads")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def list_squads(ctx: lightbulb.SlashContext) -> None:
    service = ctx.bot.d.squads_service
    
    squads = await service.list_squads(str(ctx.guild_id))
    
    if not squads:
        embed = create_error_embed("No squads have been created yet!")
        await ctx.respond(embed=embed)
        return
    
    # Get user's current squad
    user_squad = await service.get_user_squad(str(ctx.guild_id), str(ctx.author.id))
    
    embed = hikari.Embed(
        title="🏆 Available Squads",
        color=hikari.Color(0x3b82f6)
    )
    
    for squad in squads:
        # Get role color
        role = ctx.get_guild().get_role(int(squad.role_id))
        
        name = f"{'✅ ' if user_squad and user_squad.id == squad.id else ''}{squad.name}"
        value = squad.description or "No description"
        
        if user_squad and user_squad.id != squad.id:
            value += f"\n💰 Switch cost: **{squad.switch_cost}** bytes"
        
        embed.add_field(name, value, inline=False)
    
    await ctx.respond(embed=embed)

@squads_group.child
@lightbulb.command("join", "Join a squad")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def join_squad(ctx: lightbulb.SlashContext) -> None:
    service = ctx.bot.d.squads_service
    bytes_service = ctx.bot.d.bytes_service
    
    # Get available squads
    squads = await service.list_squads(str(ctx.guild_id))
    
    if not squads:
        embed = create_error_embed("No squads available to join!")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    # Get user's balance and current squad
    balance = await bytes_service.get_balance(str(ctx.guild_id), str(ctx.author.id))
    current_squad = await service.get_user_squad(str(ctx.guild_id), str(ctx.author.id))
    
    # Create squad selection view
    view = SquadSelectView(
        squads=squads,
        current_squad=current_squad,
        user_balance=balance.balance,
        timeout=60
    )
    
    embed = hikari.Embed(
        title="🏆 Select a Squad",
        description="Choose a squad to join. You have 60 seconds to decide.",
        color=hikari.Color(0x3b82f6)
    )
    
    embed.add_field("Your Balance", f"**{balance.balance:,}** bytes", inline=True)
    
    if current_squad:
        embed.add_field("Current Squad", current_squad.name, inline=True)
    
    message = await ctx.respond(embed=embed, components=view, flags=hikari.MessageFlag.EPHEMERAL)
    
    # Wait for selection
    await view.wait()
    
    if view.selected_squad_id is None:
        embed = create_error_embed("Squad selection timed out!")
        await message.edit(embed=embed, components=[])
        return
    
    # Process squad join
    result = await service.join_squad(
        str(ctx.guild_id),
        str(ctx.author.id),
        view.selected_squad_id,
        balance.balance
    )
    
    if not result.success:
        embed = create_error_embed(result.reason)
        await message.edit(embed=embed, components=[])
        return
    
    # Success!
    embed = hikari.Embed(
        title="✅ Squad Joined!",
        description=f"You've successfully joined **{result.squad.name}**!",
        color=hikari.Color(0x22c55e)
    )
    
    if result.cost > 0:
        embed.add_field("Cost", f"**{result.cost}** bytes", inline=True)
        new_balance = balance.balance - result.cost
        embed.add_field("New Balance", f"**{new_balance:,}** bytes", inline=True)
    
    await message.edit(embed=embed, components=[])
```

### 4. bot/views/squad_views.py - Interactive components:
```python
import hikari
from typing import List, Optional, UUID

class SquadSelectView(hikari.impl.ActionRowBuilder):
    def __init__(self, squads: List[Squad], current_squad: Optional[Squad], 
                 user_balance: int, timeout: int = 60):
        super().__init__()
        self.squads = squads
        self.current_squad = current_squad
        self.user_balance = user_balance
        self.selected_squad_id: Optional[UUID] = None
        
        # Create select menu
        options = []
        for squad in squads[:25]:  # Discord limit
            # Calculate cost
            cost = squad.switch_cost if current_squad and current_squad.id != squad.id else 0
            can_afford = user_balance >= cost
            
            option = hikari.SelectMenuOption(
                label=squad.name,
                value=str(squad.id),
                description=f"Cost: {cost} bytes" if cost > 0 else "Free to join!",
                emoji="✅" if current_squad and current_squad.id == squad.id else None,
                is_default=current_squad and current_squad.id == squad.id
            )
            
            if not can_afford and cost > 0:
                option.description = f"⚠️ Need {cost} bytes (you have {user_balance})"
            
            options.append(option)
        
        self.add_select_menu(
            custom_id="squad_select",
            options=options,
            placeholder="Choose a squad..."
        )
```

### 5. bot/utils/embeds.py - Embed builders:
```python
def create_balance_embed(
    balance: BytesBalance,
    daily_earned: Optional[int] = None,
    streak: Optional[int] = None,
    multiplier: Optional[int] = None
) -> hikari.Embed:
    embed = hikari.Embed(color=hikari.Color(0x3b82f6))
    
    # Main balance
    embed.add_field("Balance", f"**{balance.balance:,}** bytes", inline=True)
    embed.add_field("Total Received", f"{balance.total_received:,}", inline=True)
    embed.add_field("Total Sent", f"{balance.total_sent:,}", inline=True)
    
    # Daily info if provided
    if daily_earned is not None:
        embed.add_field(
            "Daily Earned", 
            f"**+{daily_earned}** bytes", 
            inline=True
        )
        
        if streak and streak > 1:
            streak_name = get_streak_name(streak)
            embed.add_field(
                "Streak", 
                f"🔥 **{streak}** days ({streak_name})", 
                inline=True
            )
            
            if multiplier and multiplier > 1:
                embed.add_field(
                    "Multiplier", 
                    f"**{multiplier}x**", 
                    inline=True
                )
    
    return embed

def get_streak_name(days: int) -> str:
    if days >= 60:
        return "LEGENDARY"
    elif days >= 30:
        return "EPIC"
    elif days >= 14:
        return "RARE"
    elif days >= 7:
        return "COMMON"
    return "BUILDING"
```

### 6. tests/bot/test_bot_integration.py - Integration tests:
```python
@pytest.mark.asyncio
class TestBotIntegration:
    async def test_bytes_balance_command(self, bot_app, mock_service):
        # Mock service response
        mock_service.get_balance.return_value = BytesBalance(
            guild_id="123",
            user_id="456",
            balance=100,
            total_received=150,
            total_sent=50,
            streak_count=5,
            last_daily=date.today()
        )
        
        mock_service.claim_daily.return_value = DailyClaimResult(
            success=False,
            reason="Already claimed"
        )
        
        # Simulate command
        ctx = create_mock_context(
            guild_id="123",
            author_id="456"
        )
        
        await balance(ctx)
        
        # Verify response
        assert ctx.respond.called
        embed = ctx.respond.call_args[1]["embed"]
        assert "100" in str(embed.fields[0].value)
```

## Quality Requirements
All bot code should:
- Use services for business logic
- Handle errors gracefully
- Provide rich Discord interactions
- Be testable through mocked services