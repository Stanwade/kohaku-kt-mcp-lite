import asyncio
import queue
import secrets
import socket
import time

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import kt_mcp.desktop as module
from kt_mcp.desktop import DesktopService, DesktopSettings


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_event(service, kind, value=None, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = service.events.get(timeout=0.1)
        except queue.Empty:
            continue
        if event[0] == "error" and kind != "error":
            pytest.fail(event[1])
        if event[0] == kind and (value is None or event[1] == value):
            return event[1]
    pytest.fail(f"Missing desktop event {kind}={value}")


@pytest.mark.parametrize("mode", ["local", "quick", "named"])
async def test_desktop_mcp_and_public_host(tmp_path, monkeypatch, mode):
    connectors = []

    def start(tunnel, executable, port, token=""):
        tunnel.url = "https://desktop-test.trycloudflare.com"
        tunnel.connected.set()
        connectors.append(token)

    monkeypatch.setattr(module, "find_cloudflared", lambda *args: "test-connector")
    monkeypatch.setattr(module.Tunnel, "start", start)
    port = free_port()
    token = secrets.token_urlsafe(32)
    origin = "https://mcp.example.com"
    settings = DesktopSettings(str(tmp_path), port, mode, origin)
    service = DesktopService()
    service.start(settings, token, "connector-token" if mode == "named" else "")
    try:
        url = await asyncio.to_thread(wait_event, service, "url")
        assert (
            url
            == {
                "local": f"http://127.0.0.1:{port}/mcp/{token}",
                "quick": "https://desktop-test.trycloudflare.com/mcp/" + token,
                "named": origin + "/mcp/" + token,
            }[mode]
        )
        local = f"http://127.0.0.1:{port}/mcp/{token}"
        headers = {}
        if mode != "local":
            headers.update(Host=url.split("/")[2], Origin=url.split("/mcp/")[0])
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(local.rsplit("/", 1)[0])).status_code == 404
            assert (await client.get(local + "wrong")).status_code == 404
            invalid = {
                **headers,
                "Host": "untrusted.example.com",
                "Accept": "application/json, text/event-stream",
            }
            assert (await client.get(local, headers=invalid)).status_code == 421
        async with (
            httpx.AsyncClient(trust_env=False, headers=headers) as http,
            streamable_http_client(local, http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            assert (await session.initialize()).serverInfo.name == "Kohaku KT MCP"
            assert len((await session.list_tools()).tools) == 12
            result = await session.call_tool(
                "write", {"path": "desktop.txt", "content": "桌面连接成功"}
            )
            assert not result.isError
            assert (tmp_path / "desktop.txt").read_text(
                encoding="utf-8"
            ) == "桌面连接成功"
        assert connectors == (
            [] if mode == "local" else ["connector-token" if mode == "named" else ""]
        )
    finally:
        service.stop()
        await asyncio.to_thread(service.worker.join, 15)
    assert not service.running
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))


def test_port_conflict_leaves_existing_listener(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        service = DesktopService()
        service.start(DesktopSettings(str(tmp_path), port, "local"), "x" * 32)
        assert wait_event(service, "error")
        service.worker.join(5)
        assert not service.running
        assert listener.getsockname()[1] == port


def test_cancel_pending_tunnel_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "find_cloudflared", lambda *args: "test-connector")
    monkeypatch.setattr(module.Tunnel, "start", lambda *args: None)
    service = DesktopService()
    settings = DesktopSettings(str(tmp_path), free_port(), "quick")
    service.start(settings, "x" * 32)
    wait_event(service, "state", "正在连接 Cloudflare…")
    service.stop()
    service.worker.join(5)
    assert not service.running
    # Same controller and port must be reusable after cancellation.
    service.start(DesktopSettings(str(tmp_path), settings.port, "local"), "x" * 32)
    try:
        wait_event(service, "state", "运行中")
    finally:
        service.stop()
        service.worker.join(10)
    assert not service.running
