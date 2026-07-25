"""Tests for the /rule slash command and its pure rendering helpers.

The model is always mocked: nothing here may reach a real LLM. The rendering
rail (rule text verbatim, explanation truncatable, no mass mentions) and the
validation rail (a hallucinated id or index posts nothing publicly) are the two
things these tests exist to hold.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import hikari
import pytest

from smarter_dev.bot.agents.rule_matcher import RuleCitation
from smarter_dev.bot.agents.rule_matcher import RuleMatchMessage
from smarter_dev.bot.plugins import rules as rules_plugin
from smarter_dev.bot.plugins.rules import CHARS_PER_TOKEN
from smarter_dev.bot.plugins.rules import DISCORD_MESSAGE_CHAR_LIMIT
from smarter_dev.bot.plugins.rules import MAX_RULES_PROMPT_CHARS
from smarter_dev.bot.plugins.rules import MINIMUM_EXPLANATION_CHARS
from smarter_dev.bot.plugins.rules import RULE_COMMAND_TYPE
from smarter_dev.bot.plugins.rules import RULE_CONTEXT_MESSAGE_COUNT
from smarter_dev.bot.plugins.rules import bot_target_rejection
from smarter_dev.bot.plugins.rules import collect_recent_messages
from smarter_dev.bot.plugins.rules import estimate_rule_match_tokens
from smarter_dev.bot.plugins.rules import format_rule_block
from smarter_dev.bot.plugins.rules import no_candidates_message
from smarter_dev.bot.plugins.rules import no_match_message
from smarter_dev.bot.plugins.rules import rate_limit_message
from smarter_dev.bot.plugins.rules import render_rule_citation
from smarter_dev.bot.plugins.rules import rules_within_prompt_budget
from smarter_dev.bot.plugins.rules import select_candidate_message_ids
from smarter_dev.bot.plugins.rules import to_rule_match_message
from smarter_dev.bot.plugins.rules import truncate_with_ellipsis
from smarter_dev.bot.services.rate_limiter import RateLimiter
from smarter_dev.web.guild_rules import GuildRule
from smarter_dev.web.guild_rules import parse_guild_rules

ZERO_WIDTH_SPACE = "​"

GUILD_ID = 111
CHANNEL_ID = 555
BOT_USER_ID = 42
INVOKER_ID = 999
OFFENDER_ID = 222
BYSTANDER_ID = 333

NOW = datetime(2026, 7, 25, 12, 0)

RULES_MARKDOWN = """\
## No self-promotion
Do not advertise a product or service without asking a moderator first.

