"""Secret-path authentication for every HTTP request to the MCP app."""

import re
import secrets

from starlette.responses import PlainTextResponse


class MCPPathAuth:
    """Gate the exact access URL and redact its token before MCP dispatch."""

    def __init__(self, app, *, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            raise ValueError(
                "HTTP access token must contain 32–128 URL-safe characters (A-Z, a-z, 0-9, _ or -)"
            )
        self.app = app
        self.expected = ("/mcp/" + token).encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        if not secrets.compare_digest(scope["path"].encode("utf-8"), self.expected):
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return
        clean = dict(scope, path="/mcp", raw_path=b"/mcp", query_string=b"")
        await self.app(clean, receive, send)
