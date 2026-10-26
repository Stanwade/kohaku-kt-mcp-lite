import asyncio

import pytest

from kt_mcp.config import Config
from kt_mcp.jobs import Jobs


async def test_jobs_wait_cancel_eviction_and_shutdown(tmp_path):
    jobs = Jobs(Config(tmp_path, max_jobs=2, max_running_jobs=1))
    try:
        job = await jobs.start("python", "import time; time.sleep(30)", 60, True)
        job_id = job["job_id"]
        await jobs.wait(job_id, 0.05)
        assert jobs.status(job_id)["state"] == "running"
        for _ in range(2):
            waiter = asyncio.create_task(jobs.wait(job_id, 10))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        assert jobs.status(job_id)["state"] == "running"
        with pytest.raises(ValueError, match="Too many"):
            await jobs.start("python", "pass", 1)
        waiter = asyncio.create_task(jobs.wait(job_id, 10))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert jobs.status(job_id)["state"] == "running"
        assert (await jobs.cancel(job_id))["state"] == "cancelled"
        assert (await jobs.wait(job_id))["state"] == "cancelled"
        done = await jobs.start("python", "print('done')", 10)
        assert done["output"].strip() == "done"
        assert (await jobs.cancel(done["job_id"]))["state"] == "completed"
        failed = await jobs.start("python", "raise ValueError('broken')", 10)
        assert failed["exit_code"] != 0 and "broken" in failed["output"]
        with pytest.raises(ValueError, match="Unknown or expired"):
            jobs.status(job_id)
        pending = await jobs.start("python", "import time; time.sleep(30)", 60, True)
        await jobs.close()
        assert jobs.status(pending["job_id"])["state"] == "cancelled"
        with pytest.raises(ValueError, match="closed"):
            await jobs.start("python", "pass", 1)
    finally:
        await jobs.close()
