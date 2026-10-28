import hashlib
import io
import tarfile
import zipfile

import pytest

from kt_mcp import releases


def bundle_fixture(tmp_path, *, unsafe=None, code_version="9.8.7"):
    version = "9.8.7"
    wheel_bytes = io.BytesIO()
    with zipfile.ZipFile(wheel_bytes, "w") as wheel:
        wheel.writestr(
            f"kohaku_kt_mcp-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: kohaku-kt-mcp\nVersion: {version}\n",
        )
        wheel.writestr("kt_mcp/__init__.py", f'__version__ = "{code_version}"\n')
    files = {
        f"kohaku_kt_mcp-{version}-py3-none-any.whl": wheel_bytes.getvalue(),
        "install.sh": b"#!/bin/sh\nexit 0\n",
        "README.md": b"readme\n",
        "UBUNTU.md": b"ubuntu\n",
        "LICENSE": b"license\n",
        "NOTICE": b"notice\n",
    }
    files["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in sorted(files.items())
    ).encode("ascii")
    if unsafe == "inner-digest":
        files["README.md"] = b"altered after the inner manifest was made\n"
    archive = tmp_path / f"kt-mcp-{version}-linux.tar.gz"
    root = f"kt-mcp-{version}-linux"
    with tarfile.open(archive, "w:gz") as tar:
        directory = tarfile.TarInfo(root)
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
        for name, data in files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            if name == "README.md" and unsafe == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "../../escaped"
                info.size = 0
                tar.addfile(info)
            else:
                tar.addfile(info, io.BytesIO(data))
        if unsafe in {"traversal", "duplicate"}:
            info = tarfile.TarInfo(
                f"{root}/../escaped" if unsafe == "traversal" else f"{root}/NOTICE"
            )
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    release = releases.Release(version, {archive.name: releases.sha256(archive)})
    return archive, release, files


@pytest.mark.parametrize("version", ["0.2.4", "v0.2.4", "10.20.300"])
def test_stable_versions_are_normalized_and_compared_numerically(version):
    assert releases.version_text(version) == version.removeprefix("v")
    assert releases.version_key("0.2.10") > releases.version_key("0.2.9")


@pytest.mark.parametrize(
    "version", ["0.2", "0.2.04", "0.2.4rc1", "../0.2.4", "1.0.0\n"]
)
def test_invalid_or_prerelease_versions_are_rejected(version):
    with pytest.raises(releases.ReleaseError, match="stable"):
        releases.version_text(version)


def test_latest_manifest_is_read_once_then_download_is_pinned(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    archive, release, files = bundle_fixture(fixture)
    manifest = f"{release.checksums[archive.name]}  {archive.name}\n".encode()
    calls = []

    def fetch(url, destination, limit):
        calls.append(url)
        data = manifest if url.endswith("/SHA256SUMS") else archive.read_bytes()
        assert len(data) < limit
        destination.write_bytes(data)

    monkeypatch.setattr(releases, "fetch", fetch)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    selected = releases.latest_release(downloads)
    extracted = releases.download_release(selected, downloads)

    assert calls == [
        releases.RELEASES + "/latest/download/SHA256SUMS",
        releases.RELEASES + "/download/v9.8.7/kt-mcp-9.8.7-linux.tar.gz",
    ]
    assert selected.version == "9.8.7"
    assert {path.name for path in extracted.iterdir()} == set(files)
    for name, data in files.items():
        assert (extracted / name).read_bytes() == data


@pytest.mark.parametrize(
    "case", ["duplicate", "ambiguous", "version", "non-ascii", "unsafe-name"]
)
def test_manifest_rejects_ambiguous_or_mismatched_targets(case):
    line = "a" * 64 + "  kt-mcp-9.8.7-linux.tar.gz\n"
    requested = "9.8.7"
    if case == "duplicate":
        line += line
    elif case == "ambiguous":
        line += "b" * 64 + "  kt-mcp-9.8.8-linux.tar.gz\n"
    elif case == "version":
        requested = "9.8.8"
    elif case == "non-ascii":
        line += "中文"
    else:
        line += "b" * 64 + "  ../install.sh\n"
    with pytest.raises(releases.ReleaseError):
        releases.parse_manifest(line.encode("utf-8"), requested)


@pytest.mark.parametrize(
    "unsafe", ["symlink", "traversal", "duplicate", "inner-digest"]
)
def test_bundle_integrity_checks_run_before_any_installer(tmp_path, unsafe):
    source = tmp_path / "source"
    source.mkdir()
    archive, release, _ = bundle_fixture(source, unsafe=unsafe)
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(releases.ReleaseError):
        releases.unpack_release(archive, target, release)
    assert not (target / "escaped").exists()


def test_outer_checksum_failure_does_not_extract_files(tmp_path):
    archive, release, _ = bundle_fixture(tmp_path)
    wrong = releases.Release(release.version, {archive.name: "0" * 64})
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(releases.ReleaseError, match="archive checksum"):
        releases.unpack_release(archive, target, wrong)
    assert list(target.iterdir()) == []


def test_valid_digests_do_not_hide_a_different_code_version(tmp_path):
    archive, release, _ = bundle_fixture(tmp_path, code_version="0.0.1")
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(releases.ReleaseError, match="code version"):
        releases.unpack_release(archive, target, release)
