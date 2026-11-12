"""Runtime configuration (REQUIREMENTS.md §10, NFR-DEP-03, NFR-SEC-11).

All settings come from ``LAKELOOM_*`` environment variables; no ``.env`` file is read.
Secrets (``om_bot_token``, ``git_token``, ``confirm_secret``) and ``db_url`` (which may
embed a password) may instead be supplied as ``LAKELOOM_<NAME>_FILE`` pointing at a file
(Docker/Kubernetes secrets). Trailing whitespace is stripped from the file content, and
setting both forms is an error. Secrets are ``SecretStr`` so they never appear in ``repr``
or ``model_dump``;
``db_url`` and ``git_remote`` stay plain ``str`` (callers use them as URLs) but are
excluded from ``repr``.
"""

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from lakeloom.scopes import SCOPES

ENV_PREFIX = "LAKELOOM_"
SECRET_FILE_FIELDS = (
    "om_bot_token",
    "git_token",
    "db_url",
    "confirm_secret",
    "oidc_client_secret",
)


class ConfigError(Exception):
    """Required configuration is missing or unusable at startup."""


def _secret_file_error(field: str, message: str) -> ValidationError:
    return ValidationError.from_exception_data(
        "Settings",
        [
            InitErrorDetails(
                type=PydanticCustomError("secret_file", "{message}", {"message": message}),
                loc=(field,),
                input=None,
            )
        ],
    )


def _with_secret_files(values: dict[str, Any]) -> dict[str, Any]:
    merged = dict(values)
    for name in SECRET_FILE_FIELDS:
        var = f"{ENV_PREFIX}{name}".upper()
        file_var = f"{var}_FILE"
        path = os.environ.get(file_var)
        if not path:
            continue
        if merged.get(name) is not None or os.environ.get(var):
            raise _secret_file_error(name, f"{var} and {file_var} are both set; set only one")
        try:
            merged[name] = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise _secret_file_error(
                name, f"cannot read {file_var} ({path}): {exc.strerror}"
            ) from None
    return merged


class Settings(BaseSettings):
    """Lakeloom settings. ``Settings()`` with an empty environment always succeeds."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, extra="ignore", hide_input_in_errors=True
    )

    env: Literal["development", "production"] = "development"
    config_file: Path | None = None
    public_url: AnyHttpUrl | None = None
    om_url: AnyHttpUrl | None = None
    om_bot_token: SecretStr | None = None
    oidc_issuer: str | None = None
    oidc_audience: str = "lakeloom"
    oidc_token_exchange: bool = True
    # M2b: on-behalf-of OM tokens for human reviewers (ADR 0012, NFR-SEC-08..10).
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_token_endpoint: str | None = None
    om_audience: str = "openmetadata"
    # Sent as ``scope`` in the exchange; Keycloak 26 V2 needs the client scope that carries
    # the OM audience mapper. None omits the field.
    om_exchange_scope: str | None = "openmetadata-audience"
    om_forward_user_token: bool = False
    scope_group_map: dict[str, list[str]] = {}
    # May embed credentials: kept out of repr (NFR-SEC-11).
    db_url: str = Field(default="sqlite:////data/lakeloom.db", repr=False)
    git_remote: str | None = Field(default=None, repr=False)
    git_branch: str = "main"
    git_mode: Literal["direct", "pull_request"] = "pull_request"
    git_ssh_key_file: Path | None = None
    git_token: SecretStr | None = None
    forge: Literal["github", "gitlab", "gitea", "none"] = "github"
    profile_samples: bool = False
    # JSON list or comma-separated string.
    sensitive_tags: Annotated[list[str], NoDecode] = ["PII.*"]
    max_proposal_ops: int = 50
    # M2a: HMAC key of the stateless confirm tokens (FR-AGT-03). Required in production.
    confirm_secret: SecretStr | None = None
    confirm_ttl_seconds: int = Field(600, ge=30, le=3600)
    # M1a additions (docs/decisions/0008-lake-sources-design.md)
    profile_topk: int = Field(default=10, ge=1, le=1000)
    profile_low_cardinality: int = Field(default=50, ge=1, le=100_000)
    profile_value_max_length: int = Field(default=64, ge=1, le=10_000)
    scan_concurrency: int = Field(default=8, ge=1, le=256)
    triplestore_enabled: bool = False
    model_cache_ttl_seconds: int = Field(60, ge=0, le=3600)
    # None means "auto": False in production, True in development (resolved after validation).
    stdio_enabled: bool | None = None
    otel_exporter_otlp_endpoint: str | None = None
    # M1c: the configured local principal of the stdio transport (NFR-SEC-04). JSON or comma list.
    stdio_principal: str = Field(default="local", min_length=1)
    stdio_scopes: Annotated[list[str], NoDecode] = ["lakeloom:read"]

    # M0 additions (docs/decisions/0002-additional-m0-settings.md)
    data_dir: Path = Path("/data")
    oidc_jwks_url: str | None = None
    oidc_agent_claim: str = "client_id"
    oidc_clock_skew_seconds: int = 60
    log_level: str = "INFO"
    host: str = "0.0.0.0"  # noqa: S104 - container service binds all interfaces
    port: int = 8000

    def __init__(self, **values: Any) -> None:
        # *_FILE resolution happens before validation so that errors never carry the
        # raw input (which holds plaintext secrets) -- NFR-SEC-11.
        super().__init__(**_with_secret_files(values))

    @field_validator("sensitive_tags", mode="before")
    @classmethod
    def _parse_sensitive_tags(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.lstrip().startswith("["):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, str):
            return json.loads(value)
        return value

    @field_validator("stdio_scopes", mode="before")
    @classmethod
    def _parse_stdio_scopes(cls, value: Any) -> Any:
        if isinstance(value, str) and value.lstrip().startswith("["):
            return json.loads(value)
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("stdio_scopes", mode="after")
    @classmethod
    def _check_stdio_scopes(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(SCOPES))
        if unknown:
            raise ConfigError(
                f"LAKELOOM_STDIO_SCOPES names unknown scopes {unknown}; known: {list(SCOPES)}"
            )
        return value

    @model_validator(mode="after")
    def _resolve_stdio(self) -> Self:
        if self.stdio_enabled is None:
            self.stdio_enabled = self.env != "production"
        return self

    @property
    def public_base_url(self) -> str | None:
        """``public_url`` without the trailing slash pydantic adds, or None."""
        return None if self.public_url is None else str(self.public_url).rstrip("/")


def get_settings() -> Settings:
    """Build settings from the environment (no caching; callers pass Settings explicitly)."""
    return Settings()
