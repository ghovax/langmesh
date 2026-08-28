"""The daemon's application configuration and the settings for its hosted features.

The core library receives explicit runtime values and capabilities. It does not own this
application aggregate or read the daemon's configuration file.
"""

from __future__ import annotations

import os
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from langmesh.base.secrets import (
    COMPOSIO_API_KEY,
    EXA_API_KEY,
    EMAIL_IMAP_PASSWORD,
    EMAIL_OAUTH_CLIENT_SECRET,
    EMAIL_OAUTH_REFRESH_TOKEN,
    EMAIL_SMTP_PASSWORD,
    FIRECRAWL_API_KEY,
    JINA_API_KEY,
    read_secret,
)
from langmesh.protocol.dtos import SettingsUpdateRequest
from langmesh.base.configuration.configuration import SandboxConfiguration
from langmesh.base.contracts.mcp_client import MCPConfiguration
from langmesh.runtime.plugins.compaction.configuration import CompactionConfiguration
from langmesh.runtime.plugins.computer_use.configuration import ComputerControlConfiguration
from langmesh.runtime.plugins.goal_review.configuration import GoalReviewConfiguration
from langmesh.runtime.plugins.permission_reviewer.configuration import PermissionReviewConfiguration
from langmesh.runtime.plugins.titling.configuration import TitlingConfiguration
from langmeshd.commons import timing


# Hosts we can fill in from the mailbox domain so a Gmail/Fastmail/Outlook address
# only needs a password, not a copy-paste of imap.gmail.com.
_MAIL_HOSTS: dict[str, tuple[str, str]] = {
    "gmail.com": ("imap.gmail.com", "smtp.gmail.com"),
    "googlemail.com": ("imap.gmail.com", "smtp.gmail.com"),
    "fastmail.com": ("imap.fastmail.com", "smtp.fastmail.com"),
    "messagingengine.com": ("imap.fastmail.com", "smtp.fastmail.com"),
    "outlook.com": ("outlook.office365.com", "smtp.office365.com"),
    "hotmail.com": ("outlook.office365.com", "smtp.office365.com"),
    "live.com": ("outlook.office365.com", "smtp.office365.com"),
    "msn.com": ("outlook.office365.com", "smtp.office365.com"),
    "yahoo.com": ("imap.mail.yahoo.com", "smtp.mail.yahoo.com"),
    "icloud.com": ("imap.mail.me.com", "smtp.mail.me.com"),
    "me.com": ("imap.mail.me.com", "smtp.mail.me.com"),
    "mac.com": ("imap.mail.me.com", "smtp.mail.me.com"),
    # Proton Mail has no public IMAP. These point at Proton Bridge on the same host.
    "proton.me": ("127.0.0.1", "127.0.0.1"),
    "protonmail.com": ("127.0.0.1", "127.0.0.1"),
    "protonmail.ch": ("127.0.0.1", "127.0.0.1"),
    "pm.me": ("127.0.0.1", "127.0.0.1"),
}

_PROTON_DOMAINS = frozenset({"proton.me", "protonmail.com", "protonmail.ch", "pm.me"})
_MACHINE_SLUG = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_OAUTH_DOMAINS = {
    "gmail.com": "google",
    "googlemail.com": "google",
    "outlook.com": "microsoft",
    "hotmail.com": "microsoft",
    "live.com": "microsoft",
    "msn.com": "microsoft",
    "yahoo.com": "yahoo",
}

# The daemon's presentation order; the generic schema walker does not know these names.
SETTING_SECTION_ORDER = (
    "agent",
    "workspace",
    "sandbox",
    "toolbox",
    "compaction",
    "user_context",
    "goal_review",
    "permission_reviewer",
    "titling",
    "computer_control",
    "providers",
    "exa",
    "jina",
    "firecrawl",
    "web_fetch",
    "mcp",
    "remote_agents",
    "telemetry",
    "limits",
)


def _domain(address: str) -> str:
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[-1].lower()


def _is_proton(address: str) -> bool:
    return _domain(address) in _PROTON_DOMAINS


