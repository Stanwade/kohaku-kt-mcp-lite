"""A curses control panel; systemd retains ownership when the panel exits."""

import argparse
import json
import locale
import os
import shlex
import stat
import sys
import time
import unicodedata
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from kt_mcp.daemon import client_url, default_config_path, startup_url
from kt_mcp.desktop import client_configuration
from kt_mcp.instances import catalog_lock, ensure_available, read_config
from kt_mcp.managed_service import (
    ManagedServiceError,
    assert_external_process,
    inspect_service,
    restore_unit,
    stop_service,
)
from kt_mcp.onboarding import ConfigEditor, SetupCancelled, edit_config, setup_config
from kt_mcp.operations import control_service, service_setting
from kt_mcp.service import ServiceError, ServiceManager, render_unit
from kt_mcp.terminal_style import TerminalStyle
from kt_mcp.tui_update import ReloadTUI

UI_ERRORS = (ValueError, TypeError, OSError, ServiceError, ManagedServiceError)


def _clean(text: str) -> str:
    return "".join(character if character.isprintable() else " " for character in text)


def _cells(text: str) -> list[tuple[str, int]]:
    """Keep combining marks attached when clipping ordinary terminal text."""
    cells = []
    for character in _clean(text):
        if unicodedata.category(character) in {"Mn", "Me"}:
            if cells:
                previous, width = cells[-1]
                cells[-1] = (previous + character, width)
            else:
                cells.append((character, 0))
        else:
            width = 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
            cells.append((character, width))
    return cells


def display_width(text: str) -> int:
    """Terminal cells for printable text, including wide Chinese characters."""
    return sum(width for _, width in _cells(text))


def clip_display(text: str, width: int) -> str:
    """Return a prefix that fits, without splitting a wide or combining character."""
    if width <= 0:
        return ""
    parts = []
    used = 0
    for character, cells in _cells(text):
        if used + cells > width:
            break
        parts.append(character)
        used += cells
    return "".join(parts)


def ellipsize(text: str, width: int) -> str:
    """Fit a path or label using a middle ellipsis while preserving its tail."""
    if width <= 0:
        return ""
    cells = _cells(text)
    if sum(size for _, size in cells) <= width:
        return "".join(character for character, _ in cells)
    left = []
    used = 0
    for character, size in cells:
        if used + size > (width - 1) // 2:
            break
        left.append(character)
        used += size
    right = []
    remaining = width - 1 - used
    for character, size in reversed(cells[len(left) :]):
        if size > remaining:
            break
        right.append(character)
        remaining -= size
    return "".join(left) + "…" + "".join(reversed(right))


def wrap_display(text: str, width: int) -> list[str]:
    """Wrap at terminal-cell boundaries; preserve spaces and unbroken paths."""
    if width < 2:
        raise ValueError("display width must be at least 2")
    lines = []
    for source in text.split("\n"):
        parts = []
        used = 0
        for character, size in _cells(source):
            if used + size > width:
                lines.append("".join(parts))
                parts = []
                used = 0
            parts.append(character)
            used += size
        lines.append("".join(parts))
    return lines


def next_steps(
    status: dict[str, str],
    *,
    linger: str = "unknown",
    system: bool = False,
    error: str = "",
) -> tuple[str, str]:
    """Keep the relevant service action and persistence reminders compact."""
    if error or not status:
        return "Status unavailable. [l] Logs  [?] Help", ""
    load = status.get("LoadState", "")
    active = status.get("ActiveState", "")
    if load == "not-found":
        return (
            (
                "[i] Install this system service; [a] Actions"
                if system
                else "[s] Register/start this instance  [a] Actions"
            ),
            "",
        )
    if load in {"error", "bad-setting", "masked"}:
        action = "[l] Logs  [?] Help to inspect the unit"
    elif active == "failed":
        action = "[r] Restart  [l] Logs"
    elif active == "activating":
        action = "Starting... [l] Logs"
    elif active in {"deactivating", "reloading"}:
        action = "State changing... [l] Logs"
    elif active == "inactive":
        action = "[s] Start this service"
    elif active == "active":
        action = "[x] Stop  [r] Restart"
    else:
        action = "[l] Logs  [?] Help to inspect this state"
    reminders = []
    if status.get("UnitFileState") != "enabled":
        reminders.append("Boot: [e] Enable.")
    if not system and linger != "yes":
        reminders.append("Linger: [g] Enable (see ?).")
    return action, " ".join(reminders)


