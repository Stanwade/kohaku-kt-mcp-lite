"""Update failures preserve real files; only external service/package work is faked."""

import importlib.metadata
import json
import os
import signal
import stat
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import kt_mcp
import kt_mcp.update as module

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux updater")
TOKEN = "update-fixture-token-" + "a" * 32


def tree(path):
    result = {}
    for item in path.rglob("*"):
        name = str(item.relative_to(path))
        if item.is_symlink():
            result[name] = ("link", os.readlink(item))
        elif item.is_file():
            result[name] = (
                "file",
                stat.S_IMODE(item.stat().st_mode),
                item.read_bytes(),
            )
    return result


class UpdateFixture:
    """A filesystem installation plus a small external systemd test boundary."""

    def __init__(self, root, monkeypatch):
        self.root = root
        self.prefix = root / 'installation 中文 % $ "quote"'
        self.prefix.mkdir(mode=0o700)
        self.venv = self.prefix / "venv"
        (self.venv / "bin").mkdir(parents=True)
        self.python = self.venv / "bin/python"
        self.python.symlink_to(sys.executable)
        (self.venv / ".kt-mcp-managed").write_text(module.MARKER + "\n")
        (self.venv / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        (self.venv / "package.json").write_text(
            json.dumps({"version": "0.2.3", "dependency": "original"})
        )
        (self.venv / "user-package.bin").write_bytes(b"user package\x00\xff")
        (self.venv / "user-package.bin").chmod(0o640)
        (self.venv / "user-package-link").symlink_to("user-package.bin")
        (self.venv / "bin/user-tool").write_text("#!/bin/sh\nexit 0\n")
        (self.venv / "bin/user-tool").chmod(0o750)
        self.workspace = root / "workspace 中文"
        self.workspace.mkdir()
        self.config = root / "private config.json"
        self.config.write_text(
            json.dumps({"version": 1, "workspace": str(self.workspace), "token": TOKEN})
        )
        self.config.chmod(0o600)
        self.unit = root / "kt-mcp-update-fixture.service"
        self.unit.write_bytes(b"# External systemd boundary fixture\n")
        self.unit.chmod(0o644)
        self.installation = module.Installation(
            self.prefix, self.python, Path(sys._base_executable).resolve(), "0.2.3"
        )
        self.release = module.releases.Release("9.8.7", {})
        self.directory = self.prefix / module.TRANSACTION
        self.directory.mkdir(mode=0o700)
        self.bundle = root / "verified bundle"
        self.bundle.mkdir()
        (self.bundle / self.release.wheel_name).write_bytes(b"fixture wheel")
        self.wheels = root / "wheelhouse"
        self.wheels.mkdir()
        self.status = {
            "LoadState": "loaded",
            "ActiveState": "active",
            "MainPID": "1234",
            "UnitFileState": "enabled",
        }
        self.name = "kt-mcp-update-fixture"
        self.events = []
        self.other_active = False
        self.fail_new_start = False
        self.fail_old_start = False
        self.original = tree(self.venv)
        self.original_config = self.config.read_bytes()
        self.original_unit = self.unit.read_bytes()
        monkeypatch.setattr(module.services, "find_services", self.find)
        monkeypatch.setattr(module.services, "inspect_service", self.inspect)
        monkeypatch.setattr(module.services, "stop_service", self.stop)
        monkeypatch.setattr(module.services, "start_service", self.start)
        monkeypatch.setattr(module.services, "assert_external_process", lambda b: None)
        # Account/platform validation is covered separately. This fixture works
        # both for root CI containers and ordinary Ubuntu test accounts.
        monkeypatch.setattr(module, "_validate_installation", lambda installation: None)
        monkeypatch.setattr(
            module, "_prepare", lambda *args: (self.bundle, self.wheels)
        )
        monkeypatch.setattr(module, "_install", self.install)
        monkeypatch.setattr(module, "_verify", self.verify)
        # The copied worker normally exits the process, so its signal handlers
        # must not become handlers of the pytest runner.
        monkeypatch.setattr(module.signal, "signal", lambda *args: None)

    def state(self):
        return {
            "schema": 1,
            "installation": {
                key: str(value) for key, value in asdict(self.installation).items()
            },
            "release": asdict(self.release),
            "name": self.name,
            "phase": "preparing",
            "service": None,
            "stop_requested": False,
            "backup_ready": False,
        }

    def binding(self):
        info = self.config.stat()
        unit_info = self.unit.stat()
        return module.services.BoundService(
            name=self.name,
            system=False,
            unit_path=self.unit,
            python=self.python,
            config_path=self.config,
            run_as=None,
            owner_uid=info.st_uid,
            owner_gid=info.st_gid,
            unit_bytes=self.unit.read_bytes(),
            unit_hash=module.releases.sha256(self.unit),
            status=dict(self.status),
            unit_mode=stat.S_IMODE(unit_info.st_mode),
            unit_owner_uid=unit_info.st_uid,
            unit_owner_gid=unit_info.st_gid,
        )

    def find(self, python):
        assert python == self.python
        bindings = [self.binding()]
        if self.other_active:
            bindings.append(
                replace(
                    self.binding(),
                    name="kt-mcp-another-instance",
                    status={**self.status, "ActiveState": "active", "MainPID": "9876"},
                )
            )
        return bindings

    def inspect(self, name, system, *, expected_python, expected_config):
        assert (name, system) == (self.name, False)
        assert expected_python == self.python
        assert expected_config == self.config
        return self.binding()

    def stop(self, binding):
        assert binding.name == self.name
        self.events.append(("stop",))
        self.status.update(ActiveState="inactive", MainPID="0")

    def start(self, binding, *, expected_version):
        assert binding.name == self.name
        assert self.package()["version"] == expected_version
        self.events.append(("start", expected_version))
        self.status.update(ActiveState="active", MainPID="4321")
        if (expected_version == self.release.version and self.fail_new_start) or (
            expected_version == self.installation.version and self.fail_old_start
        ):
            self.status.update(ActiveState="failed", MainPID="0")
            raise module.services.ManagedServiceError("Fixture service startup failed")

    def package(self, prefix=None):
        return json.loads(((prefix or self.prefix) / "venv/package.json").read_text())

    def install(self, installation, bundle, wheelhouse, *, prefix=None):
        assert installation == self.installation
        target = (prefix or self.prefix) / "venv"
        if prefix is None:
            assert self.status["ActiveState"] == "inactive"
        self.events.append(("install", str(target)))
        (target / "package.json").write_text(
            json.dumps({"version": self.release.version, "dependency": "replacement"})
        )
        (target / "new-only-file").write_bytes(b"new package data")

    def verify(self, python, version, **kwargs):
        package = json.loads((python.parent.parent / "package.json").read_text())
        assert package["version"] == version
        expected = "original" if version == self.installation.version else "replacement"
        assert package["dependency"] == expected
        self.events.append(("verify", version))

    def assert_external_unchanged(self):
        assert self.config.read_bytes() == self.original_config
        assert stat.S_IMODE(self.config.stat().st_mode) == 0o600
        assert self.unit.read_bytes() == self.original_unit
        assert self.status["UnitFileState"] == "enabled"

    def run_worker(self, *arguments):
        previous = Path.cwd()
        try:
            return module.worker_main(self.directory, list(arguments))
        finally:
            os.chdir(previous)

    def failed_install(self, installation, bundle, wheelhouse, *, prefix=None):
        self.install(installation, bundle, wheelhouse, prefix=prefix)
        raise module.UpdateError("Fixture installation failed after replacing files")


@pytest.fixture
def update_fixture(tmp_path, monkeypatch):
    return UpdateFixture(tmp_path, monkeypatch)


def forbid(*args, **kwargs):
    raise AssertionError("read-only command performed an update operation")


def test_help_never_discovers_or_downloads(monkeypatch, capsys):
    monkeypatch.setattr(module, "detect_installation", forbid)
    monkeypatch.setattr(module.releases, "latest_release", forbid)
    monkeypatch.setattr(module, "_handoff", forbid)
    with pytest.raises(SystemExit) as finished:
        module.main(["--help"])
    assert finished.value.code == 0
    assert "--check" in capsys.readouterr().out


def test_check_works_without_managed_installation_and_never_controls_service(
    monkeypatch, capsys
):
    monkeypatch.setattr(kt_mcp, "__version__", "1.2.3")
    monkeypatch.setattr(module, "detect_installation", forbid)
    monkeypatch.setattr(module, "_handoff", forbid)
    monkeypatch.setattr(module.services, "find_services", forbid)
    monkeypatch.setattr(module.services, "stop_service", forbid)
    monkeypatch.setattr(module.services, "start_service", forbid)
    monkeypatch.setattr(
        module.releases,
        "latest_release",
        lambda *args: module.releases.Release("1.2.10", {}),
    )
    assert module.main(["--check"]) == 0
    output = capsys.readouterr().out
    assert "1.2.3 → 1.2.10" in output
    assert "kt-mcp update" in output


@pytest.mark.parametrize("target", ["1.2.3", "1.2.2"])
def test_current_or_newer_installation_does_not_handoff(monkeypatch, target):
    monkeypatch.setattr(kt_mcp, "__version__", "1.2.3")
    monkeypatch.setattr(module, "detect_installation", lambda version: object())
    monkeypatch.setattr(module, "_handoff", forbid)
    monkeypatch.setattr(module.services, "find_services", forbid)
    monkeypatch.setattr(
        module.releases,
        "latest_release",
        lambda *args: module.releases.Release(target, {}),
    )
    assert module.main([]) == 0


def test_explicit_downgrade_fails_without_service_action(monkeypatch, capsys):
    monkeypatch.setattr(kt_mcp, "__version__", "1.2.3")
    monkeypatch.setattr(module, "detect_installation", lambda version: object())
    monkeypatch.setattr(module, "_handoff", forbid)
    monkeypatch.setattr(module.services, "find_services", forbid)
    monkeypatch.setattr(
        module.releases,
        "latest_release",
        lambda *args: module.releases.Release("1.2.2", {}),
    )
    assert module.main(["--version", "1.2.2"]) == 1
    assert "Downgrades" in capsys.readouterr().err


@pytest.fixture
def detectable_installation(tmp_path, monkeypatch):
    prefix = tmp_path / "selected custom prefix"
    venv = prefix / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    marker = venv / ".kt-mcp-managed"
    marker.write_text(module.MARKER + "\n")
    configuration = venv / "pyvenv.cfg"
    configuration.write_text("include-system-site-packages = false\n")
    package = venv / "lib/site-packages/kt_mcp"
    package.mkdir(parents=True)
    source = package / "update.py"
    source.write_text("# location-only fixture\n")
    metadata_path = package.parent / "kohaku_kt_mcp-0.2.3.dist-info"
    metadata_path.mkdir()
    (metadata_path / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: kohaku-kt-mcp\nVersion: 0.2.3\n"
    )
    (metadata_path / "direct_url.json").write_text('{"url":"file:///fixture.whl"}')
    distribution = importlib.metadata.PathDistribution(metadata_path)
    uid = os.geteuid()
    if uid == 0:
        for path in (prefix, venv, marker, configuration):
            os.chown(path, 1000, 1000)
        monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(module.sys, "prefix", str(venv))
    monkeypatch.setattr(module.sys, "base_prefix", "/base/python")
    monkeypatch.setattr(module, "__file__", str(source))
    monkeypatch.setattr(
        module.importlib.metadata, "distribution", lambda name: distribution
    )
    monkeypatch.setattr(
        module,
        "_command",
        lambda *args, **kwargs: f"{sys.version_info.major}.{sys.version_info.minor}",
    )
    return SimpleNamespace(prefix=prefix, venv=venv, metadata=metadata_path)


def test_detection_uses_running_venv_not_changed_xdg_or_path(
    detectable_installation, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", "/different/user/default")
    monkeypatch.setenv("PATH", "/not/the/original/python")
    actual = module.detect_installation("0.2.3")
    assert actual.prefix == detectable_installation.prefix
    assert actual.python == detectable_installation.venv / "bin/python"
    assert actual.base_python == Path(sys._base_executable).resolve()


@pytest.mark.parametrize("kind", ["source", "editable", "metadata", "marker"])
def test_detection_rejects_unmanaged_or_mismatched_installation(
    detectable_installation, monkeypatch, kind
):
    item = detectable_installation
    if kind == "source":
        monkeypatch.setattr(module, "__file__", str(item.prefix / "checkout/update.py"))
    elif kind == "editable":
        (item.metadata / "direct_url.json").write_text('{"dir_info":{"editable":true}}')
    elif kind == "metadata":
        (item.metadata / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: kohaku-kt-mcp\nVersion: 0.2.2\n"
        )
    else:
        (item.venv / ".kt-mcp-managed").write_text("another installer\n")
    with pytest.raises(module.UpdateError):
        module.detect_installation("0.2.3")


def test_changed_base_python_requires_new_prefix(detectable_installation, monkeypatch):
    monkeypatch.setattr(module, "_command", lambda *args, **kwargs: "99.1")
    with pytest.raises(module.UpdateError, match="new install prefix"):
        module.detect_installation("0.2.3")


@pytest.mark.parametrize("active", [True, False])
def test_update_preserves_configuration_unit_extra_packages_and_service_intent(
    update_fixture, active
):
    fixture = update_fixture
    if not active:
        fixture.status.update(ActiveState="inactive", MainPID="0")
    state = fixture.state()
    module._apply(fixture.installation, fixture.release, fixture.directory, state)
    assert state["phase"] == "committed"
    assert fixture.package()["version"] == fixture.release.version
    assert tree(fixture.directory / "backup") == fixture.original
    current = tree(fixture.venv)
    for name in ("user-package.bin", "user-package-link", "bin/user-tool"):
        assert current[name] == fixture.original[name]
    fixture.assert_external_unchanged()
    assert fixture.status["ActiveState"] == ("active" if active else "inactive")
    assert (
        ("start", fixture.release.version) in fixture.events
        if active
        else not any(event[0] in {"stop", "start"} for event in fixture.events)
    )


@pytest.mark.parametrize("failure", ["discovery", "download", "preflight"])
def test_preparation_failure_never_stops_or_changes_the_running_installation(
    update_fixture, monkeypatch, capsys, failure
):
    fixture = update_fixture
    original_prepare = PREPARE
    monkeypatch.setattr(module, "_prepare", original_prepare)
    if failure == "discovery":

        def discovery(*args):
            raise module.services.ManagedServiceError("Fixture service file is missing")

        monkeypatch.setattr(module.services, "find_services", discovery)

    def download(*args):
        if failure == "download":
            raise module.releases.ReleaseError("Fixture download failed")
        return fixture.bundle

    def verify(python, version, **kwargs):
        assert python.parent.parent != fixture.venv
        raise module.UpdateError("Fixture incompatible candidate dependency")

    monkeypatch.setattr(module.releases, "download_release", download)
    monkeypatch.setattr(module, "_command", lambda *args, **kwargs: "")
    monkeypatch.setattr(module, "_verify", verify)
    module._write_state(fixture.directory, fixture.state())
    assert fixture.run_worker() == 1
    assert tree(fixture.venv) == fixture.original
    fixture.assert_external_unchanged()
    assert fixture.status["MainPID"] == "1234"
    assert not any(event[0] in {"stop", "start"} for event in fixture.events)
    assert not fixture.directory.exists()
    output = capsys.readouterr().out
    assert "Update stopped before installation" in output
    assert "installed files and services are unchanged" in output
    assert "original service state restored" not in output


PREPARE = module._prepare


@pytest.mark.parametrize("failure", ["install", "start", "interrupt"])
def test_worker_restores_complete_venv_and_original_service_after_failure(
    update_fixture, monkeypatch, capsys, failure
):
    fixture = update_fixture
    if failure == "start":
        fixture.fail_new_start = True
    else:

        def install(*args, **kwargs):
            fixture.install(*args, **kwargs)
            if failure == "interrupt":
                raise KeyboardInterrupt
            raise module.UpdateError("Fixture installation failed")

        monkeypatch.setattr(module, "_install", install)
    module._write_state(fixture.directory, fixture.state())
    assert fixture.run_worker() == 1
    assert tree(fixture.venv) == fixture.original
    fixture.assert_external_unchanged()
    assert fixture.status["ActiveState"] == "active"
    assert ("start", fixture.installation.version) in fixture.events
    assert not fixture.directory.exists()
    assert TOKEN not in capsys.readouterr().out


def test_failure_restores_files_without_starting_an_originally_stopped_service(
    update_fixture, monkeypatch
):
    fixture = update_fixture
    fixture.status.update(ActiveState="inactive", MainPID="0")
    monkeypatch.setattr(module, "_install", fixture.failed_install)
    module._write_state(fixture.directory, fixture.state())
    assert fixture.run_worker() == 1
    assert tree(fixture.venv) == fixture.original
    assert fixture.status["ActiveState"] == "inactive"
    assert not any(event[0] == "start" for event in fixture.events)
    fixture.assert_external_unchanged()


def test_external_start_during_install_is_stopped_before_restoring_files(
    update_fixture, monkeypatch
):
    fixture = update_fixture

    def externally_started(*args, **kwargs):
        fixture.install(*args, **kwargs)
        fixture.status.update(ActiveState="active", MainPID="7654")

    original_replace = os.replace

    def replace_when_stopped(source, target):
        if Path(source) in {fixture.venv, fixture.directory / "backup"}:
            assert fixture.status["ActiveState"] == "inactive"
            assert fixture.status["MainPID"] == "0"
        return original_replace(source, target)

    monkeypatch.setattr(module, "_install", externally_started)
    monkeypatch.setattr(module.os, "replace", replace_when_stopped)
    module._write_state(fixture.directory, fixture.state())
    assert fixture.run_worker() == 1
    assert tree(fixture.venv) == fixture.original
    assert fixture.status["ActiveState"] == "active"


def prepared_recovery(fixture, *, phase="installing"):
    state = fixture.state()
    state.update(
        phase=phase,
        service=module._service_record(fixture.binding()),
        stop_requested=True,
        backup_ready=True,
    )
    fixture.stop(fixture.binding())
    module.shutil.copytree(fixture.venv, fixture.directory / "backup", symlinks=True)
    fixture.install(fixture.installation, fixture.bundle, fixture.wheels)
    module._write_state(fixture.directory, state)
    return state


def test_recovery_does_not_move_files_used_by_another_instance(update_fixture):
    fixture = update_fixture
    state = prepared_recovery(fixture)
    fixture.other_active = True
    replacement = tree(fixture.venv)
    with pytest.raises(module.UpdateError, match="Another service"):
        module._rollback(fixture.installation, fixture.directory, state)
    assert tree(fixture.venv) == replacement
    assert tree(fixture.directory / "backup") == fixture.original
    assert not (fixture.directory / "failed-venv").exists()


def test_recovery_resumes_between_the_two_venv_renames(update_fixture):
    fixture = update_fixture
    prepared_recovery(fixture, phase="restoring")
    os.replace(fixture.venv, fixture.directory / "failed-venv")
    assert not fixture.venv.exists()
    assert fixture.run_worker("--recover") == 0
    assert tree(fixture.venv) == fixture.original
    assert fixture.status["ActiveState"] == "active"
    fixture.assert_external_unchanged()
    assert not fixture.directory.exists()


def test_failed_recovery_keeps_saved_files_and_can_be_retried(
    update_fixture, monkeypatch, capsys
):
    fixture = update_fixture
    fixture.fail_old_start = True
    monkeypatch.setattr(module, "_install", fixture.failed_install)
    module._write_state(fixture.directory, fixture.state())
    assert fixture.run_worker() == 1
    assert tree(fixture.venv) == fixture.original
    assert (fixture.directory / "failed-venv/new-only-file").exists()
    assert (
        json.loads((fixture.directory / "state.json").read_text())["phase"]
        == "restored"
    )
    output = capsys.readouterr().out
    assert "Recovery needs attention" in output
    assert module.recovery_command(fixture.installation) in output
    assert TOKEN not in output
    fixture.fail_old_start = False
    assert fixture.run_worker("--recover") == 0
    assert tree(fixture.venv) == fixture.original
    assert fixture.status["ActiveState"] == "active"
    assert not fixture.directory.exists()


@pytest.mark.parametrize("damage", ["unknown-phase", "missing-flag", "wrong-flag"])
def test_damaged_journal_never_discards_the_saved_environment(update_fixture, damage):
    fixture = update_fixture
    state = prepared_recovery(fixture)
    if damage == "unknown-phase":
        state["phase"] = "unknown-future-phase"
    elif damage == "missing-flag":
        del state["backup_ready"]
    else:
        state["backup_ready"] = "false"
    module._write_state(fixture.directory, state)
    replacement = tree(fixture.venv)
    assert fixture.run_worker("--recover") == 1
    assert tree(fixture.venv) == replacement
    assert tree(fixture.directory / "backup") == fixture.original
    assert (fixture.directory / "state.json").exists()


def test_committed_journal_only_cleans_up_and_never_restores_old_packages(
    update_fixture,
):
    fixture = update_fixture
    state = prepared_recovery(fixture, phase="committed")
    fixture.events.clear()
    assert fixture.run_worker("--recover") == 0
    assert fixture.package()["version"] == fixture.release.version
    assert fixture.events == []
    assert not fixture.directory.exists()
    assert state["backup_ready"]


def test_committed_cleanup_failure_retains_recovery_entry_and_new_running_version(
    update_fixture, monkeypatch, capsys
):
    fixture = update_fixture
    runtime = fixture.directory / "runtime"
    runtime.mkdir()
    (runtime / "update.py").write_text("# saved recovery runtime\n")
    worker = fixture.directory / "worker.py"
    worker.write_text("# saved recovery entry\n")
    original_rmtree = module.shutil.rmtree
    refuse_backup_cleanup = True

    def cleanup(path, *args, **kwargs):
        if Path(path) == fixture.directory / "backup" and refuse_backup_cleanup:
            raise PermissionError("fixture backup cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "rmtree", cleanup)
    module._write_state(fixture.directory, fixture.state())
    assert fixture.run_worker() == 1
    assert fixture.package()["version"] == fixture.release.version
    assert fixture.status["ActiveState"] == "active"
    assert ("start", fixture.installation.version) not in fixture.events
    fixture.assert_external_unchanged()
    assert worker.read_text() == "# saved recovery entry\n"
    assert (runtime / "update.py").read_text() == "# saved recovery runtime\n"
    assert tree(fixture.directory / "backup") == fixture.original
    assert (
        json.loads((fixture.directory / "state.json").read_text())["phase"]
        == "committed"
    )
    output = capsys.readouterr().out
    assert "Update completed; cleanup needs attention" in output
    assert "Previous installation retained" not in output
    assert module.recovery_command(fixture.installation) in output

    refuse_backup_cleanup = False
    fixture.events.clear()
    assert fixture.run_worker("--recover") == 0
    assert fixture.package()["version"] == fixture.release.version
    assert fixture.status["ActiveState"] == "active"
    assert fixture.events == []
    assert not fixture.directory.exists()


def test_lock_is_exclusive_and_reusable_after_owner_exits(tmp_path):
    first = module._lock(tmp_path)
    try:
        with pytest.raises(module.UpdateError, match="already running"):
            module._lock(tmp_path)
    finally:
        os.close(first)
    second = module._lock(tmp_path)
    os.close(second)
    assert stat.S_IMODE((tmp_path / ".kt-mcp-update.lock").stat().st_mode) == 0o600


def test_handoff_copies_a_private_worker_that_runs_without_site_packages(
    update_fixture, monkeypatch
):
    fixture = update_fixture
    fixture.directory.rmdir()
    monkeypatch.setenv("KT_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("TUNNEL_TOKEN", "fixture-tunnel-token")

    class HandoffInspected(BaseException):
        pass

    def inspect_exec(executable, arguments, environment):
        assert executable == str(fixture.installation.base_python)
        assert arguments[:2] == [executable, "-I"]
        descriptor = int(arguments[arguments.index("--lock-fd") + 1])
        assert os.get_inheritable(descriptor)
        assert stat.S_IMODE(fixture.directory.stat().st_mode) == 0o700
        state_path = fixture.directory / "state.json"
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
        state = json.loads(state_path.read_text())
        assert state["installation"]["prefix"] == str(fixture.prefix)
        assert state["phase"] == "preparing"
        assert "KT_MCP_TOKEN" not in environment
        assert "TUNNEL_TOKEN" not in environment
        result = subprocess.run(
            [executable, "-I", "-S", arguments[2], "--help"],
            cwd=fixture.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "--recover" in result.stdout
        raise HandoffInspected

    monkeypatch.setattr(module.os, "execve", inspect_exec)
    with pytest.raises(HandoffInspected):
        module._handoff(fixture.installation, fixture.release, fixture.name)
    assert tree(fixture.venv) == fixture.original
    fixture.assert_external_unchanged()
    assert not fixture.directory.exists()
    descriptor = module._lock(fixture.prefix)
    os.close(descriptor)


def test_handoff_does_not_overwrite_an_unfinished_transaction(update_fixture):
    fixture = update_fixture
    saved = fixture.directory / "saved-files"
    saved.write_bytes(b"unfinished update recovery data")
    with pytest.raises(module.UpdateError, match="unfinished update"):
        module._handoff(fixture.installation, fixture.release, fixture.name)
    assert saved.read_bytes() == b"unfinished update recovery data"
    assert tree(fixture.venv) == fixture.original


@pytest.mark.parametrize("kind", ["different-file", "competing-lock"])
def test_worker_cannot_recover_or_cleanup_without_the_correct_exclusive_lock(
    update_fixture, kind
):
    fixture = update_fixture
    prepared_recovery(fixture)
    held = module._lock(fixture.prefix)
    supplied = None
    try:
        if kind == "different-file":
            supplied = os.open(
                fixture.prefix / "unrelated", os.O_CREAT | os.O_RDWR, 0o600
            )
        else:
            supplied = os.open(fixture.prefix / ".kt-mcp-update.lock", os.O_RDWR)
        replacement = tree(fixture.venv)
        assert fixture.run_worker("--recover", "--lock-fd", str(supplied)) == 1
        assert tree(fixture.venv) == replacement
        assert tree(fixture.directory / "backup") == fixture.original
        assert (fixture.directory / "state.json").exists()
    finally:
        if supplied is not None:
            os.close(supplied)
        os.close(held)


def test_config_changed_during_download_prevents_pause_and_install(
    update_fixture, monkeypatch
):
    fixture = update_fixture

    def prepare(*args):
        fixture.config.write_bytes(fixture.original_config + b"\n")
        return fixture.bundle, fixture.wheels

    monkeypatch.setattr(module, "_prepare", prepare)
    with pytest.raises(module.UpdateError, match="changed"):
        module._apply(
            fixture.installation, fixture.release, fixture.directory, fixture.state()
        )
    assert tree(fixture.venv) == fixture.original
    assert fixture.events == []
    assert fixture.config.read_bytes() == fixture.original_config + b"\n"
    assert fixture.status["MainPID"] == "1234"


def test_offline_environment_removes_hidden_pip_routing_and_credentials(
    tmp_path, monkeypatch
):
    for key in (
        "PIP_TARGET",
        "PIP_PREFIX",
        "PIP_USER",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
    ):
        monkeypatch.setenv(key, "fixture-sensitive-value")
    monkeypatch.setenv("KT_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("TUNNEL_TOKEN", "fixture-tunnel-token")
    wheelhouse = tmp_path / "wheels 中文 % with spaces"
    environment = module._offline_environment(wheelhouse)
    assert environment["PIP_CONFIG_FILE"] == os.devnull
    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["PIP_FIND_LINKS"] == wheelhouse.as_uri()
    assert not any(value.isspace() for value in environment["PIP_FIND_LINKS"])
    assert not any("fixture-sensitive-value" in value for value in environment.values())
    assert "KT_MCP_TOKEN" not in environment
    assert "TUNNEL_TOKEN" not in environment


def test_subprocess_failure_does_not_expose_package_index_credentials(tmp_path):
    secret = "https://username:fixture-secret@private-index.invalid"
    with pytest.raises(module.UpdateError) as failure:
        module._command(
            [
                sys.executable,
                "-I",
                "-c",
                f"import sys; sys.stderr.write({secret!r}); sys.exit(3)",
            ],
            cwd=tmp_path,
            label="Dependency download",
            timeout=10,
        )
    assert "Dependency download failed" in str(failure.value)
    assert secret not in str(failure.value)


def test_timeout_reaps_installer_descendants_before_recovery_can_begin(tmp_path):
    started = tmp_path / "descendant.pid"
    wrote = tmp_path / "late-venv-write"
    child = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(started)!r}).write_text(str(os.getpid())); "
        f"time.sleep(20); Path({str(wrote)!r}).write_text('unsafe late write')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-I', '-c', {child!r}]); time.sleep(30)"
    )
    try:
        with pytest.raises(module.UpdateError, match="could not complete"):
            module._command(
                [sys.executable, "-I", "-c", parent],
                cwd=tmp_path,
                timeout=0.8,
                label="Installer fixture",
            )
        assert started.exists(), "fixture child never reached its write phase"
        pid = int(started.read_text())
        status_path = Path(f"/proc/{pid}/status")
        if status_path.exists():
            status = status_path.read_text()
            assert "State:\tZ" in status or "State:\tX" in status
        assert not wrote.exists()
    finally:
        if started.exists():
            try:
                os.kill(int(started.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
