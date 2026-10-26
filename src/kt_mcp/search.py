"""Bounded workspace discovery adapted from KT glob, grep, and tree tools."""

import fnmatch
import os
import re
from pathlib import Path

from kt_mcp.config import Config, clip

SKIP_DIRECTORIES = {".git", ".venv", "venv", "node_modules", "__pycache__"}


class Search:
    """List and search workspace files without following directory symlinks."""

    def __init__(self, config: Config):
        self.config = config

    def _entries(self, root: Path, *, depth: int = 100):
        if not root.is_dir():
            raise ValueError("Search path must be an existing directory")
        count = 0
        for current, directories, files in os.walk(root):
            parent = Path(current)
            level = len(parent.relative_to(root).parts)
            directories[:] = sorted(
                name
                for name in directories
                if name not in SKIP_DIRECTORIES and not (parent / name).is_symlink()
            )
            visible_directories = directories.copy()
            if level >= depth:
                directories[:] = []
            for name in [*visible_directories, *sorted(files)]:
                count += 1
                if count > self.config.max_scan_entries:
                    raise ValueError(
                        "Scan limit exceeded; choose a narrower search path"
                    )
                entry = parent / name
                if entry.is_symlink():
                    continue
                yield entry

    @staticmethod
    def _matches(path: str, pattern: str) -> bool:
        return fnmatch.fnmatchcase(path, pattern) or (
            pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])
        )

    def glob(self, pattern: str, path: str = ".", limit: int = 200) -> dict:
        root = self.config.resolve(path)
        matches = []
        for entry in self._entries(root):
            relative = entry.relative_to(root).as_posix()
            if entry.is_file() and self._matches(relative, pattern):
                if len(matches) == limit:
                    return {"files": matches, "truncated": True}
                matches.append(relative)
        return {"files": matches, "truncated": False}

    def grep(
        self,
        pattern: str,
        path: str = ".",
        glob: str = "**/*",
        ignore_case: bool = False,
        limit: int = 100,
    ) -> dict:
        root = self.config.resolve(path)
        expression = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        entries = [root] if root.is_file() else self._entries(root)
        matches = []
        output_bytes = 0
        skipped = 0
        for entry in entries:
            relative = (
                entry.name if root.is_file() else entry.relative_to(root).as_posix()
            )
            if not entry.is_file() or not self._matches(relative, glob):
                continue
            try:
                with entry.open("rb") as stream:
                    data = stream.read(self.config.max_file_bytes + 1)
                if len(data) > self.config.max_file_bytes or b"\0" in data:
                    skipped += 1
                    continue
                lines = data.decode("utf-8").splitlines()
            except (OSError, UnicodeError):
                skipped += 1
                continue
            for number, line in enumerate(lines, 1):
                if expression.search(line):
                    text, clipped = clip(line, 2000)
                    output_bytes += (
                        len(text.encode("utf-8")) + len(relative.encode("utf-8")) + 64
                    )
                    if (
                        len(matches) == limit
                        or output_bytes > self.config.max_output_bytes
                    ):
                        return {
                            "matches": matches,
                            "truncated": True,
                            "skipped_files": skipped,
                        }
                    matches.append(
                        {
                            "path": relative,
                            "line": number,
                            "text": text,
                            "truncated": clipped,
                        }
                    )
        return {"matches": matches, "truncated": False, "skipped_files": skipped}

    def tree(self, path: str = ".", depth: int = 3, limit: int = 200) -> dict:
        root = self.config.resolve(path)
        entries = []
        for entry in self._entries(root, depth=depth - 1):
            if len(entries) == limit:
                return {"entries": entries, "truncated": True}
            relative = entry.relative_to(root).as_posix()
            entries.append(relative + ("/" if entry.is_dir() else ""))
        return {"entries": entries, "truncated": False}
