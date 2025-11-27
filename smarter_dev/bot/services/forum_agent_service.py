"""Forum Agent Service for managing AI-driven forum post monitoring and responses."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from smarter_dev.bot.agents.forum_agent import ForumMonitorAgent
from smarter_dev.bot.services.base import BaseService
from smarter_dev.bot.services.exceptions import APIError, ServiceError, ValidationError
from smarter_dev.bot.services.models import ServiceHealth

logger = logging.getLogger(__name__)


class ForumAgentService(BaseService):
    """Service for managing forum monitoring agents and processing posts."""
    
    def __init__(self, api_client, cache_manager=None):
        super().__init__(api_client, cache_manager, "ForumAgentService")
        self._evaluations_processed = 0
        self._responses_generated = 0
        self._total_tokens_used = 0
        self._evaluation_times = []
    
    async def initialize(self):
        """Initialize the forum agent service."""
        logger.info("Initializing ForumAgentService...")
        self._is_initialized = True
        logger.info("ForumAgentService initialized")
    
    async def load_guild_agents(self, guild_id: str) -> List[Dict[str, Any]]:
        """Load all active forum agents for a guild.
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            List of forum agent configurations
        """
        try:
            response = await self._api_client.get(f"/guilds/{guild_id}/forum-agents")
            
            # Handle error responses
            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)
            
            # Parse successful response
            agents = response.json()
            logger.debug(f"Loaded {len(agents)} forum agents for guild {guild_id}")
            return agents
        except Exception as e:
            logger.error(f"Failed to load forum agents for guild {guild_id}: {e}")
            raise APIError(f"Failed to load forum agents: {e}")
    
    def should_agent_monitor_forum(self, agent: Dict[str, Any], channel_id: str) -> bool:
        """Check if an agent should monitor a specific forum channel.
        
        Args:
            agent: Forum agent configuration
            channel_id: Discord channel ID to check
            
        Returns:
            True if agent should monitor this channel
        """
        if not agent.get('is_active', True):
            return False
        
        monitored_forums = agent.get('monitored_forums', [])
        return channel_id in monitored_forums
    
    async def evaluate_post(
        self, 
        agent: Dict[str, Any], 
        post: Any
    ) -> tuple[str, float, str, int]:
        """Evaluate a forum post using an agent's AI.
        
        Args:
            agent: Forum agent configuration
            post: Forum post object with title, content, author, tags, attachments
            
        Returns:
            tuple[str, float, str, int]: Decision reason, confidence score, response content, tokens used
        """
        start_time = datetime.now(timezone.utc)
        
        try:
            # Create the AI agent
            ai_agent = ForumMonitorAgent()
            
            # Extract post data
            title = getattr(post, 'title', '')
            content = getattr(post, 'content', '')
            author = getattr(post, 'author_display_name', 'Unknown')
            tags = getattr(post, 'tags', [])
            attachments = getattr(post, 'attachments', [])
            
            # Evaluate the post
            decision, confidence, response_content, tokens_used = await ai_agent.evaluate_post(
                system_prompt=agent['system_prompt'],
                post_title=title,
                post_content=content,
                author_display_name=author,
                post_tags=tags,
                attachment_names=attachments
            )
            
            # Update statistics
            self._evaluations_processed += 1
            self._total_tokens_used += tokens_used
            
            evaluation_time = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
            self._evaluation_times.append(evaluation_time)
            
            logger.debug(f"Agent {agent['name']} evaluated post: confidence={confidence:.2f}, tokens={tokens_used}")
            
            return decision, confidence, response_content, tokens_used
            
        except Exception as e:
            logger.error(f"Error evaluating post with agent {agent.get('name', 'Unknown')}: {e}")
            raise ServiceError(f"Post evaluation failed: {e}")
    
    async def record_response(
        self,
        agent: Dict[str, Any],
        post: Any,
        decision_reason: str,
        confidence_score: float,
        response_content: str,
        tokens_used: int,
        response_time_ms: int,
        responded: bool,
        user_mentions: List[str] = None
    ) -> str:
        """Record an agent response in the database.
        
        Args:
            agent: Forum agent configuration
            post: Forum post object
            decision_reason: AI's reasoning for the decision
            confidence_score: Confidence score (0.0-1.0)
            response_content: Generated response content
            tokens_used: Number of tokens consumed
            response_time_ms: Time taken for evaluation
            responded: Whether a response was actually posted
            user_mentions: Optional list of user mentions to include in logging
            
        Returns:
            Response record ID
        """
        try:
            # Include user mentions in the response content for logging if provided
            logged_response_content = response_content
            if user_mentions:
                # Convert Discord mentions to literal @username format for logging
                literal_mentions = []
                for mention in user_mentions:
                    if mention.startswith('<@') and mention.endswith('>'):
                        # Extract user ID from Discord mention format
                        user_id = mention[2:-1]
                        literal_mentions.append(f"@user_{user_id}")  # Will need to be improved with actual usernames
                    else:
                        literal_mentions.append(mention)  # Already in literal format
                
                # Append mentions to response content for database logging
                if literal_mentions:
                    mentions_text = " ".join(literal_mentions)
                    if logged_response_content:
                        logged_response_content += f"\n\n-# {mentions_text}"
                    else:
                        logged_response_content = f"-# {mentions_text}"
            
            response_data = {
                'channel_id': getattr(post, 'channel_id', ''),
                'thread_id': getattr(post, 'thread_id', ''),
                'post_title': getattr(post, 'title', ''),
                'post_content': getattr(post, 'content', ''),
                'author_display_name': getattr(post, 'author_display_name', 'Unknown'),
                'post_tags': getattr(post, 'tags', []),
                'attachments': getattr(post, 'attachments', []),
                'decision_reason': decision_reason,
                'confidence_score': confidence_score,
                'response_content': logged_response_content,  # Include mentions in logged content
                'tokens_used': tokens_used,
                'response_time_ms': response_time_ms,
                'responded': responded
            }
            
            # Debug logging for API data
            logger.debug(f"FORUM API DEBUG - Recording response for agent {agent.get('name', 'Unknown')}")
            logger.debug(f"FORUM API DEBUG - Post title: '{response_data['post_title']}'")
            logger.debug(f"FORUM API DEBUG - Post content: '{response_data['post_content'][:100]}...' ({len(response_data['post_content'])} chars)")
            logger.debug(f"FORUM API DEBUG - Author: '{response_data['author_display_name']}'")
            logger.debug(f"FORUM API DEBUG - Tokens used: {response_data['tokens_used']}")
            logger.debug(f"FORUM API DEBUG - Decision: '{decision_reason[:100]}...'")
            logger.debug(f"FORUM API DEBUG - Confidence: {confidence_score}")
            logger.debug(f"FORUM API DEBUG - Responded: {responded}")
            
            # Data validation before API call
            if not response_data['post_content'] and not response_data['post_title']:
                logger.warning(f"FORUM API WARNING - Both post_content and post_title are empty for agent {agent.get('name', 'Unknown')}")
            
            if response_data['author_display_name'] == 'Unknown':
                logger.warning(f"FORUM API WARNING - Author display name is 'Unknown' for agent {agent.get('name', 'Unknown')}")
            
            if response_data['tokens_used'] == 0:
                logger.warning(f"FORUM API WARNING - Zero tokens used for agent {agent.get('name', 'Unknown')} - this may indicate token extraction issues")
            
            response = await self._api_client.post(
                f"/guilds/{agent['guild_id']}/forum-agents/{agent['id']}/responses",
                json_data=response_data
            )
            
            # Handle error responses
            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)
            
            # Parse successful response
            result = response.json()
            
            if responded:
                self._responses_generated += 1
            
            logger.debug(f"Recorded response for agent {agent['name']}: responded={responded}")
            return result['id']
            
        except Exception as e:
            logger.error(f"Failed to record response for agent {agent.get('name', 'Unknown')}: {e}")
            raise APIError(f"Failed to record response: {e}")
    
    async def check_rate_limit(self, agent: Dict[str, Any]) -> bool:
        """Check if an agent is within its rate limits.
        
        Args:
            agent: Forum agent configuration
            
        Returns:
            True if agent is within rate limits
        """
        try:
            max_responses = agent.get('max_responses_per_hour', 5)
            
            # Get response count in the last hour
            response = await self._api_client.get(
                f"/guilds/{agent['guild_id']}/forum-agents/{agent['id']}/responses/count",
                params={'hours': 1}
            )
            
            # Handle error responses
            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                logger.error(f"Failed to check rate limit: {error_message}")
                # On error, err on the side of caution and allow the request
                return True
            
            # Parse successful response
            response_count_data = response.json()
            current_count = response_count_data.get('count', 0)
            
            logger.debug(f"Agent {agent['name']} rate limit: {current_count}/{max_responses}")
            
            return current_count < max_responses
            
        except Exception as e:
            logger.error(f"Failed to check rate limit for agent {agent.get('name', 'Unknown')}: {e}")
            # On error, err on the side of caution and allow the request
            return True
    
    async def get_agent_analytics(self, agent_id: str) -> Dict[str, Any]:
        """Get analytics data for a specific agent.
        
        Args:
            agent_id: Forum agent ID
            
        Returns:
            Analytics data dictionary
        """
        try:
            response = await self._api_client.get(f"/forum-agents/{agent_id}/analytics")
            
            # Handle error responses
            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)
            
            # Parse successful response
            analytics = response.json()
            logger.debug(f"Retrieved analytics for agent {agent_id}")
            return analytics
        except Exception as e:
            logger.error(f"Failed to get analytics for agent {agent_id}: {e}")
            raise APIError(f"Failed to get agent analytics: {e}")
    
    async def get_user_subscriptions(self, guild_id: str, forum_channel_id: str) -> List[Dict[str, Any]]:
        """Get all active user subscriptions for a specific forum channel.
        
        Args:
            guild_id: Discord guild ID
            forum_channel_id: Discord forum channel ID
            
        Returns:
            List of user subscription data
        """
        try:
            response = await self._api_client.get(
                f"/guilds/{guild_id}/forum-channels/{forum_channel_id}/user-subscriptions"
            )
            
            if response.status_code >= 400:
                if response.status_code == 404:
                    return []  # No subscriptions found
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)
            
            subscriptions = response.json()
            logger.debug(f"Found {len(subscriptions)} user subscriptions for forum {forum_channel_id}")
            return subscriptions
        except Exception as e:
            logger.error(f"Failed to get user subscriptions for forum {forum_channel_id}: {e}")
            raise APIError(f"Failed to get user subscriptions: {e}")

    async def get_notification_topics(self, guild_id: str, forum_channel_id: str) -> List[str]:
        """Get available notification topics for a specific forum channel.
        
        Args:
            guild_id: Discord guild ID
            forum_channel_id: Discord forum channel ID
            
        Returns:
            List of topic names
        """
        try:
            response = await self._api_client.get(
                f"/guilds/{guild_id}/forum-channels/{forum_channel_id}/notification-topics"
            )
            
            if response.status_code >= 400:
                if response.status_code == 404:
                    return []  # No topics configured
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)
            
            topics_data = response.json()
            topic_names = [topic["topic_name"] for topic in topics_data]
            logger.debug(f"Found {len(topic_names)} notification topics for forum {forum_channel_id}")
            return topic_names
        except Exception as e:
            logger.error(f"Failed to get notification topics for forum {forum_channel_id}: {e}")
            raise APIError(f"Failed to get notification topics: {e}")

    def determine_agent_operation_mode(self, agent: Dict[str, Any]) -> str:
        """Determine what operation mode an agent should use.
        
        Args:
            agent: Forum agent configuration
            
        Returns:
            Operation mode: 'response_only', 'tagging_only', or 'combined'
        """
        enable_responses = agent.get('enable_responses', True)
        enable_tagging = agent.get('enable_user_tagging', False)
        
        if enable_responses and enable_tagging:
            return 'combined'
        elif enable_tagging:
            return 'tagging_only'
        else:
            return 'response_only'

    async def process_forum_post_with_tagging(
        self, 
        guild_id: str, 
        post: Any,
        user_subscriptions: List[Dict[str, Any]] = None
    ) -> tuple[List[Dict[str, Any]], List[str]]:
        """Process a forum post through all applicable agents with user tagging support.
        
        Args:
            guild_id: Discord guild ID
            post: Forum post object
            user_subscriptions: Optional cached user subscriptions
            
        Returns:
            tuple[List[Dict[str, Any]], List[str]]: Agent responses, user mentions for notifications
        """
        responses = []
        all_matching_topics = set()
        
        try:
            # Load all active agents for the guild
            agents = await self.load_guild_agents(guild_id)
            logger.error(f"DEBUG NOTIFICATIONS: Loaded {len(agents)} agents: {[{k: v for k, v in agent.items() if k in ['name', 'enable_responses', 'enable_user_tagging']} for agent in agents]}")
            
            if not agents:
                logger.debug(f"No forum agents found for guild {guild_id}")
                return responses, []
            
            channel_id = getattr(post, 'channel_id', '')
            logger.error(f"DEBUG NOTIFICATIONS: Processing post in channel {channel_id}")
            
            # Get user subscriptions if any agent has tagging enabled
            has_tagging_agents = any(agent.get('enable_user_tagging', False) for agent in agents)
            if has_tagging_agents and user_subscriptions is None:
                user_subscriptions = await self.get_user_subscriptions(guild_id, channel_id)
            
            # Get available topics if needed
            available_topics = []
            if has_tagging_agents and user_subscriptions:
                available_topics = await self.get_notification_topics(guild_id, channel_id)
                
            # Process post through each relevant agent
            for agent_data in agents:
                try:
                    # Check if agent should monitor this forum
                    if not self.should_agent_monitor_forum(agent_data, channel_id):
                        continue
                    
                    # Determine operation mode for this agent
                    operation_mode = self.determine_agent_operation_mode(agent_data)
                    logger.error(f"DEBUG NOTIFICATIONS: Agent {agent_data.get('name', 'unknown')} has operation_mode={operation_mode}, available_topics={len(available_topics)} topics: {available_topics}")
                    
                    # Check rate limits for response generation
                    within_rate_limit = True
                    if operation_mode in ['response_only', 'combined']:
                        within_rate_limit = await self.check_rate_limit(agent_data)
                        if not within_rate_limit:
                            responses.append({
                                'agent_id': agent_data['id'],
                                'agent_name': agent_data['name'],
                                'should_respond': False,
                                'decision_reason': f"Agent rate limit exceeded ({agent_data.get('max_responses_per_hour', 5)}/hour)",
                                'confidence': 0.0,
                                'response_content': '',
                                'matching_topics': [],
                                'tokens_used': 0,
                                'operation_mode': operation_mode
                            })
                            continue
                    
                    # Process based on operation mode
                    if operation_mode == 'response_only':
                        # Standard response evaluation (existing logic)
                        decision, confidence, response_content, tokens_used = await self.evaluate_post(agent_data, post)
                        threshold = agent_data.get('response_threshold', 0.7)
                        should_respond = bool(confidence >= threshold and response_content.strip())
                        matching_topics = []
                        
                    elif operation_mode == 'tagging_only':
                        # Topic classification only
                        from smarter_dev.bot.agents.forum_agent import ForumMonitorAgent
                        ai_agent = ForumMonitorAgent()
                        
                        matching_topics, tokens_used = await ai_agent.classify_topics_only(
                            available_topics=available_topics,
                            post_title=getattr(post, 'title', ''),
                            post_content=getattr(post, 'content', ''),
                            author_display_name=getattr(post, 'author_display_name', 'Unknown'),
                            post_tags=getattr(post, 'tags', []),
                            attachment_names=getattr(post, 'attachments', [])
                        )
                        
                        decision = f"Topic classification only: {len(matching_topics)} topics matched"
                        confidence = 0.0
                        response_content = ""
                        should_respond = False
                        
                    else:  # combined mode
                        # Both response evaluation and topic classification
                        from smarter_dev.bot.agents.forum_agent import ForumMonitorAgent
                        ai_agent = ForumMonitorAgent()
                        
                        decision, confidence, response_content, matching_topics, tokens_used = await ai_agent.evaluate_post_combined(
                            system_prompt=agent_data['system_prompt'],
                            available_topics=available_topics,
                            post_title=getattr(post, 'title', ''),
                            post_content=getattr(post, 'content', ''),
                            author_display_name=getattr(post, 'author_display_name', 'Unknown'),
                            post_tags=getattr(post, 'tags', []),
                            attachment_names=getattr(post, 'attachments', [])
                        )
                        
                        threshold = agent_data.get('response_threshold', 0.7)
                        should_respond = bool(confidence >= threshold and response_content.strip())
                    
                    # Collect matching topics for user notifications
                    logger.error(f"DEBUG NOTIFICATIONS: Agent {agent_data.get('name', 'unknown')} operation_mode={operation_mode}, matching_topics={matching_topics}")
                    logger.error(f"DEBUG NOTIFICATIONS: Post content for classification: title='{getattr(post, 'title', '')}', content='{getattr(post, 'content', '')}', author='{getattr(post, 'author_display_name', 'Unknown')}'")
                    if matching_topics:
                        all_matching_topics.update(matching_topics)
                    
                    # Calculate response time (approximate)
                    response_time_ms = int(self._evaluation_times[-1]) if self._evaluation_times else 1000
                    
                    # Record the response (we'll handle user mentions later for efficiency)
                    await self.record_response(
                        agent_data, post, decision, confidence, response_content,
                        tokens_used, response_time_ms, should_respond
                    )
                    
                    responses.append({
                        'agent_id': agent_data['id'],
                        'agent_name': agent_data['name'],
                        'should_respond': should_respond,
                        'decision_reason': decision,
                        'confidence': confidence,
                        'response_content': response_content if should_respond else '',
                        'matching_topics': matching_topics,
                        'tokens_used': tokens_used,
                        'operation_mode': operation_mode
                    })
                    
                except Exception as e:
                    logger.error(f"Error processing post with agent {agent_data.get('name', 'Unknown')}: {e}")
                    # Continue processing other agents
                    continue
            
            # Generate user mentions organized by topic
            topic_user_map = {}  # topic -> set of user mentions
            logger.error(f"DEBUG NOTIFICATIONS: all_matching_topics={all_matching_topics}, user_subscriptions={user_subscriptions}")
            
            if all_matching_topics and user_subscriptions:
                from datetime import datetime, timezone, timedelta
                current_time = datetime.now(timezone.utc)
                logger.error(f"DEBUG NOTIFICATIONS: Processing {len(user_subscriptions)} subscriptions at {current_time}")
                
                for subscription in user_subscriptions:
                    logger.error(f"DEBUG NOTIFICATIONS: Checking subscription for user {subscription.get('user_id')}")
                    
                    # Check if subscription has expired
                    if subscription.get('notification_hours', -1) != -1:
                        updated_at = datetime.fromisoformat(subscription['updated_at'].replace('Z', '+00:00'))
                        expiry_time = updated_at + timedelta(hours=subscription['notification_hours'])
                        logger.error(f"DEBUG NOTIFICATIONS: Expiry check - current: {current_time}, expiry: {expiry_time}, expired: {current_time > expiry_time}")
                        if current_time > expiry_time:
                            logger.error(f"DEBUG NOTIFICATIONS: Subscription expired, skipping user {subscription.get('user_id')}")
                            continue
                    
                    # Check if any subscribed topics match
                    subscribed_topics = set(subscription.get('subscribed_topics', []))
                    matching = subscribed_topics & all_matching_topics
                    logger.error(f"DEBUG NOTIFICATIONS: Topic match check - subscribed: {subscribed_topics}, detected: {all_matching_topics}, matching: {matching}")
                    
                    if matching:  # Intersection check
                        user_mention = f"<@{subscription['user_id']}>"
                        # Add this user to each matching topic
                        for topic in matching:
                            if topic not in topic_user_map:
                                topic_user_map[topic] = set()
                            topic_user_map[topic].add(user_mention)
                        logger.error(f"DEBUG NOTIFICATIONS: Will notify user {subscription['username']} for topics: {matching}")
                    else:
                        logger.error(f"DEBUG NOTIFICATIONS: No topic match for user {subscription.get('username')}")
            else:
                logger.error(f"DEBUG NOTIFICATIONS: Skipped notifications - topics empty: {not all_matching_topics}, subscriptions empty: {not user_subscriptions}")
            
            logger.info(f"Processed forum post through {len(responses)} agents, {sum(1 for r in responses if r['should_respond'])} will respond, topic notifications: {list(topic_user_map.keys())}")
            return responses, topic_user_map
            
        except Exception as e:
            logger.error(f"Error processing forum post for guild {guild_id}: {e}")
            raise ServiceError(f"Forum post processing failed: {e}")

    async def process_forum_post(self, guild_id: str, post: Any) -> List[Dict[str, Any]]:
        """Process a forum post through all applicable agents.
        
        Args:
            guild_id: Discord guild ID
            post: Forum post object
            
        Returns:
            List of agent responses and decisions
        """
        responses = []
        
        try:
            # Load all active agents for the guild
            agents = await self.load_guild_agents(guild_id)
            
            if not agents:
                logger.debug(f"No forum agents found for guild {guild_id}")
                return responses
            
            # Process post through each relevant agent
            for agent_data in agents:
                try:
                    # Check if agent should monitor this forum
                    channel_id = getattr(post, 'channel_id', '')
                    if not self.should_agent_monitor_forum(agent_data, channel_id):
                        continue
                    
                    # Check rate limits
                    within_rate_limit = await self.check_rate_limit(agent_data)
                    if not within_rate_limit:
                        responses.append({
                            'agent_id': agent_data['id'],
                            'agent_name': agent_data['name'],
                            'should_respond': False,
                            'decision_reason': f"Agent rate limit exceeded ({agent_data.get('max_responses_per_hour', 5)}/hour)",
                            'confidence': 0.0,
                            'response_content': '',
                            'tokens_used': 0
                        })
                        continue
                    
                    # Evaluate the post
                    decision, confidence, response_content, tokens_used = await self.evaluate_post(agent_data, post)
                    
                    # Determine if we should respond based on confidence threshold
                    threshold = agent_data.get('response_threshold', 0.7)
                    should_respond = bool(confidence >= threshold and response_content.strip())
                    
                    # Calculate response time (approximate)
                    response_time_ms = int(self._evaluation_times[-1]) if self._evaluation_times else 1000
                    
                    # Record the response
                    await self.record_response(
                        agent_data, post, decision, confidence, response_content,
                        tokens_used, response_time_ms, should_respond
                    )
                    
                    responses.append({
                        'agent_id': agent_data['id'],
                        'agent_name': agent_data['name'],
                        'should_respond': should_respond,
                        'decision_reason': decision,
                        'confidence': confidence,
                        'response_content': response_content if should_respond else '',
                        'tokens_used': tokens_used
                    })
                    
                except Exception as e:
                    logger.error(f"Error processing post with agent {agent_data.get('name', 'Unknown')}: {e}")
                    # Continue processing other agents
                    continue
            
            logger.info(f"Processed forum post through {len(responses)} agents, {sum(1 for r in responses if r['should_respond'])} will respond")
            return responses
            
        except Exception as e:
            logger.error(f"Error processing forum post for guild {guild_id}: {e}")
            raise ServiceError(f"Forum post processing failed: {e}")
    
    async def health_check(self) -> ServiceHealth:
        """Check service health."""
        try:
            details = {
                "agents": "Forum agents are operational",
                "evaluations_processed": self._evaluations_processed,
                "responses_generated": self._responses_generated,
                "total_tokens_used": self._total_tokens_used,
                "average_evaluation_time": sum(self._evaluation_times) / len(self._evaluation_times) if self._evaluation_times else 0
            }
            
            return ServiceHealth(
                service_name=self._service_name,
                is_healthy=True,
                details=details
            )
        except Exception as e:
            return ServiceHealth(
                service_name=self._service_name,
                is_healthy=False,
                details={"error": str(e)}
            )
    
    def get_service_stats(self) -> Dict[str, Any]:
        """Get service statistics."""
        avg_eval_time = sum(self._evaluation_times) / len(self._evaluation_times) if self._evaluation_times else 0
        
        return {
            "service_name": self._service_name,
            "evaluations_processed": self._evaluations_processed,
            "responses_generated": self._responses_generated,
            "total_tokens_used": self._total_tokens_used,
            "average_evaluation_time": round(avg_eval_time, 2),
            "response_rate": self._responses_generated / max(1, self._evaluations_processed),
            "is_initialized": self._is_initialized
        }