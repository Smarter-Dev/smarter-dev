# Session 7.3: Leaderboard and Display System - Bytes Economy Rankings

## Overview
Implement leaderboard storage and display system for showcasing top users by bytes balance, providing competitive engagement and community recognition.

## Key Components
- Efficient leaderboard data retrieval
- Paginated leaderboard display
- Real-time ranking calculations
- User position lookup
- Guild-specific leaderboards

## Implementation Details

### Leaderboard Service Method
Core service for retrieving top users by balance:

```python
async def get_leaderboard(
    self,
    guild_id: str,
    limit: int = 10
) -> List[Dict[str, Any]]:
    """Get guild leaderboard."""
    try:
        return await self.api.get_bytes_leaderboard(guild_id, limit)
    except Exception as e:
        logger.error(
            "Failed to get leaderboard",
            guild_id=guild_id,
            error=str(e)
        )
        return []
```

### Discord Leaderboard Command
User-friendly command for viewing top users:

```python
@bytes_group.child
@lightbulb.option(
    "limit",
    "Number of users to show",
    type=int,
    required=False,
    default=10,
    min_value=5,
    max_value=25
)
@lightbulb.command("leaderboard", "View the bytes leaderboard", aliases=["top", "lb"])
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
async def leaderboard_command(ctx: lightbulb.SlashContext) -> None:
    """View bytes leaderboard."""
    await ctx.respond(hikari.ResponseType.DEFERRED)
    
    limit = ctx.options.limit
    
    try:
        # Get leaderboard
        leaderboard = await bytes_plugin.service.get_leaderboard(
            str(ctx.guild_id),
            limit
        )
        
        if not leaderboard:
            await bytes_plugin.send_error(
                ctx,
                "No users with bytes found in this server!"
            )
            return
        
        # Format entries
        entries = []
        for i, entry in enumerate(leaderboard, 1):
            # Try to get user from cache
            user = ctx.bot.cache.get_user(int(entry["user_id"]))
            username = user.username if user else f"User {entry['user_id']}"
            
            entries.append((username, entry["balance"], i))
        
        # Create embed
        embed = EmbedBuilder.leaderboard(
            f"Top {len(entries)} Richest Users",
            entries,
            footer=f"Requested by {ctx.author.username}"
        )
        
        await ctx.respond(embed=embed)
        
    except Exception as e:
        logger.error("Leaderboard failed", error=str(e))
        await bytes_plugin.send_error(
            ctx,
            "Failed to fetch leaderboard. Please try again later."
        )
```

### API Leaderboard Endpoint
Backend API for efficient leaderboard data retrieval:

```python
@router.get("/guilds/{guild_id}/bytes/leaderboard", response_model=BytesLeaderboardResponse)
async def get_leaderboard(
    guild_id: str,
    limit: int = Query(10, ge=1, le=100),
    api_key: CurrentAPIKey,
    db: DatabaseSession
) -> BytesLeaderboardResponse:
    """Get bytes leaderboard for guild."""
    result = await db.execute(
        select(BytesBalance)
        .where(BytesBalance.guild_id == guild_id)
        .order_by(desc(BytesBalance.balance))
        .limit(limit)
    )
    
    entries = result.scalars().all()
    
    leaderboard = [
        {
            "user_id": entry.user_id,
            "balance": entry.balance,
            "total_received": entry.total_received,
            "total_sent": entry.total_sent
        }
        for entry in entries
    ]
    
    return BytesLeaderboardResponse(leaderboard=leaderboard)
```

### Balance Check Command
Command to check other users' balances:

```python
@bytes_group.child
@lightbulb.option(
    "user",
    "User to check (defaults to yourself)",
    type=hikari.User,
    required=False
)
@lightbulb.command("check", "Check someone's bytes balance")
@lightbulb.implements(lightbulb.SlashSubCommand)
@guild_only()
async def check_command(ctx: lightbulb.SlashContext) -> None:
    """Check another user's balance."""
    user = ctx.options.user or ctx.author
    
    # Don't check bots
    if user.is_bot:
        await bytes_plugin.send_error(ctx, "Bots don't have bytes!")
        return
    
    await ctx.respond(hikari.ResponseType.DEFERRED)
    
    try:
        # Get balance
        balance_data = await bytes_plugin.service.check_balance(
            str(ctx.guild_id),
            str(user.id),
            user.username
        )
        
        # Create embed
        embed = EmbedBuilder.bytes_balance(
            user=user,
            balance=balance_data["balance"],
            total_received=balance_data["total_received"],
            total_sent=balance_data["total_sent"],
            streak=balance_data.get("streak_count", 0),
            daily_available=False  # Don't show for other users
        )
        
        await ctx.respond(embed=embed)
        
    except Exception as e:
        logger.error("Check balance failed", error=str(e))
        await bytes_plugin.send_error(
            ctx,
            "Failed to check balance. Please try again later."
        )
```

