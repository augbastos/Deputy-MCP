"""Tests for dotenv-file configuration loading (DEPUTY_ENV_FILE / cwd .env)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deputy_mcp.client.errors import DeputyConfigError
from deputy_mcp.config import DeputyConfig

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


def test_file_only_supplies_deputy_variables(tmp_path: Path) -> None:
    env_file = _write_env_file(
        tmp_path / "deputy.env", extra="OTHER_SECRET=nope\nDEPUTY_ALLOW_WRITES=true\n"
    )
    config = DeputyConfig.from_env({"DEPUTY_ENV_FILE": str(env_file)})
    assert config.allow_writes is True
