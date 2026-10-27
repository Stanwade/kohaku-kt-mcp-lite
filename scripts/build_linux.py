"""Build Python packages, an Ubuntu bundle, and the online installer asset."""

import argparse
import ast
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_version(source: str) -> str:
    """Read a literal version without importing or executing package code."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("package __version__ must be a literal string")


def project_version() -> str:
    # The project intentionally uses a static, single-line PEP 621 version.
    # This narrow parser also works with Python 3.10, without a TOML dependency.
    in_project = False
    versions = []
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped.split("#", 1)[0].strip() == "[project]"
        elif in_project and re.match(r"version\s*=", stripped):
            versions.append(ast.literal_eval(stripped.partition("=")[2].strip()))
    if len(versions) != 1 or not isinstance(versions[0], str):
        raise ValueError("pyproject [project].version must be one literal string")
    version = versions[0]
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,63}", version):
        raise ValueError("project version is not suitable for release filenames")
    package = source_version(
        (ROOT / "src/kt_mcp/__init__.py").read_text(encoding="utf-8")
    )
    if version != package:
        raise ValueError("pyproject version and package __version__ differ")
    return version


def check_wheel(path: Path, version: str):
    with zipfile.ZipFile(path) as wheel:
        metadata_paths = [
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise ValueError("wheel must contain exactly one METADATA file")
        metadata = BytesParser().parsebytes(wheel.read(metadata_paths[0]))
        name = re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower()
        if name != "kohaku-kt-mcp" or metadata.get("Version") != version:
            raise ValueError("wheel project name or METADATA version differs")
        packaged_version = source_version(
            wheel.read("kt_mcp/__init__.py").decode("utf-8")
        )
        if packaged_version != version:
            raise ValueError("wheel package __version__ differs from METADATA")


def checksums(files: dict[str, Path]) -> str:
    lines = []
    for name, path in sorted(files.items()):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest()}  {name}\n")
    return "".join(lines)


def build_bundle(path: Path, directory: str, files: dict[str, Path]):
    # Normalize tar metadata so published files do not include local user names.
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as bundle,
    ):
        root = tarfile.TarInfo(directory)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        bundle.addfile(root)
        for name, source in sorted(files.items()):
            info = tarfile.TarInfo(f"{directory}/{name}")
            info.size = source.stat().st_size
            info.mode = 0o755 if name == "install.sh" else 0o644
            with source.open("rb") as data:
                bundle.addfile(info, data)


def publish_file(source: Path, target: Path):
    """Replace one release file atomically, including across filesystems."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + target.name + ".", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as data:
            shutil.copyfileobj(data, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def build_release(output: Path, *, no_isolation: bool = False) -> list[Path]:
    version = project_version()
    output = output.expanduser().absolute()
    with tempfile.TemporaryDirectory(prefix="kt-mcp-linux-build-") as temporary:
        staging = Path(temporary)
        distributions = staging / "distributions"
        command = [
            sys.executable,
            "-m",
            "build",
            "--outdir",
            str(distributions),
            str(ROOT),
        ]
        if no_isolation:
            command.append("--no-isolation")
        subprocess.run(command, cwd=ROOT, check=True, timeout=900)
        wheel = distributions / f"kohaku_kt_mcp-{version}-py3-none-any.whl"
        sdist = distributions / f"kohaku_kt_mcp-{version}.tar.gz"
        if not wheel.is_file() or not sdist.is_file():
            raise ValueError("build did not produce the expected wheel and sdist")
        if project_version() != version:
            raise ValueError("source version changed while the release was building")
        check_wheel(wheel, version)
        bundle_files = {
            wheel.name: wheel,
            "install.sh": ROOT / "scripts/install_linux.sh",
            "UBUNTU.md": ROOT / "UBUNTU.md",
            "README.md": ROOT / "README.md",
            "LICENSE": ROOT / "LICENSE",
            "NOTICE": ROOT / "NOTICE",
        }
        internal_sums = staging / "bundle-SHA256SUMS"
        internal_sums.write_text(
            checksums(bundle_files), encoding="ascii", newline="\n"
        )
        bundle_files["SHA256SUMS"] = internal_sums
        bundle_name = f"kt-mcp-{version}-linux"
        archive = staging / f"{bundle_name}.tar.gz"
        build_bundle(archive, bundle_name, bundle_files)
        artifacts = {path.name: path for path in (wheel, sdist, archive)}
        # The public bootstrap is distinct from the local installer in the bundle.
        artifacts["install.sh"] = ROOT / "install.sh"
        release_sums = staging / "SHA256SUMS"
        release_sums.write_text(checksums(artifacts), encoding="ascii", newline="\n")
        output.mkdir(parents=True, exist_ok=True)
        for name, path in artifacts.items():
            publish_file(path, output / name)
        # Publish the manifest last, after every referenced artifact is complete.
        publish_file(release_sums, output / "SHA256SUMS")
    return [output / name for name in (*artifacts, "SHA256SUMS")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/linux")
    parser.add_argument("--no-isolation", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        paths = build_release(arguments.output, no_isolation=arguments.no_isolation)
    except (
        OSError,
        ValueError,
        KeyError,
        SyntaxError,
        zipfile.BadZipFile,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        parser.exit(1, f"Linux release build failed: {exc}\n")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
