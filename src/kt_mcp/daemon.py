"""Persistent headless configuration and lifecycle for service managers."""

import argparse
import json
import os
import queue
import secrets
import shlex
import signal
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from kt_mcp.auth import MCPPathAuth
from kt_mcp.desktop import DesktopService, DesktopSettings


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not base.is_absolute():
        base = Path.home() / ".config"
    return base / "kt-mcp" / "config.json"


@dataclass(frozen=True)
class DaemonConfig:
    settings: DesktopSettings
    token: str
    tunnel_token: str = ""

    def validate(self):
        if not isinstance(self.settings.workspace, str) or not self.settings.workspace:
            raise ValueError("workspace must be an absolute, existing directory")
        if not Path(self.settings.workspace).is_absolute():
            raise ValueError("workspace must be an absolute, existing directory")
        self.settings.validate()
        if not isinstance(self.token, str):
            raise TypeError("token must be a string")
        MCPPathAuth(None, token=self.token)
        if not isinstance(self.tunnel_token, str):
            raise TypeError("tunnel_token must be a string")
        if not isinstance(self.settings.cloudflared, str):
            raise TypeError("cloudflared must be a string")
        if not isinstance(self.settings.public_origin, str):
            raise TypeError("public_origin must be a string")
        if self.settings.mode == "named":
            if not self.tunnel_token:
                raise ValueError("named mode requires TUNNEL_TOKEN or tunnel_token")
            origin = urlsplit(self.settings.public_origin)
            if (
                origin.scheme != "https"
                or not origin.hostname
                or origin.username is not None
                or origin.password is not None
                or origin.path
                or origin.query
                or origin.fragment
                or "\\" in self.settings.public_origin
                or any(
                    character.isspace() or not character.isprintable()
                    for character in self.settings.public_origin
                )
            ):
                raise ValueError("public_origin must be an HTTPS origin without a path")
            # Accessing port validates malformed or out-of-range values.
            _ = origin.port
        elif self.settings.public_origin:
            raise ValueError("public_origin is only used in named mode")


def write_private_json(path: Path, payload: dict, *, replace: bool = True):
    """Publish a complete file; tokens never pass through a world-readable file."""
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # Atomic no-clobber publication, including concurrent initializers.
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_config(config: DaemonConfig, path: Path, *, replace: bool = False):
    config.validate()
    write_private_json(
        path,
        {
            "version": 1,
            **asdict(config.settings),
            "token": config.token,
            "tunnel_token": config.tunnel_token,
        },
        replace=replace,
    )


def load_config(path: Path) -> DaemonConfig:
    path = Path(path).expanduser()
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("config must be a regular file")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(f"config must be private; run chmod 600 {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
    ):
        raise ValueError("config must be a version 1 JSON object")
    fields = set(DesktopSettings.__dataclass_fields__)
    if set(data) - fields - {"version", "token", "tunnel_token"}:
        raise ValueError("config contains unknown fields")
    config = DaemonConfig(
        DesktopSettings(**{key: data[key] for key in fields if key in data}),
        os.environ.get("KT_MCP_TOKEN", data.get("token", "")),
        os.environ.get("TUNNEL_TOKEN", data.get("tunnel_token", "")),
    )
    config.validate()
    return config


def startup_url(
    config: DaemonConfig, path: Path, *, expected_pid: int | None = None
) -> str:
    """Read a live daemon's matching startup record, without probing its endpoint.

    The panel supplies its unit's MainPID so another process's record cannot
    make that unit look ready. The record describes startup, not ongoing health.
    """
    if os.name != "posix":
        raise ValueError("daemon startup status requires POSIX")
    try:
        status = json.loads(
            Path(path)
            .expanduser()
            .with_suffix(".status.json")
            .read_text(encoding="utf-8")
        )
        pid = status["pid"]
        if type(pid) is not int or pid <= 0:
            raise ValueError("invalid daemon PID")
        if expected_pid is not None and (
            type(expected_pid) is not int or pid != expected_pid
        ):
            raise ValueError("daemon PID does not match this service")
        os.kill(pid, 0)
        url = status["url"]
        if not isinstance(url, str) or any(
            character.isspace() or not character.isprintable() for character in url
        ):
            raise ValueError("invalid daemon URL")
        if config.settings.mode in {"local", "named"}:
            if url != client_url(config, path):
                raise ValueError("outdated daemon URL")
        elif config.settings.mode == "quick":
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.path != "/mcp/" + config.token
                or "\\" in url
            ):
                raise ValueError("invalid tunnel URL")
            _ = parsed.port
        else:
            raise ValueError("outdated daemon status")
        return url
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        raise ValueError(
            "MCP startup is not confirmed; check the service logs"
        ) from None


