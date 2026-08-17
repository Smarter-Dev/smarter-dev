"""The two-pass ProactiveBotAdapter: watcher gate, then the K3 agent."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval.simulation import (  # noqa: E402
    ActivationContext,
    ActivationResult,
)
from scripts.proactive_eval.twopass.agent import (  # noqa: E402
    AgentDeps,
    KimiAgentRunner,
    ToolBudget,
)
from scripts.proactive_eval.twopass.environment import (  # noqa: E402
    ChannelEnvironment,
    InstructionStore,
    WakeActions,
)
from scripts.proactive_eval.twopass.watcher import (  # noqa: E402
    SkimRunner,
    WatcherDecision,
    WatcherRunner,
)

WATCHER_CONTEXT_SIZE = 30


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
        decision, watcher_usage = await self.watcher.decide(
            instructions=self.instruction_store.current(),
            context_transcript=env.render(context.history[-self.context_size :]),
            new_transcript=env.render(context.new_messages),
        )
        _merge_usage(usage_by_model, self.watcher_model_id, watcher_usage)
        details: dict = {"watcher": decision.model_dump()}

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
