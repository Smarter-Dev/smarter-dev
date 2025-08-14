"""Campaign challenges commands for the Discord bot.

This module implements all campaign-related slash commands using the service layer
for business logic. Commands include listing campaigns, viewing challenges,
submitting solutions, and checking leaderboards.
"""

from __future__ import annotations

import hikari
import lightbulb
import logging
from datetime import datetime, timezone
from typing import List, TYPE_CHECKING
from uuid import UUID

from smarter_dev.bot.utils.embeds import (
    create_error_embed, 
    create_success_embed
)
from smarter_dev.bot.utils.image_embeds import get_generator
from smarter_dev.bot.utils.campaign_templates import get_available_templates, get_template
from smarter_dev.bot.services.exceptions import (
    ServiceError,
    ValidationError
)

if TYPE_CHECKING:
    from smarter_dev.bot.services.campaigns_service import CampaignsService

logger = logging.getLogger(__name__)

# Create plugin
plugin = lightbulb.Plugin("campaigns")


def create_campaign_embed(campaign, embed_color=0x7289DA):
    """Create a campaign info embed."""
    embed = hikari.Embed(
        title=f"🏆 {campaign.title}",
        description=campaign.description or "No description provided",
        color=embed_color
    )
    
    # Add campaign details
    embed.add_field(
        name="📊 Details",
        value=f"**Type:** {campaign.participant_type.title()}\n"
              f"**Status:** {campaign.status.title()}\n"
              f"**Strategy:** {campaign.scoring_strategy.replace('_', ' ').title()}",
        inline=True
    )
    
    # Add timing info if available
    timing_info = []
    if campaign.start_date:
        timing_info.append(f"**Start:** <t:{int(campaign.start_date.timestamp())}:F>")
    if campaign.end_date:
        timing_info.append(f"**End:** <t:{int(campaign.end_date.timestamp())}:F>")
    if campaign.challenge_release_delay_hours:
        timing_info.append(f"**Release Delay:** {campaign.challenge_release_delay_hours}h")
    
    if timing_info:
        embed.add_field(
            name="⏰ Timing",
            value="\n".join(timing_info),
            inline=True
        )
    
    # Add metadata
    embed.add_field(
        name="📅 Created",
        value=f"<t:{int(campaign.created_at.timestamp())}:R>",
        inline=True
    )
    
    embed.set_footer(text=f"Campaign ID: {campaign.id}")
    return embed


async def get_participant_display_name(ctx: lightbulb.Context, participant_id: str, participant_type: str, squad_cache: dict = None) -> str:
    """Get display name for a participant (player or squad)."""
    if participant_type == "player":
        try:
            user = ctx.bot.cache.get_user(int(participant_id))
            return user.username if user else f"User {participant_id[:8]}..."
        except:
            return f"User {participant_id[:8]}..."
    else:  # squad
        try:
            # Use cached squad list if available
            if squad_cache is None:
                squads_service = getattr(ctx.bot, 'd', {}).get('squads_service')
                if squads_service:
                    squads = await squads_service.list_squads(str(ctx.guild_id))
                    squad_cache = {str(s.id): s.name for s in squads}
                else:
                    squad_cache = {}
            
            return squad_cache.get(participant_id, f"Squad {participant_id[:8]}...")
        except:
            return f"Squad {participant_id[:8]}..."


async def send_campaign_announcement(bot, campaign, announcement_type: str, **kwargs):
    """Send campaign announcement to the configured channel."""
    if not campaign.announcement_channel_id:
        return
    
    try:
        channel = bot.cache.get_guild_channel(int(campaign.announcement_channel_id))
        if not channel:
            logger.warning(f"Announcement channel {campaign.announcement_channel_id} not found for campaign {campaign.id}")
            return
        
        embed = None
        
        if announcement_type == "campaign_start":
            embed = hikari.Embed(
                title="🚀 Campaign Started!",
                description=f"**{campaign.title}** has officially begun!",
                color=0x00ff00
            )
            embed.add_field(
                name="📋 Campaign Details",
                value=f"**Type:** {campaign.participant_type.title()}\n"
                      f"**Strategy:** {campaign.scoring_strategy.replace('_', ' ').title()}\n"
                      f"**Duration:** Ongoing",
                inline=True
            )
            if campaign.description:
                embed.add_field(
                    name="📝 Description",
                    value=campaign.description[:500],
                    inline=False
                )
            embed.add_field(
                name="🎯 Get Started",
                value="Use `/campaigns list` to view available campaigns\n"
                      "Use `/campaigns challenges <campaign_id>` to see challenges",
                inline=False
            )
        
        elif announcement_type == "challenge_released":
            challenge = kwargs.get("challenge")
            if challenge:
                embed = hikari.Embed(
                    title="🎯 New Challenge Released!",
                    description=f"**{challenge.title}** is now available in **{campaign.title}**!",
                    color=0x7289DA
                )
                embed.add_field(
                    name="📊 Challenge Info",
                    value=f"**Difficulty:** {challenge.difficulty.title()}\n"
                          f"**Order:** #{challenge.order_index}",
                    inline=True
                )
                if challenge.description:
                    embed.add_field(
                        name="📝 Description",
                        value=challenge.description[:300],
                        inline=False
                    )
                embed.add_field(
                    name="🏁 Ready to Solve?",
                    value=f"Use `/campaigns challenge {campaign.id} {challenge.id}` to view the full problem",
                    inline=False
                )
        
        elif announcement_type == "leaderboard_update":
            top_participants = kwargs.get("top_participants", [])
            if top_participants:
                embed = hikari.Embed(
                    title="🏆 Leaderboard Update",
                    description=f"Current standings for **{campaign.title}**",
                    color=0xffd700
                )
                
                leaderboard_text = ""
                medals = ["🥇", "🥈", "🥉"]
                for i, participant in enumerate(top_participants[:5]):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    participant_id = participant.get("participant_id", "Unknown")
                    points = participant.get("total_points", 0)
                    
                    # Try to resolve participant name
                    try:
                        if participant.get("participant_type") == "player":
                            user = bot.cache.get_user(int(participant_id))
                            display_name = user.username if user else f"User {participant_id[:8]}..."
                        else:
                            display_name = f"Squad {participant_id[:8]}..."
                    except:
                        display_name = f"Participant {participant_id[:8]}..."
                    
                    leaderboard_text += f"{medal} **{display_name}** - {points} pts\n"
                
                embed.add_field(
                    name="🏅 Top Performers",
                    value=leaderboard_text,
                    inline=False
                )
                embed.add_field(
                    name="📊 View Full Leaderboard",
                    value=f"Use `/campaigns leaderboard {campaign.id}` for complete rankings",
                    inline=False
                )
        
        if embed:
            embed.set_footer(text=f"Campaign: {campaign.title}")
            await bot.rest.create_message(channel.id, embed=embed)
            logger.info(f"Sent {announcement_type} announcement for campaign {campaign.id}")
    
    except Exception as e:
        logger.error(f"Failed to send campaign announcement: {e}")


