import json
import os
import queue
import signal
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import kt_mcp.daemon as module
from kt_mcp.daemon import (
    DaemonConfig,
    client_url,
    default_config_path,
    load_config,
    save_config,
    startup_url,
)
from kt_mcp.desktop import DesktopSettings

TOKEN = "test-daemon-token-" + "a" * 32


@pytest.fixture(autouse=True)
def isolated_tokens(monkeypatch):
    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("TUNNEL_TOKEN", raising=False)


@pytest.fixture
def config(tmp_path):
    return DaemonConfig(DesktopSettings(str(tmp_path), mode="local"), TOKEN)


def test_default_config_path_honors_absolute_xdg_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config space"))
    assert default_config_path() == tmp_path / "config space/kt-mcp/config.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    assert default_config_path() == Path.home() / ".config/kt-mcp/config.json"


def test_private_config_round_trip_and_no_clobber(tmp_path, config):
    path = tmp_path / "private/config.json"
    save_config(config, path)
    original = path.read_bytes()
    assert load_config(path) == config
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    changed = replace(config, token="b" * 48)
    with pytest.raises(FileExistsError):
        save_config(changed, path)
    assert path.read_bytes() == original
    save_config(changed, path, replace=True)
    assert load_config(path) == changed
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission enforcement")
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_readable_config_is_rejected(tmp_path, config, mode):
    path = tmp_path / "config.json"
    save_config(config, path)
    path.chmod(mode)
    with pytest.raises(ValueError, match="private|600"):
        load_config(path)


