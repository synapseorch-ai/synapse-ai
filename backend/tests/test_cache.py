"""
Unit tests for the cache layer (prompt_cache, tool_cache, response_cache, store).

Run with:
    cd backend && python -m pytest tests/test_cache.py -v

Tests use SYNAPSE_DATA_DIR so the real data/ is never touched. Set BEFORE
importing core modules — module-level reads of DATA_DIR happen at import time.
"""
import os
import sys
import time
import tempfile
import pathlib
from unittest.mock import patch

# Sandbox a temp data dir for all tests in this module
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="synapse_test_cache_")
os.environ["SYNAPSE_DATA_DIR"] = _TMP_DATA_DIR

# Ensure backend/ is importable when run from the repo root
_BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))


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

    # Force expiry by manipulating created_at instead of sleeping
    path = store._path_for("ttl_ns", store._hash_key(key) if len(key) != 64 else key)
    import json
    raw = json.loads(path.read_text())
    raw["created_at"] = time.time() - 3600
    path.write_text(json.dumps(raw))

    assert store.get("ttl_ns", key) is None
    # Expired entries are reaped on get()
    assert not path.exists()


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
    from core.cache.prompt_cache import decorate_anthropic_kwargs, VOLATILE_SEPARATOR
    system = "X" * 5000 + VOLATILE_SEPARATOR + "current time: now"
    tools = [
        {"name": "tool_a", "input_schema": {}},
        {"name": "tool_b", "input_schema": {}},
    ]
    kwargs = {"messages": [], "tools": tools}
    out = decorate_anthropic_kwargs(kwargs, system)

    # System is a list of blocks; stable prefix has cache_control, volatile suffix does not.
    assert isinstance(out["system"], list)
    assert out["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "current time" in out["system"][1]["text"]
    assert "cache_control" not in out["system"][1]

    # Tools: only the LAST tool gets cache_control (whole array becomes prefix).
    assert "cache_control" not in out["tools"][0]
    assert out["tools"][1]["cache_control"] == {"type": "ephemeral"}


def test_prompt_cache_split_no_separator():
    from core.cache.prompt_cache import split_stable_volatile
    stable, volatile = split_stable_volatile("just a flat prompt")
    assert stable == "just a flat prompt"
    assert volatile == ""


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