async def check_campaign_milestones(ctx, bytes_service, user_stats, campaign, challenge):
    """Check for campaign milestone achievements and award bytes."""
    milestone_rewards = []
    
    if not user_stats or not bytes_service:
        return milestone_rewards
    
    try:
        completed_challenges = user_stats.get('completed_challenges', 0)
        total_points = user_stats.get('total_points', 0)
        success_rate = user_stats.get('success_rate', 0)
        
        # Challenge completion milestones
        challenge_milestones = {
            1: {"bytes": 10, "title": "First Steps", "desc": "solving your first challenge"},
            5: {"bytes": 25, "title": "Getting Started", "desc": "solving 5 challenges"},
            10: {"bytes": 50, "title": "Problem Solver", "desc": "solving 10 challenges"},
            25: {"bytes": 100, "title": "Challenge Master", "desc": "solving 25 challenges"},
            50: {"bytes": 200, "title": "Legend", "desc": "solving 50 challenges"}
        }
        
        # Points milestones
        points_milestones = {
            100: {"bytes": 15, "title": "Century Club", "desc": "earning 100 points"},
            500: {"bytes": 50, "title": "Point Collector", "desc": "earning 500 points"},
            1000: {"bytes": 100, "title": "High Scorer", "desc": "earning 1000 points"},
            2500: {"bytes": 200, "title": "Elite Performer", "desc": "earning 2500 points"}
        }
        
        # Accuracy milestones (only if user has made at least 5 submissions)
        if user_stats.get('total_submissions', 0) >= 5:
            accuracy_milestones = {
                80: {"bytes": 30, "title": "Accurate Solver", "desc": "maintaining 80% accuracy"},
                90: {"bytes": 50, "title": "Precision Expert", "desc": "maintaining 90% accuracy"},
                95: {"bytes": 75, "title": "Perfect Aim", "desc": "maintaining 95% accuracy"}
            }
        else:
            accuracy_milestones = {}
        
        # Check which milestones have been achieved
        all_milestones = [
            (challenge_milestones, completed_challenges, "challenges"),
            (points_milestones, total_points, "points"),
            (accuracy_milestones, success_rate, "accuracy")
        ]
        
        for milestones, current_value, milestone_type in all_milestones:
            for threshold, milestone_data in milestones.items():
                if current_value >= threshold:
                    # Check if we've already awarded this milestone (simple heuristic)
                    # In a real implementation, you'd track awarded milestones in the database
                    try:
                        award_result = await bytes_service.award_campaign_bytes(
                            guild_id=str(ctx.guild_id),
                            user_id=str(ctx.user.id),
                            username=str(ctx.user.username),
                            amount=milestone_data["bytes"],
                            reason=f"Milestone: {milestone_data['title']}",
                            campaign_title=campaign.title
                        )
                        
                        if award_result.get("success"):
                            milestone_rewards.append({
                                "title": milestone_data["title"],
                                "description": milestone_data["desc"],
                                "bytes_awarded": milestone_data["bytes"],
                                "threshold": threshold,
                                "type": milestone_type
                            })
                            
                            # Only award the highest achieved milestone per category
                            break
                            
                    except Exception as e:
                        logger.warning(f"Failed to award milestone {milestone_data['title']}: {e}")
    
    except Exception as e:
        logger.error(f"Error checking campaign milestones: {e}")
    
    return milestone_rewards


def create_challenge_embed(challenge, embed_color=0x7289DA):
    """Create a challenge info embed."""
    embed = hikari.Embed(
        title=f"🎯 {challenge.title}",
        description=challenge.description or "No description provided",
        color=embed_color
    )
    
    # Add challenge details
    embed.add_field(
        name="📊 Details",
        value=f"**Difficulty:** {challenge.difficulty.title()}\n"
              f"**Order:** #{challenge.order_index}",
        inline=True
    )
    
    # Add limits if specified
    limits = []
    if challenge.time_limit_minutes:
        limits.append(f"⏱️ {challenge.time_limit_minutes} minutes")
    if challenge.memory_limit_mb:
        limits.append(f"💾 {challenge.memory_limit_mb} MB")
    
    if limits:
        embed.add_field(
            name="🚫 Limits",
            value="\n".join(limits),
            inline=True
        )
    
    # Add release date if available
    if challenge.release_date:
        embed.add_field(
            name="🚀 Released",
            value=f"<t:{int(challenge.release_date.timestamp())}:R>",
            inline=True
        )
    
    # Add problem statement (truncated if too long)
    problem = challenge.problem_statement
    if len(problem) > 800:
        problem = problem[:800] + "..."
    
    embed.add_field(
        name="📋 Problem Statement",
        value=f"```\n{problem}\n```",
        inline=False
    )
    
    if challenge.expected_output_format:
        embed.add_field(
            name="📤 Expected Output Format",
            value=f"```\n{challenge.expected_output_format}\n```",
            inline=False
        )
    
    embed.set_footer(text=f"Challenge ID: {challenge.id}")
    return embed


@plugin.command
@lightbulb.command("campaigns", "Campaign challenges commands")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def campaigns_group(ctx: lightbulb.Context) -> None:
    """Base campaigns command group."""
    pass


