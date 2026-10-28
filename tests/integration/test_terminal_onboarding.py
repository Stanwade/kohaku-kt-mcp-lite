"""Exercise onboarding and the Linux panel without registering services."""

import errno
import json
import os
import re
import secrets
import select
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_tui_parity import VisibleScreen

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() == 0,
    reason="terminal onboarding requires Linux and an ordinary user",
)


class TerminalSession:
    """Drive the real CLI using prompts, keeping captured credentials out of failures."""

    def __init__(self, arguments, *, cwd, environment, rows=30, columns=110):
        import fcntl
        import pty
        import struct
        import termios

        self.master, slave = pty.openpty()
        self.output = bytearray()
        self.position = 0
        self.native = not arguments or arguments[0] == "tui"
        self.screen = VisibleScreen(rows, columns)
        self.process = None
        try:
            fcntl.ioctl(
                slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0)
            )
            self.process = subprocess.Popen(
                [sys.executable, "-m", "kt_mcp", *arguments],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=environment,
                start_new_session=True,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)

    def pump(self, timeout=0.1):
        if not select.select([self.master], [], [], timeout)[0]:
            return False
        try:
            chunk = os.read(self.master, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            return False
        self.output.extend(chunk)
        self.screen.feed(chunk)
        return bool(chunk)

    def expect(self, prompt):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.pump()
            if self.native:
                # ncurses redraws changed cells only. Validate the current screen,
                # including wrapped labels, instead of searching old frame bytes.
                expected = "".join(prompt.split())
                if expected in "".join(self.screen.text.split()):
                    return
            else:
                expected = prompt.encode("utf-8")
                plain = re.sub(
                    rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\([0-~])", b"", self.output
                )
                found = plain.find(expected, self.position)
                if found >= 0:
                    self.position = found + len(expected)
                    return
            if self.process.poll() is not None:
                break
        # Never include a transcript: a hidden-input regression may expose secrets.
        pytest.fail(f"Terminal did not reach expected prompt: {prompt}", pytrace=False)

    def assert_native(self):
        import termios

        flags = termios.tcgetattr(self.master)[3]
        assert not flags & termios.ICANON
        assert not flags & termios.ECHO
        assert not re.search(rb"\x1b\[\?(?:1049|1047|47)l", self.output)

    def send(self, value):
        os.write(self.master, value.encode("utf-8"))

    def assert_hidden(self, value, *, since=0):
        if value.encode("utf-8") in self.output[since:]:
            pytest.fail("A credential was echoed to the terminal", pytrace=False)

    def assert_link_rendered(self, url, *, since=0):
        """Require the complete link in the current native frame."""
        if self.native:
            visible = re.sub(r"\s+", "", self.screen.text)
            rendered = url in visible
        else:
            plain = re.sub(
                rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\([0-~])", b"", self.output[since:]
            )
            rendered = url.encode("utf-8") in re.sub(rb"\s+", b"", plain)
        if not rendered:
            pytest.fail("The view did not render the complete MCP link", pytrace=False)

    def resize(self, rows, columns):
        import fcntl
        import signal
        import struct
        import termios

        fcntl.ioctl(
            self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0)
        )
        self.screen = VisibleScreen(rows, columns)
        self.process.send_signal(signal.SIGWINCH)

    def attributes_at(self, text):
        """Read the actual SGR state at a label, rather than finding any SGR byte."""
        position = self.output.find(text.encode("utf-8"))
        if position < 0:
            pytest.fail("The terminal styling target was not rendered", pytrace=False)
        bold = False
        foreground = None
        for match in re.finditer(rb"\x1b\[([0-9;]*)m", self.output[:position]):
            parameters = iter(int(value or b"0") for value in match[1].split(b";"))
            for value in parameters:
                if value == 0:
                    bold, foreground = False, None
                elif value == 1:
                    bold = True
                elif value == 22:
                    bold = False
                elif 30 <= value <= 37:
                    foreground = value - 30
                elif 90 <= value <= 97:
                    foreground = value - 90 + 8
                elif value == 39:
                    foreground = None
                elif value in (38, 48):
                    mode = next(parameters, None)
                    color = tuple(
                        next(parameters, None) for _ in range(3 if mode == 2 else 1)
                    )
                    if value == 38:
                        foreground = color
        return bold, foreground

    def finish(self):
        try:
            code = self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("Terminal did not exit within 15 seconds", pytrace=False)
        assert code == 0

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        finally:
            os.close(self.master)


