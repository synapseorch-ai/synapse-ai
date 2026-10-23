"""
A build's code step never reaches an outside service itself, and never reads a key from the environment (F947,
SAI2-P3, D300).

Measured (SAI2-P3's after-batch, bld-09 on Claude Code Haiku): with no tool for Buffer, the planner wrote a transform
that posted to Buffer's API with `requests`, its key from `os.getenv('BUFFER_API_KEY')`, and told the person to "add
these in Settings > Variables"; the check offered Build it. Transforms run with the internet, so that step reached a
service with no tool (F489), no Connect card, no approval (TAG1) and a key that never went through the Vault. The class:
a step reaches a service through a tool, and its key through the Vault, never from code. The code is read with `ast`
(an aliased import is the same import), and the plan goes back to the planner, which then plans the tool it lacks the
normal way (find who provides it, or build without it).
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.engine import live
from core.orchestration.state import SharedState

#: bld-09's step, as Claude Code Haiku wrote it (abridged).
BUFFER = """import requests
import os

tip_text = state.generate_tip
buffer_api_key = os.getenv('BUFFER_API_KEY')
url = 'https://api.bufferapp.com/1/updates/create.json'
response = requests.post(url, json={'text': tip_text}, headers={'Authorization': f'Bearer {buffer_api_key}'})
result = response.json()
"""


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


async def _call(tool, args):
    run = OrchestrationRun(run_id="builder_code", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create"})
    await SharedState(run).checkpoint()
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    builder_access.forget_runs()
    return json.loads(raw) if isinstance(raw, str) else raw


def _plan(code):
    return {"kind": "create", "title": "Daily tip", "summary": "Posts a tip.", "steps": [
        {"id": "tip", "type": "llm", "name": "Write a tip", "does": "Writes a dental hygiene tip",
         "prompt": "Write one short dental hygiene tip."},
        {"id": "post", "type": "transform", "name": "Post it", "does": "Reshapes the tip",
         "config": {"transform_code": code}}]}


CLASS = "a step reaches a service through a tool, and its key through the Vault, never from code"


@pytest.mark.parametrize("code", [
    BUFFER,
    "import requests as r\nresult = r.get('https://api.example.com').json()",
    "from urllib.request import urlopen\nresult = urlopen('https://api.example.com').read()",
    "import http.client\nresult = 1",
    "from http.client import HTTPSConnection\nresult = 1",
    "import httpx, json\nresult = 1",
    "from aiohttp import ClientSession\nresult = 1",
    "import socket\nresult = 1",
    "import subprocess\nresult = subprocess.run(['curl', 'https://api.example.com']).returncode",
    "import os\nresult = os.environ['BUFFER_API_KEY']",
    "from os import getenv\nresult = getenv('BUFFER_API_KEY')",
    "from os import environ as e\nresult = e.get('BUFFER_API_KEY')",
    "result = __import__('requests').get('https://api.example.com').json()",
    "import importlib\nresult = importlib.import_module('requests')",
    # SAI2's review, S5: a shell, a connection or an import that only allowed modules make.
    "import os\nresult = os.system('curl https://api.example.com')",
    "import os as o\nresult = o.popen('curl https://api.example.com').read()",
    "import os\nos.execv('/usr/bin/curl', ['curl'])",
    "from os import spawnl\nresult = 1",
    "import asyncio\nresult = asyncio.open_connection('api.example.com', 443)",
    "import ssl\nresult = 1",
    "import ctypes\nresult = 1",
    "import multiprocessing\nresult = 1",
    "import pty\nresult = 1",
    "from xmlrpc.client import ServerProxy\nresult = 1",
    "import builtins\nresult = builtins.__import__('requests')",
    "result = globals()['__builtins__'].__import__('requests')",
    "import os\nresult = getattr(os, 'environ')['BUFFER_API_KEY']",
    "result = exec('import requests')",
    "result = eval('1 + 1')",
    "result = open('/proc/self/environ').read()",
])
async def test_code_that_reaches_a_service_or_reads_a_key_goes_back_to_the_planner(client, code):
    out = await _call("submit_plan", _plan(code))
    assert CLASS in out["error"] and "step post" in out["error"]


@pytest.mark.parametrize("code", [
    ("import json, re\nfrom datetime import datetime, timezone\nimport os.path\n"
     "rows = json.loads(state.tip) if isinstance(state.tip, str) else []\n"
     "result = [r for r in rows if re.match(r'^ok', str(r))]"),
    # SAI2's review, S5: what reaches nothing is kept.
    "from http import HTTPStatus\nresult = HTTPStatus.OK.phrase",
    "result = state.environ if hasattr(state, 'environ') else None",
    "import os\nresult = os.path.basename(str(state.tip))",
])
async def test_code_that_reshapes_the_runs_own_data_is_kept(client, code):
    out = await _call("submit_plan", _plan(code))
    assert out.get("finish") == "submit_plan", out


async def test_code_that_does_not_parse_is_said_as_such_and_not_as_a_service(client):
    out = await _call("submit_plan", _plan("result = [x for x in"))
    assert "does not parse" in out["error"] and CLASS not in out["error"]


async def test_a_top_level_return_is_said_with_how_a_transform_answers(client):
    """SAI2's review, S6: `wrap_transform` runs the code at module level, so `return` cannot run there."""
    out = await _call("submit_plan", _plan("result = 1\nreturn result"))
    assert "does not parse" in out["error"] and "result = ..." in out["error"]
