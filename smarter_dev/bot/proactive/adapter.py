"""The two-pass ProactiveBotAdapter: watcher gate, then the K3 agent."""

from __future__ import annotations

from dataclasses import dataclass

from smarter_dev.bot.proactive.agent import (
    AgentDeps,
    KimiAgentRunner,
    ToolBudget,
)
from smarter_dev.bot.proactive.environment import (
    ChannelEnvironment,
    InstructionStore,
    WakeActions,
)
from smarter_dev.bot.proactive.notifications import (
    Notification,
    NotificationQueue,
    mention_notification,
    render_notifications,
    reply_notification,
    watcher_summary_notification,
)
from smarter_dev.bot.proactive.types import (
    ActivationContext,
    ActivationResult,
)
from smarter_dev.bot.proactive.watcher import (
    SkimRunner,
    WatcherDecision,
    WatcherRunner,
)

WATCHER_CONTEXT_SIZE = 30


def bot_directed_message_ids(
    new_messages: list, env: ChannelEnvironment, bot_user_id: str
) -> list[str]:
    """Messages that mention the bot or reply to one of its messages.

    These wake the agent deterministically — no LLM judgment involved.
    """
    directed = []
    for message in new_messages:
        if bot_user_id in message.mention_user_ids:
            directed.append(message.id)
            continue
        if message.reply_to_id is not None:
            target = env.lookup(message.reply_to_id)
            if target is not None and (
                target.is_bot or target.author_id == bot_user_id
            ):
                directed.append(message.id)
    return directed


def engagement_notifications(
    new_messages: list, env: ChannelEnvironment, bot_user_id: str
) -> list[Notification]:
    """Waking notifications for messages that engage the bot directly."""
    produced = []
    for message in new_messages:
        if bot_user_id in message.mention_user_ids:
            produced.append(mention_notification(message))
            continue
        if message.reply_to_id is not None:
            target = env.lookup(message.reply_to_id)
            if target is not None and (
                target.is_bot or target.author_id == bot_user_id
            ):
                produced.append(reply_notification(message, target))
    return produced


def render_active_instructions(store: InstructionStore) -> str:
    """The agent's own standing watch instructions, shown every wake.

    Seeing the mechanism in its live state each wake is what makes the agent
    actually use it — a tool mentioned only in the system prompt stays
    theoretical.
    """
    if not store.entries:
        return "YOUR WATCH INSTRUCTIONS: none set."
    lines = "\n".join(
        f"- {e.instruction_id} (expires {e.expires_at:%H:%M} UTC): {e.text}"
        for e in store.entries
    )
    return f"YOUR WATCH INSTRUCTIONS (active):\n{lines}"


def build_wake_brief(
    notifications: list[Notification], dropped: int, store: InstructionStore
) -> str:
    return f"""\
{render_notifications(notifications, dropped)}

{render_active_instructions(store)}

A notification is a lead, not the full story — pull context with your tools
when it isn't enough. Act per your policy (or deliberately don't).

Before you finish: the watcher will not wake you again except for direct
engagement or activity it independently judges interesting. If anything here
deserves a follow-up you would otherwise never hear about — someone said
they'd report back, a thread you want to see resolved, a topic worth
catching — call set_watch_instruction now with a TTL. Then finish with a
one-sentence note on what you did and why."""


