import errno
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import kt_mcp.managed_service as module
import kt_mcp.service as service_module
from kt_mcp.managed_service import (
    ManagedServiceError,
    assert_external_process,
    find_services,
    inspect_service,
    restore_unit,
    start_service,
    stop_service,
    wait_ready,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux service bindings"
)
TOKEN = "managed-service-test-" + "a" * 32


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("TUNNEL_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    workspace = tmp_path / 'workspace 中文 % $cash "quotes"'
    workspace.mkdir()
    config_path = tmp_path / 'private % $cash "quotes".json'
    config = {
        "version": 1,
        "workspace": str(workspace),
        "port": 18765,
        "mode": "local",
        "token": TOKEN,
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config_path.chmod(0o600)
    python = tmp_path / 'venv % $cash "quotes"' / "bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    info = config_path.stat()
    # Account lookup is independent of the runner (including root in containers).
    # Ownership checks below still inspect actual files; account rejection has
    # its own tests without this replacement.
    account = SimpleNamespace(
        pw_uid=info.st_uid,
        pw_gid=info.st_gid,
        pw_name="tester",
        pw_dir=str(tmp_path / "home"),
    )
    monkeypatch.setattr(module, "_account", lambda system, run_as: account)
    monkeypatch.setattr(service_module, "_account", lambda system, run_as: account)
    name = "kt-mcp-test"
    path = module._unit_directory(False) / (name + ".service")
    path.parent.mkdir(parents=True)
    content = service_module.render_unit(config_path, python=python).encode()
    path.write_bytes(content)
    path.chmod(0o644)
    status = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "UnitFileState": "disabled",
        "MainPID": "0",
        "NRestarts": "0",
        "ExecMainStatus": "0",
        "FragmentPath": str(path),
        "DropInPaths": "",
        "NeedDaemonReload": "no",
        "ControlGroup": "",
    }
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        assert arguments[:3] == [
            "systemctl",
            "--no-ask-password",
            "--no-pager",
        ]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert 0 < kwargs["timeout"] <= module.COMMAND_TIMEOUT
        assert not kwargs.get("shell", False)
        if "show" in arguments:
            output = "\n".join(f"{key}={value}" for key, value in status.items())
        else:
            output = ""
        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)

    def allow_controls():
        # All systemctl calls remain fake. Permission rejection is tested below.
        monkeypatch.setattr(module, "_authorize", lambda binding: None)

    def startup(pid=1234, mode="local"):
        config["mode"] = mode
        if mode == "named":
            config["public_origin"] = "https://mcp.example.com"
            config["tunnel_token"] = "test-only-tunnel-token"
        config_path.write_text(json.dumps(config))
        origins = {
            "local": "http://127.0.0.1:18765",
            "named": "https://mcp.example.com",
            "quick": "https://temporary.example.com",
        }
        state = {"pid": pid, "url": origins[mode] + "/mcp/" + TOKEN}
        state_path = config_path.with_suffix(".status.json")
        state_path.write_text(json.dumps(state))
        state_path.chmod(0o600)
        status.update(ActiveState="active", SubState="running", MainPID=str(pid))
        return state_path

    return SimpleNamespace(
        root=tmp_path,
        name=name,
        path=path,
        python=python,
        config_path=config_path,
        config=config,
        content=content,
        status=status,
        account=account,
        calls=calls,
        allow_controls=allow_controls,
        startup=startup,
    )


def bind(installation, **kwargs):
    return inspect_service(installation.name, **kwargs)


def test_binding_round_trips_real_rendered_special_paths(installation):
    binding = bind(
        installation,
        expected_python=installation.python,
        expected_config=installation.config_path,
    )
    assert binding.python == installation.python
    assert binding.config_path == installation.config_path
    assert binding.unit_path == installation.path
    assert binding.unit_bytes == installation.content
    assert binding.unit_hash == hashlib.sha256(installation.content).hexdigest()
    assert binding.owner_uid == installation.account.pw_uid
    assert binding.gid == installation.account.pw_gid
    assert binding.run_as is None
    assert TOKEN.encode() not in binding.unit_bytes


