"""Transport selection and remote-bind safeguards without opening sockets."""

from unittest.mock import Mock

import pytest

from deputy_mcp import cli


@pytest.fixture(autouse=True)
def transport_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for suffix in ("TRANSPORT", "HOST", "PORT", "ALLOW_REMOTE"):
        monkeypatch.delenv(f"DEPUTY_MCP_{suffix}", raising=False)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Mock:
    import deputy_mcp.server as server_pkg

    fake = Mock()
    monkeypatch.setattr(server_pkg, "create_server", lambda: fake)
    return fake


@pytest.mark.parametrize("argv", [[], ["serve"]])
def test_defaults(argv: list[str], server: Mock, capsys: pytest.CaptureFixture[str]) -> None:
    args = cli.build_parser().parse_args(argv)
    assert (args.transport, args.host, args.port, args.allow_remote) == (
        "stdio",
        "127.0.0.1",
        8000,
        False,
    )
    assert cli.main(argv) == 0
    server.run.assert_called_once_with(transport="stdio", show_banner=False)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "deputy-mcp: starting MCP server on stdio.\n"


@pytest.mark.parametrize("argv", [[], ["serve"]])
def test_env_fallback(argv: list[str], monkeypatch: pytest.MonkeyPatch, server: Mock) -> None:
    monkeypatch.setenv("DEPUTY_MCP_TRANSPORT", "http")
    monkeypatch.setenv("DEPUTY_MCP_HOST", "::1")
    monkeypatch.setenv("DEPUTY_MCP_PORT", "47999")
    assert cli.main(argv) == 0
    server.run.assert_called_once_with(
        transport="http",
        host="::1",
        port=47999,
        show_banner=False,
    )


@pytest.mark.parametrize("position", ["before", "after", "implicit"])
def test_cli_wins(position: str, monkeypatch: pytest.MonkeyPatch, server: Mock) -> None:
    monkeypatch.setenv("DEPUTY_MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("DEPUTY_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("DEPUTY_MCP_PORT", "9000")
    argv = ["--transport", "http", "--host", "localhost", "--port", "47999"]
    if position == "before":
        argv.append("serve")
    elif position == "after":
        argv.insert(0, "serve")
    assert cli.main(argv) == 0
    server.run.assert_called_once_with(
        transport="http",
        host="localhost",
        port=47999,
        show_banner=False,
    )


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.2", "example.com"])
def test_remote_refused(
    host: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import deputy_mcp.server as server_pkg

    factory = Mock()
    monkeypatch.setattr(server_pkg, "create_server", factory)
    monkeypatch.setenv("DEPUTY_MCP_ALLOW_REMOTE", "false")
    assert cli.main(["--transport", "http", "--host", host]) == 1
    factory.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--allow-remote" in captured.err


@pytest.mark.parametrize("via_env", [False, True])
def test_remote_override_warns(
    via_env: bool,
    monkeypatch: pytest.MonkeyPatch,
    server: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["serve", "--transport", "http", "--host", "0.0.0.0"]
    if via_env:
        monkeypatch.setenv("DEPUTY_MCP_ALLOW_REMOTE", "true")
    else:
        argv.append("--allow-remote")
    assert cli.main(argv) == 0
    server.run.assert_called_once_with(
        transport="http",
        host="0.0.0.0",
        port=8000,
        show_banner=False,
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "anyone who can reach the port" in captured.err
    assert "configured Deputy credentials" in captured.err


@pytest.mark.parametrize("suffix,value", [("TRANSPORT", "sse"), ("PORT", "bad"), ("PORT", "0")])
def test_invalid_env_rejected(suffix: str, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(f"DEPUTY_MCP_{suffix}", value)
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
