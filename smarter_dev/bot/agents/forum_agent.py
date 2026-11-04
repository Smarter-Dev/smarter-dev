"""Forum monitoring agent for AI-powered forum post evaluation and topic classification."""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path

from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import Agent

from smarter_dev.bot.agents.base import BaseAgent
from smarter_dev.bot.agents.utility_model import build_utility_agent
from smarter_dev.bot.agents.utility_model import render_inputs
from smarter_dev.bot.agents.utility_model import tokens_used
from smarter_dev.bot.agents.utility_model import utility_metered

logger = logging.getLogger(__name__)

FORUM_MONITOR_INSTRUCTIONS = (
    Path(__file__).parent / "prompts" / "forum_agent_forum_monitor_instructions.md"
).read_text(encoding="utf-8")

FORUM_TOPIC_CLASSIFICATION_INSTRUCTIONS = (
    Path(__file__).parent / "prompts" / "forum_agent_forum_topic_classification_instructions.md"
).read_text(encoding="utf-8")

FORUM_COMBINED_INSTRUCTIONS = (
    Path(__file__).parent / "prompts" / "forum_agent_forum_combined_instructions.md"
).read_text(encoding="utf-8")

_DECISION = "Explanation of whether and why to respond"
_CONFIDENCE = (
    "Message send confidence score (0.0-1.0) - higher values mean more confident "
    "you should send a response"
)
_RESPONSE = "Generated response content (empty if not responding)"
_MATCHING_TOPICS = "List of topic names that match this post (empty list if no matches)"


class ForumEvaluation(BaseModel):
    decision: str = Field(description=_DECISION)
    confidence: float = Field(description=_CONFIDENCE)
    response: str = Field(description=_RESPONSE)


class ForumTopicClassification(BaseModel):
    matching_topics: list[str] = Field(description=_MATCHING_TOPICS)


class ForumCombinedEvaluation(ForumEvaluation):
    matching_topics: list[str] = Field(description=_MATCHING_TOPICS)


_monitor_agent: Agent[None, ForumEvaluation] | None = None
_topic_classifier_agent: Agent[None, ForumTopicClassification] | None = None
_combined_agent: Agent[None, ForumCombinedEvaluation] | None = None


def get_forum_monitor_agent() -> Agent[None, ForumEvaluation]:
    """Return the singleton response-evaluation agent, building it on first use."""
    global _monitor_agent
    if _monitor_agent is None:
        _monitor_agent = build_utility_agent(ForumEvaluation, FORUM_MONITOR_INSTRUCTIONS)
    return _monitor_agent


def get_forum_topic_classifier_agent() -> Agent[None, ForumTopicClassification]:
    """Return the singleton topic-classification agent, building it on first use."""
    global _topic_classifier_agent
    if _topic_classifier_agent is None:
        _topic_classifier_agent = build_utility_agent(
            ForumTopicClassification, FORUM_TOPIC_CLASSIFICATION_INSTRUCTIONS
        )
    return _topic_classifier_agent


def get_forum_combined_agent() -> Agent[None, ForumCombinedEvaluation]:
    """Return the singleton evaluate-and-classify agent, building it on first use."""
    global _combined_agent
    if _combined_agent is None:
        _combined_agent = build_utility_agent(
            ForumCombinedEvaluation, FORUM_COMBINED_INSTRUCTIONS
        )
    return _combined_agent


def _bounded(confidence: float) -> float:
    return max(0.0, min(1.0, float(confidence)))


def _available(matching_topics: list[str], available_topics: list[str]) -> list[str]:
    """Keep only the topics the forum actually offers, in the model's order."""
    return [topic for topic in matching_topics if topic in available_topics]


