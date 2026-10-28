"""Native update dialogs that run the same transactional updater as the CLI."""

import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from kt_mcp import __version__, releases
from kt_mcp import managed_service as services
from kt_mcp import update as updater
from kt_mcp.onboarding import SetupCancelled

MAX_LOG_BYTES = 65536
MAX_LINE = 4096
OUTPUT_BATCH = 128
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class ReloadTUI(Exception):
    """Ask the caller to reload the manager after curses has restored the terminal."""

    def __init__(self, argv: list[str]):
        self.argv = list(argv)
        super().__init__("Reload the terminal manager.")


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    lines: tuple[str, ...]
    cancelled: bool = False


def update_command(python: Path, *, check=False, version=None, name=None) -> list[str]:
    arguments = [str(python), "-I", "-m", "kt_mcp", "update"]
    if check:
        arguments.append("--check")
    if version is not None:
        arguments.extend(["--version", releases.version_text(version)])
    if name is not None:
        services._name(name)
        arguments.extend(["--name", name])
    return arguments


def _environment():
    environment = releases.clean_environment()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["NO_COLOR"] = "1"
    return environment


def _cli_available(python: Path) -> bool:
    try:
        result = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                (
                    "import sys,kt_mcp.cli,kt_mcp.update as u; "
                    "sys.exit(0 if hasattr(u, 'recoverable_installation') else 1)"
                ),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_environment(),
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def recovery_command(python: Path, prefix: Path) -> list[str]:
    """Use a validated worker if the public recovery CLI is broken or unavailable."""
    installation = updater.recoverable_installation(prefix)
    if _cli_available(python):
        return [str(python), "-I", "-m", "kt_mcp", "update", "--recover"]
    return updater.recovery_argv(installation)


def _log_presenter(redact):
    hide_command = False

    def present(value):
        nonlocal hide_command
        line = _ANSI.sub("", str(value)).replace("\r", "")
        if hide_command and line.strip():
            hide_command = False
            return ""
        if "Recovery needs attention." in line or "Recover it with:" in line:
            hide_command = True
            line = (
                "Recovery is needed. Choose Recover interrupted update in this panel."
            )
        elif "Update completed; cleanup needs attention. Run:" in line:
            hide_command = True
            line = (
                "Update finished; choose Recover interrupted update to finish cleanup."
            )
        line = line.replace(
            "Run kt-mcp update to install.",
            "Choose Install update in this panel to continue.",
        ).replace(
            "Stop the other instances first; use --name for the service to restart.",
            "Stop the other instances in the manager first; select the service here.",
        )
        line = re.sub(r"/mcp/[^\s/\"'<>]+", "/mcp/[REDACTED]", line)
        line = re.sub(
            r"(?i)(\b[a-z][a-z0-9+.-]{0,31}://)[^\s/?#]*@",
            r"\1[REDACTED]@",
            line,
        )
        return redact(line)

    return present


class UpdateProcess:
    """A bounded output pump; only the curses thread reads keys or draws."""

    def __init__(self, command, *, redact=lambda value: value):
        self.command = list(command)
        self.process = None
        self.cancelled = False
        self._queue = queue.Queue(maxsize=OUTPUT_BATCH)
        self._finished = threading.Event()
        self._lines = deque()
        self._bytes = 0
        self._present = _log_presenter(redact)

    @property
    def lines(self):
        return tuple(self._lines)

    def start(self):
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=_environment(),
            start_new_session=os.name == "posix",
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            with self.process.stdout as output:
                while chunk := output.readline(MAX_LINE + 1):
                    value = chunk.rstrip("\n")
                    try:
                        self._queue.put_nowait(value)
                    except queue.Full:
                        try:
                            self._queue.get_nowait()
                        except queue.Empty:
                            pass
                        self._queue.put_nowait(value)
        finally:
            self._finished.set()

    def drain(self):
        # A child can refill the queue continuously. Yield to curses after a
        # bounded batch so rendering and cancellation remain responsive.
        for _ in range(OUTPUT_BATCH):
            try:
                line = self._present(self._queue.get_nowait())
            except queue.Empty:
                return
            if not line:
                continue
            self._lines.append(line)
            self._bytes += len(line.encode("utf-8"))
            while self._bytes > MAX_LOG_BYTES or len(self._lines) > 500:
                self._bytes -= len(self._lines.popleft().encode("utf-8"))

    def poll(self):
        self.drain()
        code = self.process.poll()
        if code is not None and self._finished.is_set() and self._queue.empty():
            return code
        return None

    def cancel(self):
        if self.cancelled or self.process.poll() is not None:
            return
        self.cancelled = True
        try:
            if os.name == "posix":
                os.killpg(self.process.pid, signal.SIGINT)
            else:
                self.process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass


def _progress(screen, title, command, *, redact=lambda value: value) -> ProcessResult:
    import curses

    from kt_mcp import tui, tui_forms

    # Preload the presentation code before the child can replace installed files.
    put, wrap = tui._put, tui.wrap_display
    styles = tui._styles()
    job = UpdateProcess(command, redact=redact)
    offset = 0
    follow = True
    screen.timeout(100)
    job.start()
    try:
        while True:
            code = job.poll()
            height, width = screen.getmaxyx()
            screen.erase()
            put(screen, 0, 1, title, styles["title"])
            status = (
                ("Completed" if code == 0 else "Did not complete")
                if code is not None
                else (
                    "Cancelling and restoring; please wait..."
                    if job.cancelled
                    else "Working; other management actions are paused."
                )
            )
            put(screen, 1, 1, status, styles["warning"] if code else styles["muted"])
            if height >= 10 and width >= 40:
                lines = wrap("\n".join(job.lines) or "Starting...", width - 4)
                room = height - 5
                maximum = max(0, len(lines) - room)
                offset = maximum if follow else min(maximum, max(0, offset))
                for row, line in enumerate(lines[offset : offset + room], 3):
                    put(screen, row, 2, line)
                footer = (
                    "Enter/Esc: continue   Up/Down/PgUp/PgDn: scroll"
                    if code is not None
                    else (
                        "Recovery in progress; wait for completion."
                        if job.cancelled
                        else "c/Esc: cancel and restore   Up/Down: scroll"
                    )
                )
                put(screen, height - 1, 1, footer, styles["muted"])
            else:
                room = 1
                put(screen, 3, 1, "Resize to 40 x 10. Esc requests cancellation.")
            screen.refresh()
            try:
                key = screen.getch()
            except (KeyboardInterrupt, EOFError):
                key = 3
            if code is not None and key in (
                10,
                13,
                27,
                3,
                4,
                ord("q"),
                curses.KEY_ENTER,
            ):
                return ProcessResult(code, job.lines, job.cancelled)
            if code is None and key in (27, 3, 4, ord("c"), ord("C"), ord("q")):
                if not job.cancelled:
                    try:
                        confirmed = tui_forms.confirm(
                            screen,
                            "Cancel update?",
                            "The updater will stop its work and restore the previous "
                            "installation when needed. Wait for recovery to finish.",
                        )
                    except (SetupCancelled, KeyboardInterrupt, EOFError):
                        confirmed = False
                    if confirmed:
                        job.cancel()
                    screen.timeout(100)
            elif key in (curses.KEY_UP, curses.KEY_PPAGE, curses.KEY_HOME):
                follow = False
                offset = (
                    0
                    if key == curses.KEY_HOME
                    else offset - (room if key == curses.KEY_PPAGE else 1)
                )
            elif key in (curses.KEY_DOWN, curses.KEY_NPAGE):
                follow = False
                offset += room if key == curses.KEY_NPAGE else 1
            elif key == curses.KEY_END:
                follow = True
    except BaseException:
        # A terminal failure must still let the transaction perform its rollback.
        job.cancel()
        while job.poll() is None:
            time.sleep(0.1)
        raise
    finally:
        screen.timeout(250)
        screen.clearok(True)


def _reload_options(panel):
    if panel is None:
        return []
    if getattr(panel, "return_to_list", False) and not panel.manager.system:
        return ["--instance", panel.manager.name]
    arguments = ["--config", str(panel.config_path), "--name", panel.manager.name]
    if panel.manager.system:
        arguments.append("--system")
    return arguments


def _choose_version(screen, current):
    from kt_mcp import tui_forms

    def edit(*, ask, ask_secret):
        while True:
            value = ask(
                f"Release version [{current or 'latest'}] "
                "(Enter keeps it; latest selects newest): "
            ).strip()
            if not value:
                return current
            if value.lower() == "latest":
                return None
            try:
                return releases.version_text(value)
            except releases.ReleaseError as error:
                print(str(error))

    return tui_forms.run_wizard(screen, "Target release", edit)


