"""Prompt templates supplied as values or immutable package resources."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any, Callable, Optional

from langmesh.base.persistence.file_cache import parsed_file
from langmesh.base.primitives.serialization import content_address


class PromptTemplates:
    """Renders caller-supplied prompt templates without accessing external state."""

    def __init__(
        self,
        templates: Mapping[str, str],
        *,
        overrides: Optional[Callable[[str], Optional[str]]] = None,
        fallback: Optional[Any] = None,
    ) -> None:
        self._templates = dict(templates)
        self._overrides = overrides
        self._fallback = fallback

    def load(self, template_name: str, variables: Mapping[str, object]) -> str:
        if self._overrides is not None:
            override = self._overrides(template_name)
            if override is not None:
                return self.render(override, variables, template_name)
        template = self._templates.get(template_name)
        if template is None:
            return self._fallback.load(template_name, variables) if self._fallback else ""
        return self.render(template, variables, template_name)

    def revision(self) -> str:
        """Return a content identity for the templates and fallback this loader owns."""
        fallback_revision = getattr(self._fallback, "revision", None)
        return content_address(
            {
                "templates": self._templates,
                "fallback": fallback_revision() if callable(fallback_revision) else "",
            }
        )

    @classmethod
    def render(cls, template: str, variables: Mapping[str, object], template_name: str = "") -> str:
        """Render one template with strict ``{{ name }}`` substitution."""
        where = f" in prompt '{template_name}'" if template_name else ""
        placeholder = re.compile(r"\{\{\s*(\w+)\s*\}\}")
        unsupported = re.search(r"\{[%#]|[%#]\}", template)
        if unsupported is not None:
            raise ValueError(
                f"Unsupported template directive {unsupported.group(0)!r}{where}; prompts support only '{{{{ name }}}}' placeholders."
            )

        def drop_if_empty(match: re.Match[str]) -> str:
            name = match.group(1)
            supplied = variables.get(name)
            return "" if supplied is not None and not str(supplied).strip() else match.group(0)

        own_line = r"^[ \t]*\{\{\s*(\w+)\s*\}\}[ \t]*"
        template = re.sub(own_line + r"\n(?:[ \t]*\n)?", drop_if_empty, template, flags=re.M)
        sections = set(re.findall(own_line + r"$", template, flags=re.M))
        variables = {
            name: str(value).strip() if name in sections else value
            for name, value in variables.items()
        }
        malformed = re.search(r"\{\{.*?\}\}", placeholder.sub("", template), re.DOTALL)
        if malformed is not None:
            raise ValueError(f"Malformed placeholder {malformed.group(0)!r}{where}.")

        def replace(match: re.Match[str]) -> str:
            variable_name = match.group(1)
            if variable_name not in variables:
                raise ValueError(
                    f"Unresolved placeholder '{{{{ {variable_name} }}}}'{where}: no value was provided (given: {sorted(variables)})."
                )
            return str(variables[variable_name])

        return placeholder.sub(replace, template)


class PackagePromptLoader(PromptTemplates):
    """Loads immutable prompt files shipped inside the LangMesh package."""

    def __init__(
        self,
        prompts_directory: str | Path,
        extension: str = "md",
        *,
        overrides: Optional[Callable[[str], Optional[str]]] = None,
        fallback: Optional[Any] = None,
    ) -> None:
        self._directory = Path(prompts_directory)
        package_root = Path(__file__).resolve().parents[2]
        try:
            self._directory.resolve().relative_to(package_root)
        except ValueError as error:
            raise ValueError(
                "PackagePromptLoader accepts only shipped LangMesh resources."
            ) from error
        self._extension = extension
        super().__init__({}, overrides=overrides, fallback=fallback)

    def load(self, template_name: str, variables: Mapping[str, object]) -> str:
        if self._overrides is not None:
            override = self._overrides(template_name)
            if override is not None:
                return self.render(override, variables, template_name)
        path = self._directory / f"{template_name}.{self._extension}"
        template = parsed_file(path, lambda each: each.read_text())
        if template is None:
            return self._fallback.load(template_name, variables) if self._fallback else ""
        return self.render(template, variables, template_name)

    def revision(self) -> str:
        """Return a content identity for shipped templates without depending on their paths."""
        templates = {
            path.stem: parsed_file(path, lambda each: each.read_text()) or ""
            for path in sorted(self._directory.glob(f"*.{self._extension}"))
            if path.is_file()
        }
        fallback_revision = getattr(self._fallback, "revision", None)
        return content_address(
            {
                "templates": templates,
                "fallback": fallback_revision() if callable(fallback_revision) else "",
            }
        )


__all__ = ["PackagePromptLoader", "PromptTemplates"]