class TerminalController:
    def __init__(self, manager: ServiceManager, config_path: Path):
        self.manager = manager
        self.config_path = config_path.expanduser().absolute()
        self.config = read_config(self.config_path)
        self.note = ""
        self.status = {}
        self.linger = "unknown"
        self.linger_error = ""
        self.refresh_error = ""
        self.connection_url = ""
        self.connection_state = "Status unknown"
        self.connection_detail = "The service has not been checked yet."
        self.connection_verified = False
        self.connection_tone = "warning"
        self._configuration_paused = False
        self.return_to_list = False
        self._secrets = {
            secret
            for secret in (
                self.config.token,
                self.config.tunnel_token,
                os.environ.get("KT_MCP_TOKEN"),
                os.environ.get("TUNNEL_TOKEN"),
            )
            if secret
        }

    def redact(self, text: str) -> str:
        self._remember(self.config)
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def _remember(self, config):
        self._secrets.update(
            secret for secret in (config.token, config.tunnel_token) if secret
        )

    def _edit_binding(self):
        return inspect_service(
            self.manager.name,
            self.manager.system,
            expected_python=sys.executable,
            expected_config=self.config_path,
        )

    def _check_manual_daemon(self):
        """A missing unit does not prove a manually launched daemon has stopped."""
        path = self.config_path.with_suffix(".status.json")
        try:
            descriptor = os.open(
                path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            )
        except FileNotFoundError:
            return
        except OSError:
            raise ManagedServiceError(
                "Cannot verify the saved daemon startup record; check it before editing."
            ) from None
        try:
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != self.config_path.stat().st_uid
                ):
                    raise ValueError("unsafe startup record")
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("startup record is too large")
            status = json.loads(raw)
            pid = status.get("pid") if isinstance(status, dict) else None
            if type(pid) is not int or pid <= 0:
                raise ValueError("invalid startup PID")
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        except (OSError, ValueError, OverflowError):
            raise ManagedServiceError(
                "Cannot confirm the manual daemon is stopped; check its startup record first."
            ) from None
        raise ManagedServiceError(
            "A manual daemon recorded for this configuration is still running; "
            "stop it outside this panel before editing. No process was stopped."
        )

    def configure(
        self,
        *,
        ask: Callable[[str], str] | None = None,
        ask_secret: Callable[[str], str] | None = None,
    ):
        """Stop after confirmation; keep the service stopped through review and save."""
        self.note = ""
        ask = input if ask is None else ask
        if self.manager.system and os.geteuid() != 0:
            raise PermissionError(
                "System configuration changes require root; no automatic sudo."
            )
        editor = ConfigEditor(self.config_path)
        self._remember(editor.config)
        try:
            binding = self._edit_binding()
            if binding is not None:
                assert_external_process(binding)
            else:
                self._check_manual_daemon()
        except UI_ERRORS as exc:
            self.note = self.redact(
                f"Configuration not opened: {exc}. "
                "Check systemd access and the selected unit before retrying."
            )
            raise

        style = TerminalStyle()
        print(
            style.text("Configure MCP · " + self.manager.name, bold=True, color="cyan")
        )
        if binding is None:
            print("No service is registered. Edit the saved settings only.")
            prompt = "Edit settings?"
        else:
            print("MCP will stop and stay offline while you edit.")
            print(f"Only instance {self.manager.name} will be paused.")
            print("After saving or cancelling, press [s] Start.")
            prompt = "Stop MCP and configure?"
        while True:
            try:
                answer = ask(style.prompt(prompt, "y/N")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer in {"", "n", "no", ":q"}:
                self.note = "Configuration cancelled; service unchanged."
                return
            if answer in {"y", "yes"}:
                break
            print("Enter y to continue, or n to cancel.")

        stopped = False
        unit_attempted = False
        candidate = None
        publication = ExitStack()

        def require_stopped(current):
            nonlocal stopped
            if (
                current.status.get("ActiveState") not in {"inactive", "failed"}
                or current.status.get("MainPID") != "0"
            ):
                stopped = False
                stop_service(current)
                stopped = True
                raise ManagedServiceError(
                    "MCP was started during editing and has been stopped; retry configuring."
                )

        def before_save(config):
            nonlocal stopped
            self._remember(config)
            if not self.manager.system:
                publication.enter_context(catalog_lock())
                ensure_available(config, exclude=self.config_path)
            current = self._edit_binding()
            if binding is None:
                if current is not None:
                    raise ManagedServiceError(
                        "A service was registered during editing; reopen Configure to stop it."
                    )
                self._check_manual_daemon()
                return
            if current is None or (
                current.unit_bytes != binding.unit_bytes
                or current.owner_uid != binding.owner_uid
                or current.owner_gid != binding.owner_gid
                or current.unit_mode != binding.unit_mode
                or current.unit_owner_uid != binding.unit_owner_uid
                or current.unit_owner_gid != binding.unit_owner_gid
            ):
                stopped = False
                raise ManagedServiceError(
                    "The service changed during editing; configuration was not saved."
                )
            require_stopped(current)

        try:
            if binding is not None:
                print("Stopping MCP...")
                stop_service(binding)
                stopped = True
                self._configuration_paused = True
                print(style.text("MCP stopped.", bold=True, color="yellow"))
            edited = edit_config(
                self.config_path,
                ask=ask,
                ask_secret=ask_secret,
                editor=editor,
                before_save=before_save,
            )
            self._remember(edited)
            if binding is not None:
                candidate = render_unit(
                    self.config_path,
                    system=self.manager.system,
                    run_as=binding.run_as,
                ).encode("utf-8")
                unit_attempted = True
                self.manager.install(
                    self.config_path,
                    replace=True,
                    run_as=binding.run_as,
                    now=False,
                    enable=False,
                    linger=False,
                )
                current = self._edit_binding()
                if current is None or current.unit_bytes != candidate:
                    stopped = False
                    raise ManagedServiceError(
                        "The updated service could not be confirmed."
                    )
                require_stopped(current)
            self.config = edited
            self.note = (
                "Saved; MCP is stopped. [s] Start when ready."
                if binding is not None
                else (
                    "Saved; register the system unit before starting."
                    if self.manager.system
                    else "Saved. [i] Install/start when ready."
                )
            )
        except BaseException as exc:
            recovery = []
            if editor.saved:
                if unit_attempted:
                    try:
                        restore_unit(binding, expected_unit_bytes=candidate)
                    except UI_ERRORS as recovery_error:
                        recovery.append(str(recovery_error))
                try:
                    editor.restore()
                except UI_ERRORS as recovery_error:
                    recovery.append(str(recovery_error))
            state = (
                "MCP remains stopped. [s] Start when ready."
                if stopped
                else "No service was started; check its state before continuing."
            )
            cancelled = isinstance(exc, (SetupCancelled, KeyboardInterrupt, EOFError))
            self.note = self.redact(
                (
                    "Changes cancelled. "
                    if cancelled
                    else f"Configuration failed: {exc}. "
                )
                + state
                + (" Recovery incomplete: " + "; ".join(recovery) if recovery else "")
            )
            if not cancelled:
                raise
        finally:
            publication.close()

    def refresh(self):
        """Refresh process and startup evidence; logs are queried only on request."""
        self.status = {}
        self.linger = "unknown"
        self.linger_error = ""
        self.refresh_error = ""
        self.connection_url = ""
        self.connection_verified = False
        self.connection_state = "Status unknown"
        self.connection_detail = "Service state could not be checked."
        self.connection_tone = "warning"
        try:
            self.config = read_config(self.config_path)
            self.redact("")  # Remember this generation's secrets before another reload.
            if self.config.settings.mode != "quick":
                self.connection_url = client_url(self.config, self.config_path)
        except UI_ERRORS as exc:
            self.refresh_error = self.redact(str(exc))
            self.connection_state = "Configuration unavailable"
            self.connection_detail = "Fix the saved configuration and retry."
            self.connection_tone = "error"
            return
        try:
            self.status = self.manager.status()
        except UI_ERRORS as exc:
            self.refresh_error = self.redact(str(exc))
            return
        try:
            self.linger = self.manager.linger_status()
        except UI_ERRORS as exc:
            # Persistence diagnostics do not invalidate a current startup record.
            self.linger_error = self.redact(str(exc))
        self._update_connection()

    def _update_connection(self):
        mode = self.config.settings.mode
        active = self.status.get("ActiveState", "")
        load = self.status.get("LoadState", "")
        prefix = "Configured" if self.connection_url else "URL unavailable"
        if load in {"error", "bad-setting", "masked"} or active == "failed":
            self.connection_state = prefix + " - service failed"
            self.connection_detail = "Inspect [l] Logs before retrying the service."
            self.connection_tone = "error"
            return
        if load == "not-found" or active == "inactive":
            self.connection_state = prefix + " - not running"
            self.connection_detail = (
                "Start the service to use this configured address."
                if self.connection_url
                else "Start the service to obtain a new quick tunnel URL."
            )
            self.connection_tone = "muted"
            return
        if not active:
            return
        self.connection_state = (
            "Waiting for tunnel URL"
            if mode == "quick"
            else "Configured - startup unverified"
        )
        self.connection_detail = "Waiting for a current startup record; [l] Logs."
        if active != "active":
            return
        try:
            pid = int(self.status.get("MainPID") or "0")
            if pid <= 0:
                return
            url = startup_url(self.config, self.config_path, expected_pid=pid)
        except UI_ERRORS:
            # Missing, stale or mismatched records must never retain an old URL.
            return
        self.connection_url = url
        self.connection_verified = True
        self.connection_tone = "ok"
        self.connection_state = (
            "Startup verified" if mode == "local" else "Tunnel started"
        )
        self.connection_detail = (
            "MCP startup check passed. This address is local to the service host."
            if mode == "local"
            else "MCP startup and initial tunnel connection passed; public reachability is untested."
        )

    def link_text(self) -> str:
        """Expose the authorized MCP link, with an exact plain-text export command."""
        return "\n".join(
            [
                self.connection_url or self.connection_state,
                "",
                "Copy as one line:",
                self._command("link", "--config", str(self.config_path)),
                "",
                "The command prints the full URL without inserted line breaks.",
                "State: " + self.connection_state,
            ]
        )

    def _command(self, *arguments: str) -> str:
        return shlex.join([os.path.abspath(sys.executable), "-m", "kt_mcp", *arguments])

    def help_text(self) -> str:
        lines = [
            "Global navigation:",
            "n creates a new instance; b opens the instance overview.",
            "a opens all management actions without leaving this interface.",
            "q returns to the overview from managed details, or closes an explicit panel.",
            "",
            "This instance:",
            "s registers if needed and starts; x stops; r restarts.",
            "Start does not enable automatic startup or user linger.",
            "o configures workspace, port and connection settings after a pause warning.",
            "After saving or cancelling configuration, use s to start again.",
            "u views or exports the MCP link; c views or exports client JSON.",
            "l opens redacted logs; v shows service details.",
            "e enables automatic startup; d disables it without stopping the service.",
            "Use a for installation options, uninstall, linger, exports, updates and recovery.",
            "",
            "Up/Down/PgUp/PgDn scroll long links and viewers.",
            "Uppercase shortcut keys work too.",
            "A configured link does not mean the service is running.",
            "Startup evidence does not continuously test public reachability.",
        ]
        if self.manager.system:
            lines[1] = (
                "This panel manages a system service; user instances use a user session."
            )
            lines.append(
                "Install in a to select an ordinary service account and startup options."
            )
        return self.redact("\n".join(lines))

    def details_text(self) -> str:
        settings = self.config.settings
        fields = [
            ("Workspace", settings.workspace),
            ("Configuration file", str(self.config_path)),
            ("Unit file", str(self.manager.unit_path)),
            ("Loaded unit file", self.status.get("FragmentPath") or "not reported"),
            ("Unit", self.manager.unit_name),
            ("Scope", "system" if self.manager.system else "user"),
            ("Load state", self.status.get("LoadState") or "unknown"),
            ("State", self.status.get("ActiveState") or "unknown"),
            ("Substate", self.status.get("SubState") or "unknown"),
            ("PID", self.status.get("MainPID") or "unknown"),
            ("Restarts", self.status.get("NRestarts") or "unknown"),
            ("Last exit status", self.status.get("ExecMainStatus") or "unknown"),
            ("Boot", self.status.get("UnitFileState") or "unknown"),
            ("Linger", self.linger),
            ("Connection mode", settings.mode),
            ("Connection state", self.connection_state),
            ("Startup evidence", self.connection_detail),
            ("Local HTTP port", str(settings.port)),
        ]
        if settings.public_origin:
            fields.append(("Public HTTPS origin", settings.public_origin))
        if self.refresh_error:
            fields.append(("Status unavailable", self.refresh_error))
        if self.linger_error:
            fields.append(("Linger unavailable", self.linger_error))
        lines = [f"{label}: {value}" for label, value in fields]
        lines.extend(
            ["", "Startup evidence does not confirm ongoing or public reachability."]
        )
        return self.redact("\n".join(lines))

    def _check_start_available(self):
        """Check this saved instance before install/start, including post-edit starts."""
        if self.manager.system:
            # Explicit system units are outside the current user's instance catalog.
            return
        config = read_config(self.config_path)
        status = self.manager.status()
        ensure_available(
            config,
            exclude=self.config_path,
            check_socket=status.get("ActiveState")
            not in {"active", "activating", "deactivating", "reloading"},
        )

    def act(self, key: str):
        """Explicit management actions only; quitting never stops the daemon."""
        key = key.lower()
        if key == "?":
            return "Panel help", self.help_text()
        if key == "v":
            return "Service details", self.details_text()
        if key == "i":
            if self.manager.system:
                raise ValueError(
                    "Use Actions > Install to choose the system service account."
                )
            self._check_start_available()
            self.manager.install(self.config_path, now=True, linger=True)
            self._configuration_paused = False
            self.note = "Service registered and enabled; check MCP startup in the logs."
        elif key in {"s", "x", "r"}:
            action = {"s": "start", "x": "stop", "r": "restart"}[key]
            status = control_service(self.manager, self.config_path, action)
            if isinstance(status, dict):
                self.status = status
            if key in {"s", "r"}:
                self._configuration_paused = False
                self.note = "MCP started; local connection verified."
            else:
                self.note = "MCP stopped."
        elif key in {"e", "d"}:
            action = "enable" if key == "e" else "disable"
            status = service_setting(self.manager, self.config_path, action)
            if isinstance(status, dict):
                self.status = status
            self.note = (
                "Boot disabled; a running service is left unchanged."
                if key == "d"
                else "Automatic startup enabled."
            )
        elif key == "g":
            self.manager.enable_linger()
            self.note = "Linger enabled: the user service can survive logout."
        elif key == "l":
            return "Service logs", self.redact(self.manager.logs(200))
        elif key in {"u", "c"}:
            self.refresh()
            if key == "u":
                return "MCP link", self.link_text()
            if not self.connection_url:
                raise ValueError(
                    "No current MCP link; start the service and check logs."
                )
            return (
                "Client configuration - contains credentials; share only with trusted clients",
                client_configuration(self.connection_url),
            )
        return None


def _styles() -> dict[str, int]:
    """Use terminal colors when available, retaining emphasis in monochrome."""
    import curses

    plain = {
        "normal": 0,
        "muted": curses.A_DIM,
        "title": curses.A_BOLD,
        "link": curses.A_BOLD,
        "ok": curses.A_BOLD,
        "warning": curses.A_BOLD,
        "error": curses.A_BOLD,
    }
    try:
        if not curses.has_colors():
            return plain
        curses.start_color()
        curses.use_default_colors()
        # wrapper() starts color support even for NO_COLOR. Reset pair zero
        # before opting out so the terminal retains its own foreground/background.
        if "NO_COLOR" in os.environ:
            return plain
        colors = (
            ("title", curses.COLOR_CYAN),
            ("link", curses.COLOR_CYAN),
            ("ok", curses.COLOR_GREEN),
            ("warning", curses.COLOR_YELLOW),
            ("error", curses.COLOR_RED),
        )
        styled = dict(plain)
        for pair, (role, foreground) in enumerate(colors, 1):
            curses.init_pair(pair, foreground, -1)
            styled[role] |= curses.color_pair(pair)
        return styled
    except curses.error:
        return plain


def _put(screen, row, column, text, attribute=0):
    import curses

    height, width = screen.getmaxyx()
    if 0 <= row < height and 0 <= column < width - 1:
        clipped = clip_display(str(text), width - column - 1)
        try:
            # addnstr counts characters; the text has already been clipped by cells.
            screen.addnstr(row, column, clipped, len(clipped), attribute)
        except curses.error:
            # Some terminals report an error after drawing their bottom-right cell.
            pass


def _popup(screen, title: str, body: str):
    import curses

    styles = _styles()
    offset = 0
    screen.timeout(-1)
    try:
        while True:
            height, width = screen.getmaxyx()
            page = max(1, height - 4)
            lines = wrap_display(body, width - 4) if width >= 6 else []
            offset = max(0, min(offset, max(0, len(lines) - page)))
            screen.erase()
            _put(screen, 0, 1, title, styles["title"])
            if height < 4 or width < 6:
                _put(screen, 1, 0, "Resize terminal; q returns.")
            else:
                for row, line in enumerate(lines[offset : offset + page], 2):
                    _put(screen, row, 2, line)
            _put(screen, height - 1, 1, "Up/Down/PgUp/PgDn: scroll | Enter/Esc/q: back")
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), ord("Q"), 27, 10, 13):
                return
            if key in (curses.KEY_DOWN, ord("j"), ord("J")):
                offset += 1
            elif key in (curses.KEY_UP, ord("k"), ord("K")):
                offset -= 1
            elif key == curses.KEY_NPAGE:
                offset += page
            elif key == curses.KEY_PPAGE:
                offset -= page
    finally:
        screen.timeout(250)


