"""Post the mention acknowledgement from ``prompts/*.md``.

The Action runs this after checkout and before the venv, so this file cannot
``import langmesh``. It loads ``PackagePromptLoader`` from source instead.
"""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from detect import is_mention_turn
from files import ACK_ID_NAME, write_job_file


def _prompt_loader():
    path = Path(__file__).resolve().parents[1] / "base" / "content" / "prompts.py"
    spec = importlib.util.spec_from_file_location("langmesh_prompt_loader", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load PackagePromptLoader from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PackagePromptLoader(Path(__file__).resolve().parent / "prompts")


_PROMPTS = _prompt_loader()


def run_log_url() -> str:
    server = (os.environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    repository = os.environ.get("GITHUB_REPOSITORY") or ""
    run_id = os.environ.get("GITHUB_RUN_ID") or ""
    if repository and run_id:
        return f"{server}/{repository}/actions/runs/{run_id}"
    return ""


def acknowledgement() -> str:
    return _PROMPTS.load("acknowledgement", {}).strip()


def working_comment(text: str) -> str:
    url = run_log_url()
    body = (text or acknowledgement()).strip()
    if not url:
        return body
    return _PROMPTS.load("working_comment", {"body": body, "url": url}).strip()


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


def _write_output(*, start: bool) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with Path(output).open("a", encoding="utf-8") as handle:
        print(f"start={'true' if start else 'false'}", file=handle)


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
    _write_output(start=True)


if __name__ == "__main__":
    main()
