"""
HTTP for the installer: JSON and downloads, with `curl` as the fallback.

The installer runs on a Python uv downloaded, whose OpenSSL does not always know
where the system's certificates are (macOS is the usual case), so a request that
fails certificate verification is retried through `curl`, which uses the
system's trust store. Nothing else is special: a timeout, three tries, a
User-Agent that says who is asking.
"""
from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

USER_AGENT = "synapse-installer"


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _is_cert_problem(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(exc)


def get_json(url: str, headers: dict | None = None, timeout: float = 15,
             method: str = "GET", body: dict | None = None):
    """The decoded JSON at `url`. Raises HttpError (with .status for 4xx/5xx)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HttpError(f"{url}: HTTP {exc.code}", exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        if _is_cert_problem(exc) and shutil.which("curl"):
            return _curl_json(url, headers, timeout, method, data)
        raise HttpError(f"{url}: {getattr(exc, 'reason', exc)}") from exc
    except ValueError as exc:
        raise HttpError(f"{url}: not JSON") from exc


def _curl_json(url, headers, timeout, method, data):
    cmd = ["curl", "-sS", "-L", "--max-time", str(int(timeout)), "-X", method,
           "-w", "\n%{http_code}"]
    for key, value in headers.items():
        cmd += ["-H", f"{key}: {value}"]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    out = subprocess.run(cmd + [url], input=data, capture_output=True, timeout=timeout + 5)
    if out.returncode != 0:
        raise HttpError(f"{url}: {out.stderr.decode(errors='replace').strip()}")
    text, _, status = out.stdout.decode("utf-8", errors="replace").rpartition("\n")
    if not status.isdigit() or int(status) >= 400:
        raise HttpError(f"{url}: HTTP {status}", int(status) if status.isdigit() else None)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise HttpError(f"{url}: not JSON") from exc


def get_text(url: str, timeout: float = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        if _is_cert_problem(exc) and shutil.which("curl"):
            out = subprocess.run(["curl", "-fsSL", url], capture_output=True, timeout=timeout)
            if out.returncode == 0:
                return out.stdout.decode("utf-8", errors="replace")
        raise HttpError(f"{url}: {getattr(exc, 'reason', exc)}") from exc


def download(url: str, dest: Path, progress: Callable[[float], None] | None = None,
             tries: int = 3) -> Path:
    """Fetch `url` to `dest`, reporting 0..1 when the size is known."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    last: Exception | None = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response, open(partial, "wb") as f:
                total = int(response.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress and total:
                        progress(got / total)
            partial.replace(dest)
            return dest
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            if _is_cert_problem(exc) and shutil.which("curl"):
                out = subprocess.run(["curl", "-fsSL", "-o", str(partial), url],
                                     capture_output=True, timeout=1800)
                if out.returncode == 0:
                    partial.replace(dest)
                    return dest
                last = HttpError(out.stderr.decode(errors="replace").strip())
                break
            time.sleep(2 * (attempt + 1))
    try:
        partial.unlink()
    except OSError:
        pass
    raise HttpError(f"could not download {url}: {last}")
