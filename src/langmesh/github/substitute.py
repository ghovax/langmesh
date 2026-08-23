"""Stdlib ``{{ name }}`` substitution for mention comment templates.

The Action posts the acknowledgement after checkout and before the venv exists, so
this module cannot import the LangMesh package. The rules match ``PromptTemplates.render``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path


def render(template: str, variables: Mapping[str, object], template_name: str = "") -> str:
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


def render_file(path: Path, variables: Mapping[str, object]) -> str:
    return render(path.read_text(), variables, path.stem).strip()
