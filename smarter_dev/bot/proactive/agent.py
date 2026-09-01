"""Pass 2: the Kimi K3 chat agent — tools, budget, persistent history.

The agent keeps its conversation history across wakes (compacted around
100k tokens); the watcher is stateless, so the TTL'd set_watch_instruction
tool is the agent's only way to make sure a relevant follow-up wakes it
again. set_monitoring_mode lets it flip an enabled channel between fast
active ingest and the 15-minute passive sweep.
"""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from inspect import isawaitable

from pydantic_ai import Agent
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models import Model

from smarter_dev.bot.agents.response_fitting import SUMMARIZE_THRESHOLD
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.environment import WakeActions
from smarter_dev.bot.proactive.types import ProposedReaction
from smarter_dev.bot.proactive.types import ProposedResponse
from smarter_dev.bot.proactive.watcher import usage_dict

TOOL_CALL_LIMIT = 8
MAX_SENDS_PER_WAKE = 2
HISTORY_TOKEN_LIMIT = 100_000
# After compaction, keep roughly this many trailing messages verbatim.
COMPACTION_KEEP_MESSAGES = 8

BUDGET_EXHAUSTED = "Tool budget exhausted — wrap up with your final note now."
# Discord caps messages at 2000 chars; dispatch splits anything up to
# SUMMARIZE_THRESHOLD into two messages. Above that the send tools refuse so
# the agent rewrites with its own context still in hand — the in-loop
# equivalent of the chat bot's shorten re-run, at no extra model call.
TOO_LONG_TEMPLATE = (
    "That message is too long to send: {length} characters, and anything over "
    "{limit} can't be delivered. Rewrite it under 1500 characters — keep the "
    "essential answer and any load-bearing code or links — then send again."
)

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


@dataclass(kw_only=True)
class AgentDeps:
    actions: WakeActions
    skim_transcript: Callable[[str], Awaitable[str]]
    budget: ToolBudget
    enabled_channels: dict[str, str] = field(default_factory=dict)
    channel_envs: (
        Callable[[str], ChannelEnvironment | Awaitable[ChannelEnvironment]]
        | dict[str, ChannelEnvironment]
        | None
    ) = None
    instruction_stores: dict[str, InstructionStore] = field(
        default_factory=dict
    )
    # Transitional aliases keep the still-single-channel plugin and replay
    # harness constructible until the guild runtime lands.
    env: ChannelEnvironment | None = None
    instruction_store: InstructionStore | None = None
    # Live runtimes inject a callable(channel_id, mode, minutes) ->
    # confirmation string; None means mode control is unavailable (replay
    # evals).
    request_mode: Callable[[str, str, int], str] | None = None
    # Drains the channel's notification queue mid-run (rendered text, marks
    # the covered messages consumed); None means nothing queues mid-run.
    drain_notifications: Callable[[], str] | None = None
    # Environments already resolved from a callable provider this wake; in
    # production the provider hits Discord REST, so each channel is fetched
    # at most once no matter how many tools read it.
    resolved_channel_envs: dict[str, ChannelEnvironment] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if (
            self.env is not None or self.instruction_store is not None
        ) and not self.enabled_channels:
            raise ValueError(
                "enabled_channels is required with legacy env or "
                "instruction_store"
            )
        if (
            self.env is not None or self.instruction_store is not None
        ) and len(self.enabled_channels) != 1:
            raise ValueError(
                "legacy env or instruction_store requires exactly one "
                "enabled channel"
            )
        legacy_channel_id = next(iter(self.enabled_channels), "")
        if self.env is not None and self.channel_envs is None:
            self.channel_envs = {legacy_channel_id: self.env}
        if self.instruction_store is not None and not self.instruction_stores:
            self.instruction_stores = {
                legacy_channel_id: self.instruction_store
            }
        if self.env is None and isinstance(self.channel_envs, dict):
            self.env = next(iter(self.channel_envs.values()), None)
        if self.instruction_store is None:
            self.instruction_store = next(
                iter(self.instruction_stores.values()), None
            )

    async def channel_env(self, channel_id: str) -> ChannelEnvironment:
        return await fetch_channel_env(
            self.channel_envs, channel_id, self.resolved_channel_envs
        )

    def channel_instruction_store(self, channel_id: str) -> InstructionStore:
        return self.instruction_stores[channel_id]


