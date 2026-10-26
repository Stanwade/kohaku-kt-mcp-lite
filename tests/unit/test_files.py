import os

import pytest

from kt_mcp.config import Config
from kt_mcp.files import Files


def test_read_write_edit_guards_and_atomic_multi_edit(tmp_path):
    path = tmp_path / "data.txt"
    path.write_bytes(b"one\r\ntwo\r\n")
    files = Files(Config(tmp_path))
    with pytest.raises(ValueError, match="Read the existing"):
        files.write("data.txt", "oops")
    assert path.read_bytes() == b"one\r\ntwo\r\n"
    result = files.read("data.txt", offset=1, limit=1)
    assert result["output"] == "     2→two"
    assert result["total_lines"] == 2
    timestamp = path.stat().st_mtime_ns
    path.write_bytes(b"new\r\ntwo\r\n")
    os.utime(path, ns=(timestamp, timestamp))
    with pytest.raises(ValueError, match="changed since"):
        files.edit("data.txt", "new", "old")
    files.read("data.txt")
    with pytest.raises(ValueError, match="not found"):
        files.multi_edit(
            "data.txt",
            [
                {"old_string": "new", "new_string": "ONE"},
                {"old_string": "missing", "new_string": "TWO"},
            ],
        )
    assert path.read_bytes() == b"new\r\ntwo\r\n"
    files.multi_edit(
        "data.txt",
        [
            {"old_string": "new", "new_string": "ONE"},
            {"old_string": "two", "new_string": "TWO"},
        ],
    )
    assert path.read_bytes() == b"ONE\r\nTWO\r\n"
    files.write("nested/new.txt", "a a")
    with pytest.raises(ValueError, match="ambiguous"):
        files.edit("nested/new.txt", "a", "b")
    with pytest.raises(ValueError, match="empty"):
        files.edit("nested/new.txt", "", "b")
    files.edit("nested/new.txt", "a", "b", replace_all=True)
    assert (tmp_path / "nested/new.txt").read_text() == "b b"
    assert not list(tmp_path.rglob(".kt-mcp-*"))


def test_file_limits_binary_utf8_and_missing_files(tmp_path):
    files = Files(Config(tmp_path, max_file_bytes=8, max_output_bytes=4))
    files.write("text", "中abc")
    assert files.read("text")["truncated"]
    with pytest.raises(ValueError, match="exceeds"):
        files.write("text", "longer than eight")
    assert (tmp_path / "text").read_text(encoding="utf-8") == "中abc"
    with pytest.raises(ValueError, match="missing"):
        files.edit("missing", "x", "y")
    with pytest.raises(FileNotFoundError):
        files.read("missing")
    (tmp_path / "binary").write_bytes(b"a\0b")
    with pytest.raises(ValueError, match="Binary"):
        files.read("binary")
    with pytest.raises(ValueError, match="Binary"):
        files.write("new", "a\0b")
    (tmp_path / "invalid").write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        files.read("invalid")
    (tmp_path / "large").write_bytes(b"a" * 9)
    with pytest.raises(ValueError, match="exceeds"):
        files.read("large")
