import json
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import kt_mcp.service as module
from kt_mcp.daemon import DaemonConfig, save_config
from kt_mcp.desktop import DesktopSettings
from kt_mcp.service import MANAGED_MARKER, ServiceError, ServiceManager, render_unit

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux systemd management"
)
TOKEN = "service-unit-test-" + "a" * 32


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("TUNNEL_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = tmp_path / "private/config.json"
    config = DaemonConfig(DesktopSettings(str(workspace), mode="local"), TOKEN)
    save_config(config, config_path)
    account = SimpleNamespace(
        pw_uid=config_path.stat().st_uid,
        pw_gid=config_path.stat().st_gid,
        pw_name="tester",
        pw_dir=str(tmp_path / "home"),
    )
    monkeypatch.setattr(module, "_account", lambda system, run_as: account)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    return SimpleNamespace(
        path=config_path,
        config=config,
        account=account,
        calls=calls,
        manager=ServiceManager(),
        root=tmp_path,
    )


@pytest.mark.parametrize(
    "name",
    [
        "other",
        "../kt-mcp",
        "kt-mcp.service",
        "kt-mcp@",
        "kt-mcp-",
        "kt-mcp/x",
        "kt-mcp-" + "x" * 74,
    ],
)
def test_service_names_cannot_escape_project_namespace(name):
    with pytest.raises(ValueError):
        ServiceManager(name)


def test_unit_paths_and_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    manager = ServiceManager("kt-mcp-demo_2")
    assert manager.unit_name == "kt-mcp-demo_2.service"
    assert manager.unit_path == tmp_path / "config/systemd/user/kt-mcp-demo_2.service"
    assert ServiceManager(system=True).unit_path == Path(
        "/etc/systemd/system/kt-mcp.service"
    )


def test_render_keeps_venv_symlink_and_excludes_credentials(installation):
    python = installation.root / "virtual env/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    unit = render_unit(installation.path, python=python)
    assert unit.startswith(MANAGED_MARKER + "\n")
    assert (
        f'ExecStart=:"/usr/bin/env" "--" "{python}" "-m" "kt_mcp" "daemon" "--config"'
        in unit
    )
    assert f'Environment="PATH={python.parent}:/usr/local/sbin:' in unit
    assert f'Environment="HOME={installation.account.pw_dir}"' in unit
    assert TOKEN not in unit
    assert "tunnel_token" not in unit
    assert "User=" not in unit
    for setting in (
        "Type=exec",
        "Restart=on-failure",
        "RestartSec=5",
        "TimeoutStopSec=30",
        "KillMode=control-group",
        "UMask=0077",
        "NoNewPrivileges=true",
        "WantedBy=default.target",
    ):
        assert setting in unit.splitlines()


def test_executable_dollars_are_literal_and_venv_path_is_preserved(installation):
    python = (
        installation.root / 'virtual env $cash ${HOME} %percent "quoted"/bin/python'
    )
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    config_path = installation.path.parent / "config $cash.json"
    save_config(installation.config, config_path)
    unit = render_unit(config_path, python=python)
    command = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    executable, arguments = command.split(' "-m" ', 1)
    assert executable.startswith('ExecStart=:"/usr/bin/env" "--" "')
    assert '/virtual env $cash ${HOME} %%percent \\"quoted\\"/bin/python"' in executable
    assert "$$" not in command
    assert "config $cash.json" in arguments
    assert str(python.resolve()) not in executable


def test_special_paths_use_different_exec_and_working_directory_rules(installation):
    workspace = installation.root / 'repo 中文 % $HOME "quoted" \\directory'
    workspace.mkdir()
    path = installation.path.parent / 'config % $HOME "quoted" \\name.json'
    config = DaemonConfig(DesktopSettings(str(workspace), mode="local"), TOKEN)
    save_config(config, path)
    unit = render_unit(path)
    assert "WorkingDirectory=" + str(workspace).replace("%", "%%") in unit.splitlines()
    command = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert 'config %% $HOME \\"quoted\\" \\\\name.json"' in command
    assert "--config" in command


def test_system_unit_has_non_root_identity_and_target(installation):
    unit = render_unit(installation.path, system=True, run_as="tester")
    assert "User=tester" in unit.splitlines()
    assert f"Group={installation.account.pw_gid}" in unit.splitlines()
    assert "WantedBy=multi-user.target" in unit.splitlines()


def test_config_ownership_and_permissions_are_checked(installation, monkeypatch):
    installation.path.chmod(0o640)
    with pytest.raises(ValueError, match="mode 600"):
        render_unit(installation.path)
    installation.path.chmod(0o600)
    other = SimpleNamespace(**vars(installation.account))
    other.pw_uid += 1
    monkeypatch.setattr(module, "_account", lambda system, run_as: other)
    with pytest.raises(ValueError, match="owned"):
        render_unit(installation.path)


def test_account_selection_requires_explicit_non_root_system_user(monkeypatch):
    import pwd

    root = SimpleNamespace(pw_uid=0, pw_dir="/root")
    user = SimpleNamespace(pw_uid=1000, pw_dir="/home/tester")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: root)
    with pytest.raises(ValueError, match="non-root"):
        module._account(False, None)
    with pytest.raises(ValueError, match="--run-as"):
        module._account(True, None)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: root)
    with pytest.raises(ValueError, match="non-root"):
        module._account(True, "root")
    monkeypatch.setattr(pwd, "getpwnam", lambda name: user)
    assert module._account(True, "tester") is user
    with pytest.raises(ValueError, match="only"):
        module._account(False, "tester")

    def missing(name):
        raise KeyError(name)

    monkeypatch.setattr(pwd, "getpwnam", missing)
    with pytest.raises(ValueError, match="existing"):
        module._account(True, "missing")


