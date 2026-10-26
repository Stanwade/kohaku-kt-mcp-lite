import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


@asynccontextmanager
async def connect(transport, workspace):
    arguments = ["-m", "kt_mcp", "--workspace", str(workspace)]
    if transport == "stdio":
        parameters = StdioServerParameters(
            command=sys.executable, args=arguments, cwd=str(workspace)
        )
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            yield session
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *arguments,
        "--transport",
        "http",
        "--port",
        str(port),
        cwd=workspace,
        env={**os.environ, "KT_MCP_TOKEN": token},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    base_url = f"http://127.0.0.1:{port}/mcp"
    url = base_url + "/" + token
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2) as http:
            for _ in range(200):
                assert process.returncode is None, "HTTP CLI exited during startup"
                try:
                    response = await http.get(base_url)
                    if response.status_code == 404:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("HTTP server did not start")
        async with (
            httpx.AsyncClient(trust_env=False) as http,
            streamable_http_client(url, http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            yield session
    finally:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=10)


class TestMCPWorkflow:
    @pytest.mark.parametrize("transport", ["stdio", "http"])
    async def test_files_commands_jobs_and_restart(self, tmp_path, transport):
        existing = tmp_path / "existing.txt"
        existing.write_text("original")
        async with connect(transport, tmp_path) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "Kohaku KT MCP"
            advertised = {
                tool.name: tool for tool in (await session.list_tools()).tools
            }
            assert set(advertised) == {
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

            async def call(name, args=None, error=False):
                result = await session.call_tool(name, args or {})
                assert result.isError is error, result
                if result.structuredContent is None:
                    return {"error": result.content[0].text}
                assert json.loads(result.content[0].text) == result.structuredContent
                return result.structuredContent

            blocked = await call(
                "write", {"path": "existing.txt", "content": "oops"}, True
            )
            assert "Read the existing" in blocked["error"]
            assert existing.read_text() == "original"
            assert (
                "original" in (await call("read", {"path": "existing.txt"}))["output"]
            )
            existing.write_text("external update")
            stale = await call(
                "edit",
                {
                    "path": "existing.txt",
                    "old_string": "external",
                    "new_string": "local",
                },
                True,
            )
            assert "changed since" in stale["error"]
            await call("read", {"path": "existing.txt"})
            await call(
                "edit",
                {
                    "path": "existing.txt",
                    "old_string": "external",
                    "new_string": "local",
                },
            )
            assert existing.read_text() == "local update"
            await call(
                "write", {"path": "nested/new.txt", "content": "red red\nblue\n"}
            )
            ambiguous = await call(
                "edit",
                {"path": "nested/new.txt", "old_string": "red", "new_string": "green"},
                True,
            )
            assert "ambiguous" in ambiguous["error"]
            await call(
                "multi_edit",
                {
                    "path": "nested/new.txt",
                    "edits": [
                        {
                            "old_string": "red",
                            "new_string": "green",
                            "replace_all": True,
                        },
                        {"old_string": "blue", "new_string": "yellow"},
                    ],
                },
            )
            assert (tmp_path / "nested/new.txt").read_text() == "green green\nyellow\n"
            assert (
                "nested/new.txt"
                in (await call("glob", {"pattern": "**/*.txt"}))["files"]
            )
            assert (await call("grep", {"pattern": "yellow"}))["matches"][0][
                "line"
            ] == 2
            assert "nested/" in (await call("tree"))["entries"]
            assert (
                "outside"
                in (await call("read", {"path": "../outside.txt"}, True))["error"]
            )
            assert "error" in await call(
                "read", {"path": "existing.txt", "offset": -1}, True
            )
            assert "error" in await call("tree", {"unexpected": True}, True)
            assert "error" in await call("nonexistent", {}, True)
            code = "import sys; from pathlib import Path; assert 'kohakuterrarium' not in sys.modules; Path('command.txt').write_text('ok'); print('python-ok')"
            assert (await call("python", {"code": code}))[
                "output"
            ].strip() == "python-ok"
            assert (tmp_path / "command.txt").read_text() == "ok"
            failed = await call("python", {"code": "raise SystemExit(9)"}, True)
            assert failed["exit_code"] == 9
            timed = await call(
                "python", {"code": "import time; time.sleep(30)", "timeout": 0.1}, True
            )
            assert "timed out" in timed["error"]
            job = await call(
                "python",
                {
                    "code": "import time; time.sleep(0.2); print('background-ok')",
                    "run_in_background": True,
                },
            )
            finished = await call("job_wait", {"job_id": job["job_id"], "timeout": 10})
            assert finished["state"] == "completed"
            assert finished["result"]["output"].strip() == "background-ok"
            job = await call(
                "python",
                {"code": "import time; time.sleep(30)", "run_in_background": True},
            )
            assert (await call("job_wait", {"job_id": job["job_id"], "timeout": 0}))[
                "state"
            ] == "running"
            assert (await call("job_cancel", {"job_id": job["job_id"]}))[
                "state"
            ] == "cancelled"
            assert (await call("job_status", {"job_id": job["job_id"]}))[
                "state"
            ] == "cancelled"
            assert len((await call("job_status"))["jobs"]) >= 4
            if transport == "stdio":
                await call(
                    "python",
                    {
                        "code": "import time; from pathlib import Path; Path('shutdown-started').touch(); time.sleep(2); Path('shutdown-orphan').touch()",
                        "run_in_background": True,
                    },
                )
                for _ in range(100):
                    if (tmp_path / "shutdown-started").exists():
                        break
                    await asyncio.sleep(0.02)
                assert (tmp_path / "shutdown-started").exists()
        if transport == "stdio":
            await asyncio.sleep(2.1)
            assert not (tmp_path / "shutdown-orphan").exists()
        async with connect(transport, tmp_path) as restarted:
            await restarted.initialize()
            expired = await restarted.call_tool("job_status", {"job_id": job["job_id"]})
            assert expired.isError and "expired" in expired.structuredContent["error"]
            unread = await restarted.call_tool(
                "write", {"path": "existing.txt", "content": "lost"}
            )
            assert unread.isError
            assert existing.read_text() == "local update"
