"""Readiness cancellation leaves confirmed service lifecycle changes intact."""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from kt_mcp import managed_service, operations

TOKEN = "startup-wait-secret-" + "a" * 32


@pytest.fixture
def backend(monkeypatch):
    events = []
    status = {"ActiveState": "active", "MainPID": "12345"}
    binding = SimpleNamespace(
        name="kt-mcp-private-name",
        system=False,
        config_path=Path("/private/config.json"),
        status=status,
    )

    def refresh(target, timeout):
        assert target is binding
        assert 0 < timeout <= 60
        events.append("refresh")
        return binding

    def startup(target, pid):
        assert target is binding and pid == 12345
        events.append("startup")
        return 8765, "/mcp/" + TOKEN

    def initialize(port, path, timeout):
        assert (port, path) == (8765, "/mcp/" + TOKEN)
        assert 0 < timeout <= 2
        events.append("initialize")
        return "0.3.2"

    def command(system, *arguments, timeout):
        assert system is False
        assert arguments == ("start", binding.name + ".service")
        assert 0 < timeout <= 60
        events.append("start")

    monkeypatch.setattr(managed_service, "_refresh", refresh)
    monkeypatch.setattr(managed_service, "_startup", startup)
    monkeypatch.setattr(managed_service, "_initialize", initialize)
    monkeypatch.setattr(managed_service, "_run", command)
    monkeypatch.setattr(
        managed_service, "_authorize", lambda target: events.append("authorize")
    )
    monkeypatch.setattr(
        managed_service,
        "assert_external_process",
        lambda target, **kwargs: events.append("external"),
    )
    monkeypatch.setattr(
        managed_service.time,
        "sleep",
        lambda seconds: pytest.fail("Cancellation must leave the retry loop"),
    )
    return SimpleNamespace(binding=binding, status=status, events=events)


def test_precancelled_start_still_sends_the_confirmed_start_request(backend):
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(managed_service.StartupWaitCancelled):
        managed_service.start_service(backend.binding, cancel=cancelled.is_set)

    assert backend.events == ["authorize", "external", "refresh", "start"]


def test_wait_cancelled_at_entry_performs_no_service_operation(backend):
    with pytest.raises(managed_service.StartupWaitCancelled):
        managed_service.wait_ready(backend.binding, cancel=lambda: True)

    assert backend.events == []


def test_cancel_before_initialize_is_not_retried(backend):
    cancelled = threading.Event()

    def progress(message):
        if message == "Checking the local MCP connection...":
            cancelled.set()

    with pytest.raises(managed_service.StartupWaitCancelled):
        managed_service.wait_ready(
            backend.binding, on_progress=progress, cancel=cancelled.is_set
        )

    assert backend.events == ["refresh", "startup"]


@pytest.mark.parametrize("probe_succeeds", [False, True])
def test_cancel_during_initialize_is_observed_after_the_probe(
    backend, monkeypatch, probe_succeeds
):
    cancelled = threading.Event()

    def initialize(*arguments):
        backend.events.append("initialize")
        cancelled.set()
        if not probe_succeeds:
            raise managed_service.ManagedServiceError("Not ready yet")
        return "0.3.2"

    monkeypatch.setattr(managed_service, "_initialize", initialize)

    with pytest.raises(managed_service.StartupWaitCancelled):
        managed_service.wait_ready(backend.binding, cancel=cancelled.is_set)

    assert backend.events == ["refresh", "startup", "initialize"]


def test_cancel_while_startup_record_is_unavailable_ends_the_wait(backend, monkeypatch):
    cancelled = threading.Event()

    def startup(*arguments):
        backend.events.append("startup")
        cancelled.set()
        raise managed_service.ManagedServiceError("Current startup is unavailable")

    monkeypatch.setattr(managed_service, "_startup", startup)

    with pytest.raises(managed_service.StartupWaitCancelled):
        managed_service.wait_ready(backend.binding, cancel=cancelled.is_set)

    assert backend.events == ["refresh", "startup"]


