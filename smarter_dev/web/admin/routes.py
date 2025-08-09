"""Admin interface routing configuration."""

from __future__ import annotations

from starlette.routing import Route

from smarter_dev.web.admin.auth import login, logout, discord_oauth_callback, admin_required
from smarter_dev.web.admin.views import (
    dashboard,
    guild_list,
    guild_detail,
    bytes_config,
    squads_config,
    api_keys_list,
    api_keys_create,
    api_keys_delete,
    conversations_list,
    conversation_detail,
    cleanup_expired_conversations,
    blog_list,
    blog_create,
    blog_edit,
    blog_delete,
    forum_agents_list,
    forum_agent_create,
    forum_agent_edit,
    forum_agent_delete,
    forum_agent_toggle,
    forum_agent_analytics,
    get_forum_response_details,
    forum_agents_bulk
)


# Define admin routes
admin_routes = [
    # Authentication routes
    Route("/login", login, methods=["GET"], name="admin_login"),
    Route("/auth/discord/callback", discord_oauth_callback, methods=["GET"], name="discord_oauth_callback"),
    Route("/logout", logout, methods=["POST"], name="admin_logout"),
    
    # Dashboard and overview
    Route("/", admin_required(dashboard), name="admin_dashboard"),
    
    # Guild management
    Route("/guilds", admin_required(guild_list), name="admin_guilds"),
    Route("/guilds/{guild_id}", admin_required(guild_detail), name="admin_guild_detail"),
    Route("/guilds/{guild_id}/bytes", admin_required(bytes_config), methods=["GET", "POST"], name="admin_bytes_config"),
    Route("/guilds/{guild_id}/squads", admin_required(squads_config), methods=["GET", "POST"], name="admin_squads_config"),
    
    # Forum agent management
    Route("/guilds/{guild_id}/forum-agents", admin_required(forum_agents_list), name="admin_forum_agents"),
    Route("/guilds/{guild_id}/forum-agents/create", admin_required(forum_agent_create), methods=["GET", "POST"], name="admin_forum_agent_create"),
    Route("/guilds/{guild_id}/forum-agents/{agent_id}/edit", admin_required(forum_agent_edit), methods=["GET", "POST"], name="admin_forum_agent_edit"),
    Route("/guilds/{guild_id}/forum-agents/{agent_id}/delete", admin_required(forum_agent_delete), methods=["POST"], name="admin_forum_agent_delete"),
    Route("/guilds/{guild_id}/forum-agents/{agent_id}/toggle", admin_required(forum_agent_toggle), methods=["POST"], name="admin_forum_agent_toggle"),
    Route("/guilds/{guild_id}/forum-agents/{agent_id}/analytics", admin_required(forum_agent_analytics), name="admin_forum_agent_analytics"),
    Route("/api/forum-responses/{response_id}/details", admin_required(get_forum_response_details), name="api_forum_response_details"),
    Route("/guilds/{guild_id}/forum-agents/bulk", admin_required(forum_agents_bulk), methods=["POST"], name="admin_forum_agents_bulk"),
    
    # API key management
    Route("/api-keys", admin_required(api_keys_list), name="admin_api_keys"),
    Route("/api-keys/create", admin_required(api_keys_create), methods=["GET", "POST"], name="admin_api_keys_create"),
    Route("/api-keys/{key_id}/delete", admin_required(api_keys_delete), methods=["POST"], name="admin_api_keys_delete"),
    
    # Conversation management
    Route("/conversations", admin_required(conversations_list), name="admin_conversations"),
    Route("/conversations/{conversation_id}", admin_required(conversation_detail), name="admin_conversation_detail"),
    Route("/conversations/cleanup", admin_required(cleanup_expired_conversations), methods=["GET", "POST"], name="admin_conversation_cleanup"),
    
    # Blog management
    Route("/blogs", admin_required(blog_list), name="admin_blogs"),
    Route("/blogs/create", admin_required(blog_create), methods=["GET", "POST"], name="admin_blog_create"),
    Route("/blogs/{blog_id}/edit", admin_required(blog_edit), methods=["GET", "POST"], name="admin_blog_edit"),
    Route("/blogs/{blog_id}/delete", admin_required(blog_delete), methods=["POST"], name="admin_blog_delete"),
]