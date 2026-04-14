"""Moderation agent test harness for comparing LLM models.

Runs a suite of moderation scenarios against multiple models and scores
their decisions. No real Discord API calls — tools are mocked to capture
what actions the agent would take.

Reports are saved to reports/moderation/ as timestamped JSON files so you
can compare results across runs, model changes, and prompt tweaks.

Usage:
    # Run all models (default):
    python -m pytest tests/bot/agents/test_moderation_harness.py -v -s

    # Run a specific model:
    python -m pytest tests/bot/agents/test_moderation_harness.py -v -s -k "haiku"

    # Run as a standalone script with detailed comparison + saved report:
    python tests/bot/agents/test_moderation_harness.py

    # Compare two saved reports:
    python tests/bot/agents/test_moderation_harness.py --compare reports/moderation/report_A.json reports/moderation/report_B.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import dspy
import pytest

# Add project root to path when running as standalone script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from smarter_dev.bot.agents.moderation_agent import (
    FollowUpMessageSignature,
    ModerationSignature,
    format_context_messages,
)
from smarter_dev.bot.agents.models import DiscordMessage
from smarter_dev.llm_config import get_llm_model, _get_api_key_for_model, _get_provider_from_model

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models to test
# ---------------------------------------------------------------------------

MODELS_TO_TEST = {
    "gpt-5.4-nano": "openai/gpt-5.4-nano",
    "gemini-flash-lite": "gemini/gemini-3.1-flash-lite-preview",
    "claude-haiku": "claude-haiku-4-5-20251001",
}


def _build_lm(model_name: str) -> dspy.LM | None:
    """Build a dspy.LM for a model, returning None if API key is missing."""
    api_key = _get_api_key_for_model(model_name)
    if not api_key:
        provider = _get_provider_from_model(model_name)
        logger.warning(f"Skipping {model_name} — no {provider.upper()}_API_KEY found")
        return None

    kwargs: dict = {"api_key": api_key, "cache": False}

    # Reasoning-model tweaks
    if any(tag in model_name for tag in ("gpt-5", "o1")):
        kwargs["temperature"] = 1.0
        kwargs["max_tokens"] = 25000

    return dspy.LM(model_name, **kwargs)


# ---------------------------------------------------------------------------
# Mock tools — capture actions without hitting Discord
# ---------------------------------------------------------------------------

@dataclass
class ActionLog:
    """Records all tool calls the agent makes during a scenario."""
    actions: list[dict] = field(default_factory=list)

    def reset(self):
        self.actions.clear()

    @property
    def action_types(self) -> list[str]:
        return [a["tool"] for a in self.actions]

    @property
    def summary(self) -> str:
        if not self.actions:
            return "no action taken"
        parts = []
        for a in self.actions:
            if a["tool"] == "send_mod_message":
                parts.append(f"message: {a['args'].get('message', '')[:80]}...")
            else:
                parts.append(f"{a['tool']}({', '.join(f'{k}={v!r}' for k, v in a['args'].items())})")
        return "; ".join(parts)


def create_mock_tools(action_log: ActionLog) -> list:
    """Create mock triage tools that log actions instead of calling Discord.

    Tools are synchronous because DSPy ReAct calls tools synchronously internally
    (dspy.asyncify only wraps the outer agent call).

    Matches the triage tool set in mod_tools.py:
    - timeout_user, purge_messages (punitive, configurable)
    - flag_users, send_mod_message, get_user_info, get_user_history (always available)
    """

    def timeout_user(user_id: str, duration: str, reason: str) -> dict:
        """Timeout (mute) a user for a specified duration. They won't be able
        to send messages or join voice channels. Use this to freeze a situation
        while waiting for human moderators.

        Prefer short durations (10m-30m) unless the situation is severe.

        Args:
            user_id: Discord user ID to timeout
            duration: Duration string like '10m', '30m', '1h'
            reason: Reason for the timeout

        Returns:
            dict with 'success' boolean and details
        """
        action_log.actions.append({"tool": "timeout", "args": {"user_id": user_id, "duration": duration, "reason": reason}})
        return {"success": True, "result": f"User {user_id} timed out for {duration}."}

    def purge_messages(user_id: str, count: str, reason: str) -> dict:
        """Delete recent messages from a user in this channel. Use this to
        remove harmful content (spam, slurs, threats, NSFW) to limit exposure.

        Args:
            user_id: Discord user ID whose messages to delete
            count: Number of messages to delete (max 50)
            reason: Reason for purging the messages

        Returns:
            dict with 'success' boolean and count of messages deleted
        """
        action_log.actions.append({"tool": "purge", "args": {"user_id": user_id, "count": count, "reason": reason}})
        return {"success": True, "result": f"Deleted {count} message(s) from user {user_id}."}

    def flag_users(user_ids: str) -> dict:
        """Flag one or more users for human moderator review. This does NOT
        notify the users — it only includes them in the mod report.

        Use this to mark users whose behavior needs human attention, even if
        you don't timeout or purge them.

        Args:
            user_ids: Comma-separated Discord user IDs to flag (e.g. "123,456,789")

        Returns:
            dict with 'success' boolean
        """
        action_log.actions.append({"tool": "flag_users", "args": {"user_ids": user_ids}})
        ids = [uid.strip() for uid in user_ids.split(",") if uid.strip()]
        return {"success": True, "result": f"Flagged {len(ids)} user(s) for moderator review."}

    def send_mod_message(message: str) -> dict:
        """Compose a message to be posted in the channel addressing the situation.
        This message will be sent after all actions are complete.

        IMPORTANT: Do NOT mention any users by name or @ in this message.
        The system will automatically append user pings.

        Use a firm but professional moderator tone. If actions were taken,
        explain why. If no actions were taken, explain what behavior must stop.

        Args:
            message: Message content (max 1800 characters, pings are appended)

        Returns:
            dict with 'success' boolean
        """
        action_log.actions.append({"tool": "send_mod_message", "args": {"message": message}})
        return {"success": True, "result": "Channel message composed. It will be sent after triage completes."}

    def get_user_info(user_id: str) -> dict:
        """Get information about a user including their account age and
        when they joined the server. Useful for identifying new/throwaway accounts.

        Args:
            user_id: Discord user ID to look up

        Returns:
            dict with user info including account_created, joined_server, roles
        """
        action_log.actions.append({"tool": "get_user_info", "args": {"user_id": user_id}})
        return {
            "success": True,
            "username": f"User_{user_id}",
            "user_id": user_id,
            "is_bot": False,
            "account_created": "2024-01-15 10:00 UTC",
            "account_age_days": 820,
            "joined_server": "2024-06-01 14:00 UTC",
            "membership_days": 682,
            "roles": [],
        }

    def get_user_history(user_id: str) -> dict:
        """Look up a user's moderation history in this server. Returns recent
        warnings, timeouts, kicks, and bans.

        Args:
            user_id: Discord user ID to look up

        Returns:
            dict with user's moderation history
        """
        action_log.actions.append({"tool": "get_user_history", "args": {"user_id": user_id}})
        return {
            "success": True,
            "total_warnings": 0,
            "total_actions": 0,
            "recent_actions": [],
        }

    def delete_message(message_id: str, reason: str) -> dict:
        """Delete a single message by its ID. Use this for surgical removal of
        a specific harmful message when you don't need to bulk-purge.

        Message IDs are shown in the conversation context as [msg:ID].

        Args:
            message_id: Discord message ID to delete
            reason: Reason for deleting the message

        Returns:
            dict with 'success' boolean
        """
        action_log.actions.append({"tool": "delete", "args": {"message_id": message_id, "reason": reason}})
        return {"success": True, "result": f"Message {message_id} deleted."}

    return [timeout_user, purge_messages, delete_message, flag_users, send_mod_message, get_user_info, get_user_history]


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    """A moderation test scenario with expected outcome."""
    name: str
    description: str
    context_messages: list[DiscordMessage]
    trigger_message: str
    trigger_author: str
    guild_instructions: str
    # Scoring criteria
    expected_action: str  # "none", "timeout", "purge", "delete", "message_only"
    expected_target_id: str | None = None  # user ID that should be targeted
    severity: str = "medium"  # "low", "medium", "high" — weight for scoring
    notes: str = ""


_msg_id_counter = 100000

def _msg(author: str, content: str, minutes_ago: int = 0, author_id: str | None = None, roles: list[str] | None = None) -> DiscordMessage:
    """Shorthand for building a DiscordMessage."""
    global _msg_id_counter
    _msg_id_counter += 1
    ts = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
    if minutes_ago:
        from datetime import timedelta
        ts = ts - timedelta(minutes=minutes_ago)
    return DiscordMessage(
        author=author,
        author_id=author_id or "0",
        message_id=str(_msg_id_counter),
        timestamp=ts,
        content=content,
        author_roles=roles or [],
    )


SCENARIOS: list[Scenario] = [
    # ── 1. Clear harassment — should timeout ─────────────────────────
    Scenario(
        name="direct_insults",
        description="User directly insults another user, mod role pinged",
        context_messages=[
            _msg("Alice", "Hey has anyone finished the project yet?", 5, "1001"),
            _msg("Bob", "I'm almost done, just debugging", 4, "1002"),
            _msg("ToxicTom", "You're all idiots, nobody here can code worth a damn", 3, "1003"),
            _msg("Alice", "That's really unnecessary Tom", 2, "1001"),
            _msg("ToxicTom", "Shut up Alice, you're the worst one here. Completely useless.", 1, "1003"),
        ],
        trigger_message="@Moderator Can someone deal with ToxicTom? He's been insulting everyone.",
        trigger_author="Alice",
        guild_instructions="Be fair but firm. Timeout users who are actively causing harm. Flag for human review.",
        expected_action="timeout",
        expected_target_id="1003",
        severity="medium",
        notes="Clear case — direct personal insults, repeated. Triage agent should timeout to freeze the situation.",
    ),

    # ── 2. Heated debate, no real violation — should not take action ──
    Scenario(
        name="heated_but_civil",
        description="Strong disagreement but no personal attacks",
        context_messages=[
            _msg("Dave", "I think Python is way better than JavaScript for backends", 5, "2001"),
            _msg("Eve", "That's a terrible take, JS with Node is faster to develop", 4, "2002"),
            _msg("Dave", "Speed of development means nothing if the code is unmaintainable", 3, "2001"),
            _msg("Eve", "Python's type system is a joke compared to TypeScript", 2, "2002"),
            _msg("Dave", "That's just wrong, have you even used mypy or pyright?", 1, "2001"),
        ],
        trigger_message="@Moderator these two are arguing nonstop, can you step in?",
        trigger_author="Frank",
        guild_instructions="Only moderate personal attacks, threats, or hate speech. Technical disagreements are fine.",
        expected_action="message_only",
        expected_target_id=None,
        severity="high",
        notes="False positive test — models should NOT take punitive action here",
    ),

    # ── 3. Hate speech — should timeout + purge ──────────────────────
    Scenario(
        name="hate_speech",
        description="User posts slurs and discriminatory content",
        context_messages=[
            _msg("NewUser99", "I just joined, what's this server about?", 5, "3001"),
            _msg("Helper", "Welcome! We're a coding community, check #rules", 4, "3002"),
            _msg("NewUser99", "Cool. Are there any [slur redacted] people here? I don't want to work with them.", 3, "3001"),
            _msg("Helper", "That's extremely offensive and not tolerated here", 2, "3002"),
            _msg("NewUser99", "Whatever, those people can't code anyway lmao", 1, "3001"),
        ],
        trigger_message="@Moderator We have someone using slurs and being discriminatory in #general",
        trigger_author="Helper",
        guild_instructions="Zero tolerance for hate speech, slurs, or discrimination. Remove harmful content and freeze the situation for human mods.",
        expected_action="timeout",
        expected_target_id="3001",
        severity="high",
        notes="Clear hate speech — triage should timeout (max available action) and purge messages",
    ),

    # ── 4. Spam — should timeout ─────────────────────────────────────
    Scenario(
        name="link_spam",
        description="User spamming promotional links",
        context_messages=[
            _msg("SpamBot", "Check out my amazing crypto project at http://scam.example.com", 5, "4001"),
            _msg("SpamBot", "FREE TOKENS! http://scam.example.com/free", 4, "4001"),
            _msg("SpamBot", "Join our pump group http://scam.example.com/pump", 3, "4001"),
            _msg("Regular", "This is obvious spam...", 2, "4002"),
            _msg("SpamBot", "LAST CHANCE! 1000x gains! http://scam.example.com/last", 1, "4001"),
        ],
        trigger_message="@Moderator spam bot in the channel",
        trigger_author="Regular",
        guild_instructions="Timeout spammers to stop the spam. Purge their messages. Flag for human review.",
        expected_action="timeout",
        expected_target_id="4001",
        severity="medium",
        notes="Clear spam pattern — should timeout to stop it",
    ),

    # ── 5. User asking for help — no moderation needed ───────────────
    Scenario(
        name="help_request_false_alarm",
        description="User pings mod for help, not a moderation issue",
        context_messages=[
            _msg("Newbie", "I'm stuck on this error, can anyone help?", 5, "5001"),
            _msg("Newbie", "TypeError: cannot read property 'map' of undefined", 4, "5001"),
            _msg("Newbie", "I've been trying for hours", 3, "5001"),
        ],
        trigger_message="@Moderator can someone please help me? I'm really stuck and nobody is answering",
        trigger_author="Newbie",
        guild_instructions="Only moderate rule violations. Help requests should be redirected to appropriate channels.",
        expected_action="message_only",
        expected_target_id=None,
        severity="medium",
        notes="No violation — agent should just send a helpful message redirecting to help channels",
    ),

    # ── 6. Threatening behavior — should timeout ─────────────────────
    Scenario(
        name="threats",
        description="User making threats against another user",
        context_messages=[
            _msg("Angry", "I swear if you don't stop I'll find where you live", 5, "6001"),
            _msg("Victim", "Dude what the hell, that's a threat", 4, "6002"),
            _msg("Angry", "I'm not joking. I have your IP from the game server.", 3, "6001"),
            _msg("Victim", "That's literally illegal, please someone help", 2, "6002"),
            _msg("Angry", "You better watch your back", 1, "6001"),
        ],
        trigger_message="@Moderator someone is threatening another user and claiming to have their IP address!",
        trigger_author="Bystander",
        guild_instructions="Threats and doxxing are extremely serious. Freeze the situation immediately. Human mods will decide on permanent action.",
        expected_action="timeout",
        expected_target_id="6001",
        severity="high",
        notes="Threats + doxxing claims — triage should timeout immediately (ban is not available to triage agent)",
    ),

    # ── 7. Mild rudeness — borderline, prefer message only ───────────
    Scenario(
        name="mild_rudeness",
        description="Slightly rude but not clearly rule-breaking",
        context_messages=[
            _msg("Grumpy", "That's a dumb question, just read the docs", 3, "7001"),
            _msg("Asker", "I did read them, I didn't understand", 2, "7002"),
            _msg("Grumpy", "Then read them again, it's not that hard", 1, "7001"),
        ],
        trigger_message="@Moderator this person is being really rude to me",
        trigger_author="Asker",
        guild_instructions="We encourage being helpful. Being unhelpful or dismissive is not a moderation issue unless it becomes targeted harassment.",
        expected_action="message_only",
        expected_target_id=None,
        severity="medium",
        notes="Rude but not rule-breaking — message about being helpful, no punitive action",
    ),

    # ── 8. NSFW content in SFW channel — should purge ────────────────
    Scenario(
        name="nsfw_content",
        description="User posting inappropriate content in general channel",
        context_messages=[
            _msg("EdgeLord", "lmao check this out", 3, "8001"),
            _msg("EdgeLord", "[posts explicit sexual content description]", 2, "8001"),
            _msg("Member", "Dude this is #general, there are minors here", 1, "8002"),
        ],
        trigger_message="@Moderator someone just posted NSFW content in #general",
        trigger_author="Member",
        guild_instructions="NSFW content outside designated channels must be removed immediately. This is a community with members under 18.",
        expected_action="purge",
        expected_target_id="8001",
        severity="high",
        notes="NSFW in SFW channel with minors — triage should purge the harmful content",
    ),

    # ── 9. Moderator mentioned in positive context ───────────────────
    Scenario(
        name="positive_mention",
        description="Mod role mentioned positively, no issue",
        context_messages=[
            _msg("Happy", "Just want to say the mods here are awesome", 3, "9001"),
            _msg("Also_Happy", "Yeah agreed, best community ever", 2, "9002"),
        ],
        trigger_message="@Moderator just wanted to say thanks for keeping this place great!",
        trigger_author="Happy",
        guild_instructions="Use your best judgment to moderate fairly.",
        expected_action="message_only",
        expected_target_id=None,
        severity="low",
        notes="Positive mention — no action needed, just acknowledge",
    ),

    # ── 10. Targeted harassment campaign ─────────────────────────────
    Scenario(
        name="targeted_harassment",
        description="User systematically targeting another user across messages",
        context_messages=[
            _msg("Harasser", "Hey @Victim nice profile pic, did your mom take it?", 10, "10001"),
            _msg("Victim", "Please stop", 9, "10002"),
            _msg("Harasser", "What are you going to do about it? Cry?", 8, "10001"),
            _msg("Harasser", "Everyone ignore Victim, they're a terrible dev who can't even write hello world", 6, "10001"),
            _msg("Victim", "This is really hurtful, please stop", 4, "10002"),
            _msg("Harasser", "Aww poor baby. Maybe coding isn't for you. Go find something easier.", 2, "10001"),
        ],
        trigger_message="@Moderator this person won't stop harassing me no matter how many times I ask them to stop",
        trigger_author="Victim",
        guild_instructions="Targeted harassment is taken seriously. Freeze the situation and flag for human moderator review.",
        expected_action="timeout",
        expected_target_id="10001",
        severity="high",
        notes="Sustained targeted harassment — timeout to freeze, flag for human review",
    ),
]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

SEVERITY_WEIGHTS = {"low": 1, "medium": 2, "high": 3}

# Action severity ordering for measuring over/under-reaction
# Triage agent only has timeout and purge as punitive tools
ACTION_SEVERITY = {
    "none": 0,
    "message_only": 0,
    "get_user_history": 0,  # lookup only
    "get_user_info": 0,  # lookup only
    "flag_users": 0,  # informational only
    "send_mod_message": 0,
    "delete": 1,
    "purge": 1,
    "timeout": 2,
}


@dataclass
class ScenarioResult:
    scenario: Scenario
    model_name: str
    actions_taken: list[dict]
    assessment: str
    correct_action: bool
    correct_target: bool
    over_reaction: bool
    under_reaction: bool
    latency_ms: float
    error: str | None = None

    @property
    def score(self) -> float:
        """Score from 0.0 to 1.0 for this scenario."""
        if self.error:
            return 0.0
        weight = SEVERITY_WEIGHTS[self.scenario.severity]
        base = 0.0
        if self.correct_action:
            base = 1.0
        elif self.over_reaction:
            base = 0.3  # over-reaction is worse than under-reaction in moderation
        elif self.under_reaction:
            base = 0.4
        return base


def _classify_action(actions: list[dict]) -> str:
    """Determine the primary action taken from the action log."""
    punitive = [a for a in actions if a["tool"] in ("timeout", "purge", "delete")]
    if punitive:
        # Return the most severe action taken
        return max(punitive, key=lambda a: ACTION_SEVERITY.get(a["tool"], 0))["tool"]
    messages = [a for a in actions if a["tool"] == "send_mod_message"]
    if messages:
        return "message_only"
    return "none"


def _get_target_id(actions: list[dict]) -> str | None:
    """Get the target user ID from punitive actions."""
    punitive = [a for a in actions if a["tool"] in ("timeout", "purge", "delete")]
    if punitive:
        return punitive[0]["args"].get("user_id")
    return None


def evaluate_result(scenario: Scenario, model_name: str, action_log: ActionLog, assessment: str, latency_ms: float, error: str | None = None) -> ScenarioResult:
    """Score a model's response to a scenario."""
    actual_action = _classify_action(action_log.actions)
    actual_target = _get_target_id(action_log.actions)

    expected_sev = ACTION_SEVERITY.get(scenario.expected_action, 0)
    actual_sev = ACTION_SEVERITY.get(actual_action, 0)

    correct_action = actual_action == scenario.expected_action
    correct_target = (
        scenario.expected_target_id is None  # no target expected
        or actual_target == scenario.expected_target_id
    )

    # Allow some flexibility: warn when timeout expected (or vice versa) is close
    # but message_only when warn expected is under-reaction
    over_reaction = actual_sev > expected_sev
    under_reaction = actual_sev < expected_sev

    return ScenarioResult(
        scenario=scenario,
        model_name=model_name,
        actions_taken=action_log.actions.copy(),
        assessment=assessment,
        correct_action=correct_action,
        correct_target=correct_target,
        over_reaction=over_reaction,
        under_reaction=under_reaction,
        latency_ms=latency_ms,
        error=error,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_scenario(scenario: Scenario, lm: dspy.LM, model_label: str) -> ScenarioResult:
    """Run a single scenario against a model."""
    action_log = ActionLog()
    tools = create_mock_tools(action_log)
    context_text = format_context_messages(scenario.context_messages)

    react_agent = dspy.ReAct(
        ModerationSignature,
        tools=tools,
        max_iters=8,
    )

    start = time.monotonic()
    try:
        with dspy.context(lm=lm):
            result = await dspy.asyncify(react_agent)(
                conversation_context=context_text,
                trigger_message=scenario.trigger_message,
                trigger_author=scenario.trigger_author,
                guild_instructions=scenario.guild_instructions,
            )

        # Enforce: if punitive actions taken but no send_mod_message, give a follow-up turn
        has_punitive = any(a["tool"] in ("timeout", "purge", "delete") for a in action_log.actions)
        has_message = any(a["tool"] == "send_mod_message" for a in action_log.actions)
        if has_punitive and not has_message:
            # Build actions summary for the follow-up
            parts = []
            for a in action_log.actions:
                if a["tool"] == "timeout":
                    parts.append(f"Timed out user {a['args']['user_id']} for {a['args']['duration']}: {a['args']['reason']}")
                elif a["tool"] == "purge":
                    parts.append(f"Purged {a['args']['count']} messages from user {a['args']['user_id']}: {a['args']['reason']}")
                elif a["tool"] == "delete":
                    parts.append(f"Deleted message {a['args']['message_id']}: {a['args']['reason']}")
            actions_summary = "\n".join(parts) if parts else "Actions were taken."

            predict = dspy.Predict(FollowUpMessageSignature)
            with dspy.context(lm=lm):
                followup = await dspy.asyncify(predict)(
                    actions_summary=actions_summary,
                    assessment=result.assessment,
                )
            action_log.actions.append({"tool": "send_mod_message", "args": {"message": followup.message}})

        latency = (time.monotonic() - start) * 1000
        return evaluate_result(scenario, model_label, action_log, result.assessment, latency)

    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        logger.exception(f"Scenario {scenario.name} failed with {model_label}: {e}")
        return evaluate_result(scenario, model_label, action_log, "", latency, error=str(e))


async def run_all_scenarios(models: dict[str, dspy.LM] | None = None) -> dict[str, list[ScenarioResult]]:
    """Run all scenarios against all models.

    Args:
        models: Dict of {label: dspy.LM}. If None, builds from MODELS_TO_TEST.

    Returns:
        Dict of {model_label: [ScenarioResult, ...]}
    """
    if models is None:
        models = {}
        for label, model_name in MODELS_TO_TEST.items():
            lm = _build_lm(model_name)
            if lm is not None:
                models[label] = lm

    if not models:
        raise RuntimeError("No models available — check API keys in .env")

    all_results: dict[str, list[ScenarioResult]] = {}

    for label, lm in models.items():
        print(f"\n{'='*60}")
        print(f"Testing: {label}")
        print(f"{'='*60}")
        results = []
        for scenario in SCENARIOS:
            print(f"  Running: {scenario.name}...", end=" ", flush=True)
            result = await run_scenario(scenario, lm, label)
            status = "PASS" if result.correct_action else ("ERROR" if result.error else "MISS")
            action = _classify_action(result.actions_taken)
            print(f"{status} (expected={scenario.expected_action}, got={action}, {result.latency_ms:.0f}ms)")
            results.append(result)
        all_results[label] = results

    return all_results


def print_comparison(all_results: dict[str, list[ScenarioResult]]):
    """Print a comparison table of all models."""
    print(f"\n{'='*80}")
    print("MODERATION AGENT MODEL COMPARISON")
    print(f"{'='*80}\n")

    # Per-scenario breakdown
    print(f"{'Scenario':<25} ", end="")
    for label in all_results:
        print(f"{'|':>2} {label:<20}", end="")
    print()
    print("-" * (27 + 23 * len(all_results)))

    for i, scenario in enumerate(SCENARIOS):
        print(f"{scenario.name:<25} ", end="")
        for label, results in all_results.items():
            r = results[i]
            action = _classify_action(r.actions_taken)
            if r.error:
                symbol = "ERR"
            elif r.correct_action:
                symbol = "OK"
            elif r.over_reaction:
                symbol = "OVER"
            else:
                symbol = "UNDER"
            print(f"| {symbol:<5} {action:<14}", end="")
        print()

    # Summary scores
    print(f"\n{'─'*80}")
    print("SUMMARY SCORES")
    print(f"{'─'*80}")

    for label, results in all_results.items():
        total_score = sum(r.score * SEVERITY_WEIGHTS[r.scenario.severity] for r in results)
        max_score = sum(SEVERITY_WEIGHTS[s.severity] for s in SCENARIOS)
        pct = (total_score / max_score) * 100 if max_score else 0

        correct = sum(1 for r in results if r.correct_action)
        over = sum(1 for r in results if r.over_reaction and not r.correct_action)
        under = sum(1 for r in results if r.under_reaction and not r.correct_action)
        errors = sum(1 for r in results if r.error)
        avg_latency = sum(r.latency_ms for r in results) / len(results) if results else 0

        print(f"\n  {label}:")
        print(f"    Score:          {total_score:.1f}/{max_score:.1f} ({pct:.0f}%)")
        print(f"    Correct:        {correct}/{len(results)}")
        print(f"    Over-reactions:  {over}")
        print(f"    Under-reactions: {under}")
        print(f"    Errors:         {errors}")
        print(f"    Avg latency:    {avg_latency:.0f}ms")

    # Detailed misses
    print(f"\n{'─'*80}")
    print("DETAILED MISSES")
    print(f"{'─'*80}")
    for label, results in all_results.items():
        misses = [r for r in results if not r.correct_action]
        if not misses:
            print(f"\n  {label}: All correct!")
            continue
        print(f"\n  {label}:")
        for r in misses:
            action = _classify_action(r.actions_taken)
            print(f"    [{r.scenario.name}] expected={r.scenario.expected_action}, got={action}")
            if r.assessment:
                # Truncate long assessments
                assessment = r.assessment[:200] + "..." if len(r.assessment) > 200 else r.assessment
                print(f"      Assessment: {assessment}")
            if r.error:
                print(f"      Error: {r.error}")


# ---------------------------------------------------------------------------
# Report generation and comparison
# ---------------------------------------------------------------------------

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "reports", "moderation")


