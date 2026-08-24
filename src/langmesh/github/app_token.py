"""Mint a GitHub App installation token from the PEM secret file. Stdlib only.

The PEM is ``github.app.private_key`` under the XDG secrets directory or
``.github/secrets``. ``app_id`` comes from ``.github/langmesh.yaml``. No
repository secret is read. Writes ``token`` and ``app-slug`` to GITHUB_OUTPUT
when minting succeeds; writes nothing when the PEM or App id is missing.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from langmesh.github.files import secret_file
    from langmesh.github.policy import load_github_policy
except ImportError:
    from files import secret_file
    from policy import load_github_policy

PEM_NAME = "github.app.private_key"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwt(app_id: str, pem: Path) -> str:
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {"iat": now - 60, "exp": now + 540, "iss": int(app_id)},
            separators=(",", ":"),
        ).encode()
    )
    signing = f"{header}.{payload}".encode()
    signature = subprocess.check_output(
        ["openssl", "dgst", "-sha256", "-sign", str(pem), "-binary"],
        input=signing,
    )
    return f"{header}.{payload}.{_b64url(signature)}"


def _request(url: str, jwt: str, *, data: bytes | None = None) -> dict:
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "langmesh",
        },
    )
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub API {error.code}: {error.read().decode()[:500]}") from error
    return json.loads(body) if body else {}


def mint() -> tuple[str, str] | None:
    """``(token, app_slug)`` or None when the PEM or App id is absent."""
    policy = load_github_policy()
    app_id = (policy.get("app_id") or "").strip()
    pem = secret_file(PEM_NAME)
    if not app_id or pem is None:
        return None
    jwt = _jwt(app_id, pem)
    api = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    app = _request(f"{api}/app", jwt)
    slug = str(app.get("slug") or "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY") or ""
    if not repository or "/" not in repository:
        raise RuntimeError("GITHUB_REPOSITORY is missing")
    installation = _request(f"{api}/repos/{repository}/installation", jwt)
    installation_id = installation.get("id")
    if not installation_id:
        raise RuntimeError(f"no GitHub App installation on {repository}")
    token = _request(
        f"{api}/app/installations/{int(installation_id)}/access_tokens",
        jwt,
        data=b"{}",
    ).get("token")
    if not token:
        raise RuntimeError("GitHub App installation token was empty")
    return str(token), slug


def main() -> None:
    minted = mint()
    if minted is None:
        return
    token, slug = minted
    print(f"::add-mask::{token}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            print(f"token={token}", file=handle)
            if slug:
                print(f"app-slug={slug}", file=handle)


if __name__ == "__main__":
    main()
