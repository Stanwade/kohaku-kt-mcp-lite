"""Shared, reversible terminal setup before any configuration is written."""

import getpass
import json
import os
import secrets
import stat
import tempfile
import warnings
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from kt_mcp.auth import MCPPathAuth
from kt_mcp.daemon import DaemonConfig, save_config
from kt_mcp.desktop import DesktopSettings
from kt_mcp.terminal_style import TerminalStyle as _TerminalStyle


class SetupCancelled(Exception):
    """The user left setup without confirming a configuration."""


@dataclass(frozen=True)
class _ConfigSnapshot:
    data: bytes = field(repr=False)
    identity: tuple[int, ...]
    uid: int
    gid: int


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_gid,
    )


def _snapshot(path: Path) -> _ConfigSnapshot:
    if path.is_symlink():
        raise ValueError("Configuration must not be a symbolic link.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Configuration must be a regular file.")
        if os.name == "posix":
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("Configuration must be private; set its mode to 600.")
            if os.geteuid() not in (0, info.st_uid):
                raise PermissionError("Configuration belongs to another account.")
        data = stream.read()
        identity = _file_identity(info)
        if (
            _file_identity(os.fstat(stream.fileno())) != identity
            or _file_identity(path.lstat()) != identity
        ):
            raise ValueError("Configuration changed while it was being read; retry.")
    return _ConfigSnapshot(data, identity, info.st_uid, info.st_gid)


class ConfigEditor:
    """Keep a private config's original bytes for checked publication and rollback.

    The saved file, rather than this shell's token environment, supplies edit
    defaults. Both save and rollback refuse a file changed by another writer.
    """

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().absolute()
        self._original = _snapshot(self.path)
        self._saved: _ConfigSnapshot | None = None
        try:
            data = json.loads(self._original.data)
        except (ValueError, UnicodeError):
            raise ValueError("Configuration must contain valid JSON.") from None
        fields = set(DesktopSettings.__dataclass_fields__)
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != 1
            or set(data) - fields - {"version", "token", "tunnel_token"}
        ):
            raise ValueError("Configuration must be a supported version 1 object.")
        self.config = DaemonConfig(
            DesktopSettings(**{key: data[key] for key in fields if key in data}),
            data.get("token", ""),
            data.get("tunnel_token", ""),
        )
        self.config.validate()

    @property
    def saved(self) -> bool:
        return self._saved is not None

    def _unchanged(self, expected: _ConfigSnapshot):
        try:
            unchanged = _snapshot(self.path) == expected
        except (OSError, ValueError):
            unchanged = False
        if not unchanged:
            raise ValueError(
                "Configuration changed outside this editor; reload and try again."
            )

    def _replace(self, expected: _ConfigSnapshot, data: bytes) -> _ConfigSnapshot:
        self._unchanged(expected)
        fd, temporary_name = tempfile.mkstemp(
            prefix="." + self.path.name + ".", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                if os.name == "posix":
                    info = os.fstat(stream.fileno())
                    if (info.st_uid, info.st_gid) != (expected.uid, expected.gid):
                        os.fchown(stream.fileno(), expected.uid, expected.gid)
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                written = os.fstat(stream.fileno())
            self._unchanged(expected)
            os.replace(temporary, self.path)
            published = _snapshot(self.path)
            if published.data != data or published.identity[:2] != (
                written.st_dev,
                written.st_ino,
            ):
                raise ValueError("Configuration changed while saving; reload it.")
            return published
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, config: DaemonConfig):
        if self.saved:
            raise ValueError("This configuration edit has already been saved.")
        config.validate()
        if config.token != self.config.token:
            raise ValueError("Editing settings must preserve the MCP access token.")
        data = (
            json.dumps(
                {
                    "version": 1,
                    **asdict(config.settings),
                    "token": config.token,
                    "tunnel_token": config.tunnel_token,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        self._saved = self._replace(self._original, data)

    def restore(self):
        """Restore only this editor's still-current publication after a failed edit."""
        if self._saved is not None:
            self._replace(self._saved, self._original.data)
            self._saved = None


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
    settings = defaults if defaults is not None else DesktopSettings(mode="local")
    token = os.environ.get("KT_MCP_TOKEN") or secrets.token_urlsafe(32)
    try:
        MCPPathAuth(None, token=token)
    except ValueError:
        raise ValueError(
            "KT_MCP_TOKEN must contain 32-128 URL-safe characters "
            "(letters, digits, _ or -); fix or unset this environment variable."
        ) from None
    return _collect_config(
        path,
        settings,
        token,
        ask=ask,
        ask_secret=ask_secret,
        tunnel_token="",
        environment_tunnel_token=os.environ.get("TUNNEL_TOKEN", ""),
        save=lambda config: save_config(config, path),
        title="Setup",
    )


def edit_config(
    path: Path,
    *,
    ask: Callable[[str], str] | None = None,
    ask_secret: Callable[[str], str] | None = None,
    editor: ConfigEditor | None = None,
    before_save: Callable[[DaemonConfig], None] | None = None,
) -> DaemonConfig:
    """Review existing settings, preserving credentials and the file's owner.

    A caller coordinating a service change can supply an editor and call its
    restore() method if updating the unit fails after the user saves. before_save
    checks service state while the original configuration is still on disk.
    """
    path = Path(path).expanduser().absolute()
    editor = ConfigEditor(path) if editor is None else editor
    if editor.path != path:
        raise ValueError("The editor belongs to a different configuration file.")

    def save(config: DaemonConfig):
        if before_save is not None:
            before_save(config)
        editor.save(config)

    try:
        return _collect_config(
            path,
            editor.config.settings,
            editor.config.token,
            ask=ask,
            ask_secret=ask_secret,
            tunnel_token=editor.config.tunnel_token,
            environment_tunnel_token="",
            save=save,
            title="Configure",
        )
    except SetupCancelled:
        raise SetupCancelled("Changes cancelled; configuration unchanged.") from None


def _collect_config(
    path: Path,
    settings: DesktopSettings,
    token: str,
    *,
    ask: Callable[[str], str] | None,
    ask_secret: Callable[[str], str] | None,
    tunnel_token: str,
    environment_tunnel_token: str,
    save: Callable[[DaemonConfig], None],
    title: str,
) -> DaemonConfig:
    ask = input if ask is None else ask
    ask_secret = getpass.getpass if ask_secret is None else ask_secret
    cwd = Path.cwd().resolve()
    workspace = Path(settings.workspace or cwd).expanduser()
    if not workspace.is_absolute():
        workspace = cwd / workspace
    values = {
        "workspace": str(workspace),
        "port": str(settings.port),
        "mode": settings.mode,
        "origin": settings.public_origin,
        "tunnel_token": tunnel_token,
    }
    style = _TerminalStyle()
    print(style.text("Kohaku KT MCP · " + title, bold=True, color="cyan"))
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
            save(config)
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
