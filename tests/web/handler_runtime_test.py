"""Tests for the Monty handler runtime and its rails (offline)."""

from __future__ import annotations

from dataclasses import dataclass, field

from smarter_dev.web.handler_budget import CapExceeded, HandlerBudget
from smarter_dev.web.handler_runtime import run_handler_script


@dataclass
class _FakeEmitter:
    messages: list[tuple[str, str]] = field(default_factory=list)
    reactions: list[tuple[str, str, str]] = field(default_factory=list)
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

    async def create_message(self, channel_id: str, content: str) -> str:
        self.messages.append((channel_id, content))
        return f"msg{len(self.messages)}"

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append((channel_id, message_id, emoji))

    async def list_threads(self, channel_id: str, limit: int = 50) -> list:
        return self.threads_by_channel.get(channel_id, [])

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


@dataclass
class _StubLimiter:
    allow: bool = True
    calls: list = field(default_factory=list)

    async def hit(self, key: str, limit: int) -> bool:
        self.calls.append((key, limit))
        return self.allow


@dataclass
class _FakeActor:
    calls: list = field(default_factory=list)
    # Thread ids that model a gone target (404): the mutation returns False.
    gone: set = field(default_factory=set)

    async def ban_user(self, user_id, reason=None):
        self.calls.append(("ban", user_id, reason)); return f"banned {user_id}"

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


async def _run(
    script, *, budget=None, emitter=None, limiter=None, agent_runner=None,
    actor=None, channel_ids=None,
):
    emitter = emitter or _FakeEmitter()
    limiter = limiter or _StubLimiter()
    kwargs = {}
    if agent_runner is not None:
        kwargs["agent_runner"] = agent_runner
    if actor is not None:
        kwargs["actor"] = actor
    if channel_ids is not None:
        kwargs["channel_ids"] = channel_ids
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
    assert (channel_message_key("C1"), CHANNEL_MESSAGES_PER_MIN) in limiter.calls
    # §5.3: a create is a mutating thread op and draws the guild thread-op window
    # too — but it spends the message budget, not the thread_ops counter.
    assert (guild_thread_ops_key("G1"), GUILD_THREAD_OPS_PER_MIN) in limiter.calls
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
    assert (channel_message_key("C1"), CHANNEL_MESSAGES_PER_MIN) in limiter.calls
    assert (guild_thread_ops_key("G1"), GUILD_THREAD_OPS_PER_MIN) in limiter.calls


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
    assert (guild_thread_ops_key("G1"), GUILD_THREAD_OPS_PER_MIN) in limiter.calls


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
