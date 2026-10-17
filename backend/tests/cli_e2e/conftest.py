"""A scratch Synapse, as its own process, for the CLI's end-to-end suite (CLI1, D294).

The suite drives `python -m synapse …` as subprocesses against a real backend process (uvicorn on
`core.server:app`, lifespan and all) with a model that answers instantly (`fake_model.py`). Nothing is shared with
the machine it runs on:

- every variable either process reads is set here, from nothing (`env -i`): its own SQLite store, blobs, state,
  scratch and `SYNAPSE_HOME` under this session's tmp dir, its own internal token and JWT secret;
- the backend runs from `backend/` on `core.server:app`, so no `.env` is read (`main.py` would read the repo's);
- the CLI's config is its own (`SYNAPSE_CLI_CONFIG`), so `~/.synapse/cli.json` is never read or written;
- the session refuses to start unless the store is under the tmp dir, and fails if this checkout's store or a
  Synapse installed for this user (`~/.local/share/SynapseAI`) changed while it ran.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
REPO = BACKEND.parent
TOKEN = "cli-e2e-internal-token"
#: Stores this machine's people use: a session that changes any of them failed, whatever its tests said.
_GUARDED = (REPO / "backend" / "var" / "synapse.db", Path.home() / ".local" / "share" / "SynapseAI")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fingerprint(path: Path) -> str | None:
    """What a guarded store is now: a file's hash, a folder's names, sizes and times."""
    if not path.exists():
        return None
    if path.is_file():
        return hashlib.md5(path.read_bytes()).hexdigest()
    rows = sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in path.rglob("*") if p.is_file())
    return hashlib.md5(json.dumps(rows).encode()).hexdigest()


def _wait_until(check, seconds: float, what: str, log: Path | None = None) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.25)
    tail = log.read_text(errors="replace")[-3000:] if log and log.exists() else ""
    raise RuntimeError(f"{what} did not come up in {seconds:.0f}s.\n{tail}")


@dataclass
class Result:
    code: int
    out: str
    err: str

    def json(self):
        return json.loads(self.out)


@dataclass
class Scratch:
    root: Path
    url: str
    port: int
    env: dict
    key: str = ""
    log: Path = field(default=None)

    def cli(self, *argv: str, key: bool = True, url: bool = True, stdin: str | None = None,
            env: dict | None = None, timeout: float = 120) -> Result:
        """`synapse ARGV…` as a person runs it, in its own process; the scratch's URL and key unless told not."""
        args = [sys.executable, "-m", "synapse", *argv]
        if url:
            args += ["--url", self.url]
        if key:
            args += ["--api-key", self.key]
        done = subprocess.run(args, cwd=REPO, env={**self.env, **(env or {})}, input=stdin, capture_output=True,
                              text=True, timeout=timeout)
        return Result(done.returncode, done.stdout, done.stderr)

    def internal(self, method: str, path: str, body: dict | None = None):
        """The app's own routes, for setting the scratch up (never what a test checks the CLI with)."""
        resp = httpx.request(method, self.url + path, json=body, headers={"X-Synapse-Internal": TOKEN}, timeout=30)
        resp.raise_for_status()
        return resp.json()


@pytest.fixture(scope="session")
def scratch(tmp_path_factory):
    root = tmp_path_factory.mktemp("cli_e2e")
    assert str(root).startswith(tempfile.gettempdir()), f"the scratch must live under the tmp dir, not {root}"
    before = {p: _fingerprint(p) for p in _GUARDED}
    db = root / "var" / "synapse.db"
    db.parent.mkdir(parents=True)
    port, model_port = _free_port(), _free_port()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root / "home"), "LANG": "C.UTF-8",
        "SYNAPSE_HOME": str(root), "SYNAPSE_DB_URL": f"sqlite+aiosqlite:///{db}",
        "SYNAPSE_BLOB_DIR": str(root / "blobs"), "SYNAPSE_STATE_DIR": str(root / "state"),
        "SYNAPSE_SCRATCH_DIR": str(root / "scratch"), "SYNAPSE_INTERNAL_TOKEN": TOKEN,
        "SYNAPSE_JWT_SECRET": "cli-e2e-jwt-secret-long-enough-for-hs256", "SYNAPSE_BACKEND_PORT": str(port),
        "SYNAPSE_CLI_CONFIG": str(root / "home" / "cli.json"), "OLLAMA_BASE_URL": "http://127.0.0.1:9",
        "SYNAPSE_TELEMETRY": "off", "PYTHONUNBUFFERED": "1",
    }
    assert env["SYNAPSE_DB_URL"].endswith(str(db)) and str(db).startswith(str(root))
    (root / "home").mkdir()
    log = root / "backend.log"
    model = subprocess.Popen([sys.executable, str(HERE / "fake_model.py"), str(model_port)], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    backend = subprocess.Popen([sys.executable, "-m", "uvicorn", "core.server:app", "--host", "127.0.0.1",
                                "--port", str(port), "--log-level", "warning"],
                               cwd=BACKEND, env=env, stdout=log.open("w"), stderr=subprocess.STDOUT)
    s = Scratch(root=root, url=f"http://127.0.0.1:{port}", port=port, env=env, log=log)
    try:
        _wait_until(lambda: httpx.get(f"http://127.0.0.1:{model_port}/v1/models", timeout=2).status_code == 200,
                    15, "The fake model")
        _wait_until(lambda: httpx.get(s.url + "/api/v1/agents", timeout=2).status_code == 401, 90,
                    "The scratch backend", log)
        s.internal("POST", "/api/settings", {"model": "oaic.fake",
                                             "openai_compatible_base_url": f"http://127.0.0.1:{model_port}"})
        made = s.cli("api-keys", "generate", "cli-e2e", key=False, url=False)
        assert made.code == 0, made.err
        s.key = next(w for w in made.out.split() if w.startswith("sk-syn-"))
        yield s
    finally:
        for proc in (backend, model):
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        after = {p: _fingerprint(p) for p in _GUARDED}
        changed = [str(p) for p in _GUARDED if before[p] != after[p]]
        assert not changed, f"the end-to-end suite changed a store it must never touch: {changed}"
