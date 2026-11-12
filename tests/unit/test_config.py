"""Settings tests (NFR-DEP-03 defaults, NFR-SEC-11 secrets, §10 configuration)."""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from lakeloom.config import Settings, get_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("LAKELOOM_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)  # no stray .env can be picked up


def test_nfr_dep_03_defaults_match_spec() -> None:
    s = Settings()

    assert s.oidc_audience == "lakeloom"
    assert s.db_url == "sqlite:////data/lakeloom.db"
    assert s.git_branch == "main"
    assert s.git_mode == "pull_request"
    assert s.forge == "github"
    assert s.profile_samples is False
    assert s.sensitive_tags == ["PII.*"]
    assert s.max_proposal_ops == 50
    assert s.triplestore_enabled is False
    assert s.stdio_enabled is True
    assert s.env == "development"
    assert s.oidc_token_exchange is True
    assert s.scope_group_map == {}
    assert s.data_dir == Path("/data")
    assert s.oidc_agent_claim == "client_id"
    assert s.oidc_clock_skew_seconds == 60
    assert s.port == 8000


def test_stdio_disabled_by_default_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKELOOM_ENV", "production")
    assert Settings().stdio_enabled is False

    monkeypatch.setenv("LAKELOOM_STDIO_ENABLED", "true")
    assert Settings().stdio_enabled is True


def test_nfr_sec_11_secret_from_file_strips_trailing_newline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "tok"
    f.write_text("abc\n")
    monkeypatch.setenv("LAKELOOM_OM_BOT_TOKEN_FILE", str(f))

    s = Settings()

    assert s.om_bot_token is not None
    assert s.om_bot_token.get_secret_value() == "abc"


def test_nfr_sec_11_git_token_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "gt"
    f.write_text("ghp_x\r\n")
    monkeypatch.setenv("LAKELOOM_GIT_TOKEN_FILE", str(f))

    s = Settings()

    assert s.git_token is not None
    assert s.git_token.get_secret_value() == "ghp_x"


def test_nfr_sec_11_secret_and_file_both_set_is_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "tok"
    f.write_text("abc\n")
    monkeypatch.setenv("LAKELOOM_OM_BOT_TOKEN_FILE", str(f))
    monkeypatch.setenv("LAKELOOM_OM_BOT_TOKEN", "zzz")

    with pytest.raises(ValidationError, match="LAKELOOM_OM_BOT_TOKEN"):
        Settings()


def test_nfr_sec_11_unreadable_secret_file_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKELOOM_OM_BOT_TOKEN_FILE", "/nonexistent/secret")

    with pytest.raises(ValidationError, match="LAKELOOM_OM_BOT_TOKEN_FILE"):
        Settings()


def test_nfr_sec_11_secrets_not_in_repr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "tok"
    f.write_text("abc\n")
    monkeypatch.setenv("LAKELOOM_OM_BOT_TOKEN_FILE", str(f))
    monkeypatch.setenv("LAKELOOM_GIT_TOKEN", "abc")

    s = Settings()

    assert "abc" not in repr(s)
    assert "abc" not in str(s.model_dump())


def test_scope_group_map_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKELOOM_SCOPE_GROUP_MAP", '{"ontology-reviewers":["lakeloom:review"]}')

    assert Settings().scope_group_map == {"ontology-reviewers": ["lakeloom:review"]}


def test_sensitive_tags_accepts_json_and_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKELOOM_SENSITIVE_TAGS", '["PII.*","GDPR.*"]')
    assert Settings().sensitive_tags == ["PII.*", "GDPR.*"]

    monkeypatch.setenv("LAKELOOM_SENSITIVE_TAGS", "PII.*, GDPR.*")
    assert Settings().sensitive_tags == ["PII.*", "GDPR.*"]


def test_public_base_url_has_no_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().public_base_url is None

    monkeypatch.setenv("LAKELOOM_PUBLIC_URL", "http://localhost:8000")
    assert Settings().public_base_url == "http://localhost:8000"


def test_get_settings_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKELOOM_PORT", "9001")

    assert get_settings().port == 9001


def test_nfr_sec_11_validation_errors_do_not_echo_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LAKELOOM_GIT_TOKEN", "gitsecret")
    monkeypatch.setenv("LAKELOOM_OM_BOT_TOKEN", "supersecret")
    monkeypatch.setenv("LAKELOOM_OM_BOT_TOKEN_FILE", "/nonexistent/secret")

    with pytest.raises(ValidationError) as unreadable:
        Settings()
    assert "LAKELOOM_OM_BOT_TOKEN" in str(unreadable.value)

    f = tmp_path / "tok"
    f.write_text("filesecret\n")
    monkeypatch.setenv("LAKELOOM_OM_BOT_TOKEN_FILE", str(f))
    with pytest.raises(ValidationError) as both:
        Settings()

    for exc in (unreadable, both):
        text = str(exc.value) + repr(exc.value) + str(exc.value.errors())
        assert "supersecret" not in text
        assert "gitsecret" not in text
        assert "filesecret" not in text


def test_nfr_sec_11_db_url_and_git_remote_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKELOOM_DB_URL", "postgresql://u:hunter2@h/db")
    monkeypatch.setenv("LAKELOOM_GIT_REMOTE", "https://bot:gitpass@git.test/o/r.git")

    s = Settings()

    assert s.db_url == "postgresql://u:hunter2@h/db"  # still a plain str for callers
    for text in (repr(s), str(s)):
        assert "hunter2" not in text
        assert "gitpass" not in text


def test_nfr_sec_11_db_url_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "db_url"
    f.write_text("postgresql://u:hunter2@h/db\n")
    monkeypatch.setenv("LAKELOOM_DB_URL_FILE", str(f))

    assert Settings().db_url == "postgresql://u:hunter2@h/db"


def test_nfr_sec_11_db_url_and_file_both_set_is_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "db_url"
    f.write_text("postgresql://u:fromfile@h/db\n")
    monkeypatch.setenv("LAKELOOM_DB_URL_FILE", str(f))
    monkeypatch.setenv("LAKELOOM_DB_URL", "postgresql://u:hunter2@h/db")

    with pytest.raises(ValidationError, match="LAKELOOM_DB_URL and LAKELOOM_DB_URL_FILE") as exc:
        Settings()
    text = str(exc.value) + repr(exc.value)
    assert "hunter2" not in text
    assert "fromfile" not in text


BOUNDS = [
    ("LAKELOOM_PROFILE_TOPK", "profile_topk", 1, 1000),
    ("LAKELOOM_PROFILE_LOW_CARDINALITY", "profile_low_cardinality", 1, 100_000),
    ("LAKELOOM_PROFILE_VALUE_MAX_LENGTH", "profile_value_max_length", 1, 10_000),
    ("LAKELOOM_SCAN_CONCURRENCY", "scan_concurrency", 1, 256),
]


@pytest.mark.parametrize(("env", "field", "low", "high"), BOUNDS)
def test_fr_src_09_profile_and_scan_settings_are_bounded(
    monkeypatch: pytest.MonkeyPatch, env: str, field: str, low: int, high: int
) -> None:
    for ok in (low, high):
        monkeypatch.setenv(env, str(ok))
        assert getattr(Settings(), field) == ok
    for bad in (low - 1, high + 1):
        monkeypatch.setenv(env, str(bad))
        with pytest.raises(ValidationError):
            Settings()


def test_m2b_obo_defaults() -> None:
    s = Settings()

    assert s.oidc_client_id is None
    assert s.oidc_client_secret is None
    assert s.oidc_token_endpoint is None
    assert s.om_audience == "openmetadata"
    assert s.om_exchange_scope == "openmetadata-audience"
    assert s.om_forward_user_token is False


def test_m2b_oidc_client_secret_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "cs"
    f.write_text("s3cret\n")
    monkeypatch.setenv("LAKELOOM_OIDC_CLIENT_SECRET_FILE", str(f))

    s = Settings()

    assert s.oidc_client_secret is not None
    assert s.oidc_client_secret.get_secret_value() == "s3cret"
    assert "s3cret" not in repr(s)
