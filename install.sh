#!/usr/bin/env bash
# Download and verify an official Linux release, then run its bundled installer.
kt_mcp_online_install() (
    set -euo pipefail

    kt_die() { printf 'kt-mcp bootstrap: %s\n' "$*" >&2; exit 1; }
    kt_help() {
        cat <<'HELP'
Usage: install.sh [OPTIONS]
       curl -fsSL https://github.com/Stanwade/kohaku-kt-mcp-lite/releases/latest/download/install.sh | bash
       curl -fsSL URL | bash -s -- --version 0.3.5 --prefix "/path/to/installation"

  --version VERSION  Stable release X.Y.Z or vX.Y.Z (default: latest release)
  --prefix DIR       Forward the installation directory to the bundled installer
  --bin-dir DIR      Forward the command-link directory (default: ~/.local/bin)
  --python EXEC      Python 3.10+ executable (default: python3)
  --no-link          Do not create a convenience command link
  --update           Upgrade an existing installation, preserving service state
  --name NAME        User service to resume (with --update; usually detected)
  --help, -h         Show this help

Run as your ordinary Linux user, without sudo. This bootstrap downloads only
official Stanwade/kohaku-kt-mcp-lite release assets and verifies their checksums.
Fresh installs do not change your shell profile, create configuration, or start services.
With --update, a running managed user service pauses during installation and resumes
after verification. Future updates need only: kt-mcp update
HELP
    }

    kt_requested=""
    kt_python="python3"
    kt_prefix=""
    kt_bin_dir=""
    kt_no_link=0
    kt_update=0
    kt_service_name=""
    while (( $# )); do
        case "$1" in
            --version|--prefix|--bin-dir|--python|--name)
                (( $# >= 2 )) || kt_die "$1 requires a value"
                [[ -n "$2" ]] || kt_die "$1 requires a non-empty value"
                if [[ "$1" == "--version" ]]; then
                    kt_requested="${2#v}"
                    [[ "$kt_requested" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] ||
                        kt_die "invalid version; use X.Y.Z or vX.Y.Z"
                else
                    [[ "$1" != "--prefix" ]] || kt_prefix="$2"
                    [[ "$1" != "--python" ]] || kt_python="$2"
                    [[ "$1" != "--bin-dir" ]] || kt_bin_dir="$2"
                    [[ "$1" != "--name" ]] || kt_service_name="$2"
                fi
                shift 2 ;;
            --no-link) kt_no_link=1; shift ;;
            --update) kt_update=1; shift ;;
            --help|-h) kt_help; exit 0 ;;
            *) kt_die "unknown argument: $1 (see --help)" ;;
        esac
    done
    if (( kt_update )); then
        [[ -z "$kt_bin_dir" && "$kt_no_link" == 0 ]] ||
            kt_die "--update preserves existing command links; omit --bin-dir and --no-link"
    else
        [[ -z "$kt_service_name" ]] || kt_die "--name requires --update"
    fi

    printf 'Checking Linux installation prerequisites...\n'
    for kt_tool in uname mktemp rm bash; do
        command -v -- "$kt_tool" >/dev/null ||
            kt_die "missing $kt_tool; on Ubuntu run: sudo apt-get install bash coreutils"
    done
    [[ "$(uname -s </dev/null)" == "Linux" ]] || kt_die "this installer requires Linux"
    (( EUID != 0 )) || kt_die "run as an ordinary user, without sudo"
    command -v curl >/dev/null ||
        kt_die "curl is missing; on Ubuntu run: sudo apt-get install curl ca-certificates"
    kt_python_path="$(command -v -- "$kt_python")" ||
        kt_die "Python is missing; on Ubuntu run: sudo apt-get install python3 python3-venv"
    [[ -x "$kt_python_path" && ! -d "$kt_python_path" ]] ||
        kt_die "the selected Python must be an executable file"
    "$kt_python_path" -I -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' </dev/null ||
        kt_die "Python 3.10 or newer is required"
    if ! "$kt_python_path" -I -c 'import venv, ensurepip' </dev/null >/dev/null 2>&1; then
        printf '%s\n' \
            'kt-mcp bootstrap: venv/ensurepip is missing for the selected Python.' \
            'Install the matching venv package. For Ubuntu default python3, run:' \
            '  sudo apt-get install python3-venv' >&2
        exit 1
    fi

    # Keep relative destinations anchored to the caller before isolating the installer.
    kt_absolute() {
        "$kt_python_path" -I -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1" </dev/null
    }
    kt_prefix="$(kt_absolute "${kt_prefix:-${XDG_DATA_HOME:-$HOME/.local/share}/kt-mcp}")"
    kt_bin_dir="$(kt_absolute "${kt_bin_dir:-$HOME/.local/bin}")"
    kt_python_path="$(kt_absolute "$kt_python_path")"
    kt_install_options=(--prefix "$kt_prefix" --bin-dir "$kt_bin_dir" --python "$kt_python_path")
    if (( kt_no_link )); then
        kt_install_options+=(--no-link)
    fi

    umask 077
    kt_temporary="$(mktemp -d "${TMPDIR:-/tmp}/kt-mcp-online.XXXXXXXX" </dev/null)" ||
        kt_die "could not create a private temporary directory"
    trap 'rm -rf -- "$kt_temporary" </dev/null' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    kt_release_base="https://github.com/Stanwade/kohaku-kt-mcp-lite/releases"
    kt_fetch() {
        if ! curl -q --fail --silent --show-error --location \
            --proto '=https' --proto-redir '=https' \
            --retry 3 --retry-delay 1 --retry-max-time 90 \
            --connect-timeout 10 --max-time 60 --max-filesize "$3" \
            --output "$2" "$1" </dev/null; then
            kt_die "download failed; check your network and curl CA certificates"
        fi
    }
    if [[ -n "$kt_requested" ]]; then
        kt_manifest_url="$kt_release_base/download/v$kt_requested/SHA256SUMS"
    else
        kt_manifest_url="$kt_release_base/latest/download/SHA256SUMS"
    fi
    printf 'Reading the official release checksum manifest...\n'
    kt_fetch "$kt_manifest_url" "$kt_temporary/SHA256SUMS" 65536
    kt_metadata="$("$kt_python_path" -I - "$kt_temporary/SHA256SUMS" "$kt_requested" <<'PY'
import re
import sys
from pathlib import Path

def fail(message):
    raise SystemExit("kt-mcp bootstrap: " + message)

try:
    raw = Path(sys.argv[1]).read_bytes()
    if len(raw) > 65536:
        fail("release checksum manifest is too large")
    lines = raw.decode("ascii").splitlines()
except (OSError, UnicodeError):
    fail("could not read the release checksum manifest")
if not 0 < len(lines) <= 32:
    fail("invalid release checksum manifest")
version_pattern = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
candidates = []
names = set()
for line in lines:
    entry = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)", line)
    if entry is None:
        fail("invalid release checksum manifest line")
    checksum, name = entry.groups()
    if name in names:
        fail("duplicate release checksum entry")
    names.add(name)
    bundle = re.fullmatch(r"kt-mcp-(" + version_pattern + r")-linux\.tar\.gz", name)
    if bundle:
        candidates.append((bundle.group(1), checksum))
