"""Tests for the Monty handler runtime and its rails (offline)."""

from __future__ import annotations

from dataclasses import dataclass, field

from smarter_dev.web.handler_budget import CapExceeded, HandlerBudget
from smarter_dev.web.handler_runtime import run_handler_script


@dataclass
class _FakeEmitter:
    messages: list[tuple[str, str]] = field(default_factory=list)
    # Full create_message calls including the mention-rail arg (channel, content,
    # ping_role_id) so the runtime's admin-only ping pass-through is observable.
    message_calls: list[tuple[str, str, str | None]] = field(default_factory=list)
    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    # (channel, message_id, content) for each edit_message; renames record
    # (channel, name). status/behaviour is happy-path unless a test overrides.
    edits: list[tuple[str, str, str]] = field(default_factory=list)
    renames: list[tuple[str, str]] = field(default_factory=list)
    # channel_id -> list[dict] returned by list_threads (missing key -> [], the
    # emitter's gone-channel (404) shape).
    threads_by_channel: dict = field(default_factory=dict)
    created_threads: list = field(default_factory=list)
    created_posts: list = field(default_factory=list)
    # thread_id -> parent channel id (None models a non-thread / gone channel).
    parent_by_thread: dict = field(default_factory=dict)
    parent_calls: list = field(default_factory=list)
    # channel_id -> owning guild id (None models a gone channel). Backs the admin
    # list_threads guild-scope rail.
    guild_by_channel: dict = field(default_factory=dict)
    guild_calls: list = field(default_factory=list)
    # Value get_guild_member_count returns (overridable per test).
    member_count: int = 1234

    async def create_message(
        self, channel_id: str, content: str, ping_role_id: str | None = None
    ) -> str:
        self.messages.append((channel_id, content))
        self.message_calls.append((channel_id, content, ping_role_id))
        return f"msg{len(self.messages)}"

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append((channel_id, message_id, emoji))

    async def edit_message(self, channel_id: str, message_id: str, content: str) -> str:
        self.edits.append((channel_id, message_id, content))
        return message_id

    async def rename_channel(self, channel_id: str, name: str) -> bool:
        self.renames.append((channel_id, name))
        return True

    async def list_threads(self, channel_id: str, limit: int = 50) -> list:
        return self.threads_by_channel.get(channel_id, [])

    async def get_guild_member_count(self) -> int:
        return self.member_count

    async def get_channel_guild_id(self, channel_id: str):
        self.guild_calls.append(channel_id)
        return self.guild_by_channel.get(channel_id)

    async def create_thread(self, channel_id, name, message_id=None) -> str:
        self.created_threads.append((channel_id, name, message_id))
        return f"thread{len(self.created_threads)}"

    async def create_post(self, channel_id, title, content, tag_names=None) -> str:
        self.created_posts.append((channel_id, title, content, tag_names))
        return f"post{len(self.created_posts)}"

    async def get_thread_parent_id(self, thread_id: str):
        self.parent_calls.append(thread_id)
        return self.parent_by_thread.get(thread_id)

    # (user_id, content) for each send_dm; ``dm_result`` is what the emitter
    # returns (a message id str on success, or False on a closed door).
    dm_sends: list = field(default_factory=list)
    dm_result: object = "dm-msg-id"

    async def send_dm(self, user_id: str, content: str):
        self.dm_sends.append((user_id, content))
        return self.dm_result


@dataclass
class _StubLimiter:
    allow: bool = True
    calls: list = field(default_factory=list)

    async def hit(self, key: str, limit: int, window_seconds: int | None = None) -> bool:
        self.calls.append((key, limit, window_seconds))
        return self.allow


@dataclass
class _FakeActor:
    calls: list = field(default_factory=list)
    # Thread ids that model a gone target (404): the mutation returns False.
    gone: set = field(default_factory=set)

    async def ban_user(self, user_id, reason=None, delete_message_seconds=0):
        self.calls.append(("ban", user_id, reason, delete_message_seconds))
        return f"banned {user_id}"

    async def kick_user(self, user_id):
        self.calls.append(("kick", user_id)); return f"kicked {user_id}"

    async def timeout_user(self, user_id, duration_seconds=600):
        self.calls.append(("timeout", user_id, duration_seconds)); return "ok"

    async def delete_message(self, channel_id, message_id):
        self.calls.append(("delete", channel_id, message_id)); return "ok"

    async def close_thread(self, thread_id):
        self.calls.append(("close", thread_id)); return thread_id not in self.gone

    async def lock_thread(self, thread_id):
        self.calls.append(("lock", thread_id)); return thread_id not in self.gone

    async def reopen_thread(self, thread_id):
        self.calls.append(("reopen", thread_id)); return thread_id not in self.gone

    async def delete_thread(self, thread_id):
        self.calls.append(("delete_thread", thread_id)); return thread_id not in self.gone

    async def add_role(self, user_id, role_id, reason=None):
        self.calls.append(("add_role", user_id, role_id, reason))
        return user_id not in self.gone

    async def remove_role(self, user_id, role_id, reason=None):
        self.calls.append(("remove_role", user_id, role_id, reason))
        return user_id not in self.gone

    # webhook_result: what delete_webhook returns (True killed / False 404).
    webhook_result: bool = True

    async def delete_webhook(self, webhook_url):
        self.calls.append(("delete_webhook", webhook_url))
        return self.webhook_result

    # member_info / search_result: canned mod-audit REST reads.
    member_info: dict = field(default_factory=lambda: {"user_id": "U1", "in_guild": True})
    search_result: dict = field(
        default_factory=lambda: {"members": [], "overflow_count": 0}
    )

    async def get_member_info(self, user_id):
        self.calls.append(("get_member_info", user_id))
        return dict(self.member_info)

    async def search_guild_members(self, query, limit=10):
        self.calls.append(("search_guild_members", query, limit))
        return dict(self.search_result)


