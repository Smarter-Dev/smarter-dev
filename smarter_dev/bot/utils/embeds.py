"""Rich Discord embed builders for consistent UI across all bot commands.

This module provides utilities for creating consistent, well-formatted embeds
for the Discord bot, including balance displays, error messages, success
confirmations, and other UI elements.
"""

from __future__ import annotations

import hikari
from datetime import datetime, timezone
from typing import Optional

from smarter_dev.bot.services.models import BytesBalance


def create_balance_embed(
    balance: BytesBalance,
    daily_earned: Optional[int] = None,
    streak: Optional[int] = None,
    multiplier: Optional[int] = None
) -> hikari.Embed:
    """Create a rich balance display embed.
    
    Args:
        balance: BytesBalance object with user's balance information
        daily_earned: Amount earned from daily claim (if applicable)
        streak: Current streak count (if daily was claimed)
        multiplier: Streak multiplier applied (if applicable)
        
    Returns:
        Formatted embed for balance display
    """
    embed = hikari.Embed(
        title="💰 Your Bytes Balance",
        color=hikari.Color(0x3b82f6),
        timestamp=datetime.now(timezone.utc)
    )
    
    # Main balance information
    embed.add_field("Current Balance", f"**{balance.balance:,}** bytes", inline=True)
    embed.add_field("Total Received", f"{balance.total_received:,} bytes", inline=True)
    embed.add_field("Total Sent", f"{balance.total_sent:,} bytes", inline=True)
    
    # Daily claim information if provided
    if daily_earned is not None and daily_earned > 0:
        embed.add_field("Daily Earned", f"**+{daily_earned:,}** bytes", inline=True)
        
        if streak and streak > 0:
            streak_name = get_streak_name(streak)
            embed.add_field(
                "Current Streak", 
                f"🔥 **{streak}** days ({streak_name})", 
                inline=True
            )
            
            if multiplier and multiplier > 1:
                embed.add_field("Streak Bonus", f"**{multiplier}x** multiplier", inline=True)
    
    # Add streak information even without daily claim
    elif balance.streak_count > 0:
        streak_name = get_streak_name(balance.streak_count)
        embed.add_field(
            "Current Streak",
            f"🔥 **{balance.streak_count}** days ({streak_name})",
            inline=True
        )
    
    # Last daily claim information
    if balance.last_daily:
        embed.add_field(
            "Last Daily Claim",
            balance.last_daily.strftime("%B %d, %Y"),
            inline=True
        )
    
    return embed


def get_streak_name(days: int) -> str:
    """Get streak tier name based on consecutive days.
    
    Args:
        days: Number of consecutive days
        
    Returns:
        String representation of streak tier
    """
    if days >= 60:
        return "LEGENDARY"
    elif days >= 30:
        return "EPIC"
    elif days >= 14:
        return "RARE"
    elif days >= 7:
        return "COMMON"
    else:
        return "BUILDING"


def create_error_embed(message: str) -> hikari.Embed:
    """Create a consistent error embed.
    
    Args:
        message: Error message to display
        
    Returns:
        Formatted error embed
    """
    return hikari.Embed(
        title="❌ Error",
        description=message,
        color=hikari.Color(0xef4444),
        timestamp=datetime.now(timezone.utc)
    )


def create_cooldown_embed(message: str, cooldown_end_timestamp: Optional[int] = None) -> hikari.Embed:
    """Create a cooldown-specific error embed with Discord timestamp formatting.
    
    Args:
        message: Cooldown message to display
        cooldown_end_timestamp: Unix timestamp when cooldown expires
        
    Returns:
        Formatted cooldown embed with Discord timestamp
    """
    import logging
    logger = logging.getLogger(__name__)
    
    # Build the description with Discord timestamp if available
    if cooldown_end_timestamp:
        # Use Discord's relative timestamp format (:R) for "in X hours" display
        description = f"You can send bytes again <t:{cooldown_end_timestamp}:R>."
        logger.debug(f"Created cooldown embed with timestamp {cooldown_end_timestamp}: {description}")
    else:
        # Fall back to the original message if no timestamp available
        description = message
        logger.debug(f"Created cooldown embed without timestamp: {description}")
    
    return hikari.Embed(
        title="⏰ Transfer Cooldown",
        description=description,
        color=hikari.Color(0xf59e0b),  # Amber color for warnings
        timestamp=datetime.now(timezone.utc)
    )


