"""Shared, reversible terminal setup before any configuration is written."""

import getpass
import os
import re
import secrets
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from kt_mcp.auth import MCPPathAuth
from kt_mcp.daemon import DaemonConfig, save_config
from kt_mcp.desktop import DesktopSettings


class SetupCancelled(Exception):
    """The user left setup without confirming a configuration."""


class _TerminalStyle:
    """Use the current terminal's capabilities without decorating redirected text."""

    def __init__(self):
        self._bold = ""
        self._reset = ""
        self._colors = {}
        term = os.environ.get("TERM", "")
        if (
            not sys.stdin.isatty()
            or not sys.stdout.isatty()
            or not term
            or term.lower() == "dumb"
        ):
            return
        try:
            import curses
        except ImportError:
            return

        def sequence(value: bytes | None) -> str:
            # terminfo padding directives are for tputs(), not printable text.
            return re.sub(rb"\$<[^>]*>", b"", value or b"").decode("ascii")

        try:
            curses.setupterm(term=term, fd=sys.stdout.fileno())
            reset = sequence(curses.tigetstr("sgr0"))
            if not reset:
                return
            bold = sequence(curses.tigetstr("bold"))
            colors = {}
            foreground = curses.tigetstr("setaf")
            if (
                "NO_COLOR" not in os.environ
                and foreground
                and curses.tigetnum("colors") >= 8
            ):
                for name, number in (("cyan", 6), ("green", 2), ("yellow", 3)):
                    colors[name] = sequence(curses.tparm(foreground, number))
        except (curses.error, OSError, ValueError):
            return
        self._bold = bold
        self._reset = reset
        self._colors = colors

    def text(self, value: str, *, bold: bool = False, color: str = "") -> str:
        prefix = (self._bold if bold else "") + self._colors.get(color, "")
        return prefix + value + self._reset if prefix else value

    def prompt(self, label: str, default: str | None = None) -> str:
        label = self.text(label, bold=True)
        if default is not None:
            label += " [" + self.text(default, bold=True, color="cyan") + "]"
        return label + ": "


def _read(reader: Callable[[str], str], prompt: str) -> str:
    try:
        # getpass normally falls back to echoing input if no terminal is usable.
        # Refuse that fallback rather than accidentally displaying a credential.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return reader(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise SetupCancelled("Setup cancelled; no configuration was saved.") from None
    except getpass.GetPassWarning:
        raise ValueError(
            "Hidden input is unavailable; use an interactive terminal or set TUNNEL_TOKEN."
        ) from None


def _workspace(value: str, cwd: Path) -> str:
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = cwd / path
        path = path.resolve()
        if path.is_dir():
            return str(path)
    except (OSError, ValueError, RuntimeError):
        pass
    raise ValueError(
        "Workspace must be an existing directory; enter an absolute, relative, or ~ path."
    )


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ValueError("Port must be an integer from 1 to 65535.")
    return port


def _mode(value: str) -> str:
    mode = {"1": "local", "2": "quick", "3": "named"}.get(value.lower(), value.lower())
    if mode not in {"local", "quick", "named"}:
        raise ValueError("Choose local, quick, or named (or 1, 2, or 3).")
    return mode


def _origin(value: str) -> str:
    try:
        origin = urlsplit(value)
        valid = (
            origin.scheme == "https"
            and bool(origin.hostname)
            and origin.username is None
            and origin.password is None
            and not origin.path
            and not origin.query
            and not origin.fragment
            and not any(character.isspace() for character in value)
            and "\\" not in value
        )
        _ = origin.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "Use an HTTPS origin such as https://mcp.example.com, "
            "without credentials, a trailing slash, path, query, or fragment."
        )
    return value


