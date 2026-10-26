"""Automatically verify the real onefile GUI and all 12 tools over MCP."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kt_mcp.processes import find_bash

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--tunnel", action="store_true")
parser.add_argument("--executable", type=Path, default=ROOT / "dist/KT-MCP.exe")
arguments = parser.parse_args()
USE_TUNNEL = arguments.tunnel


async def verify(url, token, workspace):
    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                response = await client.get(url.rsplit("/", 1)[0])
                if response.status_code == 404:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
        else:
            raise TimeoutError("GUI start button was not pressed or server failed")
    print("ONEFILE HTTP authentication: PASS", flush=True)
    async with (
        httpx.AsyncClient(trust_env=False) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert names == {
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
        passed = set()

        async def call(name, arguments):
            result = await session.call_tool(name, arguments)
            assert not result.isError, result
            passed.add(name)
            return result.structuredContent

        await call("write", {"path": "中文测试.txt", "content": "onefile-ok"})
        assert "onefile-ok" in (await call("read", {"path": "中文测试.txt"}))["output"]
        await call(
            "edit",
            {"path": "中文测试.txt", "old_string": "onefile", "new_string": "packaged"},
        )
        await call(
            "multi_edit",
            {
                "path": "中文测试.txt",
                "edits": [
                    {"old_string": "packaged", "new_string": "edited"},
                    {"old_string": "ok", "new_string": "success"},
                ],
            },
        )
        assert (workspace / "中文测试.txt").read_text(
            encoding="utf-8"
        ) == "edited-success"
        await call("write", {"path": "nested/another.txt", "content": "alpha\nbeta\n"})
        assert (
            "nested/another.txt"
            in (await call("glob", {"pattern": "**/*.txt"}))["files"]
        )
        matches = (await call("grep", {"pattern": "^BETA$", "ignore_case": True}))[
            "matches"
        ]
        assert any(
            item["path"] == "nested/another.txt" and item["line"] == 2
            for item in matches
        )
        assert "nested/another.txt" in (await call("tree", {"depth": 3}))["entries"]
        print(
            "ONEFILE file read/write/edit/multi_edit/glob/grep/tree: PASS", flush=True
        )
        blocked = await session.call_tool("read", {"path": "../outside.txt"})
        assert blocked.isError
        (workspace / "unread.txt").write_text("original")
        unread = await session.call_tool(
            "write", {"path": "unread.txt", "content": "overwritten"}
        )
        assert unread.isError
        assert (workspace / "unread.txt").read_text() == "original"
        before = (workspace / "中文测试.txt").read_text(encoding="utf-8")
        atomic = await session.call_tool(
            "multi_edit",
            {
                "path": "中文测试.txt",
                "edits": [
                    {"old_string": "edited", "new_string": "partial"},
                    {"old_string": "absent", "new_string": "fail"},
                ],
            },
        )
        assert atomic.isError
        assert (workspace / "中文测试.txt").read_text(encoding="utf-8") == before
        result = await call(
            "bash",
            {
                "command": "printf packaged-bash-ok > bash-output.txt; printf 'bash-stdout-ok 中文输出'"
            },
        )
        assert (
            result["exit_code"] == 0 and result["output"] == "bash-stdout-ok 中文输出"
        ), result
        assert (workspace / "bash-output.txt").read_text() == "packaged-bash-ok"
        bash_failed = await session.call_tool("bash", {"command": "exit 9"})
        assert bash_failed.isError and bash_failed.structuredContent["exit_code"] == 9
        print("ONEFILE Bash cwd/stdout/exit-code: PASS", flush=True)
        code = "import sys, ssl, sqlite3, ctypes, localmodule; from pathlib import Path; assert localmodule.value == 42; assert Path.cwd().name == 'workspace'; print(sys.executable); print(sys.version); print('embedded-python-ok 中文输出 ✓')"
        result = await call("python", {"code": code})
        assert result["exit_code"] == 0, result
        assert "runtimes" in result["output"], result
        assert "embedded-python-ok" in result["output"], result
        assert "中文输出 ✓" in result["output"], result
        print(
            "ONEFILE independent Python, stdlib, SQLite, SSL, workspace imports: PASS",
            flush=True,
        )
        await call("python", {"code": "state=42"})
        fresh = await session.call_tool("python", {"code": "print(state)"})
        assert fresh.isError
        failed = await session.call_tool("python", {"code": "raise SystemExit(7)"})
        assert failed.isError and failed.structuredContent["exit_code"] == 7
        timed = await session.call_tool(
            "python", {"code": "import time; time.sleep(30)", "timeout": 0.15}
        )
        assert timed.isError and "timed out" in timed.structuredContent["error"]
        job = await call(
            "python",
            {
                "code": "import time; time.sleep(.5); print('background-ok')",
                "run_in_background": True,
            },
        )
        assert (await call("job_status", {"job_id": job["job_id"]}))["job_id"] == job[
            "job_id"
        ]
        assert any(
            item["job_id"] == job["job_id"]
            for item in (await call("job_status", {}))["jobs"]
        )
        complete = await call("job_wait", {"job_id": job["job_id"], "timeout": 10})
        assert (
            complete["state"] == "completed"
            and complete["result"]["output"].strip() == "background-ok"
        )
        descendant_code = "import time; from pathlib import Path; Path('descendant-started').touch(); time.sleep(3); Path('descendant-finished').touch()"
        cancel_code = (
            "import subprocess,sys,time; from pathlib import Path; "
            f"subprocess.Popen([sys.executable, '-E', '-s', '-c', {descendant_code!r}], creationflags=subprocess.CREATE_NO_WINDOW); "
            "Path('job-started').touch(); time.sleep(30); Path('cancelled-job-finished').touch()"
        )
        job = await call(
            "python",
            {
                "code": cancel_code,
                "run_in_background": True,
            },
        )
        deadline = time.monotonic() + 5
        while (
            not (workspace / "descendant-started").exists()
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.05)
        assert (workspace / "job-started").exists()
        assert (workspace / "descendant-started").exists()
        cancelled = await call("job_cancel", {"job_id": job["job_id"]})
        assert cancelled["state"] == "cancelled"
        await asyncio.sleep(3.1)
        assert not (workspace / "cancelled-job-finished").exists()
        assert not (workspace / "descendant-finished").exists()
        invalid = await session.call_tool("tree", {"unexpected": True})
        assert invalid.isError
        assert passed == names, passed
        print(
            "ONEFILE jobs status/list/wait/cancel and process cleanup: PASS", flush=True
        )
        print(
            "ONEFILE all 12 tools plus guard/atomicity/timeout/schema error cases: PASS",
            flush=True,
        )
    return {
        "authentication": True,
        "tools": {name: "PASS" for name in sorted(passed)},
        "guards": True,
        "atomic_edits": True,
        "python_worker": True,
        "python_fresh_state": True,
        "unicode_stdout": True,
        "command_timeout": True,
        "job_cancellation": True,
        "process_tree_cleanup": True,
    }


with tempfile.TemporaryDirectory(
    prefix="kt onefile 验证 ", ignore_cleanup_errors=True
) as temporary:
    directory = Path(temporary)
    portable = directory / "portable"
    portable.mkdir()
    executable = portable / "KT-MCP.exe"
    shutil.copy2(arguments.executable, executable)
    workspace = directory / "workspace"
    workspace.mkdir()
    (workspace / "localmodule.py").write_text("value=42", encoding="utf-8")
    appdata = directory / "AppData"
    settings = appdata / "Kohaku KT MCP/desktop.json"
    settings.parent.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    tunnel_binary = (
        Path(os.environ["LOCALAPPDATA"]) / "Kohaku KT MCP/bin/cloudflared.exe"
    )
    settings.write_text(
        json.dumps(
            {
                "workspace": str(workspace),
                "port": port,
                "mode": "quick" if USE_TUNNEL else "local",
                "cloudflared": (
                    str(tunnel_binary) if USE_TUNNEL and tunnel_binary.is_file() else ""
                ),
            }
        ),
        encoding="utf-8",
    )
    token = secrets.token_urlsafe(32)
    environment = dict(os.environ)
    environment.update(
        LOCALAPPDATA=str(appdata),
        KT_MCP_TOKEN=token,
        PATH=os.pathsep.join(
            [
                str(Path(os.environ["SystemRoot"]) / "System32"),
                str(Path(find_bash()).parent),
            ]
        ),
        PYTHONHOME="Z:\\missing-python",
        PYTHONPATH="Z:\\missing-python",
    )
    process = subprocess.Popen(
        [str(executable), "--verify-onefile", str(directory)],
        cwd=portable,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    print(
        f"PORTABLE EXE PID {process.pid}; HTTP port {port}; waiting for GUI startup",
        flush=True,
    )
    try:
        deadline = time.monotonic() + 180
        state = {}
        while time.monotonic() < deadline:
            try:
                state = json.loads(
                    (directory / "gui-state.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                pass
            if state.get("url"):
                break
            if process.poll() is not None:
                raise RuntimeError(f"EXE exited early: {process.returncode}")
            if "错误：" in state.get("logs", ""):
                raise RuntimeError(state["logs"])
            time.sleep(0.2)
        assert state.get("url"), state
        assert state["viewable"], state
        url = state["url"]
        print(f"ONEFILE GUI visible, geometry {state['geometry']}: PASS", flush=True)
        if USE_TUNNEL:
            assert url.startswith("https://") and url.endswith(
                ".trycloudflare.com/mcp/" + token
            )
            print(
                "ONEFILE Cloudflare Tunnel connected; testing all tools over public HTTPS",
                flush=True,
            )
        results = asyncio.run(verify(url, token, workspace))
        (directory / "stop").touch()
        process.wait(timeout=30)
        assert process.returncode == 0, process.returncode
        with socket.socket() as sock:
            assert sock.connect_ex(("127.0.0.1", port)) != 0
        results.update(
            portable=True,
            shutdown=True,
            exit_code=process.returncode,
            cloudflare_tunnel=USE_TUNNEL,
            gui_geometry=state["geometry"],
            gui_viewable=state["viewable"],
            binary_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        )
        report_name = (
            "onefile-smoke-tunnel.json" if USE_TUNNEL else "onefile-smoke.json"
        )
        (ROOT / "dist" / report_name).write_text(
            json.dumps(results, indent=2), encoding="utf-8"
        )
        print("ONEFILE relocated EXE and graceful shutdown: PASS", flush=True)
    finally:
        if process.poll() is None:
            subprocess.run(
                [
                    str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"),
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                capture_output=True,
                check=False,
            )
            process.wait(timeout=20)
