"""
setup.sh and setup.ps1 move past an address that drops the connection, within seconds (F1445).

Naveen's re-run of v2.0.0-rc1's installer sat at "Getting the Synapse installer…" for
minutes. GitHub's release files resolve to several addresses; one of them dropped his
connection attempts (its SYN unanswered), and curl, with no connect timeout, waited on
it for minutes before trying the next. Each download now gives an address at most
10 s (curl splits that over the addresses it has, so the next one is tried at once),
keeps its retries, and says after 10 s that it is still trying.

The setup.sh cases run the script itself, with stand-ins for curl, wget and uv where the
network would be, and once with the real curl against an address that drops the
connection, as GitHub's did.
"""
from __future__ import annotations

import hashlib
import http.server
import os
import pty
import re
import select
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SETUP_SH = ROOT / "setup.sh"
SETUP_PS1 = (ROOT / "setup.ps1").read_text(encoding="utf-8")

#: The most an address may hold a download before the next one is tried.
CONNECT_LIMIT = 10
#: PowerShell's only portable cap covers the whole request; the installer is about 66 KB.
PS_LIMIT = 30

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="setup.sh is for Linux and macOS")

TOOLS = ("bash", "sh", "uname", "mkdir", "date", "rm", "mv", "cp", "cat", "awk", "sleep", "chmod")

STAND_IN_DOWNLOAD = r"""#!/bin/sh
# A stand-in for curl or wget: records how it was called, then answers as the release would.
printf '%s\n' "$*" >> "$LOG"
for a; do url=$a; done
case "$url" in
  *slow*) sleep 12; printf 'pyz' ;;
  */install.sh) printf 'mkdir -p "$UV_UNMANAGED_INSTALL" && cp "$UV_STUB" "$UV_UNMANAGED_INSTALL/uv"\n' ;;
  *.sha256) printf '%s  synapse-install.pyz\n' "$SUM" ;;
  *) printf 'pyz' ;;
esac
"""


def _without_main() -> str:
    """setup.sh as written, less its last line (`main "$@"`), so a case can call one of its functions."""
    text = SETUP_SH.read_text(encoding="utf-8").rstrip("\n")
    head, last = text.rsplit("\n", 1)
    assert last == 'main "$@"', "setup.sh no longer ends by calling main"
    return head + "\n"


