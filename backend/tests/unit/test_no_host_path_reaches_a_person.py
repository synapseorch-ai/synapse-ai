"""No host filesystem path reaches a person: not in a Vault tool's answer, a step's error or a run's (F1627).

The sandbox's Vault tools answered with the absolute path of every file they touched, and said "File not found:
/home/<user>/…/vault/expenses.md" when one was missing; the engine copied a tool's error into the step's and the run's
error as it was, so the run page showed the host's user name and folder layout, and a read's answer in run state
carried the same path. Now the Vault tools answer with Vault-relative paths (an absolute one inside the Vault is still
read, so a workflow saved with one runs as before), and a tool's error text is said with the Vault's paths relative
and the home folders by name. Another tool's absolute path in a successful answer stays in state as it was: a later
step may need it.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from _fakes import seed as S
from core.storage import LocalBlobStore, get_blob_store, set_blob_store
from tests.unit.test_tool_step_errors import _AFTER, _run, _server


def _json(result) -> dict:
    return json.loads(result[0].text)


@pytest.fixture
def vault(tmp_path):
    before = get_blob_store()
    set_blob_store(LocalBlobStore(tmp_path))
    from core.vault_backend import get_vault

    root = get_vault().root
    root.mkdir(parents=True, exist_ok=True)
    yield root
    set_blob_store(before)


class TestTheVaultToolsAnswerWithVaultPaths:
    def test_each_answer_names_the_file_by_its_vault_path(self, vault):
        from tools import sandbox

        assert _json(sandbox._handle_write({"path": "reports/a.md", "content": "x"}))["path"] == "reports/a.md"
        assert _json(sandbox._handle_read({"path": "reports/a.md"}))["path"] == "reports/a.md"
        assert _json(sandbox._handle_create({"filename": "notes/b.md", "content": "y"}))["path"] == "notes/b.md"
        assert _json(sandbox._handle_patch({"path": "reports/a.md", "find": "x", "replace": "z"}))["path"] == "reports/a.md"

    def test_a_refusal_names_the_file_by_its_vault_path(self, vault):
        from tools import sandbox

        said = _json(sandbox._handle_read({"path": "expenses.md"}))["error"]
        assert "expenses.md" in said and str(vault) not in said and "/home/" not in said, said
        (vault / "taken.md").write_text("here")
        said = _json(sandbox._handle_create({"filename": "taken.md", "content": "y"}))["error"]
        assert "taken.md" in said and str(vault) not in said, said

    def test_an_absolute_path_inside_the_vault_is_still_read(self, vault):
        """A workflow saved with the old answer's absolute path in its tool arguments runs as before."""
        from tools import sandbox

        (vault / "old.md").write_text("as before")
        out = _json(sandbox._handle_read({"path": str(vault / "old.md")}))
        assert out["content"] == "as before" and out["path"] == "old.md", out


class TestATextIsSaidWithoutTheHostsPaths:
    def test_the_vaults_paths_are_relative_and_the_homes_by_name(self, vault):
        from core.host_paths import scrub

        home = str(Path.home())
        text = f"File not found: {vault}/expenses.md, and {home}/.cache/thing failed"
        said = scrub(text)
        assert "expenses.md" in said and str(vault) not in said and home not in said, said

    def test_text_without_a_host_path_is_unchanged(self):
        from core.host_paths import scrub

        assert scrub("A plain error: the city is missing.") == "A plain error: the city is missing."


def _session(text: str, is_error: bool):
    class Session:
        async def call_tool(self, name, args, read_timeout_seconds=None, **kw):
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)], isError=is_error)
    return Session()


@pytest.fixture
def one_tool(monkeypatch):
    import core.tools as tools_mod

    async def resolve_one_tool(server_module, name, agent, custom):
        return types.SimpleNamespace(name=name, description="d", inputSchema={"type": "object"})

    monkeypatch.setattr(tools_mod, "resolve_one_tool", resolve_one_tool)

    def server_for(session):
        server = _server()
        server.agent_sessions = {"vault_sandbox": session}
        server.tool_router = {"t": ("vault_sandbox", "t")}
        return server
    return server_for


def _step() -> dict:
    return {"id": "call", "name": "Record it", "type": "tool", "forced_tool": "t",
            "tool_arguments": {"path": "expenses.md"}, "output_key": "result", "next_step_id": "after"}


class TestARunSaysNoHostPath:
    async def test_a_failed_tool_steps_error_names_the_file_not_the_host(self, vault, one_tool):
        text = json.dumps({"error": f"File not found: {vault}/expenses.md"})
        events, done = await _run(S.make_orchestration(entry_step_id="call", steps=[_step(), _AFTER]),
                                  one_tool(_session(text, True)))
        assert done["status"] == "failed"
        # What the run page shows of a failed step: the step's reason, and its last answer and data.
        said = " ".join(json.dumps([e.get(k) for k in ("reason", "response", "data", "error", "message")], default=str)
                        for e in events if e.get("type") in ("step_error", "final", "orchestration_error",
                                                             "orchestration_complete"))
        assert "expenses.md" in said, said
        assert str(vault) not in said and str(Path.home()) not in said, said

    async def test_another_tools_absolute_path_in_a_good_answer_stays_in_state(self, vault, one_tool):
        text = json.dumps({"path": "/srv/data/orders.csv", "rows": 3})
        _, done = await _run(S.make_orchestration(entry_step_id="call", steps=[_step(), _AFTER]),
                             one_tool(_session(text, False)))
        assert done["status"] == "completed"
        assert "/srv/data/orders.csv" in json.dumps(done["final_state"].get("result"))
