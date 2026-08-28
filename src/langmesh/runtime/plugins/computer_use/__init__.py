"""The computer-use plugin: driving the screen through the control_screen tool.

The library core never names computer use: the tool, the screen context, and the guidance are
this plugin's, contributed through the feature seam when the host composes it. A bare library
embedding has no screen-control surface at all.
"""

from __future__ import annotations

import asyncio
import logging
import re
import statistics
import time
from pathlib import Path
from typing import Any

from langchain.tools import tool

from langmesh.base.content.prompts import PackagePromptLoader
from langmesh.base.primitives.limits import current_limits
from langmesh.base.primitives.serialization import compact
from langmesh.computer import (
    control,
    engine as native_surface,
    retrieval,
    surface as surface_module,
    targets as target_registry,
    web as web_surface,
)
from langmesh.computer.retrieval import retrieval_policy_from, set_retrieval_policy
from langmesh.computer.surface import message_loader
from langmesh.runtime.features import Feature
from langmesh.runtime.plugins.permissions import MUTATING_SCREEN_PRIMITIVES
from langmesh.runtime.plugins.computer_use.configuration import ComputerControlConfiguration
from langmesh.runtime.tools import context as tool_context
from langmesh.runtime.background import current_tool_call_id
from langmesh.runtime.tools.execution import current_tool_decision, current_tool_services

logger = logging.getLogger(__name__)

#: The tool's model-facing description, read from this plugin's own prompts directory.
_DESCRIPTIONS = PackagePromptLoader(Path(__file__).parent / "prompts")

# The queries this plugin has already asked the screen, so it can tell a rephrasing from a fresh
# question. The plugin owns its own history; the core never carries a screen-control concept.
_asked_queries: list[tuple[Any, str]] = []

#: Live control-screen children keyed by tool-call id.
_live_control: dict[str, Any] = {}


def terminate_control_call(tool_call_id: str) -> bool:
    """Kill the screen-control child still running for this call."""
    process = _live_control.get(tool_call_id)
    if process is None or process.returncode is not None:
        return False
    try:
        process.kill()
    except ProcessLookupError:
        return False
    except Exception:
        try:
            process.terminate()
        except ProcessLookupError:
            return False
    return True


# What an element id looks like on both surfaces, so one can be told from a description of an element.
_ELEMENT_ID = re.compile(r"(?:f\d+)?e\d+|req\d+|ws\d+|\d+(?:\.\d+)+")


def _workflow_catalogue(services: Any) -> Any:
    bundle = services.plugin_services
    return bundle.get("workflows") if isinstance(bundle, dict) else None


def _plugin_service(services: Any, name: str) -> Any:
    bundle = services.plugin_services
    return bundle.get(name) if isinstance(bundle, dict) else None


def _surface_for(surface_name: str):
    """The live surface a screen tool names: the native macOS tree, or the user's Chrome."""
    return native_surface.SURFACE if surface_name == "computer" else web_surface.SURFACE


