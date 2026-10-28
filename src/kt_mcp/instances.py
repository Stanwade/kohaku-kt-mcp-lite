"""A private, file-backed catalog of independent workspace MCP instances."""

import hashlib
import json
import os
import re
import secrets
import socket
import stat
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from kt_mcp.daemon import DaemonConfig, default_config_path, save_config
from kt_mcp.desktop import DesktopSettings

MAX_FILE_BYTES = 1024 * 1024


class InstanceError(ValueError):
    """An instance cannot be selected or saved without conflicting with another."""


@dataclass(frozen=True)
class Instance:
    name: str
    config_path: Path
    workspace: str
    port: int
    mode: str
    error: str = ""


def canonical_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise InstanceError("An instance name is required.")
    value = name if name == "kt-mcp" or name.startswith("kt-mcp-") else "kt-mcp-" + name
    if len(value) > 80 or not re.fullmatch(r"kt-mcp(?:-[A-Za-z0-9_-]+)?", value):
        raise InstanceError(
            "Use an instance name containing letters, digits, _ or - "
            "(at most 73 characters after kt-mcp-)."
        )
    return value


def config_path(name: str) -> Path:
    name = canonical_name(name)
    default = default_config_path()
    return (
        default if name == "kt-mcp" else default.parent / "instances" / (name + ".json")
    )


def _path_key(path: Path) -> str:
    path = Path(path).expanduser()
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        path = path.absolute()
    return os.path.normcase(str(path))


def _unit_directory() -> Path:
    return default_config_path().parents[1] / "systemd/user"


def _read_file(path: Path, *, private: bool) -> bytes:
    if path.is_symlink():
        raise InstanceError("Linked files are not supported.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise InstanceError("The instance file is not a regular file.")
        if os.name == "posix" and (
            (info.st_uid != os.geteuid() and not (private and os.geteuid() == 0))
            or info.st_mode & (0o077 if private else 0o022)
        ):
            raise InstanceError(
                "The instance file has unsafe ownership or permissions."
            )
        data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise InstanceError("The instance file is too large.")
        latest = path.lstat()
        if (latest.st_dev, latest.st_ino, latest.st_mtime_ns, latest.st_size) != (
            info.st_dev,
            info.st_ino,
            info.st_mtime_ns,
            info.st_size,
        ):
            raise InstanceError("The instance file changed while being read.")
        return data


def read_config(path: Path) -> DaemonConfig:
    """Read saved credentials, without shell token overrides or error data leaks."""
    try:
        data = json.loads(_read_file(Path(path), private=True))
        fields = set(DesktopSettings.__dataclass_fields__)
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != 1
            or set(data) - fields - {"version", "token", "tunnel_token"}
        ):
            raise ValueError
        config = DaemonConfig(
            DesktopSettings(**{key: data[key] for key in fields if key in data}),
            data.get("token", ""),
            data.get("tunnel_token", ""),
        )
        config.validate()
        return config
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        raise InstanceError(
            "Configuration is invalid, unavailable, or not privately owned; inspect the file."
        ) from None


def _instance(name: str, path: Path) -> Instance:
    path = Path(path).expanduser().absolute()
    try:
        config = read_config(path)
    except InstanceError as error:
        return Instance(name, path, "", 0, "", str(error))
    settings = config.settings
    return Instance(name, path, settings.workspace, settings.port, settings.mode)


