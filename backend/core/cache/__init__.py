"""
Caching layer for the orchestration and agent system.

Two sub-modules:
- prompt_cache: provider-payload decorators (Anthropic cache_control, etc.)
- response_cache: exact + semantic cache for LLM responses (skips AGENT steps)

There is no tool-result cache. One existed; it served exactly one tool
(`collect_data`), and a hit skipped marking the reply as a form (F67). A
tool's result is whatever the tool returns now.
"""
from core.cache import prompt_cache, response_cache, store

__all__ = ["prompt_cache", "response_cache", "store"]
