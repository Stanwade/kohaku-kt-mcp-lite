"""A curses control panel; systemd retains ownership when the panel exits."""

import argparse
import locale
import os
import shlex
import sys
import time
import unicodedata
from pathlib import Path

from kt_mcp.daemon import client_url, default_config_path, load_config, startup_url
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
    """Keep the relevant service action and persistence reminders compact."""
    if error or not status:
        return "Status unavailable. [l] Logs  [?] Help", ""
    load = status.get("LoadState", "")
    active = status.get("ActiveState", "")
    if load == "not-found":
        return (
            (
                "Install with --system --run-as USER; [?] Help"
                if system
                else "[i] Install/start this user service"
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
        self.config = load_config(self.config_path)
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
            self.config = load_config(self.config_path)
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
            "The home screen shows the MCP link. u opens the link and its one-line export command.",
            "Up/Down/PgUp/PgDn scroll long home-screen links. c opens client JSON.",
            "v shows details without credentials; l keeps credentials redacted in logs.",
            "q quits this panel and leaves the service unchanged. Uppercase keys also work.",
            (
                "State is the systemd process state. Startup verified / Tunnel started "
                "requires a matching startup record; it is not a continuous health check."
            ),
            "Local/named links can be copied before startup; a configured link does not mean it is running.",
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
            link_hint = "[u] Link view and one-line export"
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
                "[u] Link [l] Logs [v] Details [?] Help [q] Quit panel",
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
        if character not in "isxredglcuv?":
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
