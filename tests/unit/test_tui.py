import json
import os
import shlex
import sys
from contextlib import nullcontext
from dataclasses import replace
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
def isolated_tokens(monkeypatch, tmp_path):
    import curses

    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("TUNNEL_TOKEN", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "isolated-config"))
    # Catalog/socket guards have dedicated coverage in test_instance_tui.py.
    monkeypatch.setattr(module, "ensure_available", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "catalog_lock", nullcontext)

    def control(manager, path, action):
        manager.control(action)
        return manager.status()

    monkeypatch.setattr(module, "control_service", control)
    monkeypatch.setattr(module, "service_setting", control)
    monkeypatch.setattr(curses, "has_colors", lambda: False)


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
    for key in ("q", "Q", "", "z", "\x1b"):
        assert terminal.controller.act(key) is None
    assert terminal.manager.calls == []


def test_system_panel_does_not_guess_installation_user(terminal):
    terminal.manager.system = True
    with pytest.raises(ValueError, match="Actions > Install"):
        terminal.controller.act("i")
    assert terminal.manager.calls == []


def test_logs_and_diagnostic_text_redact_both_credentials(terminal):
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


def test_missing_system_config_uses_native_account_setup_and_can_cancel(
    tmp_path, monkeypatch
):
    import curses

    from kt_mcp import tui_actions

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    path = tmp_path / "missing/config.json"
    calls = []

    def prepare(screen, selected_path):
        calls.append(selected_path)
        return False

    monkeypatch.setattr(tui_actions, "prepare_system_config", prepare)
    monkeypatch.setattr(
        curses, "wrapper", lambda function, *args: function(object(), *args)
    )
    assert module.main(["--system", "--config", str(path)]) == 0
    assert calls == [path]
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

    from kt_mcp import instance_tui, tui_forms

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    path = tmp_path / "config.json"

    def cancelled_setup(path):
        raise cancellation

    def must_not_open_panel(*args):
        pytest.fail("a cancelled setup must not open the service panel")

    monkeypatch.setattr(module, "setup_config", cancelled_setup)
    monkeypatch.setattr(instance_tui, "single_screen", must_not_open_panel)
    monkeypatch.setattr(
        tui_forms, "run_wizard", lambda screen, title, callback: callback()
    )
    monkeypatch.setattr(
        curses, "wrapper", lambda function, *args: function(object(), *args)
    )
    assert module.main(["--config", str(path)]) == 0
    assert not path.exists()
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == ""


def test_small_terminal_ignores_hidden_stop_key(terminal, monkeypatch):
    import curses

    monkeypatch.setattr(curses, "curs_set", lambda visible: None)
    monkeypatch.setattr(terminal.manager, "status", dict, raising=False)
    monkeypatch.setattr(terminal.manager, "linger_status", lambda: "no", raising=False)

    class SmallScreen:
        def __init__(self):
            self.keys = iter((ord("x"), ord("o"), ord("q")))

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
        (active_status(LoadState="not-found", ActiveState="inactive"), ("[s]",)),
        (active_status(ActiveState="inactive", SubState="dead"), ("[s]",)),
        (active_status(ActiveState="failed", SubState="failed"), ("[l]", "[r]")),
        (active_status(ActiveState="activating", SubState="start"), ("[l]",)),
        (active_status(), ("[x]", "[r]")),
    ],
    ids=["not-installed", "stopped", "failed", "starting", "active"],
)
def test_next_steps_match_current_service_state(status, expected_keys):
    next_action, _ = module.next_steps(status, linger="yes")

    assert all(key in next_action.lower() for key in expected_keys)
    assert "mcp ready" not in next_action.lower()
    assert "[c]" not in next_action.lower()


def test_missing_system_service_guidance_requires_an_explicit_run_user():
    next_action, _ = module.next_steps(
        active_status(LoadState="not-found", ActiveState="inactive"),
        system=True,
    )

    assert "[i]" in next_action.lower()
    assert "[a]" in next_action.lower()


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
        self.styled = []
        self.style_frames = []

    def getmaxyx(self):
        return self.height, self.width

    def keypad(self, enabled):
        pass

    def timeout(self, milliseconds):
        pass

    def clearok(self, enabled):
        self.clear_requested = enabled

    def erase(self):
        self.drawn = []
        self.styled = []

    def addnstr(self, row, column, text, limit, attribute=0):
        self.addstr(row, column, text[:limit], attribute)

    def addstr(self, row, column, text, attribute=0):
        assert 0 <= row < self.height
        assert column + cjk_cells(text) < self.width, "text overflowed the terminal"
        self.drawn.append((row, column, text))
        self.styled.append((row, column, text, attribute))

    def refresh(self):
        self.frames.append(list(self.drawn))
        self.style_frames.append(list(self.styled))

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


