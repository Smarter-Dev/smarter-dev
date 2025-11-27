"""Forum monitoring agent for AI-powered forum post evaluation and topic classification."""

from __future__ import annotations

import html
import logging

import dspy

from smarter_dev.bot.agents.base import BaseAgent
from smarter_dev.llm_config import get_llm_model
from smarter_dev.llm_config import get_model_info

logger = logging.getLogger(__name__)

# Configure LLM model from environment
FORUM_AGENT_LM = get_llm_model("default")

# Log which model is being used
model_info = get_model_info("default")
logger.info(f"ForumMonitorAgent using LLM model: {model_info['model_name']} (provider: {model_info['provider']})")


class ForumMonitorSignature(dspy.Signature):
    """You are an AI agent that monitors Discord forum posts and decides whether to respond.

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
    """

    system_prompt: str = dspy.InputField(description="Your specific role and response criteria")
    post_context: str = dspy.InputField(description="Complete forum post information including title, content, author, tags, and attachments")
    decision: str = dspy.OutputField(description="Explanation of whether and why to respond")
    confidence: float = dspy.OutputField(description="Message send confidence score (0.0-1.0) - higher values mean more confident you should send a response")
    response: str = dspy.OutputField(description="Generated response content (empty if not responding)")


class ForumTopicClassificationSignature(dspy.Signature):
    """You are an AI topic classifier that categorizes forum posts into predefined notification topics.

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
    """

    available_topics: list = dspy.InputField(description="List of available topic names for this forum")
    post_context: str = dspy.InputField(description="Complete forum post information including title, content, author, tags, and attachments")
    matching_topics: list = dspy.OutputField(description="List of topic names that match this post (empty list if no matches)")


class ForumCombinedSignature(dspy.Signature):
    """You are an AI agent that both evaluates forum posts for responses AND classifies them into notification topics.

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
    """

    system_prompt: str = dspy.InputField(description="Your specific role and response criteria")
    available_topics: list = dspy.InputField(description="List of available topic names for this forum")
    post_context: str = dspy.InputField(description="Complete forum post information including title, content, author, tags, and attachments")
    decision: str = dspy.OutputField(description="Explanation of whether and why to respond")
    confidence: float = dspy.OutputField(description="Message send confidence score (0.0-1.0) - higher values mean more confident you should send a response")
    response: str = dspy.OutputField(description="Generated response content (empty if not responding)")
    matching_topics: list = dspy.OutputField(description="List of topic names that match this post (empty list if no matches)")


class ForumMonitorAgent(BaseAgent):
    """Discord forum monitoring agent for post evaluation and response generation."""

    def __init__(self):
        """Initialize the forum monitoring agent."""
        super().__init__()
        self._agent = dspy.ChainOfThought(ForumMonitorSignature)
        self._topic_classifier = dspy.ChainOfThought(ForumTopicClassificationSignature)
        self._combined_agent = dspy.ChainOfThought(ForumCombinedSignature)

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

        # Generate evaluation and response using async agent with proper LM context
        with dspy.context(lm=FORUM_AGENT_LM, track_usage=True):
            async_agent = dspy.asyncify(self._agent)
            result = await async_agent(
                system_prompt=system_prompt,
                post_context=post_context
            )

        # Get token usage
        tokens_used = self._extract_token_usage(result)

        # Final fallback: estimate tokens from text length
        if tokens_used == 0:
            input_text = f"{system_prompt}\n{post_context}"
            output_text = result.decision + result.response
            tokens_used = (len(input_text) + len(output_text)) // 4
            logger.debug(f"FORUM DEBUG: Fallback estimation - {tokens_used} tokens from text length")

        # Ensure confidence is bounded between 0.0 and 1.0
        confidence = max(0.0, min(1.0, float(result.confidence)))

        return result.decision, confidence, result.response, tokens_used

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

        # Generate topic classification using async agent with proper LM context
        with dspy.context(lm=FORUM_AGENT_LM, track_usage=True):
            async_classifier = dspy.asyncify(self._topic_classifier)
            result = await async_classifier(
                available_topics=available_topics,
                post_context=post_context
            )

        # Extract token usage
        tokens_used = self._extract_token_usage(result)

        # Ensure matching_topics is a list and filter out any empty/invalid topics
        matching_topics = result.matching_topics or []
        if isinstance(matching_topics, str):
            # Handle case where AI returns a comma-separated string instead of list
            matching_topics = [topic.strip() for topic in matching_topics.split(",") if topic.strip()]

        # Filter to only include topics that are actually available
        valid_topics = [topic for topic in matching_topics if topic in available_topics]

        return valid_topics, tokens_used

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

        # Generate combined evaluation and topic classification using async agent with proper LM context
        with dspy.context(lm=FORUM_AGENT_LM, track_usage=True):
            async_combined = dspy.asyncify(self._combined_agent)
            result = await async_combined(
                system_prompt=system_prompt,
                available_topics=available_topics,
                post_context=post_context
            )

        # Extract token usage
        tokens_used = self._extract_token_usage(result)

        # Ensure confidence is bounded between 0.0 and 1.0
        confidence = max(0.0, min(1.0, float(result.confidence)))

        # Ensure matching_topics is a list and filter out any empty/invalid topics
        matching_topics = result.matching_topics or []
        if isinstance(matching_topics, str):
            # Handle case where AI returns a comma-separated string instead of list
            matching_topics = [topic.strip() for topic in matching_topics.split(",") if topic.strip()]

        # Filter to only include topics that are actually available
        valid_topics = [topic for topic in matching_topics if topic in available_topics]

        return result.decision, confidence, result.response, valid_topics, tokens_used


# Global forum monitor agent instance
forum_monitor_agent = ForumMonitorAgent()
