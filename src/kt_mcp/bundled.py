"""Locate the Python worker in a Nuitka build without relaunching the GUI."""

import hashlib
import os
import sys
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath

from kt_mcp.tunnel import data_directory

PYTHON_VERSION = "3.13.17"
PYTHON_ARCHIVE = f"python-{PYTHON_VERSION}-embed-amd64.zip"
# SHA256 published in the release's Sigstore bundle on python.org.
PYTHON_SHA256 = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"
_install_lock = threading.Lock()


def is_compiled() -> bool:
    return "__compiled__" in globals()


def install_python(archive: Path, directory: Path) -> Path:
    """Verify and atomically unpack the CPython worker shipped inside the EXE."""
    target = directory / f"python-{PYTHON_VERSION}-{PYTHON_SHA256[:12]}"
    executable = target / "python.exe"
    with _install_lock:
        if executable.is_file() and (target / ".ready").is_file():
            return executable
        if hashlib.sha256(archive.read_bytes()).hexdigest() != PYTHON_SHA256:
            raise ValueError("内置 Python 运行时校验失败，请重新下载应用")
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="python-install-", dir=directory
        ) as temporary:
            staging = Path(temporary) / "runtime"
            staging.mkdir()
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    # The official embeddable distribution has only flat files.
                    parts = PurePosixPath(member.filename).parts
                    if (
                        len(parts) != 1
                        or member.filename in (".", "..")
                        or "\\" in member.filename
                        or ":" in member.filename
                    ):
                        raise ValueError("内置 Python 压缩包包含无效路径")
                bundle.extractall(staging)
            if not (staging / "python.exe").is_file():
                raise ValueError("内置 Python 运行时缺少可执行文件")
            # Normal -c semantics include the current workspace on sys.path.
            # -E -s below keeps host Python environment/user packages out.
            for path in staging.glob("*._pth"):
                path.unlink()
            (staging / ".ready").write_text(PYTHON_SHA256, encoding="ascii")
            try:
                staging.rename(target)
            except FileExistsError:
                # Another application instance finished the same install.
                if not executable.is_file() or not (target / ".ready").is_file():
                    raise
        return executable


def python_command(source: str) -> list[str]:
    if not is_compiled():
        return [sys.executable, "-X", "utf8", "-u", "-c", source]
    if os.name != "nt":
        raise ValueError("此单文件构建仅支持 Windows")
    archive = Path(__file__).resolve().parent / "_vendor" / PYTHON_ARCHIVE
    if not archive.is_file():
        raise ValueError("单文件构建缺少内置 Python 运行时")
    executable = install_python(archive, data_directory() / "runtimes")
    return [str(executable), "-E", "-s", "-X", "utf8", "-u", "-c", source]