def unit_status(name, environment):
    """Read only; a missing user bus does not prevent onboarding coverage."""
    if shutil.which("systemctl") is None:
        return None
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "--no-ask-password",
                "--no-pager",
                "show",
                "--property=LoadState,ActiveState,MainPID",
                name + ".service",
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values if "LoadState" in values else None


def assert_unregistered(name, environment, *, systemd_available):
    unit = name + ".service"
    assert not (Path(environment["XDG_CONFIG_HOME"]) / "systemd/user" / unit).exists()
    assert not (Path.home() / ".config/systemd/user" / unit).exists()
    if systemd_available:
        status = unit_status(name, environment)
        assert status is not None, "The initially available user manager disappeared"
        assert status["LoadState"] == "not-found"
        assert status.get("MainPID", "0") in {"", "0"}


def terminal_environment(tmp_path):
    environment = {
        **os.environ,
        "TERM": "xterm-256color",
        "LC_ALL": "C.UTF-8",
        "XDG_CONFIG_HOME": str(tmp_path / "xdg configuration"),
    }
    for key in (
        "KT_MCP_TOKEN",
        "TUNNEL_TOKEN",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "NO_COLOR",
    ):
        environment.pop(key, None)
    return environment


@pytest.fixture
def onboarding(tmp_path):
    workspace = tmp_path / "project with spaces 中文"
    workspace.mkdir()
    config = tmp_path / "new configuration" / "config.json"
    name = "kt-mcp-onboard-" + secrets.token_hex(6)
    environment = terminal_environment(tmp_path)
    status = unit_status(name, environment)
    systemd_available = status is not None
    assert_unregistered(name, environment, systemd_available=systemd_available)
    terminal = TerminalSession(
        ["tui", "--config", str(config), "--name", name],
        cwd=workspace,
        environment=environment,
    )
    try:
        yield terminal, workspace, config
    finally:
        terminal.close()
        assert_unregistered(name, environment, systemd_available=systemd_available)


def accept_workspace_and_port(terminal):
    terminal.expect("Workspace [")
    terminal.send("\n")
    terminal.expect("Port [")
    terminal.send("\n")
    terminal.expect("Mode [")


@pytest.mark.parametrize(
    "term,no_color,expected_bold,expected_color",
    [
        ("xterm-256color", False, True, True),
        ("xterm-256color", True, True, False),
        ("dumb", False, False, False),
        ("kt-mcp-test-unsupported", False, False, False),
    ],
    ids=["color-terminal", "no-color", "dumb-terminal", "unsupported-terminal"],
)
def test_setup_styles_the_current_question_in_a_real_terminal(
    tmp_path, term, no_color, expected_bold, expected_color
):
    workspace = tmp_path / "style project 中文"
    workspace.mkdir()
    config = tmp_path / "new configuration" / "config.json"
    environment = terminal_environment(tmp_path)
    environment["TERM"] = term
    if no_color:
        environment["NO_COLOR"] = "1"
    terminal = TerminalSession(
        ["init", "--config", str(config)], cwd=workspace, environment=environment
    )
    try:
        terminal.expect(f"Workspace [{workspace}]: ")
        for label in ("Kohaku KT MCP · Setup", "Workspace", str(workspace)):
            bold, color = terminal.attributes_at(label)
            assert bold is expected_bold
            if label == "Workspace":
                assert color is None
            else:
                assert (color is not None) is expected_color
        # Styling must reset before the input area and the ordinary help text.
        assert terminal.attributes_at("Enter = default") == (False, None)
        assert terminal.attributes_at("]: ") == (False, None)
        if not expected_bold:
            assert b"\x1b" not in terminal.output

        terminal.send(":q\n")
        terminal.expect("Setup cancelled; no configuration was saved.")
        terminal.finish()
        assert not config.parent.exists()
    finally:
        terminal.close()


