"""Data-driven expectations for the smoke harness.

THIS IS THE FILE LATER PHASES EDIT when behavior intentionally changes
(new key format, removed /bot-admin, renamed admin routes, ...). Each check
is one row; the runner in ``checks.py`` interprets them in order.

- ``ApiCheck.path`` is relative to /api and may contain ``{saved:name}``
  placeholders filled from values captured by earlier checks (``save_key``).
- ``auth`` selects the Authorization header: ``bot`` (seeded Skrift-native
  sk_ key — the only accepted key shape since the phase-02 DB
  consolidation), ``legacy_bot`` (retired legacy sk- key, must 401),
  ``unknown_key`` / ``unknown_skrift_key`` (valid format, not in DB),
  ``malformed_key``, or ``none``.
- ``validate`` (optional) receives the parsed JSON body and returns an error
  string, or None when the body is acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from scripts.local_harness import config


@dataclass(frozen=True)
class ApiCheck:
    name: str
    method: str
    path: str
    auth: str = "bot"
    body: dict | None = None
    expect_status: tuple[int, ...] = (200,)
    validate: Callable[[object], str | None] | None = None
    save_key: str | None = None      # store body[save_field] under this name
    save_field: str = "id"


@dataclass(frozen=True)
class AdminPageCheck:
    name: str
    path: str
    expect_status: tuple[int, ...] = (200,)
    expect_substring: str | None = None


def _expect_field(name: str, expected: object) -> Callable[[object], str | None]:
    def _check(body: object) -> str | None:
        if not isinstance(body, dict):
            return f"expected JSON object, got {type(body).__name__}"
        if body.get(name) != expected:
            return f"expected {name}={expected!r}, got {body.get(name)!r}"
        return None
    return _check


def _expect_keys(*names: str) -> Callable[[object], str | None]:
    def _check(body: object) -> str | None:
        if not isinstance(body, dict):
            return f"expected JSON object, got {type(body).__name__}"
        missing = [n for n in names if n not in body]
        return f"missing keys: {missing}" if missing else None
    return _check


def _expect_nonempty_list(key: str | None = None) -> Callable[[object], str | None]:
    def _check(body: object) -> str | None:
        items = body.get(key) if key is not None and isinstance(body, dict) else body
        if not isinstance(items, list):
            return f"expected list at {key or '<root>'}, got {type(items).__name__}"
        if not items:
            return f"expected non-empty list at {key or '<root>'}"
        return None
    return _check


def _expect_quest_present(body: object) -> str | None:
    if not isinstance(body, dict) or not isinstance(body.get("quest"), dict):
        return f"expected an active daily quest, got {body!r}"
    return None


_G = config.GUILD_ID
_F = config.FORUM_CHANNEL_ID

API_CHECKS: tuple[ApiCheck, ...] = (
    # -- health + auth ------------------------------------------------------
    ApiCheck("health", "GET", "/health", auth="none",
             validate=_expect_field("status", "healthy")),
    ApiCheck("auth-status-valid-key", "GET", "/auth/status",
             validate=_expect_field("authenticated", True)),
    ApiCheck("auth-validate", "POST", "/auth/validate",
             validate=_expect_field("valid", True)),
    # Phase 02 (DB consolidation): the legacy sk- key table is unreachable
    # from the runtime database, so the retired legacy key must cleanly 401.
    ApiCheck("auth-legacy-key-401", "GET", "/auth/status",
             auth="legacy_bot", expect_status=(401,)),
    ApiCheck("auth-unknown-key-401", "GET", "/auth/status",
             auth="unknown_key", expect_status=(401,)),
    ApiCheck("auth-unknown-skrift-key-401", "GET", "/auth/status",
             auth="unknown_skrift_key", expect_status=(401,)),
    ApiCheck("auth-malformed-key-401", "GET", f"/guilds/{_G}/bytes/config",
             auth="malformed_key", expect_status=(401,)),
    ApiCheck("auth-missing-key-401", "GET", f"/guilds/{_G}/bytes/config",
             auth="none", expect_status=(401, 403)),
    # -- bytes economy ------------------------------------------------------
    ApiCheck("bytes-balance", "GET",
             f"/guilds/{_G}/bytes/balance/{config.USER_ID}",
             validate=_expect_field("balance", 1000)),
    ApiCheck("bytes-daily-claim", "POST", f"/guilds/{_G}/bytes/daily",
             body={"user_id": config.JOINER_USER_ID,
                   "username": config.JOINER_USER_NAME},
             validate=_expect_keys("balance", "reward_amount")),
    ApiCheck("bytes-transaction-create", "POST",
             f"/guilds/{_G}/bytes/transactions",
             body={
                 "giver_id": config.USER_ID,
                 "giver_username": config.USER_NAME,
                 "receiver_id": config.JOINER_USER_ID,
                 "receiver_username": config.JOINER_USER_NAME,
                 "amount": 5,
                 "reason": "harness check transfer",
             },
             validate=_expect_field("amount", 5)),
    ApiCheck("bytes-leaderboard", "GET", f"/guilds/{_G}/bytes/leaderboard",
             validate=_expect_nonempty_list("users")),
    ApiCheck("bytes-transaction-history", "GET",
             f"/guilds/{_G}/bytes/transactions",
             validate=_expect_nonempty_list("transactions")),
    ApiCheck("bytes-config", "GET", f"/guilds/{_G}/bytes/config",
             validate=_expect_field("daily_amount", 10)),
    # -- squads -------------------------------------------------------------
    ApiCheck("squads-list", "GET", f"/guilds/{_G}/squads/",
             validate=_expect_nonempty_list()),
    ApiCheck("squad-detail", "GET", f"/guilds/{_G}/squads/{config.SQUAD_ID}",
             validate=_expect_field("name", "Harness Squad")),
    ApiCheck("squad-join", "POST",
             f"/guilds/{_G}/squads/{config.SQUAD_ID}/join",
             body={"user_id": config.JOINER_USER_ID,
                   "username": config.JOINER_USER_NAME}),
    ApiCheck("squad-members", "GET",
             f"/guilds/{_G}/squads/{config.SQUAD_ID}/members",
             validate=_expect_nonempty_list("members")),
    ApiCheck("squad-membership-lookup", "GET",
             f"/guilds/{_G}/squads/members/{config.USER_ID}",
             validate=_expect_keys("squad")),
    ApiCheck("squad-leave", "DELETE", f"/guilds/{_G}/squads/leave",
             body={"user_id": config.JOINER_USER_ID}),
    # -- campaigns + challenges ---------------------------------------------
    ApiCheck("challenges-pending-announcements", "GET",
             "/challenges/pending-announcements",
             validate=_expect_nonempty_list("challenges")),
    ApiCheck("challenges-upcoming-announcements", "GET",
             "/challenges/upcoming-announcements?seconds=45",
             validate=_expect_keys("challenges")),
    ApiCheck("challenge-detail", "GET", f"/challenges/{config.CHALLENGE_ID}",
             validate=_expect_keys("challenge")),
    ApiCheck("challenge-mark-announced", "POST",
             f"/challenges/{config.CHALLENGE_ID}/mark-announced"),
    ApiCheck("challenge-mark-released", "POST",
             f"/challenges/{config.CHALLENGE_ID}/mark-released"),
    ApiCheck("challenges-scoreboard", "GET",
             f"/challenges/scoreboard?guild_id={_G}",
             validate=_expect_keys("scoreboard")),
    ApiCheck("challenges-upcoming-campaign", "GET",
             f"/challenges/upcoming-campaign?guild_id={_G}",
             validate=_expect_keys("campaign")),
    # -- quests ---------------------------------------------------------------
    ApiCheck("quests-daily-current", "GET",
             f"/quests/daily/current?guild_id={_G}",
             validate=_expect_quest_present),
    ApiCheck("quest-mark-announced", "POST",
             f"/quests/{config.DAILY_QUEST_ID}/mark-announced"),
    ApiCheck("quest-mark-active", "POST",
             f"/quests/{config.DAILY_QUEST_ID}/mark-active"),
    ApiCheck("quests-upcoming-announcements", "GET",
             "/quests/upcoming-announcements?seconds=45",
             validate=_expect_keys("quests")),
    # -- scheduled + repeating messages --------------------------------------
    ApiCheck("scheduled-messages-pending", "GET", "/scheduled-messages/pending",
             validate=_expect_nonempty_list("scheduled_messages")),
    ApiCheck("scheduled-messages-upcoming", "GET",
             "/scheduled-messages/upcoming?seconds=45",
             validate=_expect_keys("scheduled_messages")),
    ApiCheck("scheduled-message-mark-sent", "POST",
             f"/scheduled-messages/{config.SCHEDULED_MESSAGE_ID}/mark-sent"),
    ApiCheck("repeating-messages-due", "GET", "/repeating-messages/due",
             validate=_expect_nonempty_list("repeating_messages")),
    ApiCheck("repeating-message-mark-sent", "POST",
             f"/repeating-messages/{config.REPEATING_MESSAGE_ID}/mark-sent"),
    ApiCheck("repeating-messages-guild", "GET",
             f"/repeating-messages/guild/{_G}",
             validate=_expect_nonempty_list("repeating_messages")),
    # -- advent of code -------------------------------------------------------
    ApiCheck("aoc-active-configs", "GET", "/advent-of-code/active-configs",
             validate=_expect_nonempty_list("configs")),
    ApiCheck("aoc-guild-config", "GET", f"/advent-of-code/{_G}/config",
             validate=_expect_field("is_active", True)),
    ApiCheck("aoc-guild-threads", "GET", f"/advent-of-code/{_G}/threads",
             validate=_expect_nonempty_list("threads")),
    ApiCheck("aoc-thread-lookup", "GET",
             f"/advent-of-code/{_G}/threads/{config.AOC_YEAR}/{config.AOC_DAY}",
             validate=_expect_keys("thread")),
    ApiCheck("aoc-thread-record", "POST", f"/advent-of-code/{_G}/threads",
             body={
                 "year": config.AOC_YEAR,
                 "day": config.AOC_DAY + 1,
                 "thread_id": "555500000000000002",
                 "thread_title": f"AoC {config.AOC_YEAR} Day {config.AOC_DAY + 1}",
             },
             validate=_expect_field("success", True)),
    # -- forum agents + notifications -----------------------------------------
    ApiCheck("forum-agents-list", "GET", f"/guilds/{_G}/forum-agents",
             validate=_expect_nonempty_list()),
    ApiCheck("forum-agent-record-response", "POST",
             f"/guilds/{_G}/forum-agents/{config.FORUM_AGENT_ID}/responses",
             body={
                 "channel_id": _F,
                 "thread_id": "666600000000000002",
                 "post_title": "Harness check post",
                 "post_content": "Content",
                 "author_display_name": config.USER_NAME,
                 "decision_reason": "harness",
                 "confidence_score": 0.5,
                 "responded": True,
                 "response_content": "answer",
                 "tokens_used": 10,
                 "response_time_ms": 100,
             },
             validate=_expect_keys("id")),
    ApiCheck("forum-agent-response-count", "GET",
             f"/guilds/{_G}/forum-agents/{config.FORUM_AGENT_ID}/responses/count",
             validate=_expect_keys("count")),
    ApiCheck("forum-notification-topics", "GET",
             f"/guilds/{_G}/forum-channels/{_F}/notification-topics",
             validate=_expect_nonempty_list()),
    ApiCheck("forum-user-subscriptions", "GET",
             f"/guilds/{_G}/forum-channels/{_F}/user-subscriptions",
             validate=_expect_nonempty_list()),
    ApiCheck("forum-subscription-lookup", "GET",
             f"/guilds/{_G}/users/{config.USER_ID}/forum-subscriptions/{_F}",
             validate=_expect_field("user_id", config.USER_ID)),
    ApiCheck("forum-subscription-upsert", "PUT",
             f"/guilds/{_G}/users/{config.USER_ID}/forum-subscriptions/{_F}",
             body={
                 "user_id": config.USER_ID,
                 "username": config.USER_NAME,
                 "forum_channel_id": _F,
                 "subscribed_topics": ["general-help"],
                 "notification_hours": 12,
             },
             validate=_expect_field("notification_hours", 12)),
    # -- channel model overrides ----------------------------------------------
    ApiCheck("model-override-seeded", "GET",
             f"/guilds/{_G}/channels/{config.TEXT_CHANNEL_ID}/model-override",
             validate=_expect_field("model_key", config.MODEL_OVERRIDE_MODEL_KEY)),
    ApiCheck("model-override-put", "PUT",
             f"/guilds/{_G}/channels/{config.MODEL_OVERRIDE_CHANNEL_ID}/model-override",
             body={"model_key": config.MODEL_OVERRIDE_MODEL_KEY,
                   "daily_token_budget": 0, "hourly_token_budget": 0},
             validate=_expect_field("model_key", config.MODEL_OVERRIDE_MODEL_KEY)),
    ApiCheck("model-override-get", "GET",
             f"/guilds/{_G}/channels/{config.MODEL_OVERRIDE_CHANNEL_ID}/model-override",
             validate=_expect_field("channel_id", config.MODEL_OVERRIDE_CHANNEL_ID)),
    ApiCheck("model-override-delete", "DELETE",
             f"/guilds/{_G}/channels/{config.MODEL_OVERRIDE_CHANNEL_ID}/model-override",
             expect_status=(204,)),
    ApiCheck("model-override-gone-404", "GET",
             f"/guilds/{_G}/channels/{config.MODEL_OVERRIDE_CHANNEL_ID}/model-override",
             expect_status=(404,)),
    # -- chat agent conversations ---------------------------------------------
    ApiCheck("chat-engagement-create", "POST", "/chat-conversations/engagements",
             body={
                 "guild_id": _G,
                 "channel_id": config.TEXT_CHANNEL_ID,
                 "guild_name": "Harness Guild",
                 "channel_name": "harness-general",
                 "activation_user_id": config.USER_ID,
                 "activation_username": config.USER_NAME,
                 "activation_message_id": "777700000000000002",
             },
             expect_status=(201,), save_key="engagement_id"),
    ApiCheck("chat-turn-create", "POST", "/chat-conversations/turns",
             body={
                 "engagement_id": "{saved:engagement_id}",
                 "request_id": "harness-req-1",
                 "turn_kind": "initial",
                 "output_kind": "send_response",
                 "triggering_messages": [{"author": config.USER_NAME,
                                          "content": "hello"}],
                 "agent_output": {"response": "hi"},
                 "chat_tokens_input": 10,
                 "chat_tokens_output": 5,
                 "chat_model_name": "harness-model",
             },
             expect_status=(201,)),
    ApiCheck("chat-engagement-end", "POST",
             "/chat-conversations/engagements/{saved:engagement_id}/end",
             body={"deactivation_reason": "timeout"}),
    ApiCheck("chat-usage-leaderboard", "GET",
             f"/chat-conversations/usage-leaderboard?guild_id={_G}",
             validate=_expect_keys("entries", "total_tokens_all_time")),
    # -- image generation quota ------------------------------------------------
    ApiCheck("image-quota", "GET", f"/image-generations/quota?guild_id={_G}",
             validate=_expect_keys("limit", "remaining")),
    # -- member activity + handlers --------------------------------------------
    ApiCheck("activity-batch", "POST", "/activity/batch",
             body={"events": [{"guild_id": _G,
                               "user_id": config.JOINER_USER_ID,
                               "message_at": "2026-07-16T00:00:00Z"}]},
             validate=_expect_field("recorded", 1)),
    ApiCheck("handlers-list", "GET",
             f"/handlers?channel_id={config.TEXT_CHANNEL_ID}",
             validate=_expect_nonempty_list()),
    ApiCheck("handlers-active-channels", "GET", "/handlers/active-channels",
             validate=_expect_keys("channels")),
    # -- member purge (uses its own throwaway user) -----------------------------
    ApiCheck("member-delete", "DELETE",
             f"/guilds/{_G}/members/{config.DELETABLE_USER_ID}"),
)

# Every registered Skrift /admin page with a parameterless GET route.
SKRIFT_ADMIN_PAGES: tuple[AdminPageCheck, ...] = (
    AdminPageCheck("admin-index", "/admin/"),
    AdminPageCheck("admin-pages", "/admin/pages"),
    AdminPageCheck("admin-users", "/admin/users"),
    AdminPageCheck("admin-settings", "/admin/settings"),
    AdminPageCheck("admin-system-info", "/admin/system-info"),
    AdminPageCheck("admin-media", "/admin/media"),
    AdminPageCheck("admin-oauth-clients", "/admin/oauth-clients"),
    AdminPageCheck("admin-api-keys", "/admin/api-keys"),
    AdminPageCheck("admin-workers", "/admin/workers"),
    AdminPageCheck("admin-agent-usage", "/admin/agent-usage"),
    AdminPageCheck("admin-webhooks", "/admin/webhooks"),
    AdminPageCheck("admin-feature-flags", "/admin/feature-flags"),
    AdminPageCheck("admin-campaign-signups", "/admin/campaign-signups"),
    AdminPageCheck("admin-click-tracking", "/admin/click-tracking"),
    AdminPageCheck("admin-jina-cache", "/admin/jina-cache"),
    AdminPageCheck("admin-usage", "/admin/usage"),
    AdminPageCheck("admin-bot", "/admin/bot"),
    AdminPageCheck("admin-bot-guilds", "/admin/bot/guilds"),
    AdminPageCheck("admin-bot-guild-detail", f"/admin/bot/guilds/{_G}"),
    AdminPageCheck("admin-bot-moderation", f"/admin/bot/moderation/{_G}"),
    AdminPageCheck("admin-bot-mod-actions", f"/admin/bot/mod-actions/{_G}"),
    AdminPageCheck("admin-handlers", "/admin/handlers"),
    AdminPageCheck("admin-quests", "/admin/quests"),
    AdminPageCheck("admin-quests-create", "/admin/quests/create"),
    AdminPageCheck("admin-chat-conversations", "/admin/chat-conversations"),
    AdminPageCheck("admin-blogging-topics", "/admin/blogging-agent/topics"),
    AdminPageCheck("admin-blogging-runs", "/admin/blogging-agent/runs"),
)

# Every legacy /bot-admin page reachable by GET for an admin session.
LEGACY_ADMIN_PAGES: tuple[AdminPageCheck, ...] = (
    AdminPageCheck("bot-admin-dashboard", "/bot-admin/"),
    AdminPageCheck("bot-admin-guilds", "/bot-admin/guilds"),
    AdminPageCheck("bot-admin-guild-detail", f"/bot-admin/guilds/{_G}"),
    AdminPageCheck("bot-admin-bytes-config", f"/bot-admin/guilds/{_G}/bytes"),
    AdminPageCheck("bot-admin-squads-config", f"/bot-admin/guilds/{_G}/squads"),
    AdminPageCheck("bot-admin-audit-logs", f"/bot-admin/guilds/{_G}/audit-logs"),
    AdminPageCheck("bot-admin-attachment-filter",
                   f"/bot-admin/guilds/{_G}/attachment-filter"),
    AdminPageCheck("bot-admin-squad-sale-events",
                   f"/bot-admin/guilds/{_G}/squad-sale-events"),
    AdminPageCheck("bot-admin-forum-agents", f"/bot-admin/guilds/{_G}/forum-agents"),
    AdminPageCheck("bot-admin-forum-agent-create",
                   f"/bot-admin/guilds/{_G}/forum-agents/create"),
    AdminPageCheck(
        "bot-admin-forum-agent-edit",
        f"/bot-admin/guilds/{_G}/forum-agents/{config.FORUM_AGENT_ID}/edit"),
    AdminPageCheck(
        "bot-admin-forum-agent-analytics",
        f"/bot-admin/guilds/{_G}/forum-agents/{config.FORUM_AGENT_ID}/analytics"),
    # /bot-admin/api-keys was removed in phase 02: key management lives in
    # Skrift's built-in /admin/api-keys (checked above in SKRIFT_ADMIN_PAGES).
    AdminPageCheck("bot-admin-conversations", "/bot-admin/conversations"),
    AdminPageCheck(
        "bot-admin-conversation-detail",
        f"/bot-admin/conversations/{config.HELP_CONVERSATION_ID}"),
    AdminPageCheck("bot-admin-conversation-cleanup", "/bot-admin/conversations/cleanup"),
    AdminPageCheck("bot-admin-campaign-signups", "/bot-admin/campaign-signups"),
    AdminPageCheck("bot-admin-campaigns", f"/bot-admin/guilds/{_G}/campaigns"),
    AdminPageCheck("bot-admin-campaign-create",
                   f"/bot-admin/guilds/{_G}/campaigns/create"),
    AdminPageCheck(
        "bot-admin-campaign-edit",
        f"/bot-admin/guilds/{_G}/campaigns/{config.CAMPAIGN_ID}/edit"),
    AdminPageCheck(
        "bot-admin-campaign-challenges",
        f"/bot-admin/guilds/{_G}/campaigns/{config.CAMPAIGN_ID}/challenges"),
    AdminPageCheck(
        "bot-admin-challenge-create",
        f"/bot-admin/guilds/{_G}/campaigns/{config.CAMPAIGN_ID}/challenges/create"),
    AdminPageCheck(
        "bot-admin-scheduled-messages",
        f"/bot-admin/guilds/{_G}/campaigns/{config.CAMPAIGN_ID}/scheduled-messages"),
    AdminPageCheck(
        "bot-admin-scheduled-message-create",
        f"/bot-admin/guilds/{_G}/campaigns/{config.CAMPAIGN_ID}/scheduled-messages/create"),
    AdminPageCheck(
        "bot-admin-scheduled-message-edit",
        f"/bot-admin/guilds/{_G}/campaigns/{config.CAMPAIGN_ID}"
        f"/scheduled-messages/{config.SCHEDULED_MESSAGE_ID}/edit"),
    AdminPageCheck("bot-admin-repeating-messages",
                   f"/bot-admin/guilds/{_G}/repeating-messages"),
    AdminPageCheck("bot-admin-repeating-message-create",
                   f"/bot-admin/guilds/{_G}/repeating-messages/create"),
    AdminPageCheck(
        "bot-admin-repeating-message-edit",
        f"/bot-admin/guilds/{_G}/repeating-messages/{config.REPEATING_MESSAGE_ID}/edit"),
    AdminPageCheck("bot-admin-advent-of-code",
                   f"/bot-admin/guilds/{_G}/advent-of-code"),
)

# Unauthenticated requests to protected admin surfaces must NOT return 200.
UNAUTHENTICATED_PAGES: tuple[AdminPageCheck, ...] = (
    AdminPageCheck("skrift-admin-anon-redirects", "/admin/",
                   expect_status=(302, 303, 307, 401, 403)),
    AdminPageCheck("bot-admin-anon-redirects", "/bot-admin/",
                   expect_status=(302, 303, 307, 401, 403)),
)
