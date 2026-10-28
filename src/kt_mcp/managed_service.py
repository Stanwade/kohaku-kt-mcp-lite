"""Strict service bindings usable by a standalone, standard-library updater.

This module intentionally imports no other kt_mcp modules or third-party
packages: an updater must remain usable while its installed venv is replaced.
"""

import errno
import hashlib
import http.client
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

MANAGED_MARKER = "# Managed by kt-mcp service (v1)"
STATUS_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "MainPID",
    "NRestarts",
    "ExecMainStatus",
    "FragmentPath",
    "DropInPaths",
    "NeedDaemonReload",
    "ControlGroup",
)
COMMAND_TIMEOUT = 40.0
_STANDARD_PATH = ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_WORD = re.compile(r'"((?:[^"\\\x00-\x1f\x7f]|\\["\\])*)"')


class ManagedServiceError(RuntimeError):
    """A binding cannot safely be inspected, controlled, or restored."""


class StartupWaitCancelled(ManagedServiceError):
    """The caller ended readiness waiting after issuing the start request."""


@dataclass(frozen=True)
class BoundService:
    name: str
    system: bool
    unit_path: Path
    python: Path
    config_path: Path
    run_as: str | None
    owner_uid: int
    owner_gid: int
    unit_bytes: bytes
    unit_hash: str
    status: dict[str, str]
    unit_mode: int
    unit_owner_uid: int
    unit_owner_gid: int

    @property
    def gid(self) -> int:
        return self.owner_gid


def _linux():
    if sys.platform != "linux":
        raise ManagedServiceError("Managed service operations require Linux")


def _name(name: str):
    if (
        not isinstance(name, str)
        or len(name) > 80
        or not re.fullmatch(r"kt-mcp(?:-[A-Za-z0-9_-]+)?", name)
    ):
        raise ManagedServiceError("Invalid managed service name")


