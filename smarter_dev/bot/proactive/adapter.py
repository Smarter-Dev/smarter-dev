"""The two-pass ProactiveBotAdapter: watcher gate, then the K3 agent."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from smarter_dev.bot.proactive.agent import AgentDeps
from smarter_dev.bot.proactive.agent import KimiAgentRunner
from smarter_dev.bot.proactive.agent import ToolBudget
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.environment import WakeActions
from smarter_dev.bot.proactive.notifications import Notification
from smarter_dev.bot.proactive.notifications import NotificationQueue
from smarter_dev.bot.proactive.notifications import mention_notification
from smarter_dev.bot.proactive.notifications import render_notifications
from smarter_dev.bot.proactive.notifications import reply_notification
from smarter_dev.bot.proactive.notifications import watcher_summary_notification
from smarter_dev.bot.proactive.types import ActivationContext
from smarter_dev.bot.proactive.types import ActivationResult
from smarter_dev.bot.proactive.watcher import SkimRunner
from smarter_dev.bot.proactive.watcher import WatcherDecision
from smarter_dev.bot.proactive.watcher import WatcherRunner

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
            # Our own id only: a reply to some other bot in the channel is
            # not someone engaging us.
            if target is not None and target.author_id == bot_user_id:
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
            # Our own id only: a reply to some other bot in the channel is
            # not someone engaging us.
            if target is not None and target.author_id == bot_user_id:
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
class WatcherProducer:
    watcher: WatcherRunner
    instruction_store: InstructionStore
    watcher_model_id: str
    notification_queue: NotificationQueue
    context_size: int = WATCHER_CONTEXT_SIZE
    bot_display_name: str = "the bot"
    details: dict = field(default_factory=dict, init=False)
    wake_produced: bool = field(default=False, init=False)

    async def produce(self, context: ActivationContext) -> dict[str, dict]:
        usage_by_model: dict[str, dict] = {}
        self.wake_produced = False
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
                    "A new message mentions the bot or replies to one of its messages."
                ),
            )
            details: dict = {
                "watcher": {**decision.model_dump(), "deterministic": True}
            }
            for notification in wake_notifications:
                self.notification_queue.push(notification)
            self.wake_produced = True
        else:
            decision, watcher_usage = await self.watcher.decide(
                instructions=self.instruction_store.current(),
                context_transcript=env.render(context.history[-self.context_size :]),
                new_transcript=env.render(context.new_messages),
                bot_user_id=context.bot_user_id,
                bot_display_name=self.bot_display_name,
            )
            _merge_usage(usage_by_model, self.watcher_model_id, watcher_usage)
            details = {"watcher": decision.model_dump()}
            if decision.wake:
                # Non-waking watcher summaries are deliberately discarded —
                # only waking activity reaches the agent.
                self.notification_queue.push(
                    watcher_summary_notification(
                        summary=decision.summary or decision.reason,
                        message_ids=list(decision.relevant_message_ids),
                        wake=True,
                        created_at=context.activated_at,
                    )
                )
                self.wake_produced = True

        self.details = details
        return usage_by_model

    async def activate(self, context: ActivationContext) -> dict[str, dict]:
        return await self.produce(context)


@dataclass
class AgentConsumer:
    agent_runner: KimiAgentRunner
    skim: SkimRunner
    instruction_store: InstructionStore
    agent_model_id: str
    notification_queue: NotificationQueue
    watcher_model_id: str | None = None
    # Builds the deps object handed to the agent's tools. The default is the
    # eval's AgentDeps; the production plugin injects a factory that returns
    # ProactiveDeps carrying the live bot/channel for the parity tools.
    deps_factory: object | None = None
    # Prepended to the wake brief when non-empty. The plugin uses it for the
    # hourly memory refresh: since the agent's history persists across wakes,
    # the block only needs to ride one brief per refresh.
    brief_preamble: str = ""

    async def consume(self, context: ActivationContext) -> ActivationResult:
        usage_by_model: dict[str, dict] = {}
        env = ChannelEnvironment(
            visible=[*context.history, *context.new_messages],
            bot_user_id=context.bot_user_id,
        )

        actions = WakeActions()

        async def skim_transcript(transcript: str) -> str:
            text, skim_usage = await self.skim.skim(transcript)
            if self.watcher_model_id is None:
                raise ValueError(
                    "watcher_model_id is required to attribute skim usage"
                )
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
        pending, dropped = self.notification_queue.drain()
        brief = build_wake_brief(pending, dropped, self.instruction_store)
        if self.brief_preamble:
            brief = f"{self.brief_preamble}\n\n{brief}"
        note, agent_usage = await self.agent_runner.wake(brief, deps)
        _merge_usage(usage_by_model, self.agent_model_id, agent_usage)
        details = {
            "agent": {
                "tool_calls": deps.budget.used,
                "note": str(note)[:300],
            },
            "watch_instruction_updates": self.instruction_store.updates,
        }

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

    async def activate(self, context: ActivationContext) -> ActivationResult:
        return await self.consume(context)


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
    deps_factory: object | None = None
    brief_preamble: str = ""
    # Queue-only notifications (mode changes, expiries, non-wake watcher
    # summaries) accumulate here and drain into the next wake's brief. None
    # gives bare eval runs an isolated queue for each activation.
    notification_queue: NotificationQueue | None = None

    async def activate(self, context: ActivationContext) -> ActivationResult:
        notification_queue = self.notification_queue or NotificationQueue()
        producer = WatcherProducer(
            watcher=self.watcher,
            instruction_store=self.instruction_store,
            watcher_model_id=self.watcher_model_id,
            notification_queue=notification_queue,
            context_size=self.context_size,
            bot_display_name=self.bot_display_name,
        )
        usage_by_model = await producer.produce(context)
        if not producer.wake_produced:
            totals = {
                key: sum(entry[key] for entry in usage_by_model.values())
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                )
            }
            return ActivationResult(
                responses=[],
                input_tokens=totals["input_tokens"],
                output_tokens=totals["output_tokens"],
                cache_read_tokens=totals["cache_read_tokens"],
                model_id=self.agent_model_id,
                usage_by_model=usage_by_model,
                details=producer.details,
            )

        consumer = AgentConsumer(
            agent_runner=self.agent_runner,
            skim=self.skim,
            instruction_store=self.instruction_store,
            agent_model_id=self.agent_model_id,
            notification_queue=notification_queue,
            watcher_model_id=self.watcher_model_id,
            deps_factory=self.deps_factory,
            brief_preamble=self.brief_preamble,
        )
        agent_result = await consumer.consume(context)
        for model_id, usage in (agent_result.usage_by_model or {}).items():
            _merge_usage(usage_by_model, model_id, usage)
        totals = {
            key: sum(entry[key] for entry in usage_by_model.values())
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
            )
        }
        return ActivationResult(
            responses=agent_result.responses,
            input_tokens=totals["input_tokens"],
            output_tokens=totals["output_tokens"],
            cache_read_tokens=totals["cache_read_tokens"],
            model_id=self.agent_model_id,
            reactions=agent_result.reactions,
            usage_by_model=usage_by_model,
            details={**producer.details, **(agent_result.details or {})},
        )
