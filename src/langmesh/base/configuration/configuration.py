from __future__ import annotations

from collections.abc import Iterable
import logging
import os
from langmesh.base.secrets import (
    EXA_API_KEY,
    FIRECRAWL_API_KEY,
    JINA_API_KEY,
    read_secret,
)
import re
from fnmatch import fnmatch
from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from langmesh.base.configuration.permission_mode import PermissionMode
from langmesh.base import confinement


logger = logging.getLogger(__name__)


class Section(BaseModel, extra="forbid"):
    """A part of the configuration file, refusing every key it does not define so a typo is an error."""


class ExaConfiguration(Section):
    """Exa — the web-search tool's backend."""

    api_key: str = Field(default="", json_schema_extra={"secret": True})

    @property
    def effective_api_key(self) -> str:
        return read_secret(EXA_API_KEY)


class JinaConfiguration(Section):
    """Jina Reader, the web-fetch tool's default engine. The key is optional: it works keyless, slower."""

    api_key: str = Field(default="", json_schema_extra={"secret": True})

    @property
    def effective_api_key(self) -> str:
        return read_secret(JINA_API_KEY)


class FirecrawlConfiguration(Section):
    """Firecrawl, the fallback for pages Jina returns thin. Without a key the fallback is skipped."""

    api_key: str = Field(default="", json_schema_extra={"secret": True})
    api_url: str = Field(default="")

    @property
    def effective_api_key(self) -> str:
        return read_secret(FIRECRAWL_API_KEY)

    @property
    def effective_api_url(self) -> str:
        return self.api_url


class WebFetchConfiguration(Section):
    """Fetching a page, through the reader engines and then directly for sites that refuse them."""

    proxy_url: str = Field(default="")
    # How long one engine is given before the cascade moves on, and how long a download is given.
    timeout_seconds: int = Field(default=30)
    download_timeout_seconds: int = Field(default=120)
    # Below this a page is a wall or a stub rather than the content, so the next engine is tried.
    minimum_useful_characters: int = Field(default=64)

    @property
    def effective_proxy_url(self) -> str:
        return self.proxy_url


class FilesystemConfiguration(Section):
    """Which paths a tool's child may read and write. The system stays readable; the home is closed."""

    readable: list[str] = Field(
        default_factory=lambda: [
            # Where a person's own agents, skills and workflows live, which a screen script imports from.
            "~/.agents",
            "~/.config",
            "~/.local",
            "~/.ssh",
            "~/.gitconfig",
            "~/.gitignore_global",
            "~/.cargo",
            "~/.rustup",
            "~/.npmrc",
            "~/.nvm",
            "~/.pyenv",
            "~/.docker",
            "~/.netrc",
            # Allow Git's macOS credential helper to read the login keychain for HTTPS pushes.
            "~/Library/Keychains",
        ],
    )
    writable: list[str] = Field(
        default_factory=lambda: ["$WORKSPACE", "$TMPDIR", "/tmp", "$XDG_CACHE_HOME", "~/.cache"]
    )
    # `/tmp` beside `$TMPDIR` because on macOS they are not the same place, and nothing personal lives there.
    grantable: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class SandboxConfiguration(Section):
    """A session's confinement, enforced by the operating system. Every other field is POSIX under its own name."""

    enforce: Literal["required", "preferred", "off"] = Field(default="required")
    filesystem: FilesystemConfiguration = Field(default_factory=FilesystemConfiguration)
    network: bool = Field(default=False)
    limits: dict[str, int] = Field(
        default_factory=lambda: {
            "RLIMIT_CORE": 0,
            "RLIMIT_FSIZE": 8 * 1024 * 1024 * 1024,
            "RLIMIT_NPROC": 2048,
        }
    )
    umask: Optional[str] = Field(default=None)
    nice: int = Field(default=0)

    def to_profile(self):
        """This configuration as the :class:`~langmesh.base.confinement.Profile` the spawn path applies."""
        return confinement.Profile(
            filesystem=confinement.Filesystem(
                readable=tuple(self.filesystem.readable),
                writable=tuple(self.filesystem.writable),
                deny=tuple(self.filesystem.deny),
                grantable=tuple(self.filesystem.grantable),
            ),
            network=self.network,
            limits={name: int(value) for name, value in self.limits.items()},
            umask=int(self.umask, 8) if self.umask else None,
            nice=self.nice,
            enforce=self.enforce,
        )


