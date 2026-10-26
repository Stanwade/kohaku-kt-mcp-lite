"""Compose standalone file tools and command jobs without the KT agent runtime."""

import asyncio

from kt_mcp.config import Config
from kt_mcp.files import Files
from kt_mcp.jobs import Jobs
from kt_mcp.search import Search


class Runtime:
    """Own workspace baselines, file serialization, and command lifetime."""

    def __init__(self, config: Config):
        self.config = config
        self.files = Files(config)
        self.search = Search(config)
        self.jobs = Jobs(config)
        self._files_lock = asyncio.Lock()
        self._closed = False

    async def call(self, name: str, arguments: dict) -> dict:
        if self._closed:
            raise ValueError("Runtime is closed")
        if name in {"read", "write", "edit", "multi_edit"}:
            async with self._files_lock:
                task = asyncio.create_task(
                    asyncio.to_thread(getattr(self.files, name), **arguments)
                )
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
        if name in {"glob", "grep", "tree"}:
            return await asyncio.to_thread(getattr(self.search, name), **arguments)
        if name in {"bash", "python"}:
            args = dict(arguments)
            source = args.pop("command" if name == "bash" else "code")
            return await self.jobs.start(name, source, **{"timeout": 60, **args})
        match name:
            case "job_status":
                return self.jobs.status(**arguments)
            case "job_wait":
                return await self.jobs.wait(**arguments)
            case "job_cancel":
                return await self.jobs.cancel(**arguments)
            case _:
                raise ValueError(f"Unknown tool: {name}")

    async def __aenter__(self):
        if self._closed:
            raise ValueError("Runtime is closed")
        return self

    async def __aexit__(self, *exc):
        self._closed = True
        await self.jobs.close()
