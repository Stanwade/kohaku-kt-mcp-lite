import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kt_mcp import instance_cli, instances, managed_service, operations, service
from kt_mcp import instance_delete as deletion
from kt_mcp.onboarding import _file_identity


@pytest.fixture(autouse=True)
def private_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(instances, "_socket_available", lambda port: True)


@pytest.fixture
def item(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "keep.txt").write_text("user project data", encoding="utf-8")
    return instances.create_instance("delete-test", workspace=workspace, port=18765)


def prepare(item):
    return operations.prepare_delete_instance(item.name, item.config_path)


def test_user_deletion_does_not_require_reading_the_privileged_manager_environment(
    item, monkeypatch
):
    def reject(owner_uid):
        raise PermissionError("systemd user-manager environ is protected")

    monkeypatch.setattr(deletion, "_user_manager_directories", reject)
    plan = prepare(item)
    operations.delete_instance(plan)
    assert not item.config_path.exists()


def test_saved_only_delete_removes_config_and_record_but_preserves_workspace_and_peer(
    item, tmp_path
):
    other_workspace = tmp_path / "peer"
    other_workspace.mkdir()
    peer = instances.create_instance("peer", workspace=other_workspace, port=18766)
    peer_bytes = peer.config_path.read_bytes()
    record = item.config_path.with_suffix(".status.json")
    record.write_text("{damaged stale record", encoding="utf-8")
    record.chmod(0o600)
    original = instances.read_config(item.config_path)
    plan = prepare(item)
    assert original.token not in repr(plan)
    assert original.token not in plan.summary
    assert not plan.has_service
    operations.delete_instance(plan)
    assert not item.config_path.exists()
    assert not record.exists()
    assert (Path(item.workspace) / "keep.txt").read_text() == "user project data"
    assert peer.config_path.read_bytes() == peer_bytes
    assert [entry.name for entry in instances.list_instances()] == [peer.name]
    replacement = instances.create_instance(
        item.name, workspace=Path(item.workspace), port=item.port
    )
    assert instances.read_config(replacement.config_path).token != original.token


@pytest.mark.parametrize("damage", ["json", "workspace"])
def test_saved_invalid_instance_can_be_deleted_without_repair(item, damage):
    if damage == "json":
        item.config_path.write_text(
            "{broken-token-must-not-be-printed", encoding="utf-8"
        )
    else:
        (Path(item.workspace) / "keep.txt").unlink()
        Path(item.workspace).rmdir()
    assert instances.get_instance(item.name).error
    plan = prepare(item)
    assert "broken-token" not in plan.summary
    operations.delete_instance(plan)
    assert instances.list_instances() == []


def test_replaced_config_after_confirmation_is_retained(item):
    plan = prepare(item)
    data = item.config_path.read_bytes()
    item.config_path.unlink()
    item.config_path.write_bytes(data)
    item.config_path.chmod(0o600)
    with pytest.raises(instances.InstanceError, match="changed"):
        operations.delete_instance(plan)
    assert item.config_path.read_bytes() == data


def test_edited_config_after_confirmation_is_retained(item):
    plan = prepare(item)
    data = item.config_path.read_bytes() + b"\n"
    item.config_path.write_bytes(data)
    with pytest.raises(instances.InstanceError, match="changed"):
        operations.delete_instance(plan)
    assert item.config_path.read_bytes() == data


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
@pytest.mark.parametrize("which", ["config", "record"])
def test_linked_config_or_record_never_deletes_referent(item, tmp_path, which):
    target = tmp_path / "private-user-file"
    target.write_text("preserve", encoding="utf-8")
    target.chmod(0o600)
    path = (
        item.config_path
        if which == "config"
        else item.config_path.with_suffix(".status.json")
    )
    path.unlink(missing_ok=True)
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        prepare(item)
    assert target.read_text() == "preserve"
    assert path.is_symlink()


