"""Runtime configuration (REQUIREMENTS.md §10, NFR-DEP-03, NFR-SEC-11).

All settings come from ``LAKELOOM_*`` environment variables; no ``.env`` file is read.
Secrets (``om_bot_token``, ``git_token``) may instead be supplied as
``LAKELOOM_<NAME>_FILE`` pointing at a file (Docker/Kubernetes secrets). Trailing
whitespace is stripped from the file content, and setting both forms is an error.
Secrets are ``SecretStr`` so they never appear in ``repr`` or ``model_dump``.
"""

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import AnyHttpUrl, SecretStr, ValidationError, field_validator, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_PREFIX = "LAKELOOM_"
SECRET_FILE_FIELDS = ("om_bot_token", "git_token")


def _secret_file_error(field: str, message: str) -> ValidationError:
    return ValidationError.from_exception_data(
        "Settings",
        [
            InitErrorDetails(
                type=PydanticCustomError("secret_file", message),
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
    scope_group_map: dict[str, list[str]] = {}
    db_url: str = "sqlite:////data/lakeloom.db"
    git_remote: str | None = None
    git_branch: str = "main"
    git_mode: Literal["direct", "pull_request"] = "pull_request"
    git_ssh_key_file: Path | None = None
    git_token: SecretStr | None = None
    forge: Literal["github", "gitlab", "gitea", "none"] = "github"
    profile_samples: bool = False
    # JSON list or comma-separated string.
    sensitive_tags: Annotated[list[str], NoDecode] = ["PII.*"]
    max_proposal_ops: int = 50
    triplestore_enabled: bool = False
    # None means "auto": False in production, True in development (resolved after validation).
    stdio_enabled: bool | None = None
    otel_exporter_otlp_endpoint: str | None = None

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