def test_64_column_panel_shows_the_complete_link_and_workspace_name(
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
    url = "http://127.0.0.1:8765/mcp/" + MCP_TOKEN
    assert url in "".join(texts)
    assert TUNNEL_TOKEN not in "\n".join(texts)
    assert not any(call[0] == "logs" for call in terminal.manager.calls)
    assert "Recent service log" not in "\n".join(texts)
    assert "[u] Link" in "\n".join(texts)
    assert "[o] Configure" in "\n".join(texts)
    assert any(
        attribute & curses.A_BOLD
        for _, _, text, attribute in screen.style_frames[-1]
        if text.startswith("http://")
    )


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
    assert terminal.controller.connection_state == "Status unknown"
    assert not terminal.controller.connection_verified
    assert terminal.controller.connection_url == (
        "https://mcp.example.com/mcp/" + MCP_TOKEN
    )
    assert TUNNEL_TOKEN not in last_frame


def set_mode(terminal, mode, *, token=MCP_TOKEN, origin="https://mcp.example.com"):
    terminal.config = replace(
        terminal.config,
        settings=replace(
            terminal.config.settings,
            mode=mode,
            public_origin=origin if mode == "named" else "",
        ),
        token=token,
        tunnel_token=TUNNEL_TOKEN if mode == "named" else "",
    )
    save_config(terminal.config, terminal.path, replace=True)
    if mode == "local":
        return "http://127.0.0.1:8765/mcp/" + token
    if mode == "named":
        return origin + "/mcp/" + token
    return "https://current-tunnel.trycloudflare.com/mcp/" + token


def record_startup(terminal, url):
    pid = os.getpid()
    terminal.manager.status_data = active_status(MainPID=str(pid))
    terminal.path.with_suffix(".status.json").write_text(
        json.dumps({"pid": pid, "url": url}), encoding="utf-8"
    )
    return pid


@pytest.mark.parametrize("mode", ["local", "named"])
@pytest.mark.parametrize("state", ["stopped", "unknown"])
def test_configured_link_is_visible_without_claiming_a_running_service(
    terminal, monkeypatch, mode, state
):
    url = set_mode(terminal, mode)
    if state == "unknown":

        def unavailable():
            raise module.ServiceError("user systemd is unavailable")

        monkeypatch.setattr(terminal.manager, "status", unavailable)
    terminal.controller.refresh()

    assert terminal.controller.connection_url == url
    assert not terminal.controller.connection_verified
    assert terminal.controller.connection_tone != "ok"
    if state == "unknown":
        assert terminal.controller.connection_state == "Status unknown"
    else:
        assert "not running" in terminal.controller.connection_state.lower()
    assert terminal.manager.calls == []


@pytest.mark.parametrize("mode", ["local", "named", "quick"])
def test_only_a_matching_active_startup_record_verifies_the_link(terminal, mode):
    url = set_mode(terminal, mode)
    record_startup(terminal, url)

    terminal.controller.refresh()

    assert terminal.controller.connection_url == url
    assert terminal.controller.connection_verified
    assert terminal.controller.connection_tone == "ok"
    assert terminal.controller.connection_state == (
        "Startup verified" if mode == "local" else "Tunnel started"
    )
    assert terminal.manager.calls == []


@pytest.mark.parametrize("mode", ["local", "named"])
def test_active_process_without_startup_record_is_not_verified(terminal, mode):
    url = set_mode(terminal, mode)
    terminal.manager.status_data = active_status(MainPID=str(os.getpid()))

    terminal.controller.refresh()

    assert terminal.controller.connection_url == url
    assert not terminal.controller.connection_verified
    assert terminal.controller.connection_tone != "ok"
    assert "verified" not in terminal.controller.connection_state.lower().replace(
        "unverified", ""
    )


@pytest.mark.parametrize("invalid", ["missing", "different-pid", "stopped", "failed"])
def test_quick_link_is_never_reused_for_an_unconfirmed_service(terminal, invalid):
    url = set_mode(terminal, "quick")
    pid = record_startup(terminal, url)
    terminal.controller.refresh()
    assert terminal.controller.connection_url == url

    if invalid == "missing":
        terminal.path.with_suffix(".status.json").unlink()
    elif invalid == "different-pid":
        terminal.manager.status_data["MainPID"] = str(pid + 1)
    else:
        terminal.manager.status_data.update(
            ActiveState="inactive" if invalid == "stopped" else "failed",
            MainPID="0",
        )
    terminal.controller.refresh()

    assert terminal.controller.connection_url == ""
    assert not terminal.controller.connection_verified
    assert terminal.controller.connection_tone != "ok"
    title, body = terminal.controller.act("u")
    assert title == "MCP link"
    assert url not in body and MCP_TOKEN not in body


def test_rotated_quick_token_clears_the_old_link(terminal):
    old_url = set_mode(terminal, "quick")
    record_startup(terminal, old_url)
    terminal.controller.refresh()
    assert terminal.controller.connection_verified
    set_mode(terminal, "quick", token="replacement-token-" + "b" * 32)

    terminal.controller.refresh()

    assert terminal.controller.connection_url == ""
    assert not terminal.controller.connection_verified
    assert MCP_TOKEN not in terminal.controller.redact("old token: " + MCP_TOKEN)


def test_status_failure_clears_a_previously_verified_quick_link(terminal, monkeypatch):
    url = set_mode(terminal, "quick")
    record_startup(terminal, url)
    terminal.controller.refresh()

    def unavailable():
        raise module.ServiceError("manager failed: " + MCP_TOKEN)

    monkeypatch.setattr(terminal.manager, "status", unavailable)
    terminal.controller.refresh()

    assert terminal.controller.connection_url == ""
    assert terminal.controller.connection_state == "Status unknown"
    assert not terminal.controller.connection_verified
    assert terminal.controller.status == {}
    assert MCP_TOKEN not in terminal.controller.refresh_error


@pytest.mark.parametrize("mode", ["local", "quick"])
def test_configuration_failure_does_not_keep_the_previous_link(terminal, mode):
    url = set_mode(terminal, mode)
    record_startup(terminal, url)
    terminal.controller.refresh()
    terminal.path.write_text("not valid JSON", encoding="utf-8")

    terminal.controller.refresh()

    assert terminal.controller.connection_url == ""
    assert not terminal.controller.connection_verified
    assert terminal.controller.status == {}


def test_linger_query_failure_does_not_erase_verified_service_state(
    terminal, monkeypatch
):
    url = set_mode(terminal, "local")
    record_startup(terminal, url)

    def unavailable():
        raise module.ServiceError("loginctl unavailable")

    monkeypatch.setattr(terminal.manager, "linger_status", unavailable)
    terminal.controller.refresh()

    assert terminal.controller.connection_url == url
    assert terminal.controller.connection_verified
    assert terminal.controller.status["ActiveState"] == "active"
    assert terminal.controller.linger == "unknown"


def test_refresh_does_not_query_logs_and_log_failure_preserves_service_state(
    terminal, monkeypatch
):
    import curses

    url = set_mode(terminal, "local")
    record_startup(terminal, url)
    queried = []

    def unavailable(lines):
        queried.append(lines)
        raise module.ServiceError(f"journal unavailable: {MCP_TOKEN} {TUNNEL_TOKEN}")

    monkeypatch.setattr(terminal.manager, "logs", unavailable)
    monkeypatch.setattr(curses, "curs_set", lambda visible: None)
    terminal.controller.refresh()
    assert queried == []

    screen = RecordingScreen([ord("l"), ord("q"), ord("q")])
    module._screen(screen, terminal.controller)

    assert queried == [200]
    assert terminal.controller.connection_url == url
    assert terminal.controller.connection_verified
    assert terminal.controller.status["ActiveState"] == "active"
    log_frames = [
        frame
        for frame in screen.frames
        if any("Service logs" in text for _, _, text in frame)
    ]
    logs = "\n".join(text for frame in log_frames for _, _, text in frame)
    assert "journal unavailable" in logs
    assert MCP_TOKEN not in logs
    assert TUNNEL_TOKEN not in logs


def test_link_popup_is_read_only_and_exports_the_exact_quoted_configuration(
    terminal, tmp_path
):
    path = tmp_path / "中文 config 'quoted' % ${HOME}.json"
    save_config(terminal.config, path)
    controller = TerminalController(terminal.manager, path)

    title, body = controller.act("U")

    url = "https://mcp.example.com/mcp/" + MCP_TOKEN
    assert title == "MCP link"
    assert body.split("\n\n", 1)[0] == url
    assert "Copy as one line:" in body
    command = body.split("Copy as one line:", 1)[1].strip().splitlines()[0]
    assert shlex.split(command) == [
        os.path.abspath(sys.executable),
        "-m",
        "kt_mcp",
        "link",
        "--config",
        str(path),
    ]
    assert TUNNEL_TOKEN not in body
    assert terminal.manager.calls == []


@pytest.mark.parametrize("key", ["u", "l", "c", "v", "?"])
def test_read_only_popups_restore_the_previous_note(terminal, monkeypatch, key):
    import curses

    note = "Earlier operation completed."
    terminal.controller.note = note
    monkeypatch.setattr(curses, "curs_set", lambda visible: None)
    from kt_mcp import tui_actions

    monkeypatch.setattr(module, "_popup", lambda *args: None)
    monkeypatch.setattr(tui_actions, "show_logs", lambda *args: None)
    monkeypatch.setattr(tui_actions, "show_export", lambda *args, **kwargs: None)

    module._screen(RecordingScreen([ord(key), ord("q")]), terminal.controller)

    assert terminal.controller.note == note
    assert not any(
        call[0] in {"install", "control", "linger"} for call in terminal.manager.calls
    )


def test_long_link_can_be_scrolled_without_losing_any_url_characters(
    terminal, monkeypatch
):
    import curses

    # The configuration accepts an arbitrary HTTPS origin; no visual truncation
    # may silently turn such a saved address into a different copyable value.
    origin = (
        "https://" + "".join(f"segment{number:02}." for number in range(80)) + "test"
    )
    url = set_mode(terminal, "named", token="z" * 128, origin=origin)
    monkeypatch.setattr(curses, "curs_set", lambda visible: None)
    monkeypatch.setattr(module.time, "monotonic", lambda: 0)
    screen = RecordingScreen([curses.KEY_NPAGE] * 10 + [ord("q")], height=18, width=64)

    module._screen(screen, terminal.controller)

    remaining = url
    for frame in screen.frames:
        for _, _, text in frame:
            if text and remaining.startswith(text):
                remaining = remaining[len(text) :]
    assert remaining == "", "Scrolling did not expose the complete URL"
    assert any(
        all(fragment in text for fragment in ("Up/Down/PgUp/PgDn", "scroll", "[u]"))
        for _, _, text in screen.frames[0]
    )
    assert not any(
        call[0] in {"install", "control", "linger"} for call in terminal.manager.calls
    )


@pytest.mark.parametrize("no_color", [None, "", "1"])
def test_monochrome_style_keeps_bold_without_color_pairs(monkeypatch, no_color):
    import curses

    if no_color is not None:
        monkeypatch.setenv("NO_COLOR", no_color)
        monkeypatch.setattr(curses, "has_colors", lambda: True)
        monkeypatch.setattr(curses, "start_color", lambda: None)
        monkeypatch.setattr(curses, "use_default_colors", lambda: None)

    def unexpected_color(*args):
        pytest.fail("monochrome mode must not initialize or select color pairs")

    monkeypatch.setattr(curses, "init_pair", unexpected_color)
    monkeypatch.setattr(curses, "color_pair", unexpected_color)

    styles = module._styles()

    assert styles["title"] & curses.A_BOLD
    assert styles["link"] & curses.A_BOLD
    assert all(attribute & curses.A_COLOR == 0 for attribute in styles.values())


@pytest.fixture
def editable_service(terminal, monkeypatch, tmp_path):
    unit = tmp_path / "kt-mcp-test.service"
    unit.write_bytes(b"original generated unit\n")
    unit.chmod(0o644)
    terminal.manager.unit_path = unit
    terminal.manager.status_data = active_status()
    events = []

    def binding():
        info = unit.stat()
        owner = terminal.path.stat()
        return SimpleNamespace(
            name=terminal.manager.name,
            system=terminal.manager.system,
            unit_path=unit,
            python=Path(sys.executable),
            config_path=terminal.path,
            run_as="service-user" if terminal.manager.system else None,
            owner_uid=owner.st_uid,
            owner_gid=owner.st_gid,
            unit_bytes=unit.read_bytes(),
            status=dict(terminal.manager.status_data),
            unit_mode=info.st_mode & 0o7777,
            unit_owner_uid=info.st_uid,
            unit_owner_gid=info.st_gid,
        )

    def inspect(name, system, **options):
        assert name == terminal.manager.name
        assert system == terminal.manager.system
        assert options == {
            "expected_python": sys.executable,
            "expected_config": terminal.path,
        }
        events.append(("inspect",))
        return binding()

    def stop(current):
        events.append(("stop", current.unit_bytes))
        terminal.manager.status_data.update(
            ActiveState="inactive", SubState="dead", MainPID="0"
        )

    def render(path, *, system=False, run_as=None):
        saved = json.loads(path.read_text(encoding="utf-8"))
        return "new generated unit\nWorkingDirectory=" + saved["workspace"] + "\n"

    def install(path, **options):
        events.append(("install",))
        terminal.manager.calls.append(("install", path, options))
        unit.write_text(
            render(path, system=terminal.manager.system, run_as=options["run_as"]),
            encoding="utf-8",
        )

    def restore(current, *, expected_unit_bytes=None):
        events.append(("restore",))
        assert unit.read_bytes() in {current.unit_bytes, expected_unit_bytes}
        unit.write_bytes(current.unit_bytes)

    def start(current):
        events.append(("start", current.unit_bytes))
        terminal.manager.status_data.update(
            ActiveState="active", SubState="running", MainPID="12345"
        )
        return dict(terminal.manager.status_data)

    monkeypatch.setattr(module, "inspect_service", inspect)
    monkeypatch.setattr(module, "assert_external_process", lambda current: None)
    monkeypatch.setattr(module, "stop_service", stop)
    monkeypatch.setattr(module, "render_unit", render)
    monkeypatch.setattr(module, "restore_unit", restore)

    def control(manager, path, action):
        current = inspect(
            manager.name,
            manager.system,
            expected_python=sys.executable,
            expected_config=path,
        )
        if action in {"stop", "restart"}:
            stop(current)
        if action in {"start", "restart"}:
            return start(current)
        return manager.status()

    monkeypatch.setattr(module, "control_service", control)
    monkeypatch.setattr(terminal.manager, "install", install)
    terminal.events = events
    terminal.original_unit = unit.read_bytes()
    terminal.install = install
    return terminal


def answers(values):
    remaining = iter(values)

    def ask(prompt):
        value = next(remaining)
        if isinstance(value, BaseException):
            raise value
        return value

    return ask


def test_configure_stops_after_confirmation_and_requires_an_explicit_start(
    editable_service, tmp_path, capsys
):
    fixture = editable_service
    workspace = tmp_path / "new workspace 中文"
    workspace.mkdir()
    original = fixture.path.read_bytes()
    entered = iter(["y", str(workspace), "8123", "local", ""])
    first = True

    def ask(prompt):
        nonlocal first
        if first:
            assert "Stop MCP" in prompt and "y/N" in prompt
            assert not any(event[0] == "stop" for event in fixture.events)
            first = False
        else:
            assert fixture.manager.status_data["ActiveState"] == "inactive"
            assert fixture.path.read_bytes() == original
        return next(entered)

    fixture.controller.configure(ask=ask)

    assert fixture.manager.calls == [
        (
            "install",
            fixture.path,
            {
                "replace": True,
                "run_as": None,
                "now": False,
                "enable": False,
                "linger": False,
            },
        )
    ]
    assert [event[0] for event in fixture.events if event[0] != "inspect"] == [
        "stop",
        "install",
    ]
    assert fixture.manager.status_data["MainPID"] == "0"
    saved = json.loads(fixture.path.read_text(encoding="utf-8"))
    assert saved["workspace"] == str(workspace)
    assert saved["mode"] == "local" and saved["port"] == 8123
    assert saved["token"] == MCP_TOKEN
    assert str(workspace).encode() in fixture.manager.unit_path.read_bytes()
    assert "[s] Start" in fixture.controller.note
    assert "MCP will stop" in capsys.readouterr().out

    updated_unit = fixture.manager.unit_path.read_bytes()
    fixture.controller.act("s")
    assert fixture.events[-2:] == [("inspect",), ("start", updated_unit)]
    assert fixture.manager.status_data["ActiveState"] == "active"


@pytest.mark.parametrize("answer", ["", ":q", EOFError(), KeyboardInterrupt()])
def test_declining_configuration_does_not_stop_or_write(editable_service, answer):
    fixture = editable_service
    original = fixture.path.read_bytes()

    fixture.controller.configure(ask=answers([answer]))

    assert fixture.path.read_bytes() == original
    assert fixture.manager.unit_path.read_bytes() == fixture.original_unit
    assert fixture.manager.status_data["ActiveState"] == "active"
    assert not fixture.manager.calls
    assert all(event[0] == "inspect" for event in fixture.events)


@pytest.mark.parametrize("stage", ["inspect_service", "assert_external_process"])
def test_unsafe_binding_or_same_cgroup_is_rejected_before_confirmation(
    editable_service, monkeypatch, stage
):
    fixture = editable_service
    original = fixture.path.read_bytes()

    def refuse(*args, **kwargs):
        raise module.ManagedServiceError("fixture binding cannot be trusted")

    monkeypatch.setattr(module, stage, refuse)

    def no_prompt(prompt):
        pytest.fail("an unverified service must not offer a stop/edit confirmation")

    with pytest.raises(module.ManagedServiceError, match="cannot be trusted"):
        fixture.controller.configure(ask=no_prompt)

    assert fixture.path.read_bytes() == original
    assert not fixture.manager.calls
    assert not any(event[0] == "stop" for event in fixture.events)


def test_stop_failure_does_not_open_the_editor_or_change_configuration(
    editable_service, monkeypatch
):
    fixture = editable_service
    original = fixture.path.read_bytes()
    prompts = []

    def stop(binding):
        raise module.ManagedServiceError("fixture stop timed out")

    def ask(prompt):
        prompts.append(prompt)
        assert len(prompts) == 1, "the editor opened before stop was confirmed"
        return "y"

    monkeypatch.setattr(module, "stop_service", stop)

    with pytest.raises(module.ManagedServiceError, match="stop timed out"):
        fixture.controller.configure(ask=ask)

    assert fixture.path.read_bytes() == original
    assert fixture.manager.unit_path.read_bytes() == fixture.original_unit
    assert not fixture.manager.calls
    assert "remains stopped" not in fixture.controller.note


def test_cancelling_after_stop_keeps_old_settings_and_does_not_restart(
    editable_service,
):
    fixture = editable_service
    original = fixture.path.read_bytes()

    fixture.controller.configure(ask=answers(["y", ":q"]))

    assert fixture.path.read_bytes() == original
    assert fixture.manager.unit_path.read_bytes() == fixture.original_unit
    assert fixture.manager.status_data["ActiveState"] == "inactive"
    assert not fixture.manager.calls
    assert [event[0] for event in fixture.events if event[0] != "inspect"] == ["stop"]
    assert "remains stopped" in fixture.controller.note
    assert "[s] Start" in fixture.controller.note


def test_failed_unit_reload_restores_both_files_and_redacts_saved_and_new_credentials(
    editable_service, monkeypatch, capsys
):
    fixture = editable_service
    original = fixture.path.read_bytes()
    changed_secret = "new-hidden-tunnel-credential"
    environment_token = "environment-access-token-" + "b" * 32
    monkeypatch.setenv("KT_MCP_TOKEN", environment_token)
    monkeypatch.setenv("TUNNEL_TOKEN", "environment-tunnel-credential")
    fixture.controller = TerminalController(fixture.manager, fixture.path)
    failure = module.ServiceError(
        "fixture reload failed: "
        + MCP_TOKEN
        + " "
        + TUNNEL_TOKEN
        + " "
        + changed_secret
        + " "
        + environment_token
    )

    def install(path, **options):
        fixture.install(path, **options)
        raise failure

    monkeypatch.setattr(fixture.manager, "install", install)

    with pytest.raises(module.ServiceError) as result:
        fixture.controller.configure(
            ask=answers(["y", "", "8123", "", "", ""]),
            ask_secret=answers([changed_secret]),
        )

    assert result.value is failure, "rollback replaced the original failure"
    assert fixture.path.read_bytes() == original
    assert fixture.manager.unit_path.read_bytes() == fixture.original_unit
    assert fixture.manager.status_data["ActiveState"] == "inactive"
    assert not any(event[0] == "start" for event in fixture.events)
    assert "fixture reload failed" in fixture.controller.note
    shown = fixture.controller.note + capsys.readouterr().out
    for secret in (MCP_TOKEN, TUNNEL_TOKEN, changed_secret, environment_token):
        assert secret not in shown, "configuration diagnostics leaked a credential"


def test_rollback_failure_preserves_original_error_and_attempts_config_restoration(
    editable_service, monkeypatch
):
    fixture = editable_service
    original = fixture.path.read_bytes()
    failure = module.ServiceError("original reload failure")

    def install(path, **options):
        fixture.install(path, **options)
        raise failure

    def restore(*args, **kwargs):
        raise module.ManagedServiceError("unit recovery failed")

    monkeypatch.setattr(fixture.manager, "install", install)
    monkeypatch.setattr(module, "restore_unit", restore)

    with pytest.raises(module.ServiceError) as result:
        fixture.controller.configure(ask=answers(["y", "", "", "local", ""]))

    assert result.value is failure
    assert fixture.path.read_bytes() == original
    assert "original reload failure" in fixture.controller.note
    assert "Recovery incomplete" in fixture.controller.note
    assert "unit recovery failed" in fixture.controller.note
    assert not any(event[0] == "start" for event in fixture.events)


def test_unit_changed_during_editing_is_not_overwritten(editable_service):
    fixture = editable_service
    original = fixture.path.read_bytes()
    entered = answers(["y", "", "", "local"])

    def ask(prompt):
        if "Save?" not in prompt:
            return entered(prompt)
        fixture.manager.unit_path.write_bytes(b"external unit change\n")
        return ""

    with pytest.raises(module.ManagedServiceError, match="changed during editing"):
        fixture.controller.configure(ask=ask)

    assert fixture.path.read_bytes() == original
    assert fixture.manager.unit_path.read_bytes() == b"external unit change\n"
    assert not fixture.manager.calls
    assert not any(event[0] == "start" for event in fixture.events)


def test_system_reconfiguration_keeps_the_verified_run_account(
    editable_service, monkeypatch
):
    fixture = editable_service
    fixture.manager.system = True
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    fixture.controller.configure(ask=answers(["y", "", "", "local", ""]))

    assert fixture.manager.calls[0][2] == {
        "replace": True,
        "run_as": "service-user",
        "now": False,
        "enable": False,
        "linger": False,
    }
    assert fixture.manager.status_data["ActiveState"] == "inactive"


def test_system_configuration_with_preserved_user_xdg_never_uses_user_catalog(
    editable_service, monkeypatch, tmp_path
):
    fixture = editable_service
    fixture.manager.system = True
    # This existing private config belongs to the run user, not the simulated root.
    owner = fixture.path.stat().st_uid
    user_xdg = tmp_path / "preserved-user-xdg"
    user_xdg.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user_xdg))
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def no_user_catalog(*args, **kwargs):
        pytest.fail(
            "system operations must not read or write the user instance catalog"
        )

    monkeypatch.setattr(module, "catalog_lock", no_user_catalog)
    monkeypatch.setattr(module, "ensure_available", no_user_catalog)
    fixture.controller = TerminalController(fixture.manager, fixture.path)
    fixture.controller.configure(ask=answers(["y", "", "8123", "local", ""]))

    saved = json.loads(fixture.path.read_text(encoding="utf-8"))
    assert saved["port"] == 8123
    assert saved["token"] == MCP_TOKEN
    assert fixture.path.stat().st_uid == owner
    assert fixture.path.stat().st_mode & 0o777 == 0o600
    assert fixture.manager.calls[0][2]["run_as"] == "service-user"
    assert fixture.manager.status_data["ActiveState"] == "inactive"
    assert list(user_xdg.iterdir()) == []

    fixture.controller.act("s")
    assert fixture.events[-1][0] == "start"
    assert fixture.manager.status_data["ActiveState"] == "active"
    assert list(user_xdg.iterdir()) == []