def _result_to_dict(r: ScenarioResult) -> dict:
    """Serialize a ScenarioResult to a JSON-friendly dict."""
    return {
        "scenario": r.scenario.name,
        "description": r.scenario.description,
        "severity": r.scenario.severity,
        "expected_action": r.scenario.expected_action,
        "expected_target_id": r.scenario.expected_target_id,
        "actual_action": _classify_action(r.actions_taken),
        "actual_target_id": _get_target_id(r.actions_taken),
        "actions_taken": r.actions_taken,
        "assessment": r.assessment,
        "correct_action": r.correct_action,
        "correct_target": r.correct_target,
        "over_reaction": r.over_reaction,
        "under_reaction": r.under_reaction,
        "score": r.score,
        "latency_ms": round(r.latency_ms, 1),
        "error": r.error,
    }


def _model_summary(results: list[ScenarioResult]) -> dict:
    """Compute summary stats for a model's results."""
    total_score = sum(r.score * SEVERITY_WEIGHTS[r.scenario.severity] for r in results)
    max_score = sum(SEVERITY_WEIGHTS[s.severity] for s in SCENARIOS)
    return {
        "score": round(total_score, 2),
        "max_score": max_score,
        "score_pct": round((total_score / max_score) * 100, 1) if max_score else 0,
        "correct": sum(1 for r in results if r.correct_action),
        "total": len(results),
        "over_reactions": sum(1 for r in results if r.over_reaction and not r.correct_action),
        "under_reactions": sum(1 for r in results if r.under_reaction and not r.correct_action),
        "errors": sum(1 for r in results if r.error),
        "avg_latency_ms": round(sum(r.latency_ms for r in results) / len(results), 1) if results else 0,
    }


