"""The plugin seam and the library's shipped plugins.

A feature is a pluggable sub-behavior a session runs beyond the plain model turn. This package
is the public surface a caller composes with: `Feature` (the hooks), `PluginContext` (what a
feature is given to live), `PluginBus` (the decoupled channel between features), the core's own
turn events, and `feature_prompts` for a plugin's own templates. The shipped plugins live under `langmesh.runtime.plugins`,
and the host composes which of them a session runs — never the library.
"""

from langmesh.runtime.features.bus import PluginBus, TurnEnded, TurnStarted
from langmesh.runtime.features.context import PluginContext
from langmesh.runtime.features.host import (
    BookkeepingView,
    BoundaryView,
    ConversationView,
    PluginHost,
    ToolsView,
    TurnView,
    WindowView,
)
from langmesh.runtime.features.seam import Feature, Features, build_features, feature_prompts

__all__ = [
    "BookkeepingView",
    "BoundaryView",
    "ConversationView",
    "Feature",
    "Features",
    "PluginBus",
    "PluginContext",
    "PluginHost",
    "ToolsView",
    "TurnEnded",
    "TurnStarted",
    "TurnView",
    "WindowView",
    "build_features",
    "feature_prompts",
]