@tool
async def control_screen(
    *,
    script: str,
    target: str = "",
) -> str:
    """Drive the screen; described in descriptions/control_screen.md."""
    services = current_tool_services()
    script = str(script)
    if not script.strip():
        return compact({"ok": False, "error": "control_screen needs a script to run."})
    target_id = str(target or "").strip()
    if not target_id:
        return compact(
            {
                "ok": False,
                "error": "control_screen needs a target — the window or tab to act in.",
                "targets": {"current": target_registry.describe_all()},
            }
        )
    target_obj = target_registry.find_target(target_id)
    if target_obj is None:
        listing = target_registry.list_targets()
        same_app = [place for place in listing if place.app.lower() == target_id.strip().lower()]
        if same_app:
            described = target_registry.describe_all(
                sorted(same_app, key=target_registry._worth_naming)
            )
            error = f"{target_id!r} is an application, not a window — an application has no single place to act in. Its windows are listed under 'candidates', likeliest first."
            payload = {"ok": False, "error": error, "targets": {"candidates": described}}
        else:
            error = f"Target {target_id!r} is not among the windows and tabs I can see."
            payload = {
                "ok": False,
                "error": error,
                "targets": {
                    "missing": [target_id],
                    "current": target_registry.describe_all(listing),
                },
            }
        return compact(payload)
    surface_name = target_obj.surface
    surface = _surface_for(surface_name)
    gate = surface.preflight("documents")
    if gate is not None:
        return compact(gate)

    control_message = message_loader("control")
    permitted_primitives = set(surface.primitives())
    decision_value = current_tool_decision()
    if decision_value is None or not decision_value.screen_mutations:
        permitted_primitives -= MUTATING_SCREEN_PRIMITIVES
    known_ids: dict[str, dict[str, str]] = {}
    changed: list[dict[str, Any]] = []
    read_failures: list[dict[str, Any]] = []
    ran: list[dict[str, Any]] = []
    element_mutating_verbs = frozenset({"click", "type", "choose", "upload", "drag"})
    targeting_verbs = element_mutating_verbs | frozenset(
        {"read", "hover", "scroll", "caret", "select", "focus"}
    )
    watched_verbs = element_mutating_verbs | frozenset({"press", "navigate", "caret", "select"})
    navigating_verbs = frozenset({"navigate"})

    def _facets(clickable: Any, name: str, context: str) -> dict:
        facets: dict = {}
        if clickable is not None:
            facets["clickable"] = bool(clickable)
        if name:
            facets["name"] = name
        if context:
            facets["context"] = context
        return facets

    def _matching(documents: list, facets: dict) -> list:
        if not facets:
            return documents

        def admits(document) -> bool:
            for field, wanted in facets.items():
                if field == "clickable":
                    if bool(document.payload.get("clickable", False)) is not bool(wanted):
                        return False
                elif field == "context":
                    if str(wanted) not in str(document.payload.get(field, "") or ""):
                        return False
                elif str(document.payload.get(field, "") or "") != str(wanted):
                    return False
            return True

        return [document for document in documents if admits(document)]

    asked: list[tuple[Any, str]] = _asked_queries
    rephrased: list[str] = []

    def _note_if_rephrasing(query: str, hits: list) -> None:
        top = hits[0].id if hits else ""
        if not top or rephrased:
            return
        vector = retrieval.intent(query)
        if vector is None:
            return
        alike = current_limits().find_rephrasing_similarity
        for earlier, found in asked:
            if found == top and float(earlier @ vector) >= alike:
                rephrased.append(control_message("rephrasing", query=query))
                return
        asked.append((vector, top))
        del asked[:-12]

    def _rank(
        query: str, limit: int, floor: float = 0.0, facets: dict | None = None, near: str = ""
    ) -> list:
        raw = surface.documents(target_id)
        if not raw.get("ok"):
            read_failures.append({key: value for key, value in raw.items() if key != "ok"})
            raise RuntimeError(raw.get("error", "Could not read the screen."))
        documents = raw.get("documents", [])
        candidates = _matching(documents, facets or {})
        if not candidates and documents:
            logger.info(
                "screen find: facets %r admitted nothing; ranking the whole surface", facets
            )
            candidates = documents
        index = retrieval.Index(candidates)
        if near:
            limits = current_limits()
            try:
                hits = index.anchored(
                    query,
                    near,
                    top_k=limit,
                    weight=limits.find_near_weight,
                    anchor_margin=limits.find_anchor_margin,
                )
            except retrieval.WeakAnchor as weak:
                raise RuntimeError(
                    control_message("weak_anchor", query=str(query), anchor=weak.anchor)
                ) from None
        else:
            hits = index.search(query, top_k=limit, floor=floor)
        logger.info(
            "screen find: surface=%s query=%r results=%d top=%r",
            surface_name,
            query,
            len(hits),
            (hits[0].payload.get("name", "") if hits else ""),
        )
        _note_if_rephrasing(query, hits)
        return hits

    appeared_detail_limit = 12

    def _hydrate(ids: frozenset[str]) -> dict:
        if not ids:
            return {}
        known = [known_ids[identifier] for identifier in ids if identifier in known_ids]
        sample = known[:appeared_detail_limit] or [
            {"id": identifier} for identifier in sorted(ids)[:appeared_detail_limit]
        ]
        report: dict[str, Any] = {"appeared": sample}
        if len(ids) > len(sample):
            report["appeared_total"] = len(ids)
        return report

    async def _record_change(name: str, args: list, before: surface_module.Glance):
        after = await asyncio.to_thread(surface.glance, target_id)
        moved = surface_module.changes_between(before.facts, after.facts)
        appeared = surface_module.appeared_between(before, after)
        record: dict[str, Any] = {"action": name}
        if args and isinstance(args[0], str):
            record.update(known_ids.get(args[0], {"id": args[0]}))
        record.update(moved)
        navigated = name in navigating_verbs or "url" in moved
        if navigated:
            record["navigated"] = {
                "title": after.facts.get("title", ""),
                "url": after.facts.get("url", ""),
                "elements": len(after.ids),
            }
            record.pop("appeared", None)
        elif appeared:
            record.update(_hydrate(appeared))
        if not moved and not appeared:
            record["changed"] = []
        if not target_obj.visible:
            record["visible"] = False
        return record

    def _record(hit: Any) -> dict:
        return {"id": hit.id, **hit.payload}

    def _register(record: dict) -> None:
        known_ids[record["id"]] = {
            "id": record["id"],
            "name": record.get("name", ""),
            "role": record.get("role", ""),
            "context": record.get("context", ""),
        }

    def _identity(record: dict) -> tuple:
        return (record.get("name", ""), record.get("role", ""), record.get("context", ""))

    def _candidates(records: list) -> str:
        return compact(
            [
                {
                    field: record.get(field)
                    for field in ("id", "name", "role", "context", "parent", "bounds")
                    if record.get(field)
                }
                for record in records
            ]
        )

    def find_many(query, limit=8, clickable=None, near="", name="", context="", **_):
        limits = current_limits()
        wanted = max(1, min(int(limit), limits.find_many_ceiling))
        floor = limits.find_relevance_floor
        facets = _facets(clickable, name, context)
        records = [_record(hit) for hit in _rank(str(query), wanted, floor, facets, str(near))]
        for record in records:
            _register(record)
        ran.append(
            {
                "find_many": str(query),
                "matched": len(records),
                "ids": [record["id"] for record in records],
            }
        )
        return records

    def find_one(query, clickable=None, near="", name="", context="", **_):
        facets = _facets(clickable, name, context)
        scored = [
            (_record(hit), float(hit.score or 0.0))
            for hit in _rank(str(query), 8, 0.0, facets, str(near))
        ]
        if not scored:
            raise RuntimeError(control_message("no_match", query=str(query)))
        top, top_score = scored[0]
        shortlist = current_limits().find_candidates
        competitive = [
            record
            for record, score in scored[:shortlist]
            if top_score <= 0 or score >= 0.9 * top_score
        ]
        twins = [record for record in competitive[1:] if _identity(record) == _identity(top)]
        if twins:
            raise RuntimeError(
                control_message(
                    "ambiguous_match", query=str(query), candidates=_candidates([top, *twins])
                )
            )
        runner_up = scored[1][1] if len(scored) > 1 else 0.0
        spread = statistics.pstdev([score for _record, score in scored]) if len(scored) > 1 else 0.0
        margin = (top_score - runner_up) / spread if spread > 1e-9 else 1.0
        if margin < current_limits().find_one_margin:
            raise RuntimeError(
                control_message(
                    "unsure_match",
                    query=str(query),
                    candidates=_candidates([record for record, _ in scored[:shortlist]]),
                )
            )
        _register(top)
        ran.append(
            {
                "find_one": str(query),
                "matched": {key: top.get(key) for key in ("id", "role", "name") if top.get(key)},
            }
        )
        return top

    async def wait_for(query, seconds=5.0, clickable=None, near="", name="", context="", **_):
        deadline = time.monotonic() + max(0.0, float(seconds))
        interval = current_limits().settle_poll_seconds
        while True:
            hits = await asyncio.to_thread(
                find_many, query, 1, clickable=clickable, near=near, name=name, context=context
            )
            if hits:
                return hits[0]
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    control_message("waited_in_vain", query=str(query), seconds=f"{seconds:g}")
                )
            await asyncio.sleep(interval)

    def _resolve_target(verb: str, args: list) -> list:
        if not args:
            return args
        target = args[0]
        if isinstance(target, dict) and "id" in target:
            return [target["id"], *args[1:]]
        if not isinstance(target, str) or target in known_ids:
            return args
        if _ELEMENT_ID.fullmatch(target):
            return args
        if verb in element_mutating_verbs:
            resolved = find_one(target)["id"]
        else:
            hits = _rank(target, 1, 0.0)
            if not hits:
                raise RuntimeError(control_message("no_match", query=target))
            record = _record(hits[0])
            _register(record)
            resolved = record["id"]
        return [resolved, *args[1:]]

    async def dispatch(name: str, args: list, keywords: dict) -> Any:
        if name == "find_many":
            return await asyncio.to_thread(find_many, *args, **keywords)
        if name == "find_one":
            return await asyncio.to_thread(find_one, *args, **keywords)
        if name == "wait_for":
            return await wait_for(*args, **keywords)
        if name in targeting_verbs:
            args = await asyncio.to_thread(_resolve_target, name, list(args))
        watched = name in watched_verbs
        before = (
            await asyncio.to_thread(surface.glance, target_id)
            if watched
            else surface_module.Glance()
        )
        outcome = await asyncio.to_thread(surface.perform, target_id, name, list(args), keywords)
        if isinstance(outcome, dict):
            if outcome.get("ok") is False:
                ran.append({name: args[0] if args else "", "failed": outcome.get("error", "")})
                raise RuntimeError(outcome.get("error", f"{name} failed"))
            step: dict[str, Any] = {name: args[0] if args and isinstance(args[0], str) else ""}
            if watched:
                record = await _record_change(name, args, before)
                if record is not None:
                    changed.append(record)
                    step.update({key: value for key, value in record.items() if key != "action"})
            ran.append(step)
            if "result" in outcome:
                return outcome["result"]
            if "lines" in outcome:
                return outcome["lines"]
            return {key: value for key, value in outcome.items() if key != "ok"}
        return outcome

    active = tool_context.current()
    workflows = _workflow_catalogue(services)
    scratch_spaces = _plugin_service(services, "scratch_spaces")
    scratch = await scratch_spaces.create("screen") if scratch_spaces is not None else ""
    project_directory = services.project_directory or active.workspace or ""
    targets_before = target_registry.list_targets()
    call_id = current_tool_call_id()

    def on_started(process: Any) -> None:
        if call_id:
            _live_control[call_id] = process

    try:
        result = await control.run_control_script(
            script,
            dispatch,
            profile=active.sandbox,
            workspace=active.workspace,
            primitives=tuple(sorted(permitted_primitives)),
            target=target_id,
            import_roots=(
                workflows.import_roots(project_directory) if workflows is not None else None
            ),
            dependency_roots=(
                workflows.dependency_roots(project_directory) if workflows is not None else None
            ),
            library_roots=(
                workflows.library_roots(project_directory) if workflows is not None else None
            ),
            scratch=scratch,
            on_started=on_started,
        )
    finally:
        if call_id:
            _live_control.pop(call_id, None)
        if scratch_spaces is not None and scratch:
            await scratch_spaces.release(scratch)
    if isinstance(result, dict):
        moved = target_registry.difference(targets_before, target_registry.list_targets())
        if moved:
            result.setdefault("targets", moved)
        for failure in read_failures[-1:]:
            for key, value in failure.items():
                result.setdefault(key, value)
    if changed and isinstance(result, dict):
        result.setdefault("changed", changed)
    if ran and isinstance(result, dict):
        result.setdefault("ran", ran)
    if rephrased and isinstance(result, dict):
        result.setdefault("note", rephrased[0])
    return compact(result)