def _configure(screen, controller: TerminalController):
    """Keep the shared pause/edit/save workflow inside the native interface."""
    from kt_mcp.tui_forms import message, run_wizard

    try:
        run_wizard(
            screen,
            "Configure " + controller.manager.name,
            controller.configure,
        )
    except SetupCancelled:
        if not controller.note:
            controller.note = "Configuration cancelled."
    except UI_ERRORS as exc:
        if not controller.note:
            controller.note = controller.redact(f"Configuration not opened: {exc}")
    if controller.note:
        message(screen, "Configuration", controller.note)


def _screen(screen, controller: TerminalController):
    import curses

    styles = _styles()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(250)
    next_refresh = 0.0
    link_offset = 0
    previous_link = None
    while True:
        if time.monotonic() >= next_refresh:
            controller.refresh()
            next_refresh = time.monotonic() + 2
        height, width = screen.getmaxyx()
        screen.erase()
        _put(screen, 0, 1, "Kohaku KT MCP", styles["title"])
        if height < 18 or width < 64:
            _put(screen, 2, 1, "Resize terminal to at least 64 x 18. Press q to quit.")
        else:
            available = width - 4
            config = controller.config.settings
            status = controller.status
            scope = "System" if controller.manager.system else "User"
            suffix = f"  Scope: {scope}"
            unit = ellipsize(
                controller.redact(controller.manager.unit_name),
                available - display_width("Unit: " + suffix),
            )
            _put(screen, 1, 2, f"Unit: {unit}{suffix}", styles["muted"])
            state = status.get("ActiveState") or "unknown"
            if status.get("SubState"):
                state += "/" + status["SubState"]
            suffix = (
                f"  PID: {status.get('MainPID') or '?'}"
                f" Restarts: {status.get('NRestarts') or '?'}"
            )
            state = ellipsize(state, available - display_width("State: " + suffix))
            _put(
                screen,
                2,
                2,
                controller.redact(f"State: {state}{suffix}"),
                styles[controller.connection_tone] | curses.A_BOLD,
            )
            boot = status.get("UnitFileState") or "unknown"
            _put(
                screen,
                3,
                2,
                controller.redact(f"Boot: {boot}   Linger: {controller.linger}"),
            )
            _put(
                screen,
                4,
                2,
                "Workspace: "
                + ellipsize(controller.redact(config.workspace), available - 11),
            )
            _put(
                screen,
                5,
                2,
                "MCP link | " + controller.connection_state,
                styles[controller.connection_tone] | curses.A_BOLD,
            )
            # This is the intentionally visible credential area. Never ellipsize
            # a URL or pass it through general-purpose diagnostic redaction.
            link = controller.connection_url
            if link != previous_link:
                link_offset = 0
                previous_link = link
            link_lines = wrap_display(link or "No current link.", available)
            link_page = height - 11
            link_offset = max(0, min(link_offset, max(0, len(link_lines) - link_page)))
            for row, line in enumerate(
                link_lines[link_offset : link_offset + link_page], 6
            ):
                _put(screen, row, 2, line, styles["link" if link else "muted"])
            link_hint = "[o] Configure [u] Link [l] Logs [?] Help"
            if len(link_lines) > link_page:
                link_hint = (
                    f"Lines {link_offset + 1}-{link_offset + link_page}/{len(link_lines)}"
                    " | Up/Down/PgUp/PgDn scroll | [u] Export"
                )
            _put(screen, height - 5, 2, link_hint, styles["muted"])
            action, persistence = next_steps(
                status,
                linger=controller.linger,
                system=controller.manager.system,
                error=controller.refresh_error,
            )
            _put(screen, height - 4, 2, action, curses.A_BOLD)
            _put(
                screen,
                height - 3,
                2,
                persistence
                or (
                    "Local access only."
                    if config.mode == "local"
                    else "Public reachability is not checked."
                ),
                styles["muted"],
            )
            _put(
                screen,
                height - 2,
                2,
                (
                    "[a] Actions [q] Quit panel"
                    if controller.manager.system
                    else "[n] New [a] Actions [b] Instances [q] "
                    + ("Back" if controller.return_to_list else "Quit")
                ),
            )
            _put(
                screen,
                height - 1,
                1,
                controller.redact(
                    controller.refresh_error
                    or controller.note
                    or controller.linger_error
                ),
                styles["warning"],
            )
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        if height < 18 or width < 64:
            continue
        if key in (curses.KEY_DOWN, curses.KEY_UP, curses.KEY_NPAGE, curses.KEY_PPAGE):
            movement = {
                curses.KEY_DOWN: 1,
                curses.KEY_UP: -1,
                curses.KEY_NPAGE: link_page,
                curses.KEY_PPAGE: -link_page,
            }
            link_offset += movement[key]
            continue
        if key < 0 or key > 255:
            continue
        character = chr(key).lower()
        if character not in "naboisxredglcuv?":
            continue
        previous_note = controller.note
        if character not in "nabv?oulcig":
            controller.note = "Working..."
            _put(screen, height - 1, 1, controller.note)
            screen.refresh()
        try:
            from kt_mcp import tui_actions
            from kt_mcp.tui_forms import message

            popup = None
            if character in {"n", "b"}:
                if controller.manager.system:
                    message(
                        screen,
                        "System service",
                        "This panel manages system services. User instances are managed in a user session.",
                    )
                else:
                    return "new" if character == "n" else "instances"
            elif character == "a":
                destination = tui_actions.actions_menu(screen, controller)
                if destination is not None:
                    return destination
            elif character == "o":
                _configure(screen, controller)
            elif character == "i":
                tui_actions.install_dialog(screen, controller)
            elif character == "g":
                tui_actions._enable_linger(screen, controller)
            elif character == "l":
                tui_actions.show_logs(screen, controller)
            elif character in {"u", "c"}:
                tui_actions.show_export(
                    screen,
                    controller,
                    kind="link" if character == "u" else "client-config",
                )
            else:
                popup = controller.act(character)
            if popup:
                _popup(screen, *popup)
                controller.note = previous_note
        except SetupCancelled:
            controller.note = "Operation cancelled."
        except UI_ERRORS as exc:
            controller.note = controller.redact(str(exc))
            _popup(screen, "Action could not be completed", controller.note)
        next_refresh = 0.0


