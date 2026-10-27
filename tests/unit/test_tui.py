import json
import os
import sys
from itertools import count
from pathlib import Path
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
    name = "kt-mcp-test"
    unit_name = "kt-mcp-test.service"
    unit_path = Path("/fake/systemd/user/kt-mcp-test.service")

    def __init__(self):
        self.calls = []
        self.log_text = ""
        self.status_data = {
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "SubState": "dead",
            "UnitFileState": "",
            "MainPID": "0",
            "NRestarts": "0",
        }
        self.linger = "no"

    def status(self):
        return dict(self.status_data)

    def linger_status(self):
        return self.linger

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
    for key in ("q", "", "z", "\x1b"):
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
    assert not any(
        call[0] in {"install", "control", "linger"} for call in terminal.manager.calls
    )


def active_status(**overrides):
    return {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "enabled",
        "MainPID": "987654",
        "NRestarts": "0",
        **overrides,
    }


@pytest.mark.parametrize(
    "status,expected_keys",
    [
        (active_status(LoadState="not-found", ActiveState="inactive"), ("[i]",)),
        (active_status(ActiveState="inactive", SubState="dead"), ("[s]",)),
        (active_status(ActiveState="failed", SubState="failed"), ("[l]", "[r]")),
        (active_status(ActiveState="activating", SubState="start"), ("[l]",)),
        (active_status(), ("[c]",)),
    ],
    ids=["not-installed", "stopped", "failed", "starting", "active"],
)
def test_next_steps_match_current_service_state(status, expected_keys):
    next_action, _ = module.next_steps(status, linger="yes")

    assert all(key in next_action.lower() for key in expected_keys)
    if status["ActiveState"] == "active" and status["LoadState"] == "loaded":
        assert "logs" in next_action.lower()
        assert "mcp ready" not in next_action.lower()
    else:
        assert "[c]" not in next_action.lower()


def test_missing_system_service_guidance_requires_an_explicit_run_user():
    next_action, _ = module.next_steps(
        active_status(LoadState="not-found", ActiveState="inactive"),
        system=True,
    )

    assert "--run-as" in next_action
    assert "[i]" not in next_action.lower()


@pytest.mark.parametrize(
    "boot,linger,system,expected,absent",
    [
        ("disabled", "no", False, ("[e]", "[g]"), ()),
        ("enabled-runtime", "yes", False, ("[e]",), ("[g]",)),
        ("enabled", "no", False, ("[g]",), ("[e]",)),
        ("enabled", "yes", False, (), ("[e]", "[g]")),
        ("enabled", "no", True, (), ("[e]", "[g]")),
    ],
    ids=["boot-and-linger", "temporary-boot", "linger-only", "persistent", "system"],
)
def test_persistence_guidance_distinguishes_boot_and_user_linger(
    boot, linger, system, expected, absent
):
    _, persistence = module.next_steps(
        active_status(UnitFileState=boot), linger=linger, system=system
    )

    assert all(key in persistence.lower() for key in expected)
    assert all(key not in persistence.lower() for key in absent)


def test_poll_error_takes_priority_over_a_previously_active_snapshot():
    next_action, _ = module.next_steps(
        active_status(), linger="yes", error="manager unavailable"
    )

    assert "[c]" not in next_action.lower()
    assert any(
        word in next_action.lower()
        for word in ("unknown", "unavailable", "cannot", "unable")
    )


def test_help_and_details_are_read_only_and_hide_credentials(terminal):
    terminal.controller.status = active_status()
    terminal.controller.linger = "yes"

    help_title, help_body = terminal.controller.act("?")
    details_title, details_body = terminal.controller.act("v")

    assert help_title and help_body
    assert details_title and details_body
    assert str(terminal.path) in details_body
    assert terminal.config.settings.workspace in details_body
    assert str(terminal.manager.unit_path) in details_body
    for text in (help_title, help_body, details_title, details_body):
        assert MCP_TOKEN not in text
        assert TUNNEL_TOKEN not in text
    assert not any(
        call[0] in {"install", "control", "linger"} for call in terminal.manager.calls
    )


