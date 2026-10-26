import httpx
import pytest

from kt_mcp.auth import MCPPathAuth
from kt_mcp.config import Config
from kt_mcp.runtime import Runtime
from kt_mcp.server import create_app


async def test_auth_all_methods_and_host_origin_validation(tmp_path):
    token = "a" * 43
    endpoint = "/mcp/" + token
    app = create_app(Runtime(Config(tmp_path)), token=token)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client,
    ):
        for method in ("GET", "POST", "DELETE", "OPTIONS"):
            for path in (
                "/mcp",
                "/mcp/wrong",
                endpoint + "/",
                endpoint + "extra",
                "/mcp?token=" + token,
            ):
                response = await client.request(
                    method, path, headers={"Authorization": f"Bearer {token}"}
                )
                assert response.status_code == 404
                assert token not in response.text
                assert "www-authenticate" not in response.headers
        headers = {"Accept": "application/json, text/event-stream"}
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
        assert (
            await client.post(endpoint, headers=headers, json=body)
        ).status_code == 200
        assert (
            await client.post(
                endpoint, headers={**headers, "Host": "evil.invalid"}, json=body
            )
        ).status_code == 421
        assert (
            await client.post(
                endpoint,
                headers={**headers, "Origin": "https://evil.invalid"},
                json=body,
            )
        ).status_code == 403
    for bad in (
        "",
        "short",
        "中" * 32,
        "a" * 32 + "\n",
        "a" * 32 + "/",
        "a" * 32 + "?",
        "a" * 32 + " ",
        "a" * 129,
    ):
        with pytest.raises(ValueError, match="token"):
            MCPPathAuth(None, token=bad)


async def test_secret_path_redacted_before_downstream():
    observed = []

    async def downstream(scope, receive, send):
        observed.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    token = "abc_-123" * 4
    app = MCPPathAuth(downstream, token=token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (await client.post("/mcp/" + token + "?extra=hidden")).status_code == 204
    assert observed[0]["path"] == "/mcp"
    assert observed[0]["raw_path"] == b"/mcp"
    assert observed[0]["query_string"] == b""


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://example.com/",
        "https://u:p@example.com",
        "https://example.com?q=x",
        "https://example.com:bad",
    ],
)
def test_invalid_public_origin(tmp_path, origin):
    with pytest.raises(ValueError):
        create_app(Runtime(Config(tmp_path)), token="a" * 43, public_origin=origin)
