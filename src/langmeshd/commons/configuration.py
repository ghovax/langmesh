"""The app's own configuration sections: features the daemon hosts but the library never models.

The library's Configuration carries only what the runtime itself reads. Dictation and Composio
are daemon-hosted features, so their sections live here, read and written straight from the
shared configuration file.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from langmesh.base.secrets import (
    COMPOSIO_API_KEY,
    EMAIL_IMAP_PASSWORD,
    EMAIL_SMTP_PASSWORD,
    read_secret,
)
from langmesh.protocol.dtos import SettingsUpdateRequest
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
}


def _hosts_for(address: str) -> tuple[str, str]:
    if "@" not in address:
        return "", ""
    return _MAIL_HOSTS.get(address.rsplit("@", 1)[-1].lower(), ("", ""))


def _account_login(address: str) -> str:
    """Gmail IMAP/SMTP auth is the account, not a plus-address alias used as From."""
    if "@" not in address:
        return address
    local, domain = address.rsplit("@", 1)
    if domain.lower() in {"gmail.com", "googlemail.com"} and "+" in local:
        return f"{local.split('+', 1)[0]}@{domain}"
    return address


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


class AppSettingsUpdateRequest(SettingsUpdateRequest):
    """The library's settings request, plus the app-owned Composio key."""

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

    Off by default. Passwords live as secret files, not in this document. The mail
    process reads this section; the library never does. ``provider`` and ``model``
    overlay the agent profile for mailbox sessions only.
    """

    enabled: bool = Field(default=False)
    address: str = Field(default="")
    allow_from: list[str] = Field(default_factory=list)
    agent: str = Field(default="reviewer")
    provider: str = Field(default="")
    model: str = Field(default="")
    working_directory: str = Field(default="")
    permission_mode: str = Field(default="automatic")
    idle_timeout_seconds: float = Field(default=60.0, gt=0)
    turn_timeout_seconds: float = Field(default=1800.0, gt=0)
    imap: EmailImapConfiguration = Field(default_factory=EmailImapConfiguration)
    smtp: EmailSmtpConfiguration = Field(default_factory=EmailSmtpConfiguration)

    @property
    def effective_enabled(self) -> bool:
        return self.enabled or bool(self.address.strip())

    @property
    def effective_address(self) -> str:
        return self.address.strip()

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
        return compact_mail_secret(read_secret(EMAIL_IMAP_PASSWORD))

    @property
    def effective_smtp_host(self) -> str:
        return self.smtp.host.strip() or _hosts_for(self.effective_address)[1]

    @property
    def effective_smtp_username(self) -> str:
        return self.smtp.username.strip() or self.effective_imap_username

    @property
    def effective_smtp_password(self) -> str:
        secret = compact_mail_secret(read_secret(EMAIL_SMTP_PASSWORD))
        if secret:
            return secret
        inferred = _hosts_for(self.effective_address)[1]
        if inferred and self.effective_smtp_host == inferred:
            return self.effective_imap_password
        return ""

    @property
    def effective_imap_port(self) -> int:
        return self.imap.port

    @property
    def effective_imap_ssl(self) -> bool:
        return self.imap.ssl

    @property
    def effective_imap_mailbox(self) -> str:
        return self.imap.mailbox

    @property
    def effective_smtp_port(self) -> int:
        return self.smtp.port

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
