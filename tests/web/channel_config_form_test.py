"""Form parsing for the per-channel bot configuration page.

Parsing is a pure function so the validation rules — which are what stop a
bad form from writing nonsense into a channel's model or budgets — are
testable without a request.
"""

from __future__ import annotations

import pytest

from smarter_dev.web.bot_admin.channel_config import (
    ChannelConfigForm,
    parse_channel_config_form,
)


def _form(**overrides) -> dict:
    fields = {
        "bot_kind": "legacy",
        "model_key": "gemini-3-7-flash",
        "reasoning_level": "",
        "daily_token_budget": "0",
        "hourly_token_budget": "0",
        "auto_respond": "",
        "fallback_model_key": "",
        "response_filter": "",
        "drafter_model": "",
    }
    fields.update(overrides)
    return fields


def test_parses_a_legacy_configuration():
    parsed = parse_channel_config_form(_form(auto_respond="on"))
    assert parsed == ChannelConfigForm(
        proactive_enabled=False,
        model_key="gemini-3-7-flash",
        reasoning_level=None,
        daily_token_budget=0,
        hourly_token_budget=0,
        auto_respond=True,
        fallback_model_key=None,
        response_filter=None,
        drafter_model=None,
    )


def test_proactive_kind_sets_the_selector():
    parsed = parse_channel_config_form(_form(bot_kind="proactive"))
    assert parsed.proactive_enabled is True
    # The model settings still parse: they drive the proactive agent too.
    assert parsed.model_key == "gemini-3-7-flash"


def test_blank_model_means_server_default():
    parsed = parse_channel_config_form(_form(model_key=""))
    assert parsed.model_key is None


def test_unknown_model_key_is_rejected():
    with pytest.raises(ValueError, match="model"):
        parse_channel_config_form(_form(model_key="not-a-real-model"))


def test_unknown_fallback_model_is_rejected():
    with pytest.raises(ValueError, match="fallback"):
        parse_channel_config_form(_form(fallback_model_key="nope"))


def test_unknown_reasoning_level_is_rejected():
    with pytest.raises(ValueError, match="reasoning"):
        parse_channel_config_form(_form(reasoning_level="telepathic"))


def test_valid_reasoning_level_is_kept():
    parsed = parse_channel_config_form(_form(reasoning_level="high"))
    assert parsed.reasoning_level == "high"


@pytest.mark.parametrize("field", ["daily_token_budget", "hourly_token_budget"])
def test_budgets_reject_negatives_and_nonsense(field):
    with pytest.raises(ValueError, match="budget"):
        parse_channel_config_form(_form(**{field: "-1"}))
    with pytest.raises(ValueError, match="budget"):
        parse_channel_config_form(_form(**{field: "lots"}))


def test_budgets_accept_zero_and_positive():
    parsed = parse_channel_config_form(
        _form(daily_token_budget="50000", hourly_token_budget="0")
    )
    assert parsed.daily_token_budget == 50000
    assert parsed.hourly_token_budget == 0


def test_blank_text_fields_become_none_not_empty_strings():
    parsed = parse_channel_config_form(
        _form(response_filter="   ", drafter_model="")
    )
    assert parsed.response_filter is None
    assert parsed.drafter_model is None


def test_response_filter_is_kept_verbatim_when_set():
    parsed = parse_channel_config_form(
        _form(response_filter="only answer python questions")
    )
    assert parsed.response_filter == "only answer python questions"


def test_unknown_bot_kind_is_rejected():
    with pytest.raises(ValueError, match="bot"):
        parse_channel_config_form(_form(bot_kind="sentient"))
