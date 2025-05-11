"""
API routes registration for the Smarter Dev website.

This module registers all API routes for the Smarter Dev website.
"""

from starlette.routing import Route

from .api_routes import (
    # Authentication
    api_token,

    # Guild endpoints
    guild_list, guild_detail, guild_create, guild_update,

    # User endpoints
    user_list, user_detail, user_create, user_update, user_delete,

    # Guild member endpoints
    guild_member_list, guild_member_detail, guild_member_create, guild_member_update,

    # Bytes endpoints
    bytes_list, bytes_detail, bytes_create,
    bytes_config_get, bytes_config_create, bytes_config_update,
    bytes_roles_list, bytes_role_create, bytes_role_update, bytes_role_delete,
    bytes_cooldown_get, user_bytes_balance, bytes_leaderboard,

    # API key endpoints
    api_key_list, api_key_create, api_key_delete,

    # Auto moderation endpoints
    automod_regex_rules_list, automod_regex_rule_detail, automod_regex_rule_create,
    automod_regex_rule_update, automod_regex_rule_delete,
    automod_rate_limits_list, automod_rate_limit_detail, automod_rate_limit_create,
    automod_rate_limit_update, automod_rate_limit_delete,

    # Squad endpoints
    squad_list, squad_detail, squad_create, squad_update, squad_delete,
    squad_member_list, squad_member_add, squad_member_remove,
    user_squads, user_eligible_squads
)

# Define API routes
api_routes = [
    # Authentication
    Route("/api/auth/token", api_token, methods=["POST"]),

    # Guild endpoints
    Route("/api/guilds", guild_list, methods=["GET"]),
    Route("/api/guilds/{guild_id:int}", guild_detail, methods=["GET"]),
    Route("/api/guilds", guild_create, methods=["POST"]),
    Route("/api/guilds/{guild_id:int}", guild_update, methods=["PUT"]),

    # User endpoints
    Route("/api/users", user_list, methods=["GET"]),
    Route("/api/users/{user_id:int}", user_detail, methods=["GET"]),
    Route("/api/users", user_create, methods=["POST"]),
    Route("/api/users/{user_id:int}", user_update, methods=["PUT"]),
    Route("/api/users/{user_id:int}", user_delete, methods=["DELETE"]),

    # Guild member endpoints
    Route("/api/guild-members", guild_member_list, methods=["GET"]),
    Route("/api/guild-members/{member_id:int}", guild_member_detail, methods=["GET"]),
    Route("/api/guild-members", guild_member_create, methods=["POST"]),
    Route("/api/guild-members/{member_id:int}", guild_member_update, methods=["PUT"]),

    # Bytes endpoints
    Route("/api/bytes", bytes_list, methods=["GET"]),
    Route("/api/bytes/{bytes_id:int}", bytes_detail, methods=["GET"]),
    Route("/api/bytes", bytes_create, methods=["POST"]),
    Route("/api/bytes/config/{guild_id}", bytes_config_get, methods=["GET"]),
    Route("/api/bytes/config", bytes_config_create, methods=["POST"]),
    Route("/api/bytes/config/{guild_id}", bytes_config_update, methods=["PUT"]),
    Route("/api/bytes/roles/{guild_id}", bytes_roles_list, methods=["GET"]),
    Route("/api/bytes/roles", bytes_role_create, methods=["POST"]),
    Route("/api/bytes/roles/{role_id:int}", bytes_role_update, methods=["PUT"]),
    Route("/api/bytes/roles/{role_id:int}", bytes_role_delete, methods=["DELETE"]),
    Route("/api/bytes/cooldown/{user_id}/{guild_id}", bytes_cooldown_get, methods=["GET"]),
    Route("/api/bytes/balance/{user_id}", user_bytes_balance, methods=["GET"]),
    Route("/api/bytes/leaderboard/{guild_id}", bytes_leaderboard, methods=["GET"]),

    # API key endpoints
    Route("/api/keys", api_key_list, methods=["GET"]),
    Route("/api/keys", api_key_create, methods=["POST"]),
    Route("/api/keys/{key_id:int}", api_key_delete, methods=["DELETE"]),

    # Auto moderation endpoints
    Route("/api/automod/regex-rules", automod_regex_rules_list, methods=["GET"]),
    Route("/api/automod/regex-rules/{rule_id:int}", automod_regex_rule_detail, methods=["GET"]),
    Route("/api/automod/regex-rules", automod_regex_rule_create, methods=["POST"]),
    Route("/api/automod/regex-rules/{rule_id:int}", automod_regex_rule_update, methods=["PUT"]),
    Route("/api/automod/regex-rules/{rule_id:int}", automod_regex_rule_delete, methods=["DELETE"]),
    Route("/api/automod/rate-limits", automod_rate_limits_list, methods=["GET"]),
    Route("/api/automod/rate-limits/{limit_id:int}", automod_rate_limit_detail, methods=["GET"]),
    Route("/api/automod/rate-limits", automod_rate_limit_create, methods=["POST"]),
    Route("/api/automod/rate-limits/{limit_id:int}", automod_rate_limit_update, methods=["PUT"]),
    Route("/api/automod/rate-limits/{limit_id:int}", automod_rate_limit_delete, methods=["DELETE"]),

    # Squad endpoints
    Route("/api/squads", squad_list, methods=["GET"]),
    Route("/api/squads/{squad_id:int}", squad_detail, methods=["GET"]),
    Route("/api/squads", squad_create, methods=["POST"]),
    Route("/api/squads/{squad_id:int}", squad_update, methods=["PUT"]),
    Route("/api/squads/{squad_id:int}", squad_delete, methods=["DELETE"]),
    Route("/api/squads/{squad_id:int}/members", squad_member_list, methods=["GET"]),
    Route("/api/squads/{squad_id:int}/members", squad_member_add, methods=["POST"]),
    Route("/api/squads/{squad_id:int}/members/{user_id}", squad_member_remove, methods=["DELETE"]),
    Route("/api/users/{user_id}/squads", user_squads, methods=["GET"]),
    Route("/api/users/{user_id}/eligible-squads", user_eligible_squads, methods=["GET"])
]