@pytest.mark.parametrize("appears_during_edit", [False, True])
def test_live_manual_daemon_blocks_unregistered_configuration_without_stopping_it(
    terminal, monkeypatch, appears_during_edit
):
    original = terminal.path.read_bytes()
    startup = terminal.path.with_suffix(".status.json")
    probes = []
    monkeypatch.setattr(module, "inspect_service", lambda *args, **kwargs: None)

    def record():
        startup.write_text(json.dumps({"pid": 12345}), encoding="utf-8")
        startup.chmod(0o600)

    def probe(pid, signal):
        probes.append((pid, signal))
        assert signal == 0, "configuration attempted to signal a manual daemon"

    monkeypatch.setattr(module.os, "kill", probe)
    if not appears_during_edit:
        record()
    entered = answers(["y", "", "", "local"])

    def ask(prompt):
        if not appears_during_edit:
            pytest.fail(
                "a live manual daemon must block configuration before prompting"
            )
        if "Save?" not in prompt:
            return entered(prompt)
        record()
        return ""

    with pytest.raises(
        module.ManagedServiceError, match="manual daemon.*still running"
    ):
        terminal.controller.configure(ask=ask)

    assert probes == [(12345, 0)]
    assert terminal.path.read_bytes() == original
    assert json.loads(startup.read_text()) == {"pid": 12345}
    assert not terminal.manager.calls


