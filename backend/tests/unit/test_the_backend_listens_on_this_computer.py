"""The backend listens on this computer only, unless its person says otherwise (F1534, BRW5; Naveen's pick 11-11).

`synapse start` ran the backend on 0.0.0.0:8765 on every OS. Its only callers are on this computer: the frontend
(BACKEND_URL=http://127.0.0.1:<port>), the CLI's own probes, the MCP bridge, tunnels to localhost. On a shared Wi-Fi a
device could reach its API docs, Google's sign-in callback and the metrics with no token. Docker already published it
on loopback only (docker-compose.yml, GHSA-3j67-x3j8-r32x). Now:

- the backend listens on 127.0.0.1 by default, and says so when it starts;
- SYNAPSE_BACKEND_HOST, in the environment or the install's .env, is the explicit way to listen wider;
- no backend entry point listens on 0.0.0.0 by a literal (only a scale worker's health server, which a container's
  probe reaches on the pod's address);
- the Docker images listen on 0.0.0.0 inside the container, where the port publish and the compose network reach it.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent

#: backend/main.py run as every launcher runs it, with a uvicorn that prints what it was asked to listen on, and a
#: stand-in for the app (the server's import is not what is measured here).
PROBE = (
    "import json, runpy, sys, types\n"
    "fake = types.ModuleType('uvicorn')\n"
    "fake.run = lambda *a, **kw: print('RUN ' + json.dumps({'host': kw.get('host'), 'port': kw.get('port')}))\n"
    "sys.modules['uvicorn'] = fake\n"
    "server = types.ModuleType('core.server'); server.app = object(); sys.modules['core.server'] = server\n"
    f"runpy.run_path({str(BACKEND / 'main.py')!r}, run_name='__main__')\n"
)


def _start(tmp_path, *, env_host: str | None = None, dotenv: str = "") -> tuple[dict, str]:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(dotenv, encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "SYNAPSE_HOME": str(home), "PYTHONPATH": str(BACKEND),
           "SYNAPSE_BACKEND_PORT": "18765"}
    if env_host is not None:
        env["SYNAPSE_BACKEND_HOST"] = env_host
    out = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=60,
                         check=False)
    assert out.returncode == 0, out.stderr
    ran = [line for line in out.stdout.splitlines() if line.startswith("RUN ")]
    assert len(ran) == 1, out.stdout
    return json.loads(ran[0][4:]), out.stdout


def test_it_listens_on_this_computer_and_says_so(tmp_path):
    asked, said = _start(tmp_path)
    assert asked == {"host": "127.0.0.1", "port": 18765}
    assert "127.0.0.1:18765" in said and "SYNAPSE_BACKEND_HOST" in said


def test_its_person_may_open_it_to_the_network_and_it_says_so(tmp_path):
    asked, said = _start(tmp_path, env_host="0.0.0.0")
    assert asked["host"] == "0.0.0.0"
    assert "0.0.0.0:18765" in said and "other devices" in said


def test_the_installs_env_file_may_name_it(tmp_path):
    asked, _said = _start(tmp_path, dotenv="SYNAPSE_BACKEND_HOST=192.168.1.20\n")
    assert asked["host"] == "192.168.1.20"


def test_no_backend_entry_point_listens_everywhere_by_a_literal():
    allowed = {("worker_main.py", "uvicorn.run")}  # a scale worker's /health, probed on the pod's own address
    found = set()
    for path in [*BACKEND.glob("*.py"), *(BACKEND / "core").rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "host" and isinstance(kw.value, ast.Constant) and kw.value.value == "0.0.0.0":
                        found.add((str(path.relative_to(BACKEND)), ast.unparse(node.func)))
    assert found == allowed, f"listens on 0.0.0.0 by a literal: {sorted(found - allowed)}"


def test_the_docker_images_listen_where_their_published_port_reaches():
    for name in ("Dockerfile", "Dockerfile.backend"):
        text = (REPO / name).read_text("utf-8")
        assert re.search(r"^ENV\s+SYNAPSE_BACKEND_HOST=\"?0\.0\.0\.0\"?\s*$", text, re.MULTILINE), \
            f"{name} must listen on 0.0.0.0 inside its container"