async def _run(
    script, *, budget=None, emitter=None, limiter=None, agent_runner=None,
    actor=None, channel_ids=None, allowed_role_ids=None,
    timer_scheduler=None, timer_limiter=None, dm_user_limiter=None, handler_id=None,
    mod_action_reader=None,
):
    emitter = emitter or _FakeEmitter()
    limiter = limiter or _StubLimiter()
    kwargs = {}
    if dm_user_limiter is not None:
        kwargs["dm_user_limiter"] = dm_user_limiter
    if mod_action_reader is not None:
        kwargs["mod_action_reader"] = mod_action_reader
    if agent_runner is not None:
        kwargs["agent_runner"] = agent_runner
    if actor is not None:
        kwargs["actor"] = actor
    if channel_ids is not None:
        kwargs["channel_ids"] = channel_ids
    if allowed_role_ids is not None:
        kwargs["allowed_role_ids"] = allowed_role_ids
    if timer_scheduler is not None:
        kwargs["timer_scheduler"] = timer_scheduler
    if timer_limiter is not None:
        kwargs["timer_limiter"] = timer_limiter
    if handler_id is not None:
        kwargs["handler_id"] = handler_id
    result = await run_handler_script(
        script,
        {
            "trigger_type": "message",
            "message_content": "hi",
            "message_id": "M1",
            "author_id": "U1",
        },
        channel_id="C1",
        guild_id="G1",
        emitter=emitter,
        limiter=limiter,
        budget=budget,
        **kwargs,
    )
    return result, emitter, limiter


async def test_happy_path_single_emitter():
    script = (
        'name = context["message_content"]\n'
        'await send_message(f"hello {name}")\n'
        'await send_message("again")\n'
    )
    result, emitter, _ = await _run(script)
    assert result.outcome == "ok"
    assert [c for _, c in emitter.messages] == ["hello hi", "again"]
    assert result.usage["messages_sent"] == 2


async def test_send_then_react_uses_returned_message_id():
    script = (
        'mid = await send_message("hi")\n'
        'await add_reaction(mid, "🎉")\n'
    )
    result, emitter, _ = await _run(script)
    assert result.outcome == "ok"
    assert emitter.reactions == [("C1", "msg1", "🎉")]


async def test_message_cap_breaches_mid_flight():
    script = "for i in range(5):\n    await send_message(str(i))\n"
    result, emitter, _ = await _run(script, budget=HandlerBudget(max_messages=3))
    assert result.outcome == "cap_exceeded"
    assert result.cap == "messages"
    # Two-then-fail: the first three went out before the breach, no rollback.
    assert len(emitter.messages) == 3
    assert result.usage["messages_sent"] == 3