class ComputerUse(Feature):
    """Drives the screen: contributes the control_screen tool and the screen context."""

    def __init__(self, configuration: ComputerControlConfiguration | None = None) -> None:
        self._configuration = configuration or ComputerControlConfiguration()

    def attach(self, context, host) -> None:
        self._context = context
        self._host = host
        bundle = getattr(host, "services", None) or {}
        self._workflows = bundle.get("workflows") if isinstance(bundle, dict) else None
        if isinstance(bundle, dict):
            web_surface.SURFACE.configure(
                endpoint_resolver=bundle.get("browser_endpoint"),
                download_handler=bundle.get("browser_download"),
            )
        self._prompts = context.prompts("computer_use")
        set_retrieval_policy(retrieval_policy_from(self._configuration.retrieval))

    @property
    def _enabled(self) -> bool:
        """Whether computer use is turned on for this session."""
        context = getattr(self, "_context", None)
        if context is None:
            return False
        return self._configuration.enabled

    def contribute_tools(self) -> list:
        return [control_screen] if self._enabled else []

    def compose_prompt(self, variables: dict[str, str]) -> None:
        """Place stable screen guidance in the session prompt once when computer use is enabled."""
        if not self._enabled:
            return
        guidance = self._prompts.load("computer_control_guidance", {}).strip()
        if guidance:
            variables["computer_control_guidance"] = guidance

    def compose_context(self, context: dict) -> None:
        """The screen targets and primitives, when the feature is enabled."""
        if not self._enabled:
            return
        try:
            if not target_registry.warm():
                context["screen"] = {"reading": message_loader("computer")("screen_warming")}
                return
            block = target_registry.context_block()
            saved = (
                self._workflows.available(self._context.working_directory or "")
                if self._workflows is not None
                else []
            )
            if saved:
                block["workflows"] = saved
            context["screen"] = block
        except Exception:
            context["screen"] = {}

    def terminate_tool_call(self, tool_call_id: str) -> bool:
        """Kill the control-screen child still running for this call."""
        return terminate_control_call(tool_call_id)


# The tool's model-facing description is this plugin's own file, applied once at import.
control_screen.description = (
    _DESCRIPTIONS.load("control_screen", {}).strip() or control_screen.description
)

__all__ = ["ComputerUse", "control_screen"]
