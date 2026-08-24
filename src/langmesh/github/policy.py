"""Committed GitHub mention policy. Stdlib only: the ack step runs before the venv.

The file is ``.github/langmesh.yaml`` next to the workflow. It names the agent
profile (provider and model live in that profile), an optional mention handle,
and an optional GitHub App id. Secrets are not in this file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

POLICY_FILENAME = "langmesh.yaml"
DEFAULT_AGENT = "github"


def policy_path(workspace: str | Path | None = None) -> Path:
    root = Path(workspace or os.environ.get("GITHUB_WORKSPACE") or os.getcwd()).resolve()
    return root / ".github" / POLICY_FILENAME


def parse_scalar_yaml(text: str) -> dict[str, str]:
    """Top-level ``key: value`` pairs. Nested documents are out of scope for this file."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        name = key.strip()
        if not name or name.startswith("-") or " " in name:
            continue
        value = rest.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def load_github_policy(
    workspace: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Agent name, optional mention handle, optional App id. Missing file yields defaults."""
    del environ  # reserved so callers can pass os.environ without a second code path
    path = policy_path(workspace)
    values: dict[str, str] = {"agent": DEFAULT_AGENT}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    parsed = parse_scalar_yaml(text)
    agent = (parsed.get("agent") or "").strip()
    if agent:
        values["agent"] = agent
    mention = (parsed.get("mention") or "").strip()
    if mention:
        values["mention"] = mention
    app_id = (parsed.get("app_id") or "").strip()
    if app_id:
        values["app_id"] = app_id
    return values


if __name__ == "__main__":
    import json
    import sys

    policy = load_github_policy()
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            for key, value in policy.items():
                print(f"{key}={value}", file=handle)
    json.dump(policy, sys.stdout)
    sys.stdout.write("\n")
