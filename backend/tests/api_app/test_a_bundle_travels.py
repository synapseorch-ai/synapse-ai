"""A bundle travels between Cloud and self-host (F464, P-2, CLAIM3 D266).

The site says "take your workflows with you" and "the same file imports into
Cloud" (claims AN-14, AN-25). CLAIM1 measured the other direction: Cloud's
export was refused here outright ("Not a valid Synapse export bundle.", no
`synapse_export`), and with the envelope patched, 0 of 75 workflows arrived —
Cloud strips their ids and this importer skipped any workflow without one.

The fixtures are Cloud's own files: `cloud-export-v1.json` is a trimmed copy of
the export acme's Settings → Import / Export wrote on 2026-10-11 (Cloud's old
shape: `version: 1`, `tools`, no ids), and `cloud-export-v1.1.json` is the same
entities in the envelope both products now write (`synapse_export`,
`version "1.1"`, `exported_from`, `custom_tools`, workflow ids kept).

Naveen's calls (2026-10-13): a model this install can't serve is imported
unchanged and listed in `warnings`; a workflow whose id already exists is
replaced, and the result says "replaced".
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bundles"

MARKET_RESEARCHER = "agent_01a1278625f27222894f471bae1aaa99"
C1B_FLOW_AGENT = "agent_01a0ed1c29ee72a6ac242f9079c3de6b"
C1B_FLOW_ORCH = "orch_01a0ed1c29cc729b9de2d7ebdce5ce8d"
DELEGATE = "agent_01a09a4a450c73c7bcac81f81ca25b01"
WIKI = "agent_gpjkm15gtu"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def everything(bundle: dict[str, Any]) -> dict[str, Any]:
    """The request the import screen sends with every row ticked. A workflow
    with no id is keyed by its place in the file, `#<index>`."""
    return {
        "bundle": bundle,
        "selected_orchestration_ids": [
            o.get("id") or f"#{i}" for i, o in enumerate(bundle.get("orchestrations", []))
        ],
        "selected_agent_ids": [a["id"] for a in bundle.get("agents", [])],
        "selected_mcp_server_names": [m["name"] for m in bundle.get("mcp_servers", [])],
        "selected_custom_tool_names": [
            t["name"] for t in bundle.get("custom_tools", bundle.get("tools", []))
        ],
    }


def warned(body: dict[str, Any], text: str) -> list[dict[str, Any]]:
    return [w for w in body.get("warnings", []) if text in w.get("message", "")]


async def _orchestrations(client: Any) -> list[dict[str, Any]]:
    r = await client.get("/api/orchestrations")
    assert r.status_code == 200, r.text
    return r.json()


class TestACloudExportImports:
    async def test_as_cloud_wrote_it_before_the_envelope(self, client: Any) -> None:
        r = await client.post("/api/import", json=everything(_load("cloud-export-v1.json")))
        assert r.status_code == 200, r.text
        body = r.json()
        res = body["results"]

        assert [o["status"] for o in res["orchestrations"]] == ["imported"] * 3
        minted = [o["id"] for o in res["orchestrations"]]
        assert all(i.startswith("orch_") for i in minted) and len(set(minted)) == 3
        assert {a["id"] for a in res["agents"]} == {
            MARKET_RESEARCHER, C1B_FLOW_AGENT, "agent_01a0ed1c299571d9ba453d0728f7c9a1", DELEGATE, WIKI}
        assert {t["name"] for t in res["custom_tools"]} == {"test", "i1_echo"}

        saved = {o["name"]: o for o in await _orchestrations(client)}
        assert {"Market brief", "C1B Parent", "C1B Flow"} <= saved.keys()
        assert saved["Market brief"]["id"] in minted
        [research] = [s for s in saved["Market brief"]["steps"] if s["id"] == "step_research"]
        assert research["agent_id"] == MARKET_RESEARCHER

        tools = (await client.get("/api/tools/custom")).json()
        assert {"test", "i1_echo"} <= {t["name"] for t in tools}

        # Cloud's old file stripped workflow ids, so an orchestrator agent's
        # workflow can't be found — said, not dropped.
        [dangling] = warned(body, C1B_FLOW_ORCH)
        assert (dangling["kind"], dangling["name"]) == ("agents", "C1B Flow")
        # A model this install can't serve is imported unchanged, and listed.
        assert warned(body, "synapse.kimi-k3")
        assert [a for a in (await client.get("/api/agents")).json()
                if a["id"] == MARKET_RESEARCHER][0]["model"] == "synapse.nova-lite"

    async def test_in_the_envelope_it_keeps_its_references(self, client: Any) -> None:
        bundle = _load("cloud-export-v1.1.json")
        r = await client.post("/api/import", json=everything(bundle))
        assert r.status_code == 200, r.text
        body = r.json()

        assert [o["id"] for o in body["results"]["orchestrations"]] == [
            o["id"] for o in bundle["orchestrations"]]
        saved = {o["id"] for o in await _orchestrations(client)}
        assert C1B_FLOW_ORCH in saved
        # The orchestrator agent's workflow and the delegate's agent arrived with it,
        # so the only warnings are about what this install can't run.
        assert not warned(body, C1B_FLOW_ORCH)
        assert not warned(body, WIKI)
        assert warned(body, "synapse.kimi-k3")

    async def test_a_workflow_with_the_same_id_is_replaced_and_says_so(self, client: Any) -> None:
        bundle = _load("cloud-export-v1.1.json")
        first = (await client.post("/api/import", json=everything(bundle))).json()["results"]
        renamed = copy.deepcopy(bundle)
        renamed["orchestrations"][0]["name"] = "Market brief (edited in Cloud)"
        again = (await client.post("/api/import", json=everything(renamed))).json()["results"]

        assert {o["status"] for o in first["orchestrations"]} == {"imported"}
        assert {o["status"] for o in again["orchestrations"]} == {"replaced"}
        assert {a["status"] for a in again["agents"]} == {"replaced"}
        assert {t["status"] for t in again["custom_tools"]} == {"replaced"}
        names = [o["name"] for o in await _orchestrations(client)]
        assert "Market brief (edited in Cloud)" in names and "Market brief" not in names

    async def test_only_what_was_ticked_arrives(self, client: Any) -> None:
        bundle = _load("cloud-export-v1.json")
        request = everything(bundle)
        request["selected_orchestration_ids"] = ["#0"]
        r = await client.post("/api/import", json=request)
        assert [o["name"] for o in r.json()["results"]["orchestrations"]] == ["Market brief"]

    async def test_a_cloud_connected_app_is_skipped_with_a_reason(self, client: Any) -> None:
        bundle = _load("cloud-export-v1.1.json")
        bundle["mcp_servers"] = [{"name": "google", "label": "Google", "platform": "google",
                                  "transport": "streamable_http", "url": "http://google-mcp:8021/mcp"}]
        r = await client.post("/api/import", json=everything(bundle))
        [server] = r.json()["results"]["mcp_servers"]
        assert server["status"] == "skipped"
        assert "connected app" in server["message"]
        servers = (await client.get("/api/mcp/servers")).json()
        assert "google" not in {s.get("name") for s in (servers if isinstance(servers, list) else [])}

    async def test_a_file_that_is_neither_is_refused(self, client: Any) -> None:
        r = await client.post("/api/import", json={"bundle": {"version": 2, "steps": []}})
        assert r.status_code == 400


class TestASelfHostBundleTravels:
    async def test_out_and_back_it_comes_out_the_same(self, client: Any, seed_agent: Any,
                                                       seed_orchestration: Any) -> None:
        agent = await seed_agent(id="rt_agent", tools=["rt_tool"])
        orch = await seed_orchestration(id="rt_orch")
        tool = {"name": "rt_tool", "generalName": "Round trip", "description": "echo", "tool_type": "http",
                "url": "https://example.com/{q}", "method": "GET", "headers": {"X-Key": "secret"},
                "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}
        assert (await client.post("/api/tools/custom", json=tool)).status_code == 200
        pick = {"orchestration_ids": [orch["id"]], "agent_ids": [agent["id"]],
                "custom_tool_names": ["rt_tool"]}

        first = (await client.post("/api/export", json=pick)).json()
        assert (first["synapse_export"], first["version"], first["exported_from"]) == (
            True, "1.1", "self-host")
        assert first["custom_tools"][0]["headers"] == {"X-Key": ""}, "a secret never travels"

        r = await client.post("/api/import", json=everything(first))
        assert r.status_code == 200, r.text
        second = (await client.post("/api/export", json=pick)).json()

        def same(bundle: dict[str, Any]) -> dict[str, Any]:
            out = copy.deepcopy(bundle)
            out.pop("exported_at")
            for o in out["orchestrations"]:
                o.pop("created_at", None)
                o.pop("updated_at", None)
            return out

        assert same(second) == same(first)

    @pytest.mark.parametrize("name", sorted(p.name for p in (Path(__file__).resolve().parents[2] / "examples").glob("*.bundle.json")))
    async def test_every_example_still_imports(self, client: Any, name: str) -> None:
        bundle = json.loads((Path(__file__).resolve().parents[2] / "examples" / name).read_text())
        request = everything(bundle)
        request["selected_mcp_server_names"] = []  # no server is dialled from a test
        r = await client.post("/api/import", json=request)
        assert r.status_code == 200, r.text
        assert len(r.json()["results"]["orchestrations"]) == len(bundle.get("orchestrations", []))
        # Alone on a fresh install, nothing it names is missing (F790).
        dangling = [w for w in r.json()["warnings"] if "isn't in this file or on this install" in str(w)]
        assert dangling == [], dangling
