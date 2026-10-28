"""Transactional Linux updates, with a copied stdlib-only recovery worker."""

import argparse
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import managed_service as services
from . import releases
from .terminal_style import TerminalStyle

MARKER = "kohaku-kt-mcp installer v1"
TRANSACTION = ".kt-mcp-update"
WORKER_MODULES = ("update.py", "releases.py", "managed_service.py", "terminal_style.py")
WORKER_SOURCE = (
    "import sys\nfrom pathlib import Path\n"
    "root = Path(__file__).resolve().parent\n"
    "sys.path.insert(0, str(root))\n"
    "from runtime.update import worker_main\n"
    "raise SystemExit(worker_main(root, sys.argv[1:]))\n"
)
PHASES = {
    "preparing",
    "backing-up",
    "installing",
    "starting",
    "restoring",
    "restored",
    "committed",
}


class UpdateError(ValueError):
    """An update cannot proceed without changing unrelated state."""


@dataclass(frozen=True)
class Installation:
    prefix: Path
    python: Path
    base_python: Path
    version: str

    @property
    def venv(self):
        return self.prefix / "venv"


def _owned(path: Path, *, directory=False, private=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not kind(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & (0o077 if private else 0o022)
    ):
        raise UpdateError("The installation contains an unsafe or unowned path.")
    return info


def _run(command, *, environment=None, timeout=600, label="Update command", cwd=None):
    process = None
    try:
        process = subprocess.Popen(
            [str(part) for part in command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment or releases.clean_environment(),
            cwd=cwd,
            start_new_session=True,
        )
        output, _ = process.communicate(timeout=timeout)
    except BaseException:
        if process is not None:
            # A timed-out installer may leave pip writing into the venv. Reap the
            # entire process group before the caller is allowed to restore it.
            handlers = {
                value: signal.getsignal(value)
                for value in (signal.SIGINT, signal.SIGTERM)
            }
            for value in handlers:
                signal.signal(value, signal.SIG_IGN)
            try:
                for signum in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, signum)
                    except ProcessLookupError:
                        pass
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        continue
                process.wait(timeout=5)
            finally:
                for value, handler in handlers.items():
                    signal.signal(value, handler)
        raise
    if process.returncode:
        # pip output can include credentials from private index URLs.
        raise UpdateError(f"{label} failed.")
    return output.strip()


def _command(command, **options):
    """Translate transport failures without exposing command output or credentials."""
    try:
        return _run(command, **options)
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateError(
            f"{options.get('label', 'Update command')} could not complete."
        ) from None


def _validate_installation(installation: Installation):
    if sys.platform != "linux":
        raise UpdateError("Automatic updates require Linux.")
    if os.geteuid() == 0:
        raise UpdateError("Run kt-mcp update as the installing user, without sudo.")
    for path in (installation.prefix, installation.venv):
        _owned(path, directory=True)
    marker = installation.venv / ".kt-mcp-managed"
    _owned(marker)
    _owned(installation.venv / "pyvenv.cfg")
    if marker.read_text(encoding="utf-8").strip() != MARKER:
        raise UpdateError("This environment was not created by the kt-mcp installer.")
    if installation.python != installation.venv / "bin/python":
        raise UpdateError("The managed interpreter path is invalid.")
    if (
        not installation.python.is_file()
        or not os.access(installation.python, os.X_OK)
        or not installation.base_python.is_absolute()
        or installation.base_python.resolve().is_relative_to(installation.venv)
        or not os.access(installation.base_python, os.X_OK)
    ):
        raise UpdateError("The original Python interpreter is unavailable.")
    releases.version_text(installation.version)


def detect_installation(version: str) -> Installation:
    """Use the running environment, never today's XDG defaults or PATH Python."""
    venv = Path(sys.prefix).absolute()
    if venv.name != "venv" or sys.prefix == sys.base_prefix:
        raise UpdateError(
            "Automatic updates require an installation made by install.sh."
        )
    if venv.is_symlink():
        raise UpdateError("A symlinked managed venv cannot be updated automatically.")
    venv = venv.resolve()
    installation = Installation(
        venv.parent,
        venv / "bin/python",
        Path(sys._base_executable).resolve(),
        releases.version_text(version),
    )
    _validate_installation(installation)
    if not Path(__file__).resolve().is_relative_to(venv):
        raise UpdateError(
            "An editable/source checkout cannot update the installed CLI."
        )
    distribution = importlib.metadata.distribution("kohaku-kt-mcp")
    if distribution.version != version or not Path(
        distribution.locate_file("kt_mcp")
    ).resolve().is_relative_to(venv):
        raise UpdateError("The installed package metadata does not match this CLI.")
    direct = distribution.read_text("direct_url.json")
    if direct and json.loads(direct).get("dir_info", {}).get("editable"):
        raise UpdateError(
            "Editable installations must be updated through their checkout."
        )
    code = "import sys,venv,ensurepip; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    if (
        _command(
            [installation.base_python, "-I", "-c", code],
            timeout=30,
            label="Base Python/venv check",
        )
        != current
    ):
        raise UpdateError(
            "The original Python version changed; use a new install prefix."
        )
    return installation


