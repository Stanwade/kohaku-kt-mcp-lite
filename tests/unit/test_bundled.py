import hashlib
import sys
import zipfile

import pytest

from kt_mcp import bundled


def test_source_uses_current_python():
    assert bundled.python_command("print('ok')") == [
        sys.executable,
        "-X",
        "utf8",
        "-u",
        "-c",
        "print('ok')",
    ]


def archive_fixture(tmp_path, monkeypatch, names):
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name, data in names.items():
            output.writestr(name, data)
    monkeypatch.setattr(
        bundled, "PYTHON_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    return archive


def test_runtime_install_cache_and_no_host_path(tmp_path, monkeypatch):
    archive = archive_fixture(
        tmp_path,
        monkeypatch,
        {
            "python.exe": b"fixture",
            "python313._pth": b"python313.zip\n.\n",
            "python313.zip": b"stdlib",
        },
    )
    executable = bundled.install_python(archive, tmp_path / "cache")
    assert executable.read_bytes() == b"fixture"
    assert not list(executable.parent.glob("*._pth"))
    assert (executable.parent / ".ready").read_text() == bundled.PYTHON_SHA256
    archive.unlink()
    assert bundled.install_python(archive, tmp_path / "cache") == executable


def test_runtime_rejects_bad_hash_and_traversal(tmp_path, monkeypatch):
    archive = archive_fixture(tmp_path, monkeypatch, {"python.exe": b"fixture"})
    monkeypatch.setattr(bundled, "PYTHON_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="校验"):
        bundled.install_python(archive, tmp_path / "cache")
    archive = archive_fixture(
        tmp_path, monkeypatch, {"python.exe": b"fixture", "../escape": b"no"}
    )
    with pytest.raises(ValueError, match="路径"):
        bundled.install_python(archive, tmp_path / "cache")
    assert not (tmp_path / "escape").exists()


def test_compiled_command_uses_shipped_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(bundled, "is_compiled", lambda: True)
    if bundled.os.name != "nt":
        pytest.skip("Windows bundle")
    fake_module = tmp_path / "kt_mcp" / "bundled.py"
    vendor = fake_module.parent / "_vendor"
    vendor.mkdir(parents=True)
    (vendor / bundled.PYTHON_ARCHIVE).touch()
    monkeypatch.setattr(bundled, "__file__", str(fake_module))
    monkeypatch.setattr(bundled, "data_directory", lambda: tmp_path)
    monkeypatch.setattr(
        bundled, "install_python", lambda *args: tmp_path / "python.exe"
    )
    assert bundled.python_command("print(1)") == [
        str(tmp_path / "python.exe"),
        "-E",
        "-s",
        "-X",
        "utf8",
        "-u",
        "-c",
        "print(1)",
    ]
