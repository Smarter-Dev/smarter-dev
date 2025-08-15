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
        # Defer the response immediately to avoid timeout
        await ctx.respond(hikari.ResponseType.DEFERRED_MESSAGE_CREATE, flags=hikari.MessageFlag.EPHEMERAL)
        
        # Get guild ID
        guild_id = str(ctx.guild_id) if ctx.guild_id else None
        if not guild_id:
            await ctx.edit_last_response("This command can only be used in a server.")
            return

        # Initialize API client
        api_client = APIClient(
            base_url=settings.api_base_url,
            api_key=settings.bot_api_key,
            default_timeout=30.0
        )

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
                        title="Challenge Scoreboard",
                        description=f"**No active campaign**\n\nThe next campaign **{upcoming_campaign.get('name', 'Unnamed Campaign')}** starts on {start_date}.\n\nStay tuned for challenges!",
                        color=0x3498db
                    )
                    embed.set_footer(text="Use the scoreboard command again once the campaign begins!")
                else:
                    # No campaigns at all
                    embed = hikari.Embed(
                        title="Challenge Scoreboard", 
                        description="**No events scheduled**\n\nThere are currently no challenge campaigns scheduled for this server.\n\nContact an administrator to set up challenges!",
                        color=0x95a5a6
                    )
                    embed.set_footer(text="Check back later for upcoming challenges!")
                
                await ctx.edit_last_response(content=None, embed=embed)
                return

            # Create fancy embed for scoreboard
            embed = hikari.Embed(
                title="Challenge Scoreboard",
                description=f"**{campaign.get('name', 'Current Campaign')}**",
                color=0xf39c12  # Orange/gold color
            )

            # Add campaign info
            embed.add_field(
                name="Campaign Stats",
                value=f"**Challenges:** {total_challenges}\n**Total Submissions:** {total_submissions}",
                inline=True
            )

            if campaign.get("end_date"):
                embed.add_field(
                    name="Campaign Ends",
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
                    name="Top Squads",
                    value=scoreboard_text or "No submissions yet",
                    inline=False
                )
                
                # Show if there are more squads
                if len(scoreboard) > 10:
                    embed.add_field(
                        name="More Squads",
                        value=f"... and {len(scoreboard) - 10} more squads competing!",
                        inline=False
                    )
            else:
                embed.add_field(
                    name="Scoreboard",
                    value="No submissions yet. Be the first to solve a challenge!",
                    inline=False
                )

            # Add helpful footer
            embed.set_footer(
                text="Tip: Join a squad with /squads join to participate in challenges!"
            )


            await ctx.edit_last_response(content=None, embed=embed)

        except APIError as api_error:
            logger.error(f"API error in scoreboard command: {api_error}")
            
            error_message = str(api_error)
            if "404" in error_message:
                await ctx.edit_last_response(
                    content="Scoreboard data not found. There may be no active campaigns.",
                    embed=None
                )
            else:
                await ctx.edit_last_response(
                    content=f"Failed to fetch scoreboard data: {error_message}",
                    embed=None
                )

        except Exception as e:
            logger.exception(f"Unexpected error in scoreboard command: {e}")
            await ctx.edit_last_response(
                content="An unexpected error occurred while fetching the scoreboard.",
                embed=None
            )

        finally:
            await api_client.close()

    except Exception as e:
        logger.exception(f"Fatal error in scoreboard command: {e}")
        try:
            await ctx.respond(
                "A fatal error occurred. Please try again later.",
                flags=hikari.MessageFlag.EPHEMERAL
            )
        except Exception:
            # Last resort if even the error response fails
            pass


