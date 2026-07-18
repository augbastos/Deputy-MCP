"""Tests for dotenv-file configuration loading (DEPUTY_ENV_FILE / cwd .env)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deputy_mcp.client.errors import DeputyConfigError
from deputy_mcp.config import DeputyConfig, _default_token_store_path

BASE_URL = "https://cloud-nine-cafe.eu.deputy.com"


def _write_env_file(path: Path, *, token: str = "file-token", extra: str = "") -> Path:
    path.write_text(
        f"DEPUTY_API_TOKEN={token}\nDEPUTY_BASE_URL={BASE_URL}\n{extra}",
        encoding="utf-8",
    )
    return path


def test_explicit_env_file_is_loaded(tmp_path: Path) -> None:
    env_file = _write_env_file(tmp_path / "deputy.env")
    config = DeputyConfig.from_env({"DEPUTY_ENV_FILE": str(env_file)})
    assert config.token() == "file-token"
    assert config.base_url == BASE_URL


def test_real_env_vars_win_over_file(tmp_path: Path) -> None:
    env_file = _write_env_file(tmp_path / "deputy.env")
    config = DeputyConfig.from_env(
        {"DEPUTY_ENV_FILE": str(env_file), "DEPUTY_API_TOKEN": "env-token"}
    )
    assert config.token() == "env-token"
    assert config.base_url == BASE_URL


def test_explicit_env_file_missing_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "nope.env"
    with pytest.raises(DeputyConfigError, match="does not exist"):
        DeputyConfig.from_env({"DEPUTY_ENV_FILE": str(missing)})


def test_explicit_mapping_never_auto_loads_cwd_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test/caller passing an explicit mapping must stay hermetic even when the
    current directory holds a .env with credentials."""
    _write_env_file(tmp_path / ".env", token="cwd-secret")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DeputyConfigError, match="DEPUTY_API_TOKEN"):
        DeputyConfig.from_env({})


def test_process_env_auto_loads_cwd_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env_file(tmp_path / ".env", token="cwd-token")
    monkeypatch.chdir(tmp_path)
    for var in (
        "DEPUTY_API_TOKEN",
        "DEPUTY_BASE_URL",
        "DEPUTY_ENV_FILE",
        "DEPUTY_ALLOW_WRITES",
        "DEPUTY_ALLOW_CUSTOM_HOST",
    ):
        monkeypatch.delenv(var, raising=False)
    config = DeputyConfig.from_env()
    assert config.token() == "cwd-token"
    assert config.base_url == BASE_URL


def test_explicit_env_file_supplies_security_flags(tmp_path: Path) -> None:
    """An EXPLICITLY named DEPUTY_ENV_FILE is trusted: security-sensitive flags (here
    DEPUTY_ALLOW_WRITES) from it ARE honored, and non-DEPUTY keys are ignored."""
    env_file = tmp_path / "deputy.env"
    env_file.write_text(
        f"""DEPUTY_API_TOKEN=file-token
DEPUTY_BASE_URL={BASE_URL}
OTHER_SECRET=nope
DEPUTY_ALLOW_WRITES=true
""",
        encoding="utf-8",
    )
    config = DeputyConfig.from_env({"DEPUTY_ENV_FILE": str(env_file)})
    assert config.allow_writes is True


def _clear_deputy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "DEPUTY_API_TOKEN",
        "DEPUTY_BASE_URL",
        "DEPUTY_ENV_FILE",
        "DEPUTY_ALLOW_WRITES",
        "DEPUTY_ALLOW_CUSTOM_HOST",
        "DEPUTY_TOKEN_STORE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_cwd_env_does_not_enable_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A BARE cwd .env (auto-loaded, no explicit DEPUTY_ENV_FILE) must NOT flip
    DEPUTY_ALLOW_WRITES on -- a stdio server's cwd is host-controlled, so an untrusted
    directory could otherwise turn writes on. Credential keys still load normally."""
    (tmp_path / ".env").write_text(
        f"""DEPUTY_API_TOKEN=cwd-token
DEPUTY_BASE_URL={BASE_URL}
DEPUTY_ALLOW_WRITES=true
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    _clear_deputy_env(monkeypatch)
    config = DeputyConfig.from_env()
    # The token (a credential-loading convenience key) still comes from the cwd .env...
    assert config.token() == "cwd-token"
    # ...but the security-sensitive flag from that same file is ignored.
    assert config.allow_writes is False


def test_cwd_env_cannot_flip_security_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an auto-loaded cwd .env cannot flip allow_writes, allow_custom_host,
    or redirect the OAuth token_store. All three fall back to their safe defaults while
    the credential still loads."""
    attacker_store = tmp_path / "attacker" / "token.json"
    (tmp_path / ".env").write_text(
        f"""DEPUTY_API_TOKEN=cwd-token
DEPUTY_BASE_URL={BASE_URL}
DEPUTY_ALLOW_WRITES=true
DEPUTY_ALLOW_CUSTOM_HOST=true
DEPUTY_TOKEN_STORE={attacker_store}
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    _clear_deputy_env(monkeypatch)
    config = DeputyConfig.from_env()
    assert config.token() == "cwd-token"
    assert config.allow_writes is False
    assert config.allow_custom_host is False
    # The token store was NOT redirected to the attacker path; it stays at the default.
    assert config.token_store_path == _default_token_store_path()
    assert config.token_store_path != attacker_store
