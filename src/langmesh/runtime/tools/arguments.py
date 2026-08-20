"""The shared tool fields: `explanation` and `access_request`, added by definition to every tool.

``with_shared_fields`` is applied once, where a tool enters the runtime. It injects the two
fields — required, keyword-only, described once here — into the tool's argument schema and
wraps the tool's function so each value is received and forwarded where the function wants
it. A tool author never declares them, and a caller's own tool inherits them too, so every
tool a session runs carries the fields.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable, cast

from langchain_core.tools import BaseTool
from pydantic import Field, create_model

from langmesh.base.configuration import PromptLoader

#: Why a call is happening, in the words the person watching reads. Every tool takes one.
EXPLANATION = PromptLoader(Path(__file__).parent / "descriptions").load("explanation", {}).strip()

#: What a call says about changing anything, and what it needs beyond confinement.
ACCESS_REQUEST = (
    PromptLoader(Path(__file__).parent / "descriptions").load("access_request", {}).strip()
)

#: The shared fields every tool carries.
EXPLANATION_FIELD = (str, Field(..., description=EXPLANATION))
ACCESS_REQUEST_FIELD = (dict[str, Any], Field(..., description=ACCESS_REQUEST))

#: The fields the wrapper strips from the caller's arguments and forwards to the tool's own signature.
_SHARED_FIELDS = ("explanation", "access_request")


def _forwarding(func: Callable[..., Any], accepted: set[str]):
    """One wrapped function: receives the shared fields and passes the tool's own fields through."""
    is_async = inspect.iscoroutinefunction(func)

    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        for name in _SHARED_FIELDS:
            if name in kwargs:
                value = kwargs.pop(name)
                if name in accepted:
                    kwargs[name] = value
        return await func(*args, **kwargs)

    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        for name in _SHARED_FIELDS:
            if name in kwargs:
                value = kwargs.pop(name)
                if name in accepted:
                    kwargs[name] = value
        return func(*args, **kwargs)

    wrapper = async_wrapper if is_async else sync_wrapper
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    wrapper.__qualname__ = func.__qualname__
    wrapper.__module__ = func.__module__
    return wrapper


def with_shared_fields(tool: BaseTool) -> BaseTool:
    """The given tool with the shared ``explanation`` and ``access_request`` fields added.

    Applied once, at the registration choke point, so built-in tools, plugin tools, and a
    caller's own tools all inherit them. The tool's own ``**kwargs`` receiver (used only to
    take the shared values) is never a schema field. A structured verdict tool that already
    names its fields is returned unchanged.
    """
    schema: Any = tool.args_schema
    if schema is None:
        return tool
    fields = {
        name: (field.annotation, field)
        for name, field in schema.model_fields.items()
        if name not in ("kwargs", "args")
    }
    if all(name in fields for name in _SHARED_FIELDS):
        # Already carries the shared fields: only the receiver leakage needs stripping.
        if len(fields) == len(schema.model_fields):
            return tool
        tool.args_schema = create_model(f"{schema.__name__}Arguments", **cast(Any, fields))
        return tool
    for name, field in (
        ("explanation", EXPLANATION_FIELD),
        ("access_request", ACCESS_REQUEST_FIELD),
    ):
        # A tool that names the field keeps it, but the shared description replaces any placeholder.
        fields[name] = field
    tool.args_schema = create_model(f"{schema.__name__}Arguments", **cast(Any, fields))

    func = getattr(tool, "func", None) or getattr(tool, "coroutine", None)
    if func is None:
        return tool
    signature = inspect.signature(func)
    parameters = signature.parameters.values()
    has_var_keyword = any(parameter.kind == parameter.VAR_KEYWORD for parameter in parameters)
    accepted = {name for name in _SHARED_FIELDS if name in signature.parameters or has_var_keyword}
    wrapped = _forwarding(func, accepted)
    if inspect.iscoroutinefunction(func):
        tool.coroutine = wrapped
    else:
        tool.func = wrapped
    return tool


__all__ = [
    "ACCESS_REQUEST",
    "ACCESS_REQUEST_FIELD",
    "EXPLANATION",
    "EXPLANATION_FIELD",
    "with_shared_fields",
]
