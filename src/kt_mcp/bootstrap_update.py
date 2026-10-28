"""One-time migration for releases that predate the update CLI.

The online bootstrap extracts this and the stdlib updater from a verified wheel.
It inspects the old environment without importing this module into that environment.
"""

import json
import os
import re
import sys
from pathlib import Path

from . import releases, update

PROBE = """
import importlib.metadata, json, sys, kt_mcp
distribution = importlib.metadata.distribution('kohaku-kt-mcp')
direct = json.loads(distribution.read_text('direct_url.json') or '{}')
print(json.dumps({
    'prefix': sys.prefix,
    'base_python': sys._base_executable,
    'python_version': f'{sys.version_info.major}.{sys.version_info.minor}',
    'version': kt_mcp.__version__,
    'metadata_version': distribution.version,
    'module': kt_mcp.__file__,
    'package': str(distribution.locate_file('kt_mcp')),
    'editable': bool(direct.get('dir_info', {}).get('editable')),
}))
"""


def inspect_installation(prefix: Path) -> update.Installation:
    """Read the old installed version even when its CLI has no update command."""
    if sys.platform != "linux" or os.geteuid() == 0:
        raise update.UpdateError(
            "Run the updater as the ordinary installing Linux user."
        )
    prefix = Path(prefix).expanduser().resolve()
    venv = prefix / "venv"
    update._owned(prefix, directory=True)
    update._owned(venv, directory=True)
    marker = venv / ".kt-mcp-managed"
    update._owned(marker)
    update._owned(venv / "pyvenv.cfg")
    if marker.read_text(encoding="utf-8").strip() != update.MARKER:
        raise update.UpdateError(
            "Use --update only for an existing install.sh installation."
        )
    raw = update._command(
        [venv / "bin/python", "-I", "-c", PROBE],
        cwd=Path(__file__).parent,
        timeout=30,
        label="Existing installation inspection",
    )
    if len(raw) > 65536:
        raise update.UpdateError("The existing installation returned invalid metadata.")
    record = json.loads(raw)
    if (
        not isinstance(record, dict)
        or record.get("editable") is not False
        or record.get("version") != record.get("metadata_version")
        or Path(record["prefix"]).resolve() != venv
        or not Path(record["module"]).resolve().is_relative_to(venv)
        or not Path(record["package"]).resolve().is_relative_to(venv)
    ):
        raise update.UpdateError(
            "The existing installation is editable or does not match this prefix."
        )
    installation = update.Installation(
        prefix,
        venv / "bin/python",
        Path(record["base_python"]).resolve(),
        releases.version_text(record["version"]),
    )
    update._validate_installation(installation)
    current = update._command(
        [
            installation.base_python,
            "-I",
            "-c",
            "import sys,venv,ensurepip; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        cwd=Path(__file__).parent,
        timeout=30,
        label="Original Python/venv check",
    )
    if current != record["python_version"]:
        raise update.UpdateError(
            "The original Python version changed; use a new install prefix."
        )
    return installation


def main(prefix: Path, manifest: Path, version: str, name: str | None = None) -> int:
    try:
        if name and (
            len(name) > 80 or not re.fullmatch(r"kt-mcp(?:-[A-Za-z0-9_-]+)?", name)
        ):
            raise update.UpdateError("Use a service name such as kt-mcp or kt-mcp-dev.")
        release = releases.parse_manifest(Path(manifest).read_bytes(), version)
        installation = inspect_installation(prefix)
        current, target = releases.version_key(
            installation.version
        ), releases.version_key(release.version)
        if target < current:
            raise update.UpdateError(
                "Downgrades are not supported by automatic update."
            )
        if target == current:
            update._say(
                f"Up to date · {installation.version}", color="green", bold=True
            )
            return 0
        update._say(f"Updating {installation.version} → {release.version}", bold=True)
        update._handoff(installation, release, name or None)
    except (update.UpdateError, releases.ReleaseError) as error:
        print(f"Update: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print(
            "Update: could not inspect the selected installation; installed files are unchanged.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Update cancelled; installed files are unchanged.", file=sys.stderr)
        return 130
    return 0
