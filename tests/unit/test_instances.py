import json
import os
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from kt_mcp import instances, service
from kt_mcp.daemon import DaemonConfig, save_config
from kt_mcp.desktop import DesktopSettings
from kt_mcp.onboarding import SetupCancelled

SOCKET_AVAILABLE = instances._socket_available


@pytest.fixture(autouse=True)
def private_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("TUNNEL_TOKEN", raising=False)
    monkeypatch.setattr(instances, "_socket_available", lambda port: True)


def workspace(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def configuration(path, port=8765, **settings):
    return DaemonConfig(
        DesktopSettings(
            workspace=str(path.resolve()), port=port, mode="local", **settings
        ),
        "a" * 40,
    )


def test_empty_catalog_is_read_only(tmp_path):
    assert instances.list_instances() == []
    assert not (tmp_path / "config").exists()


def test_create_separate_configs_ports_and_tokens_without_environment_override(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KT_MCP_TOKEN", "environment-token-" + "a" * 32)
    first = instances.create_instance("alpha", workspace=workspace(tmp_path, "one"))
    second = instances.create_instance(
        "kt-mcp-beta", workspace=workspace(tmp_path, "two")
    )
    assert (first.name, second.name) == ("kt-mcp-alpha", "kt-mcp-beta")
    assert (first.port, second.port) == (8765, 8766)
    assert first.config_path != second.config_path
    a = instances.read_config(first.config_path)
    b = instances.read_config(second.config_path)
    assert a.token != b.token
    assert a.token != os.environ["KT_MCP_TOKEN"]
    assert b.token != os.environ["KT_MCP_TOKEN"]
    assert instances.get_instance("alpha") == first
    assert instances.find_workspace(Path(first.workspace)) == first
    if os.name == "posix":
        assert first.config_path.stat().st_mode & 0o777 == 0o600


def test_legacy_default_is_discovered_and_remains_unchanged(tmp_path):
    config = configuration(workspace(tmp_path, "legacy"))
    path = instances.config_path("kt-mcp")
    save_config(config, path)
    original = path.read_bytes()
    item = instances.get_instance("kt-mcp")
    assert item.config_path == path
    assert item.workspace == config.settings.workspace
    created = instances.create_instance("new", workspace=workspace(tmp_path, "new"))
    assert created.port == 8766
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "name", ["../escape", "/tmp/name", "", "x" * 74, "bad name", "kt-mcp-"]
)
def test_invalid_names_cannot_escape_catalog(tmp_path, name):
    with pytest.raises(instances.InstanceError):
        instances.create_instance(name, workspace=tmp_path)
    assert instances.list_instances() == []


def test_name_port_and_workspace_conflicts_do_not_replace_files(tmp_path):
    first = instances.create_instance("one", workspace=workspace(tmp_path, "one"))
    original = first.config_path.read_bytes()
    other = workspace(tmp_path, "two")
    with pytest.raises(instances.InstanceError, match="already exists"):
        instances.create_instance("one", workspace=other)
    with pytest.raises(instances.InstanceError, match="Port"):
        instances.create_instance("two", workspace=other, port=first.port)
    with pytest.raises(instances.InstanceError, match="workspace"):
        instances.create_instance(
            "two", workspace=Path(first.workspace), port=first.port + 1
        )
    assert first.config_path.read_bytes() == original
    assert not instances.config_path("two").exists()
    instances.ensure_available(
        instances.read_config(first.config_path), exclude=first.config_path
    )


def test_named_origins_are_compared_as_https_origins(tmp_path):
    one = instances.create_instance(
        "one",
        workspace=workspace(tmp_path, "one"),
        mode="named",
        public_origin="https://MCP.Example.com",
        tunnel_token="first-secret",
    )
    with pytest.raises(instances.InstanceError, match="origin"):
        instances.create_instance(
            "two",
            workspace=workspace(tmp_path, "two"),
            mode="named",
            public_origin="https://mcp.example.com.:443",
            tunnel_token="second-secret",
        )
    assert instances.get_instance("one") == one
    assert not instances.config_path("two").exists()


def test_choose_port_skips_reserved_and_bound_ports(tmp_path, monkeypatch):
    first = instances.create_instance("one", workspace=workspace(tmp_path, "one"))
    monkeypatch.setattr(instances, "_socket_available", lambda port: port != 8766)
    assert instances.choose_port() == 8767
    assert instances.choose_port(exclude=first) == 8765


def test_socket_probe_detects_live_listener_even_with_address_reuse(monkeypatch):
    monkeypatch.setattr(instances, "_socket_available", SOCKET_AVAILABLE)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert not instances._socket_available(listener.getsockname()[1])


@pytest.mark.skipif(os.name != "posix", reason="POSIX listener restart semantics")
def test_socket_probe_allows_restart_after_completed_connection(monkeypatch):
    monkeypatch.setattr(instances, "_socket_available", SOCKET_AVAILABLE)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            accepted, _ = listener.accept()
            with accepted:
                accepted.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
    assert instances._socket_available(port)


def test_malformed_config_stays_visible_without_exposing_its_contents(tmp_path):
    path = instances.config_path("broken")
    path.parent.mkdir(parents=True)
    secret = "never-print-this-credential"
    path.write_text(
        json.dumps({"token": secret, "workspace": secret}), encoding="utf-8"
    )
    path.chmod(0o600)
    item = instances.get_instance("broken")
    assert item.error
    assert item.workspace == ""
    assert secret not in repr(item)
    valid = instances.create_instance("valid", workspace=workspace(tmp_path, "valid"))
    assert {item.name for item in instances.list_instances()} == {
        "kt-mcp-broken",
        valid.name,
    }


def test_suggested_names_are_stable_path_specific_and_valid(tmp_path):
    first = workspace(tmp_path, "项目 一")
    second = workspace(tmp_path, "项目 二")
    assert instances.suggest_name(first) == instances.suggest_name(first / ".")
    assert instances.suggest_name(first) != instances.suggest_name(second)
    assert instances.canonical_name(
        instances.suggest_name(first)
    ) == instances.suggest_name(first)


def test_duplicate_legacy_workspace_requires_explicit_selection(tmp_path):
    selected = workspace(tmp_path, "same")
    save_config(configuration(selected), instances.config_path("kt-mcp"))
    save_config(configuration(selected, port=8766), instances.config_path("other"))
    with pytest.raises(instances.InstanceError, match="Several instances"):
        instances.find_workspace(selected)


@pytest.mark.skipif(sys.platform != "linux", reason="real generated Linux unit grammar")
def test_owned_generated_unit_discovers_custom_config_without_systemctl(
    tmp_path, monkeypatch
):
    path = tmp_path / "custom" / "daemon.json"
    save_config(configuration(workspace(tmp_path, "custom-project")), path)
    monkeypatch.setattr(
        service,
        "_account",
        lambda *_: SimpleNamespace(
            pw_uid=os.geteuid(),
            pw_gid=os.getegid(),
            pw_dir=str(tmp_path),
            pw_name="test",
        ),
    )
    unit = instances._unit_directory() / "kt-mcp-custom.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(service.render_unit(path), encoding="utf-8")
    unit.chmod(0o644)
    monkeypatch.setattr(
        service, "_run", lambda *a, **k: pytest.fail("discovery called systemd")
    )
    item = instances.get_instance("custom")
    assert item.config_path == path
    assert not item.error
    assert item.workspace == str(tmp_path / "custom-project")


@pytest.fixture
def generated_unit(tmp_path, monkeypatch):
    if sys.platform != "linux":
        pytest.skip("real generated Linux unit grammar")
    monkeypatch.setattr(
        service,
        "_account",
        lambda *_: SimpleNamespace(
            pw_uid=os.geteuid(),
            pw_gid=os.getegid(),
            pw_dir=str(tmp_path),
            pw_name="test",
        ),
    )
    monkeypatch.setattr(
        service, "_run", lambda *a, **k: pytest.fail("discovery called systemd")
    )

    def write(name, path):
        unit = instances._unit_directory() / (name + ".service")
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(service.render_unit(path), encoding="utf-8")
        unit.chmod(0o644)
        return unit

    return write


@pytest.mark.parametrize("saved_name", ["kt-mcp", "catalog-name"])
def test_actual_unit_name_replaces_only_the_synthetic_config_alias(
    tmp_path, generated_unit, saved_name
):
    project = workspace(tmp_path, "legacy-named-project")
    path = instances.config_path(saved_name)
    save_config(configuration(project), path)
    original = path.read_bytes()
    generated_unit("kt-mcp-dev", path)
    items = instances.list_instances()
    assert len(items) == 1
    assert items[0].name == "kt-mcp-dev"
    assert items[0].config_path == path
    assert not items[0].error
    assert instances.get_instance("dev") == items[0]
    assert instances.find_workspace(project) == items[0]
    assert path.read_bytes() == original


def test_two_actual_units_sharing_config_remain_ambiguous(tmp_path, generated_unit):
    project = workspace(tmp_path, "shared-project")
    path = instances.config_path("kt-mcp")
    save_config(configuration(project), path)
    generated_unit("kt-mcp-one", path)
    generated_unit("kt-mcp-two", path)
    items = instances.list_instances()
    assert {item.name for item in items} == {"kt-mcp-one", "kt-mcp-two"}
    assert all("share" in item.error for item in items)
    with pytest.raises(instances.InstanceError, match="Several instances"):
        instances.find_workspace(project)


def test_unmarked_service_name_is_not_overwritten(tmp_path):
    unit = instances._unit_directory() / "kt-mcp-other.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/some/other/program\n", encoding="utf-8")
    original = unit.read_bytes()
    assert instances.list_instances() == []
    with pytest.raises(instances.InstanceError, match="already exists"):
        instances.create_instance("other", workspace=tmp_path)
    assert unit.read_bytes() == original


