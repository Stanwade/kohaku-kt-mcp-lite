"""Real terminal coverage for management parity, with exact temporary resources.

The ordinary tests never register units. Set KT_MCP_TEST_SYSTEMD=1 as a
non-root Linux user to exercise two randomly named user services. No test
controls the default kt-mcp.service or changes the account's linger setting.
"""

import codecs
import errno
import hashlib
import json
import os
import re
import secrets
import select
import socket
import stat
import subprocess
import sys
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="real curses interaction requires an ordinary Linux user",
)


class ParityFailure(AssertionError):
    """A static failure message that cannot contain a private transcript."""


def require(condition, message):
    if not condition:
        raise ParityFailure(message)


def fingerprint(path):
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(
        path.stat().st_mode
    )


class VisibleScreen:
    """Small VT reader for the cursor/erase operations emitted by ncurses.

    Assertions use the current visible frame, not strings retained from an old
    frame. Attributes and terminal modes do not affect visible text.
    """

    def __init__(self, rows, columns):
        self.rows, self.columns = rows, columns
        self.cells = [[" "] * columns for _ in range(rows)]
        self.row = self.column = 0
        self.saved = (0, 0)
        self.top, self.bottom = 0, rows - 1
        self.pending = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    @property
    def text(self):
        return "\n".join("".join(line).rstrip() for line in self.cells)

    def _linefeed(self):
        if self.row == self.bottom:
            del self.cells[self.top]
            self.cells.insert(self.bottom, [" "] * self.columns)
        else:
            self.row = min(self.rows - 1, self.row + 1)

    def _put(self, character):
        width = 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        if unicodedata.combining(character):
            return
        if self.column >= self.columns:
            self.column = 0
            self._linefeed()
        self.cells[self.row][self.column] = character
        if width == 2 and self.column + 1 < self.columns:
            self.cells[self.row][self.column + 1] = ""
        self.column += width

    def _csi(self, raw, command):
        if raw.startswith(("?", ">", "!")):
            return
        values = [int(value or "0") for value in raw.split(";")] if raw else [0]
        amount = values[0] or 1
        if command in {"H", "f"}:
            self.row = min(self.rows - 1, amount - 1)
            self.column = (
                min(self.columns - 1, (values[1] or 1) - 1) if len(values) > 1 else 0
            )
        elif command == "A":
            self.row = max(0, self.row - amount)
        elif command == "B":
            self.row = min(self.rows - 1, self.row + amount)
        elif command == "C":
            self.column = min(self.columns - 1, self.column + amount)
        elif command == "D":
            self.column = max(0, self.column - amount)
        elif command == "G":
            self.column = min(self.columns - 1, amount - 1)
        elif command == "d":
            self.row = min(self.rows - 1, amount - 1)
        elif command == "J":
            first = (
                0 if values[0] in {1, 2, 3} else self.row * self.columns + self.column
            )
            last = (
                self.rows * self.columns
                if values[0] != 1
                else self.row * self.columns + self.column + 1
            )
            for position in range(first, last):
                self.cells[position // self.columns][position % self.columns] = " "
        elif command == "K":
            first = 0 if values[0] in {1, 2} else self.column
            last = self.columns if values[0] != 1 else self.column + 1
            self.cells[self.row][first:last] = [" "] * (last - first)
        elif command == "X":
            for column in range(self.column, min(self.columns, self.column + amount)):
                self.cells[self.row][column] = " "
        elif command in {"P", "@"}:
            line = self.cells[self.row]
            if command == "P":
                del line[self.column : self.column + amount]
                line.extend([" "] * (self.columns - len(line)))
            else:
                line[self.column : self.column] = [" "] * amount
                del line[self.columns :]
        elif command in {"L", "M"}:
            for _ in range(min(amount, self.bottom - self.row + 1)):
                if command == "L":
                    self.cells.insert(self.row, [" "] * self.columns)
                    del self.cells[self.bottom + 1]
                else:
                    del self.cells[self.row]
                    self.cells.insert(self.bottom, [" "] * self.columns)
        elif command == "r":
            self.top = amount - 1
            self.bottom = (
                (values[1] or self.rows) - 1 if len(values) > 1 else self.rows - 1
            )
            self.row = self.column = 0
        elif command == "s":
            self.saved = (self.row, self.column)
        elif command == "u":
            self.row, self.column = self.saved

    def feed(self, data):
        self.pending += self.decoder.decode(data)
        while self.pending:
            character = self.pending[0]
            if character == "\x1b":
                match = re.match(r"\x1b\[([0-?]*)([ -/]*)([@-~])", self.pending)
                if match:
                    self._csi(match[1], match[3])
                    self.pending = self.pending[match.end() :]
                    continue
                if self.pending.startswith("\x1b[") or len(self.pending) == 1:
                    return
                if self.pending.startswith("\x1b]"):
                    end = re.search(r"\x07|\x1b\\", self.pending[2:])
                    if end is None:
                        return
                    self.pending = self.pending[2 + end.end() :]
                    continue
                if self.pending[1] in "()":
                    if len(self.pending) < 3:
                        return
                    self.pending = self.pending[3:]
                    continue
                if self.pending[1] == "7":
                    self.saved = (self.row, self.column)
                elif self.pending[1] == "8":
                    self.row, self.column = self.saved
                elif self.pending[1] == "M":
                    if self.row == self.top:
                        self.cells.insert(self.top, [" "] * self.columns)
                        del self.cells[self.bottom + 1]
                    else:
                        self.row = max(0, self.row - 1)
                self.pending = self.pending[2:]
                continue
            self.pending = self.pending[1:]
            if character == "\r":
                self.column = 0
            elif character in "\n\v\f":
                self._linefeed()
            elif character == "\b":
                self.column = max(0, self.column - 1)
            elif character == "\t":
                self.column = min(self.columns - 1, (self.column // 8 + 1) * 8)
            elif character >= " " and character != "\x7f":
                self._put(character)


class Terminal:
    """Drive actual CLI input; never attach the captured transcript to a failure."""

    def __init__(self, arguments, *, cwd, environment, rows=18, columns=64):
        import fcntl
        import pty
        import struct
        import termios

        self.master, slave = pty.openpty()
        self.output = bytearray()
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
            data = os.read(self.master, 65536)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            return False
        self.output.extend(data)
        self.screen.feed(data)
        return bool(data)

    def expect(self, text, timeout=20):
        deadline = time.monotonic() + timeout
        wanted = " ".join(text.split())
        while time.monotonic() < deadline:
            self.pump()
            if wanted in " ".join(self.screen.text.split()):
                return
            if self.process.poll() is not None:
                break
        raise ParityFailure("Terminal did not display expected stage: " + text)

    def send(self, value):
        os.write(self.master, value.encode("utf-8"))

    def enter(self, prompt, value=""):
        self.expect(prompt)
        self.require_curses()
        self.send(value + "\n")

    def require_curses(self):
        import termios

        flags = termios.tcgetattr(self.master)[3]
        require(
            not flags & termios.ICANON and not flags & termios.ECHO,
            "An in-app form returned to canonical/echoing terminal input",
        )
        require(
            not re.search(rb"\x1b\[\?(?:1049|1047|47)l", self.output),
            "An in-app action left the curses alternate screen",
        )

    def choose(self, label):
        """Use the visible selection, allowing menus longer than a small screen."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.pump()
            selected = next(
                (
                    line.strip()[2:].strip()
                    for line in self.screen.text.splitlines()
                    if line.strip().startswith("> ")
                ),
                "",
            )
            if selected == label:
                self.require_curses()
                self.send("\n")
                return
            self.send("j")
            time.sleep(0.05)
        raise ParityFailure("Terminal menu did not offer action: " + label)

    def hidden(self, token):
        require(
            token.encode() not in self.output, "A secret was echoed in the terminal"
        )

    def finish(self):
        try:
            code = self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            raise ParityFailure("The TUI did not exit") from None
        require(code == 0, "The TUI exited unsuccessfully")

    def close(self):
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        finally:
            os.close(self.master)


def terminal_environment(*, config_home=None):
    environment = {
        **os.environ,
        "TERM": "xterm-256color",
        "LC_ALL": "C.UTF-8",
    }
    if config_home is not None:
        environment["XDG_CONFIG_HOME"] = str(config_home)
    for variable in (
        "KT_MCP_TOKEN",
        "TUNNEL_TOKEN",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "NO_COLOR",
    ):
        environment.pop(variable, None)
    return environment


@contextmanager
def terminal(arguments, *, cwd, environment, rows=18, columns=64):
    session = Terminal(
        arguments, cwd=cwd, environment=environment, rows=rows, columns=columns
    )
    try:
        yield session
    finally:
        session.close()


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def run_cli(arguments, *, cwd, environment, input_text=None):
    result = subprocess.run(
        [sys.executable, "-m", "kt_mcp", *arguments],
        cwd=cwd,
        env=environment,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    require(result.returncode == 0, "A parity CLI operation failed")
    return result.stdout


def wait_for(callback, message, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callback():
            return
        time.sleep(0.1)
    raise ParityFailure(message)


def service_state(manager):
    status = manager.status()
    return {
        key: status.get(key)
        for key in ("LoadState", "ActiveState", "MainPID", "UnitFileState")
    }


class OwnedServices:
    """Reserve unique resources, and remove only verified test-owned bindings."""

    def __init__(self):
        from kt_mcp.daemon import default_config_path
        from kt_mcp.service import ServiceManager

        self.prefix = "kt-mcp-pty-" + secrets.token_hex(5)
        self.resources = []
        self.default_manager = ServiceManager()
        self.default_config = default_config_path()
        self.default_snapshot = self.snapshot_default()

    def snapshot_default(self):
        return (
            fingerprint(self.default_config),
            fingerprint(self.default_manager.unit_path),
            service_state(self.default_manager),
        )

    def reserve(self, suffix):
        from kt_mcp import instances
        from kt_mcp.service import ServiceManager

        name = self.prefix + "-" + suffix
        manager = ServiceManager(name)
        path = instances.config_path(name)
        require(
            not path.exists()
            and not manager.unit_path.exists()
            and manager.status().get("LoadState") == "not-found",
            "A random test resource already exists",
        )
        self.resources.append((manager, path))
        return manager, path

    def cleanup(self):
        from kt_mcp.managed_service import inspect_service

        failures = []
        for manager, path in reversed(self.resources):
            try:
                if manager.unit_path.exists():
                    binding = inspect_service(
                        manager.name,
                        False,
                        expected_python=sys.executable,
                        expected_config=path,
                    )
                    require(binding is not None, "A test unit binding changed")
                    manager.uninstall()
                require(
                    manager.status().get("LoadState") == "not-found",
                    "An exact temporary unit remains registered",
                )
                path.with_suffix(".status.json").unlink(missing_ok=True)
                path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001 -- retain config on unsafe cleanup
                failures.append(manager.name)
        require(
            self.snapshot_default() == self.default_snapshot,
            "A temporary operation affected the default instance",
        )
        require(
            not failures,
            "Temporary cleanup failed; retain and inspect only: " + ", ".join(failures),
        )


@pytest.fixture
def owned_services():
    resources = OwnedServices()
    try:
        yield resources
    finally:
        resources.cleanup()


def catalog_path(environment, name):
    return Path(environment["XDG_CONFIG_HOME"]) / "kt-mcp/instances" / (name + ".json")


def create_form(session, name, workspace, port, *, mode="local", origin="", secret=""):
    session.send("n")
    session.enter("Instance name [", name)
    session.enter("Workspace [", str(workspace))
    session.enter("Port [", str(port))
    session.enter("Mode [", mode)
    if mode == "named":
        session.enter("HTTPS origin", origin)
        session.enter("Tunnel token (hidden)", secret)
    session.expect("Save?")
    session.require_curses()


def save_without_starting(session):
    session.send("\n")
    session.expect("Start instance now?")
    session.require_curses()
    # No is the visible default; accepting it must not register a service.
    session.choose("No, return")
    session.expect("Instances")
    session.require_curses()


@pytest.mark.parametrize("entry", ["overview", "details", "bare", "explicit"])
def test_native_creation_is_reachable_from_every_entry_at_64x18(tmp_path, entry):
    from kt_mcp.instances import read_config

    environment = terminal_environment(config_home=tmp_path / "xdg")
    seed_name = "kt-mcp-pty-seed-" + secrets.token_hex(4)
    new_name = "kt-mcp-pty-new-" + secrets.token_hex(4)
    seed_workspace = tmp_path / "existing workspace"
    new_workspace = tmp_path / "new workspace"
    seed_workspace.mkdir()
    new_workspace.mkdir()
    run_cli(
        ["instance", "create", seed_name, "--workspace", str(seed_workspace)],
        cwd=tmp_path,
        environment=environment,
    )
    seed_path = catalog_path(environment, seed_name)
    seed_before = fingerprint(seed_path)
    arguments = {
        "overview": ["tui"],
        "details": ["tui"],
        "bare": [],
        "explicit": ["tui", "--config", str(seed_path), "--name", seed_name],
    }[entry]
    port = available_port()
    with terminal(arguments, cwd=seed_workspace, environment=environment) as session:
        session.expect("Kohaku KT MCP")
        if entry == "details":
            session.expect("Instances")
            session.send("\n")
            session.expect("Unit:")
        elif entry == "explicit":
            session.expect("Unit:")
        create_form(session, new_name, new_workspace, port)
        save_without_starting(session)
        session.expect("Selected: " + new_name)
        path = catalog_path(environment, new_name)
        config = read_config(path)
        require(
            config.settings.workspace == str(new_workspace.resolve())
            and config.settings.port == port
            and config.settings.mode == "local",
            "The native form did not save the selected workspace, port, and mode",
        )
        require(
            fingerprint(seed_path) == seed_before,
            "Creating another instance rewrote the existing configuration",
        )
        require(
            not (
                Path(environment["XDG_CONFIG_HOME"])
                / "systemd/user"
                / (new_name + ".service")
            ).exists(),
            "Declining Start registered a service",
        )

        # Returning from detail and opening/cancelling a second form are native,
        # including the explicit-config compatibility entry.
        session.send("\n")
        session.expect("Unit:")
        session.send("b")
        session.expect("Instances")
        session.send("n")
        session.enter("Instance name [", ":q")
        session.expect("Instances")
        session.require_curses()
        session.send("q")
        session.finish()


def test_native_port_conflict_retains_named_settings_and_masks_token(tmp_path):
    from kt_mcp.instances import read_config

    environment = terminal_environment(config_home=tmp_path / "xdg")
    seed_name = "kt-mcp-pty-reserved-" + secrets.token_hex(4)
    new_name = "kt-mcp-pty-conflict-" + secrets.token_hex(4)
    seed_workspace = tmp_path / "reserved workspace"
    new_workspace = tmp_path / "retained workspace"
    seed_workspace.mkdir()
    new_workspace.mkdir()
    reserved = available_port()
    run_cli(
        [
            "instance",
            "create",
            seed_name,
            "--workspace",
            str(seed_workspace),
            "--port",
            str(reserved),
        ],
        cwd=tmp_path,
        environment=environment,
    )
    seed_before = fingerprint(catalog_path(environment, seed_name))
    path = catalog_path(environment, new_name)
    tunnel_secret = "pty-secret-" + secrets.token_urlsafe(24)
    origin = "https://pty-" + secrets.token_hex(6) + ".example.invalid"
    with terminal(["tui"], cwd=tmp_path, environment=environment) as session:
        session.expect("Instances")
        create_form(
            session,
            new_name,
            new_workspace,
            reserved,
            mode="named",
            origin=origin,
            secret=tunnel_secret,
        )
        session.send("\n")
        session.expect("is reserved by")
        session.expect("Save?")
        session.require_curses()
        require(not path.exists(), "A conflicting configuration was saved")
        session.hidden(tunnel_secret)
        session.send("edit\n")
        session.enter("Workspace [")
        corrected = available_port()
        while corrected == reserved:
            corrected = available_port()
        session.enter("Port [", str(corrected))
        session.enter("Mode [")
        session.enter("HTTPS origin")
        session.enter("Tunnel token (hidden; Enter keeps it)")
        session.expect("Save?")
        save_without_starting(session)
        config = read_config(path)
        require(
            config.settings.workspace == str(new_workspace.resolve())
            and config.settings.port == corrected
            and config.settings.mode == "named"
            and config.settings.public_origin == origin
            and config.tunnel_token == tunnel_secret,
            "Retrying a port conflict lost accepted form input",
        )
        require(
            fingerprint(catalog_path(environment, seed_name)) == seed_before,
            "A conflict retry altered the other instance",
        )
        session.hidden(tunnel_secret)
        session.send("q")
        session.finish()


def open_action(session, label):
    session.send("a")
    session.expect("Actions")
    session.choose(label)


def test_native_exports_match_cli_without_overwriting_existing_files(tmp_path):
    environment = terminal_environment(config_home=tmp_path / "xdg")
    name = "kt-mcp-pty-export-" + secrets.token_hex(4)
    workspace = tmp_path / "export workspace"
    workspace.mkdir()
    run_cli(
        ["instance", "create", name, "--workspace", str(workspace)],
        cwd=tmp_path,
        environment=environment,
    )
    path = catalog_path(environment, name)
    before = fingerprint(path)
    cases = [
        ("Export MCP link", "MCP link", ["instance", "link", name], "link.txt"),
        (
            "Export HTTP client JSON",
            "HTTP client configuration",
            ["instance", "client-config", name],
            "http.json",
        ),
        (
            "Export stdio client JSON",
            "Stdio client configuration",
            ["instance", "client-config", name, "--transport", "stdio"],
            "stdio.json",
        ),
    ]
    with terminal(
        ["tui", "--config", str(path), "--name", name],
        cwd=workspace,
        environment=environment,
    ) as session:
        session.expect("Unit:")
        for label, title, command, filename in cases:
            expected = run_cli(command, cwd=tmp_path, environment=environment)
            open_action(session, label)
            session.expect(title)
            session.send("w")
            session.expect("Export file (a new file; existing files are preserved)")
            if filename == "link.txt":
                occupied = tmp_path / "preserve-this.txt"
                occupied.write_text("must stay unchanged\n", encoding="utf-8")
                session.send(str(occupied) + "\n")
                session.expect("Export not saved:")
                require(
                    occupied.read_text(encoding="utf-8") == "must stay unchanged\n",
                    "A native export overwrote an existing file",
                )
            output = tmp_path / filename
            session.enter(
                "Export file (a new file; existing files are preserved)", str(output)
            )
            session.expect("Export saved")
            session.require_curses()
            require(
                output.read_text(encoding="utf-8") == expected,
                "Native exported content differs from the public CLI",
            )
            require(
                stat.S_IMODE(output.stat().st_mode) == 0o600,
                "The native credential export is not private",
            )
            session.send("\n")
            session.expect(title)
            session.send("q")
            session.expect("Unit:")
        require(fingerprint(path) == before, "Exporting changed saved configuration")
        session.send("q")
        session.finish()


def test_native_explicit_config_creation_and_system_authorization_cancel(tmp_path):
    from kt_mcp.instances import read_config

    environment = terminal_environment(config_home=tmp_path / "xdg")
    path = tmp_path / "explicit-private.json"
    workspace = tmp_path / "explicit workspace"
    workspace.mkdir()
    name = "kt-mcp-pty-open-" + secrets.token_hex(4)
    port = available_port()
    with terminal(["tui"], cwd=workspace, environment=environment) as session:
        session.expect("Instances")
        open_action(session, "Open / create configuration")
        session.enter("Configuration file", str(path))
        session.enter("Service name", name)
        session.enter("Service scope (user/system)", "user")
        session.enter("Workspace [", str(workspace))
        session.enter("Port [", str(port))
        session.enter("Mode [", "local")
        session.enter("Save?")
        session.expect("Unit:")
        config = read_config(path)
        require(
            config.settings.workspace == str(workspace.resolve())
            and config.settings.port == port,
            "Creating an explicit configuration saved different form values",
        )
        before = fingerprint(path)
        open_action(session, "Open / create configuration")
        session.enter("Configuration file", str(path))
        session.enter("Service name", name)
        session.enter("Service scope (user/system)", "system")
        session.expect("Administrator authorization")
        session.require_curses()
        session.choose("No, return")
        session.expect("Unit:")
        session.require_curses()
        require(
            fingerprint(path) == before, "Declining authorization changed configuration"
        )
        require(
            not (Path("/etc/systemd/system") / (name + ".service")).exists()
            and not (
                Path(environment["XDG_CONFIG_HOME"])
                / "systemd/user"
                / (name + ".service")
            ).exists(),
            "Opening a configuration registered a service",
        )
        session.send("b")
        session.expect("Instances")
        session.send("q")
        session.finish()


def ready_pid(manager, path):
    state = manager.status()
    if state.get("ActiveState") != "active" or state.get("MainPID") in {"0", "", None}:
        return None
    try:
        record = json.loads(
            path.with_suffix(".status.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    pid = int(state["MainPID"])
    return pid if record.get("pid") == pid else None


async def read_workspace_marker(path, expected):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from kt_mcp.daemon import client_url
    from kt_mcp.instances import read_config

    url = client_url(read_config(path), path)
    async with (
        httpx.AsyncClient(trust_env=False, timeout=10) as http,
        streamable_http_client(url, http_client=http) as (reader, writer, _),
        ClientSession(reader, writer) as client,
    ):
        await client.initialize()
        result = await client.call_tool("read", {"path": "marker.txt"})
        require(
            not result.isError
            and expected in result.structuredContent.get("output", ""),
            "The independent instance did not serve its own workspace",
        )


def failure_detail(error):
    children = getattr(error, "exceptions", ())
    if children:
        return "; ".join(failure_detail(child) for child in children)
    return str(error) if isinstance(error, ParityFailure) else type(error).__name__


@pytest.mark.skipif(
    os.environ.get("KT_MCP_TEST_SYSTEMD") != "1",
    reason="set KT_MCP_TEST_SYSTEMD=1 for exact temporary real-service lifecycle checks",
)
def test_real_tui_cli_lifecycle_parity_and_independent_peer(tmp_path, owned_services):
    import asyncio

    from kt_mcp.instances import read_config

    environment = terminal_environment()
    peer, peer_path = owned_services.reserve("peer")
    target, target_path = owned_services.reserve("new")
    peer_workspace = tmp_path / "peer workspace"
    target_workspace = tmp_path / "terminal workspace"
    peer_workspace.mkdir()
    target_workspace.mkdir()
    (peer_workspace / "marker.txt").write_text("peer-stays-online", encoding="utf-8")
    (target_workspace / "marker.txt").write_text("terminal-workspace", encoding="utf-8")
    linger_before = target.linger_status()
    failure = None
    stage = "creating the independent CLI peer"

    def peer_unchanged():
        current = peer.status()
        require(
            current.get("ActiveState") == "active"
            and current.get("MainPID") == str(peer_pid),
            "An action on the selected instance changed the independent peer",
        )
        asyncio.run(read_workspace_marker(peer_path, "peer-stays-online"))

    try:
        run_cli(
            [
                "instance",
                "create",
                peer.name,
                "--workspace",
                str(peer_workspace),
                "--start",
            ],
            cwd=tmp_path,
            environment=environment,
        )
        wait_for(
            lambda: ready_pid(peer, peer_path), "The CLI peer did not become ready"
        )
        peer_pid = ready_pid(peer, peer_path)
        require(
            peer.status().get("UnitFileState") == "disabled",
            "CLI Start enabled automatic startup",
        )
        stage = "creating and starting the selected instance through curses"
        with terminal(
            ["tui", "--instance", peer.name], cwd=tmp_path, environment=environment
        ) as session:
            session.expect("Kohaku KT MCP")
            create_form(session, target.name, target_workspace, available_port())
            save_without_starting(session)
            session.expect("Selected: " + target.name)
            require(
                not target.unit_path.exists(), "Saving without Start installed a unit"
            )
            config = read_config(target_path)
            require(
                config.token != read_config(peer_path).token,
                "The TUI and CLI instances share an access token",
            )
            before = fingerprint(target_path)
            open_action(session, "Start instance")
            wait_for(
                lambda: ready_pid(target, target_path), "TUI Start did not become ready"
            )
            session.expect("Instances")
            require(
                target.status().get("UnitFileState") == "disabled"
                and target.linger_status() == linger_before,
                "TUI Start changed automatic startup or user linger",
            )
            peer_unchanged()
            asyncio.run(read_workspace_marker(target_path, "terminal-workspace"))

            stage = "changing only explicit automatic-start settings"
            target_pid = ready_pid(target, target_path)
            open_action(session, "Enable automatic startup")
            session.expect("Apply running state too?")
            session.send("\x1b")
            session.expect("Instances")
            require(
                target.status().get("UnitFileState") == "disabled"
                and ready_pid(target, target_path) == target_pid,
                "Cancelling the automatic-start dialog changed service settings",
            )
            open_action(session, "Enable automatic startup")
            session.expect("Apply running state too?")
            session.choose("Only change automatic startup")
            wait_for(
                lambda: target.status().get("UnitFileState") == "enabled",
                "Explicit Enable did not enable the selected service",
            )
            require(
                ready_pid(target, target_path) == target_pid,
                "Enabling automatic startup without now restarted the instance",
            )
            session.expect("Instances")
            open_action(session, "Disable automatic startup")
            session.expect("Apply running state too?")
            session.choose("Also stop now")
            wait_for(
                lambda: target.status().get("MainPID") == "0",
                "Disable with now did not stop the selected service",
            )
            require(
                target.status().get("UnitFileState") == "disabled",
                "Disable did not clear automatic startup",
            )
            peer_unchanged()

            stage = "restarting through CLI and cancelling a confirmed native configuration pause"
            run_cli(
                ["instance", "start", target.name],
                cwd=tmp_path,
                environment=environment,
            )
            wait_for(
                lambda: ready_pid(target, target_path),
                "CLI Start did not restore the TUI instance",
            )
            session.expect("Instances")
            session.send("o")
            session.expect("MCP will stop and stay offline while you edit.")
            session.enter("Stop MCP and configure?", "y")
            session.expect("Workspace [")
            require(
                target.status().get("MainPID") == "0",
                "Configuration did not pause the selected service",
            )
            session.send(":q\n")
            session.expect("Configuration")
            session.send("\n")
            session.expect("Instances")
            session.require_curses()
            require(
                target.status().get("MainPID") == "0"
                and fingerprint(target_path) == before,
                "Cancelling configuration restarted the service or changed saved settings",
            )
            peer_unchanged()

            stage = "viewing native logs and uninstalling while retaining configuration"
            run_cli(
                ["instance", "start", target.name],
                cwd=tmp_path,
                environment=environment,
            )
            wait_for(
                lambda: ready_pid(target, target_path),
                "The selected service did not restart",
            )
            open_action(session, "View / follow logs")
            session.expect("Service logs - Following")
            session.send("n")
            session.enter("Log lines (1-1000)", "20")
            session.expect("20 lines")
            session.send("f")
            session.expect("Service logs - Paused")
            session.send("q")
            session.expect("Instances")
            open_action(session, "Uninstall service (keep configuration)")
            session.expect("Uninstall service")
            session.choose("Yes, continue")
            wait_for(
                lambda: not target.unit_path.exists(),
                "Native Uninstall did not remove its unit",
            )
            session.expect("Instances")
            require(
                target.status().get("LoadState") == "not-found"
                and fingerprint(target_path) == before
                and (target_workspace / "marker.txt").read_text(encoding="utf-8")
                == "terminal-workspace",
                "Uninstall did not retain configuration and workspace",
            )
            require(
                target.linger_status() == linger_before,
                "Native actions changed account linger",
            )
            peer_unchanged()
            session.require_curses()
            session.send("q")
            session.finish()

        stage = "re-registering and uninstalling the retained instance through CLI"
        run_cli(
            ["instance", "start", target.name], cwd=tmp_path, environment=environment
        )
        wait_for(
            lambda: ready_pid(target, target_path),
            "The retained configuration could not start again",
        )
        require(
            target.status().get("UnitFileState") == "disabled",
            "Re-registering implicitly enabled startup",
        )
        run_cli(
            ["instance", "uninstall", target.name],
            cwd=tmp_path,
            environment=environment,
        )
        require(
            fingerprint(target_path) == before,
            "CLI Uninstall changed retained configuration",
        )
        peer_unchanged()
    except Exception as error:  # noqa: BLE001 -- redact nested HTTP failures
        failure = "TUI parity " + stage + " failed: " + failure_detail(error)
    if failure:
        # Outside the exception handler, to avoid chaining private HTTP URLs.
        pytest.fail(failure, pytrace=False)