class WorkspaceConfiguration(Section):
    """Where a session's tools actually run."""

    strategy: Literal["none", "branch", "worktree"] = Field(default="none")


class CompactionConfiguration(Section):
    """Context compacting thresholds. Observational memory is a separate, user-managed concern.

    The hidden summarizer is asked again until it submits its summary — emitting the tool call
    correctly is the model's own job, so nothing caps how often it may be reminded.
    """

    automatic: bool = Field(default=True)
    reclaim_at_fraction: float = Field(default=0.85)
    output_reserve_fraction: float = Field(default=0.1)
    recent_working_set_fraction: float = Field(default=0.15)

    @field_validator(
        "reclaim_at_fraction",
        "output_reserve_fraction",
        "recent_working_set_fraction",
    )
    @classmethod
    def _fraction(cls, value: float) -> float:
        if not 0 < value < 1:
            raise ValueError("compaction fractions must be greater than 0 and less than 1")
        return value


class GoalReviewConfiguration(Section):
    """Who settles an agent-marked goal.

    The agent owns its goal's status. A marked ``satisfied`` or ``blocked`` is settled either by
    an independent reviewer that confirms or overrides the mark, or by the working agent itself,
    in which case that mark is final and the session ends. An open, unmarked goal is re-opened
    with a light continuation reminder. When a reviewer runs, it is asked again until it
    submits — modelling correctly is the model's own job, so nothing caps how often.
    """

    #: An isolated session confirms or overrides the working agent's mark.
    REVIEWER: ClassVar[str] = "reviewer"
    #: The working agent's mark is the settlement; there is no second session.
    AGENT: ClassVar[str] = "agent"

    settlement: Literal["reviewer", "agent"] = Field(default="reviewer")


class AttachmentsConfiguration(Section):
    """What a file the person attaches may cost the conversation it rides in."""

    # A generous ceiling, since a huge image would blow up the persisted conversation it is inlined into.
    inline_image_megabytes: float = Field(default=20.0)

    @property
    def inline_image_bytes(self) -> int:
        return max(0, int(self.inline_image_megabytes * 1024 * 1024))


class TuningConfiguration(Section):
    """Explicit size, count, and timing limits for tools."""

    limits: dict[str, int | float] = Field(default_factory=dict)

    @field_validator("limits")
    @classmethod
    def _known_limits(cls, value: dict[str, int | float]) -> dict[str, int | float]:
        from langmesh.base.primitives.limits import Limits

        unknown = sorted(name for name in value if not hasattr(Limits, name))
        if unknown:
            raise ValueError(
                f"unknown limit(s): {', '.join(unknown)}. The names that exist are the fields of `langmesh.base.primitives.limits.Limits`; the settings panel lists them with their shipped values."
            )
        return value


class UserContextConfiguration(Section):
    """Opt-in snapshot of how the user works on this machine, appended as session context."""

    enabled: bool = Field(default=False)
    refresh_hours: float = Field(default=6.0, gt=0)


class RetrievalConfiguration(Section):
    """How a screen is ranked when a script asks for an element by name. The defaults are fitted, not chosen."""

    multilingual_rank_model: str = Field(default="minishlab/M2V_multilingual_output")
    english_rank_model: str = Field(default="minishlab/potion-base-32M")
    lexical_gate_short_words: int = Field(default=3, ge=0)
    lexical_gate_long_words: int = Field(default=7, ge=1)


class ComputerControlConfiguration(Section):
    """Opt-in control of macOS apps through the screen tools. Off by default, and needs an Accessibility grant."""

    enabled: bool = Field(default=False)
    retrieval: RetrievalConfiguration = Field(default_factory=RetrievalConfiguration)


class ToolboxConfiguration(Section):
    """Whether a session may install tools for itself, into a directory deleted when it ends."""

    enabled: bool = Field(default=True)


class MCPServerConfiguration(BaseModel):
    """One MCP server from an ``mcp.json`` entry, permissive about keys it does not model."""

    enabled: bool = True
    transport: Literal["stdio", "streamable_http"] = "stdio"
    stateful: bool = True
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 30


