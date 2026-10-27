"""A curses control panel; systemd retains ownership when the panel exits."""

import argparse
import getpass
import locale
import os
import secrets
import sys
import textwrap
import time
from pathlib import Path

from kt_mcp.daemon import (
    DaemonConfig,
    client_url,
    default_config_path,
    load_config,
    save_config,
)
from kt_mcp.desktop import DesktopSettings, client_configuration
from kt_mcp.service import ServiceError, ServiceManager

UI_ERRORS = (ValueError, TypeError, OSError, ServiceError)


def setup_config(path: Path, *, ask=input, ask_secret=getpass.getpass):
    """First-run setup, before curses starts; secrets are never terminal-echoed."""
    if path.expanduser().exists():
        raise FileExistsError(
            "configuration already exists; it will not be overwritten"
        )
    print("Kohaku KT MCP - first-time Linux setup")
    workspace = ask(f"Workspace [{Path.cwd()}]: ").strip() or str(Path.cwd())
    port = int(ask("Local HTTP port [8765]: ").strip() or "8765")
    mode = ask("Connection mode: local / quick / named [local]: ").strip() or "local"
    origin = ""
    tunnel_token = ""
    if mode == "named":
        origin = ask("Public HTTPS origin (without /mcp): ").strip()
        tunnel_token = (
            os.environ.get("TUNNEL_TOKEN")
            or ask_secret("Cloudflare Tunnel token (hidden): ").strip()
        )
    config = DaemonConfig(
        DesktopSettings(
            str(Path(workspace).expanduser().resolve()), port, mode, origin
        ),
        os.environ.get("KT_MCP_TOKEN") or secrets.token_urlsafe(32),
        tunnel_token,
    )
    save_config(config, path)
    print(f"Private configuration created: {path}")
    return config


class TerminalController:
    def __init__(self, manager: ServiceManager, config_path: Path):
        self.manager = manager
        self.config_path = config_path.expanduser().absolute()
        self.config = load_config(self.config_path)
        self.note = "Press i to register/start, or choose an action below."

    def redact(self, text: str) -> str:
        for secret in (self.config.token, self.config.tunnel_token):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def act(self, key: str):
        """Explicit management actions only; quitting never stops the daemon."""
        if key == "i":
            if self.manager.system:
                raise ValueError(
                    "Install a system unit first with service install --system --run-as USER."
                )
            self.manager.install(self.config_path, now=True, linger=True)
            self.note = "Service registered and enabled; check readiness in the logs."
        elif key in {"s", "x", "r", "e", "d"}:
            action = {
                "s": "start",
                "x": "stop",
                "r": "restart",
                "e": "enable",
                "d": "disable",
            }[key]
            self.manager.control(action)
            self.note = f"{action}: request completed."
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


def _clean(text: str) -> str:
    return "".join(character if character.isprintable() else " " for character in text)


def _put(screen, row, column, text, attribute=0):
    # Terminals count wide characters differently; curses clips and may report
    # the bottom-right cell as an error after drawing it. Both are harmless.
    import curses

    height, width = screen.getmaxyx()
    if 0 <= row < height and 0 <= column < width - 1:
        try:
            screen.addnstr(
                row, column, _clean(str(text)), width - column - 1, attribute
            )
        except curses.error:
            pass


def _popup(screen, title: str, body: str):
    import curses

    offset = 0
    screen.timeout(-1)
    try:
        while True:
            height, width = screen.getmaxyx()
            lines = [
                part
                for line in body.splitlines()
                for part in (textwrap.wrap(_clean(line), max(10, width - 4)) or [""])
            ]
            page = max(1, height - 4)
            offset = max(0, min(offset, max(0, len(lines) - page)))
            screen.erase()
            _put(screen, 0, 1, title, curses.A_BOLD)
            for row, line in enumerate(lines[offset : offset + page], 2):
                _put(screen, row, 2, line)
            _put(screen, height - 1, 1, "Up/Down/PgUp/PgDn: scroll | Enter/Esc/q: back")
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), 27, 10, 13):
                return
            if key in (curses.KEY_DOWN, ord("j")):
                offset += 1
            elif key in (curses.KEY_UP, ord("k")):
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
    status = {}
    logs = ""
    linger = "unknown"
    next_refresh = 0.0
    while True:
        if time.monotonic() >= next_refresh:
            try:
                status = controller.manager.status()
                controller.config = load_config(controller.config_path)
                linger = controller.manager.linger_status()
                logs = controller.redact(controller.manager.logs(8))
            except UI_ERRORS as exc:
                controller.note = controller.redact(str(exc))
            next_refresh = time.monotonic() + 2
        height, width = screen.getmaxyx()
        screen.erase()
        _put(screen, 0, 1, "Kohaku KT MCP | Linux service manager", curses.A_BOLD)
        if height < 18 or width < 64:
            _put(screen, 2, 1, "Resize terminal to at least 64 x 18. Press q to quit.")
        else:
            config = controller.config.settings
            scope = "System" if controller.manager.system else "User"
            _put(screen, 2, 2, f"Unit: {controller.manager.unit_name}   Scope: {scope}")
            _put(
                screen,
                3,
                2,
                f"State: {status.get('ActiveState', '?')} / {status.get('SubState', '?')}",
            )
            _put(
                screen,
                4,
                2,
                f"Boot: {status.get('UnitFileState', '?')}   "
                f"PID: {status.get('MainPID', '0')}   Restarts: {status.get('NRestarts', '0')}   Linger: {linger}",
            )
            _put(screen, 5, 2, f"Workspace: {config.workspace}")
            _put(screen, 6, 2, f"Mode: {config.mode}   Local port: {config.port}")
            _put(screen, 7, 2, f"Config: {controller.config_path}")
            _put(
                screen,
                8,
                2,
                "systemd owns the service. Closing this panel keeps it running.",
            )
            _put(screen, 10, 2, "[i] Install/start  [s] Start  [x] Stop  [r] Restart")
            _put(
                screen, 11, 2, "[e] Enable boot   [d] Disable boot   [g] Enable linger"
            )
            _put(screen, 12, 2, "[l] Logs          [c] Client JSON    [q] Quit panel")
            _put(screen, 14, 2, "Recent service log", curses.A_BOLD)
            for row, line in enumerate(logs.splitlines()[-max(0, height - 18) :], 15):
                if row >= height - 2:
                    break
                _put(screen, row, 2, line)
            _put(screen, height - 1, 1, controller.note)
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), 27):
            return
        if height < 18 or width < 64:
            continue
        if key < 0 or key > 255:
            continue
        character = chr(key)
        if character not in "isxredglc":
            continue
        controller.note = "Working..."
        _put(screen, height - 1, 1, controller.note)
        screen.refresh()
        try:
            popup = controller.act(character)
            if popup:
                _popup(screen, *popup)
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
    except (KeyboardInterrupt, EOFError):
        return 0
    except (locale.Error, *UI_ERRORS) as exc:
        parser.error(str(exc))
    return 0
