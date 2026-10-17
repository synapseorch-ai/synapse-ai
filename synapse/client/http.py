"""v1 over HTTP: JSON calls and SSE streams, with the key a target resolved."""
from __future__ import annotations

import json
import time
from collections.abc import Iterator

import httpx

from synapse.client import errors
from synapse.client.errors import ApiError, CliError, StreamBroken
from synapse.client.target import Target

#: A stream's read timeout. Every v1 stream sends a heartbeat at least every
#: 15 s while it is quiet, so a minute of silence means the server is gone.
_STREAM_READ_TIMEOUT = 60.0
#: The longest `Retry-After` a command waits out, once, before it gives up with
#: UNAVAILABLE (D294). Longer than this, a script is better told at once.
_MAX_RETRY_AFTER = 30.0
_BUSY = (429, 503)


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()[:300] or resp.reason_phrase
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):  # the OpenAI-shaped routes
        detail = body["error"].get("message")
    if isinstance(detail, list):  # FastAPI's validation errors
        return "; ".join(
            f"{'.'.join(str(p) for p in d.get('loc', [])[1:])}: {d.get('msg')}" for d in detail
        )
    return str(detail or body)


def _retry_after(resp: httpx.Response) -> float | None:
    """The seconds a busy server asked for, when it asked for few enough to wait."""
    try:
        seconds = float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None
    return seconds if 0 <= seconds <= _MAX_RETRY_AFTER else None


def _body(resp: httpx.Response):
    """A response's JSON, or None when it has none (204, or an empty 202)."""
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


def parse_sse(lines: Iterator[str]) -> Iterator[dict]:
    """SSE frames as `{"id": str | None, "data": <decoded JSON>}`; comments skipped."""
    def frame(data: list[str], event_id: str | None) -> dict:
        raw = "\n".join(data)
        try:
            return {"id": event_id, "data": json.loads(raw)}
        except ValueError:
            return {"id": event_id, "data": {"type": "text", "text": raw}}

    data: list[str] = []
    event_id: str | None = None
    for line in lines:
        if line == "":
            if data:
                yield frame(data, event_id)
            data, event_id = [], None
        elif line.startswith(":"):
            continue
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line.startswith("id:"):
            event_id = line[3:].strip()
    if data:
        yield frame(data, event_id)


class ApiClient:
    def __init__(self, target: Target):
        self.target = target
        self._http = httpx.Client(timeout=httpx.Timeout(30.0, read=30.0))

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict:
        if self.target.managed:
            self.target.ensure_key()
        return {"Authorization": f"Bearer {self.target.api_key}"}

    def _url(self, path: str) -> str:
        return self.target.api_base + path

    def _unreachable(self, exc: Exception) -> CliError:
        hint = " Is it running? Start it with `synapse start`." if self.target.is_local else ""
        return CliError(f"Could not reach Synapse at {self.target.origin}.{hint} ({exc.__class__.__name__})",
                        errors.UNAVAILABLE)

    def _refused(self) -> CliError:
        """The server refused a key the CLI had just minted: drop it and say why."""
        self.target.forget_key()
        return CliError(
            f"{self.target.origin} refused the key this machine's install minted, so it "
            "is reading a different database than the CLI. Pass --api-key.",
            errors.REFUSED, 401,
        )

    def send(self, method: str, path: str, *, url: str | None = None, check: bool = True,
             extra_headers: dict | None = None, **kwargs) -> httpx.Response:
        """One call, its key renewed once if the CLI's own was refused, and a short wait out once if busy."""
        minted = waited = False
        while True:
            try:
                resp = self._http.request(method, url or self._url(path),
                                          headers={**self._headers(), **(extra_headers or {})}, **kwargs)
            except httpx.TransportError as exc:
                raise self._unreachable(exc) from None
            if resp.status_code == 401 and self.target.managed and not minted:
                # The CLI's own key was revoked, or belongs to another install on
                # this port: replace it once, then say so if that is refused too.
                self.target.mint_key()
                minted = True
                continue
            if resp.status_code in _BUSY and not waited and (delay := _retry_after(resp)) is not None:
                time.sleep(delay)
                waited = True
                continue
            break
        if resp.status_code == 401 and self.target.managed:
            raise self._refused()
        if check and resp.status_code >= 400:
            raise ApiError(resp.status_code, _detail(resp))
        return resp

    def get(self, path: str, **params):
        return _body(self.send("GET", path, params={k: v for k, v in params.items() if v is not None}))

    def post(self, path: str, body: dict | None = None):
        return _body(self.send("POST", path, json=body or {}))

    def patch(self, path: str, body: dict):
        return _body(self.send("PATCH", path, json=body))

    def put(self, path: str, body: dict):
        return _body(self.send("PUT", path, json=body))

    def delete(self, path: str):
        return _body(self.send("DELETE", path))

    def put_part(self, url: str, data: bytes, headers: dict) -> httpx.Response:
        """One upload part, to the URL the server handed out, with exactly its headers and no key.

        A part URL is cloud's presigned one or OSS v1's (CCR-8): either way the
        URL and its headers are the authority, so the key is never sent there.
        """
        if url.startswith("/"):
            url = self.target.origin + url  # OSS's part URLs are paths on the same origin
        try:
            resp = self._http.put(url, content=data, headers=headers, timeout=httpx.Timeout(30.0, read=300.0))
        except httpx.TransportError as exc:
            raise self._unreachable(exc) from None
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, _detail(resp))
        return resp

    def stream(self, method: str, path: str, body: dict | None = None,
               params: dict | None = None, headers: dict | None = None) -> Iterator[dict]:
        """An SSE route's frames, decoded. Stops when the server closes the stream.

        A route that answers JSON instead (a team member took the answer while
        the run kept going) yields that body once, as `{"id": None, "data": …}`.
        A stream cut after it started raises `StreamBroken`, so a caller that
        knows where it was can read on from there.
        """
        timeout = httpx.Timeout(30.0, read=_STREAM_READ_TIMEOUT)
        started = False
        for attempt in (1, 2):
            try:
                with self._http.stream(method, self._url(path), headers={**self._headers(), **(headers or {})},
                                       json=body if method != "GET" else None,
                                       params=params, timeout=timeout) as resp:
                    if resp.status_code == 401 and self.target.managed:
                        if attempt == 2:
                            raise self._refused()
                        self.target.mint_key()
                        continue
                    if resp.status_code >= 400:
                        resp.read()
                        raise ApiError(resp.status_code, _detail(resp))
                    if resp.headers.get("content-type", "").startswith("application/json"):
                        resp.read()
                        yield {"id": None, "data": _body(resp)}
                        return
                    for frame in parse_sse(resp.iter_lines()):
                        started = True
                        yield frame
                    return
            except httpx.TransportError as exc:
                if started:
                    raise StreamBroken(f"The connection to {self.target.origin} dropped "
                                       f"({exc.__class__.__name__}).") from None
                raise self._unreachable(exc) from None
