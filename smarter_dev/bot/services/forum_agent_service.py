"""Forum Agent Service for managing AI-driven forum post monitoring and responses."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from smarter_dev.bot.agent import ForumMonitorAgent
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
        responded: bool
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
            
        Returns:
            Response record ID
        """
        try:
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
                'response_content': response_content,
                'tokens_used': tokens_used,
                'response_time_ms': response_time_ms,
                'responded': responded
            }
            
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