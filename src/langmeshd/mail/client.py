"""The daemon as this process sees it: unix-socket RPC and an attach stream, with the capability token."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Optional

import httpx

from langmeshd.daemon.paths import daemon_socket_path, daemon_token_path


class MailDaemonError(RuntimeError):
    """The mail client could not complete a control-plane call."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def _token() -> str:
    try:
        return daemon_token_path().read_text().strip()
    except OSError as error:
        raise MailDaemonError("The daemon token is not available.") from error


def _socket() -> str:
    path = daemon_socket_path()
    if not path.exists():
        raise MailDaemonError("The daemon socket is not available.")
    return str(path)


def _authorization() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def connect() -> httpx.AsyncClient:
    """An HTTP client on the daemon's unix socket. The URL host is a placeholder the transport ignores."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=_socket()),
        base_url="http://daemon",
        headers=_authorization(),
        timeout=httpx.Timeout(60.0, read=None),
    )


async def health(client: httpx.AsyncClient) -> bool:
    """Whether this client is talking to a daemon that is actually accepting calls."""
    try:
        response = await client.get("/health", timeout=2.0)
    except (httpx.HTTPError, OSError):
        return False
    return response.status_code == 200


async def rpc(client: httpx.AsyncClient, method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await client.post("/rpc", json={"method": method, "params": params})
    except (httpx.HTTPError, OSError) as error:
        raise MailDaemonError(f"{method} could not reach the daemon.") from error
    try:
        payload = response.json()
    except ValueError as error:
        raise MailDaemonError(f"{method} returned non-JSON ({response.status_code}).") from error
    if not isinstance(payload, dict):
        raise MailDaemonError(f"{method} returned a non-object.")
    if response.status_code >= 400 or "error" in payload:
        reported = payload.get("error")
        error = reported if isinstance(reported, dict) else {}
        message = str(error.get("message") or payload) or f"{method} failed"
        raise MailDaemonError(message, code=str(error.get("code") or ""))
    result = payload.get("result")
    if not isinstance(result, dict):
        raise MailDaemonError(f"{method} returned no result object.")
    return result


async def iter_attach_frames(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Parse the daemon's attach stream. Frames are JSON on `data:` lines; comments are ignored."""
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if not buffer:
                continue
            payload = "\n".join(buffer)
            buffer = []
            try:
                frame = json.loads(payload)
            except ValueError:
                continue
            if isinstance(frame, dict):
                yield frame
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())


async def attach(client: httpx.AsyncClient, session_id: str) -> httpx.Response:
    request = client.build_request("GET", f"/sessions/{session_id}/attach")
    return await client.send(request, stream=True)


def _text_from_part(part: Any) -> str:
    if not isinstance(part, dict):
        return ""
    if part.get("kind") == "text":
        return str(part.get("text") or "")
    return ""


def consume_frame(frame: dict[str, Any], chunks: list[str]) -> Optional[bool]:
    """Fold one attach frame into `chunks`. True means the turn is running, False it ended, None neither.

    Only `turn` frames decide liveness. A snapshot of an idle session arrives before send and is not an end.
    """
    kind = str(frame.get("kind") or "")
    if kind == "delta" and frame.get("channel") == "text":
        text = str(frame.get("text") or "")
        if text:
            chunks.append(text)
        return None
    if kind == "live":
        text = _text_from_part(frame.get("part"))
        if text:
            chunks.append(text)
        return None
    if kind == "turn":
        return bool(frame.get("running"))
    return None


async def wait_ready(frames: AsyncIterator[dict[str, Any]]) -> dict[str, Any] | None:
    """The attach handshake: the first `ready` frame, after which a send cannot miss the turn."""
    async for frame in frames:
        if frame.get("kind") == "ready":
            return frame
    return None