def test_cancel_during_final_state_refresh_is_not_reported_as_ready(
    backend, monkeypatch
):
    cancelled = threading.Event()
    refresh = managed_service._refresh

    def final_refresh(*arguments):
        result = refresh(*arguments)
        if backend.events.count("refresh") == 2:
            cancelled.set()
        return result

    monkeypatch.setattr(managed_service, "_refresh", final_refresh)

    with pytest.raises(managed_service.StartupWaitCancelled):
        managed_service.wait_ready(backend.binding, cancel=cancelled.is_set)

    assert backend.events == ["refresh", "startup", "initialize", "refresh"]


def test_default_ready_result_and_fixed_progress_contain_no_credentials(backend):
    progress = []

    result = managed_service.wait_ready(
        backend.binding, expected_version="0.3.2", on_progress=progress.append
    )

    assert result == backend.status
    assert backend.events == ["refresh", "startup", "initialize", "refresh"]
    assert progress == [
        "Waiting for systemd to start the service...",
        "Waiting for MCP / Cloudflare startup...",
        "Checking the local MCP connection...",
    ]
    assert all(
        value not in "\n".join(progress)
        for value in (TOKEN, backend.binding.name, str(backend.binding.config_path))
    )


def test_default_start_keeps_the_existing_wait_signature(backend, monkeypatch):
    def ready(binding, *, timeout, expected_version):
        assert binding is backend.binding
        assert 0 < timeout <= 60
        assert expected_version == "0.3.2"
        return backend.status

    monkeypatch.setattr(managed_service, "wait_ready", ready)

    assert (
        managed_service.start_service(backend.binding, expected_version="0.3.2")
        == backend.status
    )
    assert backend.events == ["authorize", "external", "refresh", "start"]


def prepare_operation(backend, monkeypatch, *, installed=True):
    loaded = installed

    def status():
        return {
            **backend.status,
            "LoadState": "loaded" if loaded else "not-found",
        }

    def install(path):
        nonlocal loaded
        assert path == backend.binding.config_path
        backend.events.append("install")
        loaded = True

    manager = SimpleNamespace(
        system=False, status=status, install=install, name=backend.binding.name
    )
    monkeypatch.setattr(
        operations, "bound_service", lambda manager, path: backend.binding
    )
    monkeypatch.setattr(operations.instances, "read_config", lambda path: object())
    monkeypatch.setattr(
        operations.instances, "ensure_available", lambda config, **kwargs: None
    )
    return manager


def test_control_first_start_registers_and_starts_before_cancelling_wait(
    backend, monkeypatch
):
    manager = prepare_operation(backend, monkeypatch, installed=False)
    progress = []

    with pytest.raises(managed_service.StartupWaitCancelled):
        operations.control_service(
            manager,
            backend.binding.config_path,
            "start",
            on_progress=progress.append,
            cancel=lambda: True,
        )

    assert backend.events == ["install", "authorize", "external", "refresh", "start"]
    assert progress == [
        "Checking the instance service...",
        "Registering the instance service...",
        "Starting the instance...",
        "Starting the systemd service...",
    ]


def test_restart_finishes_stop_and_start_before_honoring_wait_cancellation(
    backend, monkeypatch
):
    manager = prepare_operation(backend, monkeypatch)
    cancelled = threading.Event()
    progress = []

    def stop(target):
        assert target is backend.binding
        backend.events.append("stop")
        cancelled.set()

    monkeypatch.setattr(managed_service, "stop_service", stop)

    with pytest.raises(managed_service.StartupWaitCancelled):
        operations.control_service(
            manager,
            backend.binding.config_path,
            "restart",
            on_progress=progress.append,
            cancel=cancelled.is_set,
        )

    assert backend.events == ["stop", "authorize", "external", "refresh", "start"]
    assert "Stopping the instance..." in progress
    assert "Starting the instance..." in progress


def test_stop_is_completed_without_consulting_startup_wait_cancellation(
    backend, monkeypatch
):
    manager = prepare_operation(backend, monkeypatch)
    progress = []
    monkeypatch.setattr(
        managed_service, "stop_service", lambda target: backend.events.append("stop")
    )

    def cancel():
        pytest.fail("Stop operations complete independently of startup wait controls")

    result = operations.control_service(
        manager,
        backend.binding.config_path,
        "stop",
        on_progress=progress.append,
        cancel=cancel,
    )

    assert result == manager.status()
    assert backend.events == ["stop"]
    assert progress == ["Checking the instance service...", "Stopping the instance..."]