def _hosts_for(address: str) -> tuple[str, str]:
    if "@" not in address:
        return "", ""
    return _MAIL_HOSTS.get(_domain(address), ("", ""))


def _untagged_local(address: str) -> tuple[str, str]:
    """Local part without a plus-tag, and domain. Empty domain when the address has no @."""
    if "@" not in address:
        return address, ""
    local, domain = address.rsplit("@", 1)
    return local.split("+", 1)[0], domain


def _account_login(address: str) -> str:
    """IMAP/SMTP auth is the account, not the plus-address used as From."""
    local, domain = _untagged_local(address)
    if not domain:
        return local
    return f"{local}@{domain}"


def _address_plus_tag(address: str) -> str:
    """The plus-tag in a mailbox address, or empty when the local part has none."""
    if "@" not in address:
        return ""
    local = address.rsplit("@", 1)[0]
    if "+" not in local:
        return ""
    return local.split("+", 1)[1].lower()


def compact_mail_secret(value: str | None) -> str:
    """IMAP/SMTP secrets as the provider expects them.

    Gmail copies app passwords as four groups of four; those spaces are display-only.
    Surrounding whitespace is never part of the secret. Files under the secrets
    directory store the compacted form.
    """
    if not value:
        return ""
    text = value.strip()
    compacted = text.replace(" ", "")
    if len(compacted) == 16 and compacted.isalnum():
        return compacted
    return text


class AppConfigurationSection(BaseModel, extra="forbid"):
    """A daemon-owned configuration section that rejects unknown fields."""


class ExaConfiguration(AppConfigurationSection):
    """The daemon's Exa credential for the web-search plugin."""

    api_key: str = Field(default="", json_schema_extra={"secret": True})

    @property
    def effective_api_key(self) -> str:
        return read_secret(EXA_API_KEY)


class JinaConfiguration(AppConfigurationSection):
    """The daemon's Jina credential for the web-fetch plugin."""

    api_key: str = Field(default="", json_schema_extra={"secret": True})

    @property
    def effective_api_key(self) -> str:
        return read_secret(JINA_API_KEY)


class FirecrawlConfiguration(AppConfigurationSection):
    """The daemon's optional Firecrawl fallback for the web-fetch plugin."""

    api_key: str = Field(default="", json_schema_extra={"secret": True})
    api_url: str = ""

    @property
    def effective_api_key(self) -> str:
        return read_secret(FIRECRAWL_API_KEY)

    @property
    def effective_api_url(self) -> str:
        return self.api_url


class WebFetchConfiguration(AppConfigurationSection):
    """The daemon's HTTP settings for the web-fetch plugin."""

    proxy_url: str = ""
    timeout_seconds: int = 30
    download_timeout_seconds: int = 120
    minimum_useful_characters: int = 64

    @property
    def effective_proxy_url(self) -> str:
        return self.proxy_url


class WorkspaceConfiguration(AppConfigurationSection):
    """The daemon's workspace lifecycle choice."""

    strategy: Literal["none", "branch", "worktree"] = "none"


class AttachmentsConfiguration(AppConfigurationSection):
    """The daemon's ceiling for images inlined into a session."""

    inline_image_megabytes: float = 20.0

    @property
    def inline_image_bytes(self) -> int:
        return max(0, int(self.inline_image_megabytes * 1024 * 1024))


class UserContextConfiguration(AppConfigurationSection):
    """The daemon's opt-in user-context snapshot settings."""

    enabled: bool = False
    refresh_hours: float = Field(default=6.0, gt=0)


class ToolboxConfiguration(AppConfigurationSection):
    """Whether the daemon provisions an ephemeral toolbox for a session."""

    enabled: bool = True


class ProviderCredential(AppConfigurationSection):
    """A daemon-owned provider credential reference and optional endpoint."""

    api_key: str = Field(default="", json_schema_extra={"secret": True})
    base_url: str = ""


class AgentDefaults(AppConfigurationSection):
    """Defaults applied by the daemon when a session omits a choice."""

    permission_mode: Literal["ask", "automatic", "allow"] = "ask"


