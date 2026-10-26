"""MCP stdio and authenticated Streamable HTTP adapters, extracted from KT."""

import json
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

from kt_mcp import __version__
from kt_mcp.auth import MCPPathAuth
from kt_mcp.catalog import tools
from kt_mcp.runtime import Runtime


def create_server(runtime: Runtime) -> Server:
    server = Server(
        "Kohaku KT MCP",
        version=__version__,
        website_url="https://github.com/Kohaku-Lab/KohakuTerrarium",
        instructions=(
            f"Standalone KohakuTerrarium workspace tools. Workspace: {runtime.config.workspace}. "
            "Read files before modifying existing contents. Stale reads require rereading. "
            "File tools restrict paths to the workspace by default. Commands run with the user's "
            "permissions and are not sandboxed. Python uses a fresh process for each call. "
            "Background jobs have no automatic completion notification; poll job_status/job_wait. "
            "Jobs and read baselines are shared by clients of this instance and lost on restart. "
            "Do not replay a write or command automatically after losing its reply."
        ),
    )

    @server.list_tools()
    async def list_tools():
        return tools()

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            data = await runtime.call(name, arguments or {})
        except (ValueError, OSError, UnicodeError) as exc:
            data = {"error": str(exc)}
        error = bool(data.get("error")) or data.get("exit_code") not in (None, 0)
        return CallToolResult(
            content=[
                TextContent(type="text", text=json.dumps(data, ensure_ascii=False))
            ],
            structuredContent=data,
            isError=error,
        )

    return server


async def serve_stdio(runtime: Runtime):
    async with runtime, stdio_server() as (read, write):
        server = create_server(runtime)
        await server.run(read, write, server.create_initialization_options())


class HTTPTransport:
    def __init__(self, manager):
        self.manager = manager

    async def __call__(self, scope, receive, send):
        await self.manager.handle_request(scope, receive, send)


def create_app(
    runtime: Runtime, *, token: str, port: int = 8765, public_origin: str = ""
) -> Starlette:
    """Create a loopback HTTP app with a shared token and DNS rebinding checks."""
    if not 1 <= port <= 65535:
        raise ValueError("Invalid listen port")
    MCPPathAuth(None, token=token)
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}"]
    origins = [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
    if public_origin:
        parsed = urlsplit(public_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "public_origin must be an HTTPS origin without path or credentials"
            )
        public_port = parsed.port
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        suffixes = ("", ":443") if public_port in (None, 443) else (f":{public_port}",)
        hosts.extend(host + suffix for suffix in suffixes)
        origins.extend("https://" + host + suffix for suffix in suffixes)
    manager = StreamableHTTPSessionManager(
        create_server(runtime),
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(
            allowed_hosts=hosts, allowed_origins=origins
        ),
    )

    @asynccontextmanager
    async def lifespan(app):
        async with runtime, manager.run():
            yield

    return Starlette(
        routes=[
            Route("/mcp", HTTPTransport(manager), methods=["GET", "POST", "DELETE"])
        ],
        middleware=[Middleware(MCPPathAuth, token=token)],
        lifespan=lifespan,
    )