def create_success_embed(title: str, description: str) -> hikari.Embed:
    """Create a consistent success embed.
    
    Args:
        title: Success message title
        description: Success message description
        
    Returns:
        Formatted success embed
    """
    return hikari.Embed(
        title=title,
        description=description,
        color=hikari.Color(0x22c55e),
        timestamp=datetime.now(timezone.utc)
    )


def create_warning_embed(title: str, description: str) -> hikari.Embed:
    """Create a consistent warning embed.
    
    Args:
        title: Warning message title
        description: Warning message description
        
    Returns:
        Formatted warning embed
    """
    return hikari.Embed(
        title=title,
        description=description,
        color=hikari.Color(0xf59e0b),
        timestamp=datetime.now(timezone.utc)
    )


def create_info_embed(title: str, description: str) -> hikari.Embed:
    """Create a consistent info embed.
    
    Args:
        title: Info message title
        description: Info message description
        
    Returns:
        Formatted info embed
    """
    return hikari.Embed(
        title=title,
        description=description,
        color=hikari.Color(0x3b82f6),
        timestamp=datetime.now(timezone.utc)
    )


def create_leaderboard_embed(entries: list, guild_name: str = "Server", user_display_names: dict = None) -> hikari.Embed:
    """Create a formatted leaderboard embed.
    
    Args:
        entries: List of leaderboard entries
        guild_name: Name of the guild for the title
        user_display_names: Optional mapping of user_id to display name
        
    Returns:
        Formatted leaderboard embed
    """
    embed = hikari.Embed(
        title="🏆 Bytes Leaderboard",
        color=hikari.Color(0x3b82f6),
        timestamp=datetime.now(timezone.utc)
    )
    
    if not entries:
        embed.description = "No leaderboard data available yet!"
        return embed
    
    lines = []
    for entry in entries:
        # Medal for top 3
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(entry.rank, "🏅")
        
        # Use display name from mapping, fall back to username or user_id
        if user_display_names and entry.user_id in user_display_names:
            display_name = user_display_names[entry.user_id]
        elif hasattr(entry, 'username') and entry.username:
            display_name = entry.username
        else:
            display_name = f"User {entry.user_id[:8]}"
        
        lines.append(
            f"{medal} **{entry.rank}.** {display_name}\n"
            f"    💰 {entry.balance:,} bytes | 📈 {entry.total_received:,} received"
        )
    
    embed.description = "\n".join(lines)
    embed.set_footer(f"Showing top {len(entries)} users")
    
    return embed


def create_transaction_history_embed(transactions: list, user_id: str) -> hikari.Embed:
    """Create a formatted transaction history embed.
    
    Args:
        transactions: List of transaction objects
        user_id: ID of the user viewing the history
        
    Returns:
        Formatted transaction history embed
    """
    embed = hikari.Embed(
        title="📊 Your Transaction History",
        color=hikari.Color(0x3b82f6),
        timestamp=datetime.now(timezone.utc)
    )
    
    if not transactions:
        embed.description = "No transaction history found!"
        return embed
    
    lines = []
    for tx in transactions:
        if tx.giver_id == user_id:
            # Sent transaction
            receiver_display = tx.receiver_username
            
            # Special handling for system transactions
            if tx.receiver_id == "SYSTEM":
                icon = "➖"  # System deduction
                if (tx.reason and 
                    tx.reason.startswith("Squad join fee:")):
                    # Extract squad name from reason: "Squad join fee: Squad Name"
                    squad_name = tx.reason.replace("Squad join fee: ", "")
                    receiver_display = f"Joined {squad_name}"
                else:
                    receiver_display = "System Charge"
            else:
                icon = "📤"  # Regular transfer
            
            lines.append(f"{icon} **-{tx.amount:,}** to {receiver_display}")
        else:
            # Received transaction
            if tx.giver_id == "SYSTEM":
                icon = "➕"  # System reward
                if (tx.reason and 
                    tx.reason.strip() == "New member welcome bonus"):
                    giver_display = "Welcome Bonus"
                elif (tx.reason and 
                    tx.reason.startswith("Daily reward")):
                    # Extract streak info from reason: "Daily reward (Day 5, 2x multiplier)"
                    if "multiplier)" in tx.reason:
                        import re
                        match = re.search(r'Day (\d+)(?:, (\d+)x multiplier)?', tx.reason)
                        if match:
                            day = match.group(1)
                            multiplier = match.group(2)
                            if multiplier and multiplier != "1":
                                giver_display = f"Daily ({multiplier}x)"
                            else:
                                giver_display = f"Daily (Day {day})"
                        else:
                            giver_display = "Daily Reward"
                    else:
                        giver_display = "Daily Reward"
                else:
                    giver_display = "System Reward"
            else:
                icon = "📥"  # Regular transfer
                giver_display = tx.giver_username
                
            lines.append(f"{icon} **+{tx.amount:,}** from {giver_display}")
        
        if tx.reason:
            lines.append(f"    💬 {tx.reason}")
        # Handle string timestamps from API
        if isinstance(tx.created_at, str):
            try:
                created_dt = datetime.fromisoformat(tx.created_at.replace('Z', '+00:00'))
                time_str = created_dt.strftime('%m/%d %H:%M')
            except:
                time_str = tx.created_at[:10]  # Fallback to date portion
        else:
            time_str = tx.created_at.strftime('%m/%d %H:%M')
        lines.append(f"    🕒 {time_str}")
        lines.append("")  # Spacing
    
    # Respect Discord's embed description limit
    description = "\n".join(lines)
    if len(description) > 4000:
        description = description[:3950] + "\n... (truncated)"
    
    embed.description = description
    embed.set_footer(f"Showing {len(transactions)} recent transactions")
    
    return embed


