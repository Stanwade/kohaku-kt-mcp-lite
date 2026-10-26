import pytest

from kt_mcp.config import Config
from kt_mcp.search import Search


def test_search_limits_and_results(tmp_path):
    (tmp_path / "root.py").write_text("alpha\nbeta\nALPHA\n")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/child.py").write_text("alpha\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/hidden.py").write_text("alpha\n")
    (tmp_path / "binary").write_bytes(b"\0alpha")
    search = Search(Config(tmp_path))
    assert search.glob("**/*.py")["files"] == ["root.py", "nested/child.py"]
    assert search.glob("**/*.py", limit=1) == {"files": ["root.py"], "truncated": True}
    result = search.grep("^alpha$", ignore_case=True)
    assert [(m["path"], m["line"]) for m in result["matches"]] == [
        ("root.py", 1),
        ("root.py", 3),
        ("nested/child.py", 1),
    ]
    assert result["skipped_files"] == 1
    assert search.grep("alpha", limit=1)["truncated"]
    assert search.grep("alpha", path="root.py")["matches"][0]["line"] == 1
    assert search.tree(depth=1)["entries"] == ["nested/", "binary", "root.py"]
    assert "nested/child.py" not in search.tree(depth=1)["entries"]
    assert search.tree(limit=1)["truncated"]
    with pytest.raises(ValueError, match="Scan limit"):
        Search(Config(tmp_path, max_scan_entries=1)).glob("missing")
    with pytest.raises(ValueError, match="existing directory"):
        search.tree("missing")