def test_python_matching_uses_venv_directory_not_resolved_binary(installation):
    another = installation.root / "other-venv/bin/python"
    another.parent.mkdir(parents=True)
    another.symlink_to(sys.executable)
    assert another.resolve() == installation.python.resolve()
    with pytest.raises(ManagedServiceError, match="another Python"):
        bind(installation, expected_python=another)
    alias = installation.root / "same-venv-alias"
    alias.symlink_to(installation.python.parent.parent, target_is_directory=True)
    assert bind(installation, expected_python=alias / "bin/python") is not None


def test_config_must_match_selected_instance(installation):
    with pytest.raises(ManagedServiceError, match="another configuration"):
        bind(installation, expected_config=installation.root / "other.json")


def test_missing_unit_is_distinct_from_unloaded_file_or_bus_failure(
    installation, monkeypatch
):
    installation.status["LoadState"] = "not-found"
    with pytest.raises(ManagedServiceError, match="file exists"):
        bind(installation)
    installation.path.unlink()
    assert bind(installation) is None

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("systemctl", 40)

    monkeypatch.setattr(module.subprocess, "run", fail)
    with pytest.raises(ManagedServiceError, match="contact systemd"):
        bind(installation)


@pytest.mark.parametrize(
    "property_name,value",
    [
        ("FragmentPath", "/other/kt-mcp-test.service"),
        ("DropInPaths", "/run/systemd/override.conf"),
        ("NeedDaemonReload", "yes"),
        ("NeedDaemonReload", ""),
        ("LoadState", "masked"),
        ("LoadState", "bad-setting"),
    ],
)
def test_inspection_rejects_unknown_loaded_bindings(installation, property_name, value):
    installation.status[property_name] = value
    with pytest.raises(ManagedServiceError):
        bind(installation)


@pytest.mark.parametrize(
    "before,after",
    [
        (module.MANAGED_MARKER, "# somebody else's unit"),
        ("Type=exec", "Type=exec\nExecStart=/bin/false"),
        ("Type=exec", "Type=exec\nEnvironmentFile=/tmp/unknown"),
        ("Type=exec", "Type=exec\nUser=root"),
        ("ExecStart=:", "ExecStart="),
        ('"--config"', '"extra" "--config"'),
        ("%%", "%n"),
        ("Environment=PYTHONUNBUFFERED=1", "Environment=TOKEN=unsafe"),
        ("KillMode=control-group", "KillMode=process"),
    ],
)
def test_inspection_rejects_unknown_or_overridden_unit_syntax(
    installation, before, after
):
    installation.path.write_bytes(
        installation.content.replace(before.encode(), after.encode(), 1)
    )
    with pytest.raises(ManagedServiceError):
        bind(installation)


def test_private_config_and_unit_file_checks(installation):
    installation.config_path.chmod(0o640)
    with pytest.raises(ManagedServiceError, match="mode 600"):
        bind(installation)
    installation.config_path.chmod(0o600)
    installation.path.chmod(0o666)
    with pytest.raises(ManagedServiceError, match="ownership or write"):
        bind(installation)
    installation.path.chmod(0o644)
    original = installation.path.with_suffix(".original")
    installation.path.rename(original)
    installation.path.symlink_to(original)
    with pytest.raises(ManagedServiceError, match="regular managed"):
        bind(installation)


def test_config_symlink_and_wrong_owner_are_rejected(installation, monkeypatch):
    original = installation.config_path.with_suffix(".original")
    installation.config_path.rename(original)
    installation.config_path.symlink_to(original)
    with pytest.raises(ManagedServiceError):
        bind(installation)
    installation.config_path.unlink()
    original.rename(installation.config_path)
    read = module._read_regular

    def wrong_owner(path, **kwargs):
        data, info = read(path, **kwargs)
        if path == installation.config_path:
            info = SimpleNamespace(st_uid=info.st_uid + 1, st_mode=info.st_mode)
        return data, info

    monkeypatch.setattr(module, "_read_regular", wrong_owner)
    with pytest.raises(ManagedServiceError, match="owned by its user"):
        bind(installation)