class ForumMonitorAgent(BaseAgent):
    """Discord forum monitoring agent for post evaluation and response generation."""

    def _format_post_context(
        self,
        post_title: str,
        post_content: str,
        author_display_name: str,
        post_tags: list[str] | None = None,
        attachment_names: list[str] | None = None
    ) -> str:
        """Format post data into XML context for the AI."""
        post_tags = post_tags or []
        attachment_names = attachment_names or []

        context_parts = [
            "<post>",
            f"<title>{html.escape(post_title)}</title>",
            f"<author>{html.escape(author_display_name)}</author>",
            f"<content>{html.escape(post_content)}</content>",
        ]

        if post_tags:
            tags_str = ", ".join(html.escape(tag) for tag in post_tags)
            context_parts.append(f"<tags>{html.escape(tags_str)}</tags>")

        if attachment_names:
            attachments_str = ", ".join(html.escape(name) for name in attachment_names)
            context_parts.append(f"<attachments>{html.escape(attachments_str)}</attachments>")

        context_parts.append("</post>")

        return "\n".join(context_parts)

    async def evaluate_post(
        self,
        system_prompt: str,
        post_title: str,
        post_content: str,
        author_display_name: str,
        post_tags: list[str] | None = None,
        attachment_names: list[str] | None = None,
        *,
        guild_id: str | None = None,
        channel_id: str | None = None,
    ) -> tuple[str, float, str, int]:
        """Evaluate a forum post and generate response if warranted.

        Args:
            system_prompt: Agent's specific role and criteria
            post_title: Title of the forum post
            post_content: Content of the forum post
            author_display_name: Display name of the post author
            post_tags: List of tags on the post
            attachment_names: List of attachment filenames
            guild_id, channel_id: The post's guild and forum, for the usage ledger

        Returns:
            Tuple[str, float, str, int]: Decision reason, confidence score, response content, tokens used
        """
        post_context = self._format_post_context(
            post_title, post_content, author_display_name, post_tags, attachment_names
        )

        agent = get_forum_monitor_agent()
        async with utility_metered(
            "forum_agent", guild_id=guild_id, channel_id=channel_id
        ) as meter:
            result = await agent.run(
                render_inputs(system_prompt=system_prompt, post_context=post_context),
                model=meter.wrap(agent.model),
            )
        evaluation = result.output

        return (
            evaluation.decision,
            _bounded(evaluation.confidence),
            evaluation.response.strip(),
            tokens_used(result),
        )

    async def classify_topics_only(
        self,
        available_topics: list[str],
        post_title: str,
        post_content: str,
        author_display_name: str,
        post_tags: list[str] | None = None,
        attachment_names: list[str] | None = None,
        *,
        guild_id: str | None = None,
        channel_id: str | None = None,
    ) -> tuple[list[str], int]:
        """Classify a forum post into notification topics only (no response generation).

        Args:
            available_topics: List of topic names available for this forum
            post_title: Title of the forum post
            post_content: Content of the forum post
            author_display_name: Display name of the post author
            post_tags: List of tags on the post
            attachment_names: List of attachment filenames
            guild_id, channel_id: The post's guild and forum, for the usage ledger

        Returns:
            Tuple[List[str], int]: Matching topic names, tokens used
        """
        post_context = self._format_post_context(
            post_title, post_content, author_display_name, post_tags, attachment_names
        )

        agent = get_forum_topic_classifier_agent()
        async with utility_metered(
            "forum_agent", guild_id=guild_id, channel_id=channel_id
        ) as meter:
            result = await agent.run(
                render_inputs(
                    available_topics=json.dumps(available_topics),
                    post_context=post_context,
                ),
                model=meter.wrap(agent.model),
            )

        return (
            _available(result.output.matching_topics, available_topics),
            tokens_used(result),
        )

    async def evaluate_post_combined(
        self,
        system_prompt: str,
        available_topics: list[str],
        post_title: str,
        post_content: str,
        author_display_name: str,
        post_tags: list[str] | None = None,
        attachment_names: list[str] | None = None,
        *,
        guild_id: str | None = None,
        channel_id: str | None = None,
    ) -> tuple[str, float, str, list[str], int]:
        """Evaluate a forum post for both response generation AND topic classification.

        Args:
            system_prompt: Agent's specific role and criteria
            available_topics: List of topic names available for this forum
            post_title: Title of the forum post
            post_content: Content of the forum post
            author_display_name: Display name of the post author
            post_tags: List of tags on the post
            attachment_names: List of attachment filenames
            guild_id, channel_id: The post's guild and forum, for the usage ledger

        Returns:
            Tuple[str, float, str, List[str], int]: Decision reason, confidence score, response content, matching topics, tokens used
        """
        post_context = self._format_post_context(
            post_title, post_content, author_display_name, post_tags, attachment_names
        )

        agent = get_forum_combined_agent()
        async with utility_metered(
            "forum_agent", guild_id=guild_id, channel_id=channel_id
        ) as meter:
            result = await agent.run(
                render_inputs(
                    system_prompt=system_prompt,
                    available_topics=json.dumps(available_topics),
                    post_context=post_context,
                ),
                model=meter.wrap(agent.model),
            )
        evaluation = result.output

        return (
            evaluation.decision,
            _bounded(evaluation.confidence),
            evaluation.response.strip(),
            _available(evaluation.matching_topics, available_topics),
            tokens_used(result),
        )
