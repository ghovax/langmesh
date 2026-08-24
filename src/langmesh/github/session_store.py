"""Restore the mention session from the previous job's artifact.

``issue_comment`` runs on the default branch get a read-only Actions cache
token, so ``actions/cache`` cannot save ``.github/langmesh``. Artifacts stay
writable. This file is stdlib only so the Action can restore before the venv.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

STATE_DIRECTORY = ".github/langmesh"
SESSION_FILE = "session.sqlite"


def artifact_name(event: dict) -> str:
    issue = event.get("issue") or {}
    pull = event.get("pull_request") or {}
    if issue:
        kind = "pull" if issue.get("pull_request") else "issue"
        number = issue["number"]
    else:
        kind = "pull"
        number = pull["number"]
    return f"langmesh-session-{kind}-{number}"


def _gh(*arguments: str, output: Path | None = None) -> str:
    command = ["gh", "api", "-H", "Accept: application/vnd.github+json", *arguments]
    if output is not None:
        # Runner ``gh`` has no ``--output``; write the body (the zip) to stdout.
        with output.open("wb") as handle:
            subprocess.check_call(command, stdout=handle)
        return ""
    return subprocess.check_output(command, text=True)


def latest_artifact(repository: str, name: str) -> dict | None:
    payload = json.loads(_gh(f"/repos/{repository}/actions/artifacts?name={name}&per_page=5"))
    for artifact in payload.get("artifacts") or ():
        if artifact.get("expired"):
            continue
        if artifact.get("id"):
            return artifact
    return None


def restore(workspace: Path, event: dict, repository: str) -> bool:
    name = artifact_name(event)
    artifact = latest_artifact(repository, name)
    if artifact is None:
        print(f"session artifact {name} not found", flush=True)
        return False
    target = workspace / STATE_DIRECTORY / SESSION_FILE
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "session.zip"
        extracted = Path(temporary) / "extracted"
        extracted.mkdir()
        _gh(f"/repos/{repository}/actions/artifacts/{artifact['id']}/zip", output=archive)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(extracted)
        matches = list(extracted.rglob(SESSION_FILE))
        if not matches:
            print(f"session artifact {name} had no {SESSION_FILE}", flush=True)
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(matches[0], target)
    _describe_session(target)
    print(
        f"restored session artifact {name} id={artifact['id']} "
        f"from run {artifact.get('workflow_run', {}).get('id') or artifact.get('workflow_run_id')}",
        flush=True,
    )
    return True


def _describe_session(path: Path) -> None:
    """What the restored sqlite holds, so a miss is visible before the venv exists."""
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            checkpoints = 0
            if "langmesh_checkpoints" in tables:
                checkpoints = int(
                    connection.execute("SELECT COUNT(*) FROM langmesh_checkpoints").fetchone()[0]
                )
            traces = 0
            if "mention_trace" in tables:
                traces = int(connection.execute("SELECT COUNT(*) FROM mention_trace").fetchone()[0])
            size = path.stat().st_size
            print(
                f"session sqlite bytes={size} tables={','.join(tables) or '-'} "
                f"checkpoints={checkpoints} traces={traces}",
                flush=True,
            )
        finally:
            connection.close()
    except Exception as error:
        print(f"session sqlite could not be read: {error}", flush=True)


def main() -> None:
    if (sys.argv[1:] or [""])[0] != "restore":
        raise SystemExit("usage: session_store.py restore")
    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or os.getcwd()).resolve()
    repository = os.environ["GITHUB_REPOSITORY"]
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    restore(workspace, event, repository)


if __name__ == "__main__":
    main()