def test_confirmed_missing_unit_allows_configuration_without_registering_a_service(
    terminal, monkeypatch
):
    monkeypatch.setattr(module, "inspect_service", lambda *args, **kwargs: None)

    def unexpected(*args, **kwargs):
        pytest.fail("configuration-only editing must not control a service")

    monkeypatch.setattr(module, "stop_service", unexpected)
    monkeypatch.setattr(module, "control_service", unexpected)
    monkeypatch.setattr(module, "render_unit", unexpected)

    terminal.controller.configure(ask=answers(["y", "", "", "local", ""]))

    assert json.loads(terminal.path.read_text())["mode"] == "local"
    assert not terminal.manager.calls
    assert "[i] Install/start" in terminal.controller.note


@pytest.mark.parametrize("failure", [False, True])
def test_configuration_uses_native_wizard_and_never_leaves_curses(
    terminal, monkeypatch, failure
):
    import builtins
    import curses

    from kt_mcp import tui_forms

    operations = []
    shown = []

    def forbidden(*args):
        pytest.fail("native configuration must not leave curses or call input()")

    monkeypatch.setattr(curses, "endwin", forbidden)
    monkeypatch.setattr(builtins, "input", forbidden)
    ask = lambda prompt: ""
    ask_secret = lambda prompt: ""

    def wizard(screen, title, callback):
        operations.append(("wizard", title))
        return callback(ask=ask, ask_secret=ask_secret)

    def configure(**readers):
        assert readers == {"ask": ask, "ask_secret": ask_secret}
        operations.append(("configure",))
        if failure:
            raise module.ServiceError("fixture configure failure " + TUNNEL_TOKEN)
        terminal.controller.note = "Changes cancelled. MCP remains stopped."

    monkeypatch.setattr(tui_forms, "run_wizard", wizard)
    monkeypatch.setattr(
        tui_forms, "message", lambda screen, title, text: shown.append(text)
    )
    monkeypatch.setattr(terminal.controller, "configure", configure)
    module._configure(RecordingScreen([]), terminal.controller)

    assert operations == [
        ("wizard", "Configure " + terminal.manager.name),
        ("configure",),
    ]
    assert shown == [terminal.controller.note]
    assert TUNNEL_TOKEN not in terminal.controller.note


def test_color_styles_emphasize_the_link_and_semantic_states(monkeypatch):
    import curses

    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda *args: None)
    monkeypatch.setattr(curses, "color_pair", lambda index: index << 8)
    monkeypatch.setattr(curses, "COLORS", 8, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)

    styles = module._styles()

    assert styles["link"] & curses.A_BOLD
    assert styles["link"] & curses.A_COLOR
    assert len({styles["ok"], styles["warning"], styles["error"]}) == 3


def test_color_initialization_failure_falls_back_to_bold(monkeypatch):
    import curses

    def unavailable(*args):
        raise curses.error("terminal rejected color initialization")

    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", unavailable)
    monkeypatch.setattr(curses, "COLORS", 8, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)

    styles = module._styles()

    assert styles["link"] & curses.A_BOLD
    assert all(attribute & curses.A_COLOR == 0 for attribute in styles.values())
