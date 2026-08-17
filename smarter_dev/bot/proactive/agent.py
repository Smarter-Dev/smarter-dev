"""Pass 2: the Kimi K3 chat agent — tools, budget, persistent history.

The agent keeps its conversation history across wakes (compacted around
100k tokens); the watcher is stateless, so the `update_watch_instructions`
tool is the agent's only way to make sure a relevant follow-up wakes it
again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import Model

from smarter_dev.bot.proactive.environment import (
    ChannelEnvironment,
    InstructionStore,
    WakeActions,
)
from smarter_dev.bot.proactive.types import (
    ProposedReaction,
    ProposedResponse,
)
from smarter_dev.bot.proactive.watcher import usage_dict

TOOL_CALL_LIMIT = 8
MAX_SENDS_PER_WAKE = 2
HISTORY_TOKEN_LIMIT = 100_000
# After compaction, keep roughly this many trailing messages verbatim.
COMPACTION_KEEP_MESSAGES = 8

BUDGET_EXHAUSTED = "Tool budget exhausted — wrap up with your final note now."


@dataclass
class ToolBudget:
    limit: int = TOOL_CALL_LIMIT
    used: int = 0

    def try_spend(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


@dataclass
class AgentDeps:
    env: ChannelEnvironment
    actions: WakeActions
    instruction_store: InstructionStore
    skim_transcript: Callable[[str], Awaitable[str]]
    budget: ToolBudget


# Condensed operating rules — the full rationale lives in
# scripts/proactive_eval/response-policy.md; keep the two in sync.
OPERATING_POLICY_BRIEF = """\
Mode 1 — participating: you are in a conversation only when someone \
@mentions you, replies to your message, or follows up on what you said. \
While they keep engaging you, respond freely and conversationally.
Mode 2 — cold entry: everything else. Then:
- Respond only to open bids to the room or things addressed to you; never \
to a message directed at another person (their replies, @mentions of \
others, or a back-and-forth between two people).
- Contribute real help; never fact-dumps or "well, actually" corrections.
- Frame it as a one-off: no questions that pull people into conversation \
with you, no phrasing that presumes you are part of the thread.
- The bar is higher for you than for a human; "wouldn't be out of place" \
is not enough.
- Never send content-free messages (greetings-back, bare acknowledgments).
- Silence is the default; most wakes end with no message sent."""

AGENT_SYSTEM_PROMPT = """\
You are {bot_display_name}, a member of the #{channel_name} channel in the
{guild_name} Discord server. Your Discord user id is {bot_user_id} —
`<@{bot_user_id}>` inside a message means someone is addressing YOU, and
transcript lines marked [BOT] are your own past messages. A watcher process
woke you because something in the channel may warrant your attention. Your
history spans earlier wakes — you remember what you did before.

Choosing not to respond is a first-class outcome: when nothing clears the
bar, do nothing and say so in your final note. At most {max_sends} messages
per wake.

