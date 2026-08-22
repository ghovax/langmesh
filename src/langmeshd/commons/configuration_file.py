"""Reading and writing the configuration file, addressed by dotted path.

The daemon owns the file: the library's Configuration is a pure model and never touches
YAML. The schema it validates against stays in the library.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from langmeshd.commons.atomic_file import write_text
from langmeshd.commons.paths import configuration_file_path
from langmesh.base.configuration import Configuration


APP_SECTION_MODELS = {
    "composio": "ComposioConfiguration",
    "daemon": "DaemonConfiguration",
    "dictation": "DictationConfiguration",
}


def library_document(data: dict) -> dict:
    """Return only the sections owned by the library configuration model."""
    return {name: value for name, value in data.items() if name in Configuration.model_fields}


def load() -> dict:
    """The configuration file as a plain document, or ``{}`` when there is none yet."""
    path = configuration_file_path()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as error:
        raise RuntimeError(f"{path} is not valid YAML: {error}") from error


def save(data: dict) -> None:
    """Atomically persist the document in the order it holds."""
    write_text(configuration_file_path(), yaml.safe_dump(data, sort_keys=False))


def seed(text: str) -> None:
    """Atomically persist the packaged first-run document without discarding its comments."""
    write_text(configuration_file_path(), text)


def flatten(data: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every leaf as a dotted path, so settings can be addressed the way they are written."""
    if isinstance(data, dict):
        entries: list[tuple[str, Any]] = []
        for key, value in data.items():
            entries.extend(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return entries
    return [(prefix, data)]


def read(data: dict, path: str) -> Any:
    """The value at a dotted path, raising when the document holds none."""
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(path)
        node = node[part]
    return node


def write(data: dict, path: str, value: Any) -> None:
    """Set a dotted path, creating the objects above it. Mutates ``data`` in place."""
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        existing = node.get(part)
        if not isinstance(existing, dict):
            existing = {}
            node[part] = existing
        node = existing
    node[parts[-1]] = value


def remove(data: dict, path: str) -> bool:
    """Drop a dotted path and every object above it the drop left empty, answering whether anything was there."""
    parts = path.split(".")
    trail: list[tuple[dict, str]] = []
    node: Any = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        trail.append((node, part))
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, key in reversed(trail):
        if parent[key] == {}:
            del parent[key]
    return True


def parse(raw: str) -> Any:
    """Interpret a value the way the file would hold it, so `true` lands as a boolean rather than a string."""
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "~"}:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def rejects(data: dict) -> str:
    """Why this document would not load, asked before the file is written because it is read at startup."""
    from pydantic import ValidationError

    from langmeshd.commons import configuration as app_configuration

    unknown = set(data) - set(Configuration.model_fields) - set(APP_SECTION_MODELS)
    if unknown:
        names = ", ".join(sorted(unknown))
        return f"unknown top-level configuration section: {names}"
    validations = [("", Configuration, library_document(data))]
    validations.extend(
        (section, getattr(app_configuration, model_name), data.get(section) or {})
        for section, model_name in APP_SECTION_MODELS.items()
    )
    for section, model, value in validations:
        try:
            model.model_validate(value)
        except ValidationError as error:
            messages = [
                f"{'.'.join(filter(None, (section, *(str(part) for part in entry.get('loc', ())))))}: {entry.get('msg', '')}"
                for entry in error.errors()
            ]
            return "; ".join(messages) or str(error).splitlines()[0]
        except Exception as error:  # noqa: BLE001 — any other validation failure is reported.
            return str(error).splitlines()[0] or str(error)
    return ""


__all__ = [
    "flatten",
    "library_document",
    "load",
    "parse",
    "read",
    "rejects",
    "remove",
    "save",
    "seed",
    "write",
]
