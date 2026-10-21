"""Distribution metadata must describe the same release and configuration."""

import json
from pathlib import Path

from deputy_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_versions_agree() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    registry = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert manifest["version"] == registry["version"] == __version__
    assert all(package["version"] == __version__ for package in registry["packages"])
    assert (ROOT / manifest["server"]["entry_point"]).is_file()


def test_bundle_configuration_maps_to_deputy_environment() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    config = manifest["user_config"]
    env = manifest["server"]["mcp_config"]["env"]
    assert set(config) == {
        "calendar_url",
        "oauth_client_id",
        "oauth_client_secret",
        "api_token",
        "base_url",
        "allow_writes",
    }
    assert env == {f"DEPUTY_{key.upper()}": "${user_config." + key + "}" for key in config}
    assert config["allow_writes"]["default"] is False
    for key in ("calendar_url", "oauth_client_secret", "api_token"):
        assert config[key]["sensitive"] is True
    assert all(not field["required"] for field in config.values())
