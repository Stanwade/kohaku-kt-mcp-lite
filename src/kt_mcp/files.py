"""Guarded text file operations adapted from KohakuTerrarium's tools."""

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from kt_mcp.config import Config, clip


class Files:
    """Track content baselines and atomically replace workspace text files."""

    def __init__(self, config: Config):
        self.config = config
        self._reads: dict[Path, bytes] = {}

    def _bytes(self, path: Path) -> bytes:
        with path.open("rb") as stream:
            data = stream.read(self.config.max_file_bytes + 1)
        if len(data) > self.config.max_file_bytes:
            raise ValueError("File exceeds max_file_bytes")
        if b"\0" in data:
            raise ValueError("Binary files are not supported by text tools")
        return data

    @staticmethod
    def _digest(data: bytes) -> bytes:
        return hashlib.sha256(data).digest()

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> dict:
        target = self.config.resolve(path)
        data = self._bytes(target)
        lines = data.decode("utf-8").splitlines()
        selected = lines[offset : offset + limit]
        output, truncated = clip(
            "\n".join(f"{offset + i + 1:6}→{line}" for i, line in enumerate(selected)),
            self.config.max_output_bytes,
        )
        self._reads[target] = self._digest(data)
        return {
            "path": str(target),
            "output": output,
            "total_lines": len(lines),
            "truncated": truncated or offset + len(selected) < len(lines),
        }

    def _current(self, target: Path) -> bytes | None:
        if not target.exists():
            return None
        data = self._bytes(target)
        baseline = self._reads.get(target)
        if baseline is None:
            raise ValueError("Read the existing file before modifying it")
        if self._digest(data) != baseline:
            raise ValueError("File changed since last read; read it again")
        return data

    def _replace(self, target: Path, content: str, expected: bytes | None) -> dict:
        data = content.encode("utf-8")
        if len(data) > self.config.max_file_bytes:
            raise ValueError("Content exceeds max_file_bytes")
        if b"\0" in data:
            raise ValueError("Binary content is not supported by text tools")
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(target.stat().st_mode) if expected is not None else None
        fd, temporary = tempfile.mkstemp(prefix=".kt-mcp-", dir=target.parent)
        temporary = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            if mode is not None:
                temporary.chmod(mode)
            current = self._bytes(target) if target.exists() else None
            if current != expected:
                raise ValueError("File changed during operation; read it again")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        self._reads[target] = self._digest(data)
        return {"path": str(target), "bytes_written": len(data)}

    def write(self, path: str, content: str) -> dict:
        target = self.config.resolve(path)
        return self._replace(target, content, self._current(target))

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> dict:
        return self.multi_edit(
            path,
            [
                {
                    "old_string": old_string,
                    "new_string": new_string,
                    "replace_all": replace_all,
                }
            ],
        )

    def multi_edit(self, path: str, edits: list[dict]) -> dict:
        target = self.config.resolve(path)
        data = self._current(target)
        if data is None:
            raise ValueError("Cannot edit a missing file")
        content = data.decode("utf-8")
        for edit in edits:
            old, new = edit["old_string"], edit["new_string"]
            if not old:
                raise ValueError("old_string must not be empty")
            count = content.count(old)
            if count == 0:
                raise ValueError("old_string was not found; no edits applied")
            if count > 1 and not edit.get("replace_all", False):
                raise ValueError(
                    "old_string is ambiguous; use replace_all or more context"
                )
            content = content.replace(old, new, -1 if edit.get("replace_all") else 1)
        return self._replace(target, content, data)
