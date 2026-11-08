"""
setup.ps1 reads the installer's published checksum as text, whatever type it is served as (F833).

GitHub serves release assets as `application/octet-stream`. Windows PowerShell then
hands back `Invoke-WebRequest`'s `.Content` as a `System.Byte[]`, and `-split` on
it made the "expected" hash `51` (the first byte, as a number): every Windows
install of 2.0 refused its own installer ("did not match its published checksum").
Found by Naveen running the local 2.0.0rc0 (REL2, D312).

The behavioural test runs the script's own function in whatever PowerShell this
machine has (`pwsh`, or Windows PowerShell from WSL), against a server that sends
the checksum as octet-stream; it skips where there is none.
"""
from __future__ import annotations

import http.server
import os
import re
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = (ROOT / "setup.ps1").read_text(encoding="utf-8")
DIGEST = "3e0faa5dd9424ab7a5592c1f7c0a8a5a0c6b1f0e2d4c3b2a1908f7e6d5c4b3a2"


def _function() -> str:
    m = re.search(r"^function Get-PublishedSha256 \{.*?^\}", SCRIPT, re.S | re.M)
    assert m, "setup.ps1 has no Get-PublishedSha256 function"
    return m.group(0)


def test_the_checksum_is_read_through_the_function_not_from_content():
    assert "Get-PublishedSha256" in SCRIPT.split("function Install-Synapse", 1)[1]
    assert not re.search(r"\.sha256\"?\)\.Content", SCRIPT), "the checksum must not be read from .Content"


def _powershell() -> list[str] | None:
    """`pwsh` where it is installed (CI's setup-scripts job). Windows PowerShell from
    WSL only when asked (SYNAPSE_TEST_WINDOWS_POWERSHELL=1): WSL's interop fails now
    and then (UtilAcceptVsock), and the suite must not fail with it."""
    if shutil.which("pwsh"):
        return ["pwsh", "-NoProfile", "-NonInteractive", "-Command"]
    if os.environ.get("SYNAPSE_TEST_WINDOWS_POWERSHELL") == "1" and shutil.which("powershell.exe"):
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    return None


@pytest.fixture
def octet_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = f"{DIGEST}  synapse-install.pyz\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/synapse-install.pyz.sha256"
    srv.shutdown()


def test_an_octet_stream_checksum_is_read_as_its_hex(octet_server):
    shell = _powershell()
    if not shell:
        pytest.skip("no PowerShell here; CI's setup-scripts job has pwsh")
    # A method call as a command's argument needs its own parentheses, or pwsh can't parse the line (F1162).
    tmp = "$env:TEMP" if shell[0] == "powershell.exe" else "([System.IO.Path]::GetTempPath())"
    script = (f"{_function()}\n"
              f"Get-PublishedSha256 -Uri '{octet_server}' -Path (Join-Path {tmp} 'f833-check.sha256')")
    out = subprocess.run([*shell, script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == DIGEST.upper()
