# Features and plugins

A session runs a plain model turn by itself. Everything else — goal review, compaction, permission
gates, autonomous continuation, the observational-memory ledger, background jobs — is a **feature**
you compose onto the core. The core knows none of them: it runs its turn and, at fixed points,
calls the hooks the installed features implement. Features do not know each other either; if one
is interested in what another produces, it subscribes to an event on the shared bus and reacts.

## The seam

The public surface lives in `langmesh.runtime.features`:

- `Feature` — the hooks a feature implements. Hooks you omit are no-ops.
- `PluginContext` — what a feature is given to live: identity, configuration, its templates, the bus.
- `PluginBus` — the decoupled channel between features. `subscribe(type, handler)` to hear an event, `emit(event)` to publish one.
- `Features` — the installed set, which the harness reaches with `features.by_type(SomeFeature)`.

The hooks are the points in the turn where a feature can act:

| Hook | When the core calls it |
| --- | --- |
| `compose_context(context)` | building the turn's model-facing context |
| `compose_prompt(variables)` | building the system prompt's named sections |
| `prepare_request(messages)` | the exact request about to leave |
| `should_maintain` / `begin_maintenance` / `advance_maintenance` / `run_maintenance` / `maintenance_ready` / `valid_during_maintenance` / `maintenance_tool_schemas` / `fail_maintenance` | holding the loop to reclaim context |
| `plan_tool_calls` / `resolve_gates` / `review_automatic_gate` | gating a batch of tool calls |
| `drain()` | turn-driven events (e.g. finished background jobs) |
| `snapshot()` / `restore(snapshot)` | durable session state beside the checkpoint |
| `attach(context, host)` | installation; the library's own features keep the internal host here |

## Composing a session's features

The application layer composes which features a session runs. `Session` composes the shipped
battery by default; pass your own list to change it. A feature you leave out simply is not there.

```python
from langmesh import Session, SessionComponents
from langmesh.runtime.features.plugins.goal_review import GoalReviewFeature
from langmesh.runtime.features.plugins.compaction import Compaction

session = Session(
    agent,
    directory="/srv/checkout",
    components=SessionComponents(
        features=[
            GoalReviewFeature(journal=journal),
            Compaction(strategy=custom_strategy),
        ],
    ),
)
```

The shipped classes are ordinary classes: construct them with the ports they declare (a journal,
a strategy, a store) and hand the instances over. `features=()` runs a plain session with no
features at all. `Session`'s default battery is `langmesh.runtime.features.battery.default_features`.

## Writing a feature

A feature is a subclass of `Feature` implementing the hooks it needs:

```python
from langmesh.runtime.features import Feature, PluginContext

class MyFeature(Feature):
    def __init__(self, *, some_port: int = 0) -> None:
        self._port = some_port

    def compose_context(self, context: dict) -> None:
        context["custom_thing"] = {"value": self._port}
```

A feature that wants to hear what others publish subscribes in `attach`:

```python
    def attach(self, context: PluginContext, host=None) -> None:
        context.bus.subscribe(CustomEvent, self._on_custom_event)
```

The library's own plugins additionally receive the internal `PluginHost` — grouped views of the
conversation, boundary, tools, window, turn machinery, and bookkeeping. A caller's plugin never
needs it; the hooks and the context are the whole surface.

## Prompts are configurable

Each shipped plugin keeps its own prompt templates in its `prompts/` directory beside its code —
shipping them is an arbitrary choice, not a hardcoded part of the core. A template resolves from
the catalogue's overrides first, then the plugin's own directory, then the shared set. Supply a
`Catalogue(prompts={...})` (or any catalogue whose `prompt_override` answers a name) to override
any plugin template from code; edit the plugin's `prompts/` files to change the shipped ones.