if len(candidates) != 1:
    fail("checksum manifest must identify exactly one stable Linux bundle")
version, checksum = candidates[0]
if sys.argv[2] and sys.argv[2] != version:
    fail("checksum manifest version does not match the requested release")
print(version, checksum)
PY
)"
    read -r kt_version kt_checksum <<< "$kt_metadata"
    kt_archive_name="kt-mcp-$kt_version-linux.tar.gz"
    printf 'Downloading Kohaku KT MCP %s...\n' "$kt_version"
    kt_fetch "$kt_release_base/download/v$kt_version/$kt_archive_name" "$kt_temporary/$kt_archive_name" 134217728
    printf 'Verifying the archive and its bundled files...\n'
    kt_bundle="$("$kt_python_path" -I - "$kt_temporary/$kt_archive_name" "$kt_checksum" "$kt_version" "$kt_temporary" <<'PY'
import hashlib
import re
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

class InvalidRelease(Exception):
    pass

def require(condition, message):
    if not condition:
        raise InvalidRelease(message)

def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()

def verify():
    archive = Path(sys.argv[1])
    version = sys.argv[3]
    root_name = f"kt-mcp-{version}-linux"
    root = Path(sys.argv[4]) / root_name
    expected = {
        f"kohaku_kt_mcp-{version}-py3-none-any.whl",
        "install.sh", "README.md", "UBUNTU.md", "LICENSE", "NOTICE",
    }
    allowed = expected | {"SHA256SUMS"}
    require(archive.stat().st_size <= 128 * 1024 * 1024, "release archive is too large")
    require(digest(archive) == sys.argv[2], "release archive checksum mismatch")
    with tarfile.open(archive, "r:gz") as bundle:
        seen = set()
        files = {}
        total = 0
        for member in bundle:
            path = PurePosixPath(member.name)
            require(
                bool(path.parts) and not path.is_absolute() and ".." not in path.parts
                and "\\" not in member.name and path.parts[0] == root_name,
                "unsafe archive path or unexpected root",
            )
            require(path not in seen, "unsafe archive: duplicate member")
            seen.add(path)
            require(len(seen) <= 8, "unsafe archive: unexpected members")
            if member.isdir():
                require(
                    len(path.parts) == 1 and member.name in {root_name, root_name + "/"}
                    and member.size == 0,
                    "unsafe archive: unexpected directory",
                )
                continue
            require(
                member.type in {tarfile.REGTYPE, tarfile.AREGTYPE}
                and member.sparse is None and len(path.parts) == 2
                and path.name in allowed and member.name == f"{root_name}/{path.name}",
                "unsafe archive: only the expected regular bundle files are allowed",
            )
            require(member.size >= 0, "unsafe archive: invalid file size")
            total += member.size
            require(total <= 128 * 1024 * 1024, "release archive expands beyond the size limit")
            if path.name == "SHA256SUMS":
                require(member.size <= 65536, "bundle checksum manifest is too large")
            files[path.name] = member
        require(set(files) == allowed, "unsafe archive: missing or unexpected bundle files")
        root.mkdir(mode=0o700)
        for name, member in files.items():
            source = bundle.extractfile(member)
            require(source is not None, "unsafe archive: unreadable regular file")
            with source, (root / name).open("xb") as target:
                shutil.copyfileobj(source, target)
    checksums = {}
    for line in (root / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        entry = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)", line)
        require(entry is not None, "invalid bundle checksum manifest line")
        checksum, name = entry.groups()
        require(name in expected and name not in checksums, "unsafe or duplicate bundle checksum entry")
        checksums[name] = checksum
    require(set(checksums) == expected, "bundle checksum manifest must cover every bundled file")
    for name, checksum in checksums.items():
        require(digest(root / name) == checksum, "bundle checksum mismatch: " + name)
    print(root)