class TelemetryExporterConfiguration(AppConfigurationSection):
    """The daemon's OTLP destination."""

    endpoint: str = ""
    protocol: Literal["http/protobuf", "grpc"] = "http/protobuf"
    headers: dict[str, str] = Field(default_factory=dict)


class TelemetryConfiguration(AppConfigurationSection):
    """The daemon's optional telemetry export."""

    enabled: bool = False
    exporter: TelemetryExporterConfiguration = Field(default_factory=TelemetryExporterConfiguration)
    sample_ratio: float = 1.0

    def resolved_headers(self) -> dict[str, str]:
        return {key: os.path.expandvars(value) for key, value in self.exporter.headers.items()}


class RemoteAgentAuthConfiguration(BaseModel):
    """Credentials for one remote agent configured by the daemon."""

    type: Literal["none", "bearer", "api_key", "oauth2"] = "none"
    token: str = ""
    header: str = "Authorization"
    scheme_prefix: str = "Bearer"
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = Field(default_factory=list)


class RemoteAgentServerConfiguration(BaseModel):
    """One remote agent endpoint and its daemon-side access policy."""

    enabled: bool = True
    card_url: str = ""
    auth: RemoteAgentAuthConfiguration = Field(default_factory=RemoteAgentAuthConfiguration)
    card_ttl_seconds: int = 3600
    allowed_hosts: list[str] = Field(default_factory=list)
    allow_private: bool = False
    allowed_profiles: list[str] = Field(default_factory=list)


class RemoteAgentsConfiguration(AppConfigurationSection):
    """Remote agents configured outside the core runtime."""

    agents: dict[str, RemoteAgentServerConfiguration] = Field(default_factory=dict)

    def enabled_agents(self) -> dict[str, RemoteAgentServerConfiguration]:
        return {
            name: agent for name, agent in self.agents.items() if agent.enabled and agent.card_url
        }


class ApplicationConfiguration(AppConfigurationSection):
    """The daemon's complete file-backed configuration, never imported by ``langmesh``."""

    providers: dict[str, ProviderCredential] = Field(default_factory=dict)
    exa: ExaConfiguration = Field(default_factory=ExaConfiguration)
    jina: JinaConfiguration = Field(default_factory=JinaConfiguration)
    firecrawl: FirecrawlConfiguration = Field(default_factory=FirecrawlConfiguration)
    web_fetch: WebFetchConfiguration = Field(default_factory=WebFetchConfiguration)
    sandbox: SandboxConfiguration = Field(default_factory=SandboxConfiguration)
    workspace: WorkspaceConfiguration = Field(default_factory=WorkspaceConfiguration)
    compaction: CompactionConfiguration = Field(default_factory=CompactionConfiguration)
    goal_review: GoalReviewConfiguration = Field(default_factory=GoalReviewConfiguration)
    permission_reviewer: PermissionReviewConfiguration = Field(
        default_factory=PermissionReviewConfiguration
    )
    titling: TitlingConfiguration = Field(default_factory=TitlingConfiguration)
    attachments: AttachmentsConfiguration = Field(default_factory=AttachmentsConfiguration)
    user_context: UserContextConfiguration = Field(default_factory=UserContextConfiguration)
    computer_control: ComputerControlConfiguration = Field(
        default_factory=ComputerControlConfiguration
    )
    toolbox: ToolboxConfiguration = Field(default_factory=ToolboxConfiguration)
    limits: dict[str, int | float] = Field(default_factory=dict)
    mcp: MCPConfiguration = Field(default_factory=MCPConfiguration)
    remote_agents: RemoteAgentsConfiguration = Field(default_factory=RemoteAgentsConfiguration)
    telemetry: TelemetryConfiguration = Field(default_factory=TelemetryConfiguration)
    agent: AgentDefaults = Field(default_factory=AgentDefaults)

    def configured_provider_keys(self) -> dict[str, str]:
        return {
            identifier: credential.api_key
            for identifier, credential in self.providers.items()
            if credential.api_key
        }

    def configured_provider_bases(self) -> dict[str, str]:
        return {
            identifier: credential.base_url
            for identifier, credential in self.providers.items()
            if credential.base_url
        }