def test_install_registers_without_implicit_start_and_is_idempotent(installation):
    manager = installation.manager
    manager.install(installation.path)
    content = manager.unit_path.read_text()
    timestamp = manager.unit_path.stat().st_mtime_ns
    assert stat.S_IMODE(manager.unit_path.stat().st_mode) == 0o644
    assert [call[0][-1] for call in installation.calls] == ["daemon-reload"]
    assert installation.calls[0][0] == [
        "systemctl",
        "--no-ask-password",
        "--no-pager",
        "--user",
        "daemon-reload",
    ]
    manager.install(installation.path)
    assert manager.unit_path.stat().st_mtime_ns == timestamp
    assert manager.unit_path.read_text() == content
    assert list(manager.unit_path.parent.iterdir()) == [manager.unit_path]
    for _, options in installation.calls:
        assert options["timeout"] == module.COMMAND_TIMEOUT
        assert options.get("shell", False) is False


def test_install_now_enables_and_linger_is_explicit(installation):
    manager = installation.manager
    manager.install(installation.path, now=True, linger=True)
    commands = [call[0] for call in installation.calls]
    assert commands[1] == ["loginctl", "--no-ask-password", "enable-linger", "tester"]
    assert commands[2][-3:] == ["enable", "--now", manager.unit_name]
    assert not any(command[0] == "sudo" for command in commands)


def test_only_explicit_replace_updates_a_managed_unit(installation):
    manager = installation.manager
    manager.install(installation.path)
    custom = manager.unit_path.read_text() + "# user addition\n"
    manager.unit_path.write_text(custom)
    with pytest.raises(FileExistsError, match="--replace"):
        manager.install(installation.path)
    assert manager.unit_path.read_text() == custom
    manager.install(installation.path, replace=True)
    assert "# user addition" not in manager.unit_path.read_text()