def _merge_usage(usage_by_model: dict, model_id: str, usage: dict) -> None:
    entry = usage_by_model.setdefault(
        model_id, {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
    )
    for key, value in usage.items():
        entry[key] += value


@dataclass
class TwoPassAdapter:
    watcher: WatcherRunner
    agent_runner: KimiAgentRunner
    skim: SkimRunner
    instruction_store: InstructionStore
    watcher_model_id: str
    agent_model_id: str
    context_size: int = WATCHER_CONTEXT_SIZE
    # Shown to the watcher so name-mentions ("hey smarter dev…") register as
    # directed at the bot even without an @.
    bot_display_name: str = "the bot"
    # Builds the deps object handed to the agent's tools. The default is the
    # eval's AgentDeps; the production plugin injects a factory that returns
    # ProactiveDeps carrying the live bot/channel for the parity tools.
    deps_factory: object = None
    # Prepended to the wake brief when non-empty. The plugin uses it for the
    # hourly memory refresh: since the agent's history persists across wakes,
    # the block only needs to ride one brief per refresh.
    brief_preamble: str = ""
    # Queue-only notifications (mode changes, expiries, non-wake watcher
    # summaries) accumulate here and drain into the next wake's brief. None
    # means no queue (bare eval runs).
    notification_queue: NotificationQueue | None = None

    async def activate(self, context: ActivationContext) -> ActivationResult:
        usage_by_model: dict[str, dict] = {}
        env = ChannelEnvironment(
            visible=[*context.history, *context.new_messages],
            bot_user_id=context.bot_user_id,
        )
        wake_notifications = engagement_notifications(
            context.new_messages, env, context.bot_user_id
        )
        if wake_notifications:
            forced_ids = [n.message_ids[0] for n in wake_notifications]
            decision = WatcherDecision(
                wake=True,
                reason="bot mentioned or replied to (deterministic wake)",
                relevant_message_ids=forced_ids,
                summary=(
                    "A new message mentions the bot or replies to one of its "
                    "messages."
                ),
            )
            details: dict = {
                "watcher": {**decision.model_dump(), "deterministic": True}
            }
        else:
            decision, watcher_usage = await self.watcher.decide(
                instructions=self.instruction_store.current(),
                context_transcript=env.render(
                    context.history[-self.context_size :]
                ),
                new_transcript=env.render(context.new_messages),
                bot_user_id=context.bot_user_id,
                bot_display_name=self.bot_display_name,
            )
            _merge_usage(usage_by_model, self.watcher_model_id, watcher_usage)
            details = {"watcher": decision.model_dump()}
            if decision.wake:
                # Non-waking watcher summaries are deliberately discarded —
                # only waking activity reaches the agent.
                wake_notifications.append(
                    watcher_summary_notification(
                        summary=decision.summary or decision.reason,
                        message_ids=list(decision.relevant_message_ids),
                        wake=True,
                        created_at=context.activated_at,
                    )
                )

        actions = WakeActions()
        if decision.wake:

            async def skim_transcript(transcript: str) -> str:
                text, skim_usage = await self.skim.skim(transcript)
                _merge_usage(usage_by_model, self.watcher_model_id, skim_usage)
                return text

            build_deps = self.deps_factory or AgentDeps
            deps = build_deps(
                env=env,
                actions=actions,
                instruction_store=self.instruction_store,
                skim_transcript=skim_transcript,
                budget=ToolBudget(),
            )
            pending, dropped = (
                self.notification_queue.drain()
                if self.notification_queue is not None
                else ([], 0)
            )
            brief = build_wake_brief(
                pending + wake_notifications, dropped, self.instruction_store
            )
            if self.brief_preamble:
                brief = f"{self.brief_preamble}\n\n{brief}"
            note, agent_usage = await self.agent_runner.wake(brief, deps)
            _merge_usage(usage_by_model, self.agent_model_id, agent_usage)
            details["agent"] = {
                "tool_calls": deps.budget.used,
                "note": str(note)[:300],
            }
            details["watch_instruction_updates"] = self.instruction_store.updates

        totals = {
            key: sum(entry[key] for entry in usage_by_model.values())
            for key in ("input_tokens", "output_tokens", "cache_read_tokens")
        }
        return ActivationResult(
            responses=list(actions.sent),
            input_tokens=totals["input_tokens"],
            output_tokens=totals["output_tokens"],
            cache_read_tokens=totals["cache_read_tokens"],
            model_id=self.agent_model_id,
            reactions=tuple(actions.reactions),
            usage_by_model=usage_by_model,
            details=details,
        )
