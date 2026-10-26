import pytest

from kt_mcp.config import Config
from kt_mcp.runtime import Runtime


async def test_runtime_dispatch_and_shutdown(tmp_path):
    runtime = Runtime(Config(tmp_path))
    async with runtime:
        await runtime.call("write", {"path": "a", "content": "before"})
        assert "before" in (await runtime.call("read", {"path": "a"}))["output"]
        await runtime.call(
            "edit", {"path": "a", "old_string": "before", "new_string": "after"}
        )
        assert (tmp_path / "a").read_text() == "after"
        assert (await runtime.call("glob", {"pattern": "a"}))["files"] == ["a"]
        assert (await runtime.call("grep", {"pattern": "after"}))["matches"][0][
            "line"
        ] == 1
        with pytest.raises(ValueError, match="Unknown tool"):
            await runtime.call("unknown", {})
        job = await runtime.call(
            "python", {"code": "import time; time.sleep(30)", "run_in_background": True}
        )
        assert (
            await runtime.call("job_wait", {"job_id": job["job_id"], "timeout": 0})
        )["state"] == "running"
    assert runtime.jobs.status(job["job_id"])["state"] == "cancelled"
    with pytest.raises(ValueError, match="closed"):
        await runtime.call("tree", {})