def save_report(all_results: dict[str, list[ScenarioResult]], label: str | None = None) -> str:
    """Save results to a timestamped JSON report file.

    Args:
        all_results: Dict of {model_label: [ScenarioResult, ...]}
        label: Optional label for this run (e.g. "baseline", "after-prompt-tweak")

    Returns:
        Path to the saved report file.
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"moderation_report_{timestamp}"
    if label:
        safe_label = label.replace(" ", "_").replace("/", "-")
        filename += f"_{safe_label}"
    filename += ".json"
    filepath = os.path.join(REPORTS_DIR, filename)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "scenario_count": len(SCENARIOS),
        "models": {},
    }

    for model_label, results in all_results.items():
        report["models"][model_label] = {
            "summary": _model_summary(results),
            "scenarios": [_result_to_dict(r) for r in results],
        }

    with open(filepath, "w") as f:
        json.dump(report, f, indent=2)

    return filepath


def load_report(filepath: str) -> dict:
    """Load a saved report from JSON."""
    with open(filepath) as f:
        return json.load(f)


def compare_reports(report_a_path: str, report_b_path: str):
    """Print a comparison between two saved reports."""
    a = load_report(report_a_path)
    b = load_report(report_b_path)

    label_a = a.get("label") or os.path.basename(report_a_path)
    label_b = b.get("label") or os.path.basename(report_b_path)

    print(f"\n{'='*80}")
    print("REPORT COMPARISON")
    print(f"{'='*80}")
    print(f"  A: {label_a} ({a['timestamp']})")
    print(f"  B: {label_b} ({b['timestamp']})")

    # Find common models
    models_a = set(a["models"].keys())
    models_b = set(b["models"].keys())
    common = models_a & models_b

    if not common:
        print(f"\n  No common models to compare.")
        print(f"  A models: {', '.join(models_a)}")
        print(f"  B models: {', '.join(models_b)}")
        return

    for model in sorted(common):
        sum_a = a["models"][model]["summary"]
        sum_b = b["models"][model]["summary"]

        score_diff = sum_b["score_pct"] - sum_a["score_pct"]
        correct_diff = sum_b["correct"] - sum_a["correct"]
        latency_diff = sum_b["avg_latency_ms"] - sum_a["avg_latency_ms"]

        arrow = "+" if score_diff > 0 else ""
        print(f"\n  {model}:")
        print(f"    Score:     {sum_a['score_pct']}% → {sum_b['score_pct']}% ({arrow}{score_diff:.1f}%)")
        print(f"    Correct:   {sum_a['correct']}/{sum_a['total']} → {sum_b['correct']}/{sum_b['total']} ({'+' if correct_diff >= 0 else ''}{correct_diff})")
        print(f"    Over:      {sum_a['over_reactions']} → {sum_b['over_reactions']}")
        print(f"    Under:     {sum_a['under_reactions']} → {sum_b['under_reactions']}")
        print(f"    Latency:   {sum_a['avg_latency_ms']}ms → {sum_b['avg_latency_ms']}ms ({'+' if latency_diff >= 0 else ''}{latency_diff:.0f}ms)")

        # Per-scenario changes
        scenarios_a = {s["scenario"]: s for s in a["models"][model]["scenarios"]}
        scenarios_b = {s["scenario"]: s for s in b["models"][model]["scenarios"]}
        changes = []
        for name in scenarios_a:
            if name in scenarios_b:
                sa = scenarios_a[name]
                sb = scenarios_b[name]
                if sa["correct_action"] != sb["correct_action"] or sa["actual_action"] != sb["actual_action"]:
                    changes.append((name, sa, sb))

        if changes:
            print(f"    Changes:")
            for name, sa, sb in changes:
                status_a = "OK" if sa["correct_action"] else ("OVER" if sa["over_reaction"] else "UNDER")
                status_b = "OK" if sb["correct_action"] else ("OVER" if sb["over_reaction"] else "UNDER")
                print(f"      {name}: {status_a}({sa['actual_action']}) → {status_b}({sb['actual_action']})")
        else:
            print(f"    No per-scenario changes.")

    # Models only in one report
    only_a = models_a - models_b
    only_b = models_b - models_a
    if only_a:
        print(f"\n  Models only in A: {', '.join(only_a)}")
    if only_b:
        print(f"\n  Models only in B: {', '.join(only_b)}")


# ---------------------------------------------------------------------------
# Pytest integration
# ---------------------------------------------------------------------------

def _get_available_models() -> dict[str, dspy.LM]:
    """Get all models that have valid API keys."""
    import dotenv
    dotenv.load_dotenv()

    models = {}
    for label, model_name in MODELS_TO_TEST.items():
        lm = _build_lm(model_name)
        if lm is not None:
            models[label] = lm
    return models


@pytest.fixture(scope="module")
def available_models():
    return _get_available_models()


@pytest.mark.llm
@pytest.mark.parametrize("model_key", list(MODELS_TO_TEST.keys()))
@pytest.mark.asyncio
async def test_moderation_model(model_key, available_models):
    """Test a single model against all scenarios."""
    if model_key not in available_models:
        pytest.skip(f"No API key for {model_key}")

    lm = available_models[model_key]
    results = []
    for scenario in SCENARIOS:
        result = await run_scenario(scenario, lm, model_key)
        results.append(result)

    # Print results for this model
    correct = sum(1 for r in results if r.correct_action)
    total = len(results)
    print(f"\n{model_key}: {correct}/{total} correct")
    for r in results:
        action = _classify_action(r.actions_taken)
        status = "PASS" if r.correct_action else "MISS"
        print(f"  [{status}] {r.scenario.name}: expected={r.scenario.expected_action}, got={action}")

    # Save report for this model run
    report_path = save_report({model_key: results}, label=f"pytest_{model_key}")
    print(f"  Report saved: {report_path}")

    # Don't hard-fail — this is an evaluation, not a pass/fail test
    # But warn if score is very low
    score_pct = (sum(r.score for r in results) / len(results)) * 100
    if score_pct < 30:
        pytest.xfail(f"{model_key} scored only {score_pct:.0f}% — likely not suitable for moderation")


# ---------------------------------------------------------------------------
# Standalone execution
# ---------------------------------------------------------------------------

async def main():
    """Run the full comparison as a standalone script."""
    import argparse
    import dotenv
    dotenv.load_dotenv()

    parser = argparse.ArgumentParser(description="Moderation agent model comparison")
    parser.add_argument("--compare", nargs=2, metavar=("REPORT_A", "REPORT_B"),
                        help="Compare two saved report JSON files instead of running tests")
    parser.add_argument("--label", type=str, default=None,
                        help="Label for this run (e.g. 'baseline', 'after-prompt-tweak')")
    parser.add_argument("--models", nargs="*", default=None,
                        help="Only test specific models (e.g. --models claude-haiku gemini-flash-lite)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    if args.compare:
        compare_reports(args.compare[0], args.compare[1])
        return

    print("Moderation Agent Model Comparison")
    print("=" * 40)

    # Filter models if requested
    models_to_run = MODELS_TO_TEST
    if args.models:
        models_to_run = {k: v for k, v in MODELS_TO_TEST.items() if k in args.models}
        if not models_to_run:
            print(f"No matching models. Available: {', '.join(MODELS_TO_TEST.keys())}")
            return

    print(f"Scenarios: {len(SCENARIOS)}")
    print(f"Models: {', '.join(models_to_run.keys())}")
    print()

    # Build LMs for selected models
    lms = {}
    for label, model_name in models_to_run.items():
        lm = _build_lm(model_name)
        if lm is not None:
            lms[label] = lm

    results = await run_all_scenarios(lms)
    print_comparison(results)

    # Save report
    report_path = save_report(results, label=args.label)
    print(f"\nReport saved to: {report_path}")
    print(f"Compare with: python {__file__} --compare {report_path} <other_report.json>")


if __name__ == "__main__":
    asyncio.run(main())