def _choose_service(screen, python):
    from kt_mcp import tui_forms

    bindings = services.find_services(python)
    options = [("auto", "Automatically choose a managed user service")]
    options.extend(
        (
            binding.name,
            f"{binding.name} · {binding.status.get('ActiveState', 'unknown')}",
        )
        for binding in bindings
        if not binding.system
    )
    selected = tui_forms.choose(
        screen,
        "Service associated with this update",
        options,
        body="Only services using this installation are listed. Other running "
        "instances must be stopped from the manager before installing. "
        "System services are managed separately.",
    )
    return selected


def run_update(screen, panel=None):
    """Provide every public updater action without leaving the curses application."""
    from kt_mcp import tui_forms

    python = Path(os.path.abspath(sys.executable))
    venv = Path(sys.prefix).absolute()
    prefix = (
        venv.parent if venv.name == "venv" and sys.prefix != sys.base_prefix else None
    )
    version = None
    name = None
    redact = panel.redact if panel is not None else (lambda value: value)
    while True:
        action = tui_forms.choose(
            screen,
            "Update Kohaku KT MCP",
            [
                ("check", "Check for updates"),
                ("install", "Install update"),
                ("version", f"Target release: {version or 'latest stable'}"),
                ("service", f"Associated service: {name or 'automatic'}"),
                ("recover", "Recover interrupted update"),
                ("help", "Update and shared-installation help"),
                ("back", "Return to manager"),
            ],
            body=f"Current version: {__version__}\n"
            "All instances using this installation share the program version.\n"
            "Checks keep services running. Installation may pause its associated service.",
        )
        if action in (None, "back"):
            return
        try:
            if action == "version":
                version = _choose_version(screen, version)
                continue
            if action == "service":
                selected = _choose_service(screen, python)
                if selected is not None:
                    name = None if selected == "auto" else selected
                continue
            if action == "help":
                tui_forms.message(
                    screen,
                    "Updating from this manager",
                    "Check, choose a stable release, and install from this panel.\n\n"
                    "The updater verifies packages before pausing its associated user "
                    "service. It restores that service's original running/stopped state. "
                    "Other active instances sharing the installation must first be "
                    "stopped from their service menus; return here afterwards.\n\n"
                    "An active system service also blocks updating. System service "
                    "controls remain in its explicit system manager. Updates and "
                    "recovery require the original ordinary installation account.\n\n"
                    "If an update was interrupted, choose Recover interrupted update "
                    "here. The existing saved worker can recover even when the installed "
                    "CLI is damaged. Successful installation/recovery reloads the UI.",
                )
                continue
            if action == "install" and not tui_forms.confirm(
                screen,
                "Install verified update?",
                f"Target: {version or 'latest stable release'}.\n"
                "This updates the shared program installation. Its associated running "
                "user service may pause and resume; stopped services stay stopped. "
                "Other instances are not stopped automatically. Configuration and "
                "credentials are preserved. The manager reloads after success.",
            ):
                continue
            if action == "recover":
                if prefix is None:
                    raise updater.UpdateError("This is not a managed installation.")
                if not tui_forms.confirm(
                    screen,
                    "Recover interrupted update?",
                    "Use this installation's validated saved worker to restore its "
                    "previous environment and original service state, or finish cleanup "
                    "after a committed update.",
                ):
                    continue
                command = recovery_command(python, prefix)
                title = "Recovering installation"
            else:
                command = update_command(
                    python, check=action == "check", version=version, name=name
                )
                title = (
                    "Checking for updates" if action == "check" else "Installing update"
                )
            result = _progress(screen, title, command, redact=redact)
            if result.returncode == 0 and action != "check":
                tui_forms.message(
                    screen,
                    "Installation ready",
                    "The operation completed. Continue to reload the manager from the "
                    "installed version and retain the selected instance.",
                )
                raise ReloadTUI(_reload_options(panel))
            if result.returncode != 0:
                tui_forms.message(
                    screen,
                    "Operation did not complete",
                    "Review the updater's result above. If recovery is needed, choose "
                    "Recover interrupted update here. To stop a blocking instance, "
                    "return to its service menu, then open Updates again.",
                )
        except SetupCancelled:
            continue
        except (ValueError, OSError, services.ManagedServiceError) as error:
            tui_forms.message(screen, "Update unavailable", redact(str(error)))