@pytest.mark.parametrize("suffix", ["", "/subgroup/child"])
def test_self_or_descendant_cgroup_cannot_stop_service(
    installation, monkeypatch, suffix
):
    installation.status.update(
        MainPID="1234", ControlGroup="/user.slice/kt-mcp.service"
    )
    binding = bind(installation)
    monkeypatch.setattr(
        module.Path,
        "read_text",
        lambda *args, **kwargs: "0::/user.slice/kt-mcp.service" + suffix + "\n",
    )
    with pytest.raises(ManagedServiceError, match="outside the target"):
        assert_external_process(binding)
    assert not any("stop" in arguments for arguments, _ in installation.calls)


def test_cgroup_prefix_is_not_an_ancestry_match(installation, monkeypatch):
    installation.status.update(
        MainPID="1234", ControlGroup="/user.slice/kt-mcp.service"
    )
    binding = bind(installation)
    monkeypatch.setattr(
        module.Path,
        "read_text",
        lambda *args, **kwargs: "0::/user.slice/kt-mcp.service-other\n",
    )
    assert_external_process(binding)


def test_control_rechecks_unit_bytes_before_stop(installation):
    binding = bind(installation)
    installation.allow_controls()
    installation.path.write_bytes(installation.content + b"# external edit\n")
    with pytest.raises(ManagedServiceError, match="binding changed"):
        stop_service(binding)
    assert not any("stop" in arguments for arguments, _ in installation.calls)


def test_damaged_interpreter_does_not_prevent_binding_or_safe_stop(installation):
    installation.python.unlink()
    binding = bind(installation, expected_python=installation.python)
    assert binding.python == installation.python
    installation.allow_controls()
    stop_service(binding)
    assert any(
        arguments[-2:] == ["stop", "kt-mcp-test.service"]
        for arguments, _ in installation.calls
    )


@pytest.mark.parametrize("enabled", ["enabled", "disabled", "enabled-runtime"])
def test_stop_preserves_enable_state_and_only_controls_selected_name(
    installation, enabled
):
    installation.status["UnitFileState"] = enabled
    binding = bind(installation)
    installation.allow_controls()
    stop_service(binding)
    mutations = [
        arguments
        for arguments, _ in installation.calls
        if any(action in arguments for action in ("start", "stop", "enable", "disable"))
    ]
    assert len(mutations) == 1
    assert mutations[0][-2:] == ["stop", "kt-mcp-test.service"]
    assert installation.status["UnitFileState"] == enabled
    assert installation.path.read_bytes() == installation.content


@pytest.mark.parametrize(
    "system,euid,owner",
    [(False, 0, 1000), (False, 1001, 1000), (True, 1000, 1000)],
)
def test_control_requires_matching_account_scope(
    installation, monkeypatch, system, euid, owner
):
    binding = replace(bind(installation), system=system, owner_uid=owner)
    monkeypatch.setattr(module.os, "geteuid", lambda: euid)
    with pytest.raises(ManagedServiceError, match="require"):
        stop_service(binding)
    assert not any("stop" in arguments for arguments, _ in installation.calls)


@pytest.mark.parametrize("mode", ["local", "named", "quick"])
def test_ready_requires_matching_record_and_actual_initialize(
    installation, monkeypatch, mode
):
    installation.startup(mode=mode)
    binding = bind(installation)
    monkeypatch.setattr(module.os, "kill", lambda pid, signal: None)
    initialized = []

    def initialize(port, path, timeout):
        initialized.append((port, path, timeout))
        return "0.2.4"

    monkeypatch.setattr(module, "_initialize", initialize)
    status = wait_ready(binding, expected_version="0.2.4")
    assert status["MainPID"] == "1234"
    assert initialized[0][:2] == (18765, "/mcp/" + TOKEN)
    assert 0 < initialized[0][2] <= 2


