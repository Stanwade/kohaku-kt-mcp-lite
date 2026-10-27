"""Exercise first-run setup through a real terminal without registering services."""

import errno
import json
import os
import secrets
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() == 0,
    reason="terminal onboarding requires Linux and an ordinary user",
)


class TerminalSession:
    """Drive the real CLI using prompts, keeping captured credentials out of failures."""

    def __init__(self, config, name, *, cwd, environment):
        import fcntl
        import pty
        import struct
        import termios

        self.master, slave = pty.openpty()
        self.output = bytearray()
        self.position = 0
        self.process = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "kt_mcp",
                    "tui",
                    "--config",
                    str(config),
                    "--name",
                    name,
                ],
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

    def expect(self, prompt):
        expected = prompt.encode("utf-8")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            found = self.output.find(expected, self.position)
            if found >= 0:
                self.position = found + len(expected)
                return
            if select.select([self.master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                self.output.extend(chunk)
            elif self.process.poll() is not None:
                break
        # Do not include the transcript: it may contain a credential if the
        # hidden-input regression being tested has occurred.
        pytest.fail(f"Terminal did not reach expected prompt: {prompt}", pytrace=False)

    def send(self, value):
        os.write(self.master, value.encode("utf-8"))

    def assert_hidden(self, value):
        if value.encode("utf-8") in self.output:
            pytest.fail("A credential was echoed to the terminal", pytrace=False)

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


@pytest.fixture
def onboarding(tmp_path):
    workspace = tmp_path / "project with spaces 中文"
    workspace.mkdir()
    config = tmp_path / "new configuration" / "config.json"
    name = "kt-mcp-onboard-" + secrets.token_hex(6)
    environment = {
        **os.environ,
        "TERM": "xterm-256color",
        "LC_ALL": "C.UTF-8",
        "XDG_CONFIG_HOME": str(tmp_path / "xdg configuration"),
    }
    for key in ("KT_MCP_TOKEN", "TUNNEL_TOKEN", "PYTHONHOME", "PYTHONSTARTUP"):
        environment.pop(key, None)
    status = unit_status(name, environment)
    systemd_available = status is not None
    assert_unregistered(name, environment, systemd_available=systemd_available)
    terminal = TerminalSession(config, name, cwd=workspace, environment=environment)
    try:
        yield terminal, workspace, config
    finally:
        terminal.close()
        assert_unregistered(name, environment, systemd_available=systemd_available)


def accept_workspace_and_port(terminal):
    terminal.expect("Workspace directory [")
    terminal.send("\n")
    terminal.expect("Local HTTP port [")
    terminal.send("\n")
    terminal.expect("Connection mode (")


def test_first_run_uses_current_directory_and_recovers_from_invalid_port(onboarding):
    terminal, workspace, config = onboarding
    terminal.expect(
        "Current directory where this command was started: " + str(workspace)
    )
    terminal.expect("Workspace directory [")
    terminal.send("\n")
    terminal.expect("Local HTTP port [")
    terminal.send("not-a-port\n")
    terminal.expect("Port must be an integer from 1 to 65535.")
    terminal.expect("Local HTTP port [")
    terminal.send("9123\n")
    terminal.expect("Connection mode (")
    terminal.send("\n")
    terminal.expect("Review configuration before saving")
    terminal.expect("Save configuration?")
    assert not config.exists()
    assert not config.parent.exists()

    terminal.send("\n")
    terminal.expect("Quit panel")
    settings = json.loads(config.read_text(encoding="utf-8"))
    assert settings["workspace"] == str(workspace.resolve())
    assert settings["port"] == 9123
    assert settings["mode"] == "local"
    assert not config.with_suffix(".status.json").exists()
    terminal.assert_hidden(settings["token"])
    terminal.send("q")
    terminal.finish()


def test_cancelling_at_summary_creates_no_configuration_or_directories(
    onboarding, tmp_path
):
    terminal, _, config = onboarding
    before = set(tmp_path.rglob("*"))
    accept_workspace_and_port(terminal)
    terminal.send("\n")
    terminal.expect("Save configuration?")
    assert not config.exists()
    assert not config.parent.exists()
    terminal.send(":q\n")
    terminal.expect("Setup cancelled; no configuration was saved.")
    terminal.finish()

    assert set(tmp_path.rglob("*")) == before
    assert not config.exists()


def test_named_tunnel_secret_is_hidden_in_a_real_terminal(onboarding, tmp_path):
    terminal, _, config = onboarding
    before = set(tmp_path.rglob("*"))
    accept_workspace_and_port(terminal)
    terminal.send("named\n")
    terminal.expect("Public HTTPS origin")
    terminal.send("https://mcp.example.com\n")
    terminal.expect("Cloudflare Tunnel token (hidden): ")
    tunnel_token = "test-only-" + secrets.token_urlsafe(32)
    terminal.send(tunnel_token + "\n")
    terminal.expect("Save configuration?")
    terminal.assert_hidden(tunnel_token)
    assert not config.exists()
    terminal.send(":q\n")
    terminal.expect("Setup cancelled; no configuration was saved.")
    terminal.finish()

    assert set(tmp_path.rglob("*")) == before
    assert not config.exists()