class MCPConfiguration(Section):
    """The MCP servers available to a session, from ``mcp.json`` rather than the configuration file."""

    servers: dict[str, MCPServerConfiguration] = Field(default_factory=dict)

    def enabled_servers(self) -> dict[str, MCPServerConfiguration]:
        return {name: server for name, server in self.servers.items() if server.enabled}


class RemoteAgentAuthConfiguration(BaseModel):
    """How to authenticate to one external agent. ``${VAR}`` expands at load, so tokens stay out of the file."""

    type: Literal["none", "bearer", "api_key", "oauth2"] = "none"
    token: str = ""
    header: str = "Authorization"
    scheme_prefix: str = "Bearer"
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = Field(default_factory=list)


class RemoteAgentServerConfiguration(BaseModel):
    """One registered external A2A agent, permissive about unmodelled keys for the same reason."""

    enabled: bool = True
    card_url: str = ""
    auth: RemoteAgentAuthConfiguration = Field(default_factory=RemoteAgentAuthConfiguration)
    card_ttl_seconds: int = 3600
    # Hostnames allowed beyond the card_url origin (the origin is always allowed).
    allowed_hosts: list[str] = Field(default_factory=list)
    # Permit private/loopback targets (e.g. a LangMesh-to-LangMesh loopback).
    allow_private: bool = False
    # Which local agent profiles may delegate to this remote agent. Empty = all profiles.
    allowed_profiles: list[str] = Field(default_factory=list)


class RemoteAgentsConfiguration(Section):
    """The external agents the harness may delegate to, from ``remote-agents.json``."""

    agents: dict[str, RemoteAgentServerConfiguration] = Field(default_factory=dict)

    def enabled_agents(self) -> dict[str, RemoteAgentServerConfiguration]:
        return {
            name: agent for name, agent in self.agents.items() if agent.enabled and agent.card_url
        }


class TelemetryExporterConfiguration(Section):
    """Where traces are sent."""

    endpoint: str = Field(default="")
    protocol: Literal["http/protobuf", "grpc"] = Field(default="http/protobuf")
    headers: dict[str, str] = Field(default_factory=dict)


class TelemetryConfiguration(Section):
    """OTLP export of traces, off until an endpoint is set, and carrying no prompt or completion bodies."""

    enabled: bool = Field(default=False)
    exporter: TelemetryExporterConfiguration = Field(default_factory=TelemetryExporterConfiguration)
    sample_ratio: float = Field(default=1.0)

    def resolved_headers(self) -> dict[str, str]:
        return {key: os.path.expandvars(value) for key, value in self.exporter.headers.items()}


class ProviderCredential(Section):
    """Credentials for one model provider."""

    api_key: str = Field(default="", json_schema_extra={"secret": True})
    base_url: str = Field(default="")


class AgentDefaults(Section):
    """What a session gets when its creator did not say."""

    permission_mode: Literal["ask", "automatic", "allow"] = Field(default="ask")


class Configuration(Section):
    """The library-owned configuration surface, with every unknown field rejected."""

    providers: dict[str, ProviderCredential] = Field(default_factory=dict)
    exa: ExaConfiguration = Field(default_factory=ExaConfiguration)
    jina: JinaConfiguration = Field(default_factory=JinaConfiguration)
    firecrawl: FirecrawlConfiguration = Field(default_factory=FirecrawlConfiguration)
    web_fetch: WebFetchConfiguration = Field(default_factory=WebFetchConfiguration)
    sandbox: SandboxConfiguration = Field(default_factory=SandboxConfiguration)
    workspace: WorkspaceConfiguration = Field(default_factory=WorkspaceConfiguration)
    compaction: CompactionConfiguration = Field(default_factory=CompactionConfiguration)
    goal_review: GoalReviewConfiguration = Field(default_factory=GoalReviewConfiguration)
    attachments: AttachmentsConfiguration = Field(default_factory=AttachmentsConfiguration)
    user_context: UserContextConfiguration = Field(default_factory=UserContextConfiguration)
    computer_control: ComputerControlConfiguration = Field(
        default_factory=ComputerControlConfiguration
    )
    toolbox: ToolboxConfiguration = Field(default_factory=ToolboxConfiguration)
    tuning: TuningConfiguration = Field(default_factory=TuningConfiguration)
    mcp: MCPConfiguration = Field(default_factory=MCPConfiguration)
    remote_agents: RemoteAgentsConfiguration = Field(default_factory=RemoteAgentsConfiguration)
    telemetry: TelemetryConfiguration = Field(default_factory=TelemetryConfiguration)
    agent: AgentDefaults = Field(default_factory=AgentDefaults)

    def configured_provider_keys(self) -> dict[str, str]:
        """The non-empty API keys per provider, for credential resolution and for filtering the model picker.

        Secret files are the configuration. An in-memory value on this object is a
        caller-supplied Session credential, used only when that file is absent.
        """
        from langmesh.base.secrets import provider_keys_from_files

        keys = provider_keys_from_files()
        for identifier, credential in self.providers.items():
            if credential.api_key and identifier not in keys:
                keys[identifier] = credential.api_key
        return keys

    def configured_provider_bases(self) -> dict[str, str]:
        """Configured non-empty base URLs per provider."""
        return {
            identifier: credential.base_url
            for identifier, credential in self.providers.items()
            if credential.base_url
        }


