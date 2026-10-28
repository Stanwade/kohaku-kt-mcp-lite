"""Bounded, verified official release downloads; usable outside the installed venv."""

import ast
import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

RELEASES = "https://github.com/Stanwade/kohaku-kt-mcp-lite/releases"
MANIFEST_LIMIT = 65536
ARCHIVE_LIMIT = 128 * 1024 * 1024
VERSION_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"


class ReleaseError(ValueError):
    """A release could not be downloaded or verified."""


def version_text(value: str) -> str:
    version = value.removeprefix("v")
    if len(version) > 63 or re.fullmatch(VERSION_PATTERN, version) is None:
        raise ReleaseError("Use a stable release version such as 0.2.4 or v0.2.4.")
    return version


def version_key(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version_text(value).split("."))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Release:
    version: str
    checksums: dict[str, str]

    @property
    def archive_name(self) -> str:
        return f"kt-mcp-{self.version}-linux.tar.gz"

    @property
    def wheel_name(self) -> str:
        return f"kohaku_kt_mcp-{self.version}-py3-none-any.whl"

    def url(self, name: str) -> str:
        if name not in self.checksums:
            raise ReleaseError("The requested asset is absent from the release.")
        return f"{RELEASES}/download/v{self.version}/{name}"


def parse_manifest(raw: bytes, requested: str | None = None) -> Release:
    if len(raw) > MANIFEST_LIMIT:
        raise ReleaseError("The release checksum manifest is too large.")
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError:
        raise ReleaseError(
            "The release checksum manifest is not valid ASCII."
        ) from None
    if not 0 < len(lines) <= 32:
        raise ReleaseError("The release checksum manifest has an invalid length.")
    checksums = {}
    candidates = []
    for line in lines:
        entry = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)", line)
        if entry is None:
            raise ReleaseError(
                "The release checksum manifest contains an invalid line."
            )
        digest, name = entry.groups()
        if name in checksums:
            raise ReleaseError("The release checksum manifest repeats an asset.")
        checksums[name] = digest
        bundle = re.fullmatch(r"kt-mcp-(" + VERSION_PATTERN + r")-linux\.tar\.gz", name)
        if bundle:
            candidates.append(version_text(bundle.group(1)))
    if len(candidates) != 1:
        raise ReleaseError(
            "The manifest must identify exactly one stable Linux release."
        )
    version = candidates[0]
    if requested is not None and version != version_text(requested):
        raise ReleaseError("The manifest does not match the requested release version.")
    return Release(version, checksums)


def clean_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "KT_MCP_TOKEN",
        "TUNNEL_TOKEN",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "BASH_ENV",
        "ENV",
        "PIP_TARGET",
        "PIP_PREFIX",
        "PIP_USER",
    ):
        environment.pop(name, None)
    return environment


def fetch(url: str, destination: Path, limit: int) -> None:
    """Download only from the fixed official release URL, without printing transport logs."""
    if not url.startswith(RELEASES + "/"):
        raise ReleaseError("Updates must come from the official release repository.")
    if destination.exists() or destination.is_symlink():
        raise ReleaseError("The private download destination already exists.")
    command = [
        "curl",
        "-q",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--retry",
        "2",
        "--retry-delay",
        "1",
        "--retry-max-time",
        "70",
        "--connect-timeout",
        "10",
        "--max-time",
        "30",
        "--max-filesize",
        str(limit),
        "--output",
        str(destination),
        url,
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=clean_environment(),
            timeout=90,
            check=False,
        )
        if (
            result.returncode
            or not destination.is_file()
            or destination.stat().st_size > limit
        ):
            raise ReleaseError(
                "Download failed; check your network and curl CA certificates."
            )
    except FileNotFoundError:
        raise ReleaseError(
            "curl is required; on Ubuntu install curl and ca-certificates."
        ) from None
    except subprocess.TimeoutExpired:
        raise ReleaseError(
            "The release download timed out; the installed version is unchanged."
        ) from None


def latest_release(directory: Path, requested: str | None = None) -> Release:
    requested = version_text(requested) if requested is not None else None
    url = (
        f"{RELEASES}/download/v{requested}/SHA256SUMS"
        if requested is not None
        else f"{RELEASES}/latest/download/SHA256SUMS"
    )
    manifest = directory / "release-SHA256SUMS"
    fetch(url, manifest, MANIFEST_LIMIT)
    return parse_manifest(manifest.read_bytes(), requested)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseError(message)


