import hashlib
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

import kt_mcp.tunnel as module
from kt_mcp.tunnel import Tunnel, find_cloudflared, release_asset


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Windows", "AMD64", "cloudflared-windows-amd64.exe"),
        ("Darwin", "arm64", "cloudflared-darwin-arm64.tgz"),
        ("Linux", "x86_64", "cloudflared-linux-amd64"),
    ],
)
def test_release_asset(system, machine, expected):
    assert release_asset(system, machine) == expected


@pytest.mark.parametrize("bad_digest", [False, True])
def test_verified_download(tmp_path, monkeypatch, bad_digest):
    payload = b"test-executable"
    name = release_asset(module.platform.system(), module.platform.machine())
    if name.endswith(".tgz"):
        pytest.skip("test fixture uses an unarchived executable")
    digest = hashlib.sha256(payload if not bad_digest else b"tampered").hexdigest()

    def respond(request):
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json={
                    "tag_name": "test",
                    "assets": [
                        {
                            "name": name,
                            "digest": "sha256:" + digest,
                            "browser_download_url": "https://github.com/test/asset",
                        }
                    ],
                },
            )
        return httpx.Response(200, content=payload)

    client_class = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    monkeypatch.setattr(module, "data_directory", lambda: tmp_path)
    if bad_digest:
        with pytest.raises(ValueError, match="SHA256"):
            find_cloudflared("", lambda _: None, threading.Event())
        assert not list((tmp_path / "bin").iterdir())
    else:
        binary = find_cloudflared("", lambda _: None, threading.Event())
        assert Path(binary).read_bytes() == payload
        assert find_cloudflared("", lambda _: None, threading.Event()) == binary


@pytest.mark.parametrize("token", ["", "connector-secret"])
def test_connector_arguments_url_and_cleanup(monkeypatch, token):
    actual_popen = subprocess.Popen
    launched = {}

    def fake_popen(command, **kwargs):
        launched.update(command=command, **kwargs)
        code = "import time; print('https://sample-mcp.trycloudflare.com', flush=True); print('Registered tunnel connection', flush=True); time.sleep(30)"
        return actual_popen([sys.executable, "-u", "-c", code], **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("TUNNEL_TOKEN", "inherited-secret")
    monkeypatch.setenv("TUNNEL_TOKEN_FILE", "inherited-file")
    tunnel = Tunnel(lambda _: None)
    try:
        tunnel.start("cloudflared", 8765, token)
        assert tunnel.connected.wait(5)
        assert tunnel.url == "https://sample-mcp.trycloudflare.com"
        assert "inherited-secret" not in str(launched)
        assert "TUNNEL_TOKEN_FILE" not in launched["env"]
        if token:
            assert token not in launched["command"]
            assert launched["env"]["TUNNEL_TOKEN"] == token
            assert launched["command"][-1] == "run"
        else:
            assert "TUNNEL_TOKEN" not in launched["env"]
            assert launched["command"][-2:] == ["--url", "http://127.0.0.1:8765"]
        config = Path(launched["command"][2])
        assert config.read_text().strip() == "{}"
    finally:
        tunnel.stop()
    assert tunnel.process.poll() is not None
    assert not tunnel.reader.is_alive()
    assert not config.exists()