def _first_configuration(screen, path, name, system):
    """Create an explicit configuration inside curses before opening its panel."""
    from kt_mcp import tui_actions
    from kt_mcp.instance_tui import single_screen
    from kt_mcp.tui_forms import run_wizard

    if system:
        if not tui_actions.prepare_system_config(screen, path):
            return
    else:
        try:
            run_wizard(
                screen,
                "New configuration",
                lambda **readers: setup_config(path, **readers),
            )
        except SetupCancelled:
            return
    single_screen(screen, TerminalController(ServiceManager(name, system), path))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kt-mcp tui", description=__doc__)
    parser.add_argument("--config", type=Path, help="open one explicit configuration")
    parser.add_argument("--name", help="service name for --config or --system")
    parser.add_argument("--system", action="store_true")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--instance", help="open a saved instance in the manager")
    selection.add_argument(
        "--workspace", type=Path, help="select or set up this workspace"
    )
    args = parser.parse_args(argv)
    if (args.instance is not None or args.workspace is not None) and (
        args.config is not None or args.name is not None or args.system
    ):
        parser.error(
            "--instance/--workspace cannot be combined with --config/--name/--system"
        )
    if sys.platform != "linux":
        parser.error(
            "the terminal manager requires Linux; use the desktop GUI on Windows"
        )
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error(
            "tui requires an interactive terminal; for SSH, allocate a terminal with -t"
        )
    if os.geteuid() == 0 and not args.system:
        parser.error(
            "run the user TUI without sudo; --system manages an existing system unit"
        )
    try:
        import curses

        from kt_mcp.instance_tui import (
            InstanceManagerController,
            instance_screen,
            single_screen,
        )

        locale.setlocale(locale.LC_ALL, "")
        if args.config is not None or args.name is not None or args.system:
            path = (args.config or default_config_path()).expanduser().absolute()
            name = args.name or "kt-mcp"
            if path.exists():
                controller = TerminalController(ServiceManager(name, args.system), path)
                curses.wrapper(single_screen, controller)
            else:
                curses.wrapper(_first_configuration, path, name, args.system)
        else:
            from kt_mcp import instances

            selected = None
            missing_workspace = None
            if args.instance is not None:
                selected = instances.get_instance(args.instance)
            elif args.workspace is not None:
                selected = instances.find_workspace(args.workspace)
                if selected is None:
                    missing_workspace = args.workspace
            manager = InstanceManagerController(
                selected_name=selected.name if selected is not None else None
            )
            curses.wrapper(
                instance_screen,
                manager,
                args.instance is not None,
                None,
                missing_workspace,
            )
    except ReloadTUI as reload:
        try:
            os.execv(
                sys.executable,
                [sys.executable, "-I", "-m", "kt_mcp", "tui", *reload.argv],
            )
        except OSError as exc:
            parser.error(f"Updated program could not reopen its interface: {exc}")
    except ImportError:
        parser.error("this Python installation needs curses support")
    except curses.error:
        parser.error(
            "terminal initialization failed; check TERM and use an interactive terminal"
        )
    except SetupCancelled:
        return 0
    except (KeyboardInterrupt, EOFError):
        return 0
    except (locale.Error, *UI_ERRORS) as exc:
        parser.error(str(exc))
    return 0