class AppSettingsUpdateRequest(SettingsUpdateRequest):
    """The daemon's settings request, plus the daemon-owned Composio key."""

    composio_api_key: str | None = None


class DictationUpdateRequest(BaseModel):
    """Opt-in/out of transcribing the composer's dictation on this machine."""

    enabled: bool


class DictationTimingConfiguration(AppConfigurationSection):
    """How long dictation waits before giving up, separated because these are what a slow machine must move."""

    minimum_transcription_timeout_seconds: float = Field(default=30.0)
    transcription_timeout_realtime_multiplier: float = Field(default=0.5)
    maximum_attempts: int = Field(default=2, ge=1)
    worker_shutdown_seconds: float = Field(default=2.0)


class DaemonConfiguration(AppConfigurationSection):
    """The daemon's own lifecycle timings, configurable in the file and never part of the library.

    The library must not carry these: they are properties of the daemon process, not of a
    session or the runtime.
    """

    # How long to wait for the daemon to come up after starting it.
    startup_seconds: float = Field(default=timing.DAEMON_STARTUP_SECONDS)
    # How often a probe retries a not-yet-listening daemon, and how long each connect may wait.
    probe_interval_seconds: float = Field(default=timing.DAEMON_PROBE_INTERVAL_SECONDS)
    probe_connect_seconds: float = Field(default=timing.DAEMON_PROBE_CONNECT_SECONDS)
    # How long an idle hosted session sleeps before it is let go (five hours).
    session_idle_sleep_seconds: float = Field(default=timing.SESSION_IDLE_SLEEP_SECONDS)


class DictationConfiguration(AppConfigurationSection):
    """Opt-in speech-to-text, transcribed locally. Off by default: the first use downloads about a gigabyte."""

    enabled: bool = Field(default=False)
    model: str = Field(default="mlx-community/parakeet-tdt-0.6b-v3")
    timing: DictationTimingConfiguration = Field(default_factory=DictationTimingConfiguration)


class ComposioConfiguration(AppConfigurationSection):
    """Composio's hosted MCP endpoint, exposed as an ordinary streamable_http server."""

    enabled: bool = Field(default=False)
    url: str = Field(default="https://connect.composio.dev/mcp")
    api_key: str = Field(default="", json_schema_extra={"secret": True})
    server_name: str = Field(default="composio")
    timeout_seconds: float = Field(default=60)

    @property
    def effective_api_key(self) -> str:
        return read_secret(COMPOSIO_API_KEY)


class EmailOAuthConfiguration(AppConfigurationSection):
    """OAuth2 for IMAP/SMTP XOAUTH2. Tokens live as secret files, not in this document.

    Proton Mail is not an issuer: it has no IMAP OAuth. Paid Proton uses Bridge and a password.
    """

    issuer: str = Field(default="")
    client_id: str = Field(default="")
    tenant: str = Field(default="common")
    token_url: str = Field(default="")
    authorize_url: str = Field(default="")
    scopes: list[str] = Field(default_factory=list)
    redirect_uri: str = Field(default="http://127.0.0.1:8765/callback")
    client_secret: str = Field(default="", json_schema_extra={"secret": True})
    refresh_token: str = Field(default="", json_schema_extra={"secret": True})


class EmailImapConfiguration(AppConfigurationSection):
    """The mailbox the mail client IDLEs on. The password is the secret file email.imap.password."""

    host: str = Field(default="")
    port: int = Field(default=993, ge=1, le=65535)
    username: str = Field(default="")
    password: str = Field(default="", json_schema_extra={"secret": True})
    mailbox: str = Field(default="INBOX")
    ssl: bool = Field(default=True)


class EmailSmtpConfiguration(AppConfigurationSection):
    """Where replies are sent. The password is the secret file email.smtp.password."""

    host: str = Field(default="")
    port: int = Field(default=587, ge=1, le=65535)
    username: str = Field(default="")
    password: str = Field(default="", json_schema_extra={"secret": True})
    start_tls: bool = Field(default=True)
    use_tls: bool = Field(default=False)


