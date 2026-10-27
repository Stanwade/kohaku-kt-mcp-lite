"""A curses control panel; systemd retains ownership when the panel exits."""

import argparse
import locale
import os
import shlex
import sys
import time
import unicodedata
from pathlib import Path

from kt_mcp.daemon import client_url, default_config_path, load_config
from kt_mcp.desktop import client_configuration
from kt_mcp.onboarding import SetupCancelled, setup_config
from kt_mcp.service import ServiceError, ServiceManager

UI_ERRORS = (ValueError, TypeError, OSError, ServiceError)


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
    """Two short guidance lines based on systemd process and persistence state."""
    if error or not status:
        return (
            "Next: [l] Logs or [?] Help; status is unavailable.",
            "Status will be checked again automatically.",
        )
    load = status.get("LoadState", "")
    active = status.get("ActiveState", "")
    if load == "not-found":
        return (
            (
                "Next: [?] Help to install a system unit with --run-as."
                if system
                else "Next: [i] Register/start this user service."
            ),
            "The saved configuration will be used for installation.",
        )
    if load in {"error", "bad-setting", "masked"}:
        action = "Next: [l] Logs and [?] Help to inspect the unit."
    elif active == "failed":
        action = "Next: [l] Logs to diagnose; [r] Restart to try again."
    elif active == "activating":
        action = "Next: [l] Logs to follow startup."
    elif active in {"deactivating", "reloading"}:
        action = "Next: [l] Logs to follow the state change."
    elif active == "inactive":
        action = "Next: [s] Start this registered service."
    elif active == "active":
        action = "Next: [c] Client JSON; check MCP startup in [l] Logs."
    else:
        action = "Next: [l] Logs or [?] Help to inspect this state."
    reminders = []
    if status.get("UnitFileState") != "enabled":
        reminders.append("Boot: [e] Enable.")
    if not system and linger != "yes":
        reminders.append("Linger: [g] Enable (see ?).")
    return action, " ".join(reminders) or "Panel exit leaves the service unchanged."


class TerminalController:
    def __init__(self, manager: ServiceManager, config_path: Path):
        self.manager = manager
        self.config_path = config_path.expanduser().absolute()
        self.config = load_config(self.config_path)
        self.note = ""
        self.status = {}
        self.linger = "unknown"
        self.logs = ""
        self.refresh_error = ""
        self._secrets = {
            secret for secret in (self.config.token, self.config.tunnel_token) if secret
        }

    def redact(self, text: str) -> str:
        self._secrets.update(
            secret for secret in (self.config.token, self.config.tunnel_token) if secret
        )
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def refresh(self):
        """Publish a fresh snapshot, or discard old state after any polling error."""
        try:
            self.config = load_config(self.config_path)
            status = self.manager.status()
            linger = self.manager.linger_status()
            logs = self.manager.logs(8)
        except UI_ERRORS as exc:
            self.status = {}
            self.linger = "unknown"
            self.logs = ""
            self.refresh_error = self.redact(str(exc))
            return
        self.status = status
        self.linger = linger
        self.logs = self.redact(logs)
        self.refresh_error = ""

    def _command(self, *arguments: str) -> str:
        return shlex.join([os.path.abspath(sys.executable), "-m", "kt_mcp", *arguments])

    def help_text(self) -> str:
        common = ["--name", self.manager.name]
        if self.manager.system:
            common.append("--system")
        lines = [
            "First use:",
            (
                "System units require an explicit ordinary --run-as user. "
                "Install with the command below; i is unavailable in this scope."
                if self.manager.system
                else "i registers the saved configuration, enables boot and user linger, "
                "and starts the service."
            ),
            "",
            "s starts; x stops; r restarts. l opens logs.",
            (
                "e enables boot. d only disables future automatic startup; "
                "it does not stop a running service."
            ),
            "c explicitly displays client credentials. v shows details without credentials.",
            "q quits this panel and leaves the service unchanged. Uppercase keys also work.",
            "State is reported by systemd. Use l to check MCP startup.",
            (
                "Existing configurations keep their saved workspace; "
                "changing your shell directory does not reconfigure the service."
            ),
        ]
        if not self.manager.system:
            import pwd

            account = pwd.getpwuid(os.geteuid()).pw_name
            lines.extend(
                [
                    "",
                    "g enables user linger so the user service can continue after logout.",
                    "If enabling linger is denied, run in another terminal:",
                    shlex.join(["sudo", "loginctl", "enable-linger", account]),
                    "Then retry i for the first install/start, or g for linger only.",
                ]
            )
        lines.extend(
            [
                "",
                "Reopen this panel with the same configuration and unit:",
                self._command("tui", "--config", str(self.config_path), *common),
                "",
                "Inspect this unit:",
                self._command("service", "status", *common),
                "",
                (
                    "Install as root; replace RUN_USER with an ordinary account:"
                    if self.manager.system
                    else "Install/start this saved configuration from a terminal:"
                ),
                self._command(
                    "service",
                    "install",
                    "--config",
                    str(self.config_path),
                    *common,
                    "--now",
                    *(
                        ["--run-as", "RUN_USER"]
                        if self.manager.system
                        else ["--linger"]
                    ),
                ),
            ]
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
            ("Local HTTP port", str(settings.port)),
        ]
        if settings.public_origin:
            fields.append(("Public HTTPS origin", settings.public_origin))
        if self.refresh_error:
            fields.append(("Status unavailable", self.refresh_error))
        lines = [f"{label}: {value}" for label, value in fields]
        lines.extend(
            ["", "State describes the systemd process; check logs for MCP startup."]
        )
        return self.redact("\n".join(lines))

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
                    "Install a system unit first with service install --system --run-as USER."
                )
            self.manager.install(self.config_path, now=True, linger=True)
            self.note = "Service registered and enabled; check MCP startup in the logs."
        elif key in {"s", "x", "r", "e", "d"}:
            action = {
                "s": "start",
                "x": "stop",
                "r": "restart",
                "e": "enable",
                "d": "disable",
            }[key]
            self.manager.control(action)
            self.note = (
                "Boot disabled; a running service is left unchanged."
                if key == "d"
                else f"{action}: request completed."
            )
        elif key == "g":
            self.manager.enable_linger()
            self.note = "Linger enabled: the user service can survive logout."
        elif key == "l":
            return "Service logs", self.redact(self.manager.logs(200))
        elif key == "c":
            self.config = load_config(self.config_path)
            return (
                "Client configuration - contains credentials; share only with trusted clients",
                client_configuration(client_url(self.config, self.config_path)),
            )
        return None


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

    offset = 0
    screen.timeout(-1)
    try:
        while True:
            height, width = screen.getmaxyx()
            page = max(1, height - 4)
            lines = wrap_display(body, width - 4) if width >= 6 else []
            offset = max(0, min(offset, max(0, len(lines) - page)))
            screen.erase()
            _put(screen, 0, 1, title, curses.A_BOLD)
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