def list_instances() -> list[Instance]:
    """Discover configs and owned generated user units; never invoke systemd."""
    entries: dict[tuple[str, str], Instance] = {}

    def add(name, path, error=""):
        path = Path(path).expanduser().absolute()
        key = (name, _path_key(path))
        item = entries.get(key) or _instance(name, path)
        entries[key] = replace(item, error=error) if error else item

    default = default_config_path()
    if default.exists() or default.is_symlink():
        add("kt-mcp", default)
    directory = default.parent / "instances"
    if directory.is_dir() and not directory.is_symlink():
        for path in sorted(directory.glob("kt-mcp*.json")):
            try:
                name = canonical_name(path.stem)
            except InstanceError:
                continue
            if name != path.stem:
                continue
            add(name, path)

    from kt_mcp import managed_service

    synthetic = set(entries)
    unit_targets: dict[str, set[str]] = {}
    invalid_units = set()
    directory = _unit_directory()
    if directory.is_dir() and not directory.is_symlink():
        for path in sorted(directory.glob("kt-mcp*.service")):
            try:
                name = canonical_name(path.stem)
                if name != path.stem:
                    continue
                data = _read_file(path, private=False)
                if not data.startswith(
                    (managed_service.MANAGED_MARKER + "\n").encode()
                ):
                    continue
            except (OSError, InstanceError):
                continue
            try:
                _, _, target = managed_service._parse_unit(data, False)
            except (managed_service.ManagedServiceError, ValueError):
                invalid_units.add(name)
                add(
                    name,
                    config_path(name),
                    "The managed service unit needs inspection.",
                )
                continue
            unit_targets.setdefault(_path_key(target), set()).add(name)
            add(name, target)

    # A saved filename suggests a name only until an actual generated unit
    # identifies its service. Legacy --name installs may use config.json or a
    # differently named catalog file; do not invent a second instance for them.
    result = [
        item
        for key, item in entries.items()
        if key not in synthetic
        or key[1] not in unit_targets
        or item.name in unit_targets[key[1]]
        or item.name in invalid_units
    ]
    names: dict[str, int] = {}
    paths: dict[str, set[str]] = {}
    for item in result:
        names[item.name] = names.get(item.name, 0) + 1
        paths.setdefault(_path_key(item.config_path), set()).add(item.name)
    result = [
        (
            replace(item, error="Several configurations use this service name.")
            if names[item.name] > 1
            else (
                replace(item, error="Several service names share this configuration.")
                if len(paths[_path_key(item.config_path)]) > 1
                else item
            )
        )
        for item in result
    ]
    return sorted(
        result,
        key=lambda item: (item.name != "kt-mcp", item.name, str(item.config_path)),
    )


def get_instance(name: str) -> Instance:
    name = canonical_name(name)
    matches = [item for item in list_instances() if item.name == name]
    if not matches:
        raise InstanceError(f"Instance {name} does not exist.")
    if len(matches) != 1:
        raise InstanceError(f"Instance {name} has conflicting configuration paths.")
    return matches[0]


def find_workspace(path: Path) -> Instance | None:
    key = _path_key(path)
    matches = [
        item
        for item in list_instances()
        if item.workspace and _path_key(Path(item.workspace)) == key
    ]
    if len(matches) > 1:
        raise InstanceError(
            "Several instances use this workspace; select an instance by name."
        )
    return matches[0] if matches else None


def suggest_name(workspace: Path) -> str:
    path = Path(workspace).expanduser().resolve()
    plain = unicodedata.normalize("NFKD", path.name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:40] or "workspace"
    digest = hashlib.sha256(_path_key(path).encode("utf-8")).hexdigest()[:10]
    return "kt-mcp-" + slug + "-" + digest


def _excluded_path(exclude) -> str | None:
    if exclude is None:
        return None
    if isinstance(exclude, Instance):
        return _path_key(exclude.config_path)
    if isinstance(exclude, str) and "/" not in exclude and "\\" not in exclude:
        try:
            return _path_key(get_instance(exclude).config_path)
        except InstanceError:
            return _path_key(config_path(exclude))
    return _path_key(Path(exclude))


def _socket_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if os.name == "posix":
                # Match the server: closed connections in TIME_WAIT must not
                # prevent an immediate restart, but a live listener still wins.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def choose_port(start: int = 8765, exclude=None) -> int:
    if type(start) is not int or not 1 <= start <= 65535:
        raise InstanceError("The first port must be an integer from 1 to 65535.")
    excluded = _excluded_path(exclude)
    reserved = {
        item.port
        for item in list_instances()
        if _path_key(item.config_path) != excluded
    }
    for port in range(start, 65536):
        if port not in reserved and _socket_available(port):
            return port
    raise InstanceError("No available port remains; choose a lower starting port.")


def _origin_key(origin: str):
    parsed = urlsplit(origin)
    hostname = (parsed.hostname or "").rstrip(".").encode("idna").decode().lower()
    return hostname, parsed.port or 443


