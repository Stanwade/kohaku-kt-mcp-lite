"""The explicit link command exports a copyable URL without panel decoration."""

import json
import os
import sys

import pytest

from kt_mcp.cli import main
from kt_mcp.daemon import DaemonConfig, save_config
from kt_mcp.desktop import DesktopSettings

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux link command")
TOKEN = "link-test-token-" + "a" * 32


@pytest.fixture(autouse=True)
def isolated_tokens(monkeypatch):
    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("TUNNEL_TOKEN", raising=False)


@pytest.mark.parametrize("mode", ["local", "named", "quick"])
def test_link_prints_exactly_one_unstyled_url_even_in_a_terminal(
    tmp_path, monkeypatch, capsys, mode
):
    path = tmp_path / "config with spaces" / "config.json"
    config = DaemonConfig(
        DesktopSettings(
            str(tmp_path),
            mode=mode,
            public_origin="https://mcp.example.com" if mode == "named" else "",
        ),
        TOKEN,
        "hidden-connector-token" if mode == "named" else "",
    )
    save_config(config, path)
    origin = {
        "local": "http://127.0.0.1:8765",
        "named": "https://mcp.example.com",
        "quick": "https://link-test.trycloudflare.com",
    }[mode]
    expected = origin + "/mcp/" + TOKEN
    if mode == "quick":
        path.with_suffix(".status.json").write_text(
            json.dumps({"pid": os.getpid(), "url": expected}), encoding="utf-8"
        )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert main(["link", "--config", str(path)]) == 0

    captured = capsys.readouterr()
    assert captured.out == expected + "\n"
    assert captured.err == ""


def test_link_fails_cleanly_when_quick_tunnel_has_no_current_address(tmp_path, capsys):
    path = tmp_path / "config.json"
    save_config(DaemonConfig(DesktopSettings(str(tmp_path), mode="quick"), TOKEN), path)

    with pytest.raises(SystemExit) as error:
        main(["link", "--config", str(path)])

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "quick tunnel is not ready" in captured.err
    assert TOKEN not in captured.err
