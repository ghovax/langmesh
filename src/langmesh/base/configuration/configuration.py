"""Pure profile and boundary models used by the LangMesh runtime."""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatch
import re
from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from langmesh.base.configuration.permission_mode import PermissionMode
from langmesh.base import confinement


class Section(BaseModel, extra="forbid"):
    """A validated value object, not a file or application configuration container."""


class FilesystemConfiguration(Section):
    """The paths a child may read, write, or receive as an explicit grant."""

    readable: list[str] = Field(
        default_factory=lambda: [
            "~/.agents",
            "~/.config",
            "~/.local",
            "~/.nix-profile",
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
            "~/Library/Keychains",
        ]
    )
    writable: list[str] = Field(
        default_factory=lambda: ["$WORKSPACE", "$TMPDIR", "/tmp", "$XDG_CACHE_HOME", "~/.cache"]
    )
    grantable: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class SandboxConfiguration(Section):
    """A caller-facing description of the operating-system boundary for a session."""

    enforce: Literal["required", "preferred", "off"] = "required"
    filesystem: FilesystemConfiguration = Field(default_factory=FilesystemConfiguration)
    network: bool = False
    limits: dict[str, int] = Field(
        default_factory=lambda: {
            "RLIMIT_CORE": 0,
            "RLIMIT_FSIZE": 8 * 1024 * 1024 * 1024,
            "RLIMIT_NPROC": 2048,
        }
    )
    umask: Optional[str] = None
    nice: int = 0

    def to_profile(self) -> confinement.Profile:
        """Convert this value into the boundary the runtime applies."""
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


class NamedToolPermissions(BaseModel):
    """Per-call policy for a tool whose calls have names."""

    permissions: dict[str, str] = Field(default_factory=dict)

    def decide(self, subject: str, unmatched: str = "ask") -> str:
        """Return the most specific configured decision for ``subject``."""
        best_length, best_decision = 0, unmatched
        for pattern, decision in self.permissions.items():
            if pattern and fnmatch(subject, pattern) and len(pattern) > best_length:
                best_length = len(pattern)
                best_decision = str(decision).lower()
        return best_decision


class BashToolConfiguration(BaseModel):
    """The bash policy attached to an agent profile."""

    background_allowed: bool = True
    permissions: dict[str, str] = Field(default_factory=dict)

    _SHELL_SPLIT = re.compile(r"\s*(?:&&|\|\||[;|])\s*")
    _SUBSHELL = re.compile(r"\$\((.+?)\)|`(.+?)`")
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
        """Return destructive defaults overlaid by the profile's explicit rules."""
        return {**self.DESTRUCTIVE_DEFAULTS, **self.permissions}

    def evaluate_permission(self, command: str, unmatched: str = "allow") -> str:
        """Return the most specific decision matching any command segment."""
        best_match_length, best_decision = 0, unmatched
        for segment in self._extract_segments(command):
            for pattern, decision in self.effective_permissions.items():
                if self._segment_matches(segment, pattern) and (
                    not best_match_length or len(pattern) > best_match_length
                ):
                    best_match_length = len(pattern)
                    best_decision = decision.lower()
        return best_decision

    def command_matches(self, command: str, patterns: Iterable[str]) -> bool:
        """Return whether any command segment matches any supplied pattern."""
        return any(
            self._segment_matches(segment, pattern)
            for segment in self._extract_segments(command)
            for pattern in patterns
            if pattern
        )

    def _extract_segments(self, command: str) -> list[str]:
        """Split a command at shell operators and recursively inspect substitutions."""
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
        """Fold short ``rm`` flags into the canonical destructive form."""
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
            return segment.startswith(pattern[:-1].rstrip())
        return segment == pattern


class ToolsConfiguration(BaseModel):
    """The per-tool policies declared by an agent profile."""

    bash: BashToolConfiguration = Field(default_factory=BashToolConfiguration)
    mcp: NamedToolPermissions = Field(default_factory=NamedToolPermissions)
    screen: NamedToolPermissions = Field(default_factory=NamedToolPermissions)


class AgentConfiguration(BaseModel):
    """A self-contained agent profile accepted by the runtime."""

    name: str = ""
    title: str = ""
    aliases: list[str] = Field(default_factory=list)
    color: str = ""
    description: str = ""
    role: str = ""
    enabled: bool = True
    skills: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    provider: Optional[str] = None
    reasoning_effort: str | None = None
    permission_mode: Literal["ask", "automatic", "allow"] = "ask"
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
        """Return the provider/model identifier when both profile fields are present."""
        if not self.model or not self.provider:
            return None
        return f"{self.provider}/{self.model}"

    @property
    def permission_default(self) -> PermissionMode:
        """Return the profile's default permission mode."""
        return PermissionMode.resolve(self.permission_mode)


class PermissionEvaluator:
    """Evaluate the permissions declared by one agent profile."""

    def __init__(self, agent_configuration: AgentConfiguration):
        self._configuration = agent_configuration

    def evaluate_bash_permission(self, command: str, unmatched: str = "allow") -> str:
        return self._configuration.tools.bash.evaluate_permission(command, unmatched=unmatched)

    def check_bash_background(self) -> None:
        if not self._configuration.tools.bash.background_allowed:
            raise PermissionDenied("Background bash execution is not allowed")


class PermissionDenied(RuntimeError):
    """A tool call refused by profile policy rather than by the operating system."""


__all__ = [
    "AgentConfiguration",
    "BashToolConfiguration",
    "FilesystemConfiguration",
    "NamedToolPermissions",
    "PermissionDenied",
    "PermissionEvaluator",
    "SandboxConfiguration",
    "Section",
    "ToolsConfiguration",
]
