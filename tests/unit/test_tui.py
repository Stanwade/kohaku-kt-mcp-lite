import json
import os
import sys
from types import SimpleNamespace

import pytest

import kt_mcp.tui as module
from kt_mcp.daemon import DaemonConfig, save_config
from kt_mcp.desktop import DesktopSettings
from kt_mcp.onboarding import SetupCancelled
from kt_mcp.tui import TerminalController

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux terminal manager"
)
MCP_TOKEN = "tui-test-token-" + "a" * 32
TUNNEL_TOKEN = "private-cloudflare-tunnel-token"


@pytest.fixture(autouse=True)
def isolated_tokens(monkeypatch):
    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("TUNNEL_TOKEN", raising=False)


class FakeManager:
    system = False

    def __init__(self):
        self.calls = []
        self.log_text = ""

    def install(self, path, **options):
        self.calls.append(("install", path, options))

    def control(self, action):
        self.calls.append(("control", action))

    def enable_linger(self):
        self.calls.append(("linger",))

    def logs(self, lines):
        self.calls.append(("logs", lines))
        return self.log_text


@pytest.fixture
def terminal(tmp_path):
    config = DaemonConfig(
        DesktopSettings(
            str(tmp_path), mode="named", public_origin="https://mcp.example.com"
        ),
        MCP_TOKEN,
        TUNNEL_TOKEN,
    )
    path = tmp_path / "config.json"
    save_config(config, path)
    manager = FakeManager()
    return SimpleNamespace(
        path=path,
        config=config,
        manager=manager,
        controller=TerminalController(manager, path),
    )


def test_actions_are_explicit_systemd_operations(terminal):
    for key in "isxredg":
        assert terminal.controller.act(key) is None
    assert terminal.manager.calls == [
        ("install", terminal.path, {"now": True, "linger": True}),
        ("control", "start"),
        ("control", "stop"),
        ("control", "restart"),
        ("control", "enable"),
        ("control", "disable"),
        ("linger",),
    ]


def test_quit_and_unknown_keys_never_stop_or_modify_service(terminal):
    for key in ("q", "", "?", "\x1b"):
        assert terminal.controller.act(key) is None
    assert terminal.manager.calls == []


def test_system_panel_does_not_guess_installation_user(terminal):
    terminal.manager.system = True
    with pytest.raises(ValueError, match="--run-as"):
        terminal.controller.act("i")
    assert terminal.manager.calls == []


def test_logs_and_default_text_redact_both_credentials(terminal):
    terminal.manager.log_text = (
        f"Connection http://127.0.0.1:8765/mcp/{MCP_TOKEN}\n"
        f"Connector reported {TUNNEL_TOKEN}\n"
        "ordinary diagnostic"
    )
    title, body = terminal.controller.act("l")
    assert title
    assert MCP_TOKEN not in body
    assert TUNNEL_TOKEN not in body
    assert "ordinary diagnostic" in body
    assert terminal.manager.calls == [("logs", 200)]
    redacted = terminal.controller.redact(f"{MCP_TOKEN} {TUNNEL_TOKEN}")
    assert MCP_TOKEN not in redacted and TUNNEL_TOKEN not in redacted


def test_client_configuration_explicitly_contains_only_mcp_credential(terminal):
    title, body = terminal.controller.act("c")
    assert "credential" in title.lower()
    assert json.loads(body) == {
        "mcpServers": {"kt-mcp": {"url": "https://mcp.example.com/mcp/" + MCP_TOKEN}}
    }
    assert TUNNEL_TOKEN not in body
    assert terminal.manager.calls == []


def test_non_tty_fails_before_curses_or_setup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setitem(sys.modules, "curses", None)
    path = tmp_path / "config.json"
    with pytest.raises(SystemExit) as error:
        module.main(["--config", str(path)])
    assert error.value.code == 2
    assert "interactive terminal" in capsys.readouterr().err
    assert not path.exists()


def test_system_mode_does_not_create_missing_config_as_root(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    path = tmp_path / "missing/config.json"

    def no_wizard(path):
        pytest.fail("system mode must require prior run-user configuration")

    monkeypatch.setattr(module, "setup_config", no_wizard)
    with pytest.raises(SystemExit) as error:
        module.main(["--system", "--config", str(path)])
    assert error.value.code == 2
    assert "run user" in capsys.readouterr().err
    assert not path.exists()


def test_root_cannot_start_default_user_panel(terminal, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(SystemExit) as error:
        module.main(["--config", str(terminal.path)])
    assert error.value.code == 2
    assert "without sudo" in capsys.readouterr().err
    assert terminal.manager.calls == []


@pytest.mark.parametrize(
    "cancellation",
    [
        EOFError(),
        KeyboardInterrupt(),
        SetupCancelled("Setup cancelled; no configuration was saved."),
    ],
    ids=["eof", "ctrl-c", "explicit-cancel"],
)
def test_cancelled_first_run_never_opens_panel_or_leaves_partial_config(
    tmp_path, monkeypatch, capsys, cancellation
):
    import curses

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    path = tmp_path / "config.json"

    def cancelled_setup(path):
        raise cancellation

    def must_not_open_panel(*args):
        pytest.fail("a cancelled setup must not open the service panel")

    monkeypatch.setattr(module, "setup_config", cancelled_setup)
    monkeypatch.setattr(curses, "wrapper", must_not_open_panel)
    assert module.main(["--config", str(path)]) == 0
    assert not path.exists()
    output = capsys.readouterr()
    assert output.err == ""
    if isinstance(cancellation, SetupCancelled):
        assert str(cancellation) in output.out


def test_small_terminal_ignores_hidden_stop_key(terminal, monkeypatch):
    import curses

    monkeypatch.setattr(curses, "curs_set", lambda visible: None)
    monkeypatch.setattr(terminal.manager, "status", dict, raising=False)
    monkeypatch.setattr(terminal.manager, "linger_status", lambda: "no", raising=False)

    class SmallScreen:
        def __init__(self):
            self.keys = iter((ord("x"), ord("q")))

        def getmaxyx(self):
            return 10, 40

        def keypad(self, enabled):
            pass

        def timeout(self, milliseconds):
            pass

        def erase(self):
            pass

        def addnstr(self, *args):
            pass

        def refresh(self):
            pass

        def getch(self):
            return next(self.keys)

    module._screen(SmallScreen(), terminal.controller)
    assert terminal.manager.calls == [("logs", 8)]