class ProvisionFlyConfiguration(AppConfigurationSection):
    """Fly.io app name and region for packaging/mail/provision.sh."""

    app: str = Field(default="langmesh-mail")
    region: str = Field(default="iad")


class ProvisionHetznerConfiguration(AppConfigurationSection):
    """Hetzner Cloud server create fields for packaging/mail/provision.sh."""

    image: str = Field(default="ubuntu-24.04")
    type: str = Field(default="cpx11")
    location: str = Field(default="fsn1")
    ssh_key: str = Field(default="")


class ProvisionDigitalOceanConfiguration(AppConfigurationSection):
    """DigitalOcean droplet create fields for packaging/mail/provision.sh."""

    region: str = Field(default="nyc1")
    ssh_key: str = Field(default="")


class ProvisionConfiguration(AppConfigurationSection):
    """How packaging/mail/provision.sh finds a host. Not used by the running daemon.

    Fill this in packaging/mail/configuration.yaml. host is an SSH target for a
    machine you already have. Cloud create still needs that provider's own token
    (FLY_API_TOKEN, HCLOUD_TOKEN, DIGITALOCEAN_ACCESS_TOKEN) because those CLIs
    read it.
    """

    host: str = Field(default="")
    name: str = Field(default="langmesh-mail")
    fly: ProvisionFlyConfiguration = Field(default_factory=ProvisionFlyConfiguration)
    hetzner: ProvisionHetznerConfiguration = Field(default_factory=ProvisionHetznerConfiguration)
    digitalocean: ProvisionDigitalOceanConfiguration = Field(
        default_factory=ProvisionDigitalOceanConfiguration
    )