@pytest.fixture
def machine(tmp_path):
    """A PATH of only the tools setup.sh uses (no curl, wget or uv of this machine's), and a stand-in uv."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in TOOLS + ("sha256sum", "shasum"):
        found = shutil.which(name)
        if found:
            (tools / name).symlink_to(found)
    uv = tmp_path / "uv-stub"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)
    log = tmp_path / "downloads.log"
    log.touch()
    env = {"PATH": str(tools), "HOME": str(tmp_path), "SYNAPSE_HOME": str(tmp_path / "home"), "LOG": str(log),
           "UV_STUB": str(uv), "SUM": hashlib.sha256(b"pyz").hexdigest(),
           "SYNAPSE_INSTALLER_URL": "https://example.invalid/synapse-install.pyz"}
    return {"tools": tools, "env": env, "log": log}


def _stand_in(machine, name: str) -> None:
    path = machine["tools"] / name
    path.write_text(STAND_IN_DOWNLOAD)
    path.chmod(0o755)


def _limit(args: list[str], *names: str) -> float | None:
    """The connect limit a download was given, under any of `names` (`--name V` or `--name=V`)."""
    for i, a in enumerate(args):
        for name in names:
            if a == name and i + 1 < len(args):
                return float(args[i + 1])
            if a.startswith(name + "="):
                return float(a.split("=", 1)[1])
    return None


def _downloads(machine) -> list[list[str]]:
    return [line.split() for line in machine["log"].read_text().splitlines()]


def test_every_curl_download_gives_an_address_ten_seconds_and_retries(machine):
    _stand_in(machine, "curl")
    run = subprocess.run(["bash", str(SETUP_SH), "--yes"], env=machine["env"], capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    calls = _downloads(machine)
    assert len(calls) == 3, calls            # uv's install script, the installer, its checksum
    for args in calls:
        limit = _limit(args, "--connect-timeout")
        assert limit is not None and 0 < limit <= CONNECT_LIMIT, args
        assert "--retry" in args, args


def test_with_only_wget_every_download_gives_an_address_ten_seconds(machine):
    _stand_in(machine, "wget")
    run = subprocess.run(["bash", str(SETUP_SH), "--yes"], env=machine["env"], capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    calls = _downloads(machine)
    assert len(calls) == 3, calls
    for args in calls:
        limit = _limit(args, "--connect-timeout", "--timeout", "-T")
        assert limit is not None and 0 < limit <= CONNECT_LIMIT, args


def test_a_slow_download_says_once_that_it_is_still_trying(machine):
    """A download still going after 10 s says so on the terminal; one that finished in time says nothing later."""
    _stand_in(machine, "curl")
    script = _without_main() + 'fetch "https://example.invalid/fast" >/dev/null\nfetch "https://example.invalid/slow"\n'
    master, slave = pty.openpty()
    proc = subprocess.Popen(["bash", "-c", script], env=machine["env"], stdout=subprocess.PIPE, stderr=slave)
    os.close(slave)
    said = b""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.5)
        if ready:
            try:
                said += os.read(master, 4096)
            except OSError:
                break
        if proc.poll() is not None and not ready:
            break
    body = proc.stdout.read()
    proc.wait(timeout=5)
    os.close(master)
    assert body == b"pyz"
    assert said.count(b"Still getting") == 1, said


# ── the real curl, and an address that drops the connection ─────────────────

@pytest.fixture
def dead_then_live():
    """One port on two loopback addresses: 127.0.0.2 drops new connections (its accept queue is full,
    so the kernel leaves each SYN unanswered, as GitHub's dead address did) and 127.0.0.1 serves."""
    if not sys.platform.startswith("linux"):
        pytest.skip("the dropped-SYN stand-in needs Linux's loopback")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"pyz")

        def log_message(self, *a):
            pass

    live = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = live.server_address[1]
    threading.Thread(target=live.serve_forever, daemon=True).start()
    dead = socket.socket()
    held = []
    try:
        try:
            dead.bind(("127.0.0.2", port))
        except OSError:
            pytest.skip("127.0.0.2 is not usable here")
        dead.listen(0)
        for _ in range(3):                                   # fill the accept queue
            s = socket.socket()
            s.setblocking(False)
            s.connect_ex(("127.0.0.2", port))
            held.append(s)
        time.sleep(0.2)
        probe = socket.socket()
        probe.settimeout(1.5)
        try:
            probe.connect(("127.0.0.2", port))
            pytest.skip("this kernel answers a full accept queue; no dropped SYN to test against")
        except (socket.timeout, TimeoutError):
            pass
        finally:
            probe.close()
        yield port
    finally:
        for s in held:
            s.close()
        dead.close()
        live.shutdown()
        live.server_close()


def test_the_real_curl_passes_over_a_dead_address_within_seconds(dead_then_live, tmp_path):
    curl = shutil.which("curl")
    if not curl:
        pytest.skip("no curl here")
    port = dead_then_live
    # The address list curl gets for the host, dead one first, as a resolver may hand it out.
    (tmp_path / ".curlrc").write_text(f'resolve = "release.test:{port}:127.0.0.2,127.0.0.1"\n')
    script = _without_main() + f'fetch "http://release.test:{port}/synapse-install.pyz"\n'
    started = time.monotonic()
    try:
        run = subprocess.run(["bash", "-c", script], env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
                             capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        pytest.fail("the download was still waiting on the dead address after 45 s")
    took = time.monotonic() - started
    assert run.returncode == 0 and run.stdout == b"pyz", run.stderr
    assert took < 2 * CONNECT_LIMIT, f"took {took:.1f} s"


# ── setup.ps1 ────────────────────────────────────────────────────────────────

def test_every_powershell_download_has_a_time_limit():
    """PowerShell 5.1 and 7 both take -TimeoutSec; Windows gives up on a dead address by itself after about 21 s
    and .NET then tries the next, so a cap per request keeps a stalled one from holding the install."""
    code = [line.split("#", 1)[0] for line in SETUP_PS1.splitlines()]
    calls = [line for line in code if "Invoke-WebRequest" in line]
    assert len(calls) >= 3, calls             # uv's install script, the installer, its checksum
    for call in calls:
        m = re.search(r"-TimeoutSec\s+(\d+)", call)
        assert m and 0 < int(m.group(1)) <= PS_LIMIT, call