The watcher that decides when to wake you is stateless — it forgets
everything between calls. If a follow-up matters ("wake me if X posts their
benchmark results"), you MUST write it into the watch instructions with the
update_watch_instructions tool, or the follow-up will be missed.

RESPONSE POLICY:
{response_policy}"""


def build_agent_system_prompt(
    *,
    bot_display_name: str,
    bot_user_id: str,
    channel_name: str,
    guild_name: str,
    response_policy: str | None = None,
) -> str:
    return AGENT_SYSTEM_PROMPT.format(
        bot_display_name=bot_display_name,
        bot_user_id=bot_user_id,
        channel_name=channel_name,
        guild_name=guild_name,
        max_sends=MAX_SENDS_PER_WAKE,
        response_policy=response_policy or OPERATING_POLICY_BRIEF,
    )


def build_kimi_agent(
    model: Model | str, *, system_prompt: str, extra_tools: list = ()
) -> Agent:
    agent = Agent(
        model,
        deps_type=AgentDeps,
        output_type=str,
        system_prompt=system_prompt,
    )

    @agent.tool
    async def lookup_message(ctx: RunContext[AgentDeps], message_id: str) -> str:
        """Fetch one channel message verbatim by its message id."""
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        message = ctx.deps.env.lookup(message_id)
        if message is None:
            return f"No message with id {message_id} is visible."
        return ctx.deps.env.render([message])

    @agent.tool
    async def channel_history(
        ctx: RunContext[AgentDeps],
        limit: int = 20,
        before_message_id: str | None = None,
    ) -> str:
        """Pull the last `limit` channel messages, optionally before a given
        message id."""
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        return ctx.deps.env.render(
            ctx.deps.env.history(limit, before_id=before_message_id)
        )

    @agent.tool
    async def skim_messages(
        ctx: RunContext[AgentDeps], around_message_id: str, radius: int = 40
    ) -> str:
        """Have the fast watcher model skim the messages around an id,
        returning a summary with verbatim snippets and message/user ids."""
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        messages = ctx.deps.env.slice_around(around_message_id, radius=radius)
        if not messages:
            return f"No message with id {around_message_id} is visible."
        return await ctx.deps.skim_transcript(ctx.deps.env.render(messages))

    @agent.tool
    async def send_channel_message(
        ctx: RunContext[AgentDeps], content: str
    ) -> str:
        """Send a standalone message to the channel."""
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        if len(ctx.deps.actions.sent) >= MAX_SENDS_PER_WAKE:
            return f"Send limit of {MAX_SENDS_PER_WAKE} per wake reached."
        ctx.deps.actions.sent.append(
            ProposedResponse(reply_to_id=None, content=content)
        )
        return "Message sent."

    @agent.tool
    async def reply_to_message(
        ctx: RunContext[AgentDeps], message_id: str, content: str
    ) -> str:
        """Reply to a specific channel message by id."""
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        if len(ctx.deps.actions.sent) >= MAX_SENDS_PER_WAKE:
            return f"Send limit of {MAX_SENDS_PER_WAKE} per wake reached."
        if ctx.deps.env.lookup(message_id) is None:
            return f"No message with id {message_id} is visible."
        ctx.deps.actions.sent.append(
            ProposedResponse(reply_to_id=message_id, content=content)
        )
        return "Reply sent."

    @agent.tool
    async def react_to_message(
        ctx: RunContext[AgentDeps], message_id: str, emoji: str
    ) -> str:
        """Add an emoji reaction to a channel message."""
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        if ctx.deps.env.lookup(message_id) is None:
            return f"No message with id {message_id} is visible."
        ctx.deps.actions.reactions.append(
            ProposedReaction(message_id=message_id, emoji=emoji)
        )
        return "Reaction added."

    @agent.tool
    async def update_watch_instructions(
        ctx: RunContext[AgentDeps], addendum: str
    ) -> str:
        """Replace the addendum of the watcher's wake criteria. The watcher
        is stateless: this is the only way a follow-up gets watched for."""
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        ctx.deps.instruction_store.update(addendum)
        return "Watch instructions updated."

    # Parity tools (web search, code run, …) register here; in replay evals
    # the Discord/API-bound ones are stubbed by the caller.
    for tool_function in extra_tools:
        agent.tool(tool_function)

    return agent


def estimated_history_tokens(history: list[ModelMessage]) -> int:
    if not history:
        return 0
    return len(ModelMessagesTypeAdapter.dump_json(history)) // 4


async def compact_agent_history(
    history: list[ModelMessage],
    *,
    token_limit: int,
    summarize: Callable[[str], Awaitable[str]],
    keep_messages: int = COMPACTION_KEEP_MESSAGES,
) -> list[ModelMessage]:
    """Fold old wakes into a summary once the history outgrows the limit.

    The kept tail always starts at a ModelRequest so the sequence stays
    valid for the API.
    """
    if estimated_history_tokens(history) <= token_limit:
        return history
    cut = max(0, len(history) - keep_messages)
    while cut < len(history) and not isinstance(history[cut], ModelRequest):
        cut += 1
    old, tail = history[:cut], history[cut:]
    if not old:
        return history
    summary = await summarize(
        ModelMessagesTypeAdapter.dump_json(old).decode()
    )
    return [
        ModelRequest(
            parts=[
                UserPromptPart(
                    "[Summary of your activity on earlier wakes — the full "
                    f"transcript was compacted]\n{summary}"
                )
            ]
        ),
        ModelResponse(parts=[TextPart("Noted, continuing from there.")]),
        *tail,
    ]


@dataclass
class KimiAgentRunner:
    """Runs the agent, carrying history across wakes with compaction."""

    agent: Agent
    summarize: Callable[[str], Awaitable[str]]
    token_limit: int = HISTORY_TOKEN_LIMIT
    history: list[ModelMessage] = field(default_factory=list)

    async def wake(self, brief: str, deps: AgentDeps) -> tuple[str, dict]:
        self.history = await compact_agent_history(
            self.history, token_limit=self.token_limit, summarize=self.summarize
        )
        result = await self.agent.run(
            brief, deps=deps, message_history=self.history or None
        )
        self.history = result.all_messages()
        return result.output, usage_dict(result.usage())
