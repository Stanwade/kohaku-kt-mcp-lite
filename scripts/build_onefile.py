"""Build the Windows x64 desktop EXE with Nuitka and a bundled Python worker."""

import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kt_mcp.bundled import PYTHON_ARCHIVE, PYTHON_SHA256, PYTHON_VERSION


def main():
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SystemExit("This build script requires Windows x64 and x64 Python.")
    assets = ROOT / ".build-assets"
    assets.mkdir(exist_ok=True)
    archive = assets / PYTHON_ARCHIVE
    if (
        not archive.is_file()
        or hashlib.sha256(archive.read_bytes()).hexdigest() != PYTHON_SHA256
    ):
        url = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_ARCHIVE}"
        print(f"Downloading bundled Python worker: {url}", flush=True)
        partial = archive.with_suffix(".partial")
        try:
            with urlopen(url, timeout=60) as response, partial.open("wb") as output:
                shutil.copyfileobj(response, output)
            if hashlib.sha256(partial.read_bytes()).hexdigest() != PYTHON_SHA256:
                raise SystemExit("Bundled Python SHA256 mismatch")
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)
    output = ROOT / "dist" / "nuitka"
    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--mode=onefile",
        "--enable-plugin=tk-inter",
        "--windows-console-mode=disable",
        "--assume-yes-for-downloads",
        "--msvc=latest",
        "--jobs=4",
        "--lto=no",
        f"--output-dir={output}",
        "--output-filename=KT-MCP.exe",
        f"--include-data-files={archive}=kt_mcp/_vendor/{PYTHON_ARCHIVE}",
        f"--include-data-files={ROOT / 'LICENSE'}=LICENSE",
        f"--include-data-files={ROOT / 'NOTICE'}=NOTICE",
        "--include-package=uvicorn",
        "--include-module=anyio._backends._asyncio",
        "--include-distribution-metadata=mcp",
        "--include-distribution-metadata=kohaku-kt-mcp",
        f"--report={output / 'compilation-report.xml'}",
        "--company-name=Kohaku KT MCP",
        "--product-name=Kohaku KT MCP",
        "--file-description=Kohaku KT MCP desktop with Cloudflare Tunnel",
        "--file-version=0.1.0.0",
        "--product-version=0.1.0.0",
        str(ROOT / "packaging" / "desktop_entry.py"),
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    executable = ROOT / "dist" / "KT-MCP.exe"
    shutil.copy2(output / "KT-MCP.exe", executable)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    executable.with_suffix(".exe.sha256").write_text(
        f"{digest}  {executable.name}\n", encoding="ascii"
    )
    print(
        f"Built {executable} ({executable.stat().st_size / 1024 / 1024:.1f} MiB)\nSHA256 {digest}",
        flush=True,
    )


if __name__ == "__main__":
    main()
