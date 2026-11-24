"""Explicit local configuration. Provider access and internet identity fail closed."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import json
import ipaddress
import re
from urllib.parse import urlsplit

import idna


def _canonical_public_origin(value: str) -> str:
    if any(ord(character) <= 32 or ord(character) == 127 for character in value) or "\\" in value:
        raise ValueError("Hosted origins cannot contain whitespace, controls or backslashes")
    parsed = urlsplit(value)
    port = parsed.port
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or port is not None and not 1 <= port <= 65535 or "%" in parsed.hostname):
        raise ValueError("Hosted public origin requires a credential-free HTTPS origin")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{ipaddress.IPv6Address(host).compressed}]"
    elif re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]*)", host.rstrip(".").rsplit(".", 1)[-1]):
        # Reject WHATWG's shortened, hexadecimal and octal IPv4 aliases. A
        # canonical dotted address has the same authority in Python/browser.
        host = str(ipaddress.IPv4Address(host))
    else:
        host = idna.encode(host, uts46=True, transitional=False, std3_rules=True).decode("ascii").lower()
        # UTS 46 can turn fullwidth digits or dots into a numeric host. Apply
        # the same strict IPv4 check after mapping so browser aliases are not
        # returned as apparently canonical DNS names.
        if re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]*)", host.rstrip(".").rsplit(".", 1)[-1]):
            host = str(ipaddress.IPv4Address(host))
    return f"https://{host}" + (f":{port}" if port not in {None, 443} else "")


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_DATABASE_URL", "postgresql+psycopg://round_robin:round_robin_local@127.0.0.1:55432/round_robin"))
    development: bool = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_ENV", "development") == "development")
    execution_mode: str = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_EXECUTION_MODE", "simulated"))
    paid_access_enabled: bool = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_PAID_ACCESS", "0") == "1")
    provider_config_file: Path | None = field(default_factory=lambda: Path(os.environ["ROUND_ROBIN_PROVIDER_CONFIG_FILE"]) if os.environ.get("ROUND_ROBIN_PROVIDER_CONFIG_FILE") else None)
    deployment_epoch: int = field(default_factory=lambda: int(os.environ.get("ROUND_ROBIN_DEPLOYMENT_EPOCH", "1")))
    artifact_directory: Path = field(default_factory=lambda: Path(os.environ.get("ROUND_ROBIN_ARTIFACT_DIRECTORY", ".local/artifacts")))
    durability_control_directory: Path | None = field(default_factory=lambda: Path(os.environ["ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY"]) if os.environ.get("ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY") else None)
    command_hash_key: str = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_COMMAND_HASH_KEY", "localhost-development-command-hash-key"), repr=False)
    allowed_origins: tuple[str, ...] = field(default_factory=lambda: tuple(filter(None, os.environ.get("ROUND_ROBIN_ALLOWED_ORIGINS", "http://127.0.0.1:3000,http://localhost:3000").split(","))))
    hosted_enabled: bool = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_HOSTED_ENABLED", "0") == "1")
    public_origin: str = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_PUBLIC_ORIGIN", "http://127.0.0.1:3000"))
    oidc_issuer: str = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_OIDC_ISSUER", ""))
    oidc_client_id: str = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_OIDC_CLIENT_ID", ""))
    oidc_client_secret: str = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_OIDC_CLIENT_SECRET", ""), repr=False)
    invite_allowlist: tuple[tuple[str, str], ...] = field(default_factory=lambda: tuple(tuple(item) for item in json.loads(os.environ.get("ROUND_ROBIN_INVITE_ALLOWLIST", "[]"))))
    hosted_quotas_configured: bool = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_HOSTED_QUOTAS_CONFIGURED", "0") == "1")
    private_storage_configured: bool = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_PRIVATE_STORAGE_CONFIGURED", "0") == "1")
    retention_configured: bool = field(default_factory=lambda: os.environ.get("ROUND_ROBIN_RETENTION_CONFIGURED", "0") == "1")
    max_queued_per_owner: int = 3
    max_meetings_per_owner: int = 100
    schema_version: str = "1"

    def __post_init__(self) -> None:
        # Browser Origin serialization lowercases hosts, omits default HTTPS
        # ports and never contains a trailing slash. Normalize valid web
        # origins once so CORS, CSRF, redirects and cookie hosts agree.
        if self.development:
            return
        try:
            object.__setattr__(self, "public_origin", _canonical_public_origin(self.public_origin))
        except (ValueError, UnicodeError):
            # Preserve invalid configuration for the explicit startup check.
            return

    def validate(self) -> None:
        if not self.development:
            if not all((self.hosted_enabled, self.oidc_client_id, self.oidc_client_secret, self.invite_allowlist, self.hosted_quotas_configured, self.private_storage_configured, self.retention_configured)):
                raise ValueError("Hosted startup requires explicit OIDC, invitations, quotas, private storage and retention configuration")
            if _canonical_public_origin(self.public_origin) != self.public_origin:
                raise ValueError("Hosted public origin must use a canonical browser origin")
            for address in (self.public_origin, self.oidc_issuer):
                parsed = urlsplit(address)
                if (any(ord(character) <= 32 or ord(character) == 127 for character in address) or "\\" in address
                        or parsed.scheme != "https" or not parsed.hostname or "%" in parsed.hostname
                        or parsed.username or parsed.password or parsed.query or parsed.fragment
                        or parsed.port is not None and not 1 <= parsed.port <= 65535):
                    raise ValueError("Hosted public origin and issuer require credential-free HTTPS URLs")
            if urlsplit(self.public_origin).path not in {"", "/"}:
                raise ValueError("Hosted public origin must not contain a path")
            if self.command_hash_key == "localhost-development-command-hash-key" or len(self.command_hash_key) < 32:
                raise ValueError("Hosted command tombstones require a persistent private hash key of at least 32 characters")
            if any(len(identity) != 2 or identity[0] != self.oidc_issuer or not identity[1] for identity in self.invite_allowlist):
                raise ValueError("Invitations must bind configured issuer and immutable subject")
        if self.execution_mode not in {"simulated", "live"}:
            raise ValueError("Unknown execution mode")
        if self.execution_mode == "live" and not self.paid_access_enabled:
            raise ValueError("Live execution requires explicit operator funding configuration")


settings = Settings()