def test_foreign_units_and_symlinks_are_never_changed(installation):
    manager = installation.manager
    manager.unit_path.parent.mkdir(parents=True)
    foreign = "[Service]\nExecStart=/bin/true\n"
    manager.unit_path.write_text(foreign)
    for action in (
        lambda: manager.install(installation.path, replace=True),
        manager.uninstall,
        lambda: manager.control("start"),
    ):
        with pytest.raises(ValueError, match="not managed"):
            action()
    assert manager.unit_path.read_text() == foreign
    assert not installation.calls
    manager.unit_path.unlink()
    target = installation.root / "other.service"
    target.write_text(MANAGED_MARKER + "\n" + foreign)
    manager.unit_path.symlink_to(target)
    with pytest.raises(ValueError, match="symlinked"):
        manager.uninstall()
    assert target.exists()
    assert not installation.calls


def test_mutations_require_correct_user_and_scope(installation, monkeypatch):
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    with pytest.raises(PermissionError, match="non-root"):
        installation.manager.install(installation.path)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    with pytest.raises(PermissionError, match="root"):
        ServiceManager(system=True).install(installation.path, run_as="tester")
    with pytest.raises(ValueError, match="user services"):
        ServiceManager(system=True).enable_linger()
    assert not installation.calls


def test_uninstall_stops_disables_and_retains_config(installation):
    manager = installation.manager
    manager.install(installation.path)
    original = installation.path.read_bytes()
    installation.calls.clear()
    manager.uninstall()
    commands = [call[0] for call in installation.calls]
    assert commands[0][-3:] == ["disable", "--now", manager.unit_name]
    assert commands[1][-1] == "daemon-reload"
    assert not manager.unit_path.exists()
    assert installation.path.read_bytes() == original


def test_status_is_structured_and_read_only_for_missing_unit(installation, monkeypatch):
    def show(arguments, **kwargs):
        installation.calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(
            arguments,
            1,
            stdout="LoadState=not-found\nActiveState=inactive\nMainPID=0\n",
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", show)
    status = installation.manager.status()
    assert status["LoadState"] == "not-found"
    assert status["MainPID"] == "0"
    assert set(status) == set(module.STATUS_PROPERTIES)
    assert all(isinstance(value, str) for value in status.values())
    assert not installation.manager.unit_path.exists()
    command = installation.calls[0][0]
    assert "show" in command
    assert "--property=" + ",".join(module.STATUS_PROPERTIES) in command


def test_logs_are_bounded_and_use_journalctl_without_shell(installation):
    assert installation.manager.logs(25) == ""
    command, options = installation.calls[0]
    assert command[0] == "journalctl"
    assert "--user" in command
    assert command[-4:] == ["--unit", "kt-mcp.service", "--lines", "25"]
    assert options["timeout"] == module.COMMAND_TIMEOUT
    for lines in (0, 1001, True):
        with pytest.raises(ValueError, match="1 to 1000"):
            installation.manager.logs(lines)


def test_failures_propagate_without_echoing_secrets(installation, monkeypatch):
    manager = installation.manager
    manager.install(installation.path)

    def denied(arguments, **kwargs):
        return subprocess.CompletedProcess(
            arguments, 1, stdout="", stderr="Access denied " + TOKEN
        )

    monkeypatch.setattr(module.subprocess, "run", denied)
    with pytest.raises(ServiceError, match="permission denied") as error:
        manager.control("restart")
    assert TOKEN not in str(error.value)
    assert manager.unit_path.exists()

    def timeout(arguments, **kwargs):
        raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", timeout)
    with pytest.raises(ServiceError, match="timed out"):
        manager.control("stop")


def test_cli_supports_common_options_and_explicit_enable(installation, capsys):
    assert (
        module.main(
            [
                "--name",
                "kt-mcp-demo",
                "install",
                "--config",
                str(installation.path),
                "--now",
            ]
        )
        == 0
    )
    assert installation.calls[-1][0][-3:] == ["enable", "--now", "kt-mcp-demo.service"]
    assert TOKEN not in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        module.main(["start", "--enable"])
    assert error.value.code == 2


def test_cli_status_returns_json(installation, capsys):
    assert module.main(["status"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == set(module.STATUS_PROPERTIES)
