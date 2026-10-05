"""Deleting what a member made on the website, one item at a time or all at once.

Three kinds of thing a signed-in member makes here can be deleted from the
site: Chat conversations, questions about our resources, and dashboard web
searches. Each delete removes the stored rows and every copy the site keeps of
them, not just the listing:

- a Chat conversation: its turns, messages, documents, threads, compactions,
  sub-agents and runtime events (by cascade), its uploaded files in object
  storage, the copies of the model's replies kept in ``usage_cost_rows.details``
  for crash recovery, and its ``work_dispatches`` rows;
- a question about our resources: the conversation, its messages and its runs
  (by cascade), the progress notifications Skrift queued for the browser
  (``stored_notifications``) and its ``work_dispatches`` rows;
- a dashboard web search: the run row, which holds the request, queries,
  results and answer, and its ``work_dispatches`` row.

The cost rows themselves stay, holding no content: they are what usage limits
and spend are counted from. Skrift's worker tables keep no content of finished
work (``smarter_dev/web/worker_state_store.py``,
``smarter_dev/web/agent_session_cleanup.py``).

Something still being worked on is not deleted: the worker writing it would
fail or recreate rows. A single delete refuses it; a delete-all skips it and
says how many it left. A Resources run or a search that has not changed for
:data:`STALLED_AFTER` is no longer being worked on, whatever its status says,
so a run its worker abandoned never blocks a delete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID

from skrift.db.models.notification import StoredNotification
from skrift.db.models.push_subscription import PushSubscription
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import AgentConversation
from smarter_dev.web.models import ResourceAgentRun
from smarter_dev.web.models import UsageCostRow
from smarter_dev.web.models import WebChatAttachment
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatSubagent
from smarter_dev.web.models import WebChatTurn
from smarter_dev.web.models import WebSearchLink
from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.models import WorkDispatch

ACTIVE_TURN_STATUSES = ("submitted", "queued", "running", "stopping")
STALLED_AFTER = timedelta(minutes=15)
ACTIVE_RESOURCE_RUN_STATUSES = ("submitted", "running")
FINISHED_SEARCH_STATUSES = ("complete", "error")

# What a chat usage row keeps of a model reply so a crashed turn can be
# replayed without paying for it twice. Useless once the conversation is gone.
_REPLY_COPIES = ("model_response", "durable_delta")


class StillRunning(Exception):
    """The item is still being worked on; stop it or wait, then delete."""


class UploadsStranded(Exception):
    """Some uploaded files could not be removed; the conversation stays."""


@dataclass(frozen=True)
class DeleteAllResult:
    deleted: int
    running: int
    # Chats whose uploads could not all be removed; they stay, retryable.
    stranded: int = 0


def _recent(changed_at: datetime | None) -> bool:
    if changed_at is None:
        return False
    if changed_at.tzinfo is None:  # SQLite hands timestamps back naive
        changed_at = changed_at.replace(tzinfo=UTC)
    return changed_at > datetime.now(UTC) - STALLED_AFTER


def _lease_held(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > datetime.now(UTC)


async def forget_dispatches(
    session: AsyncSession, job_type: str, aggregate_ids: list[UUID]
) -> None:
    if aggregate_ids:
        await session.execute(
            delete(WorkDispatch).where(
                WorkDispatch.job_type == job_type,
                WorkDispatch.aggregate_id.in_(aggregate_ids),
            )
        )


# ── Chat ─────────────────────────────────────────────────────────────


async def chat_is_running(session: AsyncSession, conversation_id: UUID) -> bool:
    return (
        await session.scalar(
            select(WebChatTurn.id).where(
                WebChatTurn.conversation_id == conversation_id,
                WebChatTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        )
    ) is not None


async def delete_chat_conversation(
    session: AsyncSession, storage, conversation: WebChatConversation
) -> None:
    """Destroy one conversation and everything hanging off it.

    The uploaded objects are outside the database and have to be removed by
    hand. They are marked ``deleting`` first, so a failure part way through
    leaves them to the periodic orphan reconciler instead of stranding private
    files with no row pointing at them, and the conversation itself survives,
    so the owner can simply try again (:class:`UploadsStranded`).
    """
    if await chat_is_running(session, conversation.id):
        raise StillRunning
    conversation_id = conversation.id
    attachments = list(
        (
            await session.execute(
                select(WebChatAttachment).where(
                    WebChatAttachment.conversation_id == conversation_id
                )
            )
        ).scalars()
    )
    if attachments:
        for attachment in attachments:
            attachment.status = "deleting"
        await session.commit()
        stranded = 0
        for attachment in attachments:
            try:
                await storage.delete(attachment.storage_key)
            except Exception:
                stranded += 1
                continue
            await session.delete(attachment)
        await session.commit()
        if stranded:
            raise UploadsStranded
    # Committing the uploads released any lock the caller held: take it again,
    # so a message sent meanwhile (its turn starts under this lock) is either
    # seen here or never starts.
    await session.execute(
        select(WebChatConversation.id)
        .where(WebChatConversation.id == conversation_id)
        .with_for_update()
    )
    if await chat_is_running(session, conversation_id):
        raise StillRunning
    turn_ids = list(
        (
            await session.execute(
                select(WebChatTurn.id).where(
                    WebChatTurn.conversation_id == conversation_id
                )
            )
        ).scalars()
    )
    subagent_ids = (
        list(
            (
                await session.execute(
                    select(WebChatSubagent.id).where(
                        WebChatSubagent.root_turn_id.in_(turn_ids)
                    )
                )
            ).scalars()
        )
        if turn_ids
        else []
    )
    for row in (
        await session.execute(
            select(UsageCostRow).where(UsageCostRow.conversation_id == conversation_id)
        )
    ).scalars():
        if any(key in (row.details or {}) for key in _REPLY_COPIES):
            row.details = {
                key: value
                for key, value in row.details.items()
                if key not in _REPLY_COPIES
            }
    await forget_dispatches(session, "chat.turn.run", turn_ids)
    await forget_dispatches(session, "chat.subagent.run", subagent_ids)
    await session.delete(conversation)
    await session.commit()


async def delete_all_chats(
    session: AsyncSession, storage, user_id: UUID
) -> DeleteAllResult:
    """Every conversation the member owns, the Quick chat and archived ones included."""
    conversations = list(
        (
            await session.execute(
                select(WebChatConversation).where(
                    WebChatConversation.owner_user_id == user_id
                )
            )
        ).scalars()
    )
    deleted = running = stranded = 0
    for conversation in conversations:
        try:
            await delete_chat_conversation(session, storage, conversation)
        except StillRunning:
            running += 1
            continue
        except UploadsStranded:
            stranded += 1
            continue
        deleted += 1
    return DeleteAllResult(deleted=deleted, running=running, stranded=stranded)


# ── Questions about our resources ────────────────────────────────────


async def delete_resources_conversation(
    session: AsyncSession, conversation: AgentConversation
) -> None:
    runs = list(
        (
            await session.execute(
                select(
                    ResourceAgentRun.id,
                    ResourceAgentRun.status,
                    ResourceAgentRun.updated_at,
                    ResourceAgentRun.worker_lease_expires_at,
                )
                .where(ResourceAgentRun.conversation_id == conversation.id)
                # A worker takes a run under this lock, so it either sees the
                # run gone or this sees its lease.
                .with_for_update()
            )
        ).all()
    )
    if any(
        run.status in ACTIVE_RESOURCE_RUN_STATUSES
        and (_recent(run.updated_at) or _lease_held(run.worker_lease_expires_at))
        for run in runs
    ):
        raise StillRunning
    # The progress the browser was shown (the restated question, the research
    # steps) is queued for a day under the owner's notification source.
    await session.execute(
        delete(StoredNotification).where(
            StoredNotification.source_key == f"user:{conversation.owner_user_id}",
            StoredNotification.payload_json.contains(str(conversation.id)),
        )
    )
    await forget_dispatches(session, "resources.agent.run", [run.id for run in runs])
    await session.delete(conversation)
    await session.commit()


async def delete_all_resources_conversations(
    session: AsyncSession, user_id: UUID
) -> DeleteAllResult:
    conversations = list(
        (
            await session.execute(
                select(AgentConversation).where(
                    AgentConversation.owner_user_id == user_id
                )
            )
        ).scalars()
    )
    deleted = running = 0
    for conversation in conversations:
        try:
            await delete_resources_conversation(session, conversation)
        except StillRunning:
            running += 1
            continue
        deleted += 1
    return DeleteAllResult(deleted=deleted, running=running)


# ── Dashboard web searches ───────────────────────────────────────────


async def delete_search(session: AsyncSession, run: WebSearchRun) -> None:
    if run.status not in FINISHED_SEARCH_STATUSES and _recent(run.updated_at):
        raise StillRunning
    await forget_dispatches(session, "web_search.run", [run.id])
    await session.delete(run)
    await session.commit()


async def delete_all_searches(session: AsyncSession, user_id: UUID) -> DeleteAllResult:
    runs = list(
        (
            await session.execute(
                select(WebSearchRun).where(WebSearchRun.owner_user_id == user_id)
            )
        ).scalars()
    )
    deleted = running = 0
    for run in runs:
        try:
            await delete_search(session, run)
        except StillRunning:
            running += 1
            continue
        deleted += 1
    return DeleteAllResult(deleted=deleted, running=running)


# ── Account deletion ─────────────────────────────────────────────────


async def delete_account_leftovers(session: AsyncSession, user_id: UUID) -> None:
    """What deleting the account's user row does not reach by cascade.

    Run in the account deletion's own transaction, before the user row goes:
    dashboard searches, the search link and push subscriptions have no foreign
    key to the user, ``work_dispatches`` rows name only the work they
    dispatched (a Resources one holds the question in full), and queued
    notifications are keyed by a string. The dispatch rows are found through
    the work they name, so this has to run while that work still exists.
    """
    turn_ids = list(
        (
            await session.execute(
                select(WebChatTurn.id)
                .join(
                    WebChatConversation,
                    WebChatConversation.id == WebChatTurn.conversation_id,
                )
                .where(WebChatConversation.owner_user_id == user_id)
            )
        ).scalars()
    )
    subagent_ids = (
        list(
            (
                await session.execute(
                    select(WebChatSubagent.id).where(
                        WebChatSubagent.root_turn_id.in_(turn_ids)
                    )
                )
            ).scalars()
        )
        if turn_ids
        else []
    )
    run_ids = list(
        (
            await session.execute(
                select(ResourceAgentRun.id).where(
                    ResourceAgentRun.owner_user_id == user_id
                )
            )
        ).scalars()
    )
    search_ids = list(
        (
            await session.execute(
                select(WebSearchRun.id).where(WebSearchRun.owner_user_id == user_id)
            )
        ).scalars()
    )
    await forget_dispatches(session, "chat.turn.run", turn_ids)
    await forget_dispatches(session, "chat.subagent.run", subagent_ids)
    await forget_dispatches(session, "resources.agent.run", run_ids)
    await forget_dispatches(session, "web_search.run", search_ids)
    await session.execute(
        delete(WebSearchRun).where(WebSearchRun.owner_user_id == user_id)
    )
    await session.execute(
        delete(WebSearchLink).where(WebSearchLink.owner_user_id == user_id)
    )
    await session.execute(
        delete(PushSubscription).where(PushSubscription.user_id == str(user_id))
    )
    await session.execute(
        delete(StoredNotification).where(
            StoredNotification.source_key == f"user:{user_id}"
        )
    )
