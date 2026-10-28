"""One-time updates inspect old installs without needing their missing update CLI."""

import json
import os
import sys
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest

import kt_mcp.bootstrap_update as module

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux update migration"
)


@pytest.fixture
def legacy_installation(tmp_path, monkeypatch):
    prefix = tmp_path / 'legacy install 中文 % $ "quotes"'
    prefix.mkdir(mode=0o700)
    environment = prefix / "venv"
    # A real interpreter and real metadata, with no application dependencies.
    # The legacy package has no usable CLI at all; only its version is readable.
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    packages = next((environment / "lib").glob("python*/site-packages"))
    package = packages / "kt_mcp"
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "0.2.3"\n')
    cli_called = prefix / "legacy-cli-was-called"
    (package / "__main__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(cli_called)!r}).touch()\n"
        "raise SystemExit('legacy CLI has no update command')\n"
    )
    metadata = packages / "kohaku_kt_mcp-0.2.3.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: kohaku-kt-mcp\nVersion: 0.2.3\n"
    )
    (metadata / "direct_url.json").write_text('{"url":"file:///legacy.whl"}')
    marker = environment / ".kt-mcp-managed"
    marker.write_text(module.update.MARKER + "\n")
    if os.geteuid() == 0:
        for path in (prefix, environment, marker, environment / "pyvenv.cfg"):
            os.chown(path, 1000, 1000)
        monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    record = {
        "prefix": str(environment),
        "base_python": str(Path(sys._base_executable).resolve()),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "version": "0.2.3",
        "metadata_version": "0.2.3",
        "module": str(package / "__init__.py"),
        "package": str(package),
        "editable": False,
    }
    return SimpleNamespace(
        prefix=prefix,
        venv=environment,
        marker=marker,
        package=package,
        metadata=metadata,
        cli_called=cli_called,
        record=record,
    )


def test_real_legacy_probe_does_not_import_cli_or_need_old_update_command(
    legacy_installation, monkeypatch
):
    item = legacy_installation
    calls = []
    original = module.update._command

    def command(arguments, **kwargs):
        calls.append(arguments)
        return original(arguments, **kwargs)

    monkeypatch.setattr(module.update, "_command", command)
    monkeypatch.setenv("XDG_DATA_HOME", "/unrelated/new/default")
    result = module.inspect_installation(item.prefix)
    assert result.prefix == item.prefix
    assert result.python == item.venv / "bin/python"
    assert result.base_python == Path(item.record["base_python"])
    assert result.version == "0.2.3"
    assert calls[0][:3] == [item.venv / "bin/python", "-I", "-c"]
    assert calls[1][0] == result.base_python
    assert not item.cli_called.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("editable", True),
        ("prefix", "/another/installation/venv"),
        ("module", "/another/checkout/kt_mcp/__init__.py"),
        ("package", "/another/checkout/kt_mcp"),
        ("metadata_version", "0.2.2"),
    ],
)
def test_probe_rejects_editable_or_mismatched_paths_and_metadata(
    legacy_installation, monkeypatch, field, value
):
    record = {**legacy_installation.record, field: value}
    monkeypatch.setattr(
        module.update, "_command", lambda *args, **kwargs: json.dumps(record)
    )
    with pytest.raises(module.update.UpdateError, match="editable|match this prefix"):
        module.inspect_installation(legacy_installation.prefix)
    assert not legacy_installation.cli_called.exists()


def test_original_base_python_version_must_match_the_existing_venv(
    legacy_installation, monkeypatch
):
    record = legacy_installation.record

    def command(arguments, **kwargs):
        if arguments[-1] == module.PROBE:
            return json.dumps(record)
        assert arguments[0] == Path(record["base_python"])
        return "99.1"

    monkeypatch.setattr(module.update, "_command", command)
    with pytest.raises(module.update.UpdateError, match="new install prefix"):
        module.inspect_installation(legacy_installation.prefix)


def test_unmanaged_prefix_is_rejected_before_running_an_interpreter(
    legacy_installation, monkeypatch
):
    legacy_installation.marker.write_text("not this installer\n")

    def unexpected(*args, **kwargs):
        raise AssertionError("unmanaged environment was executed")

    monkeypatch.setattr(module.update, "_command", unexpected)
    with pytest.raises(module.update.UpdateError, match="existing install.sh"):
        module.inspect_installation(legacy_installation.prefix)


def test_root_is_rejected_before_inspection(tmp_path, monkeypatch):
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    with pytest.raises(module.update.UpdateError, match="ordinary"):
        module.inspect_installation(tmp_path / "does-not-exist")


def manifest(tmp_path, version):
    path = tmp_path / "SHA256SUMS"
    path.write_text("a" * 64 + f"  kt-mcp-{version}-linux.tar.gz\n")
    return path


def selected_installation(tmp_path, version="0.2.3"):
    prefix = tmp_path / "existing selected prefix"
    return module.update.Installation(
        prefix,
        prefix / "venv/bin/python",
        Path(sys._base_executable).resolve(),
        version,
    )


def test_migration_hands_current_installation_and_selected_service_to_shared_worker(
    tmp_path, monkeypatch
):
    installation = selected_installation(tmp_path)
    calls = []
    monkeypatch.setattr(module, "inspect_installation", lambda path: installation)
    monkeypatch.setattr(module.update, "_handoff", lambda *args: calls.append(args))
    assert (
        module.main(
            installation.prefix,
            manifest(tmp_path, "0.2.4"),
            "0.2.4",
            "kt-mcp-custom",
        )
        == 0
    )
    assert len(calls) == 1
    current, release, name = calls[0]
    assert current == installation
    assert release.version == "0.2.4"
    assert name == "kt-mcp-custom"


@pytest.mark.parametrize("target,code", [("0.2.3", 0), ("0.2.2", 1)])
def test_current_version_or_downgrade_never_starts_the_worker(
    tmp_path, monkeypatch, capsys, target, code
):
    installation = selected_installation(tmp_path)
    monkeypatch.setattr(module, "inspect_installation", lambda path: installation)

    def unexpected(*args, **kwargs):
        raise AssertionError("an unnecessary update was started")

    monkeypatch.setattr(module.update, "_handoff", unexpected)
    assert module.main(installation.prefix, manifest(tmp_path, target), target) == code
    output = capsys.readouterr()
    assert ("Downgrades" in output.err) if code else ("Up to date" in output.out)


@pytest.mark.parametrize("error_type", [OSError, ValueError, KeyError, TypeError])
def test_untrusted_inspection_errors_do_not_print_credentials(
    tmp_path, monkeypatch, capsys, error_type
):
    secret = "https://user:fixture-private-password@index.example.invalid"

    def fail(path):
        raise error_type(secret)

    monkeypatch.setattr(module, "inspect_installation", fail)
    assert module.main(tmp_path, manifest(tmp_path, "0.2.4"), "0.2.4") == 1
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert "installed files are unchanged" in output.err


@pytest.mark.parametrize("bad_input", ["manifest", "name"])
def test_bad_bootstrap_input_fails_before_inspecting_or_changing_installation(
    tmp_path, monkeypatch, bad_input
):
    def unexpected(*args, **kwargs):
        raise AssertionError("invalid bootstrap input reached the old environment")

    monkeypatch.setattr(module, "inspect_installation", unexpected)
    path = manifest(tmp_path, "0.2.4")
    name = "kt-mcp"
    if bad_input == "manifest":
        path.write_text("invalid release checksums\n")
    else:
        name = "unrelated-service"
    assert module.main(tmp_path, path, "0.2.4", name) == 1