def test_stale_pid_does_not_probe_or_report_ready(installation, monkeypatch):
    state_path = installation.startup()
    state = json.loads(state_path.read_text())
    state["pid"] = 9876
    state_path.write_text(json.dumps(state))
    binding = bind(installation)
    calls = []
    monkeypatch.setattr(module, "_initialize", lambda *args: calls.append(args))
    with pytest.raises(ManagedServiceError, match="in time"):
        wait_ready(binding, timeout=0.015)
    assert calls == []


@pytest.mark.parametrize("userinfo", ["@", ":@"])
def test_quick_record_rejects_empty_userinfo(installation, userinfo):
    state_path = installation.startup(mode="quick")
    state = json.loads(state_path.read_text())
    state["url"] = state["url"].replace("https://", "https://" + userinfo)
    state_path.write_text(json.dumps(state))
    with pytest.raises(ManagedServiceError, match="startup has not been confirmed"):
        module._startup(bind(installation), 1234)


def test_wrong_runtime_version_fails_without_returning_url(installation, monkeypatch):
    installation.startup()
    binding = bind(installation)
    monkeypatch.setattr(module.os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(module, "_initialize", lambda *args: "0.2.3")
    with pytest.raises(ManagedServiceError, match="version differs") as failure:
        wait_ready(binding, expected_version="0.2.4")
    assert TOKEN not in str(failure.value)


def test_start_is_not_enable_or_install(installation, monkeypatch):
    binding = bind(installation)
    installation.allow_controls()
    checked = []

    def ready(value, **kwargs):
        checked.append((value, kwargs))
        return {"ActiveState": "active", "MainPID": "1234"}

    monkeypatch.setattr(module, "wait_ready", ready)
    assert start_service(binding, expected_version="0.2.4")["MainPID"] == "1234"
    commands = [arguments for arguments, _ in installation.calls]
    assert any(
        arguments[-2:] == ["start", "kt-mcp-test.service"] for arguments in commands
    )
    assert not any(
        "enable" in arguments or "daemon-reload" in arguments for arguments in commands
    )
    assert checked[0][1]["expected_version"] == "0.2.4"


def test_initialize_is_loopback_plain_http_and_does_not_follow_redirects(monkeypatch):
    calls = []

    class Connection:
        def __init__(self, host, port, timeout):
            calls.append((host, port, timeout))

        def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))

        def getresponse(self):
            return SimpleNamespace(status=302)

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(module.http.client, "HTTPConnection", Connection)
    with pytest.raises(
        ManagedServiceError, match="initialization check failed"
    ) as error:
        module._initialize(18765, "/mcp/" + TOKEN, 1.5)
    assert calls[0] == ("127.0.0.1", 18765, 1.5)
    request = calls[1]
    assert request[0] == "POST"
    assert json.loads(request[2]["body"])["method"] == "initialize"
    assert calls[-1] == "closed"
    assert TOKEN not in str(error.value)


def test_restore_only_own_replacement_and_never_changes_enabled_state(installation):
    binding = bind(installation)
    installation.allow_controls()
    replacement = installation.content + b"# known candidate\n"
    installation.path.write_bytes(replacement)
    installation.status["NeedDaemonReload"] = "yes"
    restore_unit(binding, expected_unit_bytes=replacement)
    assert installation.path.read_bytes() == binding.unit_bytes
    assert any(arguments[-1] == "daemon-reload" for arguments, _ in installation.calls)
    assert not any(
        action in arguments
        for arguments, _ in installation.calls
        for action in ("start", "stop", "enable", "disable")
    )


def test_restore_refuses_a_third_party_edit(installation):
    binding = bind(installation)
    installation.allow_controls()
    replacement = installation.content + b"# our replacement\n"
    changed = replacement + b"# another editor\n"
    installation.path.write_bytes(changed)
    with pytest.raises(ManagedServiceError, match="outside this operation"):
        restore_unit(binding, expected_unit_bytes=replacement)
    assert installation.path.read_bytes() == changed
    assert not any("daemon-reload" in arguments for arguments, _ in installation.calls)