def test_first_run_uses_current_directory_and_recovers_from_invalid_port(onboarding):
    terminal, workspace, config = onboarding
    terminal.expect(f"Workspace [{workspace}]: ")
    terminal.send("\n")
    terminal.expect("Port [")
    terminal.send("not-a-port\n")
    terminal.expect("Port must be an integer from 1 to 65535.")
    terminal.assert_native()
    # Native errors remain visible while the current question stays emphasized.
    assert terminal.attributes_at("Port [")[0]
    terminal.expect("Port [")
    terminal.send("9123\n")
    terminal.expect("Mode [")
    terminal.send("\n")
    terminal.expect("Review")
    terminal.expect("Save? [")
    assert terminal.attributes_at("Save?")[0]
    assert not config.exists()
    assert not config.parent.exists()
    before_save = bytes(terminal.output)

    terminal.send("\n")
    terminal.expect("[q] Quit")
    settings = json.loads(config.read_text(encoding="utf-8"))
    assert settings["workspace"] == str(workspace.resolve())
    assert settings["port"] == 9123
    assert settings["mode"] == "local"
    assert not config.with_suffix(".status.json").exists()
    if settings["token"].encode("ascii") in before_save:
        pytest.fail("Setup displayed its access token before saving", pytrace=False)
    url = "http://127.0.0.1:9123/mcp/" + settings["token"]
    terminal.assert_link_rendered(url)
    assert terminal.attributes_at("MCP link")[0]
    bold, color = terminal.attributes_at(url)
    assert bold and color is not None
    terminal.send("q")
    terminal.finish()


def test_cancelling_at_summary_creates_no_configuration_or_directories(
    onboarding, tmp_path
):
    terminal, _, config = onboarding
    before = set(tmp_path.rglob("*"))
    accept_workspace_and_port(terminal)
    terminal.send("\n")
    terminal.expect("Save? [")
    assert not config.exists()
    assert not config.parent.exists()
    terminal.assert_native()
    terminal.send(":q\n")
    terminal.finish()

    assert set(tmp_path.rglob("*")) == before
    assert not config.exists()


def test_named_tunnel_secret_is_hidden_in_a_real_terminal(onboarding, tmp_path):
    terminal, _, config = onboarding
    before = set(tmp_path.rglob("*"))
    accept_workspace_and_port(terminal)
    terminal.send("named\n")
    terminal.expect("HTTPS origin")
    terminal.send("https://mcp.example.com\n")
    terminal.expect("Tunnel token (hidden): ")
    tunnel_token = "test-only-" + secrets.token_urlsafe(32)
    terminal.send(tunnel_token + "\n")
    terminal.expect("Save? [")
    terminal.assert_hidden(tunnel_token)
    assert not config.exists()
    terminal.assert_native()
    terminal.send(":q\n")
    terminal.finish()

    assert set(tmp_path.rglob("*")) == before
    assert not config.exists()


def test_interactive_init_preserves_port_option_and_defaults_to_current_directory(
    tmp_path,
):
    workspace = tmp_path / "init project 中文"
    workspace.mkdir()
    config = tmp_path / "init configuration" / "config.json"
    terminal = TerminalSession(
        ["init", "--config", str(config), "--port", "9876"],
        cwd=workspace,
        environment=terminal_environment(tmp_path),
    )
    try:
        terminal.expect(f"Workspace [{workspace}]: ")
        terminal.send("\n")
        terminal.expect("Port [9876]: ")
        terminal.send("\n")
        terminal.expect("Mode [")
        terminal.send("\n")
        terminal.expect("Save? [")
        assert not config.exists()
        assert not config.parent.exists()

        terminal.send("\n")
        terminal.expect("Saved ")
        bold, color = terminal.attributes_at("Saved")
        assert bold and color is not None
        terminal.finish()
        settings = json.loads(config.read_text(encoding="utf-8"))
        assert settings["workspace"] == str(workspace.resolve())
        assert settings["port"] == 9876
        assert settings["mode"] == "local"
        assert not config.with_suffix(".status.json").exists()
        terminal.assert_hidden(settings["token"])
    finally:
        terminal.close()


def test_bare_terminal_command_enters_native_setup_and_cancel_returns_to_overview(
    tmp_path,
):
    workspace = tmp_path / "bare command project"
    workspace.mkdir()
    environment = terminal_environment(tmp_path)
    xdg_config = Path(environment["XDG_CONFIG_HOME"])
    before = set(tmp_path.rglob("*"))
    terminal = TerminalSession([], cwd=workspace, environment=environment)
    try:
        terminal.expect("Instance name [")
        terminal.assert_native()
        terminal.send("\n")
        terminal.expect(f"Workspace [{workspace}]: ")
        terminal.assert_native()
        assert not xdg_config.exists()
        terminal.send(":q\n")
        terminal.expect("Kohaku KT MCP · Instances")
        terminal.expect("No instances yet.")
        terminal.assert_native()
        terminal.send("q")
        terminal.finish()

        assert not xdg_config.exists()
        assert set(tmp_path.rglob("*")) == before
    finally:
        terminal.close()


