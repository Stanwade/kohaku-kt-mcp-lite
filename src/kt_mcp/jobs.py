"""Own foreground and retained background command tasks."""

import asyncio
import uuid
from dataclasses import dataclass

from kt_mcp.config import Config
from kt_mcp.processes import execute


@dataclass
class Job:
    job_id: str
    kind: str
    state: str = "running"
    result: dict | None = None
    task: asyncio.Task | None = None

    def snapshot(self) -> dict:
        return {
            "job_id": self.job_id,
            "tool": self.kind,
            "state": self.state,
            "result": self.result,
        }


class Jobs:
    """Keep bounded in-memory results and cancel all owned commands on shutdown."""

    def __init__(self, config: Config):
        self.config = config
        self._jobs: dict[str, Job] = {}
        self._closed = False

    async def _run(self, job: Job, source: str, timeout: float):
        try:
            job.result = await execute(self.config, job.kind, source, timeout)
            failed = job.result.get("error") or job.result.get("exit_code") != 0
            job.state = "error" if failed else "completed"
        except asyncio.CancelledError:
            job.state = "cancelled"
            raise
        except (OSError, ValueError) as exc:
            job.result = {"error": str(exc)}
            job.state = "error"

    async def start(
        self, kind: str, source: str, timeout: float, run_in_background: bool = False
    ) -> dict:
        if self._closed:
            raise ValueError("Runtime is closed")
        if (
            sum(job.state == "running" for job in self._jobs.values())
            >= self.config.max_running_jobs
        ):
            raise ValueError("Too many running jobs; wait for or cancel a job")
        if len(self._jobs) >= self.config.max_jobs:
            completed = next(
                (key for key, job in self._jobs.items() if job.state != "running"), None
            )
            if completed is None:
                raise ValueError("Job retention is full")
            del self._jobs[completed]
        job = Job(uuid.uuid4().hex, kind)
        self._jobs[job.job_id] = job
        job.task = asyncio.create_task(self._run(job, source, timeout))
        if run_in_background:
            return job.snapshot()
        await job.task
        return {"job_id": job.job_id, **job.result}

    def _get(self, job_id: str) -> Job:
        if job_id not in self._jobs:
            raise ValueError("Unknown or expired job_id; jobs are lost on restart")
        return self._jobs[job_id]

    def status(self, job_id: str | None = None) -> dict:
        if job_id is None:
            return {"jobs": [job.snapshot() for job in self._jobs.values()]}
        return self._get(job_id).snapshot()

    async def wait(self, job_id: str, timeout: float = 10) -> dict:
        job = self._get(job_id)
        await asyncio.wait({job.task}, timeout=timeout)
        return job.snapshot()

    async def cancel(self, job_id: str) -> dict:
        job = self._get(job_id)
        if not job.task.done():
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            job.state = "cancelled"
        return job.snapshot()

    async def close(self):
        self._closed = True
        await asyncio.gather(*(self.cancel(key) for key in self._jobs))