def test_catalog_lock_rejects_concurrent_writer_and_is_reusable():
    with (
        instances.catalog_lock(),
        pytest.raises(instances.InstanceError, match="in progress"),
        instances.catalog_lock(),
    ):
        pytest.fail("concurrent catalog lock was acquired")
    with instances.catalog_lock():
        pass


def test_concurrent_creates_cannot_reserve_the_same_port(tmp_path):
    paths = [workspace(tmp_path, "one"), workspace(tmp_path, "two")]

    def create(index):
        try:
            return instances.create_instance(
                str(index), workspace=paths[index], port=8765
            )
        except instances.InstanceError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, [0, 1]))
    assert sum(item is not None for item in results) == 1
    assert len(instances.list_instances()) == 1


def test_cancelled_setup_creates_no_config(tmp_path):
    with pytest.raises(SetupCancelled):
        instances.setup_instance(tmp_path, "cancelled", ask=lambda _: ":q")
    assert instances.list_instances() == []
    assert not instances.config_path("cancelled").exists()


def test_setup_rechecks_conflicts_at_save_and_keeps_failed_file_absent(tmp_path):
    other = workspace(tmp_path, "competitor")
    answers = iter(["", "", "", ""])

    def ask(prompt):
        answer = next(answers)
        if "Save?" in prompt:
            instances.create_instance("competitor", workspace=other, port=8765)
        return answer

    with pytest.raises(instances.InstanceError, match="reserved"):
        instances.setup_instance(tmp_path, "attempt", ask=ask)
    assert not instances.config_path("attempt").exists()
    assert instances.get_instance("competitor").port == 8765


