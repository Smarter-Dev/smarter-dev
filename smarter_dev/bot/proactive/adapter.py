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


def build_wake_brief(decision: WatcherDecision, env: ChannelEnvironment) -> str:
    snippet_messages = [
        message
        for message_id in decision.relevant_message_ids
        if (message := env.lookup(message_id)) is not None
    ]
    snippets = env.render(snippet_messages) if snippet_messages else "(none)"
    return f"""\
The watcher woke you.

WATCHER SUMMARY: {decision.summary or decision.reason}

RELEVANT MESSAGES (verbatim, with message ids):
{snippets}

Use your tools to investigate further if needed, act per your policy (or
deliberately don't), and finish with a one-sentence note on what you did
and why."""


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

    async def activate(self, context: ActivationContext) -> ActivationResult:
        usage_by_model: dict[str, dict] = {}
        env = ChannelEnvironment(
            visible=[*context.history, *context.new_messages],
            bot_user_id=context.bot_user_id,
        )
        forced_ids = bot_directed_message_ids(
            context.new_messages, env, context.bot_user_id
        )
        if forced_ids:
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
            )
            _merge_usage(usage_by_model, self.watcher_model_id, watcher_usage)
            details = {"watcher": decision.model_dump()}

        actions = WakeActions()
        if decision.wake:

            async def skim_transcript(transcript: str) -> str:
                text, skim_usage = await self.skim.skim(transcript)
                _merge_usage(usage_by_model, self.watcher_model_id, skim_usage)
                return text

            deps = AgentDeps(
                env=env,
                actions=actions,
                instruction_store=self.instruction_store,
                skim_transcript=skim_transcript,
                budget=ToolBudget(),
            )
            note, agent_usage = await self.agent_runner.wake(
                build_wake_brief(decision, env), deps
            )
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