def _absolute(path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _unit_directory(system: bool) -> Path:
    if system:
        return Path("/etc/systemd/system")
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not base.is_absolute():
        base = Path.home() / ".config"
    return base / "systemd/user"


def _venv(python) -> Path:
    # Resolve the venv directory, never bin/python (usually a system symlink).
    return _absolute(python).parent.parent.resolve()


def _run(system: bool, *arguments: str, timeout=COMMAND_TIMEOUT) -> str:
    command = ["systemctl", "--no-ask-password", "--no-pager"]
    if not system:
        command.append("--user")
    try:
        result = subprocess.run(
            command + list(arguments),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(COMMAND_TIMEOUT, max(0.01, timeout)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ManagedServiceError(
            "Could not contact systemd within the command timeout"
        ) from None
    missing = (
        arguments
        and arguments[0] == "show"
        and "LoadState=not-found" in result.stdout.splitlines()
    )
    empty_unit_files = (
        arguments
        and arguments[0] == "list-unit-files"
        and result.returncode == 1
        and result.stdout == ""
        and result.stderr == ""
    )
    if result.returncode and not (missing or empty_unit_files):
        # External diagnostics may contain configuration or credential values.
        raise ManagedServiceError(
            "systemd command failed; check the service manager and account permissions"
        )
    return result.stdout


def _status(name: str, system: bool, timeout=COMMAND_TIMEOUT) -> dict[str, str]:
    output = _run(
        system,
        "show",
        "--property=" + ",".join(STATUS_PROPERTIES),
        name + ".service",
        timeout=timeout,
    )
    result = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in STATUS_PROPERTIES:
            if key in result:
                raise ManagedServiceError(
                    "systemd returned duplicate service properties"
                )
            result[key] = value
    if not result.get("LoadState"):
        raise ManagedServiceError("systemd did not return a service load state")
    return result


def _file_read_error(path: Path, error: OSError) -> ManagedServiceError:
    reason = (
        f"{errno.errorcode[error.errno]} ({os.strerror(error.errno)})"
        if error.errno in errno.errorcode
        else "operating system read error"
    )
    return ManagedServiceError(
        f"Could not read a regular managed service file {str(path)!r}: {reason}"
    )


def _read_regular(path: Path, *, limit=65536):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ManagedServiceError("Service files must be regular, not linked")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ManagedServiceError("Service file exceeds the supported size")
        return data, info
    except OSError as error:
        raise _file_read_error(path, error) from None


def _literal(value: str) -> str:
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise ManagedServiceError("Unsupported control character in service unit")
    pieces = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            if value[index : index + 2] != "%%":
                raise ManagedServiceError(
                    "Unsupported systemd specifier in service unit"
                )
            index += 1
        pieces.append(character)
        index += 1
    return "".join(pieces)


def _words(value: str) -> list[str]:
    result = []
    index = 0
    while index < len(value):
        match = _WORD.match(value, index)
        if match is None:
            raise ManagedServiceError("Unsupported quoting in managed ExecStart")
        word = re.sub(r'\\(["\\])', r"\1", match[1])
        result.append(_literal(word))
        index = match.end()
        if index < len(value):
            if value[index] != " " or index + 1 == len(value):
                raise ManagedServiceError("Unsupported managed command separator")
            index += 1
    return result


def _parse_unit(data: bytes, system: bool):
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise ManagedServiceError("Managed unit is not valid UTF-8") from None
    if not text.startswith(MANAGED_MARKER + "\n"):
        raise ManagedServiceError("Service unit is not managed by kt-mcp")
    sections = {}
    section = None
    for line in text.splitlines()[1:]:
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if line not in {"[Unit]", "[Service]", "[Install]"} or line in sections:
                raise ManagedServiceError("Unsupported or repeated service section")
            section = sections[line] = {}
            continue
        key, separator, value = line.partition("=")
        if section is None or not separator:
            raise ManagedServiceError("Unsupported managed unit syntax")
        if key in section and key != "Environment":
            raise ManagedServiceError("Repeated directive in managed service unit")
        section.setdefault(key, []).append(value)
    if set(sections) != {"[Unit]", "[Service]", "[Install]"}:
        raise ManagedServiceError("Managed service unit has incomplete sections")
    unit, service, install = (
        sections["[Unit]"],
        sections["[Service]"],
        sections["[Install]"],
    )
    if unit != {"Description": ["Kohaku KT MCP workspace service"]} or install != {
        "WantedBy": ["multi-user.target" if system else "default.target"]
    }:
        raise ManagedServiceError("Unsupported managed unit overrides")
    fixed = {
        "Type": "exec",
        "Restart": "on-failure",
        "RestartSec": "5",
        "TimeoutStopSec": "30",
        "KillMode": "control-group",
        "UMask": "0077",
        "NoNewPrivileges": "true",
    }
    allowed = set(fixed) | {"WorkingDirectory", "ExecStart", "Environment"}
    if system:
        allowed |= {"User", "Group"}
    if set(service) != allowed or any(
        service[key] != [value] for key, value in fixed.items()
    ):
        raise ManagedServiceError("Unsupported managed service directives")
    command = service["ExecStart"][0]
    if not command.startswith(":"):
        raise ManagedServiceError(
            "Managed ExecStart must disable environment expansion"
        )
    words = _words(command[1:])
    if (
        len(words) != 8
        or words[:2] != ["/usr/bin/env", "--"]
        or words[3:7] != ["-m", "kt_mcp", "daemon", "--config"]
        or not Path(words[2]).is_absolute()
        or not Path(words[7]).is_absolute()
    ):
        raise ManagedServiceError("Managed ExecStart does not bind a kt-mcp daemon")
    return service, _absolute(words[2]), _absolute(words[7])


def _account(system: bool, run_as: str | None):
    import pwd

    try:
        account = pwd.getpwnam(run_as) if system else pwd.getpwuid(os.geteuid())
    except KeyError:
        raise ManagedServiceError("Service account does not exist") from None
    if account.pw_uid == 0 or not Path(account.pw_dir).is_absolute():
        raise ManagedServiceError("Managed MCP services require an ordinary account")
    return account


def _private_json(path: Path, owner_uid: int) -> dict:
    data, info = _read_regular(path)
    if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o600:
        raise ManagedServiceError(
            "Service configuration must be owned by its user and mode 600"
        )
    try:
        value = json.loads(data)
    except (UnicodeError, ValueError):
        raise ManagedServiceError("Service private JSON file is invalid") from None
    if not isinstance(value, dict):
        raise ManagedServiceError("Service private JSON file must be an object")
    return value


def _check_loaded(status: dict[str, str], path: Path):
    if status.get("LoadState") != "loaded":
        raise ManagedServiceError("Managed unit is not loaded normally")
    if status.get("DropInPaths") != "":
        raise ManagedServiceError(
            "Service drop-ins are not supported for this operation"
        )
    if status.get("NeedDaemonReload") != "no":
        raise ManagedServiceError(
            "Service unit needs daemon-reload; inspect it before continuing"
        )
    fragment = status.get("FragmentPath", "")
    if not Path(fragment).is_absolute() or _absolute(fragment) != path:
        raise ManagedServiceError(
            "Loaded service fragment does not match the managed unit"
        )


def _inspect(
    name,
    system,
    *,
    expected_python=None,
    expected_config=None,
    timeout=COMMAND_TIMEOUT,
    for_removal=False,
):
    _linux()
    _name(name)
    path = _unit_directory(system) / (name + ".service")
    status = _status(name, system, timeout)
    if status["LoadState"] == "not-found":
        if path.exists() or path.is_symlink():
            raise ManagedServiceError(
                "A service file exists but systemd has not loaded it"
            )
        return None
    _check_loaded(status, path)
    data, info = _read_regular(path)
    service, python, config_path = _parse_unit(data, system)
    run_as = _literal(service["User"][0]) if system else None
    account = _account(system, run_as)
    if info.st_uid != (0 if system else account.pw_uid) or info.st_mode & 0o022:
        raise ManagedServiceError(
            "Managed unit has unsafe ownership or write permissions"
        )
    if system and service["Group"] != [str(account.pw_gid)]:
        raise ManagedServiceError("Managed service group differs from the account")
    if expected_python is not None and _venv(python) != _venv(expected_python):
        raise ManagedServiceError("Service belongs to another Python environment")
    if (
        expected_config is not None
        and config_path.resolve() != _absolute(expected_config).resolve()
    ):
        raise ManagedServiceError("Service belongs to another configuration")
    if not for_removal:
        config = _private_json(config_path, account.pw_uid)
        workspace = config.get("workspace")
        if (
            type(config.get("version")) is not int
            or config["version"] != 1
            or not isinstance(workspace, str)
            or not Path(workspace).is_absolute()
            or not Path(workspace).is_dir()
            or _literal(service["WorkingDirectory"][0]) != str(_absolute(workspace))
        ):
            raise ManagedServiceError(
                "Service working directory does not match its configuration"
            )
    environment = service["Environment"]
    quoted = [_words(value) for value in environment if value.startswith('"')]
    if (
        len(environment) != 3
        or environment.count("PYTHONUNBUFFERED=1") != 1
        or len(quoted) != 2
        or sorted(quoted)
        != sorted(
            [
                ["HOME=" + account.pw_dir],
                ["PATH=" + str(python.parent) + _STANDARD_PATH],
            ]
        )
    ):
        raise ManagedServiceError("Unsupported managed service environment")
    return BoundService(
        name=name,
        system=system,
        unit_path=path,
        python=python,
        config_path=config_path,
        run_as=run_as,
        owner_uid=account.pw_uid,
        owner_gid=account.pw_gid,
        unit_bytes=data,
        unit_hash=hashlib.sha256(data).hexdigest(),
        status=status,
        unit_mode=stat.S_IMODE(info.st_mode),
        unit_owner_uid=info.st_uid,
        unit_owner_gid=info.st_gid,
    )


def inspect_service(
    name="kt-mcp", system=False, *, expected_python=None, expected_config=None
) -> BoundService | None:
    """Bind only an unchanged, fully understood generated service."""
    return _inspect(
        name, system, expected_python=expected_python, expected_config=expected_config
    )


def inspect_removal_service(
    name="kt-mcp", system=False, *, expected_python=None, expected_config=None
) -> BoundService | None:
    """Verify a generated unit for deletion even if its configuration is damaged.

    The caller must independently validate the configuration file ownership and
    snapshot before unlinking it. This binding must never be used to start a unit.
    """
    return _inspect(
        name,
        system,
        expected_python=expected_python,
        expected_config=expected_config,
        for_removal=True,
    )


def _refresh(
    binding: BoundService, timeout=COMMAND_TIMEOUT, *, for_removal=False
) -> BoundService:
    current = _inspect(
        binding.name,
        binding.system,
        expected_python=binding.python,
        expected_config=binding.config_path,
        timeout=timeout,
        for_removal=for_removal,
    )
    if current is None or (
        current.unit_bytes != binding.unit_bytes
        or current.owner_uid != binding.owner_uid
        or current.owner_gid != binding.owner_gid
        or current.unit_owner_uid != binding.unit_owner_uid
        or current.unit_owner_gid != binding.unit_owner_gid
        or current.unit_mode != binding.unit_mode
    ):
        raise ManagedServiceError(
            "Service binding changed; inspect it again before continuing"
        )
    return current


def _authorize(binding: BoundService):
    if binding.system:
        if os.geteuid() != 0:
            raise ManagedServiceError(
                "System service changes require root; no automatic sudo"
            )
    elif os.geteuid() == 0 or os.geteuid() != binding.owner_uid:
        raise ManagedServiceError(
            "User service changes require the service's ordinary account"
        )


def _pid(status: dict[str, str]) -> int:
    value = status.get("MainPID", "")
    if not re.fullmatch(r"[0-9]+", value):
        raise ManagedServiceError("systemd returned an invalid service PID")
    return int(value)


def assert_external_process(binding: BoundService, *, timeout=40) -> None:
    """Refuse to stop a unit containing this process, including nested cgroups."""
    current = _refresh(binding, timeout)
    _assert_external_binding(current)


def assert_removal_external(binding: BoundService, *, timeout=40) -> None:
    """Apply the same process/cgroup protection to a removal-only binding."""
    current = _refresh(binding, timeout, for_removal=True)
    _assert_external_binding(current)


def _assert_external_binding(current: BoundService) -> None:
    pid = _pid(current.status)
    group = current.status.get("ControlGroup", "")
    if not group and not pid:
        return
    if pid == os.getpid():
        raise ManagedServiceError(
            "Run this operation in a terminal outside the target service"
        )
    if not group.startswith("/") or group == "/" or ".." in Path(group).parts:
        raise ManagedServiceError("Could not establish the target service cgroup")
    try:
        entries = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        raise ManagedServiceError(
            "Could not check the current process cgroup"
        ) from None
    checked = False
    for line in entries:
        parts = line.split(":", 2)
        if len(parts) != 3 or not (
            parts[0] == "0" or "name=systemd" in parts[1].split(",")
        ):
            continue
        checked = True
        if parts[2] == group or parts[2].startswith(group.rstrip("/") + "/"):
            raise ManagedServiceError(
                "Run this operation in a terminal outside the target service"
            )
    if not checked:
        raise ManagedServiceError("Could not identify the current systemd cgroup")


def _deadline(timeout: float) -> float:
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be positive")
    return time.monotonic() + timeout


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ManagedServiceError(
            "Managed service did not reach the requested state in time"
        )
    return remaining


def stop_service(binding: BoundService, *, timeout=40) -> None:
    """Stop exactly this binding, preserving enabled state and unit contents."""
    deadline = _deadline(timeout)
    _authorize(binding)
    assert_external_process(binding, timeout=_remaining(deadline))
    _refresh(binding, _remaining(deadline))
    _run(
        binding.system, "stop", binding.name + ".service", timeout=_remaining(deadline)
    )
    while True:
        current = _refresh(binding, _remaining(deadline))
        if current.status.get("ActiveState") in {"inactive", "failed"} and not _pid(
            current.status
        ):
            return
        time.sleep(min(0.1, _remaining(deadline)))


def _startup(binding: BoundService, pid: int) -> tuple[int, str]:
    config = _private_json(binding.config_path, binding.owner_uid)
    status = _private_json(
        binding.config_path.with_suffix(".status.json"), binding.owner_uid
    )
    token, port = config.get("token"), config.get("port", 8765)
    if (
        not isinstance(token, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)
        or type(port) is not int
        or not 1 <= port <= 65535
        or type(status.get("pid")) is not int
        or status["pid"] != pid
    ):
        raise ManagedServiceError("Current daemon startup has not been confirmed")
    url = status.get("url")
    if not isinstance(url, str) or any(
        character.isspace() or not character.isprintable() for character in url
    ):
        raise ManagedServiceError("Current daemon startup has not been confirmed")
    mode = config.get("mode", "quick")
    if mode in {"local", "named"}:
        origin = (
            f"http://127.0.0.1:{port}"
            if mode == "local"
            else config.get("public_origin", "")
        )
        if url != origin + "/mcp/" + token:
            raise ManagedServiceError("Current daemon startup has not been confirmed")
    elif mode == "quick":
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and "\\" not in url
                and parsed.path == "/mcp/" + token
            )
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ManagedServiceError("Current daemon startup has not been confirmed")
    else:
        raise ManagedServiceError("Current daemon startup has not been confirmed")
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        raise ManagedServiceError(
            "Current daemon startup has not been confirmed"
        ) from None
    return port, "/mcp/" + token


def _initialize(port: int, path: str, timeout: float) -> str:
    # Direct loopback HTTP ignores proxy settings and never follows redirects.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(
            "POST",
            path,
            body=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "kt-mcp-service-check", "version": "1"},
                    },
                }
            ),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ManagedServiceError("The local MCP initialization check failed")
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ManagedServiceError(
                "The local MCP initialization response is too large"
            )
        data = json.loads(raw)
        server = data["result"]["serverInfo"]
        if (
            data.get("jsonrpc") != "2.0"
            or data.get("id") != 1
            or "error" in data
            or server.get("name") != "Kohaku KT MCP"
            or not isinstance(server.get("version"), str)
            or not server["version"]
        ):
            raise ManagedServiceError(
                "The local MCP initialization response is invalid"
            )
        return server["version"]
    except (
        OSError,
        http.client.HTTPException,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ):
        raise ManagedServiceError("The local MCP initialization check failed") from None
    finally:
        connection.close()