def test_narrow_panel_keeps_help_and_details_credential_free(tmp_path):
    from kt_mcp.daemon import DaemonConfig, save_config
    from kt_mcp.desktop import DesktopSettings

    workspace = tmp_path / ("项目目录" * 8) / "工作区终点"
    workspace.mkdir(parents=True)
    config = tmp_path / ("配置目录" * 8) / "私密配置.json"
    token = secrets.token_urlsafe(24)
    tunnel_token = "test-only-tunnel-" + secrets.token_urlsafe(24)
    save_config(
        DaemonConfig(
            DesktopSettings(
                str(workspace), mode="named", public_origin="https://mcp.example.com"
            ),
            token,
            tunnel_token,
        ),
        config,
    )
    original = config.read_bytes()
    assert config.stat().st_mode & 0o077 == 0

    name = "kt-mcp-onboard-" + secrets.token_hex(6)
    environment = terminal_environment(tmp_path)
    systemd_available = unit_status(name, environment) is not None
    assert_unregistered(name, environment, systemd_available=systemd_available)
    terminal = TerminalSession(
        ["tui", "--config", str(config), "--name", name],
        cwd=workspace,
        environment=environment,
        rows=18,
        columns=64,
    )
    try:
        terminal.expect("[q] Quit")
        for label in ("MCP link", "[u]", "[o]", "[?]"):
            if label.encode("utf-8") not in terminal.output:
                pytest.fail(f"Narrow panel is missing {label}", pytrace=False)

        details_start = len(terminal.output)
        terminal.send("v")
        terminal.expect("Service details")
        terminal.expect("Workspace")
        # Curses can reuse part of an old label; newly drawn path text is stable.
        terminal.expect("项目目录")
        terminal.expect("配置目录")
        terminal.send("\x1b[6~" * 10)
        terminal.expect("Local HTTP port: 8765")
        for secret in (token, token[:12], token[-12:]):
            terminal.assert_hidden(secret, since=details_start)
        terminal.send("\n")
        terminal.expect("[q] Quit")

        help_start = len(terminal.output)
        terminal.send("?")
        terminal.expect("Panel help")
        terminal.expect("q returns to the overview")
        terminal.send("\x1b[6~" * 10)
        terminal.expect("Startup evidence does not")
        for secret in (token, token[:12], token[-12:]):
            terminal.assert_hidden(secret, since=help_start)
        terminal.send("\n")
        terminal.send("q")
        terminal.finish()

        # A tunnel credential must not appear even in the intended MCP link view.
        for secret in (tunnel_token, tunnel_token[:12], tunnel_token[-12:]):
            terminal.assert_hidden(secret)
        if config.read_bytes() != original:
            pytest.fail("Viewing the panel changed its configuration", pytrace=False)
        assert not config.with_suffix(".status.json").exists()
    finally:
        terminal.close()
        assert_unregistered(name, environment, systemd_available=systemd_available)


