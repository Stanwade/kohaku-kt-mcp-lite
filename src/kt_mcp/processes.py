"""Bounded subprocess execution and process-tree cleanup for standalone tools."""

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from kt_mcp import shell_startup
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


class _Capture:
    def __init__(self, maximum):
        self.maximum = maximum
        self.data = bytearray()
        self.truncated = False

    async def collect(self, reader):
        while chunk := await reader.read(8192):
            remaining = max(0, self.maximum - len(self.data))
            self.data.extend(chunk[:remaining])
            self.truncated |= len(chunk) > remaining

    def text(self):
        return bytes(self.data).decode("utf-8", errors="replace")


async def _read_pipe(descriptor):
    reader = asyncio.StreamReader()
    pipe = os.fdopen(descriptor, "rb", buffering=0)
    try:
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), pipe
        )
    except BaseException:
        pipe.close()
        raise
    return reader, transport


async def _wait_for_command(process, output_reader):
    if sys.platform != "linux":
        # Keep the existing wait/drain behavior on other platforms.
        await asyncio.shield(output_reader)
        await process.wait()
        return
    # Process.wait() can also wait for inherited stdout pipes to close, even
    # after returncode is set. Observe the leader directly so a surviving child
    # cannot hold those pipes open and postpone its own cleanup.
    while process.returncode is None:
        await asyncio.sleep(0.01)


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
    """Run commands with bounded output and the Linux user's shell environment."""
    arguments = (
        [source] if kind == "bash" else await asyncio.to_thread(python_command, source)
    )
    bash = None
    if kind == "bash" or sys.platform == "linux":
        try:
            bash = find_bash()
        except ValueError:
            if kind == "bash":
                raise
            # A Python-only installation still works without a Bash dependency.
    initialize = sys.platform == "linux" and bash is not None
    options = (
        {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    output = _Capture(config.max_output_bytes)
    startup = _Capture(config.max_output_bytes)
    status = _Capture(2)
    transports = []
    readers = []
    write_descriptors = []
    process = None
    error = None

    def close_writes():
        while write_descriptors:
            os.close(write_descriptors.pop())

    try:
        if initialize:
            startup_read, startup_write = os.pipe()
            write_descriptors.append(startup_write)
            reader, transport = await _read_pipe(startup_read)
            transports.append(transport)
            readers.append(asyncio.create_task(startup.collect(reader)))
            status_read, status_write = os.pipe()
            write_descriptors.append(status_write)
            reader, transport = await _read_pipe(status_read)
            transports.append(transport)
            readers.append(asyncio.create_task(status.collect(reader)))
            command = shell_startup.command(
                bash,
                config.workspace,
                kind,
                arguments,
                startup_write,
                status_write,
            )
            options["pass_fds"] = (startup_write, status_write)
        elif kind == "bash":
            command = [bash, "--noprofile", "--norc", "-c", source]
        else:
            command = arguments

        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                cwd=config.workspace,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                # The fixed bootstrap restores stderr before executing any
                # profile; only Bash's initial no-terminal warnings are hidden.
                stderr=(
                    asyncio.subprocess.DEVNULL
                    if initialize
                    else asyncio.subprocess.STDOUT
                ),
                **options,
            )
        )
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            # Finish acquiring ownership before propagating cancellation.
            # Otherwise a process created during this await can be orphaned.
            try:
                process = await spawning
            except (OSError, ValueError):
                process = None
            if process is not None:
                readers.append(asyncio.create_task(output.collect(process.stdout)))
            raise
        finally:
            close_writes()

        readers.append(asyncio.create_task(output.collect(process.stdout)))
        try:
            await asyncio.wait_for(_wait_for_command(process, readers[-1]), timeout)
        except asyncio.TimeoutError:
            error = f"Command timed out after {timeout} seconds"
    finally:
        close_writes()
        try:
            if process is not None:
                # Reap descendants before draining pipes, including startup
                # children that survive the main shell's successful exit.
                await _terminate(process)
            if readers:
                if process is None:
                    for reader in readers:
                        reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
        finally:
            for reader in readers:
                reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            for transport in transports:
                transport.close()

    if initialize and bytes(status.data) != b"SR" and error is None:
        error = (
            "Shell initialization exited before the command started"
            if bytes(status.data).startswith(b"S")
            else "Shell initialization could not be started"
        )
    result = {
        "output": output.text(),
        "exit_code": process.returncode,
        "truncated": output.truncated,
    }
    if startup.data or startup.truncated:
        result["startup_output"] = startup.text()
        result["startup_truncated"] = startup.truncated
    if error:
        result["error"] = error
    return result
