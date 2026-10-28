"""Install and control a managed systemd service without a terminal owner."""

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from kt_mcp.daemon import default_config_path

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
)
COMMAND_TIMEOUT = 40


class ServiceError(RuntimeError):
    """An external service-manager operation failed."""


def _linux():
    if sys.platform != "linux":
        raise ValueError("service management requires Linux with systemd")


def _absolute(path) -> Path:
    # Resolving symlinks here would turn a venv Python into the system Python.
    return Path(os.path.abspath(Path(path).expanduser()))


def _account(system: bool, run_as: str | None):
    _linux()
    import pwd

    if system:
        if not run_as:
            raise ValueError("system installation requires --run-as USER")
        try:
            account = pwd.getpwnam(run_as)
        except KeyError:
            raise ValueError("--run-as must name an existing non-root user") from None
    else:
        if run_as is not None:
            raise ValueError("--run-as is only available with --system")
        account = pwd.getpwuid(os.geteuid())
    if account.pw_uid == 0:
        raise ValueError("MCP services must run as a non-root user")
    if not Path(account.pw_dir).is_absolute():
        raise ValueError("the service account must have an absolute home directory")
    return account


def _literal(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("systemd values must not contain control characters")
    return value.replace("%", "%%")


def _quote(value: str) -> str:
    _literal(value)
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + value + '"'


def render_unit(
    config_path: Path,
    *,
    system: bool = False,
    run_as: str | None = None,
    python: str | Path | None = None,
) -> str:
    """Render a token-free unit whose process runs with ordinary user privileges."""
    account = _account(system, run_as)
    config_path = _absolute(config_path)
    info = config_path.stat()
    if info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("config must be mode 600 and owned by the service account")
    from kt_mcp.instances import read_config

    config = read_config(config_path)
    executable = _absolute(python if python is not None else sys.executable)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("the service Python executable must exist and be executable")
    launcher = Path("/usr/bin/env")
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError(
            "service installation requires /usr/bin/env (Ubuntu coreutils)"
        )
    workspace = _absolute(config.settings.workspace)
    path = (
        str(executable.parent)
        + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    # systemd forbids some characters in its executable path. env execs the
    # literal venv path without a shell and preserves the service main PID.
    # The colon prefix also disables variable expansion in every argument.
    command = ":" + " ".join(
        _quote(str(argument))
        for argument in (
            launcher,
            "--",
            executable,
            "-m",
            "kt_mcp",
            "daemon",
            "--config",
            config_path,
        )
    )
    lines = [
        MANAGED_MARKER,
        "[Unit]",
        "Description=Kohaku KT MCP workspace service",
        "",
        "[Service]",
        "Type=exec",
    ]
    if system:
        lines.extend([f"User={_literal(account.pw_name)}", f"Group={account.pw_gid}"])
    lines.extend(
        [
            f"WorkingDirectory={_literal(str(workspace))}",
            f"ExecStart={command}",
            f"Environment={_quote('HOME=' + account.pw_dir)}",
            f"Environment={_quote('PATH=' + path)}",
            "Environment=PYTHONUNBUFFERED=1",
            "Restart=on-failure",
            "RestartSec=5",
            "TimeoutStopSec=30",
            "KillMode=control-group",
            "UMask=0077",
            "NoNewPrivileges=true",
            "",
            "[Install]",
            "WantedBy=" + ("multi-user.target" if system else "default.target"),
            "",
        ]
    )
    return "\n".join(lines)


def _failure(arguments: list[str], result) -> ServiceError:
    executable = Path(arguments[0]).name
    details = (result.stderr or "").lower()
    if (
        "failed to connect to bus" in details
        or "not been booted with systemd" in details
    ):
        reason = "systemd is unavailable; check the system and current user session"
    elif (
        "access denied" in details
        or "authentication" in details
        or "permission" in details
    ):
        reason = "permission denied; use an authorized account (no automatic sudo)"
    else:
        reason = "check service status, logs, and account permissions"
    # Never echo arbitrary external output, command environments, or credentials.
    return ServiceError(f"{executable} failed (exit {result.returncode}): {reason}")


def _run(arguments: list[str], *, allow_not_found: bool = False) -> str:
    try:
        result = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        raise ServiceError(f"{Path(arguments[0]).name} is not installed") from None
    except subprocess.TimeoutExpired:
        raise ServiceError(
            f"{Path(arguments[0]).name} timed out after {COMMAND_TIMEOUT} seconds"
        ) from None
    if result.returncode and not (
        allow_not_found and "LoadState=not-found" in result.stdout.splitlines()
    ):
        raise _failure(arguments, result)
    return result.stdout


class ServiceManager:
    """Own only marked kt-mcp units; querying status never changes the service."""

    def __init__(self, name: str = "kt-mcp", system: bool = False):
        _linux()
        if not isinstance(name, str):
            raise TypeError("service name must be a string")
        if len(name) > 80 or not re.fullmatch(r"kt-mcp(?:-[A-Za-z0-9_-]+)?", name):
            raise ValueError(
                "name must be kt-mcp or kt-mcp-SUFFIX, at most 80 characters"
            )
        self.name = name
        self.system = system

    @property
    def unit_name(self) -> str:
        return self.name + ".service"

    @property
    def unit_path(self) -> Path:
        directory = (
            Path("/etc/systemd/system")
            if self.system
            else default_config_path().parents[1] / "systemd/user"
        )
        return directory / self.unit_name

    def _mutating(self):
        _linux()
        if self.system and os.geteuid() != 0:
            raise PermissionError(
                "system service changes require root; no automatic sudo"
            )
        if not self.system and os.geteuid() == 0:
            raise PermissionError("run user service changes as a non-root user")

    def _systemctl(self, *arguments: str, allow_not_found: bool = False) -> str:
        command = ["systemctl", "--no-ask-password", "--no-pager"]
        if not self.system:
            command.append("--user")
        return _run(command + list(arguments), allow_not_found=allow_not_found)

    def _existing(self) -> str | None:
        path = self.unit_path
        if path.is_symlink():
            raise ValueError("refusing to manage a symlinked or masked unit")
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError("managed unit path must be a regular file")
        content = path.read_text(encoding="utf-8")
        if not content.startswith(MANAGED_MARKER + "\n"):
            raise ValueError("refusing to modify a unit not managed by kt-mcp")
        return content

    def _managed(self) -> str:
        content = self._existing()
        if content is None:
            raise FileNotFoundError(
                "managed unit is not installed; run service install"
            )
        return content

    def install(
        self,
        config_path: Path,
        *,
        run_as: str | None = None,
        enable: bool = False,
        now: bool = False,
        linger: bool = False,
        replace: bool = False,
    ):
        self._mutating()
        if linger and self.system:
            raise ValueError("--linger is only available for user services")
        content = render_unit(config_path, system=self.system, run_as=run_as)
        if not self.system:
            from kt_mcp.instances import ensure_available, read_config

            ensure_available(read_config(config_path), exclude=Path(config_path))
        previous = self._existing()
        if previous is not None and previous != content and not replace:
            raise FileExistsError("managed unit differs; use --replace to update it")
        if previous != content:
            path = self.unit_path
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix="." + path.name, dir=path.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.chmod(0o644)
                if self._existing() != previous:
                    raise ValueError(
                        "unit changed during installation; inspect and retry"
                    )
                if previous is None:
                    os.link(temporary, path)
                else:
                    os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        self._systemctl("daemon-reload")
        if linger:
            self.enable_linger()
        if enable or now:
            self.control("enable", now=now)

    def control(self, action: str, *, now: bool = False):
        if action not in {"start", "stop", "restart", "enable", "disable"}:
            raise ValueError("unsupported service control action")
        if now and action not in {"enable", "disable"}:
            raise ValueError("--now is only available with enable or disable")
        self._mutating()
        content = self._managed()
        if not self.system and (
            action in {"start", "restart"} or (action == "enable" and now)
        ):
            from kt_mcp.instances import ensure_available, read_config
            from kt_mcp.managed_service import ManagedServiceError, _parse_unit

            try:
                _, _, config_path = _parse_unit(content.encode("utf-8"), False)
            except ManagedServiceError:
                raise ValueError(
                    "Cannot identify this managed service configuration."
                ) from None
            active = self.status().get("ActiveState")
            ensure_available(
                read_config(config_path),
                exclude=config_path,
                check_socket=active not in {"active", "activating", "reloading"},
            )
        arguments = [action]
        if now:
            arguments.append("--now")
        self._systemctl(*arguments, self.unit_name)

    def status(self) -> dict[str, str]:
        output = self._systemctl(
            "show",
            "--property=" + ",".join(STATUS_PROPERTIES),
            self.unit_name,
            allow_not_found=True,
        )
        values = dict.fromkeys(STATUS_PROPERTIES, "")
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in values:
                values[key] = value
        return values

    def _journal_arguments(self, lines: int) -> list[str]:
        if type(lines) is not int or not 1 <= lines <= 1000:
            raise ValueError("log lines must be an integer from 1 to 1000")
        command = ["journalctl", "--no-pager", "--quiet", "--output=short-iso"]
        if not self.system:
            command.append("--user")
        return command + ["--unit", self.unit_name, "--lines", str(lines)]

    def logs(self, lines: int = 80) -> str:
        return _run(self._journal_arguments(lines))

    def uninstall(self):
        self._mutating()
        previous = self._managed()
        self._systemctl("disable", "--now", self.unit_name)
        if self._managed() != previous:
            raise ValueError("unit changed during removal; inspect and retry")
        self.unit_path.unlink()
        self._systemctl("daemon-reload")

    def enable_linger(self):
        if self.system:
            raise ValueError("linger is only available for user services")
        self._mutating()
        account = _account(False, None)
        _run(["loginctl", "--no-ask-password", "enable-linger", account.pw_name])

    def linger_status(self) -> str:
        if self.system:
            return "not-required"
        return (
            _run(
                [
                    "loginctl",
                    "--no-ask-password",
                    "show-user",
                    str(os.geteuid()),
                    "--property=Linger",
                    "--value",
                ]
            ).strip()
            or "unknown"
        )

    def follow_logs(self, lines: int = 80):
        """Explicit interactive follow; finite log queries use logs instead."""
        command = self._journal_arguments(lines) + ["--follow"]
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise ServiceError("journalctl is not installed") from None
        try:
            while True:
                try:
                    code = process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if code:
                raise ServiceError(f"journalctl exited with code {code}")
        except KeyboardInterrupt:
            pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kt-mcp service")
    parser.add_argument(
        "action",
        choices=(
            "install",
            "uninstall",
            "start",
            "stop",
            "restart",
            "status",
            "logs",
            "enable",
            "disable",
            "linger",
        ),
    )
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--name", default="kt-mcp")
    parser.add_argument("--system", action="store_true")
    parser.add_argument("--run-as")
    parser.add_argument("--now", action="store_true")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--linger", action="store_true")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--lines", type=int, default=80)
    parser.add_argument("--follow", action="store_true")
    args = parser.parse_args(argv)
    try:
        manager = ServiceManager(args.name, args.system)
        if args.action != "install" and (
            args.run_as is not None or args.enable or args.linger or args.replace
        ):
            raise ValueError("--run-as/--enable/--linger/--replace require install")
        if args.now and args.action not in {"install", "enable", "disable"}:
            raise ValueError("--now requires install, enable, or disable")
        if args.follow and args.action != "logs":
            raise ValueError("--follow requires logs")
        if args.action == "install":
            manager.install(
                args.config,
                run_as=args.run_as,
                enable=args.enable,
                now=args.now,
                linger=args.linger,
                replace=args.replace,
            )
            print(f"Registered {manager.unit_path}")
        elif args.action == "uninstall":
            manager.uninstall()
            print(f"Removed {manager.unit_name}; configuration retained")
        elif args.action == "status":
            print(json.dumps(manager.status(), ensure_ascii=False, indent=2))
        elif args.action == "logs":
            if args.follow:
                manager.follow_logs(args.lines)
            else:
                print(manager.logs(args.lines), end="")
        elif args.action == "linger":
            manager.enable_linger()
            print("Enabled linger for the current user")
        else:
            manager.control(args.action, now=args.now)
            print(f"{args.action}: {manager.unit_name}")
    except (ValueError, TypeError, OSError, ServiceError) as exc:
        parser.error(str(exc))
    return 0