@pytest.mark.parametrize(
    "patch",
    [
        {"version": 2},
        {"version": True},
        {"workspace": ""},
        {"workspace": "relative/workspace"},
        {"port": 0},
        {"port": True},
        {"mode": "unknown"},
        {"token": "short"},
        {"token": None},
        {"unknown": "field"},
        {"public_origin": True},
        {"mode": "named", "public_origin": "https://mcp.example.com"},
        {
            "mode": "named",
            "public_origin": "http://mcp.example.com",
            "tunnel_token": "connector-token",
        },
        {
            "mode": "named",
            "public_origin": "https://mcp.example.com/path",
            "tunnel_token": "connector-token",
        },
        {
            "mode": "named",
            "public_origin": "https://mcp.\texample.com",
            "tunnel_token": "connector-token",
        },
        {
            "mode": "named",
            "public_origin": "https://mcp.\x1bexample.com",
            "tunnel_token": "connector-token",
        },
        {
            "mode": "named",
            "public_origin": "https://mcp.example.com\\unexpected",
            "tunnel_token": "connector-token",
        },
    ],
)
def test_invalid_config_is_rejected(tmp_path, config, patch):
    path = tmp_path / "config.json"
    save_config(config, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(patch)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises((ValueError, TypeError)):
        load_config(path)


@pytest.mark.parametrize("payload", ["{broken", "[]", "null", '{"workspace":"x"}'])
def test_malformed_config_is_not_silently_defaulted(tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ValueError):
        load_config(path)


def test_invalid_save_leaves_existing_config_unchanged(tmp_path, config):
    path = tmp_path / "config.json"
    save_config(config, path)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        save_config(replace(config, token="short"), path, replace=True)
    assert path.read_bytes() == original


def test_environment_tokens_override_file_without_rewriting(
    tmp_path, config, monkeypatch
):
    settings = replace(
        config.settings, mode="named", public_origin="https://mcp.example.com"
    )
    config = DaemonConfig(settings, TOKEN, "stored-tunnel-token")
    path = tmp_path / "config.json"
    save_config(config, path)
    original = path.read_bytes()
    monkeypatch.setenv("KT_MCP_TOKEN", "environment-token-" + "c" * 32)
    monkeypatch.setenv("TUNNEL_TOKEN", "environment-tunnel-token")
    loaded = load_config(path)
    assert loaded.token == os.environ["KT_MCP_TOKEN"]
    assert loaded.tunnel_token == "environment-tunnel-token"
    assert path.read_bytes() == original


def test_client_url_for_local_and_named_config(tmp_path, config):
    path = tmp_path / "config.json"
    assert client_url(config, path) == "http://127.0.0.1:8765/mcp/" + TOKEN
    named = replace(
        config,
        settings=replace(
            config.settings, mode="named", public_origin="https://mcp.example.com"
        ),
        tunnel_token="connector-token",
    )
    assert client_url(named, path) == "https://mcp.example.com/mcp/" + TOKEN


@pytest.mark.skipif(os.name != "posix", reason="POSIX process liveness checks")
def test_quick_client_url_requires_live_current_status(tmp_path, config, monkeypatch):
    config = replace(config, settings=replace(config.settings, mode="quick"))
    path = tmp_path / "config.json"
    status_path = path.with_suffix(".status.json")
    with pytest.raises(ValueError, match="not ready"):
        client_url(config, path)
    url = "https://test-daemon.trycloudflare.com/mcp/" + TOKEN
    calls = []
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    status_path.write_text(json.dumps({"pid": 12345, "url": url}), encoding="utf-8")
    assert client_url(config, path) == url
    assert calls == [(12345, 0)]
    tilde_path = Path("~") / os.path.relpath(path, Path.home())
    assert client_url(config, tilde_path) == url
    status_path.write_text(
        json.dumps({"pid": 12345, "url": url + "-old-token"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="not ready"):
        client_url(config, path)

    def missing_process(pid, sig):
        raise ProcessLookupError(pid)

    status_path.write_text(json.dumps({"pid": 12345, "url": url}), encoding="utf-8")
    monkeypatch.setattr(module.os, "kill", missing_process)
    with pytest.raises(ValueError, match="not ready"):
        client_url(config, path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process liveness checks")
@pytest.mark.parametrize("mode", ["local", "named", "quick"])
def test_startup_record_must_belong_to_the_selected_service(
    tmp_path, config, monkeypatch, mode
):
    config = replace(
        config,
        settings=replace(
            config.settings,
            mode=mode,
            public_origin="https://mcp.example.com" if mode == "named" else "",
        ),
        tunnel_token="connector-token" if mode == "named" else "",
    )
    path = tmp_path / "config.json"
    status_path = path.with_suffix(".status.json")
    url = (
        "https://test-daemon.trycloudflare.com/mcp/" + TOKEN
        if mode == "quick"
        else client_url(config, path)
    )
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: None)
    status_path.write_text(json.dumps({"pid": 12345, "url": url}), encoding="utf-8")

    assert startup_url(config, path, expected_pid=12345) == url
    with pytest.raises(ValueError, match="startup is not confirmed"):
        startup_url(config, path, expected_pid=98765)

    # A changed local port/domain/token must not reuse the previous URL as a
    # confirmation that the edited configuration was loaded by the service.
    changed = replace(config, token="changed-token-" + "b" * 32)
    with pytest.raises(ValueError, match="startup is not confirmed"):
        startup_url(changed, path, expected_pid=12345)
    if mode != "quick":
        settings_change = (
            {"port": 9123}
            if mode == "local"
            else {"public_origin": "https://new.example.com"}
        )
        changed = replace(
            config,
            settings=replace(config.settings, **settings_change),
        )
        with pytest.raises(ValueError, match="startup is not confirmed"):
            startup_url(changed, path, expected_pid=12345)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process liveness checks")
@pytest.mark.parametrize(
    "base",
    [
        "http://test-daemon.trycloudflare.com",
        "https://user:password@test-daemon.trycloudflare.com",
        "https://test-daemon.trycloudflare.com\\unexpected",
        "https://test-daemon.\ttrycloudflare.com",
        "https://test-daemon.\x1btrycloudflare.com",
        "https://test-daemon.trycloudflare.com:invalid",
        "https://test-daemon.trycloudflare.com/?next=",
        "https://test-daemon.trycloudflare.com/#next=",
    ],
)
def test_quick_status_rejects_non_copyable_or_credentialed_urls(
    tmp_path, config, monkeypatch, base
):
    config = replace(config, settings=replace(config.settings, mode="quick"))
    path = tmp_path / "config.json"
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: None)
    path.with_suffix(".status.json").write_text(
        json.dumps({"pid": 12345, "url": base + "/mcp/" + TOKEN}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="not ready") as error:
        client_url(config, path)
    assert TOKEN not in str(error.value)
    assert base not in str(error.value)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux daemon lifecycle")
def test_shutdown_during_start_is_not_lost(tmp_path, config, monkeypatch):
    path = tmp_path / "config.json"
    save_config(config, path)
    previous = signal.getsignal(signal.SIGTERM)

    class StartupRace:
        worker = None

        def __init__(self):
            self.events = queue.Queue()
            self.cancelled = False

        def start(self, *args):
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
            # Reproduce DesktopService.start clearing its cancellation event.
            self.cancelled = False

        @property
        def running(self):
            assert self.cancelled, "SIGTERM was lost during service startup"
            return False

        def stop(self):
            self.cancelled = True

    service = StartupRace()
    monkeypatch.setattr(module, "DesktopService", lambda: service)
    assert module.run_daemon(path) == 0
    assert service.cancelled
    assert signal.getsignal(signal.SIGTERM) == previous
