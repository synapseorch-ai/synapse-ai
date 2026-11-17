"""A step that can't read files gets what of a saved output fits its read share, and is told the rest isn't shown
(F1677, the run-time floor).

An output over the spill line is saved to the vault, and a step's prompt names the file instead of pasting it ("read
the parts you need with read_file"). An Agent step can; an LLM step has no tools, so it got the note and none of the
text, and wrote from nothing. Now an LLM step gets the head of it, up to its read share, then "the rest, N characters,
is not shown: this step cannot read files; an Agent step can", whether it reads the value by `{{ state.x }}` or by its
input keys.

Near misses: an Agent step still gets the file and how to read it; a value that fits is whole, as before.
"""
from __future__ import annotations

import re
import types
from typing import Any

import pytest
from _fakes import seed as S

from core import step_refs
from core.storage import set_blob_store
from core.storage.base import tenant_key

BIG = "".join(f"Line {n:05d}: Harbor & Pine's catalogue, made up for a test, padded to length.\n" for n in range(2000))
NOT_SHOWN = "is not shown: this step cannot read files; an Agent step can"


class _Blobs:
    """A blob store with no filesystem: what is written is in the store, or nowhere."""

    def __init__(self):
        self.blobs: dict[str, str] = {}

    def put(self, key, content):
        self.blobs[tenant_key(key)] = content

    def get(self, key):
        return self.blobs.get(tenant_key(key))

    def exists(self, key):
        return tenant_key(key) in self.blobs

    def delete(self, key):
        self.blobs.pop(tenant_key(key), None)

    def list(self, prefix=""):
        scoped, cut = tenant_key(prefix), tenant_key("")
        return [k[len(cut):] for k in self.blobs if k.startswith(scoped)]

    def path_for(self, key):
        return None


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_SCRATCH_DIR", str(tmp_path / "scratch"))
    step_refs.forget_cache()
    set_blob_store(_Blobs())
    yield
    set_blob_store(None)
    step_refs.forget_cache()


class _Site:
    def __init__(self, text: str) -> None:
        self.text = text

    async def send_ping(self):
        return None

    async def list_tools(self):
        return types.SimpleNamespace(tools=[types.SimpleNamespace(
            name="read_pages", description="Reads the site's pages.", inputSchema={"type": "object", "properties": {}},
            annotations=types.SimpleNamespace(readOnlyHint=True, destructiveHint=False, openWorldHint=False,
                                              idempotentHint=True))])

    async def call_tool(self, name, args, read_timeout_seconds=None):
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=self.text)], isError=False)


class _Model:
    """Records each call's prompt; answers "ok"."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def __call__(self, *args: Any, **kw: Any):
        self.prompts.append(str(kw.get("prompt_msg") or (args[0] if args else "")))
        return "ok"


def _install(monkeypatch, model: _Model) -> None:
    import core.llm_providers as _llm
    import core.react_engine as _re
    import core.routes.agents as _agents

    for module, name in ((_llm, "generate_response"), (_re, "llm_generate_response"),
                         (_agents, "llm_generate_response")):
        monkeypatch.setattr(module, name, model, raising=False)


async def _prompt(monkeypatch, reader: dict, *, text: str = BIG, tag: str) -> str:
    """What the reading step's model was sent, after a Tool step (no model) put `text` in state.big."""
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    model = _Model()
    _install(monkeypatch, model)
    fetch = {"id": "fetch", "name": "Read the pages", "type": "tool", "forced_tool": "site__read_pages",
             "tool_arguments": {}, "output_key": "big", "next_step_id": "use"}
    orch = S.make_orchestration(id=f"orch_f1677b_{tag}", entry_step_id="fetch",
                                steps=[fetch, {"id": "use", "name": "Use it", "output_key": "out", "next_step_id": None,
                                               **reader}])
    await S.seed_orchestrations([orch])
    server = types.SimpleNamespace(agent_sessions={"ext_mcp_site": _Site(text)}, memory_store=None,
                                   tool_router={"site__read_pages": ("ext_mcp_site", "read_pages")})
    engine = OrchestrationEngine(Orchestration.model_validate(orch), server)
    _ = [e async for e in engine.run("go", run_id=f"run_f1677b_{tag}")]
    assert model.prompts, "the reading step called its model"
    return model.prompts[0]


def _rest(prompt: str) -> int:
    m = re.search(r"the rest, ([\d,]+) characters, " + re.escape(NOT_SHOWN), prompt)
    assert m, prompt[-600:]
    return int(m.group(1).replace(",", ""))


LLM_BY_TEMPLATE = {"type": "llm", "model": "mistral", "prompt_template": "List the catalogue's lines: {{ state.big }}"}
LLM_BY_INPUT_KEY = {"type": "llm", "model": "mistral", "prompt_template": "List the catalogue's lines.",
                    "input_keys": ["big"]}


@pytest.mark.parametrize("reader", [LLM_BY_TEMPLATE, LLM_BY_INPUT_KEY], ids=["template", "input-key"])
async def test_an_llm_step_gets_the_head_and_is_told_the_rest_isnt_shown(monkeypatch, store, reader):
    prompt = await _prompt(monkeypatch, reader, tag=reader.get("input_keys") and "key" or "tpl")
    # Within any share an input key or a reference holds (an input key's is 5,000 at the least).
    assert "Line 00000:" in prompt and "Line 00030:" in prompt, "the head is in the prompt"
    assert "read_file" not in prompt, "a step with no tools is never told to read a file"
    rest = _rest(prompt)
    assert 0 < rest < len(BIG) and "Line 01999:" not in prompt, rest


async def test_an_agent_step_still_gets_the_file_and_how_to_read_it(monkeypatch, store):
    agent = S.make_agent(name="Reader", tools=[], max_turns=2)
    await S.seed_agents([agent])
    prompt = await _prompt(monkeypatch, {"type": "agent", "agent_id": agent["id"],
                                         "prompt_template": "List the catalogue's lines: {{ state.big }}"}, tag="agent")
    assert "read_file" in prompt and NOT_SHOWN not in prompt, prompt[-600:]


async def test_a_value_that_fits_is_whole(monkeypatch, store):
    small = BIG[:4000]
    prompt = await _prompt(monkeypatch, LLM_BY_TEMPLATE, text=small, tag="small")
    assert small.strip() in prompt and NOT_SHOWN not in prompt