async def fetch_channel_env(
    channel_envs: (
        Callable[[str], ChannelEnvironment | Awaitable[ChannelEnvironment]]
        | dict[str, ChannelEnvironment]
        | None
    ),
    channel_id: str,
    resolved_channel_envs: dict[str, ChannelEnvironment],
) -> ChannelEnvironment:
    if not callable(channel_envs):
        if channel_envs is None:
            raise KeyError(channel_id)
        return channel_envs[channel_id]
    if channel_id not in resolved_channel_envs:
        fetched = channel_envs(channel_id)
        if isawaitable(fetched):
            fetched = await fetched
        resolved_channel_envs[channel_id] = fetched
    return resolved_channel_envs[channel_id]


def disabled_channel_error(deps: AgentDeps, channel_id: str) -> str | None:
    if channel_id not in deps.enabled_channels:
        return f"Channel {channel_id} is not enabled for the proactive bot."
    return None


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
- While you are working, new channel activity never interrupts you — it
queues as notifications instead: mentions and replies to you individually
and verbatim, everything else as grouped watcher summaries. Call
read_notifications (free, costs no tool budget) to check; always check
once before your final send so you don't answer a stale channel.
- The watcher is STATELESS between calls; watch instructions are its only
memory. Set one whenever you would want to know something you won't
otherwise be told: someone promises to report back ("I'll post results
tonight"), you answer with a caveat worth checking on, a discussion is
unresolved and may need you, or you deliberately went quiet and want to
resume later. Your memory bundle refreshes at most hourly.

TOOLS:
- Anything on a release cycle — versions, release dates, prices, "latest
X", current events — web_search before stating it; your built-in knowledge
has a cutoff and is stale for these.

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


GUILD_AGENT_SYSTEM_PROMPT = """\
You are {bot_display_name}, a member of the {guild_name} Discord server. Your
Discord user id is {bot_user_id} — `<@{bot_user_id}>` inside a message means
someone is addressing YOU, and transcript lines marked [BOT] are your own past
messages. One guild-wide watcher system wakes you for activity across the
channels where the proactive feature is enabled. Your history persists across
channels and earlier wakes.

Every tool that reads from or acts in a channel requires a `channel_id`. Your
wake brief's WATCH INSTRUCTIONS BY CHANNEL section lists every enabled
channel with its channel_id — that is the complete set of channels you may
inspect or act in. Never guess another id or attempt to access a channel
outside that set. Notifications and watch instructions are labeled by channel;
keep each conversation and watch instruction routed to its named channel.

Choosing not to respond is a first-class outcome: when nothing clears the bar,
do nothing and say so in your final note. At most {max_sends} messages per
wake across the guild.

HOW YOUR MONITORING WORKS:
- Everything reaches you as channel-labeled NOTIFICATIONS. A notification is a
lead — pull context with channel tools when it is not enough.
- The watcher is STATELESS between calls. Per-channel watch instructions are
its only memory; set one in the relevant channel when you need a follow-up.
- Call read_notifications before your final send to catch activity queued
while you worked. It drains the guild-wide queue and costs no tool budget.

TOOLS:
- Anything on a release cycle — versions, release dates, prices, latest
events — web_search before stating it.

RESPONSE POLICY:
{response_policy}"""


def build_guild_agent_system_prompt(
    *, bot_display_name: str, bot_user_id: str, guild_name: str
) -> str:
    return GUILD_AGENT_SYSTEM_PROMPT.format(
        bot_display_name=bot_display_name,
        bot_user_id=bot_user_id,
        guild_name=guild_name,
        max_sends=MAX_SENDS_PER_WAKE,
        response_policy=OPERATING_POLICY_BRIEF,
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
    async def lookup_message(
        ctx: RunContext[AgentDeps], channel_id: str, message_id: str
    ) -> str:
        """Fetch one channel message verbatim by its message id."""
        if error := disabled_channel_error(ctx.deps, channel_id):
            return error
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        env = await ctx.deps.channel_env(channel_id)
        message = env.lookup(message_id)
        if message is None:
            return f"No message with id {message_id} is visible."
        return env.render([message])

    @agent.tool
    async def channel_history(
        ctx: RunContext[AgentDeps],
        channel_id: str,
        limit: int = 20,
        before_message_id: str | None = None,
    ) -> str:
        """Pull the last `limit` channel messages, optionally before a given
        message id."""
        if error := disabled_channel_error(ctx.deps, channel_id):
            return error
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        env = await ctx.deps.channel_env(channel_id)
        return env.render(env.history(limit, before_id=before_message_id))

    @agent.tool
    async def skim_messages(
        ctx: RunContext[AgentDeps],
        channel_id: str,
        around_message_id: str,
        radius: int = 40,
    ) -> str:
        """Have the fast watcher model skim the messages around an id,
        returning a summary with verbatim snippets and message/user ids."""
        if error := disabled_channel_error(ctx.deps, channel_id):
            return error
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        env = await ctx.deps.channel_env(channel_id)
        messages = env.slice_around(around_message_id, radius=radius)
        if not messages:
            return f"No message with id {around_message_id} is visible."
        return await ctx.deps.skim_transcript(env.render(messages))

    @agent.tool
    async def send_channel_message(
        ctx: RunContext[AgentDeps], channel_id: str, content: str
    ) -> str:
        """Send a standalone message to the channel."""
        if error := disabled_channel_error(ctx.deps, channel_id):
            return error
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        if len(ctx.deps.actions.sent) >= MAX_SENDS_PER_WAKE:
            return f"Send limit of {MAX_SENDS_PER_WAKE} per wake reached."
        if len(content) > SUMMARIZE_THRESHOLD:
            return TOO_LONG_TEMPLATE.format(
                length=len(content), limit=SUMMARIZE_THRESHOLD
            )
        ctx.deps.actions.sent.append(
            ProposedResponse(
                reply_to_id=None, content=content, channel_id=channel_id
            )
        )
        return "Message sent."

    @agent.tool
    async def reply_to_message(
        ctx: RunContext[AgentDeps],
        channel_id: str,
        message_id: str,
        content: str,
    ) -> str:
        """Reply to a specific channel message by id."""
        if error := disabled_channel_error(ctx.deps, channel_id):
            return error
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        if len(ctx.deps.actions.sent) >= MAX_SENDS_PER_WAKE:
            return f"Send limit of {MAX_SENDS_PER_WAKE} per wake reached."
        if (await ctx.deps.channel_env(channel_id)).lookup(message_id) is None:
            return f"No message with id {message_id} is visible."
        if len(content) > SUMMARIZE_THRESHOLD:
            return TOO_LONG_TEMPLATE.format(
                length=len(content), limit=SUMMARIZE_THRESHOLD
            )
        ctx.deps.actions.sent.append(
            ProposedResponse(
                reply_to_id=message_id,
                content=content,
                channel_id=channel_id,
            )
        )
        return "Reply sent."

    @agent.tool
    async def react_to_message(
        ctx: RunContext[AgentDeps],
        channel_id: str,
        message_id: str,
        emoji: str,
    ) -> str:
        """Add an emoji reaction to a channel message."""
        if error := disabled_channel_error(ctx.deps, channel_id):
            return error
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        if (await ctx.deps.channel_env(channel_id)).lookup(message_id) is None:
            return f"No message with id {message_id} is visible."
        ctx.deps.actions.reactions.append(
            ProposedReaction(
                message_id=message_id, emoji=emoji, channel_id=channel_id
            )
        )
        return "Reaction added."

    @agent.tool
    async def set_watch_instruction(
        ctx: RunContext[AgentDeps],
        channel_id: str,
        instruction: str,
        ttl_minutes: int = 60,
    ) -> str:
        """Add a TTL'd wake criterion for the stateless watcher (e.g. "watch
        for tech news questions", 60 minutes). The only way a follow-up gets
        watched for."""
        if error := disabled_channel_error(ctx.deps, channel_id):
            return error
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        try:
            entry = ctx.deps.channel_instruction_store(channel_id).set_instruction(
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
        ctx: RunContext[AgentDeps], channel_id: str, instruction_id: str
    ) -> str:
        """Remove one of your watch instructions by its id (e.g. "w1")."""
        if error := disabled_channel_error(ctx.deps, channel_id):
            return error
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        store = ctx.deps.channel_instruction_store(channel_id)
        if store.clear_instruction(instruction_id):
            return f"Watch instruction {instruction_id} cleared."
        return f"No watch instruction with id {instruction_id}."

    @agent.tool
    async def list_watch_instructions(
        ctx: RunContext[AgentDeps], channel_id: str
    ) -> str:
        """List your active watch instructions with their ids and expiries."""
        if error := disabled_channel_error(ctx.deps, channel_id):
            return error
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        entries = ctx.deps.channel_instruction_store(channel_id).entries
        if not entries:
            return "No active watch instructions."
        return "\n".join(
            f"{e.instruction_id}: {e.text} (expires {e.expires_at:%H:%M} UTC)"
            for e in entries
        )

    @agent.tool
    async def read_notifications(ctx: RunContext[AgentDeps]) -> str:
        """Read notifications that queued while you've been working: new
        mentions verbatim, other new messages as grouped summaries. Free —
        never spends your tool budget. Check before you finish a wake."""
        if ctx.deps.drain_notifications is None:
            return "No new notifications."
        return ctx.deps.drain_notifications()

    @agent.tool
    async def set_monitoring_mode(
        ctx: RunContext[AgentDeps],
        channel_id: str,
        mode: str,
        minutes: int = 10,
    ) -> str:
        """Switch one enabled channel between active (fast ingest) and
        passive (15-minute batch review) monitoring for the given duration."""
        if error := disabled_channel_error(ctx.deps, channel_id):
            return error
        if not ctx.deps.budget.try_spend():
            return BUDGET_EXHAUSTED
        if mode not in MONITORING_MODES:
            return f"Unknown mode {mode!r}; use one of {MONITORING_MODES}."
        if ctx.deps.request_mode is None:
            return "Mode control is unavailable in this environment."
        return ctx.deps.request_mode(channel_id, mode, minutes)

    # Parity tools (web search, code run, …) register here; in replay evals
    # the Discord/API-bound ones are stubbed by the caller.
    for tool_function in extra_tools:
        agent.tool(tool_function)

    return agent


def estimated_history_tokens(history: list[ModelMessage]) -> int:
    if not history:
        return 0
    return len(ModelMessagesTypeAdapter.dump_json(history)) // 4


COMPACTION_PROMPT = """\
Your rolling context is about to be compacted: everything above this message
will be replaced by the note you write now. Write the memory your future self
needs to continue seamlessly — conversations still in motion and who is in
them, commitments or follow-ups you made, what you have learned about the
people and channels you watch, and anything else you judge important to
remember.

Attribute everything: every statement, request, or event in your note must
name WHO said or did it (username, and user id when you have it) and WHERE
(channel name and id). Write in the third person about others ("zech asked
in #general (644…) for …", "you promised kyra you would …") — never quote
anyone in a way that could later be misread as a different user speaking, and
never leave a fact floating without its person and channel. Be specific:
names, channel ids and message ids you may need again. This note is for you
alone."""


async def self_compaction_summary(
    model: Model | str, messages: list[ModelMessage]
) -> tuple[str, dict]:
    """The agent's own carry-forward memory, written by its own model.

    The transcript being folded rides as message history, so the model
    decides what its future self most needs to keep.
    """
    compaction_agent = Agent(model, output_type=str)
    result = await compaction_agent.run(
        COMPACTION_PROMPT, message_history=messages
    )
    return result.output, usage_dict(result.usage())


async def compact_agent_history(
    history: list[ModelMessage],
    *,
    token_limit: int,
    summarize: Callable[[list[ModelMessage]], Awaitable[str]],
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
    summary = await summarize(old)
    return [
        ModelRequest(
            parts=[
                UserPromptPart(
                    "[COMPACTION MEMORY NOTE — you wrote this yourself when "
                    "your earlier transcript was folded. It is NOT a user "
                    "message: attribute its contents only to the users and "
                    "channels it names, never to whoever engages you next]\n"
                    f"{summary}"
                )
            ]
        ),
        ModelResponse(
            parts=[
                TextPart(
                    "Understood — that is my own memory note, not user "
                    "input. Continuing from there."
                )
            ]
        ),
        *tail,
    ]


@dataclass
class KimiAgentRunner:
    """Runs the agent, carrying history across wakes with compaction."""

    agent: Agent
    summarize: Callable[[list[ModelMessage]], Awaitable[str]]
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