## Be kind
No personal attacks, slurs, or harassment of other members.
"""


def _rule(index: int = 1, title: str = "No self-promotion", body: str = "Ask first.") -> GuildRule:
    return GuildRule(index=index, title=title, body=body)


def _match_message(
    *,
    message_id: str = "1001",
    author_id: str = str(OFFENDER_ID),
    author_display: str = "offender",
    content: str = "buy my course",
) -> RuleMatchMessage:
    return RuleMatchMessage(
        message_id=message_id,
        author_id=author_id,
        author_display=author_display,
        content=content,
    )


# --------------------------------------------------------------------------- #
# Pure helpers: rendering
# --------------------------------------------------------------------------- #


def test_format_rule_block_renders_title_and_body_verbatim():
    rule = _rule(body="Do **not** advertise.\n- not even once")

    block = format_rule_block(rule)

    assert "No self-promotion" in block
    assert "Do **not** advertise.\n- not even once" in block
    assert block.startswith("**1. No self-promotion**")


def test_format_rule_block_omits_empty_body():
    block = format_rule_block(_rule(body=""))

    assert block == "**1. No self-promotion**"


def test_render_rule_citation_puts_rules_before_the_explanation():
    rendered = render_rule_citation([_rule()], "They advertised a paid course.")

    assert rendered.index("No self-promotion") < rendered.index("paid course")


def test_render_rule_citation_keeps_rule_text_byte_for_byte():
    rules = parse_guild_rules(RULES_MARKDOWN)

    rendered = render_rule_citation(rules, "Explanation here.")

    for rule in rules:
        assert rule.title in rendered
        assert rule.body in rendered


def test_render_rule_citation_renders_every_matched_rule():
    rules = [_rule(1), _rule(2, title="Be kind", body="No attacks.")]

    rendered = render_rule_citation(rules, "why")

    assert "No self-promotion" in rendered
    assert "Be kind" in rendered


def test_render_rule_citation_truncates_the_explanation_not_the_rule():
    long_body = "b" * 1_900
    rule = _rule(body=long_body)
    explanation = "e" * 500

    rendered = render_rule_citation([rule], explanation)

    assert len(rendered) <= DISCORD_MESSAGE_CHAR_LIMIT
    assert long_body in rendered, "the rule body must never be cut"
    assert "…" in rendered
    assert explanation not in rendered


def test_render_rule_citation_drops_the_explanation_when_no_room_is_left():
    rule = _rule(body="b" * 1_950)

    rendered = render_rule_citation([rule], "an explanation that will not fit")

    assert len(rendered) <= DISCORD_MESSAGE_CHAR_LIMIT
    assert "b" * 1_950 in rendered
    assert "explanation that will not fit" not in rendered


def test_render_rule_citation_never_renders_a_partial_rule():
    rules = [
        _rule(1, title="First", body="a" * 1_500),
        _rule(2, title="Second", body="b" * 1_500),
    ]

    rendered = render_rule_citation(rules, "why")

    assert len(rendered) <= DISCORD_MESSAGE_CHAR_LIMIT
    assert "a" * 1_500 in rendered
    assert "b" * 1_500 not in rendered
    assert "1 more rule" in rendered


def test_render_rule_citation_is_nothing_at_all_when_no_rule_fits():
    """A post naming no rule and giving no explanation is worse than no post."""
    rules = [_rule(index, body="x" * 3_000) for index in (1, 2)]

    assert render_rule_citation(rules, "why") is None


def test_render_rule_citation_is_nothing_at_all_for_one_oversized_rule():
    rendered = render_rule_citation([_rule(body="b" * 2_100)], "why they broke it")

    assert rendered is None


def test_render_rule_citation_without_any_rule_renders_nothing():
    """The rendering rail: model prose is never posted on its own."""
    assert render_rule_citation([], "pure model prose, no rule behind it") is None


@pytest.mark.parametrize("char_limit", [0, -1, -100])
def test_truncate_with_ellipsis_never_exceeds_a_nonpositive_budget(char_limit: int):
    assert truncate_with_ellipsis("abcdefgh", char_limit) == ""


def test_truncate_with_ellipsis_respects_a_tiny_budget():
    assert truncate_with_ellipsis("abcdefgh", 3) == "ab…"


def test_format_rule_block_neutralizes_a_mass_mention_in_the_rule_text():
    """Defense in depth: an admin-authored rule cannot ping the server either."""
    rule = _rule(title="Do not ping @everyone", body="And never use @here.")

    block = format_rule_block(rule)

    assert "@everyone" not in block
    assert "@here" not in block
    assert f"@{ZERO_WIDTH_SPACE}everyone" in block
    assert f"@{ZERO_WIDTH_SPACE}here" in block


def test_render_rule_citation_neutralizes_a_mass_mention_in_the_rule_text():
    rules = parse_guild_rules("## No @everyone\nDon't ping @here either.\n")

    rendered = render_rule_citation(rules, "they did it")

    assert "@everyone" not in rendered
    assert "@here" not in rendered


def test_render_rule_citation_neutralizes_a_mass_mention_in_the_explanation():
    rendered = render_rule_citation([_rule()], "they pinged @everyone and @here")

    assert "@everyone" not in rendered
    assert "@here" not in rendered
    assert f"@{ZERO_WIDTH_SPACE}everyone" in rendered


def test_minimum_explanation_chars_is_the_drop_threshold():
    body_length = DISCORD_MESSAGE_CHAR_LIMIT - MINIMUM_EXPLANATION_CHARS
    rendered = render_rule_citation([_rule(body="b" * body_length)], "words")

    assert "words" not in rendered


# --------------------------------------------------------------------------- #
# Pure helpers: candidate selection and messaging
# --------------------------------------------------------------------------- #


def test_select_candidate_message_ids_excludes_the_bots_own_messages():
    messages = [
        _match_message(message_id="1", author_id=str(BOT_USER_ID)),
        _match_message(message_id="2", author_id=str(OFFENDER_ID)),
    ]

    assert select_candidate_message_ids(messages, str(BOT_USER_ID), None) == ("2",)


def test_select_candidate_message_ids_narrows_to_the_named_user():
    messages = [
        _match_message(message_id="1", author_id=str(BYSTANDER_ID)),
        _match_message(message_id="2", author_id=str(OFFENDER_ID)),
        _match_message(message_id="3", author_id=str(OFFENDER_ID)),
    ]

    selected = select_candidate_message_ids(messages, str(BOT_USER_ID), str(OFFENDER_ID))

    assert selected == ("2", "3")


def test_select_candidate_message_ids_is_empty_when_the_named_user_never_spoke():
    messages = [_match_message(message_id="1", author_id=str(BYSTANDER_ID))]

    assert select_candidate_message_ids(messages, str(BOT_USER_ID), str(OFFENDER_ID)) == ()


def test_select_candidate_message_ids_tolerates_an_unknown_bot_id():
    messages = [_match_message(message_id="1", author_id=str(OFFENDER_ID))]

    assert select_candidate_message_ids(messages, None, None) == ("1",)


def test_bot_target_rejection_explains_a_target_that_is_the_bots_own_message():
    reason = bot_target_rejection("1", {"1", "2"})

    assert reason is not None
    assert "my own message" in reason


def test_bot_target_rejection_is_none_for_a_member_message():
    assert bot_target_rejection("3", {"1", "2"}) is None


def test_to_rule_match_message_projects_a_discord_message():
    message = SimpleNamespace(
        id=1001,
        author=SimpleNamespace(id=OFFENDER_ID, username="offender"),
        content="buy my course",
    )

    assert to_rule_match_message(message) == RuleMatchMessage(
        message_id="1001",
        author_id=str(OFFENDER_ID),
        author_display="offender",
        content="buy my course",
    )


def test_to_rule_match_message_renders_contentless_messages_as_empty():
    message = SimpleNamespace(
        id=1001, author=SimpleNamespace(id=OFFENDER_ID, username="offender"), content=None
    )

    assert to_rule_match_message(message).content == ""


def test_rate_limit_message_names_the_wait():
    reset_at = NOW + timedelta(minutes=7)

    message = rate_limit_message(reset_at, NOW)

    assert "7 minutes" in message


def test_rate_limit_message_without_a_reset_time_still_says_to_wait():
    assert "later" in rate_limit_message(None, NOW).lower()


def test_no_candidates_message_names_the_user_when_one_was_given():
    message = no_candidates_message("offender", RULE_CONTEXT_MESSAGE_COUNT)

    assert "offender" in message
    assert str(RULE_CONTEXT_MESSAGE_COUNT) in message


def test_no_candidates_message_without_a_user_mentions_no_one():
    assert "@" not in no_candidates_message(None, RULE_CONTEXT_MESSAGE_COUNT)


def test_no_candidates_message_neutralizes_a_hostile_username():
    message = no_candidates_message("@everyone", RULE_CONTEXT_MESSAGE_COUNT)

    assert "@everyone" not in message


def test_no_match_message_carries_the_models_reasoning():
    message = no_match_message("Nothing in the rules covers being boring.")

    assert "no rule" in message.lower()
    assert "being boring" in message


def test_no_match_message_neutralizes_a_mass_mention():
    assert "@everyone" not in no_match_message("they pinged @everyone")


def test_estimate_rule_match_tokens_grows_with_the_conversation():
    rules = [_rule()]
    short = estimate_rule_match_tokens(rules, [_match_message(content="hi")], "why")
    long = estimate_rule_match_tokens(
        rules, [_match_message(content="hi" * 5_000)], "why"
    )

    assert 0 < short < long


def test_estimate_rule_match_tokens_counts_the_moderator_description():
    """The description goes into the prompt, so the budget has to be charged for it."""
    rules = [_rule()]
    messages = [_match_message()]

    unspoken = estimate_rule_match_tokens(rules, messages, "")
    verbose = estimate_rule_match_tokens(rules, messages, "d" * 6_000)

    assert verbose - unspoken == 6_000 // CHARS_PER_TOKEN


def test_rules_within_prompt_budget_keeps_whole_rules_and_counts_the_rest():
    rules = [_rule(index, body="b" * 400) for index in range(1, 11)]

    kept, dropped = rules_within_prompt_budget(rules, 1_000)

    assert dropped == 10 - len(kept)
    assert sum(len(rule.title) + len(rule.body) for rule in kept) <= 1_000
    assert [rule.index for rule in kept] == list(range(1, len(kept) + 1))


def test_rules_within_prompt_budget_keeps_everything_that_fits():
    rules = [_rule(index) for index in (1, 2)]

    kept, dropped = rules_within_prompt_budget(rules, MAX_RULES_PROMPT_CHARS)

    assert list(kept) == rules
    assert dropped == 0


def test_rules_within_prompt_budget_drops_a_single_oversized_rule():
    kept, dropped = rules_within_prompt_budget([_rule(body="b" * 50)], 10)

    assert kept == ()
    assert dropped == 1


# --------------------------------------------------------------------------- #
# Message collection
# --------------------------------------------------------------------------- #


class _FakeMessageIterator:
    """Stand-in for hikari's lazy channel-history iterator."""

    def __init__(self, messages: list) -> None:
        self._messages = messages
        self.requested_limit: int | None = None

    def limit(self, count: int) -> _FakeMessageIterator:
        limited = _FakeMessageIterator(self._messages[:count])
        limited.requested_limit = count
        self.requested_limit = count
        return limited

    async def __aiter__(self):
        for message in self._messages:
            yield message