def wait_ready(
    binding: BoundService,
    *,
    timeout=60,
    expected_version: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, str]:
    """Require matching startup PID and a successful local MCP initialize.

    Cancellation ends only this read-only wait. The start request and service
    lifecycle remain independent of the caller's readiness view.
    """
    deadline = _deadline(timeout)
    last_progress = None

    def check_cancelled():
        if cancel is not None and cancel():
            raise StartupWaitCancelled(
                "Startup wait cancelled; check the instance status."
            )

    def report(message):
        nonlocal last_progress
        if on_progress is not None and message != last_progress:
            on_progress(message)
        last_progress = message

    check_cancelled()
    report("Waiting for systemd to start the service...")
    while True:
        check_cancelled()
        current = _refresh(binding, _remaining(deadline))
        check_cancelled()
        state = current.status.get("ActiveState")
        if state == "failed":
            raise ManagedServiceError("The managed service failed during startup")
        if state == "active" and (pid := _pid(current.status)) > 0:
            report("Waiting for MCP / Cloudflare startup...")
            try:
                startup = _startup(current, pid)
            except ManagedServiceError:
                startup = None
            if startup is not None:
                check_cancelled()
                report("Checking the local MCP connection...")
                check_cancelled()
                try:
                    port, path = startup
                    version = _initialize(port, path, min(2.0, _remaining(deadline)))
                except ManagedServiceError:
                    version = None
                check_cancelled()
                if version is not None:
                    if expected_version is not None and version != expected_version:
                        raise ManagedServiceError(
                            "Running MCP version differs from the requested version"
                        )
                    verified = _refresh(binding, _remaining(deadline))
                    check_cancelled()
                    if (
                        verified.status.get("ActiveState") == "active"
                        and _pid(verified.status) == pid
                    ):
                        return verified.status
        else:
            report("Waiting for systemd to start the service...")
        check_cancelled()
        time.sleep(min(0.1, _remaining(deadline)))