class NamedToolPermissions(BaseModel):
    """Per-call permission rules for a tool whose calls have a name."""

    permissions: dict[str, str] = Field(default_factory=dict)

    def decide(self, subject: str, unmatched: str = "ask") -> str:
        """The configured decision for ``subject``, or ``unmatched`` when no pattern names it."""
        best_length, best = 0, unmatched
        for pattern, decision in self.permissions.items():
            if not pattern or not fnmatch(subject, pattern):
                continue
            if len(pattern) > best_length:
                best_length, best = len(pattern), str(decision).lower()
        return best


class BashToolConfiguration(BaseModel):
    # Policy only: which tools an agent uses is `tools_enabled`, not this block.
    background_allowed: bool = True
    permissions: dict[str, str] = Field(default_factory=dict)

    _SHELL_SPLIT = re.compile(r"\s*(?:&&|\|\||[;|])\s*")
    _SUBSHELL = re.compile(r"\$\((.+?)\)|`(.+?)`")

    #: Commands whose damage is not bounded by the confinement, and what to do about each.
    DESTRUCTIVE_DEFAULTS: ClassVar[dict[str, str]] = {
        "rm -rf *": "ask",
        "rm -fr *": "ask",
        "rm -r *": "ask",
        "git reset --hard*": "ask",
        "git clean -*": "ask",
        "git push --force*": "ask",
        "git push -f*": "ask",
        "sudo *": "ask",
        "chmod -R *": "ask",
        "chown -R *": "ask",
        "dd *": "ask",
        "mkfs*": "deny",
        "shutdown*": "deny",
        "reboot*": "deny",
    }

    @property
    def effective_permissions(self) -> dict[str, str]:
        """The rules actually in force: the destructive seeds, with the person's own over them."""
        return {**self.DESTRUCTIVE_DEFAULTS, **self.permissions}

    def evaluate_permission(self, command: str, unmatched: str = "allow") -> str:
        """The configured decision for `command`, with `unmatched` returned when no pattern matches."""
        segments = self._extract_segments(command)
        rules = self.effective_permissions
        best_match_length = 0
        best_decision = unmatched
        for segment in segments:
            for pattern, decision in rules.items():
                if self._segment_matches(segment, pattern):
                    if not best_match_length or len(pattern) > best_match_length:
                        best_match_length = len(pattern)
                        best_decision = decision.lower()
        return best_decision

    def command_matches(self, command: str, patterns: Iterable[str]) -> bool:
        """Whether any segment of `command` matches any of `patterns`."""
        segments = self._extract_segments(command)
        return any(
            self._segment_matches(segment, pattern)
            for segment in segments
            for pattern in patterns
            if pattern
        )

    def _extract_segments(self, command: str) -> list[str]:
        """Split a command into segments, on shell operators and through subshells."""
        segments = [
            segment.strip() for segment in self._SHELL_SPLIT.split(command) if segment.strip()
        ]
        for match in self._SUBSHELL.finditer(command):
            inner = (match.group(1) or match.group(2)).strip()
            if inner:
                segments.extend(self._extract_segments(inner))
        return segments

    @staticmethod
    def _canonical_rm_segment(segment: str) -> str:
        """Fold `rm`'s short flags into one canonical token, so `rm -Rf` and `rm -f -r`
        are judged exactly like the `rm -rf` the destructive defaults name. Only the
        plain short-flag spelling is folded; long options pass through untouched."""
        parts = segment.split()
        if not parts or parts[0] != "rm" or len(parts) < 2:
            return segment
        flag_tokens = [
            part for part in parts[1:] if part.startswith("-") and not part.startswith("--")
        ]
        if not flag_tokens:
            return segment
        flags = "".join(part.lstrip("-") for part in flag_tokens)
        if not flags:
            return segment
        canonical = "".join(dict.fromkeys(flags.lower()))
        if "r" in canonical and "f" in canonical:
            canonical = "rf"
        rest = [
            part for part in parts[1:] if not (part.startswith("-") and not part.startswith("--"))
        ]
        return " ".join(["rm", f"-{canonical}", *rest])

    @staticmethod
    def _segment_matches(segment: str, pattern: str) -> bool:
        segment = BashToolConfiguration._canonical_rm_segment(segment)
        if pattern.endswith("*"):
            keyword = pattern[:-1].rstrip()
            # A rule names a command: it matches when the segment starts with that command, never when the keyword merely appears inside a heredoc body or `-c` code. Splitting on shell operators already isolates each command, so `a && sudo rm` matches `sudo *` through its own segment; scanning the whole text would deny a doc write whose content happens to mention "sudo", "git", or "rm".
            return segment.startswith(keyword)
        return segment == pattern


