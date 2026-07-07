"""Harness sanity checks: the app imports, the client works, the fake LLM is
wired, and OpenAPI is reachable. If these fail, nothing else will run."""
import pytest


async def test_app_imports_and_openapi_served(client):
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["openapi"].startswith("3.")
    # A representative set of routes should be registered.
    paths = spec["paths"]
    assert "/chat" in paths
    assert any(p.startswith("/api/v1/") for p in paths)
    assert any(p.startswith("/api/v2/") for p in paths)


async def test_fake_llm_is_installed(fake_llm):
    # Patched at the definition site...
    import core.llm_providers as llm
    assert llm.generate_response is fake_llm
    # ...and at the eagerly-bound alias sites.
    import core.react_engine as re
    assert re.llm_generate_response is fake_llm
    result = await llm.generate_response(prompt_msg="hi", sys_prompt="", mode="cloud",
                                         current_model="claude-x", current_settings={})
    assert result == fake_llm.default
    assert fake_llm.call_count == 1


async def test_fake_llm_scripting(fake_llm):
    fake_llm.script(["first", "second"])
    import core.llm_providers as llm
    assert await llm.generate_response() == "first"
    assert await llm.generate_response() == "second"
    assert await llm.generate_response() == fake_llm.default  # exhausted → default