def client_url(config: DaemonConfig, path: Path) -> str:
    """Return a configured address, or the current quick tunnel's startup URL."""
    settings = config.settings
    if settings.mode == "local":
        return f"http://127.0.0.1:{settings.port}/mcp/{config.token}"
    if settings.mode == "named":
        return f"{settings.public_origin}/mcp/{config.token}"
    if os.name != "posix":
        raise ValueError(
            "quick daemon status requires POSIX; use the Windows desktop GUI"
        )
    try:
        return startup_url(config, path)
    except ValueError:
        raise ValueError(
            "quick tunnel is not ready; start the service and try again"
        ) from None


def run_daemon(path: Path) -> int:
    """Run in the foreground: systemd, not a terminal, owns this process."""
    if sys.platform != "linux":
        raise ValueError(
            "daemon management requires Linux; use the desktop GUI on Windows"
        )
    path = Path(path).expanduser().absolute()
    config = load_config(path)
    status_path = path.with_suffix(".status.json")
    service = DesktopService()
    stopping = False
    failed = False
    previous = {}

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        service.stop()

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
        service.start(config.settings, config.token, config.tunnel_token)
        if stopping:
            service.stop()
        while service.running or not service.events.empty():
            try:
                kind, value = service.events.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind == "url":
                if value:
                    write_private_json(status_path, {"pid": os.getpid(), "url": value})
                continue
            if kind == "error":
                failed = True
            # DesktopService redacts credentials from non-URL events.
            print(f"[{kind}] {value}", file=sys.stderr, flush=True)
    finally:
        service.stop()
        if service.worker is not None:
            service.worker.join(timeout=20)
            failed = failed or service.running
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            if status.get("pid") == os.getpid():
                status_path.unlink()
        except (OSError, ValueError, AttributeError):
            pass
    return 1 if failed or not stopping else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kt-mcp")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="Create a private, persistent daemon configuration"
    )
    init.add_argument("--config", type=Path, default=default_config_path())
    init.add_argument(
        "--workspace",
        type=Path,
        help="Directory for MCP tools; omit to choose interactively in a terminal",
    )
    init.add_argument("--port", type=int, default=8765)
    init.add_argument("--mode", choices=("local", "quick", "named"), default="local")
    init.add_argument("--public-origin", default="")
    init.add_argument("--cloudflared", default="")
    init.add_argument("--allow-outside-workspace", action="store_true")
    for name, help_text in (
        ("daemon", "Run the service in the foreground"),
        ("client-config", "Print MCP client JSON"),
        ("link", "Print the complete MCP URL on one line"),
    ):
        child = commands.add_parser(name, help=help_text)
        child.add_argument("--config", type=Path, default=default_config_path())
        if name in {"link", "client-config"}:
            child.add_argument(
                "--output", type=Path, help="Create a private export file"
            )
        if name == "client-config":
            child.add_argument("--transport", choices=["http", "stdio"], default="http")
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.error("daemon management requires Linux; use the desktop GUI on Windows")
    try:
        if args.command == "init":
            if args.workspace is None:
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    parser.error(
                        "init needs --workspace PATH without an interactive terminal; "
                        "use kt-mcp init --workspace . for the current directory"
                    )
                from kt_mcp.onboarding import SetupCancelled, setup_config

                try:
                    setup_config(
                        args.config,
                        defaults=DesktopSettings(
                            port=args.port,
                            mode=args.mode,
                            public_origin=args.public_origin,
                            cloudflared=args.cloudflared,
                            allow_outside_workspace=args.allow_outside_workspace,
                        ),
                    )
                except SetupCancelled as exc:
                    print(str(exc))
                    return 0
            else:
                config = DaemonConfig(
                    DesktopSettings(
                        str(args.workspace.expanduser().resolve()),
                        args.port,
                        args.mode,
                        args.public_origin,
                        args.cloudflared,
                        allow_outside_workspace=args.allow_outside_workspace,
                    ),
                    os.environ.get("KT_MCP_TOKEN") or secrets.token_urlsafe(32),
                    os.environ.get("TUNNEL_TOKEN", ""),
                )
                save_config(config, args.config)
                print(f"Created private configuration: {args.config}")
            command = shlex.join(
                ["kt-mcp", "tui", "--config", str(args.config.expanduser().absolute())]
            )
            print(f"Next: {command}")
            print("Press i to start the service; the panel shows your MCP link.")
            return 0
        if args.command in {"client-config", "link"}:
            from kt_mcp.operations import export_text, write_export

            kind = args.command
            if kind == "client-config" and args.transport == "stdio":
                kind = "stdio"
            text = export_text(args.config, kind)
            if args.output is not None:
                print(f"Exported to {write_export(args.output, text)}")
            else:
                print(text)
            return 0
        return run_daemon(args.config)
    except (ValueError, OSError, TypeError) as exc:
        # Parser errors must not serialize the config or its secrets.
        parser.error(str(exc))
    return 2