try:
    verify()
except InvalidRelease as error:
    raise SystemExit("kt-mcp bootstrap: " + str(error)) from None
except (OSError, ValueError, UnicodeError, tarfile.TarError, EOFError):
    raise SystemExit("kt-mcp bootstrap: invalid or unreadable release archive") from None
PY
)"
    if (( kt_update )); then
        printf 'Preparing the verified updater...\n'
        "$kt_python_path" -I - "$kt_bundle" "$kt_temporary" "$kt_prefix" "$kt_version" "$kt_service_name" <<'PY'
import sys
import zipfile
from pathlib import Path

bundle, temporary, prefix = map(Path, sys.argv[1:4])
version, name = sys.argv[4:6]
wheel = bundle / f"kohaku_kt_mcp-{version}-py3-none-any.whl"
runtime = temporary / "runtime"
runtime.mkdir(mode=0o700)
(runtime / "__init__.py").write_text("", encoding="utf-8")
try:
    with zipfile.ZipFile(wheel) as archive:
        for module in ("bootstrap_update", "update", "releases", "managed_service", "terminal_style"):
            entry = f"kt_mcp/{module}.py"
            if archive.namelist().count(entry) != 1 or archive.getinfo(entry).file_size > 131072:
                raise ValueError("updater module missing or invalid")
            (runtime / f"{module}.py").write_bytes(archive.read(entry))
except (OSError, ValueError, KeyError, zipfile.BadZipFile):
    raise SystemExit("kt-mcp bootstrap: this release does not contain a usable automatic updater") from None
sys.path.insert(0, str(temporary))
from runtime.releases import ReleaseError, check_wheel
from runtime.bootstrap_update import main

try:
    check_wheel(wheel, version)
except ReleaseError as error:
    raise SystemExit("kt-mcp bootstrap: " + str(error)) from None
raise SystemExit(main(prefix, temporary / "SHA256SUMS", version, name or None))
PY
        exit 0
    fi
    printf 'Installing the verified release...\n'
    (
        # Older bundled installers use Python without -I. Keep project modules
        # and Python startup overrides out of their import search path.
        unset PYTHONPATH PYTHONHOME PYTHONSTARTUP
        cd -- "$kt_bundle"
        bash ./install.sh "${kt_install_options[@]}" </dev/null
    )
    if (( ! kt_no_link )); then
        "$kt_python_path" -I - "$kt_bin_dir" <<'PY'
import os
import shlex
import sys

directory = os.path.abspath(os.path.expanduser(sys.argv[1] or "~/.local/bin"))
entries = {
    os.path.abspath(entry or os.curdir)
    for entry in os.environ.get("PATH", "").split(os.pathsep)
}
if directory not in entries:
    print("To use the kt-mcp command in this shell, run:")
    print("  export PATH=" + shlex.quote(directory) + ":$PATH")
PY
    fi
)

# Parse the whole function before running any command when piped into bash.
kt_mcp_online_install "$@"
