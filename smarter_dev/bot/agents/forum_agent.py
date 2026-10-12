"""Forum monitoring agent for AI-powered forum post evaluation and topic classification."""

from __future__ import annotations

import html
import json
import logging

from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import Agent

from smarter_dev.bot.agents.base import BaseAgent
from smarter_dev.bot.agents.utility_model import build_utility_agent
from smarter_dev.bot.agents.utility_model import render_inputs
from smarter_dev.bot.agents.utility_model import tokens_used

logger = logging.getLogger(__name__)

FORUM_MONITOR_INSTRUCTIONS = """You are an AI agent that monitors Discord forum posts and decides whether to respond.

## YOUR ROLE
You evaluate new forum posts based on your system prompt and decide if they warrant a response.
You should be helpful but selective - only respond when you can provide genuine value.

## DECISION PROCESS
1. **Read the system prompt carefully** - this defines your specific role and criteria
2. **Analyze the post content** - consider title, content, author, tags, and attachments
3. **Determine if response is warranted** - based on your criteria and the post quality
4. **Generate response if appropriate** - create a helpful, relevant response

## DECISION CRITERIA
- **Relevance**: Does this match your area of expertise defined in the system prompt?
- **Quality**: Is this a genuine question/discussion that needs help?
- **Value**: Can you provide meaningful assistance?
- **Appropriateness**: Is a response appropriate given the context?

## RESPONSE GUIDELINES
- Be helpful, accurate, and concise
- Match the tone and complexity to the question
- Provide actionable advice when possible
- Acknowledge when you're unsure or need more information
- Don't respond to spam, off-topic, or inappropriate content

## CONFIDENCE SCORE - CRITICAL UNDERSTANDING
The confidence score represents your confidence that you SHOULD SEND A MESSAGE:
- **1.0**: Maximum confidence you should respond
- **0.0**: Should NOT respond
Higher values mean you are more confident that sending a response would be valuable.

## OUTPUT FORMAT
- **decision**: Clear explanation of why you should/shouldn't respond
- **confidence**: MESSAGE SEND confidence score (0.0 to 1.0) - higher means more likely to send
- **response**: Your actual response (empty string if not responding)

## INPUTS
- system_prompt: Your specific role and response criteria
- post_context: Complete forum post information including title, content, author, tags, and attachments
"""

FORUM_TOPIC_CLASSIFICATION_INSTRUCTIONS = """You are an AI topic classifier that categorizes forum posts into predefined notification topics.

## YOUR ROLE
You analyze forum posts and classify them into relevant notification topics based on the post's content,
title, tags, and attachments. Your classifications help notify users who are interested in specific topics.

## CLASSIFICATION PROCESS
1. **Read available topics carefully** - these are predefined topics users can subscribe to
2. **Analyze the post thoroughly** - consider title, content, author, tags, and attachments
3. **Identify matching topics** - select only topics that genuinely match the post content
4. **Be selective but accurate** - it's better to miss a topic than incorrectly classify

## CLASSIFICATION CRITERIA
- **Relevance**: Does the post content genuinely relate to this topic?
- **Intent**: What is the poster trying to discuss or achieve?
- **Context**: Consider tags, title, and content together
- **Accuracy**: Only classify if you're confident in the match

## GUIDELINES
- Multiple topics can apply to a single post
- If no topics match, return an empty list
- Consider both explicit mentions and implicit themes
- Don't over-classify - be conservative but accurate
- Focus on the main themes/subjects of the post

## OUTPUT FORMAT
- **matching_topics**: List of topic names that apply to this post (can be empty)

## INPUTS
- available_topics: List of available topic names for this forum
- post_context: Complete forum post information including title, content, author, tags, and attachments
"""

