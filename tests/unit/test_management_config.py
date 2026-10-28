from types import SimpleNamespace

import pytest

from kt_mcp import instance_cli, operations, service
from kt_mcp.daemon import load_config
from kt_mcp.desktop import DesktopSettings
from kt_mcp.onboarding import edit_config, setup_config


def test_shared_wizard_advanced_settings_and_clear_preserve_access_token(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("TUNNEL_TOKEN", raising=False)
    path = tmp_path / "config.json"
    first = iter(["", "", "", "advanced", "/custom/cloudflared", "yes", ""])
    config = setup_config(
        path,
        defaults=DesktopSettings(str(tmp_path), mode="local"),
        ask=lambda prompt: next(first),
    )
    assert config.settings.cloudflared == "/custom/cloudflared"
    assert config.settings.allow_outside_workspace is True
    second = iter(["", "", "", "advanced", ":clear", "no", ""])
    edited = edit_config(path, ask=lambda prompt: next(second))
    assert edited.token == config.token
    assert edited.settings.cloudflared == ""
    assert edited.settings.allow_outside_workspace is False
    assert load_config(path) == edited


def test_instance_cli_advanced_and_stdio_export_use_saved_configuration(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "catalog"))
    monkeypatch.setattr(instance_cli.instances, "_socket_available", lambda port: True)
    assert (
        instance_cli.main(
            [
                "create",
                "advanced",
                "--workspace",
                str(tmp_path),
                "--allow-outside-workspace",
                "--cloudflared",
                "/custom/cloudflared",
            ]
        )
        == 0
    )
    capsys.readouterr()
    item = instance_cli.instances.get_instance("advanced")
    output = tmp_path / "stdio.json"
    assert (
        instance_cli.main(
            [
                "client-config",
                "advanced",
                "--transport",
                "stdio",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert (
        output.read_text(encoding="utf-8")
        == operations.export_text(item.config_path, "stdio") + "\n"
    )


def test_cli_configure_uses_the_same_pause_and_edit_controller(
    tmp_path, monkeypatch, capsys
):
    from kt_mcp import tui

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "catalog"))
    monkeypatch.setattr(instance_cli.instances, "_socket_available", lambda port: True)
    item = instance_cli.instances.create_instance("selected", workspace=tmp_path)
    calls = []

    class Controller:
        note = "Configuration saved; selected service remains stopped."

        def __init__(self, manager, path):
            assert manager.name == item.name
            assert path == item.config_path

        def configure(self):
            calls.append("shared-configure")

    monkeypatch.setattr(tui, "TerminalController", Controller)
    monkeypatch.setattr(
        instance_cli, "ServiceManager", lambda name: SimpleNamespace(name=name)
    )
    assert instance_cli.main(["configure", "selected"]) == 0
    assert calls == ["shared-configure"]
    assert "remains stopped" in capsys.readouterr().out


@pytest.mark.parametrize(
    "action", ["start", "stop", "restart", "enable", "disable", "uninstall"]
)
def test_service_cli_uses_registered_custom_path_and_shared_actions(
    tmp_path, monkeypatch, action
):
    from kt_mcp import managed_service

    path = tmp_path / "custom.json"
    manager = SimpleNamespace(
        name="kt-mcp-custom", system=False, unit_name="kt-mcp-custom.service"
    )
    monkeypatch.setattr(service, "ServiceManager", lambda name, system: manager)
    inspected = []

    def inspect(name, system):
        inspected.append((name, system))
        return SimpleNamespace(config_path=path)

    monkeypatch.setattr(managed_service, "inspect_service", inspect)
    calls = []
    monkeypatch.setattr(
        operations, "control_service", lambda m, p, a: calls.append((m, p, a))
    )
    monkeypatch.setattr(
        operations, "service_setting", lambda m, p, a, **kw: calls.append((m, p, a, kw))
    )
    monkeypatch.setattr(
        operations, "uninstall_service", lambda m, p: calls.append((m, p, "uninstall"))
    )
    args = [action, "--name", manager.name]
    if action in {"enable", "disable"}:
        args.append("--now")
    assert service.main(args) == 0
    assert inspected == [(manager.name, False)]
    assert calls[0][:3] == (manager, path, action)
    if action in {"enable", "disable"}:
        assert calls[0][3] == {"now": True}


def test_service_cli_keeps_explicit_config_for_binding_verification(
    tmp_path, monkeypatch
):
    from kt_mcp import managed_service

    declared = tmp_path / "displayed.json"
    other = tmp_path / "registered.json"
    manager = SimpleNamespace(
        name="kt-mcp-custom", system=False, unit_name="kt-mcp-custom.service"
    )
    monkeypatch.setattr(service, "ServiceManager", lambda *args: manager)
    monkeypatch.setattr(
        managed_service,
        "inspect_service",
        lambda *args: SimpleNamespace(config_path=other),
    )
    selected = []
    monkeypatch.setattr(
        operations, "control_service", lambda m, path, action: selected.append(path)
    )
    assert (
        service.main(["stop", "--name", manager.name, "--config", str(declared)]) == 0
    )
    assert selected == [declared]


def test_service_only_start_does_not_create_an_unknown_unit(monkeypatch):
    from kt_mcp import managed_service

    monkeypatch.setattr(service, "ServiceManager", lambda *args: SimpleNamespace())
    monkeypatch.setattr(managed_service, "inspect_service", lambda *args: None)
    monkeypatch.setattr(
        operations,
        "control_service",
        lambda *args: pytest.fail("must not install an unknown unit"),
    )
    with pytest.raises(SystemExit) as error:
        service.main(["start", "--name", "kt-mcp-missing"])
    assert error.value.code == 2
