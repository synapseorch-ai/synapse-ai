"""
Unit tests for the cache layer (prompt_cache, tool_cache, response_cache, store).

Run with:
    python -m pytest backend/tests/unit/test_cache.py -v

Isolation and sys.path setup are handled once by the suite-wide conftest.py
(backend/tests/conftest.py), which gives every test its own database, blob
store, scratch and state directory.
"""
import time
from unittest.mock import patch


# ── store ──────────────────────────────────────────────────────────────────

def test_store_set_get_delete():
    from core.cache import store
    key = store.make_key("test", "set_get", {"x": 1})
    store.set("test_ns", key, {"hello": "world"})
    entry = store.get("test_ns", key)
    assert entry is not None
    assert entry["value"] == {"hello": "world"}

    assert store.delete("test_ns", key) is True
    assert store.get("test_ns", key) is None


def test_store_ttl_expiry():
    from core.cache import store
    key = store.make_key("test", "ttl")
    store.set("ttl_ns", key, "fresh", ttl_seconds=1)
    assert store.get("ttl_ns", key)["value"] == "fresh"

    # Force expiry by manipulating created_at instead of sleeping. Entries are
    # blobs now, so this reaches through the blob store rather than the
    # filesystem — which is also what keeps the entry tenant-scoped.
    import json

    from core.storage import get_blob_store
    blob = store._blob_key("ttl_ns", store._hash_key(key) if len(key) != 64 else key)
    raw = json.loads(get_blob_store().get(blob))
    raw["created_at"] = time.time() - 3600
    get_blob_store().put(blob, json.dumps(raw))

    assert store.get("ttl_ns", key) is None
    # Expired entries are reaped on get()
    assert not get_blob_store().exists(blob)


def test_store_make_key_stable_for_dict_order():
    from core.cache import store
    k1 = store.make_key("t", {"a": 1, "b": 2})
    k2 = store.make_key("t", {"b": 2, "a": 1})
    assert k1 == k2


# ── prompt_cache ───────────────────────────────────────────────────────────

def test_prompt_cache_anthropic_decoration_below_threshold():
    from core.cache.prompt_cache import decorate_anthropic_kwargs
    kwargs = {"messages": [], "system": "short"}
    out = decorate_anthropic_kwargs(kwargs, "short")
    # System stays as a plain string when below the cacheable floor.
    assert isinstance(out["system"], str)


