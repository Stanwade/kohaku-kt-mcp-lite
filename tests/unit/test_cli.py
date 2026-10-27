import pytest

from kt_mcp import __version__
from kt_mcp.cli import main


def test_help_version_missing_workspace_and_http_token(tmp_path, capsys, monkeypatch):
    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
    assert "KohakuTerrarium" in capsys.readouterr().out
    with pytest.raises(SystemExit) as version_exit:
        main(["--version"])
    assert version_exit.value.code == 0
    assert f"Kohaku KT MCP {__version__}" in capsys.readouterr().out
    with pytest.raises(SystemExit) as missing_exit:
        main(["--workspace", str(tmp_path / "missing")])
    assert missing_exit.value.code == 2
    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    with pytest.raises(SystemExit) as token_exit:
        main(["--workspace", str(tmp_path), "--transport", "http"])
    assert token_exit.value.code == 2
    assert "token" in capsys.readouterr().err