def cjk_cells(text):
    # The fixtures contain only ASCII, CJK ideographs and a one-cell ellipsis.
    return len(text) + sum("\u4e00" <= character <= "\u9fff" for character in text)


class RecordingScreen:
    def __init__(self, keys, *, height=24, width=64):
        self.keys = iter(keys)
        self.height = height
        self.width = width
        self.drawn = []
        self.frames = []

    def getmaxyx(self):
        return self.height, self.width

    def keypad(self, enabled):
        pass

    def timeout(self, milliseconds):
        pass

    def erase(self):
        self.drawn = []

    def addnstr(self, row, column, text, limit, attribute=0):
        self.addstr(row, column, text[:limit], attribute)

    def addstr(self, row, column, text, attribute=0):
        assert 0 <= row < self.height
        assert column + cjk_cells(text) < self.width, "text overflowed the terminal"
        self.drawn.append((row, column, text))

    def refresh(self):
        self.frames.append(list(self.drawn))

    def getch(self):
        return next(self.keys)


def test_unicode_clipping_uses_terminal_cells_and_keeps_the_path_tail():
    assert module.display_width("a中文e\u0301") == 6
    assert module.clip_display("ab中文cd", 5) == "ab中"
    path = "/home/user/" + "工作目录/" * 12 + "末尾项目"
    shortened = module.ellipsize(path, 48)

    assert cjk_cells(shortened) <= 48
    assert shortened.startswith("/home/")
    assert shortened.endswith("末尾项目")
    assert "…" in shortened
    assert len(shortened) < len(path)


def test_details_popup_wraps_chinese_and_spaces_without_losing_path_text():
    path = "/home/" + "工作目录/with  spaces/" * 6 + "末尾项目"
    screen = RecordingScreen([ord("q")], height=24, width=64)

    module._popup(screen, "Full workspace path", path)

    body_lines = [
        text for row, _, text in screen.frames[-1] if 0 < row < screen.height - 1
    ]
    assert len(body_lines) > 1
    assert "".join(body_lines) == path


def test_64_column_panel_preserves_the_workspace_name_at_the_end(
    terminal, tmp_path, monkeypatch
):
    import curses

    workspace = tmp_path / ("工作目录" * 12) / "末尾项目"
    workspace.mkdir(parents=True)
    save_config(
        DaemonConfig(DesktopSettings(str(workspace), mode="local"), MCP_TOKEN),
        terminal.path,
        replace=True,
    )
    terminal.manager.status_data = active_status()
    terminal.manager.linger = "yes"
    monkeypatch.setattr(curses, "curs_set", lambda visible: None)
    screen = RecordingScreen([ord("q")], height=18, width=64)

    module._screen(screen, terminal.controller)

    texts = [text for _, _, text in screen.frames[-1]]
    assert any(text.rstrip().endswith("末尾项目") and "…" in text for text in texts)
    assert any("987654" in text for text in texts)
    assert MCP_TOKEN not in "\n".join(texts)


def test_failed_refresh_clears_old_running_state_and_redacts_the_error(
    terminal, monkeypatch
):
    import curses

    first = True

    def status():
        nonlocal first
        if first:
            first = False
            return active_status()
        raise module.ServiceError("manager unavailable: " + MCP_TOKEN)

    monkeypatch.setattr(terminal.manager, "status", status)
    terminal.manager.linger = "yes"
    monkeypatch.setattr(curses, "curs_set", lambda visible: None)
    ticks = count(0, 10)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    screen = RecordingScreen([-1, ord("q")], height=24, width=64)

    module._screen(screen, terminal.controller)

    first_frame = "\n".join(text for _, _, text in screen.frames[0])
    last_frame = "\n".join(text for _, _, text in screen.frames[-1])
    assert "987654" in first_frame
    assert "987654" not in last_frame
    assert terminal.controller.status == {}
    assert terminal.controller.linger == "unknown"
    assert "manager unavailable" in terminal.controller.refresh_error
    assert MCP_TOKEN not in terminal.controller.refresh_error
    assert MCP_TOKEN not in last_frame
