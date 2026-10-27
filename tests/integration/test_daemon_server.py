import asyncio
import json
import os
import signal
import socket
import stat
import subprocess
import sys

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kt_mcp.daemon import DaemonConfig, save_config
from kt_mcp.desktop import DesktopSettings

TOKEN = "integration-daemon-token-" + "d" * 32

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux daemon commands")


def clean_environment():
    environment = dict(os.environ)
    for key in ("KT_MCP_TOKEN", "TUNNEL_TOKEN"):
        environment.pop(key, None)
    return environment


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def cli(*arguments, cwd):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kt_mcp",
        *arguments,
        cwd=cwd,
        env=clean_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        return process.returncode, stdout.decode("utf-8"), stderr.decode("utf-8")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def wait_ready(process, port, status_path, log_path):
    async with httpx.AsyncClient(trust_env=False, timeout=1) as http:
        for _ in range(300):
            if process.returncode is not None:
                pytest.fail(
                    "Daemon exited during startup: "
                    + log_path.read_text(encoding="utf-8", errors="replace")
                )
            try:
                response = await http.get(f"http://127.0.0.1:{port}/mcp")
                if response.status_code == 404 and status_path.exists():
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    pytest.fail("Daemon did not become ready")


async def test_init_and_client_config_do_not_overwrite_credentials(tmp_path):
    path = tmp_path / "private/config.json"
    arguments = (
        "init",
        "--workspace",
        str(tmp_path),
        "--config",
        str(path),
        "--mode",
        "local",
    )
    code, stdout, stderr = await cli(*arguments, cwd=tmp_path)
    assert code == 0, stderr
    original = path.read_bytes()
    payload = json.loads(original)
    assert payload["version"] == 1
    assert len(payload["token"]) >= 32
    assert payload["token"] not in stdout + stderr
    code, _, _ = await cli(*arguments, cwd=tmp_path)
    assert code != 0
    assert path.read_bytes() == original
    code, stdout, stderr = await cli(
        "client-config", "--config", str(path), cwd=tmp_path
    )
    assert code == 0, stderr
    assert json.loads(stdout) == {
        "mcpServers": {
            "kt-mcp": {"url": "http://127.0.0.1:8765/mcp/" + payload["token"]}
        }
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX daemon shutdown signals")
@pytest.mark.parametrize("shutdown_signal", [signal.SIGTERM, signal.SIGINT])
async def test_daemon_mcp_signal_cleanup_and_private_status(tmp_path, shutdown_signal):
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    port = free_port()
    path = tmp_path / "private/config.json"
    status_path = path.with_suffix(".status.json")
    config = DaemonConfig(DesktopSettings(str(workspace), port, "local"), TOKEN)
    save_config(config, path)
    log_path = tmp_path / "daemon.log"
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "kt_mcp",
            "daemon",
            "--config",
            str(path),
            cwd=workspace,
            env=clean_environment(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            await wait_ready(process, port, status_path, log_path)
            assert stat.S_IMODE(status_path.stat().st_mode) == 0o600
            status = json.loads(status_path.read_text(encoding="utf-8"))
            assert status["pid"] == process.pid
            url = f"http://127.0.0.1:{port}/mcp/{TOKEN}"
            assert status["url"] == url
            async with (
                httpx.AsyncClient(trust_env=False) as http,
                streamable_http_client(url, http_client=http) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                assert (await session.initialize()).serverInfo.name == "Kohaku KT MCP"
                assert {tool.name for tool in (await session.list_tools()).tools} == {
                    "read",
                    "write",
                    "edit",
                    "multi_edit",
                    "glob",
                    "grep",
                    "tree",
                    "bash",
                    "python",
                    "job_status",
                    "job_wait",
                    "job_cancel",
                }
                child_source = (
                    "from pathlib import Path; import time; "
                    "Path('child-started').touch(); time.sleep(2); "
                    "Path('child-finished').touch()"
                )
                source = (
                    "import subprocess, sys, time; from pathlib import Path; "
                    f"subprocess.Popen([sys.executable, '-c', {child_source!r}]); "
                    "Path('parent-started').touch(); time.sleep(30)"
                )
                result = await session.call_tool(
                    "python", {"code": source, "run_in_background": True}
                )
                assert not result.isError
                for _ in range(150):
                    if (workspace / "child-started").exists():
                        break
                    await asyncio.sleep(0.02)
                assert (workspace / "parent-started").exists()
                assert (workspace / "child-started").exists()
            process.send_signal(shutdown_signal)
            assert await asyncio.wait_for(process.wait(), timeout=15) == 0
            assert not status_path.exists()
            with socket.socket() as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", port))
            await asyncio.sleep(2.2)
            assert not (workspace / "child-finished").exists()
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=15)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
    logs = log_path.read_text(encoding="utf-8", errors="replace")
    assert TOKEN not in logs
    assert "/mcp/" + TOKEN not in logs


async def test_daemon_startup_failure_is_nonzero_and_leaves_listener(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        path = tmp_path / "config.json"
        config = DaemonConfig(DesktopSettings(str(tmp_path), port, "local"), TOKEN)
        save_config(config, path)
        code, stdout, stderr = await cli("daemon", "--config", str(path), cwd=tmp_path)
        assert code != 0
        assert TOKEN not in stdout + stderr
        assert not path.with_suffix(".status.json").exists()
        assert listener.getsockname()[1] == port