class _RaisingMessageIterator:
    """Channel history that fails the way hikari does: on iteration, not on call."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def limit(self, count: int) -> _RaisingMessageIterator:
        return self

    async def __aiter__(self):
        raise self._error
        yield  # pragma: no cover - makes this an async generator


def _discord_message(message_id: int, author_id: int, content: str, username: str = "member"):
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=author_id, username=username),
        content=content,
    )


async def test_collect_recent_messages_returns_oldest_first():
    newest_first = [
        _discord_message(3, OFFENDER_ID, "third"),
        _discord_message(2, OFFENDER_ID, "second"),
        _discord_message(1, OFFENDER_ID, "first"),
    ]
    rest = Mock()
    rest.fetch_messages = Mock(return_value=_FakeMessageIterator(newest_first))

    collected = await collect_recent_messages(rest, CHANNEL_ID, 10)

    assert [message.content for message in collected] == ["first", "second", "third"]


async def test_collect_recent_messages_asks_discord_for_only_the_window():
    iterator = _FakeMessageIterator([_discord_message(1, OFFENDER_ID, "hi")])
    rest = Mock()
    rest.fetch_messages = Mock(return_value=iterator)

    await collect_recent_messages(rest, CHANNEL_ID, 10)

    rest.fetch_messages.assert_called_once_with(CHANNEL_ID)
    assert iterator.requested_limit == 10


# --------------------------------------------------------------------------- #
# /rule command
# --------------------------------------------------------------------------- #


class _FakeSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


@pytest.fixture(autouse=True)
def isolated_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> RateLimiter:
    """Give every test its own limiter so counts never leak between tests."""
    limiter = RateLimiter()
    monkeypatch.setattr(rules_plugin, "rate_limiter", limiter)
    return limiter


def _ctx(
    *,
    description: str = "they advertised a course",
    target_user=None,
    channel_messages: list | None = None,
    in_guild: bool = True,
):
    """A lightbulb context stand-in with a channel history and a REST mock."""
    if channel_messages is None:
        channel_messages = [
            _discord_message(1001, OFFENDER_ID, "buy my course", "offender"),
            _discord_message(1002, BYSTANDER_ID, "no thanks", "bystander"),
        ]

    ctx = Mock()
    ctx.options = SimpleNamespace(rule=description, user=target_user)
    ctx.channel_id = CHANNEL_ID
    ctx.respond = AsyncMock()
    ctx.author = SimpleNamespace(id=INVOKER_ID, username="mod")
    ctx.get_guild = Mock(return_value=Mock(id=GUILD_ID) if in_guild else None)

    ctx.bot = Mock()
    ctx.bot.get_me = Mock(return_value=SimpleNamespace(id=BOT_USER_ID))
    ctx.bot.rest = Mock()
    # Discord returns channel history newest-first.
    ctx.bot.rest.fetch_messages = Mock(
        return_value=_FakeMessageIterator(list(reversed(channel_messages)))
    )
    ctx.bot.rest.create_message = AsyncMock()
    return ctx


def _run_rule(
    ctx,
    *,
    rules_markdown: str | None = RULES_MARKDOWN,
    citation: RuleCitation | None = None,
    match_side_effect=None,
):
    """Patch the DB read and the model call, returning the model mock."""
    config = None if rules_markdown is None else SimpleNamespace(rules_markdown=rules_markdown)
    matcher = AsyncMock(
        return_value=citation
        or RuleCitation(
            matched_rule_indices=[1],
            target_message_id="1001",
            explanation="They advertised a paid course without asking.",
        ),
        side_effect=match_side_effect,
    )
    patches = (
        patch.object(
            rules_plugin, "get_db_session_context", return_value=_FakeSessionCtx(AsyncMock())
        ),
        patch.object(
            rules_plugin.guild_rules_ops, "get_config", AsyncMock(return_value=config)
        ),
        patch.object(rules_plugin, "match_rule_violation", matcher),
    )
    return patches, matcher


async def _invoke(ctx, **kwargs):
    patches, matcher = _run_rule(ctx, **kwargs)
    with patches[0], patches[1], patches[2]:
        await rules_plugin.rule(ctx)
    return matcher


def _ephemeral_text(ctx) -> str:
    ctx.respond.assert_awaited()
    args, kwargs = ctx.respond.call_args
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    return args[0]


async def test_outside_a_guild_the_command_refuses():
    ctx = _ctx(in_guild=False)

    matcher = await _invoke(ctx)

    assert "server" in _ephemeral_text(ctx)
    matcher.assert_not_awaited()
    ctx.bot.rest.create_message.assert_not_awaited()


async def test_no_rules_configured_responds_ephemerally_without_calling_the_model():
    ctx = _ctx()

    matcher = await _invoke(ctx, rules_markdown=None)

    assert "no rules" in _ephemeral_text(ctx).lower()
    matcher.assert_not_awaited()
    ctx.bot.rest.create_message.assert_not_awaited()


async def test_rules_markdown_without_headings_counts_as_no_rules():
    ctx = _ctx()

    matcher = await _invoke(ctx, rules_markdown="just some prose, no headings")

    assert "no rules" in _ephemeral_text(ctx).lower()
    matcher.assert_not_awaited()


async def test_rate_limited_invoker_gets_a_wait_message_and_no_model_call(
    isolated_rate_limiter: RateLimiter,
):
    limit = isolated_rate_limiter.COMMAND_LIMITS[RULE_COMMAND_TYPE]["limit"]
    for _ in range(limit):
        isolated_rate_limiter.record_request(str(INVOKER_ID), 0, RULE_COMMAND_TYPE)
    ctx = _ctx()

    matcher = await _invoke(ctx)

    assert "rate limit" in _ephemeral_text(ctx).lower()
    matcher.assert_not_awaited()
    ctx.bot.rest.create_message.assert_not_awaited()


async def test_a_successful_run_consumes_one_rate_limit_slot(
    isolated_rate_limiter: RateLimiter,
):
    before = isolated_rate_limiter.get_user_remaining_requests(
        str(INVOKER_ID), RULE_COMMAND_TYPE
    )

    await _invoke(_ctx())

    after = isolated_rate_limiter.get_user_remaining_requests(
        str(INVOKER_ID), RULE_COMMAND_TYPE
    )
    assert after == before - 1


async def test_a_failing_model_call_still_consumes_a_slot_and_propagates(
    isolated_rate_limiter: RateLimiter,
):
    ctx = _ctx()

    with pytest.raises(RuntimeError):
        await _invoke(ctx, match_side_effect=RuntimeError("provider exploded"))

    assert isolated_rate_limiter.get_user_remaining_requests(
        str(INVOKER_ID), RULE_COMMAND_TYPE
    ) == isolated_rate_limiter.COMMAND_LIMITS[RULE_COMMAND_TYPE]["limit"] - 1
    ctx.bot.rest.create_message.assert_not_awaited()


async def test_a_named_user_with_no_messages_stops_before_the_model():
    ctx = _ctx(target_user=SimpleNamespace(id=777, username="silent"))

    matcher = await _invoke(ctx)

    text = _ephemeral_text(ctx)
    assert "silent" in text
    matcher.assert_not_awaited()
    ctx.bot.rest.create_message.assert_not_awaited()


async def test_a_named_user_narrows_the_candidates_handed_to_the_model():
    ctx = _ctx(target_user=SimpleNamespace(id=OFFENDER_ID, username="offender"))

    matcher = await _invoke(ctx)

    _, kwargs = matcher.call_args
    assert kwargs["target_author_id"] == str(OFFENDER_ID)


async def test_a_channel_with_only_bot_messages_stops_before_the_model():
    ctx = _ctx(
        channel_messages=[_discord_message(1001, BOT_USER_ID, "beep boop", "smarterbot")]
    )

    matcher = await _invoke(ctx)

    assert _ephemeral_text(ctx)
    matcher.assert_not_awaited()
    ctx.bot.rest.create_message.assert_not_awaited()


async def test_an_empty_channel_stops_before_the_model():
    ctx = _ctx(channel_messages=[])

    matcher = await _invoke(ctx)

    assert "no recent messages" in _ephemeral_text(ctx).lower()
    matcher.assert_not_awaited()


async def test_no_matching_rule_is_reported_ephemerally_and_posts_nothing():
    ctx = _ctx()
    citation = RuleCitation(
        matched_rule_indices=[],
        target_message_id="1001",
        explanation="Nothing in the rules covers this.",
    )

    await _invoke(ctx, citation=citation)

    text = _ephemeral_text(ctx)
    assert "no rule" in text.lower()
    assert "Nothing in the rules covers this." in text
    ctx.bot.rest.create_message.assert_not_awaited()


async def test_a_hallucinated_message_id_posts_nothing_public():
    ctx = _ctx()
    citation = RuleCitation(
        matched_rule_indices=[1], target_message_id="999", explanation="nope"
    )

    await _invoke(ctx, citation=citation)

    assert "999" in _ephemeral_text(ctx)
    ctx.bot.rest.create_message.assert_not_awaited()


async def test_an_out_of_range_rule_index_posts_nothing_public():
    ctx = _ctx()
    citation = RuleCitation(
        matched_rule_indices=[47], target_message_id="1001", explanation="nope"
    )

    await _invoke(ctx, citation=citation)

    assert "47" in _ephemeral_text(ctx)
    ctx.bot.rest.create_message.assert_not_awaited()


async def test_targeting_the_bots_own_message_posts_nothing_public():
    ctx = _ctx(
        channel_messages=[
            _discord_message(1001, OFFENDER_ID, "buy my course", "offender"),
            _discord_message(1002, BOT_USER_ID, "beep boop", "smarterbot"),
        ]
    )
    citation = RuleCitation(
        matched_rule_indices=[1], target_message_id="1002", explanation="the bot did it"
    )

    await _invoke(ctx, citation=citation)

    assert "my own message" in _ephemeral_text(ctx)
    ctx.bot.rest.create_message.assert_not_awaited()


async def test_a_match_posts_the_rule_verbatim_as_a_reply_to_the_target():
    ctx = _ctx()

    await _invoke(ctx)

    ctx.bot.rest.create_message.assert_awaited_once()
    args, kwargs = ctx.bot.rest.create_message.call_args
    assert args[0] == CHANNEL_ID
    posted = args[1]
    rules = parse_guild_rules(RULES_MARKDOWN)
    assert rules[0].title in posted
    assert rules[0].body in posted
    assert "They advertised a paid course without asking." in posted
    assert kwargs["reply"].id == 1001


async def test_the_public_post_cannot_ping_everyone_or_roles():
    ctx = _ctx()
    citation = RuleCitation(
        matched_rule_indices=[1],
        target_message_id="1001",
        explanation="they pinged @everyone and @here and <@&123>",
    )

    await _invoke(ctx, citation=citation)

    args, kwargs = ctx.bot.rest.create_message.call_args
    assert kwargs["mentions_everyone"] is False
    assert kwargs["role_mentions"] is False
    assert kwargs["user_mentions"] is False
    # Defense in depth: the rendered characters cannot form a mass mention either.
    assert "@everyone" not in args[1]
    assert "@here" not in args[1]


async def test_a_mass_mention_in_the_rules_document_cannot_ping_either():
    """The admin's own rule text is rendered defanged, not just the model's prose."""
    ctx = _ctx()

    await _invoke(
        ctx,
        rules_markdown="## Never ping @everyone\nDo not use @here either.\n",
    )

    posted = ctx.bot.rest.create_message.call_args.args[1]
    assert "@everyone" not in posted
    assert "@here" not in posted
    assert f"@{ZERO_WIDTH_SPACE}everyone" in posted


async def test_the_public_post_still_pings_the_replied_to_member():
    ctx = _ctx()

    await _invoke(ctx)

    _, kwargs = ctx.bot.rest.create_message.call_args
    assert kwargs["mentions_reply"] is True


async def test_a_match_confirms_to_the_invoker_ephemerally():
    ctx = _ctx()

    await _invoke(ctx)

    assert _ephemeral_text(ctx)


async def test_missing_post_permission_is_reported_to_the_invoker():
    ctx = _ctx()
    ctx.bot.rest.create_message = AsyncMock(
        side_effect=hikari.ForbiddenError(url="u", headers={}, raw_body=b"")
    )

    await _invoke(ctx)

    assert "permission" in _ephemeral_text(ctx).lower()


async def test_multiple_matched_rules_all_reach_the_public_post():
    ctx = _ctx()
    citation = RuleCitation(
        matched_rule_indices=[2, 1],
        target_message_id="1001",
        explanation="Both applied.",
    )

    await _invoke(ctx, citation=citation)

    posted = ctx.bot.rest.create_message.call_args.args[1]
    assert "No self-promotion" in posted
    assert "Be kind" in posted


async def test_the_model_sees_every_fetched_message_including_the_bots():
    ctx = _ctx(
        channel_messages=[
            _discord_message(1001, BOT_USER_ID, "beep boop", "smarterbot"),
            _discord_message(1002, OFFENDER_ID, "buy my course", "offender"),
        ]
    )
    citation = RuleCitation(
        matched_rule_indices=[1], target_message_id="1002", explanation="advertised"
    )

    matcher = await _invoke(ctx, citation=citation)

    passed_messages = matcher.call_args.args[2]
    assert [message.message_id for message in passed_messages] == ["1001", "1002"]


async def test_the_model_is_never_offered_the_bots_own_messages_as_targets():
    """The prompt must not list ids the command has already decided to reject."""
    ctx = _ctx(
        channel_messages=[
            _discord_message(1001, BOT_USER_ID, "beep boop", "smarterbot"),
            _discord_message(1002, OFFENDER_ID, "buy my course", "offender"),
        ]
    )
    citation = RuleCitation(
        matched_rule_indices=[1], target_message_id="1002", explanation="advertised"
    )

    matcher = await _invoke(ctx, citation=citation)

    assert matcher.call_args.kwargs["excluded_author_id"] == str(BOT_USER_ID)


# --------------------------------------------------------------------------- #
# Every path out of the command answers the invoker
# --------------------------------------------------------------------------- #


async def test_a_rule_too_long_to_quote_posts_nothing_and_says_so():
    """The invoker gets a reason, never a public post with no rule and no prose."""
    ctx = _ctx()

    await _invoke(ctx, rules_markdown="## No self-promotion\n" + "b" * 2_100)

    ctx.bot.rest.create_message.assert_not_awaited()
    assert "too long" in _ephemeral_text(ctx).lower()


async def test_a_target_message_deleted_mid_call_is_reported_to_the_invoker():
    ctx = _ctx()
    ctx.bot.rest.create_message = AsyncMock(
        side_effect=hikari.NotFoundError(url="u", headers={}, raw_body=b"")
    )

    await _invoke(ctx)

    assert "no longer" in _ephemeral_text(ctx).lower()


async def test_a_rejected_reply_target_is_reported_to_the_invoker():
    ctx = _ctx()
    ctx.bot.rest.create_message = AsyncMock(
        side_effect=hikari.BadRequestError(url="u", headers={}, raw_body=b"")
    )

    await _invoke(ctx)

    assert "no longer" in _ephemeral_text(ctx).lower()


async def test_missing_read_history_permission_is_reported_to_the_invoker():
    ctx = _ctx()
    ctx.bot.rest.fetch_messages = Mock(
        return_value=_RaisingMessageIterator(
            hikari.ForbiddenError(url="u", headers={}, raw_body=b"")
        )
    )

    matcher = await _invoke(ctx)

    assert "permission" in _ephemeral_text(ctx).lower()
    matcher.assert_not_awaited()
    ctx.bot.rest.create_message.assert_not_awaited()


async def test_an_unreadable_channel_is_reported_to_the_invoker():
    ctx = _ctx()
    ctx.bot.rest.fetch_messages = Mock(
        return_value=_RaisingMessageIterator(
            hikari.NotFoundError(url="u", headers={}, raw_body=b"")
        )
    )

    matcher = await _invoke(ctx)

    assert _ephemeral_text(ctx)
    matcher.assert_not_awaited()


async def test_a_discord_outage_while_reading_history_still_answers_the_invoker():
    """A 5xx must not leave the deferred ephemeral hanging.

    There is no global error handler behind this command, so an uncaught
    exception means the invoker's spinner runs out to "the application did not
    respond" with their rate-limit slot already spent.
    """
    ctx = _ctx()
    ctx.bot.rest.fetch_messages = Mock(
        return_value=_RaisingMessageIterator(
            hikari.InternalServerError(url="u", status=500, headers={}, raw_body=b"")
        )
    )

    with pytest.raises(hikari.InternalServerError):
        await _invoke(ctx)

    assert _ephemeral_text(ctx)


async def test_a_discord_outage_while_posting_still_answers_the_invoker():
    ctx = _ctx()
    ctx.bot.rest.create_message = AsyncMock(
        side_effect=hikari.InternalServerError(url="u", status=500, headers={}, raw_body=b"")
    )

    with pytest.raises(hikari.InternalServerError):
        await _invoke(ctx)

    # Unlike a deleted target, a 5xx leaves it genuinely unknown whether the
    # post landed, so the invoker is told to check rather than told nothing
    # happened.
    assert "check the channel" in _ephemeral_text(ctx).lower()


# --------------------------------------------------------------------------- #
# The shared token budget
# --------------------------------------------------------------------------- #


def _many_rules_markdown(rule_count: int, body_chars: int) -> str:
    return "".join(
        f"## Rule {index}\n{'r' * body_chars}\n\n"
        for index in range(1, rule_count + 1)
    )


async def test_a_call_too_big_for_the_remaining_token_budget_is_refused(
    isolated_rate_limiter: RateLimiter,
):
    """The pre-flight must ask about THIS call, not a hardcoded 1000 tokens."""
    headroom = 2_000
    isolated_rate_limiter.record_request(
        "someone-else", isolated_rate_limiter.TOKEN_LIMIT - headroom, "help"
    )
    ctx = _ctx()

    matcher = await _invoke(ctx, rules_markdown=_many_rules_markdown(100, 2_000))

    assert "usage limits" in _ephemeral_text(ctx).lower()
    matcher.assert_not_awaited()
    ctx.bot.rest.create_message.assert_not_awaited()


async def test_the_budget_is_charged_what_the_preflight_checked(
    isolated_rate_limiter: RateLimiter,
):
    checked: list[int] = []
    real_check = isolated_rate_limiter.check_token_limit

    def recording_check(estimated_tokens: int = 1_000) -> bool:
        checked.append(estimated_tokens)
        return real_check(estimated_tokens)

    isolated_rate_limiter.check_token_limit = recording_check
    ctx = _ctx()

    await _invoke(ctx)

    assert checked == [isolated_rate_limiter.get_current_token_usage()]


async def test_an_enormous_rules_document_is_capped_before_the_model_call():
    ctx = _ctx()

    matcher = await _invoke(ctx, rules_markdown=_many_rules_markdown(20, 5_000))

    sent_rules = matcher.call_args.args[1]
    assert 0 < len(sent_rules) < 20
    assert (
        sum(len(rule.title) + len(rule.body) for rule in sent_rules)
        <= MAX_RULES_PROMPT_CHARS
    )


async def test_a_rules_document_whose_first_rule_alone_is_too_big_stops_early():
    ctx = _ctx()

    matcher = await _invoke(
        ctx, rules_markdown=_many_rules_markdown(1, MAX_RULES_PROMPT_CHARS + 1)
    )

    assert "too long" in _ephemeral_text(ctx).lower()
    matcher.assert_not_awaited()
    ctx.bot.rest.create_message.assert_not_awaited()
