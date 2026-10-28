"""Configuration snapshots compare stable metadata without weakening race checks."""

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from kt_mcp import onboarding as module
from kt_mcp.daemon import DaemonConfig, save_config
from kt_mcp.desktop import DesktopSettings


@pytest.fixture
def configuration(tmp_path):
    path = tmp_path / "config.json"
    config = DaemonConfig(
        DesktopSettings(str(tmp_path), mode="local"), "snapshot-token-" + "a" * 40, ""
    )
    save_config(config, path)
    return path, config


def shifted_ctime(info, shift):
    fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_mode",
        "st_uid",
        "st_gid",
    )
    values = {name: getattr(info, name) for name in fields}
    values["st_ctime_ns"] += shift
    return SimpleNamespace(**values)


def test_stable_handle_and_path_can_report_different_ctime(configuration, monkeypatch):
    path, _ = configuration
    actual_fstat = os.fstat
    monkeypatch.setattr(
        module.os, "fstat", lambda fd: shifted_ctime(actual_fstat(fd), 1_000_000)
    )
    snapshot = module._snapshot(path)
    assert snapshot.data == path.read_bytes()
    assert snapshot.identity == module._file_identity(path.lstat())


def test_descriptor_ctime_changes_are_still_rejected(configuration, monkeypatch):
    path, _ = configuration
    actual_fstat = os.fstat
    calls = 0

    def changing_ctime(fd):
        nonlocal calls
        calls += 1
        return shifted_ctime(actual_fstat(fd), calls * 1_000_000)

    monkeypatch.setattr(module.os, "fstat", changing_ctime)
    with pytest.raises(ValueError, match="changed while"):
        module._snapshot(path)


@pytest.mark.parametrize("change", ["same-inode-content", "equal-content-replacement"])
def test_editor_refuses_external_changes_on_every_platform(configuration, change):
    path, config = configuration
    editor = module.ConfigEditor(path)
    before = path.read_bytes()
    original_identity = path.stat().st_dev, path.stat().st_ino
    if change == "same-inode-content":
        path.write_bytes(before + b"\n")
        assert (path.stat().st_dev, path.stat().st_ino) == original_identity
    else:
        replacement = path.with_name("replacement.json")
        replacement.write_bytes(before)
        replacement.chmod(0o600)
        os.replace(replacement, path)
        assert (path.stat().st_dev, path.stat().st_ino) != original_identity
    external = path.read_bytes()
    with pytest.raises(ValueError, match="changed outside"):
        editor.save(replace(config, settings=replace(config.settings, port=9123)))
    assert path.read_bytes() == external
    assert not editor.saved


def test_snapshot_rejects_content_changed_during_read(configuration, monkeypatch):
    path, _ = configuration
    before = path.read_bytes()
    actual_fstat = os.fstat
    calls = 0

    def change_after_read(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            with path.open("ab") as writer:
                writer.write(b"\n")
        return actual_fstat(fd)

    monkeypatch.setattr(module.os, "fstat", change_after_read)
    with pytest.raises(ValueError, match="changed while"):
        module._snapshot(path)
    assert path.read_bytes() == before + b"\n"


def test_snapshot_rejects_equal_content_replacement_before_open(
    configuration, monkeypatch
):
    path, _ = configuration
    before = path.read_bytes()
    replacement = path.with_name("replacement.json")
    replacement.write_bytes(before)
    replacement.chmod(0o600)
    original_identity = path.stat().st_dev, path.stat().st_ino
    actual_open = os.open

    def replace_before_open(target, flags, *args, **kwargs):
        assert target == path
        os.replace(replacement, path)
        return actual_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", replace_before_open)
    with pytest.raises(ValueError, match="changed while"):
        module._snapshot(path)
    assert (path.stat().st_dev, path.stat().st_ino) != original_identity
    assert path.read_bytes() == before
