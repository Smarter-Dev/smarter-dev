"""Challenge commands for Discord bot.

This plugin provides commands for interacting with challenges,
including viewing scoreboards and challenge statistics.
"""

from __future__ import annotations

import logging
import lightbulb
import hikari
from typing import TYPE_CHECKING, List, Dict, Any, Optional

if TYPE_CHECKING:
    from smarter_dev.bot.services.api_client import APIClient

from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.exceptions import APIError, ServiceError
from smarter_dev.shared.config import get_settings

logger = logging.getLogger(__name__)

# Create the plugin
plugin = lightbulb.Plugin("challenges")

# Initialize settings
settings = get_settings()


@plugin.command
@lightbulb.command("challenges", "Challenge-related commands")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def challenges_group() -> None:
    """Challenge command group."""
    pass


@challenges_group.child
@lightbulb.command("scoreboard", "View the current challenge scoreboard")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def scoreboard_command(ctx: lightbulb.Context) -> None:
    """Display the current challenge scoreboard for the most recent campaign."""
    try:
        # Get guild ID
        guild_id = str(ctx.guild_id) if ctx.guild_id else None
        if not guild_id:
            await ctx.respond("❌ This command can only be used in a server.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Initialize API client
        api_client = APIClient(
            base_url=settings.api_base_url,
            api_key=settings.bot_api_key,
            default_timeout=30.0
        )

        await ctx.respond("🔍 Fetching scoreboard data...", flags=hikari.MessageFlag.EPHEMERAL)

        try:
            # Get current campaign and scoreboard data
            response = await api_client.get(f"/challenges/scoreboard?guild_id={guild_id}")
            data = response.json()
            
            campaign = data.get("campaign")
            scoreboard = data.get("scoreboard", [])
            total_submissions = data.get("total_submissions", 0)
            total_challenges = data.get("total_challenges", 0)
            
            if not campaign:
                # Check if there's an upcoming campaign
                upcoming_response = await api_client.get(f"/challenges/upcoming-campaign?guild_id={guild_id}")
                upcoming_data = upcoming_response.json()
                upcoming_campaign = upcoming_data.get("campaign")
                
                if upcoming_campaign:
                    # There's an upcoming campaign
                    start_date = upcoming_campaign.get("start_date", "Unknown")
                    embed = hikari.Embed(
                        title="🏆 Challenge Scoreboard",
                        description=f"**No active campaign**\n\nThe next campaign **{upcoming_campaign.get('name', 'Unnamed Campaign')}** starts on {start_date}.\n\nStay tuned for challenges!",
                        color=0x3498db
                    )
                    embed.set_footer(text="Use the scoreboard command again once the campaign begins!")
                else:
                    # No campaigns at all
                    embed = hikari.Embed(
                        title="🏆 Challenge Scoreboard", 
                        description="**No events scheduled**\n\nThere are currently no challenge campaigns scheduled for this server.\n\nContact an administrator to set up challenges!",
                        color=0x95a5a6
                    )
                    embed.set_footer(text="Check back later for upcoming challenges!")
                
                await ctx.edit_last_response(content=None, embed=embed)
                return

            # Create fancy embed for scoreboard
            embed = hikari.Embed(
                title="🏆 Challenge Scoreboard",
                description=f"**{campaign.get('name', 'Current Campaign')}**",
                color=0xf39c12  # Orange/gold color
            )

            # Add campaign info
            embed.add_field(
                name="📊 Campaign Stats",
                value=f"**Challenges:** {total_challenges}\n**Total Submissions:** {total_submissions}",
                inline=True
            )

            if campaign.get("end_date"):
                embed.add_field(
                    name="⏰ Campaign Ends",
                    value=campaign.get("end_date"),
                    inline=True
                )

            # Add scoreboard data
            if scoreboard:
                # Top 10 squads
                top_squads = scoreboard[:10]
                
                scoreboard_text = ""
                for i, squad in enumerate(top_squads, 1):
                    squad_name = squad.get("squad_name", "Unknown Squad")
                    total_points = squad.get("total_points", 0)
                    successful_submissions = squad.get("successful_submissions", 0)
                    
                    # Add medal emojis for top 3
                    if i == 1:
                        medal = "🥇"
                    elif i == 2:
                        medal = "🥈" 
                    elif i == 3:
                        medal = "🥉"
                    else:
                        medal = f"**{i}.**"
                    
                    scoreboard_text += f"{medal} **{squad_name}** - {total_points} pts ({successful_submissions} solved)\n"
                
                embed.add_field(
                    name="🎯 Top Squads",
                    value=scoreboard_text or "No submissions yet",
                    inline=False
                )
                
                # Show if there are more squads
                if len(scoreboard) > 10:
                    embed.add_field(
                        name="📈 More Squads",
                        value=f"... and {len(scoreboard) - 10} more squads competing!",
                        inline=False
                    )
            else:
                embed.add_field(
                    name="🎯 Scoreboard",
                    value="No submissions yet. Be the first to solve a challenge!",
                    inline=False
                )

            # Add helpful footer
            embed.set_footer(
                text="💡 Tip: Join a squad with /squads join to participate in challenges!"
            )

            # Set thumbnail to a trophy emoji (using Unicode)
            embed.set_thumbnail("https://twemoji.maxcdn.com/v/latest/72x72/1f3c6.png")

            await ctx.edit_last_response(content=None, embed=embed)

        except APIError as api_error:
            logger.error(f"API error in scoreboard command: {api_error}")
            
            error_message = str(api_error)
            if "404" in error_message:
                await ctx.edit_last_response(
                    content="❌ Scoreboard data not found. There may be no active campaigns.",
                    embed=None
                )
            else:
                await ctx.edit_last_response(
                    content=f"❌ Failed to fetch scoreboard data: {error_message}",
                    embed=None
                )

        except Exception as e:
            logger.exception(f"Unexpected error in scoreboard command: {e}")
            await ctx.edit_last_response(
                content="❌ An unexpected error occurred while fetching the scoreboard.",
                embed=None
            )

        finally:
            await api_client.close()

    except Exception as e:
        logger.exception(f"Fatal error in scoreboard command: {e}")
        try:
            await ctx.respond(
                "❌ A fatal error occurred. Please try again later.",
                flags=hikari.MessageFlag.EPHEMERAL
            )
        except Exception:
            # Last resort if even the error response fails
            pass


def load(bot: lightbulb.BotApp) -> None:
    """Load the challenges plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the challenges plugin."""
    bot.remove_plugin(plugin)