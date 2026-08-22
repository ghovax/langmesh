"""Discover application-owned screen workflows and their isolated dependencies."""

from __future__ import annotations

import ast
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

PACKAGE = "workflows"
PROJECT_ROOT = ".agents"
PERSONAL_ROOT = "~/.agents"
SKILLS_ROOT = "skills"


def _agent_roots(project_directory: str = "") -> list[Path]:
    roots = [Path(project_directory).expanduser() / PROJECT_ROOT] if project_directory else []
    roots.append(Path(PERSONAL_ROOT).expanduser())
    return [root.resolve() for root in roots if root.is_dir()]


def _skill_script_roots(agent_roots: list[Path]) -> list[str]:
    found: list[str] = []
    for root in agent_roots:
        skills = root / SKILLS_ROOT
        if not skills.is_dir():
            continue
        for scripts in sorted(skills.glob("*/scripts")):
            if scripts.is_dir():
                found.append(str(scripts.resolve()))
    return found


@lru_cache(maxsize=8)
def _libraries_under(root: str) -> tuple[str, ...]:
    base = Path(root)
    found: list[str] = []
    for package in sorted(base.iterdir()) if base.is_dir() else []:
        if not package.is_dir():
            continue
        for candidate in (package, package / ".dylibs"):
            if candidate.is_dir() and (
                any(candidate.glob("*.dylib")) or any(candidate.glob("lib*.so"))
            ):
                found.append(str(candidate))
    return tuple(found)


def _summarise(
    function: ast.FunctionDef | ast.AsyncFunctionDef, module: str, scope: str
) -> dict[str, Any] | None:
    parameters = [argument.arg for argument in function.args.args]
    if not parameters or parameters[0] != "screen" or function.name.startswith("_"):
        return None
    rendered = [ast.unparse(argument) for argument in function.args.args[1:]]
    defaults = function.args.defaults
    for offset, default in enumerate(defaults[-len(rendered) :] if rendered else []):
        rendered[len(rendered) - len(defaults) + offset] += f"={ast.unparse(default)}"
    entry: dict[str, Any] = {
        "import": f"from {PACKAGE}.{module} import {function.name}",
        "call": f"{function.name}(screen{''.join(', ' + part for part in rendered)})",
        "scope": scope,
    }
    if documentation := ast.get_docstring(function):
        entry["does"] = documentation.strip().splitlines()[0]
    return entry


class FilesystemWorkflowCatalogue:
    """Resolve workflow values from the daemon user's project and personal directories."""

    def import_roots(self, project_directory: str = "") -> list[str]:
        roots = _agent_roots(project_directory)
        return [str(root) for root in roots] + _skill_script_roots(roots)

    def dependency_roots(self, project_directory: str = "") -> list[str]:
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        return [
            str(packages.resolve())
            for scripts in _skill_script_roots(_agent_roots(project_directory))
            if (packages := Path(scripts) / ".venv" / "lib" / version / "site-packages").is_dir()
        ]

    def library_roots(self, project_directory: str = "") -> list[str]:
        return [
            directory
            for root in self.dependency_roots(project_directory)
            for directory in _libraries_under(root)
        ]

    def available(self, project_directory: str = "") -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for root in _agent_roots(project_directory):
            directory = root / PACKAGE
            if not directory.is_dir():
                continue
            scope = "project" if root.name == PROJECT_ROOT and project_directory else "personal"
            for path in sorted(directory.glob("*.py")):
                if path.stem.startswith("_"):
                    continue
                if path.stem in seen:
                    listed.append(
                        {
                            "import": f"{PACKAGE}.{path.stem}",
                            "scope": scope,
                            "error": "unreachable: the project defines this module name too",
                        }
                    )
                    continue
                seen.add(path.stem)
                try:
                    tree = ast.parse(path.read_text())
                except (OSError, SyntaxError) as error:
                    listed.append(
                        {
                            "import": f"{PACKAGE}.{path.stem}",
                            "scope": scope,
                            "error": f"could not be read: {error}",
                        }
                    )
                    continue
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if entry := _summarise(node, path.stem, scope):
                            listed.append(entry)
        return listed


__all__ = ["FilesystemWorkflowCatalogue"]