def _say(message, *, color="cyan", bold=False):
    print(TerminalStyle().text(message, color=color, bold=bold), flush=True)


def _lock(prefix: Path):
    import fcntl

    path = prefix / ".kt-mcp-update.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise UpdateError("The update lock has unsafe ownership or permissions.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("Another kt-mcp update is already running.") from None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _write_state(directory: Path, state: dict):
    temporary = directory / "state.new"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / "state.json")
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_state(state):
    if (
        not isinstance(state, dict)
        or type(state.get("schema")) is not int
        or state["schema"] != 1
        or state.get("phase") not in PHASES
        or type(state.get("stop_requested")) is not bool
        or type(state.get("backup_ready")) is not bool
        or (
            state["phase"]
            in {"installing", "starting", "restoring", "restored", "committed"}
            and not state["backup_ready"]
        )
    ):
        raise UpdateError(
            "The update journal is invalid; saved files have been retained."
        )
    data = state.get("installation")
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), str)
        for key in ("prefix", "python", "base_python", "version")
    ):
        raise UpdateError(
            "The installation journal is incomplete; saved files have been retained."
        )
    for key in ("prefix", "python", "base_python"):
        if not Path(data[key]).is_absolute():
            raise UpdateError("The installation journal contains a relative path.")
    if Path(data["python"]) != Path(data["prefix"]) / "venv/bin/python":
        raise UpdateError("The installation journal does not identify its own venv.")
    releases.version_text(data["version"])
    record = state.get("service")
    if record is not None and (
        not isinstance(record, dict)
        or type(record.get("active")) is not bool
        or type(record.get("system")) is not bool
        or not isinstance(record.get("name"), str)
        or not isinstance(record.get("config"), str)
        or not isinstance(record.get("unit_hash"), str)
        or not isinstance(record.get("config_stamp"), list)
    ):
        raise UpdateError(
            "The service journal is incomplete; saved files have been retained."
        )


def recovery_argv(installation: Installation) -> list[str]:
    """Arguments for the independent worker; callers must validate before launching."""
    return [
        str(installation.base_python),
        "-I",
        str(installation.prefix / TRANSACTION / "worker.py"),
        "--recover",
    ]


def recovery_command(installation: Installation):
    return shlex.join(recovery_argv(installation))