def test_restore_preserves_old_mode_after_known_install_replacement(installation):
    installation.path.chmod(0o600)
    binding = bind(installation)
    installation.allow_controls()
    replacement = installation.content + b"# known replacement\n"
    installation.path.write_bytes(replacement)
    installation.path.chmod(0o644)
    restore_unit(binding, expected_unit_bytes=replacement)
    assert installation.path.read_bytes() == binding.unit_bytes
    assert installation.path.stat().st_mode & 0o777 == 0o600


def test_restore_rechecks_permissions_immediately_before_replacement(
    installation, monkeypatch
):
    binding = bind(installation)
    installation.allow_controls()
    replacement = installation.content + b"# our replacement\n"
    installation.path.write_bytes(replacement)
    original_mtime = installation.path.stat().st_mtime_ns
    read = module._read_regular
    reads = 0

    def external_chmod(path, **kwargs):
        nonlocal reads
        if path == installation.path:
            reads += 1
            if reads == 2:
                path.chmod(0o600)
        return read(path, **kwargs)

    monkeypatch.setattr(module, "_read_regular", external_chmod)
    with pytest.raises(ManagedServiceError, match="changed during restoration"):
        restore_unit(binding, expected_unit_bytes=replacement)
    assert installation.path.read_bytes() == replacement
    assert installation.path.stat().st_mode & 0o777 == 0o600
    assert installation.path.stat().st_mtime_ns == original_mtime
    assert not any("daemon-reload" in arguments for arguments, _ in installation.calls)


def test_root_account_is_never_accepted(monkeypatch):
    import pwd

    account = SimpleNamespace(pw_uid=0, pw_gid=0, pw_name="root", pw_dir="/root")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: account)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: account)
    for system, run_as in ((False, None), (True, "root")):
        with pytest.raises(ManagedServiceError, match="ordinary account"):
            module._account(system, run_as)


