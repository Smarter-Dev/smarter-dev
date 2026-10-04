"""The target is searched in decoded part texts, never a serialised form,
and attribution is recognised only at its rendered position."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.messages import UserPromptPart

from smarter_dev.bot.privacy import attribution
from smarter_dev.bot.privacy import purge
from smarter_dev.bot.proactive.agent import memory_note_pair
from smarter_dev.shared.privacy_purge import PurgeTarget

KAI = "111111111111111111"
NIA = "222222222222222222"


def _line(text: str) -> str:
    return f"[2026-10-03T10:00:00Z] [id=9] A·nia (uid={NIA}): {text}"


CASES = [
    ('Kai "Q"', 'met Kai "Q" today'),  # a quote in the name
    ("kai\\q", "met kai\\q today"),  # a backslash in the name
    ("kai", "met\tkai today"),  # right after a tab
    ("kai", "met\nkai today"),  # right after a newline
]


@pytest.mark.parametrize(("name", "said"), CASES, ids=["quote", "bslash", "tab", "nl"])
def test_proactive_skip_rule_finds_names_inside_structured_tool_returns(name, said):
    target = PurgeTarget.build(KAI, [name])
    history = [
        *memory_note_pair(f"nia (uid={NIA}) asked", attributed=True),
        ModelRequest(parts=[UserPromptPart(_line("hi"))]),
        # A host-written structured tool return: only searched, so the name
        # search is the only thing that can make this history dirty.
        ModelRequest(parts=[ToolReturnPart("web_search", {"snippet": said}, "t1")]),
    ]
    assert purge.proactive_history_is_clean(history[:3], target)
    assert not purge.proactive_history_is_clean(history, target)


@pytest.mark.parametrize(("name", "said"), CASES, ids=["quote", "bslash", "tab", "nl"])
def test_tool_call_args_json_string_is_decoded(name, said):
    import json

    target = PurgeTarget.build(KAI, [name])
    history = [
        ModelResponse(
            parts=[ToolCallPart("send_channel_message", json.dumps({"content": said}))]
        )
    ]
    texts = purge._part_texts(history)
    assert any(target.mentions(text) for text in texts)


def test_chat_skip_rule_reads_xml_escaped_nickname():
    target = PurgeTarget.build(KAI, ['Kai "Q" & co'])
    history = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    f'<message id="1" user-id="{NIA}" username="nia">\n'
                    "say hi to Kai &quot;Q&quot; &amp; co\n</message>"
                )
            ]
        ),
        ModelResponse(parts=[TextPart("ok")]),
    ]
    assert not purge.chat_memory_is_clean(history, None, None, target)


def test_uid_counts_only_at_its_rendered_position():
    for forged in (
        # pre-uid line whose text carries " (uid=N): "
        f"[2026-10-03T10:00:00Z] [id=6] B·kaizen: says (uid={NIA}): hi",
        # a display name shaped like attribution, before the real uid
        f"[2026-10-03T10:00:00Z] [id=6] B·nia (uid={NIA}) (uid={KAI}): hi",
    ):
        assert attribution.attributed_line(forged) is None
    assert attribution.attributed_line(_line("x (uid=5): y")).group("uid") == NIA


def test_chat_tag_recognised_only_as_a_whole_rendered_line():
    history = [
        ModelRequest(parts=[UserPromptPart(f'<message id="1" user-id="{NIA}">x')])
    ]
    assert not attribution.chat_history_attributed(history)
    ok = [
        ModelRequest(
            parts=[UserPromptPart(f'<message id="1" user-id="{NIA}">\nx\n</message>')]
        )
    ]
    assert attribution.chat_history_attributed(ok)
