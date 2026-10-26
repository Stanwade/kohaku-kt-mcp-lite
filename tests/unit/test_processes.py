import asyncio

import pytest

from kt_mcp.config import Config
from kt_mcp.processes import execute, find_bash


async def test_python_unicode_stdout(tmp_path):
    result = await execute(Config(tmp_path), "python", "print('中文输出 ✓')")
    assert result["exit_code"] == 0
    assert result["output"].strip() == "中文输出 ✓"


async def test_process_cwd_exit_output_and_timeout(tmp_path):
    config = Config(tmp_path, max_output_bytes=32)
    result = await execute(
        config,
        "python",
        "from pathlib import Path; Path('ok').write_text('yes'); print('x'*100000)",
    )
    assert (tmp_path / "ok").read_text() == "yes"
    assert result["output"] == "x" * 32
    assert result["truncated"] and result["exit_code"] == 0
    failed = await execute(config, "python", "raise SystemExit(7)")
    assert failed["exit_code"] == 7
    timed = await execute(config, "python", "import time; time.sleep(30)", 0.1)
    assert "timed out" in timed["error"]
    assert timed["exit_code"] != 0
    first = await execute(config, "python", "x = 10")
    second = await execute(config, "python", "print(x)")
    assert first["exit_code"] == 0
    assert second["exit_code"] != 0


async def test_cancel_reaps_running_process_tree(tmp_path):
    marker = tmp_path / "child-finished"
    child_source = "import time; from pathlib import Path; time.sleep(1.5); Path('child-finished').touch()"
    source = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"subprocess.Popen([sys.executable, '-c', {child_source!r}]); "
        "Path('started').touch(); time.sleep(30)"
    )
    task = asyncio.create_task(execute(Config(tmp_path), "python", source))
    try:
        for _ in range(100):
            if (tmp_path / "started").exists():
                break
            await asyncio.sleep(0.02)
        assert (tmp_path / "started").exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.8)
        assert not marker.exists()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_bash_runs_in_workspace(tmp_path):
    try:
        find_bash()
    except ValueError:
        pytest.skip("Bash is not installed")
    result = await execute(
        Config(tmp_path), "bash", "printf hello > bash-file; printf done"
    )
    assert result["exit_code"] == 0
    assert result["output"] == "done"
    assert (tmp_path / "bash-file").read_text() == "hello"


async def test_cancellation_while_spawning_does_not_orphan_command(tmp_path):
    source = (
        "import time; from pathlib import Path; time.sleep(0.5); Path('orphan').touch()"
    )
    task = asyncio.create_task(execute(Config(tmp_path), "python", source))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.8)
    assert not (tmp_path / "orphan").exists()
