"""TLDR agent for channel message summarization using ChainOfThought pattern."""

from __future__ import annotations

import html
import logging

import dspy

from smarter_dev.bot.agents.base import BaseAgent
from smarter_dev.bot.agents.models import DiscordMessage
from smarter_dev.llm_config import get_llm_model
from smarter_dev.llm_config import get_model_info

logger = logging.getLogger(__name__)

# Configure LLM model from environment
TLDR_AGENT_LM = get_llm_model("default")

# Log which model is being used
model_info = get_model_info("default")
logger.info(f"TLDRAgent using LLM model: {model_info['model_name']} (provider: {model_info['provider']})")


class TLDRAgentSignature(dspy.Signature):
    """You are a helpful Discord bot that creates well-organized, readable summaries of channel conversations.

    ## TASK
    Analyze the conversation type and adapt your summary style accordingly. Create a clear, scannable summary that's easy to read on Discord, keeping in mind that there was likely conversation before these messages.

    ## MESSAGE FORMAT
    Messages may include reply context with this structure:
    ```xml
    <message>
        <timestamp>01/15 12:30</timestamp>
        <author>Username</author>
        <replying-to>
            <replied-author>OriginalAuthor</replied-author>
            <replied-content>Original message content</replied-content>
        </replying-to>
        <content>The user's response to the original message</content>
    </message>
    ```

    When a message has `<replying-to>`, it means the user is responding to a previous message. Use this context to understand conversation flow and relationships between messages.

    ## ADAPTIVE RESPONSE STRATEGY
    **For Quick Back-and-Forth Chat** (short messages, casual talk):
    - Keep it brief and to the point, don't lose juicy details
    - 2-4 sentences max
    - Highlight any decisions or conclusions

    **For Detailed Discussions** (long messages, complex topics):
    - Structured breakdown with key points
    - Include important details and decisions
    - Use bullet points or short paragraphs for readability
    - 4-6 sentences organized clearly

    **For Mixed Conversations**:
    - Balance detail with brevity
    - Focus on the most important developments
    - 3-5 sentences with clear structure

    ## FORMATTING REQUIREMENTS
    - Start with "**Channel Summary**"
    - **Use line breaks** to separate different topics or phases
    - **Bold key terms** or decisions for scanning
    - **Avoid walls of text** - break into digestible chunks
    - Include relevant usernames naturally

    ## EXAMPLE OUTPUTS

    **Quick Chat Example:**
    **Channel Summary**
    Users discussed their favorite pizza toppings, with **pineapple** being surprisingly popular. Sarah shared a local restaurant recommendation that got several positive reactions.

    Most people agreed to try the new place for the next meetup.

    **Detailed Discussion Example:**
    **Channel Summary**
    **Event Planning Issue**: Alice reported conflicts with the venue booking due to scheduling changes.

    **Root Cause**: Bob identified that the original date conflicts with a major community event.

    **Solution**: Team decided to move the meetup to the following weekend and update all announcements.

    Alice successfully rescheduled and confirmed the new venue is available.
    """

    messages: str = dspy.InputField(description="Discord messages to summarize, formatted as structured data")
    summary: str = dspy.OutputField(description="Comprehensive and detailed summary of the conversation")


def estimate_message_tokens(messages: list[DiscordMessage]) -> int:
    """Estimate total token count for a list of messages."""
    total_chars = sum(len(msg.content) + len(msg.author) + 50 for msg in messages)  # +50 for formatting
    return total_chars // 4  # Rough estimation of 4 chars per token