def test_module_imports_without_site_packages_or_installed_kt_mcp():
    source = Path(module.__file__).resolve()
    program = (
        "import importlib.util,sys; "
        "spec=importlib.util.spec_from_file_location('standalone_service',sys.argv[1]); "
        "module=importlib.util.module_from_spec(spec); "
        "sys.modules[spec.name]=module; spec.loader.exec_module(module); "
        "assert 'mcp' not in sys.modules; "
        "assert 'kt_mcp.service' not in sys.modules; "
        "assert callable(module.inspect_service)"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-I", "-S", "-c", program, str(source)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_external_command_failures_do_not_echo_credentials(installation, monkeypatch):
    def fail(arguments, **kwargs):
        return subprocess.CompletedProcess(
            arguments, 1, stdout="unexpected", stderr="private: " + TOKEN
        )

    monkeypatch.setattr(module.subprocess, "run", fail)
    with pytest.raises(ManagedServiceError) as failure:
        bind(installation)
    assert TOKEN not in str(failure.value)


def test_list_unit_files_accepts_empty_no_match_result(monkeypatch):
    def empty(arguments, **kwargs):
        return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", empty)
    assert module._run(True, "list-unit-files", "kt-mcp*.service") == ""


@pytest.mark.parametrize(
    "action,code,output,error",
    [
        ("list-unit-files", 1, "", "Failed to connect to bus: " + TOKEN),
        ("list-unit-files", 1, "unexpected output", ""),
        ("list-unit-files", 2, "", ""),
        ("list-units", 1, "", ""),
    ],
)
def test_empty_listing_exception_does_not_hide_systemd_errors(
    monkeypatch, action, code, output, error
):
    def fail(arguments, **kwargs):
        return subprocess.CompletedProcess(arguments, code, stdout=output, stderr=error)

    monkeypatch.setattr(module.subprocess, "run", fail)
    with pytest.raises(ManagedServiceError, match="systemd command failed") as failure:
        module._run(True, action, "kt-mcp*.service")
    assert TOKEN not in str(failure.value)


def test_find_services_includes_both_scopes_without_modifying_them(
    installation, monkeypatch
):
    directories = {
        False: installation.path.parent,
        True: installation.root / "system",
    }
    directories[True].mkdir()
    system_name = "kt-mcp-system"
    system_path = directories[True] / (system_name + ".service")
    system_path.write_text(
        service_module.render_unit(
            installation.config_path,
            python=installation.python,
            system=True,
            run_as="tester",
        )
    )
    other_python = installation.root / "another/bin/python"
    other_python.parent.mkdir(parents=True)
    other_python.symlink_to(sys.executable)
    other_path = directories[False] / "kt-mcp-other.service"
    other_path.write_text(
        service_module.render_unit(installation.config_path, python=other_python)
    )
    monkeypatch.setattr(module, "_unit_directory", directories.__getitem__)
    calls = []

    def run(system, action, *args, **kwargs):
        calls.append((system, action, args))
        names = [system_name] if system else [installation.name, "kt-mcp-other"]
        if action in {"list-units", "list-unit-files"}:
            return "".join(name + ".service enabled\n" for name in names)
        assert action == "show"
        status = dict(installation.status)
        status["FragmentPath"] = str(system_path if system else installation.path)
        return "\n".join(f"{key}={value}" for key, value in status.items())

    monkeypatch.setattr(module, "_run", run)
    read = module._read_regular

    def owner(path, **kwargs):
        data, info = read(path, **kwargs)
        if path == system_path:
            info = SimpleNamespace(st_uid=0, st_gid=info.st_gid, st_mode=info.st_mode)
        return data, info

    monkeypatch.setattr(module, "_read_regular", owner)
    found = find_services(installation.python)
    assert {(item.name, item.system) for item in found} == {
        (installation.name, False),
        (system_name, True),
    }
    system_binding = next(item for item in found if item.system)
    assert system_binding.run_as == "tester"
    assert not any(
        action in {"start", "stop", "enable", "disable", "daemon-reload"}
        for _, action, _ in calls
    )


def discovery_candidate(installation, monkeypatch, *, system=False, **overrides):
    directories = {
        False: installation.path.parent,
        True: installation.root / "system",
    }
    directories[True].mkdir()
    name = "kt-mcp-deleted"
    path = directories[system] / (name + ".service")
    missing = dict(
        installation.status,
        LoadState="not-found",
        ActiveState="inactive",
        MainPID="0",
        FragmentPath="",
        DropInPaths="",
        ControlGroup="",
        NeedDaemonReload="no",
    )
    missing.update(overrides)
    statuses = {
        (False, installation.name): installation.status,
        (system, name): missing,
    }
    calls = []

    def run(scope, action, *args, **kwargs):
        calls.append((scope, action, args))
        if action == "list-units":
            return "".join(
                unit + ".service listed\n"
                for candidate_scope, unit in statuses
                if candidate_scope == scope
            )
        if action == "list-unit-files":
            return "" if scope else installation.name + ".service disabled\n"
        assert action == "show", "Discovery attempted to change systemd state"
        status = statuses[scope, args[-1][:-8]]
        return "\n".join(f"{key}={value}" for key, value in status.items())

    monkeypatch.setattr(module, "_unit_directory", directories.__getitem__)
    monkeypatch.setattr(module, "_run", run)
    return SimpleNamespace(path=path, status=missing, calls=calls, name=name)


@pytest.mark.parametrize("system", [False, True])
@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_discovery_ignores_only_absent_stopped_records(
    installation, monkeypatch, system, state
):
    candidate = discovery_candidate(
        installation, monkeypatch, system=system, ActiveState=state
    )
    before = installation.path.read_bytes(), installation.config_path.read_bytes()
    bindings = find_services(installation.python)
    assert [(item.name, item.system) for item in bindings] == [
        (installation.name, False)
    ]
    assert not candidate.path.exists()
    assert (
        installation.path.read_bytes(),
        installation.config_path.read_bytes(),
    ) == before
    assert all(
        action in {"show", "list-units", "list-unit-files"}
        for _, action, _ in candidate.calls
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"ActiveState": "active"},
        {"ActiveState": "activating"},
        {"ActiveState": ""},
        {"MainPID": "1234"},
        {"MainPID": ""},
        {"ControlGroup": "/user.slice/remaining-child"},
        {"ControlGroup": None},
        {"FragmentPath": "/run/systemd/transient/kt-mcp-deleted.service"},
        {"DropInPaths": "/run/systemd/override.conf"},
        {"NeedDaemonReload": "yes"},
    ],
)
def test_discovery_refuses_missing_records_with_unresolved_state(
    installation, monkeypatch, overrides
):
    candidate = discovery_candidate(installation, monkeypatch, **overrides)
    if overrides.get("ControlGroup", "") is None:
        candidate.status.pop("ControlGroup")
    with pytest.raises(ManagedServiceError, match="active or unresolved") as failure:
        find_services(installation.python)
    assert candidate.name in str(failure.value)
    assert all(
        action in {"show", "list-units", "list-unit-files"}
        for _, action, _ in candidate.calls
    )