@challenges_group.child
@lightbulb.command("breakdown", "View detailed scoreboard with points breakdown by challenge")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def breakdown_command(ctx: lightbulb.Context) -> None:
    """Display detailed scoreboard with challenge-by-challenge breakdown."""
    try:
        # Defer the response immediately to avoid timeout
        await ctx.respond(hikari.ResponseType.DEFERRED_MESSAGE_CREATE, flags=hikari.MessageFlag.EPHEMERAL)
        
        # Get guild ID
        guild_id = str(ctx.guild_id) if ctx.guild_id else None
        if not guild_id:
            await ctx.edit_last_response("This command can only be used in a server.")
            return

        # Initialize API client
        api_client = APIClient(
            base_url=settings.api_base_url,
            api_key=settings.bot_api_key,
            default_timeout=30.0
        )

        try:
            # Get detailed scoreboard data
            response = await api_client.get(f"/challenges/detailed-scoreboard?guild_id={guild_id}")
            data = response.json()
            
            campaign = data.get("campaign")
            detailed_data = data.get("detailed_scoreboard", {})
            
            # Debug: Check the type and structure of detailed_data
            logger.info(f"detailed_data type: {type(detailed_data)}")
            logger.info(f"detailed_data content: {detailed_data}")
            
            # Handle case where detailed_data might be a list instead of dict
            if isinstance(detailed_data, list):
                # If it's a list, it might be empty or have a different structure
                challenges_breakdown = []
                squad_totals = []
            elif isinstance(detailed_data, dict):
                challenges_breakdown = detailed_data.get("challenges_breakdown", [])
                squad_totals = detailed_data.get("squad_totals", [])
            else:
                # Fallback
                challenges_breakdown = []
                squad_totals = []
                
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
                        title="📊 Detailed Challenge Breakdown",
                        description=f"**No active campaign**\n\nThe next campaign **{upcoming_campaign.get('name', 'Unnamed Campaign')}** starts on {start_date}.\n\nStay tuned for challenges!",
                        color=0x3498db
                    )
                    embed.set_footer(text="Use the breakdown command again once the campaign begins!")
                else:
                    # No campaigns at all
                    embed = hikari.Embed(
                        title="📊 Detailed Challenge Breakdown", 
                        description="**No events scheduled**\n\nThere are currently no challenge campaigns scheduled for this server.\n\nContact an administrator to set up challenges!",
                        color=0x95a5a6
                    )
                    embed.set_footer(text="Check back later for upcoming challenges!")
                
                await ctx.edit_last_response(content=None, embed=embed)
                return

            # Create detailed embed for breakdown
            embed = hikari.Embed(
                title="Detailed Challenge Breakdown",
                description=f"**{campaign.get('name', 'Current Campaign')}**",
                color=0x9b59b6  # Purple color
            )

            # Add campaign info
            embed.add_field(
                name="Campaign Stats",
                value=f"**Challenges:** {total_challenges}\n**Total Submissions:** {total_submissions}",
                inline=True
            )

            if campaign.get("end_date"):
                embed.add_field(
                    name="Campaign Ends",
                    value=campaign.get("end_date"),
                    inline=True
                )

            # Add challenge breakdown sections
            if challenges_breakdown:
                for i, challenge in enumerate(challenges_breakdown[:8]):  # Show up to 8 challenges
                    challenge_title = challenge.get("challenge_title", "Unknown Challenge")
                    submissions = challenge.get("submissions", [])
                    
                    # Build submissions text
                    submissions_text = ""
                    for j, submission in enumerate(submissions[:3]):  # Show top 3 submissions per challenge
                        squad_name = submission.get("squad_name", "Unknown Squad")
                        points = submission.get("points_earned", 0)
                        
                        # Add medal for top submission
                        if j == 0:
                            medal = "🥇"
                        elif j == 1:
                            medal = "🥈"
                        elif j == 2:
                            medal = "🥉"
                        else:
                            medal = "•"
                        
                        submissions_text += f"{medal} {squad_name}: {points} pts\n"
                    
                    if not submissions_text:
                        submissions_text = "No submissions yet"
                    elif len(submissions) > 3:
                        submissions_text += f"... and {len(submissions) - 3} more submissions"
                    
                    embed.add_field(
                        name=f"{challenge_title}",
                        value=submissions_text,
                        inline=False
                    )
                
                # Add overall standings if we have squad totals
                if squad_totals:
                    standings_text = ""
                    for i, squad in enumerate(squad_totals[:5]):  # Top 5 overall
                        squad_name = squad.get("squad_name", "Unknown Squad")
                        total_points = squad.get("total_points", 0)
                        challenges_completed = squad.get("challenges_completed", 0)
                        
                        if i == 0:
                            medal = "🥇"
                        elif i == 1:
                            medal = "🥈" 
                        elif i == 2:
                            medal = "🥉"
                        else:
                            medal = f"**{i+1}.**"
                        
                        standings_text += f"{medal} {squad_name}: {total_points} pts ({challenges_completed} solved)\n"
                    
                    embed.add_field(
                        name="Overall Standings",
                        value=standings_text or "No completed challenges",
                        inline=False
                    )
                
                # Show if there are more challenges
                if len(challenges_breakdown) > 8:
                    embed.add_field(
                        name="More Challenges",
                        value=f"... and {len(challenges_breakdown) - 8} more challenges with submissions!",
                        inline=False
                    )
            else:
                embed.add_field(
                    name="Challenge Progress",
                    value="No completed challenges yet. Be the first to solve one!",
                    inline=False
                )

            # Add helpful footer
            embed.set_footer(
                text="This shows detailed points breakdown for each completed challenge"
            )


            await ctx.edit_last_response(content=None, embed=embed)

        except APIError as api_error:
            logger.error(f"API error in breakdown command: {api_error}")
            
            error_message = str(api_error)
            if "404" in error_message:
                await ctx.edit_last_response(
                    content="Detailed breakdown data not found. There may be no active campaigns.",
                    embed=None
                )
            else:
                await ctx.edit_last_response(
                    content=f"Failed to fetch breakdown data: {error_message}",
                    embed=None
                )

        except Exception as e:
            logger.exception(f"Unexpected error in breakdown command: {e}")
            await ctx.edit_last_response(
                content="An unexpected error occurred while fetching the detailed breakdown.",
                embed=None
            )

        finally:
            await api_client.close()

    except Exception as e:
        logger.exception(f"Fatal error in breakdown command: {e}")
        try:
            await ctx.respond(
                "A fatal error occurred. Please try again later.",
                flags=hikari.MessageFlag.EPHEMERAL
            )
        except Exception:
            # Last resort if even the error response fails
            pass


