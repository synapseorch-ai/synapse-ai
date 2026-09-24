"""v1 over HTTP: JSON calls and SSE streams, with the key a target resolved."""
from __future__ import annotations

import json
from collections.abc import Iterator

import httpx

from synapse.client.errors import ApiError, CliError
from synapse.client.target import Target

#: A stream's read timeout. Every v1 stream sends a heartbeat at least every
#: 15 s while it is quiet, so a minute of silence means the server is gone.
_STREAM_READ_TIMEOUT = 60.0


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()[:300] or resp.reason_phrase
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):  # FastAPI's validation errors
        return "; ".join(
            f"{'.'.join(str(p) for p in d.get('loc', [])[1:])}: {d.get('msg')}" for d in detail
        )
    return str(detail or body)


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
        return CliError(f"Could not reach Synapse at {self.target.origin}.{hint} ({exc.__class__.__name__})")

    def _refused(self) -> CliError:
        """The server refused a key the CLI had just minted: drop it and say why."""
        self.target.forget_key()
        return CliError(
            f"{self.target.origin} refused the key this machine's install minted, so it "
            "is reading a different database than the CLI. Pass --api-key."
        )

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in (1, 2):
            try:
                resp = self._http.request(method, self._url(path), headers=self._headers(), **kwargs)
            except httpx.TransportError as exc:
                raise self._unreachable(exc) from None
            if resp.status_code == 401 and self.target.managed and attempt == 1:
                # The CLI's own key was revoked, or belongs to another install on
                # this port: replace it once, then say so if that is refused too.
                self.target.mint_key()
                continue
            break
        if resp.status_code == 401 and self.target.managed:
            raise self._refused()
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, _detail(resp))
        return resp

    def get(self, path: str, **params):
        return self._send("GET", path, params={k: v for k, v in params.items() if v is not None}).json()

    def post(self, path: str, body: dict | None = None):
        return self._send("POST", path, json=body or {}).json()

    def patch(self, path: str, body: dict):
        return self._send("PATCH", path, json=body).json()

    def delete(self, path: str):
        return self._send("DELETE", path).json()

    def stream(self, method: str, path: str, body: dict | None = None,
               params: dict | None = None) -> Iterator[dict]:
        """An SSE route's frames, decoded. Stops when the server closes the stream."""
        timeout = httpx.Timeout(30.0, read=_STREAM_READ_TIMEOUT)
        for attempt in (1, 2):
            try:
                with self._http.stream(method, self._url(path), headers=self._headers(),
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
                    yield from parse_sse(resp.iter_lines())
                    return
            except httpx.TransportError as exc:
                raise self._unreachable(exc) from None
