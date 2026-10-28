"""Checked deletion of one instance, shared by the CLI and terminal interface."""

import json
import os
import stat
import sys
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from kt_mcp import instances, managed_service
from kt_mcp.onboarding import _ConfigSnapshot, _file_identity, _snapshot
from kt_mcp.service import ServiceManager


@dataclass(frozen=True)
class DeletePlan:
    name: str
    system: bool
    config_path: Path
    workspace: str
    has_service: bool
    _config: _ConfigSnapshot | None = field(repr=False)
    _binding: managed_service.BoundService | None = field(repr=False)
    _unit_identity: tuple | None = field(repr=False)
    _owner_uid: int = field(repr=False)

    @property
    def summary(self):
        lines = [f"Instance: {self.name}"]
        if self.has_service:
            lines.append(
                "Stop, disable automatic startup, and unregister this service."
            )
        else:
            lines.append("No service is installed for this instance.")
        lines.extend(
            [
                "Delete its saved configuration, credentials, and local startup record.",
                "Keep all workspace files. Other instances are unchanged.",
                "This deletion cannot be undone.",
                f"Configuration: {self.config_path}",
                "Workspace: " + (self.workspace or "(configuration unavailable)"),
            ]
        )
        return "\n".join(_display(line) for line in lines)


def _display(value):
    return "".join(
        character if character.isprintable() else "?" for character in str(value)
    )


