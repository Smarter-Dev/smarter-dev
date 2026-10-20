"""Replay recorded incidents through the coordinator, offline.

A :class:`ReplayCase` is everything one incident needs: the guild policy,
the messages in arrival order, what the reads would have answered (who is
staff, each author's history, the channel window a ring sweep reads) and what
the classifier answers each label. :func:`replay` runs it through a fresh
coordinator, with a classifier whose models are scripted stand-ins that count
their requests, and returns one :class:`~.coordinator.Evaluation` per message.

The cases themselves (the 2026-10-18 17:49 scam, the spam-ring incidents,
established-member negatives) live with the tests
(``tests/bot/moderation_coordinator/replay_cases.py``); a case built from a
production incident's evidence can be run the same way.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime

from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel

from smarter_dev.bot.moderation_coordinator.classifier import ClassifierBudget
from smarter_dev.bot.moderation_coordinator.classifier import ModerationClassifier
from smarter_dev.bot.moderation_coordinator.coordinator import Coordinator
from smarter_dev.bot.moderation_coordinator.coordinator import Evaluation
from smarter_dev.bot.moderation_coordinator.model import EVENT_PROMOTION
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.model import MessageFacts
from smarter_dev.bot.moderation_coordinator.model import participant_label
from smarter_dev.bot.moderation_coordinator.model import participant_of
from smarter_dev.bot.moderation_coordinator.policy import GuildPolicy
from smarter_dev.bot.spam_ring import AD_FIELD
from smarter_dev.bot.spam_ring import AuthorFacts
from smarter_dev.bot.spam_ring import TranscriptMessage

#: label -> (answer, confidence); a label missing here is not answered.
Answers = Mapping[str, tuple[bool, float]]
#: Answers that apply when the judged text contains the key (an ad's phrase),
#: over the case's defaults: one case can hold a no for an innocent question
#: and a yes for the ad that follows it.
AnswersByText = Mapping[str, Answers]


def _answers_for(material: str, answers: Answers, by_text: AnswersByText) -> Answers:
    chosen = dict(answers)
    for phrase, override in by_text.items():
        if phrase in material:
            chosen.update(override)
    return chosen


@dataclass
class ModelCalls:
    """What the scripted models were asked: one entry per request."""

    haiku: list[str] = field(default_factory=list)
    jev: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.haiku) + len(self.jev)


def scripted_haiku(
    answers: Answers, calls: ModelCalls, by_text: AnswersByText | None = None
) -> FunctionModel:
    """Haiku answering every label it knows, as the agreed JSON object."""

    def respond(messages, info: AgentInfo) -> ModelResponse:
        prompt = messages[-1].parts[-1].content
        calls.haiku.append(prompt)
        message = prompt.split("<untrusted>", 1)[-1].split("CONTEXT", 1)[0]
        body: dict = {"participants": {}}
        for label, (answer, confidence) in _answers_for(
            message, answers, by_text or {}
        ).items():
            entry = {"answer": answer, "confidence": confidence}
            user_id = participant_of(label)
            if user_id is not None:
                body["participants"][user_id] = entry
            else:
                body[label] = entry
        return ModelResponse(parts=[TextPart(json.dumps(body))])

    return FunctionModel(respond, model_name="claude-haiku-5-5")


_CANDIDATE_ID = re.compile(r"\[(\d+)\]")


def scripted_jev(
    answers: Answers, calls: ModelCalls, by_text: AnswersByText | None = None
) -> FunctionModel:
    """Jev answering each schema field: the ad field and ``candidate_<n>``s."""

    def respond(messages, info: AgentInfo) -> ModelResponse:
        material = messages[-1].parts[-1].content
        ad = material.split("AD:\n", 1)[-1].split("\n\nCANDIDATES", 1)[0]
        chosen = _answers_for(ad, answers, by_text or {})
        tool = info.output_tools[0]
        schema = tool.parameters_json_schema
        args: dict[str, bool] = {}
        confidence: dict[str, float] = {}
        for name, prop in (schema.get("properties") or {}).items():
            if name == AD_FIELD:
                label = EVENT_PROMOTION
            else:
                found = _CANDIDATE_ID.search(prop.get("description") or "")
                label = participant_label(found.group(1)) if found else name
            answer, conf = chosen.get(label, (False, 0.0))
            args[name] = answer
            confidence[name] = conf
        calls.jev.append(
            {
                "fields": sorted(args),
                "instructions": messages[-1].instructions,
                "material": messages[-1].parts[-1].content,
            }
        )
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool.name, args=args)],
            provider_details={"confidence": confidence},
        )

    return FunctionModel(respond, model_name="jev-1.13.0")


@dataclass
class ReplayReads:
    """The reads, answered from the case."""

    staff: Mapping[str, str] = field(default_factory=dict)
    histories: Mapping[str, AuthorHistory] = field(default_factory=dict)
    #: channel id -> the window a sweep there reads, and its authors.
    windows: Mapping[
        str, tuple[list[TranscriptMessage], dict[str, AuthorFacts]] | None
    ] = field(default_factory=dict)
    window_reads: int = 0
    #: The replayed clock: a history read sees nothing posted after it.
    now: float | None = None

    async def staff_refusal(self, guild_id, user_id, *, is_bot, extra_role_ids):
        if is_bot:
            return "a bot"
        return self.staff.get(str(user_id))

    async def author_history(self, guild_id, user_id, before: datetime):
        return self.histories.get(
            str(user_id),
            AuthorHistory(is_first_message=False, days_since_last_message=1),
        )

    async def channel_window(
        self, guild_id, channel_id, since, limit, extra_role_ids=()
    ):
        self.window_reads += 1
        window = self.windows.get(str(channel_id))
        if window is None:
            return None
        messages, authors = window
        seen = [
            m
            for m in messages
            if m.timestamp >= since
            and (self.now is None or m.timestamp.timestamp() <= self.now)
        ]
        return seen[-limit:], dict(authors)


@dataclass
class ReplayCase:
    name: str
    policy: GuildPolicy
    messages: list[MessageFacts]
    answers: Answers = field(default_factory=dict)
    answers_by_text: AnswersByText = field(default_factory=dict)
    reads: ReplayReads = field(default_factory=ReplayReads)
    #: Swap in a model that fails (a timeout, an error) for the case.
    haiku: Callable[..., object] = scripted_haiku
    jev: Callable[..., object] = scripted_jev
    deadline: float = 10.0


@dataclass
class ReplayResult:
    evaluations: list[Evaluation]
    calls: ModelCalls

    def by_message(self, message_id: str) -> Evaluation:
        return next(e for e in self.evaluations if e.facts.message_id == message_id)


async def replay(case: ReplayCase) -> ReplayResult:
    """Run the case's messages, in order, through one fresh coordinator."""
    calls = ModelCalls()
    # The budget runs on the replayed messages' clock, not the wall clock.
    now = [case.messages[0].posted_at if case.messages else 0.0]
    classifier = ModerationClassifier(
        budget=ClassifierBudget(per_guild_day=10_000),
        haiku_model=case.haiku(case.answers, calls, case.answers_by_text),
        jev_model=case.jev(case.answers, calls, case.answers_by_text),
        deadline=case.deadline,
        clock=lambda: now[0],
    )
    coordinator = Coordinator(case.reads, classifier)
    evaluations = []
    for facts in case.messages:
        now[0] = facts.posted_at
        if isinstance(case.reads, ReplayReads):
            case.reads.now = facts.posted_at
        evaluations.append(await coordinator.evaluate(facts, case.policy))
    return ReplayResult(evaluations=evaluations, calls=calls)
