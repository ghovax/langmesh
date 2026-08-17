"""The shared tool field: `explanation`, added by definition to every tool's schema.

``with_explanation`` is applied once, where a tool enters the runtime. It injects the
``explanation`` field — required, keyword-only, described once here — into the tool's argument
schema and wraps the tool's function so the value is received and forwarded where the function
wants it. A tool author never declares it, and a caller's own tool inherits it too, so every
tool a session runs carries the field.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable

from langchain_core.tools import BaseTool
from pydantic import Field, create_model

from langmesh.base.configuration import PromptLoader

#: Why a call is happening, in the words the person watching reads. Every tool takes one.
EXPLANATION = PromptLoader(Path(__file__).parent / "descriptions").load("explanation", {}).strip()

#: The shared field every tool carries: why this call is happening.
EXPLANATION_FIELD = (str, Field(..., description=EXPLANATION))

def _forwarding(func: Callable[..., Any], accepts_explanation: bool):
    """One wrapped function: receives `explanation` and passes the tool's own fields through."""
    is_async = inspect.iscoroutinefunction(func)

    if is_async:

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            explanation = kwargs.pop("explanation", "")
            if accepts_explanation:
                kwargs["explanation"] = explanation
            return await func(*args, **kwargs)

    else:

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            explanation = kwargs.pop("explanation", "")
            if accepts_explanation:
                kwargs["explanation"] = explanation
            return func(*args, **kwargs)

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    wrapper.__qualname__ = func.__qualname__
    wrapper.__module__ = func.__module__
    return wrapper


def with_explanation(tool: BaseTool) -> BaseTool:
    """The given tool with the shared ``explanation`` field added to its schema.

    Applied once, at the registration choke point, so built-in tools, plugin tools, and a
    caller's own tools all inherit it. The tool's own ``**kwargs`` receiver (used only to take
    the shared value) is never a schema field. A structured verdict tool that already names its
    fields is returned unchanged.
    """
    schema = tool.args_schema
    if schema is None:
        return tool
    fields = {
        name: (field.annotation, field)
        for name, field in schema.model_fields.items()
        if name not in ("kwargs", "args")
    }
    if "explanation" in fields:
        # Already carries the shared field: only the receiver leakage needs stripping.
        if len(fields) == len(schema.model_fields):
            return tool
        tool.args_schema = create_model(f"{schema.__name__}Arguments", **fields)
        return tool
    fields["explanation"] = EXPLANATION_FIELD
    tool.args_schema = create_model(f"{schema.__name__}Arguments", **fields)

    func = getattr(tool, "func", None) or getattr(tool, "coroutine", None)
    if func is None:
        return tool
    signature = inspect.signature(func)
    accepts_explanation = "explanation" in signature.parameters or any(
        parameter.kind == parameter.VAR_KEYWORD for parameter in signature.parameters.values()
    )
    wrapped = _forwarding(func, accepts_explanation)
    if inspect.iscoroutinefunction(func):
        tool.coroutine = wrapped
    else:
        tool.func = wrapped
    return tool

__all__ = ["EXPLANATION", "EXPLANATION_FIELD", "with_explanation"]