def _screen(screen, controller: TerminalController):
    import curses

    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(250)
    next_refresh = 0.0
    while True:
        if time.monotonic() >= next_refresh:
            controller.refresh()
            next_refresh = time.monotonic() + 2
        height, width = screen.getmaxyx()
        screen.erase()
        _put(screen, 0, 1, "Kohaku KT MCP | Linux service manager", curses.A_BOLD)
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
            _put(screen, 1, 2, f"Unit: {unit}{suffix}")
            state = status.get("ActiveState") or "unknown"
            if status.get("SubState"):
                state += "/" + status["SubState"]
            suffix = (
                f"  PID: {status.get('MainPID') or '?'}"
                f" Restarts: {status.get('NRestarts') or '?'}"
            )
            state = ellipsize(state, available - display_width("State: " + suffix))
            _put(screen, 2, 2, controller.redact(f"State: {state}{suffix}"))
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
                "Config: "
                + ellipsize(
                    controller.redact(str(controller.config_path)), available - 8
                ),
            )
            _put(screen, 6, 2, f"Mode: {config.mode}   Local port: {config.port}")
            _put(
                screen,
                7,
                2,
                "State tracks systemd; use [l] Logs to verify MCP startup.",
            )
            action, persistence = next_steps(
                status,
                linger=controller.linger,
                system=controller.manager.system,
                error=controller.refresh_error,
            )
            _put(screen, 8, 2, action, curses.A_BOLD)
            _put(screen, 9, 2, persistence)
            service_actions = "[s] Start  [x] Stop  [r] Restart"
            if not controller.manager.system:
                service_actions = "[i] Install/start  " + service_actions
            _put(screen, 10, 2, service_actions)
            boot_actions = "[e] Enable boot   [d] Disable boot"
            if not controller.manager.system:
                boot_actions += "   [g] Enable linger"
            _put(screen, 11, 2, boot_actions)
            _put(screen, 12, 2, "[l] Logs  [c] Client JSON  [v] Details  [?] Help")
            _put(screen, 13, 2, "[q] Quit panel (service keeps its current state)")
            _put(screen, 14, 2, "Recent service log", curses.A_BOLD)
            for row, line in enumerate(
                controller.logs.splitlines()[-(height - 16) :], 15
            ):
                _put(screen, row, 2, line)
            _put(
                screen,
                height - 1,
                1,
                controller.redact(controller.refresh_error or controller.note)
                or "Press ? for help; v shows full paths.",
            )
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        if height < 18 or width < 64:
            continue
        if key < 0 or key > 255:
            continue
        character = chr(key).lower()
        if character not in "isxredglcv?":
            continue
        previous_note = controller.note
        if character not in "v?":
            controller.note = "Working..."
            _put(screen, height - 1, 1, controller.note)
            screen.refresh()
        try:
            popup = controller.act(character)
            if popup:
                _popup(screen, *popup)
                controller.note = previous_note
        except UI_ERRORS as exc:
            controller.note = controller.redact(str(exc))
            _popup(screen, "Action could not be completed", controller.note)
        next_refresh = 0.0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kt-mcp tui", description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--name", default="kt-mcp")
    parser.add_argument("--system", action="store_true")
    args = parser.parse_args(argv)
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

        locale.setlocale(locale.LC_ALL, "")
        path = args.config.expanduser().absolute()
        if not path.exists():
            if args.system:
                raise ValueError(
                    "create the configuration as the run user and install the system unit first"
                )
            setup_config(path)
        controller = TerminalController(ServiceManager(args.name, args.system), path)
        curses.wrapper(_screen, controller)
    except ImportError:
        parser.error("this Python installation needs curses support")
    except curses.error:
        parser.error(
            "terminal initialization failed; check TERM and use an interactive terminal"
        )
    except SetupCancelled as exc:
        print(str(exc))
        return 0
    except (KeyboardInterrupt, EOFError):
        return 0
    except (locale.Error, *UI_ERRORS) as exc:
        parser.error(str(exc))
    return 0
