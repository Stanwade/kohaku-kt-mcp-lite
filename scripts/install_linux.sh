#!/usr/bin/env bash
# Install Kohaku KT MCP into an ordinary user's private virtual environment.
set -euo pipefail

kt_die() { printf 'kt-mcp installer: %s\n' "$*" >&2; exit 1; }
kt_help() {
    cat <<'HELP'
Usage: install_linux.sh [OPTIONS]
       install.sh [OPTIONS]                 # from a Linux release bundle

  --prefix DIR    Data/install directory (default: $XDG_DATA_HOME/kt-mcp,
                  or $HOME/.local/share/kt-mcp)
  --bin-dir DIR   Directory for the kt-mcp link (default: $HOME/.local/bin)
  --python EXEC   Python 3.10+ executable (default: python3)
  --source PATH   Local wheel, source archive, or project directory to install
  --no-link       Do not create a convenience command link
  --help          Show this help

Run as your ordinary user, without sudo. The installer never installs packages
into the system Python, overwrites another application's venv/command, or changes
MCP configuration or systemd service state. Existing services need a restart
after an upgrade.
HELP
}

kt_prefix="${XDG_DATA_HOME:-$HOME/.local/share}/kt-mcp"
kt_bin_dir="$HOME/.local/bin"
kt_python="python3"
kt_source=""
kt_no_link=0
while (( $# )); do
    case "$1" in
        --prefix|--bin-dir|--python|--source)
            (( $# >= 2 )) || kt_die "$1 requires a value"
            [[ -n "$2" ]] || kt_die "$1 requires a non-empty value"
            case "$1" in
                --prefix) kt_prefix="$2" ;;
                --bin-dir) kt_bin_dir="$2" ;;
                --python) kt_python="$2" ;;
                --source) kt_source="$2" ;;
            esac
            shift 2 ;;
        --no-link) kt_no_link=1; shift ;;
        --help|-h) kt_help; exit 0 ;;
        *) kt_die "unknown argument: $1 (see --help)" ;;
    esac
done

[[ "$(uname -s)" == "Linux" ]] || kt_die "this installer requires Linux"
(( EUID != 0 )) || kt_die "run this installer as an ordinary user, without sudo"
kt_python="$(command -v -- "$kt_python")" || kt_die "Python executable was not found"
[[ -x "$kt_python" && ! -d "$kt_python" ]] || kt_die "Python must be an executable file"
"$kt_python" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' ||
    kt_die "Python 3.10 or newer is required"
if ! "$kt_python" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    kt_die "venv/ensurepip is missing. On Ubuntu install the matching package with: sudo apt-get install python3-venv"
fi

kt_absolute() {
    "$kt_python" -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1"
}
kt_prefix="$(kt_absolute "$kt_prefix")"
kt_bin_dir="$(kt_absolute "$kt_bin_dir")"
kt_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ -z "$kt_source" ]]; then
    shopt -s nullglob
    kt_wheels=("$kt_script_dir"/kohaku_kt_mcp-*.whl)
    shopt -u nullglob
    if (( ${#kt_wheels[@]} == 1 )); then
        kt_source="${kt_wheels[0]}"
    elif (( ${#kt_wheels[@]} > 1 )); then
        kt_die "multiple release wheels found; select one with --source PATH"
    elif [[ "${kt_script_dir##*/}" == "scripts" && -f "$kt_script_dir/../pyproject.toml" ]]; then
        kt_source="$kt_script_dir/.."
    else
        kt_die "no release wheel or source checkout found; use --source PATH"
    fi
fi
kt_source="$(kt_absolute "$kt_source")"
[[ -f "$kt_source" || -d "$kt_source" ]] || kt_die "--source must be an existing local path"

kt_venv="$kt_prefix/venv"
kt_marker="$kt_venv/.kt-mcp-managed"
kt_marker_text="kohaku-kt-mcp installer v1"
kt_target="$kt_venv/bin/kt-mcp"
kt_link="$kt_bin_dir/kt-mcp"
if [[ -e "$kt_venv" || -L "$kt_venv" ]]; then
    [[ -d "$kt_venv" && ! -L "$kt_venv" && -f "$kt_marker" && -f "$kt_venv/pyvenv.cfg" ]] ||
        kt_die "refusing to update an unmarked or symlinked venv: $kt_venv"
    [[ "$(<"$kt_marker")" == "$kt_marker_text" ]] ||
        kt_die "the existing venv is not managed by this installer"
    [[ -x "$kt_venv/bin/python" && ! -d "$kt_venv/bin/python" ]] ||
        kt_die "the managed venv interpreter is unavailable; inspect the environment or choose a new --prefix"
    kt_selected_version="$("$kt_python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" ||
        kt_die "the selected Python interpreter failed; inspect its installation"
    if ! kt_existing_version="$("$kt_venv/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)"; then
        kt_die "the managed venv interpreter is unusable; inspect the environment or choose a new --prefix"
    fi
    [[ "$kt_existing_version" == "$kt_selected_version" ]] ||
        kt_die "Python version mismatch ($kt_existing_version versus $kt_selected_version); choose a new --prefix instead of rebuilding this venv"
fi

kt_check_link() {
    if [[ -e "$kt_link" || -L "$kt_link" ]]; then
        [[ -L "$kt_link" ]] || kt_die "refusing to overwrite existing command: $kt_link"
        "$kt_python" -c 'import os,sys; raise SystemExit(os.path.realpath(sys.argv[1]) != os.path.realpath(sys.argv[2]))' "$kt_link" "$kt_target" ||
            kt_die "the existing command link points elsewhere: $kt_link"
    fi
}
if (( ! kt_no_link )); then
    [[ ! -e "$kt_bin_dir" || -d "$kt_bin_dir" ]] || kt_die "--bin-dir is not a directory"
    kt_check_link
fi

# Never clear a directory. A marked venv can be refreshed after an interrupted
# package installation without removing user configuration or cached downloads.
mkdir -p -- "$kt_prefix"
"$kt_python" -m venv "$kt_venv"
printf '%s\n' "$kt_marker_text" > "$kt_marker"
"$kt_venv/bin/python" -m pip install --upgrade -- "$kt_source"
# distlib's shell trampoline does not safely quote every special venv path.
# Replace only our own entry point, preserving the interpreter's venv path.
"$kt_venv/bin/python" - "$kt_target" <<'PY'
import os
import shlex
import sys
import tempfile
from pathlib import Path

target = Path(sys.argv[1])
launcher = "#!/bin/sh\nexec " + shlex.quote(os.path.abspath(sys.executable)) + ' -m kt_mcp "$@"\n'
fd, temporary = tempfile.mkstemp(prefix=".kt-mcp-launcher-", dir=target.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
        output.write(launcher)
    os.chmod(temporary, 0o755)
    os.replace(temporary, target)
finally:
    Path(temporary).unlink(missing_ok=True)
PY
"$kt_target" --version

if (( ! kt_no_link )); then
    mkdir -p -- "$kt_bin_dir"
    kt_check_link
    if [[ ! -L "$kt_link" ]]; then
        ln -s -- "$kt_target" "$kt_link"
    fi
    printf 'Command link: %s\n' "$kt_link"
fi
printf 'Installed CLI: %s\n' "$kt_target"
printf 'Open the terminal manager: %q tui\n' "$kt_target"
printf 'After upgrading, restart existing services with: %q service restart\n' "$kt_target"
printf 'Include --name NAME and/or --system when managing a non-default service.\n'
