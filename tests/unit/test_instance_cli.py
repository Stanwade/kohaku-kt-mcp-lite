import json
from types import SimpleNamespace

import pytest

from kt_mcp import instance_cli, instances


@pytest.fixture(autouse=True)
def private_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("TUNNEL_TOKEN", raising=False)
    monkeypatch.setattr(instances, "_socket_available", lambda port: True)


@pytest.fixture
def lifecycle(monkeypatch):
    events = []
    states = {}

    class Manager:
        def __init__(self, name):
            self.name = name

        def status(self):
            return {"LoadState": "loaded" if self.name in states else "not-found"}

        def install(self, path):
            events.append(("install", self.name, path))
            states[self.name] = {"path": path, "active": False}

    def inspect(name, *, expected_config):
        if name not in states:
            return None
        assert expected_config == states[name]["path"]
        return SimpleNamespace(
            name=name,
            status={"ActiveState": "active" if states[name]["active"] else "inactive"},
        )

    def start(binding):
        events.append(("start", binding.name))
        states[binding.name]["active"] = True

    def stop(binding):
        events.append(("stop", binding.name))
        states[binding.name]["active"] = False

    monkeypatch.setattr(instance_cli, "ServiceManager", Manager)
    monkeypatch.setattr(instance_cli.managed_service, "inspect_service", inspect)
    monkeypatch.setattr(instance_cli.managed_service, "start_service", start)
    monkeypatch.setattr(instance_cli.managed_service, "stop_service", stop)
    return SimpleNamespace(events=events, states=states)


def make_workspace(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def test_create_defaults_to_cwd_and_stays_stopped(
    tmp_path, monkeypatch, lifecycle, capsys
):
    monkeypatch.chdir(tmp_path)
    assert instance_cli.main(["create", "project"]) == 0
    item = instances.get_instance("project")
    assert item.workspace == str(tmp_path.resolve())
    assert lifecycle.events == []
    assert "Start: kt-mcp instance start kt-mcp-project" in capsys.readouterr().out


def test_create_start_and_stop_target_only_selected_instance(tmp_path, lifecycle):
    first = make_workspace(tmp_path, "first")
    second = make_workspace(tmp_path, "second")
    assert (
        instance_cli.main(["create", "first", "--workspace", str(first), "--start"])
        == 0
    )
    assert (
        instance_cli.main(["create", "second", "--workspace", str(second), "--start"])
        == 0
    )
    before = list(lifecycle.events)
    assert instance_cli.main(["stop", "first"]) == 0
    assert lifecycle.events[len(before) :] == [("stop", "kt-mcp-first")]
    assert lifecycle.states["kt-mcp-second"]["active"]
    assert not lifecycle.states["kt-mcp-first"]["active"]


def test_restart_preserves_other_instance_state(tmp_path, lifecycle):
    for name in ("one", "two"):
        path = make_workspace(tmp_path, name)
        assert (
            instance_cli.main(["create", name, "--workspace", str(path), "--start"])
            == 0
        )
    before = list(lifecycle.events)
    assert instance_cli.main(["restart", "one"]) == 0
    assert lifecycle.events[len(before) :] == [
        ("stop", "kt-mcp-one"),
        ("start", "kt-mcp-one"),
    ]
    assert all(state["active"] for state in lifecycle.states.values())


def test_list_never_prints_token_and_link_uses_saved_instance_token(
    tmp_path, monkeypatch, capsys
):
    item = instances.create_instance("private", workspace=tmp_path)
    config = instances.read_config(item.config_path)
    monkeypatch.setenv("KT_MCP_TOKEN", "environment-override-" + "a" * 32)
    assert instance_cli.main(["list", "--json"]) == 0
    listing = capsys.readouterr().out
    assert config.token not in listing
    record = json.loads(listing)[0]
    assert record["name"] == item.name
    assert "token" not in record
    assert instance_cli.main(["link", "private"]) == 0
    assert (
        capsys.readouterr().out == f"http://127.0.0.1:{item.port}/mcp/{config.token}\n"
    )


def test_unknown_instance_and_invalid_config_are_actionable_without_secrets(
    tmp_path, capsys
):
    assert instance_cli.main(["start", "absent"]) == 1
    assert "does not exist" in capsys.readouterr().err
    path = instances.config_path("broken")
    path.parent.mkdir(parents=True)
    path.write_text('{"token":"do-not-print-this"}', encoding="utf-8")
    path.chmod(0o600)
    assert instance_cli.main(["link", "broken"]) == 1
    error = capsys.readouterr().err
    assert "invalid" in error
    assert "do-not-print-this" not in error


def test_create_conflict_leaves_existing_config_and_service_unchanged(
    tmp_path, lifecycle, capsys
):
    item = instances.create_instance("one", workspace=tmp_path)
    before = item.config_path.read_bytes()
    other = make_workspace(tmp_path, "other")
    assert (
        instance_cli.main(
            [
                "create",
                "other",
                "--workspace",
                str(other),
                "--port",
                str(item.port),
                "--start",
            ]
        )
        == 1
    )
    assert "reserved" in capsys.readouterr().err
    assert item.config_path.read_bytes() == before
    assert not instances.config_path("other").exists()
    assert lifecycle.events == []


def test_named_creation_uses_hidden_environment_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUNNEL_TOKEN", "private-cloudflare-token")
    assert (
        instance_cli.main(
            [
                "create",
                "named",
                "--workspace",
                str(tmp_path),
                "--mode",
                "named",
                "--public-origin",
                "https://mcp.example.com",
            ]
        )
        == 0
    )
    config = instances.read_config(instances.get_instance("named").config_path)
    assert config.tunnel_token == "private-cloudflare-token"
    assert "private-cloudflare-token" not in capsys.readouterr().out


def test_empty_list_has_create_guidance(capsys):
    assert instance_cli.main(["list"]) == 0
    assert "instance create" in capsys.readouterr().out