@pytest.mark.parametrize("kind", ["regular", "link", "broken-link"])
def test_discovery_does_not_skip_existing_unloaded_files(
    installation, monkeypatch, kind
):
    candidate = discovery_candidate(installation, monkeypatch)
    if kind == "regular":
        candidate.path.write_bytes(installation.content)
    else:
        target = (
            installation.path
            if kind == "link"
            else installation.root / "nonexistent-unit"
        )
        candidate.path.symlink_to(target)
    with pytest.raises(ManagedServiceError):
        find_services(installation.python)
    assert candidate.path.lstat()
    assert installation.path.read_bytes() == installation.content


def test_discovery_refuses_unreadable_absence_check(installation, monkeypatch):
    candidate = discovery_candidate(installation, monkeypatch)
    original = Path.lstat

    def denied(path, *args, **kwargs):
        if path == candidate.path:
            raise PermissionError(errno.EACCES, TOKEN, str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(ManagedServiceError, match="EACCES") as failure:
        find_services(installation.python)
    assert repr(str(candidate.path)) in str(failure.value)
    assert TOKEN not in str(failure.value)


def test_discovery_keeps_foreign_unloaded_unit_outside_target_installation(
    installation, monkeypatch
):
    candidate = discovery_candidate(installation, monkeypatch)
    other_python = installation.root / "other-venv/bin/python"
    other_python.parent.mkdir(parents=True)
    other_python.symlink_to(sys.executable)
    candidate.path.write_text(
        service_module.render_unit(installation.config_path, python=other_python)
    )
    original = candidate.path.read_bytes()
    assert [item.name for item in find_services(installation.python)] == [
        installation.name
    ]
    assert candidate.path.read_bytes() == original


def test_discovery_still_rejects_missing_config_for_current_installation(
    installation, monkeypatch
):
    discovery_candidate(installation, monkeypatch)
    installation.config_path.unlink()
    with pytest.raises(ManagedServiceError, match="ENOENT") as failure:
        find_services(installation.python)
    assert repr(str(installation.config_path)) in str(failure.value)


@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.EACCES, errno.ELOOP])
def test_file_read_errors_identify_path_and_errno_without_raw_diagnostics(
    tmp_path, monkeypatch, error_number
):
    path = tmp_path / "config\n\x1b[31m.json"
    original = module.os.open

    def fail(file, *args, **kwargs):
        if file == path:
            raise OSError(error_number, TOKEN, str(path))
        return original(file, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", fail)
    with pytest.raises(ManagedServiceError, match="regular managed") as failure:
        module._read_regular(path)
    message = str(failure.value)
    assert repr(str(path)) in message
    assert errno.errorcode[error_number] in message
    assert TOKEN not in message
    assert "\n" not in message and "\x1b" not in message


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_wait_timeout_is_finite_positive(installation, timeout):
    with pytest.raises(ValueError, match="positive"):
        wait_ready(bind(installation), timeout=timeout)
