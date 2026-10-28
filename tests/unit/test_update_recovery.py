"""Public recovery entry validates and delegates to the existing saved worker."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kt_mcp import update as module

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux updater")


@pytest.fixture
def retained(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("A managed update belongs to an ordinary installing account.")
    prefix = tmp_path / 'installation 中文 $ "quotes"'
    python = prefix / "venv/bin/python"
    python.parent.mkdir(parents=True)
    prefix.chmod(0o700)
    python.symlink_to(Path(sys._base_executable).resolve())
    (prefix / "venv/.kt-mcp-managed").write_text(module.MARKER + "\n")
    (prefix / "venv/pyvenv.cfg").write_text("include-system-site-packages = false\n")
    installation = module.Installation(
        prefix, python, Path(sys._base_executable).resolve(), "0.3.0"
    )
    # A real exec never returns. Retain its exact worker/journal while releasing
    # the inherited descriptor so validation and the later worker can lock it.
    with monkeypatch.context() as setup:
        setup.setattr(
            module.os,
            "execve",
            lambda executable, arguments, environment: os.close(
                int(arguments[arguments.index("--lock-fd") + 1])
            ),
        )
        module._handoff(installation, module.releases.Release("0.4.0", {}), None)
    directory = prefix / module.TRANSACTION
    monkeypatch.setattr(module.sys, "prefix", str(installation.venv))
    monkeypatch.setattr(module.sys, "base_prefix", "/unrelated-base")
    monkeypatch.setattr(
        module, "__file__", str(installation.venv / "lib/kt_mcp/update.py")
    )
    return SimpleNamespace(installation=installation, directory=directory)


def test_recovery_uses_retained_journal_without_distribution_metadata(
    retained, monkeypatch
):
    monkeypatch.setattr(
        module.importlib.metadata,
        "distribution",
        lambda *args: pytest.fail("Recovery must not depend on installed metadata"),
    )
    assert module.recoverable_installation() == retained.installation
    assert module.recovery_argv(retained.installation) == [
        str(retained.installation.base_python),
        "-I",
        str(retained.directory / "worker.py"),
        "--recover",
    ]


def test_recovery_can_start_with_a_complete_backup_and_missing_venv(retained):
    installation = retained.installation
    shutil.copytree(installation.venv, retained.directory / "backup", symlinks=True)
    state_path = retained.directory / "state.json"
    state = json.loads(state_path.read_text())
    state.update(phase="restoring", backup_ready=True)
    module._write_state(retained.directory, state)
    shutil.rmtree(installation.venv)
    assert module.recoverable_installation(installation.prefix) == installation


@pytest.mark.parametrize(
    "damage",
    [
        "journal-mode",
        "journal-symlink",
        "worker-content",
        "runtime-symlink",
        "foreign-prefix",
        "venv-interpreter",
        "marker",
    ],
)
def test_recovery_rejects_unsafe_or_unrelated_retained_files(
    retained, damage, tmp_path
):
    directory = retained.directory
    installation = retained.installation
    journal = directory / "state.json"
    if damage == "journal-mode":
        journal.chmod(0o644)
    elif damage == "journal-symlink":
        other = tmp_path / "other-journal"
        journal.rename(other)
        journal.symlink_to(other)
    elif damage == "worker-content":
        (directory / "worker.py").write_text(
            "raise SystemExit('not a recovery worker')\n"
        )
    elif damage == "runtime-symlink":
        saved = directory / "runtime/update.py"
        other = tmp_path / "other-update.py"
        saved.rename(other)
        saved.symlink_to(other)
    elif damage in {"foreign-prefix", "venv-interpreter"}:
        state = json.loads(journal.read_text())
        if damage == "foreign-prefix":
            foreign = tmp_path / "different installation"
            state["installation"].update(
                prefix=str(foreign), python=str(foreign / "venv/bin/python")
            )
        else:
            state["installation"]["base_python"] = str(installation.python)
        module._write_state(directory, state)
    else:
        (installation.venv / ".kt-mcp-managed").write_text("unrelated environment\n")
    with pytest.raises(module.UpdateError):
        module.recoverable_installation(installation.prefix)
    assert directory.exists(), "Validation must retain unfinished recovery files"


def test_recovery_requires_original_managed_invocation(retained, monkeypatch):
    monkeypatch.setattr(module, "__file__", "/editable/source/kt_mcp/update.py")
    with pytest.raises(module.UpdateError, match="original managed"):
        module.recoverable_installation()
    # An already-running TUI captured the real managed prefix before damage.
    assert (
        module.recoverable_installation(retained.installation.prefix)
        == retained.installation
    )


def test_recovery_rejects_root_before_reading_files(tmp_path, monkeypatch):
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    with pytest.raises(module.UpdateError, match="ordinary installing"):
        module.recoverable_installation(tmp_path)


def test_public_recovery_does_not_check_network_or_prepare_new_update(
    retained, monkeypatch
):
    captured = {}

    def forbidden(*args, **kwargs):
        pytest.fail("Recovery must only delegate to the retained worker")

    def execute(executable, arguments, environment):
        captured.update(
            executable=executable, arguments=arguments, environment=environment
        )

    monkeypatch.setattr(module, "detect_installation", forbidden)
    monkeypatch.setattr(module.releases, "latest_release", forbidden)
    monkeypatch.setattr(module, "_handoff", forbidden)
    monkeypatch.setattr(module.os, "execve", execute)
    monkeypatch.setenv("KT_MCP_TOKEN", "fixture-private-token")
    monkeypatch.setenv("TUNNEL_TOKEN", "fixture-private-tunnel-token")
    assert module.main(["--recover"]) == 0
    assert captured["arguments"] == module.recovery_argv(retained.installation)
    assert captured["executable"] == str(retained.installation.base_python)
    assert not {"KT_MCP_TOKEN", "TUNNEL_TOKEN"} & captured["environment"].keys()


@pytest.mark.parametrize(
    "extra", [["--check"], ["--version", "0.4.0"], ["--name", "kt-mcp"]]
)
def test_recovery_rejects_unrelated_update_options(extra, monkeypatch):
    monkeypatch.setattr(
        module,
        "recoverable_installation",
        lambda: pytest.fail("Incompatible options must fail before inspecting files"),
    )
    with pytest.raises(SystemExit) as parsed:
        module.main(["--recover", *extra])
    assert parsed.value.code == 2


def test_invalid_recovery_reports_failure_without_executing_worker(
    retained, monkeypatch, capsys
):
    (retained.directory / "worker.py").write_text("invalid worker\n")
    monkeypatch.setattr(
        module.os, "execve", lambda *args: pytest.fail("Invalid worker executed")
    )
    assert module.main(["--recover"]) == 1
    assert "saved recovery entry is invalid" in capsys.readouterr().err
    assert retained.directory.exists()


def test_real_retained_worker_recovers_preparing_transaction_without_site_packages(
    retained, tmp_path
):
    # This fixture has no service record, no package install, and no network.
    # Execute the real independent runtime to prove the public command's target
    # works even without importable installed dependencies.
    arguments = module.recovery_argv(module.recoverable_installation())
    arguments.insert(2, "-S")
    result = subprocess.run(
        arguments,
        cwd=tmp_path,
        env=module.releases.clean_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Recovery complete" in result.stdout
    assert retained.installation.venv.exists()
    assert not retained.directory.exists()