def test_narrow_link_view_keeps_long_url_and_exports_the_selected_config(tmp_path):
    from kt_mcp.daemon import DaemonConfig, save_config
    from kt_mcp.desktop import DesktopSettings

    workspace = tmp_path / "link project 中文"
    workspace.mkdir()
    config = tmp_path / "chosen config 中文.json"
    token = secrets.token_urlsafe(96)
    tunnel_token = "test-only-tunnel-" + secrets.token_urlsafe(24)
    url = "https://mcp.example.com/mcp/" + token
    save_config(
        DaemonConfig(
            DesktopSettings(
                str(workspace), mode="named", public_origin="https://mcp.example.com"
            ),
            token,
            tunnel_token,
        ),
        config,
    )
    original = config.read_bytes()
    name = "kt-mcp-onboard-" + secrets.token_hex(6)
    environment = terminal_environment(tmp_path)
    systemd_available = unit_status(name, environment) is not None
    assert_unregistered(name, environment, systemd_available=systemd_available)
    terminal = TerminalSession(
        ["tui", "--config", str(config), "--name", name],
        cwd=workspace,
        environment=environment,
        rows=18,
        columns=64,
    )
    try:
        terminal.expect("[q] Quit")
        terminal.assert_link_rendered(url)
        assert terminal.attributes_at("MCP link")[0]
        bold, color = terminal.attributes_at("https://mcp.example.com/mcp/")
        assert bold and color is not None

        link_start = len(terminal.output)
        terminal.send("u")
        terminal.expect("[w] Save file")
        terminal.assert_native()
        terminal.assert_link_rendered(url, since=link_start)
        terminal.assert_hidden(tunnel_token)

        # Export the full selected link inside the manager, including when its
        # configuration and destination paths contain spaces and Chinese text.
        export = tmp_path / "chosen link export 中文.txt"
        terminal.send("w")
        terminal.expect("Export file")
        terminal.assert_native()
        terminal.send(str(export) + "\n")
        terminal.expect("Export saved")
        terminal.assert_native()
        terminal.send("\n")
        terminal.expect("[w] Save file")
        terminal.send("q")
        terminal.expect("[q] Quit")
        terminal.send("q")
        terminal.finish()

        result = subprocess.run(
            [sys.executable, "-m", "kt_mcp", "link", "--config", str(config)],
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0 or result.stdout != (url + "\n").encode("utf-8"):
            pytest.fail("CLI export did not return the selected link", pytrace=False)
        if not export.exists() or export.read_bytes() != result.stdout:
            pytest.fail(
                "Native export differs from the complete CLI link", pytrace=False
            )
        assert export.stat().st_mode & 0o777 == 0o600
        if result.stderr:
            pytest.fail("Link export produced unexpected diagnostics", pytrace=False)
        terminal.assert_hidden(tunnel_token)
        if config.read_bytes() != original:
            pytest.fail("Viewing a link changed its configuration", pytrace=False)
        assert not config.with_suffix(".status.json").exists()
    finally:
        terminal.close()
        assert_unregistered(name, environment, systemd_available=systemd_available)


def test_no_color_panel_keeps_its_link_bold_without_foreground_color(tmp_path):
    from kt_mcp.daemon import DaemonConfig, save_config
    from kt_mcp.desktop import DesktopSettings

    config = tmp_path / "config.json"
    token = secrets.token_urlsafe(32)
    save_config(
        DaemonConfig(DesktopSettings(str(tmp_path), mode="local"), token), config
    )
    url = "http://127.0.0.1:8765/mcp/" + token
    name = "kt-mcp-onboard-" + secrets.token_hex(6)
    environment = terminal_environment(tmp_path)
    environment["NO_COLOR"] = "1"
    systemd_available = unit_status(name, environment) is not None
    assert_unregistered(name, environment, systemd_available=systemd_available)
    terminal = TerminalSession(
        ["tui", "--config", str(config), "--name", name],
        cwd=tmp_path,
        environment=environment,
    )
    try:
        terminal.expect("[q] Quit")
        terminal.assert_link_rendered(url)
        assert terminal.attributes_at("MCP link")[0]
        assert terminal.attributes_at(url) == (True, None)
        terminal.send("q")
        terminal.finish()
    finally:
        terminal.close()
        assert_unregistered(name, environment, systemd_available=systemd_available)


def configuration_fixture(tmp_path, *, registered=True):
    """A real terminal with a controlled systemctl, not a real service manager."""
    from types import SimpleNamespace

    from kt_mcp.daemon import DaemonConfig, save_config
    from kt_mcp.desktop import DesktopSettings
    from kt_mcp.service import render_unit

    workspace = tmp_path / "original workspace 中文"
    workspace.mkdir()
    destination = tmp_path / "configured workspace 中文"
    destination.mkdir()
    config = tmp_path / "private configuration" / "settings.json"
    token = secrets.token_urlsafe(32)
    tunnel_token = "hidden-tunnel-" + secrets.token_urlsafe(32)
    save_config(
        DaemonConfig(
            DesktopSettings(
                str(workspace), mode="named", public_origin="https://mcp.example.com"
            ),
            token,
            tunnel_token,
        ),
        config,
    )
    environment = terminal_environment(tmp_path)
    name = "kt-mcp-configure-" + secrets.token_hex(6)
    unit = Path(environment["XDG_CONFIG_HOME"]) / "systemd/user" / (name + ".service")
    if registered:
        unit.parent.mkdir(parents=True)
        unit.write_text(render_unit(config), encoding="utf-8")
        unit.chmod(0o644)
    state = tmp_path / "fixture-systemd-state.json"
    state.write_text(
        json.dumps(
            {
                "LoadState": "loaded" if registered else "not-found",
                "ActiveState": "active" if registered else "inactive",
                "SubState": "running" if registered else "dead",
                "UnitFileState": "enabled" if registered else "",
                "MainPID": str(os.getpid()) if registered else "0",
                "NRestarts": "0",
                "ExecMainStatus": "0",
                "FragmentPath": str(unit) if registered else "",
                "DropInPaths": "",
                "NeedDaemonReload": "no",
                "ControlGroup": "/kt-mcp-pty-fixture/" + name if registered else "",
            }
        ),
        encoding="utf-8",
    )
    calls = tmp_path / "fixture-systemd-calls.jsonl"
    program = tmp_path / "fixture_systemd.py"
    program.write_text(
        """import json, os, sys
from pathlib import Path
tool, arguments = sys.argv[1], sys.argv[2:]
with Path(os.environ["KT_MCP_TEST_SERVICE_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"tool":tool, "arguments":arguments}) + "\\n")
if tool == "loginctl":
    if "show-user" not in arguments:
        raise SystemExit("unexpected persistence change in terminal fixture")
    print("no")
    raise SystemExit(0)
path = Path(os.environ["KT_MCP_TEST_SERVICE_STATE"])
state = json.loads(path.read_text(encoding="utf-8"))
if "show" in arguments:
    for key, value in state.items():
        print(key + "=" + value)
elif "stop" in arguments:
    state.update(ActiveState="inactive", SubState="dead", MainPID="0", ControlGroup="")
    path.write_text(json.dumps(state), encoding="utf-8")
elif "daemon-reload" not in arguments:
    raise SystemExit("unexpected service action in terminal fixture")
""",
        encoding="utf-8",
    )
    binary = tmp_path / "controlled service tools"
    binary.mkdir()
    for tool in ("systemctl", "loginctl"):
        launcher = binary / tool
        launcher.write_text(
            "#!/bin/sh\nexec "
            + shlex.join([sys.executable, "-I", str(program), tool])
            + ' "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    environment.update(
        PATH=str(binary) + os.pathsep + environment.get("PATH", os.defpath),
        KT_MCP_TEST_SERVICE_CALLS=str(calls),
        KT_MCP_TEST_SERVICE_STATE=str(state),
    )
    return SimpleNamespace(
        workspace=workspace,
        destination=destination,
        config=config,
        token=token,
        tunnel_token=tunnel_token,
        environment=environment,
        name=name,
        unit=unit,
        state=state,
        calls=calls,
    )


def configuration_terminal(fixture):
    return TerminalSession(
        ["tui", "--config", str(fixture.config), "--name", fixture.name],
        cwd=fixture.workspace,
        environment=fixture.environment,
        rows=18,
        columns=64,
    )


def configuration_actions(fixture):
    records = [
        json.loads(line)
        for line in fixture.calls.read_text(encoding="utf-8").splitlines()
    ]
    return [
        action
        for record in records
        for action in ("stop", "start", "restart", "enable", "disable", "daemon-reload")
        if action in record["arguments"]
    ]


def test_configure_stays_in_curses_and_keeps_mcp_stopped_after_saving(tmp_path):
    fixture = configuration_fixture(tmp_path)
    owner = fixture.config.stat().st_uid, fixture.config.stat().st_gid
    terminal = configuration_terminal(fixture)
    try:
        terminal.expect("[q] Quit")
        terminal.send("o")
        terminal.expect("MCP will stop and stay offline while you edit.")
        terminal.expect("Stop MCP and configure? [y/N]")
        assert json.loads(fixture.state.read_text())["ActiveState"] == "active"
        wizard_start = len(terminal.output)
        terminal.send("y\n")
        terminal.expect("MCP stopped.")
        assert json.loads(fixture.state.read_text())["ActiveState"] == "inactive"
        terminal.expect("Workspace [")
        terminal.send(str(fixture.destination) + "\n")
        terminal.expect("Port [")
        terminal.send("9123\n")
        terminal.expect("Mode [")
        terminal.send("local\n")
        terminal.expect("Review")
        terminal.expect("Save?")
        terminal.send("\n")
        terminal.expect("MCP is stopped. [s] Start when ready.")
        terminal.expect("Enter/Esc/q: back")
        terminal.assert_native()
        assert terminal.attributes_at("Configure " + fixture.name)[0]
        terminal.assert_hidden(fixture.token, since=wizard_start)
        terminal.assert_hidden(fixture.tunnel_token, since=wizard_start)
        terminal.send("\n")
        terminal.expect("[q] Quit")
        terminal.assert_link_rendered(
            "http://127.0.0.1:9123/mcp/" + fixture.token, since=wizard_start
        )
        terminal.send("q")
        terminal.finish()

        saved = json.loads(fixture.config.read_text(encoding="utf-8"))
        assert saved["workspace"] == str(fixture.destination)
        assert saved["mode"] == "local" and saved["port"] == 9123
        if saved["token"] != fixture.token:
            pytest.fail("Reconfiguration rotated the MCP access token", pytrace=False)
        assert (fixture.config.stat().st_uid, fixture.config.stat().st_gid) == owner
        assert fixture.config.stat().st_mode & 0o777 == 0o600
        assert (
            "WorkingDirectory=" + str(fixture.destination) in fixture.unit.read_text()
        )
        assert json.loads(fixture.state.read_text())["ActiveState"] == "inactive"
        assert configuration_actions(fixture) == ["stop", "daemon-reload"]
        terminal.assert_hidden(fixture.tunnel_token)
    finally:
        terminal.close()


@pytest.mark.parametrize(
    "cancel", [False, True], ids=["save-defaults", "cancel-review"]
)
def test_configure_hidden_token_keeps_its_default_and_never_restarts(tmp_path, cancel):
    fixture = configuration_fixture(tmp_path)
    original = fixture.config.read_bytes()
    inode = fixture.config.stat().st_ino
    terminal = configuration_terminal(fixture)
    try:
        terminal.expect("[q] Quit")
        terminal.send("o")
        terminal.expect("Stop MCP and configure?")
        wizard_start = len(terminal.output)
        terminal.send("y\n")
        terminal.expect("MCP stopped.")
        for prompt in ("Workspace [", "Port [", "Mode [", "HTTPS origin ["):
            terminal.expect(prompt)
            terminal.send("\n")
        terminal.expect("Tunnel token (hidden; Enter keeps it)")
        terminal.send("\n")
        terminal.expect("Save?")
        terminal.send(":q\n" if cancel else "\n")
        terminal.expect("MCP remains stopped." if cancel else "MCP is stopped.")
        terminal.expect("Enter/Esc/q: back")
        terminal.assert_native()
        terminal.assert_hidden(fixture.token, since=wizard_start)
        terminal.assert_hidden(fixture.tunnel_token, since=wizard_start)
        terminal.send("\n")
        terminal.expect("[q] Quit")
        terminal.send("q")
        terminal.finish()

        if fixture.config.read_bytes() != original:
            pytest.fail(
                "Keeping or cancelling settings changed their saved values",
                pytrace=False,
            )
        if cancel:
            assert fixture.config.stat().st_ino == inode
        assert json.loads(fixture.state.read_text())["ActiveState"] == "inactive"
        assert configuration_actions(fixture) == (
            ["stop"] if cancel else ["stop", "daemon-reload"]
        )
        terminal.assert_hidden(fixture.tunnel_token)
    finally:
        terminal.close()


def test_configure_without_a_registered_unit_only_edits_settings(tmp_path):
    fixture = configuration_fixture(tmp_path, registered=False)
    terminal = configuration_terminal(fixture)
    try:
        terminal.expect("[q] Quit")
        terminal.send("o")
        terminal.expect("No service is registered.")
        terminal.expect("Edit settings? [y/N]")
        terminal.send("y\n")
        terminal.expect("Workspace [")
        terminal.send("\n")
        terminal.expect("Port [")
        terminal.send("\n")
        terminal.expect("Mode [")
        terminal.send("quick\n")
        terminal.expect("Save?")
        terminal.send("\n")
        terminal.expect("Saved. [i] Install/start when ready.")
        terminal.expect("Enter/Esc/q: back")
        terminal.assert_native()
        terminal.send("\n")
        terminal.expect("[q] Quit")
        terminal.send("q")
        terminal.finish()

        assert json.loads(fixture.config.read_text())["mode"] == "quick"
        assert not fixture.unit.exists()
        assert configuration_actions(fixture) == []
    finally:
        terminal.close()
