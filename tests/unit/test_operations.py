import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from kt_mcp import instances, operations
from kt_mcp.daemon import DaemonConfig, save_config
from kt_mcp.desktop import DesktopSettings

TOKEN = "operations-token-" + "a" * 40


class Manager:
    def __init__(self, installed=True):
        self.name = "kt-mcp-selected"
        self.system = False
        self.installed = installed
        self.events = []

    def status(self):
        return {
            "LoadState": "loaded" if self.installed else "not-found",
            "ActiveState": "inactive",
        }

    def install(self, path, **options):
        self.events.append(("install", path, options))
        self.installed = True

    def control(self, action, **options):
        self.events.append((action, options))

    def uninstall(self):
        self.events.append(("uninstall",))
        self.installed = False


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "catalog"))
    path = tmp_path / "config.json"
    save_config(
        DaemonConfig(
            DesktopSettings(str(tmp_path), mode="local", allow_outside_workspace=True),
            TOKEN,
            "",
        ),
        path,
    )
    return path


def bind(monkeypatch, manager, path):
    binding = SimpleNamespace(status={"ActiveState": "inactive"}, name=manager.name)
    calls = []

    def inspect(name, system, **expected):
        calls.append((name, system, expected))
        assert expected == {
            "expected_python": sys.executable,
            "expected_config": path,
        }
        return binding

    monkeypatch.setattr(operations.managed_service, "inspect_service", inspect)
    monkeypatch.setattr(instances, "ensure_available", lambda *args, **kwargs: None)
    return binding, calls


def test_first_start_has_identical_explicit_registration_semantics(
    configuration, monkeypatch
):
    manager = Manager(installed=False)
    binding, calls = bind(monkeypatch, manager, configuration)
    expected = {"ActiveState": "active", "MainPID": "123"}
    started = []
    monkeypatch.setattr(
        operations.managed_service,
        "start_service",
        lambda target: started.append(target) or expected,
    )
    result = operations.control_service(manager, configuration, "start")
    assert result == expected
    assert manager.events == [("install", configuration, {})]
    assert started == [binding]
    assert calls[0][0] == manager.name


def test_mismatched_configuration_never_stops_or_uninstalls(configuration, monkeypatch):
    manager = Manager()

    def reject(*args, **kwargs):
        raise operations.managed_service.ManagedServiceError(
            "wrong selected configuration"
        )

    monkeypatch.setattr(operations.managed_service, "inspect_service", reject)
    monkeypatch.setattr(
        operations.managed_service,
        "stop_service",
        lambda binding: pytest.fail("mismatched unit must not be stopped"),
    )
    for operation in [
        lambda: operations.control_service(manager, configuration, "stop"),
        lambda: operations.uninstall_service(manager, configuration),
        lambda: operations.service_setting(manager, configuration, "disable", now=True),
    ]:
        with pytest.raises(operations.managed_service.ManagedServiceError):
            operation()
    assert manager.events == []
    assert configuration.exists()


def test_restart_waits_for_stop_then_verified_start(configuration, monkeypatch):
    manager = Manager()
    binding, _ = bind(monkeypatch, manager, configuration)
    events = []
    monkeypatch.setattr(
        operations.managed_service,
        "stop_service",
        lambda item: events.append(("stop", item)),
    )
    monkeypatch.setattr(
        operations.managed_service,
        "start_service",
        lambda item: events.append(("start", item)),
    )
    operations.control_service(manager, configuration, "restart")
    assert events == [("stop", binding), ("start", binding)]
    assert manager.events == []


def test_boot_now_and_uninstall_require_bound_external_service(
    configuration, monkeypatch
):
    manager = Manager()
    binding, _ = bind(monkeypatch, manager, configuration)
    checked = []
    monkeypatch.setattr(
        operations.managed_service,
        "assert_external_process",
        lambda item: checked.append(item),
    )
    operations.service_setting(manager, configuration, "disable", now=True)
    operations.uninstall_service(manager, configuration)
    assert checked == [binding, binding]
    assert manager.events == [("disable", {"now": True}), ("uninstall",)]
    assert configuration.exists()


def test_exports_use_saved_credentials_and_real_workspace(configuration, monkeypatch):
    monkeypatch.setenv("KT_MCP_TOKEN", "another-token-" + "b" * 40)
    assert operations.export_text(configuration) == "http://127.0.0.1:8765/mcp/" + TOKEN
    client = json.loads(operations.export_text(configuration, "client-config"))
    assert client["mcpServers"]["kt-mcp"]["url"].endswith(TOKEN)
    stdio = operations.export_text(configuration, "stdio")
    assert TOKEN not in stdio
    command = json.loads(stdio)["mcpServers"]["kt-mcp"]
    assert command["command"] == os.path.abspath(sys.executable)
    assert command["args"][-1] == "--allow-outside-workspace"
    assert str(configuration.parent) in command["args"]


def test_private_export_refuses_existing_destination_and_preserves_unwrapped_text(
    tmp_path,
):
    path = tmp_path / "client.json"
    text = "secret-url-" + "x" * 300
    assert operations.write_export(path, text) == path
    assert path.read_text(encoding="utf-8") == text + "\n"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        operations.write_export(path, "replacement")
    assert path.read_text(encoding="utf-8") == text + "\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink behavior")
def test_export_never_follows_symlink(tmp_path):
    target = tmp_path / "original"
    target.write_text("preserve", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        operations.write_export(link, "secret")
    assert target.read_text(encoding="utf-8") == "preserve"


def test_failed_export_removes_only_its_incomplete_file(tmp_path, monkeypatch):
    path = tmp_path / "failed"

    def fail(descriptor):
        raise OSError("injected write failure")

    monkeypatch.setattr(operations.os, "fsync", fail)
    with pytest.raises(OSError, match="injected"):
        operations.write_export(path, "private")
    assert not path.exists()


def test_desktop_runtime_receives_explicit_file_scope_setting(tmp_path, monkeypatch):
    from kt_mcp import desktop

    captured = []

    def capture(config):
        captured.append(config)
        raise ValueError("stop before constructing a server")

    monkeypatch.setattr(desktop, "Runtime", capture)
    service = desktop.DesktopService()
    service._run(
        DesktopSettings(str(tmp_path), mode="local", allow_outside_workspace=True),
        TOKEN,
        "",
    )
    assert len(captured) == 1
    assert captured[0].allow_outside_workspace is True
