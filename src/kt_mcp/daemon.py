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
from kt_mcp.desktop import DesktopService, DesktopSettings, client_configuration


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


def client_url(config: DaemonConfig, path: Path) -> str:
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
        status = json.loads(
            Path(path)
            .expanduser()
            .with_suffix(".status.json")
            .read_text(encoding="utf-8")
        )
        pid = status["pid"]
        if type(pid) is not int or pid <= 0:
            raise ValueError("invalid daemon PID")
        os.kill(pid, 0)
        url = status["url"]
        if not isinstance(url, str) or not url.endswith("/mcp/" + config.token):
            raise ValueError("outdated daemon status")
        return url
    except (OSError, ValueError, KeyError, TypeError):
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
    for name in ("daemon", "client-config"):
        child = commands.add_parser(name)
        child.add_argument("--config", type=Path, default=default_config_path())
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
            print("Press i to register/start the service, then c for client settings.")
            return 0
        if args.command == "client-config":
            config = load_config(args.config)
            print(client_configuration(client_url(config, args.config)))
            return 0
        return run_daemon(args.config)
    except (ValueError, OSError, TypeError) as exc:
        # Parser errors must not serialize the config or its secrets.
        parser.error(str(exc))
    return 2