class EmailConfiguration(AppConfigurationSection):
    """IMAP IDLE plus SMTP in front of the daemon: a client, not a second session embedder.

    Off by default. Passwords and OAuth refresh tokens live as secret files, not
    in this document. The mail process reads this section; the library never does.
    ``provider`` and ``model`` overlay the agent profile for mailbox sessions only.

    ``machine`` is this host's plus-tag. A new thread addressed to
    ``local+machine@domain`` starts a session here; a reply steers the same
    conversation. IMAP still logs in as the account without the plus.
    """

    enabled: bool = Field(default=False)
    address: str = Field(default="")
    machine: str = Field(default="")
    allow_from: list[str] = Field(default_factory=list)
    agent: str = Field(default="reviewer")
    provider: str = Field(default="")
    model: str = Field(default="")
    auth: Literal["password", "oauth"] = Field(default="password")
    working_directory: str = Field(default="")
    permission_mode: str = Field(default="automatic")
    idle_timeout_seconds: float = Field(default=60.0, gt=0)
    turn_timeout_seconds: float = Field(default=1800.0, gt=0)
    oauth: EmailOAuthConfiguration = Field(default_factory=EmailOAuthConfiguration)
    imap: EmailImapConfiguration = Field(default_factory=EmailImapConfiguration)
    smtp: EmailSmtpConfiguration = Field(default_factory=EmailSmtpConfiguration)

    @property
    def effective_enabled(self) -> bool:
        return self.enabled or bool(self.address.strip())

    @property
    def effective_address(self) -> str:
        return self.address.strip()

    @property
    def effective_machine(self) -> str:
        return self.machine.strip().lower()

    @property
    def effective_from_address(self) -> str:
        """SMTP From: the account's local part plus this host's machine slug.

        Replies stay tagged, so the next inbound mail still names this machine.
        """
        address = self.effective_address
        if not address:
            return ""
        local, domain = _untagged_local(address)
        slug = self.effective_machine
        if not domain:
            return f"{local}+{slug}" if slug else local
        if not slug:
            return f"{local}@{domain}"
        return f"{local}+{slug}@{domain}"

    @field_validator("machine")
    @classmethod
    def _machine_slug(cls, value: str) -> str:
        slug = value.strip().lower()
        if not slug:
            return ""
        if not _MACHINE_SLUG.fullmatch(slug):
            raise ValueError(
                "must be a lowercase slug: a letter, then letters, digits, or hyphens, at most 32 characters"
            )
        return slug

    @model_validator(mode="after")
    def _plus_tag_matches_machine(self) -> EmailConfiguration:
        tag = _address_plus_tag(self.effective_address)
        slug = self.effective_machine
        if tag and slug and tag != slug:
            raise ValueError("email.address plus-tag must equal email.machine")
        return self

    @property
    def effective_agent(self) -> str:
        return self.agent.strip() or "reviewer"

    @property
    def effective_provider(self) -> str:
        """Catalogue provider overlay for mailbox sessions. Empty keeps the agent profile's provider."""
        return self.provider.strip()

    @property
    def effective_model(self) -> str:
        """Catalogue model overlay for mailbox sessions. Empty keeps the agent profile's model."""
        return self.model.strip()

    @property
    def uses_oauth(self) -> bool:
        return self.auth.strip().lower() == "oauth"

    @property
    def is_proton(self) -> bool:
        """Proton Mail has no public IMAP; inferred hosts are Proton Bridge on this machine."""
        return _is_proton(self.effective_address)

    @property
    def effective_oauth_issuer(self) -> str:
        named = self.oauth.issuer.strip().lower()
        if named:
            return named
        return _OAUTH_DOMAINS.get(_domain(self.effective_address), "")

    @property
    def effective_oauth_client_id(self) -> str:
        return self.oauth.client_id.strip()

    @property
    def effective_oauth_client_secret(self) -> str:
        return read_secret(EMAIL_OAUTH_CLIENT_SECRET)

    @property
    def effective_oauth_refresh_token(self) -> str:
        return read_secret(EMAIL_OAUTH_REFRESH_TOKEN)

    @property
    def effective_oauth_redirect_uri(self) -> str:
        return self.oauth.redirect_uri.strip() or "http://127.0.0.1:8765/callback"

    @property
    def effective_working_directory(self) -> str:
        """Where mail sessions run. Empty leaves the daemon's current directory."""
        return self.working_directory.strip()

    @property
    def effective_allow_from(self) -> list[str]:
        return [item.strip() for item in self.allow_from if item.strip()]

    @property
    def effective_imap_host(self) -> str:
        return self.imap.host.strip() or _hosts_for(self.effective_address)[0]

    @property
    def effective_imap_username(self) -> str:
        return self.imap.username.strip() or _account_login(self.effective_address)

    @property
    def effective_imap_password(self) -> str:
        if self.uses_oauth:
            return ""
        return compact_mail_secret(read_secret(EMAIL_IMAP_PASSWORD))

    @property
    def effective_smtp_host(self) -> str:
        return self.smtp.host.strip() or _hosts_for(self.effective_address)[1]

    @property
    def effective_smtp_username(self) -> str:
        return self.smtp.username.strip() or self.effective_imap_username

    @property
    def effective_smtp_password(self) -> str:
        if self.uses_oauth:
            return ""
        secret = compact_mail_secret(read_secret(EMAIL_SMTP_PASSWORD))
        if secret:
            return secret
        inferred = _hosts_for(self.effective_address)[1]
        if inferred and self.effective_smtp_host == inferred:
            return self.effective_imap_password
        return ""

    @property
    def effective_imap_port(self) -> int:
        if self.imap.port != 993:
            return self.imap.port
        return 1143 if _is_proton(self.effective_address) else self.imap.port

    @property
    def effective_imap_ssl(self) -> bool:
        return self.imap.ssl

    @property
    def effective_imap_mailbox(self) -> str:
        return self.imap.mailbox

    @property
    def effective_smtp_port(self) -> int:
        if self.smtp.port != 587:
            return self.smtp.port
        return 1025 if _is_proton(self.effective_address) else self.smtp.port

    @property
    def effective_smtp_use_tls(self) -> bool:
        if self.smtp.use_tls:
            return True
        return self.effective_smtp_port == 465

    @property
    def effective_smtp_start_tls(self) -> bool:
        if self.effective_smtp_use_tls:
            return False
        return self.smtp.start_tls
