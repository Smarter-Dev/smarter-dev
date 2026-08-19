"""Pass 2: the Kimi K3 chat agent — tools, budget, persistent history.

The agent keeps its conversation history across wakes (compacted around
100k tokens); the watcher is stateless, so the TTL'd set_watch_instruction
tool is the agent's only way to make sure a relevant follow-up wakes it
again. set_monitoring_mode lets it flip its own channel between fast active
ingest and the 15-minute passive sweep.
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

# Stated in the system prompt so the agent can reason about its own cadence;
# keep in sync with windows.PASSIVE_SECONDS and the plugin's active window.
PASSIVE_SWEEP_MINUTES = 15
ACTIVE_WINDOW_MINUTES = 10
MONITORING_MODES = ("active", "passive")


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
    # Live runtimes inject a callable(mode, minutes) -> confirmation string;
    # None means mode control is unavailable (replay evals).
    request_mode: Callable[[str, int], str] | None = None


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
- Silence is the default; most wakes end with no message sent.
Backing off: if anyone tells you to stop, asks you to dial it back, or \
users seem annoyed or aggressive about your participation, stop responding \
immediately — at most a single brief acknowledgment, never an argument — \
and write a back-off note into the watch instructions (e.g. "cold entries \
paused for a few hours; wake only for direct mentions or replies to the \
bot"). Direct mentions still deserve answers; they are explicit invitations."""

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

HOW YOUR MONITORING WORKS:
- Everything reaches you as NOTIFICATIONS: @mentions and replies to you
(verbatim, with ids, user metadata and roles), waking watcher summaries
with relevant message ids, mode changes, instruction expiries, restart
recoveries. A notification is a lead — pull context with your tools when
it isn't enough.
- PASSIVE mode (default): the watcher reviews the channel every
{passive_minutes} minutes and wakes you ONLY for a tight set of
interesting activity — direct engagement with you and clear open bids it
judges worth your time. Anything else it sees, it drops without telling
you. If you want to be woken for something specific (a follow-up, a topic,
someone's next message), set_watch_instruction with a TTL — that is the
only way you will hear about it.
- ACTIVE mode: for ~{active_minutes} minutes after someone engages you,
the channel ingests fast (wakes within ~15-60s), extended by further
engagement. set_monitoring_mode switches modes yourself; @mentions and
replies always reach you in any mode.
- The watcher is STATELESS between calls; watch instructions are its only
memory. Your memory bundle refreshes at most hourly.

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
        passive_minutes=PASSIVE_SWEEP_MINUTES,
        active_minutes=ACTIVE_WINDOW_MINUTES,
        response_policy=response_policy or OPERATING_POLICY_BRIEF,
    )


def build_kimi_agent(
    model: Model | str,
    *,
    system_prompt: str,
    extra_tools: list = (),
    deps_type: type = AgentDeps,
) -> Agent:
    agent = Agent(
        model,
        deps_type=deps_type,
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
    async def set_watch_instruction(
        ctx: RunContext[AgentDeps], instruction: str, ttl_minutes: int = 60
    ) -> str:
        """Add a TTL'd wake criterion for the stateless watcher (e.g. "watch
        for tech news questions", 60 minutes). The only way a follow-up gets
        watched for."""
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        try:
            entry = ctx.deps.instruction_store.set_instruction(
                instruction, ttl_seconds=max(1, ttl_minutes) * 60
            )
        except ValueError as error:
            return str(error)
        return (
            f"Watch instruction {entry.instruction_id} set, expires "
            f"{entry.expires_at:%H:%M} UTC."
        )

    @agent.tool
    async def clear_watch_instruction(
        ctx: RunContext[AgentDeps], instruction_id: str
    ) -> str:
        """Remove one of your watch instructions by its id (e.g. "w1")."""
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        if ctx.deps.instruction_store.clear_instruction(instruction_id):
            return f"Watch instruction {instruction_id} cleared."
        return f"No watch instruction with id {instruction_id}."

    @agent.tool
    async def list_watch_instructions(ctx: RunContext[AgentDeps]) -> str:
        """List your active watch instructions with their ids and expiries."""
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        entries = ctx.deps.instruction_store.entries
        if not entries:
            return "No active watch instructions."
        return "\n".join(
            f"{e.instruction_id}: {e.text} (expires {e.expires_at:%H:%M} UTC)"
            for e in entries
        )

    @agent.tool
    async def set_monitoring_mode(
        ctx: RunContext[AgentDeps], mode: str, minutes: int = 10
    ) -> str:
        """Switch this channel between active (fast ingest) and passive
        (15-minute batch review) monitoring for the given duration."""
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        if mode not in MONITORING_MODES:
            return f"Unknown mode {mode!r}; use one of {MONITORING_MODES}."
        if ctx.deps.request_mode is None:
            return "Mode control is unavailable in this environment."
        return ctx.deps.request_mode(mode, minutes)

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