def start_service(
    binding: BoundService,
    *,
    timeout=60,
    expected_version: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, str]:
    """Start this unit, then allow cancellation of readiness waiting only."""
    deadline = _deadline(timeout)
    _authorize(binding)
    assert_external_process(binding, timeout=_remaining(deadline))
    _refresh(binding, _remaining(deadline))
    if on_progress is not None:
        on_progress("Starting the systemd service...")
    _run(
        binding.system, "start", binding.name + ".service", timeout=_remaining(deadline)
    )
    options = {}
    if on_progress is not None:
        options["on_progress"] = on_progress
    if cancel is not None:
        options["cancel"] = cancel
    return wait_ready(
        binding,
        timeout=_remaining(deadline),
        expected_version=expected_version,
        **options,
    )


def find_services(python) -> list[BoundService]:
    """Find user/system units using this venv, failing on ambiguous project units."""
    _linux()
    target = _venv(python)
    bindings = []
    for system in (False, True):
        names = set()
        for action in ("list-units", "list-unit-files"):
            output = _run(
                system,
                action,
                "--all",
                "--type=service",
                "--plain",
                "--no-legend",
                "kt-mcp*.service",
            )
            for line in output.splitlines():
                fields = line.split()
                if fields and fields[0].endswith(".service"):
                    candidate = fields[0][:-8]
                    _name(candidate)
                    names.add(candidate)
        names.update(
            path.name[:-8] for path in _unit_directory(system).glob("kt-mcp*.service")
        )
        for name in sorted(names):
            _name(name)
            path = _unit_directory(system) / (name + ".service")
            status = _status(name, system)
            if status["LoadState"] == "not-found":
                try:
                    path.lstat()
                except FileNotFoundError:
                    # systemd can retain a stopped/failed unit after its file
                    # is removed. Only discard a record with no live binding.
                    if (
                        status.get("ActiveState") in {"inactive", "failed"}
                        and status.get("MainPID") == "0"
                        and status.get("FragmentPath") == ""
                        and status.get("DropInPaths") == ""
                        and status.get("ControlGroup") == ""
                        and status.get("NeedDaemonReload") == "no"
                    ):
                        continue
                    scope = "system" if system else "user"
                    raise ManagedServiceError(
                        f"Missing {scope} service {name!r} has an active or unresolved "
                        "state; inspect the service before updating"
                    ) from None
                except OSError as error:
                    raise _file_read_error(path, error) from None
            data, _ = _read_regular(path)
            _, executable, _ = _parse_unit(data, system)
            if _venv(executable) != target:
                continue
            binding = inspect_service(name, system, expected_python=python)
            if binding is not None:
                bindings.append(binding)
    return bindings