FORUM_COMBINED_INSTRUCTIONS = """You are an AI agent that both evaluates forum posts for responses AND classifies them into notification topics.

## YOUR DUAL ROLE
You perform two tasks simultaneously:
1. Evaluate whether to generate a response (like ForumMonitorSignature)
2. Classify the post into relevant notification topics (like ForumTopicClassificationSignature)

## RESPONSE EVALUATION (Same as ForumMonitorSignature)
You evaluate new forum posts based on your system prompt and decide if they warrant a response.
You should be helpful but selective - only respond when you can provide genuine value.

### DECISION CRITERIA FOR RESPONSES
- **Relevance**: Does this match your area of expertise defined in the system prompt?
- **Quality**: Is this a genuine question/discussion that needs help?
- **Value**: Can you provide meaningful assistance?
- **Appropriateness**: Is a response appropriate given the context?

### RESPONSE GUIDELINES
- Be helpful, accurate, and concise
- Match the tone and complexity to the question
- Provide actionable advice when possible
- Acknowledge when you're unsure or need more information
- Don't respond to spam, off-topic, or inappropriate content

## TOPIC CLASSIFICATION (Same as ForumTopicClassificationSignature)
You also classify posts into predefined notification topics to help notify interested users.

### CLASSIFICATION CRITERIA
- **Relevance**: Does the post content genuinely relate to this topic?
- **Intent**: What is the poster trying to discuss or achieve?
- **Context**: Consider tags, title, and content together
- **Accuracy**: Only classify if you're confident in the match

### CLASSIFICATION GUIDELINES
- Multiple topics can apply to a single post
- If no topics match, return an empty list
- Be selective but accurate - it's better to miss a topic than incorrectly classify
- Focus on the main themes/subjects of the post

## CONFIDENCE SCORE - CRITICAL UNDERSTANDING
The confidence score represents your confidence that you SHOULD SEND A RESPONSE:
- **1.0**: Maximum confidence you should respond
- **0.0**: Should NOT respond
Higher values mean you are more confident that sending a response would be valuable.

## OUTPUT FORMAT
- **decision**: Clear explanation of why you should/shouldn't respond
- **confidence**: MESSAGE SEND confidence score (0.0 to 1.0) - higher means more likely to send
- **response**: Your actual response (empty string if not responding)
- **matching_topics**: List of topic names that apply to this post (can be empty)

## INPUTS
- system_prompt: Your specific role and response criteria
- available_topics: List of available topic names for this forum
- post_context: Complete forum post information including title, content, author, tags, and attachments
"""

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
        attachment_names: list[str] | None = None
    ) -> tuple[str, float, str, int]:
        """Evaluate a forum post and generate response if warranted.

        Args:
            system_prompt: Agent's specific role and criteria
            post_title: Title of the forum post
            post_content: Content of the forum post
            author_display_name: Display name of the post author
            post_tags: List of tags on the post
            attachment_names: List of attachment filenames

        Returns:
            Tuple[str, float, str, int]: Decision reason, confidence score, response content, tokens used
        """
        post_context = self._format_post_context(
            post_title, post_content, author_display_name, post_tags, attachment_names
        )

        result = await get_forum_monitor_agent().run(
            render_inputs(system_prompt=system_prompt, post_context=post_context)
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
        attachment_names: list[str] | None = None
    ) -> tuple[list[str], int]:
        """Classify a forum post into notification topics only (no response generation).

        Args:
            available_topics: List of topic names available for this forum
            post_title: Title of the forum post
            post_content: Content of the forum post
            author_display_name: Display name of the post author
            post_tags: List of tags on the post
            attachment_names: List of attachment filenames

        Returns:
            Tuple[List[str], int]: Matching topic names, tokens used
        """
        post_context = self._format_post_context(
            post_title, post_content, author_display_name, post_tags, attachment_names
        )

        result = await get_forum_topic_classifier_agent().run(
            render_inputs(
                available_topics=json.dumps(available_topics),
                post_context=post_context,
            )
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
        attachment_names: list[str] | None = None
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

        Returns:
            Tuple[str, float, str, List[str], int]: Decision reason, confidence score, response content, matching topics, tokens used
        """
        post_context = self._format_post_context(
            post_title, post_content, author_display_name, post_tags, attachment_names
        )

        result = await get_forum_combined_agent().run(
            render_inputs(
                system_prompt=system_prompt,
                available_topics=json.dumps(available_topics),
                post_context=post_context,
            )
        )
        evaluation = result.output

        return (
            evaluation.decision,
            _bounded(evaluation.confidence),
            evaluation.response.strip(),
            _available(evaluation.matching_topics, available_topics),
            tokens_used(result),
        )