def test_prompt_cache_anthropic_decoration_above_threshold():
    from core.cache.prompt_cache import decorate_anthropic_kwargs
    system = "X" * 5000
    tools = [
        {"name": "tool_a", "input_schema": {}},
        {"name": "tool_b", "input_schema": {}},
    ]
    kwargs = {"messages": [], "tools": tools}
    out = decorate_anthropic_kwargs(kwargs, system)

    # The whole system prompt is one cached block: it is stable by construction
    # now, so there is no volatile suffix to leave unmarked.
    assert out["system"] == [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    # Tools: only the LAST tool gets cache_control (whole array becomes prefix).
    assert "cache_control" not in out["tools"][0]
    assert out["tools"][1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in tools[1]  # caller's list untouched


def test_prompt_cache_markdown_rule_in_system_is_not_a_boundary():
    """A linked DB, a linked repo, or a user's own `---` used to split the
    system prompt at the first one, leaving everything after it uncached."""
    from core.cache.prompt_cache import decorate_anthropic_kwargs
    system = "A" * 3000 + "\n---\n" + "B" * 3000
    out = decorate_anthropic_kwargs({"messages": []}, system)
    assert len(out["system"]) == 1
    assert out["system"][0]["text"] == system


def test_prompt_cache_user_content_marks_last_transcript_block_only():
    from core.cache.prompt_cache import build_anthropic_user_content
    blocks = ["User Request: go\n", "\n[Turn 1 — Action]\n{...}\n", "\n[Turn 2 — Action]\n{...}\n"]
    out = build_anthropic_user_content(blocks, "### TURN BUDGET\n3 left", prefix_chars=10_000)
    assert [b["text"] for b in out] == blocks + ["### TURN BUDGET\n3 left"]
    marked = [i for i, b in enumerate(out) if "cache_control" in b]
    assert marked == [2]           # the last transcript block, never the tail


def test_prompt_cache_user_content_gate_is_the_cumulative_prefix():
    """A short agent prompt in front of a long transcript is still worth
    caching; the floor applies to everything before the marker, not to the
    system prompt alone."""
    from core.cache.prompt_cache import build_anthropic_user_content
    blocks = ["short", "\n[Turn 1 — Action]\n" + "x" * 5000]
    assert "cache_control" in build_anthropic_user_content(blocks, None, prefix_chars=5_100)[-1]
    assert "cache_control" not in build_anthropic_user_content(blocks, None, prefix_chars=100)[-1]


def test_prompt_cache_user_content_without_tail_has_no_trailing_block():
    from core.cache.prompt_cache import build_anthropic_user_content
    out = build_anthropic_user_content(["a", "b"], None, prefix_chars=0)
    assert [b["text"] for b in out] == ["a", "b"]
    assert not any("cache_control" in b for b in out)


def test_prompt_cache_extract_openai_cache_tokens():
    from core.cache.prompt_cache import extract_openai_cache_tokens
    usage = {"prompt_tokens": 1500, "prompt_tokens_details": {"cached_tokens": 1000}}
    read, write = extract_openai_cache_tokens(usage)
    assert read == 1000
    assert write == 0


def test_prompt_cache_extract_deepseek_cache_tokens():
    from core.cache.prompt_cache import extract_deepseek_cache_tokens
    usage = {"prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200}
    read, write = extract_deepseek_cache_tokens(usage)
    assert read == 800
    assert write == 0


def test_prompt_cache_global_toggle_default():
    from core.cache.prompt_cache import cache_enabled
    assert cache_enabled(None) is True
    assert cache_enabled({}) is True
    assert cache_enabled({"prompt_cache_enabled": False}) is False
    assert cache_enabled({"prompt_cache_enabled": True}) is True


# ── tool_cache ─────────────────────────────────────────────────────────────

def test_tool_cache_only_deterministic():
    from core.cache import tool_cache
    assert tool_cache.is_cacheable("code_search") is True
    assert tool_cache.is_cacheable("pdf_parser") is True
    assert tool_cache.is_cacheable("bash") is False
    assert tool_cache.is_cacheable("sql_agent") is False
    assert tool_cache.is_cacheable("nonexistent_tool") is False


def test_tool_cache_set_get_hit():
    from core.cache import tool_cache
    args = {"query": "foo bar", "limit": 10}
    assert tool_cache.get("code_search", args) is None
    tool_cache.set("code_search", args, "search results here", ttl_seconds=60)
    assert tool_cache.get("code_search", args) == "search results here"


def test_tool_cache_session_scope():
    """personal_details is session-scoped — different sessions, different cache."""
    from core.cache import tool_cache
    args = {"key": "address"}
    tool_cache.set("personal_details", args, "alice's address", session_id="sess_alice")
    tool_cache.set("personal_details", args, "bob's address", session_id="sess_bob")
    assert tool_cache.get("personal_details", args, session_id="sess_alice") == "alice's address"
    assert tool_cache.get("personal_details", args, session_id="sess_bob") == "bob's address"


def test_tool_cache_side_effectful_silently_ignored():
    from core.cache import tool_cache
    # Setting on a non-deterministic tool is a no-op; get returns None.
    tool_cache.set("bash", {"cmd": "ls"}, "/tmp output")
    assert tool_cache.get("bash", {"cmd": "ls"}) is None


# ── response_cache (exact match only — semantic path needs a live MemoryStore) ─

def test_response_cache_exact_miss_and_hit():
    from core.cache import response_cache
    system = "you are a helpful assistant"
    msgs = [{"role": "user", "content": "what's 2+2?"}]
    tools = None

    assert response_cache.get_exact("model-x", system, msgs, tools) is None
    response_cache.set_exact(
        "model-x", system, msgs, tools,
        text="4", input_tokens=10, output_tokens=1, ttl_seconds=60,
    )
    hit = response_cache.get_exact("model-x", system, msgs, tools)
    assert hit is not None
    assert hit["text"] == "4"
    assert hit["input_tokens"] == 10


def test_response_cache_key_differs_by_model():
    from core.cache import response_cache
    msgs = [{"role": "user", "content": "ping"}]
    response_cache.set_exact("model-a", "sys", msgs, None, text="A", input_tokens=1, output_tokens=1)
    assert response_cache.get_exact("model-a", "sys", msgs, None)["text"] == "A"
    assert response_cache.get_exact("model-b", "sys", msgs, None) is None


def test_response_cache_key_normalises_tool_schema():
    """Different surrounding metadata, same function name+params → same cache key."""
    from core.cache import response_cache
    msgs = [{"role": "user", "content": "use a tool"}]
    tools_v1 = [{"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}}}]
    tools_v2 = [{"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}, "description": "wrapped"}}]
    response_cache.set_exact("m", "sys", msgs, tools_v1, text="ok", input_tokens=1, output_tokens=1)
    # Description not part of cache key; same params → hit.
    assert response_cache.get_exact("m", "sys", msgs, tools_v2)["text"] == "ok"


# ── usage_tracker cache math ───────────────────────────────────────────────

def test_calculate_cost_with_cache_tokens():
    from core import usage_tracker
    # Stub pricing so the test is deterministic regardless of model_pricing.json.
    with patch.object(usage_tracker, "_load_pricing", return_value={
        "stub-model": {"provider": "anthropic", "input_per_1m": 3.0, "output_per_1m": 15.0}
    }):
        # No cache: 1M input = $3.00
        assert usage_tracker.calculate_cost("stub-model", 1_000_000, 0) == 3.0
        # Anthropic defaults: cache_read at 10% (=$0.30/1M), cache_write at 125% (=$3.75/1M)
        cost = usage_tracker.calculate_cost(
            "stub-model", 0, 0,
            cache_read_tokens=1_000_000, cache_write_tokens=0,
        )
        assert cost == 0.3
        cost = usage_tracker.calculate_cost(
            "stub-model", 0, 0,
            cache_read_tokens=0, cache_write_tokens=1_000_000,
        )
        assert cost == 3.75


def test_calculate_savings():
    from core import usage_tracker
    with patch.object(usage_tracker, "_load_pricing", return_value={
        "stub-model": {"provider": "anthropic", "input_per_1m": 3.0, "output_per_1m": 15.0}
    }):
        # 1M cache reads at 10% rate → saved $2.70 vs full $3.00
        assert usage_tracker.calculate_savings("stub-model", 1_000_000) == 2.7
        assert usage_tracker.calculate_savings("stub-model", 0) == 0.0


def test_calculate_cost_unknown_model_returns_zero():
    from core import usage_tracker
    with patch.object(usage_tracker, "_load_pricing", return_value={}):
        assert usage_tracker.calculate_cost("never-heard-of-it", 1000, 1000) == 0.0
