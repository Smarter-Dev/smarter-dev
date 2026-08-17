"""Two-pass proactive chat bot: DeepSeek V4 Flash watcher + chat agent.

Born as the eval experiment under scripts/proactive_eval/twopass/ (which now
re-exports from here); this package is the production home. The watcher
decides when to wake the agent; the agent reads, acts or deliberately stays
silent, and updates the watcher's criteria for continuity.
"""
