"""Post the mention acknowledgement from ``prompts/*.md``.

The Action runs this after checkout and before the venv, so this file is stdlib only.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from detect import is_mention_turn
from files import ACK_ID_NAME, write_job_file
from substitute import render_file

_PROMPTS = Path(__file__).resolve().parent / "prompts"


def run_log_url() -> str:
    server = (os.environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    repository = os.environ.get("GITHUB_REPOSITORY") or ""
    run_id = os.environ.get("GITHUB_RUN_ID") or ""
    if repository and run_id:
        return f"{server}/{repository}/actions/runs/{run_id}"
    return ""


def acknowledgement() -> str:
    return render_file(_PROMPTS / "acknowledgement.md", {})


def working_comment(text: str) -> str:
    url = run_log_url()
    body = (text or acknowledgement()).strip()
    if not url:
        return body
    return render_file(_PROMPTS / "working_comment.md", {"body": body, "url": url})


def _post(repository: str, number: int, text: str, token: str, api: str) -> int:
    request = urllib.request.Request(
        f"{api}/repos/{repository}/issues/{number}/comments",
        data=json.dumps({"body": text[:65536]}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "langmesh",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            record = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub API {error.code}: {error.read().decode()[:500]}") from error
    return int(record["id"])


def _write_output(*, start: bool, comment_id: int | None = None) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with Path(output).open("a", encoding="utf-8") as handle:
        print(f"start={'true' if start else 'false'}", file=handle)
        print(f"comment_id={comment_id or ''}", file=handle)


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    number = (event.get("issue") or {}).get("number") or (
        event.get("pull_request") or {}
    ).get("number")
    if not number:
        raise SystemExit("no issue or pull request number on this event")
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    api = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    if not is_mention_turn(event, repository=repository, token=token, api=api):
        _write_output(start=False)
        return
    comment_id = _post(
        repository,
        int(number),
        working_comment(acknowledgement()),
        token,
        api,
    )
    write_job_file(ACK_ID_NAME, str(comment_id))
    _write_output(start=True, comment_id=comment_id)


if __name__ == "__main__":
    main()