@campaigns_group.child
@lightbulb.command("list", "View available campaigns in this server")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def list_campaigns_command(ctx: lightbulb.Context) -> None:
    """Handle campaigns list command - show available campaigns."""
    service: CampaignsService = getattr(ctx.bot, 'd', {}).get('campaigns_service')
    if not service:
        # Fallback to _services dict
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('campaigns_service')
    
    if not service:
        generator = get_generator()
        image_file = generator.create_error_embed("Campaign services are not initialized. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    try:
        campaigns = await service.list_campaigns(str(ctx.guild_id), status="active")
        
        if not campaigns:
            embed = create_error_embed("No active campaigns available in this server!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Create campaigns list embed
        embed = hikari.Embed(
            title="🏆 Active Campaigns",
            description=f"Found {len(campaigns)} active campaign(s) in this server",
            color=0x00ff00
        )
        
        for i, campaign in enumerate(campaigns[:10]):  # Limit to 10 campaigns
            status_emoji = {"active": "🟢", "draft": "🟡", "completed": "🔴", "cancelled": "⚫"}.get(campaign.status, "⚪")
            
            timing = ""
            if campaign.start_date:
                timing += f"Started <t:{int(campaign.start_date.timestamp())}:R>"
            if campaign.end_date:
                timing += f"\nEnds <t:{int(campaign.end_date.timestamp())}:R>"
            
            embed.add_field(
                name=f"{status_emoji} {campaign.title}",
                value=f"**Type:** {campaign.participant_type.title()}\n"
                      f"**ID:** `{str(campaign.id)[:8]}...`\n"
                      f"{timing}",
                inline=True
            )
        
        if len(campaigns) > 10:
            embed.set_footer(text=f"Showing first 10 of {len(campaigns)} campaigns. Use /campaigns info <id> for details.")
        
        await ctx.respond(embed=embed)
        
    except ServiceError as e:
        logger.error(f"Service error listing campaigns: {e}")
        embed = create_error_embed(f"Failed to retrieve campaigns: {e}")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.error(f"Unexpected error listing campaigns: {e}")
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.option("campaign_id", "Campaign ID to view details", type=str, required=True)
@lightbulb.command("info", "Get detailed information about a campaign")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def campaign_info_command(ctx: lightbulb.Context) -> None:
    """Handle campaign info command - show detailed campaign information."""
    service: CampaignsService = getattr(ctx.bot, 'd', {}).get('campaigns_service')
    if not service:
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('campaigns_service')
    
    if not service:
        embed = create_error_embed("Campaign services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    campaign_id = ctx.options.campaign_id.strip()
    
    try:
        campaign = await service.get_campaign(campaign_id)
        
        if not campaign:
            embed = create_error_embed(f"Campaign `{campaign_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Check if campaign belongs to this guild
        if campaign.guild_id and campaign.guild_id != str(ctx.guild_id):
            embed = create_error_embed("This campaign is not available in this server!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        embed = create_campaign_embed(campaign)
        await ctx.respond(embed=embed)
        
    except ServiceError as e:
        logger.error(f"Service error getting campaign info: {e}")
        embed = create_error_embed(f"Failed to retrieve campaign information: {e}")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.error(f"Unexpected error getting campaign info: {e}")
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.option("campaign_id", "Campaign ID to view challenges", type=str, required=True)
@lightbulb.command("challenges", "List challenges in a campaign")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def list_challenges_command(ctx: lightbulb.Context) -> None:
    """Handle campaign challenges command - show available challenges."""
    service: CampaignsService = getattr(ctx.bot, 'd', {}).get('campaigns_service')
    if not service:
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('campaigns_service')
    
    if not service:
        embed = create_error_embed("Campaign services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    campaign_id = ctx.options.campaign_id.strip()
    
    try:
        # First verify campaign exists and is accessible
        campaign = await service.get_campaign(campaign_id)
        if not campaign:
            embed = create_error_embed(f"Campaign `{campaign_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        if campaign.guild_id and campaign.guild_id != str(ctx.guild_id):
            embed = create_error_embed("This campaign is not available in this server!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Get challenges (only released ones for regular users)
        challenges = await service.list_challenges(campaign_id, include_unreleased=False)
        
        if not challenges:
            embed = create_error_embed(f"No challenges are available yet in campaign `{campaign.title}`!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Create challenges list embed
        embed = hikari.Embed(
            title=f"🎯 {campaign.title} - Challenges",
            description=f"Found {len(challenges)} released challenge(s)",
            color=0x7289DA
        )
        
        for challenge in challenges[:10]:  # Limit to 10 challenges
            difficulty_emoji = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}.get(challenge.difficulty, "⚪")
            
            released_text = ""
            if challenge.release_date:
                released_text = f"Released <t:{int(challenge.release_date.timestamp())}:R>"
            
            embed.add_field(
                name=f"{difficulty_emoji} #{challenge.order_index} {challenge.title}",
                value=f"**Difficulty:** {challenge.difficulty.title()}\n"
                      f"**ID:** `{str(challenge.id)[:8]}...`\n"
                      f"{released_text}",
                inline=True
            )
        
        if len(challenges) > 10:
            embed.set_footer(text=f"Showing first 10 of {len(challenges)} challenges. Use /campaigns challenge <id> for details.")
        
        await ctx.respond(embed=embed)
        
    except ServiceError as e:
        logger.error(f"Service error listing challenges: {e}")
        embed = create_error_embed(f"Failed to retrieve challenges: {e}")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.error(f"Unexpected error listing challenges: {e}")
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.option("challenge_id", "Challenge ID to view details", type=str, required=True)
@lightbulb.option("campaign_id", "Campaign ID containing the challenge", type=str, required=True)
@lightbulb.command("challenge", "Get detailed information about a challenge")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def challenge_info_command(ctx: lightbulb.Context) -> None:
    """Handle challenge info command - show detailed challenge information."""
    service: CampaignsService = getattr(ctx.bot, 'd', {}).get('campaigns_service')
    if not service:
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('campaigns_service')
    
    if not service:
        embed = create_error_embed("Campaign services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    campaign_id = ctx.options.campaign_id.strip()
    challenge_id = ctx.options.challenge_id.strip()
    
    try:
        # Verify campaign access first
        campaign = await service.get_campaign(campaign_id)
        if not campaign:
            embed = create_error_embed(f"Campaign `{campaign_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        if campaign.guild_id and campaign.guild_id != str(ctx.guild_id):
            embed = create_error_embed("This campaign is not available in this server!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        challenge = await service.get_challenge(campaign_id, challenge_id)
        
        if not challenge:
            embed = create_error_embed(f"Challenge `{challenge_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Check if challenge is released
        if challenge.release_date and challenge.release_date > datetime.now(timezone.utc):
            embed = create_error_embed(f"Challenge `{challenge.title}` has not been released yet!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        embed = create_challenge_embed(challenge)
        await ctx.respond(embed=embed)
        
    except ServiceError as e:
        logger.error(f"Service error getting challenge info: {e}")
        embed = create_error_embed(f"Failed to retrieve challenge information: {e}")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.error(f"Unexpected error getting challenge info: {e}")
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.option("answer", "Your solution to the challenge", type=str, required=True)
@lightbulb.option("challenge_id", "Challenge ID to submit answer for", type=str, required=True)
@lightbulb.option("campaign_id", "Campaign ID containing the challenge", type=str, required=True)
@lightbulb.command("submit", "Submit your answer for a challenge")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def submit_answer_command(ctx: lightbulb.Context) -> None:
    """Handle challenge submission command."""
    service: CampaignsService = getattr(ctx.bot, 'd', {}).get('campaigns_service')
    if not service:
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('campaigns_service')
    
    if not service:
        embed = create_error_embed("Campaign services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    campaign_id = ctx.options.campaign_id.strip()
    challenge_id = ctx.options.challenge_id.strip()
    answer = ctx.options.answer.strip()
    
    try:
        # Verify campaign and challenge access
        campaign = await service.get_campaign(campaign_id)
        if not campaign:
            embed = create_error_embed(f"Campaign `{campaign_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        if campaign.guild_id and campaign.guild_id != str(ctx.guild_id):
            embed = create_error_embed("This campaign is not available in this server!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        challenge = await service.get_challenge(campaign_id, challenge_id)
        if not challenge:
            embed = create_error_embed(f"Challenge `{challenge_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Check if challenge is released
        if challenge.release_date and challenge.release_date > datetime.now(timezone.utc):
            embed = create_error_embed(f"Challenge `{challenge.title}` has not been released yet!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Submit the answer
        submission = await service.submit_challenge_answer(
            campaign_id=campaign_id,
            challenge_id=challenge_id,
            participant_id=str(ctx.user.id),
            submitted_result=answer
        )
        
        # Create result embed
        if submission.is_correct:
            embed = hikari.Embed(
                title="✅ Correct Answer!",
                description=f"Congratulations! Your answer for **{challenge.title}** is correct.",
                color=0x00ff00
            )
            embed.add_field(
                name="🎉 Points Earned",
                value=f"**{submission.points_awarded}** points",
                inline=True
            )
            
            # Award bytes based on points earned
            bytes_awarded = 0
            try:
                bytes_service = getattr(ctx.bot, 'd', {}).get('bytes_service')
                if bytes_service and submission.points_awarded > 0:
                    # Calculate bytes reward (e.g., 1 byte per 10 points)
                    bytes_reward = max(1, submission.points_awarded // 10)
                    
                    award_result = await bytes_service.award_campaign_bytes(
                        guild_id=str(ctx.guild_id),
                        user_id=str(ctx.user.id),
                        username=str(ctx.user.username),
                        amount=bytes_reward,
                        reason=f"Challenge solved: {challenge.title}",
                        campaign_title=campaign.title
                    )
                    
                    if award_result.get("success"):
                        bytes_awarded = award_result.get("amount_awarded", 0)
                        
            except Exception as bytes_error:
                logger.warning(f"Failed to award bytes for challenge completion: {bytes_error}")
            
            if bytes_awarded > 0:
                embed.add_field(
                    name="💰 Bytes Reward",
                    value=f"**{bytes_awarded}** bytes",
                    inline=True
                )
            
            # Check progress milestones and achievements
            try:
                # Get user's campaign statistics for progress tracking
                user_stats = await service.get_participant_stats(
                    campaign_id=campaign_id,
                    participant_id=str(ctx.user.id)
                )
                
                # Check for milestone achievements
                milestone_rewards = await check_campaign_milestones(
                    ctx, bytes_service, user_stats, campaign, challenge
                )
                
                # Add milestone rewards to embed
                for reward in milestone_rewards:
                    embed.add_field(
                        name=f"🎖️ {reward['title']}",
                        value=f"**{reward['bytes_awarded']}** bytes for {reward['description']}",
                        inline=True
                    )
                
                # Get updated leaderboard to see if rankings changed significantly
                leaderboard_data = await service.get_campaign_leaderboard(
                    campaign_id=campaign_id,
                    limit=10
                )
                
                # Send announcement if this user is now in top 3
                if leaderboard_data:
                    user_rank = None
                    for entry in leaderboard_data:
                        if entry.get("participant_id") == str(ctx.user.id):
                            user_rank = entry.get("rank")
                            break
                    
                    if user_rank and user_rank <= 3:
                        # Award bonus bytes for reaching top 3
                        milestone_bytes = 0
                        try:
                            if user_rank == 1:
                                milestone_bytes = 50  # First place bonus
                            elif user_rank == 2:
                                milestone_bytes = 30  # Second place bonus
                            elif user_rank == 3:
                                milestone_bytes = 20  # Third place bonus
                            
                            if milestone_bytes > 0:
                                milestone_result = await bytes_service.award_campaign_bytes(
                                    guild_id=str(ctx.guild_id),
                                    user_id=str(ctx.user.id),
                                    username=str(ctx.user.username),
                                    amount=milestone_bytes,
                                    reason=f"Reached rank #{user_rank} in leaderboard",
                                    campaign_title=campaign.title
                                )
                                
                                if milestone_result.get("success"):
                                    # Update embed to show milestone bonus
                                    embed.add_field(
                                        name="🏆 Milestone Bonus",
                                        value=f"**{milestone_bytes}** bytes for reaching rank #{user_rank}!",
                                        inline=True
                                    )
                        except Exception as milestone_error:
                            logger.warning(f"Failed to award milestone bytes: {milestone_error}")
                        
                        # Send leaderboard update announcement
                        await send_campaign_announcement(
                            ctx.bot, 
                            campaign, 
                            "leaderboard_update",
                            top_participants=leaderboard_data[:5]
                        )
            except Exception as announcement_error:
                logger.warning(f"Failed to send achievement announcement: {announcement_error}")
        
        else:
            embed = hikari.Embed(
                title="❌ Incorrect Answer",
                description=f"Sorry, your answer for **{challenge.title}** is incorrect. Try again!",
                color=0xff0000
            )
            embed.add_field(
                name="💡 Keep trying!",
                value="Review the problem statement and try a different approach.",
                inline=False
            )
        
        embed.add_field(
            name="📝 Your Submission",
            value=f"```\n{answer[:500]}{'...' if len(answer) > 500 else ''}\n```",
            inline=False
        )
        
        embed.set_footer(text=f"Submitted at {submission.submission_timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        
    except ServiceError as e:
        logger.error(f"Service error submitting answer: {e}")
        embed = create_error_embed(f"Failed to submit answer: {e}")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.error(f"Unexpected error submitting answer: {e}")
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.option("participant_type", "Filter by participant type (player/squad)", type=str, required=False)
@lightbulb.option("campaign_id", "Campaign ID to view leaderboard", type=str, required=True)
@lightbulb.command("leaderboard", "View the campaign leaderboard")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def leaderboard_command(ctx: lightbulb.Context) -> None:
    """Handle campaign leaderboard command."""
    service: CampaignsService = getattr(ctx.bot, 'd', {}).get('campaigns_service')
    if not service:
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('campaigns_service')
    
    if not service:
        embed = create_error_embed("Campaign services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    campaign_id = ctx.options.campaign_id.strip()
    participant_type = ctx.options.participant_type.strip().lower() if ctx.options.participant_type else None
    
    # Validate participant_type if provided
    if participant_type and participant_type not in ["player", "squad"]:
        embed = create_error_embed("Participant type must be 'player' or 'squad'.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    try:
        # Verify campaign access
        campaign = await service.get_campaign(campaign_id)
        if not campaign:
            embed = create_error_embed(f"Campaign `{campaign_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        if campaign.guild_id and campaign.guild_id != str(ctx.guild_id):
            embed = create_error_embed("This campaign is not available in this server!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Get leaderboard data from API
        leaderboard_data = await service.get_campaign_leaderboard(
            campaign_id=campaign_id,
            participant_type=participant_type,
            limit=15
        )
        
        # Get campaign statistics for context
        stats = await service.get_campaign_stats(campaign_id)
        
        # Create leaderboard embed
        title = f"🏆 {campaign.title} - Leaderboard"
        if participant_type:
            title += f" ({participant_type.title()}s Only)"
        
        embed = hikari.Embed(
            title=title,
            color=0xffd700
        )
        
        # Add campaign stats
        embed.add_field(
            name="📊 Campaign Stats",
            value=f"**Participants:** {stats.total_participants}\n"
                  f"**Submissions:** {stats.total_submissions}\n"
                  f"**Success Rate:** {stats.success_rate:.1f}%\n"
                  f"**Avg Points:** {stats.avg_points_per_participant:.1f}",
            inline=True
        )
        
        embed.add_field(
            name="🎯 Challenges",
            value=f"**Released:** {stats.released_challenges}\n"
                  f"**Total:** {stats.total_challenges}",
            inline=True
        )
        
        # Add leaderboard entries
        if leaderboard_data:
            leaderboard_text = ""
            medals = ["🥇", "🥈", "🥉"]
            
            # Cache squad names for efficient lookup
            squad_cache = None
            try:
                squads_service = getattr(ctx.bot, 'd', {}).get('squads_service')
                if squads_service:
                    squads = await squads_service.list_squads(str(ctx.guild_id))
                    squad_cache = {str(s.id): s.name for s in squads}
            except:
                pass
            
            for i, entry in enumerate(leaderboard_data[:15]):
                rank = entry.get("rank", i + 1)
                medal = medals[i] if i < 3 else f"{rank}."
                participant_id = entry.get("participant_id", "Unknown")
                participant_type_entry = entry.get("participant_type", "unknown")
                total_points = entry.get("total_points", 0)
                completed_challenges = entry.get("completed_challenges", 0)
                
                # Get display name
                display_name = await get_participant_display_name(
                    ctx, participant_id, participant_type_entry, squad_cache
                )
                
                leaderboard_text += f"{medal} **{display_name}** - {total_points} pts ({completed_challenges} solved)\n"
            
            embed.add_field(
                name="🏅 Top Participants",
                value=leaderboard_text,
                inline=False
            )
        else:
            embed.add_field(
                name="🏅 Leaderboard",
                value="No participants yet",
                inline=False
            )
        
        # Add filter info if applied
        if participant_type:
            embed.set_footer(text=f"Showing {participant_type}s only • Use /campaigns stats for detailed analytics")
        else:
            embed.set_footer(text="Use /campaigns stats for detailed analytics")
        
        await ctx.respond(embed=embed)
        
    except ServiceError as e:
        logger.error(f"Service error getting leaderboard: {e}")
        embed = create_error_embed(f"Failed to retrieve leaderboard: {e}")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.error(f"Unexpected error getting leaderboard: {e}")
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.option("participant_id", "Participant ID to view stats for (defaults to your user)", type=str, required=False)
@lightbulb.option("campaign_id", "Campaign ID to view stats", type=str, required=True)
@lightbulb.command("stats", "View detailed participant statistics")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def participant_stats_command(ctx: lightbulb.Context) -> None:
    """Handle participant statistics command."""
    service: CampaignsService = getattr(ctx.bot, 'd', {}).get('campaigns_service')
    if not service:
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('campaigns_service')
    
    if not service:
        embed = create_error_embed("Campaign services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    campaign_id = ctx.options.campaign_id.strip()
    participant_id = ctx.options.participant_id.strip() if ctx.options.participant_id else str(ctx.user.id)
    
    try:
        # Verify campaign access
        campaign = await service.get_campaign(campaign_id)
        if not campaign:
            embed = create_error_embed(f"Campaign `{campaign_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        if campaign.guild_id and campaign.guild_id != str(ctx.guild_id):
            embed = create_error_embed("This campaign is not available in this server!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Get participant statistics
        participant_stats = await service.get_participant_stats(
            campaign_id=campaign_id,
            participant_id=participant_id
        )
        
        # Determine if viewing own stats or another participant's
        viewing_self = participant_id == str(ctx.user.id)
        
        # Format display name
        if viewing_self:
            display_name = "Your"
        else:
            try:
                user = ctx.bot.cache.get_user(int(participant_id))
                display_name = f"{user.username}'s" if user else f"User {participant_id[:8]}...'s"
            except:
                display_name = f"User {participant_id[:8]}...'s"
        
        # Create statistics embed
        embed = hikari.Embed(
            title=f"📊 {display_name} Statistics - {campaign.title}",
            color=0x3498db
        )
        
        if participant_stats:
            # Performance overview
            embed.add_field(
                name="🎯 Performance Overview",
                value=f"**Rank:** #{participant_stats.get('rank', 'N/A')}\n"
                      f"**Total Points:** {participant_stats.get('total_points', 0)}\n"
                      f"**Challenges Solved:** {participant_stats.get('completed_challenges', 0)}\n"
                      f"**Success Rate:** {participant_stats.get('success_rate', 0):.1f}%",
                inline=True
            )
            
            # Timing statistics
            avg_solve_time = participant_stats.get('avg_solve_time_minutes', 0)
            fastest_solve = participant_stats.get('fastest_solve_minutes', 0)
            
            embed.add_field(
                name="⏱️ Timing Stats",
                value=f"**Avg Solve Time:** {avg_solve_time:.1f} min\n"
                      f"**Fastest Solve:** {fastest_solve:.1f} min\n"
                      f"**Total Submissions:** {participant_stats.get('total_submissions', 0)}\n"
                      f"**First Submission:** {participant_stats.get('first_submission_date', 'N/A')}",
                inline=True
            )
            
            # Recent activity if available
            recent_submissions = participant_stats.get('recent_submissions', [])
            if recent_submissions:
                activity_text = ""
                for submission in recent_submissions[:5]:
                    challenge_title = submission.get('challenge_title', 'Unknown Challenge')[:30]
                    status = "✅" if submission.get('is_correct') else "❌"
                    points = submission.get('points_awarded', 0)
                    activity_text += f"{status} **{challenge_title}** - {points} pts\n"
                
                embed.add_field(
                    name="📈 Recent Activity",
                    value=activity_text,
                    inline=False
                )
        else:
            embed.add_field(
                name="📝 No Statistics Available",
                value="This participant hasn't made any submissions yet.",
                inline=False
            )
        
        # Add campaign context
        embed.set_footer(text=f"Campaign: {campaign.title} • Use /campaigns leaderboard to see all rankings")
        
        await ctx.respond(embed=embed)
        
    except ServiceError as e:
        logger.error(f"Service error getting participant stats: {e}")
        embed = create_error_embed(f"Failed to retrieve statistics: {e}")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.error(f"Unexpected error getting participant stats: {e}")
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.option("challenge_id", "Challenge ID to view leaderboard", type=str, required=True)
@lightbulb.option("campaign_id", "Campaign ID containing the challenge", type=str, required=True)
@lightbulb.command("challenge-leaderboard", "View leaderboard for a specific challenge")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def challenge_leaderboard_command(ctx: lightbulb.Context) -> None:
    """Handle challenge-specific leaderboard command."""
    service: CampaignsService = getattr(ctx.bot, 'd', {}).get('campaigns_service')
    if not service:
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('campaigns_service')
    
    if not service:
        embed = create_error_embed("Campaign services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    campaign_id = ctx.options.campaign_id.strip()
    challenge_id = ctx.options.challenge_id.strip()
    
    try:
        # Verify campaign and challenge access
        campaign = await service.get_campaign(campaign_id)
        if not campaign:
            embed = create_error_embed(f"Campaign `{campaign_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        if campaign.guild_id and campaign.guild_id != str(ctx.guild_id):
            embed = create_error_embed("This campaign is not available in this server!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        challenge = await service.get_challenge(campaign_id, challenge_id)
        if not challenge:
            embed = create_error_embed(f"Challenge `{challenge_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Check if challenge is released
        if challenge.release_date and challenge.release_date > datetime.now(timezone.utc):
            embed = create_error_embed(f"Challenge `{challenge.title}` has not been released yet!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Get challenge leaderboard
        challenge_leaderboard = await service.get_challenge_leaderboard(
            campaign_id=campaign_id,
            challenge_id=challenge_id,
            limit=15
        )
        
        # Create leaderboard embed
        embed = hikari.Embed(
            title=f"🎯 {challenge.title} - Leaderboard",
            description=f"Challenge #{challenge.order_index} • Difficulty: {challenge.difficulty.title()}",
            color=0x7289DA
        )
        
        # Add challenge stats
        embed.add_field(
            name="📊 Challenge Stats",
            value=f"**Total Solvers:** {len(challenge_leaderboard)}\n"
                  f"**Difficulty:** {challenge.difficulty.title()}\n"
                  f"**Order:** #{challenge.order_index}",
            inline=True
        )
        
        # Add leaderboard entries
        if challenge_leaderboard:
            leaderboard_text = ""
            medals = ["🥇", "🥈", "🥉"]
            
            # Cache squad names for efficient lookup
            squad_cache = None
            try:
                squads_service = getattr(ctx.bot, 'd', {}).get('squads_service')
                if squads_service:
                    squads = await squads_service.list_squads(str(ctx.guild_id))
                    squad_cache = {str(s.id): s.name for s in squads}
            except:
                pass
            
            for i, entry in enumerate(challenge_leaderboard[:15]):
                rank = i + 1
                medal = medals[i] if i < 3 else f"{rank}."
                participant_id = entry.get("participant_id", "Unknown")
                participant_type_entry = entry.get("participant_type", "unknown")
                points = entry.get("points_awarded", 0)
                solve_time = entry.get("solve_time_minutes", 0)
                
                # Get display name
                display_name = await get_participant_display_name(
                    ctx, participant_id, participant_type_entry, squad_cache
                )
                
                time_text = f" ({solve_time:.1f}m)" if solve_time > 0 else ""
                leaderboard_text += f"{medal} **{display_name}** - {points} pts{time_text}\n"
            
            embed.add_field(
                name="🏅 Top Solvers",
                value=leaderboard_text,
                inline=False
            )
        else:
            embed.add_field(
                name="🏅 Challenge Leaderboard",
                value="No one has solved this challenge yet!",
                inline=False
            )
        
        embed.set_footer(text=f"Use /campaigns challenge {campaign_id} {challenge_id} for problem details")
        
        await ctx.respond(embed=embed)
        
    except ServiceError as e:
        logger.error(f"Service error getting challenge leaderboard: {e}")
        embed = create_error_embed(f"Failed to retrieve challenge leaderboard: {e}")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.error(f"Unexpected error getting challenge leaderboard: {e}")
        embed = create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.command("generate-input", "Generate input for a challenge (admin only)")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def generate_input_command(ctx: lightbulb.Context) -> None:
    """Generate input for a challenge."""
    # Check if user has admin permissions (basic check)
    if not ctx.member.guild_permissions.administrator:
        embed = create_error_embed("Permission Denied", "This command requires administrator permissions.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return

    service = ctx.bot.campaigns_service
    if not service:
        embed = create_error_embed("Service Unavailable", "Campaigns service is not available.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return

    try:
        # Get campaign and challenge via modal/options (simplified implementation)
        embed = hikari.Embed(
            title="🔧 Generate Challenge Input",
            description="Use the API endpoints for full input generation management:\n\n"
                       "**Generate Input:**\n"
                       f"`POST /campaigns/{{campaign_id}}/challenges/{{challenge_id}}/generate-input`\n\n"
                       "**Clear Cache:**\n"
                       f"`DELETE /campaigns/{{campaign_id}}/challenges/{{challenge_id}}/input-cache`\n\n"
                       "These endpoints allow you to generate inputs for testing and manage the input cache.",
            color=0x3498db
        )
        
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    except Exception as e:
        logger.error(f"Error in generate input command: {e}")
        embed = create_error_embed("Generation Error", "Failed to process input generation command.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.option("announcement_type", "Type of announcement to send", type=str, required=True, choices=["campaign_start", "leaderboard_update"])
@lightbulb.option("campaign_id", "Campaign ID to announce", type=str, required=True)
@lightbulb.command("announce", "Send campaign announcement (admin only)")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def campaign_announce_command(ctx: lightbulb.Context) -> None:
    """Send campaign announcement."""
    # Check if user has admin permissions
    if not ctx.member.guild_permissions.administrator:
        embed = create_error_embed("Permission Denied", "This command requires administrator permissions.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return

    service: CampaignsService = getattr(ctx.bot, 'd', {}).get('campaigns_service')
    if not service:
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('campaigns_service')
    
    if not service:
        embed = create_error_embed("Campaign services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return

    campaign_id = ctx.options.campaign_id.strip()
    announcement_type = ctx.options.announcement_type.strip()

    try:
        # Get campaign
        campaign = await service.get_campaign(campaign_id)
        if not campaign:
            embed = create_error_embed(f"Campaign `{campaign_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        if campaign.guild_id and campaign.guild_id != str(ctx.guild_id):
            embed = create_error_embed("This campaign is not available in this server!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Prepare announcement data
        kwargs = {}
        if announcement_type == "leaderboard_update":
            # Get current leaderboard
            leaderboard_data = await service.get_campaign_leaderboard(
                campaign_id=campaign_id,
                limit=10
            )
            kwargs["top_participants"] = leaderboard_data

        # Send announcement
        await send_campaign_announcement(ctx.bot, campaign, announcement_type, **kwargs)
        
        embed = create_success_embed(
            "Announcement Sent",
            f"Successfully sent {announcement_type.replace('_', ' ')} announcement for **{campaign.title}**"
        )
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    except Exception as e:
        logger.error(f"Error sending campaign announcement: {e}")
        embed = create_error_embed("Announcement Error", "Failed to send campaign announcement.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.option("reason", "Reason for the bytes award", type=str, required=True)
@lightbulb.option("amount", "Amount of bytes to award", type=int, required=True)
@lightbulb.option("user", "User to award bytes to", type=hikari.User, required=True)
@lightbulb.option("campaign_id", "Campaign ID for context", type=str, required=True)
@lightbulb.command("award-bytes", "Award bytes for campaign achievements (admin only)")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def award_bytes_command(ctx: lightbulb.Context) -> None:
    """Award bytes to a user for campaign achievements."""
    # Check if user has admin permissions
    if not ctx.member.guild_permissions.administrator:
        embed = create_error_embed("Permission Denied", "This command requires administrator permissions.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return

    service: CampaignsService = getattr(ctx.bot, 'd', {}).get('campaigns_service')
    bytes_service = getattr(ctx.bot, 'd', {}).get('bytes_service')
    
    if not service or not bytes_service:
        embed = create_error_embed("Services not available", "Campaign or bytes services are not initialized.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return

    campaign_id = ctx.options.campaign_id.strip()
    target_user = ctx.options.user
    amount = ctx.options.amount
    reason = ctx.options.reason.strip()

    # Validate amount
    if amount <= 0:
        embed = create_error_embed("Invalid Amount", "Bytes amount must be positive.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return

    try:
        # Verify campaign exists
        campaign = await service.get_campaign(campaign_id)
        if not campaign:
            embed = create_error_embed(f"Campaign `{campaign_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        if campaign.guild_id and campaign.guild_id != str(ctx.guild_id):
            embed = create_error_embed("This campaign is not available in this server!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Award bytes
        award_result = await bytes_service.award_campaign_bytes(
            guild_id=str(ctx.guild_id),
            user_id=str(target_user.id),
            username=str(target_user.username),
            amount=amount,
            reason=reason,
            campaign_title=campaign.title
        )
        
        if award_result.get("success"):
            embed = hikari.Embed(
                title="💰 Bytes Awarded!",
                description=f"Successfully awarded **{amount}** bytes to {target_user.mention}",
                color=0x00ff00
            )
            embed.add_field(
                name="📋 Details",
                value=f"**Campaign:** {campaign.title}\n"
                      f"**Reason:** {reason}\n"
                      f"**Transaction ID:** {award_result.get('transaction_id', 'N/A')}",
                inline=False
            )
            
            if award_result.get("new_balance"):
                embed.add_field(
                    name="💳 New Balance",
                    value=f"{award_result['new_balance']:,} bytes",
                    inline=True
                )
        else:
            embed = create_error_embed("Award Failed", "Failed to award bytes. Please try again.")
        
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    except Exception as e:
        logger.error(f"Error awarding campaign bytes: {e}")
        embed = create_error_embed("Award Error", "Failed to award bytes.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.option("campaign_id", "Campaign ID to view progress", type=str, required=True)
@lightbulb.command("progress", "View your campaign progress and milestones")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def campaign_progress_command(ctx: lightbulb.Context) -> None:
    """Show user's campaign progress and milestone status."""
    service: CampaignsService = getattr(ctx.bot, 'd', {}).get('campaigns_service')
    if not service:
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('campaigns_service')
    
    if not service:
        embed = create_error_embed("Campaign services are not initialized. Please try again later.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    campaign_id = ctx.options.campaign_id.strip()
    
    try:
        # Verify campaign access
        campaign = await service.get_campaign(campaign_id)
        if not campaign:
            embed = create_error_embed(f"Campaign `{campaign_id[:8]}...` not found!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        if campaign.guild_id and campaign.guild_id != str(ctx.guild_id):
            embed = create_error_embed("This campaign is not available in this server!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Get user's statistics
        user_stats = await service.get_participant_stats(
            campaign_id=campaign_id,
            participant_id=str(ctx.user.id)
        )
        
        # Create progress embed
        embed = hikari.Embed(
            title=f"📊 Your Progress - {campaign.title}",
            color=0x3498db
        )
        
        if user_stats:
            # Current stats
            embed.add_field(
                name="🎯 Current Stats",
                value=f"**Rank:** #{user_stats.get('rank', 'N/A')}\n"
                      f"**Total Points:** {user_stats.get('total_points', 0):,}\n"
                      f"**Challenges Solved:** {user_stats.get('completed_challenges', 0)}\n"
                      f"**Success Rate:** {user_stats.get('success_rate', 0):.1f}%",
                inline=True
            )
            
            # Progress towards next milestones
            completed_challenges = user_stats.get('completed_challenges', 0)
            total_points = user_stats.get('total_points', 0)
            success_rate = user_stats.get('success_rate', 0)
            
            # Challenge milestones
            challenge_milestones = [1, 5, 10, 25, 50]
            next_challenge_milestone = None
            for milestone in challenge_milestones:
                if completed_challenges < milestone:
                    next_challenge_milestone = milestone
                    break
            
            # Points milestones
            points_milestones = [100, 500, 1000, 2500]
            next_points_milestone = None
            for milestone in points_milestones:
                if total_points < milestone:
                    next_points_milestone = milestone
                    break
            
            progress_text = ""
            if next_challenge_milestone:
                remaining = next_challenge_milestone - completed_challenges
                progress_text += f"**Next Challenge Goal:** {remaining} more challenge(s) to reach {next_challenge_milestone}\n"
            
            if next_points_milestone:
                remaining = next_points_milestone - total_points
                progress_text += f"**Next Points Goal:** {remaining} more points to reach {next_points_milestone:,}\n"
            
            if user_stats.get('total_submissions', 0) >= 5:
                if success_rate < 80:
                    needed = 80 - success_rate
                    progress_text += f"**Accuracy Goal:** Improve by {needed:.1f}% to reach 80% accuracy milestone\n"
            
            if progress_text:
                embed.add_field(
                    name="🎯 Next Milestones",
                    value=progress_text,
                    inline=True
                )
            
            # Available milestone rewards
            rewards_text = ""
            
            # Show challenge milestones
            for milestone in challenge_milestones:
                if completed_challenges >= milestone:
                    rewards_text += f"✅ {milestone} challenges solved\n"
                else:
                    bytes_reward = {1: 10, 5: 25, 10: 50, 25: 100, 50: 200}.get(milestone, 0)
                    rewards_text += f"🔒 {milestone} challenges → {bytes_reward} bytes\n"
            
            embed.add_field(
                name="🏆 Challenge Milestones",
                value=rewards_text[:500] or "None available",
                inline=False
            )
            
        else:
            embed.add_field(
                name="📝 No Progress Yet",
                value="You haven't participated in this campaign yet!\n"
                      "Use `/campaigns challenges` to see available challenges.",
                inline=False
            )
            
            # Show initial milestones available
            embed.add_field(
                name="🎯 Available Milestones",
                value="🔒 First challenge solved → 10 bytes\n"
                      "🔒 5 challenges solved → 25 bytes\n"
                      "🔒 100 points earned → 15 bytes\n"
                      "🔒 80% accuracy → 30 bytes",
                inline=False
            )
        
        embed.set_footer(text=f"Use /campaigns submit to start earning progress!")
        
        await ctx.respond(embed=embed)
        
    except Exception as e:
        logger.error(f"Error getting campaign progress: {e}")
        embed = create_error_embed("Failed to retrieve campaign progress.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.command("templates", "View available campaign templates")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def campaign_templates_command(ctx: lightbulb.Context) -> None:
    """Show available campaign templates."""
    # Check if user has admin permissions
    if not ctx.member.guild_permissions.administrator:
        embed = create_error_embed("Permission Denied", "This command requires administrator permissions.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return

    try:
        templates = get_available_templates()
        
        embed = hikari.Embed(
            title="🎯 Campaign Templates",
            description="Choose from these pre-configured campaign templates to get started quickly!",
            color=0x7289DA
        )
        
        for template_key, template in templates.items():
            challenges_count = len(template.get_challenges())
            embed.add_field(
                name=f"📋 {template.name}",
                value=f"{template.description}\n"
                      f"**Challenges:** {challenges_count}\n"
                      f"**ID:** `{template_key}`",
                inline=True
            )
        
        embed.add_field(
            name="🚀 How to Use",
            value="Use `/campaigns setup-wizard <template_id>` to create a campaign from a template.\n"
                  "Example: `/campaigns setup-wizard beginner_python`",
            inline=False
        )
        
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        
    except Exception as e:
        logger.error(f"Error showing campaign templates: {e}")
        embed = create_error_embed("Template Error", "Failed to load campaign templates.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.option("announcement_channel", "Channel for campaign announcements", type=hikari.TextableGuildChannel, required=False)
@lightbulb.option("campaign_type", "Type of campaign", type=str, required=False, choices=["player", "squad"])
@lightbulb.option("campaign_name", "Custom name for the campaign", type=str, required=False)
@lightbulb.option("template_id", "Template to use for setup", type=str, required=True)
@lightbulb.command("setup-wizard", "Quick campaign setup using templates (admin only)")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def campaign_setup_wizard_command(ctx: lightbulb.Context) -> None:
    """Set up a campaign using a template."""
    # Check if user has admin permissions
    if not ctx.member.guild_permissions.administrator:
        embed = create_error_embed("Permission Denied", "This command requires administrator permissions.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return

    template_id = ctx.options.template_id.strip()
    campaign_name = ctx.options.campaign_name
    campaign_type = ctx.options.campaign_type or "player"
    announcement_channel = ctx.options.announcement_channel

    try:
        # Get template
        template = get_template(template_id)
        if not template:
            available_templates = list(get_available_templates().keys())
            embed = create_error_embed(
                "Invalid Template", 
                f"Template '{template_id}' not found.\n\n"
                f"Available templates: {', '.join(available_templates)}\n"
                f"Use `/campaigns templates` to see details."
            )
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Generate campaign configuration
        config = template.generate_config(
            guild_id=str(ctx.guild_id),
            name=campaign_name,
            campaign_type=campaign_type,
            announcement_channel_id=str(announcement_channel.id) if announcement_channel else None
        )

        # Get challenges from template
        challenges = template.get_challenges()

        # Show preview embed
        preview_embed = hikari.Embed(
            title="📋 Campaign Setup Preview",
            description=f"Ready to create campaign from **{template.name}** template",
            color=0x3498db
        )
        
        preview_embed.add_field(
            name="📊 Campaign Details",
            value=f"**Name:** {config['name']}\n"
                  f"**Type:** {config['campaign_type'].title()}\n"
                  f"**Challenges:** {len(challenges)}\n"
                  f"**Scoring:** {config['scoring_type'].replace('_', ' ').title()}",
            inline=True
        )
        
        if announcement_channel:
            preview_embed.add_field(
                name="📢 Announcements",
                value=f"Will be sent to {announcement_channel.mention}",
                inline=True
            )
        
        # Show first few challenges
        challenge_preview = ""
        for i, challenge in enumerate(challenges[:3]):
            challenge_preview += f"{i+1}. **{challenge['title']}** (Difficulty {challenge['difficulty_level']})\n"
        
        if len(challenges) > 3:
            challenge_preview += f"... and {len(challenges) - 3} more challenges"
        
        preview_embed.add_field(
            name="🎯 Sample Challenges",
            value=challenge_preview,
            inline=False
        )
        
        preview_embed.add_field(
            name="⚠️ Next Steps",
            value="This will create a **draft** campaign. You can:\n"
                  "• Review and modify challenges via the admin interface\n"
                  "• Activate when ready to start\n"
                  "• Configure additional settings as needed",
            inline=False
        )
        
        preview_embed.set_footer(text="Creating campaign... please wait")
        
        await ctx.respond(embed=preview_embed, flags=hikari.MessageFlag.EPHEMERAL)
        
        # Get campaigns service
        service: CampaignsService = getattr(ctx.bot, 'd', {}).get('campaigns_service')
        if not service:
            service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('campaigns_service')
        
        if not service:
            embed = create_error_embed("Campaign Service Error", "Campaign services are not initialized. Please try again later.")
            await ctx.edit_last_response(embed=embed)
            return
        
        try:
            # Create the campaign
            created_campaign = await service.create_campaign(
                title=config['name'],
                description=config.get('description', ''),
                guild_id=config['guild_id'],
                participant_type=config['campaign_type'],
                start_date=config.get('start_date'),
                end_date=config.get('end_date'),
                challenge_release_delay_hours=config.get('release_delay_minutes', 1440) // 60,
                scoring_strategy=config.get('scoring_type', 'time_based'),
                scoring_config={
                    'starting_points': config.get('starting_points', 100),
                    'points_decrease_step': config.get('points_decrease_step', 10)
                }
            )
            
            # Create all challenges
            created_challenges = []
            for challenge_data in challenges:
                try:
                    created_challenge = await service.create_challenge(
                        campaign_id=str(created_campaign.id),
                        title=challenge_data['title'],
                        description=challenge_data.get('description', ''),
                        difficulty=str(challenge_data.get('difficulty_level', 5)),
                        problem_statement=challenge_data.get('problem_statement', ''),
                        generation_script=challenge_data.get('generation_script', ''),
                        expected_output_format=challenge_data.get('expected_output_format'),
                        order_index=challenge_data.get('order_position', 1)
                    )
                    created_challenges.append(created_challenge)
                except Exception as e:
                    logger.error(f"Failed to create challenge {challenge_data['title']}: {e}")
                    # Continue creating other challenges even if one fails
            
            # Send success confirmation
            success_embed = hikari.Embed(
                title="✅ Campaign Created Successfully!",
                description=f"Campaign **{created_campaign.title}** has been created from the **{template.name}** template.",
                color=0x27ae60
            )
            
            success_embed.add_field(
                name="📊 Campaign Details",
                value=f"**ID:** `{str(created_campaign.id)[:8]}...`\n"
                      f"**Type:** {created_campaign.participant_type.title()}\n"
                      f"**Status:** {created_campaign.status.title()}\n"
                      f"**Challenges Created:** {len(created_challenges)}/{len(challenges)}",
                inline=True
            )
            
            if announcement_channel:
                success_embed.add_field(
                    name="📢 Announcements",
                    value=f"Configured for {announcement_channel.mention}",
                    inline=True
                )
            
            success_embed.add_field(
                name="🚀 Next Steps",
                value="• Use `/campaigns list` to view your campaigns\n"
                      "• Use `/campaigns manage` to configure and activate\n"
                      "• The campaign is created in **draft** status\n"
                      "• Activate when ready to start accepting submissions",
                inline=False
            )
            
            success_embed.set_footer(text=f"Campaign ID: {created_campaign.id}")
            
            await ctx.edit_last_response(embed=success_embed)
            
            logger.info(f"Campaign setup wizard completed for template {template_id} by {ctx.user.id}: {created_campaign.id}")
            
        except ServiceError as e:
            logger.error(f"Service error creating campaign from template: {e}")
            embed = create_error_embed("Campaign Creation Failed", f"Failed to create campaign: {e}")
            await ctx.edit_last_response(embed=embed)
            return
        except Exception as e:
            logger.error(f"Unexpected error creating campaign from template: {e}")
            embed = create_error_embed("Campaign Creation Failed", "An unexpected error occurred while creating the campaign.")
            await ctx.edit_last_response(embed=embed)
            return

    except Exception as e:
        logger.error(f"Error in campaign setup wizard: {e}")
        embed = create_error_embed("Setup Error", "Failed to set up campaign from template.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.option("campaign_id", "Campaign ID to check schedule for", type=str, required=True)
@lightbulb.command("schedule", "Check challenge release schedule for a campaign")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def schedule_command(ctx: lightbulb.Context) -> None:
    """Check challenge release schedule for a campaign."""
    # Check if user has admin permissions
    if not ctx.member.guild_permissions.administrator:
        embed = create_error_embed("Permission Denied", "This command requires administrator permissions.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return

    campaign_id = ctx.options.campaign_id.strip()

    try:
        # Import the scheduler service
        from smarter_dev.services.challenge_scheduler import ChallengeReleaseService
        
        # Get next challenge release info
        next_release = await ChallengeReleaseService.get_next_challenge_release(UUID(campaign_id))
        
        # Get released challenges
        released_challenges = await ChallengeReleaseService.get_released_challenges(UUID(campaign_id))
        
        # Create schedule embed
        embed = hikari.Embed(
            title="📅 Challenge Release Schedule",
            description=f"Schedule for campaign `{campaign_id[:8]}...`",
            color=0x3498db
        )
        
        # Add released challenges info
        if released_challenges:
            released_text = ""
            for challenge in released_challenges:
                released_text += f"✅ **{challenge.title}** (#{challenge.order_position})\n"
                if challenge.released_at:
                    released_text += f"   Released: <t:{int(challenge.released_at.timestamp())}:R>\n"
            
            embed.add_field(
                name=f"🎯 Released Challenges ({len(released_challenges)})",
                value=released_text if len(released_text) < 1000 else released_text[:997] + "...",
                inline=False
            )
        
        # Add next release info
        if next_release:
            next_text = f"**{next_release['challenge_title']}** (#{next_release['order_position']})\n"
            next_text += f"Scheduled: <t:{int(next_release['release_time'].timestamp())}:R>\n"
            
            if next_release['is_ready']:
                next_text += "🟢 **Ready for release!**"
            else:
                hours_remaining = int(next_release['time_until_release'] // 3600)
                minutes_remaining = int((next_release['time_until_release'] % 3600) // 60)
                next_text += f"⏰ Time remaining: {hours_remaining}h {minutes_remaining}m"
            
            embed.add_field(
                name="⏭️ Next Challenge",
                value=next_text,
                inline=False
            )
        else:
            embed.add_field(
                name="⏭️ Next Challenge",
                value="All challenges have been released",
                inline=False
            )
        
        embed.set_footer(text="Release times are automatically managed by the scheduler")
        
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        
    except ValueError:
        embed = create_error_embed("Invalid Campaign ID", "Please provide a valid campaign ID.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.error(f"Error checking campaign schedule: {e}")
        embed = create_error_embed("Schedule Error", "Failed to retrieve campaign schedule.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@campaigns_group.child
@lightbulb.command("cache-status", "Check input cache status for a campaign")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def cache_status_command(ctx: lightbulb.Context) -> None:
    """Check input cache status."""
    # Check if user has admin permissions
    if not ctx.member.guild_permissions.administrator:
        embed = create_error_embed("Permission Denied", "This command requires administrator permissions.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        return

    try:
        embed = hikari.Embed(
            title="📊 Input Cache Management",
            description="Input cache statistics and management are available through the API:\n\n"
                       "**Cache Statistics:**\n"
                       "View cached entries via campaign statistics endpoints.\n\n"
                       "**Cache Management:**\n"
                       "Use the cache invalidation endpoints to clear cached inputs when scripts are updated.\n\n"
                       "**Performance Benefits:**\n"
                       "✅ Faster submission validation\n"
                       "✅ Reduced script execution overhead\n"
                       "✅ Consistent inputs for participants",
            color=0x27ae60
        )
        
        embed.add_field(
            name="🔄 Cache Lifecycle",
            value="• Inputs are cached per participant\n"
                  "• Cache survives across submissions\n"
                  "• Invalidated when scripts change\n"
                  "• Automatic cleanup of old entries",
            inline=False
        )
        
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    except Exception as e:
        logger.error(f"Error in cache status command: {e}")
        embed = create_error_embed("Cache Error", "Failed to retrieve cache status.")
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


def load(bot: lightbulb.BotApp) -> None:
    """Load the campaigns plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the campaigns plugin."""
    bot.remove_plugin(plugin)