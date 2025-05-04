"""
Tests for the API models.
"""

import os
import sys
import pytest
from datetime import datetime

# Add the project root to the path so we can import the bot package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.api_models import (
    Guild, DiscordUser, GuildMember, Kudos, UserNote, UserWarning,
    ModerationCase, PersistentRole, TemporaryRole, ChannelLock,
    BumpStat, CommandUsage
)

def test_guild_model():
    """Test the Guild model"""
    guild = Guild(
        id=1,
        discord_id=123456789,
        name="Test Guild",
        icon_url="https://example.com/icon.png",
        joined_at=datetime(2023, 1, 1),
        created_at=datetime(2023, 1, 1)
    )
    
    assert guild.id == 1
    assert guild.discord_id == 123456789
    assert guild.name == "Test Guild"
    assert guild.icon_url == "https://example.com/icon.png"
    assert guild.joined_at == datetime(2023, 1, 1)
    assert guild.created_at == datetime(2023, 1, 1)

def test_discord_user_model():
    """Test the DiscordUser model"""
    user = DiscordUser(
        id=1,
        discord_id=123456789,
        username="TestUser",
        discriminator="1234",
        avatar_url="https://example.com/avatar.png",
        created_at=datetime(2023, 1, 1)
    )
    
    assert user.id == 1
    assert user.discord_id == 123456789
    assert user.username == "TestUser"
    assert user.discriminator == "1234"
    assert user.avatar_url == "https://example.com/avatar.png"
    assert user.created_at == datetime(2023, 1, 1)

def test_kudos_model():
    """Test the Kudos model"""
    kudos = Kudos(
        id=1,
        giver_id=2,
        receiver_id=3,
        guild_id=4,
        amount=5,
        reason="For being awesome",
        awarded_at=datetime(2023, 1, 1)
    )
    
    assert kudos.id == 1
    assert kudos.giver_id == 2
    assert kudos.receiver_id == 3
    assert kudos.guild_id == 4
    assert kudos.amount == 5
    assert kudos.reason == "For being awesome"
    assert kudos.awarded_at == datetime(2023, 1, 1)

def test_user_warning_model():
    """Test the UserWarning model"""
    warning = UserWarning(
        id=1,
        user_id=2,
        mod_id=3,
        guild_id=4,
        reason="Breaking rules",
        warned_at=datetime(2023, 1, 1)
    )
    
    assert warning.id == 1
    assert warning.user_id == 2
    assert warning.mod_id == 3
    assert warning.guild_id == 4
    assert warning.reason == "Breaking rules"
    assert warning.warned_at == datetime(2023, 1, 1)

def test_moderation_case_model():
    """Test the ModerationCase model"""
    case = ModerationCase(
        id=1,
        case_number=2,
        guild_id=3,
        user_id=4,
        mod_id=5,
        action="ban",
        reason="Breaking rules",
        created_at=datetime(2023, 1, 1),
        duration_sec=3600,
        resolved_at=datetime(2023, 1, 2),
        resolution_note="User apologized"
    )
    
    assert case.id == 1
    assert case.case_number == 2
    assert case.guild_id == 3
    assert case.user_id == 4
    assert case.mod_id == 5
    assert case.action == "ban"
    assert case.reason == "Breaking rules"
    assert case.created_at == datetime(2023, 1, 1)
    assert case.duration_sec == 3600
    assert case.resolved_at == datetime(2023, 1, 2)
    assert case.resolution_note == "User apologized"
