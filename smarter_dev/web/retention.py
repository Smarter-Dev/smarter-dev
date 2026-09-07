"""Scheduled scrubbing of the text our AI features derived from Discord.

Verbatim Discord message text does not reach these tables. Every row that
would otherwise carry it is written with the placeholder
:data:`~smarter_dev.shared.message_content.MESSAGE_CONTENT_PLACEHOLDER`
(``[message content]``) at write time, in the web tier, at the moment the row
is built — see ``docs/data-retention.md`` for the per-table list. Verbatim text
survives in the agents' own working history — the chat agent's Redis history
and the proactive agent's history, with a recovery copy in
``proactive_agent_histories`` — and in one column no write-time rule can cover,
``chat_agent_errors.provider_body``, which this sweep clears. What bounds each
of those, and what does not, is ``docs/data-retention.md``'s to state; this
docstring does not repeat it. The proactive Redis streams that carry
notification envelopes are trimmed to the same
:data:`~smarter_dev.shared.message_content.CONTENT_RETENTION_WINDOW` (48 hours)
by the bot, not by this sweep.

What this sweep owns is the other half: text an AI *wrote* about what it read.
A summary quotes nobody and describes everything, so it gets the same 48-hour
window message text used to get — ``agent_output``, the compaction ``summary``,
``ai_context_summary``, ``provider_body``, ``bot_response``,
``response_content``, ``decision_reason``, and a handler script's own error
message. The sweep is also the back-fill path for rows written before write-time
redaction landed, which is why it still nulls the columns the write path now
placeholders. Each scrubbed row is stamped ``content_purged_at``; the row
itself stays — timestamps, token counts, cost, model name, the decision the
agent took — so cost dashboards and abuse monitoring keep their long history
without keeping anyone's words.

Moderation auditing of what was actually said uses the audit log the bot posts
to the guild's activity channel, not these rows.

What is deliberately *not* swept here:

- ``quest_submissions`` / ``challenge_submissions`` — typed into one of our
  modals, an obvious submission to us.
- ``agent_messages`` / ``agent_conversations`` — the website's own agent chat,
  not Discord.
- ``research_sessions`` — the ``/scan`` query is an explicit command argument
  and the results are a user-facing artifact with its own lifecycle.
- ``proactive_agent_histories`` — the proactive agent's working history, bounded
  as ``docs/data-retention.md`` describes.
- Identity fields (user ids, usernames, display names) and Discord snowflakes.
  Those come from the members intent, not the message-content intent, and the
  audit trail is worthless without knowing who an action was about.

The chat agent's three-layer memory — ``chat_agent_guild_memory``,
``chat_agent_memory_notes`` and ``chat_agent_memory_revisions`` — is exempt by
design, and the exemption is pinned by a test in ``tests/web/test_retention.py``.
The blob and its revision history are prose the agent wrote *about itself*
rather than message text it read: no verbatim quotes, no private or sensitive
detail, only who these people are to it. Scrubbing that on a 48-hour window
would not protect anyone's words, it would just give the bot amnesia every
other day. The mid-term notes are exempt for a different reason — they are
deleted outright by the nightly dream session that consumes them, so they live
well under a day by construction and never reach a retention cutoff. Adding a
scrubber for any of the three needs the same explicit sign-off the exemption
got, plus a matching row in ``docs/data-retention.md``.

Every scrubber is idempotent: rows already stamped ``content_purged_at`` are
skipped, so re-running the sweep costs one indexed lookup per table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.message_content import redact_trigger_context
from smarter_dev.web.models import (
    CONTENT_RETENTION_WINDOW,
    ChatAgentCompactionEvent,
    ChatAgentEngagement,
    ChatAgentError,
    ChatAgentTurn,
    ForumAgentResponse,
    HandlerRun,
    HelpConversation,
    ModerationAction,
)

logger = logging.getLogger(__name__)

# How many handler_runs rows to rewrite per round trip. The trigger_context
# scrub is the one that cannot be expressed as a single UPDATE (it edits keys
# inside a JSON blob), so it streams in batches instead of loading a busy
# guild's full 48 hours into memory at once.
_HANDLER_RUN_BATCH = 500


def cutoff_for(now: datetime) -> datetime:
    """The timestamp at which content becomes due for scrubbing."""
    return now - CONTENT_RETENTION_WINDOW


@dataclass
class SweepResult:
    """Per-table counts of rows scrubbed by one sweep."""

    cutoff: datetime
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def __str__(self) -> str:
        if not self.total:
            return f"nothing due (cutoff {self.cutoff.isoformat()})"
        scrubbed = ", ".join(
            f"{table}={count}" for table, count in sorted(self.counts.items()) if count
        )
        return f"{self.total} rows scrubbed (cutoff {self.cutoff.isoformat()}): {scrubbed}"


async def _scrub(
    session: AsyncSession,
    model: Any,
    *,
    timestamp_column: Any,
    cutoff: datetime,
    now: datetime,
    values: dict[str, Any],
) -> int:
    """Blank ``values`` on every unpurged row older than ``cutoff``.

    Returns the number of rows touched. Does not commit — :func:`run_retention_sweep`
    owns the transaction boundary.
    """
    result = await session.execute(
        update(model)
        .where(
            timestamp_column <= cutoff,
            model.content_purged_at.is_(None),
        )
        .values(**values, content_purged_at=now)
    )
    return result.rowcount or 0


async def scrub_help_conversations(
    session: AsyncSession, cutoff: datetime, now: datetime
) -> int:
    """Drop the question, the answer and the surrounding channel context.

    Keeps the session/guild/user ids, the interaction type, token count and
    latency — everything the help-agent usage dashboards read.
    """
    return await _scrub(
        session,
        HelpConversation,
        timestamp_column=HelpConversation.created_at,
        cutoff=cutoff,
        now=now,
        values={
            "user_question": "",
            "bot_response": "",
            "context_messages": [],
        },
    )


async def scrub_chat_agent_turns(
    session: AsyncSession, cutoff: datetime, now: datetime
) -> int:
    """Drop the messages that fired a turn and the model transcript delta.

    ``agent_output`` goes too: it carries the reply text plus the agent's
    running topic/notes, all of it derived from what people said.
    """
    return await _scrub(
        session,
        ChatAgentTurn,
        timestamp_column=ChatAgentTurn.started_at,
        cutoff=cutoff,
        now=now,
        values={
            "triggering_messages": [],
            "agent_output": {},
            "model_messages_delta": None,
        },
    )


async def scrub_chat_agent_engagements(
    session: AsyncSession, cutoff: datetime, now: datetime
) -> int:
    """Drop the denormalised topic/notes an engagement carries for the list view."""
    return await _scrub(
        session,
        ChatAgentEngagement,
        timestamp_column=ChatAgentEngagement.started_at,
        cutoff=cutoff,
        now=now,
        values={
            "last_topic": None,
            "last_notes": None,
        },
    )


async def scrub_chat_agent_compaction_events(
    session: AsyncSession, cutoff: datetime, now: datetime
) -> int:
    """Drop compacted content and its summary; keep the char-count arithmetic.

    Compaction events hang off a turn and have no timestamp of their own, so
    they inherit their turn's ``started_at``.
    """
    due_turns = select(ChatAgentTurn.id).where(ChatAgentTurn.started_at <= cutoff)
    result = await session.execute(
        update(ChatAgentCompactionEvent)
        .where(
            ChatAgentCompactionEvent.turn_id.in_(due_turns),
            ChatAgentCompactionEvent.content_purged_at.is_(None),
        )
        .values(original_content="", summary="", content_purged_at=now)
    )
    return result.rowcount or 0


async def scrub_chat_agent_errors(
    session: AsyncSession, cutoff: datetime, now: datetime
) -> int:
    """Drop the raw provider error body, which can echo the prompt back."""
    return await _scrub(
        session,
        ChatAgentError,
        timestamp_column=ChatAgentError.occurred_at,
        cutoff=cutoff,
        now=now,
        values={"provider_body": None},
    )


async def scrub_forum_agent_responses(
    session: AsyncSession, cutoff: datetime, now: datetime
) -> int:
    """Drop the evaluated forum post, its attachments and the agent's reply.

    A forum post is message content like any other — the agent sees it through
    the same intent — so it gets the same window. The confidence score, token
    count and whether the agent responded all survive.
    """
    return await _scrub(
        session,
        ForumAgentResponse,
        timestamp_column=ForumAgentResponse.created_at,
        cutoff=cutoff,
        now=now,
        values={
            "post_title": "",
            "post_content": "",
            "attachments": [],
            "decision_reason": "",
            "response_content": "",
        },
    )


async def scrub_moderation_actions(
    session: AsyncSession, cutoff: datetime, now: datetime
) -> int:
    """Drop the AI's narrative of the exchange; keep the action record.

    ``ai_context_summary`` is a retelling of what people said in the channel,
    so it goes. ``reason`` stays whoever wrote it: it is the justification for
    an action *we* took — DM'd to the target and posted to the mod log — not a
    chat message, and the moderation log is the one place that record has to
    survive.
    """
    return await _scrub(
        session,
        ModerationAction,
        timestamp_column=ModerationAction.created_at,
        cutoff=cutoff,
        now=now,
        values={"ai_context_summary": None},
    )


_SCRIPT_AUTHORED_ERROR_OUTCOMES = ("error", "cap_exceeded")


async def scrub_handler_runs(
    session: AsyncSession, cutoff: datetime, now: datetime
) -> int:
    """Redact message text out of every due handler run's trigger context.

    Streams in batches: ``trigger_context`` is a JSON blob whose content keys
    have to be rewritten key-by-key, so this is a read-modify-write rather than
    a single UPDATE. Rows written since the write-time redaction landed already
    hold placeholders; re-redacting them is a no-op.

    ``error`` is cleared with it when a script wrote it. A script that trips
    over the message it is reacting to puts that text into its exception
    message, and nothing at write time can tell which errors quote a member —
    so it is treated like every other derived text the sweep owns. The
    host-authored explanations on ``skipped`` and ``rearmed`` rows name no
    member and stay: they are the only record of why a chain stalled. The
    outcome and every counter stay too, so a long-dead failure is still visible
    as a failure.
    """
    scrubbed = 0
    while True:
        due = await session.execute(
            select(HandlerRun.id, HandlerRun.trigger_context)
            .where(
                HandlerRun.fired_at <= cutoff,
                HandlerRun.content_purged_at.is_(None),
            )
            .limit(_HANDLER_RUN_BATCH)
        )
        rows = due.all()
        if not rows:
            return scrubbed

        for run_id, context in rows:
            await session.execute(
                update(HandlerRun)
                .where(HandlerRun.id == run_id)
                .values(
                    trigger_context=redact_trigger_context(context or {}),
                    error=case(
                        (HandlerRun.outcome.in_(_SCRIPT_AUTHORED_ERROR_OUTCOMES), None),
                        else_=HandlerRun.error,
                    ),
                    content_purged_at=now,
                )
            )
        scrubbed += len(rows)

        if len(rows) < _HANDLER_RUN_BATCH:
            return scrubbed


# Table name -> scrubber. Ordered as they run; the name is what shows up in the
# sweep log and in the operator-facing summary.
SCRUBBERS: dict[str, Any] = {
    "help_conversations": scrub_help_conversations,
    "chat_agent_turns": scrub_chat_agent_turns,
    "chat_agent_engagements": scrub_chat_agent_engagements,
    "chat_agent_compaction_events": scrub_chat_agent_compaction_events,
    "chat_agent_errors": scrub_chat_agent_errors,
    "forum_agent_responses": scrub_forum_agent_responses,
    "moderation_actions": scrub_moderation_actions,
    "handler_runs": scrub_handler_runs,
}


async def run_retention_sweep(
    session: AsyncSession, now: datetime | None = None
) -> SweepResult:
    """Scrub every table whose retention window has elapsed.

    Commits per table rather than once at the end. Purging is monotonic — a
    scrubbed row never needs un-scrubbing — so there is nothing to gain from
    all-or-nothing, and plenty to lose: the very first run has to work through
    however much history predates the sweep, and a run that dies partway
    through should keep the tables it finished rather than roll them back and
    hand the next run the same oversized job.
    """
    now = now or datetime.now(UTC)
    cutoff = cutoff_for(now)
    result = SweepResult(cutoff=cutoff)

    for table, scrubber in SCRUBBERS.items():
        result.counts[table] = await scrubber(session, cutoff, now)
        await session.commit()

    logger.info("retention sweep: %s", result)
    return result