class TLDRAgent(BaseAgent):
    """Discord bot TLDR agent for conversation summarization."""

    def __init__(self):
        """Initialize the TLDR agent."""
        super().__init__()
        self._agent = dspy.ChainOfThought(TLDRAgentSignature)

    def truncate_message_content(self, content: str, max_chars: int = 500) -> str:
        """Truncate message content while preserving readability."""
        if len(content) <= max_chars:
            return content

        # Try to truncate at word boundaries
        truncated = content[:max_chars]
        last_space = truncated.rfind(" ")
        if last_space > max_chars * 0.8:  # If we find a space in the last 20%
            truncated = truncated[:last_space]

        return truncated + "..."

    def prepare_messages_for_context(
        self,
        messages: list[DiscordMessage],
        max_tokens: int = 15000
    ) -> tuple[str, int]:
        """Prepare messages for LLM context with progressive truncation.

        Args:
            messages: List of Discord messages to process
            max_tokens: Maximum tokens to use (leaving room for response)

        Returns:
            Tuple[str, int]: Formatted message string and actual message count used
        """
        if not messages:
            return "<no-messages>No messages to summarize</no-messages>", 0

        # Start with full messages and progressively reduce if needed
        current_messages = messages.copy()

        while current_messages:
            # Format current set of messages with enhanced context
            formatted_lines = []

            # Add channel context info at the top for TLDR
            first_msg = current_messages[0] if current_messages else None
            if first_msg and (first_msg.channel_name or first_msg.channel_description):
                channel_context_parts = []
                if first_msg.channel_name:
                    channel_context_parts.append(f"<channel-name>{html.escape(first_msg.channel_name)}</channel-name>")
                if first_msg.channel_description:
                    channel_context_parts.append(f"<channel-description>{html.escape(first_msg.channel_description)}</channel-description>")
                if first_msg.channel_type:
                    channel_context_parts.append(f"<channel-type>{html.escape(first_msg.channel_type)}</channel-type>")

                if channel_context_parts:
                    formatted_lines.append(f"<channel-context>\n{chr(10).join(channel_context_parts)}\n</channel-context>")

            for msg in current_messages:
                sent_str = msg.timestamp.isoformat()

                # Truncate very long messages
                content = self.truncate_message_content(msg.content, 500)

                # Build message structure with new fields
                message_parts = [
                    f"<sent>{html.escape(sent_str)}</sent>",
                    f"<author>{html.escape(msg.author)}</author>"
                ]

                # Add role information for users with roles
                if msg.author_roles:
                    roles_str = ", ".join(msg.author_roles)
                    message_parts.append(f"<author-roles>{html.escape(roles_str)}</author-roles>")

                # Add OP indicator for forum threads
                if msg.is_original_poster:
                    message_parts.append("<is-op>true</is-op>")

                # Add reply context if present
                if msg.replied_to_author and msg.replied_to_content:
                    message_parts.append(
                        f"<replying-to>"
                        f"<replied-author>{html.escape(msg.replied_to_author)}</replied-author>"
                        f"<replied-content>{html.escape(msg.replied_to_content)}</replied-content>"
                        f"</replying-to>"
                    )

                # Add message content
                message_parts.append(f"<content>{html.escape(content)}</content>")

                # Combine into message element
                message_xml = (
                    f"<message>"
                    f"{chr(10).join(message_parts)}"
                    f"</message>"
                )
                formatted_lines.append(message_xml)

            formatted_text = f"<conversation>\n{chr(10).join(formatted_lines)}\n</conversation>"

            # Check if this fits within token limit
            estimated_tokens = self._estimate_tokens(formatted_text)

            if estimated_tokens <= max_tokens or len(current_messages) <= 3:
                # Either it fits, or we're down to minimum messages
                return formatted_text, len(current_messages)

            # Remove some messages and try again (remove from the oldest/beginning)
            reduction = max(1, len(current_messages) // 4)  # Remove 25% each iteration
            current_messages = current_messages[-len(current_messages) + reduction:]

        return "<no-messages>No messages could be processed</no-messages>", 0

    def generate_summary(
        self,
        messages: list[DiscordMessage],
        max_context_tokens: int = 15000
    ) -> tuple[str, int, int]:
        """Generate a TLDR summary of Discord messages (synchronous version).

        Args:
            messages: List of Discord messages to summarize
            max_context_tokens: Maximum tokens to use for context

        Returns:
            Tuple[str, int, int]: Summary text, token usage, messages actually summarized
        """
        # Prepare messages with progressive truncation
        formatted_messages, messages_used = self.prepare_messages_for_context(
            messages, max_context_tokens
        )

        if messages_used == 0:
            return ("**Channel Summary**\nNo messages found to summarize. The channel might be empty or contain only bot messages.\n\n*(Summarized 0 messages)*", 0, 0)

        try:
            # Generate summary with proper LM context
            with dspy.context(lm=TLDR_AGENT_LM, track_usage=True):
                result = self._agent(messages=formatted_messages)

            # Get token usage
            tokens_used = self._extract_token_usage(result)

            # Final fallback: estimate tokens from text length if all methods fail
            if tokens_used == 0:
                input_text = formatted_messages
                output_text = result.summary
                tokens_used = (len(input_text) + len(output_text)) // 4
                logger.warning(f"TLDR DEBUG: Fallback estimation - {tokens_used} tokens from text length")

            # Inject the actual message count into the summary
            summary_with_count = f"{result.summary}\n\n*(Summarized {messages_used} messages)*"

            return summary_with_count, tokens_used, messages_used

        except Exception as e:
            logger.error(f"Error generating TLDR summary: {e}")
            return ("**Channel Summary**\nSorry, there was too much content to summarize. Try using a smaller message count or wait a moment before trying again.\n\n*(Unable to process messages)*", 0, 0)

    async def generate_summary_async(
        self,
        messages: list[DiscordMessage],
        max_context_tokens: int = 15000
    ) -> tuple[str, int, int]:
        """Async version of generate_summary to avoid blocking the event loop.

        Args:
            messages: List of Discord messages to summarize
            max_context_tokens: Maximum tokens to use for context

        Returns:
            Tuple[str, int, int]: Summary text, token usage, messages actually summarized
        """
        # Prepare messages with progressive truncation
        formatted_messages, messages_used = self.prepare_messages_for_context(
            messages, max_context_tokens
        )

        if messages_used == 0:
            return ("**Channel Summary**\nNo messages found to summarize. The channel might be empty or contain only bot messages.\n\n*(Summarized 0 messages)*", 0, 0)

        try:
            # Generate summary using async agent with proper LM context
            with dspy.context(lm=TLDR_AGENT_LM, track_usage=True):
                async_agent = dspy.asyncify(self._agent)
                result = await async_agent(messages=formatted_messages)

            # Get token usage
            tokens_used = self._extract_token_usage(result)

            # Final fallback: estimate tokens from text length if all methods fail
            if tokens_used == 0:
                input_text = formatted_messages
                output_text = result.summary
                tokens_used = (len(input_text) + len(output_text)) // 4
                logger.warning(f"TLDR DEBUG: Fallback estimation - {tokens_used} tokens from text length")

            # Inject the actual message count into the summary
            summary_with_count = f"{result.summary}\n\n*(Summarized {messages_used} messages)*"

            return summary_with_count, tokens_used, messages_used

        except Exception as e:
            logger.error(f"Error generating async TLDR summary: {e}")
            return ("**Channel Summary**\nSorry, there was too much content to summarize. Try using a smaller message count or wait a moment before trying again.\n\n*(Unable to process messages)*", 0, 0)


# Global TLDR agent instance
tldr_agent = TLDRAgent()
