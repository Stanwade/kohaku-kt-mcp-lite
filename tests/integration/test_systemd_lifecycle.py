"""Opt-in tests against a real user systemd manager, never a mocked daemon.

Run with KT_MCP_TEST_SYSTEMD=1 on a Linux host with systemctl --user available.
Every instance uses a unique unit, private temp config and loopback port.
"""

import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kt_mcp.daemon import DaemonConfig, save_config
from kt_mcp.desktop import DesktopSettings

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("KT_MCP_TEST_SYSTEMD") != "1",
    reason="set KT_MCP_TEST_SYSTEMD=1 to exercise a real user systemd manager",
)


def quit_tui_in_terminal(path, name):
    import fcntl
    import pty
    import select
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-m", "kt_mcp", "tui", "--config", str(path), "--name", name],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color", "LC_ALL": "C.UTF-8"},
        start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
                if b"Quit panel" in output:
                    break
            if process.poll() is not None:
                break
        assert b"Quit panel" in output, output.decode("utf-8", errors="replace")
        os.write(master, b"q")
        assert process.wait(timeout=10) == 0
        return output.decode("utf-8", errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)


def wait_ready(manager, path, *, previous_pid=None, timeout=30):
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = manager.status()
        try:
            status = json.loads(path.with_suffix(".status.json").read_text())
            pid = int(last.get("MainPID", "0"))
            if (
                last.get("ActiveState") == "active"
                and pid > 0
                and pid != previous_pid
                and status.get("pid") == pid
            ):
                return pid
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    pytest.fail(f"systemd daemon did not become ready: {last}\n{manager.logs()}")


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="the user-manager test runs as an ordinary account",
)
async def test_real_systemd_restart_cleanup_and_independent_lifetime(tmp_path):
    from kt_mcp.service import ServiceManager

    workspace = tmp_path / 'workspace 空间 % $ "quote"'
    workspace.mkdir()
    venv_alias = tmp_path / 'venv % $ ${HOME} "quote"'
    venv_alias.symlink_to(sys.prefix, target_is_directory=True)
    path = tmp_path / 'private % $ ${HOME} "quote"' / "config.json"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    save_config(
        DaemonConfig(DesktopSettings(str(workspace), port, "local"), token), path
    )
    name = "kt-mcp-smoke-" + secrets.token_hex(6)
    manager = ServiceManager(name=name)
    try:
        # The registering CLI exits; the systemd daemon must remain alive.
        result = await asyncio.to_thread(
            subprocess.run,
            [
                str(venv_alias / "bin/python"),
                "-m",
                "kt_mcp",
                "service",
                "install",
                "--name",
                name,
                "--config",
                str(path),
                "--now",
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr
        assert token not in result.stdout + result.stderr
        first_pid = await asyncio.to_thread(wait_ready, manager, path)
        assert manager.status()["UnitFileState"] == "enabled"
        assert token not in manager.unit_path.read_text()
        terminal_output = await asyncio.to_thread(quit_tui_in_terminal, path, name)
        assert token not in terminal_output
        assert int(manager.status()["MainPID"]) == first_pid
        assert manager.status()["ActiveState"] == "active"

        url = f"http://127.0.0.1:{port}/mcp/{token}"
        async with (
            httpx.AsyncClient(trust_env=False) as http,
            streamable_http_client(url, http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            assert len((await session.list_tools()).tools) == 12
            child = (
                "import time; from pathlib import Path; "
                "Path('systemd-child-started').touch(); "
                "time.sleep(6); Path('systemd-orphan').touch()"
            )
            code = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
                "time.sleep(30)"
            )
            result = await session.call_tool(
                "python", {"code": code, "run_in_background": True}
            )
            assert not result.isError
            for _ in range(100):
                if (workspace / "systemd-child-started").exists():
                    break
                await asyncio.sleep(0.05)
            assert (workspace / "systemd-child-started").exists()

        # SIGKILL cannot run Python cleanup: systemd must clean the entire cgroup
        # and Restart=on-failure must create a fresh daemon.
        os.kill(first_pid, signal.SIGKILL)
        restarted_pid = await asyncio.to_thread(
            wait_ready, manager, path, previous_pid=first_pid
        )
        assert int(manager.status()["NRestarts"]) >= 1
        await asyncio.sleep(1.2)
        assert not (workspace / "systemd-orphan").exists()
        logs = manager.logs()
        assert "[state]" in logs
        assert token not in logs

        await asyncio.to_thread(manager.control, "stop")
        assert manager.status()["ActiveState"] == "inactive"
        assert not path.with_suffix(".status.json").exists()
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))

        await asyncio.to_thread(manager.control, "start")
        await asyncio.to_thread(wait_ready, manager, path, previous_pid=restarted_pid)
        await asyncio.to_thread(manager.control, "disable", now=True)
        status = manager.status()
        assert status["ActiveState"] == "inactive"
        assert status["UnitFileState"] == "disabled"
    finally:
        if manager.unit_path.exists():
            await asyncio.to_thread(manager.uninstall)
    assert not manager.unit_path.exists()


@pytest.mark.skipif(
    not os.environ.get("KT_MCP_SYSTEM_TEST_USER")
    or sys.platform != "linux"
    or os.geteuid() != 0,
    reason="system-scope test requires root and KT_MCP_SYSTEM_TEST_USER",
)
def test_real_system_service_uses_requested_ordinary_account():
    import pwd

    from kt_mcp.service import ServiceManager

    account = pwd.getpwnam(os.environ["KT_MCP_SYSTEM_TEST_USER"])
    assert account.pw_uid != 0
    with tempfile.TemporaryDirectory(prefix="kt-mcp-system-") as directory:
        base = Path(directory)
        os.chown(base, account.pw_uid, account.pw_gid)
        workspace = base / "workspace"
        workspace.mkdir()
        os.chown(workspace, account.pw_uid, account.pw_gid)
        path = base / "config.json"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token = secrets.token_urlsafe(32)
        save_config(
            DaemonConfig(DesktopSettings(str(workspace), port, "local"), token), path
        )
        os.chown(path, account.pw_uid, account.pw_gid)
        manager = ServiceManager(
            "kt-mcp-system-smoke-" + secrets.token_hex(6), system=True
        )
        try:
            manager.install(path, run_as=account.pw_name, now=True)
            pid = wait_ready(manager, path)
            uid_line = next(
                line
                for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                if line.startswith("Uid:")
            )
            assert set(uid_line.split()[1:]) == {str(account.pw_uid)}
            assert manager.status()["UnitFileState"] == "enabled"
            assert token not in manager.unit_path.read_text()
            assert token not in manager.logs()
            manager.control("restart")
            wait_ready(manager, path, previous_pid=pid)
        finally:
            if manager.unit_path.exists():
                manager.uninstall()
        assert not manager.unit_path.exists()