def _saved(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if info.st_size > instances.MAX_FILE_BYTES:
        raise instances.InstanceError(
            "The instance file is too large to verify for deletion."
        )
    return _snapshot(path)


def _unit_path(name, system):
    return managed_service._unit_directory(system) / (name + ".service")


def _binding(name, system, path):
    unit = _unit_path(name, system)
    if not unit.exists() and not unit.is_symlink():
        return None
    return managed_service.inspect_removal_service(
        name, system, expected_python=sys.executable, expected_config=path
    )


def _same_path(one, two):
    return instances._path_key(Path(one)) == instances._path_key(Path(two))


def _overlapping_files(one, two):
    first = {
        instances._path_key(Path(one)),
        instances._path_key(Path(one).with_suffix(".status.json")),
    }
    second = {
        instances._path_key(Path(two)),
        instances._path_key(Path(two).with_suffix(".status.json")),
    }
    return bool(first & second)


def _assert_unique(name, system, path, owner_uid, *, check_catalog=True):
    if not system and check_catalog:
        for item in instances.list_instances():
            if item.name == name and not _same_path(item.config_path, path):
                raise instances.InstanceError(
                    "Several configurations use this service name; deletion is ambiguous."
                )
            if item.name != name and _overlapping_files(item.config_path, path):
                raise instances.InstanceError(
                    "Another instance shares this configuration or startup record; it was not deleted."
                )
    directories = {managed_service._unit_directory(False)}
    if sys.platform == "linux":
        directories.add(managed_service._unit_directory(True))
        import pwd

        directories.add(Path(pwd.getpwuid(owner_uid).pw_dir) / ".config/systemd/user")
        if system:
            directories.update(_user_manager_directories(owner_uid))
        # A custom XDG catalog may belong to the ordinary service account.
        base = path.parent.parent if path.parent.name == "instances" else path.parent
        if base.name == "kt-mcp":
            directories.add(base.parent / "systemd/user")
    selected_unit = _unit_path(name, system)
    for directory in directories:
        if not directory.is_dir() or directory.is_symlink():
            continue
        for unit in directory.glob("kt-mcp*.service"):
            if _same_path(unit, selected_unit):
                continue
            try:
                info = unit.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                    continue
                data = unit.read_bytes()
                is_system = directory == managed_service._unit_directory(True)
                _, _, other_path = managed_service._parse_unit(data, is_system)
            except (OSError, ValueError, managed_service.ManagedServiceError):
                continue
            if _overlapping_files(path, other_path):
                raise instances.InstanceError(
                    "Another service shares this configuration or startup record; uninstall that binding before deleting it."
                )


def _process_paths(process, owner_uid):
    """Read only the process routing variables, without exposing other values."""
    import pwd

    environment = {}
    for entry in (process / "environ").read_bytes().split(b"\0"):
        key, separator, value = entry.partition(b"=")
        if separator and key in {b"HOME", b"XDG_CONFIG_HOME"}:
            environment[os.fsdecode(key)] = os.fsdecode(value)
    home = Path(environment.get("HOME") or pwd.getpwuid(owner_uid).pw_dir)
    base = Path(environment.get("XDG_CONFIG_HOME") or home / ".config")
    if not base.is_absolute():
        base = home / ".config"
    return home, base


def _user_manager_directories(owner_uid):
    """Include a live user manager catalog when deleting from a system panel."""
    directories = {Path("/run/user") / str(owner_uid) / "systemd/user"}
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != owner_uid:
                continue
            arguments = [
                os.fsdecode(word)
                for word in (process / "cmdline").read_bytes().split(b"\0")
                if word
            ]
            if (
                arguments
                and Path(arguments[0]).name == "systemd"
                and "--user" in arguments
            ):
                _, base = _process_paths(process, owner_uid)
                directories.add(base / "systemd/user")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise instances.InstanceError(
                "Cannot verify the service account catalog; deletion was not started."
            ) from None
    return directories


def _matching_daemons(path, owner_uid):
    """Find this configuration even before a foreground daemon writes its status."""
    if sys.platform != "linux":
        return set()
    result = set()
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != owner_uid:
                continue
            words = (process / "cmdline").read_bytes().split(b"\0")
            arguments = [os.fsdecode(word) for word in words if word]
            if "daemon" not in arguments or not any(
                word == "kt_mcp" or Path(word).name == "kt-mcp" for word in arguments
            ):
                continue
            tail = arguments[arguments.index("daemon") + 1 :]
            value = None
            for index, word in enumerate(tail):
                if word == "--config" and index + 1 < len(tail):
                    value = tail[index + 1]
                elif word.startswith("--config="):
                    value = word.split("=", 1)[1]
            if value is None:
                _, base = _process_paths(process, owner_uid)
                candidate = base / "kt-mcp/config.json"
            else:
                if value == "~" or value.startswith("~/"):
                    home, _ = _process_paths(process, owner_uid)
                    candidate = home / value[2:] if value != "~" else home
                else:
                    candidate = Path(value).expanduser()
                if not candidate.is_absolute():
                    candidate = (process / "cwd").resolve() / candidate
            if _overlapping_files(candidate, path):
                result.add(int(process.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise instances.InstanceError(
                "Cannot verify a possible foreground daemon; deletion was not started."
            ) from None
    return result


def _assert_no_foreground(path, owner_uid, binding=None):
    allowed = {managed_service._pid(binding.status)} if binding is not None else set()
    if _matching_daemons(path, owner_uid) - allowed:
        raise instances.InstanceError(
            "A foreground daemon is using this configuration or its startup record. Stop its foreground session before deleting the instance."
        )


def _workspace(snapshot, binding):
    if snapshot is not None:
        try:
            data = json.loads(snapshot.data)
            value = data.get("workspace") if isinstance(data, dict) else None
            if isinstance(value, str) and Path(value).is_absolute():
                return value
        except (ValueError, UnicodeError):
            pass
    if binding is not None:
        service, _, _ = managed_service._parse_unit(binding.unit_bytes, binding.system)
        return managed_service._literal(service["WorkingDirectory"][0])
    return ""


def prepare_delete_instance(manager, config_path, *, system=False):
    """Read and validate a deletion target without changing files or services."""
    name = instances.canonical_name(
        manager if isinstance(manager, str) else manager.name
    )
    system = system if isinstance(manager, str) else manager.system
    if system and (sys.platform != "linux" or os.geteuid() != 0):
        raise PermissionError(
            "System instance deletion requires administrator authentication."
        )
    path = Path(config_path).expanduser().absolute()
    saved = _saved(path)
    binding = _binding(name, system, path)
    if saved is None and binding is None:
        raise instances.InstanceError("This instance no longer exists.")
    owner_uid = binding.owner_uid if binding is not None else saved.uid
    if saved is not None and saved.uid != owner_uid:
        raise instances.InstanceError(
            "The configuration belongs to a different service account."
        )
    if binding is not None:
        managed_service._authorize(binding)
        managed_service.assert_removal_external(binding)
    _assert_unique(name, system, path, owner_uid)
    record = _saved(path.with_suffix(".status.json"))
    if record is not None and record.uid != owner_uid:
        raise instances.InstanceError(
            "The startup record belongs to a different account."
        )
    _assert_no_foreground(path, owner_uid, binding)
    unit_identity = (
        _file_identity(binding.unit_path.lstat()) if binding is not None else None
    )
    return DeletePlan(
        name,
        system,
        path,
        _workspace(saved, binding),
        binding is not None,
        saved,
        binding,
        unit_identity,
        owner_uid,
    )


def _unchanged(plan):
    current = prepare_delete_instance(plan.name, plan.config_path, system=plan.system)
    if (
        current._config != plan._config
        or current._unit_identity != plan._unit_identity
        or current.has_service != plan.has_service
        or current._owner_uid != plan._owner_uid
        or (
            current._binding is not None
            and current._binding.unit_bytes != plan._binding.unit_bytes
        )
    ):
        raise instances.InstanceError(
            "The instance changed after the deletion was reviewed; review it again."
        )
    return current


def delete_instance(plan):
    """Delete only the reviewed instance; never recursively remove any directory."""
    if not isinstance(plan, DeletePlan):
        raise TypeError("Deletion requires a reviewed instance plan.")
    # The user catalog lock is also held by creation and configuration edits.
    # System targets use file identities, without creating a root user catalog.
    lock = nullcontext() if plan.system else instances.catalog_lock()
    with lock:
        current = _unchanged(plan)
        service_touched = False
        try:
            if current.has_service:
                manager = ServiceManager(plan.name, plan.system)
                managed_service.assert_removal_external(current._binding)
                service_touched = True
                manager.uninstall(
                    expected_content=current._binding.unit_bytes.decode("utf-8"),
                    expected_identity=current._unit_identity,
                )
                status = manager.status()
                if (
                    status.get("ActiveState") not in {"inactive", "failed", ""}
                    or status.get("MainPID", "0") != "0"
                ):
                    raise instances.InstanceError(
                        "The service has not stopped completely."
                    )
            _assert_no_foreground(plan.config_path, plan._owner_uid)
            _assert_unique(
                plan.name,
                plan.system,
                plan.config_path,
                plan._owner_uid,
                check_catalog=not current.has_service,
            )
            if _saved(plan.config_path) != plan._config:
                raise instances.InstanceError(
                    "The configuration changed during deletion; the current file was retained."
                )
            record_path = plan.config_path.with_suffix(".status.json")
            record = _saved(record_path)
            if record is not None:
                if record.uid != plan._owner_uid:
                    raise instances.InstanceError(
                        "The startup record belongs to a different account."
                    )
                if _saved(record_path) != record:
                    raise instances.InstanceError(
                        "The startup record changed during deletion."
                    )
                record_path.unlink()
            if plan._config is not None:
                if _saved(plan.config_path) != plan._config:
                    raise instances.InstanceError(
                        "The configuration changed during deletion; the current file was retained."
                    )
                plan.config_path.unlink()
        except (OSError, ValueError, RuntimeError) as error:
            if service_touched:
                raise instances.InstanceError(
                    "Deletion incomplete: the configuration was retained. The service may already be stopped or unregistered. "
                    + str(error)
                ) from None
            raise