def _recovery_bytes(path: Path, *, private=False, limit=1048576) -> bytes:
    expected = _owned(path, private=private)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        actual = os.fstat(stream.fileno())
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise UpdateError("Recovery files changed while being inspected; retry.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("A recovery file exceeds the supported size.")
    return data


def _managed_environment_marker(path: Path) -> bool:
    try:
        _owned(path, directory=True)
        _owned(path / "pyvenv.cfg")
        return (
            _recovery_bytes(path / ".kt-mcp-managed", limit=4096)
            .decode("utf-8")
            .strip()
            == MARKER
        )
    except (OSError, UnicodeError, UpdateError):
        return False


def recoverable_installation(prefix: Path | None = None) -> Installation:
    """Validate a retained private worker without requiring working package metadata.

    An already-running TUI may pass its captured installation prefix if the
    installed CLI was damaged. The worker still takes its exclusive lock and
    validates the current journal before it changes any files or services.
    """
    if sys.platform != "linux" or os.geteuid() == 0:
        raise UpdateError("Recovery requires the ordinary installing Linux account.")
    if prefix is None:
        venv = Path(sys.prefix).absolute()
        if (
            venv.name != "venv"
            or sys.prefix == sys.base_prefix
            or venv.is_symlink()
            or not Path(__file__).resolve().is_relative_to(venv.resolve())
        ):
            raise UpdateError("Recovery requires the original managed installation.")
        prefix = venv.parent
    prefix = Path(prefix).expanduser().absolute()
    _owned(prefix, directory=True)
    prefix = prefix.resolve()
    directory = prefix / TRANSACTION
    if not directory.exists() and not directory.is_symlink():
        raise UpdateError("No unfinished update was found for this installation.")
    _owned(directory, directory=True, private=True)
    state = json.loads(_recovery_bytes(directory / "state.json", private=True))
    _validate_state(state)
    data = state["installation"]
    installation = Installation(
        Path(data["prefix"]),
        Path(data["python"]),
        Path(data["base_python"]),
        data["version"],
    )
    if installation.prefix != prefix:
        raise UpdateError("The recovery journal belongs to a different installation.")
    if (
        not installation.base_python.is_file()
        or not os.access(installation.base_python, os.X_OK)
        or installation.base_python.is_relative_to(installation.venv)
        or installation.base_python.resolve().is_relative_to(installation.venv)
    ):
        raise UpdateError("The original recovery Python is unavailable.")
    runtime = directory / "runtime"
    _owned(runtime, directory=True, private=True)
    for filename in ("__init__.py", *WORKER_MODULES):
        _owned(runtime / filename)
    if _recovery_bytes(directory / "worker.py", limit=65536) != WORKER_SOURCE.encode(
        "utf-8"
    ):
        raise UpdateError(
            "The saved recovery entry is invalid; retained files are unchanged."
        )
    original_available = _managed_environment_marker(installation.venv)
    backup_available = state["backup_ready"] and _managed_environment_marker(
        directory / "backup"
    )
    if not (original_available or backup_available):
        raise UpdateError(
            "No managed environment or complete backup is available for recovery."
        )
    return installation


def _handoff(installation: Installation, release: releases.Release, name: str | None):
    """Exec a private copy under base Python before any installed file is replaced."""
    descriptor = _lock(installation.prefix)
    directory = installation.prefix / TRANSACTION
    created = False
    try:
        if directory.exists() or directory.is_symlink():
            raise UpdateError(
                "An unfinished update exists. Recover it with:\n  "
                + recovery_command(installation)
            )
        directory.mkdir(mode=0o700)
        created = True
        runtime = directory / "runtime"
        runtime.mkdir(mode=0o700)
        (runtime / "__init__.py").write_text("", encoding="utf-8")
        for filename in WORKER_MODULES:
            shutil.copyfile(Path(__file__).with_name(filename), runtime / filename)
        (directory / "worker.py").write_text(WORKER_SOURCE, encoding="utf-8")
        state = {
            "schema": 1,
            "installation": {
                key: str(value) for key, value in asdict(installation).items()
            },
            "release": asdict(release),
            "name": name,
            "phase": "preparing",
            "service": None,
            "stop_requested": False,
            "backup_ready": False,
        }
        _write_state(directory, state)
        os.set_inheritable(descriptor, True)
        command = [
            str(installation.base_python),
            "-I",
            str(directory / "worker.py"),
            "--lock-fd",
            str(descriptor),
        ]
        os.execve(command[0], command, releases.clean_environment())
    except BaseException:
        if created:
            shutil.rmtree(directory)
        os.close(descriptor)
        raise


def _config_stamp(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise UpdateError("The service configuration is not a regular file.")
    return [releases.sha256(path), info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]


def _busy(binding):
    state = binding.status.get("ActiveState")
    if state not in {"active", "inactive", "failed"}:
        raise UpdateError("A service is changing state; retry after it settles.")
    return state == "active" or binding.status.get("MainPID") != "0"


def _select_service(installation: Installation, name):
    bindings = services.find_services(installation.python)
    active = [binding for binding in bindings if _busy(binding)]
    if any(binding.system for binding in active):
        raise UpdateError(
            "This installation serves an active system unit. Stop it with the system administrator before updating."
        )
    users = [binding for binding in bindings if not binding.system]
    if name is not None:
        selected = next((binding for binding in users if binding.name == name), None)
        if selected is None:
            raise UpdateError(
                "The selected user service does not use this installation."
            )
    elif len(active) == 1:
        selected = active[0]
    elif len(users) == 1:
        selected = users[0]
    else:
        selected = None
    if any(binding is not selected for binding in active):
        raise UpdateError(
            "Several services share this installation. Stop the other instances first; use --name for the service to restart."
        )
    for binding in bindings:
        if binding.config_path.resolve().is_relative_to(installation.venv):
            raise UpdateError(
                "Move the service configuration outside the installed venv before updating."
            )
        config = json.loads(binding.config_path.read_bytes())
        if Path(config["workspace"]).resolve().is_relative_to(installation.venv):
            raise UpdateError(
                "Move the MCP workspace outside the installed venv before updating."
            )
    if selected:
        services.assert_external_process(selected)
    return selected


def _service_record(binding):
    if binding is None:
        return None
    return {
        "name": binding.name,
        "system": binding.system,
        "config": str(binding.config_path),
        "unit_hash": binding.unit_hash,
        "config_stamp": _config_stamp(binding.config_path),
        "active": _busy(binding),
        "pid": binding.status["MainPID"],
        "enabled": binding.status.get("UnitFileState"),
    }


def _current_service(installation, record):
    if record is None:
        return None
    binding = services.inspect_service(
        record["name"],
        record["system"],
        expected_python=installation.python,
        expected_config=Path(record["config"]),
    )
    if (
        binding is None
        or binding.unit_hash != record["unit_hash"]
        or _config_stamp(binding.config_path) != record["config_stamp"]
        or binding.status.get("UnitFileState") != record["enabled"]
    ):
        raise UpdateError(
            "Service configuration or registration changed during the update."
        )
    return binding


def _offline_environment(wheelhouse):
    environment = releases.clean_environment()
    # Do not inherit hidden pip routing/options during the actual installation.
    for key in list(environment):
        if key.startswith("PIP_"):
            environment.pop(key)
    environment.update(
        PIP_CONFIG_FILE=os.devnull,
        PIP_NO_INDEX="1",
        PIP_FIND_LINKS=Path(wheelhouse).absolute().as_uri(),
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_NO_INPUT="1",
    )
    return environment


def _install(installation, bundle, wheelhouse, *, prefix=None):
    _command(
        [
            "bash",
            bundle / "install.sh",
            "--prefix",
            prefix or installation.prefix,
            "--python",
            installation.base_python,
            "--no-link",
            "--source",
            next(bundle.glob("kohaku_kt_mcp-*.whl")),
        ],
        environment=_offline_environment(wheelhouse),
        label="Offline package installation",
        cwd=bundle,
    )


def _verify(python, version, *, environment=None):
    actual = _command(
        [python, "-I", "-m", "kt_mcp", "--version"],
        environment=environment,
        timeout=30,
        label="Installed CLI check",
    )
    if actual != f"Kohaku KT MCP {version}":
        raise UpdateError("The installed CLI version does not match the release.")
    _command(
        [python, "-I", "-c", "import kt_mcp.daemon,kt_mcp.server,kt_mcp.runtime"],
        environment=environment,
        timeout=30,
        label="Application import check",
    )
    _command(
        [python, "-I", "-m", "pip", "check"],
        environment=environment,
        timeout=60,
        label="Dependency compatibility check",
    )


def _prepare(installation, release, directory):
    downloads = directory / "downloads"
    downloads.mkdir()
    bundle = releases.download_release(release, downloads)
    wheelhouse = directory / "wheels"
    wheelhouse.mkdir()
    environment = releases.clean_environment()
    environment.update(PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    _say("Preparing packages · service keeps running")
    _command(
        [
            installation.python,
            "-I",
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            wheelhouse,
            "--",
            bundle / release.wheel_name,
        ],
        environment=environment,
        label="Dependency download",
    )
    trial = directory / "trial"
    trial.mkdir()
    shutil.copytree(installation.venv, trial / "venv", symlinks=True)
    _install(installation, bundle, wheelhouse, prefix=trial)
    _verify(
        trial / "venv/bin/python",
        release.version,
        environment=_offline_environment(wheelhouse),
    )
    shutil.rmtree(trial)
    return bundle, wheelhouse


def _rollback(installation, directory, state):
    """Restore the whole environment at its original absolute path, then its service."""
    record = state.get("service")
    phase = state["phase"]
    if phase not in PHASES:
        raise UpdateError("Unknown recovery phase; saved files have been retained.")
    if phase == "committed":
        return
    if state.get("backup_ready") and phase in {"installing", "starting", "restoring"}:
        for binding in services.find_services(installation.python):
            if _busy(binding) and (
                record is None
                or binding.system != record["system"]
                or binding.name != record["name"]
            ):
                raise UpdateError(
                    "Another service is using this environment; stop it before recovery."
                )
        # Also handle a service that somebody started during installation.
        if record is not None:
            binding = _current_service(installation, record)
            services.stop_service(binding)
        state["phase"] = "restoring"
        _write_state(directory, state)
        backup = directory / "backup"
        if backup.exists():
            if installation.venv.exists():
                failed = directory / "failed-venv"
                if failed.exists() or failed.is_symlink():
                    raise UpdateError(
                        "A previous recovery needs inspection; the saved environment is retained."
                    )
                os.replace(installation.venv, failed)
            os.replace(backup, installation.venv)
        _verify(installation.python, installation.version)
        state["phase"] = "restored"
        _write_state(directory, state)
    if record is not None and record["active"] and state.get("stop_requested"):
        binding = _current_service(installation, record)
        services.start_service(binding, expected_version=installation.version)


def _apply(installation, release, directory, state):
    selected = _select_service(installation, state.get("name"))
    state["service"] = _service_record(selected)
    _write_state(directory, state)
    bundle, wheelhouse = _prepare(installation, release, directory)
    # Downloads/validation may take time. Rebind before pausing anything.
    latest = _select_service(installation, state.get("name"))
    if _service_record(latest) != state["service"]:
        raise UpdateError(
            "The service changed while packages were prepared; retry the update."
        )
    _validate_installation(installation)
    if latest is not None and state["service"]["active"]:
        _say("Pausing MCP · installing verified update", color="yellow")
        state["stop_requested"] = True
        _write_state(directory, state)
        services.stop_service(latest)
    else:
        _say("Installing verified update · service stays stopped")
    state["phase"] = "backing-up"
    _write_state(directory, state)
    shutil.copytree(installation.venv, directory / "backup", symlinks=True)
    state["backup_ready"] = True
    if any(_busy(binding) for binding in services.find_services(installation.python)):
        raise UpdateError(
            "A service started during backup; no installed files were replaced."
        )
    _current_service(installation, state["service"])
    state["phase"] = "installing"
    _write_state(directory, state)
    _install(installation, bundle, wheelhouse)
    _verify(
        installation.python,
        release.version,
        environment=_offline_environment(wheelhouse),
    )
    if state["service"] is not None:
        latest = _current_service(installation, state["service"])
        if _busy(latest):
            raise UpdateError("The service was started externally during installation.")
        if state["service"]["active"]:
            state["phase"] = "starting"
            _write_state(directory, state)
            _say("Starting MCP · checking connection")
            services.start_service(latest, expected_version=release.version)
    state["phase"] = "committed"
    _write_state(directory, state)


def _cleanup(directory: Path):
    # Keep the working recovery entry and journal until all potentially large
    # or read-only environment copies have been removed successfully.
    control = {"runtime", "worker.py", "state.json", "state.new"}
    for path in directory.iterdir():
        if path.name in control:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    shutil.rmtree(directory)


def worker_main(directory: Path, argv) -> int:
    """Private worker entry; recoverable even if the installed package is broken."""
    parser = argparse.ArgumentParser(description="Recover an interrupted kt-mcp update")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--lock-fd", type=int)
    arguments = parser.parse_args(argv)
    descriptor = None
    state = None
    installation = None
    journal_valid = False
    previous_cwd = Path.cwd()
    previous_signals = {
        value: signal.getsignal(value) for value in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        if not directory.is_absolute() or directory.name != TRANSACTION:
            raise UpdateError("The recovery directory is invalid.")
        _owned(directory.parent, directory=True)
        if arguments.lock_fd is None:
            descriptor = _lock(directory.parent)
        else:
            import fcntl

            actual = os.fstat(arguments.lock_fd)
            expected = (directory.parent / ".kt-mcp-update.lock").lstat()
            if (
                (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
                or not stat.S_ISREG(actual.st_mode)
                or actual.st_uid != os.geteuid()
                or actual.st_mode & 0o077
            ):
                raise UpdateError("The inherited update lock is invalid.")
            fcntl.flock(arguments.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            descriptor = arguments.lock_fd
        # Read the journal only after locking: another worker may have just
        # completed and removed its backup while this recovery was launching.
        _owned(directory, directory=True, private=True)
        os.chdir(directory)
        _owned(directory / "state.json", private=True)
        state = json.loads((directory / "state.json").read_bytes())
        _validate_state(state)
        data = state["installation"]
        installation = Installation(
            Path(data["prefix"]),
            Path(data["python"]),
            Path(data["base_python"]),
            data["version"],
        )
        if directory != installation.prefix / TRANSACTION:
            raise UpdateError("The update journal is in a different installation.")
        journal_valid = True

        def interrupted(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
        if arguments.recover:
            _say("Recovering the previous installation", color="yellow")
            _rollback(installation, directory, state)
        else:
            if state["phase"] != "preparing":
                raise UpdateError(
                    "This update was interrupted; restoring the previous installation."
                )
            _validate_installation(installation)
            release = releases.Release(**state["release"])
            _apply(installation, release, directory, state)
        _cleanup(directory)
        if arguments.recover:
            _say("Recovery complete.", color="green", bold=True)
        else:
            suffix = (
                "MCP is running."
                if state["service"] and state["service"]["active"]
                else "Service remains stopped."
            )
            _say(
                f"Updated {installation.version} → {release.version}. {suffix}",
                color="green",
                bold=True,
            )
        return 0
    # This transaction boundary must attempt recovery on every failure.
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        message = (
            str(error)
            if isinstance(
                error,
                (UpdateError, releases.ReleaseError, services.ManagedServiceError),
            )
            else (
                "Update interrupted."
                if isinstance(error, KeyboardInterrupt)
                else "Update could not complete."
            )
        )
        _say(message, color="yellow")
        if journal_valid:
            try:
                _rollback(installation, directory, state)
                _cleanup(directory)
            # Keep the backup and helper if any recovery step fails.
            except (Exception, KeyboardInterrupt):  # noqa: BLE001
                _say(
                    (
                        "Update completed; cleanup needs attention. Run:\n  "
                        if state["phase"] == "committed"
                        else "Recovery needs attention. Saved files are retained. Run:\n  "
                    )
                    + recovery_command(installation),
                    color="yellow",
                )
            else:
                if state["phase"] == "committed":
                    result = "Update completed; leftover files cleaned."
                elif (
                    state["phase"] == "preparing"
                    and not state["stop_requested"]
                    and not state["backup_ready"]
                ):
                    result = (
                        "Update stopped before installation; "
                        "installed files and services are unchanged."
                    )
                else:
                    result = "Previous installation retained; original service state restored."
                _say(result, color="yellow")
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.chdir(previous_cwd)
        for value, handler in previous_signals.items():
            signal.signal(value, handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Update this Linux installation and resume its running user service."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for a release without installing or stopping MCP",
    )
    parser.add_argument(
        "--version",
        metavar="X.Y.Z",
        help="Choose a stable version; downgrades are refused",
    )
    parser.add_argument(
        "--name", help="User service name, if this installation has multiple instances"
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Recover an interrupted update using its retained independent worker",
    )
    arguments = parser.parse_args(argv)
    if arguments.recover and (arguments.check or arguments.version or arguments.name):
        parser.error("--recover cannot be combined with --check, --version, or --name")
    try:
        if arguments.recover:
            installation = recoverable_installation()
            command = recovery_argv(installation)
            os.execve(command[0], command, releases.clean_environment())
            return 0
        from kt_mcp import __version__

        requested = (
            releases.version_text(arguments.version) if arguments.version else None
        )
        if arguments.name and (
            len(arguments.name) > 80
            or not re.fullmatch(r"kt-mcp(?:-[A-Za-z0-9_-]+)?", arguments.name)
        ):
            raise UpdateError("Use a service name such as kt-mcp or kt-mcp-dev.")
        installation = None if arguments.check else detect_installation(__version__)
        _say("Checking for updates…", bold=True)
        with tempfile.TemporaryDirectory(prefix="kt-mcp-release-") as temporary:
            release = releases.latest_release(Path(temporary), requested)
        current, target = releases.version_key(__version__), releases.version_key(
            release.version
        )
        if target <= current:
            if requested and target < current:
                raise UpdateError("Downgrades are not supported by automatic update.")
            _say(f"Up to date · {__version__}", color="green", bold=True)
            return 0
        _say(f"{__version__} → {release.version}", bold=True)
        if arguments.check:
            _say("Run kt-mcp update to install.")
            return 0
        _handoff(installation, release, arguments.name)
    except (UpdateError, releases.ReleaseError, services.ManagedServiceError) as error:
        print(f"Update: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError):
        print(
            "Update: could not inspect this installation; installed files are unchanged.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Update cancelled; installed files are unchanged.", file=sys.stderr)
        return 130
    return 0
