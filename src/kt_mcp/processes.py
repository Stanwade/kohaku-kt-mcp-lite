"""Bounded subprocess execution and process-tree cleanup for standalone tools."""

import asyncio
import os
import shutil
import signal
import subprocess
from pathlib import Path

from kt_mcp.bundled import python_command
from kt_mcp.config import Config


def find_bash() -> str:
    """Find Bash, preferring native Git Bash on Windows."""
    candidates = []
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            for parent in Path(git).parents:
                if parent.name.lower() == "git":
                    candidates.append(parent / "bin" / "bash.exe")
        for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(variable):
                candidates.append(Path(os.environ[variable]) / "Git/bin/bash.exe")
        if os.environ.get("LOCALAPPDATA"):
            candidates.append(
                Path(os.environ["LOCALAPPDATA"]) / "Programs/Git/bin/bash.exe"
            )
    found = shutil.which("bash")
    if found and not (os.name == "nt" and "windows" in str(Path(found).parent).lower()):
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise ValueError("Bash was not found; install Git for Windows or Bash on PATH")


async def _terminate(process):
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def execute(config: Config, kind: str, source: str, timeout: float = 60) -> dict:
    """Run Bash or fresh Python with bounded combined stdout and stderr."""
    command = (
        [find_bash(), "--noprofile", "--norc", "-c", source]
        if kind == "bash"
        else await asyncio.to_thread(python_command, source)
    )
    options = (
        {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=config.workspace,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **options,
    )
    output = bytearray()
    truncated = False

    async def collect():
        nonlocal truncated
        while chunk := await process.stdout.read(8192):
            remaining = max(0, config.max_output_bytes - len(output))
            output.extend(chunk[:remaining])
            truncated |= len(chunk) > remaining
        await process.wait()

    reader = asyncio.create_task(collect())
    error = None
    try:
        await asyncio.wait_for(asyncio.shield(reader), timeout)
    except asyncio.TimeoutError:
        error = f"Command timed out after {timeout} seconds"
    finally:
        # Reap descendants even if the original shell has already exited.
        await _terminate(process)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    result = {
        "output": bytes(output).decode("utf-8", errors="replace"),
        "exit_code": process.returncode,
        "truncated": truncated,
    }
    if error:
        result["error"] = error
    return result