async def test_agent_context_byte_cap_blocks_before_running_agent():
    ran = []

    async def runner(prompt, has_tools, budget):
        ran.append(prompt)
        return "ok"

    script = 'await spawn_agent("x" * 40000, has_tools=True)\n'
    result, _, _ = await _run(
        script, budget=HandlerBudget(max_agent_context_bytes=32 * 1024), agent_runner=runner
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "agent_context_bytes"
    assert ran == []  # agent never invoked


async def test_agent_call_cap():
    calls = []

    async def runner(prompt, has_tools, budget):
        calls.append(prompt)
        return "r"

    script = (
        'await spawn_agent("a")\n'
        'await spawn_agent("b")\n'
        'await spawn_agent("c")\n'
    )
    result, _, _ = await _run(
        script, budget=HandlerBudget(max_agent_calls=2), agent_runner=runner
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "agent_calls"
    assert calls == ["a", "b"]


async def test_search_read_budget_shared_across_agents():
    # Each agent reads two pages; with max_web_reads=3 the second agent's second
    # read breaches the shared pool — proving metering is shared, not per-agent.
    async def runner(prompt, has_tools, budget):
        budget.spend_web_read()
        budget.spend_web_read()
        return "done"

    script = 'await spawn_agent("a", has_tools=True)\nawait spawn_agent("b", has_tools=True)\n'
    result, _, _ = await _run(
        script,
        budget=HandlerBudget(max_agent_calls=2, max_web_reads=3),
        agent_runner=runner,
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "web_reads"


async def test_channel_window_denial_fails_loud():
    script = 'await send_message("hi")\n'
    result, emitter, _ = await _run(script, limiter=_StubLimiter(allow=False))
    assert result.outcome == "cap_exceeded"
    assert result.cap == "channel_messages_per_min"
    assert emitter.messages == []  # window denied before the REST emit


async def test_wall_clock_zero_budget_fails_immediately():
    script = 'await send_message("hi")\n'
    result, emitter, _ = await _run(script, budget=HandlerBudget(wall_clock_seconds=0.0))
    assert result.outcome == "cap_exceeded"
    assert result.cap == "wall_clock"
    assert emitter.messages == []


async def test_compile_error_is_captured_not_raised():
    result, _, _ = await _run("this is not valid python ::::\n")
    assert result.outcome == "error"
    assert "compile" in (result.error or "")


# -- admin handlers (actor set) ------------------------------------------------

from smarter_dev.web.handler_budget import admin_budget


async def test_standard_handler_has_no_admin_functions():
    # No actor -> ban_user is not defined in the sandbox -> NameError -> error.
    result, _, _ = await _run('await ban_user("U1")\n')
    assert result.outcome == "error"


async def test_standard_handler_cannot_send_to_other_channel():
    result, emitter, _ = await _run('await send_message("hi", "OTHER")\n')
    assert result.outcome == "cap_exceeded"
    assert result.cap == "cross_channel_send"
    assert emitter.messages == []


async def test_admin_handler_moderation_metered():
    actor = _FakeActor()
    script = (
        'await delete_message(context["message_id"], "C1")\n'
        'await ban_user(context["author_id"], "scam")\n'
        'await send_message("banned a scammer", "MODCHAT")\n'
    )
    result, emitter, _ = await _run(
        script, budget=admin_budget(), actor=actor,
        # context needs message_id/author_id
    )
    assert result.outcome == "ok", result.error
    assert ("delete", "C1", "hi") not in actor.calls  # message_id is from context
    kinds = [c[0] for c in actor.calls]
    assert "delete" in kinds and "ban" in kinds
    assert result.usage["mod_actions"] == 2
    assert ("MODCHAT", "banned a scammer") in emitter.messages  # admin cross-channel ok


async def test_admin_send_message_forwards_ping_role_id():
    # An admin (actor set) escalation send may ping exactly one role.
    actor = _FakeActor()
    script = 'await send_message("mods needed", ping_role_id="R7")\n'
    result, emitter, _ = await _run(script, budget=admin_budget(), actor=actor)
    assert result.outcome == "ok", result.error
    assert emitter.message_calls == [("C1", "mods needed", "R7")]


async def test_standard_send_message_drops_ping_role_id():
    # A standard handler (no actor) never pings a role even if the script asks;
    # the emitter's suppressing default stands (fail-safe, arg silently dropped).
    script = 'await send_message("mods needed", ping_role_id="R7")\n'
    result, emitter, _ = await _run(script)
    assert result.outcome == "ok", result.error
    assert emitter.message_calls == [("C1", "mods needed", None)]


async def test_admin_mod_action_cap():
    actor = _FakeActor()
    script = "for i in range(5):\n    await kick_user(str(i))\n"
    result, _, _ = await _run(
        script, budget=HandlerBudget(max_mod_actions=3, max_messages=5), actor=actor
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "mod_actions"
    assert len(actor.calls) == 3


async def test_random_globals_available_without_import():
    # Deterministic picks so the assertion is stable: randint(5,5)=5,
    # choice/shuffled/sample over singletons/known sizes.
    script = (
        "r = randint(5, 5)\n"
        'c = choice(["only"])\n'
        "s = shuffled([1])\n"
        "p = sample([1, 2, 3], 2)\n"
        'await send_message(f"{r}{c}{len(s)}{len(p)}")\n'
    )
    result, emitter, _ = await _run(script)
    assert result.outcome == "ok", result.error
    assert emitter.messages[0][1] == "5only12"


async def test_import_random_still_errors():
    # random is injected as globals; `import random` must still fail loud so the
    # judge's "reject disallowed imports" guidance matches runtime reality.
    result, emitter, _ = await _run("import random\nawait send_message('x')\n")
    assert result.outcome == "error"
    assert "random" in (result.error or "")
    assert emitter.messages == []


async def test_datetime_now_returns_host_clock():
    # datetime.now(tz) is an OS call Monty refuses unless the host wires a clock;
    # _clock_os grants it. A script must be able to compute a recency window
    # against an ISO timestamp from context — the canonical "new account" guard.
    script = (
        "import datetime\n"
        "now = datetime.datetime.now(datetime.timezone.utc)\n"
        'joined = datetime.datetime.fromisoformat("2000-01-01T00:00:00+00:00")\n'
        "age = (now - joined).total_seconds()\n"
        'await send_message("old" if age > 86400 else "new")\n'
    )
    result, emitter, _ = await _run(script)
    assert result.outcome == "ok", result.error
    assert emitter.messages[0][1] == "old"  # joined in year 2000 -> well past 24h


async def test_date_today_available():
    script = (
        "import datetime\n"
        "y = datetime.date.today().year\n"
        'await send_message("yes" if y >= 2026 else "no")\n'
    )
    result, emitter, _ = await _run(script)
    assert result.outcome == "ok", result.error
    assert emitter.messages[0][1] == "yes"


async def test_filesystem_still_blocked_with_clock_wired():
    # Wiring the clock OS callback must not crack open any other OS surface:
    # filesystem reads still fail loud.
    script = (
        "from pathlib import Path\n"
        'await send_message(Path("/etc/passwd").read_text())\n'
    )
    result, emitter, _ = await _run(script)
    assert result.outcome == "error"
    assert emitter.messages == []


# -- thread reads / creates (general, both tiers) ------------------------------

from smarter_dev.web.handler_caps import (
    CHANNEL_MESSAGES_PER_MIN,
    GUILD_THREAD_OPS_PER_MIN,
    channel_message_key,
    guild_thread_ops_key,
)


async def test_list_threads_home_channel_spends_discord_read():
    emitter = _FakeEmitter(threads_by_channel={"C1": [{"thread_id": "T1"}]})
    script = (
        "threads = await list_threads()\n"
        'await send_message(f"{len(threads)}:{threads[0][\'thread_id\']}")\n'
    )
    result, emitter, _ = await _run(script, emitter=emitter)
    assert result.outcome == "ok", result.error
    assert emitter.messages[0][1] == "1:T1"
    assert result.usage["discord_reads"] == 1


async def test_list_threads_gone_channel_returns_empty_without_error():
    # A gone channel is [] from the emitter; the script sees [] and does not error.
    script = (
        "threads = await list_threads()\n"
        'await send_message("empty" if threads == [] else "nonempty")\n'
    )
    result, emitter, _ = await _run(script)  # no threads registered -> []
    assert result.outcome == "ok", result.error
    assert emitter.messages[0][1] == "empty"


async def test_list_threads_discord_read_cap_breaches():
    script = "for i in range(3):\n    await list_threads()\n"
    result, _, _ = await _run(script, budget=HandlerBudget(max_discord_reads=2))
    assert result.outcome == "cap_exceeded"
    assert result.cap == "discord_reads"


async def test_get_guild_member_count_available_to_standard_handler():
    # A read-only, cheap guild-count read is present for BOTH tiers (no actor).
    emitter = _FakeEmitter(member_count=1234)
    script = (
        "n = await get_guild_member_count()\n"
        'await send_message(f"{n}")\n'
    )
    result, emitter, _ = await _run(script, emitter=emitter)
    assert result.outcome == "ok", result.error
    assert emitter.messages[0][1] == "1234"


async def test_get_guild_member_count_spends_discord_read():
    emitter = _FakeEmitter(member_count=42)
    result, _, _ = await _run(
        "await get_guild_member_count()\n", emitter=emitter
    )
    assert result.outcome == "ok", result.error
    assert result.usage["discord_reads"] == 1


async def test_get_guild_member_count_breach_raises_discord_reads():
    # Shares the discord_reads pool with list_threads; exhausting it fails the fire.
    script = "for i in range(3):\n    await get_guild_member_count()\n"
    result, _, _ = await _run(script, budget=HandlerBudget(max_discord_reads=2))
    assert result.outcome == "cap_exceeded"
    assert result.cap == "discord_reads"


async def test_standard_list_threads_foreign_channel_denied():
    result, emitter, _ = await _run('await list_threads("OTHER")\n')
    assert result.outcome == "cap_exceeded"
    assert result.cap == "cross_channel_send"
    assert result.usage["discord_reads"] == 0  # denied before spending


async def test_create_thread_spends_message_and_channel_window():
    script = 'tid = await create_thread("help")\nawait send_message(tid)\n'
    result, emitter, limiter = await _run(script)
    assert result.outcome == "ok", result.error
    assert emitter.created_threads == [("C1", "help", None)]
    assert emitter.messages[0][1] == "thread1"  # returned id flows to the script
    assert result.usage["messages_sent"] == 2  # create + the send_message
    assert (channel_message_key("C1"), CHANNEL_MESSAGES_PER_MIN, None) in limiter.calls
    # §5.3: a create is a mutating thread op and draws the guild thread-op window
    # too — but it spends the message budget, not the thread_ops counter.
    assert (guild_thread_ops_key("G1"), GUILD_THREAD_OPS_PER_MIN, None) in limiter.calls
    assert result.usage["thread_ops"] == 0


async def test_create_thread_guild_window_denied_fails_loud():
    # Channel window passes, guild thread-op window denies -> the create is
    # stopped before the REST call with the guild cap named.
    class _GuildWindowLimiter(_StubLimiter):
        async def hit(self, key, limit):
            self.calls.append((key, limit))
            return key != guild_thread_ops_key("G1")

    result, emitter, _ = await _run(
        'await create_thread("t")\n', limiter=_GuildWindowLimiter()
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "guild_thread_ops_per_min"
    assert emitter.created_threads == []


async def test_create_thread_with_message_id_hangs_off_message():
    result, emitter, _ = await _run('await create_thread("t", "M9")\n')
    assert result.outcome == "ok", result.error
    assert emitter.created_threads == [("C1", "t", "M9")]


async def test_create_thread_window_denied_fails_loud():
    result, emitter, _ = await _run(
        'await create_thread("t")\n', limiter=_StubLimiter(allow=False)
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "channel_messages_per_min"
    assert emitter.created_threads == []  # window denied before the REST create


async def test_create_post_spends_message_and_window():
    script = 'pid = await create_post("Title", "body", ["news"])\nawait send_message(pid)\n'
    result, emitter, limiter = await _run(script)
    assert result.outcome == "ok", result.error
    assert emitter.created_posts == [("C1", "Title", "body", ["news"])]
    assert emitter.messages[0][1] == "post1"
    assert (channel_message_key("C1"), CHANNEL_MESSAGES_PER_MIN, None) in limiter.calls
    assert (guild_thread_ops_key("G1"), GUILD_THREAD_OPS_PER_MIN, None) in limiter.calls


# -- send_message thread-of-home relaxation (standard tier) --------------------


async def test_standard_send_into_home_thread_allowed():
    emitter = _FakeEmitter(parent_by_thread={"T1": "C1"})
    result, emitter, _ = await _run(
        'await send_message("in thread", "T1")\n', emitter=emitter
    )
    assert result.outcome == "ok", result.error
    assert emitter.messages == [("T1", "in thread")]


async def test_standard_send_into_foreign_thread_denied():
    # A thread whose parent is a different channel is still a cross-channel send.
    emitter = _FakeEmitter(parent_by_thread={"T2": "OTHER"})
    result, emitter, _ = await _run(
        'await send_message("nope", "T2")\n', emitter=emitter
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "cross_channel_send"
    assert emitter.messages == []


async def test_home_thread_parent_lookup_cached_across_sends():
    emitter = _FakeEmitter(parent_by_thread={"T1": "C1"})
    script = (
        'await send_message("one", "T1")\n'
        'await send_message("two", "T1")\n'
    )
    result, emitter, _ = await _run(
        script, budget=HandlerBudget(max_messages=5), emitter=emitter
    )
    assert result.outcome == "ok", result.error
    assert emitter.parent_calls == ["T1"]  # verified once, then cached


# -- admin thread ops (actor set) ----------------------------------------------


async def test_admin_list_threads_foreign_channel_in_guild_allowed():
    # Empty scope = guild-wide: a channel in THIS guild is readable.
    emitter = _FakeEmitter(
        threads_by_channel={"OTHER": [{"thread_id": "TX"}]},
        guild_by_channel={"OTHER": "G1"},
    )
    script = (
        'threads = await list_threads("OTHER")\n'
        'await send_message(f"{len(threads)}", "OTHER")\n'
    )
    result, emitter, _ = await _run(
        script, budget=admin_budget(), actor=_FakeActor(), emitter=emitter
    )
    assert result.outcome == "ok", result.error
    assert result.usage["discord_reads"] == 1
    assert ("OTHER", "1") in emitter.messages


async def test_admin_list_threads_foreign_guild_channel_denied():
    # Empty scope but the channel belongs to a DIFFERENT guild the bot inhabits:
    # the guild-scope rail denies the read before spending a discord read.
    emitter = _FakeEmitter(
        threads_by_channel={"OTHER": [{"thread_id": "TX"}]},
        guild_by_channel={"OTHER": "OTHER_GUILD"},
    )
    result, emitter, _ = await _run(
        'await list_threads("OTHER")\n',
        budget=admin_budget(), actor=_FakeActor(), emitter=emitter,
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "out_of_scope_channel"
    assert result.usage["discord_reads"] == 0  # denied before spending


async def test_admin_list_threads_out_of_channel_scope_denied():
    # A non-empty scope is an exact allow-list; a channel outside it is denied
    # purely (no guild fetch needed).
    emitter = _FakeEmitter(threads_by_channel={"OTHER": [{"thread_id": "TX"}]})
    result, emitter, _ = await _run(
        'await list_threads("OTHER")\n',
        budget=admin_budget(), actor=_FakeActor(), emitter=emitter,
        channel_ids=["C2", "C3"],
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "out_of_scope_channel"
    assert result.usage["discord_reads"] == 0
    assert emitter.guild_calls == []  # scope allow-list resolved without a fetch


async def test_admin_list_threads_in_channel_scope_allowed():
    emitter = _FakeEmitter(threads_by_channel={"C2": [{"thread_id": "TX"}]})
    result, emitter, _ = await _run(
        'threads = await list_threads("C2")\nawait send_message(f"{len(threads)}", "C2")\n',
        budget=admin_budget(), actor=_FakeActor(), emitter=emitter,
        channel_ids=["C2", "C3"],
    )
    assert result.outcome == "ok", result.error
    assert result.usage["discord_reads"] == 1
    assert emitter.guild_calls == []  # in-scope: no guild fetch
    assert ("C2", "1") in emitter.messages


async def test_admin_close_thread_spends_thread_op_and_guild_window():
    actor = _FakeActor()
    result, _, limiter = await _run(
        'ok = await close_thread("T1")\nawait send_message(f"{ok}")\n',
        budget=admin_budget(),
        actor=actor,
    )
    assert result.outcome == "ok", result.error
    assert ("close", "T1") in actor.calls
    assert result.usage["thread_ops"] == 1
    assert (guild_thread_ops_key("G1"), GUILD_THREAD_OPS_PER_MIN, None) in limiter.calls


async def test_admin_lock_reopen_thread_metered():
    actor = _FakeActor()
    script = 'await lock_thread("T1")\nawait reopen_thread("T2")\n'
    result, _, _ = await _run(script, budget=admin_budget(), actor=actor)
    assert result.outcome == "ok", result.error
    assert ("lock", "T1") in actor.calls and ("reopen", "T2") in actor.calls
    assert result.usage["thread_ops"] == 2


async def test_admin_thread_op_budget_cap_breaches():
    actor = _FakeActor()
    script = "for i in range(5):\n    await delete_thread(str(i))\n"
    result, _, _ = await _run(
        script, budget=HandlerBudget(max_thread_ops=2), actor=actor
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "thread_ops"
    assert len(actor.calls) == 2  # two ops before the third breaches


async def test_admin_thread_op_guild_window_denied_fails_loud():
    actor = _FakeActor()
    result, _, _ = await _run(
        'await close_thread("T1")\n',
        budget=admin_budget(),
        actor=actor,
        limiter=_StubLimiter(allow=False),
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "guild_thread_ops_per_min"
    assert actor.calls == []  # window denied before the REST mutation


async def test_admin_delete_gone_thread_returns_false_without_error():
    actor = _FakeActor(gone={"T1"})
    script = (
        'gone = await delete_thread("T1")\n'
        'await send_message("noop" if gone is False else "deleted")\n'
    )
    result, emitter, _ = await _run(
        script, budget=admin_budget(), actor=actor, emitter=None
    )
    assert result.outcome == "ok", result.error
    assert emitter.messages[0][1] == "noop"


async def test_standard_handler_has_no_thread_op_functions():
    # No actor -> close_thread is undefined in the sandbox -> NameError -> error.
    result, _, _ = await _run('await close_thread("T1")\n')
    assert result.outcome == "error"


# -- admin edit_message (actor set) --------------------------------------------

from smarter_dev.web.handler_caps import (
    RENAME_WINDOW_SECONDS,
    RENAMES_PER_WINDOW,
    channel_rename_key,
)


@dataclass
class _CountingLimiter:
    """Real fixed-window behaviour: denies once a key's count exceeds its limit."""

    counts: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    async def hit(self, key: str, limit: int, window_seconds: int | None = None) -> bool:
        self.calls.append((key, limit, window_seconds))
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key] <= limit


async def test_edit_message_absent_without_actor():
    # No actor -> edit_message is undefined in the sandbox -> NameError -> error.
    result, _, _ = await _run('await edit_message("M1", "new")\n')
    assert result.outcome == "error"


async def test_edit_message_admin_edits_bot_message_default_channel():
    actor = _FakeActor()
    result, emitter, _ = await _run(
        'await edit_message("M9", "updated rules")\n',
        budget=admin_budget(),
        actor=actor,
    )
    assert result.outcome == "ok", result.error
    assert emitter.edits == [("C1", "M9", "updated rules")]
    assert result.usage["messages_sent"] == 1


async def test_edit_message_targets_explicit_channel():
    actor = _FakeActor()
    result, emitter, _ = await _run(
        'await edit_message("M9", "text", "C2")\n',
        budget=admin_budget(),
        actor=actor,
    )
    assert result.outcome == "ok", result.error
    assert emitter.edits == [("C2", "M9", "text")]


async def test_edit_message_spends_message_budget_and_breaches_at_sixth():
    actor = _FakeActor()
    script = "for i in range(6):\n    await edit_message('M9', str(i))\n"
    result, emitter, _ = await _run(script, budget=admin_budget(), actor=actor)
    assert result.outcome == "cap_exceeded"
    assert result.cap == "messages"
    # Admin cap is 5: five edits went out before the sixth breached.
    assert len(emitter.edits) == 5


async def test_edit_message_does_not_hit_channel_message_window():
    actor = _FakeActor()
    limiter = _StubLimiter()
    result, emitter, limiter = await _run(
        'await edit_message("M9", "a")\nawait edit_message("M9", "b")\n',
        budget=admin_budget(),
        actor=actor,
        limiter=limiter,
    )
    assert result.outcome == "ok", result.error
    # An edit is not channel volume: the per-channel message window is untouched.
    assert not any(
        key == channel_message_key("C1") for key, _, _ in limiter.calls
    )


# -- admin rename_channel (actor set) ------------------------------------------


async def test_rename_channel_absent_without_actor():
    # No actor -> rename_channel is undefined -> NameError -> error.
    result, _, _ = await _run('await rename_channel("C1", "x")\n')
    assert result.outcome == "error"


async def test_rename_channel_admin_spends_mod_action_and_renames():
    actor = _FakeActor()
    limiter = _CountingLimiter()
    # In-scope via channel_ids so the scope check resolves without a guild fetch.
    result, emitter, limiter = await _run(
        'await rename_channel("C1", "📊Members: 1.2k")\n',
        budget=admin_budget(),
        actor=actor,
        limiter=limiter,
        channel_ids=["C1"],
    )
    assert result.outcome == "ok", result.error
    assert emitter.renames == [("C1", "📊Members: 1.2k")]
    assert result.usage["mod_actions"] == 1
    assert (
        channel_rename_key("C1"),
        RENAMES_PER_WINDOW,
        RENAME_WINDOW_SECONDS,
    ) in limiter.calls


async def test_rename_channel_out_of_scope_raises():
    actor = _FakeActor()
    result, emitter, _ = await _run(
        'await rename_channel("OTHER", "x")\n',
        budget=admin_budget(),
        actor=actor,
        channel_ids=["C1", "C2"],
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "out_of_scope_channel"
    assert emitter.renames == []  # denied before the REST call
    assert result.usage["mod_actions"] == 0


async def test_rename_channel_guild_wide_verifies_guild_ownership():
    # Empty scope + foreign-guild channel: the guild-scope rail denies it.
    emitter = _FakeEmitter(guild_by_channel={"OTHER": "OTHER_GUILD"})
    result, emitter, _ = await _run(
        'await rename_channel("OTHER", "x")\n',
        budget=admin_budget(),
        actor=_FakeActor(),
        emitter=emitter,
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "out_of_scope_channel"
    assert emitter.renames == []


async def test_rename_channel_third_in_window_raises_channel_renames_cap():
    actor = _FakeActor()
    limiter = _CountingLimiter()
    script = "for i in range(3):\n    await rename_channel('C1', str(i))\n"
    result, emitter, _ = await _run(
        script, budget=admin_budget(), actor=actor, limiter=limiter,
        channel_ids=["C1"],
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "channel_renames_per_10min"
    # Two renames land before the third breaches the 2/600s window.
    assert len(emitter.renames) == 2


# -- admin role mutation (add_role / remove_role) + ban purge window (E2) --

from smarter_dev.web.handler_caps import guild_role_changes_key  # noqa: E402


async def test_role_functions_absent_for_standard_tier():
    # No actor -> add_role/remove_role not injected -> NameError -> error.
    result, _, _ = await _run('await add_role("U1", "R1")\n')
    assert result.outcome == "error"
    result, _, _ = await _run('await remove_role("U1", "R1")\n')
    assert result.outcome == "error"


async def test_role_functions_present_with_actor():
    actor = _FakeActor()
    script = 'ok = await add_role(context["author_id"], "R1")\n'
    result, _, _ = await _run(
        script, budget=admin_budget(), actor=actor, allowed_role_ids=["R1"]
    )
    assert result.outcome == "ok", result.error
    assert ("add_role", "U1", "R1", None) in actor.calls


async def test_add_role_denied_when_role_not_in_allowlist():
    """Fail-closed: a role not on the allowlist raises before any spend/REST."""
    actor = _FakeActor()
    limiter = _StubLimiter()
    script = 'await add_role(context["author_id"], "R9")\n'
    result, _, _ = await _run(
        script, budget=admin_budget(), actor=actor, limiter=limiter,
        allowed_role_ids=["R1"],
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "role_not_allowed"
    assert actor.calls == []          # actor never called
    assert limiter.calls == []        # window never charged
    assert result.usage["role_changes"] == 0  # budget never spent


async def test_add_role_denied_when_allowlist_empty():
    actor = _FakeActor()
    script = 'await add_role(context["author_id"], "R1")\n'
    result, _, _ = await _run(
        script, budget=admin_budget(), actor=actor, allowed_role_ids=[],
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "role_not_allowed"
    assert actor.calls == []


async def test_add_role_allowed_role_spends_budget_and_window_and_calls_actor():
    actor = _FakeActor()
    limiter = _StubLimiter()
    script = 'await add_role(context["author_id"], "R1", reason="onboard")\n'
    result, _, _ = await _run(
        script, budget=admin_budget(), actor=actor, limiter=limiter,
        allowed_role_ids=["R1"],
    )
    assert result.outcome == "ok", result.error
    assert ("add_role", "U1", "R1", "onboard") in actor.calls
    assert result.usage["role_changes"] == 1
    assert (guild_role_changes_key("G1"), 30, None) in limiter.calls


async def test_add_role_guild_window_breach_mid_fire_caps():
    actor = _FakeActor()
    limiter = _StubLimiter(allow=False)
    script = 'await add_role(context["author_id"], "R1")\n'
    result, _, _ = await _run(
        script, budget=admin_budget(), actor=actor, limiter=limiter,
        allowed_role_ids=["R1"],
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "guild_role_changes_per_min"
    assert actor.calls == []  # window checked before the REST call


async def test_add_role_member_gone_returns_false_outcome_ok():
    actor = _FakeActor(gone={"U1"})
    script = (
        'ok = await add_role(context["author_id"], "R1")\n'
        'await send_message("added" if ok else "member gone", "MODCHAT")\n'
    )
    result, emitter, _ = await _run(
        script, budget=admin_budget(), actor=actor, allowed_role_ids=["R1"],
    )
    assert result.outcome == "ok", result.error
    assert ("MODCHAT", "member gone") in emitter.messages


async def test_remove_role_member_gone_returns_false():
    actor = _FakeActor(gone={"U1"})
    script = (
        'ok = await remove_role(context["author_id"], "R1")\n'
        'await send_message("removed" if ok else "gone", "MODCHAT")\n'
    )
    result, emitter, _ = await _run(
        script, budget=admin_budget(), actor=actor, allowed_role_ids=["R1"],
    )
    assert result.outcome == "ok", result.error
    assert ("MODCHAT", "gone") in emitter.messages


async def test_add_role_budget_cap_breach():
    actor = _FakeActor()
    script = "for i in range(3):\n    await add_role(context['author_id'], 'R1')\n"
    result, _, _ = await _run(
        script, budget=HandlerBudget(max_role_changes=2), actor=actor,
        allowed_role_ids=["R1"],
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "role_changes"
    assert result.usage["role_changes"] == 2


async def test_ban_user_delete_message_seconds_passthrough():
    actor = _FakeActor()
    script = 'await ban_user(context["author_id"], "bot", 3600)\n'
    result, _, _ = await _run(script, budget=admin_budget(), actor=actor)
    assert result.outcome == "ok", result.error
    assert ("ban", "U1", "bot", 3600) in actor.calls


# -- schedule_timer (persisted one-shot self re-arm, E3) -----------------------

import datetime as _dt

from smarter_dev.web.handler_caps import (  # noqa: E402
    HANDLER_TIMERS_PER_HOUR,
    TIMER_ARMING_WINDOW_SECONDS,
    handler_timer_arm_key,
)


@dataclass
class _TimerRecorder:
    """Records schedule_timer enqueues; stands in for the fire job's closure."""

    calls: list = field(default_factory=list)

    async def __call__(self, fire_at, refire_context):
        self.calls.append((fire_at, refire_context))


async def test_schedule_timer_submits_refire_with_payload_and_scheduled_at():
    recorder = _TimerRecorder()
    before = _dt.datetime.now(_dt.timezone.utc)
    script = 'await schedule_timer(120, {"user_id": context["author_id"]})\n'
    result, _, limiter = await _run(
        script, timer_scheduler=recorder, handler_id="H1"
    )
    after = _dt.datetime.now(_dt.timezone.utc)
    assert result.outcome == "ok", result.error
    assert result.usage["timers_scheduled"] == 1
    assert len(recorder.calls) == 1
    fire_at, ctx = recorder.calls[0]
    # fire_at ≈ now + 120s.
    assert before + _dt.timedelta(seconds=120) <= fire_at <= after + _dt.timedelta(seconds=120)
    assert ctx["trigger_type"] == "timer"
    assert ctx["payload"] == {"user_id": "U1"}
    scheduled_at = _dt.datetime.fromisoformat(ctx["scheduled_at"])
    assert before <= scheduled_at <= after
    # The arming window was charged with the 3600s override on the handler key.
    assert (
        handler_timer_arm_key("H1"),
        HANDLER_TIMERS_PER_HOUR,
        TIMER_ARMING_WINDOW_SECONDS,
    ) in limiter.calls


async def test_schedule_timer_below_min_delay_errors():
    recorder = _TimerRecorder()
    result, _, _ = await _run(
        'await schedule_timer(59, {"k": 1})\n', timer_scheduler=recorder
    )
    assert result.outcome == "error"
    assert recorder.calls == []
    assert result.usage["timers_scheduled"] == 0


async def test_schedule_timer_above_max_delay_errors():
    recorder = _TimerRecorder()
    result, _, _ = await _run(
        f"await schedule_timer({30 * 86400 + 1}, {{}})\n", timer_scheduler=recorder
    )
    assert result.outcome == "error"
    assert recorder.calls == []


async def test_schedule_timer_non_json_payload_errors():
    recorder = _TimerRecorder()
    # A set is not JSON-serializable -> ValueError -> "error".
    result, _, _ = await _run(
        'await schedule_timer(120, {"bad": {1, 2}})\n', timer_scheduler=recorder
    )
    assert result.outcome == "error"
    assert recorder.calls == []


async def test_schedule_timer_oversize_payload_cap_exceeded():
    recorder = _TimerRecorder()
    script = 'await schedule_timer(120, {"blob": "x" * 5000})\n'
    result, _, _ = await _run(script, timer_scheduler=recorder)
    assert result.outcome == "cap_exceeded"
    assert result.cap == "timer_payload_size"
    assert recorder.calls == []


async def test_schedule_timer_budget_cap_breaches_standard():
    recorder = _TimerRecorder()
    script = "for i in range(3):\n    await schedule_timer(120, {'i': i})\n"
    result, _, _ = await _run(
        script, budget=HandlerBudget(max_timers=2), timer_scheduler=recorder
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "timers"
    # Two armed before the third breached.
    assert len(recorder.calls) == 2


async def test_schedule_timer_arming_window_declined():
    recorder = _TimerRecorder()
    result, _, _ = await _run(
        'await schedule_timer(120, {})\n',
        timer_scheduler=recorder,
        timer_limiter=_StubLimiter(allow=False),
        handler_id="H1",
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "handler_timers_per_hour"
    assert recorder.calls == []  # denied before the enqueue


async def test_schedule_timer_not_configured_raises():
    # Default _no_timer scheduler: schedule_timer is present but fails loud.
    result, _, _ = await _run('await schedule_timer(120, {})\n')
    assert result.outcome == "error"


async def test_schedule_timer_denied_budget_does_not_submit():
    # Ordering: a budget-denied arm never enqueues a job.
    recorder = _TimerRecorder()
    result, _, _ = await _run(
        'await schedule_timer(120, {})\n',
        budget=HandlerBudget(max_timers=0),
        timer_scheduler=recorder,
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "timers"
    assert recorder.calls == []


async def test_schedule_timer_available_to_admin_tier():
    recorder = _TimerRecorder()
    result, _, _ = await _run(
        'await schedule_timer(86400, {"user_id": "U9"})\n',
        budget=admin_budget(),
        actor=_FakeActor(),
        timer_scheduler=recorder,
        handler_id="H2",
    )
    assert result.outcome == "ok", result.error
    assert len(recorder.calls) == 1


# ---------------------------------------------------------------------------
# send_dm — admin-only DM emit with its own cap family (E2)
# ---------------------------------------------------------------------------


@dataclass
class _CountingLimiter:
    """Enforces whatever limit is passed to hit(), counting per key."""

    counts: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    async def hit(self, key, limit, window_seconds=None):
        self.calls.append((key, limit, window_seconds))
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key] <= limit


async def test_send_dm_absent_without_actor():
    # send_dm is admin-only — a standard fire never gets it.
    script = 'await send_dm("U9", "hi")\n'
    result, _, _ = await _run(script)
    assert result.outcome == "error"
    assert "send_dm" in result.error


async def test_send_dm_present_for_admin():
    script = 'ok = await send_dm("U9", "hello")\n'
    emitter = _FakeEmitter()
    result, emitter, _ = await _run(
        script, budget=admin_budget(), actor=_FakeActor(), emitter=emitter
    )
    assert result.outcome == "ok"
    assert emitter.dm_sends == [("U9", "hello")]


async def test_send_dm_spends_message_pool_and_hits_both_windows():
    script = 'await send_dm("U9", "hi")\n'
    limiter = _StubLimiter()          # the global per-minute window
    dm_user_limiter = _StubLimiter()  # the per-recipient hour window
    result, _, _ = await _run(
        script, budget=admin_budget(), actor=_FakeActor(),
        limiter=limiter, dm_user_limiter=dm_user_limiter,
    )
    assert result.outcome == "ok"
    # One message-pool unit spent.
    assert result.usage["messages_sent"] == 1
    # The per-recipient HOUR window (3600s) is hit on the dedicated limiter.
    assert ("hcap:dmuser:U9", 30, 3600) in dm_user_limiter.calls
    # The global per-minute window rides the shared 60s limiter (no override).
    assert ("hcap:dm:global", 10, None) in limiter.calls


async def test_send_dm_30_pass_31st_raises_per_user_cap():
    # A realistic staff<->user relay conversation passes; a runaway breaches loud.
    script = (
        "async def run():\n"
        "    for i in range(31):\n"
        '        await send_dm("U9", "reply")\n'
        "await run()\n"
    )
    emitter = _FakeEmitter()
    result, emitter, _ = await _run(
        script,
        budget=HandlerBudget(max_messages=1000),
        actor=_FakeActor(),
        emitter=emitter,
        limiter=_StubLimiter(),            # global window never trips here
        dm_user_limiter=_CountingLimiter(),
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "dm_user_per_hour"
    # The 31st never reached the emitter (window checked before the REST call).
    assert len(emitter.dm_sends) == 30


async def test_send_dm_global_minute_cap_raises():
    script = (
        "async def run():\n"
        "    for i in range(11):\n"
        '        await send_dm("U9", "drip")\n'
        "await run()\n"
    )
    result, _, _ = await _run(
        script,
        budget=HandlerBudget(max_messages=1000),
        actor=_FakeActor(),
        limiter=_CountingLimiter(),         # global 10/min window trips at the 11th
        dm_user_limiter=_StubLimiter(),
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "global_dms_per_min"


async def test_send_dm_returns_false_does_not_raise():
    # A closed door (emitter False) is an expected value the script branches on,
    # NOT a cap breach or an error — the fire ends ok.
    script = (
        'delivered = await send_dm("U9", "hi")\n'
        "if delivered:\n"
        '    await send_message("logged")\n'
    )
    emitter = _FakeEmitter()
    emitter.dm_result = False
    result, emitter, _ = await _run(
        script, budget=admin_budget(), actor=_FakeActor(), emitter=emitter
    )
    assert result.outcome == "ok"
    assert result.cap is None
    assert emitter.dm_sends == [("U9", "hi")]
    assert emitter.messages == []  # the delivered branch did not run


# -- mod-audit surface: delete_webhook + reads (admin only) --------------------


async def test_delete_webhook_spends_mod_action_and_present_only_for_admin():
    actor = _FakeActor()
    script = "r = await delete_webhook('https://discord.com/api/webhooks/1/tok')\n"
    result, _, _ = await _run(
        script, budget=admin_budget(), actor=actor
    )
    assert result.outcome == "ok"
    assert ("delete_webhook", "https://discord.com/api/webhooks/1/tok") in actor.calls
    assert result.usage["mod_actions"] == 1

    # A standard (no-actor) handler has no delete_webhook function at all.
    standard = await _run(
        "await delete_webhook('https://discord.com/api/webhooks/1/tok')\n"
    )
    assert standard[0].outcome == "error"
    assert "delete_webhook" in (standard[0].error or "")


async def test_read_functions_admin_only_and_spend_lookup():
    actor = _FakeActor()

    async def reader(user_id, limit):
        return []

    script = (
        "a = await list_mod_actions('U1', 5)\n"
        "b = await get_member_info('U1')\n"
        "c = await search_guild_members('ali', 3)\n"
    )
    result, _, _ = await _run(
        script, budget=admin_budget(), actor=actor, mod_action_reader=reader
    )
    assert result.outcome == "ok"
    # One lookup spent per read call.
    assert result.usage["lookups"] == 3
    assert ("get_member_info", "U1") in actor.calls
    assert ("search_guild_members", "ali", 3) in actor.calls

    # None of the three reads exist for a standard (no-actor) handler.
    for fn in ("list_mod_actions('U1')", "get_member_info('U1')",
               "search_guild_members('x')"):
        res = await _run(f"await {fn}\n")
        assert res[0].outcome == "error"


async def test_read_lookup_cap_breach_raises_cap_exceeded():
    actor = _FakeActor()

    async def reader(user_id, limit):
        return []

    # Only ONE lookup allowed; the second read must breach.
    script = (
        "await get_member_info('U1')\n"
        "await get_member_info('U2')\n"
    )
    result, _, _ = await _run(
        script,
        budget=HandlerBudget(max_lookups=1, max_messages=5),
        actor=actor,
        mod_action_reader=reader,
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "lookups"


async def test_list_mod_actions_passes_through_channel_and_trigger_message_ids():
    actor = _FakeActor()
    captured = {}
    rows = [
        {"action_type": "ban", "channel_id": "C9", "trigger_message_id": "M9",
         "created_at": "2026-01-02T00:00:00+00:00"},
        {"action_type": "warn", "channel_id": None, "trigger_message_id": None,
         "created_at": "2026-01-01T00:00:00+00:00"},
    ]

    async def reader(user_id, limit):
        captured["args"] = (user_id, limit)
        return rows

    script = (
        "actions = await list_mod_actions('U7', 25)\n"
        "await send_message(str(len(actions)))\n"
        "await send_message(str(actions[1]['channel_id']))\n"
    )
    result, emitter, _ = await _run(
        script, budget=admin_budget(), actor=actor, mod_action_reader=reader
    )
    assert result.outcome == "ok"
    # guild id is bound host-side by the reader; the script controls user + limit.
    assert captured["args"] == ("U7", 25)
    # Rows carrying None channel/message ids are passed through unchanged.
    assert emitter.messages[0][1] == "2"
    assert emitter.messages[1][1] == "None"
    assert result.usage["lookups"] == 1