class ToolsConfiguration(BaseModel):
    """How an agent's tools behave: per-tool policy only, never membership.

    Which tools an agent uses is `tools_enabled` on the agent itself, composed by whoever builds
    the session; this block only carries the settings of the tools that are on (bash background
    and command rules, MCP and screen permissions). There is deliberately no `enabled` or
    `disabled` here, so a tool's presence has exactly one source.
    """

    bash: BashToolConfiguration = Field(default_factory=BashToolConfiguration)
    mcp: NamedToolPermissions = Field(default_factory=NamedToolPermissions)
    screen: NamedToolPermissions = Field(default_factory=NamedToolPermissions)


class AgentConfiguration(BaseModel):
    name: str = ""
    title: str = ""
    aliases: list[str] = Field(default_factory=list)
    color: str = ""
    description: str = ""
    role: str = ""
    enabled: bool = True
    # The skills this agent may use; empty offers every available one.
    skills: list[str] = Field(default_factory=list)
    # The model and its provider are separate fields, recombined into an identifier where one is wanted.
    model: Optional[str] = None
    provider: Optional[str] = None
    reasoning_effort: str = "high"
    # The mode this profile starts with when the session creator does not choose one.
    permission_mode: Literal["ask", "automatic", "allow"] = "ask"

    # An agent's own confinement, narrowing the global one. Unset means whatever the machine says.
    sandbox: Optional[SandboxConfiguration] = None
    tools: ToolsConfiguration = Field(default_factory=ToolsConfiguration)
    tools_enabled: list[str] = Field(default_factory=list)
    system_prompt: str = ""

    @property
    def identifier(self) -> str:
        return self.name

    @property
    def display_name(self) -> str:
        return self.title or self.name

    @property
    def model_identifier(self) -> Optional[str]:
        """The agent's model as the ``provider/model`` form the factory expects."""
        if not self.model or not self.provider:
            return None
        return f"{self.provider}/{self.model}"

    @property
    def permission_default(self) -> PermissionMode:
        """The card's default permission mode."""
        return PermissionMode.resolve(self.permission_mode)


class PermissionEvaluator:
    def __init__(self, agent_configuration: AgentConfiguration):
        self._configuration = agent_configuration

    def evaluate_bash_permission(self, command: str, unmatched: str = "allow") -> str:
        return self._configuration.tools.bash.evaluate_permission(command, unmatched=unmatched)

    def check_bash_background(self) -> None:
        if not self._configuration.tools.bash.background_allowed:
            raise PermissionDenied("Background bash execution is not allowed")


class PermissionDenied(RuntimeError):
    """A tool call refused by policy rather than by the operating system, and named apart from the builtin."""
