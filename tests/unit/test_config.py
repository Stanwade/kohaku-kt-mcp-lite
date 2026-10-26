import pytest

from kt_mcp.config import Config, clip


def test_workspace_resolution_and_boundaries(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    config = Config(root)
    assert config.resolve("new.txt") == root / "new.txt"
    with pytest.raises(ValueError, match="outside"):
        config.resolve("../outside.txt")
    with pytest.raises(ValueError, match="empty"):
        config.resolve("")
    assert Config(root, allow_outside_workspace=True).resolve("..") == tmp_path
    with pytest.raises(ValueError, match="existing directory"):
        Config(root / "missing")
    with pytest.raises(ValueError, match="positive integer"):
        Config(root, max_jobs=0)
    with pytest.raises(ValueError, match="cannot exceed"):
        Config(root, max_jobs=1)
    assert clip("中abc", 2) == ("", True)
    assert clip("中abc", 4) == ("中a", True)


def test_symlink_target_cannot_escape(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    try:
        (root / "link").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires OS privileges")
    with pytest.raises(ValueError, match="outside"):
        Config(root).resolve("link")