def test_auto_name_and_review_path_follow_final_workspace(tmp_path, capsys):
    first = workspace(tmp_path, "launch-project")
    second = workspace(tmp_path, "chosen-project")
    answers = iter([str(second), "", "", ""])
    created = instances.setup_instance(first, ask=lambda _: next(answers))
    assert created.name == instances.suggest_name(second)
    assert created.config_path == instances.config_path(instances.suggest_name(second))
    output = capsys.readouterr().out
    assert str(created.config_path) in output
    assert str(created.config_path) in output.rsplit("Saved", 1)[1]
    original = instances.setup_instance(first, ask=lambda _: "")
    assert original.name == instances.suggest_name(first)
    assert original.config_path != created.config_path
    assert original.workspace == str(first)
    third = workspace(tmp_path, "another-project")
    answers = iter([str(third), "", "", ""])
    another = instances.setup_instance(first, ask=lambda _: next(answers))
    assert another.name == instances.suggest_name(third)


def test_setup_reuses_wizard_and_generates_fresh_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KT_MCP_TOKEN", "b" * 40)
    item = instances.setup_instance(tmp_path, "wizard", ask=lambda _: "")
    assert item.workspace == str(tmp_path.resolve())
    assert item.mode == "local"
    assert instances.read_config(item.config_path).token != "b" * 40