### Database Optimization
Optimized queries for leaderboard performance:

```python
# Efficient leaderboard query with proper indexing
stmt = (
    select(BytesBalance)
    .where(BytesBalance.guild_id == guild_id)
    .order_by(desc(BytesBalance.balance))
    .limit(limit)
)

# Consider adding database index:
# CREATE INDEX idx_bytes_balance_guild_balance ON bytes_balances(guild_id, balance DESC);
```

### EmbedBuilder for Leaderboards
Utility for creating leaderboard displays:

```python
@staticmethod
def leaderboard(
    title: str,
    entries: List[Tuple[str, int, int]],
    footer: str = None
) -> hikari.Embed:
    """Create leaderboard embed."""
    embed = hikari.Embed(
        title=f"🏆 {title}",
        color=0xFFD700,  # Gold color
        timestamp=datetime.utcnow()
    )
    
    if not entries:
        embed.description = "No entries found."
        return embed
    
    # Format leaderboard entries
    leaderboard_text = []
    for username, balance, position in entries[:10]:  # Top 10
        # Add medal emojis for top 3
        if position == 1:
            emoji = "🥇"
        elif position == 2:
            emoji = "🥈"
        elif position == 3:
            emoji = "🥉"
        else:
            emoji = f"{position}."
        
        leaderboard_text.append(
            f"{emoji} **{username}** - {balance:,} bytes"
        )
    
    embed.description = "\n".join(leaderboard_text)
    
    if footer:
        embed.set_footer(footer)
    
    return embed
```

### Role Rewards Integration
System for checking earned role rewards based on total bytes received:

```python
async def check_role_rewards(
    self,
    guild_id: str,
    user_id: str,
    total_received: int
) -> List[str]:
    """Check which role rewards user has earned."""
    config = await self.get_config(guild_id)
    role_rewards = config.get("role_rewards", {})
    
    earned_roles = []
    
    for role_id, threshold in role_rewards.items():
        if total_received >= threshold:
            earned_roles.append(role_id)
    
    return earned_roles
```

## Related Files
- `bot/services/bytes_service.py` - Leaderboard service logic
- `bot/plugins/bytes.py` - Discord leaderboard commands
- `bot/utils/embeds.py` - Leaderboard embed formatting
- `web/api/routers/bytes.py` - Leaderboard API endpoint
- `web/api/schemas.py` - Leaderboard response models

## Goals Achieved
- **Competitive Display**: Clear ranking system encourages engagement
- **Performance Optimized**: Efficient database queries for large guilds
- **User-Friendly**: Easy-to-read leaderboard format
- **Configurable**: Adjustable display limits
- **Social Features**: Check other users' balances

## Dependencies
- Database with proper indexing for performance
- Discord bot cache for user information
- Embed builder utilities for consistent formatting
- API client for backend data retrieval
- Error handling for graceful failures

## Storage Considerations
- **Database Indexing**: Guild ID + Balance DESC for fast queries
- **Caching Strategy**: Consider Redis cache for frequently accessed leaderboards
- **Pagination**: Support for large leaderboards (100+ users)
- **Historical Data**: Track leaderboard positions over time

## Testing Strategy
```python
@pytest.mark.asyncio
async def test_leaderboard_ranking(bytes_service, mock_api):
    """Test leaderboard ranking logic."""
    mock_api.get_bytes_leaderboard.return_value = [
        {"user_id": "user1", "balance": 1000},
        {"user_id": "user2", "balance": 500},
        {"user_id": "user3", "balance": 100}
    ]
    
    leaderboard = await bytes_service.get_leaderboard("guild1", 10)
    
    assert len(leaderboard) == 3
    assert leaderboard[0]["balance"] == 1000  # Highest first
    assert leaderboard[2]["balance"] == 100   # Lowest last
```

This leaderboard and display system creates an engaging competitive environment where users can see their ranking and progress compared to others in their guild.