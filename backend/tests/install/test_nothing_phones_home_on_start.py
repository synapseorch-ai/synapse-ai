"""
Synapse sends nothing home, and neither do the libraries it ships (F1567).

SHN1 timed a fresh 2.0.8 install (11-13) and found a connection to `cocoindex.gateway.scarf.sh` at every backend
start: cocoindex's Rust engine sends usage tracking when the package is imported, unless
`COCOINDEX_DISABLE_USAGE_TRACKING` is set, and the backend's start imports it (`services/code_indexer.py`, from the
lifespan's `init_cocoindex`). The site, the README and the FAQ say Synapse has no telemetry.

The class is a library that phones home unless told not to. Each one Synapse ships has a switch, and every process
Synapse starts sets it before the library loads, whichever way it was installed (pip, the installer and npm install
the same wheel; Docker runs the same `main.py` and `worker_main.py`):

* cocoindex: `COCOINDEX_DISABLE_USAGE_TRACKING` (Scarf, at import);
* Chroma: `ANONYMIZED_TELEMETRY`, for every client, not only the one `core/memory.py` makes (PostHog in the 0.x
  releases `chromadb>=0.5` still allows);
* Hugging Face Hub: `HF_HUB_DISABLE_TELEMETRY`, read by transformers too (Knowledge's Docling models);
* `DO_NOT_TRACK`, the shared switch, which the Hub reads as well and the tools Synapse starts may;
* Next.js: `NEXT_TELEMETRY_DISABLED`, for the web wherever Next runs on a person's machine (`next build` in a
  Docker build or a checkout, `npm run dev` and `npm start` in a checkout); the packaged web server sends none.

What is checked is what each library reads, in a fresh process, with none of the switches set beforehand.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
BACKEND = REPO / "backend"

SWITCHES = ("COCOINDEX_DISABLE_USAGE_TRACKING", "ANONYMIZED_TELEMETRY", "HF_HUB_DISABLE_TELEMETRY",
            "DO_NOT_TRACK", "NEXT_TELEMETRY_DISABLED")
_PROXIES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")

#: What each process imports first: the backend (`python main.py`), the worker's job runner (`worker_main.main`
#: loads it), the one module that imports cocoindex (the backend's start and the code-search tool server load it),
#: and the CLI, which starts the backend, the web server and its own checks.
ENTRY_POINTS = ("main", "core.scale.worker", "services.code_indexer", "synapse.cli")

_REPORT = """
import importlib, json, os, sys
importlib.import_module(sys.argv[1])
seen = {k: os.environ.get(k) for k in %r}
try:
    from chromadb.config import Settings
    seen["chroma"] = Settings().anonymized_telemetry
except ImportError:
    seen["chroma"] = None
try:
    from huggingface_hub import constants
    seen["hf_hub"] = constants.HF_HUB_DISABLE_TELEMETRY
except ImportError:
    seen["hf_hub"] = None
print("REPORT " + json.dumps(seen))
""" % (SWITCHES,)


def _fresh_env(tmp_path: Path) -> dict:
    """This process's (isolated) environment, without a single switch or proxy: the test process has itself
    imported the backend, so whatever it set would otherwise reach the child and prove nothing."""
    env = {k: v for k, v in os.environ.items() if k not in SWITCHES and k not in _PROXIES
           and k.upper() not in ("NO_PROXY",)}
    env["PYTHONPATH"] = os.pathsep.join([str(BACKEND), str(REPO)])
    env["SYNAPSE_HOME"] = str(tmp_path / "home")
    # Should anything still phone home, it is refused here and reaches nobody.
    for name in _PROXIES:
        env[name] = "http://127.0.0.1:9"
    return env


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_each_entry_point_turns_library_tracking_off(tmp_path, entry):
    out = subprocess.run([sys.executable, "-c", _REPORT, entry], cwd=tmp_path, env=_fresh_env(tmp_path),
                         capture_output=True, text=True, timeout=180)
    line = next((x for x in out.stdout.splitlines() if x.startswith("REPORT ")), None)
    assert line, f"importing {entry} failed:\n{out.stderr[-2000:]}"
    seen = json.loads(line.removeprefix("REPORT "))

    assert {k: seen[k] for k in SWITCHES} == {
        "COCOINDEX_DISABLE_USAGE_TRACKING": "true", "ANONYMIZED_TELEMETRY": "False",
        "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "NEXT_TELEMETRY_DISABLED": "1"}, entry
    # And what the libraries themselves read from it (None: not installed here).
    assert seen["chroma"] in (False, None), (entry, seen)
    assert seen["hf_hub"] in (True, None), (entry, seen)


class _Proxy:
    """A local HTTP proxy that only listens: it keeps the first line of every request and answers nothing, so a
    library that phones home is seen here and reaches nobody."""

    def __init__(self):
        self.seen: list[str] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            conn.settimeout(2)
            try:
                first = conn.recv(4096).split(b"\r\n", 1)[0].decode("latin-1")
            except OSError:
                first = "(no request line)"
            self.seen.append(first)
            conn.close()

    def close(self):
        self._stop = True
        self._thread.join(timeout=2)
        self._sock.close()


@pytest.mark.skipif(importlib.util.find_spec("cocoindex") is None, reason="cocoindex is not installed")
def test_the_backends_start_opens_no_connection_of_its_own(tmp_path):
    """The backend's start, as far as its imports go: `main`, then the code indexer its lifespan loads. Every proxy
    variable points at a local listener, which is where cocoindex's engine went (`CONNECT
    cocoindex.gateway.scarf.sh:443`, about a second after the import, on 2.0.8)."""
    proxy = _Proxy()
    try:
        env = _fresh_env(tmp_path)
        for name in _PROXIES:
            env[name] = f"http://127.0.0.1:{proxy.port}"
        out = subprocess.run(
            [sys.executable, "-c", "import time, main, services.code_indexer; time.sleep(4)"],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
        assert out.returncode == 0, out.stderr[-2000:]
        time.sleep(0.5)
    finally:
        proxy.close()
    assert proxy.seen == [], proxy.seen


def test_the_web_turns_nextjs_telemetry_off_where_next_runs():
    """`next build` (a Docker build, the wheel's build, a checkout) reads next.config.ts before it makes its
    telemetry; `npm run dev` and `npm start` start Next through scripts/load-env.js."""
    config = (REPO / "frontend" / "next.config.ts").read_text(encoding="utf-8")
    loader = (REPO / "frontend" / "scripts" / "load-env.js").read_text(encoding="utf-8")
    for name, text in (("next.config.ts", config), ("load-env.js", loader)):
        assert "NEXT_TELEMETRY_DISABLED" in text, name
    assert loader.index("NEXT_TELEMETRY_DISABLED") < loader.index("spawnSync("), "set before Next starts"