def ensure_available(config: DaemonConfig, exclude=None, check_socket: bool = False):
    """Check other saved instances, including stopped ones; exclude the current file."""
    config.validate()
    excluded = _excluded_path(exclude)
    workspace = _path_key(Path(config.settings.workspace))
    for item in list_instances():
        if _path_key(item.config_path) == excluded:
            continue
        if item.port == config.settings.port:
            raise InstanceError(f"Port {item.port} is reserved by {item.name}.")
        if item.workspace and _path_key(Path(item.workspace)) == workspace:
            raise InstanceError(f"This workspace already belongs to {item.name}.")
        if config.settings.mode == "named" and item.mode == "named":
            existing = read_config(item.config_path)
            if _origin_key(existing.settings.public_origin) == _origin_key(
                config.settings.public_origin
            ):
                raise InstanceError(
                    f"The named HTTPS origin is already used by {item.name}."
                )
    if check_socket and not _socket_available(config.settings.port):
        raise InstanceError(
            f"Port {config.settings.port} is already in use; choose another port."
        )


def _private_directory(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (
        os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o022)
    ):
        raise InstanceError(
            "The instance directory has unsafe ownership or permissions."
        )


@contextmanager
def catalog_lock():
    """Hold across conflict validation and publication, including configuration edits."""
    root = default_config_path().parent
    _private_directory(root)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root / ".instances.lock", flags, 0o600)
    acquired = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (
            os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077)
        ):
            raise InstanceError(
                "The instance lock has unsafe ownership or permissions."
            )
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                if not info.st_size:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            acquired = True
        except OSError:
            raise InstanceError(
                "Another instance configuration change is in progress; retry."
            ) from None
        yield
    finally:
        if acquired and os.name != "posix":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


def _assert_new(name: str) -> Path:
    path = config_path(name)
    if (
        path.exists()
        or path.is_symlink()
        or any(item.name == name for item in list_instances())
        or (_unit_directory() / (name + ".service")).exists()
        or (_unit_directory() / (name + ".service")).is_symlink()
    ):
        raise InstanceError(
            f"Instance {name} already exists; its configuration was not overwritten."
        )
    return path


def _save_new(name: str, config: DaemonConfig) -> Instance:
    path = _assert_new(name)
    ensure_available(config, check_socket=True)
    _private_directory(path.parent)
    save_config(config, path, replace=False)
    settings = config.settings
    return Instance(name, path, settings.workspace, settings.port, settings.mode)


def create_instance(
    name: str,
    *,
    workspace: Path,
    port: int | None = None,
    mode: str = "local",
    public_origin: str = "",
    cloudflared: str = "",
    tunnel_token: str = "",
) -> Instance:
    name = canonical_name(name)
    workspace = Path(workspace).expanduser().resolve()
    with catalog_lock():
        _assert_new(name)
        config = DaemonConfig(
            DesktopSettings(
                workspace=str(workspace),
                port=choose_port() if port is None else port,
                mode=mode,
                public_origin=public_origin,
                cloudflared=cloudflared,
            ),
            secrets.token_urlsafe(32),
            tunnel_token,
        )
        return _save_new(name, config)


def setup_instance(
    workspace: Path | None = None,
    name: str | None = None,
    ask=None,
    ask_secret=None,
) -> Instance:
    from kt_mcp.onboarding import _collect_config

    workspace = Path.cwd() if workspace is None else Path(workspace).expanduser()
    workspace = workspace.resolve()
    name = canonical_name(name) if name is not None else None
    path = (
        _assert_new(name) if name is not None else config_path(suggest_name(workspace))
    )
    saved = None

    def final_name(config):
        return (
            name if name is not None else suggest_name(Path(config.settings.workspace))
        )

    def publish(config):
        nonlocal saved
        with catalog_lock():
            saved = _save_new(final_name(config), config)

    _collect_config(
        path,
        DesktopSettings(workspace=str(workspace), port=choose_port(), mode="local"),
        secrets.token_urlsafe(32),
        ask=ask,
        ask_secret=ask_secret,
        tunnel_token="",
        environment_tunnel_token=os.environ.get("TUNNEL_TOKEN", ""),
        save=publish,
        title="New instance" if name is None else "New instance · " + name,
        destination=lambda config: config_path(final_name(config)),
    )
    return saved