def restore_unit(
    binding: BoundService, *, expected_unit_bytes: bytes | None = None
) -> None:
    """Restore a snapshot only over its original or the caller's known replacement."""
    _linux()
    _authorize(binding)
    if hashlib.sha256(binding.unit_bytes).hexdigest() != binding.unit_hash:
        raise ManagedServiceError("The saved managed unit snapshot is invalid")
    allowed = [binding.unit_bytes]
    if expected_unit_bytes is not None:
        _parse_unit(expected_unit_bytes, binding.system)
        allowed.append(expected_unit_bytes)
    current, info = _read_regular(binding.unit_path)
    metadata = (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
    allowed_metadata = {
        (binding.unit_owner_uid, binding.unit_owner_gid, binding.unit_mode)
    }
    if current != binding.unit_bytes and current == expected_unit_bytes:
        # ServiceManager.install writes a new 0644 tempfile. Its group comes
        # from the setgid parent, if present, or the writing process otherwise.
        parent = binding.unit_path.parent.stat()
        created_gid = parent.st_gid if parent.st_mode & stat.S_ISGID else os.getegid()
        allowed_metadata.add((os.geteuid(), created_gid, 0o644))
    if current not in allowed or metadata not in allowed_metadata:
        raise ManagedServiceError(
            "Service unit changed outside this operation; not restoring it"
        )
    status = _status(binding.name, binding.system)
    if (
        status.get("LoadState") not in {"loaded", "error", "bad-setting"}
        or status.get("FragmentPath") != str(binding.unit_path)
        or status.get("DropInPaths") != ""
    ):
        raise ManagedServiceError("Loaded service binding changed; not restoring it")
    if current != binding.unit_bytes:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="." + binding.unit_path.name + ".", dir=binding.unit_path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(binding.unit_bytes)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), binding.unit_mode)
                os.fchown(
                    stream.fileno(), binding.unit_owner_uid, binding.unit_owner_gid
                )
            latest, latest_info = _read_regular(binding.unit_path)
            if latest != current or (
                latest_info.st_dev,
                latest_info.st_ino,
                latest_info.st_mtime_ns,
                latest_info.st_ctime_ns,
                latest_info.st_uid,
                latest_info.st_gid,
                latest_info.st_mode,
            ) != (
                info.st_dev,
                info.st_ino,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_uid,
                info.st_gid,
                info.st_mode,
            ):
                raise ManagedServiceError("Service unit changed during restoration")
            os.replace(temporary, binding.unit_path)
        finally:
            temporary.unlink(missing_ok=True)
    _run(binding.system, "daemon-reload")