@challenges_group.child
@lightbulb.command("event", "View current challenge event/campaign information")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def event_command(ctx: lightbulb.Context) -> None:
    """Display current challenge event/campaign information and current challenge."""
    try:
        # Defer the response immediately to avoid timeout
        await ctx.respond(hikari.ResponseType.DEFERRED_MESSAGE_CREATE, flags=hikari.MessageFlag.EPHEMERAL)
        
        # Get guild ID
        guild_id = str(ctx.guild_id) if ctx.guild_id else None
        if not guild_id:
            await ctx.edit_last_response("This command can only be used in a server.")
            return

        # Initialize API client
        api_client = APIClient(
            base_url=settings.api_base_url,
            api_key=settings.bot_api_key,
            default_timeout=30.0
        )

        try:
            # Get current campaign info
            response = await api_client.get(f"/challenges/scoreboard?guild_id={guild_id}")
            data = response.json()
            
            campaign = data.get("campaign")
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
                        title="Challenge Event",
                        description=f"**{upcoming_campaign.get('name', 'Unnamed Campaign')}**\n\n{upcoming_campaign.get('description', 'No description available.')}",
                        color=0x3498db
                    )
                    embed.add_field(
                        name="Event Status",
                        value="Upcoming",
                        inline=True
                    )
                    embed.add_field(
                        name="Start Date",
                        value=start_date,
                        inline=True
                    )
                    if upcoming_campaign.get("end_date"):
                        embed.add_field(
                            name="End Date",
                            value=upcoming_campaign.get("end_date"),
                            inline=True
                        )
                    embed.set_footer(text="Use this command again once the campaign begins!")
                else:
                    # No campaigns at all
                    embed = hikari.Embed(
                        title="Challenge Event",
                        description="**No events scheduled**\n\nThere are currently no challenge campaigns scheduled for this server.",
                        color=0x95a5a6
                    )
                    embed.set_footer(text="Contact an administrator to set up challenges!")
                
                await ctx.edit_last_response(content=None, embed=embed)
                return

            # Try to get current challenge info (this endpoint may not exist yet)
            current_challenge = None
            try:
                current_challenge_response = await api_client.get(f"/challenges?guild_id={guild_id}&limit=1&status=active")
                current_challenge_data = current_challenge_response.json()
                current_challenge = current_challenge_data.get("challenges", [])
                current_challenge = current_challenge[0] if current_challenge else None
            except APIError:
                # Endpoint doesn't exist yet, that's okay
                current_challenge = None

            # Create event information embed
            embed = hikari.Embed(
                title="Challenge Event",
                description=f"**{campaign.get('name', 'Current Campaign')}**\n\n{campaign.get('description', 'No description available.')}",
                color=0x27ae60  # Green color for active
            )

            # Add campaign timing info
            embed.add_field(
                name="Event Status",
                value="Active" if campaign.get("is_active") else "Ended",
                inline=True
            )
            
            if campaign.get("start_date"):
                embed.add_field(
                    name="Started",
                    value=campaign.get("start_date"),
                    inline=True
                )

            if campaign.get("end_date"):
                embed.add_field(
                    name="Ends",
                    value=campaign.get("end_date"),
                    inline=True
                )

            # Add challenge count
            embed.add_field(
                name="Total Challenges",
                value=str(total_challenges),
                inline=True
            )

            # Add current challenge info
            if current_challenge:
                embed.add_field(
                    name="Current Challenge",
                    value=current_challenge.get("title", "Unknown Challenge"),
                    inline=True
                )
            else:
                embed.add_field(
                    name="Current Challenge",
                    value="No active challenges",
                    inline=True
                )

            # Add helpful footer
            embed.set_footer(
                text="Use /challenges scoreboard to see rankings or /challenges breakdown for detailed stats"
            )

            await ctx.edit_last_response(content=None, embed=embed)

        except APIError as api_error:
            logger.error(f"API error in event command: {api_error}")
            
            error_message = str(api_error)
            if "not found" in error_message.lower():
                await ctx.edit_last_response(
                    content="Event data not found. There may be no active campaigns."
                )
            else:
                await ctx.edit_last_response(
                    content=f"Failed to fetch event data: {error_message}"
                )

        except Exception as e:
            logger.error(f"Unexpected error in event command: {e}")
            await ctx.edit_last_response(
                content="An unexpected error occurred while fetching the event information."
            )

    except Exception as fatal_error:
        logger.error(f"Fatal error in event command: {fatal_error}")
        try:
            await ctx.edit_last_response(
                "A fatal error occurred. Please try again later."
            )
        except:
            pass


def load(bot: lightbulb.BotApp) -> None:
    """Load the challenges plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the challenges plugin."""
    bot.remove_plugin(plugin)