def setup_config(
    path: Path,
    *,
    ask: Callable[[str], str] | None = None,
    ask_secret: Callable[[str], str] | None = None,
    defaults: DesktopSettings | None = None,
) -> DaemonConfig:
    """Collect, review, and atomically save one new private configuration.

    Only the final empty confirmation writes a file. Backtracking retains valid
    answers, while cancellation raises SetupCancelled without saving.
    """
    path = Path(path).expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise FileExistsError(
            "configuration already exists; it will not be overwritten"
        )
    ask = input if ask is None else ask
    ask_secret = getpass.getpass if ask_secret is None else ask_secret
    cwd = Path.cwd().resolve()
    settings = defaults if defaults is not None else DesktopSettings(mode="local")
    workspace = Path(settings.workspace or cwd).expanduser()
    if not workspace.is_absolute():
        workspace = cwd / workspace
    values = {
        "workspace": str(workspace),
        "port": str(settings.port),
        "mode": settings.mode,
        "origin": settings.public_origin,
        "tunnel_token": "",
    }
    token = os.environ.get("KT_MCP_TOKEN") or secrets.token_urlsafe(32)
    try:
        MCPPathAuth(None, token=token)
    except ValueError:
        raise ValueError(
            "KT_MCP_TOKEN must contain 32-128 URL-safe characters "
            "(letters, digits, _ or -); fix or unset this environment variable."
        ) from None
    environment_tunnel_token = os.environ.get("TUNNEL_TOKEN", "")
    style = _TerminalStyle()
    print(style.text("Kohaku KT MCP · Setup", bold=True, color="cyan"))
    print("Enter = default   :back = back   :q = cancel")
    step = 0
    while True:
        fields = ["workspace", "port", "mode"]
        if values["mode"] == "named":
            fields.append("origin")
            if not environment_tunnel_token:
                fields.append("tunnel_token")

        if step == len(fields):
            config = DaemonConfig(
                DesktopSettings(
                    workspace=values["workspace"],
                    port=int(values["port"]),
                    mode=values["mode"],
                    public_origin=values["origin"] if values["mode"] == "named" else "",
                    cloudflared=settings.cloudflared,
                ),
                token,
                (
                    (environment_tunnel_token or values["tunnel_token"])
                    if values["mode"] == "named"
                    else ""
                ),
            )
            # Reuse the daemon's final validation before showing a save action.
            config.validate()
            print("\n" + style.text("Review", bold=True, color="cyan"))
            summary = [
                ("Workspace", config.settings.workspace),
                ("Port", str(config.settings.port)),
                ("Mode", config.settings.mode),
            ]
            if config.settings.mode == "named":
                summary.append(("Origin", config.settings.public_origin))
            if config.settings.cloudflared:
                summary.append(("Cloudflared", config.settings.cloudflared))
            summary.append(("Save to", str(path)))
            for label, value in summary:
                print(f"  {label:<11} {style.text(value, bold=True)}")
            answer = _read(
                ask,
                style.prompt("Save?", "Enter=save / edit / :back / :q"),
            ).lower()
            if answer == ":q":
                raise SetupCancelled("Setup cancelled; no configuration was saved.")
            if answer == ":back":
                step -= 1
                continue
            if answer == "edit":
                step = 0
                continue
            if answer:
                print(
                    style.text(
                        "Use Enter to save, edit to change, :back, or :q.",
                        color="yellow",
                    )
                )
                continue
            save_config(config, path)
            print(style.text("Saved", bold=True, color="green") + f" {path}")
            return config

        field = fields[step]
        if field == "workspace":
            prompt = style.prompt("Workspace", values[field])
        elif field == "port":
            prompt = style.prompt("Port", values[field])
        elif field == "mode":
            choices = (
                ("1 local", "this PC"),
                ("2 quick", "temp URL"),
                ("3 named", "your domain"),
            )
            print(
                "  ".join(
                    style.text(name, bold=True) + ": " + description
                    for name, description in choices
                )
            )
            prompt = style.prompt("Mode", values[field])
        elif field == "origin":
            prompt = (
                style.prompt("HTTPS origin", values[field])
                if values[field]
                else style.prompt("HTTPS origin (e.g. https://mcp.example.com)")
            )
        else:
            prompt = style.prompt(
                "Tunnel token (hidden; Enter keeps it)"
                if values[field]
                else "Tunnel token (hidden)"
            )
        answer = _read(ask_secret if field == "tunnel_token" else ask, prompt)
        command = answer.lower()
        if command == ":q":
            raise SetupCancelled("Setup cancelled; no configuration was saved.")
        if command == ":back":
            if step:
                step -= 1
            else:
                print("First step. Enter a workspace or :q.")
            continue
        value = answer or values[field]
        try:
            if field == "workspace":
                value = _workspace(value, cwd)
            elif field == "port":
                value = str(_port(value))
            elif field == "mode":
                value = _mode(value)
            elif field == "origin":
                value = _origin(value)
            elif not value:
                raise ValueError(
                    "A Cloudflare Tunnel token is required for named mode."
                )
        except ValueError as exc:
            print(style.text(str(exc), color="yellow"))
            continue
        values[field] = value
        if field == "mode" and value == "named" and environment_tunnel_token:
            print("Tunnel token: using TUNNEL_TOKEN (hidden).")
        step += 1