def create_daily_claim_embed(
    earned: int,
    balance: int,
    streak: int,
    multiplier: int,
    guild_name: str = "Server"
) -> hikari.Embed:
    """Create an embed for daily claim notification.
    
    Args:
        earned: Amount of bytes earned
        balance: User's new total balance
        streak: Current streak count
        multiplier: Streak multiplier applied
        guild_name: Name of the guild for personalization
        
    Returns:
        Formatted embed for daily claim notification
    """
    embed = hikari.Embed(
        title="<:daily_bytes_received:1403748840477163642> Daily Bytes Claimed!",
        description=f"You've earned **{earned:,} bytes** from your first message today in {guild_name}!",
        color=hikari.Color(0x22c55e),
        timestamp=datetime.now(timezone.utc)
    )
    
    embed.add_field("Your Balance", f"**{balance:,}** bytes", inline=True)
    embed.add_field("Streak", f"**{streak:,}** days", inline=True)
    
    if multiplier > 1:
        embed.add_field("Multiplier", f"**{multiplier}x** streak bonus!", inline=True)
    
    embed.set_footer("💡 Send a message each day to maintain your streak!")
    
    return embed




def create_squad_list_embed(squads: list, current_squad_id: Optional[str] = None) -> hikari.Embed:
    """Create a formatted squad list embed.
    
    Args:
        squads: List of squad objects
        current_squad_id: ID of user's current squad (if any)
        
    Returns:
        Formatted squad list embed
    """
    embed = hikari.Embed(
        title="🏆 Available Squads",
        color=hikari.Color(0x3b82f6),
        description=f"There are **{len(squads)}** squads in this server"
    )
    
    if not squads:
        embed.description = "No squads have been created yet!"
        return embed
    
    for squad in squads[:10]:  # Limit to 10 for embed space
        # Visual indicator if user is in this squad
        name = f"{'✅ ' if current_squad_id and str(current_squad_id) == str(squad.id) else ''}{squad.name}"
        
        # Squad information
        value_parts = []
        if squad.description:
            value_parts.append(squad.description)
        
        value_parts.append(f"👥 {squad.member_count}")
        if squad.max_members:
            value_parts[-1] += f"/{squad.max_members} members"
        else:
            value_parts[-1] += " members"
        
        # Show switch cost if user not in this squad
        if not (current_squad_id and str(current_squad_id) == str(squad.id)):
            if squad.switch_cost > 0:
                value_parts.append(f"💰 Switch cost: **{squad.switch_cost}** bytes")
            else:
                value_parts.append("💰 **Free** to join!")
        
        embed.add_field(name, "\n".join(value_parts), inline=False)
    
    return embed