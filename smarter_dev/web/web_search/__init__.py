"""The dashboard's web search: Luna writes queries, Brave runs them, Jev ranks.

``snapshot`` is the only module the web pod imports; it has no model
dependencies. ``pipeline`` and the modules it uses run in the agent-worker
(``smarter_dev.web.web_search_jobs``), because pydantic-ai costs more memory
than the web pod's limit allows.

The wording comes from the search eval in ``scripts/search_query_eval``: the
query prompt is its ``prompts/v6.md`` and the ranking prompt its
``judge_prompts/v7b.yaml``.
"""