def test_running_foreground_refuses_without_signalling_any_process(item, monkeypatch):
    monkeypatch.setattr(deletion, "_matching_daemons", lambda *args: {12345})
    monkeypatch.setattr(
        os, "kill", lambda *args: pytest.fail("must not signal an unowned process")
    )
    before = item.config_path.read_bytes()
    with pytest.raises(instances.InstanceError, match="foreground session"):
        prepare(item)
    assert item.config_path.read_bytes() == before


def test_catalog_lock_prevents_parallel_deletion(item):
    plan = prepare(item)
    with (
        instances.catalog_lock(),
        pytest.raises(instances.InstanceError, match="in progress"),
    ):
        operations.delete_instance(plan)
    assert item.config_path.exists()


def test_noninteractive_cli_requires_explicit_yes_before_preflight(
    item, monkeypatch, capsys
):
    monkeypatch.setattr(instance_cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        operations,
        "prepare_delete_instance",
        lambda *args, **kwargs: pytest.fail("no preflight without confirmation mode"),
    )
    assert instance_cli.main(["delete", item.name]) == 1
    assert "--yes" in capsys.readouterr().err
    assert item.config_path.exists()


@pytest.mark.parametrize("answer", ["", "n", "no", "y", "yes"])
def test_cli_confirmation_default_no_and_positive_answers_share_backend(
    item, monkeypatch, capsys, answer
):
    monkeypatch.setattr(instance_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(instance_cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert instance_cli.main(["delete", item.name]) == 0
    assert item.config_path.exists() == (answer not in {"y", "yes"})
    assert "user project data" == (Path(item.workspace) / "keep.txt").read_text()
    assert (
        "Deleted" if answer in {"y", "yes"} else "cancelled"
    ) in capsys.readouterr().out


def test_cli_delete_custom_path_and_default_configuration(item, tmp_path):
    target = tmp_path / "custom.json"
    item.config_path.rename(target)
    assert (
        instance_cli.main(["delete", item.name, "--config", str(target), "--yes"]) == 0
    )
    assert not target.exists()
    default = instances.create_instance(
        "kt-mcp", workspace=Path(item.workspace), port=item.port
    )
    assert instance_cli.main(["delete", "kt-mcp", "--yes"]) == 0
    assert not default.config_path.exists()


@pytest.fixture
def installed(item, monkeypatch):
    if sys.platform != "linux":
        pytest.skip("generated Linux units")
    info = item.config_path.stat()
    account = SimpleNamespace(
        pw_uid=info.st_uid,
        pw_gid=info.st_gid,
        pw_dir=str(item.config_path.parent),
        pw_name="tester",
    )
    monkeypatch.setattr(service, "_account", lambda *args: account)
    monkeypatch.setattr(managed_service, "_account", lambda *args: account)
    monkeypatch.setattr(managed_service, "_authorize", lambda binding: None)
    monkeypatch.setattr(service.ServiceManager, "_mutating", lambda self: None)
    manager = service.ServiceManager(item.name)
    unit = manager.unit_path
    unit.parent.mkdir(parents=True)
    unit.write_text(service.render_unit(item.config_path), encoding="utf-8")
    unit.chmod(0o644)
    events = []
    status = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "MainPID": "0",
        "FragmentPath": str(unit),
        "DropInPaths": "",
        "NeedDaemonReload": "no",
        "ControlGroup": "",
    }
    monkeypatch.setattr(managed_service, "_status", lambda *args: dict(status))

    def systemctl(self, *args, **kwargs):
        events.append(args)
        if args[0] == "show":
            return "LoadState=not-found\nActiveState=inactive\nMainPID=0\n"
        return ""

    monkeypatch.setattr(service.ServiceManager, "_systemctl", systemctl)
    return SimpleNamespace(
        item=item, manager=manager, unit=unit, events=events, status=status
    )


def test_installed_delete_checks_unit_and_removes_service_before_config(installed):
    plan = prepare(installed.item)
    assert plan.has_service
    operations.delete_instance(plan)
    assert installed.events[0] == ("disable", "--now", installed.manager.unit_name)
    assert ("daemon-reload",) in installed.events
    assert not installed.unit.exists()
    assert not installed.item.config_path.exists()
    assert Path(installed.item.workspace).is_dir()


@pytest.mark.parametrize("damage", ["json", "missing"])
def test_corrupt_or_missing_config_does_not_prevent_removing_verified_unit(
    installed, damage
):
    path = installed.item.config_path
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("{invalid-json", encoding="utf-8")
    with pytest.raises(managed_service.ManagedServiceError):
        managed_service.inspect_service(installed.item.name)
    plan = prepare(installed.item)
    operations.delete_instance(plan)
    assert not installed.unit.exists()
    assert not path.exists()


def test_failed_service_removal_retains_config_and_startup_record(
    installed, monkeypatch
):
    path = installed.item.config_path
    record = path.with_suffix(".status.json")
    record.write_text("{}", encoding="utf-8")
    record.chmod(0o600)
    before = path.read_bytes()
    plan = prepare(installed.item)

    def fail(self, **kwargs):
        raise service.ServiceError("injected stop failure")

    monkeypatch.setattr(service.ServiceManager, "uninstall", fail)
    with pytest.raises(instances.InstanceError, match="configuration was retained"):
        operations.delete_instance(plan)
    assert path.read_bytes() == before
    assert record.exists()
    assert installed.unit.exists()


def test_changed_unit_is_not_stopped_after_confirmation(installed):
    plan = prepare(installed.item)
    installed.unit.write_text(installed.unit.read_text() + "# changed\n")
    with pytest.raises(instances.InstanceError, match="changed"):
        operations.delete_instance(plan)
    assert installed.events == []
    assert installed.item.config_path.exists()


def test_uninstall_checks_expected_unit_at_last_mutation_boundary(installed):
    content = installed.unit.read_text()
    identity = _file_identity(installed.unit.lstat())
    installed.unit.write_text(content + "# rebound\n")
    with pytest.raises(ValueError, match="changed before removal"):
        installed.manager.uninstall(
            expected_content=content, expected_identity=identity
        )
    assert installed.events == []
    assert installed.unit.exists()


def test_shared_configuration_is_rejected_before_service_changes(installed):
    other = installed.unit.with_name("kt-mcp-peer.service")
    other.write_bytes(installed.unit.read_bytes())
    other.chmod(0o644)
    with pytest.raises(instances.InstanceError, match="shares"):
        prepare(installed.item)
    assert installed.events == []
    assert installed.item.config_path.exists()


def test_different_config_suffixes_cannot_share_deleted_status_record(installed):
    alternate = installed.item.config_path.with_suffix(".cfg")
    alternate.write_bytes(installed.item.config_path.read_bytes())
    alternate.chmod(0o600)
    other = installed.unit.with_name("kt-mcp-peer.service")
    other.write_text(service.render_unit(alternate), encoding="utf-8")
    other.chmod(0o644)
    with pytest.raises(instances.InstanceError, match="startup record"):
        prepare(installed.item)
    assert installed.events == []
    assert alternate.exists()
    assert installed.item.config_path.exists()


def test_legacy_named_unit_using_default_config_does_not_leave_synthetic_row(installed):
    default = instances.config_path("kt-mcp")
    installed.item.config_path.rename(default)
    installed.unit.write_text(service.render_unit(default), encoding="utf-8")
    assert [entry.name for entry in instances.list_instances()] == [installed.item.name]
    plan = operations.prepare_delete_instance(installed.item.name, default)
    operations.delete_instance(plan)
    assert not default.exists()
    assert not installed.unit.exists()
    assert instances.list_instances() == []


def test_self_service_protection_runs_before_mutation(installed, monkeypatch):
    def refuse(binding):
        raise managed_service.ManagedServiceError("target contains this process")

    monkeypatch.setattr(managed_service, "assert_removal_external", refuse)
    with pytest.raises(managed_service.ManagedServiceError, match="contains"):
        prepare(installed.item)
    assert installed.events == []
    assert installed.item.config_path.exists()
