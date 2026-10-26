"""Configure a single workspace and bounded tool execution."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Execution limits shared by the standalone tools."""

    workspace: Path
    allow_outside_workspace: bool = False
    max_file_bytes: int = 2 * 1024 * 1024
    max_output_bytes: int = 64 * 1024
    max_scan_entries: int = 10_000
    max_jobs: int = 64
    max_running_jobs: int = 8

    def __post_init__(self):
        workspace = self.workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        object.__setattr__(self, "workspace", workspace)
        for name in (
            "max_file_bytes",
            "max_output_bytes",
            "max_scan_entries",
            "max_jobs",
            "max_running_jobs",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_running_jobs > self.max_jobs:
            raise ValueError("max_running_jobs cannot exceed max_jobs")

    def resolve(self, path: str) -> Path:
        if not path:
            raise ValueError("path must not be empty")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        candidate = candidate.resolve()
        if not self.allow_outside_workspace and not candidate.is_relative_to(
            self.workspace
        ):
            raise ValueError("Path is outside the workspace")
        return candidate


def clip(text: str, maximum: int) -> tuple[str, bool]:
    """Limit UTF-8 output without splitting a character."""
    data = text.encode("utf-8")
    return data[:maximum].decode("utf-8", errors="ignore"), len(data) > maximum