def check_wheel(path: Path, version: str) -> None:
    try:
        with zipfile.ZipFile(path) as wheel:
            metadata = [
                name
                for name in wheel.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            _require(len(metadata) == 1, "The release wheel has ambiguous metadata.")
            _require(
                wheel.getinfo(metadata[0]).file_size <= MANIFEST_LIMIT,
                "Wheel metadata is too large.",
            )
            record = BytesParser().parsebytes(wheel.read(metadata[0]))
            name = re.sub(r"[-_.]+", "-", record.get("Name", "")).lower()
            _require(
                name == "kohaku-kt-mcp" and record.get("Version") == version,
                "The wheel project or version differs from the release.",
            )
            _require(
                wheel.getinfo("kt_mcp/__init__.py").file_size <= MANIFEST_LIMIT,
                "Wheel version source is too large.",
            )
            tree = ast.parse(wheel.read("kt_mcp/__init__.py").decode("utf-8"))
            values = [
                ast.literal_eval(node.value)
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "__version__"
                    for target in node.targets
                )
            ]
            _require(
                values == [version], "The wheel code version differs from its metadata."
            )
    except ReleaseError:
        raise
    except (
        OSError,
        KeyError,
        UnicodeError,
        SyntaxError,
        ValueError,
        TypeError,
        zipfile.BadZipFile,
    ):
        raise ReleaseError("The release wheel is invalid or unreadable.") from None


def unpack_release(archive: Path, directory: Path, release: Release) -> Path:
    """Accept exactly the official flat bundle, checking outer and inner digests."""
    root_name = f"kt-mcp-{release.version}-linux"
    root = directory / root_name
    expected = {
        release.wheel_name,
        "install.sh",
        "README.md",
        "UBUNTU.md",
        "LICENSE",
        "NOTICE",
    }
    allowed = expected | {"SHA256SUMS"}
    _require(
        archive.stat().st_size <= ARCHIVE_LIMIT, "The release archive is too large."
    )
    _require(
        sha256(archive) == release.checksums[release.archive_name],
        "The release archive checksum does not match.",
    )
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            seen = set()
            members = {}
            total = 0
            for member in bundle:
                path = PurePosixPath(member.name)
                _require(
                    bool(path.parts)
                    and not path.is_absolute()
                    and ".." not in path.parts
                    and "\\" not in member.name
                    and path.parts[0] == root_name,
                    "The release archive contains an unsafe path.",
                )
                _require(
                    path not in seen, "The release archive contains a duplicate member."
                )
                seen.add(path)
                _require(
                    len(seen) <= 8, "The release archive contains unexpected members."
                )
                if member.isdir():
                    _require(
                        len(path.parts) == 1
                        and member.name in {root_name, root_name + "/"}
                        and member.size == 0,
                        "The release archive contains an unexpected directory.",
                    )
                    continue
                _require(
                    member.type in {tarfile.REGTYPE, tarfile.AREGTYPE}
                    and member.sparse is None
                    and len(path.parts) == 2
                    and path.name in allowed
                    and member.name == f"{root_name}/{path.name}",
                    "The release archive must contain only its expected regular files.",
                )
                _require(
                    member.size >= 0,
                    "The release archive contains an invalid file size.",
                )
                total += member.size
                _require(
                    total <= ARCHIVE_LIMIT, "The expanded release archive is too large."
                )
                if path.name == "SHA256SUMS":
                    _require(
                        member.size <= MANIFEST_LIMIT,
                        "The inner manifest is too large.",
                    )
                members[path.name] = member
            _require(
                set(members) == allowed,
                "The release archive has missing or unexpected files.",
            )
            root.mkdir(mode=0o700)
            for name, member in members.items():
                source = bundle.extractfile(member)
                _require(
                    source is not None, "The release archive has an unreadable member."
                )
                with source, (root / name).open("xb") as target:
                    shutil.copyfileobj(source, target)
        checksums = {}
        for line in (root / "SHA256SUMS").read_text(encoding="ascii").splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)", line)
            _require(
                match is not None, "The inner checksum manifest has an invalid line."
            )
            digest, name = match.groups()
            _require(
                name in expected and name not in checksums,
                "The inner manifest contains an unsafe or duplicate entry.",
            )
            checksums[name] = digest
        _require(
            set(checksums) == expected,
            "The inner manifest must cover every bundled file.",
        )
        for name, digest in checksums.items():
            _require(
                sha256(root / name) == digest,
                "A bundled file failed checksum verification.",
            )
        check_wheel(root / release.wheel_name, release.version)
    except (OSError, UnicodeError, tarfile.TarError, EOFError):
        raise ReleaseError("The release archive is invalid or unreadable.") from None
    return root


def download_release(release: Release, directory: Path) -> Path:
    archive = directory / release.archive_name
    fetch(release.url(release.archive_name), archive, ARCHIVE_LIMIT)
    return unpack_release(archive, directory, release)
