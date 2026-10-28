"""Shared management operations for the command line and terminal interface."""

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from kt_mcp import instances, managed_service
from kt_mcp.daemon import client_url
from kt_mcp.desktop import client_configuration


def bound_service(manager, config_path):
    """Refuse to control a unit pointing at a different displayed configuration."""
    return managed_service.inspect_service(
        manager.name,
        manager.system,
        expected_python=sys.executable,
        expected_config=Path(config_path),
    )


def control_service(manager, config_path, action):
    """Register on first start, without implicitly enabling boot or user linger."""
    if action not in {"start", "stop", "restart"}:
        raise ValueError("Unsupported instance lifecycle action.")
    path = Path(config_path)
    if action == "start" and manager.status().get("LoadState") == "not-found":
        manager.install(path)
    binding = bound_service(manager, path)
    if binding is None:
        raise instances.InstanceError(
            "The service is not registered; choose Start or Install."
        )
    if action in {"start", "restart"} and not manager.system:
        instances.ensure_available(
            instances.read_config(path),
            exclude=path,
            check_socket=binding.status.get("ActiveState")
            not in {"active", "activating", "deactivating", "reloading"},
        )
    if action in {"stop", "restart"}:
        managed_service.stop_service(binding)
    if action in {"start", "restart"}:
        result = managed_service.start_service(binding)
        return result if isinstance(result, dict) else manager.status()
    return manager.status()


def service_setting(manager, config_path, action, *, now=False):
    """Boot settings still require the same verified instance/unit binding."""
    if action not in {"enable", "disable"}:
        raise ValueError("Unsupported automatic-start setting.")
    binding = bound_service(manager, config_path)
    if binding is None:
        raise instances.InstanceError(
            "Register this service before changing automatic startup."
        )
    if now:
        managed_service.assert_external_process(binding)
    manager.control(action, now=now)
    return manager.status()


def uninstall_service(manager, config_path):
    binding = bound_service(manager, config_path)
    if binding is None:
        raise instances.InstanceError("This service is not registered.")
    managed_service.assert_external_process(binding)
    manager.uninstall()


def instance_metadata():
    return [
        {**asdict(item), "config_path": str(item.config_path)}
        for item in instances.list_instances()
    ]


def export_text(config_path, kind="link"):
    """One serialization shared by CLI output, the viewer, and file exports."""
    config = instances.read_config(Path(config_path))
    if kind == "stdio":
        arguments = [
            "-I",
            "-m",
            "kt_mcp",
            "--transport",
            "stdio",
            "--workspace",
            config.settings.workspace,
        ]
        if getattr(config.settings, "allow_outside_workspace", False):
            arguments.append("--allow-outside-workspace")
        return json.dumps(
            {
                "mcpServers": {
                    "kt-mcp": {
                        "command": os.path.abspath(sys.executable),
                        "args": arguments,
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )
    url = client_url(config, Path(config_path))
    if kind == "link":
        return url
    if kind == "client-config":
        return client_configuration(url)
    raise ValueError("Unsupported export format.")


def write_export(path, text):
    """Create a private, complete export; never overwrite a file or follow a link."""
    path = Path(path).expanduser().absolute()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    owned = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text.rstrip("\n") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # This process created the path exclusively. Avoid removing a replacement.
        try:
            info = path.lstat()
            if (info.st_dev, info.st_ino) == (owned.st_dev, owned.st_ino):
                path.unlink()
        except OSError:
            pass
        raise
    return path
